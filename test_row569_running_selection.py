"""Pure row569 controls for Running-list selection event ordering."""

import inspect
import unittest

import session_hub


class RunningSelectionGenerationTests(unittest.TestCase):
    def test_stale_refresh_cannot_replace_newer_click(self):
        state = session_hub.running_selection_clicked(session_hub.RunningSelection(), "B")
        stale_snapshot_generation = state.generation
        state = session_hub.running_selection_clicked(state, "A")

        # The old refresh only knows about B. It must not clear or restore B over A.
        state = session_hub.running_selection_after_snapshot(
            state, stale_snapshot_generation, {"B"}
        )
        self.assertEqual(state.identity, "A")

        # A newer/current refresh confirms A and keeps it visibly selected.
        state = session_hub.running_selection_after_snapshot(state, state.generation, {"A"})
        self.assertEqual(state.identity, "A")

    def test_current_snapshot_clears_a_genuinely_missing_identity(self):
        state = session_hub.running_selection_clicked(session_hub.RunningSelection(), "A")
        state = session_hub.running_selection_after_snapshot(state, state.generation, {"B"})
        self.assertIsNone(state.identity)
        self.assertEqual(state.generation, 1)

    def test_newer_click_is_allowed_to_change_the_visible_identity(self):
        state = session_hub.running_selection_clicked(session_hub.RunningSelection(), "A")
        state = session_hub.running_selection_clicked(state, "B")
        self.assertEqual(state.identity, "B")
        self.assertEqual(state.generation, 2)

    def test_newer_authoritative_snapshot_can_change_the_visible_identity(self):
        state = session_hub.running_selection_clicked(session_hub.RunningSelection(), "A")
        state = session_hub.running_selection_after_snapshot(
            state, state.generation + 1, {"A", "B"}, snapshot_identity="B"
        )
        self.assertEqual(state.identity, "B")
        self.assertEqual(state.generation, 2)

    def test_desktop_wiring_uses_the_generation_authority_at_both_boundaries(self):
        activate = inspect.getsource(session_hub.SessionHub._activate_running_row)
        refresh = inspect.getsource(session_hub.SessionHub.refresh_running_tab)
        self.assertIn("running_selection_clicked", activate)
        self.assertIn("running_selection_after_snapshot", refresh)
        self.assertIn("snapshot_generation", refresh)


if __name__ == "__main__":
    unittest.main()
