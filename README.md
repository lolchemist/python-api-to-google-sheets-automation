# Python API → Google Sheets Automation

A lightweight Python workflow that validates and deduplicates incoming records, appends them to Google Sheets, and can optionally send Telegram alerts or enrich data with OpenAI.

## Business use cases

- Lead capture: collect qualified inquiries in a shared sales sheet.
- CRM export: normalize exported records before adding them to a team workbook.
- Form submissions: validate contact details and prevent repeated submissions by ID.
- Reporting automation: collect structured events for weekly reports.
- Order/event tracking: record incoming events in a consistent column layout.
- Customer inquiry processing: optionally summarize and classify messages for review.

This small demo is a starting point for a scoped freelance integration. It accepts one JSON record per invocation; an existing API integration can call `process_record()` directly.

## Workflow

```text
JSON file / caller-provided API data
  → validate and trim fields
  → check local record_id store → skip duplicates
  → optional OpenAI enrichment
  → append one Google Sheets row (or local demo row)
  → atomically save processed ID
  → optional Telegram notification
```

## Project structure

```text
python-api-to-google-sheets-automation/
├── app.py                  # CLI, configuration and workflow
├── integrations.py         # Sheets, Telegram and OpenAI clients
├── utils.py                # Validation, ID store and HTTP retry
├── data/
│   └── sample_input.json
├── tests/
│   ├── __init__.py
│   ├── test_app.py
│   └── test_integrations.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Quick start

Use Python 3.11 or newer. There are no third-party dependencies.

```bash
cd python-api-to-google-sheets-automation
python3.11 -m venv .venv
source .venv/bin/activate
python app.py data/sample_input.json
```

With no environment configuration, demo mode is enabled and both optional features are disabled. First run:

```json
{"status": "processed", "record_id": "lead-1001"}
```

Run the same command again:

```json
{"status": "duplicate", "record_id": "lead-1001"}
```

Application/runtime errors (including exhausted transport retries) produce JSON on stderr and exit code 1. Argparse usage errors, such as a missing input-file argument, may return argparse text on stderr with exit code 2. Successful and duplicate runs return 0.

## Sample input and validation

```json
{
  "record_id": "lead-1001",
  "name": "Alex Rivera",
  "email": "alex@example.com",
  "company": "Example Inc",
  "message": "We need help automating our weekly reports."
}
```

Required fields are non-empty strings. All accepted fields are trimmed. Company is optional and defaults to an empty string; explicit null values are rejected. Unknown fields are ignored, including caller-supplied AI fields. Email syntax is checked for common ASCII addresses; delivery is not verified.

| Field | Maximum characters after trimming |
| --- | ---: |
| record_id | 128 |
| name | 200 |
| email | 254 |
| company | 200 |
| message | 5000 |

Normalized output always contains `record_id`, `name`, `email`, `company`, `message`, `ai_summary`, `ai_category`, in that row order. AI fields are empty when enrichment is disabled. IDs are case-sensitive.

## Demo mode

`DEMO_MODE=true` prevents all external calls, even when optional features are enabled:

- Rows are JSON arrays, one per line in `data/demo_sheet.jsonl`.
- Enabled notifications are JSON objects in `data/demo_notifications.jsonl`.
- Enabled enrichment uses a deterministic message preview and the category `other`; it does not run an AI model.

```bash
ENABLE_TELEGRAM=true ENABLE_OPENAI=true python app.py data/sample_input.json
```

Use a new record ID to demonstrate optional features after the first run. Generated files are ignored by Git. The processed-ID path defaults to `data/processed_ids.json`. Relative state paths are resolved from the project directory; input paths are resolved from the working directory.

## Environment configuration

`.env.example` lists every setting. The application reads environment variables; it does **not** load `.env` automatically. For a locally maintained, trusted configuration in bash/zsh:

```bash
cp .env.example .env
# Edit .env; quote values containing shell special characters.
set -a
source .env
set +a
```

Boolean settings accept `true` or `false` (case-insensitive). Other values are rejected.

## Google Sheets integration

Set `DEMO_MODE=false`, `GOOGLE_SHEETS_ID`, `GOOGLE_ACCESS_TOKEN`, and optionally `GOOGLE_SHEETS_RANGE` (default `Sheet1!A:G`). Create the sheet tab and, if desired, add the seven column headers yourself. The injected access token must have permission to edit that spreadsheet and a suitable Sheets OAuth scope. Obtain and refresh the token outside this project; no OAuth flow or refresh mechanism is included.

`GoogleSheetsClient(spreadsheet_id, access_token, range_name)` calls the [Sheets append REST endpoint](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append). It uses `RAW` values so customer input is stored as text, and confirms one updated row before saving the ID. When switching from demo to live mode, use a separate store, for example `PROCESSED_IDS_FILE=data/processed_ids_live.json`, so demo IDs do not suppress live records.

## Optional Telegram integration

Set `ENABLE_TELEGRAM=true`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The client uses [sendMessage](https://core.telegram.org/bots/api#sendmessage) to send the name, email, company and a 160-character message preview after the row and local ID have been saved. Text is sent without markup parsing.

Telegram delivery is best-effort after successful processing. If notification attempts ultimately fail, including from truncated HTTP responses, the CLI returns `status: processed` with a warning and exit code 0. It does not undo the saved row or ID. Failed delivery is not automatically replayed: the next invocation with the same ID returns `duplicate` without sending another notification.

## Optional OpenAI enrichment

Set `ENABLE_OPENAI=true`, `OPENAI_API_KEY` and `OPENAI_MODEL` to a model available in your account that supports structured output. There is no default paid model. Live enrichment sends only the message to the [OpenAI Responses API using structured output](https://developers.openai.com/api/docs/guides/structured-outputs), with `store=false`.

The result must contain a non-empty summary of at most 1,000 characters and a category of `sales`, `support`, `operations` or `other`. Invalid JSON, invalid categories, refusals and incomplete responses stop processing before a Sheets write. Review AI classifications before relying on them for business decisions. With enrichment disabled, the rest of the workflow needs no OpenAI configuration.

## Tests

```bash
python -m unittest discover -v
```

Final test count: **23**. Tests cover validation, normalization, duplicate side effects, failed writes, retries, atomic store replacement, malformed state, HTTP contracts, enrichment validation, disabled enrichment, notification failures and the offline CLI. Regression coverage includes truncated-response recovery and exhaustion, explicit timeout exhaustion, CLI JSON errors, and Telegram partial failure followed by a duplicate rerun. All HTTP calls are mocked; retry sleeps are mocked. Tests use temporary directories and no real credentials.

## Reliability features

- Duplicate IDs skip Sheets, enrichment and notifications.
- IDs are saved only after a successful sheet write.
- Atomic replacement and file flushing protect the ID file from partial JSON writes.
- Malformed ID files fail closed instead of silently resetting deduplication.
- HTTP requests have a 20-second timeout and at most three total attempts.
- Network failures (including timeouts and `http.client.IncompleteRead` from truncated responses), HTTP 429 and HTTP 5xx retry after 0.25 and 0.5 seconds. Exhausted retries raise a clean application error. Other HTTP errors fail immediately.
- Transport errors omit response bodies, tokens and request URLs from CLI output.

## Security and secrets

Credentials are injected from the environment. `.env`, generated data and the virtual environment are ignored by Git. Only synthetic example records and fake test tokens are included. Keep state files private: they contain IDs and demo contact data. Custom state files outside the default ignored naming pattern need their own ignore rules. Never commit access tokens or client data. Enable live integrations only with permission to share the corresponding customer data; OpenAI usage may incur charges.

## Limitations

This is a small, single-process portfolio demo, not an exactly-once delivery system. Do not run concurrent workers against the same state file. A server can accept an append and lose the response; retries can then duplicate that row. A crash or local storage failure after a remote append but before saving its ID can also cause duplicates on rerun. Reconcile the sheet by record ID before retrying an ambiguous failure. Telegram retries have the same uncertain-delivery limitation.

There is no inbound web server, source-API polling, bulk import, scheduler, database, OAuth renewal or automatic recovery workflow. State grows with the number of records. Demo JSONL files are simple append-only files. HTTP retries do not implement provider-specific `Retry-After` scheduling. Local validation does not verify email ownership. Live credentials, permissions and model availability must be checked in an owner-approved smoke test before client use.

## Portfolio positioning

**Suggested repository description:** Python automation for validating, deduplicating and syncing API data to Google Sheets, with optional Telegram alerts and OpenAI enrichment.

**Suggested Upwork portfolio title:** Python API to Google Sheets Automation | Data Sync & Alerts
