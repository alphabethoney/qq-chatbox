"""人格档案提炼：离线统计 + 可选 LLM 结构化摘要。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .schema import Corpus, PersonaProfile, speaker_qq
from .index.embeddings import tokenize

_STOP = {
    "我", "你", "他", "她", "它", "的", "了", "是", "在", "就", "都", "也", "还",
    "有", "和", "与", "或", "吧", "啊", "呢", "吗", "哦", "嗯", "好", "不", "没",
    "这个", "那个", "一个", "什么", "怎么", "这样", "那样", "我们", "你们", "他们",
    "the", "a", "an", "is", "are", "to", "of", "and", "in", "for", "it",
}

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF\U0000FE00-\U0000FE0F]"
)
_SENT_SPLIT = re.compile(r"[。！？!?；;\n]")


def build_profile(
    corpus: Corpus,
    speaker_key: str,
    llm=None,
    *,
    kind: str | None = None,
    sample: int = 200,
    progress=None,
) -> PersonaProfile:
    # 用 QQ 号匹配这个人（跨私聊 + 群聊），并按 kind 细分（c2c=私聊人格 / group=群聊人格）
    qq = speaker_qq(speaker_key)
    all_turns = [t for t in corpus.turns if speaker_qq(t.speaker.key) == qq]
    turns = [t for t in all_turns if t.kind == kind] if kind is not None else all_turns
    profile = PersonaProfile(speaker=speaker_key)
    if not all_turns:
        return profile

    texts = [t.text for t in turns]
    profile.style = _offline_style(texts)
    profile.topics = _top_topics(texts)

    if llm is not None and getattr(llm, "name", "") not in ("mock", ""):
        # 完整时间线：跨私聊 + 群聊，按时间先后抽取关键事件
        try:
            if progress:
                progress(f"LLM 提炼时间线（{speaker_key}，{len(all_turns)} 轮）…")
            profile.timeline = _llm_timeline(llm, speaker_key, all_turns)
        except Exception as exc:  # noqa: BLE001
            if progress:
                progress(f"时间线提炼跳过（LLM 失败）：{exc}")
        try:
            if progress:
                progress(f"LLM 提炼人格档案（{speaker_key}，采样 {min(len(texts), sample)} 轮）…")
            enriched = _llm_summarize(llm, speaker_key, texts[:sample])
            if enriched:
                if enriched.get("style"):
                    profile.style.update(enriched["style"])
                profile.facts = enriched.get("facts") or []
                profile.opinions = enriched.get("opinions") or []
                profile.relationships = enriched.get("relationships") or []
        except Exception as exc:  # noqa: BLE001 —— LLM 失败不阻断主流程
            if progress:
                progress(f"人格提炼跳过（LLM 失败）：{exc}")
    return profile


def _offline_style(texts: list[str]) -> dict:
    all_text = "\n".join(texts)
    sentences = [s for s in _SENT_SPLIT.split(all_text) if s.strip()]
    avg_len = round(sum(len(s) for s in sentences) / len(sentences), 1) if sentences else 0.0

    words = Counter(t for t in tokenize(all_text) if t not in _STOP)
    catchphrases = [w for w, _ in words.most_common(12) if len(w) >= 2]
    emojis = [e for e, _ in Counter(_EMOJI.findall(all_text)).most_common(8)]

    return {
        "avg_sentence_len": avg_len,
        "catchphrases": catchphrases,
        "emojis": emojis,
        "tone": _guess_tone(all_text),
    }


def _guess_tone(text: str) -> str:
    exclaim = text.count("！") + text.count("!")
    question = text.count("？") + text.count("?")
    total = max(len(text), 1)
    if (exclaim + question) / total > 0.02:
        return "活泼、语气强烈"
    if question / total > 0.01:
        return "常提问、偏随和"
    return "平实、简洁"


def _top_topics(texts: list[str], n: int = 15) -> list[str]:
    words = Counter(t for t in tokenize("\n".join(texts)) if t not in _STOP and len(t) >= 2)
    return [w for w, _ in words.most_common(n)]


_SUMMARY_PROMPT = """你是一名文本分析师。下面是从某个人的聊天记录里抽取的样本。\
请提炼出这个人的说话风格、关键事实、观点、以及与他人的关系。\
只输出一个 JSON 对象，不要输出任何其他文字，结构如下：
{{"style": {{"tone": "…", "catchphrases": ["…"]}}, "facts": [{{"topic": "…", "fact": "…"}}], "opinions": [{{"topic": "…", "opinion": "…"}}], "relationships": [{{"person": "…", "relation": "…"}}]}}

