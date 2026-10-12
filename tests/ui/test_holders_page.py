"""占用进程页：六个状态的呈现，以及"什么能给用户、什么不能"。

**这一页的每一处措辞都在替一个不可逆的动作做铺垫**（关程序会丢未保存的内容），
所以断言的重点不是"显示了什么"，而是"该说的有没有说到位"。
"""
import sys

import pytest
import qfluentwidgets as qf

from PySide6.QtGui import QColor

from dmig.core.locks import Holder
from dmig.ui.holders_page import (CLOSING, DONE, HOLDERS, NONE, SCANNING,
                                  HoldersPage)
from dmig.ui.theme import warning_color

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

WPS = Holder(name="WPS PDF", pid=12208, exe=r"C:\WPS\wpspdf.exe",
             protected_reason="")
CODE = Holder(name="Visual Studio Code", pid=3344,
              exe=r"C:\Program Files\Microsoft VS Code\Code.exe",
              protected_reason="")
EXPLORER = Holder(name="资源管理器", pid=8812, exe=r"C:\Windows\explorer.exe",
                  protected_reason="关掉它会让桌面和任务栏一起消失")


@pytest.fixture
def page(qtbot):
    widget = HoldersPage()
    qtbot.addWidget(widget)
    return widget


def test_page_starts_in_the_scanning_state(page):
    assert page.state() == SCANNING
    assert page.close_button.isHidden()
    assert page.force_button.isHidden()


def test_progress_reports_both_stages(page):
    page.set_probe_progress(3, 12)
    assert "3 / 12" in page.stage_text() and "缩小范围" in page.stage_text()
    page.set_scan_progress(1200, 4800)
    assert "1,200 / 4,800" in page.stage_text() and "扫描占用" in page.stage_text()


def test_holders_are_listed_and_marked_closeable(page):
    page.show_holders([WPS], names=["node_modules"])

    assert page.state() == HOLDERS
    assert page.holder_rows() == [
        ["WPS PDF", "12208", r"C:\WPS\wpspdf.exe", "可由本工具关闭"]]
    assert page.close_button.isEnabled() is True
    assert "1 个程序" in page.close_label()
    assert "node_modules" in page.stage_text()


def test_protected_holder_is_shown_with_its_reason_and_not_closeable(page):
    """受保护进程照列，但必须写明**为什么**工具不帮用户关。

    默默不做、或者干脆一起关掉，都比说清楚更糟。
    """
    page.show_holders([EXPLORER], names=["build"])

    assert page.holder_rows()[0][3] == "不关闭"
    assert "不会关闭" in page.note_text()
    assert "桌面" in page.note_text()
    assert page.close_button.isEnabled() is False
    assert page.close_button.isHidden() is True


def test_mixed_batch_closes_only_the_closeable_count(page):
    page.show_holders([WPS, EXPLORER])

    assert page.close_button.isEnabled() is True
    assert "1 个程序" in page.close_label()
    assert "不会关闭" in page.note_text()


def test_no_holder_state_names_the_likely_cause(page):
    """查不出进程时不能只说「没查到」——要点名最可能的原因，否则用户没法行动。"""
    page.show_no_holder(names=["build"])

    assert page.state() == NONE
    assert "没有查出" in page.verdict_text()
    assert "build" in page.note_text()
    assert "工作目录" in page.note_text()
    assert page.close_button.isHidden()


def test_the_scan_note_goes_to_the_note_line_not_the_stage(page):
    """扫描的交代进**说明行**，阶段行只留短状态。

    阶段行是状态行（「正在扫描占用：412/5000 个文件」那种），把「顶层条目太多，
    已跳过逐个试探」这种整句话塞进去会把卡片撑得很难看，而且那不是"状态"。
    """
    page.show_holders([WPS], names=[],
                      note="源目录顶层有 8,000 个条目，已跳过逐个试探直接扫整棵树。")

    assert page.stage_text() == "扫描完成", page.stage_text()
    assert "跳过" in page.note_text()
    assert "跳过" not in page.stage_text()


def test_scan_note_and_protected_warning_can_coexist(page):
    """两段说明都要在：一段讲扫描怎么了，一段讲哪些程序工具不动。

    原来只处理其中一段——加了扫描说明就会把「不会关闭」那段警告盖掉。
    """
    page.show_holders([WPS, EXPLORER], names=[],
                      note="源目录顶层有 8,000 个条目，已跳过逐个试探直接扫整棵树。")

    text = page.note_text()
    assert "跳过" in text, text
    assert "不会关闭" in text and "桌面" in text, text
    # 有受保护进程时整段按警告色显示，不能因为是「说明」就变成灰的
    assert page.note_color() == warning_color()


