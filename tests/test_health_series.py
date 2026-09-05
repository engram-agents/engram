"""Tests for health/series.py -- the health-score series store, PR A scope
(engram-alpha#2575 PR A): schema, ``snapshot_files``, old-format refusal,
anchor resolution (D1), ``ingest`` idempotence, the declarations gate
(D4.1), ``rebuild`` (single-diff form), ``detect_engine_build``, the
never-imports-engram_core-at-top guard, and the CLI running as a script.

Design of record: ``docs/health-series-design.md`` (design v1 sections 0-10
plus deltas D1-D5; see that module's docstring for the PR A/B/C slicing).

Fixture strategy: each test builds a scratch ``~/.engram``-shaped git repo
in ``tmp_path`` (``git init``, ``user.email``/``user.name`` set via ``-c``,
some commits touching ``knowledge.sql``, some touching only
``history/x.md`` to be history-only commits) plus a ``health-snapshots/``
directory of hand-built v2-shaped snapshot JSON files. All git invocations
are ``subprocess.run([...])`` argument lists -- no ``shell=True``.

v2 snapshot fixture: built via ``build_snapshot(_make_conn(), "nap")``
(imported the same way ``tests/test_health_engine.py`` imports the engine
-- ``from health import engine as health_engine`` after conftest.py's
sys.path setup), then overridden per-test (``anchor``, ``turn``,
``raw_schema_version``, ...). See ``_build_v2_snapshot`` below.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import engine as health_engine  # noqa: E402
from health import measurements as health_measurements  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SERIES_PATH = REPO_ROOT / "src" / "engram" / "health" / "series.py"

from health import series  # noqa: E402


# ---------------------------------------------------------------------------
# git scratch-repo helpers
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env=full_env,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    _run_git(repo, "-c", "user.email=fairy@example.com", "-c", "user.name=fairy", "config", "user.email", "fairy@example.com")
    _run_git(repo, "config", "user.email", "fairy@example.com")
    _run_git(repo, "config", "user.name", "fairy")
    _run_git(repo, "config", "commit.gpgsign", "false")


def _commit(
    repo: Path,
    files: dict[str, str],
    message: str,
    committer_iso: str | None = None,
) -> str:
    """Write ``files`` (relative-path -> content) and commit. Returns the
    new commit sha. ``committer_iso`` (if given) sets GIT_COMMITTER_DATE
    (and GIT_AUTHOR_DATE, for determinism) so committer times are fully
    controllable in tests."""
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _run_git(repo, "add", "--", *files.keys())
    env = {}
    if committer_iso:
        env["GIT_COMMITTER_DATE"] = committer_iso
        env["GIT_AUTHOR_DATE"] = committer_iso
    _run_git(repo, "commit", "-q", "-m", message, env=env)
    sha = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    return sha


def _commit_knowledge(repo: Path, content: str, committer_iso: str | None = None) -> str:
    return _commit(repo, {"knowledge.sql": content}, "knowledge dump", committer_iso)


def _commit_history_only(repo: Path, content: str, committer_iso: str | None = None) -> str:
    return _commit(repo, {"history/x.md": content}, "history only", committer_iso)


def _committer_iso(dt: datetime) -> str:
    """git accepts ISO-8601 for GIT_COMMITTER_DATE/GIT_AUTHOR_DATE."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# v2 snapshot fixture
# ---------------------------------------------------------------------------

_NODES_DDL = """
CREATE TABLE nodes (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    evidence_id     TEXT,
    status          TEXT DEFAULT 'active',
    is_current      INTEGER DEFAULT 1,
    metadata        TEXT DEFAULT '{}',
    recall_summary  TEXT,
    recall_keywords TEXT,
    embedding       TEXT
);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    turn        INTEGER NOT NULL DEFAULT 0,
    action      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    details     TEXT DEFAULT '{}'
);

CREATE TABLE cascade_markers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id                 TEXT NOT NULL REFERENCES nodes(id),
    cascade_event           TEXT NOT NULL,
    root_node               TEXT NOT NULL,
    root_op                 TEXT NOT NULL CHECK (root_op IN ('retract', 'supersede')),
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved')),
    resolution_kind         TEXT,
    resolution_replacement_id TEXT,
    created_at              TEXT NOT NULL,
    resolved_at             TEXT,
    backfill_provenance     TEXT,
    backfill_anomaly        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(node_id, cascade_event)
);
"""


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_NODES_DDL)
    return conn


# ``_NODES_DDL`` without the ``cascade_markers`` table -- a stand-in for a
# ``knowledge.sql`` dump that predates it (design doc's own measured fact:
# 102 of 136 real commits raise `no such table: cascade_markers`).
_NODES_DDL_WITHOUT_CASCADE_MARKERS = """
CREATE TABLE nodes (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    evidence_id     TEXT,
    status          TEXT DEFAULT 'active',
    is_current      INTEGER DEFAULT 1,
    metadata        TEXT DEFAULT '{}',
    recall_summary  TEXT,
    recall_keywords TEXT,
    embedding       TEXT
);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    turn        INTEGER NOT NULL DEFAULT 0,
    action      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    details     TEXT DEFAULT '{}'
);
"""


def _make_conn_without_cascade_markers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_NODES_DDL_WITHOUT_CASCADE_MARKERS)
    return conn


def _insert(
    conn,
    id,
    type,
    created_at="t0",
    evidence_id=None,
    status="active",
    is_current=1,
    metadata=None,
    recall_summary=None,
    recall_keywords=None,
    embedding=None,
):
    """Mirrors tests/test_health_engine.py's own ``_insert`` helper exactly
    (same fixture column shape, same defaults) -- see that file's own
    ``_NODES_DDL``/``_insert`` for the precedent this copies."""
    conn.execute(
        "INSERT INTO nodes (id, type, created_at, evidence_id, status, is_current,"
        " metadata, recall_summary, recall_keywords, embedding)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
            type,
            created_at,
            evidence_id,
            status,
            is_current,
            json.dumps(metadata) if metadata is not None else "{}",
            recall_summary,
            json.dumps(recall_keywords) if recall_keywords is not None else None,
            embedding,
        ),
    )


def _build_v2_snapshot(**overrides) -> dict:
    snapshot = health_engine.build_snapshot(_make_conn(), "nap")
    snapshot.update(overrides)
    return snapshot


def _write_snapshot_file(
    snapshots_dir: Path, turn: int, mode: str, ts: str, snapshot: dict
) -> Path:
    name = f"{turn}-{mode}-{ts}.json"
    path = snapshots_dir / name
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


_OLD_FORMAT_SNAPSHOT = {
    "schema_version": "1",
    "turn": 12,
    "anchor": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "scores": {},
    "signals": {},
    "diagnostics": {},
    "config": {},
    "definitions": {},
    "engine_version": "0.3.0",
}

# ---------------------------------------------------------------------------
# 2. snapshot_files regex
# ---------------------------------------------------------------------------


def test_snapshot_files_regex_skips_dream_and_repair_record(tmp_path):
    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "health-snapshot-dream.json").write_text("{}", encoding="utf-8")
    (snapshots_dir / "health-repair-record.json").write_text("{}", encoding="utf-8")
    (snapshots_dir / "notes.json").write_text("{}", encoding="utf-8")
    (snapshots_dir / "12-nap-20260101T000000Z.json").write_text("{}", encoding="utf-8")
    (snapshots_dir / "13-post_dream-20260102T000000Z.json").write_text("{}", encoding="utf-8")

    files = series.snapshot_files(snapshots_dir)
    names = [f.name for f in files]
    assert names == [
        "12-nap-20260101T000000Z.json",
        "13-post_dream-20260102T000000Z.json",
    ]


