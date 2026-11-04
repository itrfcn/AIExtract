"""文本切分：把一份文本拆成一条或多条人物记录。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME_LABEL = re.compile(r"(?:姓名|名字)\s*[:：]\s*([\u4e00-\u9fa5·]{2,6})")
_NAME_FIRST = re.compile(r"^([\u4e00-\u9fa5]{2,4})[，,]")
_NAME_I_AM = re.compile(r"我叫([\u4e00-\u9fa5·]{2,6})")


@dataclass
class TextRecord:
    record_id: str
    text: str
    name_hint: str | None = None
    source_name: str = ""
    index: int = 0
    start: int = 0
    end: int = 0
    warnings: list[str] = field(default_factory=list)


def guess_name(text: str) -> str | None:
    for pattern in (_NAME_LABEL, _NAME_I_AM, _NAME_FIRST):
        match = pattern.search(text.strip())
        if match:
            return match.group(1)
    return None


def _numbered_split(text: str) -> list[str]:
    parts = re.split(r"(?m)^\s*(?:\d{1,2}[.、)）]|（\d{1,2}）|第\d{1,2}[位条])\s*", text)
    return [p.strip() for p in parts if p.strip()]


def _name_split(text: str) -> list[str]:
    marks = [m.start() for m in re.finditer(r"(?:姓名|名字)\s*[:：]", text)]
    if len(marks) < 2:
        return []
    parts = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            parts.append(chunk)
    return parts


def _blocks(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) == 1:
        numbered = _numbered_split(blocks[0])
        if len(numbered) >= 2:
            return numbered
        by_name = _name_split(blocks[0])
        if len(by_name) >= 2:
            return by_name
    return blocks


def split_records(text: str, mode: str = "auto", source_name: str = "") -> list[TextRecord]:
    """mode: auto 按空行/编号/姓名多段切分；single 整段视为一条记录。"""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    if mode == "single":
        chunks = [normalized]
    else:
        chunks = _blocks(normalized)

    records: list[TextRecord] = []
    cursor = 0
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < 4:
            continue
        start = normalized.find(chunk, cursor)
        if start == -1:
            start = cursor
            end = start + len(chunk)
        else:
            end = start + len(chunk)
        cursor = end
        index = len(records)
        records.append(
            TextRecord(
                record_id=f"r{index + 1}",
                text=chunk,
                name_hint=guess_name(chunk),
                source_name=source_name,
                index=index,
                start=start,
                end=end,
            )
        )

    if chunks and not records:
        records.append(TextRecord(record_id="r1", text=normalized, name_hint=guess_name(normalized),
                                  source_name=source_name, index=0, start=0, end=len(normalized)))

    for record in records:
        if len(record.text) > 5000:
            record.warnings.append("单条记录超过 5000 字，建议拆分后再抽取")
    return records
