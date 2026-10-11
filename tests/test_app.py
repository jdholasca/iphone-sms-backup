import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iphone_backup_app import latest_progress, parse_udids


class AppTest(unittest.TestCase):
    def test_reads_latest_whole_backup_progress(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            log = Path(temporary_dir) / "backup.log"
            log.write_bytes(b"10.2/100\r58.75/100\r")
            self.assertEqual(latest_progress(log), 58.75)

    def test_missing_progress_log_is_safe(self):
        self.assertIsNone(latest_progress(Path("/definitely/missing/backup.log")))

    def test_parses_device_list(self):
        self.assertEqual(parse_udids('["test-device-1234"]'), ["test-device-1234"])


if __name__ == "__main__":
    unittest.main()
