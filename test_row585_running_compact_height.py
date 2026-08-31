"""Pure row585 control: the Running list must hug one/few cards with only a small
intentional gap, cap growth so a many-row list still scrolls instead of pushing the
terminal off-screen, and hand the terminal whatever height it does not use. Parses
RunningPane.CSS structurally rather than repeating the new literal, so it fails if the
list reverts to a fixed reservation OR the many-card cap disappears."""

import re
import unittest

import session_hub_tui


def _css_rule(css: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"no {selector!r} rule in RunningPane.CSS"
    body = match.group(1)
    return dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", body))


class RunningListCompactHeightTests(unittest.TestCase):
    def setUp(self):
        self.list_rule = _css_rule(session_hub_tui.RunningPane.CSS, "#running-list")
        self.host_rule = _css_rule(session_hub_tui.RunningPane.CSS, "#terminal-host")

    def test_list_height_is_content_driven_not_a_fixed_reservation(self):
        # A fixed integer height (the old "height: 12") reserves that space even for
        # one card; "auto" is what lets the list hug its actual content.
        self.assertEqual(self.list_rule["height"], "auto")

    def test_list_still_caps_growth_so_many_cards_scroll_instead_of_expanding(self):
        self.assertIn("max-height", self.list_rule)
        cap = int(self.list_rule["max-height"])
        self.assertGreater(cap, 0)

    def test_empty_or_single_card_keeps_only_a_small_gap_not_a_large_reservation(self):
        min_height = int(self.list_rule["min-height"])
        cap = int(self.list_rule["max-height"])
        self.assertLess(min_height, cap)

    def test_terminal_host_still_claims_the_remaining_height(self):
        self.assertEqual(self.host_rule["height"], "1fr")


if __name__ == "__main__":
    unittest.main()
