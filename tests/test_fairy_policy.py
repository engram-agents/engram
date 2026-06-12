"""Tests for the fairy-policy config + SessionStart hook integration.

Covers:
1. Schema metadata: both keys present, editable=True, restart_required=False,
   tier=TIER_A, tooltip present, allowed_values=["explicit","auto","always"].
2. Default config: server.py _ensure_data_dir default_config includes both
   keys with value "auto".
3. Hook integration (startup): both mode-lines appear in startup
   additionalContext; neither appears in compact/resume; non-dict-config
   shapes return empty silently (null, [], "x", missing-field).
4. Mode-line content: each of 3 values produces the correct mode-line text.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

_HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

from tools.config_schema import SCHEMA, TIER_A  # noqa: E402


def _schema_by_key() -> dict[str, dict]:
    return {row["key"]: row for row in SCHEMA}


# ---------------------------------------------------------------------------
# 1. Schema metadata
# ---------------------------------------------------------------------------

class TestFairyPolicySchemaEntries:
    """Both fairy-policy keys must be present with correct metadata."""

    def test_coder_fairy_policy_in_schema(self):
        by_key = _schema_by_key()
        assert "coder_fairy_policy" in by_key, "coder_fairy_policy missing from SCHEMA"

    def test_reviewer_fairy_policy_in_schema(self):
        by_key = _schema_by_key()
        assert "reviewer_fairy_policy" in by_key, "reviewer_fairy_policy missing from SCHEMA"

    def test_coder_fairy_policy_editable(self):
        row = _schema_by_key()["coder_fairy_policy"]
        assert row.get("editable") is True, (
            f"coder_fairy_policy editable={row.get('editable')!r}; expected True"
        )

    def test_reviewer_fairy_policy_editable(self):
        row = _schema_by_key()["reviewer_fairy_policy"]
        assert row.get("editable") is True, (
            f"reviewer_fairy_policy editable={row.get('editable')!r}; expected True"
        )

    def test_coder_fairy_policy_not_restart_required(self):
        row = _schema_by_key()["coder_fairy_policy"]
        assert row.get("restart_required") is False, (
            f"coder_fairy_policy restart_required={row.get('restart_required')!r}; expected False"
        )

    def test_reviewer_fairy_policy_not_restart_required(self):
        row = _schema_by_key()["reviewer_fairy_policy"]
        assert row.get("restart_required") is False, (
            f"reviewer_fairy_policy restart_required={row.get('restart_required')!r}; expected False"
        )

    def test_coder_fairy_policy_tier_a(self):
        row = _schema_by_key()["coder_fairy_policy"]
        assert row.get("tier") == TIER_A, (
            f"coder_fairy_policy tier={row.get('tier')!r}; expected TIER_A={TIER_A!r}"
        )

    def test_reviewer_fairy_policy_tier_a(self):
        row = _schema_by_key()["reviewer_fairy_policy"]
        assert row.get("tier") == TIER_A, (
            f"reviewer_fairy_policy tier={row.get('tier')!r}; expected TIER_A={TIER_A!r}"
        )

    def test_coder_fairy_policy_has_tooltip(self):
        row = _schema_by_key()["coder_fairy_policy"]
        tooltip = row.get("tooltip")
        assert tooltip and len(tooltip) > 10, (
            f"coder_fairy_policy tooltip missing or too short: {tooltip!r}"
        )

    def test_reviewer_fairy_policy_has_tooltip(self):
        row = _schema_by_key()["reviewer_fairy_policy"]
        tooltip = row.get("tooltip")
        assert tooltip and len(tooltip) > 10, (
            f"reviewer_fairy_policy tooltip missing or too short: {tooltip!r}"
        )

    def test_coder_fairy_policy_allowed_values(self):
        row = _schema_by_key()["coder_fairy_policy"]
        assert row.get("allowed_values") == ["explicit", "auto", "always"], (
            f"coder_fairy_policy allowed_values={row.get('allowed_values')!r}"
        )

    def test_reviewer_fairy_policy_allowed_values(self):
        row = _schema_by_key()["reviewer_fairy_policy"]
        assert row.get("allowed_values") == ["explicit", "auto", "always"], (
            f"reviewer_fairy_policy allowed_values={row.get('allowed_values')!r}"
        )

    def test_coder_fairy_policy_control_is_select(self):
        row = _schema_by_key()["coder_fairy_policy"]
        assert row.get("control") == "select", (
            f"coder_fairy_policy control={row.get('control')!r}; expected 'select'"
        )

    def test_reviewer_fairy_policy_control_is_select(self):
        row = _schema_by_key()["reviewer_fairy_policy"]
        assert row.get("control") == "select", (
            f"reviewer_fairy_policy control={row.get('control')!r}; expected 'select'"
        )


# ---------------------------------------------------------------------------
# 2. Default config (_ensure_data_dir in server.py)
# ---------------------------------------------------------------------------

class TestFairyPolicyDefaultConfig:
    """server.py _ensure_data_dir default_config must include both keys as "auto".

    Test approach: manipulate the module-level DATA_DIR and CONFIG_PATH globals
    to point at a temp dir, call _ensure_data_dir(), then read back the written
    config.json. This mirrors how server.py wires these paths at runtime.
    """

    def _get_default_config(self, tmp: Path) -> dict:
        """Write default config to tmp dir and return it."""
        import server as _server  # noqa: E402 (repo root is in sys.path)
        orig_data_dir = _server.core.DATA_DIR
        orig_config_path = _server.core.CONFIG_PATH
        try:
            _server.core.DATA_DIR = tmp
            _server.core.CONFIG_PATH = tmp / "config.json"
            _server._ensure_data_dir()
            return json.loads((tmp / "config.json").read_text())
        finally:
            _server.core.DATA_DIR = orig_data_dir
            _server.core.CONFIG_PATH = orig_config_path

    def test_default_config_has_coder_fairy_policy(self, tmp_path):
        config = self._get_default_config(tmp_path)
        assert "coder_fairy_policy" in config, "coder_fairy_policy absent from default_config"
        assert config["coder_fairy_policy"] == "auto", (
            f"coder_fairy_policy default={config['coder_fairy_policy']!r}; expected 'auto'"
        )

    def test_default_config_has_reviewer_fairy_policy(self, tmp_path):
        config = self._get_default_config(tmp_path)
        assert "reviewer_fairy_policy" in config, "reviewer_fairy_policy absent from default_config"
        assert config["reviewer_fairy_policy"] == "auto", (
            f"reviewer_fairy_policy default={config['reviewer_fairy_policy']!r}; expected 'auto'"
        )


# ---------------------------------------------------------------------------
# Hook test helpers
# ---------------------------------------------------------------------------

def _load_hook_module(engram_home: Path):
    """Load the session-start hook with ENGRAM_HOME pointing at engram_home."""
    os.environ["ENGRAM_HOME"] = str(engram_home)
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in ("engram-session-start-hook", "engram_session_start_hook"):
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_hook_main(module, hook_input: dict) -> dict:
    """Run module.main() with hook_input on stdin; return parsed output JSON."""
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def _make_engram_home(tmp_path: Path) -> Path:
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()
    return engram_home


def _write_config(engram_home: Path, config: dict) -> None:
    (engram_home / "config.json").write_text(json.dumps(config))


def _mock_no_engram_client():
    return patch.dict("sys.modules", {"engram_client": None})


# ---------------------------------------------------------------------------
# 3. Hook integration — startup fires, compact/resume does not
# ---------------------------------------------------------------------------

class TestFairyPolicyHookIntegration:
    """fairy_policy_block() fires on startup, not on compact/resume."""

    def test_startup_emits_coder_mode_line(self, tmp_path):
        """On source==startup + interactive, coder policy line appears in additionalContext."""
        engram_home = _make_engram_home(tmp_path)
        _write_config(engram_home, {"coder_fairy_policy": "auto", "reviewer_fairy_policy": "auto"})
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, {
                    "session_id": "test-fairy-startup",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "source": "startup",
                })

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Coder fairy policy" in ctx, f"Coder policy line missing from startup context: {ctx!r}"

    def test_startup_emits_reviewer_mode_line(self, tmp_path):
        """On source==startup + interactive, reviewer policy line appears in additionalContext."""
        engram_home = _make_engram_home(tmp_path)
        _write_config(engram_home, {"coder_fairy_policy": "auto", "reviewer_fairy_policy": "auto"})
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, {
                    "session_id": "test-fairy-startup",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "source": "startup",
                })

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Reviewer fairy policy" in ctx, f"Reviewer policy line missing from startup context: {ctx!r}"

    def test_compact_does_not_emit_policy_lines(self, tmp_path):
        """On source==compact, no fairy policy lines appear in additionalContext."""
        engram_home = _make_engram_home(tmp_path)
        _write_config(engram_home, {"coder_fairy_policy": "auto", "reviewer_fairy_policy": "auto"})
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, {
                    "session_id": "test-fairy-compact",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "source": "compact",
                })

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Coder fairy policy" not in ctx, (
            f"Coder policy line should NOT appear on compact: {ctx!r}"
        )
        assert "Reviewer fairy policy" not in ctx, (
            f"Reviewer policy line should NOT appear on compact: {ctx!r}"
        )

    def test_resume_does_not_emit_policy_lines(self, tmp_path):
        """On source==resume, no fairy policy lines appear in additionalContext."""
        engram_home = _make_engram_home(tmp_path)
        _write_config(engram_home, {"coder_fairy_policy": "auto", "reviewer_fairy_policy": "auto"})
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, {
                    "session_id": "test-fairy-resume",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "source": "resume",
                })

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Coder fairy policy" not in ctx, (
            f"Coder policy line should NOT appear on resume: {ctx!r}"
        )

    # ---- Defensive shapes ----

    def _startup_ctx(self, tmp_path: Path, config_content) -> str:
        """Helper: write raw config_content as config.json, run startup, return ctx."""
        engram_home = _make_engram_home(tmp_path)
        (engram_home / "config.json").write_text(
            json.dumps(config_content) if not isinstance(config_content, str)
            else config_content
        )
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"
        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, {
                    "session_id": "test",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "source": "startup",
                })
        return result["hookSpecificOutput"]["additionalContext"]

    def test_null_config_silent(self, tmp_path):
        """config.json containing null must not crash the hook."""
        ctx = self._startup_ctx(tmp_path, None)
        # Hook should still produce output (other blocks), just no crash.
        # Policy lines may be absent — that's fine.
        assert isinstance(ctx, str)

    def test_list_config_silent(self, tmp_path):
        """config.json containing [] must not crash the hook."""
        ctx = self._startup_ctx(tmp_path, [])
        assert isinstance(ctx, str)

    def test_string_config_silent(self, tmp_path):
        """config.json containing "x" (a bare string) must not crash the hook."""
        engram_home = _make_engram_home(tmp_path)
        (engram_home / "config.json").write_text('"x"')
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"
        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, {
                    "session_id": "test",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "source": "startup",
                })
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert isinstance(ctx, str)

    def test_missing_config_file_silent(self, tmp_path):
        """Missing config.json must not crash the hook (OSError path)."""
        engram_home = _make_engram_home(tmp_path)
        # Do NOT write a config.json — let the hook hit OSError.
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"
        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, {
                    "session_id": "test",
                    "transcript_path": str(tmp_path / "t.jsonl"),
                    "source": "startup",
                })
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert isinstance(ctx, str)


# ---------------------------------------------------------------------------
# 4. Mode-line content — each value produces the correct text
# ---------------------------------------------------------------------------

class TestFairyPolicyModeLines:
    """fairy_policy_block() must emit the correct text per policy value."""

    def _get_block(
        self,
        tmp_path: Path,
        coder: str,
        reviewer: str,
        *,
        primary_user: str | None = "borges",
        primary_user_raw_value: object = ...,  # use sentinel; allows passing None/non-string
    ) -> str:
        """Load hook, set config, call fairy_policy_block() directly.

        primary_user: a string (or None to omit from config). Default "borges" gives
            tests a stable name that's distinct from the safe-default "you".
        primary_user_raw_value: when not the sentinel, set primary_user to this raw
            value (used to test non-string coercion: null, int, etc.).
        """
        engram_home = _make_engram_home(tmp_path)
        config: dict[str, object] = {
            "coder_fairy_policy": coder,
            "reviewer_fairy_policy": reviewer,
        }
        if primary_user_raw_value is not ...:
            config["primary_user"] = primary_user_raw_value
        elif primary_user is not None:
            config["primary_user"] = primary_user
        _write_config(engram_home, config)
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"
        return mod.fairy_policy_block()

    def test_coder_auto_contains_skill_name(self, tmp_path):
        block = self._get_block(tmp_path, "auto", "auto")
        assert "engram-auto-coder-fairy-judgement" in block, (
            f"auto mode should name the skill: {block!r}"
        )

    def test_coder_explicit_line(self, tmp_path):
        block = self._get_block(tmp_path, "explicit", "auto")
        assert "Coder fairy policy: explicit" in block, (
            f"explicit mode label missing: {block!r}"
        )

    def test_coder_always_line(self, tmp_path):
        block = self._get_block(tmp_path, "always", "auto")
        assert "Coder fairy policy: always" in block, (
            f"always mode label missing: {block!r}"
        )

    def test_reviewer_auto_contains_skill_name(self, tmp_path):
        block = self._get_block(tmp_path, "auto", "auto")
        assert "engram-auto-reviewer-fairy-judgement" in block, (
            f"reviewer auto mode should name the skill: {block!r}"
        )

    def test_reviewer_explicit_line(self, tmp_path):
        block = self._get_block(tmp_path, "auto", "explicit")
        assert "Reviewer fairy policy: explicit" in block, (
            f"reviewer explicit mode label missing: {block!r}"
        )

    def test_reviewer_always_line(self, tmp_path):
        block = self._get_block(tmp_path, "auto", "always")
        assert "Reviewer fairy policy: always" in block, (
            f"reviewer always mode label missing: {block!r}"
        )

    def test_block_has_two_lines(self, tmp_path):
        """fairy_policy_block returns exactly 2 non-empty lines (one per policy)."""
        block = self._get_block(tmp_path, "auto", "auto")
        lines = [l for l in block.splitlines() if l.strip()]
        assert len(lines) == 2, (
            f"Expected 2 non-empty lines from fairy_policy_block, got {len(lines)}: {block!r}"
        )

    def test_explicit_mode_substitutes_primary_user(self, tmp_path):
        """When primary_user is set, the explicit-mode text contains the user's name,
        not the literal '{user_name}' placeholder."""
        block = self._get_block(
            tmp_path, "explicit", "explicit", primary_user="lin"
        )
        assert "lin explicitly invokes" in block, (
            f"Expected primary_user 'lin' substituted in explicit block: {block!r}"
        )
        assert "{user_name}" not in block, (
            f"Literal '{{user_name}}' placeholder leaked into block: {block!r}"
        )
        assert "{primary_user}" not in block, (
            f"Literal '{{primary_user}}' placeholder leaked into block: {block!r}"
        )

    def test_explicit_mode_falls_back_to_you_when_primary_user_missing(self, tmp_path):
        """When primary_user is absent from config, 'you' is the safe default."""
        block = self._get_block(
            tmp_path, "explicit", "explicit", primary_user=None
        )
        assert "you explicitly invokes" in block, (
            f"Expected fallback 'you' when primary_user absent: {block!r}"
        )
        assert "{user_name}" not in block

    def test_explicit_mode_null_primary_user_falls_back_to_you(self, tmp_path):
        """A null primary_user (config has the key but value is None) coerces to 'you'."""
        block = self._get_block(
            tmp_path, "explicit", "explicit", primary_user_raw_value=None
        )
        assert "you explicitly invokes" in block, f"null primary_user fallback: {block!r}"

    def test_explicit_mode_numeric_primary_user_falls_back_to_you(self, tmp_path):
        """An int primary_user (config corrupted to a number) coerces to 'you' rather than crashing."""
        block = self._get_block(
            tmp_path, "explicit", "explicit", primary_user_raw_value=123
        )
        assert "you explicitly invokes" in block, f"int primary_user fallback: {block!r}"

    def test_explicit_mode_empty_string_primary_user_falls_back_to_you(self, tmp_path):
        """An empty-string primary_user is falsy and falls back to 'you'."""
        block = self._get_block(
            tmp_path, "explicit", "explicit", primary_user=""
        )
        assert "you explicitly invokes" in block, f"empty primary_user fallback: {block!r}"

    def test_unhashable_coder_policy_falls_back_to_auto(self, tmp_path):
        """A list/dict value for coder_fairy_policy (hand-edited config) must coerce to
        'auto' rather than crashing dict.get with TypeError: unhashable type.

        Per Ari fresh-eye catch on round-2 — the dispatch lives outside the config-read
        try/except, so the isinstance check is the safety net for never-break-session-start.
        """
        engram_home = _make_engram_home(tmp_path)
        _write_config(engram_home, {
            "coder_fairy_policy": [],  # unhashable; dict.get([]) would raise
            "reviewer_fairy_policy": "auto",
            "primary_user": "lin",
        })
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"
        # Must not raise; must return the "auto" line for coder.
        block = mod.fairy_policy_block()
        assert "Coder fairy policy: auto" in block, (
            f"Unhashable coder policy should coerce to auto: {block!r}"
        )
        assert "Reviewer fairy policy: auto" in block

    def test_unhashable_reviewer_policy_falls_back_to_auto(self, tmp_path):
        """Same as the coder case but for reviewer_fairy_policy (dict value this time)."""
        engram_home = _make_engram_home(tmp_path)
        _write_config(engram_home, {
            "coder_fairy_policy": "auto",
            "reviewer_fairy_policy": {"nested": "dict"},  # unhashable
            "primary_user": "lin",
        })
        mod = _load_hook_module(engram_home)
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"
        block = mod.fairy_policy_block()
        assert "Reviewer fairy policy: auto" in block, (
            f"Unhashable reviewer policy should coerce to auto: {block!r}"
        )
        assert "Coder fairy policy: auto" in block

    def test_non_interactive_role_returns_empty(self, tmp_path):
        """fairy_policy_block returns '' for non-interactive role.

        SESSION_ROLE is a module-level constant set at import time from
        ENGRAM_SESSION_ROLE env var — must set the env var BEFORE loading.
        """
        engram_home = _make_engram_home(tmp_path)
        _write_config(engram_home, {"coder_fairy_policy": "auto", "reviewer_fairy_policy": "auto"})
        os.environ["ENGRAM_SESSION_ROLE"] = "heartbeat"
        mod = _load_hook_module(engram_home)
        result = mod.fairy_policy_block()
        # Restore interactive for other tests
        os.environ["ENGRAM_SESSION_ROLE"] = "interactive"
        assert result == "", f"Non-interactive role should return '': {result!r}"
