import sys
from pathlib import Path

import pytest

from dmig.core.models import (EntryKind, LinkPlan, LinkTargetClass,
                              MigrationPlan, PlanTotals, TargetGroup)
from dmig.ui.plan_page import LOCK_HINT, PlanPage

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def _plan(**kwargs):
    plan = MigrationPlan(source=Path(r"D:\myapp\data"),
                         target=Path(r"E:\myapp\data"))
    plan.totals = PlanTotals(files=8, bytes=2054, dirs=3)
    plan.required_bytes = 2054
    plan.free_bytes = 336 * 1024 ** 3
    plan.links = [LinkPlan(rel="nm\\a", kind=EntryKind.JUNCTION,
                           raw_target=r"C:\cache\a",
                           new_target=r"C:\cache\a",
                           target_class=LinkTargetClass.OUTSIDE_SOURCE,
                           is_dir=True)]
    plan.link_groups = [TargetGroup(prefix=r"C:\Users\me\AppData\Local\npm-cache",
                                    count=482, mapped_to="")]
    plan.unknown_reparse = ["weird-link"]
    for key, value in kwargs.items():
        setattr(plan, key, value)
    return plan


@pytest.fixture
def page(qtbot):
    widget = PlanPage()
    qtbot.addWidget(widget)
    return widget


def test_metrics_are_rendered(page):
    page.load(_plan())
    assert page.metric("bytes") == "2.0 KB"
    assert page.metric("files") == "8"
    assert page.metric("dirs") == "3"
    assert page.metric("links") == "1"
    assert page.metric("elevation") == "0"
    assert page.metric("locks") == LOCK_HINT
    assert page.metric("eta") != "-"


def test_group_table_is_rendered(page):
    page.load(_plan())
    assert page.group_rows() == [(r"C:\Users\me\AppData\Local\npm-cache", 482, "")]


def test_warning_mentions_links_and_target(page):
    page.load(_plan())
    text = page.warning_text()
    assert "E:\\myapp\\data" in text
    assert "482" in text


def test_unknown_reparse_is_listed(page):
    page.load(_plan())
    assert page.unknown_count() == 1
    assert "未知重解析点" in page.warning_text()


def test_mapping_is_collected(page):
    page.load(_plan())
    page.set_mapping(0, r"E:\npm-cache")
    assert page.mappings() == {r"C:\Users\me\AppData\Local\npm-cache": r"E:\npm-cache"}


def test_identity_mapping_is_not_collected(page):
    page.load(_plan())
    page.set_mapping(0, r"C:\Users\me\AppData\Local\npm-cache")
    assert page.mappings() == {}


def test_blank_mapping_is_not_collected(page):
    page.load(_plan())
    page.set_mapping(0, "")
    assert page.mappings() == {}


def test_mapping_flows_into_migrator(page):
    from dmig.core.migrator import Migrator
    page.load(_plan())
    page.set_mapping(0, r"E:\npm-cache")
    migrator = Migrator(r"D:\myapp\data",
                        r"E:\myapp\data",
                        mappings=page.mappings())
    assert migrator.mappings == {
        r"C:\Users\me\AppData\Local\npm-cache": r"E:\npm-cache"}


def test_elevate_button_follows_permission_issues(page):
    page.load(_plan())
    assert page.elevate_button.isEnabled() is False
    page.load(_plan(permission_issues=[str(Path(r"D:\myapp\data\locked"))]))
    assert page.elevate_button.isEnabled() is True
    assert page.metric("elevation") == "1"
    assert "以管理员身份重试" in page.warning_text()


def test_elevate_button_disabled_when_already_elevated(page):
    page.load(_plan(permission_issues=["D:\\locked"]), already_elevated=True)
    assert page.elevate_button.isEnabled() is False
    assert "即使提权也访问不了" in page.warning_text()


def test_apply_button_emits_signal(page, qtbot):
    page.load(_plan())
    with qtbot.waitSignal(page.mappings_applied, timeout=2000):
        page.apply_button.click()


def test_elevate_button_emits_signal(page, qtbot):
    page.load(_plan(permission_issues=["D:\\locked"]))
    with qtbot.waitSignal(page.elevate_requested, timeout=2000):
        page.elevate_button.click()


def test_deep_verify_checkbox(page):
    page.load(_plan())
    assert page.deep_verify() is False
    page.deep_check.setChecked(True)
    assert page.deep_verify() is True
