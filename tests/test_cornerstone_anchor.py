"""Tests for the cornerstone anchor channel (#1691, P3 slice 1).

Coverage:
  §1 Cache rebuild — _rebuild_cornerstone_anchors_cache walks exemplifies
     edges to cornerstone targets, writes the index, anchor_line falls back
     to the claim, and engram_register_exemplar on a cornerstone target
     triggers the rebuild (the previously-reserved slot).
  §2 Surface-hook check — check_cornerstone_anchor was folded into the
     unified check_principle_triggers (#1698 slice 2). Most of this
     section's original coverage (exemplar-ID firing, cooldown
     suppress/expire, missing-registry silence) is now redundant with
     tests/test_principle_triggers_registry.py's §5 byte-compat/cooldown
     suite for kind=cornerstone. What survives here are the cornerstone-kind
     behaviors NOT exercised there: direct-principal-ID firing via the
     by_principle reverse lookup, the compaction counter-reset case, and
     malformed-registry-shape silence. Also added (reviewer-fairy flag on
     PR #1713): the lesson-kind counterpart of the direct-ID test, since
     unification widened the by_principle reverse lookup to all four kinds,
     not cornerstone-only as it was pre-#1698.
  §3 _stamp_new_node importance_base override (#2250) — the explicit
     override parameter added to skip the confidence/surprise-derived
     interim computation entirely (implementation-detail companion to the
     end-to-end anchor-invariant tests in test_add_cornerstone_payload.py /
     test_outgrow_cornerstone_payload.py).
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(SRC_ENGRAM))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_cornerstone_anchor_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _seed_axiom(server, claim, basis="test fixture basis"):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim,
        "basis": basis,
    })))
    assert "error" not in r, r
    return r["axiom_id"]


def _seed_cs(server, supporting_ids, tag="cs-tag"):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": tag,
        "title": f"Cornerstone: {tag}",
        "new_frame": f"Principle for {tag}.",
        "supporting_ids": supporting_ids,
    })))
    assert "error" not in r, r
    return r["cornerstone_id"]


# ---------------------------------------------------------------------------
# §1 Cache rebuild
# ---------------------------------------------------------------------------


def test_rebuild_cache_from_graph():
    """Rebuild walks exemplifies→cornerstone edges into the index."""
    server = fresh_server()
    import engram_core as core

    sup = _seed_axiom(server, "Supporting instance for the anchor cornerstone.")
    cs_id = _seed_cs(server, sup, tag="anchor-rebuild-cs")
    exemplar = _seed_axiom(server, "A concrete situation the principle fired in.")

    r = json.loads(server.engram_register_exemplar(payload_json=json.dumps({
        "target_id": cs_id,
        "exemplar_id": exemplar,
    })))
    assert r["status"] == "created", r

    result = core._rebuild_cornerstone_anchors_cache()
    assert result["status"] == "rebuilt"
    assert result["cornerstone_count"] >= 1

    with open(core.CORNERSTONE_ANCHORS_PATH) as f:
        index = json.load(f)
    assert exemplar in index
    assert index[exemplar]["cornerstone_id"] == cs_id


def test_rebuild_anchor_line_fallback_to_claim():
    """anchor_line falls back to the cornerstone claim when metadata unset."""
    server = fresh_server()
    import engram_core as core

    sup = _seed_axiom(server, "Support for fallback cornerstone.")
    cs_id = _seed_cs(server, sup, tag="fallback-cs")
    exemplar = _seed_axiom(server, "Exemplar for the fallback test.")
    json.loads(server.engram_register_exemplar(payload_json=json.dumps({
        "target_id": cs_id, "exemplar_id": exemplar,
    })))

    core._rebuild_cornerstone_anchors_cache()
    with open(core.CORNERSTONE_ANCHORS_PATH) as f:
        index = json.load(f)
    entry = index[exemplar]
    assert entry["anchor_line"]  # non-empty
    assert entry["anchor_line"] == entry["cornerstone_claim"]


def test_register_exemplar_cornerstone_rebuilds_cache():
    """The register call itself refreshes the cache (reserved slot filled)."""
    server = fresh_server()
    import engram_core as core

    sup = _seed_axiom(server, "Support for auto-rebuild cornerstone.")
    cs_id = _seed_cs(server, sup, tag="auto-rebuild-cs")
    exemplar = _seed_axiom(server, "Exemplar registered via MCP tool.")

    r = json.loads(server.engram_register_exemplar(payload_json=json.dumps({
        "target_id": cs_id, "exemplar_id": exemplar,
    })))
    assert r["status"] == "created", r
    assert r.get("cache_rebuild", {}).get("status") == "rebuilt", r

    with open(core.CORNERSTONE_ANCHORS_PATH) as f:
        index = json.load(f)
    assert exemplar in index
    assert index[exemplar]["cornerstone_id"] == cs_id


# ---------------------------------------------------------------------------
# §2 Surface-hook check_principle_triggers (cornerstone kind) — surviving
# gap-coverage after the check_cornerstone_anchor -> check_principle_triggers
# fold (#1698 slice 2). See tests/test_principle_triggers_registry.py §5 for
# the byte-compat + cooldown + missing-registry coverage this section used
# to carry.
# ---------------------------------------------------------------------------


def _load_surface_hook(engram_home: str):
    """Load the hyphenated surface hook with ENGRAM_HOME pointed at a dir."""
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = engram_home
    try:
        hook_path = SRC_ENGRAM / "hooks" / "claude" / "engram-surface-hook.py"
        spec = importlib.util.spec_from_file_location(
            f"surface_hook_{Path(engram_home).name}", hook_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old


def _hook_with_registry(tmpdir: str):
    """Unified-registry counterpart to the retired _hook_with_index — writes
    principle_triggers.json (kind: cornerstone) instead of the folded
    cornerstone_anchors.json shape."""
    hook = _load_surface_hook(tmpdir)
    registry = {
        "ob_9001": {
            "principle_id": "cs_9001",
            "kind": "cornerstone",
            "claim": "Gate the decidable.",
            "nudge": "If a machine can check it, make it a mechanism.",
        }
    }
    with open(hook.PRINCIPLE_TRIGGERS_PATH, "w") as f:
        json.dump(registry, f)
    return hook


def test_fires_on_direct_cornerstone_id(tmp_path):
    """A matched ID that IS the cornerstone's own principle_id (not a
    trigger) fires via the by_principle reverse lookup -- not exercised by
    test_principle_triggers_registry.py's byte-compat tests, which only
    match on trigger IDs, never on a principal's own ID."""
    hook = _hook_with_registry(str(tmp_path))
    out = hook.check_principle_triggers(["cs_9001"], prompt_count=5)
    assert "cs_9001" in out


