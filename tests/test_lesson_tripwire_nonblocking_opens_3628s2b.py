"""#3628 slice 2b: the lesson-tripwire PreToolUse hook must never block on a
non-regular file (FIFO) and must stay regular-file-only.

This hook runs on EVERY tool call. A plain `open()` / `Path.read_text()` of a
path where a FIFO sits waits for a writer; a plain write or append waits for a
reader. The harness then times the hook out and kills it silently, and a
`try/except Exception` is fail-open for an exception, not for a hang. #3591 /
#3628 slice 2a fixed the shared libs and the small hooks; this file covers
`engram-lesson-tripwire-hook.py` (every file open; the sqlite connects are
slice 2d).

Each cell drives the REAL function in a SUBPROCESS with a hard timeout, so a
regression fails (timeout) instead of hanging the suite. Every cell runs twice:

  * kind="fifo"    -- a FIFO at the path; must return promptly with the
                      documented fail-open fallback;
  * kind="regular" -- a regular file at the path; positive control that the
                      real behaviour is unchanged (green before and after).

Further groups: a PermissionError-on-a-REGULAR-file group (the handlers are
`except Exception`, so the hook stays fail-open exactly as on origin/dev); the
WHOLE-HOOK cells (the real main() with a FIFO planted at the paths the hook
touches); and a SOURCE GUARD that walks the file's AST and fails on any raw
`open()` / `os.open()` / `Path.read_text|write_text|read_bytes|write_bytes`
call, so a site no cell can drive still cannot regress to a blocking open.

Fixture lesson ids are `lfix-*` on purpose: the hook's real ids look like
graph node ids and must not appear in test sources.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK_FILE = "engram-lesson-tripwire-hook.py"

GUARD_S = int(os.environ.get("HOOKS_3628S2B_GUARD_S", "8"))
WHOLE_GUARD_S = int(os.environ.get("HOOKS_3628S2B_WHOLE_GUARD_S", "40"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")

# Runs inside the child: load the module by path, exec the cell's code, print JSON.
_CHILD = r"""
import importlib.util, json, os, sys
spec_in = json.loads(sys.argv[1])
sys.path.insert(0, os.path.dirname(spec_in["hook"]))
spec = importlib.util.spec_from_file_location("hook_3628s2b_child", spec_in["hook"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = spec_in["paths"]
ns = {"mod": mod, "P": P, "os": os, "json": json, "result": None}
exec(spec_in["code"], ns)
print("RESULT:" + json.dumps(ns["result"]))
"""

_ENV_DROP = ("ENGRAM_RUNTIME_DIR", "CLAUDE_PLUGIN_ROOT", "CODEX_PLUGIN_ROOT", "PYTHONPATH")


class Ctx:
    def __init__(self, tmp: Path, kind: str):
        self.kind = kind
        self.tmp = tmp
        self.home = tmp / "home"
        self.eh = self.home / ".engram"
        self.eh.mkdir(parents=True, exist_ok=True)
        self.hook = str(HOOK_DIR / HOOK_FILE)

    def special(self, path: Path, regular_text: str):
        """A FIFO (kind=fifo) or a regular file holding `regular_text` at path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_text(regular_text)

    def paths(self):
        return {"home": str(self.home), "eh": str(self.eh), "tmp": str(self.tmp)}

    def env(self):
        env = dict(os.environ)
        env.update({"HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
                    "ENGRAM_SESSION_ROLE": "interactive"})
        for k in _ENV_DROP:
            env.pop(k, None)
        return env


def _run(ctx: Ctx, code: str, timeout=GUARD_S):
    payload = json.dumps({"hook": ctx.hook, "paths": ctx.paths(), "code": code})
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD, payload],
                              capture_output=True, text=True, timeout=timeout, env=ctx.env())
    except subprocess.TimeoutExpired:
        pytest.fail(f"blocked for more than {timeout}s on a non-regular file "
                    f"({HOOK_FILE}, kind={ctx.kind})")
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


# --------------------------------------------------------------------------
# Cells: name -> prepare(ctx) -> (code, {kind: predicate})
# --------------------------------------------------------------------------

CELLS = {}
# cells whose target is READ under a handler that must also stay fail-open for a
# PermissionError on a regular file: name -> file (under the engram home)
PERM_CELLS = {}


