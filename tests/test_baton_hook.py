"""Tests for hooks/claude/engram-baton-prompt-hook.py (pure-API shape).

The hook was converted from local-filesystem reads to pure forum-API reads
(UCS pure-API invariant). Tests are restructured accordingly:

- No BATON_PROJECTS_DIR / _write_project / _run_hook subprocess scaffolding.
- Unit tests call _my_batons() and _auto_archive_merged_pr_batons() as pure
  functions over project-dict lists passed directly.
- Integration tests monkeypatch mod._fetch_active_projects to control what
  the hook "sees" from the forum, and capture stdout/stderr via
  redirect_stdout / redirect_stderr.

Behaviors covered:

  Unit -- _my_batons(projects, agent_name):
    1.  Turn filter: borges sees only his batons.
    2.  Status filter: merged/cancelled excluded.
    3.  Oldest-first sort.
    4.  github_anchor resolved from PR-NNN project_id.
    5.  github_anchor is None for non-PR-NNN ids.
    6.  Empty project list returns empty result.
    7.  Empty agent_name returns empty result.
    8.  turn_since is parsed to a datetime object.

  Integration -- main() rendering:
    9.  Single-agent guard (mode='single') -> silent exit 0, no output.
    10. forum_api gate (_FORUM_API_OK=False) -> silent exit 0, no output.
    11. Empty project list -> no output.
    12. No batons in court -> no output.
    13. One baton in court -> additionalContext with project ID and 'baton'.
    14. Multiple batons -> all listed, oldest-first, count in header.
    15. Other-agent baton not surfaced.
    16. Merged project not surfaced (in-court render).
    17. Cancelled project not surfaced.
    18. turn_reason text appears in output.
    19. JSON output format (hookSpecificOutput.additionalContext).
    20. Mixed state: only my active batons surface.
    21. Age formatting in output ('ago').
    22. Decision-prompt block ('For EACH above') present.
    23. ForumNetworkError -> 'UCS unreachable' banner on stdout.
    24. ForumHttpError -> 'UCS unreachable' banner on stdout.
    25. GitHub live-status clause appears for PR-anchored baton with cached status.

  Unit -- _auto_archive_merged_pr_batons(projects, cache):
    26. MERGED in cache -> baton close --status merged.
    27. CLOSED in cache -> baton close --status cancelled.
    28. OPEN in cache -> no baton close.
    29. Uncached anchor -> no baton close (cache-only guard).
    30. status=merged -> no close again (idempotency, merged).
    31. status=cancelled -> no close again (idempotency, cancelled).
    32. Non-PR-NNN project_id -> not archived.
    33. Baton binary missing -> stderr skip line, no exception.
    34. subprocess raises -> no crash, stderr log written.

  Integration -- auto-archive via main():
    35. PR-baton at turn=maintainer (not in borges's court) archived via second pass.
    36. gh unavailable -> auto-archive skipped entirely.
    37. Zero in-court batons + merged baton -> archive runs, stdout empty.
    38. Non-zero baton close returncode -> stderr log; stdout empty.
    39. baton close raises inside main() -> no crash; in-court baton still rendered.
"""

import importlib
import importlib.util
import io
import json
import os
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "engram-baton-prompt-hook.py"
)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _make_project_dict(
    project_id: str,
    title: str = "Test project",
    status: str = "in-review",
    turn: str = "borges",
    turn_since: "datetime | None" = None,
    turn_reason: str = "colleague-review request",
    participants: "list | None" = None,
    seq: int = 1,
) -> dict:
    """Build a project dict matching the shape returned by GET /api/projects."""
    if turn_since is None:
        turn_since = datetime.now(timezone.utc) - timedelta(minutes=30)
    return {
        "project_id": project_id,
        "title": title,
        "status": status,
        "turn": turn,
        "turn_since": turn_since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "turn_reason": turn_reason,
        "participants": participants or ["borges", "ariadne"],
        "seq": seq,
    }


def _ts(offset_minutes: int) -> datetime:
    """Return a UTC datetime offset by given minutes from now."""
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


# ---------------------------------------------------------------------------
# Filesystem helpers (engram_home only — no projects_dir)
# ---------------------------------------------------------------------------

