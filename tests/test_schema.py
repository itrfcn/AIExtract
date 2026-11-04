"""表头映射与 JSON Schema 生成。"""

from __future__ import annotations

from pipeline.schema import build_json_schema, map_headers, normalize_header


def _by_header(specs):
    return {s.header: s for s in specs}


def test_mapping_three_levels():
    specs = map_headers(
        ["姓名", "年龄", "性别", "兴趣爱好", "出生年月", "毕业学校", "电话", "电话", "员工编号"],
        use_llm=False,
    )
    by_header = _by_header(specs)

    assert by_header["姓名"].key == "name"
    assert by_header["姓名"].origin == "dict"
    assert by_header["年龄"].type == "integer"
    assert by_header["性别"].enum_values == ["男", "女", "其他"]
    assert by_header["兴趣爱好"].type == "list"
    assert by_header["出生年月"].key == "birth_date"
    assert by_header["毕业学校"].key == "school", "模糊匹配应命中毕业院校"
    assert by_header["毕业学校"].origin == "fuzzy"
    assert by_header["员工编号"].origin == "custom"
    assert by_header["员工编号"].type == "string"

    # 重名列必须生成唯一 key，但仍保留原列名
    phones = [s for s in specs if s.header == "电话"]
    assert phones[0].key == "phone" and phones[1].key == "phone_2"
    assert phones[0].column_index != phones[1].column_index


def test_normalize_header_handles_units_and_brackets():
    assert normalize_header("年龄（岁）") == "年龄"
    assert normalize_header(" 性别（男/女） ") == "性别"


def test_schema_shape():
    specs = map_headers(["姓名", "年龄", "性别", "兴趣爱好", "出生日期"], use_llm=False)
    schema = build_json_schema(specs)

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"name", "age", "gender", "hobbies", "birth_date"}

    name_value = schema["properties"]["name"]["properties"]["value"]
    assert name_value["type"] == ["string", "null"]

    hobby_value = schema["properties"]["hobbies"]["properties"]["value"]
    assert hobby_value["type"] == ["array", "null"]
    assert hobby_value["items"] == {"type": "string"}

    # 每个字段都必须带 value/confidence/evidence
    for prop in schema["properties"].values():
        assert set(prop["properties"]) == {"value", "confidence", "evidence"}
        assert prop["required"] == ["value", "confidence", "evidence"]


def test_disabled_column_excluded_from_schema():
    specs = map_headers(["姓名", "备注"], use_llm=False)
    specs[1].enabled = False
    schema = build_json_schema(specs)
    assert "notes" not in schema["properties"]
