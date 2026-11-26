"""Gboard S3 speech backend client (the exact backend PixelBoard's Rambler uses).

Reverse-engineered from the PixelBoard 1.3.6 APK (Gboard 18.4.1 base):
- gRPC over HTTP/2 to speechs3proto2-pa.googleapis.com:443
- method speech.s3.S3StubbyClientConnectorService/S3Bidi (bidirectional streaming)
- credentials loaded locally from environment variables or
  ~/.config/linux-dictation/rambler.env (RAMBLER_S3_API_KEY,
  RAMBLER_S3_ANDROID_CERT, optional RAMBLER_S3_ANDROID_PACKAGE)
- audio: Ogg/Opus 16 kHz mono, chunked into S3Request extension 293101
- personalization (Zwieback auth token) is optional and omitted here

The stream mirrors what Gboard does: one session-start request, then audio
chunks while speaking, then an end_of_data request; the server streams
RecognizerEvent responses (partials, final hypothesis, endpointer events).
"""
from __future__ import annotations

import os
from pathlib import Path
import struct
import threading
import time
from typing import Callable, Iterable

import httpx

HOST = "speechs3proto2-pa.googleapis.com"
PATH = "/speech.s3.S3StubbyClientConnectorService/S3Bidi"
DEFAULT_ANDROID_PACKAGE = "com.google.android.googlequicksearchbox"


