"""OCR：扫描版 PDF / 图片 → 文字。

两种后端（OCR_BACKEND 控制）：
- vision（默认）：云端视觉模型直接读页图，中文版面/双栏更准，走 LLM_API_KEY；
- rapidocr：本地离线模型（pip install rapidocr-onnxruntime）。

vision 失败且 VISION_FALLBACK_LOCAL=1 时自动回退本地 OCR。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import settings
from . import vision

ENGINE_HINT = "pip install rapidocr-onnxruntime"


def backend() -> str:
    return getattr(settings, "ocr_backend", "rapidocr")


def is_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺依赖或 onnxruntime 在本机加载失败
        return False
    return True


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _run(image: Any) -> tuple[str, list[str]]:
    try:
        engine = _engine()
    except Exception as exc:  # noqa: BLE001 - 模型/运行时问题要能报出来
        return "", [f"OCR 引擎加载失败：{exc}；请检查 {ENGINE_HINT}"]

    try:
        result, _elapse = engine(image)
    except Exception as exc:  # noqa: BLE001
        return "", [f"OCR 识别失败：{exc}"]

    lines = [str(item[1]).strip() for item in (result or []) if len(item) > 1 and str(item[1]).strip()]
    if not lines:
        return "", ["OCR 未识别到文字"]
    return "\n".join(lines), []


def ocr_pil_image(image) -> tuple[str, list[str]]:
    """对 PIL.Image 做 OCR。"""
    import numpy as np

    return _run(np.array(image.convert("RGB")))


def ocr_image_file(path: str | Path) -> tuple[str, list[str]]:
    """对图片文件（png/jpg/tif…）做识别（按当前后端）。"""
    from PIL import Image

    try:
        with Image.open(path) as image:
            return transcribe_images([image.copy()])[0]
    except Exception as exc:  # noqa: BLE001 - 损坏/被加密的图片
        return "", [f"图片无法打开：{exc}"]


def _vision_with_fallback(image) -> tuple[str, list[str]]:
    text, warnings = vision.image_to_text(image)
    if text.strip():
        return text, warnings
    if getattr(settings, "vision_fallback_local", True) and is_available():
        local_text, local_warnings = ocr_pil_image(image)
        if local_text.strip():
            return local_text, warnings + ["视觉模型未返回结果，已回退本地 OCR"] + local_warnings
        return "", warnings + local_warnings
    return "", warnings or ["视觉模型未返回文字"]


def transcribe_images(images: list) -> list[tuple[str, list[str]]]:
    """批量识别 PIL.Image，按传入顺序返回 [(文本, 警告)]。

    vision 后端按 VISION_CONCURRENCY 并发（网络等待为主，并发收益明显）。
    """
    if not images:
        return []

    if backend() == "vision":
        workers = max(1, min(len(images), getattr(settings, "vision_concurrency", 4)))
        if workers == 1:
            return [_vision_with_fallback(image) for image in images]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vision-ocr") as pool:
            return list(pool.map(_vision_with_fallback, images))

    if not is_available():
        return [("", [f"图片需要 OCR：{ENGINE_HINT}"]) for _ in images]
    return [ocr_pil_image(image) for image in images]
