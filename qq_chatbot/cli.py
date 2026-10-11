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
from .profile import build_profile, save_profile


def _p(msg: str) -> None:
    print(msg, flush=True)


def cmd_build(args) -> int:
    cfg = load_config(args.config)
    if args.out:
        cfg.store.dir = Path(args.out)
    people = set(args.people.split(",")) if args.people else None
    corpus = load_corpus(
        db=args.db,
        json_dir=args.json_dir,
        names=args.names,
        limit=args.limit,
        since=args.since,
        people=people,
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
    bot = load_chatbot(cfg)
    run_chat(bot, args.mode, args.person)
    return 0


def cmd_ask(args) -> int:
    cfg = load_config(args.config)
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
    bot = load_chatbot(cfg)
    profile = bot.profiles.get(args.person)
    if profile is None:
        profile = build_profile(bot.corpus, args.person, bot.llm, progress=_p)
    path = save_profile(cfg.store.dir, profile)
    payload = {
        "speaker": profile.speaker,
        "style": profile.style,
        "facts": profile.facts,
        "opinions": profile.opinions,
        "relationships": profile.relationships,
        "topics": profile.topics,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _p(f"已保存到 {path}")
    return 0


def cmd_serve(args) -> int:
    cfg = load_config(args.config)
    bot = load_chatbot(cfg)
    from .serve import serve

    serve(bot, host=args.host, port=args.port)
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
    p.add_argument("--out", help="索引输出目录（默认 .chatbot）")
    p.add_argument("--config", help="配置文件（YAML/JSON）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("chat", help="交互式聊天")
    p.add_argument("--mode", default="qa", choices=["qa", "persona"])
    p.add_argument("--person", help="persona 模式要模仿的说话人 key")
    p.add_argument("--config")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("ask", help="单次提问")
    p.add_argument("question")
    p.add_argument("--mode", default="qa", choices=["qa", "persona"])
    p.add_argument("--person")
    p.add_argument("--config")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("profile", help="生成/查看某说话人的人格档案")
    p.add_argument("--person", required=True)
    p.add_argument("--config")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("serve", help="启动 HTTP 服务（/chat /health）")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--config")
    p.set_defaults(func=cmd_serve)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
