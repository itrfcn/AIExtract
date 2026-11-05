"""云端视觉模型 OCR：并发、顺序、失败回退本地。"""

from __future__ import annotations

import base64
import io
from dataclasses import replace

import pytest
from PIL import Image

from pipeline import ocr, vision
from pipeline.textsource import read_text_file


@pytest.fixture
def vision_backend(monkeypatch):
    monkeypatch.setattr(
        ocr, "settings",
        replace(ocr.settings, ocr_backend="vision", vision_concurrency=2, vision_fallback_local=True),
    )


def test_vision_backend_keeps_page_order(vision_backend, monkeypatch):
    def fake(image):
        return f"image-{image.size[0]}", []

    monkeypatch.setattr(vision, "image_to_text", fake)
    images = [Image.new("RGB", (w, 10), "white") for w in (100, 200, 300)]
    results = ocr.transcribe_images(images)

    assert [text for text, _ in results] == ["image-100", "image-200", "image-300"]


def test_vision_failure_falls_back_to_local(vision_backend, monkeypatch):
    monkeypatch.setattr(vision, "image_to_text", lambda image: ("", ["视觉模型调用失败"]))
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_pil_image", lambda image: ("本地识别：张伟", []))

    text, warnings = ocr.transcribe_images([Image.new("RGB", (50, 50), "white")])[0]

    assert text == "本地识别：张伟"
    assert any("回退本地 OCR" in w for w in warnings)


def test_vision_failure_without_fallback_returns_warning(vision_backend, monkeypatch):
    monkeypatch.setattr(ocr, "settings", replace(ocr.settings, vision_fallback_local=False))
    monkeypatch.setattr(vision, "image_to_text", lambda image: ("", ["视觉模型调用失败"]))

    text, warnings = ocr.transcribe_images([Image.new("RGB", (50, 50), "white")])[0]

    assert text == ""
    assert any("视觉模型调用失败" in w for w in warnings)


def test_scanned_pdf_uses_vision_per_page(vision_backend, monkeypatch, tmp_path):
    path = tmp_path / "scan.pdf"
    pages = [Image.new("RGB", (200, 100), "white") for _ in range(2)]
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])

    monkeypatch.setattr(vision, "image_to_text", lambda image: ("姓名：张伟", []))

    text, warnings = read_text_file(path)

    assert text.split("\f") == ["姓名：张伟", "姓名：张伟"]
    assert any("OCR" in w for w in warnings)


def test_prepare_image_downscales_to_limit():
    uri = vision.prepare_image(Image.new("RGB", (4000, 3000), "white"), max_side=800)

    assert uri.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) <= 800
