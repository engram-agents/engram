"""Shared ledger-row helpers used by both extract_spans.py and analyze.py.

fire_id in particular MUST be computed identically by both tools -- it is
the join key between extract_spans.py's key.jsonl / spans/*.md and
analyze.py's re-reading of the ledger, so a fire's identity can never depend
on which of the two tools computed it.
"""
from __future__ import annotations

import hashlib
import json


def compute_fire_id(row: dict) -> str:
    """Deterministic, content-derived fire id.

    Deliberately never hashes arm, shown_lesson_ids, or heads_by_lesson --
    those are exactly the fields the id must not let an adjudicator infer
    (DO NOT: "put arm information in any adjudicator-facing file"). The
    hashed basis (transcript_path, ts, tool_use_id-or-nonce) is stable
    across repeated runs over the same ledger row and unique per fire in
    practice (ts is the fire's own epoch-float timestamp)."""
    basis = row.get("tool_use_id") or row.get("nonce") or ""
    key = f"{row.get('transcript_path', '')}:{row.get('ts', '')}:{basis}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"fire-{digest}"


def load_jsonl(path) -> list:
    """Read a JSONL file into a list of dicts. Raises on a genuinely
    unreadable file (missing / permission) -- callers decide what that
    means for their own reporting (e.g. extract_spans.py's unlocatable
    accounting only applies to TRANSCRIPTS, not to the ledger/verdicts/key
    inputs themselves, which are assumed present per the CLI contract)."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def append_jsonl(path, row) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")


def rows_for_date_and_arms(rows: list, date_str: str, arms) -> list:
    """Filter ledger rows to a UTC date (matched against ts_iso's first 10
    characters, i.e. its YYYY-MM-DD prefix) and a set of arms."""
    out = []
    for row in rows:
        ts_iso = row.get("ts_iso") or ""
        if ts_iso[:10] != date_str:
            continue
        if row.get("arm") not in arms:
            continue
        out.append(row)
    return out


def flatten_heads(heads_by_lesson: dict) -> list:
    """All head ids across every shown lesson of one fire, deduped,
    order-preserving."""
    heads = []
    for _lesson_id, hs in (heads_by_lesson or {}).items():
        for h in hs or []:
            if h not in heads:
                heads.append(h)
    return heads


def expand_per_lesson(row: dict) -> list:
    """Pre-registration: "Per-lesson counting on multi-lesson fires." One
    unit per shown lesson id, each carrying just that lesson's own heads
    (from heads_by_lesson). A participating fire should always have at
    least one shown_lesson_ids entry; the empty-list fallback below is
    defensive only, so a malformed/synthetic row is never silently dropped
    from a denominator instead of surfacing as a zero-heads unit."""
    shown = row.get("shown_lesson_ids") or []
    heads_by_lesson = row.get("heads_by_lesson") or {}
    if not shown:
        return [{"lesson_id": None, "heads": []}]
    return [
        {"lesson_id": lesson_id, "heads": list(heads_by_lesson.get(lesson_id) or [])}
        for lesson_id in shown
    ]
