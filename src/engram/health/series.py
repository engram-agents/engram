"""health.series -- the health-score series store: ingest + rebuild + status
+ replay + backfill + verify.

Design of record: ``docs/health-series-design.md`` (design v1 sections 0-10,
amended by deltas D1-D5; the deltas win over the sections they amend). This
module implements **PR A and PR B1** ("#2575 PR A", "#2575 PR B") of the
three-PR slicing named in that document's "Slicing after the deltas"
section (further split into B1/B2 in that doc's "Scope split" paragraph):

  - **PR A** -- the store schema (all tables, including ``replay_decl_
    hash``, the ``unreplayable`` class shape, ``anchor_resolution``,
    ``declarations``, ``unresolved_snapshots``), ``ingest``, old-format
    refusal, anchor resolution (D1), a single-diff ``rebuild``, and the CLI
    skeleton.
  - **PR B1 (this module, additionally)** -- the replay driver
    (``replay_commit``), ``MeasurementDef.replay`` (in ``measurements.py``)
    and its ``replay_decl_hash`` fingerprint (D2), the equality control
    (``equality_check``), the regular backfill mechanism (``backfill``),
    ``verify``, and the CLI's ``backfill``/``verify`` subcommands.
  - **PR B2 (NOT this module -- follow-up)** -- the two-diff form of
    ``rebuild`` (D4.2: content-changed under the stored ``engine_build`` vs
    build-changed under HEAD), which needs a checkout of a historical
    ``engine_build`` into a scratch tree -- a real blast-radius jump from
    "replay a dump into :memory:" to "checkout a historical commit's
    ``health/`` package," deliberately kept out of B1.
  - **PR C (NOT this module)** -- ``derived_points`` population, ``export``,
    and the lifecycle checkpoint hook.

What PR B1 explicitly does NOT do (left as ``TODO(#2575 PR C)`` / ``TODO(
#2575 PR B2)`` markers below): no ``derived_points`` population, no
``export``, no lifecycle wiring (nothing in ``engram_lifecycle.py`` or
``engram_snapshot_worker.py`` calls into this module), no two-diff
``rebuild``.

The store (``series.db``) is DERIVED and REBUILDABLE -- never the SSoT. The
snapshot JSON files under ``<snapshots_dir>/*.json`` (plus git history for
replay, in PR B) are the SSoT; this store is an index over them. ``rebuild``
exists precisely because of this: it reconstructs a fresh store from
scratch and diffs it against the live one, and only replaces the live store
when they agree byte-for-byte on every non-run-metadata column (design
doc's "safety property... and a test", section 2).

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
``hashlib``, ``datetime``, ``pathlib``, ``argparse``) and takes every path
as an explicit argument -- it deliberately does NOT ``import engram_core``
or read ``DATA_DIR`` at module top level (or anywhere), matching health/
engine.py's own lazy-import discipline (see that module's "NOTE on
engram_core access"). Every git call is a ``subprocess.run([...])`` argument
list -- never ``shell=True``, never a piped string, never ``git describe``.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
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
    signals TEXT NOT NULL,
    scores TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    engine_build TEXT NOT NULL,
    PRIMARY KEY (commit_sha, health_version)
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
    reason TEXT CHECK(reason IN ('ingest','initial','gap_fill','major_bump','verify','rebuild')),
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


def open_store(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the series store at ``db_path``, with all
    tables created idempotently. Plain rollback journal -- WAL is NOT
    wanted here (design instruction: the store is single-writer, offline).

    Because tables are created with ``CREATE TABLE IF NOT EXISTS``, an
    already-created store keeps whatever CHECK constraints (or column set)
    it was created with -- a schema-level change (widening ``unresolved_
    snapshots.reason`` to add ``commit_already_keyed``; adding ``equality_
    checks.replay_decl_hash``) does not reach an existing store on its own.
    These are the store's first two schema migrations, run in order after
    creating tables: (1) detect the old ``unresolved_snapshots`` CHECK
    (``PRAGMA table_info`` doesn't expose CHECKs, so the literal is read off
    ``sqlite_master.sql``) and, if found, recreate the table preserving its
    rows; (2) detect a missing ``equality_checks.replay_decl_hash`` column
    (a plain column, so ``PRAGMA table_info`` sees it directly) and, if
    missing, ``ALTER TABLE ADD COLUMN`` it in place. Both are safe
    specifically because both tables are DERIVED state -- ``rebuild``
    reconstructs ``unresolved_snapshots`` from the snapshot files and the
    repo, and ``equality_checks`` is itself a re-runnable control
    (``verify`` re-populates it) -- so migrating either in place never
    risks a source of truth."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(_SCHEMA)
    conn.commit()
    if _unresolved_snapshots_needs_migration(conn):
        _migrate_unresolved_snapshots(conn)
    if _equality_checks_needs_migration(conn):
        _migrate_equality_checks(conn)
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
    """Default ``repo_dir`` for ``detect_engine_build`` -- the engram-alpha
    checkout containing THIS module (``src/engram/health/series.py`` ->
    parents[3] == the repo root). Only used when a caller does not pass
    ``repo_dir`` explicitly."""
    return Path(__file__).resolve().parents[3]


def detect_engine_build(
    data_dir: str | Path | None = None,
    repo_dir: str | Path | None = None,
) -> str:
    """First available of: env ``ENGRAM_ENGINE_BUILD``; ``alpha_sha=`` from
    ``<data_dir>/.deployed-version`` (a ``key=value`` file, one entry per
    line); ``git rev-parse HEAD`` of ``repo_dir``; else the literal
    ``'unknown'``. Never raises."""
    import os

    env_val = os.environ.get("ENGRAM_ENGINE_BUILD")
    if env_val:
        return env_val

    if data_dir is not None:
        deployed = Path(data_dir) / ".deployed-version"
        try:
            for line in deployed.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "alpha_sha" and value.strip():
                    return value.strip()
        except OSError:
            pass

    effective_repo_dir = Path(repo_dir) if repo_dir is not None else _default_repo_dir()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(effective_repo_dir),
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except OSError:
        pass

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
    """
    conn = open_store(db_path)
    try:
        started_at = _utc_now_iso(now)
        build = engine_build if engine_build is not None else detect_engine_build(
            data_dir=None, repo_dir=repo_dir
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
        detail: dict[str, list] = {"old_format": [], "unreadable": [], "collisions": []}

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

            existing_decl = conn.execute(
                "SELECT signal_defs, curve_defs, config FROM declarations WHERE health_version = ?",
                (hv,),
            ).fetchone()
            if existing_decl is None:
                conn.execute(
                    "INSERT INTO declarations "
                    "(health_version, signal_defs, curve_defs, config, "
                    "first_seen_commit, first_seen_file, engine_build) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        hv,
                        signal_defs_json,
                        curve_defs_json,
                        config_json,
                        None,
                        f.name,
                        build,
                    ),
                )
                conn.commit()
            else:
                if existing_decl["signal_defs"] != signal_defs_json:
                    first_diff = "signal_defs"
                elif existing_decl["curve_defs"] != curve_defs_json:
                    first_diff = "curve_defs"
                elif existing_decl["config"] != config_json:
                    first_diff = "config"
                else:
                    first_diff = None
                if first_diff is not None:
                    raise SeriesIntegrityError(
                        f"declarations gate: health_version {hv!r} already has a "
                        f"stored declaration that disagrees with {f.name!r} in "
                        f"{first_diff!r} (D4.1: a version's declared signal_defs/"
                        "curve_defs/config must never differ across snapshots)."
                    )

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


