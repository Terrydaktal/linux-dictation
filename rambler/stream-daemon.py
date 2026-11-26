#!/usr/bin/env python3
"""Persistent live-streaming Rambler S3 dictation daemon.

Mirrors Gboard's phone Rambler flow on the desktop, with unlimited-length safety:
- On START (1st Numpad Minus): immediately opens PipeWire raw 16 kHz PCM capture
  (with automatic direct-ALSA fallback), tees PCM to a temporary WAV on disk,
  live-encodes 100 ms Ogg/Opus pages via ffmpeg, and streams chunks in real time
  over HTTP/2 S3Bidi to beyond-a2a-recognizer (Gemini v3) while you speak.
- Rolling S3 segments: if a recording reaches RAMBLER_SEGMENT_SOFT_S (default
  180 s), the daemon rolls cleanly to a fresh ffmpeg + S3Bidi stream on the next
  brief silence pause (or at RAMBLER_SEGMENT_HARD_S, default 240 s). Earlier
  segments finish transcribing in the background while you keep speaking, avoiding
  Google's ~5-minute stream cap and keeping post-stop latency low.
- On STOP (2nd Numpad Minus): stops capture, flushes the active segment, waits
  for all segments in order, joins their transcripts, and pipes the result into
  the shared Wayland ydotool paste helper.
- Failure safety: if an S3 segment fails after buffered retry, the daemon
  optionally falls back to local Whisper (127.0.0.1:5001) and preserves the
  finalized WAV under ~/.local/state/rambler-desktop-dictation/failed/ if
  transcription still fails so audio is never lost.
"""
from __future__ import annotations

import array
from collections import deque
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import wave

RAMBLER_DIR = Path(__file__).resolve().parent
if str(RAMBLER_DIR) not in sys.path:
    sys.path.insert(0, str(RAMBLER_DIR))

import httpx  # noqa: E402
import s3  # noqa: E402

RUNTIME_ROOT = Path(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
) / "rambler-desktop-dictation"
STATE_ROOT = Path(
    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
) / "rambler-desktop-dictation"
ACTIVE_DIR = STATE_ROOT / "active"
FAILED_DIR = STATE_ROOT / "failed"
LAST_DELIVERED_WAV = STATE_ROOT / "last_delivered.wav"
MAX_LAST_DELIVERED_BYTES = 10 * 1024 * 1024
SOCK_PATH = RUNTIME_ROOT / "control.sock"
LOCK_PATH = RUNTIME_ROOT / "daemon.lock"
PID_PATH = RUNTIME_ROOT / "daemon.pid"
LOG_PATH = STATE_ROOT / "dictation.log"

TYPE_HELPER = Path(
    os.environ.get(
        "RAMBLER_TYPE_HELPER",
        str(Path(__file__).resolve().parent.parent / "wayland-type-helper.sh"),
    )
)
PIPEWIRE_LATENCY_MS = os.environ.get(
    "RAMBLER_PIPEWIRE_LATENCY_MS",
    os.environ.get("ASSISTANT_PIPEWIRE_LATENCY_MS", "10"),
)
PIPEWIRE_HEALTH_DELAY_S = float(
    os.environ.get(
        "RAMBLER_PIPEWIRE_HEALTH_DELAY_S",
        os.environ.get("ASSISTANT_PIPEWIRE_HEALTH_DELAY_S", "0.75"),
    )
)
PIPEWIRE_STARTUP_TIMEOUT_MS = int(
    os.environ.get(
        "RAMBLER_PIPEWIRE_STARTUP_TIMEOUT_MS",
        os.environ.get("ASSISTANT_PIPEWIRE_STARTUP_TIMEOUT_MS", "3000"),
    )
)
CONFIGURED_ALSA_DEVICE = os.environ.get(
    "RAMBLER_ALSA_DEVICE", os.environ.get("ASSISTANT_ALSA_DEVICE", "")
)

