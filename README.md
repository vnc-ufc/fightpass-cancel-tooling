Google Play Bulk Cancellation

High-level overview
- Goal: stop recurring billing for all Google Play–billed subscriptions ahead of the shutdown.
- Approach: scripted batch using Google Android Publisher API to validate tokens (`subscriptionsv2.get`) and optionally revoke with prorated refunds (`subscriptionsv2.revoke`) or cancel renewals (`subscriptionsv2.cancel`).
- Safeguards: service account with Manage Orders only, dry-run mode, throttling, retries on transient errors, structured audit logs, resume checkpoints, and post-run reporting.

One-time tool to bulk-cancel or revoke Google Play–billed Fight Pass subscriptions ahead of the sunset.

## Current state
- Script: `scripts/cancel_subscriptions.py`
- Env: Python venv created at `.venv`
- Installed deps: `google-api-python-client`, `google-auth` (and transitive deps)
- Reporting: `scripts/report_cancellation_log.py` to summarize JSONL logs; packaged CLI entry points available.

## Folder layout
- `configs/`: configuration files (no secrets in git)
- `inputs/`: source CSVs (ignored)
- `outputs/`: derived CSVs (ignored)
- `logs/`: JSONL logs (ignored)
- `checkpoints/`: resume checkpoints (ignored)
- `scripts/`: Python entry points
- `secrets/`: service account keys (ignored)

Quick tree:
```text
play_api/
  configs/
  inputs/
  outputs/
  logs/
  checkpoints/
  scripts/
  secrets/
```

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
python scripts/cancel_subscriptions.py \
  --input inputs/tokens.csv \
  --service-account secrets/key.json \
  --package-name com.ufc.brazil.app \
  --mode cancel \
  --delay 0.15 \
  --retries 3
```

Key flags:
- `--mode`: `cancel`, `validate`, `revoke-prorated`.
- `--input`: CSV with `purchaseToken` column (optional `subscriptionId`).
- `--service-account`: JSON key with Manage Orders permission.
- `--package-name`: App package name.
- `--log`: JSONL audit log output (default is timestamped by mode).
- `--delay`: Fixed delay between rows (seconds).
- `--retries`: Retries on 429/500/503 (exponential backoff + jitter).
- `--backoff`/`--jitter`: Tune retry backoff.
- `--max-rows`: Process only first N rows (for tests).
- `--sample-size`: Randomly process N rows (reservoir sampling).
- `--dry-run`: Skip API calls; still parses and logs with status `dry_run`.
- `--token-column`: Column name for purchase tokens (default `purchaseToken`; falls back to `purchase_token`/`token`).
- `--subscription-id-column`: Optional column name for subscription IDs (default `subscriptionId`; falls back to `subscription_id`/`product`).
- `--package-column`/`--product-column`/`--order-id-column`: Column names for those fields (defaults: `package`, `product`, `order_id`).
- `--progress` / `--no-progress`: Show or suppress a progress bar (default on).
- `--config`: Optional JSON file supplying defaults for any flags.
- `--timestamp-logs` / `--no-timestamp-logs`: Timestamped log filenames (default on).
- `--eligible-output`: CSV output path for validation mode (eligible-for-revoke list).
- `--ineligible-output`: CSV output path for validation mode (ineligible list).
- `--log-response`: Include full API response payload in validation logs.
- `--checkpoint`: Track successful tokens so you can resume safely (legacy alias).
- `--checkpoint-success`: Track successful tokens for resume support.
- `--checkpoint-failed`: Track failed tokens for review/retry.

Logging & summary:
- Each row logged to JSONL with timestamp, token, subscriptionId, status, attempts, HTTP status, error type, and message.
- Summary printed at end: processed, success, already_cancelled, failed_transient, failed_permanent, dry_run.

## Notes / next steps
- Confirm the correct package name before running.
- Start with `--dry-run --max-rows <N>` to validate CSV parsing.
- We can extend README as we add features (e.g., CSV validation, progress reporting, resumable runs).

## Config file (optional)
You can supply a JSON config and omit most CLI flags. Example: `configs/config.example.json`.
```json
{
  "input": "inputs/tokens.csv",
  "service_account": "secrets/service-account.json",
  "package_name": "com.ufc.brazil.app",
  "log": null,
  "delay": 0.15,
  "retries": 3,
  "backoff": 0.25,
  "jitter": 0.25,
  "max_rows": null,
  "dry_run": false,
  "progress": true,
  "token_column": "purchaseToken",
  "subscription_id_column": "subscriptionId",
  "package_column": "package",
  "product_column": "product",
  "order_id_column": "order_id",
  "mode": "cancel",
  "timestamp_logs": true,
  "eligible_output": null,
  "ineligible_output": null,
  "log_response": false,
  "checkpoint": null,
  "checkpoint_success": "checkpoints/run_success.txt",
  "checkpoint_failed": "checkpoints/run_failed.txt",
  "sample_size": null
}
```
CLI flags still override config values; required fields must be present via config or CLI.

Validation:
- The script validates that a token column exists before processing (using the configured column plus fallbacks).
- If your CSV uses `token` (example provided) set `--token-column token` or put `"token_column": "token"` in your config.

## Validate + revoke workflow (prorated refunds)
Recommended for the “revoke + prorated refund” path:
1) Validation pass to confirm tokens are valid and build the eligible list.
```bash
python scripts/cancel_subscriptions.py \
  --config configs/config.json \
  --mode validate \
  --eligible-output outputs/eligible_for_revoke.csv \
  --ineligible-output outputs/ineligible_for_revoke.csv \
  --log-response \
  --checkpoint-success checkpoints/validate_success.txt \
  --checkpoint-failed checkpoints/validate_failed.txt
