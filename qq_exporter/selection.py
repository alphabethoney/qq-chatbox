"""Shared metadata-first, read-only message selection (never parses bodies)."""
from __future__ import annotations
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from . import db


def parse_time(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError(f"无效时间: {value!r}") from exc
    if isinstance(value, bool) or not math.isfinite(result):
        raise ValueError("时间必须是有限 Unix 秒或 ISO 日期")
    return result


def _text(value):
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            text = raw.decode("utf-8")
            if text and all(c.isprintable() for c in text):
                return text
        except UnicodeDecodeError:
            pass
        return str(int.from_bytes(raw, "big")) if raw else None
    return str(value)


def _integer(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if not raw:
            return None
        try:
            return int(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return int.from_bytes(raw, "big")
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return None


def _qq(value):
    result = _integer(value)
    return result if result and result > 0 else None


@dataclass(frozen=True)
class Selection:
    conversations: tuple[str, ...] = ()
    kind: str | None = None
    after: float | None = None
    before: float | None = None
    self_qq: str | None = None
    standard_layout: bool = False

    def __post_init__(self):
        if self.kind not in (None, "group", "c2c"):
            raise ValueError("kind 必须是 group 或 c2c")
        keys = []
        values = (self.conversations,) if isinstance(self.conversations, str) else self.conversations
        for value in values:
            prefix, separator, ident = str(value).strip().partition(":")
            if not separator or prefix not in ("group", "c2c") or not ident.strip() or ident != ident.strip():
                raise ValueError(f"无效会话键: {value!r}，请使用 group:ID 或 c2c:ID")
            if self.kind and prefix != self.kind:
                raise ValueError("会话键与 kind 筛选矛盾")
            if ident.isdecimal():
                ident = str(int(ident))
            keys.append(f"{prefix}:{ident}")
        object.__setattr__(self, "conversations", tuple(sorted(set(keys))))
        object.__setattr__(self, "after", parse_time(self.after))
        object.__setattr__(self, "before", parse_time(self.before))
        if self.after is not None and self.before is not None and self.after >= self.before:
            raise ValueError("after 必须早于 before")
        if self.self_qq is not None:
            ident = _qq(self.self_qq)
            if ident is None:
                raise ValueError("--self-qq 必须是正整数 QQ 号")
            object.__setattr__(self, "self_qq", str(ident))

    @property
    def active(self):
        return bool(self.standard_layout or self.conversations or self.kind or self.after is not None or self.before is not None or self.self_qq)

    def matches_kind(self, kind):
        return (self.kind is None or self.kind == kind) and (not self.conversations or any(k.startswith(kind + ":") for k in self.conversations))

    includes_kind = matches_kind

    def to_dict(self):
        result = asdict(self)
        result["conversations"] = list(self.conversations)
        return result


def add_arguments(parser):
    parser.add_argument("--standard-layout", action="store_true", help="确认标准 int/text 布局，跳过兼容扫描")
    parser.add_argument("--conversation", action="append", default=[], help="会话键，可重复：group:ID / c2c:ID")
    parser.add_argument("--kind", choices=("group", "c2c"))
    parser.add_argument("--after", type=parse_time, help="包含起点：Unix 秒或本地 ISO 日期")
    parser.add_argument("--before", type=parse_time, help="不含终点：Unix 秒或本地 ISO 日期")
    parser.add_argument("--self-qq", help="自己的 QQ 号")
    parser.add_argument("--explain-query", action="store_true", help="只显示查询计划")


def selection_from_args(args):
    return Selection(standard_layout=bool(getattr(args, "standard_layout", False)), conversations=getattr(args, "conversation", None) or (), **{k: getattr(args, k, None) for k in ("kind", "after", "before", "self_qq")})


def normalize_metadata(data, kind, self_qq=None):
    msg = {key: _integer(data.get(key)) for key in ("msg_id", "msg_type", "subtype")}
    msg.update({key: _qq(data.get(key)) for key in ("sender_qq", "peer_qq")})
    msg.update({key: _text(data.get(key)) for key in ("sender_uid", "peer_uid", "sender_name")})
    direction = data.get("direction")
    outgoing = direction == "out" or _integer(direction) == 1
    own = _qq(self_qq)
    if kind == "c2c" and own is not None and msg["sender_qq"] is not None:
        outgoing = msg["sender_qq"] == own
    msg["direction"] = "out" if outgoing else "in"
    raw_ts = data.get("timestamp", data.get("time"))
    if isinstance(raw_ts, (bytes, bytearray, memoryview)):
        try:
            ts = float(bytes(raw_ts).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            ts = _integer(raw_ts)
    else:
        try:
            ts = float(raw_ts) if raw_ts is not None else None
        except (ValueError, TypeError, OverflowError):
            ts = None
    if ts is not None and not math.isfinite(ts):
        ts = None
    if ts is not None and ts == int(ts):
        ts = int(ts)
    msg["time"] = ts
    msg["time_str"] = db._fmt_ts(ts)
    return msg


def peer_of(msg, kind, self_qq=None):
    msg = normalize_metadata(msg, kind, self_qq)
    side = "peer" if kind == "group" or msg["direction"] == "out" else "sender"
    qq, uid = msg.get(side + "_qq"), msg.get(side + "_uid")
    ident = str(qq) if qq is not None else uid
    return f"{kind}:{ident}", str(qq) if qq is not None else None, uid


def conversation_key(msg, kind, self_qq=None):
    return peer_of(msg, kind, self_qq)[0]


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def infer_self(conn, tables=None, *, required=False):
    candidates = set()
    for table, kind in tables if tables is not None else db.detect_message_tables(conn):
        columns = db.resolve_columns(conn, table)
        if "direction" not in columns or "sender_qq" not in columns:
            continue
        sql = f'SELECT DISTINCT {_quote(columns["direction"])}, {_quote(columns["sender_qq"])} FROM {_quote(table)}'
        for direction, sender in conn.execute(sql):
            if _integer(direction) == 1 and (qq := _qq(sender)) is not None:
                candidates.add(str(qq))
    if len(candidates) > 1:
        if required:
            raise ValueError("自动识别自己的 QQ 出现多个候选，请指定 --self-qq")
        return None
    if not candidates and required:
        raise ValueError("无法可靠识别自己的 QQ，私聊筛选请指定 --self-qq")
    return next(iter(candidates), None)


@dataclass(frozen=True)
class Query:
    sql: str
    params: tuple
    fallback: bool
    fallback_reasons: tuple[str, ...]
    metadata_sql: str
    self_qq: str | None


def _unsafe(conn, table, column, *, numeric=False):
    col = _quote(column)
    allowed = "'integer','real'" if numeric else "'integer','text'"
    sql = f"SELECT 1 FROM {_quote(table)} WHERE {col} IS NOT NULL AND typeof({col}) NOT IN ({allowed}) LIMIT 1"
    if conn.execute(sql).fetchone():
        return True
    if not numeric:
        sql = f"SELECT DISTINCT {col} FROM {_quote(table)} WHERE typeof({col})='text'"
        for (value,) in conn.execute(sql):
            normalized = _qq(value)
            if normalized is not None and value != str(normalized):
                return True
    return False


def build_query(conn, table, kind, columns, selection, *, min_id=None, since=None,
                reverse=False, self_qq=None, metadata_only=False):
    selection = selection or Selection()
    if self_qq is not None and selection.self_qq is not None and str(_qq(self_qq)) != selection.self_qq:
        raise ValueError("self_qq 与 Selection.self_qq 矛盾")
    own = str(_qq(self_qq)) if self_qq is not None else selection.self_qq
    clauses, params, reasons = [], [], []
    if not selection.matches_kind(kind):
        clauses.append("0")
    elif kind == "c2c" and own is None:
        own = infer_self(conn, required=selection.active)
    if selection.conversations and selection.matches_kind(kind):
        ids = [key.split(":", 1)[1] for key in selection.conversations if key.startswith(kind + ":")]
        fields = ("peer_qq", "peer_uid") if kind == "group" else ("sender_qq", "sender_uid", "peer_qq", "peer_uid")
        # Always include UID candidates: NULL, zero, negative and malformed
        # QQ values all normalize to absent and can legitimately use a UID.
        usable = [columns[f] for f in fields if f in columns]
        if not usable or (not selection.standard_layout and any(_unsafe(conn, table, col) for col in usable)):
            reasons.append("conversation metadata requires normalization")
        else:
            alternatives = []
            bound_ids = ids + [int(ident) for ident in ids if ident.isdecimal() and int(ident) <= 2**63 - 1]
            for col in usable:
                alternatives.append(f'{_quote(col)} IN ({",".join("?" for _ in bound_ids)})')
                params.extend(bound_ids)
            clauses.append("(" + " OR ".join(alternatives) + ")")
    lower = selection.after
    since = parse_time(since)
    if since is not None:
        lower = max(lower, since) if lower is not None else since
    if lower is not None or selection.before is not None:
        col = columns.get("timestamp")
        if not col or (not selection.standard_layout and _unsafe(conn, table, col, numeric=True)):
            reasons.append("timestamp metadata requires normalization")
        else:
            c = _quote(col)
            branches = [f"{c} IS NULL"]
            for millis in (False, True):
                terms = [f"{c} {'>' if millis else '<='} 1000000000000"]
                scale = 1000 if millis else 1
                if lower is not None:
                    terms.append(f"{c} {'>' if since is not None and lower == since else '>='} ?")
                    params.append(lower * scale)
                if selection.before is not None:
                    terms.append(f"{c} < ?")
                    params.append(selection.before * scale)
                branches.append("(" + " AND ".join(terms) + ")")
            clauses.append("(" + " OR ".join(branches) + ")")
    if min_id is not None:
        col = columns.get("msg_id")
        if not col or (not selection.standard_layout and _unsafe(conn, table, col, numeric=True)):
            reasons.append("message id metadata requires normalization")
        else:
            clauses.append(f"{_quote(col)} > ?")
            params.append(min_id)
    suffix = f" FROM {_quote(table)}" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    suffix += " ORDER BY rowid" + (" DESC" if reverse else "")
    metadata = [f'{_quote(col)} AS {_quote(key)}' for key, col in columns.items() if key != "body"]
    projection = ['rowid AS "_selection_rowid"'] + metadata
    metadata_sql = "SELECT " + ", ".join(projection) + suffix
    if not metadata_only:
        projection += [f'{_quote(columns["body"])} AS "body"']
    return Query("SELECT " + ", ".join(projection) + suffix, tuple(params), bool(reasons), tuple(reasons), metadata_sql, own)


def query_plan(conn, table, kind, columns, selection, **kwargs):
    preflights = []

    class RecordingConnection:
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("SELECT"):
                preflights.append({"sql": sql, "params": list(params), "details": [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]})
            return conn.execute(sql, params)

    query = build_query(RecordingConnection(), table, kind, columns, selection, **kwargs)
    details = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query.metadata_sql, query.params)]
    return {"sql": query.sql, "metadata_sql": query.metadata_sql, "params": list(query.params), "fallback": query.fallback,
            "fallback_reasons": list(query.fallback_reasons), "details": details,
            "preflight": preflights, "metadata_scan": any("SCAN" in detail for detail in details) or any("SCAN" in detail for item in preflights for detail in item["details"])}


def iter_rows(conn, table, kind, columns, selection, *, min_id=None, since=None,
              reverse=False, self_qq=None, metadata_only=False):
    selection = selection or Selection()
    query = build_query(conn, table, kind, columns, selection, min_id=min_id, since=since,
                        reverse=reverse, self_qq=self_qq, metadata_only=metadata_only)
    lower = selection.after
    since = parse_time(since)
    if since is not None:
        lower = max(lower, since) if lower is not None else since
    cursor = conn.execute(query.metadata_sql, query.params)
    names = [col[0] for col in cursor.description]
    pending = []

    def emit(batch):
        bodies = {}
        if not metadata_only and batch:
            placeholders = ",".join("?" for _ in batch)
            sql = f'SELECT rowid, {_quote(columns["body"])} FROM {_quote(table)} WHERE rowid IN ({placeholders})'
            bodies = dict(conn.execute(sql, [row["_selection_rowid"] for row in batch]))
        for row in batch:
            if not metadata_only:
                row["body"] = bodies.get(row["_selection_rowid"])
            row["_selection_fallback"] = query.fallback
            yield row

    for values in cursor:
        data = dict(zip(names, values))
        msg = normalize_metadata(data, kind, query.self_qq)
        if selection.conversations and conversation_key(msg, kind, query.self_qq) not in selection.conversations:
            continue
        ts = msg["time"]
        seconds = ts / 1000 if ts is not None and ts > 10**12 else ts
        if seconds is not None and since is not None and seconds <= since:
            continue
        if seconds is not None and lower is not None and seconds < lower:
            continue
        if seconds is not None and selection.before is not None and seconds >= selection.before:
            continue
        if min_id is not None and (msg["msg_id"] is None or msg["msg_id"] <= min_id):
            continue
        pending.append(data)
        if len(pending) >= 256:
            yield from emit(pending)
            pending = []
    yield from emit(pending)
