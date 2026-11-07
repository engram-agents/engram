"""#4421 follow-up: the BLOCKED_STALE refusal no longer contradicts itself.

Written BEFORE the fix (contract-first). Before: the top-level `guidance` said a premise carrying a cascade marker "is
resolved by superseding or retracting it", while every per-premise `route` said a plain supersede/retract on it is
REFUSED while markers are pending and pointed at engram_cascade_frontier / engram_cascade_resolve. The guidance also never
mentioned `acknowledged`, which IS eligible downstream of a supersede-rooted marker.

Contract: the guidance resolves a marked premise through engram_cascade_resolve (no refused verb named as the way to clear
it) and states each marked premise's eligible shapes in words, in the frontier sentence's form
("<id>: retracted or superseded (not acknowledged)"), computed with `_cascade_eligible_shapes`; a directly superseded
premise keeps its "cite the successor" route and is not listed as a marked premise.
"""
import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core
import engram_derive
import engram_observation
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_stale_guidance_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))
J = json.loads
_SEQ = [0]


def _mods():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, core


def _ob(obs, claim):
    _SEQ[0] += 1
    r = J(obs._add_observation_impl(
        url=f"https://example.com/g{_SEQ[0]}", title="S", quoted_text=claim + " in the source",
        interpretation=claim, claim=claim, quote_type="attributed_analysis"))
    return r["observation_id"]


def _dv(derive, claim, premises):
    r = J(derive._derive_impl(claim=claim, supporting_ids=",".join(premises), logical_chain=claim,
                              reasoning_type="deductive_modus_ponens"))
    assert "derivation_id" in r, r
    return r["derivation_id"]


def _try(derive, premises):
    return J(derive._derive_impl(claim="A new one.", supporting_ids=",".join(premises), logical_chain="c",
                                 reasoning_type="deductive_modus_ponens"))


def _supersede(rev, old, new):
    assert "error" not in J(rev._supersede_impl(old_node_id=old, new_node_id=new, supersede_reason="refined"))


def test_guidance_resolves_a_marked_premise_through_cascade_resolve_and_names_no_refused_verb():
    rev, obs, derive, core = _mods()
    o = _ob(obs, "old")
    d1 = _dv(derive, "depends", [o])
    _supersede(rev, o, _ob(obs, "new"))
    r = _try(derive, [d1])
    assert r["status"] == "blocked_stale"
    g = r["guidance"]
    assert "engram_cascade_resolve" in g
    for refused in ("engram_supersede", "engram_retract", "superseding or retracting"):
        assert refused not in g, (refused, g)


def test_guidance_lists_acknowledged_for_a_supersede_rooted_premise():
    rev, obs, derive, core = _mods()
    o = _ob(obs, "old")
    d1 = _dv(derive, "depends", [o])
    _supersede(rev, o, _ob(obs, "new"))
    g = _try(derive, [d1])["guidance"]
    assert f"{d1}: retracted, superseded or acknowledged" in g
    assert "(not acknowledged)" not in g


def test_guidance_omits_acknowledged_for_a_marked_premise_whose_own_premise_was_resolved_retracted():
    rev, obs, derive, core = _mods()
    o = _ob(obs, "old")
    p1 = _dv(derive, "first link", [o])
    x = _dv(derive, "second link", [p1])
    _supersede(rev, o, _ob(obs, "new"))
    res = J(rev._cascade_resolve_impl(node_id=p1, resolution_kind="retracted",
                                      replacement_absent_because="NONE_BECAUSE_UNKNOWN",
                                      replacement_absent_note="fixture"))
    assert "error" not in res, res
    r = _try(derive, [x])
    assert r["status"] == "blocked_stale"
    assert f"{x}: retracted or superseded (not acknowledged)" in r["guidance"]


def test_a_directly_superseded_premise_keeps_the_cite_the_successor_route_and_is_not_listed_as_marked():
    rev, obs, derive, core = _mods()
    o = _ob(obs, "old")
    succ = _ob(obs, "new")
    _supersede(rev, o, succ)
    r = _try(derive, [o])
    assert r["status"] == "blocked_stale"
    assert f"cite the successor {succ}" in r["stale_premises"][0]["stale_because"][0]["route"]
    assert "successor" in r["guidance"]
    assert f"{o}: " not in r["guidance"]
