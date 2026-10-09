import sys

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

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


def test_valid_pair_passes(page, tmp_path):
    source = tmp_path / "a"
    source.mkdir()
    page.set_source(str(source))
    page.set_target(str(tmp_path / "b"))
    page.refresh_check()
    assert page.has_blocking_error() is False
    assert "前置校验通过" in page.issues_text()
    assert [i.code for i in page.issues()] == []


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
