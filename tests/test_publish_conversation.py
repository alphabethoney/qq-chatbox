"""Offline synthetic publication tests; no real data or network."""
from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qq_chatbot.index.embeddings import HashEmbedding
from qq_chatbot.index.store import build_store, load_store, save_store
from qq_chatbot.schema import Speaker, Turn
from tools import publish_conversation as pub
from tools import sync_index


def turn(tid, conversation="c2c:22", mid=1, text="old", speaker="me"):
    return Turn(tid, Speaker(speaker, "old speaker", speaker == "me"), conversation,
                conversation.split(":")[0], 1700000000, "fixture", text, [mid])


def index(path, turns, dim=256):
    hp = HashEmbedding(dim)
    save_store(build_store(turns, hp), path,
               {"conversation_titles": {t.conversation_key: "title" for t in turns}, "custom": "retained"},
               embedding_provider="hash", dim=dim)


def mutate(path, name, change):
    data = json.loads((path / name).read_bytes())
    change(data)
    (path / name).write_text(json.dumps(data), encoding="utf-8")


def hold_lock(target, ready, release):
    with sync_index._sync_lock(Path(target)):
        ready.set()
        release.wait(20)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.target, self.backups = (root / n for n in ("source", "target", "backups"))
        self.others = [turn("other" + str(i), "group:" + str(i), i, "retain " + str(i)) for i in range(35)]
        index(self.target, self.others + [turn("old")])
        index(self.source, [turn("other0", text="replaced"), turn("added", mid=2, text="new")])
        mutate(self.target, "manifest.json", lambda d: d.update(sync={"old": "whole-corpus"}, custom="keep"))
        mutate(self.source, "manifest.json", lambda d: d.update(sync={"scope": "single-private"}))
        mutate(self.source, "corpus.json", lambda d: d["speakers"]["me"].update(name="new name"))
        self.before = pub._read_blobs(self.target)

    def publish(self):
        return pub.publish(self.source, self.target, "c2c:22", self.backups)

    def unchanged(self):
        self.assertEqual(self.before, pub._read_blobs(self.target))

    def test_merge_preserves_35_other_conversations_vectors_metadata_and_profiles(self):
        for directory in ("profiles", "memory"):
            (self.target / directory).mkdir()
            (self.target / directory / "opaque.bin").write_bytes(b"\x00untouched\xff")
        original = json.loads(self.before["corpus.json"])
        original_vectors = json.loads(self.before["embeddings.json"])["vectors"]
        result = self.publish()
        self.assertEqual(result["status"], "published")
        corpus = json.loads((self.target / "corpus.json").read_bytes())
        embeddings = json.loads((self.target / "embeddings.json").read_bytes())
        self.assertEqual(corpus["turns"][:35], original["turns"][:35])
        self.assertEqual(embeddings["vectors"][:35], original_vectors[:35])
        self.assertEqual(corpus["speakers"]["me"], original["speakers"]["me"])
        self.assertEqual(corpus["meta"], original["meta"])
        self.assertEqual([t["text"] for t in corpus["turns"][35:]], ["replaced", "new"])
        self.assertNotEqual(corpus["turns"][35]["id"], "other0")
        self.assertEqual(len({t["id"] for t in corpus["turns"]}), 37)
        manifest = json.loads((self.target / "manifest.json").read_bytes())
        self.assertEqual(manifest["sync"], {"old": "whole-corpus"})
        self.assertEqual(manifest["custom"], "keep")
        self.assertEqual(manifest["publication"]["conversation"], "c2c:22")
        for directory in ("profiles", "memory"):
            self.assertEqual((self.target / directory / "opaque.bin").read_bytes(), b"\x00untouched\xff")
        store, _, _ = load_store(self.target, HashEmbedding(256))
        self.assertEqual(len(store.turns), 37)

    def test_backup_is_exact_readable_and_hashed(self):
        result = self.publish()
        backup = Path(result["backup"])
        self.assertEqual(pub._read_blobs(backup), self.before)
        self.assertEqual(json.loads((backup / "hashmanifest.json").read_bytes())["sha256"], pub._hashes(self.before))
        self.assertEqual(len(load_store(backup, HashEmbedding(256))[0].turns), 36)

    def test_repeat_is_noop_and_modified_target_not_skipped(self):
        first = self.publish()
        before = pub._read_blobs(self.target)
        self.assertEqual(self.publish()["status"], "skipped")
        self.assertEqual(before, pub._read_blobs(self.target))
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        mutate(self.target, "corpus.json", lambda d: d["turns"][0].update(text="externally updated"))
        self.assertEqual(self.publish()["status"], "published")
        self.assertEqual(json.loads((self.target / "corpus.json").read_bytes())["turns"][0]["text"], "externally updated")
        self.assertTrue(Path(first["backup"]).exists())

    def test_missing_history_rejected(self):
        index(self.source, [turn("new", mid=2)])
        with self.assertRaisesRegex(ValueError, "missing old"):
            self.publish()
        self.unchanged()

    def test_empty_and_wrong_scope_rejected(self):
        for turns in ([], [turn("wrong", "c2c:99")]):
            with self.subTest(turns=turns):
                index(self.source, turns)
                with self.assertRaises(ValueError):
                    self.publish()
                self.unchanged()

    def test_space_mismatch_and_remote_and_fallback_rejected(self):
        index(self.source, [turn("new")], dim=32)
        with self.assertRaisesRegex(ValueError, "space mismatch"):
            self.publish()
        for changes in ({"embedding_provider": "openai"}, {"embedding_model": "other"}, {"fallback_embedding": {"dim": 256}}):
            index(self.source, [turn("new")])
            mutate(self.source, "manifest.json", lambda d: d.update(changes))
            with self.assertRaises(ValueError):
                self.publish()
            self.unchanged()

    def test_same_nondefault_dimension_supported(self):
        index(self.source, [turn("new")], dim=32)
        index(self.target, [turn("old")], dim=32)
        self.assertEqual(self.publish()["status"], "published")
        self.assertEqual(len(load_store(self.target, HashEmbedding(32))[0].vectors[0]), 32)

    def test_corrupt_schema_generation_count_and_vectors_rejected(self):
        source_bytes = pub._read_blobs(self.source)
        cases = [("corpus.json", lambda d: d.pop("turns")),
                 ("corpus.json", lambda d: d.update(generation_id="bad")),
                 ("manifest.json", lambda d: d.update(turn_count=0)),
                 ("embeddings.json", lambda d: d["vectors"].pop()),
                 ("corpus.json", lambda d: d["turns"][0].update(msg_ids=None))]
        for name, change in cases:
            with self.subTest(name=name):
                for fn, data in source_bytes.items():
                    (self.source / fn).write_bytes(data)
                mutate(self.source, name, change)
                with self.assertRaises(ValueError):
                    self.publish()
                self.unchanged()

    def test_publish_failure_rolls_back_all_files(self):
        real_replace = sync_index.os.replace
        failed = False
        def fail_once(src, dst):
            nonlocal failed
            if Path(dst) == self.target / "manifest.json" and not failed:
                failed = True
                raise OSError("injected commit failure")
            return real_replace(src, dst)
        with patch.object(sync_index.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected"):
                self.publish()
        self.assertTrue(failed)
        self.unchanged()
        backup = next(self.backups.iterdir())
        self.assertEqual(pub._read_blobs(backup), self.before)

    def test_backup_failure_prevents_publish(self):
        real_write = pub._write_durable
        def fail_receipt(path, data):
            if path.name == "hashmanifest.json":
                raise OSError("backup failed")
            real_write(path, data)
        with patch.object(pub, "_write_durable", side_effect=fail_receipt), patch.object(pub, "_publish") as commit:
            with self.assertRaisesRegex(OSError, "backup failed"):
                self.publish()
            commit.assert_not_called()
        self.unchanged()

    def test_source_or_target_changed_before_commit_rejected(self):
        for directory in (self.source, self.target):
            with self.subTest(directory=directory):
                original = (directory / "corpus.json").read_bytes()
                real_write = pub._write_durable
                def change_after_backup(path, data):
                    real_write(path, data)
                    if path.name == "hashmanifest.json":
                        mutate(directory, "corpus.json", lambda d: d["meta"].update(external=True))
                with patch.object(pub, "_write_durable", side_effect=change_after_backup), patch.object(pub, "_publish") as commit:
                    with self.assertRaisesRegex(ValueError, "changed during"):
                        self.publish()
                    commit.assert_not_called()
                (directory / "corpus.json").write_bytes(original)
                self.unchanged()

    def test_cross_process_lock_shared_with_sync(self):
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        process = ctx.Process(target=hold_lock, args=(str(self.target), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(15), "lock child did not start")
            with self.assertRaises(OSError):
                self.publish()
            self.unchanged()
        finally:
            release.set()
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)

    def test_source_sync_lock_blocks_publication(self):
        with sync_index._sync_lock(self.source):
            with self.assertRaises(OSError):
                self.publish()
        self.unchanged()

    def test_only_target_title_and_unshared_speaker_updated(self):
        index(self.source, [turn("new", speaker="private")])
        mutate(self.source, "corpus.json", lambda d: d["meta"].update(
            conversation_titles={"c2c:22": "updated private", "group:0": "must not change"}, custom="must not copy"))
        mutate(self.source, "corpus.json", lambda d: d["speakers"]["private"].update(name="Private name"))
        self.publish()
        corpus = json.loads((self.target / "corpus.json").read_bytes())
        self.assertEqual(corpus["meta"]["conversation_titles"]["c2c:22"], "updated private")
        self.assertEqual(corpus["meta"]["conversation_titles"]["group:0"], "title")
        self.assertEqual(corpus["meta"]["custom"], "retained")
        self.assertEqual(corpus["speakers"]["private"]["name"], "Private name")

    def test_collision_mapping_is_deterministic_after_republication(self):
        self.publish()
        first_ids = json.loads((self.target / "embeddings.json").read_bytes())["turn_ids"]
        mutate(self.source, "manifest.json", lambda d: d.update(extra="new source digest"))
        self.assertEqual(self.publish()["status"], "published")
        second_ids = json.loads((self.target / "embeddings.json").read_bytes())["turn_ids"]
        self.assertEqual(first_ids, second_ids)

    def test_real_ingest_build_save_title_lists_and_legacy_target_ids(self):
        from qq_chatbot.ingest import load_corpus
        def build_from_export(out, entries):
            export = out.parent / (out.name + "-export")
            export.mkdir()
            records = []
            for i, (key, title, mids) in enumerate(entries):
                name = f"{i}.json"
                records.append({"id": key, "kind": key.split(":")[0], "title": title, "file": name})
                messages = [{"msg_id": mid, "time": 1700000000 + mid,
                             "text": f"fixture message {mid}", "sender_qq": 22, "direction": "in"} for mid in mids]
                (export / name).write_text(json.dumps({"messages": messages}), encoding="utf-8")
            (export / "index.json").write_text(json.dumps({"conversations": records}), encoding="utf-8")
            corpus = load_corpus(json_dir=export)
            save_store(build_store(corpus.turns, HashEmbedding(256)), out, corpus.meta,
                       embedding_provider="hash", dim=256)
        build_from_export(self.target, [("group:100", "Other title", [9]),
                                        ("group:101", "Shared old name", [10]),
                                        ("c2c:22", "Shared old name", [1])])
        build_from_export(self.source, [("c2c:22", "New private title", [1, 2])])
        original = pub._read_blobs(self.target)
        original_corpus = json.loads(original["corpus.json"])
        original_vectors = json.loads(original["embeddings.json"])["vectors"]
        self.assertIsInstance(original_corpus["meta"]["conversation_titles"], list)
        self.assertEqual(self.publish()["status"], "published")
        corpus = json.loads((self.target / "corpus.json").read_bytes())
        vectors = json.loads((self.target / "embeddings.json").read_bytes())["vectors"]
        retained = [(t, v) for t, v in zip(original_corpus["turns"], original_vectors)
                    if t["conversation_key"] != "c2c:22"]
        self.assertEqual(corpus["turns"][:2], [t for t, _ in retained])
        self.assertEqual(vectors[:2], [v for _, v in retained])
        self.assertEqual(corpus["meta"]["conversation_titles"],
                         ["Other title", "Shared old name", "New private title"])
        self.assertEqual(len(load_store(self.target, HashEmbedding(256))[0].turns), 3)
        self.assertEqual(self.publish()["status"], "skipped")

    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = pub.main(["--source", str(self.source), "--target", str(self.target),
                             "--conversation", "c2c:22", "--backup-root", str(self.backups)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "published")


if __name__ == "__main__":
    unittest.main()
