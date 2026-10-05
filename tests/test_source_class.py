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


def test_pdf_url_provenance_warning_names_the_format_gap():
    """#2765: a url:-sourced PDF observation is always quote_verified=False
    (no fetch at creation time, same as any other web source) -- but for a
    PDF the check could never have run even with a fetch, since quotes
    verify against extracted text, not raw bytes. The provenance warning
    must name the PDF case, not read like the ordinary, softer
    could-not-fetch caveat every other web source gets."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/paper.pdf",
        "title": "A PDF paper",
        "quoted_text": "Some data.",
        "interpretation": "Test.",
        "claim": "PDF source test claim",
        "quote_type": "hard_data",
    })
    assert r["status"] == "created"
    assert r["quote_verified"] is False
    warning = r["quote_provenance_warning"]
    assert "This source is a PDF" in warning
    assert "extracted text" in warning
    assert "#2765" in warning
    # The generic web-source caveat's own wording must NOT appear here --
    # this is a replacement for the PDF case, not an addition.
    assert "no page fetch" not in warning


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


def test_quote_verified_https_false():
    """https:// URL observations should have quote_verified: False and a warning."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/paper",
        "title": "Research Paper",
        "quoted_text": "Memory is fundamental to cognition.",
        "interpretation": "Core claim from paper.",
        "claim": "Memory is fundamental to cognition",
        "quote_type": "attributed_analysis",
    })
    assert r["status"] == "created", f"Unexpected: {r}"
    assert r["quote_verified"] is False
    assert "quote_provenance_warning" in r
    assert "unverified" in r["quote_provenance_warning"]


def test_quote_verified_file_true():
    """file:// URL observations should have quote_verified: True and no warning."""
    fd, tmpfile = tempfile.mkstemp(prefix="engram_test_qv_", suffix=".txt")
    os.close(fd)
    with open(tmpfile, "w") as f:
        f.write("This is the verified quote text.\n")
    try:
        client = fresh_client()
        r = client.call("engram_add_observation", {
            "url": f"file://{tmpfile}",
            "title": "Temp file",
            "quoted_text": "This is the verified quote text.",
            "interpretation": "Quote from local file.",
            "claim": "Temp file contains verified quote",
            "quote_type": "hard_data",
        })
        assert r["status"] == "created", f"Unexpected: {r}"
        assert r["quote_verified"] is True
        assert "quote_provenance_warning" not in r
    finally:
        os.unlink(tmpfile)


def test_quote_verified_stored_in_metadata():
    """quote_verified should be stored in node metadata."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Web Article",
        "quoted_text": "Some web claim.",
        "interpretation": "From the web.",
        "claim": "Quote verified stored in metadata",
        "quote_type": "editorial",
    })
    assert r["status"] == "created"
    insp = client.call("engram_inspect", {"node_id": r["observation_id"], "view": "deep"})
    node = insp["node"]
    meta = node.get("metadata", {})
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert "quote_verified" in meta
    assert meta["quote_verified"] is False  # https:// → False


def test_quote_verified_batch_propagates():
    """Batch observations should carry quote_verified in each result dict."""
    client = fresh_client()
    obs = [
        {
            "quoted_text": "Batch claim one.",
            "interpretation": "First.",
            "claim": "Batch claim one verified",
            "quote_type": "hard_data",
        },
        {
            "quoted_text": "Batch claim two.",
            "interpretation": "Second.",
            "claim": "Batch claim two verified",
            "quote_type": "editorial",
        },
    ]
    r = client.call("engram_add_observation_batch", {
        "url": "https://example.com/batch-source",
        "title": "Batch Source",
        "observations_json": json.dumps(obs),
    })
    assert r["status"] == "batch_complete"
    assert r["created"] == 2
    for result in r["results"]:
        assert result["quote_verified"] is False
        assert "quote_provenance_warning" in result


def test_quote_verified_visible_in_recall_view():
    """quote_verified should appear in engram_inspect recall view (not buried in metadata)."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/recall-test",
        "title": "Recall test article",
        "quoted_text": "Claim for recall view test.",
        "interpretation": "Testing recall view visibility.",
        "claim": "Quote verified visible in recall view",
        "quote_type": "editorial",
    })
    assert r["status"] == "created"
    insp = client.call("engram_inspect", {"node_id": r["observation_id"], "view": "recall"})
    node = insp["node"]
    assert "quote_verified" in node, f"quote_verified missing from recall view; got keys: {list(node.keys())}"
    assert node["quote_verified"] is False  # https:// → False
    assert "metadata" not in node, "raw metadata should remain stripped from recall view"


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


