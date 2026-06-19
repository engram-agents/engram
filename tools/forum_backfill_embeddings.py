"""Forum embedding backfill tool.

Usage:
    python tools/forum_backfill_embeddings.py --db /path/to/forum.db
    python tools/forum_backfill_embeddings.py --db /path/to/forum.db --dry-run

Idempotent three-phase backfill:

Phase 1 -- Post embeddings:
    Embeds all posts with embedding IS NULL using the forum embedding model
    (all-MiniLM-L6-v2, 384-dim, L2-normalized). Already-embedded posts are
    skipped -- the operation is safe to re-run.

Phase 2 -- Thread centroids:
    Recomputes ALL thread centroids as renormalized_mean(post embeddings for
    that thread). Uses renormalized_mean() from forum/embeddings.py, which is
    the canonical order-invariant recompute. This MAY DIFFER from the live
    incrementally-built centroids in threads.embedding: the incremental formula
    is a fast approximation that is order-dependent and exact only when a
    thread's post vectors are collinear. Divergence after backfill is expected,
    not a sign of corruption -- the centroid is a recall signal, not a
    reproducibility-critical value.

Phase 3 -- FTS sanity rebuild:
    Rebuilds the posts_fts FTS5 external-content table using INSERT INTO
    posts_fts(posts_fts) VALUES ('rebuild').

    FTS rebuild notes:
    - Bare 'rebuild' is safe here because forum posts have NO retraction-exclusion
      semantics. Every post in the posts table should be in FTS, so a full rebuild
      simply re-indexes all rows. This is distinct from the engram nodes_fts case
      (issue #727), where retracted nodes must be excluded from FTS -- a bare
      rebuild would corrupt those exclusions. Forum has no such constraint.
    - 'rebuild' is idempotent: re-running it is safe and costs a full re-scan.

Progress and counts are reported to stderr.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = "/home/agents-shared/forum/forum.db"


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension if available. Returns True on success."""
    try:
        import sqlite_vec  # type: ignore
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def run_backfill(db_path: str, dry_run: bool = False) -> dict[str, int]:
    """Run the three-phase backfill.

    Returns:
        dict with keys: posts_embedded, threads_updated, fts_rows_after_rebuild
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    vec_loaded = _load_vec_extension(conn)

    if vec_loaded:
        print("[backfill] sqlite-vec loaded -- vec0 indexes will be updated.", file=sys.stderr)
    else:
        print("[backfill] sqlite-vec unavailable -- skipping vec0 index updates.", file=sys.stderr)

    # Lazy import of the forum embedding module; requires sentence-transformers.
    # Add the repo root to sys.path so this script can be run from the tools/ dir.
    import os
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from forum.embeddings import (  # type: ignore
        EMBEDDING_DIM,
        available,
        deserialize,
        encode_batch,
        incremental_centroid,
        renormalized_mean,
        serialize,
    )

    if not available():
        print(
            "[backfill] ERROR: embedding layer is not available. "
            "Ensure sentence-transformers==5.3.0 is installed and "
            "FORUM_NO_EMBEDDINGS is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Phase 1: embed posts with embedding IS NULL
    # ------------------------------------------------------------------
    null_post_rows = conn.execute(
        "SELECT id, body_md FROM posts WHERE embedding IS NULL ORDER BY id"
    ).fetchall()

    posts_to_embed = [(r[0], r[1]) for r in null_post_rows]
    print(
        f"[backfill] Phase 1: {len(posts_to_embed)} posts need embedding.",
        file=sys.stderr,
    )

    posts_embedded = 0
    if posts_to_embed and not dry_run:
        texts = [body for _, body in posts_to_embed]
        vectors = encode_batch(texts)
        if vectors is None:
            print("[backfill] ERROR: encode_batch returned None unexpectedly.", file=sys.stderr)
            sys.exit(1)

        for (post_id, _), vector in zip(posts_to_embed, vectors):
            blob = serialize(vector)
            conn.execute(
                "UPDATE posts SET embedding = ? WHERE id = ?", (blob, post_id)
            )
            if vec_loaded:
                try:
                    conn.execute("DELETE FROM vec_posts WHERE post_id = ?", (post_id,))
                    conn.execute(
                        "INSERT INTO vec_posts(post_id, embedding) VALUES (?, ?)",
                        (post_id, blob),
                    )
                except sqlite3.OperationalError:
                    pass  # vec_posts table absent -- skip

            posts_embedded += 1
            if posts_embedded % 50 == 0:
                print(f"[backfill]   ... {posts_embedded}/{len(posts_to_embed)} posts embedded", file=sys.stderr)

        conn.commit()
        print(f"[backfill] Phase 1 complete: {posts_embedded} posts embedded.", file=sys.stderr)
    elif dry_run:
        print(f"[backfill] Phase 1 (dry-run): would embed {len(posts_to_embed)} posts.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Phase 2: recompute thread centroids (batch, all threads)
    # ------------------------------------------------------------------
    thread_rows = conn.execute("SELECT id FROM threads ORDER BY id").fetchall()
    thread_ids = [r[0] for r in thread_rows]
    print(
        f"[backfill] Phase 2: recomputing centroids for {len(thread_ids)} threads.",
        file=sys.stderr,
    )

    threads_updated = 0
    if not dry_run:
        for tid in thread_ids:
            post_blobs = conn.execute(
                "SELECT embedding FROM posts "
                "WHERE thread_id = ? AND embedding IS NOT NULL "
                "ORDER BY id",
                (tid,),
            ).fetchall()

            if not post_blobs:
                # No embedded posts -- clear any stale centroid.
                conn.execute(
                    "UPDATE threads SET embedding = NULL WHERE id = ?", (tid,)
                )
                if vec_loaded:
                    try:
                        conn.execute(
                            "DELETE FROM vec_threads WHERE thread_id = ?", (tid,)
                        )
                    except sqlite3.OperationalError:
                        pass
                continue

            vectors = [deserialize(r[0]) for r in post_blobs]
            centroid = renormalized_mean(vectors)
            if centroid is None:
                continue

            blob = serialize(centroid)
            conn.execute(
                "UPDATE threads SET embedding = ? WHERE id = ?", (blob, tid)
            )
            if vec_loaded:
                try:
                    conn.execute(
                        "DELETE FROM vec_threads WHERE thread_id = ?", (tid,)
                    )
                    conn.execute(
                        "INSERT INTO vec_threads(thread_id, embedding) VALUES (?, ?)",
                        (tid, blob),
                    )
                except sqlite3.OperationalError:
                    pass  # vec_threads absent -- skip

            threads_updated += 1

        conn.commit()
        print(
            f"[backfill] Phase 2 complete: {threads_updated} thread centroids updated.",
            file=sys.stderr,
        )
    else:
        print(
            f"[backfill] Phase 2 (dry-run): would recompute centroids for {len(thread_ids)} threads.",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Phase 3: FTS sanity rebuild
    # Bare 'rebuild' is safe here -- no retraction-exclusion semantics in
    # forum posts (unlike engram's nodes_fts, which must exclude retracted
    # nodes -- see #727 for the inversion that makes bare rebuild unsafe there).
    # ------------------------------------------------------------------
    print("[backfill] Phase 3: rebuilding posts_fts.", file=sys.stderr)
    fts_rows_after = 0
    if not dry_run:
        try:
            conn.execute("INSERT INTO posts_fts(posts_fts) VALUES ('rebuild')")
            conn.commit()
            fts_rows_after_row = conn.execute(
                "SELECT COUNT(*) FROM posts_fts"
            ).fetchone()
            fts_rows_after = fts_rows_after_row[0] if fts_rows_after_row else 0
            print(
                f"[backfill] Phase 3 complete: posts_fts has {fts_rows_after} rows.",
                file=sys.stderr,
            )
        except sqlite3.OperationalError as exc:
            print(
                f"[backfill] WARNING: posts_fts rebuild failed (table may not exist yet): {exc}",
                file=sys.stderr,
            )
    else:
        try:
            fts_rows_after_row = conn.execute(
                "SELECT COUNT(*) FROM posts_fts"
            ).fetchone()
            fts_rows_after = fts_rows_after_row[0] if fts_rows_after_row else 0
            print(
                f"[backfill] Phase 3 (dry-run): posts_fts currently has {fts_rows_after} rows.",
                file=sys.stderr,
            )
        except sqlite3.OperationalError:
            print("[backfill] Phase 3 (dry-run): posts_fts table not yet created.", file=sys.stderr)

    conn.close()
    return {
        "posts_embedded": posts_embedded,
        "threads_updated": threads_updated,
        "fts_rows_after_rebuild": fts_rows_after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Forum embedding backfill (posts + thread centroids + FTS rebuild).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to forum.db (required).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report what would be done without writing anything.",
    )
    args = parser.parse_args()

    db_path = args.db
    if not Path(db_path).exists():
        print(f"ERROR: database not found: {db_path!r}", file=sys.stderr)
        return 1

    counts = run_backfill(db_path, dry_run=args.dry_run)
    if args.dry_run:
        print(
            f"[backfill] dry-run summary: "
            f"posts_would_embed={counts['posts_embedded']}, "
            f"threads_would_update={counts['threads_updated']}, "
            f"fts_rows_current={counts['fts_rows_after_rebuild']}",
            file=sys.stderr,
        )
    else:
        print(
            f"[backfill] done: "
            f"posts_embedded={counts['posts_embedded']}, "
            f"threads_updated={counts['threads_updated']}, "
            f"fts_rows={counts['fts_rows_after_rebuild']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
