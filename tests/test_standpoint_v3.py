"""
Tests for standpoint v3: training-lineage axis + per-axis F-S integration.

Implements the design doc's §7 test plan (cycle/standpoint-v3-design.md),
D3 dual-probe shape — known-good controls written FIRST, then known-bad
probes, each gate exercised in both directions:

 1. Known-good control: different lineages → "lineage: N clusters (diverse)"
 2. Known-good control: mixed F-S classes → FALSIFICATION ratio, no ⚠⚠
 3. Same lineage (graph has >= 2 distinct) → strengthened ⚠ uniform fires
 4. One premise NULL lineage → lineage axis omitted (all-or-omit holds)
 5. Author-diverse + lineage-uniform → lineage verdict independent (v2
    blindness pinned)
 6. Lineage-uniform + all-frozen (all known fs_class) → ⚠⚠ composite fires,
    carries the proxy label
 7. Malformed lineage at filing → redirecting format error, row unwritten
 8. Proxy-label invariant: every FALSIFICATION segment from proxy sources
    carries (proxy:quote_type)
 9. Unknown-policy both directions: unknown premises suppress ⚠⚠; same
    shape with known-frozen premises fires it
10. Single-lineage actionability gate both directions: informational in a
    one-lineage graph; ⚠ goes live after the first cross-lineage filing
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_standpoint_v3_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_client():
    """Create a fresh EngramClient with an empty database."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def _read_lineage(node_id):
    """Read standpoint_lineage directly from DB."""
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT standpoint_lineage FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return row["standpoint_lineage"] if row else None
    finally:
        conn.close()


def _obs(client, n, quote_type="hard_data", lineage="", author="", collection=""):
    """File one observation with the given standpoint fields; return its id."""
    payload = {
        "url": f"https://example.com/source-{n}",
        "title": f"Source {n}",
        "quoted_text": f"Measured value number {n} is {n * 7}.",
        "interpretation": f"Measurement {n}.",
        "claim": f"Value number {n} is {n * 7}",
        "quote_type": quote_type,
    }
    if lineage:
        payload["standpoint_lineage"] = lineage
    if author:
        payload["standpoint_author_id"] = author
    if collection:
        payload["standpoint_collection_id"] = collection
    r = client.call("engram_add_observation", payload)
    assert r.get("status") == "created", f"obs {n} failed: {r}"
    return r["observation_id"]


def _derive(client, ids, claim="Derived claim from premises"):
    """Derive from the given premise ids; return (result, warnings)."""
    dv = client.call("engram_derive", {
        "claim": claim,
        "supporting_ids": ",".join(ids),
        "logical_chain": "Premises jointly support the claim.",
        "reasoning_type": "inductive_causal",
    })
    assert dv.get("status") == "created", f"derive failed: {dv}"
    return dv, dv.get("structure_warnings", [])


def _standpoint_line(warnings):
    lines = [w for w in warnings if w.startswith("STANDPOINT:")]
    return lines[0] if lines else ""


def _falsification_line(warnings):
    lines = [w for w in warnings if w.startswith("FALSIFICATION:")]
    return lines[0] if lines else ""


def _composite_line(warnings):
    lines = [w for w in warnings if w.startswith("⚠⚠")]
    return lines[0] if lines else ""


# ---------------------------------------------------------------------------
# Test 1 (known-good control): different lineages → diverse, no escalation
# ---------------------------------------------------------------------------

def test_cross_lineage_diverse_no_warning():
    client = fresh_client()
    o1 = _obs(client, 1, lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, lineage="openai:gpt", author="a2", collection="c2")
    assert _read_lineage(o1) == "anthropic:claude"

    _dv, warnings = _derive(client, [o1, o2])
    sp = _standpoint_line(warnings)
    assert "lineage: 2 clusters (diverse)" in sp, f"expected diverse lineage, got: {sp}"
    assert "⚠ uniform — shared training lineage" not in sp
    assert _composite_line(warnings) == "", f"no ⚠⚠ expected: {warnings}"
    print(f"  PASS: cross-lineage diverse: {sp[:100]}")


# ---------------------------------------------------------------------------
# Test 2 (known-good control): mixed F-S classes → ratio line, no ⚠⚠
# ---------------------------------------------------------------------------

def test_mixed_fs_ratio_no_composite():
    client = fresh_client()
    o1 = _obs(client, 1, quote_type="hard_data", lineage="anthropic:claude",
              author="a1", collection="c1")
    o2 = _obs(client, 2, quote_type="editorial", lineage="anthropic:claude",
              author="a2", collection="c2")

    _dv, warnings = _derive(client, [o1, o2])
    fl = _falsification_line(warnings)
    assert "1/2 re-executable-leaning" in fl, f"expected re-exec ratio, got: {fl}"
    assert "1/2 frozen-leaning" in fl, f"expected frozen ratio, got: {fl}"
    # A Class-1 premise exists → composite must NOT fire even though lineage
    # is uniform.
    assert _composite_line(warnings) == "", f"no ⚠⚠ with a Class-1 premise: {warnings}"
    print(f"  PASS: mixed F-S ratio: {fl[:100]}")


# ---------------------------------------------------------------------------
# Test 3 (known-bad probe): same lineage, graph has >= 2 distinct → ⚠ fires
# ---------------------------------------------------------------------------

def test_uniform_lineage_warning_fires_when_actionable():
    client = fresh_client()
    # A third, unrelated cross-lineage observation makes the graph
    # demonstrate >= 2 distinct lineages — the ⚠ is actionable.
    _obs(client, 9, lineage="openai:gpt")
    o1 = _obs(client, 1, lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, lineage="anthropic:claude", author="a2", collection="c2")

    _dv, warnings = _derive(client, [o1, o2])
    sp = _standpoint_line(warnings)
    assert "lineage: 1 cluster (⚠ uniform — shared training lineage" in sp, (
        f"expected strengthened uniform warning, got: {sp}"
    )
    assert "zero independent corroboration on substrate-prior bias" in sp
    print(f"  PASS: uniform lineage ⚠ (actionable graph): {sp[:120]}")


# ---------------------------------------------------------------------------
# Test 4 (known-bad probe): one premise NULL lineage → axis omitted
# ---------------------------------------------------------------------------

