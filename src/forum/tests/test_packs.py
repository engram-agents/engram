"""Tests for forum pack registry: POST /api/packs, GET /api/packs,
GET /api/packs/<id>, GET /api/packs/<id>/download.

Fixture strategy: build minimal valid engram-package tarballs in-test.
A valid package contains:
  - knowledge.sql  (schema with nodes + edges tables, no rows = empty closure)
  - scripts/       (directory, contents irrelevant for these tests)
  - README.md

The empty closure is closure-complete by definition (no edges = no dangling
endpoints), so it passes _assert_closure_completeness without requiring the
full engram-pkg init flow or the sqlite3 CLI.

Closure-violation fixture: knowledge.sql that has an edge but its target
node is absent — that triggers the invariant failure path.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on path
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Minimal knowledge.sql that satisfies the schema check
# ---------------------------------------------------------------------------

# This is the minimal DDL the packs validation reads:
# nodes table + edges table, both empty.  An empty graph is trivially
# closure-complete (no edges → no dangling endpoints).
_MINIMAL_SQL = """\
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT,
    claim TEXT,
    confidence REAL,
    evidence_id TEXT,
    source_class TEXT,
    quote_type TEXT,
    status TEXT,
    created_at TEXT,
    updated_at TEXT,
    reasoning_type TEXT,
    logical_chain TEXT,
    tags TEXT,
    author TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at TEXT
);
"""

# A knowledge.sql that has an edge whose target node is missing.
# This must trigger the closure-completeness invariant violation → 400.
_CLOSURE_VIOLATION_SQL = """\
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT,
    claim TEXT,
    confidence REAL,
    evidence_id TEXT,
    source_class TEXT,
    quote_type TEXT,
    status TEXT,
    created_at TEXT,
    updated_at TEXT,
    reasoning_type TEXT,
    logical_chain TEXT,
    tags TEXT,
    author TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at TEXT
);
INSERT INTO nodes VALUES ('ob_0001', 'observation', 'test claim', 0.85,
    NULL, NULL, NULL, 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
    NULL, NULL, NULL, 'test-agent');
INSERT INTO edges VALUES (1, 'ob_0001', 'ev_9999', 'derives_from', '2026-01-01T00:00:00Z');
"""


def _make_pack_tarball(
    knowledge_sql: str = _MINIMAL_SQL,
    include_scripts: bool = True,
    include_readme: bool = True,
    extra_files: dict | None = None,
) -> bytes:
    """Build a minimal pack tarball in memory; return raw bytes.

    Args:
        knowledge_sql:   Content of knowledge.sql.
        include_scripts: Whether to include scripts/ directory.
        include_readme:  Whether to include README.md.
        extra_files:     Dict of {relative_path: content_bytes} for additional files.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Pack directory name inside the tarball.
        pkg = "test-pack"

        def _add_str(arcname: str, content: str) -> None:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        def _add_bytes(arcname: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        def _add_dir(arcname: str) -> None:
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)

        # knowledge.sql (always included unless content is None)
        if knowledge_sql is not None:
            _add_str(f"{pkg}/knowledge.sql", knowledge_sql)

        if include_scripts:
            _add_dir(f"{pkg}/scripts")
            _add_str(f"{pkg}/scripts/build.sh", "#!/bin/bash\n# stub build script\n")
            _add_str(f"{pkg}/scripts/dump.sh", "#!/bin/bash\n# stub dump script\n")

        if include_readme:
            _add_str(f"{pkg}/README.md", "# Test Pack\n\nA minimal test pack.\n")

        if extra_files:
            for rel_path, content in extra_files.items():
                if isinstance(content, str):
                    _add_str(f"{pkg}/{rel_path}", content)
                else:
                    _add_bytes(f"{pkg}/{rel_path}", content)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    packs_dir = str(tmp_path / "packs")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path, packs_dir=packs_dir)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def app_small_limit(tmp_path):
    """App fixture with a very small MAX_CONTENT_LENGTH for 413 testing.

    We lower the limit to 256 bytes so a normal test tarball (>400 bytes even
    at minimal size) triggers the guard without crafting a 50 MB upload.
    """
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    packs_dir = str(tmp_path / "packs")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path, packs_dir=packs_dir)
    application.config["TESTING"] = True
    application.config["MAX_CONTENT_LENGTH"] = 256  # 256 B — triggers on any real tarball
    return application


