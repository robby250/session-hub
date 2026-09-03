"""Running tab row separation: alternating row colours + a visible 1px divider.

The user reported three times that the Running tab's separators were invisible and that it did not
match the All Sessions tab. Both halves are asserted here so a future palette change cannot quietly
take them away again.
"""
import _test_sandbox  # noqa: F401  (sandboxes XDG_DATA_HOME at import)

import pytest

session_hub = pytest.importorskip("session_hub")
QColor = pytest.importorskip("PyQt6.QtGui").QColor


def test_separator_is_visible_against_a_dark_row():
    """The regression itself: on the dark theme the old QPalette.Mid line vanished."""
    base = QColor("#25272d")
    line = session_hub._running_separator_color(base)
    assert line.lightness() > base.lightness() + 20, (
        f"separator {line.name()} is not lighter enough than base {base.name()} to be seen"
    )


def test_separator_is_visible_against_a_light_row():
    base = QColor("#ffffff")
    line = session_hub._running_separator_color(base)
    assert line.lightness() < base.lightness() - 20, (
        f"separator {line.name()} is not darker enough than base {base.name()} to be seen"
    )


def test_separator_contrasts_across_the_whole_grey_ramp():
    """Negative control for the branch itself -- neither arm may return the base unchanged."""
    for level in range(0, 256, 15):
        base = QColor(level, level, level)
        line = session_hub._running_separator_color(base)
        assert abs(line.lightness() - base.lightness()) >= 15, (
            f"no contrast at base lightness {base.lightness()}: {line.name()}"
        )


def test_theme_alternate_base_is_preserved_when_it_is_visible():
    """Match All Sessions: a theme that already stripes must be used as-is."""
    base, alt = QColor("#1b1d22"), QColor("#24272e")
    assert session_hub._running_alternate_base(base, alt) == alt


def test_alternate_base_is_derived_when_the_theme_stripes_invisibly():
    """The failure the user would see as 'still not separated'."""
    base = QColor("#1b1d22")
    got = session_hub._running_alternate_base(base, QColor(base))
    assert got != base
    assert abs(got.lightness() - base.lightness()) >= 10


def test_alternate_base_is_derived_at_both_ends_of_the_ramp():
    for level in (0, 8, 127, 128, 250, 255):
        base = QColor(level, level, level)
        got = session_hub._running_alternate_base(base, QColor(base))
        assert abs(got.lightness() - base.lightness()) >= 10, (
            f"no visible stripe at base lightness {level}: {got.name()}"
        )


def test_running_table_uses_alternating_row_colours():
    """It must match All Sessions. This is the line that was set to False."""
    source = open(session_hub.__file__, encoding="utf-8").read()
    assert "self.running_table.setAlternatingRowColors(True)" in source
    assert "self.running_table.setAlternatingRowColors(False)" not in source
    assert "self.table.setAlternatingRowColors(True)" in source
