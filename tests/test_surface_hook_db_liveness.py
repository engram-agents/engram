"""Tests for the DB liveness check in engram-surface-hook.py.

Verifies that _check_db_liveness returns (False, reason) when the SQLite
file is absent or corrupt, and that main() surfaces the ⚠️ SUBSTRATE DOWN
warning when the probe fails. See #1218 (surface serves stale daemon cache
under complete DB failure).
"""

import importlib.util
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"


def _load_hook_module(monkeypatch=None, engram_home: Path | None = None):
    """Import the surface hook as a module despite the hyphenated filename."""
    if engram_home and monkeypatch:
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_surface_hook", None)
    spec = importlib.util.spec_from_file_location("engram_surface_hook", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCheckDbLiveness:
    """Unit tests for _check_db_liveness."""

    def test_missing_db_returns_false(self, tmp_path):
        """Absent knowledge.db → False with 'not found' reason."""
        mod = _load_hook_module()
        ok, reason = mod._check_db_liveness(str(tmp_path / "knowledge.db"))
        assert ok is False
        assert "not found" in reason

    def test_valid_db_returns_ok(self, tmp_path):
        """Valid SQLite DB → True."""
        db_path = tmp_path / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.close()
        mod = _load_hook_module()
        ok, reason = mod._check_db_liveness(str(db_path))
        assert ok is True
        assert reason == "ok"

    def test_corrupt_db_returns_false(self, tmp_path):
        """Non-SQLite file (corrupted bytes) → False with SQLite error reason."""
        db_path = tmp_path / "knowledge.db"
        db_path.write_bytes(b"not a sqlite database at all!")
        mod = _load_hook_module()
        ok, reason = mod._check_db_liveness(str(db_path))
        assert ok is False
        assert reason  # non-empty error string

    def test_truncated_sqlite_header_returns_false(self, tmp_path):
        """Partial SQLite header (16 bytes) → False (not a valid database)."""
        db_path = tmp_path / "knowledge.db"
        # SQLite header is 100 bytes; 16 bytes is enough to confuse the parser
        db_path.write_bytes(b"SQLite format 3\x00")
        mod = _load_hook_module()
        ok, reason = mod._check_db_liveness(str(db_path))
        assert ok is False


class TestDbLivenessIntegration:
    """Integration test: main() wires the DB warning into hook output."""

    def _run_main(self, mod, monkeypatch, prompt: str = "hello") -> dict:
        """Run mod.main() with stubbed deps and return parsed hook output."""
        monkeypatch.setattr(
            mod, "query_daemon",
            lambda *a, **kw: {"matched_ids": [], "match_count": 0, "nodes": []},
        )
        monkeypatch.setattr(mod, "_get_auto_surface_config",
                            lambda: {"idf_gate_enabled": False,
                                     "short_prompt_threshold_chars": 0,
                                     "prev_response_tail_chars": 0})
        monkeypatch.setattr(mod, "check_incident_tripwire", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
        monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: "")
        monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
        monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
        monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
        monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
        monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
        # Suppress MCP liveness warning (not under test here)
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

    def test_main_emits_db_warning_when_db_missing(self, tmp_path, monkeypatch):
        """main() must surface DB FAILED in additionalContext when DB is absent."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        # knowledge.db deliberately not created
        mod = _load_hook_module(monkeypatch, engram_home=engram_home)

        output = self._run_main(mod, monkeypatch)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "DB liveness: FAILED" in ctx
        assert "not found" in ctx

    def test_main_no_db_warning_when_db_ok(self, tmp_path, monkeypatch):
        """main() must NOT emit DB warning when knowledge.db is readable."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        db_path = engram_home / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.close()
        mod = _load_hook_module(monkeypatch, engram_home=engram_home)

        output = self._run_main(mod, monkeypatch)
        # When parts is empty, main outputs {} without hookSpecificOutput
        ctx = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "DB liveness: FAILED" not in ctx
