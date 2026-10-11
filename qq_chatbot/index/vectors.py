"""Vector-space identity and strict provider-response validation."""
from __future__ import annotations

import math


def identity(provider) -> dict:
    name = getattr(provider, "name", "")
    if name == "mock":
        name = "hash"
    return {"provider": name, "model": getattr(provider, "model", "")}


def validate_vectors(vectors, count: int, dim: int | None = None) -> int | None:
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ValueError("embedding 向量数量与文档/查询数量不一致")
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("embedding 向量不能为空")
        if dim is None:
            dim = len(vector)
        if len(vector) != dim:
            raise ValueError("embedding 向量维度不一致，请重新 build")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
            raise ValueError("embedding 向量必须为有限数值")
    return dim


def validate_space(space: dict, provider) -> None:
    expected = identity(provider)
    if any(space.get(key) != value for key, value in expected.items()):
        raise ValueError("embedding provider/model 与索引不一致，请使用构建时配置或重新 build")
    configured_dim = getattr(provider, "dim", None)
    if configured_dim is not None and configured_dim != space["dim"]:
        raise ValueError("embedding dim 与索引不一致，请重新 build")
