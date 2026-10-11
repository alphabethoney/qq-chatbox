"""OneBot11 传输层：连接 NapCat 正向 WebSocket，收发事件与动作。

需要 ``websocket-client``（可选依赖）：``pip install websocket-client``。
"""

from __future__ import annotations

import json
import time

from ..chat import Chatbot
from ..config import Config
from .policy import BridgeRuntime, Outbound


class OneBotClient:
    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self.ws = None
        self.self_id = 0

    def _connect(self) -> None:
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "接入 QQ 需要 websocket-client：pip install websocket-client"
            ) from exc
        self.ws = websocket.create_connection(self.ws_url, timeout=60)

    def send(self, out: Outbound) -> None:
        payload: dict = {"action": out.action, "params": out.params}
        if out.echo:
            payload["echo"] = out.echo
        self.ws.send(json.dumps(payload, ensure_ascii=False))

    def run(self, on_event) -> None:
        self._connect()
        print(f"已连接 NapCat：{self.ws_url}")
        while True:
            try:
                data = self.ws.recv()
            except Exception:  # noqa: BLE001 —— 断线重连
                print("连接断开，3 秒后重连…")
                time.sleep(3)
                self._connect()
                continue
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if event.get("post_type") == "meta_event" and event.get("meta_event_type") == "lifecycle":
                if event.get("sub_type") == "connect" and not self.self_id:
                    self.self_id = event.get("self_id", 0)
                    print(f"机器人账号：{self.self_id}")
                continue
            for out in on_event(event, self.self_id):
                try:
                    self.send(out)
                except Exception as exc:  # noqa: BLE001
                    print(f"发送失败：{exc}")


def run_bridge(chatbot: Chatbot, config: Config) -> None:
    runtime = BridgeRuntime(chatbot, config)
    client = OneBotClient(config.bridge.ws_url)
    try:
        client.run(runtime.handle)
    except KeyboardInterrupt:
        print("\n已退出")
