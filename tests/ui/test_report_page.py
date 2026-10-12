import csv
import io
import sys

import pytest
from PySide6.QtGui import QColor

from dmig.core.models import CopyFailure, CopyResult, VerifyResult
from dmig.core.migrator import MigrateOutcome
from dmig.ui import theme
from dmig.ui.report_page import ReportPage

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _outcome(**kwargs):
    defaults = dict(
        ok=False, phase="copy_failed",
        copy=CopyResult(copied_files=2, copied_bytes=1003, skipped=1,
                        failures=[CopyFailure(rel="x\\y.db", reason="locked",
                                              process="chrome.exe (1234)")]),
        verify=VerifyResult(ok=False, missing=["m.txt"]),
        backup_path=r"E:\src.bak_20261009120000",
        message="2 个文件未能复制",
    )
    defaults.update(kwargs)
    return MigrateOutcome(**defaults)


@pytest.fixture
def page(qtbot):
    widget = ReportPage()
    qtbot.addWidget(widget)
    return widget


def test_summary_is_rendered(page):
    page.load(_outcome())
    assert page.summary_text("copied_files") == "2"
    assert page.summary_text("copied_bytes") == "1003 B"
    assert page.summary_text("skipped") == "1"
    assert page.summary_text("failed") == "1"
    assert page.summary_text("missing") == "1"
    assert page.summary_text("mismatch") == "0"
    assert page.message_text() == "2 个文件未能复制"


def test_title_translates_the_phase_key(page):
    """标题里不能再出现内部阶段键。

    真实反馈：报告页顶着 60pt 的红字「未完成：rename_failed」，下面还挂着
    Python 异常原文。原来这条测试断言的正是「标题里有 copy_failed」——
    把 bug 当成规格钉住了。
    """
    page.load(_outcome(ok=False, phase="rename_failed"))
    assert page.title_text() == "未完成：源目录改名失败"
    assert "rename_failed" not in page.title_text()


def test_title_keeps_an_unknown_phase(page):
    """没收录的阶段键原样显示：宁可露出生词，也不能把信息吞掉。"""
    page.load(_outcome(ok=False, phase="copy_failed"))
    assert page.title_text() == "未完成：copy_failed"


def test_success_title(page):
    page.load(_outcome(ok=True, phase="done"))
    assert page.title_text() == "迁移成功"


# --- 备份还在时必须警告「别手动删」 -----------------------------------

def test_backup_warning_tells_users_not_to_delete_it_by_hand(page):
    """备份还在时要讲清：不确定就别删。

    用户反馈过两版措辞都不对，最后定下来的是这三点：
      1. 请勿在不确定的情况下删除备份（判据是「确定 / 不确定」，不是「手动 / 用工具」，
         用本页的「删除备份」按钮删同样失去回滚能力）
      2. 删除备份后将无法再进行回滚
      3. 回滚必须依靠备份文件（这是根本原因，必须写出来）
    """
    page.load(_outcome(ok=True, phase="done",
                       backup_path=r"C:\wwwwww.bak_20261011163623"))

    text = page.backup_warning_text()
    assert "请勿在不确定的情况下删除备份" in text
    assert r"C:\wwwwww.bak_20261011163623" in text, "要指明是哪个目录"
    assert "回滚必须依靠备份" in text, "要写出根本原因"
    assert "删除备份后将无法再进行回滚" in text, "要直说后果"


def test_no_warning_when_there_is_no_backup(page):
    """没有备份就没有可手删的东西，别挂一句无意义的警告。"""
    page.load(_outcome(ok=True, phase="done", backup_path=None))
    assert page.backup_warning_text() == ""
    page.load(_outcome(ok=True, phase="done", backup_path=""))
    assert page.backup_warning_text() == ""


# --- 回滚之后：这一页该讲「恢复」，不是「迁移」 -------------------------

def _restore(**kwargs):
    from dmig.core.migrator import RestoreOutcome
    defaults = dict(ok=True, source=r"C:\wwwwww", source_restored=True,
                    backup_path=r"C:\wwwwww.bak_20261011",
                    target=r"D:\myapp\1", target_state="cleared")
    defaults.update(kwargs)
    return RestoreOutcome(**defaults)


