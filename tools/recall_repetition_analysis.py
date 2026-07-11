#!/usr/bin/env python3
"""Quantify recall-repetition (complaint #3, Lei's recall-triggering review directive).

Mines $ENGRAM_HOME/logs/index.db's (default ~/.engram/logs/index.db)
engram.surface.fire events and measures, per session, what fraction of
surfaced node IDs at each prompt are repeats of IDs already surfaced within
a trailing window of k prior prompts (k=1,3,5,10).

Also reports the distribution of "times the same node ID surfaced within one
session" (max, p95) -- the worst-offender view -- and a rough wasted-context-
chars estimate.

This is the acceptance-metric measurement tool for the recall-triggering
blueprint's §4 acceptance criteria (docs/recall-triggering-blueprint.md) --
run it before and after landing #1689's render-layer suppression to confirm
the repeat fraction at k=5 actually drops.

Usage:
    python3 tools/recall_repetition_analysis.py
    python3 tools/recall_repetition_analysis.py --db-path /path/to/index.db

Relocated from a scratch analysis script into tools/ (issue #1689) so it's
reproducible across installs -- the original hardcoded a single agent's
$ENGRAM_HOME; this version resolves the DB path portably (CLI arg >
$ENGRAM_HOME env var > ~/.engram default), matching the resolution pattern
already used elsewhere in tools/ (e.g. the ENGRAM_HOME env-var convention in
tools/dump_mcp_schema.py's invocation contract).
"""
import argparse
import json
import os
import sqlite3
import statistics
from collections import defaultdict, Counter

WINDOWS = [1, 3, 5, 10]

# Rough average rendered-line-length estimate (chars), per engram-surface-hook.py
# render_one_node_line(): "Specials"/"Top claims" entries (max 6/prompt) render
# full lines (id + conf/type-tag + age + keywords + summary), roughly 90-170
# chars observed in practice; "Others" entries (the rest of matched_ids, up to
# ~10 total per prompt) render id+keywords only, roughly 40-70 chars. This is
# explicitly a ROUGH estimate (Kepler's ask), not a byte-exact accounting --
# blended average across both tiers.
AVG_FULL_LINE_CHARS = 130   # specials + top_claims tier (up to 6 of the ~10 ids)
AVG_OTHERS_LINE_CHARS = 55  # remaining "Others" tier ids
FULL_TIER_SLOTS = 6         # 3 specials + 3 top_claims, per format_nudge()


def _default_db_path() -> str:
    """Resolve the index.db path: $ENGRAM_HOME/logs/index.db, falling back to
    ~/.engram/logs/index.db when ENGRAM_HOME is unset (matches the hook's own
    _resolve_engram_home() precedent)."""
    engram_home = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    return os.path.join(engram_home, "logs", "index.db")


def load_events(db_path: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT sessionId, ts, data FROM events "
        "WHERE event_type='engram.surface.fire' ORDER BY sessionId, ts"
    )
    rows = cur.fetchall()
    conn.close()
    by_session = defaultdict(list)
    for sid, ts, data in rows:
        d = json.loads(data)
        ids = d.get("matched_ids") or []
        by_session[sid].append((ts, ids))
    return by_session


def repeat_fraction_at_k(by_session, k):
    """Pooled fraction: across ALL surfaced ids in ALL sessions, what fraction
    are repeats of an id seen in the trailing k prompts (same session)?"""
    total_ids = 0
    total_repeats = 0
    per_session_fracs = []
    for sid, prompts in by_session.items():
        sess_ids = 0
        sess_repeats = 0
        for i, (ts, ids) in enumerate(prompts):
            if not ids:
                continue
            window_ids = set()
            for j in range(max(0, i - k), i):
                window_ids.update(prompts[j][1])
            for nid in ids:
                total_ids += 1
                sess_ids += 1
                if nid in window_ids:
                    total_repeats += 1
                    sess_repeats += 1
        if sess_ids > 0:
            per_session_fracs.append(sess_repeats / sess_ids)
    pooled = (total_repeats / total_ids) if total_ids else 0.0
    median_per_session = statistics.median(per_session_fracs) if per_session_fracs else 0.0
    return pooled, median_per_session, total_ids, total_repeats


