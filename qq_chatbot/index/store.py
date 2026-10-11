"""内存向量 + BM25 倒排索引，及 JSON 持久化。

纯 stdlib 实现：向量用余弦相似度（Python 列表），关键词用 BM25 倒排索引。
中等规模语料（数万轮）足够；超大规模（百万轮）时建议换成 sqlite-vec / faiss，
本模块保留接口不变即可替换。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable

from ..schema import Speaker, Turn
from .embeddings import tokenize


class MemoryStore:
    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.vectors: list[list[float]] = []
        self.inv_index: dict[str, dict[int, int]] = {}  # term -> {idx: tf}
        self.doc_len: list[int] = []
        self.avg_len: float = 0.0
        self.idf: dict[str, float] = {}
        self._finalized = False

    def add(self, turn: Turn, vector: list[float]) -> None:
        idx = len(self.turns)
        self.turns.append(turn)
        self.vectors.append(vector)
        toks = tokenize(turn.text)
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            self.inv_index.setdefault(t, {})[idx] = c
        self.doc_len.append(len(toks))
        self._finalized = False

    def finalize(self) -> None:
        if self._finalized:
            return
        n = len(self.turns)
        self.avg_len = (sum(self.doc_len) / n) if n else 0.0
        for term, postings in self.inv_index.items():
            df = len(postings)
            self.idf[term] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        self._finalized = True

    # ---- 检索 ------------------------------------------------------------

    def search_vector(self, qvec: list[float], top_k: int) -> list[tuple[int, float]]:
        scored = [(i, self._cosine(qvec, self.vectors[i])) for i in range(len(self.turns))]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def search_keyword(self, query: str, top_k: int) -> list[tuple[int, float]]:
        self.finalize()
        k1, b = 1.2, 0.75
        qterms = {t for t in tokenize(query) if t in self.idf}
        scores = [0.0] * len(self.turns)
        for t in qterms:
            idf = self.idf[t]
            for idx, tf in self.inv_index[t].items():
                num = tf * (k1 + 1.0)
                den = tf + k1 * (1.0 - b + b * (self.doc_len[idx] / (self.avg_len or 1.0)))
                scores[idx] += idf * num / den
        ranked = sorted(((i, s) for i, s in enumerate(scores) if s > 0), key=lambda x: -x[1])
        return ranked[:top_k]

    def search_hybrid(
        self,
        qvec: list[float],
        query: str,
        top_k: int,
        vector_weight: float = 0.7,
    ) -> list[tuple[int, float, float, float]]:
        """返回 ``[(idx, hybrid, v_norm, k_norm)]``，按 hybrid 降序。"""
        self.finalize()
        n = len(self.turns)
        cos = [self._cosine(qvec, self.vectors[i]) for i in range(n)]

        k1, b = 1.2, 0.75
        kws = [0.0] * n
        for t in {t for t in tokenize(query) if t in self.idf}:
            idf = self.idf[t]
            for idx, tf in self.inv_index[t].items():
                num = tf * (k1 + 1.0)
                den = tf + k1 * (1.0 - b + b * (self.doc_len[idx] / (self.avg_len or 1.0)))
                kws[idx] += idf * num / den
        max_kw = max(kws, default=0.0) or 1.0
        max_cos = max(cos, default=0.0)

        scored = []
        for i in range(n):
            # 向量分数相对最大余弦归一化（负余弦视为 0），避免无关联文档拿到恒定底分
            vn = (cos[i] / max_cos) if max_cos > 0 else 0.0
            vn = 0.0 if vn < 0 else vn
            kn = kws[i] / max_kw
            h = vector_weight * vn + (1.0 - vector_weight) * kn
            scored.append((i, h, vn, kn))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)


# ---------------------------------------------------------------------------
# 构建与持久化
# ---------------------------------------------------------------------------


def build_store(turns: Iterable[Turn], embed_provider, *, progress=None) -> MemoryStore:
    store = MemoryStore()
    turns = list(turns)
    texts = [t.text for t in turns]
    vectors = embed_provider.embed(texts) if texts else []
    if progress:
        progress(f"向量化 {len(turns)} 条轮次（provider={getattr(embed_provider, 'name', '?')}）")
    for turn, vec in zip(turns, vectors):
        store.add(turn, vec)
    store.finalize()
    return store


def _turn_to_dict(t: Turn) -> dict:
    return {
        "id": t.id,
        "speaker_key": t.speaker.key,
        "conversation_key": t.conversation_key,
        "kind": t.kind,
        "time": t.time,
        "time_str": t.time_str,
        "text": t.text,
        "msg_ids": t.msg_ids,
    }


def _turn_from_dict(d: dict, speakers: dict[str, Speaker]) -> Turn:
    key = d["speaker_key"]
    sp = speakers.setdefault(key, Speaker(key=key, name=key))
    return Turn(
        id=d["id"],
        speaker=sp,
        conversation_key=d["conversation_key"],
        kind=d["kind"],
        time=d.get("time") or 0,
        time_str=d.get("time_str") or "",
        text=d.get("text") or "",
        msg_ids=d.get("msg_ids") or [],
    )


def save_store(
    store: MemoryStore,
    out_dir: Path,
    meta: dict,
    *,
    embedding_provider: str,
    dim: int,
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    speakers: dict[str, dict] = {}
    for t in store.turns:
        sp = t.speaker
        speakers.setdefault(sp.key, {"name": sp.name, "is_self": sp.is_self})

    corpus = {
        "meta": meta,
        "speakers": speakers,
        "turns": [_turn_to_dict(t) for t in store.turns],
    }
    (out_dir / "corpus.json").write_text(
        json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "embedding_provider": embedding_provider,
                "dim": dim,
                "turn_count": len(store.turns),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # hash/mock 向量可廉价重算，不落盘；真实向量持久化避免重复调用
    if embedding_provider not in ("hash", "mock"):
        emb = {t.id: store.vectors[i] for i, t in enumerate(store.turns)}
        (out_dir / "embeddings.json").write_text(json.dumps(emb), encoding="utf-8")


def load_store(out_dir: Path, embed_provider) -> tuple[MemoryStore, dict, dict[str, Speaker]]:
    out_dir = Path(out_dir)
    corpus_path = out_dir / "corpus.json"
    if not corpus_path.exists():
        raise FileNotFoundError(f"未找到索引：{corpus_path}，请先运行 `build`")

    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    speakers: dict[str, Speaker] = {}
    for key, info in corpus.get("speakers", {}).items():
        speakers[key] = Speaker(key=key, name=info.get("name", key), is_self=info.get("is_self", False))
    turns = [_turn_from_dict(d, speakers) for d in corpus.get("turns", [])]

    store = MemoryStore()
    emb_path = out_dir / "embeddings.json"
    if emb_path.exists():
        emb = json.loads(emb_path.read_text(encoding="utf-8"))
        for turn in turns:
            store.add(turn, emb.get(turn.id, [0.0] * 1))
    else:
        vectors = embed_provider.embed([t.text for t in turns]) if turns else []
        for turn, vec in zip(turns, vectors):
            store.add(turn, vec)
    store.finalize()
    return store, corpus.get("meta", {}), speakers
