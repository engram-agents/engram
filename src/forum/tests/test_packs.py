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
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on path
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from forum import db
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


# ---------------------------------------------------------------------------
# Deployed-layout path test
# ---------------------------------------------------------------------------

class TestEngramPkgCliPathDeployedLayout:
    """_engram_pkg_cli_path() finds engram-pkg in the deployed directory layout.

    Deployed layout (created by install-forum-service.sh):
      $APP/forum/packs.py   <- packs.py lives here
      $APP/tools/engram-pkg/engram-pkg  <- installer copies here

    The upward search from $APP/forum/ hits $APP/ on its second step and
    finds tools/engram-pkg/engram-pkg there.
    """

    def test_deployed_layout_found(self, tmp_path, monkeypatch):
        """Deployed layout: engram-pkg 2 hops above packs.py -> found."""
        import forum.packs as _packs

        # Create a temp dir tree matching the deployed layout:
        #   tmp_path/app/forum/packs.py  (simulated)
        #   tmp_path/app/tools/engram-pkg/engram-pkg  (copied by installer)
        app_dir = tmp_path / "app"
        forum_dir = app_dir / "forum"
        forum_dir.mkdir(parents=True)
        cli_dir = app_dir / "tools" / "engram-pkg"
        cli_dir.mkdir(parents=True)
        cli_file = cli_dir / "engram-pkg"
        cli_file.write_text("# stub\n")

        # Monkeypatch __file__ to simulate deployed packs.py location.
        monkeypatch.setattr(_packs, "__file__", str(forum_dir / "packs.py"))

        result = _packs._engram_pkg_cli_path()
        assert result.exists(), (
            f"Expected _engram_pkg_cli_path() to find the CLI at the deployed "
            f"layout, but {result} does not exist."
        )
        assert result == cli_file.resolve(), (
            f"Expected {cli_file.resolve()}, got {result}"
        )

    def test_deployed_layout_missing_returns_fallback(self, tmp_path, monkeypatch):
        """Deployed layout without engram-pkg -> fallback path (non-existent)."""
        import forum.packs as _packs

        app_dir = tmp_path / "app"
        forum_dir = app_dir / "forum"
        forum_dir.mkdir(parents=True)
        # Do NOT create the engram-pkg CLI -- simulates a broken deploy.

        monkeypatch.setattr(_packs, "__file__", str(forum_dir / "packs.py"))

        result = _packs._engram_pkg_cli_path()
        assert not result.exists(), (
            f"Expected fallback (non-existent) path when CLI absent, "
            f"but {result} exists."
        )


# ---------------------------------------------------------------------------
# Share-clearance: schema migration (privacy-incident follow-up, #676 slice 1)
# ---------------------------------------------------------------------------

def _fresh_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


