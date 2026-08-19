#!/usr/bin/env python3
"""engram-backfill-special-moment-edges.py — backfill missing `about` edges
for special_moments entries (#1948 item 4).

Usage:
    python tools/engram-backfill-special-moment-edges.py [--engram-home PATH]
    python tools/engram-backfill-special-moment-edges.py --dry-run

When to run:
    Any time — this is a one-time data-integrity repair for a historical
    defect, safe to re-run. Recommended once after upgrading to a build that
    includes #1948, to backfill any pre-existing gap on your own graph.

What it does:
    Every metadata.special_moments[].node_id on every live (type='person',
    is_current=1) person node is supposed to have an `about` edge to its
    person. The historical write path (_add_special_moment_impl) never
    guaranteed this — entries added without a prior engram_link_about call
    are silently missing the edge. #1948's own audit found this true for 4
    of 7 real moments on a live graph.

    This script calls _backfill_special_moment_about_edges(conn) in
    engram_trust.py, which creates the missing edges using the same
    insert-or-noop SQL pattern engram_cornerstone.py's _link_about_impl uses
    for the identical edge type.

Idempotent:
    Running this twice creates zero additional edges the second time —
    every insert is guarded by both an explicit existence check and the
    edges table's own UNIQUE(source_id, target_id, relation) constraint.

Progress and the final edge count are reported to stderr.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Locate server.py — mirrors engram-regenerate-embeddings.py's priority
# order (plugin runtime dir first, then scatter fallback, then repo-adjacent)
# so this script works the same way across plugin and dev-repo layouts.
#
# NOTE: engram-regenerate-embeddings.py's own repo-adjacent fallback assumes
# server.py sits directly at <repo_root>/server.py. In this repo's actual
# current layout, server.py lives at <repo_root>/src/engram/server.py (see
# tools/release_gates.py's own path construction for the same fact). Added
# as an extra candidate below rather than "fixing" the other script, which
# is out of this change's scope.
# ---------------------------------------------------------------------------

def _find_server_py(engram_home: Path) -> Path | None:
    candidates = [
        engram_home / "marketplace" / "plugins" / "engram" / "server.py",
        engram_home / "server.py",
        Path(__file__).resolve().parent.parent / "server.py",
        Path(__file__).resolve().parent.parent / "src" / "engram" / "server.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _import_server(engram_home: Path):
    """Import server.py (and its family modules) configured to use engram_home."""
    server_py = _find_server_py(engram_home)
    if server_py is None:
        print(
            "ERROR: could not locate server.py. "
            "Set ENGRAM_HOME or run from the repo root.",
            file=sys.stderr,
        )
        sys.exit(1)
    if str(server_py.parent) not in sys.path:
        sys.path.insert(0, str(server_py.parent))
    import importlib
    # Purge server AND engram_core as a pair (#872 wave 1 convention): the
    # resolved server tree must load its own matching core — a cached
    # engram_core from another tree/version would carry foreign path state.
    for _mod in ("server", "engram_core", "engram_trust"):
        sys.modules.pop(_mod, None)
    import server
    server._configure_paths(engram_home)
    return server


def run_backfill(engram_home: Path, dry_run: bool = False) -> int:
    """Run the special_moments about-edge backfill. Returns the edge count
    created (0 for --dry-run, which only reports what WOULD be created)."""
    db_path = engram_home / "knowledge.db"
    if not db_path.exists():
        print(f"ERROR: knowledge.db not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    server = _import_server(engram_home)
    import engram_core as core
    import engram_trust
    import json

    conn = core._get_db()
    try:
        if dry_run:
            # Mirror the scan half of _backfill_special_moment_about_edges
            # without writing, so --dry-run reports an accurate count.
            person_rows = conn.execute(
                "SELECT id, metadata FROM nodes WHERE type = 'person' AND is_current = 1"
            ).fetchall()
            would_create = 0
            for prow in person_rows:
                try:
                    meta = json.loads(prow["metadata"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                for moment in (meta.get("special_moments") or []):
                    if not isinstance(moment, dict):
                        continue
                    anchor_id = moment.get("node_id")
                    if not anchor_id:
                        continue
                    anchor_exists = conn.execute(
                        "SELECT 1 FROM nodes WHERE id = ?", (anchor_id,)
                    ).fetchone()
                    if not anchor_exists:
                        continue
                    already = conn.execute(
                        "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? "
                        "AND relation = 'about'",
                        (anchor_id, prow["id"]),
                    ).fetchone()
                    if not already:
                        would_create += 1
            print(
                f"[backfill] dry-run: would create {would_create} `about` edge(s).",
                file=sys.stderr,
            )
            return would_create

        created = engram_trust._backfill_special_moment_about_edges(conn)
        print(
            f"[backfill] done: created {created} `about` edge(s) for "
            f"special_moments entries that were missing one.",
            file=sys.stderr,
        )
        return created
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill missing `about` edges for special_moments entries "
            "(#1948 item 4)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--engram-home",
        default=os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"),
        help="Path to the ENGRAM data directory (default: $ENGRAM_HOME or ~/.engram)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report how many edges would be created without writing anything.",
    )
    args = parser.parse_args()
    engram_home = Path(args.engram_home).expanduser().resolve()
    run_backfill(engram_home, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
