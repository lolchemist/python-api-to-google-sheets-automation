"""Run one JSON record through the automation workflow."""

import argparse
import json
import os
from pathlib import Path
import sys

from integrations import GoogleSheetsClient, OpenAIEnricher, TelegramNotifier
from utils import ProcessedIDStore, normalize_record

ROOT = Path(__file__).resolve().parent


def process_record(value, sheet, store, notifier=None, enricher=None):
    record = normalize_record(value)
    record_id = record["record_id"]
    if store.contains(record_id):
        return {"status": "duplicate", "record_id": record_id}
    if enricher is not None:
        enrichment = enricher.enrich(record["message"])
        record.update(ai_summary=enrichment["summary"], ai_category=enrichment["category"])
    sheet.append(record)
    store.add(record_id)
    result = {"status": "processed", "record_id": record_id}
    if notifier is not None:
        try:
            notifier.notify(record)
        except (RuntimeError, OSError, ValueError):
            result["warning"] = "Record saved; notification failed and will not be automatically resent"
    return result


def env_bool(name, default=False):
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", type=Path)
    args = parser.parse_args(argv)
    try:
        demo = env_bool("DEMO_MODE", True)
        sheet = GoogleSheetsClient(os.getenv("GOOGLE_SHEETS_ID", ""), os.getenv("GOOGLE_ACCESS_TOKEN", ""),
                                   os.getenv("GOOGLE_SHEETS_RANGE", "Sheet1!A:G"),
                                   demo_path=ROOT / "data/demo_sheet.jsonl" if demo else None)
        notifier = TelegramNotifier(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""),
                                    demo_path=ROOT / "data/demo_notifications.jsonl" if demo else None) if env_bool("ENABLE_TELEGRAM") else None
        enricher = OpenAIEnricher(os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_MODEL", ""), demo=demo) if env_bool("ENABLE_OPENAI") else None
        store_path = ROOT / os.getenv("PROCESSED_IDS_FILE", "data/processed_ids.json")
        store = ProcessedIDStore(store_path)
        value = json.loads(args.input_file.read_text(encoding="utf-8"))
        print(json.dumps(process_record(value, sheet, store, notifier, enricher), indent=2))
        return 0
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 1
    except OSError:
        print(json.dumps({"status": "error", "error": "Could not read or write a local file"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
