import contextlib
import io
import os
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

import session_hub


class SessionHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Safety net, not a substitute for tests patching these deliberately:
        # a bare SessionHub() with no read_metadata patch still reads the
        # real metadata.json, and discover_sessions() has conditional
        # write_metadata() paths (clear-continuation linking, group
        # session_key resync) that can fire against real data mid-test and
        # write it straight back out. Defaulting METADATA_PATH/PID_DIR to a
        # throwaway temp dir here means any test that forgets its own patch
        # still can't touch the user's real files - see the incident where
        # this exact gap overwrote real settings with test fixture data.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        metadata_patcher = patch(
            "session_hub.METADATA_PATH", Path(temp_dir.name) / "metadata.json"
        )
        pid_dir_patcher = patch(
            "session_hub.PID_DIR", Path(temp_dir.name) / "pids"
        )
        metadata_patcher.start()
        pid_dir_patcher.start()
        self.addCleanup(metadata_patcher.stop)
        self.addCleanup(pid_dir_patcher.stop)

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
        self.assertEqual(
            window.table.selectionMode(),
            window.table.SelectionMode.ExtendedSelection,
        )
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

    def test_cached_file_scan_reuses_result_until_file_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.txt"
            path.write_text("v1", encoding="utf-8")
            calls = []

            def scan(p):
                calls.append(p)
                return {"content": p.read_text(encoding="utf-8")}

            first = session_hub._cached_file_scan(path, scan)
            second = session_hub._cached_file_scan(path, scan)
            self.assertEqual(first, {"content": "v1"})
            self.assertEqual(second, {"content": "v1"})
            self.assertEqual(len(calls), 1)

            # mtime must actually advance on some filesystems with coarse
            # timestamp resolution, so also change the size to be safe.
            os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
            path.write_text("v2-longer", encoding="utf-8")
            third = session_hub._cached_file_scan(path, scan)
            self.assertEqual(third, {"content": "v2-longer"})
            self.assertEqual(len(calls), 2)

    def test_scan_claude_file_stops_after_max_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "-home-user-projects-example-project"
            project.mkdir()
            path = project / "session.jsonl"
            rows = [{"type": "user", "cwd": "/irrelevant"} for _ in range(5)]
            rows.append({"type": "ai-title", "aiTitle": "Found late"})
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
            capped = session_hub._scan_claude_file(path, max_lines=3)
            self.assertNotIn("title", capped)
            uncapped = session_hub._scan_claude_file(path, max_lines=100)
            self.assertEqual(uncapped["title"], "Found late")

    def test_scan_claude_file_exits_early_once_title_and_cwd_found(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "-home-user-projects-example-project"
            project.mkdir()
            path = project / "session.jsonl"
            rows = [
                {"type": "user", "cwd": "/home/user/projects/example-project"},
                {"type": "ai-title", "aiTitle": "Early title"},
                # If the scan kept going past the point where both title and
                # cwd are resolved, this later row would overwrite the title
                # (later ai-title rows always win) - so seeing "Early title"
                # survive proves the scan actually stopped.
                {"type": "ai-title", "aiTitle": "Late title that should be ignored"},
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
            info = session_hub._scan_claude_file(path, max_lines=500)
            self.assertEqual(info["title"], "Early title")
            self.assertEqual(
                info["project_cwd"], "/home/user/projects/example-project"
            )

    def test_usage_probe_sessions_are_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "-home-user"
            project.mkdir()
            probe = project / "probe.jsonl"
            probe.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {"type": "queue-operation", "content": "/usage"},
                        {"type": "user", "entrypoint": "sdk-cli", "cwd": "/home/user"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            real = project / "real.jsonl"
            real.write_text(
                json.dumps({"type": "user", "entrypoint": "cli", "cwd": "/home/user"})
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(session_hub, "CLAUDE_PROJECTS", Path(temp)),
                patch("session_hub.claude_history_index", return_value={}),
            ):
                sessions = session_hub.claude_sessions()
        ids = {session.session_id for session in sessions}
        self.assertIn("real", ids)
        self.assertNotIn("probe", ids)

    def test_parses_claude_five_hour_and_weekly_usage(self):
        text = (
            "Current session: 75% used · resets Jun 19, 9:00pm (Europe/Bucharest)\n"
            "Current week (all models): 40% used · resets Jun 23, 11:59pm "
            "(Europe/Bucharest)\n"
            "Current week (Fable): 90% used · resets Jun 23, 11:59pm "
            "(Europe/Bucharest)"
        )
        with patch("session_hub.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 6, 19, 12, 0)
            mocked_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            windows = session_hub.parse_claude_usage(text)
        self.assertEqual(
            [(window.name, window.used_percent) for window in windows],
            [("5-hour", 75), ("Weekly", 40), ("Weekly (Fable)", 90)],
        )
        self.assertEqual(windows[0].resets, "Resets 2026-06-19 21:00")
        self.assertEqual(windows[1].resets, "Resets 2026-06-23 23:59")
        self.assertEqual(windows[0].window_minutes, 300)
        self.assertEqual(windows[1].window_minutes, 10080)
        self.assertIsNotNone(windows[0].reset_epoch)
        self.assertIsNotNone(windows[1].reset_epoch)

    def test_parses_claude_usage_activity_fallback(self):
        text = (
            "You are currently using your subscription to power your Claude "
            "Code usage\n\nWhat's contributing to your limits usage?\n"
            "Approximate, based on local sessions on this machine\n\n"
            "Last 24h · 2,520 requests · 6 sessions\n"
            "  99% of your usage came from sessions active for 8+ hours\n\n"
            "Last 7d · 18,091 requests · 12 sessions\n"
            "  95% of your usage was at >150k context"
        )
        activity = session_hub.parse_claude_usage_activity(text)
        self.assertEqual(
            [(a.label, a.requests, a.sessions) for a in activity],
            [("Last 24h", 2520, 6), ("Last 7d", 18091, 12)],
        )

    def test_read_claude_usage_falls_back_to_activity_when_bars_missing(self):
        payload = {
            "result": (
                "What's contributing to your limits usage?\n"
                "Last 24h · 2,520 requests · 6 sessions\n"
                "Last 7d · 18,091 requests · 12 sessions"
            )
        }
        completed = session_hub.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with patch("session_hub.subprocess.run", return_value=completed):
            result = session_hub.read_claude_usage()
        self.assertEqual(
            [(a.label, a.requests, a.sessions) for a in result],
            [("Last 24h", 2520, 6), ("Last 7d", 18091, 12)],
        )

    def test_usage_loaded_shows_activity_fallback_and_reverts_when_bars_return(self):
        metadata = {
            "sessions": {},
            "settings": {"enable_codex": True, "enable_claude": True,
                         "enable_antigravity": True},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            rows = window.usage_widgets["Claude"]
            activity = [
                session_hub.UsageActivity("Last 24h", 2520, 6),
                session_hub.UsageActivity("Last 7d", 18091, 12),
            ]
            window.usage_loaded("Claude", activity, "")
            self.assertFalse(rows[0][1].isHidden())
            self.assertEqual(rows[0][0].text(), "Last 24h")
            self.assertIn("2520 requests", rows[0][1].format())
            self.assertEqual(rows[0][2].text(), "6 sessions")
            self.assertTrue(rows[2][1].isHidden())

            # Once real percentage windows come back, the normal bar
            # rendering takes over automatically.
            real_windows = [
                session_hub.UsageWindow("5-hour", 10, "Resets later"),
                session_hub.UsageWindow("Weekly", 20, "Resets later"),
            ]
            window.usage_loaded("Claude", real_windows, "")
            self.assertIn("% used", rows[0][1].format())
        finally:
            window.close()

    def test_fable_usage_row_hides_when_no_fable_window(self):
        metadata = {
            "sessions": {},
            "settings": {"enable_codex": True, "enable_claude": True,
                         "enable_antigravity": True},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            fable_row = window.usage_widgets["Claude"][2]

            def make(name, pct):
                return session_hub.UsageWindow(name, pct, "Resets later")

            # Fable window present -> row visible.
            window.usage_loaded(
                "Claude",
                [make("5-hour", 10), make("Weekly", 20), make("Weekly (Fable)", 30)],
                "",
            )
            self.assertFalse(fable_row[1].isHidden())

            # Fable window absent (credit-only) -> row hidden, not "Unavailable".
            window.usage_loaded(
                "Claude", [make("5-hour", 10), make("Weekly", 20)], ""
            )
            self.assertTrue(fable_row[1].isHidden())

            # Errors also hide the optional row rather than flagging it.
            window.usage_loaded("Claude", [], "boom")
            self.assertTrue(fable_row[1].isHidden())
        finally:
            window.close()

    def test_usage_pace_flags_usage_ahead_of_even_allocation(self):
        now = datetime(2026, 6, 19, 12, 0)
        reset = now + timedelta(days=5)
        window = session_hub.UsageWindow(
            "Weekly", 30, "Resets later", window_minutes=10080, reset_epoch=reset.timestamp()
        )
        self.assertEqual(
            session_hub.usage_pace_text(window, now=now),
            "28.6% expected · 1.4% over pace",
        )

    def test_usage_pace_flags_usage_under_pace_and_missing_data(self):
        now = datetime(2026, 6, 19, 12, 0)
        reset = now + timedelta(days=5)
        under = session_hub.UsageWindow(
            "Weekly", 10, "Resets later", window_minutes=10080, reset_epoch=reset.timestamp()
        )
        self.assertEqual(
            session_hub.usage_pace_text(under, now=now),
            "28.6% expected · 18.6% under pace",
        )
        missing = session_hub.UsageWindow("Weekly", 10, "Resets later")
        self.assertIsNone(session_hub.usage_pace_text(missing, now=now))

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
    def test_new_claude_session_passes_selected_model(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        command = window.terminal_command(
            "Claude", None, "/home/user", model="opus"
        )
        self.assertEqual(command[-2:], ["--model", "opus"])
        # Default (no model) must not append a --model flag.
        default_command = window.terminal_command("Claude", None, "/home/user")
        self.assertNotIn("--model", default_command)
        window.close()

    def test_new_session_dialog_offers_models_only_for_claude(self):
        claude_dialog = session_hub.NewSessionDialog("Claude", {})
        self.assertIsNotNone(claude_dialog.model_combo)
        aliases = [
            claude_dialog.model_combo.itemData(index)
            for index in range(claude_dialog.model_combo.count())
        ]
        self.assertEqual(aliases[0], None)
        self.assertIn("opus", aliases)
        claude_dialog.model_combo.setCurrentIndex(aliases.index("opus"))
        claude_dialog.directory = Path.home()
        claude_dialog.accept()
        self.assertEqual(claude_dialog.model, "opus")
        claude_dialog.close()
        codex_dialog = session_hub.NewSessionDialog("Codex", {})
        self.assertIsNone(codex_dialog.model_combo)
        codex_dialog.close()

    def _codex_models_fixture(self):
        return [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "priority": 1,
                "visibility": "list",
                "supported_reasoning_levels": [
                    {"effort": "low"}, {"effort": "medium"}, {"effort": "high"},
                ],
            },
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "priority": 2,
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "medium"}],
            },
        ]

    def test_new_session_dialog_offers_model_and_effort_dropdowns_for_codex(self):
        with patch("session_hub.codex_models", return_value=self._codex_models_fixture()):
            codex_dialog = session_hub.NewSessionDialog("Codex", {})
            self.assertIsNone(codex_dialog.model_combo)
            self.assertIsInstance(codex_dialog.codex_model_combo, session_hub.QComboBox)
            self.assertIsInstance(codex_dialog.codex_effort_combo, session_hub.QComboBox)
            aliases = [
                codex_dialog.codex_model_combo.itemData(i)
                for i in range(codex_dialog.codex_model_combo.count())
            ]
            self.assertEqual(aliases, [None, "gpt-5.6-sol", "gpt-5.5"])
            codex_dialog.codex_model_combo.setCurrentIndex(aliases.index("gpt-5.6-sol"))
            efforts = [
                codex_dialog.codex_effort_combo.itemData(i)
                for i in range(codex_dialog.codex_effort_combo.count())
            ]
            self.assertEqual(efforts, [None, "low", "medium", "high"])
            codex_dialog.codex_effort_combo.setCurrentIndex(efforts.index("high"))
            codex_dialog.directory = Path.home()
            codex_dialog.accept()
            self.assertEqual(codex_dialog.model, "gpt-5.6-sol")
            self.assertEqual(codex_dialog.reasoning_effort, "high")
            codex_dialog.close()

            blank_dialog = session_hub.NewSessionDialog("Codex", {})
            blank_dialog.directory = Path.home()
            blank_dialog.accept()
            self.assertIsNone(blank_dialog.model)
            self.assertIsNone(blank_dialog.reasoning_effort)
            blank_dialog.close()

            # A custom slug this machine's cache doesn't know is still typeable.
            custom_dialog = session_hub.NewSessionDialog("Codex", {})
            custom_dialog.codex_model_combo.setCurrentText("gpt-6-preview")
            custom_dialog.directory = Path.home()
            custom_dialog.accept()
            self.assertEqual(custom_dialog.model, "gpt-6-preview")
            custom_dialog.close()

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

    def test_linked_sessions_render_as_one_active_row(self):
        codex = session_hub.Session(
            "Codex",
            "codex-id",
            "Original",
            "/home/user",
            "/home/user",
            100,
            Path("/tmp/codex.jsonl"),
        )
        claude = session_hub.Session(
            "Claude",
            "claude-id",
            "Destination",
            "/home/user",
            "/home/user",
            200,
            Path("/tmp/claude.jsonl"),
        )
        metadata = {
            "sessions": {"Codex:codex-id": {"name": "Logical Session"}},
            "links": {
                "Codex:codex-id": {
                    "members": ["Codex:codex-id", "Claude:claude-id"],
                    "active": "Claude:claude-id",
                }
            },
        }
        with (
            patch("session_hub.codex_sessions", return_value=[codex]),
            patch("session_hub.claude_sessions", return_value=[claude]),
            patch("session_hub.antigravity_sessions", return_value=[]),
        ):
            sessions = session_hub.discover_sessions(metadata)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].provider, "Claude")
        self.assertEqual(sessions[0].title, "Logical Session")
        self.assertEqual(sessions[0].key, "Codex:codex-id")

    def test_pending_handoff_matches_unique_handoff_filename(self):
        source_key = "Claude:source-id"
        destination = session_hub.Session(
            "Antigravity",
            "agy-id",
            (
                "Continue using "
                "/home/user/.local/share/session-hub/handoffs/unique-file.md"
            ),
            "/home/user",
            "/home/user",
            2000,
            Path("/tmp/agy.db"),
        )
        metadata = {
            "sessions": {},
            "links": {
                source_key: {
                    "members": [source_key, "Codex:codex-id"],
                    "active": "Codex:codex-id",
                }
            },
            "pending_handoffs": [
                {
                    "logical_key": source_key,
                    "target_provider": "Antigravity",
                    "existing_keys": [],
                    "cwd": "/different/path",
                    "handoff_path": (
                        "/home/user/.local/share/session-hub/handoffs/"
                        "unique-file.md"
                    ),
                    "started_ms": 1000,
                    "expires_ms": 9999999999999,
                }
            ],
        }
        changed = session_hub.resolve_pending_handoffs(metadata, [destination])
        self.assertTrue(changed)
        self.assertEqual(metadata["pending_handoffs"], [])
        self.assertEqual(metadata["links"][source_key]["active"], destination.native_key)
        self.assertIn(
            destination.native_key,
            metadata["links"][source_key]["members"],
        )

    def test_native_session_index_applies_name_overrides(self):
        claude = session_hub.Session(
            "Claude", "id-old", "Fix parser bug in ordnance defects module",
            "/tmp/vamp", "/tmp/vamp", 100, Path("/tmp/old.jsonl"),
        )
        metadata = {"sessions": {"Claude:id-old": {"name": "tm4-ordnance-defects-fixes"}}}
        with (
            patch("session_hub.codex_sessions", return_value=[]),
            patch("session_hub.claude_sessions", return_value=[claude]),
            patch("session_hub.antigravity_sessions", return_value=[]),
            patch("session_hub.read_metadata", return_value=metadata),
        ):
            index = session_hub.native_session_index()
        self.assertEqual(index["Claude:id-old"].title, "tm4-ordnance-defects-fixes")

    def test_linked_conversations_exclude_active_native_session(self):
        active = session_hub.Session(
            "Codex",
            "codex-id",
            "Logical Session",
            "/home/user/project",
            "/home/user/project",
            300,
            Path("/tmp/codex.jsonl"),
            logical_key="Claude:claude-id",
            linked_keys=(
                "Claude:claude-id",
                "Codex:codex-id",
                "Antigravity:agy-id",
            ),
        )
        claude = session_hub.Session(
            "Claude",
            "claude-id",
            "Original Claude",
            "/home/user/original",
            "/home/user/original",
            100,
            Path("/tmp/claude.jsonl"),
        )
        antigravity = session_hub.Session(
            "Antigravity",
            "agy-id",
            "Antigravity copy",
            "/home/user/project",
            "/home/user/project",
            200,
            Path("/tmp/agy.db"),
        )
        window = session_hub.SessionHub()
        with patch(
            "session_hub.native_session_index",
            return_value={
                active.native_key: active,
                claude.native_key: claude,
                antigravity.native_key: antigravity,
            },
        ):
            conversations = window.linked_conversations(active)
        self.assertEqual(
            [conversation.native_key for conversation in conversations],
            ["Antigravity:agy-id", "Claude:claude-id"],
        )
        window.close()

    def test_open_linked_conversation_does_not_change_active_link(self):
        active = session_hub.Session(
            "Codex",
            "codex-id",
            "Logical Session",
            "/home/user/project",
            "/home/user/project",
            300,
            Path("/tmp/codex.jsonl"),
            logical_key="Claude:claude-id",
            linked_keys=("Claude:claude-id", "Codex:codex-id"),
        )
        claude = session_hub.Session(
            "Claude",
            "claude-id",
            "Original Claude",
            "/home/user/original",
            "/home/user/original",
            100,
            Path("/tmp/claude.jsonl"),
        )
        window = session_hub.SessionHub()
        original_links = json.loads(json.dumps(window.metadata.get("links", {})))
        with (
            patch.object(window, "selected", return_value=active),
            patch.object(window, "linked_conversations", return_value=[claude]),
            patch(
                "session_hub.QInputDialog.getItem",
                return_value=("Claude — Original Claude  [claude-i]", True),
            ),
            patch.object(window, "launch") as launch,
        ):
            window.open_linked_conversation()
        launch.assert_called_once_with(
            "Claude",
            "claude-id",
            "/home/user/original",
            "/home/user/original",
            session_key=claude.key,
        )
        self.assertEqual(window.metadata.get("links", {}), original_links)
        window.close()

    def test_link_to_existing_conversation_creates_link_between_same_cwd_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                new_session = session_hub.Session(
                    "Claude", "id-new", "Claude 3e410ca0", "/tmp/vamp", "/tmp/vamp",
                    200, Path("/tmp/new.jsonl"),
                )
                old_session = session_hub.Session(
                    "Claude", "id-old", "vamp-s1", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/old.jsonl"),
                )
                other_cwd_session = session_hub.Session(
                    "Claude", "id-other", "unrelated", "/tmp/other", "/tmp/other",
                    150, Path("/tmp/other.jsonl"),
                )
                index = {
                    s.native_key: s for s in (new_session, old_session, other_cwd_session)
                }
                with (
                    patch("session_hub.native_session_index", return_value=index),
                    patch(
                        "session_hub.QInputDialog.getItem",
                        return_value=("Claude — vamp-s1  [id-old]", True),
                    ),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.link_to_existing_conversation_for(new_session)
                links = window.metadata["links"]
                self.assertEqual(len(links), 1)
                link = next(iter(links.values()))
                self.assertEqual(set(link["members"]), {"Claude:id-old", "Claude:id-new"})
                self.assertEqual(link["active"], "Claude:id-new")
            finally:
                window.close()

    def test_link_to_existing_conversation_copies_old_name_and_launch_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {
                    "Claude:id-old": {
                        "name": "vamp-s1",
                        "env": {"ANTHROPIC_MODEL": "opus"},
                        "flags": {"--dangerously-skip-permissions": True},
                    }
                },
                "settings": {},
                "groups": {},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                new_session = session_hub.Session(
                    "Claude", "id-new", "Claude 3e410ca0", "/tmp/vamp", "/tmp/vamp",
                    200, Path("/tmp/new.jsonl"),
                )
                old_session = session_hub.Session(
                    "Claude", "id-old", "vamp-s1", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/old.jsonl"),
                )
                index = {s.native_key: s for s in (new_session, old_session)}
                with (
                    patch("session_hub.native_session_index", return_value=index),
                    patch(
                        "session_hub.QInputDialog.getItem",
                        return_value=("Claude — vamp-s1  [id-old]", True),
                    ),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.link_to_existing_conversation_for(new_session)
                link_id = next(iter(window.metadata["links"]))
                new_overrides = window.metadata["sessions"]["Claude:id-new"]
                self.assertEqual(new_overrides["name"], "vamp-s1")
                link_overrides = window.metadata["sessions"][link_id]
                self.assertEqual(link_overrides["env"], {"ANTHROPIC_MODEL": "opus"})
                self.assertEqual(
                    link_overrides["flags"], {"--dangerously-skip-permissions": True}
                )
            finally:
                window.close()

    def test_link_to_existing_conversation_copies_organic_title_with_no_explicit_override(self):
        # The old session was never explicitly renamed via Session Hub -
        # its title is just whatever Claude Code auto-generated - so there's
        # no metadata["sessions"][old_key]["name"] to copy. The new session
        # should still inherit that organic title, not silently stay
        # unnamed just because it was never an explicit rename.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                new_session = session_hub.Session(
                    "Claude", "id-new", "Claude 3e410ca0", "/tmp/vamp", "/tmp/vamp",
                    200, Path("/tmp/new.jsonl"),
                )
                old_session = session_hub.Session(
                    "Claude", "id-old", "vampulse-orchestrator", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/old.jsonl"),
                )
                index = {s.native_key: s for s in (new_session, old_session)}
                with (
                    patch("session_hub.native_session_index", return_value=index),
                    patch(
                        "session_hub.QInputDialog.getItem",
                        return_value=(
                            "Claude — vampulse-orchestrator  [id-old]", True
                        ),
                    ),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.link_to_existing_conversation_for(new_session)
                self.assertEqual(
                    window.metadata["sessions"]["Claude:id-new"]["name"],
                    "vampulse-orchestrator",
                )
            finally:
                window.close()

    def test_handoff_export_keeps_conversation_without_tool_payloads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "claude.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "message": {"content": "Please fix the parser."},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "I found the bug."},
                                        {
                                            "type": "tool_use",
                                            "name": "Bash",
                                            "input": {"command": "secret-command"},
                                        },
                                    ]
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            session = session_hub.Session(
                "Claude",
                "id",
                "Parser",
                str(root),
                str(root),
                0,
                transcript,
            )
            with patch("session_hub.HANDOFF_DIR", root / "handoffs"):
                handoff = session_hub.write_handoff(session, "Codex")
            text = handoff.read_text(encoding="utf-8")
            self.assertIn("Please fix the parser.", text)
            self.assertIn("I found the bug.", text)
            self.assertNotIn("secret-command", text)

    def test_handoff_includes_full_compact_summary_when_present(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "claude.jsonl"
            rows = [
                {
                    "type": "user",
                    "uuid": "before",
                    "message": {"content": "Earlier context " + "x" * 30000},
                },
                {
                    "type": "user",
                    "uuid": "compact-1",
                    "isCompactSummary": True,
                    "message": {"content": "Older compaction, superseded."},
                },
                {
                    "type": "user",
                    "uuid": "compact-2",
                    "isCompactSummary": True,
                    "message": {"content": "Complete summary " + "y" * 15000},
                },
                {
                    "type": "assistant",
                    "uuid": "noise",
                    "message": {
                        "content": "You've hit your session limit · resets later"
                    },
                },
                {
                    "type": "user",
                    "uuid": "after",
                    "message": {"content": "Latest real request"},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )
            session = session_hub.Session(
                "Claude",
                "id",
                "Project",
                str(root),
                str(root),
                0,
                transcript,
            )
            with patch("session_hub.HANDOFF_DIR", root / "handoffs"):
                handoff = session_hub.write_handoff(session, "Codex")
            text = handoff.read_text(encoding="utf-8")
            self.assertIn("Full /compact summary", text)
            self.assertIn("Complete summary " + "y" * 15000, text)
            self.assertNotIn("Older compaction, superseded.", text)
            self.assertNotIn("Earlier context", text)
            self.assertIn("Latest real request", text)
            self.assertNotIn("You've hit your session limit", text)

    def test_long_handoff_message_has_explicit_omission_marker(self):
        compacted = session_hub.compact_message("a" * 20000, 12000)
        self.assertEqual(len(compacted), 12000)
        self.assertIn("middle of this message omitted", compacted)

    @patch("session_hub.shutil.which")
    def test_handoff_commands_launch_destination_agent(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "codex": "/home/user/.local/bin/codex",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        claude = window.handoff_terminal_command(
            "Claude",
            "/home/user",
            Path("/tmp/handoff.md"),
            "Linked",
            "11111111-1111-4111-8111-111111111111",
        )
        codex = window.handoff_terminal_command(
            "Codex", "/home/user", Path("/tmp/handoff.md"), "Linked"
        )
        self.assertIn("--session-id", claude)
        self.assertIn("11111111-1111-4111-8111-111111111111", claude)
        self.assertIn("-C", codex)
        self.assertTrue(any("/tmp/handoff.md" in value for value in codex))
        window.close()

    @patch("session_hub.shutil.which")
    def test_handoff_commands_resume_existing_linked_sessions(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "codex": "/home/user/.local/bin/codex",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        claude = window.handoff_terminal_command(
            "Claude",
            "/home/user/new-location",
            Path("/tmp/handoff.md"),
            "Linked",
            "claude-existing",
            resume_existing=True,
            source_cwd="/home/user/original-location",
        )
        codex = window.handoff_terminal_command(
            "Codex",
            "/home/user",
            Path("/tmp/handoff.md"),
            "Linked",
            "codex-existing",
            resume_existing=True,
        )
        self.assertIn("--working-directory=/home/user/original-location", claude)
        self.assertIn("--resume", claude)
        self.assertIn("claude-existing", claude)
        self.assertNotIn("--session-id", claude)
        self.assertIn("resume", codex)
        self.assertIn("codex-existing", codex)
        self.assertTrue(any("/tmp/handoff.md" in value for value in codex))
        window.close()

    def test_continue_with_other_agent_sets_correct_target_provider(self):
        active = session_hub.Session(
            "Claude",
            "claude-id",
            "Logical Session",
            "/home/user/project",
            "/home/user/project",
            300,
            Path("/tmp/claude.jsonl"),
        )
        with tempfile.TemporaryDirectory() as temp:
            fake_metadata = Path(temp) / "metadata.json"
            with (
                patch("session_hub.METADATA_PATH", fake_metadata),
                patch("session_hub.codex_sessions", return_value=[]),
                patch("session_hub.claude_sessions", return_value=[]),
                patch("session_hub.antigravity_sessions", return_value=[]),
            ):
                window = session_hub.SessionHub()
                window.metadata = {
                    "sessions": {},
                    "links": {},
                    "pending_handoffs": []
                }
                with (
                    patch.object(window, "selected", return_value=active),
                    patch("session_hub.QInputDialog.getItem", return_value=("Antigravity", True)),
                    patch("session_hub.QMessageBox.question", return_value=session_hub.QMessageBox.StandardButton.Yes),
                    patch("session_hub.write_handoff", return_value=Path("/tmp/handoff.md")),
                    patch.object(window, "handoff_terminal_command", return_value=["cmd"]),
                    patch("session_hub.subprocess.Popen") as popen,
                ):
                    window.continue_with_other_agent()
                pending = window.metadata.get("pending_handoffs", [])
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["target_provider"], "Antigravity")

                window.metadata["pending_handoffs"] = []
                with (
                    patch.object(window, "selected", return_value=active),
                    patch("session_hub.QInputDialog.getItem", return_value=("Codex", True)),
                    patch("session_hub.QMessageBox.question", return_value=session_hub.QMessageBox.StandardButton.Yes),
                    patch("session_hub.write_handoff", return_value=Path("/tmp/handoff.md")),
                    patch.object(window, "handoff_terminal_command", return_value=["cmd"]),
                    patch("session_hub.subprocess.Popen") as popen,
                ):
                    window.continue_with_other_agent()
                pending = window.metadata.get("pending_handoffs", [])
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["target_provider"], "Codex")
                window.close()

    def test_parses_antigravity_model_group_quotas(self):
        text = (
            "GEMINI MODELS\n"
            "Weekly Limit\n"
            "[bar] 86.91%\n"
            "87% remaining · Refreshes in 167h 58m\n"
            "Five Hour Limit\n"
            "[bar] 75.00%\n"
            "75% remaining · Refreshes in 4h 30m\n"
            "CLAUDE AND GPT MODELS\n"
            "Weekly Limit\n"
            "[bar] 100.00%\n"
            "Quota available\n"
            "Five Hour Limit\n"
            "[bar] 100.00%\n"
            "Quota available\n"
        )
        with patch("session_hub.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 6, 20, 1, 0)
            mocked_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
            windows = session_hub.parse_antigravity_usage(text)
        self.assertEqual(
            [(window.name, window.used_percent) for window in windows],
            [
                ("Gemini weekly", 13),
                ("Gemini 5-hour", 25),
                ("Claude/GPT weekly", 0),
                ("Claude/GPT 5-hour", 0),
            ],
        )
        self.assertEqual(windows[0].resets, "Resets 2026-06-27 00:58")
        self.assertEqual(windows[1].resets, "Resets 2026-06-20 05:30")
        self.assertEqual(windows[2].resets, "Quota available")
        self.assertEqual(windows[3].resets, "Quota available")

    def test_parses_antigravity_starter_weekly_only_quotas(self):
        windows = session_hub.parse_antigravity_usage(
            "GEMINI MODELS\nWeekly Limit\n80% remaining · Refreshes in 100h\n"
            "CLAUDE AND GPT MODELS\nWeekly Limit\nQuota available\n"
        )
        self.assertEqual(
            [window.name for window in windows],
            ["Gemini weekly", "Claude/GPT weekly"],
        )

    @patch("session_hub.shutil.which")
    def test_antigravity_resume_and_handoff_commands(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "agy": "/home/user/.local/bin/agy",
        }.get(name)
        window = session_hub.SessionHub()
        window.metadata["settings"] = {"antigravity_danger_mode": True}
        resume = window.terminal_command(
            "Antigravity",
            "agy-id",
            "/home/user",
        )
        handoff = window.handoff_terminal_command(
            "Antigravity",
            "/home/user",
            Path("/tmp/handoff.md"),
            "Linked",
            "agy-id",
            resume_existing=True,
        )
        self.assertIn("--dangerously-skip-permissions", resume)
        self.assertEqual(resume[-2:], ["--conversation", "agy-id"])
        self.assertIn("--conversation", handoff)
        self.assertIn("--prompt-interactive", handoff)
        window.close()

    def test_antigravity_transcript_is_available_to_handoffs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = (
                root
                / "brain"
                / "agy-id"
                / ".system_generated"
                / "logs"
                / "transcript.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "USER_INPUT",
                                "content": (
                                    "<USER_REQUEST>Fix the launcher.</USER_REQUEST>"
                                ),
                            }
                        ),
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "content": "I found the desktop entry.",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "RUN_COMMAND",
                                "content": "secret tool output",
                            }
                        ),
                    )
                ),
                encoding="utf-8",
            )
            database = root / "agy-id.db"
            database.touch()
            session = session_hub.Session(
                "Antigravity",
                "agy-id",
                "Launcher",
                str(root),
                str(root),
                0,
                database,
            )
            with patch("session_hub.ANTIGRAVITY_BRAIN", root / "brain"):
                messages = session_hub.transcript_messages(session)
            self.assertEqual(
                messages,
                [
                    ("user", "Fix the launcher."),
                    ("assistant", "I found the desktop entry."),
                ],
            )

    def test_manual_refresh_refreshes_usage(self):
        window = session_hub.SessionHub()
        self.assertFalse(hasattr(window, "usage_timer"))
        with (
            patch.object(window, "refresh") as refresh,
            patch.object(window, "refresh_usage") as refresh_usage,
        ):
            window.refresh_all()
        refresh.assert_called_once_with()
        refresh_usage.assert_called_once_with()
        window.close()

    def test_new_session_toolbar_uses_selected_provider(self):
        all_enabled = {
            "sessions": {},
            "settings": {
                "enable_codex": True,
                "enable_claude": True,
                "enable_antigravity": True,
            },
        }
        with patch("session_hub.read_metadata", return_value=all_enabled):
            window = session_hub.SessionHub()
        self.assertEqual(
            [window.new_provider.itemText(index) for index in range(3)],
            list(session_hub.PROVIDERS),
        )
        window.new_provider.setCurrentText("Antigravity")
        with patch.object(window, "launch_new") as launch_new:
            window.launch_selected_provider()
        launch_new.assert_called_once_with("Antigravity")
        window.close()

    def test_enter_key_resumes_selected_session(self):
        from PyQt6.QtGui import QKeySequence, QShortcut

        # Patch at the class level so the QShortcut (connected during
        # build_ui()) binds to the mock instead of the real method.
        with patch.object(session_hub.SessionHub, "resume_selected") as resume_selected:
            window = session_hub.SessionHub()
            shortcuts = [
                shortcut
                for shortcut in window.table.findChildren(QShortcut)
                if shortcut.key() == QKeySequence(session_hub.Qt.Key.Key_Return)
            ]
            self.assertTrue(shortcuts)
            shortcuts[0].activated.emit()
            resume_selected.assert_called_once()
            window.close()

    def test_f5_triggers_refresh(self):
        from PyQt6.QtGui import QKeySequence, QShortcut

        with patch.object(session_hub.SessionHub, "refresh_all") as refresh_all:
            window = session_hub.SessionHub()
            shortcuts = [
                shortcut
                for shortcut in window.findChildren(QShortcut)
                if shortcut.key() == QKeySequence(session_hub.Qt.Key.Key_F5)
            ]
            self.assertTrue(shortcuts)
            shortcuts[0].activated.emit()
            refresh_all.assert_called_once()
            window.close()

    def test_new_session_dialog_defaults_to_home(self):
        dialog = session_hub.NewSessionDialog("Codex", {})
        dialog.accept()
        self.assertEqual(dialog.directory, session_hub.DEFAULT_SESSION_DIR)
        dialog.close()

    def test_new_session_dialog_creates_primary_project_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            dialog = session_hub.NewSessionDialog(
                "Claude", {"primary_projects_dir": temp}
            )
            dialog.location.setCurrentIndex(dialog.location.findData("primary"))
            dialog.project_name.setText("example-project")
            dialog.accept()
            self.assertEqual(dialog.directory, Path(temp) / "example-project")
            self.assertTrue(dialog.directory.is_dir())
            dialog.close()

    def test_new_session_dialog_uses_configured_secondary_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            dialog = session_hub.NewSessionDialog(
                "Codex", {"secondary_projects_dir": temp}
            )
            dialog.location.setCurrentIndex(dialog.location.findData("secondary"))
            dialog.project_name.setText("synced-project")
            dialog.accept()
            self.assertEqual(dialog.directory, Path(temp) / "synced-project")
            dialog.close()

    def test_settings_default_to_never_deleting_trash(self):
        dialog = session_hub.SettingsDialog({})
        self.assertEqual(dialog.values()["trash_retention_days"], 0)
        dialog.close()

    def test_settings_preserve_geometry_and_save_project_roots(self):
        dialog = session_hub.SettingsDialog({"window_geometry": "saved-value"})
        dialog.primary_projects.setText("~/code")
        dialog.secondary_projects.setText("~/synced-code")
        values = dialog.values()
        self.assertEqual(values["window_geometry"], "saved-value")
        self.assertEqual(values["primary_projects_dir"], str(Path("~/code").expanduser()))
        self.assertEqual(
            values["secondary_projects_dir"],
            str(Path("~/synced-code").expanduser()),
        )
        self.assertTrue(values["enable_codex"])
        self.assertTrue(values["enable_claude"])
        self.assertTrue(values["enable_antigravity"])

        # Test validation: uncheck two, remaining one should be disabled
        dialog.enable_codex.setChecked(False)
        dialog.enable_claude.setChecked(False)
        self.assertFalse(dialog.enable_antigravity.isEnabled())
        
        # Checking one back should re-enable it
        dialog.enable_codex.setChecked(True)
        self.assertTrue(dialog.enable_antigravity.isEnabled())
        dialog.close()

    def test_danger_mode_checkbox_hidden_when_agent_disabled(self):
        dialog = session_hub.SettingsDialog({})
        self.assertFalse(dialog.codex_danger.isHidden())
        self.assertFalse(dialog.claude_danger.isHidden())
        self.assertFalse(dialog.antigravity_danger.isHidden())

        dialog.enable_codex.setChecked(False)
        self.assertTrue(dialog.codex_danger.isHidden())
        self.assertFalse(dialog.claude_danger.isHidden())

        dialog.enable_codex.setChecked(True)
        self.assertFalse(dialog.codex_danger.isHidden())
        dialog.close()

    def test_env_editor_round_trips_custom_and_ignores_blank_names(self):
        editor = session_hub.EnvEditor({"FOO": "1", "BAR": "two"})
        self.assertEqual(editor.env(), {"FOO": "1", "BAR": "two"})
        editor.add_custom_row("  ", "orphan")
        editor.add_custom_row("BAZ", "3")
        self.assertEqual(editor.env(), {"FOO": "1", "BAR": "two", "BAZ": "3"})

    def test_env_editor_uses_typed_widgets_for_known_variables(self):
        editor = session_hub.EnvEditor(
            {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65", "USE_BUILTIN_RIPGREP": "0"}
        )
        # Value cells for known variables are widgets, not plain text items.
        self.assertIsNotNone(editor.table.cellWidget(0, 1))
        self.assertEqual(
            editor.env(),
            {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65", "USE_BUILTIN_RIPGREP": "0"},
        )

    def test_env_editor_percent_default_and_clamping(self):
        editor = session_hub.EnvEditor({})
        editor.add_known_row("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
        # Falls back to the spec default when no value is supplied.
        self.assertEqual(editor.env(), {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"})

    def test_env_editor_toggle_off_is_dropped(self):
        editor = session_hub.EnvEditor({})
        editor.add_known_row("DISABLE_TELEMETRY")
        # A toggle left Off resolves to no variable at all.
        self.assertEqual(editor.env(), {})

    def test_settings_dialog_saves_global_env(self):
        dialog = session_hub.SettingsDialog(
            {"global_env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}}
        )
        self.assertEqual(
            dialog.env_editor.env(), {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}
        )
        dialog.env_editor.add_known_row("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "40000")
        self.assertEqual(
            dialog.values()["global_env"],
            {
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "40000",
            },
        )
        dialog.close()

    def test_launch_env_merges_global_and_session_overrides(self):
        metadata = {
            "sessions": {"Claude:s1": {"env": {"FOO": "session", "ONLY": "s"}}},
            "settings": {"global_env": {"FOO": "global", "BAR": "g"}},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            merged = window.launch_env("Claude:s1")
            self.assertEqual(merged["FOO"], "session")
            self.assertEqual(merged["BAR"], "g")
            self.assertEqual(merged["ONLY"], "s")
            self.assertEqual(merged["PATH"], os.environ["PATH"])
            self.assertEqual(window.launch_env("Claude:other")["FOO"], "global")
            self.assertEqual(window.launch_env(None)["BAR"], "g")
        finally:
            window.close()

    def test_launch_env_returns_none_when_unset(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertIsNone(window.launch_env(None))
            self.assertIsNone(window.launch_env("Claude:s1"))
        finally:
            window.close()

    def test_launch_passes_resolved_env_to_spawn(self):
        metadata = {
            "sessions": {},
            "settings": {"global_env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}},
        }
        with (
            patch("session_hub.read_metadata", return_value=metadata),
            patch("session_hub.Path.is_dir", return_value=True),
            patch("session_hub.subprocess.Popen") as popen,
            patch.object(
                session_hub.SessionHub, "terminal_command", return_value=["cmd"]
            ),
        ):
            window = session_hub.SessionHub()
            try:
                window.launch("Claude", "s1", "/tmp", session_key="Claude:s1")
            finally:
                window.close()
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "70")

    @patch("session_hub.shutil.which", return_value="/usr/bin/wmctrl")
    @patch("session_hub.subprocess.run")
    def test_focus_window_by_title_activates_matching_window(self, run, which):
        run.return_value = MagicMock(
            stdout="0x01 0 gnome-terminal-server.Gnome-terminal host Claude — session-hub\n"
        )
        session_hub.focus_window_by_title("Claude — session-hub")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/wmctrl", "-i", "-a", "0x01"],
        )

    @patch("session_hub.shutil.which", return_value="/usr/bin/wmctrl")
    @patch("session_hub.subprocess.run")
    def test_focus_window_by_title_ignores_non_terminal_window_with_same_title(self, run, which):
        """A Claude session renamed to match Session Hub's own window title
        ("Session Hub" - exactly what happened for this project's own dev
        session) must not cause the launcher's own window to be treated as
        the terminal to reveal."""
        run.return_value = MagicMock(
            stdout=(
                "0x01 0 session_hub.py.Session Hub host Session Hub\n"
                "0x02 0 gnome-terminal-server.Gnome-terminal host Session Hub\n"
            )
        )
        session_hub.focus_window_by_title("Session Hub")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/wmctrl", "-i", "-a", "0x02"],
        )

    @patch("session_hub.shutil.which", return_value=None)
    @patch("session_hub.subprocess.run")
    def test_focus_window_by_title_noop_when_wmctrl_missing(self, run, which):
        session_hub.focus_window_by_title("Claude — session-hub")
        run.assert_not_called()

    @patch("session_hub.threading.Thread")
    @patch("session_hub.subprocess.Popen")
    def test_spawn_starts_focus_thread_for_titled_command(self, popen, thread):
        window = session_hub.SessionHub()
        try:
            window.spawn(
                ["gnome-terminal", "--title=Claude — session-hub", "--"],
            )
        finally:
            window.close()
        self.assertEqual(
            thread.call_args.kwargs["args"], ("Claude — session-hub",)
        )
        thread.return_value.start.assert_called_once()

    def test_pid_capture_command_wraps_real_args_via_exec(self):
        wrapped = session_hub.pid_capture_command(
            Path("/tmp/x.pid"), ["claude", "--resume", "abc"]
        )
        self.assertEqual(
            wrapped,
            [
                "bash",
                "-c",
                'echo $$ > "$1"; shift; exec "$@"',
                "session-hub",
                "/tmp/x.pid",
                "claude",
                "--resume",
                "abc",
            ],
        )

    def test_prefix_env_command_adds_env_overrides_and_strips(self):
        # No "--" before the command: confirmed against a real launch
        # failure that some env builds reject it there ("env: '--': No
        # such file or directory"), silently killing the whole tmux
        # session before it could ever attach - see prefix_env_command.
        wrapped = session_hub.prefix_env_command(
            ["claude", "--name", "vamp-s1"],
            {"ANTHROPIC_MODEL": "opus"},
            ["CLAUDE_CODE_CHILD_SESSION"],
        )
        self.assertEqual(
            wrapped,
            [
                "env",
                "-u",
                "CLAUDE_CODE_CHILD_SESSION",
                "ANTHROPIC_MODEL=opus",
                "claude",
                "--name",
                "vamp-s1",
            ],
        )
        self.assertNotIn("--", wrapped)

    def test_prefix_env_command_is_actually_runnable_by_the_real_env_binary(self):
        # Regression: the old "-- " placement parsed fine in isolation but
        # broke the real launch, because this system's env only recognizes
        # NAME=VALUE assignments up to the first non-matching argument and
        # does not treat a later "--" as an options terminator. Exercise
        # the real env binary, not just string-shape assertions, so this
        # class of bug can't slip through unnoticed again.
        wrapped = session_hub.prefix_env_command(
            ["bash", "-c", "echo $MARKER_VAR"],
            {"MARKER_VAR": "it-worked"},
            None,
        )
        result = subprocess.run(wrapped, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "it-worked")

    def test_prefix_env_command_is_noop_with_nothing_to_apply(self):
        args = ["claude", "--name", "vamp-s1"]
        self.assertEqual(session_hub.prefix_env_command(args, {}, None), args)

    @patch("session_hub.shutil.which")
    def test_tmux_group_launch_command_matches_name_to_tmux_session(self, which):
        # VAMPULSE-orchestrator's request: the tmux session name and the
        # Claude --name must be identical - external tooling looks a
        # session up by that exact name via `tmux send-keys -t <name>`.
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "tmux": "/usr/bin/tmux",
        }.get(name)
        command = session_hub.tmux_group_launch_command(
            "vamp-sonnet1",
            "/home/user/VAMPULSE-game",
            [
                "claude", "--dangerously-skip-permissions",
                "--name", "vamp-sonnet1", "--model", "sonnet",
            ],
        )
        self.assertEqual(
            command,
            [
                "bash",
                "-c",
                '"$1" has-session -t "$2" 2>/dev/null || "$1" new-session -d -s "$2" -c "$3" "$4";'
                ' "$1" set-option -g set-titles on >/dev/null;'
                ' "$1" set-option -g set-titles-string "#S" >/dev/null;'
                ' exec "$5" --window -- "$1" attach -t "$2"',
                "session-hub",
                "/usr/bin/tmux",
                "vamp-sonnet1",
                "/home/user/VAMPULSE-game",
                "claude --dangerously-skip-permissions --name vamp-sonnet1 --model sonnet",
                "/usr/bin/gnome-terminal",
            ],
        )

    def test_rename_group_row_in_renames_row_key_and_bucket(self):
        metadata = {
            "sessions": {
                "group:/tmp/vamp#vamp-sonnet1": {"name": "VAMP-worker1", "flags": {"--effort": "high"}},
                "Claude:abc": {"name": "vamp-sonnet1"},
            },
            "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": [
                {"name": "vamp-sonnet1", "override_key": "group:/tmp/vamp#vamp-sonnet1",
                 "session_key": "Claude:abc"},
                {"name": "other", "override_key": "group:/tmp/vamp#other"},
            ]}},
        }
        result = session_hub.rename_group_row_in(metadata, "/tmp/vamp", "vamp-sonnet1", "VAMP-worker1")
        self.assertEqual(result["status"], "renamed")
        row = metadata["groups"]["/tmp/vamp"]["rows"][0]
        self.assertEqual(row["name"], "VAMP-worker1")
        self.assertEqual(row["override_key"], "group:/tmp/vamp#VAMP-worker1")
        self.assertEqual(row["session_key"], "Claude:abc")
        # bucket moved, its stale display name dropped, other fields kept
        self.assertNotIn("group:/tmp/vamp#vamp-sonnet1", metadata["sessions"])
        self.assertEqual(metadata["sessions"]["group:/tmp/vamp#VAMP-worker1"],
                         {"flags": {"--effort": "high"}})
        # collisions and unknown rows refuse
        self.assertEqual(session_hub.rename_group_row_in(metadata, "/tmp/vamp", "VAMP-worker1", "other")["status"], "error")
        self.assertEqual(session_hub.rename_group_row_in(metadata, "/tmp/vamp", "nope", "x")["status"], "error")
        self.assertEqual(session_hub.rename_group_row_in(metadata, "/tmp/vamp", "other", "other")["status"], "unchanged")

    @patch("session_hub.shutil.which", return_value=None)
    def test_tmux_group_launch_command_raises_when_tmux_missing(self, which):
        with self.assertRaises(RuntimeError):
            session_hub.tmux_group_launch_command("vamp-s1", "/tmp/vamp", ["claude"])

    def test_tmux_group_launch_command_actually_creates_a_live_tmux_session(self):
        # Regression: string-shape assertions alone missed a real bug (env's
        # "--" placement, see prefix_env_command) that killed the tmux
        # session before it could ever attach - only running the real
        # binaries end to end catches that class of failure.
        if not (session_hub.shutil.which("tmux") and session_hub.shutil.which("gnome-terminal")):
            self.skipTest("tmux/gnome-terminal not installed")
        session_name = f"session-hub-test-{session_hub.uuid.uuid4().hex[:8]}"
        claude_args = session_hub.prefix_env_command(
            ["bash", "-c", "echo $MARKER_VAR; sleep 10"],
            {"MARKER_VAR": "it-worked"},
            None,
        )
        command = session_hub.tmux_group_launch_command(session_name, "/tmp", claude_args)
        proc = subprocess.Popen(command, start_new_session=True)
        try:
            deadline = session_hub.time.monotonic() + 5
            output = ""
            while session_hub.time.monotonic() < deadline:
                result = subprocess.run(
                    ["tmux", "capture-pane", "-t", session_name, "-p"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    output = result.stdout
                    break
                session_hub.time.sleep(0.2)
            self.assertIn("it-worked", output)
        finally:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name], capture_output=True
            )
            proc.wait(timeout=5)

    @patch("session_hub.shutil.which")
    def test_launch_with_tmux_builds_tmux_command_and_skips_pid_capture(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "tmux": "/usr/bin/tmux",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        metadata = {
            "sessions": {
                "group:/tmp/vamp#vamp-s1": {"env": {"ANTHROPIC_MODEL": "opus"}}
            },
            "settings": {"claude_danger_mode": True},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            with patch.object(session_hub.SessionHub, "spawn") as spawn:
                window.launch(
                    "Claude",
                    None,
                    "/tmp/vamp",
                    session_key="group:/tmp/vamp#vamp-s1",
                    flag_overrides={"--name": "vamp-s1"},
                    use_tmux=True,
                )
            spawn.assert_called_once()
            command = spawn.call_args.args[0]
            self.assertEqual(command[0], "bash")
            self.assertEqual(command[5], "vamp-s1")
            claude_command = command[-2]
            self.assertIn("ANTHROPIC_MODEL=opus", claude_command)
            self.assertIn("--dangerously-skip-permissions", claude_command)
            self.assertIn("--name vamp-s1", claude_command)
            self.assertNotIn("pidfile", spawn.call_args.kwargs)
        finally:
            window.close()

    def test_launch_with_tmux_requires_a_name(self):
        window = session_hub.SessionHub()
        try:
            with patch.object(session_hub.SessionHub, "spawn") as spawn, patch(
                "session_hub.QMessageBox.critical"
            ) as critical:
                window.launch("Claude", None, str(Path.home()), use_tmux=True)
            spawn.assert_not_called()
            critical.assert_called_once()
        finally:
            window.close()

    def test_read_pid_capture_file_reads_and_deletes(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "x.pid"
            pidfile.write_text("4242\n")
            pid = session_hub.read_pid_capture_file(pidfile, timeout=1)
        self.assertEqual(pid, 4242)
        self.assertFalse(pidfile.exists())

    def test_read_pid_capture_file_times_out_when_never_written(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "never.pid"
            pid = session_hub.read_pid_capture_file(pidfile, timeout=0.2)
        self.assertIsNone(pid)

    def test_process_alive_true_for_current_process_false_for_bogus_pid(self):
        self.assertTrue(session_hub.process_alive(os.getpid()))
        self.assertFalse(session_hub.process_alive(999999999999))

    def test_resolve_clear_continuations_links_old_and_new_session(self):
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / f"{os.getpid()}.json").write_text(
                json.dumps({"cwd": "/home/user/proj", "session_id": "old-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "new-id", "title", "/home/user/proj",
                    "/home/user/proj", 500_000, Path("/tmp/new.jsonl"),
                )
            ]
            metadata = {}
            with patch("session_hub.PID_DIR", pid_dir):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
            tracking = json.loads((pid_dir / f"{os.getpid()}.json").read_text())
        self.assertTrue(changed)
        self.assertEqual(tracking["session_id"], "new-id")
        (link,) = metadata["links"].values()
        self.assertEqual(set(link["members"]), {"Claude:old-id", "Claude:new-id"})
        self.assertEqual(link["active"], "Claude:new-id")

    def test_resolve_clear_continuations_copies_old_name_and_launch_overrides(self):
        # Mirrors test_link_to_existing_conversation_copies_old_name_and_launch_overrides:
        # an automatically-detected /clear should inherit the old session's
        # display name and launch env/flag overrides exactly like a manual
        # link does - both paths go through link_continuation now.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / f"{os.getpid()}.json").write_text(
                json.dumps({"cwd": "/home/user/proj", "session_id": "old-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "new-id", "title", "/home/user/proj",
                    "/home/user/proj", 500_000, Path("/tmp/new.jsonl"),
                )
            ]
            metadata = {
                "sessions": {
                    "Claude:old-id": {
                        "name": "vamp-s1",
                        "env": {"ANTHROPIC_MODEL": "opus"},
                        "flags": {"--dangerously-skip-permissions": True},
                    }
                }
            }
            with patch("session_hub.PID_DIR", pid_dir):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertTrue(changed)
        new_overrides = metadata["sessions"]["Claude:new-id"]
        self.assertEqual(new_overrides["name"], "vamp-s1")
        link_id = next(iter(metadata["links"]))
        link_overrides = metadata["sessions"][link_id]
        self.assertEqual(link_overrides["env"], {"ANTHROPIC_MODEL": "opus"})
        self.assertEqual(link_overrides["flags"], {"--dangerously-skip-permissions": True})

    def test_resolve_clear_continuations_copies_organic_title_with_no_explicit_override(self):
        # The old session was never explicitly renamed - its title is just
        # whatever Claude Code auto-generated - so there's no
        # metadata["sessions"][old_key]["name"] to copy. The new session
        # should still inherit that organic title.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / f"{os.getpid()}.json").write_text(
                json.dumps({"cwd": "/home/user/proj", "session_id": "old-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "old-id", "vampulse-orchestrator", "/home/user/proj",
                    "/home/user/proj", 100_000, Path("/tmp/old.jsonl"),
                ),
                session_hub.Session(
                    "Claude", "new-id", "Claude 3e410ca0", "/home/user/proj",
                    "/home/user/proj", 500_000, Path("/tmp/new.jsonl"),
                ),
            ]
            metadata = {}
            with patch("session_hub.PID_DIR", pid_dir):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertTrue(changed)
        self.assertEqual(
            metadata["sessions"]["Claude:new-id"]["name"], "vampulse-orchestrator"
        )

    def test_resolve_clear_continuations_extends_existing_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / f"{os.getpid()}.json").write_text(
                json.dumps({"cwd": "/home/user/proj", "session_id": "mid-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "newest-id", "title", "/home/user/proj",
                    "/home/user/proj", 500_000, Path("/tmp/newest.jsonl"),
                )
            ]
            metadata = {
                "links": {
                    "clear:existing": {
                        "members": ["Claude:old-id", "Claude:mid-id"],
                        "active": "Claude:mid-id",
                    }
                }
            }
            with patch("session_hub.PID_DIR", pid_dir):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertTrue(changed)
        self.assertEqual(len(metadata["links"]), 1)
        link = metadata["links"]["clear:existing"]
        self.assertEqual(
            set(link["members"]),
            {"Claude:old-id", "Claude:mid-id", "Claude:newest-id"},
        )
        self.assertEqual(link["active"], "Claude:newest-id")

    def test_resolve_clear_continuations_does_not_merge_unrelated_sessions_sharing_cwd(self):
        # Regression: several Session-Hub-launched processes in the SAME
        # cwd (a session group) each track their own PID -> session_id.
        # The old "whichever session in this cwd was updated last" check
        # treated every OTHER tracked PID's own current session as if it
        # were a /clear target for the first PID checked, merging entirely
        # unrelated live sessions into one - this is what corrupted a real
        # VAMPULSE group (fable/opus/sonnet rows all collapsing onto one
        # session id) even though none of them had actually /clear'd.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / "111111.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "orchestrator-old"})
            )
            (pid_dir / "222222.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "opus-old"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "orchestrator-old", "title", "/home/user/vamp",
                    "/home/user/vamp", 100_000, Path("/tmp/orch.jsonl"),
                ),
                session_hub.Session(
                    "Claude", "opus-old", "title", "/home/user/vamp",
                    "/home/user/vamp", 999_000, Path("/tmp/opus.jsonl"),
                ),
            ]
            metadata = {}
            with (
                patch("session_hub.PID_DIR", pid_dir),
                patch("session_hub.process_alive", return_value=True),
            ):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertFalse(changed)
        self.assertEqual(metadata.get("links", {}), {})

    def test_resolve_clear_continuations_does_not_absorb_an_already_named_sibling(self):
        # Regression (real incident): the orchestrator's tracked PID lost
        # track of its own session_id, and the "most recently updated,
        # unclaimed session in this cwd" guess landed on VAMPULSE-old - a
        # completely unrelated, already-named session sharing the group's
        # directory, not a fresh /clear continuation. It was neither another
        # tracked PID's own session nor a claimed group row's session_key,
        # so the older "claimed" guard alone didn't catch it. A session the
        # user has explicitly named is a deliberately distinct identity and
        # must never be silently absorbed into someone else's continuation.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / "111111.json").write_text(
                json.dumps(
                    {"cwd": "/home/user/vamp", "session_id": "orchestrator-old"}
                )
            )
            sessions = [
                session_hub.Session(
                    "Claude", "vampulse-old", "VAMPULSE-old", "/home/user/vamp",
                    "/home/user/vamp", 999_000, Path("/tmp/old.jsonl"),
                )
            ]
            metadata = {"sessions": {"Claude:vampulse-old": {"name": "VAMPULSE-old"}}}
            with (
                patch("session_hub.PID_DIR", pid_dir),
                patch("session_hub.process_alive", return_value=True),
            ):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertFalse(changed)
        self.assertEqual(metadata.get("links", {}), {})

    def test_resolve_clear_continuations_still_detects_real_clear_with_sibling_sessions_in_cwd(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / "111111.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "old-id"})
            )
            (pid_dir / "222222.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "sibling-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "sibling-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 100_000, Path("/tmp/sibling.jsonl"),
                ),
                session_hub.Session(
                    "Claude", "new-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 999_000, Path("/tmp/new.jsonl"),
                ),
            ]
            metadata = {}
            with (
                patch("session_hub.PID_DIR", pid_dir),
                patch("session_hub.process_alive", return_value=True),
            ):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertTrue(changed)
        (link,) = metadata["links"].values()
        self.assertEqual(set(link["members"]), {"Claude:old-id", "Claude:new-id"})

    def test_resolve_clear_continuations_excludes_idle_group_sibling_sessions(self):
        # The bug that actually corrupted a real VAMPULSE group: opus's row
        # had a session_key, but opus's own process had already exited (no
        # PID tracking file at all - the ordinary "idle" state for a group
        # member that isn't currently running). The live-PIDs-only claimed
        # set didn't cover that, so orchestrator's still-running PID got
        # merged into opus's session the moment opus's transcript happened
        # to be the most recently updated one in their shared cwd - which
        # is the normal state for an active session group.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / "111111.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "orchestrator-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "orchestrator-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 100_000, Path("/tmp/orch.jsonl"),
                ),
                session_hub.Session(
                    "Claude", "opus-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 999_000, Path("/tmp/opus.jsonl"),
                ),
            ]
            metadata = {
                "groups": {
                    "/home/user/vamp": {
                        "cwd": "/home/user/vamp",
                        "rows": [
                            {"name": "orchestrator", "session_key": "Claude:orchestrator-id"},
                            {"name": "opus", "session_key": "Claude:opus-id"},
                        ],
                    }
                }
            }
            with (
                patch("session_hub.PID_DIR", pid_dir),
                patch("session_hub.process_alive", return_value=True),
            ):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertFalse(changed)
        self.assertEqual(metadata.get("links", {}), {})

    def test_resolve_clear_continuations_does_not_discard_an_identified_old_session(self):
        # Regression (real incident): VAMPULSE-orchestrator's tracked PID's
        # own session_id was still perfectly valid and still present, but
        # Vampulse-sonnet1 - a totally unrelated, unnamed sibling in the
        # same group cwd - had more recent activity of its own, so the old
        # "most recently updated in this cwd, unclaimed, unnamed" guess won
        # the max() race even though the tracked session was never gone.
        # The result: orchestrator's session_id got /clear-linked into
        # sonnet1's, and every future refresh silently repointed the
        # orchestrator row at sonnet1's conversation. An old session that's
        # explicitly named or is a saved group row's own session_key must
        # never lose that race just for being the less recently active one.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / "111111.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "orchestrator-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "orchestrator-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 100_000, Path("/tmp/orch.jsonl"),
                ),
                session_hub.Session(
                    "Claude", "sonnet1-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 999_000, Path("/tmp/sonnet1.jsonl"),
                ),
            ]
            metadata = {
                "sessions": {"Claude:orchestrator-id": {"name": "VAMPULSE-orchestrator"}}
            }
            with (
                patch("session_hub.PID_DIR", pid_dir),
                patch("session_hub.process_alive", return_value=True),
            ):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertFalse(changed)
        self.assertEqual(metadata.get("links", {}), {})

    def test_resolve_clear_continuations_does_not_discard_a_group_rows_own_session(self):
        # Same incident as above, but identified via a group row's
        # session_key instead of an explicit metadata["sessions"] name -
        # this is how a freshly-launched, never-manually-renamed group
        # member's identity is actually recorded in practice.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / "111111.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "orchestrator-id"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "orchestrator-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 100_000, Path("/tmp/orch.jsonl"),
                ),
                session_hub.Session(
                    "Claude", "sonnet1-id", "title", "/home/user/vamp",
                    "/home/user/vamp", 999_000, Path("/tmp/sonnet1.jsonl"),
                ),
            ]
            metadata = {
                "groups": {
                    "/home/user/vamp": {
                        "cwd": "/home/user/vamp",
                        "rows": [
                            {
                                "name": "VAMPULSE-orchestrator",
                                "session_key": "Claude:orchestrator-id",
                            },
                        ],
                    }
                }
            }
            with (
                patch("session_hub.PID_DIR", pid_dir),
                patch("session_hub.process_alive", return_value=True),
            ):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
        self.assertFalse(changed)
        self.assertEqual(metadata.get("links", {}), {})

    def test_adopt_untracked_sessions_backfills_tracking_for_live_claude_process(self):
        with tempfile.TemporaryDirectory() as temp:
            proc_root = Path(temp) / "proc"
            pid_dir = Path(temp) / "pids"
            target_cwd = Path(temp) / "vamp"
            target_cwd.mkdir()
            pid = os.getpid()
            proc_pid_dir = proc_root / str(pid)
            proc_pid_dir.mkdir(parents=True)
            (proc_pid_dir / "cmdline").write_bytes(b"claude\x00--resume\x00")
            (proc_pid_dir / "cwd").symlink_to(target_cwd)
            sessions = [
                session_hub.Session(
                    "Claude", "live-id", "title", str(target_cwd), str(target_cwd),
                    500_000, Path("/tmp/live.jsonl"),
                )
            ]
            with (
                patch("session_hub.PROC_ROOT", proc_root),
                patch("session_hub.PID_DIR", pid_dir),
            ):
                session_hub.adopt_untracked_sessions(sessions)
            tracking = json.loads((pid_dir / f"{pid}.json").read_text())
        self.assertEqual(tracking["session_id"], "live-id")
        self.assertEqual(tracking["cwd"], str(target_cwd))

    def test_adopt_untracked_sessions_skips_already_tracked_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            proc_root = Path(temp) / "proc"
            pid_dir = Path(temp) / "pids"
            target_cwd = Path(temp) / "vamp"
            target_cwd.mkdir()
            pid = os.getpid()
            proc_pid_dir = proc_root / str(pid)
            proc_pid_dir.mkdir(parents=True)
            (proc_pid_dir / "cmdline").write_bytes(b"claude\x00--resume\x00")
            (proc_pid_dir / "cwd").symlink_to(target_cwd)
            pid_dir.mkdir()
            (pid_dir / f"{pid}.json").write_text(
                json.dumps({"cwd": str(target_cwd), "session_id": "already-tracked"})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "live-id", "title", str(target_cwd), str(target_cwd),
                    500_000, Path("/tmp/live.jsonl"),
                )
            ]
            with (
                patch("session_hub.PROC_ROOT", proc_root),
                patch("session_hub.PID_DIR", pid_dir),
            ):
                session_hub.adopt_untracked_sessions(sessions)
            tracking = json.loads((pid_dir / f"{pid}.json").read_text())
        self.assertEqual(tracking["session_id"], "already-tracked")

    def test_adopt_untracked_sessions_ignores_non_claude_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            proc_root = Path(temp) / "proc"
            pid_dir = Path(temp) / "pids"
            target_cwd = Path(temp) / "vamp"
            target_cwd.mkdir()
            pid = os.getpid()
            proc_pid_dir = proc_root / str(pid)
            proc_pid_dir.mkdir(parents=True)
            (proc_pid_dir / "cmdline").write_bytes(b"bash\x00")
            (proc_pid_dir / "cwd").symlink_to(target_cwd)
            sessions = [
                session_hub.Session(
                    "Claude", "live-id", "title", str(target_cwd), str(target_cwd),
                    500_000, Path("/tmp/live.jsonl"),
                )
            ]
            with (
                patch("session_hub.PROC_ROOT", proc_root),
                patch("session_hub.PID_DIR", pid_dir),
            ):
                session_hub.adopt_untracked_sessions(sessions)
            self.assertFalse((pid_dir / f"{pid}.json").exists())

    def test_resolve_clear_continuations_first_sighting_records_without_link(self):
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / f"{os.getpid()}.json").write_text(
                json.dumps({"cwd": "/home/user/proj", "session_id": None})
            )
            sessions = [
                session_hub.Session(
                    "Claude", "first-id", "title", "/home/user/proj",
                    "/home/user/proj", 500_000, Path("/tmp/first.jsonl"),
                )
            ]
            metadata = {}
            with patch("session_hub.PID_DIR", pid_dir):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
            tracking = json.loads((pid_dir / f"{os.getpid()}.json").read_text())
        self.assertFalse(changed)
        self.assertEqual(tracking["session_id"], "first-id")
        self.assertEqual(metadata.get("links", {}), {})

    def test_resolve_clear_continuations_applies_pending_model_on_first_sighting(self):
        # The model chosen in the New Session dialog has nowhere to live
        # until the brand new session's real native key is known - it rides
        # along in the PID tracking file (see record_hub_launch) and turns
        # into a durable ANTHROPIC_MODEL override right here, the first time
        # that key is discovered.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / f"{os.getpid()}.json").write_text(
                json.dumps(
                    {"cwd": "/home/user/proj", "session_id": None, "pending_model": "opus"}
                )
            )
            sessions = [
                session_hub.Session(
                    "Claude", "first-id", "title", "/home/user/proj",
                    "/home/user/proj", 500_000, Path("/tmp/first.jsonl"),
                )
            ]
            metadata = {}
            with patch("session_hub.PID_DIR", pid_dir):
                changed = session_hub.resolve_clear_continuations(metadata, sessions)
            tracking = json.loads((pid_dir / f"{os.getpid()}.json").read_text())
        self.assertTrue(changed)
        self.assertEqual(
            metadata["sessions"]["Claude:first-id"]["env"], {"ANTHROPIC_MODEL": "opus"}
        )
        self.assertNotIn("pending_model", tracking)

    def test_resolve_clear_continuations_cleans_up_dead_process_file(self):
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            tracking_file = pid_dir / "999999999999.json"
            tracking_file.write_text(
                json.dumps({"cwd": "/home/user/proj", "session_id": "old-id"})
            )
            with patch("session_hub.PID_DIR", pid_dir):
                changed = session_hub.resolve_clear_continuations({}, [])
            self.assertFalse(tracking_file.exists())
        self.assertFalse(changed)

    def test_name_flag_present_in_cli_flag_specs(self):
        self.assertEqual(session_hub.CLI_FLAG_SPECS["--name"]["kind"], "text")

    def test_scan_claude_file_captures_agent_name(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "-home-user-proj"
            project.mkdir()
            transcript = project / "abc.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {"type": "custom-title", "customTitle": "vamp-s1"},
                        {"type": "agent-name", "agentName": "vamp-s1"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = session_hub._scan_claude_file(transcript)
            self.assertEqual(result.get("agent_name"), "vamp-s1")

    def test_find_group_member_session_matches_by_agent_name_and_cwd(self):
        sessions = [
            session_hub.Session(
                "Claude", "id-1", "t", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/a.jsonl"), agent_name="vamp-s1",
            ),
            session_hub.Session(
                "Claude", "id-2", "t", "/tmp/other", "/tmp/other", 100,
                Path("/tmp/b.jsonl"), agent_name="vamp-s1",
            ),
        ]
        match = session_hub.find_group_member_session(
            {"name": "vamp-s1"}, "/tmp/vamp", sessions
        )
        self.assertEqual(match.session_id, "id-1")
        self.assertIsNone(
            session_hub.find_group_member_session({"name": "nope"}, "/tmp/vamp", sessions)
        )

    def test_find_group_member_session_matches_by_session_key_after_restart(self):
        # No agent_name at all (a manual restart never carries --name), but the
        # active session's linked_keys chains back to the row's old session_key -
        # see link_to_existing_conversation_for / resolve_clear_continuations.
        restarted = session_hub.Session(
            "Claude", "id-new", "t", "/tmp/vamp", "/tmp/vamp", 200, Path("/tmp/c.jsonl"),
        )
        restarted.linked_keys = ("Claude:id-old", "Claude:id-new")
        match = session_hub.find_group_member_session(
            {"name": "vamp-s1", "session_key": "Claude:id-old"}, "/tmp/vamp", [restarted]
        )
        self.assertEqual(match.session_id, "id-new")
        self.assertIsNone(
            session_hub.find_group_member_session(
                {"name": "vamp-s1", "session_key": "Claude:unrelated"}, "/tmp/vamp", [restarted]
            )
        )

    def test_find_group_member_session_filters_by_row_provider(self):
        sessions = [
            session_hub.Session(
                "Codex", "id-codex", "t", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/a.jsonl"),
            ),
            session_hub.Session(
                "Claude", "id-claude", "t", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/b.jsonl"), agent_name="vamp-s1",
            ),
        ]
        # A Claude row's --name bootstrap match must not pick up a Codex
        # session sharing the same cwd.
        claude_match = session_hub.find_group_member_session(
            {"name": "vamp-s1", "provider": "Claude"}, "/tmp/vamp", sessions
        )
        self.assertEqual(claude_match.native_key, "Claude:id-claude")
        # A Codex row's session_key must only resolve against a Codex session.
        codex_row = {
            "provider": "Codex",
            "session_key": "Codex:id-codex",
            "codex_pending_since": 0,
        }
        codex_match = session_hub.find_group_member_session(codex_row, "/tmp/vamp", sessions)
        self.assertEqual(codex_match.native_key, "Codex:id-codex")

    def test_find_group_member_session_codex_guess_respects_pending_since_and_exclusions(self):
        old_session = session_hub.Session(
            "Codex", "id-old", "t", "/tmp/vamp", "/tmp/vamp", 100, Path("/tmp/a.jsonl"),
        )
        new_session = session_hub.Session(
            "Codex", "id-new", "t", "/tmp/vamp", "/tmp/vamp", 500, Path("/tmp/b.jsonl"),
        )
        sessions = [old_session, new_session]
        row = {"provider": "Codex", "codex_pending_since": 200}
        # Only the session updated after codex_pending_since is a candidate -
        # the pre-existing unrelated one (updated_ms=100) must not be guessed.
        match = session_hub.find_group_member_session(row, "/tmp/vamp", sessions)
        self.assertEqual(match.native_key, "Codex:id-new")
        # No pending_since stamped yet (row never launched) -> no guess at all.
        self.assertIsNone(
            session_hub.find_group_member_session(
                {"provider": "Codex"}, "/tmp/vamp", sessions
            )
        )
        # Already claimed by a sibling row this same pass -> excluded.
        self.assertIsNone(
            session_hub.find_group_member_session(
                row, "/tmp/vamp", sessions, frozenset({"Codex:id-new"})
            )
        )

    def test_discover_sessions_collapses_group_members_into_one_row(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "-tmp-vamp"
            project.mkdir()
            for name, session_id in (("vamp-opus", "id-1"), ("vamp-s1", "id-2")):
                (project / f"{session_id}.jsonl").write_text(
                    "\n".join(
                        json.dumps(row)
                        for row in [
                            {"type": "custom-title", "customTitle": name},
                            {"type": "agent-name", "agentName": name},
                            {"type": "user", "cwd": "/tmp/vamp"},
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
            metadata = {
                "settings": {"enable_codex": False, "enable_antigravity": False},
                "sessions": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-opus", "override_key": "group:/tmp/vamp#vamp-opus"},
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"},
                        ],
                    }
                },
            }
            with (
                patch.object(session_hub, "CLAUDE_PROJECTS", Path(temp)),
                patch.object(session_hub, "CODEX_SESSIONS", Path(temp) / "none"),
                patch.object(
                    session_hub, "ANTIGRAVITY_CONVERSATIONS", Path(temp) / "none"
                ),
                patch("session_hub.claude_history_index", return_value={}),
                patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
            ):
                sessions = session_hub.discover_sessions(metadata)
            self.assertEqual(len(sessions), 1)
            self.assertTrue(sessions[0].session_id.startswith("group:"))
            self.assertEqual(sessions[0].cwd, "/tmp/vamp")
            self.assertEqual(
                metadata["groups"]["/tmp/vamp"]["rows"][0]["session_key"], "Claude:id-1"
            )
            self.assertEqual(
                metadata["groups"]["/tmp/vamp"]["rows"][1]["session_key"], "Claude:id-2"
            )

    def test_is_group_session(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            group_session = session_hub.Session(
                "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                100, Path("/tmp/vamp"),
            )
            real_session = session_hub.Session(
                "Claude", "abc-123", "t", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/a.jsonl"),
            )
            self.assertTrue(window.is_group_session(group_session))
            self.assertFalse(window.is_group_session(real_session))
        finally:
            window.close()

    def test_resume_selected_opens_manage_group_for_group_session(self):
        metadata = {
            "sessions": {},
            "settings": {},
            "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            group_session = session_hub.Session(
                "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                100, Path("/tmp/vamp"),
            )
            with (
                patch.object(window, "selected", return_value=group_session),
                patch.object(session_hub.SessionHub, "manage_group") as manage,
                patch.object(session_hub.SessionHub, "launch") as launch,
            ):
                window.resume_selected()
            manage.assert_called_once()
            launch.assert_not_called()
        finally:
            window.close()

    def test_context_menu_actions_are_group_specific_for_group_session(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            group_session = session_hub.Session(
                "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                100, Path("/tmp/vamp"),
            )
            with patch.object(window, "selected", return_value=group_session):
                labels = [label for label, _ in window.context_menu_actions()]
            self.assertEqual(
                labels,
                [
                    "Manage group…",
                    "Group launch options…",
                    "Rename group",
                    "Change directory",
                    "Delete group",
                ],
            )
        finally:
            window.close()

    def test_context_menu_group_launch_options_targets_the_right_click_groups_cwd(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            group_session = session_hub.Session(
                "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                100, Path("/tmp/vamp"),
            )
            with patch.object(window, "selected", return_value=group_session):
                actions = dict(window.context_menu_actions())
            with patch.object(window, "edit_group_launch_options") as edit:
                actions["Group launch options…"]()
            edit.assert_called_once_with("/tmp/vamp")
        finally:
            window.close()

    def test_rename_group_updates_display_name(self):
        metadata = {
            "sessions": {},
            "settings": {},
            "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
        }
        with tempfile.TemporaryDirectory() as temp:
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                group_session = session_hub.Session(
                    "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/vamp"),
                )
                with (
                    patch.object(window, "selected", return_value=group_session),
                    patch(
                        "session_hub.QInputDialog.getText",
                        return_value=("VAMPULSE team", True),
                    ),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.rename_group()
                self.assertEqual(
                    window.metadata["groups"]["/tmp/vamp"]["display_name"],
                    "VAMPULSE team",
                )
            finally:
                window.close()

    def test_change_group_directory_rekeys_group_and_overrides_members(self):
        metadata = {
            "sessions": {},
            "settings": {},
            "groups": {
                "/tmp/vamp": {
                    "cwd": "/tmp/vamp",
                    "rows": [
                        {"name": "vamp-opus", "override_key": "group:/tmp/vamp#vamp-opus"},
                        {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"},
                    ],
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                group_session = session_hub.Session(
                    "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/vamp"),
                )
                with (
                    patch.object(window, "selected", return_value=group_session),
                    patch(
                        "session_hub.QFileDialog.getExistingDirectory",
                        return_value="/tmp/vamp-new",
                    ),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.change_group_directory()
                self.assertNotIn("/tmp/vamp", window.metadata["groups"])
                new_group = window.metadata["groups"]["/tmp/vamp-new"]
                self.assertEqual(new_group["cwd"], "/tmp/vamp-new")
                self.assertEqual(
                    window.metadata["sessions"]["group:/tmp/vamp#vamp-opus"]["cwd"],
                    "/tmp/vamp-new",
                )
                self.assertEqual(
                    window.metadata["sessions"]["group:/tmp/vamp#vamp-s1"]["cwd"],
                    "/tmp/vamp-new",
                )
            finally:
                window.close()

    def test_change_group_directory_refuses_when_target_already_a_group(self):
        metadata = {
            "sessions": {},
            "settings": {},
            "groups": {
                "/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []},
                "/tmp/other": {"cwd": "/tmp/other", "rows": []},
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                group_session = session_hub.Session(
                    "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/vamp"),
                )
                with (
                    patch.object(window, "selected", return_value=group_session),
                    patch(
                        "session_hub.QFileDialog.getExistingDirectory",
                        return_value="/tmp/other",
                    ),
                    patch("session_hub.QMessageBox.warning") as warning,
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.change_group_directory()
                warning.assert_called_once()
                self.assertIn("/tmp/vamp", window.metadata["groups"])
            finally:
                window.close()

    def test_delete_group_trashes_members_and_removes_group(self):
        metadata = {
            "sessions": {},
            "settings": {},
            "groups": {
                "/tmp/vamp": {
                    "cwd": "/tmp/vamp",
                    "rows": [{"name": "vamp-opus", "model": "opus", "launched": True}],
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                group_session = session_hub.Session(
                    "Claude", "group:/tmp/vamp", "vamp", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/vamp"),
                )
                live = session_hub.Session(
                    "Claude", "abc-123", "t", "/tmp/vamp", "/tmp/vamp", 100,
                    Path("/tmp/a.jsonl"), agent_name="vamp-opus",
                )
                with (
                    patch.object(window, "selected", return_value=group_session),
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch.object(session_hub.SessionHub, "move_session_to_trash") as trash,
                    patch(
                        "session_hub.QMessageBox.warning",
                        return_value=session_hub.QMessageBox.StandardButton.Yes,
                    ),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.delete_group()
                trash.assert_called_once_with(live)
                self.assertNotIn("/tmp/vamp", window.metadata["groups"])
            finally:
                window.close()

    @patch("session_hub.threading.Thread")
    @patch("session_hub.subprocess.Popen")
    def test_spawn_skips_focus_thread_when_focus_false(self, popen, thread):
        window = session_hub.SessionHub()
        try:
            window.spawn(
                ["gnome-terminal", "--title=Claude — session-hub", "--"],
                focus=False,
            )
        finally:
            window.close()
        thread.assert_not_called()

    def test_launch_env_strips_requested_keys(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            with patch.dict(
                session_hub.os.environ, {"CLAUDE_CODE_CHILD_SESSION": "1"}
            ):
                env = window.launch_env(strip=["CLAUDE_CODE_CHILD_SESSION"])
                self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", env)
                self.assertIsNone(window.launch_env())
        finally:
            window.close()

    def _group_metadata(self):
        return {
            "sessions": {},
            "settings": {
                "global_env": {"ANTHROPIC_MODEL": "sonnet"},
                "global_flags": {"--effort": "low"},
            },
            "groups": {
                "/tmp/vamp": {
                    "cwd": "/tmp/vamp",
                    "env": {"ANTHROPIC_MODEL": "opus"},
                    "flags": {"--effort": "high"},
                    "rows": [
                        {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                    ],
                }
            },
        }

    def test_group_cwd_for_session_key_resolves_both_key_shapes(self):
        metadata = self._group_metadata()
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertEqual(
                window.group_cwd_for_session_key("group:/tmp/vamp#vamp-s1"), "/tmp/vamp"
            )
            self.assertEqual(
                window.group_cwd_for_session_key("Claude:group:/tmp/vamp"), "/tmp/vamp"
            )
            self.assertIsNone(window.group_cwd_for_session_key("Claude:abc123"))
            self.assertIsNone(window.group_cwd_for_session_key(None))
        finally:
            window.close()

    def test_effective_model_falls_back_global_then_group_then_session(self):
        metadata = self._group_metadata()
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            # No session-level override yet: group's model wins over global.
            self.assertEqual(window.effective_model("group:/tmp/vamp#vamp-s1"), "opus")
            # A row's own override still wins over the group.
            metadata["sessions"]["group:/tmp/vamp#vamp-s1"] = {
                "env": {"ANTHROPIC_MODEL": "fable"}
            }
            self.assertEqual(window.effective_model("group:/tmp/vamp#vamp-s1"), "fable")
            # Ungrouped session key: only global applies.
            self.assertEqual(window.effective_model("Claude:abc123"), "sonnet")
        finally:
            window.close()

    def test_effective_model_does_not_leak_claude_global_onto_codex(self):
        metadata = self._group_metadata()
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            # global_env's ANTHROPIC_MODEL ("sonnet") is Claude-scoped and
            # must not leak onto a Codex session just because no provider
            # was passed in.
            self.assertIsNone(window.effective_model("Codex:abc123", "Codex"))
            self.assertIsNone(window.effective_model("Codex:abc123", "Antigravity"))
            metadata["sessions"]["Codex:abc123"] = {"model": "gpt-5"}
            self.assertEqual(window.effective_model("Codex:abc123", "Codex"), "gpt-5")
        finally:
            window.close()

    def test_launch_env_and_flags_apply_group_tier_between_global_and_session(self):
        metadata = self._group_metadata()
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            env = window.launch_env("group:/tmp/vamp#vamp-s1")
            self.assertEqual(env["ANTHROPIC_MODEL"], "opus")
            flags = window.launch_flags("group:/tmp/vamp#vamp-s1")
            self.assertEqual(flags, ["--effort", "high"])

            metadata["sessions"]["group:/tmp/vamp#vamp-s1"] = {
                "env": {"ANTHROPIC_MODEL": "fable"},
                "flags": {"--effort": "medium"},
            }
            env = window.launch_env("group:/tmp/vamp#vamp-s1")
            self.assertEqual(env["ANTHROPIC_MODEL"], "fable")
            flags = window.launch_flags("group:/tmp/vamp#vamp-s1")
            self.assertEqual(flags, ["--effort", "medium"])
        finally:
            window.close()

    def test_edit_group_launch_options_persists_env_and_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                dialog_instance = session_hub.SessionLaunchOptionsDialog.__new__(
                    session_hub.SessionLaunchOptionsDialog
                )
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                dialog_instance.env = MagicMock(return_value={"ANTHROPIC_MODEL": "opus"})
                dialog_instance.flags = MagicMock(return_value={"--effort": "high"})
                with (
                    patch(
                        "session_hub.SessionLaunchOptionsDialog",
                        return_value=dialog_instance,
                    ) as dialog_cls,
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.edit_group_launch_options("/tmp/vamp")
                self.assertEqual(dialog_cls.call_args.kwargs["scope"], "this group")
                group = window.metadata["groups"]["/tmp/vamp"]
                self.assertEqual(group["env"], {"ANTHROPIC_MODEL": "opus"})
                self.assertEqual(group["flags"], {"--effort": "high"})
            finally:
                window.close()

    def test_edit_session_launch_options_offers_tmux_for_an_independent_session(self):
        # Group members already have their own tmux opt-in (ManageGroupDialog's
        # "Launch in tmux" checkbox) - this dialog only needs to offer it for
        # sessions that aren't part of any saved group.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Claude", "id-1", "Independent", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/a.jsonl"),
                )
                dialog_instance = session_hub.SessionLaunchOptionsDialog.__new__(
                    session_hub.SessionLaunchOptionsDialog
                )
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                dialog_instance.env = MagicMock(return_value={})
                dialog_instance.flags = MagicMock(return_value={"--name": "indie"})
                dialog_instance.tmux = MagicMock(return_value=True)
                with (
                    patch(
                        "session_hub.SessionLaunchOptionsDialog",
                        return_value=dialog_instance,
                    ) as dialog_cls,
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.edit_session_launch_options_for(session)
                self.assertTrue(dialog_cls.call_args.kwargs["show_tmux"])
                self.assertEqual(
                    window.metadata["sessions"]["Claude:id-1"]["tmux"], True
                )
            finally:
                window.close()

    def test_edit_session_launch_options_for_codex_reads_and_writes_model(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {"Codex:id-1": {"model": "o3"}},
                "settings": {},
                "groups": {},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Codex", "id-1", "Independent", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/a.jsonl"),
                )
                dialog_instance = session_hub.SessionLaunchOptionsDialog.__new__(
                    session_hub.SessionLaunchOptionsDialog
                )
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                dialog_instance.env = MagicMock(return_value={})
                dialog_instance.flags = MagicMock(return_value={})
                dialog_instance.tmux = MagicMock(return_value=False)
                dialog_instance.model = MagicMock(return_value="gpt-5")
                dialog_instance.reasoning_effort = MagicMock(return_value="high")
                with (
                    patch(
                        "session_hub.SessionLaunchOptionsDialog",
                        return_value=dialog_instance,
                    ) as dialog_cls,
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.edit_session_launch_options_for(session)
                # Constructed with the session's existing model as the seed.
                self.assertEqual(dialog_cls.call_args.kwargs["provider"], "Codex")
                self.assertEqual(dialog_cls.call_args.kwargs["model"], "o3")
                self.assertEqual(
                    window.metadata["sessions"]["Codex:id-1"]["model"], "gpt-5"
                )
                self.assertEqual(
                    window.metadata["sessions"]["Codex:id-1"]["reasoning_effort"], "high"
                )
            finally:
                window.close()

    def test_resume_session_launches_via_tmux_when_session_opted_in(self):
        metadata = {
            "sessions": {
                "Claude:id-1": {"tmux": True, "flags": {"--name": "indie"}}
            },
            "settings": {},
            "groups": {},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            session = session_hub.Session(
                "Claude", "id-1", "Independent", "/tmp/vamp", "/tmp/vamp",
                100, Path("/tmp/a.jsonl"),
            )
            with patch.object(window, "launch") as launch:
                window.resume_session(session)
            self.assertTrue(launch.call_args.kwargs["use_tmux"])
            self.assertEqual(launch.call_args.kwargs["tmux_name"], "indie")
        finally:
            window.close()

    def test_resume_session_passes_stored_codex_model(self):
        metadata = {
            "sessions": {"Codex:id-1": {"model": "gpt-5"}},
            "settings": {},
            "groups": {},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            session = session_hub.Session(
                "Codex", "id-1", "Independent", "/tmp/vamp", "/tmp/vamp",
                100, Path("/tmp/a.jsonl"),
            )
            with patch.object(window, "launch") as launch:
                window.resume_session(session)
            self.assertEqual(launch.call_args.kwargs["model"], "gpt-5")
        finally:
            window.close()

    def test_manage_group_dialog_selection_mode_allows_ctrl_click_multi_select(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    self.assertEqual(
                        dialog.table.selectionMode(),
                        session_hub.QTableWidget.SelectionMode.ExtendedSelection,
                    )
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_manage_group_dialog_launch_selected_rows_launches_each_selection(self):
        hub = MagicMock()
        dialog = session_hub.ManageGroupDialog.__new__(session_hub.ManageGroupDialog)
        dialog.hub = hub
        dialog.cwd = "/tmp/vamp"
        dialog.reload = MagicMock()
        dialog.pair_at_table_row = MagicMock(
            side_effect=[
                ({"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}, None),
                (
                    {"name": "vamp-s2", "override_key": "group:/tmp/vamp#vamp-s2"},
                    MagicMock(),
                ),
            ]
        )
        index0, index1 = MagicMock(), MagicMock()
        index0.row.return_value = 0
        index1.row.return_value = 1
        selection_model = MagicMock()
        selection_model.selectedRows.return_value = [index0, index1]
        dialog.table = MagicMock()
        dialog.table.selectionModel.return_value = selection_model

        dialog.launch_selected_rows()

        hub.launch_group_row.assert_called_once_with("/tmp/vamp", "vamp-s1")
        hub.resume_group_row.assert_called_once_with("/tmp/vamp", "vamp-s2")
        hub.refresh.assert_called_once()
        dialog.reload.assert_called_once_with(
            select_override_keys={
                "group:/tmp/vamp#vamp-s1",
                "group:/tmp/vamp#vamp-s2",
            }
        )

    def test_manage_group_dialog_launch_selected_rows_does_nothing_when_empty(self):
        hub = MagicMock()
        dialog = session_hub.ManageGroupDialog.__new__(session_hub.ManageGroupDialog)
        dialog.hub = hub
        dialog.reload = MagicMock()
        selection_model = MagicMock()
        selection_model.selectedRows.return_value = []
        dialog.table = MagicMock()
        dialog.table.selectionModel.return_value = selection_model

        dialog.launch_selected_rows()

        hub.launch_group_row.assert_not_called()
        hub.resume_group_row.assert_not_called()
        hub.refresh.assert_not_called()
        dialog.reload.assert_not_called()

    def test_manage_group_dialog_launch_row_delegates_to_hub(self):
        hub = MagicMock()
        dialog = session_hub.ManageGroupDialog.__new__(session_hub.ManageGroupDialog)
        dialog.hub = hub
        dialog.cwd = "/tmp/vamp"
        dialog.reload = MagicMock()
        dialog.group = MagicMock(
            return_value={
                "rows": [{"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}]
            }
        )
        dialog.launch_row("vamp-s1")
        hub.launch_group_row.assert_called_once_with("/tmp/vamp", "vamp-s1")
        hub.refresh.assert_called_once()
        dialog.reload.assert_called_once_with(
            select_override_keys={"group:/tmp/vamp#vamp-s1"}
        )

    def test_manage_group_dialog_table_disables_double_click_edit(self):
        # Double-click is wired to launch/resume (see launch_or_resume_row) -
        # it must not also open the cell for inline text editing, which read
        # like an accidental rename.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    self.assertEqual(
                        dialog.table.editTriggers(),
                        session_hub.QTableWidget.EditTrigger.NoEditTriggers,
                    )
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_row_context_menu_uses_override_key_for_shared_actions(self):
        # "Launch options..." (and every other shared action) must resolve
        # the SAME override bucket the Model column reads from
        # (effective_model keyed by override_key) - otherwise the column
        # shows a model nothing in the menu can ever edit.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.session_is_tracked_alive", return_value=False),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with (
                        patch.object(
                            window, "context_menu_actions", return_value=[]
                        ) as actions,
                        patch("session_hub.QMenu") as menu_cls,
                        patch("session_hub.claude_sessions", return_value=[live]),
                    ):
                        menu_cls.return_value = MagicMock()
                        item = dialog.table.item(0, 0)
                        point = dialog.table.visualItemRect(item).center()
                        dialog.row_context_menu(point)
                    session_arg = actions.call_args[0][0]
                    self.assertEqual(session_arg.key, "group:/tmp/vamp#vamp-s1")
                    menu = menu_cls.return_value
                    labels = [call.args[0].text() for call in menu.addAction.call_args_list]
                    self.assertEqual(labels, ["Remove from group"])
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_remove_row_preserves_model_override_on_ungrouped_session(self):
        # Regression: removing a member from a group used to just drop its
        # row and leave the model/env override sitting under the row's
        # override_key - the ungrouped session (now keyed by its own native
        # key) silently reverted to "Default" the moment it left the group.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {
                    "group:/tmp/vamp#vamp-orchestrator": {
                        "env": {"ANTHROPIC_MODEL": "opus"}
                    }
                },
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {
                                "name": "vamp-orchestrator",
                                "override_key": "group:/tmp/vamp#vamp-orchestrator",
                            }
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-orchestrator",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.session_is_tracked_alive", return_value=False),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with (
                        patch("session_hub.claude_sessions", return_value=[live]),
                        patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                    ):
                        dialog.remove_row("vamp-orchestrator")
                    self.assertEqual(
                        window.metadata["sessions"]["Claude:abc123"]["env"],
                        {"ANTHROPIC_MODEL": "opus"},
                    )
                    self.assertEqual(window.metadata["groups"]["/tmp/vamp"]["rows"], [])
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_row_context_menu_session_has_linked_keys_populated(self):
        # Regression: row_context_menu used to re-derive its own `match` via
        # a direct find_group_member_session() call instead of going
        # through matched_sessions() - the one place that fills in
        # linked_keys from metadata["links"]. That meant "Open linked
        # conversation..." always came back empty for a group row's
        # session even when metadata["links"] genuinely had an entry for
        # it, because the session object the menu action actually received
        # never had linked_keys set in the first place.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:abc": {
                        "members": ["Claude:abc123", "Claude:id-old"],
                        "active": "Claude:abc123",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.session_is_tracked_alive", return_value=False),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with (
                        patch.object(
                            window, "context_menu_actions", return_value=[]
                        ) as actions,
                        patch("session_hub.QMenu") as menu_cls,
                        patch("session_hub.claude_sessions", return_value=[live]),
                    ):
                        menu_cls.return_value = MagicMock()
                        item = dialog.table.item(0, 0)
                        point = dialog.table.visualItemRect(item).center()
                        dialog.row_context_menu(point)
                    session_arg = actions.call_args[0][0]
                    self.assertEqual(
                        set(session_arg.linked_keys), {"Claude:abc123", "Claude:id-old"}
                    )
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_matched_sessions_prefers_override_key_name_over_native_key(self):
        # Rename (routed through row_session, see row_context_menu) writes
        # the display name under the row's override_key. A stale native-key
        # override left over from an old restart or a manual link must not
        # shadow it - override_key is the row's stable identity, the native
        # key changes every restart.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {
                    "Claude:abc123": {"name": "stale leftover name"},
                    "group:/tmp/vamp#vamp-s1": {"name": "VAMPULSE-orchestrator"},
                },
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with patch("session_hub.claude_sessions", return_value=[live]):
                        pairs = dialog.matched_sessions()
                    self.assertEqual(pairs[0][1].title, "VAMPULSE-orchestrator")
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_matched_sessions_populates_linked_keys_from_metadata_links(self):
        # claude_sessions() is raw and never applies metadata["links"], so
        # without this, session.linked_keys is always empty for a group row
        # and "Open linked conversation..." can never find anything.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:abc": {
                        "members": ["Claude:abc123", "Claude:id-old"],
                        "active": "Claude:abc123",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with patch("session_hub.claude_sessions", return_value=[live]):
                        pairs = dialog.matched_sessions()
                    self.assertEqual(
                        set(pairs[0][1].linked_keys),
                        {"Claude:abc123", "Claude:id-old"},
                    )
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_open_linked_conversation_for_group_row_finds_old_session(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:abc": {
                        "members": ["Claude:abc123", "Claude:id-old"],
                        "active": "Claude:abc123",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            old = session_hub.Session(
                "Claude", "id-old", "vampulse-orchestrator", "/tmp/vamp", "/tmp/vamp",
                50, Path("/tmp/old.jsonl"),
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with patch("session_hub.claude_sessions", return_value=[live]):
                        row, match = dialog.matched_sessions()[0]
                    session = dialog.row_session(row, match)
                    with (
                        patch(
                            "session_hub.native_session_index",
                            return_value={live.native_key: live, old.native_key: old},
                        ),
                        patch(
                            "session_hub.QInputDialog.getItem",
                            return_value=("Claude — vampulse-orchestrator  [id-old]", False),
                        ) as getitem,
                    ):
                        window.open_linked_conversation_for(session)
                    labels = getitem.call_args[0][3]
                    self.assertIn("Claude — vampulse-orchestrator  [id-old]", labels)
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_manage_group_dialog_default_column_order_puts_status_first_and_agent_last(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    header = dialog.table.horizontalHeader()
                    last = dialog.table.columnCount() - 1
                    self.assertEqual(header.logicalIndex(0), dialog.STATUS_COLUMN)
                    self.assertEqual(
                        header.logicalIndex(last - 1),
                        dialog.SHARED_COLUMNS.index("Agent"),
                    )
                    self.assertEqual(header.logicalIndex(last), dialog.SESSION_ID_COLUMN)
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_manage_group_dialog_columns_stay_movable_after_restoring_saved_state(self):
        # QHeaderView.restoreState() also restores whether sections are
        # movable, so a blob saved before drag-reordering existed would
        # otherwise silently turn dragging back off on every open.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"):
                    scratch = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                    scratch.table.horizontalHeader().setSectionsMovable(False)
                    stale_state = session_hub.column_widths_state(scratch.table)
                    scratch.close()
                    window.metadata["settings"]["group_table_columns_v2"] = stale_state
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    self.assertTrue(dialog.table.horizontalHeader().sectionsMovable())
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_manage_group_dialog_double_click_launches_unmatched_row(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with (
                        patch.object(window, "launch_group_row") as launch_group_row,
                        patch.object(window, "resume_group_row") as resume_group_row,
                        patch("session_hub.claude_sessions", return_value=[]),
                    ):
                        index = dialog.table.model().index(0, 0)
                        dialog.launch_or_resume_row(index)
                    launch_group_row.assert_called_once_with("/tmp/vamp", "vamp-s1")
                    resume_group_row.assert_not_called()
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_manage_group_dialog_double_click_resumes_matched_row(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.session_is_tracked_alive", return_value=False),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    with (
                        patch.object(window, "launch_group_row") as launch_group_row,
                        patch.object(window, "resume_group_row") as resume_group_row,
                        patch("session_hub.claude_sessions", return_value=[live]),
                    ):
                        index = dialog.table.model().index(0, 0)
                        dialog.launch_or_resume_row(index)
                    resume_group_row.assert_called_once_with("/tmp/vamp", "vamp-s1")
                    launch_group_row.assert_not_called()
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_manage_group_dialog_double_click_keeps_selection_on_the_launched_row(self):
        # Regression: launching a row changes its "Last updated" timestamp,
        # which re-sorts the table, and populate_session_table() replaces
        # every QTableWidgetItem - without reload() reselecting by
        # override_key, the highlight was left on whatever row ended up in
        # that visual slot next, not the row the user actually launched.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"},
                            {"name": "vamp-s2", "override_key": "group:/tmp/vamp#vamp-s2"},
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "raw title", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.session_is_tracked_alive", return_value=False),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                    patch("session_hub.read_metadata", return_value=metadata),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                    try:
                        row_of = {
                            dialog.table.item(r, 0).data(
                                session_hub.Qt.ItemDataRole.UserRole + 1
                            ): r
                            for r in range(dialog.table.rowCount())
                        }
                        index = dialog.table.model().index(
                            row_of["group:/tmp/vamp#vamp-s1"], 0
                        )
                        with patch.object(window, "resume_group_row"):
                            dialog.launch_or_resume_row(index)
                        selected_keys = {
                            dialog.table.item(idx.row(), 0).data(
                                session_hub.Qt.ItemDataRole.UserRole + 1
                            )
                            for idx in dialog.table.selectionModel().selectedRows()
                        }
                        self.assertEqual(selected_keys, {"group:/tmp/vamp#vamp-s1"})
                    finally:
                        dialog.close()
            finally:
                window.close()

    def test_launch_group_row_strips_env_when_transcripts_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {
                                "name": "vamp-s1",
                                "override_key": "group:/tmp/vamp#vamp-s1",
                                "transcripts": True,
                            }
                        ],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.launch_group_row("/tmp/vamp", "vamp-s1")
                launch.assert_called_once_with(
                    "Claude",
                    None,
                    "/tmp/vamp",
                    model=None,
                    reasoning_effort=None,
                    session_key="group:/tmp/vamp#vamp-s1",
                    flag_overrides={"--name": "vamp-s1"},
                    strip_env=["CLAUDE_CODE_CHILD_SESSION"],
                    wait_for_tracking=False,
                    use_tmux=False,
                )
                self.assertEqual(result, {"status": "launched", "name": "vamp-s1"})
            finally:
                window.close()

    def test_launch_group_row_keeps_env_when_transcripts_unchecked(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {
                                "name": "vamp-s1",
                                "override_key": "group:/tmp/vamp#vamp-s1",
                                "transcripts": False,
                            }
                        ],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.launch_group_row("/tmp/vamp", "vamp-s1")
                launch.assert_called_once_with(
                    "Claude",
                    None,
                    "/tmp/vamp",
                    model=None,
                    reasoning_effort=None,
                    session_key="group:/tmp/vamp#vamp-s1",
                    flag_overrides={"--name": "vamp-s1"},
                    strip_env=None,
                    wait_for_tracking=False,
                    use_tmux=False,
                )
            finally:
                window.close()

    def test_launch_group_row_reports_error_for_unknown_group_or_row(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"):
                    result = window.launch_group_row("/tmp/nowhere", "vamp-s1")
                self.assertEqual(result["status"], "error")
            finally:
                window.close()

    def test_resume_group_row_uses_tmux_when_group_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "tmux": True,
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Claude", "abc123", "vamp-s1", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[live]),
                    patch("session_hub.session_is_tracked_alive", return_value=False),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.resume_group_row("/tmp/vamp", "vamp-s1")
                launch.assert_called_once_with(
                    "Claude",
                    "abc123",
                    "/tmp/vamp",
                    "/tmp/vamp",
                    model=None,
                    reasoning_effort=None,
                    session_key="group:/tmp/vamp#vamp-s1",
                    use_tmux=True,
                    tmux_name="vamp-s1",
                )
                self.assertEqual(result, {"status": "resumed", "name": "vamp-s1"})
            finally:
                window.close()

    def test_resume_group_row_passes_stored_codex_model(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {"group:/tmp/vamp#vamp-codex": {"model": "gpt-5"}},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {
                                "name": "vamp-codex",
                                "provider": "Codex",
                                "override_key": "group:/tmp/vamp#vamp-codex",
                                "session_key": "Codex:abc123",
                            }
                        ],
                    }
                },
            }
            live = session_hub.Session(
                "Codex", "abc123", "vamp-codex", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/x.jsonl"),
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.codex_sessions", return_value=[live]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.resume_group_row("/tmp/vamp", "vamp-codex")
                self.assertEqual(launch.call_args.args[0], "Codex")
                self.assertEqual(launch.call_args.kwargs["model"], "gpt-5")
            finally:
                window.close()

    def test_resume_group_row_errors_when_row_has_no_history(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.resume_group_row("/tmp/vamp", "vamp-s1")
                launch.assert_not_called()
                self.assertEqual(result["status"], "error")
            finally:
                window.close()

    def test_launch_group_row_cli_prints_json_and_exits_nonzero_on_error(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with (
                patch("session_hub.read_metadata", return_value=metadata),
                patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = session_hub.launch_group_row_cli(
                        ["session_hub.py", "--launch-group-row", "/tmp/nowhere", "vamp-s1"]
                    )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(buffer.getvalue())["status"], "error")

    def test_suggest_session_name_first_then_numbered(self):
        directory = Path("/home/user/Dropbox/Backups/projects/VAMPULSE-game")
        first = session_hub.suggest_session_name(directory, "sonnet", set())
        self.assertEqual(first, "VAMPULSE-game-sonnet")
        second = session_hub.suggest_session_name(directory, "sonnet", {first})
        self.assertEqual(second, "VAMPULSE-game-sonnet-2")
        third = session_hub.suggest_session_name(directory, "sonnet", {first, second})
        self.assertEqual(third, "VAMPULSE-game-sonnet-3")

    def test_launch_new_group_sessions_dialog_add_remove_rows_and_suggests_names(self):
        with tempfile.TemporaryDirectory() as temp:
            dialog = session_hub.LaunchNewGroupSessionsDialog(temp, set(), False)
            self.assertEqual(dialog.table.rowCount(), 1)
            dialog.add_row()
            self.assertEqual(dialog.table.rowCount(), 2)
            rows = dialog.rows()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["name"], rows[1]["name"])
            dialog.table.selectRow(1)
            dialog.remove_selected_rows()
            self.assertEqual(dialog.table.rowCount(), 1)
            dialog.close()

    def test_launch_new_group_sessions_dialog_tmux_checkbox_carries_into_accept(self):
        dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
        self.assertFalse(dialog.tmux_checkbox.isChecked())
        dialog.tmux_checkbox.setChecked(True)
        dialog.accept()
        self.assertTrue(dialog.use_tmux)
        dialog.close()

    def test_manage_group_dialog_tmux_checkbox_persists_to_group(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    self.assertFalse(dialog.tmux_checkbox.isChecked())
                    with patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ):
                        dialog.tmux_checkbox.setChecked(True)
                    self.assertTrue(window.metadata["groups"]["/tmp/vamp"]["tmux"])
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_manage_group_dialog_migrate_rows_backfills_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "vamp-s1", "override_key": "group:/tmp/vamp#vamp-s1"}
                        ],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    row = window.metadata["groups"]["/tmp/vamp"]["rows"][0]
                    self.assertEqual(row["provider"], "Claude")
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_launch_new_group_sessions_dialog_rejects_duplicate_names(self):
        dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
        dialog.add_row()
        name_edit_0 = dialog.table.cellWidget(0, 3)
        name_edit_1 = dialog.table.cellWidget(1, 3)
        name_edit_0.setText("same-name")
        name_edit_0.auto_suggested = False
        name_edit_1.setText("same-name")
        name_edit_1.auto_suggested = False
        with patch("session_hub.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.group_rows, [])
        dialog.close()

    def test_launch_new_group_sessions_dialog_rejects_name_already_in_group(self):
        dialog = session_hub.LaunchNewGroupSessionsDialog(
            "/tmp/vamp", {"vampulse-fable"}, False
        )
        name_edit = dialog.table.cellWidget(0, 3)
        name_edit.setText("vampulse-fable")
        name_edit.auto_suggested = False
        with patch("session_hub.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.group_rows, [])
        dialog.close()

    def test_launch_new_group_sessions_dialog_per_row_provider_switch(self):
        with patch("session_hub.codex_models", return_value=self._codex_models_fixture()):
            dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
            provider_combo = dialog.table.cellWidget(0, 0)
            self.assertEqual(provider_combo.currentData(), "Claude")
            self.assertIsInstance(dialog.table.cellWidget(0, 1), session_hub.QComboBox)
            self.assertIsInstance(dialog.table.cellWidget(0, 2), session_hub.QLabel)
            provider_combo.setCurrentIndex(provider_combo.findData("Codex"))
            self.assertIsInstance(dialog.table.cellWidget(0, 1), session_hub.QComboBox)
            self.assertIsInstance(dialog.table.cellWidget(0, 2), session_hub.QComboBox)
            model_combo = dialog.table.cellWidget(0, 1)
            model_combo.setCurrentIndex(model_combo.findData("gpt-5.6-sol"))
            effort_combo = dialog.table.cellWidget(0, 2)
            self.assertEqual(
                [effort_combo.itemData(i) for i in range(effort_combo.count())],
                [None, "low", "medium", "high"],
            )
            effort_combo.setCurrentIndex(effort_combo.findData("medium"))
            name_edit = dialog.table.cellWidget(0, 3)
            name_edit.setText("vampulse-codex")
            name_edit.auto_suggested = False
            rows = dialog.rows()
            self.assertEqual(
                rows[0],
                {
                    "name": "vampulse-codex",
                    "provider": "Codex",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "medium",
                },
            )
            # Switching a Claude row's combo back keeps the existing combo behavior.
            provider_combo.setCurrentIndex(provider_combo.findData("Claude"))
            self.assertIsInstance(dialog.table.cellWidget(0, 1), session_hub.QComboBox)
            self.assertIsInstance(dialog.table.cellWidget(0, 2), session_hub.QLabel)
            dialog.close()

    def test_launch_new_rows_into_group_launches_all_rows_and_saves_group(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                rows = [
                    {"name": "vampulse-fable", "model": "fable"},
                    {"name": "vampulse-sonnet", "model": "sonnet"},
                ]
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.launch_new_rows_into_group(temp, rows, False)
                self.assertEqual(launch.call_count, 2)
                for call in launch.call_args_list:
                    self.assertFalse(call.kwargs["focus"])
                saved = window.metadata["groups"][temp]
                self.assertEqual(
                    {row["name"] for row in saved["rows"]},
                    {"vampulse-fable", "vampulse-sonnet"},
                )
            finally:
                window.close()

    def test_launch_new_rows_into_group_launches_codex_row_with_its_own_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                rows = [{"name": "vampulse-codex", "provider": "Codex", "model": "gpt-5"}]
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.launch_new_rows_into_group(temp, rows, False)
                launch.assert_called_once()
                call_args = launch.call_args
                self.assertEqual(call_args.args[0], "Codex")
                self.assertEqual(call_args.kwargs["model"], "gpt-5")
                saved_row = window.metadata["groups"][temp]["rows"][0]
                self.assertEqual(saved_row["provider"], "Codex")
                self.assertIn("codex_pending_since", saved_row)
                self.assertEqual(
                    window.metadata["sessions"][saved_row["override_key"]]["model"], "gpt-5"
                )
            finally:
                window.close()

    def test_launch_new_rows_into_group_merges_without_duplicating_existing_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    temp: {
                        "cwd": temp,
                        "rows": [{"name": "vampulse-fable", "model": "fable"}],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                rows = [
                    {"name": "vampulse-fable", "model": "fable"},
                    {"name": "vampulse-sonnet", "model": "sonnet"},
                ]
                with (
                    patch.object(session_hub.SessionHub, "launch"),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.launch_new_rows_into_group(temp, rows, False)
                saved = window.metadata["groups"][temp]
                self.assertEqual(len(saved["rows"]), 2)
            finally:
                window.close()

    def test_manage_group_dialog_launch_new_rows_delegates_to_hub(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    dialog = session_hub.ManageGroupDialog(window, "/tmp/vamp")
                try:
                    dialog_instance = MagicMock()
                    dialog_instance.exec.return_value = (
                        session_hub.QDialog.DialogCode.Accepted
                    )
                    dialog_instance.group_rows = [{"name": "vamp-new", "model": None}]
                    dialog_instance.use_tmux = True
                    with (
                        patch(
                            "session_hub.LaunchNewGroupSessionsDialog",
                            return_value=dialog_instance,
                        ),
                        patch.object(window, "launch_new_rows_into_group") as launch,
                    ):
                        dialog.launch_new_rows()
                    launch.assert_called_once_with(
                        "/tmp/vamp", [{"name": "vamp-new", "model": None}], True
                    )
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_add_session_to_group_new_group_creates_and_files_no_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Claude", "id-1", "vampulse-orchestrator", "/home/user/proj",
                    "/home/user/proj", 100, Path("/tmp/x.jsonl"),
                )
                dialog_instance = session_hub.MoveToGroupDialog.__new__(
                    session_hub.MoveToGroupDialog
                )
                dialog_instance.cwd = None
                dialog_instance.new_group_name = "My New Group"
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(window, "selected", return_value=session),
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.MoveToGroupDialog", return_value=dialog_instance
                    ),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.add_session_to_group()
                launch.assert_not_called()
                group = window.metadata["groups"]["/home/user/proj"]
                self.assertEqual(group["display_name"], "My New Group")
                self.assertEqual(len(group["rows"]), 1)
                self.assertEqual(group["rows"][0]["name"], "vampulse-orchestrator")
            finally:
                window.close()

    def test_file_session_into_group_stamps_provider_and_hides_codex_session(self):
        # Regression: file_session_into_group used to omit "provider" from
        # the saved row, so find_group_member_session (default "Claude")
        # never matched a filed Codex session back on the next refresh - it
        # stayed visible as a duplicate ungrouped row in the main list
        # forever instead of collapsing into the group.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Codex", "id-1", "vamp-codex", "/tmp/vamp", "/tmp/vamp",
                    100, Path("/tmp/x.jsonl"),
                )
                with (
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.codex_sessions", return_value=[session]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.file_session_into_group(session, "/tmp/vamp")
                row = window.metadata["groups"]["/tmp/vamp"]["rows"][0]
                self.assertEqual(row["provider"], "Codex")
                self.assertFalse(
                    any(s.session_id == "id-1" for s in window.sessions),
                    "Codex session should collapse into its group, not stay a duplicate row",
                )
                self.assertTrue(any(s.session_id == "group:/tmp/vamp" for s in window.sessions))
            finally:
                window.close()

    def test_add_session_to_group_new_group_reuses_existing_group_at_same_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/home/user/proj": {
                        "cwd": "/home/user/proj",
                        "rows": [{"name": "proj-fable", "model": "fable"}],
                        "display_name": "Existing",
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Claude", "id-1", "vampulse-orchestrator", "/home/user/proj",
                    "/home/user/proj", 100, Path("/tmp/x.jsonl"),
                )
                dialog_instance = session_hub.MoveToGroupDialog.__new__(
                    session_hub.MoveToGroupDialog
                )
                dialog_instance.cwd = None
                dialog_instance.new_group_name = "My New Group"
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(window, "selected", return_value=session),
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.MoveToGroupDialog", return_value=dialog_instance
                    ),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                    patch("session_hub.QMessageBox.information") as info,
                ):
                    window.add_session_to_group()
                launch.assert_not_called()
                info.assert_called_once()
                group = window.metadata["groups"]["/home/user/proj"]
                self.assertEqual(group["display_name"], "Existing")
                self.assertEqual(len(group["rows"]), 2)
            finally:
                window.close()

    def test_add_session_to_group_moves_existing_session_without_launching(self):
        # "Add session to group" on an already-running session must file it
        # into the group as-is - no new process, no Model/Name prompt (see
        # MoveToGroupDialog). Regression: this used to always launch a brand
        # new session instead of using the one you selected.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {
                    "Claude:id-1": {
                        "env": {"ANTHROPIC_MODEL": "opus"},
                        "flags": {"--dangerously-skip-permissions": True},
                    }
                },
                "settings": {},
                "groups": {
                    "/home/user/proj": {
                        "cwd": "/home/user/proj",
                        "rows": [{"name": "proj-fable", "model": "fable"}],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Claude", "id-1", "vampulse-orchestrator", "/home/user/proj",
                    "/home/user/proj", 100, Path("/tmp/x.jsonl"),
                )
                dialog_instance = session_hub.MoveToGroupDialog.__new__(
                    session_hub.MoveToGroupDialog
                )
                dialog_instance.cwd = "/home/user/proj"
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(window, "selected", return_value=session),
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.MoveToGroupDialog", return_value=dialog_instance
                    ),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.add_session_to_group()
                launch.assert_not_called()
                rows = window.metadata["groups"]["/home/user/proj"]["rows"]
                self.assertEqual(len(rows), 2)
                new_row = rows[-1]
                self.assertEqual(new_row["name"], "vampulse-orchestrator")
                self.assertEqual(new_row["session_key"], "Claude:id-1")
                row_overrides = window.metadata["sessions"][new_row["override_key"]]
                self.assertEqual(row_overrides["env"], {"ANTHROPIC_MODEL": "opus"})
                self.assertEqual(
                    row_overrides["flags"], {"--dangerously-skip-permissions": True}
                )
            finally:
                window.close()

    def test_add_session_to_group_dedupes_name_and_overrides_cwd_for_other_group(self):
        # Moving into a group whose directory differs from the session's own
        # requires a cwd override so find_group_member_session's cwd check
        # matches on the next refresh (same mechanism change_directory_for
        # uses) - and a name collision must not silently clobber the
        # existing row.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/home/user/vamp": {
                        "cwd": "/home/user/vamp",
                        "rows": [{"name": "worker", "override_key": "group:/home/user/vamp#worker"}],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Claude", "id-2", "worker", "/home/user/elsewhere",
                    "/home/user/elsewhere", 100, Path("/tmp/x.jsonl"),
                )
                dialog_instance = session_hub.MoveToGroupDialog.__new__(
                    session_hub.MoveToGroupDialog
                )
                dialog_instance.cwd = "/home/user/vamp"
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(window, "selected", return_value=session),
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.MoveToGroupDialog", return_value=dialog_instance
                    ),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.add_session_to_group()
                launch.assert_not_called()
                rows = window.metadata["groups"]["/home/user/vamp"]["rows"]
                self.assertEqual([row["name"] for row in rows], ["worker", "worker-2"])
                self.assertEqual(
                    window.metadata["sessions"]["Claude:id-2"]["cwd"], "/home/user/vamp"
                )
            finally:
                window.close()

    def test_context_menu_includes_add_session_to_group(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            with patch.object(window, "selected", return_value=None):
                labels = [label for label, _ in window.context_menu_actions()]
                self.assertIn("Add session to group…", labels)
        finally:
            window.close()

    def test_move_to_group_dialog_lists_all_groups_and_preselects_match(self):
        groups = {
            "/tmp/vamp": {"cwd": "/tmp/vamp", "rows": [], "display_name": "VAMPULSE"},
            "/tmp/other": {"cwd": "/tmp/other", "rows": []},
        }
        dialog = session_hub.MoveToGroupDialog(groups, "/tmp/other")
        try:
            labels = [
                dialog.group_combo.itemText(i) for i in range(dialog.group_combo.count())
            ]
            self.assertEqual(set(labels), {"New group…", "VAMPULSE", "other"})
            self.assertEqual(dialog.group_combo.currentData(), "/tmp/other")
            self.assertEqual(dialog.directory_label.text(), "/tmp/other")
            dialog.accept()
            self.assertEqual(dialog.cwd, "/tmp/other")
        finally:
            dialog.close()

    def test_move_to_group_dialog_offers_new_group_with_no_groups(self):
        dialog = session_hub.MoveToGroupDialog({}, None)
        try:
            labels = [
                dialog.group_combo.itemText(i) for i in range(dialog.group_combo.count())
            ]
            self.assertEqual(labels, ["New group…"])
        finally:
            dialog.close()

    def test_move_to_group_dialog_new_group_prompts_for_a_name(self):
        dialog = session_hub.MoveToGroupDialog({}, None)
        try:
            dialog.group_combo.setCurrentIndex(
                dialog.group_combo.findData(session_hub.MoveToGroupDialog.NEW_GROUP)
            )
            with patch(
                "session_hub.QInputDialog.getText", return_value=("Workers", True)
            ):
                dialog.accept()
            self.assertEqual(dialog.new_group_name, "Workers")
            self.assertIsNone(dialog.cwd)
        finally:
            dialog.close()

    def test_move_to_group_dialog_new_group_cancelled_stays_open(self):
        dialog = session_hub.MoveToGroupDialog({}, None)
        try:
            dialog.group_combo.setCurrentIndex(
                dialog.group_combo.findData(session_hub.MoveToGroupDialog.NEW_GROUP)
            )
            with (
                patch(
                    "session_hub.QInputDialog.getText", return_value=("", False)
                ),
                patch.object(
                    session_hub.QDialog, "accept"
                ) as base_accept,
            ):
                dialog.accept()
            base_accept.assert_not_called()
            self.assertIsNone(dialog.new_group_name)
        finally:
            dialog.close()

    def test_add_session_to_group_offers_picker_for_unmatched_cwd(self):
        # The selected session's own directory has no group of its own, but
        # a group exists elsewhere - this must open the picker rather than
        # refuse with "no group for this session" (see
        # SessionHub.add_session_to_group_for).
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []},
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Claude", "id-1", "title", "/home/user/unrelated",
                    "/home/user/unrelated", 100, Path("/tmp/x.jsonl"),
                )
                dialog_instance = session_hub.MoveToGroupDialog.__new__(
                    session_hub.MoveToGroupDialog
                )
                dialog_instance.cwd = "/tmp/vamp"
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(window, "selected", return_value=session),
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.MoveToGroupDialog",
                        return_value=dialog_instance,
                    ) as dialog_cls,
                    patch("session_hub.QMessageBox.information") as info,
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.add_session_to_group()
                info.assert_not_called()
                dialog_cls.assert_called_once()
                self.assertEqual(dialog_cls.call_args.args[1], "/home/user/unrelated")
                launch.assert_not_called()
                self.assertEqual(
                    len(window.metadata["groups"]["/tmp/vamp"]["rows"]), 1
                )
            finally:
                window.close()

    def test_manage_group_reuses_open_dialog_instead_of_a_duplicate(self):
        # exec() is application-modal and used to freeze the whole main
        # window while a group was being managed; show() (non-modal) fixes
        # that, but then a second "Manage group…" click on the same group
        # must raise the existing window rather than open a second one.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {"/tmp/vamp": {"cwd": "/tmp/vamp", "rows": []}},
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                session = session_hub.Session(
                    "Claude", "group:/tmp/vamp", "VAMPULSE", "/tmp/vamp",
                    "/tmp/vamp", 100, Path("/tmp/vamp"),
                )
                with (
                    patch.object(window, "selected", return_value=session),
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.manage_group()
                    first = window.group_dialogs["/tmp/vamp"]
                    self.assertEqual(
                        first.windowModality(), session_hub.Qt.WindowModality.NonModal
                    )
                    window.manage_group()
                    self.assertIs(window.group_dialogs["/tmp/vamp"], first)
                first.close()
                self.assertNotIn("/tmp/vamp", window.group_dialogs)
            finally:
                window.close()

    def test_flags_editor_uses_typed_widget_for_effort(self):
        editor = session_hub.EnvEditor(
            {"--effort": "xhigh"}, specs=session_hub.CLI_FLAG_SPECS
        )
        self.assertIsNotNone(editor.table.cellWidget(0, 1))
        self.assertEqual(editor.env(), {"--effort": "xhigh"})

    def test_launch_options_editor_exposes_env_and_flags_separately(self):
        editor = session_hub.LaunchOptionsEditor(
            {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}, {"--effort": "max"}
        )
        self.assertEqual(editor.env(), {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"})
        self.assertEqual(editor.flags(), {"--effort": "max"})

    def test_settings_dialog_saves_global_flags(self):
        dialog = session_hub.SettingsDialog({"global_flags": {"--effort": "xhigh"}})
        self.assertEqual(dialog.flags_editor.env(), {"--effort": "xhigh"})
        dialog.flags_editor.add_known_row("--max-turns", "50")
        self.assertEqual(
            dialog.values()["global_flags"],
            {"--effort": "xhigh", "--max-turns": "50"},
        )
        dialog.close()

    def test_launch_flags_merges_global_and_session_overrides(self):
        metadata = {
            "sessions": {"Claude:s1": {"flags": {"--effort": "max"}}},
            "settings": {"global_flags": {"--effort": "xhigh", "--max-turns": "50"}},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertEqual(
                window.launch_flags("Claude:s1"),
                ["--effort", "max", "--max-turns", "50"],
            )
            self.assertEqual(
                window.launch_flags("Claude:other"),
                ["--effort", "xhigh", "--max-turns", "50"],
            )
            self.assertEqual(
                window.launch_flags(None),
                ["--effort", "xhigh", "--max-turns", "50"],
            )
        finally:
            window.close()

    def test_launch_flags_returns_empty_list_when_unset(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertEqual(window.launch_flags(None), [])
            self.assertEqual(window.launch_flags("Claude:s1"), [])
        finally:
            window.close()

    def test_caveman_prompt_levels_and_artifact_scope(self):
        chat_only = session_hub.caveman_system_prompt("full")
        self.assertIn("CAVEMAN MODE (full)", chat_only)
        self.assertNotIn("WRITE TO FILES", chat_only)

        with_files = session_hub.caveman_system_prompt("ultra+files")
        self.assertIn("CAVEMAN MODE (ultra)", with_files)
        self.assertIn("WRITE TO FILES", with_files)
        # The carve-outs are the whole reason the artifact scope is safe to ship.
        self.assertIn("description:", with_files)
        self.assertIn("Structural syntax is untouched", with_files)
        # Commit messages are IN scope by explicit user override, so the old
        # exemption wording must not creep back in with a prompt reword.
        self.assertIn("GIT COMMIT MESSAGES", with_files)
        self.assertNotIn("commit messages stay ordinary prose", with_files)

    def test_caveman_prompt_is_none_for_off_and_garbage(self):
        for value in ("", "off", "Off", None, "banana", "full+banana"):
            self.assertIsNone(session_hub.caveman_system_prompt(value), value)

    def test_launch_flags_expands_caveman_to_append_system_prompt(self):
        metadata = {
            "sessions": {},
            "settings": {"global_flags": {"--caveman": "full", "--max-turns": "50"}},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            argv = window.launch_flags(None)
            # Never passed through literally -- no agent CLI knows --caveman.
            self.assertNotIn("--caveman", argv)
            self.assertEqual(argv[0], "--append-system-prompt")
            self.assertIn("CAVEMAN MODE (full)", argv[1])
            self.assertEqual(argv[2:], ["--max-turns", "50"])
        finally:
            window.close()

    def test_launch_flags_omits_caveman_entirely_when_off(self):
        metadata = {"sessions": {}, "settings": {"global_flags": {"--caveman": ""}}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertEqual(window.launch_flags(None), [])
        finally:
            window.close()

    def test_launch_flags_extra_overrides_global_and_session(self):
        metadata = {
            "sessions": {"Claude:s1": {"flags": {"--caveman": "lite"}}},
            "settings": {"global_flags": {"--caveman": "full"}},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertIn("(lite)", window.launch_flags("Claude:s1")[1])
            argv = window.launch_flags("Claude:s1", {"--caveman": "ultra+files"})
            self.assertEqual(argv.count("--append-system-prompt"), 1)
            self.assertIn("(ultra)", argv[1])
            # The dialog choice replaces the stored one; it must not stack.
            self.assertNotIn("(lite)", argv[1])
        finally:
            window.close()

    def test_new_session_dialog_has_no_caveman_combo(self):
        dialog = session_hub.NewSessionDialog(
            "Claude", {"global_flags": {"--caveman": "full+files"}}
        )
        try:
            self.assertFalse(hasattr(dialog, "caveman_combo"))
        finally:
            dialog.close()

    def test_flags_editor_renders_bare_flag_as_on_off_with_no_free_value(self):
        editor = session_hub.EnvEditor(
            {"--chrome": "1"}, specs=session_hub.CLI_FLAG_SPECS
        )
        widget = editor.table.cellWidget(0, 1)
        self.assertIsInstance(widget, session_hub.QComboBox)
        self.assertEqual(editor.env(), {"--chrome": "1"})

    def test_launch_flags_emits_bare_flag_without_value(self):
        metadata = {
            "sessions": {},
            "settings": {"global_flags": {"--chrome": "1", "--max-turns": "50"}},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertEqual(
                window.launch_flags(None),
                ["--chrome", "--max-turns", "50"],
            )
        finally:
            window.close()

    @patch("session_hub.shutil.which")
    def test_terminal_command_appends_claude_flags(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        command = window.terminal_command(
            "Claude", "def-456", "/home/user", flags=["--effort", "xhigh"]
        )
        self.assertIn("--effort", command)
        self.assertEqual(
            command[command.index("--effort") : command.index("--effort") + 2],
            ["--effort", "xhigh"],
        )
        window.close()

    @patch("session_hub.shutil.which")
    def test_terminal_command_passes_codex_model_flag(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "codex": "/home/user/.local/bin/codex",
        }.get(name)
        window = session_hub.SessionHub()
        command = window.terminal_command("Codex", None, "/home/user", model="gpt-5")
        self.assertIn("-m", command)
        self.assertEqual(command[command.index("-m") + 1], "gpt-5")
        no_model_command = window.terminal_command("Codex", None, "/home/user")
        self.assertNotIn("-m", no_model_command)
        window.close()

    @patch("session_hub.shutil.which")
    def test_terminal_command_passes_codex_reasoning_effort(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "codex": "/home/user/.local/bin/codex",
        }.get(name)
        window = session_hub.SessionHub()
        command = window.terminal_command(
            "Codex", None, "/home/user", model="gpt-5", reasoning_effort="high"
        )
        self.assertIn("-c", command)
        self.assertEqual(
            command[command.index("-c") + 1], "model_reasoning_effort=high"
        )
        no_effort_command = window.terminal_command("Codex", None, "/home/user")
        self.assertNotIn("-c", no_effort_command)
        window.close()

    def test_launch_passes_global_effort_flag_to_claude_command(self):
        metadata = {
            "sessions": {},
            "settings": {"global_flags": {"--effort": "xhigh"}},
        }
        with (
            patch("session_hub.read_metadata", return_value=metadata),
            patch("session_hub.Path.is_dir", return_value=True),
            patch("session_hub.subprocess.Popen"),
            patch.object(
                session_hub.SessionHub, "terminal_command", return_value=["cmd"]
            ) as terminal_command,
        ):
            window = session_hub.SessionHub()
            try:
                window.launch("Claude", "s1", "/tmp", session_key="Claude:s1")
            finally:
                window.close()
        self.assertEqual(
            terminal_command.call_args.args[-2], ["--effort", "xhigh"]
        )

    def test_settings_toggles_hide_providers(self):
        with tempfile.TemporaryDirectory() as temp:
            fake_metadata = Path(temp) / "metadata.json"
            fake_metadata.write_text(json.dumps({
                "settings": {
                    "enable_codex": False,
                    "enable_claude": True,
                    "enable_antigravity": True
                }
            }))
            with (
                patch("session_hub.METADATA_PATH", fake_metadata),
                patch("session_hub.codex_sessions", return_value=[
                    session_hub.Session("Codex", "id", "C", "/cwd", "/cwd", 0, Path("/p"))
                ]),
                patch("session_hub.claude_sessions", return_value=[]),
                patch("session_hub.antigravity_sessions", return_value=[]),
            ):
                # 1. Discover sessions: Codex sessions should be skipped
                sessions = session_hub.discover_sessions(session_hub.read_metadata())
                self.assertEqual(len(sessions), 0)

                # 2. Open Settings dialog and check visibility toggle UI update
                window = session_hub.SessionHub()
                window.usage_headers = {
                    "Codex": session_hub.QLabel(),
                    "Claude": session_hub.QLabel(),
                    "Antigravity": session_hub.QLabel(),
                }
                window.usage_widgets = {
                    "Codex": [(session_hub.QLabel(), session_hub.QProgressBar(), session_hub.QLabel())],
                    "Claude": [],
                    "Antigravity": [],
                }
                window.update_usage_visibility()
                self.assertFalse(window.usage_headers["Codex"].isVisible())
                self.assertTrue(window.usage_headers["Claude"].isVisible())

                # 3. New dropdown should only list enabled providers
                window.update_new_provider_list()
                items = [window.new_provider.itemText(i) for i in range(window.new_provider.count())]
                self.assertNotIn("Codex", items)
                self.assertIn("Claude", items)

                window.close()

    def test_handoff_actions_hidden_with_single_agent_enabled(self):
        single_agent = {
            "sessions": {},
            "settings": {
                "enable_codex": False,
                "enable_claude": True,
                "enable_antigravity": False,
            },
        }
        with patch("session_hub.read_metadata", return_value=single_agent):
            window = session_hub.SessionHub()
        self.assertTrue(window.continue_with_other_button.isHidden())
        # context_menu_actions() with no session falls through to selected(),
        # which pops a real (blocking, never auto-dismissed) QMessageBox when
        # nothing is selected in a fresh table - patch it out like every
        # other no-selection context_menu_actions() call in this file does.
        with patch.object(window, "selected", return_value=None):
            labels = [label for label, _ in window.context_menu_actions()]
        self.assertNotIn("Continue with other agent", labels)
        window.close()

    def test_handoff_actions_shown_with_multiple_agents_enabled(self):
        multiple_agents = {
            "sessions": {},
            "settings": {
                "enable_codex": True,
                "enable_claude": True,
                "enable_antigravity": False,
            },
        }
        with patch("session_hub.read_metadata", return_value=multiple_agents):
            window = session_hub.SessionHub()
        self.assertFalse(window.continue_with_other_button.isHidden())
        with patch.object(window, "selected", return_value=None):
            labels = [label for label, _ in window.context_menu_actions()]
        self.assertIn("Continue with other agent", labels)
        window.close()

    def test_project_move_works_in_both_directions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / "primary"
            secondary = root / "secondary"
            project = primary / "example"
            project.mkdir(parents=True)
            (project / "file.txt").write_text("data", encoding="utf-8")

            synced = secondary / "example"
            session_hub.move_project_files(project, synced)
            self.assertTrue((synced / "file.txt").is_file())
            self.assertTrue(project.is_symlink())
            self.assertEqual(project.resolve(), synced.resolve())

            session_hub.move_project_files(synced, project)
            self.assertTrue((project / "file.txt").is_file())
            self.assertFalse(project.is_symlink())
            self.assertTrue(synced.is_symlink())
            self.assertEqual(synced.resolve(), project.resolve())

    def test_move_dialog_prefers_custom_session_name(self):
        with tempfile.TemporaryDirectory() as temp:
            primary = Path(temp) / "primary"
            secondary = Path(temp) / "secondary"
            (primary / "folder-name").mkdir(parents=True)
            dialog = session_hub.MoveProjectDialog(
                {
                    "primary_projects_dir": str(primary),
                    "secondary_projects_dir": str(secondary),
                },
                {str(primary / "folder-name"): "My Session Name"},
            )
            self.assertEqual(
                dialog.project.currentText(), "My Session Name  [folder-name]"
            )
            dialog.close()

    def test_restore_deleted_session_from_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "trash" / "codex" / "entry"
            entry.mkdir(parents=True)
            trashed = entry / "session.jsonl"
            trashed.write_text("{}\n", encoding="utf-8")
            destination = root / "restored" / "session.jsonl"
            manifest = {
                "provider": "Codex",
                "session_id": "abc",
                "title": "Restored",
                "deleted_at": "2026-06-19T12:00:00",
                "items": [
                    {"trash": trashed.name, "original": str(destination)}
                ],
                "metadata_override": {"name": "Restored"},
            }
            (entry / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with (
                patch("session_hub.METADATA_PATH", root / "metadata.json"),
                patch("session_hub.QMessageBox.information"),
            ):
                window = session_hub.SessionHub()
                self.assertTrue(window.restore_deleted_entry(entry, manifest))
                self.assertTrue(destination.is_file())
                self.assertFalse(entry.exists())
                window.close()

    def test_window_geometry_is_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata_path = Path(temp) / "metadata.json"
            with (
                patch("session_hub.METADATA_PATH", metadata_path),
                patch("session_hub.QApplication.platformName", return_value="xcb"),
            ):
                window = session_hub.SessionHub()
                window.resize(1100, 700)
                window.close()
                saved = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertTrue(saved["settings"]["window_geometry"])

    def test_refresh_reloads_metadata_from_disk(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata_path = Path(temp) / "metadata.json"
            metadata_path.write_text(
                json.dumps({"sessions": {}, "links": {}}),
                encoding="utf-8",
            )
            with (
                patch("session_hub.METADATA_PATH", metadata_path),
                patch("session_hub.codex_sessions", return_value=[]),
                patch("session_hub.claude_sessions", return_value=[]),
                patch("session_hub.antigravity_sessions", return_value=[]),
            ):
                window = session_hub.SessionHub()
                metadata_path.write_text(
                    json.dumps(
                        {
                            "sessions": {},
                            "links": {"logical": {"members": [], "active": ""}},
                        }
                    ),
                    encoding="utf-8",
                )
                window.refresh()
                self.assertIn("logical", window.metadata["links"])
                window.close()

    def test_close_geometry_preserves_latest_link_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata_path = Path(temp) / "metadata.json"
            metadata_path.write_text(
                json.dumps({"sessions": {}, "links": {}}),
                encoding="utf-8",
            )
            with (
                patch("session_hub.METADATA_PATH", metadata_path),
                patch("session_hub.codex_sessions", return_value=[]),
                patch("session_hub.claude_sessions", return_value=[]),
                patch("session_hub.antigravity_sessions", return_value=[]),
                patch("session_hub.QApplication.platformName", return_value="xcb"),
            ):
                window = session_hub.SessionHub()
                metadata_path.write_text(
                    json.dumps(
                        {
                            "sessions": {},
                            "settings": {},
                            "links": {
                                "logical": {
                                    "members": ["Claude:id", "Codex:id"],
                                    "active": "Codex:id",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                window.close()
                saved = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertIn("logical", saved["links"])


if __name__ == "__main__":
    unittest.main()
