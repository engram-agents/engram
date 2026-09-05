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

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import engine as health_engine  # noqa: E402

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
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_NODES_DDL)
    return conn


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
# 13. detect_engine_build order
# ---------------------------------------------------------------------------


def test_detect_engine_build_order(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    sha = _commit_knowledge(repo_dir, "k1")

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # env wins
    monkeypatch.setenv("ENGRAM_ENGINE_BUILD", "env-build")
    assert series.detect_engine_build(data_dir=data_dir, repo_dir=repo_dir) == "env-build"
    monkeypatch.delenv("ENGRAM_ENGINE_BUILD")

    # then .deployed-version's alpha_sha=
    (data_dir / ".deployed-version").write_text(
        "some_other_key=ignored\nalpha_sha=deployed-sha\n", encoding="utf-8"
    )
    assert (
        series.detect_engine_build(data_dir=data_dir, repo_dir=repo_dir)
        == "deployed-sha"
    )

    # a .deployed-version with no alpha_sha= line falls through
    (data_dir / ".deployed-version").write_text("foo=bar\n", encoding="utf-8")
    assert series.detect_engine_build(data_dir=data_dir, repo_dir=repo_dir) == sha

    # then git HEAD of repo_dir
    (data_dir / ".deployed-version").unlink()
    assert series.detect_engine_build(data_dir=data_dir, repo_dir=repo_dir) == sha

    # else 'unknown'
    empty_repo = tmp_path / "not-a-repo"
    empty_repo.mkdir()
    assert series.detect_engine_build(data_dir=None, repo_dir=empty_repo) == "unknown"


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
