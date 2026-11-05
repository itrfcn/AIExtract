"""证据回查与置信度：编造/无证据的值必须降级为 uncertain。

注意：这里的 FakeChatProvider 是测试专用，直接返回预设 payload，
不走真实 LLM 调用。
"""

from __future__ import annotations

from datetime import date

import pytest

from pipeline.extract import _age_from_birth, _norm_for_evidence, check_evidence, extract_record
from pipeline.providers import BaseProvider
from pipeline.schema import map_headers


class FakeChatProvider(BaseProvider):
    """mock 一个 LLM 返回，用于验证 evidence 回查逻辑。"""

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, messages, json_schema=None, max_tokens=1600):
        return self.payload


def test_check_evidence_tolerates_spacing_but_not_fabrication():
    text = "姓名：张伟，32 岁，产品经理。"
    assert check_evidence("32岁", text)
    assert check_evidence("姓名：张伟", text)
    assert not check_evidence("35岁", text)
    assert not check_evidence(None, text)


def test_hallucinated_value_is_downgraded():
    specs = map_headers(["姓名"], use_llm=False)
    text = "姓名：张伟，32岁。"
    provider = FakeChatProvider({"name": {"value": "张三", "confidence": 0.95, "evidence": "姓名：张三"}})
    result = extract_record(provider, text, specs)["name"]

    assert result["value"] == "张伟"  # 姓名列以原文「姓名：张伟」为准，不用编造值
    assert result["status"] == "uncertain"
    assert result["confidence"] <= 0.49
    assert "校正" in result["reason"]


def test_missing_field_is_not_invented():
    specs = map_headers(["姓名", "公司"], use_llm=False)
    provider = FakeChatProvider({"name": {"value": "张伟", "confidence": 0.9, "evidence": "姓名：张伟"},
                                 "company": {"value": None, "confidence": 0, "evidence": None}})
    result = extract_record(provider, "姓名：张伟，32岁。", specs)

    assert result["name"]["status"] == "ok"
    assert result["company"]["value"] is None
    assert result["company"]["status"] == "not_mentioned"


def test_evidence_correct_but_value_wrong_is_downgraded():
    """evidence 在原文里，但 value 从 evidence 中读错了字 —— 反查应兜住。"""
    specs = map_headers(["年龄"], use_llm=False)
    text = "张伟，32岁，产品经理。"
    # evidence 确实命中原文（"32岁" 在原文里），但 value 读成了 29
    provider = FakeChatProvider({"age": {"value": 29, "confidence": 0.9, "evidence": "32岁"}})
    result = extract_record(provider, text, specs, threshold=0.75)["age"]

    assert result["evidence_ok"] is True, "evidence 应该在原文里命中"
    assert result["status"] == "uncertain", "value 反查失败必须降级"
    assert result["confidence"] <= 0.49
    assert "反查" in (result.get("reason") or "")


def test_short_evidence_rejected():
    """evidence 归一化后 < 3 字符 → check_evidence 返回 False（防误命中）。"""
    text = "张伟，32岁，产品经理。"
    assert check_evidence("32", text) is False        # 单字符数字，太短
    assert check_evidence("张伟", text) is False       # 两字姓名，太短
    assert check_evidence("32岁", text) is True        # 三字，可以
    assert check_evidence("姓名张伟", "姓名张伟，32岁") is True


def test_age_can_be_derived_from_birth_date():
    """年龄是唯一允许推算的字段：原文只有出生年月时，evidence 引用出生年月即可。"""
    today = date.today()
    born_year, born_month = 2003, 10
    expected = today.year - born_year - ((today.month, today.day) < (born_month, 1))
    if not 14 <= expected <= 80:
        pytest.skip("当前年份下该出生年月不在合理年龄范围")

    specs = map_headers(["年龄"], use_llm=False)
    text = "刘泽宇\n出生年月：2003年10月\n电话：13800000000"
    provider = FakeChatProvider({"age": {"value": expected, "confidence": 0.9, "evidence": "出生年月：2003年10月"}})
    result = extract_record(provider, text, specs)["age"]

    assert result["value"] == expected
    assert result["derived"] is True
    assert result["status"] == "ok"
    assert "推算" in result["reason"]
    assert result["confidence"] <= 0.8


def test_graduation_year_is_not_used_for_age():
    specs = map_headers(["年龄"], use_llm=False)
    text = "刘泽宇，2027届毕业生。"
    provider = FakeChatProvider({"age": {"value": 23, "confidence": 0.9, "evidence": "2027届毕业生"}})
    result = extract_record(provider, text, specs)["age"]

    assert result["derived"] is False
    assert result["status"] == "uncertain"


