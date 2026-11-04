"""证据回查与置信度：编造/无证据的值必须降级为 uncertain。

注意：这里的 FakeChatProvider 是测试专用，直接返回预设 payload，
不走真实 LLM 调用。
"""

from __future__ import annotations

from pipeline.extract import check_evidence, extract_record
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
