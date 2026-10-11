"""内存向量 + BM25 倒排索引，及 JSON 持久化。

纯 stdlib 实现：向量用余弦相似度（Python 列表），关键词用 BM25 倒排索引。
中等规模语料（数万轮）足够；超大规模（百万轮）时建议换成 sqlite-vec / faiss，
本模块保留接口不变即可替换。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Iterable

from ..schema import Speaker, Turn
from .embeddings import HashEmbedding, tokenize
from .vectors import identity, validate_space, validate_vectors


class MemoryStore:
    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.vectors: list[list[float]] = []
        self.inv_index: dict[str, dict[int, int]] = {}  # term -> {idx: tf}
        self.doc_len: list[int] = []
        self.avg_len: float = 0.0
        self.idf: dict[str, float] = {}
        self._finalized = False
        self.embedding: dict | None = None
        self.embed_provider = None
        self.fallback_vectors: list[list[float]] | None = None
        self.fallback_embedding: dict | None = None

    def add(self, turn: Turn, vector: list[float]) -> None:
        validate_vectors([vector], 1, len(self.vectors[0]) if self.vectors else None)
        idx = len(self.turns)
        self.turns.append(turn)
        self.vectors.append(vector)
        if self.fallback_vectors is not None and len(self.fallback_vectors) < len(self.turns):
            self.fallback_vectors = None
            self.fallback_embedding = None
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
        if len(a) != len(b):
            raise ValueError("查询和文档向量维度不一致")
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
    active = embed_provider
    fallback = bool(getattr(embed_provider, "fallback_to_hash", False))
    try:
        vectors = active.embed(texts) if texts else []
    except Exception:
        if not fallback or identity(active)["provider"] == "hash":
            raise
        warnings.warn("语义 embedding 不可用；整套索引回退为 hash 字面匹配，不具备语义能力", RuntimeWarning)
        active = HashEmbedding(getattr(embed_provider, "hash_dim", 256))
        vectors = active.embed(texts)
    dim = validate_vectors(vectors, len(turns))
    store.embedding = {**identity(active), "dim": dim or getattr(active, "dim", None)}
    store.embed_provider = active
    if fallback and identity(active)["provider"] != "hash":
        hp = HashEmbedding(getattr(embed_provider, "hash_dim", 256))
        store.fallback_vectors = hp.embed(texts)
        store.fallback_embedding = {**identity(hp), "dim": hp.dim}
    if progress:
        progress(f"向量化 {len(turns)} 条轮次（provider={identity(active)['provider']}）")
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
    embedding_model: str = "",
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    actual_dim = validate_vectors(store.vectors, len(store.turns))
    space = store.embedding or {
        "provider": "hash" if embedding_provider == "mock" else embedding_provider,
        "model": "fnv1a-ngram-v1" if embedding_provider in ("hash", "mock") else embedding_model,
        "dim": actual_dim or dim,
    }
    if space["dim"] is None:
        space = {**space, "dim": dim}
    validate_vectors(store.vectors, len(store.turns), space["dim"])
    if store.fallback_vectors is not None:
        validate_vectors(store.fallback_vectors, len(store.turns), store.fallback_embedding["dim"])

    speakers: dict[str, dict] = {}
    for t in store.turns:
        sp = t.speaker
        speakers.setdefault(sp.key, {"name": sp.name, "is_self": sp.is_self})
    corpus = {
        "meta": meta,
        "speakers": speakers,
        "turns": [_turn_to_dict(t) for t in store.turns],
    }
    vectors = {
        "format_version": 2,
        "turn_ids": [t.id for t in store.turns],
        "vectors": store.vectors,
        "fallback_vectors": store.fallback_vectors,
    }
    manifest = {
        "format_version": 2,
        "embedding_provider": space["provider"],
        "embedding_model": space["model"],
        "dim": space["dim"],
        "turn_count": len(store.turns),
        "fallback_embedding": store.fallback_embedding,
    }
    generation = uuid.uuid4().hex
    payloads = (("corpus.json", corpus), ("embeddings.json", vectors), ("manifest.json", manifest))
    with tempfile.TemporaryDirectory(prefix=".index-save-", dir=out_dir) as staging:
        stage = Path(staging)
        # Fully serialize and flush every file before touching the active index.
        # The manifest is published last and binds the three files to one build.
        for name, payload in payloads:
            payload["generation_id"] = generation
            with (stage / name).open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            target = out_dir / name
            if target.exists():
                shutil.copy2(target, stage / (name + ".previous"))
        replaced = []
        try:
            for name, _ in payloads:
                os.replace(stage / name, out_dir / name)
                replaced.append(name)
        except BaseException:
            for name in reversed(replaced):
                target = out_dir / name
                previous = stage / (name + ".previous")
                if previous.exists():
                    os.replace(previous, target)
                else:
                    target.unlink()
            raise


def load_store(out_dir: Path, embed_provider) -> tuple[MemoryStore, dict, dict[str, Speaker]]:
    out_dir = Path(out_dir)
    corpus_path = out_dir / "corpus.json"
    if not corpus_path.exists():
        raise FileNotFoundError(f"未找到索引：{corpus_path}，请先运行 `build`")
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("索引缺少 embedding 元信息，请重新 build")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("format_version", 1)
    if version not in (1, 2):
        raise ValueError("不支持的索引格式，请重新 build")
    name = manifest.get("embedding_provider")
    if name == "mock":
        name = "hash"
    model = manifest.get("embedding_model")
    if model is None:
        if version == 2 or name != "hash":
            raise ValueError("索引缺少 model 元信息，请重新 build")
        model = "fnv1a-ngram-v1"
    dim = manifest.get("dim")
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise ValueError("索引 embedding dim 无效，请重新 build")
    space = {"provider": name, "model": model, "dim": dim}
    active = embed_provider
    if name == "hash" and identity(active)["provider"] != "hash" and getattr(active, "fallback_to_hash", False):
        active = HashEmbedding(dim)
        warnings.warn("加载的是 hash 字面匹配索引，不具备语义能力", RuntimeWarning)
    validate_space(space, active)

    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    if corpus.get("generation_id") != manifest.get("generation_id"):
        raise ValueError("索引 generation 不一致，保存可能中断，请重新 build")
    speakers: dict[str, Speaker] = {}
    for key, info in corpus.get("speakers", {}).items():
        speakers[key] = Speaker(key=key, name=info.get("name", key), is_self=info.get("is_self", False))
    turns = [_turn_from_dict(d, speakers) for d in corpus.get("turns", [])]
    if manifest.get("turn_count") != len(turns):
        raise ValueError("索引 turn_count 与语料不一致，请重新 build")
    store = MemoryStore()
    store.embedding = space
    store.embed_provider = active
    emb_path = out_dir / "embeddings.json"
    if version == 2:
        if not emb_path.exists():
            raise ValueError("索引缺少持久化向量，请重新 build")
        emb = json.loads(emb_path.read_text(encoding="utf-8"))
        if emb.get("generation_id") != manifest.get("generation_id"):
            raise ValueError("索引 generation 不一致，保存可能中断，请重新 build")
        if emb.get("format_version") != 2 or emb.get("turn_ids") != [t.id for t in turns]:
            raise ValueError("持久化向量与语料轮次不一致，请重新 build")
        vectors = emb.get("vectors")
        fallback_vectors = emb.get("fallback_vectors")
        fallback_space = manifest.get("fallback_embedding")
        if fallback_space is not None or fallback_vectors is not None:
            if not isinstance(fallback_space, dict):
                raise ValueError("hash 回退元信息缺失")
            fallback_dim = fallback_space.get("dim")
            if isinstance(fallback_dim, bool) or not isinstance(fallback_dim, int) or fallback_dim <= 0:
                raise ValueError("hash 回退 dim 无效")
            validate_space(fallback_space, HashEmbedding(fallback_dim))
            validate_vectors(fallback_vectors, len(turns), fallback_dim)
            store.fallback_vectors = fallback_vectors
            store.fallback_embedding = fallback_space
    elif name == "hash":
        # Legacy hash vectors were not persisted. Only this explicit local migration
        # may rebuild them; never call a remote provider during load.
        vectors = HashEmbedding(dim).embed([t.text for t in turns])
    else:
        if not emb_path.exists():
            raise ValueError("索引缺少持久化向量，请重新 build")
        emb = json.loads(emb_path.read_text(encoding="utf-8"))
        if len({t.id for t in turns}) != len(turns) or set(emb) != {t.id for t in turns}:
            raise ValueError("持久化向量与语料轮次不一致，请重新 build")
        vectors = [emb[t.id] for t in turns]
    validate_vectors(vectors, len(turns), dim)
    for turn, vec in zip(turns, vectors):
        store.add(turn, vec)
    store.finalize()
    return store, corpus.get("meta", {}), speakers
