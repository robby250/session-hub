"""Row787: an embedded terminal answer invalidates a stale needs-input badge immediately."""

import inspect
import os
import shutil
import tempfile
import unittest
import atexit
from pathlib import Path
from unittest.mock import patch

# Main does not yet carry the shared test-sandbox helper. Keep this module safe on its own so
# importing session_hub can never resolve the user's live metadata/status paths.
_TEST_XDG_DATA_HOME = tempfile.mkdtemp(prefix="session-hub-row787-xdg-")
os.environ["XDG_DATA_HOME"] = _TEST_XDG_DATA_HOME
atexit.register(shutil.rmtree, _TEST_XDG_DATA_HOME, ignore_errors=True)

import session_hub
from session_status import mark_needs_input_answered


class NeedsInputInvalidationTests(unittest.TestCase):
    def _session(self):
        return session_hub.Session(
            "Claude", "session-787", "terminal", "/tmp", "/tmp", 0,
            Path("/tmp/session-787.jsonl"),
        )

    def test_submitted_answer_clears_needs_input_without_a_census(self):
        with tempfile.TemporaryDirectory(prefix="session-hub-row787-") as directory:
            status_dir = Path(directory)
            with patch.object(session_hub, "STATUS_DIR", status_dir), \
                    patch.object(session_hub, "session_is_tracked_alive", return_value=True):
                session_hub.write_session_status(
                    "session-787", "needs_input", "pick one", reason="agent_needs_input"
                )
                # The answer path is the embedded helper's atomic status invalidation. Read the
                # same verdict function immediately; no refresh_running_tab/census is involved.
                self.assertTrue(mark_needs_input_answered(status_dir, "session-787"))
                self.assertEqual(session_hub.session_activity(self._session())[0], "working")

    def test_genuine_blocker_still_shows_needs_input_until_answered(self):
        with tempfile.TemporaryDirectory(prefix="session-hub-row787-") as directory:
            status_dir = Path(directory)
            with patch.object(session_hub, "STATUS_DIR", status_dir), \
                    patch.object(session_hub, "session_is_tracked_alive", return_value=True):
                session_hub.write_session_status(
                    "session-787", "needs_input", "pick one", reason="agent_needs_input"
                )
                self.assertEqual(session_hub.session_activity(self._session())[0], "needs_input")

    def test_after_answer_a_new_question_restores_needs_input(self):
        with tempfile.TemporaryDirectory(prefix="session-hub-row787-") as directory:
            status_dir = Path(directory)
            with patch.object(session_hub, "STATUS_DIR", status_dir), \
                    patch.object(session_hub, "session_is_tracked_alive", return_value=True):
                session_hub.write_session_status(
                    "session-787", "needs_input", "first", reason="agent_needs_input"
                )
                self.assertTrue(mark_needs_input_answered(status_dir, "session-787"))
                session_hub.write_session_status(
                    "session-787", "needs_input", "second", reason="agent_needs_input"
                )
                self.assertEqual(session_hub.session_activity(self._session())[0], "needs_input")

    def test_reverted_invalidation_negative_control_stays_needs_input(self):
        with tempfile.TemporaryDirectory(prefix="session-hub-row787-") as directory:
            status_dir = Path(directory)
            with patch.object(session_hub, "STATUS_DIR", status_dir), \
                    patch.object(session_hub, "session_is_tracked_alive", return_value=True):
                session_hub.write_session_status(
                    "session-787", "needs_input", "stale", reason="agent_needs_input"
                )
                # Negative control: with the answer invalidation omitted, the stale verdict is
                # still observable immediately, which is the bug this row closes.
                self.assertEqual(session_hub.session_activity(self._session())[0], "needs_input")

    def test_embedded_answer_wiring_is_event_driven_and_interval_is_unchanged(self):
        helper = Path(__file__).with_name("vte_embed_helper.py").read_text(encoding="utf-8")
        controller = inspect.getsource(session_hub.EmbeddedTerminalController.begin_attach)
        session_hub_source = inspect.getsource(session_hub.SessionHub)
        self.assertIn("Gdk.KEY_Return", helper)
        self.assertIn("mark_needs_input_answered", helper)
        self.assertIn('"--session-id"', controller)
        self.assertIn("_activity_status_watcher", session_hub_source)
        self.assertIn("QFileSystemWatcher", session_hub_source)
        self.assertIn("self._status_timer.setInterval(2000)", session_hub_source)


if __name__ == "__main__":
    unittest.main()
