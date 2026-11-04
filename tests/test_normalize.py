"""归一化：年龄、性别、日期、电话、多值与"未提及"。"""

from __future__ import annotations

from pipeline.normalize import normalize_for_export, normalize_value, parse_date
from pipeline.schema import map_headers


def _spec(header: str, headers=None):
    specs = map_headers(headers or [header], use_llm=False)
    return [s for s in specs if s.header == header][0]


def test_age():
    spec = _spec("年龄")
    value, ok, _ = normalize_value("32岁", spec)
    assert value == 32 and ok
    value, ok, _ = normalize_value(" 三十 ", spec)
    assert not ok and value is not None  # 无法解析时保留原值供复核


def test_gender():
    spec = _spec("性别")
    assert normalize_value("先生", spec)[0] == "男"
    assert normalize_value("F", spec)[0] == "女"
    value, ok, _ = normalize_value("保密", spec)
    assert value == "保密" and not ok, "无法映射时保留原值并标记失败"


def test_date_variants():
    spec = _spec("出生日期")
    assert normalize_value("1993年4月12日", spec)[0] == "1993-04-12"
    assert normalize_value("1993/4/12", spec)[0] == "1993-04-12"
    assert normalize_value("1995年8月", spec)[0] == "1995-08"
    assert normalize_value("1995-08", spec)[0] == "1995-08"
    assert parse_date("不是日期") is None


def test_missing_never_filled():
    spec = _spec("年龄")
    for text in ("未提及", "未知", "无", "暂无", "", None):
        value, ok, _ = normalize_value(text, spec)
        assert value is None and ok


def test_list_split_and_export():
    spec = _spec("兴趣爱好")
    value, ok, _ = normalize_value("徒步、摄影和读书", spec)
    assert value == ["徒步", "摄影", "读书"] and ok
    assert normalize_for_export(value, spec) == "徒步、摄影、读书"


def test_phone_and_export_date():
    spec = _spec("手机号")
    assert normalize_value("138 0013 8000", spec)[0] == "13800138000"
    value, ok, _ = normalize_value("12345", spec)
    assert not ok

    date_spec = _spec("出生日期")
    assert normalize_for_export("1993-04-12", date_spec).isoformat() == "1993-04-12"
