"""向导外壳：品牌栏、步骤条、指标行、操作栏、卡片容器。

设计原则：
- 视觉令牌全部来自 `theme`（每个颜色都给浅/深两个值），**不出现任何硬编码颜色**，
  否则深色模式下必然出现「某个控件文字变白、背景还是白的」。
- 需要自定义绘制（步骤条圆点、分隔线、徽标）的控件，颜色在 `paintEvent` 里经
  `theme.pick()` 按当前主题取；并监听库的主题变更信号触发重绘，主题切换时即时刷新。
- 专业外观的关键是「层级」与「节奏」：发丝分隔线区隔相邻区块、图标芯片标识分区、
  数字与说明文字分明、留白统一。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QRectF, QPoint
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                               QVBoxLayout, QWidget)

import qfluentwidgets as qf

from . import logo, theme
from .theme import (ACCENT, ACCENT_DARK, ACCENT_TINT, BORDER, BORDER_STRONG,
                    ERROR, MUTED, ON_ACCENT, SUCCESS, TEXT, WARNING)

APP_NAME = logo.NAME
APP_SUBTITLE = logo.TAGLINE
APP_VERSION = "1.0.0"

DONE, CURRENT, TODO = "done", "current", "todo"

_STEP_CIRCLE = 24          # 步骤圆点直径
_STEP_GAP = 10             # 圆点与文字之间的间距
# 行高必须装下：顶距 8 + 圆点半径 12 + 光环 4 + 间距 10 + 文字 18 + 底距 8 ≈ 64。
# 之前 54px 装不下，文字下半截被裁、贴着圆点，看起来就是圆点与文字重叠。
_STEP_ROW_H = 66           # 步骤条行高（圆点 + 文字 + 上下留白）


def _on_theme_change(widget, callback):
    """主题或主题色变化时回调一次。API 在不同库版本里命名可能不同，静默兜底。"""
    try:
        qf.qconfig.themeChanged.connect(callback)
    except Exception:
        pass
    try:
        qf.qconfig.themeColorChanged.connect(callback)
    except Exception:
        pass


def pick_color(pair: tuple[str, str]) -> QColor:
    return QColor(theme.pick(pair))


def fluent_icon(name: str) -> QIcon:
    """按名字取 FluentIcon，取不到（库版本差异）就返回空图标，不让构造失败。"""
    member = getattr(qf.FluentIcon, name, None)
    try:
        return member.icon() if member is not None else QIcon()
    except Exception:
        return QIcon()


# 结果徽章的图标名。**不要凭印象写名字**：这个库的 174 个图标里没有
# WARNING/ALERT/ERROR 这类警示三角，写错的后果是 `fluent_icon()` 静默返回
# 空图标——红色徽章里空空如也、分区标题也少一个芯片，只有盯着渲染图才看得出来。
# 改这两个常量（或任何 fluent_icon 的入参）之前先跑 tests/ui/test_icons.py，
# 它会扫描整个 ui 包，把不存在的图标名直接报出来。
OK_ICON = "ACCEPT"
FAIL_ICON = "CANCEL_MEDIUM"


def tint_pixmap(source, color: QColor):
    """把单色图标的像素整体染成指定颜色（保留透明度）。

    FluentIcon 的默认色随主题，做「强调底 + 白图标」这类品牌化徽标时
    需要把它染成固定颜色。取不到 pixmap 时原样返回，不让调用方崩溃。
    """
    from PySide6.QtGui import QPixmap, QPainter
    if source is None or source.isNull():
        return source
    pm = QPixmap(source.size())
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.drawPixmap(0, 0, source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pm.rect(), color)
    painter.end()
    return pm


class Card(qf.SimpleCardWidget):
    """统一圆角与内边距的卡片容器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dmigCard")


class Rule(QWidget):
    """发丝分隔线。orientation 决定水平/垂直；颜色按主题走，主题切换时重绘。"""

    def __init__(self, orientation=Qt.Orientation.Horizontal,
                 strong: bool = False, parent=None):
        super().__init__(parent)
        self._orientation = orientation
        self._strong = strong
        if orientation == Qt.Orientation.Horizontal:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        _on_theme_change(self, self.update)

    def paintEvent(self, _event) -> None:       # noqa: N802 - Qt 命名
        pair = BORDER_STRONG if self._strong else BORDER
        color = pick_color(pair)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(color, 1))
        if self._orientation == Qt.Orientation.Horizontal:
            painter.drawLine(0, 0, self.width(), 0)
        else:
            painter.drawLine(0, 0, 0, self.height())


