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
from .media import extract_media

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


# 消息体 MsgContent 的内容类型（字段 45002）-> 占位标签。
# 1 是纯文本（特判，直接取 45101）；其余类型只输出占位符，
# 避免把图片/文件/表情里的 URL、MD5、文件名等元数据误当正文。
CONTENT_TYPE_LABELS: dict[int, str] = {
    2: "图片",
    3: "文件",
    4: "语音",
    5: "视频",
    6: "表情",
    7: "表情",
    8: "回复",
    10: "小程序",
    11: "表情",
    14: "闪传",
    16: "卡片",
    21: "通话",
    26: "链接",
}


def _first_field(fields, number, wire_type):
    for n, w, v in fields:
        if n == number and w == wire_type:
            return v
    return None


def parse_body(blob: bytes) -> tuple[str, list[dict]]:
    """从消息体 Protobuf 中提取文本与结构化片段。

    返回 ``(合并后的纯文本, 片段列表)``。

    NTQQ 的消息体结构是 ``MsgBody { repeated MsgContent content = 40800 }``，
    每个 ``MsgContent`` 用 ``45002``（内容类型）区分，``45101`` 才是文本正文。
    """
    if not blob:
        return "", []

    try:
        fields = wire.parse_fields(blob)
    except wire.WireError:
        return "", []

    # 主路径：MsgBody -> 逐个 MsgContent 按内容类型处理
    contents = [v for n, w, v in fields if n == 40800 and w == wire.WT_LEN]
    if contents:
        segments: list[dict] = []
        texts: list[str] = []
        for content in contents:
            try:
                inner = wire.parse_fields(content)
            except wire.WireError:
                continue
            ctype = _first_field(inner, CONTENT_TYPE_FIELD, wire.WT_VARINT)
            if ctype == 1:
                raw = _first_field(inner, TEXT_FIELD, wire.WT_LEN)
                text = wire.decode_text(raw) if raw else None
                if text:
                    texts.append(text)
                    segments.append({"type": "text", "text": text})
                continue
            label = CONTENT_TYPE_LABELS.get(ctype, f"消息{ctype}" if ctype is not None else "消息")
            texts.append(f"[{label}]")
            segments.append({"type": "other", "content_type": ctype, "label": f"[{label}]"})
        return "".join(texts), segments

    # 兜底：结构不同 / 老格式时，退化为递归抓 45101 文本（不再抓 URL 等元数据）
    try:
        texts = wire.extract_text_segments(blob, text_field=TEXT_FIELD, max_depth=8)
    except wire.WireError:
        texts = []
    segments = [{"type": "text", "text": t} for t in texts]
    return "".join(texts), segments


def iter_messages(
    conn: sqlite3.Connection,
    table: str,
    kind: str,
    columns: dict[str, str],
    *,
    include_content: bool = False,
    min_id: int | None = None,
) -> Iterator[dict]:
    names = list(columns.values())
    select = ", ".join(f'"{c}"' for c in names)
    sql = f'SELECT {select} FROM "{table}"'
    params: tuple = ()
    if min_id is not None and columns.get("msg_id"):
        sql += f' WHERE "{columns["msg_id"]}" > ?'
        params = (min_id,)
    sql += " ORDER BY rowid"
    cur = conn.execute(sql, params)
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
        if blob:
            media = extract_media(blob)
            if media:
                msg["media"] = media
        if include_content and blob:
            try:
                msg["raw"] = wire.to_dict(blob, max_depth=4)
            except Exception:  # noqa: BLE001
                msg["raw_hex"] = blob.hex()
        yield msg
