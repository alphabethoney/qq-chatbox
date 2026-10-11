"""Offline R1 regression tests: python tests/test_sync_index.py.

Only temporary synthetic SQLite/JSON fixtures; no real QQ data or network.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import export_large as exporter
from tools import sync_index as syncer

T = 1700000000


def make_db(path):
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        for table in ("c2c_msg_table", "group_msg_table"):
            cols = ",".join(f'"{c}" ' + ("BLOB" if c == "40800" else "INTEGER") for c in exporter.MSG_COLS)
            conn.execute(f'CREATE TABLE {table} ({cols})')
            conn.execute(f'CREATE INDEX {table}_time ON {table}("40050")')


def add(path, mid, timestamp, *, group=False, peer=22):
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        table = "group_msg_table" if group else "c2c_msg_table"
        conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (mid, timestamp, 1, "u11", 11, f"u{peer}", peer, 2, 0, b""))


def write_export(path, ids):
    path.mkdir(parents=True, exist_ok=True)
    msgs = [{"msg_id": i, "time": T - 100 + i, "text": str(i)} for i in ids]
    entry = {"id": "c2c:22", "kind": "c2c", "title": "fixture", "file": "conversation.json"}
    (path / "index.json").write_text(json.dumps({"conversations": [entry]}), encoding="utf-8")
    (path / "conversation.json").write_text(json.dumps({"messages": msgs}), encoding="utf-8")


def export_local(db, out, *, since=None, strict=True):
    argv = ["export_large", "--db", str(db), "--out", str(out), "--format", "json"]
    if since is not None:
        argv += ["--since", str(since)]
    if strict:
        argv += ["--strict"]
    with patch.object(sys, "argv", argv):
        if exporter.main() != 0:
            raise RuntimeError("export failed")


def fake_build(person, json_dir, out, *, config=None):
    out.mkdir(parents=True, exist_ok=True)
    (out / "corpus.json").write_text(json.dumps(syncer._load_json_dir(json_dir)), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"embedding_provider": "hash", "dim": 32, "person": person}), encoding="utf-8")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qq-sync-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "source.db"
        make_db(self.db)
        self.out = self.root / "store"
        self.base = self.root / "base"
        self.work = self.root / "work"
        self.calls = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(syncer, "decrypt", side_effect=lambda src, dst, **kw: shutil.copyfile(src, dst)))
        self.stack.enter_context(patch.object(syncer, "export_fault_tolerant", side_effect=self.export))
        self.stack.enter_context(patch.object(syncer, "build", side_effect=fake_build))
        # Any accidentally invoked subprocess (including scp/ssh) fails offline.
        self.stack.enter_context(patch.object(syncer.subprocess, "run", side_effect=AssertionError("subprocess forbidden")))

    def export(self, db, out, *, since=None):
        self.calls.append(since)
        export_local(db, out, since=since)

    def sync(self, **kw):
        args = dict(live_db=self.db, base_json=self.base, work=self.work,
                    out=self.out, person="11", overlap_seconds=10)
        args.update(kw)
        return syncer.sync(**args)

    def state(self):
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))["sync"]

    def messages(self):
        archive = self.out / self.state()["archive"]
        return [m for c in syncer._load_json_dir(archive).values() for m in c["messages"]]

    def test_first_full_and_three_round_cumulative_retention(self):
        write_export(self.base, [90])
        add(self.db, 1, T)
        self.sync()
        self.assertIsNone(self.calls[-1])
        add(self.db, 2, T + 20)
        self.sync()
        self.assertEqual(self.calls[-1], T - 10)
        shutil.rmtree(self.work)  # work is disposable, never the canonical archive
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM c2c_msg_table WHERE "40001"=1')
        add(self.db, 3, T + 40)
        self.sync()
        self.assertEqual(self.calls[-1], T + 10)
        self.assertEqual({m["msg_id"] for m in self.messages()}, {1, 2, 3, 90})
        self.assertEqual(self.state()["last_msg_time"], T + 40)
        self.assertEqual(len(list((self.out / ".sync").glob("data-*"))), 1)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_overlap_same_second_late_arrivals_dedup_and_full_repair(self):
        add(self.db, 1, T)
        self.sync()
        add(self.db, 2, T)
        add(self.db, 3, T - 9)
        add(self.db, 4, T - 20)
        self.sync()
        self.assertEqual({m["msg_id"] for m in self.messages()}, {1, 2, 3})
        self.sync()
        self.assertEqual(len(self.messages()), 3)
        self.sync(full=True)
        self.assertIsNone(self.calls[-1])
        self.assertEqual({m["msg_id"] for m in self.messages()}, {1, 2, 3, 4})

    def test_no_new_rows_never_uses_wall_clock(self):
        add(self.db, 1, T)
        self.sync()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM c2c_msg_table")
        self.sync()
        self.assertEqual(self.state()["last_msg_time"], T)
        self.assertEqual(len(self.messages()), 1)

    def test_config_change_and_base_content_change_force_full(self):
        add(self.db, 1, T)
        write_export(self.base, [90])
        self.sync()
        self.sync(person="22")
        self.assertIsNone(self.calls[-1])
        write_export(self.base, [91])
        self.sync(person="22")
        self.assertIsNone(self.calls[-1])
        self.assertEqual({m["msg_id"] for m in self.messages()}, {1, 90, 91})
        self.sync(person="22", overlap_seconds=20)
        self.assertIsNone(self.calls[-1])

    def test_source_change_starts_new_lineage(self):
        add(self.db, 1, T)
        self.sync()
        other = self.root / "other.db"
        make_db(other)
        add(other, 2, T - 100)
        self.sync(live_db=other)
        self.assertIsNone(self.calls[-1])
        self.assertEqual({m["msg_id"] for m in self.messages()}, {2})
        self.assertEqual(self.state()["last_msg_time"], T - 100)

    def test_failure_at_each_stage_preserves_committed_bytes(self):
        add(self.db, 1, T)
        self.sync()
        add(self.db, 2, T + 20)
        manifest = (self.out / "manifest.json").read_bytes()
        corpus = (self.out / "corpus.json").read_bytes()
        for method in ("decrypt", "export_fault_tolerant", "merge", "build", "push"):
            with self.subTest(stage=method), patch.object(syncer, method, side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    self.sync(host="unused" if method == "push" else None)
            self.assertEqual((self.out / "manifest.json").read_bytes(), manifest)
            self.assertEqual((self.out / "corpus.json").read_bytes(), corpus)
            self.assertEqual(len(self.messages()), 1)
        self.sync()
        self.assertEqual({m["msg_id"] for m in self.messages()}, {1, 2})

    def test_publish_manifest_failure_rolls_back_index(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes(), (self.out / "corpus.json").read_bytes()
        original = syncer._atomic_write
        def fail_manifest(path, data):
            if path.name == "manifest.json":
                raise OSError("simulated disk full")
            original(path, data)
        add(self.db, 2, T + 20)
        with patch.object(syncer, "_atomic_write", side_effect=fail_manifest), self.assertRaises(OSError):
            self.sync()
        self.assertEqual(before, ((self.out / "manifest.json").read_bytes(), (self.out / "corpus.json").read_bytes()))
        self.assertEqual(len(list((self.out / ".sync").glob("data-*"))), 1)

    def test_partial_export_even_exit_zero_cannot_commit(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        def partial(db, out, *, since):
            export_local(db, out, since=since)
            path = out / "json" / "index.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["export"]["complete"] = False
            path.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(syncer, "export_fault_tolerant", side_effect=partial), self.assertRaises(ValueError):
            self.sync()
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)

    def test_missing_archive_fails_closed(self):
        add(self.db, 1, T)
        self.sync()
        archive = self.out / self.state()["archive"]
        (archive / "index.json").unlink()
        count = len(self.calls)
        with self.assertRaises(FileNotFoundError):
            self.sync()
        self.assertEqual(len(self.calls), count)

    def test_modified_archive_fails_closed(self):
        add(self.db, 1, T)
        self.sync()
        archive = self.out / self.state()["archive"]
        index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
        path = archive / index["conversations"][0]["file"]
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "校验"):
            self.sync()

    def test_concurrent_sync_lock_and_release(self):
        add(self.db, 1, T)
        def nested(src, dst, **kw):
            with self.assertRaises(OSError):
                self.sync()
            shutil.copyfile(src, dst)
        with patch.object(syncer, "decrypt", side_effect=nested):
            self.sync()
        self.sync()  # released even with nested attempt

    def test_build_config_forwarding_and_content_fingerprint(self):
        add(self.db, 1, T)
        config = self.root / "build.json"
        config.write_text('{"embedding":{"provider":"hash","dim":16}}', encoding="utf-8")
        self.sync(build_config=config)
        self.assertEqual(syncer.build.call_args.kwargs, {"config": config.resolve()})
        self.sync(build_config=config)
        self.assertEqual(self.calls[-1], T - 10)
        config.write_text('{"embedding":{"provider":"hash","dim":32}}', encoding="utf-8")
        self.sync(build_config=config)
        self.assertIsNone(self.calls[-1])

    def test_push_contains_only_index_without_local_sync_state(self):
        add(self.db, 1, T)
        def inspect_push(host, stage):
            self.assertEqual(host, "synthetic-host")
            self.assertEqual(stage.name, self.out.name)
            self.assertEqual({p.name for p in stage.iterdir()}, {"corpus.json", "manifest.json"})
            self.assertFalse((stage / ".sync").exists())
            self.assertNotIn("sync", json.loads((stage / "manifest.json").read_text(encoding="utf-8")))
        with patch.object(syncer, "push", side_effect=inspect_push) as pushed:
            self.sync(host="synthetic-host")
        pushed.assert_called_once()
        self.assertIn("archive", self.state())

    def test_runtime_profiles_and_memory_are_preserved(self):
        add(self.db, 1, T)
        self.out.mkdir()
        for name in ("profiles.json", "memory.json"):
            (self.out / name).write_bytes(b"runtime state")
        self.sync()
        for name in ("profiles.json", "memory.json"):
            self.assertEqual((self.out / name).read_bytes(), b"runtime state")

    def test_replace_failure_restores_already_published_corpus(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes(), (self.out / "corpus.json").read_bytes()
        original = syncer.os.replace
        def fail_manifest(source, target):
            if Path(target) == self.out / "manifest.json":
                raise OSError("injected final rename failure")
            return original(source, target)
        add(self.db, 2, T + 20)
        with patch.object(syncer.os, "replace", side_effect=fail_manifest), self.assertRaises(OSError):
            self.sync()
        self.assertEqual(before, ((self.out / "manifest.json").read_bytes(), (self.out / "corpus.json").read_bytes()))

    def test_first_failure_has_no_committed_state(self):
        add(self.db, 1, T)
        with patch.object(syncer, "build", side_effect=RuntimeError("failed build")), self.assertRaises(RuntimeError):
            self.sync()
        self.assertFalse((self.out / "manifest.json").exists())
        self.sync()
        self.assertIsNone(self.calls[-1])

    def test_invalid_state_fails_closed(self):
        self.out.mkdir()
        (self.out / "manifest.json").write_text('{"sync":{"version":1}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.sync()
        self.assertFalse(self.calls)

    def test_zero_overlap_rejected(self):
        with self.assertRaises(ValueError):
            self.sync(overlap_seconds=0)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qq-export-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "fixture.db"
        make_db(self.db)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def test_sql_filter_strict_boundary_seconds_and_milliseconds(self):
        for mid, ts in enumerate((T - 1, T, T + 1, T * 1000, T * 1000 + 500), 1):
            add(self.db, mid, ts)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            statements = []
            conn.set_trace_callback(statements.append)
            rows = list(exporter.read_messages(conn, "c2c_msg_table", since=T))
        self.assertEqual({r[0] for r in rows}, {3, 5})
        self.assertIn('WHERE (("40050"', statements[0])
        export_local(self.db, self.root / "out", since=T)
        data = syncer._load_json_dir(self.root / "out" / "json")
        self.assertEqual(sorted(m["time"] for c in data.values() for m in c["messages"]), [T + .5, T + 1])

    def test_negative_zero_ids_and_collisions_across_conversations(self):
        add(self.db, -1, T, group=True)
        add(self.db, 0, T, group=True)
        add(self.db, 5, T, peer=22)
        add(self.db, 5, T, peer=33)
        add(self.db, 5, T, group=True)
        export_local(self.db, self.root / "out")
        data = syncer._load_json_dir(self.root / "out" / "json")
        self.assertEqual(sum(len(c["messages"]) for c in data.values()), 5)

    def test_bad_page_strict_raises_and_salvage_marks_incomplete(self):
        add(self.db, 1, T)
        original = exporter.read_messages
        def broken(conn, table, **kw):
            yield from original(conn, table, **kw)
            raise sqlite3.DatabaseError("synthetic malformed page")
        with patch.object(exporter, "read_messages", side_effect=broken):
            with self.assertRaises(sqlite3.DatabaseError):
                export_local(self.db, self.root / "strict")
            self.assertFalse((self.root / "strict" / "json" / "index.json").exists())
            export_local(self.db, self.root / "salvage", strict=False)
        report = json.loads((self.root / "salvage" / "json" / "index.json").read_text())["export"]
        self.assertFalse(report["complete"])
        self.assertTrue(report["errors"])

    def test_unknown_timestamp_strict_fails(self):
        add(self.db, 1, None)
        with self.assertRaises(ValueError):
            export_local(self.db, self.root / "out")
        with self.assertRaises(ValueError):
            export_local(self.db, self.root / "incremental", since=T)

    def test_only_group_table_supported(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE c2c_msg_table")
        add(self.db, 1, T, group=True)
        export_local(self.db, self.root / "out", since=T - 1)
        self.assertEqual(len(syncer._load_json_dir(self.root / "out" / "json")), 1)

    def test_missing_conversation_file_not_silently_dropped(self):
        write_export(self.root / "base", [1])
        (self.root / "base" / "conversation.json").unlink()
        with self.assertRaises(FileNotFoundError):
            syncer._load_json_dir(self.root / "base")


if __name__ == "__main__":
    unittest.main(verbosity=2)
