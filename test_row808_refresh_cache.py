"""Row808 controls: the Running tab keeps its 2s cadence without repeating unchanged work."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="session-hub-row808-xdg-")

from PyQt6.QtWidgets import QApplication

import session_hub


class RunningRefreshCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window_patches(self, root: Path, calls: dict[str, int]):
        session = session_hub.Session(
            "Claude", "id-808", "demo", "/tmp/row808", "/tmp/row808", 100,
            root / "transcript.jsonl",
        )
        metadata = {
            "settings": {}, "sessions": {},
            "groups": {"/tmp/row808": {"tmux": True, "rows": [{"name": "demo"}]}},
        }

        def provider(name, value):
            def read():
                calls[name] += 1
                return value
            return read

        return [
            patch.object(session_hub, "read_metadata", return_value=metadata),
            patch.object(session_hub, "discover_sessions", return_value=[]),
            patch.object(session_hub, "codex_sessions", side_effect=provider("codex", [])),
            patch.object(session_hub, "claude_sessions", side_effect=provider("claude", [session])),
            patch.object(session_hub, "antigravity_sessions", side_effect=provider("antigravity", [])),
            patch.object(session_hub, "reconcile_tmux_desktop_env"),
            patch.object(session_hub, "tmux_live_pane_snapshot", return_value={"demo": ("%0", "1", "1")}),
            patch.object(session_hub, "live_remote_owner_names", return_value={}),
            patch.object(session_hub, "session_activity", return_value=("working", "")),
            patch.object(session_hub, "_source_change_token", return_value=None),
            patch.object(session_hub, "_directory_change_token", return_value=None),
            patch.object(session_hub, "compute_codex_tmux_owner_census", return_value={}),
            patch.object(session_hub, "CODEX_STATE", root / "codex.sqlite"),
            patch.object(session_hub, "CLAUDE_HISTORY", root / "history.jsonl"),
            patch.object(session_hub, "CLAUDE_PROJECTS", root / "projects"),
            patch.object(session_hub, "ANTIGRAVITY_CONVERSATIONS", root / "conversations"),
            patch.object(session_hub.QApplication, "platformName", return_value="xcb"),
        ]

    def test_unchanged_source_reuses_provider_lists_across_timer_ticks(self):
        calls = {"codex": 0, "claude": 0, "antigravity": 0}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            patches = self._window_patches(root, calls)
            for item in patches:
                item.start()
            try:
                window = session_hub.SessionHub()
                calls.update({key: 0 for key in calls})
                window._running_sessions_cache = None
                window.refresh_running_tab()
                window.refresh_running_tab()
                self.assertEqual(calls, {"codex": 1, "claude": 1, "antigravity": 1})
                window.close()
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_unchanged_visible_snapshot_skips_qt_table_rebuild(self):
        calls = {"codex": 0, "claude": 0, "antigravity": 0}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            patches = self._window_patches(root, calls)
            for item in patches:
                item.start()
            try:
                window = session_hub.SessionHub()
                window._running_render_signature = None
                with patch.object(window.running_table, "clearSpans", wraps=window.running_table.clearSpans) as clear:
                    window.refresh_running_tab()
                    window.refresh_running_tab()
                self.assertEqual(clear.call_count, 1)
                window.close()
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_status_json_is_read_once_until_its_file_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            status_dir = Path(temp)
            with patch.object(session_hub, "STATUS_DIR", status_dir):
                session_hub._STATUS_CACHE.clear()
                session_hub.write_session_status("id-808", "working", "detail")
                path = status_dir / "id-808.json"
                real_read_text = Path.read_text
                reads = []

                def counted_read_text(path, *args, **kwargs):
                    reads.append(path)
                    return real_read_text(path, *args, **kwargs)

                with patch.object(Path, "read_text", new=counted_read_text):
                    self.assertEqual(session_hub.read_session_status("id-808")["state"], "working")
                    self.assertEqual(session_hub.read_session_status("id-808")["state"], "working")
                self.assertEqual(reads, [path])
                session_hub.write_session_status("id-808", "done", "finished")
                self.assertEqual(session_hub.read_session_status("id-808")["state"], "done")

    def test_combined_tmux_snapshot_preserves_names_and_first_pane(self):
        result = type("Result", (), {"returncode": 0, "stdout": "a\t%0\t11\t10\na\t%1\t12\t11\nb\t%2\t13\t12\n"})()
        with patch.object(session_hub.shutil, "which", return_value="/usr/bin/tmux"):
            with patch.object(session_hub.subprocess, "run", return_value=result) as run:
                snapshot = session_hub.tmux_live_pane_snapshot()
        self.assertEqual(snapshot, {"a": ("%0", "11", "10"), "b": ("%2", "13", "12")})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ["list-panes", "-a"])


if __name__ == "__main__":
    unittest.main()
