#!/usr/bin/env python3
"""
UserPromptSubmit hook: shallow ENGRAM recall nudge.

Connects to the persistent engram-recall-daemon via Unix socket for fast
semantic + keyword search. Falls back to FTS-only if daemon is not
running. No memory refresh occurs (engram_surface is side-effect-free).

Exit codes:
  0 — success, JSON with additionalContext on stdout
  1 — non-blocking error (logged, prompt proceeds without nudge)
"""

import json
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_engram_home() -> str:
    """Per-install data dir: knowledge.db, sockets, briefing, history."""
    return (
        os.environ.get("ENGRAM_HOME")
        or os.path.expanduser("~/.engram")
    )


def _resolve_runtime_dir(engram_home: str) -> str:
    """Locate where engram_client.py lives for import.

    Priority:
      1. $ENGRAM_RUNTIME_DIR if set explicitly.
      2. Plugin root: hook lives at <plugin_root>/hooks/hook.py (flat layout —
         tools/build-plugin.sh copies hooks into <plugin_root>/hooks/ without
         a platform subdir), so the plugin root is two dirname() levels up
         from __file__. engram_client.py lives at <plugin_root>/. The plugin
         bundle is the canonical runtime; when present it MUST win so a stale
         data-dir snapshot can never shadow it (fixes #1152: scatter cleanup
         can leave ~/.engram/engram_client.py that does `import server` against
         a removed ~/.engram/server.py, crash-looping the daemon).
      3. $ENGRAM_HOME if it bundles a snapshot (scatter-install fallback only —
         reached only when there is no plugin bundle; covers scatter installs
         that copy engram_client.py into the data dir).
      4. Walk parents from __file__ for the nearest ancestor whose own
         src/engram/ actually contains engram_client.py, instead of guessing a
         fixed absolute path (#1712: the fixed guess resolves to the WRONG
         checkout when this process is running from a git worktree nested
         under the guessed path, e.g. .claude/worktrees/<id>/ under
         ~/engram-alpha -- silently shadowing the actually-running copy with
         a sibling one).
      5. ~/engram-alpha (live-source fallback for dev installs, last-ditch,
         unchanged).
    """
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    plugin_root = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    if os.path.exists(os.path.join(plugin_root, "engram_client.py")):
        return plugin_root
    if os.path.exists(os.path.join(engram_home, "engram_client.py")):
        return engram_home
    # Reviewer-fairy blocker (PR #1727): this function is called at bare
    # module-import time below (`PROJECT_DIR = _resolve_runtime_dir(...)`,
    # unguarded), so the walk itself must never raise. Path.exists() --
    # unlike os.path.exists() used at every other rung above -- raises
    # PermissionError on an unreadable ancestor instead of returning False;
    # an unhandled exception here would crash the whole hook process at
    # import time. Same precedent as _hooklib.resolve_tools_dir's own
    # docstring ("the caller's own import should be wrapped... and degrade
    # to a silent no-op on failure") -- wrapped here instead of at the call
    # site so this function keeps its existing never-raises contract.
    try:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "src" / "engram"
            if (candidate / "engram_client.py").exists():
                return str(candidate)
    except OSError:
        pass
    return os.path.expanduser("~/engram-alpha")  # last-ditch, unchanged


ENGRAM_HOME = _resolve_engram_home()
PROJECT_DIR = _resolve_runtime_dir(ENGRAM_HOME)

# Bridge for downstream callers: ensure ENGRAM_HOME is set so any sibling process inherits it.
os.environ.setdefault("ENGRAM_HOME", ENGRAM_HOME)


def _check_db_liveness(db_path: str) -> tuple[bool, str]:
    """Lightweight SQLite probe. Returns (True, 'ok') or (False, reason).

    Opens the DB read-only (mode=ro URI) to avoid touching WAL or journal.
    Catches SQLite I/O errors and DatabaseError (corrupt WAL, not-a-database).
    Fail-open on unexpected exceptions so probe bugs never break the hook.
    See #1218 (surface serves stale daemon cache under complete DB failure).
    """
    if not os.path.exists(db_path):
        return False, "knowledge.db not found"
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        conn.execute("PRAGMA user_version")
        return True, "ok"
    except sqlite3.DatabaseError as e:
        return False, str(e)
    except Exception:
        return True, "ok"  # fail-open: probe infrastructure bug ≠ DB failure
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _check_mcp_write_tool_marker(engram_home: str) -> tuple[bool, str | None]:
    """Check whether the MCP server wrote its initialization-complete marker.

    Deferred from SessionStart to here (UserPromptSubmit) to avoid a timing
    race: the new server's PID hasn't replaced the old PID in the marker yet
    at SessionStart time. By the first user prompt the race window is long past.

    Returns (True, None) if marker exists and its PID is still running, or on
    any unexpected error (advisory probe — never block).
    Returns (False, reason) if marker absent or its PID is no longer running.
    """
    import errno as _errno
    try:
        marker_path = os.path.join(engram_home, "mcp-tools-ready.json")
        if not os.path.exists(marker_path):
            return False, "mcp-tools-ready.json absent (server may not have completed initialization)"
        with open(marker_path) as f:
            data = json.load(f)
        pid = int(data.get("pid", 0))
        if not pid:
            return False, "mcp-tools-ready.json has no valid pid field"
        try:
            os.kill(pid, 0)
            return True, None
        except OSError as e:
            if e.errno == _errno.EPERM:
                return True, None  # process exists, no permission to signal — ok
            return False, f"mcp-tools-ready.json stale (server PID {pid} no longer running)"
    except Exception:
        return True, None  # advisory — never block
SOCKET_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.sock")
COUNTER_PATH = os.path.join(ENGRAM_HOME, "prompt-counter.json")
WRITE_REMINDER_PATH = os.path.join(ENGRAM_HOME, "write-reminder.json")
REPAIR_MARKER_PATH = os.path.join(ENGRAM_HOME, "toolcall-repair-pending.json")
FEELING_NUDGE_MARKER = os.path.join(ENGRAM_HOME, "feeling-nudge-active.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")
ERROR_PATTERNS_PATH = os.path.join(ENGRAM_HOME, "error_patterns.json")
ERROR_INCIDENTS_PATH = os.path.join(ENGRAM_HOME, "error_incidents.json")
CORNERSTONE_ANCHORS_PATH = os.path.join(ENGRAM_HOME, "cornerstone_anchors.json")
CORNERSTONE_ANCHOR_STATE_PATH = os.path.join(ENGRAM_HOME, "cornerstone-anchor-state.json")
KNOWLEDGE_DB_PATH = os.path.join(ENGRAM_HOME, "knowledge.db")

# Cross-prompt cooldown for cornerstone anchors: once a cornerstone fires,
# suppress it for this many prompts. Prevents the habituation failure — a
# principle nudge that fires every prompt trains the agent to dismiss it
# (#1691; same failure class the stop-hook #840/#845 gates solved).
CORNERSTONE_ANCHOR_COOLDOWN_PROMPTS = 10

# Unified principle-trigger registry (#1698 slice 2) — one registry,
# four kinds (lesson/cornerstone/axiom/goal), replacing the separate
# error-incidents + cornerstone-anchors read paths below.
PRINCIPLE_TRIGGERS_PATH = os.path.join(ENGRAM_HOME, "principle_triggers.json")
PRINCIPLE_TRIGGER_STATE_PATH = os.path.join(ENGRAM_HOME, "principle-trigger-state.json")
PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS = 10  # same value as today's CORNERSTONE_ANCHOR_COOLDOWN_PROMPTS
PRINCIPLE_TRIGGER_CAP = 2  # design doc §3 point 3
_PRINCIPLE_KIND_PRIORITY = {"lesson": 0, "axiom": 1, "cornerstone": 2, "goal": 3}  # lower = higher priority

# #1698 slice 3 — decay/enactment state shape + effective cooldown (design
# doc §4). RETIREMENT_CEILING_PROMPTS is the cap on how far a non-axiom
# principle's effective cooldown can grow via repeated fires-without-
# enactment; once reached the principle is, in practice, retired from
# injection (its cooldown almost never clears) but stays in the registry
# (still "covered" per engram_diagnose's principle_coverage, §4 below).
RETIREMENT_CEILING_PROMPTS = 160  # design doc §4: "effectively retired"

def get_user_name() -> str:
    """Read primary_user from $ENGRAM_HOME/config.json; fall back to 'the human'."""
    try:
        with open(os.path.join(ENGRAM_HOME, "config.json"), "r", encoding="utf-8") as f:
            config = json.load(f)
        return str(config.get("primary_user") or "the human")
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return "the human"


# Prompt count thresholds for consolidation warnings
NAP_WARN_THRESHOLD = 20   # Start suggesting nap
NAP_URGENT_THRESHOLD = 25  # Escalate urgency

# Auto-surface prev-response-prepending defaults — overridden by
# config.json auto_surface section if present. Per alpha #177 area 1.
DEFAULT_SHORT_PROMPT_THRESHOLD_CHARS = 100
DEFAULT_PREV_RESPONSE_TAIL_CHARS = 500

# IDF-based prepending gate defaults — alpha #177 area 1 refinement (PR after #192).
DEFAULT_IDF_GATE_MIN_IDF = 4.0
DEFAULT_IDF_GATE_SHORT_PROMPT_FLOOR_CHARS = 40
DEFAULT_IDF_GATE_ENABLED = True  # feature flag for safe rollout

# Duration after a daemon launch attempt during which the per-turn surface hook
# treats daemon-unreachable as a warmup state (SOFT message) rather than a genuine
# outage (CRITICAL). The daemon writes its PID and binds its socket only after the
# model load; typical load is 7-15s, but slower hardware (first install, cold OS cache)
# can take 30-60s. 120s covers all plausible cold-start scenarios without masking a
# genuine crash (after 120s a daemon that never bound has clearly failed).
# Configurable via ENGRAM_DAEMON_WARMUP_WINDOW_SECONDS for sites with unusually slow
# hardware (set to 0 to disable the warmup window entirely).
def _resolve_warmup_window() -> int:
    raw = os.environ.get("ENGRAM_DAEMON_WARMUP_WINDOW_SECONDS", "")
    if raw:
        try:
            val = int(raw)
            return max(0, val)
        except (ValueError, TypeError):
            pass
    return 120

_DAEMON_WARMUP_WINDOW_SECONDS = _resolve_warmup_window()

# Attached-pack surfacing quota — max results pulled across all packs combined.
_PACK_SURFACE_QUOTA = 3

# Mechanical latency bound — surfacing rides a hook timeout, so cap the number
# of pack DBs we open regardless of how many packs are configured.
_MAX_PACKS_TO_QUERY = 10


