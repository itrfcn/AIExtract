"""OCR 接入：图片直读、扫描版 PDF 回退、缺依赖时给提示。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from PIL import Image

from pipeline import ocr
from pipeline.textsource import is_supported_text_file, read_text_file


@pytest.fixture(autouse=True)
def _local_ocr_backend(monkeypatch):
    """这里的用例验证本地 rapidocr 路径；云端 vision 另有 test_vision.py。"""
    monkeypatch.setattr(ocr, "settings", replace(ocr.settings, ocr_backend="rapidocr"))


def test_image_files_are_supported():
    for name in ("a.png", "b.jpg", "c.jpeg", "d.tif", "e.webp"):
        assert is_supported_text_file(name)


def test_image_reads_via_ocr(tmp_path, monkeypatch):
    path = tmp_path / "resume.png"
    Image.new("RGB", (40, 20), "white").save(path)

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image_file", lambda p: ("姓名：张伟\n年龄：32", []))

    text, warnings = read_text_file(path)
    assert "张伟" in text
    assert warnings == []


def test_image_without_ocr_engine_gives_hint(tmp_path, monkeypatch):
    path = tmp_path / "resume.png"
    Image.new("RGB", (40, 20), "white").save(path)

    monkeypatch.setattr(ocr, "is_available", lambda: False)

    text, warnings = read_text_file(path)
    assert text == ""
    assert any("OCR" in w for w in warnings)


def test_scanned_pdf_falls_back_to_ocr(tmp_path, monkeypatch):
    """没有文字层、只有图片的 PDF → 渲染成图后走 OCR。"""
    path = tmp_path / "scan.pdf"
    Image.new("RGB", (300, 120), "white").save(path, "PDF")

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    calls: list[tuple[int, int]] = []

    def fake_ocr(image):
        calls.append(image.size)
        return "姓名：李四", []

    monkeypatch.setattr(ocr, "ocr_pil_image", fake_ocr)
    monkeypatch.setattr(ocr, "ocr_image_file", lambda p: ("", []))

    text, warnings = read_text_file(path)
    assert "李四" in text
    assert calls, "应当把页面渲染成图片后调用 OCR"
    assert any("OCR" in w for w in warnings)


def test_scanned_pdf_keeps_page_break_marker(tmp_path, monkeypatch):
    """多页 PDF 用 \\f 分隔，供 --split page 使用。"""
    path = tmp_path / "two_pages.pdf"
    pages = [Image.new("RGB", (200, 100), "white") for _ in range(2)]
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    outputs = iter(["第一页：张伟", "第二页：李娜"])

    def fake_ocr(image):
        return next(outputs), []

    monkeypatch.setattr(ocr, "ocr_pil_image", fake_ocr)

    text, _warnings = read_text_file(path)
    assert text.split("\f") == ["第一页：张伟", "第二页：李娜"]


def test_text_pdf_does_not_use_ocr(tmp_path, monkeypatch):
    """有文字层的 PDF 不触发 OCR。"""
    path = tmp_path / "text.pdf"
    # 用 reportlab 生成带文字层的 PDF
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    c.drawString(72, 720, "Name: Zhang Wei, Age: 32")
    c.save()

    def fail(*args, **kwargs):
        raise AssertionError("有文字层时不应调用 OCR")

    monkeypatch.setattr(ocr, "ocr_pil_image", fail)
    text, _warnings = read_text_file(path)
    assert "Zhang Wei" in text
