"""#405 slice 1 -- the MECHANICAL half of hardening tools/surgical.py.

Item A: the live `edge_add` defect -- the INSERT omitted `created_at`
(`edges.created_at` is `TEXT NOT NULL`), so every `edge_add` execute raised
`sqlite3.IntegrityError`. Fixed by including `created_at` via the file's own
`_now()` helper.

Item B: preview-time invariant checks (`_check_invariants`), called by every
`preview_*` before a token is issued, and re-run at execute time so a
stale-but-valid token cannot outlive a fresh violation:

  1. temporal_causal direction on edge_add (older-source-than-target DAG
     violation) -- refuse; equal timestamps -- warn; a temporal_causal=False
     relation is exempt.
  2. a pending cascade marker on any node the op would touch -- refuse.
  3. legacy marker fields (tainted_by / stale_by, the pre-#2411 field form)
     -- refuse.
  4. orphan/strand on edge_remove -- warn (advisory; health penalises
     orphans, doesn't invalidate them).
  5. a non-current (is_current=0) target -- warn (advisory; audit history).

Each check gets a discriminating pair: the violating case fires, and the
nearest valid neighbour does NOT. Policy items (CLAUDE.md warning, a
surgical skill, a human-greenlight gate beyond the token handshake, maturity
gating) are explicitly OUT OF SCOPE for this slice and are not tested here.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"

SCHEMA = """
CREATE TABLE nodes (
    id                  TEXT PRIMARY KEY,
    type                TEXT,
    claim               TEXT,
    created_at          TEXT,
    metadata            TEXT,
    confidence          REAL,
    confidence_history  TEXT,
    quote_type          TEXT,
    quoted_text         TEXT,
    interpretation      TEXT,
    logical_chain       TEXT,
    source_url          TEXT,
    source_title        TEXT,
    source_domain       TEXT,
    source_date         TEXT,
    predicted_event     TEXT,
    status              TEXT,
    importance_base     REAL,
    importance_score    REAL,
    is_current          INTEGER DEFAULT 1
);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE cascade_markers (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id                     TEXT NOT NULL,
    cascade_event               TEXT NOT NULL,
    root_node                   TEXT NOT NULL,
    root_op                     TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'pending',
    resolution_kind             TEXT,
    resolution_replacement_id   TEXT,
    created_at                  TEXT NOT NULL,
    resolved_at                 TEXT
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


def _load_surgical():
    """Import tools/surgical.py fresh, isolated from any cached module.

    Mirrors tests/test_surgical_relation_sets.py's loader (see that file's
    own docstring for why this does NOT touch sys.path: tests/conftest.py
    already puts SRC_ENGRAM on sys.path for the whole session)."""
    sys.modules.pop("surgical", None)
    spec = importlib.util.spec_from_file_location(
        "surgical", str(SRC_ENGRAM / "tools" / "surgical.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def surgical(tmp_path, monkeypatch):
    """A fresh surgical module wired to a scratch on-disk sqlite DB (the
    module's DB access goes through a real file path, not :memory:, since
    _get_db() opens DB_PATH fresh on every call)."""
    mod = _load_surgical()
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(mod, "SNAPSHOT_PATH", tmp_path / "graph_snapshot.md")
    return mod


@pytest.fixture
def db_path(surgical):
    return surgical.DB_PATH


def _conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


def _insert_node(conn, node_id, ntype="observation_factual", created_at="2025-01-01T00:00:00+00:00",
                  claim=None, metadata=None, confidence=None, is_current=1, **extra):
    cols = ["id", "type", "claim", "created_at", "metadata", "confidence", "is_current"]
    vals = [node_id, ntype, claim, created_at, json.dumps(metadata or {}), confidence, is_current]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    conn.execute(
        f"INSERT INTO nodes ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        vals,
    )


def _insert_edge(conn, source_id, target_id, relation, created_at="2025-01-01T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        (source_id, target_id, relation, created_at),
    )


def _insert_marker(conn, node_id, root_node="ob_root", root_op="retract",
                    status="pending", cascade_event="evt_1",
                    created_at="2025-01-01T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, cascade_event, root_node, root_op, status, created_at),
    )


# ── Item A: the created_at defect ───────────────────────────────────────

def test_edge_add_round_trip_carries_created_at(surgical, db_path):
    """preview -> execute round trip for edge_add succeeds and the edge row
    carries a non-null created_at. Uses `contradicts` (temporal_causal=False)
    so the round trip isn't entangled with Item B's invariant checks."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_a")
    _insert_node(conn, "ob_b")
    conn.commit()
    conn.close()

    preview = surgical.edge_add(
        "preview", "ob_a", "ob_b", "contradicts", "test reason", "ob_bug",
    )
    assert "confirmation_token" in preview
    token = preview["confirmation_token"]

    executed = surgical.edge_add(
        "execute", "ob_a", "ob_b", "contradicts", "test reason", "ob_bug", token,
    )
    assert executed["phase"] == "executed", executed

    conn = _conn(db_path)
    row = conn.execute(
        "SELECT created_at FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
        ("ob_a", "ob_b", "contradicts"),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["created_at"] is not None
    assert row["created_at"] != ""


def test_old_insert_shape_control_raises_integrity_error(db_path):
    """Control: the OLD 3-column INSERT shape (no created_at) against the
    same scratch schema raises sqlite3.IntegrityError -- demonstrating the
    defect this PR fixes, not just asserting the fix works in isolation."""
    conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation) VALUES (?, ?, ?)",
            ("ob_a", "ob_b", "contradicts"),
        )
    conn.close()


# ── Item B.1: temporal_causal direction on edge_add ─────────────────────

def test_edge_add_older_source_temporal_causal_refuses(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_old", created_at="2020-01-01T00:00:00+00:00")
    _insert_node(conn, "ob_new", created_at="2024-01-01T00:00:00+00:00")
    conn.commit()
    conn.close()

    result = surgical.edge_add(
        "preview", "ob_old", "ob_new", "derives_from", "test reason", "ob_bug",
    )
    assert result.get("error") == "invariant_refused"
    assert "confirmation_token" not in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "temporal_causal_direction" in names
    refuse = [f for f in result["invariant_findings"] if f["invariant"] == "temporal_causal_direction"]
    assert refuse[0]["severity"] == "refuse"


def test_edge_add_older_source_non_temporal_causal_relation_does_not_refuse(surgical, db_path):
    """Discriminating pair: same older-source/newer-target timestamps, but
    `resolves` is temporal_causal=False -- deliberately exempt."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_old", created_at="2020-01-01T00:00:00+00:00")
    _insert_node(conn, "ob_new", created_at="2024-01-01T00:00:00+00:00")
    conn.commit()
    conn.close()

    result = surgical.edge_add(
        "preview", "ob_old", "ob_new", "resolves", "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "temporal_causal_direction" not in names


def test_edge_add_equal_timestamps_warns_not_refuses(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_x", created_at="2022-06-01T00:00:00+00:00")
    _insert_node(conn, "ob_y", created_at="2022-06-01T00:00:00+00:00")
    conn.commit()
    conn.close()

    result = surgical.edge_add(
        "preview", "ob_x", "ob_y", "derives_from", "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    warn = [f for f in result["invariant_findings"] if f["invariant"] == "temporal_causal_direction"]
    assert len(warn) == 1
    assert warn[0]["severity"] == "warn"


# ── Item B.2: pending cascade marker on a touched node ──────────────────

def test_metadata_patch_pending_marker_refuses(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_marked", metadata={})
    _insert_marker(conn, "ob_marked", status="pending")
    conn.commit()
    conn.close()

    result = surgical.metadata_patch(
        "preview", "ob_marked", {"foo": "bar"}, "test reason", "ob_bug",
    )
    assert result.get("error") == "invariant_refused"
    assert "confirmation_token" not in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "pending_cascade_marker" in names


def test_metadata_patch_resolved_marker_does_not_refuse(surgical, db_path):
    """Discriminating pair: a marker exists but is already 'resolved' --
    _pending_markers_for_node filters to status='pending' only."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_clear", metadata={})
    _insert_marker(conn, "ob_clear", status="resolved")
    conn.commit()
    conn.close()

    result = surgical.metadata_patch(
        "preview", "ob_clear", {"foo": "bar"}, "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "pending_cascade_marker" not in names


def test_edge_add_pending_marker_on_either_endpoint_refuses(surgical, db_path):
    """Item B.2 explicitly ranges over 'all five ops; for edges, both
    endpoints' -- a marker on the TARGET (not the source) still refuses."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_source")
    _insert_node(conn, "ob_target")
    _insert_marker(conn, "ob_target", status="pending")
    conn.commit()
    conn.close()

    result = surgical.edge_add(
        "preview", "ob_source", "ob_target", "contradicts", "test reason", "ob_bug",
    )
    assert result.get("error") == "invariant_refused"
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "pending_cascade_marker" in names


# ── Item B.3: legacy marker fields (pre-#2411 field form) ───────────────

def test_metadata_patch_tainted_by_refuses(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_legacy", metadata={})
    conn.commit()
    conn.close()

    result = surgical.metadata_patch(
        "preview", "ob_legacy", {"tainted_by": ["ob_root"]}, "test reason", "ob_bug",
    )
    assert result.get("error") == "invariant_refused"
    assert "confirmation_token" not in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "legacy_marker_field" in names


def test_metadata_patch_non_marker_field_does_not_refuse(surgical, db_path):
    """Discriminating pair: a patch of a non-marker metadata key passes
    normally."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_ok", metadata={})
    conn.commit()
    conn.close()

    result = surgical.metadata_patch(
        "preview", "ob_ok", {"source_class": "external"}, "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "legacy_marker_field" not in names


def test_check_invariants_field_patch_legacy_marker_field_refuses(surgical, db_path):
    """field_patch's own ALLOWED_FIELDS allowlist already excludes
    tainted_by/stale_by (they were never real node columns -- they were
    metadata keys), so the real field_patch() entry point can never reach
    this branch today. This calls _check_invariants directly (operation=
    'field_patch') to verify the defensive check still exists and fires,
    per the spec's explicit 'metadata_patch / field_patch' wording."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_z")
    conn.commit()

    findings, applied = surgical._check_invariants(
        conn, "field_patch",
        {"node_id": "ob_z", "field_name": "stale_by", "new_value": "[]"},
    )
    conn.close()
    names = [f["invariant"] for f in findings]
    assert "legacy_marker_field" in names
    refuse = [f for f in findings if f["invariant"] == "legacy_marker_field"]
    assert refuse[0]["severity"] == "refuse"


# ── Item B.4: orphan/strand on edge_remove ───────────────────────────────

def test_edge_remove_orphans_both_endpoints_warns(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_a")
    _insert_node(conn, "ob_b")
    _insert_edge(conn, "ob_a", "ob_b", "contradicts")
    conn.commit()
    conn.close()

    result = surgical.edge_remove(
        "preview", "ob_a", "ob_b", "contradicts", "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    orphan_findings = [f for f in result["invariant_findings"] if f["invariant"] == "orphan_strand"]
    assert len(orphan_findings) == 2
    assert all(f["severity"] == "warn" for f in orphan_findings)


def test_edge_remove_with_remaining_edges_does_not_orphan(surgical, db_path):
    """Discriminating pair: both endpoints retain another edge after the
    removal -- no orphan warning fires."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_a")
    _insert_node(conn, "ob_b")
    _insert_node(conn, "ob_c")
    _insert_edge(conn, "ob_a", "ob_b", "contradicts")
    _insert_edge(conn, "ob_a", "ob_c", "contradicts")
    _insert_edge(conn, "ob_b", "ob_c", "contradicts")
    conn.commit()
    conn.close()

    result = surgical.edge_remove(
        "preview", "ob_a", "ob_b", "contradicts", "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    orphan_findings = [f for f in result["invariant_findings"] if f["invariant"] == "orphan_strand"]
    assert orphan_findings == []


# ── Item B.5: non-current target ─────────────────────────────────────────

def test_field_patch_non_current_node_warns(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_retracted", is_current=0, status="active")
    conn.commit()
    conn.close()

    result = surgical.field_patch(
        "preview", "ob_retracted", "status", "superseded", "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "non_current_target" in names
    finding = [f for f in result["invariant_findings"] if f["invariant"] == "non_current_target"][0]
    assert finding["severity"] == "warn"


def test_field_patch_current_node_does_not_warn(surgical, db_path):
    """Discriminating pair: an is_current=1 node produces no such finding."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_current", is_current=1, status="active")
    conn.commit()
    conn.close()

    result = surgical.field_patch(
        "preview", "ob_current", "status", "superseded", "test reason", "ob_bug",
    )
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "non_current_target" not in names


# ── Execute-time re-check: a stale-but-valid token must not outlive a ──
# ── fresh violation ───────────────────────────────────────────────────

def test_execute_refuses_on_fresh_violation_despite_valid_token(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_between", metadata={})
    conn.commit()
    conn.close()

    preview = surgical.metadata_patch(
        "preview", "ob_between", {"foo": "bar"}, "test reason", "ob_bug",
    )
    assert "confirmation_token" in preview
    token = preview["confirmation_token"]

    # A pending cascade marker lands on the node between preview and execute.
    conn = _conn(db_path)
    _insert_marker(conn, "ob_between", status="pending")
    conn.commit()
    conn.close()

    executed = surgical.metadata_patch(
        "execute", "ob_between", {"foo": "bar"}, "test reason", "ob_bug", token,
    )
    assert executed.get("error") == "invariant_refused"
    assert executed.get("phase") != "executed"

    # And the patch was never applied.
    conn = _conn(db_path)
    row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", ("ob_between",)).fetchone()
    conn.close()
    assert json.loads(row["metadata"]) == {}


# ── --override-invariant names the override in the edit log ─────────────

def test_override_invariant_recorded_in_edit_log(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_override", metadata={})
    _insert_marker(conn, "ob_override", status="pending")
    conn.commit()
    conn.close()

    preview = surgical.metadata_patch(
        "preview", "ob_override", {"foo": "bar"}, "test reason", "ob_bug",
        overrides=["pending_cascade_marker"],
    )
    assert preview.get("error") != "invariant_refused"
    assert "confirmation_token" in preview
    warn = [f for f in preview["invariant_findings"] if f["invariant"] == "pending_cascade_marker"]
    assert warn[0]["severity"] == "warn"
    token = preview["confirmation_token"]

    executed = surgical.metadata_patch(
        "execute", "ob_override", {"foo": "bar"}, "test reason", "ob_bug", token,
        overrides=["pending_cascade_marker"],
    )
    assert executed["phase"] == "executed", executed

    conn = _conn(db_path)
    row = conn.execute(
        "SELECT details FROM edit_history WHERE node_id = ? AND action = ? "
        "ORDER BY id DESC LIMIT 1",
        ("ob_override", "surgical_metadata_patch"),
    ).fetchone()
    conn.close()
    details = json.loads(row["details"])
    assert details["invariant_overrides"] == ["pending_cascade_marker"]


# ── Item B.2 / B.5 via recompute_confidence's public entry point ────────
# (round-2 fold-in: the other four ops are integration-tested through their
# public entry points; recompute_confidence's B.2/B.5 coverage previously
# only exercised _check_invariants directly.)

def test_recompute_confidence_pending_marker_refuses(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_rc_marked", quote_type="hard_data")
    _insert_marker(conn, "ob_rc_marked", status="pending")
    conn.commit()
    conn.close()

    result = surgical.recompute_confidence(
        "preview", "ob_rc_marked", "test reason", "ob_bug",
    )
    assert result.get("error") == "invariant_refused"
    assert "confirmation_token" not in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "pending_cascade_marker" in names


def test_recompute_confidence_resolved_marker_does_not_refuse(surgical, db_path):
    """Discriminating pair: same node, but the marker is already resolved --
    _pending_markers_for_node filters to status='pending' only."""
    conn = _conn(db_path)
    _insert_node(conn, "ob_rc_clear", quote_type="hard_data")
    _insert_marker(conn, "ob_rc_clear", status="resolved")
    conn.commit()
    conn.close()

    result = surgical.recompute_confidence(
        "preview", "ob_rc_clear", "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "pending_cascade_marker" not in names


def test_recompute_confidence_non_current_node_warns_but_issues_token(surgical, db_path):
    conn = _conn(db_path)
    _insert_node(conn, "ob_rc_retracted", quote_type="hard_data", is_current=0)
    conn.commit()
    conn.close()

    result = surgical.recompute_confidence(
        "preview", "ob_rc_retracted", "test reason", "ob_bug",
    )
    assert result.get("error") != "invariant_refused"
    assert "confirmation_token" in result
    names = [f["invariant"] for f in result["invariant_findings"]]
    assert "non_current_target" in names
    finding = [f for f in result["invariant_findings"] if f["invariant"] == "non_current_target"][0]
    assert finding["severity"] == "warn"
