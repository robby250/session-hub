import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

import session_hub


class SessionHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_discovers_both_agents(self):
        sessions = session_hub.discover_sessions({"sessions": {}})
        providers = {item.provider for item in sessions}
        self.assertIn("Claude", providers)
        self.assertIn("Codex", providers)
        self.assertTrue(all(item.path.is_file() for item in sessions))

    def test_window_populates_all_discovered_sessions(self):
        window = session_hub.SessionHub()
        self.assertEqual(window.table.rowCount(), len(window.sessions))
        self.assertGreater(window.table.rowCount(), 0)
        window.close()

    @patch("session_hub.shutil.which")
    def test_codex_resume_uses_new_gnome_terminal_window(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "codex": "/home/user/.local/bin/codex",
        }.get(name)
        window = session_hub.SessionHub()
        command = window.terminal_command("Codex", "abc-123", "/home/user")
        self.assertIn("--window", command)
        self.assertEqual(command[-4:], ["resume", "-C", "/home/user", "abc-123"])
        window.close()

    @patch("session_hub.shutil.which")
    def test_claude_resume_uses_new_gnome_terminal_window(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        command = window.terminal_command("Claude", "def-456", "/home/user")
        self.assertIn("--window", command)
        self.assertEqual(command[-2:], ["--resume", "def-456"])
        window.close()


if __name__ == "__main__":
    unittest.main()
