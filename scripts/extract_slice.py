"""
scripts/extract_slice.py — Extract a coherent slice from the Kaggle Enron CSV.

The Kaggle dataset (wcukierski/enron-email-dataset) is a single CSV:
  columns: file, message
  'file'    — original path, e.g. "maildir/skilling-j/inbox/1."
  'message' — raw RFC 2822 email text

Usage (PowerShell):
    python scripts/extract_slice.py `
      --csv data/raw/enron/emails.csv `
      --mailboxes skilling-j lay-k dasovich-j `
      --start 2000-10-01 `
      --end 2001-03-31 `
      --min-thread-size 3 `
      --output data/slice/

Also handles legacy maildir layout if --maildir is provided instead of --csv.
"""

from __future__ import annotations
import argparse
import csv
import email
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


# ──────────────────────────────────────────────
# Subject normalisation
# ──────────────────────────────────────────────

def _norm_subject(s: str) -> str:
    import re
    s = re.sub(r"^(re|fwd?|aw)[\s:\[]+", "", s.strip(), flags=re.IGNORECASE)
    return s.lower().strip()


# ──────────────────────────────────────────────
# Core extraction (shared between CSV and maildir modes)
# ──────────────────────────────────────────────

def _process_message(
    raw_bytes: bytes,
    mailbox_name: str,
    start_dt: datetime,
    end_dt: datetime,
    require_attachments: bool,
    thread_buckets: dict,
):
    """Parse one raw email and add it to thread_buckets if it passes filters."""
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return

    # Date filter
    date_str = msg.get("Date", "")
    if not date_str:
        return
    try:
        msg_dt = parsedate_to_datetime(date_str)
        if msg_dt.tzinfo is None:
            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return

    if not (start_dt <= msg_dt <= end_dt):
        return

    # Attachment filter
    has_attachment = any(part.get_filename() for part in msg.walk())
    if require_attachments and not has_attachment:
        return

    thread_key = _norm_subject(msg.get("Subject", ""))
    thread_buckets[thread_key].append((raw_bytes, msg, mailbox_name))


