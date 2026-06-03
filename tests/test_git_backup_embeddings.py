"""Tests for the git-backup embedding-stripping fix (#717).

Coverage (four test areas):

  1. _commit_snapshot embedding-stripped dump
     - The SQL dump written to knowledge.sql must NOT contain the embedding column.
     - Rebuilding that dump into a fresh DB must yield identical node/edge/edit_history
       counts (lossless rebuild).

  2. _init_git / _ensure_engram_gitignore
     - .gitignore is written when absent.
     - .gitignore contains all required patterns.
     - .gitignore is left unchanged when all patterns are already present.

  3. engram-regenerate-embeddings.py backfill
     - A node with embedding IS NULL gets a non-NULL embedding after backfill.
     - A node with an existing embedding is left unchanged (idempotent).

  4. _commit_snapshot early-return paths carry dump fields (round-2 fix)
     - When the dump fails, ALL early-return paths (no tracked files, git add
       failed, git commit failed, no changes since last commit) carry
       sql_dump_error so the agent-visible response surfaces the failure.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers: minimal fixture DB
# ---------------------------------------------------------------------------

def _make_fixture_db(path: Path, num_nodes: int = 3, with_embeddings: bool = True) -> None:
    """Create a minimal ENGRAM DB for testing.

    Creates: nodes table, edges table, edit_history table, sequences table.
    Inserts num_nodes observation nodes.
    If with_embeddings=True, fills in a fake embedding JSON on half the nodes.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            claim TEXT,
            source_title TEXT,
            predicted_event TEXT,
            is_current INTEGER DEFAULT 1,
            embedding TEXT
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, target_id, relation)
        );
        CREATE TABLE IF NOT EXISTS edit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT,
            action TEXT,
            timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS sequences (
            node_type TEXT PRIMARY KEY,
            last_num INTEGER NOT NULL DEFAULT 0
        );
    """)
    fake_embedding = json.dumps([0.1] * 384)
    for i in range(num_nodes):
        node_id = f"ob_{i:04d}"
        emb = fake_embedding if (with_embeddings and i % 2 == 0) else None
        conn.execute(
            "INSERT INTO nodes (id, type, claim, is_current, embedding) VALUES (?, 'observation', ?, 1, ?)",
            (node_id, f"Claim {i}", emb),
        )
        conn.execute(
            "INSERT INTO edit_history (node_id, action, timestamp) VALUES (?, 'create', '2026-01-01T00:00:00')",
            (node_id,),
        )
    conn.commit()
    conn.close()


def _count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _get_embeddings(db_path: Path) -> dict:
    """Return {node_id: embedding_value} for all nodes."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT id, embedding FROM nodes").fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test Area 1: _commit_snapshot embedding-stripped dump
# ---------------------------------------------------------------------------

