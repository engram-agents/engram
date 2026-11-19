"""Tests for #4793: ``engram_query_pattern``'s ``stale_load_bearing`` and
``tainted_still_valid`` builders must show a prediction node's text.

A prediction keeps its text in ``predicted_event`` and has a NULL ``claim``
(#2328).  Both builders used to select ``claim`` only, so a prediction that
reached them rendered ``claim: None``.  They now use ``_tier1_fallback_text``,
the same helper the Tier-1 render uses, so the two paths cannot drift.

Cells, per builder: a prediction shows its (160-cut) predicted_event; a
prediction with both fields shows predicted_event (the helper's order, which
mirrors engram_core's ``predicted_event or claim``); a prediction with only a
claim shows the claim; a claim-only non-prediction node is byte-identical to
the old output.

Not covered, by design: the three other ``n.get("claim")`` readers in
``_surface_impl`` (special nodes, top claims, the tier A/B debug block).  They
are the auto-surface path, a separate change.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_pattern_pe_4793_")

LONG_EVENT = "quuxlar event " + ("x" * 200)
SHORT_EVENT = "quuxlar short event"


def _fresh():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    import engram_query
    return server, engram_query


def _add_prediction(server, predicted_event):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/pe-4793",
        "title": "Predictive source",
        "quoted_text": "quuxlar appears in the source",
        "interpretation": "Test interpretation.",
        "claim": "quuxlar observation claim",
        "quote_type": "attributed_analysis",
        "is_predictive": True,
        "predicted_event": predicted_event,
    })))
    assert "error" not in r, r
    return r["prediction_id"]


def _add_observation(server, claim):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/plain-4793",
        "title": "Plain source",
        "quoted_text": claim + " in the cited source",
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "error" not in r, r
    return r["observation_id"]


def _set(server, node_id, claim=..., predicted_event=...):
    conn = server._get_db()
    try:
        if claim is not ...:
            conn.execute("UPDATE nodes SET claim=? WHERE id=?", (claim, node_id))
        if predicted_event is not ...:
            conn.execute("UPDATE nodes SET predicted_event=? WHERE id=?",
                         (predicted_event, node_id))
        conn.commit()
    finally:
        conn.close()


def _make_stale(server, node_id):
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=2.0, importance_score=2.0, "
            "recall_count=0 WHERE id=?", (node_id,))
        conn.commit()
    finally:
        conn.close()


def _make_tainted(server, node_id):
    conn = server._get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO cascade_markers "
            "(node_id, cascade_event, root_node, root_op, status, created_at) "
            "VALUES (?, 'cev_pe_4793', 'some_upstream_id', 'retract', 'pending', ?)",
            (node_id, server._now()),
        )
        conn.commit()
    finally:
        conn.close()


def _run(server, qry, builder_name, node_id, prepare):
    prepare(server, node_id)
    conn = server._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        candidates = getattr(qry, builder_name)(conn, params)
    finally:
        conn.close()
    matches = [c for c in candidates if c["id"] == node_id]
    assert matches, f"{node_id} not returned by {builder_name}: {candidates}"
    return matches[0]


BUILDERS = [
    ("_pattern_stale_load_bearing", _make_stale),
    ("_pattern_tainted_still_valid", _make_tainted),
]


def test_prediction_shows_predicted_event_truncated_like_claim():
    for name, prepare in BUILDERS:
        server, qry = _fresh()
        pid = _add_prediction(server, LONG_EVENT)
        _set(server, pid, claim=None)
        cand = _run(server, qry, name, pid, prepare)
        assert cand["claim"] == LONG_EVENT[:160] + "...", (name, cand["claim"])


def test_short_prediction_renders_whole():
    for name, prepare in BUILDERS:
        server, qry = _fresh()
        pid = _add_prediction(server, SHORT_EVENT)
        _set(server, pid, claim=None)
        cand = _run(server, qry, name, pid, prepare)
        assert cand["claim"] == SHORT_EVENT, (name, cand["claim"])


def test_prediction_with_both_fields_shows_predicted_event():
    # Pins the order the helper (and engram_core's `predicted_event or claim`)
    # uses for prediction nodes.
    for name, prepare in BUILDERS:
        server, qry = _fresh()
        pid = _add_prediction(server, SHORT_EVENT)
        _set(server, pid, claim="the claim")
        cand = _run(server, qry, name, pid, prepare)
        assert cand["claim"] == SHORT_EVENT, (name, cand["claim"])


def test_prediction_with_only_a_claim_shows_the_claim():
    for name, prepare in BUILDERS:
        server, qry = _fresh()
        pid = _add_prediction(server, SHORT_EVENT)
        _set(server, pid, claim="the claim", predicted_event=None)
        cand = _run(server, qry, name, pid, prepare)
        assert cand["claim"] == "the claim", (name, cand["claim"])


def test_prediction_with_neither_field_keeps_the_old_null():
    for name, prepare in BUILDERS:
        server, qry = _fresh()
        pid = _add_prediction(server, SHORT_EVENT)
        _set(server, pid, claim=None, predicted_event=None)
        cand = _run(server, qry, name, pid, prepare)
        assert cand["claim"] is None, (name, cand["claim"])


def test_claim_only_node_is_byte_identical_to_the_old_output():
    for name, prepare in BUILDERS:
        for claim in ("short claim 4793", "c" * 160, "c" * 161, "c" * 400):
            server, qry = _fresh()
            oid = _add_observation(server, claim)
            cand = _run(server, qry, name, oid, prepare)
            old = (claim[:160] + "...") if len(claim) > 160 else claim
            assert cand["claim"] == old, (name, len(claim), cand["claim"])


def test_engram_query_pattern_tier1_summary_shows_predicted_event():
    # The user-visible path: the pattern's output carries no `claim` key, only
    # the Tier-1 `summary` that _build_tiered_results derives from it.
    for name, prepare in (("stale_load_bearing", _make_stale),
                          ("tainted_still_valid", _make_tainted)):
        server, qry = _fresh()
        pid = _add_prediction(server, LONG_EVENT)
        _set(server, pid, claim=None)
        prepare(server, pid)
        r = json.loads(qry._query_pattern_impl(pattern_name=name, preset="balanced"))
        row = next((e for e in r["results"] if e["id"] == pid), None)
        assert row is not None, (name, r)
        assert row.get("summary") == LONG_EVENT[:160] + "...", (name, row)