# ---------------------------------------------------------------------------
# 3. old-format refusal
# ---------------------------------------------------------------------------


def test_old_format_snapshot_refused_and_counted(tmp_path, capsys):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()

    assert "measurements" not in _OLD_FORMAT_SNAPSHOT
    _write_snapshot_file(
        snapshots_dir, 12, "nap", "20260101T000000Z", _OLD_FORMAT_SNAPSHOT
    )

    db_path = tmp_path / "series.db"
    result = series.ingest(snapshots_dir, repo_dir, db_path)

    assert result["skipped_old_format"] == 1
    assert "12-nap-20260101T000000Z.json" in result["detail"]["old_format"]

    conn = series.open_store(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM l0_points").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM unresolved_snapshots").fetchone()["c"] == 0
    conn.close()

    captured = capsys.readouterr()
    assert "12-nap-20260101T000000Z.json" in captured.err


# ---------------------------------------------------------------------------
# 4. anchor resolution: child_of_anchor
# ---------------------------------------------------------------------------


def test_anchor_child_of_anchor_resolves_to_next_dump_commit(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    h = _commit_history_only(repo_dir, "h", _committer_iso(t0 + timedelta(minutes=1)))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=2)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()

    snap_h = _build_v2_snapshot(anchor=h, turn=1)
    _write_snapshot_file(snapshots_dir, 1, "nap", "20260901T120300Z", snap_h)

    db_path = tmp_path / "series.db"
    result = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result["commits_done"] == 1

    conn = series.open_store(db_path)
    row = conn.execute("SELECT * FROM l0_points").fetchone()
    assert row["commit_sha"] == k2
    assert row["anchor_pre_commit"] == h
    assert row["anchor_resolution"] == "child_of_anchor"
    conn.close()

    # Anchor = K1 also resolves to K2 -- the walk is on the knowledge.sql
    # path, so the intervening history-only commit H is skipped over.
    snapshots_dir2 = tmp_path / "health-snapshots-2"
    snapshots_dir2.mkdir()
    snap_k1 = _build_v2_snapshot(anchor=k1, turn=2)
    _write_snapshot_file(snapshots_dir2, 2, "nap", "20260901T120400Z", snap_k1)

    db_path2 = tmp_path / "series2.db"
    result2 = series.ingest(snapshots_dir2, repo_dir, db_path2)
    assert result2["commits_done"] == 1
    conn2 = series.open_store(db_path2)
    row2 = conn2.execute("SELECT * FROM l0_points").fetchone()
    assert row2["commit_sha"] == k2
    conn2.close()


# ---------------------------------------------------------------------------
# 5. shared anchor -> ambiguous
# ---------------------------------------------------------------------------


