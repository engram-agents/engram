"""Pure-Python ENGRAM backup dump + index rebuild — zero sqlite3-CLI dependency.

This is the single reusable backup mechanism shared by every #720 call site
(server `_commit_snapshot`, `tools/engram-fix-git-backup.sh`,
`tools/engram-regenerate-embeddings.py`). It deliberately uses only the
stdlib `sqlite3` module — never the `sqlite3` command-line binary — because
that binary is NOT guaranteed installed (verified absent on real installs),
and shelling out to it under `except: pass` silently skips the backup on
those hosts (the class-5 failure mode #720 itself was meant to prevent).

The dump excludes ALL regenerable/derived data so git diffs stay small and
grow linearly instead of churning a fresh multi-MB blob every nap:

  - embedding values  — nulled (regenerable from claim text via the embed model)
  - nodes_fts*         — the FTS5 index + its shadow tables + the fts5vocab
                         table (rebuildable from the restored rows)
  - vec_nodes          — the sqlite-vec KNN index, if the extension is loaded
                         (install-conditional: absent when sqlite-vec didn't load)

Restore contract (all three regenerable layers rebuilt, none shipped):
  1. sqlite3 new.db < knowledge.sql          # base rows; embedding NULL; no FTS/vec
  2. backfill embeddings (needs the embed model — lives in the regen tool)
  3. rebuild_fts_index(db)                    # rebuild + exclude retracted rows (#727)
  4. vec backfill (server _backfill_vec_nodes at first _get_db, if ext loads)

Verified round-trip (2026-06-03, Ariadne): base-table parity exact, embeddings
NULL+regenerable, FTS search returns byte-identical hit counts post-rebuild,
no sqlite3 CLI anywhere. The fts5vocab table must be dropped BEFORE its parent
nodes_fts or `iterdump` itself raises `no such fts5 table: main.nodes_fts`.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile


# Tables whose contents are derived/regenerable and must never reach the dump.
# nodes_fts* matches the FTS5 virtual table, its shadow tables
# (nodes_fts_data/idx/docsize/config) and the fts5vocab table (nodes_fts_vocab).
_DERIVED_TABLE_PREFIXES = ("nodes_fts",)
_DERIVED_TABLE_NAMES = ("vec_nodes",)


# Canonical nodes_fts column list — SINGLE SOURCE OF TRUTH (#2077).
#
# Before this fix, the column list was hand-written in TWO places that
# drifted apart: engram_core.py's CREATE VIRTUAL TABLE nodes_fts carried all
# five columns (added by #403 to cover feeling_report's reported_state /
# trigger_text fields), while this module's rebuild_fts_index still had the
# original three. Two independent consequences followed:
#   1. rebuild_fts_index's delete-insert only touched 3 columns, so a
#      retracted node stayed FTS-findable through reported_state/trigger_text.
#   2. On restore, the dump strips nodes_fts and rebuild_fts_index recreated
#      it with 3 columns; engram_core's `CREATE TABLE IF NOT EXISTS` then
#      no-ops forever, leaving the graph permanently under-indexed.
#
# engram_core.py's CREATE VIRTUAL TABLE statement and every INSERT /
# delete-insert in this module now derive their column list from this one
# tuple. engram_backup.py is intentionally the home for it (not
# engram_core.py): it is the leaner, stdlib-only module several call sites
# (engram-regenerate-embeddings.py, the server's own restore path) already
# import without pulling in engram_core's full dependency chain.
NODES_FTS_COLUMNS: tuple = (
    "claim",
    "quoted_text",
    "interpretation",
    "reported_state",
    "trigger_text",
)


def _derived_drop_plan(conn: sqlite3.Connection) -> tuple[list[str], list[str], list[str]]:
    """Return (fts_triggers, vocab_tables, base_virtual_tables) to drop, in safe order.

    Order matters: fts5vocab tables depend on their parent FTS table, so they
    must be dropped first or `iterdump()` chokes trying to read the orphan.
    """
    triggers = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%nodes_fts%'"
        ).fetchall()
    ]
    vocab = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE sql LIKE '%fts5vocab%'"
        ).fetchall()
    ]
    # Parent virtual tables (nodes_fts + vec_nodes); dropping nodes_fts also
    # removes its shadow tables. Exclude the vocab tables already handled above.
    base = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "(name='nodes_fts' OR name='vec_nodes')"
        ).fetchall()
    ]
    return triggers, vocab, base


def dump_stripped(src_db_path: str, out_sql_path: str) -> dict:
    """Write a regenerable-stripped SQL text dump of ``src_db_path`` to ``out_sql_path``.

    Pure-Python: takes a consistent hot backup via the sqlite3 module's
    ``Connection.backup`` (never locks the live DB hard, WAL-safe), strips the
    derived data on the COPY only (the source DB is never modified), then writes
    a restorable ``iterdump`` to ``out_sql_path``.

    Returns a stats dict: ``{statements, bytes, fts_excluded, vec_excluded}``.
    Raises on failure — callers MUST surface errors, never ``except: pass`` them
    (silent skip is the failure mode this module exists to eliminate).
    """
    src = sqlite3.connect(src_db_path)
    fd, snap = tempfile.mkstemp(
        suffix=".snap.db", dir=os.path.dirname(os.path.abspath(src_db_path)), prefix=".engram-bk-"
    )
    os.close(fd)
    try:
        bk = sqlite3.connect(snap)
        try:
            src.backup(bk)  # consistent hot backup, pure-Python, no CLI
        finally:
            src.close()

        triggers, vocab, base = _derived_drop_plan(bk)
        fts_excluded = any(t == "nodes_fts" for t in base) or bool(vocab)
        vec_excluded = any(t == "vec_nodes" for t in base)

        # Strip on the snapshot only — live DB untouched.
        bk.execute("UPDATE nodes SET embedding=NULL")

        # Load sqlite-vec into the snapshot connection so the vec0 virtual table
        # (vec_nodes) can be dropped — dropping a vec0 vtable requires its module.
        # Best-effort: if the extension is unavailable the DB won't have a vec_nodes
        # table to drop anyway. Do NOT swallow a drop failure into a silent no-op —
        # loud failure is the whole point of this module (#720).
        try:
            import sqlite_vec
            bk.enable_load_extension(True)
            sqlite_vec.load(bk)
            bk.enable_load_extension(False)
        except Exception:
            pass  # extension unavailable; if vec_nodes exists the drop below will raise (acceptable: pathological install)

        for tn in triggers:
            bk.execute(f"DROP TRIGGER IF EXISTS {tn}")
        for vn in vocab:  # vocab depends on parent FTS table → drop first
            bk.execute(f"DROP TABLE IF EXISTS {vn}")
        for tn in base:   # nodes_fts (+shadow) / vec_nodes
            bk.execute(f"DROP TABLE IF EXISTS {tn}")
        bk.commit()

        # Read user_version BEFORE closing the snapshot — iterdump() does not
        # emit PRAGMA user_version, so restored DBs would land at 0 and
        # re-fire one-shot migrations (e.g. #274) even on an already-migrated
        # graph.  We append the PRAGMA explicitly so restore reproduces the
        # source's migration state (#781).
        src_user_version = bk.execute("PRAGMA user_version").fetchone()[0]

        statements = 0
        with open(out_sql_path, "w", encoding="utf-8") as f:
            for line in bk.iterdump():
                f.write(line + "\n")
                statements += 1
            # Append user_version after the COMMIT so it executes on restore.
            f.write(f"PRAGMA user_version = {src_user_version};\n")
            statements += 1
        bk.close()
    finally:
        try:
            os.unlink(snap)
        except OSError:
            pass

    return {
        "statements": statements,
        "bytes": os.path.getsize(out_sql_path),
        "fts_excluded": fts_excluded,
        "vec_excluded": vec_excluded,
    }


def _existing_fts_columns(conn: sqlite3.Connection) -> list | None:
    """Return the visible column names of an existing ``nodes_fts`` table, in
    declared order, or None if the table doesn't exist.

    PRAGMA table_info works on FTS5 virtual tables and reports exactly the
    columns declared in the ``fts5(...)`` clause (excluding the content=/
    content_rowid= key=value config, which aren't columns) — a more reliable
    read than parsing the stored CREATE VIRTUAL TABLE SQL text by hand.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
    ).fetchone()
    if not has_table:
        return None
    return [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)").fetchall()]


