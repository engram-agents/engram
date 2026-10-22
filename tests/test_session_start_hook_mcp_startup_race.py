"""SessionStart MCP-liveness contract for #3202: no confident OFFLINE on a race.

Measured on one seat before this was written: the SessionStart block printed
"MCP server: OFFLINE (no engram server process found)" on 12 of 12
``source=startup`` boots between 2026-09-17 and 2026-09-30, and on no
``source=compact`` start, while the server was live every time. A startup or
resume launches a fresh server alongside this hook, so one pgrep can run
before the process is visible; a compact or clear start keeps the running
server, so a miss there IS evidence.

The contract (Kepler, 2026-09-30, riders included):

1. A live process with a dead marker PID says the marker is stale and the
   server was found. Never OFFLINE.
2. No process at a compact or clear start prints the hard OFFLINE alarm.
3. No process at a startup, resume or any unrecognised source (including an
   invented non-empty one, not only a missing one) prints a soft line
   that names "no match" and points at the first prompt. It must not contain
   the word OFFLINE or the tell-the-user instruction. The prompt hook is the
   alarm of record for that case (see
   test_surface_hook_mcp_liveness.py::test_main_emits_write_tool_warning_on_stale_marker
   and the marker-absent case in this file's sibling below).
4. pgrep timing out, pgrep exiting with an error, or the probe raising prints
   an "unknown" line that names which of the three it saw. Never OFFLINE.
"""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOKS = _REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_START_HOOK = _HOOKS / "engram-session-start-hook.py"
_SURFACE_HOOK = _HOOKS / "engram-surface-hook.py"

DEAD_PID = 9_999_999  # above any Linux pid_max default; never a live process

OFFLINE = "MCP server: OFFLINE"
TELL_USER = "Tell the user immediately"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load(monkeypatch, engram_home: Path, path: Path, name: str):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _claude_runtime(monkeypatch, mod, tmp_path: Path):
    runtime_root = tmp_path / "plugin-runtime"
    hooks_dir = runtime_root / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_path = hooks_dir / "engram-session-start-hook.py"
    hook_path.write_text("# test hook path\n")
    (runtime_root / "platform.json").write_text(json.dumps({"platform": "claude-code"}))
    (runtime_root / "server.py").write_text("# test server\n")
    monkeypatch.setattr(mod, "__file__", str(hook_path))
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    return runtime_root


class _Completed:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _pgrep_only(outcome):
    """subprocess.run stub: `outcome` for pgrep, rc=1 for anything else.

    `outcome` is a return code or an exception instance to raise.
    """
    def run(cmd, *a, **kw):
        if cmd and cmd[0] == "pgrep":
            if isinstance(outcome, BaseException):
                raise outcome
            return _Completed(outcome)
        return _Completed(1)
    return MagicMock(side_effect=run)


def _session_start_ctx(tmp_path, monkeypatch, *, source, pgrep, marker=None):
    engram_home = tmp_path / "engram"
    (engram_home / "sessions").mkdir(parents=True)
    if marker is not None:
        (engram_home / "mcp-tools-ready.json").write_text(marker)
    mod = _load(monkeypatch, engram_home, _START_HOOK, "engram_session_start_hook")
    _claude_runtime(monkeypatch, mod, tmp_path)
    monkeypatch.setattr(mod.subprocess, "run", _pgrep_only(pgrep))
    hook_input = {
        "session_id": "s-3202",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    if source is not None:
        hook_input["source"] = source
    buf = io.StringIO()
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(hook_input).encode()))
    with patch.object(sys, "stdin", stdin), patch("sys.stdout", buf):
        mod.main()
    return json.loads(buf.getvalue().strip())["hookSpecificOutput"]["additionalContext"]


def _mcp_lines(ctx: str) -> list[str]:
    return [l for l in ctx.splitlines() if "MCP" in l]


def _dead_marker() -> str:
    return json.dumps({"pid": DEAD_PID, "registered_at": "2026-09-29T12:16:07Z"})


# ── 1. live process + dead marker PID → stale-marker note, never OFFLINE ──────

@pytest.mark.parametrize("source", ["startup", "resume", "compact", "clear"])
def test_live_server_with_dead_marker_pid_says_stale_not_offline(
    tmp_path, monkeypatch, source
):
    ctx = _session_start_ctx(
        tmp_path, monkeypatch, source=source, pgrep=0, marker=_dead_marker()
    )
    assert OFFLINE not in ctx
    assert TELL_USER not in ctx
    assert (
        f"marker stale (PID {DEAD_PID} no longer running); live server found" in ctx
    ), _mcp_lines(ctx)


