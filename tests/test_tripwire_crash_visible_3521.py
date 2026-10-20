"""Behaviour contract for making a lesson-tripwire hook CRASH visible (#3521 row 9).

Written BEFORE the fix (contract-first). The defect: `main()` ends in an outer
`except Exception: sys.exit(0)`, so "the tripwire crashed" (exit 0, empty
stdout, empty stderr) was byte-identical to "no lesson matched".

Contract (when an exception reaches main()'s outer handler):
  C1 exactly one stderr line, written by
     `_hooklib.write_probe_unknown_stderr("engram-lesson-tripwire-hook",
     "tripwire-run", f"{type(exc).__name__}: {exc}")`.
  C2 if and only if NO response JSON has been printed yet in this run AND
     `_hooklib.probe_notice_due(ENGRAM_HOME, "tripwire-run")` is true: print ONE
     PreToolUse response whose `additionalContext` is exactly one line from
     `_hooklib.probe_unknown_line("lesson-tripwire", reason, <suffix>,
     prefix="[lesson-tripwire] ")` (exception TYPE only, never the raw
     message); flush stdout, and only after a successful flush call
     `_hooklib.mark_probe_notice(ENGRAM_HOME, "tripwire-run")`.
  C3 inside the rate-limit window: the stderr line only, nothing on stdout.
  C4 if the exception comes AFTER a response was already printed: nothing more
     on stdout (never two JSON objects).
  C5 exit 0 always, never deny; the handler itself never raises (a failure
     inside it falls back to today's silent exit 0). This includes the case where
     the stdout reader has gone away: the failed flush must not leave a stamp
     and must not let interpreter shutdown replace the exit code (it was 120).
  C6 healthy path byte-identical to dev (no exception): same stdout / stderr /
     exit for a no-match run and for a matching run.
Out of scope: the early `json.load(sys.stdin)` failure (malformed harness input)
keeps its silent exit.

Cell design: every crash cell has a CONTROL (the healthy cells C6) and the
healthy cells compare against the dev hook text (`git show origin/dev:...`),
copied beside the same `_hooklib`.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK_NAME = "engram-lesson-tripwire-hook.py"
HOOK_REL = "src/engram/hooks/claude/" + HOOK_NAME
if str(HOOKS_SRC) not in sys.path:
    sys.path.insert(0, str(HOOKS_SRC))
import _hooklib  # noqa: E402

RAW_MSG = "planted-secret-msg-xyz"
STAMP_NAME = _hooklib.PROBE_NOTICE_STAMP_NAME
KEY = "tripwire-run"
MATCH_CMD = "git push --force origin main"
NOMATCH_CMD = "ls -la"
PLANT_TOP = f'    raise RuntimeError("{RAW_MSG}")\n'


def _dev_hook_text(required=True):
    for ref in ("origin/dev", "dev"):
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{ref}:{HOOK_REL}"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
    if not required:
        return None
    pytest.skip("no origin/dev or dev ref available to compare against")


def _plant_top(text):
    """Insert a raise as the first statement of build_match_target (the
    parent's reproduction of the defect)."""
    marker = "def build_match_target(hook_input: dict) -> str:\n"
    i = text.index(marker)
    j = text.index('    """\n', text.index('"""', i) + 3) + len('    """\n')
    return text[:j] + PLANT_TOP + text[j:]


def _plant_after_print(text):
    """Raise right after the normal-path response was printed (the loader-notice
    flush position)."""
    marker = "        print(json.dumps(response))\n        if loader_notice:\n"
    flagged = "        print(json.dumps(response))\n        response_printed = True\n        if loader_notice:\n"
    plant = f'        raise RuntimeError("{RAW_MSG}")\n'
    if flagged in text:  # fixed hook: plant after the printed-flag line
        return text.replace(flagged, flagged.replace("        if loader_notice:\n", plant + "        if loader_notice:\n"))
    assert text.count(marker) == 1
    return text.replace(marker, marker.replace("        if loader_notice:\n", plant + "        if loader_notice:\n"))


@pytest.fixture()
def sandbox(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for p in HOOKS_SRC.iterdir():
        if p.is_file() and p.suffix == ".py":
            shutil.copy2(p, hooks / p.name)
    # the deployed layout ships the flag helper at the plugin root (#3665)
    shutil.copy2(REPO_ROOT / "src" / "engram" / "engramd_owner_flags.py", tmp_path / "engramd_owner_flags.py")
    home = tmp_path / "home"
    eh = home / ".engram"
    eh.mkdir(parents=True)
    return {"hooks": hooks, "home": home, "eh": eh, "tmp": tmp_path}


def _install(sandbox, text, name="hook.py"):
    path = sandbox["hooks"] / name
    path.write_text(text, encoding="utf-8")
    return path


def _run(sandbox, hook_path, command):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM")}
    env["HOME"] = str(sandbox["home"])
    env["ENGRAM_HOME"] = str(sandbox["eh"])
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    r = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload), capture_output=True, text=True,
        timeout=60, env=env, cwd=str(sandbox["tmp"]),
    )
    return r


