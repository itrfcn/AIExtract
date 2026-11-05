"""记录切分：由用户手动选择模式，不做自动猜测。"""

from __future__ import annotations

import pytest

from pipeline.segment import split_records

RESUME = """候选人推荐报告
推荐顾问：张丹

个人信息：
姓    名：魏延庆
性    别：男

工作经历：
2015-2020 某公司 IT 经理
"""

TWO_PEOPLE = """姓名：张伟
年龄：32岁
性别：男

姓名：李娜
年龄：28岁
性别：女"""

BLANK_SEPARATED = """张伟，32岁，男，产品经理。

李娜，28岁，女，设计师。"""


def test_default_mode_is_no_split():
    assert len(split_records(RESUME)) == 1


def test_blank_mode_splits_resume_sections():
    """手动选了「按空行拆分」，简历就会被拆成多段（这是用户的选择）。"""
    assert len(split_records(RESUME, "blank")) > 1


def test_name_mode_keeps_single_resume_as_one():
    records = split_records(RESUME, "name")
    assert len(records) == 1
    assert records[0].name_hint == "魏延庆"


def test_name_mode_splits_two_people():
    records = split_records(TWO_PEOPLE, "name")
    assert len(records) == 2
    assert [r.name_hint for r in records] == ["张伟", "李娜"]


def test_blank_mode_splits_blank_separated_people():
    records = split_records(BLANK_SEPARATED, "blank")
    assert len(records) == 2
    assert records[0].name_hint == "张伟"


def test_no_split_keeps_two_people_together():
    assert len(split_records(TWO_PEOPLE, "none")) == 1


def test_auto_keeps_single_resume_as_one():
    assert len(split_records(RESUME, "auto")) == 1


def test_auto_splits_two_people():
    records = split_records(TWO_PEOPLE, "auto")
    assert [r.name_hint for r in records] == ["张伟", "李娜"]


def test_auto_splits_blank_separated_people():
    """没有姓名标签、也没有简历特征时，auto 退回按空行拆。"""
    assert len(split_records(BLANK_SEPARATED, "auto")) == 2


def test_auto_recognizes_resume_without_name_label():
    """很多简历不写「姓名」两个字，靠分节标题判断。"""
    text = """魏延庆
电话：13800000000

工作经历
2015-2020 某公司 IT 经理

教育经历
某大学 本科
"""
    records = split_records(text, "auto")
    assert len(records) == 1
    assert records[0].name_hint == "魏延庆"  # 开头单独一行兜底


def test_auto_splits_people_by_multiple_phones():
    """没有姓名标签，但出现两个手机号 → 明显是多人。"""
    text = """张伟
电话：13812345678

李娜
电话：13912345678"""
    assert len(split_records(text, "auto")) == 2


def test_section_title_is_not_treated_as_name():
    text = "个人信息\n\n工作经历：某公司 IT 经理"
    assert split_records(text, "none")[0].name_hint is None


def test_first_line_name_requires_resume_context():
    """开头一行只有像姓名、后面还跟着联系方式时才算姓名，避免把"教育背员"当人名。"""
    text = "教育背员\n2023.09-2027.06\n德州学院软件工程\n绩点3.82/6.0"
    assert split_records(text, "none")[0].name_hint is None

    good = "孟玉\n电话：17653485301\n求职意向：Java后端开发"
    assert split_records(good, "none")[0].name_hint == "孟玉"


def test_page_mode_splits_by_form_feed():
    text = "陈锋\n求职意向：机械设计\n\f李丽云\n现居：济南市\n电话：13563562552\n\f李天华\n电话：15998747679"
    records = split_records(text, "page")
    assert len(records) == 3
    assert [r.name_hint for r in records] == ["陈锋", "李丽云", "李天华"]


def test_page_breaks_become_blank_lines_in_other_modes():
    records = split_records("第一页\f第二页", "none")
    assert len(records) == 1
    assert "\f" not in records[0].text


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        split_records("随便一段文本", "bogus")
