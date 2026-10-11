"""Offline synthetic SQLCipher/WAL tests; no real QQ format compatibility claim.

Run: python -m unittest discover -s tests -p test_wal.py -v
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from qq_exporter import sqlcipher as sc

KEY = bytes(range(32))
SALT = b"0123456789abcdef"
RESERVE = 48
PAGE = 4096


def plain_page1(version: int = 0, pages: int = 1) -> bytes:
    """An actual empty SQLite database, with space reserved for the codec."""
    page = bytearray(PAGE)
    page[:16] = b"SQLite format 3\x00"
    page[16:18] = struct.pack(">H", PAGE)
    page[18:24] = bytes((1, 1, RESERVE, 64, 32, 32))
    for offset, value in ((24, 1), (28, pages), (40, 1), (44, 4),
                          (56, 1), (60, version), (92, 1), (96, 3040001)):
        struct.pack_into(">I", page, offset, value)
    page[100] = 13
    struct.pack_into(">H", page, 105, PAGE - RESERVE)
    return bytes(page)


def data_page(value: int) -> bytes:
    return bytes([value]) * (PAGE - RESERVE) + bytes(RESERVE)


def encrypt_page(plain: bytes, pgno: int, salt: bytes = SALT) -> bytes:
    assert len(plain) == PAGE and len(salt) == 16
    skip = 16 if pgno == 1 else 0
    iv = bytes((pgno + i) % 256 for i in range(16))
    cipher = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    ciphertext = cipher.update(plain[skip:PAGE - RESERVE]) + cipher.finalize()
    # Deliberately derive independently of the implementation under test.
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, bytes(x ^ 0x3A for x in salt), 2, 32)
    mac = hmac.new(mac_key, ciphertext + iv + struct.pack("<I", pgno), hashlib.sha1).digest()
    return (salt if skip else b"") + ciphertext + iv + mac + bytes(12)


def checksum(data: bytes, state=(0, 0), endian="little"):
    """Independent reference, not the production checksum helper."""
    a, b = state
    for offset in range(0, len(data), 8):
        a = (a + int.from_bytes(data[offset:offset + 4], endian) + b) % 2**32
        b = (b + int.from_bytes(data[offset + 4:offset + 8], endian) + a) % 2**32
    return a, b


def wal_bytes(frames, endian="little", salts=(0x12345678, 0x98765432)) -> bytes:
    magic = 0x377F0682 if endian == "little" else 0x377F0683
    header = struct.pack(">6I", magic, 3007000, PAGE, 0, *salts)
    state = checksum(header, endian=endian)
    output = bytearray(header + struct.pack(">II", *state))
    for pgno, commit_size, encrypted in frames:
        prefix = struct.pack(">II", pgno, commit_size)
        state = checksum(prefix + encrypted, state, endian)
        output += prefix + struct.pack(">4I", *salts, *state) + encrypted
    return bytes(output)


class WalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "encrypted.db"
        self.dst = self.root / "output.db"
        self.wal = Path(str(self.src) + "-wal")
        self.write_db([plain_page1()])

    def tearDown(self):
        self.tmp.cleanup()

    def write_db(self, pages):
        header = b"SQLite header 3\x00" + bytes(sc.EXT_HEADER - 16)
        self.src.write_bytes(header + b"".join(encrypt_page(page, n) for n, page in enumerate(pages, 1)))

    def write_wal(self, frames, **kwargs):
        self.wal.write_bytes(wal_bytes([(n, size, encrypt_page(page, n))
                                       for n, size, page in frames], **kwargs))

    def assert_protected_failure(self, pattern=None, **kwargs):
        original = self.src.read_bytes()
        self.dst.write_bytes(b"previous complete output")
        with self.assertRaisesRegex(ValueError, pattern or "."):
            sc.decrypt_database(self.src, self.dst, KEY, **kwargs)
        self.assertEqual(self.src.read_bytes(), original)
        self.assertEqual(self.dst.read_bytes(), b"previous complete output")
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_complete_sqlite_roundtrip_and_hmac(self):
        stats = sc.decrypt_database(self.src, self.dst, KEY)
        self.assertTrue(stats["ok"], stats)
        self.assertTrue(stats["hmac_verified"])
        self.assertEqual(self.dst.read_bytes(), plain_page1())

    def test_wal_mode_header_and_uri_path_leave_no_sidecars(self):
        page = bytearray(plain_page1())
        page[18:20] = b"\x02\x02"
        self.write_db([bytes(page)])
        self.dst = self.root / "output #1.db"
        stats = sc.decrypt_database(self.src, self.dst, KEY)
        self.assertTrue(stats["ok"], stats)
        self.assertEqual(set(self.root.iterdir()), {self.src, self.dst})

    def test_checksum_endianness_and_uncommitted_tail(self):
        for endian in ("little", "big"):
            with self.subTest(endian=endian):
                self.write_wal([(1, 1, plain_page1(7)), (1, 0, plain_page1(99))], endian=endian)
                frames, size = sc.decrypt_wal(self.wal, KEY, RESERVE)
                self.assertEqual((frames, size), ([(1, plain_page1(7))], 1))
                stats = sc.decrypt_database(self.src, self.dst, KEY)
                self.assertTrue(stats["ok"], stats)
                self.assertEqual(stats["wal_pages"], 1)
                with closing(sqlite3.connect(self.dst)) as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_no_commit_does_not_apply(self):
        self.write_wal([(1, 0, plain_page1(8)), (2, 0, data_page(2))])
        self.assertEqual(sc.decrypt_wal(self.wal, KEY, RESERVE), ([], 0))
        stats = sc.decrypt_database(self.src, self.dst, KEY)
        self.assertEqual(stats["wal_pages"], 0)
        self.assertEqual(self.dst.read_bytes(), plain_page1())

    def test_transaction_prefix_and_repeated_page(self):
        self.write_db([plain_page1(pages=3), data_page(2), data_page(3)])
        self.write_wal([(2, 0, data_page(4)), (3, 3, data_page(5)),
                        (2, 3, data_page(6)), (3, 0, data_page(7))])
        with patch.object(sc, "quick_check", return_value="ok"):
            stats = sc.decrypt_database(self.src, self.dst, KEY)
        self.assertEqual(stats["wal_pages"], 3)
        self.assertEqual(self.dst.read_bytes(), plain_page1(pages=3) + data_page(6) + data_page(5))

    def test_commit_truncates_database(self):
        self.write_db([plain_page1(pages=3), data_page(2), data_page(3)])
        self.write_wal([(1, 1, plain_page1(12))])
        stats = sc.decrypt_database(self.src, self.dst, KEY)
        self.assertTrue(stats["ok"], stats)
        self.assertEqual(self.dst.read_bytes(), plain_page1(12))
        self.assertEqual(self.src.stat().st_size, sc.EXT_HEADER + 3 * PAGE)

    def test_intermediate_truncation_does_not_resurrect_pages(self):
        self.write_db([plain_page1(pages=3), data_page(2), data_page(3)])
        self.write_wal([(2, 3, data_page(9)), (1, 1, plain_page1()), (3, 3, data_page(7))])
        with patch.object(sc, "quick_check", return_value="ok"):
            sc.decrypt_database(self.src, self.dst, KEY)
        self.assertEqual(self.dst.read_bytes(), plain_page1() + bytes(PAGE) + data_page(7))
        frames, size = sc.decrypt_wal(self.wal, KEY, RESERVE)
        self.assertEqual(size, 3)
        self.assertNotIn(2, dict(frames))

    def test_header_checksum_rejected(self):
        self.write_wal([(1, 1, plain_page1())])
        data = bytearray(self.wal.read_bytes())
        data[12] ^= 1
        self.wal.write_bytes(data)
        self.assert_protected_failure("头部 checksum")

    def test_frame_checksum_rejected_after_valid_commit(self):
        self.write_wal([(1, 1, plain_page1(1)), (1, 1, plain_page1(2))])
        data = bytearray(self.wal.read_bytes())
        data[32 + 24 + PAGE + 24 + 100] ^= 1
        self.wal.write_bytes(data)
        self.assert_protected_failure("帧 checksum")

    def test_checksum_chain_is_not_reset_per_frame(self):
        page = encrypt_page(plain_page1(2), 1)
        self.write_wal([(1, 1, plain_page1(1)), (1, 1, plain_page1(2))])
        data = bytearray(self.wal.read_bytes())
        offset = 32 + 24 + PAGE
        incorrect = checksum(data[offset:offset + 8] + page)
        data[offset + 16:offset + 24] = struct.pack(">II", *incorrect)
        self.wal.write_bytes(data)
        self.assert_protected_failure("帧 checksum")

    def test_frame_salt_rejected(self):
        self.write_wal([(1, 1, plain_page1())])
        data = bytearray(self.wal.read_bytes())
        data[32 + 8] ^= 1
        self.wal.write_bytes(data)
        self.assert_protected_failure("salt")

    def test_sqlcipher_salt_mismatch_rejected(self):
        page = encrypt_page(plain_page1(), 1, salt=b"fedcba9876543210")
        self.wal.write_bytes(wal_bytes([(1, 1, page)]))
        self.assert_protected_failure("HMAC|salt")

    def test_hmac_detects_payload_with_recomputed_wal_checksum(self):
        page = bytearray(encrypt_page(plain_page1(), 1))
        page[200] ^= 1
        self.wal.write_bytes(wal_bytes([(1, 1, bytes(page))]))
        self.assert_protected_failure("HMAC")

    def test_invalid_headers_and_truncated_frames(self):
        good = wal_bytes([(1, 1, encrypt_page(plain_page1(), 1))])
        variants = [good[:12], good[:-1], good + b"torn frame", b"nope" + good[4:]]
        for field, value in ((4, 123), (8, 8192)):
            bad = bytearray(good)
            struct.pack_into(">I", bad, field, value)
            struct.pack_into(">II", bad, 24, *checksum(bad[:24]))
            variants.append(bytes(bad))
        for variant in variants:
            with self.subTest(length=len(variant), head=variant[:12]):
                self.wal.write_bytes(variant)
                self.assert_protected_failure()

    def test_invalid_page_number(self):
        for pgno in (0, 0xFFFFFFFF):
            self.wal.write_bytes(wal_bytes([(pgno, 1, encrypt_page(data_page(1), 2))]))
            self.assert_protected_failure("页号")

    def test_missing_empty_and_header_only_wal(self):
        self.assertEqual(sc.decrypt_wal(self.wal, KEY, RESERVE), ([], 0))
        for content in (b"", wal_bytes([])):
            self.wal.write_bytes(content)
            self.assertEqual(sc.decrypt_wal(self.wal, KEY, RESERVE), ([], 0))

    def test_database_bad_later_page_and_truncation(self):
        self.write_db([plain_page1(pages=2), data_page(2)])
        good = self.src.read_bytes()
        bad = bytearray(good)
        bad[sc.EXT_HEADER + PAGE + 100] ^= 1
        self.src.write_bytes(bad)
        self.assert_protected_failure("HMAC")
        self.src.write_bytes(good[:-1])
        self.assert_protected_failure("末页截断")

    def test_wrong_key_explicit_reserve_and_bad_layout(self):
        self.dst.write_bytes(b"keep")
        for reserve in (RESERVE, 0, 24):
            with self.assertRaises(ValueError):
                sc.decrypt_database(self.src, self.dst, bytes(32), reserve=reserve)
            self.assertEqual(self.dst.read_bytes(), b"keep")

    def test_same_source_wal_and_hardlink_destination_rejected(self):
        original = self.src.read_bytes()
        self.write_wal([(1, 1, plain_page1())])
        for target in (self.src, self.wal):
            with self.assertRaises(ValueError):
                sc.decrypt_database(self.src, target, KEY)
        os.link(self.src, self.dst)
        with self.assertRaises(ValueError):
            sc.decrypt_database(self.src, self.dst, KEY)
        self.assertEqual(self.src.read_bytes(), original)

    def test_progress_exception_cleans_output_and_preserves_previous(self):
        self.dst.write_bytes(b"keep")
        def fail(*args):
            raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            sc.decrypt_database(self.src, self.dst, KEY, progress=fail)
        self.assertEqual(self.dst.read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob("*.partial")), [])
        self.dst.unlink()
        with self.assertRaises(RuntimeError):
            sc.decrypt_database(self.src, self.dst, KEY, progress=fail)
        self.assertFalse(self.dst.exists())

    def test_source_mutation_detected(self):
        def mutate(*args):
            with self.src.open("ab") as fh:
                fh.write(b"change")
        self.dst.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            sc.decrypt_database(self.src, self.dst, KEY, progress=mutate)
        self.assertEqual(self.dst.read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_wal_mutation_detected(self):
        self.write_wal([(1, 1, plain_page1())])
        calls = 0
        def mutate(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.wal.open("ab") as fh:
                    fh.write(b"change")
        self.assert_protected_failure("发生变化", progress=mutate)

    def test_failed_integrity_is_never_success(self):
        with patch.object(sc, "quick_check", return_value="malformed"):
            stats = sc.decrypt_database(self.src, self.dst, KEY)
        self.assertFalse(stats["ok"])
        self.assertEqual(stats["integrity"], "malformed")
        self.assertEqual(self.dst.stat().st_size, PAGE)

    def test_explicit_hmac_opt_out_is_reported(self):
        bad = bytearray(self.src.read_bytes())
        bad[-RESERVE + 16] ^= 1
        self.src.write_bytes(bad)
        self.assert_protected_failure("HMAC")
        stats = sc.decrypt_database(self.src, self.dst, KEY, verify_hmac=False)
        self.assertTrue(stats["ok"])
        self.assertFalse(stats["hmac_verified"])

    def test_output_sidecar_rejected(self):
        Path(str(self.dst) + "-wal").write_bytes(b"active")
        self.assert_protected_failure("sidecar")

    def test_replace_failure_preserves_output_and_cleans_temp(self):
        self.dst.write_bytes(b"keep")
        with patch.object(sc.os, "replace", side_effect=OSError("replace failure")):
            with self.assertRaisesRegex(OSError, "replace failure"):
                sc.decrypt_database(self.src, self.dst, KEY)
        self.assertEqual(self.dst.read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_large_wal_streaming_memory_is_bounded(self):
        encrypted = encrypt_page(plain_page1(42), 1)
        self.wal.write_bytes(wal_bytes([(1, 1, encrypted)] * 1024))
        tracemalloc.start()
        try:
            with patch.object(Path, "read_bytes", side_effect=AssertionError("whole file read")), \
                 patch.object(sc, "decrypt_wal", side_effect=AssertionError("list WAL API")), \
                 patch.object(sc, "quick_check", return_value="ok"):
                stats = sc.decrypt_database(self.src, self.dst, KEY)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(stats["wal_pages"], 1024)
        self.assertLess(peak, 2 * 1024 * 1024, f"Python peak memory {peak}")
        self.assertEqual(self.dst.read_bytes(), plain_page1(42))

    def test_checksum_matches_sqlite_generated_wal(self):
        # Generated locally in a temporary directory, never a real user database.
        path = self.root / "sqlite-generated.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA page_size=4096")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t(value)")
            conn.commit()
            raw = Path(str(path) + "-wal").read_bytes()
            magic = int.from_bytes(raw[:4], "big")
            order = "<" if magic == 0x377F0682 else ">"
            state = sc._wal_checksum(raw[:24], (0, 0), order)
            self.assertEqual(state, struct.unpack(">II", raw[24:32]))
            for offset in range(32, len(raw), 24 + PAGE):
                frame = raw[offset:offset + 24 + PAGE]
                state = sc._wal_checksum(frame[:8] + frame[24:], state, order)
                self.assertEqual(state, struct.unpack(">II", frame[16:24]))


if __name__ == "__main__":
    unittest.main()
