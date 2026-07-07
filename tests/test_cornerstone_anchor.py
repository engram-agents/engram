"""Tests for the cornerstone anchor channel (#1691, P3 slice 1).

Coverage:
  §1 Cache rebuild — _rebuild_cornerstone_anchors_cache walks exemplifies
     edges to cornerstone targets, writes the index, anchor_line falls back
     to the claim, and engram_register_exemplar on a cornerstone target
     triggers the rebuild (the previously-reserved slot).
  §2 Surface-hook check — check_cornerstone_anchor fires on a surfaced
     exemplar ID and on the cornerstone's own ID; cross-prompt cooldown
     suppresses within the window, expires after it, and clears on a
     compaction counter reset; missing cache is silent.
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
# §2 Surface-hook check_cornerstone_anchor
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


def _hook_with_index(tmpdir: str):
    hook = _load_surface_hook(tmpdir)
    index = {
        "ob_9001": {
            "cornerstone_id": "cs_9001",
            "cornerstone_claim": "Gate the decidable.",
            "anchor_line": "If a machine can check it, make it a mechanism.",
        }
    }
    with open(hook.CORNERSTONE_ANCHORS_PATH, "w") as f:
        json.dump(index, f)
    return hook


def test_fires_on_exemplar_id(tmp_path):
    hook = _hook_with_index(str(tmp_path))
    out = hook.check_cornerstone_anchor(["ob_9001"], prompt_count=5)
    assert "cs_9001" in out
    assert "make it a mechanism" in out


def test_fires_on_direct_cornerstone_id(tmp_path):
    hook = _hook_with_index(str(tmp_path))
    out = hook.check_cornerstone_anchor(["cs_9001"], prompt_count=5)
    assert "cs_9001" in out


def test_cooldown_suppresses_within_window(tmp_path):
    hook = _hook_with_index(str(tmp_path))
    assert hook.check_cornerstone_anchor(["ob_9001"], prompt_count=5)
    assert hook.check_cornerstone_anchor(["ob_9001"], prompt_count=7) == ""


def test_cooldown_expires(tmp_path):
    hook = _hook_with_index(str(tmp_path))
    hook.check_cornerstone_anchor(["ob_9001"], prompt_count=5)
    window = hook.CORNERSTONE_ANCHOR_COOLDOWN_PROMPTS
    assert hook.check_cornerstone_anchor(["ob_9001"], prompt_count=5 + window)


def test_counter_reset_clears_cooldown(tmp_path):
    hook = _hook_with_index(str(tmp_path))
    hook.check_cornerstone_anchor(["ob_9001"], prompt_count=20)
    # Compaction resets the counter; last_fired(20) > current(1) → allowed
    assert hook.check_cornerstone_anchor(["ob_9001"], prompt_count=1)


def test_missing_cache_is_silent(tmp_path):
    hook = _hook_with_index(str(tmp_path))
    os.remove(hook.CORNERSTONE_ANCHORS_PATH)
    assert hook.check_cornerstone_anchor(["ob_9001"], prompt_count=5) == ""


def test_malformed_cache_shape_is_silent(tmp_path):
    """Valid JSON, wrong shape (e.g. a list, or string entries) must degrade
    to silence — the hook runs on every prompt (reviewer-fairy suggestion,
    PR #1695)."""
    hook = _hook_with_index(str(tmp_path))
    with open(hook.CORNERSTONE_ANCHORS_PATH, "w") as f:
        json.dump(["not", "a", "dict"], f)
    assert hook.check_cornerstone_anchor(["ob_9001"], prompt_count=5) == ""
    with open(hook.CORNERSTONE_ANCHORS_PATH, "w") as f:
        json.dump({"ob_9001": "entry-is-a-string-not-a-dict"}, f)
    assert hook.check_cornerstone_anchor(["ob_9001"], prompt_count=5) == ""


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