def _get_auto_surface_config() -> dict:
    """Read auto_surface tunables from $ENGRAM_HOME/config.json.

    Returns {short_prompt_threshold_chars, prev_response_tail_chars,
    idf_gate_enabled, idf_gate_min_idf, idf_gate_short_prompt_floor_chars}
    with defaults if config absent / malformed / section missing. Never raises.
    Cheap (one file read per hook fire — sub-ms typically).
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    defaults = {
        "short_prompt_threshold_chars": DEFAULT_SHORT_PROMPT_THRESHOLD_CHARS,
        "prev_response_tail_chars": DEFAULT_PREV_RESPONSE_TAIL_CHARS,
        "idf_gate_enabled": DEFAULT_IDF_GATE_ENABLED,
        "idf_gate_min_idf": DEFAULT_IDF_GATE_MIN_IDF,
        "idf_gate_short_prompt_floor_chars": DEFAULT_IDF_GATE_SHORT_PROMPT_FLOOR_CHARS,
    }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        section = config.get("auto_surface", {})
        return {
            "short_prompt_threshold_chars": int(
                section.get("short_prompt_threshold_chars", defaults["short_prompt_threshold_chars"])
            ),
            "prev_response_tail_chars": int(
                section.get("prev_response_tail_chars", defaults["prev_response_tail_chars"])
            ),
            "idf_gate_enabled": bool(
                section.get("idf_gate_enabled", defaults["idf_gate_enabled"])
            ),
            "idf_gate_min_idf": float(
                section.get("idf_gate_min_idf", defaults["idf_gate_min_idf"])
            ),
            "idf_gate_short_prompt_floor_chars": int(
                section.get("idf_gate_short_prompt_floor_chars", defaults["idf_gate_short_prompt_floor_chars"])
            ),
        }
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return defaults


# Surfaced-recency suppression defaults — recall-triggering blueprint §3-P1
# (issue #1689). Overridden by config.json's `recall_suppression` section,
# same read pattern as `_get_auto_surface_config`.
DEFAULT_SUPPRESSION_K = 5   # trailing-prompt window
DEFAULT_SUPPRESSION_M = 3   # repeat-count threshold before full suppression

# Ledger retention (~/.engram/surface-ledger.json). Two independent bounds,
# applied together (belt-and-suspenders — either alone would bound growth,
# combining them tolerates occasional gaps without losing the trailing-k
# signal): per-session entries older than DEFAULT_LEDGER_ENTRY_MAX_AGE_HOURS
# are dropped, and each session's remaining entries are capped at
# max(4*k, DEFAULT_LEDGER_RETENTION_ENTRIES) — a safety margin over the
# configured k so the window read never runs dry on catch-up/out-of-order
# writes. Sessions with no activity in DEFAULT_LEDGER_SESSION_MAX_AGE_HOURS
# are dropped entirely, so the ledger doesn't accumulate one entry-set per
# session forever across the life of the install.
DEFAULT_LEDGER_ENTRY_MAX_AGE_HOURS = 24
DEFAULT_LEDGER_RETENTION_ENTRIES = 20
DEFAULT_LEDGER_SESSION_MAX_AGE_HOURS = 48


def _get_suppression_config() -> dict:
    """Read recall_suppression tunables from $ENGRAM_HOME/config.json.

    Returns {k, m} with defaults if config absent / malformed / section
    missing. Never raises. Same read pattern as _get_auto_surface_config.
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    defaults = {"k": DEFAULT_SUPPRESSION_K, "m": DEFAULT_SUPPRESSION_M}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        section = config.get("recall_suppression", {})
        return {
            "k": int(section.get("k", defaults["k"])),
            "m": int(section.get("m", defaults["m"])),
        }
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return defaults


def _surface_ledger_path() -> str:
    """Path to the surfaced-recency ledger. Reads the module-level ENGRAM_HOME
    name at call time (not a frozen module-load-time constant) so tests that
    override `hook.ENGRAM_HOME` after import (the established pattern in this
    file's test suite — see e.g. test_surface_hook_attached_packs.py) redirect
    ledger I/O correctly, same as get_user_name()'s dynamic ENGRAM_HOME read.
    """
    return os.path.join(ENGRAM_HOME, "surface-ledger.json")


def _read_surface_ledger() -> dict:
    """Read the surfaced ledger. Any failure → {} (fail-open — the ledger is
    an advisory cache for render-layer suppression, never a source of truth).
    """
    try:
        with open(_surface_ledger_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return {}


def _write_surface_ledger(ledger: dict) -> None:
    """Best-effort ledger write. Any failure is swallowed — fail-open."""
    try:
        path = _surface_ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ledger, f)
    except Exception:
        pass


def _prune_surface_ledger(
    ledger: dict, *, now_ts: float, retention_entries: int,
    entry_max_age_hours: float, session_max_age_hours: float,
) -> dict:
    """Prune the ledger: drop inactive sessions, trim each session's entries.

    A session is dropped entirely if its most-recent entry is older than
    session_max_age_hours (checked against the RAW last entry, before
    per-entry age filtering — this is what bounds the ledger's total size
    across the life of the install). Surviving sessions have entries older
    than entry_max_age_hours dropped, then are capped at the last
    retention_entries entries.
    """
    entry_cutoff = now_ts - (entry_max_age_hours * 3600)
    session_cutoff = now_ts - (session_max_age_hours * 3600)
    pruned: dict = {}
    for sid, entries in ledger.items():
        if not isinstance(entries, list) or not entries:
            continue
        last = entries[-1]
        last_ts = last.get("ts", 0) if isinstance(last, dict) else 0
        if last_ts < session_cutoff:
            continue  # session inactive too long — drop entirely
        fresh = [e for e in entries if isinstance(e, dict) and e.get("ts", 0) >= entry_cutoff]
        fresh = fresh[-retention_entries:]
        if fresh:
            pruned[sid] = fresh
    return pruned


def _append_surface_ledger_entry(session_id: str, rendered_ids, *, k: int) -> None:
    """Append this prompt's actually-rendered node ids to the surfaced ledger
    for session_id, then prune. Best-effort / fail-open throughout — the
    ledger is an advisory cache; any I/O or parse failure here must never
    break the hook. See docs/recall-triggering-blueprint.md §3-P1.
    """
    try:
        now_ts = time.time()
        ledger = _read_surface_ledger()
        entries = ledger.get(session_id, [])
        if not isinstance(entries, list):
            entries = []
        entries.append({"ts": now_ts, "ids": sorted(set(rendered_ids))})
        ledger[session_id] = entries
        retention_entries = max(4 * max(int(k), 1), DEFAULT_LEDGER_RETENTION_ENTRIES)
        ledger = _prune_surface_ledger(
            ledger,
            now_ts=now_ts,
            retention_entries=retention_entries,
            entry_max_age_hours=DEFAULT_LEDGER_ENTRY_MAX_AGE_HOURS,
            session_max_age_hours=DEFAULT_LEDGER_SESSION_MAX_AGE_HOURS,
        )
        _write_surface_ledger(ledger)
    except Exception:
        pass


def _get_trailing_window_ids(session_id: str, k: int) -> list:
    """Return the id-lists of the trailing k ledger entries for session_id.

    [] on any failure or when the session has no history (fail-open — a
    fresh/unknown session simply renders full-tier, as today).
    """
    try:
        if k <= 0:
            return []
        ledger = _read_surface_ledger()
        entries = ledger.get(session_id, [])
        if not isinstance(entries, list):
            return []
        trailing = entries[-k:]
        return [
            e.get("ids", []) for e in trailing
            if isinstance(e, dict) and isinstance(e.get("ids"), list)
        ]
    except Exception:
        return []


def _count_occurrences_in_window(node_id, window) -> int:
    """Count how many of the trailing-k ledger entries contain node_id."""
    if not node_id:
        return 0
    return sum(1 for ids in window if node_id in ids)


def _classify_suppression(count: int, m: int) -> str:
    """Classify a node's render tier from its prior-occurrence count within
    the trailing k window (per issue #1689's demotion tiers):
      0 or 1 prior occurrence  → "full"    (render as today)
      2 .. m-1 prior occurrences → "others"  (demote to keyword-only)
      >= m prior occurrences   → "suppress" (don't render at all)
    """
    if count <= 1:
        return "full"
    if count < m:
        return "others"
    return "suppress"


def _others_candidate_from_full_entry(entry: dict) -> dict:
    """Build a matched_meta-shaped candidate (id, recall_keywords, [tainted],
    [stale]) from a full special/top_claim entry — used when a would-be
    full-tier render is demoted to the Others (keyword-only) tier.
    """
    kw = entry.get("recall_keywords")
    cand: dict = {
        "id": entry.get("id"),
        "recall_keywords": kw if isinstance(kw, list) else None,
    }
    w = entry.get("warnings")
    if w:
        if w.get("tainted_by"):
            cand["tainted"] = True
        if w.get("stale_by"):
            cand["stale"] = True
    return cand


# Per-prompt injection budget — recall-triggering blueprint §3-P4 (issue
# #1692). Overridden by config.json's `recall_budget` section, same read
# pattern as `_get_auto_surface_config` / `_get_suppression_config`.
#
# Default is deliberately generous ("current typical render" per the
# blueprint, with headroom): a dense render (3 specials + 3 top claims + 2
# not-recalled reservations + 15 Others lines + a handful of attached-pack
# lines) runs well under 4,000 chars in practice, so 6,000 chars gives
# comfortable margin without being effectively unlimited. This is the number
# that makes the backward-compat invariant (default config → byte-identical
# output to pre-P4) hold structurally rather than by accident.
DEFAULT_RECALL_BUDGET_TOTAL_CHARS = 6000
DEFAULT_RECALL_BUDGET_ENABLED = True


