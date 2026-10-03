"""SessionStart JSON1 probe (issue #3327).

After alpha#3326, `engram_core.generalization_chain` needs SQLite JSON1
(`json_each`). On a build without it the lesson tripwire hook returns "" on
any failure (its never-blocks contract), so the "[triggered principles]"
section would vanish silently. `_check_json1` makes that case a NAMED line
in the session-start substrate-health output.

Contract (each line is pinned below):
  J1  three-state probe on a FRESH in-memory connection, positive control
      (`SELECT count(*) FROM json_each('[1,2]')` must equal 2).
  J2  "ok" adds nothing to the health output.
  J3  "missing" (OperationalError naming json_each / no such function):
      WARNING line inside the existing warning block, naming both versions,
      the silently-absent section, a remedy, and what was NOT checked.
      2-3 lines total.
  J4  anything else -> informational "unknown" line (not a warning), never ok.
  J5  never raises.
  J6  connection factory injectable.
  J7  everything else unchanged (the pre-existing session-start suites).
"""

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(monkeypatch, engram_home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)
    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _defer_mcp_probe(monkeypatch, mod, tmp_path: Path):
    """platform=codex defers the MCP pgrep probe (same helper idea as
    test_session_start_hook_viz_probe.py) so only the JSON1 wiring varies."""
    runtime_root = tmp_path / "plugin-runtime"
    hooks_dir = runtime_root / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_path = hooks_dir / "engram-session-start-hook.py"
    hook_path.write_text("# test hook path\n")
    (runtime_root / "platform.json").write_text(json.dumps({"platform": "codex"}))
    monkeypatch.setattr(mod, "__file__", str(hook_path))


def _run_main(mod, tmp_path: Path) -> str:
    stdin_data = json.dumps(
        {
            "session_id": "s-json1",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        }
    )
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            mod.main()
    return json.loads(buf.getvalue().strip())["hookSpecificOutput"]["additionalContext"]


class _StubConn:
    """Connection whose execute() either raises or returns a canned row."""

    def __init__(self, raises=None, count=2):
        self._raises = raises
        self._count = count
        self.closed = False
        self.sql = []

    def execute(self, sql, *args):
        self.sql.append(sql)
        if self._raises is not None:
            raise self._raises
        cur = MagicMock()
        cur.fetchone.return_value = (self._count,)
        return cur

    def close(self):
        self.closed = True


def _factory(conn=None, raises=None):
    def connect(*a, **k):
        if raises is not None:
            raise raises
        return conn

    return connect


