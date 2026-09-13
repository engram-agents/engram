#!/usr/bin/env python3
"""
PreToolUse hook (Bash + MCP): surfaces lesson tripwires at action-moment.

PROBLEM SOLVED
==============
The prompt-time recall hook (engram-surface-hook.py) fires at UserPromptSubmit
and matches lessons against the user's prompt text. But action-moment lessons —
ones whose true retrieval cue is a specific CLI command or MCP tool call about
to be executed — systematically fail to surface because the encoding cue is
absent from the prompt. This is encoding specificity (Tulving & Thomson 1973):
retrieval requires reinstatement of the encoding context.

THREE-WAY TAXONOMY OF LESSON RETRIEVAL CUES (forum thread #135):
  Locus 1 — Bash-command-cued: cue is a shell command shape.
             Caught by this hook (matches against the Bash command string).
  Locus 2 — MCP-tool-cued: cue is an MCP tool invocation.
             Caught by this hook (matches against tool_name + serialized args).
  Locus 3 — Semantic-content-cued: cue is the content of an assertion, not any
             tool call. Not catchable at PreToolUse; stays with prompt-time hook.

HOW THIS HOOK WORKS
===================
Fires on every Bash or MCP-tool PreToolUse event. Queries active lessons AND
cornerstones that have a `situation_pattern` field in their metadata JSON
(#1691 — cornerstones nudge via `scaffolding_nudge` or `anchor_line`). For Bash calls, matches
the pending command against each pattern. For MCP calls, matches against
"{tool_name} {json(args)}" — so a pattern like `engram_add_observation` fires
before that specific MCP tool call. For any match, injects the lesson's
`scaffolding_nudge` as additionalContext before the tool call runs.
Complements (does not replace) the prompt-time recall hook.

TIER: T2 (Convenience) — degrades gracefully on DB unavailability; never blocks.
Issues: #1203 (Bash/locus-1) + #1297 (MCP/locus-2) — lesson-tripwire encoding-specificity gap.
#2858 — the harness previews only the first ~1964-2016 bytes / ~1964-2000
characters of a hook's additionalContext inline (composite rule measured
across two seats: min(2000 chars, last complete line under ~2048 bytes)); a
heavily-loaded seat with many matching patterns silently lost most nudges
past that cut, with no signal anything was dropped — and since the harness
truncates the TAIL of the whole additionalContext string, a trailer placed
at the END of this hook's own block was cut right along with the nudges on
exactly the large captures this issue is about. Fixed by ordering hits by
specificity, rendering the trailer FIRST (so it survives the tail-cut even
when everything after it doesn't), capping the fully-rendered block
(trailer + header + bullets) to a character ceiling below the harness's own
cut, persisting the overflow to a file, and always emitting a
"N shown · M cut" trailer on the tripwire block.

#2863 WS2 — this hook's own bespoke render mechanism (formerly
select_and_cap_hits / _render_tripwire_block / _build_trailer / its own
_placeholder_overflow_path) was the REFERENCE IMPLEMENTATION
`_hooklib.emit_context()` was generalized FROM (see that function's own
docstring). This hook has now been folded onto the shared helper: the
actual rendering (trailer + header + bullets, capped to
TRIPWIRE_BLOCK_CHAR_CEILING as `target`, never exceeding the harness's hard
`budget`) is emit_context()'s job now, called from main() below.
write_tripwire_overflow() is unchanged and still owns the on-disk overflow
format (node_id-keyed, one block per cut hit) — it is invoked from a small
overflow_writer adapter that reconstructs the cut hit dicts BY POSITION
from emit_context()'s own selection (see main()'s own comments at the
call site for why position, not string value).
"""
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import atomic_write_json, resolve_runtime_dir, emit_context
from _harness import detect_harness, get_harness_budgets

ENGRAM_HOME = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
DB_PATH = Path(ENGRAM_HOME) / "knowledge.db"

# ---------------------------------------------------------------------------
# #2858 -- tripwire-block preview cap (harness-truncation defense)
# ---------------------------------------------------------------------------
# Claude Code's harness only shows a preview of a PreToolUse hook's
# additionalContext INLINE; everything past that is spilled to a file the
# agent must deliberately open, and usually doesn't. When many tripwire
# patterns match one command (a heavily-loaded seat, or a broad pattern),
# this hook used to emit ALL hits in DB-query order with no cap -- so most
# nudges were silently truncated out of context, with no signal anything
# was cut at all.
#
# Round 2 (this fix): two colleagues (Borges, Ariadne) independently
# measured the ACTUAL harness truncation boundary on real captures from live
# sessions: it lands around 1964-2016 bytes / 1964-2000 characters (two
# measurement rounds, reconciled to a composite rule: min(2000 characters,
# last complete line under ~2048 bytes)). 1800 characters on the FULLY
# RENDERED block (trailer + header + bullets, exactly as emitted -- not just
# the bullet lines in isolation) sits safely under that boundary with
# measured margin: worst-case byte expansion ratio ~1.008 for this file's
# actual nudge text gives ~1814 bytes at 1800 chars -- ~200 bytes of
# headroom under the 2016-byte observed ceiling. That headroom is a reserved
# margin for JSON-envelope overhead and whatever other hook blocks may
# precede this one on the same tool call -- not a value to treat as exact;
# if the envelope/other-blocks overhead is measured precisely later, retune
# this constant rather than treating it as derived science.
#
# #2863 WS2: this is now passed as `target` to `_hooklib.emit_context()`
# (see main()), which owns the actual capping/trailer-rendering mechanism
# this hook's own bespoke select_and_cap_hits/_render_tripwire_block/
# _build_trailer used to implement directly. The ceiling VALUE and its
# margin reasoning above are unchanged.
TRIPWIRE_BLOCK_CHAR_CEILING = 1800

# #2863 WS2: the fixed header line emit_context()'s `head` argument renders
# right after the trailer -- extracted as a module constant (was previously
# inlined inside _render_tripwire_block) so main() and this module's own
# tests share exactly one copy of the literal text.
TRIPWIRE_HEADER = "[lesson-tripwire] Action-moment pattern matched — remember:"

_QUERY = """
SELECT
    id,
    COALESCE(
        json_extract(metadata, '$.scaffolding_nudge'),
        json_extract(metadata, '$.anchor_line'),
        json_extract(metadata, '$.surfacing_nudge'),
        claim
    ) AS nudge,
    json_extract(metadata, '$.situation_pattern')  AS pattern
FROM nodes
WHERE type IN ('lesson', 'cornerstone', 'axiom', 'goal')
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
"""


