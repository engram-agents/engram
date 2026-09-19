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
#
# #1952 PR2 (engram-alpha#1817 / #1875): reported_state dropped, recall_summary
# + recall_keywords added. reported_state is now redundant with the untruncated
# claim (#1875 removes claim's 80-char cap for feeling_report nodes) plus
# recall_summary — indexing it separately just duplicates content in the FTS
# table. The column itself is NOT dropped from `nodes` (deliberately
# conservative scoping, see #1952 PR2's PR description) — only removed from
# the FTS column set. recall_summary/recall_keywords participate in FTS so a
# curated summary/keyword set actually affects retrieval (#1817 — before this
# they were write-only/display-only fields with zero effect on search hits).
# See PRAGMA user_version rung 4 in engram_core.py for the paired migration.
NODES_FTS_COLUMNS: tuple = (
    "claim",
    "quoted_text",
    "interpretation",
    "trigger_text",
    "recall_summary",
    "recall_keywords",
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


def check_fts_integrity(conn: sqlite3.Connection, probe_k: int | None = None) -> dict:
    """Probe ``nodes_fts`` for ranking-path corruption and report a distinct,
    structured result -- never a silent pass (engram-alpha#2678, forum #398).

    ``nodes_fts`` is an external-content FTS5 table (``content='nodes'``):
    the index stores only tokens/positions, and ranking a match (``ORDER BY
    rank``, which invokes ``bm25()``) reads auxiliary data to score it. If
    that auxiliary data has drifted out of sync for a given term's postings
    (SQLite's own ``SQLITE_CORRUPT_VTAB``), ranking raises "database disk
    image is malformed" for THAT TERM while ``PRAGMA quick_check`` still
    reports "ok", plain ``MATCH`` (no ``ORDER BY rank``) still succeeds, and
    every OTHER term ranks fine -- so the desync is invisible to every check
    ENGRAM already runs and silent until a real query happens to rank the
    one bad term. See Consumers below for what goes dark before that day.

    DESIGN HISTORY -- read before changing the vocab traversal; three designs
    were tried, in order, each falsified by the next real measurement against
    Lei's fleet's ONLY known instance of the real bug (forum #398,
    2026-09-07). This is not a hypothetical caution -- every one of the first
    three shipped or nearly shipped before the next measurement corrected it:

    1. Plain FTS5 ``INSERT INTO nodes_fts(nodes_fts) VALUES('integrity-check')``.
       Tolerates ENGRAM's by-design retracted-row exclusion (a retracted node
       is deleted from the index while remaining present in ``nodes`` --
       deliberate, see ``rebuild_fts_index``'s docstring and #781), chosen
       for exactly that reason -- but reported CLEAN against a byte-copy of
       the one graph that actually had the bug. False negative, fairy-
       approved, pulled back before merge.
    2. ``rank=1`` (the "more thorough" content-verifying variant). Correctly
       caught that same real corruption, but false-positives on ANY graph
       with even one correctly-retracted node -- confirmed on a scratch DB
       with exactly one retracted row and nothing else wrong. Also confirmed
       NOT fixable by healing the mismatch after the fact: genuinely deleting
       the retracted rows from ``nodes`` in a scratch copy (so content and
       index agree retraction happened on both sides) did not clear the
       failure -- whatever it inspects survives content-table cleanup.
    3. A differential probe (plain ``MATCH`` vs ``MATCH ... ORDER BY rank``
       per term, retraction-immune by construction since retracted rows
       never enter a ``MATCH`` result set) over the top-k highest-document-
       frequency terms from ``nodes_fts_vocab``. Validated at k=30 against a
       fresh copy of the real corrupted graph -- caught the one poisoned
       term known at the time ("by", document-frequency rank 18 of 30).
       Shipped, fresh review round dispatched. THEN a full-vocabulary sweep
       of that same graph (all 26,745 terms, 1.5s) found 24 poisoned terms,
       not 1 -- "by" was the ONLY one in the top-30 by frequency; the other
       23 span mid/low frequency and bare numerals, uncorrelated with rank.
       No k a person would pick catches more than one of twenty-four, and a
       detector that fires and under-reports by 24x is worse than one that
       stays silent, because the count reads as a real measurement.

    So this function sweeps the FULL vocabulary, not a top-k sample --
    Kepler's ruling, and the measurement that forced it: full-vocab cost is
    negligible (1.5s over 26,745 terms on the largest known seat; 0.53s over
    13,805 terms and 33 retracted nodes on a second, healthy seat, zero false
    positives -- see the PR body for both). ``probe_k`` is kept as an escape
    hatch (``None`` = sweep everything, the default and the only mode that
    has been shown honest) for a future graph where full-vocab cost turns out
    not to be negligible -- do not lower it as a default without a fresh
    cost measurement on a graph at least as large as the ones above.

    Consumers put at risk by an undetected mismatch (deployed tree, recursive
    grep, 3 rows -- per #2678): ``engram_core._similar_existing_matches``
    (~line 6247, the check-before-derive dedup gate for ``add_observation``
    and lesson minting -- a ``DatabaseError`` here escapes its inner
    ``OperationalError`` catch to the outer ``except Exception`` and returns
    ``[]``, not a degraded result); ``hooks/engram-surface-hook.py``'s
    ``_query_attached_packs`` (ranks each attached pack's own db, fails open
    per pack by its own docstring); and the viz server's graph endpoint.
    This function only detects; it does not repair and does not touch
    ``_similar_existing_matches``'s exception handling -- both are separate,
    write-path-adjacent work (#2678 items 2/3), out of scope here.

    Returns one of FOUR statuses -- kept distinct rather than collapsed,
    because a caller must be able to tell "I found damage" from "I could
    not look" from "I never checked at all" (Ariadne's #2731 colleague-
    review refinement, forum #398, raised on top of her own earlier
    "never a silent pass" finding, before the same status value quietly
    swallowed the distinction one layer up from where her original
    finding fixed it):

      {"status": "unavailable", "reason": <str>} -- no ``nodes_fts`` or
          ``nodes_fts_vocab`` table to probe, or the probe itself could not
          run at all (any unexpected ``sqlite3.Error`` before any term was
          examined).
      {"status": "mismatch", "problems": [...], "unprobed": [...]}
          -- CONFIRMED ranking-path corruption: at least one term's plain
          MATCH succeeded but its ranked query raised
          ({"term", "error", "stage": "ranked_match"}). ``unprobed`` may
          also be present alongside (not classified further; see below) but
          never changes this status once ``problems`` is non-empty. The
          full list, not a boolean or a truncated sample (per Kepler's
          ruling: "report the count and the list, never a boolean" --
          design 3's top-k sample under-reported real damage by 24x, and a
          truncated list would repeat that mistake one level down). A real,
          actionable finding: consider ``rebuild_fts_index`` (own-substrate
          authority; snapshot first) -- though note its repair effectiveness
          against either failure class is unverified; re-run this check
          after to confirm.
      {"status": "partial", "unprobed": [...], "probed_terms": <int>}
          -- an UNKNOWN, not confirmed damage: every term that WAS fully
          probed (plain-matched AND ranked) came back clean, but at least
          one term's PLAIN (unranked) query itself raised ({"term", "error",
          "stage": "plain_match"}), so it was never reached by the ranked
          check at all. NOTE: ``probed_terms`` here is ``len(terms)`` -- the
          size of the full sweep, INCLUDING the unprobed term(s) -- not a
          count of how many were successfully checked; a caller wanting the
          latter must compute ``probed_terms - len(unprobed)``. Deliberately NOT
          classified as syntax-error-vs-corruption -- the error text is the
          same generic SQLITE_CORRUPT_VTAB message either way, and text-
          matching it would reintroduce the brittleness this design escaped
          -- just never silently dropped. On the default tokenizer no
          legitimate vocab term produces this (verified: embedded quotes,
          boolean operators, wildcards, punctuation, bare numerals all
          plain-match cleanly), so any entry here is itself worth
          investigating; damage bad enough to break even the unranked path
          would be worse than a "mismatch" entry, IF it turns out to be
          corruption rather than an untested tokenizer's real syntax
          restriction (bound: default tokenizer only -- see the exception
          handler's own comment). This status exists so that possibility
          is never silently reported as either "ok" (would hide a real
          unknown) or "mismatch" (would falsely confirm damage that was
          never actually observed, on a graph whose only sin might be a
          custom tokenizer this design hasn't been tested against).
      {"status": "ok", "probed_terms": <int>} -- every vocabulary term both
          plain-matched AND ranked clean, with nothing left unprobed at all;
          here (and only here) ``probed_terms`` equals the count that came
          back fully clean, since none were skipped (full sweep by default
          -- see ``probe_k`` above for when this is instead a sample).

    Never raises: every SQL call is guarded, and unlike the special-command
    designs this replaced, none of these are write-shaped -- no implicit
    transaction is opened, so there is nothing to roll back and no reason
    this needs write access at all (confirmed: works against a read-only
    connection, unlike the first design -- Ariadne's #2731 review caught
    that "never raises" was NOT actually true of that design, on exactly the
    paths that mattered most; fixed here by construction, not by narrowing
    the claim).
    """
    try:
        if _existing_fts_columns(conn) is None:
            return {"status": "unavailable", "reason": "nodes_fts table does not exist"}
        has_vocab = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts_vocab'"
        ).fetchone()
        if not has_vocab:
            return {"status": "unavailable", "reason": "nodes_fts_vocab table does not exist"}

        if probe_k is None:
            terms = [row[0] for row in conn.execute("SELECT term FROM nodes_fts_vocab").fetchall()]
        else:
            terms = [
                row[0]
                for row in conn.execute(
                    "SELECT term FROM nodes_fts_vocab ORDER BY doc DESC LIMIT ?", (probe_k,)
                ).fetchall()
            ]

        problems = []
        unprobed = []
        for term in terms:
            # Double-quoted so FTS5 treats the term as a literal phrase, not
            # query syntax -- a vocab term could itself collide with an FTS5
            # operator (e.g. "or", "not") if passed unquoted.
            quoted = f'"{term}"'
            try:
                plain_rows = conn.execute(
                    "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (quoted,)
                ).fetchall()
            except sqlite3.Error as e:
                # Ariadne's colleague-review finding (#2731, forum #398):
                # this branch does NOT mean "term doesn't plain-match" in
                # general -- it's ANY error on the plain query, and on the
                # default tokenizer there is no legitimate occupant (tested:
                # embedded quotes, AND/OR/NOT/NEAR, *, colons, hyphens,
                # parens, bare numerals -- zero vocab terms raise on a quoted
                # plain MATCH). A term whose UNRANKED match itself raises is
                # corruption strictly worse than what "mismatch" reports
                # (bad enough to break even the simple path), and silently
                # `continue`-ing here made it byte-identical to a clean
                # sweep -- exactly the silent pass this function exists to
                # prevent. Recorded, never dropped. Deliberately NOT
                # classified by matching the error text against known
                # syntax-error strings -- SQLITE_CORRUPT_VTAB surfaces as
                # the same generic message this whole design has been
                # navigating, and text-matching it would reintroduce the
                # exact brittleness three prior designs were built to
                # escape. Bound: default tokenizer only: a custom or
                # trigram tokenizer is untested and may have a real
                # syntax-error case this branch would then also swallow the
                # WAY it did before.
                unprobed.append({"term": term, "error": str(e), "stage": "plain_match"})
                continue
            if not plain_rows:
                continue
            try:
                conn.execute(
                    "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY rank",
                    (quoted,),
                ).fetchall()
            except sqlite3.Error as e:
                problems.append({"term": term, "error": str(e), "stage": "ranked_match"})

        # Ariadne's #2731 forum #398 refinement, raised before her colleague
        # read on 4e4436d converged: an unprobed term is an UNKNOWN, not a
        # confirmed problem -- collapsing both into "mismatch" would (on an
        # untested custom/trigram tokenizer, where a legitimate plain-match
        # syntax error might exist) reintroduce the false-positive shape
        # that killed design 2, one layer up in the reporting rather than
        # the probe. "mismatch" means confirmed damage found (problems is
        # non-empty); "partial" means everything probed came back clean but
        # something could not be checked at all (unprobed non-empty,
        # problems empty) -- a caller must be able to tell "I found damage"
        # from "I could not look", and only a distinct status value keeps
        # that true even for a caller who checks status alone rather than
        # reading the whole dict.
        if problems:
            status = "mismatch"
        elif unprobed:
            status = "partial"
        else:
            status = "ok"
        result = {"status": status}
        if problems:
            result["problems"] = problems
        if unprobed:
            result["unprobed"] = unprobed
        if status in ("ok", "partial"):
            result["probed_terms"] = len(terms)
        return result
    except sqlite3.Error as e:
        return {"status": "unavailable", "reason": str(e)}


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

    Guard (#2186): SQLite treats an empty string ``db_path`` as "open a
    private temporary scratch database" rather than an error — it does not
    raise. Callers that derive ``db_path`` from
    ``conn.execute("PRAGMA database_list").fetchone()[2]`` get an empty
    string for an in-memory connection (``:memory:``, or a temp DB never
    attached to a file), so an unguarded ``rebuild_fts_index("")`` would
    silently rebuild an unrelated scratch database while the real
    connection stays under-indexed. Reject a falsy ``db_path`` up front,
    loudly, so this is a named refusal rather than a silent no-op against
    the wrong database.
    """
    if not db_path:
        raise ValueError(
            "rebuild_fts_index: db_path is empty — refusing to open an "
            "implicit SQLite scratch database. This usually means the "
            "caller derived the path from PRAGMA database_list on an "
            "in-memory connection (empty string for :memory: / an "
            "unattached temp DB). Pass a real file path, or skip the "
            "rebuild entirely for in-memory connections."
        )
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
