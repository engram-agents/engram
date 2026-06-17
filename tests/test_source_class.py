"""
Tests for the source_class system.

Tests cover:
1. source_class stored in node metadata
2. External source_class — no confidence adjustment
3. Introspective source_class — ×0.95 confidence discount
4. User_stated source_class — overrides to official_statement confidence
5. Invalid source_class rejected
6. Default source_class is 'external'
7. Batch observations with per-observation source_class
8. source_class appears in observation result

Import-pattern contract (#948 wave C — R1):
    This file imports engram_core directly and contains no ``import server``.
    DB_PATH is defined in engram_core; _get_confidence_history_from_db uses
    a function-local ``import engram_core`` to get the freshly-reloaded module
    after fresh_client() purges and re-imports the engram_* family.
    The top-level ``import engram_core`` is the AST-visible selection edge.
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engram_core  # noqa: E402 — top-level import for AST-visible selection edge

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_source_class_")
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


def test_external_no_discount():
    """External source_class should not modify confidence."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Test Article",
        "quoted_text": "The rate limit is 1000/hr.",
        "interpretation": "Hard data from docs.",
        "claim": "API rate limit is 1000 requests per hour",
        "quote_type": "hard_data",
        "source_class": "external",
    })
    assert r["status"] == "created"
    assert r["confidence"] == 0.95  # hard_data, no discount
    assert r["source_class"] == "external"


def test_introspective_discount():
    """Introspective source_class should apply ×0.95 discount."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/agent-log",
        "title": "Agent daily log",
        "quoted_text": "We decided to use PostgreSQL for JSON support.",
        "interpretation": "Architecture decision from prior session.",
        "claim": "PostgreSQL was chosen for JSON support",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert r["status"] == "created"
    # hard_data (0.95) × introspective discount (0.95) = 0.9025 → rounded to 0.902
    assert r["confidence"] == 0.902, f"Expected 0.902, got {r['confidence']}"
    assert r["source_class"] == "introspective"


def test_introspective_editorial():
    """Introspective editorial should get double-discounted."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/agent-log",
        "title": "Agent daily log",
        "quoted_text": "I think the auth module might be overly complex.",
        "interpretation": "Agent's subjective assessment.",
        "claim": "The auth module may be overly complex",
        "quote_type": "editorial",
        "source_class": "introspective",
    })
    assert r["status"] == "created"
    # editorial (0.35) × introspective (0.95) = 0.3325 → 0.332
    assert r["confidence"] == 0.332, f"Expected 0.332, got {r['confidence']}"


def test_user_stated_overrides_quote_type():
    """User_stated should override any quote_type to official_statement confidence."""
    client = fresh_client()
    # Even with editorial quote_type, user_stated should give official_statement confidence
    r = client.call("engram_add_observation", {
        "url": "https://example.com/user-conversation",
        "title": "User conversation",
        "quoted_text": "Our deadline is March 15.",
        "interpretation": "User stated a project deadline.",
        "claim": "Project deadline is March 15",
        "quote_type": "editorial",  # would normally be 0.35
        "source_class": "user_stated",
    })
    assert r["status"] == "created"
    assert r["confidence"] == 0.85, f"Expected 0.85 (official_statement), got {r['confidence']}"
    assert r["source_class"] == "user_stated"


def test_user_stated_hard_data():
    """User_stated with hard_data should still give official_statement confidence."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/user-conversation",
        "title": "User conversation",
        "quoted_text": "We have 12 employees.",
        "interpretation": "User provided company fact.",
        "claim": "Company has 12 employees",
        "quote_type": "hard_data",  # would normally be 0.95
        "source_class": "user_stated",
    })
    assert r["status"] == "created"
    # user_stated overrides to official_statement = 0.85 (not hard_data 0.95)
    assert r["confidence"] == 0.85, f"Expected 0.85, got {r['confidence']}"


def test_default_is_external():
    """Omitting source_class should default to 'external'."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Test Article",
        "quoted_text": "GDP grew 2.1%.",
        "interpretation": "Economic data.",
        "claim": "GDP grew 2.1%",
        "quote_type": "hard_data",
    })
    assert r["status"] == "created"
    assert r["confidence"] == 0.95  # No discount
    assert r["source_class"] == "external"


def test_invalid_source_class_rejected():
    """Invalid source_class should return an error."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Test Article",
        "quoted_text": "Something.",
        "interpretation": "Test.",
        "claim": "Test claim",
        "quote_type": "hard_data",
        "source_class": "agent_private",
    })
    assert "error" in r
    assert "source_class" in r["error"]


def test_source_class_in_metadata():
    """source_class should be stored in node metadata."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Test Article",
        "quoted_text": "Test data point.",
        "interpretation": "Testing metadata storage.",
        "claim": "Test claim for metadata",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert r["status"] == "created"
    # Verify via inspect — full node content
    insp = client.call("engram_inspect", {"node_id": r["observation_id"], "view": "deep"})
    node = insp["node"]
    meta = node.get("metadata", {})
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta.get("source_class") == "introspective"


