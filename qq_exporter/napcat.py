"""NapCat 富媒体 API 客户端（纯 urllib，零额外依赖）。

媒体下载 URL 里的 ``rkey`` 约 2 小时过期；但 ``fileid`` 是稳定的。
NapCat 能用 ``fileid`` 换新 rkey / 新直链，用于补齐被 QQ 清理掉的旧媒体。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path


class NapCat:
    """OneBot 11 / NapCat HTTP 客户端。"""

    def __init__(self, base: str = "http://127.0.0.1:6099", token: str | None = None):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, action: str, params: dict) -> dict:
        payload = {"action": action, "params": params}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            self.base + "/" + action,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def refresh(self, media: dict) -> dict:
        """按媒体类型换取新的下载地址。

        返回 ``{"url": ...}``、``{"path": ...}`` 或 ``{"base64": ...}``，
        失败返回空字典。
        """
        fid = media.get("fileid")
        if not fid:
            return {}
        kind = media.get("type")
        if kind == "image":
            action, params = "get_image", {"file": fid}
        elif kind == "voice":
            action, params = "get_record", {"file": fid, "out_format": "mp3"}
        elif kind == "video":
            action, params = "get_video", {"file": fid}
        else:
            action, params = "get_file", {"file_id": fid}

        try:
            data = self.call(action, params).get("data") or {}
        except Exception:  # noqa: BLE001
            return {}

        url = data.get("url")
        if isinstance(url, str) and url:
            return {"url": url}
        file = data.get("file")
        if isinstance(file, str) and file:
            if file.startswith(("http://", "https://")):
                return {"url": file}
            if Path(file).is_file():
                return {"path": file}
            return {"base64": file}
        return {}