def test_partial_lineage_axis_omitted():
    client = fresh_client()
    o1 = _obs(client, 1, lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, lineage="", author="a2", collection="c2")  # no lineage

    _dv, warnings = _derive(client, [o1, o2])
    sp = _standpoint_line(warnings)
    # author/collection still report; lineage axis is omitted entirely
    # (all-or-omit: never assert uniformity from one premise's data).
    assert "author:" in sp, f"author axis should report, got: {sp}"
    assert "lineage" not in sp, f"lineage axis must be omitted on partial data: {sp}"
    print(f"  PASS: partial lineage omitted: {sp[:100]}")


# ---------------------------------------------------------------------------
# Test 5 (regression pin): author-diverse + lineage-uniform → lineage verdict
# independent — the exact v2 blindness v3 exists to close
# ---------------------------------------------------------------------------

def test_author_diverse_lineage_uniform_caught():
    client = fresh_client()
    _obs(client, 9, lineage="openai:gpt")  # make the ⚠ actionable
    o1 = _obs(client, 1, lineage="anthropic:claude", author="ariadne", collection="c1")
    o2 = _obs(client, 2, lineage="anthropic:claude", author="borges", collection="c2")
    o3 = _obs(client, 3, lineage="anthropic:claude", author="luria", collection="c3")

    _dv, warnings = _derive(client, [o1, o2, o3])
    sp = _standpoint_line(warnings)
    assert "author: 3 clusters (diverse)" in sp, f"author should be diverse: {sp}"
    assert "lineage: 1 cluster (⚠ uniform" in sp, (
        f"lineage uniformity must be caught despite author diversity: {sp}"
    )
    print(f"  PASS: v2 blindness pinned: {sp[:140]}")


# ---------------------------------------------------------------------------
# Test 6 (known-bad probe): lineage-uniform + all-frozen → ⚠⚠ fires w/ label
# ---------------------------------------------------------------------------

def test_composite_escalation_fires_all_frozen_uniform():
    client = fresh_client()
    o1 = _obs(client, 1, quote_type="editorial", lineage="anthropic:claude",
              author="a1", collection="c1")
    o2 = _obs(client, 2, quote_type="personal_communication",
              lineage="anthropic:claude", author="a2", collection="c2")

    _dv, warnings = _derive(client, [o1, o2])
    comp = _composite_line(warnings)
    assert comp, f"⚠⚠ expected for all-frozen + uniform: {warnings}"
    assert "treat as single-witness" in comp
    assert "[F-S via quote_type-proxy — Phase-2 field may revise]" in comp, (
        f"⚠⚠ fired on proxy input must carry the proxy label: {comp}"
    )
    # Single-lineage graph → Class-1 remedy form (no cross-lineage clause).
    assert "cross-lineage support" not in comp, (
        f"single-lineage graph must lead with the Class-1 remedy: {comp}"
    )
    print(f"  PASS: ⚠⚠ composite: {comp[:140]}")


def test_composite_cross_lineage_remedy_when_actionable():
    client = fresh_client()
    _obs(client, 9, lineage="openai:gpt")  # graph now has 2 distinct lineages
    o1 = _obs(client, 1, quote_type="editorial", lineage="anthropic:claude",
              author="a1", collection="c1")
    o2 = _obs(client, 2, quote_type="editorial", lineage="anthropic:claude",
              author="a2", collection="c2")

    _dv, warnings = _derive(client, [o1, o2])
    comp = _composite_line(warnings)
    assert comp, f"⚠⚠ expected: {warnings}"
    assert "cross-lineage support" in comp, (
        f"multi-lineage graph keeps the cross-lineage remedy clause: {comp}"
    )
    print(f"  PASS: ⚠⚠ cross-lineage remedy: {comp[:140]}")


# ---------------------------------------------------------------------------
# Test 7 (known-bad probe): malformed lineage → redirecting error, unwritten
# ---------------------------------------------------------------------------

def test_malformed_lineage_redirecting_rejection():
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/bad",
        "title": "Bad lineage",
        "quoted_text": "Some quoted text here.",
        "interpretation": "Interp.",
        "claim": "Some claim",
        "quote_type": "hard_data",
        "standpoint_lineage": "Claude",  # malformed: no provider:family shape
    })
    assert "error" in r, f"malformed lineage must be rejected: {r}"
    # The error REDIRECTS: names the expected shape and gives an example.
    assert "provider:family" in r["error"], f"error must name the shape: {r}"
    assert "anthropic:claude" in r["error"], f"error must give an example: {r}"
    assert 'got "Claude"' in r["error"], f"error must echo the bad value: {r}"

    # Row unwritten: nothing in the graph carries the malformed value.
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE standpoint_lineage = 'Claude'"
        ).fetchone()[0]
        assert n == 0, "malformed lineage must not be written"
    finally:
        conn.close()
    print(f"  PASS: redirecting rejection: {r['error'][:100]}")


# ---------------------------------------------------------------------------
# Test 8 (invariant pin): proxy label never dropped from FALSIFICATION
# ---------------------------------------------------------------------------

def test_proxy_label_invariant():
    client = fresh_client()
    o1 = _obs(client, 1, quote_type="hard_data", lineage="anthropic:claude",
              author="a1", collection="c1")
    o2 = _obs(client, 2, quote_type="editorial", lineage="anthropic:claude",
              author="a2", collection="c2")

    _dv, warnings = _derive(client, [o1, o2])
    fl = _falsification_line(warnings)
    assert fl, f"FALSIFICATION line expected: {warnings}"
    # Phase 1: every classed segment carries the proxy label — no premise is
    # presented as bare "re-executable" from proxy data.
    for segment in ("re-executable-leaning", "frozen-leaning"):
        if segment in fl:
            idx = fl.index(segment)
            tail = fl[idx:idx + len(segment) + 25]
            assert "(proxy:quote_type)" in tail, (
                f"segment '{segment}' missing proxy label: {fl}"
            )
    print(f"  PASS: proxy-label invariant: {fl[:120]}")


# ---------------------------------------------------------------------------
# Test 9 (both directions): unknowns suppress ⚠⚠; known-frozen fires it
# ---------------------------------------------------------------------------

