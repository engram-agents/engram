"""#3628 slice 2c: the UserPromptSubmit surface hook must never block on a
non-regular file (FIFO) at any of its FILE open sites.

The surface hook runs on every prompt under a harness timeout. A plain
`open()` of a path where a FIFO sits waits for a writer (read) or a reader
(write), so the hook hangs and is killed silently. Slice 2a/2b/#3591 fixed the
sibling hooks; this file covers `engram-surface-hook.py`'s file opens. Its
sqlite connects are slice 2d and are deliberately NOT exercised here.

Each cell drives the REAL function in a SUBPROCESS with a hard timeout, so a
regression fails (timeout) instead of hanging the suite. Every cell runs twice:

  * kind="fifo"    -- a FIFO at the path; must return promptly with the
                      documented fallback (file treated as absent);
  * kind="regular" -- a regular file at the path; positive control that the
                      real behaviour is unchanged (green before and after).

Write sites that are only reachable after a successful READ of the same path
(the marker clear-writes) cannot be hit by planting a FIFO up front, so their
cells swap the regular file for a FIFO between the read and the write (a TOCTOU
swap) by wrapping the hook's own `open_regular_text` name. A static guard
additionally pins that no builtin `open(` is left in the hook.
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
# SURFACE_3628S2C_HOOK_DIR lets the regular-file propagation cells be pointed at
# another tree (e.g. an extracted origin/dev hooks dir) to prove they pass there.
HOOK_DIR = Path(os.environ.get("SURFACE_3628S2C_HOOK_DIR")
                or PROJECT_ROOT / "src" / "engram" / "hooks" / "claude")
HOOK_NAME = "engram-surface-hook.py"

GUARD_S = int(os.environ.get("SURFACE_3628S2C_GUARD_S", "10"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")

_CHILD = r"""
import importlib.util, json, os, sys
spec_in = json.loads(sys.argv[1])
sys.path.insert(0, os.path.dirname(spec_in["hook"]))
spec = importlib.util.spec_from_file_location("hook_3628s2c_child", spec_in["hook"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = spec_in["paths"]
ns = {"mod": mod, "P": P, "os": os, "json": json, "result": None}
exec(spec_in["code"], ns)
print("RESULT:" + json.dumps(ns["result"]))
"""

_PRE = """
import stat, io
def kind_of(p):
    try:
        m = os.stat(p).st_mode
    except OSError:
        return None
    return 'fifo' if stat.S_ISFIFO(m) else ('reg' if stat.S_ISREG(m) else 'other')

def swap_after_first_read(path):
    # TOCTOU: the hook reads a regular file at `path`; before its follow-up
    # write the path becomes a FIFO. Only effective when the hook reads via its
    # own `open_regular_text` name (at dev it does not, so nothing is swapped).
    orig = getattr(mod, 'open_regular_text', None)
    if orig is None:
        return
    done = []
    def wrapped(p, encoding=None):
        f = orig(p, encoding=encoding)
        if str(p) == str(path) and not done:
            done.append(1)
            data = f.read()
            f.close()
            os.unlink(p)
            os.mkfifo(p)
            return io.StringIO(data)
        return f
    mod.open_regular_text = wrapped

def deny_open(path, write=False, nth=1):
    # Force the hook's own open of `path` (the nth time) to raise a
    # PermissionError, on a REGULAR file. At HEAD the hook opens through
    # `open_regular_text[_write]`; the origin/dev hook uses the builtin open(),
    # so there the builtin is wrapped. Same observable fault either way.
    import builtins
    seen = []
    def gate(p):
        if str(p) == str(path):
            seen.append(1)
            if len(seen) == nth:
                raise PermissionError(13, 'denied', str(p))
    name = 'open_regular_text_write' if write else 'open_regular_text'
    orig = getattr(mod, name, None)
    if orig is not None:
        def wrapped(p, *a, **k):
            gate(p)
            return orig(p, *a, **k)
        setattr(mod, name, wrapped)
    else:
        orig_open = builtins.open
        def wrapped_open(p, mode='r', *a, **k):
            if ('w' in mode) == write:
                gate(p)
            return orig_open(p, mode, *a, **k)
        builtins.open = wrapped_open

def deny_state_read(path, nth=1, errno_=13):
    # #3521 row 40: the principle-trigger STATE reads (the snapshot and the
    # write-back re-read) now happen inside _hooklib.read_state_json_classified,
    # which calls _hooklib.read_regular_text -- NOT the hook module's own
    # open_regular_text, so deny_open cannot reach them. Same fault (a
    # PermissionError at the nth open of `path`, on a REGULAR file), injected
    # at the seam where the read now lives.
    import sys
    hl = sys.modules.get('_hooklib') or __import__('_hooklib')
    seen = []
    orig = hl.read_regular_text
    def wrapped(p, *a, **k):
        if str(p) == str(path):
            seen.append(1)
            if len(seen) == nth:
                if errno_ == 13:
                    raise PermissionError(13, 'denied', str(p))
                raise OSError(errno_, 'injected', str(p))
        return orig(p, *a, **k)
    hl.read_regular_text = wrapped
"""


class Ctx:
    def __init__(self, tmp: Path, kind: str):
        self.kind = kind
        self.tmp = tmp
        self.home = tmp / "home"
        self.eh = self.home / ".engram"
        self.sessions = self.eh / "sessions"
        for d in (self.eh, self.sessions):
            d.mkdir(parents=True, exist_ok=True)
        self.hook = str(HOOK_DIR / HOOK_NAME)

    def special(self, path: Path, regular_text: str):
        """A FIFO (kind=fifo) or a regular file with `regular_text` at path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_text(regular_text)

    def paths(self):
        return {"home": str(self.home), "eh": str(self.eh),
                "sessions": str(self.sessions), "tmp": str(self.tmp)}

    def env(self):
        env = dict(os.environ)
        env.update({"HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
                    "ENGRAM_SESSION_ROLE": "interactive"})
        env.pop("ENGRAM_RUNTIME_DIR", None)
        return env


def _run(ctx: Ctx, code: str, timeout=GUARD_S):
    payload = json.dumps({"hook": ctx.hook, "paths": ctx.paths(), "code": _PRE + code})
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD, payload],
                              capture_output=True, text=True, timeout=timeout, env=ctx.env())
    except subprocess.TimeoutExpired:
        pytest.fail(f"blocked for more than {timeout}s on a non-regular file (kind={ctx.kind})")
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


