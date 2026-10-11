"""qq_chatbot 的规范数据模型。

把导出器产出（会话 + 消息字典）归一化成语义更明确的 ``Speaker`` / ``Turn`` / ``Corpus``，
后续索引、检索、提示词拼装都只依赖这些结构，从而屏蔽「明文库」与「JSON 导出」两种输入源的差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Speaker:
    key: str                # "c2c:10001" / "group:123456/10001" / "me"
    name: str               # 解析后的显示名
    is_self: bool = False

    def __hash__(self) -> int:
        return hash(self.key)


def speaker_qq(key: str) -> str:
    """从 speaker key 提取 QQ 号（末尾一段）。

    "c2c:10001" -> "10001"；"group:123:10001" -> "10001"；"me" -> "me"。
    用于「某个人跨私聊/群聊」的匹配。
    """
    return key.rsplit(":", 1)[-1]


@dataclass
class Turn:
    id: str
    speaker: Speaker
    conversation_key: str   # "c2c:10001" / "group:123456"
    kind: str               # "c2c" | "group"
    time: int
    time_str: str
    text: str
    msg_ids: list = field(default_factory=list)  # 该轮覆盖的原始消息 ID（用于出处）


@dataclass
class Corpus:
    speakers: dict[str, Speaker] = field(default_factory=dict)
    turns: list[Turn] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def speaker(self, key: str) -> Speaker:
        return self.speakers.setdefault(key, Speaker(key=key, name=key))


@dataclass
class RetrievedTurn:
    turn: Turn
    score: float
    vector_score: float = 0.0
    keyword_score: float = 0.0


@dataclass
class PersonaProfile:
    speaker: str
    name: str = ""                                        # 自称/昵称（空则回退到 speaker 的 QQ 号）
    style: dict = field(default_factory=dict)            # tone/catchphrases/emojis/avg_sentence_len
    facts: list[dict] = field(default_factory=list)      # {topic, fact, evidence}
    opinions: list[dict] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)   # 带来源的历史事实
    kind: str | None = None
    conversation: str | None = None
    timeline_state: dict = field(default_factory=dict)


@dataclass
class Citation:
    conversation_key: str
    time_str: str
    speaker: str
    text: str
    score: float = 0.0


@dataclass
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    grounded: bool = True  # 是否有达到阈值的检索证据，不代表事实已认证
    trace_id: str = ""
