"""输入装载：把导出器的产出（明文库或 JSON 目录）归一化为 ``Corpus``。

复用 ``qq_exporter.collect.build_conversations``（其内部已复用
``db.iter_messages`` + ``wire.parse_body``），因此解密/解析逻辑不在这里重复。
"""

from __future__ import annotations

import json
from pathlib import Path

from .schema import Corpus
from .turns import build_turns


def load_corpus(
    *,
    db: str | Path | None = None,
    json_dir: str | Path | None = None,
    names: str | Path | None = None,
    limit: int | None = None,
    since: int | None = None,
    people: set[str] | None = None,
    conversations: set[str] | None = None,
    person: str | None = None,
    progress=None,
) -> Corpus:
    """从明文库或 JSON 导出目录装载语料。

    参数
    ----
    db / json_dir：二选一。
    names：会话名称映射文件（同导出器 ``--names``）。
    limit：只取前 N 条消息（调试用）。
    since：只保留该时间戳之后的消息（秒）。
    people：只保留这些说话人（Speaker.key 集合）。
    conversations：只保留这些会话（conversation_key 集合，如 c2c:10001）。
    person：只保留这个 QQ 号的人（跨私聊 + 所有群）。
    """
    if bool(db) == bool(json_dir):
        raise ValueError("load_corpus 需要 --db 或 --json 之一（且只能其一）")

    if db:
        convs, meta = _from_db(Path(db), names, limit, progress)
        source = str(db)
    else:
        convs, meta = _from_json(Path(json_dir), limit)
        source = str(json_dir)

    turns = build_turns(convs, since=since, people=people, conversation_keys=conversations, person_qq=person)
    speakers = {t.speaker.key: t.speaker for t in turns}
    meta = dict(meta)
    meta.update({"source": source, "turn_count": len(turns), "speaker_count": len(speakers)})
    if progress:
        progress(f"语料就绪：{len(turns)} 轮 / {len(speakers)} 个说话人")
    return Corpus(speakers=speakers, turns=turns, meta=meta)


def _from_db(db: Path, names, limit, progress) -> tuple[list[dict], dict]:
    from qq_exporter.collect import build_conversations, load_names

    name_map = load_names(Path(names)) if names else {}
    meta, conversations = build_conversations(
        db, names=name_map, limit=limit, progress=progress
    )
    out = [
        {
            "key": c.key,
            "kind": c.kind,
            "title": c.title,
            "group_id": c.group_id,
            "messages": c.messages,
        }
        for c in conversations
    ]
    return out, meta


def _from_json(json_dir: Path, limit) -> tuple[list[dict], dict]:
    json_dir = Path(json_dir)
    index_path = json_dir / "index.json"
    if not index_path.exists():
        raise FileNotFoundError(f"未找到 {index_path}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    meta = index.get("meta", {})

    out: list[dict] = []
    total = 0

    if index.get("file"):
        # --single-json：所有会话合并进一个文件
        data = json.loads((json_dir / index["file"]).read_text(encoding="utf-8"))
        for entry in data.get("conversations", []):
            conv = _conv_from_entry(entry)
            conv["messages"] = entry.get("messages", [])
            out.append(conv)
            total += len(conv["messages"])
            if limit and total >= limit:
                break
    else:
        for entry in index.get("conversations", []):
            fname = entry.get("file")
            if not fname:
                continue
            data = json.loads((json_dir / fname).read_text(encoding="utf-8"))
            conv = _conv_from_entry(entry)
            conv["messages"] = data.get("messages", [])
            out.append(conv)
            total += len(conv["messages"])
            if limit and total >= limit:
                break
    return out, meta


def _conv_from_entry(entry: dict) -> dict:
    return {
        "key": entry.get("id") or entry.get("key"),
        "kind": entry.get("kind"),
        "title": entry.get("title"),
        "group_id": entry.get("group_id"),
    }
