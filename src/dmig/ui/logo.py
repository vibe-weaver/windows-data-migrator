"""品牌标记：几何来自 `assets/logo.png` 这一张主图。

**几何只有这一处来源**——exe 图标（`packaging/make_icon.py`）与界面品牌栏
（`chrome.BrandBar`）都从这里取；两处各画一遍迟早会走样。

主图是 AI 生成后抠成透明底的（过程见 `assets/README.md`）。原来这里是代码画的
矢量几何，换成位图的原因：主图有光影与材质，代码画不出那个质感；而图标要的
多档位缩放，交给 Qt 的平滑缩放即可。

**透明区域的 RGB 已染成边缘蓝**，不是白色——见 `assets/README.md` 里那条
「白边是怎么来的」，这是位图图标最常见的一个坑。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap

# 品牌文案：界面与打包元数据都从这里取，避免出现两个名字
NAME = "Windows数据迁移助手"
TAGLINE = "搬走数据，留下链接"

# 图标档位。64 与 128 是必需的：资源管理器「中等图标」是 64px，缺了这一档
# 系统只能把 48 硬拉大，看着发虚。
SIZES = (16, 32, 48, 64, 128, 256)

ASSET_NAME = "logo.png"

# 主图缓存。一张 1024 的图读一次约 10 毫秒，六个档位各读一次没必要。
_master: QImage | None = None


def asset_path() -> str:
    """主图的绝对路径；找不到返回空串。

    打包成 onedir 之后 `__file__` 在 `_internal/dmig/ui/`，而 `--add-data` 把
    资源放在 `_internal/dmig/assets/`，两者相对位置与源码树里一致，所以
    「模块旁边的 ../assets/」这一个规则在两种情况下都对。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    package = os.path.dirname(here)
    candidates = [
        os.path.join(package, "assets", ASSET_NAME),
        # 源码树外直接跑打包产物时的兜底
        os.path.join(os.path.dirname(package), "assets", ASSET_NAME),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return ""


def master_image() -> QImage | None:
    """主图；取不到返回 None。"""
    global _master
    if _master is None:
        path = asset_path()
        if not path:
            return None
        image = QImage(path)
        if image.isNull():
            return None
        _master = image
    return _master


def draw_mark(painter: QPainter, size: float) -> None:
    """把品牌标记画进 painter 的 (0, 0, size, size) 区域。

    调用方负责开抗锯齿。size 是逻辑像素，调用方可自行按 DPI 放大。
    """
    source = master_image()
    if source is None:
        # 兜底：资源丢了也不能让品牌栏与图标变成空白，铺一块品牌底色并说明不了
        # 什么，但至少看得出是哪个程序。
        painter.fillRect(QRectF(0, 0, size, size), QColor("#17309A"))
        return
    painter.drawImage(QRectF(0, 0, size, size), source)


def mark_pixmap(size: int) -> QPixmap:
    """渲染一枚标记位图。品牌栏用这个。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # 平滑缩放必须开：主图是 1024，缩到 16 靠默认的最近邻会碎成格子
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    draw_mark(painter, size)
    painter.end()
    return pixmap


def mark_icon() -> QIcon:
    """多档位 QIcon，供窗口图标 / 任务栏使用。"""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(mark_pixmap(size))
    return icon


def app_icon() -> QIcon:
    """优先用打包好的 `app.ico`（系统会自己挑档位）；取不到就现画一枚。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    for candidate in (os.path.join(here, "app.ico"),
                      os.path.join(root, "packaging", "app.ico")):
        if os.path.exists(candidate):
            icon = QIcon(candidate)
            if not icon.isNull():
                return icon
    return mark_icon()
