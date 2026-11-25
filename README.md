# Linux Dictation (`~/Dev/linux-dictation`)

Wayland/KDE desktop voice dictation unifying both hotkey pipelines and a shared instant-paste helper:

- **Whisper (`Num++`):** records 16 kHz mono WAV via PipeWire (with automatic direct-ALSA fallback), queues completed recordings in `~/.local/state/assistant-desktop-dictation/queue/`, uploads to `http://127.0.0.1:5001/transcribe_raw` (`whisper_service.py`), and inserts the transcript.
- **Rambler (`Num+-`):** persistent live-streaming daemon (`stream-daemon.py`) that captures 16 kHz PCM from PipeWire/ALSA, encodes 100 ms Ogg/Opus pages with `ffmpeg`, and streams over HTTP/2 gRPC (`S3Bidi`) to Google’s `beyond-a2a-recognizer` (Gemini v3, profanity filter off, `en-GB` default), with 3–4 minute rolling segments, local Whisper fallback, and failed-WAV preservation under `~/.local/state/rambler-desktop-dictation/failed/`.
- **Shared text insertion (`wayland-type-helper.sh`):** instant tagged-clipboard paste via CopyQ (`application/x-assistant-dictation-transient`, ignored by CopyQ history) with automatic clipboard restore, `Ctrl+V` / `Ctrl+Shift+V` routing, `wl-copy`/`wl-paste` fallback, and `ydotool type` fallback.

## Project Structure

```text
linux-dictation/
├── pyproject.toml                       # uv environment (httpx[http2])
├── wayland-type-helper.sh               # Shared instant paste + ydotool fallback helper
├── whisper/
│   ├── toggle.sh                        # Num++ start/stop toggle & PipeWire health check
│   ├── queue-worker.sh                  # Persistent FIFO queue & backoff retry worker
│   ├── transcribe-and-type.py           # Uploads WAV to :5001/transcribe_raw and inserts text
│   └── net.local.linux-dictation-whisper.desktop # KDE launcher for Num++
├── rambler/
│   ├── toggle.sh                        # Num+- socat toggle (auto-starts stream-daemon.py)
│   ├── stream-daemon.py                 # Live-streaming Rambler daemon with rolling segments
│   ├── s3.py                            # S3Bidi Gemini v3 protobuf + HTTP/2 client
│   └── net.local.linux-dictation-rambler.desktop # KDE launcher for Num+-
├── docs/
│   └── desktop-recording-startup.md
└── tests/
    ├── test_desktop_dictation.py
    ├── test_desktop_recording_queue.py
    └── test_wayland_type_helper.py
```

## Setup & Execution Order

1. Install Python dependencies with `uv`:
   ```sh
   UV_CACHE_DIR=/data/.cache/uv uv sync
   ```
2. Install KDE launchers into `~/.local/share/applications/` and rebuild KDE's service cache:
   ```sh
   cp whisper/net.local.linux-dictation-whisper.desktop ~/.local/share/applications/
   cp rambler/net.local.linux-dictation-rambler.desktop ~/.local/share/applications/
   kbuildsycoca6 --noincremental
   ```
3. Bind `Desktop Dictation Toggle` to `Num++` and `Rambler Dictation Toggle` to `Num+-` in KDE Shortcuts.
4. For Whisper (`Num++`), ensure `whisper_service.py` is running on `127.0.0.1:5001`. For Rambler (`Num+-`), pressing the shortcut auto-starts `rambler/stream-daemon.py`.

## Running Tests

```sh
python3 -m unittest discover -s tests -v
shellcheck wayland-type-helper.sh whisper/toggle.sh whisper/queue-worker.sh rambler/toggle.sh
```
