"""Pure row542 contract checks; deliberately outside the frozen Session Hub test suite."""

import inspect
import unittest

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
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
        # Exercise the same pure helper called by the production delegate.  This is a deliberately
        # narrow cell with the long identity that previously painted underneath the age label.
        cell = (4, 2, 88, 32)
        name = "VAMP-orchestrator"
        subtitle = "gpt-5.6-luna · /home/user/projects/vampulse"
        age = "48m"
        age_width = 24  # representative QFontMetrics width plus the delegate's padding
        geometry = session_hub.running_card_text_geometry(
            cell[0], cell[1], cell[2], cell[3], age_width
        )
        self.assertEqual(age, "48m")
        self.assertGreater(len(name), 8)
        self.assertGreater(len(subtitle), 8)

        cell_left, cell_top, cell_width, cell_height = cell
        cell_right = cell_left + cell_width
        cell_bottom = cell_top + cell_height
        identity_x, identity_y, identity_width, identity_height = geometry["identity"]
        age_x, age_y, age_rect_width, age_height = geometry["age"]
        self.assertLessEqual(identity_x + identity_width, age_x)
        for x, y, width, height in geometry.values():
            self.assertGreaterEqual(x, cell_left)
            self.assertGreaterEqual(y, cell_top)
            self.assertLessEqual(x + width, cell_right)
            self.assertLessEqual(y + height, cell_bottom)
        self.assertGreater(age_rect_width, 0)
        self.assertEqual(age_y, cell_top)
        self.assertEqual(identity_y, cell_top)
        self.assertEqual(age_height, cell_height)
        self.assertEqual(identity_height, cell_height)

    def test_oversized_age_reservation_stays_inside_narrow_cell(self):
        geometry = session_hub.running_card_text_geometry(10, 3, 12, 28, 40)
        for x, y, width, height in geometry.values():
            self.assertGreaterEqual(width, 0)
            self.assertGreaterEqual(height, 0)
            self.assertGreaterEqual(x, 10)
            self.assertGreaterEqual(y, 3)
            self.assertLessEqual(x + width, 22)
            self.assertLessEqual(y + height, 31)
        self.assertEqual(geometry["identity"][2], 0)

    def test_identity_stack_uses_the_full_height_card(self):
        geometry = session_hub.running_card_text_geometry(4, 2, 240, 58, 28)
        self.assertEqual(geometry["identity"], (4, 2, 212, 58))
        self.assertEqual(geometry["age"][1], 2)
        self.assertEqual(geometry["age"][3], 58)

    def test_subtitle_is_clipped_at_the_real_cell_not_pre_elided(self):
        source = inspect.getsource(session_hub.RunningNameAgeDelegate.paint)
        self.assertNotIn("elidedText", source)
        self.assertIn("horizontalAdvance(age) + 3", source)
        self.assertIn('geometry["identity"]', source)
        self.assertIn("Qt.TextFlag.TextWordWrap", source)
        self.assertNotIn("Qt.TextFlag.TextSingleLine", source)
        self.assertNotIn('geometry["subtitle"]', source)
        self.assertIn("painter.setClipRect(rect)", source)


if __name__ == "__main__":
    unittest.main()