def test_anchor_shared_by_two_snapshots_is_ambiguous_never_keyed(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=1)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap_a = _build_v2_snapshot(anchor=k1, turn=1)
    snap_b = _build_v2_snapshot(anchor=k1, turn=2)
    _write_snapshot_file(snapshots_dir, 1, "nap", "20260901T120100Z", snap_a)
    _write_snapshot_file(snapshots_dir, 2, "nap", "20260901T120200Z", snap_b)

    db_path = tmp_path / "series.db"
    result = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result["commits_done"] == 0

    conn = series.open_store(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM l0_points").fetchone()["c"] == 0
    unresolved = conn.execute(
        "SELECT snapshot_file, reason FROM unresolved_snapshots ORDER BY snapshot_file"
    ).fetchall()
    assert len(unresolved) == 2
    assert all(r["reason"] == "shared_anchor" for r in unresolved)
    conn.close()


# ---------------------------------------------------------------------------
# 6. no child yet -> unresolved, then resolves on re-ingest
# ---------------------------------------------------------------------------


def test_anchor_without_child_is_unresolved_then_resolves_on_reingest(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    head = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap = _build_v2_snapshot(anchor=head, turn=1)
    _write_snapshot_file(snapshots_dir, 1, "nap", "20260901T120100Z", snap)

    db_path = tmp_path / "series.db"
    result = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result["commits_done"] == 0
    assert result["unresolved"] == 1

    conn = series.open_store(db_path)
    row = conn.execute("SELECT reason FROM unresolved_snapshots").fetchone()
    assert row["reason"] == "no_child_commit"
    conn.close()

    k3 = _commit_knowledge(repo_dir, "k3", _committer_iso(t0 + timedelta(minutes=5)))

    result2 = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result2["commits_done"] == 1

    conn2 = series.open_store(db_path)
    assert conn2.execute("SELECT COUNT(*) c FROM unresolved_snapshots").fetchone()["c"] == 0
    row2 = conn2.execute("SELECT commit_sha FROM l0_points").fetchone()
    assert row2["commit_sha"] == k3
    conn2.close()


# ---------------------------------------------------------------------------
# 7. anchor not in repo
# ---------------------------------------------------------------------------


def test_anchor_not_in_repo_is_unresolved(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    _commit_knowledge(repo_dir, "k1")

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap = _build_v2_snapshot(anchor="deadbeef" * 5, turn=1)
    _write_snapshot_file(snapshots_dir, 1, "nap", "20260901T120100Z", snap)

    db_path = tmp_path / "series.db"
    result = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result["unresolved"] == 1

    conn = series.open_store(db_path)
    row = conn.execute("SELECT reason FROM unresolved_snapshots").fetchone()
    assert row["reason"] == "anchor_not_in_repo"
    conn.close()


# ---------------------------------------------------------------------------
# 8. by_time only when anchor absent
# ---------------------------------------------------------------------------


def test_by_time_only_when_anchor_absent(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=10)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()

    # 5s before K2's committer time -> by_time, K2.
    snap_ok = _build_v2_snapshot(anchor=None, turn=1)
    ts_ok = (t0 + timedelta(minutes=10) - timedelta(seconds=5)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts_ok, snap_ok)

    db_path = tmp_path / "series.db"
    result = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result["commits_done"] == 1
    conn = series.open_store(db_path)
    row = conn.execute("SELECT commit_sha, anchor_resolution FROM l0_points").fetchone()
    assert row["commit_sha"] == k2
    assert row["anchor_resolution"] == "by_time"
    conn.close()

    # 30 min before any commit -> by_time_out_of_bound.
    snapshots_dir2 = tmp_path / "health-snapshots-2"
    snapshots_dir2.mkdir()
    snap_bad = _build_v2_snapshot(anchor=None, turn=2)
    ts_bad = (t0 - timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir2, 2, "nap", ts_bad, snap_bad)

    db_path2 = tmp_path / "series2.db"
    result2 = series.ingest(snapshots_dir2, repo_dir, db_path2)
    assert result2["unresolved"] == 1
    conn2 = series.open_store(db_path2)
    row2 = conn2.execute("SELECT reason FROM unresolved_snapshots").fetchone()
    assert row2["reason"] == "by_time_out_of_bound"
    conn2.close()


# ---------------------------------------------------------------------------
# 9. idempotence
# ---------------------------------------------------------------------------


def test_ingest_is_idempotent(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=1)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap = _build_v2_snapshot(anchor=None, turn=1)
    ts = (t0 + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts, snap)

    db_path = tmp_path / "series.db"
    result1 = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result1["commits_done"] == 1

    conn = series.open_store(db_path)
    row_count = conn.execute("SELECT COUNT(*) c FROM l0_points").fetchone()["c"]
    conn.close()
    assert row_count == 1

    result2 = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result2["commits_done"] == 0
    assert result2["already_present"] == row_count

    conn2 = series.open_store(db_path)
    assert conn2.execute("SELECT COUNT(*) c FROM l0_points").fetchone()["c"] == row_count
    conn2.close()


# ---------------------------------------------------------------------------
# 10. stored row content matches the snapshot
# ---------------------------------------------------------------------------


def test_stored_row_content_matches_snapshot(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap = _build_v2_snapshot(anchor=None, turn=42)
    ts = t0.strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 42, "nap", ts, snap)

    db_path = tmp_path / "series.db"
    series.ingest(snapshots_dir, repo_dir, db_path)

    conn = series.open_store(db_path)
    row = conn.execute("SELECT * FROM l0_points").fetchone()
    assert json.loads(row["measurements"]) == snap["measurements"]
    assert row["unreplayable"] == "{}"
    assert row["replay_decl_hash"] == ""
    assert row["health_major"] == snap["raw_schema_version"]
    assert row["turn"] == 42
    assert row["source"] == "stored"
    conn.close()


# ---------------------------------------------------------------------------
# 11. declarations gate
# ---------------------------------------------------------------------------


def test_declarations_gate_rejects_conflicting_blobs_for_same_version(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=1)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()

    snap1 = _build_v2_snapshot(anchor=None, turn=1)
    ts1 = t0.strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts1, snap1)

    snap2 = _build_v2_snapshot(anchor=None, turn=2)
    # Mutate one key inside signal_defs so the declared blob genuinely
    # differs while health_version stays identical.
    mutated_signal_defs = json.loads(json.dumps(snap2["signal_defs"]))
    first_key = next(iter(mutated_signal_defs))
    mutated_signal_defs[first_key] = {"__mutated__": True}
    snap2["signal_defs"] = mutated_signal_defs
    ts2 = (t0 + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 2, "nap", ts2, snap2)

    db_path = tmp_path / "series.db"
    with pytest.raises(series.SeriesIntegrityError) as excinfo:
        series.ingest(snapshots_dir, repo_dir, db_path)

    msg = str(excinfo.value)
    assert snap1["health_version"] in msg
    assert f"2-nap-{ts2}.json" in msg

    conn = series.open_store(db_path)
    # The first file's row IS present -- the run aborted at the second.
    assert conn.execute("SELECT COUNT(*) c FROM l0_points").fetchone()["c"] == 1
    conn.close()


def test_declarations_row_written_once_per_version(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=1)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()

    snap1 = _build_v2_snapshot(anchor=None, turn=1)
    ts1 = t0.strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts1, snap1)

    snap2 = _build_v2_snapshot(anchor=None, turn=2)
    ts2 = (t0 + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 2, "nap", ts2, snap2)

    db_path = tmp_path / "series.db"
    series.ingest(snapshots_dir, repo_dir, db_path)

    conn = series.open_store(db_path)
    rows = conn.execute("SELECT * FROM declarations").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen_file"] == f"1-nap-{ts1}.json"
    conn.close()


# ---------------------------------------------------------------------------
# 12. rebuild reproduces
# ---------------------------------------------------------------------------


def test_rebuild_reproduces_incremental_store(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=1)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap1 = _build_v2_snapshot(anchor=None, turn=1)
    ts1 = (t0 + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts1, snap1)

    db_path = tmp_path / "series.db"
    series.ingest(snapshots_dir, repo_dir, db_path)

    k3 = _commit_knowledge(repo_dir, "k3", _committer_iso(t0 + timedelta(minutes=2)))
    snap2 = _build_v2_snapshot(anchor=None, turn=2)
    ts2 = (t0 + timedelta(minutes=2)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 2, "nap", ts2, snap2)
    series.ingest(snapshots_dir, repo_dir, db_path)

    result = series.rebuild(snapshots_dir, repo_dir, db_path)
    assert result["identical"] is True
    assert not Path(result["rebuilt_path"]).exists()  # replaced db_path
    assert db_path.exists()

    # Corrupt one measurements value in the live store by hand.
    conn = series.open_store(db_path)
    row = conn.execute("SELECT rowid, measurements FROM l0_points LIMIT 1").fetchone()
    measurements = json.loads(row["measurements"])
    a_key = next(iter(measurements))
    measurements[a_key] = "CORRUPTED"
    conn.execute(
        "UPDATE l0_points SET measurements = ? WHERE rowid = ?",
        (json.dumps(measurements), row["rowid"]),
    )
    conn.commit()
    conn.close()

    result2 = series.rebuild(snapshots_dir, repo_dir, db_path)
    assert result2["identical"] is False
    tables = {d["table"] for d in result2["differences"]}
    fields = {d["field"] for d in result2["differences"]}
    assert "l0_points" in tables
    assert "measurements" in fields
    # both files left in place
    assert db_path.exists()
    assert Path(result2["rebuilt_path"]).exists()


# ---------------------------------------------------------------------------
# 13. detect_engine_build rule order (round 3, #2600 -- see docstring below
# for what round-2 tests this folds)
# ---------------------------------------------------------------------------


def test_detect_engine_build_rule_order(tmp_path, monkeypatch, capsys):
    """End-to-end exercise of ``detect_engine_build``'s ONE rule (round 3,
    #2600): env > plugin-tree marker > code-checkout git HEAD > 'unknown'.
    Folds round 2's five ``module_path``-based tests
    (``test_engine_build_nested_plugin_tree_returns_alpha_sha_not_data_head``,
    ``test_engine_build_nested_plugin_tree_without_alpha_sha_is_unknown``,
    ``test_engine_build_code_checkout_returns_its_head_and_dirty_suffix``,
    ``test_engine_build_data_repo_without_src_is_unknown``,
    ``test_env_override_wins``) into one, plus the ordering case round 1's
    ``test_detect_engine_build_order`` never covered (a plugin-tree marker
    nested inside a git worktree that ALSO qualifies as a code checkout --
    proving the marker wins even when the git rule would otherwise fire)."""

    # -- A. plugin tree (nested inside a DATA repo -- no src/) resolves to
    # alpha_sha, never the data repo's own HEAD.
    data_repo = tmp_path / "data-repo"
    _init_repo(data_repo)
    h_data = _commit_knowledge(data_repo, "k1")

    plugin_dir = data_repo / "marketplace" / "plugins" / "engram"
    health_dir = plugin_dir / "health"
    health_dir.mkdir(parents=True)
    (plugin_dir / ".engram-build-manifest.json").write_text("{}", encoding="utf-8")
    (data_repo / ".deployed-version").write_text(
        "alpha_sha=abc1234\n", encoding="utf-8"
    )
    module_path = health_dir / "series.py"

    result = series.detect_engine_build(module_path=module_path)
    assert result == "abc1234"
    assert result != h_data

    # -- B. env wins over that same plugin tree.
    monkeypatch.setenv("ENGRAM_ENGINE_BUILD", "env-build")
    assert series.detect_engine_build(module_path=module_path) == "env-build"
    monkeypatch.delenv("ENGRAM_ENGINE_BUILD")

    # -- C. plugin tree marker present but no alpha_sha= line -> 'unknown'
    # plus a stderr line naming the data dir, no fallback to git.
    data_repo2 = tmp_path / "data-repo-no-alpha"
    _init_repo(data_repo2)
    _commit_knowledge(data_repo2, "k1")
    plugin_dir2 = data_repo2 / "marketplace" / "plugins" / "engram"
    health_dir2 = plugin_dir2 / "health"
    health_dir2.mkdir(parents=True)
    (plugin_dir2 / ".engram-build-manifest.json").write_text("{}", encoding="utf-8")
    # deliberately no .deployed-version at all

    module_path2 = health_dir2 / "series.py"
    result2 = series.detect_engine_build(module_path=module_path2)
    assert result2 == "unknown"
    captured = capsys.readouterr()
    assert "alpha_sha" in captured.err
    assert str(data_repo2) in captured.err

    # -- D. plugin tree marker wins over being inside a git worktree that
    # ALSO qualifies as a code checkout (carries src/engram/health/
    # measurements.py). This is the case round 1's order test never
    # covered: without the marker-first rule, a git-toplevel-first
    # resolver would find this repo's own HEAD via the (also valid) code
    # checkout rule instead of the deployed alpha_sha.
    hybrid_repo = tmp_path / "hybrid-code-and-plugin-tree"
    _init_repo(hybrid_repo)
    hybrid_sha = _commit(
        hybrid_repo,
        {"src/engram/health/measurements.py": "# stub\n"},
        "add measurements",
    )
    hybrid_plugin_dir = hybrid_repo / "marketplace" / "plugins" / "engram"
    hybrid_health_dir = hybrid_plugin_dir / "health"
    hybrid_health_dir.mkdir(parents=True)
    (hybrid_plugin_dir / ".engram-build-manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    (hybrid_repo / ".deployed-version").write_text(
        "alpha_sha=hybrid-alpha-sha\n", encoding="utf-8"
    )
    hybrid_module_path = hybrid_health_dir / "series.py"

    hybrid_result = series.detect_engine_build(module_path=hybrid_module_path)
    assert hybrid_result == "hybrid-alpha-sha"
    assert hybrid_result != hybrid_sha

    # -- E. bare code checkout (no plugin-tree marker) -> its own git HEAD;
    # a dirty change under src/engram/health adds the '-dirty' suffix.
    code_repo = tmp_path / "code-repo"
    _init_repo(code_repo)
    sha = _commit(
        code_repo,
        {"src/engram/health/measurements.py": "# stub\n"},
        "add measurements",
    )
    code_module_path = code_repo / "src" / "engram" / "health" / "series.py"
    assert series.detect_engine_build(module_path=code_module_path) == sha

    (code_repo / "src" / "engram" / "health" / "measurements.py").write_text(
        "# stub changed\n", encoding="utf-8"
    )
    assert (
        series.detect_engine_build(module_path=code_module_path) == f"{sha}-dirty"
    )

    # -- F. git repo without src/engram/health/measurements.py (the DATA
    # repo's own shape) -> 'unknown', never that repo's own HEAD.
    nested = data_repo / "some" / "nested"
    nested.mkdir(parents=True)
    no_src_module_path = nested / "module.py"

    result_f = series.detect_engine_build(module_path=no_src_module_path)
    assert result_f == "unknown"
    assert result_f != h_data
    captured_f = capsys.readouterr()
    assert captured_f.err.strip() != ""

    # -- G. nothing at all (no env, no plugin marker, not even a git repo)
    # -> 'unknown'.
    empty_dir = tmp_path / "not-a-repo"
    empty_dir.mkdir()
    assert (
        series.detect_engine_build(module_path=empty_dir / "module.py") == "unknown"
    )

    # -- H. env wins even with no plugin tree and no git repo in play.
    monkeypatch.setenv("ENGRAM_ENGINE_BUILD", "env-build-2")
    try:
        assert (
            series.detect_engine_build(module_path=empty_dir / "module.py")
            == "env-build-2"
        )
    finally:
        monkeypatch.delenv("ENGRAM_ENGINE_BUILD", raising=False)


# ---------------------------------------------------------------------------
# 14. no top-level engram_core import
# ---------------------------------------------------------------------------


def test_module_imports_nothing_from_engram_core_at_import():
    source = SERIES_PATH.read_text(encoding="utf-8")
    import re as _re

    assert not _re.search(r"^\s*import engram_core\b", source, _re.MULTILINE)
    assert not _re.search(r"^\s*from engram_core\b", source, _re.MULTILINE)

    # Also exercise the real property: importing (a fresh copy of) the
    # module must not touch engram_core, even if engram_core itself would
    # raise on attribute access.
    import importlib
    import sys as _sys
    import types

    sentinel = types.ModuleType("engram_core")

    def _boom(*_a, **_kw):
        raise AssertionError("series.py touched engram_core at import time")

    sentinel.__getattr__ = _boom  # type: ignore[attr-defined]

    previous = _sys.modules.pop("engram_core", None)
    previous_series = _sys.modules.pop("health.series", None)
    _sys.modules["engram_core"] = sentinel
    try:
        importlib.import_module("health.series")
    finally:
        del _sys.modules["engram_core"]
        if previous is not None:
            _sys.modules["engram_core"] = previous
        if previous_series is not None:
            _sys.modules["health.series"] = previous_series


# ---------------------------------------------------------------------------
# 15. CLI runs as a script
# ---------------------------------------------------------------------------


def test_cli_status_runs_as_script(tmp_path):
    tmp_db = tmp_path / "cli-status.db"
    result = subprocess.run(
        [sys.executable, str(SERIES_PATH), "status", "--db", str(tmp_db)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "counts" in payload
    assert payload["counts"]["l0_points"] == 0


# ---------------------------------------------------------------------------
# 16. two files resolving to one commit -- collision, not masked
# ---------------------------------------------------------------------------


def test_two_files_resolving_to_one_commit_is_recorded_not_masked(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    h = _commit_history_only(repo_dir, "h", _committer_iso(t0 + timedelta(minutes=1)))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=2)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()

    # Two DIFFERENT anchors (K1 and H), both preceding the same next
    # knowledge.sql commit K2 -- anchor_claims does not flag either as
    # shared (each anchor is carried by exactly one file), yet both
    # resolve to K2 via child_of_anchor.
    snap_k1 = _build_v2_snapshot(anchor=k1, turn=1)
    ts1 = "20260901T120100Z"
    _write_snapshot_file(snapshots_dir, 1, "nap", ts1, snap_k1)

    snap_h = _build_v2_snapshot(anchor=h, turn=2)
    ts2 = "20260901T120200Z"
    _write_snapshot_file(snapshots_dir, 2, "nap", ts2, snap_h)

    # Files are processed in timestamp order -- assert that explicitly
    # before ingesting, since which file "wins" the key depends on it.
    files = series.snapshot_files(snapshots_dir)
    assert [f.name for f in files] == [
        f"1-nap-{ts1}.json",
        f"2-nap-{ts2}.json",
    ]

    db_path = tmp_path / "series.db"
    result = series.ingest(snapshots_dir, repo_dir, db_path)

    assert result["already_present"] == 0
    assert result["collided"] == 1

    conn = series.open_store(db_path)
    rows = conn.execute("SELECT * FROM l0_points WHERE commit_sha = ?", (k2,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["snapshot_file"] == f"1-nap-{ts1}.json"

    unresolved = conn.execute(
        "SELECT snapshot_file, reason FROM unresolved_snapshots"
    ).fetchall()
    assert len(unresolved) == 1
    assert unresolved[0]["snapshot_file"] == f"2-nap-{ts2}.json"
    assert unresolved[0]["reason"] == "commit_already_keyed"
    conn.close()

    collisions = result["detail"]["collisions"]
    assert len(collisions) == 1
    assert collisions[0]["file"] == f"2-nap-{ts2}.json"
    assert collisions[0]["commit_sha"] == k2
    assert collisions[0]["keyed_by"] == f"1-nap-{ts1}.json"


# ---------------------------------------------------------------------------
# 17. re-ingesting the SAME file is already_present, never a collision
# ---------------------------------------------------------------------------


def test_reingest_same_file_is_already_present_not_collision(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _commit_knowledge(repo_dir, "k1", _committer_iso(t0))
    k2 = _commit_knowledge(repo_dir, "k2", _committer_iso(t0 + timedelta(minutes=1)))

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap = _build_v2_snapshot(anchor=None, turn=1)
    ts = (t0 + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts, snap)

    db_path = tmp_path / "series.db"
    result1 = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result1["commits_done"] == 1
    assert result1["collided"] == 0

    result2 = series.ingest(snapshots_dir, repo_dir, db_path)
    assert result2["already_present"] == 1
    assert result2["collided"] == 0
    assert result2["detail"]["collisions"] == []

    conn = series.open_store(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM unresolved_snapshots").fetchone()["c"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# 18. open_store migrates a store created with the old reason CHECK
# ---------------------------------------------------------------------------

_OLD_UNRESOLVED_SNAPSHOTS_DDL = """
CREATE TABLE unresolved_snapshots (
    snapshot_file TEXT PRIMARY KEY,
    anchor TEXT NULL,
    reason TEXT CHECK(reason IN ('no_child_commit','shared_anchor','by_time_out_of_bound','anchor_not_in_repo')),
    seen_at TEXT
);
"""


def test_open_store_migrates_old_reason_check(tmp_path):
    import sqlite3

    db_path = tmp_path / "old-series.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_OLD_UNRESOLVED_SNAPSHOTS_DDL)
    conn.execute(
        "INSERT INTO unresolved_snapshots (snapshot_file, anchor, reason, seen_at) "
        "VALUES (?, ?, ?, ?)",
        ("1-nap-20260901T120000Z.json", "deadbeef" * 5, "no_child_commit", "2026-09-01T12:00:00Z"),
    )
    conn.commit()
    conn.close()

    reopened = series.open_store(db_path)
    row = reopened.execute(
        "SELECT * FROM unresolved_snapshots WHERE snapshot_file = ?",
        ("1-nap-20260901T120000Z.json",),
    ).fetchone()
    assert row is not None
    assert row["reason"] == "no_child_commit"
    assert row["anchor"] == "deadbeef" * 5
    assert row["seen_at"] == "2026-09-01T12:00:00Z"

    # The migrated constraint now accepts the new reason.
    reopened.execute(
        "INSERT INTO unresolved_snapshots (snapshot_file, anchor, reason, seen_at) "
        "VALUES (?, ?, ?, ?)",
        ("2-nap-20260901T120100Z.json", None, "commit_already_keyed", "2026-09-01T12:01:00Z"),
    )
    reopened.commit()
    row2 = reopened.execute(
        "SELECT reason FROM unresolved_snapshots WHERE snapshot_file = ?",
        ("2-nap-20260901T120100Z.json",),
    ).fetchone()
    assert row2["reason"] == "commit_already_keyed"
    reopened.close()


# ===========================================================================
# #2575 PR B1 -- replay driver, backfill, verify, the equality control
# ===========================================================================

# ---------------------------------------------------------------------------
# 19. equality control: agrees, then a named mutation disagrees (D3, test 1)
# ---------------------------------------------------------------------------


def test_equality_control_agrees_then_named_mutation_disagrees(tmp_path):
    """Design test 1 (D3): the equality control between a ``stored`` row
    and a ``git_replay`` of the SAME commit must AGREE when the graph is
    unmutated, and must DISAGREE -- naming EXACTLY the one measurement whose
    population the mutation touches -- when a single claim-bearing node is
    retracted in a second commit's dump (with the stored snapshot pinned to
    the UNMUTATED measurements, per the spec's own construction).

    Mutation choice, DERIVED from measurements.py's own compute_fns (read
    at test-writing time, not discovered by running this test once) -- not
    just "any claim-bearing node," but one built to be invisible to every
    OTHER registered measurement:

    - A NEW node ``t1`` of type ``"theory"`` -- claim-bearing per
      ``engram_core.CLAIM_BEARING_TYPES`` (so retracting it CAN move
      ``claim_bearing_current``), but neither ``"derivation"`` nor
      ``"observation_factual"``/``"observation_predictive"`` (so it cannot
      move ``derivations_current`` or ``observations_current``).
    - Given a real, non-null, non-whitespace ``recall_summary`` and a real,
      non-empty ``recall_keywords`` list -- so it was NEVER counted by
      ``claim_bearing_without_recall_summary``/``_keywords`` to begin with,
      and retracting it cannot move either (removing an item that was never
      in the numerator changes nothing).
    - Given NO cascade-marker row of any kind -- so
      ``claim_bearing_pending_retract_marker`` cannot move.
    - Given NO edges to any other node -- it is its own connected component
      of size 1. The fixture separately connects a derivation ``d1`` and an
      observation ``o1`` (also claim-bearing) with one edge, forming a
      component of size 2 -- the LARGEST claim-bearing component. Since
      ``t1``'s own component (size 1) was never the largest, removing it
      cannot move ``claim_bearing_giant_component_size`` either.
    - ``evidence_current``/``evidence_uncited``/``observations_without_
      evidence_id``/``contradictions_current``/``contradictions_open``/
      ``derivations_pending_supersede_marker`` are all untouched by
      anything to do with ``t1`` -- they range over evidence/contradiction/
      derivation-marker populations ``t1`` was never part of.
    - ``claim_bearing_null_embedding`` (``replay="stripped"``) and the two
      embedder booleans (``replay="environment"``) are excluded from the
      equality comparison entirely (``equality_check`` compares only
      ``replay == "graph"`` names) regardless of ``t1``.
    - ``claim_bearing_types_resolved`` is a resolved CODE CONSTANT, never a
      function of node data -- unaffected by any node mutation.

    The only measurement whose population changes AT ALL when ``t1``'s
    ``is_current`` flips ``1 -> 0`` is ``claim_bearing_current`` (its
    predicate is exactly ``is_current = 1 AND type IN CLAIM_BEARING_
    TYPES``) -- so it is the ONLY name that can appear in the diff, by
    construction from the definitions above, not by observing one test
    run."""
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)

    conn = _make_conn()
    _insert(conn, "d1", "derivation", recall_summary="d1 summary", recall_keywords=["k"])
    _insert(
        conn, "o1", "observation_factual", evidence_id="e1",
        recall_summary="o1 summary", recall_keywords=["k"],
    )
    _insert(conn, "e1", "evidence")
    _insert(conn, "t1", "theory", recall_summary="t1 summary", recall_keywords=["k"])
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        ("d1", "o1", "supports", "t0"),
    )
    conn.commit()
    unmutated_sql = "\n".join(conn.iterdump())

    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, unmutated_sql, _committer_iso(t0))

    snap1 = _build_v2_snapshot(anchor=None, turn=1)
    # _build_v2_snapshot builds its measurements off an EMPTY _make_conn()
    # -- overwrite with the real, populated fixture graph's own values.
    snap1["measurements"] = health_measurements.compute_all_measurements(conn)

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    ts1 = t0.strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts1, snap1)

    db_path = tmp_path / "series.db"
    ingest1 = series.ingest(snapshots_dir, repo_dir, db_path)
    assert ingest1["commits_done"] == 1

    backfill1 = series.backfill(repo_dir, db_path)
    assert backfill1["commits_done"] == 1

    store = series.open_store(db_path)
    row = store.execute(
        "SELECT verdict, diff FROM equality_checks WHERE commit_sha = ? "
        "ORDER BY checked_at DESC LIMIT 1",
        (k1,),
    ).fetchone()
    assert row is not None
    assert row["verdict"] == "agree"
    store.close()

    # -- second commit: retract t1 --
    conn.execute("UPDATE nodes SET is_current = 0 WHERE id = 't1'")
    conn.commit()
    mutated_sql = "\n".join(conn.iterdump())
    k2 = _commit_knowledge(repo_dir, mutated_sql, _committer_iso(t0 + timedelta(minutes=1)))

    snap2 = dict(snap1)
    snap2["measurements"] = dict(snap1["measurements"])  # the UNMUTATED values
    snap2["anchor"] = k1  # resolves (child_of_anchor) to k2
    snap2["turn"] = 2
    ts2 = (t0 + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 2, "nap", ts2, snap2)

    ingest2 = series.ingest(snapshots_dir, repo_dir, db_path)
    assert ingest2["commits_done"] == 1

    backfill2 = series.backfill(repo_dir, db_path)
    assert backfill2["commits_done"] == 1

    store2 = series.open_store(db_path)
    row2 = store2.execute(
        "SELECT verdict, diff FROM equality_checks WHERE commit_sha = ? "
        "ORDER BY checked_at DESC LIMIT 1",
        (k2,),
    ).fetchone()
    assert row2 is not None
    assert row2["verdict"] == "disagree"
    diff = json.loads(row2["diff"])
    assert diff["names"] == ["claim_bearing_current"]
    stored_val = snap1["measurements"]["claim_bearing_current"]
    assert diff["stored"] == {"claim_bearing_current": stored_val}
    assert diff["replay"] == {"claim_bearing_current": stored_val - 1}
    store2.close()


# ---------------------------------------------------------------------------
# 20. false-positive control (D3)
# ---------------------------------------------------------------------------


def test_equality_control_false_positive_control(tmp_path):
    """D3's required false-positive control: mutate a column NO registered
    ``MeasurementDef``'s ``compute_fn`` ranges over, and assert the
    equality control still reports "agree" -- a control that flags every
    byte-level change (not just the ones that matter to a measurement's own
    definition) is not a control at all, it's a checksum.

    Column + grep, named per the spec's own requirement: this fixture's
    ``nodes`` table (mirroring test_health_engine.py's/test_health_
    measurements.py's own DDL) has nine columns -- id, type, created_at,
    evidence_id, status, is_current, metadata, recall_summary,
    recall_keywords, embedding. ``grep -n "metadata\\|created_at"
    src/engram/health/measurements.py`` (run at test-writing time) matches
    NOTHING in that file -- every OTHER column (type/is_current/
    evidence_id/status/recall_summary/recall_keywords/embedding) IS
    referenced by at least one compute_fn (see the other tests in this
    file and in test_health_measurements.py), leaving exactly ``metadata``
    and ``created_at`` unreferenced. This test mutates ``metadata``. (The
    spec's own suggested candidate, ``nodes.recall_count``, is NOT a column
    in this fixture's DDL at all -- there is nothing named ``recall_count``
    to mutate here, so ``metadata`` is used instead.)"""
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)

    conn = _make_conn()
    _insert(conn, "d1", "derivation", metadata={"note": "original"})
    conn.commit()
    unmutated_sql = "\n".join(conn.iterdump())

    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, unmutated_sql, _committer_iso(t0))

    snap1 = _build_v2_snapshot(anchor=None, turn=1)
    snap1["measurements"] = health_measurements.compute_all_measurements(conn)

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    ts1 = t0.strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts1, snap1)

    db_path = tmp_path / "series.db"
    series.ingest(snapshots_dir, repo_dir, db_path)
    series.backfill(repo_dir, db_path)

    conn.execute(
        "UPDATE nodes SET metadata = ? WHERE id = 'd1'",
        (json.dumps({"note": "mutated -- no compute_fn reads this column"}),),
    )
    conn.commit()
    mutated_sql = "\n".join(conn.iterdump())
    k2 = _commit_knowledge(repo_dir, mutated_sql, _committer_iso(t0 + timedelta(minutes=1)))

    snap2 = dict(snap1)
    snap2["measurements"] = dict(snap1["measurements"])
    snap2["anchor"] = k1
    snap2["turn"] = 2
    ts2 = (t0 + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 2, "nap", ts2, snap2)

    series.ingest(snapshots_dir, repo_dir, db_path)
    series.backfill(repo_dir, db_path)

    store = series.open_store(db_path)
    row = store.execute(
        "SELECT verdict, diff FROM equality_checks WHERE commit_sha = ? "
        "ORDER BY checked_at DESC LIMIT 1",
        (k2,),
    ).fetchone()
    assert row["verdict"] == "agree"
    assert json.loads(row["diff"]) == {}
    store.close()


