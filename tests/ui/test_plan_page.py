import sys
from pathlib import Path

import pytest

from dmig.core.models import (EntryKind, LinkPlan, LinkTargetClass,
                              MigrationPlan, PlanTotals, TargetGroup,
                              UnknownReparse)
from dmig.ui.plan_page import LOCK_HINT, PlanPage, mapping_summary

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

NPM_CACHE = r"C:\Users\me\AppData\Local\npm-cache"


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
    plan.link_groups = [TargetGroup(prefix=NPM_CACHE, count=482, mapped_to="")]
    plan.unknown_reparse = [UnknownReparse(rel="weird-link", tag=0x8000001B)]
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
    assert page.metric_text("bytes") == "2.0 KB"
    assert page.metric_text("files") == "8"
    assert page.metric_text("dirs") == "3"
    assert page.metric_text("links") == "1"
    assert page.metric_text("elevation") == "0"
    assert page.metric_text("locks") == LOCK_HINT
    assert page.metric_text("eta") != "-"


def test_group_table_is_rendered(page):
    page.load(_plan())
    assert page.group_rows() == [(r"C:\Users\me\AppData\Local\npm-cache", 482, "")]


def test_warning_mentions_links_and_target(page):
    page.load(_plan())
    text = page.warning_text()
    assert "E:\\myapp\\data" in text
    assert "482" in text


def test_warning_names_dangling_links(page):
    """断链只在 scanner 里被算出来过，页面上此前一个字都不显示。"""
    page.load(_plan(links=[
        LinkPlan(rel="nm\\a", kind=EntryKind.JUNCTION, raw_target=r"C:\cache\a",
                 new_target=r"C:\cache\a",
                 target_class=LinkTargetClass.OUTSIDE_SOURCE, is_dir=True),
        LinkPlan(rel="gone", kind=EntryKind.JUNCTION, raw_target=r"C:\vanished",
                 new_target=r"C:\vanished",
                 target_class=LinkTargetClass.OUTSIDE_SOURCE, is_dir=True,
                 is_dangling=True),
    ]))
    text = page.warning_text()
    assert "1 条链接的目标现在就不存在（断链）：gone" in text
    assert "仍会原样重建" in text


def test_dangling_line_is_absent_when_nothing_is_broken(page):
    page.load(_plan())
    assert "断链" not in page.warning_text()


def test_warning_discloses_lossy_attributes(page):
    """稀疏实化、硬链接拆开会改变结果；ADS 则会一并保留——不能反着说。

    P3 回归：这里原先写「ADS 不保留」，与 copy_one 走内核 CopyFileW 快路径的
    事实相反（copier 自己的注释就写着会连 ADS 一起带过去，另有
    test_kernel_copy_preserves_alternate_data_streams 逐字节钉着）。
    """
    page.load(_plan(sparse_files=2, hardlinked_files=482, ads_files=7))
    text = page.warning_text()
    assert "复制方式的固有限制" in text
    assert "2 个稀疏文件会被实化" in text
    assert "482 个硬链接会变成各自独立的副本" in text
    assert "7 个文件带附加数据流" in text
    assert "复制后会一并保留" in text
    assert "附加数据流（ADS），复制后不保留" not in text


def test_lossy_line_is_absent_when_nothing_is_lost(page):
    page.load(_plan())
    assert "固有限制" not in page.warning_text()


# --- C4：无权限目录的出路 ---------------------------------------------

def test_denied_hint_names_the_exit(page):
    """页面此前只写「请点以管理员身份重试」，拿不到权限的用户就卡死在这里。"""
    page.load(_plan(permission_issues=[r"D:\src\locked"]))
    assert "跳过无权限访问的目录" in page.warning_text()
    assert page.skip_denied_check.isEnabled() is True
    assert page.skip_denied_check.isChecked() is False
    assert page.excludes() == []


def test_checking_the_box_excludes_the_denied_directories(page):
    page.load(_plan(permission_issues=[r"D:\src\locked"]))
    page.skip_denied_check.setChecked(True)
    assert page.excludes() == [r"D:\src\locked"]
    page.skip_denied_check.setChecked(False)
    assert page.excludes() == []


