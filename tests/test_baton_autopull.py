"""Tests for the auto-pull GitHub status enhancement in
hooks/claude/engram-baton-prompt-hook.py — pure forum-API model.

Migrated from the old subprocess/_write_project scaffolding to the pure
forum-API pattern: _fetch_active_projects is monkeypatched to return
project-dict lists, and integration tests call main() directly via
_call_main() rather than spawning a subprocess.

Unique behavioral coverage preserved from the original file:
  1. Anchor resolution (TestAnchorResolution) — _resolve_anchor() unit tests.
  2. gh-status CACHE hit/stale (TestCacheHit / TestCacheStale) — direct unit
     tests of _get_live_status / _cache_get / _cache_set.
  3. gh quick-fail / timeout fallback (TestQuickFail) — unit + integration.
  4. gh-absent graceful degrade (TestGhAbsent) — integration with gh_ok=False.
  5. Cache persistence round-trip (TestCachePersistence) — _save_cache /
     _load_cache unit tests.
  6. Live-status-clause rendering (TestLiveClauseInOutput) — integration with
     pre-populated on-disk cache; verifies the [STATUS] clause appears in the
     rendered additionalContext output.

Dropped (pure duplicate of test_baton_hook.py):
  None. All categories cover angles not present in test_baton_hook.py.

Note on project-anchored batons (github: project/N):
  The forum /api/projects list endpoint does not carry the github: field, so
  _my_batons() always resolves anchors from the project_id only (PR-NNN →
  pr/NNN; pool-x → None). Project-anchored batons therefore receive no live-
  status clause — graceful degradation. Tests that previously relied on the
  github: frontmatter field for project/N anchors are updated to reflect this
  (they either use PR-NNN ids or assert no clause for non-PR-NNN ids).
"""

import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Hook path
# ---------------------------------------------------------------------------

_HOOK_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "engram-baton-prompt-hook.py"
)


# ---------------------------------------------------------------------------
# Data helpers (match test_baton_hook.py convention)
# ---------------------------------------------------------------------------

def _make_project_dict(
    project_id: str,
    title: str = "Test project",
    status: str = "in-review",
    turn: str = "ariadne",
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
        "participants": participants or ["ariadne", "borges"],
        "seq": seq,
    }


def _ts(offset_minutes: int) -> datetime:
    """Return a UTC datetime offset by given minutes from now."""
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


# ---------------------------------------------------------------------------
# Filesystem helpers (engram_home only — no projects_dir)
# ---------------------------------------------------------------------------