CELLS = {}


def cell(name):
    def deco(fn):
        CELLS[name] = fn
        return fn
    return deco


# --- config.json readers -----------------------------------------------------

@cell("get_user_name_config")  # :398
def _c_user(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"primary_user": "Zed"}))
    return ("result = mod.get_user_name()",
            {"fifo": lambda r: r == "the human", "regular": lambda r: r == "Zed"})


@cell("auto_surface_config")  # :464
def _c_auto(ctx):
    ctx.special(ctx.eh / "config.json",
                json.dumps({"auto_surface": {"short_prompt_threshold_chars": 7}}))
    return ("result = mod._get_auto_surface_config()['short_prompt_threshold_chars']",
            {"fifo": lambda r: r == 100, "regular": lambda r: r == 7})


@cell("suppression_config")  # :536
def _c_supp(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"recall_suppression": {"k": 9}}))
    return ("result = mod._get_suppression_config()['k']",
            {"fifo": lambda r: r == 5, "regular": lambda r: r == 9})


@cell("echo_guard_config")  # :568
def _c_echo(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"same_session_echo": {"minutes": 3}}))
    return ("result = mod._get_echo_guard_config()['minutes']",
            {"fifo": lambda r: r == 10, "regular": lambda r: r == 3})


@cell("person_moment_cooldown_config")  # :961
def _c_pm(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"person_moment_cooldown": {"seconds": 123}}))
    return ("result = [mod._get_person_moment_cooldown_config()['seconds'], "
            "float(mod._PERSON_MOMENT_COOLDOWN_SECONDS)]",
            {"fifo": lambda r: r[0] == r[1], "regular": lambda r: r[0] == 123.0})


@cell("budget_config")  # :1259
def _c_budget(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"recall_budget": {"total_chars": 4321}}))
    return ("result = mod._get_budget_config()['total_chars']",
            {"fifo": lambda r: r == mod_default_budget(), "regular": lambda r: r == 4321})


def mod_default_budget():
    # DEFAULT_RECALL_BUDGET_TOTAL_CHARS, read from source so the cell does not
    # hard-code a tunable.
    import re
    m = re.search(r"^DEFAULT_RECALL_BUDGET_TOTAL_CHARS\s*=\s*(\d+)",
                  (HOOK_DIR / HOOK_NAME).read_text(encoding="utf-8"), re.M)
    return int(m.group(1))


@cell("attached_packs_config")  # :1590
def _c_packs(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps(
        {"attached_packs": [{"id": "p", "path": "/x", "enabled": True}]}))
    return ("result = mod._get_attached_packs()",
            {"fifo": lambda r: r == [], "regular": lambda r: r == [{"id": "p", "path": "/x"}]})