def test_non_age_field_is_not_derived_from_birth_date():
    specs = map_headers(["工作年限"], use_llm=False)
    text = "刘泽宇\n出生年月：2003年10月"
    provider = FakeChatProvider({"work_years": {"value": 23, "confidence": 0.9, "evidence": "2003年10月"}})
    result = extract_record(provider, text, specs)["work_years"]

    assert result["derived"] is False
    assert result["status"] == "uncertain"


def test_age_fallback_from_labeled_birth_date():
    """模型没给年龄，但原文有「出生日期：1983.1」→ 按出生年月推算。"""
    specs = map_headers(["年龄"], use_llm=False)
    text = "孙志海\n出生日期：1983.1\n学历：本科"
    provider = FakeChatProvider({"age": {"value": None, "confidence": 0, "evidence": None}})
    result = extract_record(provider, text, specs)["age"]

    assert result["value"] == _age_from_birth(1983, 1)
    assert result["derived"] is True
    assert result["status"] == "ok"
    assert result["evidence"] == "出生日期：1983.1"


def test_age_fallback_with_year_only_is_uncertain():
    """只给了出生年份 → 可能差 1 岁，标黄待核对。"""
    specs = map_headers(["年龄"], use_llm=False)
    text = "魏延庆\n出生日期：1981\n学历：本科"
    provider = FakeChatProvider({"age": {"value": None, "confidence": 0, "evidence": None}})
    result = extract_record(provider, text, specs)["age"]

    assert result["value"] == _age_from_birth(1981, None)
    assert result["derived"] is True
    assert result["status"] == "uncertain"
    assert result["confidence"] <= 0.49
    assert "相差 1 岁" in result["reason"]


def test_chinese_numeral_evidence_matches_arabic():
    """弱点4：evidence 用中文数字写"三十二岁"，原文用阿拉伯数字"32岁" → 归一化后等价。"""
    text = "张伟，32岁，产品经理。"
    # LLM 返回 evidence 是"三十二岁"（中文数字），原文是"32岁"（阿拉伯数字）
    assert check_evidence("三十二岁", text) is True, "中文数字 → 阿拉伯数字归一化后应命中"
    assert _norm_for_evidence("三十二岁") == _norm_for_evidence("32岁"), "归一化结果相同"

    # 完整链路：中文证据（三十二岁）归一化后等价于原文的 32岁
    specs = map_headers(["年龄"], use_llm=False)
    provider = FakeChatProvider({"age": {"value": 32, "confidence": 0.9, "evidence": "三十二岁"}})
    result = extract_record(provider, text, specs)["age"]
    assert result["evidence_ok"] is True, "中文数字证据归一化后应命中原文"


def test_referrer_name_is_replaced_by_candidate_label():
    """简历开头的「推荐顾问：张丹」不能被写进姓名列。"""
    specs = map_headers(["姓名"], use_llm=False)
    text = "候选人推荐报告\n推荐顾问：张丹\n姓    名：魏延庆\n性    别：男"
    provider = FakeChatProvider({"name": {"value": "张丹", "confidence": 0.95, "evidence": "推荐顾问：张丹"}})

    result = extract_record(provider, text, specs)["name"]
    assert result["value"] == "魏延庆"
    assert result["status"] == "uncertain"
    assert "标签" in result["reason"]


def test_missing_name_is_filled_from_label():
    specs = map_headers(["姓名"], use_llm=False)
    text = "候选人推荐报告\n姓    名：李洪生\n性    别：男"
    provider = FakeChatProvider({"name": {"value": None, "confidence": 0, "evidence": None}})

    result = extract_record(provider, text, specs)["name"]
    assert result["value"] == "李洪生"
    assert result["status"] == "ok"


def test_referrer_value_without_name_label_is_dropped():
    specs = map_headers(["姓名"], use_llm=False)
    text = "本次由推荐顾问张丹负责对接。"
    provider = FakeChatProvider({"name": {"value": "张丹", "confidence": 0.9, "evidence": "推荐顾问张丹"}})

    result = extract_record(provider, text, specs)["name"]
    assert result["value"] is None
    assert "推荐人" in result["reason"]


def test_referrer_value_dropped_even_with_short_evidence():
    """没有姓名标签、evidence 又很短时，靠原文里「推荐顾问：张丹」这句兜住。"""
    specs = map_headers(["姓名"], use_llm=False)
    text = "推荐顾问：张丹\n候选人工作经历：10 年数字化管理经验。"
    provider = FakeChatProvider({"name": {"value": "张丹", "confidence": 0.9, "evidence": "张丹"}})

    result = extract_record(provider, text, specs)["name"]
    assert result["value"] is None
    assert "推荐人" in result["reason"]
