"""#2624 -- sibling hardcoded `relation = 'derives_from'` literals survive
#2618's fix, at three sites: engram_lifecycle.py's thin-support scan,
engram_stats.py's single_source_derivations count, and engram_stats.py's
cross_evidence_derivations walk.

All three ask the SAME one-hop-premise question core._direct_premise_ids
answers ("what are this derivation's direct premises"), so all three now
range over core.CITATION_SUPPORT_RELATIONS (derives_from, supported_by,
acknowledged_derives_from) rather than a hand-rolled `relation =
'derives_from'` literal that missed acknowledged_derives_from -- the
#2349 defect class, three files (two modules) over from #2618's own fix.

Coverage:
  1. engram_lifecycle's thin_support_derivations no longer flags a
     derivation whose SECOND premise arrives only via
     acknowledged_derives_from (the live gap).
  2. engram_stats's single_source_derivations no longer counts that same
     derivation (same live gap, sibling metric).
  3. engram_stats's cross_evidence_derivations correctly counts a
     derivation whose two premises' evidence_id values differ, when the
     SECOND premise arrives only via acknowledged_derives_from (the live
     gap, applied to the evidence-diversity walk).
  4. Sourcing proof (checked to be able to FAIL): monkeypatching
     engram_core.CITATION_SUPPORT_RELATIONS to a decoy set changes what
     all three metrics report -- if any of the three sites were still a
     hardcoded literal, patching the module constant would have no effect
     on that site.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_relation_literals_2624_")
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


def _add_observation(server, claim, url):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _derive(server, claim, supporting_ids):
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids),
        "logical_chain": "Test chain.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derive failed: {r}"
    return r["derivation_id"]


def _add_ack_edge(server, source_id, target_id):
    """Directly insert an acknowledged_derives_from edge -- the acknowledge
    path's real machinery (engram_resolve's cascade-frontier flow) is out
    of scope here; this test cares only about whether the READ side
    (thin_support / single_source / cross_evidence) sees the edge, not
    about how a real acknowledgement gets written."""
    conn = server._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (source_id, target_id, "acknowledged_derives_from"),
        )
        conn.commit()
    finally:
        conn.close()


def _get_entries(result, key):
    val = result.get(key, [])
    if isinstance(val, dict) and "shown" in val:
        return val["shown"]
    return val if isinstance(val, list) else []


def _build_two_premise_derivation(server):
    """ax1 (evidence A) --derives_from-- dv1 --acknowledged_derives_from-- ax2
    (evidence B). dv1 has exactly ONE real `derives_from` edge (to ax1) and
    ONE `acknowledged_derives_from` edge (to ax2, added directly) -- so a
    literal `relation = 'derives_from'` count reads 1 (thin/single-source,
    single-evidence), while the correct CITATION_SUPPORT_RELATIONS count
    reads 2 (not thin/single-source, cross-evidence, since ax1 and ax2 come
    from different evidence URLs)."""
    ax1 = _add_observation(server, "Premise one, evidence A.", "https://example.com/evidence-a")
    ax2 = _add_observation(server, "Premise two, evidence B.", "https://example.com/evidence-b")
    dv1 = _derive(server, "Derivation with a second, acknowledged premise.", [ax1])
    _add_ack_edge(server, dv1, ax2)
    return dv1, ax1, ax2


def _build_one_premise_control_derivation(server):
    """A genuinely single-premise derivation -- must STILL read as
    thin/single-source under the fix. Without this control, a fix that
    always returns "not thin" for every derivation would pass tests 1-2
    vacuously."""
    ax = _add_observation(server, "Sole premise, evidence C.", "https://example.com/evidence-c")
    return _derive(server, "Genuinely single-source derivation.", [ax])


def _build_two_plain_derives_from_derivation(server):
    """Two REAL `derives_from` edges (via engram_derive's own supporting_ids,
    no direct-SQL edge involved) -- the sourcing-proof control. A hardcoded
    `relation = 'derives_from'` literal reads this as 2 premises REGARDLESS
    of what core.CITATION_SUPPORT_RELATIONS is monkeypatched to (the literal
    never looks at the constant at all); code that genuinely SOURCES from
    the constant reads 0 premises once the constant is patched to a set
    excluding 'derives_from'. That divergence -- not merely "does the
    count change" -- is what proves the SQL reads the module constant."""
    ax_d = _add_observation(server, "Plain premise D.", "https://example.com/evidence-d")
    ax_e = _add_observation(server, "Plain premise E.", "https://example.com/evidence-e")
    return _derive(server, "Derivation with two ordinary derives_from premises.", [ax_d, ax_e])


# ---------------------------------------------------------------------------
# 1. engram_lifecycle.py: thin_support_derivations
# ---------------------------------------------------------------------------


def test_thin_support_sees_acknowledged_derives_from_premise():
    server = fresh_server()
    dv1, _ax1, _ax2 = _build_two_premise_derivation(server)
    control_dv = _build_one_premise_control_derivation(server)

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 5})))
    assert "error" not in result, result
    thin_ids = {e["id"] for e in _get_entries(result, "thin_support_derivations")}

    assert dv1 not in thin_ids, (
        f"{dv1} has 2 premises (1 derives_from + 1 acknowledged_derives_from) "
        f"but still reads as thin-support -- the acknowledged_derives_from "
        f"premise is invisible to the count (#2624 live gap)"
    )
    assert control_dv in thin_ids, (
        "the genuinely single-premise control derivation must still be "
        "flagged thin-support -- a fix that stops flagging EVERYTHING would "
        "pass the assertion above vacuously"
    )


