"""Tests for engram_derive single-payload signature (wave 3 of ob_1499 — issue #99).

Coverage:
  1. Happy path — well-formed payload creates a derivation.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Internal helper — _derive_impl works with named kwargs.
  4. use_stale bool round-trips through JSON.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_derive_")
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


def _seed_premises(server):
    """Create two axioms as claim-bearing premises; return their IDs.

    Axioms avoid the evidence-URL DNS-validation dependency that observations
    carry. The migration test only needs claim-bearing nodes for engram_derive
    to cite — axiom semantics (confidence 1.0, no URL) are fine for this.
    """
    r1 = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": "Premise A holds in the test scenario.",
        "basis": "test fixture premise A.",
    })))
    r2 = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": "Premise B holds in the test scenario.",
        "basis": "test fixture premise B.",
    })))
    return r1["axiom_id"], r2["axiom_id"]


def test_payload_json_creates_derivation():
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "A and B together imply C.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "From premises A and B, conclusion C follows by conjunction.",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "derivation_id" in r and r["derivation_id"].startswith("dv_"), r
    assert r["reasoning_type"] == "deductive_modus_ponens"
    assert set(r["supporting_nodes"]) == {obs_a, obs_b}


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_derive(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_derive(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "x",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "x",
        "reasoning_type": "deductive_modus_ponens",
        "bogus_field": "reject me",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_missing_supporting_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "supporting_ids": "",
        "logical_chain": "x",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" in r and "supporting" in r["error"].lower(), r


def test_payload_json_invalid_reasoning_type_rejected():
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "x",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "x",
        "reasoning_type": "made_up_type",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid reasoning_type" in r["error"], r
    assert "valid_types" in r


def test_payload_json_use_stale_bool_round_trips():
    """use_stale arrives as a Python bool from json.loads — verify the path."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "Bool round-trip test.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "Premises stand; use_stale=False is the default and harmless here.",
        "reasoning_type": "deductive_modus_ponens",
        "use_stale": False,
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "derivation_id" in r


def test_payload_json_use_stale_string_rejected_per_ls_0034():
    """JSON-string 'false' is truthy in Python, so the isinstance guard must
    reject the string form with a clear error directing the caller to a JSON
    boolean.

    THE ORIGINAL RATIONALE IS DEAD; THE TEST IS NOT (#2324). This docstring
    used to say a use_stale: 'false' payload "would silently bypass the
    MECH-5 stale-premise block". That is no longer true in either direction:
    stale premises now block UNCONDITIONALLY — _validate_premises' stale
    return is not gated on use_stale's value at all — so no value of this
    parameter, well-formed or malformed, can bypass anything. There is no
    blocking-guard weakening left for a truthy string to achieve here.

    Kept for schema hygiene rather than safety: a caller passing a JSON
    string where a boolean is declared should get a consistent, immediate
    error on this parameter for the same reason as every other, and the
    parameter itself survives only until #2013 slice 2 removes it.

    Fairy round-1 blocker on PR #110 caught the original gap.

    This docstring used to add "Pattern parallels _resolve_impl's identical
    guard." Dropped: _resolve_impl has no use_stale parameter and no such
    guard — issue #229 removed the field on 2026-05-20, three months before
    #2324, when engram_resolve became pure-wire. The sibling that documents
    that removal (test_wave_2_bool_coercion_sweep.py) has said so since;
    this file kept pointing at the guard anyway. Predates #2324 and is not
    drift this PR caused, but it is a false sentence in a docstring this PR
    is editing, which is a poor place to leave one.
    """
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    for bad_value in ["false", "true", "False", "True", 0, 1]:
        payload = {
            "claim": "Bypass attempt.",
            "supporting_ids": f"{obs_a},{obs_b}",
            "logical_chain": "Testing the string-truthy bypass.",
            "reasoning_type": "deductive_modus_ponens",
            "use_stale": bad_value,
        }
        r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
        assert "error" in r, f"use_stale={bad_value!r} should be rejected: {r}"
        assert "use_stale must be a JSON boolean" in r["error"], r


