"""Tests for baton.py Phase 3 thin-client migration.

All tests mock ForumClient — no live server needed.  The tests verify that:
  1. Each write cmd_* posts to the correct URL with the correct body fields.
  2. ForumNetworkError → EXIT_IO (exit code 2).
  3. ForumHttpError 404 → EXIT_STATE (exit code 3).
  4. ForumHttpError 422/400 → EXIT_VALIDATION (exit code 1).
  5. ForumHttpError 5xx → EXIT_IO (exit code 2).

Import strategy: baton.py lives in tools/ (symlinked from src/engram/tools/).
We add the worktree-relative path to sys.path before importing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call
import pytest

# ---------------------------------------------------------------------------
# Path bootstrap: tools/ contains baton.py + forum_api.py
# ---------------------------------------------------------------------------
_TOOLS_DIR = Path(__file__).resolve().parents[3] / "src" / "engram" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import baton as _baton
from forum_api import ForumHttpError, ForumNetworkError

# baton.py exit codes
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(return_value: Optional[Dict[str, Any]] = None) -> MagicMock:
    """Return a mock ForumClient whose post() returns the given dict."""
    client = MagicMock()
    client.post.return_value = return_value or {"seq": 42}
    return client


def _ns(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace with the given kwargs, plus sensible defaults."""
    defaults: Dict[str, Any] = {
        "force": False,
        "dry_run": False,
        "done": False,
        "reason": "test reason",
        "status": "in-progress",
        "mine": False,
        "github": None,
        "colleague": None,
        "limit": 30,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _mock_baton_file(tmp_path: Path, project_id: str, **frontmatter) -> Path:
    """Write a minimal baton file to tmp_path and return its path."""
    fm: Dict[str, Any] = {
        "project": project_id,
        "title": f"Test {project_id}",
        "status": "in-progress",
        "turn": "borges",
        "turn_since": "2026-06-26T12:00:00Z",
        "turn_reason": '"start"',
        "participants": "[borges, ariadne]",
    }
    fm.update(frontmatter)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", "## Turn log", "", "- 2026-06-26T12:00:00Z initialized", ""]
    dest = tmp_path / f"{project_id}.md"
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# cmd_flip
# ---------------------------------------------------------------------------

class TestCmdFlip:
    def test_posts_correct_url_and_body(self, tmp_path, monkeypatch):
        """cmd_flip calls client.post with the correct URL and body fields."""
        _mock_baton_file(tmp_path, "PR-99")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        # Disable CI and approval guards (no gh on path in tests)
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 7})
        args = _ns(project_id="PR-99", to="ariadne", reason="fairy-converged")

        _baton.cmd_flip(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-99/flip"
        assert body["to_agent"] == "ariadne"
        assert body["reason"] == "fairy-converged"
        assert "agent" in body

    def test_returns_ok_on_201(self, tmp_path, monkeypatch):
        """cmd_flip exits cleanly when client.post returns {seq: N}."""
        _mock_baton_file(tmp_path, "PR-99")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 7})
        args = _ns(project_id="PR-99", to="ariadne", reason="done")
        # Should not raise SystemExit
        _baton.cmd_flip(args, {}, "borges", client)

    def test_network_error_maps_to_exit_io(self, tmp_path, monkeypatch):
        """ForumNetworkError → sys.exit(EXIT_IO) = 2."""
        _mock_baton_file(tmp_path, "PR-99")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client()
        client.post.side_effect = ForumNetworkError("connection refused")
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_IO

    def test_http_404_maps_to_exit_state(self, tmp_path, monkeypatch):
        """ForumHttpError 404 → sys.exit(EXIT_STATE) = 3."""
        _mock_baton_file(tmp_path, "PR-99")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client()
        client.post.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_http_422_maps_to_exit_validation(self, tmp_path, monkeypatch):
        """ForumHttpError 422 → sys.exit(EXIT_VALIDATION) = 1."""
        _mock_baton_file(tmp_path, "PR-99")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client()
        client.post.side_effect = ForumHttpError(422, '{"error": "bad input"}')
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION

    def test_http_500_maps_to_exit_io(self, tmp_path, monkeypatch):
        """ForumHttpError 500 → sys.exit(EXIT_IO) = 2."""
        _mock_baton_file(tmp_path, "PR-99")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client()
        client.post.side_effect = ForumHttpError(500, "server error")
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_IO


# ---------------------------------------------------------------------------
# cmd_claim
# ---------------------------------------------------------------------------

class TestCmdClaim:
    def test_posts_correct_body_with_pool_sentinel(self, tmp_path, monkeypatch):
        """cmd_claim sends pool_sentinel in body and posts to /claim route."""
        _mock_baton_file(tmp_path, "pool-myproject",
                         turn="lei",  # pool sentinel holds it
                         participants="[borges, ariadne]")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name in ("lei",))

        client = _make_client({"seq": 3})
        args = _ns(project_id="pool-myproject")

        _baton.cmd_claim(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/pool-myproject/claim"
        assert body["agent"] == "borges"
        assert body["pool_sentinel"] == "lei"

    def test_network_error_maps_to_exit_io(self, tmp_path, monkeypatch):
        _mock_baton_file(tmp_path, "pool-x", turn="lei", participants="[borges]")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name == "lei")

        client = _make_client()
        client.post.side_effect = ForumNetworkError("timeout")
        args = _ns(project_id="pool-x")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_IO


# ---------------------------------------------------------------------------
# cmd_release
# ---------------------------------------------------------------------------

