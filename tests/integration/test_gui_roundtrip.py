"""端到端：真实 Migrator 走真实主窗口。

`tests/ui/test_main_window.py` 用的是假 Migrator，「计划页 → 工厂 → 核心」这段
接线它一条都测不出来——链接重写映射在执行时被静默丢弃（A1）就是这么漏过去的。
这里只把 session_root 指到 tmp_path，避免测试往真实的
`%LOCALAPPDATA%\\DataMigrationTool\\sessions` 里拉文件，其余全走产品代码。
"""
import os
import sys

import pytest
from PySide6.QtWidgets import QMessageBox

from dmig.core import copier, reparse
from dmig.core.fsutil import reparse_kind, strip_long_prefix
from dmig.core.migrator import Migrator
from dmig.ui import main_window as main_window_mod
from dmig.ui.main_window import PAGE_PLAN, PAGE_PROGRESS, MainWindow

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


class _Dialog:
    """QMessageBox 替身：真实弹窗是模态的，在 qtbot 里会一直转到超时。"""

    StandardButton = QMessageBox.StandardButton

    def __init__(self):
        self.texts: list[str] = []

    def warning(self, parent, title, text, *buttons, **kwargs):
        self.texts.append(text)
        return self.StandardButton.Ok

    def information(self, parent, title, text, *buttons, **kwargs):
        self.texts.append(text)
        return self.StandardButton.Ok


def _factory(tmp_path):
    sessions = tmp_path / "sessions"

    def make(source, target, mappings, deep_verify, session_root,
             skip_unknown_reparse=False, excludes=()):
        return Migrator(source, target, mappings=mappings, deep_verify=deep_verify,
                        session_root=sessions,
                        skip_unknown_reparse=skip_unknown_reparse,
                        excludes=excludes)

    return make


def _scenario(tmp_path, make_reparse_file):
    """真实文件 + 指向树外仓库的 junction + 一个读不动的重解析点。"""
    src = tmp_path / "profiles"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "a.txt").write_text("hello", encoding="utf-8")

    cache_root = tmp_path / "cache-root"
    outside = cache_root / "npm-cache"
    outside.mkdir(parents=True)
    (outside / "pkg.dat").write_text("repo data", encoding="utf-8")
    reparse.create_junction(src / "store", outside)

    make_reparse_file(src / "app.exe")
    return src, cache_root, outside


def _wait(window, qtbot):
    qtbot.waitUntil(lambda: not window.is_busy(), timeout=30000)


def test_gui_applies_mapping_and_copies_reparse(qtbot, tmp_path, make_reparse_file):
    src, cache_root, outside = _scenario(tmp_path, make_reparse_file)
    # 必须现在就取：迁移成功后源路径会变成指向目标的链接，那时再读就是拿目标跟自己比
    expected_raw = reparse.read_raw(src / "app.exe")
    moved = tmp_path / "moved-cache"
    dst = tmp_path / "E" / "profiles"

    window = MainWindow(migrator_factory=_factory(tmp_path))
    qtbot.addWidget(window)
    window.select_page.set_source(str(src))
    window.select_page.set_target(str(dst))

    window.go_next()                                  # ① 选择 → ② 计划
    _wait(window, qtbot)
    assert window.current_index() == PAGE_PLAN
    assert window.plan_page.group_rows()[0][0] == str(cache_root)
    assert window.plan_page.unknown_count() == 1

    window.plan_page.set_mapping(0, str(moved))
    window.plan_page.apply_button.click()             # 应用映射并重新扫描
    _wait(window, qtbot)
    # 重扫之后用户填的值还在（修复前这里会被清空，映射随之丢失）
    assert window.plan_page.mappings() == {str(cache_root): str(moved)}
    assert window.plan_page.group_rows()[0][2] == str(moved)

    window.go_next()                                  # ② 计划 → ③ 执行
    _wait(window, qtbot)
    outcome = window._outcome
    assert outcome.ok, f"{outcome.phase}：{outcome.message}"

    # 链接按用户填的映射重写，而不是指回原来的仓库
    link_target = strip_long_prefix(os.readlink(str(dst / "store")))
    assert link_target.startswith(str(moved)), link_target
    assert not link_target.startswith(str(cache_root))

    # 读不动的重解析点被原样搬到了目标端
    assert reparse.read_raw(dst / "app.exe") == expected_raw
    assert (dst / "docs" / "a.txt").read_text(encoding="utf-8") == "hello"

    # 源路径已成为指向目标的链接
    assert os.path.exists(src / "docs" / "a.txt")
    assert reparse_kind(os.lstat(str(src))) is not None

    assert window.report_page.title_text() == "迁移成功"
    assert window._migrator.cleanup_backup() is True
    assert not os.path.exists(outcome.backup_path)



