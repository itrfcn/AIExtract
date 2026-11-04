"""抽取编排：prompt 构造、结构化输出解析、证据回查、置信度判定。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

from .normalize import normalize_value
from .providers import BaseProvider
from .schema import FieldSpec, build_json_schema

CellStatus = Literal["ok", "uncertain", "not_mentioned"]


class CellResult(BaseModel):
    value: Any = None
    confidence: float = 0.0
    evidence: str | None = None
    evidence_ok: bool | None = None
    status: CellStatus = "not_mentioned"
    reason: str = ""


SYSTEM_PROMPT = """你是一个严谨的信息抽取器。你会收到一段关于人物的文本，以及一组需要抽取的字段。

必须遵守的规则：
1. 只从文本中抽取，禁止使用外部常识补全，禁止编造；文本没有明确写出的内容一律返回 null。
2. evidence 必须是从文本中"逐字复制"的连续片段（10-60 字），不能改写、不能拼接、不能加引号；没有 evidence 时返回 null。
3. evidence 必须能直接支持 value：值是从 evidence 中读出来的，而不是猜出来的。
4. 日期统一输出 ISO 格式（1993-04-12，仅有年月用 1993-04）；年龄只输出整数；多个值输出数组；性别只取 男/女/其他。
5. confidence 是 0-1 的数字，表示该值有明确原文支撑的把握程度；没有 evidence 时 confidence 必须为 0。
6. 只输出一个 JSON 对象，键是字段 key，值是 {"value": ..., "confidence": ..., "evidence": ...}，不要输出任何解释。

宁可漏抽，不可错抽。"""


def build_extraction_messages(text: str, specs: list[FieldSpec]) -> list[dict]:
    field_lines = []
    for spec in specs:
        if not spec.enabled:
            continue
        type_hint = spec.type
        if spec.type == "enum" and spec.enum_values:
            type_hint += f"，可选值：{'、'.join(spec.enum_values)}"
        elif spec.type == "list":
            type_hint += "，数组"
        field_lines.append(f"- {spec.key}（表格列名：{spec.header}；类型：{type_hint}）：{spec.description}")

    user = (
        "待抽取文本：\n<text>\n"
        + text.strip()
        + "\n</text>\n\n需要抽取的字段：\n"
        + "\n".join(field_lines)
        + '\n\n请输出 JSON 对象，形如：{"字段key": {"value": 值或null, "confidence": 0到1, "evidence": "原文片段或null"}}'
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


_EVIDENCE_STRIP = re.compile(r"[\s\u3000,，。.;；:：!！?？'\"“”‘’()（）\[\]【】]")


def _norm_for_evidence(text: str) -> str:
    return _EVIDENCE_STRIP.sub("", text or "")


def check_evidence(evidence: Any, source_text: str) -> bool:
    """evidence 必须能在原文中原样找到（忽略空白与常见标点差异）。"""
    if not evidence or not isinstance(evidence, str):
        return False
    ev = evidence.strip()
    if len(_norm_for_evidence(ev)) < 2:
        return False
    return _norm_for_evidence(ev) in _norm_for_evidence(source_text)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def extract_record(
    provider: BaseProvider,
    text: str,
    specs: list[FieldSpec],
    threshold: float = 0.75,
) -> dict[str, dict]:
    """抽取一条记录，返回 {key: CellResult 的 dict 形式}。"""
    enabled = [s for s in specs if s.enabled]
    if not enabled:
        return {}

    raw = provider.chat_json(build_extraction_messages(text, enabled), build_json_schema(enabled))
    if not isinstance(raw, dict):
        raw = {}

    results: dict[str, dict] = {}
    for spec in enabled:
        item = raw.get(spec.key)
        if not isinstance(item, dict):
            item = {}

        raw_value = item.get("value")
        llm_conf = _as_float(item.get("confidence"))
        evidence = item.get("evidence")
        if isinstance(evidence, list):
            evidence = " ".join(str(e) for e in evidence)
        if not isinstance(evidence, str):
            evidence = None

        value, normalized_ok, reason = normalize_value(raw_value, spec)
        if value is None:
            results[spec.key] = CellResult(
                value=None,
                confidence=0.0,
                evidence=evidence,
                evidence_ok=None,
                status="not_mentioned",
                reason="",
            ).model_dump(mode="json")
            continue

        evidence_ok = check_evidence(evidence, text)
        score = 0.5 * llm_conf + 0.3 * (1.0 if evidence_ok else 0.0) + 0.2 * (1.0 if normalized_ok else 0.0)
        if not evidence_ok:
            score = min(score, 0.49)
            reason = reason or "证据未能在原文中命中"

        status: CellStatus = "ok" if (evidence_ok and normalized_ok and score >= threshold) else "uncertain"
        results[spec.key] = CellResult(
            value=value,
            confidence=round(score, 3),
            evidence=evidence,
            evidence_ok=evidence_ok,
            status=status,
            reason=reason,
        ).model_dump(mode="json")

    return results


def estimate_tokens(texts: list[str], prompt_overhead: int = 550) -> dict:
    """按字符数粗估 token 与调用次数（仅用于成本提示，不追求精确）。"""
    total_chars = sum(len(t or "") for t in texts)
    calls = len(texts)
    input_tokens = int(total_chars * 0.75) + prompt_overhead * calls
    output_tokens = int(calls * 320)
    return {
        "chars": total_chars,
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "rough_cost_note": "以 gpt-4o-mini 级别价格估算约 $%.4f；旗舰模型请按实际单价放大" % (
            input_tokens / 1_000_000 * 0.15 + output_tokens / 1_000_000 * 0.6
        ),
    }


def summarize_cells(cells: dict[str, dict]) -> dict:
    total = len(cells)
    filled = sum(1 for c in cells.values() if c.get("value") is not None)
    uncertain = sum(1 for c in cells.values() if c.get("status") == "uncertain")
    return {"total": total, "filled": filled, "uncertain": uncertain}