def _git_run(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _symlinked_repo():
    """A git repo (with one commit) reachable through a symlinked directory path.

    Returns (base, real_repo, linked_repo): linked_repo is an os.symlink to
    real_repo, built explicitly so the cells do not depend on TMPDIR (macOS
    tempdirs are /var/folders/..., a symlink to /private/var/...; #3292).
    """
    base = os.path.realpath(tempfile.mkdtemp(prefix="engram_test_symlink_"))
    real_repo = os.path.join(base, "real")
    linked_repo = os.path.join(base, "link")
    _init_git_repo(real_repo)
    os.symlink(real_repo, linked_repo)
    with open(os.path.join(real_repo, "dummy.txt"), "w") as f:
        f.write("placeholder\n")
    _git_run(real_repo, "add", "dummy.txt")
    _git_run(real_repo, "commit", "-m", "initial")
    return base, real_repo, linked_repo


def _cite_file(client, path, text):
    return client.call("engram_add_observation", {
        "url": f"file://{path}",
        "title": "Symlink evidence",
        "quoted_text": text,
        "interpretation": "Testing git enforcement through symlinks.",
        "claim": f"Symlink citation of {os.path.basename(path)}",
        "quote_type": "hard_data",
    })


def test_git_enforcement_committed_file_via_symlinked_dir_accepted():
    """#3292: a committed file cited through a symlinked directory is accepted."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("Symlinked dir evidence.\n")
        _git_run(real_repo, "add", "evidence.md")
        _git_run(real_repo, "commit", "-m", "add evidence")
        client = fresh_client()
        r = _cite_file(client, os.path.join(linked_repo, "evidence.md"),
                       "Symlinked dir evidence.")
        assert r.get("status") == "created", f"committed file via symlinked path rejected: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_untracked_file_via_symlinked_dir_rejected():
    """An untracked file cited through a symlinked directory is still rejected."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("Symlinked dir evidence.\n")
        client = fresh_client()
        r = _cite_file(client, os.path.join(linked_repo, "evidence.md"),
                       "Symlinked dir evidence.")
        assert "error" in r, f"untracked file via symlinked path accepted: {r}"
        err = r["error"].lower()
        assert "untracked" in err or "uncommitted" in err, r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_committed_symlink_to_uncommitted_outside_file_rejected():
    """A committed in-repo symlink to an uncommitted file OUTSIDE the repo is rejected.

    cat-file would find the committed symlink blob while the bytes hashed come
    from the (uncommitted) target; the check must look at the target.
    """
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        outside = os.path.join(base, "outside.md")
        with open(outside, "w") as f:
            f.write("Outside uncommitted evidence.\n")
        os.symlink(outside, os.path.join(real_repo, "evlink.md"))
        _git_run(real_repo, "add", "evlink.md")
        _git_run(real_repo, "commit", "-m", "add symlink")
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "evlink.md"),
                       "Outside uncommitted evidence.")
        assert "error" in r, f"symlink to uncommitted outside file accepted: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_committed_symlink_to_committed_inrepo_file_accepted():
    """A committed in-repo symlink to a committed in-repo file is accepted."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("In-repo target evidence.\n")
        os.symlink("evidence.md", os.path.join(real_repo, "evlink.md"))
        _git_run(real_repo, "add", "evidence.md", "evlink.md")
        _git_run(real_repo, "commit", "-m", "add evidence + symlink")
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "evlink.md"),
                       "In-repo target evidence.")
        assert r.get("status") == "created", f"committed symlink to committed file rejected: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_committed_symlink_to_outside_file_rejected_by_name():
    """A committed in-repo symlink to a file OUTSIDE the repo is rejected, and the
    error says the cited file resolves outside the repository (not a misleading
    "does not exist at git revision")."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        outside = os.path.join(base, "outside.md")
        with open(outside, "w") as f:
            f.write("Outside evidence.\n")
        link = os.path.join(real_repo, "evlink.md")
        os.symlink(outside, link)
        _git_run(real_repo, "add", "evlink.md")
        _git_run(real_repo, "commit", "-m", "add symlink")
        client = fresh_client()
        r = _cite_file(client, link, "Outside evidence.")
        assert "error" in r, f"symlink to outside file accepted: {r}"
        assert "resolves outside the repository" in r["error"], r["error"]
        assert link in r["error"], r["error"]
        assert outside in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_failing_git_status_rejects_not_clean():
    """A non-zero exit of the `git status` call rejects the citation (fail closed)
    with an error naming the git failure; it is never read as 'clean'."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("Committed evidence.\n")
        _git_run(real_repo, "add", "evidence.md")
        _git_run(real_repo, "commit", "-m", "add evidence")
        real_git = shutil.which("git")
        wrapper = os.path.join(base, "git-status-fails.sh")
        with open(wrapper, "w") as f:
            f.write(
                "#!/bin/sh\n"
                'if [ "$1" = "status" ]; then echo "fatal: simulated status failure" >&2; exit 128; fi\n'
                f'exec "{real_git}" "$@"\n'
            )
        os.chmod(wrapper, 0o755)
        client = fresh_client()
        # Seam: the function builds every git call from core.GIT_EXE.
        sys.modules["engram_core"].GIT_EXE = wrapper
        r = _cite_file(client, os.path.join(real_repo, "evidence.md"),
                       "Committed evidence.")
        assert "error" in r, f"failing git status read as clean: {r}"
        assert "git status failed" in r["error"], r["error"]
        assert "simulated status failure" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _untracked_inrepo_link_setup(base, real_repo, wrapper_body):
    """Commit evidence.md, add an UNTRACKED link sub/lnk.md -> ../evidence.md, and
    install a git wrapper (core.GIT_EXE seam) that runs `wrapper_body` for the
    link-classification call (`rev-parse --show-toplevel` with cwd == sub/) and
    execs the real git for every other call. Returns the link path."""
    with open(os.path.join(real_repo, "evidence.md"), "w") as f:
        f.write("Committed evidence.\n")
    _commit_all(real_repo, "add evidence")
    os.makedirs(os.path.join(real_repo, "sub"))
    link = os.path.join(real_repo, "sub", "lnk.md")
    os.symlink(os.path.join("..", "evidence.md"), link)
    real_git = shutil.which("git")
    wrapper = os.path.join(base, "git-classify-fails.sh")
    with open(wrapper, "w") as f:
        f.write(
            "#!/bin/sh\n"
            'case "$(pwd -P)" in\n'
            f'  */sub) if [ "$1" = "rev-parse" ] && [ "$2" = "--show-toplevel" ]; then {wrapper_body}; fi ;;\n'
            "esac\n"
            f'exec "{real_git}" "$@"\n'
        )
    os.chmod(wrapper, 0o755)
    return link, wrapper


def test_git_enforcement_link_classify_git_error_rejects_naming_exit_and_stderr():
    """Cell 46A: a non-zero `rev-parse --show-toplevel` that is NOT git's
    "not a git repository" answer must not classify the link as environment:
    the citation is rejected, naming the link, git's exit code and its stderr."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        link, wrapper = _untracked_inrepo_link_setup(
            base, real_repo, 'echo "fatal: simulated classify failure" >&2; exit 1')
        client = fresh_client()
        sys.modules["engram_core"].GIT_EXE = wrapper
        r = _cite_file(client, link, "Committed evidence.")
        assert "error" in r, f"git error during link classification vouched: {r}"
        assert link in r["error"], r["error"]
        assert "exit 1" in r["error"], r["error"]
        assert "simulated classify failure" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_link_classify_signal_exit_rejects():
    """Cell 46B1: a git killed by a signal (exit 137) during link classification
    rejects the citation, naming the link and the exit code."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        link, wrapper = _untracked_inrepo_link_setup(base, real_repo, "exit 137")
        client = fresh_client()
        sys.modules["engram_core"].GIT_EXE = wrapper
        r = _cite_file(client, link, "Committed evidence.")
        assert "error" in r, f"git exit 137 during link classification vouched: {r}"
        assert link in r["error"], r["error"]
        assert "137" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_link_classify_timeout_rejects_naming_exception():
    """Cell 46B2: a git TIMEOUT during link classification rejects the citation,
    naming the link and the exception class (it is never read as 'environment')."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        link, wrapper = _untracked_inrepo_link_setup(base, real_repo, "sleep 5")
        client = fresh_client()
        core = sys.modules["engram_core"]
        core.GIT_EXE = wrapper
        core.GIT_TIMEOUT = 1
        r = _cite_file(client, link, "Committed evidence.")
        assert "error" in r, f"git timeout during link classification vouched: {r}"
        assert link in r["error"], r["error"]
        assert "TimeoutExpired" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_link_classify_missing_git_rejects_naming_exception():
    """Cell 46B3: git missing entirely (FileNotFoundError) rejects, naming the
    link and the exception class."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        link, wrapper = _untracked_inrepo_link_setup(base, real_repo, "exit 1")
        client = fresh_client()
        sys.modules["engram_core"].GIT_EXE = os.path.join(base, "no-such-git")
        r = _cite_file(client, link, "Committed evidence.")
        assert "error" in r, f"missing git during link classification vouched: {r}"
        assert link in r["error"], r["error"]
        assert "FileNotFoundError" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_link_classify_genuine_not_a_repo_stays_environment():
    """Cell 46C: a link genuinely outside any work tree (git answers "not a git
    repository") is still environment and the citation is accepted, even when
    every git call goes through a pass-through wrapper."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("Committed evidence.\n")
        _commit_all(real_repo, "add evidence")
        env_link = os.path.join(base, "envlink.md")
        os.symlink(os.path.join(real_repo, "evidence.md"), env_link)
        wrapper = os.path.join(base, "git-passthru.sh")
        with open(wrapper, "w") as f:
            f.write(f'#!/bin/sh\nexec "{shutil.which("git")}" "$@"\n')
        os.chmod(wrapper, 0o755)
        client = fresh_client()
        sys.modules["engram_core"].GIT_EXE = wrapper
        r = _cite_file(client, env_link, "Committed evidence.")
        assert r.get("status") == "created", f"environment link outside every work tree rejected: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _commit_all(repo, msg):
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-m", msg)


def test_git_enforcement_uncommitted_symlink_to_committed_inrepo_file_rejected():
    """Cell 7: an UNCOMMITTED symlink to a committed in-repo file is rejected.

    The bytes are committed but the cited NAME's mapping to them is not.
    """
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("Committed target evidence.\n")
        _commit_all(real_repo, "add evidence")
        link = os.path.join(real_repo, "evlink.md")
        os.symlink("evidence.md", link)
        client = fresh_client()
        r = _cite_file(client, link, "Committed target evidence.")
        assert "error" in r, f"uncommitted symlink vouched for committed bytes: {r}"
        assert "evlink.md" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_retargeted_committed_symlink_rejected():
    """Cell 8: a committed symlink RETARGETED in the worktree (uncommitted) to
    another committed file is rejected: the cited name is modified."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        for n, body in (("a.md", "Evidence A.\n"), ("b.md", "Evidence B.\n")):
            with open(os.path.join(real_repo, n), "w") as f:
                f.write(body)
        link = os.path.join(real_repo, "evlink.md")
        os.symlink("a.md", link)
        _commit_all(real_repo, "add a, b, symlink to a")
        os.remove(link)
        os.symlink("b.md", link)
        client = fresh_client()
        r = _cite_file(client, link, "Evidence B.")
        assert "error" in r, f"retargeted symlink accepted: {r}"
        assert "evlink.md" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_untracked_directory_symlink_to_committed_file_rejected():
    """Cell 9: a file cited through an UNTRACKED directory symlink is rejected."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        os.makedirs(os.path.join(real_repo, "realdir"))
        with open(os.path.join(real_repo, "realdir", "f.md"), "w") as f:
            f.write("Dir evidence.\n")
        _commit_all(real_repo, "add realdir/f.md")
        os.symlink("realdir", os.path.join(real_repo, "dirlink"))
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "dirlink", "f.md"),
                       "Dir evidence.")
        assert "error" in r, f"untracked directory symlink accepted: {r}"
        assert "dirlink" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_dotdot_prefixed_names_inside_repo_accepted():
    """Cell 10: committed `..foo` and `..d/f.md` are INSIDE the repo. Pins the
    `'..' + os.sep` separator: a bare startswith('..') would reject them."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "..foo"), "w") as f:
            f.write("Dotdot file evidence.\n")
        os.makedirs(os.path.join(real_repo, "..d"))
        with open(os.path.join(real_repo, "..d", "f.md"), "w") as f:
            f.write("Dotdot dir evidence.\n")
        _commit_all(real_repo, "add dotdot-prefixed names")
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "..foo"),
                       "Dotdot file evidence.")
        assert r.get("status") == "created", f"committed ..foo rejected: {r}"
        r = _cite_file(client, os.path.join(real_repo, "..d", "f.md"),
                       "Dotdot dir evidence.")
        assert r.get("status") == "created", f"committed ..d/f.md rejected: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_symlinked_repo_root_committed_accepted_untracked_rejected():
    """Cell 11: through a symlinked repo ROOT, a committed file is accepted and
    an untracked file is rejected."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("Root-link committed.\n")
        _commit_all(real_repo, "add evidence")
        with open(os.path.join(real_repo, "loose.md"), "w") as f:
            f.write("Root-link untracked.\n")
        client = fresh_client()
        r = _cite_file(client, os.path.join(linked_repo, "evidence.md"),
                       "Root-link committed.")
        assert r.get("status") == "created", f"committed via symlinked root rejected: {r}"
        r = _cite_file(client, os.path.join(linked_repo, "loose.md"),
                       "Root-link untracked.")
        assert "error" in r, f"untracked via symlinked root accepted: {r}"
        assert "untracked" in r["error"].lower(), r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_inrepo_self_symlink_cannot_collapse_cited_path():
    """Cell 12: an untracked `self -> .` symlink in the repo must not let
    `repo/self/f.md` (or `repo/self/self/f.md`) anchor at `repo/self` and
    collapse to the committed `f.md`. The anchor is the FARTHEST ancestor that
    realpaths to the root, so the checked path stays `self/f.md`."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "f.md"), "w") as f:
            f.write("Self-link evidence.\n")
        _commit_all(real_repo, "add f.md")
        os.symlink(".", os.path.join(real_repo, "self"))
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "self", "f.md"),
                       "Self-link evidence.")
        assert "error" in r, f"self-symlink path collapsed and accepted: {r}"
        r = _cite_file(client, os.path.join(real_repo, "self", "self", "f.md"),
                       "Self-link evidence.")
        assert "error" in r, f"double self-symlink path collapsed and accepted: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_committed_symlink_to_modified_target_rejected_naming_target():
    """Cell 13: a committed symlink to a committed in-repo target that has an
    uncommitted modification is rejected, and the error names the resolved
    target. Pins the resolved-target check (the cited symlink path alone is
    clean in `git status`)."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        target = os.path.join(real_repo, "target.md")
        with open(target, "w") as f:
            f.write("Original target.\n")
        os.symlink("target.md", os.path.join(real_repo, "link.md"))
        _commit_all(real_repo, "add target + link")
        with open(target, "w") as f:
            f.write("Modified target.\n")
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "link.md"),
                       "Modified target.")
        assert "error" in r, f"symlink to modified target accepted: {r}"
        assert "resolved target 'target.md'" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_outside_symlink_to_repo_subdir_accepted_link_is_environment():
    """Cell 14 (CHANGED expectation, round 4): a symlink OUTSIDE any work tree
    pointing at a repo SUBDIRECTORY (`<base>/sublink -> repo/sub`) is now
    ACCEPTED. It was rejected at 7b976c1a ("not inside the repository"). The
    link lives outside every git work tree, so it is environment (the same class
    as macOS /var -> /private/var); the physical file is committed and clean."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        os.makedirs(os.path.join(real_repo, "sub"))
        with open(os.path.join(real_repo, "sub", "f.md"), "w") as f:
            f.write("Subdir evidence.\n")
        _commit_all(real_repo, "add sub/f.md")
        os.symlink(os.path.join(real_repo, "sub"), os.path.join(base, "sublink"))
        client = fresh_client()
        r = _cite_file(client, os.path.join(base, "sublink", "f.md"),
                       "Subdir evidence.")
        assert r.get("status") == "created", f"environment link above the repo rejected: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_untracked_symlink_into_nested_repo_rejected():
    """Cell 15: an UNTRACKED symlink inside repo A pointing at a file committed in
    a NESTED repo B is rejected (dev rejected it; 7b976c1a accepted it). Also the
    sibling-repo variant (B beside A, not inside it)."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        nested = os.path.join(real_repo, "inner")
        _init_git_repo(nested)
        with open(os.path.join(nested, "f.md"), "w") as f:
            f.write("Nested repo evidence.\n")
        _commit_all(nested, "add f.md")
        sib = os.path.join(base, "sibling")
        _init_git_repo(sib)
        with open(os.path.join(sib, "g.md"), "w") as f:
            f.write("Sibling repo evidence.\n")
        _commit_all(sib, "add g.md")
        os.symlink(os.path.join("inner", "f.md"), os.path.join(real_repo, "lnk.md"))
        os.symlink(os.path.join(sib, "g.md"), os.path.join(real_repo, "lnk2.md"))
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "lnk.md"), "Nested repo evidence.")
        assert "error" in r, f"untracked link into nested repo accepted: {r}"
        assert "lnk.md" in r["error"] and "untracked" in r["error"].lower(), r["error"]
        r = _cite_file(client, os.path.join(real_repo, "lnk2.md"), "Sibling repo evidence.")
        assert "error" in r, f"untracked link into sibling repo accepted: {r}"
        assert "lnk2.md" in r["error"] and "untracked" in r["error"].lower(), r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _dirlink_repo():
    """repo with committed realdir/f.md, otherdir/f.md and a COMMITTED dirlink -> realdir."""
    base, real_repo, linked_repo = _symlinked_repo()
    for d, body in (("realdir", "Dirlink evidence.\n"), ("otherdir", "Other dir evidence.\n")):
        os.makedirs(os.path.join(real_repo, d))
        with open(os.path.join(real_repo, d, "f.md"), "w") as f:
            f.write(body)
    os.symlink("realdir", os.path.join(real_repo, "dirlink"))
    _commit_all(real_repo, "add dirs + dirlink")
    return base, real_repo


