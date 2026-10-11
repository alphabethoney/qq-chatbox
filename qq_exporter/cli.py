"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, keyfinder, sqlcipher
from .collect import build_conversations, load_names
from .exporters import export_json, export_txt

DEFAULT_WORK = Path("_work")


def _p(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------


def cmd_scan(args) -> int:
    dbs = keyfinder.discover_databases(args.qq)
    if not dbs:
        _p(r"没有发现 NTQQ 数据库。默认路径: %USERPROFILE%\Documents\Tencent Files\<QQ号>\nt_qq\nt_db")
        return 1
    for account, files in dbs.items():
        _p(f"[{account}]")
        for path in files:
            size = path.stat().st_size / 1048576
            salt = ""
            try:
                salt = sqlcipher.extract_salt(path).hex()
            except Exception:  # noqa: BLE001
                pass
            _p(f"  {path.name:<26} {size:8.2f} MB  盐值={salt}")
        wal = keyfinder.pending_wal(files[0]) if files else 0
        if wal:
            _p(f"  (存在 -wal 文件 {wal/1048576:.2f} MB，建议先退出 QQ 再解密以合并最新消息)")
    return 0


def cmd_keys(args) -> int:
    if args.db:
        main_db = Path(args.db)
        account = main_db.parent.parent.name
    else:
        account, main_db = keyfinder.find_main_db(args.qq)
    _p(f"目标库: {main_db}")
    is_global = "global" in main_db.parts
    if not is_global and keyfinder.qq_is_likely_idle(main_db):
        _p("警告: 该数据库很久没有被写过了。")
        _p("     如果 QQ 当前没有登录这个账号，数据库就不会被打开，密钥也不会在内存里。")
        _p("     请先在 QQ 里登录该账号，随意点开几个聊天窗口，然后重试。")
    hit, stats = keyfinder.recover_key(
        main_db,
        pid=args.pid,
        deep=args.deep,
        passphrase=args.brute_passphrase,
        progress=_p,
    )
    _p(
        f"扫描 {stats.scanned_bytes/1048576:.0f} MB / {stats.regions} 个区域 / "
        f"{stats.elapsed:.1f}s / {stats.candidates:,} 个候选"
    )
    if not hit:
        _p("未能恢复密钥。可尝试：")
        _p("  1) 确认 QQ 已登录该账号，并且点开过聊天窗口；")
        _p("  2) 用管理员终端重试（OpenProcess 可能被拒绝）；")
        _p("  3) 加 --deep 做一次全量内存扫描；")
        _p("  4) 加 --brute-passphrase 做口令枚举（很慢）。")
        return 2
    _p(f"密钥: {hit.key_hex}")
    _p(f"来源: {hit.method}  {hit.detail}  校验: {'通过' if hit.verified else '未通过'}")
    if args.save:
        payload = {
            "account": account,
            "db": str(main_db),
            "salt": hit.salt_hex,
            "key": hit.key_hex,
            "method": hit.method,
            "verified": hit.verified,
        }
        Path(args.save).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _p(f"已保存到 {args.save}")
    return 0


def cmd_decrypt(args) -> int:
    db_path = Path(args.db)
    if args.key:
        key = bytes.fromhex(args.key.replace(" ", ""))
        if len(key) != 32:
            _p("密钥必须是 64 位十六进制（32 字节）")
            return 1
    elif args.passphrase:
        key = sqlcipher.key_from_passphrase(db_path, args.passphrase)
    else:
        _p("需要 --key（64 位十六进制密钥）或 --passphrase（16 字符口令）之一")
        return 1
    out = Path(args.out)
    stats = sqlcipher.decrypt_database(
        db_path, out, key, progress=lambda a, b: _p(f"  解密 {a}/{b} 页")
    )
    _p(
        f"完成: {out}  页数={stats['pages']}（含 WAL {stats['wal_pages']} 页）"
        f"  保留区={stats['reserve']}  quick_check={stats['integrity']}"
    )
    return 0


def cmd_schema(args) -> int:
    from .db import dump_schema

    schema = dump_schema(Path(args.db))
    text = json.dumps(schema, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        _p(f"已保存到 {args.out}")
    else:
        _p(text)
    return 0


def cmd_export(args) -> int:
    db_path = Path(args.db)
    out_dir = Path(args.out)
    formats = [f.strip().lower() for f in args.format.split(",") if f.strip()]
    names = load_names(Path(args.names) if args.names else None)

    meta, conversations = build_conversations(
        db_path,
        include_content=args.include_content,
        names=names,
        limit=args.limit,
        progress=_p,
    )
    _p(f"共 {meta['conversation_count']} 个会话 / {meta['message_count']} 条消息")
    if not conversations:
        _p("没有可导出的消息")
        return 1
    if "json" in formats:
        index = export_json(
            conversations,
            out_dir / "json",
            meta,
            single=args.single_json,
            include_segments=not args.no_segments,
        )
        _p(f"JSON 已写入 {out_dir / 'json'}（{len(index['conversations'])} 个文件）")
    if "txt" in formats:
        index = export_txt(conversations, out_dir / "txt", meta)
        _p(f"TXT 已写入 {out_dir / 'txt'}（{len(index['conversations'])} 个文件）")
    return 0


def cmd_all(args) -> int:
    account = args.qq
    if not account:
        account, _ = keyfinder.find_main_db()
    _, main_db = keyfinder.find_main_db(account)
    _p(f"账号 {account}，主库 {main_db}")

    wal = keyfinder.pending_wal(main_db)
    if wal and not args.allow_wal:
        _p(
            f"提示: 检测到 {wal/1048576:.2f} MB 的 -wal 文件，"
            "其中可能有尚未写入主库的新消息。"
        )
        _p("     想让导出更完整，可以退出 QQ 后重跑（密钥会缓存）：加 --allow-wal 可忽略此提示。")
        if sys.stdin.isatty() and not args.yes:
            try:
                if input("     仍要继续吗？[y/N] ").strip().lower() not in ("y", "yes"):
                    return 1
            except EOFError:
                pass

    dbs = keyfinder.discover_databases(account)
    files = dbs.get(account, [])
    if not files:
        _p("没有找到该账号的数据库")
        return 1

    key_file = Path(args.key_file) if args.key_file else DEFAULT_WORK / "keys.json"
    key_hex = None
    if args.key:
        key_hex = args.key
    elif args.passphrase:
        key_hex = sqlcipher.key_from_passphrase(main_db, args.passphrase).hex()
        _p("已由 --passphrase 推导出 AES 密钥")
    elif key_file.exists():
        saved = json.loads(key_file.read_text(encoding="utf-8"))
        entry = saved.get(main_db.name) or saved.get("key")
        if isinstance(entry, dict):
            key_hex = entry.get("key")
        elif isinstance(entry, str):
            key_hex = entry
        if key_hex:
            _p(f"使用已缓存的密钥（{key_file}）")
    if not key_hex:
        if keyfinder.qq_is_likely_idle(main_db):
            _p("警告: 该数据库很久没有被写过，QQ 可能没有登录这个账号。")
            _p("     未登录时数据库不会被打开，密钥也就不在内存里。")
        hit, stats = keyfinder.recover_key(
            main_db,
            pid=args.pid,
            deep=args.deep,
            passphrase=args.brute_passphrase,
            progress=_p,
        )
        if not hit:
            _p("未能取到主库密钥。请确认 QQ 已登录该账号并点开过聊天窗口；")
            _p("或用管理员终端重试，或加 --deep / --brute-passphrase，")
            _p("或改用 NapCat 拿口令后加 --passphrase 传入。")
            return 2
        key_hex = hit.key_hex
        DEFAULT_WORK.mkdir(parents=True, exist_ok=True)
        key_file.write_text(
            json.dumps(
                {
                    main_db.name: {
                        "key": hit.key_hex,
                        "salt": hit.salt_hex,
                        "method": hit.method,
                        "verified": hit.verified,
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _p(f"密钥已缓存到 {key_file}")

    plain_dir = Path(args.work) / "plain"
    plain_db = plain_dir / main_db.name
    stats = sqlcipher.decrypt_database(
        main_db, plain_db, bytes.fromhex(key_hex), progress=lambda a, b: None
    )
    _p(f"解密完成: {plain_db}  quick_check={stats['integrity']}")
    if stats["integrity"] != "ok":
        _p("警告: quick_check 未通过，导出结果可能不完整。")

    args.db = str(plain_db)
    return cmd_export(args)


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="qq-exporter",
        description="从 Windows NTQQ 本地数据库中导出聊天记录（JSON / TXT）",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="列出本机 QQ 账号与数据库")
    p.add_argument("--qq", help="只显示指定账号")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("keys", help="从运行中的 QQ 进程提取数据库密钥")
    p.add_argument("--qq", help="指定账号")
    p.add_argument("--db", help="直接指定数据库文件（例如 global 目录下的 login.db）")
    p.add_argument("--pid", type=int, help="手动指定 QQ 主进程 PID")
    p.add_argument("--deep", action="store_true", help="额外做全量内存扫描（慢）")
    p.add_argument("--brute-passphrase", action="store_true", help="额外做口令枚举（很慢）")
    p.add_argument("--save", help="把密钥保存为 JSON")
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("decrypt", help="用已知密钥解密数据库")
    p.add_argument("--db", required=True, help="nt_msg.db 路径")
    p.add_argument("--key", help="64 位十六进制密钥")
    p.add_argument("--passphrase", help="16 字符 ASCII 口令（NapCat 等工具给出的就是它）")
    p.add_argument("--out", required=True, help="输出的明文 sqlite 路径")
    p.set_defaults(func=cmd_decrypt)

    p = sub.add_parser("schema", help="打印（明文）数据库的表结构")
    p.add_argument("--db", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("export", help="从明文数据库导出")
    p.add_argument("--db", required=True, help="已解密的 sqlite 文件")
    p.add_argument("--out", default="out", help="输出目录")
    p.add_argument("--format", default="json,txt", help="导出格式，逗号分隔")
    p.add_argument("--single-json", action="store_true", help="JSON 合并为单文件")
    p.add_argument("--no-segments", action="store_true", help="JSON 中不写消息片段")
    p.add_argument("--include-content", action="store_true", help="JSON 中附带原始字段树")
    p.add_argument("--names", help="会话名称映射 JSON")
    p.add_argument("--limit", type=int, help="只导出前 N 条消息（调试用）")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("all", help="一条龙：取密钥 -> 解密 -> 导出")
    p.add_argument("--qq", help="账号（默认自动检测）")
    p.add_argument("--pid", type=int)
    p.add_argument("--out", default="out")
    p.add_argument("--format", default="json,txt")
    p.add_argument("--work", default=str(DEFAULT_WORK), help="中间文件目录")
    p.add_argument("--key", help="直接提供 64 位十六进制密钥")
    p.add_argument("--passphrase", help="直接提供 16 字符 ASCII 口令（NapCat 等工具给出的就是它）")
    p.add_argument("--key-file", help="密钥缓存文件（默认 _work/keys.json）")
    p.add_argument("--deep", action="store_true", help="提取密钥时做全量内存扫描")
    p.add_argument("--brute-passphrase", action="store_true", help="提取密钥时做口令枚举")
    p.add_argument("--single-json", action="store_true")
    p.add_argument("--no-segments", action="store_true")
    p.add_argument("--include-content", action="store_true")
    p.add_argument("--names")
    p.add_argument("--limit", type=int)
    p.add_argument("--yes", action="store_true", help="跳过交互确认")
    p.add_argument("--allow-wal", action="store_true", help="忽略 -wal 提示")
    p.set_defaults(func=cmd_all)

    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
