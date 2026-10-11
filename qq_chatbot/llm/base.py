"""LLM 后端抽象。"""

from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):
    """生成式对话后端的最小接口。

    ``messages`` 是 OpenAI 风格的 ``[{"role": "system"|"user"|"assistant", "content": str}]``。
    """

    name: str

    def chat(self, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 1024) -> str:
        ...
