"""表头驱动的字段定义：列名 -> FieldSpec -> JSON Schema。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

FieldType = Literal["string", "integer", "number", "date", "enum", "list"]
MappingOrigin = Literal["dict", "fuzzy", "llm", "custom", "user"]


class FieldSpec(BaseModel):
    """一列对应一个抽取字段。column_index 是回填的锚点，key 是给 LLM 的字段名。"""

    column_index: int
    header: str
    key: str
    type: FieldType = "string"
    required: bool = False
    description: str = ""
    enum_values: list[str] | None = None
    normalizers: list[str] = Field(default_factory=list)
    enabled: bool = True
    origin: MappingOrigin = "custom"
    mapping_score: float = 1.0
    sample_values: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CanonicalField:
    key: str
    label: str
    synonyms: tuple[str, ...]
    type: FieldType
    description: str
    normalizers: tuple[str, ...] = ()
    enum_values: tuple[str, ...] | None = None
    required: bool = False


CANONICAL_FIELDS: tuple[CanonicalField, ...] = (
    CanonicalField("name", "姓名", ("姓名", "名字", "人员姓名", "员工姓名", "客户姓名", "联系人", "name"), "string",
                   "文本中出现的人物姓名，只保留姓名本身。", ("name",), required=True),
    CanonicalField("age", "年龄", ("年龄", "岁数", "年纪", "年龄岁", "age"), "integer",
                   "人物当前年龄，必须是整数岁数。", ("age",)),
    CanonicalField("gender", "性别", ("性别", "sex", "gender"), "enum",
                   "人物性别。", ("gender_cn",), enum_values=("男", "女", "其他")),
    CanonicalField("birth_date", "出生日期", ("出生日期", "生日", "出生年月", "出生年月日", "出生时间", "出生", "birthday", "dateofbirth"),
                   "date", "人物出生日期，输出 ISO 格式（1993-04-12 或 1993-04）。", ("date",)),
    CanonicalField("phone", "手机号", ("手机号", "手机号码", "手机", "电话", "联系电话", "联系方式", "电话号码", "phone", "tel", "mobile"),
                   "string", "人物手机号或联系电话。", ("phone",)),
    CanonicalField("email", "邮箱", ("邮箱", "电子邮箱", "电子邮件", "email", "e-mail"), "string",
                   "人物电子邮箱地址。", ()),
    CanonicalField("city", "城市", ("城市", "所在地", "现居地", "现居城市", "居住城市", "所在城市", "常住城市"), "string",
                   "人物当前所在城市。", ()),
    CanonicalField("address", "地址", ("地址", "住址", "通讯地址", "详细地址", "家庭住址", "address"), "string",
                   "人物详细地址。", ()),
    CanonicalField("company", "公司", ("公司", "公司名称", "单位", "工作单位", "所在单位", "现公司", "就职公司", "企业", "company", "employer"),
                   "string", "人物就职或所在的公司/单位名称。", ()),
    CanonicalField("job_title", "职位", ("职位", "岗位", "职务", "头衔", "岗位名称", "现任职位", "position", "title"), "string",
                   "人物担任的职位或岗位。", ()),
    CanonicalField("department", "部门", ("部门", "所属部门", "所在部门", "department"), "string",
                   "人物所在部门。", ()),
    CanonicalField("education", "学历", ("学历", "最高学历", "教育程度", "文化程度", "education"), "enum",
                   "人物最高学历。", ("education_cn",), enum_values=("博士", "硕士", "本科", "大专", "高中及以下", "其他")),
    CanonicalField("school", "学校", ("学校", "毕业院校", "院校", "母校", "就读学校", "school", "university"), "string",
                   "人物毕业或在读学校。", ()),
    CanonicalField("major", "专业", ("专业", "所学专业", "毕业专业", "major"), "string",
                   "人物所学专业。", ()),
    CanonicalField("hobbies", "兴趣爱好", ("兴趣爱好", "爱好", "兴趣", "特长", "个人爱好", "hobbies", "interests"), "list",
                   "人物的兴趣爱好，多个值用数组返回。", ("split_list",)),
    CanonicalField("salary", "薪资", ("薪资", "期望薪资", "月薪", "年薪", "工资", "salary"), "string",
                   "人物收入或期望薪资，保留原文单位。", ()),
    CanonicalField("id_number", "身份证号", ("身份证号", "身份证号码", "证件号码", "身份证", "id", "idnumber"), "string",
                   "人物身份证号，只保留号码本身。", ("id_number",)),
    CanonicalField("marital_status", "婚姻状况", ("婚姻状况", "婚否", "婚姻状态", "marital"), "enum",
                   "人物婚姻状况。", (), enum_values=("未婚", "已婚", "离异", "丧偶")),
    CanonicalField("hometown", "籍贯", ("籍贯", "家乡", "户籍所在地", "hometown"), "string",
                   "人物籍贯或家乡。", ()),
    CanonicalField("work_years", "工作年限", ("工作年限", "工作经验", "从业年限", "经验", "workyears"), "integer",
                   "人物工作年限（年）。", ("years",)),
    CanonicalField("wechat", "微信", ("微信", "微信号", "wechat"), "string",
                   "人物微信号。", ()),
    CanonicalField("notes", "备注", ("备注", "说明", "补充说明", "其他", "其他信息", "remark", "remarks", "note"), "string",
                   "其他值得记录的补充信息。", ()),
)

CANON_BY_KEY = {f.key: f for f in CANONICAL_FIELDS}

_PUNCT_RE = re.compile(r"[\s\u3000:：,，.。;；()（）\[\]【】{}<>《》\-_/\\|、·*'\"“”‘’]+")
_BRACKET_RE = re.compile(r"[（(【\[].*?[）)】\]]")


def normalize_header(header: str) -> str:
    """表头归一化：去括号内容、空白、标点，全角转半角。"""
    s = unicodedata.normalize("NFKC", header or "").lower()
    s = _BRACKET_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    return s


def sanitize_key(key: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5]+", "_", (key or "").strip()).strip("_")
    if not s:
        s = "field"
    if s[0].isdigit():
        s = "f_" + s
    return s


_SYNONYM_INDEX: dict[str, CanonicalField] = {}
for _f in CANONICAL_FIELDS:
    for _syn in (_f.label, *_f.synonyms, _f.key):
        _SYNONYM_INDEX.setdefault(normalize_header(_syn), _f)

_FUZZY_CHOICES: list[str] = list(_SYNONYM_INDEX.keys())


def _fuzzy_match(header: str, cutoff: float = 85.0) -> tuple[CanonicalField | None, float]:
    from rapidfuzz import fuzz, process

    norm = normalize_header(header)
    if not norm:
        return None, 0.0
    result = process.extractOne(norm, _FUZZY_CHOICES, scorer=fuzz.WRatio, score_cutoff=cutoff)
    if not result:
        return None, 0.0
    _, score, idx = result
    return _SYNONYM_INDEX[_FUZZY_CHOICES[idx]], float(score) / 100.0


def _guess_type(header: str, samples: list[str]) -> tuple[FieldType, list[str] | None, list[str]]:
    h = normalize_header(header)
    if "性别" in h:
        return "enum", ["男", "女", "其他"], ["gender_cn"]
    if any(k in h for k in ("日期", "生日", "时间", "年月")):
        return "date", None, ["date"]
    if any(k in h for k in ("年龄", "岁数", "年限", "数量", "人数", "次数")):
        return "integer", None, ["age"] if "年龄" in h or "岁数" in h else []
    if any(k in h for k in ("爱好", "兴趣", "特长", "标签", "技能")):
        return "list", None, ["split_list"]

    values = [v.strip() for v in samples if v and v.strip()]
    if values and len(values) >= 2:
        if all(re.fullmatch(r"\d{1,3}", v) for v in values):
            return "integer", None, []
        if all(re.fullmatch(r"\d{4}[-/.年]\d{1,2}([-/.月]\d{1,2}日?)?", v) for v in values):
            return "date", None, ["date"]
    return "string", None, []


def _build_spec(
    index: int,
    header: str,
    canonical: CanonicalField | None,
    origin: MappingOrigin,
    score: float,
    samples: list[str] | None = None,
    key_override: str | None = None,
) -> FieldSpec:
    samples = samples or []
    if canonical:
        return FieldSpec(
            column_index=index,
            header=header,
            key=canonical.key,
            type=canonical.type,
            required=canonical.required,
            description=canonical.description,
            enum_values=list(canonical.enum_values) if canonical.enum_values else None,
            normalizers=list(canonical.normalizers),
            origin=origin,
            mapping_score=score,
            sample_values=samples[:5],
        )

    ftype, enum_values, normalizers = _guess_type(header, samples)
    desc = f"从文本中抽取与「{header}」直接相关的最具体信息；文本没有明确提到时返回 null。"
    return FieldSpec(
        column_index=index,
        header=header,
        key=key_override or sanitize_key(header) or f"col_{index + 1}",
        type=ftype,
        required=False,
        description=desc,
        enum_values=enum_values,
        normalizers=normalizers,
        origin=origin,
        mapping_score=score,
        sample_values=samples[:5],
    )


def _llm_map(headers: list[str], provider) -> dict[str, dict]:
    """让 LLM 为未命中列名给出字段定义；失败时静默降级。"""
    if not headers:
        return {}
    canon_lines = "\n".join(
        f"- {f.key}｜{f.label}｜别名：{'、'.join(f.synonyms[:6])}｜类型：{f.type}"
        for f in CANONICAL_FIELDS
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是表格字段映射助手。给定用户表格的列名，把每个列名映射到最合适的规范字段 key；"
                "无法对应到规范字段时 key 用 custom，并给出 type（string/integer/number/date/enum/list）"
                "与一句中文抽取说明。只输出 JSON，不要解释。\n\n"
                f"规范字段：\n{canon_lines}\n\n"
                '输出格式：{"mappings": {"列名": {"key": "规范key或custom", "type": "string", '
                '"description": "说明", "enum": ["可选值"]}}}'
            ),
        },
        {"role": "user", "content": json.dumps(headers, ensure_ascii=False)},
    ]
    try:
        data = provider.chat_json(messages)
    except Exception:
        return {}
    mappings = data.get("mappings") if isinstance(data, dict) else None
    if not isinstance(mappings, dict):
        return {}
    return {str(k): v for k, v in mappings.items() if isinstance(v, dict)}


def map_headers(
    headers: list[str],
    samples: dict[int, list[str]] | None = None,
    use_llm: bool = True,
    provider=None,
) -> list[FieldSpec]:
    """列名三级映射：同义词词典 -> 模糊匹配 -> LLM 兜底 -> 自定义列。"""
    samples = samples or {}
    specs: list[FieldSpec] = []
    unresolved: list[tuple[int, str]] = []

    for idx, raw_header in enumerate(headers):
        header = (raw_header or "").strip() or f"第{idx + 1}列"
        norm = normalize_header(header)
        col_samples = samples.get(idx, [])

        canon = _SYNONYM_INDEX.get(norm) if norm else None
        if canon:
            specs.append(_build_spec(idx, header, canon, "dict", 1.0, col_samples))
            continue

        canon, score = _fuzzy_match(header)
        if canon:
            specs.append(_build_spec(idx, header, canon, "fuzzy", score, col_samples))
            continue

        specs.append(_build_spec(idx, header, None, "custom", 0.0, col_samples))
        unresolved.append((idx, header))

    if use_llm and provider is not None and unresolved:
        llm_result = _llm_map([h for _, h in unresolved], provider)
        if llm_result:
            for idx, header in unresolved:
                item = llm_result.get(header)
                if not item:
                    continue
                key = str(item.get("key") or "custom")
                if key in CANON_BY_KEY:
                    specs[idx] = _build_spec(idx, header, CANON_BY_KEY[key], "llm", 0.9, samples.get(idx, []))
                else:
                    spec = specs[idx]
                    ftype = item.get("type")
                    if ftype in ("string", "integer", "number", "date", "enum", "list"):
                        spec.type = ftype
                    if isinstance(item.get("enum"), list):
                        spec.enum_values = [str(v) for v in item["enum"]]
                    if item.get("description"):
                        spec.description = str(item["description"])
                    spec.origin = "llm"
                    spec.mapping_score = 0.85

    # 去重 key：同一张表允许两个"电话"列
    used: dict[str, int] = {}
    for spec in specs:
        base = spec.key
        if base in used:
            used[base] += 1
            spec.key = f"{base}_{used[base]}"
        else:
            used[base] = 1

    return specs


def build_json_schema(specs: list[FieldSpec]) -> dict:
    """生成抽取用的 JSON Schema：每个字段返回 {value, confidence, evidence}。"""
    enabled = [s for s in specs if s.enabled]
    props: dict[str, dict] = {}

    for spec in enabled:
        if spec.type == "date":
            base_type, desc = "string", spec.description + "（ISO 格式，如 1993-04-12；仅有年月用 1993-04）"
        elif spec.type == "enum":
            allowed = spec.enum_values or []
            base_type = "string"
            desc = spec.description + (f"（只能取以下值之一：{'、'.join(allowed)}）" if allowed else "")
        elif spec.type == "list":
            base_type, desc = "array", spec.description + "（多个值用数组，单值也用数组）"
        elif spec.type == "integer":
            base_type, desc = "integer", spec.description
        elif spec.type == "number":
            base_type, desc = "number", spec.description
        else:
            base_type, desc = "string", spec.description

        value_schema: dict = {"type": [base_type, "null"], "description": desc}
        if spec.type == "list":
            value_schema["items"] = {"type": "string"}

        props[spec.key] = {
            "type": "object",
            "properties": {
                "value": value_schema,
                "confidence": {"type": "number", "description": "0-1，对本次抽取的把握程度"},
                "evidence": {"type": ["string", "null"], "description": "支持该值的原文连续片段，逐字复制；没有则为 null"},
            },
            "required": ["value", "confidence", "evidence"],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def schema_hash(specs: list[FieldSpec]) -> str:
    payload = json.dumps(
        [s.model_dump(include={"key", "type", "enum_values", "description"}) for s in specs if s.enabled],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
