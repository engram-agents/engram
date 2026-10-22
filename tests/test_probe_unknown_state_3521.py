"""Contract-first tests for the advisory-probe tri-state (part of #3521, rows 1-4).

The contract (Kepler's ruling, 2026-10-06): "never block" means a probe must
not block the tool call; it never meant a probe may REPORT HEALTHY when it
could not measure. Each probe's result is tri-state:

    True  = healthy      False = confirmed not-ok      None = UNKNOWN

and `unknown` renders ONE bounded line (the #3504 loader notice is the
template) while the hook still exits 0 and never raises.

Rows covered:
  1. engram-surface-hook.py::_check_embedding_restore_flag(db_path)      (per prompt)
  2. engram-session-start-hook.py::_check_embedding_restore_flag(home)   (per session)
  3. engram-surface-hook.py::_check_mcp_write_tool_marker(home)          (per prompt)
     (+ the pgrep step, _is_any_server_py_running)
  4. engram-stale-tree-warn-hook.py::_behind_count + main()              (per tool call)

Cell classes (the dev-code red/green record is part of the PR handoff):
  * healthy / not-ok cells  -- GREEN on unmodified origin/dev (current behaviour
    is already right there; they pin that the fix does not disturb it);
  * unknown cells           -- RED on unmodified origin/dev (it says healthy);
  * helper / shape cells    -- RED on dev by construction (the new shared
    helpers and the row-4 tuple shape do not exist there).
"""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
_SURFACE_PATH = HOOK_DIR / "engram-surface-hook.py"
_SESSION_PATH = HOOK_DIR / "engram-session-start-hook.py"
_WARN_PATH = HOOK_DIR / "engram-stale-tree-warn-hook.py"
_HOOKLIB_PATH = HOOK_DIR / "_hooklib.py"

STAMP_NAME = "probe-unknown-notice.json"
DEAD_PID = 9999999

