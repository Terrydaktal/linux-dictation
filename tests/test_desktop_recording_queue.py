import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Fixtures never open audio devices, contact a server, or invoke a typing tool.
RECORDER = r"""
import json
import os
import signal
import sys
import time
from pathlib import Path

def stop(*_):
    if os.environ.get('FAKE_FLUSH_ON_STOP'):
        with open(sys.argv[-1], 'ab', buffering=0) as output:
            output.write(b'\x00' * 1600)
    if os.environ.get('FAKE_HOLD_STOP'):
        root = Path(os.environ['FAKE_ROOT'])
        (root / 'recorder-stopping').touch()
        while not (root / 'allow-stop').exists():
            time.sleep(0.01)
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if '-l' in sys.argv:
    if os.environ.get('FAKE_ALSA_LIST_FAIL'):
        print('fixture ALSA device enumeration failed', file=sys.stderr)
        sys.exit(1)
    print(os.environ.get('FAKE_ALSA_LIST', 'card 3: Microphone [Yeti Stereo Microphone], device 0: USB Audio [USB Audio]'))
    sys.exit(0)
with (Path(os.environ['FAKE_ROOT']) / 'recorder-commands').open('a') as commands:
    commands.write(json.dumps(sys.argv) + '\n')
empty = Path(sys.argv[0]).name == 'pw-record' and os.environ.get('FAKE_EMPTY_PIPEWIRE')
with open(sys.argv[-1], 'wb', buffering=0) as output:
    output.write(b'RIFF' + b'\x00' * 40)
    if Path(sys.argv[0]).name == 'pw-record':
        time.sleep(float(os.environ.get('FAKE_PIPEWIRE_START_DELAY', '0')))
    while True:
        if not empty:
            output.write(b'\x00' * 1600)
        time.sleep(0.02)
"""

CLIENT = r"""
import os
import sys
from pathlib import Path

root = Path(os.environ['FAKE_ROOT'])
audio = Path(sys.argv[-1])
with (root / 'attempts').open('a') as output:
    output.write(audio.name + '\n')
if not (root / 'server-ready').exists():
    sys.exit(1)
with (root / 'served').open('a') as output:
    output.write(audio.name + '\n')
"""


