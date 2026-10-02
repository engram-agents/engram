"""health.series -- the health-score series store: ingest + rebuild + status
+ replay + backfill + verify.

Design of record: ``docs/health-series-design.md`` (design v1 sections 0-10,
amended by deltas D1-D5; the deltas win over the sections they amend). This
module implements **PR A, PR B1, and PR B2** ("#2575 PR A", "#2575 PR B") of
the three-PR slicing named in that document's "Slicing after the deltas"
section (further split into B1/B2 in that doc's "Scope split" paragraph):

  - **PR A** -- the store schema (all tables, including ``replay_decl_
    hash``, the ``unreplayable`` class shape, ``anchor_resolution``,
    ``declarations``, ``unresolved_snapshots``), ``ingest``, old-format
    refusal, anchor resolution (D1), a single-diff ``rebuild``, and the CLI
    skeleton.
  - **PR B1** -- the replay driver (``replay_commit``), ``MeasurementDef.
    replay`` (in ``measurements.py``) and its ``replay_decl_hash``
    fingerprint (D2), the equality control (``equality_check``), the
    regular backfill mechanism (``backfill``), ``verify``, and the CLI's
    ``backfill``/``verify`` subcommands.
  - **PR B2 (this module, additionally)** -- the two-diff form of
    ``rebuild`` (D4.2: content-changed under the stored ``engine_build`` vs
    build-changed under HEAD), which needs a checkout of a historical
    ``engine_build`` into a scratch tree -- a real blast-radius jump from
    "replay a dump into :memory:" to "checkout a historical commit's
    ``health/`` package." The old single-diff form survives as an explicit
    CLI/API opt-back (``--single-diff`` / ``rebuild(..., single_diff=True)``)
    since it confounds content and build change and is never the default.
  - **PR C1 (this module, additionally)** -- ``derived_points`` population
    from STORED declarations (D4.1), never HEAD's live ``SIGNAL_DEFS``/
    ``MODEL`` for a non-current version (``derive``/``derive_point``), the
    equality control against each stored row's own snapshot file
    (``derive_checks``), ``export`` (the JSON ``/api/health/v2/series``
    serves), and ``checkpoint_hook`` (wired into ``engram_lifecycle.py``'s
    post-dream checkpoint, ``ingest`` -> ``backfill`` -> ``derive`` in one
    best-effort call).

PR C1 does not touch ``rebuild``'s two-diff machinery, or
``health/engine.py``/``health/model.py``/``health/signals.py``/``health/
curves.py``/``health/measurements.py``/``health/version.py`` -- it only
calls into ``health.model.model_from_defs``/``health.engine.score_signals``
(both lazily, from inside ``derive_point``/``derive``), and reads
``health.signals.signal_defs_jsonable``/``health.curves.curve_defs_jsonable``
only for the CURRENT-version seeding path.

The store (``series.db``) is DERIVED and REBUILDABLE -- never the SSoT. The
snapshot JSON files under ``<snapshots_dir>/*.json`` (plus git history for
replay) are the SSoT; this store is an index over them. ``rebuild`` exists
precisely because of this: it reconstructs a fresh store from scratch and
diffs it against the live one, and only replaces the live store when they
agree byte-for-byte on every non-run-metadata column (design doc's "safety
property... and a test", section 2) -- extended by D4.2 into TWO diffs
(content vs build) so that agreement is meaningful even when the code that
computed the stored rows has since changed (see ``rebuild``'s own
docstring).

**Never-mutate rule**: rows in ``l0_points``, ``declarations``, and
``unresolved_snapshots`` are never UPDATEd once inserted (``unresolved_
snapshots`` rows are the sole exception -- their PRIMARY KEY is the
snapshot filename, and a re-``ingest`` that resolves a previously-unresolved
file DELETEs its row rather than updating it in place; a still-unresolved
file's row is upserted only to refresh ``seen_at``/``reason``, per D1). A
new MAJOR/health_version adds NEW rows; old rows are read-only history. A
file whose anchor resolves to a ``(commit_sha, health_major)`` already
keyed by a DIFFERENT snapshot file is never inserted into ``l0_points``
either (that would be a false row) -- it is upserted into
``unresolved_snapshots`` with reason ``commit_already_keyed``, the same
upsert-only-for-``seen_at``/``reason`` discipline as every other
unresolved reason.

This module is pure stdlib (``sqlite3``, ``json``, ``subprocess``,
``hashlib``, ``datetime``, ``pathlib``, ``argparse``, ``tarfile``,
``tempfile``, ``shutil``, ``io``, ``functools``) and takes every path as an
explicit argument -- it deliberately does NOT ``import engram_core`` or
read ``DATA_DIR`` at module top level (or anywhere), matching health/
engine.py's own lazy-import discipline (see that module's "NOTE on
engram_core access"). Every git call is a ``subprocess.run([...])`` argument
list -- never ``shell=True``, never a piped string, never ``git describe``.

**PR B2's scratch-tree mechanism never imports a historical ``health``
package into this process.** ``rebuild``'s default ``regenerate_under_build``
archives a historical ``engine_build``'s ``src/engram`` tree and runs its
``health/series.py`` as a ``subprocess.run([sys.executable, ...])`` child --
never an in-process ``import`` of anything under the scratch tree. This
keeps the two-diff form's "checkout a historical commit's ``health/``
package" blast radius contained to a disposable child process and a
disposable temp directory, never this module's own ``sys.modules``.
"""

from __future__ import annotations

import argparse
import functools
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# health.measurements (registry + replay_decl_hash) and health.version
# (MAJOR, for the raw_schema_version stamp) are needed for PR B1's replay
# driver / backfill / verify. NOTE this module's own "NOTE on engram_core
# access" discipline still holds for engram_core ITSELF (never imported
# here, at any level) -- but importing health.measurements is not the same
# thing as importing engram_core, and it is NOT engram_core-free at ITS OWN
# import time: health.measurements resolves ``engram_core.CLAIM_BEARING_
# TYPES`` once, at ITS module level (see that module's own
# ``_resolve_claim_bearing_types`` docstring for why that one exception is
# safe there). This module's tests rely on health.measurements already
# being imported (and thus engram_core already resolved and cached) by the
# time any test exercises the "series.py itself never touches engram_core"
# guarantee -- see ``test_module_imports_nothing_from_engram_core_at_import``,
# which is unaffected because it patches ``sys.modules['engram_core']``
# after health.measurements (and the real engram_core it resolved) are
# already cached, so re-importing health.series a second time never
# re-executes health.measurements' module body.
#
# Dual execution mode (see the CLI section's own comment below for the
# fuller version of this problem): a bare relative import
# (``from . import measurements``) fails with "attempted relative import
# with no known parent package" when this file is run directly as a script
# (``python3 src/engram/health/series.py ...``, no package context). Try
# the relative import first (the normal, package-imported path -- e.g.
# ``from health import series``); on failure, add ``src/engram`` (this
# file's grandparent) to ``sys.path`` and import absolutely, mirroring the
# same ``sys.path.insert`` + ``from health import ...`` pattern every test
# file in this repo already uses (see tests/conftest.py).
try:  # pragma: no cover -- exercised by both execution modes in practice
    from . import measurements as health_measurements
    from . import version as health_version
except ImportError:
    _src_engram = str(Path(__file__).resolve().parent.parent)
    if _src_engram not in sys.path:
        sys.path.insert(0, _src_engram)
    from health import measurements as health_measurements  # type: ignore[no-redef]
    from health import version as health_version  # type: ignore[no-redef]

# Second alias to the SAME `health.version` module object as `health_version`
# above -- used ONLY inside PR C1's `derive`/`export`, whose own PARAMETER is
# named `health_version` (a string, per the spec'd signature) and would
# otherwise shadow the module-level import of the same name inside those two
# function bodies. Every other function in this file keeps using the plain
# `health_version` name (no shadowing risk there -- none of them takes a
# same-named parameter).
health_version_module = health_version

__all__ = [
    "SeriesIntegrityError",
    "open_store",
    "snapshot_files",
    "parse_snapshot_name",
    "detect_engine_build",
    "knowledge_commits",
    "resolve_anchor",
    "ingest",
    "rebuild",
    "status",
    "replay_commit",
    "parse_turn_from_message",
    "equality_check",
    "backfill",
    "verify",
    "derive_point",
    "derive",
    "export",
    "checkpoint_hook",
]


class SeriesIntegrityError(Exception):
    """Raised by ``ingest`` when two snapshot files declare the SAME
    ``health_version`` with DIFFERENT ``signal_defs``/``curve_defs``/
    ``config`` blobs (D4.1) -- a version-discipline violation. This is the
    one path in ``ingest`` that aborts the run (every other per-file failure
    -- unreadable JSON, old-format, unresolved anchor -- is counted and
    logged, never raised)."""


