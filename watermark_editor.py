"""
Video Watermark Editor
=======================
A lightweight PySide6 desktop app for:
  - loading a source video and playing it back
  - loading watermark assets (image / gif / video clip)
  - dragging watermarks onto the video to position them
  - defining a start/end time range per watermark on a timeline
  - exporting the final composited video via FFmpeg

Requirements:
    pip install PySide6
    FFmpeg + ffprobe must be installed and available on PATH.
    (Windows: download a build from https://www.gyan.dev/ffmpeg/builds/,
     unzip, and add the "bin" folder to your PATH environment variable.)

Run:
    python watermark_editor.py

Notes:
  - The on-screen preview (drag to move, spin-boxes to scale) is an
    approximation for layout purposes. The actual pixel-accurate result
    is produced by FFmpeg at export time.
  - This is a first working version, not a polished product - if the
    exact PySide6 API names differ slightly on your installed version,
    the error messages will point at the line to fix.
"""

import sys
import os
import re
import json
import shutil
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List

from PySide6.QtCore import Qt, QUrl, QRectF, QPointF, QSize, QTimer, QThread, Signal, QMimeData
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QPen, QBrush, QFont, QDrag, QMovie, QImage
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QLabel,
    QSlider, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsItem, QSplitter, QMessageBox, QDoubleSpinBox, QGroupBox,
    QFormLayout, QAbstractItemView, QStyle, QProgressDialog, QSizePolicy,
    QScrollArea, QComboBox,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Watermark:
    id: int
    asset_path: str
    kind: str               # "image", "gif", "video"
    x: float = 0.05          # fraction of video width, top-left corner
    y: float = 0.05          # fraction of video height, top-left corner
    scale: float = 0.25      # watermark width as a fraction of video width
    start: float = 0.0       # seconds
    end: float = 5.0         # seconds
    label: str = ""
    item: Optional["WatermarkGraphicsItem"] = field(default=None, repr=False)


def classify_asset(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".gif":
        return "gif"
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
        return "image"
    if ext in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"):
        return "video"
    return "image"


def extract_thumbnail(path: str, kind: str) -> QPixmap:
    """Best-effort thumbnail for the asset list / overlay preview."""
    if kind == "image":
        pix = QPixmap(path)
        if not pix.isNull():
            return pix
    if kind == "gif":
        movie = QMovie(path)
        if movie.isValid():
            movie.jumpToFrame(0)
            pix = movie.currentPixmap()
            if not pix.isNull():
                return pix
    if kind == "video":
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            tmp = os.path.join(tempfile.gettempdir(), f"_wm_thumb_{os.getpid()}.png")
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-ss", "0", "-i", path, "-frames:v", "1", tmp],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
                )
                pix = QPixmap(tmp)
                if os.path.exists(tmp):
                    os.remove(tmp)
                if not pix.isNull():
                    return pix
            except Exception:
                pass
    # Fallback placeholder
    pix = QPixmap(120, 80)
    pix.fill(QColor("#555"))
    return pix


# ---------------------------------------------------------------------------
# Asset list (drag source)
# ---------------------------------------------------------------------------

ASSET_MIME_TYPE = "application/x-watermark-asset-index"


class AssetListWidget(QListWidget):
    """List of loaded watermark assets. Items can be dragged onto the video."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(__import__("PySide6.QtCore", fromlist=["QSize"]).QSize(64, 48))
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if item is None:
            return
        index = self.row(item)
        mime = QMimeData()
        mime.setData(ASSET_MIME_TYPE, str(index).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pix = item.icon().pixmap(64, 48)
        if not pix.isNull():
            drag.setPixmap(pix)
        drag.exec(Qt.DropAction.CopyAction)


# ---------------------------------------------------------------------------
# Draggable / resizable overlay item on the video
# ---------------------------------------------------------------------------

class WatermarkGraphicsItem(QGraphicsPixmapItem):
    """A movable pixmap representing a watermark's on-screen position."""

    def __init__(self, watermark: "Watermark", pixmap: QPixmap):
        super().__init__(pixmap)
        self.watermark = watermark
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            new_pos = value
            rect = self.scene().sceneRect()
            w, h = self.pixmap().width(), self.pixmap().height()
            new_pos.setX(min(max(new_pos.x(), rect.left()), rect.right() - w))
            new_pos.setY(min(max(new_pos.y(), rect.top()), rect.bottom() - h))
            return new_pos
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and self.scene():
            rect = self.scene().sceneRect()
            if rect.width() > 0 and rect.height() > 0:
                self.watermark.x = self.pos().x() / rect.width()
                self.watermark.y = self.pos().y() / rect.height()
        return super().itemChange(change, value)


# ---------------------------------------------------------------------------
# Video view (plays the source video, accepts drops for video + watermarks)
# ---------------------------------------------------------------------------

