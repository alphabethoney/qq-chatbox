"""Synthetic scoped acceptance and cache rollback; no real QQ data."""
from pathlib import Path
import sqlite3
import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_wal import KEY, PAGE, RESERVE, plain_page1, encrypt_page, wal_bytes
from qq_exporter import sqlcipher as sc, decrypt_cache as dc
from qq_exporter import integrity


class ScopedIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.root = Path(self.tmp.name)
        self.src = self.root / 'encrypted.db'
        self.dst = self.root / 'output.db'
        self.cache = self.root / 'cache'
        plain = self.root / 'fixture.db'
        plain.write_bytes(plain_page1())
        with sqlite3.connect(plain) as conn:
            conn.executescript("CREATE TABLE c2c_msg_table(peer INTEGER, body TEXT);"
                               "CREATE INDEX c2c_peer ON c2c_msg_table(peer);"
                               "INSERT INTO c2c_msg_table VALUES(7,'hello');"
                               "CREATE TABLE group_msg_table(body TEXT);"
                               "INSERT INTO group_msg_table VALUES('group');")
            self.roots = dict(conn.execute("SELECT name,rootpage FROM sqlite_schema"))
        conn.close()
        data = plain.read_bytes()
        self.pages = [data[i:i+PAGE] for i in range(0, len(data), PAGE)]
        self.write_source()

    def tearDown(self):
        self.tmp.cleanup()

    def write_source(self, zero=()):
        self.src.write_bytes(b'SQLite header 3\0' + bytes(sc.EXT_HEADER-16) + b''.join(
            bytes(PAGE) if i in zero else encrypt_page(p, i)
            for i, p in enumerate(self.pages, 1)))

    def decrypt(self, **kw):
        return sc.decrypt_database(self.src, self.dst, KEY, integrity_mode='selected', conversation='c2c:7', **kw)

    def cache_open(self, **kw):
        return dc.open_decrypted_cache(self.src, self.cache, KEY, integrity_mode='selected', conversation=kw.pop('conversation', 'c2c:7'), **kw)

    def test_unrelated_zero_and_reuse_complete_report(self):
        pg = self.roots['group_msg_table']
        self.write_source(zero=[pg])
        report = self.decrypt()
        self.assertTrue(report['accepted'])
        self.assertFalse(report['full_integrity_ok'])
        self.assertTrue(report['selected_integrity_ok'])
        self.assertFalse(report['hmac_verified'])
        self.assertTrue(report['nonzero_pages_hmac_verified'])
        self.assertEqual(report['zero_pages'], [pg])
        self.assertEqual(report['selected_integrity']['indexes'], ['c2c_peer'])
        self.assertEqual(self.dst.read_bytes()[(pg-1)*PAGE:pg*PAGE], bytes(PAGE))
        for _ in range(2):
            with self.cache_open() as (_, result):
                self.assertEqual(result['zero_pages'], [pg])
                self.assertTrue(result['accepted'])
                self.assertFalse(result['hmac_verified'])
        self.assertEqual(result['decrypted_pages'], 0)

    def test_target_table_and_index_corruption_reject(self):
        for name in ('c2c_msg_table', 'c2c_peer'):
            with self.subTest(name=name):
                self.write_source(zero=[self.roots[name]])
                with self.assertRaisesRegex(ValueError, 'selected integrity'):
                    self.decrypt()
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    with self.cache_open():
                        pass
                self.assertFalse((self.cache / 'plain.db').exists())

    def test_zero_first_truncation_nonzero_hmac_fail(self):
        original = self.src.read_bytes()
        for mode in ('firstzero', 'truncated', 'nonzero'):
            data = bytearray(original)
            if mode == 'firstzero':
                data[sc.EXT_HEADER:sc.EXT_HEADER+PAGE] = bytes(PAGE)
            elif mode == 'truncated':
                del data[-1]
            else:
                data[sc.EXT_HEADER + (self.roots['group_msg_table']-1)*PAGE + 20] ^= 1
            self.src.write_bytes(data)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.decrypt()
            with self.assertRaises(ValueError):
                with self.cache_open():
                    pass

    def test_explicit_policy_and_full_diagnostic_compatibility(self):
        with self.assertRaisesRegex(ValueError, 'verify_hmac'):
            self.decrypt(verify_hmac=False)
        for conversation in (None, 'group:7', 'c2c:', 'c2c: bad'):
            with self.assertRaises(ValueError):
                integrity.validate_policy('selected', conversation)
        self.assertEqual(integrity.validate_policy('selected', 'c2c:u_example')['conversation'], 'c2c:u_example')
        self.assertEqual(integrity.validate_policy('selected', 'c2c:0007')['conversation'], 'c2c:7')
        pg = self.roots['group_msg_table']
        self.pages[pg-1] = bytes(PAGE)  # authenticated invalid SQLite page
        self.write_source()
        full = sc.decrypt_database(self.src, self.dst, KEY)
        self.assertFalse(full['ok'])
        self.assertTrue(self.dst.exists())
        self.assertTrue(self.decrypt()['accepted'])

    def test_policy_change_requires_rebuild_and_failure_rolls_back(self):
        with self.cache_open() as (path, _):
            baseline = path.read_bytes()
        for kw in ({'conversation': 'c2c:8'},):
            with self.assertRaisesRegex(ValueError, 'force_rebuild'):
                with self.cache_open(**kw):
                    pass
        with self.assertRaisesRegex(ValueError, 'force_rebuild'):
            with dc.open_decrypted_cache(self.src, self.cache, KEY):
                pass
        with patch.object(integrity, 'selected_check', return_value={'ok': False}):
            with self.assertRaises(ValueError):
                with self.cache_open(conversation='c2c:8', force_rebuild=True):
                    pass
        self.assertEqual(path.read_bytes(), baseline)
        with self.cache_open() as (_, report):
            self.assertFalse(report['rebuilt'])
        with self.cache_open(conversation='c2c:8', force_rebuild=True) as (_, report):
            self.assertTrue(report['rebuilt'])

    def test_stable_source_identity_across_snapshot_paths(self):
        original = self.root / 'original.db'
        with self.cache_open(source_identity_path=original) as (_, report):
            identity = report['source_identity']
        relocated = self.root / 'snapshot2.db'
        relocated.write_bytes(self.src.read_bytes())
        self.src = relocated
        with self.cache_open(source_identity_path=original) as (_, report):
            self.assertFalse(report['rebuilt'])
            self.assertEqual(report['source_identity'], identity)
        self.assertEqual(self.decrypt(source_identity_path=original)['source_identity'], identity)

    def test_killed_policy_rebuild_recovers_old_baseline(self):
        with self.cache_open() as (path, _):
            baseline = path.read_bytes()
        marker = self.root / 'prepared-marker'
        script = f'''from pathlib import Path
import time
from qq_exporter import decrypt_cache as dc
def checkpoint(name):
    if name == 'applied_page':
        Path({str(marker)!r}).write_text('ready')
        time.sleep(60)
dc._checkpoint = checkpoint
with dc.open_decrypted_cache(Path({str(self.src)!r}), Path({str(self.cache)!r}), bytes(range(32)), integrity_mode='selected', conversation='c2c:8', force_rebuild=True):
    pass
'''
        child = subprocess.Popen([sys.executable, '-c', script], cwd=Path(__file__).resolve().parent.parent,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(.02)
            self.assertTrue(marker.exists())
        finally:
            child.kill()
            child.wait(timeout=10)
        with self.cache_open() as (_, report):
            self.assertTrue(report['recovered'])
            self.assertEqual(path.read_bytes(), baseline)
            self.assertEqual(report['conversation'], 'c2c:7')

    def test_selected_wal_zero_and_nonzero_hmac(self):
        pg = self.roots['group_msg_table']
        wal = Path(str(self.src) + '-wal')
        wal.write_bytes(wal_bytes([(pg, len(self.pages), bytes(PAGE))]))
        report = self.decrypt()
        self.assertEqual(report['zero_pages'], [pg])
        self.assertTrue(report['accepted'])
        self.assertFalse(report['hmac_verified'])
        corrupted = bytearray(encrypt_page(self.pages[pg-1], pg))
        corrupted[20] ^= 1
        wal.write_bytes(wal_bytes([(pg, len(self.pages), bytes(corrupted))]))
        with self.assertRaisesRegex(ValueError, 'HMAC'):
            self.decrypt()
        wal.write_bytes(wal_bytes([(1, len(self.pages), bytes(PAGE))]))
        with self.assertRaises(ValueError):
            self.decrypt()

    def test_metadata_traversal_does_not_return_body_values(self):
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(integrity.sqlite3, 'connect', side_effect=traced):
            report = self.decrypt()
        scans = [sql for sql in statements if 'NOT INDEXED' in sql]
        self.assertEqual(len(scans), 1)
        self.assertNotIn('SELECT *', scans[0])
        self.assertIn('typeof("body")', scans[0])
        self.assertIn('length(CAST("body" AS BLOB))', scans[0])
        self.assertEqual(report['selected_integrity']['traversal'], 'storage-metadata')
        self.assertEqual(report['selected_integrity']['rows_read'], 1)

    def test_policy_version_mismatch_and_target_failure_preserve_baseline(self):
        with self.cache_open() as (path, _):
            baseline = path.read_bytes()
        self.write_source(zero=[self.roots['c2c_msg_table']])
        with self.assertRaises(ValueError):
            with self.cache_open():
                pass
        self.assertEqual(path.read_bytes(), baseline)
        self.write_source()
        conn = sqlite3.connect(self.cache / 'metadata.sqlite3')
        try:
            state = json.loads(conn.execute('SELECT value FROM state').fetchone()[0])
            state['validation_version'] = 0
            conn.execute('UPDATE state SET value=?', (json.dumps(state),))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(ValueError, 'force_rebuild'):
            with self.cache_open():
                pass
        self.assertEqual(path.read_bytes(), baseline)
        with self.cache_open(force_rebuild=True) as (_, report):
            self.assertTrue(report['rebuilt'])
            self.assertEqual(report['validation_version'], integrity.VALIDATION_VERSION)

    def test_lock_byte_page_is_mathematical_diagnostic_only(self):
        # SQLite PENDING_BYTE is 0x40000000 in the decrypted file. This does
        # not excuse any zero page: the codec has no page-number exception.
        self.assertEqual(0x40000000 // PAGE + 1, 262145)
        self.assertNotEqual(262144, 0x40000000 // PAGE + 1)


if __name__ == '__main__':
    unittest.main()