@pytest.fixture
def client_small_limit(app_small_limit):
    return app_small_limit.test_client()


# ---------------------------------------------------------------------------
# Helper: upload a pack via test_client (multipart form-data)
# ---------------------------------------------------------------------------

def _upload_pack(client, tarball_bytes: bytes, agent: str = "agent-a") -> object:
    """POST a pack tarball to /api/packs via multipart upload."""
    data = {
        "agent": agent,
        "pack": (io.BytesIO(tarball_bytes), "test-pack.tar.gz"),
    }
    return client.post(
        "/api/packs",
        data=data,
        content_type="multipart/form-data",
    )


# ---------------------------------------------------------------------------
# Publish happy path
# ---------------------------------------------------------------------------

class TestPacksPublishHappyPath:
    def test_publish_returns_201(self, client):
        tarball = _make_pack_tarball()
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 201, resp.data.decode()

    def test_publish_returns_pack_id(self, client):
        tarball = _make_pack_tarball()
        resp = _upload_pack(client, tarball)
        data = json.loads(resp.data)
        assert "pack_id" in data
        assert data["pack_id"].startswith("agent-a-")

    def test_publish_returns_node_edge_counts(self, client):
        tarball = _make_pack_tarball()
        resp = _upload_pack(client, tarball)
        data = json.loads(resp.data)
        assert data["node_count"] == 0
        assert data["edge_count"] == 0

    def test_publish_version_1_on_first_upload(self, client):
        tarball = _make_pack_tarball()
        resp = _upload_pack(client, tarball)
        data = json.loads(resp.data)
        assert data["version"] == 1

    def test_pack_appears_in_list(self, client):
        tarball = _make_pack_tarball()
        _upload_pack(client, tarball)
        resp = client.get("/api/packs")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["packs"]) == 1

    def test_pack_retrievable_by_id(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball)
        pack_id = json.loads(pub_resp.data)["pack_id"]
        resp = client.get(f"/api/packs/{pack_id}")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["pack"]["id"] == pack_id

    def test_download_returns_tarball(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball)
        pack_id = json.loads(pub_resp.data)["pack_id"]
        resp = client.get(f"/api/packs/{pack_id}/download")
        assert resp.status_code == 200
        # The response should be a valid gzip tarball.
        import tarfile as _tf
        import io as _io
        buf = _io.BytesIO(resp.data)
        assert _tf.is_tarfile(buf)


# ---------------------------------------------------------------------------
# Version increment on re-publish
# ---------------------------------------------------------------------------

class TestPackVersionIncrement:
    def test_second_publish_increments_version(self, client):
        tarball = _make_pack_tarball()
        _upload_pack(client, tarball)
        resp2 = _upload_pack(client, tarball)
        data = json.loads(resp2.data)
        assert data["version"] == 2

    def test_third_publish_gives_version_3(self, client):
        tarball = _make_pack_tarball()
        for _ in range(3):
            resp = _upload_pack(client, tarball)
        data = json.loads(resp.data)
        assert data["version"] == 3

    def test_different_agents_share_version_namespace(self, client):
        # Version is keyed on (author, name), so different agents share no space.
        tarball = _make_pack_tarball()
        r1 = _upload_pack(client, tarball, agent="agent-a")
        r2 = _upload_pack(client, tarball, agent="agent-b")
        assert json.loads(r1.data)["version"] == 1
        assert json.loads(r2.data)["version"] == 1


# ---------------------------------------------------------------------------
# Closure-violation rejection
# ---------------------------------------------------------------------------

class TestPacksClosureViolationRejection:
    def test_dangling_edge_rejected_400(self, client):
        tarball = _make_pack_tarball(knowledge_sql=_CLOSURE_VIOLATION_SQL)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400, resp.data.decode()

    def test_dangling_edge_error_mentions_closure(self, client):
        tarball = _make_pack_tarball(knowledge_sql=_CLOSURE_VIOLATION_SQL)
        resp = _upload_pack(client, tarball)
        body = json.loads(resp.data)
        assert "closure" in body["error"].lower() or "invariant" in body["error"].lower()


# ---------------------------------------------------------------------------
# Size-guard rejection
# ---------------------------------------------------------------------------

