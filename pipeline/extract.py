"""抽取编排：prompt 构造、结构化输出解析、证据回查、置信度判定。"""

from __future__ import annotations

import re
from datetime import date
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
    derived: bool = False


SYSTEM_PROMPT = """你是一个严谨的信息抽取器。你会收到一段关于人物的文本，以及一组需要抽取的字段。

必须遵守的规则：
1. 只从文本中抽取，禁止使用外部常识补全，禁止编造；文本没有明确写出的内容一律返回 null。
2. evidence 必须是从文本中"逐字复制"的连续片段（6-60 字），不能改写、不能拼接、不能加引号；
   必须包含足够上下文（至少能唯一定位，建议 ≥ 6 字），避免只返回一个数字或姓名造成误匹配；没有 evidence 时返回 null。
3. evidence 必须能直接支持 value：值是从 evidence 中读出来的，而不是猜出来的。
4. 日期统一输出 ISO 格式（1993-04-12，仅有年月用 1993-04）；年龄只输出整数；多个值输出数组；性别只取 男/女/其他。
   唯一允许的推算：字段是年龄、原文只给了出生年月时，可按当前年份计算周岁，evidence 必须引用出生年月原文；其他字段禁止任何推算。
5. confidence 是 0-1 的数字，表示该值有明确原文支撑的把握程度；没有 evidence 时 confidence 必须为 0。
6. 文本可能是「候选人推荐报告」或简历：只抽取文本主角（候选人本人）的信息。
   「推荐顾问」「推荐人」「招聘顾问」「推荐时间」「应聘企业」「联系人」「HR」等第三方信息一律不得作为候选人字段的值；
   姓名必须是候选人本人（可能写在「姓名」标签里，也可能只是简历开头单独一行），绝不能取推荐顾问/联系人/公司名。
7. 字段值必须与字段含义直接对应：不要把工作年限当成年龄、把毕业年份当出生日期、把学校当公司、把应聘职位当现任职位。
8. 只输出一个 JSON 对象，键是字段 key，值是 {"value": ..., "confidence": ..., "evidence": ...}，不要输出任何解释。

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


# ---------- 证据归一化：去空白标点 + 中文数字 → 阿拉伯数字 ----------

_EVIDENCE_STRIP = re.compile(
    r"[\s\u3000,\uff0c\u3002.;\uff1b:\uff1a!\uff01?\uff1f'\"\u201c\u201d\u2018\u2019()\uff08\uff09\[\]\uff3b\uff3d]"
)

_CN_DIGITS = "零一二三四五六七八九"


def _cn_num_to_arabic(text: str) -> str:
    """把中文数字简单归一化到阿拉伯数字，按优先级处理：

    1. "X十Y"（三十二、四十三）→ XY（32、43）
    2. "X十"（二十、三十）→ X0（20、30）
    3. "十X"（十二、十三）→ 1X（12、13）
    4. 单个汉字数字 → 0-9
    5. 孤立"十" → 10
    """
    # 1. X十Y → XY
    for i in range(1, 10):
        for j in range(1, 10):
            text = text.replace(f"{_CN_DIGITS[i]}十{_CN_DIGITS[j]}", f"{i}{j}")
    # 2. X十 → X0
    for i in range(1, 10):
        text = text.replace(f"{_CN_DIGITS[i]}十", f"{i}0")
    # 3. 十X → 1X
    for i in range(2, 10):
        text = text.replace(f"十{_CN_DIGITS[i]}", f"1{i}")
    # 4. 单个汉字
    for i, cn in enumerate(_CN_DIGITS):
        text = text.replace(cn, str(i))
    # 5. 孤立十
    text = text.replace("十", "10")
    return text


def _norm_for_evidence(text: str) -> str:
    """证据归一化：去空白标点 + 中文数字 → 阿拉伯数字。"""
    stripped = _EVIDENCE_STRIP.sub("", text or "")
    return _cn_num_to_arabic(stripped)


# ---------- 三道回查关卡 ----------

def check_evidence(evidence: Any, source_text: str) -> bool:
    """evidence 必须能在原文中原样找到（忽略空白标点 + 中文数字等价）。

    硬限：归一化后少于 3 字符视为太短，返回 False（避免 "32" 这种 2 字符误命中）。
    """
    if not evidence or not isinstance(evidence, str):
        return False
    ev = evidence.strip()
    norm_ev = _norm_for_evidence(ev)
    if len(norm_ev) < 3:
        return False
    return norm_ev in _norm_for_evidence(source_text)


def check_value_in_evidence(value: Any, raw_value: Any, evidence: str | None,
                            source_text: str, spec_type: str) -> bool:
    """归一化后的 value 必须能在 evidence 或原文中找到痕迹（反查）。

    处理的边界：
    - 中文数字归一化成 int 后，反查 int 搜不到原文的"三十二" → 同时搜 raw_value
    - date 对象 → 搜 ISO 字符串
    - list → 每个元素都要命中
    - 归一化后是 int/float 的 → 同时搜阿拉伯数字和原始字符串
    """
    if value is None or value == "" or (isinstance(value, list) and not value):
        return True  # 空值不反查

    search_target = evidence or source_text
    if not search_target:
        return False
    norm_target = _norm_for_evidence(search_target)

    # 收集候选搜索词（归一化后 + 原始值，都过一遍中文数字归一化）
    candidates: set[str] = set()

    if isinstance(value, list):
        for item in value:
            s = str(item).strip()
            if s:
                candidates.add(_norm_for_evidence(s))
        if isinstance(raw_value, list):
            for item in raw_value:
                s = str(item).strip()
                if s:
                    candidates.add(_norm_for_evidence(s))
    elif hasattr(value, "isoformat"):  # date / datetime
        candidates.add(_norm_for_evidence(value.isoformat()))
    else:
        # 归一化后的值
        s = str(value).strip()
        if s:
            candidates.add(_norm_for_evidence(s))
        # 原始值（中文数字 → 阿拉伯数字归一化的情况靠这个兜）
        if raw_value is not None and str(raw_value).strip() != s:
            rs = str(raw_value).strip()
            if rs:
                candidates.add(_norm_for_evidence(rs))

    candidates = {c for c in candidates if len(c) >= 1}
    if not candidates:
        return True

    if isinstance(value, list):
        # 列表：每个归一化元素都必须命中（允许用 raw_value 兜底）
        return all(any(c in norm_target for c in candidates if c in _norm_for_evidence(str(v)))
                   for v in value)

    return any(c in norm_target for c in candidates)


# ---------- 候选人身份校验：推荐人/联系人不能当候选人 ----------

_LABEL_NAME = re.compile(r"姓\s*名\s*[:：]\s*([\u4e00-\u9fa5·]{2,8})")
_REFERRER_WORDS = ("推荐顾问", "推荐人", "招聘顾问", "联系人", "汇报对象", "对接人", "HR")
_NAME_KEYS = {"name", "fullname", "full_name", "candidate_name", "person_name"}
_AGE_KEYS = {"age", "年龄", "current_age"}
_BIRTH_DATE = re.compile(r"((?:19|20)\d{2})\s*[年./\-]\s*(\d{1,2})")
_BIRTH_LABEL = re.compile(
    r"出生(?:日期|年月|时间|年份)?\s*[：:]\s*((?:19|20)\d{2})\s*(?:[年./\-]\s*(\d{1,2}))?"
)


def _age_from_birth(year: int, month: int | None) -> int | None:
    today = date.today()
    if month is None:
        age = today.year - year
    else:
        age = today.year - year - ((today.month, today.day) < (month, 1))
    return age if 14 <= age <= 80 else None


def _is_age_spec(spec: FieldSpec) -> bool:
    return spec.key.lower() in _AGE_KEYS or "年龄" in spec.header


def _age_from_birth_evidence(evidence: str) -> int | None:
    """从证据里的出生年月推算周岁；只接受 14-80 岁，避开毕业年份等数字。"""
    match = _BIRTH_DATE.search(evidence or "")
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return _age_from_birth(year, month)


def _birth_label_from_text(text: str) -> tuple[str, int, int | None] | None:
    """从原文「出生日期：1983.1」这类显式标签取出生年月，返回 (原文片段, 年, 月)。"""
    match = _BIRTH_LABEL.search(text or "")
    if not match:
        return None
    snippet = match.group(0).strip()
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    if month is not None and not 1 <= month <= 12:
        month = None
    return snippet, year, month


def _is_name_spec(spec: FieldSpec) -> bool:
    """只认「姓名」类字段，避免把 company_name 之类误判成人名。"""
    if any(hint in f"{spec.key}{spec.header}" for hint in ("姓名", "名字", "候选人")):
        return True
    return spec.key.lower() in _NAME_KEYS


def _candidate_label_name(text: str) -> str | None:
    """取原文里的「姓 名：xxx」，跳过「推荐人姓名：张丹」这类行。"""
    for line in text.splitlines():
        if any(word in line for word in _REFERRER_WORDS):
            continue
        match = _LABEL_NAME.search(line)
        if match:
            return match.group(1)
    return None


def _plain(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _value_is_referrer(value: Any, text: str) -> bool:
    """值是否紧跟在「推荐顾问/联系人」等角色词后面（例：推荐顾问：张丹）。"""
    target = _plain(value)
    if not target:
        return False
    for line in text.splitlines():
        flat = _plain(line)
        for role in _REFERRER_WORDS:
            idx = flat.find(role)
            if idx != -1 and target in flat[idx:idx + len(role) + 8]:
                return True
    return False


# ---------- 主流程 ----------

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

        # 姓名列：以原文「姓名：xxx」标签为准，避免把推荐顾问/联系人写成候选人
        if _is_name_spec(spec):
            labelled = _candidate_label_name(text)
            if labelled:
                expected = labelled
                if value is None:
                    results[spec.key] = CellResult(
                        value=expected, confidence=0.85, evidence=f"姓名：{expected}",
                        evidence_ok=True, status="ok", reason="取自原文「姓名」标签",
                    ).model_dump(mode="json")
                    continue
                if _plain(value) != _plain(expected):
                    results[spec.key] = CellResult(
                        value=expected, confidence=0.49, evidence=f"姓名：{expected}",
                        evidence_ok=True, status="uncertain",
                        reason="与原文「姓名」标签不一致，已按标签校正",
                    ).model_dump(mode="json")
                    continue
            elif value is not None and (
                (evidence and any(w in evidence for w in _REFERRER_WORDS))
                or _value_is_referrer(value, text)
            ):
                results[spec.key] = CellResult(
                    value=None, confidence=0.0, evidence=evidence,
                    evidence_ok=False, status="uncertain",
                    reason="疑似推荐人/联系人信息，已丢弃",
                ).model_dump(mode="json")
                continue

        # 年龄缺失时，用原文显式「出生日期」兜底推算（其他字段不这么做）
        if value is None and _is_age_spec(spec):
            birth = _birth_label_from_text(text)
            if birth:
                snippet, birth_year, birth_month = birth
                guessed = _age_from_birth(birth_year, birth_month)
                if guessed is not None:
                    if birth_month is None:
                        results[spec.key] = CellResult(
                            value=guessed, confidence=0.49, evidence=snippet,
                            evidence_ok=True, status="uncertain", derived=True,
                            reason="仅按出生年份推算，生日未知，可能相差 1 岁",
                        ).model_dump(mode="json")
                    else:
                        results[spec.key] = CellResult(
                            value=guessed, confidence=0.8, evidence=snippet,
                            evidence_ok=True,
                            status="ok" if 0.8 >= threshold else "uncertain",
                            derived=True, reason="由出生年月推算（请核对）",
                        ).model_dump(mode="json")
                    continue

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
        value_ok_in_evidence = check_value_in_evidence(value, raw_value, evidence, text, spec.type)

        # 年龄是唯一允许推算的字段：原文只给出生年月时，按当前年份算周岁
        derived = False
        if value is not None and not value_ok_in_evidence and evidence and _is_age_spec(spec):
            guessed = _age_from_birth_evidence(evidence)
            if guessed is not None and str(guessed) == str(value):
                value_ok_in_evidence = True
                derived = True

        # evidence 长度惩罚：归一化后 < 6 字（上下文太少），扣 0.15
        evidence_len_penalty = 0.0
        if evidence:
            norm_len = len(_norm_for_evidence(evidence))
            if 3 <= norm_len < 6:
                evidence_len_penalty = 0.15

        score = (0.5 * llm_conf
                 + 0.3 * (1.0 if evidence_ok else 0.0)
                 + 0.2 * (1.0 if normalized_ok else 0.0)
                 - evidence_len_penalty)

        if not evidence_ok:
            score = min(score, 0.49)
            reason = reason or "证据未能在原文中命中"
        elif not value_ok_in_evidence:
            score = min(score, 0.49)
            reason = reason or "值未能在证据中反查命中"

        if derived:
            score = min(score, 0.8)
            reason = "由出生年月推算（请核对）"

        status: CellStatus = "ok" if (evidence_ok and value_ok_in_evidence and normalized_ok and score >= threshold) else "uncertain"
        results[spec.key] = CellResult(
            value=value,
            confidence=round(score, 3),
            evidence=evidence,
            evidence_ok=evidence_ok,
            status=status,
            reason=reason,
            derived=derived,
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
