# Linux Dictation (`~/Dev/linux-dictation`)

Wayland/KDE desktop voice dictation unifying both hotkey pipelines and a shared instant-paste helper:

- **Whisper (`Num++`):** records 16 kHz mono WAV via PipeWire (with automatic direct-ALSA fallback), queues completed recordings in `~/.local/state/assistant-desktop-dictation/queue/`, uploads to `http://127.0.0.1:5001/transcribe_raw` ([`transcription-server`](https://github.com/Terrydaktal/transcription-server)), and inserts the transcript.
- **Rambler (`Num+-`):** persistent live-streaming daemon (`stream-daemon.py`) that captures 16 kHz PCM from PipeWire/ALSA, encodes 100 ms Ogg/Opus pages with `ffmpeg`, and streams over HTTP/2 gRPC (`S3Bidi`) to Google’s `beyond-a2a-recognizer` (Gemini v3, profanity filter off, `en-GB` default), with 3–4 minute rolling segments, local Whisper fallback, and failed-WAV preservation under `~/.local/state/rambler-desktop-dictation/failed/`.
- **Shared text insertion (`wayland-type-helper.sh`):** instant tagged-clipboard paste via CopyQ (`application/x-assistant-dictation-transient`, ignored by CopyQ history) with automatic clipboard restore, `Ctrl+V` / `Ctrl+Shift+V` terminal routing, `wl-copy`/`wl-paste` fallback, and paced `ydotool type` fallback.

---

## Project Structure

```text
linux-dictation/
├── pyproject.toml                       # uv environment (httpx[http2])
├── wayland-type-helper.sh               # Shared instant paste + paced ydotool fallback helper
├── whisper/
│   ├── toggle.sh                        # Num++ start/stop toggle, PipeWire health check & ALSA fallback
│   ├── queue-worker.sh                  # Persistent FIFO queue & exponential-backoff retry worker
│   ├── transcribe-and-type.py           # Uploads WAV to :5001/transcribe_raw, inserts text, reports delivery
│   └── net.local.linux-dictation-whisper.desktop # KDE launcher for Num++
├── rambler/
│   ├── toggle.sh                        # Num+- socat toggle (auto-starts stream-daemon.py)
│   ├── stream-daemon.py                 # Combined live-streaming Rambler daemon (~37 MB RAM, ~2 ms toggle)
│   ├── s3.py                            # S3Bidi Gemini v3 protobuf + HTTP/2 client
│   └── net.local.linux-dictation-rambler.desktop # KDE launcher for Num+-
├── docs/
│   └── desktop-recording-startup.md     # PipeWire vs ALSA microphone startup latency audit
└── tests/
    ├── test_desktop_dictation.py
    ├── test_desktop_recording_queue.py
    └── test_wayland_type_helper.py
```

---

## How Both Pipelines Work

### Whisper (`Num++`) vs Rambler (`Num+-`) Background Processes
1. **Where the model runs:**
   - **Whisper (`Num++`)** runs locally on your GPU via [`~/Dev/transcription-server/whisper_service.py`](https://github.com/Terrydaktal/transcription-server) (`127.0.0.1:5001`, keeping the model warm in VRAM), paired with a lightweight FIFO queue consumer (`whisper/queue-worker.sh`) that sends finished WAVs and inserts the returned text.
   - **Rambler (`Num+-`)** runs remotely on Google’s S3 endpoint (`speechs3proto2-pa.googleapis.com`), so no local model server is needed — only `rambler/stream-daemon.py` runs on your machine.
2. **Combined Rambler daemon (`rambler/stream-daemon.py`):**
   - Handles recording control, live Ogg/Opus streaming, and text insertion inside one lightweight Python process (~37 MB RAM) listening on `$XDG_RUNTIME_DIR/rambler-desktop-dictation/control.sock` so `rambler/toggle.sh` starts/stops a stream in ~2 ms without paying Python startup cost on every keypress.

### Recording, PipeWire Latency & Direct-ALSA Fallback
- First press starts a uniquely named 16 kHz mono PCM16 WAV capture; second press stops and queues it. Recording controls remain completely independent of network uploads and retries.
- PipeWire is invoked with `--latency 10ms` to reduce capture buffering without keeping the mic open between recordings or pinning the global graph quantum. The [microphone startup audit](docs/desktop-recording-startup.md) measured a median of **22 ms** from toggle launch to first WAV samples (vs **31 ms** with automatic buffering).
  - Override with `ASSISTANT_PIPEWIRE_LATENCY_MS` (`1`–`1000`, or `0` for automatic buffering).
- A background health check starts after **750 ms** (`ASSISTANT_PIPEWIRE_HEALTH_DELAY_S`) and gives a live PipeWire process up to **3 seconds** (`ASSISTANT_PIPEWIRE_STARTUP_TIMEOUT_MS`), polled every 100 ms, to produce audio beyond the 44-byte WAV header without delaying Stop.
- If PipeWire exits or misses the startup deadline, capture falls back automatically to direct ALSA (`plughw`), preferring `Yeti Stereo Microphone` or otherwise the first capture device (`ASSISTANT_ALSA_DEVICE=plughw:<card>,<device>` overrides detection). Any audio arriving before fallback or flushed on shutdown is preserved, never overwritten.

### Durable Offline Queue & Delivery Reporting (Whisper `Num++`)
- Completed recordings wait under `$XDG_STATE_HOME/assistant-desktop-dictation/queue/` and survive server restarts or crashes.
- `whisper/queue-worker.sh` sleeps on `$XDG_RUNTIME_DIR/assistant-desktop-dictation/worker-wakeup.fifo` when idle (so new recordings cannot race worker exit) and retries unavailable-server errors with exponential backoff (`ASSISTANT_QUEUE_RETRY_INITIAL_S`, default `1`; `ASSISTANT_QUEUE_RETRY_MAX_S`, default `30`).
- Starting `whisper_service.py` (or invoking `whisper/queue-worker.sh`) immediately wakes the existing worker rather than waiting for the backoff timer or spawning a second consumer.
- Logs are written to `$XDG_STATE_HOME/assistant-desktop-dictation/dictation.log`; control locks live under `$XDG_RUNTIME_DIR/assistant-desktop-dictation/`.
- `whisper/transcribe-and-type.py` reports delivery timing and modifier refusals to `/desktop-delivery-report`; refused text is never retried as delayed keyboard input.

### Shared Text Insertion (`wayland-type-helper.sh`)
- **Default (`ASSISTANT_INSERT_MODE=paste`):** publishes UTF-8 text via CopyQ tagged with `application/x-assistant-dictation-transient` (ignored by CopyQ’s history override), sends `Ctrl+V` for GUI apps and `xfce4-terminal` or `Ctrl+Shift+V` for other terminal emulators, and restores the prior clipboard. Falls back to `wl-copy`/`wl-paste` if CopyQ is unavailable, or to `ydotool type` if clipboard tools are unavailable.
- **Paced typing fallback (`ASSISTANT_INSERT_MODE=type`):** uses a paced 3-character lead-in (10 ms key holds, 20 ms between characters, 30 ms settling pause; ~100 ms per message) before the fast remainder to avoid dropped opening characters in web editors.
- Both modes verify no modifier keys are held before insertion (and monitor for modifiers pressed during typing). The destination editor must already be focused; the helper never steals focus or inserts sacrificial characters.

---

## Setup & Execution Order

1. Install Python dependencies with `uv`:
   ```sh
   cd ~/Dev/linux-dictation
   UV_CACHE_DIR=/data/.cache/uv uv sync
   ```
2. Install KDE launchers into `~/.local/share/applications/` and rebuild KDE’s service cache:
   ```sh
   cp whisper/net.local.linux-dictation-whisper.desktop ~/.local/share/applications/
   cp rambler/net.local.linux-dictation-rambler.desktop ~/.local/share/applications/
   kbuildsycoca6 --noincremental
   ```
3. Bind `Desktop Dictation Toggle` to `Num++` and `Rambler Dictation Toggle` to `Num+-` in KDE Shortcuts.
4. For Whisper (`Num++`), start `whisper_service.py` from `~/Dev/transcription-server` (`127.0.0.1:5001`) — or record offline and let the queue drain automatically when the server starts. For Rambler (`Num+-`), pressing `Num+-` auto-starts `rambler/stream-daemon.py`.

---

## Running Tests & Shell Validation

```sh
cd ~/Dev/linux-dictation
python3 -m unittest discover -s tests -v
shellcheck wayland-type-helper.sh whisper/toggle.sh whisper/queue-worker.sh rambler/toggle.sh
```
