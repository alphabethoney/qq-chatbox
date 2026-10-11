"""Synthetic offline codec cache tests; never read a real QQ database."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_wal import KEY, SALT, PAGE, RESERVE, plain_page1, data_page, encrypt_page
from qq_exporter import decrypt_cache as dc
from qq_exporter import sqlcipher as sc


def database_pages(count=4, version=0):
    """Valid SQLite: an empty schema plus freelist pages usable for mutations."""
    first = bytearray(plain_page1(version, pages=count))
    if count == 1:
        return [bytes(first)]
    struct.pack_into(">II", first, 32, 2, count - 1)
    trunk = bytearray(PAGE)
    struct.pack_into(">II", trunk, 0, 0, count - 2)
    for i, number in enumerate(range(3, count + 1)):
        struct.pack_into(">I", trunk, 8 + 4 * i, number)
    return [bytes(first), bytes(trunk)] + [data_page(n % 255) for n in range(3, count + 1)]


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "encrypted.db"
        self.cache = self.root / "cache"
        self.pages = database_pages()
        self.write_source()
        self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            if job.poll() is None:
                job.kill()
            job.wait(timeout=10)
        self.tmp.cleanup()

    def write_source(self, salt=SALT, header_byte=0):
        header = b"SQLite header 3\0" + bytes([header_byte]) + bytes(sc.EXT_HEADER - 17)
        self.src.write_bytes(header + b"".join(encrypt_page(p, i, salt=salt)
                                               for i, p in enumerate(self.pages, 1)))

    def open(self, **kwargs):
        return dc.open_decrypted_cache(self.src, self.cache, KEY, **kwargs)

    def baseline(self):
        with self.open() as (path, stats):
            return path, path.read_bytes(), stats

    def child(self, body):
        script = ("import sys,time\nfrom pathlib import Path\n"
                  f"sys.path.insert(0,{str(Path(__file__).resolve().parent.parent)!r})\n"
                  "from qq_exporter import decrypt_cache as dc\n"
                  f"src=Path({str(self.src)!r}); cache=Path({str(self.cache)!r})\n"
                  "key=bytes(range(32))\n" + body)
        process = subprocess.Popen([sys.executable, "-c", script],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.jobs.append(process)
        return process

    def wait_marker(self, marker, process):
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"child exited early: {process.returncode}")
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "child did not reach checkpoint")

    def test_first_noop_changed_actual_decrypt_calls(self):
        with patch.object(sc, "decrypt_page", wraps=sc.decrypt_page) as decrypt:
            path, content, stats = self.baseline()
            self.assertEqual(decrypt.call_count, 4)
            self.assertEqual(content, b"".join(self.pages))
            self.assertTrue(stats["rebuilt"])
        with patch.object(sc, "decrypt_page", wraps=sc.decrypt_page) as decrypt:
            with self.open() as (_, stats):
                self.assertEqual(decrypt.call_count, 0)
                self.assertEqual((stats["decrypted_pages"], stats["reused_pages"]), (0, 4))
                self.assertEqual(stats["written_bytes"], 0)
        self.pages[2] = data_page(82)
        self.write_source()
        with patch.object(sc, "decrypt_page", wraps=sc.decrypt_page) as decrypt:
            with self.open() as (path, stats):
                self.assertEqual(decrypt.call_count, 1)
                self.assertEqual((stats["decrypted_pages"], stats["reused_pages"]), (1, 3))
                self.assertEqual(path.read_bytes(), b"".join(self.pages))
                self.assertEqual(stats["written_bytes"], PAGE)
                self.assertEqual(stats["journal_bytes"], 2 * PAGE)

    def test_growth_and_truncation(self):
        self.baseline()
        for count in (8, 2, 6, 1):
            previous = len(self.pages)
            self.pages = database_pages(count)
            self.write_source()
            with self.open() as (path, stats):
                self.assertEqual(path.read_bytes(), b"".join(self.pages))
                self.assertEqual(stats["removed_pages"], max(0, previous - count))
                self.assertEqual(sc.quick_check(path), "ok")

    def test_identity_reset_header_salt_path_force(self):
        _, _, first = self.baseline()
        identities = [first["source_identity"]]
        for kwargs in ({"header_byte": 1}, {"salt": b"fedcba9876543210"}):
            self.write_source(**kwargs)
            with self.open() as (_, stats):
                self.assertTrue(stats["rebuilt"])
                self.assertEqual(stats["decrypted_pages"], 4)
                self.assertNotIn(stats["source_identity"], identities)
                identities.append(stats["source_identity"])
        replacement = self.root / "another.db"
        replacement.write_bytes(self.src.read_bytes())
        self.src = replacement
        with self.open() as (_, stats):
            self.assertTrue(stats["rebuilt"])
            self.assertNotIn(stats["source_identity"], identities)
            identity = stats["source_identity"]
        with self.open(force_rebuild=True) as (_, stats):
            self.assertTrue(stats["rebuilt"])
            self.assertEqual(stats["source_identity"], identity)
            self.assertEqual(stats["decrypted_pages"], 4)

    def test_wrong_key_preserves_bytes_and_metadata(self):
        path, content, _ = self.baseline()
        meta = (self.cache / "metadata.sqlite3").read_bytes()
        with self.assertRaisesRegex(ValueError, "HMAC"):
            with dc.open_decrypted_cache(self.src, self.cache, bytes(32)):
                self.fail("wrong key accepted")
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual((self.cache / "metadata.sqlite3").read_bytes(), meta)

    def test_corrupt_plain_refused_and_force_repairs(self):
        path, content, _ = self.baseline()
        damaged = bytearray(content)
        damaged[2 * PAGE + 200] ^= 1
        path.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, "完整性"):
            with self.open():
                pass
        self.assertEqual(path.read_bytes(), damaged)
        with self.open(force_rebuild=True) as (path, stats):
            self.assertEqual(path.read_bytes(), content)
            self.assertTrue(stats["rebuilt"])

    def test_corrupt_size_force_failure_restores_partial_tail(self):
        path, content, _ = self.baseline()
        for damaged in (content + b"partial-tail", content[:-23]):
            path.write_bytes(damaged)
            with self.assertRaisesRegex(ValueError, "长度"):
                with self.open():
                    pass
            with patch.object(sc, "quick_check", return_value="bad"):
                with self.assertRaisesRegex(ValueError, "quick_check"):
                    with self.open(force_rebuild=True):
                        pass
            self.assertEqual(path.read_bytes(), damaged)
            with self.open(force_rebuild=True):
                pass
            self.assertEqual(path.read_bytes(), content)

    def test_hmac_and_quickcheck_failure_keep_baseline(self):
        path, content, _ = self.baseline()
        original = self.src.read_bytes()
        damaged = bytearray(original)
        damaged[sc.EXT_HEADER + 2 * PAGE + 40] ^= 1
        self.src.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, "HMAC"):
            with self.open():
                pass
        self.assertEqual(path.read_bytes(), content)
        self.src.write_bytes(original)
        self.pages[2] = data_page(52)
        self.write_source()
        with patch.object(sc, "quick_check", return_value="malformed"):
            with self.assertRaisesRegex(ValueError, "quick_check"):
                with self.open():
                    pass
        self.assertEqual(path.read_bytes(), content)
        with self.open() as (_, stats):
            self.assertEqual(stats["decrypted_pages"], 1)

    def test_source_mutation_replacement_sidecar_detected(self):
        path, content, _ = self.baseline()
        original = self.src.read_bytes()
        for mode in ("append", "replace", "sidecar", "mtime-restored"):
            self.src.write_bytes(original)
            def mutate(done, total):
                if done != total:
                    return
                if mode == "append":
                    with self.src.open("ab") as fh:
                        fh.write(b"x")
                elif mode == "replace":
                    new = self.root / "replacement"
                    new.write_bytes(original)
                    os.replace(new, self.src)
                elif mode == "sidecar":
                    Path(str(self.src) + "-wal").write_bytes(b"new wal")
                else:
                    before = self.src.stat()
                    with self.src.open("r+b") as fh:
                        fh.seek(sc.EXT_HEADER + PAGE + 10)
                        fh.write(b"different")
                    os.utime(self.src, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.subTest(mode=mode), self.assertRaises((ValueError, PermissionError)) as raised:
                with self.open(progress=mutate):
                    pass
            if isinstance(raised.exception, PermissionError):
                # Windows blocks replacement of our open source handle itself;
                # POSIX permits rename and reaches the explicit inode check.
                self.assertEqual((os.name, mode), ("nt", "replace"))
            else:
                self.assertRegex(str(raised.exception), "变化|替换")
            self.assertEqual(path.read_bytes(), content)

    def test_source_mutation_after_apply_rolls_back(self):
        path, content, _ = self.baseline()
        self.pages[2] = data_page(42)
        self.write_source()
        def fault(name):
            if name == "plain_synced":
                Path(str(self.src) + "-shm").write_bytes(b"change")
        with patch.object(dc, "_checkpoint", side_effect=fault):
            with self.assertRaisesRegex(ValueError, "变化"):
                with self.open():
                    pass
        self.assertEqual(path.read_bytes(), content)

    def test_stable_wal_is_not_merged(self):
        from test_wal import wal_bytes
        Path(str(self.src) + "-wal").write_bytes(wal_bytes([(1, 1, encrypt_page(plain_page1(99), 1))]))
        path, content, stats = self.baseline()
        self.assertEqual(content, b"".join(self.pages))
        self.assertEqual(stats["wal_pages"], 0)

    def test_consumer_failure_does_not_rollback(self):
        self.baseline()
        self.pages[2] = data_page(22)
        self.write_source()
        with self.assertRaisesRegex(RuntimeError, "consumer"):
            with self.open():
                raise RuntimeError("consumer")
        with self.open() as (path, stats):
            self.assertEqual(stats["decrypted_pages"], 0)
            self.assertEqual(path.read_bytes(), b"".join(self.pages))

    def test_exception_fault_boundaries_restore_baseline(self):
        path, content, _ = self.baseline()
        self.pages = database_pages(1, version=9)
        self.write_source()
        for stage in ("prepared", "applied_page", "truncated", "plain_synced", "before_commit"):
            def fail(name):
                if name == stage:
                    raise OSError("injected")
            with self.subTest(stage=stage), patch.object(dc, "_checkpoint", side_effect=fail):
                with self.assertRaisesRegex(OSError, "injected"):
                    with self.open():
                        pass
            self.assertEqual(path.read_bytes(), content)
        with self.open() as (path, stats):
            self.assertEqual(path.read_bytes(), b"".join(self.pages))
            self.assertEqual(stats["removed_pages"], 3)

    def test_process_kill_recovery_and_commit_boundaries(self):
        path, content, _ = self.baseline()
        original = self.src.read_bytes()
        for stage in ("prepared", "applied_page", "truncated", "plain_synced", "before_commit", "committed"):
            self.pages = database_pages(1, version=17)
            self.write_source()
            marker = self.root / (stage + ".marker")
            body = (f"marker=Path({str(marker)!r})\n"
                    "def fault(name):\n"
                    f" if name == {stage!r}:\n"
                    "  marker.write_text('ready')\n"
                    "  while True: time.sleep(1)\n"
                    "dc._checkpoint=fault\n"
                    "with dc.open_decrypted_cache(src,cache,key): pass\n")
            child = self.child(body)
            self.wait_marker(marker, child)
            child.kill()
            child.wait(timeout=10)
            # Verify the baseline BEFORE a new source update can hide recovery bugs.
            with dc._lock(self.cache / "cache.lock"):
                conn = dc._connect(self.cache / "metadata.sqlite3")
                try:
                    recovered = dc._recover(conn, path)
                finally:
                    conn.close()
            self.assertEqual(recovered, stage != "committed")
            self.assertEqual(path.read_bytes(), b"".join(self.pages) if stage == "committed" else content)
            self.src.write_bytes(original)
            with self.open():
                pass

    def test_killed_recovery_is_idempotent(self):
        path, content, _ = self.baseline()
        self.pages = database_pages(1, version=4)
        self.write_source()
        first_marker = self.root / "first.marker"
        child = self.child(f"def fault(name):\n if name=='truncated':\n  Path({str(first_marker)!r}).write_text('x')\n  while True: time.sleep(1)\ndc._checkpoint=fault\nwith dc.open_decrypted_cache(src,cache,key): pass\n")
        self.wait_marker(first_marker, child)
        child.kill()
        child.wait(timeout=10)
        recovery_marker = self.root / "recovery.marker"
        child = self.child(f"def fault(name):\n if name=='recovery_page':\n  Path({str(recovery_marker)!r}).write_text('x')\n  while True: time.sleep(1)\ndc._checkpoint=fault\nwith dc.open_decrypted_cache(src,cache,key): pass\n")
        self.wait_marker(recovery_marker, child)
        child.kill()
        child.wait(timeout=10)
        conn = dc._connect(self.cache / "metadata.sqlite3")
        try:
            self.assertTrue(dc._recover(conn, path))
        finally:
            conn.close()
        self.assertEqual(path.read_bytes(), content)

    def test_lock_covers_yield_across_processes(self):
        marker = self.root / "entered.marker"
        with self.open() as (path, _):
            child = self.child(f"with dc.open_decrypted_cache(src,cache,key):\n Path({str(marker)!r}).write_text('entered')\n")
            time.sleep(0.3)
            self.assertIsNone(child.poll())
            self.assertFalse(marker.exists())
            self.assertEqual(path.read_bytes(), b"".join(self.pages))
        self.wait_marker(marker, child)
        self.assertEqual(child.wait(timeout=10), 0)

    def test_space_scales_with_changed_pages(self):
        self.pages = database_pages(400)
        self.write_source()
        self.baseline()
        metadata = self.cache / "metadata.sqlite3"
        self.assertLess(metadata.stat().st_size, 160 * 1024)
        self.pages[100] = data_page(98)
        self.write_source()
        sizes = []
        def measure(name):
            if name == "prepared":
                sizes.append(metadata.stat().st_size)
        with patch.object(dc, "_checkpoint", side_effect=measure):
            with self.open() as (_, stats):
                self.assertEqual(stats["written_bytes"], PAGE)
                self.assertEqual(stats["journal_bytes"], 2 * PAGE)
        self.assertLess(max(sizes), 180 * 1024)
        self.assertLess(metadata.stat().st_size, 160 * 1024)

    def test_path_alias_and_sidecar_protection(self):
        self.cache.mkdir()
        original = self.src.read_bytes()
        for name in ("plain.db", "metadata.sqlite3", "cache.lock", "metadata.sqlite3-journal"):
            alias = self.cache / name
            os.link(self.src, alias)
            with self.assertRaisesRegex(ValueError, "链接|覆盖"):
                with self.open():
                    pass
            self.assertEqual(self.src.read_bytes(), original)
            alias.unlink()
        sidecar = self.cache / "plain.db-wal"
        sidecar.write_bytes(b"active")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            with self.open():
                pass

    def test_rekey_rebuild_keeps_source_lineage(self):
        import test_wal
        _, _, before = self.baseline()
        replacement_key = bytes(reversed(KEY))
        with patch.object(test_wal, "KEY", replacement_key):
            self.write_source()
        with dc.open_decrypted_cache(self.src, self.cache, replacement_key) as (path, stats):
            self.assertTrue(stats["rebuilt"])
            self.assertEqual(stats["decrypted_pages"], 4)
            self.assertEqual(stats["source_identity"], before["source_identity"])
            self.assertEqual(path.read_bytes(), b"".join(self.pages))

    def test_wal_mode_immutable_consumer_leaves_cache_reusable(self):
        first = bytearray(self.pages[0])
        first[18:20] = b"\x02\x02"
        self.pages[0] = bytes(first)
        self.write_source()
        with self.open() as (path, _):
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                conn.close()
        with self.open() as (_, stats):
            self.assertEqual(stats["decrypted_pages"], 0)
        self.assertFalse(Path(str(path) + "-wal").exists())

    def test_initial_failure_removes_only_new_plaintext(self):
        with patch.object(sc, "quick_check", return_value="bad"):
            with self.assertRaisesRegex(ValueError, "quick_check"):
                with self.open():
                    pass
        self.assertFalse((self.cache / "plain.db").exists())
        with self.open() as (_, stats):
            self.assertTrue(stats["rebuilt"])

    def test_header_reserved_byte_fallback_hash_is_reusable(self):
        self.pages = database_pages(1)
        first = bytearray(self.pages[0])
        first[20] = 0
        self.pages[0] = bytes(first)
        self.write_source()
        real_check = sc.quick_check
        calls = 0
        def check(path):
            nonlocal calls
            calls += 1
            return "synthetic mismatch" if calls == 1 else real_check(path)
        with patch.object(sc, "quick_check", side_effect=check):
            with self.open() as (path, _):
                self.assertEqual(path.read_bytes()[20], RESERVE)
        with self.open() as (_, stats):
            self.assertEqual(stats["decrypted_pages"], 0)

    def test_committed_exception_keeps_new_baseline(self):
        self.baseline()
        self.pages[2] = data_page(12)
        self.write_source()
        def fail(name):
            if name == "committed":
                raise OSError("commit already succeeded")
        with patch.object(dc, "_checkpoint", side_effect=fail):
            with self.assertRaisesRegex(OSError, "succeeded"):
                with self.open():
                    pass
        with self.open() as (path, stats):
            self.assertEqual(stats["decrypted_pages"], 0)
            self.assertEqual(path.read_bytes(), b"".join(self.pages))

    def test_plain_fsync_failure_preserves_baseline(self):
        path, baseline, _ = self.baseline()
        self.pages[2] = data_page(95)
        self.write_source()
        original_sync = dc._sync
        calls = 0
        def failing_sync(fh):
            nonlocal calls
            calls += 1
            original_sync(fh)
            if calls == 1:
                raise OSError("fsync failure")
        with patch.object(dc, "_sync", side_effect=failing_sync):
            with self.assertRaisesRegex(OSError, "fsync failure"):
                with self.open():
                    pass
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(path.read_bytes(), baseline)
        with self.open() as (_, stats):
            self.assertEqual(stats["decrypted_pages"], 1)

    def test_sqlite_commit_failure_before_and_after_commit(self):
        path, baseline, _ = self.baseline()
        original_source = self.src.read_bytes()
        original_connect = dc._connect
        for did_commit in (False, True):
            self.pages[2] = data_page(96)
            self.write_source()
            armed = False
            class ConnectionProxy:
                def __init__(self, inner):
                    self.inner = inner
                def __getattr__(self, name):
                    return getattr(self.inner, name)
                def __enter__(self):
                    self.inner.__enter__()
                    return self
                def __exit__(self, *args):
                    nonlocal armed
                    if armed:
                        armed = False
                        if did_commit:
                            self.inner.__exit__(*args)
                        raise OSError("ambiguous commit")
                    return self.inner.__exit__(*args)
            def checkpoint(name):
                nonlocal armed
                if name == "before_commit":
                    armed = True
            with patch.object(dc, "_connect", side_effect=lambda p: ConnectionProxy(original_connect(p))), \
                 patch.object(dc, "_checkpoint", side_effect=checkpoint):
                with self.assertRaisesRegex(OSError, "ambiguous commit"):
                    with self.open():
                        pass
            expected = b"".join(self.pages) if did_commit else baseline
            self.assertEqual(path.read_bytes(), expected)
            with self.open() as (_, stats):
                self.assertEqual(stats["decrypted_pages"], 0 if did_commit else 1)
            self.src.write_bytes(original_source)
            with self.open():
                pass

    def test_metadata_does_not_store_key(self):
        self.baseline()
        meta = (self.cache / "metadata.sqlite3").read_bytes()
        self.assertNotIn(KEY, meta)
        self.assertNotIn(KEY.hex().encode(), meta)
        conn = sqlite3.connect(self.cache / "metadata.sqlite3")
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM changes").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA auto_vacuum").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