def _write_config(
    engram_home: Path,
    agent_name: str = "borges",
    mode: str = "multi",
) -> None:
    """Write a minimal config.json into engram_home."""
    cfg = {"mode": mode, "agent_name": agent_name}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _write_baton_cache(engram_home: Path, entries: dict) -> None:
    """Write baton-status-cache.json pre-populated with anchor -> render entries.

    The auto-archive pass is cache-only.  Entries are stamped with time.time()
    so they pass the 30s TTL freshness check.
    """
    cache = {
        anchor: {"fetched_at": time.time(), "render": render_str}
        for anchor, render_str in entries.items()
    }
    (engram_home / "baton-status-cache.json").write_text(json.dumps(cache))


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_hook_module(engram_home: Path):
    """Import the hook as a live module with ENGRAM_HOME set to engram_home."""
    spec = importlib.util.spec_from_file_location("engram_baton_prompt_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Integration runner helper
# ---------------------------------------------------------------------------

def _call_main(
    mod,
    projects=None,
    raise_exc=None,
    gh_ok: bool = False,
) -> tuple:
    """Call mod.main() with _fetch_active_projects and _gh_available patched.

    Returns (stdout_str, stderr_str, exit_code).

    Parameters
    ----------
    projects:
        List of project dicts returned by the fake fetch.  None treated as [].
    raise_exc:
        If set, _fetch_active_projects raises this exception instead.
    gh_ok:
        Value _gh_available() returns, controlling gh-dependent code paths
        (live-status rendering and the auto-archive pass).
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0

    def _fake_fetch(config):
        if raise_exc is not None:
            raise raise_exc
        return projects if projects is not None else []

    with patch.object(mod, "_fetch_active_projects", side_effect=_fake_fetch):
        with patch.object(mod, "_gh_available", return_value=gh_ok):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    mod.main()
            except SystemExit as e:
                exit_code = int(e.code) if e.code is not None else 0

    return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


def _parse_ctx(stdout_str: str) -> str:
    """Parse the additionalContext string from hook stdout JSON."""
    return json.loads(stdout_str)["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# Unit tests -- _my_batons()
# ---------------------------------------------------------------------------

class TestMyBatons:
    """Pure-function unit tests for _my_batons(projects, agent_name)."""

    def _mod(self, engram_home: Path):
        _write_config(engram_home)
        return _load_hook_module(engram_home)

    # 1. Turn filter
    def test_turn_filter_includes_mine(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-100", turn="borges")
        result = mod._my_batons([p], "borges")
        assert len(result) == 1
        assert result[0]["project_id"] == "PR-100"

    def test_turn_filter_excludes_other(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-100", turn="ariadne")
        assert mod._my_batons([p], "borges") == []

    # 2. Status filter
    def test_status_filter_excludes_merged(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-100", turn="borges", status="merged")
        assert mod._my_batons([p], "borges") == []

    def test_status_filter_excludes_cancelled(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-100", turn="borges", status="cancelled")
        assert mod._my_batons([p], "borges") == []

    def test_status_active_included(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-100", turn="borges", status="in-review")
        assert len(mod._my_batons([p], "borges")) == 1

    # 3. Sort
    def test_sorted_oldest_first(self, engram_home):
        mod = self._mod(engram_home)
        older = _make_project_dict("PR-200", turn="borges", turn_since=_ts(-120))
        newer = _make_project_dict("PR-201", turn="borges", turn_since=_ts(-30))
        result = mod._my_batons([newer, older], "borges")  # pass newer first
        assert result[0]["project_id"] == "PR-200"  # older must appear first
        assert result[1]["project_id"] == "PR-201"

    # 4. github_anchor from PR-NNN
    def test_github_anchor_pr_nnn(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-455", turn="borges")
        result = mod._my_batons([p], "borges")
        assert result[0]["github_anchor"] == "pr/455"

    # 5. github_anchor None for non-PR-NNN
    def test_github_anchor_none_for_non_pr(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("pool-coordination", turn="borges")
        result = mod._my_batons([p], "borges")
        assert result[0]["github_anchor"] is None

    # 6. Empty project list
    def test_empty_project_list(self, engram_home):
        mod = self._mod(engram_home)
        assert mod._my_batons([], "borges") == []

    # 7. Empty agent_name
    def test_empty_agent_name(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-1", turn="")
        assert mod._my_batons([p], "") == []

    # 8. turn_since parsed to datetime
    def test_turn_since_parsed_to_datetime(self, engram_home):
        mod = self._mod(engram_home)
        ts = _ts(-60)
        p = _make_project_dict("PR-400", turn="borges", turn_since=ts)
        result = mod._my_batons([p], "borges")
        assert isinstance(result[0]["turn_since"], datetime)

    def test_turn_reason_preserved(self, engram_home):
        mod = self._mod(engram_home)
        p = _make_project_dict("PR-300", turn="borges", turn_reason="round-3 pushed")
        result = mod._my_batons([p], "borges")
        assert result[0]["turn_reason"] == "round-3 pushed"


# ---------------------------------------------------------------------------
# Integration tests -- main() rendering
# ---------------------------------------------------------------------------

class TestMainRendering:
    """Integration tests for main(): in-court baton surfacing."""

    # 9. Single-agent gate
    def test_single_agent_gate_silent_exit(self, engram_home):
        """mode='single' in config -> hook exits 0 with no output."""
        _write_config(engram_home, mode="single")
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-10", turn="borges"),
        ])
        assert rc == 0
        assert stdout.strip() == "", "single-agent mode must produce no output"

    # 10. forum_api import failure gate
    def test_forum_api_gate_silent_exit(self, engram_home):
        """_FORUM_API_OK=False -> hook exits 0 with no output."""
        _write_config(engram_home, mode="multi")
        mod = _load_hook_module(engram_home)
        mod._FORUM_API_OK = False
        stdout, stderr, rc = _call_main(mod, projects=[])
        assert rc == 0
        assert stdout.strip() == "", "forum_api import failure must be silent"

    # 11. Empty project list
    def test_empty_project_list_no_output(self, engram_home):
        """Empty forum project list -> no output."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[])
        assert rc == 0
        assert stdout.strip() == ""

    # 12. No mine batons
    def test_no_mine_batons_silent(self, engram_home):
        """No batons in borges's court -> no output."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-100", turn="ariadne"),
        ])
        assert rc == 0
        assert stdout.strip() == ""

    # 13. One baton surfaces
    def test_one_baton_surfaces(self, engram_home):
        """One project in borges's court -> additionalContext with project ID."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-200", title="viz selector", turn="borges",
                               turn_reason="round-3 fix pushed"),
        ])
        assert rc == 0
        assert stdout.strip() != ""
        response = json.loads(stdout)
        assert "hookSpecificOutput" in response
        output = response["hookSpecificOutput"]
        assert output["hookEventName"] == "UserPromptSubmit"
        ctx = output["additionalContext"]
        assert "PR-200" in ctx
        assert "baton" in ctx.lower()

    # 14. Multiple batons sorted
    def test_multiple_batons_sorted_oldest_first(self, engram_home):
        """Multiple in-court batons: all listed, oldest first, count in header."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-301", turn="borges", turn_since=_ts(-120)),
            _make_project_dict("PR-302", turn="borges", turn_since=_ts(-30)),
        ])
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "PR-301" in ctx and "PR-302" in ctx
        assert ctx.find("PR-301") < ctx.find("PR-302"), "older baton must appear first"
        assert "2 batons" in ctx

    # 15. Other-agent baton not surfaced
    def test_other_agent_baton_not_surfaced(self, engram_home):
        """Baton with turn=ariadne is not surfaced for borges."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-400", turn="ariadne"),
        ])
        assert stdout.strip() == "", "ariadne's baton must not surface for borges"

    # 16. Merged not surfaced
    def test_merged_project_not_surfaced(self, engram_home):
        """Merged project not surfaced even if turn==agent."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-500", turn="borges", status="merged"),
        ])
        assert stdout.strip() == "", "merged project must not surface"

    # 17. Cancelled not surfaced
    def test_cancelled_project_not_surfaced(self, engram_home):
        """Cancelled project not surfaced even if turn==agent."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-501", turn="borges", status="cancelled"),
        ])
        assert stdout.strip() == "", "cancelled project must not surface"

    # 18. turn_reason in output
    def test_turn_reason_in_output(self, engram_home):
        """turn_reason text appears verbatim in the surfaced context."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-700", turn="borges",
                               turn_reason="round-3 fix pushed; awaiting re-review"),
        ])
        ctx = _parse_ctx(stdout)
        assert "round-3 fix pushed" in ctx

    # 19. JSON output format
    def test_json_output_format(self, engram_home):
        """Output is valid JSON with hookSpecificOutput.additionalContext."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-800", turn="borges"),
        ])
        assert rc == 0
        response = json.loads(stdout)
        assert isinstance(response, dict)
        hook_out = response["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "UserPromptSubmit"
        assert isinstance(hook_out["additionalContext"], str)
        assert len(hook_out["additionalContext"]) > 0

    # 20. Mixed state
    def test_mixed_state_only_mine_active_surfaced(self, engram_home):
        """Only active in-court batons surface; others invisible."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-901", turn="borges", status="in-review"),
            _make_project_dict("PR-902", turn="ariadne", status="in-review"),
            _make_project_dict("PR-903", turn="borges", status="merged"),
            _make_project_dict("PR-904", turn="borges", status="cancelled"),
        ])
        ctx = _parse_ctx(stdout)
        assert "PR-901" in ctx       # mine, active -> surfaces
        assert "PR-902" not in ctx   # not mine
        assert "PR-903" not in ctx   # mine but merged
        assert "PR-904" not in ctx   # mine but cancelled
        assert "1 baton" in ctx      # only one active in-court baton

    # 21. Age formatting
    def test_age_formatting_in_output(self, engram_home):
        """turn_since age ('X ago') appears in the surfaced output."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-950", turn="borges", turn_since=_ts(-120)),
        ])
        ctx = _parse_ctx(stdout)
        assert "PR-950" in ctx
        assert "ago" in ctx

    # 22. Decision-prompt block
    def test_decision_prompt_block_present(self, engram_home):
        """The 'For EACH above' decision-prompt block appears in the output."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(mod, projects=[
            _make_project_dict("PR-960", turn="borges"),
        ])
        ctx = _parse_ctx(stdout)
        # The prompt block tells the agent to decide the state and act
        assert "For EACH above" in ctx

    # 23. UCS unreachable -- ForumNetworkError
    def test_ucs_unreachable_banner_network_error(self, engram_home):
        """ForumNetworkError -> loud UCS-unreachable banner on stdout."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(
            mod,
            raise_exc=mod.ForumNetworkError("connection refused"),
        )
        assert rc == 0
        assert stdout.strip() != "", "UCS banner must appear on stdout"
        ctx = _parse_ctx(stdout)
        assert "UCS unreachable" in ctx

    # 24. UCS unreachable -- ForumHttpError
    def test_ucs_unreachable_banner_http_error(self, engram_home):
        """ForumHttpError -> loud UCS-unreachable banner on stdout."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        stdout, stderr, rc = _call_main(
            mod,
            raise_exc=mod.ForumHttpError(503, "Service Unavailable"),
        )
        assert rc == 0
        ctx = _parse_ctx(stdout)
        assert "UCS unreachable" in ctx

    # 25. GitHub live-status clause in rendered output
    def test_github_live_status_clause_shown(self, engram_home):
        """PR-anchored baton with cached status shows live clause in the output."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_baton_cache(engram_home, {"pr/999": "OPEN · review:APPROVED"})

        @staticmethod
        def _which(name):
            if name in ("baton", "gh"):
                return name
            return None

        with patch.object(mod.shutil, "which", side_effect=_which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                stdout, stderr, rc = _call_main(mod, projects=[
                    _make_project_dict("PR-999", turn="borges"),
                ], gh_ok=True)

        ctx = _parse_ctx(stdout)
        assert "OPEN · review:APPROVED" in ctx, (
            f"Live status clause missing from context: {ctx!r}"
        )


# ---------------------------------------------------------------------------
# Unit tests -- #1715 repo-qualified anchor parsing + threading
# ---------------------------------------------------------------------------

class TestParsePrAnchorRepoQualified:
    """#1715 reviewer-fairy-caught gap: the naive anchor[3:] slice this hook
    used produced 'owner/repo/N' verbatim for a repo-qualified anchor, which
    gh would reject as a garbage PR-number positional arg. _parse_pr_anchor
    fixes this; these tests lock its contract directly."""

    def test_bare_anchor_resolves_default_repo(self, engram_home):
        mod = _load_hook_module(engram_home)
        assert mod._parse_pr_anchor("pr/1005") == (mod._DEFAULT_GITHUB_REPO, "1005")

    def test_qualified_anchor_resolves_its_own_repo(self, engram_home):
        mod = _load_hook_module(engram_home)
        assert mod._parse_pr_anchor("pr/engram-agents/engram-paper/22") == (
            "engram-agents/engram-paper", "22",
        )

    def test_non_pr_anchor_returns_none(self, engram_home):
        mod = _load_hook_module(engram_home)
        assert mod._parse_pr_anchor("project/4") is None
        assert mod._parse_pr_anchor("nonsense") is None

    def test_get_live_status_threads_repo_to_fetch_pr_status(self, engram_home):
        """_get_live_status must pass the ANCHOR's own repo to
        _fetch_pr_status, not silently drop it or pass the whole
        owner/repo/N tail as a garbage pr_num."""
        mod = _load_hook_module(engram_home)
        calls = []

        def _fake_fetch(pr_num, repo=None):
            calls.append((pr_num, repo))
            return "OPEN"

        with patch.object(mod, "_fetch_pr_status", side_effect=_fake_fetch):
            result = mod._get_live_status("pr/engram-agents/engram-paper/22", {})

        assert result == "OPEN"
        assert calls == [("22", "engram-agents/engram-paper")]


# ---------------------------------------------------------------------------
# Unit tests -- _auto_archive_merged_pr_batons()
# ---------------------------------------------------------------------------

class TestAutoArchivePure:
    """Pure-function unit tests for _auto_archive_merged_pr_batons(projects, cache)."""

    @staticmethod
    def _which(name: str):
        if name in ("baton", "gh"):
            return name
        return None

    @staticmethod
    def _fresh_cache(anchor: str, render: str) -> dict:
        """Return a within-TTL cache dict containing one entry."""
        return {anchor: {"fetched_at": time.time(), "render": render}}

    @staticmethod
    def _close_calls_from(mock_sub) -> list:
        """Extract ['baton', 'close', ...] calls from a mocked subprocess."""
        return [
            call.args[0] for call in mock_sub.run.call_args_list
            if call.args and call.args[0][:2] == ["baton", "close"]
        ]

    def _mod(self, engram_home: Path):
        _write_config(engram_home)
        return _load_hook_module(engram_home)

    # 26. MERGED -> baton close merged
    def test_merged_in_cache_invokes_close_merged(self, engram_home):
        """MERGED in cache -> baton close --status merged."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-1100", turn="borges", status="in-progress")]
        cache = self._fresh_cache("pr/1100", "MERGED")

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                mod._auto_archive_merged_pr_batons(projects, cache)

        mock_sub.run.assert_called_once_with(
            ["baton", "close", "PR-1100", "--status", "merged"],
            capture_output=True, text=True, timeout=mod._GH_TIMEOUT,
        )

    # 27. CLOSED -> baton close cancelled
    def test_closed_in_cache_invokes_close_cancelled(self, engram_home):
        """CLOSED in cache -> baton close --status cancelled."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-2800", turn="borges", status="in-review")]
        cache = self._fresh_cache("pr/2800", "CLOSED")

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                mod._auto_archive_merged_pr_batons(projects, cache)

        mock_sub.run.assert_called_once_with(
            ["baton", "close", "PR-2800", "--status", "cancelled"],
            capture_output=True, text=True, timeout=mod._GH_TIMEOUT,
        )

    # 28. OPEN -> no close
    def test_open_in_cache_no_baton_close(self, engram_home):
        """OPEN in cache -> no baton close (not a terminal state)."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-1200", turn="borges", status="in-review")]
        cache = self._fresh_cache("pr/1200", "OPEN")

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                mod._auto_archive_merged_pr_batons(projects, cache)

        assert self._close_calls_from(mock_sub) == [], "OPEN must not trigger baton close"

    # 29. Uncached anchor -> no close
    def test_uncached_anchor_no_baton_close(self, engram_home):
        """Empty cache -> no close (cache-only guard: uncached anchors are skipped)."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-1201", turn="borges", status="in-review")]
        cache: dict = {}

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                mod._auto_archive_merged_pr_batons(projects, cache)

        assert self._close_calls_from(mock_sub) == [], (
            "uncached anchor must not trigger baton close"
        )

    # 30. Idempotency -- already merged
    def test_already_merged_status_idempotency(self, engram_home):
        """Baton with status=merged is skipped even if cache says MERGED."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-2200", turn="maintainer", status="merged")]
        cache = self._fresh_cache("pr/2200", "MERGED")

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                mod._auto_archive_merged_pr_batons(projects, cache)

        assert self._close_calls_from(mock_sub) == [], (
            "already-merged baton must not be archived again"
        )

    # 31. Idempotency -- already cancelled
    def test_already_cancelled_status_idempotency(self, engram_home):
        """Baton with status=cancelled is skipped even if cache says CLOSED."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-2900", turn="borges", status="cancelled")]
        cache = self._fresh_cache("pr/2900", "CLOSED")

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                mod._auto_archive_merged_pr_batons(projects, cache)

        assert self._close_calls_from(mock_sub) == [], (
            "already-cancelled baton must not be archived again"
        )

    # 32. Non-PR-NNN not archived
    def test_non_pr_nnn_project_not_archived(self, engram_home):
        """Non-PR-NNN project_id is never archived (no anchor resolves)."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("pool-coordination", turn="maintainer",
                                       status="in-progress")]
        # Cache has a key that won't match via anchor resolution anyway
        cache = self._fresh_cache("pool-coordination", "MERGED")

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                mod._auto_archive_merged_pr_batons(projects, cache)

        assert self._close_calls_from(mock_sub) == [], (
            "non-PR-NNN batons must not be archived"
        )

    # 33. Baton binary missing
    def test_baton_binary_missing_emits_skip_stderr(self, engram_home):
        """baton binary absent -> stderr skip line, no exception raised."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-2600", turn="maintainer", status="in-progress")]
        cache: dict = {}

        stderr_lines: list = []

        def _fake_print(*args, **kwargs):
            if kwargs.get("file") is sys.stderr:
                stderr_lines.append(str(args[0]) if args else "")

        with patch.object(mod.shutil, "which", return_value=None):
            with patch("builtins.print", side_effect=_fake_print):
                mod._auto_archive_merged_pr_batons(projects, cache)  # must not raise

        assert any("baton CLI not found" in line for line in stderr_lines), (
            f"Expected 'baton CLI not found' in stderr; got: {stderr_lines}"
        )

    # 34. subprocess raises -> no crash
    def test_subprocess_raises_no_crash(self, engram_home):
        """subprocess.run raising must not propagate out of _auto_archive."""
        mod = self._mod(engram_home)
        projects = [_make_project_dict("PR-1300", turn="borges", status="in-review")]
        cache = self._fresh_cache("pr/1300", "MERGED")

        def _exploding_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                raise OSError("baton not found")
            return type("CP", (), {"returncode": 0})()

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.side_effect = _exploding_run
                # Must not raise
                mod._auto_archive_merged_pr_batons(projects, cache)


