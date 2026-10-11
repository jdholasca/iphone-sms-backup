import unittest
from unittest.mock import patch

from admin_server import run_action


class AdminServerTest(unittest.TestCase):
    @patch("admin_server.command")
    def test_actions_are_allowlisted(self, command):
        command.return_value.returncode = 0
        command.return_value.stderr = ""
        self.assertEqual(run_action("start:android"), {"ok": True})
        command.assert_called_once_with("systemctl", "--user", "enable", "--now", "iphone-android-bridge.service")
        with self.assertRaises(ValueError):
            run_action("start:anything")


if __name__ == "__main__":
    unittest.main()
