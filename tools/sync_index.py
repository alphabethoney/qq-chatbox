"""定时同步：把导出账号的新消息增量并入机器人索引，并推送到主机。

流程（每步可独立跑）：
    解密当前库 → 容错导出(live) → 与全量历史(base)合并去重 → 重建索引 → (可选)推送到主机并重启服务

用法:
    python tools/sync_index.py --passphrase <16字符口令> --person 10001 \
        [--base-json out/json] [--live-db <当前库路径>] [--out .chatbot] \
        [--host example-user@192.0.2.10] [--push]

口令可用 --passphrase 传，或用 --passphrase-file 指向一个只含口令的文件（避免出现在命令行历史里）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_LIVE_DB = Path.home() / "Documents/Tencent Files/20002/nt_qq/nt_db/nt_msg.db"


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def decrypt(live_db: Path, plain_db: Path, *, key: str = "", passphrase: str = "") -> None:
    from qq_exporter import sqlcipher

    if key:
        enc_key = bytes.fromhex(key)
    else:
        enc_key = sqlcipher.key_from_passphrase(live_db, passphrase)
    # 注意：QQ 9.9.x 的 -wal 文件用现有逻辑合并会损坏整库（quick_check 报 malformed），
    # 这里跳过 WAL，只解主库；主库本身可读，偶发 B-tree 坏页交给容错导出处理。
    stats = sqlcipher.decrypt_database(live_db, plain_db, enc_key, merge_wal=False, progress=None)
    print(f"解密完成: {plain_db}  页数={stats['pages']}  quick_check={stats['integrity']}", flush=True)
    if stats["integrity"] != "ok":
        print("警告: quick_check 未通过，后续容错导出会跳过坏页", flush=True)


def export_fault_tolerant(plain_db: Path, out_dir: Path) -> None:
    _run([sys.executable, str(ROOT / "tools/export_large.py"),
          "--db", str(plain_db), "--out", str(out_dir), "--format", "json"])


def _load_json_dir(d: Path) -> dict[str, dict]:
    """读一个 export 目录，返回 {conversation_key: {meta, messages}}。"""
    index = json.loads((d / "index.json").read_text(encoding="utf-8"))
    convs: dict[str, dict] = {}
    for entry in index.get("conversations", []):
        key = entry.get("id")
        fname = entry.get("file")
        if not key or not fname:
            continue
        fp = d / fname
        if not fp.exists():
            continue
        data = json.loads(fp.read_text(encoding="utf-8"))
        convs[key] = {
            "entry": {k: v for k, v in entry.items() if k != "file"},
            "messages": data.get("messages", []),
        }
    return convs


def merge(base_dir: Path, live_dir: Path, merged_dir: Path) -> None:
    """合并两个导出目录：按会话合并消息、按 msg_id 去重、按时间排序。"""
    base = _load_json_dir(base_dir)
    live = _load_json_dir(live_dir)
    merged_dir.mkdir(parents=True, exist_ok=True)

    all_keys = dict.fromkeys(list(base) + list(live))
    idx = []
    for i, key in enumerate(all_keys):
        b = base.get(key, {"entry": {}, "messages": []})
        l = live.get(key, {"entry": {}, "messages": []})
        entry = {**b["entry"], **l["entry"], "id": key, "kind": l["entry"].get("kind") or b["entry"].get("kind")}
        entry.setdefault("title", str(key))
        seen: set = set()
        msgs: list[dict] = []
        for m in b["messages"] + l["messages"]:
            mid = m.get("msg_id")
            if mid is not None and mid in seen:
                continue
            if mid is not None:
                seen.add(mid)
            msgs.append(m)
        msgs.sort(key=lambda m: (m.get("time") or 0, m.get("msg_id") or 0))
        entry["message_count"] = len(msgs)
        entry["first_time"] = msgs[0].get("time") if msgs else None
        entry["last_time"] = msgs[-1].get("time") if msgs else None

        stem = f"{i:04d}_{_safe(entry['title'], key)}"
        entry["file"] = f"{stem}.json"
        (merged_dir / entry["file"]).write_text(
            json.dumps({"conversation": entry, "messages": msgs}, ensure_ascii=False),
            encoding="utf-8",
        )
        idx.append(entry)

    total = sum(e["message_count"] for e in idx)
    (merged_dir / "index.json").write_text(
        json.dumps({"conversation_count": len(idx), "message_count": total, "conversations": idx},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"合并完成: {len(idx)} 会话 / {total:,} 条 -> {merged_dir}", flush=True)


def _safe(name: str, fallback: str) -> str:
    import re

    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(". ")
    return (name or fallback)[:60]


def build(person: str, json_dir: Path, out: Path) -> None:
    _run([sys.executable, "-m", "qq_chatbot", "build",
          "--json", str(json_dir), "--out", str(out), "--person", person])


def push(host: str, out: Path) -> None:
    _run(["scp", "-q", "-r", str(out), f"{host}:~/qq-chatbot/"])
    # 重启远端服务，加载新索引
    subprocess.run(["ssh", host, "systemctl --user restart qq-chatbot"], check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passphrase", help="16 字符 ASCII 口令")
    ap.add_argument("--passphrase-file", help="只含口令的文件（比命令行更安全）")
    ap.add_argument("--key", help="32 字节密钥（hex 64 字符），优先于口令")
    ap.add_argument("--live-db", default=str(DEFAULT_LIVE_DB), help="导出账号当前库路径")
    ap.add_argument("--person", default="10001", help="只索引这个 QQ 号的人")
    ap.add_argument("--base-json", default="out/json", help="全量历史导出目录（首次同步保留）")
    ap.add_argument("--work", default="_work/sync", help="中间文件目录")
    ap.add_argument("--out", default=".chatbot", help="索引输出目录")
    ap.add_argument("--host", default="example-user@192.0.2.10", help="机器人主机 SSH 地址")
    ap.add_argument("--push", action="store_true", help="推送到主机并重启服务")
    args = ap.parse_args()

    passphrase = args.passphrase
    if not passphrase and args.passphrase_file:
        passphrase = Path(args.passphrase_file).read_text(encoding="utf-8").strip()
    if not args.key and not passphrase:
        print("需要 --passphrase / --passphrase-file 或 --key")
        return 1

    work = Path(args.work)
    live_json = work / "live" / "json"
    merged_json = work / "merged" / "json"
    plain_db = work / "plain" / "nt_msg.db"

    decrypt(Path(args.live_db), plain_db, key=args.key or "", passphrase=passphrase or "")
    export_fault_tolerant(plain_db, work / "live")

    base_json = Path(args.base_json)
    if base_json.exists():
        merge(base_json, live_json, merged_json)
        build(args.person, merged_json, Path(args.out))
    else:
        # 没有全量历史时，直接用 live 导出
        print("未找到 base-json，直接用 live 导出建索引", flush=True)
        build(args.person, live_json, Path(args.out))

    if args.push:
        push(args.host, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
