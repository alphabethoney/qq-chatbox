"""从 NapCat OneBot11 拉取群列表，生成 --names 映射（群号 → 群名）。

用途：聊天记录导出时，群聊会话的 title 默认回退成群号（QQ 号）；把群名映射喂给
导出器（``--names``）后，会话标题就是人读的群名，也能被「发送前脱敏」正确覆盖。

用法（需 NapCat 已登录导出账号、OneBot11 正向 WebSocket 已启动）：
    python tools/export_group_names.py --ws ws://127.0.0.1:3001 --out names.json

依赖：websocket-client（``pip install websocket-client``）。
"""

from __future__ import annotations

import argparse
import json
import sys


def get_group_list(ws_url: str, timeout: int = 10) -> list[dict]:
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise SystemExit("需要 websocket-client：pip install websocket-client") from exc

    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({"action": "get_group_list", "params": {}, "echo": "grp"}))
        while True:
            raw = ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("echo") == "grp":
                status = msg.get("status")
                retcode = msg.get("retcode")
                if status not in (None, "ok") or retcode not in (None, 0):
                    raise RuntimeError(f"get_group_list 失败：{msg}")
                return msg.get("data") or []
    finally:
        ws.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:3001", help="NapCat OneBot11 正向 WebSocket 地址")
    ap.add_argument("--out", default="names.json", help="输出的 names 映射 JSON")
    args = ap.parse_args()

    groups = get_group_list(args.ws)
    mapping = {
        str(g.get("group_id")): str(g.get("group_name") or g.get("group_id"))
        for g in groups
        if g.get("group_id") is not None
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"已写入 {len(mapping)} 个群名映射 -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