def test_batch_with_source_class():
    """Batch observations should support per-observation source_class."""
    client = fresh_client()
    obs = [
        {
            "quoted_text": "Revenue grew 15%.",
            "interpretation": "Strong growth.",
            "claim": "Revenue grew 15%",
            "quote_type": "hard_data",
            "source_class": "external",
        },
        {
            "quoted_text": "I noted this seemed unusual.",
            "interpretation": "Agent's prior note.",
            "claim": "Revenue growth was unusual",
            "quote_type": "editorial",
            "source_class": "introspective",
        },
        {
            "quoted_text": "Boss said target is 20%.",
            "interpretation": "User-stated goal.",
            "claim": "Revenue target is 20%",
            "quote_type": "editorial",
            "source_class": "user_stated",
        },
    ]
    r = client.call("engram_add_observation_batch", {
        "url": "https://example.com/report",
        "title": "Quarterly Report",
        "observations_json": json.dumps(obs),
    })
    assert r["status"] == "batch_complete"
    assert r["created"] == 3

    results = r["results"]
    # External hard_data: 0.95
    assert results[0]["confidence"] == 0.95
    assert results[0]["source_class"] == "external"
    # Introspective editorial: 0.35 × 0.95 = 0.3325 → 0.332
    assert results[1]["confidence"] == 0.332
    assert results[1]["source_class"] == "introspective"
    # User_stated: always 0.85 regardless of quote_type
    assert results[2]["confidence"] == 0.85
    assert results[2]["source_class"] == "user_stated"


def _get_confidence_history_from_db(client, node_id):
    """Read confidence_history from DB (stripped from MCP returns per #358)."""
    # Use function-local import to get the freshly-reloaded engram_core after
    # fresh_client() has purged and re-imported the engram_* family.
    import engram_core as _core
    conn = sqlite3.connect(str(_core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT confidence_history FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return []
        raw = row["confidence_history"] or "[]"
        return json.loads(raw)
    finally:
        conn.close()


def test_confidence_history_records_source_class():
    """Confidence history should note source_class when not external."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/agent-log",
        "title": "Agent log",
        "quoted_text": "We chose React.",
        "interpretation": "Framework decision.",
        "claim": "React was chosen as frontend framework",
        "quote_type": "attributed_analysis",
        "source_class": "introspective",
    })
    assert r["status"] == "created"
    # Read confidence_history from DB (stripped from MCP returns per #358).
    history = _get_confidence_history_from_db(client, r["observation_id"])
    assert len(history) > 0
    assert "introspective" in history[0]["reason"]


def test_synthetic_url_rejected():
    """Synthetic URL schemes like conversation:// should be rejected."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "conversation://2026-04-08/test",
        "title": "Fake conversation",
        "quoted_text": "Some quote.",
        "interpretation": "Test.",
        "claim": "Test claim",
        "quote_type": "hard_data",
    })
    assert "error" in r
    assert "Unrecognized URL scheme" in r["error"]


