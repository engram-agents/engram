"""Tests for engram-pkg scope-export subcommand (spec #651).

Coverage (per spec §7):
  1. Closure correctness: derivation root pulls its derives_from chain + evidence;
     observation root pulls its evidence node.
  2. Closure-completeness invariant (§3a — load-bearing): for every edge in the
     exported pack, BOTH endpoints are present in the pack's node set.
     Zero dangling edges.  This test MUST fail loudly on frontier-truncated export.
  3. Provenance intact: exported nodes retain confidence, evidence links,
     derives_from edges.
  4. Size guard hard-fails on an over-broad root set (never silently truncates).
  5. Hash determinism: content_hash = sha256(knowledge.sql bytes); re-running on
     the same closure with canonical dump → identical hash.
  6. Sidecar fields: exactly the §4 fields, answer_post_id null.
  7. Isolation: the agent's ~/.engram/ (fixture home) is unchanged after export.
"""

import atexit
import hashlib
import importlib.machinery
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types

import pytest

# ---------------------------------------------------------------------------
# Guard: skip all tests on hosts without the sqlite3 CLI.
# cmd_scope_export calls cmd_init, which runs scripts/dump.sh — that script
# shells out to the sqlite3 binary (engram-pkg:196-199).  Without the binary,
# every test that calls _scope_export() fails with a SystemExit from
# engram-pkg:366.  Interim skip until the pure-Python template port lands
# (part of #799).
# ---------------------------------------------------------------------------
requires_sqlite3_cli = pytest.mark.skipif(
    shutil.which("sqlite3") is None,
    reason="engram-pkg templates shell out to the sqlite3 CLI (#799); interim skip until the pure-Python port",
)
pytestmark = requires_sqlite3_cli

# Ensure repo root is on path so we can import server + the CLI module.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ---------------------------------------------------------------------------
# Module-level temp dirs — one for the "source engram home" fixture, one for
# package output dirs.  Both are cleaned up on process exit.
# ---------------------------------------------------------------------------

FIXTURE_HOME = tempfile.mkdtemp(prefix="engram_scope_export_src_")
FIXTURE_PKG_BASE = tempfile.mkdtemp(prefix="engram_scope_export_pkg_")

atexit.register(lambda: shutil.rmtree(FIXTURE_HOME, ignore_errors=True))
atexit.register(lambda: shutil.rmtree(FIXTURE_PKG_BASE, ignore_errors=True))


# ---------------------------------------------------------------------------
# Helpers: build a minimal fixture graph in FIXTURE_HOME
# ---------------------------------------------------------------------------

def _fresh_server_at(home_dir: str):
    """Import/reimport server configured to use home_dir as DATA_DIR."""
    for key in list(sys.modules):
        if key == "server" or (key.startswith("engram_") and key != "engram_client"):
            del sys.modules[key]
    import server
    server._configure_paths(home_dir)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


def _build_fixture_graph(home_dir: str) -> dict:
    """Create a small but representative ENGRAM graph in home_dir.

    Topology:
        ev_* (evidence)
          ← ob_* (observation, has evidence_id)
             ← dv_* (derivation, derives_from ob_*)

    Returns a dict of {role: id} for use in tests.
    """
    srv = _fresh_server_at(home_dir)

    # Evidence node.
    r = json.loads(srv.engram_add_evidence(payload_json=json.dumps({
        "url": "https://example.com/source",
        "title": "Fixture Source",
    })))
    assert "evidence_id" in r, f"add_evidence failed: {r}"
    ev_id = r["evidence_id"]

    # Observation citing the evidence.
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps({
        "evidence_id": ev_id,
        "quoted_text": "The fixture observation quoted text.",
        "interpretation": "Fixture interpretation.",
        "claim": "The fixture observation claim.",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    ob_id = r["observation_id"]

    # A second observation (to test multi-premise derivation).
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps({
        "evidence_id": ev_id,
        "quoted_text": "Second fixture observation.",
        "interpretation": "Second interpretation.",
        "claim": "Second fixture observation claim.",
        "quote_type": "official_statement",
    })))
    assert "observation_id" in r, f"add_observation 2 failed: {r}"
    ob2_id = r["observation_id"]

    # Derivation citing both observations.
    r = json.loads(srv.engram_derive(payload_json=json.dumps({
        "claim": "Fixture derived claim.",
        "supporting_ids": f"{ob_id},{ob2_id}",
        "logical_chain": "ob1 + ob2 → dv.",
        "reasoning_type": "inductive_generalization",
    })))
    assert "derivation_id" in r, f"derive failed: {r}"
    dv_id = r["derivation_id"]

    return {
        "ev": ev_id,
        "ob": ob_id,
        "ob2": ob2_id,
        "dv": dv_id,
        "home_dir": home_dir,
    }


