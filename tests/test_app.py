import contextlib
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import app
from utils import LIMITS, ProcessedIDStore, normalize_record

SAMPLE = {"record_id": "lead-1001", "name": "Alex Rivera", "email": "alex@example.com",
          "company": "Example Inc", "message": "We need weekly reporting automation."}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProcessedIDStore(self.root / "ids.json")

    @patch("utils.time.sleep")
    @patch("utils.urlopen")
    def test_cli_exhausted_truncated_sheet_response_returns_json_error(self, http, sleep):
        source = self.root / "input.json"
        source.write_text(json.dumps(SAMPLE))
        http.return_value.__enter__.return_value.read.side_effect = IncompleteRead(b"private body", 20)
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {
            "DEMO_MODE": "false", "GOOGLE_SHEETS_ID": "fake-id", "GOOGLE_ACCESS_TOKEN": "fake-token",
            "PROCESSED_IDS_FILE": str(self.store.path),
        }, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(app.main([str(source)]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue()), {
            "status": "error", "error": "External request failed after 3 attempts (network error)"})
        self.assertFalse(self.store.path.exists())
        self.assertEqual(http.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])

    @patch("utils.time.sleep")
    @patch("utils.urlopen")
    def test_cli_truncated_telegram_returns_warning_then_duplicate(self, http, sleep):
        source = self.root / "input.json"
        source.write_text(json.dumps(SAMPLE))
        sheet_response = MagicMock()
        sheet_response.__enter__.return_value.read.return_value = b'{"updates":{"updatedRows":1}}'
        telegram_response = MagicMock()
        telegram_response.__enter__.return_value.read.side_effect = IncompleteRead(b"partial", 20)

        def transport(request, **kwargs):
            if "sheets.googleapis.com" in request.full_url:
                return sheet_response
            self.assertIn("api.telegram.org", request.full_url)
            self.assertTrue(ProcessedIDStore(self.store.path).contains(SAMPLE["record_id"]))
            return telegram_response

        http.side_effect = transport
        with patch.dict(os.environ, {
            "DEMO_MODE": "false", "GOOGLE_SHEETS_ID": "fake-id", "GOOGLE_ACCESS_TOKEN": "fake-token",
            "ENABLE_TELEGRAM": "true", "TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "123",
            "PROCESSED_IDS_FILE": str(self.store.path),
        }, clear=True):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(app.main([str(source)]), 0)
            self.assertEqual(json.loads(output.getvalue()), {
                "status": "processed", "record_id": SAMPLE["record_id"],
                "warning": "Record saved; notification failed and will not be automatically resent"})
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(http.call_count, 4)
            self.assertEqual(sheet_response.__enter__.return_value.read.call_count, 1)
            self.assertEqual(telegram_response.__enter__.return_value.read.call_count, 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])
            self.assertTrue(ProcessedIDStore(self.store.path).contains(SAMPLE["record_id"]))

            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(app.main([str(source)]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"status": "duplicate", "record_id": SAMPLE["record_id"]})
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(http.call_count, 4)

    def test_valid_normalization_and_optional_company(self):
        result = normalize_record(SAMPLE)
        self.assertEqual(result, {**SAMPLE, "ai_summary": "", "ai_category": ""})
        self.assertEqual(normalize_record({k: v for k, v in SAMPLE.items() if k != "company"})["company"], "")
        self.assertEqual(normalize_record({k: f"  {v}  " for k, v in SAMPLE.items()}), result)

    def test_required_missing_empty_and_wrong_types(self):
        for field in ("record_id", "name", "email", "message"):
            for value in (None, "", "  ", 12):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    normalize_record({**SAMPLE, field: value})
            with self.assertRaisesRegex(ValueError, field):
                normalize_record({k: v for k, v in SAMPLE.items() if k != field})
        with self.assertRaises(ValueError):
            normalize_record([])

    def test_invalid_email(self):
        for email in ("bad", "a@", "a b@example.com", "a@-example.com", "a..b@example.com", "a@example", ".a@example.com"):
            with self.subTest(email=email), self.assertRaisesRegex(ValueError, "email"):
                normalize_record({**SAMPLE, "email": email})

    def test_oversized_fields(self):
        for field, limit in LIMITS.items():
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "exceeds"):
                normalize_record({**SAMPLE, field: "x" * (limit + 1)})

    def test_processing_and_duplicate_skip_all_side_effects(self):
        sheet, notifier, enricher = Mock(), Mock(), Mock()
        enricher.enrich.return_value = {"summary": "Reports", "category": "operations"}
        self.assertEqual(app.process_record(SAMPLE, sheet, self.store, notifier, enricher)["status"], "processed")
        self.assertEqual(sheet.append.call_args.args[0]["ai_category"], "operations")
        self.assertTrue(ProcessedIDStore(self.store.path).contains(SAMPLE["record_id"]))
        self.assertEqual(app.process_record(SAMPLE, sheet, self.store, notifier, enricher)["status"], "duplicate")
        sheet.append.assert_called_once()
        notifier.notify.assert_called_once()
        enricher.enrich.assert_called_once()

    def test_failed_sheet_does_not_mark_or_notify(self):
        sheet, notifier = Mock(), Mock()
        sheet.append.side_effect = RuntimeError("write failed")
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            app.process_record(SAMPLE, sheet, self.store, notifier)
        self.assertFalse(self.store.contains(SAMPLE["record_id"]))
        notifier.notify.assert_not_called()

    def test_disabled_enrichment_and_notification_failure(self):
        sheet, notifier = Mock(), Mock()
        notifier.notify.side_effect = RuntimeError("unavailable")
        result = app.process_record(SAMPLE, sheet, self.store, notifier)
        self.assertEqual(result["status"], "processed")
        self.assertIn("warning", result)
        self.assertEqual(sheet.append.call_args.args[0]["ai_summary"], "")
        self.assertTrue(self.store.contains(SAMPLE["record_id"]))

    def test_store_roundtrip_idempotency_and_failed_atomic_replace(self):
        self.store.add("first")
        self.store.add("first")
        self.assertEqual(json.loads(self.store.path.read_text()), ["first"])
        with patch("utils.os.replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
            self.store.add("second")
        self.assertFalse(self.store.contains("second"))
        self.assertEqual(json.loads(self.store.path.read_text()), ["first"])
        self.assertEqual(list(self.root.iterdir()), [self.store.path])

    def test_malformed_store_fails_closed(self):
        for value in ("broken", "{}", '[1]', '[""]'):
            self.store.path.write_text(value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Processed IDs"):
                ProcessedIDStore(self.store.path)

    def test_cli_demo_without_credentials_and_duplicate(self):
        source = self.root / "input.json"
        source.write_text(json.dumps(SAMPLE))
        with patch.object(app, "ROOT", self.root), patch.dict(os.environ, {
            "ENABLE_TELEGRAM": "true", "ENABLE_OPENAI": "true"}, clear=True), patch("utils.urlopen") as http:
            for status in ("processed", "duplicate"):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(app.main([str(source)]), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], status)
            http.assert_not_called()
        for filename in ("demo_sheet.jsonl", "demo_notifications.jsonl"):
            self.assertEqual(len((self.root / "data" / filename).read_text().splitlines()), 1)
        with patch.dict(os.environ, {"DEMO_MODE": "typo"}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main([str(source)]), 1)


if __name__ == "__main__":
    unittest.main()
