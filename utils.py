"""Validation, local duplicate tracking, and a small HTTP helper."""

from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FIELDS = ("record_id", "name", "email", "company", "message", "ai_summary", "ai_category")
LIMITS = {"record_id": 128, "name": 200, "email": 254, "company": 200, "message": 5000}


def normalize_record(value):
    if not isinstance(value, dict):
        raise ValueError("Input must be a JSON object")
    result = {}
    for field, limit in LIMITS.items():
        item = value.get(field, "")
        if not isinstance(item, str):
            raise ValueError(f"{field} must be a string")
        item = item.strip()
        if not item and field != "company":
            raise ValueError(f"{field} is required and must not be empty")
        if len(item) > limit:
            raise ValueError(f"{field} exceeds {limit} characters")
        result[field] = item
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", result["email"]):
        raise ValueError("email must be a valid email address")
    local, domain = result["email"].rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local or any(len(label) > 63 for label in domain.split(".")):
        raise ValueError("email must be a valid email address")
    result.update(ai_summary="", ai_category="")
    return result


class ProcessedIDStore:
    """Single-process store. Atomic replacement protects against partial JSON."""

    def __init__(self, path):
        self.path = Path(path)
        self.ids = set()
        if self.path.exists():
            try:
                values = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise ValueError("Processed IDs file must contain a JSON array of non-empty strings") from None
            self.ids = set(values)

    def contains(self, record_id):
        return record_id in self.ids

    def add(self, record_id):
        if self.contains(record_id):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        updated = self.ids | {record_id}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(sorted(updated), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.ids = updated
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def append_jsonl(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def post_json(url, payload, headers=None):
    """Three total attempts for network errors, 429 and 5xx; no response bodies in errors."""
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            status = error.code
            error.close()
            if status != 429 and not 500 <= status < 600:
                raise RuntimeError(f"External request failed (HTTP {status})") from None
            reason = f"HTTP {status}"
        except (URLError, TimeoutError, ConnectionError, IncompleteRead):
            reason = "network error"
        except (ValueError, UnicodeError):
            raise RuntimeError("External service returned invalid JSON") from None
        if attempt == 2:
            raise RuntimeError(f"External request failed after 3 attempts ({reason})") from None
        time.sleep(0.25 * 2 ** attempt)
