"""Tests for engram_inspect include_superseded gate (issue #443).

Background (#443):
    engram_inspect returned 1-hop neighbors without filtering by is_current,
    so retracted/superseded nodes appeared alongside live neighbors in all three
    view modes. The fix adds include_superseded: bool = False to the payload
    schema; when False, only is_current=1 neighbors are shown.

Coverage:
    1. Default behavior (include_superseded absent / False) — non-current neighbors
       are hidden in all three views: recall, deep, edges.
    2. include_superseded=True — non-current neighbors appear in all three views.
    3. truncated_superseded_count — present with correct N when neighbors are
       filtered; absent when none are filtered.
    4. unknown-field rejection — include_superseded accepted by schema (no "Unknown
       fields" error).
    5. Parametrized across all three views (recall / deep / edges) for scenarios 1-3.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_inspect_include_superseded")


def fresh_server():
    """Reset state and return the server module bound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for mod in list(sys.modules.keys()):
        if mod == "server":
            del sys.modules[mod]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim: str, url: str = "https://example.com/test") -> str:
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _add_axiom(server, claim: str) -> str:
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim,
        "basis": "Test basis.",
    })))
    assert "axiom_id" in r, f"add_axiom failed: {r}"
    return r["axiom_id"]


def _derive(server, claim: str, supporting_ids: list) -> str:
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids),
        "logical_chain": "Test chain.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derive failed: {r}"
    return r["derivation_id"]


def _supersede(server, old_id: str, new_id: str) -> None:
    r = json.loads(server._supersede_impl(old_id, new_id, supersede_reason="test supersede"))
    assert r.get("status") == "superseded", f"supersede failed: {r}"


def _retract(server, node_id: str) -> None:
    r = json.loads(server._retract_impl(
        node_id=node_id,
        error_type="other",
        reason="test retraction",
    ))
    assert r.get("status") == "retracted", f"retract failed: {r}"


def _inspect(server, node_id: str, view: str = "recall", include_superseded: bool = None) -> dict:
    payload = {"node_id": node_id, "view": view}
    if include_superseded is not None:
        payload["include_superseded"] = include_superseded
    return json.loads(server.engram_inspect(payload_json=json.dumps(payload)))


def _get_neighbor_ids_from_result(result: dict, view: str) -> set:
    """Extract all neighbor node IDs from an inspect result for any view."""
    ids = set()
    if view == "recall":
        ln = result.get("logical_neighbors", {})
        for group in ("upstream", "downstream", "lateral"):
            for nb in ln.get(group, []):
                ids.add(nb["id"])
        for nb in result.get("contextual_neighbors", []):
            ids.add(nb["id"])
    elif view in ("deep", "edges"):
        topology = result.get("topology", {})
        for entries in topology.values():
            for entry in entries:
                ids.add(entry["to"])
    return ids


# ---------------------------------------------------------------------------
# Shared fixture builder — creates a focal node connected to:
#   - current_id: is_current=1 (live neighbor)
#   - superseded_id: is_current=0 (superseded — was old_id before being superseded)
#   - retracted_id: is_current=0 (retracted)
#
# Strategy: the ENGRAM derive guard blocks derivation from retracted/tainted
# premises (BLOCKED_TAINTED). To test inspect filtering without fighting the
# guard, we:
#   1. Build the focal node via derive from two LIVE premises (the live
#      current_id plus the to-be-superseded old_id BEFORE superseding).
#   2. After creating the focal node, supersede old_id with a replacement so
#      old_id becomes is_current=0. The existing derives_from edge focal→old_id
#      remains in the edges table (the supersede operation does not delete edges
#      to the old node).
#   3. Insert one more edge directly into the DB: focal→retracted_id, which
#      was retracted and set is_current=0 via the retract API.
#
# This mirrors the real-world scenario from cs_0003 (issue #443): edges that
# were valid at creation time, but their targets later became non-current.
#
# Returns (server, focal_id, current_id, superseded_id, retracted_id).
# ---------------------------------------------------------------------------