def default_env_file() -> Path:
    explicit = os.environ.get("RAMBLER_S3_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base / "linux-dictation" / "rambler.env"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def load_s3_headers(env_file: Path | None = None) -> dict[str, str]:
    """Build gRPC request headers from environment variables or local rambler.env."""
    cfg_path = env_file if env_file is not None else default_env_file()
    file_vals = _read_env_file(cfg_path)

    def _get_val(key: str, default: str = "") -> str:
        env_v = os.environ.get(key)
        if env_v is not None and env_v.strip():
            return env_v.strip()
        file_v = file_vals.get(key, "")
        if file_v.strip():
            return file_v.strip()
        return default

    api_key = _get_val("RAMBLER_S3_API_KEY")
    android_cert = _get_val("RAMBLER_S3_ANDROID_CERT")
    android_pkg = _get_val("RAMBLER_S3_ANDROID_PACKAGE", DEFAULT_ANDROID_PACKAGE)
    if not api_key or not android_cert:
        raise RuntimeError(
            "Rambler S3 credentials not configured. Set RAMBLER_S3_API_KEY and "
            f"RAMBLER_S3_ANDROID_CERT in your environment or in {cfg_path}"
        )
    return {
        "content-type": "application/grpc",
        "te": "trailers",
        "x-goog-api-key": api_key,
        "x-android-package": android_pkg,
        "x-android-cert": android_cert,
    }

# ---------------------------------------------------------------- protobuf

def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def _tag(f: int, w: int) -> bytes:
    return _varint((f << 3) | w)


def _f_string(f: int, s: str) -> bytes:
    b = s.encode()
    return _tag(f, 2) + _varint(len(b)) + b


def _f_bytes(f: int, b: bytes) -> bytes:
    return _tag(f, 2) + _varint(len(b)) + b


def _f_bool(f: int, v: bool) -> bytes:
    return _tag(f, 0) + _varint(1 if v else 0)


def _f_int32(f: int, v: int) -> bytes:
    return _tag(f, 0) + _varint(v & 0xFFFFFFFF)


def _f_int64(f: int, v: int) -> bytes:
    return _tag(f, 0) + _varint(v & 0xFFFFFFFFFFFFFFFF)


def _f_float(f: int, v: float) -> bytes:
    return _tag(f, 5) + struct.pack("<f", v)


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    v = 0
    shift = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << shift
        if not x & 0x80:
            return v, i
        shift += 7


def _parse_fields(b: bytes):
    i = 0
    while i < len(b):
        t, i = _read_varint(b, i)
        f, w = t >> 3, t & 7
        if w == 0:
            v, i = _read_varint(b, i)
        elif w == 2:
            ln, i = _read_varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif w == 5:
            v = b[i:i + 4]
            i += 4
        elif w == 1:
            v = b[i:i + 8]
            i += 8
        else:
            raise ValueError(f"wire {w}")
        yield f, w, v


def _get(b: bytes | None, f: int):
    if b is None:
        return None
    for ff, _w, v in _parse_fields(b):
        if ff == f:
            return v
    return None


# S3Request field numbers (from the decompiled PixelBoard 1.3.6 APK)
EXT_INPUT_BOX_INFO = 1203
EXT_AUDIO_INFO, EXT_AUDIO_DATA, EXT_USER_INFO = 293100, 293101, 293000
EXT_CLIENT_INFO, EXT_RECOG_INFO, EXT_HEARTBEAT = 294000, 294500, 29735924
OGG_OPUS = 10
SAMPLE_RATE = 16000

DEFAULT_SERVICE = os.environ.get("RAMBLER_S3_SERVICE", "beyond-a2a-recognizer")
DEFAULT_ROUTING_KEY = os.environ.get(
    "RAMBLER_S3_ROUTING_KEY", "gboard_gemini_v3_streaming_voice_edit_mul"
)
DEFAULT_LOCALE = os.environ.get("RAMBLER_LOCALE", "en-GB")
DEFAULT_PROFANITY_FILTER = int(os.environ.get("RAMBLER_PROFANITY_FILTER", "0"))


def _audio_info(ts_ms: int | None = None) -> bytes:
    m = _f_float(2, float(SAMPLE_RATE)) + _f_int32(3, OGG_OPUS) + _f_int32(4, 1)
    if ts_ms is not None:
        m += _f_int64(9, ts_ms)
    return _f_bytes(EXT_AUDIO_INFO, m)


def header_request(
    service: str = DEFAULT_SERVICE,
    locale: str = DEFAULT_LOCALE,
    routing_key: str = DEFAULT_ROUTING_KEY,
    profanity_filter: int = DEFAULT_PROFANITY_FILTER,
) -> bytes:
    rec_info = (
        _f_bytes(1, _f_bool(4, False))
        + _f_bool(5, True)
        + _f_int32(7, profanity_filter)  # 0 = off (no asterisks), 2 = mask offensive words
        + _f_bool(13, True)
        + _f_bool(20, True)
        + _f_bool(40, True)
        + _f_bool(41, True)
    )
    if routing_key:
        rec_info += _f_string(50, routing_key)
    if service == "beyond-a2a-recognizer":
        # Laapa (#91): #2 = AdultAccount, #4 = required enable flag for beyond-a2a-recognizer
        rec_info += _f_bytes(91, _f_bool(2, True) + _f_bool(4, True))

    user_info = _f_bytes(2, _f_string(1, locale) + _f_int32(2, 1)) + _f_bool(20, True)

    hdr = (
        _f_string(1, service)
        + _f_bool(2, True)
        + _audio_info(ts_ms=0)
        + _f_bytes(EXT_USER_INFO, user_info)
        + _f_bytes(
            EXT_CLIENT_INFO,
            _f_string(2, "gboard") + _f_string(8, "Android") + _f_string(9, "16")
            + _f_string(10, "-1") + _f_string(11, "NX809J")
            + _f_bytes(22, _f_bool(1, True)) + _f_int32(26, 1),
        )
        + _f_bytes(EXT_RECOG_INFO, rec_info)
        + _f_bytes(EXT_HEARTBEAT, _f_int32(1, 500))
    )
    if service == "beyond-a2a-recognizer":
        hdr += _f_bytes(EXT_INPUT_BOX_INFO, _f_string(1, ""))
    return hdr


def chunk_request(payload: bytes, ts_ms: int | None = None) -> bytes:
    m = _f_bytes(EXT_AUDIO_DATA, _f_bytes(1, payload))
    if ts_ms is not None:
        m += _f_bytes(EXT_AUDIO_INFO, _f_int64(9, ts_ms))
    return m


def end_request() -> bytes:
    return _f_bool(2, True) + _f_bool(3, True)


# ---------------------------------------------------------------- decoding

def _hyp_texts(h: bytes | None) -> list[str]:
    if not isinstance(h, bytes):
        return []
    return [v.decode(errors="replace") for ff, _w, v in _parse_fields(h) if ff == 1]


def decode_response(raw: bytes) -> dict:
    out: dict = {}
    if _get(raw, 1) is not None:
        out["status"] = _get(raw, 1)
    if _get(raw, 2) is not None:
        out["errcode"] = _get(raw, 2)
    if _get(raw, 3) is not None:
        out["errdesc"] = _get(raw, 3)
    ev = _get(raw, 1253625)
    if ev is None:
        return out
    lang = _get(ev, 3)
    if isinstance(lang, bytes):
        out["lang"] = lang.decode()
    sl = _get(ev, 4)
    if isinstance(sl, bytes):
        out["spokenlang"] = sl.decode()
    ep = _get(ev, 2)
    if isinstance(ep, bytes) and _get(ep, 1) is not None:
        out["endpointer"] = _get(ep, 1)  # 0 speech started, 1 speech ended, 2 end/timeout
    rec = _get(ev, 1)
    if rec is None:
        return out
    if _get(rec, 1) is not None:
        out["rec_status"] = _get(rec, 1)  # 0 in progress, 1 final
    for fn, key in ((3, "final"), (4, "partial"), (5, "alt"), (7, "result")):
        r = _get(rec, fn)
        if not isinstance(r, bytes):
            continue
        texts = _hyp_texts(_get(r, 3))
        if texts:
            out[key] = texts
        p = _get(r, 5)
        if isinstance(p, bytes):
            t = _get(_get(p, 1), 1)
            if isinstance(t, bytes):
                out[key + "_fb"] = t.decode()
        if key == "partial":
            parts = [
                _get(v, 1).decode(errors="replace")
                for _ff, _w, v in _parse_fields(r)
                if _ff == 1 and isinstance(v, bytes) and _get(v, 1)
            ]
            if parts:
                out["partial_parts"] = parts
    return out


# ---------------------------------------------------------------- session

class S3Session:
    """One dictation session over S3Bidi.

    run() blocks for the whole session: the audio iterator must stay alive
    for the duration of the recording (httpx consumes the request body
    lazily while the response is read in parallel).

    Usage:
        s = S3Session()
        text = s.run(live_ogg_chunks(), on_event=callback)
    """

    def __init__(self, timeout_s: float = 60.0):
        self.timeout_s = timeout_s
        self._partial: list[str] = []
        self._final: list[str] = []
        self._events: list[dict] = []
        self._error: str | None = None

    def run(
        self,
        audio_chunks: "Iterable[bytes]",
        on_event: Callable[[dict], None] | None = None,
    ) -> str:
        """audio_chunks: an iterator yielding ogg/opus byte chunks (live or pre-recorded)."""
        client = httpx.Client(http2=True, timeout=httpx.Timeout(90, read=90))
        responses: list[bytes] = []
        done = threading.Event()
        frames_q: list[bytes] = []

        def gen():
            yield _grpc_frame(header_request())
            for chunk in audio_chunks:
                if chunk:
                    for i in range(0, len(chunk), 8192):
                        yield _grpc_frame(chunk_request(chunk[i:i + 8192]))
            yield _grpc_frame(end_request())

        def reader(r):
            try:
                for b in r.iter_raw():
                    responses.append(b)
                    for m in self._split_frames(responses, frames_q):
                        d = decode_response(m)
                        if not d:
                            continue
                        self._events.append(d)
                        if d.get("partial") and " ".join(d["partial"]) not in self._partial:
                            self._partial.append(" ".join(d["partial"]))
                        final_list = d.get("final") or d.get("result")
                        if not final_list:
                            fb = d.get("final_fb") or d.get("result_fb")
                            if fb:
                                final_list = [fb]
                        if final_list and " ".join(final_list) not in self._final:
                            self._final.append(" ".join(final_list))
                        if on_event:
                            on_event(d)
            except Exception as e:  # noqa: BLE001
                self._error = f"{type(e).__name__}: {e}"
            finally:
                done.set()

        with client.stream(
            "POST", f"https://{HOST}{PATH}", headers=load_s3_headers(), content=gen()
        ) as r:
            t = threading.Thread(target=reader, args=(r,), daemon=True)
            t.start()
            t.join(self.timeout_s)
        if self._error:
            raise RuntimeError(self._error)
        return self.final_text

    def _split_frames(self, responses: list[bytes], out: list[bytes]) -> list[bytes]:
        data = b"".join(responses)
        responses.clear()
        msgs = out
        out.clear()
        i = 0
        while i + 5 <= len(data):
            ln = int.from_bytes(data[i + 1:i + 5], "big")
            if i + 5 + ln > len(data):
                break
            msgs.append(data[i + 5:i + 5 + ln])
            i += 5 + ln
        if i < len(data):
            responses.append(data[i:])
        return msgs

    @property
    def final_text(self) -> str:
        if self._final:
            return " ".join(self._final)
        if self._partial:
            return " ".join(self._partial)
        return ""


def _grpc_frame(payload: bytes) -> bytes:
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def transcribe_ogg(ogg_path: str, on_event: Callable[[dict], None] | None = None) -> str:
    """Convenience: transcribe a finished .ogg (16 kHz mono opus) file."""
    with open(ogg_path, "rb") as f:
        data = f.read()

    def gen():
        for i in range(0, len(data), 8192):
            yield data[i:i + 8192]

    return S3Session().run(gen(), on_event)
