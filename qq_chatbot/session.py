"""对话记忆：短期环形缓冲（长期摘要留待扩展）。"""

from __future__ import annotations


class Session:
    def __init__(self, max_turns: int = 12) -> None:
        self.max_turns = max_turns
        self.history: list[dict] = []

    def add(self, user: str, assistant: str) -> None:
        self.history.append({"role": "user", "content": user})
        self.history.append({"role": "assistant", "content": assistant})
        limit = self.max_turns * 2
        if len(self.history) > limit:
            self.history = self.history[-limit:]

    def as_messages(self) -> list[dict]:
        return list(self.history)
