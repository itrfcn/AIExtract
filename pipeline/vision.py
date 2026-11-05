"""云端视觉模型读页图：把简历扫描页按阅读顺序转写成纯文本。

用于替代本地 OCR（rapidocr）：中文版面、双栏、设计稿简历的准确率更高，
速度取决于网络，一般每页数秒。
"""

from __future__ import annotations

import base64
import io
import random
import re
import time
from functools import lru_cache

from PIL import Image

from core.config import Settings, settings

SYSTEM_PROMPT = """你是简历 OCR 引擎。请把图片中的简历按阅读顺序逐行转写成纯文本。

规则：
1. 双栏排版先读完左栏再读右栏，同一栏内从上到下；不要把两栏文字交错拼接。
2. 逐字转写原文，不要总结、不要改写、不要补全、不要翻译。
3. 表格用「单元格 | 单元格」表示，一行表格占一行文本。
4. 图片里没有文字时输出空字符串。
5. 只输出转写结果，不要加任何解释、标题或 Markdown 代码块。"""

USER_PROMPT = "请转写这张简历图片。"


@lru_cache(maxsize=4)
def _client(base_url: str, api_key: str, timeout: float):
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout, max_retries=0)


def prepare_image(image: Image.Image, max_side: int = 1600) -> str:
    """缩放 + 压成 JPEG，返回 data URI（控制请求体积和 token 成本）。"""
    rgb = image.convert("RGB")
    if max(rgb.size) > max_side:
        rgb.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _clean(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|text)?\s*|\s*```$", "", text, flags=re.S)
    return text.strip()


def image_to_text(image: Image.Image, cfg: Settings | None = None) -> tuple[str, list[str]]:
    """返回 (转写文本, 警告)。失败时不抛异常，返回空文本 + 可读警告。"""
    cfg = cfg or settings
    if not cfg.api_key:
        return "", ["视觉模型不可用：缺少 LLM_API_KEY"]

    model = cfg.vision_model or cfg.model
    data_uri = prepare_image(image, cfg.vision_max_side)
    client = _client(cfg.base_url, cfg.api_key, cfg.timeout)

    attempts = cfg.max_retries + 1
    last_error = ""
    for attempt in range(attempts):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": USER_PROMPT},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    },
                ],
                temperature=0,
                max_tokens=2400,
            )
            content = response.choices[0].message.content if response.choices else ""
            text = _clean(content or "")
            if not text:
                return "", [f"视觉模型返回空文本（{model}）"]
            return text, []
        except Exception as exc:  # noqa: BLE001 - 统一降级为警告
            last_error = str(exc)
            if attempt >= attempts - 1:
                break
            time.sleep(min(20.0, 2 ** attempt) + random.uniform(0, 0.4))

    return "", [f"视觉模型调用失败（{model}）：{last_error[:160]}"]
