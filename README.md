# WaterMark PipeLine

A desktop tool for placing visual watermarks on a video and replacing or mixing in audio over set time ranges. Everything is laid out on a visual timeline, and the final file is rendered with FFmpeg.

The app is built with PySide6 (Qt for Python). The preview is only a layout aid. The final pixel-accurate output is produced by FFmpeg at export time.

## Features

**Video watermarks**
- Load images (PNG, JPG, BMP, WebP, TIFF), animated GIFs, or short video clips as watermark assets.
- Drag an asset from the asset list onto the video preview to place it.
- Move a watermark by dragging it on the preview. Set its size as a fraction of the video width.
- Each watermark has its own start and end time. Edit them on the timeline (drag the bar to move it, drag an edge to resize it) or type exact values in the properties panel.

**Audio replacement**
- Load audio files (MP3, WAV, AAC, FLAC, M4A, OGG, Opus, WMA) and place them on the timeline at the playhead position.
- Three modes for each audio segment:
  - **Replace**: the source audio is muted inside the segment and only the new audio plays.
  - **Overlay**: the new audio is mixed on top of the source at full volume.
  - **Duck**: the source is turned down by a set amount (in dB) and the new audio plays on top.
- A separate volume multiplier for the new audio.
- Choose which audio streams of the source file to change, for files with more than one audio track (for example, several languages).
- Audio segments keep the length of their source file. They can be moved on the timeline but not resized.

**Export**
- Video encoders: libx264, libx265, NVIDIA NVENC, Intel Quick Sync (QSV), and AMD AMF, for both H.264 and HEVC.
- **Auto** mode test-encodes a short clip on each GPU encoder to find one that actually works on this machine, then falls back to a software encoder that matches the source codec.
- Rate control by CRF (constant quality) or by target bitrate. A **Match Source Bitrate** button reads the source bitrate for you.
- When no image watermarks are applied, the video stream is copied without re-encoding.
- Audio streams you did not change are copied without re-encoding.
- Subtitles, chapters, and global and per-stream metadata (such as language tags) are kept.
- Output as MKV, MP4, or MOV.
- **Copy FFmpeg Command** copies the exact command the export would run to the clipboard, so you can inspect it or run it yourself.
- FFmpeg runs on a background thread with live progress and speed, so the window stays responsive during long encodes.

## Requirements

- Python 3.9 or newer
- PySide6, including the QtMultimedia module (part of the standard `PySide6` package)
- FFmpeg and ffprobe available on your `PATH`. A recent release (5.0 or newer) is recommended.
- Optional: a supported GPU and driver for hardware encoding (NVIDIA, Intel, or AMD)

## Installation

```bash
git clone https://github.com/amirroox/WaterMark_PipeLine.git
cd WaterMark_PipeLine

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install PySide6
```

### Installing FFmpeg

- **Windows**: download a build from https://www.gyan.dev/ffmpeg/builds/, unzip it, and add its `bin` folder to your `PATH` environment variable.
- **macOS**: `brew install ffmpeg`
- **Debian / Ubuntu**: `sudo apt install ffmpeg`

To check the installation, run:

```bash
ffmpeg -version
ffprobe -version
```

## Usage

```bash
python watermark_editor.py
```

### Basic workflow

1. **Load a video.** Drag a video file onto the preview area, or click **Open Video...**.
2. **Add watermark assets.** Click **Add Watermark Asset...** and choose an image, GIF, or video clip. The asset appears in the asset list.
3. **Place a watermark.** Drag an asset from the list onto the video preview. It is created at the drop position, starting at the current playhead time and lasting 5 seconds.
4. **Adjust the watermark.** Drag it on the preview to reposition it. Use the **Selected Watermark** panel to change its width, start, and end. You can also drag its bar on the timeline.
5. **Add audio (optional).** Click **Add Audio Asset...**, then double-click the file in the audio list to place it on the timeline at the current playhead position. Choose a mode, volume, and target streams in the **Selected Audio** panel.
6. **Choose encoding settings.** Pick a rate mode (CRF or bitrate), preset, and encoder, or leave the encoder on Auto.
7. **Export.** Under **Export Options**, choose whether to apply image watermarks, audio replacement, or both. Then click **Export Video...** and pick an output file.

### Timeline controls

| Action                                         | Result                                     |
|------------------------------------------------|--------------------------------------------|
| Click or drag on the ruler                     | Seek the video                             |
| Drag the middle of a watermark bar             | Move the watermark in time                 |
| Drag the left or right edge of a watermark bar | Change the start or end time               |
| Drag an audio bar                              | Move the audio segment in time             |
| Click a bar                                    | Select that watermark or audio segment     |
| Zoom slider                                    | Change the timeline scale                  |
| Fit                                            | Fit the whole video into the timeline view |

Watermark rows are blue and audio rows are green. The selected item is highlighted, and the red line is the playhead.

### Encoding settings

| Setting | Description                                                                                                                                                    |
|---------|----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Mode    | **CRF** keeps a constant quality level (recommended). **Bitrate** targets a fixed file size.                                                                   |
| CRF     | Lower means better quality and a larger file. 18 is close to visually lossless, 23 is the default, and 28 is smaller with visible loss.                        |
| Preset  | Speed versus compression trade-off for software encoders. Slower presets compress better.                                                                      |
| Bitrate | Target video bitrate in kbps, used in Bitrate mode.                                                                                                            |
| Encoder | **Auto** tests GPU encoders in the order NVENC, QSV, AMF (HEVC first, then H.264) and falls back to libx264 or libx265. You can also force a specific encoder. |

After an export, or after **Copy FFmpeg Command**, the label above the export buttons shows which encoder was chosen. Hover over it to see why each GPU encoder was or was not used.

## How it works

The application builds a single FFmpeg command with a `filter_complex` graph:

- **Watermarks**: each asset is scaled to its configured width and composited with the `overlay` filter. It is shown only between its start and end times, using `enable='between(t,start,end)'`.
- **Audio**: for each targeted source stream, the replacements are chained one after another. Each step changes the source volume inside its window based on the mode, then trims and delays the new audio so it starts at the right time, and mixes the two with `amix`.

Streams that were not changed are mapped straight through with `-c copy`.

## Project structure

```
WaterMark_PipeLine/
  watermark_editor.py   Main application (UI, timeline, FFmpeg command builder, export worker)
  .gitignore
  README.md
```

## Known limitations

- The preview shows the first frame of GIF and video watermarks as a still image. Animation only appears in the exported file.
- Video and GIF watermarks start playing from the beginning of the output, not from the watermark's start time (see the issue list).
- Projects cannot be saved or loaded yet. Closing the app discards the current layout.
- There is no undo or redo.
- An export cannot be canceled once it has started.

## License

No license has been chosen yet. Until one is added, all rights are reserved by the author.
