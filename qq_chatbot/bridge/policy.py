"""桥接策略：把 OneBot11 事件翻译成机器人回答与发消息动作。

本模块是**纯逻辑**（不依赖网络），便于单元测试；传输层在 ``onebot.py``。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass

from ..chat import Chatbot
from ..config import Config
from ..session import Session
from ..schema import speaker_qq

_AT = re.compile(r"\[CQ:at,qq=(\d+|all)(?:,[^\]]*)?\]")
# 拆句：优先按换行(回车)，其次按句末标点（保留标点）
_SENT_SPLIT = re.compile(r"\n+|(?<=[。！？!?；;])")


def _split_sentences(text: str, max_n: int = 6) -> list[str]:
    """把回复按换行/句末标点拆成多条，逐条发出（更像真人）。"""
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


def _mentions(event: dict) -> tuple[str, ...]:
    """Mention targets are context, never proof of membership or authority."""
    msg = event.get("message")
    if isinstance(msg, list):
        values = [str((seg.get("data") or {}).get("qq", ""))
                  for seg in msg if isinstance(seg, dict) and seg.get("type") == "at"]
    else:
        values = _AT.findall(msg if isinstance(msg, str) else str(event.get("raw_message") or ""))
    return tuple(dict.fromkeys(v for v in values if v.isdigit() or v == "all"))


def _at_self(event: dict, self_id: int) -> bool:
    return bool(self_id) and str(self_id) in _mentions(event)


@dataclass(frozen=True)
class EventContext:
    sender_id: str
    sender_name: str
    group_id: str | None
    mentions: tuple[str, ...]
    sender_is_member: bool | None


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
        self._members: dict[str, tuple[float, dict[str, dict]]] = {}
        self.event_context: dict[str, EventContext] = {}

    def group_members(self, group_id) -> dict[str, dict] | None:
        cached = self._members.get(str(group_id))
        ttl = float(getattr(self.cfg, "group_member_cache_ttl", 300))
        if cached is None or time.monotonic() - cached[0] >= ttl:
            return None
        return cached[1]

    def update_group_members(self, group_id, members: list[dict]) -> bool:
        """Accept a complete OneBot roster; malformed responses never authorize."""
        if not isinstance(members, list) or any(
            not isinstance(m, dict) or not str(m.get("user_id", "")).isdigit()
            or (m.get("group_id") is not None and str(m["group_id"]) != str(group_id))
            for m in members
        ):
            return False
        self._members[str(group_id)] = (time.monotonic(), {str(m["user_id"]): dict(m) for m in members})
        return True

    def invalidate_group_members(self, group_id) -> None:
        self._members.pop(str(group_id), None)

    def _reset_context(self, key: str) -> None:
        self.sessions.pop(key, None)
        self.last_cites.pop(key, None)

    def _session(self, key: str, msg_type: str) -> Session:
        if key not in self.sessions:
            mode, person = self._resolve_mode(key)
            kind = "group" if msg_type == "group" and self.cfg.restrict_group_scope else self._kind(key, msg_type)
            factory = getattr(self.bot, "create_session", None)
            scope = json.dumps([key, mode, person if mode == "persona" else "", kind or ""], ensure_ascii=False)
            self.sessions[key] = factory(scope) if callable(factory) else Session(self.bot.config.session.max_turns)
        return self.sessions[key]

    def _kind(self, key: str, msg_type: str) -> str | None:
        mode, _ = self._resolve_mode(key)
        if mode != "persona":
            return None
        return self.persona_kinds.get(key) or ("group" if msg_type == "group" else "c2c")

    def _scope(self, key: str, msg_type: str, group_id) -> dict:
        mode, person = self._resolve_mode(key)
        kind = self._kind(key, msg_type)
        scope = {}
        if mode == "persona" and person:
            scope["person_qq"] = speaker_qq(person)
        if kind is not None:
            scope["kind"] = kind
        if msg_type == "group" and self.cfg.restrict_group_scope:
            scope.update(kind="group", conversation=f"group:{group_id}")
        return scope

    # ---- 主入口 -----------------------------------------------------------

    def handle(self, event: dict, self_id: int = 0) -> list[Outbound]:
        if event.get("post_type") != "message":
            return []
        msg_type = event.get("message_type")
        user_id = event.get("user_id")
        if not str(user_id).isdigit():
            return []
        sender = event.get("sender") or {}
        if not isinstance(sender, dict) or (sender.get("user_id") is not None
                                          and str(sender["user_id"]) != str(user_id)):
            return []

        sid = self.cfg.self_id or self_id or event.get("self_id", 0)
        if sid and str(user_id) == str(sid):
            return []  # 忽略机器人自己发的消息

        group_id = None
        if msg_type == "group":
            group_id = event.get("group_id")
            if not str(group_id).isdigit():
                return []
            if not self._should_reply_group(event, sid):
                return []
            key = f"bot:{sid}:group:{group_id}:user:{user_id}"
            text = _text_of(event)
            if not text:
                text = "你好"
        elif msg_type == "private":
            text = _text_of(event)
            if not text:
                return []
            key = f"bot:{sid}:private:{user_id}"
        else:
            return []

        roster = self.group_members(group_id) if group_id is not None else None
        is_member = str(user_id) in roster if roster is not None else None
        member = (roster or {}).get(str(user_id), {})
        self.event_context[key] = EventContext(
            str(user_id), str(member.get("card") or member.get("nickname")
                              or sender.get("card") or sender.get("nickname") or user_id),
            str(group_id) if group_id is not None else None,
            tuple(m for m in _mentions(event) if m != str(sid)), is_member,
        )
        if msg_type == "group" and getattr(self.cfg, "group_member_check", False) and is_member is not True:
            self._reset_context(key)
            message = "群成员身份尚未确认，请稍后重试。" if is_member is None else "拒绝访问：提问者不在本群成员列表中。"
            return [self._reply(group_id, user_id, msg_type, message)]

        parts = self._parse_command(text.strip())
        if parts is not None:
            return self._handle_command(key, parts, group_id, user_id, msg_type)

        if not self._allow(key):
            return []

        mode, person = self._resolve_mode(key)
        if mode == "persona" and not person:
            return [self._reply(group_id, user_id, msg_type,
                               "persona 模式需要先指定说话人：发送 /persona <key>")]

        if mode == "persona" and msg_type == "group":
            if (self.cfg.restrict_group_scope and person.startswith("group:")
                    and not person.startswith(f"group:{group_id}:")):
                self._reset_context(key)
                return [self._reply(group_id, user_id, msg_type, "拒绝跨群访问：只能使用本群的说话人。")]
            if (getattr(self.cfg, "group_member_check", False)
                    and speaker_qq(person) not in (roster or {})):
                self._reset_context(key)
                return [self._reply(group_id, user_id, msg_type, "拒绝访问：指定的说话人不在本群成员列表中。")]
        persona_kind = self._kind(key, msg_type)
        if msg_type == "group" and self.cfg.restrict_group_scope:
            persona_kind = "group"
        session = self._session(key, msg_type)
        self.last_cites.pop(key, None)
        try:
            if msg_type == "group" and self.cfg.restrict_group_scope:
                ans = self.bot.answer(text, mode=mode, person=person, session=session,
                                      kind=persona_kind, conversation=f"group:{group_id}")
            else:
                ans = self.bot.answer(text, mode=mode, person=person, session=session,
                                      kind=persona_kind)
        except ValueError as exc:
            return [self._reply(group_id, user_id, msg_type, f"错误：{exc}")]

        memory_warning = None
        try:
            session.add(text, ans.text)
        except (sqlite3.Error, OSError):
            memory_warning = "记忆保存失败，本次对话未写入记忆，请稍后重试。"
        if ans.citations:
            self.last_cites[key] = ans.citations
        texts = _split_sentences(ans.text, self.cfg.max_sentences) if self.cfg.split_sentences else [ans.text]
        if memory_warning:
            texts.append(memory_warning)
        return [self._reply(group_id, user_id, msg_type, t) for t in texts]

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

        if cmd in ("forget", "忘记"):
            session = self._session(key, msg_type)
            store = getattr(session, "memory_store", None)
            if store is not None:
                try:
                    store.delete(session.scope)
                except (sqlite3.Error, OSError):
                    self._reset_context(key)
                    return reply("长期记忆删除失败，当前上下文与出处已清除，请稍后重试。")
            self._reset_context(key)
            return reply("已清除当前会话人格的记忆与出处。")
        if cmd in ("qa", "问答"):
            self._reset_context(key)
            self.modes[key] = "qa"
            return reply("已切换到历史问答模式。")
        if cmd == "persona":
            if len(parts) < 2:
                return reply("用法：/persona <说话人key>，例如 /persona c2c:10001")
            if (msg_type == "group" and self.cfg.restrict_group_scope
                    and parts[1].startswith("group:")
                    and not parts[1].startswith(f"group:{group_id}:")):
                return reply("拒绝跨群访问：只能使用本群的说话人。")
            self._reset_context(key)
            self.modes[key] = "persona"
            self.persons[key] = parts[1]
            return reply(f"已切换到数字分身：{parts[1]}")
        if cmd == "kind":
            if len(parts) < 2:
                return reply("用法：/kind c2c | group | auto")
            v = parts[1].lower()
            if (msg_type == "group" and self.cfg.restrict_group_scope
                    and v in ("c2c", "私聊", "private")):
                return reply("拒绝越权访问：群聊只能使用本群记录。")
            if v in ("c2c", "私聊", "private", "group", "群聊", "auto", "自动"):
                self._reset_context(key)
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
            self.last_cites.pop(key, None)
            scope = self._scope(key, msg_type, group_id)
            explicit = {}
            query_parts = []
            i = 1
            while i < len(parts):
                token = parts[i]
                if token.startswith("--"):
                    option, sep, value = token[2:].partition("=")
                    if option not in ("kind", "person", "conversation"):
                        return reply(f"未知检索选项：{option}")
                    if not sep:
                        i += 1
                        if i >= len(parts) or parts[i].startswith("--"):
                            return reply(f"检索选项缺少值：{option}")
                        value = parts[i]
                    if not value:
                        return reply(f"检索选项缺少值：{option}")
                    explicit[option] = value
                else:
                    query_parts.append(token)
                i += 1
            if not query_parts:
                return reply("用法：/search <查询词> [--kind c2c|group] [--person key] [--conversation key]")
            if explicit.get("kind", "group") not in ("c2c", "group"):
                return reply("未知检索类型：kind 只能是 c2c 或 group")
            if msg_type == "group" and self.cfg.restrict_group_scope:
                if (explicit.get("kind", "group") != "group"
                        or explicit.get("conversation", f"group:{group_id}") != f"group:{group_id}"
                        or (explicit.get("person", "").startswith("group:")
                            and not explicit["person"].startswith(f"group:{group_id}:"))):
                    return reply("拒绝跨群或越权检索：只能检索本群记录。")
            for name in ("kind", "conversation"):
                if name in explicit:
                    scope[name] = explicit[name]
            if "person" in explicit:
                scope["person_qq"] = speaker_qq(explicit["person"])
            else:
                context = self.event_context.get(key)
                mentions = context.mentions if context else ()
                if len(mentions) == 1 and mentions[0] != "all" and "person_qq" not in scope:
                    scope["person_qq"] = mentions[0]
            # A mention can narrow an authorized search but cannot bypass the sender check.
            if msg_type == "group" and getattr(self.cfg, "group_member_check", False):
                person_qq = scope.get("person_qq")
                if person_qq and person_qq not in (self.group_members(group_id) or {}):
                    return reply("拒绝访问：指定的说话人不在本群成员列表中。")
            q = " ".join(query_parts)
            results = self.bot.retriever.retrieve(q, top_k=5, **scope)
            if not results:
                return reply("没有检索到相关记录")
            lines = [f"[{r.score:.2f}] {r.turn.speaker.name} {r.turn.time_str}: {r.turn.text[:36]}" for r in results]
            return reply("检索命中：\n" + "\n".join(lines))
        if cmd in ("help", "帮助"):
            return reply("命令：/qa 历史问答 · /persona <key> 数字分身 · /kind c2c|group|auto 切人格 · /cite 出处 · /search 检索 · /forget 忘记当前记忆 · /help")
        return reply(f"未知命令：{cmd}（/help 查看）")
