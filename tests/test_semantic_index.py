"""Offline semantic index contract tests; synthetic vectors are not model benchmarks.

Run: python tests/test_semantic_index.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_chatbot.config import EmbeddingConfig
from qq_chatbot.index.embeddings import HashEmbedding, OpenAIEmbedding, make_embedding
from qq_chatbot.index.hybrid import HybridRetriever
from qq_chatbot.index.store import build_store, load_store, save_store
from qq_chatbot.schema import Speaker, Turn


class FakeProvider:
    name = "fake-semantic"
    model = "synthetic-v1"
    fallback_to_hash = False
    hash_dim = 16

    def __init__(self):
        self.calls = []
        self.fail = False

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise OSError("offline")
        return [[1.0, 0.0] if any(word in text for word in ("约饭", "聚餐")) else [0.0, 1.0] for text in texts]


def turn(number, text, conversation="c2c:1"):
    return Turn(str(number), Speaker("c2c:1", "Fixture"), conversation, "c2c", number, "", text, [number])


class SemanticIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.turns = [turn(1, "电脑维修"), turn(2, "周末聚餐")]
        self.provider = FakeProvider()
        # Any accidental real provider network call is a test failure.
        guard = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def save(self, provider=None, turns=None):
        provider = provider or self.provider
        store = build_store(self.turns if turns is None else turns, provider)
        save_store(store, self.path, {"fixture": True}, embedding_provider=provider.name, dim=256)
        return store

    def mutate(self, filename, callback):
        path = self.path / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        callback(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_semantic_roundtrip_uses_saved_vectors_and_only_embeds_query(self):
        original = self.save()
        self.provider.calls.clear()
        loaded, meta, speakers = load_store(self.path, self.provider)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(loaded.vectors, original.vectors)
        self.assertTrue(meta["fixture"])
        self.assertEqual(speakers["c2c:1"].name, "Fixture")
        manifest = json.loads((self.path / "manifest.json").read_text())
        self.assertEqual((manifest["embedding_provider"], manifest["embedding_model"], manifest["dim"]), (self.provider.name, self.provider.model, 2))
        hits = HybridRetriever(loaded, self.provider).retrieve("约饭", top_k=1)
        self.assertEqual(hits[0].turn.id, "2")
        self.assertEqual(self.provider.calls, [["约饭"]])

    def test_hash_roundtrip_does_not_embed_documents(self):
        hp = HashEmbedding(16)
        self.save(hp)
        with patch.object(hp, "embed", side_effect=AssertionError("load must not embed")):
            store, _, _ = load_store(self.path, hp)
        self.assertEqual(len(store.vectors), 2)

    def test_load_rejects_provider_model_and_hash_dimension_mismatch(self):
        self.save()
        for attr, value in (("name", "other"), ("model", "other")):
            with self.subTest(attr=attr):
                provider = FakeProvider()
                setattr(provider, attr, value)
                with self.assertRaisesRegex(ValueError, "provider/model"):
                    load_store(self.path, provider)
                self.assertEqual(provider.calls, [])
        self.save(HashEmbedding(16))
        with self.assertRaisesRegex(ValueError, "dim"):
            load_store(self.path, HashEmbedding(32))

    def test_missing_vectors_never_reembed(self):
        self.save()
        (self.path / "embeddings.json").unlink()
        self.provider.calls.clear()
        with self.assertRaisesRegex(ValueError, "持久化向量"):
            load_store(self.path, self.provider)
        self.assertEqual(self.provider.calls, [])

    def test_corrupt_vectors_and_manifest_are_rejected(self):
        mutations = [
            ("embeddings.json", lambda d: d["vectors"].pop()),
            ("embeddings.json", lambda d: d["vectors"].__setitem__(0, [1.])),
            ("embeddings.json", lambda d: d["vectors"].__setitem__(0, [float("nan"), 1.])),
            ("embeddings.json", lambda d: d["turn_ids"].reverse()),
            ("manifest.json", lambda d: d.__setitem__("dim", 3)),
            ("manifest.json", lambda d: d.__setitem__("turn_count", 10)),
        ]
        for filename, mutation in mutations:
            with self.subTest(filename=filename, mutation=mutation):
                self.save()
                self.mutate(filename, mutation)
                with self.assertRaises(ValueError):
                    load_store(self.path, self.provider)

    def test_bad_build_responses_fail_without_partial_store(self):
        for vectors in ([], [[1., 0.]], [[1., 0.], [1.]], [[float("inf"), 0.], [0., 1.]], [[True, 0.], [0., 1.]]):
            with self.subTest(vectors=vectors), patch.object(self.provider, "embed", return_value=vectors):
                with self.assertRaises(ValueError):
                    build_store(self.turns, self.provider)

    def test_query_response_dimension_and_model_are_checked(self):
        store = self.save()
        retriever = HybridRetriever(store, self.provider)
        with patch.object(self.provider, "embed", return_value=[[1., 2., 3.]]):
            with self.assertRaisesRegex(ValueError, "维度"):
                retriever.retrieve("约饭")
        self.provider.model = "changed"
        with self.assertRaisesRegex(ValueError, "provider/model"):
            retriever.retrieve("约饭")

    def test_disabled_fallback_propagates_failure(self):
        store = self.save()
        self.provider.fail = True
        with self.assertRaises(OSError):
            build_store(self.turns, self.provider)
        with self.assertRaises(OSError):
            HybridRetriever(store, self.provider).retrieve("聚餐")

    def test_build_fallback_is_entire_hash_space_and_persisted_as_hash(self):
        self.provider.fallback_to_hash = True
        self.provider.fail = True
        with self.assertWarnsRegex(RuntimeWarning, "不具备语义能力"):
            store = self.save()
        self.assertEqual(store.vectors, HashEmbedding(16).embed([t.text for t in self.turns]))
        self.assertEqual(store.embedding["provider"], "hash")
        with self.assertWarns(RuntimeWarning):
            loaded, _, _ = load_store(self.path, self.provider)
        hits = HybridRetriever(loaded, self.provider).retrieve("聚餐", top_k=1)
        self.assertEqual(hits[0].turn.id, "2")

    def test_query_fallback_uses_full_persisted_hash_space(self):
        self.provider.fallback_to_hash = True
        store = self.save()
        self.provider.fail = True
        loaded, _, _ = load_store(self.path, self.provider)
        baseline = HybridRetriever(build_store(self.turns, HashEmbedding(16)), HashEmbedding(16))
        retriever = HybridRetriever(loaded, self.provider)
        with self.assertWarnsRegex(RuntimeWarning, "不具备语义能力"):
            hits = retriever.retrieve("聚餐")
        expected = baseline.retrieve("聚餐")
        self.assertEqual([(h.turn.id, h.score) for h in hits], [(h.turn.id, h.score) for h in expected])
        self.assertEqual(loaded.vectors, store.vectors)
        self.assertEqual(loaded.embedding["provider"], self.provider.name)
        self.assertEqual(retriever._fallback_store.vectors, store.fallback_vectors)

    def test_scoped_retrieval_does_not_drop_candidates_outside_global_top_2k(self):
        turns = [turn(i, "聚餐", "other") for i in range(8)] + [turn(9, "约饭", "target")]
        store = build_store(turns, self.provider)
        hits = HybridRetriever(store, self.provider).retrieve("约饭", top_k=1, conversation="target")
        self.assertEqual([h.turn.id for h in hits], ["9"])

    def test_legacy_hash_migrates_but_legacy_semantic_requires_model(self):
        self.save(HashEmbedding(16))
        self.mutate("manifest.json", lambda d: (d.pop("format_version"), d.pop("embedding_model")))
        (self.path / "embeddings.json").unlink()
        loaded, _, _ = load_store(self.path, HashEmbedding(16))
        self.assertEqual(len(loaded.vectors), 2)
        self.save()
        self.mutate("manifest.json", lambda d: (d.pop("format_version"), d.pop("embedding_model")))
        with self.assertRaisesRegex(ValueError, "缺少 model"):
            load_store(self.path, self.provider)

    def test_openai_response_order_follows_index(self):
        provider = OpenAIEmbedding("fixture", api_key="not-a-real-key")
        with patch("qq_chatbot.index.embeddings._post", return_value={"data": [{"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}]}):
            self.assertEqual(provider.embed(["a", "b"]), [[1., 0.], [0., 1.]])
        with patch("qq_chatbot.index.embeddings._post", return_value={"data": [{"index": 0, "embedding": [1.]}, {"index": 0, "embedding": [2.]}]}):
            with self.assertRaises(ValueError):
                provider.embed(["a", "b"])

    def test_duplicate_turn_ids_preserve_vector_order(self):
        self.turns[1].id = self.turns[0].id
        store = self.save()
        loaded, _, _ = load_store(self.path, self.provider)
        self.assertEqual(loaded.vectors, store.vectors)

    def test_factory_attaches_explicit_fallback_policy(self):
        config = EmbeddingConfig(provider="hash", dim=16)
        config.fallback_to_hash = True
        provider = make_embedding(config)
        self.assertTrue(provider.fallback_to_hash)
        self.assertEqual(provider.hash_dim, 16)

    def test_fallback_vector_corruption_is_rejected_at_load(self):
        self.provider.fallback_to_hash = True
        self.save()
        self.mutate("embeddings.json", lambda d: d["fallback_vectors"].pop())
        with self.assertRaisesRegex(ValueError, "数量"):
            load_store(self.path, self.provider)

    def test_enable_query_fallback_after_build_creates_whole_offline_space(self):
        store = self.save()
        self.provider.fallback_to_hash = True
        self.provider.fail = True
        retriever = HybridRetriever(store, self.provider)
        with self.assertWarns(RuntimeWarning):
            hits = retriever.retrieve("聚餐", top_k=1)
        self.assertEqual(hits[0].turn.id, "2")
        self.assertEqual(retriever._fallback_store.vectors, HashEmbedding(16).embed([t.text for t in self.turns]))

    def test_interrupted_publish_restores_previous_complete_index(self):
        import os
        self.save()
        names = ("corpus.json", "embeddings.json", "manifest.json")
        previous = {name: (self.path / name).read_bytes() for name in names}
        replacement = build_store([turn(1, "变更文本"), turn(2, "聚餐")], self.provider)
        real_replace = os.replace
        calls = 0

        def interrupted_replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interrupted publish")
            return real_replace(source, destination)

        with patch("qq_chatbot.index.store.os.replace", side_effect=interrupted_replace):
            with self.assertRaisesRegex(OSError, "interrupted"):
                save_store(replacement, self.path, {}, embedding_provider=self.provider.name, dim=2)
        self.assertEqual(previous, {name: (self.path / name).read_bytes() for name in names})
        loaded, _, _ = load_store(self.path, self.provider)
        self.assertEqual(loaded.turns[0].text, self.turns[0].text)

    def test_serialization_failure_preserves_previous_complete_index(self):
        store = self.save()
        before = (self.path / "manifest.json").read_bytes()
        with self.assertRaises(TypeError):
            save_store(store, self.path, {"bad": object()}, embedding_provider=self.provider.name, dim=2)
        self.assertEqual((self.path / "manifest.json").read_bytes(), before)
        loaded, _, _ = load_store(self.path, self.provider)
        self.assertEqual(loaded.vectors, store.vectors)

    def test_mixed_generations_fail_even_with_identical_turn_ids(self):
        self.save()
        old_corpus = (self.path / "corpus.json").read_bytes()
        old_vectors = (self.path / "embeddings.json").read_bytes()
        self.save()
        for filename, old in (("corpus.json", old_corpus), ("embeddings.json", old_vectors)):
            with self.subTest(filename=filename):
                current = (self.path / filename).read_bytes()
                (self.path / filename).write_bytes(old)
                with self.assertRaisesRegex(ValueError, "generation"):
                    load_store(self.path, self.provider)
                (self.path / filename).write_bytes(current)

    def test_in_memory_partial_fallback_is_rejected(self):
        self.provider.fallback_to_hash = True
        store = self.save()
        store.fallback_vectors.pop()
        self.provider.fail = True
        with self.assertWarns(RuntimeWarning), self.assertRaisesRegex(ValueError, "数量"):
            HybridRetriever(store, self.provider).retrieve("聚餐")

    def test_fallback_cache_refreshes_after_adding_document(self):
        self.provider.fallback_to_hash = True
        store = self.save()
        self.provider.fail = True
        retriever = HybridRetriever(store, self.provider)
        with self.assertWarns(RuntimeWarning):
            retriever.retrieve("聚餐")
        store.add(turn(3, "独特新词"), [0., 1.])
        with self.assertWarns(RuntimeWarning):
            hits = retriever.retrieve("独特新词", top_k=1)
        self.assertEqual(hits[0].turn.id, "3")
        self.assertEqual(len(retriever._fallback_store.vectors), 3)

    def test_empty_index_roundtrip(self):
        self.save(turns=[])
        loaded, _, _ = load_store(self.path, self.provider)
        self.assertEqual(HybridRetriever(loaded, self.provider).retrieve("约饭"), [])
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