# Rolling segment limits (well under Google S3's ~5-minute stream cap)
SEGMENT_SOFT_S = float(os.environ.get("RAMBLER_SEGMENT_SOFT_S", "180"))
SEGMENT_HARD_S = float(os.environ.get("RAMBLER_SEGMENT_HARD_S", "240"))
SILENCE_RMS_THRESHOLD = int(os.environ.get("RAMBLER_SILENCE_RMS", "400"))
SILENCE_PAUSE_CHUNKS = int(os.environ.get("RAMBLER_SILENCE_PAUSE_CHUNKS", "3"))  # 3 * 100 ms
TAIL_DRAIN_MS = int(os.environ.get("RAMBLER_TAIL_DRAIN_MS", "280"))
TAIL_RMS_WINDOW_CHUNKS = 20  # ~2.0 s at 100 ms/chunk

# Local Whisper fallback on S3 outage/error
WHISPER_FALLBACK_ENABLED = os.environ.get("RAMBLER_WHISPER_FALLBACK", "1") != "0"
WHISPER_SERVER_URL = os.environ.get(
    "WHISPER_SERVER_URL", "http://127.0.0.1:5001/transcribe_raw"
)

SAMPLE_RATE = 16000
PCM_CHUNK_BYTES = 3200  # 100 ms of 16 kHz mono s16le

_log_lock = threading.Lock()


def log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ms = int((time.time() % 1) * 1000)
    line = f"{ts}.{ms:03d} - {message}\n"
    with _log_lock:
        try:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        if sys.stderr.isatty():
            sys.stderr.write(line)
            sys.stderr.flush()


def pcm_rms(pcm_bytes: bytes) -> float:
    n = len(pcm_bytes) // 2
    if n <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm_bytes[: n * 2])
    total = sum(s * s for s in samples)
    return math.sqrt(total / n)


def find_direct_alsa_device() -> str | None:
    if CONFIGURED_ALSA_DEVICE:
        return (
            CONFIGURED_ALSA_DEVICE
            if ":" in CONFIGURED_ALSA_DEVICE
            else f"plughw:{CONFIGURED_ALSA_DEVICE}"
        )
    if not shutil.which("arecord"):
        log("ERROR: direct ALSA fallback unavailable: arecord not in PATH")
        return None
    try:
        proc = subprocess.run(
            ["arecord", "-l"],
            env={**os.environ, "LC_ALL": "C"},
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: ALSA capture-device lookup failed: {exc}")
        return None
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout).replace("\n", "; ").strip()
        log(f"ERROR: ALSA capture-device lookup failed status={proc.returncode}: {details}")
        return None

    first_device: str | None = None
    for line in proc.stdout.splitlines():
        m_card = re.search(r"^card (\d+)", line)
        m_dev = re.search(r"device (\d+)", line)
        if not (m_card and m_dev):
            continue
        dev = f"plughw:{m_card.group(1)},{m_dev.group(1)}"
        if first_device is None:
            first_device = dev
        if "yeti stereo microphone" in line.lower():
            return dev
    return first_device