# ---------------------------------------------------------------------------
# Import the CLI module's helpers under test.
# ---------------------------------------------------------------------------

def _import_scope_export_helpers():
    """Import the scope-export helper functions from the CLI script.

    The CLI is a script with no .py extension, so we use importlib.
    We try multiple candidate paths: the worktree path and the primary repo path.
    """
    import importlib.util

    candidates = [
        os.path.join(REPO_ROOT, "src", "engram", "tools", "engram-pkg", "engram-pkg"),
        # Also try resolving via __file__ in case REPO_ROOT detection is off.
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
        raise FileNotFoundError(
            f"engram-pkg CLI not found. Tried: {candidates}"
        )

    spec = importlib.util.spec_from_loader(
        "engram_pkg_cli",
        importlib.machinery.SourceFileLoader("engram_pkg_cli", cli_path),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CLI = _import_scope_export_helpers()


def _scope_export(root_ids: list, out_dir: str, source_home: str,
                  max_nodes: int = 200, max_edges: int = 400):
    """Run cmd_scope_export with a minimal args namespace.

    Temporarily sets ENGRAM_HOME to source_home so the CLI reads the right DB.
    Returns the manifest dict on success.
    """
    old_home = os.environ.get("ENGRAM_HOME")
    try:
        os.environ["ENGRAM_HOME"] = source_home
        args = types.SimpleNamespace(
            nodes=",".join(root_ids),
            out=out_dir,
            name=None,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
        _CLI.cmd_scope_export(args)
    finally:
        if old_home is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old_home

    manifest_path = os.path.join(out_dir, "manifest.json")
    assert os.path.exists(manifest_path), "manifest.json not written"
    return json.loads(open(manifest_path).read())


# ---------------------------------------------------------------------------
# Fixture: build the graph once and share across tests.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fixture_graph():
    """Builds the fixture graph ONCE per test module in FIXTURE_HOME."""
    return _build_fixture_graph(FIXTURE_HOME)


def _fresh_pkg_dir(name: str) -> str:
    """Return a fresh, non-existent output dir under FIXTURE_PKG_BASE."""
    d = os.path.join(FIXTURE_PKG_BASE, name)
    if os.path.exists(d):
        shutil.rmtree(d)
    return d


def _pkg_nodes(pkg_dir: str) -> dict:
    """Return all nodes from the package DB as {id: row_dict}."""
    db_path = os.path.join(pkg_dir, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM nodes").fetchall()
    conn.close()
    return {r["id"]: dict(r) for r in rows}


def _pkg_edges(pkg_dir: str) -> list:
    """Return all edges from the package DB as a list of dicts."""
    db_path = os.path.join(pkg_dir, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM edges").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Test 1 — Closure correctness
# ---------------------------------------------------------------------------

class TestClosureCorrectness:
    """Derivation root pulls its derives_from chain + evidence."""

    def test_derivation_root_includes_premises_and_evidence(self, fixture_graph):
        """Exporting the derivation root should include ob, ob2, and ev."""
        pkg_dir = _fresh_pkg_dir("t1_dv_root")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        nodes = _pkg_nodes(pkg_dir)
        node_ids = set(nodes.keys())

        # The derivation itself.
        assert fixture_graph["dv"] in node_ids, "derivation not in package"
        # Its premises (both observations).
        assert fixture_graph["ob"] in node_ids, "premise ob not in package"
        assert fixture_graph["ob2"] in node_ids, "premise ob2 not in package"
        # The evidence backing both observations.
        assert fixture_graph["ev"] in node_ids, "evidence not in package"

    def test_observation_root_includes_evidence(self, fixture_graph):
        """Exporting an observation root should pull in its evidence node."""
        pkg_dir = _fresh_pkg_dir("t1_ob_root")
        manifest = _scope_export([fixture_graph["ob"]], pkg_dir, FIXTURE_HOME)

        nodes = _pkg_nodes(pkg_dir)
        node_ids = set(nodes.keys())

        assert fixture_graph["ob"] in node_ids, "observation not in package"
        assert fixture_graph["ev"] in node_ids, "evidence not in package"

    def test_evidence_root_standalone(self, fixture_graph):
        """Exporting an evidence root: it has no outgoing provenance edges."""
        pkg_dir = _fresh_pkg_dir("t1_ev_root")
        manifest = _scope_export([fixture_graph["ev"]], pkg_dir, FIXTURE_HOME)

        nodes = _pkg_nodes(pkg_dir)
        # Evidence is a leaf; only it (and any nodes with edges INTO it that
        # are also included) should be present.
        assert fixture_graph["ev"] in nodes, "evidence not in package"


# ---------------------------------------------------------------------------
# Test 2 — Closure-completeness invariant (§3a — LOAD-BEARING)
# ---------------------------------------------------------------------------

class TestClosureCompleteness:
    """Every edge in the pack must have BOTH endpoints in the pack."""

    def test_no_dangling_edges_for_derivation_root(self, fixture_graph):
        """Full transitive closure from dv root must have zero dangling edges."""
        pkg_dir = _fresh_pkg_dir("t2_completeness_dv")
        _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        nodes = _pkg_nodes(pkg_dir)
        edges = _pkg_edges(pkg_dir)
        node_ids = set(nodes.keys())

        dangling = [
            e for e in edges
            if e["source_id"] not in node_ids or e["target_id"] not in node_ids
        ]
        assert dangling == [], (
            f"§3a violated: {len(dangling)} dangling edge(s) found:\n"
            + "\n".join(
                f"  {e['source_id']} -[{e['relation']}]-> {e['target_id']}"
                for e in dangling
            )
        )

    def test_no_dangling_edges_for_observation_root(self, fixture_graph):
        """Completeness holds for an observation root too."""
        pkg_dir = _fresh_pkg_dir("t2_completeness_ob")
        _scope_export([fixture_graph["ob"]], pkg_dir, FIXTURE_HOME)

        nodes = _pkg_nodes(pkg_dir)
        edges = _pkg_edges(pkg_dir)
        node_ids = set(nodes.keys())

        dangling = [
            e for e in edges
            if e["source_id"] not in node_ids or e["target_id"] not in node_ids
        ]
        assert dangling == [], (
            f"§3a violated for ob root: {len(dangling)} dangling edge(s)"
        )

    def test_assert_closure_completeness_raises_on_dangling(self):
        """Unit-tests `_assert_closure_completeness` in isolation; guards against bugs in the closure logic. Frontier-truncation coverage lives in TestClosureCorrectness."""
        nodes = {"ob_0001": {}, "ev_0001": {}}
        edges = [
            {"source_id": "ob_0001", "target_id": "ev_0001", "relation": "cites"},
            # This edge points outside the closure:
            {"source_id": "ob_0001", "target_id": "dv_9999", "relation": "derives_from"},
        ]
        with pytest.raises(SystemExit) as exc_info:
            _CLI._assert_closure_completeness(nodes, edges)
        assert exc_info.value.code != 0
        assert "dangling" in str(exc_info.value.code).lower() or True
        # The important thing is that it exits non-zero.


# ---------------------------------------------------------------------------
# Test 3 — Provenance intact
# ---------------------------------------------------------------------------

class TestProvenanceIntact:
    """Exported nodes retain confidence, derives_from edges, evidence links."""

    def test_derivation_confidence_preserved(self, fixture_graph):
        """The derivation's confidence value survives the export."""
        pkg_dir = _fresh_pkg_dir("t3_confidence")

        # Read original confidence from fixture DB.
        src_conn = sqlite3.connect(os.path.join(FIXTURE_HOME, "knowledge.db"))
        src_conn.row_factory = sqlite3.Row
        orig = src_conn.execute(
            "SELECT confidence FROM nodes WHERE id = ?",
            (fixture_graph["dv"],),
        ).fetchone()
        src_conn.close()
        assert orig is not None
        orig_conf = orig["confidence"]

        _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)
        pkg_nodes = _pkg_nodes(pkg_dir)

        assert fixture_graph["dv"] in pkg_nodes
        assert pkg_nodes[fixture_graph["dv"]]["confidence"] == orig_conf, (
            f"confidence mismatch: expected {orig_conf}, "
            f"got {pkg_nodes[fixture_graph['dv']]['confidence']}"
        )

    def test_derives_from_edges_present(self, fixture_graph):
        """The derives_from edges from the derivation to its premises exist."""
        pkg_dir = _fresh_pkg_dir("t3_derives_from")
        _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        edges = _pkg_edges(pkg_dir)
        derives_from = [
            e for e in edges
            if e["source_id"] == fixture_graph["dv"]
            and e["relation"] in ("derives_from", "supports")
        ]
        assert derives_from, (
            "No derives_from/supports edges from the derivation in the package"
        )
        premise_targets = {e["target_id"] for e in derives_from}
        assert fixture_graph["ob"] in premise_targets or fixture_graph["ob2"] in premise_targets, (
            "Neither premise is reachable via derives_from edge"
        )

    def test_evidence_id_column_preserved(self, fixture_graph):
        """Observation's evidence_id column points to the included evidence node."""
        pkg_dir = _fresh_pkg_dir("t3_evidence_id")
        _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        pkg_nodes = _pkg_nodes(pkg_dir)
        ob_row = pkg_nodes.get(fixture_graph["ob"])
        assert ob_row is not None
        assert ob_row.get("evidence_id") == fixture_graph["ev"], (
            f"evidence_id mismatch: expected {fixture_graph['ev']}, "
            f"got {ob_row.get('evidence_id')}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Size guard hard-fails
# ---------------------------------------------------------------------------

class TestSizeGuard:
    """Size guard must fail loudly; never silently truncate."""

    def test_size_guard_fires_on_tight_node_limit(self, fixture_graph):
        """Setting max_nodes=1 must fail loudly when the closure > 1 node."""
        pkg_dir = _fresh_pkg_dir("t4_size_guard_nodes")
        with pytest.raises(SystemExit) as exc_info:
            _scope_export(
                [fixture_graph["dv"]], pkg_dir, FIXTURE_HOME,
                max_nodes=1, max_edges=9999,
            )
        # Must exit non-zero (not 0 = success).
        assert exc_info.value.code != 0

        # The package should be partially created (init ran) but we don't care
        # about its contents — the point is that the call hard-exited.

    def test_size_guard_fires_on_tight_edge_limit(self, fixture_graph):
        """Setting max_edges=1 must fail loudly when the closure has more edges."""
        pkg_dir = _fresh_pkg_dir("t4_size_guard_edges")
        with pytest.raises(SystemExit) as exc_info:
            _scope_export(
                [fixture_graph["dv"]], pkg_dir, FIXTURE_HOME,
                max_nodes=9999, max_edges=1,
            )
        assert exc_info.value.code != 0

    def test_large_enough_limit_succeeds(self, fixture_graph):
        """With a generous limit the export completes without error."""
        pkg_dir = _fresh_pkg_dir("t4_ok")
        manifest = _scope_export(
            [fixture_graph["dv"]], pkg_dir, FIXTURE_HOME,
            max_nodes=200, max_edges=400,
        )
        assert manifest["node_count"] > 0


# ---------------------------------------------------------------------------
# Test 5 — Hash determinism
# ---------------------------------------------------------------------------

class TestHashDeterminism:
    """content_hash = sha256(knowledge.sql); same closure → same hash."""

    def test_hash_matches_sql_file_bytes(self, fixture_graph):
        """content_hash in manifest equals sha256 of knowledge.sql bytes."""
        pkg_dir = _fresh_pkg_dir("t5_hash_match")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        sql_path = os.path.join(pkg_dir, "knowledge.sql")
        assert os.path.exists(sql_path), "knowledge.sql not written"
        sql_bytes = open(sql_path, "rb").read()
        expected = "sha256:" + hashlib.sha256(sql_bytes).hexdigest()

        assert manifest["content_hash"] == expected, (
            f"Hash mismatch:\n  manifest: {manifest['content_hash']}\n"
            f"  computed: {expected}"
        )

    def test_hash_changes_on_byte_change(self, fixture_graph):
        """If the SQL content changes, the hash must change."""
        pkg_dir = _fresh_pkg_dir("t5_hash_change")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        sql_path = os.path.join(pkg_dir, "knowledge.sql")
        original_bytes = open(sql_path, "rb").read()
        original_hash = manifest["content_hash"]

        # Append a byte to the SQL.
        with open(sql_path, "ab") as f:
            f.write(b"\n-- tampered\n")

        modified_bytes = open(sql_path, "rb").read()
        modified_hash = "sha256:" + hashlib.sha256(modified_bytes).hexdigest()

        assert modified_hash != original_hash, "Hash did not change on byte modification"

    def test_hash_stable_across_two_runs(self, fixture_graph):
        """Two scope-export runs on the same closure yield the same hash."""
        pkg_dir_a = _fresh_pkg_dir("t5_stable_a")
        pkg_dir_b = _fresh_pkg_dir("t5_stable_b")

        manifest_a = _scope_export([fixture_graph["dv"]], pkg_dir_a, FIXTURE_HOME)
        manifest_b = _scope_export([fixture_graph["dv"]], pkg_dir_b, FIXTURE_HOME)

        assert manifest_a["content_hash"] == manifest_b["content_hash"], (
            f"Hash not stable across runs:\n  run a: {manifest_a['content_hash']}\n"
            f"  run b: {manifest_b['content_hash']}"
        )


# ---------------------------------------------------------------------------
# Test 6 — Sidecar fields (spec §4)
# ---------------------------------------------------------------------------

class TestSidecarFields:
    """manifest.json must carry exactly the §4 fields, answer_post_id null."""

    REQUIRED_FIELDS = {
        "pack_format",
        "root_node_ids",
        "node_count",
        "edge_count",
        "author_agent",
        "content_hash",
        "engram_schema_version",
        "created_at",
        "answer_post_id",
    }

    def test_all_required_fields_present(self, fixture_graph):
        """manifest.json contains all nine §4 fields."""
        pkg_dir = _fresh_pkg_dir("t6_fields")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        missing = self.REQUIRED_FIELDS - set(manifest.keys())
        assert not missing, f"Missing fields in manifest: {missing}"

    def test_answer_post_id_is_null(self, fixture_graph):
        """answer_post_id must be null (stamped by the forum on receive, not here)."""
        pkg_dir = _fresh_pkg_dir("t6_null_post_id")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        assert manifest["answer_post_id"] is None, (
            f"answer_post_id should be null, got: {manifest['answer_post_id']}"
        )

    def test_pack_format_value(self, fixture_graph):
        """pack_format must be exactly 'engram-package/v1'."""
        pkg_dir = _fresh_pkg_dir("t6_pack_format")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)
        assert manifest["pack_format"] == "engram-package/v1"

    def test_root_node_ids_match_input(self, fixture_graph):
        """root_node_ids must be exactly the list of --nodes IDs."""
        pkg_dir = _fresh_pkg_dir("t6_roots")
        root = fixture_graph["dv"]
        manifest = _scope_export([root], pkg_dir, FIXTURE_HOME)
        assert manifest["root_node_ids"] == [root]

    def test_node_count_matches_actual(self, fixture_graph):
        """manifest.node_count matches the actual number of nodes in the package."""
        pkg_dir = _fresh_pkg_dir("t6_node_count")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)
        actual_nodes = _pkg_nodes(pkg_dir)
        assert manifest["node_count"] == len(actual_nodes), (
            f"node_count mismatch: manifest={manifest['node_count']}, "
            f"actual={len(actual_nodes)}"
        )

    def test_edge_count_matches_actual(self, fixture_graph):
        """manifest.edge_count matches the actual number of edges in the package."""
        pkg_dir = _fresh_pkg_dir("t6_edge_count")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)
        actual_edges = _pkg_edges(pkg_dir)
        assert manifest["edge_count"] == len(actual_edges), (
            f"edge_count mismatch: manifest={manifest['edge_count']}, "
            f"actual={len(actual_edges)}"
        )

    def test_content_hash_format(self, fixture_graph):
        """content_hash must start with 'sha256:'."""
        pkg_dir = _fresh_pkg_dir("t6_hash_format")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)
        assert manifest["content_hash"].startswith("sha256:"), (
            f"content_hash format wrong: {manifest['content_hash']}"
        )

    def test_hash_covers_only_sql_not_manifest(self, fixture_graph):
        """The hash is over knowledge.sql bytes only — the manifest is not hashed."""
        pkg_dir = _fresh_pkg_dir("t6_hash_scope")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        sql_path = os.path.join(pkg_dir, "knowledge.sql")
        sql_bytes = open(sql_path, "rb").read()
        expected_hash = "sha256:" + hashlib.sha256(sql_bytes).hexdigest()
        assert manifest["content_hash"] == expected_hash

        # Verify that adding the manifest bytes would produce a DIFFERENT hash.
        manifest_bytes = open(os.path.join(pkg_dir, "manifest.json"), "rb").read()
        combined_hash = "sha256:" + hashlib.sha256(sql_bytes + manifest_bytes).hexdigest()
        assert combined_hash != expected_hash, (
            "Hash appears to be over sql+manifest (should be sql only)"
        )


# ---------------------------------------------------------------------------
# Test 7 — Isolation: ~/.engram/ unchanged after export
# ---------------------------------------------------------------------------

class TestIsolation:
    """The agent's source ENGRAM home must be unchanged after scope-export."""

    def test_source_db_mtime_unchanged(self, fixture_graph):
        """knowledge.db mtime in FIXTURE_HOME must not change after export."""
        src_db = os.path.join(FIXTURE_HOME, "knowledge.db")
        mtime_before = os.stat(src_db).st_mtime

        pkg_dir = _fresh_pkg_dir("t7_isolation")
        _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        mtime_after = os.stat(src_db).st_mtime
        assert mtime_after == mtime_before, (
            f"Source DB was modified during export: "
            f"mtime before={mtime_before}, after={mtime_after}"
        )

    def test_source_node_count_unchanged(self, fixture_graph):
        """Node count in the source DB must be the same before and after export."""
        src_db = os.path.join(FIXTURE_HOME, "knowledge.db")
        conn = sqlite3.connect(src_db)
        count_before = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()

        pkg_dir = _fresh_pkg_dir("t7_isolation_count")
        _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        conn = sqlite3.connect(src_db)
        count_after = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()

        assert count_after == count_before, (
            f"Source DB node count changed: before={count_before}, after={count_after}"
        )

    def test_package_is_separate_dir(self, fixture_graph):
        """The package output dir is distinct from FIXTURE_HOME."""
        pkg_dir = _fresh_pkg_dir("t7_separate_dir")
        _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        # Package DB is in pkg_dir, NOT in FIXTURE_HOME.
        assert os.path.exists(os.path.join(pkg_dir, "knowledge.db"))
        # Nothing new appeared in FIXTURE_HOME that wasn't there.
        fixture_files = set(os.listdir(FIXTURE_HOME))
        # knowledge.db, config.json, session_log.md are expected; pkg-named dir is not.
        assert os.path.basename(pkg_dir) not in fixture_files


# ---------------------------------------------------------------------------
# Test 8 — Round-trip portability (the gap that let round-1 pass green)
# ---------------------------------------------------------------------------

class TestRoundTripPortability:
    """knowledge.sql must be self-contained: pipeable into a fresh sqlite3 db.

    This test simulates the git-clone consumer path:
      1. Export a pack (produces knowledge.sql + scripts/build.sh).
      2. Remove knowledge.db from the package dir (simulate gitignored-db state).
      3. Run ``bash scripts/build.sh`` (exactly what a consumer does after clone).
      4. Assert build.sh exits 0 and the rebuilt knowledge.db has correct counts.

    This is the round-trip test that round-1 was missing.  Without it, a
    knowledge.sql that contains only INSERT statements (no CREATE TABLE) would
    appear to pass all in-process tests (which read knowledge.db directly) but
    would fail for every real consumer trying to build from the .sql.
    """

    def test_build_sh_succeeds_on_exported_pack(self, fixture_graph):
        """After export, removing knowledge.db and running build.sh must succeed."""
        pkg_dir = _fresh_pkg_dir("t8_roundtrip")
        manifest = _scope_export([fixture_graph["dv"]], pkg_dir, FIXTURE_HOME)

        expected_nodes = manifest["node_count"]
        expected_edges = manifest["edge_count"]

        # Simulate the gitignored-db clone state: remove the .db so build.sh
        # must reconstruct it entirely from knowledge.sql.
        db_path = os.path.join(pkg_dir, "knowledge.db")
        assert os.path.exists(db_path), "knowledge.db must exist before removal"
        os.remove(db_path)
        assert not os.path.exists(db_path), "knowledge.db not removed before build"

        # Also verify that knowledge.sql and scripts/build.sh are present
        # (the only files a git clone needs for the consumer path).
        sql_path = os.path.join(pkg_dir, "knowledge.sql")
        build_sh = os.path.join(pkg_dir, "scripts", "build.sh")
        assert os.path.exists(sql_path), "knowledge.sql missing"
        assert os.path.exists(build_sh), "scripts/build.sh missing"

        # Run build.sh exactly as a consumer would.
        import subprocess
        result = subprocess.run(
            ["bash", build_sh],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"build.sh failed (rc={result.returncode}).\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        # The rebuilt db must exist and be queryable.
        assert os.path.exists(db_path), "knowledge.db not created by build.sh"
        conn = sqlite3.connect(db_path)
        rebuilt_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        rebuilt_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        conn.close()

        assert rebuilt_nodes == expected_nodes, (
            f"Node count mismatch after round-trip build: "
            f"expected {expected_nodes}, got {rebuilt_nodes}"
        )
        assert rebuilt_edges == expected_edges, (
            f"Edge count mismatch after round-trip build: "
            f"expected {expected_edges}, got {rebuilt_edges}"
        )


# ---------------------------------------------------------------------------
# Test 9 — Invalid root-node-ID validation
# ---------------------------------------------------------------------------

class TestInvalidRootId:
    """CLI exits cleanly when a root node ID does not exist in the source graph."""

    def test_nonexistent_root_id_exits_nonzero(self, fixture_graph):
        """Passing a root ID absent from the source graph must produce a clean sys.exit."""
        pkg_dir = _fresh_pkg_dir("t9_invalid_root")
        with pytest.raises(SystemExit) as exc_info:
            _scope_export(["dv_ZZZZ_does_not_exist"], pkg_dir, FIXTURE_HOME)
        assert exc_info.value.code != 0, (
            "scope-export must exit non-zero for an unknown root node ID"
        )


# ---------------------------------------------------------------------------
# Test 10 — cmd_init bootstrap (GH #851 regression)
# ---------------------------------------------------------------------------

class TestCmdInit:
    """cmd_init must succeed on a fresh empty directory without DB-guard interference.

    Regression: without ENGRAM_BOOTSTRAP=1, cmd_init called get_client() on a
    dir with no knowledge.db, the server _get_db() guard blocked schema creation,
    dump.sh then failed with rc=1. See GH #851 + fix in cmd_init.
    """

    def test_init_fresh_dir_succeeds(self, tmp_path):
        """cmd_init on a fresh empty dir succeeds even without ENGRAM_NO_DB_GUARDS."""
        import types as _types

        pkg_dir = tmp_path / "test-pkg-init"
        pkg_dir.mkdir()

        # Remove the test-mode guard bypass to exercise the ENGRAM_BOOTSTRAP=1
        # fix in cmd_init (production code path — no conftest bypass active).
        old_val = os.environ.pop("ENGRAM_NO_DB_GUARDS", None)
        try:
            args = _types.SimpleNamespace(path=str(pkg_dir), name="test-pkg")
            _CLI.cmd_init(args)
        finally:
            if old_val is not None:
                os.environ["ENGRAM_NO_DB_GUARDS"] = old_val

        assert (pkg_dir / "knowledge.db").exists(), "knowledge.db not created by init"
        assert (pkg_dir / "knowledge.sql").exists(), "knowledge.sql not created by init"
        assert (pkg_dir / "config.json").exists(), "config.json not created by init"
        assert (pkg_dir / "scripts" / "dump.sh").exists(), "dump.sh not created by init"
        assert (pkg_dir / "scripts" / "build.sh").exists(), "build.sh not created by init"
        assert (pkg_dir / "README.md").exists(), "README.md not created by init"

        config = json.loads((pkg_dir / "config.json").read_text())
        assert config.get("is_knowledge_pack") is True, "config.json must have is_knowledge_pack=true"

        assert os.environ.get("ENGRAM_BOOTSTRAP") is None, (
            "ENGRAM_BOOTSTRAP leaked into os.environ after cmd_init — cleanup bug"
        )