def _write_slice(
    thread_buckets: dict,
    min_thread_size: int,
    output_dir: Path,
) -> tuple[int, int]:
    """Write qualifying threads to output_dir. Returns (emails_written, threads_written)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "attachments").mkdir(exist_ok=True)

    emails_written = 0
    threads_written = 0
    seen_ids: set[str] = set()

    for thread_key, entries in thread_buckets.items():
        if len(entries) < min_thread_size:
            continue
        threads_written += 1

        for raw_bytes, msg, _mailbox in entries:
            msg_id = msg.get("Message-ID", "").strip("<>").strip()
            if not msg_id:
                msg_id = f"unknown_{emails_written}"
            safe_id = msg_id.replace("/", "_").replace(":", "_").replace(" ", "_")

            # Deduplicate — same message may appear in multiple mailboxes
            if safe_id in seen_ids:
                continue
            seen_ids.add(safe_id)

            # Write .eml
            eml_path = output_dir / f"{safe_id}.eml"
            eml_path.write_bytes(raw_bytes)

            # Extract attachments
            att_dir = output_dir / "attachments" / safe_id
            att_dir.mkdir(parents=True, exist_ok=True)
            for part in msg.walk():
                fname = part.get_filename()
                if fname:
                    payload = part.get_payload(decode=True)
                    if payload:
                        # Sanitise filename
                        safe_fname = fname.replace("/", "_").replace("\\", "_")
                        (att_dir / safe_fname).write_bytes(payload)

            emails_written += 1

    return emails_written, threads_written


# ──────────────────────────────────────────────
# CSV mode (Kaggle download)
# ──────────────────────────────────────────────

def extract_from_csv(
    csv_path: Path,
    mailboxes: list[str],
    start: str,
    end: str,
    min_thread_size: int,
    require_attachments: bool,
    output_dir: Path,
):
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)

    thread_buckets: dict[str, list] = defaultdict(list)
    total_rows = 0
    skipped_mailbox = 0

    # Raise the per-field limit — Enron email bodies exceed the 131 KB default
    # sys.maxsize overflows on Windows; 10 MB is more than enough for any email body
    csv.field_size_limit(10 * 1024 * 1024)

    print(f"Reading CSV: {csv_path}  (this may take a minute — file is ~1.4 GB)")
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rows += 1
            if total_rows % 50_000 == 0:
                print(f"  … {total_rows:,} rows read, "
                      f"{sum(len(v) for v in thread_buckets.values()):,} candidates so far")

            file_path = row.get("file", "")
            # file_path looks like "skilling-j/inbox/1." or "maildir/skilling-j/inbox/1."
            # Strip optional leading "maildir/" prefix then take the first component.
            parts = file_path.replace("\\", "/").lstrip("/").split("/")
            if parts[0].lower() == "maildir" and len(parts) > 1:
                parts = parts[1:]
            if not parts:
                continue
            mailbox_name = parts[0]  # e.g. "skilling-j"

            if mailbox_name not in mailboxes:
                skipped_mailbox += 1
                continue

            raw_text = row.get("message", "")
            if not raw_text:
                continue

            _process_message(
                raw_bytes=raw_text.encode("utf-8", errors="replace"),
                mailbox_name=mailbox_name,
                start_dt=start_dt,
                end_dt=end_dt,
                require_attachments=require_attachments,
                thread_buckets=thread_buckets,
            )

    print(f"CSV scan complete: {total_rows:,} rows, "
          f"{skipped_mailbox:,} skipped (wrong mailbox), "
          f"{sum(len(v) for v in thread_buckets.values()):,} date/attachment-passing emails")

    emails_written, threads_written = _write_slice(
        thread_buckets, min_thread_size, output_dir
    )

    print(f"\nDone: Extracted {emails_written} emails across {threads_written} threads -> {output_dir}")
    print(f"  (threads with < {min_thread_size} messages were skipped)")


# ──────────────────────────────────────────────
# Maildir mode (legacy raw directory layout)
# ──────────────────────────────────────────────

def extract_from_maildir(
    raw_dir: Path,
    mailboxes: list[str],
    start: str,
    end: str,
    min_thread_size: int,
    require_attachments: bool,
    output_dir: Path,
):
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)

    thread_buckets: dict[str, list] = defaultdict(list)

    for mailbox in mailboxes:
        mb_dir = raw_dir / mailbox
        if not mb_dir.exists():
            print(f"[WARN] Mailbox dir not found: {mb_dir}")
            continue
        for eml_path in mb_dir.rglob("*"):
            if not eml_path.is_file():
                continue
            try:
                raw_bytes = eml_path.read_bytes()
            except Exception:
                continue
            _process_message(
                raw_bytes=raw_bytes,
                mailbox_name=mailbox,
                start_dt=start_dt,
                end_dt=end_dt,
                require_attachments=require_attachments,
                thread_buckets=thread_buckets,
            )

    emails_written, threads_written = _write_slice(
        thread_buckets, min_thread_size, output_dir
    )
    print(f"Done: Extracted {emails_written} emails across {threads_written} threads -> {output_dir}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Extract an Enron email slice for indexing."
    )
    # Source — one of csv or maildir
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv",     type=Path, help="Path to Kaggle emails.csv")
    src.add_argument("--maildir", type=Path, help="Path to raw maildir/ root")

    p.add_argument("--mailboxes", nargs="+",
                   default=["skilling-j", "lay-k", "dasovich-j"])
    p.add_argument("--start",  default="2000-10-01")
    p.add_argument("--end",    default="2001-03-31")
    p.add_argument("--min-thread-size", type=int, default=3)
    p.add_argument("--require-attachments", action="store_true")
    p.add_argument("--output", type=Path, default=Path("data/slice/"))
    args = p.parse_args()

    if args.csv:
        if not args.csv.exists():
            print(f"ERROR: CSV not found: {args.csv}")
            sys.exit(1)
        extract_from_csv(
            csv_path=args.csv,
            mailboxes=args.mailboxes,
            start=args.start,
            end=args.end,
            min_thread_size=args.min_thread_size,
            require_attachments=args.require_attachments,
            output_dir=args.output,
        )
    else:
        extract_from_maildir(
            raw_dir=args.maildir,
            mailboxes=args.mailboxes,
            start=args.start,
            end=args.end,
            min_thread_size=args.min_thread_size,
            require_attachments=args.require_attachments,
            output_dir=args.output,
        )
