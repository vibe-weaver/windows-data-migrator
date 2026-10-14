# -*- coding: utf-8 -*-
"""构建使用说明书：渲染截图 -> 内嵌进模板 -> 输出单文件 HTML。

产物是**自包含**的（图片以 base64 内嵌），双击就能看，不怕被单独拷走之后图挂掉。
模板 `template.html` 里用 `{{SHOT:名字}}` 占位，这里的 SHOTS 表提供对应的画面。

用法（在仓库根目录）：
    env\\Scripts\\python.exe docs\\manual\\build.py
"""
from __future__ import annotations

import os

# 截图倍率钉死，不跟随系统显示缩放。
#
# 必须在导入/创建 QApplication **之前**设好：截图的像素尺寸 = 逻辑尺寸 ×
# devicePixelRatio，而 dpr 跟随系统缩放。不钉死的话，同一份脚本在 150% 的机器上
# 抓出 1770x990、在 100% 的机器上抓出 1180x660——同一份说明书，清晰度和体积都
# 不一样（实测差 220 KB）。这里用覆盖而不是 setdefault，就是为了让产物可复现。
#
# 1.5 是文档里按 CSS 宽度显示时的合适倍率：widget.grab() 会**按这个倍率重新渲染**，
# 多出来的是真实细节，不是把位图放大。
RENDER_SCALE = "1.5"
os.environ["QT_SCALE_FACTOR"] = RENDER_SCALE

import base64
import re
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
TARGET = HERE / "使用说明书.html"

from PySide6.QtCore import QSize, Qt                      # noqa: E402
from PySide6.QtGui import QGuiApplication                # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from dmig.core.locks import Holder                       # noqa: E402
from dmig.core.models import (CopyFailure, CopyResult,   # noqa: E402
                              MigrationPlan, PlanTotals, TargetGroup,
                              VerifyResult)
from dmig.core.migrator import (MigrateOutcome,          # noqa: E402
                                RestoreOutcome)
from dmig.core import guard                              # noqa: E402
from dmig.ui import theme                                # noqa: E402
from dmig.ui.holders_page import HoldersPage             # noqa: E402
from dmig.ui.plan_page import PlanPage                   # noqa: E402
from dmig.ui.progress_page import ProgressPage           # noqa: E402
from dmig.ui.report_page import ReportPage               # noqa: E402
from dmig.ui.select_page import SelectPage               # noqa: E402

SIZE = QSize(1180, 660)


def _grab(widget) -> bytes:
    """把控件渲染成 PNG 字节。"""
    widget.resize(SIZE)
    widget.show()
    for _ in range(10):
        QApplication.processEvents()
    pixmap = widget.grab()
    widget.hide()
    storage = BytesIO()
    from PySide6.QtCore import QBuffer, QByteArray
    blob = QByteArray()
    buffer = QBuffer(blob)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    buffer.close()
    storage.write(bytes(blob))
    return storage.getvalue()


# --- 各个画面 -----------------------------------------------------------

def shot_select_empty():
    page = SelectPage()
    page.set_source(r"E:\SteamLibrary\steamapps\common\某游戏\存档")
    page.set_target(r"D:\GameData\某游戏存档")
    return page


def shot_select_issues():
    page = SelectPage()
    page.set_source(r"C:\a")
    page.set_target(r"C:\b")
    page._issues = [
        guard.Issue("rename_blocked",
                    "源目录无法改名：拒绝访问（WinError 5）。"
                    "正在占用源目录的进程：WPS PDF (12208)。"
                    "关掉它们之后重新检验；确认无占用再开始迁移。",
                    severity="error"),
        guard.Issue("same_volume",
                    "源与目标在同一个卷 C:\\ 上：迁移不会释放空间，"
                    "备份还与原件同盘，这块盘一出问题两边一起丢",
                    severity="warning"),
    ]
    page._checked_pair = (page.source(), page.target())
    page._render_issues()
    return page


