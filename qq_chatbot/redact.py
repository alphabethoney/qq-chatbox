"""发送前脱敏：把聊天记录里的真实身份信息替换成占位符再上云，回复后再还原。

用途：使用云端 LLM（openai 兼容端点）做「总结 / 生成」时，聊天内容原文会离开本机。
本模块在 ``llm.chat()`` 边界加一层过滤——把语料里的真实昵称、QQ 号、手机号、邮箱
替换成占位符，避免云端拿到真实身份；收到回复后再把昵称占位符还原，保证回答仍用真实称呼。
"""

from __future__ import annotations

import re
from typing import Iterable, Protocol

# 手机号（1 开头 11 位）、邮箱
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# QQ 号：5~11 位数字，前后不是数字（避免误伤更长的 id / 时间戳）
_QQ_RE = re.compile(r"(?<!\d)\d{5,11}(?!\d)")


class Redactor:
    """把一组真实标识符映射为可逆占位符，并脱敏数字/邮箱。

    - 昵称/群名等：映射为 ``〔名N〕``，回复后可还原。
    - QQ 号/手机号/邮箱：映射为 ``〔号码〕/〔电话〕/〔邮箱〕``，不回填（不泄露原值）。
    """

    def __init__(self, names: Iterable[str] = ()) -> None:
        self._map: dict[str, str] = {}
        self._rev: dict[str, str] = {}
        self._counter = 0
        # 按长度从长到短注册，避免短名先匹配破坏长名
        for name in sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True):
            self._register(name)

    def _register(self, real: str) -> None:
        if real in self._map or len(real) < 2:
            return
        ph = f"〔名{self._counter}〕"
        self._counter += 1
        self._map[real] = ph
        self._rev[ph] = real

    def redact(self, text: str) -> str:
        if not text:
            return text
        for real, ph in self._map.items():
            text = text.replace(real, ph)
        text = _PHONE_RE.sub("〔电话〕", text)
        text = _EMAIL_RE.sub("〔邮箱〕", text)
        text = _QQ_RE.sub("〔号码〕", text)
        return text

    def unredact(self, text: str) -> str:
        if not text:
            return text
        for ph, real in self._rev.items():
            text = text.replace(ph, real)
        return text


class _LLM(Protocol):
    name: str

    def chat(self, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 1024) -> str: ...


class RedactingLLM:
    """包装底层 LLM：请求前脱敏消息内容，回复后还原昵称占位符。"""

    name: str

    def __init__(self, inner: _LLM, redactor: Redactor) -> None:
        self._inner = inner
        self._redactor = redactor
        self.name = getattr(inner, "name", "unknown")

    def chat(self, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 1024) -> str:
        redacted = [
            {"role": m.get("role", "user"), "content": self._redactor.redact(m.get("content", ""))}
            for m in messages
        ]
        raw = self._inner.chat(redacted, temperature=temperature, max_tokens=max_tokens)
        return self._redactor.unredact(raw)
