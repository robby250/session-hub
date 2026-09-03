"""Running TAB striping in the TUI, asserted on RESOLVED COLOUR rather than on CSS classes.

The user reported invisible row separation in the TUI three times while
`RunningPaneSeparatorTests` stayed green, because those tests only ever assert that a CSS
class is ATTACHED to the ListItem. `RunningPane` declared its rules as `CSS`, which Textual
reads only on an App -- on a Widget it is an inert attribute -- so every rule in the block
was dead and each striped row resolved to exactly the same background as its neighbour.

A class-attachment assertion cannot see that. These read the colour Textual actually
resolved, which is the only thing the user can see.

NOTE: Session Hub's test suite is frozen (emergency row503, task-2179). This file is written
for when that freeze lifts; the fix it covers was verified by running the same measurement
directly.
"""
import unittest

from textual.app import App

import session_hub_tui


class _PaneHost(App):
    def __init__(self, pane):
        super().__init__()
        self._pane = pane

    def compose(self):
        yield self._pane


def _session(key: str, label: str = "Working") -> dict:
    return {
        "is_group": False, "provider": "Claude", "title": key, "key": key,
        "tmux_name": key, "age": "1m", "status": "Running", "activity_label": label,
    }


class RunningPaneStripingTests(unittest.IsolatedAsyncioTestCase):
    def test_rules_are_declared_where_textual_reads_them(self):
        """`CSS` on a Widget is inert -- the whole bug in one assertion."""
        self.assertTrue(hasattr(session_hub_tui.RunningPane, "DEFAULT_CSS"))
        self.assertNotIn("CSS", vars(session_hub_tui.RunningPane))

    def test_stylesheet_has_no_hash_comment(self):
        """A `#` line is an ID selector, so it raises the moment the block goes live."""
        for line in session_hub_tui.RunningPane.DEFAULT_CSS.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                self.assertIn("{", stripped, f"`#` comment line parses as a selector: {line!r}")

    async def test_adjacent_rows_resolve_to_different_backgrounds(self):
        pane = session_hub_tui.RunningPane()
        async with _PaneHost(pane).run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            pane.apply_sessions(
                {"sessions": [_session(f"s{i}") for i in range(4)], "groups": {}}
            )
            await pilot.pause()
            rows = [
                child for child in pane.query_one("#running-list").children
                if child.has_class("running-row")
            ]
            self.assertEqual(len(rows), 4)
            backgrounds = [row.background_colors[1] for row in rows]
            for first, second in zip(backgrounds, backgrounds[1:]):
                self.assertNotEqual(
                    first, second,
                    f"adjacent Running rows resolve to the same background {first!r} -- "
                    "the user sees no separation",
                )

    async def test_striping_darkens_like_the_all_sessions_tab(self):
        """It must match All Sessions, which darkens. Lighter would stripe the other way."""
        pane = session_hub_tui.RunningPane()
        async with _PaneHost(pane).run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            pane.apply_sessions(
                {"sessions": [_session(f"s{i}") for i in range(4)], "groups": {}}
            )
            await pilot.pause()
            rows = [
                child for child in pane.query_one("#running-list").children
                if child.has_class("running-row")
            ]
            plain = next(r for r in rows if not r.has_class("running-row-even"))
            striped = next(r for r in rows if r.has_class("running-row-even"))
            plain_bg = plain.background_colors[1]
            striped_bg = striped.background_colors[1]
            self.assertLess(
                sum(striped_bg.rgb), sum(plain_bg.rgb),
                f"striped row {striped_bg!r} is not darker than {plain_bg!r}",
            )


if __name__ == "__main__":
    unittest.main()