def test_excludes_survive_a_rescan(page):
    """重扫会换一份计划，那些目录从此不再出现在 permission_issues 里。"""
    page.load(_plan(permission_issues=[r"D:\src\locked"]))
    page.skip_denied_check.setChecked(True)

    page.load(_plan())                       # 新计划里已经没有无权限目录
    assert page.skip_denied_check.isChecked() is True
    assert page.excludes() == [r"D:\src\locked"]


def test_excluded_items_are_shown_in_the_summary(page):
    page.load(_plan(excludes=[r"D:\src\locked"], excluded=["locked"]))
    text = page.warning_text()
    assert "已跳过 1 项（locked）" in text
    assert "只存在于备份里" in text


def test_box_is_disabled_when_there_is_nothing_to_skip(page):
    page.load(_plan())
    assert page.skip_denied_check.isEnabled() is False


def test_set_options_restores_the_excludes(page):
    """续传时丢了排除项，用户明确排除的目录会被重新复制一遍。"""
    page.set_options({"excludes": [r"D:\src\locked"]})
    assert page.skip_denied_check.isChecked() is True
    assert page.excludes() == [r"D:\src\locked"]


def test_set_options_tolerates_sessions_without_excludes(page):
    for options in (None, {}, {"excludes": None}):
        page.set_options(options)
        assert page.excludes() == []


# --- C2：接管属主的入口 -----------------------------------------------

def test_takeover_button_requires_elevation(page):
    page.load(_plan(permission_issues=[r"D:\src\locked"]), already_elevated=True)
    assert page.takeover_button.isEnabled() is True


def test_takeover_button_names_the_prerequisite_when_not_elevated(page):
    """未提权时按钮不可用，必须说清要先做什么，否则用户只会觉得按不动。"""
    page.load(_plan(permission_issues=[r"D:\src\locked"]), already_elevated=False)
    assert page.takeover_button.isEnabled() is False
    assert "以管理员身份重试" in page.takeover_button.toolTip()


def test_takeover_button_is_disabled_without_denied_directories(page):
    page.load(_plan(), already_elevated=True)
    assert page.takeover_button.isEnabled() is False
    assert "没有无权限访问的目录" in page.takeover_button.toolTip()


def test_takeover_tooltip_states_the_acl_is_recorded(page):
    page.load(_plan(permission_issues=[r"D:\src\locked"]), already_elevated=True)
    assert "原始 ACL" in page.takeover_button.toolTip()
    assert "回滚时按记录还原" in page.takeover_button.toolTip()


def test_takeover_button_emits_signal(page, qtbot):
    page.load(_plan(permission_issues=[r"D:\src\locked"]), already_elevated=True)
    with qtbot.waitSignal(page.takeover_requested, timeout=2000):
        page.takeover_button.click()


def test_unknown_reparse_is_listed(page):
    page.load(_plan())
    assert page.unknown_count() == 1
    assert "非链接类重解析点" in page.warning_text()
    assert "按真实内容复制" in page.warning_text()


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
    assert page.metric_text("elevation") == "1"
    assert "以管理员身份重试" in page.warning_text()


def test_elevate_button_disabled_when_already_elevated(page):
    page.load(_plan(permission_issues=["D:\\locked"]), already_elevated=True)
    assert page.elevate_button.isEnabled() is False
    assert "即使提权也访问不了" in page.warning_text()


def test_apply_button_emits_signal(page, qtbot):
    page.load(_plan())
    with qtbot.waitSignal(page.replan_requested, timeout=2000):
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


def test_mapping_survives_reload(page):
    """A1 回归：重扫会按新计划重建表格，用户填过的映射不能被清空。"""
    page.load(_plan())
    page.set_mapping(0, r"E:\npm-cache")
    page.load(_plan())                       # 模拟「应用映射并重新扫描」后的回显
    assert page.mappings() == {NPM_CACHE: r"E:\npm-cache"}
    assert page.group_rows() == [(NPM_CACHE, 482, r"E:\npm-cache")]