def test_nonexistent_file_rejected():
    """file:// URLs pointing to nonexistent files should be rejected."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "file:///nonexistent/path/fakefile.txt",
        "title": "Nonexistent file",
        "quoted_text": "Quote.",
        "interpretation": "Test.",
        "claim": "Test claim",
        "quote_type": "hard_data",
    })
    assert "error" in r
    assert "File not found" in r["error"]


def test_valid_file_url_accepted():
    """file:// URLs pointing to real, committed files should work."""
    import os, subprocess
    client = fresh_client()
    # Use the archived SKILL.md — a committed file in our repo that we haven't modified
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skill_file = os.path.join(repo_root, "docs", "archive", "SKILL.md")
    # Verify the file is clean (no uncommitted changes) — skip test if not
    status = subprocess.run(["git", "status", "--porcelain", "--", "docs/archive/SKILL.md"],
                            cwd=repo_root, capture_output=True, text=True)
    if status.stdout.strip():
        import pytest
        pytest.skip("docs/archive/SKILL.md has uncommitted changes")
    r = client.call("engram_add_observation", {
        "url": f"file://{skill_file}",
        "title": "SKILL.md agent protocol (archived)",
        "quoted_text": "# SKILL.md — ENGRAM Agent Protocol",
        "interpretation": "The archived SKILL.md file exists and is committed.",
        "claim": "docs/archive/SKILL.md exists and is committed in the repo",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert r["status"] == "created", f"Expected created, got: {r}"


def test_https_url_accepted():
    """https:// URLs should always be accepted (no file existence check)."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Web article",
        "quoted_text": "Some data.",
        "interpretation": "Test.",
        "claim": "Web source test claim",
        "quote_type": "hard_data",
    })
    assert r["status"] == "created"


def test_file_evidence_per_observation_versioning():
    """Same file URL → same evidence node, regardless of content. Per-observation
    versioning (dv_0547): each observation carries its own git_sha + content_hash.

    Note: the test uses a non-git path so the committed-before-cite guard is
    not triggered — outside-git files are allowed without versioning, but
    explicit content_hash/git_sha args still get stored on the observation.
    """
    fd, version_file = tempfile.mkstemp(prefix="engram_test_versioned_source_", suffix=".txt")
    os.close(fd)
    with open(version_file, "w") as f:
        f.write("Line alpha from version one.\n")
        f.write("Line beta from version one.\n")
        f.write("Shared line present in both versions.\n")

    client = fresh_client()
    file_url = f"file://{version_file}"

    r1 = client.call("engram_add_observation", {
        "url": file_url,
        "title": "versioned_source.txt",
        "quoted_text": "Line alpha from version one.",
        "interpretation": "Reading first version.",
        "claim": "Source contains alpha line",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert r1["status"] == "created"
    ev1 = r1["evidence_id"]

    r2 = client.call("engram_add_observation", {
        "url": file_url,
        "title": "versioned_source.txt",
        "quoted_text": "Line beta from version one.",
        "interpretation": "Another observation from same version.",
        "claim": "Source contains beta line",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert r2["status"] == "created"
    assert r2["evidence_id"] == ev1, f"Same URL must reuse evidence, got {r2['evidence_id']}"

    r3 = client.call("engram_add_observation", {
        "url": file_url,
        "title": "versioned_source.txt",
        "quoted_text": "Shared line present in both versions.",
        "interpretation": "Reading after content change.",
        "claim": "Source was updated to new version",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert r3["status"] == "created"
    assert r3["evidence_id"] == ev1, (
        f"Same URL must reuse evidence even after content change "
        f"(per-observation versioning), got {r3['evidence_id']}"
    )

    # Check evidence node metadata via inspect
    insp = client.call("engram_inspect", {"node_id": ev1, "view": "deep"})
    ev_node = insp["node"]
    ev_meta = ev_node.get("metadata", {})
    if isinstance(ev_meta, str):
        ev_meta = json.loads(ev_meta)
    assert "git_sha" not in ev_meta, "evidence must NOT carry git_sha"
    assert "content_hash" not in ev_meta, "evidence must NOT carry content_hash"
    assert "prior_evidence_id" not in ev_meta, "evidence-version chains are gone"

    # Check no supersedes edges via subgraph topology
    sub = client.call("engram_get_subgraph", {"node_id": ev1, "view": "edges"})
    topo_entries = []
    for entries in sub["topology"].values():
        topo_entries.extend(entries)
    ev_supersedes = [
        e for e in topo_entries
        if e["relation"] == "supersedes"
    ]
    assert ev_supersedes == [], "no supersedes edges between evidence rows in v2 model"

    os.unlink(version_file)


def _init_git_repo(git_dir):
    """Helper: create a fresh git repo with initial config."""
    import subprocess
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir)
    os.makedirs(git_dir)
    subprocess.run(["git", "init"], cwd=git_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=git_dir, capture_output=True)


def test_git_enforcement_uncommitted_file_rejected():
    """File evidence in a git repo must be committed before it can be cited."""
    import subprocess

    git_dir = tempfile.mkdtemp(prefix="engram_test_git_enforce_")
    _init_git_repo(git_dir)

    # Need at least one commit for HEAD to exist
    dummy = os.path.join(git_dir, "dummy.txt")
    with open(dummy, "w") as f:
        f.write("placeholder\n")
    subprocess.run(["git", "add", "dummy.txt"], cwd=git_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=git_dir, capture_output=True)

    # Create an untracked file
    test_file = os.path.join(git_dir, "evidence.md")
    with open(test_file, "w") as f:
        f.write("This is test evidence content.\n")

    client = fresh_client()
    file_url = f"file://{test_file}"

    # Should be rejected — file is untracked
    r = client.call("engram_add_observation", {
        "url": file_url,
        "title": "Uncommitted evidence",
        "quoted_text": "This is test evidence content.",
        "interpretation": "Testing git enforcement.",
        "claim": "Uncommitted file should be rejected",
        "quote_type": "hard_data",
    })
    assert "error" in r, f"Expected rejection for untracked file, got: {r}"
    assert "untracked" in r["error"].lower() or "uncommitted" in r["error"].lower()

    # Now commit it
    subprocess.run(["git", "add", "evidence.md"], cwd=git_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add evidence"], cwd=git_dir, capture_output=True)

    # Should succeed now
    r2 = client.call("engram_add_observation", {
        "url": file_url,
        "title": "Committed evidence",
        "quoted_text": "This is test evidence content.",
        "interpretation": "Testing git enforcement.",
        "claim": "Committed file should be accepted",
        "quote_type": "hard_data",
    })
    assert r2.get("status") == "created", f"Expected success for committed file, got: {r2}"

    # Per dv_0547: version metadata lives on the observation, not the evidence.
    obs_insp = client.call("engram_inspect", {"node_id": r2["observation_id"], "view": "deep"})
    obs_node = obs_insp["node"]
    obs_meta = obs_node.get("metadata", "{}")
    if isinstance(obs_meta, str):
        obs_meta = json.loads(obs_meta)
    assert obs_meta.get("content_hash"), "obs content_hash should be auto-populated"
    assert obs_meta.get("git_sha"), "obs git_sha should be auto-populated"

    ev_insp = client.call("engram_inspect", {"node_id": r2["evidence_id"], "view": "deep"})
    ev_node = ev_insp["node"]
    ev_meta = ev_node.get("metadata", "{}")
    if isinstance(ev_meta, str):
        ev_meta = json.loads(ev_meta)
    assert "content_hash" not in ev_meta, "evidence must NOT carry content_hash"
    assert "git_sha" not in ev_meta, "evidence must NOT carry git_sha"

    shutil.rmtree(git_dir)


def test_git_enforcement_modified_file_rejected():
    """Modified but uncommitted file in git repo should be rejected."""
    import subprocess

    git_dir = tempfile.mkdtemp(prefix="engram_test_git_modify_")
    _init_git_repo(git_dir)

    # Create and commit a file
    test_file = os.path.join(git_dir, "source.md")
    with open(test_file, "w") as f:
        f.write("Original content.\n")
    subprocess.run(["git", "add", "source.md"], cwd=git_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=git_dir, capture_output=True)

    # Modify without committing
    with open(test_file, "w") as f:
        f.write("Modified content.\n")

    client = fresh_client()
    file_url = f"file://{test_file}"

    # Should be rejected — file is modified
    r = client.call("engram_add_observation", {
        "url": file_url,
        "title": "Modified source",
        "quoted_text": "Modified content.",
        "interpretation": "Testing modified rejection.",
        "claim": "Modified file should be rejected",
        "quote_type": "hard_data",
    })
    assert "error" in r, f"Expected rejection for modified file, got: {r}"
    assert "modified" in r["error"].lower() or "uncommitted" in r["error"].lower()

    shutil.rmtree(git_dir)


def test_git_enforcement_bad_sha_rejected():
    """Providing a git_sha where the file doesn't exist should be rejected."""
    import subprocess

    git_dir = tempfile.mkdtemp(prefix="engram_test_git_badsha_")
    _init_git_repo(git_dir)

    # Make an initial commit without our file
    dummy = os.path.join(git_dir, "dummy.txt")
    with open(dummy, "w") as f:
        f.write("placeholder\n")
    subprocess.run(["git", "add", "dummy.txt"], cwd=git_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=git_dir, capture_output=True)
    old_sha_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_dir, capture_output=True, text=True)
    old_sha = old_sha_result.stdout.strip()

    # Create and commit the evidence file in a second commit
    test_file = os.path.join(git_dir, "evidence.md")
    with open(test_file, "w") as f:
        f.write("Evidence content.\n")
    subprocess.run(["git", "add", "evidence.md"], cwd=git_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add evidence"], cwd=git_dir, capture_output=True)

    client = fresh_client()
    file_url = f"file://{test_file}"

    # Should be rejected — file doesn't exist at old_sha
    r = client.call("engram_add_observation", {
        "url": file_url,
        "title": "Wrong SHA evidence",
        "quoted_text": "Evidence content.",
        "interpretation": "Testing bad SHA rejection.",
        "claim": "Bad SHA should be rejected",
        "quote_type": "hard_data",
        "git_sha": old_sha,
    })
    assert "error" in r, f"Expected rejection for wrong SHA, got: {r}"
    assert "does not exist at git revision" in r["error"]

    shutil.rmtree(git_dir)


if __name__ == "__main__":
    tests = [
        test_external_no_discount,
        test_introspective_discount,
        test_introspective_editorial,
        test_user_stated_overrides_quote_type,
        test_user_stated_hard_data,
        test_default_is_external,
        test_invalid_source_class_rejected,
        test_source_class_in_metadata,
        test_batch_with_source_class,
        test_confidence_history_records_source_class,
        test_synthetic_url_rejected,
        test_nonexistent_file_rejected,
        test_valid_file_url_accepted,
        test_https_url_accepted,
        test_file_evidence_per_observation_versioning,
        test_git_enforcement_uncommitted_file_rejected,
        test_git_enforcement_modified_file_rejected,
        test_git_enforcement_bad_sha_rejected,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)