def load_tripwires(db_path: Path = DB_PATH):
    """Return list of (pattern_str, nudge_str, node_id) from active lessons
    with situation_pattern.

    #2753 epic WS2: node_id was added (3rd tuple element, minimal extension
    of the pre-existing (pattern, nudge) shape) so check_command's hits can
    carry the firing node's id for generalization_chain() lookups, without
    touching what the existing nudge block renders."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        rows = conn.execute(_QUERY).fetchall()
        conn.close()
        return [(row[2], row[1], row[0]) for row in rows if row[1] and row[2]]
    except Exception:
        return []


def check_command(match_target: str, tripwires) -> list:
    """Return list of {"nudge": str, "node_id": str, "pattern": str} dicts
    for every situation_pattern that matches match_target.

    #2753 epic WS2: hits carry node_id (in addition to the pre-existing
    nudge text) so the caller can compute generalization_chain() per hit for
    the additive "[triggered principles]" section, without changing what
    the existing nudge block renders (main() reads hit["nudge"] where it
    used to read the hit string directly).

    #2858: hits ALSO carry the firing pattern itself now (3rd-then-4th
    field, minimal extension of the same shape) -- purely so main() can
    order hits by specificity (len(pattern), descending) before rendering
    and capping, per the preview-truncation fix above. This does NOT change
    the matching logic or the circumstances under which a hit fires -- still
    exactly one hit per matching pattern."""
    hits = []
    for pattern, nudge, node_id in tripwires:
        try:
            if re.search(pattern, match_target, re.IGNORECASE):
                hits.append({"nudge": nudge, "node_id": node_id, "pattern": pattern})
        except re.error:
            pass  # malformed pattern — skip silently
    return hits


def _tripwire_bullet(hit: dict) -> str:
    """Render one hit's bullet line -- the exact per-item body string
    emit_context() renders, one per shown hit (was previously inlined
    inside the now-removed _render_tripwire_block). Extracted as its own
    function so main() and this module's tests share one copy of the
    format."""
    return f"  • {hit['nudge']}"


# #2863 WS2: select_and_cap_hits / _render_tripwire_block / _build_trailer /
# this module's own _placeholder_overflow_path (the bespoke mechanism
# `_hooklib.emit_context()` was generalized FROM -- see that function's
# docstring) are REMOVED. main() below calls emit_context() directly for
# both the selection (shown vs. cut) AND the rendering (trailer + header +
# bullets) -- one call, one source of truth, replacing four separate
# hand-written functions that had to be kept in lockstep by hand. The
# empirical equivalence (byte-identical selection + rendering on every
# fixture this file's own test suite exercises, including the
# always-show-at-least-one-hit edge case) was verified before this removal
# landed -- see the PR description for the comparison.


def write_tripwire_overflow(cut_hits: list, now: float | None = None) -> str:
    """Persist the full node_id + nudge text of every hit emit_context()
    cut (or, for the rare item-alone-exceeds-the-hard-budget case, marked
    truncated-in-band -- see main()'s own overflow_writer adapter), so the
    trailer/marker in the visible tripwire block can point somewhere real
    instead of leaving the reader with no way to recover what was dropped.

    #2858 Piece 2: written to ENGRAM_HOME/tripwire-overflow/<ts>-<pid>.txt (a
    NEW directory, created on demand). File name is deterministic enough for a
    test to reconstruct the exact path given the same `now` + pid. `now`
    defaults to time.time() but accepts an injected value so tests can assert
    on an exact resulting path.

    Never raises -- like every other helper in this file, a broken overflow-
    file write must never suppress the nudges that DID make it into the
    visible block, and must never crash the hook (this hook's whole contract,
    per the module docstring, is "never blocks"). Returns "" on any failure,
    including an empty cut_hits list (nothing to write).
    """
    if not cut_hits:
        return ""
    try:
        if now is None:
            import time as _time
            now = _time.time()
        overflow_dir = Path(ENGRAM_HOME) / "tripwire-overflow"
        overflow_dir.mkdir(parents=True, exist_ok=True)
        overflow_path = overflow_dir / f"{int(now)}-{os.getpid()}.txt"
        blocks = [
            f"[{hit.get('node_id', '?')}] {hit.get('nudge', '')}"
            for hit in cut_hits
        ]
        overflow_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        return str(overflow_path)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# #2753 epic WS2 — generalization-chain render at the tripwire (D3 point 1)
# ---------------------------------------------------------------------------
# ADDITIVE to the existing nudge block above: for each hit this fire, walk
# generalization_chain() on its node id and render one merged, deduped-by-
# head "[triggered principles]" section. Dedupe is scoped to THIS ONE fire
# (Case 1 in the issue's D3) -- no new cooldown/suppression across fires
# (Case 2 is explicitly unchanged, per the issue). Never raises: the call
# site in main() wraps this in its own try/except, matching
# check_in_turn_recall's posture -- a failure here must never suppress the
# existing nudge block, and vice versa.

_GENERALIZATION_CHAIN_FN = None
_GENERALIZATION_CHAIN_IMPORT_ATTEMPTED = False


def _import_generalization_chain():
    """Lazily import generalization_chain from engram_core (#2753 WS1) --
    lazy, like engram_idf's import in check_in_turn_recall below, so this
    hook never crashes at import time if engram_core isn't yet resolvable
    (e.g. a partial/mid-upgrade install). Memoized (including a failed
    lookup) so a missing module doesn't re-pay the resolve+import cost on
    every hit within one process; each hook invocation is a fresh process
    anyway, so the memo only helps within a single fire's multiple hits."""
    global _GENERALIZATION_CHAIN_FN, _GENERALIZATION_CHAIN_IMPORT_ATTEMPTED
    if _GENERALIZATION_CHAIN_IMPORT_ATTEMPTED:
        return _GENERALIZATION_CHAIN_FN
    _GENERALIZATION_CHAIN_IMPORT_ATTEMPTED = True
    try:
        runtime_dir = resolve_runtime_dir(ENGRAM_HOME, "engram_core.py")
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        from engram_core import generalization_chain
        _GENERALIZATION_CHAIN_FN = generalization_chain
    except Exception:
        _GENERALIZATION_CHAIN_FN = None
    return _GENERALIZATION_CHAIN_FN


def _truncate_for_label(text: str, limit: int = 60) -> str:
    """Short, single-line excerpt for the merged-head summary line -- never
    raises on a non-string/None input (defensive: claim/recall_summary are
    both nullable per generalization_chain's own docstring)."""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_triggered_principles(hits: list, db_path: "Path | None" = None) -> str:
    """#2753 epic WS2, D3 point 1: for this ONE tripwire fire, walk each
    hit's generalization_chain() and render ONE additive "[triggered
    principles]" section, deduped by HEAD across all of this fire's hits
    (D3's Case 1 -- within one fire; Case 2, the existing per-node
    recent-id/repeat-count cooldown machinery, is untouched and not this
    function's concern).

    Per head: every non-head node reached (from any hit sharing that head,
    merged into one subgraph, ordered by depth) renders as a one-liner
    (`recall_summary` only -- never `claim`, which is absent on non-head
    entries by generalization_chain's own contract); the head itself
    renders its `claim` IN FULL. When more than one of this fire's
    chain-bearing hits share a head, one line names the convergence
    ("N of these M matches sit under <head_id> (<short label>)") before the
    merged rungs.

    A hit whose own node has NO generalizes ancestor at all (already a head
    with nothing above it -- chain contains only the node itself) renders
    NOTHING for that hit; the section is emitted at all only when at least
    one hit produced a real chain OR at least one hit's chain reported
    `cycle_detected` (see below).

    #2753 WS2 round 2 -- `cycle_detected` (WS1's cycle-guard fix, 5e14f83b):
    a hit whose chain comes back with `cycle_detected: True` has NO valid
    head to render (structurally impossible in a valid DAG -- the resolved
    graph actually contains a cycle). Rendering the normal head/rung
    structure is impossible (there is no head), and silently skipping it
    like the no-ancestor case above would be the exact bug WS1 fixed one
    layer down: an omitted section reads as "no chain exists", which is
    false -- a chain WAS found, it's just corrupt. So a cycle-detected hit
    renders its own one-line malformed-data notice instead, visibly
    distinct from both a normal rendered chain and from omission.

    #2753 WS2 round 3 -- `truncated` (present since WS1's original commit,
    read by this function for the first time): a hit whose chain reports
    `heads: []` and `cycle_detected: False` and `truncated: True` has real
    partial ancestry -- `max_depth` was hit while nodes were still pending
    expansion, so a head genuinely exists above what was walked, it just
    wasn't reached. Falling into the same silent skip as the no-ancestor
    case would misreport "no chain exists" exactly as the cycle_detected
    case would -- a milder instance of the same problem, since the data
    here is incomplete rather than corrupt. So a truncated-with-no-head hit
    renders its own one-line "chain truncated" notice (no rungs shown --
    the walk never reached far enough to have any to show) -- worded and
    marked (`…`, not `⚠`) to read as incomplete-but-honest data rather than
    malformed data, and never both: `cycle_detected` and
    `truncated` are mutually exclusive by generalization_chain's own
    contract (`cycle_detected` requires `truncated: False`), so the two
    checks below never fire on the same hit.

    Returns "" on: no hits, no importable generalization_chain, any DB/walk
    failure, or no hit producing a real chain and no hit reporting
    cycle_detected or a truncated-with-no-head walk. Never raises -- this
    hook's T2 "never blocks" contract (see module docstring) applies to
    this additive section exactly as it does to the pre-existing nudge
    block.

    db_path defaults to None (resolved to the module-level DB_PATH INSIDE
    the function body, not as a mutable default-argument value) so a test
    that monkeypatches DB_PATH after this module is imported is honored on
    the no-arg call path main() uses -- a `db_path: Path = DB_PATH` default
    would instead freeze at the value DB_PATH held at function-definition
    time (import time), same footgun load_tripwires's own default already
    carries (harmless there since every caller passes db_path explicitly).
    """
    if not hits:
        return ""
    if db_path is None:
        db_path = DB_PATH
    generalization_chain = _import_generalization_chain()
    if generalization_chain is None:
        return ""

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        # head_id -> {"nodes": {resolved_id: entry, ...}, "hit_ids": [node_id, ...]}
        heads_seen: dict = {}
        head_order: list = []
        relevant_hit_count = 0
        # #2753 WS2 round 2 -- node ids whose own chain() call reported
        # cycle_detected: True (WS1 5e14f83b). Kept separate from
        # heads_seen: a cycle-detected chain has NO head to merge into, by
        # definition (cycle_detected implies heads == []), so it is never a
        # candidate for the head-dedupe machinery above -- it renders its
        # own standalone malformed-data line instead. Dedup by node_id in
        # first-seen order (a hit list should not repeat a node_id in
        # practice, but this stays cheap insurance).
        cycle_detected_ids: list = []
        # #2753 WS2 round 3 -- node ids whose own chain() call reported
        # heads == [] with truncated: True (real ancestry exists, the walk
        # was just cut off by max_depth before reaching a head). Kept
        # separate from heads_seen for the same reason cycle_detected_ids
        # is: there is no head to merge into. Kept separate from
        # cycle_detected_ids too, even though both are "heads == [] and
        # nothing to merge into" -- the two states are mutually exclusive
        # (see docstring) and read differently to a human, so each gets its
        # own notice line and its own dedup list.
        truncated_ids: list = []
        for hit in hits:
            node_id = hit.get("node_id") if isinstance(hit, dict) else None
            if not node_id:
                continue
            chain = generalization_chain(conn, node_id)
            nodes = chain.get("nodes") or {}
            start_resolved = chain.get("start_resolved_id")
            heads = chain.get("heads") or []
            if chain.get("cycle_detected"):
                if node_id not in cycle_detected_ids:
                    cycle_detected_ids.append(node_id)
                continue
            if not heads and chain.get("truncated"):
                if node_id not in truncated_ids:
                    truncated_ids.append(node_id)
                continue
            # No generalizes ancestor at all -- already a head with nothing
            # above it. Nothing to render for this hit (D3: "render nothing
            # extra for it; only render the section when there is an
            # actual chain to show").
            if not heads or (len(nodes) <= 1 and start_resolved in nodes):
                continue
            relevant_hit_count += 1
            for head_id in heads:
                if head_id not in heads_seen:
                    heads_seen[head_id] = {"nodes": {}, "hit_ids": []}
                    head_order.append(head_id)
                heads_seen[head_id]["hit_ids"].append(node_id)
                heads_seen[head_id]["nodes"].update(nodes)
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not heads_seen and not cycle_detected_ids and not truncated_ids:
        return ""

    lines = ["[triggered principles]"]
    for node_id in cycle_detected_ids:
        lines.append(
            f"  ⚠ {node_id} — principle-chain data is malformed (a cycle "
            "was detected in the resolved generalizes graph); chain not shown"
        )
    for node_id in truncated_ids:
        lines.append(
            f"  … {node_id} — principle chain truncated (max depth reached "
            "before a head was found); further ancestry likely exists but "
            "is not shown"
        )
    for head_id in head_order:
        entry = heads_seen[head_id]
        merged_nodes = entry["nodes"]
        contributing = entry["hit_ids"]
        head_entry = merged_nodes.get(head_id, {})
        if len(contributing) > 1:
            label = _truncate_for_label(head_entry.get("claim") or head_entry.get("recall_summary"))
            lines.append(
                f"  {len(contributing)} of these {relevant_hit_count} matches sit under "
                f"{head_id} ({label})" if label else
                f"  {len(contributing)} of these {relevant_hit_count} matches sit under {head_id}"
            )
        non_head_ids = sorted(
            (nid for nid in merged_nodes if nid != head_id),
            key=lambda nid: merged_nodes[nid].get("depth", 0),
        )
        for nid in non_head_ids:
            summary = merged_nodes[nid].get("recall_summary") or ""
            lines.append(f"  ↑ {nid} — {summary}")
        head_claim = head_entry.get("claim") or ""
        lines.append(f"  ↑ {head_id} — {head_claim}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# alpha#1944 step 2 — shadow-block logging
# ---------------------------------------------------------------------------
# A SEPARATE query + check path from load_tripwires/check_command above,
# deliberately not folded into them: those two feed the advisory-nudge
# injection every lesson/cornerstone/axiom/goal has always had, and this
# repo's tests construct bare (pattern, nudge) 2-tuples as fixtures for that
# path. Widening that tuple shape to carry prevention_type would silently
# break every such fixture. prevention_type only exists on lesson nodes
# (engram_epistemic.PREVENTION_TYPES), so this path queries just lessons,
# runs independently, and — like check_in_turn_recall — must never suppress
# or be suppressed by the advisory-nudge path.

PREVENTION_LEDGER_PATH = Path(ENGRAM_HOME) / "lesson-prevention-ledger.jsonl"

_PREVENTION_QUERY = """
SELECT
    id,
    json_extract(metadata, '$.situation_pattern')  AS pattern,
    json_extract(metadata, '$.prevention_type')    AS prevention_type,
    COALESCE(json_extract(metadata, '$.scaffolding_nudge'), claim) AS nudge
FROM nodes
WHERE type = 'lesson'
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
  AND json_extract(metadata, '$.prevention_type') IN ('shadow', 'blocking')
"""


def load_prevention_lessons(db_path: Path = DB_PATH):
    """Return list of {id, pattern, prevention_type, nudge} for lessons
    whose prevention_type is 'shadow' or 'blocking' (advisory/absent
    lessons are excluded — they have nothing to log or deny here)."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        rows = conn.execute(_PREVENTION_QUERY).fetchall()
        conn.close()
        return [
            {"id": row[0], "pattern": row[1], "prevention_type": row[2], "nudge": row[3] or ""}
            for row in rows if row[1] and row[2]
        ]
    except Exception:
        return []


# alpha#1944 step 3 — override token detection. Kepler's verified design
# (Lei-authorized throwaway test, PreToolUse deny confirmed reachable on
# this plugin): the override is STATELESS and single-call-scoped — nothing
# persists, nothing to re-arm. The retry IS the override. Bash embeds it as
# a harmless leading env-var assignment (ENGRAM_OVERRIDE="ls_NNNN: <reason>"
# <same command> — the shell sets the var and runs the command unchanged;
# the hook only reads the literal command STRING, never executes it). MCP
# tools carry it as an "override_lesson" field in payload_json, which lands
# inside build_match_target's compact-JSON args string the same way any
# other field would. One regex covers both shapes since both end up
# somewhere inside match_target.
#
# The hook itself can only ALLOW or DENY — it cannot strip override_lesson
# before the call reaches the tool. That gap is closed centrally instead:
# server.py's _parse_payload() pops override_lesson before any tool's own
# unknown-field check runs, so every MCP tool is covered without touching
# each one's individual allowlist (see _parse_payload's own docstring).
_OVERRIDE_TOKEN_RE = r'(?:ENGRAM_OVERRIDE\s*=\s*"|"override_lesson"\s*:\s*")%s\s*:\s*([^"]*)"'


def _find_override(match_target: str, lesson_id: str) -> str | None:
    """Return the override reason for lesson_id if a matching
    ENGRAM_OVERRIDE / override_lesson token is present in match_target,
    else None. Never raises."""
    try:
        m = re.search(_OVERRIDE_TOKEN_RE % re.escape(lesson_id), match_target)
    except re.error:
        return None
    return m.group(1).strip() if m else None


def check_shadow_block(match_target: str, prevention_lessons, hook_input: dict | None = None) -> list:
    """Return a list of would-have-blocked fire records — one per matching
    shadow/blocking lesson, including whether an override token for that
    lesson was present (#1944 step 3 — the override token IS the label
    step 4's auto-demotion counter reads). Never raises: a malformed
    pattern is skipped silently, matching check_command's posture."""
    hits = []
    for lesson in prevention_lessons:
        pattern = lesson["pattern"]
        try:
            match = re.search(pattern, match_target, re.IGNORECASE)
        except re.error:
            continue  # malformed pattern — skip silently, same as check_command
        if not match:
            continue
        override_reason = _find_override(match_target, lesson["id"])
        hits.append({
            "lesson_id": lesson["id"],
            "prevention_type": lesson["prevention_type"],
            "matched_span": match.group(0),
            "tool_name": (hook_input or {}).get("tool_name"),
            "session_id": (hook_input or {}).get("session_id"),
            "override_present": override_reason is not None,
            "override_reason": override_reason,
        })
    return hits


# alpha#1944 step 4 — auto-demotion, Kepler's read-time-lease design
# (forum #387, 2026-09-04): the hook NEVER writes the graph directly — that
# is the no-raw-DB-edits rule, and the audit trail (field_traces,
# edit_history, turn stamp) belongs to engram_update_lesson, the module
# that owns it. "Mechanically demoted" stays true anyway because the hook
# computes the EFFECTIVE prevention_type at read time from the ledger, not
# only from the lesson's stored metadata: once a blocking lesson has N
# overrides inside the window, THIS call (and every call after it, until
# the durable write lands) is treated as advisory. A lease, not a latch —
# it decays to safe immediately from the signal already on disk; the
# durable write is a follow-up (a person, or a dream-cycle reconcile pass
# reading the "threshold crossed" ledger entries this emits) tracked
# separately, not built in this hook.
_DEMOTION_DEFAULTS = {"overrides": 2, "window_days": 7}


def _demotion_config() -> dict:
    """Read lessons.blocking_demotion from config.json; safe defaults.

    Default (overrides=2, window_days=7) is Kepler's pick made 2026-09-04,
    not a considered policy from Lei — retune via config.json:
      {"lessons": {"blocking_demotion": {"overrides": 2, "window_days": 7}}}
    Reasoning stated on the issue: a lesson only reaches "blocking" after a
    zero-false-positive shadow window, so one override is plausibly an
    accepted-risk case; two legitimate overrides in a week is evidence the
    pattern matches legitimate calls and should stop blocking.
    """
    try:
        with open(Path(ENGRAM_HOME) / "config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        section = (config.get("lessons", {}) or {}).get("blocking_demotion", {}) or {}
        return {
            "overrides": int(section.get("overrides", _DEMOTION_DEFAULTS["overrides"])),
            "window_days": float(section.get("window_days", _DEMOTION_DEFAULTS["window_days"])),
        }
    except Exception:
        return dict(_DEMOTION_DEFAULTS)


def _count_recent_overrides(lesson_id: str, window_days: float, now: float) -> int:
    """Count override_present=True ledger entries for lesson_id within the
    last window_days. Best-effort: any read failure counts as 0, never
    raises. Only called on an actual blocking-lesson match (rare by
    design — the whole point of blocking is deterrence), so scanning the
    full ledger stays cheap in practice."""
    if not PREVENTION_LEDGER_PATH.exists():
        return 0
    cutoff = now - (window_days * 86400)
    count = 0
    try:
        with open(PREVENTION_LEDGER_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if (
                    entry.get("lesson_id") == lesson_id
                    and entry.get("override_present")
                    and entry.get("ts", 0) >= cutoff
                ):
                    count += 1
    except Exception:
        return count
    return count


def check_blocking_deny(match_target: str, prevention_lessons, now: float | None = None) -> dict | None:
    """Return a decision for the FIRST 'blocking' lesson matched without a
    valid override this call, or None if nothing to report.

    Only prevention_type == 'blocking' is ever consulted — 'shadow' never
    denies or demotes (that is the entire distinction; shadow only logs,
    via check_shadow_block above). A lesson-id-matching override token
    lets THIS call through outright (still recorded as a hit by
    check_shadow_block); it is checked before the demotion count so an
    overridden call is never ALSO reported as a demotion.

    Otherwise checks the read-time lease: if this lesson already has
    demotion_config()["overrides"] overrides inside the window, the
    returned dict carries demoted=True and override_count/threshold —
    main() must NOT deny on that, only log + warn. A genuine denial (no
    override this call, threshold not yet crossed) carries demoted=False.
    """
    if now is None:
        import time as _time
        now = _time.time()
    cfg = _demotion_config()
    for lesson in prevention_lessons:
        if lesson["prevention_type"] != "blocking":
            continue
        pattern = lesson["pattern"]
        try:
            match = re.search(pattern, match_target, re.IGNORECASE)
        except re.error:
            continue
        if not match:
            continue
        if _find_override(match_target, lesson["id"]) is not None:
            continue  # overridden this call — do not deny or demote-report
        override_count = _count_recent_overrides(lesson["id"], cfg["window_days"], now)
        base = {
            "lesson_id": lesson["id"],
            "matched_span": match.group(0),
            "nudge": lesson.get("nudge", ""),
        }
        if override_count >= cfg["overrides"]:
            base.update({
                "demoted": True,
                "override_count": override_count,
                "threshold": cfg["overrides"],
                "window_days": cfg["window_days"],
            })
        else:
            base["demoted"] = False
        return base
    return None


def build_deny_reason(deny: dict) -> str:
    """Build the permissionDecisionReason text for a blocking denial —
    lesson id, matched span, the nudge, and the exact override recipe
    (Kepler's verified design: JSON permissionDecision form, its reason
    rendering cleanly as the tool error rather than a hook-crash-shaped
    exit-2 message)."""
    lesson_id = deny["lesson_id"]
    return (
        f"[lesson-tripwire BLOCKING] {lesson_id} matched {deny['matched_span']!r} "
        f"— this call is blocked, not merely advised.\n"
        f"{deny['nudge']}\n\n"
        f"If you've reviewed this and the call is correct, retry with the exact "
        f"same call plus a single-use override naming this lesson and your reason:\n"
        f"  Bash:  ENGRAM_OVERRIDE=\"{lesson_id}: <your reason>\" <same command>\n"
        f"  MCP:   add \"override_lesson\": \"{lesson_id}: <your reason>\" to payload_json\n"
        f"The override is single-call-scoped — nothing persists, nothing to re-arm."
    )


def _log_threshold_crossed(now: float, deny: dict) -> None:
    """alpha#1944 step 4 — the read-time-lease signal: a loud stderr line
    (hook stdout is reserved for the additionalContext/deny JSON contract;
    stderr is safe to use freely) plus a distinct ledger entry, emitted
    the FIRST time a call would have denied but the override threshold was
    already crossed. Best-effort, never raises."""
    lesson_id = deny["lesson_id"]
    try:
        print(
            f"[lesson-tripwire] AUTO-DEMOTED (lease) {lesson_id}: "
            f"{deny['override_count']} overrides in the last {deny['window_days']} "
            f"day(s) (threshold {deny['threshold']}) — treating as advisory on this "
            f"call; durable prevention_type write still pending (engram_update_lesson).",
            file=sys.stderr,
        )
    except Exception:
        pass
    try:
        with open(PREVENTION_LEDGER_PATH, "a") as f:
            f.write(json.dumps({
                "ts": now,
                "lesson_id": lesson_id,
                "mode": "auto_demoted_lease",
                "override_count": deny["override_count"],
                "threshold": deny["threshold"],
                "window_days": deny["window_days"],
            }) + "\n")
    except Exception:
        pass


def _append_prevention_ledger(now: float, hits: list) -> None:
    """Best-effort append of one JSONL entry per would-have-blocked fire
    (alpha#1944 step 2, extended step 3 with override fields — the log a
    future promotion/demotion gate reads). Mirrors _append_ledger's
    never-raises contract: a ledger failure must never suppress or delay
    the tripwire path it rides alongside."""
    if not hits:
        return
    try:
        with open(PREVENTION_LEDGER_PATH, "a") as f:
            for hit in hits:
                f.write(json.dumps({
                    "ts": now,
                    "lesson_id": hit["lesson_id"],
                    "mode": hit["prevention_type"],
                    "tool_name": hit["tool_name"],
                    "session_id": hit["session_id"],
                    "matched_span": hit["matched_span"],
                    "override_present": hit.get("override_present", False),
                    "override_reason": hit.get("override_reason"),
                    # step 4 field (independent-predicate verdict) lands
                    # when the auto-demotion counter is wired.
                }) + "\n")
    except Exception:
        pass


def build_match_target(hook_input: dict) -> str:
    """Build the string to match situation_pattern against, or "" to skip.

    Bash calls   → the shell command string.
    MCP calls    → "{tool_name} {compact_json_args}" so patterns like
                   `engram_add_observation` match the tool name substring.
    Other calls  → "" (caller exits early).
    """
    tool_name = hook_input.get("tool_name", "")
    ti = hook_input.get("tool_input") or hook_input.get("input") or {}

    if tool_name == "Bash":
        return ti.get("command", "") if isinstance(ti.get("command"), str) else ""

    if tool_name.startswith("mcp__"):
        try:
            args_str = json.dumps(ti, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            args_str = ""
        return f"{tool_name} {args_str}"

    return ""


# ---------------------------------------------------------------------------
# In-turn ambient recall (#1690, P2 of the recall-triggering blueprint)
# ---------------------------------------------------------------------------
# Piggybacks on this hook's existing PreToolUse spawn (one process, two
# checks) instead of registering a new hook — hook-spawn overhead is the
# dominant latency cost (~300ms/spawn), so the general-recall check rides
# the spawn the tripwire already pays for.
#
# Novelty gate (all must pass, else silence):
#   1. config auto_surface.in_turn_recall.enabled is true (default TRUE +
#      zero cooldown since v0.3.0 — #1746, validated by the zero-cooldown
#      ledger experiment; enabled=false is the per-install kill-switch)
#   2. cooldown: ≥ cooldown_seconds since the last in-turn fire (cheap
#      state-file check, runs before any DB work)
#   3. the pending tool call's args contain ≥1 high-IDF term not seen in
#      any recent fire's term set (topic-shift detection)
# On gate-pass: query the recall daemon with the novel terms, render up to
# max_lines nodes not recently rendered by this channel.

STATE_PATH = Path(ENGRAM_HOME) / "in-turn-recall-state.json"
SOCKET_PATH = Path(ENGRAM_HOME) / "recall-daemon.sock"
LEDGER_PATH = Path(ENGRAM_HOME) / "in-turn-recall-ledger.jsonl"
_RECENT_TERM_SETS_KEPT = 10
_RECENT_IDS_KEPT = 30


def _append_ledger(build_entry) -> None:
    """Best-effort append of one JSONL telemetry entry per gate-passing
    in-turn-recall fire (#1749). Takes a zero-arg callable so that entry
    CONSTRUCTION runs inside the guard too — an exception while building
    the dict (e.g. a malformed hook_input) must be swallowed exactly like
    a write failure, never escape into the recall path (review round 2).
    Never-raises: a ledger failure must never suppress or delay the
    tripwire/recall path. Call sites are inside the existing flock-guarded
    region, so appends are already serialized here -- no new locking is
    added."""
    try:
        entry = build_entry()
        with open(LEDGER_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# rec-3 (#266): junk-token stoplist for the novel-terms gate. Shell/code
# execution tokens carry zero recall signal but pollute the high-IDF
# novel-term set (a seat-level ledger measured ~30% of novel tokens as
# junk-class). The canonical list now lives as `JUNK_STOPLIST` in engram_idf
# (#1784: single source of truth shared with the rec-4 measurement harness so
# the filter and the measurement can't drift). It is imported lazily at
# point-of-use — NOT at module top-level — because this hook must never crash
# at import when engram_idf isn't yet on sys.path (the whole recall path is
# guarded and returns "" if the import fails). `_in_turn_config()` therefore
# carries a None sentinel for "use the canonical default", kept distinct from
# a config-supplied `[]` which DISABLES filtering; the sentinel is resolved to
# JUNK_STOPLIST only after the import succeeds.


def _apply_junk_stoplist(terms: list, stoplist: frozenset) -> list:
    """Drop junk-class tokens (case-insensitive) from an ordered term list,
    preserving order. rec-3 (#266) -- keeps shell/code execution noise out of
    the novel-term set that drives in-turn recall + the ledger."""
    if not stoplist:
        return terms
    return [t for t in terms if t.lower() not in stoplist]


def _in_turn_config() -> dict:
    """Read auto_surface.in_turn_recall from config.json; safe defaults.

    min_idf is graph-size-relative by default (#1734): IDF's ceiling is
    ln(N_current_nodes), so a fixed absolute threshold dead-zones small
    graphs (6.0 was unreachable under ~400 nodes). min_idf_ratio=0.7 gives
    ~4.16 at 380 nodes and ~6.03 at 5500 nodes — matching both
    empirically-working absolute values. An explicit min_idf still wins
    verbatim (absolute override; existing configs keep exact behavior).
    """
    defaults = {
        # Defaults flipped for v0.3.0 (#1746, Lei's 2026-07-10 ruling): the
        # release-candidate default is ON with ZERO channel cooldown — the
        # n=252 zero-cooldown ledger showed junk fires render nothing and
        # cost ~0.7s daemon time/hr, while the render-side layers (recent_ids
        # + term-set memory) handle repetition. cooldown_seconds is retained
        # as a config knob (and kill-switch enabled=false stays documented).
        "enabled": True,
        "cooldown_seconds": 0,
        "max_lines": 3,
        "min_idf": None,
        "min_idf_ratio": 0.7,
        # None sentinel = "not overridden, use the canonical engram_idf.JUNK_STOPLIST
        # (resolved at point-of-use after the lazy import)". A config-supplied []
        # is a real empty frozenset that DISABLES filtering — kept distinct below.
        "junk_stoplist": None,
    }
    try:
        with open(Path(ENGRAM_HOME) / "config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        section = (config.get("auto_surface", {}) or {}).get("in_turn_recall", {}) or {}
        raw_min_idf = section.get("min_idf")
        return {
            "enabled": bool(section.get("enabled", defaults["enabled"])),
            "cooldown_seconds": int(section.get("cooldown_seconds", defaults["cooldown_seconds"])),
            "max_lines": int(section.get("max_lines", defaults["max_lines"])),
            "min_idf": float(raw_min_idf) if raw_min_idf is not None else None,
            "min_idf_ratio": float(section.get("min_idf_ratio", defaults["min_idf_ratio"])),
            "junk_stoplist": (
                frozenset(str(t).lower() for t in section["junk_stoplist"])
                if isinstance(section.get("junk_stoplist"), list)
                else defaults["junk_stoplist"]
            ),
        }
    except Exception:
        return defaults


def _resolve_runtime_dir() -> str:
    """Locate engram_idf.py — plugin root (two levels up, flat layout) first,
    then ENGRAM_HOME snapshot, then a walk-parents search for the nearest
    ancestor whose own src/engram/ actually contains engram_idf.py, then the
    dev-source fallback (last-ditch, unchanged). Same ladder as
    engram-surface-hook.py; never a fixed parents[N] against src/ layout."""
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(plugin_root, "engram_idf.py")):
        return plugin_root
    if os.path.exists(os.path.join(ENGRAM_HOME, "engram_idf.py")):
        return ENGRAM_HOME
    # Walk parents from __file__ and take the first ancestor that actually
    # contains src/engram/engram_idf.py, instead of guessing a fixed
    # absolute path (#1712: the fixed guess resolves to the WRONG checkout
    # when this process is running from a git worktree nested under the
    # guessed path, e.g. .claude/worktrees/<id>/ under ~/engram-alpha --
    # silently shadowing the actually-running copy with a sibling one).
    # Reviewer-fairy blocker (PR #1727, found in the sibling surface-hook
    # copy, applied here too for consistency): Path.exists() raises
    # PermissionError on an unreadable ancestor instead of returning False
    # like os.path.exists() does at the rungs above -- wrap so this function
    # keeps its never-raises contract regardless of caller-side guarding.
    try:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "src" / "engram"
            if (candidate / "engram_idf.py").exists():
                return str(candidate)
    except OSError:
        pass
    return os.path.expanduser("~/engram-alpha/src/engram")  # last-ditch, unchanged


def _query_recall_daemon(query: str, top_k: int = 5) -> dict | None:
    """Minimal daemon socket query (same protocol as engram-surface-hook)."""
    import socket as _socket
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(str(SOCKET_PATH))
        sock.sendall((json.dumps({"query": query, "top_k": top_k}) + "\n").encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()
        response = json.loads(data.decode("utf-8").strip())
        if response.get("status") == "ok":
            return response.get("result", {})
        return None
    except Exception:
        return None


def check_in_turn_recall(match_target: str, hook_input: dict | None = None) -> str:
    """Budgeted general recall at action-moment, behind the novelty gate.

    hook_input (#1749): the raw PreToolUse payload, passed through from
    main() so the fire ledger can stamp session_id/transcript_path/tool_name
    per entry. Optional -- existing callers/tests that don't pass it keep
    working, with those fields recorded as null."""
    import time

    cfg = _in_turn_config()
    if not cfg["enabled"]:
        return ""

    # Atomic cooldown claim (#1709) — non-blocking exclusive flock over the
    # WHOLE check->record sequence below (cooldown read, novelty gate, daemon
    # query, _record_fire/_write_state). Under concurrent PreToolUse
    # invocations, exactly one caller wins the lock and proceeds; every
    # contender returns "" immediately (suppress, never block — a lost race
    # only risks a miss, which this T2 default-off channel already tolerates
    # per _write_state's docstring). Locks a DEDICATED lockfile, never
    # STATE_PATH itself (STATE_PATH is rewritten via os.replace, which would
    # break the lock's file-descriptor association).
    lock_fd = None
    lock_acquired = False
    lock_degraded = False
    lock_degrade_reason = ""
    try:
        import fcntl
        lock_fd = os.open(str(STATE_PATH) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    except Exception as exc:
        # fcntl unavailable (non-POSIX) or the lockfile couldn't be opened
        # (e.g. permissions) — degrade to the unlocked path exactly as
        # before this fix. The race is a perf issue, not a correctness one;
        # disabling the feature over an environment limitation would be
        # worse than accepting the (already-tolerated) race.
        #
        # #1737: this degrade was completely silent -- nothing distinguished
        # "locked and suppressed correctly" from "running unlocked," which
        # cost a full diagnosis round during PR #1733's review (a shared-/tmp
        # lockfile collision made this fallback engage, and the only tell was
        # stampede-shaped latency numbers downstream). Two cheap, never-
        # block/never-raise tells: one stderr line now (hook stderr goes to
        # hook logs, not the model -- safe on every call), and a state-file
        # flag below wherever state ends up written this call, so any future
        # bench/diagnostic can see it without re-deriving the cause.
        lock_fd = None
        lock_degraded = True
        lock_degrade_reason = f"{type(exc).__name__}: {exc}"
        try:
            print(
                f"[engram-lesson-tripwire-hook] in_turn_recall lock degraded to "
                f"unlocked path: {lock_degrade_reason}",
                file=sys.stderr,
            )
        except Exception:
            pass
    else:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except (BlockingIOError, OSError):
            # Another caller currently holds the claim on this fire-window —
            # suppress immediately rather than block.
            try:
                os.close(lock_fd)
            except Exception:
                pass
            return ""

    try:
        # Gate 2 — cooldown (state-file read only; no DB touched). Broad
        # except: any state-read failure (incl. PermissionError) must
        # degrade to empty state, never block the tripwire path.
        try:
            with open(STATE_PATH, "r") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        # #1737: carry the degrade tell into the state file, EAGERLY
        # persisted the moment the flag transitions -- independent of
        # whatever the cooldown/novelty gates below decide to do. Reviewer-
        # fairy catch on PR #1742's first review: piggybacking purely on
        # record_fire/_write_state (which several early-return gates below
        # skip entirely) meant the flag could go stale in BOTH directions
        # under sustained cooldown-heavy traffic -- under-reporting an
        # ongoing degrade (never written because every call keeps hitting
        # the cooldown gate), or over-reporting a resolved one (a stale
        # True surviving because no call has reached a write since
        # recovery). Eagerly writing just the transition, here, before any
        # gate can short-circuit, closes both gaps while still only paying
        # the extra write on an actual True<->False flip (steady state,
        # once clear, costs nothing).
        prev_degraded = bool(state.get("lock_degraded"))
        if lock_degraded:
            state["lock_degraded"] = True
            state["lock_degrade_reason"] = lock_degrade_reason
            flag_changed = not prev_degraded
        else:
            flag_changed = prev_degraded
            state.pop("lock_degraded", None)
            state.pop("lock_degrade_reason", None)
        if flag_changed:
            try:
                atomic_write_json(str(STATE_PATH), state)
            except OSError:
                pass
        now = time.time()
        last_fire = state.get("last_fire_ts", 0)
        if isinstance(last_fire, (int, float)) and (now - last_fire) < cfg["cooldown_seconds"]:
            return ""

        # Gate 3 — high-IDF novel terms in the pending tool call.
        # extract_keywords returns (token, idf_score) PAIRS — unwrap to tokens
        # (#1696 reviewer-fairy blocker: the flat-string treatment TypeError'd
        # against the real module and was masked by a wrong-contract test stub).
        runtime_dir = _resolve_runtime_dir()
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        try:
            from engram_idf import extract_keywords, STOPWORDS, JUNK_STOPLIST
            # Resolve the None sentinel from _in_turn_config() to the canonical
            # shared list now that the import has succeeded (#1784). A
            # config-supplied frozenset — including an empty one (filtering
            # disabled) — passes through unchanged.
            junk = cfg["junk_stoplist"] if cfg["junk_stoplist"] is not None else JUNK_STOPLIST
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1.0)
            try:
                if cfg["min_idf"] is not None:
                    effective_min_idf = cfg["min_idf"]
                else:
                    n_docs = conn.execute(
                        "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
                    ).fetchone()[0]
                    if n_docs == 0:
                        return ""
                    effective_min_idf = cfg["min_idf_ratio"] * math.log(n_docs)
                # rec-3 (#266) round 2 (colleague-review suggestion): exclude
                # junk tokens BEFORE the top_k cut, so >=5 high-IDF junk tokens
                # can't crowd a real lower-ranked term out of `pairs` entirely.
                # MERGE with the built-in STOPWORDS -- never replace, which
                # would drop common-English stopword filtering. The post-filter
                # below stays as a backstop.
                pairs = extract_keywords(
                    conn, match_target, min_idf=effective_min_idf, top_k=5,
                    stopwords=STOPWORDS | junk)
            finally:
                conn.close()
            terms = [tok for tok, _score in pairs]
            # rec-3 (#266): drop junk-class shell/code tokens BEFORE the
            # novelty check + fire, so execution noise neither triggers recall
            # nor pollutes the term-memory/ledger. Targeted, not a min_idf bump.
            terms = _apply_junk_stoplist(terms, junk)
        except Exception:
            return ""
        if not terms:
            return ""
        seen_terms = {t for ts in state.get("recent_term_sets", []) if isinstance(ts, list) for t in ts}
        novel = [t for t in terms if t not in seen_terms]
        if not novel:
            return ""

        def _write_state() -> None:
            """Best-effort atomic state write via _hooklib.atomic_write_json()
            (tmp + os.replace so a concurrent reader never sees a truncated
            file; a lost race only risks an early re-fire, which is
            acceptable for this T2 channel)."""
            try:
                atomic_write_json(str(STATE_PATH), state)
            except OSError:
                pass

        def _record_fire() -> None:
            """Stamp cooldown + term-set memory. Runs on EVERY gate-pass — also
            when nothing new rendered — so a near-miss (novel terms, daemon hits
            all already-rendered) cannot re-trigger the daemon on every call
            (#1696 reviewer-fairy: the render-only stamping bypassed gate 2)."""
            state["last_fire_ts"] = now
            term_sets = state.get("recent_term_sets", [])
            if not isinstance(term_sets, list):
                term_sets = []
            term_sets.append(list(terms))
            state["recent_term_sets"] = term_sets[-_RECENT_TERM_SETS_KEPT:]

        def _ledger_entry(outcome: str, match_count: int, rendered_ids_: list, skipped_recent_: int) -> dict:
            return {
                "ts": now,
                "session_id": hook_input.get("session_id") if hook_input else None,
                "transcript_path": hook_input.get("transcript_path") if hook_input else None,
                "tool_name": hook_input.get("tool_name") if hook_input else None,
                "novel_terms": novel,
                "terms": terms,
                "effective_min_idf": effective_min_idf,
                "match_count": match_count,
                "rendered_ids": rendered_ids_,
                "skipped_recent": skipped_recent_,
                "cooldown_seconds": cfg["cooldown_seconds"],
                "outcome": outcome,
            }

        # Gate passed — query the daemon with the novel terms.
        result = _query_recall_daemon(" ".join(novel))
        if not result or not result.get("match_count"):
            _record_fire()
            _write_state()
            _append_ledger(lambda: _ledger_entry("no_matches", 0, [], 0))
            return ""

        recent_ids = set(state.get("recent_ids", []))
        lines = []
        rendered_ids = []
        skipped_recent = 0
        pool = (result.get("special_nodes") or []) + (result.get("top_claims") or [])
        for n in pool:
            if len(lines) >= cfg["max_lines"]:
                break
            nid = n.get("id", "?")
            if nid in recent_ids:
                skipped_recent += 1
                continue
            kw = n.get("recall_keywords")
            kw_str = (" · ".join(f"`{k}`" for k in kw) + " — ") if isinstance(kw, list) and kw else ""
            body = n.get("recall_summary") or (n.get("claim", "") or "")[:120]
            lines.append(f"  - [{nid}] {kw_str}{body}")
            rendered_ids.append(nid)

        _record_fire()
        if not lines:
            _write_state()
            _append_ledger(lambda: _ledger_entry("all_suppressed", result.get("match_count", 0), [], skipped_recent))
            return ""

        ids = state.get("recent_ids", [])
        if not isinstance(ids, list):
            ids = []
        ids.extend(rendered_ids)
        state["recent_ids"] = ids[-_RECENT_IDS_KEPT:]
        _write_state()
        _append_ledger(lambda: _ledger_entry("rendered", result.get("match_count", 0), rendered_ids, skipped_recent))

        return (
            f"[ENGRAM in-turn recall — novel terms: {', '.join(novel)}]\n"
            + "\n".join(lines)
            + "\n  (ambient action-moment recall; engram_inspect(id) for detail)"
        )
    finally:
        if lock_fd is not None and lock_acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)

    try:
        match_target = build_match_target(hook_input)
        if not match_target or not match_target.strip():
            sys.exit(0)

        # alpha#1944 step 3 — the deny gate, checked FIRST and computed
        # inside its own fail-open try/except. This is the one place this
        # hook can refuse a tool call at all; every other check in this
        # function only ever ADDS advisory context. A failure loading
        # prevention lessons, or any other exception while computing the
        # deny decision, MUST resolve to "do not deny" — infra failure is
        # never a reason to block a call (Kepler's design point 5: the only
        # reachable failure mode is loud-advisory, never silent no-op and
        # never deadlock). prevention_lessons is computed once and reused
        # by the shadow-block logging below.
        prevention_lessons = []
        deny = None
        try:
            prevention_lessons = load_prevention_lessons()
            if prevention_lessons:
                deny = check_blocking_deny(match_target, prevention_lessons)
        except Exception:
            deny = None

        if deny is not None and deny.get("demoted"):
            # Read-time lease (step 4): do NOT deny — this lesson's
            # override count already crossed the auto-demotion threshold,
            # so the effective state is advisory for this call even though
            # the durable prevention_type write hasn't landed. Log loudly
            # and fall through to the normal advisory path below.
            try:
                import time as _time
                _log_threshold_crossed(_time.time(), deny)
            except Exception:
                pass
            deny = None

        if deny is not None:
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": build_deny_reason(deny),
                }
            }
            print(json.dumps(response))
            sys.exit(0)

        parts = []

        tripwires = load_tripwires()
        hits = check_command(match_target, tripwires) if tripwires else []
        # #2858 Piece 1 — order by specificity (pattern length, descending)
        # before rendering/capping. Ties preserve check_command's original
        # (DB-query) order — sorted() is stable, so this is just the sort
        # key, no explicit tiebreak needed. This does NOT change which hits
        # fire, only the order they're considered for the character-ceiling
        # selection below (a longer, more specific pattern is judged more
        # likely to be the useful nudge when not everything can fit).
        #
        # Reviewer-fairy round 1 (PR #2860): this resort also changes the
        # order `shown_hits` carries into render_triggered_principles below
        # (Piece 3) -- including in the ZERO-CUT case, where shown_hits ==
        # hits by MEMBERSHIP but not by the pre-sort DB-query ORDER. That
        # section only uses order to decide which "generalizes" head prints
        # first when several hits share one, so this is a presentation-only
        # effect (which head-group appears first), never a correctness one
        # (which heads appear, or what they say) -- not worth threading a
        # second, unsorted hits list through main() just to preserve an
        # ordering nothing downstream depends on.
        shown_hits = []
        if hits:
            hits = sorted(hits, key=lambda h: -len(h.get("pattern") or ""))
            # #2863 WS2 — render via the shared _hooklib.emit_context()
            # helper instead of this hook's own former select_and_cap_hits/
            # _render_tripwire_block/_build_trailer (removed above).
            # `bullets` is the SAME ordered (most-specific-first) list of
            # per-hit strings the old _render_tripwire_block used to build
            # inline; emit_context() now owns the capping (which prefix
            # fits under `target`) AND the trailer/header rendering in one
            # call -- verified (see PR description) to select and render
            # byte-identically to the old mechanism on this file's own test
            # fixtures, including the always-show-at-least-one-hit case
            # (allow_single_overflow=True reproduces that guarantee: the
            # single most-important hit is still shown in full as long as
            # it fits the harness's hard `budget`, only truncated in-band
            # in the pathological case where even one hit alone exceeds
            # that hard ceiling -- something the old mechanism had no
            # protection against at all).
            bullets = [_tripwire_bullet(h) for h in hits]

            # emit_context() calls this ONLY with the item strings it
            # itself decided to cut/truncate -- always either a trailing
            # SUFFIX of `bullets` (the ordinary cut case), or, in the rare
            # case where even the single most-important hit's own block
            # exceeds the harness's hard budget, the ENTIRE list (that
            # hit's own FULL untruncated text is included alongside the
            # genuinely-cut rest, so nothing is unrecoverable -- see
            # emit_context()'s own docstring, "Stage 3"). Reconstructed
            # back to hit dicts BY POSITION (never by nudge-text VALUE,
            # which could collide if two different hits happen to share
            # identical nudge text) so write_tripwire_overflow()'s existing
            # node_id-keyed on-disk format is preserved byte-for-byte.
            cut_hits_holder: dict = {"cut": []}

            def _tripwire_overflow_writer(items: list) -> str:
                cut_n = len(items)
                cut = hits[-cut_n:] if 0 < cut_n <= len(hits) else []
                cut_hits_holder["cut"] = cut
                return write_tripwire_overflow(cut) if cut else ""

            budgets = get_harness_budgets(detect_harness())
            parts.append(emit_context(
                TRIPWIRE_HEADER,
                bullets,
                budget=budgets["budget"],
                target=TRIPWIRE_BLOCK_CHAR_CEILING,
                allow_single_overflow=True,
                overflow_writer=_tripwire_overflow_writer,
            ))

            # shown_hits feeds render_triggered_principles below (Piece 3)
            # — never the full, uncapped `hits`, so a "[triggered
            # principles]" line never cites a lesson whose own nudge text
            # was cut from the visible block. Derived from cut_hits_holder
            # (set as a side effect of the emit_context() call above, via
            # the overflow_writer): everything NOT cut is shown, except the
            # pathological case above where the writer receives ALL hits
            # (item 0's full text for recovery + the genuinely-cut rest) —
            # there, item 0 is still "shown" (marker-truncated in-band, not
            # dropped), so it is never left out of the principles section.
            cut_hits = cut_hits_holder["cut"]
            shown_hits = hits[: len(hits) - len(cut_hits)] if cut_hits else hits
            if not shown_hits and hits:
                shown_hits = hits[:1]

        # #2753 epic WS2 (D3 point 1) — generalization-chain render, ADDITIVE
        # after the nudge block above and independently guarded: a failure
        # computing the principles section must NEVER suppress the existing
        # nudge (already appended above), and vice versa (the existing nudge
        # append above already ran and is untouched by anything below).
        #
        # #2858 Piece 3 — only SHOWN hits feed this, never the full/uncapped
        # `hits` list: a principle-chain citation for a hit whose own nudge
        # text was cut from the visible block above would reference
        # something the reader can't see the reason for.
        if hits:
            try:
                principles_block = render_triggered_principles(shown_hits)
            except Exception:
                principles_block = ""
            if principles_block:
                parts.append(principles_block)

        # alpha#1944 step 2 — shadow-block logging. Independently guarded,
        # same posture as in-turn recall below: a logging failure must never
        # suppress a tripwire hit, and a tripwire failure must never suppress
        # this. Nothing here changes what the agent sees — shadow lessons
        # (and any blocking lesson that WAS overridden, since deny already
        # returned above otherwise) still only ever inject the advisory
        # nudge above; this purely appends to the promotion-gate ledger.
        try:
            import time as _time
            shadow_hits = (
                check_shadow_block(match_target, prevention_lessons, hook_input)
                if prevention_lessons else []
            )
            _append_prevention_ledger(_time.time(), shadow_hits)
        except Exception:
            pass

        # In-turn ambient recall (#1690) — independently guarded; a recall
        # failure must never suppress a tripwire hit (and vice versa).
        try:
            recall = check_in_turn_recall(match_target, hook_input)
        except Exception:
            recall = ""
        if recall:
            parts.append(recall)

        if not parts:
            sys.exit(0)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n".join(parts),
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)  # hook failures must never block a tool call


if __name__ == "__main__":
    main()
