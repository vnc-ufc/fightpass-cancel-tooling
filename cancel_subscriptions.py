"""Bulk-cancel Google Play subscriptions for UFC Fight Pass sunset.

Reads a CSV of purchase tokens and invokes purchases.subscriptionsv2.cancel
with developer-requested stop payments. Writes a JSONL audit log and prints a
summary.

This script is intentionally verbose with comments for clarity (aimed at a
technical but non-developer operator).

Usage example:
    python cancel_subscriptions.py \
        --input tokens.csv \
        --service-account key.json \
        --package-name com.ufc.brazil.app \
        --log cancellation_log.jsonl \
        --delay 0.15 \
        --retries 3
"""
from __future__ import annotations

import argparse  # Command-line argument parsing
import csv  # CSV input parsing
import json  # Config and JSONL logging
import random  # Jitter for backoff
import sys
import time  # Throttling between calls
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from google.oauth2 import service_account  # Auth with service account key
from googleapiclient.discovery import build  # Builds the Android Publisher client
from googleapiclient.errors import HttpError  # HTTP errors from Google API
from tqdm import tqdm  # Progress bars


SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]
RETRY_STATUS = {429, 500, 503}


@dataclass
class CancelResult:
    """Structured result for one cancellation attempt."""
    status: str
    attempts: int
    http_status: Optional[int] = None
    message: Optional[str] = None
    error_type: Optional[str] = None


def build_service(service_account_path: str):
    """Create the Android Publisher API client using a service account key."""
    credentials = service_account.Credentials.from_service_account_file(
        service_account_path, scopes=SCOPES
    )
    return build(
        "androidpublisher", "v3", credentials=credentials, cache_discovery=False
    )


def parse_http_error(err: HttpError) -> tuple[Optional[int], str]:
    """Extract status code and message from HttpError, with safe fallbacks."""
    status = getattr(err.resp, "status", None)
    try:
        payload = json.loads(err.content.decode("utf-8"))
        message = payload.get("error", {}).get("message") or str(err)
    except Exception:
        message = str(err)
    return status, message


def classify_error(message: str) -> str:
    """Lightweight classifier to separate common failure types for reporting."""
    lowered = message.lower()
    if "already" in lowered and "cancel" in lowered:
        return "already_cancelled"
    if "not found" in lowered:
        return "not_found"
    if "permission" in lowered or "forbidden" in lowered:
        return "permission"
    return "other"


def cancel_with_retries(
    service,
    package_name: str,
    token: str,
    retries: int,
    base_backoff: float,
    jitter: float,
) -> CancelResult:
    """Call the cancellation endpoint with exponential backoff on transient errors."""
    name = f"applications/{package_name}/purchases/subscriptions/{token}"
    body = {
        "cancellationContext": {
            "cancellationType": "DEVELOPER_REQUESTED_STOP_PAYMENTS"
        }
    }

    for attempt in range(1, retries + 2):
        try:
            service.purchases().subscriptionsv2().cancel(
                name=name, body=body
            ).execute()
            return CancelResult(status="success", attempts=attempt, http_status=200)
        except HttpError as err:
            status, message = parse_http_error(err)
            if status in RETRY_STATUS and attempt <= retries:
                # Exponential backoff with jitter for 429/5xx
                delay = base_backoff * (2 ** (attempt - 1))
                delay = delay + random.uniform(0, jitter)
                time.sleep(delay)
                continue
            error_type = classify_error(message)
            return CancelResult(
                status="failure",
                attempts=attempt,
                http_status=status,
                message=message,
                error_type=error_type,
            )
        except Exception as err:  # Catch-all to avoid halting the batch
            return CancelResult(
                status="failure",
                attempts=attempt,
                http_status=None,
                message=str(err),
                error_type="exception",
            )
    return CancelResult(status="failure", attempts=retries + 1, error_type="unknown")


def load_rows(path: str) -> Iterable[Dict[str, Any]]:
    """Stream rows from CSV to keep memory usage small."""
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield row


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_record(fh, record: Dict[str, Any]) -> None:
    """Append a single JSON record to the audit log."""
    fh.write(json.dumps(record, ensure_ascii=True))
    fh.write("\n")
    fh.flush()


