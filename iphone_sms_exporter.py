#!/usr/bin/env python3
import csv
import os
import shutil
import sqlite3
import sys
import tempfile
import argparse
import typedstream
from datetime import datetime, timedelta, timezone
from pathlib import Path

UDID = os.environ.get("IPHONE_UDID", "")
BACKUP_ROOT = Path(os.environ.get("IPHONE_BACKUP_ROOT", "backups"))
EXPORT_PATH = Path(os.environ.get("IPHONE_SMS_EXPORT", "messages.csv"))
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def csv_text(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def apple_datetime(value: object) -> str:
    if value in (None, "", 0):
        return ""
    seconds = float(value)
    while abs(seconds) > 50_000_000_000:
        seconds /= 1_000
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


def decode_attributed_body(blob: object) -> str:
    if not blob:
        return ""
    try:
        root = typedstream.unarchive_from_data(blob)
        item = root.contents[0]
        value = getattr(item, "value", item)
        text = getattr(value, "value", "")
        return text if isinstance(text, str) else ""
    except (AttributeError, IndexError, OSError, TypeError, ValueError):
        return ""


def find_sms_database(backup_root: Path | None = None, udid: str | None = None) -> Path:
    backup_root = BACKUP_ROOT if backup_root is None else backup_root
    udid = UDID if udid is None else udid
    device_dir = backup_root / udid
    manifest = device_dir / "Manifest.db"
    if not manifest.is_file():
        raise RuntimeError("completed backup manifest was not found")
    with sqlite3.connect(f"file:{manifest}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ? LIMIT 1",
            ("HomeDomain", "Library/SMS/sms.db"),
        ).fetchone()
    if row is None:
        raise RuntimeError("SMS database is missing from the backup")
    file_id = row[0]
    candidates = (device_dir / file_id[:2] / file_id, device_dir / file_id)
    try:
        return next(path for path in candidates if path.is_file())
    except StopIteration as error:
        raise RuntimeError("SMS database payload is missing from the backup") from error


def export_csv(source: Path, destination: Path = EXPORT_PATH) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary_dir:
        database_copy = Path(temporary_dir) / "sms.db"
        shutil.copy2(source, database_copy)
        with sqlite3.connect(f"file:{database_copy}?mode=ro", uri=True) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(message)")}
            required = {"ROWID", "guid", "text", "date", "is_from_me", "handle_id"}
            if not required.issubset(columns):
                raise RuntimeError(f"unsupported SMS database schema; missing {sorted(required - columns)}")
            service = "m.service" if "service" in columns else "''"
            subject = "m.subject" if "subject" in columns else "''"
            attributed_body = "m.attributedBody" if "attributedBody" in columns else "NULL"
            rows = db.execute(
                f"""
                SELECT m.ROWID, m.guid, m.date, m.is_from_me,
                       COALESCE(h.id, ''), {service}, COALESCE({subject}, ''), COALESCE(m.text, ''),
                       {attributed_body}
                  FROM message AS m
             LEFT JOIN handle AS h ON h.ROWID = m.handle_id
              ORDER BY m.date, m.ROWID
                """
            )
            temporary_csv = Path(temporary_dir) / "messages.csv"
            count = 0
            with temporary_csv.open("w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(("id", "guid", "date", "direction", "contact", "service", "subject", "message"))
                for row_id, guid, date, is_from_me, contact, row_service, row_subject, text, attributed in rows:
                    text = text or decode_attributed_body(attributed)
                    writer.writerow(
                        tuple(csv_text(value) for value in
                              (row_id, guid, apple_datetime(date), "sent" if is_from_me else "received", contact,
                               row_service, row_subject, text))
                    )
                    count += 1
            os.chmod(temporary_csv, 0o600)
            os.replace(temporary_csv, destination)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Export messages from an iPhone backup")
    parser.add_argument("--backup-root", type=Path, default=BACKUP_ROOT)
    parser.add_argument("--udid", default=UDID)
    parser.add_argument("--output", type=Path, default=EXPORT_PATH)
    args = parser.parse_args()
    try:
        count = export_csv(find_sms_database(args.backup_root, args.udid), args.output)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    print(f"Exported {count} messages to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
