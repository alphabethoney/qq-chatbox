"""Offline scoped synchronization regression tests; only synthetic data."""
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
sys.path.insert(0, str(ROOT / "tests"))
from test_sync_index import make_db, add, T
from tools import sync_index as syncer, export_large
real_build = syncer.build


def build_fixture(person, source, target, **kw):
    target.mkdir(parents=True, exist_ok=True)
    data = syncer._load_json_dir(source)
    turns = [{"conversation_key": key, "kind": item["entry"]["kind"],
              "text": m.get("text", "")} for key, item in data.items() for m in item["messages"]]
    (target / "corpus.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")
    (target / "manifest.json").write_text('{"format_version":2}', encoding="utf-8")


class ScopedSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="scoped-sync-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "source.db"
        make_db(self.db)
        self.out, self.base, self.work = (self.root / n for n in ("out", "base", "work"))
        self.calls = []
        self.identity = "synthetic-account-identity"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(syncer, "decrypt", side_effect=self.decrypt))
        self.stack.enter_context(patch.object(syncer, "export_fault_tolerant", side_effect=self.export))
        self.stack.enter_context(patch.object(syncer, "build", side_effect=build_fixture))
        self.stack.enter_context(patch.object(syncer.subprocess, "run", side_effect=AssertionError("network/subprocess forbidden")))

    def decrypt(self, source, target, **kw):
        self.assertEqual(kw["source_identity_path"], self.db.resolve())
        shutil.copyfile(source, target)
        return {"pages": 1, "integrity": "ok", "accepted": True, "source_identity": self.identity}

    def export(self, db, out, *, since=None, immutable=False, conversation=None, self_qq=None):
        self.calls.append(since)
        argv = ["export", "--db", str(db), "--out", str(out), "--format", "json", "--strict"]
        if since is not None:
            argv += ["--since", str(since)]
        if immutable:
            argv += ["--immutable"]
        if conversation:
            argv += ["--conversation", conversation]
        if self_qq:
            argv += ["--self-qq", str(self_qq)]
        with patch.object(sys, "argv", argv):
            if export_large.main() != 0:
                raise RuntimeError("export failed")

    def sync(self, **kw):
        args = dict(live_db=self.db, base_json=self.base, work=self.work, out=self.out,
                    conversation="c2c:22", self_qq="11", overlap_seconds=10)
        args.update(kw)
        return syncer.sync(**args)

    def state(self):
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))["sync"]

    def ids(self):
        data = syncer._load_json_dir(self.out / self.state()["archive"])
        return {m["msg_id"] for c in data.values() for m in c["messages"]}

    def assert_unchanged(self, before):
        self.assertEqual(before, (self.out / "manifest.json").read_bytes())

    def test_incoming_ntqq_partner_layout_is_accepted(self):
        add(self.db, 1, T)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (2, T, 0, 'u22', 22, 'u22', 22, 2, 0, b''))
        self.sync()
        self.assertEqual(self.ids(), {1, 2})

    def test_build_omits_empty_person_filter(self):
        with patch.object(syncer, '_run') as run:
            real_build('', self.base, self.out)
            self.assertNotIn('--person', run.call_args.args[0])
            real_build('11', self.base, self.out)
            self.assertEqual(run.call_args.args[0][-2:], ['--person', '11'])

    def test_base_from_another_recipient_is_rejected(self):
        self.base.mkdir()
        entry = {'id': 'c2c:22', 'kind': 'c2c', 'file': 'target.json'}
        (self.base / 'index.json').write_text(json.dumps({'conversations': [entry]}), encoding='utf-8')
        message = {'msg_id': 7, 'time': T, 'direction': 'in', 'sender_qq': 22, 'peer_qq': 888}
        (self.base / 'target.json').write_text(json.dumps({'messages': [message]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '收件账号'):
            self.sync()
        self.assertFalse((self.out / 'manifest.json').exists())

    def test_scope_both_speakers_same_second_history_and_full(self):
        add(self.db, 1, T)
        add(self.db, 9, T + 999, group=True, peer=888)
        add(self.db, 10, T + 999, peer=33)
        self.sync()
        add(self.db, 2, T)
        add(self.db, 3, T - 100)
        # Incoming target speaker, distinct from --person.
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (4, T, 0, "u22", 22, "u11", 11, 2, 0, b""))
        self.sync()
        self.assertEqual(self.ids(), {1, 2, 4})
        self.sync(full=True)
        self.assertEqual(self.ids(), {1, 2, 3, 4})
        self.assertEqual(self.state()["last_msg_time"], T)
        self.assertEqual(self.state()["scope"]["person"], "")
        self.assertEqual(self.state()["version"], 2)

    def test_zero_startup_and_zero_recent_keep_history(self):
        result = self.sync()
        self.assertEqual(result["status"], "no_target")
        self.assertFalse((self.out / "manifest.json").exists())
        add(self.db, 1, T)
        self.sync()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM c2c_msg_table")
        self.sync()
        self.assertEqual(self.ids(), {1})
        self.assertEqual(self.state()["last_msg_time"], T)

    def test_failures_preserve_watermark_and_retry(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        add(self.db, 2, T + 10)
        for method in ("decrypt", "export_fault_tolerant", "merge", "build", "_publish", "push"):
            with self.subTest(method=method), patch.object(syncer, method, side_effect=RuntimeError("secret body/key")):
                with self.assertRaises(RuntimeError):
                    self.sync(host="synthetic" if method == "push" else None)
            self.assert_unchanged(before)
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in self.work.glob("sync-report-*.json")]
        self.assertTrue(any(r["status"] == "failed" for r in reports))
        self.assertNotIn("secret body/key", json.dumps(reports))
        self.sync()
        self.assertEqual(self.ids(), {1, 2})

    def test_identity_and_scope_policy_changes_rejected(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        for kw in ({"conversation": "c2c:33"}, {"self_qq": "12"}, {"person": "22"},
                   {"integrity_mode": "selected"}, {"snapshot_mode": "offline-copy"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.sync(**kw)
            self.assert_unchanged(before)
        self.identity = "different-salt-account"
        with self.assertRaisesRegex(ValueError, "身份"):
            self.sync()
        self.assert_unchanged(before)

    def test_base_reads_only_target_and_rejects_body_mismatch(self):
        add(self.db, 1, T)
        self.export(self.db, self.base.parent / "base-export", conversation="c2c:22", self_qq="11")
        shutil.copytree(self.base.parent / "base-export" / "json", self.base)
        path = self.base / "index.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["conversations"].append({"id": "group:999", "kind": "group", "file": "missing-never-read.json"})
        path.write_text(json.dumps(data), encoding="utf-8")
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        body = self.base / data["conversations"][0]["file"]
        doc = json.loads(body.read_text(encoding="utf-8"))
        doc["messages"][0]["peer_qq"] = 999
        body.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "范围"):
            self.sync()
        self.assert_unchanged(before)

    def test_cumulative_and_artifact_mismatch_rejected(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        def bad_build(person, source, target, **kw):
            build_fixture(person, source, target, **kw)
            (target / "corpus.json").write_text('{"turns":[{"conversation_key":"group:9","kind":"group"}]}', encoding="utf-8")
        with patch.object(syncer, "build", side_effect=bad_build), self.assertRaisesRegex(ValueError, "产物"):
            self.sync()
        self.assert_unchanged(before)
        archive = self.out / self.state()["archive"]
        index = json.loads((archive / "index.json").read_text(encoding="utf-8"))
        index["conversations"].append({"id": "group:9", "kind": "group", "file": "not-read.json"})
        (archive / "index.json").write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "范围"):
            self.sync()
        self.assert_unchanged(before)

    def test_snapshot_verify_before_publish_and_exit_rolls_back(self):
        from qq_exporter.snapshot import SourceBusy
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        class Snapshot:
            path = self.db
            metadata = {}
            def verify(inner):
                raise SourceBusy("synthetic change")
        @contextlib.contextmanager
        def snapshot(*a, **kw):
            yield Snapshot()
        with patch("qq_exporter.snapshot.open_snapshot", snapshot), self.assertRaises(SourceBusy):
            self.sync()
        self.assert_unchanged(before)
        @contextlib.contextmanager
        def late_snapshot(*a, **kw):
            obj = Snapshot()
            obj.verify = lambda: None
            yield obj
            raise SourceBusy("late change")
        with patch("qq_exporter.snapshot.open_snapshot", late_snapshot), self.assertRaises(SourceBusy):
            self.sync()
        self.assert_unchanged(before)
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in self.work.glob("sync-report-*.json")]
        self.assertTrue(any(r["status"] == "source_busy" for r in reports))

    def test_key_files_and_remote_manifest(self):
        add(self.db, 1, T)
        keyfile = self.root / "key.json"
        keyfile.write_text(json.dumps({"key": "ab" * 32, "db": str(self.db), "account": "11"}), encoding="utf-8")
        def inspect(host, path):
            payload = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["scope"]["conversation"], "c2c:22")
            self.assertNotIn("sync", payload)
            self.assertNotIn(str(self.root), json.dumps(payload))
            self.assertNotIn("ab" * 32, json.dumps(payload))
        with patch.object(syncer, "push", side_effect=inspect):
            self.sync(key_file=keyfile, host="synthetic")
        with self.assertRaises(ValueError):
            self.sync(key_file=keyfile, key="ab" * 32)
        keyfile.write_text("cd" * 32, encoding="utf-8")
        self.assertEqual(syncer._read_key_file(keyfile, self.db, "11"), "cd" * 32)

    def test_v1_cannot_narrow(self):
        self.out.mkdir()
        (self.out / "manifest.json").write_text('{"sync":{"version":1}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "新 --out"):
            self.sync()

    def test_cli_mutual_exclusion_and_sourcebusy_exit(self):
        from qq_exporter.snapshot import SourceBusy
        with patch.object(sys, "argv", ["sync", "--key", "aa" * 32, "--passphrase-file", "x"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                syncer.main()
            self.assertEqual(raised.exception.code, 2)
        with patch.object(sys, "argv", ["sync", "--key", "aa" * 32, "--live-db", str(self.db)]), patch.object(syncer, "sync", side_effect=SourceBusy("busy")) as called, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(syncer.main(), 3)
            self.assertEqual(called.call_args.kwargs["snapshot_mode"], "offline-copy")


if __name__ == "__main__":
    unittest.main()
