"""生成应用图标：Qt 负责画，标准库 struct 负责组装 ICO 容器。"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

SIZES = (16, 32, 48, 256)

BACKDROP = "#1F6FEB"
FOREGROUND = "#FFFFFF"


def render_png(size: int) -> bytes:
    """画一个圆角底 + 双向箭头，返回 PNG 字节。"""
    from PySide6.QtCore import QBuffer, QByteArray, QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPolygonF

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    radius = size * 0.22
    backdrop = QPainterPath()
    backdrop.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.fillPath(backdrop, QColor(BACKDROP))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(FOREGROUND))
    bar = max(2.0, size * 0.11)
    head = size * 0.16
    for ratio, direction in ((0.34, 1), (0.66, -1)):
        center_y = size * ratio
        left, right = size * 0.24, size * 0.76
        painter.drawRect(QRectF(left, center_y - bar / 2, right - left, bar))
        tip = right if direction > 0 else left
        painter.drawPolygon(QPolygonF([
            QPointF(tip + head * 0.5 * direction, center_y),
            QPointF(tip - head * 0.5 * direction, center_y - head * 0.5),
            QPointF(tip - head * 0.5 * direction, center_y + head * 0.5),
        ]))
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

    pngs = {size: render_png(size) for size in SIZES}
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_ico(pngs))
    print(f"图标已生成：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "app.ico"))
