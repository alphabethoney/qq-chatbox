"""OpenAI 兼容客户端（可接通义 / DeepSeek / 任意兼容端点）。"""

from __future__ import annotations

import json
import os
import urllib.request

from .base import LLMProvider


class OpenAILLM(LLMProvider):
    name = "openai"

    def __init__(self, model: str, base_url: str = "", api_key: str = "", **_) -> None:
        self.model = model or "gpt-4o-mini"
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    def chat(self, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 1024) -> str:
        if not self.api_key:
            raise RuntimeError("openai 后端需要 api_key（配置 llm.api_key 或环境变量 OPENAI_API_KEY）")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = _post(f"{self.base_url}/chat/completions", payload, self.api_key)
        return data["choices"][0]["message"]["content"]


def _post(url: str, payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))
