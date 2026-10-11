"""可插拔向量化后端。

- ``hash``：纯 stdlib 的确定性哈希向量（字符 n-gram + 词袋），离线可用，适合自测/演示。
- ``ollama``：调用本机 ``/api/embed``。
- ``openai``：调用 OpenAI 兼容 ``/embeddings``。
"""

from __future__ import annotations

import json
import math
import re
import urllib.request

from ..config import EmbeddingConfig
from .base import EmbeddingProvider

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    """简单分词：拉丁词 + 中日韩单字 + 中日韩二元组。"""
    toks: list[str] = []
    for w in _WORD.findall(text):
        toks.append(w.lower())
    cjk = _CJK.findall(text)
    toks.extend(cjk)
    toks.extend(a + b for a, b in zip(cjk, cjk[1:]))
    return toks


def _fnv1a(s: str) -> int:
    h = 0x811C9DC5
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def hash_embed(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    if not dim:
        return vec
    for tok in tokenize(text):
        h = _fnv1a(tok)
        idx = h % dim
        sign = 1.0 if (h >> 32) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class HashEmbedding(EmbeddingProvider):
    name = "hash"

    def __init__(self, dim: int = 256, **_) -> None:
        self.dim = int(dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_embed(t, self.dim) for t in texts]


class OllamaEmbedding(EmbeddingProvider):
    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://localhost:11434", **_) -> None:
        self.model = model or "nomic-embed-text"
        self.base_url = (base_url or "http://localhost:11434").rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            data = _post(f"{self.base_url}/api/embed", {"model": self.model, "input": t})
            out.append(data["embeddings"][0])
        return out


class OpenAIEmbedding(EmbeddingProvider):
    name = "openai"

    def __init__(self, model: str, base_url: str = "", api_key: str = "", **_) -> None:
        self.model = model or "text-embedding-3-small"
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        import os

        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError("openai 嵌入后端需要 api_key（配置 embedding.api_key 或环境变量 OPENAI_API_KEY）")
        data = _post(
            f"{self.base_url}/embeddings",
            {"model": self.model, "input": texts},
            self.api_key,
        )
        return [item["embedding"] for item in data["data"]]


_REGISTRY = {
    "hash": HashEmbedding,
    "mock": HashEmbedding,
    "ollama": OllamaEmbedding,
    "openai": OpenAIEmbedding,
}


def make_embedding(cfg: EmbeddingConfig) -> EmbeddingProvider:
    cls = _REGISTRY.get(cfg.provider)
    if cls is None:
        raise RuntimeError(
            f"未知 embedding provider: {cfg.provider!r}（可选: {', '.join(_REGISTRY)}）"
        )
    return cls(dim=cfg.dim, model=cfg.model, base_url=cfg.base_url, api_key=cfg.api_key)


def _post(url: str, payload: dict, api_key: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))
