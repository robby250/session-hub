import atexit
import contextlib
import io
import os
import json
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# task-2134: a per-test METADATA_PATH/PID_DIR patch is not a sufficient
# safety boundary - a scope escape, a direct test-method invocation that
# skips setUp(), or a delayed Qt close firing after the patch context has
# exited can still resolve session_hub's module-level path constants to the
# real, unpatched value (this is exactly how the row434 test overwrote the
# user's live metadata.json). Forcing XDG_DATA_HOME to a fresh,
# process-owned directory before session_hub is ever imported makes every
# path constant derived from DATA_DIR (METADATA_PATH, PID_DIR, STATUS_DIR,
# METADATA_BACKUP_DIR, TRASH_DIR, ...) sandboxed for the lifetime of this
# process, independent of any later patch scope. Only the directory created
# here is ever removed - a caller-provided XDG_DATA_HOME, if any, is
# overridden in-process but never touched on disk.
_TEST_XDG_DATA_HOME = tempfile.mkdtemp(prefix="session-hub-test-xdg-")
os.environ["XDG_DATA_HOME"] = _TEST_XDG_DATA_HOME
atexit.register(shutil.rmtree, _TEST_XDG_DATA_HOME, ignore_errors=True)

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication

import session_hub

# Captured before any per-test patch.start() can shadow them (setUp() below
# patches session_hub.METADATA_PATH/PID_DIR for defense-in-depth on every
# test), so the sandbox-membership check has the module's true unpatched
# default to compare against rather than whichever test happens to be
# running.
_ORIGINAL_METADATA_PATH = session_hub.METADATA_PATH
_ORIGINAL_PID_DIR = session_hub.PID_DIR


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

        # Same safety-net principle as above, for two more real-I/O seams a bare
        # SessionHub()/refresh() can hit without any test-specific patch:
        #
        # 1. tmux_live_session_names() spawns a REAL `tmux list-sessions` subprocess
        #    whenever populate_session_table() decides a refresh needs activity data,
        #    which any SessionHub() construction can trigger. That is real, slow,
        #    non-hermetic I/O, and on this environment it crashed outright
        #    (subprocess.run -> ValueError: not enough values to unpack) in tests that
        #    never intended to exercise tmux at all (row432 audit). Default `which`
        #    to report "tmux" as absent (tmux_live_session_names() then short-circuits
        #    to frozenset() with no subprocess spawn); every other binary name still
        #    resolves through the REAL shutil.which, unchanged, and any test that
        #    patches session_hub.shutil.which itself (most of the tmux/launch tests
        #    do, with their own dict/side_effect) shadows this net for its own scope
        #    exactly as it did before this net existed.
        # 2. refresh_usage() queues its three UsageWorker QRunnables via
        #    QTimer.singleShot(0, ...), which only fires if something pumps the Qt
        #    event loop (a modal .exec(), QApplication.processEvents()). No test here
        #    exercises real refresh_usage()/UsageWorker dispatch - they all inject
        #    fixture data through usage_loaded() directly - so if a nested event loop
        #    tick ever DOES fire that singleShot, the real readers each carry up to a
        #    12-15s timeout and can call out over the network. Stub refresh_usage()
        #    itself to a no-op (NOT the three read_*_usage functions - those are
        #    directly under test elsewhere, e.g.
        #    test_read_claude_usage_falls_back_to_activity_when_bars_missing calls
        #    session_hub.read_claude_usage() for real with its own subprocess.run
        #    fixture, and a blanket stub of the reader silently broke that test
        #    during this same audit) so an accidental fire costs nothing instead of
        #    up to ~45s, without touching the readers' own direct tests.
        real_which = shutil.which

        def _which_no_tmux(name, *args, **kwargs):
            if name == "tmux":
                return None
            return real_which(name, *args, **kwargs)

        which_patcher = patch.object(session_hub.shutil, "which", side_effect=_which_no_tmux)
        which_patcher.start()
        self.addCleanup(which_patcher.stop)

        refresh_usage_patcher = patch.object(session_hub.SessionHub, "refresh_usage")
        refresh_usage_patcher.start()
        self.addCleanup(refresh_usage_patcher.stop)

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

    def test_pending_link_matches_by_provider_and_cwd(self):
        source_key = "Claude:source-id"
        destination = session_hub.Session(
            "Antigravity",
            "agy-id",
            "Continuation session",
            "/different/path",
            "/different/path",
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
            "pending_links": [
                {
                    "logical_key": source_key,
                    "target_provider": "Antigravity",
                    "existing_keys": [],
                    "cwd": "/different/path",
                    "started_ms": 1000,
                    "expires_ms": 9999999999999,
                }
            ],
        }
        changed = session_hub.resolve_pending_links(metadata, [destination])
        self.assertTrue(changed)
        self.assertEqual(metadata["pending_links"], [])
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

    def _assert_no_live_spawned_threads(self, before: set) -> None:
        """Join every thread that appeared since `before` (bounded, not a
        sleep - a real focus_window_by_title poller blocked on wmctrl would
        still be alive after this and fail the assertion) and assert none
        are still running."""
        spawned = set(threading.enumerate()) - before
        for thread in spawned:
            thread.join(timeout=1)
        self.assertFalse(any(thread.is_alive() for thread in spawned))

    def test_continue_with_other_agent_sets_correct_target_provider(self):
        real_cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, real_cwd, ignore_errors=True)
        active = session_hub.Session(
            "Claude",
            "claude-id",
            "Logical Session",
            real_cwd,
            real_cwd,
            300,
            Path("/tmp/claude.jsonl"),
        )

        def fake_copy_dialog(*args, **kwargs):
            dialog = MagicMock()
            dialog.exec.return_value = session_hub.QDialog.DialogCode.Accepted
            dialog.include_prompt = False
            return dialog

        def fake_model_dialog(*args, **kwargs):
            dialog = MagicMock()
            dialog.exec.return_value = session_hub.QDialog.DialogCode.Accepted
            dialog.model = None
            dialog.reasoning_effort = None
            dialog.account_config_dir = None
            return dialog

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
                    "pending_links": [],
                }
                with (
                    patch.object(window, "selected", return_value=active),
                    patch("session_hub.QInputDialog.getItem", return_value=("Antigravity", True)),
                    patch("session_hub.TranscriptPathDialog", side_effect=fake_copy_dialog),
                    patch("session_hub.AgentModelEffortDialog", side_effect=fake_model_dialog),
                    patch("session_hub.QApplication.clipboard"),
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.subprocess.Popen"),
                    patch("session_hub.focus_window_by_title") as focus_antigravity,
                ):
                    threads_before = set(threading.enumerate())
                    window.continue_with_other_agent()
                pending = window.metadata.get("pending_links", [])
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["target_provider"], "Antigravity")
                # The real focus_window_by_title polls wmctrl on a daemon
                # thread for up to 3s; asserting it was CALLED proves the
                # launch path still reaches that seam (not silently skipped),
                # and joining every thread it spawned proves the fake seam -
                # not a real background poller - is what ran (a real poller
                # would still be alive/blocked on wmctrl after this join).
                focus_antigravity.assert_called_once()
                self._assert_no_live_spawned_threads(threads_before)

                window.metadata["pending_links"] = []
                with (
                    patch.object(window, "selected", return_value=active),
                    patch("session_hub.QInputDialog.getItem", return_value=("Codex", True)),
                    patch("session_hub.TranscriptPathDialog", side_effect=fake_copy_dialog),
                    patch("session_hub.AgentModelEffortDialog", side_effect=fake_model_dialog),
                    patch("session_hub.QApplication.clipboard"),
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.subprocess.Popen"),
                    patch("session_hub.focus_window_by_title") as focus_codex,
                ):
                    threads_before = set(threading.enumerate())
                    window.continue_with_other_agent()
                pending = window.metadata.get("pending_links", [])
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["target_provider"], "Codex")
                focus_codex.assert_called_once()
                self._assert_no_live_spawned_threads(threads_before)
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
    def test_antigravity_resume_command_uses_danger_mode_and_conversation_flag(self, which):
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
        self.assertIn("--dangerously-skip-permissions", resume)
        self.assertEqual(resume[-2:], ["--conversation", "agy-id"])
        window.close()

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

    def test_new_session_toolbar_defaults_to_claude_not_codex(self):
        # task-2139/row447: PROVIDERS lists Codex first, and addItems()
        # alone leaves index 0 (Codex) selected - which is already "current"
        # by the time update_new_provider_list() runs right after build_ui(),
        # so its own Claude-fallback comment never actually fired on a fresh
        # launch. A real widget test, not a string-shape assertion: this
        # constructs the real SessionHub/QComboBox and reads back its actual
        # selection before any interaction, the same state a "New" click
        # would open NewSessionDialog with.
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
        self.assertEqual(window.new_provider.currentText(), "Claude")
        with patch.object(window, "launch_new") as launch_new:
            window.launch_selected_provider()
        launch_new.assert_called_once_with("Claude")
        window.close()

    def test_new_session_toolbar_retains_explicit_non_claude_choice(self):
        # The default-to-Claude fix must not override an explicit user pick,
        # including across an update_new_provider_list() re-population
        # (settings toggles trigger this).
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
        window.new_provider.setCurrentText("Codex")
        window.update_new_provider_list()
        self.assertEqual(window.new_provider.currentText(), "Codex")
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
                # row447 second rework: `-t "=$2"`, not `-t "$2"` - tmux's
                # default target resolution accepts an unambiguous PREFIX
                # match, so a bare name could bind to a different,
                # merely-prefixed session (see tmux_exact_target).
                '"$1" has-session -t "=$2" 2>/dev/null || "$1" new-session -d -s "$2" -c "$3" "$4";'
                ' "$1" set-option -g set-titles on >/dev/null;'
                ' "$1" set-option -g set-titles-string "#S" >/dev/null;'
                ' "$1" set-option -g focus-events on >/dev/null;'
                ' exec "$5" --window -- "$1" attach -t "=$2"',
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
            "links": {
                "manual:old": {
                    "members": ["Claude:older", "Claude:abc"],
                    "active": "Claude:abc",
                }
            },
            "pending_links": [{
                "logical_key": "group:/tmp/vamp#vamp-sonnet1",
                "target_tmux_name": "vamp-sonnet1",
            }],
        }
        result = session_hub.rename_group_row_in(metadata, "/tmp/vamp", "vamp-sonnet1", "VAMP-worker1")
        self.assertEqual(result["status"], "renamed")
        row = metadata["groups"]["/tmp/vamp"]["rows"][0]
        self.assertEqual(row["name"], "VAMP-worker1")
        self.assertEqual(row["override_key"], "group:/tmp/vamp#VAMP-worker1")
        self.assertEqual(row["session_key"], "Claude:abc")
        # The row, stable bucket, active/history names, link identity and
        # pending tmux identity all move together.
        self.assertNotIn("group:/tmp/vamp#vamp-sonnet1", metadata["sessions"])
        self.assertEqual(metadata["sessions"]["group:/tmp/vamp#VAMP-worker1"],
                         {"name": "VAMP-worker1", "flags": {"--effort": "high"}})
        self.assertEqual(metadata["sessions"]["Claude:abc"]["name"], "VAMP-worker1")
        self.assertEqual(metadata["sessions"]["Claude:older"]["name"], "VAMP-worker1")
        self.assertNotIn("manual:old", metadata["links"])
        self.assertIn("group:/tmp/vamp#VAMP-worker1", metadata["links"])
        self.assertEqual(
            metadata["pending_links"][0],
            {
                "logical_key": "group:/tmp/vamp#VAMP-worker1",
                "target_tmux_name": "VAMP-worker1",
            },
        )
        # collisions and unknown rows refuse
        self.assertEqual(session_hub.rename_group_row_in(metadata, "/tmp/vamp", "VAMP-worker1", "other")["status"], "error")
        self.assertEqual(session_hub.rename_group_row_in(metadata, "/tmp/vamp", "nope", "x")["status"], "error")
        self.assertEqual(session_hub.rename_group_row_in(metadata, "/tmp/vamp", "other", "other")["status"], "unchanged")

    @patch("session_hub.shutil.which", return_value=None)
    def test_tmux_group_launch_command_raises_when_tmux_missing(self, which):
        with self.assertRaises(RuntimeError):
            session_hub.tmux_group_launch_command("vamp-s1", "/tmp/vamp", ["claude"])

    def test_tmux_group_launch_command_actually_executes_the_shell_script(self):
        """Replaces the live tmux/gnome-terminal integration test (row432 audit rework
        r2): row432's setUp-level `shutil.which("tmux") -> None` net made this test's
        own `if not (which("tmux") and which("gnome-terminal")): skipTest(...)` guard
        fire unconditionally, silently losing the coverage its own comment says caught
        a real bug (env's `--` placement killing the tmux session before it could ever
        attach) - string-shape assertions alone had missed that once already.

        This hermetic replacement resolves "tmux"/"gnome-terminal"/"env" to tiny fake
        executables on a temp PATH instead of the real binaries, then runs the FULL
        generated bash script (all five positional args: has-session/new-session/
        set-option x3/exec attach) for real via subprocess.run - proving actual
        command execution, not just its string shape - with no real tmux server,
        window, live background daemon, or polling loop: the fake "tmux new-session"
        handler runs the built command synchronously and captures its real output
        before returning, so the whole test is one blocking subprocess call.

        The fake "env" specifically rejects a bare `--`, mirroring the disc-confirmed
        bug prefix_env_command's own docstring describes, independent of what
        /usr/bin/env the test host actually ships - GNU coreutils 9.4 here happens to
        accept `--`, which is exactly why relying on the real binary would not catch a
        regression back to the old behavior."""
        with tempfile.TemporaryDirectory() as fakebin_dir:
            fakebin = Path(fakebin_dir)
            marker = fakebin / "captured.txt"

            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                'case "$1" in\n'
                "  has-session) exit 1 ;;\n"
                '  new-session) sh -c "${@: -1}" > "%s" 2>&1; exit 0 ;;\n'
                "  set-option) exit 0 ;;\n"
                "  attach) exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n" % marker
            )
            fake_tmux.chmod(0o755)

            fake_terminal = fakebin / "gnome-terminal"
            fake_terminal.write_text("#!/bin/bash\nexit 0\n")
            fake_terminal.chmod(0o755)

            fake_env = fakebin / "env"
            fake_env.write_text(
                "#!/bin/bash\n"
                "while [ $# -gt 0 ]; do\n"
                '  case "$1" in\n'
                '    --) echo "env: -- : No such file or directory" >&2; exit 127 ;;\n'
                '    -u) unset "$2"; shift 2 ;;\n'
                '    *=*) export "$1"; shift ;;\n'
                "    *) break ;;\n"
                "  esac\n"
                "done\n"
                'exec "$@"\n'
            )
            fake_env.chmod(0o755)

            spawn_env = dict(os.environ, PATH=f"{fakebin}:{os.environ.get('PATH', '')}")

            def run_and_capture(claude_args: list[str]) -> str:
                marker.unlink(missing_ok=True)
                with patch.object(
                    session_hub.shutil, "which",
                    side_effect=lambda name: {
                        "tmux": str(fake_tmux), "gnome-terminal": str(fake_terminal),
                    }.get(name),
                ):
                    command = session_hub.tmux_group_launch_command(
                        "session-hub-test", "/tmp", claude_args
                    )
                subprocess.run(command, env=spawn_env, timeout=5, check=True)
                return marker.read_text(encoding="utf-8") if marker.exists() else ""

            # Positive: today's prefix_env_command emits no `--`, so the real child
            # process actually ran and the marker sees its real output.
            good_args = session_hub.prefix_env_command(
                ["bash", "-c", "echo $MARKER_VAR"], {"MARKER_VAR": "it-worked"}, None
            )
            self.assertIn("it-worked", run_and_capture(good_args))

            # Negative control: reconstruct the exact old buggy shape (a bare `--`
            # inserted before the NAME=VALUE pairs) - proves this harness actually
            # discriminates good from bad execution, not just that it runs something.
            bad_args = ["env", "--", "MARKER_VAR=it-worked", "bash", "-c", "echo $MARKER_VAR"]
            self.assertNotIn("it-worked", run_and_capture(bad_args))

    def test_sanitize_tmux_session_name_matches_real_tmux_substitution(self):
        # Empirically confirmed against the real tmux binary (row447): `tmux
        # new-session -s "x.y"` silently creates a session actually named
        # "x_y", and `tmux has-session -t "x.y"`/`attach -t "x.y"` both then
        # fail to find it (dots/colons are session:window.pane separators in
        # a target spec). sanitize_tmux_session_name must apply the identical
        # substitution so a name used consistently through it never diverges
        # from what tmux itself would call the session.
        self.assertEqual(
            session_hub.sanitize_tmux_session_name("gpt-5.6-luna"), "gpt-5_6-luna"
        )
        self.assertEqual(session_hub.sanitize_tmux_session_name("a:b.c"), "a_b_c")
        self.assertEqual(session_hub.sanitize_tmux_session_name("plain-name"), "plain-name")

    def test_suggest_session_name_pre_sanitizes_dotted_model_slugs(self):
        # Every offered Codex model slug follows the "gpt-5.x[.y]" naming
        # convention (dots), so this is the common case, not an edge case -
        # the auto-suggested name must already be tmux-safe before it is ever
        # shown in a UI field or stored as this session's address.
        name = session_hub.suggest_session_name(
            Path("/home/user/projects"), "gpt-5.6-luna", set()
        )
        self.assertEqual(name, "projects-gpt-5_6-luna")
        self.assertNotIn(".", name)

    def test_tmux_group_launch_command_sanitizes_a_raw_dotted_name(self):
        # Defense in depth: even a caller that skips suggest_session_name
        # (a manually-typed name, or a future call site) gets a tmux-safe
        # name out of the one shared launch helper.
        with patch.object(
            session_hub.shutil, "which",
            side_effect=lambda n: {"tmux": "/usr/bin/tmux", "gnome-terminal": "/usr/bin/gnome-terminal"}.get(n),
        ):
            command = session_hub.tmux_group_launch_command("gpt-5.6-luna", "/tmp", ["codex"])
        self.assertIn("gpt-5_6-luna", command)
        self.assertNotIn("gpt-5.6-luna", command)

    def test_dotted_tmux_name_fails_end_to_end_before_the_fix_succeeds_after(self):
        """Negative-controls the exact row447 mechanism with a hermetic fake tmux
        that reproduces the REAL binary's confirmed behavior (see
        test_sanitize_tmux_session_name_matches_real_tmux_substitution's docstring):
        has-session/new-session/attach all silently apply the same dot/colon->'_'
        substitution tmux itself does, tracked via one state file so the three
        calls agree with each other exactly like the real daemon does.

        Feeding the OLD unsanitized dotted name into all three positions (bypassing
        today's fix) reproduces the user's report: new-session succeeds (creating
        the substituted name), but the final `attach -t <dotted name>` cannot find
        it, so the script exits non-zero - a real Codex process could be running
        headless in tmux while gnome-terminal never attaches to it, i.e. "launches
        nothing" from the user's side. Today's tmux_group_launch_command output
        (name pre-sanitized) exits zero for the identical fake tmux and codex.
        """
        with tempfile.TemporaryDirectory() as fakebin_dir:
            fakebin = Path(fakebin_dir)
            state = fakebin / "created_name.txt"

            # Models the real tmux binary's two DIFFERENT behaviors for the
            # same string (confirmed live against /usr/bin/tmux, row447):
            # `-s NAME` on session CREATION silently substitutes '.'/':' with
            # '_' in the name it actually stores; but `-t NAME` on has-session/
            # attach TARGET RESOLUTION parses '.'/':' as session:window.pane
            # separators instead, taking only the literal text before the
            # first one as the session part - it does NOT apply the same
            # substitution. So querying with the original dotted string never
            # matches the substituted name that was actually created.
            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                "substitute() { echo \"$1\" | tr '.:' '__'; }\n"
                # Strips a leading '=' (tmux_exact_target's exact-match marker,
                # row447 second rework) before the existing dot/colon-target
                # truncation, so this stub still parses the -t value the same
                # way the real tmux target-spec grammar does.
                "session_part() { echo \"$1\" | sed -E 's/^=//; s/[.:].*//'; }\n"
                'case "$1" in\n'
                '  has-session) t=$(session_part "$3");'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && exit 0 || exit 1 ;;\n'
                '  new-session) n=$(substitute "$4"); echo "$n" > "%s"; exit 0 ;;\n'
                '  attach) t=$(session_part "$3");'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && exit 0 || exit 1 ;;\n'
                "  set-option) exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n" % state
            )
            fake_tmux.chmod(0o755)

            # Unlike the no-op stub used elsewhere in this file, this fake
            # terminal actually execs its trailing `-- CMD ARGS...` (as a
            # real gnome-terminal ultimately runs the command it's given) so
            # the script's final `exec "$5" --window -- "$1" attach -t "$2"`
            # really invokes the fake tmux's `attach` branch instead of the
            # terminal launcher silently swallowing its exit status.
            fake_terminal = fakebin / "gnome-terminal"
            fake_terminal.write_text(
                "#!/bin/bash\n"
                'while [ "$1" != "--" ]; do shift; done\n'
                "shift\n"
                'exec "$@"\n'
            )
            fake_terminal.chmod(0o755)

            with patch.object(
                session_hub.shutil, "which",
                side_effect=lambda n: {"tmux": str(fake_tmux), "gnome-terminal": str(fake_terminal)}.get(n),
            ):
                fixed_command = session_hub.tmux_group_launch_command(
                    "gpt-5.6-luna", "/tmp", ["codex", "-m", "gpt-5.6-luna"]
                )

            # Reconstruct the pre-fix shape: the raw dotted name in every
            # position tmux_group_launch_command's script uses it.
            buggy_command = list(fixed_command)
            sanitized_index = buggy_command.index("gpt-5_6-luna")
            buggy_command[sanitized_index] = "gpt-5.6-luna"

            state.unlink(missing_ok=True)
            buggy_result = subprocess.run(buggy_command, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(
                buggy_result.returncode, 0,
                "the pre-fix dotted-name script should fail to attach, exactly "
                "like the user's report",
            )

            state.unlink(missing_ok=True)
            fixed_result = subprocess.run(fixed_command, capture_output=True, text=True, timeout=5)
            self.assertEqual(fixed_result.returncode, 0, fixed_result.stderr)

    def test_codex_launch_args_reach_the_real_child_for_default_and_every_custom_model_effort(self):
        """Table-driven per the row447 REWORK: not 3 representative pairs (the
        orchestrator's finding #2) but every model/effort combination the UI
        actually offers, DERIVED from the real local ~/.codex/models_cache.json
        via session_hub.codex_models() rather than hardcoded - so a model
        added or dropped from the cache changes what this test covers without
        anyone having to remember to update a literal list. For each offered
        model: Default effort and every one of its supported_reasoning_levels
        (this is also where "custom-model+Default-effort" lives - the (model,
        None) entry). Also covers the "Default-model+custom-effort" boundary
        the UI itself never actually offers (populate_codex_effort_combo has
        no levels for a None model slug) but codex_launch_args must still
        handle sanely, since it is a plain function callable with any args.

        Proven at the real child argv boundary - the full
        tmux_group_launch_command script (shlex.join -> a real shell
        re-parse), not just this process's own string-shape assertions on the
        built list - via a fake `codex` that records the argv it actually
        received.
        """
        with tempfile.TemporaryDirectory() as fakebin_dir:
            fakebin = Path(fakebin_dir)
            marker = fakebin / "argv.txt"

            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                'case "$1" in\n'
                "  has-session) exit 1 ;;\n"
                '  new-session) sh -c "${@: -1}" ; exit 0 ;;\n'
                "  set-option) exit 0 ;;\n"
                "  attach) exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            fake_tmux.chmod(0o755)

            fake_terminal = fakebin / "gnome-terminal"
            fake_terminal.write_text("#!/bin/bash\nexit 0\n")
            fake_terminal.chmod(0o755)

            fake_codex = fakebin / "codex"
            fake_codex.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{marker}"\n')
            fake_codex.chmod(0o755)

            spawn_env = dict(os.environ, PATH=f"{fakebin}:{os.environ.get('PATH', '')}")

            cases = [("Default", None, None)]
            offered_models = session_hub.codex_models()
            for model_entry in offered_models:
                slug = model_entry["slug"]
                cases.append((f"{slug} / Default effort", slug, None))
                for level in model_entry.get("supported_reasoning_levels", []):
                    effort = level.get("effort")
                    if effort:
                        cases.append((f"{slug} / {effort}", slug, effort))
            # The user's exact report must be in the derived set on this
            # machine (row447's original repro), not just structurally covered.
            self.assertIn(
                ("gpt-5.6-luna / high", "gpt-5.6-luna", "high"), cases,
                "the reported failing pair is missing from the local models "
                "cache this test derived its coverage from",
            )
            if offered_models:
                # Boundary case no combo box actually offers (Default model
                # has no slug to look up supported levels for) but the plain
                # function must still handle without crashing or mismatching.
                cases.append(("Default model / custom effort", None, "high"))
            for label, model, effort in cases:
                with self.subTest(label):
                    marker.unlink(missing_ok=True)
                    with patch.object(session_hub, "executable", return_value=str(fake_codex)):
                        args = session_hub.codex_launch_args(
                            "/tmp", model=model, reasoning_effort=effort
                        )
                    with patch.object(
                        session_hub.shutil, "which",
                        side_effect=lambda n: {
                            "tmux": str(fake_tmux), "gnome-terminal": str(fake_terminal),
                        }.get(n),
                    ):
                        name = session_hub.suggest_session_name(Path("/tmp"), model, set())
                        command = session_hub.tmux_group_launch_command(name, "/tmp", args)
                    subprocess.run(command, env=spawn_env, timeout=5, check=True)
                    recorded = marker.read_text(encoding="utf-8").splitlines()
                    # Round-trip fidelity through shlex.join -> tmux -> a real
                    # shell re-parse: the fake codex must see the exact argv
                    # codex_launch_args built (minus argv[0], which the fake
                    # binary itself replaces).
                    self.assertEqual(recorded, args[1:])
                    if model:
                        self.assertIn(model, recorded)
                    if effort:
                        self.assertIn(f"model_reasoning_effort={effort}", recorded)

    def test_group_row_status_canonicalizes_a_legacy_unsafe_stored_name(self):
        # The orchestrator's own row447 rework repro, verbatim: a row whose
        # stored name predates this fix (or slipped past it) must still read
        # correctly against the real tmux daemon's actual (substituted) name.
        row = {"name": "foo.bar"}
        self.assertEqual(
            session_hub.group_row_status(row, None, True, frozenset({"foo_bar"})),
            "Running",
        )
        self.assertEqual(
            session_hub.group_row_status(row, None, True, frozenset({"foo.bar"})),
            "Stopped",
        )

    def test_standalone_tmux_status_canonicalizes_an_unsafe_override_name(self):
        session = session_hub.Session(
            "Codex", "s1", "title", "/tmp/vamp", "/tmp/vamp", 0, Path("/tmp/x.jsonl"),
        )
        overrides = {"tmux": True, "name": "foo.bar"}
        enabled, name, status = session_hub.standalone_tmux_status(
            session, overrides, {}, frozenset({"foo_bar"}),
        )
        self.assertTrue(enabled)
        self.assertEqual(status, "Running")

    def test_tmux_primitives_canonicalize_unsafe_names(self):
        """stop_tmux_session, rename_tmux_session and codex_tmux_native_key
        all target the real (substituted) tmux session even when handed a
        stored-but-unsafe name - not just tmux_session_alive/
        tmux_group_launch_command, which row447's first pass already fixed.
        """
        with tempfile.TemporaryDirectory() as fakebin_dir:
            fakebin = Path(fakebin_dir)
            state = fakebin / "created_name.txt"
            state.write_text("foo_bar")

            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                # Strips a leading '=' (tmux_exact_target's exact-match marker,
                # row447 second rework) before the existing dot/colon-target
                # truncation, so this stub still parses the -t value the same
                # way the real tmux target-spec grammar does.
                "session_part() { echo \"$1\" | sed -E 's/^=//; s/[.:].*//'; }\n"
                'case "$1" in\n'
                '  kill-session) t=$(session_part "$3");'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && rm -f "{state}" && exit 0 || exit 1 ;;\n'
                '  has-session) t=$(session_part "$3");'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && exit 0 || exit 1 ;;\n'
                '  rename-session) t=$(session_part "$3"); n="$4";'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && echo "$n" > "{state}" && exit 0 || exit 1 ;;\n'
                "  list-panes) exit 1 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            fake_tmux.chmod(0o755)
            with patch.object(
                session_hub.shutil, "which",
                side_effect=lambda n: {"tmux": str(fake_tmux)}.get(n),
            ):
                # kill-session -t "foo.bar" must resolve to the real
                # "foo_bar" session (the stub only accepts a target session
                # part equal to what's in `state`).
                session_hub.stop_tmux_session("foo.bar")
                self.assertFalse(state.exists(), "kill-session never reached the real session")

                state.write_text("foo_bar")
                self.assertTrue(session_hub.rename_tmux_session("foo.bar", "baz.qux"))
                self.assertEqual(state.read_text().strip(), "baz_qux")

                # codex_tmux_native_key's own list-panes call must also be
                # targeted at the sanitized name (the stub's list-panes
                # case exits 1 regardless of target, so this only proves no
                # crash on the unsafe literal and a clean None result).
                self.assertIsNone(session_hub.codex_tmux_native_key("foo.bar"))

    def test_tmux_exact_target_prevents_a_stale_prefix_name_from_matching_a_live_session(self):
        """Behavioral control, row447 SECOND rework: real isolated-tmux control
        (orchestrator) - with only session "foo2" live, the REAL
        `tmux has-session -t foo` exits 0, because tmux's default target
        resolution accepts an unambiguous PREFIX match. Every identity
        primitive must therefore send an EXACT target (tmux_exact_target's
        leading '=', not a bare name), or a stale/wrong "foo" can still see,
        stop, rename, or inspect a live "foo2" it only happens to prefix.

        The fake tmux below implements BOTH resolution rules (bare target =
        prefix match, "=target" = exact match only) - proven against itself
        first (the `real_tmux_probe` sanity check), so this is a genuine
        control on the mechanism, not a tautology that only exercises
        whatever this test itself assumes. has-session/attach share one
        script line in tmux_group_launch_command (see
        test_tmux_group_launch_command_matches_name_to_tmux_session for its
        exact-string proof), so this covers attach structurally rather than
        by spawning a second live session here.
        """
        with tempfile.TemporaryDirectory() as fakebin_dir:
            fakebin = Path(fakebin_dir)
            state = fakebin / "created_name.txt"
            state.write_text("foo2")

            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                'live=$(cat "%s" 2>/dev/null)\n'
                'target="$3"\n'
                'case "$target" in\n'
                '  =*) [ "${target#=}" = "$live" ] && ok=1 || ok=0 ;;\n'
                '  *) case "$live" in "$target"*) ok=1 ;; *) ok=0 ;; esac ;;\n'
                "esac\n"
                'case "$1" in\n'
                '  has-session) [ "$ok" = 1 ] && exit 0 || exit 1 ;;\n'
                '  kill-session) if [ "$ok" = 1 ]; then rm -f "%s"; exit 0; else exit 1; fi ;;\n'
                '  rename-session) if [ "$ok" = 1 ]; then echo "$4" > "%s"; exit 0; else exit 1; fi ;;\n'
                '  list-panes) [ "$ok" = 1 ] && printf "1234\\n" || exit 1 ;;\n'
                "  *) exit 0 ;;\n"
                "esac\n" % (state, state, state)
            )
            fake_tmux.chmod(0o755)

            with patch.object(
                session_hub.shutil, "which",
                side_effect=lambda n: {"tmux": str(fake_tmux)}.get(n),
            ):
                # Sanity: this fake tmux really does prefix-match a bare
                # target, exactly like the real binary the orchestrator
                # tested - a control that can't reproduce the bug proves
                # nothing about the fix.
                probe = subprocess.run(
                    [str(fake_tmux), "has-session", "-t", "foo"], capture_output=True,
                )
                self.assertEqual(probe.returncode, 0)

                # Production code always sends the exact form - "foo" must
                # not see, stop, rename or (via list-panes) inspect "foo2".
                self.assertFalse(session_hub.tmux_session_alive("foo"))
                session_hub.stop_tmux_session("foo")
                self.assertTrue(state.exists(), "stale 'foo' must not kill live 'foo2'")
                self.assertFalse(session_hub.rename_tmux_session("foo", "bar"))
                self.assertEqual(state.read_text(), "foo2")
                self.assertIsNone(session_hub.codex_tmux_native_key("foo"))

                # The real session is still reachable under its own exact name.
                self.assertTrue(session_hub.tmux_session_alive("foo2"))

    def test_register_group_row_canonicalizes_the_stored_name(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            self.addCleanup(window.close)
            with patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"):
                registered = window.register_group_row(
                    "/tmp/vamp", "gpt-5.6-luna", "Codex", None
                )
            self.assertEqual(registered["name"], "gpt-5_6-luna")
            self.assertEqual(
                registered["override_key"], "group:/tmp/vamp#gpt-5_6-luna"
            )

    def test_rename_group_row_in_canonicalizes_and_rejects_a_same_group_collision(self):
        metadata = {
            "groups": {
                "/tmp/vamp": {
                    "cwd": "/tmp/vamp",
                    "rows": [
                        {"name": "a", "override_key": "group:/tmp/vamp#a"},
                        {"name": "b_c", "override_key": "group:/tmp/vamp#b_c"},
                    ],
                }
            },
            "sessions": {}, "links": {}, "pending_links": [],
        }
        result = session_hub.rename_group_row_in(metadata, "/tmp/vamp", "a", "x.y")
        self.assertEqual(result, {"status": "renamed", "old": "a", "name": "x_y"})
        self.assertEqual(metadata["groups"]["/tmp/vamp"]["rows"][0]["name"], "x_y")

        # "b:c" canonicalizes to "b_c", which already names the OTHER row in
        # this group - must collide post-sanitization, not silently mint a
        # second row sharing that identity.
        collision = session_hub.rename_group_row_in(metadata, "/tmp/vamp", "x_y", "b:c")
        self.assertEqual(collision["status"], "error")

    def test_launch_new_group_sessions_dialog_rejects_a_post_sanitization_name_collision(self):
        # Sibling of test_launch_new_group_sessions_dialog_rejects_duplicate_names:
        # "a.b" and "a:b" are different RAW text but the same tmux identity
        # once canonicalized - the dialog's own duplicate check must catch
        # this, not just a byte-identical raw match.
        dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
        dialog.add_row()
        name_edit_0 = dialog.table.cellWidget(0, 4)
        name_edit_1 = dialog.table.cellWidget(1, 4)
        name_edit_0.setText("a.b")
        name_edit_0.auto_suggested = False
        name_edit_1.setText("a:b")
        name_edit_1.auto_suggested = False
        with patch("session_hub.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.group_rows, [])
        dialog.close()

    def test_invalid_codex_model_effort_reason_rejects_a_mismatched_known_model(self):
        with patch("session_hub.codex_models", return_value=self._codex_models_fixture()):
            # gpt-5.5 supports only "medium" in the fixture.
            self.assertIsNotNone(
                session_hub.invalid_codex_model_effort_reason("gpt-5.5", "high")
            )
            self.assertIsNone(
                session_hub.invalid_codex_model_effort_reason("gpt-5.5", "medium")
            )
            # No model, no effort, or Default (None) never reject.
            self.assertIsNone(session_hub.invalid_codex_model_effort_reason(None, "high"))
            self.assertIsNone(session_hub.invalid_codex_model_effort_reason("gpt-5.5", None))
            # A model absent from the roster is deliberately NOT rejected -
            # the cache can be stale/incomplete by design (populate_codex_model_combo's
            # own docstring); only a KNOWN model with a mismatched effort is.
            self.assertIsNone(
                session_hub.invalid_codex_model_effort_reason("gpt-9-not-cached-yet", "ultra")
            )

    def test_agent_model_effort_dialog_rejects_a_mismatched_codex_model_effort(self):
        """Widget-level negative control (row447 third rework): a Codex
        model/effort combination the local roster proves invalid must be
        rejected visibly, with the dialog never reaching the Accepted state a
        caller (continue_with_other_agent_for) gates its launch() call on -
        so a caller correctly never spawns anything for this input.
        """
        with patch("session_hub.codex_models", return_value=self._codex_models_fixture()):
            dialog = session_hub.AgentModelEffortDialog("Codex", None)
            self.addCleanup(dialog.close)
            dialog.codex_model_combo.setCurrentIndex(
                dialog.codex_model_combo.findData("gpt-5.5")
            )
            # Free-typed, unsupported effort - exactly what an editable
            # combo box allows (populate_codex_effort_combo's own docstring).
            dialog.codex_effort_combo.setCurrentText("high")
            with patch("session_hub.QMessageBox.warning") as warning:
                dialog.accept()
            warning.assert_called_once()
            self.assertNotEqual(dialog.result(), session_hub.QDialog.DialogCode.Accepted)

            # Positive control: a supported pair for the same dialog proceeds.
            dialog.codex_effort_combo.setCurrentText("medium")
            with patch("session_hub.QMessageBox.warning") as warning:
                dialog.accept()
            warning.assert_not_called()
            self.assertEqual(dialog.result(), session_hub.QDialog.DialogCode.Accepted)

    def test_session_launch_options_dialog_rejects_a_mismatched_codex_model_effort(self):
        with patch("session_hub.codex_models", return_value=self._codex_models_fixture()):
            dialog = session_hub.SessionLaunchOptionsDialog(
                "worker", {}, {}, {}, {}, provider="Codex",
                model="gpt-5.5", reasoning_effort="medium",
            )
            self.addCleanup(dialog.close)
            dialog.codex_effort_combo.setCurrentText("high")
            with patch("session_hub.QMessageBox.warning") as warning:
                dialog.accept()
            warning.assert_called_once()
            self.assertNotEqual(dialog.result(), session_hub.QDialog.DialogCode.Accepted)

    def test_launch_new_group_sessions_dialog_rejects_a_row_with_mismatched_codex_model_effort(self):
        with patch("session_hub.codex_models", return_value=self._codex_models_fixture()):
            dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
            self.addCleanup(dialog.close)
            provider_combo = dialog.table.cellWidget(0, 0)
            provider_combo.setCurrentIndex(provider_combo.findData("Codex"))
            model_combo = dialog.table.cellWidget(0, 1)
            model_combo.setCurrentIndex(model_combo.findData("gpt-5.5"))
            effort_combo = dialog.table.cellWidget(0, 2)
            effort_combo.setCurrentText("high")  # gpt-5.5 supports only "medium"
            name_edit = dialog.table.cellWidget(0, 4)
            name_edit.setText("vampulse-codex")
            name_edit.auto_suggested = False
            with patch("session_hub.QMessageBox.warning") as warning:
                dialog.accept()
            warning.assert_called_once()
            self.assertEqual(dialog.group_rows, [])

    def test_codex_launch_args_scopes_mcp_by_the_actual_resume_execution_directory(self):
        """codex resume actually runs `-C source_cwd-or-cwd`, not `-C cwd` -
        row447 fourth rework. MCP scoping must key off the SAME directory the
        process really executes in, or a resume's display cwd and its real
        source_cwd disagreeing about VAMPULSE membership silently leaks the
        MCP into a directory it must never reach, or wrongly disables it for
        a legitimately-scoped session.
        """
        canonical_root, _worktree, base = self._make_vampulse_fixture()
        outside = base / "elsewhere"
        outside.mkdir()

        # display cwd INSIDE VAMPULSE, real source_cwd OUTSIDE - must scope
        # off source_cwd (outside) and disable the MCP.
        args = session_hub.codex_launch_args(
            str(canonical_root), session_id="sess-1", source_cwd=str(outside),
        )
        self.assertIn("mcp_servers.vampulse.enabled=false", args)
        self.assertIn(str(outside), args)
        self.assertNotIn(str(canonical_root), args)

        # display cwd OUTSIDE VAMPULSE, real source_cwd INSIDE - must scope
        # off source_cwd (inside) and leave the MCP enabled.
        args = session_hub.codex_launch_args(
            str(outside), session_id="sess-2", source_cwd=str(canonical_root),
        )
        self.assertNotIn("mcp_servers.vampulse.enabled=false", args)
        self.assertIn(str(canonical_root), args)
        self.assertNotIn(str(outside), args)

    def test_launch_canonicalizes_tmux_name_and_claude_name_flag_together(self):
        """Full row447-rework chain: a row minted from an unsafe raw name is
        launched, and the tmux session launch() creates, the --name flag
        baked into the real Claude argv, and the status/stop readers
        afterwards all agree on the ONE canonical name - never the raw one
        in one place and the substituted one in another.
        """
        with tempfile.TemporaryDirectory() as fakebin_dir, tempfile.TemporaryDirectory() as temp:
            fakebin = Path(fakebin_dir)
            state = fakebin / "created_name.txt"
            marker = fakebin / "claude_argv.txt"
            cwd_dir = Path(temp) / "vamp"
            cwd_dir.mkdir()

            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                "substitute() { echo \"$1\" | tr '.:' '__'; }\n"
                # Strips a leading '=' (tmux_exact_target's exact-match marker,
                # row447 second rework) before the existing dot/colon-target
                # truncation, so this stub still parses the -t value the same
                # way the real tmux target-spec grammar does.
                "session_part() { echo \"$1\" | sed -E 's/^=//; s/[.:].*//'; }\n"
                'case "$1" in\n'
                '  has-session) t=$(session_part "$3");'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && exit 0 || exit 1 ;;\n'
                '  new-session) n=$(substitute "$4"); echo "$n" > "%s";'
                '    sh -c "${@: -1}"; exit 0 ;;\n'
                '  attach) t=$(session_part "$3");'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && exit 0 || exit 1 ;;\n'
                '  kill-session) t=$(session_part "$3");'
                f'    [ -f "{state}" ] && [ "$(cat "{state}")" = "$t" ] && rm -f "{state}" && exit 0 || exit 1 ;;\n'
                "  set-option) exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n" % state
            )
            fake_tmux.chmod(0o755)

            fake_terminal = fakebin / "gnome-terminal"
            fake_terminal.write_text(
                "#!/bin/bash\n"
                'while [ "$1" != "--" ]; do shift; done\n'
                "shift\n"
                'exec "$@"\n'
            )
            fake_terminal.chmod(0o755)

            fake_claude = fakebin / "claude"
            fake_claude.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{marker}"\n')
            fake_claude.chmod(0o755)

            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            self.addCleanup(window.close)

            raw_name = "gpt-5.6-luna"
            canonical = "gpt-5_6-luna"
            with patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"):
                registered = window.register_group_row(
                    str(cwd_dir), raw_name, "Claude", None
                )
            self.assertEqual(registered["name"], canonical)

            spawn_env = dict(os.environ, PATH=f"{fakebin}:{os.environ.get('PATH', '')}")
            captured = {}

            def fake_spawn(command, *args, **kwargs):
                captured["command"] = command
                subprocess.run(command, env=spawn_env, timeout=5, check=True)

            with (
                patch.object(window, "spawn", side_effect=fake_spawn),
                patch.object(session_hub, "executable", return_value=str(fake_claude)),
                patch.object(
                    session_hub.shutil, "which",
                    side_effect=lambda n: {
                        "tmux": str(fake_tmux), "gnome-terminal": str(fake_terminal),
                    }.get(n),
                ),
            ):
                window.launch(
                    "Claude", None, str(cwd_dir),
                    session_key=registered["override_key"],
                    flag_overrides={"--name": registered["name"]},
                    use_tmux=True,
                )

            command = captured["command"]
            self.assertEqual(command[5], canonical)  # the tmux session name
            claude_command = command[7]
            self.assertIn(f"--name {canonical}", claude_command)
            self.assertNotIn(raw_name, claude_command)

            recorded = marker.read_text(encoding="utf-8").splitlines()
            self.assertIn(canonical, recorded)  # Claude's own --name, at the real child

            row = {"name": registered["name"]}
            with patch.object(
                session_hub.shutil, "which",
                side_effect=lambda n: {"tmux": str(fake_tmux)}.get(n),
            ):
                self.assertEqual(session_hub.group_row_status(row, None, True), "Running")
                session_hub.stop_tmux_session(row["name"])
                self.assertEqual(session_hub.group_row_status(row, None, True), "Stopped")

    def test_rename_group_row_reconciles_live_tmux_atomically_and_refuses_a_collision(self):
        with tempfile.TemporaryDirectory() as fakebin_dir, tempfile.TemporaryDirectory() as temp:
            fakebin = Path(fakebin_dir)
            live = {"old-row", "taken"}

            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                'case "$1" in\n'
                '  list-sessions) printf "%s\\n" ' + " ".join(f'"{n}"' for n in live) + ' ;;\n'
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            fake_tmux.chmod(0o755)

            metadata = {
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [
                            {"name": "old-row", "override_key": "group:/tmp/vamp#old-row"},
                        ],
                    }
                },
                "sessions": {}, "links": {}, "pending_links": [],
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            self.addCleanup(window.close)

            with (
                patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                patch.object(session_hub, "rename_tmux_session") as fake_rename,
                patch.object(
                    session_hub.shutil, "which",
                    side_effect=lambda n: {"tmux": str(fake_tmux)}.get(n),
                ),
            ):
                # Refused: "taken" already names a DIFFERENT live tmux
                # session. Metadata must be untouched - atomic, not
                # renamed-in-metadata-but-tmux-disagrees.
                blocked = window.rename_group_row("/tmp/vamp", "old-row", "taken")
                self.assertEqual(blocked["status"], "error")
                fake_rename.assert_not_called()
                self.assertEqual(
                    metadata["groups"]["/tmp/vamp"]["rows"][0]["name"], "old-row"
                )

                # Allowed: no collision, so both the metadata AND the live
                # tmux session are renamed together.
                fake_rename.return_value = True
                ok = window.rename_group_row("/tmp/vamp", "old-row", "new-row")
                self.assertEqual(ok["status"], "renamed")
                self.assertTrue(ok["tmux_renamed"])
                fake_rename.assert_called_once_with("old-row", "new-row")
                self.assertEqual(
                    metadata["groups"]["/tmp/vamp"]["rows"][0]["name"], "new-row"
                )

    def test_rename_session_name_reconciles_a_codex_conversation_rename_to_tmux(self):
        """The orchestrator's own live repro, reproduced hermetically: a
        Codex row renamed via Session Hub's "Rename session" while its tmux
        session was still named "projects" must rename BOTH together, not
        leave the tmux/peer address stuck on the old name the way plain
        save_override("name", ...) used to (row447 rework finding #3).
        """
        with tempfile.TemporaryDirectory() as fakebin_dir, tempfile.TemporaryDirectory() as temp:
            fakebin = Path(fakebin_dir)
            state = fakebin / "created_name.txt"
            state.write_text("projects")

            # `${3#=}` strips tmux_exact_target's leading '=' before comparing
            # (row447 second rework) - `-t` targets now always arrive here as
            # "=name", not a bare name.
            fake_tmux = fakebin / "tmux"
            fake_tmux.write_text(
                "#!/bin/bash\n"
                'case "$1" in\n'
                '  list-sessions) [ -f "%s" ] && cat "%s"; exit 0 ;;\n'
                '  has-session) [ -f "%s" ] && [ "$(cat "%s")" = "${3#=}" ] && exit 0 || exit 1 ;;\n'
                '  rename-session) [ -f "%s" ] && [ "$(cat "%s")" = "${3#=}" ]'
                ' && echo "$4" > "%s" && exit 0 || exit 1 ;;\n'
                "  *) exit 0 ;;\n"
                "esac\n" % (state, state, state, state, state, state, state)
            )
            fake_tmux.chmod(0o755)

            metadata = {"sessions": {}, "settings": {}, "groups": {}}
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            self.addCleanup(window.close)

            session = session_hub.Session(
                "Codex", "worker9", "old title", "/tmp/vamp", "/tmp/vamp", 0,
                Path("/tmp/worker9.jsonl"),
            )
            metadata.setdefault("sessions", {})[session.key] = {"tmux": True, "name": "projects"}

            with (
                patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                patch.object(
                    session_hub.shutil, "which",
                    side_effect=lambda n: {"tmux": str(fake_tmux)}.get(n),
                ),
            ):
                result = window.rename_session_name(session, "Music Download")

            self.assertEqual(result["status"], "renamed")
            self.assertTrue(result["tmux_renamed"])
            self.assertEqual(state.read_text().strip(), "Music Download")
            self.assertEqual(
                metadata["sessions"][session.key]["name"], "Music Download"
            )

    def _make_vampulse_fixture(self):
        """A REAL temp git repo standing in for the canonical VAMPULSE-game
        checkout, with one REAL `git worktree` of it - not mocked, since
        vampulse_mcp_applies's whole job is resolving real filesystem paths
        (symlinks, `..`) and vampulse_governed_worktrees's is parsing real
        `git worktree list --porcelain` output. Returns
        (canonical_root, governed_worktree, cleanup_dir) and patches
        session_hub.VAMPULSE_PROJECT_ROOT to canonical_root for the caller's
        remaining scope (caller must use it inside a `with` on the returned
        patcher or call .stop() itself - done via addCleanup below).
        """
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        canonical_root = base / "VAMPULSE-game"
        canonical_root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=canonical_root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
             "--allow-empty", "-q", "-m", "init"],
            cwd=canonical_root, check=True,
        )
        worktree = base / "worktrees" / "vamp-worker9"
        worktree.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "vamp-worker9", str(worktree)],
            cwd=canonical_root, check=True,
        )
        patcher = patch.object(session_hub, "VAMPULSE_PROJECT_ROOT", canonical_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return canonical_root, worktree, base

    def test_vampulse_mcp_applies_admits_canonical_root_subdir_and_governed_worktree(self):
        canonical_root, worktree, _base = self._make_vampulse_fixture()
        self.assertTrue(session_hub.vampulse_mcp_applies(canonical_root))
        self.assertTrue(session_hub.vampulse_mcp_applies(canonical_root / "docs"))
        self.assertTrue(session_hub.vampulse_mcp_applies(worktree))

    def test_vampulse_mcp_applies_excludes_sibling_and_textual_prefix_impostor(self):
        canonical_root, _worktree, base = self._make_vampulse_fixture()
        sibling = base / "other-project"
        sibling.mkdir()
        self.assertFalse(session_hub.vampulse_mcp_applies(sibling))

        # Same parent directory, a name that starts with the canonical root's
        # own string but is a different directory - Path.is_relative_to must
        # be used, not `str(cwd).startswith(str(root))`.
        impostor = base / (canonical_root.name + "-old")
        impostor.mkdir()
        self.assertFalse(session_hub.vampulse_mcp_applies(impostor))

    def test_vampulse_mcp_applies_excludes_a_symlink_that_escapes_the_root(self):
        canonical_root, _worktree, base = self._make_vampulse_fixture()
        outside = base / "outside-target"
        outside.mkdir()
        escape_link = canonical_root / "escape"
        escape_link.symlink_to(outside, target_is_directory=True)
        # The symlink's literal path sits inside the canonical root, but it
        # resolves outside it - real-path resolution must catch this, a plain
        # string-prefix check on the unresolved path would not.
        self.assertFalse(session_hub.vampulse_mcp_applies(escape_link))
        self.assertFalse(session_hub.vampulse_mcp_applies(escape_link / "anything"))

    def test_vampulse_mcp_applies_excludes_unrelated_real_directory(self):
        self._make_vampulse_fixture()
        with tempfile.TemporaryDirectory() as unrelated:
            self.assertFalse(session_hub.vampulse_mcp_applies(unrelated))

    def test_vampulse_governed_worktrees_fails_closed_on_a_missing_root(self):
        missing = Path(tempfile.mkdtemp()) / "does-not-exist"
        self.assertEqual(session_hub.vampulse_governed_worktrees(missing), [])

    def test_codex_launch_args_never_touches_the_global_codex_config_file(self):
        # "Do not globally disable the user's other Codex MCPs" (row447 brief):
        # scoping must be a per-launch argv override, never a config.toml edit.
        # Proven directly - patch CODEX_CONFIG to a real file with known
        # content and assert codex_launch_args, for both an in-scope and an
        # out-of-scope cwd, never so much as opens it.
        canonical_root, _worktree, base = self._make_vampulse_fixture()
        fake_config = base / "config.toml"
        original = "[mcp_servers.vampulse]\ncommand = \"nice\"\n"
        fake_config.write_text(original, encoding="utf-8")
        with patch.object(session_hub, "CODEX_CONFIG", fake_config):
            session_hub.codex_launch_args(str(canonical_root), model="gpt-5.6-luna")
            session_hub.codex_launch_args(str(base / "unrelated"), model="gpt-5.6-luna")
        self.assertEqual(fake_config.read_text(encoding="utf-8"), original)

    def test_codex_launch_args_scopes_vampulse_mcp_by_cwd(self):
        canonical_root, worktree, base = self._make_vampulse_fixture()
        in_scope = session_hub.codex_launch_args(str(canonical_root), model="gpt-5.6-luna")
        self.assertNotIn("mcp_servers.vampulse.enabled=false", in_scope)

        in_scope_worktree = session_hub.codex_launch_args(str(worktree))
        self.assertNotIn("mcp_servers.vampulse.enabled=false", in_scope_worktree)

        unrelated = base / "unrelated"
        unrelated.mkdir()
        out_of_scope = session_hub.codex_launch_args(str(unrelated))
        self.assertIn("mcp_servers.vampulse.enabled=false", out_of_scope)
        # Never a blanket MCP disable - only the vampulse server is named.
        self.assertNotIn("mcp_servers.google_sheets.enabled=false", out_of_scope)

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
            # "/tmp/vamp" is a fixture-only path, not a real directory in this
            # environment - launch()'s Path(cwd).is_dir() guard would otherwise fail
            # and pop a REAL modal QMessageBox.warning(), which blocks forever with no
            # user present to click it (row432 audit: this was the suite's actual
            # hang, not tmux/usage I/O). Same precedent as the two other tests here
            # that patch session_hub.Path.is_dir directly.
            with (
                patch.object(session_hub.SessionHub, "spawn") as spawn,
                patch("session_hub.Path.is_dir", return_value=True),
            ):
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

    # --- row432 audit: negative controls for the suite-speed/hermeticity fixes ---

    def test_launch_missing_cwd_shows_warning_dialog_not_a_hang(self):
        """The Path(cwd).is_dir() guard in launch() pops a REAL QMessageBox.warning()
        when the directory doesn't exist - unmocked, that blocks the whole suite
        forever with no user to click it. This was the suite's actual hang (found via
        faulthandler.dump_traceback_later stuck at session_hub.py:6652, not tmux/usage
        I/O as first suspected) - the fixed test above patches
        session_hub.Path.is_dir so its "/tmp/vamp" cwd is treated as real. This test
        exercises the guard itself with QMessageBox mocked, proving it still fires
        (spawn never called) and that mocking it is what keeps this fast rather than
        the guard having been silently removed."""
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            missing = "/tmp/session-hub-row432-does-not-exist"
            self.assertFalse(Path(missing).is_dir())
            with (
                patch.object(session_hub.SessionHub, "spawn") as spawn,
                patch("session_hub.QMessageBox.warning") as warning,
            ):
                window.launch("Claude", None, missing)
            warning.assert_called_once()
            spawn.assert_not_called()
        finally:
            window.close()

    def test_setup_which_net_blocks_real_tmux_lookup_but_passes_through_other_names(self):
        """Regression trap for the shutil.which safety net added in setUp (row432): if
        it were removed or narrowed, a bare SessionHub() would resolve real tmux via
        shutil.which and could hit the real environment's tmux binary/subprocess again
        - the exact seam that crashed two other tests with the sandbox's own
        subprocess.run unpack failure (ValueError: not enough values to unpack).
        Proves the net is active AND that it still passes through a non-tmux binary
        lookup unchanged, so it isn't blanket-stubbing every name."""
        self.assertIsNone(session_hub.shutil.which("tmux"))
        self.assertIsNotNone(session_hub.shutil.which("python3"))

    def test_setup_refresh_usage_net_is_a_stub_but_leaves_the_readers_real(self):
        """Regression trap for the refresh_usage() safety net added in setUp
        (row432): if it were removed, an accidental Qt event-loop tick firing the
        queued refresh_usage() QTimer.singleShot could spawn real UsageWorker
        QRunnables, each with up to a 15s timeout and network access. Proves
        refresh_usage() is stubbed (calling it does not raise or populate
        usage_workers) while the three read_*_usage functions it would have
        dispatched to are left genuinely real - a blanket stub of the readers
        themselves broke test_read_claude_usage_falls_back_to_activity_when_bars_missing
        earlier in this same audit, which is why the net sits on refresh_usage()
        and not on the readers."""
        metadata = {"sessions": {}, "settings": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            window.refresh_usage()
            self.assertEqual(window.usage_workers, {})
            self.assertIsInstance(session_hub.SessionHub.refresh_usage, MagicMock)
        finally:
            window.close()
        payload = {"result": "Last 24h · 1 requests · 1 sessions"}
        completed = session_hub.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with patch("session_hub.subprocess.run", return_value=completed):
            result = session_hub.read_claude_usage()
        self.assertEqual([(a.label, a.requests, a.sessions) for a in result], [("Last 24h", 1, 1)])

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

    def test_resolve_clear_continuations_sibling_absorption_is_independent_of_glob_order(self):
        # task-2127 root cause: PID_DIR.glob() order is filesystem-dependent,
        # not sorted. The bug this discriminates: sibling-id's own tracked
        # PID has a live self-match (no /clear at all for it) but, if its
        # tracking entry was resolved BEFORE old-id's (whose session
        # genuinely vanished), the two competed for the same unclaimed
        # "new-id" and max(updated_ms) could hand new-id to sibling-id
        # instead of old-id - silently absorbing an unrelated, still-active
        # sibling into a continuation that wasn't its own. Same fixture as
        # the sibling test above, but with the tracking files created in
        # the OPPOSITE order (sibling's file first) - this must still land
        # on the same correct link, proving the fix (resolving genuinely-
        # vanished PIDs before still-live ones) doesn't depend on creation/
        # glob order.
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            (pid_dir / "222222.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "sibling-id"})
            )
            (pid_dir / "111111.json").write_text(
                json.dumps({"cwd": "/home/user/vamp", "session_id": "old-id"})
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
            sibling_tracking = json.loads((pid_dir / "222222.json").read_text())
        self.assertTrue(changed)
        (link,) = metadata["links"].values()
        self.assertEqual(set(link["members"]), {"Claude:old-id", "Claude:new-id"})
        # sibling-id must never be repointed at new-id, regardless of order.
        self.assertEqual(sibling_tracking["session_id"], "sibling-id")

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

    def test_resolve_link_active_returns_active_member_when_it_still_exists(self):
        by_key = {
            "Codex:old": session_hub.Session(
                "Codex", "old", "t", "/tmp", "/tmp", 100, Path("/tmp/o.jsonl")
            ),
            "Codex:new": session_hub.Session(
                "Codex", "new", "t", "/tmp", "/tmp", 200, Path("/tmp/n.jsonl")
            ),
        }
        link = {"members": ["Codex:old", "Codex:new"], "active": "Codex:new"}
        result = session_hub.resolve_link_active(link, by_key)
        self.assertEqual(result.native_key, "Codex:new")

    def test_resolve_link_active_repairs_deleted_active_to_newest_member(self):
        by_key = {
            "Codex:old": session_hub.Session(
                "Codex", "old", "t", "/tmp", "/tmp", 100, Path("/tmp/o.jsonl")
            ),
            "Codex:newest": session_hub.Session(
                "Codex", "newest", "t", "/tmp", "/tmp", 900, Path("/tmp/x.jsonl")
            ),
        }
        # "active" names a member missing from by_key (deleted/trashed) -
        # first in members order too, so a reversed-insertion-order guess
        # would have wrongly returned "old".
        link = {
            "members": ["Codex:deleted", "Codex:old", "Codex:newest"],
            "active": "Codex:deleted",
        }
        result = session_hub.resolve_link_active(link, by_key)
        self.assertEqual(result.native_key, "Codex:newest")

    def test_resolve_link_active_tie_break_on_equal_updated_ms_is_deterministic(self):
        by_key = {
            "Codex:aaa": session_hub.Session(
                "Codex", "aaa", "t", "/tmp", "/tmp", 100, Path("/tmp/a.jsonl")
            ),
            "Codex:zzz": session_hub.Session(
                "Codex", "zzz", "t", "/tmp", "/tmp", 100, Path("/tmp/z.jsonl")
            ),
        }
        link = {"members": ["Codex:aaa", "Codex:zzz"], "active": "Codex:gone"}
        result = session_hub.resolve_link_active(link, by_key)
        result_reordered = session_hub.resolve_link_active(
            {"members": ["Codex:zzz", "Codex:aaa"], "active": "Codex:gone"}, by_key
        )
        self.assertEqual(result.native_key, result_reordered.native_key)
        self.assertEqual(result.native_key, "Codex:zzz")

    def test_resolve_link_active_returns_none_when_every_member_is_gone(self):
        link = {"members": ["Codex:gone1", "Codex:gone2"], "active": "Codex:gone1"}
        self.assertIsNone(session_hub.resolve_link_active(link, {}))

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

    def test_find_group_member_session_falls_back_to_name_match_without_link_guard(self):
        # Control for the audit rework: with no linked_session_keys passed,
        # a session_key naming an orphaned link member (every member gone)
        # still falls through to the loose name+cwd match below and wrongly
        # picks an unrelated sibling that merely shares this row's
        # agent_name - the exact old behavior the fail-closed test that
        # follows replaces.
        sibling = session_hub.Session(
            "Claude", "sibling-id", "t", "/tmp/vamp", "/tmp/vamp", 500,
            Path("/tmp/sibling.jsonl"), agent_name="vamp-s1",
        )
        match = session_hub.find_group_member_session(
            {"provider": "Claude", "name": "vamp-s1", "session_key": "Claude:gone"},
            "/tmp/vamp",
            [sibling],
        )
        self.assertEqual(match.native_key, "Claude:sibling-id")

    def test_find_group_member_session_fails_closed_when_session_key_is_orphaned_link_member(self):
        sibling = session_hub.Session(
            "Claude", "sibling-id", "t", "/tmp/vamp", "/tmp/vamp", 500,
            Path("/tmp/sibling.jsonl"), agent_name="vamp-s1",
        )
        match = session_hub.find_group_member_session(
            {"provider": "Claude", "name": "vamp-s1", "session_key": "Claude:gone"},
            "/tmp/vamp",
            [sibling],
            linked_session_keys=frozenset({"Claude:gone"}),
        )
        self.assertIsNone(match)

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

        ambiguous = session_hub.Session(
            "Codex", "id-sibling", "t", "/tmp/vamp", "/tmp/vamp", 600,
            Path("/tmp/c.jsonl"),
        )
        self.assertIsNone(
            session_hub.find_group_member_session(row, "/tmp/vamp", sessions + [ambiguous])
        )

    def test_codex_tmux_native_key_reads_the_open_rollout_fd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc_root = root / "proc"
            sessions_root = root / "sessions"
            session_id = "01a047ab-db9f-78b0-91e2-5a089bc2ddc2"
            rollout = sessions_root / f"rollout-2026-08-28T12-20-40-{session_id}.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text("{}\n")
            process = proc_root / "101"
            (process / "task" / "101").mkdir(parents=True)
            (process / "task" / "101" / "children").write_text("")
            (process / "fd").mkdir()
            (process / "fd" / "9").symlink_to(rollout)
            completed = MagicMock(returncode=0, stdout="101\n")
            with (
                patch("session_hub.shutil.which", return_value="/usr/bin/tmux"),
                patch("session_hub.subprocess.run", return_value=completed),
            ):
                key = session_hub.codex_tmux_native_key(
                    "VAMP-worker4", proc_root=proc_root, sessions_root=sessions_root
                )
            self.assertEqual(key, f"Codex:{session_id}")

    def test_pending_link_uses_exact_tmux_codex_identity(self):
        worker = session_hub.Session(
            "Codex", "worker", "worker", "/tmp/vamp", "/tmp/vamp", 300,
            Path("/tmp/worker.jsonl"),
        )
        orchestrator = session_hub.Session(
            "Codex", "orchestrator", "orchestrator", "/tmp/vamp", "/tmp/vamp", 200,
            Path("/tmp/orchestrator.jsonl"),
        )
        metadata = {
            "sessions": {},
            "links": {"logical": {"members": ["Claude:old"], "active": "Claude:old"}},
            "pending_links": [{
                "logical_key": "logical",
                "target_provider": "Codex",
                "existing_keys": [],
                "cwd": "/tmp/vamp",
                "started_ms": 100,
                "expires_ms": 9999999999999,
                "target_tmux_name": "VAMPULSE-orchestrator",
            }],
        }
        with patch(
            "session_hub.codex_tmux_native_key", return_value="Codex:orchestrator"
        ):
            changed = session_hub.resolve_pending_links(metadata, [worker, orchestrator])
        self.assertTrue(changed)
        self.assertEqual(metadata["links"]["logical"]["active"], "Codex:orchestrator")

    def test_pending_link_refuses_ambiguous_same_cwd_candidates(self):
        sessions = [
            session_hub.Session(
                "Codex", value, value, "/tmp/vamp", "/tmp/vamp", 300,
                Path(f"/tmp/{value}.jsonl"),
            )
            for value in ("worker", "orchestrator")
        ]
        item = {
            "logical_key": "logical",
            "target_provider": "Codex",
            "existing_keys": [],
            "cwd": "/tmp/vamp",
            "started_ms": 100,
            "expires_ms": 9999999999999,
        }
        metadata = {"sessions": {}, "links": {}, "pending_links": [item]}
        self.assertFalse(session_hub.resolve_pending_links(metadata, sessions))
        self.assertEqual(metadata["pending_links"], [item])

    def test_pending_codex_group_row_uses_its_tmux_rollout(self):
        worker = session_hub.Session(
            "Codex", "worker", "worker", "/tmp/vamp", "/tmp/vamp", 300,
            Path("/tmp/worker.jsonl"),
        )
        sibling = session_hub.Session(
            "Codex", "sibling", "sibling", "/tmp/vamp", "/tmp/vamp", 400,
            Path("/tmp/sibling.jsonl"),
        )
        row = {
            "name": "VAMP-worker4",
            "provider": "Codex",
            "session_key": "Codex:old",
            "codex_pending_since": 200,
        }
        metadata = {"groups": {"/tmp/vamp": {"tmux": True, "rows": [row]}}}
        with patch("session_hub.codex_tmux_native_key", return_value="Codex:worker"):
            changed = session_hub.resolve_pending_codex_group_rows(
                metadata, [worker, sibling]
            )
        self.assertTrue(changed)
        self.assertEqual(row["session_key"], "Codex:worker")
        self.assertNotIn("codex_pending_since", row)

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

    def test_manage_group_dialog_launch_selected_rows_preserves_table_order(self):
        # Ctrl/Shift-click selection order need not match visual table order
        # (here: row 2 clicked first, then 0, then 1) - launches must still
        # go out top-to-bottom, not in click order.
        hub = MagicMock()
        dialog = session_hub.ManageGroupDialog.__new__(session_hub.ManageGroupDialog)
        dialog.hub = hub
        dialog.cwd = "/tmp/vamp"
        dialog.reload = MagicMock()
        dialog.pair_at_table_row = MagicMock(
            side_effect=lambda table_row: (
                {
                    "name": f"vamp-s{table_row}",
                    "override_key": f"group:/tmp/vamp#vamp-s{table_row}",
                },
                None,
            )
        )
        index2, index0, index1 = MagicMock(), MagicMock(), MagicMock()
        index2.row.return_value = 2
        index0.row.return_value = 0
        index1.row.return_value = 1
        selection_model = MagicMock()
        selection_model.selectedRows.return_value = [index2, index0, index1]
        dialog.table = MagicMock()
        dialog.table.selectionModel.return_value = selection_model

        dialog.launch_selected_rows()

        called_rows = [call.args[0] for call in dialog.pair_at_table_row.call_args_list]
        self.assertEqual(called_rows, [0, 1, 2])

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

    def test_manage_group_dialog_launch_selected_button_enabled_only_with_selection(self):
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
                    self.assertFalse(dialog.launch_selected_button.isEnabled())
                    dialog.table.selectRow(0)
                    self.assertTrue(dialog.launch_selected_button.isEnabled())
                    dialog.table.clearSelection()
                    self.assertFalse(dialog.launch_selected_button.isEnabled())
                finally:
                    dialog.close()
            finally:
                window.close()

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

    def test_matched_sessions_prefers_row_identity_over_stale_native_key_override(self):
        # task-2127: superseded contract. "ONE name per row" (rename_group_
        # row_in, sync_group_row_name - user 2026-08-22) means the
        # override_key bucket's name is always kept equal to row["name"] on
        # every refresh - a fixture pinning a DIFFERENT value under
        # override_key (the old shape of this test) is unreachable state;
        # sync_group_row_names, called every refresh via discover_sessions,
        # silently overwrites it back to row["name"] before matched_sessions
        # ever runs. What row-identity precedence still has to prove: a
        # STALE rename left under the session's NATIVE key (a leftover from
        # before the session was grouped, or from a previous restart's
        # native key) must never leak through and override the row's own
        # current name.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {
                    "Claude:abc123": {"name": "stale leftover name"},
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
                    self.assertEqual(pairs[0][1].title, "vamp-s1")
                finally:
                    dialog.close()
            finally:
                window.close()

    def test_module_data_paths_all_share_the_forced_test_sandbox_root(self):
        # task-2134: every module-level mutable data path must be born inside
        # the process-wide XDG_DATA_HOME sandbox forced at import time (top of
        # this file) - protecting METADATA_PATH alone while a sibling live
        # path (e.g. the backup dir) stays exposed is the same class of bug.
        sandbox = Path(_TEST_XDG_DATA_HOME)
        for path in (
            session_hub.DATA_DIR,
            _ORIGINAL_METADATA_PATH,
            _ORIGINAL_PID_DIR,
            session_hub.STATUS_DIR,
            session_hub.METADATA_BACKUP_DIR,
            session_hub.TRASH_DIR,
        ):
            self.assertTrue(
                path.is_relative_to(sandbox),
                f"{path} escapes the forced test sandbox {sandbox}",
            )

    def test_metadata_writes_never_escape_the_test_process_sandbox(self):
        # task-2134: replays the exact incident. The row434 leak was NOT a
        # normal unittest run - setUp()'s METADATA_PATH patch was always the
        # outer scope in that path and would have caught the write anyway.
        # The brief names the real trigger: "a scope escape, direct
        # test-method invocation ... can run after that mock ends" - i.e.
        # the test method executed WITHOUT going through TestCase.run(), so
        # setUp() (and its METADATA_PATH/PID_DIR patch) never fires and the
        # dialog.close() write hits whatever session_hub.METADATA_PATH
        # resolves to at import time. A caller/production XDG root ("live"
        # data, sentinel below) is present in the child's environment; the
        # harness script below instantiates the previously-leaking test
        # and calls it directly, exactly reproducing that bypass. The
        # top-of-file XDG_DATA_HOME override must force session_hub onto its
        # own private sandbox at import time - before setUp ever would have
        # run - so the sentinel stays byte-identical regardless.
        sentinel_root = tempfile.mkdtemp(prefix="session-hub-test-sentinel-")
        self.addCleanup(shutil.rmtree, sentinel_root, ignore_errors=True)
        sentinel_metadata_dir = Path(sentinel_root) / "session-hub"
        sentinel_metadata_dir.mkdir()
        sentinel_metadata = sentinel_metadata_dir / "metadata.json"
        sentinel_bytes = (
            b'{"sessions": {"Claude:live-real": {"name": "the users real session"}}, '
            b'"settings": {}, "sentinel": "must-stay-byte-identical"}'
        )
        sentinel_metadata.write_bytes(sentinel_bytes)

        harness = (
            "import os\n"
            "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
            "import test_session_hub as m\n"
            "from PyQt6.QtWidgets import QApplication\n"
            "_app = QApplication.instance() or QApplication([])\n"
            "tc = m.SessionHubTests(\n"
            "    'test_matched_sessions_prefers_row_identity_over_stale_native_key_override'\n"
            ")\n"
            # Deliberately skip tc.setUp() - this is the direct
            # test-method invocation the brief names as the real leak path.
            "tc.test_matched_sessions_prefers_row_identity_over_stale_native_key_override()\n"
        )

        env = dict(os.environ)
        env["XDG_DATA_HOME"] = sentinel_root
        result = subprocess.run(
            [sys.executable, "-c", harness],
            cwd=str(Path(__file__).resolve().parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sentinel_metadata.read_bytes(),
            sentinel_bytes,
            "the sentinel 'live' metadata.json was modified by the test subprocess",
        )

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

    def test_all_sessions_table_restored_to_six_original_columns_running_unchanged(self):
        # task-2136: task-2114 added Status/Last message to All Sessions
        # without being asked, corrupting the saved widths/order of its
        # original six columns. Running is a different table/widget
        # entirely and keeps its own Status/Last message unchanged.
        metadata = {"sessions": {}, "settings": {}, "groups": {}}
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            headers = [
                window.table.horizontalHeaderItem(i).text()
                for i in range(window.table.columnCount())
            ]
            self.assertEqual(
                headers,
                ["Agent", "Model", "Name", "Working directory", "Last updated", "Session ID"],
            )
            running_headers = [
                window.running_table.horizontalHeaderItem(i).text()
                for i in range(window.running_table.columnCount())
            ]
            self.assertEqual(
                running_headers, ["Project", "Name", "Provider", "Status", "Last message"]
            )
        finally:
            window.close()

    def test_all_sessions_column_state_ignores_stale_eight_column_blob_restores_v2(self):
        # An eight-column state blob (from the rejected task-2114 layout)
        # restored onto today's six-column header is exactly the corruption
        # task-2136 fixes - restoreState() with a mismatched section count
        # scrambles widths/order. The settings key changed to
        # main_table_columns_v2 (mirroring ManageGroupDialog's own
        # group_table_columns_v2 precedent) so the old blob is never read;
        # a real six-column blob under the new key still round-trips.
        eight_col_scratch = session_hub.QTableWidget(0, 8)
        eight_col_scratch.setHorizontalHeaderLabels([str(i) for i in range(8)])
        stale_eight_col_state = session_hub.column_widths_state(eight_col_scratch)
        eight_col_scratch.deleteLater()

        with patch("session_hub.read_metadata", return_value={"sessions": {}, "settings": {}, "groups": {}}):
            scratch = session_hub.SessionHub()
        agent_index = list(session_hub.SessionHub.SESSION_TABLE_COLUMNS).index("Agent")
        scratch.table.horizontalHeader().resizeSection(agent_index, 321)
        valid_six_col_state = session_hub.column_widths_state(scratch.table)
        scratch.close()

        metadata = {
            "sessions": {},
            "settings": {
                "main_table_columns": stale_eight_col_state,
                "main_table_columns_v2": valid_six_col_state,
            },
            "groups": {},
        }
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            self.assertEqual(window.table.columnCount(), 6)
            headers = [
                window.table.horizontalHeaderItem(i).text()
                for i in range(window.table.columnCount())
            ]
            self.assertEqual(
                headers,
                ["Agent", "Model", "Name", "Working directory", "Last updated", "Session ID"],
            )
            self.assertEqual(window.table.horizontalHeader().sectionSize(agent_index), 321)
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
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
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
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
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

    def test_launch_group_row_resumes_saved_history_after_restart(self):
        metadata = {
            "sessions": {},
            "settings": {},
            "groups": {
                "/tmp/vamp": {
                    "cwd": "/tmp/vamp",
                    "tmux": True,
                    "rows": [{
                        "name": "VAMP-worker4",
                        "provider": "Codex",
                        "override_key": "group:/tmp/vamp#VAMP-worker4",
                        "session_key": "Codex:old-worker",
                    }],
                }
            },
        }
        history = session_hub.Session(
            "Codex", "old-worker", "worker", "/tmp/vamp", "/tmp/vamp", 100,
            Path("/tmp/worker.jsonl"),
        )
        with patch("session_hub.read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            with (
                patch("session_hub.tmux_session_alive", return_value=False),
                patch("session_hub.claude_sessions", return_value=[]),
                patch("session_hub.codex_sessions", return_value=[history]),
                patch("session_hub.antigravity_sessions", return_value=[]),
                patch.object(
                    window,
                    "resume_group_row",
                    return_value={"status": "resumed", "name": "VAMP-worker4"},
                ) as resume,
                patch.object(window, "launch") as launch,
            ):
                result = window.launch_group_row("/tmp/vamp", "VAMP-worker4")
            self.assertEqual(result["status"], "resumed")
            resume.assert_called_once_with("/tmp/vamp", "VAMP-worker4")
            launch.assert_not_called()
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
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
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
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.codex_sessions", return_value=[live]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
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
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.resume_group_row("/tmp/vamp", "vamp-s1")
                launch.assert_not_called()
                self.assertEqual(result["status"], "error")
            finally:
                window.close()

    def test_resume_group_row_selects_active_codex_member_over_older_linked_rollout(self):
        # task-2126: relaunching used to find the row's stale saved
        # session_key literally (an unlinked candidates() call never applied
        # metadata["links"]) instead of the link's current active member -
        # reopening an older linked rollout instead of the latest
        # conversation. The row's own session_key still names the OLD
        # member; only metadata["links"]["active"] has been advanced.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:worker": {
                        "members": ["Codex:old-rollout", "Codex:new-rollout"],
                        "active": "Codex:new-rollout",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [{
                            "name": "VAMP-worker4",
                            "provider": "Codex",
                            "override_key": "group:/tmp/vamp#VAMP-worker4",
                            "session_key": "Codex:old-rollout",
                        }],
                    }
                },
            }
            old = session_hub.Session(
                "Codex", "old-rollout", "VAMP-worker4", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/old.jsonl"),
            )
            new = session_hub.Session(
                "Codex", "new-rollout", "VAMP-worker4", "/tmp/vamp", "/tmp/vamp", 500,
                Path("/tmp/new.jsonl"),
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.codex_sessions", return_value=[old, new]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.resume_group_row("/tmp/vamp", "VAMP-worker4")
                self.assertEqual(result, {"status": "resumed", "name": "VAMP-worker4"})
                # Records the exact native (provider, session_id) launch
                # actually received, not just the row title/status.
                self.assertEqual(launch.call_args.args[0], "Codex")
                self.assertEqual(launch.call_args.args[1], "new-rollout")
            finally:
                window.close()

    def test_resume_group_row_selects_active_claude_member_over_older_linked_rollout(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:s1": {
                        "members": ["Claude:old-clear", "Claude:post-clear"],
                        "active": "Claude:post-clear",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [{
                            "name": "vamp-s1",
                            "override_key": "group:/tmp/vamp#vamp-s1",
                            "session_key": "Claude:old-clear",
                        }],
                    }
                },
            }
            old = session_hub.Session(
                "Claude", "old-clear", "vamp-s1", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/old.jsonl"), agent_name="vamp-s1",
            )
            new = session_hub.Session(
                "Claude", "post-clear", "vamp-s1", "/tmp/vamp", "/tmp/vamp", 500,
                Path("/tmp/new.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[old, new]),
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.resume_group_row("/tmp/vamp", "vamp-s1")
                self.assertEqual(launch.call_args.args[0], "Claude")
                self.assertEqual(launch.call_args.args[1], "post-clear")
            finally:
                window.close()

    def test_resume_group_row_repairs_missing_active_to_newest_valid_member(self):
        # link["active"] names a member that no longer exists (deleted/
        # trashed session) - must repair to the newest surviving member by
        # updated_ms rather than leaving the row unmatched.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:worker": {
                        "members": ["Codex:old-rollout", "Codex:deleted", "Codex:newest"],
                        "active": "Codex:deleted",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [{
                            "name": "VAMP-worker4",
                            "provider": "Codex",
                            "override_key": "group:/tmp/vamp#VAMP-worker4",
                            "session_key": "Codex:old-rollout",
                        }],
                    }
                },
            }
            old = session_hub.Session(
                "Codex", "old-rollout", "VAMP-worker4", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/old.jsonl"),
            )
            newest = session_hub.Session(
                "Codex", "newest", "VAMP-worker4", "/tmp/vamp", "/tmp/vamp", 900,
                Path("/tmp/newest.jsonl"),
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.codex_sessions", return_value=[old, newest]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.resume_group_row("/tmp/vamp", "VAMP-worker4")
                self.assertEqual(launch.call_args.args[1], "newest")
            finally:
                window.close()

    def test_resume_group_row_rejects_stale_name_duplicate_from_other_provider(self):
        # A same-titled session from a provider the row/link never named
        # must not be picked just because the candidate pool now spans every
        # enabled provider - only an exact native-key or linked_keys match
        # may select a session, never its display title.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [{
                            "name": "vamp-s1",
                            "provider": "Claude",
                            "override_key": "group:/tmp/vamp#vamp-s1",
                            "session_key": "Claude:abc123",
                        }],
                    }
                },
            }
            real = session_hub.Session(
                "Claude", "abc123", "vamp-s1", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/real.jsonl"), agent_name="vamp-s1",
            )
            decoy = session_hub.Session(
                "Codex", "unrelated-decoy", "vamp-s1", "/tmp/vamp", "/tmp/vamp", 999,
                Path("/tmp/decoy.jsonl"),
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[real]),
                    patch("session_hub.codex_sessions", return_value=[decoy]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    window.resume_group_row("/tmp/vamp", "vamp-s1")
                self.assertEqual(launch.call_args.args[0], "Claude")
                self.assertEqual(launch.call_args.args[1], "abc123")
            finally:
                window.close()

    def test_resume_group_row_fails_closed_when_linked_members_all_missing(self):
        # Every member of the row's link is gone (deleted/trashed) - must
        # error and never launch, not fall back to a guess.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:worker": {
                        "members": ["Codex:gone1", "Codex:gone2"],
                        "active": "Codex:gone1",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [{
                            "name": "VAMP-worker4",
                            "provider": "Codex",
                            "override_key": "group:/tmp/vamp#VAMP-worker4",
                            "session_key": "Codex:gone1",
                        }],
                    }
                },
            }
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.resume_group_row("/tmp/vamp", "VAMP-worker4")
                launch.assert_not_called()
                self.assertEqual(result["status"], "error")
            finally:
                window.close()

    def test_resume_group_row_rejects_stale_name_duplicate_from_orphaned_link_sibling(self):
        # Audit rework of test_resume_group_row_rejects_stale_name_duplicate_
        # from_other_provider: that test keeps the real exact-match session
        # present, so it passes on ordinary exact-key precedence without
        # ever reaching the name+cwd fallback. Here the row's link has every
        # member gone and the only live Claude session is an unrelated
        # sibling sharing this row's agent_name/cwd - see the
        # find_group_member_session control test proving the old fallback
        # would have picked it.
        with tempfile.TemporaryDirectory() as temp:
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:s1": {
                        "members": ["Claude:gone1", "Claude:gone2"],
                        "active": "Claude:gone1",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [{
                            "name": "vamp-s1",
                            "provider": "Claude",
                            "override_key": "group:/tmp/vamp#vamp-s1",
                            "session_key": "Claude:gone1",
                        }],
                    }
                },
            }
            sibling = session_hub.Session(
                "Claude", "unrelated-sibling", "vamp-s1", "/tmp/vamp", "/tmp/vamp", 999,
                Path("/tmp/sibling.jsonl"), agent_name="vamp-s1",
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch("session_hub.claude_sessions", return_value=[sibling]),
                    patch("session_hub.codex_sessions", return_value=[]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", Path(temp) / "metadata.json"),
                ):
                    result = window.resume_group_row("/tmp/vamp", "vamp-s1")
                launch.assert_not_called()
                self.assertEqual(result["status"], "error")
            finally:
                window.close()

    def test_resume_group_row_persists_repaired_link_active_to_metadata_file(self):
        # Audit rework: resolve_link_active only SELECTS a repair; it was
        # never written back, so metadata.json kept naming the deleted
        # member forever and a CLI resume run (no prior GUI refresh)
        # re-derived the same repair from scratch on every call. Assert by
        # reloading the actual written file, not the in-memory dict.
        with tempfile.TemporaryDirectory() as temp:
            metadata_path = Path(temp) / "metadata.json"
            metadata = {
                "sessions": {},
                "settings": {},
                "links": {
                    "manual:worker": {
                        "members": ["Codex:old-rollout", "Codex:deleted", "Codex:newest"],
                        "active": "Codex:deleted",
                    }
                },
                "groups": {
                    "/tmp/vamp": {
                        "cwd": "/tmp/vamp",
                        "rows": [{
                            "name": "VAMP-worker4",
                            "provider": "Codex",
                            "override_key": "group:/tmp/vamp#VAMP-worker4",
                            "session_key": "Codex:old-rollout",
                        }],
                    }
                },
            }
            old = session_hub.Session(
                "Codex", "old-rollout", "VAMP-worker4", "/tmp/vamp", "/tmp/vamp", 100,
                Path("/tmp/old.jsonl"),
            )
            newest = session_hub.Session(
                "Codex", "newest", "VAMP-worker4", "/tmp/vamp", "/tmp/vamp", 900,
                Path("/tmp/newest.jsonl"),
            )
            with patch("session_hub.read_metadata", return_value=metadata):
                window = session_hub.SessionHub()
            try:
                with (
                    patch.object(session_hub.SessionHub, "launch"),
                    patch("session_hub.claude_sessions", return_value=[]),
                    patch("session_hub.codex_sessions", return_value=[old, newest]),
                    patch("session_hub.antigravity_sessions", return_value=[]),
                    patch("session_hub.METADATA_PATH", metadata_path),
                ):
                    window.resume_group_row("/tmp/vamp", "VAMP-worker4")
                on_disk = json.loads(metadata_path.read_text())
                self.assertEqual(
                    on_disk["links"]["manual:worker"]["active"], "Codex:newest"
                )
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

    def test_launch_new_group_sessions_dialog_will_launch_false_hides_launch_copy(self):
        # task-2137: "Add new…" reuses this dialog but must not claim it
        # launches anything - the tmux choice has no effect when nothing is
        # launched, so it's hidden rather than shown and then ignored.
        launch_dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
        self.assertEqual(launch_dialog.windowTitle(), "Launch new sessions")
        self.assertFalse(launch_dialog.tmux_checkbox.isHidden())
        ok_button = launch_dialog.findChild(
            session_hub.QDialogButtonBox
        ).button(session_hub.QDialogButtonBox.StandardButton.Ok)
        self.assertEqual(ok_button.text(), "Launch")
        launch_dialog.close()

        add_dialog = session_hub.LaunchNewGroupSessionsDialog(
            "/tmp/vamp", set(), False, will_launch=False
        )
        self.assertEqual(add_dialog.windowTitle(), "Add new sessions")
        self.assertTrue(add_dialog.tmux_checkbox.isHidden())
        ok_button = add_dialog.findChild(
            session_hub.QDialogButtonBox
        ).button(session_hub.QDialogButtonBox.StandardButton.Ok)
        self.assertEqual(ok_button.text(), "Add")
        add_dialog.close()

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
        # Column 4 is Name - column 3 is Account, inserted by 78981ee (per-row
        # Claude account picker) between the old Effort/Name columns. This
        # test targeted the pre-78981ee index and had been silently writing
        # into the (usually hidden) Account QLabel ever since, so the real
        # Name field kept its unique auto-suggested default and no duplicate
        # was ever actually formed - warning() was never called for real.
        dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
        dialog.add_row()
        name_edit_0 = dialog.table.cellWidget(0, 4)
        name_edit_1 = dialog.table.cellWidget(1, 4)
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
        # Column 4 is Name; see the sibling duplicate-name test for why 3 is wrong.
        dialog = session_hub.LaunchNewGroupSessionsDialog(
            "/tmp/vamp", {"vampulse-fable"}, False
        )
        name_edit = dialog.table.cellWidget(0, 4)
        name_edit.setText("vampulse-fable")
        name_edit.auto_suggested = False
        with patch("session_hub.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.group_rows, [])
        dialog.close()

    def test_launch_new_group_sessions_dialog_wrong_name_column_never_admits_duplicates(self):
        # Negative control for the two duplicate-name tests above: writing
        # into column 3 (Account) instead of the real Name field (4) is
        # exactly the pre-78981ee bug shape those tests silently had - the
        # real Name field keeps its unique auto-suggested default, so no
        # duplicate ever actually forms and warning() is never called. This
        # must fail for that reason while the corrected tests (column 4)
        # pass, proving the column-index fix is what discriminates.
        dialog = session_hub.LaunchNewGroupSessionsDialog("/tmp/vamp", set(), False)
        dialog.add_row()
        wrong_widget_0 = dialog.table.cellWidget(0, 3)
        wrong_widget_1 = dialog.table.cellWidget(1, 3)
        wrong_widget_0.setText("same-name")
        wrong_widget_1.setText("same-name")
        with patch("session_hub.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_not_called()
        self.assertEqual(len(dialog.group_rows), 2)
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
            # Column 4 is Name; see test_launch_new_group_sessions_dialog_rejects_duplicate_names.
            name_edit = dialog.table.cellWidget(0, 4)
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
                    # account_config_dir: shipped by 78981ee (per-row Claude
                    # account picker) - always present in rows(), None for a
                    # Codex row (Account is a Claude-only concept there).
                    "account_config_dir": None,
                },
            )
            # Switching a Claude row's combo back keeps the existing combo behavior.
            provider_combo.setCurrentIndex(provider_combo.findData("Claude"))
            self.assertIsInstance(dialog.table.cellWidget(0, 1), session_hub.QComboBox)
            self.assertIsInstance(dialog.table.cellWidget(0, 2), session_hub.QLabel)
            # Provider-switch state left stale: switching back must
            # actually replace the row's Model/Effort widgets
            # (set_model_widget, called from on_provider_changed), not
            # just swap the type check above - a widget reference kept
            # from the Codex leg would leak "gpt-5.6-sol"/"medium" into a
            # row rows() now reports as Claude.
            rows = dialog.rows()
            self.assertEqual(rows[0]["provider"], "Claude")
            self.assertIsNone(rows[0]["reasoning_effort"])
            self.assertNotEqual(rows[0]["model"], "gpt-5.6-sol")
            dialog.close()

    def test_add_new_rows_into_group_saves_all_rows_without_launching(self):
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
                    window.add_new_rows_into_group(temp, rows)
                launch.assert_not_called()
                saved = window.metadata["groups"][temp]
                self.assertEqual(
                    {row["name"] for row in saved["rows"]},
                    {"vampulse-fable", "vampulse-sonnet"},
                )
            finally:
                window.close()

    def test_add_new_rows_into_group_saves_codex_row_without_pending_marker(self):
        # codex_pending_since only means anything for a process just
        # launched - a saved-but-never-launched row must not carry one, or
        # its first real launch reads as a stale/orphaned relaunch.
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
                    window.add_new_rows_into_group(temp, rows)
                launch.assert_not_called()
                saved_row = window.metadata["groups"][temp]["rows"][0]
                self.assertEqual(saved_row["provider"], "Codex")
                self.assertNotIn("codex_pending_since", saved_row)
                self.assertEqual(
                    window.metadata["sessions"][saved_row["override_key"]]["model"], "gpt-5"
                )
            finally:
                window.close()

    def test_add_new_rows_into_group_merges_without_duplicating_existing_rows(self):
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
                    patch.object(session_hub.SessionHub, "launch") as launch,
                    patch(
                        "session_hub.METADATA_PATH", Path(temp) / "metadata.json"
                    ),
                ):
                    window.add_new_rows_into_group(temp, rows)
                launch.assert_not_called()
                saved = window.metadata["groups"][temp]
                self.assertEqual(len(saved["rows"]), 2)
            finally:
                window.close()

    def test_manage_group_dialog_add_new_rows_delegates_to_hub_no_launch_dialog(self):
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
                        ) as dialog_ctor,
                        patch.object(window, "add_new_rows_into_group") as add_new,
                    ):
                        dialog.add_new_rows()
                    add_new.assert_called_once_with(
                        "/tmp/vamp", [{"name": "vamp-new", "model": None}]
                    )
                    self.assertFalse(dialog_ctor.call_args.kwargs["will_launch"])
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
        # "/home/user" is outside the canonical VAMPULSE root (row447), so
        # codex_launch_args now always adds its own "-c
        # mcp_servers.vampulse.enabled=false" here regardless of effort -
        # "-c" is no longer exclusively the reasoning-effort flag's marker.
        no_effort_command = window.terminal_command("Codex", None, "/home/user")
        self.assertNotIn(
            "model_reasoning_effort=", " ".join(no_effort_command)
        )
        self.assertIn("mcp_servers.vampulse.enabled=false", no_effort_command)
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


