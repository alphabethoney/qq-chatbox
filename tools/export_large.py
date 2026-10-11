"""大库容错导出：group_msg_table 有页级损坏，正向/反向游标都过不去。

策略：正向按 rowid 读一段，出错后换反向按 rowid 读另一段，两段合并去重，
把能读到的消息尽量全部导出。中间够不到的那一小段单独记录。

用法:
    python tools/export_large.py --db path/to/decrypted/nt_msg.db --out out
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qq_exporter import db as dbmod
from qq_exporter.db import _to_int, parse_body
from qq_exporter.exporters import safe_name

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


def _lean(row, me):
    """row 是一段按 MSG_COLS 顺序的值序列，转成精简 dict。

    方向判定用「发送者是否等于本账号」，比 40013 的取值更可靠
    （实测 40013∈{1,2,4,5} 表示发出、{0,3} 表示收到，版本间易变）。
    """
    m = dict(zip(MSG_COLS, row))
    blob = m.get("40800")
    text, _ = parse_body(bytes(blob)) if isinstance(blob, (bytes, bytearray)) else ("", [])
    ts = _to_int(m.get("40050"))
    sq = _to_int(m.get("40033"))
    return {
        "t": ts,
        "ts": _fmt(ts),
        "d": "out" if (sq is not None and str(sq) == str(me)) else "in",
        "su": m.get("40020"),
        "sq": sq,
        "pu": m.get("40021"),
        "pq": _to_int(m.get("40030")),
        "mt": _to_int(m.get("40011")),
        "sub": _to_int(m.get("40012")),
        "id": _to_int(m.get("40001")),
        "text": text,
    }


def conv_key_of(kind, m):
    if kind == "group":
        gid = m["pq"] or m["pu"] or "?"
        return f"group:{gid}", str(gid), m["pu"]
    if m["d"] == "in":
        q, u = m["sq"], m["su"]
    else:
        q, u = m["pq"], m["pu"]
    ident = q if q is not None else u
    return f"c2c:{ident}", str(q) if q is not None else None, u


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--format", default="json,txt")
    ap.add_argument("--names", default=None)
    args = ap.parse_args()

    dbp = Path(args.db)
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    tables = dbmod.detect_message_tables(conn)
    print("消息表:", tables, flush=True)

    # 自动识别本账号 QQ 号：40013=1 的行 sender 必是自己
    me = conn.execute(
        'SELECT "40033" FROM c2c_msg_table WHERE "40013"=1 AND "40033" IS NOT NULL LIMIT 1'
    ).fetchone()
    me = str(me[0]) if me else None
    print("本账号 QQ:", me, flush=True)

    sel = ",".join(f'"{c}"' for c in MSG_COLS)
    convs: "OrderedDict[str, dict]" = OrderedDict()
    seen: set[int] = set()

    def bucket(kind, m):
        key, pq, pu = conv_key_of(kind, m)
        c = convs.get(key)
        if c is None:
            c = {
                "kind": kind,
                "key": key,
                "group_id": pq if kind == "group" else None,
                "peer_qq": pq,
                "peer_uid": pu,
                "messages": [],
            }
            convs[key] = c
        c["messages"].append(m)

    # ---- c2c 全量 ----
    c2c_tab = next((t for t, k in tables if t == "c2c_msg_table"), [t for t, k in tables if k == "c2c"][0])
    n = 0
    for row in conn.execute(f'SELECT {sel} FROM "{c2c_tab}" ORDER BY rowid'):
        m = _lean(tuple(row), me)
        seen.add(m["id"])
        bucket("c2c", m)
        n += 1
    print(f"c2c 读取 {n:,} 条", flush=True)

    # ---- group 正向 + 反向 ----
    # 主群聊表是 group_msg_table；discuss_msg_table 是旧版讨论组表，忽略
    gtab = next((t for t, k in tables if t == "group_msg_table"), [t for t, k in tables if k == "group"][0])

    def read_batch(cond, order, last, limit=200000):
        sql = f'SELECT {sel} FROM "{gtab}" WHERE "40001" {cond} ? ORDER BY "40001" {order} LIMIT ?'
        return conn.execute(sql, (last, limit)).fetchall()

    fwd, bwd = 0, 0
    last = 0
    while True:
        try:
            rows = read_batch(">", "ASC", last)
        except Exception as e:
            print(f"正向在 rowid={last} 处结束（累计 {fwd:,} 条）: {e}", flush=True)
            break
        if not rows:
            break
        for r in rows:
            m = _lean(tuple(r), me)
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            bucket("group", m)
        fwd += len(rows)
        last = rows[-1]["40001"]
        if fwd % 500000 < 200000:
            print(f"  正向 {fwd:,} 条", flush=True)

    max_id = conn.execute(f'SELECT max("40001") FROM "{gtab}"').fetchone()[0]
    last = (max_id or 0) + 1
    while True:
        try:
            rows = read_batch("<", "DESC", last)
        except Exception as e:
            print(f"反向在 rowid={last} 处结束（累计 {bwd:,} 条）: {e}", flush=True)
            break
        if not rows:
            break
        for r in rows:
            m = _lean(tuple(r), me)
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            bucket("group", m)
        bwd += len(rows)
        last = rows[-1]["40001"]
        if bwd % 500000 < 200000:
            print(f"  反向 {bwd:,} 条", flush=True)

    conn.close()

    total = sum(len(c["messages"]) for c in convs.values())
    print(f"\n共 {len(convs)} 个会话 / {total:,} 条消息（group 正向 {fwd:,} + 反向 {bwd:,}）", flush=True)

    for c in convs.values():
        c["messages"].sort(key=lambda m: (m["t"] or 0, m["id"] or 0))

    names = {}
    if args.names:
        names = {str(k): str(v) for k, v in json.loads(Path(args.names).read_text(encoding="utf-8")).items()}
    for c in convs.values():
        c["title"] = names.get(c["key"], names.get(str(c["peer_qq"]), c["peer_qq"] or c["peer_uid"] or "未知"))

    out = Path(args.out)
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
            "first_time": c["messages"][0]["t"] if c["messages"] else None,
            "last_time": c["messages"][-1]["t"] if c["messages"] else None,
        }
        idx.append(entry)

        if "json" in args.format:
            (out / "json" / f"{stem}.json").write_text(
                json.dumps({"conversation": entry, "messages": c["messages"]}, ensure_ascii=False),
                encoding="utf-8",
            )
        if "txt" in args.format:
            lines = ["=" * 70, f"会话: {c['title']}", f"类型: {'群聊' if c['kind']=='group' else '私聊'}",
                     f"标识: {key}", f"消息数: {len(c['messages'])}", "=" * 70, ""]
            for m in c["messages"]:
                who = m["sq"] or m["su"] or "?"
                arrow = "->" if m["d"] == "out" else "<-"
                lines.append(f"[{m['ts'] or '未知时间'}] {arrow} {who}")
                t = (m["text"] or "").rstrip() or "（非文本消息）"
                lines.extend(f"    {ln}" for ln in t.splitlines())
                lines.append("")
            (out / "txt" / f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8", newline="\n")

        if (i + 1) % 200 == 0:
            print(f"  已导出 {i+1}/{len(convs)} 个会话", flush=True)

    (out / "index.json").write_text(
        json.dumps({"conversation_count": len(idx), "message_count": total, "conversations": idx},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    with open(out / "_index.txt", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"QQ 聊天记录导出索引\n\n会话数: {len(idx)}   消息数: {total:,}\n\n")
        for e in idx:
            rng = ""
            if e["first_time"] and e["last_time"]:
                rng = f"  {_fmt(e['first_time'])} ~ {_fmt(e['last_time'])}"
            fh.write(f"{e['id']:<26} {e['message_count']:>9,} 条{rng}\n")

    print(f"\n导出完成 -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