def _write_config(engram_home: Path, agent_name: str = "ariadne") -> None:
    """Write a minimal multi-agent config.json into engram_home."""
    cfg = {"mode": "multi", "agent_name": agent_name}
    (engram_home / "config.json").write_text(json.dumps(cfg))


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_hook_module(engram_home: Path):
    """Import the hook as a module with ENGRAM_HOME patched to tmp dir."""
    spec = importlib.util.spec_from_file_location(
        "engram_baton_prompt_hook", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Integration runner helper (mirrors test_baton_hook.py)
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


# ===========================================================================
# Category 1: Anchor resolution (_resolve_anchor)
# ===========================================================================

class TestAnchorResolution:
    """Unit tests for _resolve_anchor."""

    def _mod(self, tmp_engram_home):
        return _load_hook_module(tmp_engram_home)

    def test_explicit_pr_field(self, tmp_engram_home):
        """``github: pr/455`` frontmatter → anchor ``pr/455``."""
        mod = self._mod(tmp_engram_home)
        assert mod._resolve_anchor("SOME-ID", "pr/455") == "pr/455"

    def test_explicit_project_field(self, tmp_engram_home):
        """``github: project/4`` frontmatter → anchor ``project/4``."""
        mod = self._mod(tmp_engram_home)
        assert mod._resolve_anchor("SOME-ID", "project/4") == "project/4"

    def test_fallback_pr_id_pattern(self, tmp_engram_home):
        """ID ``PR-455`` (no github field) → anchor ``pr/455`` via fallback."""
        mod = self._mod(tmp_engram_home)
        assert mod._resolve_anchor("PR-455", "") == "pr/455"

    def test_fallback_pr_id_no_github_field(self, tmp_engram_home):
        """ID ``PR-123`` with no github field at all → anchor ``pr/123``."""
        mod = self._mod(tmp_engram_home)
        assert mod._resolve_anchor("PR-123", "") is not None
        assert mod._resolve_anchor("PR-123", "").startswith("pr/")

    def test_no_anchor_pool_id(self, tmp_engram_home):
        """``pool-x`` ID with no github field → no anchor (None)."""
        mod = self._mod(tmp_engram_home)
        assert mod._resolve_anchor("pool-x", "") is None

    def test_no_anchor_design_id(self, tmp_engram_home):
        """``DESIGN-foo`` ID with no github field → no anchor (None)."""
        mod = self._mod(tmp_engram_home)
        assert mod._resolve_anchor("DESIGN-foo", "") is None

    def test_explicit_field_wins_over_fallback(self, tmp_engram_home):
        """Explicit ``github:`` field overrides PR-N fallback."""
        mod = self._mod(tmp_engram_home)
        # PR-100 would normally resolve to pr/100 via fallback,
        # but explicit github: pr/999 should win.
        assert mod._resolve_anchor("PR-100", "pr/999") == "pr/999"

    def test_explicit_field_case_insensitive(self, tmp_engram_home):
        """``github:`` field value is normalised to lowercase."""
        mod = self._mod(tmp_engram_home)
        result = mod._resolve_anchor("SOME-ID", "PR/455")
        # After lower(), "pr/455" starts with "pr/" → should match
        assert result == "pr/455"


# ===========================================================================
# Category 2: Cache hit — fresh entry skips gh
# ===========================================================================

class TestCacheHit:
    """A fresh (<30s) cache entry is reused; gh is NOT called."""

    def test_cache_hit_skips_gh_call(self, tmp_engram_home):
        """Fresh cache entry returned without calling gh."""
        mod = _load_hook_module(tmp_engram_home)

        # Prime the cache with a fresh entry
        cache = {}
        mod._cache_set(cache, "pr/455", "OPEN · review:APPROVED")
        # Entry is <1s old → should be treated as fresh

        with patch.object(mod, "_fetch_pr_status") as mock_fetch:
            result = mod._get_live_status("pr/455", cache)

        assert result == "OPEN · review:APPROVED"
        mock_fetch.assert_not_called()

    def test_cache_hit_project_skips_gh_call(self, tmp_engram_home):
        """Fresh project cache entry returned without calling gh.

        _get_live_status handles project/ anchors; the function is still in
        the codebase even though _my_batons no longer produces project/ anchors
        (the API list endpoint omits the github: field). This unit test verifies
        the code path remains correct.
        """
        mod = _load_hook_module(tmp_engram_home)

        cache = {}
        mod._cache_set(cache, "project/4", "3 Done · 1 In Progress")

        with patch.object(mod, "_fetch_project_status") as mock_fetch:
            result = mod._get_live_status("project/4", cache)

        assert result == "3 Done · 1 In Progress"
        mock_fetch.assert_not_called()


# ===========================================================================
# Category 3: Cache stale — triggers a refresh
# ===========================================================================

class TestCacheStale:
    """A stale (>30s) cache entry causes a fresh gh call."""

    def test_stale_entry_triggers_refresh(self, tmp_engram_home):
        """Stale cache entry (>30s old) triggers a fresh gh call."""
        mod = _load_hook_module(tmp_engram_home)

        # Build a stale cache entry (>30s ago)
        stale_time = time.time() - 60  # 60s ago
        cache = {
            "pr/455": {"fetched_at": stale_time, "render": "OPEN (stale)"}
        }

        with patch.object(mod, "_fetch_pr_status", return_value="MERGED") as mock_fetch:
            result = mod._get_live_status("pr/455", cache)

        mock_fetch.assert_called_once_with("455")
        assert result == "MERGED"

    def test_stale_entry_updates_cache(self, tmp_engram_home):
        """After a refresh, the cache entry is updated with the new value."""
        mod = _load_hook_module(tmp_engram_home)

        stale_time = time.time() - 60
        cache = {"pr/100": {"fetched_at": stale_time, "render": "OLD"}}

        with patch.object(mod, "_fetch_pr_status", return_value="OPEN"):
            mod._get_live_status("pr/100", cache)

        # Cache should now have the fresh value
        assert cache["pr/100"]["render"] == "OPEN"
        assert cache["pr/100"]["fetched_at"] > stale_time


# ===========================================================================
# Category 4: Quick-fail — gh timeout/error degrades gracefully
# ===========================================================================

class TestQuickFail:
    """gh timeout/error falls back to cached render or omits clause; hook never raises."""

    def test_gh_timeout_falls_back_to_cache(self, tmp_engram_home):
        """On gh timeout, returns the stale cached render (not empty)."""
        mod = _load_hook_module(tmp_engram_home)

        # Stale cache entry available
        stale_time = time.time() - 60
        cache = {"pr/200": {"fetched_at": stale_time, "render": "OPEN (fallback)"}}

        with patch.object(
            mod, "_fetch_pr_status",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=4)
        ):
            result = mod._get_live_status("pr/200", cache)

        # Should fall back to stale cached render
        assert result == "OPEN (fallback)"

    def test_gh_error_no_cache_omits_clause(self, tmp_engram_home):
        """On gh error with no cache, returns empty string (clause omitted)."""
        mod = _load_hook_module(tmp_engram_home)

        cache = {}  # No cache at all

        with patch.object(mod, "_fetch_pr_status", return_value=None):
            result = mod._get_live_status("pr/300", cache)

        assert result == ""

    def test_gh_exception_no_cache_omits_clause(self, tmp_engram_home):
        """On unexpected exception, returns empty string (never raises)."""
        mod = _load_hook_module(tmp_engram_home)

        cache = {}

        with patch.object(
            mod, "_fetch_pr_status",
            side_effect=RuntimeError("unexpected")
        ):
            # _get_live_status wraps in try/except — should never raise
            result = mod._get_live_status("pr/400", cache)

        assert result == ""

    def test_hook_never_raises_on_gh_failure(self, tmp_engram_home):
        """End-to-end: hook returns valid JSON even when _fetch_pr_status fails.

        PR-500 is in ariadne's court; gh is "available" (gh_ok=True) but
        _fetch_pr_status returns None (simulating auth failure / non-zero gh
        exit). The hook must still surface PR-500 without a live clause and
        must not raise.
        """
        _write_config(tmp_engram_home)
        mod = _load_hook_module(tmp_engram_home)

        with patch.object(mod, "_fetch_pr_status", return_value=None):
            stdout, stderr, rc = _call_main(
                mod,
                projects=[_make_project_dict(
                    "PR-500",
                    turn="ariadne",
                    turn_reason="needs final review",
                )],
                gh_ok=True,
            )

        assert rc == 0
        assert stdout.strip() != ""
        response = json.loads(stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-500" in ctx
        # No crash — live clause simply absent or empty
        assert "Traceback" not in stderr

    def test_hook_summary_present_on_any_gh_state(self, tmp_engram_home):
        """Hook emits baton summary regardless of gh availability.

        gh_ok=False simulates gh absent from PATH entirely.  The baton must
        still surface in the rendered context without a live clause.
        """
        _write_config(tmp_engram_home)
        mod = _load_hook_module(tmp_engram_home)

        stdout, stderr, rc = _call_main(
            mod,
            projects=[_make_project_dict("PR-600", turn="ariadne")],
            gh_ok=False,
        )

        assert rc == 0
        response = json.loads(stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-600" in ctx


# ===========================================================================
# Category 5: gh absent — full degrade path (integration)
# ===========================================================================

class TestGhAbsent:
    """When gh is unavailable (gh_ok=False), hook degrades to turn_reason-only render."""

    def test_degrade_path_no_gh(self, tmp_engram_home):
        """Hook runs normally when gh is unavailable — no live clause, no crash."""
        _write_config(tmp_engram_home)
        mod = _load_hook_module(tmp_engram_home)

        stdout, stderr, rc = _call_main(
            mod,
            projects=[_make_project_dict(
                "PR-700",
                turn="ariadne",
                turn_reason="fix pushed",
            )],
            gh_ok=False,
        )

        assert rc == 0, f"Hook must exit 0; stderr: {stderr}"
        assert "Traceback" not in stderr
        response = json.loads(stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-700" in ctx
        assert "fix pushed" in ctx
        # No live status brackets when gh unavailable (best-effort omission)

    def test_degrade_path_pr_fallback_no_gh(self, tmp_engram_home):
        """PR-N id resolves its anchor but degrade is clean when gh unavailable.

        The project dict has no github: field (the API list endpoint doesn't
        carry it). _my_batons() resolves the anchor from the project_id:
        PR-800 → pr/800. With gh_ok=False, no live clause is attempted.
        """
        _write_config(tmp_engram_home)
        mod = _load_hook_module(tmp_engram_home)

        stdout, stderr, rc = _call_main(
            mod,
            projects=[_make_project_dict(
                "PR-800",
                turn="ariadne",
                turn_reason="awaiting review",
            )],
            gh_ok=False,
        )

        assert rc == 0
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "PR-800" in ctx
        assert "awaiting review" in ctx


# ===========================================================================
# Category 6: Cache persistence — cache written to disk after gh fetch
# ===========================================================================

class TestCachePersistence:
    """Cache is written to $ENGRAM_HOME/baton-status-cache.json."""

    def test_cache_written_after_fetch(self, tmp_engram_home):
        """After a successful gh fetch, cache is persisted to disk."""
        mod = _load_hook_module(tmp_engram_home)

        cache = {}
        with patch.object(mod, "_fetch_pr_status", return_value="OPEN"):
            mod._get_live_status("pr/900", cache)

        # Save the cache as main() would
        mod._save_cache(cache)

        cache_file = tmp_engram_home / "baton-status-cache.json"
        assert cache_file.exists()
        saved = json.loads(cache_file.read_text())
        assert "pr/900" in saved
        assert saved["pr/900"]["render"] == "OPEN"

    def test_cache_load_roundtrip(self, tmp_engram_home):
        """_save_cache / _load_cache round-trip preserves entries."""
        mod = _load_hook_module(tmp_engram_home)

        original = {
            "pr/100": {"fetched_at": 1_700_000_000.0, "render": "MERGED"}
        }
        mod._save_cache(original)

        loaded = mod._load_cache()
        assert loaded == original

    def test_cache_load_missing_file_returns_empty(self, tmp_engram_home):
        """_load_cache returns {} when cache file does not exist."""
        mod = _load_hook_module(tmp_engram_home)
        # No cache file written
        result = mod._load_cache()
        assert result == {}

    def test_cache_load_corrupt_file_returns_empty(self, tmp_engram_home):
        """_load_cache returns {} when cache file is corrupt JSON."""
        mod = _load_hook_module(tmp_engram_home)
        cache_file = tmp_engram_home / "baton-status-cache.json"
        cache_file.write_text("not json {{{{")
        result = mod._load_cache()
        assert result == {}


# ===========================================================================
# Category 7: Integration — live clause appears in rendered output
# ===========================================================================

class TestLiveClauseInOutput:
    """End-to-end: live clause appears in additionalContext when gh works."""

    def test_live_clause_appended_to_line(self, tmp_engram_home):
        """When a fresh cache entry exists, rendered line includes [STATUS] clause.

        Pre-populates the on-disk cache with OPEN · review:APPROVED for
        pr/1000.  The hook loads the cache, hits it for PR-1000's anchor, and
        appends the clause.  Also verifies that the cache file persists after
        the main() pass (main() calls _save_cache with the non-empty cache).

        Note: the live clause originates from the PR-NNN id fallback anchor
        (project_id PR-1000 → pr/1000), not from a github: frontmatter field
        (which the /api/projects list endpoint no longer carries).
        """
        _write_config(tmp_engram_home)
        mod = _load_hook_module(tmp_engram_home)

        # Pre-populate cache so _get_live_status returns without calling gh.
        cache_file = tmp_engram_home / "baton-status-cache.json"
        cache_file.write_text(json.dumps({
            "pr/1000": {
                "fetched_at": time.time(),
                "render": "OPEN · review:APPROVED",
            }
        }))

        stdout, stderr, rc = _call_main(
            mod,
            projects=[_make_project_dict(
                "PR-1000",
                turn="ariadne",
                turn_reason="needs final review",
            )],
            gh_ok=True,
        )

        assert rc == 0, f"Hook failed; stderr: {stderr}"
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "PR-1000" in ctx
        assert "OPEN · review:APPROVED" in ctx, (
            f"Live status clause missing from context: {ctx!r}"
        )
        # main() must persist the cache to disk after the pass.
        assert cache_file.exists(), (
            "expected baton-status-cache.json to persist after main()"
        )

    def test_no_anchor_no_live_clause(self, tmp_engram_home):
        """Baton with no anchor (pool-abc style) renders without live clause.

        pool-abc has no PR-N pattern → _resolve_anchor returns None →
        _my_batons sets github_anchor=None → the rendering loop skips
        _get_live_status entirely → _fetch_pr_status is never called.
        The hook emits a normal summary with the turn_reason only.

        Note: PR-anchored batons (PR-N ids) still get the clause; this test
        confirms the graceful degradation for non-PR-NNN ids.
        """
        _write_config(tmp_engram_home)
        mod = _load_hook_module(tmp_engram_home)

        # Unit check: pool-abc has no anchor
        anchor = mod._resolve_anchor("pool-abc", "")
        assert anchor is None

        # Integration: hook emits a normal summary without crashing,
        # and _fetch_pr_status is never invoked (no anchor to query).
        with patch.object(mod, "_fetch_pr_status") as mock_fetch:
            stdout, stderr, rc = _call_main(
                mod,
                projects=[_make_project_dict(
                    "pool-abc",
                    turn="ariadne",
                    turn_reason="pool work",
                )],
                gh_ok=True,
            )

        assert rc == 0
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "pool-abc" in ctx
        assert "pool work" in ctx
        # _fetch_pr_status must NOT have been called — no anchor resolves for pool-abc
        mock_fetch.assert_not_called()


def test_tools_resolution_in_flattened_deployed_topology(tmp_path):
    """Regression — a deploy-break the source-tree CI/reviews could not see.

    The plugin build FLATTENS hooks/claude/ -> hooks/, so the DEPLOYED hook sits
    one level shallower than source. A fixed ``parents[2]/tools`` resolved
    forum_api in the source tree but OVERSHOT in the deployed tree -> the import
    failed -> _FORUM_API_OK=False -> the hook silently no-op'd (rendered nothing).

    Recreate the deployed layout (hooks/<name>.py with a SIBLING tools/) with
    CLAUDE_PLUGIN_ROOT unset (forcing the walk-parents fallback) and assert the
    hook still resolves forum_api: with mode=='multi' and a forum stub that
    errors, a resolved import produces the loud "UCS unreachable" banner; a
    broken import silently exits with empty stdout.
    """
    root = tmp_path / "engram"
    (root / "hooks").mkdir(parents=True)
    (root / "tools").mkdir()
    hook_src = (
        Path(__file__).resolve().parents[1]
        / "src" / "engram" / "hooks" / "claude" / "engram-baton-prompt-hook.py"
    )
    (root / "hooks" / "engram-baton-prompt-hook.py").write_text(hook_src.read_text())
    # Stub forum_api whose ForumClient.get raises ForumNetworkError, so a
    # resolved import + passed mode-gate yields the banner (not a silent exit).
    (root / "tools" / "forum_api.py").write_text(
        "class ForumNetworkError(Exception):\n"
        "    def __init__(self, cause=''):\n"
        "        super().__init__('forum unreachable — ' + cause)\n"
        "class ForumHttpError(Exception):\n"
        "    def __init__(self, status=0, body=''):\n"
        "        self.status = status\n"
        "        super().__init__(body)\n"
        "def forum_url_from_config(config):\n"
        "    return 'http://127.0.0.1:1'\n"
        "class ForumClient:\n"
        "    def __init__(self, url, timeout=10):\n"
        "        pass\n"
        "    def get(self, path, params=None):\n"
        "        raise ForumNetworkError('stub')\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"mode": "multi", "agent_name": "ariadne"})
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(home)
    r = subprocess.run(
        [sys.executable, str(root / "hooks" / "engram-baton-prompt-hook.py")],
        input="{}", capture_output=True, text=True, env=env,
    )
    assert "UCS unreachable" in r.stdout, (
        "deployed-topology hook silently no-op'd — forum_api not resolved "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
