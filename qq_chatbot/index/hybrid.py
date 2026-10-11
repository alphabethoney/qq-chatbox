"""混合检索器：向量 + BM25 加权融合 + 元数据过滤。"""

from __future__ import annotations

import warnings

from ..config import RetrievalConfig
from ..schema import RetrievedTurn, speaker_qq
from .base import EmbeddingProvider, Retriever
from .store import MemoryStore
from .embeddings import HashEmbedding
from .vectors import identity, validate_space, validate_vectors


class HybridRetriever(Retriever):
    def __init__(
        self,
        store: MemoryStore,
        embed_provider: EmbeddingProvider,
        config: RetrievalConfig | None = None,
    ) -> None:
        self.store = store
        self.embed_provider = embed_provider
        self.allow_fallback = bool(getattr(embed_provider, "fallback_to_hash", False))
        if store.embedding is not None:
            if store.embedding["provider"] == "hash" and identity(embed_provider)["provider"] != "hash" and self.allow_fallback:
                self.embed_provider = HashEmbedding(store.embedding["dim"])
            validate_space(store.embedding, self.embed_provider)
        self._fallback_store = None
        self.config = config or RetrievalConfig()

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        speaker: str | None = None,
        person_qq: str | None = None,
        conversation: str | None = None,
        kind: str | None = None,
        time_range: tuple[int, int] | None = None,
    ) -> list[RetrievedTurn]:
        self.last_embedding = {}
        top_k = self.config.top_k if top_k is None else top_k
        if top_k <= 0 or not self.store.turns:
            return []
        search_store = self.store
        if self.store.embedding is not None:
            validate_space(self.store.embedding, self.embed_provider)
        try:
            vectors = self.embed_provider.embed([query])
        except Exception:
            if not self.allow_fallback or identity(self.embed_provider)["provider"] == "hash":
                raise
            warnings.warn("语义查询不可用；本次整套检索回退为 hash 字面匹配，不具备语义能力", RuntimeWarning)
            if self._fallback_store is None or len(self._fallback_store.turns) != len(self.store.turns):
                fallback_space = self.store.fallback_embedding
                hp = HashEmbedding(fallback_space["dim"] if fallback_space else getattr(self.embed_provider, "hash_dim", 256))
                fallback_vectors = self.store.fallback_vectors
                if fallback_vectors is None:
                    fallback_vectors = hp.embed([t.text for t in self.store.turns])
                if fallback_space is not None:
                    validate_space(fallback_space, hp)
                validate_vectors(fallback_vectors, len(self.store.turns), hp.dim)
                fallback_store = MemoryStore()
                for turn, vector in zip(self.store.turns, fallback_vectors):
                    fallback_store.add(turn, vector)
                fallback_store.embedding = {**identity(hp), "dim": hp.dim}
                self._fallback_store = fallback_store
            search_store = self._fallback_store
            vectors = HashEmbedding(search_store.embedding["dim"]).embed([query])
        self.last_embedding = dict(search_store.embedding or {})
        dim = search_store.embedding["dim"] if search_store.embedding else len(search_store.vectors[0])
        validate_vectors(vectors, 1, dim)
        qvec = vectors[0]
        # Rank every candidate before filtering so a narrow scope cannot lose hits
        # merely because unrelated conversations occupy the global top results.
        raw = search_store.search_hybrid(
            qvec, query, len(search_store.turns), self.config.vector_weight
        )
        out: list[RetrievedTurn] = []
        for idx, score, vs, ks in raw:
            turn = self.store.turns[idx]
            if speaker is not None and turn.speaker.key != speaker:
                continue
            if person_qq is not None and speaker_qq(turn.speaker.key) != person_qq:
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
