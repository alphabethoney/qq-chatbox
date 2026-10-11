"""Opt-in local summaries. Stored statements are not verified facts.

SQLite transactions provide atomic commits and serialize writers across instances
and processes. Fresh connections prevent stale snapshots overwriting other writers.
No LLM or network access is required.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable, Iterator
import uuid


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    scope: str
    user_summary: str
    assistant_summary: str
    created_at: float
    expires_at: float
    score: float = 0.0


def _scope(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("memory scope must be a non-empty trusted identity string")
    return value  # Exact matching: never normalize distinct identity scopes.


def _positive(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            tokens.add(run)
    return tokens


class MemoryStore:
    """Bounded extractive summaries, separated by speaker and exact scope.

    Callers must construct scopes from trusted identity metadata, never prompts,
    using an unambiguous encoding of all identity fields. Expiry is enforced on
    every read/write; ``prune`` also supports external maintenance timers.
    """

    def __init__(self, path: str | Path, *, ttl_days: float = 90,
                 half_life_days: float = 30, max_entries_per_scope: int = 1000,
                 summary_chars: int = 600, clock: Callable[[], float] = time.time) -> None:
        self.ttl_seconds = _positive(ttl_days, "ttl_days") * 86400
        self.half_life_seconds = _positive(half_life_days, "half_life_days") * 86400
        for name, value in (("max_entries_per_scope", max_entries_per_scope),
                            ("summary_chars", summary_chars)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_entries_per_scope = max_entries_per_scope
        self.summary_chars = summary_chars
        self.clock = clock
        if str(path) == ":memory:":
            raise ValueError("MemoryStore requires a persistent filesystem path")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                user_summary TEXT NOT NULL, assistant_summary TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS memories_scope_time ON memories(scope, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS memories_expiry ON memories(expires_at)")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now(self) -> float:
        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite timestamp")
        return now

    def _summary(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("memory content must be a string")
        text = text.strip()
        return text if len(text) <= self.summary_chars else text[:self.summary_chars] + "…"

    def add(self, scope: str, user: str, assistant: str) -> MemoryEntry:
        scope = _scope(scope)
        now = self._now()
        entry = MemoryEntry(uuid.uuid4().hex, scope, self._summary(user),
                            self._summary(assistant), now, now + self.ttl_seconds)
        with self._transaction() as conn:
            conn.execute("DELETE FROM memories WHERE expires_at <= ?", (now,))
            conn.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?)",
                         (entry.id, entry.scope, entry.user_summary,
                          entry.assistant_summary, entry.created_at, entry.expires_at))
            conn.execute("""DELETE FROM memories WHERE scope = ? AND id NOT IN (
                SELECT id FROM memories WHERE scope = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
            )""", (scope, scope, self.max_entries_per_scope))
        return entry

    def retrieve(self, scope: str, query: str | None = None, limit: int = 5) -> list[MemoryEntry]:
        scope = _scope(scope)
        if query is not None and not isinstance(query, str):
            raise TypeError("query must be a string or None")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        now = self._now()
        with self._transaction() as conn:
            conn.execute("DELETE FROM memories WHERE expires_at <= ?", (now,))
            rows = conn.execute("SELECT * FROM memories WHERE scope = ? ORDER BY created_at DESC, rowid DESC",
                                (scope,)).fetchall()
        query_tokens = _tokens(query or "")
        entries = []
        for row in rows:
            if query and query.strip():
                candidates = (row["user_summary"], row["assistant_summary"])
                relevance = max((len(query_tokens & _tokens(text)) / len(query_tokens)
                                 if query_tokens else float(query.strip() in text))
                                for text in candidates)
                if relevance == 0:
                    continue
            else:
                relevance = 1.0
            decay = 2 ** (-max(0.0, now - row["created_at"]) / self.half_life_seconds)
            entries.append(MemoryEntry(**dict(row), score=relevance * decay))
        entries.sort(key=lambda entry: (entry.score, entry.created_at), reverse=True)
        return entries[:limit]

    def delete(self, scope: str, memory_id: str | None = None) -> int:
        """Delete one scoped entry, or all entries in exactly this scope."""
        scope = _scope(scope)
        with self._transaction() as conn:
            if memory_id is None:
                result = conn.execute("DELETE FROM memories WHERE scope = ?", (scope,))
            else:
                result = conn.execute("DELETE FROM memories WHERE scope = ? AND id = ?", (scope, memory_id))
            return result.rowcount

    def prune(self) -> int:
        """Physically remove expired entries across scopes; return removal count."""
        with self._transaction() as conn:
            return conn.execute("DELETE FROM memories WHERE expires_at <= ?", (self._now(),)).rowcount