def test_no_holder_stage_never_claims_the_range_was_narrowed(page):
    """阶段文字不能说假话。

    「范围已缩小」只对「试探过」那一支成立。顶层条目太多、跳过试探直接扫整棵树
    时（note 会解释这件事），顶着「范围已缩小」就和 note 自相矛盾——实测在平铺
    目录上就是这么显示的。
    """
    page.show_no_holder(names=["build"])
    assert "范围已缩小" not in page.stage_text(), page.stage_text()

    page.show_no_holder(note="源目录顶层有 8,000 个条目，已跳过这一步直接扫整棵树。")
    assert "范围已缩小" not in page.stage_text(), page.stage_text()
    assert "跳过" in page.note_text()


def test_no_holder_state_can_carry_a_custom_note(page):
    page.show_no_holder(note="扫描没做成：权限不足")
    assert "权限不足" in page.note_text()


def test_force_is_offered_only_after_a_graceful_failure(page):
    """强制结束是单独一步，只有优雅关闭失败之后才给。"""
    page.show_closed(True, "已请求占用程序关闭")
    assert page.can_force() is False

    page.show_closed(False, "有程序拒绝了关闭请求", can_force=True)
    assert page.can_force() is True
    assert page.force_button.isEnabled() is True


def test_closing_state_disables_everything(page):
    page.show_closing("正在请求占用程序关闭…")
    assert page.state() == CLOSING
    assert page.close_button.isEnabled() is False
    assert page.rescan_button.isEnabled() is False


def test_result_state_tells_whether_the_occupancy_is_gone(page):
    page.show_result(True, "占用已解除，源目录现在可以改名了。")
    assert page.state() == DONE
    assert "已解除" in page.verdict_text()
    assert page.can_force() is False

    page.show_result(False, "关闭了程序，但源目录仍然无法改名。")
    assert "仍然无法改名" in page.verdict_text()
    assert page.close_button.isVisibleTo(page) is True


def test_result_state_clears_every_trace_of_the_closing_step(page):
    """**实测踩到的坑**：结论出来了，界面却还卡在「正在强制结束…」。

    `show_result` 原来只设了一句结论，把关闭阶段留下的东西全留着了：进度条还在、
    阶段文字还写着「正在强制结束占用程序…」、而且「重新扫描」还是灰的——用户既看
    不出结果，也动不了。反馈很差。
    """
    page.show_holders([WPS, CODE], names=["node_modules"])
    page.show_closing("正在强制结束占用程序…")
    assert page.bar.isVisibleTo(page) is True
    assert page.rescan_button.isEnabled() is False

    page.show_result(True, "占用已解除，源目录现在可以改名了。")

    assert page.bar.isVisibleTo(page) is False, "进度条还在"
    assert "强制结束" not in page.stage_text(), page.stage_text()
    assert "已解除" in page.stage_text()
    assert page.rescan_button.isEnabled() is True, "重新扫描被留在禁用状态，用户动不了"
    assert page.probe_note.isVisibleTo(page) is False
    assert page.verdict_text().startswith("占用已解除")


def test_result_state_drops_the_processes_that_were_closed(page):
    """关掉的进程不该再列在表里——列着用户会以为还要再关一次。"""
    page.show_holders([WPS, CODE], names=["node_modules"])
    assert page.holder_rows()

    page.show_result(True, "占用已解除，源目录现在可以改名了。")

    assert page.holder_rows() == []
    assert page.note_text() == ""


def test_failed_result_keeps_the_table_but_leaves_no_dead_button(page):
    """真没解决时：进程清单要留着（还要接着处理），但按钮不能是残废的。"""
    page.show_holders([WPS], names=["node_modules"])
    page.show_closing("正在请求占用程序关闭…")

    page.show_result(False, "关闭了程序，但源目录仍然无法改名。")

    assert page.holder_rows() == [["WPS PDF", "12208", r"C:\WPS\wpspdf.exe",
                                   "可由本工具关闭"]]
    assert page.close_button.isVisibleTo(page) is True
    assert page.close_button.isEnabled() is True, "可见却点不动 = 死按钮"
    assert page.rescan_button.isEnabled() is True
    assert page.bar.isVisibleTo(page) is False