# --- marker / ledger / transcript reads --------------------------------------

@cell("mcp_marker")  # :228
def _c_mcp(ctx):
    ctx.special(ctx.eh / "mcp-tools-ready.json", json.dumps({"pid": 0}))
    return ("result = list(mod._check_mcp_write_tool_marker(P['eh']))",
            # unreadable marker -> UNKNOWN (None), never a hang
            {"fifo": lambda r: r[0] is None,
             "regular": lambda r: r[0] is False and "no valid pid" in r[1]})


@cell("session_started_at_marker")  # :589
def _c_started(ctx):
    ctx.special(ctx.sessions / "sess-1.json", json.dumps({"started_at": "2026-10-01T00:00:00Z"}))
    return ("result = mod._get_session_started_at('sess-1') is not None",
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


@cell("surface_ledger_read")  # :645
def _c_ledger_r(ctx):
    ctx.special(ctx.eh / "surface-ledger.json", json.dumps({"a": 1}))
    return ("result = mod._read_surface_ledger()",
            {"fifo": lambda r: r == {}, "regular": lambda r: r == {"a": 1}})


@cell("prev_assistant_tail_transcript")  # :1731
def _c_tail(ctx):
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": "hello there"}]}})
    ctx.special(ctx.tmp / "transcript.jsonl", line + "\n")
    return ("result = mod._read_prev_assistant_tail(P['tmp'] + '/transcript.jsonl', 50)",
            {"fifo": lambda r: r == "", "regular": lambda r: r == "hello there"})


@cell("prompt_counter_read")  # :2770
def _c_counter_r(ctx):
    ctx.special(ctx.eh / "prompt-counter.json", json.dumps({"prompts_since_compaction": 7}))
    return ("result = mod.read_prompt_counter()['prompts_since_compaction']",
            {"fifo": lambda r: r == 0, "regular": lambda r: r == 7})


@cell("write_reminder_read")  # :2789
def _c_wr_r(ctx):
    ctx.special(ctx.eh / "write-reminder.json", json.dumps({"pending": True}))
    return ("result = mod.check_write_reminder()[:20]",
            {"fifo": lambda r: r == "", "regular": lambda r: r.startswith("[ENGRAM Write Check")})


@cell("feeling_nudge_read")  # :2823
def _c_feel(ctx):
    ctx.special(ctx.eh / "feeling-nudge-active.json", json.dumps({"source": "post_compact"}))
    return ("result = mod.check_feeling_nudge_marker()",
            {"fifo": lambda r: r == "", "regular": lambda r: r.startswith("[ENGRAM Feeling Nudge")})


@cell("repair_marker_read")  # :2851
def _c_repair_r(ctx):
    ctx.special(ctx.eh / "toolcall-repair-pending.json", json.dumps(
        {"pending": True, "tool_name": "Xtool", "repairs": ["r1"]}))
    return ("result = mod.check_repair_marker()",
            {"fifo": lambda r: r == "", "regular": lambda r: "Tool-Call Repair" in r and "Xtool" in r})


@cell("error_patterns_read")  # :2908
def _c_errpat(ctx):
    ctx.special(ctx.eh / "error_patterns.json", json.dumps(
        [{"situation_keywords": ["foo"], "trigger_description": "d", "scaffolding_nudge": "n"}]))
    return ("result = mod.check_error_patterns('a foo b')",
            {"fifo": lambda r: r == "", "regular": lambda r: "ENGRAM Tripwire" in r})


_PID = "pfix-a"  # an arbitrary principle id (not an ENGRAM node-id shape)
_REG = {"t1": [{"principle_id": _PID, "kind": "lesson", "claim": "c", "nudge": "n"}]}


@cell("principle_triggers_registry_read")  # :3092
def _c_reg(ctx):
    ctx.special(ctx.eh / "principle_triggers.json", json.dumps(_REG))
    return ("t, r = mod._check_principle_triggers_inner(['t1'], 5)\nresult = [t, len(r)]",
            {"fifo": lambda r: r == ["", 0], "regular": lambda r: _PID in r[0] and r[1] == 1})


@cell("principle_trigger_state_read")  # :3140 (+ :3277 write-back re-read)
def _c_state(ctx):
    (ctx.eh / "principle_triggers.json").write_text(json.dumps(_REG))
    ctx.special(ctx.eh / "principle-trigger-state.json", json.dumps(
        {_PID: {"last_fired_prompt": 4, "strength": 1.0, "enactments": 0, "fires": 1}}))
    return ("t, r = mod._check_principle_triggers_inner(['t1'], 5)\nresult = t",
            # regular state: fired at prompt 4 -> in cooldown -> silent.
            # FIFO state: treated as absent -> fires.
            {"fifo": lambda r: _PID in r, "regular": lambda r: r == ""})


# --- write sites --------------------------------------------------------------
# The atomic_write_json branch (mkstemp in the same dir + os.replace) cannot
# block on a FIFO at the destination: it replaces the FIFO. The cells below are
# for the branch used when that optional import is unavailable (_HOOKLIB_OK
# False), which opens the destination (or a predictable .tmp) directly.

@cell("surface_ledger_write_fallback")  # :660
def _c_ledger_w(ctx):
    ctx.special(ctx.eh / "surface-ledger.json", "")
    return ("mod._HOOKLIB_OK = False\n"
            "mod._write_surface_ledger({'x': 1})\n"
            "p = P['eh'] + '/surface-ledger.json'\n"
            "result = [kind_of(p), json.load(open(p)) if kind_of(p) == 'reg' else None]",
            {"fifo": lambda r: r[0] == "fifo", "regular": lambda r: r == ["reg", {"x": 1}]})


@cell("prompt_counter_write_fallback")  # :2782
def _c_counter_w(ctx):
    ctx.special(ctx.eh / "prompt-counter.json", "")
    return ("mod._HOOKLIB_OK = False\n"
            "mod.write_prompt_counter({'prompts_since_compaction': 3})\n"
            "p = P['eh'] + '/prompt-counter.json'\n"
            "result = [kind_of(p), json.load(open(p)) if kind_of(p) == 'reg' else None]",
            # non-regular target: skipped silently (no handler existed; main()
            # calls this unguarded). Regular: written as before.
            {"fifo": lambda r: r[0] == "fifo",
             "regular": lambda r: r == ["reg", {"prompts_since_compaction": 3}]})


@cell("prompt_counter_write_atomic_control")  # atomic branch, control
def _c_counter_a(ctx):
    ctx.special(ctx.eh / "prompt-counter.json", "")
    return ("mod.write_prompt_counter({'prompts_since_compaction': 3})\n"
            "p = P['eh'] + '/prompt-counter.json'\n"
            "result = [kind_of(p), json.load(open(p))]",
            {"fifo": lambda r: r == ["reg", {"prompts_since_compaction": 3}],
             "regular": lambda r: r == ["reg", {"prompts_since_compaction": 3}]})


def _swap_line(ctx, name):
    return (f"swap_after_first_read(P['eh'] + '/{name}')\n") if ctx.kind == "fifo" else ""


@cell("write_reminder_clear_write")  # :2797 (post-read write, TOCTOU swap)
def _c_wr_w(ctx):
    (ctx.eh / "write-reminder.json").write_text(json.dumps({"pending": True}))
    return ("mod._HOOKLIB_OK = False\n" + _swap_line(ctx, "write-reminder.json") +
            "ret = mod.check_write_reminder()\n"
            "p = P['eh'] + '/write-reminder.json'\n"
            "result = [ret[:20], kind_of(p), json.load(open(p)) if kind_of(p) == 'reg' else None]",
            # fifo: swapped after the read -> write refused -> nothing returned, FIFO kept.
            {"fifo": lambda r: r[0] == "" and r[1] == "fifo",
             "regular": lambda r: r[0].startswith("[ENGRAM Write Check") and r[1] == "reg"
             and r[2] == {"pending": False}})


@cell("repair_marker_clear_write")  # :2860 (post-read write, TOCTOU swap)
def _c_repair_w(ctx):
    (ctx.eh / "toolcall-repair-pending.json").write_text(json.dumps(
        {"pending": True, "tool_name": "Xtool", "repairs": ["r1"]}))
    return ("mod._HOOKLIB_OK = False\n" + _swap_line(ctx, "toolcall-repair-pending.json") +
            "ret = mod.check_repair_marker()\n"
            "p = P['eh'] + '/toolcall-repair-pending.json'\n"
            "result = [ret[:25], kind_of(p), json.load(open(p))['pending'] if kind_of(p) == 'reg' else None]",
            {"fifo": lambda r: r[0] == "" and r[1] == "fifo",
             "regular": lambda r: r[0].startswith("[ENGRAM Tool-Call Repa") and r[1] == "reg"
             and r[2] is False})


_RAISE_WRITE = (
    "def _boom(*a, **k):\n"
    "    raise PermissionError(13, 'read-only dir')\n"
    "mod._HOOKLIB_OK = True\n"
    "mod.atomic_write_json = _boom\n"
    "try:\n"
    "    ret = mod.{fn}()\n"
    "    result = 'returned:' + repr(ret[:12])\n"
    "except OSError as e:\n"
    "    result = 'raised:' + type(e).__name__\n")


@cell("write_reminder_clear_write_error_propagates")  # OSError of the clear-write is NOT swallowed
def _c_wr_err(ctx):
    ctx.special(ctx.eh / "write-reminder.json", json.dumps({"pending": True}))
    return (_RAISE_WRITE.format(fn="check_write_reminder"),
            # fifo: the READ is refused, the write is never reached -> "".
            # regular: the read succeeds, the clear-write's error propagates (dev parity).
            {"fifo": lambda r: r == "returned:''",
             "regular": lambda r: r == "raised:PermissionError"})


@cell("repair_marker_clear_write_error_propagates")  # OSError of the clear-write is NOT swallowed
def _c_repair_err(ctx):
    ctx.special(ctx.eh / "toolcall-repair-pending.json", json.dumps(
        {"pending": True, "tool_name": "Xtool", "repairs": ["r1"]}))
    return (_RAISE_WRITE.format(fn="check_repair_marker"),
            {"fifo": lambda r: r == "returned:''",
             "regular": lambda r: r == "raised:PermissionError"})


@cell("principle_state_tmp_write_fallback")  # :3318 (predictable <state>.tmp)
def _c_tmp(ctx):
    (ctx.eh / "principle_triggers.json").write_text(json.dumps(_REG))
    if ctx.kind == "fifo":
        os.mkfifo(ctx.eh / "principle-trigger-state.json.tmp")
    return ("mod._HOOKLIB_OK = False\n"
            "t, r = mod._check_principle_triggers_inner(['t1'], 5)\n"
            "sp = P['eh'] + '/principle-trigger-state.json'\n"
            "result = [t, kind_of(sp + '.tmp'), os.path.isfile(sp)]",
            # FIFO planted at the temp NAME: write refused, FIFO untouched, no state written.
            {"fifo": lambda r: _PID in r[0] and r[1] == "fifo" and r[2] is False,
             "regular": lambda r: _PID in r[0] and r[1] is None and r[2] is True})


@cell("state_lock_measurement")  # :3051 -- O_RDWR on a FIFO does not block (measured)
def _c_lock(ctx):
    ctx.special(ctx.eh / "x.lock", "")
    return ("try:\n"
            "    out = mod._with_state_lock(P['eh'] + '/x.lock', lambda: 'ran')\n"
            "except OSError as e:\n"
            "    out = 'oserror'\n"
            "result = out",
            # The open returns at once on a FIFO (measured). On macOS the flock
            # that follows is unsupported on a FIFO (ENOTSUP) and propagates;
            # either way the call RETURNS inside the guard, which is the contract.
            {"fifo": lambda r: r in ("ran", "oserror"), "regular": lambda r: r == "ran"})


# --- regular-file OSError is NOT swallowed (dev parity) -------------------------
# The read handlers below caught only (FileNotFoundError, JSONDecodeError) on
# origin/dev, so a PermissionError on a REGULAR file propagated. #3628 may only
# add tolerance for a NON-regular target. Each cell forces the read of a
# regular, valid file to raise PermissionError and expects it to propagate; the
# same assertions are run against the origin/dev hook to prove they hold there.

_RAISES = ("try:\n"
           "    ret = {call}\n"
           "    result = 'returned:' + repr(ret)[:30]\n"
           "except OSError as e:\n"
           "    result = 'raised:' + type(e).__name__\n")

_STALE_STATE = json.dumps(
    {_PID: {"last_fired_prompt": 0, "strength": 1.0, "enactments": 0, "fires": 1}})

# name -> (relative path, file text, extra (path, text) or None, call, nth open denied)
_PROPAGATE = {
    "read_prompt_counter": ("prompt-counter.json",
                            json.dumps({"prompts_since_compaction": 7}), None,
                            "mod.read_prompt_counter()", 1),
    "check_write_reminder": ("write-reminder.json", json.dumps({"pending": True}), None,
                             "mod.check_write_reminder()", 1),
    "check_feeling_nudge_marker": ("feeling-nudge-active.json",
                                   json.dumps({"source": "post_compact"}), None,
                                   "mod.check_feeling_nudge_marker()", 1),
    "check_repair_marker": ("toolcall-repair-pending.json",
                            json.dumps({"pending": True, "tool_name": "X", "repairs": []}),
                            None, "mod.check_repair_marker()", 1),
    "check_error_patterns": ("error_patterns.json", json.dumps(
        [{"situation_keywords": ["foo"], "trigger_description": "d", "scaffolding_nudge": "n"}]),
        None, "mod.check_error_patterns('a foo b')", 1),
    "principle_triggers_registry": ("principle_triggers.json", json.dumps(_REG), None,
                                    "mod._check_principle_triggers_inner(['t1'], 100)", 1),
    # The two principle-trigger STATE entries that were here (the snapshot read
    # and the write-back re-read) are retired: #3628 pinned "OSError on a regular
    # file propagates" only to keep dev behaviour unchanged, and the #3521 row 40
    # ruling makes an unreadable regular STATE file UNKNOWN (never raised). The
    # state reads moved into _hooklib.read_state_json_classified; the re-pinned
    # cells are test_state_read_denied_* below. The registry entry stays.
}


@pytest.mark.parametrize("name", sorted(_PROPAGATE))
def test_regular_file_oserror_propagates(tmp_path, name):
    rel, text, extra, call, nth = _PROPAGATE[name]
    ctx = Ctx(tmp_path, "regular")
    (ctx.eh / rel).write_text(text)
    if extra:
        (ctx.eh / extra[0]).write_text(extra[1])
    code = (f"deny_open(P['eh'] + '/{rel}', nth={nth})\n" + _RAISES.format(call=call))
    assert _run(ctx, code) == "raised:PermissionError", name


_STATE_REL = "principle-trigger-state.json"

_STATE_CALL = (
    "su = []\n"
    "deny_state_read(P['eh'] + '/{rel}', nth={nth}, errno_={errno_})\n"
    "try:\n"
    "    ret = mod._check_principle_triggers_inner(['t1'], 100, None, su)\n"
    "    raised = None\n"
    "except Exception as e:\n"
    "    ret = None\n"
    "    raised = type(e).__name__\n"
    "side = P['eh'] + '/{rel}.corrupt'\n"
    "live = P['eh'] + '/{rel}'\n"
    "result = {{\n"
    "    'raised': raised,\n"
    "    'fired': bool(ret and ret[0]),\n"
    "    'su': su,\n"
    "    'side': open(side).read() if os.path.exists(side) else None,\n"
    "    'live': json.load(open(live)) if os.path.exists(live) else None,\n"
    "    'live_raw': open(live).read() if os.path.exists(live) else None,\n"
    "}}\n"
)


def _state_ctx(tmp_path):
    ctx = Ctx(tmp_path, "regular")
    (ctx.eh / _STATE_REL).write_text(_STALE_STATE)
    (ctx.eh / "principle_triggers.json").write_text(json.dumps(_REG))
    return ctx


def test_state_read_denied_wrapper_fires_positive_control(tmp_path):
    # A deny_state_read that never fires would let the two cells below pass
    # vacuously; prove it raises exactly on the nth read of the path.
    ctx = _state_ctx(tmp_path)
    code = (
        "import sys\n"
        "hl = sys.modules['_hooklib']\n"
        "p = P['eh'] + '/principle-trigger-state.json'\n"
        "deny_state_read(p, nth=1)\n"
        "first = hl.read_state_json_classified(p)\n"
        "result = {'first_reason': first[1], 'first_movable': first[2], 'first_state': first[0]}\n"
    )
    out = _run(ctx, code)
    assert out["first_reason"].startswith("PermissionError: "), out
    assert out["first_movable"] is False and out["first_state"] == {}


def test_state_read_denied_nth_is_exact(tmp_path):
    ctx = _state_ctx(tmp_path)
    code = (
        "import sys\n"
        "hl = sys.modules['_hooklib']\n"
        "p = P['eh'] + '/principle-trigger-state.json'\n"
        "deny_state_read(p, nth=2)\n"
        "a = hl.read_state_json_classified(p)\n"
        "b = hl.read_state_json_classified(p)\n"
        "c = hl.read_state_json_classified(p)\n"
        "result = {'a': a[1], 'b': b[1], 'c': c[1], 'a_has': bool(a[0])}\n"
    )
    out = _run(ctx, code)
    assert out["a"] is None and out["a_has"] is True
    assert out["b"].startswith("PermissionError: ")
    assert out["c"] is None


def test_state_snapshot_read_denied_is_unknown_not_raised(tmp_path):
    # nth=1: the snapshot read is denied; the in-lock fresh read succeeds, so
    # the file is merged into -- NOT moved aside.
    ctx = _state_ctx(tmp_path)
    out = _run(ctx, _STATE_CALL.format(rel=_STATE_REL, nth=1, errno_=13))
    assert out["raised"] is None, out
    assert out["fired"] is True
    assert len(out["su"]) == 1 and out["su"][0]["reason"].startswith("PermissionError: ")
    assert out["su"][0]["moved_to"] is None
    assert out["side"] is None
    assert out["live"][_PID]["last_fired_prompt"] == 100
    assert out["live"][_PID]["fires"] == 2  # merged onto the stale entry (fires 1)


def test_state_writeback_reread_denied_is_unknown_not_moved_not_written(tmp_path):
    # nth=2: the snapshot read works, the write-back re-read is denied
    # (PermissionError). An unreadable file has not been shown unparseable, so
    # it is UNKNOWN but never movable: no sidecar, no write, bytes unchanged.
    ctx = _state_ctx(tmp_path)
    out = _run(ctx, _STATE_CALL.format(rel=_STATE_REL, nth=2, errno_=13))
    assert out["raised"] is None, out
    assert out["fired"] is True  # firing decision unchanged
    assert out["side"] is None
    assert out["live_raw"] == _STALE_STATE
    # The snapshot was fine, so nothing was reported for this call.
    assert out["su"] == []


def test_state_valid_file_with_injected_emfile_is_not_moved(tmp_path):
    # Reviewer's case (#3654 round 1): an injected EMFILE on a VALID dict file
    # must not rename it to .corrupt and reset every principle's history.
    import errno
    ctx = _state_ctx(tmp_path)
    # nth=1: the snapshot read fails; the in-lock re-read is fine, so the file
    # is merged into (history kept: fires 1 -> 2), never moved.
    out = _run(ctx, _STATE_CALL.format(rel=_STATE_REL, nth=1, errno_=errno.EMFILE))
    assert out["raised"] is None and out["side"] is None, out
    assert out["live"][_PID]["fires"] == 2, out
    # nth=2: the write-back re-read fails; neither moved nor written.
    ctx = _state_ctx(tmp_path / "second")
    out = _run(ctx, _STATE_CALL.format(rel=_STATE_REL, nth=2, errno_=errno.EMFILE))
    assert out["raised"] is None and out["side"] is None, out
    assert out["live_raw"] == _STALE_STATE, out
    # And the classifier itself: movable only for a READ-and-unparseable file.
    code = (
        "import sys, errno\n"
        "hl = sys.modules['_hooklib']\n"
        "p = P['eh'] + '/principle-trigger-state.json'\n"
        f"deny_state_read(p, nth=1, errno_={errno.EMFILE})\n"
        "r = hl.read_state_json_classified(p)\n"
        "result = {'reason': r[1], 'movable': r[2], 'state': r[0]}\n"
    )
    out = _run(ctx, code)
    assert out["reason"].startswith("OSError: ") and out["movable"] is False
    assert out["state"] == {}


def test_write_prompt_counter_regular_oserror_propagates(tmp_path):
    ctx = Ctx(tmp_path, "regular")
    (ctx.eh / "prompt-counter.json").write_text("{}")
    code = ("mod._HOOKLIB_OK = False\n"
            "deny_open(P['eh'] + '/prompt-counter.json', write=True)\n" +
            _RAISES.format(call="mod.write_prompt_counter({'prompts_since_compaction': 1})"))
    assert _run(ctx, code) == "raised:PermissionError"


@pytest.mark.parametrize("kind", ["fifo", "regular"])
@pytest.mark.parametrize("name", sorted(CELLS))
def test_cell(tmp_path, name, kind):
    ctx = Ctx(tmp_path, kind)
    code, expect = CELLS[name](ctx)
    result = _run(ctx, code)
    assert expect[kind](result), f"{name}/{kind}: unexpected result {result!r}"


# --- static guard: no builtin open() left in the hook -------------------------

_PATH_IO_ATTRS = {"read_text", "read_bytes", "write_text", "write_bytes"}


def _open_like_calls(source: str):
    """Line numbers of (builtin open, os.open, Path-style IO, io.open) calls."""
    tree = ast.parse(source)
    builtin_open, os_open, path_io, io_open = [], [], [], []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == "open":
            builtin_open.append(n.lineno)
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else None
            if f.attr == "open" and base == "os":
                os_open.append(n.lineno)
            if f.attr == "open" and base == "io":
                io_open.append(n.lineno)
            if f.attr in _PATH_IO_ATTRS:
                path_io.append(n.lineno)
    return builtin_open, os_open, path_io, io_open


def test_no_builtin_open_calls_remain():
    builtin_open, os_open, path_io, io_open = _open_like_calls(
        (HOOK_DIR / HOOK_NAME).read_text(encoding="utf-8"))
    assert builtin_open == [], f"builtin open() at lines {builtin_open}"
    assert len(os_open) <= 1, f"unexpected os.open() sites at lines {os_open}"  # the state lock
    # Path.read_text/.read_bytes/.write_text/.write_bytes and io.open block on a
    # FIFO exactly like open(); none may appear (use the _hooklib helpers).
    assert path_io == [], f"Path read/write_text/bytes at lines {path_io}"
    assert io_open == [], f"io.open() at lines {io_open}"


@pytest.mark.parametrize("snippet,which", [
    ("from pathlib import Path\nPath(x).read_text()\n", "path_io"),
    ("p.read_bytes()\n", "path_io"),
    ("p.write_text('x')\n", "path_io"),
    ("p.write_bytes(b'x')\n", "path_io"),
    ("import io\nio.open(x)\n", "io_open"),
    ("open(x)\n", "builtin_open"),
])
def test_open_guard_detects_planted_calls(snippet, which):
    """Control: the guard's scanner flags each planted form, so it cannot be
    vacuously green."""
    b, _o, p, i = _open_like_calls(snippet)
    hits = {"builtin_open": b, "path_io": p, "io_open": i}[which]
    assert hits, f"scanner missed a planted {which} call"


# --- end to end: the real main() with a FIFO at one hook-only path at a time ---

_MAIN_PATHS = [
    "config.json", "prompt-counter.json", "write-reminder.json",
    "toolcall-repair-pending.json", "feeling-nudge-active.json", "error_patterns.json",
    "principle_triggers.json", "principle-trigger-state.json", "surface-ledger.json",
    "daemon-launch-attempt", "daemon-idle-shutdown", "sessions/sess-main.json",
]


def _run_main(ctx: Ctx, stdin: dict, timeout=45):
    try:
        return subprocess.run([sys.executable, str(HOOK_DIR / HOOK_NAME)],
                              input=json.dumps(stdin), capture_output=True, text=True,
                              timeout=timeout, env=ctx.env())
    except subprocess.TimeoutExpired:
        pytest.fail(f"surface hook main() blocked for more than {timeout}s (kind={ctx.kind})")


def _stdin(ctx):
    return {"prompt": "what did we decide about the recall budget last week?",
            "session_id": "sess-main", "transcript_path": str(ctx.tmp / "t.jsonl"),
            "cwd": str(ctx.tmp)}


# A FIFO as the transcript: the surface hook's own read
# (`_read_prev_assistant_tail`) is covered by the per-function cell above; the
# whole-main() cell also exercises `context_tracker.read_current_tokens` (called
# via estimate_usage), which #3636 made non-blocking.
@pytest.mark.parametrize("rel", _MAIN_PATHS + ["TRANSCRIPT"])
def test_main_with_fifo_at_one_path(tmp_path, rel):
    ctx = Ctx(tmp_path, "fifo")
    target = ctx.tmp / "t.jsonl" if rel == "TRANSCRIPT" else ctx.eh / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(target)
    done = _run_main(ctx, _stdin(ctx), timeout=15 if rel == "TRANSCRIPT" else 45)
    assert done.returncode == 0, done.stderr
    assert "additionalContext" in json.loads(done.stdout)["hookSpecificOutput"]


def test_main_with_fifo_at_every_hook_path(tmp_path):
    ctx = Ctx(tmp_path, "fifo")
    for rel in _MAIN_PATHS:
        p = ctx.eh / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(p)
    done = _run_main(ctx, _stdin(ctx))
    assert done.returncode == 0, done.stderr
    assert "additionalContext" in json.loads(done.stdout)["hookSpecificOutput"]


def test_main_regular_control(tmp_path):
    ctx = Ctx(tmp_path, "regular")
    for rel in _MAIN_PATHS:
        p = ctx.eh / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
    (ctx.tmp / "t.jsonl").write_text("")
    done = _run_main(ctx, _stdin(ctx))
    assert done.returncode == 0, done.stderr
    assert "additionalContext" in json.loads(done.stdout)["hookSpecificOutput"]
