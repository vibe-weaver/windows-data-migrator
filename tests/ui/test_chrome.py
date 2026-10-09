import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from dmig.ui import chrome


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_brand_bar_shows_app_name(qapp):
    bar = chrome.BrandBar()
    assert "数据目录迁移工具" in bar.title_text()


def test_step_bar_marks_done_current_todo(qapp):
    bar = chrome.StepBar(["一", "二", "三", "四"])
    bar.set_current(1)
    assert bar.current() == 1
    assert bar.states() == ["done", "current", "todo", "todo"]
    bar.set_current(0)
    assert bar.states() == ["current", "todo", "todo", "todo"]
    bar.set_current(3)
    assert bar.states() == ["done", "done", "done", "current"]


def test_step_bar_clamps_out_of_range(qapp):
    bar = chrome.StepBar(["一", "二"])
    bar.set_current(99)
    assert bar.current() == 1
    bar.set_current(-5)
    assert bar.current() == 0


def test_metric_row_reads_back_values(qapp):
    row = chrome.MetricRow([("文件数", "8"), ("链接数", "482")])
    assert row.value_text("文件数") == "8"
    assert row.value_text("链接数") == "482"
    row.set_value("文件数", "9")
    assert row.value_text("文件数") == "9"


def test_metric_row_unknown_key_returns_empty(qapp):
    row = chrome.MetricRow([("文件数", "8")])
    assert row.value_text("不存在") == ""


def test_action_bar_places_widgets(qapp):
    bar = chrome.ActionBar()
    left = QPushButton("左")
    right = QPushButton("右")
    bar.add_left(left)
    bar.add_right(right)
    assert left.parent() is not None
    assert right.parent() is not None


def test_card_is_a_widget(qapp):
    c = chrome.Card()
    assert c.objectName() == "dmigCard"
