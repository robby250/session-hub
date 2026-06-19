import os
import json
import tempfile
import unittest
from datetime import datetime
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

    def test_claude_project_directory_beats_home_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "-home-user-projects-example-project"
            project.mkdir()
            history = project / "session.jsonl"
            rows = [
                {"type": "user", "cwd": "/home/user"},
                {"type": "assistant", "cwd": "/home/user/projects/example-project"},
            ]
            history.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            info = session_hub.inspect_claude_file(history)
            self.assertEqual(
                info["project_cwd"], "/home/user/projects/example-project"
            )

    def test_parses_claude_five_hour_and_weekly_usage(self):
        text = (
            "Current session: 75% used · resets Jun 19, 9:00pm (Europe/Bucharest)\n"
            "Current week (all models): 40% used · resets Jun 23, 11:59pm "
            "(Europe/Bucharest)"
        )
        with patch("session_hub.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 6, 19, 12, 0)
            mocked_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            windows = session_hub.parse_claude_usage(text)
        self.assertEqual(
            [(window.name, window.used_percent) for window in windows],
            [("5-hour", 75), ("Weekly", 40)],
        )
        self.assertEqual(windows[0].resets, "Resets 2026-06-19 21:00")
        self.assertEqual(windows[1].resets, "Resets 2026-06-23 23:59")

    def test_claude_reset_rolls_into_next_year(self):
        self.assertEqual(
            session_hub.format_claude_reset(
                "Jan 2, 1:05am (Europe/Bucharest)",
                now=datetime(2026, 12, 31, 20, 0),
            ),
            "Resets 2027-01-02 01:05",
        )

    def test_claude_reset_accepts_hour_without_minutes(self):
        self.assertEqual(
            session_hub.format_claude_reset(
                "Jun 24, 12am (Europe/Bucharest)",
                now=datetime(2026, 6, 19, 20, 0),
            ),
            "Resets 2026-06-24 00:00",
        )

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

    @patch("session_hub.shutil.which")
    def test_claude_override_resumes_from_source_then_changes_directory(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        command = window.terminal_command(
            "Claude",
            "def-456",
            "/home/user/projects/new-location",
            "/home/user/projects/original-location",
        )
        self.assertIn(
            "--working-directory=/home/user/projects/original-location", command
        )
        self.assertEqual(
            command[-3:],
            [
                "--resume",
                "def-456",
                "/cd /home/user/projects/new-location",
            ],
        )
        window.close()

    @patch("session_hub.shutil.which")
    def test_danger_mode_adds_provider_flags(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "codex": "/home/user/.local/bin/codex",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        window.metadata["settings"] = {
            "codex_danger_mode": True,
            "claude_danger_mode": True,
        }
        codex = window.terminal_command("Codex", None, "/home/user")
        claude = window.terminal_command("Claude", None, "/home/user")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertIn("--dangerously-skip-permissions", claude)
        window.close()


if __name__ == "__main__":
    unittest.main()