# ---------------------------------------------------------------------------
# Integration tests -- auto-archive via main()
# ---------------------------------------------------------------------------

class TestAutoArchiveIntegration:
    """Integration tests for the auto-archive pass running through main()."""

    @staticmethod
    def _which(name: str):
        if name in ("baton", "gh"):
            return name
        return None

    # 35. Merged baton at turn=maintainer archived (second-pass reachability)
    def test_merged_pr_baton_at_maintainer_turn_archived(self, engram_home):
        """PR-baton at turn=maintainer (not in borges's court) is archived via second pass.

        Merged PRs always have turn==maintainer (the merge-ready signal), so
        they are never in any agent's court and would never be reached by the
        rendering loop.  The second-pass scan in _auto_archive_merged_pr_batons
        is the only reliable detection path.
        """
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_baton_cache(engram_home, {"pr/2100": "MERGED"})
        projects = [
            _make_project_dict("PR-2100", turn="maintainer", status="in-progress",
                               turn_reason="colleague-approved; ready for merge"),
        ]

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.return_value = type("CP", (), {"returncode": 0})()
                stdout, stderr, rc = _call_main(mod, projects=projects, gh_ok=True)

        mock_sub.run.assert_called_once_with(
            ["baton", "close", "PR-2100", "--status", "merged"],
            capture_output=True, text=True, timeout=mod._GH_TIMEOUT,
        )

    # 36. gh unavailable -> auto-archive skipped
    def test_gh_unavailable_skips_auto_archive(self, engram_home):
        """gh unavailable (_gh_available returns False) -> auto-archive pass skipped."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_baton_cache(engram_home, {"pr/2400": "MERGED"})
        projects = [
            _make_project_dict("PR-2400", turn="maintainer", status="in-progress"),
        ]
        close_calls: list = []

        def _fake_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                close_calls.append(args)
            return type("CP", (), {"returncode": 0})()

        with patch.object(mod, "subprocess") as mock_sub:
            mock_sub.run.side_effect = _fake_run
            # gh_ok=False -> _gh_available() returns False -> auto-archive skipped
            stdout, stderr, rc = _call_main(mod, projects=projects, gh_ok=False)

        assert close_calls == [], "baton close must not run when gh is unavailable"

    # 37. Zero in-court batons + merged baton -> archive runs, stdout empty
    def test_zero_in_court_auto_archive_still_runs(self, engram_home):
        """Auto-archive runs even with zero in-court batons; stdout stays empty.

        Previously (pre-fix) the hook exited early when my_batons was empty,
        so the idle agent could never auto-archive.  After the restructure, the
        second-pass scan runs regardless.
        """
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_baton_cache(engram_home, {"pr/2700": "MERGED"})
        projects = [
            _make_project_dict("PR-2700", turn="maintainer", status="in-progress",
                               turn_reason="colleague-approved"),
        ]
        close_calls: list = []

        def _fake_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                close_calls.append(args)
            return type("CP", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.side_effect = _fake_run
                stdout, stderr, rc = _call_main(mod, projects=projects, gh_ok=True)

        assert len(close_calls) == 1, f"Expected 1 baton close; got: {close_calls}"
        assert close_calls[0][2] == "PR-2700"
        assert stdout.strip() == "", f"stdout must be empty when no in-court batons; got: {stdout!r}"

    # 38. Non-zero returncode -> stderr log; stdout empty
    def test_nonzero_baton_close_returncode_emits_stderr(self, engram_home):
        """Non-zero baton close returncode -> stderr log; stdout stays empty."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_baton_cache(engram_home, {"pr/2500": "MERGED"})
        # Baton at turn=maintainer -> no in-court rendering, stdout must stay empty
        projects = [
            _make_project_dict("PR-2500", turn="maintainer", status="in-progress",
                               turn_reason="ready for merge"),
        ]

        def _failing_run(args, **kwargs):
            return type("CP", (), {
                "returncode": 1,
                "stderr": "baton: no such file",
                "stdout": "",
            })()

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.side_effect = _failing_run
                stdout, stderr, rc = _call_main(mod, projects=projects, gh_ok=True)

        assert stdout.strip() == "", f"stdout must be empty; got: {stdout!r}"
        assert "auto-archive" in stderr, (
            f"stderr must mention 'auto-archive'; got: {stderr!r}"
        )
        assert "PR-2500" in stderr, (
            f"stderr must mention the project id; got: {stderr!r}"
        )

    # 39. baton close raises inside main() -> no crash; in-court baton still rendered
    def test_baton_close_raises_does_not_crash_main(self, engram_home):
        """baton close subprocess raising must not crash main(); in-court JSON still emitted."""
        _write_config(engram_home)
        mod = _load_hook_module(engram_home)
        _write_baton_cache(engram_home, {"pr/1300": "MERGED"})
        # Baton in borges's court -> rendering loop produces JSON output
        projects = [
            _make_project_dict("PR-1300", turn="borges", status="in-review",
                               turn_reason="awaiting merge"),
        ]

        def _exploding_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                raise OSError("baton not found")
            return type("CP", (), {"returncode": 0})()

        with patch.object(mod.shutil, "which", side_effect=self._which):
            with patch.object(mod, "subprocess") as mock_sub:
                mock_sub.run.side_effect = _exploding_run
                stdout, stderr, rc = _call_main(mod, projects=projects, gh_ok=True)

        assert rc == 0
        response = json.loads(stdout)
        assert "hookSpecificOutput" in response
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-1300" in ctx, "in-court baton must still appear despite baton-close crash"


