"""提示词拼装：qa（历史问答）与 persona（数字分身）两套模板。"""

from __future__ import annotations

from .schema import PersonaProfile, RetrievedTurn

QA_SYSTEM = (
    "你是「聊天记录助手」。你只能依据用户提供的聊天记录证据回答，不得编造记录里不存在的内容。\n"
    "规则：\n"
    "1. 回答时尽量引用证据，并给出时间与说话人。\n"
    "2. 如果证据不足以回答问题，必须明确说「记录里没有相关证据」。\n"
    "3. 用简洁、自然的中文回答。"
)


def _evidence_block(items: list[RetrievedTurn]) -> str:
    lines = []
    for i, rt in enumerate(items, 1):
        t = rt.turn
        lines.append(f"[{i}] {t.conversation_key} · {t.time_str} · {t.speaker.name}")
        lines.append(f"    {t.text}")
    return "\n".join(lines)


def build_qa_messages(
    query: str,
    retrieved: list[RetrievedTurn],
    history: list[dict] | None = None,
) -> list[dict]:
    parts = []
    if retrieved:
        parts.append("以下是从聊天记录里检索到的相关片段（每条带出处）：")
        parts.append(_evidence_block(retrieved))
    else:
        parts.append("（未检索到相关记录）")
    parts.append("")
    parts.append(f"问题：{query}")
    parts.append("请回答：")

    msgs = [{"role": "system", "content": QA_SYSTEM}]
    msgs.extend(history or [])
    msgs.append({"role": "user", "content": "\n".join(parts)})
    return msgs


def _persona_system(profile: PersonaProfile) -> str:
    name = profile.name or (profile.speaker.rsplit(":", 1)[-1] if ":" in profile.speaker else profile.speaker)
    facts = "；".join(f"{f.get('topic', '')}: {f.get('fact', '')}" for f in profile.facts) or "（无）"
    opinions = "；".join(f"{o.get('topic', '')}: {o.get('opinion', '')}" for o in profile.opinions) or "（无）"
    rels = "；".join(f"{r.get('person', '')} → {r.get('relation', '')}" for r in profile.relationships) or "（无）"
    style = profile.style
    tone = style.get("tone", "自然")
    phrases = "、".join(style.get("catchphrases", [])) or "（无）"
    avg_len = style.get("avg_sentence_len")
    len_hint = f"- 平均句长约 {avg_len} 字" if avg_len else "- 平均句长：短"
    return (
        f"你是 {name} 的数字分身，请用 TA 的口吻、以第一人称聊天。\n"
        f"- 说话风格：{tone}\n"
        f"{len_hint}\n"
        f"- 口头禅/常用词：{phrases}\n"
        f"- 关于你的事实：{facts}\n"
        f"- 你与他人的关系：{rels}\n"
        f"- 你的观点：{opinions}\n"
        "规则：自然、像本人一样说话；可以引用下面提供的真实聊天片段里的内容；"
        "不要编造与上述事实冲突的内容。"
        "回复要简短，像真人发消息一样，一般就 1~3 句，别长篇大论。"
    )


def build_persona_messages(
    query: str,
    retrieved: list[RetrievedTurn],
    profile: PersonaProfile,
    history: list[dict] | None = None,
) -> list[dict]:
    parts = []
    if retrieved:
        parts.append("以下是真实的聊天片段（供你模仿语气与用词，并可引用其中的事实）：")
        parts.append(_evidence_block(retrieved))
        parts.append("")
    parts.append(f"对方说：{query}")
    parts.append("请以本人身份回复：")

    msgs = [{"role": "system", "content": _persona_system(profile)}]
    msgs.extend(history or [])
    msgs.append({"role": "user", "content": "\n".join(parts)})
    return msgs
