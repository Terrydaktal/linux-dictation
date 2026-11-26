import importlib.util
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

DAEMON_PATH = Path(__file__).resolve().parent.parent / "rambler" / "stream-daemon.py"
SPEC = importlib.util.spec_from_file_location("rambler_stream_daemon", DAEMON_PATH)
assert SPEC and SPEC.loader
daemon_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daemon_mod)


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, trailers=None, raw_chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self.extensions = {"trailers": trailers} if trailers is not None else {}
        self._raw_chunks = list(raw_chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def iter_raw(self):
        yield from self._raw_chunks


class _FakeClient:
    def __init__(self, response, consume_content=True):
        self._response = response
        self._consume_content = consume_content

    def stream(self, _method, _url, headers=None, content=None):
        if self._consume_content and content is not None:
            for _ in content:
                pass
        return self._response


def _make_stub_segment():
    seg = object.__new__(daemon_mod.S3Segment)
    seg.index = 0
    seg._pool = mock.MagicMock()
    seg.started_monotonic = 0.0
    seg.pcm_bytes = 0
    seg.ogg_bytes = 0
    seg.max_rms = 0.0
    seg._recent_rms = daemon_mod.deque(maxlen=daemon_mod.TAIL_RMS_WINDOW_CHUNKS)
    seg.flushed_ms = -1
    seg.finished_ms = -1
    seg.text = ""
    seg.error = None
    seg.used_buffered_retry = False
    seg.last_call_stats = {}
    seg._ogg_q = queue.Queue()
    seg._ogg_backup = []
    seg._closed_input = threading.Event()
    seg._encoder_done = threading.Event()
    seg._done = threading.Event()
    return seg


class RamblerStreamDaemonTests(unittest.TestCase):
    def test_extract_grpc_status_prefers_trailers_and_handles_bytes(self):
        status, msg = daemon_mod.extract_grpc_status(
            headers={"content-type": "application/grpc"},
            trailers=[(b"grpc-status", b"4"), (b"grpc-message", b"DEADLINE_EXCEEDED")],
        )
        self.assertEqual(status, "4")
        self.assertEqual(msg, "DEADLINE_EXCEEDED")

    def test_nonzero_grpc_trailer_raises_instead_of_empty_speech(self):
        seg = _make_stub_segment()
        resp = _FakeResponse(
            status_code=200,
            trailers=[(b"grpc-status", b"13"), (b"grpc-message", b"INTERNAL")],
            raw_chunks=[],
        )
        client = _FakeClient(resp, consume_content=True)
        with self.assertRaisesRegex(RuntimeError, "gRPC status 13: INTERNAL"):
            seg._stream_chunks_once(client, iter([b"ogg1"]), mode="live")

    def test_premature_live_eof_before_end_request_raises(self):
        seg = _make_stub_segment()
        resp = _FakeResponse(status_code=200, raw_chunks=[])
        # Simulate server half-closing without consuming the generator through end_request()
        client = _FakeClient(resp, consume_content=False)
        with self.assertRaisesRegex(RuntimeError, "before end_of_data"):
            seg._stream_chunks_once(client, iter([b"ogg1"]), mode="live")

    def test_wait_done_sets_timeout_error_when_latch_expires(self):
        seg = _make_stub_segment()
        completed = seg.wait_done(timeout=0.01)
        self.assertFalse(completed)
        self.assertIsNotNone(seg.error)
        self.assertIn("timed out", seg.error)

    def test_empty_live_response_with_speech_retries_buffered(self):
        seg = _make_stub_segment()
        seg.pcm_bytes = 64000
        seg.max_rms = 1800.0
        seg._ogg_backup = [b"OggS-hdr", b"OggS-tags", b"OggS-audio"]
        seg._encoder_done.set()
        seg._pool.get.return_value = mock.MagicMock()
        seg._pool.reset.return_value = mock.MagicMock()

        calls = []

        def fake_stream(_client, _chunks, _sent_flag=None, mode="live"):
            calls.append(mode)
            return "" if mode == "live" else "recovered final sentence"

        seg._stream_chunks_once = fake_stream
        seg._run_s3_loop()

        self.assertEqual(calls, ["live", "buffered"])
        self.assertTrue(seg.used_buffered_retry)
        self.assertEqual(seg.text, "recovered final sentence")
        self.assertIsNone(seg.error)

    def test_retain_last_delivered_wav_overwrites_rolling_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "last_delivered.wav"
            wav1 = root / "rec1.wav"
            wav2 = root / "rec2.wav"
            wav1.write_bytes(b"A" * 128)
            wav2.write_bytes(b"B" * 256)

            with mock.patch.object(daemon_mod, "LAST_DELIVERED_WAV", target):
                sess = object.__new__(daemon_mod.LiveSession)
                sess._wav_path = wav1
                sess._retain_last_delivered_wav()
                self.assertFalse(wav1.exists())
                self.assertEqual(target.read_bytes(), b"A" * 128)

                sess._wav_path = wav2
                sess._retain_last_delivered_wav()
                self.assertFalse(wav2.exists())
                self.assertEqual(target.read_bytes(), b"B" * 256)

    def test_s3_load_headers_from_env_file_and_missing_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "rambler.env"
            env_path.write_text(
                "# comment\n"
                "RAMBLER_S3_API_KEY='unit-test-key'\n"
                "RAMBLER_S3_ANDROID_CERT=UNITTESTCERT\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                headers = daemon_mod.s3.load_s3_headers(env_file=env_path)
                self.assertEqual(headers["x-goog-api-key"], "unit-test-key")
                self.assertEqual(headers["x-android-cert"], "UNITTESTCERT")
                self.assertEqual(
                    headers["x-android-package"],
                    "com.google.android.googlequicksearchbox",
                )

                with self.assertRaises(RuntimeError):
                    daemon_mod.s3.load_s3_headers(env_file=Path(tmp) / "missing.env")

            with mock.patch.dict(
                os.environ,
                {"RAMBLER_S3_API_KEY": "env-override-key"},
                clear=True,
            ):
                headers = daemon_mod.s3.load_s3_headers(env_file=env_path)
                self.assertEqual(headers["x-goog-api-key"], "env-override-key")
                self.assertEqual(headers["x-android-cert"], "UNITTESTCERT")


if __name__ == "__main__":
    unittest.main()