def shot_holders():
    page = HoldersPage()
    page.show_holders(
        [Holder("WPS PDF", 12208,
                r"C:\Program Files\WPS Office\12.1.0\office6\wpspdf.exe", ""),
         Holder("资源管理器", 8812, r"C:\Windows\explorer.exe",
                "资源管理器。关掉它会让桌面、任务栏和所有已打开的文件夹一起消失")],
        names=["缓存", "素材库"])
    return page


def shot_plan():
    page = PlanPage()
    plan = MigrationPlan(source=Path(r"E:\SteamLibrary\某游戏"),
                         target=Path(r"D:\GameData\某游戏"))
    plan.totals = PlanTotals(files=48213, dirs=3120, bytes=38 * 1024 ** 3)
    plan.required_bytes = 38 * 1024 ** 3
    plan.free_bytes = 210 * 1024 ** 3
    plan.link_groups = [
        TargetGroup(prefix=r"E:\npm-cache", count=482, mapped_to=""),
        TargetGroup(prefix=r"\\NAS\素材", count=17, mapped_to=r"F:\素材"),
    ]
    plan.ads_files = 3
    page.load(plan)
    return page


def shot_progress():
    page = ProgressPage()
    page.reset()
    page.set_running(True)
    from dmig.core.models import Progress
    page.update_progress(Progress(
        phase="copy", done_files=31004, total_files=48213,
        done_bytes=24 * 1024 ** 3, total_bytes=38 * 1024 ** 3,
        current=r"assets\pack\characters\hero_04.pak",
        speed_bps=186 * 1024 ** 2, eta_seconds=78))
    page.append_log("[复制] assets\\pack\\characters\\hero_04.pak")
    page.append_log("[校验] 已核对 31004 个文件，未发现不一致")
    page.set_failures([
        CopyFailure(rel=r"logs\session.log", reason="locked",
                    process="WPS PDF (12208)"),
    ])
    return page


def shot_report():
    page = ReportPage()
    page.load(MigrateOutcome(
        True, "done",
        copy=CopyResult(copied_files=48211, copied_bytes=38 * 1024 ** 3,
                        skipped=2, failures=[
                            CopyFailure(rel=r"logs\session.log", reason="locked",
                                        process="WPS PDF (12208)")]),
        verify=VerifyResult(ok=True, expected_files=48213, actual_files=48212),
        backup_path=r"E:\SteamLibrary\某游戏.bak_20261012143012"))
    page.set_actions_enabled(True, True)
    return page


def shot_restored():
    page = ReportPage()
    page.load_restored(RestoreOutcome(
        ok=True, source=r"E:\SteamLibrary\某游戏", source_restored=True,
        backup_path=r"E:\SteamLibrary\某游戏.bak_20261012143012",
        target=r"D:\GameData\某游戏", target_state="cleared"))
    page.set_actions_enabled(False, False, discard=True)
    return page


SHOTS = {
    "select_empty": shot_select_empty,
    "select_issues": shot_select_issues,
    "holders": shot_holders,
    "plan": shot_plan,
    "progress": shot_progress,
    "report": shot_report,
    "restored": shot_restored,
}


def main() -> int:
    app = QGuiApplication.instance() or QApplication([])
    theme.apply_theme("light")

    html = TEMPLATE.read_text(encoding="utf-8")
    for name, build in SHOTS.items():
        widget = build()
        data = _grab(widget)
        encoded = base64.b64encode(data).decode("ascii")
        tag = (f'<img alt="{name}" src="data:image/png;base64,{encoded}">')
        html, count = re.subn(r"\{\{SHOT:" + name + r"\}\}", tag, html)
        print(f"  {name:<16} {len(data) // 1024:>5} KB  替换 {count} 处")
        widget.deleteLater()

    missing = re.findall(r"\{\{SHOT:(\w+)\}\}", html)
    if missing:
        print(f"  **模板里还有没提供的占位符：{sorted(set(missing))}**")
        return 1
    TARGET.write_text(html, encoding="utf-8")
    print(f"\n  已生成：{TARGET}  （{TARGET.stat().st_size // 1024} KB，自包含）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


