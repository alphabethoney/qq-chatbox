"""混合检索器：向量 + BM25 加权融合 + 元数据过滤。"""

from __future__ import annotations

from ..config import RetrievalConfig
from ..schema import RetrievedTurn
from .base import EmbeddingProvider, Retriever
from .store import MemoryStore


class HybridRetriever(Retriever):
    def __init__(
        self,
        store: MemoryStore,
        embed_provider: EmbeddingProvider,
        config: RetrievalConfig | None = None,
    ) -> None:
        self.store = store
        self.embed_provider = embed_provider
        self.config = config or RetrievalConfig()

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        speaker: str | None = None,
        conversation: str | None = None,
        kind: str | None = None,
        time_range: tuple[int, int] | None = None,
    ) -> list[RetrievedTurn]:
        top_k = top_k or self.config.top_k
        qvec = self.embed_provider.embed([query])[0]
        # 先取 2x，过滤后再截断，避免过滤后不足 top_k
        raw = self.store.search_hybrid(
            qvec, query, max(top_k * 2, top_k), self.config.vector_weight
        )
        out: list[RetrievedTurn] = []
        for idx, score, vs, ks in raw:
            turn = self.store.turns[idx]
            if speaker is not None and turn.speaker.key != speaker:
                continue
            if conversation is not None and turn.conversation_key != conversation:
                continue
            if kind is not None and turn.kind != kind:
                continue
            if time_range is not None and not (time_range[0] <= turn.time <= time_range[1]):
                continue
            out.append(RetrievedTurn(turn=turn, score=score, vector_score=vs, keyword_score=ks))
            if len(out) >= top_k:
                break
        return out
