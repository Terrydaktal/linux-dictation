import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


HELPER = Path(__file__).resolve().parents[1] / "wayland-type-helper.sh"
CLEAR_MODIFIERS = "META=0\nSHIFT=0\nCTRL=0\nALT=0\n"
HELD_MODIFIERS = "META=0\nSHIFT=0\nCTRL=1\nALT=0\n"

# This executable records requests only; it never connects to an input device.
FAKE_YDOTOOL = r"""
import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ['FAKE_ROOT'])
log = root / 'commands.jsonl'
args = sys.argv[1:]
text = sys.stdin.buffer.read().decode() if args[0] == 'type' else None
previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
phase = 1 + sum(item['args'][0] == 'type' for item in previous)
with log.open('a') as output:
    output.write(json.dumps({'args': args, 'text': text}) + '\n')
if (args[0] == 'key' and len(args) > 4 and os.environ.get('FAKE_MODIFIER_AFTER_RELEASE')) or (
    args[0] == 'type' and os.environ.get('FAKE_MODIFIER_DURING_PHASE') == str(phase)
):
    Path(os.environ['WAYLAND_MODIFIER_STATE_FILE']).write_text('META=0\nSHIFT=0\nCTRL=1\nALT=0\n')
    if args[0] == 'type':
        time.sleep(2)
if args[0] == 'type' and os.environ.get('FAKE_FAIL_PHASE') == str(phase):
    sys.exit(7)
"""

FAKE_COPYQ = r"""
import json
import os
import sys
from pathlib import Path

root = Path(os.environ['FAKE_ROOT'])
if os.environ.get('FAKE_COPYQ_FAIL'):
    sys.exit(1)
log = root / 'copyq.jsonl'
args = sys.argv[1:]
stdin_text = sys.stdin.buffer.read().decode()
with log.open('a') as output:
    output.write(json.dumps({'args': args, 'stdin': stdin_text}) + '\n')
if os.environ.get('FAKE_MODIFIER_AFTER_COPY') and 'transient' in ' '.join(args):
    Path(os.environ['WAYLAND_MODIFIER_STATE_FILE']).write_text('META=0\nSHIFT=0\nCTRL=1\nALT=0\n')
"""

FAKE_KDOTOOL = r"""
import os
import sys

print(os.environ.get('FAKE_ACTIVE_CLASS', 'google-chrome'))
"""

FAKE_WL_COPY = r"""
import json
import os
import sys
from pathlib import Path

root = Path(os.environ['FAKE_ROOT'])
if os.environ.get('FAKE_WL_COPY_FAIL'):
    sys.exit(1)
log = root / 'wl_copy.jsonl'
args = sys.argv[1:]
stdin_text = None if '--clear' in args else sys.stdin.buffer.read().decode()
with log.open('a') as output:
    output.write(json.dumps({'args': args, 'stdin': stdin_text}) + '\n')
"""