def test_git_enforcement_committed_directory_symlink_to_committed_file_accepted():
    """Cell 16: a path through a COMMITTED directory symlink (as engram-alpha's
    tools/engine) to a committed, clean file is accepted. Rejected at 7b976c1a
    (cat-file `sha:dirlink/f.md` fails: git stores the link as a blob)."""
    base, real_repo = _dirlink_repo()
    try:
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "dirlink", "f.md"), "Dirlink evidence.")
        assert r.get("status") == "created", f"committed dir symlink path rejected: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_committed_directory_symlink_modified_target_rejected():
    """Cell 16b: same committed dir link, target file modified -> rejected."""
    base, real_repo = _dirlink_repo()
    try:
        with open(os.path.join(real_repo, "realdir", "f.md"), "w") as f:
            f.write("Dirlink evidence MODIFIED.\n")
        client = fresh_client()
        r = _cite_file(client, os.path.join(real_repo, "dirlink", "f.md"),
                       "Dirlink evidence MODIFIED.")
        assert "error" in r, f"modified target through dir link accepted: {r}"
        assert "modified" in r["error"].lower(), r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_committed_directory_symlink_retargeted_rejected():
    """Cell 16c: same committed dir link, retargeted in the worktree (uncommitted)
    to another committed dir -> rejected, naming the link as modified."""
    base, real_repo = _dirlink_repo()
    try:
        link = os.path.join(real_repo, "dirlink")
        os.remove(link)
        os.symlink("otherdir", link)
        client = fresh_client()
        r = _cite_file(client, os.path.join(link, "f.md"), "Other dir evidence.")
        assert "error" in r, f"retargeted dir link accepted: {r}"
        assert "dirlink" in r["error"] and "modified" in r["error"].lower(), r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _dotdot_repo(commit_physical):
    """repo/link -> repo/a/b (committed); repo/f.md committed; repo/a/f.md committed
    iff commit_physical. Cited path repo/link/../f.md is PHYSICALLY repo/a/f.md."""
    base, real_repo, linked_repo = _symlinked_repo()
    os.makedirs(os.path.join(real_repo, "a", "b"))
    with open(os.path.join(real_repo, "a", "b", "keep"), "w") as f:
        f.write("keep\n")
    with open(os.path.join(real_repo, "f.md"), "w") as f:
        f.write("ROOT lexical evidence.\n")
    os.symlink(os.path.join("a", "b"), os.path.join(real_repo, "link"))
    _commit_all(real_repo, "add f.md, link")
    with open(os.path.join(real_repo, "a", "f.md"), "w") as f:
        f.write("PHYSICAL evidence.\n")
    if commit_physical:
        _commit_all(real_repo, "add a/f.md")
    return base, real_repo


