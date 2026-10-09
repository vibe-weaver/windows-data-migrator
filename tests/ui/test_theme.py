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


def test_modes_cover_auto_light_dark():
    assert set(theme.MODES) == {"auto", "light", "dark"}


def test_resolve_mode_accepts_known_values():
    for m in theme.MODES:
        assert theme.resolve_mode(m) == m


def test_resolve_mode_falls_back_to_auto():
    assert theme.resolve_mode("") == "auto"
    assert theme.resolve_mode(None) == "auto"
    assert theme.resolve_mode("rainbow") == "auto"
    assert theme.resolve_mode("DARK") == "dark"


def test_apply_theme_light_then_dark(qapp):
    theme.apply_theme("light")
    light = theme.is_dark()
    theme.apply_theme("dark")
    dark = theme.is_dark()
    assert light is False
    assert dark is True
    theme.apply_theme("auto")            # 复原，避免影响其它用例


def test_apply_theme_auto_does_not_raise(qapp):
    theme.apply_theme("auto")
    assert isinstance(theme.is_dark(), bool)


def test_apply_theme_tolerates_bad_mode(qapp):
    theme.apply_theme("nonsense")        # 不得抛异常
    assert isinstance(theme.is_dark(), bool)
