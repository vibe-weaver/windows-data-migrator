import sys

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from dmig.core import fsutil
from dmig.ui import select_page, theme
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
    assert "[警告] 源与目标在同一个卷" in page.issues_text()


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


# --- 校验只由「开始检验」触发 -----------------------------------------

def test_editing_a_path_does_not_run_the_check(page, tmp_path, monkeypatch):
    """改路径不该自动跑校验。

    自动跑的那一遍里**没有试探改名**——而改名失败恰恰是最容易让整趟迁移白跑的
    风险（复制校验全过、卡在最后一步）。一边只查了一半、一边给出绿色的
    「前置校验通过」，比什么都不给更误导。
    """
    calls = []
    monkeypatch.setattr(select_page.guard, "check",
                        lambda *a, **kw: calls.append(a) or [])
    src = tmp_path / "a"
    src.mkdir()

    page.set_source(str(src))
    page.set_target(str(tmp_path / "b"))

    assert calls == [], "改路径触发了自动校验"


def test_unchecked_state_says_so_instead_of_showing_a_pass(page, tmp_path):
    """选好目录但还没检验时，给中性指引，不能给绿色结论。"""
    src = tmp_path / "a"
    src.mkdir()
    page.set_source(str(src))
    page.set_target(str(tmp_path / "b"))

    assert page.is_checked() is False
    assert page.has_blocking_error() is False
    assert "开始检验" in page.issues_text()
    assert "通过" not in page.issues_text()


def test_editing_after_a_check_invalidates_it(page, tmp_path, monkeypatch):
    """检验过之后改了路径，那份结论就作废——它说的是另一条路径。

    检验现在是「进忙碌态 → worker 跑 → 回填」三段，页面自己不再跑重活
    （试探改名与文件数线性相关，放主线程会冻界面）。
    """
    src = tmp_path / "a"
    src.mkdir()
    other = tmp_path / "b"
    other.mkdir()

    page.set_source(str(src))
    page.set_target(str(tmp_path / "dst"))
    page.begin_check()
    page.apply_check([])                     # 空问题列表 = 检验通过
    assert page.is_checked() is True

    page.set_source(str(other))

    assert page.is_checked() is False
    assert "通过" not in page.issues_text()


# --- 检验按钮：试探改名 + 原有前置校验 ---------------------------------

def test_check_button_asks_the_window_to_run_the_check(page):
    """页面只发信号，跑检查这件事由窗口决定（和 plan_page 的 replan 一个路数）。"""
    seen = []
    page.check_requested.connect(lambda: seen.append(1))
    page.check_button.click()
    assert seen == [1]


def test_begin_check_disables_the_button_and_locks_the_paths(page, tmp_path):
    """检验期间按钮要说明自己在忙、路径框要锁住。

    试探改名动的是**当前**这些路径，中途被改掉，回来的结论就张冠李戴了。
    """
    src = tmp_path / "a"
    src.mkdir()
    page.set_source(str(src))
    page.set_target(str(tmp_path / "b"))

    page.begin_check()

    assert page.check_button.text() == "正在检验…"
    assert page.check_button.isEnabled() is False
    assert page.source_edit.isReadOnly() is True
    assert page.target_edit.isReadOnly() is True

    page.apply_check([])

    assert page.check_button.text() == "开始检验"
    assert page.check_button.isEnabled() is True
    assert page.source_edit.isReadOnly() is False


def test_probe_does_not_run_on_the_automatic_refresh(page, tmp_path, monkeypatch):
    """自动刷新（改路径时的实时校验）**不能**跑试探。

    试探有副作用：源路径会消失几毫秒、父目录 mtime 会变。每次敲键盘都触一次，
    正开着这个目录的编辑器会不停地报「文件夹已删除」。
    """
    called = []
    monkeypatch.setattr(select_page.guard, "probe_rename",
                        lambda s: called.append(s) or [])
    src = tmp_path / "a"
    src.mkdir()

    page.set_source(str(src))
    page.set_target(str(tmp_path / "b"))
    page.refresh_check()

    assert called == [], "实时刷新不该触发试探"


def test_errors_and_warnings_are_coloured_differently(page, tmp_path, monkeypatch):
    """红色 = 错误（拦），黄色 = 警告（不拦）。"""
    src = tmp_path / "a"
    src.mkdir()
    monkeypatch.setattr(select_page.guard, "check", lambda *a, **kw: [
        select_page.guard.Issue("boom", "会拦住下一步的问题"),
        select_page.guard.Issue("meh", "只是提醒", severity="warning"),
    ])
    page.set_source(str(src))
    page.set_target(str(tmp_path / "b"))
    page.refresh_check()

    colors = page.issue_colors()
    assert len(colors) == 2
    assert colors[0] == theme.error_color()
    assert colors[1] == theme.warning_color()
    assert colors[0] != colors[1]