def test_mapping_from_plan_is_displayed(page):
    """计划回传 mapped_to 时也要显示出来（CLI/重扫两条路都走得通）。"""
    page.load(_plan(link_groups=[TargetGroup(prefix=NPM_CACHE, count=482,
                                             mapped_to=r"E:\npm-cache")]))
    assert page.group_rows() == [(NPM_CACHE, 482, r"E:\npm-cache")]
    assert page.mappings() == {NPM_CACHE: r"E:\npm-cache"}


def test_mapping_survives_when_group_row_disappears(page):
    """重扫后分组可能变化；行不在了，映射仍要生效，否则链接会指回旧位置。"""
    page.load(_plan())
    page.set_mapping(0, r"E:\npm-cache")
    page.load(_plan(link_groups=[]))
    assert page.mappings() == {NPM_CACHE: r"E:\npm-cache"}


def test_clearing_cell_cancels_persisted_mapping(page):
    """留空是「取消映射」的明确意图，不能被记住的旧值复活。"""
    page.load(_plan())
    page.set_mapping(0, r"E:\npm-cache")
    page.set_mapping(0, "")
    assert page.mappings() == {}
    page.load(_plan())
    assert page.mappings() == {}


def test_typing_into_the_editor_is_remembered(page, qtbot):
    """用户在输入框里打字（不走 set_mapping），也要能跨重扫存活。"""
    page.load(_plan())
    page.mapping_editor(0).setText(r"E:\typed-cache")
    page.load(_plan())
    assert page.mappings() == {NPM_CACHE: r"E:\typed-cache"}


# --- 「重写为」必须像个输入框；结果必须看得见 ---------------------------

def test_mapping_column_is_a_real_input_box(page):
    """「重写为」要一眼看出是能输入的框。

    原来它只是个普通表格单元格，没有任何视觉提示——第一次用根本不知道那里
    能填（真实反馈：「不仔细看根本看不到」）。
    """
    from PySide6.QtWidgets import QLineEdit

    page.load(_plan())

    editor = page.mapping_editor(0)
    assert isinstance(editor, QLineEdit), "必须是真正的输入控件"
    assert editor.placeholderText(), "要有占位提示，空着也看得出能填"
    assert editor.text() == ""


def test_derived_target_column_shows_the_effective_target(page):
    """新增的「迁移后指向」列：填了映射就显示新位置，没填就等于原样。

    这是「点应用看不出变化」的根治——结果直接摆在表格里，
    而左边那列是分组前缀（原始指向），永远不会变。
    """
    page.load(_plan())

    assert page.derived_target_text(0) == NPM_CACHE, "没填映射时等于原样"

    page.set_mapping(0, r"E:\npm-cache")

    assert page.derived_target_text(0) == r"E:\npm-cache", "填完立刻可见"


def test_derived_target_updates_while_typing(page):
    """在输入框里打字时结果列要跟着变，不用先点应用。"""
    page.load(_plan())
    page.mapping_editor(0).setText(r"E:\typed")
    assert page.derived_target_text(0) == r"E:\typed"


def test_group_caption_states_the_prerequisite(page):
    """说明里必须讲清前提：工具不会替你搬那个目录，而且要讲对**什么条件才该填**。

    原来的措辞是「只有你已经把该目录搬到别处时才填」——这个条件不充分，会让人
    以为「凡是要迁移那个目录就得填」。实测：把外部目录迁移成链接之后原路径照常
    可用，链接会自己穿过那一层（两跳 junction 链读得到数据）；只有把中间那一跳
    删掉才断。所以真正的条件是**旧路径此后不再存在**。
    """
    text = page.group_caption_text()

    assert "留空" in text, "要说明留空的含义"
    assert "不会" in text or "不搬" in text, "要写明工具不搬运那个目录"
    assert "不再存在" in text, "真正的条件是旧路径此后不再存在"