FAKE_WL_PASTE = r"""
import os
import sys

saved = os.environ.get('FAKE_WL_PASTE_TEXT')
if saved is None:
    sys.exit(1)
sys.stdout.write(saved)
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)


@unittest.skipUnless(sys.platform == "linux" and shutil.which("flock"), "Linux flock required")
class WaylandPasteHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="assistant-paste-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "modifiers.env"
        self.state.write_text(CLEAR_MODIFIERS)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "XDG_RUNTIME_DIR": str(self.root),
            "YDOTOOL_SOCKET": str(self.root / "unused-socket"),
            "WAYLAND_MODIFIER_STATE_FILE": str(self.state),
            "FAKE_ROOT": str(self.root),
            "ASSISTANT_PASTE_SETTLE_S": "0",
            "ASSISTANT_PASTE_RESTORE_DELAY_S": "0",
        }
        for name in (
            "ASSISTANT_INSERT_MODE",
            "FAKE_MODIFIER_AFTER_RELEASE",
            "FAKE_MODIFIER_AFTER_COPY",
            "FAKE_COPYQ_FAIL",
            "FAKE_WL_COPY_FAIL",
            "FAKE_WL_PASTE_TEXT",
            "FAKE_ACTIVE_CLASS",
        ):
            self.env.pop(name, None)

        _write_exec(self.bin / "ydotool", FAKE_YDOTOOL)
        _write_exec(self.bin / "copyq", FAKE_COPYQ)
        _write_exec(self.bin / "kdotool", FAKE_KDOTOOL)
        _write_exec(self.bin / "wl-copy", FAKE_WL_COPY)
        _write_exec(self.bin / "wl-paste", FAKE_WL_PASTE)
        service = self.bin / "systemctl"
        service.write_text("#!/bin/sh\necho 'unexpected service access' >&2\nexit 99\n")
        service.chmod(0o755)

    def run_helper(self, text):
        return subprocess.run(
            ["bash", str(HELPER)],
            input=text,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )

    def commands(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def copyq_calls(self):
        path = self.root / "copyq.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wl_copy_calls(self):
        path = self.root / "wl_copy.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_default_paste_uses_copyq_transient_mime_ctrl_v_and_restores(self):
        text = "Café — “hello” 🚀\nsecond line"
        for window_class in ("google-chrome", "xfce4-terminal"):
            with self.subTest(window_class=window_class):
                for name in ("commands.jsonl", "copyq.jsonl"):
                    (self.root / name).unlink(missing_ok=True)
                self.env["FAKE_ACTIVE_CLASS"] = window_class
                result = self.run_helper(text)
                self.assertEqual(result.returncode, 0, result.stderr)

                calls = self.copyq_calls()
                self.assertEqual(len(calls), 2)
                self.assertIn("application/x-assistant-dictation-transient", calls[0]["args"][1])
                self.assertEqual(calls[0]["stdin"], text)
                self.assertIn("getItem(0)", calls[1]["args"][1])

                keys = [c["args"] for c in self.commands() if c["args"][0] == "key"]
                self.assertIn(["key", "--key-delay", "0", "29:1", "47:1", "47:0", "29:0"], keys)

    def test_shift_ctrl_v_terminals_receive_ctrl_shift_v(self):
        self.env["FAKE_ACTIVE_CLASS"] = "kitty"
        result = self.run_helper("Hello terminal")
        self.assertEqual(result.returncode, 0, result.stderr)
        keys = [c["args"] for c in self.commands() if c["args"][0] == "key"]
        self.assertIn(["key", "--key-delay", "0", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"], keys)

    def test_large_text_passes_via_stdin_without_arg_limit(self):
        text = "Keep every character.\n" * 10000
        result = self.run_helper(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.copyq_calls()[0]["stdin"], text)

    def test_modifier_pressed_after_copy_aborts_paste_and_still_restores_clipboard(self):
        self.env["FAKE_MODIFIER_AFTER_COPY"] = "1"
        result = self.run_helper("Do not paste")
        self.assertEqual(result.returncode, 5, result.stderr)
        # Transient copy + guaranteed EXIT trap restore
        calls = self.copyq_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("getItem(0)", calls[1]["args"][1])
        # Never sent Ctrl+V
        keys = [c["args"] for c in self.commands() if c["args"][0] == "key"]
        self.assertNotIn(["key", "--key-delay", "0", "29:1", "47:1", "47:0", "29:0"], keys)

    def test_wl_copy_fallback_saves_and_restores_previous_clipboard(self):
        self.env["FAKE_COPYQ_FAIL"] = "1"
        self.env["FAKE_WL_PASTE_TEXT"] = "previous clipboard content"
        result = self.run_helper("Dictated text")
        self.assertEqual(result.returncode, 0, result.stderr)
        wl_calls = self.wl_copy_calls()
        self.assertEqual(len(wl_calls), 2)
        self.assertEqual(wl_calls[0]["stdin"], "Dictated text")
        self.assertEqual(wl_calls[1]["stdin"], "previous clipboard content")

    def test_wl_copy_fallback_clears_when_clipboard_was_empty(self):
        self.env["FAKE_COPYQ_FAIL"] = "1"
        result = self.run_helper("Dictated text")
        self.assertEqual(result.returncode, 0, result.stderr)
        wl_calls = self.wl_copy_calls()
        self.assertEqual(len(wl_calls), 2)
        self.assertEqual(wl_calls[0]["stdin"], "Dictated text")
        self.assertIn("--clear", wl_calls[1]["args"])

    def test_falls_back_to_ydotool_type_when_no_clipboard_tool_works(self):
        self.env["FAKE_COPYQ_FAIL"] = "1"
        self.env["FAKE_WL_COPY_FAIL"] = "1"
        result = self.run_helper("Fallback text")
        self.assertEqual(result.returncode, 0, result.stderr)
        typed = [c["text"] for c in self.commands() if c["args"][0] == "type"]
        self.assertEqual("".join(typed), "Fallback text")


@unittest.skipUnless(sys.platform == "linux" and shutil.which("flock"), "Linux flock required")
class WaylandTypeHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="assistant-type-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "modifiers.env"
        self.state.write_text(CLEAR_MODIFIERS)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "XDG_RUNTIME_DIR": str(self.root),
            "YDOTOOL_SOCKET": str(self.root / "unused-socket"),
            "WAYLAND_MODIFIER_STATE_FILE": str(self.state),
            "FAKE_ROOT": str(self.root),
            "ASSISTANT_INSERT_MODE": "type",
        }
        for name in ("FAKE_MODIFIER_AFTER_RELEASE", "FAKE_MODIFIER_DURING_PHASE", "FAKE_FAIL_PHASE"):
            self.env.pop(name, None)
        _write_exec(self.bin / "ydotool", FAKE_YDOTOOL)
        service = self.bin / "systemctl"
        service.write_text("#!/bin/sh\necho 'unexpected service access' >&2\nexit 99\n")
        service.chmod(0o755)

    def run_helper(self, text):
        return subprocess.run(
            ["bash", str(HELPER)],
            input=text,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )

    def commands(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def typed(self):
        return [item for item in self.commands() if item["args"][0] == "type"]

    def test_paced_lead_in_and_fast_body_preserve_exact_text(self):
        text = "Hello, 'world'!\n--file=literal \\path\n\n"
        result = self.run_helper(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        chunks = self.typed()
        self.assertEqual([item["text"] for item in chunks], [text[:3], text[3:]])
        self.assertEqual(chunks[0]["args"], ["type", "--key-delay", "20", "--key-hold", "10", "--file=-"])
        self.assertEqual(chunks[1]["args"], ["type", "--key-delay", "0", "--key-hold", "0", "--file=-"])

    def test_short_text_has_no_empty_second_phase_or_extra_characters(self):
        for text in ("H", "Hi", "Hey", "\n\n", "  "):
            with self.subTest(text=text):
                log = self.root / "commands.jsonl"
                log.unlink(missing_ok=True)
                result = self.run_helper(text)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([item["text"] for item in self.typed()], [text])

    def test_large_text_does_not_hit_command_argument_limit(self):
        text = "Keep every character.\n" * 10000
        result = self.run_helper(text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual("".join(item["text"] for item in self.typed()), text)

    def test_empty_text_does_not_touch_keyboard(self):
        result = self.run_helper("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), [])

    def test_held_modifier_refuses_all_typing(self):
        self.state.write_text(HELD_MODIFIERS)
        result = self.run_helper("Hello world")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("modifier key is currently held", result.stderr)
        self.assertEqual(self.typed(), [])

    def test_modifier_pressed_during_settling_is_rechecked(self):
        self.env["FAKE_MODIFIER_AFTER_RELEASE"] = "1"
        result = self.run_helper("Hello world")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(self.typed(), [])

    def test_modifier_interrupts_both_typing_phases(self):
        for phase in (1, 2):
            with self.subTest(phase=phase):
                (self.root / "commands.jsonl").unlink(missing_ok=True)
                self.state.write_text(CLEAR_MODIFIERS)
                self.env["FAKE_MODIFIER_DURING_PHASE"] = str(phase)
                result = self.run_helper("Hello world")
                self.assertEqual(result.returncode, 5, result.stderr)
                self.assertIn("modifier key was pressed during typing", result.stderr)
                self.assertEqual(len(self.typed()), phase)

    def test_failed_lead_in_does_not_type_remainder(self):
        self.env["FAKE_FAIL_PHASE"] = "1"
        result = self.run_helper("Hello world")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(len(self.typed()), 1)


@unittest.skipUnless(
    sys.platform == "linux" and shutil.which("ydotool") and shutil.which("flock"),
    "Linux ydotool required",
)
class PrivateKeyboardSocketTests(unittest.TestCase):
    def test_real_ydotool_preserves_opening_letters_and_phase_boundary(self):
        # A private socket sinks all events: no daemon or desktop input is used.
        with tempfile.TemporaryDirectory(prefix="assistant-key-sink-") as directory:
            root = Path(directory)
            state = root / "modifiers.env"
            state.write_text(CLEAR_MODIFIERS)
            env = {
                **os.environ,
                "XDG_RUNTIME_DIR": str(root),
                "YDOTOOL_SOCKET": str(root / "sink"),
                "WAYLAND_MODIFIER_STATE_FILE": str(state),
                "ASSISTANT_INSERT_MODE": "type",
            }
            events = []
            event_format = struct.Struct("@llHHi")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sink:
                sink.bind(env["YDOTOOL_SOCKET"])
                sink.settimeout(0.02)
                proc = subprocess.Popen(
                    ["bash", str(HELPER)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                try:
                    proc.stdin.write(b"Hello world.\n")
                    proc.stdin.close()
                    proc.stdin = None
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            events.extend(event_format.iter_unpack(sink.recv(65536)))
                        except socket.timeout:
                            if proc.poll() is not None:
                                break
                        if time.monotonic() >= deadline:
                            self.fail("typing helper did not finish")
                    _, stderr = proc.communicate(timeout=1)
                    self.assertEqual(proc.returncode, 0, stderr.decode())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate(timeout=2)
            down = [code for _, _, kind, code, value in events if kind == 1 and value == 1]
            self.assertEqual(down, [42, 35, 18, 38, 38, 24, 57, 17, 24, 19, 38, 32, 52, 28])
            for code in set(down):
                presses = sum(kind == 1 and key == code and value == 1 for _, _, kind, key, value in events)
                releases = sum(kind == 1 and key == code and value == 0 for _, _, kind, key, value in events)
                self.assertGreaterEqual(releases, presses)


if __name__ == "__main__":
    unittest.main()
