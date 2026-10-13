import csv
import io

from dmig.core.models import CopyFailure, CopyResult, VerifyResult
from dmig.core.migrator import MigrateOutcome
from dmig.ui import report


def _outcome(**kwargs):
    defaults = dict(
        ok=False,
        phase="copy_failed",
        copy=CopyResult(copied_files=2, copied_bytes=1003, skipped=1,
                        failures=[CopyFailure(rel="x\\y.db", reason="locked",
                                              process="chrome.exe (1234)")]),
        verify=VerifyResult(ok=False, missing=["m.txt"], mismatch=["n.bin"]),
    )
    defaults.update(kwargs)
    return MigrateOutcome(**defaults)


def test_phase_keys_are_translated_for_users():
    """内部阶段键不能直接甩给用户。

    真实反馈：报告页顶着 60pt 的红字「未完成：rename_failed」，下面还挂着
    Python 异常原文。用户既不知道发生了什么，也不知道下一步做什么。
    """
    assert report.phase_text("rename_failed") == "源目录改名失败"
    assert report.phase_text("verify_failed") == "校验未通过"
    assert report.phase_text("link_failed") == "源目录链接创建失败"
    assert report.phase_text("privilege_required") == "需要管理员权限"


def test_unknown_phase_is_passed_through():
    """没收录的键原样返回：宁可露出生词，也不能把信息吞掉。"""
    assert report.phase_text("brand_new_phase") == "brand_new_phase"
    assert report.phase_text("") == ""


def test_blocked_step_gets_its_own_row():
    """整趟卡在某一步时，清单里必须有一条。

    改名失败不属于「复制失败／缺失／断链／属性丢失」任何一类，于是清单是空的
    ——用户看到刺眼的红标题配一张空表，而问题明明就在眼前（谁占着文件）。
    """
    outcome = _outcome(ok=False, phase="rename_failed",
                       blocked_path=r"C:\wwwwwww",
                       blocked_reason="拒绝访问（WinError 5）",
                       blocked_process="WPS PDF (29396)")
    rows = report.blocked_rows(outcome)
    assert rows == [[report.KIND_BLOCKED, r"C:\wwwwwww",
                     "拒绝访问（WinError 5）", "WPS PDF (29396)"]]
    assert rows[0] in report.unfinished_rows(outcome)


def test_no_blocked_step_means_no_row():
    assert report.blocked_rows(_outcome()) == []


def test_restore_csv_matches_the_restore_screen():
    """恢复态下导出的是恢复信息，不是那份已经被撤销的迁移报告。

    界面上写「恢复成功 / 恢复结果」，导出却给一份「迁移报告」——拿到文件的人
    会以为迁移还在生效。缺失、未复制这些迁移期的词在恢复报告里也不该出现。
    """
    from dmig.core.migrator import RestoreOutcome
    outcome = RestoreOutcome(
        ok=True, source=r"C:\wwwwww", source_restored=True,
        backup_path=r"C:\wwwwww.bak_20261011", target=r"D:\myapp\1",
        target_state="cleared", acl_note="1 个目录的原始权限没能还原")

    rows = report.build_restore_rows(outcome)
    text = "\n".join(",".join(row) for row in rows)

    assert report.KIND_RESTORED in text
    assert r"C:\wwwwww" in text and "已恢复" in text
    assert r"C:\wwwwww.bak_20261011" in text and "改回原名" in text
    assert r"D:\myapp\1" in text and "已清空" in text
    assert "没能还原" in text
    assert report.KIND_MISSING not in text and report.KIND_COPY_FAILED not in text


def test_restore_csv_carries_the_skip_reason():
    """目标端没清成的原因必须进导出——那正是拿到报告的人要追的东西。"""
    from dmig.core.migrator import RestoreOutcome
    outcome = RestoreOutcome(ok=True, source=r"C:\a", source_restored=True,
                             target=r"D:\b", target_state="skipped",
                             target_note="目标端有 2 项不属于这次迁移的内容")
    text = "\n".join(",".join(row) for row in report.build_restore_rows(outcome))
    assert "不属于这次迁移" in text


def test_csv_includes_a_blocked_step():
    """界面看得到的「未完成步骤」，导出的 CSV 里也要有。

    CSV 是完整存档（界面为防刷屏只显示前若干条），漏掉它会让导出的报告
    看不出这一趟到底卡在哪一步——而那往往正是整趟失败的唯一原因。
    """
    outcome = _outcome(ok=False, phase="rename_failed",
                       blocked_path=r"C:\wwwwwww",
                       blocked_reason="拒绝访问（WinError 5）",
                       blocked_process="WPS PDF (12208)")
    rows = report.build_rows(outcome)
    assert any(report.KIND_BLOCKED in str(row) for row in rows), \
        f"CSV 里没有未完成步骤：{rows}"


def test_blocked_row_without_a_path_still_shows_up():
    """查不出具体路径时也要有条目，否则又回到「红标题配空表」。"""
    rows = report.blocked_rows(_outcome(blocked_reason="需要管理员权限"))
    assert rows[0][1] == "整个源目录"
    assert rows[0][2] == "需要管理员权限"


def test_summarize_counts_everything():
    stats = report.summarize(_outcome())
    assert stats == {"copied_files": 2, "copied_bytes": 1003, "skipped": 1,
                     "failed": 1, "missing": 1, "mismatch": 1, "dangling": 0}


def test_summarize_tolerates_missing_parts():
    stats = report.summarize(MigrateOutcome(ok=True, phase="done"))
    assert stats == {"copied_files": 0, "copied_bytes": 0, "skipped": 0,
                     "failed": 0, "missing": 0, "mismatch": 0, "dangling": 0}


