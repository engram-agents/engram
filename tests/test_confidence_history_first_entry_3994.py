"""engram-alpha#3994 -- one key for a node's first confidence_history entry.

Contract:
  C1. Every node-creating path that writes a first confidence_history entry
      (observation, derivation, conjecture, axiom) writes a list of exactly one
      dict whose keys are exactly {"timestamp", "value", "reason"}, and whose
      "value" equals the node's stored `confidence` column.
  C2. Rows written before this change stay readable. A stored conjecture whose
      first history entry uses the key `confidence` (the old shape) still loads
      cleanly through (i) engram_inspect view="deep" and (ii) the
      inspect_raw.py CLI with --field confidence_history. This cell is green
      BEFORE and AFTER the fix: it guards readers, not writers. (i) proves only
      that the node loads: confidence_history is stripped from every MCP return
      (core._AGENT_STRIP_KEYS), so the deep view cannot show the entry. The
      entry itself is checked through (ii), by its printed "[0] {'confidence': "
      line (the CLI's header line also contains the word "confidence", so a
      bare substring check would prove nothing).
"""

import atexit
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "inspect_raw.py"

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_3994_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

EXPECTED_KEYS = {"timestamp", "value", "reason"}


def fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def _row(node_id):
    conn = sqlite3.connect(os.path.join(TEST_DIR, "knowledge.db"))
    try:
        return conn.execute(
            "SELECT confidence, confidence_history FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
    finally:
        conn.close()


def _assert_c1(node_id):
    conf, hist_raw = _row(node_id)
    hist = json.loads(hist_raw)
    assert isinstance(hist, list) and len(hist) == 1, hist
    assert isinstance(hist[0], dict), hist
    assert set(hist[0].keys()) == EXPECTED_KEYS, hist[0]
    assert hist[0]["value"] == conf, (hist[0], conf)


def _observation(client):
    r = client.call("engram_add_observation", {
        "url": "https://example.com/test",
        "title": "Test",
        "quoted_text": "Some evidence.",
        "interpretation": "Test.",
        "claim": "A specific claim for the history test",
        "quote_type": "hard_data",
    })
    assert r.get("status") == "created", r
    return r["observation_id"]


def test_c1_observation_first_entry():
    client = fresh_client()
    _assert_c1(_observation(client))


def test_c1_derivation_first_entry():
    client = fresh_client()
    ob = _observation(client)
    dv = client.call("engram_derive", {
        "claim": "A derived claim for the history test",
        "supporting_ids": ob,
        "logical_chain": "The observation states it. Therefore the derived claim.",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert dv.get("status") == "created", dv
    _assert_c1(dv["derivation_id"])


def test_c1_conjecture_first_entry():
    client = fresh_client()
    r = client.call("engram_add_conjecture", {
        "claim": "A conjecture for the history test",
        "basis": "Testing the first history entry.",
        "initial_confidence": 0.35,
    })
    assert r.get("status") == "created", r
    _assert_c1(r["conjecture_id"])


def test_c1_axiom_first_entry():
    client = fresh_client()
    r = client.call("engram_add_axiom", {
        "claim": "An axiom for the history test",
        "basis": "Testing the first history entry.",
    })
    assert r.get("status") == "created", r
    _assert_c1(r["axiom_id"])


def test_c2_old_confidence_key_rows_stay_readable():
    """Green before and after the fix: a pre-change row still loads."""
    client = fresh_client()
    r = client.call("engram_add_conjecture", {
        "claim": "A conjecture stored in the old shape",
        "basis": "Testing readability of old rows.",
    })
    cj = r["conjecture_id"]
    conn = sqlite3.connect(os.path.join(TEST_DIR, "knowledge.db"))
    try:
        conf, created_at = conn.execute(
            "SELECT confidence, created_at FROM nodes WHERE id=?", (cj,)
        ).fetchone()
        old = [{"confidence": conf, "reason": "conjecture — initial hypothesis",
                "timestamp": created_at}]
        conn.execute("UPDATE nodes SET confidence_history=? WHERE id=?",
                     (json.dumps(old), cj))
        conn.commit()
    finally:
        conn.close()

    # (i) deep inspect
    deep = client.call("engram_inspect", {"node_id": cj, "view": "deep"})
    assert "error" not in deep, deep
    assert deep["node"]["id"] == cj
    # confidence_history is stripped from every MCP return (core._AGENT_STRIP_KEYS),
    # so the deep view cannot show the entry; the readable-entry check is (ii).
    assert "confidence_history" not in deep["node"]
    assert deep["node"]["confidence"] == conf

    # (ii) inspect_raw CLI
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = TEST_DIR
    p = subprocess.run(
        [sys.executable, str(TOOL_PATH), cj, "--field", "confidence_history"],
        capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, (p.stdout, p.stderr)
    # The header "confidence_history for <id>: [1 entries]" always contains the
    # word "confidence"; check the printed entry line instead.
    assert "[0] {'confidence': " in p.stdout, p.stdout
    assert "1 entries" in p.stdout, p.stdout
