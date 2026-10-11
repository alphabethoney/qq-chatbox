"""读取（已解密的）NTQQ 消息数据库，抽取会话与消息。

NTQQ 的消息表列名就是协议字段号字符串，例如::

    c2c_msg_table   40001=消息ID 40050=时间戳 40013=方向 40020=发送者UID
                    40033=发送者QQ 40021=对端UID 40030=对端QQ 40011=消息类型
                    40800=消息体(Protobuf)
    group_msg_table 额外有 40012=子类型，40021/40030 是群号

字段号会随 QQ 版本变化，所以这里全部按「候选列名」逐级回退，
并把实际读到的列记录下来，方便排查。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import wire

# 逻辑字段 -> 候选列名（按优先级）
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "msg_id": ("40001", "40002"),
    "timestamp": ("40050", "40058", "49155", "40051"),
    "direction": ("40013",),
    "sender_uid": ("40020",),
    "sender_qq": ("40033",),
    "peer_uid": ("40021",),
    "peer_qq": ("40030",),
    "msg_type": ("40011",),
    "subtype": ("40012",),
    "body": ("40800",),
    "sender_name": ("40093", "40090"),
}

# 消息体 Protobuf 里的字段号
TEXT_FIELD = 45101        # 文本内容
CONTENT_TYPE_FIELD = 45002  # 内容子类型（首段）


@dataclass
class Conversation:
    kind: str                 # "c2c" | "group"
    key: str                  # 会话唯一键
    title: str                # 展示名（暂用 ID，后续可被名称表覆盖）
    group_id: str | None = None
    peer_qq: str | None = None
    peer_uid: str | None = None
    messages: list[dict] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.messages)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def detect_message_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """返回 ``[(表名, 'c2c'|'group'), ...]``。"""
    found: list[tuple[str, str]] = []
    for table in list_tables(conn):
        name = table.lower()
        if name in ("c2c_msg_table", "buddy_msg_table"):
            found.append((table, "c2c"))
        elif name in ("group_msg_table", "discuss_msg_table"):
            found.append((table, "group"))
    if found:
        return found
    # 回退：任何带有 40800 列的表都当作消息表
    for table in list_tables(conn):
        cols = table_columns(conn, table)
        if "40800" in cols:
            kind = "group" if "group" in table.lower() else "c2c"
            found.append((table, kind))
    return found


def resolve_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """把逻辑字段映射到实际存在的列名。"""
    cols = set(table_columns(conn, table))
    mapping: dict[str, str] = {}
    for logical, candidates in FIELD_CANDIDATES.items():
        for cand in candidates:
            if cand in cols:
                mapping[logical] = cand
                break
    if "body" not in mapping:
        raise RuntimeError(f"表 {table} 中找不到消息体列（40800）")
    return mapping


def dump_schema(db_path: Path) -> dict:
    conn = connect(db_path)
    try:
        schema = {}
        for table in list_tables(conn):
            cols = table_columns(conn, table)
            try:
                count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                count = -1
            schema[table] = {"columns": cols, "rows": count}
        return schema
    finally:
        conn.close()


def _to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return int.from_bytes(value, "big")
        except Exception:  # noqa: BLE001
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    if ts > 10**12:  # 毫秒
        ts //= 1000
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_body(blob: bytes) -> tuple[str, list[dict]]:
    """从消息体 Protobuf 中提取文本与结构化片段。

    返回 ``(合并后的纯文本, 片段列表)``。
    """
    if not blob:
        return "", []

    segments: list[dict] = []
    try:
        fields = wire.parse_fields(blob)
    except wire.WireError:
        return "", []

    # 常规路径：所有 45101 文本段
    try:
        texts = wire.extract_text_segments(blob, text_field=TEXT_FIELD, max_depth=8)
    except wire.WireError:
        texts = []
    for t in texts:
        segments.append({"type": "text", "text": t})

    if not segments:
        # 回退：取所有看起来像人类文本的字符串（排除明显是元数据的短串）
        for _path, number, wtype, value in wire.walk(blob, max_depth=6):
            if wtype != wire.WT_LEN or number == TEXT_FIELD:
                continue
            text = wire.decode_text(value)
            if text and len(text) >= 2 and not _looks_like_metadata(text):
                segments.append({"type": "text", "text": text, "field": number})

    merged = "".join(seg.get("text", "") for seg in segments)
    return merged, segments


_META_HINTS = (
    "u_",
    "http://",
    "https://",
    "nt_",
    "com.tencent",
    ".jpg",
    ".png",
    ".gif",
    ".mp4",
    "_ver",
)


def _looks_like_metadata(text: str) -> bool:
    return any(h in text for h in _META_HINTS) and len(text) < 96


def iter_messages(
    conn: sqlite3.Connection,
    table: str,
    kind: str,
    columns: dict[str, str],
    *,
    include_content: bool = False,
) -> Iterator[dict]:
    names = list(columns.values())
    select = ", ".join(f'"{c}"' for c in names)
    cur = conn.execute(f'SELECT {select} FROM "{table}" ORDER BY rowid')
    for row in cur:
        data = dict(zip(columns.keys(), row))
        blob = data.get("body")
        blob = bytes(blob) if isinstance(blob, (bytes, bytearray, memoryview)) else None
        text, segments = parse_body(blob) if blob else ("", [])

        ts = _to_int(data.get("timestamp"))
        msg = {
            "msg_id": _to_int(data.get("msg_id")),
            "time": ts,
            "time_str": _fmt_ts(ts),
            "direction": "out" if _to_int(data.get("direction")) == 1 else "in",
            "sender_uid": data.get("sender_uid"),
            "sender_qq": _to_int(data.get("sender_qq")),
            "peer_uid": data.get("peer_uid"),
            "peer_qq": _to_int(data.get("peer_qq")),
            "msg_type": _to_int(data.get("msg_type")),
            "subtype": _to_int(data.get("subtype")),
            "text": text,
        }
        if segments:
            msg["segments"] = segments
        if include_content and blob:
            try:
                msg["raw"] = wire.to_dict(blob, max_depth=4)
            except Exception:  # noqa: BLE001
                msg["raw_hex"] = blob.hex()
        yield msg
