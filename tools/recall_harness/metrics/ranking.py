"""Ranking metrics — precision_at_k, recall_at_k, MRR, NDCG.

Standard offline-eval ranking metrics. Each consumes symmetric (test, golden)
RRUs and returns a dict containing at minimum a `value` key.
"""
from __future__ import annotations

import math

from ..rru import RankingResultUnit
from .registry import register_metric


def _golden_relevance_map(golden: RankingResultUnit) -> dict[str, int]:
    """{id -> relevance} for nodes the curator graded. Default 0 (irrelevant)
    for ids absent from golden."""
    return {n.id: (n.grading or {}).get("relevance", 0) for n in golden.ranked_nodes}


@register_metric("precision_at_k")
def precision_at_k(test: RankingResultUnit, golden: RankingResultUnit, *, k: int = 5,
                    relevance_threshold: int = 2) -> dict:
    """Fraction of test's top-k that appear in golden at relevance >= threshold.

    Args:
        k: cutoff position in test ranking
        relevance_threshold: minimum golden grade to count as a "hit" (default 2)

    Returns: {value, hits, misses, k, threshold}
    """
    test_top_k = test.top_k_ids(k)
    rel_map = _golden_relevance_map(golden)
    hits = [tid for tid in test_top_k if rel_map.get(tid, 0) >= relevance_threshold]
    misses = [tid for tid in test_top_k if rel_map.get(tid, 0) < relevance_threshold]
    return {
        "value": len(hits) / k if k else 0.0,
        "hits": hits,
        "misses": misses,
        "k": k,
        "relevance_threshold": relevance_threshold,
    }


@register_metric("recall_at_k")
def recall_at_k(test: RankingResultUnit, golden: RankingResultUnit, *, k: int = 10,
                 relevance_threshold: int = 2) -> dict:
    """Fraction of golden's relevant nodes (relevance >= threshold) appearing
    in test's top-k. Saturates at 1.0 when k >= golden's relevant count.
    """
    test_top_k = set(test.top_k_ids(k))
    rel_map = _golden_relevance_map(golden)
    golden_relevant = {nid for nid, rel in rel_map.items() if rel >= relevance_threshold}
    if not golden_relevant:
        return {"value": None, "skip_reason": "no relevant in golden",
                "k": k, "relevance_threshold": relevance_threshold}
    hits = test_top_k & golden_relevant
    return {
        "value": len(hits) / len(golden_relevant),
        "hits": sorted(hits),
        "missed_relevant": sorted(golden_relevant - test_top_k),
        "k": k,
        "relevance_threshold": relevance_threshold,
        "golden_relevant_count": len(golden_relevant),
    }


@register_metric("mean_reciprocal_rank")
def mean_reciprocal_rank(test: RankingResultUnit, golden: RankingResultUnit,
                          *, relevance_threshold: int = 3) -> dict:
    """1 / rank of first test result with golden relevance >= threshold.
    Default threshold = 3 (highly-relevant). Per-query MRR; aggregate by
    averaging at the orchestrator level.
    """
    rel_map = _golden_relevance_map(golden)
    for i, node in enumerate(test.ranked_nodes, start=1):
        if rel_map.get(node.id, 0) >= relevance_threshold:
            return {"value": 1.0 / i, "first_hit_id": node.id, "first_hit_rank": i,
                    "relevance_threshold": relevance_threshold}
    return {"value": 0.0, "first_hit_id": None, "first_hit_rank": None,
            "relevance_threshold": relevance_threshold}


@register_metric("ndcg_at_k")
def ndcg_at_k(test: RankingResultUnit, golden: RankingResultUnit, *, k: int = 10) -> dict:
    """Normalized Discounted Cumulative Gain — graded-relevance ranking metric.

    Uses golden's `grading.relevance` (0/1/2/3) as the gain value at each rank.
    DCG = sum(gain_i / log2(i+1)) for i=1..k. NDCG = DCG / IDCG where IDCG
    is the DCG of the ideal (golden-sorted) ranking truncated to k.
    """
    rel_map = _golden_relevance_map(golden)
    test_top_k = test.top_k_ids(k)

    def dcg(grades: list[int]) -> float:
        return sum(g / math.log2(i + 2) for i, g in enumerate(grades[:k]))

    test_grades = [rel_map.get(tid, 0) for tid in test_top_k]
    ideal_grades = sorted(rel_map.values(), reverse=True)
    test_dcg = dcg(test_grades)
    ideal_dcg = dcg(ideal_grades)

    if ideal_dcg == 0:
        return {"value": None, "skip_reason": "no relevant in golden", "k": k}

    return {
        "value": test_dcg / ideal_dcg,
        "test_dcg": test_dcg,
        "ideal_dcg": ideal_dcg,
        "test_grades_top_k": test_grades,
        "k": k,
    }
