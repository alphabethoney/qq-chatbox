"""确定性 mock LLM，供离线自测与验收。

规则：回复内容 = 最后一条 user 消息里的「问题」+ 证据摘要，稳定可断言。
"""

from __future__ import annotations

import json
import re


class MockLLM:
    name = "mock"

    def __init__(self, model: str = "", **_) -> None:
        self.model = model or "mock"

    def chat(self, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 1024) -> str:
        last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        # 提取「问题：」或「对方说：」之后的一行
        question = ""
        m = re.search(r"(?:问题|对方说)[：:]\s*(.+)", last)
        if m:
            question = m.group(1).strip()
        # 统计证据片段数量（[n] 编号）
        cites = re.findall(r"^\[(\d+)\]", last, flags=re.M)
        evidence = f"，命中 {len(cites)} 条证据" if cites else ""
        body = question or "（无问题）"
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        if "数字分身" in system:
            return f"[persona]{body}{evidence}"
        return f"[qa]{body}{evidence}"


class MockEmbedding:
    """与 MockLLM 配套的确定性向量（直接返回输入文本的哈希向量，见 index.embeddings.HashEmbedding）。"""

    name = "mock"

    def embed(self, texts: list[str]) -> list[list[float]]:
        from ..index.embeddings import hash_embed

        return [hash_embed(t) for t in texts]
