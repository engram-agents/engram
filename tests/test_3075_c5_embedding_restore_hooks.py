"""Contract-first tests for #3075 C5 round 2's hook-surfacing contract
(Borges's design ruling on engram-alpha#3076): the embedding
restore-completeness flag (engram_core.py's embedding_restore_state /
_set_embedding_restore_flag / _get_embedding_restore_flag) must be surfaced
by BOTH the UserPromptSubmit surface hook and the SessionStart hook, every
turn, until cleared.

Written BEFORE the hook-side implementation, per this repo's
contract-first-tests convention (CLAUDE.md). Tests the hooks' own
output-producing functions directly (_check_embedding_restore_flag in each
hook file) rather than shelling out to the hook script, per this repo's
established pattern (see tests/test_surface_hook_db_liveness.py's own
TestCheckDbLiveness / TestDbLivenessIntegration split, which this file
mirrors).

Judgment call (documented in the PR handoff): a full main()-integration test
is added for the surface hook (lighter dependency surface, already-stubbed
in test_surface_hook_db_liveness.py's own pattern) but NOT for the
session-start hook (main() there pulls in _check_mcp_health's pgrep/
subprocess calls and several other probes — heavier to stub reliably here);
the session-start hook is covered by a direct unit test on
_check_embedding_restore_flag only, which is the function this contract is
actually about.
"""

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SURFACE_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
_SESSION_START_HOOK_PATH = (
    PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
)

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_surface_hook_module():
    sys.modules.pop("engram_surface_hook", None)
    spec = importlib.util.spec_from_file_location("engram_surface_hook", str(_SURFACE_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_session_start_hook_module():
    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location(
        "engram_session_start_hook", str(_SESSION_START_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db_with_flag(db_path: Path, *, not_fully_restored: bool, null_count: int = 0,
                        cause: str | None = None) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE embedding_restore_state (
            singleton_key       INTEGER PRIMARY KEY CHECK (singleton_key = 1),
            not_fully_restored  INTEGER NOT NULL DEFAULT 0,
            null_count          INTEGER NOT NULL DEFAULT 0,
            cause               TEXT,
            updated_at          TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO embedding_restore_state "
        "(singleton_key, not_fully_restored, null_count, cause, updated_at) "
        "VALUES (1, ?, ?, ?, '2026-09-23T00:00:00Z')",
        (1 if not_fully_restored else 0, null_count, cause),
    )
    conn.commit()
    conn.close()


class TestSurfaceHookCheckEmbeddingRestoreFlag:
    """Unit tests for engram-surface-hook.py's _check_embedding_restore_flag."""

    def test_missing_db_returns_true(self, tmp_path):
        mod = _load_surface_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(tmp_path / "knowledge.db"))
        assert ok is True
        assert msg is None

    def test_no_table_returns_true(self, tmp_path):
        """DB exists but predates this feature (no embedding_restore_state
        table) -> treated as clear, not a failure."""
        db_path = tmp_path / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE nodes (id TEXT)")
        conn.close()
        mod = _load_surface_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(db_path))
        assert ok is True
        assert msg is None

    def test_flag_clear_returns_true(self, tmp_path):
        db_path = tmp_path / "knowledge.db"
        _make_db_with_flag(db_path, not_fully_restored=False)
        mod = _load_surface_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(db_path))
        assert ok is True
        assert msg is None

    def test_flag_set_returns_false_with_count_cause_remedy(self, tmp_path):
        db_path = tmp_path / "knowledge.db"
        _make_db_with_flag(
            db_path, not_fully_restored=True, null_count=7, cause="embedder-unavailable",
        )
        mod = _load_surface_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(db_path))
        assert ok is False
        assert "7" in msg
        assert "embedder-unavailable" in msg
        assert "NOT FULLY RESTORED" in msg
        assert "engram-regenerate-embeddings.py" in msg


class TestSurfaceHookMainIntegration:
    """Integration: main() wires the embedding-restore warning into
    additionalContext, mirroring test_surface_hook_db_liveness.py's own
    TestDbLivenessIntegration harness."""

    def _run_main(self, mod, monkeypatch, prompt: str = "hello") -> dict:
        monkeypatch.setattr(
            mod, "query_daemon",
            lambda *a, **kw: {"matched_ids": [], "match_count": 0, "nodes": []},
        )
        monkeypatch.setattr(mod, "_get_auto_surface_config",
                            lambda: {"idf_gate_enabled": False,
                                     "short_prompt_threshold_chars": 0,
                                     "prev_response_tail_chars": 0})
        monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: ("", []))
        monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
        monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: "")
        monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
        monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
        monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
        monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
        monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
        monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))

        stdin_data = json.dumps({"prompt": prompt})
        buf = io.StringIO()
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
            with patch("sys.stdout", buf):
                try:
                    mod.main()
                except SystemExit:
                    pass
        return json.loads(buf.getvalue().strip())

    def test_main_emits_embedding_restore_warning_when_flag_set(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        db_path = engram_home / "knowledge.db"
        _make_db_with_flag(
            db_path, not_fully_restored=True, null_count=5, cause="per-node-failures",
        )
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        mod = _load_surface_hook_module()

        output = self._run_main(mod, monkeypatch)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "NOT FULLY RESTORED" in ctx
        assert "5" in ctx
        assert "per-node-failures" in ctx

    def test_main_no_embedding_restore_warning_when_flag_clear(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        db_path = engram_home / "knowledge.db"
        _make_db_with_flag(db_path, not_fully_restored=False)
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        mod = _load_surface_hook_module()

        output = self._run_main(mod, monkeypatch)
        ctx = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "NOT FULLY RESTORED" not in ctx


class TestSessionStartHookCheckEmbeddingRestoreFlag:
    """Unit tests for engram-session-start-hook.py's
    _check_embedding_restore_flag (duplicated by value from the surface
    hook's own version — see that function's docstring)."""

    def test_missing_db_returns_true(self, tmp_path):
        mod = _load_session_start_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(tmp_path))
        assert ok is True
        assert msg is None

    def test_no_table_returns_true(self, tmp_path):
        db_path = tmp_path / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE nodes (id TEXT)")
        conn.close()
        mod = _load_session_start_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(tmp_path))
        assert ok is True
        assert msg is None

    def test_flag_clear_returns_true(self, tmp_path):
        db_path = tmp_path / "knowledge.db"
        _make_db_with_flag(db_path, not_fully_restored=False)
        mod = _load_session_start_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(tmp_path))
        assert ok is True
        assert msg is None

    def test_flag_set_returns_false_with_count_and_cause(self, tmp_path):
        db_path = tmp_path / "knowledge.db"
        _make_db_with_flag(
            db_path, not_fully_restored=True, null_count=12, cause="disabled-by-config",
        )
        mod = _load_session_start_hook_module()
        ok, msg = mod._check_embedding_restore_flag(str(tmp_path))
        assert ok is False
        assert "12" in msg
        assert "disabled-by-config" in msg