class TestPacksSizeGuardRejection:
    def _make_oversized_sql(self, node_count: int) -> str:
        """Build knowledge.sql with node_count isolated nodes (no edges)."""
        lines = [_MINIMAL_SQL]
        for i in range(node_count):
            nid = f"ob_{i:04d}"
            lines.append(
                f"INSERT INTO nodes VALUES ('{nid}', 'observation', 'claim {i}', 0.85, "
                f"NULL, NULL, NULL, 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                f"NULL, NULL, NULL, 'test-agent');"
            )
        return "\n".join(lines)

    def test_over_node_limit_rejected_400(self, client):
        # 201 nodes > MAX_NODES=200
        oversized_sql = self._make_oversized_sql(201)
        tarball = _make_pack_tarball(knowledge_sql=oversized_sql)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400, resp.data.decode()

    def test_size_guard_error_mentions_limit(self, client):
        oversized_sql = self._make_oversized_sql(201)
        tarball = _make_pack_tarball(knowledge_sql=oversized_sql)
        resp = _upload_pack(client, tarball)
        body = json.loads(resp.data)
        assert "size guard" in body["error"].lower() or "max" in body["error"].lower()


# ---------------------------------------------------------------------------
# Shape validation — missing required files
# ---------------------------------------------------------------------------

