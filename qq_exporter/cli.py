"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, keyfinder, sqlcipher
from .collect import build_conversations, load_names, effective_self, list_conversations
from . import db as dbmod
from .selection import add_arguments, selection_from_args, query_plan
from .export_state import (atomic_json, scope_payload, validate_scope, write_scope,
                           load_checkpoint, save_checkpoint)
from .exporters import export_json, export_txt, safe_name
from .media import download_media

DEFAULT_WORK = Path("_work")


def _p(msg: str) -> None:
    print(msg, flush=True)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    atomic_json(path, state)


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


def _formats(value):
    formats = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not formats or any(part not in ("json", "txt") for part in formats):
        raise ValueError("--format 只支持 json,txt")
    return formats


def _validate_export_args(args):
    formats = _formats(args.format)
    selection = selection_from_args(args)
    if getattr(args, "limit", None) is not None:
        if args.limit <= 0:
            raise ValueError("--limit 必须大于 0")
        if getattr(args, "incremental", False):
            raise ValueError("--limit 不能与 --incremental/watch 一起使用，以免推进水位后遗漏消息")
    return formats, selection


def cmd_conversations(args) -> int:
    entries = list_conversations(Path(args.db), kind=args.kind,
                                 names=load_names(Path(args.names) if args.names else None),
                                 self_qq=args.self_qq,
                                 standard_layout=getattr(args, "standard_layout", False))
    if args.json:
        _p(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        _p("会话键\t名称\t消息数\t最早时间(Unix秒)\t最晚时间(Unix秒)\t未知时间数")
        for entry in entries:
            _p("\t".join(str(entry[key]) if entry[key] is not None else "unknown"
                         for key in ("key", "title", "message_count", "first_time", "last_time", "unknown_time_count")))
    return 0


def cmd_export(args) -> int:
    db_path = Path(args.db)
    out_dir = Path(args.out)
    formats, selection = _validate_export_args(args)
    conn = dbmod.connect(db_path)
    try:
        conn.execute("BEGIN")
        tables = [(t, k) for t, k in dbmod.detect_message_tables(conn) if selection.matches_kind(k)]
        identity = effective_self(conn, tables, selection) if selection.active else None
        scope = scope_payload(db_path, selection, identity, artifacts={
            "formats": sorted(set(formats)), "single_json": bool(args.single_json), "writer": "regular"})
        min_ids: dict[str, int] = {}
        state_path = out_dir / ".export_state.json"
        if args.incremental:
            min_ids = load_checkpoint(state_path, scope, active=selection.active)
        if args.since is not None:
            min_ids = {"*": args.since}
        if getattr(args, "explain_query", False):
            plans = []
            for table, kind in tables:
                columns = dbmod.resolve_columns(conn, table)
                minimum = min_ids.get("*", min_ids.get(table))
                if minimum is not None and "msg_id" not in columns:
                    raise ValueError(f"表 {table} 缺少消息 ID 列，不能应用 --since/增量水位")
                plans.append({"table": table, "kind": kind, **query_plan(
                    conn, table, kind, columns, selection, min_id=minimum, self_qq=identity)})
            inference = selection.self_qq is None and any(k == "c2c" for _, k in tables)
            _p(json.dumps({"selection": selection.to_dict(), "effective_self_qq": identity,
                           "identity_inference": {"performed": inference, "may_scan_metadata": inference},
                           "queries": plans}, ensure_ascii=False, indent=2))
            return 0
        validate_scope(out_dir, scope, active=selection.active)
        names = load_names(Path(args.names) if args.names else None)
        meta, conversations = build_conversations(
            db_path,
            include_content=args.include_content,
            names=names,
            limit=getattr(args, "limit", None),
            min_ids=min_ids,
            progress=_p,
            selection=selection,
            self_qq=identity,
            connection=conn,
        )
    finally:
        conn.close()
    if selection.active:
        meta["selection"] = selection.to_dict()
        meta["effective_self_qq"] = identity
    _p(f"共 {meta['conversation_count']} 个会话 / {meta['message_count']} 条消息")
    write_scope(out_dir, scope, active=selection.active)

    # 媒体下载
    media_failed = False
    if args.media:
        from .media import build_local_index

        # 收集媒体并推导 nt_data 根目录（从消息里的本地路径反推）
        conv_media = []
        nt_data_root = args.media_dir
        for conv in conversations:
            items = [m for msg in conv.messages for m in msg.get("media", [])]
            conv_media.append((conv, items))
            if not nt_data_root:
                for m in items:
                    lp = m.get("local_path") or ""
                    if "nt_data" in lp:
                        nt_data_root = lp.split("nt_data", 1)[0] + "nt_data"
                        break
        if nt_data_root:
            _p(f"媒体缓存目录: {nt_data_root}")
            local_index = build_local_index(nt_data_root)
        else:
            local_index = {}

        napcat = None
        if getattr(args, "napcat", None):
            from .napcat import NapCat

            napcat = NapCat(
                args.napcat, getattr(args, "napcat_token", None) or None
            )
            _p(f"NapCat 补下载: {args.napcat}")

        stats = {"copied": 0, "downloaded": 0, "skipped": 0, "failed": 0}
        for conv, items in conv_media:
            if not items:
                continue
            d = out_dir / "media" / safe_name(conv.title, conv.key)
            st = download_media(items, d, local_index=local_index, napcat=napcat)
            for k in stats:
                stats[k] += st[k]
        _p(
            f"媒体: 复制 {stats['copied']} / 下载 {stats['downloaded']} / "
            f"跳过 {stats['skipped']} / 失败 {stats['failed']}"
        )

        media_failed = stats["failed"] > 0
        meta["media_complete"] = not media_failed

    if "json" in formats:
        export_json(
            conversations,
            out_dir / "json",
            meta,
            single=args.single_json,
            include_segments=not args.no_segments,
        )
        _p(f"JSON 已写入 {out_dir / 'json'}")
    if "txt" in formats:
        export_txt(conversations, out_dir / "txt", meta)
        _p(f"TXT 已写入 {out_dir / 'txt'}")

    # 更新增量状态
    if media_failed:
        _p("本轮媒体下载不完整，增量水位未推进")
        return 1
    if args.incremental and meta["message_count"]:
        save_checkpoint(state_path, scope, meta.get("last_ids", {}))
        _p(f"增量状态已更新: {meta.get('last_ids')}")
    return 0


def cmd_watch(args) -> int:
    args.incremental = True
    if getattr(args, "explain_query", False):
        return cmd_export(args)
    _p(f"定时增量导出：每 {args.interval} 秒一轮，Ctrl+C 退出")
    while True:
        try:
            rc = cmd_export(args)
        except KeyboardInterrupt:
            _p("已停止")
            return 0
        _p(f"本轮完成（{rc}），等待 {args.interval} 秒…")
        time.sleep(args.interval)


def cmd_all(args) -> int:
    if getattr(args, "explain_query", False):
        raise ValueError("all --explain-query 不执行密钥提取或解密；请使用 export --db <明文库> --explain-query")
    _validate_export_args(args)
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
    p.add_argument("--limit", type=int, help="匹配结果前 N 条（调试，不能与 --incremental 同用）")
    p.add_argument("--media", action="store_true", help="下载图片/文件/语音/视频/表情到 out/media/")
    p.add_argument("--media-dir", help="QQ 的 nt_data 缓存目录（默认从消息里的路径自动推导）")
    p.add_argument("--napcat", help="NapCat HTTP 地址（如 http://127.0.0.1:6099），旧媒体 rkey 过期时换新直链")
    p.add_argument("--napcat-token", help="NapCat HTTP access_token（若 onebot11.json 配置了）")
    p.add_argument("--incremental", action="store_true", help="增量导出：只导出上次之后的新消息")
    p.add_argument("--since", type=int, help="只导出消息ID大于该值的消息")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("watch", help="定时增量导出")
    p.add_argument("--db", required=True, help="已解密的 sqlite 文件")
    p.add_argument("--out", default="out", help="输出目录")
    p.add_argument("--format", default="json,txt")
    p.add_argument("--interval", type=int, default=3600, help="间隔秒数（默认 3600）")
    p.add_argument("--media", action="store_true", help="下载媒体")
    p.add_argument("--media-dir", help="QQ 的 nt_data 缓存目录")
    p.add_argument("--napcat", help="NapCat HTTP 地址")
    p.add_argument("--napcat-token", help="NapCat HTTP access_token")
    p.add_argument("--names")
    p.add_argument("--single-json", action="store_true")
    p.add_argument("--no-segments", action="store_true")
    p.add_argument("--include-content", action="store_true")
    p.add_argument("--since", type=int)
    p.set_defaults(func=cmd_watch)

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
    p.add_argument("--media", action="store_true", help="下载媒体到 out/media/")
    p.add_argument("--media-dir", help="QQ 的 nt_data 缓存目录")
    p.add_argument("--napcat", help="NapCat HTTP 地址（旧媒体换新直链）")
    p.add_argument("--napcat-token", help="NapCat HTTP access_token")
    p.add_argument("--incremental", action="store_true", help="增量导出")
    p.add_argument("--since", type=int, help="只导出消息ID大于该值的消息")
    p.add_argument("--yes", action="store_true", help="跳过交互确认")
    p.add_argument("--allow-wal", action="store_true", help="忽略 -wal 提示")
    p.set_defaults(func=cmd_all)

    for command in ("export", "watch", "all"):
        add_arguments(sub.choices[command])
    sub.choices["watch"].add_argument("--limit", type=int)

    p = sub.add_parser("conversations", help="列出会话目录（不读取消息正文）")
    p.add_argument("--db", required=True)
    p.add_argument("--kind", choices=("group", "c2c"))
    p.add_argument("--names")
    p.add_argument("--self-qq")
    p.add_argument("--standard-layout", action="store_true")
    p.add_argument("--json", action="store_true", help="输出 JSON 数组")
    p.set_defaults(func=cmd_conversations)
    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
