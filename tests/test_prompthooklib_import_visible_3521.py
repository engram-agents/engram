"""engram-alpha#3521 (the row #3544 carried in): a failed `_prompthooklib`
import must not be a silent exit 0.

CONTRACT (maintainer ruling; stated here BEFORE the fix, cells written first).
When `from _prompthooklib import ...` fails in a prompt hook, the hook must
still exit 0, and it emits:

  1. EXACTLY ONE stderr line:
         <hook-file-stem>: _prompthooklib import failed: <ExceptionClass>
     The class name only -- never the exception message.
  2. EXACTLY ONE injected UserPromptSubmit context line with the same
     class-only text, printed through the hook's OWN minimal
         json.dumps({"hookSpecificOutput": {"hookEventName":
             "UserPromptSubmit", "additionalContext": line}})
     because the lib (and its `emit_context`) is the thing that is gone.
  3. NO rate limit: both lines appear on every run. A broken install is rare
     and severe; the per-prompt line is what gets it fixed.
  4. The HEALTHY path is byte-identical to dev: with a working
     `_prompthooklib`, stdout, stderr and exit code match dev's hook on the
     same fixture.
  5. The emission happens once per run, at the point where the hook would
     otherwise no-op (not at import time), and it never raises; if the
     emission itself fails the hook falls back to today's silent exit 0.

Every cell runs the hook as a subprocess on a scratch copy of the hooks
directory (so the real `_prompthooklib.py` can be replaced or deleted), with a
hard timeout and a scratch ENGRAM_HOME / HOME.

Scope by reproduction ("not reproduced is not a row"): the forum hook is
covered unconditionally; the baton and inter-agent hooks are covered only by
the cells further down, each of which first proves a healthy control that
PRINTS something.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
TOOLS_DIR = PROJECT_ROOT / "src" / "engram" / "tools"

FORUM_HOOK = "engram-forum-prompt-hook.py"
AGENT = "luria"
# The raise message must never appear in either emitted line.
SECRET = "SECRET-MESSAGE-3521-do-not-echo"
TIMEOUT = 60


# ---------------------------------------------------------------------------
# Scratch driver
# ---------------------------------------------------------------------------

def _dev_hook_text(hook_name: str):
    """The hook as it is on origin/dev, or None when that ref is unavailable
    (a shallow checkout); the dev-comparison cells skip in that case."""
    r = subprocess.run(
        ["git", "show", f"origin/dev:src/engram/hooks/claude/{hook_name}"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=TIMEOUT,
    )
    return r.stdout if r.returncode == 0 and r.stdout else None


def _scratch(tmp_path: Path, name: str, lib_mode: str, hook_name: str,
             hook_text: str = None) -> Path:
    """A scratch copy of the hooks dir (with `tools/` reachable by the same
    walk-parents rule the real layout uses). Returns the hook path.

    lib_mode: "real" (untouched), "raise" (`_prompthooklib.py` raises
    RuntimeError on import) or "absent" (`_prompthooklib.py` deleted)."""
    root = tmp_path / name
    hooks = root / "engram" / "hooks" / "claude"
    hooks.mkdir(parents=True)
    for src in HOOK_DIR.glob("*.py"):
        shutil.copy2(src, hooks / src.name)
    (root / "engram" / "tools").symlink_to(TOOLS_DIR, target_is_directory=True)
    # #3983: the bounded sibling-file walk anchors on the tree-root marker
    # `engram_core.py` (every real source/plugin tree has one). Without it the
    # walk falls back to two levels and never reaches `engram/tools`.
    (root / "engram" / "engram_core.py").write_text("")
    lib = hooks / "_prompthooklib.py"
    if lib_mode == "raise":
        lib.write_text(f"raise RuntimeError({SECRET!r})\n")
    elif lib_mode == "absent":
        lib.unlink()
    else:
        assert lib_mode == "real"
    if hook_text is not None:
        (hooks / hook_name).write_text(hook_text)
    return hooks / hook_name


def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def _home(tmp_path: Path, name: str = "engram_home") -> Path:
    home = tmp_path / name
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps(
        {"agent_name": AGENT, "forum": {"url": _dead_url()}}))
    return home


def _run(hook: Path, home: Path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    p = subprocess.run(
        [sys.executable, str(hook)], input="{}", capture_output=True, text=True,
        env=env, timeout=TIMEOUT,
    )
    return p.returncode, p.stdout, p.stderr


def _ctx(stdout: str) -> str:
    """additionalContext of the single JSON object on stdout (asserts it IS a
    single object: one json.loads over the whole output, no trailing text)."""
    obj = json.loads(stdout)
    assert list(obj) == ["hookSpecificOutput"], obj
    inner = obj["hookSpecificOutput"]
    assert inner["hookEventName"] == "UserPromptSubmit", inner
    return inner["additionalContext"]


def _assert_visible(hook: Path, rc, out, err, exc_class: str):
    expected = f"{hook.stem}: _prompthooklib import failed: {exc_class}"
    assert rc == 0, (rc, out, err)
    ctx = _ctx(out)
    assert "\n" not in ctx, ctx
    assert ctx == expected, ctx
    err_lines = err.splitlines()
    assert err_lines == [expected], err
    assert out.count("\n") <= 1  # one object, at most its trailing newline
    for text in (out, err):
        assert SECRET not in text, text


# ---------------------------------------------------------------------------
# Forum hook
# ---------------------------------------------------------------------------

def test_forum_healthy_control_prints_and_is_byte_identical_to_dev(tmp_path):
    dev_text = _dev_hook_text(FORUM_HOOK)
    if dev_text is None:
        pytest.skip("origin/dev not available")
    home_a = _home(tmp_path, "home_a")
    home_b = tmp_path / "home_b"
    shutil.copytree(home_a, home_b)  # same dead URL, separate stamp/cache state
    head = _scratch(tmp_path, "head", "real", FORUM_HOOK)
    dev = _scratch(tmp_path, "dev", "real", FORUM_HOOK, hook_text=dev_text)
    h = _run(head, home_a)
    d = _run(dev, home_b)
    assert h[1].strip(), "control must PRINT something (unreachable-forum line)"
    assert "forum unreachable" in _ctx(h[1])
    assert h == d


@pytest.mark.parametrize("lib_mode,exc_class", [
    ("raise", "RuntimeError"),
    ("absent", "ModuleNotFoundError"),
])
def test_forum_broken_lib_is_visible_not_silent(tmp_path, lib_mode, exc_class):
    hook = _scratch(tmp_path, "h", lib_mode, FORUM_HOOK)
    rc, out, err = _run(hook, _home(tmp_path))
    _assert_visible(hook, rc, out, err, exc_class)


@pytest.mark.parametrize("lib_mode,exc_class", [
    ("raise", "RuntimeError"),
    ("absent", "ModuleNotFoundError"),
])
def test_forum_broken_lib_two_runs_in_a_row_both_show_the_line(
        tmp_path, lib_mode, exc_class):
    hook = _scratch(tmp_path, "h", lib_mode, FORUM_HOOK)
    home = _home(tmp_path)
    for _ in range(2):
        rc, out, err = _run(hook, home)
        _assert_visible(hook, rc, out, err, exc_class)


def test_forum_broken_lib_emission_failure_falls_back_to_silent_exit_0(tmp_path):
    """Clause 5: if the emission itself cannot be made, exit 0 and silence.
    stdout and stderr both closed -> every write raises; exit code stays 0."""
    hook = _scratch(tmp_path, "h", "raise", FORUM_HOOK)
    home = _home(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    runner = (
        "import runpy, sys\n"
        "class _Bad:\n"
        "    def write(self, *a, **k): raise OSError('closed')\n"
        "    def flush(self): raise OSError('closed')\n"
        "sys.stdout = _Bad(); sys.stderr = _Bad()\n"
        "code = 0\n"
        "try:\n"
        f"    runpy.run_path({str(hook)!r}, run_name='__main__')\n"
        "except SystemExit as e:\n"
        "    code = e.code or 0\n"
        "finally:\n"
        "    sys.stdout = sys.__stdout__; sys.stderr = sys.__stderr__\n"
        "sys.exit(code)\n"
    )
    p = subprocess.run([sys.executable, "-c", runner], input="{}",
                       capture_output=True, text=True, env=env, timeout=TIMEOUT)
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "" and p.stderr == ""


def test_forum_broken_lib_is_silent_at_dev(tmp_path):
    """The reproduction: dev's hook with a raising lib prints nothing at all."""
    dev_text = _dev_hook_text(FORUM_HOOK)
    if dev_text is None:
        pytest.skip("origin/dev not available")
    hook = _scratch(tmp_path, "dev", "raise", FORUM_HOOK, hook_text=dev_text)
    rc, out, err = _run(hook, _home(tmp_path))
    # Documents dev's behaviour; once the fix is on dev this cell is moot and
    # self-skips rather than going red on the very fix it motivated.
    if out or err:
        pytest.skip("dev already carries the visible import failure")
    assert rc == 0