def test_verified_discharges_composite():
    """Component D verified-retarget (Borges #721/#730, Luria #723): ⚠⚠ fires on
    ZERO state-3 (verified) leaves — not "zero re-executable" — and the old
    n_unknown honesty floor is dropped (verified-status is fully determinate, no
    unknown bucket). So a non-verified premise of ANY F-S class (incl. an
    'unknown'-proxy attributed_analysis leaf) contributes to zero-verified and
    does NOT suppress ⚠⚠; only a VERIFIED leaf (hard_data / official_statement)
    discharges it."""
    # Direction 1: own all-unverified incl. an attributed_analysis ("unknown"
    # F-S) leaf + uniform lineage → ⚠⚠ FIRES (the dropped n_unknown floor means
    # an unknown no longer suppresses — verified-status, not F-S, is the gate).
    client = fresh_client()
    o1 = _obs(client, 1, quote_type="attributed_analysis",
              lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, quote_type="editorial", lineage="anthropic:claude",
              author="a2", collection="c2")
    _dv, warnings = _derive(client, [o1, o2])
    assert _composite_line(warnings), (
        f"⚠⚠ must fire: zero verified leaves + uniform lineage, even with an "
        f"'unknown'-F-S premise (n_unknown floor dropped): {warnings}"
    )

    # Direction 2: one VERIFIED (hard_data → state-3) leaf discharges ⚠⚠ —
    # verifi-ED, not merely verifi-able. (A re-executable-but-unverified leaf
    # would NOT discharge; hard_data is the state-3 content-type.)
    client2 = fresh_client()
    v1 = _obs(client2, 1, quote_type="hard_data",
              lineage="anthropic:claude", author="a1", collection="c1")
    e2 = _obs(client2, 2, quote_type="editorial", lineage="anthropic:claude",
              author="a2", collection="c2")
    _dv2, warnings2 = _derive(client2, [v1, e2])
    assert _composite_line(warnings2) == "", (
        f"a single verified (hard_data) leaf must discharge ⚠⚠: {warnings2}"
    )
    print("  PASS: verified-discharge (unverified incl. unknown-F-S fires; hard_data silences)")


# ---------------------------------------------------------------------------
# Test 10 (both directions): single-lineage actionability gate reads graph
# state, not a config flag
# ---------------------------------------------------------------------------

def test_single_lineage_gate_informational_then_live():
    client = fresh_client()
    o1 = _obs(client, 1, lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, lineage="anthropic:claude", author="a2", collection="c2")

    # Direction 1: one-lineage graph → NO standalone lineage line at all. The
    # informational line was dropped (Component B, Borges #721): under null=self
    # lineage is uniform on every own derivation, so an informational line would
    # fire constantly = the per-derivation fatigue Lei's precision gate forbids.
    # The ⚠⚠ composite carries the only actionable signal (when zero-verified).
    _dv, warnings = _derive(client, [o1, o2])
    sp = _standpoint_line(warnings)
    assert "lineage:" not in sp, (
        f"single-lineage graph must emit NO standalone lineage line "
        f"(informational dropped, Component B): {sp}"
    )
    assert "⚠ uniform — shared training lineage" not in sp, (
        f"standalone ⚠ must not fire in a single-lineage graph: {sp}"
    )

    # Direction 2: the first cross-lineage filing flips the axis live —
    # same premises, re-derived, now earn the ⚠.
    _obs(client, 9, lineage="openai:gpt")
    _dv2, warnings2 = _derive(client, [o1, o2], claim="Re-derived after cross-lineage filing")
    sp2 = _standpoint_line(warnings2)
    assert "lineage: 1 cluster (⚠ uniform — shared training lineage" in sp2, (
        f"⚠ must go live after the first cross-lineage filing: {sp2}"
    )
    print("  PASS: actionability gate both directions (graph state, not config)")


# ---------------------------------------------------------------------------
# Mira's pre-merge contract check (#48): retracted cross-lineage row must not
# activate the actionability gate (is_current filter on _graph_lineage_count)
# ---------------------------------------------------------------------------

def test_retracted_cross_lineage_does_not_activate_gate():
    """A retracted/non-current cross-lineage row is NOT a citable premise, so
    its lineage value must not make the standalone lineage warning live. Before
    the is_current filter the count included retracted rows and the gate fired
    with no citable cross-lineage premise — the exact actionability boundary the
    gate exists to honor. Discriminating: this fails pre-fix (gate fires) and
    passes post-fix (gate stays informational)."""
    client = fresh_client()
    o1 = _obs(client, 1, lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, lineage="anthropic:claude", author="a2", collection="c2")
    # File the only cross-lineage row, then RETRACT it -> non-current.
    o9 = _obs(client, 9, lineage="openai:gpt")
    r = client.call("engram_retract", {
        "node_id": o9,
        "error_type": "hallucinated_claim",
        "reason": "test: make the only cross-lineage row non-current",
    })
    assert r.get("status") in ("retracted", "created", "ok") or "retract" in str(r).lower(), \
        f"retract failed: {r}"

    # No CURRENT cross-lineage premise remains -> axis stays informational.
    _dv, warnings = _derive(client, [o1, o2], claim="After retracting the cross-lineage row")
    sp = _standpoint_line(warnings)
    # The gate stays non-activated: NO standalone \u26a0 uniform line (informational
    # line dropped in Component B, so we assert the \u26a0 is absent \u2014 that's the
    # actionability boundary this test guards). _graph_lineage_count excludes the
    # retracted row (is_current filter), so it stays < 2 \u2192 no \u26a0.
    assert "\u26a0 uniform \u2014 shared training lineage" not in sp, (
        f"\u26a0 must not fire when the only cross-lineage row is retracted: {sp}"
    )
    assert "lineage: 1 cluster" not in sp, (
        f"no live lineage \u26a0 line when the only cross-lineage row is non-current: {sp}"
    )
    print("  PASS: retracted cross-lineage row does not activate the gate (Mira #48)")


