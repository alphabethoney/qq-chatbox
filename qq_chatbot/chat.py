"""聊天编排：检索 → 拼提示 → 生成 → 带出处的回答，以及交互式 REPL。"""

from __future__ import annotations

from .config import Config
from .index import HybridRetriever, load_store, make_embedding
from .llm import make_llm
from .profile import build_profile, load_profiles
from .prompt import build_persona_messages, build_qa_messages
from .schema import Answer, Citation, Corpus, PersonaProfile
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

    def answer(
        self,
        query: str,
        *,
        mode: str = "qa",
        person: str | None = None,
        session: Session | None = None,
    ) -> Answer:
        if mode == "persona":
            if not person:
                raise ValueError("persona 模式需要指定 --person <speaker_key>")
            filters: dict = {"speaker": person}
        else:
            filters = {}

        retrieved = self.retriever.retrieve(query, **filters)
        history = session.as_messages() if session else None
        temp = self.config.llm.temperature
        max_tokens = self.config.llm.max_tokens

        if mode == "qa":
            good = [rt for rt in retrieved if rt.score >= self.config.retrieval.min_score]
            if not good:
                return Answer("记录里没有找到相关证据。", [], grounded=False)
            msgs = build_qa_messages(query, good, history)
            text = self.llm.chat(msgs, temperature=temp, max_tokens=max_tokens)
            return Answer(text, [self._cite(rt) for rt in good], grounded=True)

        # persona
        profile = self.profiles.get(person)
        if profile is None and self.corpus is not None:
            profile = build_profile(self.corpus, person, self.llm)
            self.profiles[person] = profile
        profile = profile or PersonaProfile(speaker=person)
        msgs = build_persona_messages(query, retrieved, profile, history)
        text = self.llm.chat(msgs, temperature=temp, max_tokens=max_tokens)
        return Answer(text, [self._cite(rt) for rt in retrieved[:5]], grounded=True)

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
    profiles = load_profiles(config.store.dir)
    return Chatbot(config, retriever, llm, corpus=corpus, profiles=profiles)


def run_chat(bot: Chatbot, mode: str, person: str | None) -> None:
    session = Session(bot.config.session.max_turns)
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
                print(f"已切换到数字分身：{person}")
                continue
            if cmd == "qa":
                mode, person = "qa", None
                print("已切换到历史问答")
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
