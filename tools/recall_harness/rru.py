"""RankingResultUnit — symmetric data structure for test and golden rankings.

Per HARNESS-DESIGN.md "Core abstractions" section. Metric functions consume
(test_rru, golden_rru) pairs of identical shape; per-side asymmetry lives
in optional fields (grading on golden side, signals on test side) plus the
metadata dict.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class RankedNode:
    id: str
    rank: int
    grading: Optional[dict] = None
    signals: Optional[dict] = None
    notes: str = ""


@dataclass
class RankingResultUnit:
    query_id: str
    query_text: str
    ranked_nodes: list[RankedNode]
    metadata: dict = field(default_factory=dict)

    @property
    def relevant_ids(self) -> set[str]:
        """Golden-side helper: ids with grading.relevance >= 2."""
        return {n.id for n in self.ranked_nodes
                if n.grading and n.grading.get("relevance", 0) >= 2}

    @property
    def highly_relevant_ids(self) -> set[str]:
        """Golden-side helper: ids with grading.relevance == 3."""
        return {n.id for n in self.ranked_nodes
                if n.grading and n.grading.get("relevance") == 3}

    def top_k_ids(self, k: int) -> list[str]:
        """First k ranked ids in this RRU's order."""
        return [n.id for n in self.ranked_nodes[:k]]

    def to_dict(self) -> dict:
        return asdict(self)
