"""OpenAI-compatible clients for the RAG provider cards.

Same shape as msc.services.llm — endpoint normalisation, thin httpx calls, and
every failure phrased for a human reading the card, never a stack trace.

Dialect notes:
- embeddings:  POST {base}/v1/embeddings  {"model", "input": [texts]}
- rerank:      POST {base}/v1/rerank      {"model", "query", "documents", "top_n"}
               (llama.cpp and Cohere agree on the response shape used here)
- whisper:     POST {base}/v1/audio/transcriptions (multipart)

Older Ollama builds answer /api/embed instead of /v1/embeddings; the response
parser accepts both shapes, so the base URL is all a user has to get right.
"""

import time
from typing import List, Sequence

import httpx

from msc.services.llm import LLMError, _error_detail, _headers


def _base(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("provider 卡片还没有填 API 地址")
    if "://" not in base:
        base = "http://" + base
    return base


def embeddings_endpoint(base_url: str) -> str:
    base = _base(base_url)
    if base.endswith("/embeddings"):
        return base
    return base + "/v1/embeddings"


def rerank_endpoint(base_url: str) -> str:
    base = _base(base_url)
    if base.endswith("/rerank"):
        return base
    return base + "/v1/rerank"


def transcriptions_endpoint(base_url: str) -> str:
    base = _base(base_url)
    if base.endswith("/transcriptions"):
        return base
    return base + "/v1/audio/transcriptions"


def _timeout(cfg) -> float:
    return float(cfg.timeout_seconds or 120)


def embed(cfg, texts: Sequence[str]) -> List[List[float]]:
    """Embed a batch in one call. Vectors come back in request order; a server
    that shuffles or drops items is reported, not silently accepted."""
    payload = {"model": (cfg.model or "").strip() or "default", "input": list(texts)}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=_timeout(cfg)) as client:
            response = client.post(embeddings_endpoint(cfg.base_url),
                                   json=payload, headers=_headers(cfg.api_key))
    except httpx.TimeoutException as exc:
        raise LLMError(f"嵌入超时（{_timeout(cfg):.0f}s）：模型没响应，可调大超时或换更小的嵌入模型") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"连接失败: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        raise LLMError(f"HTTP {response.status_code}: {_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"嵌入端点返回的不是 JSON: {response.text[:200]}") from exc

    # OpenAI/llama.cpp: {"data": [{"embedding": [...], "index": 0}, ...]}
    # Ollama /api/embed:   {"embeddings": [[...], ...]}
    entries = None
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            entries = data["data"]
        elif isinstance(data.get("embeddings"), list):
            entries = data["embeddings"]
    if entries is None:
        raise LLMError(f"嵌入响应缺 data/embeddings: {str(data)[:200]}")

    vectors: List[List[float]] = []
    by_index = {}
    plain = []
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("embedding"), list):
            by_index[int(item.get("index", len(by_index)))] = item["embedding"]
        elif isinstance(item, list):
            plain.append(item)
    if by_index:
        vectors = [by_index[i] for i in sorted(by_index)]
    else:
        vectors = plain

    if len(vectors) != len(texts):
        raise LLMError(f"嵌入返回 {len(vectors)} 条向量，请求了 {len(texts)} 条")
    for vec in vectors:
        if not vec or not all(isinstance(x, (int, float)) for x in vec):
            raise LLMError("嵌入向量格式不对：应该是数字列表")
    embed.latency_ms = latency_ms  # last-call latency, shown by the test endpoint
    return vectors


def rerank(cfg, query: str, documents: Sequence[str], top_n: int = 10):
    """Rerank documents. An unreachable or missing reranker is the CALLER's
    problem to degrade on, so this raises LLMError rather than faking scores."""
    payload = {"model": (cfg.model or "").strip() or "default", "query": query,
               "documents": list(documents), "top_n": max(1, int(top_n))}
    try:
        with httpx.Client(timeout=_timeout(cfg)) as client:
            response = client.post(rerank_endpoint(cfg.base_url),
                                   json=payload, headers=_headers(cfg.api_key))
    except httpx.TimeoutException as exc:
        raise LLMError(f"重排超时（{_timeout(cfg):.0f}s）") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"连接失败: {exc}") from exc

    if response.status_code >= 400:
        raise LLMError(f"HTTP {response.status_code}: {_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"重排端点返回的不是 JSON: {response.text[:200]}") from exc

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise LLMError(f"重排响应缺 results: {str(data)[:200]}")

    scored = []
    for item in results:
        if not isinstance(item, dict) or "index" not in item:
            continue
        scored.append({
            "index": int(item["index"]),
            "score": float(item.get("relevance_score") or item.get("score") or 0.0),
        })
    return scored


def transcribe(cfg, filename: str, content: bytes) -> dict:
    """One multipart transcription. Returns {text, latency_ms}."""
    if not content:
        raise LLMError("上传的音频是空的")
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=_timeout(cfg)) as client:
            response = client.post(
                transcriptions_endpoint(cfg.base_url),
                headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else None,
                data={"model": (cfg.model or "").strip() or "whisper-1"},
                files={"file": (filename, content, "application/octet-stream")},
            )
    except httpx.TimeoutException as exc:
        raise LLMError(f"转写超时（{_timeout(cfg):.0f}s）：换更短的音频或调大超时") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"连接失败: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        raise LLMError(f"HTTP {response.status_code}: {_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"转写端点返回的不是 JSON: {response.text[:200]}") from exc

    text = ""
    if isinstance(data, dict):
        text = str(data.get("text") or "").strip()
    if not text:
        raise LLMError(f"模型没有返回转写文本: {str(data)[:200]}")
    return {"text": text, "latency_ms": latency_ms}
