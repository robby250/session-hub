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
        )
        self.assertEqual(window.metadata.get("links", {}), original_links)
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

    def test_manual_refresh_restarts_usage_timer(self):
        window = session_hub.SessionHub()
        with (
            patch.object(window.usage_timer, "start") as start,
            patch.object(window, "refresh"),
            patch.object(window, "refresh_usage"),
        ):
            window.refresh_all()
        start.assert_called_once_with()
        window.close()

    def test_new_session_toolbar_uses_selected_provider(self):
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

    def test_new_session_dialog_defaults_to_home(self):
        dialog = session_hub.NewSessionDialog("Codex", {})
        dialog.accept()
        self.assertEqual(dialog.directory, Path.home())
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
        dialog.close()

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
