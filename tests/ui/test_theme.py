import pytest
from PySide6.QtWidgets import QApplication

from dmig.ui import theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_accent_is_a_hex_color():
    assert theme.ACCENT.startswith("#") and len(theme.ACCENT) == 7


def test_dark_accent_is_defined():
    assert theme.ACCENT_DARK.startswith("#") and len(theme.ACCENT_DARK) == 7


def test_semantic_colors_are_light_dark_pairs():
    for name in ("SUCCESS", "MUTED"):
        pair = getattr(theme, name)
        assert isinstance(pair, tuple) and len(pair) == 2
        assert all(c.startswith("#") and len(c) == 7 for c in pair)


def test_theme_is_pinned_to_light():
    """深色已从产品移除：唯一模式是 light。"""
    assert set(theme.MODES) == {"light"}


def test_resolve_mode_always_returns_light():
    assert theme.resolve_mode("light") == "light"
    assert theme.resolve_mode("") == "light"
    assert theme.resolve_mode(None) == "light"
    assert theme.resolve_mode("rainbow") == "light"
    assert theme.resolve_mode("dark") == "light"


def test_apply_theme_is_light_no_matter_what(qapp):
    theme.apply_theme("light")
    assert theme.is_dark() is False
    theme.apply_theme("dark")
    assert theme.is_dark() is False
    theme.apply_theme("auto")
    assert theme.is_dark() is False


def test_apply_theme_tolerates_bad_mode(qapp):
    theme.apply_theme("nonsense")        # 不得抛异常
    assert theme.is_dark() is False


def test_pick_always_takes_the_light_value():
    assert theme.pick(("#aaaaaa", "#bbbbbb")) == "#aaaaaa"
    assert theme.error_color() == theme.ERROR[0]
