"""Pure task-2238 controls for the real Running-row activation/focus boundary (row717).

Replaces reviewer-REWORK candidate 5a6ea0fa283e / patch afc43811092d. These controls never
construct the desktop Session Hub via its own __init__, start a live tmux session, or launch a
terminal -- but the two new proof families below (refresh staleness, handler/defer mutations) DO
execute the real production entry points and, for the wiring controls, a real (offscreen) Qt
QMainWindow built by the unmodified `build_ui` -- never a source-text presence check alone.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
import session_hub
from PyQt6.QtWidgets import QApplication, QMainWindow, QTableWidgetItem


def _app() -> QApplication:
    # A QApplication with no held Python reference can be garbage-collected out from under a
    # live QWidget, surfacing as a "Must construct a QApplication before a QWidget" native error
    # on the NEXT widget construction rather than here -- keep one alive for the whole module.
    global _QAPP
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _QAPP = app
    return app


_QAPP: QApplication | None = None


def _load_mutated_module(mutate):
    """Import a fresh copy of session_hub.py with MUTATE applied to its source text.

    A real, independent module object -- not a patched attribute on the shared `session_hub`
    module -- so every class/function in it (including ones that call each other) sees the
    mutated body, exactly like a real deployed change would.
    """
    source = open(session_hub.__file__).read()
    mutated_source = mutate(source)
    assert mutated_source != source, "mutation did not match any source text"
    name = f"session_hub_row717_mutant_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, session_hub.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        exec(compile(mutated_source, session_hub.__file__, "exec"), module.__dict__)
    finally:
        del sys.modules[name]
    return module


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

    def detach(self):
        pass


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


def _entry(controller=None, **overrides):
    values = {
        "tmux_name": "worker", "state": "ready", "controller": controller or _FakeController(),
        "container": object(), "meta": ("/tmp/project", "session-id", "saved-name"),
        "last_used": 0.0, "paint_verified": True, "paint_verify_pending_generation": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _window(mod, payload, entry, *, terminal_cache=None):
    window = mod.SessionHub.__new__(mod.SessionHub)
    window.running_table = _FakeTable(_FakeItem(payload))
    window._terminal_cache = terminal_cache if terminal_cache is not None else [entry]
    window._running_terminal_stack = _FakeStack(entry.container)
    window._selected_tmux_name = entry.tmux_name
    window._running_selection = mod.RunningSelection(entry.tmux_name, 9)
    window._qt_interaction_serial = 12
    window._focused_entry = None
    window._embed_focus_grab_serial = None
    window._preload_queue = []
    window._preload_in_flight = None
    window.running_terminal_placeholder = object()
    window._note_embed_focus_grabbed = lambda serial: setattr(
        window, "_embed_focus_grab_serial", serial
    )
    return window


class RunningFocusActivationTests(unittest.TestCase):
    """Preserved from the row708 candidate: real click/Enter/double-click activation and the
    deferred-callback authority boundary, unaffected by row717's two new proof families."""

    def test_click_enter_and_double_click_call_real_activation_and_defer_focus(self):
        controller = _FakeController()
        entry = _entry(controller)
        payload = ("/tmp/project", "saved-name", "session-id", "worker")
        window = _window(session_hub, payload, entry)
        callbacks = []
        with patch.object(
            session_hub.QTimer, "singleShot",
            side_effect=lambda delay, callback: callbacks.append((delay, callback)),
        ):
            for _gesture in ("click", "enter", "double-click"):
                window._activate_running_row(window.running_table.item_value)
        self.assertEqual([delay for delay, _callback in callbacks], [0, 0, 0])
        self.assertEqual(controller.focus_calls, 0)
        for _delay, callback in callbacks:
            callback()
        self.assertEqual(controller.focus_calls, 1)

    def test_synchronous_only_focus_is_rejected_until_the_event_loop_callback(self):
        controller = _FakeController()
        entry = _entry(controller)
        window = _window(session_hub, ("/tmp/project", "saved-name", "session-id", "worker"), entry)
        callbacks = []
        with patch.object(
            session_hub.QTimer, "singleShot",
            side_effect=lambda delay, callback: callbacks.append((delay, callback)),
        ):
            window._activate_running_row(window.running_table.item_value)
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
            entry = _entry(controller)
            window = _window(session_hub, ("/tmp/project", "saved-name", "session-id", "worker"), entry)
            callbacks = []
            with patch.object(
                session_hub.QTimer, "singleShot",
                side_effect=lambda delay, callback: callbacks.append(callback),
            ):
                window._activate_running_row(window.running_table.item_value)
            mutate(window, entry)
            callbacks[0]()
            self.assertEqual(controller.focus_calls, 0)

    def test_malformed_unsafe_and_missing_identities_never_activate(self):
        previous = session_hub.RunningSelection("worker", 9)
        for identity in (None, "", "worker:name", "worker name", "=worker"):
            entry = _entry(tmux_name="worker")
            window = _window(session_hub, ("/tmp/project", "saved-name", "session-id", identity), entry)
            window._running_selection = previous
            with patch.object(window, "_select_running_terminal") as select:
                window._activate_running_row(window.running_table.item_value)
            select.assert_not_called()
            self.assertEqual(window._running_selection, previous)

        entry = _entry()
        window = _window(session_hub, None, entry)
        with patch.object(window, "_select_running_terminal") as select:
            window._activate_running_row(window.running_table.item_value)
        select.assert_not_called()


