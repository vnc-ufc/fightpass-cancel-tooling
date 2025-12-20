"""Bulk-cancel Google Play subscriptions for UFC Fight Pass sunset.

Reads a CSV of purchase tokens and invokes purchases.subscriptionsv2.cancel
with developer-requested stop payments. Writes a JSONL audit log and prints a
summary.

This script is intentionally verbose with comments for clarity (aimed at a
technical but non-developer operator).

Usage example:
    python scripts/cancel_subscriptions.py \
        --input inputs/tokens.csv \
        --service-account secrets/key.json \
        --package-name com.ufc.brazil.app \
        --mode cancel \
        --delay 0.15 \
        --retries 3
"""
from __future__ import annotations

import argparse  # Command-line argument parsing
import csv  # CSV input parsing
import json  # Config and JSONL logging
import os
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
ELIGIBLE_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    "SUBSCRIPTION_STATE_ON_HOLD",
    "SUBSCRIPTION_STATE_PAUSED",
}
DEFAULTS: Dict[str, Any] = {
    "config": None,
    "input": None,
    "token_column": "purchaseToken",
    "subscription_id_column": "subscriptionId",
    "package_column": "package",
    "product_column": "product",
    "order_id_column": "order_id",
    "service_account": None,
    "package_name": None,
    "log": None,
    "delay": 0.15,
    "retries": 3,
    "backoff": 0.25,
    "jitter": 0.25,
    "max_rows": None,
    "dry_run": False,
    "progress": True,
    "timestamp_logs": True,
    "mode": "cancel",
    "eligible_output": None,
    "ineligible_output": None,
    "log_response": False,
    "checkpoint": None,
    "checkpoint_success": None,
    "checkpoint_failed": None,
    "sample_size": None,
}


@dataclass
class CancelResult:
    """Structured result for one cancellation attempt."""
    status: str
    attempts: int
    http_status: Optional[int] = None
    message: Optional[str] = None
    error_type: Optional[str] = None


@dataclass
class GetResult:
    """Structured result for one validation attempt."""
    status: str
    attempts: int
    http_status: Optional[int] = None
    message: Optional[str] = None
    error_type: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


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
    body = {
        "cancellationContext": {
            "cancellationType": "DEVELOPER_REQUESTED_STOP_PAYMENTS"
        }
    }

    for attempt in range(1, retries + 2):
        try:
            service.purchases().subscriptionsv2().cancel(
                packageName=package_name, token=token, body=body
            ).execute()
            return CancelResult(status="success", attempts=attempt, http_status=204)
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


def get_with_retries(
    service,
    package_name: str,
    token: str,
    retries: int,
    base_backoff: float,
    jitter: float,
) -> GetResult:
    """Fetch subscription details with retries on transient errors."""
    for attempt in range(1, retries + 2):
        try:
            payload = service.purchases().subscriptionsv2().get(
                packageName=package_name, token=token
            ).execute()
            return GetResult(
                status="success",
                attempts=attempt,
                http_status=200,
                payload=payload,
            )
        except HttpError as err:
            status, message = parse_http_error(err)
            if status in RETRY_STATUS and attempt <= retries:
                delay = base_backoff * (2 ** (attempt - 1))
                delay = delay + random.uniform(0, jitter)
                time.sleep(delay)
                continue
            error_type = classify_error(message)
            return GetResult(
                status="failure",
                attempts=attempt,
                http_status=status,
                message=message,
                error_type=error_type,
            )
        except Exception as err:
            return GetResult(
                status="failure",
                attempts=attempt,
                http_status=None,
                message=str(err),
                error_type="exception",
            )
    return GetResult(status="failure", attempts=retries + 1, error_type="unknown")