def cell(name, perm_file=None):
    def deco(fn):
        CELLS[name] = fn
        if perm_file:
            PERM_CELLS[name] = perm_file
        return fn
    return deco


_STAT = "import stat\n"

_LEDGER_APPEND_CHECK = (
    "result = 'fifo' if stat.S_ISFIFO(os.stat(p).st_mode) else open(p).read()")


@cell("loader_notice_due_stamp_read", perm_file="lesson-tripwire-loader-notice.json")
def _c_notice(ctx):
    ctx.special(ctx.eh / "lesson-tripwire-loader-notice.json", json.dumps({"shown_at": 1000.0}))
    # a regular stamp 1s old is inside the 1800s window -> not due; an unreadable one is due
    return ("result = mod._loader_notice_due(1001.0)",
            {"fifo": lambda r: r is True, "regular": lambda r: r is False})


@cell("write_tripwire_overflow")
def _c_overflow(ctx):
    # the file name is <int(now)>-<pid>.txt and the pid is the child's, so the
    # child plants the FIFO itself
    code = (_STAT +
            "d = P['eh'] + '/tripwire-overflow'\n"
            "os.makedirs(d, exist_ok=True)\n"
            "p = d + '/1000-' + str(os.getpid()) + '.txt'\n"
            + ("os.mkfifo(p)\n" if ctx.kind == "fifo" else "") +
            "r = mod.write_tripwire_overflow([{'node_id': 'lfix-a', 'nudge': 'n1'}], now=1000.0)\n"
            "m = os.stat(p).st_mode if os.path.lexists(p) else None\n"
            "result = [r == p, r == '', None if m is None else stat.S_ISFIFO(m),\n"
            "          open(p).read() if m is not None and stat.S_ISREG(m) else None]")
    return (code,
            {"fifo": lambda r: r == [False, True, True, None],
             "regular": lambda r: r == [True, False, False, "[lfix-a] n1\n"]})


@cell("ws4_config_read", perm_file="config.json")
def _c_ws4_config(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"experiments": {"ws4_chain_ab": {
        "enabled": True, "seed": "lfix-seed",
        "start": "2000-01-01T00:00:00Z", "end": "2999-01-01T00:00:00Z"}}}))
    return ("result = mod._ws4_config()",
            {"fifo": lambda r: r == {"active": False, "seed": None},
             "regular": lambda r: r == {"active": True, "seed": "lfix-seed"}})


@cell("ws4_fire_ledger_append")
def _c_ws4_ledger(ctx):
    ctx.special(ctx.eh / "tripwire-fire-ledger.jsonl", "pre\n")
    return (_STAT + "mod._append_ws4_fire_ledger({'k': 1})\n"
            "p = P['eh'] + '/tripwire-fire-ledger.jsonl'\n" + _LEDGER_APPEND_CHECK,
            {"fifo": lambda r: r == "fifo",
             "regular": lambda r: r == 'pre\n{"k": 1}\n'})


@cell("demotion_config_read", perm_file="config.json")
def _c_demotion(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps(
        {"lessons": {"blocking_demotion": {"overrides": 5, "window_days": 3}}}))
    return ("result = mod._demotion_config()",
            {"fifo": lambda r: r == {"overrides": 2, "window_days": 7},
             "regular": lambda r: r == {"overrides": 5, "window_days": 3.0}})


@cell("count_recent_overrides_ledger_read", perm_file="lesson-prevention-ledger.jsonl")
def _c_count(ctx):
    rows = "".join(json.dumps({"lesson_id": "lfix-a", "override_present": True, "ts": 999999.0})
                   + "\n" for _ in range(2))
    ctx.special(ctx.eh / "lesson-prevention-ledger.jsonl", rows)
    return ("result = mod._count_recent_overrides('lfix-a', 7, 1000000.0)",
            {"fifo": lambda r: r == 0, "regular": lambda r: r == 2})