@unittest.skipUnless(sys.platform == "linux" and shutil.which("flock"), "Linux flock required")
class DesktopRecordingQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="assistant-queue-test-")
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.runtime = self.root / "runtime" / "assistant-desktop-dictation"
        self.queue = self.root / "state" / "assistant-desktop-dictation" / "queue"
        self.runtime.mkdir(parents=True)
        self.queue.mkdir(parents=True)
        self.processes = []
        self.output = (self.root / "process.log").open("wb")
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "FAKE_ROOT": str(self.root),
            "ASSISTANT_QUEUE_RETRY_INITIAL_S": "30",
            "ASSISTANT_QUEUE_RETRY_MAX_S": "30",
            "ASSISTANT_PIPEWIRE_HEALTH_DELAY_S": "0.1",
            "ASSISTANT_PIPEWIRE_STARTUP_TIMEOUT_MS": "700",
            "ASSISTANT_ALSA_DEVICE": "plughw:fake,0",
        }
        self.env.pop("FAKE_EMPTY_PIPEWIRE", None)
        self.env.pop("FAKE_HOLD_STOP", None)
        self.env.pop("FAKE_PIPEWIRE_START_DELAY", None)
        self.env.pop("FAKE_ALSA_LIST_FAIL", None)
        self.env.pop("FAKE_ALSA_LIST", None)
        self.env.pop("FAKE_FLUSH_ON_STOP", None)
        self.env.pop("ASSISTANT_PIPEWIRE_LATENCY_MS", None)
        for name, program in (("pw-record", RECORDER), ("arecord", RECORDER), ("uv", CLIENT)):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + program)
            path.chmod(0o755)

    def tearDown(self):
        # Every child belongs to a new session created by this test, including
        # nohup workers and recorders which outlive their toggle parent.
        for proc in self.processes:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for proc in self.processes:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)
        self.output.close()
        self.temp.cleanup()

    def launch(self, script):
        proc = subprocess.Popen(
            [str(ROOT / "whisper" / script)],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=self.output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(proc)
        return proc

    def toggle(self):
        proc = self.launch("toggle.sh")
        self.assertEqual(proc.wait(timeout=2), 0)

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for isolated fixture state")

    def lines(self, name):
        path = self.root / name
        return path.read_text().splitlines() if path.exists() else []

    def start_recording(self):
        self.toggle()
        path = Path((self.runtime / "current-audio-path").read_text().strip())
        self.wait_for(lambda: path.exists() and path.stat().st_size > 44)
        return path

    def assert_action_lock_free(self):
        with (self.runtime / "recorder-action.lock").open("a") as lock:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    time.sleep(0.01)
            self.fail("Background process inherited the recorder action lock")

    def wait_for_retry(self):
        logs = (self.queue.parent / "dictation.log", self.root / "process.log")
        self.wait_for(
            lambda: any(log.exists() and "retrying in 30s" in log.read_text() for log in logs)
        )

    def add_completed(self, name):
        path = self.queue / name
        path.write_bytes(b"RIFF" + b"\x00" * 160)
        path.with_suffix(".json").write_text("{}")
        return path

    def test_pipewire_requests_low_latency_without_changing_audio_format(self):
        audio = self.start_recording()
        command = json.loads(self.lines("recorder-commands")[0])
        self.assertEqual(Path(command[0]).name, "pw-record")
        self.assertEqual(command[1:], [
            "--latency", "10ms", "--container", "wav", "--format", "s16",
            "--rate", "16000", "--channels", "1", str(audio),
        ])
        self.toggle()
        self.assertTrue(audio.exists())

    def test_pipewire_latency_can_be_increased_or_left_automatic(self):
        for value in ("20", "0"):
            with self.subTest(value=value):
                self.env["ASSISTANT_PIPEWIRE_LATENCY_MS"] = value
                self.start_recording()
                command = json.loads(self.lines("recorder-commands")[-1])
                if value == "0":
                    self.assertNotIn("--latency", command)
                else:
                    self.assertEqual(command[command.index("--latency") + 1], "20ms")
                self.toggle()

    def test_invalid_pipewire_latency_does_not_break_recording_or_stop(self):
        for value in ("invalid", "-1", "1001", "08"):
            with self.subTest(value=value):
                self.env["ASSISTANT_PIPEWIRE_LATENCY_MS"] = value
                audio = self.start_recording()
                command = json.loads(self.lines("recorder-commands")[-1])
                self.assertEqual(command[command.index("--latency") + 1], "10ms")
                self.assertIn(
                    f"invalid ASSISTANT_PIPEWIRE_LATENCY_MS='{value}'",
                    (self.queue.parent / "dictation.log").read_text(),
                )
                self.toggle()
                self.assertTrue(audio.with_suffix(".json").exists())
                self.assertFalse((self.runtime / "recorder.pid").exists())

    def test_offline_worker_does_not_inherit_recording_lock(self):
        audio = self.start_recording()
        self.toggle()
        self.wait_for(lambda: audio.name in self.lines("attempts"))
        self.assertTrue(audio.exists())
        self.assertTrue(audio.with_suffix(".json").exists())
        self.assert_action_lock_free()

    def test_next_recording_stops_while_backlog_is_offline(self):
        first = self.start_recording()
        self.toggle()
        self.wait_for_retry()
        second = self.start_recording()
        self.toggle()
        self.assertFalse((self.runtime / "recorder.pid").exists())
        self.assertFalse((self.runtime / "current-audio-path").exists())
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(second.with_suffix(".json").exists())

    def test_direct_alsa_fallback_does_not_inherit_action_lock(self):
        self.env["FAKE_EMPTY_PIPEWIRE"] = "1"
        self.toggle()
        backend = self.runtime / "recorder-backend"
        self.wait_for(lambda: backend.exists() and backend.read_text().startswith("alsa:"))
        self.assert_action_lock_free()
        audio = Path((self.runtime / "current-audio-path").read_text().strip())
        self.wait_for(lambda: audio.stat().st_size > 44)
        self.toggle()
        self.assertFalse((self.runtime / "recorder.pid").exists())

    def test_slow_healthy_pipewire_is_not_killed_at_the_first_header_only_check(self):
        self.env["FAKE_PIPEWIRE_START_DELAY"] = "0.35"
        audio = self.start_recording()
        backend = self.runtime / "recorder-backend"
        self.assertEqual(backend.read_text().strip(), "pipewire")
        log = self.queue.parent / "dictation.log"
        self.wait_for(lambda: "health check passed" in log.read_text())
        self.assertIn("PipeWire still starting", log.read_text())
        self.assertNotIn("Stopping PipeWire", log.read_text())
        self.toggle()
        self.assertTrue(audio.exists())
        self.assertTrue(audio.with_suffix(".json").exists())

    def test_stop_during_startup_grace_does_not_wait_or_restart_recording(self):
        self.env["FAKE_PIPEWIRE_START_DELAY"] = "0.5"
        self.toggle()
        log = self.queue.parent / "dictation.log"
        self.wait_for(lambda: "PipeWire still starting" in log.read_text())
        started = time.monotonic()
        self.toggle()
        self.assertLess(time.monotonic() - started, 0.5)
        time.sleep(0.75)
        self.assertFalse((self.runtime / "recorder.pid").exists())
        self.assertFalse((self.runtime / "current-audio-path").exists())
        self.assertNotIn("Using direct ALSA fallback", log.read_text())
        self.assertEqual(self.lines("attempts"), [])

    def test_audio_arriving_while_fallback_waits_for_lock_is_not_overwritten(self):
        self.env["ASSISTANT_PIPEWIRE_STARTUP_TIMEOUT_MS"] = "150"
        self.env["FAKE_PIPEWIRE_START_DELAY"] = "0.4"
        with (self.runtime / "recorder-action.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.toggle()
            audio = Path((self.runtime / "current-audio-path").read_text().strip())
            log = self.queue.parent / "dictation.log"
            self.wait_for(lambda: "trying direct ALSA" in log.read_text())
            self.wait_for(lambda: audio.stat().st_size > 44)
        self.wait_for(lambda: "produced audio before fallback" in log.read_text())
        self.assertEqual((self.runtime / "recorder-backend").read_text().strip(), "pipewire")
        self.assertNotIn("Stopping PipeWire", log.read_text())
        self.toggle()
        self.assertTrue(audio.exists())

    def test_alsa_device_is_discovered_without_a_manual_override(self):
        self.env["FAKE_EMPTY_PIPEWIRE"] = "1"
        self.env.pop("ASSISTANT_ALSA_DEVICE")
        audio = self.start_recording()
        self.assertEqual((self.runtime / "recorder-backend").read_text().strip(), "alsa:plughw:3,0")
        self.toggle()
        self.assertTrue(audio.exists())

    def test_audio_flushed_during_fallback_stop_is_not_overwritten(self):
        self.env["FAKE_EMPTY_PIPEWIRE"] = "1"
        self.env["FAKE_FLUSH_ON_STOP"] = "1"
        self.toggle()
        audio = Path((self.runtime / "current-audio-path").read_text().strip())
        log = self.queue.parent / "dictation.log"
        self.wait_for(lambda: "preserving it instead of overwriting" in log.read_text())
        self.assertGreater(audio.stat().st_size, 44)
        self.assertEqual((self.runtime / "recorder-backend").read_text().strip(), "pipewire")
        self.toggle()
        self.assertTrue(audio.exists())
        self.assertTrue(audio.with_suffix(".json").exists())

    def test_alsa_fallback_can_select_a_non_yeti_microphone(self):
        self.env["FAKE_EMPTY_PIPEWIRE"] = "1"
        self.env["FAKE_ALSA_LIST"] = "card 2: Generic [HD Audio], device 2: Analog [Analog]"
        self.env.pop("ASSISTANT_ALSA_DEVICE")
        audio = self.start_recording()
        self.assertEqual((self.runtime / "recorder-backend").read_text().strip(), "alsa:plughw:2,2")
        self.toggle()
        self.assertTrue(audio.exists())

    def test_alsa_lookup_errors_are_logged_instead_of_silently_suppressed(self):
        self.env["FAKE_EMPTY_PIPEWIRE"] = "1"
        self.env["FAKE_ALSA_LIST_FAIL"] = "1"
        self.env.pop("ASSISTANT_ALSA_DEVICE")
        self.toggle()
        log = self.queue.parent / "dictation.log"
        self.wait_for(lambda: "fallback could not start" in log.read_text())
        self.assertIn("lookup failed status=1", log.read_text())
        self.assertIn("fixture ALSA device enumeration failed", log.read_text())
        self.toggle()
        self.assertEqual(self.lines("attempts"), [])

    def test_wakeup_interrupts_thirty_second_backoff_and_drains_fifo(self):
        first = self.add_completed("recording-001.wav")
        second = self.add_completed("recording-002.wav")
        self.launch("queue-worker.sh")
        self.wait_for_retry()
        self.assertTrue(first.exists())
        self.assertTrue(first.with_suffix(".json").exists())
        (self.root / "server-ready").touch()
        wakeup = self.launch("queue-worker.sh")
        self.assertEqual(wakeup.wait(timeout=2), 0)
        self.wait_for(lambda: not first.exists() and not second.exists())
        self.assertEqual(self.lines("served"), [first.name, second.name])
        self.assertFalse(first.with_suffix(".json").exists())
        self.assertFalse(second.with_suffix(".json").exists())

    def test_background_wakeup_resumes_existing_retry(self):
        audio = self.add_completed("recording-001.wav")
        self.launch("queue-worker.sh")
        self.wait_for_retry()
        (self.root / "server-ready").touch()
        wakeup = subprocess.run(
            [str(ROOT / "whisper" / "queue-worker.sh"), "--background"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=True,
        )
        self.assertEqual(wakeup.returncode, 0)
        self.wait_for(lambda: not audio.exists())
        self.assertEqual(self.lines("served"), [audio.name])

    def test_rapid_recordings_are_all_served_once_in_order(self):
        (self.root / "server-ready").touch()
        recordings = []
        for _ in range(8):
            audio = self.start_recording()
            recordings.append(audio)
            self.toggle()
        self.wait_for(lambda: all(not audio.exists() for audio in recordings))
        self.assertEqual(self.lines("served"), [audio.name for audio in recordings])

    def test_recording_is_not_published_until_stop_finalization_completes(self):
        self.env["FAKE_HOLD_STOP"] = "1"
        audio = self.start_recording()
        stopping = self.launch("toggle.sh")
        self.wait_for(lambda: (self.root / "recorder-stopping").exists())
        (self.root / "server-ready").touch()
        wakeup = self.launch("queue-worker.sh")
        self.assertEqual(wakeup.wait(timeout=2), 0)
        time.sleep(0.1)
        self.assertEqual(self.lines("served"), [])
        self.assertTrue(audio.exists())
        (self.root / "allow-stop").touch()
        self.assertEqual(stopping.wait(timeout=2), 0)
        self.wait_for(lambda: not audio.exists())
        self.assertEqual(self.lines("served"), [audio.name])

    def test_idle_worker_can_be_woken_for_a_new_completed_recording(self):
        (self.root / "server-ready").touch()
        self.launch("queue-worker.sh")
        first = self.add_completed("recording-001.wav")
        self.launch("queue-worker.sh")
        self.wait_for(lambda: not first.exists())
        second = self.add_completed("recording-002.wav")
        self.launch("queue-worker.sh")
        self.wait_for(lambda: not second.exists())
        self.assertEqual(self.lines("served"), [first.name, second.name])

    def test_worker_does_not_upload_current_recording(self):
        audio = self.add_completed("recording-001.wav")
        marker = self.runtime / "current-audio-path"
        marker.write_text(str(audio))
        (self.root / "server-ready").touch()
        self.launch("queue-worker.sh")
        time.sleep(0.1)
        self.assertTrue(audio.exists())
        self.assertEqual(self.lines("served"), [])
        marker.unlink()
        self.launch("queue-worker.sh")
        self.wait_for(lambda: not audio.exists())
        self.assertEqual(self.lines("served"), [audio.name])


if __name__ == "__main__":
    unittest.main()
