"""主题：薄封装 qfluentwidgets 的主题 API。

设计要点：
- **不自定义整套颜色令牌**。深/浅两套资源由库提供，本模块只负责选用，
  否则必然出现「某个模式下颜色写死导致文字不可见」——spike 已实测过。
- 跟随系统直接交给 `Theme.AUTO`：库内部有 `SystemThemeListener` 线程
  （基于 darkdetect.listener），系统主题一变就自动切，不需要自己轮询或监听。
- 任何一步失败都不能让程序起不来：主题问题只影响观感，不该阻断迁移。

项目自有的颜色**只在本模块定义一次**，页面与外壳一律从这里导入，
且每个颜色都给出（浅色, 深色）两个值——这是深色模式下不出问题的关键。
"""
from __future__ import annotations

import qfluentwidgets as qf

ACCENT = "#0067c0"
ACCENT_DARK = "#4cc2ff"
# 窗口背景（浅色, 深色）：取库自带 `FluentWidget` 的默认背景色，不自造配色。
# 关掉 Mica 后由 `setCustomBackgroundColor` 显式指定，窗口背景才会跟随库主题。
WINDOW_BACKGROUND = ("#f0f4f9", "#202020")
SUCCESS = ("#0f7b0f", "#6ccb5f")   # (浅色, 深色)
# 次要文字色。浅色原为 #9a9a9a，实测在窗口背景 #f0f4f9 上只有 **2.55:1**、
# 在卡片背景 #fafbfd 上 2.72:1，正文级别的说明文字读起来很吃力（WCAG AA 要求
# 4.5:1）。改成 #6b6b6b 后是 4.83:1 / 5.15:1，通过 AA；深色侧 #8a8a8a
# 在 #202020 上是 4.72:1、在卡片 #2b2b2b 上 4.10:1，本来就没问题，保持不动。
MUTED = ("#6b6b6b", "#8a8a8a")     # (浅色, 深色)
ERROR = ("#c42b1c", "#ff99a4")     # (浅色, 深色)

MODES: tuple[str, ...] = ("auto", "light", "dark")

_MODE_MAP = {
    "light": qf.Theme.LIGHT,
    "dark": qf.Theme.DARK,
    "auto": qf.Theme.AUTO,
}


def resolve_mode(mode) -> str:
    """把外部传入的模式归一成 auto / light / dark；非法值退回 auto。"""
    if not isinstance(mode, str):
        return "auto"
    value = mode.strip().lower()
    return value if value in MODES else "auto"


def apply_theme(mode: str = "auto") -> None:
    """应用主题。失败时不抛异常——观感问题不该阻断功能。"""
    resolved = resolve_mode(mode)
    try:
        # save=False：不写用户配置文件，避免污染别人的环境
        qf.setTheme(_MODE_MAP[resolved], save=False)
    except Exception:
        return
    try:
        qf.setThemeColor(ACCENT, save=False)
    except Exception:
        pass


def is_dark() -> bool:
    """当前是否深色。判定不了时返回 False。"""
    try:
        return bool(qf.isDarkTheme())
    except Exception:
        return False


def error_color() -> str:
    """当前主题下的错误色。

    表格单元格只能用 QColor 上色，走不了 FluentLabelBase.setTextColor 那套
    （浅色, 深色）双值接口，所以要在这里按主题挑一个出来。
    """
    return ERROR[1] if is_dark() else ERROR[0]
