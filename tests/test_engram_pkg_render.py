"""Tests for engram-pkg render subcommand.

Coverage (per spec):
  1. Rendered markdown structure: R1 derived-view header, title/authors,
     inline (node_id) cites on sourced blocks, ## Abstract, ## Section titles,
     R2 appendix table sorted by agent with (origin unset) for no-origin nodes.
  2. Fidelity JSON (R3): per_author_sourced partitioned by agent_id,
     node_claim pulled from the package DB (not from the prose JSON),
     connective_prose populated.
  3. Strict validation:
     (a) prose JSON missing a manifest node → non-zero exit, nothing written.
     (b) manifest node absent from the package DB → non-zero exit, nothing written.
     (c) malformed JSON (manifest or prose) → non-zero exit, nothing written.
  4. No-LLM: the command runs offline — it is purely an assembler.

Fixture topology:
  - Package with 3 nodes: ob_a (origin agent_id="agent_alpha"),
    ob_b (origin agent_id="agent_beta"), ob_c (no origin — graceful degrade).
  - Manifest: title + authors + abstract_node_id=ob_a + one section with ob_b,ob_c.
  - Prose blocks (flat ordered list): sourced(ob_a), connective, sourced(ob_b),
    connective, sourced(ob_c).

Style mirrors test_scope_export.py: module-level temp dirs, importlib CLI load,
SimpleNamespace args, pytest fixtures.
"""

import atexit
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types

import pytest

