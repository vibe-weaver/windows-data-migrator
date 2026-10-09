"""向导第一步：选择源目录与目标目录，并实时给出前置校验结果。"""
from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton,
                               QVBoxLayout, QWidget)

from ..core import guard
from ..core.fsutil import drive_of, volume_free_bytes
from .format import human_bytes


class PathLineEdit(QLineEdit):
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
        self.source_browse = QPushButton("浏览…")
        self.target_browse = QPushButton("浏览…")
        self.source_free = QLabel("-")
        self.target_free = QLabel("-")
        self.issues_list = QListWidget()

        form = QFormLayout()
        form.addRow("源目录", self._row(self.source_edit, self.source_browse,
                                        self.source_free))
        form.addRow("目标目录", self._row(self.target_edit, self.target_browse,
                                          self.target_free))

        box = QGroupBox("前置校验")
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(self.issues_list)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(box, 1)

        self.source_browse.clicked.connect(lambda: self._browse(self.source_edit))
        self.target_browse.clicked.connect(lambda: self._browse(self.target_edit))
        for edit in (self.source_edit, self.target_edit):
            edit.textChanged.connect(self._on_changed)
            edit.dropped.connect(self._on_changed)

    @staticmethod
    def _row(edit: QLineEdit, browse: QPushButton, free: QLabel) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        row.addWidget(QLabel("剩余"))
        row.addWidget(free)
        return holder

    def _browse(self, edit: QLineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
        if chosen:
            edit.setText(chosen)

    def _on_changed(self, *_args) -> None:
        self.refresh_check()
        self.changed.emit()

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
            self.issues_list.addItem("请选择源目录与目标目录")
            return self._issues
        self._issues = guard.check(self.source(), self.target())
        for issue in self._issues:
            prefix = "错误" if issue.severity == "error" else "提示"
            self.issues_list.addItem(f"[{prefix}] {issue.message}")
        if not self._issues:
            self.issues_list.addItem("前置校验通过")
        return self._issues

    def issues(self) -> list:
        return list(self._issues)

    def issues_text(self) -> str:
        return "\n".join(self.issues_list.item(i).text()
                         for i in range(self.issues_list.count()))

    def has_blocking_error(self) -> bool:
        return guard.has_error(self._issues)