def test_unknown_reparse_card_explains_what_it_is(page):
    """这张卡片对用户完全是陌生的，必须解释清楚是什么、默认怎么处理、勾了会怎样。

    云占位符（OneDrive 按需文件）、AppExecLink、Dedup 都落在这里，而
    「跳过 + 删备份」是不可逆的——光靠一行标题和一句复选框说明远远不够：
    用户既不知道这些东西是什么，也不知道不勾会触发云端下载。
    """
    text = page.unknown_caption_text()

    assert "链接" in text, "要讲清它『不是链接』"
    assert "云占位符" in text and "AppExecLink" in text, "要举出典型类型"
    assert "下载" in text, "要说明不勾的代价：云占位符会触发下载"
    assert "备份" in text, "要说明勾选后的后果：只留在备份里"
    assert "不再需要" in text or "确认" in text, "要提醒用户确认"


def test_summary_does_not_push_a_pointless_mapping(page):
    """摘要那句「若那些目录也要迁移，请填写新路径」会把用户带偏。

    把外部目录迁移成链接之后原路径仍然可用，链接会自己穿过去，根本不需要映射。
    真实案例：用户照着这句话填了映射，结果链接指向一个空目录，数据反而读不到了。
    """
    page.load(_plan())
    text = page.warning_text()

    assert "若那些目录也要迁移" not in text, "这句话诱导用户做一次没有意义的映射"
    assert "不再存在" in text or "保持原样" in text


def test_skip_reparse_checkbox_roundtrip(page):
    page.load(_plan())
    assert page.skip_unknown_reparse() is False
    page.skip_reparse_check.setChecked(True)
    assert page.skip_unknown_reparse() is True
    page.load(_plan(skip_unknown_reparse=True))
    assert page.skip_unknown_reparse() is True


def test_skip_reparse_toggle_requests_replan(page, qtbot):
    """跳过与否会改变真实数据量，必须重扫，否则页面显示的计划与实际执行不符。"""
    page.load(_plan())
    with qtbot.waitSignal(page.replan_requested, timeout=2000):
        page.skip_reparse_check.setChecked(True)


def test_load_does_not_request_replan(page, qtbot):
    """load() 里回显复选框会触发 toggled，不能因此多扫一趟。"""
    emitted = []
    page.replan_requested.connect(lambda: emitted.append(1))
    page.load(_plan(skip_unknown_reparse=True))
    page.load(_plan(skip_unknown_reparse=True))
    assert emitted == []


def test_skipped_reparse_is_warned_as_backup_only(page):
    page.load(_plan(skip_unknown_reparse=True))
    assert "只存在于备份中" in page.warning_text()


def test_file_symlinks_without_privilege_warn_and_enable_elevation(page):
    """B4：计划页要提前说清楚文件符号链接建不起来，并给出提权出口。

    没有这道提示，用户会等整棵树复制完、进入建链阶段才看到 WinError 1314。
    """
    page.load(_plan(file_symlinks=3), symlink_capable=False)
    text = page.warning_text()
    assert "3 个文件符号链接" in text
    assert "管理员特权" in text
    assert page.elevate_button.isEnabled() is True


def test_file_symlinks_are_fine_when_capable(page):
    """本机建得起符号链接时不该吓人，提权按钮也不必为此放开。"""
    page.load(_plan(file_symlinks=3), symlink_capable=True)
    assert "文件符号链接" not in page.warning_text()
    assert page.elevate_button.isEnabled() is False


def test_symlink_warning_changes_when_already_elevated(page):
    """提权后仍建不起来（组策略/开发者模式）要说清下一步，而不是继续叫用户提权。"""
    page.load(_plan(file_symlinks=2), already_elevated=True, symlink_capable=False)
    text = page.warning_text()
    assert "即使提权也建不起来" in text
    assert "开发者模式" in text
    assert page.elevate_button.isEnabled() is False


def test_directory_links_do_not_trigger_symlink_warning(page):
    """junction 与目录符号链接能降级，不该被算成需要特权。"""
    page.load(_plan(file_symlinks=0), symlink_capable=False)
    assert "文件符号链接" not in page.warning_text()


