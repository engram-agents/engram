"""recall_summary_validator — pure-function validation for recall summary entries.

No MCP, no DB access. Used by:
  - engram_set_recall_summaries (batch tool in server.py) for per-item validation
  - downstream fairies for pre-flight validation before calling the batch tool

Constants mirror the guardrails from the singular engram_set_recall_summary tool
(PR #223, 2026-05-19). Single source of truth — server.py imports from here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECALL_SUMMARY_HARD_CAP = 200
RECALL_KEYWORDS_MIN = 3
RECALL_KEYWORDS_MAX = 5
RECALL_KEYWORD_MAX_LEN = 30


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_summary_entry(entry: dict) -> "dict | None":
    """Validate a single recall-summary entry dict.

    Args:
        entry: dict expected to have keys:
            node_id (str): target node identifier
            recall_summary (str): curated summary (hard cap: 200 chars)
            recall_keywords (list[str]): 3–5 strings, each ≤ 30 chars, no dupes

    Returns:
        None if the entry is valid.
        A structured error dict if the entry is invalid:
            {"error": str, "field": str, ...additional context fields}
    """
    if not isinstance(entry, dict):
        return {"error": "entry must be a JSON object", "field": "entry"}

    # -- Required fields present --
    for field in ("node_id", "recall_summary", "recall_keywords"):
        if field not in entry:
            return {"error": f"missing field {field}", "field": field}

    node_id = entry["node_id"]
    recall_summary = entry["recall_summary"]
    recall_keywords = entry["recall_keywords"]

    # -- node_id --
    if not isinstance(node_id, str) or not node_id.strip():
        return {"error": "node_id must be a non-empty string", "field": "node_id"}

    # -- recall_summary --
    if not isinstance(recall_summary, str) or not recall_summary.strip():
        return {"error": "recall_summary must be a non-empty string", "field": "recall_summary"}

    if len(recall_summary) > RECALL_SUMMARY_HARD_CAP:
        return {
            "error": (
                f"recall_summary exceeds the {RECALL_SUMMARY_HARD_CAP} char "
                f"hard cap (got {len(recall_summary)} chars). The authoring "
                "target is ≤120 chars; the hard cap is a defensive guard at "
                f"{RECALL_SUMMARY_HARD_CAP}. Caller may truncate, regenerate, "
                "or accept-as-truncated."
            ),
            "field": "recall_summary",
            "hard_cap": RECALL_SUMMARY_HARD_CAP,
            "got_length": len(recall_summary),
        }

    # -- recall_keywords --
    if not isinstance(recall_keywords, list):
        return {"error": "recall_keywords must be a JSON array", "field": "recall_keywords"}

    if len(recall_keywords) < RECALL_KEYWORDS_MIN or len(recall_keywords) > RECALL_KEYWORDS_MAX:
        return {
            "error": (
                f"recall_keywords must have {RECALL_KEYWORDS_MIN} to "
                f"{RECALL_KEYWORDS_MAX} elements (got {len(recall_keywords)})"
            ),
            "field": "recall_keywords",
            "got_count": len(recall_keywords),
            "min": RECALL_KEYWORDS_MIN,
            "max": RECALL_KEYWORDS_MAX,
        }

    for i, kw in enumerate(recall_keywords):
        if not isinstance(kw, str):
            return {
                "error": (
                    f"recall_keywords[{i}] must be a string "
                    f"(got {type(kw).__name__}: {kw!r})"
                ),
                "field": "recall_keywords",
            }
        if not kw.strip():
            return {
                "error": f"recall_keywords[{i}] must not be empty or whitespace-only",
                "field": "recall_keywords",
            }
        if len(kw) > RECALL_KEYWORD_MAX_LEN:
            return {
                "error": (
                    f"recall_keywords[{i}] exceeds {RECALL_KEYWORD_MAX_LEN} "
                    f"char limit (got {len(kw)} chars: {kw!r})"
                ),
                "field": "recall_keywords",
            }

    # -- No duplicate keywords (case-sensitive) --
    seen: set[str] = set()
    for i, kw in enumerate(recall_keywords):
        if kw in seen:
            return {
                "error": f"recall_keywords contains duplicate: {kw!r} (index {i})",
                "field": "recall_keywords",
            }
        seen.add(kw)

    return None