def test_restored_page_says_restore_not_migration(page):
    """回滚之后不能再顶着「迁移成功」。

    那件事已经被撤销了：源路径回来了、目标端那份也清掉了。继续显示「迁移成功」
    会让人以为迁移还生效着。
    """
    page.load(_outcome(ok=True, phase="done"))
    assert page.title_text() == "迁移成功"

    page.load_restored(_restore())

    assert page.title_text() == "恢复成功"
    assert page.is_showing_restore() is True


def test_restored_page_shows_restore_facts_not_migration_stats(page):
    """统计网格讲的是「搬过去多少」，恢复之后该讲「原路径和目标端怎么样了」。"""
    page.load_restored(_restore())

    assert "已恢复" in page.restore_text("source")
    assert r"C:\wwwwww" in page.restore_text("source")
    assert "改回原名" in page.restore_text("backup")
    assert "已清空" in page.restore_text("target")
    assert r"D:\myapp\1" in page.restore_text("target")
    # 迁移的统计与未完成清单都不该再占版面
    assert page.stats_visible() is False
    assert page.table_visible() is False


def test_restored_page_reports_kept_target(page):
    page.load_restored(_restore(target_state="kept"))
    assert "保留" in page.restore_text("target")


def test_restored_page_reports_why_the_target_was_skipped(page):
    page.load_restored(_restore(target_state="skipped",
                                target_note="目标端有 2 项不属于这次迁移的内容"))
    assert "不属于这次迁移" in page.restore_text("target")


def test_restored_page_reports_acl_leftovers(page):
    """权限没还原干净是必须说出来的：那是留在源目录上的实质改动。"""
    page.load_restored(_restore(acl_note="2 个目录的原始权限没能还原"))
    assert "没能还原" in page.restore_text("acl")


def test_restored_page_export_uses_restore_rows(page, tmp_path):
    """恢复态导出的必须是恢复信息，不是那份已经作废的迁移报告。"""
    import csv as _csv
    page.load_restored(_restore())
    out = tmp_path / "r.csv"
    page.export_csv(out)

    text = out.read_text(encoding="utf-8-sig")
    assert "恢复" in text
    assert "已清空" in text
    assert "未复制" not in text and "缺失" not in text


def test_restored_page_clears_the_backup_warning_text(page):
    """警告要连文字一起清，不能只 hide。

    留着旧文字等于把一份过期的告警藏在版面底下：警告说的是「请勿删除备份」，
    而回滚已经把备份改回原名、那个目录根本不存在了。
    """
    page.load(_outcome(ok=True, phase="done",
                       backup_path=r"C:\wwwwww.bak_20261011"))
    assert page.backup_warning.text() != ""

    page.load_restored(_restore())

    assert page.backup_warning.text() == ""
    assert page.backup_warning.isHidden() is True


def test_restored_page_clears_stale_migration_stats(page):
    """统计标签不能留着上一次的数值。

    卡片虽然藏起来了，`summary_text()` 这个读数口还会读到一份和当前版面无关的
    旧数据——「恢复成功」那一页底下藏着「成功复制 2 个」是自相矛盾的。
    """
    page.load(_outcome(ok=True, phase="done"))
    assert page.summary_text("copied_files") == "2"

    page.load_restored(_restore())

    assert page.summary_text("copied_files") == "-"


def test_restored_page_disables_reverify(page):
    """恢复之后没有「迁移结果」可校验。

    那一刻源是刚还原回来的原件、目标端是清空或半残的副本，两边早已不是同一次
    搬迁的两端，比出来的全是假的缺失。按钮留着只会给出一份没意义的报告。
    """
    page.load(_outcome(ok=True, phase="done"))
    assert page.reverify_button.isEnabled() is True

    page.load_restored(_restore())

    assert page.reverify_button.isEnabled() is False


def test_going_back_to_a_migration_report_restores_everything(page):
    """先回滚再重新加载迁移结果时，版面与按钮都要回到迁移态。"""
    page.load_restored(_restore())
    page.load(_outcome(ok=True, phase="done"))

    assert page.title_text() == "迁移成功"
    assert page.is_showing_restore() is False
    assert page.stats_visible() is True
    assert page.table_visible() is True
    assert page.reverify_button.isEnabled() is True


