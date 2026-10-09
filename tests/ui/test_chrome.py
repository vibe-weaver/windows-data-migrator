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


def test_metric_row_uses_smaller_font_for_wide_text_values(qapp):
    """值是一句话时降一档字号，否则大字号会把相邻格挤在一起。

    判断依据是估算显示宽度而不是字符数：「2.2 KB」6 个字符但很窄，
    「执行时检测」5 个字符却很宽。
    """
    row = chrome.MetricRow([("被占用", "执行时检测"), ("数据量", "2.2 KB")])
    assert row.font_px("被占用") == chrome.MetricRow.VALUE_PX_WIDE
    assert row.font_px("数据量") == chrome.MetricRow.VALUE_PX
    assert row.value_text("被占用") == "执行时检测"


def test_metric_row_restores_large_font_when_value_shrinks(qapp):
    """字号必须每次设值时重判：页面是先建占位符、再在 load() 里填真值的。"""
    row = chrome.MetricRow([("被占用", "-")])
    assert row.font_px("被占用") == chrome.MetricRow.VALUE_PX
    row.set_value("被占用", "执行时检测")
    assert row.font_px("被占用") == chrome.MetricRow.VALUE_PX_WIDE
    row.set_value("被占用", "0")
    assert row.font_px("被占用") == chrome.MetricRow.VALUE_PX


def test_metric_row_display_width_counts_cjk_as_two(qapp):
    assert chrome.MetricRow.display_width("2.2 KB") == 6
    assert chrome.MetricRow.display_width("执行时检测") == 10
    assert chrome.MetricRow.display_width("482") == 3


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
