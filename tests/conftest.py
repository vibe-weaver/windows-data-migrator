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


APPEXECLINK_TAG = 0x8000001B


def _raw_reparse_buffer(tag: int, payload: bytes = b"") -> bytes:
    """构造最小可用的重解析点缓冲区（8 字节头 + 可选负载）。"""
    import ctypes
    import struct

    buf = ctypes.create_string_buffer(8 + len(payload))
    struct.pack_into("<IHH", buf, 0, tag, len(payload), 0)
    if payload:
        buf[8:8 + len(payload)] = payload
    return buf.raw


@pytest.fixture
def raw_reparse_buffer():
    return _raw_reparse_buffer


@pytest.fixture
def make_reparse_file(tmp_path):
    """造一个**非链接类**重解析点，行为与真实 AppExecLink 一致。

    真机上 `%LOCALAPPDATA%\\Microsoft\\WindowsApps` 里那 29 个 AppExecLink
    连 open() 都失败于 Invalid argument、CopyFileW 报 WinError 1920，
    只有 FSCTL_GET/SET_REPARSE_POINT 能读写。这里的合成品实测行为相同，
    因此测试不依赖机器上装没装 Store 应用，也不去碰系统目录。

    退出时把 tmp_path 下所有非链接类重解析点的数据摘掉：复制引擎会在目标端
    原样搬出一份，pytest 的 rm_rf 对目录形态的重解析点会报 WinError 1920，
    把临时目录整个留在 %TEMP% 里。链接类（junction / symlink）不动。
    """
    from dmig.core import reparse
    from dmig.core.fsutil import is_link_kind, reparse_kind

    def _make(path, *, is_dir: bool = False, tag: int = APPEXECLINK_TAG):
        if is_dir:
            os.makedirs(path, exist_ok=True)
        else:
            with open(path, "wb"):
                pass
        reparse.set_reparse_data(path, _raw_reparse_buffer(tag))
        return str(path)

    yield _make

    for dirpath, dirnames, filenames in os.walk(str(tmp_path)):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            try:
                kind = reparse_kind(os.lstat(full))
            except OSError:
                continue
            if kind is None or is_link_kind(kind):
                continue
            try:
                reparse.delete_reparse_point(full)
            except OSError:
                pass