def test_failed_restore_says_so(page):
    page.load_restored(_restore(ok=False, message="没有可还原的备份"))
    assert page.title_text() == "恢复未完成"
    assert page.message_text() == "没有可还原的备份"


def test_rows_show_unfinished_items(page):
    page.load(_outcome())
    assert page.rows()[0] == ["未复制", "x\\y.db", "文件被占用", "chrome.exe (1234)"]
    assert page.rows()[1] == ["缺失", "m.txt", "目标不存在", ""]


def test_export_csv_writes_expected_content(page, tmp_path):
    page.load(_outcome())
    target = tmp_path / "报告.csv"
    page.export_csv(target)
    parsed = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8-sig"))))
    assert parsed[0] == ["类别", "相对路径", "原因", "占用进程"]
    assert parsed[1][1] == "x\\y.db"
    assert parsed[1][2] == "文件被占用"
    assert parsed[1][3] == "chrome.exe (1234)"
    assert parsed[2][0] == "缺失"


def test_buttons_follow_backup_presence(page):
    page.load(_outcome(backup_path=None))
    assert page.cleanup_button.isEnabled() is False
    assert page.rollback_button.isEnabled() is False
    page.load(_outcome())
    assert page.cleanup_button.isEnabled() is True
    assert page.rollback_button.isEnabled() is True


def test_rollback_and_cleanup_signals(page, qtbot):
    page.load(_outcome())
    with qtbot.waitSignal(page.rollback_requested, timeout=2000):
        page.rollback_button.click()
    with qtbot.waitSignal(page.cleanup_requested, timeout=2000):
        page.cleanup_button.click()


def test_reverify_signal(page, qtbot):
    page.load(_outcome())
    with qtbot.waitSignal(page.reverify_requested, timeout=2000):
        page.reverify_button.click()


def test_discard_enabled_only_for_partial_target(page):
    """B1 的出口：源完好、目标半份时可清；已成功或已改名时不可清。"""
    page.load(_outcome(backup_path=None))
    assert page.discard_button.isEnabled() is True

    page.load(_outcome())                     # 有备份 = 已经改过名
    assert page.discard_button.isEnabled() is False

    page.load(_outcome(ok=True, phase="done"))
    assert page.discard_button.isEnabled() is False


def test_discard_button_emits_signal(page, qtbot):
    page.load(_outcome(backup_path=None))
    with qtbot.waitSignal(page.discard_requested, timeout=2000):
        page.discard_button.click()


def test_fatal_reasons_are_translated(page):
    """磁盘写满这类中止原因不能直接把英文代号甩给用户。"""
    page.load(_outcome(
        phase="aborted", backup_path=None,
        copy=CopyResult(copied_files=0, failures=[
            CopyFailure(rel="big.bin", reason="disk_full")]),
        message="目标卷空间不足，已立即停止"))
    assert page.rows()[0] == ["未复制", "big.bin", "目标卷空间不足", ""]


# --- C5：断链在报告里标红 ---------------------------------------------

_DANGLING_LINKS = [
    {"rel": "gone", "kind": "junction", "raw_target": "D:\\vanished",
     "new_target": "D:\\vanished", "is_dangling": True},
]


def test_dangling_row_is_painted_red(page):
    """断链不标红的话，用户会以为是迁移把链接弄坏了。"""
    page.load(_outcome(links=_DANGLING_LINKS))
    rows = page.rows()
    assert rows[-1][0] == "断链"
    index = len(rows) - 1
    assert page.table.item(index, 0).foreground().color() == \
        QColor(theme.error_color())
    # 完整指向挂在 tooltip 上：表格里只放结论，免得把「相对路径」列挤没
    assert "D:\\vanished" in page.table.item(index, 1).toolTip()


def test_dangling_count_is_a_stat(page):
    page.load(_outcome(links=_DANGLING_LINKS))
    assert page.summary_text("dangling") == "1"


