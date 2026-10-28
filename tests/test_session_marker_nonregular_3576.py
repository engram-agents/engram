"""#3576: SessionStart's read of the existing session marker must not block.

`write_marker` used a plain `open()` on `$ENGRAM_HOME/sessions/<sid>.json`; on a
FIFO that waits for a writer and SessionStart hangs. It now reads through
`_hooklib._open_regular_fd` (O_NONBLOCK + S_ISREG). A non-regular marker means
"no prior marker"; the new marker is still written (os.replace swaps a FIFO out).

Hang cells run `write_marker` in a SUBPROCESS with a hard timeout so a
regression fails instead of hanging the suite. Carry-forward cells run in
process and are positive controls (green before and after the fix).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK_PATH = HOOK_DIR / "engram-session-start-hook.py"

SID = "sess-3576"
GUARD_S = 8
PRIOR = {
    "session_id": SID,
    "started_at": "2026-10-01T00:00:00+00:00",
    "session_started_at": "2026-09-01T00:00:00+00:00",
    "injected_through": "2026-09-02T00:00:00+00:00",
    "compaction_count": 2,
}

_needs_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "engram"
    (h / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAM_HOME", str(h))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    return h


def _marker_path(home):
    return home / "sessions" / f"{SID}.json"


def _hook_input():
    return {"session_id": SID, "transcript_path": "/tmp/t.jsonl", "source": "startup"}


def _in_process_write_marker():
    sys.modules["context_tracker"] = MagicMock(write_baseline=MagicMock(return_value=None))
    sys.path.insert(0, str(HOOK_DIR))
    spec = importlib.util.spec_from_file_location("engram_session_start_hook_3576", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.write_marker(_hook_input())


_CHILD = f"""
import importlib.util, json, sys
from unittest.mock import MagicMock
sys.modules["context_tracker"] = MagicMock()
spec = importlib.util.spec_from_file_location("hook_3576_child", {str(HOOK_PATH)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
m = mod.write_marker({_hook_input()!r})
print(json.dumps(None if m is None else m.get("session_id")))
"""


def _run_guarded():
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True,
                              text=True, timeout=GUARD_S, env=dict(os.environ))
    except subprocess.TimeoutExpired:
        pytest.fail(f"write_marker blocked for more than {GUARD_S}s on a non-regular marker")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


# --- hang cells (red on a plain open) ---

@_needs_fifo
def test_fifo_at_marker_path_does_not_block(home):
    os.mkfifo(_marker_path(home))
    assert _run_guarded() == SID


@_needs_fifo
def test_fifo_is_replaced_by_a_regular_marker(home):
    os.mkfifo(_marker_path(home))
    _run_guarded()
    p = _marker_path(home)
    assert p.is_file() and not p.is_fifo()
    written = json.loads(p.read_text())
    assert written["session_id"] == SID
    assert written["compaction_count"] == 0
    assert written["session_started_at"] == written["started_at"]  # nothing carried


def test_directory_at_marker_path_returns(home):
    _marker_path(home).mkdir()
    # a plain open() of a directory raises IsADirectoryError (an OSError), so
    # this cell is a no-hang/no-crash control rather than red on the old code.
    # The write cannot replace a directory (EISDIR): write_marker returns None.
    assert _run_guarded() is None
    assert _marker_path(home).is_dir()


# --- carry-forward positive controls (green before and after) ---

def test_valid_prior_marker_is_carried_forward(home):
    _marker_path(home).write_text(json.dumps(PRIOR))
    m = _in_process_write_marker()
    assert m["compaction_count"] == 2
    assert m["session_started_at"] == PRIOR["session_started_at"]
    assert m["injected_through"] == PRIOR["injected_through"]
    assert m["started_at"] != PRIOR["started_at"]  # fresh now()


def test_legacy_marker_started_at_feeds_session_started_at(home):
    _marker_path(home).write_text(json.dumps({"session_id": SID, "started_at": PRIOR["started_at"]}))
    assert _in_process_write_marker()["session_started_at"] == PRIOR["started_at"]


def test_malformed_json_carries_nothing(home):
    _marker_path(home).write_text("{not json")
    m = _in_process_write_marker()
    assert m["compaction_count"] == 0
    assert m["injected_through"] is None
    assert m["session_started_at"] == m["started_at"]


def test_non_dict_json_carries_nothing(home):
    _marker_path(home).write_text("[1]")
    m = _in_process_write_marker()
    assert m["compaction_count"] == 0
    assert m["injected_through"] is None
    assert m["session_started_at"] == m["started_at"]