_COUNTER = 0

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load(path: Path, name: str):
    global _COUNTER
    _COUNTER += 1
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    spec = importlib.util.spec_from_file_location(f"{name}_{_COUNTER}", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _surface(monkeypatch, home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return _load(_SURFACE_PATH, "surface_hook_3521")


def _session(monkeypatch, home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return _load(_SESSION_PATH, "session_hook_3521")


def _warn(home: Path):
    mod = _load(_WARN_PATH, "warn_hook_3521")
    mod.ENGRAM_HOME = str(home)
    return mod


def _hooklib():
    return _load(_HOOKLIB_PATH, "hooklib_3521")


# ---------------------------------------------------------------------------
# Embedding-flag fixtures (rows 1 and 2)
# ---------------------------------------------------------------------------

def _make_db(db: Path, *, flag: int = 0, null_count: int = 0, cause=None,
             columns: str = None) -> None:
    conn = sqlite3.connect(str(db))
    cols = columns or (
        "singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1), "
        "not_fully_restored INTEGER NOT NULL DEFAULT 0, "
        "null_count INTEGER NOT NULL DEFAULT 0, cause TEXT, updated_at TEXT"
    )
    conn.execute(f"CREATE TABLE embedding_restore_state ({cols})")
    if columns is None:
        conn.execute(
            "INSERT INTO embedding_restore_state VALUES (1, ?, ?, ?, 'x')",
            (flag, null_count, cause),
        )
    else:
        conn.execute("INSERT INTO embedding_restore_state (singleton_key) VALUES (1)")
    conn.commit()
    conn.close()


def _cell_flag_clear(home):
    _make_db(home / "knowledge.db", flag=0)


def _cell_db_absent(home):
    pass


def _cell_table_absent(home):
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("CREATE TABLE nodes (id TEXT)")
    conn.commit()
    conn.close()


def _cell_flag_set(home):
    _make_db(home / "knowledge.db", flag=1, null_count=7, cause="embedder-unavailable")


def _cell_detection_failed(home):
    _make_db(home / "knowledge.db", flag=1, null_count=-1, cause="detection-failed: boom")


def _cell_not_a_database(home):
    (home / "knowledge.db").write_text("this is not a sqlite database\n" * 20)


def _cell_missing_column(home):
    # Table present, but not_fully_restored (a queried column) is absent.
    _make_db(home / "knowledge.db", columns="singleton_key INTEGER PRIMARY KEY, null_count INTEGER, cause TEXT")


HEALTHY_CELLS = {
    "flag_clear": _cell_flag_clear,
    "db_absent": _cell_db_absent,
    "table_absent": _cell_table_absent,
}
NOTOK_CELLS = {
    "flag_set": _cell_flag_set,
    "detection_failed": _cell_detection_failed,
}
UNKNOWN_CELLS = {
    "not_a_database": (_cell_not_a_database, "DatabaseError"),
    "missing_column": (_cell_missing_column, "OperationalError"),
}


@contextlib.contextmanager
def _exclusive_lock(db: Path):
    """A second connection holding BEGIN EXCLUSIVE (rollback-journal mode, the
    default): a reader with a 1.0 s timeout gets `database is locked`."""
    conn = sqlite3.connect(str(db), isolation_level=None, timeout=0)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        try:
            conn.execute("ROLLBACK")
        finally:
            conn.close()


def _flag_probe(which, monkeypatch, home):
    """(callable, module) running the SAME probe on the chosen copy."""
    if which == "surface":
        mod = _surface(monkeypatch, home)
        return (lambda: mod._check_embedding_restore_flag(str(home / "knowledge.db"))), mod
    mod = _session(monkeypatch, home)
    return (lambda: mod._check_embedding_restore_flag(str(home))), mod


COPIES = ["surface", "session"]


@pytest.mark.parametrize("which", COPIES)
class TestEmbeddingFlagMatrix:
    @pytest.mark.parametrize("cell", sorted(HEALTHY_CELLS))
    def test_healthy(self, which, cell, tmp_path, monkeypatch):
        HEALTHY_CELLS[cell](tmp_path)
        probe, _ = _flag_probe(which, monkeypatch, tmp_path)
        assert probe() == (True, None)

    @pytest.mark.parametrize("cell", sorted(NOTOK_CELLS))
    def test_not_ok(self, which, cell, tmp_path, monkeypatch):
        NOTOK_CELLS[cell](tmp_path)
        probe, _ = _flag_probe(which, monkeypatch, tmp_path)
        state, msg = probe()
        assert state is False
        if cell == "flag_set":
            assert "7" in msg and "embedder-unavailable" in msg
        else:
            # existing wording stays NOT-OK: do not demote it to unknown
            assert "UNKNOWN" in msg and "detection-failed" in msg

    @pytest.mark.parametrize("cell", sorted(UNKNOWN_CELLS))
    def test_unknown(self, which, cell, tmp_path, monkeypatch):
        builder, exc_type = UNKNOWN_CELLS[cell]
        builder(tmp_path)
        probe, _ = _flag_probe(which, monkeypatch, tmp_path)
        state, reason = probe()
        assert state is None
        assert reason.startswith(exc_type + ":"), reason

    def test_unknown_locked_database(self, which, tmp_path, monkeypatch):
        _make_db(tmp_path / "knowledge.db", flag=1, null_count=3, cause="x")
        probe, _ = _flag_probe(which, monkeypatch, tmp_path)
        t0 = time.monotonic()
        with _exclusive_lock(tmp_path / "knowledge.db"):
            state, reason = probe()
        assert time.monotonic() - t0 >= 0.9  # the 1.0 s timeout really elapsed
        assert state is None
        assert reason.startswith("OperationalError:") and "locked" in reason
        # control: with the lock released the same DB reads not-ok again
        assert probe()[0] is False

    def test_unknown_other_exception(self, which, tmp_path, monkeypatch):
        _make_db(tmp_path / "knowledge.db", flag=1, null_count=3, cause="x")
        probe, mod = _flag_probe(which, monkeypatch, tmp_path)

        def boom(*a, **kw):
            raise RuntimeError("synthetic probe bug")

        monkeypatch.setattr(mod.sqlite3, "connect", boom)
        state, reason = probe()
        assert state is None
        assert reason.startswith("RuntimeError:")


class TestEmbeddingFlagCopiesDrift:
    """The two copies are duplicated by value; every cell must agree."""

    def test_copies_agree_on_every_cell(self, tmp_path, monkeypatch):
        cells = {}
        cells.update({k: v for k, v in HEALTHY_CELLS.items()})
        cells.update({k: v for k, v in NOTOK_CELLS.items()})
        cells.update({k: v[0] for k, v in UNKNOWN_CELLS.items()})
        for name, builder in sorted(cells.items()):
            outs = []
            for which in COPIES:
                home = tmp_path / f"{name}-{which}"
                home.mkdir()
                builder(home)
                probe, _ = _flag_probe(which, monkeypatch, home)
                state, msg = probe()
                outs.append((state, (msg or "").split(":", 1)[0] if state is None else state))
            assert outs[0] == outs[1], (name, outs)


# ---------------------------------------------------------------------------
# Row 3: the write-tool marker probe
# ---------------------------------------------------------------------------

def _marker(home: Path, content):
    p = home / "mcp-tools-ready.json"
    if isinstance(content, str):
        p.write_text(content)
    else:
        p.write_text(json.dumps(content))
    return p


class TestMarkerMatrix:
    def test_healthy_live_pid(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        _marker(tmp_path, {"pid": os.getpid()})
        assert mod._check_mcp_write_tool_marker(str(tmp_path)) == (True, None)

    def test_healthy_eperm(self, tmp_path, monkeypatch):
        import errno
        mod = _surface(monkeypatch, tmp_path)
        _marker(tmp_path, {"pid": 4242})

        def eperm(pid, sig):
            raise OSError(errno.EPERM, "denied")

        monkeypatch.setattr(mod.os, "kill", eperm)
        assert mod._check_mcp_write_tool_marker(str(tmp_path)) == (True, None)

    def test_healthy_with_note_dead_pid_live_server(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: True)
        _marker(tmp_path, {"pid": DEAD_PID})
        state, note = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is True and "stale" in note and "live server found" in note

    def test_not_ok_absent(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is False and "absent" in reason

    def test_not_ok_no_pid_field(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        _marker(tmp_path, {"registered_at": "x"})
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is False and "pid" in reason

    def test_not_ok_dead_pid_no_server(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)
        _marker(tmp_path, {"pid": DEAD_PID})
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is False and "stale" in reason

    @pytest.mark.parametrize("name,content,exc_type", [
        ("corrupt_json", '{"pid": 12', "JSONDecodeError"),
        ("pid_not_integer", {"pid": "abc"}, "ValueError"),
        ("pid_null", {"pid": None}, "TypeError"),
        ("json_list", [1, 2, 3], "AttributeError"),
    ])
    def test_unknown_damaged_marker(self, name, content, exc_type, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)
        _marker(tmp_path, content)
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is None
        assert reason.startswith(exc_type + ":"), reason

    def test_unknown_unreadable_marker(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        (tmp_path / "mcp-tools-ready.json").mkdir()  # open() raises IsADirectoryError
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is None
        assert reason.startswith(("IsADirectoryError:", "PermissionError:"))

    def test_unknown_any_other_exception(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        _marker(tmp_path, {"pid": os.getpid()})

        def boom(*a, **kw):
            raise RuntimeError("synthetic")

        monkeypatch.setattr(mod.json, "load", boom)
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is None and reason.startswith("RuntimeError:")

    def test_unknown_when_pgrep_state_unknown(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: None)
        _marker(tmp_path, {"pid": DEAD_PID})
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is None
        assert reason.startswith("pgrep-unknown:")


class TestServerPyProbeConflation:
    """`_is_any_server_py_running` swallowed every pgrep failure into False
    ("no server"), conflating unknown with not-ok. rc 0 = True, rc 1 (no
    match) = False, anything else = None."""

    def _patch_run(self, monkeypatch, behaviour):
        def fake(cmd, *a, **kw):
            if isinstance(behaviour, BaseException):
                raise behaviour
            return subprocess.CompletedProcess(cmd, behaviour)

        monkeypatch.setattr(subprocess, "run", fake)

    def test_rc0_is_true(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        self._patch_run(monkeypatch, 0)
        assert mod._is_any_server_py_running() is True

    def test_rc1_no_match_is_false(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        self._patch_run(monkeypatch, 1)
        assert mod._is_any_server_py_running() is False

    @pytest.mark.parametrize("behaviour", [
        2,
        FileNotFoundError("pgrep"),
        subprocess.TimeoutExpired("pgrep", 2),
        PermissionError("denied"),
    ])
    def test_failure_is_unknown(self, behaviour, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        self._patch_run(monkeypatch, behaviour)
        assert mod._is_any_server_py_running() is None


# ---------------------------------------------------------------------------
# Shared helper cells (new functions in _hooklib)
# ---------------------------------------------------------------------------

class TestHelperSanitizeAndLines:
    def test_sanitize_replaces_control_chars_and_bounds(self):
        hl = _hooklib()
        out = hl.sanitize_probe_text("a\nb\r\tc\x00d\x7fe" + "x" * 500, 200)
        assert "\n" not in out and "\r" not in out and "\t" not in out
        assert "\x00" not in out and "\x7f" not in out
        assert len(out) == 200
        assert out.startswith("a b  c d e")

    def test_stderr_line_shape_and_bounds(self, capsys):
        hl = _hooklib()
        reason = "OperationalError: " + "line1\nline2 " + "z" * 900
        hl.write_probe_unknown_stderr("engram-surface-hook", "embedding-restore", reason)
        err = capsys.readouterr().err
        assert err.endswith("\n") and err.count("\n") == 1
        line = err.rstrip("\n")
        assert line.startswith("engram-surface-hook: embedding-restore unknown: OperationalError: line1 line2")
        assert len(line) <= 300

    def test_injected_line_shape_carries_type_only(self):
        hl = _hooklib()
        line = hl.probe_unknown_line(
            "embedding-restore", "OperationalError: SECRET-RAW-MESSAGE", "recall may be degraded")
        assert line == "[probe] embedding-restore state UNKNOWN (OperationalError); recall may be degraded"
        assert "SECRET-RAW-MESSAGE" not in line

    def test_injected_line_bounded_200(self):
        hl = _hooklib()
        line = hl.probe_unknown_line("x" * 400, "T" * 400 + ": m", "y" * 400)
        assert len(line) <= 200
        assert "\n" not in line


class TestHelperStamp:
    def test_absent_stamp_is_due_and_mark_then_not_due(self, tmp_path):
        hl = _hooklib()
        assert hl.probe_notice_due(str(tmp_path), "k", now=1000.0) is True
        hl.mark_probe_notice(str(tmp_path), "k", now=1000.0)
        assert (tmp_path / STAMP_NAME).exists()
        assert hl.probe_notice_due(str(tmp_path), "k", now=1000.0 + 1799) is False
        assert hl.probe_notice_due(str(tmp_path), "k", now=1000.0 + 1800) is True
        # per-key: another key is independent
        assert hl.probe_notice_due(str(tmp_path), "other", now=1001.0) is True

    @pytest.mark.parametrize("content", [
        "{not json", "[]", '"str"', '{"shown_at": 5}', '{"shown_at": {"k": "soon"}}',
        '{"shown_at": {"k": true}}', '{"shown_at": {"k": 99999999999}}',  # future-dated
    ])
    def test_bad_stamp_is_due_never_raises(self, content, tmp_path):
        hl = _hooklib()
        (tmp_path / STAMP_NAME).write_text(content)
        assert hl.probe_notice_due(str(tmp_path), "k", now=1000.0) is True

    def test_mark_over_corrupt_stamp_repairs_it(self, tmp_path):
        hl = _hooklib()
        (tmp_path / STAMP_NAME).write_text("{corrupt")
        hl.mark_probe_notice(str(tmp_path), "k", now=1000.0)
        assert hl.probe_notice_due(str(tmp_path), "k", now=1010.0) is False

    def test_unwritable_stamp_never_raises_and_stays_visible(self, tmp_path):
        hl = _hooklib()
        # The stamp path is a DIRECTORY: write fails; the next check shows again.
        (tmp_path / STAMP_NAME).mkdir()
        hl.mark_probe_notice(str(tmp_path), "k", now=1000.0)  # must not raise
        assert hl.probe_notice_due(str(tmp_path), "k", now=1001.0) is True

    def test_missing_home_never_raises(self, tmp_path):
        hl = _hooklib()
        ghost = str(tmp_path / "does" / "not" / "exist")
        assert hl.probe_notice_due(ghost, "k", now=1.0) is True
        hl.mark_probe_notice(ghost, "k", now=1.0)  # creates or swallows; no raise


class TestHelperServerPyProbe:
    def test_probe_own_server_py_tristate(self, tmp_path, monkeypatch):
        hl = _hooklib()

        def mk(behaviour):
            def fake(cmd, *a, **kw):
                if isinstance(behaviour, BaseException):
                    raise behaviour
                return subprocess.CompletedProcess(cmd, behaviour)
            return fake

        monkeypatch.setattr(subprocess, "run", mk(0))
        assert hl.probe_own_server_py(str(tmp_path))[0] is True
        monkeypatch.setattr(subprocess, "run", mk(1))
        assert hl.probe_own_server_py(str(tmp_path))[0] is False
        monkeypatch.setattr(subprocess, "run", mk(2))
        state, reason = hl.probe_own_server_py(str(tmp_path))
        assert state is None and reason
        monkeypatch.setattr(subprocess, "run", mk(FileNotFoundError("pgrep")))
        assert hl.probe_own_server_py(str(tmp_path))[0] is None
        monkeypatch.setattr(subprocess, "run", mk(subprocess.TimeoutExpired("pgrep", 2)))
        assert hl.probe_own_server_py(str(tmp_path))[0] is None


# ---------------------------------------------------------------------------
# Rendering through the surface hook's main() (rows 1 and 3)
# ---------------------------------------------------------------------------

def _stub_surface_main(mod, monkeypatch, stub_marker=True):
    monkeypatch.setattr(mod, "query_daemon",
                        lambda *a, **kw: {"matched_ids": [], "match_count": 0, "nodes": []})
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
    if stub_marker:
        monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))


def _run_surface_main(mod, capsys):
    """Run main(); return (exit_code, ctx_string, stderr_lines)."""
    capsys.readouterr()
    stdin_data = json.dumps({"prompt": "hello"})
    buf = io.StringIO()
    code = None
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit as e:
                code = e.code
    out = json.loads(buf.getvalue().strip())
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    err = capsys.readouterr().err
    return code, ctx, err.splitlines()


def _unknown_lines(lines):
    return [ln for ln in lines if " unknown: " in ln]


class TestSurfaceRenderEmbeddingUnknown:
    def _home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        return home

    def test_unknown_renders_one_line_not_the_alarm(self, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _cell_not_a_database(home)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        code, ctx, err = _run_surface_main(mod, capsys)
        assert code in (0, None)
        assert "[probe] embedding-restore state UNKNOWN (DatabaseError); recall may be degraded" in ctx
        assert "NOT FULLY RESTORED" not in ctx  # unmistakably not the not-ok text
        u = _unknown_lines(err)
        assert len(u) == 1
        assert u[0].startswith("engram-surface-hook: embedding-restore unknown: DatabaseError:")
        assert len(u[0]) <= 300

    def test_injected_line_never_carries_raw_message(self, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _make_db(home / "knowledge.db", flag=1, null_count=1, cause="x")
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)

        def boom(*a, **kw):
            raise RuntimeError("SECRET-RAW\nMESSAGE " + "q" * 400)

        monkeypatch.setattr(mod.sqlite3, "connect", boom)
        _, ctx, err = _run_surface_main(mod, capsys)
        line = [ln for ln in ctx.splitlines() if "embedding-restore state UNKNOWN" in ln]
        assert len(line) == 1 and len(line[0]) <= 200
        assert "SECRET-RAW" not in ctx and "RuntimeError" in line[0]
        u = _unknown_lines(err)
        assert len(u) == 1 and len(u[0]) <= 300 and "\n" not in u[0]

    def test_rate_limited_within_window_but_stderr_every_run(self, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _cell_not_a_database(home)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _, ctx1, err1 = _run_surface_main(mod, capsys)
        assert "embedding-restore state UNKNOWN" in ctx1
        assert (home / STAMP_NAME).exists()  # stamp written after the response
        _, ctx2, err2 = _run_surface_main(mod, capsys)
        assert "embedding-restore state UNKNOWN" not in ctx2
        assert len(_unknown_lines(err2)) == 1  # one stderr line per run regardless

    def test_window_expiry_shows_again(self, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _cell_not_a_database(home)
        (home / STAMP_NAME).write_text(json.dumps({"shown_at": {"embedding-restore": time.time() - 1801}}))
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _, ctx, _ = _run_surface_main(mod, capsys)
        assert "embedding-restore state UNKNOWN" in ctx

    @pytest.mark.parametrize("stamp", ["{corrupt", "[]", '{"shown_at": {"embedding-restore": 99999999999}}'])
    def test_bad_stamp_still_shows(self, stamp, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _cell_not_a_database(home)
        (home / STAMP_NAME).write_text(stamp)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        code, ctx, _ = _run_surface_main(mod, capsys)
        assert code in (0, None)
        assert "embedding-restore state UNKNOWN" in ctx

    def test_unwritable_stamp_still_shows_and_exits_zero(self, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _cell_not_a_database(home)
        (home / STAMP_NAME).mkdir()
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        code, ctx, _ = _run_surface_main(mod, capsys)
        assert code in (0, None)
        assert "embedding-restore state UNKNOWN" in ctx

    def test_not_ok_text_unchanged_and_no_stamp(self, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _cell_flag_set(home)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _, ctx, err = _run_surface_main(mod, capsys)
        assert "NOT FULLY RESTORED: 7 node(s) lack embeddings" in ctx
        assert "state UNKNOWN" not in ctx
        assert _unknown_lines(err) == []
        assert not (home / STAMP_NAME).exists()

    def test_healthy_is_silent(self, tmp_path, monkeypatch, capsys):
        home = self._home(tmp_path)
        _cell_flag_clear(home)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _, ctx, err = _run_surface_main(mod, capsys)
        assert "UNKNOWN" not in ctx and "NOT FULLY RESTORED" not in ctx
        assert _unknown_lines(err) == []
        assert not (home / STAMP_NAME).exists()


class TestSurfaceRenderMarkerUnknown:
    def test_unknown_marker_renders_one_line_not_the_alarm(self, tmp_path, monkeypatch, capsys):
        _marker(tmp_path, '{"pid": 12')
        mod = _surface(monkeypatch, tmp_path)
        _stub_surface_main(mod, monkeypatch, stub_marker=False)
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)
        code, ctx, err = _run_surface_main(mod, capsys)
        assert code in (0, None)
        assert "MCP write-tool marker state UNKNOWN (JSONDecodeError)" in ctx
        assert "UNCONFIRMED" not in ctx  # not the dead-server alarm
        assert "Marker PID is dead" not in ctx
        u = _unknown_lines(err)
        assert len(u) == 1
        assert u[0].startswith("engram-surface-hook: mcp-write-marker unknown: JSONDecodeError:")
        line = [ln for ln in ctx.splitlines() if "MCP write-tool marker state UNKNOWN" in ln][0]
        assert len(line) <= 200

    def test_rate_limited_per_probe(self, tmp_path, monkeypatch, capsys):
        _marker(tmp_path, {"pid": "abc"})
        mod = _surface(monkeypatch, tmp_path)
        _stub_surface_main(mod, monkeypatch, stub_marker=False)
        _, ctx1, _ = _run_surface_main(mod, capsys)
        _, ctx2, err2 = _run_surface_main(mod, capsys)
        assert "MCP write-tool marker state UNKNOWN" in ctx1
        assert "MCP write-tool marker state UNKNOWN" not in ctx2
        assert len(_unknown_lines(err2)) == 1

    def test_two_probes_two_independent_windows(self, tmp_path, monkeypatch, capsys):
        # marker unknown AND embedding unknown on the same run: both lines show
        _marker(tmp_path, {"pid": "abc"})
        _cell_not_a_database(tmp_path)
        mod = _surface(monkeypatch, tmp_path)
        _stub_surface_main(mod, monkeypatch, stub_marker=False)
        _, ctx, err = _run_surface_main(mod, capsys)
        assert "MCP write-tool marker state UNKNOWN" in ctx
        assert "embedding-restore state UNKNOWN" in ctx
        assert len(_unknown_lines(err)) == 2

    def test_dead_marker_not_ok_text_unchanged(self, tmp_path, monkeypatch, capsys):
        _marker(tmp_path, {"pid": DEAD_PID})
        mod = _surface(monkeypatch, tmp_path)
        _stub_surface_main(mod, monkeypatch, stub_marker=False)
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)
        _, ctx, err = _run_surface_main(mod, capsys)
        assert "MCP write-tool liveness: UNCONFIRMED" in ctx
        assert "state UNKNOWN" not in ctx
        assert _unknown_lines(err) == []

    def test_healthy_marker_silent(self, tmp_path, monkeypatch, capsys):
        _marker(tmp_path, {"pid": os.getpid()})
        mod = _surface(monkeypatch, tmp_path)
        _stub_surface_main(mod, monkeypatch, stub_marker=False)
        _, ctx, err = _run_surface_main(mod, capsys)
        assert "state UNKNOWN" not in ctx and "UNCONFIRMED" not in ctx
        assert _unknown_lines(err) == []


# ---------------------------------------------------------------------------
# Real-hook subprocess cells (one per row; ENGRAM_HOME a temp dir)
# ---------------------------------------------------------------------------

def _run_hook(path: Path, home: Path, payload: dict):
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(path)], input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=60,
    )
    return proc


class TestSubprocessCells:
    def test_surface_unknown_embedding_subprocess(self, tmp_path):
        _cell_not_a_database(tmp_path)
        _marker(tmp_path, {"pid": os.getpid()})  # healthy marker keeps the head short
        proc = _run_hook(_SURFACE_PATH, tmp_path, {"prompt": "hello there", "session_id": "s"})
        assert proc.returncode == 0
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "embedding-restore state UNKNOWN (DatabaseError)" in ctx
        assert "NOT FULLY RESTORED" not in ctx
        assert len([ln for ln in proc.stderr.splitlines()
                    if "embedding-restore unknown:" in ln]) == 1

    # Deliberately no surface-hook SUBPROCESS not-ok embedding cell: the real
    # hook's startup DB setup rewrites a hand-built embedding_restore_state
    # fixture, so that cell is covered in-process instead
    # (TestSurfaceRenderEmbeddingUnknown.test_not_ok_text_unchanged_and_no_stamp).

    def test_surface_unknown_marker_subprocess(self, tmp_path):
        _marker(tmp_path, '{"pid": 1')
        proc = _run_hook(_SURFACE_PATH, tmp_path, {"prompt": "hello there", "session_id": "s"})
        assert proc.returncode == 0
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "MCP write-tool marker state UNKNOWN (JSONDecodeError)" in ctx
        assert "UNCONFIRMED" not in ctx

    def test_session_start_unknown_embedding_subprocess(self, tmp_path):
        _cell_not_a_database(tmp_path)
        proc = _run_hook(_SESSION_PATH, tmp_path,
                         {"source": "compact", "session_id": "s2", "cwd": str(tmp_path),
                          "hook_event_name": "SessionStart"})
        assert proc.returncode == 0
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "embedding-restore state UNKNOWN (DatabaseError); recall may be degraded" in ctx
        assert "NOT FULLY RESTORED" not in ctx
        assert len([ln for ln in proc.stderr.splitlines()
                    if "engram-session-start-hook: embedding-restore unknown:" in ln]) == 1

    def test_session_start_not_ok_embedding_subprocess(self, tmp_path):
        _cell_flag_set(tmp_path)
        proc = _run_hook(_SESSION_PATH, tmp_path,
                         {"source": "compact", "session_id": "s2", "cwd": str(tmp_path),
                          "hook_event_name": "SessionStart"})
        assert proc.returncode == 0
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Embeddings: DB NOT FULLY RESTORED (7 node(s) lack embeddings" in ctx
        assert "state UNKNOWN" not in ctx

    def test_session_start_healthy_embedding_subprocess(self, tmp_path):
        _cell_flag_clear(tmp_path)
        proc = _run_hook(_SESSION_PATH, tmp_path,
                         {"source": "compact", "session_id": "s2", "cwd": str(tmp_path),
                          "hook_event_name": "SessionStart"})
        assert proc.returncode == 0
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "embedding-restore" not in ctx and "NOT FULLY RESTORED" not in ctx
        assert "embedding-restore unknown:" not in proc.stderr


# ---------------------------------------------------------------------------
# Row 4: stale-tree warn hook
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _make_origin_and_clone(root: Path, branch="main"):
    root.mkdir(parents=True, exist_ok=True)
    origin = root / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", branch)
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", branch)
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "f.txt").write_text("v1\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", str(origin), f"{branch}:{branch}")
    clone = root / "clone"
    _git(root, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    return origin, clone


def _advance(root: Path, origin: Path, branch: str, msg: str):
    pusher = root / f"pusher-{msg.replace(' ', '_')}"
    _git(root, "clone", str(origin), str(pusher))
    _git(pusher, "config", "user.email", "t@example.com")
    _git(pusher, "config", "user.name", "T")
    (pusher / "f.txt").write_text(msg + "\n")
    _git(pusher, "add", "f.txt")
    _git(pusher, "commit", "-m", msg)
    _git(pusher, "push", str(origin), f"{branch}:{branch}")


def _watch(home: Path, path: Path, branch="main"):
    (home / "config.json").write_text(json.dumps({"stale_tree_watch": [{"path": str(path), "branch": branch}]}))


def _run_warn(mod, capsys, target: Path):
    capsys.readouterr()
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(target / "f.txt")}})
    code = None
    with patch("sys.stdin", io.StringIO(payload)):
        try:
            mod.main()
        except SystemExit as e:
            code = e.code
    cap = capsys.readouterr()
    return code, cap.out, cap.err


class TestWarnHookMatrix:
    def _behind(self, tmp_path, n=2):
        root = tmp_path / "repo"
        origin, clone = _make_origin_and_clone(root)
        for i in range(n):
            _advance(root, origin, "main", f"new commit {i}")
        _git(clone, "fetch", "origin", "main")
        return clone

    # healthy / not-ok (green on dev)
    def test_healthy_zero_behind_is_silent(self, tmp_path, capsys):
        _, clone = _make_origin_and_clone(tmp_path / "repo")
        _git(clone, "fetch", "origin", "main")
        home = tmp_path / "home"
        home.mkdir()
        _watch(home, clone)
        mod = _warn(home)
        code, out, err = _run_warn(mod, capsys, clone)
        assert code == 0 and out == "" and err == ""
        assert not (home / STAMP_NAME).exists()

    def test_not_ok_n_behind_exact_existing_warning(self, tmp_path, capsys):
        clone = self._behind(tmp_path, n=2)
        home = tmp_path / "home"
        home.mkdir()
        _watch(home, clone)
        mod = _warn(home)
        code, out, err = _run_warn(mod, capsys, clone)
        expected = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"⚠ {clone} is 2 commit(s) behind origin/main — your read may be stale; "
                    f"pull first (`git -C {clone} pull --ff-only`)."
                ),
            }
        }
        assert code == 0 and err == ""
        assert out == json.dumps(expected) + "\n"

    # unknown cells (red on dev)
    def _unknown_case(self, tmp_path, monkeypatch, capsys, how):
        home = tmp_path / "home"
        home.mkdir()
        if how == "typo_branch":
            root = tmp_path / "repo"
            _, clone = _make_origin_and_clone(root)
            _watch(home, clone, branch="mian")
            mod = _warn(home)
            return mod, clone, "nonzero"
        if how == "not_a_repo":
            plain = tmp_path / "plain"
            plain.mkdir()
            _watch(home, plain)
            mod = _warn(home)
            return mod, plain, "nonzero"
        root = tmp_path / "repo"
        _, clone = _make_origin_and_clone(root)
        _git(clone, "fetch", "origin", "main")
        _watch(home, clone)
        mod = _warn(home)
        if how == "git_absent":
            empty = tmp_path / "emptybin"
            empty.mkdir()
            monkeypatch.setenv("PATH", str(empty))
            return mod, clone, "git-missing"
        if how == "timeout":
            def fake(*a, **kw):
                raise subprocess.TimeoutExpired("git", 3)
            monkeypatch.setattr(mod.subprocess, "run", fake)
            return mod, clone, "timeout"
        if how == "unparsable":
            def fake(cmd, *a, **kw):
                return subprocess.CompletedProcess(cmd, 0, stdout="not-a-number\n", stderr="")
            monkeypatch.setattr(mod.subprocess, "run", fake)
            return mod, clone, "unparsable"
        if how == "oserror":
            def fake(*a, **kw):
                raise PermissionError("denied")
            monkeypatch.setattr(mod.subprocess, "run", fake)
            return mod, clone, None  # reason class is this implementation's choice
        raise AssertionError(how)

    @pytest.mark.parametrize("how", [
        "typo_branch", "not_a_repo", "git_absent", "timeout", "unparsable", "oserror"])
    def test_unknown_renders_line_exit_zero(self, how, tmp_path, monkeypatch, capsys):
        mod, target, cls = self._unknown_case(tmp_path, monkeypatch, capsys, how)
        code, out, err = _run_warn(mod, capsys, target)
        assert code == 0
        assert out.strip() != "", "a failed probe must not be silent"
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "UNKNOWN" in ctx and "commit(s) behind" not in ctx
        assert len(ctx) <= 300 and "\n" not in ctx
        if cls:
            assert f"({cls})" in ctx
        lines = [ln for ln in err.splitlines() if " unknown: " in ln]
        assert len(lines) == 1 and len(lines[0]) <= 300
        assert lines[0].startswith("engram-stale-tree-warn-hook: stale-tree-behind-count unknown: ")
        if cls:
            assert f"unknown: {cls}:" in lines[0]

    def test_unknown_rate_limited_per_checkout_path(self, tmp_path, monkeypatch, capsys):
        mod, target, _ = self._unknown_case(tmp_path, monkeypatch, capsys, "typo_branch")
        _, out1, _ = _run_warn(mod, capsys, target)
        assert out1.strip() != ""
        assert (tmp_path / "home" / STAMP_NAME).exists()
        _, out2, err2 = _run_warn(mod, capsys, target)
        assert out2 == ""  # within window: no second injected line
        assert len([ln for ln in err2.splitlines() if " unknown: " in ln]) == 1
        # a DIFFERENT checkout path has its own window
        plain = tmp_path / "plain"
        plain.mkdir()
        _watch(tmp_path / "home", plain)
        _, out3, _ = _run_warn(mod, capsys, plain)
        assert out3.strip() != ""

    @pytest.mark.parametrize("stamp", ["{corrupt", "[]", '{"shown_at": {"x": 1}}', "future"])
    def test_bad_stamp_still_shows(self, stamp, tmp_path, monkeypatch, capsys):
        mod, target, _ = self._unknown_case(tmp_path, monkeypatch, capsys, "typo_branch")
        if stamp == "future":
            stamp = json.dumps({"shown_at": {f"stale-tree:{target}": 99999999999}})
        (tmp_path / "home" / STAMP_NAME).write_text(stamp)
        code, out, _ = _run_warn(mod, capsys, target)
        assert code == 0 and out.strip() != ""

    def test_unwritable_stamp_still_shows(self, tmp_path, monkeypatch, capsys):
        mod, target, _ = self._unknown_case(tmp_path, monkeypatch, capsys, "typo_branch")
        (tmp_path / "home" / STAMP_NAME).mkdir()
        code, out, _ = _run_warn(mod, capsys, target)
        assert code == 0 and out.strip() != ""

    def test_long_path_line_stays_bounded(self, tmp_path, monkeypatch, capsys):
        deep = tmp_path / ("d" * 120) / ("e" * 120) / ("f" * 120)
        deep.mkdir(parents=True)
        home = tmp_path / "home"
        home.mkdir()
        _watch(home, deep)
        mod = _warn(home)
        code, out, err = _run_warn(mod, capsys, deep)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert code == 0 and len(ctx) <= 300
        assert all(len(ln) <= 300 for ln in err.splitlines() if " unknown: " in ln)

    def test_subprocess_real_hook_unknown_and_exit_zero(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _, clone = _make_origin_and_clone(tmp_path / "repo")
        _watch(home, clone, branch="mian")
        proc = _run_hook(_WARN_PATH, home, {"tool_name": "Read", "tool_input": {"file_path": str(clone / "f.txt")}})
        assert proc.returncode == 0
        assert "UNKNOWN" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        assert proc.stderr.count(" unknown: ") == 1

    def test_subprocess_real_hook_healthy_byte_silent(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _, clone = _make_origin_and_clone(tmp_path / "repo")
        _git(clone, "fetch", "origin", "main")
        _watch(home, clone)
        proc = _run_hook(_WARN_PATH, home, {"tool_name": "Read", "tool_input": {"file_path": str(clone / "f.txt")}})
        assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


class TestBehindCountShape:
    """Shape cells for the row-4 return: (count|None, reason|None). 0 behind
    and could-not-tell are different values."""

    def test_zero_behind_vs_unknown_are_distinguishable(self, tmp_path):
        _, clone = _make_origin_and_clone(tmp_path / "repo")
        _git(clone, "fetch", "origin", "main")
        mod = _warn(tmp_path)
        zero = mod._behind_count(str(clone), "main")
        unknown = mod._behind_count(str(clone), "no-such-branch")
        assert zero == (0, None)
        assert unknown[0] is None and unknown[1].startswith("nonzero:")

    @pytest.mark.parametrize("how,cls", [("timeout", "timeout"), ("unparsable", "unparsable"),
                                         ("absent", "git-missing")])
    def test_reason_classes(self, how, cls, tmp_path, monkeypatch):
        mod = _warn(tmp_path)
        if how == "timeout":
            def fake(*a, **kw):
                raise subprocess.TimeoutExpired("git", 3)
        elif how == "unparsable":
            def fake(cmd, *a, **kw):
                return subprocess.CompletedProcess(cmd, 0, stdout="zzz", stderr="")
        else:
            def fake(*a, **kw):
                raise FileNotFoundError("git")
        monkeypatch.setattr(mod.subprocess, "run", fake)
        count, reason = mod._behind_count(str(tmp_path), "main")
        assert count is None and reason.startswith(cls + ":")


# ---------------------------------------------------------------------------
# Round-1 fold: pid shapes that must be UNKNOWN, and survivors of four mutations
# ---------------------------------------------------------------------------

class TestMarkerPidShapes:
    """The marker's pid must be a positive integer to be believed. Booleans,
    non-integer JSON numbers and negatives are UNKNOWN, never healthy
    (`int(True)` is 1, `int(1.5)` is 1, `os.kill(-1, 0)` succeeds)."""

    def _mod(self, tmp_path, monkeypatch):
        mod = _surface(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)
        return mod

    # controls, same fixture shape: must be green before the unknown cells mean anything
    def test_control_live_int_pid_is_healthy(self, tmp_path, monkeypatch):
        mod = self._mod(tmp_path, monkeypatch)
        _marker(tmp_path, '{"pid": %d}' % os.getpid())
        assert mod._check_mcp_write_tool_marker(str(tmp_path)) == (True, None)

    def test_control_dead_int_pid_is_not_ok(self, tmp_path, monkeypatch):
        mod = self._mod(tmp_path, monkeypatch)
        _marker(tmp_path, '{"pid": %d}' % DEAD_PID)
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is False and "stale" in reason

    def test_control_dead_digit_string_pid_is_not_ok(self, tmp_path, monkeypatch):
        mod = self._mod(tmp_path, monkeypatch)
        _marker(tmp_path, '{"pid": "%d"}' % DEAD_PID)
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is False and "stale" in reason

    def test_control_live_digit_string_pid_is_healthy(self, tmp_path, monkeypatch):
        mod = self._mod(tmp_path, monkeypatch)
        _marker(tmp_path, '{"pid": "%d"}' % os.getpid())
        assert mod._check_mcp_write_tool_marker(str(tmp_path)) == (True, None)

    @pytest.mark.parametrize("raw", ["0", "0.0", None])
    def test_control_no_valid_pid_stays_not_ok(self, raw, tmp_path, monkeypatch):
        mod = self._mod(tmp_path, monkeypatch)
        _marker(tmp_path, '{"registered_at": "x"}' if raw is None else '{"pid": %s}' % raw)
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is False and "no valid pid" in reason

    @pytest.mark.parametrize("raw", ["true", "false", "1.5", "2.0", "-1", "-7", '"-1"', '"abc"', "NaN", "1e400"])
    def test_unknown_bad_pid_shapes(self, raw, tmp_path, monkeypatch):
        mod = self._mod(tmp_path, monkeypatch)
        _marker(tmp_path, '{"pid": %s}' % raw)
        state, reason = mod._check_mcp_write_tool_marker(str(tmp_path))
        assert state is None, (raw, state, reason)
        assert ": " in reason


class TestProbeStampSurvivors:
    """Cells added so four surviving mutations go red."""

    def test_marking_one_probe_keeps_the_others_window(self, tmp_path):
        hl = _hooklib()
        hl.mark_probe_notice(str(tmp_path), "probe-a", now=1000.0)
        hl.mark_probe_notice(str(tmp_path), "probe-b", now=1100.0)
        assert hl.probe_notice_due(str(tmp_path), "probe-a", now=1200.0) is False
        assert hl.probe_notice_due(str(tmp_path), "probe-b", now=1200.0) is False

    def test_stale_keys_are_pruned_on_next_write(self, tmp_path):
        hl = _hooklib()
        for i in range(50):
            hl.mark_probe_notice(str(tmp_path), f"stale-tree:/tmp/co{i}", now=1000.0)
        assert len(json.loads((tmp_path / STAMP_NAME).read_text())["shown_at"]) == 50
        hl.mark_probe_notice(str(tmp_path), "live", now=1000.0 + 1805)
        assert json.loads((tmp_path / STAMP_NAME).read_text())["shown_at"] == {"live": 1000.0 + 1805}

    def test_live_keys_survive_the_prune(self, tmp_path):
        hl = _hooklib()
        hl.mark_probe_notice(str(tmp_path), "old", now=1000.0)
        hl.mark_probe_notice(str(tmp_path), "recent", now=2000.0)
        hl.mark_probe_notice(str(tmp_path), "new", now=2900.0)  # old is 1900 s old, recent 900
        assert set(json.loads((tmp_path / STAMP_NAME).read_text())["shown_at"]) == {"recent", "new"}

    def test_stderr_line_capped_at_300_when_pieces_would_exceed_it(self, capsys):
        hl = _hooklib()
        hl.write_probe_unknown_stderr("h" * 60, "p" * 60, "C" * 80 + ": " + "m" * 500)
        err = capsys.readouterr().err
        assert err.endswith("\n") and err.count("\n") == 1
        assert len(err.rstrip("\n")) == 300  # uncapped it would be well past 300


class _ClosedStdout:
    def write(self, s):
        raise OSError("stdout closed")

    def flush(self):
        raise OSError("stdout closed")


class TestStampOnlyAfterResponsePrinted:
    def test_surface_main_print_fails_no_stamp(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        _cell_not_a_database(home)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        stdin_data = json.dumps({"prompt": "hello"})
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
            with patch("sys.stdout", _ClosedStdout()):
                try:
                    mod.main()
                except BaseException:
                    pass  # whether main swallows or raises is not the contract
        assert not (home / STAMP_NAME).exists()

    def test_surface_main_control_prints_then_stamps(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        _cell_not_a_database(home)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _run_surface_main(mod, capsys)
        assert (home / STAMP_NAME).exists()

    def test_stale_tree_report_unknown_print_fails_no_stamp(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        mod = _warn(home)
        with patch("sys.stdout", _ClosedStdout()):
            mod._report_unknown({"path": str(tmp_path / "co"), "branch": "main"}, "nonzero: git exited 128: x")
        assert not (home / STAMP_NAME).exists()

    def test_stale_tree_report_unknown_control_prints_then_stamps(self, tmp_path, capsys):
        home = tmp_path / "home"
        home.mkdir()
        mod = _warn(home)
        mod._report_unknown({"path": str(tmp_path / "co"), "branch": "main"}, "nonzero: git exited 128: x")
        assert "state UNKNOWN" in capsys.readouterr().out
        assert (home / STAMP_NAME).exists()


class TestStaleTreeDegradeBranch:
    def test_degrade_branch_sanitizes_all_control_chars_and_prints_plain_line(self, tmp_path, capsys):
        home = tmp_path / "home"
        home.mkdir()
        mod = _warn(home)
        mod._PROBE_LIB_OK = False
        mod._report_unknown({"path": str(tmp_path / "co"), "branch": "main"},
                            "nonzero: a\x1b[31mb\x00c\rd\ne\x7ff")
        cap = capsys.readouterr()
        assert cap.err.count("\n") == 1
        assert not any(c in cap.err.rstrip("\n") for c in "\x1b\x00\r\x7f")
        assert len(cap.err) <= 301
        assert "state UNKNOWN" in cap.out
        assert not (home / STAMP_NAME).exists()  # degrade branch has no stamp