样本：
{corpus}
"""


def _llm_summarize(llm, speaker_key: str, texts: list[str]) -> dict | None:
    corpus = "\n".join(f"- {t}" for t in texts[:200])
    messages = [
        {"role": "system", "content": "你只输出合法 JSON。"},
        {"role": "user", "content": _SUMMARY_PROMPT.format(corpus=corpus)},
    ]
    raw = llm.chat(messages, temperature=0.2, max_tokens=1500)
    return _parse_json(raw)


def _parse_json(raw: str) -> dict | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


_TIMELINE_PROMPT = """你是一名文本分析师。下面是从某个人聊天记录里按时间抽样得到的片段（每条前面带时间戳）。\
请提炼这个人的「时间线」：关键事件、状态/身份变化、重要节点，按时间先后排列。\
只输出一个 JSON 对象，不要输出其他文字，结构如下：
{{"timeline": [{{"time": "YYYY-MM", "event": "一句话概括"}}]}}
规则：time 用 YYYY-MM 格式；event 简洁、只写能确定的事实；最多 20 条。

样本：
{corpus}
"""


def _sample_for_timeline(turns, buckets: int = 20, per_bucket: int = 6):
    """按时间均匀分桶抽样，保证时间线覆盖整个时间范围。"""
    turns = sorted(turns, key=lambda t: t.time)
    if len(turns) <= buckets * per_bucket:
        return turns
    step = len(turns) / buckets
    sampled: list = []
    for i in range(buckets):
        chunk = turns[int(i * step) : int((i + 1) * step)]
        if not chunk:
            continue
        if len(chunk) <= per_bucket:
            sampled.extend(chunk)
        else:
            for j in range(per_bucket):
                idx = int(j * (len(chunk) - 1) / (per_bucket - 1))
                sampled.append(chunk[idx])
    return sampled


def _llm_timeline(llm, speaker_key: str, turns) -> list[dict]:
    sampled = _sample_for_timeline(turns)
    corpus = "\n".join(f"- [{t.time_str}] {t.text[:60]}" for t in sampled)
    messages = [
        {"role": "system", "content": "你只输出合法 JSON。"},
        {"role": "user", "content": _TIMELINE_PROMPT.format(corpus=corpus)},
    ]
    raw = llm.chat(messages, temperature=0.2, max_tokens=1200)
    data = _parse_json(raw)
    return (data or {}).get("timeline") or []


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def profiles_dir(store_dir: Path) -> Path:
    return Path(store_dir) / "profiles"


def profile_path(store_dir: Path, speaker_key: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", speaker_key)
    return profiles_dir(store_dir) / f"{safe}.json"


def save_profile(store_dir: Path, profile: PersonaProfile) -> Path:
    path = profile_path(store_dir, profile.speaker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "speaker": profile.speaker,
                "style": profile.style,
                "facts": profile.facts,
                "opinions": profile.opinions,
                "relationships": profile.relationships,
                "topics": profile.topics,
                "timeline": profile.timeline,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_profiles(store_dir: Path) -> dict[str, PersonaProfile]:
    out: dict[str, PersonaProfile] = {}
    d = profiles_dir(store_dir)
    if not d.exists():
        return out
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out[data["speaker"]] = PersonaProfile(
                speaker=data["speaker"],
                style=data.get("style", {}),
                facts=data.get("facts", []),
                opinions=data.get("opinions", []),
                relationships=data.get("relationships", []),
                topics=data.get("topics", []),
                timeline=data.get("timeline", []),
            )
        except (json.JSONDecodeError, KeyError):
            continue
    return out
