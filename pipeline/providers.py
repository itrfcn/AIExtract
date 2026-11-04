"""LLM Provider：OpenAI 兼容接口（OpenAI / DeepSeek / Ollama）。

设计要点：
- 统一 chat_json(messages, json_schema=None) -> dict
- 内置限速 + 指数退避重试；不支持 json_schema 的接口自动降级为 json_object / 纯 JSON
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from functools import lru_cache

from core.config import Settings, settings
from .schema import FieldSpec


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class RateLimiter:
    """简单的全局限速：保证两次请求的最小间隔。"""

    def __init__(self, qps: float):
        self.min_interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if not self.min_interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.min_interval
        if wait > 0:
            time.sleep(wait)


def _parse_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ProviderError(f"模型未返回 JSON：{text[:200]}", retryable=False)
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"JSON 解析失败：{exc}", retryable=True) from exc
    if not isinstance(data, dict):
        raise ProviderError("模型返回的不是 JSON 对象", retryable=False)
    return data


class BaseProvider:
    name = "base"

    def chat_json(self, messages: list[dict], json_schema: dict | None = None, max_tokens: int = 1600) -> dict:
        raise NotImplementedError

    @property
    def signature(self) -> str:
        return self.name


class OpenAICompatProvider(BaseProvider):
    def __init__(self, cfg: Settings):
        from openai import OpenAI

        self.name = cfg.provider
        self._cfg = cfg
        self._model = cfg.model
        self._client = OpenAI(
            api_key=cfg.api_key or "not-needed",
            base_url=cfg.base_url or None,
            timeout=cfg.timeout,
            max_retries=0,
        )
        self._limiter = RateLimiter(cfg.qps)
        self._json_schema_supported = True
        self._last_error = ""

    @property
    def signature(self) -> str:
        return f"{self.name}:{self._model}:{self._cfg.base_url}"

    def chat_json(self, messages: list[dict], json_schema: dict | None = None, max_tokens: int = 1600) -> dict:
        attempts = self._cfg.max_retries + 1
        for attempt in range(attempts):
            self._limiter.acquire()
            try:
                kwargs: dict = {
                    "model": self._model,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": max_tokens,
                }
                if json_schema and self._json_schema_supported:
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": "extraction", "strict": True, "schema": json_schema},
                    }
                response = self._client.chat.completions.create(**kwargs)
                content = (response.choices[0].message.content or "") if response.choices else ""
                return _parse_json(content)
            except Exception as exc:  # noqa: BLE001 - 统一包装成 ProviderError
                message = str(exc)
                self._last_error = message
                # 某些兼容接口不支持 json_schema，立即降级后重试一次
                if json_schema and self._json_schema_supported and _schema_unsupported(message):
                    self._json_schema_supported = False
                    if attempt < attempts - 1:
                        time.sleep(min(5.0, 2 ** attempt) + random.uniform(0, 0.4))
                    continue
                if attempt >= attempts - 1 or not _retryable(message):
                    raise ProviderError(message, retryable=_retryable(message)) from exc
                wait = min(30.0, 2 ** attempt) + random.uniform(0, 0.4)
                print(f"  [retryable] {type(exc).__name__}: {message[:100]}, attempt {attempt + 1}/{attempts}, sleep {wait:.1f}s")
                time.sleep(wait)
        raise ProviderError(self._last_error or "未知错误", retryable=True)


def _schema_unsupported(message: str) -> bool:
    lowered = message.lower()
    return any(k in lowered for k in ("response_format", "json_schema", "strict", "unsupported", "400"))


def _retryable(message: str) -> bool:
    lowered = message.lower()
    retry_words = (
        "429", "rate limit", "ratelimit", "rate_limit",
        "timeout", "timed out", "connection", "connection error",
        "502", "503", "504", "500",
        "temporarily", "temporary", "overloaded",
        "internal server", "service unavailable",
        "reset by peer", "broken pipe", "eof",
    )
    return any(w in lowered for w in retry_words)



@lru_cache(maxsize=8)
def _build_cached(provider: str, base_url: str, api_key: str, model: str,
                  timeout: float, retries: int, qps: float) -> BaseProvider:
    cfg = Settings(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        max_retries=retries,
        qps=qps,
    )
    return OpenAICompatProvider(cfg)


def get_provider(cfg: Settings = settings) -> BaseProvider:
    return _build_cached(
        cfg.provider, cfg.base_url, cfg.api_key, cfg.model,
        cfg.timeout, cfg.max_retries, cfg.qps,
    )
