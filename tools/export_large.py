"""大库导出：--since 在 SQL 层筛选 Unix 秒时间戳，兼容源库毫秒值。

--strict 在任何坏页/读取错误处失败，供增量同步使用。默认容错模式尝试
反向游标补救，并在 JSON 索引 export.complete/errors 中明确标记不完整。
不完整导出不能用于提交持久水位线。

--resume 首次运行即启用会话 checkpoint；后续运行校验源库/参数/产物后
跳过已完成会话的 JSON/TXT 序列化与写盘。仍需扫描、解析、分桶和排序全部
匹配消息，且需完整哈希源库及 WAL/journal；不减少数据库读取或峰值内存。

--conversation / --kind / --after / --before 使用共享 metadata-first 查询；
时间范围为闭开区间，并与既有 --since 严格大于条件取交集。--self-qq 可覆盖
账号推断。--explain-query 仅输出查询计划和 metadata 推断信息，不写出状态。
选择性输出绑定 .export-selection.json，改变范围时必须使用新输出目录。

用法:
    python tools/export_large.py --db path/to/decrypted/nt_msg.db --out out
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import tempfile
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qq_exporter import db as dbmod
from qq_exporter import selection as selection_engine
from qq_exporter.db import _to_int, parse_body
from qq_exporter.exporters import safe_name
from qq_exporter.export_state import scope_payload, validate_scope, write_scope

# 两个消息表列结构一致（本机 QQ 9.9.30），直接按字段号取
MSG_COLS = [
    "40001",  # msg id (rowid)
    "40050",  # timestamp
    "40013",  # direction
    "40020",  # sender uid
    "40033",  # sender qq
    "40021",  # peer uid / group uid
    "40030",  # peer qq / group qq
    "40011",  # msg type
    "40012",  # subtype
    "40800",  # body protobuf
]


def _fmt(ts):
    if not ts:
        return ""
    if ts > 10**12:
        ts //= 1000
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _to_str(v):
    """把 SQLite 里可能是 bytes 的 uid 字段转成字符串。"""
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.hex()
    if v is None:
        return ""
    return str(v)


def _lean(row, me):
    """row 是一段按 MSG_COLS 顺序的值序列，转成标准消息 dict（与 iter_messages 同构，供 qq_chatbot 使用）。

    方向判定用「发送者是否等于本账号」，比 40013 的取值更可靠
    （实测 40013∈{1,2,4,5} 表示发出、{0,3} 表示收到，版本间易变）。
    """
    m = dict(zip(MSG_COLS, row))
    blob = m.get("40800")
    text, segments = parse_body(bytes(blob)) if isinstance(blob, (bytes, bytearray)) else ("", [])
    ts = _to_int(m.get("40050"))
    sq = _to_int(m.get("40033"))
    msg = {
        "msg_id": _to_int(m.get("40001")),
        "time": ts,
        "time_str": _fmt(ts),
        "direction": "out" if (sq is not None and str(sq) == str(me)) else "in",
        "sender_uid": _to_str(m.get("40020")),
        "sender_qq": sq,
        "peer_uid": _to_str(m.get("40021")),
        "peer_qq": _to_int(m.get("40030")),
        "msg_type": _to_int(m.get("40011")),
        "subtype": _to_int(m.get("40012")),
        "text": text,
    }
    if segments:
        msg["segments"] = segments
    return msg


def conv_key_of(kind, m):
    if kind == "group":
        gid = m["peer_qq"] or m["peer_uid"] or "?"
        return f"group:{gid}", str(gid), m["peer_uid"]
    if m["direction"] == "in":
        q, u = m["sender_qq"], m["sender_uid"]
    else:
        q, u = m["peer_qq"], m["peer_uid"]
    ident = q if q is not None else u
    return f"c2c:{ident}", str(q) if q is not None else None, u


def _since_value(value):
    value = float(value)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("since 必须是有限 Unix 秒时间戳")
    return value


def read_messages(conn, table, *, since=None, reverse=False):
    """Filter in SQL; accept QQ timestamps stored as seconds or milliseconds.

    Do not CAST the timestamp column: ordinary timestamp indexes remain usable.
    A reverse pass is only for best-effort recovery, never for committed syncs.
    """
    table = table.replace('"', '""')
    sel = ",".join(f'"{c}"' for c in MSG_COLS)
    where, params = "", ()
    if since is not None:
        where = (' WHERE (("40050" <= 1000000000000 AND "40050" > ?)'
                 ' OR ("40050" > 1000000000000 AND "40050" > ?) OR "40050" IS NULL)')
        params = (since, since * 1000)
    order = "DESC" if reverse else "ASC"
    return conn.execute(f'SELECT {sel} FROM "{table}"{where} ORDER BY rowid {order}', params)


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_fingerprint(path):
    path = Path(path)
    if not path.exists():
        return None
    before = path.stat()
    digest = _digest(path)
    after = path.stat()
    fields = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise RuntimeError("源文件在 fingerprint 计算期间变化，拒绝恢复")
    return {**{field: getattr(after, field) for field in fields}, "sha256": digest}


def _source_fingerprint(path):
    path = Path(path).resolve()
    # WAL and rollback journals can change the logical database independently of
    # the main file. SHM is transient coordination data and holds no DB pages.
    result = {"path": str(path), "files": {suffix: _file_fingerprint(Path(str(path) + suffix))
              for suffix in ("", "-wal", "-journal")}}
    if result["files"][""] is None:
        raise FileNotFoundError(path)
    return result


def _atomic_text(path, text):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _save_checkpoint(path, state):
    _atomic_text(path, json.dumps(state, ensure_ascii=False, allow_nan=False))


def _completed_valid(out, record, entry, artifacts):
    if not isinstance(record, dict) or record.get("entry") != entry:
        return False
    digests = record.get("artifacts")
    if not isinstance(digests, dict) or set(digests) != set(artifacts):
        return False
    return all((out / name).is_file() and _digest(out / name) == digests[name] for name in artifacts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--format", default="json,txt")
    ap.add_argument("--names", default=None)
    ap.add_argument("--since", type=_since_value, help="仅导出晚于此 Unix 秒时间戳的消息（SQL 过滤）")
    ap.add_argument("--strict", action="store_true", help="任何读取失败即终止，不允许坏页导出提交同步水位线")
    ap.add_argument("--immutable", action="store_true", help="仅用于稳定离线快照：不创建 SQLite sidecar、不读取 WAL")
    ap.add_argument("--resume", action="store_true", help="启用/恢复会话checkpoint；仍扫描全部匹配消息，仅跳过有效会话序列化与写盘")
    selection_engine.add_arguments(ap)
    args = ap.parse_args()
    try:
        selection = selection_engine.selection_from_args(args)
    except ValueError as exc:
        ap.error(str(exc))
    selected = selection.active
    formats = sorted(set(part.strip() for part in args.format.split(",") if part.strip()))
    if not formats or any(part not in ("json", "txt") for part in formats):
        ap.error("format 仅支持 json、txt 或 json,txt")

    dbp = Path(args.db).resolve()
    out = Path(args.out)
    checkpoint_path = out / ".export-checkpoint.json"
    checkpoint = None
    source = None
    uri = dbp.as_uri() + "?mode=ro" + ("&immutable=1" if args.immutable else "")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        tables = dbmod.detect_message_tables(conn)
        if not tables:
            raise RuntimeError("未识别到消息表，拒绝生成完整导出")
        columns = {table: dbmod.resolve_columns(conn, table) for table, _ in tables}
        # Identity is inferred over the full snapshot, before time/peer filtering.
        needs_private = any(kind == "c2c" and selection.matches_kind(kind) for _, kind in tables)
        infer_identity = needs_private and selection.self_qq is None
        if selected or args.explain_query:
            me = selection.self_qq
            if infer_identity:
                me = selection_engine.infer_self(conn, [(t, k) for t, k in tables if k == "c2c"], required=selected)
        else:
            # Preserve the historical unfiltered entry point and mock contract.
            me = None
            for table, kind in tables:
                if kind == "c2c":
                    quoted = table.replace('"', '""')
                    row = conn.execute(
                        f'SELECT "40033" FROM "{quoted}" WHERE "40013"=1 AND "40033" IS NOT NULL LIMIT 1'
                    ).fetchone()
                    if row:
                        me = str(row[0])
                        break
        if args.explain_query:
            plans = [selection_engine.query_plan(
                conn, table, kind, columns[table], selection, since=args.since, self_qq=me)
                for table, kind in tables if selection.matches_kind(kind)]
            print(json.dumps({"selection": selection.to_dict(), "effective_self_qq": me,
                              "identity_inference": {"performed": infer_identity,
                                                     "may_scan_metadata": infer_identity},
                              "queries": plans}, ensure_ascii=False, indent=2))
            return 0

        names = {}
        names_fingerprint = None
        if args.names:
            names_path = Path(args.names).resolve()
            names_text = names_path.read_text(encoding="utf-8")
            names = {str(k): str(v) for k, v in json.loads(names_text).items()}
            names_fingerprint = {"path": str(names_path), "sha256": hashlib.sha256(names_text.encode("utf-8")).hexdigest()}
        scope = scope_payload(dbp, selection, me,
                              artifacts={"formats": formats, "single_json": False, "writer": "large"})
        validate_scope(out, scope, active=selected)
        if args.resume:
            source = _source_fingerprint(dbp)
            parameters = {"formats": formats, "since": args.since, "strict": args.strict,
                          "names": names_fingerprint, "immutable": args.immutable,
                          "selection": selection.to_dict(), "self_qq": me}
            if checkpoint_path.exists():
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if checkpoint.get("version") != 1 or checkpoint.get("source") != source:
                    raise ValueError("resume 源数据库 fingerprint 不匹配，源文件已变化；请使用新的输出目录")
                previous = checkpoint.get("parameters")
                if isinstance(previous, dict) and "selection" not in previous and not selected:
                    previous = {"selection": selection.to_dict(), "self_qq": me, **previous}
                if previous != parameters:
                    raise ValueError("resume 导出参数不匹配；请使用原参数或新的输出目录")
                if not isinstance(checkpoint.get("completed"), dict):
                    raise ValueError("resume checkpoint completed 无效")
            else:
                checkpoint = {"version": 1, "source": source, "parameters": parameters, "completed": {}, "scan_complete": False}
            checkpoint["parameters"] = parameters
        write_scope(out, scope, active=selected)
        if checkpoint is not None:
            out.mkdir(parents=True, exist_ok=True)
            checkpoint["complete"] = False
            _save_checkpoint(checkpoint_path, checkpoint)
    except BaseException:
        conn.close()
        raise
    finally:
        if args.explain_query:
            conn.close()
    print("消息表:", tables, flush=True)
    print("本账号 QQ:", me, flush=True)

    convs: "OrderedDict[str, dict]" = OrderedDict()
    seen = set()
    errors = []
    last_msg_time = None

    def bucket(kind, row):
        nonlocal last_msg_time
        if selected:
            m = selection_engine.normalize_metadata(row, kind, self_qq=me)
            key, pq, pu = selection_engine.peer_of(m, kind, self_qq=me)
            if m["time"] is not None and m["time"] > 10**12:
                m["time"] = m["time"] / 1000
        else:
            m = _lean(tuple(row), me)
            if m["time"] is not None and m["time"] > 10**12:
                m["time"] = m["time"] / 1000
            key, pq, pu = conv_key_of(kind, m)
        if m["time"] is None or not math.isfinite(m["time"]):
            raise ValueError("消息缺少有效时间戳，无法可靠推进水位线")
        if selected:
            blob = row.get("body")
            text, segments = parse_body(bytes(blob)) if isinstance(blob, (bytes, bytearray)) else ("", [])
            m["text"] = text
            if segments:
                m["segments"] = segments
        # IDs are only unique inside a conversation, not across tables/accounts.
        ident = (key, m["msg_id"]) if m["msg_id"] is not None else (key, json.dumps(m, sort_keys=True))
        if ident in seen:
            return
        seen.add(ident)
        last_msg_time = max(last_msg_time, m["time"]) if last_msg_time is not None else m["time"]
        c = convs.setdefault(key, {
            "kind": kind, "key": key, "group_id": pq if kind == "group" else None,
            "peer_qq": pq, "peer_uid": pu, "messages": [],
        })
        c["messages"].append(m)

    def rows_for(table, kind, *, reverse=False):
        if selected:
            return selection_engine.iter_rows(conn, table, kind, columns[table], selection,
                                              since=args.since, reverse=reverse, self_qq=me)
        return read_messages(conn, table, since=args.since, reverse=reverse)

    try:
        for table, kind in tables:
            if not selection.matches_kind(kind):
                continue
            try:
                for row in rows_for(table, kind):
                    bucket(kind, row)
            except (sqlite3.DatabaseError, ValueError) as exc:
                if args.strict:
                    raise
                errors.append(f"{table}: {exc}")
                print(f"警告: {table} 读取不完整: {exc}", flush=True)
                try:
                    for row in rows_for(table, kind, reverse=True):
                        bucket(kind, row)
                except (sqlite3.DatabaseError, ValueError) as reverse_exc:
                    errors.append(f"{table} reverse: {reverse_exc}")
    finally:
        conn.close()

    if args.resume and _source_fingerprint(dbp) != source:
        raise RuntimeError("源数据库在扫描期间变化，拒绝提交 resume 产物")
    if checkpoint is not None:
        if errors or not checkpoint.get("scan_complete"):
            checkpoint["completed"] = {}
        checkpoint.update(scan_complete=not errors, errors=errors, last_msg_time=last_msg_time)
        _save_checkpoint(checkpoint_path, checkpoint)

    total = sum(len(c["messages"]) for c in convs.values())
    print(f"\n共 {len(convs)} 个会话 / {total:,} 条消息", flush=True)

    for c in convs.values():
        c["messages"].sort(key=lambda m: (m["time"] or 0, m["msg_id"] or 0))

    for c in convs.values():
        c["title"] = names.get(c["key"], names.get(str(c["peer_qq"]), c["peer_qq"] or c["peer_uid"] or "未知"))

    (out / "json").mkdir(parents=True, exist_ok=True)
    (out / "txt").mkdir(parents=True, exist_ok=True)

    idx = []
    for i, (key, c) in enumerate(convs.items()):
        stem = f"{i:04d}_{safe_name(str(c['title']), c['key'])}"
        entry = {
            "id": key,
            "kind": c["kind"],
            "title": c["title"],
            "group_id": c["group_id"],
            "peer_qq": c["peer_qq"],
            "peer_uid": c["peer_uid"],
            "message_count": len(c["messages"]),
            "first_time": c["messages"][0]["time"] if c["messages"] else None,
            "last_time": c["messages"][-1]["time"] if c["messages"] else None,
            "file": f"{stem}.json",
        }
        idx.append(entry)
        artifacts = [f"{fmt}/{stem}.{fmt}" for fmt in formats]
        if checkpoint is not None and not errors and _completed_valid(out, checkpoint["completed"].get(key), entry, artifacts):
            continue

        if "json" in formats:
            _atomic_text(out / "json" / f"{stem}.json",
                         json.dumps({"conversation": entry, "messages": c["messages"]}, ensure_ascii=False))
        if "txt" in formats:
            lines = ["=" * 70, f"会话: {c['title']}", f"类型: {'群聊' if c['kind']=='group' else '私聊'}",
                     f"标识: {key}", f"消息数: {len(c['messages'])}", "=" * 70, ""]
            for m in c["messages"]:
                who = m["sender_qq"] or m["sender_uid"] or "?"
                arrow = "->" if m["direction"] == "out" else "<-"
                lines.append(f"[{m['time_str'] or '未知时间'}] {arrow} {who}")
                t = (m["text"] or "").rstrip() or "（非文本消息）"
                lines.extend(f"    {ln}" for ln in t.splitlines())
                lines.append("")
            _atomic_text(out / "txt" / f"{stem}.txt", "\n".join(lines))

        if checkpoint is not None and not errors:
            checkpoint["completed"][key] = {"entry": entry, "artifacts": {name: _digest(out / name) for name in artifacts}}
            _save_checkpoint(checkpoint_path, checkpoint)
        if (i + 1) % 200 == 0:
            print(f"  已导出 {i+1}/{len(convs)} 个会话", flush=True)

    if args.resume and _source_fingerprint(dbp) != source:
        raise RuntimeError("源数据库在导出期间变化，拒绝提交 resume 索引")
    _atomic_text(out / "json" / "index.json",
        json.dumps({"conversation_count": len(idx), "message_count": total, "conversations": idx,
                    "export": {"complete": not errors, "since": args.since,
                               "selection": selection.to_dict(), "effective_self_qq": me,
                               "last_msg_time": last_msg_time, "errors": errors}},
                   ensure_ascii=False, indent=1))
    index_lines = [f"QQ 聊天记录导出索引\n\n会话数: {len(idx)}   消息数: {total:,}\n\n"]
    for e in idx:
        rng = ""
        if e["first_time"] and e["last_time"]:
            rng = f"  {_fmt(e['first_time'])} ~ {_fmt(e['last_time'])}"
        index_lines.append(f"{e['id']:<26} {e['message_count']:>9,} 条{rng}\n")
    _atomic_text(out / "_index.txt", "".join(index_lines))
    if checkpoint is not None:
        checkpoint["complete"] = not errors
        _save_checkpoint(checkpoint_path, checkpoint)

    print(f"\n导出完成 -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
