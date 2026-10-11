"""聊天编排：检索 → 拼提示 → 生成 → 带出处的回答，以及交互式 REPL。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import warnings

from .config import Config
from .index import HybridRetriever, load_store, make_embedding
from .llm import make_llm
from .profile import build_profile, load_profiles, save_profile, profile_cache_key
from .prompt import build_persona_messages, build_qa_messages
from .schema import Answer, Citation, Corpus, PersonaProfile, speaker_qq
from .session import Session


class Chatbot:
    def __init__(
        self,
        config: Config,
        retriever,
        llm,
        corpus: Corpus | None = None,
        profiles: dict[str, PersonaProfile] | None = None,
    ) -> None:
        self.config = config
        self.retriever = retriever
        self.llm = llm
        self.corpus = corpus
        self.profiles = profiles or {}
        self._profile_lock = threading.RLock()
        self.memory = None
        self.traces = None
        if config.memory.enabled:
            from .memory import MemoryStore
            self.memory = MemoryStore(config.store.dir / "memory.sqlite3",
                                      half_life_days=config.memory.half_life_days,
                                      ttl_days=config.memory.ttl_days,
                                      max_entries_per_scope=config.memory.max_entries)
        if config.observability.enabled:
            from .observability import TraceLog
            self.traces = TraceLog(config.store.dir / "traces.jsonl",
                                   include_content=config.observability.include_content,
                                   max_bytes=config.observability.max_bytes)

    def create_session(self, key: str) -> Session:
        scope = json.dumps(["qq-chatbot", self.config.bridge.self_id, key], ensure_ascii=False)
        return Session(self.config.session.max_turns, memory_store=self.memory, scope=scope,
                       memory_top_k=self.config.memory.top_k)

    def _finish(self, query, mode, retrieved, answer, started):
        if self.traces:
            try:
                answer.trace_id = self.traces.record(query, mode, retrieved, answer,
                                                     (time.perf_counter() - started) * 1000)
            except (OSError, ValueError, sqlite3.Error):
                warnings.warn("回答追踪写入失败", RuntimeWarning)
        return answer

    def answer(
        self,
        query: str,
        *,
        mode: str = "qa",
        person: str | None = None,
        session: Session | None = None,
        conversation: str | None = None,
        kind: str | None = None,
        time_range: tuple[int, int] | None = None,
    ) -> Answer:
        started = time.perf_counter()
        if mode not in ("qa", "persona"):
            raise ValueError("mode 必须为 qa 或 persona")
        if mode == "persona":
            if not person:
                raise ValueError("persona 模式需要指定 --person <speaker_key>")
            # 用 QQ 号跨「私聊 + 群聊」匹配这个人（person 可能是 c2c:xxx 或 group:xxx:xxx）
            filters: dict = {"person_qq": speaker_qq(person)}
        else:
            filters = {}
        if conversation is not None:
            filters["conversation"] = conversation
        # 私聊人格可选「群聊知识融入、私聊语气保留」：检索不过滤 kind（融入群聊），人格档案仍用 c2c（保留语气）
        merge_into_private = (
            mode == "persona"
            and kind == "c2c"
            and self.config.bridge.merge_group_into_private
        )
        if kind is not None and not merge_into_private:
            filters["kind"] = kind
        if time_range is not None:
            filters["time_range"] = time_range

        retrieved = self.retriever.retrieve(query, **filters)
        history = session.as_messages(query=query) if session else None
        temp = self.config.llm.temperature
        max_tokens = self.config.llm.max_tokens

        if mode == "qa":
            good = [rt for rt in retrieved if rt.score >= self.config.retrieval.min_score]
            if not good:
                return self._finish(query, mode, retrieved, Answer("记录里没有找到相关证据。", [], grounded=False), started)
            msgs = build_qa_messages(query, good, history)
            text = self.llm.chat(msgs, temperature=temp, max_tokens=max_tokens)
            return self._finish(query, mode, good, Answer(text, [self._cite(rt) for rt in good], grounded=True), started)

        # persona
        # 按 (person, kind) 缓存，支持「私聊人格 / 群聊人格」分离
        cache_key = profile_cache_key(person, kind, conversation)
        with self._profile_lock:
            profile = self.profiles.get(cache_key)
            if self.corpus is not None:
                profile = build_profile(self.corpus, person, self.llm, kind=kind,
                                        conversation=conversation, previous=profile)
                save_profile(self.config.store.dir, profile, kind=kind, conversation=conversation)
                self.profiles[cache_key] = profile
        profile = profile or PersonaProfile(speaker=person)
        if self.config.bridge.persona_name:
            profile.name = self.config.bridge.persona_name
        msgs = build_persona_messages(query, retrieved, profile, history)
        text = self.llm.chat(msgs, temperature=temp, max_tokens=max_tokens)
        good = [rt for rt in retrieved if rt.score >= self.config.retrieval.min_score]
        return self._finish(query, mode, retrieved,
                            Answer(text, [self._cite(rt) for rt in good[:5]], grounded=bool(good)), started)

    @staticmethod
    def _cite(rt) -> Citation:
        t = rt.turn
        excerpt = t.text if len(t.text) <= 80 else t.text[:80] + "…"
        return Citation(
            conversation_key=t.conversation_key,
            time_str=t.time_str,
            speaker=t.speaker.name,
            text=excerpt,
            score=rt.score,
        )


def load_chatbot(config: Config) -> Chatbot:
    embed = make_embedding(config.embedding)
    store, meta, speakers = load_store(config.store.dir, embed)
    llm = make_llm(config.llm)
    retriever = HybridRetriever(store, embed, config.retrieval)
    corpus = Corpus(speakers=speakers, turns=store.turns, meta=meta)
    # 发送前脱敏：把语料里的真实昵称/QQ号/手机/邮箱替换成占位符，回复后再还原昵称
    if config.llm.redact:
        from .redact import Redactor, RedactingLLM
        names = [s.name for s in corpus.speakers.values()]
        if config.bridge.persona_name:
            names.append(config.bridge.persona_name)
        llm = RedactingLLM(llm, Redactor(names))
    profiles = load_profiles(config.store.dir)
    return Chatbot(config, retriever, llm, corpus=corpus, profiles=profiles)


def run_chat(bot: Chatbot, mode: str, person: str | None, session_key: str | None = None) -> None:
    def new_session():
        return bot.create_session(json.dumps(["cli", session_key, mode, person])) if session_key else Session(bot.config.session.max_turns)
    session = new_session()
    last_citations: list[Citation] = []
    print(f"模式：{'数字分身' if mode == 'persona' else '历史问答'}"
          + (f"（{person}）" if person else ""))
    print("输入 /persona <key> 切换分身，/qa 切换问答，/cite 显示上一条出处，/quit 退出。")
    while True:
        try:
            query = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.startswith("/"):
            parts = query[1:].strip().split()
            cmd = parts[0].lower()
            if cmd in ("quit", "exit", "q"):
                break
            if cmd == "persona" and len(parts) >= 2:
                mode, person = "persona", parts[1]
                session, last_citations = new_session(), []
                print(f"已切换到数字分身：{person}")
                continue
            if cmd == "qa":
                mode, person = "qa", None
                session, last_citations = new_session(), []
                print("已切换到历史问答")
                continue
            if cmd == "forget":
                if session.memory_store is not None:
                    session.memory_store.delete(session.scope)
                session.history.clear()
                last_citations = []
                print("已清除当前身份和模式的记忆。")
                continue
            if cmd == "cite":
                if not last_citations:
                    print("（尚无出处）")
                for c in last_citations:
                    print(f"  · {c.conversation_key} {c.time_str} {c.speaker}: {c.text}")
                continue
            print("未知命令：/persona /qa /cite /quit")
            continue

        try:
            ans = bot.answer(query, mode=mode, person=person, session=session)
        except ValueError as exc:
            print(f"错误：{exc}")
            continue
        print(f"bot> {ans.text}")
        if ans.citations:
            last_citations = ans.citations
            print("  （出处）")
            for c in ans.citations:
                print(f"    · {c.conversation_key} {c.time_str} {c.speaker}: {c.text}")
        session.add(query, ans.text)
