"""Summarize cancellation JSONL logs and optionally export CSVs.

Intended for operators reviewing outcomes of the bulk cancellation run.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from typing import Dict, List, Optional


FIELDS = [
    "timestamp",
    "purchaseToken",
    "subscriptionId",
    "status",
    "attempts",
    "httpStatus",
    "errorType",
    "message",
    "rowIndex",
]


def load_records(path: str):
    with open(path) as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"Skipping malformed JSON on line {line_no}: {exc}\n")
                continue


def summarize(records):
    status_counts = Counter()
    error_counts = Counter()
    http_counts = Counter()
    all_rows: List[Dict] = []
    for rec in records:
        status = rec.get("status", "unknown")
        status_counts[status] += 1
        if status == "failure":
            error_counts[rec.get("errorType", "unknown")] += 1
            http_counts[str(rec.get("httpStatus", "unknown"))] += 1
        all_rows.append(rec)
    return all_rows, status_counts, error_counts, http_counts


def write_csv(path: str, rows, failures_only: bool = False):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            if failures_only and row.get("status") != "failure":
                continue
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def print_summary(status_counts: Counter, error_counts: Counter, http_counts: Counter, total: int):
    print("---- Log summary ----")
    print(f"Total records: {total}")
    for status, count in status_counts.most_common():
        print(f"{status}: {count}")
    if error_counts:
        print("\nFailure breakdown by errorType:")
        for err, count in error_counts.most_common():
            print(f"  {err}: {count}")
    if http_counts:
        print("\nFailure breakdown by httpStatus:")
        for code, count in http_counts.most_common():
            print(f"  {code}: {count}")


def parse_args():
    parser = argparse.ArgumentParser(description="Summarize cancellation JSONL log and export CSVs.")
    parser.add_argument("--log", required=True, help="Path to JSONL log file produced by cancel_subscriptions.py")
    parser.add_argument("--failures-csv", help="Optional path to write failures-only CSV (tokens, errors, etc.)")
    parser.add_argument("--all-csv", help="Optional path to write all rows to CSV.")
    return parser.parse_args()


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args()
    records, status_counts, error_counts, http_counts = summarize(load_records(args.log))
    print_summary(status_counts, error_counts, http_counts, total=len(records))
    if args.failures_csv:
        write_csv(args.failures_csv, records, failures_only=True)
        print(f"Wrote failures CSV: {args.failures_csv}")
    if args.all_csv:
        write_csv(args.all_csv, records, failures_only=False)
        print(f"Wrote full CSV: {args.all_csv}")


if __name__ == "__main__":
    main()
