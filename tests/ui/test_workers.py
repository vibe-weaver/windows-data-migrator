import time

from dmig.core.models import CopyResult, PlanTotals, Progress, VerifyResult
from dmig.core.migrator import MigrateOutcome
from dmig.ui.workers import MigrateWorker, PlanWorker, VerifyWorker


class _FakeMigrator:
    """只实现 workers 需要的两个方法，绝不碰真实文件系统。"""

    def __init__(self, plan=None, outcome=None, boom=False):
        self._plan = plan
        self._outcome = outcome
        self._boom = boom
        self.executed = 0

    def plan_it(self, on_progress=None):
        if self._boom:
            raise RuntimeError("扫描炸了")
        if on_progress:
            on_progress("a/b.txt")
        return self._plan

    def execute(self, *, on_progress=None, should_cancel=None, should_pause=None):
        self.executed += 1
        if self._boom:
            raise RuntimeError("执行炸了")
        if on_progress:
            on_progress(Progress(phase="copying", done_files=1, done_bytes=10,
                                 total_files=2, total_bytes=20, current="a/b.txt"))
        return self._outcome


def test_plan_worker_emits_plan(qtbot):
    worker = PlanWorker(_FakeMigrator(plan="PLAN"))
    with qtbot.waitSignal(worker.planned, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert blocker.args[0] == "PLAN"


def test_plan_worker_reports_failure(qtbot):
    worker = PlanWorker(_FakeMigrator(boom=True))
    with qtbot.waitSignal(worker.failed, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert "扫描炸了" in blocker.args[0]


def test_plan_worker_forwards_progress(qtbot):
    worker = PlanWorker(_FakeMigrator(plan="PLAN"))
    seen = []
    worker.progressed.connect(seen.append)
    with qtbot.waitSignal(worker.planned, timeout=5000):
        worker.start()
    worker.wait(5000)
    assert seen == ["a/b.txt"]


def test_migrate_worker_emits_outcome_and_progress(qtbot):
    outcome = MigrateOutcome(ok=True, phase="done")
    worker = MigrateWorker(_FakeMigrator(outcome=outcome))
    seen = []
    worker.progressed.connect(seen.append)
    with qtbot.waitSignal(worker.done, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert blocker.args[0] is outcome
    assert seen and seen[0].current == "a/b.txt"


def test_migrate_worker_reports_failure(qtbot):
    worker = MigrateWorker(_FakeMigrator(boom=True))
    with qtbot.waitSignal(worker.failed, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert "执行炸了" in blocker.args[0]


def test_migrate_worker_pause_flag_roundtrip(qtbot):
    worker = MigrateWorker(_FakeMigrator(outcome=MigrateOutcome(True, "done")))
    assert worker.paused is False
    worker.pause()
    assert worker.paused is True
    worker.resume()
    assert worker.paused is False


def test_migrate_worker_cancel_is_observed(qtbot):
    observed = {"cancelled": False}

    class _Slow:
        def execute(self, *, on_progress=None, should_cancel=None, should_pause=None):
            for _ in range(600):
                if should_cancel and should_cancel():
                    observed["cancelled"] = True
                    return CopyResult(cancelled=True)
                time.sleep(0.005)
            return CopyResult()

    worker = MigrateWorker(_Slow())
    with qtbot.waitSignal(worker.done, timeout=15000) as blocker:
        worker.start()
        qtbot.wait(80)
        worker.cancel()
    worker.wait(5000)
    assert observed["cancelled"] is True
    assert blocker.args[0].cancelled is True


def test_migrate_worker_pause_blocks_until_resumed(qtbot):
    ticks = []

    class _Tick:
        def execute(self, *, on_progress=None, should_cancel=None, should_pause=None):
            for _ in range(120):
                ticks.append("paused" if (should_pause and should_pause()) else "run")
                time.sleep(0.005)
            return CopyResult(copied_files=len(ticks))

    worker = MigrateWorker(_Tick())
    worker.start()
    qtbot.wait(60)
    worker.pause()
    qtbot.wait(80)
    assert worker.paused is True
    worker.resume()
    with qtbot.waitSignal(worker.done, timeout=15000):
        pass
    worker.wait(5000)
    assert ticks.count("paused") > 0
    assert ticks.count("run") > 0


def test_verify_worker_returns_result(qtbot, monkeypatch, tmp_path):
    import dmig.core.verifier as verifier_mod
    monkeypatch.setattr(verifier_mod, "verify",
                        lambda source, target, deep=False, **kw: VerifyResult(
                            ok=True, expected_files=3))
    worker = VerifyWorker(tmp_path / "src", tmp_path / "dst", deep=True)
    with qtbot.waitSignal(worker.verified, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert blocker.args[0].expected_files == 3


def test_verify_worker_reports_failure(qtbot, monkeypatch, tmp_path):
    import dmig.core.verifier as verifier_mod

    def _boom(source, target, deep=False, **kw):
        raise OSError("校验炸了")

    monkeypatch.setattr(verifier_mod, "verify", _boom)
    worker = VerifyWorker(tmp_path / "src", tmp_path / "dst")
    with qtbot.waitSignal(worker.failed, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert "校验炸了" in blocker.args[0]


def test_verify_worker_cancel_is_observed(qtbot, monkeypatch, tmp_path):
    """B6：校验原来不可取消，几十万个文件时只能干等或直接杀进程。"""
    import dmig.core.verifier as verifier_mod
    observed = {"cancelled": False}

    def _slow(source, target, deep=False, on_progress=None, should_cancel=None,
              totals=None, skip_unknown_reparse=False, excludes=()):
        for _ in range(600):
            if should_cancel and should_cancel():
                observed["cancelled"] = True
                return VerifyResult(ok=False, cancelled=True)
            time.sleep(0.005)
        return VerifyResult(ok=True)

    monkeypatch.setattr(verifier_mod, "verify", _slow)
    worker = VerifyWorker(tmp_path / "src", tmp_path / "dst")
    with qtbot.waitSignal(worker.verified, timeout=15000) as blocker:
        worker.start()
        qtbot.wait(80)
        worker.cancel()
    worker.wait(5000)
    assert observed["cancelled"] is True
    assert blocker.args[0].cancelled is True
    assert blocker.args[0].ok is False


def test_verify_worker_forwards_progress_and_options(qtbot, monkeypatch, tmp_path):
    import dmig.core.verifier as verifier_mod
    forwarded = {}

    def _fake(source, target, deep=False, on_progress=None, should_cancel=None,
              totals=None, skip_unknown_reparse=False, excludes=()):
        forwarded.update(deep=deep, totals=totals, skip=skip_unknown_reparse,
                         excludes=list(excludes))
        if on_progress:
            on_progress(Progress(phase="verifying", done_files=1, total_files=4))
            on_progress(Progress(phase="verifying", done_files=4, total_files=4))
        return VerifyResult(ok=True, expected_files=4)

    monkeypatch.setattr(verifier_mod, "verify", _fake)
    totals = PlanTotals(files=4, bytes=40, dirs=1)
    worker = VerifyWorker(tmp_path / "src", tmp_path / "dst", deep=True,
                          skip_unknown_reparse=True, totals=totals,
                          excludes=["D:\\locked"])
    seen = []
    worker.progressed.connect(seen.append)
    with qtbot.waitSignal(worker.verified, timeout=5000):
        worker.start()
    worker.wait(5000)
    assert [p.done_files for p in seen] == [1, 4]
    assert all(p.phase == "verifying" for p in seen)
    # excludes 必须转发：重新校验不跳过被排除的目录，会报一堆假的「缺失」
    assert forwarded == {"deep": True, "totals": totals, "skip": True,
                         "excludes": ["D:\\locked"]}