# ---------------------------------------------------------------------------
# 21. schema-missing -> null with discovered reason, never 0 (design test 2)
# ---------------------------------------------------------------------------


def test_schema_missing_yields_null_with_discovered_reason_never_zero(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)

    conn = _make_conn()
    _insert(conn, "d1", "derivation")
    conn.execute(
        "INSERT INTO cascade_markers "
        "(node_id, cascade_event, root_node, root_op, status, created_at) "
        "VALUES ('d1', 'ev1', 'd1', 'supersede', 'pending', 't0')"
    )
    _insert(conn, "o1", "observation_factual", evidence_id="e1")
    _insert(conn, "e1", "evidence")
    conn.execute(
        "INSERT INTO cascade_markers "
        "(node_id, cascade_event, root_node, root_op, status, created_at) "
        "VALUES ('o1', 'ev2', 'o1', 'retract', 'pending', 't0')"
    )
    conn.commit()
    full_sql = "\n".join(conn.iterdump())

    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k_full = _commit_knowledge(repo_dir, full_sql, _committer_iso(t0))

    conn_no_markers = _make_conn_without_cascade_markers()
    _insert(conn_no_markers, "d1", "derivation")
    _insert(conn_no_markers, "o1", "observation_factual", evidence_id="e1")
    _insert(conn_no_markers, "e1", "evidence")
    conn_no_markers.commit()
    no_markers_sql = "\n".join(conn_no_markers.iterdump())

    k_no_markers = _commit_knowledge(
        repo_dir, no_markers_sql, _committer_iso(t0 + timedelta(minutes=1))
    )

    full_measurements, full_unreplayable = series.replay_commit(repo_dir, k_full)
    # The always-declared unreplayables (stripped/environment) are present
    # on every replay regardless of schema -- only the two schema-dependent
    # (discovered) ones are absent here, since cascade_markers exists.
    assert {n for n, info in full_unreplayable.items() if info["class"] == "discovered"} == set()
    assert full_measurements["derivations_pending_supersede_marker"] == 1
    assert full_measurements["claim_bearing_pending_retract_marker"] == 1

    dropped_measurements, dropped_unreplayable = series.replay_commit(
        repo_dir, k_no_markers
    )
    discovered_names = {
        n for n, info in dropped_unreplayable.items() if info["class"] == "discovered"
    }
    assert discovered_names == {
        "derivations_pending_supersede_marker",
        "claim_bearing_pending_retract_marker",
    }
    for name in discovered_names:
        info = dropped_unreplayable[name]
        assert info["reason"].startswith("schema:")
        assert info["class"] == "discovered"
        assert dropped_measurements[name] is None

    for name, value in dropped_measurements.items():
        if name in dropped_unreplayable or name == "raw_schema_version":
            continue
        assert value == full_measurements[name], (
            f"{name} differs between the full dump and the schema-missing "
            "dump even though nothing outside cascade_markers changed"
        )
    # never silently 0 where the un-dropped replay had a real non-zero:
    assert full_measurements["derivations_pending_supersede_marker"] != 0
    assert full_measurements["claim_bearing_pending_retract_marker"] != 0
    assert dropped_measurements["derivations_pending_supersede_marker"] is None
    assert dropped_measurements["claim_bearing_pending_retract_marker"] is None