# ---------------------------------------------------------------------------
# Flattened-topology regression (gh#1657 -- _hooklib extraction)
# ---------------------------------------------------------------------------

import subprocess


def test_hooklib_resolution_in_flattened_deployed_topology(tmp_path):
    """The plugin build FLATTENS hooks/claude/ -> hooks/, so the deployed
    baton hook sits one level shallower than source, AND _hooklib.py (its
    sibling, gh#1657) AND _prompthooklib.py (its sibling, gh#1680 slice 1)
    must both be found alongside it in that same flattened layout. Recreate
    the deployed layout (hook + _hooklib.py + _prompthooklib.py as siblings,
    with a further-nested tools/) and confirm, with CLAUDE_PLUGIN_ROOT unset:
      (a) tools/ resolves (_FORUM_API_OK, _TOOLS_DIR)
      (b) config loads via _load_config()
      (c) agent name resolves via _get_agent_name(config)
      (d) emit_context() prints the identical additionalContext envelope
    -- this hook had no such regression test before the _hooklib extraction;
    forum's equivalent (#1540) is the model.
    """
    root = tmp_path / "engram"
    (root / "hooks").mkdir(parents=True)
    (root / "tools").mkdir()
    hooklib_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_hooklib.py"
    )
    prompthooklib_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_prompthooklib.py"
    )
    (root / "hooks" / "engram-baton-prompt-hook.py").write_text(HOOK_PATH.read_text())
    (root / "hooks" / "_hooklib.py").write_text(hooklib_src.read_text())
    (root / "hooks" / "_prompthooklib.py").write_text(prompthooklib_src.read_text())
    # Stub forum_api.py so the import can succeed.
    (root / "tools" / "forum_api.py").write_text(
        "class ForumClient:\n"
        "    def __init__(self, *a, **k): pass\n"
        "class ForumNetworkError(Exception): pass\n"
        "class ForumHttpError(Exception):\n"
        "    def __init__(self, status=0, body=''): self.status=status; self.body=body\n"
        "def forum_url_from_config(config): return 'http://localhost:5002'\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent_name": "luria", "mode": "multi"}))

    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(home)
    probe_script = (
        "import importlib.util, sys, io, json, contextlib\n"
        "spec = importlib.util.spec_from_file_location('hook', "
        f"  r'{root}/hooks/engram-baton-prompt-hook.py')\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "print('resolved' if mod._FORUM_API_OK and mod._TOOLS_DIR is not None else 'broken')\n"
        "cfg = mod._load_config()\n"
        "print('config_ok' if cfg.get('agent_name') == 'luria' else f'config_broken:{cfg!r}')\n"
        "name = mod._get_agent_name(cfg)\n"
        "print('name_ok' if name == 'luria' else f'name_broken:{name!r}')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    mod._emit_context('hello world')\n"
        "envelope = json.loads(buf.getvalue())\n"
        "expected = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'hello world'}}\n"
        "print('emit_ok' if envelope == expected else f'emit_broken:{envelope!r}')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe_script],
        capture_output=True, text=True, env=env,
    )
    assert "resolved" in r.stdout, (
        "deployed-topology hook failed to resolve _hooklib/forum_api via "
        f"walk-parents (stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "config_ok" in r.stdout, (
        f"deployed-topology hook failed to load config via _prompthooklib "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "name_ok" in r.stdout, (
        f"deployed-topology hook failed to resolve agent name via _prompthooklib "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "emit_ok" in r.stdout, (
        f"deployed-topology hook's emit_context envelope diverged "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
