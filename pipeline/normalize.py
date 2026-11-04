"""字段归一化：年龄、性别、日期、电话、多值列表，以及"未提及"判定。"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .schema import FieldSpec

MISSING_WORDS = {
    "", "-", "—", "/", "无", "暂无", "没有", "不详", "未知", "未提及", "未提供",
    "n/a", "na", "none", "null", "unknown", "待补充", "待定",
}

_FULL_DATE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_YEAR_MONTH = re.compile(r"(\d{4})[-/.年](\d{1,2})月?")
_YEAR_ONLY = re.compile(r"(\d{4})年?")

_GENDER_MALE = {"男", "男性", "男生", "男士", "先生", "m", "male", "man", "boy"}
_GENDER_FEMALE = {"女", "女性", "女生", "女士", "小姐", "f", "female", "woman", "girl"}


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, set)):
        return len([v for v in value if str(v).strip()]) == 0
    if isinstance(value, str):
        return value.strip().lower().rstrip("。.") in MISSING_WORDS
    return False


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value)
    return str(value).strip()


def normalize_name(value: Any) -> tuple[Any, bool, str]:
    text = _as_text(value)
    text = re.sub(r"^(姓名|名字|名称)\s*[:：]\s*", "", text)
    text = re.sub(r"[\s　]+", "", text)
    return (text or None), bool(text), "" if text else "姓名为空"


def normalize_age(value: Any) -> tuple[Any, bool, str]:
    if isinstance(value, bool):
        return value, False, "年龄不是数字"
    if isinstance(value, (int, float)):
        age = int(value)
    else:
        match = re.search(r"\d{1,3}", _as_text(value))
        if not match:
            return value, False, "无法解析年龄"
        age = int(match.group(0))
    if not 0 <= age <= 120:
        return age, False, f"年龄 {age} 超出 0-120"
    return age, True, ""


def normalize_gender(value: Any) -> tuple[Any, bool, str]:
    text = _as_text(value).strip().lower()
    if text in _GENDER_MALE:
        return "男", True, ""
    if text in _GENDER_FEMALE:
        return "女", True, ""
    if text in {"其他", "other"}:
        return "其他", True, ""
    # 从"男，1993年"这类片段里取第一个性别词
    match = re.search(r"(男|女)", text)
    if match:
        return ("男" if match.group(1) == "男" else "女"), False, "从片段中推断性别"
    return value, False, "无法识别性别"


def parse_date(value: Any) -> str | None:
    """把各种中文/数字日期转成 ISO 字符串，失败返回 None。"""
    text = _as_text(value)
    if not text:
        return None

    m = _FULL_DATE.search(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None

    m = _YEAR_MONTH.search(text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"

    m = _YEAR_ONLY.search(text)
    if m:
        return f"{int(m.group(1)):04d}"

    return None


def normalize_date(value: Any) -> tuple[Any, bool, str]:
    iso = parse_date(value)
    if iso is None:
        return value, False, "无法解析日期"
    return iso, True, ""


def normalize_phone(value: Any) -> tuple[Any, bool, str]:
    digits = re.sub(r"[^\d+]", "", _as_text(value))
    if digits.startswith("+86"):
        digits = digits[3:]
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if re.fullmatch(r"1[3-9]\d{9}", digits):
        return digits, True, ""
    return value, False, "不是有效的 11 位手机号"


def normalize_id(value: Any) -> tuple[Any, bool, str]:
    text = re.sub(r"\s", "", _as_text(value)).upper()
    if re.fullmatch(r"\d{15}|\d{17}[\dX]", text):
        return text, True, ""
    return value, False, "身份证号格式不符"


def normalize_education(value: Any) -> tuple[Any, bool, str]:
    text = _as_text(value)
    rules = (
        (("博士",), "博士"),
        (("硕士", "研究生"), "硕士"),
        (("本科", "学士", "大学"), "本科"),
        (("大专", "专科", "高职"), "大专"),
        (("高中", "中专", "职高", "初中", "技校"), "高中及以下"),
    )
    for keys, target in rules:
        if any(k in text for k in keys):
            return target, True, ""
    return value, False, "未识别的学历"


def normalize_years(value: Any) -> tuple[Any, bool, str]:
    if isinstance(value, bool):
        return value, False, "年限不是数字"
    if isinstance(value, (int, float)):
        years = int(value)
    else:
        match = re.search(r"\d{1,2}", _as_text(value))
        if not match:
            return value, False, "无法解析年限"
        years = int(match.group(0))
    if not 0 <= years <= 60:
        return years, False, f"年限 {years} 超出 0-60"
    return years, True, ""


def split_list(value: Any) -> tuple[Any, bool, str]:
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = re.split(r"[、,，;；/|]|和|及", _as_text(value))
    cleaned: list[str] = []
    for part in parts:
        item = re.sub(r"[\s　]+", "", str(part)).strip("。.等 ")
        if item and item not in MISSING_WORDS and item not in cleaned:
            cleaned.append(item)
    return cleaned, bool(cleaned), "" if cleaned else "列表为空"


NORMALIZERS = {
    "name": normalize_name,
    "age": normalize_age,
    "gender_cn": normalize_gender,
    "date": normalize_date,
    "phone": normalize_phone,
    "id_number": normalize_id,
    "education_cn": normalize_education,
    "years": normalize_years,
    "split_list": split_list,
}


def normalize_value(value: Any, spec: FieldSpec) -> tuple[Any, bool, str]:
    """返回 (归一化值, 是否通过校验, 失败原因)。"""
    if is_missing(value):
        return None, True, "empty"

    ok = True
    reason = ""
    for name in spec.normalizers:
        func = NORMALIZERS.get(name)
        if not func:
            continue
        value, ok_part, reason_part = func(value)
        ok = ok and ok_part
        if not ok_part and reason_part:
            reason = reason_part
        if value is None:
            return None, True, "empty"

    if spec.type == "list" and not isinstance(value, list):
        value, ok_part, _ = split_list(value)
        ok = ok and ok_part
    elif spec.type == "string" and isinstance(value, list):
        value = "、".join(str(v) for v in value if str(v).strip())

    if isinstance(value, str):
        value = re.sub(r"[\s　]+", " ", value).strip() or None
    if value is None:
        return None, True, "empty"

    if spec.enum_values and value not in spec.enum_values:
        return value, False, f"{value} 不在可选值 {spec.enum_values} 内"
    if spec.type == "integer":
        try:
            value = int(value)
        except (TypeError, ValueError):
            return value, False, "应为整数"
    if spec.type == "number":
        try:
            value = float(value)
        except (TypeError, ValueError):
            return value, False, "应为数字"

    return value, ok, reason


def normalize_for_export(value: Any, spec: FieldSpec) -> Any:
    """导出前转成 Excel 友好的类型：日期写 date 对象，列表拼成中文顿号。"""
    if value is None or is_missing(value):
        return None
    if spec.type == "list":
        if isinstance(value, (list, tuple)):
            return "、".join(str(v) for v in value if str(v).strip())
        return str(value).strip()
    if spec.type == "date":
        iso = parse_date(value)
        if iso:
            if len(iso) == 10:
                try:
                    return date.fromisoformat(iso)
                except ValueError:
                    return iso
        return iso
    if spec.type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value).strip()
    if spec.type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value).strip()
    return str(value).strip()