# ---------------------------------------------------------------------------
# 2. engram_stats.py: single_source_derivations
# ---------------------------------------------------------------------------


def test_single_source_derivations_excludes_acknowledged_premise_derivation():
    server = fresh_server()
    _dv1, _ax1, _ax2 = _build_two_premise_derivation(server)
    _control_dv = _build_one_premise_control_derivation(server)

    result = json.loads(server.engram_diagnose())
    assert "error" not in result, result

    # dv1 has 2 premises (not single-source); the control has 1 (genuinely
    # single-source) -- so the count must be exactly 1, not 2. Under the
    # pre-fix literal, dv1 would ALSO read support_count=1 (missing the
    # acknowledged edge), making the count wrongly 2.
    single_source_count = result["epistemic"]["single_source_derivations"]
    assert single_source_count == 1, (
        f"expected exactly 1 single-source derivation (the genuine control), "
        f"got {single_source_count} -- the acknowledged_derives_from premise "
        f"is invisible to the count (#2624 live gap)"
    )


# ---------------------------------------------------------------------------
# 3. engram_stats.py: cross_evidence_derivations
# ---------------------------------------------------------------------------


def test_cross_evidence_derivations_sees_acknowledged_derives_from_premise():
    server = fresh_server()
    dv1, _ax1, _ax2 = _build_two_premise_derivation(server)

    result = json.loads(server.engram_diagnose())
    assert "error" not in result, result

    connectivity = result["connectivity"]
    # dv1's two premises (ax1, ax2) carry DIFFERENT evidence_id values (two
    # different URLs) -- under the fix, dv1 is walked via both edges and
    # counted as cross-evidence. Under the pre-fix literal, only ax1's
    # (derives_from) evidence would be seen -- one evidence_id -> counted
    # as single-evidence instead, not cross-evidence.
    assert connectivity["cross_evidence_derivations"] >= 1, (
        f"expected at least 1 cross-evidence derivation (dv1, whose second "
        f"premise arrives only via acknowledged_derives_from), got "
        f"connectivity={connectivity} -- the acknowledged_derives_from "
        f"premise is invisible to the evidence-diversity walk (#2624 live gap)"
    )


# ---------------------------------------------------------------------------
# 4. Sourcing proof: all three metrics read the module constant, not a
# hardcoded literal
# ---------------------------------------------------------------------------


def test_all_three_sites_source_from_the_module_constant(monkeypatch):
    """Monkeypatching engram_core.CITATION_SUPPORT_RELATIONS to a decoy set
    that EXCLUDES 'derives_from' entirely must flip all three metrics'
    verdict on a derivation with two ORDINARY derives_from premises (built
    via engram_derive's own supporting_ids, no direct-SQL edge involved).

    Why this fixture, not tests 1-3's acknowledged-edge one: a hardcoded
    `relation = 'derives_from'` literal is IMMUNE to any monkeypatch of the
    constant (it never reads it), so it would keep reporting this
    derivation's real 2 premises regardless of the decoy -- not-thin,
    excluded from single_source_derivations, counted as cross_evidence
    (its two premises carry different evidence_id values). Code that
    genuinely SOURCES the relation set from core.CITATION_SUPPORT_RELATIONS
    reads ZERO premises once the constant excludes 'derives_from' -- thin,
    included in single_source_derivations, falls into pure_chain (zero
    evidence ids) rather than cross_evidence. That is a hard verdict flip
    on the SAME derivation, not merely "the count changed" -- an earlier
    draft of this test used tests 1-3's fixture instead and could not
    actually distinguish "reads the constant" from "still hardcoded,
    coincidentally producing the same threshold-side classification"
    (both readings landed on the <=1 side either way); caught by mutation-
    testing this file against dev's own unfixed code, which passed this
    test for the wrong reason before this fixture was substituted in."""
    server = fresh_server()
    dv2 = _build_two_plain_derives_from_derivation(server)

    core = sys.modules["engram_core"]
    monkeypatch.setattr(core, "CITATION_SUPPORT_RELATIONS", frozenset({"cites"}))

    reflect_result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 5})))
    assert "error" not in reflect_result, reflect_result
    thin_ids = {e["id"] for e in _get_entries(reflect_result, "thin_support_derivations")}
    assert dv2 in thin_ids, (
        "engram_lifecycle.py's thin_support scan did not react to the "
        "CITATION_SUPPORT_RELATIONS monkeypatch (a 2-premise, genuinely "
        "not-thin derivation is still reading as not-thin) -- it may still "
        "hold a hardcoded literal rather than reading the module constant"
    )

    stats_result = json.loads(server.engram_diagnose())
    assert "error" not in stats_result, stats_result
    assert stats_result["epistemic"]["single_source_derivations"] == 1, (
        "engram_stats.py's single_source_derivations did not react to the "
        "CITATION_SUPPORT_RELATIONS monkeypatch -- it may still hold a "
        "hardcoded literal rather than reading the module constant"
    )
    connectivity = stats_result["connectivity"]
    assert connectivity["cross_evidence_derivations"] == 0, (
        "engram_stats.py's cross_evidence_derivations did not react to the "
        "CITATION_SUPPORT_RELATIONS monkeypatch (still counting the "
        "2-distinct-evidence derivation as cross-evidence) -- it may still "
        "hold a hardcoded literal rather than reading the module constant"
    )
    assert connectivity["pure_chain_derivations"] == 1, (
        f"expected the premise-less-under-the-decoy derivation to fall into "
        f"pure_chain, got connectivity={connectivity}"
    )
