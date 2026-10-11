"""轻量评测指标。"""

from __future__ import annotations

from ..schema import Answer, RetrievedTurn


def recall_at_k(gold_turn_ids: set[str], retrieved: list[RetrievedTurn], k: int | None = None) -> float:
    if not gold_turn_ids:
        return 1.0
    hits = [rt.turn.id for rt in retrieved[:k]]
    return len(gold_turn_ids & set(hits)) / len(gold_turn_ids)


def mrr(gold_turn_ids: set[str], retrieved: list[RetrievedTurn]) -> float:
    for rank, rt in enumerate(retrieved, 1):
        if rt.turn.id in gold_turn_ids:
            return 1.0 / rank
    return 0.0


def contains_evidence(answer: Answer, evidence_substrings: list[str]) -> bool:
    """判断回答文本是否包含任一条证据子串（用于 mock LLM 的确定性校验）。"""
    return any(s and s in answer.text for s in evidence_substrings)
