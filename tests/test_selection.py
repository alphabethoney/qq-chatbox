"""Synthetic SQLite coverage; no source data or optional test dependencies."""
import argparse
import sqlite3
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from unittest.mock import patch
from qq_exporter import db
from qq_exporter.selection import (Selection, parse_time, infer_self, iter_rows,
    normalize_metadata, conversation_key, query_plan, add_arguments, selection_from_args)


def database(kind="group", affinity="INTEGER"):
    conn = sqlite3.connect(":memory:")
    table = kind + "_msg_table"
    conn.execute(f'''CREATE TABLE {table} (
        "40001" INTEGER, "40050" INTEGER, "40013" INTEGER,
        "40033" {affinity}, "40030" {affinity}, "40020" TEXT,
        "40021" TEXT, "40800" BLOB)''')
    for col in ("40033", "40030", "40050", "40001", "40020", "40021"):
        conn.execute(f'CREATE INDEX ix_{col} ON {table}("{col}")')
    return conn, table, db.resolve_columns(conn, table)


def insert(conn, table, rows):
    conn.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)", rows)


class SelectionTests(unittest.TestCase):
    def test_normalized_and_args(self):
        s = Selection(["group:002", "group:1", "group:2"], after="10", before="20", self_qq=99)
        self.assertEqual(s.conversations, ("group:1", "group:2"))
        self.assertEqual(s.to_dict()["conversations"], ["group:1", "group:2"])
        self.assertTrue(s.matches_kind("group"))
        self.assertFalse(s.matches_kind("c2c"))
        with self.assertRaises(FrozenInstanceError):
            s.kind = "c2c"
        self.assertTrue(Selection(self_qq=1).active)
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        parsed = selection_from_args(parser.parse_args(["--standard-layout", "--conversation", "group:1"]))
        self.assertTrue(parsed.standard_layout)
        self.assertFalse(selection_from_args(argparse.Namespace()).active)
        self.assertEqual(normalize_metadata({"peer_qq": b"{"}, "group")["peer_qq"], 123)
        self.assertEqual(parse_time("2024-01-01T01:02:03"), datetime(2024, 1, 1, 1, 2, 3).timestamp())
        for kwargs in [{"after": "nan"}, {"before": "inf"}, {"after": 2, "before": 1}, {"after": 1, "before": 1}, {"conversations": ["x:1"]}, {"conversations": ["c2c:1"], "kind": "group"}, {"self_qq": "abc"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Selection(**kwargs)

    def test_indexed_group_and_metadata_only(self):
        for affinity in ("INTEGER", "TEXT"):
            conn, table, cols = database(affinity=affinity)
            self.addCleanup(conn.close)
            insert(conn, table, [(1, 1700000000, 0, 9, 123, "u9", "g123", b"yes"), (2, 1700000000, 0, 9, 999, "u9", "g999", b"no")])
            for standard in (False, True):
                s = Selection(["group:123"], standard_layout=standard)
                plan = query_plan(conn, table, "group", cols, s)
                self.assertTrue(any("SEARCH" in detail and "ix_40030" in detail for detail in plan["details"]))
                self.assertEqual(bool(plan["preflight"]), not standard)
                self.assertEqual(plan["metadata_scan"], not standard)
                statements = []
                conn.set_trace_callback(statements.append)
                with patch.object(db, "parse_body", side_effect=AssertionError("decode")):
                    rows = list(iter_rows(conn, table, "group", cols, s, metadata_only=True))
                self.assertEqual([r["msg_id"] for r in rows], [1])
                self.assertNotIn("body", rows[0])
                self.assertFalse(any('"40800"' in statement for statement in statements))
                if standard:
                    self.assertFalse(any("typeof" in statement or "DISTINCT" in statement for statement in statements))
                rows = list(iter_rows(conn, table, "group", cols, s))
                self.assertEqual([r["body"] for r in rows], [b"yes"])
                self.assertTrue(any('WHERE rowid IN (1)' in statement for statement in statements))
                conn.set_trace_callback(None)

    def test_c2c_identity_and_snapshot(self):
        conn, table, cols = database("c2c")
        self.addCleanup(conn.close)
        insert(conn, table, [(1, 1, 1, 10, 20, "self", "other", b"old"), (2, 1700000000, 0, 20, 10, "other", "self", b"in"), (3, 1700000001, 0, 10, 20, "self", "other", b"out"), (4, 1700000002, 0, 30, 10, "third", "self", b"no")])
        self.assertEqual(infer_self(conn, required=True), "10")
        s = Selection(["c2c:20"], after=1700000000)
        rows = list(iter_rows(conn, table, "c2c", cols, s))
        self.assertEqual([r["body"] for r in rows], [b"in", b"out"])
        self.assertEqual([normalize_metadata(r, "c2c", 10)["direction"] for r in rows], ["in", "out"])
        plan = query_plan(conn, table, "c2c", cols, Selection(["c2c:20"], self_qq=10, standard_layout=True))
        self.assertTrue(any("SEARCH" in d for d in plan["details"]))
        self.assertFalse(plan["preflight"])
        insert(conn, table, [(5, 0, 1, 99, 20, None, None, b"conflict")])
        with self.assertRaisesRegex(ValueError, "--self-qq"):
            list(iter_rows(conn, table, "c2c", cols, s))
        self.assertIsNone(infer_self(conn))

    def test_missing_identity_and_legacy(self):
        conn, table, cols = database("c2c")
        self.addCleanup(conn.close)
        insert(conn, table, [(1, 1, 0, 20, 10, None, None, b"x")])
        with self.assertRaisesRegex(ValueError, "--self-qq"):
            list(iter_rows(conn, table, "c2c", cols, Selection(["c2c:20"])))
        self.assertEqual(len(list(iter_rows(conn, table, "c2c", cols, Selection()))), 1)

    def test_bytes_uid_null_mixed_times_since(self):
        conn, table, cols = database()
        self.addCleanup(conn.close)
        insert(conn, table, [(1, 1700000000, 0, b"9", b"123", b"u9", b"g", b"a"), (2, 1700000000000, 0, 9, (123).to_bytes(4, "big"), None, None, b"b"), (3, None, 0, 9, 123, None, None, b"null"), (4, 1700000001000, 0, 9, 123, None, None, b"end"), (5, 1700000000, 0, 9, None, None, b"uid_5", b"uid")])
        s = Selection(["group:123"], after=1700000000, before=1700000001)
        rows = list(iter_rows(conn, table, "group", cols, s, min_id=1, reverse=True))
        self.assertEqual([r["msg_id"] for r in rows], [3, 2])
        self.assertTrue(all(r["_selection_fallback"] for r in rows))
        self.assertNotIn("CAST", query_plan(conn, table, "group", cols, s)["sql"])
        uid = list(iter_rows(conn, table, "group", cols, Selection(["group:uid_5"])))
        self.assertEqual([r["msg_id"] for r in uid], [5])
        self.assertEqual(conversation_key(uid[0], "group"), "group:uid_5")
        rows = list(iter_rows(conn, table, "group", cols, s, since=1700000000))
        self.assertEqual([r["msg_id"] for r in rows], [3])

    def test_fractional_timestamp_and_invalid_qq_uid(self):
        conn, table, cols = database()
        self.addCleanup(conn.close)
        insert(conn, table, [(1, 1.6, 0, 9, -1, None, "123", b"a"), (2, 1.7, 0, 9, "invalid", None, "123", b"b"), (3, 1700000000600.5, 0, 9, 123, None, None, b"ms")])
        s = Selection(["group:123"], after=1.5, before=1.8)
        rows = list(iter_rows(conn, table, "group", cols, s))
        self.assertEqual([r["msg_id"] for r in rows], [1, 2])
        self.assertEqual(normalize_metadata(rows[0], "group")["time"], 1.6)
        rows = list(iter_rows(conn, table, "group", cols, Selection(["group:123"], after=1700000000.6004, before=1700000000.6006)))
        self.assertEqual([r["msg_id"] for r in rows], [3])
        plan = query_plan(conn, table, "group", cols, Selection(standard_layout=True))
        self.assertFalse(plan["preflight"])
        self.assertTrue(plan["metadata_scan"])

    def test_explain_does_not_execute_body(self):
        conn, table, cols = database()
        self.addCleanup(conn.close)
        insert(conn, table, [(1, 1, 0, 9, 123, None, None, b"x")])
        statements = []
        conn.set_trace_callback(statements.append)
        query_plan(conn, table, "group", cols, Selection(["group:123"]))
        self.assertFalse(any('"40800"' in statement for statement in statements))


if __name__ == "__main__":
    unittest.main()
