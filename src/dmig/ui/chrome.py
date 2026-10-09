"""向导外壳：品牌栏、步骤条、指标行、操作栏、卡片容器。

这里只用 qfluentwidgets 的控件与语义色，**不写任何硬编码颜色**——
写死颜色在深色模式下必然失效（spike 实测文字会完全看不见）。
项目自有的颜色统一从 `theme` 导入，本模块一个颜色字面量都不出现。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QWidget)

import qfluentwidgets as qf

from .theme import ACCENT, ACCENT_DARK, MUTED, SUCCESS

APP_NAME = "数据目录迁移工具"
APP_SUBTITLE = "把占空间的数据目录搬到别的盘，原路径照常可用"
APP_VERSION = "1.0.0"

DONE, CURRENT, TODO = "done", "current", "todo"


def _icon() -> QIcon:
    """优先用打包进程序的图标；取不到就给个空图标，不能让构造失败。"""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), "packaging", "app.ico"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return QIcon(path)
    return QIcon()


class Card(qf.SimpleCardWidget):
    """统一圆角与内边距的卡片容器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dmigCard")


class BrandBar(QWidget):
    """顶部品牌栏：图标 + 应用名 + 一句话说明 + 版本徽标。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(12)

        logo = qf.FluentIcon.APPLICATION.icon() if hasattr(
            qf.FluentIcon, "APPLICATION") else None
        icon_label = qf.BodyLabel()
        if logo is not None:
            icon_label.setPixmap(logo.pixmap(28, 28))
        else:
            icon_label.setPixmap(_icon().pixmap(28, 28))
        row.addWidget(icon_label)

        text_box = QVBoxLayout()
        text_box.setSpacing(0)
        self._title = qf.SubtitleLabel(APP_NAME)
        self._subtitle = qf.CaptionLabel(APP_SUBTITLE)
        text_box.addWidget(self._title)
        text_box.addWidget(self._subtitle)
        row.addLayout(text_box)
        row.addStretch(1)

        version = qf.CaptionLabel(f"v{APP_VERSION}")
        row.addWidget(version, 0, Qt.AlignVCenter)

    def title_text(self) -> str:
        return self._title.text()

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)


class StepBar(QWidget):
    """四步向导的进度指示。只表达状态，不处理点击。"""

    def __init__(self, steps, parent=None):
        super().__init__(parent)
        self._steps = list(steps)
        self._index = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 10, 24, 10)
        row.setSpacing(8)

        self._labels: list[qf.StrongBodyLabel] = []
        for i, name in enumerate(self._steps):
            label = qf.StrongBodyLabel(name)
            self._labels.append(label)
            row.addWidget(label)
            if i < len(self._steps) - 1:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setMinimumWidth(24)
                row.addWidget(line, 1)
        self._refresh()

    def set_current(self, index: int) -> None:
        if not self._steps:
            return
        self._index = max(0, min(int(index), len(self._steps) - 1))
        self._refresh()

    def current(self) -> int:
        return self._index

    def states(self) -> list[str]:
        out = []
        for i in range(len(self._steps)):
            if i < self._index:
                out.append(DONE)
            elif i == self._index:
                out.append(CURRENT)
            else:
                out.append(TODO)
        return out

    def _refresh(self) -> None:
        for i, label in enumerate(self._labels):
            state = self.states()[i]
            prefix = "✓ " if state == DONE else f"{i + 1}  "
            label.setText(prefix + self._steps[i])
            # 颜色全部来自 theme，且每个颜色都给了浅/深两个值
            if state == CURRENT:
                label.setTextColor(ACCENT, ACCENT_DARK)
            elif state == DONE:
                label.setTextColor(*SUCCESS)
            else:
                label.setTextColor(*MUTED)


class MetricRow(QWidget):
    """一排「大数字 + 小标签」的指标。"""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._values: dict[str, qf.TitleLabel] = {}
        for key, value in items:
            box = QVBoxLayout()
            box.setSpacing(2)
            big = qf.TitleLabel(value)
            caption = qf.CaptionLabel(key)
            box.addWidget(big)
            box.addWidget(caption)
            row.addLayout(box, 1)
            self._values[key] = big

    def value_text(self, key: str) -> str:
        label = self._values.get(key)
        return label.text() if label is not None else ""

    def set_value(self, key: str, text: str) -> None:
        label = self._values.get(key)
        if label is not None:
            label.setText(text)


class ActionBar(QWidget):
    """底部操作栏：左侧放次要动作，右侧放主要动作。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(24, 10, 24, 14)
        self._row.setSpacing(10)
        self._row.addStretch(1)

    def add_left(self, widget) -> None:
        self._row.insertWidget(0, widget)

    def add_right(self, widget) -> None:
        self._row.addWidget(widget)