# ---------------------------------------------------------------------------
# Closed-pipe exit code + control-character sanitizer (round 2)
# ---------------------------------------------------------------------------

def _run_closed_pipe(hook: Path, home: Path, close_out: bool, close_err: bool):
    """Run the hook with the READER end of stdout and/or stderr closed right
    after Popen, so the hook's writes hit a real broken pipe. Returns the exit
    code. (Clause 5 on a REAL closed pipe, not on fake stream objects: the
    interpreter's shutdown flush is part of what must not turn this into a
    non-zero exit.)"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    p = subprocess.Popen(
        [sys.executable, str(hook)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    if close_out:
        p.stdout.close()
    if close_err:
        p.stderr.close()
    try:
        p.stdin.write(b"{}")
        p.stdin.close()
    except OSError:
        pass
    try:
        rc = p.wait(timeout=TIMEOUT)
    finally:
        for f in (p.stdout, p.stderr):
            if f is not None and not f.closed:
                f.close()
    return rc


CLOSED_PIPE_CASES = [
    pytest.param(True, False, id="stdout-reader-closed"),
    pytest.param(False, True, id="stderr-reader-closed"),
    pytest.param(True, True, id="both-readers-closed"),
]


@pytest.mark.parametrize("close_out,close_err", CLOSED_PIPE_CASES)
def test_forum_broken_lib_closed_pipe_still_exits_0(tmp_path, close_out, close_err):
    hook = _scratch(tmp_path, "h", "raise", FORUM_HOOK)
    rc = _run_closed_pipe(hook, _home(tmp_path), close_out, close_err)
    assert rc == 0, rc


def test_forum_broken_lib_class_name_control_chars_are_sanitized(tmp_path):
    """An exception class whose __name__ holds a newline and an ESC yields ONE
    stderr line and ONE context line, control characters replaced by '?'."""
    hook = _scratch(tmp_path, "h", "real", FORUM_HOOK)
    (hook.parent / "_prompthooklib.py").write_text(
        "raise type('Bad\\nName\\x1b[31m', (Exception,), {})('x')\n")
    rc, out, err = _run(hook, _home(tmp_path))
    expected = f"{hook.stem}: _prompthooklib import failed: Bad?Name?[31m"
    assert rc == 0, (rc, out, err)
    assert _ctx(out) == expected
    assert err.splitlines() == [expected], err
    assert "\x1b" not in out and "\x1b" not in err


# ---------------------------------------------------------------------------
# Baton and inter-agent hooks (in scope because the healthy control PRINTS:
# with a working lib, the scratch tree's `tools/` reachable and the forum
# unreachable, each emits its UCS-unreachable banner; with the lib broken,
# dev's hook is silent -- that is the reproduction).
# ---------------------------------------------------------------------------

OTHER_HOOKS = [
    ("engram-baton-prompt-hook.py", "UCS unreachable"),
    ("engram-inter-agent-prompt-hook.py", "DM unread check unavailable"),
]


@pytest.mark.parametrize("hook_name,marker", OTHER_HOOKS)
def test_other_healthy_control_prints_and_is_byte_identical_to_dev(
        tmp_path, hook_name, marker):
    dev_text = _dev_hook_text(hook_name)
    if dev_text is None:
        pytest.skip("origin/dev not available")
    home_a = _home(tmp_path, "home_a")
    home_b = tmp_path / "home_b"
    shutil.copytree(home_a, home_b)
    head = _scratch(tmp_path, "head", "real", hook_name)
    dev = _scratch(tmp_path, "dev", "real", hook_name, hook_text=dev_text)
    h = _run(head, home_a)
    d = _run(dev, home_b)
    assert marker in _ctx(h[1]), "control must PRINT its banner"
    assert h == d


@pytest.mark.parametrize("hook_name,marker", OTHER_HOOKS)
@pytest.mark.parametrize("lib_mode,exc_class", [
    ("raise", "RuntimeError"),
    ("absent", "ModuleNotFoundError"),
])
def test_other_broken_lib_is_visible_not_silent(
        tmp_path, hook_name, marker, lib_mode, exc_class):
    hook = _scratch(tmp_path, "h", lib_mode, hook_name)
    rc, out, err = _run(hook, _home(tmp_path))
    _assert_visible(hook, rc, out, err, exc_class)
    assert marker not in out  # the lib-gone line, not the healthy banner


@pytest.mark.parametrize("hook_name,marker", OTHER_HOOKS)
def test_other_broken_lib_two_runs_in_a_row_both_show_the_line(
        tmp_path, hook_name, marker):
    hook = _scratch(tmp_path, "h", "raise", hook_name)
    home = _home(tmp_path)
    for _ in range(2):
        rc, out, err = _run(hook, home)
        _assert_visible(hook, rc, out, err, "RuntimeError")


@pytest.mark.parametrize("close_out,close_err", CLOSED_PIPE_CASES)
@pytest.mark.parametrize("hook_name,marker", OTHER_HOOKS)
def test_other_broken_lib_closed_pipe_still_exits_0(
        tmp_path, hook_name, marker, close_out, close_err):
    hook = _scratch(tmp_path, "h", "raise", hook_name)
    rc = _run_closed_pipe(hook, _home(tmp_path), close_out, close_err)
    assert rc == 0, rc


@pytest.mark.parametrize("hook_name,marker", OTHER_HOOKS)
def test_other_broken_lib_is_silent_at_dev(tmp_path, hook_name, marker):
    """The reproduction: dev's hook with a raising lib prints nothing."""
    dev_text = _dev_hook_text(hook_name)
    if dev_text is None:
        pytest.skip("origin/dev not available")
    hook = _scratch(tmp_path, "dev", "raise", hook_name, hook_text=dev_text)
    rc, out, err = _run(hook, _home(tmp_path))
    if out or err:
        pytest.skip("dev already carries the visible import failure")
    assert rc == 0