@cell("log_threshold_crossed_append")
def _c_threshold(ctx):
    ctx.special(ctx.eh / "lesson-prevention-ledger.jsonl", "pre\n")
    return (_STAT +
            "mod._log_threshold_crossed(1.0, {'lesson_id': 'lfix-a', 'override_count': 2,\n"
            "                                 'threshold': 2, 'window_days': 7})\n"
            "p = P['eh'] + '/lesson-prevention-ledger.jsonl'\n" + _LEDGER_APPEND_CHECK,
            {"fifo": lambda r: r == "fifo",
             "regular": lambda r: r.startswith("pre\n") and '"mode": "auto_demoted_lease"' in r})


@cell("append_prevention_ledger_append")
def _c_prevention(ctx):
    ctx.special(ctx.eh / "lesson-prevention-ledger.jsonl", "pre\n")
    return (_STAT +
            "mod._append_prevention_ledger(1.0, [{'lesson_id': 'lfix-a', 'prevention_type': 'shadow',\n"
            "    'tool_name': 'Bash', 'session_id': 's', 'matched_span': 'x'}])\n"
            "p = P['eh'] + '/lesson-prevention-ledger.jsonl'\n" + _LEDGER_APPEND_CHECK,
            {"fifo": lambda r: r == "fifo",
             "regular": lambda r: r.startswith("pre\n") and '"mode": "shadow"' in r})


@cell("append_ledger_in_turn_append")
def _c_in_turn_ledger(ctx):
    ctx.special(ctx.eh / "in-turn-recall-ledger.jsonl", "pre\n")
    return (_STAT + "mod._append_ledger(lambda: {'a': 1})\n"
            "p = P['eh'] + '/in-turn-recall-ledger.jsonl'\n" + _LEDGER_APPEND_CHECK,
            {"fifo": lambda r: r == "fifo",
             "regular": lambda r: r == 'pre\n{"a": 1}\n'})


@cell("in_turn_config_read", perm_file="config.json")
def _c_in_turn_config(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps(
        {"auto_surface": {"in_turn_recall": {"max_lines": 9}}}))
    return ("result = mod._in_turn_config()['max_lines']",
            {"fifo": lambda r: r == 3, "regular": lambda r: r == 9})


# check_in_turn_recall: stub the idf module so gate 3 stops at an ImportError
# (returns "") and record what would be written to the state file.
_RECALL_PRELUDE = (
    "import sys, types\n"
    "sys.modules['engram_idf'] = types.ModuleType('engram_idf')\n"
    "WRITES = []\n"
    "mod.atomic_write_json = lambda path, data, **k: WRITES.append(dict(data))\n")


@cell("in_turn_recall_lock_open")
def _c_lock(ctx):
    # The lock file is opened O_CREAT|O_RDWR. Measured on macOS: that open
    # returns at once on a FIFO and the following flock fails ENOTSUP, so the
    # call suppresses (returns "") instead of hanging. The hook keeps that
    # behaviour; this cell pins it (and the 0600 mode of a real lock file).
    if ctx.kind == "fifo":
        os.mkfifo(ctx.eh / "in-turn-recall-state.json.lock")
    return (_RECALL_PRELUDE + _STAT +
            "r = mod.check_in_turn_recall('lfix-cmd words here', None)\n"
            "p = P['eh'] + '/in-turn-recall-state.json.lock'\n"
            "m = os.stat(p).st_mode\n"
            "result = [r, WRITES, 'fifo' if stat.S_ISFIFO(m) else stat.S_IMODE(m) & 0o077]",
            {"fifo": lambda r: r == ["", [], "fifo"],
             # a real lock file: still the same file, created/kept without group/other bits
             "regular": lambda r: r[0] == "" and r[1] == [] and r[2] == 0})


@cell("in_turn_recall_state_read", perm_file="in-turn-recall-state.json")
def _c_state(ctx):
    # A regular state file carrying a stale degrade flag makes the call write the
    # cleared state back (eager flag-transition write); an unreadable one is the
    # empty state, so nothing is written.
    ctx.special(ctx.eh / "in-turn-recall-state.json",
                json.dumps({"lock_degraded": True, "x": 1}))
    return (_RECALL_PRELUDE + "r = mod.check_in_turn_recall('lfix-cmd words here', None)\n"
            "result = [r, WRITES]",
            {"fifo": lambda r: r == ["", []],
             "regular": lambda r: r == ["", [{"x": 1}]]})