@pytest.fixture
def mod(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    (home / "sessions").mkdir(parents=True)
    return _load_hook_module(monkeypatch, home)


# ---------------------------------------------------------------- J1 / ok


def test_ok_on_real_sqlite(mod):
    assert mod._check_json1() == ("ok", None)


def test_probe_uses_positive_control_query(mod):
    conn = _StubConn(count=2)
    assert mod._check_json1(connect=_factory(conn)) == ("ok", None)
    assert any("json_each('[1,2]')" in s for s in conn.sql)
    assert conn.closed


def test_probe_opens_in_memory_only_and_never_touches_engram_home(
    tmp_path, monkeypatch
):
    missing_home = tmp_path / "does-not-exist"
    mod = _load_hook_module(monkeypatch, missing_home)
    seen = []

    def recording_connect(*a, **k):
        seen.append(a[0] if a else k.get("database"))
        return sqlite3.connect(*a, **k)

    assert mod._check_json1(connect=recording_connect) == ("ok", None)
    assert seen == [":memory:"]
    assert not missing_home.exists()


# ---------------------------------------------------------------- J3 / missing


@pytest.mark.parametrize(
    "msg", ["no such table: json_each", "no such function: json_each"]
)
def test_missing_on_json_each_unavailable(mod, msg):
    conn = _StubConn(raises=sqlite3.OperationalError(msg))
    status, reason = mod._check_json1(connect=_factory(conn))
    assert status == "missing"
    # reason carries both versions actually probed
    assert sqlite3.sqlite_version in reason
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in reason
    assert conn.closed


def test_missing_on_no_such_function_other_json_name(mod):
    conn = _StubConn(raises=sqlite3.OperationalError("no such function: json_valid"))
    assert mod._check_json1(connect=_factory(conn))[0] == "missing"


# ---------------------------------------------------------------- J4 / unknown


@pytest.mark.parametrize(
    "conn",
    [
        _StubConn(raises=sqlite3.OperationalError("database is locked")),
        _StubConn(raises=RuntimeError("boom")),
        _StubConn(count=0),
        _StubConn(count=3),
    ],
    ids=["other-operational-error", "runtime-error", "count-0", "count-3"],
)
def test_unknown_for_everything_else(mod, conn):
    status, reason = mod._check_json1(connect=_factory(conn))
    assert status == "unknown"
    assert reason


def test_unknown_when_connect_raises(mod):
    status, reason = mod._check_json1(
        connect=_factory(raises=sqlite3.OperationalError("unable to open"))
    )
    assert status == "unknown"
    assert reason


def test_unknown_when_connect_raises_json_looking_error(mod):
    # A connect failure is never evidence about JSON1, even if the text
    # happens to mention json_each.
    status, _ = mod._check_json1(
        connect=_factory(raises=sqlite3.OperationalError("no such table: json_each"))
    )
    assert status == "unknown"


def test_unknown_when_row_shape_unexpected(mod):
    conn = _StubConn()
    conn.execute = lambda sql, *a: MagicMock(fetchone=MagicMock(return_value=None))
    assert mod._check_json1(connect=_factory(conn))[0] == "unknown"


# ---------------------------------------------------------------- J5 / never raises


@pytest.mark.parametrize(
    "exc", [MemoryError("x"), ValueError("v"), OSError("o"), TypeError("t")]
)
def test_never_raises(mod, exc):
    status, reason = mod._check_json1(connect=_factory(raises=exc))
    assert status in ("ok", "missing", "unknown")
    assert status != "ok"


# ---------------------------------------------------------------- main() wiring


def test_main_ok_emits_nothing(tmp_path, monkeypatch, mod):
    _defer_mcp_probe(monkeypatch, mod, tmp_path)
    ctx = _run_main(mod, tmp_path)  # real sqlite, JSON1 present
    assert "JSON1" not in ctx
    assert "json_each" not in ctx
    assert "triggered principles" not in ctx
    assert "ENGRAM substrate health" not in ctx


def _json1_lines(ctx: str):
    return [
        ln for ln in ctx.splitlines()
        if "JSON1" in ln or "triggered principles" in ln or "Not checked" in ln
    ]


def test_main_missing_is_warning_inside_block(tmp_path, monkeypatch, mod):
    _defer_mcp_probe(monkeypatch, mod, tmp_path)
    reason = "Python 3.9.6, SQLite 3.30.0: no such table: json_each"
    monkeypatch.setattr(mod, "_check_json1", lambda *a, **k: ("missing", reason))
    ctx = _run_main(mod, tmp_path)
    lines = ctx.splitlines()
    header = next(i for i, ln in enumerate(lines) if ln.startswith("⚠️  ENGRAM substrate health:"))
    assert lines[header + 1].startswith("  JSON1: MISSING")
    assert "Python 3.9.6" in lines[header + 1] and "SQLite 3.30.0" in lines[header + 1]
    assert lines[header + 2].startswith("  → ")
    assert "lesson tripwires" in lines[header + 2]
    assert "[triggered principles]" in lines[header + 2]
    assert "silently absent" in lines[header + 2]
    assert "not checked" in lines[header + 3].lower()
    assert "MCP server" in lines[header + 3]
    # no informational duplicate of the same finding
    assert "ℹ️  ENGRAM substrate health: JSON1" not in ctx


def test_main_missing_added_lines_bounded(tmp_path, monkeypatch, mod):
    _defer_mcp_probe(monkeypatch, mod, tmp_path)
    monkeypatch.setattr(
        mod, "_check_json1", lambda *a, **k: ("missing", "Python 3.9.6, SQLite 3.30.0: x")
    )
    ctx = _run_main(mod, tmp_path)
    added = [ln for ln in ctx.splitlines() if ln.startswith("  JSON1") or ln.startswith("  →") or "Not checked" in ln]
    assert 2 <= len(_json1_lines(ctx)) <= 3
    assert all(len(ln) <= 260 for ln in _json1_lines(ctx))
    assert added


def test_main_unknown_is_info_not_warning(tmp_path, monkeypatch, mod):
    _defer_mcp_probe(monkeypatch, mod, tmp_path)
    monkeypatch.setattr(
        mod, "_check_json1", lambda *a, **k: ("unknown", "database is locked")
    )
    ctx = _run_main(mod, tmp_path)
    assert "ℹ️  ENGRAM substrate health: JSON1 probe: unknown (database is locked)" in ctx
    assert "⚠️" not in ctx
    assert "MISSING" not in ctx
    assert len(_json1_lines(ctx)) == 1


def test_main_unknown_reason_is_bounded(tmp_path, monkeypatch, mod):
    _defer_mcp_probe(monkeypatch, mod, tmp_path)
    monkeypatch.setattr(mod, "_check_json1", lambda *a, **k: ("unknown", "x" * 5000))
    ctx = _run_main(mod, tmp_path)
    assert all(len(ln) <= 260 for ln in _json1_lines(ctx))
    assert "x" * 1000 not in ctx