# ---------------------------------------------------------------------------
# In-process caller survives the failure path (round 3)
# ---------------------------------------------------------------------------
# os._exit(0) on the failure path kills a pytest process that calls main()
# in-process (a run then ends with no summary line and rc=0). Only a hook run
# as a script may take that branch; an in-process caller must get SystemExit.

_CHILD_INPROC_TEST = '''
import importlib.util, io, os, sys
import pytest


def test_failure_path_raises_catchable_systemexit(monkeypatch):
    spec = importlib.util.spec_from_file_location("hook_under_test", os.environ["HOOK_UNDER_TEST"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._PROMPTHOOKLIB_OK is False
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0


def test_second_test_still_runs():
    assert True
'''


def _assert_inproc_survives(tmp_path: Path, hook_name: str):
    hook = _scratch(tmp_path, "inproc", "raise", hook_name)
    child = tmp_path / "test_child_inproc.py"
    child.write_text(_CHILD_INPROC_TEST)
    home = _home(tmp_path, "inproc_home")
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    env["HOOK_UNDER_TEST"] = str(hook)
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(child)],
        capture_output=True, text=True, env=env, timeout=TIMEOUT, cwd=str(tmp_path),
    )
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    summary = lines[-1] if lines else ""
    # Assert on the SUMMARY LINE: a killed pytest prints none (and exits 0).
    assert " 2 passed" in " " + summary, (p.returncode, p.stdout, p.stderr)
    assert p.returncode == 0


