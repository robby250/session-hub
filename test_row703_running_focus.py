"""Pure task-2238 controls for the real Running-row activation boundary.

These controls never construct the desktop Session Hub, start Qt, touch tmux, or launch a
terminal. They call the production activation method with small table/item doubles and capture
the one event-loop callback it schedules.
"""

import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import session_hub


class _FakeController:
    def __init__(self, generation=4, alive=True):
        self.generation = generation
        self.alive = alive
        self.focus_calls = 0

    def poll_alive(self):
        return self.alive

    def focus(self):
        self.focus_calls += 1
        return True


class _FakeItem:
    def __init__(self, payload, row=0):
        self.payload = payload
        self._row = row

    def row(self):
        return self._row

    def data(self, _role):
        return self.payload


class _FakeTable:
    def __init__(self, item):
        self.item_value = item

    def item(self, row, column):
        return self.item_value if row == 0 and column == 0 else None


class _FakeStack:
    def __init__(self, current):
        self.current = current

    def currentWidget(self):
        return self.current

    def setCurrentWidget(self, widget):
        self.current = widget


class RunningFocusActivationTests(unittest.TestCase):
    def _entry(self, controller=None, **overrides):
        values = {
            "tmux_name": "worker",
            "state": "ready",
            "controller": controller or _FakeController(),
            "container": object(),
            "meta": ("/tmp/project", "session-id", "saved-name"),
            "last_used": 0.0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _window(self, payload, entry):
        window = session_hub.SessionHub.__new__(session_hub.SessionHub)
        window.running_table = _FakeTable(_FakeItem(payload))
        window._terminal_cache = [entry]
        window._running_terminal_stack = _FakeStack(entry.container)
        window._selected_tmux_name = "worker"
        window._running_selection = session_hub.RunningSelection("worker", 9)
        window._qt_interaction_serial = 12
        window._focused_entry = None
        window._embed_focus_grab_serial = None
        window._note_embed_focus_grabbed = lambda serial: setattr(
            window, "_embed_focus_grab_serial", serial
        )
        return window

    def test_click_enter_and_double_click_call_real_activation_and_defer_focus(self):
        controller = _FakeController()
        entry = self._entry(controller)
        payload = ("/tmp/project", "saved-name", "session-id", "worker")
        window = self._window(payload, entry)
        callbacks = []
        with patch.object(
            session_hub.QTimer,
            "singleShot",
            side_effect=lambda delay, callback: callbacks.append((delay, callback)),
        ):
            # itemClicked is the single-click path; itemActivated is Qt's shared Enter and
            # double-click path. All three invoke the real production handler below.
            for _gesture in ("click", "enter", "double-click"):
                window._activate_running_row(window.running_table.item_value)

        self.assertEqual([delay for delay, _callback in callbacks], [0, 0, 0])
        self.assertEqual(controller.focus_calls, 0)
        for _delay, callback in callbacks:
            callback()
        # Each gesture schedules its own callback, but only the final activation remains
        # authoritative after the selection generation advances twice.
        self.assertEqual(controller.focus_calls, 1)

    def test_synchronous_only_focus_is_rejected_until_the_event_loop_callback(self):
        controller = _FakeController()
        entry = self._entry(controller)
        window = self._window(
            ("/tmp/project", "saved-name", "session-id", "worker"), entry
        )
        callbacks = []
        with patch.object(
            session_hub.QTimer,
            "singleShot",
            side_effect=lambda delay, callback: callbacks.append((delay, callback)),
        ):
            window._activate_running_row(window.running_table.item_value)
            # A synchronous-only implementation would already have grabbed focus here.
            self.assertEqual(controller.focus_calls, 0)
        self.assertEqual(len(callbacks), 1)
        callbacks[0][1]()
        self.assertEqual(controller.focus_calls, 1)

    def test_each_independent_authority_mutation_fails_closed(self):
        mutations = (
            lambda window, entry: setattr(entry.controller, "generation", 5),
            lambda window, entry: setattr(entry, "state", "preparing"),
            lambda window, entry: setattr(entry.controller, "alive", False),
            lambda window, entry: setattr(window, "_qt_interaction_serial", 13),
            lambda window, entry: setattr(
                window, "_running_selection", session_hub.RunningSelection("worker", 11)
            ),
            lambda window, entry: window._terminal_cache.clear(),
        )
        for mutate in mutations:
            controller = _FakeController()
            entry = self._entry(controller)
            window = self._window(
                ("/tmp/project", "saved-name", "session-id", "worker"), entry
            )
            callbacks = []
            with patch.object(
                session_hub.QTimer,
                "singleShot",
                side_effect=lambda delay, callback: callbacks.append(callback),
            ):
                window._activate_running_row(window.running_table.item_value)
            mutate(window, entry)
            callbacks[0]()
            self.assertEqual(controller.focus_calls, 0)

    def test_malformed_unsafe_and_missing_identities_never_activate(self):
        previous = session_hub.RunningSelection("worker", 9)
        for identity in (None, "", "worker:name", "worker name", "=worker"):
            entry = self._entry(tmux_name="worker")
            window = self._window(
                ("/tmp/project", "saved-name", "session-id", identity), entry
            )
            window._running_selection = previous
            with patch.object(window, "_select_running_terminal") as select:
                window._activate_running_row(window.running_table.item_value)
            select.assert_not_called()
            self.assertEqual(window._running_selection, previous)

        entry = self._entry()
        window = self._window(None, entry)
        with patch.object(window, "_select_running_terminal") as select:
            window._activate_running_row(window.running_table.item_value)
        select.assert_not_called()

    def test_production_wires_click_and_activation_to_the_same_boundary(self):
        setup = inspect.getsource(session_hub.SessionHub.build_ui)
        activate = inspect.getsource(session_hub.SessionHub._activate_running_row)
        self.assertIn("self.running_table.itemClicked.connect(self._activate_running_row)", setup)
        self.assertIn("self.running_table.itemActivated.connect(self._activate_running_row)", setup)
        self.assertIn("defer_focus=True", activate)
        self.assertIn("valid_tmux_session_identity", activate)
        self.assertIn("QTimer.singleShot", inspect.getsource(
            session_hub.SessionHub._schedule_running_terminal_focus
        ))


if __name__ == "__main__":
    unittest.main()
