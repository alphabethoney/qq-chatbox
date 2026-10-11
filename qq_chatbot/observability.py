"""Opt-in local JSONL traces. Scores are relevance scores, not truth probabilities."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
import threading
import time
import uuid
from pathlib import Path


class TraceLog:
    def __init__(self, path: Path, *, include_content: bool = False, max_bytes: int = 5_000_000):
        self.path = Path(path)
        self.include_content = include_content
        self.max_bytes = max(1024, int(max_bytes))
        self._lock = threading.RLock()

    @contextmanager
    def _access(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path) + ".lock.sqlite3", timeout=10)
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield
                connection.commit()
            finally:
                connection.close()

    def record(self, query, mode, retrieved, answer, elapsed_ms):
        trace_id = uuid.uuid4().hex
        row = {"id": trace_id, "time": time.time(), "mode": mode,
               "grounded": answer.grounded, "elapsed_ms": round(elapsed_ms, 2),
               "evidence": [{"ref": hashlib.sha256((rt.turn.conversation_key + '\0' + rt.turn.id).encode()).hexdigest(),
                             "score": rt.score, "vector_score": rt.vector_score,
                             "keyword_score": rt.keyword_score} for rt in retrieved]}
        if self.include_content:
            row.update(query=query, answer=answer.text)
            for evidence, rt in zip(row["evidence"], retrieved):
                evidence.update(turn_id=rt.turn.id, conversation=rt.turn.conversation_key, text=rt.turn.text)
        with self._access():
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                os.replace(self.path, self.path.with_suffix(".previous.jsonl"))
            needs_newline = False
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as tail:
                    tail.seek(-1, os.SEEK_END)
                    needs_newline = tail.read(1) != b"\n"
            with self.path.open("a", encoding="utf-8") as f:
                if needs_newline:
                    f.write("\n")
                f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        return trace_id

    def recent(self, limit=50):
        with self._access():
            if not self.path.exists():
                return []
            from collections import deque
            with self.path.open(encoding="utf-8", errors="replace") as f:
                lines = deque(f, maxlen=min(200, max(1, limit)))
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