def rebuild_fts_index(db_path: str) -> bool:
    """Rebuild the FTS5 index from the current rows of ``nodes`` (post-restore).

    Self-contained restore-path fix (#781): creates the FTS table if absent,
    rebuilds from all current ``nodes`` rows (so retracted rows are present in
    the index immediately after rebuild), then 'delete'-inserts each retracted
    row to exclude it from the index (this succeeds only because rebuild just
    populated them — delete-on-empty is the crash).  Finally restores
    PRAGMA user_version to the value it had on entry (#2087): the incoming
    version is read before the rebuild and written back after, so a
    already-migrated live DB (user_version 2 or 3) is not silently downgraded
    to 1 — a downgrade that would make _run_db_one_time_setup's migration-2/3
    blocks re-run on the next call, since #2080's startup self-heal invokes
    this function mid-setup, before those later migration blocks are gated.
    The one exception is an incoming version of 0 (a genuinely fresh/
    pre-migration DB, or a legacy DB that predates the #274 migration gate):
    there the original intent of this PRAGMA — marking FTS schema v1 present
    so the server's one-shot #274 migration gate is satisfied and does not
    re-fire — still applies, so the version is bumped to at least 1.

    Order is load-bearing: rebuild THEN delete (not delete THEN rebuild, which
    is the original crash path).

    Column list is single-sourced from NODES_FTS_COLUMNS (#2077), intersected
    with whatever columns ``nodes`` actually has — real ENGRAM DBs always carry
    all five (engram_core's ALTER TABLE ADD COLUMN backfill guarantees it);
    minimal/legacy test fixtures that only carry claim/quoted_text/
    interpretation still get a 3-column index rather than failing outright.

    Detect-and-repair (#2077 consequence 2): if a ``nodes_fts`` table already
    exists with a column set that doesn't match the canonical target (e.g. a
    stale 3-column table left behind by the pre-fix rebuild path), that stale
    table is dropped and recreated from the canonical list before reindexing.
    This is logged loudly — never a silent no-op — because `CREATE TABLE IF
    NOT EXISTS` would otherwise leave the graph permanently under-indexed.

    Returns True if the rebuild ran, False if the ``nodes`` table does not
    exist (the DB is not ENGRAM-shaped — nothing to do).

    Note: nodes_fts uses content='nodes' (external-content FTS5) with
    deliberate index-side deletion of retracted rows.  FTS5's 'integrity-check'
    therefore always reports "malformed" on a graph with retractions — this is
    expected by design (#781).
    """
    conn = sqlite3.connect(db_path)
    try:
        # Bail early if this isn't an ENGRAM-shaped DB.
        has_nodes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()
        if not has_nodes:
            return False

        # #2087: capture the incoming user_version before the rebuild so it
        # can be restored afterward instead of unconditionally overwritten.
        incoming_user_version = conn.execute("PRAGMA user_version").fetchone()[0]

        # Create the FTS table if it was stripped from the dump (it always is).
        # Guard: only create if nodes has the three original required columns
        # (claim, quoted_text, interpretation).  Minimal test fixtures may
        # have a stripped-down nodes schema; we skip creation for those.
        node_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(nodes)").fetchall()
        }
        fts_required_cols = {"claim", "quoted_text", "interpretation"}
        has_fts_cols = fts_required_cols.issubset(node_cols)
        if not has_fts_cols:
            # nodes schema is incomplete — cannot create or rebuild FTS safely.
            return True  # Still report True: nodes exists, we just can't index.

        # Canonical target columns: NODES_FTS_COLUMNS intersected with what
        # `nodes` actually has, preserving declared order.
        target_cols = [c for c in NODES_FTS_COLUMNS if c in node_cols]

        existing_cols = _existing_fts_columns(conn)
        if existing_cols is not None and existing_cols != target_cols:
            print(
                f"[rebuild_fts_index] repairing under-indexed nodes_fts on {db_path}: "
                f"existing columns {existing_cols} != canonical {target_cols} "
                "— dropping and rebuilding from the canonical column list."
            )
            conn.execute("DROP TABLE IF EXISTS nodes_fts_vocab")
            conn.execute("DROP TABLE IF EXISTS nodes_fts")

        cols_sql = ", ".join(target_cols)
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                {cols_sql},
                content='nodes', content_rowid='rowid'
            )
            """
        )

        # Step 1: rebuild — populates the index from ALL nodes rows, including
        # retracted ones (external-content 'rebuild' mirrors the content table).
        # Exception-path safety (verified by fault injection, #2113 review):
        # this INSERT opens SQLite's implicit transaction; if anything below
        # raises before the commit, conn.close() in the finally ROLLS BACK the
        # rebuild AND the user_version restore together. Do not add an early
        # conn.commit() between here and the version restore — that would
        # split the transaction and let a mid-repair crash strand the version.
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")

        # Step 2: delete-insert retracted rows — succeeds because they were just
        # populated by the rebuild above.  On an empty index this would raise
        # sqlite3.DatabaseError ("database disk image is malformed") — the exact
        # crash that step 1 prevents.
        # Guard: only query status='retracted' if the status column exists
        # (minimal test fixtures may omit it; real ENGRAM DBs always have it).
        if "status" in node_cols:
            select_cols_sql = ", ".join(f"COALESCE({c}, '')" for c in target_cols)
            retracted = conn.execute(
                f"SELECT rowid, {select_cols_sql} FROM nodes WHERE status='retracted'"
            ).fetchall()
            insert_cols_sql = ", ".join(target_cols)
            placeholders_sql = ", ".join(["?"] * (len(target_cols) + 1))
            for row in retracted:
                conn.execute(
                    f"INSERT INTO nodes_fts(nodes_fts, rowid, {insert_cols_sql}) "
                    f"VALUES ('delete', {placeholders_sql})",
                    row,
                )

        # Step 3: restore the incoming user_version rather than unconditionally
        # overwriting it (#2087). PRAGMA statements do not accept bound
        # parameters (SQL grammar), so the value is interpolated; it is a
        # Python int read from this same DB via PRAGMA and is never
        # caller-supplied. user_version is per-database state persisted in the
        # file header — which is exactly why it must survive the rebuild
        # rather than be overwritten.
        # incoming_user_version == 0 means a genuinely fresh/pre-migration DB
        # (or a legacy DB predating the #274 gate) — preserve the original
        # intent of gating the server's #274 migration so it doesn't re-fire
        # by bumping to at least 1. Any incoming version >= 1 passes through
        # unchanged (max(...) is a no-op there).
        restored_user_version = max(incoming_user_version, 1)
        conn.execute(f"PRAGMA user_version = {restored_user_version}")

        conn.commit()
        return True
    finally:
        conn.close()


def verify_roundtrip(src_db_path: str) -> dict:
    """Self-check: dump ``src_db_path``, restore to a temp DB, and confirm the
    base rows survive and the FTS index rebuilds to identical hit counts.

    Returns a report dict; raises AssertionError on any divergence. Used by the
    test suite and the cleanup script's lossless-verify step (replacing the
    sqlite3-CLI verify). Does not modify the source DB.
    """
    import re

    src = sqlite3.connect(src_db_path)
    base_tables = [
        r[0]
        for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'nodes_fts%' AND name NOT LIKE 'vec_nodes%' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    orig_counts = {t: src.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in base_tables}
    has_fts = src.execute("SELECT sql FROM sqlite_master WHERE name='nodes_fts'").fetchone()
    fts_sql = has_fts[0] if has_fts else None
    fts_hits_orig = (
        src.execute("SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'").fetchone()[0]
        if fts_sql
        else None
    )
    src.close()

    dump_path = tempfile.mktemp(suffix=".sql")
    recon_path = tempfile.mktemp(suffix=".db")
    try:
        stats = dump_stripped(src_db_path, dump_path)
        # belt-and-suspenders: no derived-TABLE STATEMENT should remain.
        # Anchored at the statement keyword + table target so that node content
        # which *mentions* "vec_nodes" or "nodes_fts" in its text does NOT match
        # (e.g. INSERT INTO "nodes" VALUES(... 'vec_nodes virtual table ...')).
        # Only lines whose statement target IS the derived table will match.
        guard = re.compile(
            r'^\s*(CREATE\s+(VIRTUAL\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?["\']?(vec_nodes|nodes_fts)'
            r'|INSERT\s+INTO\s+["\']?(vec_nodes|nodes_fts)'
            r'|CREATE\s+TRIGGER\s+["\']?(vec_nodes|nodes_fts))',
            re.IGNORECASE,
        )
        leaked = [ln for ln in open(dump_path) if guard.search(ln)]
        assert not leaked, f"derived-table statements leaked into dump: {leaked[:2]}"

        rc = sqlite3.connect(recon_path)
        rc.executescript(open(dump_path).read())
        for t in base_tables:
            got = rc.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            assert got == orig_counts[t], f"{t}: {got} != {orig_counts[t]}"
        null_emb = rc.execute("SELECT count(*) FROM nodes WHERE embedding IS NULL").fetchone()[0]
        total = rc.execute("SELECT count(*) FROM nodes").fetchone()[0]
        assert null_emb == total, f"embeddings not fully stripped: {null_emb}/{total}"

        fts_ok = None
        fts_ok_orig = None
        if fts_sql:
            # Capture fts_ok_orig immediately so it is available in any AssertionError
            # context below (if the parity assert fires, fts_hits_orig is already
            # measured; without this assignment here it would stay None and obscure
            # the pollution signal in production backup logs).
            fts_ok_orig = fts_hits_orig

            # Close rc before calling rebuild_fts_index — the helper manages its
            # own connection to recon_path and must not race a live connection.
            rc.close()
            rebuild_fts_index(recon_path)
            rc = sqlite3.connect(recon_path)

            # Structural parity assertion: every current (non-retracted) row must
            # be in the index, and every retracted row must be excluded.  This is
            # the invariant rebuild_fts_index actually guarantees.
            #
            # #727: the old assertion (hits >= fts_hits_orig) is INVERTED under
            # index pollution — a CLEAN rebuild on the recon legitimately returns
            # FEWER hits than a POLLUTED live source index (Borges confirmed
            # live=137 with 2 retracted matches, clean=135 on his graph).  The >=
            # would false-fail exactly when the source index is polluted and the
            # recon is correct.  Replaced with the structural invariant below.
            recon_cols = {
                row[1]
                for row in rc.execute("PRAGMA table_info(nodes)").fetchall()
            }
            if "status" in recon_cols:
                # IS NOT is SQLite's null-safe complement of IS: a NULL-status row
                # evaluates to TRUE here (counted as non-retracted), matching what
                # rebuild_fts_index actually guarantees — it only removes rows where
                # status='retracted', so NULL-status rows remain in the index.
                # Using != 'retracted' would give NULL (not TRUE) for NULL-status rows,
                # undercounting expected_fts_count and causing a false AssertionError.
                expected_fts_count = rc.execute(
                    "SELECT count(*) FROM nodes WHERE status IS NOT 'retracted'"
                ).fetchone()[0]
            else:
                # No status column — all nodes are indexable (minimal fixture).
                expected_fts_count = rc.execute(
                    "SELECT count(*) FROM nodes"
                ).fetchone()[0]
            # FTS5 external-content note: SELECT count(*) FROM nodes_fts returns the
            # total rows in the content table (including tombstoned/retracted rows),
            # not the indexed-document count.  nodes_fts_docsize has exactly one row
            # per non-deleted document — this is the correct structural invariant.
            actual_fts_count = rc.execute(
                "SELECT count(*) FROM nodes_fts_docsize"
            ).fetchone()[0]
            assert actual_fts_count == expected_fts_count, (
                f"FTS structural parity failed: nodes_fts_docsize has {actual_fts_count} rows, "
                f"expected {expected_fts_count} (non-retracted nodes); "
                f"source fts_hits_orig={fts_ok_orig}"
            )

            # Probe-term hit counts are informational only — NOT used to gate pass/fail.
            # fts_hits_orig: source probe count (may include retracted-row pollution).
            # fts_hits: recon probe count (definitionally clean post rebuild_fts_index).
            # The delta between the two surfaces any pollution in the source index —
            # diagnostic signal that exposed the whole #727 mechanism.
            # Skip diagnostic probe when source returned 0 hits — trivially uninformative.
            if fts_hits_orig:
                fts_ok = rc.execute(
                    "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
                ).fetchone()[0]
            else:
                fts_ok = 0
        rc.close()
        return {
            **stats,
            "base_tables": len(base_tables),
            "nodes": orig_counts.get("nodes"),
            "fts_hits": fts_ok,
            "fts_hits_orig": fts_ok_orig,
            "ok": True,
        }
    finally:
        for p in (dump_path, recon_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="engram_backup", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="Write a regenerable-stripped SQL dump.")
    d.add_argument("src_db")
    d.add_argument("out_sql")
    r = sub.add_parser("rebuild-fts", help="Rebuild the FTS index from current rows.")
    r.add_argument("db")
    v = sub.add_parser("verify", help="Self-check the dump→restore→FTS-rebuild round-trip.")
    v.add_argument("src_db")
    args = p.parse_args(argv)

    if args.cmd == "dump":
        stats = dump_stripped(args.src_db, args.out_sql)
        print(f"dump: {stats['statements']} stmts, {stats['bytes']/1024/1024:.2f} MB "
              f"(fts_excluded={stats['fts_excluded']} vec_excluded={stats['vec_excluded']})")
    elif args.cmd == "rebuild-fts":
        ran = rebuild_fts_index(args.db)
        print("FTS rebuilt" if ran else "no nodes_fts table to rebuild")
    elif args.cmd == "verify":
        rep = verify_roundtrip(args.src_db)
        print(f"round-trip OK: nodes={rep['nodes']} fts_hits={rep['fts_hits']} "
              f"dump={rep['bytes']/1024/1024:.2f}MB stmts={rep['statements']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