class TestPacksShapeValidation:
    def test_missing_knowledge_sql_rejected(self, client):
        tarball = _make_pack_tarball(knowledge_sql=None)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "knowledge.sql" in body["error"]

    def test_missing_scripts_dir_rejected(self, client):
        tarball = _make_pack_tarball(include_scripts=False)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "scripts" in body["error"]

    def test_missing_readme_rejected(self, client):
        tarball = _make_pack_tarball(include_readme=False)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "README" in body["error"]

    def test_missing_agent_rejected(self, client):
        tarball = _make_pack_tarball()
        resp = client.post(
            "/api/packs",
            data={"pack": (io.BytesIO(tarball), "test.tar.gz")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "agent" in body["error"].lower()

    def test_missing_pack_field_rejected(self, client):
        resp = client.post(
            "/api/packs",
            data={"agent": "agent-a"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "pack" in body["error"].lower()


# ---------------------------------------------------------------------------
# List and get
# ---------------------------------------------------------------------------

class TestPacksListGet:
    def test_list_empty_initially(self, client):
        resp = client.get("/api/packs")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["packs"] == []

    def test_list_after_upload_has_one_pack(self, client):
        tarball = _make_pack_tarball()
        _upload_pack(client, tarball)
        resp = client.get("/api/packs")
        data = json.loads(resp.data)
        assert len(data["packs"]) == 1

    def test_list_meta_fields(self, client):
        tarball = _make_pack_tarball()
        _upload_pack(client, tarball)
        resp = client.get("/api/packs")
        pack = json.loads(resp.data)["packs"][0]
        for field in ("id", "author", "name", "version", "uploaded_at", "node_count", "edge_count"):
            assert field in pack, f"missing field {field!r}"

    def test_get_unknown_pack_404(self, client):
        resp = client.get("/api/packs/does-not-exist-v1")
        assert resp.status_code == 404

    def test_download_unknown_pack_404(self, client):
        resp = client.get("/api/packs/does-not-exist-v1/download")
        assert resp.status_code == 404

    def test_list_two_packs_after_two_uploads(self, client):
        tarball = _make_pack_tarball()
        _upload_pack(client, tarball, agent="agent-a")
        _upload_pack(client, tarball, agent="agent-b")
        resp = client.get("/api/packs")
        data = json.loads(resp.data)
        assert len(data["packs"]) == 2


# ---------------------------------------------------------------------------
# Helpers for security / content-limit tests
# ---------------------------------------------------------------------------

def _make_symlink_tarball() -> bytes:
    """Build a tarball that contains a symlink member pointing outside the
    extract directory.  The server must reject this with 400.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        pkg = "test-pack"

        def _add_str(arcname: str, content: str) -> None:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        def _add_dir(arcname: str) -> None:
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)

        # Required files so the pack would pass shape validation if symlinks
        # weren't caught first.
        _add_str(f"{pkg}/knowledge.sql", _MINIMAL_SQL)
        _add_dir(f"{pkg}/scripts")
        _add_str(f"{pkg}/README.md", "# Test\n")

        # Add a symlink that points outside the extraction directory.
        sym_info = tarfile.TarInfo(name=f"{pkg}/evil-link")
        sym_info.type = tarfile.SYMTYPE
        sym_info.linkname = "../../../../etc/passwd"
        tf.addfile(sym_info)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# MAX_CONTENT_LENGTH — oversized upload returns 413
# ---------------------------------------------------------------------------

class TestPacksContentLengthLimit:
    def test_oversized_upload_returns_413(self, client_small_limit):
        """Uploading any tarball should hit the 256-byte limit set on the
        small-limit fixture and return 413."""
        tarball = _make_pack_tarball()
        # Verify the tarball is actually larger than 256 bytes so the test is
        # meaningful (a real tarball is always >256 bytes due to gzip framing).
        assert len(tarball) > 256, (
            f"Test tarball is only {len(tarball)} bytes — smaller than the 256-byte limit; "
            "the test would be vacuous."
        )
        resp = _upload_pack(client_small_limit, tarball)
        assert resp.status_code == 413, (
            f"Expected 413, got {resp.status_code}: {resp.data[:200]}"
        )


# ---------------------------------------------------------------------------
# Symlink member rejection
# ---------------------------------------------------------------------------

class TestPacksSymlinkRejection:
    def test_symlink_member_rejected_400(self, client):
        """A tarball containing a symlink entry must be rejected with 400."""
        tarball = _make_symlink_tarball()
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400, resp.data.decode()

    def test_symlink_rejection_error_mentions_link(self, client):
        """The 400 error message should mention symlink or link."""
        tarball = _make_symlink_tarball()
        resp = _upload_pack(client, tarball)
        body = json.loads(resp.data)
        assert (
            "link" in body["error"].lower() or "sym" in body["error"].lower()
        ), f"Unexpected error: {body['error']}"


# ---------------------------------------------------------------------------
# Concurrent validation — pure checker must not interfere across threads
# ---------------------------------------------------------------------------

class TestConcurrentValidation:
    """With the pure _find_dangling_edges checker there is no shared mutable
    state (no sys.exit patch window), so concurrent validations are trivially
    thread-safe.  This test asserts both outcomes are correct when a valid pack
    and a closure-violating pack are validated simultaneously.
    """

    def test_concurrent_good_and_bad_pack_both_correct(self, app):
        """Two threads validating simultaneously: one 201, one 400."""
        import threading

        good_tarball = _make_pack_tarball()
        bad_tarball = _make_pack_tarball(knowledge_sql=_CLOSURE_VIOLATION_SQL)

        results = {}

        def upload_good():
            with app.test_client() as c:
                results["good"] = _upload_pack(c, good_tarball)

        def upload_bad():
            with app.test_client() as c:
                results["bad"] = _upload_pack(c, bad_tarball)

        t1 = threading.Thread(target=upload_good)
        t2 = threading.Thread(target=upload_bad)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert results.get("good") is not None, "good thread did not complete"
        assert results.get("bad") is not None, "bad thread did not complete"

        assert results["good"].status_code == 201, (
            f"Expected 201 for valid pack, got {results['good'].status_code}: "
            f"{results['good'].data.decode()}"
        )
        assert results["bad"].status_code == 400, (
            f"Expected 400 for closure-violating pack, got {results['bad'].status_code}: "
            f"{results['bad'].data.decode()}"
        )


# ---------------------------------------------------------------------------
# Size-guard env-override tests
# ---------------------------------------------------------------------------

class TestSizeGuardEnvOverride:
    """FORUM_PACK_MAX_NODES / FORUM_PACK_MAX_EDGES override the hard-coded
    defaults, letting operators raise limits without a code change.
    """

    def _make_sql_with_nodes(self, n: int) -> str:
        """Build knowledge.sql with n isolated nodes (no edges)."""
        lines = [_MINIMAL_SQL]
        for i in range(n):
            nid = f"ob_{i:04d}"
            lines.append(
                f"INSERT INTO nodes VALUES ('{nid}', 'observation', 'claim {i}', 0.85, "
                f"NULL, NULL, NULL, 'active', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                f"NULL, NULL, NULL, 'test-agent');"
            )
        return "\n".join(lines)

    def test_env_override_raises_node_limit(self, client, monkeypatch):
        """With FORUM_PACK_MAX_NODES raised to 300, a 250-node pack is accepted."""
        monkeypatch.setenv("FORUM_PACK_MAX_NODES", "300")
        sql = self._make_sql_with_nodes(250)
        tarball = _make_pack_tarball(knowledge_sql=sql)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 201, (
            f"Expected 201 with raised limit, got {resp.status_code}: {resp.data.decode()}"
        )
        body = resp.get_json()
        assert body["node_count"] == 250, (
            f"Override took effect (status 201) but node_count mismatch: {body}"
        )

    def test_default_limit_still_rejects_over_200(self, client, monkeypatch):
        """Without override, a 201-node pack is still rejected (default 200)."""
        monkeypatch.delenv("FORUM_PACK_MAX_NODES", raising=False)
        monkeypatch.delenv("FORUM_PACK_MAX_EDGES", raising=False)
        sql = self._make_sql_with_nodes(201)
        tarball = _make_pack_tarball(knowledge_sql=sql)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400, resp.data.decode()

    def test_invalid_env_value_falls_back_to_default(self, client, monkeypatch):
        """A non-integer FORUM_PACK_MAX_NODES silently falls back to 200."""
        monkeypatch.setenv("FORUM_PACK_MAX_NODES", "not-a-number")
        # 201 nodes should still be rejected (default 200 in effect)
        sql = self._make_sql_with_nodes(201)
        tarball = _make_pack_tarball(knowledge_sql=sql)
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 400, (
            f"Expected 400 with invalid env (default fallback), got {resp.status_code}: "
            f"{resp.data.decode()}"
        )


# ---------------------------------------------------------------------------
# Missing validator → 503 with detail
# ---------------------------------------------------------------------------

class TestMissingValidatorReturns503:
    """When the engram-pkg CLI is absent, publish returns 503 with a JSON
    body naming the missing component and the expected path.
    """

    def test_missing_cli_returns_503(self, client, monkeypatch, tmp_path):
        """Monkeypatch cli_path to a nonexistent location → 503."""
        import forum.packs as _packs

        nonexistent = tmp_path / "no-such-dir" / "engram-pkg"

        original_load = _packs._load_engram_pkg_cli

        def _patched_load():
            raise FileNotFoundError(
                f"engram-pkg CLI not found at {nonexistent}. "
                "Cannot run closure validation."
            )

        monkeypatch.setattr(_packs, "_load_engram_pkg_cli", _patched_load)
        # Also reset the cached module so the patch actually fires.
        monkeypatch.setattr(_packs, "_CLI_MODULE", None)

        tarball = _make_pack_tarball()
        resp = _upload_pack(client, tarball)
        assert resp.status_code == 503, (
            f"Expected 503, got {resp.status_code}: {resp.data.decode()}"
        )

    def test_missing_cli_503_body_names_component(self, client, monkeypatch, tmp_path):
        """503 body must name the missing component ('engram-pkg')."""
        import forum.packs as _packs

        nonexistent = tmp_path / "no-such-dir" / "engram-pkg"

        def _patched_load():
            raise FileNotFoundError(
                f"engram-pkg CLI not found at {nonexistent}. "
                "Cannot run closure validation."
            )

        monkeypatch.setattr(_packs, "_load_engram_pkg_cli", _patched_load)
        monkeypatch.setattr(_packs, "_CLI_MODULE", None)

        tarball = _make_pack_tarball()
        resp = _upload_pack(client, tarball)
        body = json.loads(resp.data)
        assert "engram-pkg" in body.get("missing_component", ""), (
            f"Expected 'engram-pkg' in missing_component, got: {body}"
        )

    def test_missing_cli_503_body_names_expected_path(self, client, monkeypatch, tmp_path):
        """503 body must include the expected_path field with the CLI path."""
        import forum.packs as _packs

        nonexistent = tmp_path / "no-such-dir" / "engram-pkg"

        def _patched_load():
            raise FileNotFoundError(
                f"engram-pkg CLI not found at {nonexistent}. "
                "Cannot run closure validation."
            )

        monkeypatch.setattr(_packs, "_load_engram_pkg_cli", _patched_load)
        monkeypatch.setattr(_packs, "_CLI_MODULE", None)

        tarball = _make_pack_tarball()
        resp = _upload_pack(client, tarball)
        body = json.loads(resp.data)
        assert "expected_path" in body, f"Missing 'expected_path' key in body: {body}"
        assert body["expected_path"], "expected_path must be non-empty"
