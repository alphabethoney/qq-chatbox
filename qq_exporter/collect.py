"""把数据库里的消息聚合为会话列表。"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from . import db as dbmod
from .db import Conversation

TOOL_VERSION = "1.0.0"


def load_names(path: Path | None) -> dict[str, str]:
    """读取可选的名称映射文件。

    形如 ``{"group:123456": "群名", "c2c:10001": "备注名"}``，
    也接受只有数字键的简写形式。
    """
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("名称映射文件必须是 JSON 对象")
    return {str(k): str(v) for k, v in data.items()}


def build_conversations(
    db_path: Path,
    *,
    include_content: bool = False,
    names: dict[str, str] | None = None,
    limit: int | None = None,
    min_ids: dict[str, int] | None = None,
    progress=None,
    selection=None,
    self_qq=None,
    connection=None,
) -> tuple[dict, list[Conversation]]:
    """读取数据库并返回 ``(元信息, 会话列表)``。

    ``min_ids`` 用于增量导出：只读取 ``{表名: 最小消息ID}`` 中大于该 ID 的消息。
    """
    names = names or {}
    min_ids = min_ids or {}
    conn = connection if connection is not None else dbmod.connect(db_path)
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        tables = dbmod.detect_message_tables(conn)
        if not tables:
            raise RuntimeError("数据库中没有找到消息表")

        if selection is not None and selection.active:
            from .selection import iter_rows, normalize_metadata, peer_of
            tables = [(t, k) for t, k in tables if selection.matches_kind(k)]
            self_qq = effective_self(conn, tables, selection, self_qq)

        buckets: "OrderedDict[str, Conversation]" = OrderedDict()
        total = 0
        max_ids: dict[str, int] = {k: v for k, v in min_ids.items() if k != "*"}
        for table, kind in tables:
            columns = dbmod.resolve_columns(conn, table)
            minimum = min_ids.get("*", min_ids.get(table))
            if minimum is not None and "msg_id" not in columns:
                raise ValueError(f"表 {table} 缺少消息 ID 列，不能应用 --since/增量水位")
            if progress:
                progress(f"读取 {table}（{kind}）…")
            table_max = minimum
            if selection is not None and selection.active:
                rows = iter_rows(conn, table, kind, columns, selection,
                                 min_id=minimum, self_qq=self_qq)
                messages = (_selected_message(row, kind, self_qq, include_content, selection) for row in rows)
            else:
                messages = dbmod.iter_messages(conn, table, kind, columns,
                                               include_content=include_content, min_id=minimum)
            for msg in messages:
                if selection is not None and selection.active:
                    identity = peer_of(msg, kind, self_qq=self_qq)
                else:
                    identity = None
                _key, conv = _bucket_for(buckets, kind, msg, identity=identity)
                conv.messages.append(msg)
                total += 1
                if msg.get("msg_id") is not None:
                    table_max = max(table_max, msg["msg_id"]) if table_max is not None else msg["msg_id"]
                if limit and total >= limit:
                    break
            if table_max is not None:
                max_ids[table] = table_max
            if limit and total >= limit:
                break
    finally:
        if connection is None:
            conn.close()

    for conv in buckets.values():
        conv.title = _apply_name(conv, names)
        conv.messages.sort(key=lambda m: (m.get("time") or 0, m.get("msg_id") or 0))

    meta = {
        "tool": "qq-chat-exporter",
        "tool_version": TOOL_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_db": str(db_path),
        "conversation_count": len(buckets),
        "message_count": total,
        "last_ids": max_ids,
    }
    return meta, list(buckets.values())


def effective_self(conn, tables, selection, self_qq=None):
    """Resolve identity only when a selected private table can be read."""
    from .selection import infer_self
    explicit = self_qq if self_qq is not None else selection.self_qq
    if explicit is not None:
        return explicit
    if any(kind == "c2c" and selection.matches_kind(kind) for _, kind in tables):
        return infer_self(conn, tables=tables, required=True)
    return None


def _selected_message(data, kind, self_qq, include_content, selection):
    from .selection import normalize_metadata
    msg = normalize_metadata(data, kind, self_qq=self_qq)
    if (selection.after is not None or selection.before is not None) and msg.get("time") is None:
        raise ValueError("时间筛选遇到未知时间，无法确认消息是否属于所选区间")
    blob = data.get("body")
    blob = bytes(blob) if isinstance(blob, (bytes, bytearray, memoryview)) else None
    text, segments = dbmod.parse_body(blob) if blob else ("", [])
    msg["text"] = text
    if segments:
        msg["segments"] = segments
    if blob:
        media = dbmod.extract_media(blob)
        if media:
            msg["media"] = media
    if include_content and blob:
        try:
            msg["raw"] = dbmod.wire.to_dict(blob, max_depth=4)
        except Exception:
            msg["raw_hex"] = blob.hex()
    return msg


def list_conversations(db_path, *, kind=None, names=None, self_qq=None, standard_layout=False):
    """Aggregate metadata only; never select or decode message bodies."""
    from .selection import Selection, iter_rows, normalize_metadata, peer_of
    selection = Selection(kind=kind, self_qq=self_qq, standard_layout=standard_layout)
    conn = dbmod.connect(db_path)
    try:
        conn.execute("BEGIN")
        tables = [(t, k) for t, k in dbmod.detect_message_tables(conn) if selection.matches_kind(k)]
        identity = effective_self(conn, tables, selection)
        entries = {}
        for table, table_kind in tables:
            columns = dbmod.resolve_columns(conn, table)
            for row in iter_rows(conn, table, table_kind, columns, selection,
                                 self_qq=identity, metadata_only=True):
                msg = normalize_metadata(row, table_kind, self_qq=identity)
                key, peer_qq, peer_uid = peer_of(msg, table_kind, self_qq=identity)
                entry = entries.setdefault(key, {
                    "key": key, "kind": table_kind,
                    "title": (names or {}).get(key, (names or {}).get(key.split(":", 1)[1], str(peer_qq or peer_uid or "未知"))),
                    "message_count": 0, "first_time": None, "last_time": None,
                    "unknown_time_count": 0,
                })
                entry["message_count"] += 1
                timestamp = msg.get("time")
                if timestamp is not None and timestamp > 10**12:
                    timestamp /= 1000
                if timestamp is None:
                    entry["unknown_time_count"] += 1
                else:
                    entry["first_time"] = timestamp if entry["first_time"] is None else min(timestamp, entry["first_time"])
                    entry["last_time"] = timestamp if entry["last_time"] is None else max(timestamp, entry["last_time"])
        return sorted(entries.values(), key=lambda entry: entry["key"])
    finally:
        conn.close()


def _peer_of(msg: dict, kind: str) -> tuple[str, str | None, str | None]:
    """从消息记录里取出会话标识。

    NTQQ 的 c2c/group 消息表里，``40021/40030`` 存的是会话对象
    （私聊是对方、群聊是群号），``40020/40033`` 是发送者。
    但历史版本里也有把发送者写进 40030 的情况，所以这里做一次修正：
    私聊会话应当保持稳定，因此优先使用「非自己」的那一侧。
    """
    peer_qq = msg.get("peer_qq")
    peer_uid = msg.get("peer_uid")
    sender_qq = msg.get("sender_qq")
    sender_uid = msg.get("sender_uid")

    if kind == "group":
        group_id = peer_qq or peer_uid
        return f"group:{group_id}", (str(peer_qq) if peer_qq else None), peer_uid

    # 私聊：取「发出去时是对方、收进来时也是对方」的那一侧
    if msg.get("direction") == "in":
        person_qq, person_uid = sender_qq, sender_uid
    else:
        person_qq, person_uid = peer_qq, peer_uid
    ident = person_qq if person_qq is not None else person_uid
    return f"c2c:{ident}", (str(person_qq) if person_qq is not None else None), person_uid


def _bucket_for(buckets, kind: str, msg: dict, *, identity=None) -> tuple[str, Conversation]:
    key, peer_qq, peer_uid = identity or _peer_of(msg, kind)
    conv = buckets.get(key)
    if conv is None:
        title = str(peer_qq or peer_uid or "未知")
        conv = Conversation(
            kind=kind,
            key=key,
            title=title,
            group_id=peer_qq if kind == "group" else None,
            peer_qq=peer_qq,
            peer_uid=peer_uid,
        )
        buckets[key] = conv
    return key, conv


def _apply_name(conv: Conversation, names: dict[str, str]) -> str:
    if conv.key in names:
        return names[conv.key]
    bare = conv.key.split(":", 1)[-1]
    if bare in names:
        return names[bare]
    return conv.title
