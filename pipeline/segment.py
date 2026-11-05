"""文本切分：把一份文本拆成一条或多条人物记录。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME_LABEL = re.compile(r"(?:姓\s*名|名字)\s*[:：]\s*([\u4e00-\u9fa5·]{2,6})")
_NAME_FIRST = re.compile(r"^([\u4e00-\u9fa5]{2,4})[，,]")
_NAME_I_AM = re.compile(r"我叫([\u4e00-\u9fa5·]{2,6})")
_NAME_MARK = re.compile(r"(?:姓\s*名|名字)\s*[:：]")
_PHONE = re.compile(r"1[3-9]\d{9}")

# 简历里不一定写「姓名」两个字，靠这些分节标题判断是不是简历
_RESUME_HINT = re.compile(
    r"简历|候选人推荐报告|工作经历|工作经验|项目经历|项目经验|教育经历|教育背景|"
    r"自我评价|个人评价|个人优势|技能特长|专业技能|求职意向|个人信息|个人资料|基本信息|"
    r"实习经历|校园经历|荣誉奖项|获奖情况|培训经历|资格证书|语言能力|兴趣爱好"
)
# 简历开头常见标题，不能当成人名
_SECTION_TITLES = {
    "个人简历", "个人信息", "个人资料", "基本信息", "工作经历", "工作经验",
    "项目经历", "项目经验", "教育经历", "教育背景", "自我评价", "求职意向",
    "技能特长", "专业技能", "候选人推荐报告",
}


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
    stripped = text.strip()
    for pattern in (_NAME_LABEL, _NAME_I_AM, _NAME_FIRST):
        match = pattern.search(stripped)
        if match:
            return match.group(1)
    # 简历开头单独一行是姓名：要求后面紧跟联系方式/基本信息，避免把分节标题当人名
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return None
    first_line = lines[0]
    if re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", first_line) and first_line not in _SECTION_TITLES:
        rest = "\n".join(lines[1:6])
        if re.search(r"电话|手机|邮箱|联系方式|求职意向|性别|年龄|出生|学历|本科|硕士|毕业", rest):
            return first_line
    return None


def _name_split(text: str) -> list[str]:
    marks = [m.start() for m in _NAME_MARK.finditer(text)]
    if len(marks) < 2:
        return []
    parts = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            parts.append(chunk)
    return parts


def _blank_split(text: str) -> list[str]:
    return [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]


def _numbered_split(text: str) -> list[str]:
    parts = re.split(r"(?m)^\s*(?:\d{1,2}[.、)）]|（\d{1,2}）|第\d{1,2}[位条])\s*", text)
    return [p.strip() for p in parts if p.strip()]


def _auto_split(text: str) -> list[str]:
    """自动判断是单人简历还是多人文本。

    - ≥2 个「姓名：」→ 按人切分
    - ≥2 个手机号 → 明显是多人，按空行/编号切
    - 1 个「姓名：」或含简历分节标题 → 单人简历，整份一条
    - 都没有 → 先按空行，单块再试编号列表
    """
    marks = list(_NAME_MARK.finditer(text))
    if len(marks) >= 2:
        return _name_split(text)

    phones = _PHONE.findall(text)
    if len(phones) >= 2:
        blocks = _blank_split(text)
        if len(blocks) >= 2:
            return blocks
        numbered = _numbered_split(text)
        if len(numbered) >= 2:
            return numbered

    if len(marks) == 1 or _RESUME_HINT.search(text):
        return [text]
    blocks = _blank_split(text)
    if len(blocks) == 1:
        numbered = _numbered_split(text)
        if len(numbered) >= 2:
            return numbered
    return blocks


def split_records(text: str, mode: str = "none", source_name: str = "") -> list[TextRecord]:
    """按用户指定的模式切分。

    - none : 整段视为一条（默认，一个文件 = 一条记录）
    - blank: 按空行拆分
    - name : 按「姓名/名字」标签拆分（不足 2 个标签则整段一条）
    - auto : 自动判断：单人简历整份一条，多人才拆
    - page : 按 PDF 分页标记 \f 拆分（一页 = 一条，适合一页一份简历的合集）
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    if mode == "page":
        chunks = [c.strip() for c in normalized.split("\f") if c.strip()]
        if not chunks:
            chunks = [normalized]
    else:
        normalized = normalized.replace("\f", "\n\n")  # 不分页时把分页符当空行
        if mode == "none":
            chunks = [normalized]
        elif mode == "blank":
            chunks = _blank_split(normalized)
        elif mode == "name":
            chunks = _name_split(normalized) or [normalized]
        elif mode == "auto":
            chunks = _auto_split(normalized)
        else:
            raise ValueError(f"未知切分模式：{mode}")

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
