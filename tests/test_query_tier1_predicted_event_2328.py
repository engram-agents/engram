"""Tests for #2328: the Tier-1 summary of an engram_query result must fall back
to ``predicted_event`` for a prediction node that has no ``recall_summary``.

Prediction nodes store their text in ``predicted_event``; their ``claim`` is
NULL by design.  ``_build_tiered_results`` used to fall back to ``claim`` only,
so such a node rendered a blank Tier-1 ``summary``.

Cells:
  A. ``_build_tiered_results`` directly (no graph): a long predicted_event
     truncates at 160 chars + "...", a short one renders whole, recall_summary
     still wins, claim-only nodes render byte-identically to the old
     expression, a real claim is preferred over predicted_event, Tier 2 is
     untouched.
  B. End to end through ``engram_query`` on a real temp graph.

Not covered: the ``engram_query_pattern`` caller.  Its pattern builders
hand-build candidate dicts that carry ``claim`` only (no ``predicted_event``
key), so a prediction node cannot reach the new fallback through that caller.
That is a separate blind spot, left unchanged here.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_tier1_pe_2328_")


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
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


def _build():
    fresh_server()
    import engram_query
    return engram_query._build_tiered_results


def _old_claim_summary(claim):
    """The pre-fix expression, kept verbatim as the byte-equality oracle."""
    claim = claim or ""
    return (claim[:160] + "...") if len(claim) > 160 else claim


LONG_EVENT = "zorblax event " + ("x" * 200)
SHORT_EVENT = "zorblax short event"


# -- A. direct helper --------------------------------------------------------

def test_long_predicted_event_truncated_like_claim():
    build = _build()
    node = {"id": "pr_0001", "type": "prediction", "claim": None,
            "predicted_event": LONG_EVENT, "confidence": 0.5}
    out = build([node], 1)
    assert out[0]["summary"] == LONG_EVENT[:160] + "..."
    assert len(out[0]["summary"]) == 163


def test_short_predicted_event_renders_whole():
    build = _build()
    for claim in (None, ""):
        node = {"id": "pr_0001", "type": "prediction", "claim": claim,
                "predicted_event": SHORT_EVENT}
        assert build([node], 1)[0]["summary"] == SHORT_EVENT


def test_recall_summary_still_preferred():
    build = _build()
    node = {"id": "pr_0001", "type": "prediction", "claim": None,
            "predicted_event": SHORT_EVENT, "recall_summary": "stored summary"}
    assert build([node], 1)[0]["summary"] == "stored summary"


def test_claim_only_nodes_byte_identical_to_old_expression():
    build = _build()
    for claim in ("short claim", "c" * 160, "c" * 161, "c" * 400, "", None):
        node = {"id": "ob_0001", "type": "observation_factual", "claim": claim}
        assert build([node], 1)[0]["summary"] == _old_claim_summary(claim)


def test_claim_preferred_over_predicted_event_when_both_present():
    build = _build()
    node = {"id": "x_1", "type": "observation_predictive", "claim": "the claim",
            "predicted_event": SHORT_EVENT}
    assert build([node], 1)[0]["summary"] == "the claim"


def test_neither_field_renders_empty_string():
    build = _build()
    node = {"id": "pr_0001", "type": "prediction", "claim": None}
    assert build([node], 1)[0]["summary"] == ""


def test_tier2_rendering_unchanged_for_prediction():
    build = _build()
    node = {"id": "pr_0001", "type": "prediction", "claim": None,
            "predicted_event": SHORT_EVENT}
    out = build([node], 0)[0]
    assert "summary" not in out and out == {"id": "pr_0001", "confidence": None}


# -- B. end to end through engram_query ---------------------------------------

def _add_prediction(server, predicted_event):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/pe-2328",
        "title": "Predictive source",
        "quoted_text": "zorblax appears in the source",
        "interpretation": "Test interpretation.",
        "claim": "zorblax observation claim",
        "quote_type": "attributed_analysis",
        "is_predictive": True,
        "predicted_event": predicted_event,
    })))
    assert "error" not in r, r
    assert r.get("prediction_id"), r
    pid = r["prediction_id"]
    # A prediction node has no claim/quoted_text/interpretation, and
    # predicted_event is not an FTS column, so keyword search cannot reach it
    # (only semantic search can).  Give it a recall_keywords value (an FTS
    # column; recall_summary stays empty) so the keyword path -- the one
    # runnable without an embedder -- returns it.
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET recall_keywords = ? WHERE id = ?",
            (json.dumps(["zorblax"]), pid),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _query_summaries(server, query):
    r = json.loads(server.engram_query(payload_json=json.dumps({
        "query": query, "summary_top_k": 10, "top_k": 10,
    })))
    assert "error" not in r, r
    return {e["id"]: e.get("summary") for e in r["results"]}


def test_engram_query_tier1_shows_predicted_event():
    server = fresh_server()
    pid = _add_prediction(server, LONG_EVENT)
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT claim, recall_summary, predicted_event FROM nodes WHERE id=?",
            (pid,),
        ).fetchone()
    finally:
        conn.close()
    assert row["claim"] is None and not row["recall_summary"]
    summaries = _query_summaries(server, "zorblax")
    assert pid in summaries, f"prediction not returned: {summaries}"
    assert summaries[pid] == LONG_EVENT[:160] + "..."


def test_engram_query_short_prediction_not_blank():
    server = fresh_server()
    pid = _add_prediction(server, SHORT_EVENT)
    summaries = _query_summaries(server, "zorblax")
    assert summaries.get(pid) == SHORT_EVENT
