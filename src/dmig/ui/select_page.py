"""向导第一步：选择源目录与目标目录，并实时给出前置校验结果。

外观全部交给 Fluent 控件与 `chrome` 外壳，本模块不写任何颜色字面量：
需要表达「校验通过」时用 `theme` 里的语义色，深浅两色成对给出。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel,
                               QListWidgetItem, QVBoxLayout, QWidget)

import qfluentwidgets as qf

from ..core import guard
from ..core.fsutil import cached_free_bytes
from . import chrome
from .format import human_bytes
from .theme import MUTED, SUCCESS, error_color, warning_color

# 校验区在「没有问题」时的说明文案。它只影响这一行提示，
# 不影响 issues_list 的内容与 issues() 的返回值。
EMPTY_HINT = "尚未选择源目录时，这里会列出前置校验的结果"
# 选好了目录但还没检验：不给任何结论，只指路。原来这一刻会直接显示
# 「前置校验通过」——而那一遍里没有试探改名，等于给了一份不完整的保证。
UNCHECKED_HINT = ("点右侧「开始检验」，检查前置条件并试探源目录能否改名"
                  "（迁移的最后一步就是改名，这一项最容易让整趟白跑）")
PASS_HINT = "前置校验通过，可以继续下一步"


class PathLineEdit(qf.LineEdit):
    """可以拖入目录的路径输入框。"""

    dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText("拖入目录，或点右侧「浏览…」")

    def dragEnterEvent(self, event):        # noqa: N802 - Qt 命名约定
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):         # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):             # noqa: N802
        urls = event.mimeData().urls()
        if not urls:
            return
        # Qt6 的 QUrl.toLocalFile() 在 Windows 上返回的是 "C:/a/b" 形式，
        # 必须转成原生反斜杠路径，否则与浏览按钮、guard.check 拿到的路径形式不一致。
        local = urls[0].toLocalFile()
        path = os.path.normpath(local) if local else ""
        if path:
            self.setText(path)
            self.dropped.emit(path)
        event.acceptProposedAction()


def free_space_text(path: str) -> str:
    """该路径所在卷的剩余空间；取不到返回 "-"。

    走 cached_free_bytes：按卷根缓存两秒，避免每敲一个字符都去问一次文件系统
    （网络盘或断开的映射盘上那一下会阻塞到超时）。
    """
    if not path:
        return "-"
    free = cached_free_bytes(path)
    return human_bytes(free) if free >= 0 else "-"


class SelectPage(QWidget):
    """① 选择页。"""

    # 用户点了「开始检验」：由窗口去跑（页面只管发出意图）
    check_requested = Signal()

    # 改名被占用挡住时的专用通道：跳到「找出占用进程」那一页
    holders_requested = Signal()

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._issues: list[guard.Issue] = []

        self.source_edit = PathLineEdit()
        self.target_edit = PathLineEdit()
        self.source_browse = qf.PushButton("浏览…")
        self.target_browse = qf.PushButton("浏览…")
        self.source_free = qf.StrongBodyLabel("-")
        self.target_free = qf.StrongBodyLabel("-")
        self.issues_list = qf.ListWidget()
        # 上次检验时的 (源, 目标)。路径一变就作废——旧结论说的是另一条路径。
        self._checked_pair = None

        # 空状态说明行：一条中性提示 + 一条成功提示，二者互斥显示。
        # 分成两个控件是为了不让成功语义色串到中性文案上
        # （FluentLabelBase.setTextColor 一旦调用就再也回不到主题默认色）。
        self._check_hint = qf.CaptionLabel(EMPTY_HINT)
        self._check_todo = qf.CaptionLabel(UNCHECKED_HINT)
        self._check_todo.hide()
        self._check_pass = qf.CaptionLabel(PASS_HINT)
        self._check_pass.setTextColor(*SUCCESS)
        # 成功提示前加一颗成功色圆点，比一行绿字更有「结论」的样子
        self._check_pass_dot = qf.CaptionLabel("●")
        self._check_pass_dot.setStyleSheet("font-size: 8px;")
        self._check_pass_dot.setTextColor(*SUCCESS)
        self._check_pass_row = QWidget()
        pass_row = QHBoxLayout(self._check_pass_row)
        pass_row.setContentsMargins(0, 0, 0, 0)
        pass_row.setSpacing(6)
        pass_row.addWidget(self._check_pass_dot)
        pass_row.addWidget(self._check_pass)
        pass_row.addStretch(1)
        self._check_pass_row.hide()

        source_card = self._path_card("源目录", chrome.fluent_icon("FOLDER"),
                                      self.source_edit, self.source_browse,
                                      self.source_free)
        target_card = self._path_card("目标目录", chrome.fluent_icon("FOLDER"),
                                      self.target_edit, self.target_browse,
                                      self.target_free)

        check_card = chrome.Card()
        check_layout = QVBoxLayout(check_card)
        check_layout.setContentsMargins(20, 14, 20, 16)
        check_layout.setSpacing(10)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        header.addWidget(chrome.make_section_header(
            "前置校验", chrome.fluent_icon("INFO")))
        header.addStretch(1)
        self.check_button = qf.PushButton("开始检验")
        self.check_button.setMinimumHeight(32)
        # 试探有副作用，必须说在前面：点一下源路径会消失几毫秒
        self.check_button.setToolTip(
            "检查前置条件，并试探源目录能否改名（迁移的最后一步就是改名）。\n"
            "试探会把源目录改名再立刻改回，因此它会短暂消失几毫秒——\n"
            "正开着这个目录的程序可能提示「文件夹已删除」，属正常现象。")
        self.check_button.clicked.connect(self.check_requested.emit)
        header.addWidget(self.check_button)
        # 改名被挡住的专用通道：只有检验失败于 rename_blocked 时才露出来。
        # 别的失败（源不存在、空间不足……）用不到这一页。
        self.holders_button = qf.PushButton("找出占用进程 →")
        self.holders_button.setMinimumHeight(32)
        self.holders_button.clicked.connect(self.holders_requested.emit)
        self.holders_button.hide()
        header.addWidget(self.holders_button)
        check_layout.addLayout(header)
        check_layout.addWidget(self._check_hint)
        check_layout.addWidget(self._check_todo)
        check_layout.addWidget(self._check_pass_row)
        check_layout.addWidget(self.issues_list)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(14)
        layout.addWidget(source_card)
        layout.addWidget(target_card)
        # 校验卡片按内容自适应高度，剩余空间留给末尾的 stretch——
        # 给它 stretch 会让卡片撑满整屏、把提示行推到卡片底部，中间空一大片。
        layout.addWidget(check_card)
        layout.addStretch(1)

        self.source_browse.clicked.connect(lambda: self._browse(self.source_edit))
        self.target_browse.clicked.connect(lambda: self._browse(self.target_edit))
        for edit in (self.source_edit, self.target_edit):
            edit.textChanged.connect(self._on_changed)
            edit.dropped.connect(self._on_changed)

        self.refresh_check()

    @staticmethod
    def _path_card(title: str, icon: QIcon, edit: qf.LineEdit,
                   browse: qf.PushButton, free: QLabel) -> QWidget:
        """一个路径行卡片：图标标题 +（右侧剩余空间）+ 输入框 + 浏览按钮。

        剩余空间从输入行右端挪到标题行右侧：输入框独占一行更宽、更不容易
        被「剩余」两个字挤得局促；标题右侧的「剩余 xx」也顺理成章成为
        卡片右上角的度量。
        """
        card = chrome.Card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 14, 20, 16)
        box.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)
        header_row.addWidget(chrome.make_section_header(title, icon), 1)
        free_box = QHBoxLayout()
        free_box.setSpacing(6)
        cap = qf.CaptionLabel("剩余")
        cap.setTextColor(*MUTED)
        free_box.addWidget(cap)
        free_box.addWidget(free)
        header_row.addLayout(free_box, 0)
        box.addLayout(header_row)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        # 浏览按钮做扎实一点，避免和输入框挤在一起显得局促
        browse.setMinimumWidth(96)
        browse.setMinimumHeight(34)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        box.addLayout(row)
        return card

    def _browse(self, edit: qf.LineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
        if chosen:
            edit.setText(chosen)

    def _on_changed(self, *_args) -> None:
        """路径变了：只刷新剩余空间并把上一次的检验结论作废。

        **这里不跑校验。** 原来顺手跑一遍前置校验，用户还没点「开始检验」就看到
        一行绿色的「前置校验通过」——而那一遍里没有试探改名，没覆盖最容易让整趟
        迁移白跑的风险（复制校验全过、卡在最后一步）。结论要么不给，要么给完整的。
        """
        self._invalidate_check()
        self.refresh_free_space()
        self.changed.emit()

    def _invalidate_check(self) -> None:
        """把上一次的检验结论作废。

        换成另一条路径之后，旧结论说的是别人——留着它等于给出一份无效的保证。
        """
        self._issues = []
        self._checked_pair = None
        self.holders_button.hide()
        if self.source() and self.target():
            self._set_hint(self.HINT_UNCHECKED)
            self._show_guidance(UNCHECKED_HINT)
        else:
            self._set_hint(self.HINT_EMPTY)
            self._show_guidance("请选择源目录与目标目录")

    def refresh_free_space(self) -> None:
        """只刷新两个「剩余 xx」。路径一变就该更新，与校验无关。"""
        self.source_free.setText(free_space_text(self.source()))
        self.target_free.setText(free_space_text(self.target()))

    def is_checked(self) -> bool:
        """当前这对路径是否已经检验过（且此后没被改动）。"""
        return self._checked_pair == (self.source(), self.target())

    HINT_EMPTY, HINT_UNCHECKED, HINT_PASS, HINT_ISSUES = (
        "empty", "unchecked", "pass", "issues")

    def _set_hint(self, state: str) -> None:
        """切换校验区的状态说明行。

        四种状态互斥：未选目录 / 已选但未检验（都给中性指引，只是措辞不同）、
        检验通过给一行绿色结论、存在问题时两条中性行和结论行都藏起来，
        把版面让给 `issues_list`。

        列表只在**真有问题**时才显示：无问题时它那一项只是同一句话的复述
        （列表内容必须保留，`issues_text()` 依赖它），显示出来就是重复。
        """
        self._check_hint.setVisible(state == self.HINT_EMPTY)
        self._check_todo.setVisible(state == self.HINT_UNCHECKED)
        self._check_pass_row.setVisible(state == self.HINT_PASS)
        self.issues_list.setVisible(state == self.HINT_ISSUES)

    def _show_guidance(self, text: str) -> None:
        """没有结论可给时，把 `issues_text()` 也填上一句指引。

        列表此时是藏起来的（`_set_hint(HINT_EMPTY/UNCHECKED)`），但
        `issues_text()` 是给测试与调试读的读数口，空着会让「什么都没做」和
        「读了但没读到」分不开。
        """
        self.issues_list.clear()
        self.issues_list.addItem(text)

    # --- 对外接口 ---------------------------------------------------
    def source(self) -> str:
        return self.source_edit.text().strip()

    def target(self) -> str:
        return self.target_edit.text().strip()

    def set_source(self, path) -> None:
        self.source_edit.setText(str(path))
        self.source_edit.setCursorPosition(0)

    def set_target(self, path) -> None:
        self.target_edit.setText(str(path))
        self.target_edit.setCursorPosition(0)

    def refresh_check(self) -> list:
        """跑**无副作用**的那些前置检查（试探改名在 `run_probe` 里）。

        这个方法不再被自动调用——校验只由「开始检验」触发。留在对外接口上，
        是因为窗口需要在检验之外单独复算一遍（例如换过目标路径之后）。
        """
        self.refresh_free_space()
        if not self.source() or not self.target():
            self._issues = []
            self._checked_pair = None
            self.holders_button.hide()
            self._set_hint(self.HINT_EMPTY)
            self._show_guidance("请选择源目录与目标目录")
            return self._issues
        self._issues = guard.check(self.source(), self.target())
        self._render_issues()
        return self._issues

    def run_probe(self) -> list:
        """用户点了「开始检验」：前置校验 + 试探改名。

        试探失败是 error 级（红色），会拦住下一步——因为改名是迁移的**最后一步**，
        失败时用户已经等了几个小时，而它本可以在几毫秒内就查出来。
        """
        issues = self.refresh_check()
        if not self.source() or not self.target():
            return issues
        if not guard.has_error(issues):
            # 前置校验已经拦住了，再试探没有意义（源可能根本不是目录），
            # 顺带也避免了在一个注定失败的选择上触发试探的副作用。
            issues.extend(guard.probe_rename(self.source()))
            self._issues = issues
            self._render_issues()
        self._checked_pair = (self.source(), self.target())
        return self._issues

    def _render_issues(self) -> None:
        """按严重程度把问题列表渲染出来：红色 = 错误（拦），黄色 = 警告（不拦）。"""
        self.issues_list.clear()
        if not self._issues:
            self._set_hint(self.HINT_PASS)
            self.issues_list.addItem("前置校验通过")
            self.holders_button.hide()
            return
        self._set_hint(self.HINT_ISSUES)
        for issue in self._issues:
            error = issue.severity == "error"
            prefix = "错误" if error else "警告"
            item = QListWidgetItem(f"[{prefix}] {issue.message}")
            item.setForeground(QBrush(QColor(
                error_color() if error else warning_color())))
            self.issues_list.addItem(item)
        # 只有「改名被挡住」这一条才给跳转通道：别的错误（源不存在、空间不足）
        # 用那一页解决不了。
        self.holders_button.setVisible(
            any(issue.code.startswith("rename_") for issue in self._issues))

    def issues(self) -> list:
        return list(self._issues)

    def issues_text(self) -> str:
        return "\n".join(self.issues_list.item(i).text()
                         for i in range(self.issues_list.count()))

    def issue_colors(self) -> list[str]:
        """每一行问题的字色（十六进制）。供测试断言「错误红 / 提醒黄」。"""
        colors = []
        for i in range(self.issues_list.count()):
            brush = self.issues_list.item(i).foreground()
            colors.append(QColor(brush.color()).name())
        return colors

    def has_blocking_error(self) -> bool:
        return guard.has_error(self._issues)





