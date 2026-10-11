"""把消息按会话归类，并导出为 JSON / 纯 TXT。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .db import Conversation

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, fallback: str, limit: int = 60) -> str:
    """把会话名转成安全的文件名。"""
    name = unicodedata.normalize("NFC", name or "").strip()
    name = _ILLEGAL.sub("_", name)
    name = name.strip(". ")
    if not name:
        name = fallback
    return name[:limit]


def conversation_id(conversation: Conversation) -> str:
    return conversation.key


# --------------------------------------------------------------------------
# 导出：JSON
# --------------------------------------------------------------------------


def build_index(meta: dict, conversations: list[Conversation]) -> dict:
    entries = []
    for conv in conversations:
        times = [m["time"] for m in conv.messages if m.get("time")]
        entries.append(
            {
                "id": conv.key,
                "kind": conv.kind,
                "title": conv.title,
                "group_id": conv.group_id,
                "peer_qq": conv.peer_qq,
                "peer_uid": conv.peer_uid,
                "message_count": conv.count,
                "first_time": min(times) if times else None,
                "last_time": max(times) if times else None,
                "file": None,
            }
        )
    return {"meta": meta, "conversations": entries}


def export_json(
    conversations: list[Conversation],
    out_dir: Path,
    meta: dict,
    *,
    single: bool = False,
    include_segments: bool = True,
) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index = build_index(meta, conversations)

    for i, entry in enumerate(index["conversations"]):
        conv = conversations[i]
        stem = f"{i:03d}_{safe_name(conv.title, conv.key)}"
        entry["file"] = f"{stem}.json"

    if single:
        payload = {
            "meta": meta,
            "conversations": [
                {
                    **entry,
                    "file": None,
                    "messages": [_clean(m, include_segments) for m in conv.messages],
                }
                for entry, conv in zip(index["conversations"], conversations)
            ],
        }
        path = out_dir / "conversations.json"
        _write_json(path, payload)
        index["file"] = path.name
    else:
        for entry, conv in zip(index["conversations"], conversations):
            payload = {
                "conversation": {k: v for k, v in entry.items() if k != "file"},
                "messages": [_clean(m, include_segments) for m in conv.messages],
            }
            _write_json(out_dir / entry["file"], payload)

    _write_json(out_dir / "index.json", index)
    return index


def _clean(msg: dict, include_segments: bool) -> dict:
    out = dict(msg)
    if not include_segments:
        out.pop("segments", None)
    return out


def _write_json(path: Path, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 导出：纯 TXT
# --------------------------------------------------------------------------


def export_txt(
    conversations: list[Conversation],
    out_dir: Path,
    meta: dict,
    *,
    width: int = 78,
) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index = build_index(meta, conversations)

    for i, entry in enumerate(index["conversations"]):
        conv = conversations[i]
        stem = f"{i:03d}_{safe_name(conv.title, conv.key)}"
        entry["file"] = f"{stem}.txt"
        lines = [
            "=" * width,
            f"会话: {conv.title}",
            f"类型: {'群聊' if conv.kind == 'group' else '私聊'}",
            f"标识: {conv.key}",
            f"消息数: {conv.count}",
            "=" * width,
            "",
        ]
        for msg in conv.messages:
            who = msg.get("sender_name") or msg.get("sender_qq") or msg.get("sender_uid") or "?"
            arrow = "->" if msg.get("direction") == "out" else "<-"
            lines.append(f"[{msg.get('time_str') or '未知时间'}] {arrow} {who}")
            text = (msg.get("text") or "").rstrip()
            if not text:
                text = _describe_non_text(msg)
            for line in text.splitlines() or [""]:
                lines.append(f"    {line}")
            lines.append("")
        path = out_dir / entry["file"]
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))

    _write_index_txt(out_dir / "_index.txt", meta, index, width)
    return index


def _describe_non_text(msg: dict) -> str:
    segments = msg.get("segments") or []
    kinds = [s.get("type", "?") for s in segments]
    if kinds:
        return f"（{'+'.join(kinds)}）"
    return "（非文本消息）"


def _write_index_txt(path: Path, meta: dict, index: dict, width: int) -> None:
    lines = [
        "=" * width,
        "QQ 聊天记录导出索引",
        "=" * width,
        f"导出时间: {meta.get('generated_at')}",
        f"来源账号: {meta.get('account')}",
        f"会话数量: {len(index['conversations'])}",
        f"消息总数: {meta.get('message_count')}",
        "",
    ]
    for entry in index["conversations"]:
        first = entry.get("first_time")
        last = entry.get("last_time")
        rng = ""
        if first and last:
            rng = f"  {_ts(first)} ~ {_ts(last)}"
        lines.append(
            f"{entry['file']:<50} {entry['message_count']:>7} 条{rng}"
        )
    lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def _ts(value: int | None) -> str:
    if not value:
        return "?"
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"