def test_live_server_with_live_marker_pid_is_silent(tmp_path, monkeypatch):
    marker = json.dumps({"pid": os.getpid(), "registered_at": "2026-09-30T12:16:07Z"})
    ctx = _session_start_ctx(
        tmp_path, monkeypatch, source="startup", pgrep=0, marker=marker
    )
    assert _mcp_lines(ctx) == [], _mcp_lines(ctx)


@pytest.mark.parametrize("marker", [None, "{not json", json.dumps({"pid": 0})])
def test_live_server_with_absent_or_unreadable_marker_is_never_offline(
    tmp_path, monkeypatch, marker
):
    ctx = _session_start_ctx(
        tmp_path, monkeypatch, source="startup", pgrep=0, marker=marker
    )
    assert OFFLINE not in ctx
    assert TELL_USER not in ctx


# ── 2. no process where the server should already run → hard OFFLINE ────────

@pytest.mark.parametrize("source", ["compact", "clear"])
def test_no_server_at_compact_or_clear_is_offline(tmp_path, monkeypatch, source):
    ctx = _session_start_ctx(
        tmp_path, monkeypatch, source=source, pgrep=1, marker=_dead_marker()
    )
    assert f"{OFFLINE} (no engram server process found)" in ctx
    assert TELL_USER in ctx


# ── 3. no process at a launch → soft line, names the cause, no alarm ─────────

# An invented, non-empty source. The hook's rule is an allowlist of
# server-keeping sources (compact, clear), so any unrecognised source must be
# soft. A denylist ("startup", "resume") is already caught by the None/""
# cells, but one that also lists "" (the hook maps a missing source to "")
# passes every other cell; this is the only cell that catches it (#3252).
@pytest.mark.parametrize(
    "source", ["startup", "resume", None, "", "reload-from-future-harness"]
)
def test_no_server_at_launch_is_soft_not_offline(tmp_path, monkeypatch, source):
    ctx = _session_start_ctx(
        tmp_path, monkeypatch, source=source, pgrep=1, marker=_dead_marker()
    )
    assert OFFLINE not in ctx
    assert "OFFLINE" not in "\n".join(_mcp_lines(ctx))
    assert TELL_USER not in ctx
    soft = [l for l in _mcp_lines(ctx) if "no match" in l]
    assert soft, _mcp_lines(ctx)
    assert "first prompt" in soft[0], soft
    # The warning header is itself an alarm: seats are told to surface any
    # "⚠️ ENGRAM substrate health" block to the user at once. An unknown
    # liveness line alone must not raise it.
    assert "⚠️  ENGRAM substrate health" not in ctx, _mcp_lines(ctx)


# ── 4. the probe failing is "unknown", and names which failure it saw ────────

@pytest.mark.parametrize("source", ["startup", "compact"])
@pytest.mark.parametrize(
    "pgrep, cause",
    [
        (2, "error (exit 2)"),
        (3, "error (exit 3)"),
        ("timeout", "timed out"),
        (OSError("pgrep missing"), "probe error"),
    ],
)
def test_probe_failure_is_unknown_and_names_its_cause(
    tmp_path, monkeypatch, source, pgrep, cause
):
    if pgrep == "timeout":
        import subprocess
        pgrep = subprocess.TimeoutExpired(["pgrep"], 1)
    ctx = _session_start_ctx(
        tmp_path, monkeypatch, source=source, pgrep=pgrep, marker=_dead_marker()
    )
    assert OFFLINE not in ctx
    assert TELL_USER not in ctx
    lines = [l for l in _mcp_lines(ctx) if "unknown" in l.lower() or "inconclusive" in l.lower()]
    assert lines, _mcp_lines(ctx)
    assert cause in lines[0], lines
    assert "⚠️  ENGRAM substrate health" not in ctx, _mcp_lines(ctx)


# ── rider (a): the first prompt IS the alarm of record for case 3 ───────────

def test_prompt_hook_alarms_when_marker_absent_and_no_server(tmp_path, monkeypatch):
    """Case 3 hands the verdict to the first prompt. With no marker at all (a
    server that never initialised), the prompt hook must raise its alarm,
    not stay silent. The dead-marker-no-server case is already pinned by
    test_surface_hook_mcp_liveness.py::test_main_emits_write_tool_warning_on_stale_marker.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load(monkeypatch, engram_home, _SURFACE_HOOK, "engram_surface_hook_3202")
    ok, reason = mod._check_mcp_write_tool_marker(str(engram_home))
    assert ok is False
    assert "absent" in reason