def test_fires_on_direct_lesson_id(tmp_path):
    """Reviewer-fairy flag (PR #1713): the by_principle reverse lookup is
    generalized across all four kinds by unification, not cornerstone-only
    as it was pre-#1698 (check_incident_tripwire had no such reverse lookup
    for lessons at all). A matched ID that IS a lesson's own principle_id
    (not an incident/trigger ID) now fires too -- this is the lesson-kind
    counterpart to test_fires_on_direct_cornerstone_id above, closing the
    gap the reviewer-fairy identified as untested."""
    hook = _load_surface_hook(str(tmp_path))
    registry = {
        "ob_9002": {
            "principle_id": "ls_9002",
            "kind": "lesson",
            "claim": "Verify before trusting a claim about system state.",
            "nudge": "Check the live artifact, not the report.",
        }
    }
    with open(hook.PRINCIPLE_TRIGGERS_PATH, "w") as f:
        json.dump(registry, f)
    out = hook.check_principle_triggers(["ls_9002"], prompt_count=5)
    assert "ls_9002" in out
    # Byte-compat register still applies even when reached via direct ID --
    # the lesson rendering doesn't distinguish how principle_id was matched.
    assert "[ENGRAM Tripwire (ls_9002)" in out


def test_counter_reset_clears_cooldown(tmp_path):
    """A compaction-driven prompt-counter reset (current prompt_count below
    the last-fired count) must clear the cooldown -- not exercised by
    test_check_principle_triggers_cooldown_suppresses_regardless_of_kind,
    which only tests monotonically-increasing prompt_count."""
    hook = _hook_with_registry(str(tmp_path))
    hook.check_principle_triggers(["ob_9001"], prompt_count=20)
    # Compaction resets the counter; last_fired(20) > current(1) → allowed
    assert hook.check_principle_triggers(["ob_9001"], prompt_count=1)


