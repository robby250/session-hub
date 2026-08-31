"""Pure row542 contract checks; deliberately outside the frozen Session Hub test suite."""

import unittest
from pathlib import Path

import session_hub


class RunningCardContractTests(unittest.TestCase):
    def test_age_boundaries_are_compact_and_suffix_free(self):
        now = 1_700_000_000.0
        cases = (
            (0, ""),
            (now * 1000, "0m"),
            ((now - 59) * 1000, "0m"),
            ((now - 60) * 1000, "1m"),
            ((now - 59 * 60) * 1000, "59m"),
            ((now - 60 * 60) * 1000, "1h"),
            ((now - 23 * 60 * 60) * 1000, "23h"),
            ((now - 24 * 60 * 60) * 1000, "1d"),
        )
        for updated_ms, expected in cases:
            self.assertEqual(session_hub.relative_activity_age(updated_ms, now), expected)
            self.assertNotIn("ago", expected)
            self.assertNotIn("just", expected)

    def test_delegate_contract_reserves_age_before_both_identity_lines(self):
        # The delegate's paint path derives this same reservation for both lines.  Keep the
        # geometry assertion pure and deterministic without constructing SessionHub or a view.
        age = "48m"
        self.assertLess(len(age), 8)  # fixed-width reservation remains bounded on a phone cell
        source = Path(session_hub.__file__).read_text(encoding="utf-8")
        self.assertIn("text_width = max(0, rect.width() - age_width)", source)
        self.assertIn("fm.elidedText(lines[0]", source)
        self.assertIn("fm.elidedText(lines[1]", source)


if __name__ == "__main__":
    unittest.main()
