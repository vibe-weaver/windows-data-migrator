"""主题：薄封装 qfluentwidgets 的主题 API，**固定浅色**。

为什么删掉深色/跟随系统：库的窗口背景层用 120ms QPropertyAnimation 过渡，
主题切换（跟随系统时随时可能发生）后控件样式立即重刷、背景却要等动画跑完；
动画一旦没有正常完成，就是「深色控件 + 浅色窗口」的混搭——浅底上全是看不清
的白字。自绘控件（品牌栏 / 步骤条 / 图标芯片）与库控件的取色时机还不一致，
单一主题是唯一能从机制上保证「任何时刻背景与控件同色」的方案。

因此本模块把主题钉死在浅色：
- `apply_theme` 无论传什么模式都应用 light；
- `is_dark()` 恒为 False，`pick()` 恒取各配对的浅色值——页面的取色代码
  不用改，深色值保留在配对里仅作备用。
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
# 主要文字色：自定义绘制（徽标上的应用名）用它，避免写死黑色在深色底上不可见。
TEXT = ("#1a1a1a", "#ffffff")      # (浅色, 深色)

# 外壳与分隔用的语义令牌：每个都给（浅色, 深色）两个值，深色模式才不会失效。
# 发丝分隔线——比卡片边框更弱，用于区隔同一卡片内的相邻区块（如指标之间、
# 品牌栏与步骤条之间）。浅色 #e2e5ea 在窗口背景上对比度够低、不抢眼。
BORDER = ("#e2e5ea", "#3a3c40")
# 强一点的分隔线，用于卡片之间、操作栏顶部的明确分割。
BORDER_STRONG = ("#d4d7dd", "#4a4d52")
# 强调色背景（浅, 深）：放在徽标、图标芯片、当前步骤圆点背后。
# 浅色取极淡的强调蓝，深色取强调蓝压暗到接近深底，避免在深色卡片上糊成一团。
ACCENT_TINT = ("#e8f1fb", "#163558")
# 强调色背景上的前景色：强调底色饱和度足够，深浅模式都用白字即可。
ON_ACCENT = ("#ffffff", "#ffffff")
# 警示色（如「删除备份」「回滚」这类不可逆操作）。
WARNING = ("#9a6700", "#f0b400")

MODES: tuple[str, ...] = ("light",)

# 强调色（浅, 深）配对：自定义绘制与 FluentLabel.setTextColor 共用
ACCENT_PAIR = (ACCENT, ACCENT_DARK)

_MODE_MAP = {
    "light": qf.Theme.LIGHT,
    "dark": qf.Theme.DARK,
    "auto": qf.Theme.AUTO,
}


def resolve_mode(mode) -> str:
    """把外部传入的模式归一；深色与跟随系统已删除，一律归到 light。"""
    return "light"


def apply_theme(mode: str = "light") -> None:
    """应用浅色主题。任何传入模式都被忽略——深色已从产品中移除。

    失败时不抛异常——观感问题不该阻断功能。
    """
    try:
        # save=False：不写用户配置文件，避免污染别人的环境
        qf.setTheme(_MODE_MAP["light"], save=False)
    except Exception:
        return
    try:
        qf.setThemeColor(ACCENT, save=False)
    except Exception:
        pass


def is_dark() -> bool:
    """当前是否深色。主题已钉死浅色，恒为 False。"""
    return False


def pick(pair: tuple[str, str]) -> str:
    """按当前主题从（浅, 深）配对里取一个颜色字面量。

    自定义绘制（QPainter / QSS）拿不到 Fluent 控件那套双值接口，统一走这里。
    主题钉死浅色后恒取浅色值。
    """
    return pair[0]


def error_color() -> str:
    """当前主题下的错误色。

    表格单元格只能用 QColor 上色，走不了 FluentLabelBase.setTextColor 那套
    （浅色, 深色）双值接口，所以在这里按主题挑一个出来。
    """
    return ERROR[0]


def warning_color() -> str:
    """当前主题下的警告色。同上，供列表项按严重程度上色。"""
    return WARNING[0]