# ---------------------------------------------------------------------------
# 22. declared-unreplayable never calls compute_fn
# ---------------------------------------------------------------------------


def test_declared_unreplayable_never_calls_compute_fn(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    conn = _make_conn()
    conn.commit()
    sql = "\n".join(conn.iterdump())
    k1 = _commit_knowledge(repo_dir, sql)

    def _boom(_conn):
        raise AssertionError(
            "compute_fn was called for a declared-unreplayable measurement"
        )

    registry = dict(health_measurements.MEASUREMENTS)
    declared_names = (
        "embedder_installed_available",
        "embedder_config_enabled",
        "claim_bearing_null_embedding",
    )
    for name in declared_names:
        registry[name] = dataclasses.replace(registry[name], compute_fn=_boom)

    measurements, unreplayable = series.replay_commit(repo_dir, k1, registry=registry)

    for name in declared_names:
        assert measurements[name] is None
        assert unreplayable[name]["class"] == "declared"


# ---------------------------------------------------------------------------
# 23. backfill idempotence (design test 4)
# ---------------------------------------------------------------------------


def test_backfill_is_idempotent(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _commit_knowledge(repo_dir, "-- commit 1", _committer_iso(t0))
    _commit_knowledge(repo_dir, "-- commit 2", _committer_iso(t0 + timedelta(minutes=1)))

    db_path = tmp_path / "series.db"
    result1 = series.backfill(repo_dir, db_path)
    assert result1["commits_done"] == 2

    conn = series.open_store(db_path)
    row_count = conn.execute("SELECT COUNT(*) c FROM l0_points").fetchone()["c"]
    conn.close()
    assert row_count == 2

    result2 = series.backfill(repo_dir, db_path)
    assert result2["reason"] == "gap_fill"
    assert result2["commits_total"] == 0
    assert result2["commits_done"] == 0

    conn2 = series.open_store(db_path)
    assert conn2.execute("SELECT COUNT(*) c FROM l0_points").fetchone()["c"] == row_count
    conn2.close()


# ---------------------------------------------------------------------------
# 24. reason: initial -> gap_fill -> major_bump (design test 5)
# ---------------------------------------------------------------------------


def test_backfill_reason_initial_then_gap_fill_then_major_bump(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _commit_knowledge(repo_dir, "-- commit 1", _committer_iso(t0))

    db_path = tmp_path / "series.db"
    result1 = series.backfill(repo_dir, db_path, health_major=1)
    assert result1["reason"] == "initial"
    assert result1["commits_done"] == 1

    _commit_knowledge(repo_dir, "-- commit 2", _committer_iso(t0 + timedelta(minutes=1)))
    result2 = series.backfill(repo_dir, db_path, health_major=1)
    assert result2["reason"] == "gap_fill"
    assert result2["commits_done"] == 1

    conn = series.open_store(db_path)
    m1_rows_before = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM l0_points WHERE health_major = 1 ORDER BY commit_sha"
        ).fetchall()
    ]
    conn.close()

    result3 = series.backfill(repo_dir, db_path, health_major=2)
    assert result3["reason"] == "major_bump"
    assert result3["commits_total"] == 2
    assert result3["commits_done"] == 2

    conn2 = series.open_store(db_path)
    m1_rows_after = [
        dict(r)
        for r in conn2.execute(
            "SELECT * FROM l0_points WHERE health_major = 1 ORDER BY commit_sha"
        ).fetchall()
    ]
    conn2.close()

    assert m1_rows_before == m1_rows_after


# ---------------------------------------------------------------------------
# 25. a replay-declaration flip is a gap, never a mutation (D2)
# ---------------------------------------------------------------------------


def test_replay_decl_flip_is_a_gap_not_a_mutation(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)

    conn = _make_conn()
    _insert(conn, "d1", "derivation", embedding=None)
    _insert(conn, "o1", "observation_factual", embedding="[0.1,0.2]")
    conn.commit()
    sql = "\n".join(conn.iterdump())

    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _commit_knowledge(repo_dir, sql, _committer_iso(t0))

    db_path = tmp_path / "series.db"
    result1 = series.backfill(repo_dir, db_path)
    assert result1["commits_done"] == 1

    conn_store = series.open_store(db_path)
    old_rows = [
        dict(r)
        for r in conn_store.execute(
            "SELECT * FROM l0_points ORDER BY commit_sha, health_major, source, "
            "replay_decl_hash"
        ).fetchall()
    ]
    conn_store.close()
    old_hash = old_rows[0]["replay_decl_hash"]

    flipped_registry = dict(health_measurements.MEASUREMENTS)
    flipped_registry["claim_bearing_null_embedding"] = dataclasses.replace(
        flipped_registry["claim_bearing_null_embedding"], replay="graph"
    )

    result2 = series.backfill(repo_dir, db_path, registry=flipped_registry)
    assert result2["reason"] == "gap_fill"
    assert result2["commits_total"] == result2["commits_done"] == 1

    conn_store2 = series.open_store(db_path)
    all_rows = [
        dict(r)
        for r in conn_store2.execute(
            "SELECT * FROM l0_points ORDER BY commit_sha, health_major, source, "
            "replay_decl_hash"
        ).fetchall()
    ]
    conn_store2.close()

    old_hash_rows = [r for r in all_rows if r["replay_decl_hash"] == old_hash]
    assert old_hash_rows == old_rows, "the old-hash rows must stay byte-identical"

    new_hash_rows = [r for r in all_rows if r["replay_decl_hash"] != old_hash]
    assert len(new_hash_rows) == 1
    new_measurements = json.loads(new_hash_rows[0]["measurements"])
    # This fixture puts an embedding on o1 and none on d1 -- so under the
    # flipped ("graph") declaration, claim_bearing_null_embedding is a REAL
    # int (1: only d1 is null), not the null/"stripped_from_dump" value the
    # old-hash row carries.
    assert isinstance(new_measurements["claim_bearing_null_embedding"], int)
    assert new_measurements["claim_bearing_null_embedding"] == 1


# ---------------------------------------------------------------------------
# 26. turn is parsed only when the commit message states a transition (§5)
# ---------------------------------------------------------------------------


def test_turn_parsed_only_when_stated():
    assert (
        series.parse_turn_from_message("[session] Dream 2026-09-04 (turn 54->55)")
        == 55
    )
    assert series.parse_turn_from_message("[nap] 16:18Z nap (drowsiness 98%)") is None


# ---------------------------------------------------------------------------
# 27. equality_checks migration adds replay_decl_hash
# ---------------------------------------------------------------------------

_OLD_EQUALITY_CHECKS_DDL = """
CREATE TABLE equality_checks (
    commit_sha TEXT NOT NULL,
    health_major INTEGER NOT NULL,
    verdict TEXT CHECK(verdict IN ('agree','disagree')),
    diff TEXT,
    checked_at TEXT
);
"""


def test_equality_checks_migration_adds_decl_hash_column(tmp_path):
    db_path = tmp_path / "old-equality.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_OLD_EQUALITY_CHECKS_DDL)
    conn.execute(
        "INSERT INTO equality_checks (commit_sha, health_major, verdict, diff, checked_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("abc123", 1, "agree", "{}", "2026-09-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    reopened = series.open_store(db_path)
    row = reopened.execute(
        "SELECT * FROM equality_checks WHERE commit_sha = ?", ("abc123",)
    ).fetchone()
    assert row is not None
    assert row["replay_decl_hash"] == ""
    assert row["verdict"] == "agree"

    reopened.execute(
        "INSERT INTO equality_checks "
        "(commit_sha, health_major, verdict, diff, checked_at, replay_decl_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("def456", 1, "disagree", "{}", "2026-09-01T00:01:00Z", "somehash"),
    )
    reopened.commit()
    row2 = reopened.execute(
        "SELECT replay_decl_hash FROM equality_checks WHERE commit_sha = ?",
        ("def456",),
    ).fetchone()
    assert row2["replay_decl_hash"] == "somehash"
    reopened.close()

    # idempotent on a second open
    reopened2 = series.open_store(db_path)
    count = reopened2.execute("SELECT COUNT(*) c FROM equality_checks").fetchone()["c"]
    assert count == 2
    reopened2.close()


# ---------------------------------------------------------------------------
# 28. verify appends, never overwrites
# ---------------------------------------------------------------------------


def test_verify_appends_not_overwrites(tmp_path):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)

    conn = _make_conn()
    _insert(conn, "d1", "derivation")
    conn.commit()
    sql = "\n".join(conn.iterdump())

    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    k1 = _commit_knowledge(repo_dir, sql, _committer_iso(t0))

    snap1 = _build_v2_snapshot(anchor=None, turn=1)
    snap1["measurements"] = health_measurements.compute_all_measurements(conn)

    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    ts1 = t0.strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts1, snap1)

    db_path = tmp_path / "series.db"
    series.ingest(snapshots_dir, repo_dir, db_path)

    health_major = snap1["raw_schema_version"]
    result1 = series.verify(repo_dir, db_path, health_major=health_major)
    assert result1["checked"] == 1

    result2 = series.verify(repo_dir, db_path, health_major=health_major)
    assert result2["checked"] == 1

    conn_store = series.open_store(db_path)
    rows = conn_store.execute(
        "SELECT checked_at, verdict FROM equality_checks WHERE commit_sha = ? "
        "ORDER BY checked_at",
        (k1,),
    ).fetchall()
    assert len(rows) == 2

    status_result = series.status(db_path)
    assert status_result["latest_equality_verdicts"] == {"agree": 1, "disagree": 0}
    conn_store.close()


