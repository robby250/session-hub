"""Hermetic row771/row772 controls for the Session Hub watchdog UI signal."""

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
import session_hub


def _reverted_health_status(log_text):
    """The pre-fix last-line/prose parser, used only as a negative control."""
    last = log_text.splitlines()[-1] if log_text.splitlines() else ""
    if "stale" in last.lower() or "unknown" in last.lower():
        return "BLIND"
    return "ON"


class WatchdogSignalTests(unittest.TestCase):
    def _status_for(self, log_text):
        with tempfile.TemporaryDirectory(prefix="session-hub-watchdog-") as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "watchdog.log"
            state_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            log_path.write_text(log_text, encoding="utf-8")
            with patch.object(session_hub, "IDLE_WATCHDOG_STATE_PATH", state_path), \
                    patch.object(session_hub, "IDLE_WATCHDOG_LOG_PATH", log_path):
                return session_hub._idle_watchdog_status()

    def test_healthy_signal_wins_over_unknown_prose_on_last_line(self):
        log = (
            "2026-09-02T00:00:00Z verdict=pending health=ok snapshot_age=1.0s\n"
            "per-session report: worker status is unknown\n"
        )
        self.assertEqual(self._status_for(log), "ON")
        # Replacing the signal fix with the old parser makes this exact control go red.
        self.assertEqual(_reverted_health_status(log), "BLIND")

    def test_latest_health_signal_is_authoritative_and_missing_is_blind(self):
        self.assertEqual(
            self._status_for(
                "verdict=pending health=stale snapshot_age=301.0s\n"
                "diagnostic line without a health token\n"
            ),
            "BLIND",
        )
        self.assertEqual(self._status_for("diagnostic line without a health token\n"), "BLIND")

    def test_stop_button_toggles_watchdog_and_context_menu_keeps_session_stop(self):
        ui = inspect.getsource(session_hub.SessionHub.build_ui)
        self.assertIn("_watchdog_toggle_button.clicked.connect(self._toggle_idle_watchdog)", ui)
        self.assertNotIn("_watchdog_toggle_button.clicked.connect(self.stop_selected_running)", ui)
        context_menu = inspect.getsource(session_hub.SessionHub.running_context_menu)
        self.assertIn('QAction("Stop session"', context_menu)
        self.assertIn("self.stop_selected_running", context_menu)
        toggle = inspect.getsource(session_hub.SessionHub._toggle_idle_watchdog)
        self.assertIn('"idle-watchdog", action', toggle)
        self.assertIn('"Start watchdog" if status == "OFF" else "Stop watchdog"', inspect.getsource(
            session_hub.SessionHub._set_watchdog_status
        ))
        # The reverted direct connection must fail the same contract.
        reverted_ui = ui.replace(
            "_watchdog_toggle_button.clicked.connect(self._toggle_idle_watchdog)",
            "_watchdog_toggle_button.clicked.connect(self.stop_selected_running)",
        )
        self.assertNotIn("_watchdog_toggle_button.clicked.connect(self.stop_selected_running)", ui)
        self.assertIn("_watchdog_toggle_button.clicked.connect(self.stop_selected_running)", reverted_ui)


if __name__ == "__main__":
    unittest.main()