class TestCommitSnapshotEmbeddingStripped:
    """_commit_snapshot writes knowledge.sql with embeddings stripped."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_backup_"))
        # Clean server module state
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        os.environ["ENGRAM_DISABLE_GIT"] = "1"  # skip git for these tests
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        self.server = server
        self.db_path = self.tmp / "knowledge.db"

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENGRAM_DISABLE_GIT", None)

    def _make_db_with_embeddings(self):
        """Create the DB with embeddings in the live path."""
        _make_fixture_db(self.db_path, num_nodes=4, with_embeddings=True)

    def test_dump_excludes_embedding_column(self):
        """The SQL dump must not contain any embedding data."""
        self._make_db_with_embeddings()

        import engram_backup
        # Verify the live DB actually has embeddings before stripping
        embs = _get_embeddings(self.db_path)
        assert any(v is not None for v in embs.values()), \
            "Fixture should have at least one non-NULL embedding"

        # Run the embedding-stripped dump via engram_backup.dump_stripped
        # (the method _commit_snapshot now uses — pure-Python, no sqlite3 CLI)
        sql_path = self.tmp / "knowledge.sql"
        engram_backup.dump_stripped(str(self.db_path), str(sql_path))

        sql_content = sql_path.read_text(encoding="utf-8")

        # The dump must not contain the fake embedding vectors
        fake_embedding_fragment = "0.1, 0.1"  # part of json.dumps([0.1]*384)
        assert fake_embedding_fragment not in sql_content, \
            "SQL dump still contains embedding data — stripping failed"

    def test_stripped_dump_rebuilds_losslessly(self):
        """Rebuild from the stripped dump yields identical node/edge/edit_history counts."""
        self._make_db_with_embeddings()

        import engram_backup
        sql_path = self.tmp / "knowledge.sql"
        rebuild_path = self.tmp / "knowledge_rebuild.db"

        engram_backup.dump_stripped(str(self.db_path), str(sql_path))

        # Rebuild
        sql_content = sql_path.read_text(encoding="utf-8")
        rebuild_conn = sqlite3.connect(str(rebuild_path))
        rebuild_conn.executescript(sql_content)
        rebuild_conn.close()

        # Compare counts
        for table in ("nodes", "edges", "edit_history"):
            orig = _count_rows(self.db_path, table)
            rebuilt = _count_rows(rebuild_path, table)
            assert orig == rebuilt, \
                f"Lossless rebuild failed for table '{table}': original={orig}, rebuilt={rebuilt}"

    def test_live_db_embeddings_not_modified(self):
        """The live DB must be unchanged after the dump procedure."""
        self._make_db_with_embeddings()

        embs_before = _get_embeddings(self.db_path)

        import engram_backup
        sql_path = self.tmp / "knowledge.sql"
        engram_backup.dump_stripped(str(self.db_path), str(sql_path))

        embs_after = _get_embeddings(self.db_path)
        assert embs_before == embs_after, \
            "Live DB embeddings were modified during the dump procedure — only the snapshot should have changed"


# ---------------------------------------------------------------------------
# Test Area 2: _ensure_engram_gitignore
# ---------------------------------------------------------------------------

REQUIRED_GITIGNORE_PATTERNS = [
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.db-journal",
    "*?mode=*",
    "*.bak",
    "*.bak.*",
    "config.json.bak*",
    "*.backfill-snapshot-*",
    "*.log",
    "marketplace/",
    "venv/",
    "__pycache__/",
    "*.pyc",
]


class TestEnsureEngramGitignore:
    """_ensure_engram_gitignore writes the canonical .gitignore."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_gitignore_"))
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import server
        server._configure_paths(self.tmp)
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gitignore_written_when_absent(self):
        """_ensure_engram_gitignore writes .gitignore when it doesn't exist."""
        gitignore = self.tmp / ".gitignore"
        assert not gitignore.exists(), "Pre-condition: .gitignore should not exist"
        self.server._ensure_engram_gitignore(self.tmp)
        assert gitignore.exists(), ".gitignore was not created"

    def test_gitignore_contains_all_required_patterns(self):
        """The written .gitignore contains all canonical patterns."""
        self.server._ensure_engram_gitignore(self.tmp)
        content = (self.tmp / ".gitignore").read_text(encoding="utf-8")
        lines = {l.strip() for l in content.splitlines()}
        missing = [p for p in REQUIRED_GITIGNORE_PATTERNS if p not in lines]
        assert not missing, (
            f".gitignore is missing required patterns: {missing}\n"
            f"Found lines: {sorted(lines)}"
        )

    def test_gitignore_not_overwritten_when_complete(self):
        """_ensure_engram_gitignore leaves .gitignore unchanged when all patterns are present."""
        gitignore = self.tmp / ".gitignore"
        # Write canonical content first
        self.server._ensure_engram_gitignore(self.tmp)
        original_mtime = gitignore.stat().st_mtime
        original_content = gitignore.read_text(encoding="utf-8")

        # Call again — should be a no-op
        import time
        time.sleep(0.05)  # ensure mtime would differ if file was written
        self.server._ensure_engram_gitignore(self.tmp)

        new_content = gitignore.read_text(encoding="utf-8")
        assert new_content == original_content, \
            "File was rewritten even though all patterns were already present"

    def test_gitignore_db_pattern_excludes_knowledge_db(self):
        """The *.db pattern must match knowledge.db."""
        self.server._ensure_engram_gitignore(self.tmp)
        content = (self.tmp / ".gitignore").read_text(encoding="utf-8")
        assert "*.db" in content, "*.db pattern missing — knowledge.db would be tracked"

    def test_gitignore_updated_when_patterns_missing(self):
        """If .gitignore exists but is missing patterns, it is refreshed."""
        gitignore = self.tmp / ".gitignore"
        # Write an incomplete .gitignore
        gitignore.write_text("*.pyc\n", encoding="utf-8")

        self.server._ensure_engram_gitignore(self.tmp)

        content = gitignore.read_text(encoding="utf-8")
        lines = {l.strip() for l in content.splitlines()}
        missing = [p for p in REQUIRED_GITIGNORE_PATTERNS if p not in lines]
        assert not missing, f"After refresh, .gitignore still missing: {missing}"


