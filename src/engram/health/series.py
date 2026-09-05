"""health.series -- the health-score series store: ingest + rebuild + status.

Design of record: ``docs/health-series-design.md`` (design v1 sections 0-10,
amended by deltas D1-D5; the deltas win over the sections they amend). This
module implements **PR A only** ("#2575 PR A") of the three-PR slicing named
in that document's "Slicing after the deltas" section:

  - **PR A (this module)** -- the store schema (all tables, including
    ``replay_decl_hash``, the ``unreplayable`` class shape, ``anchor_
    resolution``, ``declarations``, ``unresolved_snapshots``), ``ingest``,
    old-format refusal, anchor resolution (D1), a single-diff ``rebuild``,
    and the CLI skeleton.
  - **PR B (NOT this module)** -- the replay driver, ``MeasurementDef.
    replay``, ``replay_decl_hash`` computation from real declarations,
    ``backfill``/``verify``, and the two-diff form of ``rebuild`` (D4.2).
  - **PR C (NOT this module)** -- ``derived_points`` population, ``export``,
    and the lifecycle checkpoint hook.

What PR A explicitly does NOT do: no git-replay of ``knowledge.sql`` dumps,
no population of ``derived_points`` or ``equality_checks``, no lifecycle
wiring (nothing in ``engram_lifecycle.py`` or ``engram_snapshot_worker.py``
calls into this module). The tables for those later PRs are created here
(idempotently) so the schema is stable from PR A forward, but they are
never written to by this module -- see the ``TODO(#2575 PR B)`` /
``TODO(#2575 PR C)`` markers below for exactly where that work attaches.

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
    checked_at TEXT
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


def open_store(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the series store at ``db_path``, with all
    tables created idempotently. Plain rollback journal -- WAL is NOT
    wanted here (design instruction: the store is single-writer, offline).

    Because tables are created with ``CREATE TABLE IF NOT EXISTS``, an
    already-created store keeps whatever CHECK constraints it was created
    with -- a schema-level change to a CHECK (like widening ``unresolved_
    snapshots.reason`` to add ``commit_already_keyed``) does not reach an
    existing store on its own. This is the store's FIRST schema migration:
    after creating tables, detect the old constraint (``PRAGMA table_info``
    doesn't expose CHECKs, so the literal is read off ``sqlite_master.sql``)
    and, if found, recreate ``unresolved_snapshots`` preserving its rows.
    This is safe specifically because the table is DERIVED state -- ``
    rebuild`` reconstructs it from the snapshot files and the repo, so
    rewriting it in place never risks the source of truth."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(_SCHEMA)
    conn.commit()
    if _unresolved_snapshots_needs_migration(conn):
        _migrate_unresolved_snapshots(conn)
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
# status
# ---------------------------------------------------------------------------


def status(db_path: str | Path) -> dict:
    """Counts per table, the last ``replay_runs`` row, and the distinct
    ``health_major`` (from ``l0_points``) / ``health_version`` (from
    ``declarations``) values present in the store."""
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
    finally:
        conn.close()

    return {
        "counts": counts,
        "last_replay_run": last_run,
        "health_majors": health_majors,
        "health_versions": health_versions,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#
# Imports MAY NOT resolve when this file is run directly as a script
# (``python3 src/engram/health/series.py ...``) vs. imported as a package
# module (``from health import series``) -- a bare `from . import version`
# would fail under the script-execution form (no package context). PR A
# never needs health/version.py at all (it never computes a health_version
# string itself -- every health_version it touches comes verbatim off a
# snapshot file's own "health_version" key), so the CLI simply does not
# import it, sidestepping the dual-execution-mode import problem entirely.


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health.series",
        description="health-score series store: ingest / rebuild / status (PR A).",
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
    else:  # pragma: no cover -- argparse `required=True` prevents this
        parser.error(f"unknown command: {args.command!r}")
        return 2

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
