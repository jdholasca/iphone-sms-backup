import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import iphone_sms_exporter
from iphone_sms_exporter import apple_datetime, csv_text, decode_attributed_body, export_csv


class ExporterTest(unittest.TestCase):
    def test_finds_hashed_sms_database_from_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            device = root / "test-udid"
            device.mkdir()
            file_id = "ab" + "1" * 38
            with sqlite3.connect(device / "Manifest.db") as db:
                db.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT)")
                db.execute(
                    "INSERT INTO Files VALUES (?, ?, ?)",
                    (file_id, "HomeDomain", "Library/SMS/sms.db"),
                )
            payload = device / "ab" / file_id
            payload.parent.mkdir()
            payload.touch()
            with patch.object(iphone_sms_exporter, "BACKUP_ROOT", root), patch.object(
                iphone_sms_exporter, "UDID", "test-udid"
            ):
                self.assertEqual(iphone_sms_exporter.find_sms_database(), payload)

    def test_csv_formula_is_inert(self):
        self.assertEqual(csv_text("=cmd()"), "'=cmd()")
        self.assertEqual(csv_text("normal"), "normal")

    def test_decodes_attributed_body_and_rejects_bad_archives(self):
        archive = SimpleNamespace(contents=[SimpleNamespace(value=SimpleNamespace(value="Test body"))])
        with patch("iphone_sms_exporter.typedstream.unarchive_from_data", return_value=archive):
            self.assertEqual(decode_attributed_body(b"archive"), "Test body")
        with patch("iphone_sms_exporter.typedstream.unarchive_from_data", side_effect=ValueError):
            self.assertEqual(decode_attributed_body(b"bad"), "")

    def test_apple_nanoseconds_and_csv_export(self):
        self.assertEqual(apple_datetime(1_000_000_000), "2032-09-09T05:46:40+04:00")
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            database = root / "sms.db"
            with sqlite3.connect(database) as db:
                db.executescript(
                    """
                    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                    CREATE TABLE message (
                        ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, date INTEGER,
                        is_from_me INTEGER, handle_id INTEGER, service TEXT, subject TEXT
                    );
                    INSERT INTO handle VALUES (1, '+971500000000');
                    INSERT INTO message VALUES (7, 'test-guid', 'Test body', 0, 0, 1, 'SMS', NULL);
                    """
                )
            destination = root / "messages.csv"
            self.assertEqual(export_csv(database, destination), 1)
            with destination.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["contact"], "'+971500000000")
            self.assertEqual(rows[0]["message"], "Test body")
            self.assertEqual(rows[0]["direction"], "received")


if __name__ == "__main__":
    unittest.main()
