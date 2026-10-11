"""配置加载：默认值 + 文件（YAML/JSON）+ 环境变量 + CLI 覆盖。

设计目标：零硬依赖即可跑通（默认 hash 向量 + mock LLM），
需要真实模型时再把 provider 切成 ollama / openai。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass
class LLMConfig:
    provider: str = "mock"        # mock | ollama | openai
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.4
    max_tokens: int = 1024


@dataclass
class EmbeddingConfig:
    provider: str = "hash"        # hash | ollama | openai
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    dim: int = 256


@dataclass
class StoreConfig:
    type: str = "memory"          # memory（JSON 持久化到磁盘）
    dir: Path = field(default_factory=lambda: Path(".chatbot"))


@dataclass
class RetrievalConfig:
    top_k: int = 8
    vector_weight: float = 0.7
    min_score: float = 0.35


@dataclass
class SessionConfig:
    max_turns: int = 12


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    session: SessionConfig = field(default_factory=SessionConfig)

    def to_dict(self) -> dict:
        return {
            "llm": _dc_dict(self.llm),
            "embedding": _dc_dict(self.embedding),
            "store": {"type": self.store.type, "dir": str(self.store.dir)},
            "retrieval": _dc_dict(self.retrieval),
            "session": _dc_dict(self.session),
        }


def _dc_dict(obj) -> dict:
    out = {}
    for k, v in obj.__dict__.items():
        if isinstance(v, Path):
            v = str(v)
        out[k] = v
    return out


def _load_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"读取 {path} 需要 PyYAML：pip install pyyaml（或改用 .json 配置）"
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


_ENV_MAP = {
    "QQ_CHATBOT_LLM_PROVIDER": ("llm", "provider"),
    "QQ_CHATBOT_LLM_MODEL": ("llm", "model"),
    "QQ_CHATBOT_LLM_BASE_URL": ("llm", "base_url"),
    "QQ_CHATBOT_API_KEY": ("llm", "api_key"),
    "QQ_CHATBOT_LLM_TEMPERATURE": ("llm", "temperature"),
    "QQ_CHATBOT_LLM_MAX_TOKENS": ("llm", "max_tokens"),
    "QQ_CHATBOT_EMBEDDING_PROVIDER": ("embedding", "provider"),
    "QQ_CHATBOT_EMBEDDING_MODEL": ("embedding", "model"),
    "QQ_CHATBOT_EMBEDDING_BASE_URL": ("embedding", "base_url"),
    "QQ_CHATBOT_EMBEDDING_DIM": ("embedding", "dim"),
}


def _apply_section(cfg: Any, data: dict | None, *, env_prefix: str, overrides: dict | None) -> None:
    """把字典合并进 dataclass 字段（只覆盖同名字段）。"""
    if data:
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    if overrides:
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    """加载配置。优先级：默认 < 文件 < 环境变量 < overrides（CLI）。"""
    cfg = Config()
    overrides = overrides or {}

    if path:
        data = _load_file(Path(path))
        _apply_section(cfg.llm, data.get("llm"), env_prefix="", overrides=overrides.get("llm"))
        _apply_section(cfg.embedding, data.get("embedding"), env_prefix="", overrides=overrides.get("embedding"))
        _apply_section(cfg.retrieval, data.get("retrieval"), env_prefix="", overrides=overrides.get("retrieval"))
        _apply_section(cfg.session, data.get("session"), env_prefix="", overrides=overrides.get("session"))
        store = data.get("store") or {}
        if store.get("dir"):
            cfg.store.dir = Path(store["dir"])
        if store.get("type"):
            cfg.store.type = store["type"]

    # 环境变量
    for env, (section, attr) in _ENV_MAP.items():
        val = os.environ.get(env)
        if val is None:
            continue
        target = getattr(cfg, section)
        if attr in ("temperature", "max_tokens", "dim"):
            val = int(val) if attr in ("max_tokens", "dim") else float(val)  # type: ignore[assignment]
        setattr(target, attr, val)

    # CLI 覆盖（顶层 keys: llm/embedding/retrieval/session/store）
    for section in ("llm", "embedding", "retrieval", "session"):
        if section in overrides and isinstance(overrides[section], dict):
            _apply_section(getattr(cfg, section), overrides[section], env_prefix="", overrides=None)

    _coerce_numbers(cfg)
    return cfg


def _coerce_numbers(cfg: Config) -> None:
    for obj, attrs in (
        (cfg.llm, ("temperature", "max_tokens")),
        (cfg.embedding, ("dim",)),
        (cfg.retrieval, ("top_k", "vector_weight", "min_score")),
        (cfg.session, ("max_turns",)),
    ):
        for a in attrs:
            try:
                setattr(obj, a, _num(getattr(obj, a)))
            except (TypeError, ValueError):
                pass


def _num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return float(v) if ("." in v or "e" in v.lower()) else int(v)
    return v


def save_config(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
