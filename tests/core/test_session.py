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


def test_reset_failures_clears_list(tmp_path):
    s = Session.create(r"D:\src", r"E:\dst", root=tmp_path)
    s.mark_failed("a.db", "locked")
    s.reset_failures()
    assert s.failed == []


def test_load_rejects_corrupt_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert Session.load(bad) is None


def test_default_root_is_under_localappdata():
    root = session_mod.default_root()
    assert root.name == "sessions"
    assert root.parent.name == "DataMigrationTool"
