#!/usr/bin/env python3
"""Session start: record per-session marker, reset context-tracker baseline,
clear sticky user identity, and inject session info into the conversation.

Reading-reminder injection is source-conditional:
  - source=="startup" (fresh terminal): warm-briefing + newest history file +
    Piece C git log (last 2 days of commits from both repos).
  - source=="compact"/"resume" (or anything else): warm-briefing only. The
    history file is skipped because the compaction summary carries
    work-thread continuity, and re-reading today's active-day log mid-day
    introduces "yesterday/today" framing confusion (ob_NNNN, 2026-04-24).

The history/git-log bundle is Piece C of the three-piece session-handover
fix: on startup the agent cross-checks pending-status claims
("MECH-N deferred", "still uncommitted") against the git log before
reporting to the user.

The per-session marker (~/.engram/sessions/<session_id>.json) is written
on every SessionStart and carries session_id + transcript_path + cwd +
started_at + role. Concurrent sessions own their own files — no clobber.
Issue #140 retired the single shared ~/.engram/active-session.json marker
that this replaces; agents and hooks read transcript_path either from
SessionStart's injected additionalContext (preferred) or from the
per-session file by session_id.

A compact [ENGRAM Calibration] block is injected on every SessionStart
(both startup and compact/resume) so fresh-wake context has actual
distribution anchors present — not just semantic labels. Closes the
access≠presence gap. Sourced from engram_stats(sections=
["confidence"]) + engram_stats(mode="7-turn", sections=["confidence"]).
Silent skip on any stats failure: hook never blocks session start.
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_tracker import write_baseline

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)


def _resolve_runtime_dir(engram_home: str) -> str:
    """Locate directory containing engram_client.py for stats import.

    Priority:
      1. $ENGRAM_RUNTIME_DIR if set explicitly.
      2. $ENGRAM_HOME if it bundles a server snapshot (alpha-install pattern).
      3. ~/engram-alpha (live-source fallback for dev installs).
    """
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    if os.path.exists(os.path.join(engram_home, "engram_client.py")):
        return engram_home
    return os.path.expanduser("~/engram-alpha")


def _resolve_mcp_server_path(engram_home: str) -> str:
    """Resolve the MCP server.py path for process-existence check.

    Priority order:
      1. Hook-colocated server.py (works for BOTH install paths —
         scatter copies bootstrap.py + server.py + hooks together into
         ~/.engram/; plugin assembles them together at <plugin>/).
         This is the only candidate that works in plugin install — the
         MCP server runs from the plugin tree, not from ENGRAM_HOME.
      2. $ENGRAM_HOME/server.py (legacy scatter-install pattern in case
         hooks were copied separately or the colocation is unusual)
      3. ~/engram-alpha/server.py (live-source dev fallback)
    """
    # Hook lives at <runtime-dir>/hooks/engram-session-start-hook.py,
    # server.py lives at <runtime-dir>/server.py.
    hook_dir = os.path.dirname(os.path.abspath(__file__))
    runtime_dir = os.path.dirname(hook_dir)
    colocated = os.path.join(runtime_dir, "server.py")
    if os.path.exists(colocated):
        return colocated
    candidate = os.path.join(engram_home, "server.py")
    if os.path.exists(candidate):
        return candidate
    return os.path.expanduser("~/engram-alpha/server.py")


def _check_mcp_health(engram_home: str) -> tuple[bool, str | None]:
    """Check whether the ENGRAM MCP server process is running.

    Uses pgrep -f to search for the resolved server.py path in process
    command lines. Hard timeout of 1s. Any unexpected exception is treated
    as ok=True (advisory probe — never block session start).

    Returns:
        (ok, reason): ok=True if process found or probe itself errors;
                      ok=False with a reason string otherwise.
    """
    try:
        server_path = _resolve_mcp_server_path(engram_home)
        result = subprocess.run(
            ["pgrep", "-f", server_path],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if result.returncode == 0:
            return True, None
        return False, "no engram server process found"
    except subprocess.TimeoutExpired:
        return False, "pgrep timed out"
    except Exception:
        # Advisory: probe errors are treated as ok to avoid blocking session start.
        return True, None


def format_calibration_block(conf_all: dict, conf_7d: dict, current_turn: int) -> str:
    """Render a 6-10 line calibration anchor for session start.

    Formats per-type quantile rows from conf_all["by_type"] plus a
    7-turn rolling median comparison. Empty buckets are silently skipped.
    Block is always ≤10 lines and starts with [ENGRAM Calibration:].

    Args:
        conf_all: confidence section from engram_stats(sections=["confidence"]).
        conf_7d:  confidence section from engram_stats(mode="7-turn",
                  sections=["confidence"]).
        current_turn: memory.current_turn from the all-time stats response.

    Returns:
        Multi-line string with the calibration block, or empty string if
        conf_all has no usable by_type data.
    """
    by_type = conf_all.get("by_type", {})
    if not by_type:
        return ""

    lines = [f"[ENGRAM Calibration: graph distribution at turn {current_turn}]"]

    # All-claims aggregate: pool p25/p50/p75/p95 across all types that
    # appear in by_type (weighted by n).
    all_n = sum(v.get("n", 0) for v in by_type.values())
    if all_n > 0:
        # Weighted medians aren't available from per-type stats alone;
        # use simple mean of p50/p25/p75/p95 weighted by n as a proxy.
        def _wavg(key: str) -> float:
            # Guard is `key in v` — v[key] is safe, no fallback needed.
            total = sum(
                v[key] * v.get("n", 0)
                for v in by_type.values()
                if v.get("n", 0) > 0 and key in v
            )
            denom = sum(
                v.get("n", 0) for v in by_type.values()
                if v.get("n", 0) > 0 and key in v
            )
            return round(total / denom, 2) if denom else 0.0

        lines.append(
            f"  All claims   (N={all_n:4d}) "
            f"p25 {_wavg('p25'):.2f} · p50 {_wavg('p50'):.2f} · "
            f"p75 {_wavg('p75'):.2f} · p95 {_wavg('p95'):.2f}"
        )

    # Per-type rows: observations, derivations, conjectures (most salient
    # for calibration). Axioms and lessons included when present.
    _TYPE_LABELS = {
        "observation_factual": "observations ",
        "derivation":          "derivations  ",
        "conjecture":          "conjectures  ",
        "lesson":              "lessons      ",
        "axiom":               "axioms       ",
    }
    for ntype, label in _TYPE_LABELS.items():
        stats = by_type.get(ntype)
        if not stats:
            continue
        n = stats.get("n", 0)
        lines.append(
            f"  {label}(N={n:4d}) "
            f"p25 {stats.get('p25', 0):.2f} · "
            f"p50 {stats.get('p50', 0):.2f} · "
            f"p75 {stats.get('p75', 0):.2f}"
        )

    # 7-turn rolling comparison
    by_type_7d = conf_7d.get("by_type", {})
    all_7d_n = sum(v.get("n", 0) for v in by_type_7d.values())
    if all_7d_n > 0:
        # Weighted p50 across 7-day window
        total_50 = sum(
            v.get("p50", 0) * v.get("n", 0)
            for v in by_type_7d.values()
            if v.get("n", 0) > 0 and "p50" in v
        )
        denom_7d = sum(
            v.get("n", 0) for v in by_type_7d.values()
            if v.get("p50") is not None
        )
        median_7d = round(total_50 / denom_7d, 2) if denom_7d else 0.0

        # Corpus-wide p50 for comparison
        corpus_p50 = _wavg("p50") if all_n > 0 else 0.0
        lines.append(
            f"  7-turn rolling: N={all_7d_n} median {median_7d:.2f} "
            f"(vs corpus {corpus_p50:.2f})"
        )

    # Cap at 10 lines (header + 9 data lines).
    # With the current _TYPE_LABELS (5 entries) the maximum is 8 lines
    # (header + all_claims + 5 type rows + 7d rolling).  The cap is kept as
    # forward-looking defense: if _TYPE_LABELS gains entries in the future,
    # the block stays bounded without requiring a separate edit here.
    if len(lines) > 10:
        lines = lines[:10]

    return "\n".join(lines)


SESSION_ROLE = os.environ.get("ENGRAM_SESSION_ROLE", "interactive").strip().lower()
SESSION_PURPOSE = os.environ.get("ENGRAM_SESSION_PURPOSE", "").strip()
SESSIONS_DIR = os.path.join(ENGRAM_HOME, "sessions")
CURRENT_USER_PATH = os.path.join(ENGRAM_HOME, "current_user.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")
HISTORY_DIR = os.path.join(ENGRAM_HOME, "history")

# Role → default purpose if ENGRAM_SESSION_PURPOSE not set by spawner.
DEFAULT_PURPOSES = {
    "interactive": "Interactive session with user",
    "heartbeat": "Coordination heartbeat (cron-fired, periodic)",
    "telegram": "Telegram dispatch (user messages via Telegram bridge)",
    "sleep": "Coordinated daily sleep / consolidation",
}
# Repos scanned for Piece C (recent commit cross-check at startup).
# ENGRAM_HOME is always included. Extra repos via ENGRAM_PIECE_C_REPOS env var
# (colon-separated). Default extra: ~/engram-alpha if it exists (the agent's dev checkout, if present);
# for agents installed without a local alpha clone, silently
# no-ops because the isdir guard returns False.
_PIECE_C_EXTRA = os.environ.get("ENGRAM_PIECE_C_REPOS", "").strip()
if _PIECE_C_EXTRA:
    PIECE_C_REPOS = [os.path.expanduser(p) for p in _PIECE_C_EXTRA.split(":") if p] + [ENGRAM_HOME]
else:
    _default_extra = os.path.expanduser("~/engram-alpha")
    PIECE_C_REPOS = ([_default_extra] if os.path.isdir(_default_extra) else []) + [ENGRAM_HOME]
PIECE_C_WINDOW = "2 days ago"
PIECE_C_MAX_LINES = 20
PIECE_C_MAX_LINE_CHARS = 140

# Sleep-debt detection: surface a banner on fresh-terminal start (interactive
# role) if the last coordinated sleep was longer ago than this threshold.
# 24h + 4h grace covers normal cron-late, morning routine, etc.
LAST_SLEEP_MARKER_PATH = os.path.join(SESSIONS_DIR, "last-sleep-success.json")
ENGRAM_DB_PATH = os.path.join(ENGRAM_HOME, "knowledge.db")
SLEEP_DEBT_HOURS_THRESHOLD = 28
# Node-count above which a missing marker is treated as a real anomaly
# (mechanism-just-shipped, or marker accidentally deleted) rather than a
# genuine first-ever boot. A fresh install has only a handful of seed
# nodes; any established graph is well above 100.
SLEEP_BASELINE_NODE_THRESHOLD = 100

# Prune session markers older than this many seconds (7 days).
SEVEN_DAYS = 7 * 86400
# Only delete files whose names look like session markers (UUID4 8-4-4-4-12
# layout OR older hex-id shapes from pre-PR-160 markers). The UUID4 layout
# is hex-only with fixed dash positions (not the looser `[0-9a-f\-]{36}` that
# would match 36 dashes); the older shape also covers `.meta.json` siblings
# from the pre-PR-160 era that should be pruned alongside their .jsonl.
_MARKER_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$'
    r'|^[0-9a-f]{8,}(\.meta)?\.json$'
)


def prune_stale_session_markers(current_session_id: str | None) -> None:
    """Delete session marker files older than 7 days from SESSIONS_DIR.

    Best-effort: OSError on any individual delete is swallowed (logged to stderr).
    Only deletes files matching UUID-like or hex-id patterns; ignores anything
    else (forward-compat against future file formats in sessions/).
    Skips the current session's marker even if it appears old.
    """
    cutoff = time.time() - SEVEN_DAYS
    current_filename = f"{current_session_id}.json" if current_session_id else None
    try:
        entries = os.listdir(SESSIONS_DIR)
    except OSError:
        return
    for name in entries:
        if not name.endswith(".json"):
            continue
        if not _MARKER_RE.match(name):
            continue
        if name == current_filename:
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError as exc:
            print(f"[engram-session-start] prune: could not remove {path}: {exc}", file=sys.stderr)


def write_marker(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    if not session_id or not transcript_path:
        return None
    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "source": hook_input.get("source"),
        "cwd": hook_input.get("cwd"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "role": SESSION_ROLE,
        "purpose": SESSION_PURPOSE or DEFAULT_PURPOSES.get(SESSION_ROLE, SESSION_ROLE),
    }
    # context_mode field retired: mode-string machinery replaced by
    # cadence.drowsiness_ceiling_tokens (single explicit user-set value).
    # Existing markers with context_mode are gracefully ignored on read.
    try:
        # Per-session marker keyed by session_id. Concurrent sessions own
        # their own files; the role field in the contents lets readers
        # filter (e.g., heartbeat scanning for any active interactive
        # session). Issue #140 retired the single shared active-session.json
        # that this replaces.
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        # Prune is best-effort: a bug in prune (any exception type) must
        # NEVER kill the marker write, which is the load-bearing operation
        # here. Per-file OSError is already swallowed inside the function;
        # this outer guard catches anything else (AttributeError from a
        # logic bug, MemoryError, etc.).
        try:
            prune_stale_session_markers(session_id)
        except Exception as exc:
            print(f"[engram-session-start] prune: unexpected error, skipped: {exc}", file=sys.stderr)
        session_marker_path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
        with open(session_marker_path, "w") as f:
            json.dump(marker, f)
    except OSError:
        return None
    return marker


def piece_c_git_log() -> str:
    """Collect recent commits from both canonical repos for startup cross-check.

    Returns a multi-line string ready to append to SessionStart additionalContext.
    Silent empty-string on any failure — hook must not break session start.
    """
    blocks = []
    for repo in PIECE_C_REPOS:
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        try:
            out = subprocess.run(
                ["git", "-C", repo, "log", f"--since={PIECE_C_WINDOW}", "--oneline"],
                capture_output=True, text=True, timeout=3,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if out.returncode != 0:
            continue
        raw = [l for l in out.stdout.splitlines() if l.strip()]
        if not raw:
            continue
        lines = []
        for l in raw[:PIECE_C_MAX_LINES]:
            if len(l) > PIECE_C_MAX_LINE_CHARS:
                l = l[:PIECE_C_MAX_LINE_CHARS - 1] + "…"
            lines.append(l)
        if len(raw) > PIECE_C_MAX_LINES:
            lines.append(f"... ({len(raw) - PIECE_C_MAX_LINES} older commits omitted)")
        label = os.path.basename(repo) or repo
        blocks.append(f"  {label}:\n" + "\n".join(f"    {l}" for l in lines))
    if not blocks:
        return ""
    header = (
        "[Piece C — recent commits (last 2 days). Cross-check pending-status "
        f"claims in the newest {HISTORY_DIR}/ file against these commits "
        "before reporting to the user; don't propagate stale status.]"
    )
    return header + "\n" + "\n".join(blocks)


def sleep_status_block() -> str:
    """Surface a sleep-status banner if last coordinated sleep is stale,
    or if the baseline marker is missing on an established graph.

    Only fires for interactive role (other roles can't decide to catch up).
    The decision to run /engram-sleep stays with the user — this just
    surfaces the data so the user can choose case-by-case.

    Three paths:
      - Marker fresh (≤ threshold): silent.
      - Marker stale (> threshold): banner with hours-since and nodes-added.
      - Marker missing on established DB (> SLEEP_BASELINE_NODE_THRESHOLD):
        banner asking the user to run /engram-sleep to establish a baseline.
        Genuine first-ever boot (DB below threshold) stays silent.
    """
    if SESSION_ROLE != "interactive":
        return ""

    marker: dict | None = None
    completed_at = None
    completed_at_str = None
    if os.path.exists(LAST_SLEEP_MARKER_PATH):
        try:
            with open(LAST_SLEEP_MARKER_PATH) as f:
                marker = json.load(f)
            completed_at_str = marker.get("completed_at")
            if completed_at_str:
                completed_at = datetime.fromisoformat(
                    completed_at_str.replace("Z", "+00:00")
                )
            else:
                marker = None
        except (OSError, ValueError, json.JSONDecodeError):
            marker = None

    if marker is None:
        total_nodes: int | None = None
        try:
            conn = sqlite3.connect(ENGRAM_DB_PATH, timeout=2.0)
            cur = conn.execute("SELECT COUNT(*) FROM nodes")
            total_nodes = cur.fetchone()[0]
            conn.close()
        except sqlite3.Error:
            return ""
        if total_nodes is None or total_nodes < SLEEP_BASELINE_NODE_THRESHOLD:
            return ""
        return (
            f"[SLEEP STATUS: no baseline sleep marker at "
            f"{LAST_SLEEP_MARKER_PATH}. DB has {total_nodes} nodes — not a "
            "fresh install, so either the sleep-debt mechanism just "
            "shipped (no successful coordinated sleep has run yet) or the "
            "marker was deleted. Run /engram-sleep to establish a "
            "baseline; subsequent skipped-sleep nights will banner "
            "normally once a marker exists.]"
        )

    hours_since = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
    if hours_since <= SLEEP_DEBT_HOURS_THRESHOLD:
        return ""
    nodes_since: int | None = None
    try:
        conn = sqlite3.connect(ENGRAM_DB_PATH, timeout=2.0)
        cur = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE created_at > ?",
            (completed_at_str,),
        )
        nodes_since = cur.fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
    nodes_part = (
        f"{nodes_since} nodes added since"
        if nodes_since is not None
        else "node count unavailable"
    )
    turn = marker.get("turn_advanced_to", "?")
    return (
        f"[SLEEP STATUS: last coordinated sleep {hours_since:.1f}h ago "
        f"({completed_at_str}, turn {turn}). {nodes_part}. "
        "The midnight cron likely did not fire (host was off). "
        "Decide case-by-case whether to run /engram-sleep now to consolidate "
        "the gap, or skip if the pending cohort is non-essential.]"
    )


def auto_sleep_cron_block() -> str:
    """Emit a cron-registration nudge when cadence.auto_sleep_enabled is true.

    Only fires for interactive role (cron is registered per-session by the
    interactive agent; other roles don't carry it). Silent "" on any read/parse
    error — hook must never block session start.

    Reads cadence.auto_sleep_enabled (default False) and cadence.auto_sleep_time
    (default "03:00") from ~/.engram/config.json. Computes a stable per-install
    jitter so not every install fires on the same exact minute.

    Returns:
        Multi-line nudge string when enabled + interactive, otherwise "".
    """
    if SESSION_ROLE != "interactive":
        return ""

    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        cadence = config.get("cadence", {})
        enabled = cadence.get("auto_sleep_enabled", False)
        if not enabled:
            return ""
        auto_sleep_time = cadence.get("auto_sleep_time", "03:00")
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        # TypeError/AttributeError guard valid-JSON-but-non-dict shapes:
        # config.json containing `null` / `[]` / `"x"` makes config a
        # non-dict, and `{"cadence": null}` makes cadence None — either makes
        # the .get() calls above raise. The hook must never break session start.
        return ""

    # Guard: a present-but-null or non-string value would raise AttributeError
    # on .split(). Treat any non-string as a missing/invalid value.
    if not isinstance(auto_sleep_time, str):
        return ""

    # Parse HH:MM
    try:
        parts = auto_sleep_time.split(":")
        hour = int(parts[0])
        configured_minute = int(parts[1])
    except (ValueError, IndexError):
        return ""

    # Range-validate parsed time — e.g. "99:99" passes int() but produces an
    # invalid cron expression. Return "" rather than handing a bad expr to the agent.
    if not (0 <= hour <= 23 and 0 <= configured_minute <= 59):
        return ""

    # Stable per-install jitter: 0–9 minutes derived from ENGRAM_HOME path hash.
    # encode("utf-8", errors="replace") guards against surrogateescape paths from
    # os.environ that would raise UnicodeEncodeError with the default strict encoder.
    jitter = int(hashlib.sha256(ENGRAM_HOME.encode("utf-8", errors="replace")).hexdigest(), 16) % 10
    minute = configured_minute + jitter
    if minute >= 60:
        minute = configured_minute
    cron_expr = f"{minute} {hour} * * *"

    return (
        f"[Auto-sleep enabled] cadence.auto_sleep_enabled is true "
        f"(nightly sleep ~{auto_sleep_time} local). On your FIRST prompt this "
        "session, ensure the sleep cron is registered (idempotent):\n"
        '  1. Call CronList. If any job\'s prompt contains "AUTO-SLEEP NIGHTLY",'
        " it is already registered this session — skip to step 3.\n"
        f'  2. Else call CronCreate with cron="{cron_expr}", recurring=true, '
        "durable=false, and prompt set to the AUTO-SLEEP CRON-PROMPT below. "
        "(durable:false is intentional — the cron dies with this session and is "
        "re-registered every SessionStart, so the current session always carries "
        "it and stale duplicates can't accumulate.)\n"
        f'  3. Tell the user once: "Auto-sleep is on — I\'ll consolidate overnight '
        f"(target ~{auto_sleep_time}, but on macOS laptops in default sleep the cron "
        "typically fires 30-90 min late and consolidation may span hours). "
        f"Three things keep it clean: (a) wind down heavy work ~5h before {auto_sleep_time} "
        f"so the cron gets a fresh token window, (b) leave this session open and idle "
        f"through {auto_sleep_time} — a closed session can't fire the cron, and don't "
        "leave multiple sessions idle overnight or each will try to sleep, and "
        "(c) if precise nightly timing matters on macOS, see the macOS mitigations "
        'in the engram-sleep skill docs."\n'
        "\n"
        "AUTO-SLEEP CRON-PROMPT to register (use exactly this as the CronCreate prompt):\n"
        '"AUTO-SLEEP NIGHTLY consolidation. Before consolidating: (1) Read '
        "~/.engram/sessions/last-sleep-success.json; if a sleep completed recently "
        "— within roughly the last work-cycle (~18-24h; use judgment) — tonight's "
        "cohort is already consolidated, so STOP: do NOT run any sleep and do NOT "
        "advance the turn (advance is IRREVERSIBLE), report 'auto-sleep skipped: "
        "consolidated Nh ago', and exit. (2) Else check for tonight's lock at "
        "~/.engram/sleep-locks/$(date +%Y-%m-%d).lock. If it does NOT exist: "
        "mkdir -p ~/.engram/sleep-locks, then atomically create it (O_EXCL via "
        "python open(path,'x') or bash 'set -C; >file'), then proceed to step (3). "
        "If the atomic create fails (race: another session created the lock between "
        "our check and create), treat the result as the DOES-exist path and check mtime. "
        "If it DOES exist: check its mtime. If mtime is recent (< 2 hours ago): "
        "another idle session's cron just started — STOP and exit quietly with "
        "'auto-sleep skipped: another session's cron started Nm ago'. If mtime "
        "is stale (>= 2 hours ago): the lock is from a crashed run. Take over: "
        "delete the stale lock, re-create it atomically, log a warning "
        "('recovered from stale lock from <timestamp>'), then proceed to step (3). "
        "(3) If you got the lock, run /engram-sleep (full end-of-day "
        "consolidation: Phase A cohort completion + Phase B dream orchestration + "
        "turn advance + dream record). The skill updates last-sleep-success.json "
        'on completion, making step (1) idempotent for any later fire tonight."'
    )


def fairy_policy_block() -> str:
    """Emit one mode-line per fairy policy on fresh-terminal startup.

    Only fires for interactive role + source=="startup" (the startup guard is
    applied at the call site in main()). Silent "" on any read/parse error —
    hook must never block session start.

    Reads coder_fairy_policy and reviewer_fairy_policy (both default "auto")
    from ~/.engram/config.json. Emits two lines describing the active mode
    for each policy so the agent knows which skill to load (if any) per decision.

    Returns:
        Two-line string when interactive+startup, "" otherwise.
    """
    if SESSION_ROLE != "interactive":
        return ""

    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        coder_policy = config.get("coder_fairy_policy", "auto")
        reviewer_policy = config.get("reviewer_fairy_policy", "auto")
        # Substitute the user's name into the "explicit" mode text. Fall back
        # to "you" when primary_user is absent or empty (e.g. pre-first-session
        # installs where the cold-start dialogue hasn't filled it in yet).
        primary_user = config.get("primary_user") or "you"
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        # TypeError/AttributeError guard valid-JSON-but-non-dict shapes:
        # config.json containing `null` / `[]` / `"x"` makes config a
        # non-dict and .get() raises. Hook must never break session start.
        return ""

    # Defensive: primary_user may be present-but-non-string (e.g. null or a
    # number). Coerce to a safe default rather than letting str.format crash.
    if not isinstance(primary_user, str):
        primary_user = "you"

    _POLICY_LINES = {
        "explicit": (
            f"[Coder fairy policy: explicit] Do PR coding work directly unless "
            f"{primary_user} explicitly invokes a fairy. Mentally spec the change first."
        ),
        "always": (
            "[Coder fairy policy: always] Every PR coding task gets a fairy — "
            "no size exception, no quick-fix exception. Spec and dispatch."
        ),
        "auto": (
            "[Coder fairy policy: auto] Use judgment per task. Before deciding on "
            "PR-coding work, load engram-auto-coder-fairy-judgement skill — "
            "its heuristic decides direct vs. dispatch."
        ),
    }
    _REVIEWER_POLICY_LINES = {
        "explicit": (
            f"[Reviewer fairy policy: explicit] Do PR review work directly unless "
            f"{primary_user} explicitly invokes a reviewer-fairy. Review inline."
        ),
        "always": (
            "[Reviewer fairy policy: always] Every PR review task gets a "
            "reviewer-fairy — no size exception, no quick-fix exception. Dispatch."
        ),
        "auto": (
            "[Reviewer fairy policy: auto] Use judgment per task. Before deciding on "
            "PR-review work, load engram-auto-reviewer-fairy-judgement skill — "
            "its heuristic decides direct vs. dispatch."
        ),
    }

    # Defensive: a policy value of any unhashable type (list, dict — from
    # a hand-edited config.json) would crash dict.get() with TypeError.
    # Coerce to "auto" rather than letting the hook break session start.
    # Strings outside the allowed enum also coerce to "auto" via the dict
    # default below — this just adds the unhashable-shape safety net.
    if not isinstance(coder_policy, str):
        coder_policy = "auto"
    if not isinstance(reviewer_policy, str):
        reviewer_policy = "auto"

    # Fall back to "auto" for any unknown value (forward-compat for future modes).
    coder_line = _POLICY_LINES.get(coder_policy, _POLICY_LINES["auto"])
    reviewer_line = _REVIEWER_POLICY_LINES.get(reviewer_policy, _REVIEWER_POLICY_LINES["auto"])
    return coder_line + "\n" + reviewer_line


def main() -> None:
    _t0 = time.perf_counter()

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    marker = write_marker(hook_input)
    write_baseline(use_compact_boundary=True)

    if os.path.exists(CURRENT_USER_PATH):
        try:
            os.remove(CURRENT_USER_PATH)
        except OSError:
            pass

    source = (hook_input.get("source") or "").lower()

    lines = []
    if marker:
        lines.append(
            f"[Active session: {marker['session_id']} "
            f"(source={marker.get('source') or 'unknown'}) — "
            f"transcript at {marker['transcript_path']}]"
        )
    if source == "startup":
        lines.append(
            "[Session start reading — before responding to the first task "
            "(even if a user task is already waiting), follow your Session "
            "Start Reading list in ~/.claude/CLAUDE.md. Read first, respond "
            "second.]"
        )
        git_log = piece_c_git_log()
        if git_log:
            lines.append(git_log)
        sleep_block = sleep_status_block()
        if sleep_block:
            lines.append(sleep_block)
        cron_block = auto_sleep_cron_block()
        if cron_block:
            lines.append(cron_block)
        fairy_block = fairy_policy_block()
        if fairy_block:
            lines.append(fairy_block)
    else:
        lines.append(
            f"[Session start reading — re-read {WARM_BRIEFING_PATH} before "
            "responding (relational context post-compact, per Compact "
            "Instructions). Other Session Start Reading items can be skipped; "
            "the compaction summary carries work-thread continuity.]"
        )

    # ── ENGRAM Calibration block ───────────────────────────────────────────
    # Inject per-type distribution anchors on every SessionStart so fresh-wake
    # context has actual quantile data present (not just semantic labels).
    # Closes the access≠presence gap. Silent skip on any failure —
    # hook must never block session start.
    try:
        _runtime_dir = _resolve_runtime_dir(ENGRAM_HOME)
        os.environ.setdefault("ENGRAM_HOME", ENGRAM_HOME)
        if _runtime_dir not in sys.path:
            sys.path.insert(0, _runtime_dir)
        from engram_client import EngramClient
        _client = EngramClient()
        _stats_all = _client.call(
            "engram_stats", {"payload_json": json.dumps({"sections": ["confidence", "memory"]})}
        )
        _stats_7d = _client.call(
            "engram_stats", {"payload_json": json.dumps({"mode": "7-turn", "sections": ["confidence"]})}
        )
        _current_turn = _stats_all.get("memory", {}).get("current_turn", 0)
        _cal_block = format_calibration_block(
            _stats_all.get("confidence", {}),
            _stats_7d.get("confidence", {}),
            _current_turn,
        )
        if _cal_block:
            lines.append(_cal_block)
    except Exception:
        # Hook discipline: never surface calibration errors to session start.
        pass

    # ── Substrate health probe ─────────────────────────────────────────────
    # Check MCP server. Warn in additionalContext when offline so the agent
    # surfaces the degradation to the user immediately (instead of discovering
    # via mid-task tool failure). Probe is advisory: any unexpected exception
    # is treated as ok=True — never block session start.
    # Silent default: no extra line when probe passes. Closes #198.
    # Note: surface-daemon health warning is intentionally NOT done here.
    # A false alarm on startup (daemon not yet running) fires every session
    # and trains the agent to ignore the warning. The warning fires at actual
    # use-time from engram-surface-hook.py instead (closes #344).
    mcp_ok, mcp_reason = _check_mcp_health(ENGRAM_HOME)

    if not mcp_ok:
        lines.append("")
        lines.append("⚠️  ENGRAM substrate health:")
        lines.append(f"  MCP server: OFFLINE ({mcp_reason})")
        lines.append("  → Tell the user immediately. ENGRAM tool calls will fail.")

    output_obj = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    output_str = json.dumps(output_obj)
    print(output_str)

    _duration_ms = int((time.perf_counter() - _t0) * 1000)

    # Emit engram.hook.fire event. Failure must not break the hook.
    try:
        _session_id = hook_input.get("session_id", "unknown") or "unknown"
        _transcript_path = hook_input.get("transcript_path", "") or ""
        sys.path.insert(0, ENGRAM_HOME)
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=_session_id,
            transcript_path=_transcript_path,
        )
        _emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-session-start-hook",
                "hook_type": "SessionStart",
                "duration_ms": _duration_ms,
                "exit_code": 0,
                "stdout_bytes": len(output_str.encode("utf-8")),
                "stderr_bytes": 0,
            },
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