def test_forum_in_process_failure_path_does_not_kill_the_test_process(tmp_path):
    _assert_inproc_survives(tmp_path, FORUM_HOOK)


@pytest.mark.parametrize("hook_name,marker", OTHER_HOOKS)
def test_other_in_process_failure_path_does_not_kill_the_test_process(
        tmp_path, hook_name, marker):
    _assert_inproc_survives(tmp_path, hook_name)


@pytest.mark.parametrize("hook_name,marker", OTHER_HOOKS)
def test_other_broken_lib_class_name_control_chars_are_sanitized(
        tmp_path, hook_name, marker):
    """Same shape as the forum cell: an exception class whose __name__ holds a
    newline and an ESC yields ONE stderr line and ONE context line, control
    characters replaced by '?', no ESC in either stream."""
    hook = _scratch(tmp_path, "h", "real", hook_name)
    (hook.parent / "_prompthooklib.py").write_text(
        "raise type('Bad\\nName\\x1b[31m', (Exception,), {})('x')\n")
    rc, out, err = _run(hook, _home(tmp_path))
    expected = f"{hook.stem}: _prompthooklib import failed: Bad?Name?[31m"
    assert rc == 0, (rc, out, err)
    assert _ctx(out) == expected
    assert err.splitlines() == [expected], err
    assert "\x1b" not in out and "\x1b" not in err