def _build_fixture():
    server = fresh_server()

    # current neighbor — always visible
    current_id = _add_observation(
        server,
        "Current neighbor: is_current=1 inspect443regression uniquemarkerA",
        url="https://example.com/current-443",
    )

    # old neighbor — will be superseded AFTER we build the focal node
    old_id = _add_observation(
        server,
        "Old neighbor to be superseded inspect443regression uniquemarkerB",
        url="https://example.com/old-443",
    )

    # Build the focal node while old_id is still current (avoids BLOCKED_TAINTED).
    focal_id = _derive(
        server,
        "Focal node for inspect443 regression — derives_from current + old (pre-supersede)",
        [current_id, old_id],
    )

    # Now supersede old_id → it becomes is_current=0. The focal→old_id edge persists.
    replacement_id = _add_observation(
        server,
        "Replacement for old neighbor inspect443regression uniquemarkerB",
        url="https://example.com/replacement-443",
    )
    _supersede(server, old_id, replacement_id)
    superseded_id = old_id  # now is_current=0

    # Retracted neighbor: add separately (so we can control when it's retracted),
    # then insert a raw edge focal→retracted_id directly into the DB so the edge
    # exists without having gone through derive (which would block on tainted).
    retracted_id = _add_observation(
        server,
        "Retracted neighbor inspect443regression uniquemarkerC",
        url="https://example.com/retract-443",
    )
    _retract(server, retracted_id)
    # Insert edge directly — bypasses the derive guard, which is the correct pattern
    # for testing the inspect surface (we want the edge to exist but the target to
    # be non-current, simulating the real cs_0003 scenario).
    conn = server._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (focal_id, retracted_id, "supported_by", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    return server, focal_id, current_id, superseded_id, retracted_id


# ---------------------------------------------------------------------------
# 1 + 2 + 3: Default / include_superseded=False hides non-current; True shows all.
#            Parametrized across all three views.
# ---------------------------------------------------------------------------

def _run_filter_test(view: str) -> None:
    """Shared logic for view-parametrized filter tests."""
    server, focal_id, current_id, superseded_id, retracted_id = _build_fixture()

    # --- Default (no flag) ---
    r_default = _inspect(server, focal_id, view=view)
    assert "error" not in r_default, f"[{view}] default: unexpected error: {r_default}"
    ids_default = _get_neighbor_ids_from_result(r_default, view)
    assert current_id in ids_default, (
        f"[{view}] default: current neighbor should be visible; ids={ids_default}"
    )
    assert superseded_id not in ids_default, (
        f"[{view}] default: superseded neighbor should be hidden; ids={ids_default}"
    )
    assert retracted_id not in ids_default, (
        f"[{view}] default: retracted neighbor should be hidden; ids={ids_default}"
    )

    # --- include_superseded=False (explicit) ---
    r_false = _inspect(server, focal_id, view=view, include_superseded=False)
    assert "error" not in r_false, f"[{view}] False: unexpected error: {r_false}"
    ids_false = _get_neighbor_ids_from_result(r_false, view)
    assert current_id in ids_false, (
        f"[{view}] False: current neighbor should be visible; ids={ids_false}"
    )
    assert superseded_id not in ids_false, (
        f"[{view}] False: superseded neighbor should be hidden; ids={ids_false}"
    )
    assert retracted_id not in ids_false, (
        f"[{view}] False: retracted neighbor should be hidden; ids={ids_false}"
    )

    # --- include_superseded=True ---
    r_true = _inspect(server, focal_id, view=view, include_superseded=True)
    assert "error" not in r_true, f"[{view}] True: unexpected error: {r_true}"
    ids_true = _get_neighbor_ids_from_result(r_true, view)
    assert current_id in ids_true, (
        f"[{view}] True: current neighbor should be visible; ids={ids_true}"
    )
    assert superseded_id in ids_true, (
        f"[{view}] True: superseded neighbor should appear with include_superseded=True; ids={ids_true}"
    )
    assert retracted_id in ids_true, (
        f"[{view}] True: retracted neighbor should appear with include_superseded=True; ids={ids_true}"
    )


def test_filter_behavior_recall_view():
    """view='recall': default hides non-current; True reveals them."""
    _run_filter_test("recall")


def test_filter_behavior_deep_view():
    """view='deep': default hides non-current; True reveals them."""
    _run_filter_test("deep")


def test_filter_behavior_edges_view():
    """view='edges': default hides non-current; True reveals them."""
    _run_filter_test("edges")


# ---------------------------------------------------------------------------
# 3. truncated_superseded_count correctness
# ---------------------------------------------------------------------------

def _run_count_hint_test(view: str) -> None:
    """truncated_superseded_count appears with correct N when filtered; absent when none."""
    server, focal_id, current_id, superseded_id, retracted_id = _build_fixture()

    # When filtering: 2 non-current neighbors (superseded_id + retracted_id) → count=2.
    r_filtered = _inspect(server, focal_id, view=view, include_superseded=False)
    assert "error" not in r_filtered, f"[{view}] count test: unexpected error: {r_filtered}"
    count = r_filtered.get("truncated_superseded_count")
    assert count is not None, (
        f"[{view}] truncated_superseded_count should be present when neighbors are filtered; "
        f"response keys: {list(r_filtered.keys())}"
    )
    assert count == 2, (
        f"[{view}] truncated_superseded_count should be 2 (superseded + retracted); got {count}"
    )

    # When not filtering: truncated_superseded_count should be absent.
    r_all = _inspect(server, focal_id, view=view, include_superseded=True)
    assert "error" not in r_all, f"[{view}] count test all: unexpected error: {r_all}"
    assert "truncated_superseded_count" not in r_all, (
        f"[{view}] truncated_superseded_count must not appear when include_superseded=True; "
        f"response keys: {list(r_all.keys())}"
    )


def test_count_hint_recall_view():
    """view='recall': truncated_superseded_count N=2 when filtering; absent when not."""
    _run_count_hint_test("recall")


def test_count_hint_deep_view():
    """view='deep': truncated_superseded_count N=2 when filtering; absent when not."""
    _run_count_hint_test("deep")


def test_count_hint_edges_view():
    """view='edges': truncated_superseded_count N=2 when filtering; absent when not."""
    _run_count_hint_test("edges")


# ---------------------------------------------------------------------------
# 4. No-suppressed-neighbors case: truncated_superseded_count absent
# ---------------------------------------------------------------------------

def test_no_count_hint_when_no_superseded_neighbors():
    """When all neighbors are current, truncated_superseded_count is absent in all views."""
    server = fresh_server()

    ax_id = _add_axiom(server, "Axiom for no-suppression test in inspect443.")
    dv_id = _derive(server, "Derivation with only current neighbors inspect443.", [ax_id])

    for view in ("recall", "deep", "edges"):
        r = _inspect(server, dv_id, view=view)
        assert "error" not in r, f"[{view}] unexpected error: {r}"
        assert "truncated_superseded_count" not in r, (
            f"[{view}] truncated_superseded_count must not appear when no neighbors are filtered; "
            f"response keys: {list(r.keys())}"
        )


# ---------------------------------------------------------------------------
# 5. Schema: include_superseded accepted (not rejected as unknown field)
# ---------------------------------------------------------------------------

def test_include_superseded_field_accepted_by_schema():
    """include_superseded in payload is accepted, not rejected as an unknown field."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for schema acceptance test inspect443.")

    r = _inspect(server, ax_id, include_superseded=False)
    assert "error" not in r, (
        f"include_superseded=False should not trigger unknown-field error; got: {r}"
    )

    r2 = _inspect(server, ax_id, include_superseded=True)
    assert "error" not in r2, (
        f"include_superseded=True should not trigger unknown-field error; got: {r2}"
    )


# ---------------------------------------------------------------------------
# 6. neighbor_count reflects only the visible (non-filtered) neighbors
# ---------------------------------------------------------------------------

def test_neighbor_count_reflects_filtered_count():
    """neighbor_count in the response counts only visible neighbors, not filtered ones."""
    server, focal_id, current_id, superseded_id, retracted_id = _build_fixture()

    for view in ("recall", "deep", "edges"):
        r_filtered = _inspect(server, focal_id, view=view, include_superseded=False)
        assert "error" not in r_filtered, f"[{view}] unexpected error: {r_filtered}"
        n_filtered = r_filtered.get("neighbor_count", -1)

        r_all = _inspect(server, focal_id, view=view, include_superseded=True)
        assert "error" not in r_all, f"[{view}] unexpected error: {r_all}"
        n_all = r_all.get("neighbor_count", -1)

        assert n_filtered < n_all, (
            f"[{view}] neighbor_count when filtered ({n_filtered}) should be less than "
            f"unfiltered ({n_all}) — focal node has 2 non-current neighbors"
        )
        assert n_filtered >= 1, (
            f"[{view}] at least the current neighbor should be counted; got {n_filtered}"
        )


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except Exception:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
