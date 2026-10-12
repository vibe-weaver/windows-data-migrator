"""图标名必须是库真实存在的成员。

背景：`FluentIcon.WARNING` 并不存在——这个库 174 个图标里没有警示三角。
而 `fluent_icon()` 取不到名字时**静默返回空图标**，后果是红色结果徽章里
空空如也、「失败清单」标题少一个图标芯片，界面上不报任何错，
只有盯着渲染图才看得出来。

这个测试扫描整个 ui 包，把不存在的图标名直接报出来。
"""
import re
from pathlib import Path

import qfluentwidgets as qf

from dmig.ui import chrome

UI_DIR = Path(__file__).resolve().parents[2] / "src" / "dmig" / "ui"

# fluent_icon("NAME")
DIRECT = re.compile(r'fluent_icon\(\s*"([A-Z_]+)"')
# _card("标题", "NAME") —— plan_page 的卡片助手，图标名是最后一个参数
CARD = re.compile(r'_card\(\s*"[^"]*"\s*,\s*"([A-Z_]+)"', re.S)


def _icon_names() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for path in sorted(UI_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in (DIRECT, CARD):
            for match in pattern.finditer(text):
                used.setdefault(match.group(1), []).append(path.name)
    return used


def test_scan_actually_finds_icon_names():
    """扫描自身要有效：正则是失效的话，下面那条测试会永远通过。"""
    used = _icon_names()
    assert len(used) >= 5, f"只扫到 {len(used)} 个图标名，正则可能失效：{used}"


def test_every_icon_name_exists():
    available = {name for name in dir(qf.FluentIcon) if name.isupper()}
    missing = {name: files for name, files in _icon_names().items()
               if name not in available}
    assert not missing, (
        f"这些图标名在 FluentIcon 里不存在：{missing}。"
        f"fluent_icon() 会静默返回空图标，界面上只是「少一个图标」而不会报错。"
        f"当前可用名字共 {len(available)} 个。"
    )


def test_badge_icons_are_actually_drawable():
    """常量不仅要名字存在，还要真的能取到 pixmap（空图标也算画不出来）。"""
    for name in (chrome.OK_ICON, chrome.FAIL_ICON):
        icon = chrome.fluent_icon(name)
        assert not icon.isNull(), f"{name} 取不到图标"
        pixmap = icon.pixmap(20, 20)
        assert pixmap is not None and not pixmap.isNull(), f"{name} 画不出 pixmap"
