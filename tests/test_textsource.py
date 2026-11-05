"""文本来源读取：docx / doc 支持与降级提示。"""

from __future__ import annotations

from docx import Document

from pipeline.textsource import _clean_doc_text, is_supported_text_file, read_text_file


def test_docx_paragraphs_and_tables(tmp_path):
    path = tmp_path / "a.docx"
    doc = Document()
    doc.add_paragraph("姓名：陈晨")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "年龄"
    table.cell(0, 1).text = "30"
    doc.save(str(path))

    text, warnings = read_text_file(path)
    assert "姓名：陈晨" in text
    assert "年龄，30" in text
    assert warnings == []


def test_word_formats_are_supported():
    assert is_supported_text_file("简历.doc")
    assert is_supported_text_file("简历.docx")


def test_invalid_doc_falls_back_with_warning(tmp_path):
    path = tmp_path / "fake.doc"
    path.write_text("not a word file", encoding="utf-8")

    text, warnings = read_text_file(path)
    assert text == ""
    assert warnings and "另存为 .docx" in warnings[0]


def test_doc_control_chars_cleaned():
    assert _clean_doc_text("姓名：张三\r年龄：30\x07") == "姓名：张三\n年龄：30"
