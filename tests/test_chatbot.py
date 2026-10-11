"""qq_chatbot 自测套件（不依赖 pytest，直接运行）：

    python tests/test_chatbot.py

覆盖：语料装载（明文库 + JSON 目录）、分轮、索引构建与持久化、混合检索、
人格档案、提示词、qa/persona 完整问答链路（mock LLM，全程不联网）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from make_fixture import build as build_fixture  # noqa: E402

from qq_chatbot.config import Config, load_config  # noqa: E402
from qq_chatbot.chat import Chatbot, load_chatbot  # noqa: E402
from qq_chatbot.index import (  # noqa: E402
    HybridRetriever,
    build_store,
    load_store,
    make_embedding,
    save_store,
)
from qq_chatbot.ingest import load_corpus  # noqa: E402
from qq_chatbot.llm import make_llm  # noqa: E402
from qq_chatbot.profile import build_profile  # noqa: E402
from qq_chatbot.prompt import build_persona_messages, build_qa_messages  # noqa: E402
from qq_chatbot.schema import Corpus, PersonaProfile, Turn  # noqa: E402
from qq_chatbot.turns import build_turns  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def make_db(tmp: Path) -> Path:
    return build_fixture(tmp / "fixture_plain.db")


def make_chatbot(store_dir: Path, corpus: Corpus) -> Chatbot:
    cfg = Config()
    cfg.store.dir = store_dir
    embed = make_embedding(cfg.embedding)
    store = build_store(corpus.turns, embed)
    save_store(store, store_dir, corpus.meta, embedding_provider="hash", dim=cfg.embedding.dim)
    llm = make_llm(cfg.llm)
    retriever = HybridRetriever(store, embed, cfg.retrieval)
    return Chatbot(cfg, retriever, llm, corpus=corpus, profiles={})


def test_ingest(tmp: Path) -> None:
    print("\n== 语料装载 ==")
    db = make_db(tmp)
    corpus = load_corpus(db=db)
    check("从明文库装载：8 轮", len(corpus.turns) == 8, str(len(corpus.turns)))
    keys = set(corpus.speakers)
    expected = {
        "me",
        "c2c:10001",
        "c2c:10002",
        "group:123456:10003",
        "group:123456:10004",
    }
    check("说话人解析完整", keys == expected, str(keys))

    # JSON 目录装载（与明文库结果一致）
    from qq_exporter.collect import build_conversations
    from qq_exporter.exporters import export_json

    meta, convs = build_conversations(db)
    json_dir = tmp / "json"
    export_json(convs, json_dir, meta)
    corpus2 = load_corpus(json_dir=json_dir)
    check("从 JSON 目录装载：轮数一致", len(corpus2.turns) == len(corpus.turns),
          str(len(corpus2.turns)))


def test_turns() -> None:
    print("\n== 分轮 ==")
    convs = [
        {
            "key": "c2c:10001",
            "kind": "c2c",
            "title": "张三",
            "group_id": None,
            "messages": [
                {"msg_id": 1, "time": 100, "direction": "in", "sender_qq": 10001, "text": "第一条"},
                {"msg_id": 2, "time": 200, "direction": "in", "sender_qq": 10001, "text": "第二条"},
                {"msg_id": 3, "time": 2000, "direction": "in", "sender_qq": 10001, "text": "隔很久"},
                {"msg_id": 4, "time": 2100, "direction": "out", "sender_qq": 8888, "text": "我回复"},
                {"msg_id": 5, "time": 2200, "direction": "in", "sender_qq": 10001, "text": ""},
            ],
        }
    ]
    turns = build_turns(convs, merge_gap_s=300)
    check("空文本消息被过滤", len(turns) == 3, str(len(turns)))
    check("连续同人合并为一轮", any("第一条\n第二条" in t.text for t in turns))
    check("超过间隔则分轮", sum(1 for t in turns if "隔很久" in t.text) == 1)

    people_turns = build_turns(convs, people={"c2c:10001"})
    check("people 过滤只留本人", all(t.speaker.key == "c2c:10001" for t in people_turns)
          and len(people_turns) == 2, str(len(people_turns)))


def test_store_and_retrieval(tmp: Path) -> None:
    print("\n== 索引与检索 ==")
    corpus = load_corpus(db=make_db(tmp))
    embed = make_embedding(load_config().embedding)
    store = build_store(corpus.turns, embed)
    check("构建索引轮数一致", len(store.turns) == len(corpus.turns))

    store_dir = tmp / ".chatbot"
    save_store(store, store_dir, corpus.meta, embedding_provider="hash", dim=256)
    store2, meta2, speakers2 = load_store(store_dir, embed)
    check("持久化往返轮数一致", len(store2.turns) == len(store.turns))

    retriever = HybridRetriever(store2, embed, load_config().retrieval)
    hits = retriever.retrieve("吃饭")
    check("检索命中「吃饭」", any("吃饭" in rt.turn.text for rt in hits))

    alice = retriever.retrieve("在吗", speaker="c2c:10001")
    check("说话人过滤生效", alice and all(rt.turn.speaker.key == "c2c:10001" for rt in alice))


def test_profile() -> None:
    print("\n== 人格档案 ==")
    corpus = load_corpus(db=make_db(Path(tempfile.mkdtemp(prefix="qqcb_"))))
    profile = build_profile(corpus, "c2c:10001")
    check("离线风格统计非空", bool(profile.style.get("catchphrases")) or profile.style.get("avg_sentence_len", 0) > 0)
    check("话题统计返回列表", isinstance(profile.topics, list))

    profile2 = build_profile(corpus, "不存在的人")
    check("无此人时返回空档案", profile2.speaker == "不存在的人" and not profile2.style)


def test_prompt() -> None:
    print("\n== 提示词 ==")
    from qq_chatbot.schema import RetrievedTurn

    turn = Turn(
        id="t000000", speaker=__import__("qq_chatbot.schema", fromlist=["Speaker"]).Speaker("c2c:10001", "张三"),
        conversation_key="c2c:10001", kind="c2c", time=100, time_str="2025-01-01 00:00:00",
        text="周末一起吃饭？", msg_ids=[1],
    )
    rt = RetrievedTurn(turn=turn, score=0.8)
    msgs = build_qa_messages("吃什么", [rt])
    check("qa 提示词含证据与出处", "[1] c2c:10001" in msgs[-1]["content"] and "周末一起吃饭" in msgs[-1]["content"])
    check("qa 系统提示为助手角色", msgs[0]["role"] == "system")

    profile = PersonaProfile(speaker="c2c:10001", style={"tone": "活泼", "catchphrases": ["哈哈"]})
    msgs2 = build_persona_messages("在吗", [rt], profile)
    check("persona 系统提示注入风格", "活泼" in msgs2[0]["content"] and "哈哈" in msgs2[0]["content"])


def test_answer(tmp: Path) -> None:
    print("\n== qa / persona 问答链路（mock） ==")
    corpus = load_corpus(db=make_db(tmp))
    bot = make_chatbot(tmp / ".chatbot", corpus)

    ans = bot.answer("周末一起吃饭", mode="qa")
    check("qa 有证据时 grounded", ans.grounded and ans.citations, str(ans.text))
    check("qa 回复前缀", ans.text.startswith("[qa]"), ans.text)

    ans2 = bot.answer("xyzzy", mode="qa")
    check("qa 无证据时拒绝回答", not ans2.grounded and "没有找到相关证据" in ans2.text, ans2.text)

    ans3 = bot.answer("在吗", mode="persona", person="c2c:10001")
    check("persona 回复前缀", ans3.text.startswith("[persona]"), ans3.text)

    try:
        bot.answer("你好", mode="persona")
        check("persona 缺人时报错", False)
    except ValueError:
        check("persona 缺人时报错", True)


def test_config(tmp: Path) -> None:
    print("\n== 配置 ==")
    cfg = load_config()
    check("默认 llm=mock", cfg.llm.provider == "mock")
    check("默认 embedding=hash", cfg.embedding.provider == "hash")

    cfg2 = load_config(overrides={"llm": {"provider": "ollama", "model": "qwen2.5:7b"}})
    check("覆盖 llm provider/model", cfg2.llm.provider == "ollama" and cfg2.llm.model == "qwen2.5:7b")

    import json
    p = tmp / "cfg.json"
    p.write_text(json.dumps({"embedding": {"provider": "openai", "dim": 1536}}), encoding="utf-8")
    cfg3 = load_config(p)
    check("JSON 配置加载", cfg3.embedding.provider == "openai" and cfg3.embedding.dim == 1536)


def test_redact():
    from qq_chatbot.redact import Redactor, RedactingLLM

    r = Redactor(["张三", "李四", "爱丽丝"])
    t = "张三和李四说：加我 1234567890，邮箱 test@example.com，手机 13900000000"
    rt = r.redact(t)
    check("脱敏替换昵称", "张三" not in rt and "李四" not in rt, rt)
    check("脱敏替换号码", "1234567890" not in rt and "13900000000" not in rt, rt)
    check("脱敏替换邮箱", "test@example.com" not in rt, rt)
    back = r.unredact(rt)
    check("还原昵称", "张三" in back and "李四" in back, back)
    check("不还原号码", "1234567890" not in back, back)

    class _Fake:
        name = "openai"

        def __init__(self):
            self.seen = None

        def chat(self, msgs, *, temperature=0.4, max_tokens=1024):
            self.seen = msgs
            return "和〔名0〕好"

    f = _Fake()
    w = RedactingLLM(f, Redactor(["张三"]))
    out = w.chat([{"role": "user", "content": "张三在吗"}])
    check("LLM 收到脱敏内容", "张三" not in f.seen[0]["content"], str(f.seen))
    check("LLM 回复还原昵称", "张三" in out, out)
    check("包装器保留 name", w.name == "openai", w.name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qqcb_test_"))
    try:
        test_ingest(tmp)
        test_turns()
        test_store_and_retrieval(tmp)
        test_profile()
        test_prompt()
        test_answer(tmp)
        test_config(tmp)
        test_redact()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*60}")
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
