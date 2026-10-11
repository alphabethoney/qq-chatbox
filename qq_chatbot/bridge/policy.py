"""桥接策略：把 OneBot11 事件翻译成机器人回答与发消息动作。

本模块是**纯逻辑**（不依赖网络），便于单元测试；传输层在 ``onebot.py``。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass

from ..chat import Chatbot
from ..config import Config
from ..session import Session

_AT = re.compile(r"\[CQ:at,qq=\d+\]")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")


def _split_sentences(text: str, max_n: int = 6) -> list[str]:
    """把回复按句号/感叹号/问号等切句，逐条发出（更像真人）。"""
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    return parts[:max_n] if parts else [text]


def _text_of(event: dict) -> str:
    """从事件里取出纯文本，兼容 array / string 两种 messagePostFormat。"""
    msg = event.get("message")
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append((seg.get("data") or {}).get("text", ""))
        return "".join(parts)
    if isinstance(msg, str):
        return _AT.sub("", msg).strip()
    return _AT.sub("", str(event.get("raw_message") or "")).strip()


def _at_self(event: dict, self_id: int) -> bool:
    """判断是否 @ 了机器人（array 格式看 at 段，string 格式看 CQ 码）。"""
    if not self_id:
        return False
    msg = event.get("message")
    if isinstance(msg, list):
        return any(
            isinstance(s, dict) and s.get("type") == "at"
            and str((s.get("data") or {}).get("qq", "")) == str(self_id)
            for s in msg
        )
    return f"[CQ:at,qq={self_id}]" in str(event.get("raw_message") or "")


@dataclass
class Outbound:
    action: str
    params: dict
    echo: str = ""


class BridgeRuntime:
    """持有聊天机器人 + 桥接配置 + 每个会话的运行时状态。"""

    def __init__(self, chatbot: Chatbot, config: Config) -> None:
        self.bot = chatbot
        self.cfg = config.bridge
        self.modes: dict[str, str] = {}
        self.persons: dict[str, str] = {}
        self.persona_kinds: dict[str, str] = {}   # 每会话人格类型覆盖：c2c / group
        self.sessions: dict[str, Session] = {}
        self.last_cites: dict[str, list] = {}
        self._hits: dict[str, list[float]] = defaultdict(list)

    # ---- 主入口 -----------------------------------------------------------

    def handle(self, event: dict, self_id: int = 0) -> list[Outbound]:
        if event.get("post_type") != "message":
            return []
        msg_type = event.get("message_type")
        user_id = event.get("user_id")

        sid = self.cfg.self_id or self_id
        if sid and user_id == sid:
            return []  # 忽略机器人自己发的消息

        group_id = None
        if msg_type == "group":
            group_id = event.get("group_id")
            if not self._should_reply_group(event, sid):
                return []
            key = f"group:{group_id}"
            text = _text_of(event)
            if not text:
                text = "你好"
        elif msg_type == "private":
            text = _text_of(event)
            if not text:
                return []
            key = f"private:{user_id}"
        else:
            return []

        parts = self._parse_command(text)
        if parts is not None:
            return self._handle_command(key, parts, group_id, user_id, msg_type)

        if not self._allow(key):
            return []

        mode, person = self._resolve_mode(key)
        if mode == "persona" and not person:
            return [self._reply(group_id, user_id, msg_type,
                               "persona 模式需要先指定说话人：发送 /persona <key>")]

        session = self.sessions.setdefault(key, Session(self.bot.config.session.max_turns))
        # persona 模式：优先用会话内 /kind 覆盖，否则私聊用「私聊人格」、群聊用「群聊人格」
        if mode == "persona":
            override = self.persona_kinds.get(key)
            persona_kind = override if override in ("c2c", "group") else ("group" if msg_type == "group" else "c2c")
        else:
            persona_kind = None
        try:
            if msg_type == "group" and self.cfg.restrict_group_scope:
                ans = self.bot.answer(text, mode=mode, person=person, session=session,
                                      kind=persona_kind, conversation=f"group:{group_id}")
            else:
                ans = self.bot.answer(text, mode=mode, person=person, session=session,
                                      kind=persona_kind)
        except ValueError as exc:
            return [self._reply(group_id, user_id, msg_type, f"错误：{exc}")]

        session.add(text, ans.text)
        if ans.citations:
            self.last_cites[key] = ans.citations
        if self.cfg.split_sentences:
            return [self._reply(group_id, user_id, msg_type, s)
                    for s in _split_sentences(ans.text, self.cfg.max_sentences)]
        return [self._reply(group_id, user_id, msg_type, ans.text)]

    # ---- 内部 -------------------------------------------------------------

    def _should_reply_group(self, event: dict, self_id: int) -> bool:
        if self.cfg.group_trigger == "all":
            return True
        return _at_self(event, self_id)

    @staticmethod
    def _parse_command(text: str) -> list[str] | None:
        if not text.startswith("/"):
            return None
        parts = text[1:].strip().split()
        return parts or None

    def _resolve_mode(self, key: str) -> tuple[str, str]:
        return self.modes.get(key, self.cfg.mode), self.persons.get(key, self.cfg.persona_person)

    def _allow(self, key: str) -> bool:
        if self.cfg.rate_limit_per_min <= 0:
            return True
        now = time.time()
        q = self._hits[key]
        q[:] = [t for t in q if now - t < 60.0]
        if len(q) >= self.cfg.rate_limit_per_min:
            return False
        q.append(now)
        return True

    def _reply(self, group_id, user_id, msg_type: str, text: str) -> Outbound:
        if msg_type == "group":
            return Outbound("send_group_msg", {"group_id": group_id, "message": text})
        return Outbound("send_private_msg", {"user_id": user_id, "message": text})

    def _handle_command(self, key, parts, group_id, user_id, msg_type) -> list[Outbound]:
        cmd = parts[0].lower()

        def reply(t: str) -> list[Outbound]:
            return [self._reply(group_id, user_id, msg_type, t)]

        if cmd in ("qa", "问答"):
            self.modes[key] = "qa"
            return reply("已切换到历史问答模式。")
        if cmd == "persona":
            if len(parts) < 2:
                return reply("用法：/persona <说话人key>，例如 /persona c2c:10001")
            self.modes[key] = "persona"
            self.persons[key] = parts[1]
            return reply(f"已切换到数字分身：{parts[1]}")
        if cmd == "kind":
            if len(parts) < 2:
                return reply("用法：/kind c2c | group | auto")
            v = parts[1].lower()
            if v in ("c2c", "私聊", "private"):
                self.persona_kinds[key] = "c2c"
                return reply("已切换为【私聊人格】")
            if v in ("group", "群聊"):
                self.persona_kinds[key] = "group"
                return reply("已切换为【群聊人格】")
            if v in ("auto", "自动"):
                self.persona_kinds.pop(key, None)
                return reply("已切换为自动（私聊走私聊人格、群聊走群聊人格）")
            return reply("未知：/kind c2c | group | auto")
        if cmd in ("cite", "出处"):
            cites = self.last_cites.get(key)
            if not cites:
                return reply("（尚无出处）")
            lines = [f"· {c.conversation_key} {c.time_str} {c.speaker}: {c.text}" for c in cites]
            return reply("上一条出处：\n" + "\n".join(lines))
        if cmd in ("search", "检索"):
            if len(parts) < 2:
                return reply("用法：/search <查询词>")
            q = " ".join(parts[1:])
            results = self.bot.retriever.retrieve(q, top_k=5)
            if not results:
                return reply("没有检索到相关记录")
            lines = [f"[{r.score:.2f}] {r.turn.speaker.name} {r.turn.time_str}: {r.turn.text[:36]}" for r in results]
            return reply("检索命中：\n" + "\n".join(lines))
        if cmd in ("help", "帮助"):
            return reply("命令：/qa 历史问答 · /persona <key> 数字分身 · /kind c2c|group|auto 切人格 · /cite 出处 · /search 检索 · /help")
        return reply(f"未知命令：{cmd}（/help 查看）")