@pytest.mark.parametrize("kind", ["fifo", "regular"])
@pytest.mark.parametrize("name", sorted(CELLS))
def test_cell(tmp_path, name, kind):
    ctx = Ctx(tmp_path, kind)
    code, expect = CELLS[name](ctx)
    result = _run(ctx, code)
    assert expect[kind](result), f"{name}/{kind}: unexpected result {result!r}"


# --------------------------------------------------------------------------
# A REGULAR file whose open fails with PermissionError stays fail-open, exactly
# as on origin/dev (every handler in this hook is `except Exception`). Only the
# non-regular target is new; no handler was widened or narrowed.
# --------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="chmod 000 does not deny root")
@pytest.mark.parametrize("name", sorted(PERM_CELLS))
def test_regular_file_permission_error_is_still_fail_open(tmp_path, name):
    ctx = Ctx(tmp_path, "regular")
    code, expect = CELLS[name](ctx)
    target = ctx.eh / PERM_CELLS[name]
    assert target.is_file()
    target.chmod(0)
    try:
        result = _run(ctx, code)
    finally:
        target.chmod(0o600)
    # the unreadable regular file behaves as the documented fallback (== the FIFO result)
    assert expect["fifo"](result), f"{name}: unexpected result {result!r}"


# --------------------------------------------------------------------------
# Whole-hook cells: the real main(), a FIFO at paths the hook touches. Each
# returns inside the guard with the same fail-open output as the regular case.
# --------------------------------------------------------------------------

_MAIN_PRELUDE = (
    "import io, contextlib, sys, types\n"
    "sys.modules['engram_idf'] = types.ModuleType('engram_idf')\n"
    "mod.load_tripwires = lambda *a, **k: [('lfix-pat', 'LFIX-NUDGE', 'lfix-a', 'normal')]\n"
    "PREV = [{'id': 'lfix-b', 'pattern': 'lfix-pat', 'prevention_type': %r, 'nudge': 'lfix-n'}]\n"
    "mod.load_prevention_lessons = lambda *a, **k: PREV\n"
    "sys.stdin = io.StringIO(json.dumps({'session_id': 's', 'tool_name': 'Bash',\n"
    "    'tool_input': {'command': 'run lfix-pat now'}}))\n"
    "out = io.StringIO()\n"
    "try:\n"
    "    with contextlib.redirect_stdout(out):\n"
    "        mod.main()\n"
    "    exit_code = 0\n"
    "except SystemExit as e:\n"
    "    exit_code = e.code or 0\n"
    "OUT = out.getvalue()\n"
)


