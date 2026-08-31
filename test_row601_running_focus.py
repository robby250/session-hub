"""Pure task-2238 controls for deferred Running-terminal focus.

These tests model the event-loop callback with a captured callable.  They never construct the
desktop Session Hub, start Qt, touch tmux, or launch a terminal.
"""

import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import session_hub


class _FakeController:
    def __init__(self, generation=4, alive=True, focus_result=True):
        self.generation = generation
        self.alive = alive
        self.focus_result = focus_result
        self.focus_calls = 0

    def poll_alive(self):
        return self.alive

    def focus(self):
        self.focus_calls += 1
        return self.focus_result


class RunningFocusDeferredTests(unittest.TestCase):
    def _window(self, entry):
        window = session_hub.SessionHub.__new__(session_hub.SessionHub)
        window._terminal_cache = [entry]
        window._selected_tmux_name = "worker"
        window._running_selection = session_hub.RunningSelection("worker", 9)
        window._qt_interaction_serial = 12
        window._focused_entry = None
        window._embed_focus_grab_serial = None
        window._note_embed_focus_grabbed = lambda serial: setattr(
            window, "_embed_focus_grab_serial", serial
        )
        return window

    def _entry(self, **overrides):
        values = {
            "tmux_name": "worker",
            "state": "ready",
            "controller": _FakeController(),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_activation_defers_one_focus_and_revalidates_exact_entry(self):
        entry = self._entry()
        window = self._window(entry)
        callbacks = []
        with patch.object(
            session_hub.QTimer, "singleShot",
            side_effect=lambda delay, callback: callbacks.append((delay, callback)),
        ):
            window._schedule_running_terminal_focus(entry, 9, 12)
        self.assertEqual(entry.controller.focus_calls, 0)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 0)
        callbacks[0][1]()
        self.assertEqual(entry.controller.focus_calls, 1)
        self.assertIs(window._focused_entry, entry)

        # A newer exact selection makes the captured callback inert rather than letting an old
        # attach steal focus after the user has selected another Running row.
        window._running_selection = session_hub.RunningSelection("other", 10)
        callbacks[0][1]()
        self.assertEqual(entry.controller.focus_calls, 1)

    def test_stale_generation_dead_and_preparing_entries_fail_closed(self):
        for mutation in (
            lambda w, e: setattr(w, "_qt_interaction_serial", 13),
            lambda w, e: setattr(w, "_running_selection", session_hub.RunningSelection("worker", 10)),
            lambda w, e: setattr(e.controller, "alive", False),
            lambda w, e: setattr(e, "state", "preparing"),
            lambda w, e: setattr(e.controller, "generation", 5),
        ):
            entry = self._entry()
            window = self._window(entry)
            callbacks = []
            with patch.object(session_hub.QTimer, "singleShot",
                              side_effect=lambda delay, callback: callbacks.append(callback)):
                window._schedule_running_terminal_focus(entry, 9, 12)
            mutation(window, entry)
            callbacks[0]()
            self.assertEqual(entry.controller.focus_calls, 0)

    def test_focus_failure_evicts_and_falls_back_for_promotion_and_reselect(self):
        class _Stack:
            def __init__(self, current):
                self.current = current
                self.set_calls = []

            def currentWidget(self):
                return self.current

            def setCurrentWidget(self, widget):
                self.set_calls.append(widget)
                self.current = widget

        for path in ("promotion", "reselect"):
            entry = self._entry(
                controller=_FakeController(focus_result=False),
                meta=("/tmp/project", "session-id", "saved-name"),
                last_used=0.0,
                container=object(),
                paint_verified=True,
            )
            window = self._window(entry)
            window._running_terminal_stack = _Stack(entry.container)
            window.running_terminal_placeholder = object()
            evicted = []
            failures = []
            window._evict_entry = lambda candidate: evicted.append(candidate)
            window._show_embed_failure = lambda *args, **kwargs: failures.append((args, kwargs))
            callbacks = []
            with patch.object(session_hub.QTimer, "singleShot",
                              side_effect=lambda delay, callback: callbacks.append(callback)):
                if path == "promotion":
                    window._promote_entry(entry, 12, defer_focus=True)
                else:
                    window._select_running_terminal(
                        "/tmp/project", "worker", "session-id", saved_name="saved-name",
                        defer_focus=True,
                    )
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            self.assertEqual(entry.controller.focus_calls, 1)
            self.assertEqual(evicted, [entry])
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0][0][1], "worker")

    def test_click_enter_and_double_click_share_deferred_activation_boundary(self):
        setup = inspect.getsource(session_hub.SessionHub.build_ui)
        activate = inspect.getsource(session_hub.SessionHub._activate_running_row)
        self.assertIn("self.running_table.itemClicked.connect(self._activate_running_row)", setup)
        self.assertIn("self.running_table.itemActivated.connect(self._activate_running_row)", setup)
        self.assertIn("defer_focus=True", activate)
        self.assertIn("QTimer.singleShot", inspect.getsource(
            session_hub.SessionHub._schedule_running_terminal_focus
        ))


if __name__ == "__main__":
    unittest.main()
