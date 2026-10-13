"""Offline selective large-export regressions; only synthetic SQLite fixtures."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import export_large as exporter

T = 1700000000


class LargeSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="qq-large-selection-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "fixture.db"
        self.out = self.root / "out"
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            cols = ",".join(f'"{c}" ' + ("BLOB" if c == "40800" else "INTEGER") for c in exporter.MSG_COLS)
            for kind in ("c2c", "group"):
                conn.execute(f"CREATE TABLE {kind}_msg_table ({cols})")
                for column in ("40030", "40033", "40050"):
                    conn.execute(f'CREATE INDEX {kind}_{column} ON {kind}_msg_table("{column}")')
        self.stdout = io.StringIO()
        self.quiet = contextlib.redirect_stdout(self.stdout)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def add(self, mid, ts=T, *, kind="c2c", sender=11, peer=22, direction=1, body=b"good"):
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f"INSERT INTO {kind}_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (mid, ts, direction, f"u{sender}", sender, f"u{peer}", peer, 2, 0, body))

    def run_export(self, *args, out=None):
        with patch.object(sys, "argv", ["export_large", "--db", str(self.db), "--out", str(out or self.out), *args]):
            return exporter.main()

    def index(self):
        return json.loads((self.out / "json" / "index.json").read_text(encoding="utf-8"))

    def messages(self):
        return [m for entry in self.index()["conversations"]
                for m in json.loads((self.out / "json" / entry["file"]).read_text(encoding="utf-8"))["messages"]]

    def test_private_identity_does_not_read_group_rows(self):
        self.add(1, body=b"")
        original = sqlite3.connect
        def guarded(*args, **kwargs):
            conn = original(*args, **kwargs)
            def authorize(action, table, column, database, trigger):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'group_msg_table' else sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        with patch.object(sqlite3, 'connect', side_effect=guarded):
            self.assertEqual(self.run_export('--conversation', 'c2c:22', '--strict'), 0)
        self.assertEqual([m['msg_id'] for m in self.messages()], [1])

    def test_indexed_selection_skips_irrelevant_malformed_bodies(self):
        self.add(1, kind="group", peer=42)
        self.add(2, kind="group", peer=43, body=b"malformed")
        self.add(3, peer=22, body=b"malformed")
        def parse(blob):
            if blob != b"good":
                raise ValueError("irrelevant body parsed")
            return "selected", []
        with patch.object(exporter, "parse_body", side_effect=parse) as parser:
            self.run_export("--conversation", "group:42", "--strict")
        self.assertEqual(parser.call_count, 1)
        self.assertEqual([m["msg_id"] for m in self.messages()], [1])
        self.assertTrue(self.index()["export"]["complete"])

    def test_private_received_sent_and_other_kind_intersection(self):
        self.add(1)
        self.add(2, sender=22, peer=11, direction=0)
        self.add(3, peer=33)
        self.add(4, kind="group", peer=22)
        self.run_export("--conversation", "c2c:22", "--kind", "c2c", "--strict")
        self.assertEqual({m["msg_id"] for m in self.messages()}, {1, 2})
        self.assertEqual({m["direction"] for m in self.messages()}, {"in", "out"})
        self.assertEqual([c["id"] for c in self.index()["conversations"]], ["c2c:22"])

    def test_half_open_time_and_strict_since_intersection(self):
        for mid, ts in enumerate((T - 1, T, T * 1000 + 500, T + 1, T + 2), 1):
            self.add(mid, ts, kind="group", peer=42)
        self.run_export("--kind", "group", "--after", "2023-11-14T22:13:20Z",
                        "--before", str(T + 2), "--since", str(T), "--strict")
        self.assertEqual([m["time"] for m in self.messages()], [T + .5, T + 1])

    def test_resume_normalized_selection_and_parameter_mismatch(self):
        self.add(1, kind="group", peer=42)
        self.run_export("--resume", "--conversation", "group:42", "--after", str(T), "--self-qq", "11")
        self.run_export("--resume", "--conversation", "group:42", "--conversation", "group:42",
                        "--after", "2023-11-14T22:13:20+00:00", "--self-qq", "11")
        before = (self.out / ".export-checkpoint.json").read_bytes()
        for args in (("--after", str(T + 1), "--self-qq", "11"),
                     ("--after", str(T), "--self-qq", "12")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.run_export("--resume", "--conversation", "group:42", *args)
            self.assertEqual((self.out / ".export-checkpoint.json").read_bytes(), before)

    def test_legacy_checkpoint_compatible_only_without_selection(self):
        self.add(1)
        self.run_export("--resume")
        path = self.out / ".export-checkpoint.json"
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        checkpoint["parameters"].pop("selection")
        checkpoint["parameters"].pop("self_qq")
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        self.run_export("--resume")
        with self.assertRaises(ValueError):
            self.run_export("--resume", "--kind", "c2c")

    def test_output_scope_rejects_legacy_or_changed_selection_without_deletion(self):
        self.add(1, kind="group", peer=42)
        self.run_export()
        before = {p.relative_to(self.out): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        with self.assertRaises(ValueError):
            self.run_export("--conversation", "group:42")
        self.assertEqual(before, {p.relative_to(self.out): p.read_bytes() for p in self.out.rglob("*") if p.is_file()})
        selected_out = self.root / "selected"
        self.run_export("--conversation", "group:42", out=selected_out)
        for args in (("--conversation", "group:43"), ()):
            with self.assertRaises(ValueError):
                self.run_export(*args, out=selected_out)

    def test_selected_output_format_change_rejected_without_mutation(self):
        self.add(1, kind="group", peer=42)
        self.run_export("--conversation", "group:42", "--format", "json", "--resume")
        before = {p.relative_to(self.out): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        for resume_args in ((), ("--resume",)):
            with self.subTest(resume=resume_args), self.assertRaises(ValueError):
                self.run_export("--conversation", "group:42", "--format", "txt", *resume_args)
            self.assertEqual(before, {p.relative_to(self.out): p.read_bytes()
                                      for p in self.out.rglob("*") if p.is_file()})

    def test_no_result_publishes_zero_index_without_old_watermark(self):
        self.add(1, kind="group", peer=42)
        self.run_export("--conversation", "group:99", "--resume", "--strict")
        index = self.index()
        self.assertEqual(index["message_count"], 0)
        self.assertEqual(index["conversation_count"], 0)
        self.assertEqual(index["conversations"], [])
        self.assertIsNone(index["export"]["last_msg_time"])
        self.assertTrue(index["export"]["complete"])
        checkpoint = json.loads((self.out / ".export-checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["completed"], {})

    def test_explain_only_metadata_no_body_or_state_or_output(self):
        self.add(1, kind="group", peer=42, body=b"malformed")
        before = self.db.read_bytes()
        with patch.object(exporter, "parse_body", side_effect=AssertionError("body parse")), \
             patch.object(exporter, "_atomic_text", side_effect=AssertionError("write")), \
             patch.object(exporter, "_source_fingerprint", side_effect=AssertionError("fingerprint")), \
             patch.object(exporter.selection_engine, "infer_self", side_effect=AssertionError("identity scan")), \
             patch.object(exporter.selection_engine, "iter_rows", side_effect=AssertionError("read rows")):
            self.run_export("--conversation", "group:42", "--explain-query", "--resume", "--immutable")
        self.assertFalse(self.out.exists())
        self.assertEqual(before, self.db.read_bytes())
        plan = json.loads(self.stdout.getvalue())
        self.assertTrue(plan["queries"])
        self.assertIn("SEARCH", json.dumps(plan).upper())

    def test_unknown_selected_timestamp_strict_fails_tolerant_incomplete(self):
        self.add(1, None, kind="group", peer=42)
        with self.assertRaises(ValueError):
            self.run_export("--conversation", "group:42", "--after", str(T), "--strict")
        self.run_export("--conversation", "group:42", "--after", str(T))
        self.assertFalse(self.index()["export"]["complete"])
        self.assertTrue(self.index()["export"]["errors"])

    def test_reverse_recovery_uses_identical_selection(self):
        self.add(1, kind="group", peer=42)
        original = exporter.selection_engine.iter_rows
        calls = []
        def broken(*args, **kwargs):
            calls.append((args[4], kwargs.copy()))
            yield from original(*args, **kwargs)
            raise sqlite3.DatabaseError("synthetic page error")
        with patch.object(exporter.selection_engine, "iter_rows", side_effect=broken):
            self.run_export("--conversation", "group:42", "--since", str(T - 1))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertFalse(calls[0][1].pop("reverse"))
        self.assertTrue(calls[1][1].pop("reverse"))
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertFalse(self.index()["export"]["complete"])

    def test_explicit_identity_handles_receive_only_private_snapshot(self):
        self.add(1, sender=22, peer=11, direction=0)
        with self.assertRaisesRegex(ValueError, "self-qq"):
            self.run_export("--conversation", "c2c:22", "--strict")
        self.run_export("--conversation", "c2c:22", "--self-qq", "11", "--strict")
        self.assertEqual(self.messages()[0]["direction"], "in")
        self.assertEqual(self.index()["conversations"][0]["id"], "c2c:22")

    def test_selected_path_resolves_alternate_timestamp_column(self):
        self.add(1, kind="group", peer=42)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('ALTER TABLE group_msg_table RENAME COLUMN "40050" TO "40058"')
        self.run_export("--conversation", "group:42", "--after", str(T), "--strict")
        self.assertEqual(self.messages()[0]["time"], T)

    def test_repeated_conversations_form_union(self):
        self.add(1, kind="group", peer=42)
        self.add(2, kind="group", peer=43)
        self.add(3, kind="group", peer=44)
        self.run_export("--conversation", "group:42", "--conversation", "group:43", "--strict")
        self.assertEqual({c["id"] for c in self.index()["conversations"]}, {"group:42", "group:43"})

    def test_legacy_checkpoint_existing_identity_mismatch_is_rejected(self):
        self.add(1)
        self.run_export("--resume")
        path = self.out / ".export-checkpoint.json"
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        checkpoint["parameters"].pop("selection")
        checkpoint["parameters"]["self_qq"] = "999"
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "参数"):
            self.run_export("--resume")

    def test_standard_group_selection_uses_index_without_identity_or_preflight_scans(self):
        self.add(1, kind="group", peer=42)
        self.add(2, kind="group", peer=43, body=b"malformed")
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE INDEX group_uid ON group_msg_table("40021")')
            conn.executemany("INSERT INTO group_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)",
                             ((mid, T, 1, "u11", 11, f"u{mid}", mid, 2, 0, b"malformed")
                              for mid in range(1000, 3000)))
            conn.execute("ANALYZE")
        statements = []
        plans = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            def trace(sql):
                statements.append(sql)
                if sql.lstrip().upper().startswith("SELECT") and 'FROM "group_msg_table"' in sql:
                    plans.extend(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
            conn.set_trace_callback(trace)
            return conn
        with patch.object(exporter.sqlite3, "connect", side_effect=traced), \
             patch.object(exporter.selection_engine, "infer_self", side_effect=AssertionError("identity scan")):
            self.run_export("--conversation", "group:42", "--standard-layout", "--strict")
        self.assertEqual([m["msg_id"] for m in self.messages()], [1])
        self.assertFalse(any("typeof(" in sql.lower() or "SELECT DISTINCT" in sql.upper() for sql in statements))
        self.assertTrue(plans)
        self.assertTrue(any("SEARCH" in detail for detail in plans))
        self.assertFalse(any("SCAN" in detail for detail in plans), plans)

    def test_unfiltered_path_retains_legacy_read_and_lean(self):
        self.add(1)
        with patch.object(exporter, "read_messages", wraps=exporter.read_messages) as reader, \
             patch.object(exporter, "_lean", wraps=exporter._lean) as lean:
            self.run_export("--strict", "--since", str(T - 1))
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(lean.call_count, 1)
        self.assertEqual(self.index()["message_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
