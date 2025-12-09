# UFC Fight Pass Google Play Bulk Cancellation

High-level overview (stakeholder-ready)
- Goal: stop recurring billing for all Google Play–billed UFC Fight Pass subscriptions ahead of the January 1, 2026 shutdown.
- Approach: one-time scripted batch using Google Android Publisher API (`purchases.subscriptionsv2.cancel` with developer-requested stop payments).
- Safeguards: service account with Manage Orders only, dry-run mode, throttling, retries on transient errors, structured audit logs, and post-run reporting.

One-time tool to bulk-cancel Google Play–billed Fight Pass subscriptions ahead of the sunset. Uses the Google Android Publisher API `purchases.subscriptionsv2.cancel` with `DEVELOPER_REQUESTED_STOP_PAYMENTS`.

## Current state
- Script: `cancel_subscriptions.py`
- Env: Python venv created at `.venv`
- Installed deps: `google-api-python-client`, `google-auth` (and transitive deps)
- Reporting: `report_cancellation_log.py` to summarize JSONL logs; packaged CLI entry points available.

## Bootstrapping
Option A: scripted
```bash
sh setup_env.sh
source .venv/bin/activate
```

Option B: manual
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running the tool
```bash
source .venv/bin/activate
python cancel_subscriptions.py \
  --input tokens.csv \
  --service-account path/to/key.json \
  --package-name com.ufc.brazil.app \
  --log cancellation_log.jsonl \
  --delay 0.15 \
  --retries 3
```

Key flags:
- `--input`: CSV with `purchaseToken` column (optional `subscriptionId`).
- `--service-account`: JSON key with Manage Orders permission.
- `--package-name`: App package name.
- `--log`: JSONL audit log output (default `cancellation_log.jsonl`).
- `--delay`: Fixed delay between rows (seconds).
- `--retries`: Retries on 429/500/503 (exponential backoff + jitter).
- `--backoff`/`--jitter`: Tune retry backoff.
- `--max-rows`: Process only first N rows (for tests).
- `--dry-run`: Skip API calls; still parses and logs with status `dry_run`.
- `--token-column`: Column name for purchase tokens (default `purchaseToken`; falls back to `purchase_token`/`token`).
- `--subscription-id-column`: Optional column name for subscription IDs (default `subscriptionId`; falls back to `subscription_id`/`product`).
- `--progress` / `--no-progress`: Show or suppress a progress bar (default on).
- `--config`: Optional JSON file supplying defaults for any flags.

Logging & summary:
- Each row logged to JSONL with timestamp, token, subscriptionId, status, attempts, HTTP status, error type, and message.
- Summary printed at end: processed, success, already_cancelled, failed_transient, failed_permanent, dry_run.

## Notes / next steps
- Confirm the correct package name before running.
- Start with `--dry-run --max-rows <N>` to validate CSV parsing.
- We can extend README as we add features (e.g., CSV validation, progress reporting, resumable runs).

## Config file (optional)
You can supply a JSON config and omit most CLI flags. Example: `config.example.json`.
```json
{
  "input": "tokens.csv",
  "service_account": "path/to/key.json",
  "package_name": "com.ufc.brazil.app",
  "log": "cancellation_log.jsonl",
  "delay": 0.15,
  "retries": 3,
  "backoff": 0.25,
  "jitter": 0.25,
  "max_rows": null,
  "dry_run": false,
  "progress": true,
  "token_column": "purchaseToken",
  "subscription_id_column": "subscriptionId"
}
```
CLI flags still override config values; required fields must be present via config or CLI.

Validation:
- The script validates that a token column exists before processing (using the configured column plus fallbacks).
- If your CSV uses `token` (example provided) set `--token-column token` or put `"token_column": "token"` in your config.

## Reporting (summaries, CSV exports)
After a run, summarize the JSONL log and export failures/all rows to CSV:
```bash
source .venv/bin/activate
python report_cancellation_log.py \
  --log cancellation_log.jsonl \
  --failures-csv failures.csv \
  --all-csv all_rows.csv
```
Outputs:
- Console summary of status counts and failure breakdowns by errorType/httpStatus.
- `failures.csv` (optional): only failed rows.
- `all_rows.csv` (optional): every row with status and error info.

## CLI packaging (optional)
You can install console entry points to avoid typing `python …`:
```bash
source .venv/bin/activate
pip install -e .
fightpass-cancel --help
fightpass-report --help
```
Entry points:
- `fightpass-cancel` → `cancel_subscriptions.py` main.
- `fightpass-report` → `report_cancellation_log.py` main.
