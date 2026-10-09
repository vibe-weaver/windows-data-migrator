import os
import sys
from pathlib import Path

# 【实测结论，务必先读】本机 QT_QPA_PLATFORM=offscreen 下 QFontDatabase.families() 返回 0，
# 即没有任何字体：QFontMetrics.inFont("迁") 为 False，drawText 画出来是「豆腐块」占位符，
# sizeHint/字体宽度全部失真。因此：
#   - 默认走平台默认插件（Windows 上是 windows），实测 406 个字体族、SimHei 可用；
#     控件只要不 show() 就不会弹窗，无头测试完全可行。
#   - 只有在确实需要 offscreen 的环境里，由外部显式设置 QT_QPA_PLATFORM=offscreen。
#   - 任何依赖字体度量或测量文本像素的断言，都必须在 offscreen 下 skip。
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 追加到 tests/conftest.py 末尾
import pytest


def _fonts_available() -> bool:
    try:
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        return len(QFontDatabase.families()) > 0
    except Exception:
        return False


requires_fonts = pytest.mark.skipif(
    not _fonts_available(),
    reason="当前 Qt 平台插件没有可用字体（offscreen 下必然如此），字体度量断言无意义")
