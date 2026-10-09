"""向导第一步：选择源目录与目标目录，并实时给出前置校验结果。

外观全部交给 Fluent 控件与 `chrome` 外壳，本模块不写任何颜色字面量：
需要表达「校验通过」时用 `theme` 里的语义色，深浅两色成对给出。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QVBoxLayout,
                               QWidget)

import qfluentwidgets as qf

from ..core import guard
from ..core.fsutil import drive_of, volume_free_bytes
from . import chrome
from .format import human_bytes
from .theme import SUCCESS

# 校验区在「没有问题」时的说明文案。它只影响这一行提示，
# 不影响 issues_list 的内容与 issues() 的返回值。
EMPTY_HINT = "尚未选择源目录时，这里会列出前置校验的结果"
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
    """该路径所在卷的剩余空间；取不到返回 "-"。"""
    if not path:
        return "-"
    try:
        drive = drive_of(path)
        root = drive + "\\" if drive else path
        return human_bytes(volume_free_bytes(root))
    except OSError:
        return "-"


class SelectPage(QWidget):
    """① 选择页。"""

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

        # 空状态说明行：一条中性提示 + 一条成功提示，二者互斥显示。
        # 分成两个控件是为了不让成功语义色串到中性文案上
        # （FluentLabelBase.setTextColor 一旦调用就再也回不到主题默认色）。
        self._check_hint = qf.CaptionLabel(EMPTY_HINT)
        self._check_pass = qf.CaptionLabel(PASS_HINT)
        self._check_pass.setTextColor(*SUCCESS)
        self._check_pass.hide()

        source_card = self._path_card("源目录", self.source_edit,
                                      self.source_browse, self.source_free)
        target_card = self._path_card("目标目录", self.target_edit,
                                      self.target_browse, self.target_free)

        check_card = chrome.Card()
        check_layout = QVBoxLayout(check_card)
        check_layout.setContentsMargins(16, 14, 16, 14)
        check_layout.setSpacing(8)
        check_layout.addWidget(qf.StrongBodyLabel("前置校验"))
        check_layout.addWidget(self._check_hint)
        check_layout.addWidget(self._check_pass)
        check_layout.addWidget(self.issues_list)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
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
    def _path_card(title: str, edit: qf.LineEdit, browse: qf.PushButton,
                   free: QLabel) -> QWidget:
        """一个路径行卡片：小标题 + 输入框 + 浏览按钮 + 剩余空间。"""
        card = chrome.Card()
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(6)
        box.addWidget(qf.CaptionLabel(title))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        row.addWidget(qf.CaptionLabel("剩余"), 0, Qt.AlignVCenter)
        row.addWidget(free, 0, Qt.AlignVCenter)
        box.addLayout(row)
        return card

    def _browse(self, edit: qf.LineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
        if chosen:
            edit.setText(chosen)

    def _on_changed(self, *_args) -> None:
        self.refresh_check()
        self.changed.emit()

    HINT_EMPTY, HINT_PASS, HINT_ISSUES = "empty", "pass", "issues"

    def _set_hint(self, state: str) -> None:
        """切换校验区的状态说明行。

        三种状态互斥：未选目录给中性指引，校验通过给一行绿色结论，
        存在问题时两条都藏起来，把版面让给 `issues_list`。

        列表只在**真有问题**时才显示：无问题时它那一项只是同一句话的复述
        （列表内容必须保留，`issues_text()` 依赖它），显示出来就是重复。
        """
        self._check_hint.setVisible(state == self.HINT_EMPTY)
        self._check_pass.setVisible(state == self.HINT_PASS)
        self.issues_list.setVisible(state == self.HINT_ISSUES)

    # --- 对外接口 ---------------------------------------------------
    def source(self) -> str:
        return self.source_edit.text().strip()

    def target(self) -> str:
        return self.target_edit.text().strip()

    def set_source(self, path) -> None:
        self.source_edit.setText(str(path))

    def set_target(self, path) -> None:
        self.target_edit.setText(str(path))

    def refresh_check(self) -> list:
        self.source_free.setText(free_space_text(self.source()))
        self.target_free.setText(free_space_text(self.target()))
        self.issues_list.clear()
        if not self.source() or not self.target():
            self._issues = []
            self._set_hint(self.HINT_EMPTY)
            self.issues_list.addItem("请选择源目录与目标目录")
            return self._issues
        self._issues = guard.check(self.source(), self.target())
        for issue in self._issues:
            prefix = "错误" if issue.severity == "error" else "提示"
            self.issues_list.addItem(f"[{prefix}] {issue.message}")
        if not self._issues:
            # 空状态不留一大片空白：成功提示 + 一行明确结论。
            self._set_hint(self.HINT_PASS)
            self.issues_list.addItem("前置校验通过")
        else:
            self._set_hint(self.HINT_ISSUES)
        return self._issues

    def issues(self) -> list:
        return list(self._issues)

    def issues_text(self) -> str:
        return "\n".join(self.issues_list.item(i).text()
                         for i in range(self.issues_list.count()))

    def has_blocking_error(self) -> bool:
        return guard.has_error(self._issues)
