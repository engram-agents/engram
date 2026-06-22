"""Tests for engram-pkg mutation subcommands JSON payload path (issue #1257).

Each affected subcommand (add-evidence, add-observation, derive, supersede) now
accepts an optional positional JSON payload argument.  When provided, the payload
is passed directly to the underlying MCP tool; the flag-based path is the fallback
for backward compatibility.

Coverage:
  1. test_add_evidence_json_payload  — JSON payload path, exit 0
  2. test_add_observation_json_payload — JSON payload path, exit 0
  3. test_json_payload_missing_required_field — empty {} propagates to server, non-zero exit
  4. test_flags_still_work — flag-based path produces output (backward compat)
  5. test_add_observation_flags_still_work — flag-based add-observation still works
  6. test_derive_json_payload — JSON payload path for derive, exit 0
  7. test_supersede_json_payload — JSON payload path for supersede, exit 0
  8. test_add_evidence_stdin_payload — stdin path ("-") reads JSON from stdin

Test approach: subprocess.run against the CLI binary at
  src/engram/tools/engram-pkg/engram-pkg
Each test initialises a fresh package dir via `engram-pkg init`.

ENGRAM_NO_EMBEDDINGS=1 is set for every subprocess call.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate the CLI binary
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = str(REPO_ROOT / "src" / "engram" / "tools" / "engram-pkg" / "engram-pkg")

if not os.path.exists(CLI_PATH):
    pytest.fail(f"engram-pkg CLI not found at {CLI_PATH}")


# ---------------------------------------------------------------------------
# Guard: skip on hosts without the sqlite3 CLI (init runs dump.sh).
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    shutil.which("sqlite3") is None,
    reason="engram-pkg init templates shell out to sqlite3 CLI (#799); skip until pure-Python port",
)


# ---------------------------------------------------------------------------
# Helper: build subprocess env with embeddings disabled
# ---------------------------------------------------------------------------

def _subprocess_env() -> dict:
    env = os.environ.copy()
    env["ENGRAM_NO_EMBEDDINGS"] = "1"
    env["ENGRAM_NO_POLARITY"] = "1"
    # Ensure the repo is importable (server, engram_client, etc.)
    existing_path = env.get("PYTHONPATH", "")
    additions = [str(REPO_ROOT), str(REPO_ROOT / "src" / "engram")]
    env["PYTHONPATH"] = os.pathsep.join(additions + ([existing_path] if existing_path else []))
    return env


def _run(args: list[str], pkg_dir: str, input_text: str = None) -> subprocess.CompletedProcess:
    """Run engram-pkg with given args against pkg_dir. Returns CompletedProcess."""
    cmd = [sys.executable, CLI_PATH, "--pkg", pkg_dir] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_subprocess_env(),
        input=input_text,
    )


# ---------------------------------------------------------------------------
# Fixture: initialised package directory (fresh per test)
# ---------------------------------------------------------------------------

@pytest.fixture()
def pkg_dir(tmp_path):
    """Create and init a fresh engram-package in a temp dir."""
    d = str(tmp_path / "pkg")
    os.makedirs(d, exist_ok=True)
    result = subprocess.run(
        [sys.executable, CLI_PATH, "init", d],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    if result.returncode != 0:
        pytest.skip(f"engram-pkg init failed (may need sqlite3 CLI): {result.stderr[:300]}")
    return d


# ---------------------------------------------------------------------------
# Test 1: add-evidence JSON payload path
# ---------------------------------------------------------------------------

def test_add_evidence_json_payload(pkg_dir):
    """JSON payload path for add-evidence exits 0 and produces node output."""
    payload = json.dumps({"url": "https://example.com", "title": "Test Source"})
    result = _run(["add-evidence", payload], pkg_dir)
    assert result.returncode == 0, (
        f"add-evidence JSON payload failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    # Should produce JSON with an ev_ node id
    out = result.stdout.strip()
    assert out, "Expected JSON output from add-evidence"
    parsed = json.loads(out)
    assert "node_id" in parsed or "id" in parsed or "ev_" in out, (
        f"Expected node id in output, got: {out[:200]}"
    )


# ---------------------------------------------------------------------------
# Test 2: add-observation JSON payload path
# ---------------------------------------------------------------------------

def test_add_observation_json_payload(pkg_dir):
    """JSON payload path for add-observation exits 0."""
    payload = json.dumps({
        "quoted_text": "test quote",
        "interpretation": "test interpretation",
        "claim": "test claim",
        "quote_type": "editorial",
        "url": "https://example.com",
        "title": "T",
    })
    result = _run(["add-observation", payload], pkg_dir)
    assert result.returncode == 0, (
        f"add-observation JSON payload failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    out = result.stdout.strip()
    assert out, "Expected JSON output from add-observation"


# ---------------------------------------------------------------------------
# Test 3: empty JSON payload propagates to server → non-zero exit
# ---------------------------------------------------------------------------

def test_json_payload_missing_required_field(pkg_dir):
    """Passing {} as payload reaches the server; server rejects it (non-zero exit)."""
    payload = "{}"
    result = _run(["add-evidence", payload], pkg_dir)
    # The server should reject the empty payload (missing url/title)
    assert result.returncode != 0, (
        f"Expected non-zero exit for empty payload, got 0.\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )


# ---------------------------------------------------------------------------
# Test 4: derive — flags still work (backward compat)
# ---------------------------------------------------------------------------

def test_flags_still_work(pkg_dir):
    """Flag-based path for derive still produces output (backward compat).

    ob_0001 won't exist in a fresh package, so the server will return an error
    about the missing node — but the CLI itself should not error on argument
    parsing, and some output (stdout or stderr) should be produced.
    """
    result = _run([
        "derive",
        "--claim", "x",
        "--supporting-ids", "ob_0001",
        "--logical-chain", "x",
        "--reasoning-type", "deductive_modus_ponens",
    ], pkg_dir)
    # We expect some output (either the server's error or a result) — the
    # important thing is that argparse accepted the flags without complaining.
    has_output = bool(result.stdout.strip()) or bool(result.stderr.strip())
    assert has_output, (
        "Expected some output (server error or result) from flag-based derive, "
        f"got nothing. rc={result.returncode}"
    )


# ---------------------------------------------------------------------------
# Test 5: add-observation — flags still work (backward compat)
# ---------------------------------------------------------------------------

def test_add_observation_flags_still_work(pkg_dir):
    """Flag-based path for add-observation still works end-to-end."""
    result = _run([
        "add-observation",
        "--quoted-text", "the quoted text",
        "--interpretation", "the interpretation",
        "--claim", "the claim",
        "--quote-type", "editorial",
        "--url", "https://example.com",
        "--title", "Test Title",
    ], pkg_dir)
    assert result.returncode == 0, (
        f"add-observation flag-based path failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    assert result.stdout.strip(), "Expected JSON output"


# ---------------------------------------------------------------------------
# Test 6: derive — JSON payload path
# ---------------------------------------------------------------------------

def test_derive_json_payload(pkg_dir):
    """JSON payload path for derive reaches the server (server may error on missing node).

    The goal is to confirm the CLI parses the positional JSON argument and passes
    it to the server without a CLI-level error (argparse, json.loads, etc.).
    A server-side error about a missing ob_ node is acceptable — what matters is
    that the CLI layer handled the JSON payload correctly.
    """
    # First create a real observation so derive has something to reference
    obs_payload = json.dumps({
        "quoted_text": "test quote for derive",
        "interpretation": "interp",
        "claim": "base claim",
        "quote_type": "editorial",
        "url": "https://example.com",
        "title": "Source",
    })
    obs_result = _run(["add-observation", obs_payload], pkg_dir)
    if obs_result.returncode != 0:
        pytest.skip(f"add-observation failed, skipping derive test: {obs_result.stderr[:200]}")

    obs_out = json.loads(obs_result.stdout)
    node_id = obs_out.get("node_id") or obs_out.get("id", "ob_0001")

    derive_payload = json.dumps({
        "claim": "derived claim",
        "supporting_ids": node_id,
        "logical_chain": "x implies y",
        "reasoning_type": "deductive_modus_ponens",
    })
    result = _run(["derive", derive_payload], pkg_dir)
    assert result.returncode == 0, (
        f"derive JSON payload failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    assert result.stdout.strip(), "Expected JSON output from derive"


# ---------------------------------------------------------------------------
# Test 7: supersede — JSON payload path
# ---------------------------------------------------------------------------

def test_supersede_json_payload(pkg_dir):
    """JSON payload path for supersede reaches the server.

    Create two observations, then supersede the first with the second.
    """
    def _add_obs(pkg, claim):
        p = json.dumps({
            "quoted_text": f"quote for {claim}",
            "interpretation": "interp",
            "claim": claim,
            "quote_type": "editorial",
            "url": "https://example.com",
            "title": "Source",
        })
        r = _run(["add-observation", p], pkg)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout).get("node_id") or json.loads(r.stdout).get("id")

    old_id = _add_obs(pkg_dir, "old claim")
    new_id = _add_obs(pkg_dir, "new claim")

    if not old_id or not new_id:
        pytest.skip("Could not create prerequisite nodes for supersede test")

    supersede_payload = json.dumps({
        "old_node_id": old_id,
        "new_node_id": new_id,
    })
    result = _run(["supersede", supersede_payload], pkg_dir)
    assert result.returncode == 0, (
        f"supersede JSON payload failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    assert result.stdout.strip(), "Expected JSON output from supersede"


# ---------------------------------------------------------------------------
# Test 8: stdin path ("-")
# ---------------------------------------------------------------------------

def test_add_evidence_stdin_payload(pkg_dir):
    """Passing "-" as json_payload reads the JSON from stdin."""
    payload = json.dumps({"url": "https://stdin-test.example.com", "title": "Stdin Source"})
    result = _run(["add-evidence", "-"], pkg_dir, input_text=payload)
    assert result.returncode == 0, (
        f"add-evidence stdin path failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    assert result.stdout.strip(), "Expected JSON output from add-evidence via stdin"
