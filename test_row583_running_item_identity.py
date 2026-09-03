"""Pure row583 control: RunningPane._render_item must not touch Textual's read-only
ListItem.name. Constructs the pane via __new__ (no App/mount needed) and drives
_render_item directly with an active Running row, matching the existing
RunningCardPureContractTests calling convention in the frozen suite."""

import unittest

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
import session_hub_tui


class RenderItemIdentityTests(unittest.TestCase):
    def running_row(self, **overrides):
        row = {
            "kind": "group", "key": "Claude:demo", "name": "demo-tmux",
            "provider": "Claude", "tmux_name": "demo-tmux", "display": "/projects/demo",
            "status_label": "Working", "detail": "", "age": "0m",
        }
        row.update(overrides)
        return row

    def test_render_item_does_not_raise_on_an_active_running_row(self):
        pane = session_hub_tui.RunningPane.__new__(session_hub_tui.RunningPane)
        item = pane._render_item(self.running_row())
        self.assertIsInstance(item, session_hub_tui.ListItem)

    def test_render_item_identity_still_matches_the_selection_helper(self):
        pane = session_hub_tui.RunningPane.__new__(session_hub_tui.RunningPane)
        row = self.running_row()
        # _render_item must not be the thing carrying identity for selection;
        # selection is driven entirely by _identity()/selected_key/_visible_rows.
        pane._render_item(row)
        self.assertEqual(pane._identity(row), "Claude:demo")


if __name__ == "__main__":
    unittest.main()