_SNAPSHOT_NAME_RE = re.compile(
    r"^(?P<turn>\d+)-(?P<mode>nap|pre_sleep|post_dream)-(?P<ts>\d{8}T\d{6}Z)\.json$"
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS l0_points (
    commit_sha TEXT NOT NULL,
    health_major INTEGER NOT NULL,
    commit_time TEXT NOT NULL,
    turn INTEGER NULL,
    source TEXT NOT NULL CHECK(source IN ('stored','git_replay')),
    replay_decl_hash TEXT NOT NULL DEFAULT '',
    snapshot_file TEXT NULL,
    anchor_pre_commit TEXT NULL,
    anchor_resolution TEXT NULL CHECK(anchor_resolution IN ('child_of_anchor','by_time')),
    computed_at TEXT NOT NULL,
    engine_build TEXT NOT NULL,
    measurements TEXT NOT NULL,
    unreplayable TEXT NOT NULL,
    PRIMARY KEY (commit_sha, health_major, source, replay_decl_hash)
);

CREATE TABLE IF NOT EXISTS derived_points (
    commit_sha TEXT NOT NULL,
    health_version TEXT NOT NULL,
    l0_source TEXT NOT NULL CHECK(l0_source IN ('stored','git_replay')),
    replay_decl_hash TEXT NOT NULL DEFAULT '',
    signals TEXT NOT NULL,
    scores TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    engine_build TEXT NOT NULL,
    PRIMARY KEY (commit_sha, health_version, l0_source, replay_decl_hash)
);

CREATE TABLE IF NOT EXISTS derive_checks (
    commit_sha TEXT NOT NULL,
    health_version TEXT NOT NULL,
    verdict TEXT CHECK(verdict IN ('agree','disagree')),
    diff TEXT,
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS equality_checks (
    commit_sha TEXT NOT NULL,
    health_major INTEGER NOT NULL,
    verdict TEXT CHECK(verdict IN ('agree','disagree')),
    diff TEXT,
    checked_at TEXT,
    replay_decl_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS replay_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    health_major INTEGER,
    reason TEXT CHECK(reason IN ('ingest','initial','gap_fill','major_bump','verify','rebuild','derive')),
    commits_total INTEGER,
    commits_done INTEGER,
    skipped_old_format INTEGER,
    unresolved INTEGER,
    engine_build TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS declarations (
    health_version TEXT PRIMARY KEY,
    signal_defs TEXT,
    curve_defs TEXT,
    config TEXT,
    model_defs TEXT,
    first_seen_commit TEXT,
    first_seen_file TEXT,
    engine_build TEXT
);

CREATE TABLE IF NOT EXISTS unresolved_snapshots (
    snapshot_file TEXT PRIMARY KEY,
    anchor TEXT NULL,
    reason TEXT CHECK(reason IN ('no_child_commit','shared_anchor','by_time_out_of_bound','anchor_not_in_repo','commit_already_keyed')),
    seen_at TEXT
);
"""

# The literal that must appear in unresolved_snapshots' CHECK constraint for
# the store to be on the current schema -- see open_store()'s migration.
_UNRESOLVED_REASON_MIGRATION_MARKER = "commit_already_keyed"

# The literal that must appear in replay_runs' CHECK constraint for the
# store to be on the current (PR C1) schema -- see open_store()'s
# migration (5).
_REPLAY_RUNS_REASON_MIGRATION_MARKER = "'derive'"

# Tables compared by rebuild()'s ordered-dump diff (run metadata --
# computed_at/engine_build/seen_at, and every replay_runs row -- is
# deliberately excluded; see rebuild()'s own docstring).
_REBUILD_DIFF_TABLES = ("l0_points", "declarations", "unresolved_snapshots")

# columns EXCLUDED from the rebuild diff, per table (run metadata).
_REBUILD_DIFF_EXCLUDE_COLUMNS = {
    "l0_points": {"computed_at", "engine_build"},
    "declarations": {"engine_build"},
    "unresolved_snapshots": {"seen_at"},
}

_PRIMARY_KEYS = {
    "l0_points": ("commit_sha", "health_major", "source", "replay_decl_hash"),
    "declarations": ("health_version",),
    "unresolved_snapshots": ("snapshot_file",),
}


def _unresolved_snapshots_needs_migration(conn: sqlite3.Connection) -> bool:
    """``True`` iff ``unresolved_snapshots`` exists but its ``reason`` CHECK
    constraint predates ``commit_already_keyed`` (this store's first schema
    migration -- see ``open_store``'s docstring). ``PRAGMA table_info``
    cannot see CHECK constraints, so the literal text of the constraint is
    read off ``sqlite_master.sql`` instead."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'unresolved_snapshots'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return False
    return _UNRESOLVED_REASON_MIGRATION_MARKER not in row["sql"]


def _migrate_unresolved_snapshots(conn: sqlite3.Connection) -> None:
    """Recreate ``unresolved_snapshots`` with the current CHECK constraint,
    preserving its rows. This is the store's first schema migration --
    safe because ``unresolved_snapshots`` (like every table here) is
    DERIVED state: ``rebuild`` reproduces it from the snapshot files and
    the repo, so a rewritten copy of its rows is never a loss of the
    source of truth."""
    conn.execute("ALTER TABLE unresolved_snapshots RENAME TO unresolved_snapshots__old")
    conn.executescript(
        """
        CREATE TABLE unresolved_snapshots (
            snapshot_file TEXT PRIMARY KEY,
            anchor TEXT NULL,
            reason TEXT CHECK(reason IN ('no_child_commit','shared_anchor','by_time_out_of_bound','anchor_not_in_repo','commit_already_keyed')),
            seen_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO unresolved_snapshots (snapshot_file, anchor, reason, seen_at) "
        "SELECT snapshot_file, anchor, reason, seen_at FROM unresolved_snapshots__old"
    )
    conn.execute("DROP TABLE unresolved_snapshots__old")
    conn.commit()


def _equality_checks_needs_migration(conn: sqlite3.Connection) -> bool:
    """``True`` iff ``equality_checks`` exists but predates the
    ``replay_decl_hash`` column (this store's SECOND schema migration --
    same shape as ``_unresolved_snapshots_needs_migration`` above, but this
    one is a plain column addition rather than a CHECK-constraint widening,
    so ``PRAGMA table_info`` (which cannot see CHECK constraints, only
    columns) is sufficient here -- no need to read ``sqlite_master.sql``."""
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(equality_checks)").fetchall()
    }
    return "replay_decl_hash" not in cols


def _migrate_equality_checks(conn: sqlite3.Connection) -> None:
    """Add ``replay_decl_hash`` to an ``equality_checks`` table created
    under PR A's original DDL (``commit_sha, health_major, verdict, diff,
    checked_at`` -- no ``replay_decl_hash``, no primary key). A plain
    ``ALTER TABLE ... ADD COLUMN`` suffices (unlike ``unresolved_
    snapshots``'s CHECK-constraint migration, which needs a full
    rename-recreate-reinsert because SQLite cannot alter a CHECK in place):
    existing rows get ``''`` (the same "no declared-replay-fingerprint yet"
    sentinel ``l0_points.replay_decl_hash`` uses for pre-D2 ``stored`` rows),
    and the table stays append-only exactly as PR A designed it -- this
    migration only ever adds a column, never touches existing row values or
    row identity."""
    conn.execute(
        "ALTER TABLE equality_checks ADD COLUMN replay_decl_hash TEXT NOT NULL DEFAULT ''"
    )
    conn.commit()


def _derived_points_needs_migration(conn: sqlite3.Connection) -> bool:
    """``True`` iff ``derived_points`` exists but predates the ``l0_source``
    column (this store's THIRD schema migration, PR C1) -- a plain column,
    so ``PRAGMA table_info`` suffices."""
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(derived_points)").fetchall()
    }
    return "l0_source" not in cols


def _migrate_derived_points(conn: sqlite3.Connection) -> None:
    """Migrate a PR-A/B-era ``derived_points`` table (``commit_sha,
    health_version, signals, scores, computed_at, engine_build``, PK
    ``(commit_sha, health_version)``) to PR C1's shape (adds ``l0_source``/
    ``replay_decl_hash``, PK widened to ``(commit_sha, health_version,
    l0_source, replay_decl_hash)``). NO shipped writer ever populated this
    table before PR C1 (``derive`` is the first), so the safe migration is:
    if the table is EMPTY, drop and recreate under the new DDL; if it is
    NON-empty (meaning something, somehow, wrote to it under the old
    schema -- should never happen), raise ``SeriesIntegrityError`` naming
    the table rather than silently dropping rows."""
    count = conn.execute("SELECT COUNT(*) AS c FROM derived_points").fetchone()["c"]
    if count != 0:
        raise SeriesIntegrityError(
            "derived_points migration (PR C1): the table is on the pre-C1 "
            f"schema (no l0_source column) but holds {count} row(s) -- "
            "refusing to silently drop them. No shipped writer populated "
            "this table before PR C1 (`derive` is the first), so a "
            "non-empty pre-C1 table is unexpected; investigate before "
            "proceeding."
        )
    conn.execute("DROP TABLE derived_points")
    conn.executescript(
        """
        CREATE TABLE derived_points (
            commit_sha TEXT NOT NULL,
            health_version TEXT NOT NULL,
            l0_source TEXT NOT NULL CHECK(l0_source IN ('stored','git_replay')),
            replay_decl_hash TEXT NOT NULL DEFAULT '',
            signals TEXT NOT NULL,
            scores TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            engine_build TEXT NOT NULL,
            PRIMARY KEY (commit_sha, health_version, l0_source, replay_decl_hash)
        );
        """
    )
    conn.commit()


def _declarations_needs_model_defs_migration(conn: sqlite3.Connection) -> bool:
    """``True`` iff ``declarations`` exists but predates the ``model_defs``
    column (this store's FOURTH schema migration, PR C1 / D4.1) -- a plain
    column, so ``PRAGMA table_info`` suffices."""
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(declarations)").fetchall()
    }
    return "model_defs" not in cols


def _migrate_declarations_model_defs(conn: sqlite3.Connection) -> None:
    """Add the nullable ``model_defs`` column to a pre-PR-C1
    ``declarations`` table. A plain ``ALTER TABLE ... ADD COLUMN`` --
    existing rows get SQL ``NULL`` (read back as Python ``None``, distinct
    from the JSON string ``"null"`` a post-migration ``ingest`` writes for
    a snapshot that declares no ``model_defs`` -- both mean "absent"; see
    ``derive``'s own declarations-loading code for where that
    distinction is handled)."""
    conn.execute("ALTER TABLE declarations ADD COLUMN model_defs TEXT")
    conn.commit()


def _replay_runs_needs_migration(conn: sqlite3.Connection) -> bool:
    """``True`` iff ``replay_runs`` exists but its ``reason`` CHECK
    constraint predates ``'derive'`` (this store's FIFTH schema migration,
    PR C1) -- same ``sqlite_master.sql`` literal-text check as
    ``_unresolved_snapshots_needs_migration`` above, since ``PRAGMA
    table_info`` cannot see CHECK constraints."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'replay_runs'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return False
    return _REPLAY_RUNS_REASON_MIGRATION_MARKER not in row["sql"]


def _migrate_replay_runs(conn: sqlite3.Connection) -> None:
    """Recreate ``replay_runs`` with the widened ``reason`` CHECK
    (``'derive'`` added), preserving every row -- same rename-recreate-
    reinsert shape as ``_migrate_unresolved_snapshots`` (SQLite cannot
    alter a CHECK constraint in place). ``replay_runs`` is run METADATA
    (never the SSoT for anything derivable), so recreating it in place --
    including re-inserting each row's own ``run_id`` explicitly, so
    AUTOINCREMENT identity is preserved -- carries no data-loss risk."""
    conn.execute("ALTER TABLE replay_runs RENAME TO replay_runs__old")
    conn.executescript(
        """
        CREATE TABLE replay_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            health_major INTEGER,
            reason TEXT CHECK(reason IN ('ingest','initial','gap_fill','major_bump','verify','rebuild','derive')),
            commits_total INTEGER,
            commits_done INTEGER,
            skipped_old_format INTEGER,
            unresolved INTEGER,
            engine_build TEXT,
            detail TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO replay_runs (run_id, started_at, finished_at, health_major, "
        "reason, commits_total, commits_done, skipped_old_format, unresolved, "
        "engine_build, detail) "
        "SELECT run_id, started_at, finished_at, health_major, reason, "
        "commits_total, commits_done, skipped_old_format, unresolved, "
        "engine_build, detail FROM replay_runs__old"
    )
    conn.execute("DROP TABLE replay_runs__old")
    conn.commit()


# ---------------------------------------------------------------------------
# Module-layout prefix normalization (engram-alpha#3122)
# ---------------------------------------------------------------------------
#
# health/engine.py's _metric_def_to_dict/_aspect_def_to_dict serialize a
# metric/aspect's signal_fn/scoring_fn as f"{fn.__module__}.
# {fn.__qualname__}", and fn.__module__ depends on how the `health` package
# was imported: `engram.health.metrics` from the `src/`-tree layout,
# `health.metrics` in the deployed plugin (the build flattens
# `src/engram/` away). Two `declarations` rows can therefore carry either
# form for the SAME function -- a layout artifact, never a real declared
# disagreement (borges-funes's diagnosis, issue #3122 comment,
# 2026-09-26). Both the D4.1 ingest-time comparison (in `ingest()`, below)
# and the one-time store-healing migration (`_migrate_declarations_
# prefix_heal`, this store's SIXTH schema migration, wired into
# `open_store()`) canonicalize through the same JSON round-trip: parse ->
# recursively rewrite any string via `health.version.canonical_health_
# dotted` (the ONE shared implementation of the prefix-strip rule,
# engram-alpha#3122 round 2 should-fix 1 -- this module used to carry its
# own copy of the prefix constants, as did `health/engine.py`'s
# `_dotted_name`) -> re-serialize via `_canonical_json` (defined further
# down this module -- a forward reference, resolved only when these
# functions are actually CALLED, by which point the whole module has
# finished importing).


def _normalize_layout_prefix(value):
    """Recursively rewrite any string value carrying the source-tree
    module prefix ``"engram.health."`` (or the bare module name
    ``"engram.health"``) to the deployed-layout form, via
    ``health_version.canonical_health_dotted``. A pure structural walk
    over dict/list/other -- the leaf-string transformation itself is
    delegated to the shared function, so only a string matching the
    prefix AT POSITION 0 (or the bare-module edge case) is rewritten; a
    value that merely CONTAINS ``"engram.health."`` mid-string (never
    actually produced by ``engine.py``'s dotted names, but not assumed
    here) is left untouched."""
    if isinstance(value, str):
        return health_version.canonical_health_dotted(value)
    if isinstance(value, list):
        return [_normalize_layout_prefix(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_layout_prefix(v) for k, v in value.items()}
    return value


def _canonicalize_declared_blob(blob_json):
    """Parse a stored/incoming declared blob's canonical-JSON string,
    recursively normalize any ``engram.health.`` prefix via
    ``_normalize_layout_prefix``, and re-serialize via ``_canonical_json``
    -- the SAME canonical-JSON convention this module already uses for
    these blobs. ``None``/the JSON string ``"null"`` pass through
    unchanged -- the D4.1 gate's own absence handling (in ``ingest()``)
    runs BEFORE this is ever called on a genuine two-sided comparison, so
    by the time this function is invoked to settle a real disagreement
    check, both sides are already known non-absent; the pass-through here
    is only for callers (the healing migration, below) that may hand it a
    raw column value without checking absence first."""
    if blob_json is None or blob_json == "null":
        return blob_json
    parsed = json.loads(blob_json)
    normalized = _normalize_layout_prefix(parsed)
    return _canonical_json(normalized)


def _declarations_needs_prefix_heal(conn: sqlite3.Connection) -> bool:
    """``True`` iff any ``declarations`` row's four blob columns
    (signal_defs/curve_defs/config/model_defs) still carry the literal
    substring ``"engram.health."`` (this store's SIXTH schema migration).
    A row seeded by a manual ``derive``/``ingest`` run from a `src/`-tree
    checkout wrote this prefix into its declared signal_fn/scoring_fn
    names; every snapshot file on disk uses the deployed-layout
    ``"health."`` form instead (engram-alpha#3122). Only non-null columns
    are scanned -- a genuine SQL NULL can never contain the substring, and
    ``LIKE`` against a NULL column is NULL (falsy), never a match, so the
    explicit ``IS NOT NULL`` guards are belt-and-suspenders, not strictly
    required -- kept for readability."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM declarations WHERE "
        "(signal_defs IS NOT NULL AND signal_defs LIKE '%engram.health.%') OR "
        "(curve_defs IS NOT NULL AND curve_defs LIKE '%engram.health.%') OR "
        "(config IS NOT NULL AND config LIKE '%engram.health.%') OR "
        "(model_defs IS NOT NULL AND model_defs LIKE '%engram.health.%')"
    ).fetchone()
    return row["c"] > 0


def _migrate_declarations_prefix_heal(conn: sqlite3.Connection) -> None:
    """Rewrite every stored ``declarations`` row's four blob columns
    through ``_canonicalize_declared_blob`` -- the same JSON-round-trip
    layout-prefix normalization ``ingest``'s D4.1 comparison now applies at
    comparison time -- and ``UPDATE`` any row whose healed content differs
    from what is currently stored.

    Safe under the never-mutate rule (module docstring) the same way
    migrations (1)/(5) above are: ROW IDENTITY (``health_version``, the
    primary key) is untouched, and no row is added or removed. Unlike
    migrations (1)-(5), this one DOES rewrite existing BLOB CONTENT rather
    than only schema (columns/constraints) -- but the content being
    rewritten was never a genuine declaration to begin with: it is a
    source-tree import-layout ARTIFACT of the producer bug this PR fixes
    (borges-funes's diagnosis, engram-alpha#3122), not a real
    signal_defs/curve_defs/config/model_defs value anyone declared on
    purpose. Healing it in place retires that artifact; it does not mutate
    a genuine declaration, because the healed and un-healed forms declare
    the identical function under the identical health_version -- the D4.1
    gate (immediately below, in ``ingest()``) already treats them as
    agreeing for exactly this reason.

    Prints one ``health.series:`` stderr line per healed
    ``(health_version, blob_name)`` pair (engram-alpha#3122 round 2
    should-fix 2) -- a check that used to reject this content now silently
    accepts it, and a silent acceptance of something that used to be
    rejected should leave a mark."""
    rows = conn.execute(
        "SELECT health_version, signal_defs, curve_defs, config, model_defs "
        "FROM declarations"
    ).fetchall()
    for row in rows:
        updates: dict[str, str] = {}
        for blob_name in ("signal_defs", "curve_defs", "config", "model_defs"):
            raw = row[blob_name]
            if raw is None:
                continue
            healed = _canonicalize_declared_blob(raw)
            if healed != raw:
                updates[blob_name] = healed
                print(
                    f"health.series: healed layout-prefix artifact in "
                    f"declarations health_version={row['health_version']!r} "
                    f"blob={blob_name!r} (migration 6, engram-alpha#3122)",
                    file=sys.stderr,
                )
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE declarations SET {set_clause} WHERE health_version = ?",
                (*updates.values(), row["health_version"]),
            )
    conn.commit()


def open_store(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the series store at ``db_path``, with all
    tables created idempotently. Plain rollback journal -- WAL is NOT
    wanted here (design instruction: the store is single-writer, offline).

    Because tables are created with ``CREATE TABLE IF NOT EXISTS``, an
    already-created store keeps whatever CHECK constraints (or column set)
    it was created with -- a schema-level change (widening ``unresolved_
    snapshots.reason`` to add ``commit_already_keyed``; adding ``equality_
    checks.replay_decl_hash``) does not reach an existing store on its own.
    These are the store's SIX schema migrations, run in order after
    creating tables: (1) detect the old ``unresolved_snapshots`` CHECK
    (``PRAGMA table_info`` doesn't expose CHECKs, so the literal is read off
    ``sqlite_master.sql``) and, if found, recreate the table preserving its
    rows; (2) detect a missing ``equality_checks.replay_decl_hash`` column
    (a plain column, so ``PRAGMA table_info`` sees it directly) and, if
    missing, ``ALTER TABLE ADD COLUMN`` it in place; (3) (PR C1) detect a
    pre-C1 ``derived_points`` table (no ``l0_source`` column) and, if EMPTY,
    drop and recreate it under the new DDL (``l0_source``/
    ``replay_decl_hash`` added, PK widened) -- if NON-empty, raise
    ``SeriesIntegrityError`` naming the table rather than silently drop rows
    (no shipped writer populated this table before PR C1's ``derive``, so a
    non-empty pre-C1 table is unexpected); (4) (PR C1) detect a missing
    ``declarations.model_defs`` column and, if missing, ``ALTER TABLE ADD
    COLUMN`` it in place (nullable, D4.1); (5) (PR C1) detect the old
    ``replay_runs`` CHECK (missing the ``'derive'`` reason) and, if found,
    recreate the table preserving its rows (same rename-recreate-reinsert
    shape as migration (1), since SQLite cannot alter a CHECK in place);
    (6) (engram-alpha#3122) detect a ``declarations`` row whose blob
    columns still carry the literal substring ``"engram.health."`` (a
    source-tree import-layout artifact a manual ``derive``/``ingest`` run
    wrote into a declared signal_fn/scoring_fn name) and, if found, rewrite
    every stored row's blobs through the same JSON-round-trip
    layout-prefix normalization the D4.1 gate itself now applies at
    comparison time (see ``_canonicalize_declared_blob``/
    ``_migrate_declarations_prefix_heal``, above). Migrations (1)-(5) are
    safe because every migrated table is DERIVED state -- ``rebuild``/
    ``derive``/``verify`` reconstruct or re-populate each of them -- so
    migrating any of them in place never risks a source of truth;
    migration (6) is additionally safe because the content it rewrites was
    never a genuine declaration to begin with -- see that migration's own
    docstring."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(_SCHEMA)
    conn.commit()
    if _unresolved_snapshots_needs_migration(conn):
        _migrate_unresolved_snapshots(conn)
    if _equality_checks_needs_migration(conn):
        _migrate_equality_checks(conn)
    if _derived_points_needs_migration(conn):
        _migrate_derived_points(conn)
    if _declarations_needs_model_defs_migration(conn):
        _migrate_declarations_model_defs(conn)
    if _replay_runs_needs_migration(conn):
        _migrate_replay_runs(conn)
    if _declarations_needs_prefix_heal(conn):
        _migrate_declarations_prefix_heal(conn)
    return conn


# ---------------------------------------------------------------------------
# Snapshot file discovery
# ---------------------------------------------------------------------------


def snapshot_files(snapshots_dir: str | Path) -> list[Path]:
    """Every file under ``snapshots_dir`` matching the v2 snapshot filename
    shape (``<turn>-<nap|pre_sleep|post_dream>-<timestamp>.json``), sorted by
    the timestamp embedded in the name. ``health-snapshot-dream.json`` and
    ``health-repair-record.json`` (and anything else) are excluded by the
    regex not matching them, never by an explicit name-list -- see health/
    engine.py's module docstring for what those two sibling files are."""
    d = Path(snapshots_dir)
    if not d.is_dir():
        return []
    matches: list[tuple[datetime, Path]] = []
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = _SNAPSHOT_NAME_RE.match(p.name)
        if not m:
            continue
        ts = datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        matches.append((ts, p))
    matches.sort(key=lambda pair: pair[0])
    return [p for _, p in matches]


def parse_snapshot_name(name: str) -> tuple[int, str, datetime]:
    """Parse a snapshot filename into ``(turn, checkpoint_mode, ts)`` where
    ``ts`` is a timezone-aware UTC ``datetime``. Raises ``ValueError`` if
    ``name`` does not match the canonical shape."""
    m = _SNAPSHOT_NAME_RE.match(name)
    if not m:
        raise ValueError(f"not a v2 health-snapshot filename: {name!r}")
    turn = int(m.group("turn"))
    mode = m.group("mode")
    ts = datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    return turn, mode, ts


# ---------------------------------------------------------------------------
# engine_build detection
# ---------------------------------------------------------------------------


def _default_repo_dir() -> Path:
    """The engram-alpha checkout containing THIS module (``src/engram/
    health/series.py`` -> parents[3] == the repo root). Used as the
    ``code_repo_dir`` default for ``status()`` and for the ``code_repo``
    fallback passed to ``_guard_engine_build`` by ``ingest`` / ``backfill``
    / ``verify`` -- NOT by ``detect_engine_build``, which resolves the code
    location from ``module_path`` alone (round 3, #2600)."""
    return Path(__file__).resolve().parents[3]


def _read_alpha_sha(deployed_version_path: Path) -> str | None:
    """Read the ``alpha_sha=`` value (a ``key=value`` file, one entry per
    line) out of ``deployed_version_path``, or ``None`` if the file is
    missing / unreadable / has no such line."""
    try:
        for line in deployed_version_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "alpha_sha" and value.strip():
                return value.strip()
    except OSError:
        pass
    return None


def _git_head(repo_dir: Path) -> str | None:
    """``git rev-parse HEAD`` of ``repo_dir``, or ``None`` on any failure.
    Never raises."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
        )
    except OSError:
        return None
    if result.returncode == 0:
        sha = result.stdout.strip()
        if sha:
            return sha
    return None


def _find_plugin_tree_data_dir(start_dir: Path) -> Path | None:
    """Walk ``start_dir``'s ancestors (inclusive) for a directory whose path
    ENDS in ``marketplace/plugins/engram`` -- the deployed-plugin-tree shape
    -- AND that directly contains ``.engram-build-manifest.json``. Returns
    the parent of that directory's ``marketplace`` segment (i.e. the DATA
    dir, e.g. ``~/.engram``), or ``None`` if no such ancestor exists.

    The suffix check comes first deliberately: a bare
    ``.engram-build-manifest.json`` sitting somewhere unrelated must not be
    mistaken for the plugin tree (#2600's final rule)."""
    for ancestor in [start_dir, *start_dir.parents]:
        parts = ancestor.parts
        if len(parts) >= 3 and parts[-3:] == ("marketplace", "plugins", "engram"):
            if (ancestor / ".engram-build-manifest.json").is_file():
                return ancestor.parents[2]
    return None


def _find_code_repo_toplevel(start_dir: Path) -> Path | None:
    """``git rev-parse --show-toplevel`` from ``start_dir``, but only
    returned if that toplevel is a CODE checkout -- i.e. it contains
    ``src/engram/health/measurements.py``. Returns ``None`` on any git
    failure, or when the toplevel is some OTHER git work tree (notably the
    DATA repo, e.g. ``~/.engram``, which has no ``src/`` at all). Never
    raises."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(start_dir),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    toplevel_str = result.stdout.strip()
    if not toplevel_str:
        return None
    toplevel = Path(toplevel_str)
    if (toplevel / "src" / "engram" / "health" / "measurements.py").exists():
        return toplevel
    return None


def _is_git_dirty(repo_dir: Path, pathspec: str) -> bool:
    """``True`` iff ``git status --porcelain -- pathspec`` reports anything
    non-empty. Never raises -- a git failure reads as "not dirty"."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", pathspec],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
        )
    except OSError:
        return False
    return bool(result.stdout.strip())


_MODULE_PATH_DEFAULT = Path(__file__)
"""Default ``module_path`` for ``detect_engine_build`` -- this module's own
``__file__``. A module-level seam (not a hardcoded literal inside the
function) so tests can monkeypatch it and exercise callers (``ingest`` /
``backfill`` / ``verify``) that invoke ``detect_engine_build()`` bare, with
no way to pass ``module_path`` through."""


def detect_engine_build(module_path: str | Path | None = None) -> str:
    """Resolve the build identifier for the code that is EXECUTING RIGHT
    NOW, never the knowledge/data repo's HEAD (#2600). ONE rule: resolve
    from the executing module's own location. ``module_path`` defaults to
    ``_MODULE_PATH_DEFAULT`` (this module's own ``__file__``) and exists as
    a parameter only so tests can point it at a scratch tree; production
    callers (``ingest`` / ``backfill`` / ``verify``) call this bare, with no
    arguments at all.

    Order, LOAD-BEARING (#2600's final rule -- a fix to round 1's fix;
    round 3 dropped round 1's separate ``data_dir``/``code_repo_dir``
    override path, which was a SECOND rule bolted onto this one purely to
    keep an old test passing):

    1. env ``ENGRAM_ENGINE_BUILD`` -- explicit override, wins unconditionally.
    2. **Plugin-tree marker, checked BEFORE any git question.** Walk
       ``module_path``'s ancestors for a directory whose path ends in
       ``marketplace/plugins/engram`` and that contains
       ``.engram-build-manifest.json``. If found, the answer is
       ``alpha_sha=`` from ``<data_dir>/.deployed-version`` (``data_dir`` =
       the parent of that directory's ``marketplace`` segment); a marker
       present with no ``alpha_sha`` line is ``'unknown'`` plus one stderr
       line (no further fallback -- we already know we're in a deployed
       tree, so falling through to git would silently reproduce the
       original defect, see point 3).
    3. **Git work tree -- ONLY if it is a CODE checkout.**
       ``git rev-parse --show-toplevel`` from ``module_path``'s directory,
       but ONLY accepted if that toplevel contains
       ``src/engram/health/measurements.py``. If accepted: ``git rev-parse
       HEAD`` of that toplevel, with a ``-dirty`` suffix when
       ``git status --porcelain -- src/engram/health`` is non-empty.
    4. else ``'unknown'`` plus one stderr line.

    WHY step 2 must run before step 3, and why this is not merely an
    ordering preference: the deployed plugin tree is NESTED INSIDE the data
    repo's own git work tree (measured on two seats --
    ``git -C ~/.engram/marketplace/plugins/engram rev-parse --show-toplevel``
    returns ``~/.engram`` itself, a real directory nesting, not a symlink).
    So a rule that asks the git question first, for a module executing from
    the deployed tree, returns the DATA repo's HEAD -- the exact #2600
    defect, reached through a principled-looking door. Only the
    plugin-tree-marker check (which looks for the deployed-tree SHAPE, not
    "some git repo") can tell the two containments apart; hence it goes
    first, and step 3's own ``src/engram/health/measurements.py`` guard
    exists to refuse the data repo a SECOND time in case the marker check
    doesn't fire for some other reason (e.g. the marker file itself is
    absent from a given install).

    WHY the discriminator in step 3 names ``measurements.py`` specifically,
    and not e.g. ``server.py`` or ``pyproject.toml`` (round 3, #2600): the
    module EXECUTING this resolution is ``series.py`` itself, added
    2026-09-05 -- strictly newer than ``measurements.py``, added
    2026-09-04. Every tree that can even reach this code already carries
    ``measurements.py``, so the marker never under-fires against a real
    engram-alpha checkout capable of running it. More load-bearing than the
    freshness argument: ``measurements.py`` is the code that COMPUTES the
    health rows this whole module is building a build-identifier FOR --
    "is this the tree that produced these numbers" is exactly what
    ``engine_build`` means. ``server.py`` or ``pyproject.toml`` would
    answer a neighbouring question -- "is this an ENGRAM checkout at all"
    -- which is necessary but not sufficient; a checkout could carry
    ``server.py`` while lacking the health subsystem entirely (e.g. a
    stale pre-#2537 clone), and ``engine_build`` would then name a build
    that never ran this code.

    ``_guard_engine_build`` stays as defense in depth on top of this."""
    import os

    env_val = os.environ.get("ENGRAM_ENGINE_BUILD")
    if env_val:
        return env_val

    effective_module_path = (
        Path(module_path) if module_path is not None else _MODULE_PATH_DEFAULT
    )
    module_dir = effective_module_path.resolve().parent

    plugin_data_dir = _find_plugin_tree_data_dir(module_dir)
    if plugin_data_dir is not None:
        alpha = _read_alpha_sha(plugin_data_dir / ".deployed-version")
        if alpha:
            return alpha
        print(
            "health.series: engine_build -- found a deployed plugin-tree "
            f"marker under {plugin_data_dir} but no alpha_sha= line in "
            f"{plugin_data_dir / '.deployed-version'}; recording 'unknown' "
            "(see #2600).",
            file=sys.stderr,
        )
        return "unknown"

    code_toplevel = _find_code_repo_toplevel(module_dir)
    if code_toplevel is not None:
        sha = _git_head(code_toplevel)
        if sha:
            if _is_git_dirty(code_toplevel, "src/engram/health"):
                return f"{sha}-dirty"
            return sha

    print(
        "health.series: engine_build could not be resolved from the "
        f"executing module's location ({module_dir}) -- no env override, "
        "no deployed plugin-tree marker, and no git checkout containing "
        "src/engram/health/measurements.py; recording 'unknown' (see #2600).",
        file=sys.stderr,
    )
    return "unknown"


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _guard_engine_build(
    build: str, data_repo: str | Path, code_repo: str | Path | None
) -> str:
    """Loud-not-fatal collision guard (#2600): if ``build`` LOOKS like a git
    sha (7-to-40-char lowercase hex) and resolves as a commit in
    ``data_repo`` but NOT in ``code_repo``, it names a commit of the wrong
    repository -- return the literal ``'unknown'`` and print one stderr
    line naming both repos and the offending sha. Any other input (a
    non-sha value such as ``'unknown'`` or a ``.deployed-version``
    ``alpha_sha``; a sha that resolves in ``code_repo``, whether or not it
    also resolves in ``data_repo``; or a sha that resolves in neither) is
    returned UNCHANGED. This is a narrow collision check for the one
    ambiguous case, NOT a general validator of ``build`` -- it does not
    confirm ``build`` is otherwise well-formed or meaningful.

    ``code_repo=None`` (#2620): callers pass this when
    ``_find_code_repo_toplevel`` could not resolve a code checkout --
    notably a deployed plugin tree, where that resolver correctly returns
    ``None`` because the tree it walked up from is the DATA repo, not a
    code checkout. Callers MUST NOT fall back to an undiscriminated
    resolver (e.g. ``_default_repo_dir()``) in this case: doing so hands
    this function the data repo itself as ``code_repo``, so
    ``_commit_exists(data_repo, build)`` and ``_commit_exists(code_repo,
    build)`` become the same query against the same repository -- the
    second is ``True`` whenever the first is, and the guard can never
    fire on the exact input it exists to catch (a real #2600 collision,
    caught silently reintroduced in the guard meant to catch it). With
    ``code_repo=None`` the collision check is skipped outright (there is
    nothing to compare against) and ``build`` is returned UNCHANGED, with
    one stderr line noting the check was skipped -- loud about the
    degraded state, rather than silently comparing a repo against
    itself."""
    if not _SHA_RE.match(build):
        return build

    if not _commit_exists(data_repo, build):
        return build

    if code_repo is None:
        print(
            f"health.series: engine_build {build!r} looks like a sha and "
            f"resolves as a commit in the data repo ({data_repo}), but no "
            "code repo could be resolved to check it against -- skipping "
            f"the collision check and recording {build!r} unverified "
            "(see #2600, #2620).",
            file=sys.stderr,
        )
        return build

    if _commit_exists(code_repo, build):
        return build

    print(
        f"health.series: engine_build {build!r} resolves as a commit in the "
        f"data repo ({data_repo}) but not in the code repo ({code_repo}) -- "
        "recording 'unknown' instead of a data-repo sha (see #2600).",
        file=sys.stderr,
    )
    return "unknown"


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

_RECORD_SEP = "\x1f"


def knowledge_commits(repo_dir: str | Path) -> list[tuple[str, str]]:
    """``[(sha, committer_iso), ...]`` for every commit touching
    ``knowledge.sql`` in ``repo_dir``, oldest first. Committer time
    (``%cI``), not author time (design doc section 5)."""
    result = subprocess.run(
        [
            "git",
            "log",
            f"--format=%H{_RECORD_SEP}%cI",
            "--reverse",
            "--",
            "knowledge.sql",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    commits: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, committer_iso = line.partition(_RECORD_SEP)
        commits.append((sha, committer_iso))
    return commits


def _commit_exists(repo_dir: str | Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    return result.returncode == 0


def _first_knowledge_commit_after(
    repo_dir: str | Path, anchor: str
) -> tuple[str, str] | None:
    """First ``(sha, committer_iso)`` touching ``knowledge.sql`` strictly
    after ``anchor`` (``anchor..HEAD``), oldest-of-the-range first, or
    ``None`` if there is no such commit."""
    result = subprocess.run(
        [
            "git",
            "log",
            "--reverse",
            f"--format=%H{_RECORD_SEP}%cI",
            f"{anchor}..HEAD",
            "--",
            "knowledge.sql",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, committer_iso = line.partition(_RECORD_SEP)
        return sha, committer_iso
    return None


# ---------------------------------------------------------------------------
# Anchor resolution (D1)
# ---------------------------------------------------------------------------


def resolve_anchor(
    repo_dir: str | Path,
    anchor: str | None,
    snapshot_ts: datetime,
    anchor_claims: dict[str, int],
    lag: timedelta = timedelta(minutes=10),
) -> tuple[str, str | None, str | None, str | None]:
    """Resolve a snapshot's true ``knowledge.sql`` commit, per D1.

    Returns ``(status, commit_sha, commit_time, reason)`` where ``status``
    is one of ``child_of_anchor`` / ``by_time`` / ``ambiguous`` /
    ``unresolved``. On ``ambiguous``/``unresolved``, ``commit_sha`` and
    ``commit_time`` are ``None`` and ``reason`` names why (one of
    ``shared_anchor`` / ``anchor_not_in_repo`` / ``no_child_commit`` /
    ``by_time_out_of_bound``).

    ``anchor_claims`` is ``{anchor: count of snapshot files carrying it}``
    over the WHOLE snapshots directory, computed once per ingest run by the
    caller (D1: two or more snapshot files sharing one ``anchor`` means the
    async worker lagged past two checkpoints -- none of them can be safely
    keyed, since a guessed key would be a false row).
    """
    if anchor is not None:
        if anchor_claims.get(anchor, 0) >= 2:
            return ("ambiguous", None, None, "shared_anchor")
        if not _commit_exists(repo_dir, anchor):
            return ("unresolved", None, None, "anchor_not_in_repo")
        child = _first_knowledge_commit_after(repo_dir, anchor)
        if child is None:
            return ("unresolved", None, None, "no_child_commit")
        sha, committer_iso = child
        return ("child_of_anchor", sha, committer_iso, None)

    # anchor is None -> by_time: first knowledge.sql commit with committer
    # time >= snapshot_ts, within `lag`.
    for sha, committer_iso in knowledge_commits(repo_dir):
        commit_time = datetime.fromisoformat(committer_iso)
        if commit_time >= snapshot_ts and (commit_time - snapshot_ts) <= lag:
            return ("by_time", sha, committer_iso, None)
    return ("unresolved", None, None, "by_time_out_of_bound")


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now_iso(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def ingest(
    snapshots_dir: str | Path,
    repo_dir: str | Path,
    db_path: str | Path,
    engine_build: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Scan ``snapshots_dir`` for v2 snapshot files, refuse old-format ones
    loudly, gate on declared ``health_version`` blobs (D4.1 -- raises
    ``SeriesIntegrityError`` on conflict, the ONE path that aborts the run),
    resolve each file's anchor (D1), and upsert ``stored`` rows for every
    file that resolves. Idempotent: a file whose row already exists UNDER
    THE SAME ``snapshot_file`` is counted under ``already_present``, never
    re-inserted (``INSERT OR IGNORE``) and never updated (never-mutate
    rule).

    A file that resolves to a ``(commit_sha, health_major)`` already keyed
    by a DIFFERENT ``snapshot_file`` is a distinct case from re-ingesting
    the same file -- two files keyed to one commit means either the anchor
    resolution is wrong or the worker committed once for two checkpoints,
    and ``INSERT OR IGNORE`` would silently drop it into ``already_present``,
    masking the collision. Instead: no insert into ``l0_points``; the file
    is recorded in ``unresolved_snapshots`` with reason
    ``commit_already_keyed``; the run is counted under the ``collided`` key
    and ``detail.collisions`` lists ``{file, commit_sha, keyed_by}`` for
    each one.

    Returns the run record as a dict; the same dict (as JSON, with
    ``detail`` itself JSON-encoded) is inserted into ``replay_runs`` with
    ``reason='ingest'``.

    **The declarations gate now compares FOUR blobs (PR C1, D4.1's own
    delta): ``signal_defs``/``curve_defs``/``config``/``model_defs``.**
    "Absence is not disagreement" -- comparison happens only when BOTH
    sides declare a value (``_canonical_json(None) == "null"``, or a
    genuine SQL ``NULL`` on a pre-migration-(4) row, both read as
    "absent"). A stored absence filled by a later non-null value is a
    FILL: the row is ``UPDATE``d and ``detail.declaration_fills`` lists
    ``{health_version, blob, file}`` for each one. A stored non-null
    against a later absence is accepted SILENTLY (an older build wrote
    that snapshot -- it declared LESS, not DIFFERENTLY). Two non-null
    values that differ is still the loud ``SeriesIntegrityError``. Why this
    matters concretely: seats on the ``deploy/2026-09-05-health-v1.0`` tag
    write ``health-v1.0`` snapshots WITHOUT ``model_defs`` (added by PR C0,
    after that tag was cut -- no version bump, since the tag-diff gate
    reads L0 only), while a dev seat writes ``health-v1.0`` WITH it. Same
    version, same declared content wherever both sides declare -- that is
    not a version-discipline violation. Two ``signal_defs`` blobs that
    differ still is.
    """
    conn = open_store(db_path)
    try:
        started_at = _utc_now_iso(now)
        if engine_build is not None:
            build = engine_build
        else:
            build = detect_engine_build()
            build = _guard_engine_build(
                build,
                data_repo=repo_dir,
                # #2620: no fallback to _default_repo_dir() -- a None here
                # means we're in a deployed (data-repo) tree, and falling
                # back to the undiscriminated resolver would hand the
                # guard the data repo as code_repo, disarming it on the
                # exact input it exists to catch. None propagates.
                code_repo=_find_code_repo_toplevel(Path(__file__).resolve().parent),
            )

        files = snapshot_files(snapshots_dir)

        # anchor_claims: {anchor: count of files carrying it}, over the WHOLE
        # snapshots dir, computed once (D1). Only meaningful-format files that
        # actually carry an "anchor" key are counted -- an old-format file is
        # refused before it ever reaches anchor resolution, so it must not
        # pollute this count either.
        anchor_claims: dict[str, int] = {}
        parsed_snapshots: list[tuple[Path, dict]] = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            parsed_snapshots.append((f, data))
            if "measurements" in data:
                anchor = data.get("anchor")
                if anchor is not None:
                    anchor_claims[anchor] = anchor_claims.get(anchor, 0) + 1

        commits_total = len(files)
        commits_done = 0
        already_present = 0
        skipped_old_format = 0
        skipped_unreadable = 0
        collided = 0
        detail: dict[str, list] = {
            "old_format": [],
            "unreadable": [],
            "collisions": [],
            "declaration_fills": [],
            "layout_prefix_normalized": [],
        }

        # Track which files were actually parsed, so unreadable ones are
        # identified by filename even though they never made it into
        # parsed_snapshots.
        parsed_names = {f.name for f, _ in parsed_snapshots}
        for f in files:
            if f.name not in parsed_names:
                skipped_unreadable += 1
                detail["unreadable"].append(f.name)

        for f, snapshot in parsed_snapshots:
            if "measurements" not in snapshot:
                skipped_old_format += 1
                detail["old_format"].append(f.name)
                print(
                    f"health.series: refusing old-format snapshot {f.name} "
                    "(no 'measurements' key -- see viz/data/health_v2.py's "
                    "old-format guard, 'measurements' not in snapshot)",
                    file=sys.stderr,
                )
                continue

            hv = snapshot["health_version"]
            signal_defs_json = _canonical_json(snapshot.get("signal_defs"))
            curve_defs_json = _canonical_json(snapshot.get("curve_defs"))
            config_json = _canonical_json(snapshot.get("config"))
            # (PR C1, D4.1) model_defs -- the fourth declared blob, added by
            # engram-alpha#2575 PR C0. Older seats (e.g. one still on the
            # deploy/2026-09-05-health-v1.0 tag) write health-v1.0 snapshots
            # WITHOUT this key -- `_canonical_json(None) == "null"`.
            model_defs_json = _canonical_json(snapshot.get("model_defs"))

            existing_decl = conn.execute(
                "SELECT signal_defs, curve_defs, config, model_defs FROM declarations "
                "WHERE health_version = ?",
                (hv,),
            ).fetchone()
            if existing_decl is None:
                conn.execute(
                    "INSERT INTO declarations "
                    "(health_version, signal_defs, curve_defs, config, model_defs, "
                    "first_seen_commit, first_seen_file, engine_build) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        hv,
                        signal_defs_json,
                        curve_defs_json,
                        config_json,
                        model_defs_json,
                        None,
                        f.name,
                        build,
                    ),
                )
                conn.commit()
            else:
                # (PR C1, D4.1's own delta) "Absence is not disagreement":
                # comparison happens only when BOTH sides are non-null
                # (`_canonical_json(None) == "null"` is the absent sentinel
                # for a canonical-JSON-encoded blob; `existing_decl[
                # "model_defs"]` can ALSO be a genuine SQL NULL -- i.e.
                # Python `None` -- for a row created before migration (4)
                # added the column, which this loop treats the same as the
                # string "null"). A stored `null`/`None` filled by a later
                # non-null value is a FILL (UPDATE + record it); a stored
                # non-null against a later `null` is accepted SILENTLY (an
                # older build wrote that snapshot -- it declared less, not
                # differently); two non-null values that differ is still
                # the loud D4.1 violation.
                update_fields: dict[str, str] = {}
                first_diff: str | None = None
                for blob_name, new_val in (
                    ("signal_defs", signal_defs_json),
                    ("curve_defs", curve_defs_json),
                    ("config", config_json),
                    ("model_defs", model_defs_json),
                ):
                    old_val = existing_decl[blob_name]
                    old_absent = old_val is None or old_val == "null"
                    new_absent = new_val == "null"
                    if old_absent and new_absent:
                        continue
                    if old_absent and not new_absent:
                        update_fields[blob_name] = new_val
                        detail["declaration_fills"].append(
                            {"health_version": hv, "blob": blob_name, "file": f.name}
                        )
                        continue
                    if (not old_absent) and new_absent:
                        continue
                    # (engram-alpha#3122) canonicalize BOTH sides through
                    # the same source-tree/deployed-layout module-prefix
                    # normalization before the byte-for-byte comparison --
                    # a stored declaration seeded from a `src/`-layout
                    # checkout (`engram.health.metrics.x`-shaped
                    # signal_fn/scoring_fn names) must not be flagged as
                    # disagreeing with a snapshot using the deployed-layout
                    # equivalent (`health.metrics.x`) for the SAME
                    # function -- that is an import-layout artifact, never
                    # a real declared disagreement. A genuine value
                    # difference (e.g. a real weight/threshold change)
                    # still differs after normalization and still raises.
                    # By the time this line runs, `open_store()` has
                    # already run migration 6, so the STORED side here is
                    # already canonical -- what this comparison-time
                    # normalization actually guards against is the
                    # INCOMING side: a snapshot file freshly written by a
                    # process running from a `src/`-layout checkout (round
                    # 2 nit, engram-alpha#3122).
                    canon_old = _canonicalize_declared_blob(old_val)
                    canon_new = _canonicalize_declared_blob(new_val)
                    if canon_old != canon_new:
                        first_diff = blob_name
                        break
                    if old_val != new_val:
                        # Raw values differ but canonicalized values agree
                        # -- exactly the layout-prefix-only case #3122
                        # describes, not a genuine agreement. Recorded
                        # (round 2 should-fix 2) so a gate that now
                        # silently accepts what it used to reject leaves a
                        # visible trace, same shape as `declaration_fills`
                        # above.
                        detail["layout_prefix_normalized"].append(
                            {"health_version": hv, "blob": blob_name, "file": f.name}
                        )
                if first_diff is not None:
                    raise SeriesIntegrityError(
                        f"declarations gate: health_version {hv!r} already has a "
                        f"stored declaration that disagrees with {f.name!r} in "
                        f"{first_diff!r} (D4.1: a version's declared signal_defs/"
                        "curve_defs/config/model_defs must never differ across "
                        "snapshots where BOTH sides declare a value -- absence "
                        "on one side is a fill or a silently-accepted lesser "
                        "declaration, never a disagreement)."
                    )
                if update_fields:
                    set_clause = ", ".join(f"{k} = ?" for k in update_fields)
                    conn.execute(
                        f"UPDATE declarations SET {set_clause} WHERE health_version = ?",
                        (*update_fields.values(), hv),
                    )
                    conn.commit()

            _, mode, ts = parse_snapshot_name(f.name)
            anchor = snapshot.get("anchor")
            anchor_status, commit_sha, commit_time, reason = resolve_anchor(
                repo_dir, anchor, ts, anchor_claims
            )

            if anchor_status in ("ambiguous", "unresolved"):
                conn.execute(
                    "INSERT INTO unresolved_snapshots (snapshot_file, anchor, reason, seen_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(snapshot_file) DO UPDATE SET "
                    "anchor=excluded.anchor, reason=excluded.reason, seen_at=excluded.seen_at",
                    (f.name, anchor, reason, _utc_now_iso(now)),
                )
                conn.commit()
                continue

            # resolved: child_of_anchor or by_time
            health_major = snapshot["raw_schema_version"]
            turn = snapshot.get("turn")
            measurements_json = _canonical_json(snapshot["measurements"])
            computed_at = _utc_now_iso(now)

            before = conn.execute(
                "SELECT snapshot_file FROM l0_points WHERE commit_sha = ? AND health_major = ? "
                "AND source = 'stored' AND replay_decl_hash = ''",
                (commit_sha, health_major),
            ).fetchone()

            if before is not None and before["snapshot_file"] != f.name:
                # This file's anchor resolved to a (commit_sha, health_major)
                # already keyed by a DIFFERENT snapshot file -- either the
                # anchor resolution is wrong or the worker committed once for
                # two checkpoints. INSERT OR IGNORE would silently drop this
                # row and be indistinguishable from the legitimate same-file
                # idempotent case -- record it loudly instead, never insert.
                keyed_by = before["snapshot_file"]
                conn.execute(
                    "INSERT INTO unresolved_snapshots (snapshot_file, anchor, reason, seen_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(snapshot_file) DO UPDATE SET "
                    "anchor=excluded.anchor, reason=excluded.reason, seen_at=excluded.seen_at",
                    (f.name, anchor, "commit_already_keyed", _utc_now_iso(now)),
                )
                conn.commit()
                collided += 1
                detail["collisions"].append(
                    {"file": f.name, "commit_sha": commit_sha, "keyed_by": keyed_by}
                )
                continue

            conn.execute(
                "INSERT OR IGNORE INTO l0_points "
                "(commit_sha, health_major, commit_time, turn, source, "
                "replay_decl_hash, snapshot_file, anchor_pre_commit, "
                "anchor_resolution, computed_at, engine_build, measurements, "
                "unreplayable) "
                "VALUES (?, ?, ?, ?, 'stored', '', ?, ?, ?, ?, ?, ?, '{}')",
                (
                    commit_sha,
                    health_major,
                    commit_time,
                    turn,
                    f.name,
                    anchor,
                    anchor_status,
                    computed_at,
                    build,
                    measurements_json,
                ),
            )
            conn.commit()

            if before is not None:
                already_present += 1
            else:
                commits_done += 1
                # a previously unresolved file that now resolves: drop its
                # unresolved_snapshots row (D1: re-ingest retries).
                conn.execute(
                    "DELETE FROM unresolved_snapshots WHERE snapshot_file = ?",
                    (f.name,),
                )
                conn.commit()

        unresolved_count = conn.execute(
            "SELECT COUNT(*) AS c FROM unresolved_snapshots"
        ).fetchone()["c"]

        finished_at = _utc_now_iso(now)
        run_record = {
            "reason": "ingest",
            "started_at": started_at,
            "finished_at": finished_at,
            "health_major": None,
            "commits_total": commits_total,
            "commits_done": commits_done,
            "already_present": already_present,
            "skipped_old_format": skipped_old_format,
            "skipped_unreadable": skipped_unreadable,
            "collided": collided,
            "unresolved": unresolved_count,
            "engine_build": build,
            "detail": detail,
        }

        conn.execute(
            "INSERT INTO replay_runs "
            "(started_at, finished_at, health_major, reason, commits_total, "
            "commits_done, skipped_old_format, unresolved, engine_build, detail) "
            "VALUES (?, ?, ?, 'ingest', ?, ?, ?, ?, ?, ?)",
            (
                started_at,
                finished_at,
                None,
                commits_total,
                commits_done,
                skipped_old_format,
                unresolved_count,
                build,
                _canonical_json(run_record["detail"]),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return run_record


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------


def _dump_table_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    pk_cols = _PRIMARY_KEYS[table]
    order_by = ", ".join(pk_cols)
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    return [dict(row) for row in rows]


def _compare_dumps(
    old_rows: list[dict], new_rows: list[dict], table: str
) -> list[dict]:
    exclude = _REBUILD_DIFF_EXCLUDE_COLUMNS.get(table, set())
    pk_cols = _PRIMARY_KEYS[table]

    def key(row: dict) -> tuple:
        return tuple(row[c] for c in pk_cols)

    old_by_key = {key(r): r for r in old_rows}
    new_by_key = {key(r): r for r in new_rows}

    diffs: list[dict] = []
    all_keys = sorted(set(old_by_key) | set(new_by_key), key=lambda k: [str(x) for x in k])
    for k in all_keys:
        old_row = old_by_key.get(k)
        new_row = new_by_key.get(k)
        if old_row is None:
            diffs.append(
                {"table": table, "key": list(k), "field": "<row>", "old": None, "new": "present"}
            )
            continue
        if new_row is None:
            diffs.append(
                {"table": table, "key": list(k), "field": "<row>", "old": "present", "new": None}
            )
            continue
        fields = sorted(set(old_row) | set(new_row))
        for field in fields:
            if field in exclude:
                continue
            if old_row.get(field) != new_row.get(field):
                diffs.append(
                    {
                        "table": table,
                        "key": list(k),
                        "field": field,
                        "old": old_row.get(field),
                        "new": new_row.get(field),
                    }
                )
    return diffs


def _rebuild_single_diff(
    snapshots_dir: str | Path,
    repo_dir: str | Path,
    db_path: str | Path,
    engine_build: str | None = None,
) -> dict:
    """The PR A/B1 single-diff form of ``rebuild`` (design doc section 2),
    kept ONLY as the explicit opt-back behind ``rebuild(..., single_diff=
    True)`` / the CLI's ``--single-diff`` -- see ``rebuild``'s own
    docstring for why this confounds content and build change and is never
    the default from PR B2 onward.

    Build a fresh store at ``<db_path>.rebuild`` via ``ingest``, then
    compare it against the existing ``db_path`` with an ORDERED DUMP
    COMPARE over ``l0_points``/``declarations``/``unresolved_snapshots``
    (ordered by primary key; run-metadata columns -- ``computed_at``,
    ``engine_build``, ``seen_at``, and every ``replay_runs`` row -- are
    excluded, see module-level ``_REBUILD_DIFF_EXCLUDE_COLUMNS``).

    Returns ``{identical, differences, rebuilt_path}``. If identical, the
    live store at ``db_path`` is atomically replaced (``os.replace``) with
    the freshly rebuilt one; if not, BOTH files are left in place and the
    differences are returned -- a disagreeing store is never replaced.
    """
    import os

    db_path = Path(db_path)
    rebuilt_path = Path(str(db_path) + ".rebuild")
    rebuilt_path.unlink(missing_ok=True)
    for suffix in ("-journal", "-wal", "-shm"):
        Path(str(rebuilt_path) + suffix).unlink(missing_ok=True)

    ingest(snapshots_dir, repo_dir, rebuilt_path, engine_build=engine_build)

    differences: list[dict] = []
    if db_path.exists():
        old_conn = open_store(db_path)
        try:
            new_conn = open_store(rebuilt_path)
            try:
                for table in _REBUILD_DIFF_TABLES:
                    old_rows = _dump_table_rows(old_conn, table)
                    new_rows = _dump_table_rows(new_conn, table)
                    differences.extend(_compare_dumps(old_rows, new_rows, table))
            finally:
                new_conn.close()
        finally:
            old_conn.close()
    else:
        # No existing store to compare against -- an empty old store is
        # vacuously "identical" only if the rebuilt store is also empty;
        # otherwise every rebuilt row is a difference. Since there's
        # nothing meaningful to diff against, treat "no existing store" as
        # identical (a first-ever build) and let the caller install it.
        differences = []

    identical = not differences
    if identical:
        os.replace(str(rebuilt_path), str(db_path))

    return {
        "identical": identical,
        "differences": differences,
        "rebuilt_path": str(rebuilt_path),
    }


# ---------------------------------------------------------------------------
# rebuild -- two-diff form (D4.2, PR B2)
# ---------------------------------------------------------------------------


_DIRTY_SUFFIX = "-dirty"


def _strip_dirty_suffix(build: str) -> "tuple[str, bool]":
    """``(sha, True)`` if ``build`` ends in ``-dirty``, else ``(build,
    False)`` unchanged."""
    if build.endswith(_DIRTY_SUFFIX):
        return build[: -len(_DIRTY_SUFFIX)], True
    return build, False


class _RegenerateUnavailable(Exception):
    """Raised by a ``regenerate_under_build`` implementation (default or
    injected) when a historical build's side of the two-diff cannot be
    produced -- not a commit in ``alpha_repo``, a pre-B1 tree missing a
    subcommand this call uses, an archive/subprocess failure, ... -- and
    caught by the two-diff ``rebuild`` loop, which turns it into a
    ``{"status": "unavailable", "reason": ...}`` ``build_differences`` entry
    and a confounded content diff (D4.2's "Unavailable build side"). Never
    left to propagate out of ``rebuild`` itself."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _regenerate_failure_reason(exc: "Exception") -> str:
    """The reason string to record for a ``regenerate_under_build`` failure
    -- ``exc.reason`` for ``_RegenerateUnavailable`` (the default
    implementation's own exception), or the child's stderr tail for a bare
    ``subprocess.CalledProcessError`` (what an INJECTED ``regenerate_under_
    build`` may raise directly, e.g. in a test standing in for a pre-B1
    tree that never got the chance to wrap it) -- either way, the two-diff
    ``rebuild`` loop never lets either exception type propagate; both are
    turned into an ``{"status": "unavailable", "reason": ...}``
    ``build_differences`` entry."""
    if isinstance(exc, _RegenerateUnavailable):
        return exc.reason
    stderr = exc.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    stderr_tail = (stderr or "")[-2000:]
    return f"{exc} (exit {exc.returncode}): {stderr_tail}"


def _is_git_worktree(path: "str | Path") -> bool:
    """``True`` iff ``git rev-parse --is-inside-work-tree`` succeeds and
    prints ``true`` for ``path``. Never raises -- a git failure (missing
    ``git``, ``path`` not existing, ``path`` not a repo at all) reads as
    "not a work tree", per ``rebuild``'s own contract: a non-worktree
    ``alpha_repo`` makes the build side ``unavailable``, never an
    exception."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(path),
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _default_regenerate_under_build(
    alpha_repo: "str | Path",
    engine_build: str,
    snapshots_dir: "str | Path",
    repo_dir: "str | Path",
    out_db: "str | Path",
    keep_scratch: bool = False,
) -> None:
    """Default ``regenerate_under_build`` (D4.2): archive ``engine_build``'s
    ``src/engram`` tree out of ``alpha_repo`` (``git -C <alpha_repo> archive
    --format=tar <engine_build> src/engram`` -- a ``subprocess.run([...])``
    ARGUMENT LIST, never a shell pipe; the tar bytes are extracted via
    ``tarfile`` from an in-memory buffer) into a fresh ``tempfile.
    mkdtemp()``, then run THAT scratch tree's own ``health/series.py`` as a
    SUBPROCESS -- ``ingest`` then ``backfill`` -- against the SAME
    ``snapshots_dir``/``repo_dir`` and a fresh ``out_db``, with
    ``ENGRAM_ENGINE_BUILD=<engine_build>`` set on the child (so the scratch
    tree's own rows stamp ``engine_build`` exactly -- ``detect_engine_
    build``'s rule step 1 always wins, so the guard never fires on a
    scratch path) and ``ENGRAM_HOME`` pointed at a second, unrelated fresh
    temp dir as a belt-and-braces measure (``series.py`` never reads it,
    but nothing here should risk resolving a real ``~/.engram``).

    Never imports the scratch tree's code into THIS process -- the
    ``sys.executable`` subprocess call is the whole point (module docstring
    above).

    Raises ``_RegenerateUnavailable`` -- never a bare ``subprocess.
    CalledProcessError``, never any other exception -- on ANY archive or
    subprocess failure (``engine_build`` not a commit in ``alpha_repo``, a
    pre-B1 tree with no ``backfill`` subcommand, a scratch tree whose
    ``ingest``/``backfill`` otherwise exits non-zero, ...), carrying the
    failing child's stderr tail. The scratch tree (and its throwaway
    ``ENGRAM_HOME``) is removed afterwards unless ``keep_scratch``."""
    scratch_dir = Path(tempfile.mkdtemp(prefix="health-series-rebuild-"))
    scratch_home = Path(tempfile.mkdtemp(prefix="health-series-rebuild-home-"))
    try:
        archive = subprocess.run(
            ["git", "-C", str(alpha_repo), "archive", "--format=tar", engine_build, "src/engram"],
            check=False,
            capture_output=True,
        )
        if archive.returncode != 0:
            stderr_tail = archive.stderr.decode("utf-8", errors="replace")[-2000:]
            raise _RegenerateUnavailable(
                f"git archive {engine_build!r} from {alpha_repo!r} failed: {stderr_tail}"
            )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
            try:
                tf.extractall(str(scratch_dir), filter="data")
            except TypeError:  # pragma: no cover -- Python < 3.12 has no `filter=`
                tf.extractall(str(scratch_dir))

        scratch_series = scratch_dir / "src" / "engram" / "health" / "series.py"
        if not scratch_series.is_file():
            raise _RegenerateUnavailable(
                f"archived tree for {engine_build!r} has no "
                "src/engram/health/series.py (pre-health tree?)"
            )

        import os

        child_env = dict(os.environ)
        child_env["ENGRAM_ENGINE_BUILD"] = engine_build
        child_env["ENGRAM_HOME"] = str(scratch_home)

        for subcommand, extra_args in (
            ("ingest", ["--snapshots-dir", str(snapshots_dir), "--repo-dir", str(repo_dir)]),
            ("backfill", ["--repo-dir", str(repo_dir)]),
        ):
            result = subprocess.run(
                [sys.executable, str(scratch_series), subcommand, "--db", str(out_db), *extra_args],
                check=False,
                capture_output=True,
                text=True,
                env=child_env,
            )
            if result.returncode != 0:
                stderr_tail = result.stderr[-2000:]
                raise _RegenerateUnavailable(
                    f"scratch tree for {engine_build!r} failed running "
                    f"{subcommand!r} (exit {result.returncode}): {stderr_tail}"
                )
    finally:
        if not keep_scratch:
            shutil.rmtree(str(scratch_dir), ignore_errors=True)
            shutil.rmtree(str(scratch_home), ignore_errors=True)


def _compare_content_diff(old_rows: "list[dict]", new_rows: "list[dict]") -> "list[dict]":
    """The content-diff half of D4.2: ``_compare_dumps`` over ``l0_points``,
    then filtered to drop "row exists only in the fresh regenerate" entries.
    A full ``regenerate_under_build`` necessarily replays EVERY commit
    ``knowledge_commits(repo_dir)`` finds today, which can be MORE commits
    than were live under a given build historically (new commits landed
    since, or the build's own rows were never fully backfilled) -- that is
    newly-computed COVERAGE, not a data change, so it is not a content
    difference. A row present in the live store under this build but
    MISSING from the fresh regenerate (history rewritten out from under it)
    is still a genuine content difference and is kept."""
    diffs = _compare_dumps(old_rows, new_rows, "l0_points")
    return [d for d in diffs if not (d["field"] == "<row>" and d["old"] is None)]


_BUILD_DIFF_EXCLUDE_COLUMNS = {"computed_at", "engine_build"}


def _compare_build_diff(e_rows: "list[dict]", head_rows: "list[dict]") -> "list[dict]":
    """The build-diff half of D4.2: compare two ``l0_points`` dumps -- one
    regenerated under a historical ``engine_build`` E, one regenerated
    in-process under HEAD -- keyed on ``(commit_sha, health_major,
    source)``, deliberately NOT the table's real primary key (which also
    includes ``replay_decl_hash``). A ``MeasurementDef.replay`` flip
    between E and HEAD changes ``replay_decl_hash`` without changing
    ``(commit_sha, health_major, source)``, and keying on the real primary
    key would then see two ROWS THAT NEVER MEET -- one only in ``e_rows``,
    one only in ``head_rows`` -- reported as an unrelated row-added/
    row-removed pair instead of what it actually is: the SAME logical
    point, recomputed, whose declared-replay fingerprint moved. When
    ``replay_decl_hash`` differs, this function reports EXACTLY ONE diff
    entry for that key, of ``"kind": "declaration"``, naming both hashes,
    and does not also diff the two rows' other fields (a hash mismatch
    already explains why every ``unreplayable``-shaped field could differ)."""

    def key(row: dict) -> tuple:
        return (row["commit_sha"], row["health_major"], row["source"])

    e_by_key = {key(r): r for r in e_rows}
    head_by_key = {key(r): r for r in head_rows}

    diffs: "list[dict]" = []
    all_keys = sorted(set(e_by_key) | set(head_by_key), key=lambda k: [str(x) for x in k])
    for k in all_keys:
        e_row = e_by_key.get(k)
        head_row = head_by_key.get(k)
        if e_row is None:
            diffs.append({"table": "l0_points", "key": list(k), "field": "<row>", "old": None, "new": "present"})
            continue
        if head_row is None:
            diffs.append({"table": "l0_points", "key": list(k), "field": "<row>", "old": "present", "new": None})
            continue
        if e_row["replay_decl_hash"] != head_row["replay_decl_hash"]:
            diffs.append(
                {
                    "table": "l0_points",
                    "key": list(k),
                    "field": "replay_decl_hash",
                    "kind": "declaration",
                    "old": e_row["replay_decl_hash"],
                    "new": head_row["replay_decl_hash"],
                }
            )
            continue
        fields = sorted(set(e_row) | set(head_row))
        for field in fields:
            if field in _BUILD_DIFF_EXCLUDE_COLUMNS:
                continue
            if field in ("commit_sha", "health_major", "source", "replay_decl_hash"):
                continue
            if e_row.get(field) != head_row.get(field):
                diffs.append(
                    {
                        "table": "l0_points",
                        "key": list(k),
                        "field": field,
                        "old": e_row.get(field),
                        "new": head_row.get(field),
                    }
                )
    return diffs


def _record_rebuild_run(
    db_path: "str | Path", *, health_major: "int | None", engine_build: "str | None", detail: dict
) -> None:
    """Write one ``replay_runs`` row with ``reason='rebuild'`` recording
    this ``rebuild`` call's verdict, so ``status`` can report "the last
    rebuild's verdict" (deliverable 1's CLI contract) without re-running
    anything. Written into ``db_path`` AFTER any ``os.replace`` the rebuild
    itself performed, so the row lands in whichever file is live afterward."""
    conn = open_store(db_path)
    try:
        now_iso = _utc_now_iso()
        conn.execute(
            "INSERT INTO replay_runs (started_at, finished_at, health_major, reason, "
            "commits_total, commits_done, skipped_old_format, unresolved, engine_build, detail) "
            "VALUES (?, ?, ?, 'rebuild', ?, ?, ?, ?, ?, ?)",
            (now_iso, now_iso, health_major, None, None, None, None, engine_build, _canonical_json(detail)),
        )
        conn.commit()
    finally:
        conn.close()


def _two_diff_rebuild(
    snapshots_dir: "str | Path",
    repo_dir: "str | Path",
    db_path: "str | Path",
    *,
    alpha_repo: "str | Path | None" = None,
    engine_build: "str | None" = None,
    regenerate_under_build=None,
    keep_scratch: bool = False,
) -> dict:
    """The D4.2 two-diff form -- see ``rebuild``'s own docstring for the
    full contract; this function is ``rebuild``'s default (``single_diff=
    False``) dispatch target."""
    import os

    db_path = Path(db_path)
    effective_alpha_repo = Path(alpha_repo) if alpha_repo is not None else _default_repo_dir()

    # -- R_head: regenerate under HEAD, in-process, as today -- ingest THEN
    # backfill (using whatever registry health_measurements.MEASUREMENTS
    # currently is -- no override -- so an in-process monkeypatch of a
    # compute_fn or a `replay` declaration is picked up exactly like a
    # real code change would be).
    head_db = Path(str(db_path) + ".rebuild")
    head_db.unlink(missing_ok=True)
    for suffix in ("-journal", "-wal", "-shm"):
        Path(str(head_db) + suffix).unlink(missing_ok=True)

    ingest(snapshots_dir, repo_dir, head_db, engine_build=engine_build)
    backfill(repo_dir, head_db)
    head_conn = open_store(head_db)
    try:
        head_rows = _dump_table_rows(head_conn, "l0_points")
        head_health_majors = sorted({r["health_major"] for r in head_rows})
    finally:
        head_conn.close()
    head_build_id = engine_build if engine_build is not None else detect_engine_build()

    alpha_is_worktree = _is_git_worktree(effective_alpha_repo)

    regen = regenerate_under_build
    if regen is None:
        regen = functools.partial(
            _default_regenerate_under_build,
            effective_alpha_repo,
            keep_scratch=keep_scratch,
        )

    builds_meta: "list[dict]" = []
    content_differences: "dict[str, object]" = {}
    build_differences: "dict[str, object]" = {}
    build_confounded = False

    live_exists = db_path.exists()
    live_rows_all: "list[dict]" = []
    if live_exists:
        live_conn = open_store(db_path)
        try:
            live_rows_all = _dump_table_rows(live_conn, "l0_points")
        finally:
            live_conn.close()

    distinct_builds = []
    unknown_rows: "list[dict]" = []
    unknown_raw_values: "set[str]" = set()
    if live_rows_all:
        # Group builds directly against an in-memory dump rather than
        # re-opening db_path a second time (skip unknown/non-sha
        # engine_build, strip a `-dirty` suffix but record its presence).
        # Every row whose `engine_build` is NOT checkout-able (`'unknown'`,
        # or anything failing `_SHA_RE` even after stripping a `-dirty`
        # suffix) is routed into `unknown_rows` rather than silently
        # dropped -- such a row can never land in ANY bucket otherwise,
        # which means it can never make `identical` False and the live
        # store gets replaced without ever having been checked (the D4.2
        # gap Ariadne's #2605 colleague review found).
        grouped: "dict[str, list]" = {}
        for r in live_rows_all:
            raw = r["engine_build"]
            if raw == "unknown":
                unknown_rows.append(r)
                unknown_raw_values.add(raw)
                continue
            stripped, dirty = _strip_dirty_suffix(raw)
            if not _SHA_RE.match(stripped):
                unknown_rows.append(r)
                unknown_raw_values.add(raw)
                continue
            entry = grouped.setdefault(stripped, [False, 0])
            entry[0] = entry[0] or dirty
            entry[1] += 1
        distinct_builds = [(sha, dirty, count) for sha, (dirty, count) in sorted(grouped.items())]

    if unknown_rows:
        # Route into the SAME unavailable/confounded branch as an
        # unreachable sha build below (D4.2's own stated rule: a confounded
        # verdict never replaces) -- `'unknown'` is a checkout-able-build
        # question, not a data question, so it belongs in `build_
        # differences`/`content_differences` exactly like any other build
        # this rebuild cannot resolve, never in neither.
        builds_meta.append(
            {
                "engine_build": "unknown",
                "rows": len(unknown_rows),
                "status": "unavailable",
                "raw_values": sorted(unknown_raw_values),
            }
        )
        build_differences["unknown"] = {
            "status": "unavailable",
            "reason": "engine_build is not a checkout-able sha",
        }
        content_differences["unknown"] = {
            "confounded": True,
            "differences": _compare_content_diff(unknown_rows, head_rows),
        }
        build_confounded = True

    for stripped_sha, dirty_seen, row_count in distinct_builds:
        stored_under_e = [
            r for r in live_rows_all if _strip_dirty_suffix(r["engine_build"])[0] == stripped_sha
        ]

        if not alpha_is_worktree:
            builds_meta.append(
                {"engine_build": stripped_sha, "dirty_suffix": dirty_seen, "rows": row_count, "status": "unavailable"}
            )
            build_differences[stripped_sha] = {
                "status": "unavailable",
                "reason": f"alpha_repo {str(effective_alpha_repo)!r} is not a git work tree",
            }
            content_differences[stripped_sha] = {
                "confounded": True,
                "differences": _compare_content_diff(stored_under_e, head_rows),
            }
            build_confounded = True
            continue

        scratch_root = Path(tempfile.mkdtemp(prefix="health-series-rE-"))
        e_db = scratch_root / "series.db"
        try:
            regen(stripped_sha, snapshots_dir, repo_dir, e_db)
        except (_RegenerateUnavailable, subprocess.CalledProcessError) as exc:
            builds_meta.append(
                {"engine_build": stripped_sha, "dirty_suffix": dirty_seen, "rows": row_count, "status": "unavailable"}
            )
            build_differences[stripped_sha] = {
                "status": "unavailable",
                "reason": _regenerate_failure_reason(exc),
            }
            content_differences[stripped_sha] = {
                "confounded": True,
                "differences": _compare_content_diff(stored_under_e, head_rows),
            }
            build_confounded = True
            continue
        else:
            builds_meta.append(
                {"engine_build": stripped_sha, "dirty_suffix": dirty_seen, "rows": row_count, "status": "ok"}
            )
            e_conn = open_store(e_db)
            try:
                e_rows = _dump_table_rows(e_conn, "l0_points")
            finally:
                e_conn.close()

            content_differences[stripped_sha] = _compare_content_diff(stored_under_e, e_rows)
            build_differences[stripped_sha] = _compare_build_diff(e_rows, head_rows)
        finally:
            shutil.rmtree(str(scratch_root), ignore_errors=True)

    def _diff_nonempty(value: object) -> bool:
        if isinstance(value, dict):
            return bool(value.get("differences"))
        return bool(value)

    # `os.replace` below swaps the WHOLE db file, but the diffs above only
    # ever look at `l0_points`. Before replacing, also compare `declarations`
    # and `unresolved_snapshots` between the live store and R_head (the same
    # tables/exclusions `_rebuild_single_diff` already uses via
    # `_REBUILD_DIFF_TABLES`/`_compare_dumps`) -- any difference there is a
    # reason to refuse the replace too, reported under its own key rather
    # than silently overwritten.
    other_table_differences: "dict[str, list]" = {}
    if live_exists:
        live_conn = open_store(db_path)
        try:
            head_conn2 = open_store(head_db)
            try:
                for table in ("declarations", "unresolved_snapshots"):
                    old_rows = _dump_table_rows(live_conn, table)
                    new_rows = _dump_table_rows(head_conn2, table)
                    table_diffs = _compare_dumps(old_rows, new_rows, table)
                    if table_diffs:
                        other_table_differences[table] = table_diffs
            finally:
                head_conn2.close()
        finally:
            live_conn.close()

    any_content_diff = any(_diff_nonempty(v) for v in content_differences.values())
    any_build_diff = any(_diff_nonempty(v) for v in build_differences.values())
    identical = (
        (not build_confounded)
        and (not any_content_diff)
        and (not any_build_diff)
        and (not other_table_differences)
    )

    if identical:
        os.replace(str(head_db), str(db_path))

    if identical:
        advice = (
            "content and build diffs are empty for every build; the live store "
            "was replaced with a fresh regenerate under HEAD."
        )
    elif build_confounded:
        confounded_builds = sorted(
            key
            for key, value in build_differences.items()
            if isinstance(value, dict) and value.get("status") == "unavailable"
        )
        advice = (
            "the following build(s) could not be checked, so content and "
            "build cannot be separated for their rows (build_confounded="
            "true): " + ", ".join(confounded_builds) + " -- see build_"
            "differences[<engine_build>].reason for why. the live store "
            "was NOT replaced."
        )
    elif other_table_differences and (any_content_diff or any_build_diff):
        advice = (
            "differences were found in " + ", ".join(sorted(other_table_differences))
            + " (see other_table_differences), in addition to l0_points "
            "content/build differences; the live store was NOT replaced."
        )
    elif other_table_differences:
        advice = (
            "differences were found in "
            + ", ".join(sorted(other_table_differences))
            + " between the live store and the fresh regenerate under HEAD "
            "(see other_table_differences) -- the live store was NOT "
            "replaced, even though l0_points itself agreed."
        )
    elif any_content_diff and any_build_diff:
        advice = (
            "both content and build differences were found; the live store was "
            "NOT replaced. Content differences are a data problem to investigate "
            "(snapshot files, knowledge.sql history, or anchor resolution moved). "
            "Build differences mean the code changed -- rebuild never re-derives "
            "history for a code change; run backfill to gap-fill under the new "
            "declaration hash / MAJOR."
        )
    elif any_content_diff:
        advice = (
            "content differences were found under unchanged code; the live "
            "store was NOT replaced -- a data problem to investigate (snapshot "
            "files, knowledge.sql history, or anchor resolution moved)."
        )
    else:
        advice = (
            "build differences were found under unchanged data; the live store "
            "was NOT replaced -- rebuild is not the path by which a code change "
            "re-derives history; run backfill to gap-fill the affected rows "
            "under the new declaration hash / MAJOR."
        )

    result = {
        "identical": identical,
        "content_differences": content_differences,
        "build_differences": build_differences,
        "builds": builds_meta,
        "build_confounded": build_confounded,
        "other_table_differences": other_table_differences,
        "advice": advice,
        "rebuilt_path": str(head_db),
    }

    _record_rebuild_run(
        db_path,
        health_major=(head_health_majors[0] if len(head_health_majors) == 1 else None),
        engine_build=head_build_id,
        detail={
            "identical": identical,
            "builds": builds_meta,
            "build_confounded": build_confounded,
            "advice": advice,
        },
    )

    return result


def rebuild(
    snapshots_dir: str | Path,
    repo_dir: str | Path,
    db_path: str | Path,
    *,
    alpha_repo: "str | Path | None" = None,
    engine_build: str | None = None,
    regenerate_under_build=None,
    single_diff: bool = False,
    keep_scratch: bool = False,
) -> dict:
    """Rebuild the series store from scratch and diff it against the live
    one (design doc section 2's "rebuild", extended by D4.2). TWO diffs by
    default, because ``l0_points.engine_build`` records SOME build ``E``
    that computed the stored rows, and regenerating only under HEAD
    (today's behaviour before D4.2) cannot tell a code change (``E`` !=
    HEAD) apart from a data change (the snapshot files or ``knowledge.sql``
    history moved) -- both would show up as "the regenerate disagrees with
    the live store."

    **Default (two-diff) behaviour:**

    - Regenerate under HEAD, in-process, exactly as the old single-diff
      form did (plus a ``backfill`` pass, needed for the build side) ->
      ``R_head``.
    - For each distinct ``engine_build`` ``E`` recorded on the live store's
      ``l0_points`` rows (``'unknown'`` and non-sha values skipped; a
      ``-dirty`` suffix stripped but its presence recorded per build, via
      the inline bucketing logic above that groups live rows by stripped
      sha): regenerate under ``E`` via
      ``regenerate_under_build(E, snapshots_dir, repo_dir, out_db)`` (the
      injectable hook; default: ``_default_regenerate_under_build``, which
      archives ``E``'s ``src/engram`` out of ``alpha_repo`` and runs it as a
      SUBPROCESS -- see that function's own docstring) -> ``R_E``.
    - **Content diff** (``content_differences[E]``): ``_compare_dumps``
      between the LIVE store's own rows under ``E`` and ``R_E`` -- same
      code (``E``) computed both sides, so any difference is a DATA change.
    - **Build diff** (``build_differences[E]``): a dedicated comparison
      (``_compare_build_diff``) between ``R_E`` and ``R_head`` keyed on
      ``(commit_sha, health_major, source)`` (NOT the real primary key,
      which also includes ``replay_decl_hash``) -- same inputs (both
      regenerated against today's ``snapshots_dir``/``repo_dir``), so any
      difference is a CODE change. A ``replay_decl_hash`` mismatch between
      the two is reported as its own diff kind, ``"declaration"``, naming
      both hashes, rather than as two unrelated rows (see that function's
      docstring for why).
    - **Replacement policy**: the live store is replaced (``os.replace``)
      with the fresh ``R_head`` ONLY when every ``content_differences[E]``
      and every ``build_differences[E]`` is empty for EVERY ``E`` (and no
      build was ``unavailable``, see below). A content-only difference is a
      data problem -- returned, never replaced. A build-only difference
      means the code changed -- also never replaced: ``rebuild`` is not the
      path by which a code change re-derives history (that is ``backfill``'s
      regular gap-fill mechanism, under the new declaration hash / MAJOR).
      See the returned ``advice`` string for which case fired.
    - **Unavailable build side**: when ``E`` cannot be archived/replayed
      (``alpha_repo`` is not a git work tree; ``E`` is not a commit there;
      the scratch tree's subprocess run fails, e.g. a pre-B1 tree with no
      ``backfill`` subcommand) -- ``build_differences[E]`` is ``{"status":
      "unavailable", "reason": ...}``, ``content_differences[E]`` is
      computed against ``R_head`` INSTEAD of the unreachable ``R_E``
      (``{"confounded": True, "differences": [...]}``), and the top-level
      ``build_confounded`` is set ``True`` -- content and build cannot be
      told apart for those rows, and the live store is never replaced on a
      confounded verdict. This never raises -- an unreachable build is
      always reported, never an exception.
    - **Rows whose ``engine_build`` is not checkout-able** (``'unknown'``,
      or anything failing ``_SHA_RE`` even after stripping a ``-dirty``
      suffix) are grouped under the key ``"unknown"`` and routed into this
      SAME unavailable/confounded branch -- ``build_differences["unknown"]``
      is ``{"status": "unavailable", "reason": "engine_build is not a
      checkout-able sha"}``, ``content_differences["unknown"]`` is
      confounded against ``R_head``, ``build_confounded`` is set, and
      ``builds`` gains a ``{"engine_build": "unknown", "rows": N, "status":
      "unavailable", "raw_values": [...]}`` entry (``raw_values`` lists the
      distinct raw strings folded into this bucket). Never dropped, and
      never left in no bucket at all -- an unchecked build must never be
      able to make ``identical`` True.

    ``alpha_repo`` defaults to ``_default_repo_dir()`` (this module's own
    checkout). If ``alpha_repo`` itself is not a git work tree, EVERY
    build's side is ``unavailable``/confounded (never an exception).

    **Replacement also checks ``declarations``/``unresolved_snapshots``**:
    ``os.replace`` swaps the whole db file, but the diffs above only ever
    compare ``l0_points``. Before replacing, the live store's
    ``declarations`` and ``unresolved_snapshots`` tables are also compared
    against ``R_head`` (via ``_compare_dumps``, same exclusions as
    ``_rebuild_single_diff``'s ``_REBUILD_DIFF_TABLES``) -- any difference
    there is reported under ``other_table_differences`` and refuses the
    replace, even when every ``l0_points``-derived diff above is empty.

    A ``replay_runs`` row with ``reason='rebuild'`` is written on every
    call (single-diff or two-diff), so ``status`` can report the last
    rebuild's verdict without re-running anything.

    **``derived_points`` (PR C1) is deliberately NOT part of either rebuild
    diff** -- it is a pure function of ``l0_points`` + ``declarations``
    (both already diffed above), never a second source of truth, so after a
    rebuild that replaces the store the next ``derive`` call simply
    re-fills it from whatever ``l0_points``/``declarations`` rebuild left
    behind.

    Returns ``{identical, content_differences, build_differences, builds:
    [{engine_build, dirty_suffix, rows, status}], build_confounded,
    other_table_differences, advice, rebuilt_path}`` in the default
    (two-diff) mode.

    **``single_diff=True`` (CLI: ``--single-diff``)**: explicit opt-BACK
    into the PRE-D4.2 behaviour (``_rebuild_single_diff`` -- regenerate
    once under HEAD, one diff, ``{identical, differences, rebuilt_path}``)
    for callers that explicitly accept content and build being confounded.
    Never the default."""
    if single_diff:
        result = _rebuild_single_diff(snapshots_dir, repo_dir, db_path, engine_build=engine_build)
        _record_rebuild_run(
            db_path,
            health_major=None,
            engine_build=engine_build if engine_build is not None else detect_engine_build(),
            detail={"single_diff": True, "identical": result["identical"]},
        )
        return result
    return _two_diff_rebuild(
        snapshots_dir,
        repo_dir,
        db_path,
        alpha_repo=alpha_repo,
        engine_build=engine_build,
        regenerate_under_build=regenerate_under_build,
        keep_scratch=keep_scratch,
    )


# ---------------------------------------------------------------------------
# Replay declarations helper (D2)
# ---------------------------------------------------------------------------


def _replay_decl_hash_for(registry: "dict[str, object]") -> str:
    """SHA-256 hex digest over the sorted ``(name, replay)`` pairs of
    ``registry`` -- the SAME algorithm as ``health.measurements.
    replay_decl_hash()`` (sorted pairs, canonical JSON via this module's own
    ``_canonical_json``, sha256 hex), but parameterized over an arbitrary
    ``registry`` mapping rather than always reading the module-global
    ``MEASUREMENTS``.

    WHY THIS DUPLICATES THE ALGORITHM INSTEAD OF CALLING ``health.
    measurements.replay_decl_hash()`` DIRECTLY: that function's signature
    (spec'd in ``measurements.py``) takes no arguments -- it always hashes
    the real, module-level ``MEASUREMENTS`` registry. But ``backfill``/
    ``verify`` (below) accept an *injectable* ``registry`` precisely so
    tests can exercise a ``MeasurementDef.replay`` flip (D2's own scenario:
    ``stripped -> graph``) on a COPY of the registry, without mutating the
    real module global that every other test in the process shares. Calling
    the zero-arg ``replay_decl_hash()`` from inside this module would
    always hash the real registry regardless of what ``registry`` a test
    passed in, silently defeating the injection. When ``registry`` IS the
    real ``health_measurements.MEASUREMENTS`` (the default, production
    path), this function's output is byte-identical to ``health.
    measurements.replay_decl_hash()``'s -- same sort, same canonical JSON
    encoding, same hash -- by construction, since both implement the exact
    same algorithm over the exact same data."""
    import hashlib

    pairs = sorted((name, mdef.replay) for name, mdef in registry.items())
    blob = _canonical_json(pairs).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _current_health_major() -> int:
    """``health.version.MAJOR`` -- the default ``health_major`` for
    ``backfill``/``verify`` and the ``raw_schema_version`` stamp
    ``replay_commit`` writes into every replayed measurements block (see
    that function's own docstring for why this module reads the real
    running code's MAJOR rather than accepting an injected ``health_major``
    argument -- the exact spec'd ``replay_commit(repo_dir, commit_sha,
    registry=None)`` signature has no such argument)."""
    return health_version.MAJOR


# ---------------------------------------------------------------------------
# git commit message plumbing (turn parsing, D2/§5)
# ---------------------------------------------------------------------------

_TURN_TRANSITION_RE = re.compile(r"turn (\d+)->(\d+)")


def _commit_message(repo_dir: str | Path, commit_sha: str) -> str:
    """The full commit message (subject + body) for ``commit_sha``, via
    ``git log -1 --format=%B``."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%B", commit_sha],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    return result.stdout


def parse_turn_from_message(message: str) -> "int | None":
    """The POST-transition turn number from a commit message stating
    ``turn (\\d+)->(\\d+)`` (the sleep commits' own shape, e.g. ``"[session]
    Dream 2026-09-04 (turn 54->55)"`` -> ``55``), or ``None`` if the message
    states no such transition (nap commits never do -- design doc §5: "turn
    ... populated for replay rows only when the commit message states it
    ... otherwise NULL, never guessed"). Deliberately the SECOND number, not
    the first -- a turn transition names the turn the checkpoint LANDED IN,
    which is what a point on the series timeline should carry."""
    m = _TURN_TRANSITION_RE.search(message)
    if not m:
        return None
    return int(m.group(2))


# ---------------------------------------------------------------------------
# replay_commit (PR B1's replay driver, D2/§3)
# ---------------------------------------------------------------------------


def replay_commit(
    repo_dir: str | Path,
    commit_sha: str,
    registry: "dict[str, object] | None" = None,
) -> "tuple[dict, dict]":
    """Replay ONE historical ``knowledge.sql`` commit against every
    registered ``MeasurementDef`` in ``registry`` (default ``health.
    measurements.MEASUREMENTS``), honoring each def's declared ``replay``
    behaviour (see that field's own docstring for the three values).

    Mechanism: ``git show <commit_sha>:knowledge.sql`` (a ``subprocess.run``
    ARGUMENT LIST -- never ``shell=True``, never a piped string) reads the
    dump's text straight from git's object store, without ever checking out
    a working tree or touching the live ``~/.engram/knowledge.db`` (design
    doc §4: "it never opens the live knowledge.db, never writes a temp DB,
    never touches the WAL"); ``sqlite3.connect(':memory:')`` +
    ``executescript`` restores it into an in-memory database that is
    discarded when this function returns.

    Per measurement:

    - ``replay == "graph"`` -- call ``compute_fn(conn)`` against the
      restored dump. On ``sqlite3.OperationalError`` (the dump predates a
      table/column this measurement's SQL needs -- e.g. every commit before
      ``cascade_markers`` existed) the value is ``None`` and ``unreplayable
      [name] = {"reason": f"schema:{e}", "class": "discovered"}`` -- a fact
      learned only by trying against THIS SPECIFIC commit, never
      predictable from the declaration alone. Any OTHER exception
      propagates uncaught (this module's own posture, matching ``write_
      snapshot``'s: a bug in a measurement's own SQL must be loud, never
      swallowed alongside the one exception class that is a genuinely
      expected, commit-dependent fact about history).
    - ``replay == "stripped"`` -- ``compute_fn`` is NEVER called (see
      ``test_declared_unreplayable_never_calls_compute_fn``, which proves
      this by making the def's ``compute_fn`` raise and asserting replay
      still succeeds). ``None``, reason ``"stripped_from_dump"``, class
      ``"declared"``.
    - ``replay == "environment"`` -- ``compute_fn`` is NEVER called, same
      proof. ``None``, reason ``"environment_fact"``, class ``"declared"``.

    Returns ``(measurements, unreplayable)`` where ``measurements`` is a
    JSON-safe ``{name: value}`` dict that ALSO carries a ``raw_schema_
    version`` key (stamped from ``health.version.MAJOR`` -- see
    ``_current_health_major``'s own docstring for why this module reads the
    real running MAJOR rather than accepting an injected argument), so its
    key set matches what ``health.measurements.compute_all_measurements``
    puts in a ``stored`` row's ``measurements`` block. ``unreplayable`` is
    ``{name: {"reason": ..., "class": "declared"|"discovered"}}`` for every
    measurement NOT computed this way (D2's class shape)."""
    registry = registry if registry is not None else health_measurements.MEASUREMENTS

    result = subprocess.run(
        ["git", "show", f"{commit_sha}:knowledge.sql"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(result.stdout)

        measurements: "dict[str, object]" = {
            "raw_schema_version": _current_health_major()
        }
        unreplayable: "dict[str, dict]" = {}

        for name, mdef in registry.items():
            if mdef.replay == "graph":
                try:
                    value = mdef.compute_fn(conn)
                except sqlite3.OperationalError as e:
                    measurements[name] = None
                    unreplayable[name] = {"reason": f"schema:{e}", "class": "discovered"}
                    continue
                if isinstance(value, tuple):
                    value = list(value)
                measurements[name] = value
            elif mdef.replay == "stripped":
                measurements[name] = None
                unreplayable[name] = {"reason": "stripped_from_dump", "class": "declared"}
            elif mdef.replay == "environment":
                measurements[name] = None
                unreplayable[name] = {"reason": "environment_fact", "class": "declared"}
            else:  # pragma: no cover -- defensive; MeasurementDef.replay is a closed set
                raise ValueError(
                    f"measurement {name!r} declares unknown replay value {mdef.replay!r}"
                )
    finally:
        conn.close()

    return measurements, unreplayable


# ---------------------------------------------------------------------------
# equality_check -- the equality control (D3)
# ---------------------------------------------------------------------------


def equality_check(
    stored_measurements: dict,
    replay_measurements: dict,
    replay_unreplayable: dict,
    registry: "dict[str, object] | None" = None,
) -> "tuple[str, dict]":
    """Compare a ``stored`` row's ``measurements`` block against a
    ``replay_commit`` result for the SAME commit -- the design's built-in
    equality control (§1, D3).

    Compares ONLY the names both readings can agree are comparable: a
    measurement whose declared ``replay == "graph"`` in ``registry``
    (default ``health.measurements.MEASUREMENTS``) AND that is NOT a key of
    ``replay_unreplayable`` (a ``graph`` measurement can still land there,
    per-commit, on a ``schema:`` discovered failure). ``raw_schema_version``
    is excluded from the comparison unconditionally (it is not a
    ``MeasurementDef`` at all, and comparing it would fail the control on
    every commit whose stored row was written under a different running
    MAJOR than today's replay -- a fact about WHEN each side ran, not about
    whether the graph state agrees).

    Returns ``("agree", {})`` when every comparable name matches, or
    ``("disagree", {"names": [...], "stored": {name: v}, "replay": {name:
    v}})`` on the first-and-every disagreement -- D3's tightened
    requirement: the diff NAMES every differing measurement and nothing
    else, so a control failure is immediately attributable rather than a
    bare "something disagreed" signal."""
    registry = registry if registry is not None else health_measurements.MEASUREMENTS

    comparable_names = sorted(
        name
        for name, mdef in registry.items()
        if mdef.replay == "graph" and name not in replay_unreplayable
    )

    differing_names: "list[str]" = []
    stored_diff: "dict[str, object]" = {}
    replay_diff: "dict[str, object]" = {}
    for name in comparable_names:
        stored_value = stored_measurements.get(name)
        replay_value = replay_measurements.get(name)
        if stored_value != replay_value:
            differing_names.append(name)
            stored_diff[name] = stored_value
            replay_diff[name] = replay_value

    if not differing_names:
        return ("agree", {})
    return (
        "disagree",
        {"names": differing_names, "stored": stored_diff, "replay": replay_diff},
    )


# ---------------------------------------------------------------------------
# backfill -- the regular mechanism (§4, D2)
# ---------------------------------------------------------------------------


def backfill(
    repo_dir: str | Path,
    db_path: str | Path,
    health_major: "int | None" = None,
    engine_build: "str | None" = None,
    now: "datetime | None" = None,
    registry: "dict[str, object] | None" = None,
) -> dict:
    """The regular git-replay backfill mechanism (design doc §4): for the
    given ``health_major`` (default ``health.version.MAJOR``) and the
    CURRENT declared-replay fingerprint (``_replay_decl_hash_for(registry)``,
    D2), replay every ``knowledge.sql`` commit that has no ``git_replay``
    row yet under ``(health_major, replay_decl_hash)``, insert it, then run
    the equality control (``equality_check``) for every commit that now has
    BOTH a ``stored`` row and a ``git_replay`` row under this ``(health_
    major, replay_decl_hash)`` pair and has not already been checked under
    it.

    ``reason`` (recorded in the returned run record and in ``replay_runs``):

    - ``"initial"`` -- the store has ZERO ``git_replay`` rows at all (first
      run on a fresh store).
    - ``"major_bump"`` -- the store HAS ``git_replay`` rows, but none under
      THIS ``health_major`` (a MAJOR version bump, or -- in a test double --
      a caller passing a different ``health_major`` explicitly).
    - ``"gap_fill"`` -- neither of the above: some commits are simply
      missing under the current ``(health_major, replay_decl_hash)`` --
      this is the SAME path for "new commits landed since the last run" AND
      "a ``MeasurementDef.replay`` declaration flipped, changing the
      fingerprint" (D2's whole point: no special-cased mode for a
      declaration flip, it is gap-fill through the regular mechanism).

    Never mutates an existing ``l0_points`` row (the never-mutate rule) --
    every insert is for a ``(commit_sha, health_major, 'git_replay',
    replay_decl_hash)`` key that had no existing row.

    Returns the run record dict (``reason``, ``started_at``, ``finished_
    at``, ``health_major``, ``commits_total`` (missing-commit count),
    ``commits_done`` (rows actually inserted -- always equal to
    ``commits_total`` today, since every missing commit is unconditionally
    replayed and inserted), ``engine_build``, ``detail`` (``{per_reason_
    counts_of_unreplayable, decl_hash, disagreements}``)); the same record
    (JSON-encoded ``detail``) is inserted into ``replay_runs``."""
    registry = registry if registry is not None else health_measurements.MEASUREMENTS

    conn = open_store(db_path)
    try:
        started_at = _utc_now_iso(now)
        effective_health_major = (
            health_major if health_major is not None else _current_health_major()
        )
        if engine_build is not None:
            build = engine_build
        else:
            build = detect_engine_build()
            build = _guard_engine_build(
                build,
                data_repo=repo_dir,
                # #2620: no fallback to _default_repo_dir() -- a None here
                # means we're in a deployed (data-repo) tree, and falling
                # back to the undiscriminated resolver would hand the
                # guard the data repo as code_repo, disarming it on the
                # exact input it exists to catch. None propagates.
                code_repo=_find_code_repo_toplevel(Path(__file__).resolve().parent),
            )
        current_hash = _replay_decl_hash_for(registry)

        total_git_replay_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM l0_points WHERE source = 'git_replay'"
        ).fetchone()["c"]
        if total_git_replay_rows == 0:
            reason = "initial"
        else:
            under_major = conn.execute(
                "SELECT COUNT(*) AS c FROM l0_points WHERE source = 'git_replay' "
                "AND health_major = ?",
                (effective_health_major,),
            ).fetchone()["c"]
            reason = "major_bump" if under_major == 0 else "gap_fill"

        existing_shas = {
            r["commit_sha"]
            for r in conn.execute(
                "SELECT commit_sha FROM l0_points WHERE health_major = ? "
                "AND source = 'git_replay' AND replay_decl_hash = ?",
                (effective_health_major, current_hash),
            ).fetchall()
        }
        all_commits = knowledge_commits(repo_dir)
        missing = [(sha, ts) for sha, ts in all_commits if sha not in existing_shas]

        commits_done = 0
        per_reason_counts: "dict[str, int]" = {}
        for sha, committer_iso in missing:
            measurements, unreplayable = replay_commit(repo_dir, sha, registry=registry)
            turn = parse_turn_from_message(_commit_message(repo_dir, sha))
            computed_at = _utc_now_iso(now)
            conn.execute(
                "INSERT INTO l0_points "
                "(commit_sha, health_major, commit_time, turn, source, "
                "replay_decl_hash, snapshot_file, anchor_pre_commit, "
                "anchor_resolution, computed_at, engine_build, measurements, "
                "unreplayable) "
                "VALUES (?, ?, ?, ?, 'git_replay', ?, NULL, NULL, NULL, ?, ?, ?, ?)",
                (
                    sha,
                    effective_health_major,
                    committer_iso,
                    turn,
                    current_hash,
                    computed_at,
                    build,
                    _canonical_json(measurements),
                    _canonical_json(unreplayable),
                ),
            )
            conn.commit()
            commits_done += 1
            for info in unreplayable.values():
                r = info["reason"]
                per_reason_counts[r] = per_reason_counts.get(r, 0) + 1

        disagreements: "list[dict]" = []
        candidates = conn.execute(
            "SELECT s.commit_sha AS commit_sha, s.measurements AS stored_measurements, "
            "r.measurements AS replay_measurements, r.unreplayable AS replay_unreplayable "
            "FROM l0_points s "
            "JOIN l0_points r ON r.commit_sha = s.commit_sha "
            "AND r.health_major = s.health_major "
            "WHERE s.source = 'stored' AND s.health_major = ? "
            "AND r.source = 'git_replay' AND r.replay_decl_hash = ?",
            (effective_health_major, current_hash),
        ).fetchall()
        for row in candidates:
            already_checked = conn.execute(
                "SELECT 1 FROM equality_checks WHERE commit_sha = ? AND health_major = ? "
                "AND replay_decl_hash = ?",
                (row["commit_sha"], effective_health_major, current_hash),
            ).fetchone()
            if already_checked is not None:
                continue
            verdict, diff = equality_check(
                json.loads(row["stored_measurements"]),
                json.loads(row["replay_measurements"]),
                json.loads(row["replay_unreplayable"]),
                registry,
            )
            conn.execute(
                "INSERT INTO equality_checks "
                "(commit_sha, health_major, verdict, diff, checked_at, replay_decl_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["commit_sha"],
                    effective_health_major,
                    verdict,
                    _canonical_json(diff),
                    _utc_now_iso(now),
                    current_hash,
                ),
            )
            conn.commit()
            if verdict == "disagree":
                disagreements.append({"commit_sha": row["commit_sha"], **diff})

        finished_at = _utc_now_iso(now)
        detail = {
            "per_reason_counts_of_unreplayable": per_reason_counts,
            "decl_hash": current_hash,
            "disagreements": disagreements,
        }
        run_record = {
            "reason": reason,
            "started_at": started_at,
            "finished_at": finished_at,
            "health_major": effective_health_major,
            "commits_total": len(missing),
            "commits_done": commits_done,
            "engine_build": build,
            "detail": detail,
        }
        conn.execute(
            "INSERT INTO replay_runs "
            "(started_at, finished_at, health_major, reason, commits_total, "
            "commits_done, skipped_old_format, unresolved, engine_build, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                started_at,
                finished_at,
                effective_health_major,
                reason,
                len(missing),
                commits_done,
                None,
                None,
                build,
                _canonical_json(detail),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return run_record


# ---------------------------------------------------------------------------
# verify -- re-run the instrument now, record what it found (§4)
# ---------------------------------------------------------------------------


def verify(
    repo_dir: str | Path,
    db_path: str | Path,
    health_major: "int | None" = None,
    registry: "dict[str, object] | None" = None,
    now: "datetime | None" = None,
) -> dict:
    """Re-run the equality control RIGHT NOW against every ``stored`` row
    under ``health_major`` (default ``health.version.MAJOR``) -- deliberately
    NOT reusing any existing ``git_replay`` row (``backfill`` populates and
    reuses those; ``verify`` exists to re-run the instrument itself, per the
    design doc's own framing: "any disagree is surfaced as a finding ...
    all three [dump, measurement, or anchor resolution] are things we want
    to know the day they break").

    Appends an ``equality_checks`` row for every commit checked (append-only
    -- see that table's own migration docstring; a second ``verify`` run
    adds a SECOND row per commit rather than overwriting the first, and
    readers take the latest by ``checked_at``).

    Returns ``{"checked": n, "agree": a, "disagree": d, "disagreements":
    [{commit_sha, names, stored, replay}, ...]}``, and writes a
    ``replay_runs`` row with ``reason='verify'``."""
    registry = registry if registry is not None else health_measurements.MEASUREMENTS

    conn = open_store(db_path)
    try:
        started_at = _utc_now_iso(now)
        effective_health_major = (
            health_major if health_major is not None else _current_health_major()
        )
        current_hash = _replay_decl_hash_for(registry)
        build = detect_engine_build()
        build = _guard_engine_build(
            build,
            data_repo=repo_dir,
            # #2620: no fallback to _default_repo_dir() -- a None here
            # means we're in a deployed (data-repo) tree, and falling back
            # to the undiscriminated resolver would hand the guard the
            # data repo as code_repo, disarming it on the exact input it
            # exists to catch. None propagates.
            code_repo=_find_code_repo_toplevel(Path(__file__).resolve().parent),
        )

        stored_rows = conn.execute(
            "SELECT commit_sha, measurements FROM l0_points "
            "WHERE source = 'stored' AND health_major = ?",
            (effective_health_major,),
        ).fetchall()

        checked = 0
        agree = 0
        disagree = 0
        disagreements: "list[dict]" = []
        for row in stored_rows:
            commit_sha = row["commit_sha"]
            stored_measurements = json.loads(row["measurements"])
            replay_measurements, replay_unreplayable = replay_commit(
                repo_dir, commit_sha, registry=registry
            )
            verdict, diff = equality_check(
                stored_measurements, replay_measurements, replay_unreplayable, registry
            )
            conn.execute(
                "INSERT INTO equality_checks "
                "(commit_sha, health_major, verdict, diff, checked_at, replay_decl_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    commit_sha,
                    effective_health_major,
                    verdict,
                    _canonical_json(diff),
                    _utc_now_iso(now),
                    current_hash,
                ),
            )
            conn.commit()
            checked += 1
            if verdict == "agree":
                agree += 1
            else:
                disagree += 1
                disagreements.append({"commit_sha": commit_sha, **diff})

        finished_at = _utc_now_iso(now)
        result = {
            "checked": checked,
            "agree": agree,
            "disagree": disagree,
            "disagreements": disagreements,
        }
        conn.execute(
            "INSERT INTO replay_runs "
            "(started_at, finished_at, health_major, reason, commits_total, "
            "commits_done, skipped_old_format, unresolved, engine_build, detail) "
            "VALUES (?, ?, ?, 'verify', ?, ?, ?, ?, ?, ?)",
            (
                started_at,
                finished_at,
                effective_health_major,
                checked,
                checked,
                None,
                None,
                build,
                _canonical_json(result),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return result


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status(db_path: str | Path, code_repo_dir: str | Path | None = None) -> dict:
    """Counts per table, the last ``replay_runs`` row, the distinct
    ``health_major`` (from ``l0_points``) / ``health_version`` (from
    ``declarations``) values present in the store, and (PR B1 additions)
    the CURRENT declared-replay fingerprint, per-``(health_major,
    replay_decl_hash)`` ``git_replay`` row counts, and the latest equality
    verdict counts (one verdict per ``(commit_sha, health_major,
    replay_decl_hash)``, taking the row with the greatest ``checked_at`` --
    ``equality_checks`` is append-only, per that table's own migration
    docstring); and (#2600) for each DISTINCT ``engine_build`` present in
    ``l0_points``, whether it resolves as a commit in the CODE repo
    (``code_repo_dir``, default: the DISCRIMINATED resolver
    ``_find_code_repo_toplevel(Path(__file__).resolve().parent)`` -- this
    module's own git toplevel, ONLY if that toplevel is itself a code
    checkout containing ``src/engram/health/measurements.py``): ``true``/
    ``false`` for a sha-shaped value, ``null`` for a non-sha value (e.g.
    ``'unknown'`` or a ``.deployed-version`` ``alpha_sha`` that isn't a git
    sha) or when no code repo is resolvable at all -- either an explicitly
    passed ``code_repo_dir`` doesn't exist / isn't a git repo, OR, on the
    default path, this module's own git toplevel is NOT a code checkout
    (#2615: notably a DEPLOYED plugin tree, whose git toplevel is the DATA
    repo it's nested inside, not a code checkout -- reported as ``null``,
    never a false ``false``)."""
    if code_repo_dir is not None:
        effective_code_repo_dir = Path(code_repo_dir)
    else:
        effective_code_repo_dir = _find_code_repo_toplevel(
            Path(__file__).resolve().parent
        )
    conn = open_store(db_path)
    try:
        counts = {}
        for table in (
            "l0_points",
            "derived_points",
            "equality_checks",
            "derive_checks",
            "replay_runs",
            "declarations",
            "unresolved_snapshots",
        ):
            counts[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

        last_run_row = conn.execute(
            "SELECT * FROM replay_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        last_run = dict(last_run_row) if last_run_row is not None else None

        # (PR B2) the last REBUILD's verdict specifically -- last_replay_run
        # above is the latest run of ANY reason, which may not be a rebuild
        # at all if an ingest/backfill/verify ran more recently.
        last_rebuild_row = conn.execute(
            "SELECT * FROM replay_runs WHERE reason = 'rebuild' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        last_rebuild_run = dict(last_rebuild_row) if last_rebuild_row is not None else None

        health_majors = [
            r["health_major"]
            for r in conn.execute(
                "SELECT DISTINCT health_major FROM l0_points ORDER BY health_major"
            ).fetchall()
        ]
        health_versions = [
            r["health_version"]
            for r in conn.execute(
                "SELECT DISTINCT health_version FROM declarations ORDER BY health_version"
            ).fetchall()
        ]

        current_replay_decl_hash = health_measurements.replay_decl_hash()

        git_replay_counts = [
            {
                "health_major": r["health_major"],
                "replay_decl_hash": r["replay_decl_hash"],
                "count": r["c"],
            }
            for r in conn.execute(
                "SELECT health_major, replay_decl_hash, COUNT(*) AS c FROM l0_points "
                "WHERE source = 'git_replay' "
                "GROUP BY health_major, replay_decl_hash "
                "ORDER BY health_major, replay_decl_hash"
            ).fetchall()
        ]

        latest_by_key: "dict[tuple, tuple[str, str]]" = {}
        for r in conn.execute(
            "SELECT commit_sha, health_major, replay_decl_hash, verdict, checked_at "
            "FROM equality_checks"
        ).fetchall():
            key = (r["commit_sha"], r["health_major"], r["replay_decl_hash"])
            checked_at = r["checked_at"]
            if key not in latest_by_key or checked_at > latest_by_key[key][1]:
                latest_by_key[key] = (r["verdict"], checked_at)
        latest_equality_verdicts = {"agree": 0, "disagree": 0}
        for verdict, _ in latest_by_key.values():
            latest_equality_verdicts[verdict] = latest_equality_verdicts.get(verdict, 0) + 1

        # (PR C1) latest_derive_verdicts -- same "append-only, take the
        # latest per key by checked_at" shape as latest_equality_verdicts
        # above, but derive_checks' key has no replay_decl_hash column
        # (it's keyed on (commit_sha, health_version) only).
        latest_derive_by_key: "dict[tuple, tuple[str, str]]" = {}
        for r in conn.execute(
            "SELECT commit_sha, health_version, verdict, checked_at FROM derive_checks"
        ).fetchall():
            key = (r["commit_sha"], r["health_version"])
            checked_at = r["checked_at"]
            if key not in latest_derive_by_key or checked_at > latest_derive_by_key[key][1]:
                latest_derive_by_key[key] = (r["verdict"], checked_at)
        latest_derive_verdicts = {"agree": 0, "disagree": 0}
        for verdict, _ in latest_derive_by_key.values():
            latest_derive_verdicts[verdict] = latest_derive_verdicts.get(verdict, 0) + 1

        distinct_engine_builds = [
            r["engine_build"]
            for r in conn.execute(
                "SELECT DISTINCT engine_build FROM l0_points ORDER BY engine_build"
            ).fetchall()
        ]
        code_repo_valid = False
        if effective_code_repo_dir is not None:
            try:
                code_repo_check = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=str(effective_code_repo_dir),
                )
                code_repo_valid = (
                    code_repo_check.returncode == 0
                    and code_repo_check.stdout.strip() == "true"
                )
            except OSError:
                code_repo_valid = False

        engine_build_resolvability = []
        for build in distinct_engine_builds:
            if not code_repo_valid or not _SHA_RE.match(build):
                resolves = None
            else:
                resolves = _commit_exists(effective_code_repo_dir, build)
            engine_build_resolvability.append(
                {"engine_build": build, "resolves_in_code_repo": resolves}
            )
    finally:
        conn.close()

    return {
        "counts": counts,
        "last_replay_run": last_run,
        "last_rebuild_run": last_rebuild_run,
        "health_majors": health_majors,
        "health_versions": health_versions,
        "current_replay_decl_hash": current_replay_decl_hash,
        "git_replay_counts": git_replay_counts,
        "latest_equality_verdicts": latest_equality_verdicts,
        "latest_derive_verdicts": latest_derive_verdicts,
        "engine_build_resolvability": engine_build_resolvability,
    }


# ---------------------------------------------------------------------------
# PR C1 -- derived_points, export, the post-dream lifecycle hook
# ---------------------------------------------------------------------------
#
# "derived_points is derivation, not truth" (design doc §2 + D4.1, restated
# in this module's own PR C1 docstring addition above): for a version X,
# signals and scores are recomputed from l0_points.measurements through the
# declarations STORED for X (declarations.signal_defs/curve_defs/model_defs),
# NEVER through HEAD's live SIGNAL_DEFS/MODEL. The ONE exception is seeding
# (derive()'s own seed-from-live branch), allowed only when health_version
# == health.version.CURRENT, where HEAD *is* the declaration by definition.


def _import_health_module(name: str):
    """Lazy import of ``health.<name>`` (e.g. ``"engine"``, ``"model"``,
    ``"config"``, ``"signals"``, ``"curves"``), honoring this module's own
    dual execution mode -- see the top-of-file ``try``/``except
    ImportError`` block for ``measurements``/``version``, which this
    mirrors exactly. Tries the relative import first (the normal,
    package-imported path); on failure (bare script execution, no package
    context) falls back to the same ``sys.path.insert`` + absolute-import
    pattern used there. Deliberately a FUNCTION, not a module-level import,
    so callers (``derive_point``/``derive``) can import these submodules
    LAZILY -- never at series.py's own import time (this module's
    ``test_module_imports_nothing_from_engram_core_at_import`` guard stays
    green because none of these imports happen until one of PR C1's
    functions is actually CALLED)."""
    import importlib

    try:
        return importlib.import_module(f".{name}", package=__package__)
    except (ImportError, TypeError):
        # ImportError: the relative import resolved a package but the
        # submodule genuinely failed to import. TypeError: `__package__` is
        # falsy (bare script execution, no package context -- the same
        # condition the top-of-file try/except for measurements/version
        # handles via ImportError; `importlib.import_module` raises
        # TypeError instead of ImportError for this specific case).
        _src_engram = str(Path(__file__).resolve().parent.parent)
        if _src_engram not in sys.path:
            sys.path.insert(0, _src_engram)
        return importlib.import_module(f"health.{name}")


def derive_point(measurements: dict, declarations: dict) -> "tuple[dict, dict]":
    """Pure, module-level, unit-testable deriver (design doc §2 + D4.1):
    ``(signals, scores)`` for ONE L0 ``measurements`` dict, computed
    through the STORED ``declarations`` dict for a version -- never HEAD's
    live ``SIGNAL_DEFS``/``MODEL`` (see this module's "the rule this PR
    implements" note above ``derive``).

    ``declarations`` is the shape read off one ``declarations`` row, already
    JSON-decoded: ``{"signal_defs": {...} | None, "curve_defs": {...} |
    None, "model_defs": {...} | None}`` (``"config"`` is read by nothing
    here -- it is the vestigial blob the deriver never touches, per the
    design's false-positive control).

    **Signal order**: iterates ``declarations["signal_defs"]`` in ITS OWN
    dict order. On a real stored row this is alphabetical-by-name (the
    stored canonical JSON was written with ``sort_keys=True``, so Python's
    ``json.loads`` reconstructs a dict in that same sorted order) -- but
    that is NOT asserted as a contract by this function, and ``export``
    below does not rely on it either; a caller wanting a stable order
    should sort ``signals.keys()`` itself.

    **Signals**: reproduces ``health.signals._build``'s semantics exactly
    (gate check first, then the shared empty-denominator/null policy, then
    ``1 - rate`` for ``one_minus_rate``) -- but does NOT call ``health.
    signals.SIGNALS`` (HEAD's live declaration); every numerator/
    denominator/gate/form name comes from the passed-in ``declarations
    ["signal_defs"]`` dict instead (the same shape ``health.signals.
    signal_defs_jsonable()`` emits: ``{name: {numerator, denominator, form,
    gates, ...}}``). Any gate key whose ``measurements`` value is ``None``
    or falsy -> ``"UNAVAILABLE"``. A ``None`` numerator or denominator (an
    unreplayable L0 measurement, e.g. a ``git_replay`` row missing
    ``cascade_markers`` -- design §2: "a null L0 derives to UNAVAILABLE,
    never 0, never a crash") -> ``"UNAVAILABLE"``. A zero denominator ->
    ``"UNAVAILABLE"``. Otherwise ``n/d`` or ``1 - n/d`` per ``form``.

    **Scores**: ``{"unavailable_reason": "model_defs_absent"}`` when
    ``declarations["model_defs"]`` is ``None`` (a version whose
    declarations were ingested/seeded before PR C0 added the ``model_defs``
    blob). Otherwise reconstructs the historical model (``health.model.
    model_from_defs``) from ``declarations["model_defs"]``/``["curve_defs"]``
    and scores through the SAME function the live engine uses (``health.
    engine.score_signals``) -- design doc §2's "the same functions the live
    engine uses", true by construction, not by copy. This function's own
    ``"UNAVAILABLE"`` STRING sentinel (used in its ``signals`` return value)
    is mapped back to ``health.config.UNAVAILABLE`` (the real sentinel
    OBJECT) before calling ``score_signals``, which expects the object, not
    the string; ``score_signals``'s own ``_to_jsonable`` turns it back into
    the string ``"UNAVAILABLE"`` inside the returned ``scores``.

    Both ``health.model``/``health.engine``/``health.config`` are imported
    LAZILY, inside this function, via ``_import_health_module`` above --
    never at series.py's own module top level."""
    signal_defs = declarations.get("signal_defs") or {}
    signals: "dict[str, object]" = {}
    for name, d in signal_defs.items():
        gates = d.get("gates") or []
        if gates and not all(measurements.get(g) for g in gates):
            signals[name] = "UNAVAILABLE"
            continue
        num = measurements.get(d["numerator"])
        den = measurements.get(d["denominator"])
        if num is None or den is None or den == 0:
            signals[name] = "UNAVAILABLE"
            continue
        rate = num / den
        signals[name] = (1.0 - rate) if d["form"] == "one_minus_rate" else rate

    model_defs = declarations.get("model_defs")
    if model_defs is None:
        return signals, {"unavailable_reason": "model_defs_absent"}

    health_config = _import_health_module("config")
    health_model = _import_health_module("model")
    health_engine = _import_health_module("engine")

    curve_defs = declarations.get("curve_defs") or {}
    model = health_model.model_from_defs(model_defs, curve_defs)
    l1_signals = {
        name: (health_config.UNAVAILABLE if value == "UNAVAILABLE" else value)
        for name, value in signals.items()
    }
    _, scores = health_engine.score_signals(l1_signals, model)
    return signals, scores


def derive(
    db_path: "str | Path",
    health_version: "str | None" = None,
    engine_build: "str | None" = None,
    now: "datetime | None" = None,
    seed_from_live: bool = True,
    snapshots_dir: "str | Path | None" = None,
) -> dict:
    """Populate ``derived_points`` for ``health_version`` (default
    ``health.version.CURRENT``, imported via the module-level
    ``health_version_module`` alias -- see that alias's own comment for why
    a second name is needed here, distinct from ``health_version`` the
    module import, given this function's own ``health_version`` STRING
    parameter) from ``l0_points``, through the declarations STORED for
    that version -- never HEAD's live declarations, except when seeding a
    genuinely-absent CURRENT version's row (below).

    **Declarations**: loads the ``declarations`` row for ``health_version``.
    If absent:

    - ``seed_from_live and health_version == health_version_module.CURRENT``
      -- seed it from the live engine (``health.signals.
      signal_defs_jsonable()``, ``health.curves.curve_defs_jsonable(MODEL.
      curves)``, ``health.engine._config_to_dict(CONFIG)``, ``health.model.
      model_defs_jsonable(MODEL)`` -- all four lazily imported), with
      ``first_seen_file = 'live-engine'``, ``first_seen_commit = NULL``, and
      ``detail.declarations_seeded_from_live = True`` in the returned run
      record. This is the ONE place this function reads HEAD's live
      declaration -- justified because, for the CURRENT version, HEAD *is*
      the declaration by definition (nothing else could have declared it
      yet).
    - Otherwise: returns a run record with ``status: "no_declarations"`` and
      zero rows touched -- a non-current version with no stored declaration
      is NEVER derived from HEAD.

    **Candidates**: every ``l0_points`` row with ``health_major ==
    health_version_module.parse_version(health_version)[0]`` that is either
    a ``stored`` row, or a ``git_replay`` row under the CURRENT
    ``health.measurements.replay_decl_hash()`` (a stale-fingerprint
    ``git_replay`` row is never derived against -- ``backfill``'s own
    gap-fill mechanism is what re-replays it under a new fingerprint).

    **Never-mutate**: for each candidate lacking a ``derived_points`` row
    under its own ``(commit_sha, health_version, l0_source,
    replay_decl_hash)`` key, ``derive_point`` computes and an INSERT (never
    an UPDATE) adds it. A ``stored`` row arriving after a ``git_replay``
    row already derived for the same commit simply adds a SECOND derived
    row (distinct ``l0_source``) -- ``export`` picks between them (prefers
    ``stored``).

    **Equality control** (the derivation's own correctness test, run on
    real data every night via the lifecycle hook, not only in CI): for
    every ``stored`` L0 row derived THIS run, if ``snapshots_dir`` is given,
    the snapshot file that produced it (``l0_points.snapshot_file``, read
    from under ``snapshots_dir``) carries its own ``signals``/``scores``.
    ``derive_point``'s output is compared against the file's own JSON
    (parsed, exact equality -- same inputs, same functions, must be
    identical, no float tolerance): ``signals`` always; ``scores`` too when
    ``declarations["model_defs"]`` is non-null. A verdict (``"agree"``/
    ``"disagree"``) is appended to ``derive_checks`` (append-only, like
    ``equality_checks``) for every ``stored`` row checked this run; a
    ``disagree`` verdict's diff names the specific signal(s) (under
    ``diff["signals"]``) and/or scored metric(s) (``diff["scores_metrics"]``)
    that differ, or a generic ``diff["scores_other"]`` when the scores
    dicts differ in some other shape (e.g. ``metrics_in_play``/aspect
    composition) with no differing per-metric score to name -- never a bare
    "something disagreed" signal. When ``snapshots_dir`` is ``None`` the
    control is skipped entirely and the run record's
    ``detail.control.skipped_reason`` says so.

    **Run record**, appended to ``replay_runs`` with ``reason='derive'``:
    ``{reason, status, started_at, finished_at, health_version,
    commits_total (candidates), commits_done (rows inserted), engine_build,
    detail: {health_version, rows_by_source: {stored, git_replay},
    declarations_seeded_from_live, control: {compared, agree, disagree,
    skipped_reason}, disagreements: [...]}}``.
    """
    conn = open_store(db_path)
    try:
        started_at = _utc_now_iso(now)
        effective_health_version = (
            health_version if health_version is not None else health_version_module.CURRENT
        )
        build = engine_build if engine_build is not None else detect_engine_build()
        major, _minor = health_version_module.parse_version(effective_health_version)

        decl_row = conn.execute(
            "SELECT signal_defs, curve_defs, config, model_defs FROM declarations "
            "WHERE health_version = ?",
            (effective_health_version,),
        ).fetchone()

        declarations_seeded_from_live = False
        if decl_row is None:
            if seed_from_live and effective_health_version == health_version_module.CURRENT:
                health_signals = _import_health_module("signals")
                health_curves = _import_health_module("curves")
                health_engine = _import_health_module("engine")
                health_model = _import_health_module("model")

                signal_defs_json = _canonical_json(health_signals.signal_defs_jsonable())
                curve_defs_json = _canonical_json(
                    health_curves.curve_defs_jsonable(health_engine.MODEL.curves)
                )
                config_json = _canonical_json(health_engine._config_to_dict(health_engine.CONFIG))
                model_defs_json = _canonical_json(
                    health_model.model_defs_jsonable(health_engine.MODEL)
                )
                conn.execute(
                    "INSERT INTO declarations "
                    "(health_version, signal_defs, curve_defs, config, model_defs, "
                    "first_seen_commit, first_seen_file, engine_build) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        effective_health_version,
                        signal_defs_json,
                        curve_defs_json,
                        config_json,
                        model_defs_json,
                        None,
                        "live-engine",
                        build,
                    ),
                )
                conn.commit()
                decl_row = conn.execute(
                    "SELECT signal_defs, curve_defs, config, model_defs FROM declarations "
                    "WHERE health_version = ?",
                    (effective_health_version,),
                ).fetchone()
                declarations_seeded_from_live = True
            else:
                finished_at = _utc_now_iso(now)
                run_record = {
                    "reason": "derive",
                    "status": "no_declarations",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "health_version": effective_health_version,
                    "commits_total": 0,
                    "commits_done": 0,
                    "engine_build": build,
                    "detail": {
                        "health_version": effective_health_version,
                        "declarations_seeded_from_live": False,
                    },
                }
                conn.execute(
                    "INSERT INTO replay_runs "
                    "(started_at, finished_at, health_major, reason, commits_total, "
                    "commits_done, skipped_old_format, unresolved, engine_build, detail) "
                    "VALUES (?, ?, ?, 'derive', ?, ?, ?, ?, ?, ?)",
                    (
                        started_at,
                        finished_at,
                        major,
                        0,
                        0,
                        None,
                        None,
                        build,
                        _canonical_json(run_record["detail"]),
                    ),
                )
                conn.commit()
                return run_record

        declarations_dict = {
            "signal_defs": json.loads(decl_row["signal_defs"]),
            "curve_defs": json.loads(decl_row["curve_defs"]),
            "config": json.loads(decl_row["config"]),
            "model_defs": (
                json.loads(decl_row["model_defs"]) if decl_row["model_defs"] is not None else None
            ),
        }

        current_hash = health_measurements.replay_decl_hash()
        candidates = conn.execute(
            "SELECT * FROM l0_points WHERE health_major = ? "
            "AND (source = 'stored' OR (source = 'git_replay' AND replay_decl_hash = ?))",
            (major, current_hash),
        ).fetchall()

        rows_by_source = {"stored": 0, "git_replay": 0}
        compared = 0
        agree = 0
        disagree = 0
        disagreements: "list[dict]" = []
        skipped_reason = None if snapshots_dir is not None else (
            "snapshots_dir not provided -- equality control skipped"
        )

        for row in candidates:
            commit_sha = row["commit_sha"]
            source = row["source"]
            r_hash = row["replay_decl_hash"]
            existing = conn.execute(
                "SELECT 1 FROM derived_points WHERE commit_sha = ? AND health_version = ? "
                "AND l0_source = ? AND replay_decl_hash = ?",
                (commit_sha, effective_health_version, source, r_hash),
            ).fetchone()
            if existing is not None:
                continue

            measurements = json.loads(row["measurements"])
            signals, scores = derive_point(measurements, declarations_dict)
            computed_at = _utc_now_iso(now)
            conn.execute(
                "INSERT INTO derived_points "
                "(commit_sha, health_version, l0_source, replay_decl_hash, signals, "
                "scores, computed_at, engine_build) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    commit_sha,
                    effective_health_version,
                    source,
                    r_hash,
                    _canonical_json(signals),
                    _canonical_json(scores),
                    computed_at,
                    build,
                ),
            )
            conn.commit()
            rows_by_source[source] = rows_by_source.get(source, 0) + 1

            if source == "stored" and snapshots_dir is not None:
                snap_file = row["snapshot_file"]
                snap = None
                if snap_file:
                    snap_path = Path(snapshots_dir) / snap_file
                    try:
                        snap = json.loads(snap_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        snap = None
                if snap is not None:
                    compared += 1
                    file_signals = snap.get("signals") or {}
                    diff: "dict[str, object]" = {}

                    mismatched_signal_names = sorted(
                        n
                        for n in set(signals) | set(file_signals)
                        if signals.get(n) != file_signals.get(n)
                    )
                    if mismatched_signal_names:
                        diff["signals"] = {
                            "names": mismatched_signal_names,
                            "derived": {n: signals.get(n) for n in mismatched_signal_names},
                            "snapshot": {n: file_signals.get(n) for n in mismatched_signal_names},
                        }

                    if declarations_dict["model_defs"] is not None:
                        file_scores = snap.get("scores") or {}
                        file_metrics = file_scores.get("metrics") or {}
                        derived_metrics = (scores or {}).get("metrics") or {}
                        mismatched_metric_names = sorted(
                            n
                            for n in set(derived_metrics) | set(file_metrics)
                            if derived_metrics.get(n) != file_metrics.get(n)
                        )
                        if mismatched_metric_names:
                            diff["scores_metrics"] = {
                                "names": mismatched_metric_names,
                                "derived": {n: derived_metrics.get(n) for n in mismatched_metric_names},
                                "snapshot": {n: file_metrics.get(n) for n in mismatched_metric_names},
                            }
                        if scores != file_scores and "scores_metrics" not in diff:
                            diff["scores_other"] = {"derived": scores, "snapshot": file_scores}

                    verdict = "disagree" if diff else "agree"
                    conn.execute(
                        "INSERT INTO derive_checks "
                        "(commit_sha, health_version, verdict, diff, checked_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            commit_sha,
                            effective_health_version,
                            verdict,
                            _canonical_json(diff),
                            _utc_now_iso(now),
                        ),
                    )
                    conn.commit()
                    if verdict == "agree":
                        agree += 1
                    else:
                        disagree += 1
                        disagreements.append({"commit_sha": commit_sha, **diff})

        finished_at = _utc_now_iso(now)
        commits_done = sum(rows_by_source.values())
        detail = {
            "health_version": effective_health_version,
            "rows_by_source": rows_by_source,
            "declarations_seeded_from_live": declarations_seeded_from_live,
            "control": {
                "compared": compared,
                "agree": agree,
                "disagree": disagree,
                "skipped_reason": skipped_reason,
            },
            "disagreements": disagreements,
        }
        run_record = {
            "reason": "derive",
            "status": "ok",
            "started_at": started_at,
            "finished_at": finished_at,
            "health_version": effective_health_version,
            "commits_total": len(candidates),
            "commits_done": commits_done,
            "engine_build": build,
            "detail": detail,
        }
        conn.execute(
            "INSERT INTO replay_runs "
            "(started_at, finished_at, health_major, reason, commits_total, "
            "commits_done, skipped_old_format, unresolved, engine_build, detail) "
            "VALUES (?, ?, ?, 'derive', ?, ?, ?, ?, ?, ?)",
            (
                started_at,
                finished_at,
                major,
                len(candidates),
                commits_done,
                None,
                None,
                build,
                _canonical_json(detail),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return run_record


def export(
    db_path: "str | Path", health_version: "str | None" = None, signal: "str | None" = None
) -> dict:
    """The JSON shape ``/api/health/v2/series`` serves (design doc §4, §6).
    READ-ONLY: opens ``db_path`` via ``sqlite3.connect(f"file:{db_path}?
    mode=ro", uri=True)`` -- NEVER ``open_store`` (which creates tables and
    runs migrations; a read path must never write). If ``db_path`` does not
    exist, this function never even attempts to open it -- the caller can
    assert the file still doesn't exist afterward.

    Distinct fail-honest returns (a dict with ``error``/``reason``, NEVER an
    exception):

    - ``no_db`` -- ``db_path`` does not exist.
    - ``version_absent`` -- no ``derived_points`` rows for ``health_version``
      (default ``health_version_module.CURRENT``); ``has_declarations``
      says whether a ``declarations`` row exists for it regardless.
      Returned when there is no ``l0_points`` data for the version's MAJOR,
      OR when ``health_version`` is non-CURRENT and has no stored
      ``declarations`` row -- signal formulas (numerator/denominator/form/
      gate) are MINOR-scoped (``health/version.py``'s own MAJOR/MINOR
      split; ``health/measurements.py``'s ``canonical_derived_hash``), so
      serving a non-current, undeclared version's raw values under today's
      live formulas could silently mislabel a different formula's output
      with that version's string. Only when L0 data exists AND the
      requested version genuinely IS ``health_version_module.CURRENT``
      (declared or not) does ``export`` instead return the raw L0 series
      with ``scores_available: False``; see below.
    - ``signal_unknown`` -- ``signal`` is not a key of that version's
      ``signal_defs``.
    - ``unreadable`` -- a ``sqlite3.Error`` occurred (corrupt file, etc.);
      the error text is the ``error`` value.

    Shape (design doc §6, one point per commit -- prefers the ``stored``-
    derived row when both exist for a commit; a ``git_replay`` row is
    included only under the CURRENT ``replay_decl_hash``; ordered by
    ``t`` ascending, then ``commit``)::

        {"version": ..., "replay_decl_hash": <current hash>,
         "signals": {name: [point, ...]},   # every signal_defs name, or
                                             # just `signal` when given
         "aspects": {aspect: [{t, commit, source, score}, ...]},
         "composed": [{t, commit, source, value, points_in_play,
                        composed_coverage}, ...],
         "scores_available": bool}          # False when model_defs_absent

    where ``point = {t, commit, turn, source, num, den, raw, score,
    unreplayable}`` -- ``num``/``den`` read off the L0 row's own
    ``measurements`` via the version's ``signal_defs[name].numerator/
    denominator``; ``unreplayable`` is the L0 row's own ``unreplayable``
    entries restricted to those two keys (``None`` if neither is present).
    This function does NOT compute distribution statistics (#2518 §4: the
    page does that client-side over ``history``).

    ``signals[name][i]["raw"]``/``["score"]`` can be populated even when
    ``scores_available`` is ``False``: when the requested ``health_version``
    genuinely IS ``health_version_module.CURRENT`` and was never
    ``derive``d, but L0 data for its MAJOR exists, ``raw`` is computed live
    via ``derive_point`` while ``score`` stays ``None`` -- mirrors
    ``derive()``'s own live-seeding guard exactly (``seed_from_live and
    effective_health_version == health_version_module.CURRENT``). A
    non-current version with no stored declaration returns
    ``version_absent`` instead (see above), never a live-formula fallback.
    ``"UNAVAILABLE"`` (string) is a normal value for ``raw`` in that case,
    exactly as it already is in the normal derived path."""
    db_path = Path(db_path)
    if not db_path.exists():
        return {
            "error": f"series store not found at {db_path}",
            "reason": "no_db",
            "db_path": str(db_path),
        }

    effective_health_version = (
        health_version if health_version is not None else health_version_module.CURRENT
    )

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        return {"error": str(e), "reason": "unreadable"}

    try:
        try:
            major, _minor = health_version_module.parse_version(effective_health_version)
        except ValueError as e:
            return {"error": str(e), "reason": "unreadable"}

        current_hash = health_measurements.replay_decl_hash()

        try:
            derived_count = conn.execute(
                "SELECT COUNT(*) AS c FROM derived_points WHERE health_version = ?",
                (effective_health_version,),
            ).fetchone()["c"]
            decl_row = conn.execute(
                "SELECT * FROM declarations WHERE health_version = ?",
                (effective_health_version,),
            ).fetchone()
            l0_rows = conn.execute(
                "SELECT * FROM l0_points WHERE health_major = ? "
                "AND (source = 'stored' OR (source = 'git_replay' AND replay_decl_hash = ?))",
                (major, current_hash),
            ).fetchall()
        except sqlite3.Error as e:
            return {"error": str(e), "reason": "unreadable"}

        l0_by_commit: "dict[str, sqlite3.Row]" = {}
        for r in l0_rows:
            c = r["commit_sha"]
            existing_l0 = l0_by_commit.get(c)
            if existing_l0 is None or (existing_l0["source"] != "stored" and r["source"] == "stored"):
                l0_by_commit[c] = r

        if derived_count == 0 and not l0_by_commit:
            return {
                "error": f"no derived_points rows for {effective_health_version!r}",
                "reason": "version_absent",
                "has_declarations": decl_row is not None,
            }

        if decl_row is not None and decl_row["signal_defs"]:
            signal_defs = json.loads(decl_row["signal_defs"])
        elif effective_health_version == health_version_module.CURRENT:
            # No declaration was ever ingested for CURRENT (e.g. the MINOR
            # was bumped in code but no snapshot at the new version has
            # been ingested yet) -- fall back to the LIVE code's current
            # signal definitions. This is safe ONLY because the requested
            # version genuinely IS the code's live version by definition --
            # there is no formula mismatch to mislabel. Mirrors derive()'s
            # own guard (`seed_from_live and effective_health_version ==
            # health_version_module.CURRENT`) exactly.
            health_signals = _import_health_module("signals")
            signal_defs = health_signals.signal_defs_jsonable()
        else:
            # A non-current version with no stored declaration -- signal
            # formulas (numerator/denominator/form/gate) are MINOR-scoped
            # (health/version.py, health/measurements.py's
            # canonical_derived_hash), so borrowing HEAD's live definitions
            # here would silently compute values under a possibly
            # different formula and mislabel them with this (different,
            # non-current) requested version string -- exactly what
            # derive_point()'s own docstring says never to do. Treat this
            # the same as version_absent: there is nothing this function
            # can safely serve for this version.
            return {
                "error": (
                    f"{effective_health_version!r} is not CURRENT and has no "
                    "stored declaration -- refusing to derive signal_defs "
                    "from HEAD for a non-current version"
                ),
                "reason": "version_absent",
                "has_declarations": decl_row is not None,
            }

        if signal is not None and signal not in (signal_defs or {}):
            return {
                "error": f"unknown signal {signal!r} for {effective_health_version!r}",
                "reason": "signal_unknown",
            }

        try:
            derived_rows = conn.execute(
                "SELECT * FROM derived_points WHERE health_version = ?",
                (effective_health_version,),
            ).fetchall()
        except sqlite3.Error as e:
            return {"error": str(e), "reason": "unreadable"}

        derived_by_key = {
            (r["commit_sha"], r["l0_source"], r["replay_decl_hash"]): r for r in derived_rows
        }

        triples: "list[tuple[str, sqlite3.Row, sqlite3.Row | None]]" = []
        for commit_sha, l0_row in l0_by_commit.items():
            key = (commit_sha, l0_row["source"], l0_row["replay_decl_hash"])
            derived_row = derived_by_key.get(key)
            triples.append((commit_sha, l0_row, derived_row))
        triples.sort(key=lambda t: (t[1]["commit_time"], t[0]))

        names = [signal] if signal is not None else list(signal_defs.keys())
        signals_out: "dict[str, list]" = {name: [] for name in names}
        aspects_out: "dict[str, list]" = {}
        composed_out: "list[dict]" = []
        scores_available = (
            derived_count > 0
            and decl_row is not None
            and decl_row["model_defs"] is not None
        )

        for commit_sha, l0_row, derived_row in triples:
            measurements = json.loads(l0_row["measurements"])
            unreplayable_all = json.loads(l0_row["unreplayable"])
            if derived_row is not None:
                signals_json = json.loads(derived_row["signals"])
                scores_json = json.loads(derived_row["scores"])
            else:
                # No stored derived_points row for this exact commit+version --
                # compute the raw signal values live via the SAME pure deriver
                # `derive()` itself uses, rather than leaving `raw`/`score` as
                # None. `derive_point` degrades scores to
                # {"unavailable_reason": "model_defs_absent"} on its own when
                # `model_defs` is absent -- we don't need to pass curve_defs/
                # model_defs at all here, since `scores_available` (computed
                # above) already forces every point's `score` to None and skips
                # the aspects/composed block below regardless of what
                # `derive_point` returns for scores.
                signals_json, _ = derive_point(measurements, {"signal_defs": signal_defs})
                scores_json = {}
            source = l0_row["source"]
            t = l0_row["commit_time"]
            turn = l0_row["turn"]

            for name in names:
                d = signal_defs.get(name)
                if d is None:
                    continue
                num_key = d["numerator"]
                den_key = d["denominator"]
                unrep = {
                    k: unreplayable_all[k] for k in (num_key, den_key) if k in unreplayable_all
                }
                point = {
                    "t": t,
                    "commit": commit_sha,
                    "turn": turn,
                    "source": source,
                    "num": measurements.get(num_key),
                    "den": measurements.get(den_key),
                    "raw": signals_json.get(name),
                    "score": (
                        (scores_json.get("metrics") or {}).get(name)
                        if scores_available and isinstance(scores_json, dict)
                        else None
                    ),
                    "unreplayable": unrep or None,
                }
                signals_out[name].append(point)

            if derived_row is not None and scores_available and isinstance(scores_json, dict):
                for aspect_name, aspect_score in (scores_json.get("aspects") or {}).items():
                    aspects_out.setdefault(aspect_name, []).append(
                        {"t": t, "commit": commit_sha, "source": source, "score": aspect_score}
                    )
                composed_out.append(
                    {
                        "t": t,
                        "commit": commit_sha,
                        "source": source,
                        "value": scores_json.get("composed"),
                        "points_in_play": scores_json.get("points_in_play"),
                        "composed_coverage": scores_json.get("composed_coverage"),
                    }
                )

        return {
            "version": effective_health_version,
            "replay_decl_hash": current_hash,
            "signals": signals_out,
            "aspects": aspects_out,
            "composed": composed_out,
            "scores_available": scores_available,
        }
    finally:
        conn.close()


def _trim_run_record(record: dict) -> dict:
    """Trim a run-record's ``detail`` to counts only (no long lists) --
    ``checkpoint_hook``'s own contract: its summary lands in an MCP tool
    result and must stay small. Any list-valued ``detail`` entry becomes
    ``{"count": len(...)}``; one level of nested dict is walked the same
    way (so e.g. ``detail.control`` -- a small dict of counts -- passes
    through unchanged, while a nested list inside it would be trimmed
    too). Every other field is passed through unchanged."""
    trimmed = dict(record)
    detail = trimmed.get("detail")
    if isinstance(detail, dict):
        new_detail: dict = {}
        for k, v in detail.items():
            if isinstance(v, list):
                new_detail[k] = {"count": len(v)}
            elif isinstance(v, dict):
                new_detail[k] = {
                    kk: ({"count": len(vv)} if isinstance(vv, list) else vv)
                    for kk, vv in v.items()
                }
            else:
                new_detail[k] = v
        trimmed["detail"] = new_detail
    return trimmed


def checkpoint_hook(
    snapshots_dir: "str | Path", repo_dir: "str | Path", db_path: "str | Path", mode: str
) -> dict:
    """The post-dream lifecycle hook (engram-alpha#2575 PR C, design doc §4
    "When it runs"): runs ``ingest`` -> ``backfill`` -> ``derive(...,
    snapshots_dir=snapshots_dir)`` IN ORDER, each in its OWN ``try``/
    ``except`` -- one step's failure never blocks the next, and NONE of
    them ever propagates out of this function (mirrors ``write_snapshot``'s
    own best-effort posture in ``engram_lifecycle.py``: a health-series
    failure must never break the checkpoint it's piggybacked onto).

    ``mode`` is accepted but not branched on inside this function -- the
    caller (``engram_lifecycle._checkpoint_internal``) is what decides
    WHETHER to call this at all (post_dream only, never nap -- see that
    call site's own comment) and passes ``mode`` through only so a caller
    reading this function's return value can see which mode it ran under.

    Returns ``{ingest: <summary or {"error": ...}>, backfill: <...>,
    derive: <...>, elapsed_seconds: float}`` where each summary is the
    corresponding function's run record with ``detail`` TRIMMED to counts
    (``_trim_run_record`` -- no long lists in a tool result).

    Two things a caller should expect and NOT treat as a defect (design
    doc §4):

    (a) The snapshot JUST WRITTEN by ``write_snapshot`` in THIS SAME
        checkpoint has NO ``knowledge.sql`` child commit yet -- the async
        snapshot worker commits it later, out of band (design D1). THIS
        run's ``ingest`` records that file under ``unresolved_snapshots``
        with reason ``no_child_commit``; the NEXT post-dream run's
        ``ingest`` resolves it once the worker has caught up. Expected,
        not a defect.
    (b) The FIRST run on a seat is ``"initial"`` (``backfill``'s own
        reason) and replays EVERY ``knowledge.sql`` commit (~20 seconds for
        ~140 commits, measured 2026-09-05 on the design doc's own seat). A
        MAJOR version bump replays them all again, under the new
        ``health_major`` -- the design's own "backfills itself the first
        night" property (§4)."""
    import time

    start = time.monotonic()
    result: dict = {}

    try:
        ingest_result = ingest(snapshots_dir, repo_dir, db_path)
        result["ingest"] = _trim_run_record(ingest_result)
    except Exception as e:
        result["ingest"] = {"error": str(e)}

    try:
        backfill_result = backfill(repo_dir, db_path)
        result["backfill"] = _trim_run_record(backfill_result)
    except Exception as e:
        result["backfill"] = {"error": str(e)}

    try:
        derive_result = derive(db_path, snapshots_dir=snapshots_dir)
        result["derive"] = _trim_run_record(derive_result)
    except Exception as e:
        result["derive"] = {"error": str(e)}

    result["elapsed_seconds"] = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#
# Imports MAY NOT resolve when this file is run directly as a script
# (``python3 src/engram/health/series.py ...``) vs. imported as a package
# module (``from health import series``) -- a bare `from . import version`
# would fail under the script-execution form (no package context). PR A
# never needed health/version.py at all (it never computed a health_version
# string itself -- every health_version it touched came verbatim off a
# snapshot file's own "health_version" key), so its CLI simply never
# imported it. PR B1's ``backfill``/``verify`` DO need ``health.version.
# MAJOR`` (the default ``health_major`` and the ``raw_schema_version`` stamp
# in ``replay_commit``) -- see the module-level ``try``/``except ImportError``
# block near the top of this file for the try-relative-then-absolute
# fallback that makes that import work in BOTH execution modes.


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health.series",
        description="health-score series store: ingest / rebuild / status / backfill / verify (PR A + PR B1 + PR B2).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser, needs_snapshots_and_repo: bool) -> None:
        p.add_argument("--db", required=True, help="path to series.db")
        if needs_snapshots_and_repo:
            p.add_argument("--snapshots-dir", required=True)
            p.add_argument("--repo-dir", required=True)
            p.add_argument("--engine-build", default=None)

    p_ingest = sub.add_parser("ingest")
    _add_common(p_ingest, needs_snapshots_and_repo=True)

    p_rebuild = sub.add_parser("rebuild")
    _add_common(p_rebuild, needs_snapshots_and_repo=True)
    p_rebuild.add_argument(
        "--alpha-repo", default=None,
        help="engram-alpha checkout to archive historical engine_builds "
        "from (two-diff mode only; defaults to this module's own checkout)",
    )
    p_rebuild.add_argument(
        "--single-diff", action="store_true",
        help="opt back into the pre-D4.2 single-diff form -- content and "
        "build change are CONFOUNDED in this mode",
    )
    p_rebuild.add_argument(
        "--keep-scratch", action="store_true",
        help="keep the scratch tree(s) used to regenerate historical "
        "engine_builds (two-diff mode only) instead of deleting them",
    )

    p_status = sub.add_parser("status")
    _add_common(p_status, needs_snapshots_and_repo=False)
    p_status.add_argument(
        "--code-repo-dir", default=None,
        help="engram-alpha checkout to resolve engine_build shas against "
        "(defaults to this module's own checkout)",
    )

    p_backfill = sub.add_parser("backfill")
    p_backfill.add_argument("--db", required=True, help="path to series.db")
    p_backfill.add_argument("--repo-dir", required=True)
    p_backfill.add_argument("--engine-build", default=None)
    p_backfill.add_argument(
        "--health-major", type=int, default=None,
        help="defaults to health.version.MAJOR",
    )

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--db", required=True, help="path to series.db")
    p_verify.add_argument("--repo-dir", required=True)
    p_verify.add_argument(
        "--health-major", type=int, default=None,
        help="defaults to health.version.MAJOR",
    )

    p_derive = sub.add_parser("derive")
    p_derive.add_argument("--db", required=True, help="path to series.db")
    p_derive.add_argument(
        "--health-version", default=None,
        help="defaults to health.version.CURRENT (health_version_module.CURRENT)",
    )
    p_derive.add_argument(
        "--snapshots-dir", default=None,
        help="if given, runs the equality control against each stored row's own snapshot file",
    )
    p_derive.add_argument("--engine-build", default=None)
    p_derive.add_argument(
        "--no-seed", action="store_true",
        help="never seed declarations from the live engine, even for the CURRENT version",
    )

    p_export = sub.add_parser("export")
    p_export.add_argument("--db", required=True, help="path to series.db")
    p_export.add_argument(
        "--health-version", default=None,
        help="defaults to health.version.CURRENT (health_version_module.CURRENT)",
    )
    p_export.add_argument("--signal", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        result = ingest(
            args.snapshots_dir, args.repo_dir, args.db, engine_build=args.engine_build
        )
    elif args.command == "rebuild":
        if args.single_diff:
            print(
                "health.series: --single-diff requested -- content and build "
                "changes are CONFOUNDED in this mode (a code change and a data "
                "change are indistinguishable); prefer the two-diff default.",
                file=sys.stderr,
            )
        result = rebuild(
            args.snapshots_dir,
            args.repo_dir,
            args.db,
            alpha_repo=args.alpha_repo,
            engine_build=args.engine_build,
            single_diff=args.single_diff,
            keep_scratch=args.keep_scratch,
        )
    elif args.command == "status":
        result = status(args.db, code_repo_dir=args.code_repo_dir)
    elif args.command == "backfill":
        result = backfill(
            args.repo_dir,
            args.db,
            health_major=args.health_major,
            engine_build=args.engine_build,
        )
    elif args.command == "verify":
        result = verify(args.repo_dir, args.db, health_major=args.health_major)
    elif args.command == "derive":
        result = derive(
            args.db,
            health_version=args.health_version,
            engine_build=args.engine_build,
            seed_from_live=not args.no_seed,
            snapshots_dir=args.snapshots_dir,
        )
    elif args.command == "export":
        result = export(args.db, health_version=args.health_version, signal=args.signal)
    else:  # pragma: no cover -- argparse `required=True` prevents this
        parser.error(f"unknown command: {args.command!r}")
        return 2

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
