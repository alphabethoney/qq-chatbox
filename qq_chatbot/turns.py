"""归一化与分轮：说话人解析、连续合并、超长切块、过滤。

输入是「会话字典列表」：``{"key", "kind", "title", "group_id", "messages": [...]}``，
``messages`` 与导出器 ``db.iter_messages`` 产出的消息字典同构。
"""

from __future__ import annotations

from .schema import Speaker, Turn, speaker_qq

# 非文本消息：text 为空（图片/文件/语音等）默认不入索引
_EMPTY_TEXT_PLACEHOLDER = ""


def build_turns(
    conversations: list[dict],
    *,
    merge_gap_s: int = 300,
    max_turn_chars: int = 1200,
    since: int | None = None,
    people: set[str] | None = None,
    conversation_keys: set[str] | None = None,
    person_qq: str | None = None,
) -> list[Turn]:
    """把消息聚合成 ``Turn`` 列表。

    - 过滤：无文本内容的消息跳过；``since`` 之前、不在 ``people``/``conversation_keys``/``person_qq`` 内的跳过。
    - ``person_qq``：只保留该 QQ 号这个人（跨私聊 + 所有群）的发言。
    - 合并：同一会话同一人、间隔 ≤ ``merge_gap_s`` 的连续消息合并为一轮。
    - 切块：超过 ``max_turn_chars`` 的轮按字符切块（带少量 overlap）。
    """
    turns: list[Turn] = []
    counter = 0

    for conv in conversations:
        kind = conv.get("kind") or ("group" if "group" in str(conv.get("key")) else "c2c")
        conv_key = conv.get("key") or ""
        if conversation_keys is not None and conv_key not in conversation_keys:
            continue
        msgs = sorted(conv.get("messages", []), key=lambda m: (m.get("time") or 0, m.get("msg_id") or 0))

        cur: dict | None = None  # 当前正在聚合的轮
        for msg in msgs:
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            ts = msg.get("time") or 0
            if since is not None and ts < since:
                continue
            speaker = _resolve_speaker(conv, kind, msg)
            if people is not None and speaker.key not in people:
                continue
            if person_qq is not None and speaker_qq(speaker.key) != person_qq:
                continue

            if (
                cur is not None
                and cur["conv_key"] == conv_key
                and cur["speaker"].key == speaker.key
                and ts - cur["time_end"] <= merge_gap_s
            ):
                cur["texts"].append(text)
                cur["time_end"] = max(cur["time_end"], ts)
                cur["msg_ids"].append(msg.get("msg_id"))
            else:
                if cur is not None:
                    counter = _flush(cur, turns, counter, max_turn_chars)
                cur = {
                    "conv_key": conv_key,
                    "kind": kind,
                    "speaker": speaker,
                    "time_start": ts,
                    "time_end": ts,
                    "texts": [text],
                    "msg_ids": [msg.get("msg_id")],
                }
        if cur is not None:
            _flush(cur, turns, counter, max_turn_chars)

    turns.sort(key=lambda t: (t.time, t.id))
    return turns


def _resolve_speaker(conv: dict, kind: str, msg: dict) -> Speaker:
    # 本人统一用 "me"（私聊与群聊一致），persona 不针对本人
    if msg.get("direction") == "out":
        return Speaker(key="me", name="我", is_self=True)

    if kind == "group":
        gid = conv.get("group_id")
        if gid is None:
            gid = str(conv.get("key", "")).split(":", 1)[-1]
        ident = msg.get("sender_qq")
        if ident is None:
            ident = msg.get("sender_uid")
        key = f"group:{gid}:{ident}"
        name = str(ident or "未知")
        return Speaker(key=key, name=name, is_self=False)

    # 私聊：收到的是对方
    ident = msg.get("sender_qq")
    if ident is None:
        ident = msg.get("sender_uid")
    name = conv.get("title") or str(ident or "未知")
    return Speaker(key=f"c2c:{ident}", name=name, is_self=False)


def _chunks(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    step = max(1, max_chars - 40)  # 40 字 overlap
    return [text[i : i + max_chars] for i in range(0, len(text), step)]


def _flush(cur: dict, turns: list[Turn], counter: int, max_chars: int) -> int:
    full = "\n".join(cur["texts"])
    chunks = _chunks(full, max_chars)
    for ci, chunk in enumerate(chunks):
        tid = f"t{counter + ci:06d}"
        turns.append(
            Turn(
                id=tid,
                speaker=cur["speaker"],
                conversation_key=cur["conv_key"],
                kind=cur["kind"],
                time=cur["time_start"],
                time_str=_fmt(cur["time_start"]),
                text=chunk,
                msg_ids=list(cur["msg_ids"]),
            )
        )
    return counter + len(chunks)


def _fmt(ts: int) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""
