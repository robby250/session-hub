"""Isolated TTY-preflight tests for session_hub_tui.main() (row581, task-2230).
Pure unit checks only -- no live TUI, tmux, GUI, or Session Hub actions."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import session_hub_tui


class TtyPreflightTests(unittest.TestCase):
    def setUp(self):
        self.original_terminal = session_hub_tui._OssTerminal
        self.original_error = session_hub_tui._textual_terminal_import_error
        # Force past the row578 adapter-availability check so these tests exercise
        # only the TTY preflight, independent of whatever is actually installed.
        session_hub_tui._OssTerminal = object()
        session_hub_tui._textual_terminal_import_error = None

    def tearDown(self):
        session_hub_tui._OssTerminal = self.original_terminal
        session_hub_tui._textual_terminal_import_error = self.original_error

    def test_no_tty_stdin_prints_diagnostic_and_never_constructs_app(self):
        constructed = []
        with patch.object(session_hub_tui, "SessionHubTUI",
                           side_effect=lambda: constructed.append(True)), \
             patch.object(sys.stdin, "isatty", return_value=False), \
             patch.object(sys.stdout, "isatty", return_value=True):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = session_hub_tui.main()
        self.assertNotEqual(rc, 0)
        self.assertEqual(constructed, [])
        self.assertIn("ssh -tt <host> session-hub-tui", buf.getvalue())

    def test_no_tty_stdout_prints_diagnostic_and_never_constructs_app(self):
        constructed = []
        with patch.object(session_hub_tui, "SessionHubTUI",
                           side_effect=lambda: constructed.append(True)), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=False):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = session_hub_tui.main()
        self.assertNotEqual(rc, 0)
        self.assertEqual(constructed, [])
        self.assertIn("ssh -tt <host> session-hub-tui", buf.getvalue())

    def test_real_tty_reaches_normal_app_startup_path(self):
        class FakeApp:
            def run(self):
                return 0

        with patch.object(session_hub_tui, "SessionHubTUI", side_effect=FakeApp), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=True):
            rc = session_hub_tui.main()
        self.assertEqual(rc, 0)

    def test_missing_adapter_check_still_wins_over_tty_check(self):
        """Row578's adapter check must still run first -- a phone launch missing the
        adapter should not be misreported as a TTY problem."""
        session_hub_tui._OssTerminal = None
        missing_adapter = ModuleNotFoundError("No module named 'textual_terminal'")
        missing_adapter.name = "textual_terminal"
        session_hub_tui._textual_terminal_import_error = missing_adapter
        with patch.object(sys.stdin, "isatty", return_value=False), \
             patch.object(sys.stdout, "isatty", return_value=False):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = session_hub_tui.main()
        self.assertEqual(rc, 2)
        self.assertIn("install with", buf.getvalue())
        self.assertNotIn("ssh -tt", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