def revoke_prorated_with_retries(
    service,
    package_name: str,
    token: str,
    retries: int,
    base_backoff: float,
    jitter: float,
) -> CancelResult:
    """Revoke and issue a prorated refund, with retries on transient errors."""
    body = {"revocationContext": {"proratedRefund": {}}}
    for attempt in range(1, retries + 2):
        try:
            service.purchases().subscriptionsv2().revoke(
                packageName=package_name, token=token, body=body
            ).execute()
            return CancelResult(status="success", attempts=attempt, http_status=204)
        except HttpError as err:
            status, message = parse_http_error(err)
            if status in RETRY_STATUS and attempt <= retries:
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
        except Exception as err:
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


def load_checkpoint(path: Optional[str]) -> set[str]:
    """Load processed tokens to support resume."""
    if not path:
        return set()
    processed = set()
    try:
        with open(path) as fh:
            for line in fh:
                token = line.strip()
                if token:
                    processed.add(token)
    except FileNotFoundError:
        return set()
    return processed


def append_checkpoint(fh, token: str) -> None:
    """Record a processed token to the checkpoint file."""
    fh.write(token + "\n")
    fh.flush()


def build_log_path(
    mode: str, log_path: Optional[str], timestamp_logs: bool, stamp: Optional[str]
) -> str:
    """Default log path with optional timestamp."""
    if log_path:
        return log_path
    base = f"{mode}_log"
    if timestamp_logs and stamp:
        return os.path.join("logs", stamp, f"{base}_{stamp}.jsonl")
    if timestamp_logs:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return os.path.join("logs", stamp, f"{base}_{stamp}.jsonl")
    return os.path.join("logs", f"{base}.jsonl")


def append_timestamp(path: str, stamp: Optional[str] = None) -> str:
    """Append UTC timestamp before the file extension."""
    if not stamp:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(path)
    return f"{base}_{stamp}{ext}" if ext else f"{path}_{stamp}"


def apply_stamp_dir(path: str, stamp: Optional[str], base_dir: str) -> str:
    """Place path under base_dir/<stamp>/ when the path starts with base_dir."""
    if not stamp:
        return path
    parts = path.split(os.sep)
    if parts and parts[0] == base_dir:
        rest = os.path.join(*parts[1:]) if len(parts) > 1 else os.path.basename(path)
        return os.path.join(base_dir, stamp, rest)
    return path


