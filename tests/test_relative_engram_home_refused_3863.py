"""A RELATIVE ENGRAM_HOME is refused everywhere it can be resolved (alpha #3863 6b; Borges's ruling).

A relative value resolves against the CALLING process's cwd, so a hook (the session cwd), the owner daemon
and the MCP server would each land in a different home, silently. It is unsupported:

  * every shipped Python hook exits 0, writes nothing and prints ONE stderr line naming the value
    (fail open: Claude Code treats any other non-zero exit as a non-blocking error and shows the trace);
    stdout is empty, except for a WRITE-GATE HOOK (WRITE_GATE_HOOKS below), which also announces on
    stdout that it could not run (Borges's amendment of this rule, PR #4613 round 6);
  * a tool that resolves the home at import exits non-zero with the message;
  * the probe mod refuses too (tools/probes/mods-recall step.test.ts) and is pinned in
    tests/test_route_probe_dir_parity_3863.py.
The shell launchers that start long-lived processes refuse in 6c.

Mutation: drop the guard in one hook, or make a lib helper fall back to abspath -> that hook's cell red.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO / "src" / "engram" / "hooks" / "claude"
TOOLS_DIR = REPO / "src" / "engram" / "tools"
VALUE = "rel/store"


def _shipped_python_hooks():
    cmds = set()

    def walk(x):
        if isinstance(x, dict):
            if "command" in x:
                cmds.add(x["command"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(json.loads((REPO / "src" / "engram" / "hooks" / "hooks.json").read_text()))
    names = set()
    for c in cmds:
        m = re.search(r"hooks/([\w\-\.]+\.py)", c)
        if m:
            names.add(m.group(1))
    return sorted(names)


HOOKS = _shipped_python_hooks()
# engram-bash-pipe-exit-warn.py never touches the ENGRAM home: it has nothing to refuse.
NO_HOME = {"engram-bash-pipe-exit-warn.py"}


# The one exception to "silent stdout", scoped by REASON, not only by file name (Borges, #4613 review thread,
# amending the #3863 rule): a hook whose job is to GATE A WRITE announces on stdout that it could not run,
# because a safety check that silently does not run reads as health.  It still fails open (exit 0, allow,
# writes nothing) and still prints ONE stderr line naming the value.  The next write gate inherits this
# rule by being added here; every other hook, and the prompt-side gate hook, stays silent on stdout.
WRITE_GATE_HOOKS = frozenset({"engram-model-gate-pretool.py"})
GATE_IMPORT_FAILED_NOTICE = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": (
    "ENGRAM model-family gate: this write went through UNVERIFIED (`gate_import_failed`): the gate could not "
    "confirm which model made the call.")}}
WRITE_PAYLOAD = json.dumps({"session_id": "s1", "hook_event_name": "PreToolUse",
                            "tool_name": "mcp__engram__engram_add_observation", "tool_use_id": "toolu_01",
                            "transcript_path": "/nonexistent/t.jsonl", "cwd": "/tmp", "tool_input": {}})


def _run(script: Path, tmp_path: Path, args=(), stdin="{}"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "ENGRAM_HOME": VALUE, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, str(script), *args], input=stdin, capture_output=True, text=True,
                       env=env, cwd=str(cwd), timeout=60)
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    return r, written


def test_the_shipped_hook_list_is_found():
    assert len(HOOKS) >= 14, HOOKS


@pytest.mark.parametrize("hook", [h for h in HOOKS if h not in WRITE_GATE_HOOKS])
def test_a_relative_engram_home_is_a_fail_open_refusal(hook, tmp_path):
    r, written = _run(HOOKS_DIR / hook, tmp_path)
    assert r.returncode == 0, (r.returncode, r.stderr[-600:])
    assert r.stdout == "", r.stdout[:300]
    # nothing written: only the two scratch dirs the harness itself made
    assert written == ["cwd", "home"], written
    if hook in NO_HOME:
        return
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and VALUE in lines[0] and "ENGRAM_HOME" in lines[0], r.stderr


def test_the_write_gate_exception_set_is_exactly_the_expected_members():
    """The exception cannot widen silently: adding a hook to WRITE_GATE_HOOKS is a deliberate edit of this
    cell, and so of the rule.  The prompt-side gate hook (a warning, not a write gate) is not a member."""
    assert WRITE_GATE_HOOKS == {"engram-model-gate-pretool.py"}
    assert "engram-model-gate-prompt.py" not in WRITE_GATE_HOOKS
    assert WRITE_GATE_HOOKS <= set(HOOKS) and not (WRITE_GATE_HOOKS & NO_HOME)


@pytest.mark.parametrize("stdin", ["", WRITE_PAYLOAD], ids=["empty-stdin", "realistic-payload"])
@pytest.mark.parametrize("hook", sorted(WRITE_GATE_HOOKS))
def test_a_write_gate_hook_on_a_relative_engram_home_fails_open_and_says_so_on_stdout(hook, stdin, tmp_path):
    """The whole shape: exit 0; nothing written; stdout is EXACTLY the documented `gate_import_failed` notice
    and nothing else; exactly ONE stderr line, the refusal naming the value (not a second hook-own line, not
    the refusal twice)."""
    r, written = _run(HOOKS_DIR / hook, tmp_path, stdin=stdin)
    assert r.returncode == 0, (r.returncode, r.stderr[-600:])
    assert written == ["cwd", "home"], written
    assert r.stdout == json.dumps(GATE_IMPORT_FAILED_NOTICE) + "\n", r.stdout
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and VALUE in lines[0] and "ENGRAM_HOME" in lines[0], r.stderr


@pytest.mark.parametrize("hook", [h for h in HOOKS if h not in NO_HOME][:3])
def test_control_an_absolute_engram_home_is_not_refused(hook, tmp_path):
    """Positive control: the same run with an absolute home does not print the refusal line."""
    home = tmp_path / "home"
    home.mkdir()
    eh = tmp_path / "eh"
    eh.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "ENGRAM_HOME": str(eh), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, str(HOOKS_DIR / hook)], input="{}", capture_output=True, text=True,
                       env=env, cwd=str(cwd), timeout=60)
    assert "must be an absolute path" not in r.stderr, r.stderr


# tools that resolve the home at import: they exit NON-zero with the message (a tool, unlike a hook, has
# a caller who must see the failure)
TOOLS_THAT_RESOLVE_AT_IMPORT = ["ia.py", "forum.py", "baton.py", "presence.py", "deference_baseline.py",
                                "antml_repair_baseline.py", "_status_derive.py"]


@pytest.mark.parametrize("tool", TOOLS_THAT_RESOLVE_AT_IMPORT)
def test_a_tool_refuses_a_relative_engram_home_non_zero_with_the_message(tool, tmp_path):
    r, written = _run(TOOLS_DIR / tool, tmp_path, args=("--help",), stdin="")
    assert r.returncode != 0, (tool, r.returncode, r.stdout[-200:])
    assert "ENGRAM_HOME must be an absolute path" in r.stderr and VALUE in r.stderr, r.stderr[-400:]
    assert written == ["cwd", "home"], written


# `~/store` is NOT refused (the resolver expands it), but modules that read the raw env var
# (the log emitter, the telemetry store, the inline-guard hooks' own ENGRAM_HOME) must then see the
# EXPANDED value: round-2 fairy found a literal `~` directory created under the cwd.
PAYLOAD = json.dumps({"session_id": "s1", "prompt": "hello", "transcript_path": "", "hook_event_name": "Stop",
                      "cwd": "/tmp", "stop_hook_active": False})


_RUNNER = (
    "import atexit, os, runpy, sys\n"
    "atexit.register(lambda: sys.stderr.write('ENV_AT_EXIT=%s\\n' % os.environ.get('ENGRAM_HOME')))\n"
    "runpy.run_path(sys.argv[1], run_name='__main__')\n"
)


@pytest.mark.parametrize("hook", [h for h in HOOKS if h not in NO_HOME])
def test_a_tilde_engram_home_is_absolute_for_every_raw_reader(hook, tmp_path):
    """The process's ENGRAM_HOME after the hook's guard is the EXPANDED, absolute value, so a module that
    reads the raw env var cannot create a literal `~` directory under the cwd. Mutation: drop the write-back
    in a lib helper or an inline guard -> that hook's cell is red."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "ENGRAM_HOME": "~/store", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", _RUNNER, str(HOOKS_DIR / hook)], input=PAYLOAD,
                       capture_output=True, text=True, env=env, cwd=str(cwd), timeout=60)
    assert "must be an absolute path" not in r.stderr, r.stderr
    seen = [ln for ln in r.stderr.splitlines() if ln.startswith("ENV_AT_EXIT=")]
    assert seen == [f"ENV_AT_EXIT={home}/store"], (seen, r.stderr[-300:])
    assert [p for p in tmp_path.rglob("*") if "~" in p.parts[len(tmp_path.parts):]] == []


@pytest.mark.parametrize("lib", ["_hooklib", "_prompthooklib"])
def test_each_hook_lib_alone_writes_the_expanded_home_back(lib, tmp_path):
    """The prompt hooks also reach _hooklib's write-back, so the hook cells above cannot tell the two libs
    apart: dropping the write-back from _prompthooklib alone stayed green (fairy round 3). Import each lib
    on its own and look at the env var."""
    home = tmp_path / "home"
    home.mkdir()
    code = ("import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import %s as m\n"
            "m.engram_home_or_exit()\n"
            "print('ENV=' + os.environ['ENGRAM_HOME'])\n" % lib)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "ENGRAM_HOME": "~/store", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", code, str(HOOKS_DIR)], capture_output=True, text=True, env=env,
                       cwd=str(tmp_path), timeout=60)
    assert r.stdout.strip() == f"ENV={home}/store", (r.stdout, r.stderr[-300:])
