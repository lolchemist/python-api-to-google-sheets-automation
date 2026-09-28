from http.client import IncompleteRead
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from app import process_record
from integrations import GoogleSheetsClient, OpenAIEnricher, TelegramNotifier
from tests.test_app import SAMPLE
from utils import normalize_record, post_json


def response(value):
    handle = MagicMock()
    handle.__enter__.return_value.read.return_value = json.dumps(value).encode()
    return handle


def ai_response(text):
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


class IntegrationTests(unittest.TestCase):
    @patch("utils.time.sleep")
    @patch("utils.urlopen")
    def test_truncated_response_retries_then_succeeds(self, http, sleep):
        truncated = response({})
        truncated.__enter__.return_value.read.side_effect = IncompleteRead(b"partial", 20)
        http.side_effect = [truncated, response({"ok": True})]
        self.assertEqual(post_json("https://example.com", {}), {"ok": True})
        self.assertEqual(http.call_count, 2)
        truncated.__exit__.assert_called_once()
        sleep.assert_called_once_with(0.25)

    @patch("utils.time.sleep")
    @patch("utils.urlopen")
    def test_truncated_response_exhaustion(self, http, sleep):
        http.return_value = response({})
        http.return_value.__enter__.return_value.read.side_effect = IncompleteRead(b"private body", 20)
        with self.assertRaises(RuntimeError) as caught:
            post_json("https://example.com", {})
        self.assertEqual(str(caught.exception), "External request failed after 3 attempts (network error)")
        self.assertEqual(http.call_count, 3)
        self.assertEqual(http.return_value.__exit__.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])

    @patch("utils.time.sleep")
    @patch("utils.urlopen")
    def test_timeout_exhaustion(self, http, sleep):
        for phase in ("connect", "read"):
            with self.subTest(phase=phase):
                http.reset_mock(side_effect=True)
                sleep.reset_mock()
                http.return_value = response({})
                if phase == "connect":
                    http.side_effect = TimeoutError("private details")
                else:
                    http.return_value.__enter__.return_value.read.side_effect = TimeoutError("private details")
                with self.assertRaises(RuntimeError) as caught:
                    post_json("https://example.com", {})
                self.assertEqual(str(caught.exception), "External request failed after 3 attempts (network error)")
                self.assertEqual(http.call_count, 3)
                self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])

    @patch("utils.urlopen")
    def test_sheets_url_payload_auth_and_row_order(self, http):
        http.return_value = response({"updates": {"updatedRows": 1}})
        GoogleSheetsClient("sheet-id", "fake-token", "Leads 2026!A:G").append(normalize_record(SAMPLE))
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, "https://sheets.googleapis.com/v4/spreadsheets/sheet-id/values/Leads%202026%21A%3AG:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
        self.assertEqual(json.loads(request.data), {"majorDimension": "ROWS", "values": [[SAMPLE[k] for k in ("record_id", "name", "email", "company", "message")] + ["", ""]]})
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-token")
        self.assertEqual(http.call_args.kwargs["timeout"], 20)

    @patch("utils.time.sleep")
    @patch("utils.urlopen")
    def test_transient_sheet_failure_retries_then_marks(self, http, sleep):
        http.side_effect = [HTTPError("https://example.com", 503, "busy", {}, io.BytesIO()), response({"updates": {"updatedRows": 1}})]
        store = MagicMock()
        store.contains.return_value = False
        process_record(SAMPLE, GoogleSheetsClient("id", "fake"), store)
        self.assertEqual(http.call_count, 2)
        sleep.assert_called_once_with(0.25)
        store.add.assert_called_once_with("lead-1001")

    @patch("utils.time.sleep")
    @patch("utils.urlopen")
    def test_permanent_http_error_and_exhausted_retries(self, http, sleep):
        http.side_effect = HTTPError("https://example.com/secret", 401, "secret", {}, io.BytesIO())
        with self.assertRaisesRegex(RuntimeError, r"HTTP 401"):
            post_json("https://example.com", {})
        self.assertEqual(http.call_count, 1)
        sleep.assert_not_called()
        http.reset_mock()
        http.side_effect = URLError("secret")
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            post_json("https://example.com", {})
        self.assertEqual(http.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0.5])

    @patch("utils.urlopen")
    def test_telegram_payload(self, http):
        http.return_value = response({"ok": True})
        TelegramNotifier("fake-token", "123").notify(normalize_record(SAMPLE))
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/botfake-token/sendMessage")
        payload = json.loads(request.data)
        self.assertEqual(payload["chat_id"], "123")
        self.assertEqual(payload["text"], "New record: Alex Rivera\nEmail: alex@example.com\nCompany: Example Inc\nMessage: We need weekly reporting automation.")
        self.assertNotIn("parse_mode", payload)

    @patch("utils.urlopen")
    def test_openai_valid_response_and_request(self, http):
        http.return_value = response(ai_response('{"summary":" Weekly reports ","category":"operations"}'))
        result = OpenAIEnricher("fake-key", "configured-model").enrich(SAMPLE["message"])
        self.assertEqual(result, {"summary": "Weekly reports", "category": "operations"})
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        payload = json.loads(request.data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"], SAMPLE["message"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")

    @patch("utils.urlopen")
    def test_openai_malformed_json_and_response(self, http):
        for value in (ai_response("not json"), {}, [], {"status": "incomplete"}):
            http.return_value = response(value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "malformed enrichment"):
                OpenAIEnricher("fake", "model").enrich("hello")

    @patch("utils.urlopen")
    def test_openai_invalid_category_and_summary(self, http):
        for value in ({"summary": "ok", "category": "unknown"}, {"summary": "", "category": "sales"}, {"summary": 3, "category": "other"}):
            http.return_value = response(ai_response(json.dumps(value)))
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "malformed enrichment"):
                OpenAIEnricher("fake", "model").enrich("hello")

    @patch("utils.urlopen")
    def test_unconfirmed_writes_and_invalid_transport_json(self, http):
        http.return_value = response({})
        with self.assertRaisesRegex(RuntimeError, "confirm"):
            GoogleSheetsClient("id", "fake").append(normalize_record(SAMPLE))
        with self.assertRaisesRegex(RuntimeError, "confirm"):
            TelegramNotifier("fake", "chat").notify(normalize_record(SAMPLE))
        http.return_value.__enter__.return_value.read.return_value = b"bad json"
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            post_json("https://example.com", {})


if __name__ == "__main__":
    unittest.main()