def rebuild(
    snapshots_dir: str | Path,
    repo_dir: str | Path,
    db_path: str | Path,
    engine_build: str | None = None,
) -> dict:
    """Build a fresh store at ``<db_path>.rebuild`` via ``ingest``, then
    compare it against the existing ``db_path`` with an ORDERED DUMP
    COMPARE over ``l0_points``/``declarations``/``unresolved_snapshots``
    (ordered by primary key; run-metadata columns -- ``computed_at``,
    ``engine_build``, ``seen_at``, and every ``replay_runs`` row -- are
    excluded, see module-level ``_REBUILD_DIFF_EXCLUDE_COLUMNS``).

    Returns ``{identical, differences, rebuilt_path}``. If identical, the
    live store at ``db_path`` is atomically replaced (``os.replace``) with
    the freshly rebuilt one; if not, BOTH files are left in place and the
    differences are returned -- a disagreeing store is never replaced.

    TODO(#2575 PR B): this is the single-diff form (design doc section 2's
    "rebuild"). D4.2's two-diff form (content-changed under the stored
    ``engine_build`` vs build-changed under HEAD, requiring a checkout of
    the stored build's ``health/`` into a scratch tree) lands in PR B
    alongside the replay driver that makes ``engine_build`` a real,
    checkout-able sha for git_replay rows.
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
        build = engine_build if engine_build is not None else detect_engine_build(
            data_dir=None, repo_dir=repo_dir
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
        build = detect_engine_build(data_dir=None, repo_dir=repo_dir)

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


def status(db_path: str | Path) -> dict:
    """Counts per table, the last ``replay_runs`` row, the distinct
    ``health_major`` (from ``l0_points``) / ``health_version`` (from
    ``declarations``) values present in the store, and (PR B1 additions)
    the CURRENT declared-replay fingerprint, per-``(health_major,
    replay_decl_hash)`` ``git_replay`` row counts, and the latest equality
    verdict counts (one verdict per ``(commit_sha, health_major,
    replay_decl_hash)``, taking the row with the greatest ``checked_at`` --
    ``equality_checks`` is append-only, per that table's own migration
    docstring)."""
    conn = open_store(db_path)
    try:
        counts = {}
        for table in (
            "l0_points",
            "derived_points",
            "equality_checks",
            "replay_runs",
            "declarations",
            "unresolved_snapshots",
        ):
            counts[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

        last_run_row = conn.execute(
            "SELECT * FROM replay_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        last_run = dict(last_run_row) if last_run_row is not None else None

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
    finally:
        conn.close()

    return {
        "counts": counts,
        "last_replay_run": last_run,
        "health_majors": health_majors,
        "health_versions": health_versions,
        "current_replay_decl_hash": current_replay_decl_hash,
        "git_replay_counts": git_replay_counts,
        "latest_equality_verdicts": latest_equality_verdicts,
    }


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
        description="health-score series store: ingest / rebuild / status / backfill / verify (PR A + PR B1).",
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

    p_status = sub.add_parser("status")
    _add_common(p_status, needs_snapshots_and_repo=False)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        result = ingest(
            args.snapshots_dir, args.repo_dir, args.db, engine_build=args.engine_build
        )
    elif args.command == "rebuild":
        result = rebuild(
            args.snapshots_dir, args.repo_dir, args.db, engine_build=args.engine_build
        )
    elif args.command == "status":
        result = status(args.db)
    elif args.command == "backfill":
        result = backfill(
            args.repo_dir,
            args.db,
            health_major=args.health_major,
            engine_build=args.engine_build,
        )
    elif args.command == "verify":
        result = verify(args.repo_dir, args.db, health_major=args.health_major)
    else:  # pragma: no cover -- argparse `required=True` prevents this
        parser.error(f"unknown command: {args.command!r}")
        return 2

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
