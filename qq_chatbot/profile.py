"""人格档案提炼：离线统计 + 可选 LLM 结构化摘要。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .schema import Corpus, PersonaProfile
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
    sample: int = 200,
    progress=None,
) -> PersonaProfile:
    turns = [t for t in corpus.turns if t.speaker.key == speaker_key]
    profile = PersonaProfile(speaker=speaker_key)
    if not turns:
        return profile

    texts = [t.text for t in turns]
    profile.style = _offline_style(texts)
    profile.topics = _top_topics(texts)

    if llm is not None and getattr(llm, "name", "") not in ("mock", ""):
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
            )
        except (json.JSONDecodeError, KeyError):
            continue
    return out
