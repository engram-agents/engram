"""Tests for #1948 items 3-5 — special_moments about-edge backfill +
deep-view full-history verification.

Companion to test_person_node_typed_schema.py (§3, cap behavior) and
test_person_node_render.py (#1940, rotation/cursor render). This file
covers the two concerns #1948 adds that neither of those already touch:

  1. _backfill_special_moment_about_edges (engram_trust.py): a one-time,
     idempotent repair pass that creates missing `about` edges for
     special_moments entries whose anchor was never wired via
     engram_link_about. Covers: mixed missing/present edges in one call,
     idempotency (0 new edges on a second run), and skip-on-dangling-ref
     (a node_id that no longer exists in `nodes` at all).

  2. engram_inspect(view="deep") still returns the FULL special_moments
     list untouched (#1948 item 5 — a verification, not a new behavior;
     confirms nothing in #1940's or #1948's own changes started
     truncating/filtering special_moments in the deep view).

Follows the fresh_server() / TEST_DIR sandbox pattern already established
in test_person_node_typed_schema.py and test_person_node_render.py.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_special_moments_backfill_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Provision a clean server instance backed by a temporary directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _add_person(server, name="BackfillPerson"):
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"name": name})))
    assert "error" not in r, r
    return r["person_id"]


def _add_observation(server, claim):
    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": claim,
        "quoted_text": claim[:20],
        "interpretation": "a special event",
        "quote_type": "hard_data",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    assert "error" not in obs, obs
    return obs["observation_id"]


def _add_moment_without_edge(server, engram_trust, pid, obs_id, description):
    """Add a special_moments entry WITHOUT first wiring the about-edge —
    reproduces the historical defect #1948 backfills (a moment added via
    _add_special_moment_impl whose anchor never got an engram_link_about
    call)."""
    result = json.loads(engram_trust._add_special_moment_impl(
        person_id=pid,
        description=description,
        node_id=obs_id,
    ))
    assert "error" not in result, result
    return result


def _about_edge_exists(conn, node_id, person_id):
    row = conn.execute(
        "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
        (node_id, person_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# §1 — _backfill_special_moment_about_edges
# ---------------------------------------------------------------------------

def test_backfill_creates_only_missing_edges():
    """A person with 3 special_moments — 1 already has an about-edge
    (wired via engram_link_about, as the normal workflow does), 2 don't
    (added directly, reproducing the historical gap). After backfill, ALL
    3 have edges; the backfill count reflects only the 2 that were missing."""
    server = fresh_server()
    import engram_core as core
    import engram_trust

    pid = _add_person(server)

    obs_with_edge = _add_observation(server, "Moment with edge")
    server.engram_link_about(payload_json=json.dumps({
        "node_id": obs_with_edge, "person_id": pid,
    }))
    _add_moment_without_edge(server, engram_trust, pid, obs_with_edge, "Has edge already")

    obs_missing_1 = _add_observation(server, "Moment missing edge 1")
    _add_moment_without_edge(server, engram_trust, pid, obs_missing_1, "Missing edge 1")

    obs_missing_2 = _add_observation(server, "Moment missing edge 2")
    _add_moment_without_edge(server, engram_trust, pid, obs_missing_2, "Missing edge 2")

    conn = core._get_db()
    try:
        # Sanity: confirm the pre-backfill state matches the setup intent.
        assert _about_edge_exists(conn, obs_with_edge, pid)
        assert not _about_edge_exists(conn, obs_missing_1, pid)
        assert not _about_edge_exists(conn, obs_missing_2, pid)

        created = engram_trust._backfill_special_moment_about_edges(conn)
        assert created == 2, f"Expected exactly 2 new edges, backfill reported {created}"

        # All 3 now have edges.
        assert _about_edge_exists(conn, obs_with_edge, pid)
        assert _about_edge_exists(conn, obs_missing_1, pid)
        assert _about_edge_exists(conn, obs_missing_2, pid)
    finally:
        conn.close()


def test_backfill_idempotent_second_run_creates_zero():
    """Running the backfill twice must not create duplicate edges — the
    second run reports 0 new edges."""
    server = fresh_server()
    import engram_core as core
    import engram_trust

    pid = _add_person(server)
    obs_id = _add_observation(server, "Moment for idempotency check")
    _add_moment_without_edge(server, engram_trust, pid, obs_id, "No edge yet")

    conn = core._get_db()
    try:
        first_run = engram_trust._backfill_special_moment_about_edges(conn)
        assert first_run == 1, first_run

        # Confirm no duplicate edge rows were created (UNIQUE constraint
        # would raise on a raw re-insert, but the backfill's own
        # existence-check is the first line of defense — verify directly).
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'about'",
            (obs_id, pid),
        ).fetchone()
        assert rows["n"] == 1

        second_run = engram_trust._backfill_special_moment_about_edges(conn)
        assert second_run == 0, f"Expected 0 new edges on re-run, got {second_run}"

        rows_after = conn.execute(
            "SELECT COUNT(*) AS n FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'about'",
            (obs_id, pid),
        ).fetchone()
        assert rows_after["n"] == 1, "Re-run must not create a duplicate edge"
    finally:
        conn.close()


def test_backfill_skips_dangling_node_id():
    """A special_moments entry whose node_id no longer references any real
    node (e.g. a historical row that was purged, or simply a malformed
    entry) must be skipped, not raise — the backfill has no valid edge
    source to insert with."""
    server = fresh_server()
    import engram_core as core
    import engram_trust

    pid = _add_person(server)

    # Inject a special_moments entry with a node_id that was never a real
    # node — directly, since _add_special_moment_impl itself validates the
    # anchor exists at add-time (this simulates a pre-existing/historical
    # dangling reference, not a reachable live-write path).
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (pid,)).fetchone()
        meta = json.loads(row["metadata"])
        meta["special_moments"] = [
            {"description": "Dangling", "node_id": "ob_9999999", "added_at": core._now()},
        ]
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(meta), pid))
        conn.commit()

        created = engram_trust._backfill_special_moment_about_edges(conn)
        assert created == 0, f"Dangling node_id must not produce an edge, got {created}"
        assert not _about_edge_exists(conn, "ob_9999999", pid)
    finally:
        conn.close()


def test_backfill_returns_zero_on_no_person_nodes():
    """No person nodes at all → 0, no error."""
    server = fresh_server()
    import engram_core as core
    import engram_trust

    conn = core._get_db()
    try:
        created = engram_trust._backfill_special_moment_about_edges(conn)
        assert created == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §2 — engram_inspect(view="deep") returns the FULL special_moments list
# ---------------------------------------------------------------------------

def test_deep_view_returns_full_special_moments_history():
    """#1948 item 5 (verification, not new behavior): engram_inspect with
    view='deep' returns every special_moments entry, not just the one
    rotating recall/query surfaces would show. Adds more moments than the
    rotation surfaces would ever render in one call, then confirms deep
    view's node.metadata (raw JSON) carries all of them."""
    server = fresh_server()
    pid = _add_person(server, name="DeepViewPerson")

    n = 5
    node_ids = []
    for i in range(n):
        obs_id = _add_observation(server, f"Deep-view moment {i}")
        server.engram_link_about(payload_json=json.dumps({
            "node_id": obs_id, "person_id": pid,
        }))
        r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
            "person_id": pid,
            "description": f"Deep-view moment {i}",
            "node_id": obs_id,
        })))
        assert "error" not in r, r
        node_ids.append(obs_id)

    r = json.loads(server.engram_inspect(payload_json=json.dumps({
        "node_id": pid,
        "view": "deep",
    })))
    assert "error" not in r, r
    assert r.get("view") == "deep", r

    node = r["node"]
    raw_meta = json.loads(node["metadata"])
    moments = raw_meta["special_moments"]
    assert len(moments) == n, (
        f"Deep view must carry the FULL special_moments list ({n} entries), "
        f"got {len(moments)}"
    )
    assert [m["node_id"] for m in moments] == node_ids, (
        "Deep view's special_moments must be the untouched, full-order list"
    )

    # Contrast: the recall view (default) shows at most ONE rotating
    # moment via the top-level "special_moment" (singular) key, not the
    # full list — confirms deep view is genuinely different from recall,
    # not that recall also happens to show everything.
    r_recall = json.loads(server.engram_inspect(payload_json=json.dumps({
        "node_id": pid,
    })))
    assert "special_moments" not in r_recall["node"], (
        "recall view should not carry the raw special_moments list as a "
        "top-level field — only deep view's raw metadata does"
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