def _make_matching_db(eh):
    db = eh / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE nodes (id TEXT, type TEXT, is_current INTEGER, "
        "memory_status TEXT, claim TEXT, metadata TEXT, recall_summary TEXT)"  # recall_summary: alpha#3924
    )
    meta = {
        "situation_pattern": "git push --force",
        "prevention_type": "shadow",
        "scaffolding_nudge": "fixture nudge: do not force push",
        "tripwire_priority": "high",
    }
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, claim, metadata) VALUES (?,?,?,?,?,?)",
        ("ls_fix1", "lesson", 1, "active", "fixture claim", json.dumps(meta)),
    )
    conn.commit()
    conn.close()
    return db


def _stamp(sandbox):
    return sandbox["eh"] / STAMP_NAME


def _stderr_lines(r):
    return [ln for ln in r.stderr.split("\n") if ln]


def _assert_visible_crash(r):
    assert r.returncode == 0
    lines = _stderr_lines(r)
    assert len(lines) == 1, r.stderr
    assert "tripwire-run" in lines[0] and "RuntimeError" in lines[0]
    obj = json.loads(r.stdout)
    hso = obj["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    ctx = hso["additionalContext"]
    assert "\n" not in ctx
    assert "UNKNOWN" in ctx and "RuntimeError" in ctx
    assert RAW_MSG not in ctx
    assert ctx.startswith("[lesson-tripwire] ")


# --- C1/C2/C5: planted exception, first run -----------------------------------

def test_c2_planted_exception_is_visible(sandbox):
    dev_text = _dev_hook_text(required=False)
    if dev_text is not None:
        hook = _install(sandbox, _plant_top(dev_text), "planted_dev.py")
        ctl = _run(sandbox, hook, NOMATCH_CMD)  # CONTROL: unfixed dev text stays silent
        # Documents the pre-fix state: once dev carries the fix the control is
        # moot and is not asserted (it must not go red on the fix it motivated).
        if ctl.stdout or ctl.stderr:
            _stamp(sandbox).unlink(missing_ok=True)  # dev's visible crash may have stamped the window
        else:
            assert ctl.returncode == 0
    hook = _install(sandbox, _plant_top((HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8")), "planted.py")
    r = _run(sandbox, hook, NOMATCH_CMD)
    _assert_visible_crash(r)
    assert _stamp(sandbox).exists()  # stamped after a successful flush


def test_c1_stderr_line_carries_message_on_stderr_only(sandbox):
    hook = _install(sandbox, _plant_top((HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8")), "planted.py")
    r = _run(sandbox, hook, NOMATCH_CMD)
    assert RAW_MSG in _stderr_lines(r)[0]
    assert RAW_MSG not in r.stdout


# --- C3: rate limit -----------------------------------------------------------

def test_c3_second_run_in_window_is_stderr_only_then_window_expiry(sandbox):
    hook = _install(sandbox, _plant_top((HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8")), "planted.py")
    first = _run(sandbox, hook, NOMATCH_CMD)
    _assert_visible_crash(first)
    second = _run(sandbox, hook, NOMATCH_CMD)
    assert second.returncode == 0 and second.stdout == ""
    assert len(_stderr_lines(second)) == 1
    aged = time.time() - _hooklib.PROBE_NOTICE_WINDOW_SECONDS - 60
    _stamp(sandbox).write_text(json.dumps({"shown_at": {KEY: aged}}), encoding="utf-8")
    third = _run(sandbox, hook, NOMATCH_CMD)
    _assert_visible_crash(third)


# --- unwritable stamp fails visible -------------------------------------------

def test_unwritable_stamp_shows_line_every_run(sandbox):
    _stamp(sandbox).mkdir()  # a directory: neither readable nor replaceable as a file
    hook = _install(sandbox, _plant_top((HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8")), "planted.py")
    for _ in range(2):
        _assert_visible_crash(_run(sandbox, hook, NOMATCH_CMD))


# --- C4: exception after the response was printed ------------------------------

def test_c4_exception_after_print_emits_no_second_json(sandbox):
    _make_matching_db(sandbox["eh"])
    src = (HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8")
    # CONTROL: the matching fixture alone prints exactly one JSON object.
    ctl = _run(sandbox, _install(sandbox, src, "fixed.py"), MATCH_CMD)
    assert "fixture nudge" in json.loads(ctl.stdout)["hookSpecificOutput"]["additionalContext"]
    hook = _install(sandbox, _plant_after_print(src), "after_print.py")
    r = _run(sandbox, hook, MATCH_CMD)
    assert r.returncode == 0
    dec = json.JSONDecoder()
    obj, end = dec.raw_decode(r.stdout)
    assert r.stdout[end:].strip() == "", r.stdout  # nothing after the first object
    assert "fixture nudge" in obj["hookSpecificOutput"]["additionalContext"]
    lines = _stderr_lines(r)
    assert len(lines) == 1 and "tripwire-run" in lines[0]
    assert not _stamp(sandbox).exists()  # no notice was shown, so no stamp


# --- C5: stdout reader gone ----------------------------------------------------

def test_c5_closed_stdout_pipe_exits_zero_and_leaves_no_stamp(sandbox):
    hook = _install(sandbox, _plant_top((HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8")), "planted.py")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM")}
    env["HOME"] = str(sandbox["home"])
    env["ENGRAM_HOME"] = str(sandbox["eh"])
    p = subprocess.Popen(
        [sys.executable, str(hook)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, cwd=str(sandbox["tmp"]),
    )
    p.stdout.close()  # the reader is gone before the hook writes anything
    p.stdin.write(json.dumps({"tool_name": "Bash", "tool_input": {"command": NOMATCH_CMD}}).encode())
    p.stdin.close()
    try:
        p.stderr.read()
        rc = p.wait(timeout=60)
    finally:
        p.stderr.close()
    assert rc == 0
    assert not _stamp(sandbox).exists()  # a failed flush must not consume the window


# --- C6: healthy path byte-identical to dev ------------------------------------

def _healthy_pair(sandbox, command):
    dev = _install(sandbox, _dev_hook_text(), "dev_hook.py")
    new = _install(sandbox, (HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8"), "new_hook.py")
    return _run(sandbox, dev, command), _run(sandbox, new, command)


def test_c6a_healthy_no_match_identical(sandbox):
    a, b = _healthy_pair(sandbox, NOMATCH_CMD)
    assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr) == (0, "", "")
    assert not _stamp(sandbox).exists()


def test_c6b_healthy_matching_lesson_identical(sandbox):
    _make_matching_db(sandbox["eh"])
    a, b = _healthy_pair(sandbox, MATCH_CMD)
    assert "fixture nudge" in a.stdout  # the fixture really matches
    assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr)
    assert not _stamp(sandbox).exists()


# --- C7: an in-process caller survives the crash path (round 3) -----------------
# os._exit(0) on the crash path kills a pytest process that calls main()
# in-process (it ended a run with no summary line and rc=0). Only a hook run
# as a script may take that branch; an in-process caller must get SystemExit.

_CHILD_TEST = '''
import importlib.util, io, json, os, sys
import pytest


def test_crash_path_raises_catchable_systemexit(monkeypatch):
    spec = importlib.util.spec_from_file_location("tripwire_under_test", os.environ["HOOK_UNDER_TEST"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})))

    def boom(*a, **k):
        raise RuntimeError("dumps exploded")

    monkeypatch.setattr(mod.json, "dumps", boom)  # the handler's own print fails too
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0


def test_second_test_still_runs():
    assert True
'''


def test_c7_in_process_crash_path_does_not_kill_the_test_process(sandbox):
    hook = _install(sandbox, _plant_top((HOOKS_SRC / HOOK_NAME).read_text(encoding="utf-8")), "planted.py")
    child = sandbox["tmp"] / "test_child_inproc.py"
    child.write_text(_CHILD_TEST, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM")}
    env["HOME"] = str(sandbox["home"])
    env["ENGRAM_HOME"] = str(sandbox["eh"])
    env["HOOK_UNDER_TEST"] = str(hook)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(child)],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(sandbox["tmp"]),
    )
    summary = [ln for ln in r.stdout.splitlines() if ln.strip()][-1] if r.stdout.strip() else ""
    # Assert on the SUMMARY LINE: a killed pytest prints none (and exits 0).
    assert " 2 passed" in " " + summary, (r.returncode, r.stdout, r.stderr)
    assert r.returncode == 0