def test_closed_success_also_drops_the_dead_process_list(page):
    """成功这条路入口不止一个。

    `show_result` 会清表格，`show_closed` 原来不清——窗口目前只给 show_closed 传
    False 所以碰不到，但这是把同一个坑埋在另一个入口：迟早有人从这儿走一次，
    然后看到「占用已解除」底下还列着刚关掉的程序。
    """
    page.show_holders([WPS, CODE], names=["node_modules"])
    page.show_closing("正在请求占用程序关闭…")

    page.show_closed(True, "已强制结束占用程序")

    assert page.holder_rows() == []
    assert "已解除" in page.stage_text()


def test_closed_state_also_replaces_the_closing_text(page):
    """`show_closed` 同样不能把「正在…」留在阶段文字里。"""
    page.show_closing("正在请求占用程序关闭…")

    page.show_closed(False, "有的程序关掉了，有的没有关掉", can_force=True)

    assert "正在" not in page.stage_text(), page.stage_text()
    assert page.bar.isVisibleTo(page) is False
    assert page.rescan_button.isEnabled() is True


def test_holders_state_replaces_the_scanning_text_even_without_names(page):
    """`names` 为空时也要重设阶段文字。

    `names` 为空是可能的（试探没定位到具体条目、退回整棵树扫的情况），此时若不
    重设，「正在扫描占用：412/5000 个文件」这种中间态会一直留在卡片上——和用户
    实测碰到的「结论出来了界面还卡着正在关闭」是同一类毛病。
    """
    page.set_scan_progress(412, 5000)
    assert "正在扫描" in page.stage_text()

    page.show_holders([WPS], names=[])

    assert "正在扫描" not in page.stage_text(), page.stage_text()
    assert page.stage_text() == "扫描完成"


def test_holders_state_restores_the_warning_colour(page):
    """「不会关闭」那段警告不能沿用上一屏的灰色。

    `show_no_holder` 把 note 的底色改成 MUTED（那是说明文字），再回到「找到占用者」
    的状态时若不重设，警告会以灰色显示，看不出是警告。
    """
    page.show_no_holder(names=["build"])
    assert "工作目录" in page.note_text()

    page.show_holders([EXPLORER], names=["build"])

    assert "不会关闭" in page.note_text()
    assert page.note_color() == warning_color()


def test_scanning_state_does_not_leak_previous_holders(page):
    """重新扫描之前，上一轮的结果必须收干净——留着会让人以为已经解决了。"""
    page.show_holders([WPS], names=["node_modules"])
    assert page.holder_rows()

    page.set_scanning("正在缩小范围…")

    assert page.holder_rows() == []
    assert page.note_text() == ""
    assert page.verdict_text() == ""


def _all_texts(widget) -> list[str]:
    """递归收集一个页面里所有控件上的文字。"""
    from PySide6.QtWidgets import QLabel

    found = []
    for child in widget.findChildren(QLabel):
        if child.text():
            found.append(child.text())
    for child in widget.findChildren(qf.PushButton):
        if child.text():
            found.append(child.text())
    return found


def test_the_table_is_read_only(page):
    """占用者表是只读的。

    qf.TableWidget 默认 `DoubleClicked|EditKeyPressed|AnyKeyPressed`——敲任意
    一个键就进编辑态，格子变成一个带清空按钮的输入框。用户看到的是一张能改的
    表，而这里显示的是系统查出来的事实，不该能改。
    """
    page.show_holders([WPS], names=["node_modules"])

    assert page.table.editTriggers() == \
        qf.TableWidget.EditTrigger.NoEditTriggers


def test_no_markdown_leaks_into_any_state(page):
    """Qt 不渲染 Markdown，`**` 会原样显示成星号。

    这个错在本项目里已经犯过三次（计划页的映射说明、报告页的「目标端未删除」、
    这一页的说明），所以改成**逐个状态把控件文字全过一遍**，不再靠人眼看。
    """
    for build in (
        lambda: page.set_scanning("正在缩小范围…"),
        lambda: page.show_holders([WPS, EXPLORER], names=["node_modules"]),
        lambda: page.show_no_holder(names=["build"]),
        lambda: page.show_closing("正在请求占用程序关闭…"),
        lambda: page.show_closed(False, "有程序拒绝了关闭请求", can_force=True),
        lambda: page.show_result(True, "占用已解除，源目录现在可以改名了。"),
    ):
        build()
        for text in _all_texts(page):
            assert "**" not in text, f"文案里漏了 Markdown 记号：{text!r}"