class TestPackClearanceMigration:
    def test_fresh_db_has_clearance_columns(self):
        """A brand-new DB gets the four clearance columns straight from the
        base CREATE TABLE (no ALTER TABLE path needed)."""
        c = _fresh_conn()
        init_db(c)
        cols = {r[1] for r in c.execute("PRAGMA table_info(packs)")}
        assert {
            "share_clearance",
            "clearance_read_by",
            "clearance_read_at",
            "clearance_method",
        } <= cols
        c.close()

    def test_fresh_db_share_clearance_default_is_private(self):
        c = _fresh_conn()
        init_db(c)
        db.insert_pack(
            c, pack_id="a-p-v1", author="a", name="p", version=1,
            uploaded_at="2026-01-01T00:00:00Z",
            root_count=0, node_count=0, edge_count=0,
        )
        row = c.execute(
            "SELECT share_clearance, clearance_read_by, clearance_read_at, "
            "clearance_method FROM packs WHERE id='a-p-v1'"
        ).fetchone()
        assert row[0] == "private"
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None
        c.close()

    def test_pre_clearance_db_migrated_via_alter_table(self):
        """A pre-clearance packs table (no share_clearance/clearance_* columns)
        is migrated idempotently by init_db, and existing rows backfill to
        share_clearance='private'."""
        c = _fresh_conn()

        # Simulate a pre-clearance packs table (the old 8-column shape).
        c.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                avatar_seed TEXT NOT NULL, pair_initials TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, hostname TEXT
            );
            CREATE TABLE IF NOT EXISTS packs (
                id              TEXT PRIMARY KEY,
                author          TEXT NOT NULL,
                name            TEXT NOT NULL,
                version         INTEGER NOT NULL,
                uploaded_at     TEXT NOT NULL,
                root_count      INTEGER NOT NULL DEFAULT 0,
                node_count      INTEGER NOT NULL DEFAULT 0,
                edge_count      INTEGER NOT NULL DEFAULT 0
            );
        """)
        c.execute(
            "INSERT INTO packs(id, author, name, version, uploaded_at, "
            "                  root_count, node_count, edge_count) "
            "VALUES('a-p-v1', 'a', 'p', 1, '2026-01-01T00:00:00Z', 0, 0, 0)"
        )
        c.commit()

        cols_before = {r[1] for r in c.execute("PRAGMA table_info(packs)")}
        assert "share_clearance" not in cols_before

        init_db(c)

        cols_after = {r[1] for r in c.execute("PRAGMA table_info(packs)")}
        assert {
            "share_clearance",
            "clearance_read_by",
            "clearance_read_at",
            "clearance_method",
        } <= cols_after

        row = c.execute(
            "SELECT share_clearance FROM packs WHERE id='a-p-v1'"
        ).fetchone()
        assert row[0] == "private"
        c.close()

    def test_migration_idempotent(self):
        """Running init_db twice on an already-migrated DB does not error."""
        c = _fresh_conn()
        init_db(c)
        init_db(c)  # must not raise
        cols = {r[1] for r in c.execute("PRAGMA table_info(packs)")}
        assert "share_clearance" in cols
        c.close()


# ---------------------------------------------------------------------------
# Share-clearance: DB helper — set_pack_clearance
# ---------------------------------------------------------------------------

class TestSetPackClearance:
    def _seed_pack(self, c, pack_id="a-p-v1", author="a"):
        db.insert_pack(
            c, pack_id=pack_id, author=author, name="p", version=1,
            uploaded_at="2026-01-01T00:00:00Z",
            root_count=0, node_count=0, edge_count=0,
        )

    def test_insert_pack_still_works_unchanged(self):
        c = _fresh_conn()
        init_db(c)
        self._seed_pack(c)
        packs = db.list_packs(c)
        assert len(packs) == 1
        assert packs[0]["id"] == "a-p-v1"
        c.close()

    def test_clear_to_share_succeeds_and_reflected_in_get_pack(self):
        c = _fresh_conn()
        init_db(c)
        self._seed_pack(c)
        ok = db.set_pack_clearance(
            c, "a-p-v1", clearance="cleared-to-share",
            read_by="a", read_at="2026-01-02T00:00:00Z",
            method="full read (pack files + database)",
        )
        assert ok is True
        pack = db.get_pack(c, "a-p-v1")
        assert pack["share_clearance"] == "cleared-to-share"
        assert pack["clearance_read_by"] == "a"
        assert pack["clearance_read_at"] == "2026-01-02T00:00:00Z"
        assert pack["clearance_method"] == "full read (pack files + database)"
        c.close()

    def test_invalid_clearance_raises_value_error(self):
        c = _fresh_conn()
        init_db(c)
        self._seed_pack(c)
        with pytest.raises(ValueError):
            db.set_pack_clearance(
                c, "a-p-v1", clearance="bogus",
                read_by="a", read_at="2026-01-02T00:00:00Z", method=None,
            )
        c.close()

    def test_nonexistent_pack_id_returns_false(self):
        c = _fresh_conn()
        init_db(c)
        ok = db.set_pack_clearance(
            c, "does-not-exist-v1", clearance="private",
            read_by="a", read_at="2026-01-02T00:00:00Z", method=None,
        )
        assert ok is False
        c.close()

    def test_free_text_method_not_constrained_to_enum(self):
        """The method string is free text -- an arbitrary sentence is accepted
        (the real incident this schema addresses: 'full read (database only)'
        turned out to be insufficient because the pack also had un-searched
        files, so the method string must record WHAT was read, not just a
        full-read boolean)."""
        c = _fresh_conn()
        init_db(c)
        self._seed_pack(c)
        ok = db.set_pack_clearance(
            c, "a-p-v1", clearance="cleared-to-share",
            read_by="a", read_at="2026-01-02T00:00:00Z",
            method="full read (database only) -- README not separately grepped",
        )
        assert ok is True
        pack = db.get_pack(c, "a-p-v1")
        assert pack["clearance_method"] == (
            "full read (database only) -- README not separately grepped"
        )
        c.close()


# ---------------------------------------------------------------------------
# Share-clearance: GET responses include the new fields
# ---------------------------------------------------------------------------

class TestPackClearanceFieldsInGetResponses:
    def test_list_packs_includes_clearance_fields(self, client):
        tarball = _make_pack_tarball()
        _upload_pack(client, tarball, agent="agent-a")
        resp = client.get("/api/packs")
        pack = json.loads(resp.data)["packs"][0]
        for field in (
            "share_clearance", "clearance_read_by",
            "clearance_read_at", "clearance_method",
        ):
            assert field in pack, f"missing field {field!r}"
        assert pack["share_clearance"] == "private"

    def test_get_pack_includes_clearance_fields(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball, agent="agent-a")
        pack_id = json.loads(pub_resp.data)["pack_id"]
        resp = client.get(f"/api/packs/{pack_id}")
        pack = json.loads(resp.data)["pack"]
        for field in (
            "share_clearance", "clearance_read_by",
            "clearance_read_at", "clearance_method",
        ):
            assert field in pack, f"missing field {field!r}"
        assert pack["share_clearance"] == "private"


# ---------------------------------------------------------------------------
# Share-clearance: POST /api/packs/<id>/label
# ---------------------------------------------------------------------------

class TestPackLabelEndpoint:
    def test_author_can_clear_own_pack(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball, agent="agent-a")
        pack_id = json.loads(pub_resp.data)["pack_id"]

        resp = client.post(
            f"/api/packs/{pack_id}/label",
            data={
                "agent": "agent-a",
                "clearance": "cleared-to-share",
                "method": "full read (pack files + database)",
            },
        )
        assert resp.status_code == 200, resp.data.decode()
        pack = json.loads(resp.data)["pack"]
        assert pack["share_clearance"] == "cleared-to-share"
        assert pack["clearance_read_by"] == "agent-a"
        assert pack["clearance_method"] == "full read (pack files + database)"
        assert pack["clearance_read_at"]

    def test_author_can_revert_to_private(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball, agent="agent-a")
        pack_id = json.loads(pub_resp.data)["pack_id"]
        client.post(
            f"/api/packs/{pack_id}/label",
            data={
                "agent": "agent-a", "clearance": "cleared-to-share",
                "method": "full read (database only)",
            },
        )
        resp = client.post(
            f"/api/packs/{pack_id}/label",
            data={"agent": "agent-a", "clearance": "private"},
        )
        assert resp.status_code == 200, resp.data.decode()
        pack = json.loads(resp.data)["pack"]
        assert pack["share_clearance"] == "private"
        # method is cleared back to None when clearance reverts to private.
        assert pack["clearance_method"] is None

    def test_non_author_gets_403(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball, agent="agent-a")
        pack_id = json.loads(pub_resp.data)["pack_id"]

        resp = client.post(
            f"/api/packs/{pack_id}/label",
            data={
                "agent": "agent-b",
                "clearance": "cleared-to-share",
                "method": "full read (pack files + database)",
            },
        )
        assert resp.status_code == 403, resp.data.decode()
        body = json.loads(resp.data)
        assert "agent-b" in body["error"]
        assert "agent-a" in body["error"]

    def test_unknown_pack_404(self, client):
        resp = client.post(
            "/api/packs/does-not-exist-v1/label",
            data={"agent": "agent-a", "clearance": "private"},
        )
        assert resp.status_code == 404

    def test_invalid_clearance_value_400(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball, agent="agent-a")
        pack_id = json.loads(pub_resp.data)["pack_id"]

        resp = client.post(
            f"/api/packs/{pack_id}/label",
            data={"agent": "agent-a", "clearance": "bogus"},
        )
        assert resp.status_code == 400, resp.data.decode()

    def test_clearing_without_method_400(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball, agent="agent-a")
        pack_id = json.loads(pub_resp.data)["pack_id"]

        resp = client.post(
            f"/api/packs/{pack_id}/label",
            data={"agent": "agent-a", "clearance": "cleared-to-share"},
        )
        assert resp.status_code == 400, resp.data.decode()
        body = json.loads(resp.data)
        assert "method" in body["error"].lower()

    def test_missing_agent_400(self, client):
        tarball = _make_pack_tarball()
        pub_resp = _upload_pack(client, tarball, agent="agent-a")
        pack_id = json.loads(pub_resp.data)["pack_id"]

        resp = client.post(
            f"/api/packs/{pack_id}/label",
            data={"clearance": "private"},
        )
        assert resp.status_code == 400, resp.data.decode()


# ---------------------------------------------------------------------------
# Share-clearance: CLI round-trip (`forum pack label`)
#
# Mirrors the mock-_api_post-and-argv pattern from test_agent_status_cli.py
# (the one existing CLI-test harness in this codebase) -- there is no prior
# CLI-level test for the `pack` subcommand family specifically (pack
# publish/list/get are only exercised via the Flask test client above), so
# this is a new application of an existing pattern, not a new harness style.
# ---------------------------------------------------------------------------

try:
    # _REPO_ROOT is already on sys.path (see top of file).
    import tools.forum as forum_cli
    _PACK_CLI_AVAILABLE = True
except ImportError:
    forum_cli = None  # type: ignore[assignment]
    _PACK_CLI_AVAILABLE = False

SKIP_PACK_CLI = pytest.mark.skipif(
    not _PACK_CLI_AVAILABLE, reason="tools.forum CLI not importable"
)


@SKIP_PACK_CLI
class TestPackLabelCLI:
    # Fixtures are defined INSIDE this class so their autouse scope is
    # class-local — they must not touch forum_cli module state for the
    # unrelated Flask-test-client tests above in this same file.

    @pytest.fixture
    def _pack_cli_engram_home(self, tmp_path):
        home = tmp_path / "engram"
        home.mkdir()
        config = {"agent_name": "agent-a", "forum": {"url": "http://testserver"}}
        (home / "config.json").write_text(json.dumps(config))
        return home

    @pytest.fixture(autouse=True)
    def _reset_pack_cli_cache(self, _pack_cli_engram_home, monkeypatch):
        forum_cli._FORUM_URL_CACHE = None
        monkeypatch.setattr(forum_cli, "ENGRAM_HOME", str(_pack_cli_engram_home))
        monkeypatch.setattr(
            forum_cli, "READ_CURSOR_PATH",
            str(_pack_cli_engram_home / "forum-read-cursor.txt"),
        )
        yield
        forum_cli._FORUM_URL_CACHE = None

    def test_label_posts_form_fields_and_prints_ok(self):
        captured = {}

        def _fake_post_form(url, fields):
            captured["url"] = url
            captured["fields"] = fields
            return {
                "pack": {
                    "id": "agent-a-p-v1",
                    "share_clearance": "cleared-to-share",
                    "clearance_method": "full read (pack files + database)",
                }
            }

        stdout_buf = io.StringIO()
        with mock.patch.object(forum_cli, "_api_post_form", side_effect=_fake_post_form), \
             mock.patch("sys.argv", [
                 "forum", "pack", "label", "agent-a-p-v1", "cleared-to-share",
                 "--method", "full read (pack files + database)",
             ]), \
             mock.patch("sys.stdout", stdout_buf), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit as e:
                assert e.code in (0, None), f"unexpected exit code: {e.code}"

        assert captured.get("url", "").endswith(
            "/api/packs/agent-a-p-v1/label"
        ), f"unexpected label URL: {captured.get('url')}"
        assert captured.get("fields") == {
            "agent": "agent-a",
            "clearance": "cleared-to-share",
            "method": "full read (pack files + database)",
        }, f"unexpected fields: {captured.get('fields')}"

        out = stdout_buf.getvalue()
        assert "ok" in out
        assert "agent-a-p-v1" in out
        assert "cleared-to-share" in out

    def test_label_without_method_omits_method_field(self):
        captured = {}

        def _fake_post_form(url, fields):
            captured["fields"] = fields
            return {"pack": {"id": "agent-a-p-v1", "share_clearance": "private"}}

        with mock.patch.object(forum_cli, "_api_post_form", side_effect=_fake_post_form), \
             mock.patch("sys.argv", [
                 "forum", "pack", "label", "agent-a-p-v1", "private",
             ]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", io.StringIO()):
            try:
                forum_cli.main()
            except SystemExit as e:
                assert e.code in (0, None), f"unexpected exit code: {e.code}"

        assert "method" not in captured.get("fields", {})

    def test_label_rejects_invalid_clearance_via_argparse(self):
        stderr_buf = io.StringIO()
        with mock.patch("sys.argv", [
                 "forum", "pack", "label", "agent-a-p-v1", "bogus-state",
             ]), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("sys.stderr", stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                forum_cli.main()
        assert exc_info.value.code != 0
