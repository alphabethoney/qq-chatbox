"""命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .config import load_config
from .index import build_store, make_embedding, save_store
from .ingest import load_corpus
from .chat import load_chatbot, run_chat
from .profile import build_profile, save_profile, profile_cache_key


def _p(msg: str) -> None:
    print(msg, flush=True)


def cmd_build(args) -> int:
    cfg = load_config(args.config)
    if args.out:
        cfg.store.dir = Path(args.out)
    people = set(args.people.split(",")) if args.people else None
    conversations = set(args.conversations.split(",")) if args.conversations else None
    corpus = load_corpus(
        db=args.db,
        json_dir=args.json_dir,
        names=args.names,
        limit=args.limit,
        since=args.since,
        people=people,
        conversations=conversations,
        person=args.person,
        progress=_p,
    )
    if not corpus.turns:
        _p("没有可索引的文本消息")
        return 1
    embed = make_embedding(cfg.embedding)
    store = build_store(corpus.turns, embed, progress=_p)
    save_store(
        store,
        cfg.store.dir,
        corpus.meta,
        embedding_provider=cfg.embedding.provider,
        dim=cfg.embedding.dim,
    )
    _p(f"索引已写入 {cfg.store.dir}（{len(store.turns)} 轮 / {len(corpus.speakers)} 个说话人）")
    _p("可运行：python -m qq_chatbot chat --mode qa  或  --mode persona --person <key>")
    return 0


def cmd_chat(args) -> int:
    cfg = load_config(args.config)
    if args.store:
        cfg.store.dir = Path(args.store)
    bot = load_chatbot(cfg)
    run_chat(bot, args.mode, args.person, session_key=args.session)
    return 0


def cmd_ask(args) -> int:
    cfg = load_config(args.config)
    if args.store:
        cfg.store.dir = Path(args.store)
    bot = load_chatbot(cfg)
    try:
        ans = bot.answer(args.question, mode=args.mode, person=args.person)
    except ValueError as exc:
        _p(f"错误：{exc}")
        return 1
    print(ans.text)
    for c in ans.citations:
        print(f"  · {c.conversation_key} {c.time_str} {c.speaker}: {c.text}")
    return 0


def cmd_profile(args) -> int:
    cfg = load_config(args.config)
    if args.store:
        cfg.store.dir = Path(args.store)
    bot = load_chatbot(cfg)
    key = profile_cache_key(args.person, args.kind, args.conversation)
    profile = build_profile(bot.corpus, args.person, bot.llm, progress=_p,
                            kind=args.kind, conversation=args.conversation,
                            previous=bot.profiles.get(key))
    path = save_profile(cfg.store.dir, profile, kind=args.kind, conversation=args.conversation)
    payload = {
        "speaker": profile.speaker,
        "style": profile.style,
        "facts": profile.facts,
        "opinions": profile.opinions,
        "relationships": profile.relationships,
        "topics": profile.topics,
        "timeline": profile.timeline,
        "kind": profile.kind,
        "conversation": profile.conversation,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _p(f"已保存到 {path}")
    return 0


def cmd_serve(args) -> int:
    cfg = load_config(args.config)
    if args.store:
        cfg.store.dir = Path(args.store)
    bot = load_chatbot(cfg)
    from .serve import serve

    serve(bot, host=args.host, port=args.port)
    return 0


def cmd_qq(args) -> int:
    cfg = load_config(args.config)
    if args.store:
        cfg.store.dir = Path(args.store)
    if args.ws:
        cfg.bridge.ws_url = args.ws
    if args.mode:
        cfg.bridge.mode = args.mode
    if args.person:
        cfg.bridge.persona_person = args.person
    bot = load_chatbot(cfg)
    from .bridge.onebot import run_bridge

    run_bridge(bot, cfg)
    return 0


def cmd_eval(args) -> int:
    from .eval.runner import evaluate, load_cases, compare, demo_retriever, evaluate_reviews
    import hashlib
    cfg = load_config(args.config)
    if args.store:
        cfg.store.dir = Path(args.store)
    cases = load_cases(args.dataset)
    retriever = demo_retriever(cfg) if args.demo else load_chatbot(cfg).retriever
    report = evaluate(retriever, cases, k=args.k)
    report["dataset_sha256"] = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    report["retrieval"] = cfg.to_dict()["retrieval"]
    report["embedding"] = dict(retriever.store.embedding or {"provider": cfg.embedding.provider, "model": cfg.embedding.model})
    report["configured_embedding"] = {"provider": cfg.embedding.provider, "model": cfg.embedding.model}
    report["corpus_sha256"] = hashlib.sha256(json.dumps(
        [(t.id, t.speaker.key, t.kind, t.conversation_key, t.time, t.text, t.msg_ids) for t in retriever.store.turns],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        if baseline.get("dataset_sha256") != report["dataset_sha256"]:
            raise ValueError("对比必须使用同一评测样本集")
        report["delta"] = compare(report, baseline)
    if args.reviews:
        report["persona_review"] = evaluate_reviews(json.loads(Path(args.reviews).read_text(encoding="utf-8")))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="qq-chatbot",
        description="基于 qq-chat-exporter 导出的聊天记录提炼聊天机器人（数字分身 / 历史问答）",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="装载语料并构建索引")
    p.add_argument("--db", help="明文数据库（已解密）")
    p.add_argument("--json", dest="json_dir", help="已有 JSON 导出目录（含 index.json）")
    p.add_argument("--names", help="会话名称映射 JSON")
    p.add_argument("--limit", type=int, help="只取前 N 条消息")
    p.add_argument("--since", type=int, help="只保留该时间戳之后（秒）")
    p.add_argument("--people", help="只保留这些说话人（Speaker.key 逗号分隔）")
    p.add_argument("--conversations", help="只保留这些会话（conversation_key 逗号分隔，如 c2c:10001）")
    p.add_argument("--person", help="只保留这个 QQ 号的人（跨私聊 + 所有群）")
    p.add_argument("--out", help="索引输出目录（默认 .chatbot）")
    p.add_argument("--config", help="配置文件（YAML/JSON）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("chat", help="交互式聊天")
    p.add_argument("--session", help="显式本地记忆身份；需 memory.enabled，省略则仅短期记忆")
    p.add_argument("--mode", default="qa", choices=["qa", "persona"])
    p.add_argument("--person", help="persona 模式要模仿的说话人 key")
    p.add_argument("--store", help="索引目录（默认 .chatbot，需与 build 的 --out 一致）")
    p.add_argument("--config")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("ask", help="单次提问")
    p.add_argument("question")
    p.add_argument("--mode", default="qa", choices=["qa", "persona"])
    p.add_argument("--person")
    p.add_argument("--store", help="索引目录")
    p.add_argument("--config")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("profile", help="生成/增量更新某说话人的人格档案")
    p.add_argument("--person", required=True)
    p.add_argument("--kind", choices=["c2c", "group"])
    p.add_argument("--conversation", help="限制档案事实来源会话")
    p.add_argument("--store", help="索引目录")
    p.add_argument("--config")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("serve", help="启动 HTTP 服务（/chat /health）")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--store", help="索引目录")
    p.add_argument("--config")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("qq", help="接入 QQ（NapCat OneBot11 正向 WebSocket，群聊+私聊）")
    p.add_argument("--ws", help="NapCat 正向 WS 地址（默认 ws://127.0.0.1:3001）")
    p.add_argument("--mode", choices=["qa", "persona"], help="默认模式")
    p.add_argument("--person", help="persona 模式要模仿的说话人 key")
    p.add_argument("--store", help="索引目录")
    p.add_argument("--config")
    p.set_defaults(func=cmd_qq)

    p = sub.add_parser("eval", help="离线检索评测（hit@k / recall@k / MRR）")
    p.add_argument("--dataset", required=True, help="JSON 标注样本集")
    p.add_argument("--demo", action="store_true", help="使用内置虚构演示语料")
    p.add_argument("--store")
    p.add_argument("--config")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--output")
    p.add_argument("--baseline")
    p.add_argument("--reviews", help="人工人格评分 JSON（case/style/factuality/timeline）")
    p.set_defaults(func=cmd_eval)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
