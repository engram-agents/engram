"""Load v4.2 curator-fairy golden-set JSON files into RankingResultUnit objects.

Source format: per curator_fairy_spec_v4_2.md output schema (§Output format).
Each ground-truth/q_NNNN.json is one golden RRU.
"""
from __future__ import annotations

import glob
import json
import os
from typing import Iterator

from .rru import RankingResultUnit, RankedNode


def _default_golden_dir() -> str:
    """Resolve the golden-set directory from the environment.

    No default points at any home directory — the golden set is a per-seat
    PRIVATE artifact (curated ground-truth JSONs naming real people) and must
    never be assumed to live at a fixed path. Fails loudly, naming the env var
    to set, rather than silently falling back to some guessed location.
    """
    value = os.environ.get("ENGRAM_RECALL_GOLDEN_DIR")
    if not value:
        raise RuntimeError(
            "ENGRAM_RECALL_GOLDEN_DIR is not set. The golden set is a "
            "per-seat private artifact with no repo-default location — set "
            "ENGRAM_RECALL_GOLDEN_DIR to the directory containing your "
            "q_*.json ground-truth files."
        )
    return value


def load_golden(path: str) -> RankingResultUnit:
    """Load a single v4.2 golden-set JSON file."""
    with open(path) as f:
        data = json.load(f)

    ranked: list[RankedNode] = []
    for i, candidate in enumerate(data.get("ranked_candidates", []), start=1):
        ranked.append(RankedNode(
            id=candidate["id"],
            rank=i,
            grading=candidate.get("grading"),
            notes=candidate.get("notes", "") or "",
        ))

    return RankingResultUnit(
        query_id=data["query_id"],
        query_text=data["query_text"],
        ranked_nodes=ranked,
        metadata={
            "primary_category": data.get("primary_category"),
            "alt_categories": data.get("alt_categories", []),
            "source": data.get("source", "fairy-curated"),
            "engram_snapshot": data.get("engram_snapshot", {}),
            "curator": data.get("curator"),
            "curator_methodology": data.get("curator_methodology"),
            "diversification_applied": data.get("diversification_applied", ""),
            "context": data.get("context"),
        },
    )


def load_golden_set(
    golden_dir: str | None = None,
    exclude_archived: bool = True,
) -> dict[str, RankingResultUnit]:
    """Load all canonical q_NNNN.json files into {query_id: RRU} dict.

    `golden_dir` defaults to `$ENGRAM_RECALL_GOLDEN_DIR` (raises loudly if
    unset — see `_default_golden_dir`).

    `exclude_archived=True` (default) skips files with version suffixes like
    `q_NNNN_v4.json` or `q_NNNN_v4_1.json` — only canonical names are loaded.
    """
    if golden_dir is None:
        golden_dir = _default_golden_dir()
    out: dict[str, RankingResultUnit] = {}
    for path in sorted(glob.glob(os.path.join(golden_dir, "q_*.json"))):
        name = os.path.basename(path)
        if exclude_archived and "_v" in name.replace(".json", ""):
            continue
        if name == "q_pilot_3cb2d01b.json":
            continue
        rru = load_golden(path)
        out[rru.query_id] = rru
    return out


def iter_golden(golden_dir: str | None = None) -> Iterator[RankingResultUnit]:
    """Generator variant for streaming.

    `golden_dir` defaults to `$ENGRAM_RECALL_GOLDEN_DIR` (raises loudly if
    unset — see `_default_golden_dir`).
    """
    if golden_dir is None:
        golden_dir = _default_golden_dir()
    for path in sorted(glob.glob(os.path.join(golden_dir, "q_*.json"))):
        name = os.path.basename(path)
        if "_v" in name.replace(".json", "") or name == "q_pilot_3cb2d01b.json":
            continue
        yield load_golden(path)


if __name__ == "__main__":
    # Smoke test: print summary of currently-loaded golden set
    s = load_golden_set()
    print(f"Loaded {len(s)} golden RRUs")
    for qid in sorted(s.keys())[:5]:
        rru = s[qid]
        n_grade3 = sum(1 for n in rru.ranked_nodes if n.grading and n.grading.get("relevance") == 3)
        n_grade2 = sum(1 for n in rru.ranked_nodes if n.grading and n.grading.get("relevance") == 2)
        n_grade1 = sum(1 for n in rru.ranked_nodes if n.grading and n.grading.get("relevance") == 1)
        print(f"  {qid:10}  cat={rru.metadata['primary_category']:18}  "
              f"n={len(rru.ranked_nodes):3}  g3={n_grade3}  g2={n_grade2}  g1={n_grade1}")
