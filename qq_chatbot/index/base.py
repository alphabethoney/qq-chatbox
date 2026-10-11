"""检索侧抽象：Embedding 与 Retriever。"""

from __future__ import annotations

from typing import Protocol

from ..schema import RetrievedTurn


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class Retriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 8,
        speaker: str | None = None,
        conversation: str | None = None,
        kind: str | None = None,
        time_range: tuple[int, int] | None = None,
    ) -> list[RetrievedTurn]:
        ...