class IconChip(QWidget):
    """图标芯片：圆角方块底（强调色淡底）+ 图标（强调色）。

    用于卡片标题左侧，把一个区块用图标锚定，比纯文字标题更有「专业版」的视觉锚点。
    """

    def __init__(self, icon: QIcon, size: int = 18, tile: int = 28,
                 parent=None):
        super().__init__(parent)
        self._tile = tile
        self._icon = icon
        self._icon_size = size
        self.setFixedSize(tile, tile)
        _on_theme_change(self, self.update)

    def set_icon(self, icon: QIcon) -> None:
        self._icon = icon
        self.update()

    def paintEvent(self, _event) -> None:        # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0, 0, self._tile, self._tile)
        painter.setBrush(pick_color(ACCENT_TINT))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 8, 8)
        target = max(self._icon_size, 16)
        pm = self._icon.pixmap(target, target)
        painter.drawPixmap((self._tile - target) // 2,
                           (self._tile - target) // 2, pm)


def make_section_header(title: str, icon: Optional[QIcon] = None,
                        *, tile: int = 28) -> QWidget:
    """卡片标题行：图标芯片 + 标题。返回一个可 addWidget 的复合控件。

    页面里到处是「StrongBodyLabel 标题」独占一行，没有视觉锚点；换成芯片 + 标题
    后，分区一眼可辨。无图标时退化为带左侧内边距的标题，保持对齐基线一致。
    """
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    if icon is not None and not icon.isNull():
        chip = IconChip(icon, size=16, tile=tile)
        layout.addWidget(chip)
    else:
        layout.addSpacing(tile)  # 没图标也留出同样的左侧缩进，标题对齐
    label = qf.StrongBodyLabel(title)
    layout.addWidget(label)
    layout.addStretch(1)
    return row


class BrandBar(QWidget):
    """顶部品牌栏：品牌标记 + 应用名 / 副标题 + 版本胶囊 + 底部发丝线。"""

    # 标记大小。logo 自带蓝底白标，不需要再垫一层强调色方块、也不用染色。
    MARK = 38

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mark = logo.mark_pixmap(self.MARK)
        self.setFixedHeight(64)

    def paintEvent(self, _event) -> None:        # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 品牌标记：直接用 logo 本体（蓝靛渐变底 + 白色方框与链接角标），
        # 与 exe 图标、任务栏图标是同一套几何，一眼能认出是同一个东西。
        badge = self.MARK
        bx, by = 24, (self.height() - badge) // 2
        painter.drawPixmap(bx, by, self._mark)

        # 应用名（16px 半粗）
        painter.setPen(pick_color(TEXT))
        name_font = QFont(painter.font())
        name_font.setPixelSize(16)
        name_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(name_font)
        text_x = bx + badge + 12
        painter.drawText(text_x, by + 18, APP_NAME)

        # 副标题
        sub_font = QFont(painter.font())
        sub_font.setPixelSize(12)
        painter.setFont(sub_font)
        painter.setPen(pick_color(MUTED))
        painter.drawText(text_x, by + 18 + 18, APP_SUBTITLE)

        # 版本胶囊：右侧细描边 + 小字
        pill_w, pill_h = 56, 22
        px = self.width() - 24 - pill_w
        py = (self.height() - pill_h) // 2
        pill = QRectF(px, py, pill_w, pill_h)
        pen = QPen(pick_color(BORDER_STRONG), 1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(pill, pill_h / 2, pill_h / 2)
        vfont = QFont(painter.font())
        vfont.setPixelSize(11)
        painter.setFont(vfont)
        painter.setPen(pick_color(MUTED))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, f"v{APP_VERSION}")

        # 底部发丝线，与步骤条之间划一道
        painter.setPen(QPen(pick_color(BORDER), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def title_text(self) -> str:
        return APP_NAME

    def set_subtitle(self, text: str) -> None:
        # 副标题当前画在 paintEvent 里；保留接口避免调用方报错
        pass


class StepBar(QWidget):
    """四步向导的进度指示：圆点 + 连接轨道 + 文字。只表达状态，不处理点击。

    这是「专业版」观感最关键的一处：原来只是 `1 选择目录 —— 2 确认计划` 的
    纯文字加横线，看起来像草稿。换成带轨道的圆点步骤条后，进度感与状态层级
    一目了然。颜色全部经 `theme.pick()` 按主题取，并随主题切换重绘。
    """

    def __init__(self, steps, parent=None):
        super().__init__(parent)
        self._steps = list(steps)
        self._index = 0
        self.setFixedHeight(_STEP_ROW_H)
        _on_theme_change(self, self.update)

    def set_current(self, index: int) -> None:
        if not self._steps:
            return
        self._index = max(0, min(int(index), len(self._steps) - 1))
        self.update()

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

    def _centers(self) -> list[int]:
        n = len(self._steps)
        if n == 0:
            return []
        left = 24 + _STEP_CIRCLE // 2
        right = self.width() - 24 - _STEP_CIRCLE // 2
        if n == 1:
            return [(left + right) // 2]
        span = right - left
        return [int(left + i * span / (n - 1)) for i in range(n)]

    def paintEvent(self, _event) -> None:       # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        centers = self._centers()
        states = self.states()
        cy = _STEP_CIRCLE // 2 + 8  # 圆点中心 Y（留出顶部内边距，给光环留空间）

        # 轨道：相邻圆点之间一段，已走过的段涂强调色，未走过的段涂发丝色。
        # 强调色轨道是「进度感」的来源——淡底色在浅灰窗口上几乎看不见。
        for i in range(len(centers) - 1):
            x1 = centers[i] + _STEP_CIRCLE // 2 - 1
            x2 = centers[i + 1] - _STEP_CIRCLE // 2 + 1
            done_seg = states[i] in (DONE, CURRENT)
            color = pick_color((ACCENT, ACCENT_DARK)) if done_seg \
                else pick_color(BORDER)
            painter.setPen(QPen(color, 2, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(x1, cy, x2, cy)

        # 圆点与文字
        name_font = QFont(painter.font())
        name_font.setPixelSize(13)
        for i, cx in enumerate(centers):
            state = states[i]
            rect = QRectF(cx - _STEP_CIRCLE // 2, cy - _STEP_CIRCLE // 2,
                          _STEP_CIRCLE, _STEP_CIRCLE)
            if state == DONE:
                painter.setBrush(pick_color(SUCCESS))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(rect)
                self._draw_check(painter, cx, cy)
            elif state == CURRENT:
                # 光环：当前步骤外圈一层强调淡底，把「停在这里」衬出来
                halo = rect.adjusted(-4, -4, 4, 4)
                painter.setBrush(pick_color(ACCENT_TINT))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(halo)
                painter.setBrush(pick_color((ACCENT, ACCENT_DARK)))
                painter.drawEllipse(rect)
                painter.setPen(QColor(theme.pick(ON_ACCENT)))
                f = QFont(name_font)
                f.setWeight(QFont.Weight.DemiBold)
                painter.setFont(f)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(i + 1))
            else:
                # 未完成：极淡的底 + 描边，压得过当前步的光环
                painter.setBrush(QColor(theme.pick((BORDER[0], "transparent"))))
                painter.setPen(QPen(pick_color(BORDER_STRONG), 1.5))
                painter.drawEllipse(rect)
                painter.setPen(pick_color(MUTED))
                painter.setFont(name_font)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(i + 1))

            # 文字在圆点下方，矩形夹在窗口内：首尾步骤的圆点靠近边缘，
            # 不夹住的话文字会伸出窗口被裁掉半个字
            ty = cy + _STEP_CIRCLE // 2 + _STEP_GAP
            painter.setFont(name_font)
            if state == CURRENT:
                painter.setPen(pick_color((ACCENT, ACCENT_DARK)))
                f = QFont(name_font)
                f.setWeight(QFont.Weight.DemiBold)
                painter.setFont(f)
            elif state == DONE:
                painter.setPen(pick_color(TEXT))
            else:
                painter.setPen(pick_color(MUTED))
            label_w = 120
            lx = max(4, min(cx - label_w // 2, self.width() - label_w - 4))
            painter.drawText(QRectF(lx, ty, label_w, 18),
                             Qt.AlignmentFlag.AlignCenter, self._steps[i])

    @staticmethod
    def _draw_check(painter: QPainter, cx: int, cy: int) -> None:
        path = QPainterPath()
        path.moveTo(cx - 5, cy)
        path.lineTo(cx - 1, cy + 4)
        path.lineTo(cx + 6, cy - 5)
        pen = QPen(QColor(theme.pick(ON_ACCENT)), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)


class MetricRow(QWidget):
    """一排「说明在上、大数字在下」的指标，格之间用竖发丝线区隔。

    值不一定都是数字：例如「被占用」的值是一整句话「执行时检测」。
    这类长文本若仍用大字号，会把整行挤到相邻两格贴在一起。

    两个关键点：
    1. 判断依据是**估算显示宽度**而不是字符数——中文单字宽度约是 ASCII 的两倍，
       「2.2 KB」有 6 个字符却很短，「执行时检测」只有 5 个字符却很宽。
    2. 字号必须在**每次设值时**重新判定，不能只在构造时定死：
       页面是先建好指标行、再在 `load()` 里填真值的，构造时拿到的只是占位符。

    说明文字放上面、数字放下面是「专业版」仪表盘的标准层级：扫一眼先看到标签，
    再往下读到数值；原来的「数字在上、标签在下」反着来，读起来要先猜标签。
    """

    # 估算显示宽度超过这个值就降一档字号
    LONG_VALUE_WIDTH = 8
    VALUE_PX = 24
    VALUE_PX_WIDE = 14

    @staticmethod
    def display_width(text: str) -> int:
        """粗略估算显示宽度：CJK 及全角字符记 2，其余记 1。"""
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))

    def __init__(self, items, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(0)
        self._values: dict[str, qf.BodyLabel] = {}
        self._captions: dict[str, qf.CaptionLabel] = {}
        last = len(items) - 1
        for i, (key, value) in enumerate(items):
            box = QVBoxLayout()
            box.setContentsMargins(8, 4, 8, 4)
            box.setSpacing(2)
            caption = qf.CaptionLabel(key)
            caption.setTextColor(*MUTED)
            big = qf.BodyLabel(str(value))
            big.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            box.addWidget(caption)
            box.addWidget(big)
            row.addLayout(box, 1)
            self._captions[key] = caption
            self._values[key] = big
            self._apply_emphasis(key, value)
            if i < last:
                row.addWidget(Rule(Qt.Orientation.Vertical), 0)

    def _apply_emphasis(self, key: str, text) -> None:
        """按显示宽度决定字号：数字用大字号，说明性文本降一档。"""
        label = self._values.get(key)
        if label is None:
            return
        px = (self.VALUE_PX if self.display_width(text) <= self.LONG_VALUE_WIDTH
              else self.VALUE_PX_WIDE)
        font = QFont(label.font())
        font.setPixelSize(px)
        font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)

    def value_widget(self, key: str):
        label = self._values.get(key)
        return label

    def set_value_color(self, key: str, pair: tuple[str, str]) -> None:
        """给某个指标值上语义色（如错误数标红、成功数标绿）。

        颜色以（浅, 深）配对传入，深浅模式各自取值；传 None 恢复主题默认色。
        """
        label = self._values.get(key)
        if label is None:
            return
        if pair is None:
            # 库的 FluentLabelBase.setTextColor() 不带参数 = 恢复主题默认色
            label.setTextColor()
        else:
            label.setTextColor(*pair)

    def caption_widget(self, key: str):
        return self._captions.get(key)

    def value_text(self, key: str) -> str:
        label = self._values.get(key)
        return label.text() if label is not None else ""

    def font_px(self, key: str) -> int:
        """当前值标签的字号（供测试判断是否降档）。"""
        label = self._values.get(key)
        return label.font().pixelSize() if label is not None else 0

    def set_value(self, key: str, text: str) -> None:
        label = self._values.get(key)
        if label is None:
            return
        label.setText(str(text))
        self._apply_emphasis(key, text)


class StatusBadge(QWidget):
    """结果徽章：圆形语义色底 + 居中白色图标，用于报告页的状态锚点。

    之前是裸图标挨着彩色标题，观感突兀；套上圆形底之后与品牌栏徽标呼应，
    成功/失败一眼可辨。颜色取（浅, 深）配对，随主题切换重绘。
    """

    def __init__(self, diameter: int = 52, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self._pair = SUCCESS
        self._pixmap: Optional[QPixmap] = None
        self.setFixedSize(diameter, diameter)
        self.set_state(ok=True)

    def set_state(self, ok: bool) -> None:
        """成功=成功色底+对勾；失败=错误色底+叉。"""
        self._pair = SUCCESS if ok else ERROR
        icon = fluent_icon(OK_ICON if ok else FAIL_ICON)
        size = int(self._diameter * 0.46)
        pm = icon.pixmap(size, size) if not icon.isNull() else None
        self._pixmap = tint_pixmap(pm, QColor("#ffffff")) if pm is not None \
            else None
        self.update()

    def paintEvent(self, _event) -> None:        # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        d = self._diameter
        painter.setBrush(pick_color(self._pair))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(0, 0, d, d))
        if self._pixmap is not None:
            painter.drawPixmap((d - self._pixmap.width()) // 2,
                               (d - self._pixmap.height()) // 2, self._pixmap)


class ActionBar(QWidget):
    """底部操作栏：顶部发丝线 + 左侧次要动作 + 右侧主动作。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(Rule(Qt.Orientation.Horizontal, strong=True))
        self._row = QHBoxLayout()
        self._row.setContentsMargins(24, 14, 24, 16)
        self._row.setSpacing(10)
        self._row.addStretch(1)
        layout.addLayout(self._row)

    def add_left(self, widget) -> None:
        self._row.insertWidget(0, widget)

    def add_right(self, widget) -> None:
        self._row.addWidget(widget)