class Row717RefreshStalenessTests(unittest.TestCase):
    """New for row717: a real refresh (running_selection_after_snapshot + the production
    _reconcile_terminal_cache/_evict_entry it drives) replaces the authoritative entry between a
    scheduled callback and its execution -- the stale callback must not focus anything, and only
    the new, independently-activated entry may."""

    def test_real_refresh_replacement_stales_old_callback_before_new_activation(self):
        old_controller = _FakeController()
        old_entry = _entry(old_controller, tmux_name="old-worker")
        new_controller = _FakeController()
        new_entry = _entry(new_controller, tmux_name="new-worker")
        window = _window(
            session_hub, ("/tmp/project", "saved", "sid", "old-worker"), old_entry,
            terminal_cache=[old_entry, new_entry],
        )
        callbacks = []
        with patch.object(
            session_hub.QTimer, "singleShot",
            side_effect=lambda delay, callback: callbacks.append(callback),
        ):
            window._activate_running_row(window.running_table.item_value)
        self.assertEqual(len(callbacks), 1)
        stale_callback = callbacks[0]

        # The real refresh path: old-worker's tmux session is gone, so it is absent from the
        # fresh identity list `refresh_running_tab` would have computed. This drives the SAME
        # production selection-authority reconciliation and cache eviction refresh_running_tab
        # itself calls (see refresh_running_tab, session_hub.py), not a hand-set field.
        snapshot_generation = window._running_selection.generation
        window._reconcile_terminal_cache([("new-worker", "/tmp/project", "sid2", "saved2")])
        window._running_selection = session_hub.running_selection_after_snapshot(
            window._running_selection, snapshot_generation, {"new-worker"},
        )
        window._selected_tmux_name = window._running_selection.identity
        self.assertIsNone(window._running_selection.identity)
        # The cache is a fixed slot pool -- eviction empties the slot in place rather than
        # removing it from the list (see _evict_entry, session_hub.py).
        self.assertEqual(old_entry.state, "empty")
        self.assertIsNone(old_entry.tmux_name)

        stale_callback()
        self.assertEqual(old_controller.focus_calls, 0)
        self.assertEqual(new_controller.focus_calls, 0)

        # A genuine new activation of the surviving entry is a second, independent real click.
        window.running_table = _FakeTable(_FakeItem(("/tmp/project", "saved2", "sid2", "new-worker")))
        with patch.object(
            session_hub.QTimer, "singleShot",
            side_effect=lambda delay, callback: callbacks.append(callback),
        ):
            window._activate_running_row(window.running_table.item_value)
        self.assertEqual(len(callbacks), 2)
        callbacks[1]()
        self.assertEqual(new_controller.focus_calls, 1)
        self.assertEqual(old_controller.focus_calls, 0)


