"""Direct model/column tests for the TUI panes.

RunningPane must keep Status/Last message (status_pipeline_plan.md's
contract #5 - a provider-neutral activity verdict on every live-session
surface). MainPane (All Sessions) does not: task-2114 added Status/Last
message there too without being asked, corrupting the pane's original
four-column layout, and task-2136 reverts that scope expansion. These mount
the real panes in a minimal Textual test App and assert the rendered
columns/cells rather than just the data dicts, so a column silently added or
dropped from add_columns/add_row would fail here even if the underlying
JSON were right.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from textual.app import App, ComposeResult
from textual.widgets import TabbedContent

import session_hub_tui


class _PaneHost(App):
    def __init__(self, pane) -> None:
        super().__init__()
        self._pane = pane

    def compose(self) -> ComposeResult:
        yield self._pane


def _column_labels(table) -> list[str]:
    return [str(column.label) for column in table.columns.values()]


class MainPaneColumnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_pane_restored_to_original_four_columns_no_status(self):
        fake_data = {
            "sessions": [
                {
                    "is_group": False,
                    "provider": "Codex",
                    "title": "demo",
                    "cwd": "/tmp/demo",
                    "session_id": "id-1",
                    "key": "Codex:id-1",
                    "activity_label": "Working",
                    "activity_detail": "writing   the fix now",
                    "tmux": True,
                    "tmux_name": "demo",
                    "status": "Running",
                }
            ],
            "groups": {},
        }
        pane = session_hub_tui.MainPane()
        app = _PaneHost(pane)
        async with app.run_test() as pilot:
            await pilot.pause()
            pane.apply_sessions(fake_data)
            await pilot.pause()
            table = pane.query_one("#main")
            labels = _column_labels(table)
            self.assertEqual(labels, ["Provider", "Name", "Working directory", "Session ID"])
            self.assertNotIn("Status", labels)
            self.assertNotIn("Last message", labels)
            row = table.get_row_at(0)
            self.assertNotIn("Working", row)
            self.assertNotIn("writing the fix now", row)
            self.assertIn("demo", row)
            self.assertIn("/tmp/demo", row)
            self.assertIn("id-1", row)


class RunningPaneColumnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_pane_has_status_and_last_message_columns(self):
        fake_data = {
            "sessions": [
                {
                    "is_group": False,
                    "provider": "Claude",
                    "title": "demo",
                    "key": "Claude:id-2",
                    "tmux_name": "demo-tmux",
                    "status": "Running",
                    "activity_label": "Needs input",
                    "activity_detail": "approve   the plan?",
                }
            ],
            "groups": {},
        }
        pane = session_hub_tui.RunningPane()
        app = _PaneHost(pane)
        async with app.run_test() as pilot:
            await pilot.pause()
            pane.apply_sessions(fake_data)
            await pilot.pause()
            table = pane.query_one("#running")
            labels = _column_labels(table)
            self.assertIn("Status", labels)
            self.assertIn("Last message", labels)
            row = table.get_row_at(0)
            self.assertIn("Needs input", row)
            self.assertIn("approve the plan?", row)


_FAKE_SESSIONS = {
    "sessions": [
        {
            "is_group": False, "provider": "Claude", "title": "demo",
            "cwd": "/tmp/demo", "session_id": "id-1", "key": "Claude:id-1",
            "activity_label": "Idle", "activity_detail": "", "tmux": True,
            "tmux_name": "demo", "status": "Running",
        }
    ],
    "groups": {},
}


class SharedGenerationStartupTests(unittest.IsolatedAsyncioTestCase):
    """task-2142: render shell first, one async fetch feeds both tabs."""

    async def test_shell_renders_before_fetch_completes(self):
        release = threading.Event()

        def blocking_sessions_json():
            release.wait(timeout=5)
            return _FAKE_SESSIONS

        with patch.object(session_hub_tui, "sessions_json", side_effect=blocking_sessions_json):
            app = session_hub_tui.SessionHubTUI()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Shell (columns, an interactive empty table) is already up
                # even though the fetch worker is still blocked.
                main_table = app.query_one(session_hub_tui.MainPane).query_one("#main")
                self.assertEqual(
                    _column_labels(main_table),
                    ["Provider", "Name", "Working directory", "Session ID"],
                )
                self.assertEqual(main_table.row_count, 0)

                release.set()
                for _ in range(20):
                    await pilot.pause()
                    if main_table.row_count:
                        break
                self.assertEqual(main_table.row_count, 1)

    async def test_one_fetch_feeds_both_main_and_running_panes(self):
        with patch.object(
            session_hub_tui, "sessions_json", return_value=_FAKE_SESSIONS
        ) as mock_fetch:
            app = session_hub_tui.SessionHubTUI()
            async with app.run_test() as pilot:
                for _ in range(20):
                    await pilot.pause()
                    main_table = app.query_one(session_hub_tui.MainPane).query_one("#main")
                    if main_table.row_count:
                        break
                self.assertEqual(mock_fetch.call_count, 1)
                running_table = app.query_one(session_hub_tui.RunningPane).query_one("#running")
                self.assertEqual(main_table.row_count, 1)
                self.assertEqual(running_table.row_count, 1)

    async def test_manual_refresh_error_leaves_prior_generation_intact(self):
        with patch.object(
            session_hub_tui, "sessions_json",
            side_effect=[_FAKE_SESSIONS, {"status": "error", "message": "boom"}],
        ):
            app = session_hub_tui.SessionHubTUI()
            async with app.run_test() as pilot:
                main_table = app.query_one(session_hub_tui.MainPane).query_one("#main")
                for _ in range(20):
                    await pilot.pause()
                    if main_table.row_count:
                        break
                self.assertEqual(main_table.row_count, 1)

                notify_mock = app.notify = Mock()
                await app.fetch_sessions().wait()
                # The failed refresh reported an error and did not touch
                # the table that already had the last good generation.
                self.assertEqual(main_table.row_count, 1)
                notify_mock.assert_called_once()
                self.assertIn("boom", notify_mock.call_args.args[0])

    async def test_usage_tab_does_no_work_until_opened(self):
        with patch.object(session_hub_tui, "sessions_json", return_value=_FAKE_SESSIONS), \
             patch.object(session_hub_tui, "usage_json", return_value={}) as mock_usage:
            app = session_hub_tui.SessionHubTUI()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                mock_usage.assert_not_called()

                app.query_one(TabbedContent).active = "usage"
                for _ in range(20):
                    await pilot.pause()
                    if mock_usage.called:
                        break
                mock_usage.assert_called_once()


if __name__ == "__main__":
    unittest.main()