def same_node_distribution(by_session):
    """Per session: count of (times a single node id surfaced) -- return the
    overall max and p95 across all (session, node_id) pairs, plus the top
    worst offenders."""
    counts = []
    worst = []
    for sid, prompts in by_session.items():
        c = Counter()
        for ts, ids in prompts:
            for nid in ids:
                c[nid] += 1
        for nid, n in c.items():
            counts.append(n)
            worst.append((sid, nid, n))
    counts.sort()
    if not counts:
        return 0, 0, []
    p95_idx = int(len(counts) * 0.95)
    p95_idx = min(p95_idx, len(counts) - 1)
    worst.sort(key=lambda x: -x[2])
    return counts[-1], counts[p95_idx], worst[:15]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to index.db (default: $ENGRAM_HOME/logs/index.db, or "
             "~/.engram/logs/index.db if ENGRAM_HOME is unset).",
    )
    args = parser.parse_args()
    db_path = args.db_path or _default_db_path()

    by_session = load_events(db_path)
    n_sessions = len(by_session)
    n_events = sum(len(v) for v in by_session.values())
    n_ids_total = sum(len(ids) for prompts in by_session.values() for _, ids in prompts)

    print(f"# Recall-repetition analysis (complaint #3, recall-triggering review)\n")
    print(f"Source: {db_path} (engram.surface.fire events)")
    print(f"Sessions: {n_sessions} | surface-fire prompts: {n_events} | total surfaced IDs (incl. repeats): {n_ids_total}\n")

    print("## Repeat fraction by trailing window (k = prior prompts checked)\n")
    print("| k | pooled repeat % (all ids) | median per-session repeat % | ids checked |")
    print("|---|---|---|---|")
    for k in WINDOWS:
        pooled, med, total_ids, total_repeats = repeat_fraction_at_k(by_session, k)
        print(f"| {k} | {pooled*100:.1f}% | {med*100:.1f}% | {total_ids} |")

    print()
    max_n, p95_n, worst = same_node_distribution(by_session)
    print(f"## Same-node-surfaced-in-one-session distribution\n")
    print(f"Max: a single node surfaced **{max_n}×** within one session.")
    print(f"P95: {p95_n}× (95% of (session, node) pairs surface at most this many times).\n")
    print("Worst offenders (session prefix, node id, times surfaced):\n")
    print("| session | node | times surfaced |")
    print("|---|---|---|")
    for sid, nid, n in worst:
        print(f"| {sid[:8]} | {nid} | {n} |")

    print()
    print("## Rough wasted-context estimate\n")
    pooled_k5, _, total_ids_k5, total_repeats_k5 = repeat_fraction_at_k(by_session, 5)
    # blended avg line length: FULL_TIER_SLOTS get the full-line estimate, rest get Others-line estimate
    # approximate per-id average across a typical ~10-id prompt
    blended_avg = (
        FULL_TIER_SLOTS * AVG_FULL_LINE_CHARS + max(0, 10 - FULL_TIER_SLOTS) * AVG_OTHERS_LINE_CHARS
    ) / 10
    wasted_chars_total = total_repeats_k5 * blended_avg
    wasted_chars_per_session = wasted_chars_total / n_sessions if n_sessions else 0
    print(f"Blended avg rendered line length (rough): ~{blended_avg:.0f} chars/id "
          f"({FULL_TIER_SLOTS} full-tier @ {AVG_FULL_LINE_CHARS}c + rest @ {AVG_OTHERS_LINE_CHARS}c, per format_nudge()).")
    print(f"At k=5: {total_repeats_k5} repeat-id instances out of {total_ids_k5} total surfaced ids "
          f"({pooled_k5*100:.1f}%).")
    print(f"Rough wasted chars: **{wasted_chars_total:,.0f} total** "
          f"(~{wasted_chars_per_session:,.0f}/session across {n_sessions} sessions).")


if __name__ == "__main__":
    main()
