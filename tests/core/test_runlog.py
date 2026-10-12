"""运行日志：追加式、带时间戳、写不进去也不许抛。"""
from __future__ import annotations

from dmig.core.runlog import MAX_FAILURE_LINES, RunLog


def test_write_appends_a_timestamped_line(tmp_path):
    path = tmp_path / "sub" / "run.log"
    log = RunLog(path)
    log.write("开始一趟")
    log.write("结果：ok=True phase=done")

    text = path.read_text(encoding="utf-8")
    assert text.startswith("[20"), text          # [YYYY-MM-DD HH:MM:SS]
    assert "开始一趟" in text
    assert "结果：ok=True phase=done" in text
    assert len(text.splitlines()) == 2


def test_write_appends_instead_of_truncating(tmp_path):
    """续传沿用同一个会话，日志也接着写：抹掉上一趟的记录等于丢失现场。"""
    path = tmp_path / "run.log"
    RunLog(path).write("第一趟")
    RunLog(path).write("第二趟")
    text = path.read_text(encoding="utf-8")
    assert "第一趟" in text and "第二趟" in text


def test_unwritable_log_never_raises(tmp_path):
    """日志是排查用的辅助产物，绝不能因为它自己出问题把迁移搞挂。"""
    log = RunLog(tmp_path / "run.log")
    log.path = tmp_path / "run.log" / "拿文件当目录"       # 必然写不进去
    log.write("这行写不进去")
    log.failures("复制失败", ["a", "b"])


def test_failures_caps_the_listing(tmp_path):
    """几万个失败逐条写会把日志撑爆，人要看的是前几条 + 总数。"""
    path = tmp_path / "run.log"
    RunLog(path).failures("复制失败",
                          [f"f{index}" for index in range(MAX_FAILURE_LINES + 30)])
    text = path.read_text(encoding="utf-8")
    assert f"复制失败：{MAX_FAILURE_LINES + 30} 项" in text
    assert "f0\n" in text
    assert f"f{MAX_FAILURE_LINES - 1}\n" in text
    assert f"f{MAX_FAILURE_LINES}\n" not in text
    assert "其余 30 项见会话文件" in text


def test_failures_with_nothing_to_report_writes_nothing(tmp_path):
    path = tmp_path / "run.log"
    RunLog(path).failures("复制失败", [])
    assert not path.exists()


# --- 每一行同时喂给界面 -----------------------------------------------
#
# 界面上的「迁移日志」面板原来只写三个事件（校验开始/复制失败/取消），
# 一次干净成功的迁移在界面上只剩一行校验提示，而磁盘日志里有十条。
# 两条流各自演化必然继续漂移，所以让界面直接显示这份日志本身。

def test_write_feeds_the_sink_with_the_same_line(tmp_path):
    path = tmp_path / "run.log"
    lines: list[str] = []
    log = RunLog(path, on_line=lines.append)

    log.write("开始一趟")
    log.write("结果：ok=True")

    # 界面上要看到和文件里一模一样的内容（含时间戳前缀）
    assert lines == path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[20") and "开始一趟" in lines[0]


def test_failures_also_reach_the_sink(tmp_path):
    """失败清单是排查的主角，不能不进界面。"""
    lines: list[str] = []
    log = RunLog(tmp_path / "run.log", on_line=lines.append)
    log.failures("复制失败", ["a.txt（拒绝访问）"])
    assert any("复制失败：1 项" in line for line in lines)
    assert any("a.txt（拒绝访问）" in line for line in lines)


def test_broken_sink_never_breaks_logging(tmp_path):
    """界面侧出问题绝不能影响迁移，也不能让日志少写一行。"""
    path = tmp_path / "run.log"

    def boom(_line):
        raise RuntimeError("界面炸了")

    log = RunLog(path, on_line=boom)
    log.write("这行必须落盘")

    assert "这行必须落盘" in path.read_text(encoding="utf-8")


def test_sink_is_optional(tmp_path):
    path = tmp_path / "run.log"
    RunLog(path).write("没有界面也要能写")
    assert "没有界面也要能写" in path.read_text(encoding="utf-8")
