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
            module, already_present = session_hub_tui._install_textual_terminal_default_colors_shim()
            self.assertTrue(hasattr(textual_app, "DEFAULT_COLORS"))
            self.assertFalse(already_present)

            textual_app.DEFAULT_COLORS = "sentinel"
            module2, already_present2 = session_hub_tui._install_textual_terminal_default_colors_shim()
            self.assertEqual(textual_app.DEFAULT_COLORS, "sentinel")
            self.assertTrue(already_present2)
        finally:
            if had_attr:
                textual_app.DEFAULT_COLORS = original
            elif hasattr(textual_app, "DEFAULT_COLORS"):
                del textual_app.DEFAULT_COLORS

    def test_shim_is_removed_after_import_and_preexisting_value_survives(self):
        """Reviewer finding (5c7e979ff4eb): the shim must not leak a process-wide
        textual.app.DEFAULT_COLORS. Install must be undone in a finally, and a
        genuinely pre-existing value must be left untouched."""
        import textual.app as textual_app

        had_attr = hasattr(textual_app, "DEFAULT_COLORS")
        original = getattr(textual_app, "DEFAULT_COLORS", None)
        try:
            del textual_app.DEFAULT_COLORS
        except AttributeError:
            pass
        try:
            module, already_present = session_hub_tui._install_textual_terminal_default_colors_shim()
            self.assertFalse(already_present)
            session_hub_tui._remove_textual_terminal_default_colors_shim(module, already_present)
            self.assertFalse(hasattr(textual_app, "DEFAULT_COLORS"))

            textual_app.DEFAULT_COLORS = "preexisting"
            module, already_present = session_hub_tui._install_textual_terminal_default_colors_shim()
            self.assertTrue(already_present)
            session_hub_tui._remove_textual_terminal_default_colors_shim(module, already_present)
            self.assertEqual(textual_app.DEFAULT_COLORS, "preexisting")
        finally:
            if had_attr:
                textual_app.DEFAULT_COLORS = original
            elif hasattr(textual_app, "DEFAULT_COLORS"):
                del textual_app.DEFAULT_COLORS

    def test_module_boundary_leaves_no_default_colors_after_import(self):
        """The actual module-load-time shim (used around the real adapter import)
        must not leave DEFAULT_COLORS installed once the import boundary is done."""
        import textual.app as textual_app

        self.assertFalse(hasattr(textual_app, "DEFAULT_COLORS"))

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
            # Restoring the module into sys.modules needs the same transient shim
            # production code uses -- the seam is scoped to the import, not global.
            module, already_present = session_hub_tui._install_textual_terminal_default_colors_shim()
            try:
                importlib.import_module("textual_terminal")
            finally:
                session_hub_tui._remove_textual_terminal_default_colors_shim(module, already_present)

    def test_main_distinguishes_missing_from_incompatible_adapter(self):
        """Mutation control: catches reverting main() to one message for both a
        missing adapter package and an installed-but-incompatible one."""
        original_terminal = session_hub_tui._OssTerminal
        original_error = session_hub_tui._textual_terminal_import_error
        try:
            session_hub_tui._OssTerminal = None
            missing_adapter = ModuleNotFoundError("No module named 'textual_terminal'")
            missing_adapter.name = "textual_terminal"
            session_hub_tui._textual_terminal_import_error = missing_adapter
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

    def test_main_treats_nested_dependency_module_not_found_as_incompatible(self):
        """Reviewer finding (5c7e979ff4eb): a ModuleNotFoundError for a transitive
        dependency (e.g. pyte) of an INSTALLED textual_terminal must not print the
        misleading adapter-install command."""
        original_terminal = session_hub_tui._OssTerminal
        original_error = session_hub_tui._textual_terminal_import_error
        try:
            session_hub_tui._OssTerminal = None
            nested_missing = ModuleNotFoundError("No module named 'pyte'")
            nested_missing.name = "pyte"
            session_hub_tui._textual_terminal_import_error = nested_missing
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = session_hub_tui.main()
            self.assertEqual(rc, 2)
            self.assertNotIn("install with", buf.getvalue())
            self.assertIn("pyte", buf.getvalue())
        finally:
            session_hub_tui._OssTerminal = original_terminal
            session_hub_tui._textual_terminal_import_error = original_error


if __name__ == "__main__":
    unittest.main()
