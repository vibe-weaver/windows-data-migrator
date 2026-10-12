"""每趟迁移的运行日志：把人看得懂的过程按时间顺序落成一份文本。

迁移失败之后现场往往已经变了——源目录被改名成备份、目标端躺着半成品，
而会话 JSON 只记结构化数据（`.done` / `failed` / `links`）。想知道「这几个小时
到底走到了哪一步、哪个阶段开始不对」，靠的就是这份日志。它与会话文件同目录同前缀：
`<id>.json` / `<id>.done` / `<id>.log`。

刻意不长期持有文件句柄：每次写一行就开关一次。一趟迁移的日志量在几百行量级，
这点开销可以忽略；换来的是句柄不泄漏、进程被杀也不会丢掉已经写下的部分。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

LOG_SUFFIX = ".log"

# 失败项逐条写的前 N 条；几万个失败逐个写会把日志撑到几十 MB，
# 而人要看的是前几条的样子加上总数，完整清单在会话文件的 failed 里。
MAX_FAILURE_LINES = 200


class RunLog:
    """追加式文本日志。

    写不进去就静默放弃：日志是给人排查用的辅助产物，
    绝不能因为它自己出问题（目录只读、盘满、路径太长）而把迁移本身搞挂。

    `on_line` 是可选的逐行回调，用来把同一份内容同时喂给界面。界面上的
    「迁移日志」面板原来只写三个事件（校验开始/复制失败/取消），一趟干净成功的
    迁移在界面上只剩一行、磁盘上却有十条——两条流各自演化必然继续漂移，
    所以干脆让界面显示这份日志本身。
    """

    def __init__(self, path, on_line=None):
        self.path = Path(path)
        self.on_line = on_line

    # --- 写 ---------------------------------------------------------
    def _emit(self, line: str) -> None:
        """把整行（含时间戳）交给界面。界面侧出错绝不能影响迁移。"""
        if self.on_line is None:
            return
        try:
            self.on_line(line)
        except Exception:
            pass

    def write(self, message: str) -> None:
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        line = f"[{stamp}] {message}"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except OSError:
            pass
        self._emit(line)

    def failures(self, title: str, items) -> None:
        """记一批失败项：前 MAX_FAILURE_LINES 条逐条写，其余只报总数。"""
        items = list(items)
        if not items:
            return
        self.write(f"{title}：{len(items)} 项")
        for item in items[:MAX_FAILURE_LINES]:
            self.write(f"    {item}")
        if len(items) > MAX_FAILURE_LINES:
            self.write(f"    ……其余 {len(items) - MAX_FAILURE_LINES} 项见会话文件的 failed")
