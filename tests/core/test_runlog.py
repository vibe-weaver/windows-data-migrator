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
