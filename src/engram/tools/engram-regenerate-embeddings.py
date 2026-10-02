#!/usr/bin/env python3
"""engram-regenerate-embeddings.py — backfill NULL embeddings after a git restore.

Usage:
    python tools/engram-regenerate-embeddings.py [--engram-home PATH]

When to run:
    After restoring knowledge.db from the git-tracked SQL dump:
        sqlite3 ~/.engram/knowledge.db < ~/.engram/knowledge.sql
    The dump excludes embeddings (they are regenerable and their inclusion
    caused ~37 MB churn per commit). This script backfills them so semantic
    search works again.

What it does:
    For every is_current=1 node with embedding IS NULL, computes the embedding
    from the claim text (using server.py's _embedding_text_for_node + the same
    sentence-transformers model the server uses) and writes it back. Also
    updates the vec_nodes KNN index if sqlite-vec is available.

Idempotent:
    Only touches nodes where embedding IS NULL — safe to run multiple times.
    Nodes that already have an embedding are skipped.

Progress:
    Logs to stderr: total to backfill, count per batch, final summary.

Restore procedure (full):
    1. sqlite3 ~/.engram/knowledge.db < ~/.engram/knowledge.sql
    2. python tools/engram-regenerate-embeddings.py
    3. Restart the MCP server (so it reloads the fresh DB + vec index).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate server.py — probe plugin runtime dir first, then scatter fallback,
# then repo-adjacent.  Priority order matters: on a plugin install the
# canonical server.py lives at engram_home/marketplace/plugins/engram/;
# the legacy scatter copy at engram_home/server.py is an inert leftover that
# may not be current (and is absent on a clean plugin install).
# ---------------------------------------------------------------------------

def _find_server_py(engram_home: Path) -> Path | None:
    """Find server.py in priority order.

    Checks in priority order:
    1. Plugin runtime dir (engram_home/marketplace/plugins/engram/) — canonical
       on plugin installs; preferred over any scatter leftover.
    2. ENGRAM_HOME directly (engram_home/server.py) — scatter leftover or a
       non-standard layout; absent on a clean plugin install.
    3. Repo root (parent of tools/) — dev-repo layout fallback.
    """
    candidates = [
        engram_home / "marketplace" / "plugins" / "engram" / "server.py",
        engram_home / "server.py",
        Path(__file__).resolve().parent.parent / "server.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _find_engram_backup_dir(engram_home: Path, server_py: Path) -> Path | None:
    """Find the directory that contains a current engram_backup.py.

    Checks in priority order:
    1. Plugin runtime dir (engram_home/marketplace/plugins/engram/) — canonical
       on plugin installs; preferred even when a stale scatter server.py was
       found by _find_server_py.
    2. Same dir as the located server.py — correct for dev-repo layout and for
       any install where server.py and engram_backup.py co-deploy together.
    3. Repo root (parent of tools/) — fallback for dev runs without ENGRAM_HOME.
    """
    candidates = [
        engram_home / "marketplace" / "plugins" / "engram",
        server_py.parent,
        Path(__file__).resolve().parent.parent,
    ]
    for d in candidates:
        if (d / "engram_backup.py").exists():
            return d
    return None


def _import_server(engram_home: Path):
    """Import server.py and configure it to use engram_home."""
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
    # Ensure engram_backup.py is importable: find its directory and prepend it
    # to sys.path.  The plugin layout keeps engram_backup.py in the plugin
    # runtime dir (engram_home/marketplace/plugins/engram/), which may differ
    # from server_py.parent when _find_server_py resolves a stale scatter copy
    # of server.py from engram_home directly.
    backup_dir = _find_engram_backup_dir(engram_home, server_py)
    if backup_dir is not None and str(backup_dir) not in sys.path:
        sys.path.insert(0, str(backup_dir))
    import importlib
    # Purge server AND engram_core as a pair (#872 wave 1): the resolved
    # server tree must load its own matching core — a cached engram_core
    # from another tree/version would carry foreign path state and a
    # version-incoherent module pair.
    for _mod in ("server", "engram_core"):
        sys.modules.pop(_mod, None)
    import server
    server._configure_paths(engram_home)
    return server


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------

BATCH_SIZE = 50  # embed this many texts per sentence-transformers call


def _backfill_embeddings_core(conn, server, model_name: str, vec_available: bool) -> dict:
    """Core NULL-only backfill loop (#3075 C4).

    Assumes `conn` already has row_factory=sqlite3.Row set — this function's
    own dict-style row access (`r["id"]`) depends on it, same as it always
    has; unlike server._embedding_text_for_node (made independent of the
    caller's row_factory by #3075 C1), this loop is explicitly OUT of C1's
    scope to touch.

    Returns a dict with keys 'processed', 'embedded', 'skipped', 'failed':
      - 'processed': every is_current=1/embedding-IS-NULL node found at the
        start of the pass.
      - 'skipped': nodes with no embeddable text (evidence with no
        source_title, or empty claim) — expected, not a failure.
      - 'embedded': nodes whose vector was computed and written.
      - 'failed': nodes where embed_batch returned None for the whole batch,
        or an individual vector came back empty/None.

    Does NOT sys.exit — the caller (backfill_embeddings) owns the fail-loud
    exit-code contract, after printing its own summary.
    """
    null_count = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
    ).fetchone()[0]

    counts = {"processed": null_count, "embedded": 0, "skipped": 0, "failed": 0}
    if null_count == 0:
        return counts

    rows = conn.execute(
        "SELECT id FROM nodes WHERE is_current=1 AND embedding IS NULL ORDER BY id"
    ).fetchall()

    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch_ids = [r["id"] for r in rows[batch_start: batch_start + BATCH_SIZE]]
        texts = []
        valid_ids = []

        for node_id in batch_ids:
            text = server._embedding_text_for_node(conn, node_id)
            if text:
                texts.append(text)
                valid_ids.append(node_id)
            else:
                counts["skipped"] += 1

        if not texts:
            continue

        # Batch embed
        vectors = server._embedder.embed_batch(texts, model_name)
        if vectors is None:
            counts["failed"] += len(valid_ids)
            print(
                f"[regen] WARNING: embed_batch returned None for batch starting at {batch_start}",
                file=sys.stderr,
            )
            continue

        for node_id, vector in zip(valid_ids, vectors):
            if not vector:
                counts["failed"] += 1
                continue
            embedding_json = json.dumps(vector)
            conn.execute(
                "UPDATE nodes SET embedding = ? WHERE id = ?",
                (embedding_json, node_id),
            )
            # Mirror into vec_nodes KNN index if available
            if vec_available and len(vector) == 384:
                try:
                    import sqlite_vec as _sv2
                    conn.execute(
                        "DELETE FROM vec_nodes WHERE node_id = ?", (node_id,)
                    )
                    conn.execute(
                        "INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                        (node_id, _sv2.serialize_float32(vector)),
                    )
                except Exception:
                    pass  # vec_nodes missing or locked — JSON column is canonical

            counts["embedded"] += 1

        conn.commit()
        print(
            f"[regen] Batch {batch_start // BATCH_SIZE + 1}: "
            f"attempted {len(valid_ids)} node(s) "
            f"(cumulative embedded: {counts['embedded']}/{null_count})",
            file=sys.stderr,
        )

    return counts


def backfill_embeddings(engram_home: Path) -> None:
    db_path = engram_home / "knowledge.db"
    if not db_path.exists():
        print(
            f"ERROR: knowledge.db not found at {db_path}\n"
            "  Run: sqlite3 ~/.engram/knowledge.db < ~/.engram/knowledge.sql  first.",
            file=sys.stderr,
        )
        sys.exit(1)

    server = _import_server(engram_home)

    # Check embedder availability
    if not server._embedder.is_available():
        print(
            "ERROR: sentence-transformers is not installed. "
            "Install it with: pip install sentence-transformers",
            file=sys.stderr,
        )
        sys.exit(1)

    emb_config = server._get_embedding_config()
    if not emb_config.get("enabled", True):
        print(
            "WARNING: embeddings are disabled in config.json "
            "(embedding.enabled=false). Proceeding anyway — "
            "re-enable in config.json after restore if desired.",
            file=sys.stderr,
        )

    model_name = emb_config.get("model", server.DEFAULT_EMBEDDING_MODEL)
    print(f"[regen] Using model: {model_name}", file=sys.stderr)

    # Load model upfront (shows a clear error if the model isn't cached)
    server._embedder._load_model(model_name)
    if server._embedder._model is None:
        print(
            f"ERROR: could not load embedding model '{model_name}'. "
            "Check that the model is cached locally via sentence-transformers.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Load sqlite-vec extension if available (mirrors server.py startup)
    try:
        import sqlite_vec as _sv
        conn.enable_load_extension(True)
        _sv.load(conn)
        vec_available = True
    except Exception:
        vec_available = False

    try:
        # #3075 C4: the core NULL-only backfill loop now lives in
        # _backfill_embeddings_core (unit-testable in isolation) — this
        # function does setup, reports the outcome, and owns the fail-loud
        # exit-code contract.
        counts = _backfill_embeddings_core(conn, server, model_name, vec_available)

        if counts["processed"] == 0:
            print("[regen] No nodes with NULL embedding — nothing to do.", file=sys.stderr)
            import engram_backup  # available after _import_server inserted the backup module dir on sys.path
            if engram_backup.rebuild_fts_index(str(db_path)):
                print("[regen] FTS index rebuild complete.", file=sys.stderr)
            else:
                print("[regen] no nodes_fts table present — skipping FTS rebuild.", file=sys.stderr)
            print(
                "[regen] Done. processed=0 embedded=0 skipped=0 failed=0",
                file=sys.stderr,
            )
            # #3075 C5 round 2 (judgment call, documented in the PR handoff
            # — not explicitly named in Borges's ruling, but required to
            # close the loop for the MANUAL restore path the same way the
            # async server-side path does): nothing to do here means no
            # NULL embeddings exist, so any restore-completeness flag left
            # set by an earlier crashed/failed attempt is stale — clear it.
            try:
                # #3077 round 4 (Borges's colleague-pass required fix):
                # this script never acquires the regeneration lease
                # itself, so a fresh "regeneration-running" lease found
                # here is definitionally someone else's. round 2's
                # separate `if not fresh_lease_held(conn): <write>`
                # pre-check was a check-then-act TOCTOU (a lease acquired
                # in the window between the read and this write would be
                # silently clobbered) -- fixed by folding the guard into
                # the write's own atomic SQL instead
                # (respect_lease=True, see _clear_embedding_restore_flag's
                # docstring).
                server.core._clear_embedding_restore_flag(conn, respect_lease=True)
            except Exception:
                pass  # best-effort — never fail the script over flag bookkeeping
            return

        if counts["skipped"] > 0:
            print(
                "[regen] Nodes with no embeddable text (evidence with no source_title, "
                "or empty claim) were skipped — this is expected for some node types.",
                file=sys.stderr,
            )

        import engram_backup  # available after _import_server inserted the backup module dir on sys.path
        if engram_backup.rebuild_fts_index(str(db_path)):
            print("[regen] FTS index rebuild complete.", file=sys.stderr)
        else:
            print("[regen] no nodes_fts table present — skipping FTS rebuild.", file=sys.stderr)

        if counts["failed"] == 0:
            print(
                "[regen] Restart the MCP server to reload the vec index: "
                "pkill -f 'python.*server.py'",
                file=sys.stderr,
            )

        # #3075 C4: the final stderr line is ALWAYS the four-count summary —
        # printed last (after every other diagnostic above) so a reader/
        # script scanning stderr's tail sees the authoritative outcome.
        print(
            f"[regen] Done. processed={counts['processed']} "
            f"embedded={counts['embedded']} skipped={counts['skipped']} "
            f"failed={counts['failed']}",
            file=sys.stderr,
        )

        # #3075 C5 round 2 (judgment call, documented in the PR handoff —
        # same reasoning as the processed==0 branch above): close the loop
        # on the DURABLE restore-completeness flag for the manual restore
        # path, not just the automatic server-side one. "Regeneration
        # clears it on completion" (Borges's ruling) applies regardless of
        # WHICH regeneration path did the work.
        #
        # Round 3: this used to re-count remaining NULL rows directly and
        # set the flag unconditionally on any remainder — the same
        # skip-vs-fail bug _run_embedding_restore_regeneration had (see
        # _finalize_embedding_restore_flag's own docstring). Fixed by
        # routing through that same shared helper with the `counts` dict
        # already in scope here, so a permanent empty-text skip (e.g. an
        # evidence node with no source_title -- the exact case the print
        # a few lines above already calls "expected") no longer leaves
        # this flag permanently stuck with an unhelpful remedy.
        try:
            # #3077 round 4 (Borges's colleague-pass required fix): same
            # reasoning as the processed==0 branch above -- this script
            # never acquires the lease itself, so the finalize write must
            # respect a genuinely fresh lease. respect_lease=True on THIS
            # call folds the guard into _finalize_embedding_restore_flag's
            # own atomic writes, rather than a separate check-then-act
            # pre-check around the whole call (round 2's TOCTOU).
            server.core._finalize_embedding_restore_flag(
                conn, counts, respect_lease=True,
            )
        except Exception:
            pass  # best-effort — never fail the script over flag bookkeeping

        if counts["failed"] > 0:
            # #3075 C4 / Lei's C5 ruling: fail LOUD, not silently — a
            # non-zero exit is the one signal a caller (a human, or a
            # wrapping script) cannot miss.
            sys.exit(1)

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backfill NULL embeddings after a git-restore of knowledge.db."
    )
    parser.add_argument(
        "--engram-home",
        default=os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"),
        help="Path to the ENGRAM data directory (default: $ENGRAM_HOME or ~/.engram)",
    )
    args = parser.parse_args()
    engram_home = Path(args.engram_home).expanduser().resolve()
    backfill_embeddings(engram_home)


if __name__ == "__main__":
    main()
