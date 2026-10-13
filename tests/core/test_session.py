import json

from dmig.core import session as session_mod
from dmig.core.session import Session


def test_create_sets_fields_and_persists(tmp_path):
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    assert s.path.exists()
    assert s.phase == "created"
    data = json.loads(s.path.read_text(encoding="utf-8"))
    assert data["source"] == r"D:\src"
    assert data["target"] == r"E:\dst"


def test_mark_done_accumulates_and_flushes(tmp_path):
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    for i in range(3):
        s.mark_done(f"f{i}.bin", 100)
    s.save()
    reloaded = Session.load(s.path)
    assert reloaded.done["files"] == 3
    assert reloaded.done["bytes"] == 300


def test_mark_failed_records_reason_and_process(tmp_path):
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    s.mark_failed("a.db", "locked", "chrome.exe (1234)")
    s.save()
    reloaded = Session.load(s.path)
    assert reloaded.failed_rels() == {"a.db"}
    assert reloaded.failed[0]["process"] == "chrome.exe (1234)"


def test_begin_run_clears_stale_records_but_keeps_done(tmp_path):
    """续传重开一趟时：上一轮的 failed/links 描述的是当时的现场，要清掉；
    `.done` 记账是「哪些文件是我们自己复制的」的唯一依据，绝不能清。"""
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    s.mark_failed("a.db", "locked")
    s.links.append({"rel": "nm", "kind": "junction"})
    s.mark_done("two.bin", 20)
    s.save()

    s.begin_run("resumed")
    assert s.failed == []
    assert s.links == []
    assert s.phase == "resumed"
    assert s.completed_rels() == {"two.bin"}


def test_load_rejects_corrupt_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert Session.load(bad) is None


def test_default_root_is_under_localappdata():
    root = session_mod.default_root()
    assert root.name == "sessions"
    assert root.parent.name == "DataMigrationTool"


def test_completed_rels_roundtrip(tmp_path):
    """A2：续传只能靠这份记账判断「哪些文件是我们自己复制的」。"""
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    assert s.completed_rels() == set()
    s.mark_done(r"a\one.txt", 10)
    s.mark_done("two.bin", 20)
    s.save()
    assert s.completed_rels() == {r"a\one.txt", "two.bin"}
    reloaded = Session.load(s.path)
    assert reloaded.completed_rels() == {r"a\one.txt", "two.bin"}


def test_done_log_is_an_append_only_sidecar(tmp_path):
    """50 万个文件不能塞进每次都要整体重写的 JSON，必须走追加式旁挂文件。"""
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    for i in range(3):
        s.mark_done(f"f{i}.bin", 1)
    s.flush_done()
    assert s.done_log.suffix == ".done"
    assert s.done_log.read_text(encoding="utf-8").splitlines() == [
        "f0.bin", "f1.bin", "f2.bin"]
    assert "f0.bin" not in json.dumps(s.to_dict())


def test_done_sidecar_flushes_finer_than_the_json(tmp_path):
    """`.done` 的落盘阈值必须比 JSON 重写细，否则小树中断后等于全量重来。

    原来两个阈值绑在一起（500 文件 / 5 秒），小于 500 文件的树在整个复制期间
    一次都不落盘——实测 480 个文件被杀在复制中：copied=480 skipped=0。
    这里断言：复制到第 FLUSH_DONE_EVERY 个文件时 .done 已经落盘，
    而会话 JSON 的 done 计数还是 0（说明走的是只追加那条便宜路径、
    没有触发整体重写）。
    """
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    n = session_mod.FLUSH_DONE_EVERY
    assert n < session_mod.FLUSH_EVERY_FILES, "细阈值必须真的更细才有意义"

    for i in range(n):
        s.mark_done(f"f{i}.bin", 1)

    lines = s.done_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n, f"到第 {n} 个文件时 .done 应当已落盘，实际 {len(lines)} 行"
    # JSON 没被重写：只有 create() 那次留下的全零计数
    assert json.loads(s.path.read_text(encoding="utf-8"))["done"]["files"] == 0
    # 中断后重开也能读到这份记账
    assert Session.load(s.path).completed_rels() == {f"f{i}.bin" for i in range(n)}


def test_completed_rels_tolerates_missing_sidecar(tmp_path):
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    s.done_log.unlink(missing_ok=True)
    assert s.completed_rels() == set()


def test_run_options_roundtrip(tmp_path):
    """C6：运行参数要写进会话，续传才能照原样重放（尤其是链接重写映射）。"""
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    s.options = {"mappings": {r"C:\cache": r"E:\cache"}, "deep_verify": True,
                 "skip_unknown_reparse": False}
    s.save()

    reloaded = Session.load(s.path)
    assert reloaded.options == s.options


def test_options_defaults_to_empty_for_old_session_files(tmp_path):
    """升级前写的会话文件里没有 options 键，读出来必须是空字典而不是崩掉。"""
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    data = json.loads(s.path.read_text(encoding="utf-8"))
    data.pop("options")
    s.path.write_text(json.dumps(data), encoding="utf-8")
    assert Session.load(s.path).options == {}
