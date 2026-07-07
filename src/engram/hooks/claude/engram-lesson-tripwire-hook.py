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
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ENGRAM_HOME = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
DB_PATH = Path(ENGRAM_HOME) / "knowledge.db"

_QUERY = """
SELECT
    COALESCE(
        json_extract(metadata, '$.scaffolding_nudge'),
        json_extract(metadata, '$.anchor_line')
    ) AS nudge,
    json_extract(metadata, '$.situation_pattern')  AS pattern
FROM nodes
WHERE type IN ('lesson', 'cornerstone')
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
"""


def load_tripwires(db_path: Path = DB_PATH):
    """Return list of (pattern_str, nudge_str) from active lessons with situation_pattern."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        rows = conn.execute(_QUERY).fetchall()
        conn.close()
        return [(row[1], row[0]) for row in rows if row[0] and row[1]]
    except Exception:
        return []


def check_command(match_target: str, tripwires) -> list:
    """Return list of scaffolding nudges whose situation_pattern matches match_target."""
    hits = []
    for pattern, nudge in tripwires:
        try:
            if re.search(pattern, match_target, re.IGNORECASE):
                hits.append(nudge)
        except re.error:
            pass  # malformed pattern — skip silently
    return hits


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
#   1. config auto_surface.in_turn_recall.enabled is true (default FALSE —
#      flips on only after replay-bench blesses the latency, per blueprint)
#   2. cooldown: ≥ cooldown_seconds since the last in-turn fire (cheap
#      state-file check, runs before any DB work)
#   3. the pending tool call's args contain ≥1 high-IDF term not seen in
#      any recent fire's term set (topic-shift detection)
# On gate-pass: query the recall daemon with the novel terms, render up to
# max_lines nodes not recently rendered by this channel.

STATE_PATH = Path(ENGRAM_HOME) / "in-turn-recall-state.json"
SOCKET_PATH = Path(ENGRAM_HOME) / "recall-daemon.sock"
_RECENT_TERM_SETS_KEPT = 10
_RECENT_IDS_KEPT = 30


def _in_turn_config() -> dict:
    """Read auto_surface.in_turn_recall from config.json; safe defaults."""
    defaults = {
        "enabled": False,
        "cooldown_seconds": 60,
        "max_lines": 3,
        "min_idf": 6.0,
    }
    try:
        with open(Path(ENGRAM_HOME) / "config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        section = (config.get("auto_surface", {}) or {}).get("in_turn_recall", {}) or {}
        return {
            "enabled": bool(section.get("enabled", defaults["enabled"])),
            "cooldown_seconds": int(section.get("cooldown_seconds", defaults["cooldown_seconds"])),
            "max_lines": int(section.get("max_lines", defaults["max_lines"])),
            "min_idf": float(section.get("min_idf", defaults["min_idf"])),
        }
    except Exception:
        return defaults


def _resolve_runtime_dir() -> str:
    """Locate engram_idf.py — plugin root (two levels up, flat layout) first,
    then ENGRAM_HOME snapshot, then the dev-source fallback. Same ladder as
    engram-surface-hook.py; never a fixed parents[N] against src/ layout."""
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(plugin_root, "engram_idf.py")):
        return plugin_root
    if os.path.exists(os.path.join(ENGRAM_HOME, "engram_idf.py")):
        return ENGRAM_HOME
    return os.path.expanduser("~/engram-alpha/src/engram")


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


def check_in_turn_recall(match_target: str) -> str:
    """Budgeted general recall at action-moment, behind the novelty gate."""
    import time

    cfg = _in_turn_config()
    if not cfg["enabled"]:
        return ""

    # Gate 2 — cooldown (state-file read only; no DB touched). Broad except:
    # any state-read failure (incl. PermissionError) must degrade to empty
    # state, never block the tripwire path.
    try:
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
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
        from engram_idf import extract_keywords
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1.0)
        try:
            pairs = extract_keywords(conn, match_target, min_idf=cfg["min_idf"], top_k=5)
        finally:
            conn.close()
        terms = [tok for tok, _score in pairs]
    except Exception:
        return ""
    if not terms:
        return ""
    seen_terms = {t for ts in state.get("recent_term_sets", []) if isinstance(ts, list) for t in ts}
    novel = [t for t in terms if t not in seen_terms]
    if not novel:
        return ""

    def _write_state() -> None:
        """Best-effort atomic state write (tmp + os.replace so a concurrent
        reader never sees a truncated file; a lost race only risks an early
        re-fire, which is acceptable for this T2 channel)."""
        try:
            tmp = str(STATE_PATH) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, str(STATE_PATH))
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

    # Gate passed — query the daemon with the novel terms.
    result = _query_recall_daemon(" ".join(novel))
    if not result or not result.get("match_count"):
        _record_fire()
        _write_state()
        return ""

    recent_ids = set(state.get("recent_ids", []))
    lines = []
    rendered_ids = []
    pool = (result.get("special_nodes") or []) + (result.get("top_claims") or [])
    for n in pool:
        if len(lines) >= cfg["max_lines"]:
            break
        nid = n.get("id", "?")
        if nid in recent_ids:
            continue
        kw = n.get("recall_keywords")
        kw_str = (" · ".join(f"`{k}`" for k in kw) + " — ") if isinstance(kw, list) and kw else ""
        body = n.get("recall_summary") or (n.get("claim", "") or "")[:120]
        lines.append(f"  - [{nid}] {kw_str}{body}")
        rendered_ids.append(nid)

    _record_fire()
    if not lines:
        _write_state()
        return ""

    ids = state.get("recent_ids", [])
    if not isinstance(ids, list):
        ids = []
    ids.extend(rendered_ids)
    state["recent_ids"] = ids[-_RECENT_IDS_KEPT:]
    _write_state()

    return (
        f"[ENGRAM in-turn recall — novel terms: {', '.join(novel)}]\n"
        + "\n".join(lines)
        + "\n  (ambient action-moment recall; engram_inspect(id) for detail)"
    )


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)

    try:
        match_target = build_match_target(hook_input)
        if not match_target or not match_target.strip():
            sys.exit(0)

        parts = []

        tripwires = load_tripwires()
        hits = check_command(match_target, tripwires) if tripwires else []
        if hits:
            nudge_lines = "\n".join(f"  • {h}" for h in hits)
            parts.append(
                "[lesson-tripwire] Action-moment pattern matched — remember:\n"
                f"{nudge_lines}"
            )

        # In-turn ambient recall (#1690) — independently guarded; a recall
        # failure must never suppress a tripwire hit (and vice versa).
        try:
            recall = check_in_turn_recall(match_target)
        except Exception:
            recall = ""
        if recall:
            parts.append(recall)

        if not parts:
            sys.exit(0)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": "\n".join(parts),
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)  # hook failures must never block a tool call


if __name__ == "__main__":
    main()
