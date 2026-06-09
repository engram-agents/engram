"""MCP-contract tests for trust-tier payload validation.

Extracted from test_trust_tier.py (test 4b) because this test exercises
MCP-layer field validation (the unknown-field rejection path in the
payload_json dispatcher) rather than family-module behavior.

Coverage (1 test):
  4b. engram_set_trust_tier — unknown-field error does NOT leak
      'primary_user_approval_obtained' (sealed param anti-probe check).
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_trust_tier_pl_")
import atexit
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


def _mk_person(server, name: str) -> str:
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"name": name, "role": "test"})))
    assert "person_id" in r, r
    return r["person_id"]


# ── Test 4b ────────────────────────────────────────────────────────────────

def test_set_trust_tier_unknown_field_error_does_not_leak_hidden_param():
    """Unknown-field validator error must NOT reveal 'primary_user_approval_obtained'.

    The sealed param name is the teaching surface that appears only in the
    friction-warning error (Test 4). Leaking it via the unknown-field validator
    echo would let an agent probe the param name without ever hitting the
    friction-warning path — bypassing the structural-honesty stake.
    """
    server = fresh_server()
    pn_id = _mk_person(server, "SealLeakProbe")

    r = json.loads(server.engram_set_trust_tier(payload_json=json.dumps({
        "target_pn": pn_id,
        "tier": "our_side",
        "foo_typo": "bar",
    })))
    assert "error" in r, f"Expected error for unknown field, got: {r}"
    error_str = r["error"]
    assert "Unknown fields" in error_str, (
        f"Expected 'Unknown fields' in error, got:\n{error_str}"
    )
    assert "primary_user_approval_obtained" not in error_str, (
        f"Seal leak: unknown-field error must NOT contain "
        f"'primary_user_approval_obtained'. Got:\n{error_str}"
    )