def test_git_enforcement_dotdot_after_symlink_checks_the_physical_file():
    """Cell 17: `repo/link/../f.md` with `link -> repo/a/b` is physically
    `repo/a/f.md`. Untracked physical file is rejected even though the lexical
    `repo/f.md` is committed (accepted at 7b976c1a); committed physical file is
    accepted and content_hash is the sha256 of repo/a/f.md's bytes; the quote is
    verified against the same physical file."""
    import hashlib
    base, real_repo = _dotdot_repo(commit_physical=False)
    try:
        client = fresh_client()
        cited = os.path.join(real_repo, "link", "..", "f.md")
        r = _cite_file(client, cited, "PHYSICAL evidence.")
        assert "error" in r, f"lexically-committed path vouched for untracked physical file: {r}"
        assert "untracked" in r["error"].lower(), r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    base, real_repo = _dotdot_repo(commit_physical=True)
    try:
        client = fresh_client()
        cited = os.path.join(real_repo, "link", "..", "f.md")
        # a quote present only in the LEXICAL file must not verify
        r = _cite_file(client, cited, "ROOT lexical evidence.")
        assert "error" in r, f"quote verified against the lexical file: {r}"
        r = _cite_file(client, cited, "PHYSICAL evidence.")
        assert r.get("status") == "created", f"committed physical file rejected: {r}"
        insp = client.call("engram_inspect", {"node_id": r["observation_id"], "view": "deep"})
        meta = insp["node"].get("metadata", "{}")
        if isinstance(meta, str):
            meta = json.loads(meta)
        with open(os.path.join(real_repo, "a", "f.md"), "rb") as f:
            assert meta["content_hash"] == hashlib.sha256(f.read()).hexdigest()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_symlink_loop_rejected_promptly():
    """Cell 18: a symlink loop (a -> b -> a, and a self loop) is a clean
    rejection with a message, and the calls complete well under 5 s."""
    import time
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        os.symlink("b", os.path.join(real_repo, "a"))
        os.symlink("a", os.path.join(real_repo, "b"))
        os.symlink("c", os.path.join(real_repo, "c"))
        client = fresh_client()
        t0 = time.monotonic()
        for p in (("a", "f.md"), ("c",)):
            r = _cite_file(client, os.path.join(real_repo, *p), "anything")
            assert "error" in r and r["error"], f"loop not rejected with a message: {r}"
        assert time.monotonic() - t0 < 5
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_with_trace_agrees_with_realpath_on_six_trees():
    """Cell 19: `_resolve_with_trace` reaches the same final path as
    os.path.realpath (relative links, absolute links, chained links, `..` after
    a link, dir links, link to link, relative cited path), records each link as
    the absolute path of the LINK, and bounds a loop."""
    import errno
    import engram_observation
    base = tempfile.mkdtemp(prefix="engram_test_trace_")
    cwd = os.getcwd()
    try:
        j = lambda *a: os.path.join(base, *a)
        os.makedirs(j("x", "y", "z"))
        os.makedirs(j("real", "sub"))
        for rel in (("x", "f.md"), ("x", "y", "f.md"), ("real", "sub", "f.md")):
            open(j(*rel), "w").write("k\n")
        os.symlink("x/f.md", j("rel_link"))                    # 1 relative file link
        os.symlink(j("x", "f.md"), j("abs_link"))              # 2 absolute file link
        os.symlink("c2", j("c1")); os.symlink("c3", j("c2")); os.symlink("x/f.md", j("c3"))  # 3 chain
        os.symlink("x/y", j("dl"))                             # 4 `..` after a dir link
        os.symlink("real", j("dirlink"))                       # 5 dir link
        os.symlink("rel_link", j("l2"))                        # 6 link to link
        cases = [
            j("rel_link"), j("abs_link"), j("c1"), j("dl", "..", "f.md"),
            j("dirlink", "sub", "f.md"), j("l2"), j("x", "y", "..", "f.md"),
            j("dl", "z", "..", "..", "f.md"),
        ]
        for p in cases:
            physical, links = engram_observation._resolve_with_trace(p)
            assert physical == os.path.realpath(p), (p, physical, os.path.realpath(p))
            for L in links:
                assert os.path.isabs(L) and os.path.islink(L), (p, L)
        _, links = engram_observation._resolve_with_trace(j("l2"))
        assert [os.path.basename(L) for L in links][-2:] == ["l2", "rel_link"], links  # a symlinked TMPDIR adds a leading link
        os.chdir(base)
        rel = os.path.join("dl", "..", "f.md")
        physical, _ = engram_observation._resolve_with_trace(rel)
        assert physical == os.path.realpath(rel)
        os.symlink("loopb", j("loopa")); os.symlink("loopa", j("loopb"))
        try:
            engram_observation._resolve_with_trace(j("loopa", "f.md"))
            raise AssertionError("loop did not raise")
        except OSError as e:
            assert e.errno == errno.ELOOP
    finally:
        os.chdir(cwd)
        shutil.rmtree(base, ignore_errors=True)


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
        test_quote_verified_https_false,
        test_quote_verified_file_true,
        test_quote_verified_stored_in_metadata,
        test_quote_verified_batch_propagates,
        test_quote_verified_visible_in_recall_view,
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


