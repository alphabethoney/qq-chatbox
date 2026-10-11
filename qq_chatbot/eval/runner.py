"""Offline reproducible retrieval evaluation; no LLM judge or private fixtures."""
from __future__ import annotations

import json
from pathlib import Path

from .metrics import recall_at_k, mrr


def evaluate(retriever, cases: list[dict], *, k: int = 5) -> dict:
    if k < 1 or not cases:
        raise ValueError("评测需要非空样本集与正整数 k")
    rows = []
    allowed = {"speaker", "person_qq", "conversation", "kind", "time_range"}
    for i, case in enumerate(cases):
        query = case.get("query")
        gold = case.get("gold_turn_ids")
        if not isinstance(query, str) or not query.strip() or not isinstance(gold, list) or not gold or not all(isinstance(x, str) for x in gold):
            raise ValueError(f"样本 {i} 需要 query 和非空 gold_turn_ids 字符串列表")
        filters = case.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - allowed:
            raise ValueError(f"样本 {i} 含不支持的过滤器")
        found = retriever.retrieve(query, top_k=k, **filters)
        expected = set(gold)
        rows.append({"case": i, "hit_at_k": float(any(r.turn.id in expected for r in found)),
                     "recall_at_k": recall_at_k(expected, found, k), "mrr": mrr(expected, found),
                     "retrieved_count": len(found),
                     "embedding": dict(getattr(retriever, "last_embedding", {}) or {})})
    return {"version": 1, "k": k, "count": len(rows),
            "metrics": {key: sum(row[key] for row in rows) / len(rows)
                        for key in ("hit_at_k", "recall_at_k", "mrr")},
            "cases": rows,
            "limitations": "检索指标不证明生成事实正确或人格一致；后者需独立人工标注。"}


def load_cases(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError("评测文件必须是 JSON 对象数组")
    return data


def evaluate_reviews(reviews: list[dict]) -> dict:
    """Aggregate human labels; does not infer truth from lexical overlap."""
    criteria = ("style", "factuality", "timeline")
    if not reviews:
        raise ValueError("人格评审标注不能为空")
    for row in reviews:
        if not isinstance(row, dict) or not isinstance(row.get("case"), str):
            raise ValueError("人格评审需要字符串 case 标识")
        for name in criteria:
            value = row.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"人格评审 {name} 需要 0..1 人工评分")
    if len({row["case"] for row in reviews}) != len(reviews):
        raise ValueError("人格评审 case 标识不能重复")
    return {"count": len(reviews), "source": "human_annotations",
            "means": {name: sum(row[name] for row in reviews) / len(reviews) for name in criteria}}


def demo_retriever(config):
    from ..schema import Speaker, Turn
    from ..index import build_store, HybridRetriever, make_embedding
    speaker = Speaker("group:100:10001", "示例成员")
    turns = [Turn("demo-meeting", speaker, "group:100", "group", 1700000000, "2023-11-14", "项目会议安排在周三下午。"),
             Turn("demo-lunch", speaker, "group:100", "group", 1700000010, "2023-11-14", "周五中午一起午餐聚餐。"),
             Turn("demo-book", speaker, "group:100", "group", 1700000020, "2023-11-14", "周末计划在家读书。")]
    embed = make_embedding(config.embedding)
    return HybridRetriever(build_store(turns, embed), embed, config.retrieval)


def compare(current: dict, baseline: dict) -> dict:
    if current["k"] != baseline.get("k") or current["count"] != baseline.get("count"):
        raise ValueError("对比报告的 k 和样本数量必须相同")
    return {name: value - baseline["metrics"][name] for name, value in current["metrics"].items()}
