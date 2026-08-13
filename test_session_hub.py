import contextlib
import io
import os
import json
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
                link_overrides = window.metadata["sessions"][link_id]
                self.assertEqual(link_overrides["name"], "vamp-s1")
                self.assertEqual(link_overrides["env"], {"ANTHROPIC_MODEL": "opus"})
                self.assertEqual(
                    link_overrides["flags"], {"--dangerously-skip-permissions": True}
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

    def test_handoff_includes_prepared_summary_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "codex.jsonl"
            transcript.write_text("", encoding="utf-8")
            session = session_hub.Session(
                "Codex",
                "id",
                "Project",
                str(root),
                str(root),
                0,
                transcript,
            )
            with (
                patch("session_hub.HANDOFF_DIR", root / "handoffs"),
                patch("session_hub.SUMMARY_DIR", root / "summaries"),
            ):
                prepared = session_hub.summary_path(session.key)
                prepared.parent.mkdir(parents=True)
                prepared.write_text(
                    "# Agent Handoff Summary\nImportant decision.",
                    encoding="utf-8",
                )
                handoff = session_hub.write_handoff(session, "Claude")
            text = handoff.read_text(encoding="utf-8")
            self.assertIn("Prepared full-session summary", text)
            self.assertIn("Important decision.", text)

    def test_prepared_summary_handoff_keeps_recent_context_compact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "claude.jsonl"
            rows = [
                {
                    "type": "user",
                    "message": {"content": "Earlier context " + "x" * 30000},
                },
                {
                    "type": "user",
                    "message": {
                        "content": (
                            "Prepare a handoff summary for another coding agent. "
                            "This should not be copied."
                        )
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": "You've hit your session limit · resets later"
                    },
                },
                {
                    "type": "user",
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
            with (
                patch("session_hub.HANDOFF_DIR", root / "handoffs"),
                patch("session_hub.SUMMARY_DIR", root / "summaries"),
            ):
                prepared = session_hub.summary_path(session.key)
                prepared.parent.mkdir(parents=True)
                prepared.write_text(
                    "# Agent Handoff Summary\nComplete summary.",
                    encoding="utf-8",
                )
                handoff = session_hub.write_handoff(session, "Codex")
            text = handoff.read_text(encoding="utf-8")
            self.assertIn("Complete summary.", text)
            self.assertIn("Latest real request", text)
            self.assertNotIn("This should not be copied.", text)
            self.assertNotIn("You've hit your session limit", text)
            self.assertLess(len(text), 20000)

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

    @patch("session_hub.shutil.which")
    def test_summary_command_resumes_active_agent(self, which):
        which.side_effect = lambda name: {
            "gnome-terminal": "/usr/bin/gnome-terminal",
            "codex": "/home/user/.local/bin/codex",
            "claude": "/home/user/.local/bin/claude",
        }.get(name)
        window = session_hub.SessionHub()
        codex_session = session_hub.Session(
            "Codex",
            "codex-id",
            "Linked",
            "/home/user",
            "/home/user",
            0,
            Path("/tmp/codex.jsonl"),
        )
        claude_session = session_hub.Session(
            "Claude",
            "claude-id",
            "Linked",
            "/home/user/new",
            "/home/user/original",
            0,
            Path("/tmp/claude.jsonl"),
        )
        with patch("session_hub.SUMMARY_DIR", Path("/tmp/session-hub-summaries")):
            codex = window.summary_terminal_command(codex_session)
            claude = window.summary_terminal_command(claude_session)
        self.assertIn("resume", codex)
        self.assertIn("codex-id", codex)
        self.assertIn("--resume", claude)
        self.assertIn("claude-id", claude)
        self.assertIn("--working-directory=/home/user/original", claude)
        self.assertTrue(any("Agent Handoff Summary" in value for value in codex))
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
        run.return_value = MagicMock(stdout="0x01 0 host Claude — session-hub\n")
        session_hub.focus_window_by_title("Claude — session-hub")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/wmctrl", "-a", "Claude — session-hub"],
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
                labels, ["Manage group…", "Rename group", "Delete group"]
            )
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

    def test_manage_group_dialog_launch_row_delegates_to_hub(self):
        hub = MagicMock()
        dialog = session_hub.ManageGroupDialog.__new__(session_hub.ManageGroupDialog)
        dialog.hub = hub
        dialog.cwd = "/tmp/vamp"
        dialog.reload = MagicMock()
        dialog.launch_row("vamp-s1")
        hub.launch_group_row.assert_called_once_with("/tmp/vamp", "vamp-s1")
        hub.refresh.assert_called_once()
        dialog.reload.assert_called_once()

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
                    session_key="group:/tmp/vamp#vamp-s1",
                    flag_overrides={"--name": "vamp-s1"},
                    strip_env=["CLAUDE_CODE_CHILD_SESSION"],
                    wait_for_tracking=False,
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
                    session_key="group:/tmp/vamp#vamp-s1",
                    flag_overrides={"--name": "vamp-s1"},
                    strip_env=None,
                    wait_for_tracking=False,
                )
            finally:
                window.close()

    def test_launch_group_row_skips_relaunch_when_already_tracked_alive(self):
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
                    patch("session_hub.session_is_tracked_alive", return_value=True),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.launch_group_row("/tmp/vamp", "vamp-s1")
                launch.assert_not_called()
                self.assertEqual(result, {"status": "already_running", "session_id": "abc123"})
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

    def test_new_session_group_dialog_add_remove_rows_and_suggests_names(self):
        with tempfile.TemporaryDirectory() as temp:
            dialog = session_hub.NewSessionGroupDialog({})
            dialog.location.setCurrentIndex(dialog.location.findData("existing"))
            dialog.existing_path.setText(temp)
            dialog.update_preview()
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

    def test_new_session_group_dialog_rejects_duplicate_names(self):
        with tempfile.TemporaryDirectory() as temp:
            dialog = session_hub.NewSessionGroupDialog({})
            dialog.location.setCurrentIndex(dialog.location.findData("existing"))
            dialog.existing_path.setText(temp)
            dialog.update_preview()
            dialog.add_row()
            name_edit_0 = dialog.table.cellWidget(0, 1)
            name_edit_1 = dialog.table.cellWidget(1, 1)
            name_edit_0.setText("same-name")
            name_edit_0.auto_suggested = False
            name_edit_1.setText("same-name")
            name_edit_1.auto_suggested = False
            with patch("session_hub.QMessageBox.warning") as warning:
                dialog.accept()
            warning.assert_called_once()
            self.assertIsNone(dialog.directory)
            dialog.close()

    def test_new_session_group_dialog_launches_all_rows_and_saves_group(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                dialog_instance = session_hub.NewSessionGroupDialog.__new__(
                    session_hub.NewSessionGroupDialog
                )
                dialog_instance.directory = Path(temp)
                dialog_instance.group_rows = [
                    {"name": "vampulse-fable", "model": "fable"},
                    {"name": "vampulse-sonnet", "model": "sonnet"},
                ]
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.NewSessionGroupDialog", return_value=dialog_instance
                    ),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.launch_new_group()
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

    def test_launch_new_group_merges_without_duplicating_existing_rows(self):
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
                dialog_instance = session_hub.NewSessionGroupDialog.__new__(
                    session_hub.NewSessionGroupDialog
                )
                dialog_instance.directory = Path(temp)
                dialog_instance.group_rows = [
                    {"name": "vampulse-fable", "model": "fable"},
                    {"name": "vampulse-sonnet", "model": "sonnet"},
                ]
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(session_hub.SessionHub, "launch"),
                    patch(
                        "session_hub.NewSessionGroupDialog", return_value=dialog_instance
                    ),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.launch_new_group()
                saved = window.metadata["groups"][temp]
                self.assertEqual(len(saved["rows"]), 2)
            finally:
                window.close()

    def test_add_session_to_group_shows_message_when_no_group(self):
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            session = session_hub.Session(
                "Claude", "id-1", "title", "/home/user/proj",
                "/home/user/proj", 100, Path("/tmp/x.jsonl"),
            )
            with (
                patch.object(window, "selected", return_value=session),
                patch("session_hub.QMessageBox.information") as info,
            ):
                window.add_session_to_group()
            info.assert_called_once()
        finally:
            window.close()

    def test_add_session_to_group_launches_and_appends_row(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
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
                    "Claude", "id-1", "title", "/home/user/proj",
                    "/home/user/proj", 100, Path("/tmp/x.jsonl"),
                )
                dialog_instance = session_hub.AddGroupSessionDialog.__new__(
                    session_hub.AddGroupSessionDialog
                )
                dialog_instance.row = {"name": "proj-sonnet", "model": "sonnet"}
                dialog_instance.exec = MagicMock(
                    return_value=session_hub.QDialog.DialogCode.Accepted
                )
                with (
                    patch.object(window, "selected", return_value=session),
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.AddGroupSessionDialog", return_value=dialog_instance
                    ),
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.add_session_to_group()
                launch.assert_called_once()
                self.assertEqual(
                    launch.call_args.kwargs["flag_overrides"], {"--name": "proj-sonnet"}
                )
                self.assertEqual(
                    len(window.metadata["groups"]["/home/user/proj"]["rows"]), 2
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

    def test_new_session_dialog_defaults_caveman_to_global_flag(self):
        settings = {"global_flags": {"--caveman": "full+files"}}
        dialog = session_hub.NewSessionDialog("Claude", settings)
        try:
            self.assertEqual(dialog.caveman_combo.currentData(), "full+files")
            dialog.caveman = str(dialog.caveman_combo.currentData())
            self.assertEqual(dialog.flag_overrides(), {"--caveman": "full+files"})
        finally:
            dialog.close()

    def test_new_session_dialog_has_no_caveman_row_for_codex(self):
        dialog = session_hub.NewSessionDialog("Codex", {})
        try:
            self.assertIsNone(dialog.caveman_combo)
            self.assertEqual(dialog.flag_overrides(), {})
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
        self.assertTrue(window.prepare_handoff_button.isHidden())
        labels = [label for label, _ in window.context_menu_actions()]
        self.assertNotIn("Continue with other agent", labels)
        self.assertNotIn("Prepare handoff summary", labels)
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
        self.assertFalse(window.prepare_handoff_button.isHidden())
        labels = [label for label, _ in window.context_menu_actions()]
        self.assertIn("Continue with other agent", labels)
        self.assertIn("Prepare handoff summary", labels)
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
