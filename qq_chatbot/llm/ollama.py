"""本地 ollama 客户端（零 SDK 依赖，urllib）。"""

from __future__ import annotations

import json
import urllib.request

from .base import LLMProvider


class OllamaLLM(LLMProvider):
    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://localhost:11434", **_) -> None:
        self.model = model or "qwen2.5:7b"
        self.base_url = (base_url or "http://localhost:11434").rstrip("/")

    def chat(self, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 1024) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        data = _post(f"{self.base_url}/api/chat", payload)
        return data.get("message", {}).get("content", "")


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))