def test_payload_json_use_contested_string_rejected():
    """Same bool-string-truthy trap as use_stale, for the #1654 use_contested
    opt-in: a JSON-string "false" is truthy in Python, so without the
    isinstance guard a use_contested: "false" payload would silently bypass
    the MECH-5 contradicted-premise block."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    for bad_value in ["false", "true", "False", "True", 0, 1]:
        payload = {
            "claim": "Bypass attempt.",
            "supporting_ids": f"{obs_a},{obs_b}",
            "logical_chain": "Testing the string-truthy bypass.",
            "reasoning_type": "deductive_modus_ponens",
            "use_contested": bad_value,
        }
        r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
        assert "error" in r, f"use_contested={bad_value!r} should be rejected: {r}"
        assert "use_contested must be a JSON boolean" in r["error"], r


def test_payload_json_use_contested_field_allowed():
    """use_contested must be in the _DERIVE_FIELDS allowlist, not rejected as
    an unknown field."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "A and B together imply C.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "From premises A and B, conclusion C follows by conjunction.",
        "reasoning_type": "deductive_modus_ponens",
        "use_contested": False,
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "derivation_id" in r, r


def test_payload_json_derivation_mode_corroboration_legacy_fallback():
    """When reasoning_type is empty AND derivation_mode='corroboration',
    the impl falls back to inductive_generalization. Covers the legacy
    path explicitly from the payload surface (otherwise covered indirectly
    via client.call() in test_reasoning_types.py).
    """
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "Legacy corroboration fallback test.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "Premises A and B corroborate the claim.",
        "derivation_mode": "corroboration",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("reasoning_type") == "inductive_generalization", r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _derive_impl directly."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    r = json.loads(server._derive_impl(
        claim="Internal kwargs path test.",
        supporting_ids=f"{obs_a},{obs_b}",
        logical_chain="Testing _impl is callable.",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in r, r
    assert "derivation_id" in r


def test_warrant_stored_in_derive_meta():
    """Happy path: warrant string round-trips into the node's stored metadata."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    warrant_text = "When two premises are jointly established, their conjunction entails the derived claim."
    payload = {
        "claim": "A and B together entail C via the warrant.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "Premises A and B, mediated by the bridging principle, yield C.",
        "reasoning_type": "deductive_modus_ponens",
        "warrant": warrant_text,
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    dv_id = r["derivation_id"]

    # Use view="deep" to access the raw metadata field (parsed_metadata is
    # stripped by _strip_agent_facing in the recall view).
    inspect_r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": dv_id, "view": "deep"})))
    assert "error" not in inspect_r, inspect_r
    raw_meta = inspect_r["node"]["metadata"]
    meta = json.loads(raw_meta)
    assert meta.get("warrant") == warrant_text, meta


def test_warrant_omitted_leaves_no_key():
    """Omission equivalence: omitting warrant leaves no warrant key in metadata (backward compat)."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "Backward-compat: derive without warrant.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "Premises A and B entail C — no bridging principle needed.",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    dv_id = r["derivation_id"]

    inspect_r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": dv_id, "view": "deep"})))
    assert "error" not in inspect_r, inspect_r
    raw_meta = inspect_r["node"]["metadata"]
    meta = json.loads(raw_meta)
    assert "warrant" not in meta, f"warrant key must be absent when not supplied: {meta}"


def test_warrant_non_string_rejected():
    """Non-string warrant is rejected with a clear error (type guard analogous to use_stale)."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "Type-guard test for warrant.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "Premises stand.",
        "reasoning_type": "deductive_modus_ponens",
        "warrant": 42,
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" in r, f"warrant=42 should be rejected: {r}"
    assert "warrant must be a string" in r["error"], r


def test_payload_author_supplied_built_on_contested_rejected_as_unknown_field():
    """Security property (#1654): metadata.built_on_contested must be
    auto-stamped by the use_contested=True override path ONLY. An author
    cannot spoof or omit it by passing built_on_contested directly in the
    payload — engram_derive's _DERIVE_FIELDS allowlist has no such field,
    so it's rejected at the payload layer before it ever reaches the
    guard, same structural guarantee as any other unknown field."""
    server = fresh_server()
    obs_a, obs_b = _seed_premises(server)
    payload = {
        "claim": "Attempting to author-supply the audit stamp directly.",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "Spoofing attempt.",
        "reasoning_type": "deductive_modus_ponens",
        "built_on_contested": [obs_a],
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "Unknown fields" in r["error"], r
    assert "built_on_contested" in r["error"], r
