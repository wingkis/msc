"""OpenAI-compatible chat client for the model card.

The card points at whatever the user runs locally (LM Studio, llama.cpp,
ollama's OpenAI shim, vLLM), so the only contract is
``POST {base}/v1/chat/completions``.

Reasoning models get explicit handling: gpt-oss and the Qwen3 family spend the
token budget on ``reasoning`` before writing ``content``, so a small
``max_tokens`` looks like "the model answered nothing". That case has to come
back as a readable message the UI can show, not a stack trace.
"""

import json
import time
from typing import List, Optional

import httpx


class LLMError(RuntimeError):
    """Any model-side failure, already phrased for a human reading the card."""


def chat_endpoint(base_url: str) -> str:
    """Accept a bare host, a host with a port, ``.../v1`` or a full endpoint."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("模型卡片还没有填 API 地址")
    if "://" not in base:
        base = "http://" + base
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def models_endpoint(base_url: str) -> str:
    return chat_endpoint(base_url).removesuffix("/chat/completions") + "/models"


def _headers(api_key: Optional[str]) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _error_detail(response: httpx.Response) -> str:
    """Pull the useful sentence out of an OpenAI-shaped error body."""
    try:
        data = response.json()
    except ValueError:
        return (response.text or "").strip()[:300]
    if isinstance(data, dict):
        inner = data.get("error")
        if isinstance(inner, dict):
            return str(inner.get("message") or inner)[:300]
        if isinstance(inner, str):
            return inner[:300]
        return str(data.get("detail") or data)[:300]
    return str(data)[:300]


def _client(timeout_seconds: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_seconds)


def _timeout(cfg) -> float:
    return float(cfg.timeout_seconds or 180)


def list_models(cfg) -> List[str]:
    """Model ids the endpoint offers, for the card's dropdown. Some servers do
    not implement /v1/models — that is reported as an LLMError, not a crash."""
    try:
        with _client(_timeout(cfg)) as client:
            response = client.get(models_endpoint(cfg.base_url), headers=_headers(cfg.api_key))
    except httpx.HTTPError as exc:
        raise LLMError(f"连接失败: {exc}") from exc
    if response.status_code >= 400:
        raise LLMError(f"HTTP {response.status_code}: {_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"/v1/models 返回的不是 JSON: {response.text[:200]}") from exc
    entries = data.get("data") if isinstance(data, dict) else data
    return [str(item.get("id")) for item in (entries or []) if isinstance(item, dict) and item.get("id")]


def chat(cfg, messages: List[dict], max_tokens: Optional[int] = None) -> dict:
    """One completion. Returns text plus the metadata the run log records."""
    if not (cfg.base_url or "").strip():
        raise LLMError("模型卡片还没有填 API 地址")

    payload = {
        "model": (cfg.model or "").strip() or "default",
        "messages": messages,
        "temperature": float(cfg.temperature or 0),
        "max_tokens": int(max_tokens or cfg.max_tokens or 1500),
    }
    extra = (cfg.extra_json or "").strip()
    if extra:
        try:
            overrides = json.loads(extra)
        except json.JSONDecodeError as exc:
            raise LLMError(f"额外请求参数不是合法 JSON: {exc}") from exc
        if not isinstance(overrides, dict):
            raise LLMError("额外请求参数必须是一个 JSON 对象，例如 {\"reasoning\": {\"effort\": \"low\"}}")
        payload.update(overrides)

    started = time.perf_counter()
    try:
        with _client(_timeout(cfg)) as client:
            response = client.post(chat_endpoint(cfg.base_url), json=payload,
                                   headers=_headers(cfg.api_key))
    except httpx.TimeoutException as exc:
        raise LLMError(f"超时（{_timeout(cfg):.0f}s）：模型没在时间内响应，可调大超时或换更小的模型") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"连接失败: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        raise LLMError(f"HTTP {response.status_code}: {_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"模型返回的不是 JSON: {response.text[:200]}") from exc

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise LLMError(f"响应里没有 choices: {str(data)[:200]}")
    message = choices[0].get("message") or {}
    finish_reason = choices[0].get("finish_reason") or ""
    text = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
    reasoning_only = False

    if not text and reasoning:
        if finish_reason == "length":
            raise LLMError(
                "模型的输出 token 全用在思考上，正文是空的。"
                "把「最大输出 tokens」调大，或在「额外请求参数」里关闭思考。")
        text, reasoning_only = reasoning, True
    if not text:
        raise LLMError(f"模型没有返回内容（finish_reason={finish_reason or 'unknown'}）")

    return {
        "text": text,
        "model": (data.get("model") or payload["model"]),
        "finish_reason": finish_reason,
        "usage": data.get("usage") or {},
        "latency_ms": latency_ms,
        "reasoning_only": reasoning_only,
    }