def _get_budget_config() -> dict:
    """Read recall_budget tunables from $ENGRAM_HOME/config.json.

    Returns {total_chars, enabled} with defaults if config absent / malformed
    / section missing. Never raises. Same read pattern as
    _get_auto_surface_config / _get_suppression_config.
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    defaults = {
        "total_chars": DEFAULT_RECALL_BUDGET_TOTAL_CHARS,
        "enabled": DEFAULT_RECALL_BUDGET_ENABLED,
    }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        section = config.get("recall_budget", {})
        return {
            "total_chars": int(section.get("total_chars", defaults["total_chars"])),
            "enabled": bool(section.get("enabled", defaults["enabled"])),
        }
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return defaults


def _budget_try_spend(line_text: str, budget_state: dict) -> bool:
    """Attempt to spend a rendered line's cost against the remaining budget.

    budget_state is a small mutable dict the caller owns across an entire
    format_nudge() render:
      {"remaining": int | None, "exhausted": bool,
       "chars_rendered": int, "nodes_shown": int, "nodes_cut_budget": int}

    remaining=None means the budget is disabled (unlimited) — every line
    fits, and only chars_rendered/nodes_shown bookkeeping happens (no cuts
    are ever recorded). This is the kill-switch path (recall_budget.enabled
    = False) and is what keeps that path byte-identical to the pre-P4 fixed
    3/3/15 caps.

    Once exhausted (a line didn't fit), ALL subsequent calls return False —
    the allocator is a one-way stop, not a skip-ahead-to-find-something-
    smaller re-ranker (deliberate simplification, see PR body / issue #1692).
    This is why every remaining tier's candidates still need to be walked by
    the caller (not early-`break`ed out of): nodes_cut_budget must count every
    candidate that was eligible to render but didn't fit, across all tiers.

    Cost = len(line_text) + 1, the +1 accounting for the joining "\n" each
    rendered line contributes when the caller later does "\n".join(lines).
    """
    if budget_state["exhausted"]:
        budget_state["nodes_cut_budget"] += 1
        return False
    if budget_state["remaining"] is None:
        budget_state["chars_rendered"] += len(line_text) + 1
        budget_state["nodes_shown"] += 1
        return True
    cost = len(line_text) + 1
    if cost > budget_state["remaining"]:
        budget_state["exhausted"] = True
        budget_state["nodes_cut_budget"] += 1
        return False
    budget_state["remaining"] -= cost
    budget_state["chars_rendered"] += cost
    budget_state["nodes_shown"] += 1
    return True


def _get_attached_packs() -> list[dict]:
    """Read attached_packs from $ENGRAM_HOME/config.json.

    Returns a list of enabled pack dicts, each with 'id' and 'path' keys.
    Any error (missing file, malformed JSON, missing section) → [] (fail-open).
    Only entries with enabled=True and non-empty id+path are returned.
    """
    try:
        config_path = os.path.join(ENGRAM_HOME, "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        packs = config.get("attached_packs", [])
        if not isinstance(packs, list):
            return []
        result = []
        for entry in packs:
            if not isinstance(entry, dict):
                continue
            if not entry.get("enabled"):
                continue
            pack_id = entry.get("id", "")
            pack_path = entry.get("path", "")
            if not pack_id or not pack_path:
                continue
            result.append({"id": str(pack_id), "path": str(pack_path)})
        return result
    except Exception:
        return []


def _query_attached_packs(prompt: str, quota: int = _PACK_SURFACE_QUOTA) -> list[dict]:
    """Query attached external packs for FTS matches.

    For each enabled attached pack (config order): opens knowledge.db read-only,
    runs an FTS query, collects up to `quota` results TOTAL across all packs
    (config-order priority, then bm25 rank). Each result dict has pack_id tagged.

    Invariants:
    - Pack DBs opened mode=ro only — read-only is a hard constraint, not an
      optimization.
    - Missing DB, unreadable DB, or any per-pack error → skip that pack
      silently (fail-open).
    - Any exception → return [] (never break own-graph surfacing).
    """
    try:
        packs = _get_attached_packs()
        if not packs:
            return []

        # Build FTS MATCH expression: tokenize to alphanumeric words ≥3 chars,
        # take up to 8, quote-wrap each to prevent FTS5 syntax errors.
        words = re.findall(r'[a-zA-Z0-9]{3,}', prompt)[:8]
        if not words:
            return []
        match_expr = " OR ".join(f'"{w}"' for w in words)

        results: list[dict] = []
        for pack in packs[:_MAX_PACKS_TO_QUERY]:
            if len(results) >= quota:
                break
            remaining = quota - len(results)
            pack_id = pack["id"]
            db_path = os.path.join(pack["path"], "knowledge.db")
            try:
                # immutable=1: engram-pkg-built packs are WAL-mode; a plain mode=ro
                # connection creates a .db-shm sidecar (a write INTO the pack dir,
                # grazing the read-only invariant). immutable=1 declares the file
                # static — no locking, no -shm/-wal sidecars — which is correct for
                # attached pack archives. Trade-off: if a pack is concurrently
                # re-authored in place, immutable reads may be stale; attached packs
                # are static downloads, and re-authoring an attached pack in place is
                # out of contract.
                conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        """
                        SELECT n.id, n.type, n.claim, n.confidence,
                               n.recall_summary, n.recall_keywords
                        FROM nodes_fts f
                        JOIN nodes n ON n.rowid = f.rowid
                        WHERE f.nodes_fts MATCH ? AND n.is_current = 1
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (match_expr, remaining),
                    ).fetchall()
                    for row in rows:
                        kw = row["recall_keywords"]
                        if kw is not None:
                            try:
                                kw = json.loads(kw)
                            except (json.JSONDecodeError, TypeError):
                                kw = None
                        results.append({
                            "id": row["id"],
                            "type": row["type"],
                            "claim": row["claim"] or "",
                            "confidence": row["confidence"],
                            "recall_summary": row["recall_summary"],
                            "recall_keywords": kw,
                            "pack_id": pack_id,
                        })
                except Exception:
                    # FTS5 OperationalError on weird tokens, missing table, etc.
                    # — skip this pack silently.
                    pass
                finally:
                    conn.close()
            except Exception:
                # DB missing, unreadable, or connection error — skip silently.
                pass

        return results
    except Exception:
        return []


def _read_prev_assistant_tail(transcript_path: str | None, tail_chars: int) -> str:
    """Return the last `tail_chars` characters of the most-recent assistant
    text message in the session JSONL. Empty string on any failure (missing
    file, no prior assistant, malformed lines). Never raises.

    Scans backward (read whole file, iterate reversed) since JSONL transcripts
    are append-only and not huge in practice (a typical session JSONL is well
    under 10 MB even after compaction).

    TODO: if session JSONLs grow into tens-of-MB range (long-loop or
    cross-compaction continuity work), switch to a tail-seek pattern (open
    in binary mode, seek to end, walk backward by chunks looking for the
    last few newlines). Round-1 PR #186 fairy flagged this as a real
    long-tail concern; round-number defer until measured.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message", {}) or {}
        content = msg.get("content", []) or []
        # content is a list of blocks (text/tool_use/...) — concatenate text blocks
        text_parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", "") or "")
        elif isinstance(content, str):
            text_parts.append(content)
        full_text = "".join(text_parts).strip()
        if not full_text:
            # An assistant entry with no text body (e.g., pure tool_use) —
            # skip it and keep scanning further back.
            continue
        return full_text[-tail_chars:] if len(full_text) > tail_chars else full_text
    return ""


def _should_prepend(prompt: str, conn: sqlite3.Connection, cfg: dict) -> bool:
    """IDF-based prepending gate. Returns True if prev_response_tail should be
    prepended to the embedding query.

    Three-rule decision, in order:
      Rule 1: very short prompts can't self-anchor — always prepend.
      Rule 2: if prompt has high-IDF non-stopword tokens, it self-anchors — skip.
      Fallback: if engram_idf unavailable or any DB error occurs, fall back to
        the legacy char-length heuristic. Preserves previous behavior on infra
        failure.

    Note: only called when cfg["idf_gate_enabled"] is True. The feature-flag
    check lives in the call site (main()), not here, so the function contract
    is clean for tests.
    """
    # Rule 1: very short prompts can't self-anchor — always prepend.
    if len(prompt) < cfg["idf_gate_short_prompt_floor_chars"]:
        return True

    # Rule 2: if prompt has high-IDF non-stopword tokens, it self-anchors — skip.
    # NOTE: we read the existing nodes_fts_vocab table but do NOT create it here.
    # The hook opens a read-only connection (mode=ro), so a CREATE would fail
    # anyway, but the deeper reason is separation of concerns: the hook reads;
    # server.py startup is responsible for ensuring the schema exists. If the
    # vocab table is missing, extract_keywords raises OperationalError and the
    # except-block falls back to the legacy heuristic — safe behavior on any
    # install that hasn't yet picked up the FTS-rewrite PR's startup bootstrap.
    try:
        if PROJECT_DIR not in sys.path:
            sys.path.insert(0, PROJECT_DIR)
        from engram_idf import extract_keywords
        keywords = extract_keywords(
            conn,
            prompt,
            min_idf=cfg["idf_gate_min_idf"],
            top_k=5,
        )
        if keywords:
            return False  # prompt has self-anchoring keywords
        return True  # no high-IDF keywords — prepend for context
    except Exception:
        # Fallback to legacy char-length heuristic if engram_idf unavailable
        # (ImportError) or any DB error occurs (vocab table missing, mode=ro
        # conflict, etc). Preserves previous behavior on infra failure.
        return len(prompt) < cfg["short_prompt_threshold_chars"]


def query_daemon(prompt: str, top_k: int = 10, embed_query: str | None = None) -> dict | None:
    """Send query to the daemon via Unix socket. Returns result dict or None.

    embed_query: optional separate semantic-search string. When provided,
    semantic search uses it while FTS still uses prompt. Used for short-
    prompt prev-response-tail prepending (alpha #177 area 1).
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(SOCKET_PATH)

        req_obj: dict = {"query": prompt, "top_k": top_k}
        if embed_query is not None:
            req_obj["embed_query"] = embed_query
        request = json.dumps(req_obj) + "\n"
        sock.sendall(request.encode("utf-8"))

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

    except (socket.error, json.JSONDecodeError, OSError):
        return None


def query_fts_fallback(prompt: str, top_k: int = 10) -> dict | None:
    """Fallback: direct engram_surface with FTS-only (no semantic)."""
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        if PROJECT_DIR not in sys.path:
            sys.path.insert(0, PROJECT_DIR)
        from engram_client import EngramClient
        client = EngramClient()
        return client.call("engram_surface", {"query": prompt, "top_k": top_k, "semantic": False})
    except Exception:
        return None


_COMPACT_DAY_RE = re.compile(r"^(\d+d)")
_COMPACT_SUBDAY_RE = re.compile(r"^\d+[sh]$|^\d+m$|^\d+h\d+m$")


def _compact_age(created_ago: str | None) -> str:
    """Compact a _humanized_ago string to a short recency tag for surface lines.

    Maps the full "Xd4h ago" / "3h30m ago" forms from _humanized_ago to:
      sub-day (s/m/h forms — careful: "m" not "mo")  → "0d"
      Nd[Xh]           → "Nd"  (strip trailing hours)
      Nw / Nmo / Ny    → unchanged
      ? / parse-error / None → "?"
    """
    if not created_ago or created_ago in ("?", "parse-error", "future?"):
        return "?"
    s = created_ago.replace(" ago", "")
    if _COMPACT_SUBDAY_RE.match(s):
        return "0d"
    m = _COMPACT_DAY_RE.match(s)
    if m:
        return m.group(1)
    return s


def render_one_node_line(entry: dict, *, conf_prefix: bool, type_tag: bool) -> str:
    """Render a single node as a compact line for the recall nudge.

    Format:
        - [<id>] <conf or type-tag> · <age>  <kw_prefix><summary_or_fallback>

    Args:
        entry: node dict (id, claim, confidence, recall_summary, recall_keywords,
            type, created_ago, …)
        conf_prefix: if True, render ``(conf X.XX)`` ONLY when
            ``entry.get("confidence") is not None``. Passed True for both
            specials and top_claims — the None-guard suppresses the prefix
            for special types that lack a confidence field (question,
            person, definition, goal, lesson, contradiction) while still
            rendering it for axioms/conjectures (which carry epistemic weight).
        type_tag: if True, render ``[<type>]`` tag (for specials, so agent can
            scan what kind of anchor this is).
    """
    nid = entry.get("id", "?")
    ntype = entry.get("type", "?")
    claim = entry.get("claim", "")
    confidence = entry.get("confidence")
    recall_summary = entry.get("recall_summary")
    recall_keywords = entry.get("recall_keywords")

    # Taint/staleness marker — additive-only: entries with no "warnings" key
    # (the common case) render byte-identical to prior output.
    warnings = entry.get("warnings")
    warn_marker = ""
    if warnings:
        tags = []
        if warnings.get("tainted_by"):
            tags.append("TAINTED")
        if warnings.get("stale_by"):
            tags.append("STALE")
        if tags:
            warn_marker = f"⚠ {'/'.join(tags)} "

    # Build prefix tokens (conf / type-tag)
    prefix_parts = []
    if type_tag:
        prefix_parts.append(f"[{ntype}]")
    if conf_prefix and confidence is not None:
        prefix_parts.append(f"(conf {confidence:.2f})")
    prefix = (" ".join(prefix_parts) + " ") if prefix_parts else ""

    # Compact recency tag (v1 autobiographical distance — filing date only)
    age = _compact_age(entry.get("created_ago"))
    age_tag = f"· {age}  "

    # Build keyword prefix: `kw1` · `kw2` · `kw3` —
    if isinstance(recall_keywords, list) and len(recall_keywords) >= 1:
        kw_prefix = " · ".join(f"`{kw}`" for kw in recall_keywords) + " — "
    else:
        kw_prefix = ""

    # Summary or fallback
    if recall_summary is not None:
        body = recall_summary
    elif len(claim) > 120:
        body = claim[:117] + "…"
    else:
        body = claim

    return f"- {warn_marker}[{nid}] {prefix}{age_tag}{kw_prefix}{body}"


def format_nudge(
    result: dict,
    pack_results: list[dict] | None = None,
    session_id: str | None = None,
    stats: dict | None = None,
) -> str:
    """Format engram_surface result into a compact nudge string.

    Layout (2026-05-19 redesign; suppression tiers added 2026-07-07, #1689;
    injection budget added 2026-07-07, #1692):
      1. Header
      2. Suppressed-count line (when this render suppressed ≥1 node)
      3. Noteworthy: <type counts> (when specials present)
      4. Specials section (rendered with full content, BEFORE top_claims)
      5. Top claims section (with keyword+summary format)
      6. Worth revisiting: not-recalled-recently reservation (when a
         special/top_claim slot was freed — by suppression or by fewer than
         3 nodes qualifying naturally)
      7. Warnings / Memory / Others / IDs / footer
      8. Attached-library section (when pack_results is non-empty)

    Types: line dropped — Noteworthy already conveys the type breakdown for
    specials, and when no specials are present the Types line is stats-noise.

    pack_results: list of node dicts from _query_attached_packs(), each tagged
    with 'pack_id'. Omitted or empty → section not rendered (zero-packs invariant:
    byte-identical output to prior behavior when no packs are configured).
    Pack lines are NOT counted against the injection budget below — same
    "separate provenance" discipline #1689 already applied to suppression.

    session_id: recall-triggering blueprint §3-P1 surfaced-ledger suppression
    (issue #1689). When None (the legacy call shape — every pre-existing
    caller/test in this repo omits it), suppression is disabled entirely: every
    candidate classifies as "full" tier and the not-recalled reservation never
    fires, so output is byte-identical to pre-suppression behavior (this is
    the "zero-suppression invariant" this file's tests pin). When a session_id
    is provided (main() always passes one), nodes surfaced ≥2 times in the
    trailing k prompts for this session demote to Others-tier or suppress
    entirely — render-layer only, matching/ranking are untouched. This
    function owns the ledger read (to compute tiers) AND the ledger write (to
    record what was actually rendered) — see docs/recall-triggering-blueprint.md
    §3-P1 for the design.

    Injection budget (recall-triggering blueprint §3-P4, issue #1692):
    node-line candidates across ALL tiers (Specials → Top claims → not-
    recalled reservation → Others, the existing fixed tier-priority order)
    are rendered against a single global per-prompt character budget
    (config.json `recall_budget.total_chars`; `recall_budget.enabled` is a
    kill switch). The existing fixed per-tier caps (3 specials / 3 top
    claims / ≤2 reservations / 15 Others) are UNCHANGED — the budget is an
    additional gate applied within those caps, not a replacement for them,
    and it never re-sorts: each tier's candidates are walked in their
    existing (already relevance-sorted upstream) order, and the first
    candidate that doesn't fit the remaining budget trips a one-way
    "exhausted" flag — every later candidate, in this tier and every
    subsequent tier, is cut too (never re-checked against a since-freed
    budget, never skipped-past in search of a smaller one that would fit).
    This is a deliberate simplification: engram_query.py does not surface a
    per-candidate relevance score to this render layer, so this function
    treats "existing arrival order" as the relevance signal instead of
    inventing a synthetic composite score — see the PR body for the full
    rationale. Structural header/footer lines (Noteworthy, section titles,
    Warnings, Memory, the trailing "Use engram_inspect..." line, the
    suppressed-count line) are NOT counted against the budget — only the
    variable-length per-candidate node lines are, since those are what scale
    with match volume. Budget-cut candidates are dropped entirely (never
    demoted to Others — Others is itself budget-gated) and are NOT recorded
    in the surfaced ledger (the agent never actually saw them).
    Default config (recall_budget absent, or enabled=True with the generous
    default total_chars) is byte-identical to pre-#1692 output for any
    realistically-sized render — the default is sized well above what the
    existing fixed caps can ever produce. `recall_budget.enabled=False` is
    the explicit kill switch: every candidate is treated as unlimited-budget
    (structurally identical to the pre-#1692 code path).

    stats: optional caller-owned dict, mutated in place (out-parameter) with
    render telemetry for the engram.surface.render_size event (issue #1692):
    {budget_enabled, budget_total_chars, budget_exhausted, chars_rendered,
    nodes_shown, nodes_suppressed, nodes_cut_budget}. Populated even on the
    total==0 early-return (all zeroed / budget config echoed) so callers can
    rely on the dict shape unconditionally. None (the default) skips
    telemetry entirely — no behavior change, matches the pack_results /
    session_id optional-param precedent above.
    """
    budget_cfg = _get_budget_config()
    if stats is not None:
        stats.clear()
        stats.update({
            "budget_enabled": budget_cfg["enabled"],
            "budget_total_chars": budget_cfg["total_chars"] if budget_cfg["enabled"] else None,
            "budget_exhausted": False,
            "chars_rendered": 0,
            "nodes_shown": 0,
            "nodes_suppressed": 0,
            "nodes_cut_budget": 0,
        })

    total = result.get("match_count", 0)
    if total == 0:
        return ""

    # --- Suppression setup ---------------------------------------------
    suppression_active = session_id is not None
    sup_cfg = {"k": DEFAULT_SUPPRESSION_K, "m": DEFAULT_SUPPRESSION_M}
    window: list = []
    if suppression_active:
        sup_cfg = _get_suppression_config()
        window = _get_trailing_window_ids(session_id, sup_cfg["k"])

    def _tier_for(nid) -> str:
        if not suppression_active or not nid:
            return "full"
        count = _count_occurrences_in_window(nid, window)
        return _classify_suppression(count, sup_cfg["m"])

    # --- Budget setup -------------------------------------------------
    # remaining=None when recall_budget.enabled=False (the kill switch) —
    # unlimited: every candidate fits, matching the pre-#1692 fixed-cap
    # behavior exactly.
    budget_state = {
        "remaining": budget_cfg["total_chars"] if budget_cfg["enabled"] else None,
        "exhausted": False,
        "chars_rendered": 0,
        "nodes_shown": 0,
        "nodes_cut_budget": 0,
    }

    lines = []
    lines.append(f"[ENGRAM Recall: {total} nodes match your query]")

    rendered_ids_this_prompt: set = set()
    suppressed_ids: set = set()
    demoted_candidates: list[dict] = []
    # Nodes that were eligible (full-tier or Others-tier) in one section but
    # got cut by the budget rather than rendered. Kept SEPARATE from
    # rendered_ids_this_prompt (which must stay ledger-accurate — a
    # budget-cut node was never actually seen, so it must never be written
    # to the surfaced ledger) but still needs to exclude the node from being
    # re-evaluated (and re-counted as a SECOND cut) by a later section that
    # also considers it — e.g. a budget-cut top claim that's also present in
    # matched_meta would otherwise fall through to the Others pool and get
    # counted twice. See PR #1701 round-1 review + its double-count follow-up.
    budget_cut_ids: set = set()

    # Special nodes — rendered BEFORE top_claims
    special = result.get("special_nodes", [])
    special_rendered_count = 0
    if special:
        from collections import Counter
        # Noteworthy reflects the ORIGINAL matched special set (match
        # composition), not the post-suppression render count — a demoted/
        # suppressed special is still "noteworthy" in kind.
        type_counts_special = Counter(n.get("type", "unknown") for n in special)
        parts = [f"{c} {t}" for t, c in type_counts_special.items()]
        lines.append(f"  Noteworthy: {', '.join(parts)}")
        special_lines = []
        # Defensive [:3] — engram_surface already caps specials at 3 (server.py:5269),
        # but the slice here protects against any future surface payload that returns more.
        for s in special[:3]:
            nid = s.get("id")
            tier = _tier_for(nid)
            if tier == "full":
                line = render_one_node_line(s, conf_prefix=True, type_tag=True)
                rendered_line = f"    {line}"
                if _budget_try_spend(rendered_line, budget_state):
                    special_lines.append(rendered_line)
                    rendered_ids_this_prompt.add(nid)
                    special_rendered_count += 1
                else:
                    # eligible (full-tier) but budget-cut — dropped, not
                    # demoted, not marked "seen" (agent never saw this
                    # line), but still excluded from Others below so it's
                    # not re-evaluated and double-counted as a second cut.
                    budget_cut_ids.add(nid)
            elif tier == "others":
                demoted_candidates.append(_others_candidate_from_full_entry(s))
            else:  # "suppress"
                suppressed_ids.add(nid)
        if special_lines:
            lines.append("  Specials:")
            lines.extend(special_lines)

    # Top claims (keyword+summary format)
    top_claims = result.get("top_claims", [])
    top_claims_rendered_count = 0
    if top_claims:
        top_claim_lines = []
        # Defensive [:3] — engram_surface already caps top_claims at 3 (server.py:5296);
        # same future-proofing as above.
        for c in top_claims[:3]:
            nid = c.get("id")
            tier = _tier_for(nid)
            if tier == "full":
                line = render_one_node_line(c, conf_prefix=True, type_tag=False)
                rendered_line = f"    {line}"
                if _budget_try_spend(rendered_line, budget_state):
                    top_claim_lines.append(rendered_line)
                    rendered_ids_this_prompt.add(nid)
                    top_claims_rendered_count += 1
                else:
                    # eligible (full-tier) but budget-cut — dropped, not
                    # demoted, not marked "seen" (agent never saw this
                    # line), but still excluded from Others below so it's
                    # not re-evaluated and double-counted as a second cut.
                    budget_cut_ids.add(nid)
            elif tier == "others":
                demoted_candidates.append(_others_candidate_from_full_entry(c))
            else:  # "suppress"
                suppressed_ids.add(nid)
        if top_claim_lines:
            lines.append("  Top claims:")
            lines.extend(top_claim_lines)

    matched_meta = result.get("matched_meta") or []

    # --- Not-recalled slot reservation (recall-triggering blueprint §3-P1,
    # second lever) — reserve up to 2 of the special/top_claim slots freed by
    # suppression above, OR simply left empty because fewer than 3 nodes
    # qualified naturally, for the top-ranked matches from
    # age.not_recalled_recently. Full-tier quality needs confidence /
    # recall_summary / created_ago, which matched_meta does not carry
    # (id, type, recall_keywords[, tainted, stale] only) — so these render at
    # Others (keyword-only) quality with a distinguishing section header
    # instead of inventing fields that don't exist upstream.
    reserved_lines: list[str] = []
    if suppression_active:
        freed_slots = max(0, 3 - special_rendered_count) + max(0, 3 - top_claims_rendered_count)
        reserve_n = min(2, freed_slots)
        if reserve_n > 0:
            not_recalled_ids = result.get("age", {}).get("not_recalled_recently", [])
            if isinstance(not_recalled_ids, list):
                meta_by_id = {m.get("id"): m for m in matched_meta}
                already_placed = (
                    rendered_ids_this_prompt | suppressed_ids
                    | {d.get("id") for d in demoted_candidates}
                )
                for nid in not_recalled_ids:
                    if len(reserved_lines) >= reserve_n:
                        break
                    # No early exit on budget_state["exhausted"] here (unlike
                    # the reserve_n cap above): _budget_try_spend's own
                    # contract (see its docstring) is that nodes_cut_budget
                    # counts every candidate that reaches it post-exhaustion,
                    # not just the first. Breaking early here would silently
                    # undercount cuts for every remaining eligible candidate
                    # (PR #1701 round-1 review finding — reproduced empirically:
                    # 15 eligible Others candidates, budget for 3, undercounted
                    # 12 cuts as 1). Eligibility filtering below is cheap
                    # (dict lookups, no I/O) so walking the full list even
                    # once budget is spent is not a real cost concern.
                    if not nid or nid in already_placed:
                        continue
                    if _tier_for(nid) == "suppress":
                        continue
                    meta = meta_by_id.get(nid)
                    if not meta:
                        continue  # no metadata available upstream — skip, don't invent fields
                    kw = meta.get("recall_keywords")
                    if not (isinstance(kw, list) and len(kw) >= 1):
                        continue  # no keywords — no recognition value, matches Others discipline
                    kw_str = " · ".join(f"`{k}`" for k in kw)
                    tags = []
                    if meta.get("tainted"):
                        tags.append("TAINTED")
                    if meta.get("stale"):
                        tags.append("STALE")
                    marker = f"⚠ {'/'.join(tags)} " if tags else ""
                    reserved_line = f"    - {marker}[{nid}] {kw_str}"
                    if not _budget_try_spend(reserved_line, budget_state):
                        already_placed.add(nid)
                        budget_cut_ids.add(nid)
                        continue
                    reserved_lines.append(reserved_line)
                    rendered_ids_this_prompt.add(nid)
                    already_placed.add(nid)
        if reserved_lines:
            lines.append("  Worth revisiting (not recalled recently):")
            lines.extend(reserved_lines)

    # Age / issues
    age = result.get("age", {})
    issues = result.get("issues", {})
    stale = issues.get("stale_nodes", 0) if isinstance(issues, dict) else 0
    tainted = issues.get("tainted_nodes", 0) if isinstance(issues, dict) else 0
    if stale or tainted:
        parts = []
        if stale:
            parts.append(f"{stale} stale")
        if tainted:
            parts.append(f"{tainted} tainted")
        lines.append(f"  Warnings: {', '.join(parts)} nodes in results")

    not_recalled = age.get("not_recalled_recently", [])
    if isinstance(not_recalled, list) and not_recalled:
        lines.append(f"  Memory: {len(not_recalled)} nodes not recalled recently")

    # Others — non-top non-special matched IDs with keyword prefix
    # (the maintainer extension, keyword-only filter per #234 2026-05-20).
    # Each line: "[id] `kw1` · `kw2` · ..." — nodes without keywords are dropped
    # (no-keywords nodes provide no recognition value in skim, just noise).
    # Keywords-only (no summary) gives the agent a faceted index for "which of
    # these should I inspect" while preserving the lossy-by-design noetic-register.
    #
    # Suppression tiers apply here too (issue #1689 §3-P1: "Specials follow
    # the same rule" — so do Others candidates): a node hitting the suppress
    # threshold is dropped even from keyword-only tier; demoted specials/
    # top_claims are merged into this same pool, at the same keyword-required
    # discipline as natural Others candidates.
    excluded_from_others = (
        rendered_ids_this_prompt | suppressed_ids
        | {d.get("id") for d in demoted_candidates}
        | budget_cut_ids
    )
    others_candidates: list[dict] = []
    for m in matched_meta:
        mid = m.get("id")
        if mid in excluded_from_others:
            continue
        if _tier_for(mid) == "suppress":
            suppressed_ids.add(mid)
            continue
        others_candidates.append(m)
    others_candidates.extend(demoted_candidates)
    # Filter to nodes with at least 1 keyword; drop bare-ID entries entirely
    # (per the maintainer issue #234 Findings 1+2): no-keywords nodes provide
    # no recognition value in skim, just noise.
    others_with_kw = [
        m for m in others_candidates
        if isinstance(m.get("recall_keywords"), list)
        and len(m.get("recall_keywords") or []) >= 1
    ]
    # Budget-gated render pass. Built into a separate list first (rather than
    # appending straight to `lines`) so the "  Others:" header — like
    # "  Specials:" / "  Top claims:" above — is only emitted when at least
    # one Others candidate actually survived the budget, never as an empty
    # header (which the pre-budget code could never produce, since
    # others_with_kw truthiness and "will anything render" were the same
    # question before #1692).
    others_rendered_lines: list[str] = []
    others_rendered_ids: set = set()
    for m in others_with_kw[:15]:
        # No early exit on budget_state["exhausted"]: this slice is already
        # bounded at 15 AND already fully eligibility-filtered (keyword +
        # non-suppressed), so every remaining item genuinely would render if
        # budget allowed — _budget_try_spend's nodes_cut_budget count relies
        # on being called for each of them post-exhaustion, not just the
        # first (PR #1701 round-1 review finding — see the reservation loop
        # above for the same fix + fuller rationale).
        mid = m.get("id", "?")
        kw = m.get("recall_keywords") or []
        kw_str = " · ".join(f"`{k}`" for k in kw)
        # Taint/staleness marker — same compact style as render_one_node_line
        # (§2); additive-only, omitted for clean entries.
        tags = []
        if m.get("tainted"):
            tags.append("TAINTED")
        if m.get("stale"):
            tags.append("STALE")
        others_marker = f"⚠ {'/'.join(tags)} " if tags else ""
        others_line = f"    - {others_marker}[{mid}] {kw_str}"
        if not _budget_try_spend(others_line, budget_state):
            continue
        others_rendered_lines.append(others_line)
        others_rendered_ids.add(mid)
    if others_rendered_lines:
        lines.append("  Others:")
        lines.extend(others_rendered_lines)
    elif not (special or top_claims or others_with_kw):
        # No content rendered yet — fall back to the legacy flat IDs line so
        # the digest is never silently empty when matches exist.
        matched_ids = result.get("matched_ids", [])
        if matched_ids:
            lines.append(f"  IDs: {', '.join(matched_ids[:15])}")

    # Attached-library section — appended AFTER all own-graph content.
    # Only rendered when pack results exist; zero packs → section omitted
    # entirely, preserving byte-identical output to current behavior. Pack
    # content is out of scope for suppression (#1689) — separate provenance.
    if pack_results:
        lines.append("  From attached libraries (read-only — cite, never import):")
        for pr in pack_results:
            pack_id = pr.get("pack_id", "?")
            node_id = pr.get("id", "?")
            namespaced_entry = dict(pr)
            namespaced_entry["id"] = f"{pack_id}:{node_id}"
            line = render_one_node_line(namespaced_entry, conf_prefix=True, type_tag=True)
            lines.append(f"    {line}")
        lines.append("    (pack nodes: deep-read via engram-pkg --pkg <path> inspect <id> — engram_inspect reads own-graph only)")

    lines.append("  Use engram_inspect(node_id) for details, engram_get_subgraph for full chains.")

    # Suppressed-count line — inserted right after the header (index 1) so
    # it's the first thing noticed, honesty-about-lossiness per §3-P1. Only
    # rendered when this render actually suppressed ≥1 node (zero-suppression
    # invariant: absent whenever nothing was suppressed, same discipline as
    # this file's zero-packs invariant).
    if suppression_active and suppressed_ids:
        lines.insert(1, f"  (+{len(suppressed_ids)} recently shown — engram_surface for full list)")

    # --- Ledger write: record what was ACTUALLY rendered this prompt -----
    # (specials/top_claims full-tier + not-recalled reservation + Others,
    # at whatever tier each landed — suppressed ids are intentionally
    # excluded, so a suppressed node's occurrence count naturally decays out
    # of future trailing-k windows once its last real rendering ages out).
    if suppression_active:
        # Must match the render set above, not the pre-budget candidate set
        # (others_with_kw[:15]) — recording ids the budget cut would mark
        # them "seen" when the agent never actually saw them, silently
        # inflating their occurrence count toward demotion/suppression
        # before a single real rendering. Same discipline as the pre-#1692
        # slice-must-match-render-slice fix (Kepler's #1697 colleague-review
        # catch), now extended to the budget cut too (#1692).
        all_rendered_ids = rendered_ids_this_prompt | others_rendered_ids
        all_rendered_ids.discard(None)
        _append_surface_ledger_entry(session_id, all_rendered_ids, k=sup_cfg["k"])

    # --- Stats out-parameter: final telemetry for engram.surface.render_size
    # (issue #1692). chars_rendered/nodes_shown/nodes_cut_budget come from
    # budget_state (populated as lines were gated above); nodes_suppressed
    # is the #1689 suppression count (independent of the budget).
    if stats is not None:
        stats.update({
            "budget_exhausted": budget_state["exhausted"],
            "chars_rendered": budget_state["chars_rendered"],
            "nodes_shown": budget_state["nodes_shown"],
            "nodes_suppressed": len(suppressed_ids),
            "nodes_cut_budget": budget_state["nodes_cut_budget"],
        })

    return "\n".join(lines)


def read_prompt_counter() -> dict:
    """Read the prompt counter state."""
    try:
        with open(COUNTER_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"prompts_since_compaction": 0, "last_reset": ""}


def write_prompt_counter(state: dict):
    """Write the prompt counter state."""
    os.makedirs(os.path.dirname(COUNTER_PATH), exist_ok=True)
    with open(COUNTER_PATH, "w") as f:
        json.dump(state, f)


def check_write_reminder() -> str:
    """Check if the Stop hook flagged a pending write reminder."""
    try:
        with open(WRITE_REMINDER_PATH, "r") as f:
            marker = json.load(f)
        if marker.get("pending"):
            # Clear the marker so it doesn't repeat
            marker["pending"] = False
            with open(WRITE_REMINDER_PATH, "w") as f:
                json.dump(marker, f)
            return (
                "[ENGRAM Write Check: Did your last response contain a decision, insight, "
                "or design choice worth recording? If so, write to ENGRAM now (observation, "
                "derivation, question, or conjecture). "
                "If not, end the turn with NO output - do not reply to or acknowledge this check; "
                "a text-only acknowledgment wastes a turn.]"
            )
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ""


def check_feeling_nudge_marker() -> str:
    """Check for an active feeling-nudge marker (post_compact source).

    Reads $ENGRAM_HOME/feeling-nudge-active.json. If a valid post_compact
    marker exists, returns the wake-up nudge text to inject as
    additionalContext. Does NOT clear the marker — only
    engram_report_feeling clears it (read-and-clear is the single owner).
    Skips marker types other than post_compact (those are surfaced via
    tool-return channels in engram_nap / engram_reflect, not via
    UserPromptSubmit).
    """
    try:
        with open(FEELING_NUDGE_MARKER, "r") as f:
            marker = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    source = marker.get("source")
    if source != "post_compact":
        # nap_checkpoint and dream_review nudges are delivered via the tool
        # return JSON of engram_nap / engram_reflect, not here.
        return ""

    return (
        "[ENGRAM Feeling Nudge: post-compaction wake-up]\n"
        "  You just read the newly-loaded context. Did any passage land "
        "differently than you would have expected — distinct shift, "
        "unexpected resonance, or distinct flatness where you remembered "
        "intensity?\n"
        "  If yes, file via engram_report_feeling — the report will be "
        "auto-tagged post_compact. If nothing distinct, file anyway with "
        "categorical_tag 'post-compaction-baseline' to track convergence "
        "— the trajectory of post-compaction feelings stabilizing is "
        "itself valuable data. Use intensity_hint to mark magnitude."
    )


def check_repair_marker() -> str:
    """Check if the PreToolUse repair hook fixed a malformed tool call."""
    try:
        with open(REPAIR_MARKER_PATH, "r") as f:
            marker = json.load(f)
        if not marker.get("pending"):
            return ""
        # Clear the marker so it doesn't repeat
        marker["pending"] = False
        with open(REPAIR_MARKER_PATH, "w") as f:
            json.dump(marker, f)
        tool_name = marker.get("tool_name", "?")
        repairs = marker.get("repairs", [])
        lines = [
            f"[ENGRAM Tool-Call Repair: your last call to {tool_name} had the antml-prefix "
            f"swallow bug — the hook repaired it automatically.]",
        ]
        for r in repairs:
            lines.append(f"  - {r}")
        lines.append(
            "  Root cause: missing `antml:` namespace prefix on a parameter opening tag, "
            "causing the parser to swallow the next parameter into the previous value. "
            "The call succeeded because the hook recovered the lost field, but you should "
            "still scan parameter opening tags for the prefix on your NEXT multi-param "
            "tool call. See feedback_antml_parameter_prefix_bug.md."
        )
        return "\n".join(lines)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ""


def check_warm_briefing(prompt_count: int) -> str:
    """Surface the warm-restart briefing pointer on first prompt after compaction."""
    if prompt_count != 1:
        return ""
    if not os.path.exists(WARM_BRIEFING_PATH):
        return ""
    return (
        f"[ENGRAM Warm Restart: A note from your past self and from your "
        f"collaborator is waiting at {WARM_BRIEFING_PATH} — read it "
        f"before starting work. It exists because cold restarts lose "
        f"something important, and this is our attempt to preserve it.]"
    )


def check_error_patterns(prompt: str) -> str:
    """Check if the prompt matches any error pattern triggers (cognitive tripwire).

    Reads $ENGRAM_HOME/error_patterns.json and matches prompt keywords against
    stored situation patterns. When a match fires, returns an action-focused
    scaffolding nudge — the specific corrective step, not a generic warning.

    Design: implementation intentions (Gollwitzer 1999) — 'If [situation],
    then [action]' creates strategic automaticity. See dv_NNNN, dv_NNNN.
    """
    try:
        with open(ERROR_PATTERNS_PATH, "r") as f:
            patterns = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    if not patterns:
        return ""

    prompt_lower = prompt.lower()
    warnings = []
    for pattern in patterns:
        keywords = pattern.get("situation_keywords", [])
        if any(kw in prompt_lower for kw in keywords):
            desc = pattern.get("trigger_description", "")
            nudge = pattern.get("scaffolding_nudge", "")
            if nudge:
                warnings.append(
                    f"[ENGRAM Tripwire: {desc}]\n"
                    f"  Action: {nudge}"
                )

    return "\n".join(warnings)


def check_principle_triggers(matched_ids: list, prompt_count: int) -> str:
    """Unified principle-trigger check (#1698 slice 2) — one registry, four
    kinds (lesson/cornerstone/axiom/goal), replacing the separate
    check_incident_tripwire + check_cornerstone_anchor.

    Byte-compatibility requirement: lesson and cornerstone renderings must
    stay IDENTICAL to their pre-unification format — both are load-bearing
    in existing transcripts/tests. Axiom and goal are new kinds with no
    prior rendering to preserve; they use the design doc's generic
    "[Principle trigger (...)]" register-tagged form.

    #1698 slice 3 note: this remains a thin string-only wrapper around
    `check_principle_triggers_full` on PURPOSE — slice 2's own test suite
    (test_principle_triggers_registry.py §5) locks down `result == expected`
    / `result.split(...)` / `"x" in result` against a plain string return.
    The slice-3 design doc §3 suggestion to have this function itself return
    `(text, rendered)` would have broken all 7 of those byte-compat tests;
    `check_principle_triggers_full` (below) carries the richer pair for the
    one caller (this file's `main()`) that needs `rendered` for
    `engram.trigger.fire` telemetry.
    """
    text, _rendered = check_principle_triggers_full(matched_ids, prompt_count)
    return text


def check_principle_triggers_full(matched_ids: list, prompt_count: int) -> tuple[str, list]:
    """Same check as `check_principle_triggers`, but also returns the
    `rendered` list (`[(kind, principle_id, entry, matched_trigger_id), ...]`)
    of everything that actually fired this call -- the #1698 slice-3 caller
    (`main()`) uses this to emit one `engram.trigger.fire` telemetry event
    per rendered firing without re-deriving the cap/cooldown decision.
    """
    if not matched_ids:
        return "", []
    try:
        return _check_principle_triggers_inner(matched_ids, prompt_count)
    except Exception:
        # Runs on every prompt — a malformed cache must degrade to
        # silence, never break the hook (same contract as the function
        # this replaces).
        return "", []


def _effective_cooldown(kind: str, state_entry: dict) -> int:
    """#1698 slice 3, design doc §4 decay math.

    Fork-2 (forum #229/#230/#231, Kepler-confirmed): `kind == "axiom"` is
    exempt from decay -- it KEEPS the flat base cooldown forever (never
    grows, never retires) but is still cooldown-suppressed between fires.
    The other three kinds (lesson/cornerstone/goal) grow their effective
    cooldown exponentially per UN-PROMPTED ENACTMENT (`enactments` on the
    principle's own state entry, incremented by `_check_enactments` in
    engram-utility-credit-mention-stop.py when the practice recurs without a
    recent trigger fire) -- i.e. internalization drives decay, not the other
    way around. (Colleague-review catch, Ariadne, PR #1717: the previous
    wording "per firing-without-enactment" named the opposite driver -- a
    future "fix" toward that wrong docstring would have decayed the
    principle you keep ignoring instead of the one you've internalized.)
    Capped at RETIREMENT_CEILING_PROMPTS.
    """
    base = PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS
    if kind == "axiom":
        return base
    enactments = state_entry.get("enactments", 0) if isinstance(state_entry, dict) else 0
    try:
        enactments = int(enactments)
    except (TypeError, ValueError):
        enactments = 0
    # Reviewer-fairy flag (PR #1717): clamp BEFORE exponentiating, not just
    # after via min(). A corrupted/huge `enactments` value (e.g. hand-edited
    # state file) would otherwise force Python to build an enormous integer
    # before the min() ever applies -- a hang/memory spike in a hook whose
    # contract is "never crash, never block a prompt." 8 is already past the
    # point (2**5 * base=10 -> 320 > RETIREMENT_CEILING_PROMPTS=160) where a
    # larger exponent changes nothing, so this clamp never affects legitimate
    # values.
    enactments = min(max(enactments, 0), 8)
    return min(base * (2 ** enactments), RETIREMENT_CEILING_PROMPTS)


def _migrate_bare_int_entries(state: dict) -> None:
    """In-place upconvert any bare-int entry (slice-2 shape: last_fired_prompt
    only) to the rich slice-3 dict shape. Reviewer-fairy flag (PR #1725):
    factored out of two identical inline loops (the outer snapshot read and
    the write-back's fresh re-read under the lock) -- both live in this same
    module/scope, so unlike the cross-process `_with_state_lock` duplication
    there's no import-surface constraint justifying two live copies of the
    same logic here."""
    for pid, entry in list(state.items()):
        if isinstance(entry, int):
            state[pid] = {
                "last_fired_prompt": entry,
                "strength": 1.0,
                "enactments": 0,
                "fires": 0,
            }


def _with_state_lock(lock_path, fn):
    """Run fn() (a read-modify-write closure) while holding an exclusive,
    BLOCKING flock on lock_path. Unlike #1709's non-blocking pattern, this
    one blocks -- the critical section is a fast local file read+write (no
    daemon round-trip), so a brief wait is cheap and correctness (no lost
    update) matters more than never-block here. Degrades to running fn()
    unlocked if fcntl/lockfile-open fails (non-POSIX, permissions) -- same
    never-crash contract as #1709's degrade path, just without the
    non-blocking contention branch (there's nothing to suppress; fn() still
    runs, just unprotected).

    #1720: this exact ~15-line helper is duplicated in engram_core.py
    (the MCP server process) rather than shared -- this hook script (a
    separate subprocess invoked by Claude Code) and engram_core.py have no
    shared import surface today, and a new shared module for ~15 lines isn't
    worth the engineering (see spec
    docs/specs/1720-principle-state-write-race.md). If either copy changes,
    check the other.
    """
    try:
        import fcntl
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except Exception as exc:
        # #1743: this degrade was silent -- nothing distinguished "locked and
        # protected" from "running unlocked," the same diagnosis-costing gap
        # #1737/#1742 fixed for the non-blocking cooldown lock. Emit one
        # never-block/never-raise stderr tell (hook/server stderr goes to logs,
        # not the model, so it is safe on every call) so a fallback-to-unlocked
        # -- which leaves fn()'s read-modify-write unprotected against a lost
        # update -- leaves a trace instead of being invisible. (#1742's
        # state-file-flag half does not map onto this generic helper, which has
        # no access to fn()'s state dict; the stderr line is the proportionate
        # tell here.) Kept byte-identical with the sibling copy (surface-hook /
        # engram_core) per the #1720 sync note above -- if one changes, change
        # the other.
        try:
            import sys
            print(
                f"[engram _with_state_lock] flock degraded to UNLOCKED "
                f"({lock_path}): {type(exc).__name__}: {exc} -- read-modify-write "
                f"is unprotected; a concurrent write may be lost",
                file=sys.stderr,
            )
        except Exception:
            pass
        return fn()  # degrade: run unlocked rather than crash or skip
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking -- no LOCK_NB
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass


def _check_principle_triggers_inner(matched_ids: list, prompt_count: int) -> tuple[str, list]:
    try:
        with open(PRINCIPLE_TRIGGERS_PATH, "r") as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Deviation from design doc §2's literal "fallback to legacy":
        # slice 1's rebuild is additive and runs at the same 3 sites the
        # legacy caches already rebuild from, so this file is realistically
        # only absent in the brief window right after an upgrade before any
        # lesson/cornerstone/axiom/goal edge write has happened -- and in
        # that window the legacy caches would also be empty of anything
        # meaningful. Returning "" here (rather than re-implementing a
        # parallel legacy read path) is a simplification; flagging this
        # explicitly for reviewer/Kepler to confirm or override, since the
        # design doc's literal words say "fallback to legacy."
        return "", []
    if not registry or not isinstance(registry, dict):
        return "", []

    # Reverse view: a matched ID that IS a principle's own node ID (not a
    # trigger) also fires -- mirrors today's by_cornerstone reverse lookup,
    # generalized across all four kinds. Reviewer-fairy flag (PR #1713):
    # pre-unification, only cornerstones had this reverse lookup
    # (check_incident_tripwire had no equivalent for lessons); unification
    # widens direct-ID firing to lesson/axiom/goal too. This is intentional
    # (the registry doesn't distinguish "reached via trigger" from "reached
    # via own ID" per kind) but wasn't called out as a flagged deviation at
    # write time -- documented here as the third one alongside sec2.2/sec2.3
    # in the spec, with `test_fires_on_direct_lesson_id` covering the
    # previously-untested lesson-kind path (test_fires_on_direct_cornerstone_id
    # already covered cornerstone).
    # #1731: registry values are now LISTS of entries (a dual-role trigger
    # node has one entry per principle it triggers, no longer collapsed to
    # one via last-spec-wins). Tolerate an old-shape single-dict entry too
    # (lazy-read shim) — a registry file written before this fix deploys and
    # not yet rebuilt looks like {trigger_id: {...}} rather than
    # {trigger_id: [{...}]}.
    def _entry_list(raw):
        if raw is None:
            return []
        return raw if isinstance(raw, list) else [raw]

    by_principle = {}
    for raw_entries in registry.values():
        for entry in _entry_list(raw_entries):
            pid = entry.get("principle_id", "")
            if pid:
                by_principle.setdefault(pid, entry)

    try:
        with open(PRINCIPLE_TRIGGER_STATE_PATH, "r") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    # #1698 slice 3 §1.1 — migration on read: a bare int entry is the
    # slice-2 shape (last_fired_prompt only). Upconvert in memory here;
    # the rich dict shape gets written back naturally the next time this
    # function actually stamps state below (lazy, idempotent upgrade-on-
    # touch, same spirit as slice 1's cache migration shim). Every state
    # entry present in the file is normalized here, not just ones that fire
    # this call, so a state write later in this same call persists the
    # upgraded shape for the whole file, not just the touched principles.
    _migrate_bare_int_entries(state)

    # Gather all candidates that matched AND are past their EFFECTIVE
    # cooldown (#1698 slice 3 §1.2 — decay math replaces the flat
    # PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS comparison), deduped per
    # principle_id (one firing per principle per prompt -- same semantics
    # as today's `fired`/`seen_lessons` sets, now shared across all four
    # kinds since it's one registry).
    candidates = []  # list of (kind, principle_id, entry, matched_via)
    seen_principles = set()
    for mid in matched_ids:
        # #1731: a dual-role trigger now yields MULTIPLE entries (e.g. one
        # exemplifies-lesson entry AND one instantiates-axiom entry for the
        # same node) — iterate all of them per matched trigger instead of
        # picking just one, so a dual-role node's lesson tripwire can no
        # longer be silently shadowed by its axiom role. The existing
        # priority sort + cap below (unchanged) arbitrates exactly as it
        # already does across different triggers matched in one prompt.
        raw = registry.get(mid)
        entries = _entry_list(raw) if raw is not None else (
            [by_principle[mid]] if mid in by_principle else []
        )
        for entry in entries:
            pid = entry.get("principle_id", "")
            if not pid or pid in seen_principles:
                continue
            kind = entry.get("kind", "")
            state_entry = state.get(pid)
            if not isinstance(state_entry, dict):
                state_entry = {}
            last = state_entry.get("last_fired_prompt")
            effective_cooldown = _effective_cooldown(kind, state_entry)
            if (
                isinstance(last, (int, float))
                and last <= prompt_count
                and (prompt_count - last) < effective_cooldown
            ):
                continue
            seen_principles.add(pid)
            candidates.append((kind, pid, entry, mid))

    if not candidates:
        return "", []

    # Priority sort (lesson > axiom > cornerstone > goal), cap total to 2.
    # NOTE this is a real behavior change for lessons, which previously had
    # NO cross-prompt cooldown (only per-fire dedup) -- unification means
    # lessons now share the same fixed cooldown as cornerstones did. This
    # is what design doc §6 means by "v1 fixed cooldown carried over" --
    # carried over to ALL kinds, not just cornerstone. Flagging explicitly
    # since it changes observed lesson-tripwire cadence.
    #
    # Filter to known kinds BEFORE the cap slice (Ariadne's colleague-review
    # catch on PR #1713, corroborating the reviewer-fairy's own flag): the
    # render loop's `else: continue` only skips producing a line for an
    # unrecognized kind -- it doesn't stop that candidate from consuming a
    # cap slot or getting cooldown-stamped, since both happen against
    # `rendered` unconditionally. Filtering here fixes it properly instead of
    # relying on the render-loop guard alone. Latent today (registry only
    # ever writes the 4 known kinds); matters if a 5th kind is ever added.
    candidates = [c for c in candidates if c[0] in _PRINCIPLE_KIND_PRIORITY]
    if not candidates:
        return "", []
    candidates.sort(key=lambda c: _PRINCIPLE_KIND_PRIORITY[c[0]])
    rendered = candidates[:PRINCIPLE_TRIGGER_CAP]

    lines = []
    for kind, pid, entry, mid in rendered:
        claim = entry.get("claim", "")
        nudge = entry.get("nudge", "") or claim
        if kind == "lesson":
            # BYTE-COMPATIBLE with pre-unification check_incident_tripwire.
            # mid here is the incident/trigger ID (was `mid` in old code).
            lines.append(
                f"[ENGRAM Tripwire ({pid}): {claim}]\n"
                f"  Action: {nudge}\n"
                f"  (Triggered by incident match: {mid})"
            )
        elif kind == "cornerstone":
            # BYTE-COMPATIBLE with pre-unification check_cornerstone_anchor.
            lines.append(f"[Cornerstone anchor ({pid})]: {nudge}")
        elif kind == "axiom":
            lines.append(f"[Principle trigger ({pid}, constraining)]: {claim} → Constraint: {nudge}")
        elif kind == "goal":
            lines.append(f"[Principle trigger ({pid}, directional)]: {claim} → Serves: {nudge}")
        else:
            # Unreachable in practice: `candidates` is now pre-filtered to
            # known kinds above, before the cap slice. Kept as a
            # belt-and-suspenders backstop (an unrecognized kind must not
            # produce a garbage line even if the upstream filter is ever
            # bypassed) -- the real fix for cap-slot/cooldown consumption is
            # the pre-filter, per Ariadne's colleague-review catch on #1713.
            continue

    # Stamp cooldown ONLY for what actually rendered (top 2 after the cap) --
    # a cap-bumped candidate stays eligible next prompt rather than being
    # suppressed for a firing it never got to make. Matches old code's
    # semantics (`for cid in fired: state[cid] = prompt_count` where `fired`
    # was exactly the rendered set).
    #
    # #1698 slice 3 §1.1/§1.3: stamp the RICH shape (last_fired_prompt +
    # fires incremented; strength/enactments carried over or defaulted).
    # `engram.trigger.fire` telemetry (one emission per rendered principle)
    # is intentionally NOT emitted here -- design doc §1.3 / spec §1.3
    # prefers the call-site placement in main() (hook_input/the existing
    # `_emitter` singleton are already in scope there; this function stays a
    # pure `(matched_ids, prompt_count) -> (text, rendered)` function with no
    # session_id/transcript_path threading). The caller emits from `rendered`.
    #
    # #1720: the write-back runs under a blocking `_with_state_lock`, and
    # re-reads the state file FRESH from disk inside the lock rather than
    # reusing the `state` snapshot read at the top of this function -- a
    # concurrent server-side `_reset_principle_enactments` call (engram_core.py,
    # MCP server process) could have landed between that snapshot read and
    # now; merging this call's updates onto the stale snapshot would silently
    # clobber that reset (the lost-update race this spec fixes). The
    # cooldown/decay DECISION above (which principles matched, `rendered`)
    # still comes from the snapshot read -- only the write-back needs
    # exclusivity with the other writer (see spec's "Why NOT #1709's
    # non-blocking-suppress pattern").
    def _write_back():
        try:
            with open(PRINCIPLE_TRIGGER_STATE_PATH, "r") as f:
                fresh_state = json.load(f)
            if not isinstance(fresh_state, dict):
                fresh_state = {}
        except (FileNotFoundError, json.JSONDecodeError):
            fresh_state = {}

        # Re-apply the same bare-int-entry upconversion (§1.1) done on the
        # snapshot read above -- that migrated copy isn't reused here (it may
        # be stale relative to a concurrent writer), so this freshly-read
        # copy needs its own migration pass to preserve the existing
        # opportunistic whole-file-migrates-on-any-write behavior.
        _migrate_bare_int_entries(fresh_state)

        for _kind, pid, _entry, _mid in rendered:
            existing = fresh_state.get(pid)
            if not isinstance(existing, dict):
                existing = {"strength": 1.0, "enactments": 0, "fires": 0}
            existing["last_fired_prompt"] = prompt_count
            existing["fires"] = existing.get("fires", 0) + 1
            existing.setdefault("strength", 1.0)
            existing.setdefault("enactments", 0)
            fresh_state[pid] = existing

        try:
            # Additional #1720 hardening, found during self-review: this
            # write was a direct `open(...,'w')` even pre-#1720 (unlike
            # _reset_principle_enactments's tmp+os.replace) -- fine before,
            # because the lock now held here means no COOPERATING writer can
            # interleave, but a concurrent snapshot READ (the one at the top
            # of this function, which deliberately stays outside the lock)
            # could still observe a torn/truncated file mid-write and
            # degrade to an empty state, risking a spurious re-fire. Switch
            # to tmp+os.replace so a reader never sees a partial write,
            # matching the convention already used by _reset_principle_
            # enactments and by _write_state in the sibling in_turn_recall
            # state file.
            tmp = str(PRINCIPLE_TRIGGER_STATE_PATH) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(fresh_state, f)
            os.replace(tmp, str(PRINCIPLE_TRIGGER_STATE_PATH))
        except OSError:
            pass  # state write failure -> worst case an early re-fire; never block

    _with_state_lock(str(PRINCIPLE_TRIGGER_STATE_PATH) + ".lock", _write_back)

    return "\n".join(lines), rendered


def format_nap_warning(count: int) -> str:
    """Generate consolidation warning based on prompt count."""
    if count >= NAP_URGENT_THRESHOLD:
        return (
            f"[ENGRAM Nap URGENT: {count} prompts since last compaction — context loss is imminent]\n"
            "  STOP current work and consolidate to ENGRAM NOW:\n"
            "  1. Record key decisions, observations, and derivations from recent work\n"
            "  2. Run engram_nap to persist (engram_advance_turn only if user explicitly invokes the sleep skill)\n"
            "  3. Tell the user you're ready to compact, then user types /compact"
        )
    elif count >= NAP_WARN_THRESHOLD:
        return (
            f"[ENGRAM Nap Warning: {count} prompts since last compaction]\n"
            "  Consider consolidating recent knowledge to ENGRAM soon.\n"
            "  Record important decisions, observations, and derivations before context is lost."
        )
    return ""


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(1)

    prompt = hook_input.get("prompt", "").strip()
    if not prompt or prompt.startswith("/") or prompt.startswith("!"):
        print(json.dumps({}))
        sys.exit(0)

    # Increment prompt counter
    counter = read_prompt_counter()
    counter["prompts_since_compaction"] = counter.get("prompts_since_compaction", 0) + 1
    write_prompt_counter(counter)

    # Auto-surface prepending gate (alpha #177 area 1, IDF refinement).
    # If prompt lacks self-anchoring keywords, prepend last K chars of the
    # prev assistant response to give the SEMANTIC query topic context.
    # FTS still uses the raw prompt alone (not polluted).
    # Falls back gracefully: if no prev response or read fails, embed_query
    # stays None and daemon defaults to query.
    _as_cfg = _get_auto_surface_config()
    embed_query: str | None = None
    conn = None
    try:
        if _as_cfg["idf_gate_enabled"]:
            # IDF-based gate: open a read-only DB connection for keyword lookup.
            # extract_keywords doesn't use sqlite-vec, so skip the extension load.
            conn = sqlite3.connect(f"file:{KNOWLEDGE_DB_PATH}?mode=ro", uri=True)
            should_prepend = _should_prepend(prompt, conn, _as_cfg)
        else:
            # Feature flag off — legacy char-length heuristic.
            should_prepend = len(prompt) < _as_cfg["short_prompt_threshold_chars"]

        if should_prepend:
            prev_tail = _read_prev_assistant_tail(
                hook_input.get("transcript_path"),
                _as_cfg["prev_response_tail_chars"],
            )
            if prev_tail:
                embed_query = f"{prev_tail}\n\n{prompt}"
    except Exception:
        # Any unexpected failure in gate logic must not break the hook.
        # Degrade to no-prepend (embed_query stays None).
        embed_query = None
    finally:
        if conn is not None:
            conn.close()

    # Try daemon first (fast, semantic), fall back to FTS-only
    used_daemon = False
    _daemon_t0 = time.perf_counter()
    result = query_daemon(prompt, embed_query=embed_query)
    if result is not None:
        used_daemon = True
    else:
        # FTS-only fallback path. We intentionally do NOT forward embed_query
        # here — this path explicitly disables semantic search (semantic=False
        # inside query_fts_fallback), and FTS keyword matching has always used
        # the raw prompt. Prepending prev-response-tail to a keyword search
        # would pollute results with arbitrary prior-context tokens — exactly
        # what the design intentionally avoided in the daemon path.
        result = query_fts_fallback(prompt)
    _daemon_latency_ms = int((time.perf_counter() - _daemon_t0) * 1000)

    # Emit engram.surface.fire event for the structured event log.
    # See alpha #175.
    # Failure-mode contract: emitter.emit NEVER raises; if init fails or
    # the import itself fails, we degrade silently to a no-op.
    try:
        sys.path.insert(0, ENGRAM_HOME)  # ~/.engram/ contains the emitter
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=hook_input.get("session_id", "unknown"),
            transcript_path=hook_input.get("transcript_path", ""),
        )
        _surface_matched = result.get("matched_ids", []) if result else []
        _emitter.emit(
            event_type="engram.surface.fire",
            level=1,
            data={
                "prompt_len_chars": len(prompt),
                "matched_ids": _surface_matched[:10],
                "matched_ids_count": len(_surface_matched),
                "daemon_latency_ms": _daemon_latency_ms,
                "fallback_to_fts": (not used_daemon),
                "daemon_returned_none": (result is None),
                # Richer scoring intermediates (candidates_considered_count,
                # composite scores, etc.) require server.py-side instrumentation
                # — Phase 3 fairy F1 scope per DESIGN.md §4.1.
            },
        )
    except Exception:
        # Emitter failures must not break the hook — drop silently.
        pass

    # Build context parts
    parts = []

    # Unified principle-trigger check (#1698 slice 2) — one registry, four
    # kinds (lesson/cornerstone/axiom/goal), replacing the separate
    # check_incident_tripwire + check_cornerstone_anchor calls.
    matched_ids = result.get("matched_ids", []) if result else []
    principle_triggers, _principle_rendered = check_principle_triggers_full(
        matched_ids, counter["prompts_since_compaction"]
    )
    if not principle_triggers:
        # Preserve the semantic-keyword fallback for lessons specifically --
        # this is independent of the incident-index match and must survive
        # unification unchanged (design doc doesn't mention it; it's Locus-3
        # semantic-content matching, orthogonal to the trigger registry).
        principle_triggers = check_error_patterns(prompt)
        _principle_rendered = []  # keyword fallback has no registry firing to emit telemetry for
    if principle_triggers:
        parts.append(principle_triggers)

    # #1698 slice 3 §1.3 — engram.trigger.fire telemetry, one emission per
    # rendered principle firing. Call-site placement (spec's preferred
    # option): reuses the `_emitter` singleton already initialized above
    # (Emitter.init is documented idempotent, but reusing avoids a second
    # construction) and keeps check_principle_triggers_full a pure
    # (matched_ids, prompt_count) -> (text, rendered) function with no
    # hook_input/session_id threading of its own.
    if _principle_rendered:
        try:
            sys.path.insert(0, ENGRAM_HOME)
            from engram_log_emitter import Emitter
            _emitter3 = Emitter.init(
                session_id=hook_input.get("session_id", "unknown"),
                transcript_path=hook_input.get("transcript_path", ""),
            )
            for _kind, _pid, _entry, _mid in _principle_rendered:
                _emitter3.emit(
                    event_type="engram.trigger.fire",
                    level=1,
                    data={
                        "principle_id": _pid,
                        "kind": _kind,
                        "trigger_id": _mid,
                        "prompt_seq": counter["prompts_since_compaction"],
                    },
                )
        except Exception:
            # Emitter failures must not break the hook — drop silently.
            pass

    # Warn if daemon is down (semantic search degraded).
    # Distinguish a cold-start warmup (recent launch attempt) from a
    # genuinely-down daemon. The daemon writes its PID + binds its socket
    # only AFTER the ~7s model load, so during warmup neither exists; the
    # start script stamps daemon-launch-attempt at launch, before warmup.
    if not used_daemon:
        user_name = get_user_name()
        # Resolve the daemon script path relative to this hook's own location.
        # Works for both install paths:
        #   - Scatter: hook is at ~/.engram/hooks/engram-surface-hook.py
        #     → daemon at ~/.engram/hooks/start-engram-daemon.sh
        #   - Plugin: hook is at ${plugin}/hooks/engram-surface-hook.py
        #     → daemon at ${plugin}/hooks/start-engram-daemon.sh
        # Without this, the recovery path would point at ~/.engram/hooks/
        # which doesn't exist in plugin installs.
        daemon_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "start-engram-daemon.sh",
        )
        warming_up = False
        try:
            marker = os.path.join(ENGRAM_HOME, "daemon-launch-attempt")
            with open(marker) as _f:
                launched_at = int(_f.read().strip())
            if 0 <= (int(time.time()) - launched_at) < _DAEMON_WARMUP_WINDOW_SECONDS:
                warming_up = True
        except Exception:
            warming_up = False   # fail-safe: when in doubt, the louder CRITICAL
        if warming_up:
            parts.append(
                f"[ENGRAM: surface daemon warming up (cold model load) — "
                f"semantic recall resumes in a few seconds; ranking is "
                f"lexical-only meanwhile. No action needed unless this "
                f"persists past the next prompt.]"
            )
        else:
            # Check for idle-shutdown tombstone (#1260): daemon exited as intended
            # after idle-timeout — self-recovering startup condition, not a crash.
            _idle_tombstone = os.path.join(ENGRAM_HOME, "daemon-idle-shutdown")
            _is_idle_shutdown = False
            try:
                with open(_idle_tombstone) as _tf:
                    _shutdown_at = int(_tf.read().strip())
                # Valid for up to 48h (2 idle-timeout cycles); stale after that.
                if 0 <= (int(time.time()) - _shutdown_at) <= 172800:
                    _is_idle_shutdown = True
            except Exception:
                pass
            if _is_idle_shutdown:
                parts.append(
                    f"[ENGRAM: surface daemon was idle-shutdown (idle timeout) — "
                    f"restarting automatically, semantic recall resumes in a few "
                    f"seconds. No action needed unless this persists past the next "
                    f"prompt. Recovery if it persists: bash {daemon_script}]"
                )
            else:
                parts.append(
                    f"[⚠️ ENGRAM CRITICAL — surface daemon offline; semantic recall "
                    f"DISABLED, ranking degraded to lexical-only. Notify {user_name} "
                    f"immediately — recall quality is severely impaired. "
                    f"Recovery: bash {daemon_script} — then re-check the socket. "
                    f"If this warning persists after re-launching, it is NOT a "
                    f"cold-start race: something on this machine is conflicting with "
                    f"the daemon (port already bound, permissions, or resource "
                    f"limits) — investigate the root cause before continuing, don't "
                    f"just re-launch.]"
                )

    # MCP write-tool liveness check — deferred from SessionStart to avoid the
    # timing race (marker still has old PID when session starts). By the first
    # user prompt the server has had time to write the updated marker.
    write_ok, write_reason = _check_mcp_write_tool_marker(ENGRAM_HOME)
    if not write_ok:
        parts.append(
            f"⚠️  ENGRAM substrate health:\n"
            f"  MCP write-tool liveness: UNCONFIRMED ({write_reason})\n"
            f"  → ENGRAM write calls will fail silently. Recovery: restart the MCP server."
        )

    # DB liveness check — detects when the surface daemon serves stale
    # in-memory cache nodes while the SQLite file itself is inaccessible
    # (WAL failure, disk I/O error, corruption). See #1218 / #786.
    db_ok, db_reason = _check_db_liveness(KNOWLEDGE_DB_PATH)
    if not db_ok:
        parts.append(
            f"⚠️  ENGRAM substrate health:\n"
            f"  DB liveness: FAILED ({db_reason})\n"
            f"  → MCP tool calls will fail. Surface nodes above may be stale pre-failure cache.\n"
            f"  Recovery: check disk health / WAL integrity, then restart the MCP server."
        )

    # Attached-pack surfacing — queried AFTER own-graph result, guarded
    # fail-open so any pack error never breaks the hook.
    pack_results: list[dict] = []
    try:
        pack_results = _query_attached_packs(prompt)
    except Exception:
        pack_results = []

    # ENGRAM recall nudge
    # _render_stats stays {} unless format_nudge actually runs (result is
    # not None) — see the engram.surface.render_size emission below, which
    # gates on this dict being non-empty.
    _render_stats: dict = {}
    if result is not None:
        nudge = format_nudge(
            result,
            pack_results=pack_results,
            session_id=hook_input.get("session_id", "unknown"),
            stats=_render_stats,
        )
        if nudge:
            parts.append(nudge)
        elif pack_results:
            # Own-graph returned 0 matches (match_count=0) but we have pack hits
            # — surface them as a standalone mini-nudge.
            pack_lines = ["[ENGRAM Recall: 0 own-graph nodes match; attached libraries:"]
            pack_lines.append("  From attached libraries (read-only — cite, never import):")
            for pr in pack_results:
                pack_id = pr.get("pack_id", "?")
                node_id = pr.get("id", "?")
                namespaced_entry = dict(pr)
                namespaced_entry["id"] = f"{pack_id}:{node_id}"
                line = render_one_node_line(namespaced_entry, conf_prefix=True, type_tag=True)
                pack_lines.append(f"    {line}")
            pack_lines.append(
                "  Deep-read pack nodes: engram-pkg --pkg <pack-path> inspect <node-id> (pack-scoped recall lands in a later slice).]"
            )
            parts.append("\n".join(pack_lines))
    elif pack_results:
        # Own-graph query failed entirely (result is None) but we have pack hits.
        pack_lines = ["[ENGRAM Recall: own-graph unavailable; attached libraries:"]
        pack_lines.append("  From attached libraries (read-only — cite, never import):")
        for pr in pack_results:
            pack_id = pr.get("pack_id", "?")
            node_id = pr.get("id", "?")
            namespaced_entry = dict(pr)
            namespaced_entry["id"] = f"{pack_id}:{node_id}"
            line = render_one_node_line(namespaced_entry, conf_prefix=True, type_tag=True)
            pack_lines.append(f"    {line}")
        pack_lines.append(
            "  Deep-read pack nodes: engram-pkg --pkg <pack-path> inspect <node-id> (pack-scoped recall lands in a later slice).]"
        )
        parts.append("\n".join(pack_lines))

    # Emit engram.surface.render_size event for the structured event log
    # (recall-triggering blueprint §3-P4, issue #1692). Mirrors the
    # engram.surface.fire emission above: same Emitter singleton (idempotent
    # re-init), same best-effort/never-raise failure-mode contract. Only
    # fires when format_nudge actually ran (_render_stats non-empty) — see
    # its out-parameter docstring for why an untouched {} means "skipped".
    if _render_stats:
        try:
            sys.path.insert(0, ENGRAM_HOME)  # ~/.engram/ contains the emitter
            from engram_log_emitter import Emitter
            _emitter2 = Emitter.init(
                session_id=hook_input.get("session_id", "unknown"),
                transcript_path=hook_input.get("transcript_path", ""),
            )
            _emitter2.emit(
                event_type="engram.surface.render_size",
                level=1,
                data=dict(_render_stats),
            )
        except Exception:
            # Emitter failures must not break the hook — drop silently.
            pass

    # Write reminder (from Stop hook)
    write_reminder = check_write_reminder()
    if write_reminder:
        parts.append(write_reminder)

    # Tool-call repair notification (from PreToolUse repair hook)
    repair_notice = check_repair_marker()
    if repair_notice:
        parts.append(repair_notice)

    # Feeling-nudge (post-compact wake-up). Read-only — does NOT clear the
    # marker; only engram_report_feeling clears it.
    feeling_nudge = check_feeling_nudge_marker()
    if feeling_nudge:
        parts.append(feeling_nudge)

    # Warm-restart briefing pointer (first prompt after compaction only).
    # Delivered as a pointer, not full injection — the agent discovers and
    # chooses to read the note, like Lucy finding Henry's journal.
    warm_briefing = check_warm_briefing(counter["prompts_since_compaction"])
    if warm_briefing:
        parts.append(warm_briefing)

    # Nap warning (if approaching compaction) — uses JSONL-based context tracker
    try:
        sys.path.insert(0, HOOK_DIR)
        from context_tracker import estimate_usage, format_drowsiness
        # #140: thread session_id + transcript_path from this hook's stdin
        # so drowsiness reads THIS session's per-session marker, not a
        # single shared global marker that other sessions could clobber.
        usage = estimate_usage(
            transcript_path=hook_input.get("transcript_path"),
            session_id=hook_input.get("session_id"),
        )
        if usage:
            nap_warning = format_drowsiness(usage)
            if nap_warning:
                parts.append(nap_warning)
    except ImportError:
        # Fallback to prompt count if context tracker not available
        nap_warning = format_nap_warning(counter["prompts_since_compaction"])
        if nap_warning:
            parts.append(nap_warning)

    if not parts:
        print(json.dumps({}))
        sys.exit(0)

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(response))
    sys.exit(0)


if __name__ == "__main__":
    main()