def test_unfinished_rows_translates_reason():
    rows = report.unfinished_rows(_outcome())
    assert rows[0] == ["未复制", "x\\y.db", "文件被占用", "chrome.exe (1234)"]
    assert rows[1] == ["缺失", "m.txt", "目标不存在", ""]
    assert rows[2] == ["不一致", "n.bin", "大小或内容不同", ""]


def test_unknown_reason_is_kept_verbatim():
    bad = _outcome(copy=CopyResult(failures=[CopyFailure("a", "link_failed: boom")]))
    assert report.unfinished_rows(bad)[0][2] == "link_failed: boom"


def test_build_rows_prepends_header():
    rows = report.build_rows(_outcome())
    assert rows[0] == report.CSV_HEADER
    assert len(rows) == 4


def test_write_csv_is_excel_friendly(tmp_path):
    target = tmp_path / "报告.csv"
    report.write_csv(target, report.build_rows(_outcome()))
    text = target.read_text(encoding="utf-8-sig")
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["类别", "相对路径", "原因", "占用进程"]
    assert parsed[1][1] == "x\\y.db"


# --- C5：链接的断链与重写 ---------------------------------------------

_LINKS = [
    # 被改写过目标
    {"rel": "cache", "kind": "junction",
     "raw_target": "D:\\npm-cache", "new_target": "E:\\npm-cache",
     "is_dangling": False},
    # 断链：源里就已经指着空气，工具照样原样重建
    {"rel": "gone", "kind": "junction",
     "raw_target": "D:\\vanished", "new_target": "D:\\vanished",
     "is_dangling": True},
    # 既没断也没改写：相对路径原样保留
    {"rel": "kept", "kind": "symlink",
     "raw_target": "..\\sibling", "new_target": "..\\sibling",
     "is_dangling": False},
]


def test_summarize_counts_dangling_links():
    assert report.summarize(_outcome(links=_LINKS))["dangling"] == 1


def test_dangling_links_reach_the_unfinished_list():
    """断链要出现在报告清单里，用户才会知道它本来就指着空气，不是迁移弄坏的。"""
    rows = report.unfinished_rows(_outcome(links=_LINKS))
    assert [report.KIND_DANGLING, "gone", report.DANGLING_REASON, ""] in rows


def test_link_rows_keep_the_before_and_after_values():
    rows = report.link_rows(_outcome(links=_LINKS))
    by_rel = {row[1]: row for row in rows}
    assert by_rel["cache"] == ["链接重写", "cache",
                               "D:\\npm-cache → E:\\npm-cache", ""]
    assert by_rel["gone"][0] == report.KIND_DANGLING
    assert "目标不存在，已原样重建：D:\\vanished → D:\\vanished" in by_rel["gone"][2]
    # 既没断也没改写的链接不进清单，否则几万条「原样保留」会把要看的淹掉
    assert "kept" not in by_rel


def test_csv_carries_the_link_table():
    rows = report.build_rows(_outcome(links=_LINKS))
    assert rows[0] == report.CSV_HEADER
    kinds = [row[0] for row in rows]
    assert report.KIND_REWRITTEN in kinds
    assert report.KIND_DANGLING in kinds


def test_outcome_without_links_is_tolerated():
    """老会话文件、以及测试里的假 Migrator 都不会带 links。"""
    assert report.link_rows(MigrateOutcome(ok=True, phase="done")) == []
    assert report.summarize(MigrateOutcome(ok=True, phase="done"))["dangling"] == 0


# --- C9：复制保不住的属性与目录时间戳 ---------------------------------

def test_lossy_attributes_are_disclosed_in_one_row():
    """稀疏/硬链接会改变结果；ADS 会保留——三种都写出来，说法不能反。"""
    rows = report.attribute_rows(
        _outcome(sparse_files=2, hardlinked_files=5, ads_files=12))
    assert len(rows) == 1
    assert rows[0][0] == report.KIND_ATTRS
    assert "2 个稀疏文件会被实化" in rows[0][2]
    assert "5 个硬链接会变成各自独立的副本" in rows[0][2]
    assert "12 个文件带附加数据流" in rows[0][2]
    # P3 回归：这条原先反着说，与内核整文件复制会把 ADS 带过去的事实不符
    assert "复制后会一并保留" in rows[0][2]
    assert "附加数据流（ADS），复制后不保留" not in rows[0][2]


def test_no_lossy_attributes_means_no_row():
    assert report.attribute_rows(_outcome()) == []


def test_lossy_attributes_reach_the_lists():
    outcome = _outcome(ads_files=3)
    assert report.attribute_rows(outcome)[0] in report.unfinished_rows(outcome)
    assert report.attribute_rows(outcome)[0] in report.build_rows(outcome)


def test_dir_time_failures_are_listed_per_path():
    rows = report.failure_rows(_outcome(copy=CopyResult(dir_times_failed=["a\\b"])))
    assert rows[0] == [report.KIND_DIR_TIME, "a\\b", report.DIR_TIME_REASON, ""]


# --- C4：被排除的条目 -------------------------------------------------

def test_excluded_items_are_listed_with_the_backup_warning():
    """被排除的内容没复制到目标、只存在于备份里，删备份前用户必须有据可查。"""
    rows = [row for row in report.failure_rows(_outcome(excluded=["locked", "a\\b"]))
            if row[0] == report.KIND_EXCLUDED]
    assert rows == [["已跳过", "locked", report.EXCLUDED_REASON, ""],
                    ["已跳过", "a\\b", report.EXCLUDED_REASON, ""]]
    assert "只存在于备份里" in report.EXCLUDED_REASON


def test_nothing_excluded_means_no_rows():
    assert [row for row in report.failure_rows(_outcome())
            if row[0] == report.KIND_EXCLUDED] == []
