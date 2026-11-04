"""证据回查与置信度：编造/无证据的值必须降级为 uncertain。

注意：这里的 FakeChatProvider 是测试专用，直接返回预设 payload，
不走真实 LLM 调用。
"""

from __future__ import annotations

from pipeline.extract import _norm_for_evidence, check_evidence, extract_record
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

    assert result["value"] == "张三"  # 保留原值供人工核对
    assert result["status"] == "uncertain"
    assert result["evidence_ok"] is False
    assert result["confidence"] <= 0.49


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
