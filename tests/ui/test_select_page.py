import sys

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from dmig.core import fsutil
from dmig.ui import select_page
from dmig.ui.select_page import PathLineEdit, SelectPage, free_space_text

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


@pytest.fixture
def page(qtbot):
    widget = SelectPage()
    qtbot.addWidget(widget)
    return widget


def test_missing_source_blocks(page, tmp_path):
    page.set_source(str(tmp_path / "nope"))
    page.set_target(str(tmp_path / "dst"))
    page.refresh_check()
    assert page.has_blocking_error() is True
    assert "不存在" in page.issues_text()


def test_same_volume_pair_is_flagged_but_not_blocked(page, tmp_path):
    """同盘迁移不释放空间，必须说出来；但用户可能确实只想换个目录，不能拦死。"""
    source = tmp_path / "a"
    source.mkdir()
    page.set_source(str(source))
    page.set_target(str(tmp_path / "b"))
    page.refresh_check()
    assert page.has_blocking_error() is False
    assert "same_volume" in {i.code for i in page.issues()}
    assert "[提示] 源与目标在同一个卷" in page.issues_text()


def test_clean_pair_shows_the_pass_hint(page, tmp_path, monkeypatch):
    """一条问题都没有时要给绿色结论，而不是留一片空白。"""
    monkeypatch.setattr(select_page.guard, "check", lambda *a, **kw: [])
    source = tmp_path / "a"
    source.mkdir()
    page.set_source(str(source))
    page.set_target(str(tmp_path / "b"))
    page.refresh_check()
    assert page.has_blocking_error() is False
    assert page.issues() == []
    assert "前置校验通过" in page.issues_text()
    assert not page._check_hint.isVisibleTo(page)
    assert page._check_pass.isVisibleTo(page)


def test_identical_paths_block(page, tmp_path):
    source = tmp_path / "a"
    source.mkdir()
    page.set_source(str(source))
    page.set_target(str(source))
    page.refresh_check()
    assert "same_path" in {i.code for i in page.issues()}


def test_free_space_is_rendered(page, tmp_path):
    page.set_source(str(tmp_path))
    page.refresh_check()
    assert page.source_free.text() not in ("", "-")


def test_empty_selection_shows_hint(page):
    page.refresh_check()
    assert "请选择源目录与目标目录" in page.issues_text()
    assert page.has_blocking_error() is False


def test_free_space_text_handles_missing_path():
    assert free_space_text("") == "-"


def test_free_space_text_is_served_from_the_volume_cache(tmp_path, monkeypatch):
    """每敲一个字符就刷一次剩余空间：同一卷在缓存窗口内不该反复问文件系统。"""
    fsutil.clear_free_cache()
    calls: list[int] = []

    def fake(path):
        calls.append(1)
        return 2048

    monkeypatch.setattr(fsutil, "volume_free_bytes", fake)
    try:
        assert free_space_text(str(tmp_path)) == "2.0 KB"
        free_space_text(str(tmp_path / "其它目录"))
        assert len(calls) == 1
    finally:
        fsutil.clear_free_cache()


def test_free_space_text_shows_dash_when_unavailable(tmp_path, monkeypatch):
    fsutil.clear_free_cache()

    def boom(path):
        raise OSError("盘不见了")

    monkeypatch.setattr(fsutil, "volume_free_bytes", boom)
    try:
        assert free_space_text(str(tmp_path)) == "-"
    finally:
        fsutil.clear_free_cache()


def test_path_edit_accepts_dropped_url(qtbot, tmp_path):
    edit = PathLineEdit()
    qtbot.addWidget(edit)
    seen = []
    edit.dropped.connect(seen.append)

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    edit.dropEvent(event)

    assert edit.text() == str(tmp_path)
    assert seen == [str(tmp_path)]


def test_changed_signal_fires_on_set(page, tmp_path):
    seen = []
    page.changed.connect(lambda: seen.append(1))
    page.set_source(str(tmp_path))
    page.set_target(str(tmp_path / "dst"))
    assert len(seen) >= 2
