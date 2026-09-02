"""Row772 controls for the Session Hub watchdog start/stop button."""

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import session_hub


class _StatusLabel:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


def _toggle(action_status, lane_has_rows):
    status = _StatusLabel()
    hub = SimpleNamespace(status=status, _set_watchdog_status=lambda _status: None)
    with patch.object(session_hub, "_idle_watchdog_status", return_value=action_status), \
            patch.object(session_hub, "_vampulse_queue_has_lane_rows", return_value=lane_has_rows), \
            patch.object(session_hub.subprocess, "run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        session_hub.SessionHub._toggle_idle_watchdog(hub)
    return run.call_args.args[0]


def test_explicit_click_uses_watchdog_lifecycle_even_when_auto_start_is_gated():
    assert _toggle("OFF", False) == [
        "python3", "scripts/tools/review_ctl.py", "idle-watchdog", "start"
    ]
    assert _toggle("ON", False) == [
        "python3", "scripts/tools/review_ctl.py", "idle-watchdog", "stop"
    ]


def test_button_label_and_timer_wiring_are_watchdog_specific():
    ui = inspect.getsource(session_hub.SessionHub.build_ui)
    assert "_watchdog_toggle_button.clicked.connect(self._toggle_idle_watchdog)" in ui
    assert "_watchdog_toggle_button.clicked.connect(self.stop_selected_running)" not in ui
    assert 'QPushButton("Stop watchdog")' in ui

    status = inspect.getsource(session_hub.SessionHub._set_watchdog_status)
    assert '"Start watchdog" if status == "OFF" else "Stop watchdog"' in status
    tick = inspect.getsource(session_hub.SessionHub._on_running_status_tick)
    assert "self._set_watchdog_status(_idle_watchdog_status())" in tick

    context_menu = inspect.getsource(session_hub.SessionHub.running_context_menu)
    assert 'QAction("Stop session"' in context_menu
    assert "self.stop_selected_running" in context_menu

    # The pre-row772 direct connection is a negative control: it must not satisfy
    # the current wiring assertion if the button is reverted to stopping a session.
    reverted = ui.replace(
        "_watchdog_toggle_button.clicked.connect(self._toggle_idle_watchdog)",
        "_watchdog_toggle_button.clicked.connect(self.stop_selected_running)",
    )
    assert "_watchdog_toggle_button.clicked.connect(self.stop_selected_running)" in reverted