def stop_proc(proc: subprocess.Popen | None, description: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        log(f"Recorder did not stop after 2 seconds; terminating {description} pid={proc.pid}")
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass


def type_text(text: str) -> tuple[int, bool, str]:
    started = time.monotonic()
    result = subprocess.run(
        [str(TYPE_HELPER)], input=text, text=True, capture_output=True, check=False
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if result.returncode == 0:
        log(f"Rambler transcription typed with ydotool type_ms={elapsed_ms}")
        return elapsed_ms, True, ""

    details = (result.stderr or result.stdout).strip()
    if result.returncode == 5:
        log(f"{details} type_ms={elapsed_ms}")
        reason = (
            "modifier_pressed_during_typing"
            if "during typing" in details
            else "modifier_key_held_before_typing"
        )
        return elapsed_ms, False, reason
    raise RuntimeError(details or f"Typing helper exited with status {result.returncode}")


def transcribe_via_whisper(wav_path: Path) -> str:
    audio_data = wav_path.read_bytes()
    req = urllib.request.Request(
        WHISPER_SERVER_URL,
        data=audio_data,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)
    return str(payload.get("text", "")).strip()


class ClientPool:
    """Maintains a warm HTTP/2 httpx.Client with clean recovery on transport errors."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client = self._new_client()

    @staticmethod
    def _new_client() -> httpx.Client:
        return httpx.Client(
            http2=True,
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0),
        )

    def get(self) -> httpx.Client:
        with self._lock:
            return self._client

    def reset(self, old: httpx.Client) -> httpx.Client:
        with self._lock:
            if self._client is old:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._client = self._new_client()
            return self._client


def extract_grpc_status(headers, trailers=None) -> tuple[str, str]:
    """Return (grpc_status, grpc_message) from HTTP/2 headers or trailers."""
    status = ""
    message = ""
    for source in (trailers, headers):
        if not source:
            continue
        items = source.items() if hasattr(source, "items") else source
        for raw_k, raw_v in items:
            k = (raw_k.decode("latin-1") if isinstance(raw_k, bytes) else str(raw_k)).lower()
            v = raw_v.decode("utf-8", errors="replace") if isinstance(raw_v, bytes) else str(raw_v)
            if k == "grpc-status" and not status:
                status = v.strip()
            elif k == "grpc-message" and not message:
                message = v.strip()
    return status, message


class S3Segment:
    """One live ffmpeg + S3Bidi streaming segment (<= ~3-4 minutes)."""

    def __init__(self, index: int, pool: ClientPool) -> None:
        self.index = index
        self._pool = pool
        self.started_monotonic = time.monotonic()
        self.pcm_bytes = 0
        self.ogg_bytes = 0
        self.max_rms = 0.0
        self._recent_rms: deque[int] = deque(maxlen=TAIL_RMS_WINDOW_CHUNKS)
        self.flushed_ms = -1
        self.finished_ms = -1
        self.text = ""
        self.error: str | None = None
        self.used_buffered_retry = False
        self.last_call_stats: dict[str, int | str] = {}

        self._ogg_q: queue.Queue[bytes | None] = queue.Queue()
        self._ogg_backup: list[bytes] = []
        self._closed_input = threading.Event()
        self._encoder_done = threading.Event()
        self._done = threading.Event()
        self._ffmpeg = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-frame_duration",
                "20",
                "-page_duration",
                "100000",
                "-flush_packets",
                "1",
                "-f",
                "ogg",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._drain_ogg_loop, daemon=True).start()
        threading.Thread(target=self._run_s3_loop, daemon=True).start()

    def write_pcm(self, data: bytes) -> None:
        if self._closed_input.is_set() or self._ffmpeg.stdin is None:
            return
        self.pcm_bytes += len(data)
        rms = pcm_rms(data)
        if rms > self.max_rms:
            self.max_rms = rms
        self._recent_rms.append(int(rms))
        try:
            self._ffmpeg.stdin.write(data)
            self._ffmpeg.stdin.flush()
        except BrokenPipeError:
            pass

    def tail_rms_summary(self) -> tuple[int, int]:
        if not self._recent_rms:
            return 0, 0
        vals = list(self._recent_rms)
        return int(sum(vals) / len(vals)), max(vals)

    def close_input(self) -> None:
        if self._closed_input.is_set():
            return
        self._closed_input.set()
        if self._ffmpeg.stdin is not None:
            try:
                self._ffmpeg.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    def wait_done(self, timeout: float = 120.0) -> bool:
        completed = self._done.wait(timeout=timeout)
        if not completed and self.error is None:
            self.error = f"segment {self.index} timed out after {timeout:.0f}s waiting for S3"
            log(f"ERROR: {self.error}")
        return completed

    def _drain_ogg_loop(self) -> None:
        assert self._ffmpeg.stdout is not None
        try:
            while True:
                chunk = self._ffmpeg.stdout.read(4096)
                if not chunk:
                    break
                self.ogg_bytes += len(chunk)
                self._ogg_backup.append(chunk)
                self._ogg_q.put(chunk)
        finally:
            self.flushed_ms = round(time.time() * 1000)
            self._ogg_q.put(None)
            self._encoder_done.set()
            try:
                self._ffmpeg.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self._ffmpeg.kill()
                except OSError:
                    pass

    def _live_chunk_iter(self):
        while True:
            chunk = self._ogg_q.get()
            if chunk is None:
                break
            yield chunk

    def _stream_chunks_once(
        self,
        client: httpx.Client,
        chunks_iter,
        sent_audio_flag: list[bool] | None = None,
        mode: str = "live",
    ) -> str:
        sess = s3.S3Session()
        started_mono = time.monotonic()
        end_request_sent = [False]
        end_req_ms = [-1]
        frames_in = 0
        events_in = 0
        finals_in = 0
        ep0_ms = -1
        ep_end_ms = -1
        first_final_ms = -1

        def gen():
            yield s3._grpc_frame(s3.header_request())
            for chunk in chunks_iter:
                if chunk:
                    if sent_audio_flag is not None:
                        sent_audio_flag[0] = True
                    for i in range(0, len(chunk), 8192):
                        yield s3._grpc_frame(s3.chunk_request(chunk[i : i + 8192]))
            end_request_sent[0] = True
            end_req_ms[0] = round((time.monotonic() - started_mono) * 1000)
            yield s3._grpc_frame(s3.end_request())

        buf: list[bytes] = []
        out: list[bytes] = []
        grpc_status = ""
        grpc_msg = ""
        with client.stream(
            "POST", f"https://{s3.HOST}{s3.PATH}", headers=s3.load_s3_headers(), content=gen()
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"S3Bidi HTTP {resp.status_code}")
            for raw in resp.iter_raw():
                buf.append(raw)
                for msg in sess._split_frames(buf, out):
                    frames_in += 1
                    d = s3.decode_response(msg)
                    if not d:
                        continue
                    events_in += 1
                    rel_ms = round((time.monotonic() - started_mono) * 1000)
                    ep = d.get("endpointer")
                    if ep is not None:
                        if ep == 0 and ep0_ms < 0:
                            ep0_ms = rel_ms
                        elif ep >= 1 and ep_end_ms < 0:
                            ep_end_ms = rel_ms
                    if d.get("errcode"):
                        raise RuntimeError(
                            f"S3 error {d.get('errcode')}: {d.get('errdesc', '')}"
                        )
                    if d.get("partial") and " ".join(d["partial"]) not in sess._partial:
                        sess._partial.append(" ".join(d["partial"]))
                    final_list = d.get("final") or d.get("result")
                    if not final_list:
                        fb = d.get("final_fb") or d.get("result_fb")
                        if fb:
                            final_list = [fb]
                    if final_list:
                        finals_in += 1
                        if first_final_ms < 0:
                            first_final_ms = rel_ms
                        if " ".join(final_list) not in sess._final:
                            sess._final.append(" ".join(final_list))
            trailers = resp.extensions.get("trailers") if hasattr(resp, "extensions") else None
            grpc_status, grpc_msg = extract_grpc_status(resp.headers, trailers)

        text = sess.final_text.strip()
        self.last_call_stats = {
            "mode": mode,
            "endReqMs": end_req_ms[0],
            "ep0Ms": ep0_ms,
            "epEndMs": ep_end_ms,
            "firstFinalMs": first_final_ms,
            "framesIn": frames_in,
            "eventsIn": events_in,
            "finalsIn": finals_in,
            "grpcStatus": grpc_status,
        }
        if grpc_status and grpc_status != "0" and not text:
            raise RuntimeError(
                f"S3Bidi gRPC status {grpc_status}: {grpc_msg}" if grpc_msg else f"S3Bidi gRPC status {grpc_status}"
            )
        if mode == "live" and not end_request_sent[0] and not text:
            raise RuntimeError(
                f"S3Bidi closed response before end_of_data (framesIn={frames_in}, grpcStatus={grpc_status or 'none'})"
            )
        return text

    def _run_s3_loop(self) -> None:
        client = self._pool.get()
        sent_audio = [False]
        try:
            try:
                live_text = self._stream_chunks_once(
                    client, self._live_chunk_iter(), sent_audio, mode="live"
                )
            except Exception as first_exc:  # noqa: BLE001
                if not sent_audio[0] and not self._closed_input.is_set():
                    log(
                        f"Segment {self.index}: reopening stale HTTP/2 connection ({first_exc})"
                    )
                    client = self._pool.reset(client)
                    live_text = self._stream_chunks_once(
                        client, self._live_chunk_iter(), sent_audio, mode="live"
                    )
                else:
                    raise
            if live_text:
                self.text = live_text
            else:
                # Live stream returned empty without raising; if real non-silent audio was
                # captured, retry once with the complete buffered Ogg before accepting empty speech.
                self._encoder_done.wait(timeout=30.0)
                if (
                    self.pcm_bytes > 0
                    and self.max_rms >= (SILENCE_RMS_THRESHOLD / 2.0)
                    and self._ogg_backup
                ):
                    log(
                        f"WARNING: segment {self.index} live S3 returned empty despite audio "
                        f"(max_rms={int(self.max_rms)}, ogg_chunks={len(self._ogg_backup)}); "
                        "retrying buffered audio"
                    )
                    self.used_buffered_retry = True
                    client = self._pool.reset(client)
                    self.text = self._stream_chunks_once(
                        client, list(self._ogg_backup), mode="buffered"
                    )
        except Exception as exc:  # noqa: BLE001
            log(
                f"WARNING: segment {self.index} live S3Bidi failed ({exc}); "
                "draining segment and retrying buffered audio"
            )
            while self._ogg_q.get() is not None:
                pass
            self._encoder_done.wait(timeout=30.0)
            if self.pcm_bytes > 0 and self._ogg_backup:
                self.used_buffered_retry = True
                client = self._pool.reset(client)
                try:
                    self.text = self._stream_chunks_once(
                        client, list(self._ogg_backup), mode="buffered"
                    )
                except Exception as exc2:  # noqa: BLE001
                    self.error = str(exc2)
                    log(f"ERROR: segment {self.index} buffered S3 retry failed: {exc2}")
            else:
                self.error = str(exc)
        finally:
            self.finished_ms = round(time.time() * 1000)
            self._done.set()


class LiveSession:
    """Manages one user recording across 1..N rolling S3Segments + disk WAV safety."""

    def __init__(self, pool: ClientPool) -> None:
        self._pool = pool
        self._stop_event = threading.Event()
        self._proc_lock = threading.Lock()
        self._recorder: subprocess.Popen | None = None
        self._backend = "unknown"
        self._pcm_bytes = 0
        self._first_pcm_event = threading.Event()
        self._started_ms = round(time.time() * 1000)
        self._stop_requested_ms = -1

        ts_name = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
        self._wav_path = ACTIVE_DIR / f"recording-{ts_name}.wav"
        self._wav_file: wave.Wave_write | None = None
        self._segments: list[S3Segment] = []

    def start(self) -> None:
        ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        wf = wave.open(str(self._wav_path), "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        self._wav_file = wf

        first_seg = S3Segment(0, self._pool)
        self._segments.append(first_seg)
        self._launch_initial_recorder()
        threading.Thread(target=self._pump_and_finalize, daemon=True).start()

    def _launch_initial_recorder(self) -> None:
        if shutil.which("pw-record"):
            cmd = ["pw-record"]
            try:
                lat = int(PIPEWIRE_LATENCY_MS)
                if not (0 <= lat <= 1000):
                    lat = 10
            except ValueError:
                lat = 10
            if lat > 0:
                cmd += ["--latency", f"{lat}ms"]
            cmd += ["--format", "s16", "--rate", str(SAMPLE_RATE), "--channels", "1", "--raw", "-"]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            with self._proc_lock:
                self._recorder = proc
                self._backend = "pipewire"
            log(
                f"Started live streaming recording backend=pipewire latency_ms={lat} pid={proc.pid}"
            )
            threading.Thread(
                target=self._monitor_pipewire_startup, args=(proc,), daemon=True
            ).start()
        elif shutil.which("arecord"):
            self._launch_alsa_recorder()
        elif shutil.which("parec"):
            proc = subprocess.Popen(
                [
                    "parec",
                    "--raw",
                    "--format=s16le",
                    f"--rate={SAMPLE_RATE}",
                    "--channels=1",
                    "--device=@DEFAULT_SOURCE@",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            with self._proc_lock:
                self._recorder = proc
                self._backend = "parec"
            log(f"Started live streaming recording backend=parec pid={proc.pid}")
        else:
            raise RuntimeError("No supported recorder found (pw-record, arecord, or parec)")

    def _launch_alsa_recorder(self) -> bool:
        dev = find_direct_alsa_device()
        if not dev:
            log("ERROR: no direct ALSA capture device found")
            return False
        proc = subprocess.Popen(
            [
                "arecord",
                "-D",
                dev,
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                "1",
                "-t",
                "raw",
                "-q",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        with self._proc_lock:
            self._recorder = proc
            self._backend = f"alsa:{dev}"
        log(f"Started live streaming recording backend=alsa:{dev} pid={proc.pid}")
        return True

    def _monitor_pipewire_startup(self, pw_proc: subprocess.Popen) -> None:
        started = time.monotonic()
        if self._first_pcm_event.wait(timeout=PIPEWIRE_HEALTH_DELAY_S):
            elapsed = round((time.monotonic() - started) * 1000)
            log(f"PipeWire live capture health check passed startup_check_ms={elapsed}")
            return
        while not self._stop_event.is_set():
            if self._first_pcm_event.is_set():
                elapsed = round((time.monotonic() - started) * 1000)
                log(f"PipeWire live capture health check passed startup_check_ms={elapsed}")
                return
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if pw_proc.poll() is not None or elapsed_ms >= PIPEWIRE_STARTUP_TIMEOUT_MS:
                break
            time.sleep(0.1)
        if self._stop_event.is_set() or self._first_pcm_event.is_set():
            return
        log(
            f"WARNING: PipeWire live capture health check failed pid={pw_proc.pid} "
            f"after_ms={round((time.monotonic() - started) * 1000)}; falling back to direct ALSA"
        )
        stop_proc(pw_proc, "pipewire")
        if not self._stop_event.is_set() and not self._first_pcm_event.is_set():
            self._launch_alsa_recorder()

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_requested_ms = round(time.time() * 1000)
        self._stop_event.set()
        with self._proc_lock:
            proc = self._recorder
            backend = self._backend
        if proc is not None:
            if TAIL_DRAIN_MS > 0:
                # Drain a short post-Stop audio tail asynchronously so toggle.sh stays ~2 ms
                # while PipeWire/ALSA flushes any trailing samples after Stop is pressed.
                def _delayed_stop() -> None:
                    time.sleep(TAIL_DRAIN_MS / 1000.0)
                    log(f"Stopping live {backend} recorder pid={proc.pid} (tail_drain_ms={TAIL_DRAIN_MS})")
                    stop_proc(proc, backend)

                threading.Thread(target=_delayed_stop, daemon=True).start()
            else:
                log(f"Stopping live {backend} recorder pid={proc.pid}")
                stop_proc(proc, backend)

    def _roll_segment(self, reason: str) -> S3Segment:
        prev = self._segments[-1]
        elapsed_s = round(time.monotonic() - prev.started_monotonic, 1)
        prev.close_input()
        nxt = S3Segment(len(self._segments), self._pool)
        self._segments.append(nxt)
        log(
            f"Rolled live S3 stream from segment {prev.index} ({elapsed_s}s, "
            f"pcm_bytes={prev.pcm_bytes}) to segment {nxt.index} reason={reason}"
        )
        return nxt

    def _close_wav(self) -> None:
        if self._wav_file is not None:
            try:
                self._wav_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._wav_file = None

    def _preserve_failed_wav(self) -> Path | None:
        if not self._wav_path.exists():
            return None
        dest = FAILED_DIR / self._wav_path.name
        try:
            shutil.move(str(self._wav_path), str(dest))
            return dest
        except OSError as exc:
            log(f"WARNING: could not preserve failed WAV {self._wav_path}: {exc}")
            return self._wav_path

    def _retain_last_delivered_wav(self) -> None:
        if not self._wav_path.exists():
            return
        try:
            size = self._wav_path.stat().st_size
            if 0 < size <= MAX_LAST_DELIVERED_BYTES:
                shutil.move(str(self._wav_path), str(LAST_DELIVERED_WAV))
                return
        except OSError as exc:
            log(f"WARNING: could not retain last_delivered.wav ({exc})")
        self._cleanup_wav()

    def _cleanup_wav(self) -> None:
        try:
            self._wav_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _pump_and_finalize(self) -> None:
        cur_seg = self._segments[-1]
        quiet_chunks = 0
        try:
            while True:
                with self._proc_lock:
                    proc = self._recorder
                if proc is None or proc.stdout is None:
                    break
                data = proc.stdout.read(PCM_CHUNK_BYTES)
                if data:
                    self._pcm_bytes += len(data)
                    if not self._first_pcm_event.is_set():
                        self._first_pcm_event.set()
                    if self._wav_file is not None:
                        try:
                            self._wav_file.writeframesraw(data)
                        except Exception:  # noqa: BLE001
                            pass

                    cur_seg.write_pcm(data)

                    seg_age_s = time.monotonic() - cur_seg.started_monotonic
                    if seg_age_s >= SEGMENT_SOFT_S:
                        if pcm_rms(data) < SILENCE_RMS_THRESHOLD:
                            quiet_chunks += 1
                        else:
                            quiet_chunks = 0
                        if quiet_chunks >= SILENCE_PAUSE_CHUNKS:
                            cur_seg = self._roll_segment("silence_pause")
                            quiet_chunks = 0
                        elif seg_age_s >= SEGMENT_HARD_S:
                            cur_seg = self._roll_segment("hard_limit")
                            quiet_chunks = 0
                    continue

                if self._stop_event.is_set():
                    break
                with self._proc_lock:
                    if self._recorder is not proc:
                        continue
                if proc.poll() is not None:
                    time.sleep(0.05)
                    with self._proc_lock:
                        if self._recorder is not proc:
                            continue
                    break
        finally:
            cur_seg.close_input()
            self._close_wav()

        # Wait for all segments (earlier segments usually already finished while speaking)
        for seg in self._segments:
            seg.wait_done(timeout=120.0)

        response_ready_ms = round(time.time() * 1000)
        stop_ms = self._stop_requested_ms
        rec_duration_ms = max(0, stop_ms - self._started_ms) if stop_ms >= 0 else -1
        last_seg = self._segments[-1]
        flush_ms = (
            max(0, last_seg.flushed_ms - stop_ms)
            if (stop_ms >= 0 and last_seg.flushed_ms >= 0)
            else -1
        )
        after_stop_s3_ms = max(0, response_ready_ms - stop_ms) if stop_ms >= 0 else -1
        total_ogg_bytes = sum(s.ogg_bytes for s in self._segments)
        failed_segs = [s for s in self._segments if s.error]
        used_retry = any(s.used_buffered_retry for s in self._segments)

        text = ""
        used_whisper_fallback = False

        if failed_segs:
            log(
                f"WARNING: {len(failed_segs)}/{len(self._segments)} Rambler segment(s) failed "
                f"errors={[s.error for s in failed_segs]}"
            )
            if WHISPER_FALLBACK_ENABLED and self._pcm_bytes > 0 and self._wav_path.exists():
                try:
                    w0 = time.monotonic()
                    text = transcribe_via_whisper(self._wav_path)
                    used_whisper_fallback = True
                    log(
                        f"Recovered failed Rambler session via local Whisper fallback "
                        f"whisper_ms={round((time.monotonic() - w0) * 1000)} text={text!r}"
                    )
                except Exception as wexc:  # noqa: BLE001
                    saved = self._preserve_failed_wav()
                    log(
                        f"ERROR: local Whisper fallback also failed ({wexc}); "
                        f"preserved audio at {saved}"
                    )
                    return
            else:
                saved = self._preserve_failed_wav()
                log(f"ERROR: Rambler session failed; preserved audio at {saved}")
                return
        else:
            parts = [s.text for s in self._segments if s.text]
            text = " ".join(parts).strip()

        self._retain_last_delivered_wav()

        tail_avg_rms, tail_max_rms = last_seg.tail_rms_summary()
        st = last_seg.last_call_stats
        log(
            "Rambler live transcription timing "
            f"recording_duration_ms={rec_duration_ms} "
            f"segments={len(self._segments)} "
            f"stop_to_flush_ms={flush_ms} "
            f"stop_to_transcript_ms={after_stop_s3_ms} "
            f"pcm_bytes={self._pcm_bytes} ogg_bytes={total_ogg_bytes} "
            f"max_rms={int(last_seg.max_rms)} tail_avg_rms={tail_avg_rms} tail_max_rms={tail_max_rms} "
            f"end_req_ms={st.get('endReqMs', -1)} ep0_ms={st.get('ep0Ms', -1)} "
            f"ep_end_ms={st.get('epEndMs', -1)} first_final_ms={st.get('firstFinalMs', -1)} "
            f"frames_in={st.get('framesIn', 0)} finals_in={st.get('finalsIn', 0)} "
            f"grpc_status={st.get('grpcStatus', '')!r} "
            f"retry={used_retry} whisper_fallback={used_whisper_fallback} "
            f"text={text!r}"
        )

        if not text:
            log("Rambler S3 returned an empty transcription")
            return

        try:
            type_ms, inserted, refusal_reason = type_text(text)
            total_ms = (
                max(0, round(time.time() * 1000) - stop_ms) if stop_ms >= 0 else -1
            )
            details = (
                f"total_ms={total_ms} "
                f"segments={len(self._segments)} "
                f"stop_to_transcript_ms={after_stop_s3_ms} "
                f"client_paste_ms={type_ms} inserted={inserted}"
            )
            if refusal_reason:
                details += f" refusal_reason={refusal_reason}"
            log(f"Rambler live delivery timing {details}")
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR: Rambler typing failed: {exc}")


class Daemon:
    def __init__(self) -> None:
        self._pool = ClientPool()
        self._lock = threading.Lock()
        self._active: LiveSession | None = None

    def handle_command(self, cmd: str) -> str:
        cmd = cmd.strip().lower()
        with self._lock:
            if cmd == "status":
                return "recording\n" if self._active is not None else "idle\n"
            if cmd == "stop":
                if self._active is not None:
                    self._active.stop()
                    self._active = None
                    return "stopped\n"
                return "idle\n"
            if cmd == "toggle":
                if self._active is not None:
                    self._active.stop()
                    self._active = None
                    return "stopped\n"
                session = LiveSession(self._pool)
                session.start()
                self._active = session
                return "started\n"
            return "unknown\n"


def main() -> int:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)

    lock_f = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return 0
        raise

    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        SOCK_PATH.unlink()
    except FileNotFoundError:
        pass

    daemon = Daemon()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK_PATH))
    os.chmod(SOCK_PATH, 0o600)
    srv.listen(8)

    shutdown = threading.Event()

    def _on_sig(_signum, _frame) -> None:
        shutdown.set()
        try:
            srv.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    log(
        f"Rambler live-streaming daemon ready pid={os.getpid()} "
        f"soft_s={SEGMENT_SOFT_S} hard_s={SEGMENT_HARD_S} "
        f"whisper_fallback={WHISPER_FALLBACK_ENABLED} sock={SOCK_PATH}"
    )

    try:
        while not shutdown.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            with conn:
                conn.settimeout(2.0)
                try:
                    req = conn.recv(256).decode("utf-8", errors="replace")
                    reply = daemon.handle_command(req)
                    conn.sendall(reply.encode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    log(f"ERROR: control socket command failed: {exc}")
    finally:
        try:
            daemon.handle_command("stop")
        except Exception:  # noqa: BLE001
            pass
        try:
            SOCK_PATH.unlink()
        except OSError:
            pass
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        lock_f.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
