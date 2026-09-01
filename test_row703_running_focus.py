"""Pure row703 regression controls for the deferred Running-terminal focus boundary."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import session_hub


class _Controller:
    def __init__(self):
        self.generation = 7
        self.alive = True
        self.focus_calls = 0

    def poll_alive(self):
        return self.alive

    def focus(self):
        self.focus_calls += 1
        return True


class RunningFocusIdentityTests(unittest.TestCase):
    def test_deferred_focus_accepts_only_the_exact_selection_and_generation(self):
        controller = _Controller()
        entry = SimpleNamespace(tmux_name="worker", state="ready", controller=controller)
        window = session_hub.SessionHub.__new__(session_hub.SessionHub)
        window._terminal_cache = [entry]
        window._selected_tmux_name = "worker"
        window._running_selection = session_hub.RunningSelection("worker", 11)
        window._qt_interaction_serial = 19
        window._focused_entry = None
        callbacks = []
        with patch.object(session_hub.QTimer, "singleShot",
                          side_effect=lambda delay, callback: callbacks.append(callback)):
            window._schedule_running_terminal_focus(entry, 11, 19)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(controller.focus_calls, 1)
        window._running_selection = session_hub.RunningSelection("other", 12)
        callbacks[0]()
        self.assertEqual(controller.focus_calls, 1)

    def test_deferred_focus_fails_closed_for_dead_or_replaced_generation(self):
        controller = _Controller()
        entry = SimpleNamespace(tmux_name="worker", state="ready", controller=controller)
        window = session_hub.SessionHub.__new__(session_hub.SessionHub)
        window._terminal_cache = [entry]
        window._selected_tmux_name = "worker"
        window._running_selection = session_hub.RunningSelection("worker", 11)
        window._qt_interaction_serial = 19
        window._focused_entry = None
        callbacks = []
        with patch.object(session_hub.QTimer, "singleShot",
                          side_effect=lambda delay, callback: callbacks.append(callback)):
            window._schedule_running_terminal_focus(entry, 11, 19)
        controller.alive = False
        callbacks[0]()
        self.assertEqual(controller.focus_calls, 0)


if __name__ == "__main__":
    unittest.main()
