"""生成应用图标：几何取自 `dmig.ui.logo`，标准库负责组装 ICO 容器。

几何**不在这里重画**——exe 图标与界面品牌栏必须取自同一张主图
（`src/dmig/assets/logo.png`），两处各画一遍迟早走样。主图的来源与
「白边是怎么来的」见 `src/dmig/assets/README.md`。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dmig.ui import logo  # noqa: E402  （必须先补 sys.path）


def render_png(size: int) -> bytes:
    """把品牌标记渲染成指定尺寸的 PNG 字节。"""
    from PySide6.QtCore import QBuffer, QByteArray, Qt
    from PySide6.QtGui import QImage, QPainter

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    logo.draw_mark(painter, size)
    painter.end()

    # QByteArray 必须绑定到变量：若写成 QBuffer(QByteArray()) 这个临时对象会立即析构，
    # QBuffer 内部持有的是它的裸指针，image.save() 会访问已释放内存（实测 12/12 崩溃）。
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico(pngs: dict[int, bytes]) -> bytes:
    """按 ICO 容器格式打包若干 PNG；256 的宽高字节按规范写成 0。"""
    items = sorted(pngs.items())
    header = struct.pack("<HHH", 0, 1, len(items))
    offset = 6 + 16 * len(items)
    entries = b""
    payload = b""
    for size, data in items:
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32,
                               len(data), offset)
        payload += data
        offset += len(data)
    return header + entries + payload


def main(out_path) -> int:
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        QGuiApplication([sys.argv[0] if sys.argv else "make_icon"])

    pngs = {size: render_png(size) for size in logo.SIZES}
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_ico(pngs))
    print(f"图标已生成：{target}（{len(pngs)} 档："
          f"{'、'.join(str(s) for s in sorted(pngs))}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "app.ico"))