```
2) Revoke + prorated refund using the eligible list:
```bash
python scripts/cancel_subscriptions.py \
  --config configs/config.json \
  --mode revoke-prorated \
  --input outputs/eligible_for_revoke.csv \
  --token-column token \
  --package-column package \
  --product-column product \
  --order-id-column order_id \
  --checkpoint-success checkpoints/revoke_success.txt \
  --checkpoint-failed checkpoints/revoke_failed.txt
```
Notes:
- `revoke-prorated` immediately ends access and issues a prorated refund.
- Use `--sample-size 10` and `--dry-run` for a small test cohort.

## Test workflow (small cohort)
Run a dry-run validation on a small random sample, then a live revoke on the eligible output.

1) Validation dry-run on 10–20 tokens (sampled):
```bash
python scripts/cancel_subscriptions.py \
  --config configs/config.validate.test.json
```

2) Revoke dry-run on the eligible output:
```bash
python scripts/cancel_subscriptions.py \
  --config configs/config.revoke.test.json
```

3) Live revoke on the same small cohort (optional):
```bash
python scripts/cancel_subscriptions.py \
  --config configs/config.revoke.test.json \
  --dry-run false
```

Notes:
- For live tests, consider `--sample-size 5` and manual Play Console verification.

CLI equivalent (if installed via `pip install -e .`):
```bash
fightpass-cancel --config configs/config.validate.test.json
fightpass-cancel --config configs/config.revoke.test.json
fightpass-cancel --config configs/config.revoke.test.json --dry-run false
```

## Reporting (summaries, CSV exports)
After a run, summarize the JSONL log and export failures/all rows to CSV:
```bash
source .venv/bin/activate
python scripts/report_cancellation_log.py \
  --log logs/cancellation_log.jsonl \
  --failures-csv outputs/failures.csv \
  --all-csv outputs/all_rows.csv
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
- `fightpass-cancel` → `scripts/cancel_subscriptions.py` main.
- `fightpass-report` → `scripts/report_cancellation_log.py` main.
