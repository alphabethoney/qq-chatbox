"""Explicit scoped SQLite acceptance; zero pages are not a repair."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import sqlite3

VALIDATION_POLICY = "ntqq-scoped-integrity"
VALIDATION_VERSION = 1


def validate_policy(integrity_mode="full", conversation=None, *, verify_hmac=True):
    if integrity_mode not in ("full", "selected"):
        raise ValueError("integrity_mode 必须为 full 或 selected")
    if integrity_mode == "selected":
        if not verify_hmac:
            raise ValueError("selected 要求 verify_hmac=True")
        from .selection import Selection
        if not isinstance(conversation, str):
            raise ValueError("selected 要求 conversation=c2c:ID")
        selection = Selection(conversations=(conversation,), kind="c2c")
        conversation = selection.conversations[0]
    return {"validation_policy": VALIDATION_POLICY, "validation_version": VALIDATION_VERSION,
            "integrity_mode": integrity_mode, "conversation": conversation,
            "scope": conversation if integrity_mode == "selected" else "full"}


def source_identity(path, header, salt, reserve):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode("utf-8")
                          + b"\0" + header + salt + str(reserve).encode()).hexdigest()


def selected_check(path):
    """Check c2c and all indexes, then traverse row storage metadata.

    Body values stay inside SQLite; strict selected export reads selected bodies.

    Table integrity_check includes index correspondence and UNIQUE constraints.
    Partial checks do not establish global page ownership or freelist integrity.
    """
    result = {"table": "c2c_msg_table", "indexes": [], "rows_read": 0,
              "integrity_check": [], "quick_check": [], "readable": False}
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if sqlite3.sqlite_version_info < (3, 33, 0):
                raise ValueError("selected requires SQLite >= 3.33 table integrity_check")
            if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='c2c_msg_table'").fetchone() is None:
                raise ValueError("c2c_msg_table missing")
            result["indexes"] = [r[1] for r in conn.execute("PRAGMA index_list('c2c_msg_table')")]
            for pragma in ("integrity_check", "quick_check"):
                result[pragma] = [r[0] for r in conn.execute(f"PRAGMA {pragma}('c2c_msg_table')")]
            columns = [r[1] for r in conn.execute("PRAGMA table_xinfo('c2c_msg_table')")]
            expressions = []
            for column in columns:
                quoted = '"' + column.replace('"', '""') + '"'
                expressions.extend((f"typeof({quoted})", f"length(CAST({quoted} AS BLOB))"))
            if not expressions:
                raise ValueError("c2c_msg_table has no readable columns")
            sql = 'SELECT ' + ','.join(expressions) + ' FROM "c2c_msg_table" NOT INDEXED'
            for _ in conn.execute(sql):
                result["rows_read"] += 1
            result["traversal"] = "storage-metadata"
            result["readable"] = True
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        result["error"] = str(exc)
    result["ok"] = (result["readable"] and result["integrity_check"] == ["ok"]
                    and result["quick_check"] == ["ok"])
    return result


def inspect_integrity(path, *, policy, full_result, zero_pages=(), verify_hmac=True):
    """Report all observed input zero-page numbers, including scanned WAL frames.

    Even a later authenticated WAL overwrite does not erase that observation.
    Acceptance is scoped SQLite usability, not proof of complete page ownership.
    """
    selected = selected_check(path) if policy["integrity_mode"] == "selected" else None
    full_ok = full_result == "ok"
    selected_ok = selected["ok"] if selected is not None else None
    accepted = selected_ok if selected is not None else full_ok
    return {**policy, "integrity": full_result, "full_integrity_ok": full_ok,
            "selected_integrity_ok": selected_ok, "selected_integrity": selected,
            "accepted": accepted, "ok": accepted,
            "nonzero_pages_hmac_verified": bool(verify_hmac),
            "zero_pages": sorted(set(zero_pages)),
            "hmac_verified": bool(verify_hmac and not zero_pages)}
