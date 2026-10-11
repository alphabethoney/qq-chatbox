"""Offline cache integration tests: python tests/test_sync_decrypt_cache.py."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_sync_index import T, add, make_db, export_local, fake_build
from tools import sync_index as syncer
from tools import export_large as exporter


def export_cached(db, out, *, since=None, resume=False):
    argv = ["export", "--db", str(db), "--out", str(out), "--format", "json", "--strict", "--immutable"]
    if since is not None:
        argv += ["--since", str(since)]
    if resume:
        argv += ["--resume"]
    with patch.object(sys, "argv", argv):
        if exporter.main() != 0:
            raise RuntimeError("export failed")


class CacheSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qq-cache-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "source.db"
        make_db(self.db)
        self.out, self.work, self.cache = (self.root / p for p in ("index", "work", "cache"))
        self.identity = "synthetic-source-a"
        self.locked = False
        self.calls, self.cache_calls = [], []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(patch("qq_exporter.decrypt_cache.open_decrypted_cache", self.open_cache))
        self.stack.enter_context(patch.object(syncer, "decrypt", side_effect=AssertionError("legacy decrypt forbidden")))
        self.stack.enter_context(patch.object(syncer, "export_fault_tolerant", side_effect=self.export))
        self.stack.enter_context(patch.object(syncer, "build", side_effect=self.build))
        self.stack.enter_context(patch.object(syncer.subprocess, "run", side_effect=AssertionError("subprocess forbidden")))
        original_merge = syncer.merge
        def merge(*args):
            self.assertFalse(self.locked)
            return original_merge(*args)
        self.stack.enter_context(patch.object(syncer, "merge", side_effect=merge))

    @contextlib.contextmanager
    def open_cache(self, src, cache_dir, enc_key, *, force_rebuild=False, **kw):
        self.assertEqual(enc_key, b"k" * 32)
        self.cache_calls.append(force_rebuild)
        cache_dir.mkdir(parents=True, exist_ok=True)
        plain = cache_dir / "cached.db"
        unchanged = plain.exists() and plain.read_bytes() == src.read_bytes()
        shutil.copyfile(src, plain)
        self.locked = True
        try:
            yield plain, {"source_identity": self.identity, "pages": 5,
                          "decrypted_pages": 0 if unchanged else 5,
                          "reused_pages": 5 if unchanged else 0, "integrity": "ok"}
        finally:
            self.locked = False

    def export(self, db, out, *, since=None, immutable=False):
        self.assertTrue(self.locked)
        self.assertTrue(immutable)
        self.assertEqual(db, self.cache / "cached.db")
        self.assertFalse((out.parent / "plain").exists())
        self.calls.append(since)
        export_cached(db, out, since=since)

    def build(self, person, json_dir, out, **kw):
        self.assertFalse(self.locked)
        fake_build(person, json_dir, out, **kw)

    def sync(self, **kw):
        args = dict(live_db=self.db, base_json=self.root / "missing-base", work=self.work,
                    out=self.out, key=(b"k" * 32).hex(), overlap_seconds=10, decrypt_cache=self.cache)
        args.update(kw)
        return syncer.sync(**args)

    def state(self):
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))["sync"]

    def ids(self):
        return {m["msg_id"] for c in syncer._load_json_dir(self.out / self.state()["archive"]).values()
                for m in c["messages"]}

    def test_counts_context_full_and_rebuild_are_independent(self):
        add(self.db, 1, T)
        self.sync()
        self.sync(full=True)
        self.assertEqual(self.calls, [None, None])
        self.assertEqual(self.cache_calls, [False, False])
        self.sync(rebuild_decrypt_cache=True)
        self.assertEqual(self.calls[-1], T - 10)
        self.assertTrue(self.cache_calls[-1])
        self.assertIn("总页=5 解密=0 复用=5", self.output.getvalue())
        self.assertNotIn(self.identity, self.output.getvalue())
        self.assertNotIn((b"k" * 32).hex(), self.output.getvalue())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_build_failure_preserves_cache_and_retries_old_watermark(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        add(self.db, 2, T + 20)
        with patch.object(syncer, "build", side_effect=RuntimeError("build failed")), self.assertRaises(RuntimeError):
            self.sync()
        self.assertFalse(self.locked)
        self.assertTrue((self.cache / "cached.db").exists())
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        self.sync()
        self.assertEqual(self.calls[-2:], [T - 10, T - 10])
        self.assertEqual(self.ids(), {1, 2})
        self.assertIn("解密=0 复用=5", self.output.getvalue())

    def test_same_path_replacement_isolated_even_after_failed_build(self):
        add(self.db, 1, T)
        old = self.sync()
        old_archive = self.out / old["archive"]
        replacement = self.root / "replacement.db"
        make_db(replacement)
        add(replacement, 9, T - 100)
        shutil.copyfile(replacement, self.db)
        self.identity = "synthetic-source-b"
        with patch.object(syncer, "build", side_effect=RuntimeError("failed")), self.assertRaises(RuntimeError):
            self.sync()
        self.sync(adopt_existing_history=True)
        self.assertEqual(self.calls[-2:], [None, None])
        self.assertEqual(self.ids(), {9})
        self.assertEqual(self.state()["last_msg_time"], T - 100)
        self.assertTrue(old_archive.exists())

    def test_unknown_identity_requires_adoption_and_preserves_history(self):
        add(self.db, 1, T)
        state = self.sync()
        (self.out / state["archive"] / "source-identity.json").unlink()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM c2c_msg_table")
        add(self.db, 2, T + 20)
        before = (self.out / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "adopt-existing-history"):
            self.sync()
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        self.assertEqual(self.ids(), {1})
        self.sync(adopt_existing_history=True)
        self.assertIsNone(self.calls[-1])
        self.assertEqual(self.ids(), {1, 2})
        self.sync()
        self.assertEqual(self.calls[-1], T + 10)

    def test_export_failure_releases_cache_and_preserves_state(self):
        add(self.db, 1, T)
        self.sync()
        before = (self.out / "manifest.json").read_bytes()
        with patch.object(syncer, "export_fault_tolerant", side_effect=RuntimeError("export failed")), self.assertRaises(RuntimeError):
            self.sync()
        self.assertFalse(self.locked)
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)

    def test_push_and_manifest_exclude_cache_details(self):
        add(self.db, 1, T)
        def inspect(host, stage):
            self.assertEqual({p.name for p in stage.iterdir()}, {"corpus.json", "manifest.json"})
            text = (stage / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("sync", json.loads(text))
            self.assertNotIn(self.identity, text)
            self.assertNotIn(str(self.cache), text)
        with patch.object(syncer, "push", side_effect=inspect):
            self.sync(host="synthetic-host")
        text = (self.out / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn(self.identity, text)
        self.assertNotIn("decrypt_cache", text)

    def test_invalid_option_combinations(self):
        for option in ("rebuild_decrypt_cache", "adopt_existing_history"):
            with self.assertRaisesRegex(ValueError, "需要"):
                self.sync(decrypt_cache=None, **{option: True})
        with self.assertRaisesRegex(ValueError, "之外"):
            self.sync(decrypt_cache=self.out / "cache")

    def test_cli_forwards_cache_and_rejects_dependent_options_alone(self):
        argv = ["sync", "--live-db", str(self.db), "--key", "aa", "--decrypt-cache", str(self.cache),
                "--rebuild-decrypt-cache", "--adopt-existing-history", "--full"]
        with patch.object(sys, "argv", argv), patch.object(syncer, "sync") as call:
            self.assertEqual(syncer.main(), 0)
        self.assertEqual(call.call_args.kwargs["decrypt_cache"], self.cache)
        for key in ("rebuild_decrypt_cache", "adopt_existing_history", "full"):
            self.assertTrue(call.call_args.kwargs[key])
        for flag in ("--rebuild-decrypt-cache", "--adopt-existing-history"):
            with patch.object(sys, "argv", ["sync", flag]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    syncer.main()
            self.assertEqual(error.exception.code, 2)


class RealCacheSyncTests(unittest.TestCase):
    def test_real_decrypt_export_retry_replacement_and_wal_mode_header(self):
        from qq_exporter import sqlcipher
        from test_wal import KEY, PAGE, RESERVE, SALT, encrypt_page

        def encrypt_fixture(plain, encrypted, salt=SALT):
            # Leaf-only synthetic SQLite: relocate cell storage for codec reserve.
            data = bytearray(plain.read_bytes())
            data[20] = RESERVE
            for start in range(0, len(data), PAGE):
                header = start + (100 if start == 0 else 0)
                self.assertIn(data[header], (10, 13))
                self.assertEqual(data[header + 1:header + 3], b"\x00\x00")
                count = struct.unpack_from(">H", data, header + 3)[0]
                content = struct.unpack_from(">H", data, header + 5)[0]
                self.assertGreaterEqual(content - RESERVE, header - start + 8 + count * 2)
                data[start + content - RESERVE:start + PAGE - RESERVE] = data[start + content:start + PAGE]
                data[start + PAGE - RESERVE:start + PAGE] = bytes(RESERVE)
                struct.pack_into(">H", data, header + 5, content - RESERVE)
                for offset in range(header + 8, header + 8 + count * 2, 2):
                    struct.pack_into(">H", data, offset, struct.unpack_from(">H", data, offset)[0] - RESERVE)
            data[18:20] = b"\x02\x02"
            check = plain.with_suffix(".reserved.db")
            check.write_bytes(data)
            with contextlib.closing(sqlite3.connect(check.as_uri() + "?immutable=1", uri=True)) as conn:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            encrypted.write_bytes(b"SQLite header 3\x00" + bytes(sqlcipher.EXT_HEADER - 16)
                                  + b"".join(encrypt_page(bytes(data[i:i + PAGE]), i // PAGE + 1, salt)
                                             for i in range(0, len(data), PAGE)))

        with tempfile.TemporaryDirectory(prefix="qq-real-sync-") as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            plain, source = root / "fixture.db", root / "encrypted.db"
            make_db(plain)
            add(plain, 1, T)
            encrypt_fixture(plain, source)
            out, cache = root / "index", root / "cache # encoded"
            args = dict(live_db=source, base_json=root / "missing", work=root / "work", out=out,
                        decrypt_cache=cache, key=KEY.hex(), overlap_seconds=10)
            calls = []
            def export(db, directory, *, since=None, immutable=False):
                self.assertEqual(db.parent, cache)
                self.assertTrue(immutable)
                calls.append(since)
                export_cached(db, directory, since=since)
            with patch.object(syncer, "build", side_effect=fake_build), \
                 patch.object(syncer, "export_fault_tolerant", side_effect=export), \
                 patch.object(syncer.subprocess, "run", side_effect=AssertionError("subprocess forbidden")):
                first = syncer.sync(**args)
                self.assertEqual(first["last_msg_time"], T)
                syncer.sync(**args)
                for suffix in ("-wal", "-shm", "-journal"):
                    self.assertFalse((cache / ("plain.db" + suffix)).exists())
                add(plain, 2, T + 20)
                encrypt_fixture(plain, source)
                before = (out / "manifest.json").read_bytes()
                with patch.object(syncer, "build", side_effect=RuntimeError("failed")), self.assertRaises(RuntimeError):
                    syncer.sync(**args)
                self.assertEqual((out / "manifest.json").read_bytes(), before)
                with patch.object(sqlcipher, "decrypt_page", side_effect=AssertionError("cache was not reused")):
                    second = syncer.sync(**args)
                self.assertEqual(calls, [None, T - 10, T - 10, T - 10])
                self.assertEqual(second["last_msg_time"], T + 20)
                replacement = root / "other.db"
                make_db(replacement)
                add(replacement, 9, T - 100)
                encrypt_fixture(replacement, source, salt=b"fedcba9876543210")
                third = syncer.sync(**args)
                self.assertIsNone(calls[-1])
                self.assertEqual(third["last_msg_time"], T - 100)
                messages = [m for c in syncer._load_json_dir(out / third["archive"]).values() for m in c["messages"]]
                self.assertEqual({m["msg_id"] for m in messages}, {9})
                self.assertTrue((out / second["archive"]).exists())

    def test_export_uri_encoding_and_resume_immutable_parameter(self):
        with tempfile.TemporaryDirectory(prefix="qq-uri-") as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            db = root / "database # spaces.db"
            make_db(db)
            add(db, 1, T)
            export_local(db, root / "ordinary")
            export_cached(db, root / "resume", resume=True)
            checkpoint = json.loads((root / "resume" / ".export-checkpoint.json").read_text())
            self.assertTrue(checkpoint["parameters"]["immutable"])
            argv = ["export", "--db", str(db), "--out", str(root / "resume"), "--format", "json", "--strict", "--resume"]
            with patch.object(sys, "argv", argv), self.assertRaisesRegex(ValueError, "参数"):
                exporter.main()


if __name__ == "__main__":
    unittest.main(verbosity=2)