def _whole(ctx, prevention_type, tail):
    code = _MAIN_PRELUDE % prevention_type + tail
    return _run(ctx, code, timeout=WHOLE_GUARD_S)


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_config_json(tmp_path, kind):
    """A FIFO at config.json (read by _ws4_config and _in_turn_config): the nudge
    block is still printed and the hook exits 0, as for a regular empty config."""
    ctx = Ctx(tmp_path, kind)
    ctx.special(ctx.eh / "config.json", "{}")
    r = _whole(ctx, "shadow", "result = [exit_code, 'LFIX-NUDGE' in OUT]")
    assert r == [0, True]


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_prevention_ledger(tmp_path, kind):
    """A FIFO at the prevention ledger (appended by the shadow path): output unchanged."""
    ctx = Ctx(tmp_path, kind)
    ctx.special(ctx.eh / "lesson-prevention-ledger.jsonl", "")
    r = _whole(ctx, "shadow",
               "p = P['eh'] + '/lesson-prevention-ledger.jsonl'\n"
               "result = [exit_code, 'LFIX-NUDGE' in OUT,\n"
               "          os.path.isfile(p) and len(open(p).read().splitlines())]")
    assert r == ([0, True, False] if kind == "fifo" else [0, True, 1])


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_blocking_lesson_reads_the_ledger(tmp_path, kind):
    """A blocking lesson reads the prevention ledger for its override count: a FIFO
    there counts as 0 overrides, so the call is still denied, as with an empty ledger."""
    ctx = Ctx(tmp_path, kind)
    ctx.special(ctx.eh / "lesson-prevention-ledger.jsonl", "")
    r = _whole(ctx, "blocking",
               "d = json.loads(OUT)['hookSpecificOutput']\n"
               "result = [exit_code, d['permissionDecision']]")
    assert r == [0, "deny"]


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_ws4_fire_ledger(tmp_path, kind):
    """WS4 active with a FIFO at its fire ledger: output unchanged, row written
    only when the ledger is a regular file."""
    ctx = Ctx(tmp_path, kind)
    (ctx.eh / "config.json").write_text(json.dumps({"experiments": {"ws4_chain_ab": {
        "enabled": True, "seed": "lfix-seed",
        "start": "2000-01-01T00:00:00Z", "end": "2999-01-01T00:00:00Z"}}}))
    ctx.special(ctx.eh / "tripwire-fire-ledger.jsonl", "")
    r = _whole(ctx, "shadow",
               "p = P['eh'] + '/tripwire-fire-ledger.jsonl'\n"
               "result = [exit_code, 'LFIX-NUDGE' in OUT,\n"
               "          os.path.isfile(p) and len(open(p).read().splitlines())]")
    assert r == ([0, True, False] if kind == "fifo" else [0, True, 1])


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_in_turn_state(tmp_path, kind):
    """A FIFO at the in-turn state file: output unchanged."""
    ctx = Ctx(tmp_path, kind)
    ctx.special(ctx.eh / "in-turn-recall-state.json", "{}")
    r = _whole(ctx, "shadow", "result = [exit_code, 'LFIX-NUDGE' in OUT]")
    assert r == [0, True]


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_in_turn_lock_file(tmp_path, kind):
    """A FIFO at the in-turn lock file (opened O_CREAT|O_RDWR, which returns at
    once on a FIFO): output unchanged."""
    ctx = Ctx(tmp_path, kind)
    if kind == "fifo":
        os.mkfifo(ctx.eh / "in-turn-recall-state.json.lock")
    r = _whole(ctx, "shadow", "result = [exit_code, 'LFIX-NUDGE' in OUT]")
    assert r == [0, True]


# --------------------------------------------------------------------------
# Source guard: no raw blocking open anywhere in the hook; no private copies
# --------------------------------------------------------------------------

_RAW = {"read_text", "write_text", "read_bytes", "write_bytes"}


def _raw_blocking_calls(path: Path):
    """Every raw `open()`, `.read_text()`-style call, and `os.open()` whose flags
    do not say O_NONBLOCK."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == "open":
            hits.append((n.lineno, "open()"))
        elif isinstance(f, ast.Attribute):
            if f.attr in _RAW:
                hits.append((n.lineno, f".{f.attr}()"))
            elif f.attr == "open" and isinstance(f.value, ast.Name) and f.value.id == "os":
                if "O_NONBLOCK" not in (ast.get_source_segment(src, n) or ""):
                    hits.append((n.lineno, "os.open() without O_NONBLOCK"))
    return hits


def test_no_raw_blocking_open_in_hook():
    hits = _raw_blocking_calls(HOOK_DIR / HOOK_FILE)
    assert hits == [], (
        f"{HOOK_FILE}: raw blocking opens remain (route them through the _hooklib "
        f"open_regular_text* helpers): {hits}")


def test_source_guard_flags_a_planted_raw_open(tmp_path):
    """The guard itself can fail: a planted raw open / read_text / os.open is found,
    and an os.open that says O_NONBLOCK is not."""
    p = tmp_path / "planted.py"
    p.write_text("import os\n"
                 "def f(path):\n"
                 "    open(path)\n"
                 "    path.read_text()\n"
                 "    os.open(path, os.O_CREAT | os.O_RDWR)\n"
                 "    os.open(path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK)\n")
    assert [h[1] for h in _raw_blocking_calls(p)] == [
        "open()", ".read_text()", "os.open() without O_NONBLOCK"]


def test_hook_uses_the_shared_helpers_not_private_copies():
    """K1: the helpers come from _hooklib; the hook defines none of its own."""
    tree = ast.parse((HOOK_DIR / HOOK_FILE).read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not {n for n in defined if n.startswith(("open_regular", "read_regular", "_open_regular"))}
    imported = {a.name for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module == "_hooklib" for a in n.names}
    assert {"open_regular_text_write", "open_regular_text_append",
            "read_regular_text", "open_regular_text"} <= imported