def count_rows(path: str) -> int:
    """Count rows in CSV (excludes header)."""
    with open(path, newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def choose_field(fieldnames: Iterable[str], candidates: list[str]) -> Optional[str]:
    """Pick the first matching fieldname from a preference-ordered list."""
    for name in candidates:
        if name and name in fieldnames:
            return name
    return None


def validate_headers(
    path: str,
    token_candidates: list[str],
    subscription_candidates: list[str],
    extra_required: Optional[list[str]] = None,
) -> tuple[list[str], str, Optional[str]]:
    """
    Validate CSV headers and resolve fieldnames for token and subscription id.

    Returns: (fieldnames, token_field, subscription_field or None)
    """
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []

    token_field = choose_field(fieldnames, token_candidates)
    if not token_field:
        raise ValueError(
            "CSV is missing a token column. Tried: " + ", ".join(token_candidates)
        )

    subscription_field = choose_field(fieldnames, subscription_candidates)

    if extra_required:
        missing = [col for col in extra_required if col not in fieldnames]
        if missing:
            raise ValueError(
                f"CSV missing required columns: {', '.join(missing)}. Found: {', '.join(fieldnames)}"
            )

    return fieldnames, token_field, subscription_field


def apply_config(args: argparse.Namespace, cfg: Optional[Dict[str, Any]]) -> argparse.Namespace:
    """Merge config values into argparse Namespace; CLI flags win."""
    if not cfg:
        return args
    for key, value in cfg.items():
        if value is None:
            continue
        if hasattr(args, key):
            setattr(args, key, value)
    return args


def load_config(path: str) -> Dict[str, Any]:
    """Load JSON configuration from file path."""
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object of key/value pairs")
    return data


def validate_required(args: argparse.Namespace) -> None:
    """Ensure mandatory inputs are present (either via config or CLI)."""
    missing = [
        name
        for name in ("input", "service_account", "package_name")
        if getattr(args, name) in (None, "")
    ]
    if missing:
        raise ValueError(f"Missing required inputs: {', '.join(missing)}")


def run(args: argparse.Namespace) -> int:
    """Main orchestration: auth, iterate CSV, cancel, log, and summarize."""
    if args.dry_run:
        service = None
    else:
        try:
            service = build_service(args.service_account)
        except Exception as exc:  # Avoid proceeding if auth fails
            sys.stderr.write(f"Failed to build Android Publisher service: {exc}\n")
            return 1

    totals = {
        "processed": 0,
        "success": 0,
        "already_cancelled": 0,
        "failed_transient": 0,
        "failed_permanent": 0,
        "dry_run": 0,
    }

    # Validate headers and pick which column names to use for tokens/subscriptions.
    token_candidates = [
        args.token_column,
        "purchaseToken",
        "purchase_token",
        "token",
    ]
    subscription_candidates = [
        args.subscription_id_column,
        "subscriptionId",
        "subscription_id",
        "product",
    ]
    try:
        _, token_field, subscription_field = validate_headers(
            args.input, token_candidates, subscription_candidates
        )
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2

    total_rows = count_rows(args.input) if args.progress else None
    if args.progress and args.max_rows and total_rows is not None:
        total_rows = min(total_rows, args.max_rows)
    progress = tqdm(total=total_rows, unit="row", desc="Cancelling", disable=not args.progress)

    with open(args.log, "w") as log_fh:
        for idx, row in enumerate(load_rows(args.input), start=1):
            if args.max_rows and totals["processed"] >= args.max_rows:
                break

            token = (row.get(token_field) or "").strip()
            if not token:
                sys.stderr.write(f"Row {idx}: missing purchaseToken, skipping\n")
                continue

            subscription_id = (row.get(subscription_field) or "").strip() if subscription_field else None

            totals["processed"] += 1
            if args.dry_run:
                result = CancelResult(status="dry_run", attempts=0)
            else:
                result = cancel_with_retries(
                    service=service,
                    package_name=args.package_name,
                    token=token,
                    retries=args.retries,
                    base_backoff=args.backoff,
                    jitter=args.jitter,
                )

            if result.status == "success":
                totals["success"] += 1
            elif result.status == "dry_run":
                totals["dry_run"] += 1
            elif result.error_type == "already_cancelled":
                totals["already_cancelled"] += 1
            elif result.http_status in RETRY_STATUS:
                totals["failed_transient"] += 1
            else:
                totals["failed_permanent"] += 1

            record = {
                "timestamp": now_iso(),
                "purchaseToken": token,
                "subscriptionId": subscription_id,
                "status": result.status,
                "attempts": result.attempts,
                "httpStatus": result.http_status,
                "errorType": result.error_type,
                "message": result.message,
                "rowIndex": idx,
            }
            log_record(log_fh, record)

            if args.delay > 0:
                time.sleep(args.delay)
            progress.update(1)

    progress.close()
    print("---- Bulk cancellation summary ----")
    for key, value in totals.items():
        print(f"{key}: {value}")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk cancel Google Play subscriptions by purchase token."
    )
    parser.add_argument(
        "--config",
        help="Path to JSON config file supplying defaults for any CLI flags.",
    )
    parser.add_argument(
        "--input",
        help="CSV file containing purchaseToken column.",
    )
    parser.add_argument(
        "--token-column",
        default="purchaseToken",
        help="Column name to use for purchase tokens (default: purchaseToken).",
    )
    parser.add_argument(
        "--subscription-id-column",
        default="subscriptionId",
        help="Optional column name for subscription IDs (default: subscriptionId; falls back to subscription_id/product).",
    )
    parser.add_argument(
        "--service-account",
        help="Path to service account JSON key with Manage Orders permission.",
    )
    parser.add_argument(
        "--package-name",
        help="Android package name (e.g., com.ufc.brazil.app).",
    )
    parser.add_argument(
        "--log",
        default="cancellation_log.jsonl",
        help="Output JSONL audit log path.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Fixed delay between processed rows (seconds).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Number of retries for transient HTTP statuses.",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=0.25,
        help="Base exponential backoff (seconds) for retries.",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.25,
        help="Additional random jitter added to retry backoff (seconds).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        help="Process only the first N rows (useful for test runs).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip API calls; still parse and log rows as dry_run.",
    )
    parser.add_argument(
        "--progress",
        dest="progress",
        action="store_true",
        default=True,
        help="Show a progress bar (default: on).",
    )
    parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="Hide the progress bar.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    cfg = load_config(args.config) if args.config else None
    args = apply_config(args, cfg)
    try:
        validate_required(args)
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(2)
    sys.exit(run(args))


if __name__ == "__main__":
    main()
