"""全局配置：从 .env / 环境变量读取。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)

# provider -> (默认 base_url, 默认模型, 是否需要 api key)
PROVIDER_DEFAULTS: dict[str, tuple[str, str, bool]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", True),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", True),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b", False),
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    provider: str
    base_url: str
    api_key: str
    model: str
    concurrency: int = 4
    qps: float = 3.0
    max_retries: int = 3
    timeout: float = 60.0
    confidence_threshold: float = 0.75
    data_dir: Path = ROOT / "data"
    output_dir: Path = ROOT / "outputs"
    db_path: Path = ROOT / "data" / "jobs.db"
    # OCR：vision = 云端视觉模型读页图（默认）；rapidocr = 本地离线 OCR
    ocr_backend: str = "vision"
    vision_model: str = ""
    vision_concurrency: int = 4
    vision_max_side: int = 1600
    vision_fallback_local: bool = True

    @property
    def provider_label(self) -> str:
        return f"{self.provider} · {self.model}"


def _build_settings() -> Settings:
    # 测试模式：跳过真实 key 校验，返回占位 settings
    test_mode = os.environ.get("TIQU_TEST_MODE", "").lower() in ("1", "true", "yes")

    requested = _env("LLM_PROVIDER", "auto").lower() or "auto"
    api_key = _env("LLM_API_KEY")
    base_url = _env("LLM_BASE_URL")
    model = _env("LLM_MODEL")

    provider = requested
    if requested == "auto":
        provider = "deepseek" if "deepseek" in base_url.lower() else "openai"

    if provider not in PROVIDER_DEFAULTS:
        raise ValueError(
            f"LLM_PROVIDER={requested} 不支持；可选值：auto / openai / deepseek / ollama"
        )

    _default_base, default_model, needs_key = PROVIDER_DEFAULTS[provider]
    base_url = base_url or _default_base
    model = model or default_model

    if needs_key and not api_key and not test_mode:
        raise ValueError(
            f"LLM_PROVIDER={provider} 需要 LLM_API_KEY。"
            f"复制 .env.example 为 .env 后填入，或切换到 ollama（本地模型）。"
        )

    data_dir = Path(_env("DATA_DIR", str(ROOT / "data")))
    output_dir = Path(_env("OUTPUT_DIR", str(ROOT / "outputs")))
    db_path = Path(_env("DB_PATH", str(data_dir / "jobs.db")))

    ocr_backend = _env("OCR_BACKEND", "vision").lower() or "vision"
    if ocr_backend not in ("vision", "rapidocr"):
        raise ValueError(f"OCR_BACKEND={ocr_backend} 不支持；可选值：vision / rapidocr")
    vision_model = _env("VISION_MODEL") or model
    vision_fallback_local = _env("VISION_FALLBACK_LOCAL", "1").lower() in ("1", "true", "yes")

    return Settings(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        concurrency=max(1, min(16, _env_int("BATCH_CONCURRENCY", 4))),
        qps=max(0.1, _env_float("LLM_QPS", 3.0)),
        max_retries=max(0, _env_int("LLM_MAX_RETRIES", 3)),
        timeout=max(5.0, _env_float("LLM_TIMEOUT", 60.0)),
        confidence_threshold=min(0.99, max(0.3, _env_float("CONFIDENCE_THRESHOLD", 0.75))),
        data_dir=data_dir,
        output_dir=output_dir,
        db_path=db_path,
        ocr_backend=ocr_backend,
        vision_model=vision_model,
        vision_concurrency=max(1, min(8, _env_int("VISION_CONCURRENCY", 4))),
        vision_max_side=max(800, min(3000, _env_int("VISION_MAX_SIDE", 1600))),
        vision_fallback_local=vision_fallback_local,
    )


settings = _build_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.output_dir.mkdir(parents=True, exist_ok=True)
