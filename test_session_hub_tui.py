"""Pure model controls for the Session Hub TUI panes.

Running selection exits the TUI to a separate `tmux attach` (see
test_row585_running_attach.py) rather than embedding a terminal; runtime pilot/PTY
tests remain frozen by row503 and are not run here.
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
    async def test_running_pane_has_compact_rows_and_exact_identity(self):
        fake_data = {
            "sessions": [
                {
                    "is_group": False,
                    "provider": "Claude",
                    "title": "demo",
                    "key": "Claude:id-2",
                    "tmux_name": "demo-tmux",
                    "age": "8m ago",
                    "status": "Running",
                    "activity_label": "Needs input",
                    "activity_detail": "status-only text must not render",
                    "assistant_preview": "approve   the plan?",
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
            self.assertEqual(len(pane.rows), 1)
            self.assertEqual(pane.rows[0]["key"], "Claude:id-2")
            self.assertEqual(pane.rows[0]["tmux_name"], "demo-tmux")
            self.assertEqual(pane.rows[0]["detail"], "approve   the plan?")
            text = pane._row_text({**pane.rows[0], "detail": "approve the plan?"})
            self.assertIn("demo-tmux", text)
            self.assertIn("8m", text)
            self.assertNotIn("ago", text)
            self.assertIn("approve the plan?", text)
            self.assertEqual(text.count("\n"), 1)
            self.assertNotIn("status-only text must not render", text)

    async def test_running_preview_is_empty_when_serialized_field_is_missing(self):
        fake_data = {
            "sessions": [{
                "is_group": False, "provider": "Codex", "title": "no-preview",
                "key": "Codex:none", "tmux_name": "no-preview", "age": "now",
                "status": "Running", "activity_label": "Working",
            }], "groups": {},
        }
        pane = RunningPane()
        app = _PaneHost(pane)
        async with app.run_test() as pilot:
            await pilot.pause()
            pane.apply_sessions(fake_data)
            await pilot.pause()
            self.assertEqual(pane.rows[0]["detail"], "")


class RunningCardPureContractTests(unittest.TestCase):
    def row(self, **overrides):
        row = {
            "name": "a-very-long-running-session",
            "provider": "Codex",
            "display": "/projects/vampulse",
            "detail": "a preview that should use all available lower-line width",
        }
        row.update(overrides)
        return row

    def test_phone_target_has_three_cells_but_card_text_has_two_lines(self):
        item = session_hub_tui.RunningPane._render_item(
            session_hub_tui.RunningPane.__new__(session_hub_tui.RunningPane),
            self.row(age="0m"),
        )
        self.assertEqual(session_hub_tui.RUNNING_CARD_HEIGHT, 3)
        self.assertEqual(item.classes, {"running-row"})
        self.assertEqual(item.children[0].classes, {"running-card"})
        self.assertEqual(item.children[0].renderable.count("\n"), 1)

    def test_age_formats_only_compact_units_and_blank_stays_blank(self):
        for raw, expected in (("now", "0m"), ("8m ago", "8m"), ("2h", "2h"), ("3d ago", "3d"), ("", "")):
            self.assertEqual(session_hub_tui.compact_running_age(raw), expected)
        self.assertEqual(session_hub_tui.compact_running_age("just now"), "0m")
        self.assertEqual(session_hub_tui.compact_running_age("tomorrow"), "")

    def test_age_reserves_only_its_actual_width_and_lower_line_uses_full_width(self):
        row = self.row(age="12h")
        narrow = session_hub_tui.running_card_lines(row, 18)
        wide = session_hub_tui.running_card_lines(row, 60)
        self.assertEqual(narrow[0][-3:], " 12h")
        self.assertLess(len(narrow[0]), len(wide[0]))
        self.assertLess(len(narrow[1]), len(wide[1]))
        self.assertIn("Codex", narrow[1])
        self.assertNotIn("ago", "\n".join(wide))

    def test_short_name_right_aligns_age_and_tiny_width_never_overflows(self):
        row = self.row(name="A", age="12h")
        line, _ = session_hub_tui.running_card_lines(row, 18)
        self.assertEqual(len(line), 18)
        self.assertTrue(line.endswith("12h"))
        tiny, _ = session_hub_tui.running_card_lines(row, 2)
        self.assertLessEqual(len(tiny), 2)

    def test_usage_hides_only_spark_quota_windows(self):
        windows = [
            {"name": "Weekly"},
            {"name": "GPT-5.3-Codex-Spark 5-hour"},
            {"name": "GPT-5.3-Codex-Spark Weekly"},
        ]
        self.assertEqual(
            session_hub_tui.UsagePane._visible_windows("Codex", windows),
            [{"name": "Weekly"}],
        )
        self.assertEqual(session_hub_tui.UsagePane._visible_windows("Claude", windows), windows)


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
                # Shell (columns, an interactive empty list) is already up
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
                self.assertEqual(main_table.row_count, 1)
                self.assertEqual(len(app.query_one(session_hub_tui.RunningPane).rows), 1)

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
