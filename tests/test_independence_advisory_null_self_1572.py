"""#1572: the independence advisory counts unmarked observation premises as the
install's own lineage (null=self), the same read-time convention as
_standpoint_cluster_key.  Fail-closed when self_lineage is unset or malformed.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_indep_null_self_1572_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

SELF = "anthropic:opus"
OTHER = "openai:gpt"


def _fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def _set_self_lineage(value):
    import server as _server
    cp = _server.core.CONFIG_PATH
    cfg = json.loads(cp.read_text())
    cfg["self_lineage"] = value
    cp.write_text(json.dumps(cfg))


def _obs(client, n, lineage=""):
    payload = {
        "url": f"https://example.com/null-self-{n}",
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


def _check(ids):
    """Call _check_premise_independence directly on the temp graph."""
    import sqlite3
    import server as _server
    import engram_derive
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return engram_derive._check_premise_independence(conn, ids)
    finally:
        conn.close()


def _lineage_in_db(nid):
    import sqlite3
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    try:
        return conn.execute(
            "SELECT standpoint_lineage FROM nodes WHERE id = ?", (nid,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_c1_two_null_premises_fire_as_self_lineage():
    c = _fresh_client()
    _set_self_lineage(SELF)
    a, b = _obs(c, 1), _obs(c, 2)
    adv = _check([a, b])
    assert adv is not None
    assert adv["shared_lineage"] == SELF
    assert sorted(adv["null_self_premise_ids"]) == sorted([a, b])
    assert sorted(adv["collinear_premise_ids"]) == sorted([a, b])
    assert adv["collinear_verdict"] == "collinear"
    assert "null=self" in adv["message"]
    # read-time only: nothing written to the DB
    assert not (_lineage_in_db(a) or "").strip()


def test_c2_null_plus_explicit_self_is_one_cluster():
    c = _fresh_client()
    _set_self_lineage(SELF)
    a, b = _obs(c, 1), _obs(c, 2, lineage=SELF)
    adv = _check([a, b])
    assert adv is not None
    assert adv["shared_lineage"] == SELF
    assert adv["null_self_premise_ids"] == [a]
    assert sorted(adv["collinear_premise_ids"]) == sorted([a, b])


def test_c3_null_plus_explicit_other_is_diverse():
    c = _fresh_client()
    _set_self_lineage(SELF)
    a, b = _obs(c, 1), _obs(c, 2, lineage=OTHER)
    assert _check([a, b]) is None


@pytest.mark.parametrize(
    "cfg_value", ["", "opus"], ids=["self_lineage-unset", "self_lineage-malformed"]
)
def test_c4_unset_or_malformed_self_lineage_fails_closed(cfg_value):
    c = _fresh_client()
    _set_self_lineage(cfg_value)
    a, b = _obs(c, 1), _obs(c, 2)
    assert _check([a, b]) is None


def test_c5_single_null_observation_plus_derivation_is_undetermined():
    c = _fresh_client()
    _set_self_lineage(SELF)
    a, b, d = _obs(c, 1, lineage=SELF), _obs(c, 2, lineage=OTHER), _obs(c, 3)
    dv = c.call("engram_derive", {
        "claim": "Derived from two diverse observations",
        "supporting_ids": f"{a},{b}",
        "logical_chain": "Both observations jointly support the claim.",
        "reasoning_type": "inductive_causal",
    })
    assert dv.get("status") == "created", dv
    dv_id = dv.get("derivation_id") or dv.get("derived_id") or dv.get("id")
    assert dv_id, dv
    assert _check([d, dv_id]) is None


def test_c6_explicit_same_lineage_unchanged_with_empty_null_self_list():
    c = _fresh_client()
    _set_self_lineage(SELF)
    a, b = _obs(c, 1, lineage=OTHER), _obs(c, 2, lineage=OTHER)
    adv = _check([a, b])
    assert adv is not None
    assert adv["shared_lineage"] == OTHER
    assert adv["null_self_premise_ids"] == []
    assert "null=self" not in adv["message"]