def test_non_dangling_rows_keep_default_color(page):
    page.load(_outcome())
    assert page.table.item(0, 0).foreground().color() != \
        QColor(theme.error_color())


def test_dangling_links_reach_the_exported_csv(page, tmp_path):
    page.load(_outcome(links=_DANGLING_LINKS))
    target = tmp_path / "报告.csv"
    page.export_csv(target)
    text = target.read_text(encoding="utf-8-sig")
    assert "断链" in text
    assert "D:\\vanished" in text


# --- D8：运行日志入口 -------------------------------------------------

def test_report_shows_the_run_log_path(page):
    page.load(_outcome(log_path=r"E:\sessions\20261010-101010-ab12.log"))
    assert page.log_label.isVisibleTo(page) is True
    assert r"E:\sessions\20261010-101010-ab12.log" in page.log_label.text()


def test_report_hides_the_log_line_when_there_is_none(page):
    """没有日志时不留一个空行——老会话、以及测试里的假 outcome 都没有。"""
    page.load(_outcome())
    assert page.log_label.isVisibleTo(page) is False
    assert page.log_label.text() == ""


# --- C9：复制保不住的属性 ---------------------------------------------

def test_lossy_attributes_get_a_row(page):
    page.load(_outcome(hardlinked_files=482, ads_files=7))
    rows = page.rows()
    assert rows[-1][0] == "未保留属性"
    assert "482 个硬链接" in rows[-1][2]
    assert "7 个文件带附加数据流" in rows[-1][2]


def test_nothing_lossy_means_no_extra_row(page):
    page.load(_outcome())
    assert all(row[0] != "未保留属性" for row in page.rows())


def test_dir_time_failure_gets_a_row(page):
    page.load(_outcome(copy=CopyResult(dir_times_failed=["a\\b"])))
    assert ["目录时间戳", "a\\b", "目录时间戳未能写回，该目录会显示为迁移时刻", ""] \
        in page.rows()


# --- 导出失败必须给反馈 ------------------------------------------------

def test_export_failure_is_reported_not_silent(page, monkeypatch, tmp_path):
    """导出失败必须告诉用户原因。

    原来 `_on_export_clicked` 直接调 `export_csv`：写入失败时异常逃出 Qt 槽函数，
    应用不崩（PySide6 只把 traceback 打到 stderr），但界面上什么都不显示——
    用户点了「导出 CSV」毫无反应，也没有原因。导出到被 Excel 占用的文件、
    只读目录、不存在的路径时都会这样。

    这里打在 Qt 类本身上（而不是 report_page 模块的名字上），
    这样即使生产代码压根没引入 QMessageBox，失败原因也是「没有反馈」而不是导入错误。
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    page.load(_outcome())
    bad = tmp_path / "no_such_dir" / "deep" / "out.csv"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(bad), "CSV 文件 (*.csv)")))
    warned = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: warned.append(a) or None))

    page._on_export_clicked()          # 不允许抛异常

    assert warned, "导出失败应当弹窗告知，而不是静默"
    assert not bad.exists()
    assert "已导出" not in page.message_text()


def test_export_success_still_reports_the_path(page, monkeypatch, tmp_path):
    """成功的路径不能被上面的兜底改坏。"""
    from PySide6.QtWidgets import QFileDialog

    page.load(_outcome())
    good = tmp_path / "out.csv"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(good), "CSV 文件 (*.csv)")))

    page._on_export_clicked()

    assert good.is_file()
    assert "已导出" in page.message_text()


def test_export_failure_of_any_kind_is_reported(page, monkeypatch, tmp_path):
    """兜底不能只接 OSError：路径里混进空字节时 `open()` 抛的是 ValueError。

    只 catch OSError 的话这个异常照样逃出 Qt 槽函数，D4 的修复就留了个洞。
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    page.load(_outcome())
    weird = str(tmp_path) + "\\x\x00y.csv"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (weird, "CSV 文件 (*.csv)")))
    warned = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: warned.append(a) or None))

    page._on_export_clicked()          # 不允许抛异常

    assert warned, "任何导出失败都应当告知用户"
    assert "失败" in page.message_text()