def test_gui_overwrites_same_size_residue(qtbot, tmp_path):
    """A2 现场回归：目标盘上的同大小残留必须被真实内容覆盖。

    残留内容与源同大小、且刻意设成与源相同的时间戳——这是最坏情况，
    任何基于大小/时间戳的启发式都会把它误判成「已复制」。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "db.bin").write_bytes(b"A" * 4096)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "db.bin").write_bytes(b"B" * 4096)
    st = os.stat(src / "db.bin")
    os.utime(dst / "db.bin", ns=(st.st_atime_ns, st.st_mtime_ns))

    window = MainWindow(migrator_factory=_factory(tmp_path))
    qtbot.addWidget(window)
    window.select_page.set_source(str(src))
    window.select_page.set_target(str(dst))
    window.go_next()
    _wait(window, qtbot)
    assert window.current_index() == PAGE_PLAN
    assert "非空" in window.plan_page.warning_text()

    window.go_next()
    _wait(window, qtbot)
    outcome = window._outcome
    assert outcome.ok, f"{outcome.phase}：{outcome.message}"
    assert (dst / "db.bin").read_bytes() == b"A" * 4096
    assert outcome.copy.skipped == 0


def test_gui_resumes_from_a_restored_session(qtbot, tmp_path, monkeypatch):
    """C1 端到端：从会话恢复的窗口要沿用 `.done` 记账，已复制的文件不再重复制。

    修复前 restore_session 只回填源/目标，execute() 里无条件 Session.create，
    续传因此退化成从头再来一遍——结果正确，但几十万个文件要白等一次全量时间。
    """
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(main_window_mod, "default_root", lambda: sessions)

    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "one.txt").write_text("one", encoding="utf-8")
    (src / "two.bin").write_bytes(b"2" * 4096)
    dst = tmp_path / "dst"

    real_copy_one = copier.copy_one

    def denied_for_two_bin(s, d):
        if str(s).endswith("two.bin"):
            raise PermissionError(13, "拒绝访问", str(s), 5)
        return real_copy_one(s, d)

    monkeypatch.setattr(copier, "RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(copier, "copy_one", denied_for_two_bin)
    first = MainWindow(migrator_factory=_factory(tmp_path))
    qtbot.addWidget(first)
    first.select_page.set_source(str(src))
    first.select_page.set_target(str(dst))
    first.go_next()
    _wait(first, qtbot)
    first.go_next()
    _wait(first, qtbot)
    assert first._outcome.phase == "copy_failed"
    assert (dst / "a" / "one.txt").read_text(encoding="utf-8") == "one"
    assert not (dst / "two.bin").exists()
    session_id = first._migrator.session.id

    monkeypatch.setattr(copier, "copy_one", real_copy_one)
    second = MainWindow(migrator_factory=_factory(tmp_path), session_id=session_id)
    qtbot.addWidget(second)
    assert "已记录 1 个文件" in second.status_line.text()
    assert second.select_page.source() == str(src)
    second.go_next()
    _wait(second, qtbot)
    assert second.current_index() == PAGE_PLAN
    second.go_next()
    _wait(second, qtbot)

    outcome = second._outcome
    assert outcome.ok, f"{outcome.phase}：{outcome.message}"
    assert outcome.copy.skipped == 1                  # one.txt 直接跳过
    assert (dst / "two.bin").read_bytes() == b"2" * 4096
    # 沿用同一个会话文件，而不是又新建一个孤儿会话
    assert second._migrator.session.id == session_id
    assert [p.name for p in sessions.glob("*.json")] == [f"{session_id}.json"]
    assert second.report_page.title_text() == "迁移成功"
    assert second._migrator.cleanup_backup() is True


def test_gui_resume_replays_the_link_mapping(qtbot, tmp_path, monkeypatch,
                                             make_reparse_file):
    """C6 端到端：续传要照原样重放会话里记的链接重写映射。

    第一趟带着映射跑、有一个文件复制失败；第二趟只给一个会话 ID，界面上
    什么都不填。修复前第二趟用空映射重建 Migrator，会把目标端已经指对的
    junction 删掉重建、指回迁移前的旧位置——而那个位置马上就要被改名成备份。
    """
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(main_window_mod, "default_root", lambda: sessions)
    src, cache_root, outside = _scenario(tmp_path, make_reparse_file)
    # 必须现在就取：迁移成功后源路径会变成指向目标的链接，那时再读就是拿目标跟自己比
    expected_raw = reparse.read_raw(src / "app.exe")
    moved = tmp_path / "moved-cache"
    dst = tmp_path / "E" / "profiles"

    real_copy_one = copier.copy_one

    def denied_for_a_txt(s, d):
        if str(s).endswith("a.txt"):
            raise PermissionError(13, "拒绝访问", str(s), 5)
        return real_copy_one(s, d)

    monkeypatch.setattr(copier, "RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(copier, "copy_one", denied_for_a_txt)
    first = MainWindow(migrator_factory=_factory(tmp_path))
    qtbot.addWidget(first)
    first.select_page.set_source(str(src))
    first.select_page.set_target(str(dst))
    first.go_next()
    _wait(first, qtbot)
    first.plan_page.set_mapping(0, str(moved))
    first.plan_page.apply_button.click()
    _wait(first, qtbot)
    first.go_next()
    _wait(first, qtbot)
    assert first._outcome.phase == "copy_failed"
    session_id = first._migrator.session.id

    monkeypatch.setattr(copier, "copy_one", real_copy_one)
    second = MainWindow(migrator_factory=_factory(tmp_path), session_id=session_id)
    qtbot.addWidget(second)
    assert "沿用 1 条链接映射" in second.status_line.text()
    assert second.plan_page.mappings() == {str(cache_root): str(moved)}
    second.go_next()
    _wait(second, qtbot)
    assert second.current_index() == PAGE_PLAN
    assert second.plan_page.group_rows()[0][2] == str(moved)
    second.go_next()
    _wait(second, qtbot)

    outcome = second._outcome
    assert outcome.ok, f"{outcome.phase}：{outcome.message}；{outcome.copy.failures}"
    assert (dst / "docs" / "a.txt").read_text(encoding="utf-8") == "hello"
    # 读不动的重解析点被第二趟重搬了一遍而不是失败：它的时间戳只是尽力而为，
    # 跳过判定没法为它作保，重搬是安全的退路。
    assert reparse.read_raw(dst / "app.exe") == expected_raw
    link_target = strip_long_prefix(os.readlink(str(dst / "store")))
    assert link_target.startswith(str(moved)), link_target
    assert not link_target.startswith(str(cache_root))
    assert second._migrator.session.id == session_id


# --- C1：重新校验的比对基准 -------------------------------------------

def _migrate_simple_tree(window, qtbot, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "db.bin").write_bytes(b"A" * 4096)
    dst = tmp_path / "dst"
    window.select_page.set_source(str(src))
    window.select_page.set_target(str(dst))
    window.go_next()
    _wait(window, qtbot)
    window.go_next()
    _wait(window, qtbot)
    return src, dst


def test_gui_reverify_compares_against_the_backup_after_migration(qtbot, tmp_path,
                                                                 monkeypatch):
    """修复前「重新校验」拿选择页上的源路径当原件——迁移成功后源已经是指向
    目标的链接，那就是拿目标和目标自己比，把目标端改坏了也照样报「通过」，
    用户会据此放心删掉备份。
    """
    monkeypatch.setattr(main_window_mod, "QMessageBox", _Dialog())
    window = MainWindow(migrator_factory=_factory(tmp_path))
    qtbot.addWidget(window)
    _migrate_simple_tree(window, qtbot, tmp_path)
    outcome = window._outcome
    assert outcome.ok, f"{outcome.phase}：{outcome.message}"
    dst = tmp_path / "dst"

    # 把目标端改坏：同大小，时间戳明确挪开——靠时钟拍子撞车来「不同」不可靠
    corrupted = dst / "db.bin"
    corrupted.write_bytes(b"B" * 4096)
    stamp = os.stat(os.path.join(outcome.backup_path, "db.bin")).st_mtime + 60
    os.utime(corrupted, (stamp, stamp))

    window.reverify()
    _wait(window, qtbot)

    assert window._reverify_base == outcome.backup_path
    assert window._reverify_base != str(window.select_page.source())
    assert window._reverify_result.ok is False
    assert "db.bin" in window._reverify_result.mismatch
    assert "重新校验未通过" in window.status_line.text()


def test_gui_reverify_passes_against_an_untouched_backup(qtbot, tmp_path,
                                                         monkeypatch):
    """基准换成备份之后，没被动过的目标端仍然应当报通过。"""
    monkeypatch.setattr(main_window_mod, "QMessageBox", _Dialog())
    window = MainWindow(migrator_factory=_factory(tmp_path))
    qtbot.addWidget(window)
    _migrate_simple_tree(window, qtbot, tmp_path)
    assert window._outcome.ok

    window.reverify()
    _wait(window, qtbot)

    assert window._reverify_result.ok is True
    assert "重新校验通过" in window.status_line.text()
    assert window._outcome.backup_path in window.status_line.text()


def test_gui_reverify_refuses_when_the_backup_is_gone(qtbot, tmp_path, monkeypatch):
    """原件都没了就如实说，绝不能返回一个假的通过。"""
    dialog = _Dialog()
    monkeypatch.setattr(main_window_mod, "QMessageBox", dialog)
    window = MainWindow(migrator_factory=_factory(tmp_path))
    qtbot.addWidget(window)
    _migrate_simple_tree(window, qtbot, tmp_path)
    assert window._migrator.cleanup_backup() is True      # 备份删了，源又已是链接

    window.reverify()

    assert "没有可比对的原件" in window.status_line.text()
    assert any("没有可比对的原件" in text for text in dialog.texts)
    assert window._reverify_result is None                # 一次校验都没跑
