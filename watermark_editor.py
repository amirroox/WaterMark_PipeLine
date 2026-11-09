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
    QScrollArea, QComboBox, QCheckBox, QSpinBox,
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
    center_x: bool = False   # True = export centers horizontally in the frame
    center_y: bool = False   # True = export centers vertically in the frame
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


@dataclass
class AudioAsset:
    id: int
    asset_path: str
    start: float = 0.0
    end: float = 5.0
    label: str = ""
    apply_to_streams: List[int] = field(default_factory=list)
    mode: str = "replace"        # "replace" | "overlay" | "duck"
    duck_db: float = -12.0       # only used when mode == "duck"
    new_volume: float = 1.0      # multiplier for the new audio
    item: Optional["AudioGraphicsItem"] = field(default=None, repr=False)


def classify_audio(path: str) -> bool:
    """True if path looks like a pure audio file we can use."""
    ext = os.path.splitext(path)[1].lower()
    return ext in (".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg", ".opus", ".wma")


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
        self.on_moved = None  # callback(watermark), set by MainWindow

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
            if self.on_moved is not None:
                self.on_moved(self.watermark)
        return super().itemChange(change, value)


class AudioGraphicsItem:
    """Placeholder marker used only so the timeline can highlight audio
    segments the same way it highlights image watermarks. It has no
    scene presence - the timeline paints it directly."""
    def __init__(self, audio: "AudioAsset"):
        self.audio = audio


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
    # Emitted when the user clicks a segment on the timeline.
    # Arg is the index in self.watermarks / self.audio_assets (or -1).
    watermark_clicked = Signal(int)
    audio_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.watermarks: List[Watermark] = []
        self.audio_assets: List[AudioAsset] = []
        self.duration = 0.0
        self.position = 0.0
        self.pixels_per_second = 40.0
        self.setMinimumHeight(RULER_HEIGHT + 4 * ROW_HEIGHT)
        self.setMouseTracking(True)
        self._drag_mode = None    # "move" | "resize_start" | "resize_end" | "seek"
        self._drag_wm = None
        self._drag_audio = None
        self._drag_anchor_x = 0
        self._drag_orig_start = 0.0
        self._drag_orig_end = 0.0
        self.selected_watermark_index = -1
        self.selected_audio_index = -1

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
        n_rows = max(4, len(self.watermarks) + len(self.audio_assets))
        height = RULER_HEIGHT + n_rows * ROW_HEIGHT + 10
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

        # Watermark rows (blue)
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
            if i == self.selected_watermark_index:
                color = QColor("#ff9f40")  # orange = selected
            elif wm is self._drag_wm:
                color = QColor("#4a90d9")  # blue = being dragged
            else:
                color = QColor("#3a6ea5")  # dark blue = normal
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor("#1c3a5c")))
            painter.drawRoundedRect(seg_rect, 4, 4)
            painter.setBrush(QBrush(QColor("#dfeeff")))
            painter.drawRect(QRectF(x1, seg_rect.top(), HANDLE_WIDTH, seg_rect.height()))
            painter.drawRect(QRectF(x2 - HANDLE_WIDTH, seg_rect.top(), HANDLE_WIDTH, seg_rect.height()))

        # Audio rows (green) - continue row numbering after image watermarks
        audio_offset = len(self.watermarks)
        for j, au in enumerate(self.audio_assets):
            i = audio_offset + j
            row = self.row_rect(i)
            painter.setPen(QPen(QColor("#444")))
            painter.drawLine(0, int(row.top()), self.width(), int(row.top()))
            painter.setPen(QColor("#cfc"))
            painter.drawText(8, int(row.top()) + ROW_HEIGHT - 10,
                             "♪ " + (au.label or os.path.basename(au.asset_path))[:14])

            x1 = self.time_to_x(au.start)
            x2 = self.time_to_x(au.end)
            seg_rect = QRectF(x1, row.top() + 4, max(2, x2 - x1), ROW_HEIGHT - 8)
            if j == self.selected_audio_index:
                color = QColor("#a0e060")  # bright green = selected
            elif au is getattr(self, "_drag_audio", None):
                color = QColor("#3aa55c")  # green = being dragged
            else:
                color = QColor("#2d7a44")  # dark green = normal
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor("#1a4a2a")))
            painter.drawRoundedRect(seg_rect, 4, 4)
            # Tag the segment with how it is applied to the source audio.
            mode_tag = {
                "replace": "REPLACE",
                "overlay": "OVERLAY",
                "duck": f"DUCK {au.duck_db:.0f} dB",
            }.get(au.mode, "")
            painter.save()
            painter.setClipRect(seg_rect)
            painter.setPen(QColor("#e8ffe8"))
            painter.drawText(seg_rect.adjusted(6, 0, -4, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             mode_tag)
            painter.restore()
            # No resize handles drawn - audio segments are move-only.

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
        n_wm = len(self.watermarks)
        n_au = len(self.audio_assets)

        # Watermark rows
        if 0 <= row_index < n_wm:
            wm = self.watermarks[row_index]
            x1, x2 = self.time_to_x(wm.start), self.time_to_x(wm.end)
            self._drag_wm = wm
            self._drag_audio = None
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
            if self._drag_wm is not None:
                self.watermark_clicked.emit(row_index)
                self.audio_clicked.emit(-1)  # deselect any audio
        # Audio rows
        elif n_wm <= row_index < n_wm + n_au:
            au = self.audio_assets[row_index - n_wm]
            x1, x2 = self.time_to_x(au.start), self.time_to_x(au.end)
            self._drag_audio = au
            self._drag_wm = None
            self._drag_orig_start = au.start
            self._drag_orig_end = au.end
            self._drag_anchor_x = x
            # Audio segments can only be MOVED, never resized: the audio's
            # duration must stay exactly as long as the file itself, so
            # shrinking/stretching the segment would either cut the audio
            # short or leave it padded with silence. Users who want a
            # different duration should provide a different audio file.
            if x1 <= x <= x2:
                self._drag_mode = "move"
            else:
                self._drag_mode = None
                self._drag_audio = None
            if self._drag_audio is not None:
                self.audio_clicked.emit(row_index - n_wm)
                self.watermark_clicked.emit(-1)  # deselect any watermark
        else:
            self._drag_mode = None
            self._drag_wm = None
            self._drag_audio = None
            self.watermark_clicked.emit(-1)
            self.audio_clicked.emit(-1)
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag_mode == "seek":
            self.seek_requested.emit(self.x_to_time(event.position().x()))
            return
        target = self._drag_wm if self._drag_wm is not None else self._drag_audio
        if target is None or self._drag_mode is None:
            return

        # Safety: audio segments must never be resized, only moved.
        # If somehow a resize mode leaked through, force it to move.
        if self._drag_audio is not None and self._drag_mode in ("resize_start", "resize_end"):
            self._drag_mode = "move"

        dx = event.position().x() - self._drag_anchor_x
        dt = dx / max(self.pixels_per_second, 0.0001)
        if self._drag_mode == "move":
            duration = self._drag_orig_end - self._drag_orig_start
            new_start = max(0.0, min(self.duration - duration, self._drag_orig_start + dt))
            target.start = new_start
            target.end = new_start + duration
        elif self._drag_mode == "resize_start":
            target.start = max(0.0, min(self._drag_orig_end - 0.1, self._drag_orig_start + dt))
        elif self._drag_mode == "resize_end":
            target.end = min(self.duration, max(self._drag_orig_start + 0.1, self._drag_orig_end + dt))
        self.update()
        self.watermarks_changed.emit()

    def mouseReleaseEvent(self, event):
        self._drag_mode = None
        self._drag_wm = None
        self._drag_audio = None
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

        self.audio_assets: List[AudioAsset] = []
        self._next_audio_id = 1
        self._selected_audio: Optional[AudioAsset] = None
        self._audio_stream_checkboxes: List[QCheckBox] = []
        self._updating_pos = False   # True while code (not the user) moves an overlay

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

        # Separate player for the short audio-mix preview, so it never
        # disturbs the main video playback position.
        self.preview_player = QMediaPlayer(self)
        self.preview_output = QAudioOutput(self)
        self.preview_player.setAudioOutput(self.preview_output)
        self.preview_player.playbackStateChanged.connect(self._on_preview_state_changed)
        self._preview_path = os.path.join(
            tempfile.gettempdir(), f"_wm_audio_preview_{os.getpid()}.wav")
        self._preview_worker: Optional[ExportWorker] = None

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

        self.timeline.watermark_clicked.connect(self._on_timeline_watermark_clicked)
        self.timeline.audio_clicked.connect(self._on_timeline_audio_clicked)

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

        # ---------- Audio assets ----------
        self.audio_list = QListWidget()
        self.audio_list.currentRowChanged.connect(self._on_audio_selected)
        add_audio_btn = QPushButton("Add Audio Asset...")
        add_audio_btn.clicked.connect(self._browse_audio)

        audio_assets_box = QGroupBox("Audio Assets  (double-click row to place)")
        audio_assets_layout = QVBoxLayout(audio_assets_box)
        audio_assets_layout.addWidget(self.audio_list)
        audio_assets_layout.addWidget(add_audio_btn)

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

        self.pos_x_spin = QSpinBox()
        self.pos_x_spin.setRange(0, 100000)
        self.pos_x_spin.setSuffix(" px")
        self.pos_x_spin.setToolTip("Horizontal position of the watermark's centre, in video pixels.")
        self.pos_x_spin.valueChanged.connect(lambda _v: self._on_pos_spin_changed("x"))

        self.pos_y_spin = QSpinBox()
        self.pos_y_spin.setRange(0, 100000)
        self.pos_y_spin.setSuffix(" px")
        self.pos_y_spin.setToolTip("Vertical position of the watermark's centre, in video pixels.")
        self.pos_y_spin.valueChanged.connect(lambda _v: self._on_pos_spin_changed("y"))

        center_x_btn = QPushButton("Center X")
        center_x_btn.setToolTip("Center horizontally in the frame (exact at export).")
        center_x_btn.clicked.connect(lambda: self._center_watermark(True, False))
        center_y_btn = QPushButton("Center Y")
        center_y_btn.setToolTip("Center vertically in the frame (exact at export).")
        center_y_btn.clicked.connect(lambda: self._center_watermark(False, True))
        center_both_btn = QPushButton("Center Both")
        center_both_btn.setToolTip("Center on both axes: exactly the middle of the frame.")
        center_both_btn.clicked.connect(lambda: self._center_watermark(True, True))
        center_row = QHBoxLayout()
        center_row.addWidget(center_x_btn)
        center_row.addWidget(center_y_btn)
        center_row.addWidget(center_both_btn)

        self.remove_btn = QPushButton("Remove Selected Watermark")
        self.remove_btn.clicked.connect(self._remove_selected_watermark)

        self.audio_list.itemDoubleClicked.connect(self._on_audio_item_double_clicked)

        props_box = QGroupBox("Selected Watermark")
        props_form = QFormLayout(props_box)
        props_form.addRow("Width (fraction of video):", self.scale_spin)
        props_form.addRow("Center X:", self.pos_x_spin)
        props_form.addRow("Center Y:", self.pos_y_spin)
        props_form.addRow(center_row)
        props_form.addRow("Start:", self.start_spin)
        props_form.addRow("End:", self.end_spin)
        props_form.addRow(self.remove_btn)

        self.remove_audio_btn = QPushButton("Remove Selected Audio")
        self.remove_audio_btn.clicked.connect(self._remove_selected_audio)

        self.audio_preview_btn = QPushButton("Preview Audio Mix")
        self.audio_preview_btn.setToolTip(
            "Renders a short clip (the selected audio's window plus a few seconds\n"
            "of context either side) using the same filters as the export, and\n"
            "plays it. Only the selected audio is included; the video is not."
        )
        self.audio_preview_btn.clicked.connect(self._preview_audio_mix)

        # Audio codec controls
        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItems([
            "Auto (match source)",
            "aac", "libopus", "libmp3lame", "libvorbis", "flac",
        ])
        self.audio_codec_combo.setCurrentText("Auto (match source)")

        self.audio_bitrate_spin = QDoubleSpinBox()
        self.audio_bitrate_spin.setRange(32, 512)
        self.audio_bitrate_spin.setSingleStep(16)
        self.audio_bitrate_spin.setSuffix(" kbps")
        self.audio_bitrate_spin.setValue(192)

        # NEW: Replacement mode controls
        self.audio_mode_combo = QComboBox()
        self.audio_mode_combo.addItems([
            "Replace (mute source, play new audio)",
            "Overlay (mix new audio on top of source)",
            "Duck (lower source, play new audio on top)",
        ])
        self.audio_mode_combo.setCurrentText("Replace (mute source, play new audio)")
        self.audio_mode_combo.setToolTip(
            "Replace: source is silenced during the window; only new audio plays.\n"
            "Overlay: source keeps playing at full volume; new audio is mixed on top.\n"
            "Duck: source is lowered in volume (not muted); new audio plays on top."
        )
        self.audio_mode_combo.currentIndexChanged.connect(self._on_audio_mode_changed)

        self.audio_duck_db_spin = QDoubleSpinBox()
        self.audio_duck_db_spin.setRange(-60.0, -3.0)
        self.audio_duck_db_spin.setSingleStep(1.0)
        self.audio_duck_db_spin.setValue(-12.0)
        self.audio_duck_db_spin.setSuffix(" dB")
        self.audio_duck_db_spin.setToolTip(
            "Only used in Duck mode. How much to lower the source audio\n"
            "during the replacement window (e.g. -12 dB = quieter but still audible)."
        )
        self.audio_duck_db_spin.setEnabled(False)

        self.audio_new_volume_spin = QDoubleSpinBox()
        self.audio_new_volume_spin.setRange(0.0, 4.0)
        self.audio_new_volume_spin.setSingleStep(0.1)
        self.audio_new_volume_spin.setValue(1.0)
        self.audio_new_volume_spin.setToolTip(
            "Volume multiplier for the new audio. 1.0 = unchanged, "
            "0.5 = half volume, 2.0 = twice as loud."
        )

        self.audio_duck_db_spin.valueChanged.connect(self._on_audio_duck_db_changed)
        self.audio_new_volume_spin.valueChanged.connect(self._on_audio_new_volume_changed)

        # Container for stream checkboxes (populated on video load)
        self.audio_streams_box = QGroupBox("Apply to audio streams")
        self.audio_streams_layout = QVBoxLayout(self.audio_streams_box)
        self.audio_streams_placeholder = QLabel("Load a video first.")
        self.audio_streams_placeholder.setStyleSheet("color: #888;")
        self.audio_streams_layout.addWidget(self.audio_streams_placeholder)

        audio_props_box = QGroupBox("Selected Audio")
        audio_props_form = QFormLayout(audio_props_box)
        audio_props_form.addRow("Mode:", self.audio_mode_combo)
        audio_props_form.addRow("Duck amount:", self.audio_duck_db_spin)
        audio_props_form.addRow("New audio volume:", self.audio_new_volume_spin)
        audio_props_form.addRow("Codec:", self.audio_codec_combo)
        audio_props_form.addRow("Bitrate:", self.audio_bitrate_spin)
        audio_props_form.addRow(self.audio_streams_box)
        audio_props_form.addRow(self.audio_preview_btn)
        audio_props_form.addRow(self.remove_audio_btn)

        # ---------- Export options ----------
        self.apply_image_cb = QCheckBox("Apply image watermarks")
        self.apply_image_cb.setChecked(True)
        self.apply_audio_cb = QCheckBox("Apply audio replacement")
        self.apply_audio_cb.setChecked(True)

        export_opts_box = QGroupBox("Export Options")
        export_opts_layout = QVBoxLayout(export_opts_box)
        export_opts_layout.addWidget(self.apply_image_cb)
        export_opts_layout.addWidget(self.apply_audio_cb)

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
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(8)
        right_layout.addWidget(assets_box)
        right_layout.addWidget(props_box)
        right_layout.addWidget(audio_assets_box)
        right_layout.addWidget(audio_props_box)
        right_layout.addWidget(encode_box)
        right_layout.addWidget(export_opts_box)
        right_layout.addStretch(1)
        right_layout.addWidget(self.encoder_label)
        right_layout.addWidget(copy_cmd_btn)
        right_layout.addWidget(export_btn)

        # Wrap the right panel in a scroll area so all controls stay reachable
        # regardless of window height.
        right_scroll = QScrollArea()
        right_scroll.setWidget(right_panel)
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        right_scroll.setMinimumWidth(420)
        right_scroll.setMaximumWidth(560)

        splitter = QSplitter(self)
        splitter.addWidget(center)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
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
        self._refresh_audio_stream_checkboxes()

    def _on_native_size_changed(self, size):
        if size.width() <= 0 or size.height() <= 0:
            return
        self.video_width, self.video_height = size.width(), size.height()
        self.pos_x_spin.setMaximum(self.video_width)
        self.pos_y_spin.setMaximum(self.video_height)
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

    def _on_audio_duck_db_changed(self, value):
        if self._selected_audio:
            self._selected_audio.duck_db = value
            self.timeline.update()

    def _on_audio_new_volume_changed(self, value):
        if self._selected_audio:
            self._selected_audio.new_volume = value

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
        gitem.on_moved = self._on_item_moved
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
        # Axes flagged as centered are recomputed from the current size, so
        # they stay centered when the width is changed.
        if wm.center_x:
            wm.x = (self.video_width - scaled.width()) / 2 / self.video_width
        if wm.center_y:
            wm.y = (self.video_height - scaled.height()) / 2 / self.video_height
        self._updating_pos = True
        try:
            wm.item.setPos(wm.x * self.video_width, wm.y * self.video_height)
        finally:
            self._updating_pos = False
        self._sync_position_spins(wm)

    def _on_item_moved(self, wm: Watermark):
        """Called when the overlay moves. A manual drag cancels centering."""
        if self._updating_pos:
            return
        wm.center_x = False
        wm.center_y = False
        self._sync_position_spins(wm)

    def _sync_position_spins(self, wm: Watermark):
        """Show the watermark's centre point (video pixels) in the X/Y boxes."""
        if wm is not self._selected_watermark or wm.item is None:
            return
        pm = wm.item.pixmap()
        cx = round(wm.item.pos().x() + pm.width() / 2)
        cy = round(wm.item.pos().y() + pm.height() / 2)
        for spin, value in ((self.pos_x_spin, cx), (self.pos_y_spin, cy)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _on_pos_spin_changed(self, axis: str):
        wm = self._selected_watermark
        if wm is None or wm.item is None or self.video_width == 0:
            return
        pm = wm.item.pixmap()
        x, y = wm.item.pos().x(), wm.item.pos().y()
        if axis == "x":
            wm.center_x = False
            x = self.pos_x_spin.value() - pm.width() / 2
        else:
            wm.center_y = False
            y = self.pos_y_spin.value() - pm.height() / 2
        self._updating_pos = True
        try:
            wm.item.setPos(x, y)   # clamped to the frame by the item
        finally:
            self._updating_pos = False
        self._sync_position_spins(wm)

    def _center_watermark(self, horizontal: bool, vertical: bool):
        wm = self._selected_watermark
        if wm is None or wm.item is None or self.video_width == 0:
            QMessageBox.information(self, "No watermark selected",
                                     "Select a watermark first (click it on the video or the timeline).")
            return
        if horizontal:
            wm.center_x = True
        if vertical:
            wm.center_y = True
        self._position_item_from_watermark(wm)

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

    def _on_timeline_watermark_clicked(self, index):
        if 0 <= index < len(self.watermarks):
            wm = self.watermarks[index]
            self.scene.clearSelection()
            if wm.item is not None:
                wm.item.setSelected(True)
            self._select_watermark(wm)
            self._selected_audio = None
            self.timeline.selected_watermark_index = index
            self.timeline.selected_audio_index = -1
            self.timeline.update()
        else:
            self._selected_watermark = None
            self.timeline.selected_watermark_index = -1
            self.timeline.update()

    def _on_timeline_audio_clicked(self, index):
        if 0 <= index < len(self.audio_assets):
            au = self.audio_assets[index]
            self._select_audio(au)
            self.scene.clearSelection()
            self._selected_watermark = None
            # Tell the timeline to highlight this one
            self.timeline.selected_audio_index = index
            self.timeline.selected_watermark_index = -1
            self.timeline.update()
        else:
            self._selected_audio = None
            self.timeline.selected_audio_index = -1
            self.timeline.update()

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
        self._sync_position_spins(wm)
        try:
            idx = self.watermarks.index(wm)
            self.timeline.selected_watermark_index = idx
            self.timeline.selected_audio_index = -1
            self.timeline.update()
        except ValueError:
            pass

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
        self.timeline.selected_watermark_index = -1
        self.timeline.update()

    def _on_audio_item_double_clicked(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and "path" in data:
            self._add_audio_asset(data["path"])

    # ------------------------------------------------------------------
    # Audio assets
    # ------------------------------------------------------------------
    def _browse_audio(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Add Audio Asset", "",
            "Audio Files (*.mp3 *.wav *.aac *.flac *.m4a *.ogg *.opus *.wma)"
        )
        if not path:
            return
        if not classify_audio(path):
            QMessageBox.warning(self, "Not audio",
                                 "That file doesn't look like a pure audio file.")
            return
        item = QListWidgetItem(f"{os.path.basename(path)}")
        item.setData(Qt.ItemDataRole.UserRole, {"path": path})
        self.audio_list.addItem(item)
        # auto-create an AudioAsset instance so the user can see it on the timeline
        # self._add_audio_asset(path)

    def _add_audio_asset(self, path: str):
        if self.timeline.duration <= 0:
            QMessageBox.information(self, "Load video first",
                                    "Load a source video before adding audio.")
            return

        # Probe duration FIRST so we can use it in the label.
        dur = self._probe_audio_duration(path) or 5.0
        dur = max(0.1, dur)  # guard against broken/zero-length files

        au = AudioAsset(
            id=self._next_audio_id,
            asset_path=path,
            label=f"{os.path.basename(path)}  [{dur:.1f}s]",
        )
        self._next_audio_id += 1

        cur = self.player.position() / 1000.0
        au.start = cur
        au.end = min(self.timeline.duration, cur + dur)
        au.apply_to_streams = []
        self.audio_assets.append(au)
        self.timeline.audio_assets = self.audio_assets
        self.timeline.refresh_size()
        self._select_audio(au)

        # Warn if the audio is longer than the remaining video.
        remaining = self.timeline.duration - cur
        if dur > remaining:
            self.statusBar().showMessage(
                f"Note: audio ({dur:.1f}s) is longer than the remaining "
                f"video ({remaining:.1f}s); it will be cut short.",
                8000
            )

    def _on_audio_selected(self, row: int):
        if 0 <= row < len(self.audio_assets):
            self._select_audio(self.audio_assets[row])

    def _select_audio(self, au: AudioAsset):
        self._selected_audio = au
        # Sync mode combo
        mode_map = {"replace": 0, "overlay": 1, "duck": 2}
        self.audio_mode_combo.blockSignals(True)
        self.audio_mode_combo.setCurrentIndex(mode_map.get(au.mode, 0))
        self.audio_mode_combo.blockSignals(False)
        self.audio_duck_db_spin.blockSignals(True)
        self.audio_duck_db_spin.setValue(au.duck_db)
        self.audio_duck_db_spin.blockSignals(False)
        self.audio_duck_db_spin.setEnabled(au.mode == "duck")
        self.audio_new_volume_spin.blockSignals(True)
        self.audio_new_volume_spin.setValue(au.new_volume)
        self.audio_new_volume_spin.blockSignals(False)
        # refresh stream checkbox states
        self._refresh_audio_stream_checkboxes()
        try:
            idx = self.audio_assets.index(au)
            self.timeline.selected_audio_index = idx
            self.timeline.selected_watermark_index = -1
            self.timeline.update()
        except ValueError:
            pass

    def _remove_selected_audio(self):
        au = self._selected_audio
        if not au:
            return
        if au in self.audio_assets:
            self.audio_assets.remove(au)
        self.timeline.audio_assets = self.audio_assets
        self.timeline.refresh_size()
        self._selected_audio = None
        self.timeline.selected_audio_index = -1
        self.timeline.update()

    def _refresh_audio_stream_checkboxes(self):
        """Rebuild the checkbox list based on probed audio streams."""
        # clear old
        while self.audio_streams_layout.count():
            item = self.audio_streams_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._audio_stream_checkboxes = []

        if not self.video_path:
            lbl = QLabel("Load a video first.")
            lbl.setStyleSheet("color: #888;")
            self.audio_streams_layout.addWidget(lbl)
            return

        streams = self._probe_audio_streams(self.video_path)
        if not streams:
            lbl = QLabel("No audio streams found.")
            lbl.setStyleSheet("color: #888;")
            self.audio_streams_layout.addWidget(lbl)
            return

        # "All streams" master checkbox
        all_cb = QCheckBox("All audio streams")
        all_cb.setChecked(True)
        self.audio_streams_layout.addWidget(all_cb)

        cb_list = []
        for pos, s in enumerate(streams):
            label = s.get("language") or s.get("title") or f"Stream #{s['index']}"
            codec = s.get("codec_name", "?")
            channels = s.get("channels", "?")
            cb = QCheckBox(f"[{s['index']}] {label} ({codec}, {channels}ch)")
            cb.setChecked(True)
            self.audio_streams_layout.addWidget(cb)
            # Store `pos` (0-based position among AUDIO streams only), not
            # s["index"] (the stream's position in the whole container,
            # which includes video/subtitle streams before it). _build_
            # ffmpeg_command compares against audio-only indices (it maps
            # streams as 0:a:0, 0:a:1, ...), so storing the container-wide
            # index here was applying replacements to the wrong track
            # whenever anything (almost always the video stream) came
            # before the first audio stream.
            cb_list.append((pos, cb))

        def on_all_toggled(state):
            for _, cb in cb_list:
                cb.setChecked(state == Qt.CheckState.Checked.value or state == 2)

        def on_child_toggled(_):
            all_checked = all(cb.isChecked() for _, cb in cb_list)
            all_cb.blockSignals(True)
            all_cb.setChecked(all_checked)
            all_cb.blockSignals(False)
            self._on_stream_selection_changed()

        all_cb.stateChanged.connect(on_all_toggled)
        for _, cb in cb_list:
            cb.stateChanged.connect(on_child_toggled)

        self._audio_stream_checkboxes = cb_list
        self._on_stream_selection_changed()

    def _on_stream_selection_changed(self):
        """When user toggles streams, save to the selected audio asset."""
        if not self._selected_audio:
            return
        selected = [idx for idx, cb in self._audio_stream_checkboxes if cb.isChecked()]
        all_indices = [idx for idx, _ in self._audio_stream_checkboxes]
        # empty list means "all streams" - but only if literally all are selected
        if set(selected) == set(all_indices):
            self._selected_audio.apply_to_streams = []
        else:
            self._selected_audio.apply_to_streams = selected

    def _probe_audio_streams(self, path):  # noqa
        """Return list of dicts describing each audio stream in the file."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return []
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a",
                 "-show_entries",
                 "stream=index,codec_name,channels:stream_tags=language,title",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=15
            )
            data = json.loads(out.stdout)
            streams = data.get("streams", [])
            result = []
            for s in streams:
                tags = s.get("tags") or {}
                result.append({
                    "index": s.get("index", 0),
                    "codec_name": s.get("codec_name", "?"),
                    "channels": s.get("channels", "?"),
                    "language": tags.get("language"),
                    "title": tags.get("title"),
                })
            return result
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Audio mix preview
    # ------------------------------------------------------------------
    PREVIEW_PAD = 3.0    # seconds of context before/after the audio window
    PREVIEW_MAX = 45.0   # hard cap on the rendered clip length

    def _preview_audio_mix(self):
        # Second click while playing = stop.
        if self.preview_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.preview_player.stop()
            return
        if self._preview_worker is not None and self._preview_worker.isRunning():
            return
        au = self._selected_audio
        if au is None:
            QMessageBox.information(self, "No audio selected",
                                     "Click an audio segment on the timeline first.")
            return
        if not self.video_path:
            QMessageBox.warning(self, "No video", "Load a source video first.")
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            QMessageBox.critical(self, "ffmpeg not found",
                                  "ffmpeg was not found on PATH. Install it and try again.")
            return
        streams = self._probe_audio_streams(self.video_path)
        if not streams:
            QMessageBox.information(self, "No audio streams",
                                     "The source video has no audio stream to mix with.")
            return

        # Preview the first targeted source stream (all streams share the
        # same treatment, so one is enough to hear the result).
        src_idx = au.apply_to_streams[0] if au.apply_to_streams else 0
        src_idx = min(src_idx, len(streams) - 1)

        clip_start = max(0.0, au.start - self.PREVIEW_PAD)
        clip_len = min(max(0.5, au.end + self.PREVIEW_PAD - clip_start), self.PREVIEW_MAX)

        # Release the previous preview file (Windows won't overwrite it while
        # the player still holds it open) and keep the main video quiet.
        self.preview_player.stop()
        self.preview_player.setSource(QUrl())
        self.player.pause()
        self.play_btn.setText("Play")

        # Same filter chain as the export, with times shifted so the clip
        # starts at 0 (the input is seeked with -ss).
        parts, out_label = self._audio_replacement_filters(
            f"0:a:{src_idx}", 1, au, "preview", offset=clip_start)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{clip_start:.3f}", "-i", self.video_path,
            "-i", au.asset_path,
            "-filter_complex", ";".join(parts),
            "-map", f"[{out_label}]",
            "-t", f"{clip_len:.3f}",
            "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
            self._preview_path,
        ]

        self.audio_preview_btn.setEnabled(False)
        self.audio_preview_btn.setText("Rendering preview...")
        self._preview_info = (
            f"Previewing {self.timeline._fmt_time(clip_start)}"
            f"-{self.timeline._fmt_time(clip_start + clip_len)}  "
            f"(mode: {au.mode})"
        )
        worker = ExportWorker(cmd, self)
        worker.finished_export.connect(self._on_audio_preview_rendered)
        worker.start()
        self._preview_worker = worker

    def _on_audio_preview_rendered(self, exit_code, log_text):
        self.audio_preview_btn.setEnabled(True)
        self.audio_preview_btn.setText("Preview Audio Mix")
        if exit_code != 0:
            tail = "\n".join(log_text.splitlines()[-15:])
            QMessageBox.critical(self, "Preview failed",
                                  f"ffmpeg could not render the preview.\n\n{tail}")
            return
        self.preview_player.setSource(QUrl.fromLocalFile(self._preview_path))
        self.preview_player.play()
        self.statusBar().showMessage(self._preview_info, 8000)

    def _on_preview_state_changed(self, state):
        if not self.audio_preview_btn.isEnabled():
            return  # mid-render; the button text is managed there
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.audio_preview_btn.setText("Stop Preview" if playing else "Preview Audio Mix")

    def closeEvent(self, event):
        self.preview_player.stop()
        self.preview_player.setSource(QUrl())
        try:
            os.remove(self._preview_path)
        except OSError:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    def _on_rate_mode_changed(self, index):
        is_crf = (index == 0)
        self.crf_spin.setEnabled(is_crf)
        self.bitrate_spin.setEnabled(not is_crf)

    def _on_audio_mode_changed(self, index):
        # index 0 = Replace, 1 = Overlay, 2 = Duck
        is_duck = (index == 2)
        self.audio_duck_db_spin.setEnabled(is_duck)
        # Save to selected audio
        if self._selected_audio:
            self._selected_audio.mode = ["replace", "overlay", "duck"][index]
            self.timeline.update()

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

    def _probe_audio_duration(self, path):
        """Return the duration of an audio file in seconds, or None."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=15
            )
            data = json.loads(out.stdout)
            dur = (data.get("format") or {}).get("duration")
            return float(dur) if dur else None
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

    @staticmethod
    def _audio_replacement_filters(current_label, inp_idx, au, tag, offset=0.0):
        """Filter parts for ONE audio replacement, shared by export and the
        audio preview so both always sound the same.

        current_label: label of the audio chain so far (e.g. "0:a:0")
        inp_idx:       ffmpeg input index of the replacement audio file
        tag:           unique suffix for the intermediate labels
        offset:        seconds subtracted from au.start/au.end (the preview
                       seeks into the source, so its timeline starts later)

        Returns (filter_parts, output_label).
        """
        ducked_label = f"ducked_{tag}"
        delayed_label = f"delayed_{tag}"
        out_label = f"amix_{tag}"
        start = au.start - offset
        end = au.end - offset

        # 1. Modify the source audio during [start, end], depending on mode.
        mode = getattr(au, "mode", "replace")
        if mode == "replace":
            # Silence the source completely in the window.
            vol_expr = "volume=0"
        elif mode == "overlay":
            # Keep the source at full volume - no change needed.
            vol_expr = "volume=1.0"
        elif mode == "duck":
            # Lower the source volume by duck_db (e.g. -12 dB).
            # volume filter accepts a linear multiplier, so convert.
            duck_linear = 10 ** (au.duck_db / 20.0)
            vol_expr = f"volume={duck_linear:.4f}"
        else:
            vol_expr = "volume=0"

        parts = [
            f"[{current_label}]"
            f"{vol_expr}:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
            f"[{ducked_label}]"
        ]

        # 2. Trim the replacement to at most (end - start) long, then delay
        #    it so it starts at `start`, then apply the user's volume
        #    multiplier. Without the atrim, a replacement clip longer than
        #    its window would keep playing past the window and overlap with
        #    the original audio un-muting at that point.
        window_dur = max(0.05, au.end - au.start)
        delay_ms = max(0, int(start * 1000))
        new_vol = getattr(au, "new_volume", 1.0)
        parts.append(
            f"[{inp_idx}:a]"
            f"atrim=0:{window_dur:.3f},asetpts=PTS-STARTPTS,"
            f"adelay={delay_ms}|{delay_ms},"
            f"volume={new_vol:.4f}"
            f"[{delayed_label}]"
        )

        # 3. Mix the (possibly modified) source with the delayed replacement.
        #    For "overlay" mode the source is at full volume and the
        #    replacement is added on top. For "replace" the source is silent,
        #    so effectively only the new audio is heard. duration=first keeps
        #    the original stream length.
        parts.append(
            f"[{ducked_label}][{delayed_label}]"
            f"amix=inputs=2:duration=first:dropout_transition=0"
            f":normalize=0"
            f",alimiter=limit=0.95"
            f"[{out_label}]"
        )
        return parts, out_label

    def _build_ffmpeg_command(self, output_path):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg was not found on PATH.")

        stats = self._probe_source_stats(self.video_path)
        self._source_codec = stats.get("codec", "unknown")
        width, height = stats["width"], stats["height"]
        if not width or not height:
            raise RuntimeError("Could not determine source video resolution.")

        apply_image = self.apply_image_cb.isChecked()
        apply_audio = self.apply_audio_cb.isChecked()

        args = [ffmpeg, "-y", "-i", self.video_path]

        # --- Input list: image watermarks + audio replacements ---
        # We only add inputs for what's actually enabled.
        image_filter_parts = []
        audio_filter_parts = []
        current_video_label = "0:v"
        input_index = 1

        image_inputs = []   # (input_index, wm)
        audio_inputs = []   # (input_index, au)

        if apply_image:
            for wm in self.watermarks:
                args += ["-i", wm.asset_path]
                image_inputs.append((input_index, wm))
                input_index += 1

        if apply_audio:
            for au in self.audio_assets:
                args += ["-i", au.asset_path]
                audio_inputs.append((input_index, au))
                input_index += 1

        # --- Video filter graph ---
        if apply_image and image_inputs:
            for k, (idx, wm) in enumerate(image_inputs):
                wm_w = max(2, round(width * wm.scale))
                scaled_label = f"wms{k}"
                image_filter_parts.append(f"[{idx}:v]scale={wm_w}:-1[{scaled_label}]")
                x_px = round(wm.x * width)
                y_px = round(wm.y * height)
                # Centered axes use ffmpeg's own expression, so the result is
                # exact to the pixel whatever the scaled watermark size is.
                x_expr = "(main_w-overlay_w)/2" if wm.center_x else str(x_px)
                y_expr = "(main_h-overlay_h)/2" if wm.center_y else str(y_px)
                out_label = f"v{k}"
                image_filter_parts.append(
                    f"[{current_video_label}][{scaled_label}]overlay="
                    f"x={x_expr}:y={y_expr}:"
                    f"enable='between(t,{wm.start:.3f},{wm.end:.3f})'[{out_label}]"
                )
                current_video_label = out_label

        # --- Audio filter graph ---
        # Strategy: for each replacement, split the source audio, mute the
        # selected window, and mix in the replacement audio at the same
        # window. Streams not selected in `apply_to_streams` are untouched.
        n_audio_in_source = len(self._probe_audio_streams(self.video_path))

        # We only apply audio replacement to selected streams. If a stream
        # is not selected, it goes through with `-c:a copy`.
        # Build per-stream filter graphs.
        audio_out_label_by_stream = {}   # source_stream_index -> final label or None

        if apply_audio and audio_inputs:
            for src_idx in range(n_audio_in_source):
                # Find replacements targeting this stream
                active = []
                for inp_idx, au in audio_inputs:
                    if au.apply_to_streams and src_idx not in au.apply_to_streams:
                        continue
                    active.append((inp_idx, au))
                if not active:
                    continue

                # Chain the replacements together instead of splitting the
                # source. Each replacement:
                #   1. mutes its window in the *current* chain output
                #   2. mixes in the delayed replacement audio
                #   3. passes the result on to the next replacement
                #
                # This avoids asplit entirely, which had a bug where the
                # extra output would go unconnected and ffmpeg would error
                # out with "Filter 'asplit' has output N unconnected".
                current_label = f"0:a:{src_idx}"

                for i, (inp_idx, au) in enumerate(active):
                    parts, current_label = self._audio_replacement_filters(
                        current_label, inp_idx, au, f"{src_idx}_{i}")
                    audio_filter_parts += parts

                audio_out_label_by_stream[src_idx] = current_label

        # --- Emit filter_complex ---
        all_filters = image_filter_parts + audio_filter_parts
        if all_filters:
            args += ["-filter_complex", ";".join(all_filters)]

        # --- Video mapping / codec ---
        if apply_image and image_inputs:
            args += ["-map", f"[{current_video_label}]"]
            args += self._detect_video_encoder_args()
        else:
            args += ["-map", "0:v"]
            args += ["-c:v", "copy"]

        # --- Audio mapping / codec ---
        # Determine output audio codec from user selection
        chosen_audio_codec = self.audio_codec_combo.currentText()
        audio_bitrate = int(self.audio_bitrate_spin.value())
        if chosen_audio_codec.startswith("Auto"):
            # Match the first source audio codec, fall back to aac
            streams = self._probe_audio_streams(self.video_path)
            src_a = streams[0]["codec_name"] if streams else "aac"
            codec_map = {
                "aac": "aac", "opus": "libopus", "mp3": "libmp3lame",
                "vorbis": "libvorbis", "flac": "flac", "pcm_s16le": "pcm_s16le",
            }
            chosen_audio_codec = codec_map.get(src_a, "aac")

        for src_idx in range(n_audio_in_source):
            if src_idx in audio_out_label_by_stream:
                # This stream is modified: use the filter output
                args += ["-map", f"[{audio_out_label_by_stream[src_idx]}]"]
                args += [f"-c:a:{src_idx}", chosen_audio_codec]
                if chosen_audio_codec not in ("flac", "pcm_s16le"):
                    args += [f"-b:a:{src_idx}", f"{audio_bitrate}k"]
            else:
                # This stream is untouched: straight copy
                args += ["-map", f"0:a:{src_idx}"]
                args += [f"-c:a:{src_idx}", "copy"]

        # --- Subtitles: always copy ---
        args += ["-map", "0:s?", "-c:s", "copy"]

        # --- Metadata ---
        args += ["-map_metadata", "0", "-map_chapters", "0"]
        # Per-stream metadata (language tags, titles) needs an explicit index
        # per track - a bare "-map_metadata:s 0:s" isn't documented ffmpeg
        # syntax for "every stream of this type" and audio had no per-stream
        # metadata mapping at all, so language tags on audio tracks were
        # likely being silently dropped the same way subtitle tags were in
        # earlier testing.
        n_audio_total, n_sub_total = self._probe_stream_counts(self.video_path)
        for i in range(n_audio_total):
            args += [f"-map_metadata:s:a:{i}", f"0:s:a:{i}"]
        for i in range(n_sub_total):
            args += [f"-map_metadata:s:s:{i}", f"0:s:s:{i}"]

        # Safety nets, neither of which touches encoding quality/structure:
        # - "-t" hard-caps the output at the video's own known duration, so
        #   a mismatched-length secondary stream (e.g. a longer commentary
        #   audio track - a real authoring defect seen in some files) can't
        #   silently extend the whole export past the actual video content.
        # - "-max_muxing_queue_size" guards against a genuine hang on long
        #   files where the muxer has to buffer packets while reordering.
        total_duration = self.timeline.duration
        if total_duration and total_duration > 0:
            args += ["-t", f"{total_duration:.3f}"]
        args += ["-max_muxing_queue_size", "9999"]

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
        if not self.apply_image_cb.isChecked() and not self.apply_audio_cb.isChecked():
            QMessageBox.warning(self, "Nothing to do",
                                 "Enable at least one of: image watermarks or audio replacement.")
            return
        if not shutil.which("ffmpeg"):
            QMessageBox.critical(self, "ffmpeg not found",
                                  "ffmpeg was not found on PATH. Install it and try again.")
            return

        summary = []
        if self.apply_image_cb.isChecked():
            summary.append(f"{len(self.watermarks)} image watermark(s)")
        if self.apply_audio_cb.isChecked():
            summary.append(f"{len(self.audio_assets)} audio replacement(s)")
        QMessageBox.information(
            self, "Export Summary",
            f"About to export with:\n\n" + "\n".join(summary)
        )

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
                pct = min(100, int(100 * done_sec / total_duration))
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