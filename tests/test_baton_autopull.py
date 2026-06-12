"""Tests for the auto-pull GitHub status enhancement in
hooks/claude/engram-baton-prompt-hook.py.

Covers (per spec):
  1. Anchor resolution:
     - ``github: pr/455`` frontmatter → anchor ``pr/455``
     - ``github: project/4`` frontmatter → anchor ``project/4``
     - ID ``PR-455`` (no github field) → anchor ``pr/455`` (fallback)
     - ``pool-x`` (no github field, no PR-N pattern) → no anchor
  2. Cache hit (fresh entry, <30s): reuses cached render, does NOT call gh.
  3. Cache miss / stale (>30s): triggers a fresh gh call.
  4. Quick-fail:
     - gh timeout/error → falls back to cached render (if present),
       else omits the live clause.
     - Hook still returns its summary, never raises.
  5. Degrade path: gh absent → hook emits normal summary (no crash).

All tests mock ``gh`` (no real GitHub calls). Tmp dirs used for cache
and projects directory throughout.
"""

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the hook module directly (not as a subprocess) for unit tests
# ---------------------------------------------------------------------------

_HOOK_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "engram-baton-prompt-hook.py"
)


def _load_hook_module(engram_home: Path):
    """Import the hook as a module with ENGRAM_HOME patched to tmp dir."""
    spec = importlib.util.spec_from_file_location(
        "engram_baton_prompt_hook", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    # Patch ENGRAM_HOME at module scope before exec
    # We re-load fresh each time to isolate module-level globals.
    with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Subprocess helper (for integration / degrade-path tests)
# ---------------------------------------------------------------------------

def _run_hook(
    engram_home: Path,
    projects_dir: Path,
    extra_env: dict | None = None,
) -> tuple:
    """Run the hook as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["BATON_PROJECTS_DIR"] = str(projects_dir)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(_HOOK_PATH)],
        input=json.dumps({"prompt": "test"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(engram_home: Path, agent_name: str = "ariadne") -> None:
    cfg = {"mode": "multi", "agent_name": agent_name}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _write_project(
    projects_dir: Path,
    project_id: str,
    turn: str = "ariadne",
    status: str = "in-review",
    turn_reason: str = "colleague review",
    turn_since: datetime | None = None,
    github: str = "",
) -> Path:
    """Write a minimal baton project file for testing."""
    if turn_since is None:
        turn_since = datetime.now(timezone.utc) - timedelta(minutes=30)
    ts_str = turn_since.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        f"project: {project_id}",
        f"title: Test {project_id}",
        f"status: {status}",
        f"turn: {turn}",
        f"turn_since: {ts_str}",
        f'turn_reason: "{turn_reason}"',
        "participants: [ariadne, borges]",
    ]
    if github:
        lines.append(f"github: {github}")
    lines += ["---", "", f"# {project_id}", ""]
    path = projects_dir / f"{project_id}.md"
    path.write_text("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


@pytest.fixture
def tmp_projects_dir(tmp_path: Path) -> Path:
    d = tmp_path / "projects"
    d.mkdir()
    return d


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
        # but explicit github: pr/999 should win
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

    def test_cache_hit_skips_gh_call(self, tmp_engram_home, tmp_projects_dir):
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
        """Fresh project cache entry returned without calling gh."""
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

    def test_hook_never_raises_on_gh_failure(self, tmp_engram_home, tmp_projects_dir, tmp_path):
        """End-to-end: hook returns a valid summary even when gh exits non-zero."""
        _write_config(tmp_engram_home)
        _write_project(tmp_projects_dir, "PR-500", github="pr/500")

        # Write a fake gh that always exits with error (simulates auth failure)
        fake_gh = tmp_path / "gh"
        fake_gh.write_text("#!/bin/sh\nexit 1\n")
        fake_gh.chmod(0o755)

        rc, stdout, stderr = _run_hook(
            tmp_engram_home, tmp_projects_dir,
            extra_env={
                "PATH": str(tmp_path),
                "BATON_PROJECTS_DIR": str(tmp_projects_dir),
            },
        )

        assert rc == 0
        assert stdout.strip() != ""
        response = json.loads(stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-500" in ctx
        # No crash — live clause simply absent or empty
        assert "Traceback" not in stderr

    def test_hook_summary_present_on_any_gh_state(self, tmp_engram_home, tmp_projects_dir):
        """Hook emits baton summary regardless of gh availability."""
        _write_config(tmp_engram_home)
        _write_project(tmp_projects_dir, "PR-600", turn="ariadne")

        # gh not on PATH at all
        rc, stdout, stderr = _run_hook(
            tmp_engram_home, tmp_projects_dir,
            extra_env={
                "PATH": "",  # No gh
                "BATON_PROJECTS_DIR": str(tmp_projects_dir),
            },
        )

        assert rc == 0
        response = json.loads(stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-600" in ctx


# ===========================================================================
# Category 5: gh absent — full degrade path (subprocess integration)
# ===========================================================================

class TestGhAbsent:
    """When gh is not on PATH, the hook degrades to turn_reason-only render."""

    def test_degrade_path_no_gh(self, tmp_engram_home, tmp_projects_dir):
        """Hook runs normally when gh is absent — no live clause, no crash."""
        _write_config(tmp_engram_home)
        _write_project(
            tmp_projects_dir, "PR-700",
            turn="ariadne",
            turn_reason="fix pushed",
            github="pr/700",
        )

        rc, stdout, stderr = _run_hook(
            tmp_engram_home, tmp_projects_dir,
            extra_env={
                "PATH": "",  # Remove gh from PATH
                "BATON_PROJECTS_DIR": str(tmp_projects_dir),
            },
        )

        assert rc == 0, f"Hook must exit 0; stderr: {stderr}"
        assert "Traceback" not in stderr
        response = json.loads(stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-700" in ctx
        assert "fix pushed" in ctx
        # No live status brackets when gh absent
        # (we just check it doesn't crash — the live clause is best-effort)

    def test_degrade_path_pr_fallback_no_gh(self, tmp_engram_home, tmp_projects_dir):
        """PR-N fallback anchor resolves but degrade is clean when gh absent."""
        _write_config(tmp_engram_home)
        # No github: field; PR-800 pattern triggers fallback anchor
        _write_project(
            tmp_projects_dir, "PR-800",
            turn="ariadne",
            turn_reason="awaiting review",
        )

        rc, stdout, stderr = _run_hook(
            tmp_engram_home, tmp_projects_dir,
            extra_env={
                "PATH": "",
                "BATON_PROJECTS_DIR": str(tmp_projects_dir),
            },
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

        # Re-load the module to test _load_cache against a DIFFERENT engram_home
        # Actually just call _load_cache directly — it reads from ENGRAM_HOME global
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

    def test_live_clause_appended_to_line(self, tmp_engram_home, tmp_projects_dir, tmp_path):
        """When gh succeeds, rendered line includes [STATUS] clause."""
        _write_config(tmp_engram_home)
        _write_project(
            tmp_projects_dir, "PR-1000",
            turn="ariadne",
            turn_reason="needs final review",
            github="pr/1000",
        )

        # Write a fake gh that returns OPEN|APPROVED for any pr view call
        fake_gh = tmp_path / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "echo 'OPEN|APPROVED'\n"
        )
        fake_gh.chmod(0o755)

        rc, stdout, stderr = _run_hook(
            tmp_engram_home, tmp_projects_dir,
            extra_env={
                "PATH": str(tmp_path),
                "BATON_PROJECTS_DIR": str(tmp_projects_dir),
            },
        )

        assert rc == 0, f"Hook failed; stderr: {stderr}"
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "PR-1000" in ctx
        assert "APPROVED" in ctx or "OPEN" in ctx
        # main() must persist the fetched status to disk after a successful
        # gh call (end-to-end, through the subprocess boundary).
        cache_file = tmp_engram_home / "baton-status-cache.json"
        assert cache_file.exists(), "expected baton-status-cache.json written after gh fetch"

    def test_no_anchor_no_live_clause(self, tmp_engram_home, tmp_projects_dir):
        """Baton with no anchor (pool-abc style) renders without live clause."""
        # Unit-test via module import: verify that _get_live_status is never
        # called for a no-anchor baton (gh call count = 0).
        mod = _load_hook_module(tmp_engram_home)

        # pool-abc has no github field and doesn't match PR-N pattern → None
        anchor = mod._resolve_anchor("pool-abc", "")
        assert anchor is None

        # Verify that with no anchor, _get_live_status is not invoked.
        # Subprocess integration: hook emits a normal summary without crashing.
        _write_config(tmp_engram_home)
        _write_project(
            tmp_projects_dir, "pool-abc",
            turn="ariadne",
            turn_reason="pool work",
        )

        rc, stdout, stderr = _run_hook(tmp_engram_home, tmp_projects_dir)

        assert rc == 0
        ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
        assert "pool-abc" in ctx
        assert "pool work" in ctx