class Row717HandlerAndDeferMutationTests(unittest.TestCase):
    """New for row717: each mutation is EXECUTED against a real transformed module -- never a
    source-text assertIn/assertNotIn used as the oracle."""

    def test_disconnecting_item_clicked_stops_single_click_activation(self):
        mutated = _load_mutated_module(lambda src: src.replace(
            "        self.running_table.itemClicked.connect(self._activate_running_row)\n",
            "",
            1,
        ))
        self._assert_wiring(mutated, click_should_fire=False, activated_should_fire=True)

    def test_disconnecting_item_activated_stops_enter_and_double_click_activation(self):
        mutated = _load_mutated_module(lambda src: src.replace(
            "        self.running_table.itemActivated.connect(self._activate_running_row)\n",
            "",
            1,
        ))
        self._assert_wiring(mutated, click_should_fire=True, activated_should_fire=False)

    def test_production_wiring_connects_both_gestures(self):
        self._assert_wiring(session_hub, click_should_fire=True, activated_should_fire=True)

    def _assert_wiring(self, mod, *, click_should_fire, activated_should_fire):
        _app()
        window = mod.SessionHub.__new__(mod.SessionHub)
        QMainWindow.__init__(window)
        window.metadata = {}
        window.usage_widgets = {}
        window.usage_headers = {}
        window.usage_compact_labels = {}
        window.usage_compact_bars = {}
        activation = Mock()
        window._activate_running_row = activation
        mod.SessionHub.build_ui(window)

        item = QTableWidgetItem()
        window.running_table.itemClicked.emit(item)
        self.assertEqual(activation.called, click_should_fire)
        activation.reset_mock()
        window.running_table.itemActivated.emit(item)
        self.assertEqual(activation.called, activated_should_fire)

    def test_bypassing_defer_focus_grabs_focus_synchronously(self):
        mutated = _load_mutated_module(lambda src: src.replace(
            "cwd, tmux_name, session_id, saved_name=name, defer_focus=True,",
            "cwd, tmux_name, session_id, saved_name=name, defer_focus=False,",
            1,
        ))
        controller = _FakeController()
        entry = _entry(controller)
        window = _window(mutated, ("/tmp/project", "saved-name", "session-id", "worker"), entry)
        with patch.object(mutated.QTimer, "singleShot", side_effect=AssertionError(
            "defer_focus=False must never schedule a deferred callback"
        )):
            window._activate_running_row(window.running_table.item_value)
        # The intended control (row708's deferred-focus contract) fails under this mutation:
        # focus is granted synchronously instead of through the event-loop boundary.
        self.assertEqual(controller.focus_calls, 1)

    def test_bypassing_single_shot_grabs_focus_synchronously(self):
        mutated = _load_mutated_module(lambda src: src.replace(
            "        QTimer.singleShot(\n"
            "            0,\n"
            "            lambda: self._focus_running_terminal_if_current(\n"
            "                entry, identity, selection_generation, interaction_serial,\n"
            "                controller_generation,\n"
            "            ),\n"
            "        )\n",
            "        (lambda: self._focus_running_terminal_if_current(\n"
            "            entry, identity, selection_generation, interaction_serial,\n"
            "            controller_generation,\n"
            "        ))()\n",
            1,
        ))
        controller = _FakeController()
        entry = _entry(controller)
        window = _window(mutated, ("/tmp/project", "saved-name", "session-id", "worker"), entry)
        window._activate_running_row(window.running_table.item_value)
        # No QTimer patch needed/possible here: singleShot is no longer called at all, so the
        # callback (if the mutation worked) already ran inline, before _activate_running_row
        # returned.
        self.assertEqual(controller.focus_calls, 1)


if __name__ == "__main__":
    unittest.main()