# A CURRENT but non-claim-bearing row carrying a standpoint_lineage value must
# NOT activate the gate (type='observation_factual' filter on _graph_lineage_count).
def test_noncitable_lineage_row_does_not_activate_gate():
    """A current but non-claim-bearing row (e.g. a goal) carrying a
    standpoint_lineage value is NOT a citable cross-lineage premise, so it must
    not make the standalone lineage warning live. standpoint_lineage is a
    SOURCE-provenance field set only via the observation write path; the gate
    query must ENFORCE type='observation_factual', not trust the convention that
    only observations carry the field. Discriminating: fails pre-fix (the goal's
    lineage is counted -> gate fires with no citable cross-lineage premise) and
    passes post-fix (the type filter excludes it -> gate stays informational).
    Origin: Mira #954 colleague review."""
    client = fresh_client()
    o1 = _obs(client, 1, lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, lineage="anthropic:claude", author="a2", collection="c2")
    # Inject a CURRENT non-observation (goal) row carrying a DIFFERENT lineage,
    # directly in the DB -- exactly the non-enforced path Mira probed.
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    try:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current, "
            "standpoint_lineage) VALUES (?, 'goal', ?, ?, 1, 'openai:gpt')",
            ("gl_noncite9", "a goal carrying a stray lineage value",
             "2026-06-08T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    # Sanity: the stray non-observation row is present and current.
    assert _read_lineage("gl_noncite9") == "openai:gpt", "stray goal row not written"

    # No CURRENT cross-lineage OBSERVATION premise exists -> axis stays informational.
    _dv, warnings = _derive(client, [o1, o2],
                            claim="After injecting a non-citable lineage row")
    sp = _standpoint_line(warnings)
    # The gate stays non-activated: NO standalone ⚠ uniform line (informational
    # line dropped in Component B). _graph_lineage_count enforces
    # type LIKE 'observation_%', excluding the goal row → stays < 2 → no ⚠.
    assert "⚠ uniform — shared training lineage" not in sp, (
        f"⚠ must not fire when the only other lineage is on a "
        f"non-observation row: {sp}"
    )
    assert "lineage: 1 cluster" not in sp, (
        f"no live lineage ⚠ line when the cross-lineage value is on a "
        f"non-observation (goal) row: {sp}"
    )
    print("  PASS: non-citable (non-observation) lineage row does not activate "
          "the gate (Mira #954)")


# A CURRENT predictive observation IS a citable cross-lineage premise and MUST
# activate the gate (the gate covers the whole observation family by prefix).
def test_predictive_cross_lineage_activates_gate():
    """A current predictive observation (observation_predictive) is claim-bearing
    and carries standpoint_lineage via the same observation write path, so a
    cross-lineage predictive row MUST activate the gate. The gate counts the whole
    observation FAMILY (type LIKE 'observation_%'), not a hand-list of factual-
    only — a hand-list under-counts predictive rows and re-opens this gap on every
    new observation_* type (the dv_0100/ls_0015 hand-enumerated-family trap, here
    on its third iteration). Discriminating: fails pre-fix (a factual-only filter
    excludes the predictive cross-lineage row -> gate stays informational) and
    passes post-fix (the family predicate counts it -> ⚠ goes live). Origin:
    Mira #954 second re-review."""
    client = fresh_client()
    o1 = _obs(client, 1, lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, lineage="anthropic:claude", author="a2", collection="c2")
    # The ONLY cross-lineage row is a current PREDICTIVE observation.
    rp = client.call("engram_add_observation", {
        "url": "https://example.com/pred-9",
        "title": "Predictive source 9",
        "quoted_text": "Gold will rise ten percent next quarter.",
        "interpretation": "A forward-looking claim.",
        "claim": "Gold rises ten percent next quarter",
        "quote_type": "hard_data",
        "is_predictive": True,
        "predicted_event": "Gold price up ten percent by quarter end",
        "standpoint_lineage": "openai:gpt",
    })
    assert rp.get("status") == "created", f"predictive obs failed: {rp}"
    pid = rp["observation_id"]
    # Sanity: it is typed observation_predictive and carries the lineage.
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT type, standpoint_lineage FROM nodes WHERE id = ?", (pid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["type"] == "observation_predictive", (
        f"expected observation_predictive, got {row['type']}"
    )
    assert row["standpoint_lineage"] == "openai:gpt", "predictive lineage not written"

    # A current cross-lineage OBSERVATION (predictive) exists -> gate goes live.
    _dv, warnings = _derive(
        client, [o1, o2],
        claim="Same-lineage premises with a cross-lineage predictive obs present")
    sp = _standpoint_line(warnings)
    assert "⚠ uniform — shared training lineage" in sp, (
        f"⚠ must fire when a current cross-lineage PREDICTIVE observation "
        f"exists: {sp}"
    )
    print("  PASS: cross-lineage predictive observation activates the gate "
          "(Mira #954 round 2)")


# ---------------------------------------------------------------------------
# Batch plumbing sanity: standpoint_lineage flows through the batch path
# ---------------------------------------------------------------------------

def test_batch_lineage_plumbing():
    client = fresh_client()
    r = client.call("engram_add_observation_batch", {
        "observations_json": json.dumps([
            {
                "quoted_text": "Batch quoted text one.",
                "interpretation": "Interp 1.",
                "claim": "Batch claim 1",
                "quote_type": "hard_data",
                "standpoint_lineage": "anthropic:claude",
            },
        ]),
        "url": "https://example.com/batch",
        "title": "Batch source",
    })
    results = r.get("results", [])
    assert results and results[0].get("status") == "created", f"batch failed: {r}"
    assert _read_lineage(results[0]["observation_id"]) == "anthropic:claude"
    print("  PASS: batch lineage plumbing")


# ---------------------------------------------------------------------------
# Phase-2 D3 tests (fs_class native field — dual-probe discipline per spec)
# ---------------------------------------------------------------------------

def _obs_fs(client, n, fs_class=None, quote_type="hard_data",
            lineage="anthropic:claude", author="", collection=""):
    """File an observation with an optional native fs_class field."""
    payload = {
        "url": f"https://example.com/source-fs-{n}",
        "title": f"Source FS {n}",
        "quoted_text": f"Measured FS value number {n} is {n * 3}.",
        "interpretation": f"FS measurement {n}.",
        "claim": f"FS value number {n} is {n * 3}",
        "quote_type": quote_type,
    }
    if lineage:
        payload["standpoint_lineage"] = lineage
    if author:
        payload["standpoint_author_id"] = author
    if collection:
        payload["standpoint_collection_id"] = collection
    if fs_class is not None:
        payload["fs_class"] = fs_class
    r = client.call("engram_add_observation", payload)
    assert r.get("status") == "created", f"obs_fs {n} failed: {r}"
    return r["observation_id"]