# ---------------------------------------------------------------------------
# Test Area 3: engram-regenerate-embeddings.py backfill
# ---------------------------------------------------------------------------

class TestRegenerateEmbeddings:
    """engram-regenerate-embeddings.py backfills NULL embeddings."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_regen_"))
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"

    def _make_db_with_nulls(self, num_nodes: int = 4) -> Path:
        """Create a fixture DB with all embeddings NULL (simulates git-restore state)."""
        db_path = self.tmp / "knowledge.db"
        _make_fixture_db(db_path, num_nodes=num_nodes, with_embeddings=False)
        return db_path

    def _run_backfill_with_mock_embedder(self, engram_home: Path) -> None:
        """Run the backfill using a mock embedder that returns a fixed vector."""
        regen_script = REPO_ROOT / "tools" / "engram-regenerate-embeddings.py"
        if not regen_script.exists():
            pytest.skip("tools/engram-regenerate-embeddings.py not found")

        # Import the backfill function directly (avoids subprocess overhead)
        spec_name = "engram_regenerate_embeddings"
        import importlib.util
        spec = importlib.util.spec_from_file_location(spec_name, str(regen_script))
        regen_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(regen_mod)

        # Use mock server with a predictable embedder
        import server as srv
        srv._configure_paths(engram_home)

        fake_vector = [0.5] * 384

        def _fake_embed_batch(texts, model_name):
            return [fake_vector for _ in texts]

        def _fake_embed(text, model_name):
            return fake_vector

        def _fake_embedding_text(conn, node_id):
            row = conn.execute("SELECT claim FROM nodes WHERE id=?", (node_id,)).fetchone()
            return row[0] if row else ""

        with mock.patch.object(srv._embedder, "is_available", return_value=True), \
             mock.patch.object(srv._embedder, "_load_model", return_value=None), \
             mock.patch.object(srv._embedder, "_model", "fake_model", create=True), \
             mock.patch.object(srv._embedder, "embed_batch", side_effect=_fake_embed_batch), \
             mock.patch.object(srv._embedder, "embed", side_effect=_fake_embed), \
             mock.patch("server._embedding_text_for_node", side_effect=_fake_embedding_text):
            # Patch _find_server_py to return our server.py
            with mock.patch.object(regen_mod, "_find_server_py", return_value=REPO_ROOT / "server.py"), \
                 mock.patch.object(regen_mod, "_import_server", return_value=srv):
                regen_mod.backfill_embeddings(engram_home)

    def test_null_embeddings_backfilled(self):
        """Nodes with NULL embedding get a non-NULL embedding after backfill."""
        db_path = self._make_db_with_nulls(num_nodes=4)
        embs_before = _get_embeddings(db_path)
        assert all(v is None for v in embs_before.values()), \
            "Pre-condition: all embeddings should be NULL"

        self._run_backfill_with_mock_embedder(self.tmp)

        embs_after = _get_embeddings(db_path)
        non_null = [v for v in embs_after.values() if v is not None]
        assert len(non_null) == len(embs_after), (
            f"Expected all {len(embs_after)} embeddings to be backfilled, "
            f"got {len(non_null)} non-NULL"
        )

    def test_existing_embeddings_unchanged(self):
        """Nodes with an existing embedding are not re-embedded (idempotent)."""
        db_path = self.tmp / "knowledge.db"
        # Create DB with some nodes already having embeddings
        _make_fixture_db(db_path, num_nodes=4, with_embeddings=True)

        embs_before = _get_embeddings(db_path)
        non_null_before = {k: v for k, v in embs_before.items() if v is not None}
        assert non_null_before, "Pre-condition: some nodes should have embeddings"

        # Run backfill — should only touch NULL nodes
        self._run_backfill_with_mock_embedder(self.tmp)

        embs_after = _get_embeddings(db_path)
        for node_id, original_emb in non_null_before.items():
            assert embs_after[node_id] == original_emb, (
                f"Node {node_id} had its embedding changed by backfill — "
                f"expected idempotent behavior"
            )


# ---------------------------------------------------------------------------
# Test Area 4: _commit_snapshot early-return paths carry dump fields
# ---------------------------------------------------------------------------

class TestCommitSnapshotDumpFieldsOnEarlyReturn:
    """All _commit_snapshot early-return paths carry sql_dump_error when the dump fails.

    Round-2 fix: the dump fields must appear on EVERY return path, not only the
    final success path. This tests the four early-return dicts:
      - no tracked files exist
      - git add failed
      - no changes since last commit
      - git commit failed
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_earlyret_"))
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        # Do NOT set ENGRAM_DISABLE_GIT — we need _git_available=True so we reach
        # the dump block and the post-dump return paths.
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        server._git_available = True  # bypass git init; we'll mock _git calls
        self.server = server
        self.db_path = self.tmp / "knowledge.db"
        _make_fixture_db(self.db_path, num_nodes=2, with_embeddings=False)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENGRAM_DISABLE_GIT", None)

    def _failing_dump_patch(self):
        """Context manager: patch engram_backup.dump_stripped to raise."""
        import engram_backup as _eb
        return mock.patch.object(_eb, "dump_stripped", side_effect=RuntimeError("synthetic dump failure"))

    def _snapshot_patch(self):
        """Context manager: patch _generate_snapshot to avoid real schema dependency."""
        return mock.patch.object(self.server, "_generate_snapshot", return_value="# test snapshot\n")

    def _fake_git_result(self, returncode=0, stdout="", stderr=""):
        r = mock.Mock()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def _conn(self):
        import sqlite3 as _sqlite3
        return _sqlite3.connect(str(self.db_path))

    def test_no_tracked_files_path_carries_dump_error(self):
        """'no tracked files exist' return dict includes sql_dump_error on dump failure."""
        conn = self._conn()
        with self._snapshot_patch(), self._failing_dump_patch():
            with mock.patch.object(self.server, "_git", return_value=self._fake_git_result(0, "", "")):
                result = self.server._commit_snapshot(conn, "test", "nap")
        conn.close()
        # DATA_DIR has no tracked files (besides the snapshot just written) → hits
        # "no tracked files exist" path; sql_dump_error must appear.
        assert "sql_dump_error" in result, (
            f"Expected sql_dump_error in 'no tracked files' return; got: {result}"
        )
        assert "synthetic dump failure" in result["sql_dump_error"]

    def test_git_add_failed_path_carries_dump_error(self):
        """'git add failed' return dict includes sql_dump_error on dump failure."""
        # Create a tracked file so files_to_stage is non-empty
        (self.tmp / "graph_snapshot.md").write_text("x")
        conn = self._conn()
        with self._snapshot_patch(), self._failing_dump_patch():
            with mock.patch.object(
                self.server, "_git",
                side_effect=[
                    self._fake_git_result(1, stderr="git add error"),  # git add → fail
                ],
            ):
                result = self.server._commit_snapshot(conn, "test", "nap")
        conn.close()
        assert "sql_dump_error" in result, (
            f"Expected sql_dump_error in 'git add failed' return; got: {result}"
        )
        assert "synthetic dump failure" in result["sql_dump_error"]

    def test_no_changes_path_carries_dump_error(self):
        """'no changes since last commit' return dict includes sql_dump_error on dump failure."""
        (self.tmp / "graph_snapshot.md").write_text("x")
        conn = self._conn()
        with self._snapshot_patch(), self._failing_dump_patch():
            with mock.patch.object(
                self.server, "_git",
                side_effect=[
                    self._fake_git_result(0),            # git add → ok
                    self._fake_git_result(0, stdout=""),  # git status --porcelain → empty (no changes)
                    self._fake_git_result(0, stdout="abc123"),  # rev-parse HEAD
                ],
            ):
                result = self.server._commit_snapshot(conn, "test", "nap")
        conn.close()
        assert "sql_dump_error" in result, (
            f"Expected sql_dump_error in 'no changes since last commit' return; got: {result}"
        )
        assert "synthetic dump failure" in result["sql_dump_error"]

    def test_git_commit_failed_path_carries_dump_error(self):
        """'git commit failed' return dict includes sql_dump_error on dump failure."""
        (self.tmp / "graph_snapshot.md").write_text("x")
        conn = self._conn()
        with self._snapshot_patch(), self._failing_dump_patch():
            with mock.patch.object(
                self.server, "_git",
                side_effect=[
                    self._fake_git_result(0),                            # git add → ok
                    self._fake_git_result(0, stdout="M graph_snapshot.md"),  # git status → has changes
                    self._fake_git_result(1, stderr="commit error"),          # git commit → fail
                ],
            ):
                result = self.server._commit_snapshot(conn, "test", "nap")
        conn.close()
        assert "sql_dump_error" in result, (
            f"Expected sql_dump_error in 'git commit failed' return; got: {result}"
        )
        assert "synthetic dump failure" in result["sql_dump_error"]
