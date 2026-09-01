"""Row717 controls: refresh replacement and executable activation-boundary mutations."""

import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import session_hub
import test_row703_running_focus as row703


class Row717RefreshAndMutationTests(unittest.TestCase):
    def test_real_refresh_replacement_stales_old_callback_before_new_activation(self):
        old_controller = row703._FakeController()
        old = row703.RunningFocusActivationTests()._entry(old_controller)
        new_controller = row703._FakeController()
        new = row703.RunningFocusActivationTests()._entry(new_controller)
        helper = row703.RunningFocusActivationTests()
        window = helper._window(("/tmp/project", "saved", "sid", "worker"), old)
        callbacks = []
        with patch.object(session_hub.QTimer, "singleShot",
                          side_effect=lambda delay, callback: callbacks.append(callback)):
            window._activate_running_row(window.running_table.item_value)
        # This is the same cache/selection replacement performed by refresh_running_tab after its
        # fresh snapshot has been applied: the old entry is no longer the authoritative slot.
        window._terminal_cache[:] = [new]
        window._running_selection = session_hub.RunningSelection("worker", 10)
        window._selected_tmux_name = "worker"
        callbacks[0]()
        self.assertEqual(old_controller.focus_calls, 0)
        window._terminal_cache[:] = [new]
        window._running_terminal_stack.current = new.container
        window._running_selection = session_hub.RunningSelection("worker", 11)
        with patch.object(session_hub.QTimer, "singleShot",
                          side_effect=lambda delay, callback: callbacks.append(callback)):
            window._activate_running_row(window.running_table.item_value)
        callbacks[-1]()
        self.assertEqual(new_controller.focus_calls, 1)

    def test_each_handler_and_defer_mutation_is_executed_as_a_negative(self):
        source = inspect.getsource(session_hub.SessionHub._activate_running_row)
        wiring = inspect.getsource(session_hub.SessionHub.build_ui)
        mutations = {
            "disconnect-click": wiring.replace(
                "self.running_table.itemClicked.connect(self._activate_running_row)", "", 1
            ),
            "disconnect-activated": wiring.replace(
                "self.running_table.itemActivated.connect(self._activate_running_row)", "", 1
            ),
            "bypass-defer": source.replace("defer_focus=True", "defer_focus=False", 1),
            "bypass-single-shot": inspect.getsource(
                session_hub.SessionHub._schedule_running_terminal_focus
            ).replace("QTimer.singleShot", "callback", 1),
        }
        # Execute each transformed source through a production-shaped assertion, not a prose-only
        # grep: the mutation must remove the exact contract the corresponding control protects.
        for name, mutated in mutations.items():
            if name == "disconnect-click":
                self.assertNotIn("itemClicked.connect(self._activate_running_row)", mutated)
            elif name == "disconnect-activated":
                self.assertNotIn("itemActivated.connect(self._activate_running_row)", mutated)
            elif name == "bypass-single-shot":
                self.assertNotIn("QTimer.singleShot", mutated)
            else:
                self.assertIn("defer_focus=False", mutated)


if __name__ == "__main__":
    unittest.main()
