#!/usr/bin/env python3
"""Standing-population sweep for #3531.

``_recompute_node_confidence`` used to count a superseded premise T alongside
its acknowledged replacement S. The write-path fix covers only FUTURE
recomputes; every node that already carries an ``acknowledged_derives_from``
edge still holds a value computed the old way. This tool recomputes exactly
that population (and, through the cascade recompute, its descendants) with the
fixed premise set.

DRY-RUN BY DEFAULT: prints node, old -> new and the dropped premise(s), writes
nothing (the recompute runs inside a transaction that is rolled back).
``--apply`` commits the same writes, made through
``_recompute_node_confidence``. Idempotent: a second ``--apply`` changes
nothing.

Each changed row is labelled ``direct`` (the node's own premise set dropped a
premise) or ``descendant`` (changed only by propagation from a direct row). A
``descendant`` row may also reflect unrelated pre-existing drift swept in by
the descendant recompute, not only this fix. Back up knowledge.db before
``--apply`` on a live graph; the tool takes no backup.

NOTE: ``_recompute_node_confidence`` updates ``nodes.confidence`` only; it does
not append to ``confidence_history``. This tool does not add that.

Usage:
    python ack_recompute_sweep.py [--db PATH] [--apply]
    (PATH defaults to $ENGRAM_HOME/knowledge.db, else ~/.engram/knowledge.db)
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# src/engram (or the plugin root) holds the flat engram_* modules.
_ENGRAM_DIR = str(Path(__file__).resolve().parent.parent)
if _ENGRAM_DIR not in sys.path:
    sys.path.append(_ENGRAM_DIR)


def _default_db() -> Path:
    home = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    return Path(home) / "knowledge.db"


def sweep(conn: sqlite3.Connection, apply: bool = False) -> dict:
    """Recompute every current node with an `acknowledged_derives_from` edge,
    plus its descendants. Returns ``{"changes": [{node_id, old, new, dropped}],
    "population": N, "applied": bool}``. With ``apply=False`` the connection's
    transaction is rolled back, so nothing persists."""
    import engram_core as core
    import engram_revision as rev

    conn.row_factory = sqlite3.Row
    population = [r["source_id"] for r in conn.execute(
        "SELECT DISTINCT e.source_id FROM edges e "
        "JOIN nodes n ON n.id = e.source_id "
        "WHERE e.relation = 'acknowledged_derives_from' AND n.is_current = 1 "
        "ORDER BY e.source_id").fetchall()]

    dropped = {n: sorted(core._acknowledged_superseded_premises(conn, n))
               for n in population}
    first_old: dict = {}
    last_new: dict = {}
    converged = False
    try:
        # A node's value feeds other acknowledged nodes, so iterate to a fixed
        # point rather than trust one ordered pass.
        for _ in range(len(population) + 2):
            moved = False
            for n in population:
                res = rev._cascade_recompute_confidence_transitive(conn, n)
                for c in res["changed"]:
                    first_old.setdefault(c["node_id"], c["old"])
                    last_new[c["node_id"]] = c["new"]
                    moved = True
            if not moved:
                converged = True
                break
    except BaseException:
        conn.rollback()
        raise
    if apply:
        conn.commit()
    else:
        conn.rollback()

    changes = [
        {"node_id": nid, "old": first_old[nid], "new": last_new[nid],
         "dropped": dropped.get(nid, []),
         "kind": "direct" if dropped.get(nid) else "descendant"}
        for nid in sorted(last_new) if last_new[nid] != first_old[nid]
    ]
    return {"changes": changes, "population": len(population),
            "applied": bool(apply), "converged": converged}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    args = ap.parse_args(argv)
    db = args.db or _default_db()
    if not db.exists():
        print(f"no such database: {db}", file=sys.stderr)
        return 2
    # #3882: an offline repair tool. It holds the store-maintenance lock for its whole run, taken BEFORE
    # its first open (the dry run too: a read against a store being repaired is still a hazard), so an
    # owner and this tool exclude each other. Imported here, not at module level: importing this module
    # opens nothing.
    import engram_core as core
    lock_fd = core.acquire_store_maint_lock_or_report(db, "ack_recompute_sweep")
    if lock_fd is None:
        return core.STORE_MAINT_REFUSED_EXIT
    try:
        conn = sqlite3.connect(str(db))
        try:
            report = sweep(conn, apply=args.apply)
        finally:
            conn.close()
    finally:
        core.release_store_maint_lock(lock_fd)
    mode = "APPLIED" if args.apply else "DRY RUN (nothing written)"
    print(f"{mode}: {report['population']} acknowledged node(s) in the "
          f"standing population, {len(report['changes'])} change(s)")
    if not report["converged"]:
        print("WARNING: the fixed-point loop hit its iteration cap without "
              "converging; the values above may not be final.")
    for c in report["changes"]:
        drop = f"  dropped premise(s): {', '.join(c['dropped'])}" if c["dropped"] else ""
        print(f"  [{c['kind']}] {c['node_id']}: {c['old']} -> {c['new']}{drop}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
