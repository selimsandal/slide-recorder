# Slide Recorder

Native cross-platform desktop app for recording voiceovers one slide at a time.

The microphone stream stays open after startup, so recording starts by copying
audio from an already-running input stream instead of waiting for the mic device
to wake up.

## Features

- Separate mixed WAV export for every slide, with each take stored as its own
  movable recording clip.
- Record a new clip from the current cursor position without displaying slide
  content.
- Timeline-style waveform editor with drag selection and adjustable handles.
- Drag a clip header to move an individual recording portion.
- Overlapping clips are allowed. Newer clips play over older clips, lower clips
  are muted only for the covered portion, and moving a clip does not change its
  overlap priority. Priority can be changed explicitly from the waveform
  context menu.
- Fixed-scale, scrollable waveform viewport. Recordings are not automatically
  stretched to fit the window.
- Recorded audio clips are shown as distinct regions against empty timeline
  space, with overlapped portions marked in the clip strip.
- View controls for centering the current selection/playhead, fitting the whole
  recording to the window, and returning to the default timeline scale.
- Playback progress is shown in the time label and as a red playhead on the
  waveform.
- Transport uses two toggles: `Record` becomes `Stop Recording` while capturing,
  and `Play` becomes `Stop Playback` while playing.
- Right-click waveform actions for playing, trimming, cutting, clearing, or
  deleting audio or the selected clip.
- Keyboard editing on the focused waveform: Delete/Backspace cuts the selected
  range, Delete/Backspace deletes a selected clip when no range is selected,
  Enter trims to the range, Space toggles playback, and Escape clears.
- Undo audio edits with Ctrl+Z or `Edit > Undo`.
- Optional confirmation before deleting audio.
- Add, remove, and rename slides. Use Ctrl-click or Shift-click to select
  multiple slides for batch removal.
- Export one slide as WAV or all recorded slides as a ZIP.
- Export an MP4 slide video from a PDF whose page count matches the session
  slide count. Each PDF page is held for that slide's recorded audio duration,
  with H.264, H.265, and AV1 codec choices.
- Runs on Windows, macOS, and Linux with Python and PySide6 QtMultimedia.

## Run From Source With uv

```bash
uv python install 3.12
uv sync
uv run slide-recorder
```

On Windows, run the same commands from PowerShell:

```powershell
uv python install 3.12
uv sync
uv run slide-recorder
```

`uv` creates and manages the virtual environment for the project. If you do not
have it installed, follow the official installer for your platform:
https://docs.astral.sh/uv/getting-started/installation/

The repository pins Python 3.12 in `.python-version`.

The default session folder is `Documents/Slide Recorder`. It contains
`session.json` and a `recordings` directory with files such as
`slide_001.wav` for the slide mix and `slide_001_clip_0001.wav` for individual
takes.

If you choose the repository itself as a session folder while testing, the local
`session.json`, `recordings/`, WAV exports, MP4 exports, and ZIP exports are
ignored by git.

## Package

Install the packaging extra with `uv`:

```bash
uv sync --extra package
```

Windows/Linux:

```bash
uv run pyinstaller --name "Slide Recorder" --windowed --onedir --paths src packaging/pyinstaller_entry.py
```

macOS needs a microphone permission string in the app bundle. Use:

```bash
uv run pyinstaller packaging/SlideRecorder.spec
```

The packaged app will be written under `dist/`.

## Editing Workflow

1. Pick a slide from the list.
2. Put the cursor where the next take should begin.
3. Click `Record New Clip` to capture a separate recording portion.
   The same button changes to `Stop Recording` while recording.
4. Drag across the waveform timeline to select a region.
5. Drag either edge of the selection to adjust it, or drag inside the selected
   range to move it.
6. Drag the header strip at the top of a clip to move that whole recording
   portion.
   Moving a clip changes only its time position, not which clip wins in an
   overlap.
7. Use the horizontal scrollbar to move through longer recordings. Click `Fit`
   only when you want the full recording compressed into the visible timeline.
8. Right-click the waveform selection for edit actions, or use keyboard editing
   while the waveform is focused. Delete/Backspace cuts the selected range
   immediately without a confirmation dialog.
9. To change overlap priority, right-click an overlapped area and use
   `Clips Under Mouse > Bring Clip ... to Front`. This works even when a clip is
   fully hidden behind another clip.
10. Select a clip header and press Delete/Backspace to remove only that clip.
11. Use Ctrl+Z to undo the last audio edit for the current slide.
12. Click `Play` to preview from the cursor; while playing, click the same
    button again to stop playback.
13. Export current slide audio, all slide audio, or a PDF-backed slide video.

## Overlap Priority

Each recording take is stored as a separate clip with a stable priority layer.
When clips overlap, the frontmost clip is what you hear for the covered region;
the lower clip is muted only while covered and becomes audible again if the
front clip is moved, sent backward, or deleted.

Dragging a clip only changes its start time. To change which clip wins, use the
waveform context menu:

1. Right-click an overlapped part of the timeline.
2. Open `Clips Under Mouse`.
3. Choose `Bring Clip ... to Front` for the take you want to hear.

For an already selected clip, use `Bring Selected Clip to Front` or
`Send Selected Clip to Back` from the same context menu. These priority changes
are undoable with Ctrl+Z.

## PDF Video Export

Use `File > Export Slide Video from PDF...` to create an MP4 from a PDF and the
current slide recordings.

The PDF must have exactly the same number of pages as the session has slides.
Page 1 is paired with slide 1, page 2 with slide 2, and so on. For each slide,
the exporter renders the PDF page as a still frame and uses the mixed slide
audio as the segment audio. A 10-second slide 1 recording produces 10 seconds of
PDF page 1 in the output video, then the export continues with page 2 and slide
2 audio.

Slides without recordings can be included as one-second silent stills. Video is
encoded as 1920x1080 MP4 using the bundled `imageio-ffmpeg` executable, so users
do not need to install ffmpeg separately when running from source.

The exporter offers H.264/AVC, H.265/HEVC, and AV1 when the bundled ffmpeg build
supports them. It tries hardware encoders first when available, such as
VideoToolbox, NVENC, AMF, or QSV, then falls back to software encoding for the
same codec if hardware encoding is not available at runtime.
