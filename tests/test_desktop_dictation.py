import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "desktop_dictation"
    / "transcribe-and-type.py"
)
SPEC = importlib.util.spec_from_file_location("transcribe_and_type", MODULE_PATH)
transcribe_and_type = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(transcribe_and_type)


class DeliveryTimingFormatTests(unittest.TestCase):
    @patch.object(transcribe_and_type.time, "time", return_value=2.002)
    def test_total_breakdown_names_non_server_overhead(self, _mock_time):
        details = transcribe_and_type.format_delivery_timing(
            {
                "client_stop_to_upload_ms": 211,
                "client_stop_finalize_ms": 62,
                "client_queue_wait_ms": 149,
                "client_file_read_ms": 0,
                "server_total_ms": 693,
            },
            stop_requested_ms=1000,
            type_ms=81,
        )

        self.assertEqual(
            details,
            "total_ms=1002 (client_stop_to_upload_ms=211 + "
            "client_network_and_delivery_overhead_ms=17 + server_total_ms=693 + "
            "client_paste_ms=81)",
        )


class DesktopDeliveryReportTests(unittest.TestCase):
    def test_upload_declares_that_desktop_will_report_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "fixture.wav"
            audio_path.write_bytes(b"audio")
            response = io.BytesIO(json.dumps({"text": "Test transcript."}).encode())
            with (
                patch.object(
                    transcribe_and_type.urllib.request, "urlopen", return_value=response
                ) as urlopen,
                patch.object(transcribe_and_type, "log"),
            ):
                text, _, _ = transcribe_and_type.transcribe(audio_path)

        request = urlopen.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-client-delivery-report"], "desktop")
        self.assertEqual(request.data, b"audio")
        self.assertEqual(text, "Test transcript.")

    def run_client(
        self,
        *,
        text="Test transcript.",
        paste_result=(65, True, ""),
        paste_error=None,
        transcription_error=None,
    ):
        with (
            patch.object(transcribe_and_type.sys, "argv", ["client", "fixture.wav"]),
            patch.object(
                transcribe_and_type, "transcribe", return_value=(text, {}, -1),
                side_effect=transcription_error,
            ),
            patch.object(
                transcribe_and_type, "type_text", return_value=paste_result,
                side_effect=paste_error,
            ) as type_text,
            patch.object(transcribe_and_type, "report_delivery") as report,
            patch.object(transcribe_and_type, "log"),
        ):
            exit_code = transcribe_and_type.main()
        return exit_code, type_text, report

    def test_successful_paste_reports_complete(self):
        exit_code, type_text, report = self.run_client()
        self.assertEqual(exit_code, 0)
        type_text.assert_called_once_with("Test transcript.")
        report.assert_called_once_with("complete", "total_ms=-1 client_paste_ms=65")

    def test_modifier_refusal_reports_refused(self):
        exit_code, _, report = self.run_client(
            paste_result=(12, False, "modifier_pressed_during_typing"),
        )
        self.assertEqual(exit_code, 0)
        report.assert_called_once_with(
            "refused", "total_ms=-1 client_paste_ms=12 "
            "refusal_reason=modifier_pressed_during_typing",
        )

    def test_paste_failure_reports_failed_and_preserves_retry_exit_code(self):
        exit_code, _, report = self.run_client(paste_error=RuntimeError("helper failed"))
        self.assertEqual(exit_code, 1)
        report.assert_called_once_with("failed", "error=helper failed")

    def test_transcription_failure_does_not_report_delivery(self):
        exit_code, type_text, report = self.run_client(
            transcription_error=RuntimeError("server unavailable"),
        )
        self.assertEqual(exit_code, 1)
        type_text.assert_not_called()
        report.assert_not_called()

    def test_empty_transcript_does_not_report_delivery(self):
        exit_code, type_text, report = self.run_client(text="")
        self.assertEqual(exit_code, 0)
        type_text.assert_not_called()
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
