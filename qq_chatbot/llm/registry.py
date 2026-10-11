"""由配置选择 LLM 后端。"""

from __future__ import annotations

from ..config import LLMConfig
from .base import LLMProvider
from .mock import MockLLM
from .ollama import OllamaLLM
from .openai import OpenAILLM

_REGISTRY = {
    "mock": MockLLM,
    "ollama": OllamaLLM,
    "openai": OpenAILLM,
}


def make_llm(cfg: LLMConfig) -> LLMProvider:
    cls = _REGISTRY.get(cfg.provider)
    if cls is None:
        raise RuntimeError(
            f"未知 LLM provider: {cfg.provider!r}（可选: {', '.join(_REGISTRY)}）"
        )
    return cls(model=cfg.model, base_url=cfg.base_url, api_key=cfg.api_key)