# ---------------------------------------------------------------------------
# Repo root + CLI import (mirrors test_scope_export.py pattern)
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _import_cli():
    candidates = [
        os.path.join(REPO_ROOT, "src", "engram", "tools", "engram-pkg", "engram-pkg"),
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "engram", "tools", "engram-pkg", "engram-pkg",
        ),
    ]
    cli_path = None
    for c in candidates:
        if os.path.exists(c):
            cli_path = c
            break
    if cli_path is None:
        raise FileNotFoundError(f"engram-pkg CLI not found. Tried: {candidates}")

    spec = importlib.util.spec_from_loader(
        "engram_pkg_cli_render",
        importlib.machinery.SourceFileLoader("engram_pkg_cli_render", cli_path),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CLI = _import_cli()

# ---------------------------------------------------------------------------
# Module-level temp dirs — cleaned up on process exit
# ---------------------------------------------------------------------------

FIXTURE_BASE = tempfile.mkdtemp(prefix="engram_render_test_")
atexit.register(lambda: shutil.rmtree(FIXTURE_BASE, ignore_errors=True))


# ---------------------------------------------------------------------------
# Fixture package builder
# ---------------------------------------------------------------------------

def _build_fixture_package() -> str:
    """Build a minimal engram-package with 3 nodes via direct SQLite insert.

    Returns the path to the package directory (with a live knowledge.db).
    Uses direct SQLite rather than cmd_init to avoid the dump.sh shell dependency.

    Nodes:
      ob_0001  claim="Claim from alpha agent."     origin.agent_id="agent_alpha"
      ob_0002  claim="Claim from beta agent."      origin.agent_id="agent_beta"
      ob_0003  claim="Claim without origin."       no origin metadata
    """
    pkg_dir = os.path.join(FIXTURE_BASE, "fixture_pkg")
    os.makedirs(pkg_dir, exist_ok=True)

    # Write config.json so _ensure_pkg_config is a no-op.
    cfg_path = os.path.join(pkg_dir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "is_knowledge_pack": True}, f)

    db_path = os.path.join(pkg_dir, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Minimal schema: just the columns we need for render.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            claim TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'active',
            is_current INTEGER NOT NULL DEFAULT 1,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sequences (
            node_type TEXT PRIMARY KEY,
            last_num INTEGER NOT NULL DEFAULT 0
        )
    """)

    nodes = [
        (
            "ob_0001",
            "observation_factual",
            "Claim from alpha agent.",
            json.dumps({"origin": {
                "agent_id": "agent_alpha",
                "source_graph_node_id": "ob_1000",
                "lineage": "anthropic:opus",
            }}),
        ),
        (
            "ob_0002",
            "observation_factual",
            "Claim from beta agent.",
            json.dumps({"origin": {
                "agent_id": "agent_beta",
                "source_graph_node_id": "ob_2000",
                "lineage": "anthropic:sonnet",
            }}),
        ),
        (
            "ob_0003",
            "observation_factual",
            "Claim without origin.",
            "{}",
        ),
    ]
    for nid, ntype, claim, metadata in nodes:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, type, claim, metadata) VALUES (?, ?, ?, ?)",
            (nid, ntype, claim, metadata),
        )

    conn.commit()
    conn.close()
    return pkg_dir


@pytest.fixture(scope="module")
def fixture_pkg():
    """Build and return the fixture package dir once per module."""
    return _build_fixture_package()


# ---------------------------------------------------------------------------
# Manifest + prose builders (helpers shared across test classes)
# ---------------------------------------------------------------------------

def _write_manifest(dir_: str, body: dict) -> str:
    """Write a manifest JSON file; return its path."""
    p = os.path.join(dir_, "manifest.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2)
    return p


def _write_prose(dir_: str, blocks: list) -> str:
    """Write a prose-blocks JSON file; return its path."""
    p = os.path.join(dir_, "prose.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(blocks, f, indent=2)
    return p


def _default_manifest(pkg_dir: str) -> dict:
    return {
        "manifest_version": "1",
        "title": "Test Paper Title",
        "authors": ["agent_alpha", "agent_beta"],
        "abstract_node_id": "ob_0001",
        "sections": [
            {
                "title": "Findings",
                "node_ids_in_order": ["ob_0002", "ob_0003"],
            }
        ],
    }


def _default_prose() -> list:
    return [
        {"type": "sourced", "node_id": "ob_0001", "text": "Alpha abstract prose."},
        {"type": "connective", "text": "Transitional sentence."},
        {"type": "sourced", "node_id": "ob_0002", "text": "Beta sourced prose."},
        {"type": "connective", "text": "Another connector."},
        {"type": "sourced", "node_id": "ob_0003", "text": "No-origin sourced prose."},
    ]


def _run_render(pkg_dir: str, manifest_path: str, prose_path: str,
                output_path: str = None, fidelity_path: str = None) -> int:
    """Invoke cmd_render via SimpleNamespace args.

    Returns the exit code (0 = success). Catches SystemExit for validation
    failures. Captures stdout to a string (swaps sys.stdout temporarily for
    stdout-mode tests — not used in file-output mode).
    """
    args = types.SimpleNamespace(
        pkg=pkg_dir,
        auto_build=False,
        manifest=manifest_path,
        prose=prose_path,
        format="markdown",
        output=output_path,
        fidelity=fidelity_path,
    )
    try:
        rc = _CLI.cmd_render(args)
        return rc if rc is not None else 0
    except SystemExit as e:
        code = e.code
        if isinstance(code, int):
            return code
        return 1 if code else 0


# ---------------------------------------------------------------------------
# Test 1 — Rendered markdown structure
# ---------------------------------------------------------------------------

class TestMarkdownStructure:
    """R1 header, title/authors, inline cites, abstract, sections, R2 appendix."""

    def test_markdown_full_structure(self, fixture_pkg, tmp_path):
        """Full markdown output matches spec structure end-to-end."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0, "cmd_render returned non-zero unexpectedly"

        content = (tmp_path / "output.md").read_text(encoding="utf-8")

        # R1 header: derived-view disclaimer block
        assert "> **Derived view**" in content, "R1 derived-view header missing"
        assert "NOT the source of truth" in content, "R1 'NOT the source of truth' missing"
        assert "Regenerate:" in content, "R1 regenerate line missing"

        # Title
        assert "# Test Paper Title" in content, "Title heading missing"

        # Authors line
        assert "agent_alpha" in content, "Author agent_alpha missing"
        assert "agent_beta" in content, "Author agent_beta missing"

        # Abstract section (ob_0001 is abstract_node_id)
        assert "## Abstract" in content, "## Abstract section missing"
        assert "Alpha abstract prose." in content, "Abstract prose missing"
        assert "(ob_0001)" in content, "Inline cite (ob_0001) missing"

        # Body section
        assert "## Findings" in content, "## Findings section missing"
        assert "Beta sourced prose." in content, "Beta sourced prose missing"
        assert "(ob_0002)" in content, "Inline cite (ob_0002) missing"
        assert "No-origin sourced prose." in content, "No-origin prose missing"
        assert "(ob_0003)" in content, "Inline cite (ob_0003) missing"

        # Connective blocks: no inline citation
        assert "Transitional sentence." in content, "Connective block missing"
        # Connective must NOT have a node_id cite appended
        assert "Transitional sentence. (ob_" not in content, \
            "Connective block incorrectly received an inline citation"

        # R2 appendix
        assert "## Provenance / authorship" in content, "R2 appendix heading missing"
        assert "| package id |" in content, "R2 table header missing"

    def test_r2_table_sorted_by_agent(self, fixture_pkg, tmp_path):
        """R2 appendix table rows are sorted by originating agent."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)

        content = (tmp_path / "output.md").read_text(encoding="utf-8")

        # Find the appendix section and extract table rows.
        appendix_start = content.index("## Provenance / authorship")
        appendix_text = content[appendix_start:]

        # Extract lines that start with '| ob_'
        table_rows = [
            line.strip()
            for line in appendix_text.splitlines()
            if line.strip().startswith("| ob_")
        ]
        assert len(table_rows) == 3, f"Expected 3 data rows in R2 table, got {len(table_rows)}"

        # Extract agent column from each row (column index 2, 0-based, pipe-split)
        agents = [row.split("|")[2].strip() for row in table_rows]
        assert agents == sorted(agents), \
            f"R2 table rows not sorted by originating agent: {agents}"

    def test_r2_origin_unset_for_no_origin_node(self, fixture_pkg, tmp_path):
        """Node without origin metadata shows '(origin unset)' in R2 table."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)

        content = (tmp_path / "output.md").read_text(encoding="utf-8")
        assert "(origin unset)" in content, \
            "Expected '(origin unset)' for node with no origin metadata"


# ---------------------------------------------------------------------------
# Test 2 — Fidelity JSON (R3)
# ---------------------------------------------------------------------------

class TestFidelityJSON:
    """per_author_sourced partitioned by agent; node_claim from DB; connective_prose."""

    def test_fidelity_partitioned_by_agent(self, fixture_pkg, tmp_path):
        """per_author_sourced is keyed by agent_id and contains the right node_ids."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))

        assert "per_author_sourced" in fid, "per_author_sourced missing"
        per_author = fid["per_author_sourced"]

        # ob_0001 → agent_alpha, ob_0002 → agent_beta, ob_0003 → (origin unset)
        assert "agent_alpha" in per_author, "agent_alpha partition missing"
        assert "agent_beta" in per_author, "agent_beta partition missing"
        assert "(origin unset)" in per_author, "(origin unset) partition missing"

        alpha_ids = [e["node_id"] for e in per_author["agent_alpha"]]
        assert "ob_0001" in alpha_ids, "ob_0001 not in agent_alpha partition"

        beta_ids = [e["node_id"] for e in per_author["agent_beta"]]
        assert "ob_0002" in beta_ids, "ob_0002 not in agent_beta partition"

        unset_ids = [e["node_id"] for e in per_author["(origin unset)"]]
        assert "ob_0003" in unset_ids, "ob_0003 not in (origin unset) partition"

    def test_node_claim_from_db_not_prose(self, fixture_pkg, tmp_path):
        """node_claim in fidelity entries is the DB claim, not the prose text."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        # Deliberately use different text in prose than in DB claim.
        prose_with_different_text = [
            {"type": "sourced", "node_id": "ob_0001",
             "text": "THIS IS THE PROSE TEXT, not the claim."},
            {"type": "connective", "text": "connector"},
            {"type": "sourced", "node_id": "ob_0002",
             "text": "ALSO PROSE, not the claim."},
            {"type": "connective", "text": "another connector"},
            {"type": "sourced", "node_id": "ob_0003",
             "text": "PROSE FOR NO-ORIGIN."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_with_different_text)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))
        per_author = fid["per_author_sourced"]

        # Collect all entries
        all_entries = []
        for entries in per_author.values():
            all_entries.extend(entries)

        # Find ob_0001
        ob_0001_entry = next((e for e in all_entries if e["node_id"] == "ob_0001"), None)
        assert ob_0001_entry is not None, "ob_0001 entry not found in fidelity"

        # node_claim must be the DB value, not the prose text
        assert ob_0001_entry["node_claim"] == "Claim from alpha agent.", \
            f"node_claim should be DB value, got: {ob_0001_entry['node_claim']!r}"
        assert ob_0001_entry["rendered_text"] == "THIS IS THE PROSE TEXT, not the claim.", \
            f"rendered_text should be prose value, got: {ob_0001_entry['rendered_text']!r}"

    def test_connective_prose_populated(self, fixture_pkg, tmp_path):
        """connective_prose list contains all connective block texts."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))
        assert "connective_prose" in fid, "connective_prose missing from fidelity"

        conn_texts = [e["text"] for e in fid["connective_prose"]]
        assert "Transitional sentence." in conn_texts, \
            "Connective 'Transitional sentence.' not in fidelity"
        assert "Another connector." in conn_texts, \
            "Connective 'Another connector.' not in fidelity"

    def test_fidelity_fields_present(self, fixture_pkg, tmp_path):
        """Top-level fidelity fields: package, manifest, generated_for_proofread."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))
        assert fid.get("generated_for_proofread") is True, \
            "generated_for_proofread must be true"
        assert "package" in fid, "package field missing"
        assert "manifest" in fid, "manifest field missing"

    def test_status_pending(self, fixture_pkg, tmp_path):
        """All fidelity entries have status='pending'."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))
        for agent, entries in fid["per_author_sourced"].items():
            for e in entries:
                assert e.get("status") == "pending", \
                    f"Expected status='pending' for {e['node_id']}, got {e.get('status')!r}"
        for e in fid["connective_prose"]:
            assert e.get("status") == "pending", \
                f"Expected connective status='pending', got {e.get('status')!r}"


# ---------------------------------------------------------------------------
# Test 3 — Strict validation: non-zero exit + nothing written on failure
# ---------------------------------------------------------------------------

class TestStrictValidation:
    """Validation failures → sys.exit(non-zero) and no files written."""

    def test_prose_missing_manifest_node(self, fixture_pkg, tmp_path):
        """Prose JSON missing a manifest node_id → non-zero exit, nothing written."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        # Prose only covers ob_0001 and ob_0002; ob_0003 is missing.
        incomplete_prose = [
            {"type": "sourced", "node_id": "ob_0001", "text": "Abstract text."},
            {"type": "sourced", "node_id": "ob_0002", "text": "Beta text."},
            # ob_0003 intentionally absent
        ]
        prose_p = _write_prose(str(tmp_path), incomplete_prose)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc != 0, "Expected non-zero exit when prose missing a manifest node"
        assert not os.path.exists(out_p), "output.md must NOT be written on validation failure"
        assert not os.path.exists(fid_p), "fidelity.json must NOT be written on validation failure"

    def test_manifest_node_absent_from_package(self, fixture_pkg, tmp_path):
        """Manifest references a node that doesn't exist in the package → non-zero exit."""
        manifest_with_ghost = {
            "manifest_version": "1",
            "title": "Test",
            "authors": [],
            "abstract_node_id": None,
            "sections": [
                {
                    "title": "Section",
                    "node_ids_in_order": ["ob_0001", "ob_9999"],  # ob_9999 doesn't exist
                }
            ],
        }
        manifest_p = _write_manifest(str(tmp_path), manifest_with_ghost)

        prose_with_ghost = [
            {"type": "sourced", "node_id": "ob_0001", "text": "Text for ob_0001."},
            {"type": "sourced", "node_id": "ob_9999", "text": "Text for ghost node."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_with_ghost)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc != 0, "Expected non-zero exit when manifest references missing package node"
        assert not os.path.exists(out_p), "output.md must NOT be written on validation failure"
        assert not os.path.exists(fid_p), "fidelity.json must NOT be written on validation failure"

    def test_malformed_manifest_json(self, fixture_pkg, tmp_path):
        """Malformed manifest JSON → non-zero exit, nothing written."""
        manifest_path = str(tmp_path / "manifest.json")
        with open(manifest_path, "w") as f:
            f.write("{not valid json")

        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_path, prose_p, out_p, fid_p)
        assert rc != 0, "Expected non-zero exit for malformed manifest JSON"
        assert not os.path.exists(out_p), "output.md must NOT be written on malformed JSON"
        assert not os.path.exists(fid_p), "fidelity.json must NOT be written on malformed JSON"

    def test_malformed_prose_json(self, fixture_pkg, tmp_path):
        """Malformed prose JSON → non-zero exit, nothing written."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_path = str(tmp_path / "prose.json")
        with open(prose_path, "w") as f:
            f.write("[not valid json}")

        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_path, out_p, fid_p)
        assert rc != 0, "Expected non-zero exit for malformed prose JSON"
        assert not os.path.exists(out_p), "output.md must NOT be written on malformed prose JSON"
        assert not os.path.exists(fid_p), "fidelity.json must NOT be written on malformed prose JSON"

    def test_orphan_sourced_block_not_in_manifest(self, fixture_pkg, tmp_path):
        """Sourced block whose node_id is not in manifest → non-zero exit."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        prose_with_orphan = _default_prose() + [
            {"type": "sourced", "node_id": "ob_0001", "text": "Duplicate (ok)."},
            # ob_0042 is not in the manifest:
            {"type": "sourced", "node_id": "ob_0042", "text": "Orphan block."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_with_orphan)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc != 0, "Expected non-zero exit for orphan sourced block"

    def test_unsupported_format(self, fixture_pkg, tmp_path):
        """--format other than 'markdown' → non-zero exit."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())

        args = types.SimpleNamespace(
            pkg=fixture_pkg,
            auto_build=False,
            manifest=manifest_p,
            prose=prose_p,
            format="html",  # unsupported
            output=None,
            fidelity=str(tmp_path / "fidelity.json"),
        )
        rc = 0
        try:
            result = _CLI.cmd_render(args)
            rc = result if result is not None else 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        assert rc != 0, "Expected non-zero exit for unsupported --format"


# ---------------------------------------------------------------------------
# Test 3b — Leading-connective warning (Fix 2)
# ---------------------------------------------------------------------------

class TestLeadingConnectiveWarning:
    """A connective block appearing before any sourced block is dropped with a WARNING."""

    def test_leading_connective_warns_and_drops(self, fixture_pkg, tmp_path, capsys):
        """Leading connective block is dropped and a WARNING is emitted to stderr."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        # Prose starting with a connective block BEFORE the first sourced block.
        prose_leading_connective = [
            {"type": "connective", "text": "This connective has no section yet."},
            {"type": "sourced", "node_id": "ob_0001", "text": "Alpha abstract prose."},
            {"type": "connective", "text": "Transitional sentence."},
            {"type": "sourced", "node_id": "ob_0002", "text": "Beta sourced prose."},
            {"type": "connective", "text": "Another connector."},
            {"type": "sourced", "node_id": "ob_0003", "text": "No-origin sourced prose."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_leading_connective)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0, "render must succeed (leading connective is dropped, not fatal)"

        captured = capsys.readouterr()
        assert "WARNING" in captured.err, \
            "Expected a WARNING on stderr for leading-connective block"
        assert "connective block before any sourced block" in captured.err, \
            "WARNING message must mention 'connective block before any sourced block'"
        assert "This connective has no section yet" in captured.err, \
            "WARNING must include a snippet of the dropped block's text"

        # The dropped block must NOT appear in the rendered output.
        content = (tmp_path / "output.md").read_text(encoding="utf-8")
        assert "This connective has no section yet." not in content, \
            "The leading connective block must not appear in the rendered markdown"


# ---------------------------------------------------------------------------
# Test 4 — No-LLM: command is purely offline / deterministic
# ---------------------------------------------------------------------------

class TestNoLLM:
    """The render command makes no network calls — all assembly is deterministic."""

    def test_runs_without_network(self, fixture_pkg, tmp_path, monkeypatch):
        """Block all socket connections; render must still succeed."""
        import socket

        _real_connect = socket.socket.connect

        def _no_connect(self, *args, **kwargs):
            raise ConnectionRefusedError("Network blocked in test — render must be offline")

        monkeypatch.setattr(socket.socket, "connect", _no_connect)

        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0, "render must succeed without network access"
        assert os.path.exists(out_p), "output.md should be written"
        assert os.path.exists(fid_p), "fidelity.json should be written"

    def test_deterministic_output(self, fixture_pkg, tmp_path):
        """Running render twice on the same inputs produces identical outputs."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())

        out1 = str(tmp_path / "output1.md")
        fid1 = str(tmp_path / "fidelity1.json")
        out2 = str(tmp_path / "output2.md")
        fid2 = str(tmp_path / "fidelity2.json")

        _run_render(fixture_pkg, manifest_p, prose_p, out1, fid1)
        _run_render(fixture_pkg, manifest_p, prose_p, out2, fid2)

        content1 = (tmp_path / "output1.md").read_text(encoding="utf-8")
        content2 = (tmp_path / "output2.md").read_text(encoding="utf-8")
        assert content1 == content2, "Markdown output must be deterministic"

        fid_content1 = (tmp_path / "fidelity1.json").read_text(encoding="utf-8")
        fid_content2 = (tmp_path / "fidelity2.json").read_text(encoding="utf-8")
        assert fid_content1 == fid_content2, "Fidelity JSON must be deterministic"


# ---------------------------------------------------------------------------
# Round-3 calibration fixes — one class per item
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Item 1 — Strip inline intra-claim node-cross-refs from human prose
# ---------------------------------------------------------------------------

class TestStripInlineNodeRefs:
    """Inline node-id parentheticals in prose are stripped before rendering;
    the renderer's own trailing (node_id) provenance cite is preserved."""

    def test_inline_node_ref_stripped_from_sourced_block(self, fixture_pkg, tmp_path):
        """Sourced block prose containing '(dv_0316 Feature 1)' renders without it,
        but the canonical trailing '(ob_0001)' provenance cite is still present."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        prose_with_inline_refs = [
            {
                "type": "sourced",
                "node_id": "ob_0001",
                "text": "This claim (dv_0316 Feature 1) extends prior work (ob_0357).",
            },
            {"type": "connective", "text": "Transitional sentence."},
            {"type": "sourced", "node_id": "ob_0002", "text": "Beta sourced prose."},
            {"type": "connective", "text": "Another connector."},
            {"type": "sourced", "node_id": "ob_0003", "text": "No-origin sourced prose."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_with_inline_refs)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0, "render must succeed"

        content = (tmp_path / "output.md").read_text(encoding="utf-8")

        # Inline node-id refs must be stripped from the prose.
        assert "(dv_0316 Feature 1)" not in content, \
            "Inline '(dv_0316 Feature 1)' ref should be stripped from rendered prose"
        assert "(ob_0357)" not in content, \
            "Inline '(ob_0357)' intra-claim ref should be stripped from rendered prose"

        # The renderer's own trailing provenance cite must remain.
        assert "(ob_0001)" in content, \
            "Canonical trailing provenance cite '(ob_0001)' must be preserved"

    def test_inline_node_ref_with_trailing_words_stripped(self, fixture_pkg, tmp_path):
        """'(dv_0316 Feature 1)' (with trailing words inside parens) is fully stripped."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        prose_with_trailing_words = [
            {
                "type": "sourced",
                "node_id": "ob_0001",
                "text": "See (dv_0316 Feature 1) and also (ob_0356) for background.",
            },
            {"type": "connective", "text": "Connector."},
            {"type": "sourced", "node_id": "ob_0002", "text": "Beta sourced prose."},
            {"type": "connective", "text": "Another connector."},
            {"type": "sourced", "node_id": "ob_0003", "text": "No-origin sourced prose."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_with_trailing_words)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0

        content = (tmp_path / "output.md").read_text(encoding="utf-8")
        assert "(dv_0316 Feature 1)" not in content, \
            "'(dv_0316 Feature 1)' with trailing words must be fully stripped"
        assert "(ob_0001)" in content, \
            "Canonical trailing cite '(ob_0001)' must be preserved after strip"

    def test_inline_ref_stripped_from_connective_block(self, fixture_pkg, tmp_path):
        """Inline node-id refs in connective blocks are also stripped."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        prose_connective_with_ref = [
            {"type": "sourced", "node_id": "ob_0001", "text": "Alpha prose."},
            {
                "type": "connective",
                "text": "As shown in (dv_0316 Section 2), the transition follows.",
            },
            {"type": "sourced", "node_id": "ob_0002", "text": "Beta sourced prose."},
            {"type": "connective", "text": "Another connector."},
            {"type": "sourced", "node_id": "ob_0003", "text": "No-origin sourced prose."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_connective_with_ref)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0

        content = (tmp_path / "output.md").read_text(encoding="utf-8")
        assert "(dv_0316 Section 2)" not in content, \
            "Inline node ref in connective block must be stripped"


# ---------------------------------------------------------------------------
# Item 2 — Fix output file permissions (mode 644)
# ---------------------------------------------------------------------------

class TestOutputFilePermissions:
    """Output files must be mode 644 (group/other readable), not 600."""

    def test_output_files_are_mode_644(self, fixture_pkg, tmp_path):
        """After render, both output.md and fidelity.json have permissions 0o644."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0

        md_mode = oct(os.stat(out_p).st_mode & 0o777)
        fid_mode = oct(os.stat(fid_p).st_mode & 0o777)

        assert md_mode == oct(0o644), \
            f"output.md should be mode 644, got {md_mode}"
        assert fid_mode == oct(0o644), \
            f"fidelity.json should be mode 644, got {fid_mode}"


# ---------------------------------------------------------------------------
# Item 3 — Add 'section' to fidelity per_author_sourced entries
# ---------------------------------------------------------------------------

class TestFidelitySectionField:
    """Each per_author_sourced entry carries a 'section' field with the manifest section title."""

    def test_section_field_in_fidelity_entries(self, fixture_pkg, tmp_path):
        """Fidelity entries carry the correct section title from the manifest."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))
        per_author = fid["per_author_sourced"]

        # Gather all entries
        all_entries = []
        for entries in per_author.values():
            all_entries.extend(entries)

        # ob_0001 is the abstract_node_id → section should be "Abstract"
        ob_0001_entry = next((e for e in all_entries if e["node_id"] == "ob_0001"), None)
        assert ob_0001_entry is not None, "ob_0001 entry not found in fidelity"
        assert ob_0001_entry.get("section") == "Abstract", \
            f"ob_0001 (abstract) should have section='Abstract', got {ob_0001_entry.get('section')!r}"

        # ob_0002 and ob_0003 are in the 'Findings' section
        ob_0002_entry = next((e for e in all_entries if e["node_id"] == "ob_0002"), None)
        assert ob_0002_entry is not None, "ob_0002 entry not found in fidelity"
        assert ob_0002_entry.get("section") == "Findings", \
            f"ob_0002 should have section='Findings', got {ob_0002_entry.get('section')!r}"

        ob_0003_entry = next((e for e in all_entries if e["node_id"] == "ob_0003"), None)
        assert ob_0003_entry is not None, "ob_0003 entry not found in fidelity"
        assert ob_0003_entry.get("section") == "Findings", \
            f"ob_0003 should have section='Findings', got {ob_0003_entry.get('section')!r}"


# ---------------------------------------------------------------------------
# Item 4 — Record dropped connectives in the fidelity sheet
# ---------------------------------------------------------------------------

class TestDroppedConnectiveInFidelity:
    """A leading connective block (before any sourced block) is recorded in
    fidelity JSON under 'dropped_connective', not silently discarded."""

    def test_dropped_connective_recorded_in_fidelity(self, fixture_pkg, tmp_path):
        """Prose with a leading connective produces a 'dropped_connective' entry
        in the fidelity JSON with {text, reason}."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))

        prose_leading_connective = [
            {"type": "connective", "text": "This connective has no section yet."},
            {"type": "sourced", "node_id": "ob_0001", "text": "Alpha abstract prose."},
            {"type": "connective", "text": "Transitional sentence."},
            {"type": "sourced", "node_id": "ob_0002", "text": "Beta sourced prose."},
            {"type": "connective", "text": "Another connector."},
            {"type": "sourced", "node_id": "ob_0003", "text": "No-origin sourced prose."},
        ]
        prose_p = _write_prose(str(tmp_path), prose_leading_connective)
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0, "render must succeed even with a dropped leading connective"

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))

        assert "dropped_connective" in fid, \
            "'dropped_connective' key must be present in fidelity JSON"

        dropped = fid["dropped_connective"]
        assert isinstance(dropped, list), \
            "'dropped_connective' must be a list"
        assert len(dropped) == 1, \
            f"Expected 1 dropped connective, got {len(dropped)}"

        entry = dropped[0]
        assert entry.get("text") == "This connective has no section yet.", \
            f"dropped_connective text mismatch: {entry.get('text')!r}"
        assert "reason" in entry, \
            "dropped_connective entry must include a 'reason' field"
        assert "no section" in entry["reason"].lower() or \
               "before first sourced block" in entry["reason"].lower(), \
            f"reason should describe the drop cause, got: {entry['reason']!r}"

    def test_no_dropped_connectives_when_none_leading(self, fixture_pkg, tmp_path):
        """With no leading connective, 'dropped_connective' is an empty list."""
        manifest_p = _write_manifest(str(tmp_path), _default_manifest(fixture_pkg))
        prose_p = _write_prose(str(tmp_path), _default_prose())
        out_p = str(tmp_path / "output.md")
        fid_p = str(tmp_path / "fidelity.json")

        rc = _run_render(fixture_pkg, manifest_p, prose_p, out_p, fid_p)
        assert rc == 0

        fid = json.loads((tmp_path / "fidelity.json").read_text(encoding="utf-8"))
        assert "dropped_connective" in fid, \
            "'dropped_connective' key must always be present in fidelity JSON"
        assert fid["dropped_connective"] == [], \
            f"With no leading connectives, dropped_connective must be [], got {fid['dropped_connective']}"
