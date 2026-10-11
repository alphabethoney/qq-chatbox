"""Synthetic offline tests: python tests/test_export_resume.py."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import export_large as exporter


class ExportResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="qq-export-resume-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "fixture.db"
        self.out = self.root / "out"
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            cols = ",".join(f'"{c}" ' + ("BLOB" if c == "40800" else "INTEGER") for c in exporter.MSG_COLS)
            conn.execute(f"CREATE TABLE c2c_msg_table ({cols})")
            for number, peer in enumerate((21, 22, 23), 1):
                conn.execute("INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (number, 1700000000 + number, 1, "u11", 11, f"u{peer}", peer, 2, 0, b""))
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def run_export(self, *extra):
        argv = ["export_large", "--db", str(self.db), "--out", str(self.out), "--resume", *extra]
        with patch.object(sys, "argv", argv):
            return exporter.main()

    def checkpoint(self):
        return json.loads((self.out / ".export-checkpoint.json").read_text(encoding="utf-8"))

    def index(self):
        return json.loads((self.out / "json" / "index.json").read_text(encoding="utf-8"))

    def interrupt_after_first(self):
        real_write = exporter._atomic_text

        def write(path, text):
            if Path(path).parent.name == "json" and Path(path).name.startswith("0001_"):
                raise KeyboardInterrupt("synthetic interruption")
            real_write(path, text)

        with patch.object(exporter, "_atomic_text", side_effect=write), self.assertRaises(KeyboardInterrupt):
            self.run_export()
        self.assertEqual(set(self.checkpoint()["completed"]), {"c2c:21"})
        self.assertFalse(self.checkpoint()["complete"])

    def test_resume_skips_valid_sessions_but_scans_all_rows_and_builds_full_index(self):
        self.interrupt_after_first()
        with patch.object(exporter, "_atomic_text", wraps=exporter._atomic_text) as writes, patch.object(exporter, "_lean", wraps=exporter._lean) as lean:
            self.assertEqual(self.run_export(), 0)
        self.assertEqual(lean.call_count, 3)
        paths = [Path(call.args[0]) for call in writes.call_args_list]
        self.assertFalse(any(path.name.startswith("0000_") for path in paths))
        self.assertTrue(any(path.name.startswith("0001_") for path in paths))
        self.assertEqual(len(self.index()["conversations"]), 3)
        self.assertEqual(self.index()["message_count"], 3)
        self.assertTrue(self.index()["export"]["complete"])
        self.assertTrue(self.checkpoint()["complete"])
        self.assertEqual(len(self.checkpoint()["completed"]), 3)

    def test_missing_or_corrupt_artifact_rewrites_whole_session(self):
        for mode in ("missing", "corrupt"):
            with self.subTest(mode=mode):
                self.run_export()
                record = self.checkpoint()["completed"]["c2c:21"]
                path = self.out / next(name for name in record["artifacts"] if name.startswith("txt/"))
                if mode == "missing":
                    path.unlink()
                else:
                    path.write_text("corrupted", encoding="utf-8")
                with patch.object(exporter, "_atomic_text", wraps=exporter._atomic_text) as writes:
                    self.run_export()
                session_writes = [Path(call.args[0]) for call in writes.call_args_list if Path(call.args[0]).name.startswith("0000_")]
                self.assertEqual(len(session_writes), 2)
                self.assertNotEqual(path.read_text(encoding="utf-8"), "corrupted")

    def test_source_content_change_rejected_even_if_size_and_mtime_restored(self):
        self.run_export()
        before = self.db.stat()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE c2c_msg_table SET "40030"=99 WHERE "40001"=1')
        os.utime(self.db, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.db.stat().st_size, before.st_size)
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.run_export()

    def test_wal_creation_is_source_change(self):
        self.run_export()
        Path(str(self.db) + "-wal").write_bytes(b"synthetic changed WAL")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.run_export()

    def test_parameter_changes_are_rejected_before_scan(self):
        self.run_export()
        for args in (("--strict",), ("--since", "1700000001.5"), ("--format", "json")):
            with self.subTest(args=args), patch.object(exporter, "read_messages", side_effect=AssertionError("must validate before scanning")):
                with self.assertRaisesRegex(ValueError, "参数"):
                    self.run_export(*args)

    def test_name_mapping_changes_are_rejected(self):
        names = self.root / "names.json"
        names.write_text('{"21":"First"}', encoding="utf-8")
        self.run_export("--names", str(names))
        names.write_text('{"21":"Other"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "参数"):
            self.run_export("--names", str(names))

    def test_tolerant_bad_pages_never_become_completed_sessions(self):
        with patch.object(exporter, "read_messages", side_effect=sqlite3.DatabaseError("synthetic bad page")):
            self.run_export()
        self.assertFalse(self.index()["export"]["complete"])
        self.assertTrue(self.index()["export"]["errors"])
        self.assertFalse(self.checkpoint()["scan_complete"])
        self.assertFalse(self.checkpoint()["complete"])
        self.assertEqual(self.checkpoint()["completed"], {})
        self.run_export()
        self.assertTrue(self.index()["export"]["complete"])
        self.assertEqual(self.index()["message_count"], 3)

    def test_partial_bad_page_scan_does_not_reuse_previous_completed_artifacts(self):
        self.run_export()
        real_read = exporter.read_messages

        def read(conn, table, **kwargs):
            for row in real_read(conn, table, **kwargs):
                yield row
                raise sqlite3.DatabaseError("bad after first row")

        with patch.object(exporter, "read_messages", side_effect=read):
            self.run_export()
        self.assertFalse(self.index()["export"]["complete"])
        self.assertEqual(self.checkpoint()["completed"], {})
        with patch.object(exporter, "_atomic_text", wraps=exporter._atomic_text) as writes:
            self.run_export()
        self.assertEqual(sum(Path(call.args[0]).name.startswith("0000_") for call in writes.call_args_list), 2)
        self.assertEqual(self.index()["message_count"], 3)

    def test_strict_failure_never_publishes_success(self):
        with patch.object(exporter, "read_messages", side_effect=sqlite3.DatabaseError("synthetic bad page")):
            with self.assertRaises(sqlite3.DatabaseError):
                self.run_export("--strict")
        self.assertFalse(self.checkpoint()["complete"])
        self.assertFalse((self.out / "json" / "index.json").exists())

    def test_fractional_since_contract_and_complete_resume(self):
        self.run_export("--strict", "--since", "1700000001.5", "--format", "json")
        data = self.index()
        self.assertEqual(data["message_count"], 2)
        self.assertEqual(data["export"]["since"], 1700000001.5)
        self.assertEqual(data["export"]["last_msg_time"], 1700000003)
        with patch.object(exporter, "_atomic_text", wraps=exporter._atomic_text) as writes:
            self.run_export("--strict", "--since", "1700000001.5", "--format", "json")
        self.assertFalse(any(Path(call.args[0]).name.startswith("000") for call in writes.call_args_list))
        self.assertEqual(self.index(), data)

    def test_checkpoint_replace_failure_keeps_previous_json(self):
        self.run_export()
        path = self.out / ".export-checkpoint.json"
        before = path.read_bytes()
        with patch.object(exporter.os, "replace", side_effect=OSError("simulated atomic publish failure")):
            with self.assertRaises(OSError):
                exporter._save_checkpoint(path, {"invalid": True})
        self.assertEqual(path.read_bytes(), before)

    def test_source_changes_during_export_prevent_index_commit(self):
        real_fingerprint = exporter._source_fingerprint
        calls = 0

        def fingerprint(path):
            nonlocal calls
            calls += 1
            result = real_fingerprint(path)
            if calls == 3:
                result["files"][""]["sha256"] = "changed"
            return result

        with patch.object(exporter, "_source_fingerprint", side_effect=fingerprint):
            with self.assertRaisesRegex(RuntimeError, "导出期间变化"):
                self.run_export()
        self.assertFalse(self.checkpoint()["complete"])
        self.assertFalse((self.out / "json" / "index.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