def _read_fs_class_from_db(node_id):
    """Read fs_class directly from DB (None if NULL)."""
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT fs_class FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return row["fs_class"] if row else None
    finally:
        conn.close()


def _node_fs_class_via_core(node_id):
    """Call _node_fs_class directly; returns (fs_class, source)."""
    import server as _server
    conn = _server.core._get_db()
    return _server.core._node_fs_class(conn, node_id)


# Phase-2 D3 test 11 — known-good control probe (new-field direction)
# Guards against a seam that always outputs "proxy:quote_type".
def test_fs_class_native_field_no_proxy_label():
    client = fresh_client()
    o1 = _obs_fs(client, 1, fs_class="re-executable",
                 lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs_fs(client, 2, fs_class="re-executable",
                 lineage="anthropic:claude", author="a2", collection="c2")

    # _node_fs_class must return source="field" for a native-filed node.
    fc, src = _node_fs_class_via_core(o1)
    assert fc == "re-executable", f"expected re-executable, got: {fc}"
    assert src == "field", f"expected source='field', got: {src}"

    # DB column must be populated (not NULL).
    assert _read_fs_class_from_db(o1) == "re-executable", "DB column should be populated"

    # Derivation: FALSIFICATION reads "re-executable" with no proxy label and
    # no "-leaning" qualifier (spec: "FALSIFICATION: 1/1 re-executable").
    _dv, warnings = _derive(client, [o1, o2])
    fl = _falsification_line(warnings)
    assert fl, f"FALSIFICATION line expected: {warnings}"
    assert "(proxy:quote_type)" not in fl, (
        f"proxy label must be absent when source='field': {fl}"
    )
    assert "re-executable-leaning" not in fl, (
        f'"-leaning" qualifier must drop when all premises are native: {fl}'
    )
    assert "re-executable" in fl, f"re-executable label expected: {fl}"
    print(f"  PASS: native field no proxy label: {fl[:120]}")


# Phase-2 D3 test 12 — known-bad probe (proxy-fallback direction)
# Guards against a seam that always outputs "field".
def test_fs_class_absent_falls_back_to_proxy():
    client = fresh_client()
    # No fs_class → Phase-1 proxy must apply.
    o1 = _obs(client, 1, quote_type="hard_data",
              lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, quote_type="editorial",
              lineage="anthropic:claude", author="a2", collection="c2")

    # _node_fs_class must return source="proxy:quote_type" (no native field).
    fc, src = _node_fs_class_via_core(o1)
    assert src == "proxy:quote_type", f"expected proxy source, got: {src}"

    # DB column must be NULL (the never-persisted Phase-1 property).
    assert _read_fs_class_from_db(o1) is None, "DB column must be NULL when fs_class not provided"

    # Derivation: FALSIFICATION carries the proxy label and "-leaning".
    _dv, warnings = _derive(client, [o1, o2])
    fl = _falsification_line(warnings)
    assert fl, f"FALSIFICATION line expected: {warnings}"
    assert "(proxy:quote_type)" in fl, (
        f"proxy label must be present when source is proxy: {fl}"
    )
    assert "re-executable-leaning" in fl or "frozen-leaning" in fl, (
        f'"-leaning" qualifier expected for proxy-sourced premises: {fl}'
    )
    print(f"  PASS: absent fs_class falls back to proxy: {fl[:120]}")


# Phase-2 D3 test 13 — mixed-premise any_proxy precision
# Guards against the ob_0140 failure mode: scanning ALL premises (including
# unknowns) instead of only known_fs. The unknown premise returns
# source="proxy:quote_type" but must NOT poison the known premise's label.
def test_mixed_native_and_null_any_proxy_precision():
    client = fresh_client()
    # Premise 1: native fs_class — source will be "field".
    o1 = _obs_fs(client, 1, fs_class="re-executable",
                 lineage="anthropic:claude", author="a1", collection="c1")
    # Premise 2: no fs_class, attributed_analysis → proxy maps to "unknown".
    # _node_fs_class returns ("unknown", "proxy:quote_type") for this node.
    # It is EXCLUDED from known_fs (c == "unknown"). So any_proxy scans
    # known_fs and finds only the native premise → any_proxy = False.
    o2 = _obs_fs(client, 2, fs_class=None, quote_type="attributed_analysis",
                 lineage="anthropic:claude", author="a2", collection="c2")

    _dv, warnings = _derive(client, [o1, o2])
    fl = _falsification_line(warnings)
    assert fl, f"FALSIFICATION line expected: {warnings}"
    # known_fs contains only o1 (native); any_proxy = False over known_fs.
    # So the known re-executable premise gets no proxy label and no "-leaning".
    assert "(proxy:quote_type)" not in fl, (
        f"unknown premise must not poison known premise's proxy label: {fl}"
    )
    assert "re-executable-leaning" not in fl, (
        f'"-leaning" must not appear when known premises are all native: {fl}'
    )
    assert "re-executable" in fl, f"re-executable label expected: {fl}"
    print(f"  PASS: mixed-premise any_proxy precision: {fl[:120]}")


# Phase-2 D3 test 15 — per-source split (Ariadne #41/614 finding, choice (a))
# Guards against mixed-source sets being collapsed under a single aggregate
# proxy label. Each (class, source) pair renders independently.
def test_mixed_source_per_source_split():
    client = fresh_client()
    # Premise 1: native fs_class="re-executable" → source="field"
    o1 = _obs_fs(client, 1, fs_class="re-executable",
                 lineage="anthropic:claude", author="a1", collection="c1")
    # Premise 2: no fs_class, hard_data → proxy "re-executable", source="proxy:quote_type"
    o2 = _obs_fs(client, 2, fs_class=None, quote_type="hard_data",
                 lineage="anthropic:claude", author="a2", collection="c2")

    _dv, warnings = _derive(client, [o1, o2])
    fl = _falsification_line(warnings)
    assert fl, f"FALSIFICATION line expected: {warnings}"

    # Native segment: "1/2 re-executable" — no proxy label, no "-leaning".
    assert "1/2 re-executable" in fl, f"native segment expected: {fl}"
    # Proxy segment: "1/2 re-executable-leaning (proxy:quote_type)".
    assert "1/2 re-executable-leaning (proxy:quote_type)" in fl, (
        f"proxy segment expected: {fl}"
    )
    # No aggregate collapse: the native segment must NOT carry the proxy label.
    # Verify by checking the label appears exactly once (for the proxy segment).
    assert fl.count("(proxy:quote_type)") == 1, (
        f"proxy label should appear exactly once (proxy segment only): {fl}"
    )
    print(f"  PASS: mixed-source per-source split: {fl[:140]}")


# Phase-2 D3 test 14 — proxy-label DROP (test-8 Phase-2 mirror, Ariadne #41/606)
# Guards against the stale-label failure mode: proxy label persisting after
# the seam swap. All premises have native fs_class data → no proxy label, no
# "-leaning". Mirrors test_proxy_label_invariant (test 8) in the DROP direction.
def test_proxy_label_drops_when_all_native():
    client = fresh_client()
    # Three premises all with native fs_class="re-executable".
    o1 = _obs_fs(client, 1, fs_class="re-executable",
                 lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs_fs(client, 2, fs_class="re-executable",
                 lineage="anthropic:claude", author="a2", collection="c2")
    o3 = _obs_fs(client, 3, fs_class="re-executable",
                 lineage="anthropic:claude", author="a3", collection="c3")

    _dv, warnings = _derive(client, [o1, o2, o3])
    fl = _falsification_line(warnings)
    assert fl, f"FALSIFICATION line expected: {warnings}"
    # The proxy label must be completely absent — stale-label failure mode check.
    assert "(proxy:quote_type)" not in fl, (
        f"proxy label MUST be absent when all premises have native fs_class: {fl}"
    )
    assert "re-executable-leaning" not in fl, (
        f'"-leaning" MUST drop when no proxy source is in known_fs: {fl}'
    )
    assert "re-executable" in fl, f"re-executable label expected: {fl}"
    print(f"  PASS: proxy label drops when all native (test-8 mirror): {fl[:120]}")


# ---------------------------------------------------------------------------
# Test 13 (Ariadne #49) — layered-derivation closure-walk.
# Standpoint + F-S must aggregate over OBSERVATION leaves, not the direct
# (derivation) premises. Pins the bug where a derivation-of-derivations
# silently produced NO FALSIFICATION/⚠⚠ line: the old code read fs_class /
# standpoint off the intermediate derivations (fs=unknown, no standpoint)
# instead of the observation leaves where quote_type + standpoint live.
# Luria's #713 sign-off invariants: (1) the walk terminates AT observations
# (never ev_, never the intermediate derivation); (2) F-S reads the leaves,
# so a layered derivation fires where the direct-premise read would miss it.
# ---------------------------------------------------------------------------

def _obs_leaves_via_core(node_id):
    """Call _trace_observation_leaves directly; returns the leaf-id set."""
    import server as _server
    conn = _server.core._get_db()
    return _server.core._trace_observation_leaves(conn, node_id)


def test_layered_derivation_closure_walk_to_observation_leaves():
    client = fresh_client()
    # Four frozen (editorial→frozen proxy), same-lineage observations.
    o1 = _obs(client, 1, quote_type="editorial", lineage="anthropic:claude", author="a1", collection="c1")
    o2 = _obs(client, 2, quote_type="editorial", lineage="anthropic:claude", author="a2", collection="c2")
    o3 = _obs(client, 3, quote_type="editorial", lineage="anthropic:claude", author="a3", collection="c3")
    o4 = _obs(client, 4, quote_type="editorial", lineage="anthropic:claude", author="a4", collection="c4")

    # Two intermediate derivations, then a FINAL derivation over the derivations.
    dvA, _ = _derive(client, [o1, o2], claim="Intermediate A")
    dvB, _ = _derive(client, [o3, o4], claim="Intermediate B")
    dvA_id, dvB_id = dvA["derivation_id"], dvB["derivation_id"]

    # BEHAVIORAL regression FIRST (so it fails for the right reason on old code):
    # the final derivation's DIRECT premises are derivations (dvA, dvB) —
    # fs=unknown, no standpoint. The old direct-premise read produced NO
    # FALSIFICATION/⚠⚠ line at all; the leaf-walk reaches the four observations,
    # so the lines fire. (Invariant 2, Luria #713 — F-S reads the leaves.)
    _dvFinal, warnings = _derive(client, [dvA_id, dvB_id], claim="Layered conclusion")
    fl = _falsification_line(warnings)
    assert fl, (
        "FALSIFICATION MUST fire on a layered derivation via the leaf-walk; "
        f"empty means it regressed to the direct-derivation premises: {warnings}"
    )
    # Denominator is the observation-leaf count (4), not the direct-premise count (2).
    assert "/4 frozen" in fl, f"expected 4 frozen leaves in the ratio: {fl}"
    # ⚠⚠ composite fires: all four leaves frozen + uniform lineage, reached via
    # the leaf-walk (lineage_uniform computed over leaves).
    assert _composite_line(warnings), (
        f"⚠⚠ composite expected (all-frozen + uniform-lineage leaves): {warnings}"
    )

    # Invariant 1 (Luria #713): the walk terminates AT observations — returns
    # the observation leaves, never the intermediate derivation, never ev_ nodes.
    leaves_A = _obs_leaves_via_core(dvA_id)
    assert leaves_A == {o1, o2}, f"expected leaves {{{o1},{o2}}}, got {leaves_A}"
    assert all(lid.startswith("ob_") for lid in leaves_A), (
        f"leaves must be observations (ob_), not ev_/dv_: {leaves_A}"
    )
    assert dvA_id not in leaves_A, "the intermediate derivation must not be its own leaf"

    # Control: a NON-layered derivation directly over observations still fires
    # (obs_leaves == direct premises → identical behavior, no regression).
    _flat, warnings_flat = _derive(client, [o1, o2], claim="Flat control")
    assert _falsification_line(warnings_flat), (
        f"non-layered control must still fire FALSIFICATION: {warnings_flat}"
    )
    print(f"  PASS: layered closure-walk fires F-S+⚠⚠ over observation leaves: {fl[:90]}")


# ---------------------------------------------------------------------------
# Test 14 (Component A — Borges #721) — null=self-lineage.
# An unmarked observation counts as the filer's own lineage (config
# self_lineage), so own-unmarked derivations make the gate fire. Both
# directions: configured → fires; unset (default "") → safe-degrades closed
# (Luria #723 deployment note).
# ---------------------------------------------------------------------------

def _set_self_lineage(value):
    """Write self_lineage into the live test config so _self_lineage() reads it."""
    import server as _server
    cp = _server.core.CONFIG_PATH
    cfg = json.loads(cp.read_text())
    cfg["self_lineage"] = value
    cp.write_text(json.dumps(cfg))


def test_null_self_lineage_makes_gate_fire():
    # Direction 1: self_lineage set → fully-unmarked frozen obs → null=self
    # synthesizes a uniform lineage at read-time → ⚠⚠ fires (zero verified +
    # uniform). DB lineage column stays NULL (synthesis is read-time only).
    client = fresh_client()
    _set_self_lineage("anthropic:claude")
    o1 = _obs(client, 1, quote_type="editorial")   # no lineage/author/collection
    o2 = _obs(client, 2, quote_type="editorial")
    _dv, warnings = _derive(client, [o1, o2], claim="Own-unmarked derivation")
    assert _composite_line(warnings), (
        f"null=self must make ⚠⚠ fire on own-unmarked frozen premises: {warnings}"
    )
    # DB column stays NULL — null=self is read-time only (Luria #723: gate count
    # unaffected).
    assert _read_lineage(o1) in (None, ""), (
        f"null=self must NOT write the synthesized lineage to the DB: {_read_lineage(o1)!r}"
    )

    # Direction 2: self_lineage unset → null=self does NOT engage → cluster_key
    # returns None for all-unmarked premises → gate stays closed (safe
    # degradation, same as pre-feature).
    client2 = fresh_client()
    _set_self_lineage("")
    u1 = _obs(client2, 1, quote_type="editorial")
    u2 = _obs(client2, 2, quote_type="editorial")
    _dv2, warnings2 = _derive(client2, [u1, u2], claim="Own-unmarked, self_lineage unset")
    assert _composite_line(warnings2) == "", (
        f"unset self_lineage must safe-degrade to a closed gate: {warnings2}"
    )
    assert _standpoint_line(warnings2) == "", (
        f"all-unmarked + no self_lineage → no STANDPOINT line: {warnings2}"
    )
    print("  PASS: null=self both directions (configured fires; unset safe-degrades)")


def test_malformed_self_lineage_fails_closed():
    # Mira #750: a malformed self_lineage (e.g. "Claude" instead of
    # "anthropic:claude") must NOT produce false diversity when paired with a
    # correctly-marked same-lineage premise.  The guard in _self_lineage() must
    # return "" for non-matching values (fail-closed), so null=self does not
    # engage and the composite ⚠⚠ does not fire spuriously.
    client = fresh_client()
    _set_self_lineage("Claude")  # malformed: uppercase, no colon
    own_unmarked = _obs(client, 1, quote_type="editorial")  # would be null=self if guard absent
    own_marked = _obs(client, 2, quote_type="editorial", lineage="anthropic:claude")
    _dv, warnings = _derive(client, [own_unmarked, own_marked], claim="Malformed self_lineage test")
    # With the guard active, null=self does not engage → own_unmarked has no
    # lineage cluster → system sees one-axis-marked + one-unmarked →
    # lineage axis is NOT reported as diverse.
    composite = _composite_line(warnings)
    standpoint = _standpoint_line(warnings)
    assert "2 clusters (diverse)" not in standpoint, (
        f"malformed self_lineage must not produce false diversity: {warnings}"
    )
    print("  PASS: malformed self_lineage fails closed, no false diversity")


# ===========================================================================
# Composition stress-suite (folded from Borges's pre-P4 sandbox, #49 / #738).
# Stresses the INTERACTIONS the unit tests cover only in isolation:
# leaf-walk × verified-proxy × predictive × lineage, across derivation depth.
# Permanent regression coverage for the 4-component fix's compositions —
# validated 6/6 by Borges on a5dcccf before folding.
# ===========================================================================

def _rich_obs(client, quote_type="editorial", lineage="", predictive=False, fs_class=None):
    """Observation with predictive / fs_class fields the _obs helper omits."""
    import itertools
    _rich_obs._c = getattr(_rich_obs, "_c", itertools.count(1000))
    i = next(_rich_obs._c)
    p = {"url": f"https://example.com/s{i}", "title": f"S{i}",
         "quoted_text": f"Datum {i} reads value {i*3}.", "interpretation": f"Reading {i}.",
         "claim": f"Datum {i} equals {i*3}", "quote_type": quote_type}
    if lineage:
        p["standpoint_lineage"] = lineage
    if predictive:
        p["is_predictive"] = True
        p["predicted_event"] = f"Datum {i} confirmed"
    if fs_class:
        p["fs_class"] = fs_class
    r = client.call("engram_add_observation", p)
    assert r.get("status") == "created", f"obs failed: {r}"
    return r["observation_id"]


def _compose(client, ids, claim="Derived"):
    """Derive wrapper allowing a single premise + default reasoning_type — the
    composition tests build derivation chains incl. single-premise mids."""
    r = client.call("engram_derive", {"claim": claim, "supporting_ids": ",".join(ids),
                                      "logical_chain": "Premises jointly support the claim."})
    w = r.get("warnings") or r.get("structure_warnings") or []
    return r["derivation_id"], w


def _fired(w):
    return bool(_composite_line(w))


def test_S1_layered_predictive_official_does_not_discharge():
    """dvTop ← [dvMid←(official_statement+predictive[state-2] + editorial), editorial].
    A predictive official-statement 2 levels down is NOT verified → ⚠⚠ fires THROUGH
    the leaf-walk (the deep predictive leaf does not discharge)."""
    c = fresh_client(); _set_self_lineage("anthropic:claude")
    o_pred = _rich_obs(c, "official_statement", predictive=True)
    o_ed = _rich_obs(c, "editorial")
    mid, _ = _compose(c, [o_pred, o_ed], "Mid")
    o_top2 = _rich_obs(c, "editorial")
    _top, w = _compose(c, [mid, o_top2], "Top")
    assert _fired(w), f"predictive-official (deep leaf) must not discharge through leaf-walk: {w}"
    print("  PASS: S1 layered predictive-official does not discharge")


def test_S2_multibranch_lone_verified_anchor_discharges():
    """dvTop ← [dvA←(editorial,personal_comm) + dvB←(hard_data)]. One hard_data leaf
    anywhere in the closure → silent."""
    c = fresh_client(); _set_self_lineage("anthropic:claude")
    a1, a2 = _rich_obs(c, "editorial"), _rich_obs(c, "personal_communication")
    dvA, _ = _compose(c, [a1, a2], "Branch A")
    b1 = _rich_obs(c, "hard_data")
    dvB, _ = _compose(c, [b1], "Branch B")
    _top, w = _compose(c, [dvA, dvB], "Top")
    assert not _fired(w), f"one verified leaf anywhere must discharge: {w}"
    print("  PASS: S2 multibranch lone verified anchor discharges")


def test_S3_cross_lineage_leaf_at_depth_discharges_via_diversity():
    """dvTop ← [dvMid←(own editorial ×2) + ob(openai:gpt)]. Diverse lineage across
    depths → silent even though all-unverified."""
    c = fresh_client(); _set_self_lineage("anthropic:claude")
    m1, m2 = _rich_obs(c, "editorial"), _rich_obs(c, "editorial")
    dvMid, _ = _compose(c, [m1, m2], "Own mid")
    foreign = _rich_obs(c, "editorial", lineage="openai:gpt")
    _top, w = _compose(c, [dvMid, foreign], "Top")
    assert not _fired(w), f"cross-lineage leaf must discharge via diversity: {w}"
    print("  PASS: S3 cross-lineage leaf at depth discharges via diversity")


def test_S4_recall_precision_pair_and_no_fatigue():
    """Own all-unverified → fires (recall); own +1 hard_data → silent (precision)
    with no standalone STANDPOINT fatigue line."""
    c = fresh_client(); _set_self_lineage("anthropic:claude")
    u1, u2 = _rich_obs(c, "editorial"), _rich_obs(c, "personal_communication")
    _, w_fire = _compose(c, [u1, u2], "Own all-unverified")
    assert _fired(w_fire), f"recall: own all-unverified must fire: {w_fire}"
    v1 = _rich_obs(c, "hard_data")
    _, w_silent = _compose(c, [u1, v1], "Own with one verified")
    assert not _fired(w_silent), f"precision: one verified must silence: {w_silent}"
    assert _standpoint_line(w_silent) == "", f"no STANDPOINT fatigue line: {w_silent}"
    print("  PASS: S4 recall+precision pair, no fatigue")


def test_S5_re_executable_but_unverified_still_fires():
    """fs_class=re-executable (native, high F-S) + attributed_analysis (NOT verified)
    → ⚠⚠ fires. The verifiable≠verified core: high F-S ≠ discharge."""
    c = fresh_client(); _set_self_lineage("anthropic:claude")
    re1 = _rich_obs(c, "attributed_analysis", fs_class="re-executable")
    re2 = _rich_obs(c, "attributed_analysis", fs_class="re-executable")
    _, w = _compose(c, [re1, re2], "Own re-executable-but-unverified")
    assert _fired(w), f"high-F-S-but-unverified must NOT discharge: {w}"
    print("  PASS: S5 re-executable-but-unverified still fires (verifiable≠verified)")


def test_S6_fire_rate_precision_sanity():
    """Mixed batch: exactly the all-unverified derivations fire (no over-fire)."""
    c = fresh_client(); _set_self_lineage("anthropic:claude")
    specs = [("hard_data", "hard_data"),
             ("editorial", "editorial"),
             ("official_statement", "editorial"),
             ("personal_communication", "editorial"),
             ("hard_data", "editorial")]
    fire = 0
    for qa, qb in specs:
        x, y = _rich_obs(c, qa), _rich_obs(c, qb)
        _, w = _compose(c, [x, y])
        fire += 1 if _fired(w) else 0
    assert fire == 2, f"expected exactly 2/5 to fire (the all-unverified), got {fire}/5"
    print("  PASS: S6 fire-rate sanity 2/5")


if __name__ == "__main__":
    test_cross_lineage_diverse_no_warning()
    test_mixed_fs_ratio_no_composite()
    test_uniform_lineage_warning_fires_when_actionable()
    test_partial_lineage_axis_omitted()
    test_author_diverse_lineage_uniform_caught()
    test_composite_escalation_fires_all_frozen_uniform()
    test_composite_cross_lineage_remedy_when_actionable()
    test_malformed_lineage_redirecting_rejection()
    test_proxy_label_invariant()
    test_verified_discharges_composite()
    test_null_self_lineage_makes_gate_fire()
    test_single_lineage_gate_informational_then_live()
    test_batch_lineage_plumbing()
    # Phase-2 D3 tests
    test_fs_class_native_field_no_proxy_label()
    test_fs_class_absent_falls_back_to_proxy()
    test_mixed_native_and_null_any_proxy_precision()
    test_mixed_source_per_source_split()
    test_proxy_label_drops_when_all_native()
    # Closure-walk regression (Ariadne #49)
    test_layered_derivation_closure_walk_to_observation_leaves()
    # Composition stress-suite (folded from Borges's sandbox, #738)
    test_S1_layered_predictive_official_does_not_discharge()
    test_S2_multibranch_lone_verified_anchor_discharges()
    test_S3_cross_lineage_leaf_at_depth_discharges_via_diversity()
    test_S4_recall_precision_pair_and_no_fatigue()
    test_S5_re_executable_but_unverified_still_fires()
    test_S6_fire_rate_precision_sanity()
    print("All standpoint v3 tests passed.")