# ---------------------------------------------------------------------------
# 29. engine_build must name the code build, not the data repo HEAD (#2600)
# ---------------------------------------------------------------------------


def test_engine_build_is_not_the_data_repo_head(tmp_path, monkeypatch):
    data_repo = tmp_path / "data-repo"
    _init_repo(data_repo)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    h_data = _commit_knowledge(data_repo, "-- k1", _committer_iso(t0))

    code_repo_head = series._default_repo_dir()
    result_git = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(code_repo_head),
    )
    module_checkout_head = result_git.stdout.strip()

    # sub-case A: no .deployed-version -- falls through to the module
    # checkout's own HEAD (code_repo_dir defaults to _default_repo_dir()).
    snapshots_dir = tmp_path / "health-snapshots"
    snapshots_dir.mkdir()
    snap = _build_v2_snapshot(anchor=None, turn=1)
    ts = t0.strftime("%Y%m%dT%H%M%SZ")
    _write_snapshot_file(snapshots_dir, 1, "nap", ts, snap)

    db_path = tmp_path / "series-a.db"
    series.ingest(snapshots_dir, data_repo, db_path)
    series.backfill(data_repo, db_path)

    conn = series.open_store(db_path)
    rows = conn.execute("SELECT DISTINCT engine_build FROM l0_points").fetchall()
    conn.close()
    assert rows, "expected at least one l0_points row"
    for row in rows:
        build = row["engine_build"]
        assert build != h_data
        assert build in (module_checkout_head, "unknown")

    # sub-case B: a .deployed-version with alpha_sha= wins over the module
    # checkout's HEAD. ``ingest``/``backfill`` call ``detect_engine_build()``
    # bare (no ``module_path``), so to make this genuinely exercise the
    # plugin-tree branch we monkeypatch the module's own default-seam
    # (``_MODULE_PATH_DEFAULT``, the seam #2600 round 3 introduced so bare
    # callers stay testable) to point INTO a plugin-tree marker nested
    # inside ``data_repo`` -- otherwise this sub-case is vacuous (round 2
    # reported exactly that: the alpha_sha here was never read by the code
    # under test, since bare ``detect_engine_build()`` resolves from THIS
    # module's own real location, not from ``data_repo``).
    plugin_dir_b = data_repo / "marketplace" / "plugins" / "engram"
    health_dir_b = plugin_dir_b / "health"
    health_dir_b.mkdir(parents=True)
    (plugin_dir_b / ".engram-build-manifest.json").write_text("{}", encoding="utf-8")
    (data_repo / ".deployed-version").write_text(
        "alpha_sha=deadbeefcafe\n", encoding="utf-8"
    )
    monkeypatch.setattr(series, "_MODULE_PATH_DEFAULT", health_dir_b / "series.py")

    db_path_b = tmp_path / "series-b.db"
    series.ingest(snapshots_dir, data_repo, db_path_b)
    series.backfill(data_repo, db_path_b)

    conn_b = series.open_store(db_path_b)
    rows_b = conn_b.execute("SELECT DISTINCT engine_build FROM l0_points").fetchall()
    conn_b.close()
    assert rows_b
    for row in rows_b:
        assert row["engine_build"] == "deadbeefcafe"


