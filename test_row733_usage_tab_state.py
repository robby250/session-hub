"""task-2240 row733: regression guard for the usage-expand tab-switch race.

_announce_running_launch() decorates the Running tab's text ("Running  •") while the user is on
another tab, and _on_main_tab_changed() applies the DESTINATION tab's usage-expanded state before
it resets that label. The pre-fix lookup (SessionHub.USAGE_EXPANDED_SETTINGS_KEYS keyed by
main_tabs.tabText()) misses on the decorated label and silently forces compact regardless of what
the destination tab actually had saved. The fix keys the lookup by the destination's stable page
WIDGET (self._usage_expanded_page_keys) instead, so it is immune to any tabText() mutation.
"""

import atexit
import importlib.util
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TEST_XDG_DATA_HOME = tempfile.mkdtemp(prefix="session-hub-row733-xdg-")
os.environ["XDG_DATA_HOME"] = _TEST_XDG_DATA_HOME
atexit.register(shutil.rmtree, _TEST_XDG_DATA_HOME, ignore_errors=True)

from PyQt6.QtWidgets import QApplication  # noqa: E402

import session_hub  # noqa: E402

_QAPP: QApplication | None = None


def _app() -> QApplication:
    global _QAPP
    _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


def _load_mutated_module(mutate):
    source = Path(session_hub.__file__).read_text()
    mutated_source = mutate(source)
    if mutated_source == source:
        raise AssertionError("mutation did not change session_hub.py source")
    spec = importlib.util.spec_from_file_location(
        f"session_hub_row733_mut_{abs(hash(mutated_source))}", session_hub.__file__
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(compile(mutated_source, session_hub.__file__, "exec"), module.__dict__)
    return module


def _which_no_tmux(name, *args, **kwargs):
    if name == "tmux":
        return None
    return shutil.which(name, *args, **kwargs)


class _Harness:
    """Constructs a real, sandboxed SessionHub() window against fixed metadata."""

    def __init__(self, module, running_expanded, all_expanded=True):
        self.module = module
        self.temp_dir = tempfile.mkdtemp(prefix="session-hub-row733-data-")
        self.metadata = {
            "sessions": {},
            "settings": {
                "enable_codex": True, "enable_claude": True, "enable_antigravity": True,
                "usage_expanded_all_sessions": all_expanded,
                "usage_expanded_running": running_expanded,
            },
        }

    def __enter__(self):
        self._patches = [
            patch.object(self.module, "METADATA_PATH", Path(self.temp_dir) / "metadata.json"),
            patch.object(self.module, "PID_DIR", Path(self.temp_dir) / "pids"),
            patch.object(self.module.shutil, "which", side_effect=_which_no_tmux),
            patch.object(self.module.SessionHub, "refresh_usage"),
            patch.object(self.module, "read_metadata", return_value=self.metadata),
        ]
        for p in self._patches:
            p.start()
        self.window = self.module.SessionHub()
        return self.window

    def __exit__(self, *exc):
        self.window.close()
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class Row733UsageTabStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_switching_to_running_while_label_decorated_restores_running_saved_expanded(self):
        """Running saved TRUE (expanded); a pending launch announcement decorates its tab label
        before the user switches to it. The destination's OWN saved state must still apply."""
        with _Harness(session_hub, running_expanded=True) as window:
            running_index = window.main_tabs.indexOf(window.running_page)
            with patch.object(window, "refresh_running_tab"), patch.object(window, "apply_filter"):
                window._announce_running_launch("test-session")
                self.assertEqual(window.main_tabs.tabText(running_index), "Running  •")
                window.main_tabs.setCurrentIndex(running_index)
            self.assertFalse(window.usage_detail_frame.isHidden())
            self.assertTrue(window.usage_compact_row.isHidden())

    def test_switching_to_running_while_label_decorated_restores_running_saved_compact(self):
        """Same race, but Running saved FALSE (compact) -- must not spuriously show expanded
        either, proving the fix does not just flip the failure direction."""
        with _Harness(session_hub, running_expanded=False) as window:
            running_index = window.main_tabs.indexOf(window.running_page)
            with patch.object(window, "refresh_running_tab"), patch.object(window, "apply_filter"):
                window._announce_running_launch("test-session")
                self.assertEqual(window.main_tabs.tabText(running_index), "Running  •")
                window.main_tabs.setCurrentIndex(running_index)
            self.assertTrue(window.usage_detail_frame.isHidden())
            self.assertFalse(window.usage_compact_row.isHidden())

    def test_repeated_switches_and_refresh_stay_stable_across_the_decoration_cycle(self):
        """Repeated All<->Running switching around a launch announcement never drifts either
        tab's own persisted state or the currently-applied visibility."""
        with _Harness(session_hub, running_expanded=True) as window:
            running_index = window.main_tabs.indexOf(window.running_page)
            all_index = window.main_tabs.indexOf(window.all_sessions_page)
            with patch.object(window, "refresh_running_tab"), patch.object(window, "apply_filter"):
                window._announce_running_launch("test-session")
                for _ in range(3):
                    window.main_tabs.setCurrentIndex(running_index)
                    self.assertFalse(window.usage_detail_frame.isHidden())
                    window.main_tabs.setCurrentIndex(all_index)
                    self.assertFalse(window.usage_detail_frame.isHidden())
            self.assertEqual(window.settings()["usage_expanded_all_sessions"], True)
            self.assertEqual(window.settings()["usage_expanded_running"], True)

    def test_malformed_running_value_still_fails_closed_to_compact_under_decoration(self):
        with _Harness(session_hub, running_expanded="yes") as window:  # malformed: not bool
            running_index = window.main_tabs.indexOf(window.running_page)
            with patch.object(window, "refresh_running_tab"), patch.object(window, "apply_filter"):
                window._announce_running_launch("test-session")
                window.main_tabs.setCurrentIndex(running_index)
            self.assertTrue(window.usage_detail_frame.isHidden())
            self.assertFalse(window.usage_compact_row.isHidden())

    def test_missing_running_value_still_fails_closed_to_compact_under_decoration(self):
        with _Harness(session_hub, running_expanded=None) as window:
            del window.settings()["usage_expanded_running"]
            running_index = window.main_tabs.indexOf(window.running_page)
            with patch.object(window, "refresh_running_tab"), patch.object(window, "apply_filter"):
                window._announce_running_launch("test-session")
                window.main_tabs.setCurrentIndex(running_index)
            self.assertTrue(window.usage_detail_frame.isHidden())

    def test_negative_control_reverting_to_tabtext_lookup_fails_this_guard(self):
        """Mutation control: restore the old text-keyed lookup (the exact pre-fix production
        code) and prove this guard's positive assertion breaks -- demonstrating the guard
        actually exercises the fixed seam rather than something incidental."""
        def mutate(src: str) -> str:
            needle = (
                "        return self._usage_expanded_page_keys.get(self.main_tabs.widget(tab_index))"
            )
            replacement = (
                "        return SessionHub.USAGE_EXPANDED_SETTINGS_KEYS.get(self.main_tabs.tabText(tab_index))"
            )
            assert needle in src, "expected fixed lookup line not found"
            return src.replace(needle, replacement, 1)

        mutated = _load_mutated_module(mutate)
        with _Harness(mutated, running_expanded=True) as window:
            running_index = window.main_tabs.indexOf(window.running_page)
            with patch.object(window, "refresh_running_tab"), patch.object(window, "apply_filter"):
                mutated.SessionHub._announce_running_launch(window, "test-session")
                window.main_tabs.setCurrentIndex(running_index)
            # Pre-fix behavior: the decorated label desyncs the lookup and forces compact even
            # though Running's saved state is True (expanded) -- the opposite of the fixed guard.
            self.assertTrue(window.usage_detail_frame.isHidden())


if __name__ == "__main__":
    unittest.main()
