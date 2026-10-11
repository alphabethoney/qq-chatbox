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
        self._member_requests: dict[str, tuple[BridgeRuntime, str, float]] = {}
        self._request_sequence = 0

    def process_event(self, event: dict, on_event) -> list[Outbound]:
        """Route replies without waiting inside the WebSocket receive loop."""
        if not isinstance(event, dict):
            return []
        now = time.monotonic()
        for echo, (owner, group_id, sent) in list(self._member_requests.items()):
            if now - sent >= float(getattr(owner.cfg, "group_member_request_timeout", 10)):
                self._member_requests.pop(echo, None)
        # Only an action response with a live, matching echo may populate the cache.
        if not event.get("post_type"):
            echo = event.get("echo")
            pending = self._member_requests.pop(echo, None) if isinstance(echo, str) else None
            if pending and event.get("status") == "ok" and event.get("retcode") == 0:
                owner, group_id, _ = pending
                owner.update_group_members(group_id, event.get("data"))
            return []
        owner = getattr(on_event, "__self__", None)
        if not isinstance(owner, BridgeRuntime):
            return list(on_event(event, self.self_id))
        if event.get("post_type") == "notice" and event.get("notice_type") in (
            "group_increase", "group_decrease", "group_admin", "group_card"
        ):
            group_id = str(event.get("group_id", ""))
            owner.invalidate_group_members(group_id)
            # Discard an in-flight pre-notice roster as well.
            for echo, (runtime, gid, _) in list(self._member_requests.items()):
                if runtime is owner and gid == group_id:
                    self._member_requests.pop(echo, None)
            return []
        actions = []
        if (event.get("post_type") == "message" and event.get("message_type") == "group"
                and getattr(owner.cfg, "group_member_check", False)):
            group_id = str(event.get("group_id", ""))
            sid = owner.cfg.self_id or self.self_id or event.get("self_id", 0)
            if (group_id.isdigit() and str(event.get("user_id", "")).isdigit()
                    and str(event.get("user_id")) != str(sid)
                    and owner._should_reply_group(event, sid)
                    and owner.group_members(group_id) is None
                    and not any(runtime is owner and gid == group_id
                                for runtime, gid, _ in self._member_requests.values())):
                self._request_sequence += 1
                echo = f"group-members:{self._request_sequence}"
                self._member_requests[echo] = (owner, group_id, now)
                actions.append(Outbound("get_group_member_list", {"group_id": int(group_id)}, echo))
        actions.extend(on_event(event, self.self_id))
        return actions

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
                self._member_requests.clear()
                runtime = getattr(on_event, "__self__", None)
                if isinstance(runtime, BridgeRuntime):
                    runtime._members.clear()
                self._connect()
                continue
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("post_type") == "meta_event" and event.get("meta_event_type") == "lifecycle":
                if event.get("sub_type") == "connect" and not self.self_id:
                    self.self_id = event.get("self_id", 0)
                    print(f"机器人账号：{self.self_id}")
                continue
            for out in self.process_event(event, on_event):
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
