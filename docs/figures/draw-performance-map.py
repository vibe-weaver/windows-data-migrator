# -*- coding: utf-8 -*-
"""迁移耗时关系图（第三版）。

第二版的排版 bug：`AlignLeft` 的文本框我从 `ax-190` 起画，等于把文字推到锚点左边，
最左侧那个场景的标签因此压到 Y 轴上。对齐方式决定文本框该从哪边起：
  AlignLeft   -> 文本框左边 = 锚点
  AlignRight  -> 文本框右边 = 锚点
  AlignHCenter-> 锚点居中
场景也从 6 个减到 4 个：消息只需要四个角各一个点，挤在一起反而看不清。
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, r"E:\数据迁移\src")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient, QPainter,
                           QPen, QPolygonF)
from PySide6.QtWidgets import QApplication

PER_FILE = 776e-6
BANDWIDTH = 1987
W, H = 1720, 1110
OUT = Path(r"E:\数据迁移\docs\performance-map.png")
LEFT, TOP, RIGHT, BOTTOM = 168, 152, 1200, 892
N_MIN, N_MAX = 100, 2_000_000
G_MIN, G_MAX = 0.05, 20_000
T_FLOOR, T_CEIL = 1.0, 20 * 3600


def seconds(n, gb):
    return n * PER_FILE + gb * 1024 / BANDWIDTH


def human(s):
    if s < 1:
        return f"{s*1000:.0f} 毫秒"
    if s < 60:
        return f"{s:.0f} 秒"
    if s < 3600:
        return f"{s/60:.0f} 分"
    return f"{s/3600:.1f} 小时"


def lx(n):
    f = (math.log10(n) - math.log10(N_MIN)) / (math.log10(N_MAX) - math.log10(N_MIN))
    return LEFT + f * (RIGHT - LEFT)


def ly(g):
    f = (math.log10(g) - math.log10(G_MIN)) / (math.log10(G_MAX) - math.log10(G_MIN))
    return BOTTOM - f * (BOTTOM - TOP)


def colour(s):
    t = (math.log10(max(s, 0.1)) - math.log10(T_FLOOR)) / \
        (math.log10(T_CEIL) - math.log10(T_FLOOR))
    t = max(0.0, min(1.0, t))
    stops = [(0.00, (233, 246, 234)), (0.22, (129, 199, 132)),
             (0.42, (255, 241, 118)), (0.60, (255, 183, 77)),
             (0.78, (239, 108, 77)), (1.00, (150, 20, 30))]
    for i in range(len(stops) - 1):
        a, ca = stops[i]
        b, cb = stops[i + 1]
        if a <= t <= b:
            k = (t - a) / (b - a)
            return QColor(*[int(ca[j] + (cb[j] - ca[j]) * k) for j in range(3)])
    return QColor(*stops[-1][1])


app = QApplication.instance() or QApplication([])
img = QImage(W, H, QImage.Format.Format_ARGB32)
img.fill(QColor(255, 255, 255))
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
F = "Microsoft YaHei"

p.setFont(QFont(F, 30, QFont.Weight.Bold))
p.setPen(QColor(26, 26, 32))
p.drawText(QRectF(LEFT, 42, 1200, 48), Qt.AlignmentFlag.AlignLeft,
           "迁移耗时：文件数 × 总体积")
p.setFont(QFont(F, 16))
p.setPen(QColor(110, 110, 120))
p.drawText(QRectF(LEFT, 94, 1260, 30), Qt.AlignmentFlag.AlignLeft,
           f"耗时 ≈ 文件数 × {PER_FILE*1e6:.0f} 微秒 + 总体积 ÷ {BANDWIDTH} MB/秒"
           "　（本机实测：同盘 NVMe，含校验）")

COLS, ROWS = 200, 130
for cx in range(COLS):
    n = 10 ** (math.log10(N_MIN) + (cx + 0.5) / COLS *
               (math.log10(N_MAX) - math.log10(N_MIN)))
    x0 = LEFT + cx / COLS * (RIGHT - LEFT)
    x1 = LEFT + (cx + 1) / COLS * (RIGHT - LEFT)
    for cy in range(ROWS):
        g = 10 ** (math.log10(G_MIN) + (cy + 0.5) / ROWS *
                   (math.log10(G_MAX) - math.log10(G_MIN)))
        y1 = BOTTOM - cy / ROWS * (BOTTOM - TOP)
        y0 = BOTTOM - (cy + 1) / ROWS * (BOTTOM - TOP)
        p.fillRect(QRectF(x0, y0, x1 - x0 + 1, y1 - y0 + 1), colour(seconds(n, g)))

for target, label, lx_off in ((10, "10 秒", 7), (60, "1 分", 7),
                              (600, "10 分", -60), (3600, "1 小时", 7),
                              (36000, "10 小时", 7)):
    pts, steps = [], 600
    for i in range(steps + 1):
        n = 10 ** (math.log10(N_MIN) + i / steps *
                   (math.log10(N_MAX) - math.log10(N_MIN)))
        remain = target - n * PER_FILE
        if remain <= 0:
            pts.append(QPointF(lx(n), BOTTOM))
            break
        gb = remain * BANDWIDTH / 1024
        if gb > G_MAX:
            continue
        pts.append(QPointF(lx(n), ly(max(gb, G_MIN))))
    if len(pts) < 2:
        continue
    poly = QPolygonF(pts)
    p.setPen(QPen(QColor(255, 255, 255, 235), 4))
    p.drawPolyline(poly)
    p.setPen(QPen(QColor(50, 50, 60, 210), 2, Qt.PenStyle.DashLine))
    p.drawPolyline(poly)
    at = min(len(pts) - 1, max(1, len(pts) // 8))
    p.setFont(QFont(F, 15, QFont.Weight.Bold))
    p.setPen(QColor(35, 35, 45))
    p.drawText(QPointF(pts[at].x() + lx_off, pts[at].y() - 10), label)

p.setPen(QPen(QColor(90, 90, 100), 2))
p.drawRect(QRectF(LEFT, TOP, RIGHT - LEFT, BOTTOM - TOP))
p.setFont(QFont(F, 15))
for n in (100, 1000, 10000, 100000, 1000000):
    x = lx(n)
    p.setPen(QPen(QColor(90, 90, 100, 110), 1, Qt.PenStyle.DotLine))
    p.drawLine(QPointF(x, BOTTOM), QPointF(x, BOTTOM + 8))
    p.setPen(QColor(60, 60, 70))
    p.drawText(QRectF(x - 60, BOTTOM + 12, 120, 26), Qt.AlignmentFlag.AlignHCenter,
               f"{n/10000:.0f} 万" if n >= 10000 else f"{n:,}")
for g in (0.1, 1, 10, 100, 1000, 10000):
    y = ly(g)
    p.setPen(QPen(QColor(90, 90, 100, 110), 1, Qt.PenStyle.DotLine))
    p.drawLine(QPointF(LEFT - 8, y), QPointF(LEFT, y))
    p.setPen(QColor(60, 60, 70))
    txt = f"{g*1024:.0f} MB" if g < 1 else (f"{g:.0f} GB" if g < 1024 else f"{g/1024:.0f} TB")
    p.drawText(QRectF(34, y - 13, LEFT - 50, 26),
               Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)

p.setFont(QFont(F, 17, QFont.Weight.Bold))
p.setPen(QColor(40, 40, 50))
p.drawText(QRectF(LEFT, BOTTOM + 48, RIGHT - LEFT, 30),
           Qt.AlignmentFlag.AlignHCenter, "文件数量  越多越慢（与体积无关）")
p.save()
p.translate(62, (TOP + BOTTOM) / 2)
p.rotate(-90)
p.setFont(QFont(F, 17, QFont.Weight.Bold))
p.drawText(QRectF(-220, 0, 440, 30), Qt.AlignmentFlag.AlignHCenter,
           "总体积  越大越慢")
p.restore()

# 区域标注（让开等时线标签的位置）
p.setFont(QFont(F, 19, QFont.Weight.Bold))
p.setPen(QColor(140, 20, 30))
p.drawText(QPointF(lx(260000), ly(0.24)), "文件数主导区")
p.setFont(QFont(F, 13))
p.setPen(QColor(120, 30, 40))
p.drawText(QPointF(lx(105000), ly(0.115)), "等时线竖直：瓶颈不在带宽")
p.setFont(QFont(F, 19, QFont.Weight.Bold))
p.setPen(QColor(20, 80, 140))
p.drawText(QPointF(lx(112), ly(5000)), "体积主导区")
p.setFont(QFont(F, 13))
p.setPen(QColor(30, 70, 120))
p.drawText(QPointF(lx(112), ly(1800)), "等时线倾斜：换更快的盘直接见效")

# 四个角各一个场景，标签方向逐个指定
SCENES = [
    ("虚拟机镜像", 200, 98, "right"),
    ("照片库", 20000, 78, "above"),
    ("代码仓库 / node_modules", 100000, 1.9, "below"),
    ("大型素材库", 1000000, 500, "left"),
]
for name, n, gb, place in SCENES:
    x, y = lx(n), ly(gb)
    p.setPen(QPen(QColor(255, 255, 255), 5))
    p.drawEllipse(QPointF(x, y), 9, 9)
    p.setPen(QPen(QColor(20, 20, 30), 2))
    p.drawEllipse(QPointF(x, y), 9, 9)
    t = human(seconds(n, gb))
    if place == "right":
        ax, ay, align = x + 18, y - 20, Qt.AlignmentFlag.AlignLeft
        rect = QRectF(ax, ay, 360, 44)
    elif place == "left":
        ax, ay, align = x - 18, y - 20, Qt.AlignmentFlag.AlignRight
        rect = QRectF(ax - 360, ay, 360, 44)
    elif place == "above":
        ax, ay, align = x, y - 52, Qt.AlignmentFlag.AlignHCenter
        rect = QRectF(ax - 200, ay, 400, 44)
    else:
        ax, ay, align = x, y + 18, Qt.AlignmentFlag.AlignHCenter
        rect = QRectF(ax - 200, ay, 400, 44)
    p.setFont(QFont(F, 13, QFont.Weight.Bold))
    p.setPen(QColor(20, 20, 30))
    p.drawText(rect, align, name)
    p.setFont(QFont(F, 12))
    p.setPen(QColor(70, 70, 80))
    p.drawText(rect.adjusted(0, 18, 0, 18), align, f"{n:,} 个 · {gb} GB · {t}")

LX0, LY0, LW, LH = 1272, 152, 46, 700
grad = QLinearGradient(0, LY0, 0, LY0 + LH)
for i in range(101):
    t = i / 100
    grad.setColorAt(t, colour(10 ** (math.log10(T_FLOOR) + (1 - t) *
                                     (math.log10(T_CEIL) - math.log10(T_FLOOR)))))
p.fillRect(QRectF(LX0, LY0, LW, LH), grad)
p.setPen(QPen(QColor(120, 120, 130), 1))
p.drawRect(QRectF(LX0, LY0, LW, LH))
p.setFont(QFont(F, 13))
p.setPen(QColor(60, 60, 70))
for s in (1, 10, 60, 600, 3600, 36000, 72000):
    t = (math.log10(s) - math.log10(T_FLOOR)) / (math.log10(T_CEIL) - math.log10(T_FLOOR))
    if not 0 <= t <= 1:
        continue
    y = LY0 + LH - t * LH
    p.drawLine(QPointF(LX0 + LW, y), QPointF(LX0 + LW + 6, y))
    p.drawText(QPointF(LX0 + LW + 12, y + 5), human(s))
p.setFont(QFont(F, 14, QFont.Weight.Bold))
p.drawText(QPointF(LX0, LY0 - 16), "预计耗时")

p.setFont(QFont(F, 13))
p.setPen(QColor(110, 110, 120))
p.drawText(QPointF(30, 1044),
           "实测条件：C/D/E 为同一块 NVMe 的几个分区（读写共享通道），耗时含校验。"
           "体积主导区换第二块物理盘会更快；文件数主导区在同类盘之间换盘无收益（机械盘换 SSD 会大幅见效）。")
p.drawText(QPointF(30, 1070),
           "数据来源：8 万个 128 字节文件、5000 个 1 MB 文件、160 个 256 MB 文件三组实测。"
           "小数据量装得进内存缓存时会比本图更快，图上是保守估计。")
p.end()
OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(str(OUT))
print(f"  已生成 {OUT}  ({OUT.stat().st_size // 1024} KB)")
