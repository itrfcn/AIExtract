"""批量状态流转：pending -> running -> done/failed，失败重跑。

测试用的 TestRuleProvider 只有最基本的正则规则，覆盖 batch 测试场景；
它继承 BaseProvider 走 chat_json 接口，不依赖已删除的 MockProvider。
"""

from __future__ import annotations

import re

from pipeline import store
from pipeline.batch import run_batch, retry_failed
from pipeline.providers import BaseProvider
from pipeline.schema import map_headers


# 测试用的最小正则规则（只覆盖 batch 测试用到的字段）
_RULES = {
    "name": [
        (r"(?:姓名|名字|人员姓名|员工姓名)[:：]\s*([\u4e00-\u9fa5·]{2,6})", 0.95),
        (r"我叫([\u4e00-\u9fa5·]{2,6})", 0.9),
        (r"^([\u4e00-\u9fa5]{2,4})[，,]", 0.45),
    ],
    "age": [
        (r"(?:年龄|岁数)[:：]?\s*(\d{1,3})\s*岁?", 0.95),
        (r"(\d{1,3})\s*岁", 0.8),
    ],
    "gender": [
        (r"(?:性别)[:：]?\s*(男|女)", 0.95),
        (r"[，,]\s*(男|女)\s*[，,。]", 0.5),
    ],
    "birth_date": [
        (r"(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?", 0.85),
    ],
    "company": [
        (r"(?:公司|工作单位|所在单位|就职于|任职于)[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,20})", 0.85),
    ],
    "hobbies": [
        (r"(?:兴趣爱好|爱好|兴趣)[:：]\s*([^。\n]+)", 0.9),
        (r"喜欢([^。\n]+)", 0.5),
    ],
}


def _match(key, text):
    for pattern, conf in _RULES.get(key, []):
        m = re.search(pattern, text)
        if not m:
            continue
        if key == "birth_date":
            groups = m.groups()
            if len(groups) >= 2 and groups[1]:
                day = groups[2] if len(groups) >= 3 and groups[2] else "01"
                return f"{int(groups[0]):04d}-{int(groups[1]):02d}-{int(day):02d}", m.group(0), conf
            elif len(groups) == 1 and groups[0]:
                return groups[0], m.group(0), conf
        value = m.group(1) if m.groups() else m.group(0)
        return value.strip(), m.group(0).strip(), conf
    return None


class TestRuleProvider(BaseProvider):
    """测试专用：用正则模拟抽取，走 chat_json 接口。"""

    def chat_json(self, messages, json_schema=None, max_tokens=1600):
        # 从 messages 里把文本凑出来（system + user）
        text = "\n".join(msg.get("content", "") for msg in messages if isinstance(msg, dict))
        # 简化：我们的 batch 测试里 extract_record 是传 text + specs，
        # 但 extract_record 调的是 chat_json，所以需要从 messages 还原原始文本。
        # 更简单：直接在 chat_json 里用 regex 匹配即可——我们只关心返回的 JSON 结构。
        return self._extract(text)

    def _extract(self, text):
        # 通用：从 FieldSpec 不在这个 provider 里 → 我们只返回 _RULES 里有的 key
        # 测试里用到的 key 都是 _RULES 覆盖的
        result = {}
        for key in _RULES:
            hit = _match(key, text)
            if hit:
                value, evidence, conf = hit
                if key == "hobbies":
                    value = [v.strip() for v in re.split(r"[、，,；;]+", value) if v.strip()]
                elif key == "age":
                    value = int(value)
                result[key] = {"value": value, "confidence": conf, "evidence": evidence}
            else:
                result[key] = {"value": None, "confidence": 0.0, "evidence": None}
        return result


class FailOnceProvider(TestRuleProvider):
    """第一次遇到含 FAIL 的文本抛错，之后正常，用于验证失败重跑。"""

    def __init__(self) -> None:
        self.failed_texts: set[str] = set()

    def chat_json(self, messages, json_schema=None, max_tokens=1600):
        text = "\n".join(msg.get("content", "") for msg in messages if isinstance(msg, dict))
        # 检查 messages 里原始文本是否含 FAIL —— 但 chat_json 收到的是 prompt，
        # 原始文本被包在 messages[1]（user）里，格式是"文本：xxx"。
        # 我们直接搜 FAIL 关键词
        if "FAIL" in text and text not in self.failed_texts:
            self.failed_texts.add(text)
            raise RuntimeError("模拟模型调用失败")
        return super().chat_json(messages, json_schema, max_tokens)


def test_batch_status_flow_and_results(tmp_path):
    store.set_db_path(tmp_path / "jobs.db")
    specs = map_headers(["姓名", "年龄", "性别", "兴趣爱好"], use_llm=False)

    job_id = store.create_job("A", "table.xlsx", "S", 2, specs)
    store.add_items(job_id, [
        {"source_name": "a.txt", "record_id": "r1", "name_hint": "张伟",
         "text": "姓名：张伟\n年龄：32岁\n性别：男\n兴趣爱好：徒步、摄影"},
        {"source_name": "b.txt", "record_id": "r1", "name_hint": "李娜",
         "text": "李娜，女，1995年8月出生。喜欢瑜伽和旅行。"},
    ])

    stats = run_batch(job_id, specs, TestRuleProvider(), concurrency=2)
    assert stats["total"] == 2
    assert stats["done"] == 2
    assert stats["failed"] == 0

    items = store.fetch_items(job_id)
    assert all(item["status"] == "done" for item in items)
    first = items[0]["result"]
    assert first["name"]["value"] == "张伟"
    assert first["name"]["status"] == "ok"
    assert first["age"]["value"] == 32
    assert first["hobbies"]["value"] == ["徒步", "摄影"]

    # 自然语言那条：部分字段应被标记为低置信度，而不是硬填
    second = items[1]["result"]
    assert second["gender"]["value"] == "女"
    assert second["gender"]["status"] in ("ok", "uncertain")
    assert any(cell["status"] == "uncertain" for cell in second.values())


def test_failed_item_retry_only(tmp_path):
    store.set_db_path(tmp_path / "jobs.db")
    specs = map_headers(["姓名", "年龄"], use_llm=False)
    provider = FailOnceProvider()

    job_id = store.create_job("A", "table.xlsx", "S", 1, specs)
    store.add_items(job_id, [
        {"source_name": "ok.txt", "record_id": "r1", "name_hint": "某甲",
         "text": "姓名：某甲\n年龄：20岁"},
        {"source_name": "fail.txt", "record_id": "r1", "name_hint": "某乙",
         "text": "FAIL 姓名：某乙\n年龄：30岁"},
    ])

    stats = run_batch(job_id, specs, provider, concurrency=2)
    assert stats["done"] == 1
    assert stats["failed"] == 1
    failed_item = [i for i in store.fetch_items(job_id) if i["status"] == "failed"][0]
    assert failed_item["attempts"] == 1
    assert "模拟模型调用失败" in (failed_item["error"] or "")

    stats_after_retry = retry_failed(job_id, specs, provider, concurrency=2)
    assert stats_after_retry["done"] == 2
    assert stats_after_retry["failed"] == 0
    assert stats_after_retry["status"] == "done"

    retried = [i for i in store.fetch_items(job_id) if i["item_id"] == failed_item["item_id"]][0]
    assert retried["status"] == "done"
    assert retried["attempts"] == 2
