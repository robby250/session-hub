"""Direct model/column tests for the TUI panes - status_pipeline_plan.md's
contract #5 requires Status AND Last message on every activity-showing
surface; MainPane/RunningPane originally added Status only. These mount the
real panes in a minimal Textual test App and assert the rendered columns/
cells rather than just the data dicts, so a column silently dropped from
add_columns/add_row would fail here even if the underlying JSON were right.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from textual.app import App, ComposeResult

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
    async def test_main_pane_has_status_and_last_message_columns(self):
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
        with patch.object(session_hub_tui, "sessions_json", return_value=fake_data):
            app = _PaneHost(pane)
            async with app.run_test() as pilot:
                await pilot.pause()
                table = pane.query_one("#main")
                labels = _column_labels(table)
                self.assertIn("Status", labels)
                self.assertIn("Last message", labels)
                row = table.get_row_at(0)
                self.assertIn("Working", row)
                self.assertIn("writing the fix now", row)


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
        with patch.object(session_hub_tui, "sessions_json", return_value=fake_data):
            app = _PaneHost(pane)
            async with app.run_test() as pilot:
                await pilot.pause()
                table = pane.query_one("#running")
                labels = _column_labels(table)
                self.assertIn("Status", labels)
                self.assertIn("Last message", labels)
                row = table.get_row_at(0)
                self.assertIn("Needs input", row)
                self.assertIn("approve the plan?", row)


if __name__ == "__main__":
    unittest.main()