# --- #3292 round 5: the path walk must fail where the OS fails -------------------------

def _lenient_path_variants(real_repo):
    """Cited paths the OS refuses (ENOENT / ENOTDIR) but a lenient walk would accept."""
    return {
        "missing/../f.md": os.path.join(real_repo, "missing", "..", "f.md"),
        "f.md/": os.path.join(real_repo, "f.md") + "/",
        "f.md/../f.md": os.path.join(real_repo, "f.md", "..", "f.md"),
    }


def test_git_enforcement_os_rejected_cited_paths_cannot_skip_quote_verification():
    """Through the public path: a FABRICATED quote against a path the OS cannot open
    (missing intermediate dir, trailing slash or `/..` after a regular file) is
    rejected, never created. Otherwise _verify_quote_in_source skips the quote check
    because the cited path does not exist."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "f.md"), "w") as f:
            f.write("The real committed text.\n")
        _commit_all(real_repo, "add f.md")
        client = fresh_client()
        for label, cited in _lenient_path_variants(real_repo).items():
            r = _cite_file(client, cited, "A completely fabricated quote.")
            assert r.get("status") != "created", f"{label}: fabricated quote CREATED: {r}"
            assert r.get("error"), f"{label}: no error message: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_with_trace_fails_where_the_os_fails():
    """`_resolve_with_trace` raises OSError for the OS-refused shapes (ENOENT for a
    missing intermediate, ENOTDIR for a non-directory used as a directory), while a
    missing FINAL component and valid directory spellings still match realpath."""
    import errno
    import engram_observation
    base = os.path.realpath(tempfile.mkdtemp(prefix="engram_test_trace_os_"))
    try:
        j = lambda *a: os.path.join(base, *a)
        os.makedirs(j("d"))
        open(j("f.md"), "w").write("k\n")
        os.symlink("f.md", j("flink"))
        expect = {
            j("missing", "..", "f.md"): errno.ENOENT,
            j("f.md") + "/": errno.ENOTDIR,
            j("f.md", "..", "f.md"): errno.ENOTDIR,
            j("f.md", "."): errno.ENOTDIR,
            j("flink") + "/": errno.ENOTDIR,
        }
        for p, code in expect.items():
            try:
                engram_observation._resolve_with_trace(p)
            except OSError as e:
                assert e.errno == code, (p, e.errno, code)
            else:
                raise AssertionError(f"{p!r} resolved but the OS refuses it")
        for p in (j("d") + "/", j("d", "."), j("d", "..", "f.md"), j("nofile"), j("d", "nofile")):
            physical, _ = engram_observation._resolve_with_trace(p)
            assert physical == os.path.realpath(p), (p, physical, os.path.realpath(p))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_git_enforcement_link_classify_runs_under_lc_all_c():
    """The classification call runs with LC_ALL=C, so git's 'not a git repository'
    answer is English whatever the caller's locale. The wrapper answers in a
    'translated' message unless LC_ALL=C: a link outside every work tree must
    still be environment (accepted)."""
    base, real_repo, linked_repo = _symlinked_repo()
    try:
        with open(os.path.join(real_repo, "evidence.md"), "w") as f:
            f.write("Committed evidence.\n")
        _commit_all(real_repo, "add evidence")
        env_link = os.path.join(base, "envlink.md")
        os.symlink(os.path.join(real_repo, "evidence.md"), env_link)
        wrapper = os.path.join(base, "git-translated.sh")
        with open(wrapper, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f'if [ "$1" = "rev-parse" ] && [ "$2" = "--show-toplevel" ] && [ "$LC_ALL" != "C" ] && [ "$(pwd -P)" = "{base}" ]; then\n'
                '  echo "fatal: ceci n\'est pas un depot git" >&2; exit 128; fi\n'
                f'exec "{shutil.which("git")}" "$@"\n'
            )
        os.chmod(wrapper, 0o755)
        client = fresh_client()
        sys.modules["engram_core"].GIT_EXE = wrapper
        r = _cite_file(client, env_link, "Committed evidence.")
        assert r.get("status") == "created", f"translated not-a-repo read as a failure: {r}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- #3393: a file:// citation must name a regular file -----------------------

def _dir_repo():
    """Repo with a committed directory `d` (holding note.md) and a committed
    symlink `dlink` -> `d`. Returns (base, repo)."""
    base = os.path.realpath(tempfile.mkdtemp(prefix="engram_test_3393_"))
    repo = os.path.join(base, "repo")
    _init_git_repo(repo)
    os.makedirs(os.path.join(repo, "d"))
    with open(os.path.join(repo, "d", "note.md"), "w") as f:
        f.write("Quote inside the directory.\n")
    os.symlink("d", os.path.join(repo, "dlink"))
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-m", "dir evidence")
    return base, repo


def _count_nodes():
    import engram_core as _core
    # _get_db() creates the schema on first use: a bare sqlite3.connect on a
    # fresh client's DB file has no `nodes` table until a tool has run.
    conn = _core._get_db()
    return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


def _cite_dir(client, url, text, **extra):
    payload = {
        "url": url,
        "title": "Directory evidence",
        "quoted_text": text,
        "interpretation": "Testing that a directory is not evidence.",
        "claim": f"Directory citation {url[-12:]} {text[:10]}",
        "quote_type": "hard_data",
    }
    payload.update(extra)
    return client.call("engram_add_observation", payload)


def _assert_rejected_as_not_a_file(r, path, before):
    assert "error" in r, f"directory citation was not rejected: {r}"
    assert r.get("status") != "created" and "observation_id" not in r, r
    assert _count_nodes() == before, "a node row was written for a directory citation"
    err = r["error"].lower()
    assert "must be a file" in err or "regular file" in err, r["error"]
    assert path.rstrip("/") in r["error"], f"error does not name the path: {r['error']}"


def _assert_rejected_no_node(r, before):
    """The evidence_id path reaches _verify_quote_in_source before the file-version
    check, so a directory is refused there (message not under this fix's control):
    assert only the integrity property, rejected and nothing written."""
    assert "error" in r, f"directory citation was not rejected: {r}"
    assert r.get("status") != "created" and "observation_id" not in r, r
    assert _count_nodes() == before, "a node row was written for a directory citation"


def test_directory_citation_rejected_by_name_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        before = _count_nodes()
        d = os.path.join(repo, "d")
        r = _cite_dir(client, f"file://{d}", "Quote inside the directory.")
        _assert_rejected_as_not_a_file(r, d, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_directory_citation_trailing_slash_rejected_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        before = _count_nodes()
        d = os.path.join(repo, "d")
        r = _cite_dir(client, f"file://{d}/", "Quote inside the directory.")
        _assert_rejected_as_not_a_file(r, d, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_directory_citation_fabricated_quote_rejected_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        before = _count_nodes()
        d = os.path.join(repo, "d")
        r = _cite_dir(client, f"file://{d}", "A sentence nobody ever wrote anywhere.")
        _assert_rejected_as_not_a_file(r, d, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_symlink_to_directory_citation_rejected_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        before = _count_nodes()
        link = os.path.join(repo, "dlink")
        r = _cite_dir(client, f"file://{link}", "Quote inside the directory.")
        _assert_rejected_as_not_a_file(r, link, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_directory_citation_with_caller_content_hash_rejected_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        before = _count_nodes()
        d = os.path.join(repo, "d")
        r = _cite_dir(client, f"file://{d}", "Quote inside the directory.",
                      content_hash="0" * 64)
        _assert_rejected_as_not_a_file(r, d, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _evidence_for_then_swapped_to_dir(client, repo):
    """Cite committed file `f` (creating its evidence node), then replace `f` by a
    committed directory of the same name. Returns (evidence_id, path). A later
    evidence_id citation reaches the observation-level _capture_file_version
    with no evidence-creation guard in front of it."""
    f = os.path.join(repo, "f")
    with open(f, "w") as fh:
        fh.write("Quote inside the directory.\n")
    _git_run(repo, "add", "f")
    _git_run(repo, "commit", "-m", "add f")
    first = _cite_dir(client, f"file://{f}", "Quote inside the directory.")
    assert first.get("status") == "created", first
    os.remove(f)
    os.makedirs(f)
    with open(os.path.join(f, "note.md"), "w") as fh:
        fh.write("Quote inside the directory.\n")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-m", "f becomes a directory")
    return first["evidence_id"], f


def _cite_by_evidence_id(client, evidence_id, text, **extra):
    payload = {
        "evidence_id": evidence_id,
        "quoted_text": text,
        "interpretation": "Testing that a directory is not evidence.",
        "claim": f"Evidence-id citation of a directory {text[:10]}",
        "quote_type": "hard_data",
    }
    payload.update(extra)
    return client.call("engram_add_observation", payload)


def test_directory_by_evidence_id_with_content_hash_real_quote_rejected_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        ev_id, f = _evidence_for_then_swapped_to_dir(client, repo)
        before = _count_nodes()
        r = _cite_by_evidence_id(client, ev_id, "Quote inside the directory.",
                                 content_hash="0" * 64)
        _assert_rejected_no_node(r, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_directory_by_evidence_id_with_content_hash_fabricated_quote_rejected_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        ev_id, f = _evidence_for_then_swapped_to_dir(client, repo)
        before = _count_nodes()
        r = _cite_by_evidence_id(client, ev_id, "A sentence nobody ever wrote anywhere.",
                                 content_hash="0" * 64)
        _assert_rejected_no_node(r, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_regular_file_citation_still_accepted_3393():
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        r = _cite_dir(client, f"file://{os.path.join(repo, 'd', 'note.md')}",
                      "Quote inside the directory.")
        assert r.get("status") == "created", r
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- #3393 round 1: every entry rejects a non-file ----------------------------

def _count_evidence():
    import engram_core as _core
    conn = _core._get_db()
    return conn.execute("SELECT COUNT(*) FROM nodes WHERE type = 'evidence'").fetchone()[0]


def _add_evidence(client, url):
    return client.call("engram_add_evidence", {"url": url, "title": "Non-file evidence"})


def _assert_evidence_rejected_no_row(r, path, before):
    assert "error" in r, f"non-file evidence was not rejected: {r}"
    assert r.get("status") != "created" and "evidence_id" not in r, r
    assert _count_evidence() == before, "an evidence row was written for a non-file"
    if path:
        assert "must be a file" in r["error"] and path.rstrip("/") in r["error"], r["error"]


def test_A_add_evidence_directory_outside_git_rejected_no_row_3393():
    """Cell A: a directory outside any git repo. The isfile check must run BEFORE
    the not-in-git early return, or this returns `created` and the observation
    step later rejects, orphaning the evidence row."""
    base = os.path.realpath(tempfile.mkdtemp(prefix="engram_test_3393A_"))
    try:
        client = fresh_client()
        d = os.path.join(base, "plain_dir")
        os.makedirs(d)
        before = _count_evidence()
        r = _add_evidence(client, f"file://{d}")
        _assert_evidence_rejected_no_row(r, d, before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_B_add_evidence_dev_null_rejected_no_row_3393():
    """Cell B: file:///dev/null (a character device) is not a regular file."""
    client = fresh_client()
    before = _count_evidence()
    r = _add_evidence(client, "file:///dev/null")
    _assert_evidence_rejected_no_row(r, "/dev/null", before)


def _cross_repo_setup():
    """source_repo with a committed directory `d` (note.md inside) and a committed
    regular file `f.md`; the cited file lives outside source_repo."""
    base = os.path.realpath(tempfile.mkdtemp(prefix="engram_test_3393C_"))
    src = os.path.join(base, "source_repo")
    _init_git_repo(src)
    os.makedirs(os.path.join(src, "d"))
    with open(os.path.join(src, "d", "note.md"), "w") as fh:
        fh.write("Committed copy.\n")
    with open(os.path.join(src, "f.md"), "w") as fh:
        fh.write("Committed copy.\n")
    _git_run(src, "add", "-A")
    _git_run(src, "commit", "-m", "source repo")
    cited = os.path.join(base, "cited.md")
    with open(cited, "w") as fh:
        fh.write("Cited quote text.\n")
    return base, src, cited


def _cite_cross_repo(client, cited, src, repo_path):
    return client.call("engram_add_observation", {
        "url": f"file://{cited}",
        "title": "Cross-repo evidence",
        "quoted_text": "Cited quote text.",
        "interpretation": "Testing the cross-repo repo_path object type.",
        "claim": f"Cross-repo citation of {repo_path}",
        "quote_type": "hard_data",
        "source_repo": src,
        "repo_path": repo_path,
    })


def test_C_cross_repo_repo_path_directory_rejected_3393():
    """Cell C: the repo_path must name a blob; a committed directory is a tree."""
    base, src, cited = _cross_repo_setup()
    try:
        client = fresh_client()
        before = _count_nodes()
        r = _cite_cross_repo(client, cited, src, "d")
        assert "error" in r, f"a directory repo_path was accepted: {r}"
        assert r.get("status") != "created" and "observation_id" not in r, r
        assert _count_nodes() == before, "a node row was written for a directory repo_path"
        assert "repo_path" in r["error"] and "file" in r["error"].lower(), r["error"]
        assert "'d'" in r["error"], f"error does not name the repo_path: {r['error']}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_D_cross_repo_repo_path_file_still_accepted_3393():
    """Cell D: a committed regular file as repo_path is unchanged behaviour."""
    base, src, cited = _cross_repo_setup()
    try:
        client = fresh_client()
        r = _cite_cross_repo(client, cited, src, "f.md")
        assert r.get("status") == "created", r
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_E_fifo_citation_rejected_promptly_never_hangs_3393():
    """Cell E: a FIFO inside a repo blocks open() forever. The call runs in a
    daemon thread with a hard 10 s bound; exceeding it FAILS the cell (and the
    FIFO is opened read-write at the end so the blocked thread is released)."""
    import threading
    base, repo = _dir_repo()
    fifo = os.path.join(repo, "pipe")
    try:
        os.mkfifo(fifo)
        client = fresh_client()
        results = {}

        def _run():
            try:
                results["evidence"] = _add_evidence(client, f"file://{fifo}")
                results["observation"] = _cite_dir(client, f"file://{fifo}", "anything")
            except BaseException as e:  # surfaced by the assertions below
                results["exc"] = repr(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(10)
        hung = t.is_alive()
        if hung:
            # Release a thread blocked in open(fifo): a read-write open never blocks.
            fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            os.close(fd)
            t.join(5)
        assert not hung, "FIFO citation did not return within 10 s (blocked in open())"
        assert "exc" not in results, results
        for key in ("evidence", "observation"):
            r = results[key]
            assert "error" in r and r.get("status") != "created", (key, r)
            assert "must be a file" in r["error"], (key, r["error"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- #3393 round 2: quote verification requires a regular file; submodule pin --

def test_F_evidence_row_whose_path_became_a_fifo_rejected_never_hangs_3393():
    """Cell F: an evidence row is created for a committed regular file; the path is
    then replaced by a FIFO. engram_add_observation(evidence_id=...) reaches
    _verify_quote_in_source, which read the path after only an exists() test and
    blocked forever in open(). Daemon thread, hard 10 s bound; on a hang the FIFO
    is opened read-write so the blocked thread is released and the cell FAILS."""
    import threading
    base, repo = _dir_repo()
    try:
        client = fresh_client()
        f = os.path.join(repo, "f")
        with open(f, "w") as fh:
            fh.write("Quote inside the directory.\n")
        _git_run(repo, "add", "f")
        _git_run(repo, "commit", "-m", "add f")
        first = _cite_dir(client, f"file://{f}", "Quote inside the directory.")
        assert first.get("status") == "created", first
        os.remove(f)
        os.mkfifo(f)
        before = _count_nodes()
        results = {}

        def _run():
            try:
                results["r"] = _cite_by_evidence_id(
                    client, first["evidence_id"], "Quote inside the directory.")
            except BaseException as e:  # surfaced by the assertions below
                results["exc"] = repr(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(10)
        hung = t.is_alive()
        if hung:
            fd = os.open(f, os.O_RDWR | os.O_NONBLOCK)
            os.close(fd)
            t.join(5)
        assert not hung, "evidence_id citation of a FIFO did not return within 10 s"
        assert "exc" not in results, results
        r = results["r"]
        assert "error" in r and r.get("status") != "created" and "observation_id" not in r, r
        assert "must be a file" in r["error"].lower(), r["error"]
        assert f in r["error"], f"error does not name the path: {r['error']}"
        assert _count_nodes() == before, "a node row was written for a FIFO citation"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_G_cross_repo_repo_path_submodule_entry_rejected_3393():
    """Cell G: repo_path names a submodule (gitlink, mode 160000) entry whose commit
    object exists in source_repo, so `cat-file -e` passes and the `cat-file -t`
    non-blob branch (type `commit`) is the one that must reject it."""
    import subprocess
    base, src, cited = _cross_repo_setup()
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True,
                              text=True, check=True).stdout.strip()
        os.makedirs(os.path.join(src, "sub"))  # empty dir: a clean, un-initialised submodule
        _git_run(src, "update-index", "--add", "--cacheinfo", f"160000,{head},sub")
        _git_run(src, "commit", "-m", "gitlink entry")
        client = fresh_client()
        before = _count_nodes()
        r = _cite_cross_repo(client, cited, src, "sub")
        assert "error" in r, f"a submodule repo_path was accepted: {r}"
        assert r.get("status") != "created" and "observation_id" not in r, r
        assert _count_nodes() == before, "a node row was written for a submodule repo_path"
        assert "repo_path 'sub' must name a file" in r["error"], r["error"]
        assert "a commit" in r["error"], r["error"]
    finally:
        shutil.rmtree(base, ignore_errors=True)
