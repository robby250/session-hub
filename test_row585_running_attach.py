"""Pure row585 rollback control: selecting a live Running row must exit the TUI with
the row's exact discovered tmux target (group rows use their discovered tmux_name, not
necessarily the saved row name) and fail closed -- notify, never exit -- when no exact
tmux name exists. Also proves no embedded-terminal/adapter/PTY code survives in the
module, and that main() attaches to the exact `=<name>` tmux target, never a bare/
prefix-matched name."""

import inspect
import unittest
from unittest.mock import Mock, PropertyMock, patch

from textual.app import App, ComposeResult

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
import session_hub_tui


class _PaneHost(App):
    def __init__(self, pane) -> None:
        super().__init__()
        self._pane = pane

    def compose(self) -> ComposeResult:
        yield self._pane


class _FakeApp:
    def __init__(self):
        self.exited_with = "unset"

    def exit(self, result=None):
        self.exited_with = result


class RunningSelectionExitsInsteadOfEmbeddingTests(unittest.TestCase):
    def pane_with(self, picked):
        pane = session_hub_tui.RunningPane.__new__(session_hub_tui.RunningPane)
        fake_app = _FakeApp()
        patcher = patch.object(
            session_hub_tui.RunningPane, "app", new_callable=PropertyMock, return_value=fake_app
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        pane.notify = Mock()
        pane.selected = lambda: picked
        return pane, fake_app

    def test_selecting_a_group_row_exits_with_its_discovered_tmux_name(self):
        # apply_sessions sets tmux_name to the row's DISCOVERED live name, which can
        # differ from the saved row["name"] after an external restart/rename.
        picked = {
            "kind": "group", "cwd": "/proj", "name": "saved-name",
            "tmux_name": "discovered-name",
        }
        pane, fake_app = self.pane_with(picked)
        pane.on_list_view_selected(None)
        self.assertEqual(fake_app.exited_with, ("/proj", "discovered-name"))

    def test_selecting_a_standalone_row_exits_with_its_tmux_name(self):
        picked = {"kind": "standalone", "cwd": None, "name": "demo", "tmux_name": "demo"}
        pane, fake_app = self.pane_with(picked)
        pane.on_list_view_selected(None)
        self.assertEqual(fake_app.exited_with, (None, "demo"))

    def test_no_exact_tmux_name_fails_closed_without_exiting(self):
        picked = {"kind": "standalone", "cwd": None, "name": "demo", "tmux_name": ""}
        pane, fake_app = self.pane_with(picked)
        pane.on_list_view_selected(None)
        self.assertEqual(fake_app.exited_with, "unset")
        pane.notify.assert_called_once()

    def test_no_selection_neither_exits_nor_notifies(self):
        pane, fake_app = self.pane_with(None)
        pane.on_list_view_selected(None)
        self.assertEqual(fake_app.exited_with, "unset")
        pane.notify.assert_not_called()

    def test_pre_prefixed_equals_target_fails_closed_without_exiting(self):
        # main() builds f"={name}"; a discovered name already starting with "=" would
        # produce the malformed "==name" tmux target if allowed through.
        picked = {"kind": "standalone", "cwd": None, "name": "demo", "tmux_name": "=worker-session"}
        pane, fake_app = self.pane_with(picked)
        pane.on_list_view_selected(None)
        self.assertEqual(fake_app.exited_with, "unset")
        pane.notify.assert_called_once()

    def test_negative_control_a_switch_only_handler_never_exits(self):
        """Mimics the removed embedded-terminal behavior (retarget local state, never
        end the TUI). If on_list_view_selected regressed to this shape, the positive
        assertions above would fail -- this proves exited_with stays unset under it,
        so those assertions are actually checking exit, not merely "did not crash"."""
        picked = {"kind": "standalone", "cwd": None, "name": "demo", "tmux_name": "demo"}
        pane, fake_app = self.pane_with(picked)

        def switch_only(_event):
            pane.selected_key = pane.selected()["tmux_name"]

        switch_only(None)
        self.assertEqual(fake_app.exited_with, "unset")


class ApplySessionsFailClosedTests(unittest.IsolatedAsyncioTestCase):
    """apply_sessions() itself must produce fail-closed rows for the real data shapes --
    not only the hand-fabricated picked rows above."""

    async def test_group_row_missing_discovered_name_has_no_saved_name_fallback(self):
        data = {
            "groups": {
                "/proj": {
                    "display_name": "/proj",
                    "rows": [{
                        "name": "saved-name", "provider": "Codex", "status": "Running",
                        "key": "k1",
                    }],
                }
            },
            "sessions": [],
        }
        pane = session_hub_tui.RunningPane()
        app = _PaneHost(pane)
        async with app.run_test() as pilot:
            await pilot.pause()
            pane.apply_sessions(data)
            self.assertIsNone(pane.rows[0]["tmux_name"])

    async def test_standalone_row_missing_discovered_name_does_not_raise(self):
        data = {
            "groups": {},
            "sessions": [{
                "is_group": False, "provider": "Codex", "title": "demo",
                "key": "Codex:x", "status": "Running",
            }],
        }
        pane = session_hub_tui.RunningPane()
        app = _PaneHost(pane)
        async with app.run_test() as pilot:
            await pilot.pause()
            pane.apply_sessions(data)  # must not raise KeyError
            self.assertIsNone(pane.rows[0]["tmux_name"])
            self.assertEqual(pane.rows[0]["name"], "demo")


class NoEmbeddedTerminalSurvivesTests(unittest.TestCase):
    def test_no_terminal_adapter_symbols_remain_in_the_module(self):
        removed = (
            "OssTmuxTerminalAdapter", "TerminalAdapter", "tmux_attach_argv",
            "_install_textual_terminal_default_colors_shim",
            "_remove_textual_terminal_default_colors_shim",
            "_OssTerminal", "_textual_terminal_import_error", "TEXTUAL_TERMINAL_INSTALL",
        )
        for name in removed:
            self.assertFalse(hasattr(session_hub_tui, name), f"{name} should be removed")

    def test_running_pane_has_no_terminal_host_widgets_or_lifecycle_methods(self):
        pane_methods = set(vars(session_hub_tui.RunningPane))
        for name in ("_switch_terminal", "_close_adapter", "on_resize", "on_unmount"):
            self.assertNotIn(name, pane_methods)
        source = inspect.getsource(session_hub_tui.RunningPane)
        for needle in ("terminal-host", "terminal-empty", "adapter"):
            self.assertNotIn(needle, source)

    def test_main_attaches_to_the_exact_tmux_target_not_a_prefix(self):
        source = inspect.getsource(session_hub_tui.main).replace("'", '"')
        self.assertIn('"-t", f"={name}"', source)


if __name__ == "__main__":
    unittest.main()