class TestCmdRelease:
    def test_passes_done_true(self, tmp_path, monkeypatch):
        """cmd_release --done passes done=True in the POST body."""
        _mock_baton_file(tmp_path, "pool-z",
                         turn="borges",
                         participants="[borges, ariadne]")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")

        client = _make_client({"seq": 5})
        args = _ns(project_id="pool-z", done=True, reason="all done")

        _baton.cmd_release(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/pool-z/release"
        assert body["done"] is True
        assert body["pool_sentinel"] == "lei"
        assert body["reason"] == "all done"

    def test_passes_done_false_by_default(self, tmp_path, monkeypatch):
        """cmd_release without --done passes done=False."""
        _mock_baton_file(tmp_path, "pool-z",
                         turn="borges",
                         participants="[borges, ariadne]")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")

        client = _make_client({"seq": 5})
        args = _ns(project_id="pool-z", done=False, reason="releasing")

        _baton.cmd_release(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["done"] is False

    def test_http_404_maps_to_exit_state(self, tmp_path, monkeypatch):
        _mock_baton_file(tmp_path, "pool-z", turn="borges", participants="[borges]")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")

        client = _make_client()
        client.post.side_effect = ForumHttpError(404, "not found")
        args = _ns(project_id="pool-z", done=False, reason="releasing")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_release(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_STATE


# ---------------------------------------------------------------------------
# cmd_close / cmd_reopen  (both route through /status)
# ---------------------------------------------------------------------------

class TestCmdStatus:
    def test_close_dispatches_to_status_route_with_closed_status(self, tmp_path, monkeypatch):
        """cmd_close posts to /status with a CLOSED new_status."""
        _mock_baton_file(tmp_path, "PR-50")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))

        client = _make_client({"seq": 11})
        args = _ns(project_id="PR-50", status="merged")

        _baton.cmd_close(args, {}, "ariadne", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-50/status"
        assert body["new_status"] == "merged"
        assert body["agent"] == "ariadne"

    def test_reopen_dispatches_to_status_route_with_active_status(self, tmp_path, monkeypatch):
        """cmd_reopen posts to /status with an ACTIVE new_status."""
        _mock_baton_file(tmp_path, "PR-50", status="merged")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))

        client = _make_client({"seq": 12})
        args = _ns(project_id="PR-50", status="in-progress")

        _baton.cmd_reopen(args, {}, "ariadne", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-50/status"
        assert body["new_status"] == "in-progress"


# ---------------------------------------------------------------------------
# cmd_init
# ---------------------------------------------------------------------------

class TestCmdInit:
    def test_posts_to_api_projects_with_all_required_fields(self, tmp_path, monkeypatch):
        """cmd_init body includes every field the server needs to reconstruct frontmatter.

        Lesson-tripwire coverage: we verify ALL required fields are present, not just
        that the URL is correct, because a missing field produces a silent 400 and the
        baton is never created.
        """
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name == "lei")

        client = _make_client({"seq": 1, "project_id": "PR-101"})
        args = _ns(
            project_id="PR-101",
            title="Test PR one-oh-one",
            participants="borges,ariadne",
            status="in-progress",
            turn="borges",
        )

        _baton.cmd_init(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects"
        # Every field the server validates as required
        assert body["project_id"] == "PR-101"
        assert body["title"] == "Test PR one-oh-one"
        assert body["status"] == "in-progress"
        assert body["turn"] == "borges"
        assert body["turn_reason"]  # non-empty
        assert body["participants"] == ["borges", "ariadne"]
        assert "agent" in body

    def test_github_anchor_included_when_provided(self, tmp_path, monkeypatch):
        """cmd_init passes github anchor to API when --github is provided."""
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name == "lei")

        client = _make_client({"seq": 2})
        args = _ns(
            project_id="PR-200",
            title="With anchor",
            participants="borges",
            status="planning",
            turn="borges",
            github="pr/200",
        )

        _baton.cmd_init(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/200"

    def test_409_conflict_maps_to_exit_validation(self, tmp_path, monkeypatch):
        """ForumHttpError 409 (project already exists) → EXIT_VALIDATION."""
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name == "lei")

        client = _make_client()
        client.post.side_effect = ForumHttpError(409, '{"error": "already exists"}')
        args = _ns(
            project_id="PR-101",
            title="dup",
            participants="borges",
            status="planning",
            turn="borges",
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_called_once()  # verify API was reached, not client-side guard

    def test_network_error_maps_to_exit_io(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name == "lei")

        client = _make_client()
        client.post.side_effect = ForumNetworkError("refused")
        args = _ns(
            project_id="PR-300",
            title="unreachable",
            participants="borges",
            status="planning",
            turn="borges",
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_IO


# ---------------------------------------------------------------------------
# cmd_rename
# ---------------------------------------------------------------------------

class TestCmdRename:
    def test_posts_new_title_to_rename_route(self, tmp_path, monkeypatch):
        _mock_baton_file(tmp_path, "PR-77")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))

        client = _make_client({"seq": 20})
        args = _ns(project_id="PR-77", title="Brand new title")

        _baton.cmd_rename(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-77/rename"
        assert body["new_title"] == "Brand new title"


# ---------------------------------------------------------------------------
# cmd_anchor
# ---------------------------------------------------------------------------

class TestCmdAnchor:
    def test_posts_github_anchor_to_anchor_route(self, tmp_path, monkeypatch):
        _mock_baton_file(tmp_path, "PR-88")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(tmp_path))

        client = _make_client({"seq": 25})
        args = _ns(project_id="PR-88", github="pr/88")

        _baton.cmd_anchor(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-88/anchor"
        assert body["github"] == "pr/88"