def _iso(epoch: float) -> str:
    """Codex rollout timestamp format for a given epoch: '...Z', UTC."""
    from datetime import timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class SessionActivityTests(unittest.TestCase):
    """status_pipeline_plan.md's contract: one provider-neutral activity
    verdict (session_activity), with liveness and activity kept as separate
    facts and no provider branch allowed to diverge or return blank."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp = Path(temp_dir.name)
        for name, value in (
            ("STATUS_DIR", self.temp / "status"),
            ("PID_DIR", self.temp / "pids"),
            ("METADATA_PATH", self.temp / "metadata.json"),
        ):
            patcher = patch.object(session_hub, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Same real-I/O safety net as SessionHubTests.setUp (row432 audit) - this
        # class also constructs a bare SessionHub() at least once.
        real_which = shutil.which

        def _which_no_tmux(name, *args, **kwargs):
            if name == "tmux":
                return None
            return real_which(name, *args, **kwargs)

        which_patcher = patch.object(session_hub.shutil, "which", side_effect=_which_no_tmux)
        which_patcher.start()
        self.addCleanup(which_patcher.stop)

        # Stub refresh_usage() itself, not the three read_*_usage functions - see
        # SessionHubTests.setUp's comment for why (a blanket reader stub broke a test
        # that exercises read_claude_usage() directly).
        refresh_usage_patcher = patch.object(session_hub.SessionHub, "refresh_usage")
        refresh_usage_patcher.start()
        self.addCleanup(refresh_usage_patcher.stop)

    def _rollout(self, records: list[dict]) -> Path:
        path = self.temp / f"rollout_{len(list(self.temp.glob('rollout_*.jsonl')))}.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        return path

    def _event(self, epoch: float, kind: str) -> dict:
        return {"timestamp": _iso(epoch), "type": "event_msg", "payload": {"type": kind}}

    # --- live + active turn => Working -------------------------------
    def test_codex_working_when_task_started_is_the_latest_turn_event(self):
        path = self._rollout([self._event(time.time(), "task_started")])
        session = session_hub.Session("Codex", "id-work", "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "working")

    def test_claude_working_from_hook_evidence_when_live(self):
        session_id = "id-claude-work"
        session_hub.write_session_status(session_id, "working", "")
        session = session_hub.Session(
            "Claude", session_id, "t", "/tmp", "/tmp", 0, Path("/tmp/x.jsonl")
        )
        with patch.object(session_hub, "session_is_tracked_alive", return_value=True):
            state, _ = session_hub.session_activity(session)
        self.assertEqual(state, "working")

    # --- live + completed turn => Done/Idle, never blank --------------
    def test_codex_done_when_task_complete_is_the_latest_turn_event(self):
        path = self._rollout([
            self._event(time.time() - 5, "task_started"),
            self._event(time.time(), "task_complete"),
        ])
        session_id = "id-done"
        session_hub.write_session_status(session_id, "done", "finished")
        session = session_hub.Session("Codex", session_id, "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, detail = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "done")
        self.assertEqual(detail, "finished")
        self.assertNotEqual(state, "")

    def test_codex_done_from_transcript_alone_when_notify_hook_never_wrote(self):
        # A turn already finished in the transcript before session-hub's own
        # notify hook ever wrote a status file for it - must still read as
        # "done", never blank (the exact reported bug: a live Codex row with
        # no status file showed nothing at all).
        path = self._rollout([self._event(time.time(), "task_complete")])
        session = session_hub.Session("Codex", "id-no-hook", "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "done")

    # --- needs-input where the provider exposes it ---------------------
    def test_claude_needs_input_from_notification_hook_payload(self):
        mapped = session_hub.hook_event_to_status({
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "approve?",
        })
        self.assertEqual(mapped, ("needs_input", "approve?"))

    def test_codex_notify_never_produces_needs_input(self):
        # Documented, deliberate limitation (hook_event_to_status_codex):
        # Codex approval prompts are a separate, non-hookable mechanism.
        mapped = session_hub.hook_event_to_status_codex({
            "type": "approval-request", "last-assistant-message": "approve?",
        })
        self.assertIsNone(mapped)

    # --- stopped process with fresh-looking status => no live badge ----
    def test_claude_fresh_status_without_liveness_signal_reads_unknown(self):
        session_id = "id-stale"
        session_hub.write_session_status(session_id, "working", "")
        session = session_hub.Session(
            "Claude", session_id, "t", "/tmp", "/tmp", 0, Path("/tmp/x.jsonl")
        )
        # No PID tracking file and no tmux identity passed - the app has no
        # liveness signal for this session at all, so it must not surface
        # the fresh-looking "working" status file as live activity.
        state, _ = session_hub.session_activity(session)
        self.assertEqual(state, "unknown")

    def test_codex_stopped_tmux_reads_unknown_even_with_fresh_status(self):
        session_id = "id-stopped-tmux"
        session_hub.write_session_status(session_id, "working", "")
        path = self._rollout([self._event(time.time(), "task_started")])
        session = session_hub.Session("Codex", session_id, "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=False):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "unknown")

    # --- stale status older than a new turn => new turn wins -----------
    def test_codex_new_task_started_after_stale_done_status_wins(self):
        session_id = "id-restart"
        session_hub.write_session_status(session_id, "done", "old turn")
        path = self._rollout([self._event(time.time() + 5, "task_started")])
        session = session_hub.Session("Codex", session_id, "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, detail = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "working")

    # --- newer task_complete/turn_aborted beats a stale "working" status ---
    def test_codex_task_complete_newer_than_stale_working_status_wins(self):
        session_id = "id-stale-working"
        session_hub.write_session_status(session_id, "working", "")
        path = self._rollout([
            self._event(time.time() - 5, "task_started"),
            self._event(time.time() + 5, "task_complete"),
        ])
        session = session_hub.Session("Codex", session_id, "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "done")

    def test_codex_turn_aborted_newer_than_stale_working_status_wins(self):
        session_id = "id-stale-working-aborted"
        session_hub.write_session_status(session_id, "working", "")
        path = self._rollout([
            self._event(time.time() - 5, "task_started"),
            self._event(time.time() + 5, "turn_aborted"),
        ])
        session = session_hub.Session("Codex", session_id, "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "done")

    def test_codex_working_status_survives_an_older_task_complete(self):
        # Negative control: a "working" status newer than the last completion
        # event must NOT be overridden - only a newer completion beats it.
        session_id = "id-working-genuinely-current"
        path = self._rollout([self._event(time.time() - 5, "task_complete")])
        session_hub.write_session_status(session_id, "working", "")
        session = session_hub.Session("Codex", session_id, "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "working")

    # --- two same-cwd sessions => exact ids, no cross-talk --------------
    def test_two_same_cwd_sessions_get_independent_activity_states(self):
        session_hub.write_session_status("id-a", "working", "")
        session_hub.write_session_status("id-b", "needs_input", "pick one")
        a = session_hub.Session("Claude", "id-a", "a", "/tmp/vamp", "/tmp/vamp", 100, Path("/tmp/a.jsonl"))
        b = session_hub.Session("Claude", "id-b", "b", "/tmp/vamp", "/tmp/vamp", 200, Path("/tmp/b.jsonl"))
        with patch.object(session_hub, "session_is_tracked_alive", return_value=True):
            state_a, _ = session_hub.session_activity(a)
            state_b, _ = session_hub.session_activity(b)
        self.assertEqual(state_a, "working")
        self.assertEqual(state_b, "needs_input")

    # --- malformed/partial last transcript line => bounded fallback -----
    def test_codex_tail_survives_malformed_trailing_bytes(self):
        ok = json.dumps(self._event(time.time() - 10, "thread_settings_applied"))
        started = json.dumps(self._event(time.time(), "task_started"))
        path = self.temp / "malformed.jsonl"
        path.write_bytes(
            (ok + "\n" + started + "\n").encode("utf-8")
            + b'{"timestamp": "2026-08-29T00:00:0'  # truncated trailing record
        )
        session = session_hub.Session("Codex", "id-malformed", "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "working")

    def test_codex_tail_survives_a_large_trailing_tool_output_block(self):
        """A turn's own tool-output line can exceed 1MB (rework item 5) - a
        FIXED 64KB tail read lands entirely inside that one blob and never
        sees the task_started marker sitting just before it. The escalating
        window (_codex_tail_turn_state_scan) must keep growing until it does."""
        started = self._event(time.time() - 1, "task_started")
        huge_tool_output = {
            "timestamp": _iso(time.time()),
            "type": "event_msg",
            "payload": {"type": "item_completed", "blob": "x" * 1_500_000},
        }
        path = self._rollout([started, huge_tool_output])
        self.assertGreater(path.stat().st_size, session_hub._CODEX_TAIL_BYTES)
        session = session_hub.Session("Codex", "id-huge-tail", "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "working")

    def test_codex_tail_cache_reuses_unchanged_file_but_rescans_after_a_write(self):
        path = self._rollout([self._event(time.time(), "task_started")])
        with patch.object(
            session_hub, "_codex_tail_turn_state_scan",
            wraps=session_hub._codex_tail_turn_state_scan,
        ) as scan, patch.object(
            session_hub, "_codex_tail_turn_state_delta",
            wraps=session_hub._codex_tail_turn_state_delta,
        ) as delta:
            first = session_hub._codex_tail_turn_state(path)
            second = session_hub._codex_tail_turn_state(path)
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(delta.call_count, 0)
            self.assertEqual(first, second)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._event(time.time(), "task_complete")) + "\n")
            third = session_hub._codex_tail_turn_state(path)
            # Growth is scanned incrementally, not re-derived from scratch -
            # the cold/full scan must NOT run again.
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(delta.call_count, 1)
            self.assertEqual(third[0], "task_complete")

    def test_codex_tail_delta_carries_forward_a_marker_past_the_ceiling(self):
        """The exact reported false-Done ordering: a task_started is found
        while the turn's transcript is still small, then that SAME open
        turn keeps emitting tool output (no task_complete/turn_aborted in
        between) until the marker is more than _CODEX_TAIL_MAX_BYTES from
        the new end of file. A fresh cold scan from the new EOF would never
        see it again; the incremental delta path must still report the
        turn as working because nothing newer appeared in what was actually
        appended since it was found."""
        path = self._rollout([self._event(time.time(), "task_started")])
        first = session_hub._codex_tail_turn_state(path)
        self.assertEqual(first[0], "task_started")

        filler = {
            "timestamp": _iso(time.time()),
            "type": "event_msg",
            "payload": {"type": "item_completed", "blob": "x" * 500_000},
        }
        with path.open("a", encoding="utf-8") as handle:
            for _ in range(20):  # >= 10MB, past _CODEX_TAIL_MAX_BYTES (8MB)
                handle.write(json.dumps(filler) + "\n")
        self.assertGreater(path.stat().st_size, session_hub._CODEX_TAIL_MAX_BYTES)

        grown = session_hub._codex_tail_turn_state(path)
        self.assertEqual(grown[0], "task_started")
        self.assertEqual(grown[1], first[1])

        session = session_hub.Session("Codex", "id-mega-turn", "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "working")

        # Inverse: the turn actually completes (a tiny delta this time) -
        # the carried-forward marker must not freeze the verdict forever.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._event(time.time(), "task_complete")) + "\n")
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "done")

    def test_codex_tail_delta_a_truncated_file_gets_a_cold_scan(self):
        """Negative control: if the file is SMALLER than the cached size (a
        truncation, or a genuinely different/rotated file reusing the same
        path), the incremental delta path - which cannot handle a negative
        range - must not be used; a full cold scan must run instead."""
        path = self._rollout([self._event(time.time(), "task_started")])
        first = session_hub._codex_tail_turn_state(path)
        self.assertEqual(first[0], "task_started")
        self.assertGreater(path.stat().st_size, 50)

        os.truncate(path, 5)  # unambiguously smaller than the cached size
        with patch.object(
            session_hub, "_codex_tail_turn_state_delta",
            wraps=session_hub._codex_tail_turn_state_delta,
        ) as delta, patch.object(
            session_hub, "_codex_tail_turn_state_scan",
            wraps=session_hub._codex_tail_turn_state_scan,
        ) as scan:
            second = session_hub._codex_tail_turn_state(path)
        self.assertEqual(delta.call_count, 0)
        self.assertEqual(scan.call_count, 1)
        self.assertIsNone(second)

    def test_codex_tail_delta_an_equal_size_changed_identity_file_gets_a_cold_scan(self):
        """Negative control for identity: a same-path replacement that
        happens to land on the SAME byte size (so a size-only check would
        read it as "no growth, nothing to do") must still force a cold
        scan once its (dev, ino) or mtime no longer matches what was
        cached - trusting size alone here would silently keep serving the
        old file's stale verdict forever."""
        path = self._rollout([self._event(time.time(), "task_started")])
        first = session_hub._codex_tail_turn_state(path)
        self.assertEqual(first[0], "task_started")
        old_size = path.stat().st_size

        # "turn_aborted" is shorter than "task_started", so there is always
        # non-negative padding room to hit the exact same byte count.
        replacement = json.dumps(self._event(time.time(), "turn_aborted"))
        self.assertLessEqual(len(replacement) + 1, old_size)
        replacement = replacement + " " * (old_size - len(replacement) - 1) + "\n"
        self.assertEqual(len(replacement), old_size)  # exact same byte count
        path.write_text(replacement, encoding="utf-8")

        with patch.object(
            session_hub, "_codex_tail_turn_state_delta",
            wraps=session_hub._codex_tail_turn_state_delta,
        ) as delta:
            second = session_hub._codex_tail_turn_state(path)
        self.assertEqual(delta.call_count, 0)
        self.assertEqual(second[0], "turn_aborted")

    def test_codex_cold_scan_finds_a_marker_more_than_the_old_ceiling_behind_eof(self):
        """Session Hub restarting (or seeing a rollout for the first time)
        has no cached resume point to trust - the scan itself must find a
        marker regardless of how far behind EOF it sits, written in ONE
        shot so nothing is cached incrementally along the way. An
        arbitrary finite ceiling here is exactly what reproduced the false
        Done against the real 1.68GB live orchestrator rollout, whose
        genuinely still-open turn's task_started sat ~20.4MB behind EOF."""
        started = self._event(time.time(), "task_started")
        filler = {
            "timestamp": _iso(time.time()),
            "type": "event_msg",
            "payload": {"type": "item_completed", "blob": "x" * 500_000},
        }
        path = self._rollout([started] + [filler] * 20)  # >= 10MB in one write
        self.assertGreater(path.stat().st_size, session_hub._CODEX_TAIL_MAX_BYTES)

        result = session_hub._codex_tail_turn_state(path)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "task_started")

        session = session_hub.Session("Codex", "id-cold-deep", "t", "/tmp", "/tmp", 0, path)
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "working")

    def test_codex_delta_scan_finds_a_marker_near_the_start_of_a_large_delta(self):
        """More than the old ceiling can be appended between two GUI
        refreshes in one burst (a flurry of tool output). A newer marker
        sitting near the BEGINNING of that whole delta - not just its
        tail - must still be found, checked in both directions: a turn
        that just started, and one that completed right away with a large
        burst of unrelated filler following it before the next look."""
        path = self._rollout([self._event(time.time() - 100, "task_complete")])
        first = session_hub._codex_tail_turn_state(path)
        self.assertEqual(first[0], "task_complete")
        size_before_burst = path.stat().st_size

        filler = {
            "timestamp": _iso(time.time()),
            "type": "event_msg",
            "payload": {"type": "item_completed", "blob": "x" * 500_000},
        }
        # Direction 1: a new task_started lands at the START of a large
        # burst, nothing newer after it - must read as working.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._event(time.time() - 90, "task_started")) + "\n")
            for _ in range(20):
                handle.write(json.dumps(filler) + "\n")
        self.assertGreater(
            path.stat().st_size - size_before_burst, session_hub._CODEX_TAIL_MAX_BYTES
        )
        grown = session_hub._codex_tail_turn_state(path)
        self.assertEqual(grown[0], "task_started")
        size_after_direction_1 = path.stat().st_size

        # Direction 2: that turn completes immediately, but a SEPARATE
        # large burst of unrelated filler follows before the next look -
        # the task_complete near the burst's start must still win over the
        # stale "working" carried forward from direction 1.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._event(time.time() - 80, "task_complete")) + "\n")
            for _ in range(20):
                handle.write(json.dumps(filler) + "\n")
        self.assertGreater(
            path.stat().st_size - size_after_direction_1, session_hub._CODEX_TAIL_MAX_BYTES
        )
        finished = session_hub._codex_tail_turn_state(path)
        self.assertEqual(finished[0], "task_complete")

    def test_codex_tail_reconstructs_a_line_torn_across_two_reads(self):
        """A write landing between another process's stat()/read() calls
        (most likely for the >1MB single tool-output-blob case) can leave
        the file's exact byte count mid-line. Resuming the NEXT scan from
        that raw byte count would read only the tail half of that line,
        fail to parse it, and silently lose whatever marker it was - the
        scan must instead resume from before the torn line so the full
        record is read intact once the write completes."""
        started_json = json.dumps(self._event(time.time(), "task_started"))
        complete_line = json.dumps(self._event(time.time() - 10, "task_complete")) + "\n"
        path = self.temp / "torn.jsonl"
        path.write_text(complete_line + started_json[:20], encoding="utf-8")  # no trailing \n

        first = session_hub._codex_tail_turn_state(path)
        self.assertEqual(first[0], "task_complete")  # torn line ignored, not crashed

        with path.open("a", encoding="utf-8") as handle:
            handle.write(started_json[20:] + "\n")  # completes the torn line

        second = session_hub._codex_tail_turn_state(path)
        self.assertEqual(second[0], "task_started")

    class _ReadSizeSpyHandle:
        """Wraps a real file handle so every read() call's requested size
        is recorded, proving the round-2 rework's bounded-read claim
        without needing to guess at internal chunk boundaries."""

        def __init__(self, real, sizes: list[int]):
            self._real = real
            self._sizes = sizes

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._real.close()
            return False

        def seek(self, *args, **kwargs):
            return self._real.seek(*args, **kwargs)

        def read(self, size=-1):
            self._sizes.append(size)
            return self._real.read(size)

    def _spy_on_path_reads(self, sizes: list[int]):
        real_open = Path.open

        def spy_open(path_self, *args, **kwargs):
            return self._ReadSizeSpyHandle(real_open(path_self, *args, **kwargs), sizes)

        return patch.object(Path, "open", spy_open)

    def test_codex_scan_reads_stay_chunk_bounded_and_reconstruct_a_straddled_marker(self):
        """Round-2 rework proof: the scan's read size must never scale with
        the range being searched - only the fixed chunk constant, however
        deep a marker sits. A JSONL record longer than one chunk (as this
        marker deliberately is) is mathematically guaranteed to straddle at
        least one backward chunk boundary, proving cross-chunk
        reconstruction; a long unparseable non-marker line closer to EOF
        must be walked past, not mistaken for a marker or crash the scan."""
        chunk = 40
        with patch.object(session_hub, "_CODEX_TAIL_BYTES", chunk):
            marker_line = json.dumps(self._event(time.time(), "task_started"))
            self.assertGreater(len(marker_line), chunk)  # guarantees straddling
            long_non_marker = "n" * (chunk * 3)  # spans several chunks, not JSON
            content = "z" * 7 + "\n" + marker_line + "\n" + long_non_marker + "\n"
            path = self.temp / "straddle.jsonl"
            path.write_text(content, encoding="utf-8")

            read_sizes: list[int] = []
            with self._spy_on_path_reads(read_sizes):
                result, resume_from = session_hub._codex_scan_range_for_turn_marker(
                    path, 0, path.stat().st_size
                )

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "task_started")
        self.assertGreater(len(read_sizes), 3)  # actually walked back several chunks
        for size in read_sizes:
            self.assertLessEqual(size, chunk)
        self.assertEqual(resume_from, path.stat().st_size)

    def test_codex_delta_scan_reads_stay_chunk_bounded_on_a_huge_appended_burst(self):
        """Same bounded-read proof against the real _CODEX_TAIL_BYTES and a
        >old-ceiling delta burst (the exact shape of the reported bug and
        of the round-1 rework's own fixture) - every read the delta path
        issues must still be <= the chunk constant."""
        path = self._rollout([self._event(time.time() - 100, "task_started")])
        first = session_hub._codex_tail_turn_state(path)
        self.assertEqual(first[0], "task_started")

        filler = {
            "timestamp": _iso(time.time()),
            "type": "event_msg",
            "payload": {"type": "item_completed", "blob": "x" * 500_000},
        }
        with path.open("a", encoding="utf-8") as handle:
            for _ in range(20):  # >= 10MB, past the old ceiling
                handle.write(json.dumps(filler) + "\n")
        self.assertGreater(path.stat().st_size, session_hub._CODEX_TAIL_MAX_BYTES)

        read_sizes: list[int] = []
        with self._spy_on_path_reads(read_sizes):
            grown = session_hub._codex_tail_turn_state(path)

        self.assertEqual(grown[0], "task_started")
        self.assertGreater(len(read_sizes), 1)
        for size in read_sizes:
            self.assertLessEqual(size, session_hub._CODEX_TAIL_BYTES)

    def test_codex_scan_skips_an_oversized_non_marker_line_without_unbounded_carry(self):
        """Round-3 rework: markers are tiny fixed-shape records, so a
        candidate line longer than _CODEX_MAX_MARKER_LINE_BYTES can never
        be one - the carried tail_fragment must be dropped once it crosses
        that bound (`oversized` mode), never grown further by re-reading
        and re-concatenating a giant line's remaining bytes. Proven
        black-box: no line ever handed to the parser exceeds the bound,
        however large the underlying record actually is, and a normal
        marker further back (itself straddling a chunk boundary) is still
        found."""
        chunk = 64
        max_marker = 512
        with patch.object(session_hub, "_CODEX_TAIL_BYTES", chunk), patch.object(
            session_hub, "_CODEX_MAX_MARKER_LINE_BYTES", max_marker
        ):
            marker_line = json.dumps(self._event(time.time(), "task_started"))
            self.assertGreater(len(marker_line), chunk)  # guarantees straddling
            self.assertLessEqual(len(marker_line), max_marker)  # still recognizable
            huge_non_marker = "n" * (chunk * 400)  # far past max_marker
            content = "z" * 5 + "\n" + marker_line + "\n" + huge_non_marker + "\n"
            path = self.temp / "oversized.jsonl"
            path.write_text(content, encoding="utf-8")

            seen_lengths: list[int] = []
            real_parse = session_hub._codex_parse_turn_marker_line

            def spy_parse(line):
                seen_lengths.append(len(line))
                return real_parse(line)

            read_sizes: list[int] = []
            with self._spy_on_path_reads(read_sizes), patch.object(
                session_hub, "_codex_parse_turn_marker_line", side_effect=spy_parse
            ):
                result, resume_from = session_hub._codex_scan_range_for_turn_marker(
                    path, 0, path.stat().st_size
                )

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "task_started")
        for size in read_sizes:
            self.assertLessEqual(size, chunk)
        self.assertGreater(len(seen_lengths), 0)
        for length in seen_lengths:
            self.assertLessEqual(length, max_marker)
        self.assertEqual(resume_from, path.stat().st_size)

    def test_codex_parse_turn_marker_line_survives_non_dict_json(self):
        """json.loads validly returns a list/str/int/bool/None for a
        malformed or unrelated line, not just a dict - record.get() used
        to crash on any of those instead of just reporting no marker."""
        for bad in (b"[1, 2, 3]", b'"just a string"', b"null", b"42", b"true"):
            self.assertIsNone(session_hub._codex_parse_turn_marker_line(bad))

    def test_codex_parse_turn_marker_line_survives_non_dict_payload(self):
        """A genuine event_msg record whose "payload" key holds a non-dict
        value (a shape this code doesn't otherwise care about) used to
        crash on `.get("type")` against that non-dict value."""
        for bad_payload in (["task_started"], "task_started", None, 7):
            line = json.dumps(
                {"timestamp": _iso(time.time()), "type": "event_msg", "payload": bad_payload}
            ).encode("utf-8")
            self.assertIsNone(session_hub._codex_parse_turn_marker_line(line))

    def test_codex_tail_cache_evicts_oldest_path_once_over_the_bound(self):
        """codex_sessions() lists every thread ever recorded (no LIMIT), so a
        long-running GUI can be asked about far more distinct rollout paths
        than are ever concurrently relevant. The cache must stay bounded by
        LRU eviction, not grow with total historical path count."""
        session_hub._codex_tail_cache.clear()
        cap = session_hub._CODEX_TAIL_CACHE_MAX
        paths = [
            self._rollout([self._event(time.time(), "task_started")])
            for _ in range(cap + 1)
        ]
        for path in paths:
            session_hub._codex_tail_turn_state(path)
        self.assertEqual(len(session_hub._codex_tail_cache), cap)
        self.assertNotIn(str(paths[0]), session_hub._codex_tail_cache)
        self.assertIn(str(paths[-1]), session_hub._codex_tail_cache)

    def test_codex_tail_on_missing_file_reads_unknown_not_a_crash(self):
        session = session_hub.Session(
            "Codex", "id-missing", "t", "/tmp", "/tmp", 0, Path("/tmp/does-not-exist.jsonl")
        )
        with patch.object(session_hub, "tmux_session_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "unknown")

    # --- group session ids never surface a real activity state ----------
    def test_group_pseudo_session_activity_is_always_unknown(self):
        # No hook ever writes a status file keyed by a "group:..." pseudo id
        # (real hooks only ever see a provider's own native session/thread
        # id) - session_activity must short-circuit before even trying.
        session_id = "group:/tmp/vamp"
        session = session_hub.Session(
            "Claude", session_id, "g", "/tmp/vamp", "/tmp/vamp", 0, Path("/tmp/vamp")
        )
        with patch.object(session_hub, "session_is_tracked_alive", return_value=True):
            state, _ = session_hub.session_activity(session, tmux_enabled=True, tmux_name="x")
        self.assertEqual(state, "unknown")

    # --- GUI, TUI (via JSON) and --sessions-json/CLI agree exactly ------
    def test_gui_running_tab_and_sessions_json_report_the_same_label(self):
        session = session_hub.Session(
            "Claude", "id-shared", "shared", "/tmp/vamp", "/tmp/vamp", 100,
            Path("/tmp/shared.jsonl"),
        )
        session_hub.write_session_status("id-shared", "needs_input", "approve the plan?")
        row = {"name": "VAMP-shared", "provider": "Claude", "session_key": "Claude:id-shared"}
        session_hub.METADATA_PATH.write_text(
            json.dumps({
                "settings": {},
                "sessions": {},
                "groups": {"/tmp/vamp": {"tmux": True, "rows": [row]}},
            }),
            encoding="utf-8",
        )
        with (
            patch.object(session_hub, "claude_sessions", return_value=[session]),
            patch.object(session_hub, "codex_sessions", return_value=[]),
            patch.object(session_hub, "antigravity_sessions", return_value=[]),
            patch.object(session_hub, "tmux_session_alive", return_value=True),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                session_hub.sessions_json_cli()
            payload = json.loads(out.getvalue())
            json_label = payload["groups"]["/tmp/vamp"]["rows"][0]["activity_label"]

            with patch.object(session_hub.QApplication, "platformName", return_value="xcb"):
                window = session_hub.SessionHub()
                window.refresh_running_tab()
                gui_label = window.running_table.item(0, 3).text()
                window.close()

        self.assertEqual(json_label, "Needs input")
        self.assertEqual(gui_label, "Needs input")

    def test_activity_item_activation_focuses_the_exact_live_session(self):
        # task-2135: mounts the REAL Recent-activity widget through the real
        # refresh_running_tab()/_refresh_activity_list() pipeline (not a
        # hand-built dict), then activates each item and asserts the exact
        # (cwd, name, session_id) passed to the shared Running focus
        # authority. Covers: a live exact-identity Claude event, a second
        # Claude row sharing the SAME row name in a different cwd (duplicate
        # names must never conflate), a repaired Codex link row (its
        # session_key already points at the current active member, as
        # row433's persistence guarantees), and a stale/historical entry
        # whose session_id belongs to no currently-running row.
        claude_a = session_hub.Session(
            "Claude", "id-a", "t", "/tmp/vamp2135a", "/tmp/vamp2135a", 100,
            Path("/tmp/a.jsonl"), agent_name="vamp-shared",
        )
        claude_b = session_hub.Session(
            "Claude", "id-b", "t", "/tmp/vamp2135b", "/tmp/vamp2135b", 100,
            Path("/tmp/b.jsonl"), agent_name="vamp-shared",
        )
        codex_new = session_hub.Session(
            "Codex", "new-id", "t", "/tmp/vamp2135c", "/tmp/vamp2135c", 100,
            Path("/tmp/c.jsonl"),
        )
        metadata = {
            "settings": {},
            "sessions": {},
            "groups": {
                "/tmp/vamp2135a": {"tmux": True, "rows": [{"name": "vamp-shared"}]},
                "/tmp/vamp2135b": {"tmux": True, "rows": [{"name": "vamp-shared"}]},
                "/tmp/vamp2135c": {
                    "tmux": True,
                    "rows": [{
                        "name": "vamp-codex", "provider": "Codex",
                        "session_key": "Codex:new-id",
                    }],
                },
            },
        }
        statuses = [
            ("id-a", {"state": "working", "ts": 1000, "detail": "a"}),
            ("id-b", {"state": "working", "ts": 1000, "detail": "b"}),
            ("new-id", {"state": "idle", "ts": 1000, "detail": ""}),
            ("stale-ghost", {"state": "done", "ts": 1000, "detail": "long finished"}),
        ]
        with (
            patch.object(session_hub, "read_metadata", return_value=metadata),
            patch.object(session_hub, "claude_sessions", return_value=[claude_a, claude_b]),
            patch.object(session_hub, "codex_sessions", return_value=[codex_new]),
            patch.object(session_hub, "antigravity_sessions", return_value=[]),
            patch.object(session_hub, "tmux_session_alive", return_value=True),
            patch.object(session_hub, "all_session_statuses", return_value=statuses),
        ):
            window = session_hub.SessionHub()
            try:
                window.refresh_running_tab()
                self.assertEqual(window.activity_list.count(), 4)
                with patch.object(window, "_focus_or_resume_session") as focus_mock:
                    window.activate_activity_item(window.activity_list.item(0))
                    focus_mock.assert_called_once_with(
                        "/tmp/vamp2135a", "vamp-shared", "id-a")

                    focus_mock.reset_mock()
                    window.activate_activity_item(window.activity_list.item(1))
                    focus_mock.assert_called_once_with(
                        "/tmp/vamp2135b", "vamp-shared", "id-b")

                    focus_mock.reset_mock()
                    window.activate_activity_item(window.activity_list.item(2))
                    focus_mock.assert_called_once_with(
                        "/tmp/vamp2135c", "vamp-codex", "new-id")

                    focus_mock.reset_mock()
                    window.activate_activity_item(window.activity_list.item(3))
                    focus_mock.assert_not_called()
            finally:
                window.close()

    def test_running_context_menu_bring_up_window_resolves_exact_duplicate_name_row(self):
        # task-2137: right-click a Running row with a name shared by another
        # row in a different cwd - "Bring up window" must resolve the exact
        # row clicked, the same identity Running's own double-click uses,
        # never a different same-name sibling.
        claude_a = session_hub.Session(
            "Claude", "id-a", "t", "/tmp/vamp2137a", "/tmp/vamp2137a", 100,
            Path("/tmp/2137a.jsonl"), agent_name="vamp-shared",
        )
        claude_b = session_hub.Session(
            "Claude", "id-b", "t", "/tmp/vamp2137b", "/tmp/vamp2137b", 100,
            Path("/tmp/2137b.jsonl"), agent_name="vamp-shared",
        )
        metadata = {
            "settings": {}, "sessions": {},
            "groups": {
                "/tmp/vamp2137a": {"tmux": True, "rows": [{"name": "vamp-shared"}]},
                "/tmp/vamp2137b": {"tmux": True, "rows": [{"name": "vamp-shared"}]},
            },
        }
        with (
            patch.object(session_hub, "read_metadata", return_value=metadata),
            patch.object(session_hub, "claude_sessions", return_value=[claude_a, claude_b]),
            patch.object(session_hub, "codex_sessions", return_value=[]),
            patch.object(session_hub, "antigravity_sessions", return_value=[]),
            patch.object(session_hub, "tmux_session_alive", return_value=True),
        ):
            window = session_hub.SessionHub()
            try:
                window.refresh_running_tab()
                self.assertEqual(window.running_table.rowCount(), 2)
                for row, (cwd, session_id) in enumerate(
                    [("/tmp/vamp2137a", "id-a"), ("/tmp/vamp2137b", "id-b")]
                ):
                    point = window.running_table.visualItemRect(
                        window.running_table.item(row, 0)
                    ).center()
                    with patch.object(session_hub, "QMenu") as menu_cls:
                        menu_instance = MagicMock()
                        menu_cls.return_value = menu_instance
                        window.running_context_menu(point)
                    added = [call.args[0] for call in menu_instance.addAction.call_args_list]
                    self.assertEqual(
                        [action.text() for action in added],
                        ["Bring up window", "Stop session"],
                    )
                    with patch.object(window, "_focus_or_resume_session") as focus_mock:
                        added[0].trigger()
                        focus_mock.assert_called_once_with(cwd, "vamp-shared", session_id)
            finally:
                window.close()

    def test_running_context_menu_stop_session_confirms_and_stops_exact_row(self):
        claude_a = session_hub.Session(
            "Claude", "id-a", "t", "/tmp/vamp2137stop", "/tmp/vamp2137stop", 100,
            Path("/tmp/2137stop.jsonl"),
        )
        metadata = {
            "settings": {}, "sessions": {},
            "groups": {"/tmp/vamp2137stop": {"tmux": True, "rows": [{"name": "vamp-stop-me"}]}},
        }
        with (
            patch.object(session_hub, "read_metadata", return_value=metadata),
            patch.object(session_hub, "claude_sessions", return_value=[claude_a]),
            patch.object(session_hub, "codex_sessions", return_value=[]),
            patch.object(session_hub, "antigravity_sessions", return_value=[]),
            patch.object(session_hub, "tmux_session_alive", return_value=True),
        ):
            window = session_hub.SessionHub()
            try:
                window.refresh_running_tab()
                point = window.running_table.visualItemRect(
                    window.running_table.item(0, 0)
                ).center()
                with patch.object(session_hub, "QMenu") as menu_cls:
                    menu_instance = MagicMock()
                    menu_cls.return_value = menu_instance
                    window.running_context_menu(point)
                added = [call.args[0] for call in menu_instance.addAction.call_args_list]
                with (
                    patch.object(
                        session_hub.QMessageBox, "question",
                        return_value=session_hub.QMessageBox.StandardButton.Yes,
                    ) as confirm,
                    patch.object(session_hub, "stop_tmux_session") as stop_mock,
                    patch.object(session_hub.SessionHub, "refresh_running_tab") as refresh_mock,
                ):
                    added[1].trigger()
                confirm.assert_called_once()
                stop_mock.assert_called_once_with("vamp-stop-me")
                refresh_mock.assert_called_once()
            finally:
                window.close()

    def test_running_context_menu_empty_area_offers_no_menu(self):
        # Negative control for the two tests above: right-clicking where no
        # row exists must not construct a menu or touch focus/stop at all.
        metadata = {"settings": {}, "sessions": {}, "groups": {}}
        with patch.object(session_hub, "read_metadata", return_value=metadata):
            window = session_hub.SessionHub()
        try:
            window.refresh_running_tab()
            self.assertEqual(window.running_table.rowCount(), 0)
            with patch.object(session_hub, "QMenu") as menu_cls:
                window.running_context_menu(QPoint(5, 5))
            menu_cls.assert_not_called()
        finally:
            window.close()

    def test_tmux_live_session_names_survives_oserror_and_timeout(self):
        """The subprocess spawn itself can fail at the OS level (tmux binary
        removed/unexecutable between shutil.which() and the spawn, permission,
        resource limits) - not just time out. Both must fail closed to an
        empty snapshot, the same reading as 'no tmux server running', never
        an uncaught exception that would crash the whole census (row426
        audit rework 2)."""
        with patch.object(session_hub.shutil, "which", return_value="/usr/bin/tmux"):
            with patch.object(
                session_hub.subprocess, "run", side_effect=OSError("tmux vanished")
            ):
                self.assertEqual(session_hub.tmux_live_session_names(), frozenset())
            with patch.object(
                session_hub.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=2),
            ):
                self.assertEqual(session_hub.tmux_live_session_names(), frozenset())
            # Negative control: a real success path still returns the parsed names, so the
            # frozenset() above is provably the exception branches firing, not a bug that
            # always returns empty regardless of what happens.
            with patch.object(
                session_hub.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "VAMP-worker1\nVAMP-worker2\n", ""),
            ):
                self.assertEqual(
                    session_hub.tmux_live_session_names(),
                    frozenset({"VAMP-worker1", "VAMP-worker2"}),
                )

    def test_tmux_session_alive_isolated_call_survives_oserror(self):
        """The isolated (no live_names snapshot) path spawns its own `tmux
        has-session` and had only caught TimeoutExpired - tmux_live_session_names
        already fails closed on OSError but this sibling spawn did not (row426
        audit rework: 'isolated tmux_session_alive() must catch OSError too')."""
        with patch.object(session_hub.shutil, "which", return_value="/usr/bin/tmux"):
            with patch.object(
                session_hub.subprocess, "run", side_effect=OSError("tmux vanished")
            ):
                self.assertFalse(session_hub.tmux_session_alive("VAMP-x"))
            with patch.object(
                session_hub.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=2),
            ):
                self.assertFalse(session_hub.tmux_session_alive("VAMP-x"))
            # Negative control: a real success path still returns True.
            with patch.object(
                session_hub.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0),
            ):
                self.assertTrue(session_hub.tmux_session_alive("VAMP-x"))

    def test_refresh_running_tab_makes_one_tmux_subprocess_call_for_n_rows(self):
        """group_row_status and session_activity used to each call
        tmux_session_alive independently - 2 subprocess spawns per row, times
        N rows. refresh_running_tab must take one tmux_live_session_names()
        snapshot per refresh and every row reuses it (see the `live_names`
        param threaded through group_row_status/session_activity)."""
        rows = [
            {"name": f"VAMP-{i}", "provider": "Claude", "session_key": f"Claude:id-{i}"}
            for i in range(5)
        ]
        session_hub.METADATA_PATH.write_text(
            json.dumps({
                "settings": {},
                "sessions": {},
                "groups": {"/tmp/vamp": {"tmux": True, "rows": rows}},
            }),
            encoding="utf-8",
        )
        sessions = [
            session_hub.Session(
                "Claude", f"id-{i}", f"s{i}", "/tmp/vamp", "/tmp/vamp", 100,
                Path(f"/tmp/s{i}.jsonl"),
            )
            for i in range(5)
        ]

        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            result = MagicMock()
            result.returncode = 0
            if argv[1] == "list-sessions":
                result.stdout = "\n".join(row["name"] for row in rows) + "\n"
            return result

        with (
            patch.object(session_hub, "claude_sessions", return_value=sessions),
            patch.object(session_hub, "codex_sessions", return_value=[]),
            patch.object(session_hub, "antigravity_sessions", return_value=[]),
            patch.object(session_hub.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(session_hub.subprocess, "run", side_effect=fake_run),
            patch.object(session_hub.QApplication, "platformName", return_value="xcb"),
        ):
            # SessionHub() itself does a full refresh() at construction (both
            # the All Sessions table and the Running tab, each its own
            # logical refresh with its own snapshot) - isolate exactly ONE
            # refresh_running_tab() call to prove ITS subprocess count is
            # O(1) in row count, not O(N).
            window = session_hub.SessionHub()
            calls.clear()
            window.refresh_running_tab()
            self.assertEqual(window.running_table.rowCount(), 5)
            window.close()

        tmux_calls = [c for c in calls if c and c[0] == "/usr/bin/tmux"]
        self.assertEqual(len(tmux_calls), 1)
        self.assertEqual(tmux_calls[0][1], "list-sessions")


if __name__ == "__main__":
    unittest.main()