def ensure_parent_dir(path: str) -> None:
    """Create parent directories if needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def pick_package(row: Dict[str, Any], package_field: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """Resolve package name from row or fallback."""
    if package_field and row.get(package_field):
        return str(row.get(package_field)).strip()
    return fallback


def pick_field(row: Dict[str, Any], field: Optional[str]) -> Optional[str]:
    if not field:
        return None
    value = row.get(field)
    if value is None:
        return None
    return str(value).strip()


def sample_rows(path: str, sample_size: int) -> list[Dict[str, Any]]:
    """Reservoir-sample rows from a CSV without loading the entire file."""
    if sample_size <= 0:
        return []
    reservoir: list[Dict[str, Any]] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=1):
            if i <= sample_size:
                reservoir.append(row)
            else:
                j = random.randint(1, i)
                if j <= sample_size:
                    reservoir[j - 1] = row
    return reservoir


def apply_config(args: argparse.Namespace, cfg: Optional[Dict[str, Any]]) -> argparse.Namespace:
    """Merge config values into argparse Namespace; CLI flags win."""
    if not cfg:
        return args
    for key, value in cfg.items():
        if value is None:
            continue
        if hasattr(args, key):
            current = getattr(args, key)
            default = DEFAULTS.get(key, None)
            # Only override if the current value is still at its default (i.e., not set via CLI).
            if current != default:
                continue
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
        for name in ("input", "service_account")
        if getattr(args, name) in (None, "")
    ]
    if missing:
        raise ValueError(f"Missing required inputs: {', '.join(missing)}")


def run(args: argparse.Namespace) -> int:
    """Main orchestration: auth, iterate CSV, act, log, and summarize."""
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
        "skipped": 0,
    }

    # Validate headers and pick which column names to use.
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
    validation_required = ["subscription_state"]
    package_candidates = [
        args.package_column,
        "package",
    ]
    product_candidates = [
        args.product_column,
        "product",
    ]
    order_id_candidates = [
        args.order_id_column,
        "order_id",
        "orderId",
    ]
    try:
        extra_required = (
            validation_required if args.mode == "revoke-prorated" else None
        )
        fieldnames, token_field, subscription_field = validate_headers(
            args.input, token_candidates, subscription_candidates, extra_required
        )
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2

    package_field = choose_field(fieldnames, package_candidates)
    product_field = choose_field(fieldnames, product_candidates)
    order_id_field = choose_field(fieldnames, order_id_candidates)

    if not args.package_name and not package_field:
        sys.stderr.write(
            "No package name provided and no package column found in CSV.\n"
        )
        return 2

    run_stamp = (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if args.timestamp_logs
        else None
    )
    log_path = build_log_path(args.mode, args.log, args.timestamp_logs, run_stamp)
    ensure_parent_dir(log_path)
    success_checkpoint = args.checkpoint_success or args.checkpoint
    failed_checkpoint = args.checkpoint_failed
    processed_tokens = load_checkpoint(success_checkpoint)
    if success_checkpoint:
        ensure_parent_dir(success_checkpoint)
    if failed_checkpoint:
        ensure_parent_dir(failed_checkpoint)
    checkpoint_success_fh = open(success_checkpoint, "a") if success_checkpoint else None
    checkpoint_failed_fh = open(failed_checkpoint, "a") if failed_checkpoint else None

    if args.sample_size:
        rows = sample_rows(args.input, args.sample_size)
        if args.max_rows:
            rows = rows[: args.max_rows]
        total_rows = len(rows)
    else:
        rows = load_rows(args.input)
        total_rows = count_rows(args.input) if args.progress else None
        if args.progress and args.max_rows and total_rows is not None:
            total_rows = min(total_rows, args.max_rows)

    progress = tqdm(total=total_rows, unit="row", desc=args.mode, disable=not args.progress)

    eligible_fh = None
    eligible_writer = None
    ineligible_fh = None
    ineligible_writer = None
    if args.mode == "validate":
        eligible_path = args.eligible_output
        if not eligible_path:
            eligible_path = os.path.join(
                "outputs", f"eligible_for_revoke_{run_stamp}.csv"
            )
        else:
            eligible_path = append_timestamp(eligible_path, run_stamp)
        eligible_path = apply_stamp_dir(eligible_path, run_stamp, "outputs")
        ensure_parent_dir(eligible_path)
        eligible_fh = open(eligible_path, "w", newline="")
        eligible_writer = csv.DictWriter(
            eligible_fh,
            fieldnames=[
                "token",
                "package",
                "product",
                "order_id",
                "subscription_state",
                "expiry_time",
                "auto_renew_enabled",
                "latest_order_id",
            ],
        )
        eligible_writer.writeheader()

        ineligible_path = args.ineligible_output
        if not ineligible_path:
            ineligible_path = os.path.join(
                "outputs", f"ineligible_for_revoke_{run_stamp}.csv"
            )
        else:
            ineligible_path = append_timestamp(ineligible_path, run_stamp)
        ineligible_path = apply_stamp_dir(ineligible_path, run_stamp, "outputs")
        ensure_parent_dir(ineligible_path)
        ineligible_fh = open(ineligible_path, "w", newline="")
        ineligible_writer = csv.DictWriter(
            ineligible_fh,
            fieldnames=[
                "token",
                "package",
                "product",
                "order_id",
                "subscription_state",
                "expiry_time",
                "auto_renew_enabled",
                "latest_order_id",
                "status",
                "http_status",
                "error_type",
                "message",
            ],
        )
        ineligible_writer.writeheader()

    with open(log_path, "w") as log_fh:
        for idx, row in enumerate(rows, start=1):
            if args.max_rows and totals["processed"] >= args.max_rows:
                break

            token = (row.get(token_field) or "").strip()
            if not token:
                sys.stderr.write(f"Row {idx}: missing token, skipping\n")
                totals["skipped"] += 1
                continue

            if token in processed_tokens:
                totals["skipped"] += 1
                continue

            package_name = pick_package(row, package_field, args.package_name)
            if not package_name:
                sys.stderr.write(f"Row {idx}: missing package, skipping\n")
                totals["skipped"] += 1
                continue

            if args.package_name and package_name != args.package_name:
                sys.stderr.write(
                    f"Row {idx}: package mismatch ({package_name}), skipping\n"
                )
                totals["skipped"] += 1
                continue

            product = pick_field(row, product_field)
            order_id = pick_field(row, order_id_field)
            subscription_id = (
                (row.get(subscription_field) or "").strip() if subscription_field else None
            )

            totals["processed"] += 1
            if args.mode == "validate":
                if args.dry_run:
                    get_result = GetResult(status="dry_run", attempts=0)
                else:
                    get_result = get_with_retries(
                        service=service,
                        package_name=package_name,
                        token=token,
                        retries=args.retries,
                        base_backoff=args.backoff,
                        jitter=args.jitter,
                    )
                result = None
            elif args.dry_run:
                result = CancelResult(status="dry_run", attempts=0)
                get_result = None
            elif args.mode == "revoke-prorated":
                get_result = None
                result = revoke_prorated_with_retries(
                    service=service,
                    package_name=package_name,
                    token=token,
                    retries=args.retries,
                    base_backoff=args.backoff,
                    jitter=args.jitter,
                )
            else:
                get_result = None
                result = cancel_with_retries(
                    service=service,
                    package_name=package_name,
                    token=token,
                    retries=args.retries,
                    base_backoff=args.backoff,
                    jitter=args.jitter,
                )

            if args.mode == "validate" and get_result:
                if get_result.status == "success":
                    totals["success"] += 1
                elif get_result.status == "dry_run":
                    totals["dry_run"] += 1
                elif get_result.http_status in RETRY_STATUS:
                    totals["failed_transient"] += 1
                else:
                    totals["failed_permanent"] += 1

                payload = get_result.payload or {}
                subscription_state = payload.get("subscriptionState")
                line_items = payload.get("lineItems") or []
                expiry_time = None
                auto_renew_enabled = None
                latest_order_id = payload.get("latestOrderId")
                if line_items:
                    expiry_time = line_items[0].get("expiryTime")
                    auto_plan = line_items[0].get("autoRenewingPlan") or {}
                    auto_renew_enabled = auto_plan.get("autoRenewEnabled")
                    if line_items[0].get("latestSuccessfulOrderId"):
                        latest_order_id = line_items[0].get("latestSuccessfulOrderId")

                eligible = subscription_state in ELIGIBLE_STATES
                if eligible_writer and eligible:
                    eligible_writer.writerow(
                        {
                            "token": token,
                            "package": package_name,
                            "product": product,
                            "order_id": order_id,
                            "subscription_state": subscription_state,
                            "expiry_time": expiry_time,
                            "auto_renew_enabled": auto_renew_enabled,
                            "latest_order_id": latest_order_id,
                        }
                    )
                elif ineligible_writer:
                    ineligible_writer.writerow(
                        {
                            "token": token,
                            "package": package_name,
                            "product": product,
                            "order_id": order_id,
                            "subscription_state": subscription_state,
                            "expiry_time": expiry_time,
                            "auto_renew_enabled": auto_renew_enabled,
                            "latest_order_id": latest_order_id,
                            "status": get_result.status,
                            "http_status": get_result.http_status,
                            "error_type": get_result.error_type,
                            "message": get_result.message,
                        }
                    )

                record = {
                    "timestamp": now_iso(),
                    "purchaseToken": token,
                    "subscriptionId": subscription_id,
                    "package": package_name,
                    "product": product,
                    "order_id": order_id,
                    "status": get_result.status,
                    "attempts": get_result.attempts,
                    "httpStatus": get_result.http_status,
                    "errorType": get_result.error_type,
                    "message": get_result.message,
                    "subscriptionState": subscription_state,
                    "expiryTime": expiry_time,
                    "autoRenewEnabled": auto_renew_enabled,
                    "latestOrderId": latest_order_id,
                    "eligibleForRevoke": eligible,
                    "rowIndex": idx,
                }
                if args.log_response and get_result.payload is not None:
                    record["response"] = get_result.payload
                log_record(log_fh, record)
            else:
                if result is None:
                    result = CancelResult(status="dry_run", attempts=0)

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
                    "package": package_name,
                    "product": product,
                    "order_id": order_id,
                    "status": result.status,
                    "attempts": result.attempts,
                    "httpStatus": result.http_status,
                    "errorType": result.error_type,
                    "message": result.message,
                    "rowIndex": idx,
                }
                log_record(log_fh, record)

            if not args.dry_run:
                success = (
                    (args.mode == "validate" and get_result and get_result.status == "success")
                    or (args.mode != "validate" and result and result.status == "success")
                )
                if success and checkpoint_success_fh:
                    append_checkpoint(checkpoint_success_fh, token)
                    processed_tokens.add(token)
                elif not success and checkpoint_failed_fh:
                    append_checkpoint(checkpoint_failed_fh, token)

            if args.delay > 0:
                time.sleep(args.delay)
            progress.update(1)

    if eligible_fh:
        eligible_fh.close()
    if ineligible_fh:
        ineligible_fh.close()
    if checkpoint_success_fh:
        checkpoint_success_fh.close()
    if checkpoint_failed_fh:
        checkpoint_failed_fh.close()

    progress.close()
    print(f"---- {args.mode} summary ----")
    for key, value in totals.items():
        print(f"{key}: {value}")
    print(f"log: {log_path}")
    if args.mode == "validate" and eligible_writer:
        print(f"eligible_output: {eligible_path}")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk cancel/revoke or validate Google Play subscriptions by purchase token."
    )
    parser.add_argument(
        "--config",
        help="Path to JSON config file supplying defaults for any CLI flags.",
    )
    parser.add_argument(
        "--mode",
        choices=["cancel", "validate", "revoke-prorated"],
        default="cancel",
        help="Operation mode (default: cancel).",
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
        "--package-column",
        default="package",
        help="Column name for package (default: package).",
    )
    parser.add_argument(
        "--product-column",
        default="product",
        help="Column name for product ID (default: product).",
    )
    parser.add_argument(
        "--order-id-column",
        default="order_id",
        help="Column name for order id (default: order_id).",
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
        help="Output JSONL audit log path (default is timestamped by mode).",
    )
    parser.add_argument(
        "--timestamp-logs",
        dest="timestamp_logs",
        action="store_true",
        default=True,
        help="Use timestamped log filenames by default (default: on).",
    )
    parser.add_argument(
        "--no-timestamp-logs",
        dest="timestamp_logs",
        action="store_false",
        help="Disable timestamped log filenames.",
    )
    parser.add_argument(
        "--eligible-output",
        help="Path to CSV output for eligible-for-revoke list (validate mode).",
    )
    parser.add_argument(
        "--ineligible-output",
        help="Path to CSV output for ineligible rows (validate mode).",
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
        "--sample-size",
        type=int,
        help="Randomly process N rows (reservoir sampling).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip API calls; still parse and log rows as dry_run.",
    )
    parser.add_argument(
        "--log-response",
        action="store_true",
        help="Include full API response payload in validation logs.",
    )
    parser.add_argument(
        "--checkpoint",
        help="File path to track processed tokens for resume support (success only).",
    )
    parser.add_argument(
        "--checkpoint-success",
        help="File path to track successful tokens for resume support.",
    )
    parser.add_argument(
        "--checkpoint-failed",
        help="File path to track failed tokens for review/retry.",
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
