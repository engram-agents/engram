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
import math
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
                _tmp = str(STATE_PATH) + ".tmp"
                with open(_tmp, "w") as f:
                    json.dump(state, f)
                os.replace(_tmp, str(STATE_PATH))
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
