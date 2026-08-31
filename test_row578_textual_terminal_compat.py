"""Isolated dependency-contract tests for the textual/textual-terminal import
boundary (row578, task-2193). Pure import/unit checks only -- no Session Hub
GUI, TUI, tmux, or live Textual App is started."""

from __future__ import annotations

import importlib
import io
import sys
import unittest
from contextlib import redirect_stderr

import session_hub_tui


class TextualTerminalCompatTests(unittest.TestCase):
    def test_shim_adds_missing_default_colors_without_clobbering(self):
        import textual.app as textual_app

        had_attr = hasattr(textual_app, "DEFAULT_COLORS")
        original = getattr(textual_app, "DEFAULT_COLORS", None)
        try:
            if had_attr:
                del textual_app.DEFAULT_COLORS
            session_hub_tui._shim_textual_terminal_default_colors()
            self.assertTrue(hasattr(textual_app, "DEFAULT_COLORS"))

            textual_app.DEFAULT_COLORS = "sentinel"
            session_hub_tui._shim_textual_terminal_default_colors()
            self.assertEqual(textual_app.DEFAULT_COLORS, "sentinel")
        finally:
            if had_attr:
                textual_app.DEFAULT_COLORS = original
            elif hasattr(textual_app, "DEFAULT_COLORS"):
                del textual_app.DEFAULT_COLORS

    def test_installed_pair_imports_and_constructs_terminal_adapter(self):
        self.assertIsNotNone(session_hub_tui._OssTerminal)
        self.assertIsNone(session_hub_tui._textual_terminal_import_error)

    def test_without_the_compat_seam_the_pinned_pair_does_not_import(self):
        """Mutation control: prove the seam is load-bearing, not a no-op, against
        the actually-installed Textual 8.2.8 + textual-terminal 0.3.0 pair."""
        import textual.app as textual_app

        had_attr = hasattr(textual_app, "DEFAULT_COLORS")
        original = getattr(textual_app, "DEFAULT_COLORS", None)
        sys.modules.pop("textual_terminal", None)
        sys.modules.pop("textual_terminal._terminal", None)
        try:
            if had_attr:
                del textual_app.DEFAULT_COLORS
            with self.assertRaises(ImportError):
                importlib.import_module("textual_terminal")
        finally:
            if had_attr:
                textual_app.DEFAULT_COLORS = original
            elif hasattr(textual_app, "DEFAULT_COLORS"):
                del textual_app.DEFAULT_COLORS
            sys.modules.pop("textual_terminal", None)
            sys.modules.pop("textual_terminal._terminal", None)
            importlib.import_module("textual_terminal")

    def test_main_distinguishes_missing_from_incompatible_adapter(self):
        """Mutation control: catches reverting main() to one message for both a
        missing adapter package and an installed-but-incompatible one."""
        original_terminal = session_hub_tui._OssTerminal
        original_error = session_hub_tui._textual_terminal_import_error
        try:
            session_hub_tui._OssTerminal = None
            session_hub_tui._textual_terminal_import_error = ModuleNotFoundError(
                "No module named 'textual_terminal'"
            )
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = session_hub_tui.main()
            self.assertEqual(rc, 2)
            self.assertIn("install with", buf.getvalue())

            session_hub_tui._textual_terminal_import_error = ImportError(
                "cannot import name 'DEFAULT_COLORS' from 'textual.app'"
            )
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = session_hub_tui.main()
            self.assertEqual(rc, 2)
            self.assertNotIn("install with", buf.getvalue())
            self.assertIn("DEFAULT_COLORS", buf.getvalue())
        finally:
            session_hub_tui._OssTerminal = original_terminal
            session_hub_tui._textual_terminal_import_error = original_error


if __name__ == "__main__":
    unittest.main()
