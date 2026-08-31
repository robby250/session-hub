"""Pure model/adapter controls for the Session Hub TUI panes.

Running is a compact scrolling list above one retained terminal adapter; runtime pilot/PTY tests
remain frozen by row503 and are not run here.
"""
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from textual.app import App, ComposeResult
from textual.widgets import TabbedContent

import session_hub_tui


class TerminalAdapterControlsTests(unittest.TestCase):
    def test_tmux_target_is_exact_and_shell_free(self):
        with patch.object(session_hub_tui.shutil, "which", return_value="/usr/bin/tmux"):
            self.assertEqual(
                session_hub_tui.tmux_attach_argv("worker 5"),
                ["/usr/bin/tmux", "attach", "-t", "=worker 5"],
            )

    def test_adapter_lifecycle_stops_only_terminal_client(self):
        widget = Mock()
        factory = Mock(return_value=widget)
        with patch.object(session_hub_tui.shutil, "which", return_value="/usr/bin/tmux"):
            adapter = session_hub_tui.OssTmuxTerminalAdapter("worker", factory)
        adapter.start()
        adapter.close()
        factory.assert_called_once()
        widget.start.assert_called_once_with()
        widget.stop.assert_called_once_with()


class _FakeWidget:
    def __init__(self):
        self.focuses = 0

    def focus(self):
        self.focuses += 1


class _FakeAdapter:
    instances = []

    def __init__(self, name):
        self.identity = name
        self.widget = _FakeWidget()
        self.starts = 0
        self.closes = 0
        self.switches = []
        self.resizes = []
        self.active = False
        self.__class__.instances.append(self)

    def start(self):
        self.starts += 1
        self.active = True

    def switch(self, name):
        self.switches.append(name)
        self.close()
        self.identity = name
        self.start()

    def close(self):
        if self.active:
            self.closes += 1
        self.active = False

    def resize(self, width, height):
        self.resizes.append((width, height))


class _FakeHost:
    content_size = SimpleNamespace(width=80, height=20)

    def __init__(self):
        self.mounted = []

    async def mount(self, widget):
        self.mounted.append(widget)


class _FakeEmpty:
    def __init__(self):
        self.text = "Select a running session"

    def update(self, text):
        self.text = text


class RunningPaneLifecycleControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _FakeAdapter.instances.clear()
        self.host = _FakeHost()
        self.empty = _FakeEmpty()
        self.pane = session_hub_tui.RunningPane(adapter_factory=_FakeAdapter)
        def query_one(selector, *_types):
            return self.empty if selector == "#terminal-empty" else self.host
        self.pane.query_one = query_one

    @staticmethod
    def row(key, name):
        return {"key": key, "name": name, "tmux_name": name}

    async def test_selection_refresh_reorder_and_switch_retain_one_surface(self):
        first = self.row("Codex:first", "first")
        second = self.row("Codex:second", "second")
        await self.pane._switch_terminal(first)  # tap/Enter exact target
        await self.pane._switch_terminal(first)  # same-key refresh: no reattach
        self.pane.rows = [second, first]  # reorder preserves selected identity
        await self.pane._switch_terminal(first)
        await self.pane._switch_terminal(second)  # switch closes only old client
        self.assertEqual(len(_FakeAdapter.instances), 1)
        adapter = _FakeAdapter.instances[0]
        self.assertEqual(self.host.mounted, [adapter.widget])
        self.assertEqual(adapter.switches, ["second"])
        self.assertEqual(adapter.closes, 1)
        self.assertEqual(self.pane.selected_key, "Codex:second")

    async def test_disappearance_resize_and_app_exit_are_safe_and_idempotent(self):
        row = self.row("Codex:gone", "gone")
        await self.pane._switch_terminal(row)
        self.pane.selected_key = None  # apply_sessions disappearance branch
        self.pane._close_adapter()
        self.assertIsNotNone(self.pane.adapter)
        self.assertEqual(len(self.host.mounted), 1)  # app/surface remains alive
        self.assertEqual(self.empty.text, "Select a running session")
        await self.pane._switch_terminal(self.row("Codex:back", "back"))
        self.assertEqual(self.empty.text, "")
        self.assertEqual(len(_FakeAdapter.instances), 1)  # reactivation reuses the surface/client
        self.assertEqual(self.pane.adapter.switches, ["back"])
        self.pane.on_resize(SimpleNamespace(size=SimpleNamespace(width=1, height=1)))
        self.assertEqual(self.pane.adapter.resizes, [(80, 20)])
        self.pane.on_unmount()
        self.pane.on_unmount()
        self.assertEqual(self.pane.adapter.closes, 1)
        self.assertEqual(self.pane.adapter.switches, [])


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
            self.assertIn("8m ago", text)
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