def test_malformed_cache_shape_is_silent(tmp_path):
    """Valid JSON, wrong shape (e.g. a list, or string entries) must degrade
    to silence — the hook runs on every prompt (reviewer-fairy suggestion,
    PR #1695). Not exercised by test_principle_triggers_registry.py's §5
    suite, which only tests well-formed registries."""
    hook = _hook_with_registry(str(tmp_path))
    with open(hook.PRINCIPLE_TRIGGERS_PATH, "w") as f:
        json.dump(["not", "a", "dict"], f)
    assert hook.check_principle_triggers(["ob_9001"], prompt_count=5) == ""
    with open(hook.PRINCIPLE_TRIGGERS_PATH, "w") as f:
        json.dump({"ob_9001": "entry-is-a-string-not-a-dict"}, f)
    assert hook.check_principle_triggers(["ob_9001"], prompt_count=5) == ""


def test_generic_edge_tools_resync_anchor_cache():
    """engram_add_edge / engram_remove_edge on an exemplifies→cornerstone
    edge must resync cornerstone_anchors.json, not only the lesson cache
    (the sibling gap the #1695 reviewer-fairy flagged)."""
    server = fresh_server()
    import engram_core as core

    sup = _seed_axiom(server, "Support for generic-edge cornerstone.")
    cs_id = _seed_cs(server, sup, tag="generic-edge-cs")
    exemplar = _seed_axiom(server, "Exemplar linked via generic edge tool.")

    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": exemplar, "target_id": cs_id, "relation": "exemplifies",
    })))
    assert r["status"] == "created", r
    with open(core.CORNERSTONE_ANCHORS_PATH) as f:
        index = json.load(f)
    assert exemplar in index, "add_edge did not resync the anchor cache"

    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": exemplar, "target_id": cs_id, "relation": "exemplifies",
    })))
    assert r["status"] == "removed", r
    with open(core.CORNERSTONE_ANCHORS_PATH) as f:
        index = json.load(f)
    assert exemplar not in index, "remove_edge left a stale anchor entry"


# ---------------------------------------------------------------------------
# §3 _stamp_new_node importance_base override (#2250)
# ---------------------------------------------------------------------------


def test_stamp_new_node_importance_base_override():
    """importance_base override bypasses the confidence/surprise-derived
    computation entirely — the override value lands directly (#2250)."""
    server = fresh_server()
    import engram_core as core

    node_id = _seed_axiom(server, "Axiom for the stamp_new_node override test.")
    conn = core._get_db()
    try:
        # confidence=0.9/surprise=0.9 would compute base = 0.9*1.9 = 1.71
        # via _importance_base_for_node if it were used — it must NOT be,
        # since importance_base is explicitly overridden below.
        core._stamp_new_node(conn, node_id, confidence=0.9, surprise=0.9,
                              importance_base=3.3)
        conn.commit()
        row = conn.execute(
            "SELECT importance_base, importance_score FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["importance_base"] == 3.3
    expected_score = core._compute_importance(3.3, core._get_current_turn())
    assert row["importance_score"] == expected_score


def test_stamp_new_node_default_behavior_unchanged():
    """No importance_base override: behavior matches the pre-#2250 path
    exactly — confidence/surprise still derive importance_base via
    _importance_base_for_node, identical to every other _stamp_new_node
    caller that doesn't pass the new parameter."""
    server = fresh_server()
    import engram_core as core

    node_id = _seed_axiom(server, "Axiom for the stamp_new_node default-path test.")
    conn = core._get_db()
    try:
        core._stamp_new_node(conn, node_id, confidence=0.7, surprise=0.5)
        conn.commit()
        row = conn.execute(
            "SELECT importance_base, importance_score FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    finally:
        conn.close()
    expected_base = core._importance_base_for_node(0.7, 0.5)
    assert row["importance_base"] == expected_base
    expected_score = core._compute_importance(expected_base, core._get_current_turn())
    assert row["importance_score"] == expected_score