class VideoGraphicsView(QGraphicsView):
    video_dropped = Signal(str)          # local file path of a dropped video
    watermark_asset_dropped = Signal(int, QPointF)  # asset index, scene pos

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setAcceptDrops(True)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setStyleSheet("background-color: #1e1e1e; border: none;")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(ASSET_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(ASSET_MIME_TYPE):
            index = int(bytes(mime.data(ASSET_MIME_TYPE)).decode("utf-8"))
            scene_pos = self.mapToScene(event.position().toPoint())
            self.watermark_asset_dropped.emit(index, scene_pos)
            event.acceptProposedAction()
            return
        if mime.hasUrls():
            path = mime.urls()[0].toLocalFile()
            if path:
                self.video_dropped.emit(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.scene() is not None:
            rect = self.scene().sceneRect()
            if rect.width() > 0 and rect.height() > 0:
                self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)


# ---------------------------------------------------------------------------
# Timeline widget
# ---------------------------------------------------------------------------

ROW_HEIGHT = 30
RULER_HEIGHT = 26
LEFT_MARGIN = 130
HANDLE_WIDTH = 6


class TimelineWidget(QWidget):
    """
    Shows a ruler + one row per watermark. Each row has a draggable segment
    representing [start, end]. Dragging the middle moves the whole segment;
    dragging near an edge resizes that edge. Clicking the ruler seeks.
    """

    seek_requested = Signal(float)
    watermarks_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.watermarks: List[Watermark] = []
        self.duration = 0.0
        self.position = 0.0
        self.pixels_per_second = 40.0
        self.setMinimumHeight(RULER_HEIGHT + 4 * ROW_HEIGHT)
        self.setMouseTracking(True)
        self._drag_mode = None    # "move" | "resize_start" | "resize_end" | "seek"
        self._drag_wm = None
        self._drag_anchor_x = 0
        self._drag_orig_start = 0.0
        self._drag_orig_end = 0.0

    # -- coordinate helpers --------------------------------------------
    def time_to_x(self, t):
        return LEFT_MARGIN + t * self.pixels_per_second

    def x_to_time(self, x):
        if self.pixels_per_second <= 0:
            return 0.0
        t = (x - LEFT_MARGIN) / self.pixels_per_second
        return max(0.0, min(self.duration, t))

    def row_rect(self, index):
        top = RULER_HEIGHT + index * ROW_HEIGHT
        return QRectF(0, top, self.width(), ROW_HEIGHT)

    def sizeHint(self):
        width = int(LEFT_MARGIN + self.duration * self.pixels_per_second + 60)
        height = RULER_HEIGHT + max(4, len(self.watermarks)) * ROW_HEIGHT + 10
        return QSize(max(width, 400), height)

    def set_zoom(self, pixels_per_second: float):
        self.pixels_per_second = max(2.0, pixels_per_second)
        self.refresh_size()

    def refresh_size(self):
        """Call whenever duration, zoom, or the watermark list changes."""
        self.resize(self.sizeHint())
        self.updateGeometry()
        self.update()

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#252525"))

        # Ruler
        painter.setPen(QPen(QColor("#999"), 1))
        painter.setFont(QFont("Segoe UI", 8))
        if self.duration > 0:
            step = self._nice_step()
            t = 0.0
            while t <= self.duration:
                x = self.time_to_x(t)
                painter.drawLine(int(x), RULER_HEIGHT - 6, int(x), RULER_HEIGHT)
                painter.drawText(int(x) + 2, 12, self._fmt_time(t))
                t += step

        # Rows
        for i, wm in enumerate(self.watermarks):
            row = self.row_rect(i)
            painter.setPen(QPen(QColor("#444")))
            painter.drawLine(0, int(row.top()), self.width(), int(row.top()))
            painter.setPen(QColor("#ddd"))
            painter.drawText(8, int(row.top()) + ROW_HEIGHT - 10,
                              (wm.label or os.path.basename(wm.asset_path))[:16])

            x1 = self.time_to_x(wm.start)
            x2 = self.time_to_x(wm.end)
            seg_rect = QRectF(x1, row.top() + 4, max(2, x2 - x1), ROW_HEIGHT - 8)
            color = QColor("#4a90d9") if wm is self._drag_wm else QColor("#3a6ea5")
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor("#1c3a5c")))
            painter.drawRoundedRect(seg_rect, 4, 4)
            # resize handles
            painter.setBrush(QBrush(QColor("#dfeeff")))
            painter.drawRect(QRectF(x1, seg_rect.top(), HANDLE_WIDTH, seg_rect.height()))
            painter.drawRect(QRectF(x2 - HANDLE_WIDTH, seg_rect.top(), HANDLE_WIDTH, seg_rect.height()))

        # Playhead
        if self.duration > 0:
            px = self.time_to_x(self.position)
            painter.setPen(QPen(QColor("#e05c5c"), 2))
            painter.drawLine(int(px), 0, int(px), self.height())

    def _nice_step(self):
        # aim for ~80px between ticks
        target = 80 / max(self.pixels_per_second, 0.0001)
        steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
        for s in steps:
            if s >= target:
                return s
        return steps[-1]

    @staticmethod
    def _fmt_time(t):
        t = max(0, t)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    # -- interaction -------------------------------------------------------
    def mousePressEvent(self, event):
        x = event.position().x()
        y = event.position().y()
        if y <= RULER_HEIGHT:
            self._drag_mode = "seek"
            self.seek_requested.emit(self.x_to_time(x))
            return
        row_index = int((y - RULER_HEIGHT) // ROW_HEIGHT)
        if 0 <= row_index < len(self.watermarks):
            wm = self.watermarks[row_index]
            x1, x2 = self.time_to_x(wm.start), self.time_to_x(wm.end)
            self._drag_wm = wm
            self._drag_orig_start = wm.start
            self._drag_orig_end = wm.end
            self._drag_anchor_x = x
            if abs(x - x1) <= HANDLE_WIDTH + 2:
                self._drag_mode = "resize_start"
            elif abs(x - x2) <= HANDLE_WIDTH + 2:
                self._drag_mode = "resize_end"
            elif x1 <= x <= x2:
                self._drag_mode = "move"
            else:
                self._drag_mode = None
                self._drag_wm = None
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag_mode == "seek":
            self.seek_requested.emit(self.x_to_time(event.position().x()))
            return
        if self._drag_wm is None or self._drag_mode is None:
            return
        dx = event.position().x() - self._drag_anchor_x
        dt = dx / max(self.pixels_per_second, 0.0001)
        wm = self._drag_wm
        if self._drag_mode == "move":
            duration = self._drag_orig_end - self._drag_orig_start
            new_start = max(0.0, min(self.duration - duration, self._drag_orig_start + dt))
            wm.start = new_start
            wm.end = new_start + duration
        elif self._drag_mode == "resize_start":
            wm.start = max(0.0, min(self._drag_orig_end - 0.1, self._drag_orig_start + dt))
        elif self._drag_mode == "resize_end":
            wm.end = min(self.duration, max(self._drag_orig_start + 0.1, self._drag_orig_end + dt))
        self.update()
        self.watermarks_changed.emit()

    def mouseReleaseEvent(self, event):
        self._drag_mode = None
        self._drag_wm = None
        self.update()


# ---------------------------------------------------------------------------
# Export worker (runs ffmpeg on a background thread, not the GUI thread)
# ---------------------------------------------------------------------------

class ExportWorker(QThread):
    """
    Runs ffmpeg via subprocess.Popen and reads its stderr on a dedicated
    background thread using a plain blocking loop.

    This matters specifically on CPU-constrained machines: software x264
    encoding can consume every available core, starving the GUI thread of
    CPU time. If output-reading were tied to the GUI thread's event loop
    (as QProcess's signals are), a starved GUI thread means the pipe never
    gets drained, its buffer fills up, and ffmpeg blocks trying to write
    more progress output - the whole export just stops. A dedicated OS
    thread doing a blocking read is scheduled independently of the GUI
    event loop, so it keeps draining the pipe even when the GUI thread
    can't get a time slice.
    """
    progress_line = Signal(str)
    finished_export = Signal(int, str)

    def __init__(self, cmd, parent=None):
        super().__init__(parent)
        self.cmd = cmd

    def run(self):
        log_tail = deque(maxlen=200)
        try:
            proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.DEVNULL,   # explicitly discarded, never buffered/blocked on
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            # ffmpeg updates its status line with '\r', not '\n' - plain line
            # iteration only splits on '\n' and would silently buffer many
            # progress updates into one giant "line", making the UI look
            # like it freezes and then jumps. Read raw chunks and split on
            # both so progress is reported as it actually happens.
            buf = ""
            while True:
                chunk = proc.stderr.read(256)
                if not chunk:
                    break
                buf += chunk
                while True:
                    idx = min((p for p in (buf.find("\n"), buf.find("\r")) if p != -1), default=-1)
                    if idx == -1:
                        break
                    line, buf = buf[:idx], buf[idx + 1:]
                    if line:
                        log_tail.append(line)
                        self.progress_line.emit(line)
            proc.wait()
            self.finished_export.emit(proc.returncode, "\n".join(log_tail))
        except Exception as e:
            self.finished_export.emit(-1, str(e))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Watermark Editor")
        self.resize(1200, 800)

        self.video_path: Optional[str] = None
        self.video_width = 0
        self.video_height = 0
        self.watermarks: List[Watermark] = []
        self._next_id = 1

        # -- media player / video scene --------------------------------
        self.scene = QGraphicsScene(self)
        self.video_item = QGraphicsVideoItem()
        self.scene.addItem(self.video_item)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_item)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.video_item.nativeSizeChanged.connect(self._on_native_size_changed)

        self.view = VideoGraphicsView(self.scene, self)
        self.view.video_dropped.connect(self.load_video)
        self.view.watermark_asset_dropped.connect(self._on_asset_dropped)

        # -- transport controls -------------------------------------------
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self._toggle_play)
        self.time_label = QLabel("0:00 / 0:00")
        self.open_video_btn = QPushButton("Open Video...")
        self.open_video_btn.clicked.connect(self._browse_video)

        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(5, 400)
        self.zoom_slider.setValue(40)
        self.zoom_slider.setFixedWidth(150)
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        fit_btn = QPushButton("Fit")
        fit_btn.setToolTip("Zoom the timeline to fit the whole video")
        fit_btn.clicked.connect(self._fit_timeline_zoom)

        transport = QHBoxLayout()
        transport.addWidget(self.open_video_btn)
        transport.addWidget(self.play_btn)
        transport.addWidget(self.time_label)
        transport.addStretch()
        transport.addWidget(QLabel("Zoom:"))
        transport.addWidget(self.zoom_slider)
        transport.addWidget(fit_btn)

        # -- timeline -------------------------------------------------------
        self.timeline = TimelineWidget(self)
        self.timeline.seek_requested.connect(self._seek_to)
        self.timeline.watermarks_changed.connect(self._refresh_overlay_visibility)

        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidget(self.timeline)
        self.timeline_scroll.setWidgetResizable(False)
        self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(4, 4, 4, 4)
        center_layout.addWidget(self.view, stretch=3)
        center_layout.addLayout(transport)
        center_layout.addWidget(self.timeline_scroll, stretch=1)

        # -- right panel: assets + properties -------------------------------
        self.asset_list = AssetListWidget(self)
        self.asset_list.currentRowChanged.connect(self._on_asset_selected)
        add_asset_btn = QPushButton("Add Watermark Asset...")
        add_asset_btn.clicked.connect(self._browse_asset)

        assets_box = QGroupBox("Watermark Assets  (drag one onto the video)")
        assets_layout = QVBoxLayout(assets_box)
        assets_layout.addWidget(self.asset_list)
        assets_layout.addWidget(add_asset_btn)

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.02, 2.0)
        self.scale_spin.setSingleStep(0.05)
        self.scale_spin.setValue(0.25)
        self.scale_spin.valueChanged.connect(self._on_scale_changed)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 100000.0)
        self.start_spin.setSuffix(" s")
        self.start_spin.valueChanged.connect(self._on_start_changed)

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 100000.0)
        self.end_spin.setSuffix(" s")
        self.end_spin.valueChanged.connect(self._on_end_changed)

        self.remove_btn = QPushButton("Remove Selected Watermark")
        self.remove_btn.clicked.connect(self._remove_selected_watermark)

        props_box = QGroupBox("Selected Watermark")
        props_form = QFormLayout(props_box)
        props_form.addRow("Width (fraction of video):", self.scale_spin)
        props_form.addRow("Start:", self.start_spin)
        props_form.addRow("End:", self.end_spin)
        props_form.addRow(self.remove_btn)
        
        # ---------- Encoding settings box ----------
        self.rate_mode_combo = QComboBox()
        self.rate_mode_combo.addItems([
            "CRF (Static Quality - recommend)",
            "Bitrate (Static Size)",
        ])
        self.rate_mode_combo.currentIndexChanged.connect(self._on_rate_mode_changed)

        self.crf_spin = QDoubleSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setSingleStep(1)
        self.crf_spin.setValue(23)
        self.crf_spin.setToolTip(
            "Lower = Higher quality and larger file size\n"
            "18 ≈ No perceptible loss\n"
            "23 ≈ Default setting\n"
            "28 ≈ Small file size, visible loss"
        )

        self.preset_combo = QComboBox()
        self.preset_combo.addItems([
            "ultrafast", "superfast", "veryfast", "faster",
            "fast", "medium", "slow", "slower", "veryslow"
        ])
        self.preset_combo.setCurrentText("medium")
        self.preset_combo.setToolTip(
            "ultrafast = fastest, larger size\n"
            "medium = balanced (default)\n"
            "slow/veryslow = better quality, much slower"
        )

        self.bitrate_spin = QDoubleSpinBox()
        self.bitrate_spin.setRange(100, 100000)
        self.bitrate_spin.setSingleStep(100)
        self.bitrate_spin.setSuffix(" kbps")
        self.bitrate_spin.setValue(2000)
        #self.bitrate_spin.setEnabled(False)
        
        self._on_rate_mode_changed(0)

        self.force_encoder_combo = QComboBox()
        self.force_encoder_combo.addItems([
            "Auto (The best being)",
            "libx265", "libx264",
            "hevc_nvenc", "h264_nvenc",
            "hevc_qsv", "h264_qsv",
            "hevc_amf", "h264_amf",
        ])
        self.force_encoder_combo.setToolTip(
            "Auto: Automatically tests the GPU\n"
            "libx265: Better quality, slower CPU performance\n"
            "hevc_nvenc: Fast, requires NVIDIA"
        )

        self.match_source_btn = QPushButton("Match Source Bitrate")
        self.match_source_btn.setToolTip(
            "It reads the bitrate from the source video itself and populates the field."
        )
        self.match_source_btn.clicked.connect(self._match_source_bitrate)

        encode_box = QGroupBox("Encoding Settings")
        encode_form = QFormLayout(encode_box)
        encode_form.addRow("Mode:", self.rate_mode_combo)
        encode_form.addRow("CRF:", self.crf_spin)
        encode_form.addRow("Preset:", self.preset_combo)
        encode_form.addRow("Bitrate:", self.bitrate_spin)
        encode_form.addRow(self.match_source_btn)
        encode_form.addRow("Encoder:", self.force_encoder_combo)

        export_btn = QPushButton("Export Video...")
        export_btn.setStyleSheet("font-weight: bold; padding: 8px;")
        export_btn.clicked.connect(self._export)

        copy_cmd_btn = QPushButton("Copy FFmpeg Command")
        copy_cmd_btn.setToolTip("Copy the exact ffmpeg command this export would run, to the clipboard.")
        copy_cmd_btn.clicked.connect(self._copy_ffmpeg_command)

        self.encoder_label = QLabel("Encoder: (not yet determined)")
        self.encoder_label.setStyleSheet("color: #888; font-size: 11px;")
        self.encoder_label.setToolTip(
            "Click Export or Copy FFmpeg Command once to populate this. "
            "Hover afterwards to see why any GPU encoder was or wasn't used."
        )
        self.encoder_label.setWordWrap(True)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(assets_box, stretch=2)
        right_layout.addWidget(props_box)
        right_layout.addWidget(encode_box)
        right_layout.addStretch(1)
        right_layout.addWidget(self.encoder_label)
        right_layout.addWidget(copy_cmd_btn)
        right_layout.addWidget(export_btn)
        right_panel.setFixedWidth(360)

        splitter = QSplitter(self)
        splitter.addWidget(center)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        self.setCentralWidget(splitter)

        self._selected_watermark: Optional[Watermark] = None
        # Clicking a watermark on the canvas needs to update which one the
        # properties panel and Remove button act on - without this, only
        # the most-recently-created watermark could ever be edited/removed.
        self.scene.selectionChanged.connect(self._on_scene_selection_changed)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._refresh_overlay_visibility)
        self._poll_timer.start(150)

        self.statusBar().showMessage(
            "Drag a video file onto the preview area to begin, or click 'Open Video...'."
        )

    # ------------------------------------------------------------------
    # Video loading / playback
    # ------------------------------------------------------------------
    def _browse_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "", "Video Files (*.mp4 *.mov *.mkv *.avi *.webm)"
        )
        if path:
            self.load_video(path)

    def load_video(self, path: str):
        if classify_asset(path) != "video":
            QMessageBox.warning(self, "Not a video", "That doesn't look like a video file.")
            return
        self.video_path = path
        stats = self._probe_source_stats(path)
        self._source_codec = stats.get("codec", "unknown")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.setWindowTitle(f"Video Watermark Editor - {os.path.basename(path)}")
        self.statusBar().showMessage(f"Loaded {path}")

    def _on_native_size_changed(self, size):
        if size.width() <= 0 or size.height() <= 0:
            return
        self.video_width, self.video_height = size.width(), size.height()
        self.video_item.setSize(size)  # already a QSizeF
        self.scene.setSceneRect(0, 0, size.width(), size.height())
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        # Re-place any existing overlay items now that we know the frame size
        for wm in self.watermarks:
            if wm.item is not None:
                self._position_item_from_watermark(wm)

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("Play")
        else:
            self.player.play()
            self.play_btn.setText("Pause")

    def _seek_to(self, seconds: float):
        self.player.setPosition(int(seconds * 1000))

    def _on_duration_changed(self, ms: int):
        self.timeline.duration = ms / 1000.0
        self.timeline.refresh_size()
        self._fit_timeline_zoom()

    def _on_zoom_changed(self, value):
        self.timeline.set_zoom(float(value))

    def _fit_timeline_zoom(self):
        duration = self.timeline.duration
        if duration <= 0:
            return
        viewport_w = max(200, self.timeline_scroll.viewport().width() - LEFT_MARGIN - 40)
        pps = max(5, min(400, viewport_w / duration))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(pps))
        self.zoom_slider.blockSignals(False)
        self.timeline.set_zoom(pps)

    def _on_position_changed(self, ms: int):
        pos = ms / 1000.0
        self.timeline.position = pos
        self.timeline.update()
        total = self.player.duration() / 1000.0
        self.time_label.setText(f"{self.timeline._fmt_time(pos)} / {self.timeline._fmt_time(total)}")

    # ------------------------------------------------------------------
    # Watermark assets
    # ------------------------------------------------------------------
    def _browse_asset(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Add Watermark Asset", "",
            "Watermark Files (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.mp4 *.mov *.webm)"
        )
        if path:
            self._add_asset_to_list(path)

    def _add_asset_to_list(self, path: str):
        kind = classify_asset(path)
        thumb = extract_thumbnail(path, kind)
        item = QListWidgetItem(f"{os.path.basename(path)}  [{kind}]")
        item.setIcon(thumb)
        item.setData(Qt.ItemDataRole.UserRole, {"path": path, "kind": kind})
        self.asset_list.addItem(item)

    def _on_asset_dropped(self, index: int, scene_pos: QPointF):
        if self.video_width == 0:
            QMessageBox.information(self, "Load a video first",
                                     "Load your source video before placing watermarks.")
            return
        item = self.asset_list.item(index)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        wm = Watermark(
            id=self._next_id,
            asset_path=data["path"],
            kind=data["kind"],
            label=os.path.basename(data["path"]),
        )
        self._next_id += 1
        wm.x = max(0.0, min(0.9, scene_pos.x() / self.video_width))
        wm.y = max(0.0, min(0.9, scene_pos.y() / self.video_height))
        cur = self.player.position() / 1000.0
        wm.start = cur
        wm.end = min(self.timeline.duration or cur + 5, cur + 5)
        self.watermarks.append(wm)
        self._create_overlay_item(wm)
        self.timeline.watermarks = self.watermarks
        self.timeline.refresh_size()
        self.scene.clearSelection()
        wm.item.setSelected(True)  # triggers _on_scene_selection_changed

    def _create_overlay_item(self, wm: Watermark):
        thumb = extract_thumbnail(wm.asset_path, wm.kind)
        target_w = max(10, int(self.video_width * wm.scale))
        scaled = thumb.scaledToWidth(target_w, Qt.TransformationMode.SmoothTransformation)
        gitem = WatermarkGraphicsItem(wm, scaled)
        gitem.setOpacity(0.85)
        self.scene.addItem(gitem)
        wm.item = gitem
        self._position_item_from_watermark(wm)

    def _position_item_from_watermark(self, wm: Watermark):
        if wm.item is None or self.video_width == 0:
            return
        thumb = extract_thumbnail(wm.asset_path, wm.kind)
        target_w = max(10, int(self.video_width * wm.scale))
        scaled = thumb.scaledToWidth(target_w, Qt.TransformationMode.SmoothTransformation)
        wm.item.setPixmap(scaled)
        wm.item.setPos(wm.x * self.video_width, wm.y * self.video_height)

    def _refresh_overlay_visibility(self):
        pos = self.player.position() / 1000.0
        for wm in self.watermarks:
            if wm.item is not None:
                wm.item.setVisible(wm.start <= pos <= wm.end)

    # ------------------------------------------------------------------
    # Selection / property panel
    # ------------------------------------------------------------------
    def _on_asset_selected(self, row: int):
        pass  # selection in the list is just for dragging; no action needed

    def _on_scene_selection_changed(self):
        selected = [it for it in self.scene.selectedItems() if isinstance(it, WatermarkGraphicsItem)]
        if selected:
            # Bring the clicked item to the front so it's easier to grab
            # again if multiple watermarks are stacked near each other.
            self._z_counter = getattr(self, "_z_counter", 1) + 1
            selected[0].setZValue(self._z_counter)
            self._select_watermark(selected[0].watermark)
        else:
            self._selected_watermark = None

    def _select_watermark(self, wm: Watermark):
        self._selected_watermark = wm
        self.scale_spin.blockSignals(True)
        self.start_spin.blockSignals(True)
        self.end_spin.blockSignals(True)
        self.scale_spin.setValue(wm.scale)
        self.start_spin.setValue(wm.start)
        self.end_spin.setValue(wm.end)
        self.scale_spin.blockSignals(False)
        self.start_spin.blockSignals(False)
        self.end_spin.blockSignals(False)

    def _on_scale_changed(self, value):
        if self._selected_watermark:
            self._selected_watermark.scale = value
            self._position_item_from_watermark(self._selected_watermark)

    def _on_start_changed(self, value):
        if self._selected_watermark:
            self._selected_watermark.start = value
            self.timeline.update()

    def _on_end_changed(self, value):
        if self._selected_watermark:
            self._selected_watermark.end = value
            self.timeline.update()

    def _remove_selected_watermark(self):
        wm = self._selected_watermark
        if not wm:
            return
        if wm.item is not None:
            self.scene.removeItem(wm.item)
        self.watermarks.remove(wm)
        self.timeline.watermarks = self.watermarks
        self.timeline.refresh_size()
        self._selected_watermark = None
        
    def _on_rate_mode_changed(self, index):
        is_crf = (index == 0)
        self.crf_spin.setEnabled(is_crf)
        self.bitrate_spin.setEnabled(not is_crf)

    def _match_source_bitrate(self):
        if not self.video_path:
            QMessageBox.warning(self, "No video", "Load a video first.")
            return
        stats = self._probe_source_stats(self.video_path)
        kbps = stats.get("video_kbps")
        if not kbps:
            QMessageBox.information(self, "Unsuccessful",
                                     "The source bitrate could not be read. Enter it manually.")
            return
        self.bitrate_spin.setValue(kbps)
        self.rate_mode_combo.setCurrentIndex(1)  # switch to bitrate mode
        self.statusBar().showMessage(f"Source bitrate: {int(kbps)} kbps", 5000)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def _probe_source_stats(self, path):
        stats = {
            "width": self.video_width, "height": self.video_height,
            "fps": None, "video_kbps": None, "codec": "unknown",
        }
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return stats
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries",
                 "stream=width,height,r_frame_rate,bit_rate,codec_name"
                 ":stream_tags=BPS"
                 ":format=bit_rate",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=15
            )
            data = json.loads(out.stdout)
            stream = data["streams"][0]
            stats["width"] = int(stream["width"])
            stats["height"] = int(stream["height"])
            stats["codec"] = stream.get("codec_name", "unknown")
            if stream.get("r_frame_rate"):
                stats["fps"] = stream["r_frame_rate"]

            # Priority 1: stream-level bit_rate (mp4/mov often have this)
            video_bps = stream.get("bit_rate")

            # Priority 2: BPS tag written by mkvmerge into stream tags
            if not video_bps:
                tags = stream.get("tags") or {}
                bps_tag = tags.get("BPS")
                if bps_tag:
                    try:
                        video_bps = int(bps_tag)
                    except (ValueError, TypeError):
                        video_bps = None

            if video_bps:
                stats["video_kbps"] = int(video_bps) / 1000.0
            else:
                # Priority 3: container bitrate minus estimated audio
                fmt_bps = (data.get("format") or {}).get("bit_rate")
                if fmt_bps:
                    audio_kbps = self._probe_audio_kbps(path) or 160.0
                    est = int(fmt_bps) / 1000.0 - audio_kbps
                    if est > 50:
                        stats["video_kbps"] = est
        except Exception:
            pass
        return stats

    def _probe_audio_kbps(self, path):
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=bit_rate", "-of", "json", path],
                capture_output=True, text=True, timeout=15
            )
            data = json.loads(out.stdout)
            bps = data["streams"][0].get("bit_rate")
            return int(bps) / 1000.0 if bps else None
        except Exception:
            return None

    def _probe_stream_counts(self, path):
        """Number of audio and subtitle streams in the source, so per-stream
        metadata (language tags etc.) can be mapped for every track, not
        just the first one."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return 0, 0
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "stream=index,codec_type",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=15
            )
            streams = json.loads(out.stdout).get("streams", [])
            n_audio = sum(1 for s in streams if s.get("codec_type") == "audio")
            n_sub = sum(1 for s in streams if s.get("codec_type") == "subtitle")
            return n_audio, n_sub
        except Exception:
            return 0, 0

    def _encoder_works(self, ffmpeg, encode_args):
        """Actually try the encoder on a throwaway test clip, don't just trust
        that it's listed - `ffmpeg -encoders` lists everything compiled in,
        regardless of whether the driver/hardware is actually present.
        Returns (success, stderr_tail) so failures can be diagnosed."""
        try:
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "warning",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=176x144:rate=25",
            ] + encode_args + ["-f", "null", "-"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return result.returncode == 0, result.stderr.strip()
        except Exception as e:
            return False, str(e)

    def _detect_video_encoder_args(self):
        """
        Builds encoder args from the GUI settings.
        Tries the user's forced encoder first; if 'Auto', probes GPU encoders
        in order (nvenc > qsv > amf) and falls back to libx265/libx264.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found on PATH.")

        thread_cap = max(1, (os.cpu_count() or 4) - 1)

        # --- read GUI settings (with safe defaults if widgets missing) ---
        rate_mode_is_crf = True
        crf_val = 23
        preset_val = "medium"
        bitrate_val = 2000
        forced = "Auto (The best being)"
        if hasattr(self, "rate_mode_combo"):
            rate_mode_is_crf = (self.rate_mode_combo.currentIndex() == 0)
            crf_val = int(self.crf_spin.value())
            preset_val = self.preset_combo.currentText()
            bitrate_val = int(self.bitrate_spin.value())
            forced = self.force_encoder_combo.currentText()

        # --- helper: build args for a given encoder name ---
        def build_args(name):
            if rate_mode_is_crf:
                rate = ["-crf", str(crf_val)]
            else:
                rate = ["-b:v", f"{bitrate_val}k",
                        "-maxrate", f"{int(bitrate_val*1.5)}k",
                        "-bufsize", f"{int(bitrate_val*2)}k"]

            if name == "libx265":
                return ["-c:v", "libx265", "-preset", preset_val] + rate
            if name == "libx264":
                return ["-c:v", "libx264", "-preset", preset_val] + rate + \
                       ["-threads", str(thread_cap)]
            if name == "hevc_nvenc":
                if rate_mode_is_crf:
                    return ["-c:v", "hevc_nvenc", "-preset", "p5",
                            "-rc", "vbr", "-cq", str(crf_val), "-b:v", "0"]
                return ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr"] + rate
            if name == "h264_nvenc":
                if rate_mode_is_crf:
                    return ["-c:v", "h264_nvenc", "-preset", "p5",
                            "-rc", "vbr", "-cq", str(crf_val), "-b:v", "0"]
                return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr"] + rate
            if name == "hevc_qsv":
                if rate_mode_is_crf:
                    return ["-c:v", "hevc_qsv", "-preset", "medium",
                            "-global_quality", str(crf_val)]
                return ["-c:v", "hevc_qsv", "-preset", "medium"] + rate
            if name == "h264_qsv":
                if rate_mode_is_crf:
                    return ["-c:v", "h264_qsv", "-preset", "medium",
                            "-global_quality", str(crf_val)]
                return ["-c:v", "h264_qsv", "-preset", "medium"] + rate
            if name == "hevc_amf":
                if rate_mode_is_crf:
                    return ["-c:v", "hevc_amf", "-quality", "balanced",
                            "-rc", "cqp", "-qp_i", str(crf_val), "-qp_p", str(crf_val+2)]
                return ["-c:v", "hevc_amf", "-quality", "balanced", "-rc", "vbr_peak"] + rate
            if name == "h264_amf":
                if rate_mode_is_crf:
                    return ["-c:v", "h264_amf", "-quality", "balanced",
                            "-rc", "cqp", "-qp_i", str(crf_val), "-qp_p", str(crf_val+2)]
                return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "vbr_peak"] + rate
            return None

        # --- capability test args (generic, low bitrate) ---
        def test_args_for(name):
            if name == "hevc_nvenc":  return ["-c:v", "hevc_nvenc", "-preset", "p5", "-b:v", "500k"]
            if name == "h264_nvenc":  return ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "500k"]
            if name == "hevc_qsv":    return ["-c:v", "hevc_qsv", "-preset", "medium", "-b:v", "500k"]
            if name == "h264_qsv":    return ["-c:v", "h264_qsv", "-preset", "medium", "-b:v", "500k"]
            if name == "hevc_amf":    return ["-c:v", "hevc_amf", "-quality", "balanced", "-b:v", "500k"]
            if name == "h264_amf":    return ["-c:v", "h264_amf", "-quality", "balanced", "-b:v", "500k"]
            return None

        # --- decide which encoder to use ---
        chosen_name = None
        diag_lines = []

        if forced.startswith("Auto"):
            try:
                listed = subprocess.run(
                    [ffmpeg, "-hide_banner", "-encoders"],
                    capture_output=True, text=True, timeout=15
                ).stdout
                # prefer HEVC GPU encoders
                for name in ("hevc_nvenc", "hevc_qsv", "hevc_amf",
                             "h264_nvenc", "h264_qsv", "h264_amf"):
                    if name not in listed:
                        diag_lines.append(f"{name}: not compiled in.")
                        continue
                    ta = test_args_for(name)
                    ok, tail = self._encoder_works(ffmpeg, ta)
                    if ok:
                        chosen_name = name
                        diag_lines.append(f"{name}: OK - selected.")
                        break
                    else:
                        diag_lines.append(f"{name}: failed - {tail[-150:]}")
            except Exception as e:
                diag_lines.append(f"Probe failed: {e}")

            if chosen_name is None:
                # No GPU encoder worked - pick software encoder based on
                # what the source uses, so we don't re-encode h264 source
                # with x265 (which would change quality/size drastically),
                # and don't re-encode x265 source with x264 (which would
                # bloat the file massively).
                src_codec = getattr(self, "_source_codec", "unknown")
                if src_codec == "hevc":
                    chosen_name = "libx265"
                    diag_lines.append(
                        f"No GPU encoder worked → source is HEVC → libx265."
                    )
                elif src_codec == "h264":
                    chosen_name = "libx264"
                    diag_lines.append(
                        f"No GPU encoder worked → source is H.264 → libx264."
                    )
                else:
                    # Unknown/AV1/VP9/etc. - x265 is the safest modern choice
                    chosen_name = "libx265"
                    diag_lines.append(
                        f"No GPU encoder worked → source is '{src_codec}' → libx265."
                    )
        else:
            chosen_name = forced
            diag_lines.append(f"User forced: {chosen_name}")

        args = build_args(chosen_name)
        if args is None:
            # unknown forced value → safe default
            chosen_name = "libx265"
            args = build_args(chosen_name) or ["-c:v", "libx265", "-crf", "23"]

        self._encoder_diagnostics = "\n\n".join(diag_lines)
        self.statusBar().showMessage(f"Using video encoder: {chosen_name}", 5000)
        if hasattr(self, "encoder_label"):
            self.encoder_label.setText(
                f"Encoder: {chosen_name}  |  "
                f"{'CRF '+str(crf_val) if rate_mode_is_crf else 'BR '+str(bitrate_val)+'k'}  |  "
                f"{preset_val}"
            )
            self.encoder_label.setToolTip(self._encoder_diagnostics or "No diagnostic info yet.")
        return args

    @staticmethod
    def _parse_fps(fps_str):
        try:
            if fps_str and "/" in fps_str:
                num, den = fps_str.split("/")
                den = float(den)
                return float(num) / den if den else 25.0
            return float(fps_str) if fps_str else 25.0
        except Exception:
            return 25.0

    def _build_ffmpeg_command(self, output_path):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg was not found on PATH.")

        stats = self._probe_source_stats(self.video_path)
        self._source_codec = stats.get("codec", "unknown")
        width, height = stats["width"], stats["height"]
        if not width or not height:
            raise RuntimeError("Could not determine source video resolution.")

        args = [ffmpeg, "-y", "-i", self.video_path]

        filter_parts = []
        current_label = "0:v"
        input_index = 1

        for i, wm in enumerate(self.watermarks):
            #total = max(wm.end, wm.start + 0.1)
            #args += ["-loop", "1", "-t", f"{total:.3f}", "-i", wm.asset_path]  # Loop Bug Instance
            args += ["-i", wm.asset_path]

            wm_w = max(2, round(width * wm.scale))
            scaled_label = f"wms{i}"
            filter_parts.append(f"[{input_index}:v]scale={wm_w}:-1[{scaled_label}]")

            x_px = round(wm.x * width)
            y_px = round(wm.y * height)
            out_label = f"v{i}"
            filter_parts.append(
                f"[{current_label}][{scaled_label}]overlay=x={x_px}:y={y_px}:"
                f"enable='between(t,{wm.start:.3f},{wm.end:.3f})'[{out_label}]"
            )
            current_label = out_label
            input_index += 1

        if filter_parts:
            args += ["-filter_complex", ";".join(filter_parts), "-map", f"[{current_label}]"]
        else:
            args += ["-map", "0:v"]

        args += ["-map", "0:a?", "-c:a", "copy"]
        args += ["-map", "0:s?", "-c:s", "copy"]

        args += ["-map_metadata", "0", "-map_chapters", "0"]
        args += ["-map_metadata:s", "0:s"]

        args += self._detect_video_encoder_args()

        args += [output_path]
        return args

    @staticmethod
    def _quote_for_windows_cmd(arg: str) -> str:
        if arg == "":
            return '""'
        # Quote anything outside a conservative safe set. The previous
        # version only checked for a handful of characters and missed
        # [ ] ; ( ) ' - exactly what a -filter_complex value is full of -
        # so it went through unquoted and broke PowerShell's parser (which
        # reads a bare "[...]" as an attempted type literal) as well as
        # being liable to break cmd.exe. Safer to over-quote than under-quote.
        is_safe = all(c.isalnum() or c in "_.:\\/-" for c in arg)
        if is_safe:
            return arg
        return '"' + arg.replace('"', '\\"') + '"'

    def _copy_ffmpeg_command(self):
        if not self.video_path:
            QMessageBox.warning(self, "No video", "Load a source video first.")
            return
        # Use a placeholder output path since no destination has been chosen
        # yet at this point - the command is otherwise identical to what
        # Export would actually run.
        placeholder_out = os.path.splitext(os.path.basename(self.video_path))[0] + "_watermarked.mkv"
        try:
            cmd = self._build_ffmpeg_command(placeholder_out)
        except Exception as e:
            QMessageBox.critical(self, "Couldn't build command", str(e))
            return
        text = " ".join(self._quote_for_windows_cmd(a) for a in cmd)
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("FFmpeg command copied to clipboard.", 5000)

    def _export(self):
        if not self.video_path:
            QMessageBox.warning(self, "No video", "Load a source video first.")
            return
        if not shutil.which("ffmpeg"):
            QMessageBox.critical(self, "ffmpeg not found",
                                  "ffmpeg was not found on PATH. Install it and try again.")
            return
        src_base = os.path.splitext(os.path.basename(self.video_path))[0]
        src_ext = os.path.splitext(self.video_path)[1].lower()
        default_ext = src_ext if src_ext in (".mp4", ".mkv", ".mov") else ".mp4"
        default_name = f"{src_base}_watermarked{default_ext}"

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export Video As", default_name,
            "MKV Video (*.mkv);;MP4 Video (*.mp4);;MOV Video (*.mov)"
        )
        if not out_path:
            return
        if not os.path.splitext(out_path)[1]:
            out_path += default_ext
        try:
            cmd = self._build_ffmpeg_command(out_path)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return

        self.player.pause()
        self.play_btn.setText("Play")

        total_duration = self.timeline.duration or 1.0
        progress = QProgressDialog("Starting FFmpeg...", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.show()

        time_re = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
        speed_re = re.compile(r"speed=\s*([\d.]+)x")

        worker = ExportWorker(cmd, self)

        def on_progress(line):
            t_match = time_re.findall(line)
            s_match = speed_re.findall(line)
            if t_match:
                h, m, s = t_match[-1]
                done_sec = int(h) * 3600 + int(m) * 60 + float(s)
                pct = min(99, int(100 * done_sec / total_duration))
                speed = s_match[-1] if s_match else "?"
                progress.setLabelText(
                    f"Exporting... {pct}%  "
                    f"({self.timeline._fmt_time(done_sec)} / {self.timeline._fmt_time(total_duration)}, "
                    f"speed {speed}x)"
                )

        def on_finished(exit_code, log_text):
            progress.close()
            if exit_code == 0:
                QMessageBox.information(self, "Done", f"Exported to:\n{out_path}")
            else:
                tail = "".join(log_text.splitlines(keepends=True)[-30:])
                QMessageBox.critical(self, "FFmpeg error",
                                      f"Export failed (exit code {exit_code}).\n\n{tail}")

        worker.progress_line.connect(on_progress)
        worker.finished_export.connect(on_finished)
        worker.start()
        self._export_worker = worker  # keep a reference alive


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()