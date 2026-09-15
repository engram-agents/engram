"""viz.data.health — the (v1) live health-metrics data assembly for
/api/health (engram-alpha#2555 PR A, pure relocation of viz_server.py's
``get_health_data``, byte-identical function body).

v1 only -- the health-v2 SSoT panel (engram-alpha#2518) lands as a NEW
module beside this one once #2555 merges, per the design's own
sequencing note ("health.py (v1; the health-v2 panel lands beside it --
#2518)").

No HTTP, no HTML -- unit-testable in isolation.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def get_health_data(db_path: str) -> dict:
    """Compute live health metrics + trend from diagnostic_history."""
    if not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path}"}
    try:
        # Read-only via mode=ro; multi-agent peer-DB safety is enforced by
        # OS file permissions (owner UID writes; group=agents reads), not by
        # this URI flag. The previous `immutable=1` was dropped (#184) because
        # it caused the reader to ignore -wal entirely → silent staleness.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        data: dict = {}

        # ── Live metrics ─────────────────────────────────────────────
        # Node counts by type
        type_rows = conn.execute(
            """SELECT type, COUNT(*) as total, SUM(is_current) as current_count
               FROM nodes GROUP BY type"""
        ).fetchall()
        type_counts = {}
        total_nodes = 0
        total_current = 0
        for r in type_rows:
            type_counts[r["type"]] = {"total": r["total"], "current": r["current_count"]}
            total_nodes += r["total"]
            total_current += r["current_count"]
        data["node_counts"] = type_counts
        data["total_nodes"] = total_nodes
        data["total_current"] = total_current

        # Edge counts
        edge_rows = conn.execute(
            "SELECT relation, COUNT(*) as c FROM edges GROUP BY relation"
        ).fetchall()
        data["edge_counts"] = {r["relation"]: r["c"] for r in edge_rows}
        data["total_edges"] = sum(r["c"] for r in edge_rows)

        # Confidence distribution
        conf_buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0,
                        "0.6-0.8": 0, "0.8-1.0": 0}
        for cr in conn.execute(
            "SELECT confidence FROM nodes WHERE is_current = 1 AND confidence IS NOT NULL"
        ).fetchall():
            c = cr["confidence"]
            if c < 0.2: conf_buckets["0.0-0.2"] += 1
            elif c < 0.4: conf_buckets["0.2-0.4"] += 1
            elif c < 0.6: conf_buckets["0.4-0.6"] += 1
            elif c < 0.8: conf_buckets["0.6-0.8"] += 1
            else: conf_buckets["0.8-1.0"] += 1
        data["confidence_distribution"] = conf_buckets

        # Issue counts. Migrated off metadata.tainted_by/stale_by to
        # cascade_markers (#2013/#2009 slice 1) — this is exactly the class
        # of drift Borges flagged on #2013 (a raw metadata LIKE-pattern
        # count silently going stale the moment the write path changes);
        # querying the real table structurally can't drift the same way.
        data["tainted"] = conn.execute(
            "SELECT COUNT(DISTINCT cm.node_id) as c FROM cascade_markers cm "
            "JOIN nodes n ON n.id = cm.node_id "
            "WHERE n.is_current = 1 AND cm.status = 'pending' AND cm.root_op = 'retract'"
        ).fetchone()["c"]
        data["stale"] = conn.execute(
            "SELECT COUNT(DISTINCT cm.node_id) as c FROM cascade_markers cm "
            "JOIN nodes n ON n.id = cm.node_id "
            "WHERE n.is_current = 1 AND cm.status = 'pending' AND cm.root_op = 'supersede'"
        ).fetchone()["c"]
        data["retracted"] = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE status = 'retracted'"
        ).fetchone()["c"]
        data["orphans"] = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n WHERE n.is_current = 1
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.source_id = n.id)
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = n.id)
        """).fetchone()["c"]

        # Memory tiers (read config for turn)
        config_path = Path(db_path).parent / "config.json"
        current_turn = 0
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
                current_turn = int(cfg.get("memory", {}).get("current_turn", 0))
            except Exception:
                pass
        data["current_turn"] = current_turn

        # Embedding coverage
        with_emb = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND embedding IS NOT NULL"
        ).fetchone()["c"]
        data["embedding_pct"] = round(with_emb / total_current * 100, 1) if total_current else 0

        # Feeling reports
        data["feeling_reports"] = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE type = 'feeling_report' AND is_current = 1"
        ).fetchone()["c"]

        # Resolution rates
        for ntype, label in [("question", "questions"), ("conjecture", "conjectures")]:
            total = conn.execute("SELECT COUNT(*) as c FROM nodes WHERE type = ?", (ntype,)).fetchone()["c"]
            resolved = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = ? AND status NOT IN ('open', 'active')",
                (ntype,)
            ).fetchone()["c"]
            data[f"{label}_total"] = total
            data[f"{label}_resolved"] = resolved

        # ── Edit history (last 7 days, grouped by day) ───────────────
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        try:
            daily_edits = conn.execute("""
                SELECT SUBSTR(timestamp, 1, 10) as day, action, COUNT(*) as c
                FROM edit_history WHERE timestamp > ?
                GROUP BY day, action ORDER BY day
            """, (week_ago,)).fetchall()
            edit_by_day: dict = {}
            for r in daily_edits:
                d = r["day"]
                if d not in edit_by_day:
                    edit_by_day[d] = {}
                edit_by_day[d][r["action"]] = r["c"]
            data["edit_by_day"] = edit_by_day
        except sqlite3.OperationalError:
            data["edit_by_day"] = {}

        # ── Diagnostic history trend ─────────────────────────────────
        try:
            snapshots = conn.execute(
                "SELECT turn, timestamp, metrics FROM diagnostic_history ORDER BY id DESC LIMIT 20"
            ).fetchall()
            trend = []
            for s in reversed(list(snapshots)):
                m = json.loads(s["metrics"])
                trend.append({
                    "turn": s["turn"],
                    "timestamp": s["timestamp"][:16],
                    "health_score": m.get("health_score", 0),
                    "total_nodes": m.get("structure", {}).get("total_current", 0),
                })
            data["health_trend"] = trend
        except sqlite3.OperationalError:
            data["health_trend"] = []

        # ── Compute live health score ────────────────────────────────
        # engram-alpha#2940: retires the OLD pre-v2 penalty formula
        # (engram_stats.py's now-retired penalty function) in favor of health-v2's own
        # composed score (health/engine.py) as the single source of truth
        # -- the same source engram_stats/engram_diagnose now read. There
        # is NO live-compute path for v2 -- it is per-checkpoint only -- so
        # this reads back the LAST WRITTEN checkpoint's score, never
        # recomputing one live.
        #
        # engram-alpha#2940 round 2 (reviewer-fairy blocker 2): this function
        # is called for ANY agent's db_path (multi-tenant viz), so the
        # snapshots dir MUST be derived from db_path, not from the calling
        # viz process's own DATA_DIR -- passing no argument here would
        # silently report the WRONG agent's score under this agent's
        # identity (the pre-fix regression, reproduced directly by the
        # reviewer). Same directory-layout convention
        # viz.data.health_v2._resolve_health_snapshots_dir already uses for
        # the SAME reason on /api/health/v2.
        from health import engine as health_engine  # lazy, see module docstring

        snapshots_dir = Path(db_path).resolve().parent / "health-snapshots"
        summary = health_engine.read_latest_composed_summary(snapshots_dir=snapshots_dir)
        if summary["has_snapshot"]:
            data["health_score"] = summary["score"]
            data["health_score_error"] = None
            data["health_score_detail"] = {
                "points_in_play": summary["points_in_play"],
                "unscored_aspects": summary["unscored_aspects"],
                "composed_coverage": summary["composed_coverage"],
                "snapshot_id": summary["snapshot_id"],
                "checkpoint_mode": summary["checkpoint_mode"],
                "written_at": summary["written_at"],
                "turn": summary["turn"],
            }
        else:
            # engram-alpha#2940 (was #2524, for the old ImportError-fallback
            # trigger): a missing/unreadable checkpoint is not a health
            # signal. The previous `health_score = 0.0` fallback fabricated
            # a "maximally unhealthy" reading indistinguishable from a real
            # one -- same class as the fabricated-neutral-default the #2520
            # hold-out ruling rejected at the aspect level (an unscored
            # aspect reports `unscored`, never a number). Render null + the
            # reason instead, loudly, so the panel can't be silently
            # misread as "the graph is fine, it just scored zero."
            data["health_score"] = None
            data["health_score_error"] = f"health_score unavailable: {summary['reason']}"
            data["health_score_detail"] = {"error": summary["reason"]}

        conn.close()
        return data

    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"error": "Schema not initialised yet"}
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}