# --- C6：续传回显会话里的运行参数 -------------------------------------
def test_set_options_restores_mappings_and_flags(page):
    page.set_options({"mappings": {NPM_CACHE: r"E:\npm-cache"},
                      "deep_verify": True, "skip_unknown_reparse": True})
    assert page.mappings() == {NPM_CACHE: r"E:\npm-cache"}
    assert page.deep_verify() is True
    assert page.skip_unknown_reparse() is True


def test_set_options_does_not_request_replan(page):
    """它是在窗口构造期间调的，那时源/目标还没填，触发重扫必然白跑一趟。"""
    emitted = []
    page.replan_requested.connect(lambda: emitted.append(1))
    page.set_options({"mappings": {NPM_CACHE: r"E:\npm-cache"},
                      "skip_unknown_reparse": True})
    assert emitted == []


def test_mappings_from_set_options_survive_load(page):
    """回显时表格还没有行；随后扫描出的分组不能把它们冲掉。"""
    page.set_options({"mappings": {NPM_CACHE: r"E:\npm-cache"}})
    page.load(_plan())
    assert page.mappings() == {NPM_CACHE: r"E:\npm-cache"}


def test_set_options_tolerates_old_sessions(page):
    """升级前写的会话文件没有 options，回显不能崩、也不能凭空勾上什么。"""
    for options in (None, {}, {"mappings": None}):
        page.set_options(options)
    assert page.mappings() == {}
    assert page.deep_verify() is False
    assert page.skip_unknown_reparse() is False


# --- 归属表为空时必须说清楚，而不是留一张空表配一个可点的按钮 -----------

def test_empty_group_table_says_there_is_nothing_to_map(page):
    """没有指向树外的链接时，要明说「没有链接可映射」并把按钮置灰。

    真实案例：用户把 Windows 快捷方式（.lnk）当成了目录软链接，
    扫描出链接数 0、归属表空，然后反复点「应用映射并重新扫描」——
    什么都不会发生，因为本来就没有链接可映射。界面必须把这件事说出来，
    否则用户只会以为按钮坏了。
    """
    page.load(_plan(link_groups=[], links=[]))

    assert page.group_table.rowCount() == 0
    assert "没有" in page.group_hint_text(), "空表必须给出说明"
    assert page.apply_button.isEnabled() is False, "没有链接可映射时按钮应当置灰"
    # 表格必须整块收起来：留在那里会把卡片高度占满，说明文字被挤到
    # 可视区外——渲染实测过，用户根本看不到那行说明。
    assert page.group_table.isHidden() is True


def test_groups_present_hides_the_hint_and_enables_apply(page):
    page.load(_plan())

    assert page.group_table.rowCount() == 1
    assert page.group_hint_text() == "", "有分组时不该显示空表说明"
    assert page.group_table.isHidden() is False, "有分组时表格必须显示"
    assert page.apply_button.isEnabled() is True


def test_mapping_summary_reports_groups_and_links():
    """重扫只要 0.03 秒，状态栏回到「计划已生成」和点击前一模一样。

    所以必须给一句话说明「到底应用了什么、影响多少条链接」。
    """
    plan = _plan()
    plan.link_groups = [
        TargetGroup(prefix=NPM_CACHE, count=482, mapped_to=r"E:\npm-cache"),
        TargetGroup(prefix=r"D:\other", count=18, mapped_to=""),
    ]
    assert mapping_summary(plan, {NPM_CACHE: r"E:\npm-cache"}) == \
        "映射已应用：1 组、共 482 条链接的指向将被重写"


def test_mapping_summary_without_mappings():
    assert "未设置" in mapping_summary(_plan(), {})


def test_mapping_summary_when_nothing_matched():
    """映射还在、但没有任何分组对上（比如那些行在重扫后消失了）。"""
    text = mapping_summary(_plan(), {r"D:\gone": r"E:\x"})
    assert "没有匹配" in text