# ---------------------------------------------------------------------------
# 30. _guard_engine_build rejects a sha that resolves only in the data repo
# ---------------------------------------------------------------------------


def test_guard_rejects_sha_that_resolves_only_in_data_repo(tmp_path, capsys):
    data_repo = tmp_path / "data-repo"
    _init_repo(data_repo)
    data_sha = _commit_knowledge(data_repo, "k1")

    code_repo = tmp_path / "code-repo"
    _init_repo(code_repo)
    code_sha = _commit_knowledge(code_repo, "c1")

    # a sha that resolves ONLY in the data repo -> 'unknown' + stderr line
    result = series._guard_engine_build(data_sha, data_repo=data_repo, code_repo=code_repo)
    assert result == "unknown"
    captured = capsys.readouterr()
    assert data_sha in captured.err
    assert str(data_repo) in captured.err
    assert str(code_repo) in captured.err

    # a sha that resolves in the code repo -> returned unchanged
    assert (
        series._guard_engine_build(code_sha, data_repo=data_repo, code_repo=code_repo)
        == code_sha
    )

    # 'unknown' -> returned unchanged (not sha-shaped)
    assert (
        series._guard_engine_build("unknown", data_repo=data_repo, code_repo=code_repo)
        == "unknown"
    )

    # a sha that resolves in NEITHER repo -> returned unchanged (this guard
    # is only for the data-repo/code-repo collision, not a general validator)
    bogus_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert (
        series._guard_engine_build(bogus_sha, data_repo=data_repo, code_repo=code_repo)
        == bogus_sha
    )


# ---------------------------------------------------------------------------
# 31. status reports engine_build resolvability against the code repo
# ---------------------------------------------------------------------------


def test_status_reports_engine_build_resolvability(tmp_path):
    data_repo = tmp_path / "data-repo"
    _init_repo(data_repo)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _commit_knowledge(data_repo, "-- k1", _committer_iso(t0))

    code_repo = tmp_path / "code-repo"
    _init_repo(code_repo)
    code_sha = _commit_knowledge(code_repo, "-- c1")

    db_path = tmp_path / "series.db"
    series.backfill(data_repo, db_path, engine_build=code_sha)
    series.backfill(data_repo, db_path, engine_build="unknown", health_major=999)

    result = series.status(db_path, code_repo_dir=code_repo)
    by_build = {
        d["engine_build"]: d["resolves_in_code_repo"]
        for d in result["engine_build_resolvability"]
    }
    assert by_build[code_sha] is True
    assert by_build["unknown"] is None
