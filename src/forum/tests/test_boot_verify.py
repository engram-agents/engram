"""Tests for forum boot-verify probe (part of #868 slice B — A9 fix).

Covers:
- probe_deps() pass-path (all deps present)
- Each hard dep failure → probe result "missing" with diagnostic key
- _run_boot_verify() exits 2 on hard-dep failure
- Soft dep absent → starts + "degraded" in /health
- --verify-only exits 0 (all ok) or 2 (hard dep missing)
- /health additive-compat (existing keys still present with new "deps" key)

Environment note: run with the forum venv at
    /home/agents-shared/forum/.venv/bin/python
if it exists, else use the current interpreter and note any bs4-version
sensitivity (known env-skew: the plugin venv fails some forum tests on
beautifulsoup4 differences — do NOT chase those failures; run only this
test file + test_endpoints.py as canary).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on path (mirrors pattern in existing forum tests).
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent.parent.parent  # src/forum/tests/ → repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from forum import server as _server
from forum.db import init_db
from forum.server import _run_boot_verify, create_app, probe_deps


# ---------------------------------------------------------------------------
# Helper: minimal app fixture for /health tests
# ---------------------------------------------------------------------------

@pytest.fixture
def app_with_deps(tmp_path):
    """App fixture that injects a dep_results dict (as main() would)."""
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    dep_results = {
        "db": "ok",
        "audit_log": "ok",
        "engram_pkg": "ok",
        "embeddings": "ok",
    }
    app = create_app(db_path, audit_path, dep_results=dep_results)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def app_degraded(tmp_path):
    """App fixture with embeddings degraded."""
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    dep_results = {
        "db": "ok",
        "audit_log": "ok",
        "engram_pkg": "ok",
        "embeddings": "degraded",
    }
    app = create_app(db_path, audit_path, dep_results=dep_results)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def app_no_deps(tmp_path):
    """App fixture with no dep_results (simulates direct test call, old callers)."""
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, audit_path)
    app.config["TESTING"] = True
    return app


# ---------------------------------------------------------------------------
# probe_deps() — pass-path
# ---------------------------------------------------------------------------

class TestProbeDepsPassPath:
    """All deps present → all statuses "ok" or "degraded" (soft)."""

    def test_db_ok(self, tmp_path):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        # Pre-create the DB so PRAGMA user_version works.
        sqlite3.connect(db_path).close()
        results = probe_deps(db_path, audit_path)
        assert results["db"] == "ok", f"expected db:ok, got: {results}"

    def test_audit_log_ok(self, tmp_path):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        sqlite3.connect(db_path).close()
        results = probe_deps(db_path, audit_path)
        assert results["audit_log"] == "ok", f"expected audit_log:ok, got: {results}"

    def test_engram_pkg_status_reported(self, tmp_path):
        """engram-pkg is either ok or missing; either way the key is present."""
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        sqlite3.connect(db_path).close()
        results = probe_deps(db_path, audit_path)
        assert results.get("engram_pkg") in ("ok", "missing"), (
            f"engram_pkg status must be 'ok' or 'missing', got: {results.get('engram_pkg')}"
        )

    def test_embeddings_status_reported(self, tmp_path):
        """embeddings is either ok or degraded; either way the key is present."""
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        sqlite3.connect(db_path).close()
        results = probe_deps(db_path, audit_path)
        assert results.get("embeddings") in ("ok", "degraded"), (
            f"embeddings status must be 'ok' or 'degraded', got: {results.get('embeddings')}"
        )

    def test_no_private_keys_in_ok_path(self, tmp_path):
        """When db + audit are ok, no private _error keys should appear."""
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        sqlite3.connect(db_path).close()
        results = probe_deps(db_path, audit_path)
        private_keys = [k for k in results if k.startswith("_") and "error" in k]
        assert not private_keys, f"Unexpected error keys in passing results: {private_keys}"


# ---------------------------------------------------------------------------
# probe_deps() — hard dep failures
# ---------------------------------------------------------------------------

class TestProbeDepsDBFailure:
    """DB probe failure cases."""

    def test_missing_db_parent_dir(self, tmp_path):
        """Unwritable parent dir → db: missing."""
        no_such = tmp_path / "nonexistent_dir" / "forum.db"
        audit_path = str(tmp_path / "audit.jsonl")
        # Mark tmp_path unwritable to prevent os.makedirs from creating the subdir.
        # Use a path whose parent definitely doesn't exist.
        results = probe_deps(str(no_such), audit_path)
        assert results["db"] == "missing", f"Expected db:missing for non-existent parent, got: {results}"
        assert "_db_error" in results, "Expected _db_error diagnostic key"

    def test_unwritable_db_dir(self, tmp_path):
        """Chmod 000 on parent dir → db: missing (dir not writable)."""
        db_dir = tmp_path / "locked"
        db_dir.mkdir()
        db_path = str(db_dir / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        try:
            db_dir.chmod(0o000)
            results = probe_deps(db_path, audit_path)
            assert results["db"] == "missing", (
                f"Expected db:missing for locked dir, got: {results}"
            )
        finally:
            db_dir.chmod(0o755)  # restore so tmp cleanup works


class TestProbeDepsAuditLogFailure:
    """Audit-log probe failure cases."""

    def test_unwritable_audit_dir(self, tmp_path):
        """Chmod 000 on audit dir → audit_log: missing."""
        db_path = str(tmp_path / "forum.db")
        sqlite3.connect(db_path).close()
        locked_dir = tmp_path / "locked_audit"
        locked_dir.mkdir()
        audit_path = str(locked_dir / "audit.jsonl")
        try:
            locked_dir.chmod(0o000)
            results = probe_deps(db_path, audit_path)
            assert results["audit_log"] == "missing", (
                f"Expected audit_log:missing for locked dir, got: {results}"
            )
            assert "_audit_log_error" in results, "Expected _audit_log_error diagnostic key"
        finally:
            locked_dir.chmod(0o755)


class TestProbeDepsEngramPkgFailure:
    """engram-pkg probe failure cases.

    probe_deps() delegates to packs_mod._engram_pkg_cli_path() (layout-agnostic
    upward search) so we monkeypatch that function directly — no __file__ tricks.
    """

    def test_missing_engram_pkg(self, tmp_path, monkeypatch):
        """_engram_pkg_cli_path returns a nonexistent path → engram_pkg: missing."""
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        sqlite3.connect(db_path).close()

        fake_cli = tmp_path / "tools" / "engram-pkg" / "engram-pkg"
        # NOT created — exists() returns False.
        monkeypatch.setattr(
            _server.packs_mod, "_engram_pkg_cli_path", lambda: fake_cli
        )

        results = probe_deps(db_path, audit_path)
        assert results["engram_pkg"] == "missing", (
            f"Expected engram_pkg:missing when CLI not found, got: {results}"
        )
        assert "_engram_pkg_path" in results, "Expected _engram_pkg_path diagnostic key"

    def test_present_engram_pkg(self, tmp_path, monkeypatch):
        """_engram_pkg_cli_path returns an existing file → engram_pkg: ok."""
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        sqlite3.connect(db_path).close()

        fake_cli = tmp_path / "tools" / "engram-pkg" / "engram-pkg"
        fake_cli.parent.mkdir(parents=True)
        fake_cli.write_text("# stub\n")
        monkeypatch.setattr(
            _server.packs_mod, "_engram_pkg_cli_path", lambda: fake_cli
        )

        results = probe_deps(db_path, audit_path)
        assert results["engram_pkg"] == "ok", (
            f"Expected engram_pkg:ok when CLI present, got: {results}"
        )


# ---------------------------------------------------------------------------
# _run_boot_verify() — exits 2 on hard-dep failure
# ---------------------------------------------------------------------------

class TestRunBootVerifyHardFail:
    """_run_boot_verify exits 2 when any hard dep is missing."""

    def _make_probe_results(self, **overrides):
        base = {"db": "ok", "audit_log": "ok", "engram_pkg": "ok", "embeddings": "ok"}
        base.update(overrides)
        return base

    def test_db_missing_exits_2(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        bad_results = self._make_probe_results(db="missing", _db_error="test error")
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: bad_results)
        with pytest.raises(SystemExit) as exc_info:
            _run_boot_verify(db_path, audit_path)
        assert exc_info.value.code == 2

    def test_audit_log_missing_exits_2(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        bad_results = self._make_probe_results(
            audit_log="missing", _audit_log_error="test error"
        )
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: bad_results)
        with pytest.raises(SystemExit) as exc_info:
            _run_boot_verify(db_path, audit_path)
        assert exc_info.value.code == 2

    def test_engram_pkg_missing_exits_2(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        bad_results = self._make_probe_results(
            engram_pkg="missing", _engram_pkg_path="/no/such/path"
        )
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: bad_results)
        with pytest.raises(SystemExit) as exc_info:
            _run_boot_verify(db_path, audit_path)
        assert exc_info.value.code == 2

    def test_hard_fail_prints_stderr_message(self, tmp_path, monkeypatch, capsys):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        bad_results = self._make_probe_results(
            engram_pkg="missing", _engram_pkg_path="/no/such/path"
        )
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: bad_results)
        with pytest.raises(SystemExit):
            _run_boot_verify(db_path, audit_path)
        captured = capsys.readouterr()
        assert "FATAL" in captured.err, (
            f"Expected FATAL message in stderr, got: {captured.err!r}"
        )

    def test_all_ok_returns_dict(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        ok_results = self._make_probe_results()
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: ok_results)
        result = _run_boot_verify(db_path, audit_path)
        assert result == ok_results


# ---------------------------------------------------------------------------
# Soft dep — embeddings degraded: server starts, /health shows "degraded"
# ---------------------------------------------------------------------------

class TestSoftDepDegraded:
    """Soft dep absent → server starts; /health reflects degraded status."""

    def test_server_starts_with_degraded_embeddings(self, app_degraded):
        """App with degraded embeddings is still creatable (no SystemExit)."""
        assert app_degraded is not None

    def test_health_shows_embeddings_degraded(self, app_degraded):
        client = app_degraded.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get("deps", {}).get("embeddings") == "degraded", (
            f"Expected embeddings:degraded in /health deps, got: {data}"
        )

    def test_health_overall_degraded_when_soft_dep_degraded(self, app_degraded):
        client = app_degraded.test_client()
        resp = client.get("/health")
        data = json.loads(resp.data)
        assert data.get("status") == "degraded", (
            f"Expected status:degraded, got: {data}"
        )

    def test_degraded_banner_printed_to_stderr(self, tmp_path, monkeypatch, capsys):
        """_run_boot_verify prints DEGRADED banner when embeddings is soft-missing."""
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        soft_results = {
            "db": "ok",
            "audit_log": "ok",
            "engram_pkg": "ok",
            "embeddings": "degraded",
        }
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: soft_results)
        result = _run_boot_verify(db_path, audit_path)
        captured = capsys.readouterr()
        assert "DEGRADED" in captured.err, (
            f"Expected DEGRADED banner in stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# --verify-only: exits 0 (all ok) or 2 (hard dep missing)
# ---------------------------------------------------------------------------

class TestVerifyOnly:
    """--verify-only: runs probes, prints report, exits without binding port."""

    def _all_ok_results(self):
        return {
            "db": "ok",
            "audit_log": "ok",
            "engram_pkg": "ok",
            "embeddings": "ok",
        }

    def _hard_fail_results(self):
        return {
            "db": "ok",
            "audit_log": "ok",
            "engram_pkg": "missing",
            "_engram_pkg_path": "/no/such/path",
            "embeddings": "ok",
        }

    def test_verify_only_exits_0_on_all_ok(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: self._all_ok_results())
        with pytest.raises(SystemExit) as exc_info:
            _run_boot_verify(db_path, audit_path, verify_only=True)
        assert exc_info.value.code == 0, (
            f"Expected exit 0 on all-ok verify-only, got: {exc_info.value.code}"
        )

    def test_verify_only_exits_2_on_hard_fail(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: self._hard_fail_results())
        with pytest.raises(SystemExit) as exc_info:
            _run_boot_verify(db_path, audit_path, verify_only=True)
        assert exc_info.value.code == 2, (
            f"Expected exit 2 on hard-fail verify-only, got: {exc_info.value.code}"
        )

    def test_verify_only_prints_dep_status_to_stderr(self, tmp_path, monkeypatch, capsys):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: self._all_ok_results())
        with pytest.raises(SystemExit):
            _run_boot_verify(db_path, audit_path, verify_only=True)
        captured = capsys.readouterr()
        # Should print each dep status line.
        assert "engram_pkg" in captured.err, (
            f"Expected dep status lines in stderr, got: {captured.err!r}"
        )
        assert "db" in captured.err

    def test_verify_only_hard_fail_prints_failed_message(self, tmp_path, monkeypatch, capsys):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        monkeypatch.setattr(_server, "probe_deps", lambda *a, **kw: self._hard_fail_results())
        with pytest.raises(SystemExit):
            _run_boot_verify(db_path, audit_path, verify_only=True)
        captured = capsys.readouterr()
        assert "FAILED" in captured.err, (
            f"Expected FAILED message in stderr on hard fail, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# /health — additive compatibility (existing keys still present)
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """/health is additive: new "deps" field coexists with existing shape."""

    def test_health_returns_200(self, app_with_deps):
        client = app_with_deps.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_has_status_key(self, app_with_deps):
        client = app_with_deps.test_client()
        resp = client.get("/health")
        data = json.loads(resp.data)
        assert "status" in data, f"Expected 'status' key in /health response: {data}"

    def test_health_has_deps_key_when_results_injected(self, app_with_deps):
        client = app_with_deps.test_client()
        resp = client.get("/health")
        data = json.loads(resp.data)
        assert "deps" in data, f"Expected 'deps' key in /health response: {data}"

    def test_health_deps_contains_all_probed_deps(self, app_with_deps):
        client = app_with_deps.test_client()
        resp = client.get("/health")
        data = json.loads(resp.data)
        deps = data.get("deps", {})
        for expected_dep in ("db", "audit_log", "engram_pkg", "embeddings"):
            assert expected_dep in deps, (
                f"Expected dep '{expected_dep}' in /health deps, got: {deps}"
            )

    def test_health_no_private_keys_in_deps(self, app_with_deps):
        client = app_with_deps.test_client()
        resp = client.get("/health")
        data = json.loads(resp.data)
        deps = data.get("deps", {})
        private = [k for k in deps if k.startswith("_")]
        assert not private, f"Private keys must not appear in /health deps: {private}"

    def test_health_ok_when_all_deps_ok(self, app_with_deps):
        client = app_with_deps.test_client()
        resp = client.get("/health")
        data = json.loads(resp.data)
        assert data["status"] == "ok", (
            f"Expected status:ok when all deps ok, got: {data}"
        )

    def test_health_no_deps_key_without_injection(self, app_no_deps):
        """Backwards-compat: no dep_results → no 'deps' key in /health."""
        client = app_no_deps.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # status still present
        assert "status" in data
        # deps absent (old callers unaffected)
        assert "deps" not in data, (
            f"Expected no 'deps' key when no dep_results injected, got: {data}"
        )
