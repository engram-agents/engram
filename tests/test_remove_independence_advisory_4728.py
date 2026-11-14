"""#4728 -- the duplicate independence advisory is removed; the standpoint
``⚠ inductive_generalization:`` line (engram_core._validate_reasoning_structure)
is the one lineage check, and it carries the remediation options.

Cells (all through the real engram_derive path):
  A  the derive response has no ``independence_advisory`` key in the cases the
     old advisory fired (explicit same-lineage premises; null=self premises).
  B  self_lineage set, two unmarked own observation premises, graph holds >= 2
     lineages -> the standpoint line is present.
  C  same premises, graph holds one lineage -> the line stays silent (the
     actionability gate is unchanged).
  D  the folded remediation wording is present when the line fires and absent
     everywhere in the response when it does not.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_remove_indep_adv_4728_")
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


def _set_self_lineage(value):
    """Write self_lineage into the live test config so _self_lineage() reads it."""
    import server as _server
    cp = _server.core.CONFIG_PATH
    cfg = json.loads(cp.read_text())
    cfg["self_lineage"] = value
    cp.write_text(json.dumps(cfg))


def _obs(client, n, lineage=""):
    payload = {
        "url": f"https://example.com/source-{n}",
        "title": f"Source {n}",
        "quoted_text": f"Measured value number {n} is {n * 7}.",
        "interpretation": f"Measurement {n}.",
        "claim": f"Value number {n} is {n * 7}",
        "quote_type": "hard_data",
    }
    if lineage:
        payload["standpoint_lineage"] = lineage
    r = client.call("engram_add_observation", payload)
    assert r.get("status") == "created", f"obs {n} failed: {r}"
    return r["observation_id"]


def _generalize(client, ids):
    dv = client.call("engram_derive", {
        "claim": "Generalization from premises",
        "supporting_ids": ",".join(ids),
        "logical_chain": "Premises support the generalization.",
        "reasoning_type": "inductive_generalization",
    })
    assert dv.get("status") == "created", f"derive failed: {dv}"
    return dv


def _line(result):
    # The line is one part of the joined "STANDPOINT: a; b; c" warning string,
    # so return the text from its marker to the end of that warning.
    marker = "⚠ inductive_generalization:"
    for w in result.get("structure_warnings", []):
        if marker in w:
            return w[w.index(marker):]
    return ""


# Stable fragments of the folded remediation sentence.
_FOLD_FRAGMENTS = (
    "cross-lineage observation",
    "weaker reasoning type",
    "inductive_analogy",
    "abductive_best_explanation",
    "say why in logical_chain",
)


def _own_unmarked_two_lineage_graph(client):
    """self_lineage set; two unmarked own premises; graph holds 2 explicit lineages."""
    _set_self_lineage("anthropic:opus")
    _obs(client, 8, lineage="openai:gpt")        # not premises
    _obs(client, 9, lineage="google:gemini")
    return [_obs(client, 1), _obs(client, 2)]


# -- Cell A ------------------------------------------------------------------

def test_a_explicit_same_lineage_no_advisory_key():
    c = fresh_client()
    o1 = _obs(c, 1, lineage="anthropic:sonnet")
    o2 = _obs(c, 2, lineage="anthropic:sonnet")
    result = _generalize(c, [o1, o2])
    assert "independence_advisory" not in result, (
        f"independence_advisory must be gone: {sorted(result)}"
    )


def test_a_null_self_no_advisory_key():
    c = fresh_client()
    _set_self_lineage("anthropic:opus")
    result = _generalize(c, [_obs(c, 1), _obs(c, 2)])
    assert "independence_advisory" not in result, (
        f"independence_advisory must be gone: {sorted(result)}"
    )


def test_a_deductive_no_advisory_key():
    # Green on base too (the old advisory only fired for inductive_generalization);
    # pins that the key no longer exists for any reasoning type.
    c = fresh_client()
    o1 = _obs(c, 1, lineage="anthropic:sonnet")
    o2 = _obs(c, 2, lineage="anthropic:sonnet")
    result = c.call("engram_derive", {
        "claim": "Deductive claim",
        "supporting_ids": f"{o1},{o2}",
        "logical_chain": "P, and P implies Q, therefore Q.",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert result.get("status") == "created", f"derive failed: {result}"
    assert "independence_advisory" not in result, sorted(result)


def test_a_blocked_path_no_advisory_key():
    """The old code attached the key on TWO paths: success and the early return
    when _create_derivation refuses. Refusal used here: BLOCKED_STALE, reached
    via _validate_premises inside _create_derivation, i.e. AFTER the old advisory
    computation point. Reproduced by superseding one same-lineage premise and
    citing it (a hard block with no override, so it is deterministic)."""
    c = fresh_client()
    o1 = _obs(c, 1, lineage="anthropic:sonnet")
    o2 = _obs(c, 2, lineage="anthropic:sonnet")
    o3 = _obs(c, 3, lineage="anthropic:sonnet")
    sup = c.call("engram_supersede", {
        "old_node_id": o1, "new_node_id": o3, "supersede_reason": "test",
    })
    assert sup.get("status") == "superseded", f"setup failed: {sup}"
    result = c.call("engram_derive", {
        "claim": "Generalization over a superseded premise",
        "supporting_ids": f"{o1},{o2}",
        "logical_chain": "Both premises show the pattern.",
        "reasoning_type": "inductive_generalization",
    })
    assert result.get("status") == "blocked_stale", f"expected a refusal: {result}"
    assert "independence_advisory" not in result, (
        f"independence_advisory must be gone on the blocked path: {sorted(result)}"
    )


# -- Cell B / C --------------------------------------------------------------

def test_b_line_fires_when_graph_holds_two_lineages():
    c = fresh_client()
    ids = _own_unmarked_two_lineage_graph(c)
    result = _generalize(c, ids)
    assert _line(result), (
        f"standpoint line must fire for own-unmarked premises in a "
        f"2-lineage graph: {result.get('structure_warnings')}"
    )
    assert "independence_advisory" not in result


def test_c_line_silent_when_graph_holds_one_lineage():
    """Builds: self_lineage set, two UNMARKED own observations, and no
    lineage-marked observation anywhere (stored lineage count 0; null=self is
    read-time only). The line stays silent because the graph holds fewer than
    two lineages. Cell E covers the explicitly-marked single-lineage case."""
    c = fresh_client()
    _set_self_lineage("anthropic:opus")
    result = _generalize(c, [_obs(c, 1), _obs(c, 2)])
    assert not _line(result), (
        f"standpoint line must stay silent in a single-lineage graph: "
        f"{result.get('structure_warnings')}"
    )


# -- Cell E ------------------------------------------------------------------

def _stored_lineage_count():
    import sqlite3
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    try:
        return conn.execute(
            "SELECT COUNT(DISTINCT standpoint_lineage) FROM nodes "
            "WHERE is_current = 1 AND type LIKE 'observation_%' "
            "AND standpoint_lineage IS NOT NULL AND TRIM(standpoint_lineage) != ''"
        ).fetchone()[0]
    finally:
        conn.close()


def test_e_explicit_same_lineage_single_lineage_graph_is_silent():
    """The one case where the removed advisory warned and nothing warns now:
    two observations explicitly marked with the SAME lineage, in a graph whose
    only lineage-marked observations carry that lineage (count exactly 1). The
    standpoint line's gate deliberately stays silent when no cross-lineage
    evidence exists to cite (#4728's table, last row)."""
    c = fresh_client()
    _set_self_lineage("anthropic:opus")
    o1 = _obs(c, 1, lineage="anthropic:sonnet")
    o2 = _obs(c, 2, lineage="anthropic:sonnet")
    assert _stored_lineage_count() == 1, "precondition: exactly one stored lineage"
    result = _generalize(c, [o1, o2])
    assert "independence_advisory" not in result, sorted(result)
    assert not _line(result), (
        f"no standpoint line expected in a single-lineage graph: "
        f"{result.get('structure_warnings')}"
    )
    blob = json.dumps(result, ensure_ascii=False)
    for frag in _FOLD_FRAGMENTS:
        assert frag not in blob, f"{frag!r} leaked into a silent response: {blob}"


# -- Cell D ------------------------------------------------------------------

def test_d_folded_wording_present_when_line_fires():
    c = fresh_client()
    ids = _own_unmarked_two_lineage_graph(c)
    line = _line(_generalize(c, ids))
    assert line, "precondition: the line must fire"
    for frag in _FOLD_FRAGMENTS:
        assert frag in line, f"folded wording missing {frag!r}: {line}"
    assert "= 0;" in line and "0 of 2 leaves cross-lineage" in line, (
        f"existing numbers must be kept: {line}"
    )


def test_d_folded_wording_absent_when_line_silent():
    c = fresh_client()
    _set_self_lineage("anthropic:opus")
    result = _generalize(c, [_obs(c, 1), _obs(c, 2)])
    blob = json.dumps(result, ensure_ascii=False)
    # All five fragments, inductive_analogy included: measured on this silent
    # response, the word appears only in the folded line, so its absence IS a
    # property of the silent case (an earlier draft skipped it without reason).
    for frag in _FOLD_FRAGMENTS:
        assert frag not in blob, f"{frag!r} leaked into a silent response: {blob}"
