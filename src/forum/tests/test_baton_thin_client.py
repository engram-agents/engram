"""Tests for baton.py Phase 3 thin-client migration.

All tests mock ForumClient — no live server needed.  The tests verify that:
  1. Each write cmd_* posts to the correct URL with the correct body fields.
  2. Each read cmd_* calls client.get with the correct path.
  3. ForumNetworkError → EXIT_IO (exit code 2).
  4. ForumHttpError 404 → EXIT_VALIDATION (exit code 1) for project-not-found paths.
  5. ForumHttpError 422/400 → EXIT_VALIDATION (exit code 1).
  6. ForumHttpError 5xx → EXIT_IO (exit code 2).
  7. Config-based multi-agent gate: reads → silent exit 0 in single mode;
     writes → EXIT_STATE + error message in single mode.

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

# Config for multi-agent mode (all write commands need this)
MULTI_CONFIG = {"mode": "multi"}
# Config for single-agent mode
SINGLE_CONFIG = {"mode": "single"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(
    post_return: Optional[Dict[str, Any]] = None,
    get_return: Optional[Dict[str, Any]] = None,
) -> MagicMock:
    """Return a mock ForumClient with configurable post/get return values."""
    client = MagicMock()
    client.post.return_value = post_return or {"seq": 42}
    if get_return is not None:
        client.get.return_value = get_return
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


def _make_raw_baton(project_id: str, **frontmatter) -> str:
    """Return a minimal baton file as a raw markdown string (no file I/O).

    This replaces the old _mock_baton_file helper that wrote to disk.
    The returned string is what the API's GET /api/projects/<pid>["raw"] field
    would return.
    """
    fm: Dict[str, Any] = {
        "project": project_id,
        "title": f"Test {project_id}",
        "status": "in-progress",
        "turn": "borges",
        "turn_since": "2026-06-26T12:00:00Z",
        "turn_reason": "start",
        "participants": "[borges, ariadne]",
    }
    fm.update(frontmatter)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", "## Turn log", "", "- 2026-06-26T12:00:00Z initialized", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# cmd_flip
# ---------------------------------------------------------------------------

class TestCmdFlip:
    @pytest.fixture(autouse=True)
    def _sentinel(self, monkeypatch):
        # cmd_flip's turn validation calls _is_pool_sentinel → _pool_sentinel,
        # which reads the ambient $ENGRAM_HOME/config.json (NOT the passed config
        # dict). Mock it so these tests don't depend on a primary_user being set
        # in the CI env.
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

    def test_posts_correct_url_and_body(self, monkeypatch):
        """cmd_flip calls client.get to read state, then client.post to flip."""
        raw = _make_raw_baton("PR-99")
        # Disable CI and approval guards (no gh on path in tests)
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 7},
            get_return={"raw": raw},
        )
        args = _ns(project_id="PR-99", to="ariadne", reason="fairy-converged")

        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-99/flip"
        assert body["to_agent"] == "ariadne"
        assert body["reason"] == "fairy-converged"
        assert "agent" in body

    def test_returns_ok_on_success(self, monkeypatch):
        """cmd_flip exits cleanly when client.post returns {seq: N}."""
        raw = _make_raw_baton("PR-99")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 7},
            get_return={"raw": raw},
        )
        args = _ns(project_id="PR-99", to="ariadne", reason="done")
        # Should not raise SystemExit
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

    def test_network_error_maps_to_exit_io(self, monkeypatch):
        """ForumNetworkError on post → sys.exit(EXIT_IO) = 2."""
        raw = _make_raw_baton("PR-99")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(get_return={"raw": raw})
        client.post.side_effect = ForumNetworkError("connection refused")
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_IO

    def test_http_404_on_read_maps_to_exit_validation(self, monkeypatch):
        """ForumHttpError 404 on GET → sys.exit(EXIT_VALIDATION) = 1 (project not found)."""
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION

    def test_http_404_on_post_maps_to_exit_state(self, monkeypatch):
        """ForumHttpError 404 on POST → sys.exit(EXIT_STATE) = 3 (race condition)."""
        raw = _make_raw_baton("PR-99")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(get_return={"raw": raw})
        client.post.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_http_422_maps_to_exit_validation(self, monkeypatch):
        """ForumHttpError 422 on POST → sys.exit(EXIT_VALIDATION) = 1."""
        raw = _make_raw_baton("PR-99")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(get_return={"raw": raw})
        client.post.side_effect = ForumHttpError(422, '{"error": "bad input"}')
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION

    def test_http_500_maps_to_exit_io(self, monkeypatch):
        """ForumHttpError 500 on POST → sys.exit(EXIT_IO) = 2."""
        raw = _make_raw_baton("PR-99")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(get_return={"raw": raw})
        client.post.side_effect = ForumHttpError(500, "server error")
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_IO


# ---------------------------------------------------------------------------
# cmd_claim
# ---------------------------------------------------------------------------

class TestCmdClaim:
    def test_posts_correct_body_with_pool_sentinel(self, monkeypatch):
        """cmd_claim sends pool_sentinel in body and posts to /claim route."""
        raw = _make_raw_baton("pool-myproject",
                              turn="lei",
                              participants="[borges, ariadne]")
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name in ("lei",))

        client = _make_client(
            post_return={"seq": 3},
            get_return={"raw": raw},
        )
        args = _ns(project_id="pool-myproject")

        _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/pool-myproject/claim"
        assert body["agent"] == "borges"
        assert body["pool_sentinel"] == "lei"

    def test_network_error_maps_to_exit_io(self, monkeypatch):
        raw = _make_raw_baton("pool-x", turn="lei", participants="[borges]")
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name == "lei")

        client = _make_client(get_return={"raw": raw})
        client.post.side_effect = ForumNetworkError("timeout")
        args = _ns(project_id="pool-x")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_IO


# ---------------------------------------------------------------------------
# cmd_release
# ---------------------------------------------------------------------------

class TestCmdRelease:
    def test_passes_done_true(self, monkeypatch):
        """cmd_release --done passes done=True in the POST body."""
        raw = _make_raw_baton("pool-z", turn="borges", participants="[borges, ariadne]")
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")

        client = _make_client(
            post_return={"seq": 5},
            get_return={"raw": raw},
        )
        args = _ns(project_id="pool-z", done=True, reason="all done")

        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/pool-z/release"
        assert body["done"] is True
        assert body["pool_sentinel"] == "lei"
        assert body["reason"] == "all done"

    def test_passes_done_false_by_default(self, monkeypatch):
        """cmd_release without --done passes done=False."""
        raw = _make_raw_baton("pool-z", turn="borges", participants="[borges, ariadne]")
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")

        client = _make_client(
            post_return={"seq": 5},
            get_return={"raw": raw},
        )
        args = _ns(project_id="pool-z", done=False, reason="releasing")

        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["done"] is False

    def test_http_404_maps_to_exit_state(self, monkeypatch):
        raw = _make_raw_baton("pool-z", turn="borges", participants="[borges]")
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")

        client = _make_client(get_return={"raw": raw})
        client.post.side_effect = ForumHttpError(404, "not found")
        args = _ns(project_id="pool-z", done=False, reason="releasing")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_release(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE


# ---------------------------------------------------------------------------
# cmd_close / cmd_reopen  (both route through /status)
# ---------------------------------------------------------------------------

class TestCmdCloseReopen:
    def test_close_dispatches_to_status_route_with_closed_status(self, monkeypatch):
        """cmd_close posts to /status with a CLOSED new_status."""
        raw = _make_raw_baton("PR-50")
        client = _make_client(
            post_return={"seq": 11},
            get_return={"raw": raw},
        )
        args = _ns(project_id="PR-50", status="merged")

        _baton.cmd_close(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-50/status"
        assert body["new_status"] == "merged"
        assert body["agent"] == "ariadne"

    def test_reopen_dispatches_to_status_route_with_active_status(self, monkeypatch):
        """cmd_reopen posts to /status with an ACTIVE new_status."""
        raw = _make_raw_baton("PR-50", status="merged")
        client = _make_client(
            post_return={"seq": 12},
            get_return={"raw": raw},
        )
        args = _ns(project_id="PR-50", status="in-progress")

        _baton.cmd_reopen(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-50/status"
        assert body["new_status"] == "in-progress"


# ---------------------------------------------------------------------------
# cmd_init
# ---------------------------------------------------------------------------

class TestCmdInit:
    def test_posts_to_api_projects_with_all_required_fields(self, monkeypatch):
        """cmd_init body includes every field the server needs to reconstruct frontmatter.

        Lesson-tripwire coverage: we verify ALL required fields are present, not just
        that the URL is correct, because a missing field produces a silent 400 and the
        baton is never created.
        """
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

        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

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

    def test_github_anchor_included_when_provided(self, monkeypatch):
        """cmd_init passes github anchor to API when --github is provided."""
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

        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/200"

    def test_409_conflict_maps_to_exit_validation(self, monkeypatch):
        """ForumHttpError 409 (project already exists) → EXIT_VALIDATION."""
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
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_called_once()  # verify API was reached, not client-side guard

    def test_network_error_maps_to_exit_io(self, monkeypatch):
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
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_IO


# ---------------------------------------------------------------------------
# cmd_rename
# ---------------------------------------------------------------------------

class TestCmdRename:
    def test_posts_new_title_to_rename_route(self, monkeypatch):
        raw = _make_raw_baton("PR-77")
        client = _make_client(
            post_return={"seq": 20},
            get_return={"raw": raw},
        )
        args = _ns(project_id="PR-77", title="Brand new title")

        _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-77/rename"
        assert body["new_title"] == "Brand new title"


# ---------------------------------------------------------------------------
# cmd_anchor
# ---------------------------------------------------------------------------

class TestCmdAnchor:
    def test_posts_github_anchor_to_anchor_route(self, monkeypatch):
        raw = _make_raw_baton("PR-88")
        client = _make_client(
            post_return={"seq": 25},
            get_return={"raw": raw},
        )
        args = _ns(project_id="PR-88", github="pr/88")

        _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-88/anchor"
        assert body["github"] == "pr/88"


# ---------------------------------------------------------------------------
# Read commands: status / mine / show
# ---------------------------------------------------------------------------

class TestReadCommands:
    """cmd_status, cmd_mine, cmd_show now use client.get instead of local files."""

    def test_status_calls_api_projects_list(self, monkeypatch):
        """cmd_status calls GET /api/projects?active_only=true."""
        projects = [
            {
                "project_id": "PR-10",
                "title": "Test PR",
                "status": "in-progress",
                "turn": "borges",
                "turn_since": "2026-06-26T10:00:00Z",
                "turn_reason": "fairy done",
                "participants": ["borges", "ariadne"],
                "seq": 1,
            }
        ]
        client = _make_client(get_return={"projects": projects})
        args = _ns(mine=False)

        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)

        client.get.assert_called_once()
        call_path = client.get.call_args[0][0]
        assert call_path == "/api/projects"
        call_params = client.get.call_args[1].get("params") or client.get.call_args[0][1] if len(client.get.call_args[0]) > 1 else client.get.call_args[1].get("params")
        # Verify active_only param was passed (either positionally or as kwarg)
        all_args = list(client.get.call_args[0]) + list((client.get.call_args[1] or {}).values())
        assert any("active_only" in str(a) for a in all_args)

    def test_mine_filters_by_turn(self, monkeypatch):
        """cmd_mine filters projects client-side to those where turn == agent_name."""
        projects = [
            {
                "project_id": "PR-10",
                "title": "Mine",
                "status": "in-progress",
                "turn": "borges",
                "turn_since": "2026-06-26T10:00:00Z",
                "turn_reason": "my turn",
                "participants": ["borges", "ariadne"],
                "seq": 1,
            },
            {
                "project_id": "PR-11",
                "title": "Not mine",
                "status": "in-progress",
                "turn": "ariadne",
                "turn_since": "2026-06-26T11:00:00Z",
                "turn_reason": "ariadne turn",
                "participants": ["borges", "ariadne"],
                "seq": 2,
            },
        ]
        client = _make_client(get_return={"projects": projects})
        args = _ns(mine=True)

        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            _baton.cmd_mine(args, MULTI_CONFIG, "borges", client)

        output = out.getvalue()
        assert "PR-10" in output
        assert "PR-11" not in output  # ariadne's, not borges'

    def test_show_calls_api_get_raw(self, monkeypatch):
        """cmd_show calls GET /api/projects/<pid> and prints the raw markdown."""
        raw = _make_raw_baton("PR-42")
        client = _make_client(get_return={"raw": raw})
        args = _ns(project_id="PR-42")

        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            _baton.cmd_show(args, MULTI_CONFIG, "borges", client)

        assert "PR-42" in out.getvalue()
        client.get.assert_called_once_with("/api/projects/PR-42")

    def test_status_single_agent_silent_exit(self):
        """cmd_status exits 0 silently in single-agent mode (no API call made)."""
        client = _make_client()
        args = _ns(mine=False)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_status(args, SINGLE_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_OK
        client.get.assert_not_called()

    def test_mine_single_agent_silent_exit(self):
        """cmd_mine exits 0 silently in single-agent mode."""
        client = _make_client()
        args = _ns(mine=True)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_mine(args, SINGLE_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_OK
        client.get.assert_not_called()

    def test_show_single_agent_silent_exit(self):
        """cmd_show exits 0 silently in single-agent mode."""
        client = _make_client()
        args = _ns(project_id="PR-42")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_show(args, SINGLE_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_OK
        client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-agent gate (config-based)
# ---------------------------------------------------------------------------

class TestMultiAgentGate:
    """Write commands exit EXIT_STATE in single-agent mode with an error message."""

    def test_flip_single_agent_exits_state(self, capsys):
        client = _make_client()
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, SINGLE_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "single-agent mode" in captured.err
        client.get.assert_not_called()
        client.post.assert_not_called()

    def test_init_single_agent_exits_state(self, capsys, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "lei")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda name: name == "lei")
        client = _make_client()
        args = _ns(project_id="PR-101", title="t", participants="borges", turn="borges")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, SINGLE_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "single-agent mode" in captured.err

    def test_claim_single_agent_exits_state(self, capsys):
        client = _make_client()
        args = _ns(project_id="pool-x")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, SINGLE_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_close_single_agent_exits_state(self, capsys):
        client = _make_client()
        args = _ns(project_id="PR-50", status="merged")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_close(args, SINGLE_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_is_multi_agent_mode_reads_config(self):
        """_is_multi_agent_mode reads config.mode, not filesystem presence."""
        assert _baton._is_multi_agent_mode({"mode": "multi"}) is True
        assert _baton._is_multi_agent_mode({"mode": "single"}) is False
        assert _baton._is_multi_agent_mode({}) is False  # default = single


# ---------------------------------------------------------------------------
# Loud-fail: network error on GET
# ---------------------------------------------------------------------------

class TestLoudFail:
    """_api_get_raw fails LOUD (stderr + non-zero exit) when UCS is unreachable."""

    def test_flip_get_network_error_exits_io_with_message(self, capsys, monkeypatch):
        """cmd_flip GET unreachable → EXIT_IO + 'UCS unreachable' on stderr."""
        client = _make_client()
        client.get.side_effect = ForumNetworkError("connection refused")
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "UCS unreachable" in captured.err

    def test_status_get_network_error_exits_io(self, capsys):
        """cmd_status GET unreachable → EXIT_IO."""
        client = _make_client()
        client.get.side_effect = ForumNetworkError("refused")
        args = _ns(mine=False)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "UCS unreachable" in captured.err

    def test_show_get_network_error_exits_io(self, capsys):
        """cmd_show GET unreachable → EXIT_IO + 'UCS unreachable' on stderr."""
        client = _make_client()
        client.get.side_effect = ForumNetworkError("refused")
        args = _ns(project_id="PR-42")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "UCS unreachable" in captured.err


# ---------------------------------------------------------------------------
# 404 path: project not found
# ---------------------------------------------------------------------------

class TestNotFound:
    """_api_get_raw maps 404 to EXIT_VALIDATION with 'project not found' message."""

    def test_flip_404_prints_not_found(self, capsys, monkeypatch):
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = _ns(project_id="PR-99", to="ariadne", reason="done")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "project not found" in captured.err.lower() or "not found" in captured.err

    def test_show_404_prints_not_found(self, capsys):
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = _ns(project_id="PR-99")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_rename_404_on_get_exits_validation(self, capsys):
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = _ns(project_id="PR-77", title="New title")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION


# ---------------------------------------------------------------------------
# cmd_gc — enumerates active PR-batons via the API list, closes MERGED/CLOSED
# ones via POST /gc. (Coverage added in review of the pure-API conversion.)
# ---------------------------------------------------------------------------

class TestCmdGc:
    def _gh_state(self, state: str):
        """A fake subprocess.run result for `gh pr view ... -q .state`."""
        return MagicMock(returncode=0, stdout=f"{state}\n", stderr="")

    def test_closes_merged_pr_baton(self, monkeypatch):
        """A PR-baton whose gh state is MERGED → POST /api/projects/<pid>/gc (merged).

        #1715: cmd_gc reads the PR number (and repo) from the stored
        `github` anchor, not by re-deriving it from project_id -- a baton
        with no anchor is skipped (see TestGcRepoAware in tests/test_baton.py),
        so this fixture must carry one to remain gc-eligible.
        """
        client = _make_client(
            get_return={"projects": [{"project_id": "PR-500", "github": "pr/500"}]},
            post_return={"seq": 9},
        )
        monkeypatch.setattr(_baton.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(_baton.subprocess, "run", lambda *a, **k: self._gh_state("MERGED"))
        args = _ns(dry_run=False, limit=30)

        _baton.cmd_gc(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-500/gc"
        assert body["new_status"] == "merged"

    def test_dry_run_does_not_post(self, monkeypatch):
        """--dry-run previews without writing."""
        client = _make_client(get_return={"projects": [{"project_id": "PR-501", "github": "pr/501"}]})
        monkeypatch.setattr(_baton.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(_baton.subprocess, "run", lambda *a, **k: self._gh_state("MERGED"))
        args = _ns(dry_run=True, limit=30)

        _baton.cmd_gc(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_not_called()

    def test_network_error_on_list_exits_io(self, monkeypatch):
        """The /api/projects list call is unreachable → loud fail (EXIT_IO)."""
        client = _make_client()
        client.get.side_effect = ForumNetworkError("connection refused")
        monkeypatch.setattr(_baton.shutil, "which", lambda name: "/usr/bin/gh")
        args = _ns(dry_run=False, limit=30)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_gc(args, MULTI_CONFIG, "ariadne", client)
        assert exc_info.value.code == EXIT_IO
