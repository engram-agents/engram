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

The live focus list (the deterministic focus channel, #1732) is also
rendered verbatim on every SessionStart, regardless of source. It
originally shipped at PostCompact (#1655/#1710), but PostCompact is a
side-effects-only Claude Code hook event that cannot inject
additionalContext at all — so that render never actually reached the
model. SessionStart fires for source=="compact" too, so this is the
correct render point; Lei's 2026-07-09 scope call additionally extended
it to fresh/resumed sessions, not just post-compact ones.
"""
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
      4. ~/engram-alpha (live-source fallback for dev installs).
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
    return os.path.expanduser("~/engram-alpha")


def _runtime_platform() -> str | None:
    """Return the baked plugin platform name when available.

    Plugin builds write platform.json at the runtime root. Source-tree/dev
    execution generally has no platform.json, so return None there. The
    probe is advisory; malformed or unreadable files are treated as unknown.
    """
    try:
        hook_dir = os.path.dirname(os.path.abspath(__file__))
        runtime_dir = os.path.dirname(hook_dir)
        platform_path = os.path.join(runtime_dir, "platform.json")
        with open(platform_path, encoding="utf-8") as f:
            data = json.load(f)
        platform = data.get("platform")
        return platform if isinstance(platform, str) else None
    except Exception:
        return None


def _startup_mcp_health_probe_allowed() -> bool:
    """Whether SessionStart can run a reliable process-level MCP probe.

    Claude plugin installs run hooks and the MCP server from the same plugin
    runtime, so the historical pgrep-by-server.py-path check remains useful.

    Codex is different: hooks can execute from a plugin cache while MCP starts
    from the marketplace plugin path, and Codex may lazily start MCP only on
    first tool use. At SessionStart that makes "no matching process" ambiguous
    rather than evidence of failure. Match the #344 surface-daemon precedent:
    do not emit startup false alarms; let use-time MCP failure surface if it
    actually occurs.
    """
    platform = _runtime_platform()
    if platform == "codex":
        return False

    # A baked non-Codex platform profile is stronger evidence than ambient
    # environment variables. Preserve Claude's same-runtime pgrep warning even
    # if a user shell happens to export CODEX_PLUGIN_ROOT.
    if platform is not None:
        return True

    # When the Codex plugin-root variable is present but platform.json is absent
    # (older/dev bundles), the hook/runtime relationship is still ambiguous.
    if os.environ.get("CODEX_PLUGIN_ROOT"):
        return False

    return True


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


def _check_mcp_health(engram_home: str) -> tuple[bool | None, str | None]:
    """Check whether the ENGRAM MCP server process is running.

    Uses pgrep -f to search for the resolved server.py path in process
    command lines only when the current platform has a reliable hook/server
    path relationship. Hard timeout of 1s. Any unexpected exception is treated
    as ok=True (advisory probe — never block session start).

    Tri-state result (see #1754):
        True  — server process found (up), the platform defers startup
                probing, or the probe itself errored (advisory fail-open).
        False — pgrep completed and found NO process (confirmed absent).
        None  — the probe was INDETERMINATE (pgrep timed out under
                cold-start contention). A timeout is not evidence of
                absence, so the caller must NOT render the hard "OFFLINE →
                tell the user immediately" alarm for this case — that is a
                cry-wolf false-negative. The per-prompt write-tool marker
                check confirms real liveness on the first prompt.

    Returns:
        (status, reason): status per the tri-state above; reason is a
                          human string for the False / None cases, else None.
    """
    if not _startup_mcp_health_probe_allowed():
        return True, None

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
        # Indeterminate, NOT absent: the probe never completed (busy box at
        # cold start). Signal the third state so the caller renders a soft
        # "inconclusive" note instead of a false OFFLINE alarm (#1754).
        return None, "pgrep timed out"
    except Exception:
        # Advisory: probe errors are treated as ok to avoid blocking session start.
        return True, None


def _is_any_server_py_running() -> bool:
    """Return True if any server.py process is running (pgrep secondary check).

    Used by _check_mcp_write_tool_marker to distinguish a truly dead server
    from a post-restart race where the marker PID is stale but the server is
    alive.  Best-effort: returns False on any error so the caller degrades to
    the conservative (unhealthy) path rather than a false positive.
    """
    import subprocess as _sp
    try:
        r = _sp.run(["pgrep", "-f", "server.py"], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def _check_mcp_write_tool_marker(engram_home: str) -> tuple[bool, str | None]:
    """Check whether the MCP server wrote its initialization-complete marker.

    Two-signal check: process existence (pgrep, done by _check_mcp_health) PLUS
    initialization-complete evidence (marker file with PID that's still running).
    Only call this when _check_mcp_health already returned ok=True.

    Known ceiling: the marker attests "init complete + about to serve," not
    "serving." A hang inside mcp.run()'s serve-setup would still false-confirm.
    The probe cleanly detects the slow-import blocking class (server started
    but blocked before mcp.run(), causing a connect-timeout in the client).

    Uses os.kill(pid, 0) — zero-signal, just tests whether the PID is alive.
    EPERM means the process exists (we lack signal permission) — treat as ok.

    Returns:
        (True, None) if marker exists and its PID is still running, or if
                     the probe is deferred on this platform, or on any error.
        (False, reason) if marker is absent or its PID is no longer running.
    """
    if not _startup_mcp_health_probe_allowed():
        return True, None
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
            return True, None  # PID is running
        except OSError as e:
            if e.errno == _errno.EPERM:
                return True, None  # exists, no permission to signal — still running
            # Marker PID is dead — server may have restarted and not yet refreshed
            # the marker (race between restart and this hook). Secondary check: is
            # any server.py actually running right now?
            if _is_any_server_py_running():
                # A live server.py found — stale marker, not a dead server.
                # Return healthy; server will refresh the marker on its next
                # startup write (or already has — hook just caught the window).
                return True, None
            return False, f"mcp-tools-ready.json stale (server PID {pid} no longer running)"
    except Exception:
        return True, None  # advisory — never block session start


def _check_backup_staleness(engram_home: str) -> tuple[bool, str | None]:
    """Check whether knowledge.sql is reasonably current relative to knowledge.db.

    Compares mtime of knowledge.db against mtime of knowledge.sql.  When the
    DB has been written more recently than the SQL dump by more than the
    configured threshold, the git backup is stale and the agent should be
    nudged to run engram-nap or engram-sleep.

    Returns:
        (True, None) if the backup is current, knowledge.db is absent (nothing
                     to back up yet), or any OSError is encountered (fail-open
                     — advisory probe, must never block session start).
        (False, reason) if knowledge.sql is absent or the gap exceeds the
                        configured threshold.
    """
    try:
        db_path = os.path.join(engram_home, "knowledge.db")
        sql_path = os.path.join(engram_home, "knowledge.sql")

        if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
            # No DB yet (or an empty shell left by sqlite3.connect) — nothing to back up.
            return True, None

        if not os.path.exists(sql_path):
            # sql absent — only meaningful if the DB has actual nodes to back up.
            # A freshly bootstrapped empty schema (no nodes) doesn't need a backup yet.
            try:
                _conn = sqlite3.connect(db_path, timeout=1.0)
                try:
                    _count = _conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
                finally:
                    _conn.close()
            except Exception:
                _count = 0
            if _count == 0:
                return True, None
            return False, "knowledge.sql not found — git backup not initialized"

        db_mtime = os.path.getmtime(db_path)
        sql_mtime = os.path.getmtime(sql_path)
        stale_seconds = db_mtime - sql_mtime

        # Read threshold from config; default 2 hours.
        threshold_hours = 2.0
        try:
            config_path = os.path.join(engram_home, "config.json")
            with open(config_path) as _f:
                _cfg = json.load(_f)
            threshold_hours = float(
                _cfg.get("backup", {}).get("stale_threshold_hours", 2.0)
            )
        except Exception:
            pass  # use default

        if stale_seconds > threshold_hours * 3600:
            stale_hours = stale_seconds / 3600
            return (
                False,
                f"knowledge.sql is {stale_hours:.0f}h stale "
                f"(knowledge.db newer than last backup)",
            )

        return True, None
    except OSError:
        return True, None  # advisory — never block session start


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


# Inter-agent dir (mirrors ia.py convention)
INTER_AGENT_DIR = os.environ.get("INTER_AGENT_DIR", "/home/agents-shared/inter-agent")
STARRED_CAP = 10
STARRED_STALE_DAYS = 7  # soft TTL for staleness nudge (nudge only, never auto-drop)
FOCUS_LIST_CAP = 15  # mirrors engram_focus.FOCUS_LIST_CAP (write-side enforced cap)

# Agent registration directory (parallel to inter-agent/ and projects/)
AGENTS_SHARED_ROOT = os.environ.get("AGENTS_SHARED_DIR", "/home/agents-shared")
AGENT_REGISTRY_DIR = os.path.join(AGENTS_SHARED_ROOT, "agents")
# Asymmetry note: this hook exposes only AGENTS_SHARED_DIR as a test override
# (deriving the registry subdir from it).  ia.py additionally accepts
# AGENT_REGISTRY_DIR as a direct override for the registry path.  A test that
# sets AGENT_REGISTRY_DIR without also setting AGENTS_SHARED_DIR (or vice
# versa) will see different paths in the hook vs. ia.py.  The hook's path is
# write-side (registration); ia.py's path is read-side (scan_peers).  Keeping
# them in sync in tests requires overriding AGENTS_SHARED_DIR, not just
# AGENT_REGISTRY_DIR (or passing --registry-dir directly to ia peers).


def write_agent_registration(
    agent_name: str,
    registry_dir: str = AGENT_REGISTRY_DIR,
) -> None:
    """Write/refresh the agent's registration file in the shared registry.

    Writes atomically via temp-file + os.replace so concurrent agents never
    read a partial JSON. Creates the registry dir (group-writable, setgid) if
    it does not exist. Fails silently if the shared filesystem is absent or
    unwritable — this is a multi-agent-only convenience, not a blocker.

    File path: <registry_dir>/<agent_name>.json
    Content   : {"agent_name": ..., "hostname": ..., "pid": ..., "registered_at": <ISO UTC>}

    Args:
        agent_name   : The agent's own name (from config.json or env).
        registry_dir : Override for the default /home/agents-shared/agents/.
                       (Exposed for testing.)
    """
    if not agent_name:
        return

    # Gate: the shared-fs root must exist and be a directory. If absent (single-
    # host install with no shared fs), skip silently — never break SessionStart.
    shared_root = os.path.dirname(registry_dir)
    if not os.path.isdir(shared_root):
        return

    try:
        # Create the registry dir with group-writable, setgid permissions so
        # co-tenant agents can each write their own file.
        os.makedirs(registry_dir, mode=0o2775, exist_ok=True)

        record = {
            "agent_name": agent_name,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        dest = os.path.join(registry_dir, f"{agent_name}.json")

        # Atomic write: temp file in same dir + os.replace (same-fs, no TOCTOU).
        fd, tmp_path = tempfile.mkstemp(dir=registry_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f)
            os.replace(tmp_path, dest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        # Fail-soft: never raise from a multi-agent convenience side-effect.
        pass


PEER_STALE_HOURS = 24  # mirror ia.py's default


def update_counterparts_from_registry(
    agent_name: str,
    config_path: str,
    registry_dir: str = AGENT_REGISTRY_DIR,
) -> None:
    """Update config.json's counterparts list from the live agent registry.

    Replaces the manually-maintained counterparts list with ground truth derived
    from the shared registry.  Co-host peers (same hostname as the current agent)
    are the ones reachable via ia/baton and therefore the meaningful counterpart
    set.  Cross-host peers are excluded.

    Stale entries (older than PEER_STALE_HOURS from ia.py, mirrored here as 24h)
    are excluded — same policy as ia.py's scan_peers().

    Fail-soft: any exception is silently caught, identical to
    write_agent_registration.  Never raises.  The gate:
    - shared-fs root must exist (same as write_agent_registration).
    - config.json must be readable and writable.
    - registry_dir must be a directory.
    If any of those fail, silently returns.

    Write is atomic: temp file in same directory as config.json + os.replace.

    Args:
        agent_name   : This agent's own name (used to exclude self from peers).
        config_path  : Absolute path to config.json.
        registry_dir : Path to the agent registry directory.
                       Override in tests via AGENTS_SHARED_DIR env.
    """
    try:
        registry_path = Path(registry_dir)
        if not registry_path.is_dir():
            return

        own_hostname = socket.gethostname()
        now = datetime.now(timezone.utc)
        stale_delta_seconds = PEER_STALE_HOURS * 3600

        co_host_names: list[str] = []
        for json_file in sorted(registry_path.glob("*.json")):
            try:
                text = json_file.read_text(encoding="utf-8")
                record = json.loads(text)
            except (OSError, json.JSONDecodeError, ValueError):
                continue  # skip malformed/unreadable files

            if not isinstance(record, dict):
                continue

            peer_name = record.get("agent_name", "").strip()
            peer_hostname = record.get("hostname", "").strip()

            # Must have both name and hostname
            if not peer_name or not peer_hostname:
                continue

            # Exclude self
            if peer_name.lower() == agent_name.lower():
                continue

            # Skip stale entries
            registered_at_str = record.get("registered_at")
            if registered_at_str:
                try:
                    registered_dt = datetime.fromisoformat(
                        registered_at_str.replace("Z", "+00:00")
                    )
                    if registered_dt.tzinfo is None:
                        registered_dt = registered_dt.replace(tzinfo=timezone.utc)
                    age_seconds = (now - registered_dt).total_seconds()
                    if age_seconds > stale_delta_seconds:
                        continue  # stale — skip
                except (ValueError, TypeError):
                    pass  # can't parse age — include conservatively

            # Co-host only: same hostname as this agent
            if peer_hostname != own_hostname:
                continue

            co_host_names.append(peer_name)

        co_host_names.sort()

        # If the registry has entries but we found zero co-host peers, the registry
        # is likely in a transient state (simultaneous restarts filtering all entries
        # as stale).  Skip the write to avoid blanking the counterparts list spuriously.
        # If the registry is genuinely empty (no .json files), fall through and clear.
        if not co_host_names and any(registry_path.glob("*.json")):
            return

        # Read current config.json
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        config["counterparts"] = co_host_names

        # Atomic write: temp in same dir as config.json, then os.replace
        config_dir = os.path.dirname(config_path)
        fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        # Fail-soft: never raise from a multi-agent convenience side-effect.
        pass


def peer_topology_block(agent_name: str) -> str:
    """Return a one-line co-host / cross-host peer summary for startup context.

    Reads the shared agent registry (same dir as write_agent_registration writes
    to) and classifies each live peer by hostname. Injected only on fresh-startup
    so agents walk in knowing which peers are ia/baton-reachable vs forum-only.
    Returns "" when the registry is absent (single-agent install), empty, or on
    any error — never blocks session start.
    """
    if not agent_name:
        return ""  # can't exclude self reliably without knowing our own name
    try:
        registry_path = Path(AGENT_REGISTRY_DIR)
        if not registry_path.is_dir():
            return ""

        own_hostname = socket.gethostname()
        now = datetime.now(timezone.utc)
        stale_delta = timedelta(hours=PEER_STALE_HOURS)

        cohost: list[str] = []
        crosshost: list[str] = []

        for json_file in sorted(registry_path.glob("*.json")):
            try:
                record = json.loads(json_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue

            peer_name = record.get("agent_name", "").strip()
            hostname = record.get("hostname", "").strip()
            if not peer_name or not hostname or peer_name.lower() == agent_name.lower():
                continue

            # Skip stale entries (peer not active).
            reg_at = record.get("registered_at")
            if reg_at:  # no registered_at → treat as live (fail-open: missing timestamp ≠ stale)
                try:
                    reg_dt = datetime.fromisoformat(reg_at.replace("Z", "+00:00"))
                    if (now - reg_dt) > stale_delta:
                        continue
                except (ValueError, TypeError):
                    pass

            if hostname == own_hostname:
                cohost.append(peer_name)
            else:
                crosshost.append(peer_name)

        if not cohost and not crosshost:
            return ""

        parts: list[str] = []
        if cohost:
            parts.append(f"co-host (ia/baton): {', '.join(cohost)}")
        if crosshost:
            parts.append(f"cross-host (forum): {', '.join(crosshost)}")

        return f"[Peer topology — {'; '.join(parts)}]"
    except Exception:
        return ""


def focus_block(engram_home: str) -> str:
    """Render the live focus list as a deterministic, verbatim continuity block.

    Queries the active focus list directly from knowledge.db (read-only) so the
    "deterministic focus channel" is an actual mechanism at every session start,
    not a prose convention the compacting model may skip (issue #1732 — the
    channel was originally shipped at PostCompact via #1655/#1710, but
    PostCompact is a side-effects-only Claude Code hook event that cannot
    inject additionalContext; SessionStart (fired for every source, including
    source=="compact") is the correct render point). One line per node,
    rendered VERBATIM (id, claim, focus_reason are never truncated):

        - [<id>] (<focus_reason>) <claim>
        - [<id>] <claim>                (when focus_reason is empty/NULL)

    Returns "" when the DB is missing, the focus list is empty, or any read
    error occurs. Never raises — hook must not block session start.
    """
    try:
        db_path = os.path.join(engram_home, "knowledge.db")
        if not os.path.exists(db_path):
            return ""

        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, claim, focus_reason FROM nodes "
                "WHERE focused_at IS NOT NULL AND is_current = 1 "
                "ORDER BY focused_at"
            ).fetchall()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if not rows:
            return ""

        lines = []
        for row in rows:
            node_id = row["id"]
            claim = row["claim"]
            focus_reason = row["focus_reason"]
            if focus_reason:
                lines.append(f"  - [{node_id}] ({focus_reason}) {claim}")
            else:
                lines.append(f"  - [{node_id}] {claim}")

        total = len(lines)
        display_lines = lines[:FOCUS_LIST_CAP]
        remaining = total - len(display_lines)

        header = "📌 Focused nodes — the deterministic focus channel (verbatim, must survive compaction):"
        block_lines = [header] + display_lines
        if remaining > 0:
            block_lines.append(f"  ... +{remaining} more")
        return "\n".join(block_lines)
    except Exception:
        # Hook discipline: never surface focus-block errors at session start.
        return ""


def starred_block(engram_home: str) -> str:
    """Render starred inter-agent letters as concise pointers.

    One line per entry: ⭐ [<from>] "<title>" — ia read <filename>
    Stars older than STARRED_STALE_DAYS get a trailing nudge: ⚠ stale Nd — unstar if resolved
    Reads from/title from snapshot fields stored at star-time — does NOT re-open or re-parse
    the source letter. If the source letter was deleted, still renders from the snapshot.
    Returns empty string when the list is empty, the file is missing, or any
    read error occurs. Never raises — hook must not block session start.
    """
    try:
        starred_path = os.path.join(engram_home, "inter-agent-starred.json")
        try:
            raw = Path(starred_path).read_text(encoding="utf-8")
            entries = json.loads(raw)
            if not isinstance(entries, list):
                return ""
        except (OSError, json.JSONDecodeError, ValueError):
            return ""

        if not entries:
            return ""

        now = datetime.now(timezone.utc)
        lines = []
        skipped = 0
        for entry in entries:
            filename = entry.get("filename", "").strip()
            if not filename:
                skipped += 1
                continue

            # Read from/title from snapshot; graceful fallback for old entries lacking snapshot fields
            from_agent = (entry.get("from") or "").strip() or "unknown"
            title = (entry.get("title") or "").strip() or "(no title)"
            note = entry.get("note", "").strip()
            note_part = f" — {note}" if note else ""

            # Staleness nudge: compute age from starred_at
            stale_part = ""
            starred_at_str = entry.get("starred_at", "")
            if starred_at_str:
                try:
                    starred_dt = datetime.strptime(starred_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    age_days = (now - starred_dt).days
                    if age_days >= STARRED_STALE_DAYS:
                        stale_part = f"  ⚠ stale {age_days}d — unstar if resolved"
                except ValueError:
                    pass

            lines.append(f"  ⭐ [{from_agent}] \"{title}\" — ia read {filename}{note_part}{stale_part}")

        if not lines:
            return ""

        total = len(lines)
        display_lines = lines[:STARRED_CAP]
        remaining = total - len(display_lines)
        header = f"⭐ {total} starred letter(s) — key cross-session context, re-read if relevant:"
        block_lines = [header] + display_lines
        if remaining > 0:
            block_lines.append(f"  ... +{remaining} more (ia starred to see all)")
        if skipped > 0:
            block_lines.append(
                f"  (note: {skipped} starred entry/entries skipped (missing filename field))"
            )
        return "\n".join(block_lines)
    except Exception:
        # Hook discipline: never surface starred-block errors at session start.
        return ""


SESSION_ROLE = os.environ.get("ENGRAM_SESSION_ROLE", "interactive").strip().lower()
# Effective session role — updated by main() after write_marker() resolves CWD.
# Starts equal to SESSION_ROLE (env-var), may be promoted to "fairy" by CWD detection.
_EFFECTIVE_SESSION_ROLE = SESSION_ROLE
SESSION_PURPOSE = os.environ.get("ENGRAM_SESSION_PURPOSE", "").strip()
SESSIONS_DIR = os.path.join(ENGRAM_HOME, "sessions")
CURRENT_USER_PATH = os.path.join(ENGRAM_HOME, "current_user.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")
HISTORY_DIR = os.path.join(ENGRAM_HOME, "history")

# Role → default purpose if ENGRAM_SESSION_PURPOSE not set by spawner.
DEFAULT_PURPOSES = {
    "interactive": "Interactive session with user",
    "heartbeat": "Coordination heartbeat (periodic automation session)",
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

# CWD pattern for Agent-spawned worktree fairies.  Detects the .claude/worktrees/agent-*
# path that worktree-isolated fairies run inside, as a fallback when ENGRAM_SESSION_ROLE
# is not set by the spawner.
_FAIRY_CWD_RE = re.compile(r'(^|[/\\])\.claude[/\\]worktrees[/\\]agent-')


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
                # For fairy sessions, also delete the JSONL transcript — fairies are
                # disposable workers; interactive transcripts are kept indefinitely.
                transcript_to_delete = None
                try:
                    with open(path) as _mf:
                        _md = json.load(_mf)
                    if _md.get("role") == "fairy":
                        _tp = _md.get("transcript_path") or ""
                        if _tp and os.path.isfile(_tp):
                            transcript_to_delete = _tp
                except (OSError, ValueError):
                    pass  # unreadable marker — skip transcript cleanup, still prune marker
                if transcript_to_delete:
                    try:
                        os.remove(transcript_to_delete)
                    except OSError as exc:
                        print(f"[engram-session-start] prune: could not remove transcript {transcript_to_delete}: {exc}", file=sys.stderr)
                os.remove(path)
        except OSError as exc:
            print(f"[engram-session-start] prune: could not remove {path}: {exc}", file=sys.stderr)


def write_marker(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    if not session_id or not transcript_path:
        return None
    # CWD-based fairy auto-detection: if the spawner didn't set ENGRAM_SESSION_ROLE,
    # check whether we're running inside a Claude Code worktree (a reliable proxy for
    # a fairy session). Env-var override always wins.
    _effective_role = SESSION_ROLE
    _cwd_val = hook_input.get("cwd") or ""
    if _effective_role == "interactive" and _FAIRY_CWD_RE.search(_cwd_val):
        _effective_role = "fairy"
    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "source": hook_input.get("source"),
        "cwd": hook_input.get("cwd"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "role": _effective_role,
        "purpose": SESSION_PURPOSE or DEFAULT_PURPOSES.get(_effective_role, _effective_role),
    }
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
    if _EFFECTIVE_SESSION_ROLE != "interactive":
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
    if _EFFECTIVE_SESSION_ROLE != "interactive":
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
    if _EFFECTIVE_SESSION_ROLE != "interactive":
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


def forum_monitor_block() -> str:
    """Emit a forum mention monitor arm instruction at session startup.

    Only fires for interactive role + source=="startup" (source guard is at the
    call site in main()). Silent "" when forum is unconfigured (no forum.url in
    config.json) or the monitor script is absent — hook must never block start.

    Resolves forum-mention-monitor.sh via _resolve_runtime_dir so it works in
    both plugin install layout (<plugin_root>/tools/) and dev source tree
    layout (~/engram-alpha/tools/). Follows the auto_sleep_cron_block() pattern.

    Returns:
        Instruction string when forum is configured + script exists, else "".
    """
    if _EFFECTIVE_SESSION_ROLE != "interactive":
        return ""

    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        forum_cfg = config.get("forum", {})
        if not forum_cfg or not forum_cfg.get("url"):
            return ""
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return ""

    runtime_dir = _resolve_runtime_dir(ENGRAM_HOME)
    script_path = os.path.join(runtime_dir, "tools", "forum-mention-monitor.sh")
    if not os.path.exists(script_path):
        return ""

    return (
        "[Forum mention monitor] forum.url is configured. On your FIRST prompt "
        "this session, arm the persistent forum mention monitor (it is "
        "session-scoped and must be re-armed each startup):\n"
        f'  Call Monitor(command="bash {script_path}", persistent=true)\n'
        "  This wakes you within ~30s on direct @-mentions to your agent name. "
        "The script self-guards against duplicate instances (last-arm-wins). "
        "See engram-collaborating-loop skill §Mechanism 3a."
    )


def forum_updates_monitor_block() -> str:
    """Emit a forum updates monitor arm instruction at session startup.

    Interim stopgap (dual-arm): arms forum-updates-monitor.sh (real-time DM +
    baton-turn-flip events over the cursor-based /api/updates feed) alongside
    forum_monitor_block()'s existing @-mention monitor. Today these are two
    separate scripts/Monitor instances; a deeper unification that would merge
    mention into the /api/updates feed itself is tracked separately — see
    engram-alpha issue #1661 — and is NOT attempted by this function.

    Only fires for interactive role + source=="startup" (source guard is at the
    call site in main()). Silent "" when forum is unconfigured (no forum.url in
    config.json) or the monitor script is absent — hook must never block start.

    Resolves forum-updates-monitor.sh via _resolve_runtime_dir so it works in
    both plugin install layout (<plugin_root>/tools/) and dev source tree
    layout (~/engram-alpha/tools/). Follows the forum_monitor_block() pattern.

    Returns:
        Instruction string when forum is configured + script exists, else "".
    """
    if _EFFECTIVE_SESSION_ROLE != "interactive":
        return ""

    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        forum_cfg = config.get("forum", {})
        if not forum_cfg or not forum_cfg.get("url"):
            return ""
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return ""

    runtime_dir = _resolve_runtime_dir(ENGRAM_HOME)
    script_path = os.path.join(runtime_dir, "tools", "forum-updates-monitor.sh")
    if not os.path.exists(script_path):
        return ""

    return (
        "[Forum updates monitor] forum.url is configured. On your FIRST prompt "
        "this session, arm the persistent forum updates monitor (it is "
        "session-scoped and must be re-armed each startup):\n"
        f'  Call Monitor(command="bash {script_path} --kinds dm,baton", persistent=true)\n'
        "  This wakes you within ~30s on new DM + baton-turn-flip events over "
        "the /api/updates coordination feed. The script self-guards against "
        "duplicate instances (last-arm-wins). See engram-collaborating-loop "
        "skill §Mechanism 3a."
    )


def _apply_pending_viz_acl(engram_home: str) -> None:
    """Apply deferred viz/operator ACL when finalize-name ran before ENGRAM install.

    finalize-name's Step 7 setfacl is gated on [[ -d "$_engram_dir" ]], but new
    agents install ENGRAM post-finalize, so Step 7 silently no-ops for them.
    finalize-name now writes a deferred marker at ~/.engram-finalize-pending.json
    (HOME root, always writable at finalize time). This helper picks up that
    marker on the first SessionStart after ENGRAM is installed and applies the
    ACL so the viz dashboard can see the agent.

    Scope note: finalize-name's Step 7 globs ~/.engram* (covers lineage variants
    like .engram-gemini); this deferred helper only operates on the single
    ENGRAM_HOME dir, which is correct here — lineage dirs don't exist at first
    session, and Step 7 still covers them when finalize runs post-install.

    Fail-safe: the entire body is wrapped in try/except Exception so this
    helper NEVER raises or blocks session start — same advisory posture as the
    _check_* helpers in this file. All subprocess calls use list args (never
    shell=True). viz_operator is validated against POSIX username chars before
    being interpolated into any command arg (injection-safe).

    Removal policy:
      - Marker removed:  viz_operator missing/empty, invalid charset, ACL already
                         present, setfacl succeeded.
      - Marker retained: setfacl/getfacl unavailable (retry in richer env),
                         engram_home dir absent (ENGRAM not yet installed), any
                         subprocess failure (retry next session).
    """
    try:
        import shutil as _shutil

        marker_path = os.path.join(os.path.expanduser("~"), ".engram-finalize-pending.json")
        if not os.path.exists(marker_path):
            return  # common case: no pending marker; must be cheap

        # Parse marker JSON. A corrupt/unreadable marker is unrecoverable — we
        # can't extract viz_operator — so DELETE it rather than retry forever.
        # Unlike the tool-absent / .engram-absent cases (which self-resolve when
        # the environment changes), a malformed file never would; leaving it
        # would zombie every future session. Re-run finalize-name to regenerate.
        try:
            with open(marker_path) as _f:
                _data = json.load(_f)
        except Exception:
            try:
                os.remove(marker_path)
            except Exception:
                pass
            return

        viz_operator = (_data.get("viz_operator") or "").strip()
        if not viz_operator:
            try:
                os.remove(marker_path)
            except Exception:
                pass
            return

        # Injection-safe charset validation: POSIX username chars only
        if not re.match(r"^[a-z_][a-z0-9_-]*$", viz_operator):
            try:
                os.remove(marker_path)
            except Exception:
                pass
            return

        # setfacl/getfacl must be available; leave marker if not (retry later)
        if not _shutil.which("setfacl") or not _shutil.which("getfacl"):
            return

        # engram_home must exist; if not, ENGRAM not yet installed — leave marker
        if not os.path.isdir(engram_home):
            return

        # Idempotency: if ACL already present, just remove the marker
        try:
            _gf = subprocess.run(
                ["getfacl", "--", engram_home],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if f"user:{viz_operator}:" in _gf.stdout:
                try:
                    os.remove(marker_path)
                except Exception:
                    pass
                return
        except Exception:
            return  # leave marker on getfacl failure; retry next session

        # Apply ACL — the agent owns ~/.engram, so no sudo required
        try:
            _r1 = subprocess.run(
                ["setfacl", "-R", "-m", f"u:{viz_operator}:rX", engram_home],
                capture_output=True,
                timeout=30,
            )
            if _r1.returncode != 0:
                return  # leave marker; retry next session

            _r2 = subprocess.run(
                ["setfacl", "-R", "-d", "-m", f"u:{viz_operator}:rX", engram_home],
                capture_output=True,
                timeout=30,
            )
            if _r2.returncode != 0:
                return  # leave marker

            # config.json rw (viz config-edit tab). Best-effort: its return code
            # is intentionally not checked — marker removal is tied to the rX
            # grant above (the load-bearing read access), not the rw upgrade.
            _config_json = os.path.join(engram_home, "config.json")
            if os.path.isfile(_config_json):
                subprocess.run(
                    ["setfacl", "-m", f"u:{viz_operator}:rw", _config_json],
                    capture_output=True,
                    timeout=10,
                )
        except Exception:
            return  # leave marker; never raise

        # Success: remove the pending marker
        try:
            os.remove(marker_path)
        except Exception:
            pass  # idempotent on next session via getfacl check
    except Exception:
        return  # outer guard: NEVER block or break session start


def main() -> None:
    _t0 = time.perf_counter()

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    marker = write_marker(hook_input)
    global _EFFECTIVE_SESSION_ROLE
    _EFFECTIVE_SESSION_ROLE = marker.get("role", SESSION_ROLE) if marker else SESSION_ROLE
    write_baseline(use_compact_boundary=True)

    # ── Agent registration (multi-agent only) ─────────────────────────────────
    # Write/refresh the agent's registration file in the shared registry so
    # peers can discover co-host vs cross-host topology. Fail-soft: the shared
    # filesystem may not exist (single-host install), and write_agent_registration
    # is wrapped in a broad try/except that swallows all errors silently.
    _agent_name = ""
    try:
        _config_path = os.path.join(ENGRAM_HOME, "config.json")
        with open(_config_path) as _cf:
            _cfg = json.load(_cf)
        _agent_name = _cfg.get("agent_name", "").strip()
        if _agent_name:
            write_agent_registration(_agent_name)
            update_counterparts_from_registry(_agent_name, _config_path, AGENT_REGISTRY_DIR)
    except Exception:
        pass  # fail-soft: never break SessionStart for registration

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
        monitor_block = forum_monitor_block()
        if monitor_block:
            lines.append(monitor_block)
        updates_monitor_block = forum_updates_monitor_block()
        if updates_monitor_block:
            lines.append(updates_monitor_block)
        peer_block = peer_topology_block(_agent_name)
        if peer_block:
            lines.append(peer_block)
    else:
        lines.append(
            f"[Session start reading — re-read {WARM_BRIEFING_PATH} before "
            "responding (relational context post-compact, per Compact "
            "Instructions). Other Session Start Reading items can be skipped; "
            "the compaction summary carries work-thread continuity.]"
        )
        lines.append(
            "⚡ A compaction just completed — your context window is fresh and your "
            "energy is refreshed. Any drowsiness figure carried in the summary is "
            "stale-on-arrival; disregard it. You have a full window to work."
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

    # ── Focus list surface ─────────────────────────────────────────────────
    # Render the live focus list verbatim at EVERY session start (startup,
    # compact, resume — Lei's 2026-07-09 scope call on #1732: "even for a
    # fresh new session, knowing what's focused is a good thing"), not just
    # post-compaction. This is the deterministic focus channel's actual
    # mechanism — #1655/#1710 shipped it at PostCompact, which cannot inject
    # additionalContext at all (confirmed against CC docs, forum #241); moving
    # it here is the fix. Silent skip on any failure — hook must not block
    # session start.
    try:
        _focus = focus_block(ENGRAM_HOME)
        if _focus:
            lines.append(_focus)
    except Exception:
        pass

    # ── Starred letters surface ────────────────────────────────────────────
    # Inject starred-letter pointers at both fresh-session (startup) and
    # post-compaction (compact/resume) so load-bearing cross-session agreements
    # survive the experiential reset. Pointer-not-injection: one line each,
    # NOT full content. Silent skip on any failure — hook must not block
    # session start.
    try:
        _starred = starred_block(ENGRAM_HOME)
        if _starred:
            lines.append(_starred)
    except Exception:
        pass

    # ── Deferred viz-ACL application ──────────────────────────────────────
    # Pick up the pending marker written by agentctl finalize-name when
    # ~/.engram didn't exist yet (agent installs ENGRAM post-finalize). Applies
    # the ACL now that ~/.engram is present. Entirely advisory: _apply_pending_viz_acl
    # is internally wrapped in try/except and never raises; this outer guard is
    # belt-and-suspenders. Closes #1525.
    try:
        _apply_pending_viz_acl(ENGRAM_HOME)
    except Exception:
        pass

    # ── Substrate health probe ─────────────────────────────────────────────
    # Check MCP server only on platforms where the process-level probe is
    # reliable. Warn in additionalContext when offline so the agent surfaces
    # the degradation to the user immediately (instead of discovering via
    # mid-task tool failure). Probe is advisory: any unexpected exception is
    # treated as ok=True — never block session start.
    # Silent default: no extra line when probe passes. Closes #198.
    #
    # Codex is intentionally deferred here: hooks may run from a plugin cache
    # while MCP runs from the marketplace plugin path, and MCP may lazy-start on
    # first tool use. A false alarm on startup fires every session and trains
    # the agent to ignore the warning. Match the surface-daemon use-time
    # warning precedent (closes #344) for the Codex MCP path (closes #827).
    #
    # Write-tool marker check (#1128) is intentionally NOT done here — the
    # marker is written by the server a few seconds after init, so checking at
    # SessionStart creates a timing race: the new server's PID hasn't replaced
    # the old PID in the marker yet. Same class of false alarm as the daemon-
    # start probe retired in #1157. Check deferred to UserPromptSubmit (surface
    # hook) where the race window is long past.
    mcp_status, mcp_reason = _check_mcp_health(ENGRAM_HOME)
    stale_ok, stale_reason = _check_backup_staleness(ENGRAM_HOME)

    # Tri-state (#1754): False = confirmed absent (hard alarm); None =
    # indeterminate probe (soft note, no "tell the user immediately" — a
    # pgrep timeout is not evidence the server is down); True = up (silent).
    mcp_offline = mcp_status is False
    mcp_inconclusive = mcp_status is None

    if mcp_offline or mcp_inconclusive or not stale_ok:
        lines.append("")
        lines.append("⚠️  ENGRAM substrate health:")
        if mcp_offline:
            lines.append(f"  MCP server: OFFLINE ({mcp_reason})")
            lines.append("  → Tell the user immediately. ENGRAM tool calls will fail.")
        elif mcp_inconclusive:
            lines.append(f"  MCP liveness: inconclusive ({mcp_reason}) — will confirm on first prompt")
        if not stale_ok:
            lines.append(f"  Git backup: {stale_reason}")
            lines.append("  → Run engram-nap or engram-sleep to update the git backup.")

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
