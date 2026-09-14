"""viz.data.stats — the 5-panel stats data assembly for /api/stats
(engram-alpha#2555 PR A, pure relocation of viz_server.py's
``get_stats_data``/``_resolve_logs_index``, byte-identical function
LOGIC -- two deliberate reference-form changes, noted below).

No HTTP, no HTML -- unit-testable in isolation.

CROSS-MODULE NOTE: ``_resolve_logs_index`` reads the package-wide
``AGENTS`` registry, which lives in ``viz.agents`` and is REASSIGNED
(not mutated in place) whenever the agent cache refreshes
(``viz.agents._refresh_agents_if_stale``) or the server restarts
(``viz.server.main``). A bound ``from viz.agents import AGENTS`` would
capture the dict object that existed at IMPORT time and go stale the
moment agents.py reassigns its own module-level name to a new dict --
so this module uses ``viz_agents.AGENTS`` (attribute access on the
module object, always reads the CURRENT value) instead. This is the
one place this file's logic differs from a pure copy-paste of the
original -- the ORIGINAL used a bare ``AGENTS`` because it lived in the
same module as the reassignment; splitting into separate files makes
that binding-staleness hazard real, so it needed fixing here, not
merely relocating.

sys.path / sibling-file NOTE (same hazard class as data/config.py's
``_find_tools_parent`` -- see that module's docstring for the full
account): the original ``get_stats_data``'s no-index.db error message
referenced ``_THIS_DIR`` -- ``os.path.dirname(os.path.abspath(__file__))``
of ``viz_server.py`` itself, the directory that also holds
``engram_log_indexer.py`` as a direct sibling. This file sits at
``viz/data/stats.py``, two levels deeper, so counting ``__file__``'s own
directory no longer finds that sibling. Uses the same walk-parents
search pattern as ``_find_tools_parent`` (looking for the actual target
file, ``engram_log_indexer.py``, rather than assuming a fixed depth) --
found missing during PR A's own test run (``_THIS_DIR`` was referenced
at its original call site but never carried over when this file was
split out), fixed here rather than left broken.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from .. import agents as viz_agents

try:
    from engram_log_indexer import Indexer as _Indexer  # noqa: E402
    _INDEXER_AVAILABLE = True
except ImportError:
    _INDEXER_AVAILABLE = False
    _Indexer = None  # type: ignore[assignment]


def _find_indexer_dir(start: Path) -> Path | None:
    """Walk upward from *start* to find the nearest ancestor directory that
    itself contains ``engram_log_indexer.py`` -- the directory to report in
    the "index.db missing" error hint (``python3 <dir>/engram_log_indexer.py``).
    Works whether this module is running from the source tree
    (``src/engram/viz/data/stats.py``, target ``src/engram/``) or a
    built/deployed plugin tree (``plugin_root/viz/data/stats.py``, target
    ``plugin_root/``) -- see data/config.py's ``_find_tools_parent`` for the
    identical pattern applied to a different sibling file. Returns ``None``
    if not found, OR if every candidate directory is unreadable --
    ``Path.is_file()`` raises ``OSError`` (not ``False``) in that case, and
    this function runs at bare module-import time, so the walk must never
    raise (same never-raises shape as _hooklib.resolve_runtime_dir,
    engram-alpha#2671). Guards PER CANDIDATE, not the whole walk in one
    try/except: if the nearest candidate happens to be the unreadable one,
    a whole-walk guard would abort before ever checking a further,
    normally-readable ancestor -- discarding a candidate that would have
    resolved (engram-alpha#2671 review round). The caller falls back to
    ``Path(__file__).resolve().parent`` (this file's own directory) in
    either not-found case -- the same not-quite-right-but-harmless hint
    the original would give if run from an unexpected location, never
    raises.
    """
    for candidate in [start, *start.parents]:
        try:
            found = (candidate / "engram_log_indexer.py").is_file()
        except OSError:
            continue
        if found:
            return candidate
    return None


_THIS_DIR = _find_indexer_dir(Path(__file__).resolve()) or Path(__file__).resolve().parent


def _resolve_logs_index(agent_name: str) -> str:
    """Resolve agent_name to its index.db path. Returns empty string if unknown."""
    agent_meta = viz_agents.AGENTS.get(agent_name)
    if not agent_meta:
        return ""
    # AGENTS metadata stores the agent's knowledge-db path under key "db"
    # (per viz_server.py line 33 module-level comment + lines 1758, 1778, 1786,
    # 2746). The logs index.db lives at $ENGRAM_HOME/logs/index.db where
    # $ENGRAM_HOME is the parent of the knowledge.db file.
    db_path = agent_meta.get("db", "")
    if not db_path:
        return ""
    engram_home = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(engram_home, "logs", "index.db")


def get_stats_data(index_db_path: str) -> dict:
    """Run the 5 stats panels against the index.db. Returns a dict the
    Stats HTML page renders. Empty-data states are explicit so the UI can
    render a 'No events yet' placeholder rather than a blank card.
    """
    if not index_db_path or not os.path.exists(index_db_path):
        # Auto-bootstrap: run the indexer on-demand when index.db is absent.
        # Blocks the single-threaded HTTPServer for the indexing pass, but this
        # is a one-time cost per server lifecycle — once index.db exists, this
        # branch is never re-entered for the lifetime of the process.
        if _INDEXER_AVAILABLE and index_db_path:
            try:
                _Indexer.run_once(logs_dir=Path(index_db_path).parent)
            except Exception as exc:
                logging.warning("stats auto-run failed: %s", exc)
        if not index_db_path or not os.path.exists(index_db_path):
            indexer_path = os.path.join(_THIS_DIR, "engram_log_indexer.py")
            return {
                "empty": True,
                "reason": (
                    "No index.db at expected location yet — has the indexer run? "
                    f"Try: python3 {indexer_path}"
                ),
            }
    try:
        # mode=ro&immutable=1 (#2571): index.db uses WAL journal mode (#879).
        # A plain mode=ro reader still opens the writable -shm sidecar for
        # WAL locking -- fine for a same-user reader, but this endpoint
        # reads OTHER agents' index.db files, and this process cannot write
        # another user's -shm file: the first conn.execute() below raised
        # "attempt to write a readonly database", and with no except on the
        # query block (see commit history / #2571) that propagated out of
        # the handler and dropped the connection with no HTTP response --
        # the "Failed to fetch" defect. immutable=1 tells sqlite the file
        # will not change during this connection's lifetime, so it never
        # opens -shm/-wal at all, sidestepping the write attempt entirely.
        #
        # Safe because of TWO preconditions, both load-bearing and neither
        # obvious from this line alone:
        #
        # 1. The indexer drains the WAL after every pass via PRAGMA
        #    wal_checkpoint(TRUNCATE) (see engram_log_indexer.py's
        #    _checkpoint_wal), so committed rows normally live in the main db
        #    file rather than in an un-checkpointed WAL this reader would
        #    skip. BEST-EFFORT, not a guarantee: under contention that pragma
        #    returns busy WITHOUT raising and leaves -wal non-empty (two
        #    independent synthetic rigs agree on the mechanism and differ on
        #    the figures -- (1, 3, 3)/12,392 bytes and (1, 5, 5)/20,632 bytes
        #    -- so the reproducible fact is "busy, and -wal survives", not any
        #    particular count). A reader landing inside that
        #    window sees a SHORT COUNT, never corruption or an error -- so if
        #    /api/stats ever reports numbers lower than expected, check the
        #    indexer's stderr for the busy warning before suspecting the
        #    query. The state self-heals on the next pass.
        #
        # 2. This connection is per-request and short-lived: it is opened
        #    here, used, and closed in the `finally` below, with exactly one
        #    caller (viz/server.py's /api/stats handler). immutable=1 promises
        #    sqlite the file will not change for the connection's lifetime;
        #    a pooled or cached connection would hold that promise across
        #    writes it cannot see and would go SILENTLY STALE -- returning
        #    old numbers with no error. So this is not merely "we happen to
        #    open per request" -- per-request lifetime is what makes
        #    immutable=1 correct here, and pooling this connection later
        #    would break it invisibly.
        conn = sqlite3.connect(f"file:{index_db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"empty": True, "reason": f"Could not open index.db: {exc}"}
    try:
        # Confirm events table exists (indexer hasn't run if it doesn't)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        )
        if not cur.fetchone():
            return {"empty": True, "reason": "events table not found in index.db — has the indexer run?"}
        # Total L1 events (cardinality check)
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE level = 1")
        total = cur.fetchone()[0]
        if total == 0:
            return {"empty": True, "reason": "Index has no L1 events yet — start using ENGRAM to generate metrics."}

        out: dict = {"total_events": total}

        # Panel 1: auto-surface health (last 7 days)
        cur = conn.execute(
            """SELECT COUNT(*) AS fires,
                      MIN(daemon_latency_ms) AS min_ms,
                      AVG(daemon_latency_ms) AS avg_ms,
                      MAX(daemon_latency_ms) AS max_ms,
                      SUM(fallback_to_fts) AS fallback_count
               FROM events
               WHERE event_type = 'engram.surface.fire'
                 AND level = 1
                 AND ts > datetime('now', '-7 days')"""
        )
        row = cur.fetchone()
        out["surface"] = {
            "fires_7d": row["fires"] or 0,
            "latency_min_ms": row["min_ms"],
            "latency_avg_ms": round(row["avg_ms"], 1) if row["avg_ms"] is not None else None,
            "latency_max_ms": row["max_ms"],
            "fallback_count_7d": row["fallback_count"] or 0,
            "fallback_pct": (100.0 * (row["fallback_count"] or 0) / row["fires"]) if row["fires"] else 0.0,
        }

        # Panel 2: hook fire profile (last 7 days, by hook_name)
        cur = conn.execute(
            """SELECT hook_name,
                      COUNT(*) AS fires,
                      AVG(hook_duration_ms) AS avg_duration_ms,
                      SUM(CASE WHEN hook_exit_code != 0 THEN 1 ELSE 0 END) AS error_count
               FROM events
               WHERE event_type = 'engram.hook.fire'
                 AND level = 1
                 AND ts > datetime('now', '-7 days')
                 AND hook_name IS NOT NULL
               GROUP BY hook_name
               ORDER BY fires DESC"""
        )
        out["hooks"] = [
            {
                "hook_name": r["hook_name"],
                "fires_7d": r["fires"],
                "avg_duration_ms": round(r["avg_duration_ms"], 1) if r["avg_duration_ms"] is not None else None,
                "error_count_7d": r["error_count"],
            }
            for r in cur.fetchall()
        ]

        # Panel 3: tool decision context (last 7 days)
        cur = conn.execute(
            """SELECT tool_name, result_status, COUNT(*) AS n
               FROM events
               WHERE event_type = 'engram.tool.engram_call'
                 AND level = 1
                 AND ts > datetime('now', '-7 days')
                 AND tool_name IS NOT NULL
               GROUP BY tool_name, result_status
               ORDER BY tool_name, n DESC"""
        )
        out["tools"] = [
            {"tool_name": r["tool_name"], "result_status": r["result_status"], "n": r["n"]}
            for r in cur.fetchall()
        ]

        # Panel 4: cohort-aligned (last 10 turns).
        # Both the outer query AND the subquery must filter WHERE level = 1
        # for the privacy invariant. Round-1 fairy caught the missing level
        # filter on the subquery (would shift the window anchor if a future
        # L2 event happened to carry a higher turn than L1 events).
        cur = conn.execute(
            """SELECT turn, COUNT(*) AS events,
                      SUM(CASE WHEN event_type = 'engram.surface.fire' THEN 1 ELSE 0 END) AS surface_fires,
                      SUM(CASE WHEN event_type = 'engram.tool.engram_call' THEN 1 ELSE 0 END) AS tool_calls,
                      SUM(CASE WHEN event_type = 'engram.hook.fire' THEN 1 ELSE 0 END) AS hook_fires
               FROM events
               WHERE level = 1
                 AND turn > (SELECT COALESCE(MAX(turn), 0) - 10 FROM events WHERE level = 1)
               GROUP BY turn
               ORDER BY turn DESC"""
        )
        out["cohort"] = [
            {
                "turn": r["turn"],
                "events": r["events"],
                "surface_fires": r["surface_fires"],
                "tool_calls": r["tool_calls"],
                "hook_fires": r["hook_fires"],
            }
            for r in cur.fetchall()
        ]

        # Panel 5: recent errors (last 24h, capped at 25)
        cur = conn.execute(
            """SELECT ts, event_type, tool_name, hook_name, result_status, hook_exit_code
               FROM events
               WHERE level = 1
                 AND ts > datetime('now', '-1 day')
                 AND (
                   (event_type = 'engram.tool.engram_call' AND result_status IN ('error', 'blocked_tainted', 'blocked_stale', 'rejected'))
                   OR (event_type = 'engram.hook.fire' AND hook_exit_code IS NOT NULL AND hook_exit_code != 0)
                 )
               ORDER BY ts DESC
               LIMIT 25"""
        )
        out["errors"] = [
            {
                "ts": r["ts"],
                "event_type": r["event_type"],
                "tool_name": r["tool_name"],
                "hook_name": r["hook_name"],
                "detail": r["result_status"] if r["event_type"] == "engram.tool.engram_call" else f"exit_code={r['hook_exit_code']}",
            }
            for r in cur.fetchall()
        ]

        return out
    except sqlite3.Error as exc:
        return {"empty": True, "reason": f"Stats query failed: {exc}"}
    finally:
        conn.close()
