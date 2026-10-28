"""#3628 slice 2a: the shared hook libs and the small hooks must never block on
a non-regular file (FIFO) and must stay regular-file-only.

A plain `open()` / `Path.read_text()` of a path where a FIFO sits waits for a
writer; a plain write waits for a reader. A hook under a harness timeout then
hangs and is killed silently, and a `try/except` is fail-open for an exception,
not for a hang. #3591 fixed SessionStart; this file covers the shared libs
(`_harness`, `_hooklib`, `_prompthooklib`, `_stale_tree_hooklib`,
`_stophooklib`, `context_tracker`) and the small hooks (baton / inter-agent /
forum prompt hooks, deference-detector prompt + stop, end-of-day, loop-arm
stop, stop, time-bar, user-identity, utility-credit-mention stop).

Each cell drives the REAL function in a SUBPROCESS with a hard timeout, so a
regression fails (timeout) instead of hanging the suite. Every cell runs twice:

  * kind="fifo"    -- a FIFO at the path; must return promptly with the
                      documented fallback (file treated as absent / write
                      treated as failed);
  * kind="regular" -- a regular file at the path; positive control that the
                      real behaviour is unchanged (green before and after).

Three further groups:

  * WHOLE-HOOK cells run the real script with a FIFO planted at the paths that
    hook (and only that hook, no other module it calls) reads or writes;
  * the SOURCE GUARD walks every in-scope file's AST and fails on any raw
    `open()` / `os.open()` / `Path.read_text|write_text|read_bytes|write_bytes`
    call outside the shared helpers, so a site no cell can drive (a branch
    behind a network failure) still cannot regress to a blocking open;
  * the append helper's own contract is pinned in test_regular_io_parity_3628.py.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"

GUARD_S = int(os.environ.get("HOOKS_3628S2A_GUARD_S", "8"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")

# Runs inside the child: load the module by path, exec the cell's code, print JSON.
_CHILD = r"""
import importlib.util, json, os, sys
spec_in = json.loads(sys.argv[1])
sys.path.insert(0, os.path.dirname(spec_in["hook"]))
spec = importlib.util.spec_from_file_location("hook_3628s2a_child", spec_in["hook"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = spec_in["paths"]
ns = {"mod": mod, "P": P, "os": os, "json": json, "result": None}
exec(spec_in["code"], ns)
print("RESULT:" + json.dumps(ns["result"]))
"""

_ENV_DROP = ("ENGRAM_RUNTIME_DIR", "CLAUDE_PLUGIN_ROOT", "CODEX_PLUGIN_ROOT", "PYTHONPATH")

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


class Ctx:
    def __init__(self, tmp: Path, kind: str, hook_file: str):
        self.kind = kind
        self.tmp = tmp
        self.home = tmp / "home"
        self.eh = self.home / ".engram"
        self.sessions = self.eh / "sessions"
        self.rt = tmp / "rt"
        for d in (self.eh, self.sessions):
            d.mkdir(parents=True, exist_ok=True)
        self.hook_file = hook_file
        self.hook = str(HOOK_DIR / hook_file)

    def special(self, path: Path, regular_text: str):
        """A FIFO (kind=fifo) or a regular file holding `regular_text` at path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_text(regular_text)

    def fifo_if_fifo(self, path: Path):
        """A FIFO at path for kind=fifo; nothing for kind=regular."""
        if self.kind == "fifo":
            path.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(path)

    def use_runtime_copy(self):
        """Copy the hooks dir to <rt>/hooks so runtime_dir (= <rt>) is a tmp dir."""
        shutil.copytree(HOOK_DIR, self.rt / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        # #3663: the runtime module U4d's flag read now imports lazily, at the plugin root.
        shutil.copy(HOOK_DIR.parent.parent / "engramd_owner_flags.py", self.rt / "engramd_owner_flags.py")
        self.hook = str(self.rt / "hooks" / self.hook_file)

    def paths(self):
        return {"home": str(self.home), "eh": str(self.eh),
                "sessions": str(self.sessions), "rt": str(self.rt), "tmp": str(self.tmp)}

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
                    f"({ctx.hook_file}, kind={ctx.kind})")
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


# --------------------------------------------------------------------------
# Cells: name -> (hook file, prepare(ctx) -> (code, {kind: predicate}))
# --------------------------------------------------------------------------

CELLS = {}


def cell(name, hook_file):
    def deco(fn):
        CELLS[name] = (hook_file, fn)
        return fn
    return deco


_STAT = "import stat\n"


# ---- shared libs -----------------------------------------------------------

@cell("harness_platform_json", "_harness.py")  # detect_harness: <runtime>/platform.json
def _c_harness(ctx):
    ctx.use_runtime_copy()
    ctx.special(ctx.rt / "platform.json", json.dumps({"platform": "codex"}))
    return ("result = mod.detect_harness()",
            {"fifo": lambda r: r is None, "regular": lambda r: r == "codex"})


@cell("hooklib_probe_stamp_read", "_hooklib.py")  # _probe_stamp_read (#3572)
def _c_probe_stamp(ctx):
    ctx.special(ctx.eh / "probe-unknown-notice.json", json.dumps({"shown_at": {"k": 1.5}}))
    return ("result = mod._probe_stamp_read(P['eh'])",
            {"fifo": lambda r: r == {}, "regular": lambda r: r == {"k": 1.5}})


@cell("hooklib_route_ledger_append", "_hooklib.py")  # write_route_ledger: the append
def _c_ledger(ctx):
    routes = ctx.eh / "probe" / "mods-recall" / "sess-3628" / "routes"
    routes.mkdir(parents=True)
    ctx.fifo_if_fifo(routes / "prompt_surface.jsonl")
    return (_STAT +
            "mod.write_route_ledger('prompt_surface', 'sess-3628', 'fk', 'q', None, [], {}, source='t')\n"
            "p = P['eh'] + '/probe/mods-recall/sess-3628/routes/prompt_surface.jsonl'\n"
            "m = os.stat(p).st_mode if os.path.lexists(p) else None\n"
            "result = [None if m is None else stat.S_ISFIFO(m),\n"
            "          len(open(p).read().splitlines()) if m is not None and stat.S_ISREG(m) else None,\n"
            "          stat.S_IMODE(m) if m is not None and stat.S_ISREG(m) else None]",
            {"fifo": lambda r: r[0] is True,
             "regular": lambda r: r == [False, 1, 0o600]})


def _ledger_ctx(tmp_path, kind="regular"):
    ctx = Ctx(tmp_path, kind, "_hooklib.py")
    routes = ctx.eh / "probe" / "mods-recall" / "sess-3628" / "routes"
    routes.mkdir(parents=True)
    return ctx, routes / "prompt_surface.jsonl"


_LEDGER_CALL = ("mod.write_route_ledger('prompt_surface', 'sess-3628', 'fk', 'q', "
                "None, [], {}, source='t')\n")


def test_route_ledger_fifo_with_a_reader_is_a_silent_zero_byte_return(tmp_path):
    """write_route_ledger keeps origin/dev's own hardened open (O_NONBLOCK +
    fstat S_ISREG): a FIFO that HAS a reader is returned from silently -- no
    byte written, nothing on stderr (the append helper would raise and the
    fail-open handler would print a line)."""
    ctx, p = _ledger_ctx(tmp_path, "fifo")
    os.mkfifo(p)
    reader = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    try:
        payload = json.dumps({"hook": ctx.hook, "paths": ctx.paths(),
                              "code": _LEDGER_CALL + "result = 1"})
        try:
            done = subprocess.run([sys.executable, "-c", _CHILD, payload],
                                  capture_output=True, text=True, timeout=GUARD_S, env=ctx.env())
        except subprocess.TimeoutExpired:
            pytest.fail("write_route_ledger blocked on a FIFO that has a reader")
        assert done.returncode == 0, done.stderr
        assert done.stderr == ""
        try:
            data = os.read(reader, 65536)
        except BlockingIOError:
            data = b""
        assert data == b""
    finally:
        os.close(reader)


def test_route_ledger_regular_file_is_created_0600_at_open(tmp_path):
    """Regular file: the open itself passes mode 0o600 (no 0o666-then-fchmod
    window). Under umask 0 the file is already 0o600 when fchmod is reached;
    exactly one line is appended."""
    ctx, p = _ledger_ctx(tmp_path)
    code = (
        "import stat\n"
        "os.umask(0)\n"
        "seen = {}\n"
        "_ro, _fc = os.open, os.fchmod\n"
        "def ro(path, flags, mode=0o777, *a, **k):\n"
        "    if str(path).endswith('prompt_surface.jsonl'):\n"
        "        seen['mode'] = mode\n"
        "        seen['nonblock'] = bool(flags & os.O_NONBLOCK)\n"
        "    return _ro(path, flags, mode, *a, **k)\n"
        "def fc(fd, mode):\n"
        "    seen['before_fchmod'] = stat.S_IMODE(os.fstat(fd).st_mode)\n"
        "    return _fc(fd, mode)\n"
        "os.open, os.fchmod = ro, fc\n"
        + _LEDGER_CALL +
        "os.open, os.fchmod = _ro, _fc\n"
        "p = P['eh'] + '/probe/mods-recall/sess-3628/routes/prompt_surface.jsonl'\n"
        "result = [seen.get('mode'), seen.get('nonblock'), seen.get('before_fchmod'),\n"
        "          stat.S_IMODE(os.stat(p).st_mode), len(open(p).read().splitlines())]"
    )
    assert _run(ctx, code) == [0o600, True, 0o600, 0o600, 1]


@cell("prompthooklib_load_config_checked", "_prompthooklib.py")
def _c_prompt_cfg(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"agent_name": "zed"}))
    return ("result = list(mod.load_config_checked(P['eh']))",
            # present-but-unreadable is UNKNOWN (None), not "no config"
            {"fifo": lambda r: r == [None, "unreadable: OSError"],
             "regular": lambda r: r == [{"agent_name": "zed"}, None]})


@cell("stale_tree_load_watch_list", "_stale_tree_hooklib.py")
def _c_stale(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps(
        {"stale_tree_watch": [{"path": "/srv/repo", "branch": "main"}]}))
    return ("result = mod.load_watch_list(P['eh'])",
            {"fifo": lambda r: r == [],
             "regular": lambda r: r == [{"path": "/srv/repo", "branch": "main"}]})


@cell("stophooklib_resolve_transcript_marker", "_stophooklib.py")
def _c_resolve(ctx):
    t = ctx.tmp / "t.jsonl"
    t.write_text("x\n")
    ctx.special(ctx.sessions / "sess.json", json.dumps({"transcript_path": str(t)}))
    return ("result = mod.resolve_transcript_path(None, 'sess', P['sessions'])",
            {"fifo": lambda r: r is None, "regular": lambda r: r == str(t)})


@cell("stophooklib_read_offset_state", "_stophooklib.py")
def _c_offset(ctx):
    ctx.special(ctx.eh / "state.json", json.dumps({"transcript_path": "/t", "byte_offset": 5}))
    return ("try:\n"
            "    result = list(mod.read_offset_state(P['eh'] + '/state.json'))\n"
            "except OSError as e:\n"
            "    result = type(e).__name__",
            # documented: raises on any read failure; the callers catch OSError
            {"fifo": lambda r: r == "OSError", "regular": lambda r: r == ["/t", 5]})


@cell("stophooklib_read_delta", "_stophooklib.py")
def _c_delta(ctx):
    ctx.special(ctx.tmp / "t.bin", "0123456789")
    return ("try:\n"
            "    result = mod.read_delta(P['tmp'] + '/t.bin', 2, 6).decode()\n"
            "except OSError as e:\n"
            "    result = type(e).__name__",
            {"fifo": lambda r: r == "OSError", "regular": lambda r: r == "2345"})


@cell("context_tracker_find_active_jsonl", "context_tracker.py")  # session marker
def _c_ct_find(ctx):
    t = ctx.tmp / "t.jsonl"
    t.write_text("x\n")
    ctx.special(ctx.sessions / "sess.json", json.dumps({"transcript_path": str(t)}))
    return ("result = mod.find_active_jsonl('sess')",
            {"fifo": lambda r: r is None, "regular": lambda r: r == str(t)})


@cell("context_tracker_ceiling_config", "context_tracker.py")
def _c_ct_ceiling(ctx):
    ctx.special(ctx.eh / "config.json",
                json.dumps({"cadence": {"drowsiness_ceiling_tokens": 123456}}))
    return ("result = mod._read_drowsiness_ceiling_tokens()",
            {"fifo": lambda r: r == 152000, "regular": lambda r: r == 123456})


@cell("context_tracker_last_compact_offset", "context_tracker.py")
def _c_ct_compact(ctx):
    t = ctx.tmp / "t.jsonl"
    t.write_text("x\n")
    ctx.special(ctx.eh / "last-compact-at.json",
                json.dumps({"jsonl_path": str(t), "byte_offset": 3}))
    return (f"result = mod._last_compact_offset({str(t)!r})",
            {"fifo": lambda r: r is None, "regular": lambda r: r == 3})


@cell("context_tracker_read_current_tokens_transcript", "context_tracker.py")
def _c_ct_tokens(ctx):
    t = ctx.tmp / "t.jsonl"
    ctx.special(t, json.dumps({"message": {"usage": {
        "input_tokens": 10, "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 1}}}) + "\n")
    return (f"result = mod.read_current_tokens({str(t)!r})",
            {"fifo": lambda r: r is None, "regular": lambda r: r == 16})


@cell("context_tracker_drowsiness_thresholds", "context_tracker.py")
def _c_ct_thresholds(ctx):
    ctx.special(ctx.eh / "config.json",
                json.dumps({"drowsiness_display": {"1m": {"refreshed_below": 0.11}}}))
    return ("result = mod._load_drowsiness_thresholds('1m')['refreshed_below']",
            {"fifo": lambda r: r == 0.5, "regular": lambda r: r == 0.11})


# ---- prompt hooks ----------------------------------------------------------

@cell("baton_load_cache", "engram-baton-prompt-hook.py")
def _c_baton_load(ctx):
    ctx.special(ctx.eh / "baton-status-cache.json", json.dumps({"a": 1}))
    return ("result = mod._load_cache()",
            {"fifo": lambda r: r == {}, "regular": lambda r: r == {"a": 1}})


@cell("baton_save_cache", "engram-baton-prompt-hook.py")  # a direct write of a fixed path
def _c_baton_save(ctx):
    ctx.special(ctx.eh / "baton-status-cache.json", json.dumps({"a": 1}))
    return (_STAT + "mod._save_cache({'b': 2})\n"
            "p = P['eh'] + '/baton-status-cache.json'\n"
            "result = 'fifo' if stat.S_ISFIFO(os.stat(p).st_mode) else json.load(open(p))",
            {"fifo": lambda r: r == "fifo", "regular": lambda r: r == {"b": 2}})


@cell("inter_agent_read_cursor", "engram-inter-agent-prompt-hook.py")
def _c_ia_read(ctx):
    ctx.special(ctx.eh / "cur.txt", "42\n")
    return ("result = mod._read_cursor_int(P['eh'] + '/cur.txt')",
            {"fifo": lambda r: r == 0, "regular": lambda r: r == 42})


@cell("inter_agent_write_cursor_tmp_name_is_a_fifo", "engram-inter-agent-prompt-hook.py")
def _c_ia_write(ctx):
    # temp-then-rename writer whose temp NAME is predictable (<dest>.tmp)
    ctx.fifo_if_fifo(ctx.eh / "cur.txt.tmp")
    return ("mod._write_cursor_int(P['eh'] + '/cur.txt', 7)\n"
            "p = P['eh'] + '/cur.txt'\n"
            "result = open(p).read() if os.path.isfile(p) else None",
            {"fifo": lambda r: r is None, "regular": lambda r: r == "7\n"})


@cell("forum_read_cursor", "engram-forum-prompt-hook.py")
def _c_forum_read_cursor(ctx):
    ctx.special(ctx.eh / "cur.txt", "2026-01-01T00:00:00.000000Z\n")
    return ("result = str(mod._read_cursor(P['eh'] + '/cur.txt'))",
            {"fifo": lambda r: r == "None", "regular": lambda r: r.startswith("2026-01-01")})


@cell("forum_write_cursor_tmp_name_is_a_fifo", "engram-forum-prompt-hook.py")
def _c_forum_write_cursor(ctx):
    ctx.fifo_if_fifo(ctx.eh / "cur.txt.tmp")
    return ("from datetime import datetime, timezone\n"
            "mod._write_cursor(P['eh'] + '/cur.txt', datetime(2026, 1, 2, tzinfo=timezone.utc))\n"
            "p = P['eh'] + '/cur.txt'\n"
            "result = open(p).read() if os.path.isfile(p) else None",
            {"fifo": lambda r: r is None, "regular": lambda r: r.startswith("2026-01-02")})


@cell("forum_load_cache", "engram-forum-prompt-hook.py")
def _c_forum_load_cache(ctx):
    ctx.special(ctx.eh / "hc.json", json.dumps({"fetched_at": 9999999999, "threads": []}))
    return ("mod.HOOK_CACHE_PATH = P['eh'] + '/hc.json'\n"
            "r = mod._load_cache()\n"
            "result = None if r is None else r['threads']",
            {"fifo": lambda r: r is None, "regular": lambda r: r == []})


@cell("forum_save_cache_tmp_name_is_a_fifo", "engram-forum-prompt-hook.py")
def _c_forum_save_cache(ctx):
    ctx.fifo_if_fifo(ctx.eh / "hc.json.tmp")
    return ("mod.HOOK_CACHE_PATH = P['eh'] + '/hc.json'\n"
            "mod._save_cache([], [])\n"
            "p = P['eh'] + '/hc.json'\n"
            "result = os.path.isfile(p)",
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


@cell("forum_load_publish_state", "engram-forum-prompt-hook.py")
def _c_forum_load_pub(ctx):
    ctx.special(ctx.eh / "pub.json", json.dumps({"state": "idle"}))
    return ("mod.STATUS_PUBLISH_PATH = P['eh'] + '/pub.json'\n"
            "result = mod._load_publish_state()",
            {"fifo": lambda r: r is None, "regular": lambda r: r == {"state": "idle"}})


@cell("forum_save_publish_state_tmp_name_is_a_fifo", "engram-forum-prompt-hook.py")
def _c_forum_save_pub(ctx):
    ctx.fifo_if_fifo(ctx.eh / "pub.json.tmp")
    return ("mod.STATUS_PUBLISH_PATH = P['eh'] + '/pub.json'\n"
            "mod._save_publish_state('idle', None, [], None, 1.0)\n"
            "p = P['eh'] + '/pub.json'\n"
            "result = os.path.isfile(p)",
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


# ---- deference detector ----------------------------------------------------

@cell("deference_prompt_is_cron_prompt_marker", "engram-deference-detector-prompt.py")
def _c_def_prompt(ctx):
    ctx.special(ctx.eh / "loop-mode.json", json.dumps({"loop_prompt": "hello there"}))
    return ("result = mod._is_cron_prompt('hello there', P['eh'] + '/loop-mode.json')",
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


@cell("deference_stop_cooldown_config", "engram-deference-detector-stop.py")
def _c_def_stop_cd(ctx):
    ctx.special(ctx.eh / "config.json",
                json.dumps({"deference_detector": {"cooldown_minutes": 3}}))
    return ("result = mod._cooldown_minutes()",
            {"fifo": lambda r: r == 10.0, "regular": lambda r: r == 3.0})


@cell("deference_stop_last_assistant_structure_transcript", "engram-deference-detector-stop.py")
def _c_def_stop_struct(ctx):
    t = ctx.tmp / "t.jsonl"
    ctx.special(t, json.dumps({"type": "assistant", "message": {"content": "hi there"}}) + "\n")
    return (f"r = mod._last_assistant_structure(jsonl_path={str(t)!r})\n"
            "result = None if r is None else r['joined']",
            {"fifo": lambda r: r is None, "regular": lambda r: r == "hi there"})


@cell("deference_stop_log_append", "engram-deference-detector-stop.py")
def _c_def_stop_log(ctx):
    ctx.special(ctx.eh / "deference-detector.log", "pre\n")
    return (_STAT + "mod._log('hello')\n"
            "p = P['eh'] + '/deference-detector.log'\n"
            "result = 'fifo' if stat.S_ISFIFO(os.stat(p).st_mode) else open(p).read()",
            {"fifo": lambda r: r == "fifo",
             "regular": lambda r: r.startswith("pre\n") and r.rstrip().endswith("hello")})


# ---- end-of-day ------------------------------------------------------------

@cell("end_of_day_sleep_marker", "engram-end-of-day-hook.py")
def _c_eod(ctx):
    ctx.special(ctx.sessions / "last-sleep-success.json",
                json.dumps({"completed_at": "2020-01-01T00:00:00Z"}))
    return ("result = list(mod.sleep_recently())",
            {"fifo": lambda r: r == [False, ""],
             "regular": lambda r: r[0] is False and r[1].endswith("h")})


# ---- loop-arm stop hook ----------------------------------------------------

@cell("loop_arm_runtime_platform", "engram-loop-arm-stop-hook.py")
def _c_la_platform(ctx):
    ctx.use_runtime_copy()
    ctx.special(ctx.rt / "platform.json", json.dumps({"platform": "codex"}))
    return ("result = mod._runtime_platform()",
            {"fifo": lambda r: r is None, "regular": lambda r: r == "codex"})


@cell("loop_arm_pacer_marker", "engram-loop-arm-stop-hook.py")
def _c_la_pacer(ctx):
    ctx.special(ctx.eh / "loop-mode.json", json.dumps({"pacer": "scheduleWakeup"}))
    return ("result = mod._pacer_is_schedule_wakeup(P['eh'] + '/loop-mode.json')",
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


# ---- stop hook -------------------------------------------------------------

@cell("stop_hook_node_threshold_config", "engram-stop-hook.py")
def _c_stop_node(ctx):
    ctx.special(ctx.eh / "config.json",
                json.dumps({"cadence": {"write_nudge_node_threshold": 7}}))
    return ("result = mod._read_node_threshold()",
            {"fifo": lambda r: r == 300, "regular": lambda r: r == 7})


@cell("stop_hook_prose_threshold_config", "engram-stop-hook.py")
def _c_stop_prose(ctx):
    ctx.special(ctx.eh / "config.json",
                json.dumps({"cadence": {"write_nudge_prose_threshold": 9}}))
    return ("result = [mod._read_prose_threshold(), mod._DEFAULT_PROSE_THRESHOLD]",
            {"fifo": lambda r: r[0] == r[1], "regular": lambda r: r[0] == 9})


@cell("stop_hook_should_suppress_offset_state", "engram-stop-hook.py")  # read_offset_state caller
def _c_stop_suppress(ctx):
    t = ctx.tmp / "t.jsonl"
    t.write_text("xxxxx")
    ctx.special(ctx.eh / ".write-nudge-last-fire.json",
                json.dumps({"transcript_path": str(t), "byte_offset": 5}))
    return (f"result = mod._should_suppress({str(t)!r})",
            # unreadable state -> unknown -> emit (False); matching state, no new bytes -> suppress
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


# ---- time-bar --------------------------------------------------------------

@cell("time_bar_read_json", "engram-time-bar-hook.py")
def _c_tb_read(ctx):
    ctx.special(ctx.eh / "x.json", json.dumps({"a": 1}))
    return ("from pathlib import Path\n"
            "result = mod._read_json(Path(P['eh'] + '/x.json'))",
            {"fifo": lambda r: r is None, "regular": lambda r: r == {"a": 1}})


@cell("time_bar_stamp_user_activity", "engram-time-bar-hook.py")  # direct write of a fixed path
def _c_tb_stamp(ctx):
    ctx.special(ctx.eh / "last-user-activity", "old")
    return (_STAT + "mod._stamp_user_activity(1234.0)\n"
            "p = P['eh'] + '/last-user-activity'\n"
            "result = 'fifo' if stat.S_ISFIFO(os.stat(p).st_mode) else open(p).read()",
            {"fifo": lambda r: r == "fifo", "regular": lambda r: r == "1234"})


# ---- user-identity ---------------------------------------------------------

@cell("user_identity_primary_user_config", "engram-user-identity-hook.py")
def _c_ui_primary(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"primary_user": "Kfixture"}))
    return ("result = mod.get_primary_user()",
            {"fifo": lambda r: r == "", "regular": lambda r: r == "kfixture"})


@cell("user_identity_owner_reads_flag", "engram-user-identity-hook.py")
def _c_ui_owner(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps(
        {"engramd": {"user_identity_reads_via_owner": True}}))
    return ("result = mod._owner_reads_enabled()",
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


# ---- utility-credit-mention stop -------------------------------------------

@cell("utility_log_append", "engram-utility-credit-mention-stop.py")
def _c_ut_log(ctx):
    ctx.special(ctx.eh / "u.log", "pre\n")
    return (_STAT + "mod._log('hello', P['eh'] + '/u.log')\n"
            "p = P['eh'] + '/u.log'\n"
            "result = 'fifo' if stat.S_ISFIFO(os.stat(p).st_mode) else open(p).read()",
            {"fifo": lambda r: r == "fifo",
             "regular": lambda r: r.startswith("pre\n") and r.rstrip().endswith("hello")})


@cell("utility_collect_turn_text_transcript", "engram-utility-credit-mention-stop.py")
def _c_ut_turn(ctx):
    t = ctx.tmp / "t.jsonl"
    ctx.special(t, json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "earlier"}]}}) + "\n")
    return (f"result = mod.collect_turn_text({str(t)!r}, 'last')",
            {"fifo": lambda r: r == "last", "regular": lambda r: r == "earlier\nlast"})


@cell("utility_read_prompt_count", "engram-utility-credit-mention-stop.py")
def _c_ut_count(ctx):
    ctx.special(ctx.eh / "pc.json", json.dumps({"prompts_since_compaction": 12}))
    return ("result = mod._read_prompt_count(P['eh'] + '/pc.json')",
            {"fifo": lambda r: r == 0, "regular": lambda r: r == 12})


@cell("utility_check_enactments_registry", "engram-utility-credit-mention-stop.py")
def _c_ut_registry(ctx):
    # handler used to catch only FileNotFoundError / JSONDecodeError
    ctx.special(ctx.eh / "reg.json", json.dumps({"k": [{"principle_id": "other"}]}))
    return ("mod.PRINCIPLE_TRIGGERS_PATH = P['eh'] + '/reg.json'\n"
            "mod._check_enactments(['pid-a'], state_path=P['eh'] + '/st.json',\n"
            "                      prompt_count_path=P['eh'] + '/pc.json')\n"
            "result = 'returned'",
            {"fifo": lambda r: r == "returned", "regular": lambda r: r == "returned"})


@cell("utility_check_enactments_state", "engram-utility-credit-mention-stop.py")
def _c_ut_state(ctx):
    (ctx.eh / "reg.json").write_text(json.dumps({"k": [{"principle_id": "pid-a"}]}))
    (ctx.eh / "pc.json").write_text(json.dumps({"prompts_since_compaction": 50}))
    ctx.special(ctx.eh / "st.json", "{}")
    return (_STAT + "mod.PRINCIPLE_TRIGGERS_PATH = P['eh'] + '/reg.json'\n"
            "mod._check_enactments(['pid-a'], state_path=P['eh'] + '/st.json',\n"
            "                      prompt_count_path=P['eh'] + '/pc.json')\n"
            "p = P['eh'] + '/st.json'\n"
            "result = 'fifo' if stat.S_ISFIFO(os.stat(p).st_mode) else json.load(open(p))['pid-a']['enactments']",
            # #3521 row 40: a non-regular state path is UNKNOWN, so the hook
            # skips the write and the FIFO is left in place (it used to be
            # treated as empty and replaced by the atomic write). The cell
            # reads the result without open()ing the FIFO, which would block.
            {"fifo": lambda r: r == "fifo", "regular": lambda r: r == 1})


# A REGULAR registry file whose open fails with PermissionError must PROPAGATE
# out of _check_enactments exactly as on origin/dev (whose handler caught only
# FileNotFoundError / JSONDecodeError). Only a non-regular target (the FIFO /
# directory refusal) is swallowed. (The STATE file is no longer in this cell:
# since #3521 row 40 it is read through _hooklib.read_state_json_classified and
# an unreadable state file is UNKNOWN -- skipped with one stderr line, never
# raised; see tests/test_principle_trigger_state_writers_3521.py U1.) The
# failure is forced two ways at once so the very same assertion runs against
# both trees: the helper this branch calls
# (`open_regular_text`) and the builtin `open` dev calls, each raising only for
# the one target path.
_FORCE_PERM = (
    "import builtins\n"
    "_real_open = builtins.open\n"
    "_real_helper = getattr(mod, 'open_regular_text', None)\n"
    "def _deny(real):\n"
    "    def w(path, *a, **k):\n"
    "        if str(path).endswith(TARGET):\n"
    "            raise PermissionError(13, 'denied', str(path))\n"
    "        return real(path, *a, **k)\n"
    "    return w\n"
    "builtins.open = _deny(_real_open)\n"
    "if _real_helper is not None:\n"
    "    mod.open_regular_text = _deny(_real_helper)\n"
    "try:\n"
    "    mod._check_enactments(['pid-a'], state_path=P['eh'] + '/st.json',\n"
    "                          prompt_count_path=P['eh'] + '/pc.json')\n"
    "    result = 'returned'\n"
    "except OSError as e:\n"
    "    result = type(e).__name__\n"
    "finally:\n"
    "    builtins.open = _real_open\n"
)


@pytest.mark.parametrize("target", ["reg.json"])
def test_utility_check_enactments_regular_file_oserror_propagates(tmp_path, target):
    ctx = Ctx(tmp_path, "regular", "engram-utility-credit-mention-stop.py")
    (ctx.eh / "reg.json").write_text(json.dumps({"k": [{"principle_id": "pid-a"}]}))
    (ctx.eh / "pc.json").write_text(json.dumps({"prompts_since_compaction": 50}))
    (ctx.eh / "st.json").write_text("{}")
    code = (f"TARGET = {target!r}\n"
            "mod.PRINCIPLE_TRIGGERS_PATH = P['eh'] + '/reg.json'\n" + _FORCE_PERM)
    assert _run(ctx, code) == "PermissionError"


@pytest.mark.parametrize("kind", ["fifo", "regular"])
@pytest.mark.parametrize("name", sorted(CELLS))
def test_cell(tmp_path, name, kind):
    hook_file, prepare = CELLS[name]
    ctx = Ctx(tmp_path, kind, hook_file)
    code, expect = prepare(ctx)
    result = _run(ctx, code)
    assert expect[kind](result), f"{name}/{kind}: unexpected result {result!r}"


# --------------------------------------------------------------------------
# Whole-hook cells: the real script, a FIFO at the paths ONLY that hook touches
# --------------------------------------------------------------------------

def _script(ctx: Ctx, stdin: str, timeout=40):
    try:
        return subprocess.run([sys.executable, ctx.hook], input=stdin, capture_output=True,
                              text=True, timeout=timeout, env=ctx.env())
    except subprocess.TimeoutExpired:
        pytest.fail(f"{ctx.hook_file} blocked for more than {timeout}s (kind={ctx.kind})")


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_deference_prompt(tmp_path, kind):
    ctx = Ctx(tmp_path, kind, "engram-deference-detector-prompt.py")
    (ctx.eh / "loop-mode.json").write_text(json.dumps({"loop_prompt": "something else"}))
    ctx.special(ctx.eh / "deference-cooldown-at.txt", "x")   # written for a real user prompt
    ctx.special(ctx.eh / "deference-detected.json", json.dumps({"pending": False}))
    done = _script(ctx, json.dumps({"session_id": "s", "prompt": "a real user message"}))
    assert done.returncode == 0, done.stderr
    cooldown = ctx.eh / "deference-cooldown-at.txt"
    if kind == "regular":
        assert cooldown.read_text().startswith("20")   # the cooldown stamp was written


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_deference_stop(tmp_path, kind):
    ctx = Ctx(tmp_path, kind, "engram-deference-detector-stop.py")
    (ctx.eh / "loop-mode.json").write_text("{}")
    ctx.special(ctx.eh / "config.json", "{}")
    ctx.special(ctx.eh / "deference-cooldown-at.txt", "2999-01-01T00:00:00+00:00")
    ctx.special(ctx.eh / "deference-detector.log", "")
    done = _script(ctx, json.dumps({"session_id": "s", "last_assistant_message": "Should I proceed?"}))
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_end_of_day(tmp_path, kind):
    ctx = Ctx(tmp_path, kind, "engram-end-of-day-hook.py")
    ctx.special(ctx.sessions / "last-sleep-success.json",
                json.dumps({"completed_at": "2020-01-01T00:00:00Z"}))
    done = _script(ctx, json.dumps({"prompt": "good night"}))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip(), "the nudge (or an empty {}) is still printed"


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_time_bar(tmp_path, kind):
    ctx = Ctx(tmp_path, kind, "engram-time-bar-hook.py")
    ctx.special(ctx.eh / "config.json", json.dumps({"user": {"timezone": "UTC"}}))
    ctx.special(ctx.eh / "last-user-msg.json", json.dumps({"ts": "2020-01-01T00:00:00Z"}))
    ctx.special(ctx.eh / "last-user-activity", "1")
    ctx.special(ctx.sessions / "s.json", json.dumps({"started_at": "2020-01-01T00:00:00Z"}))
    done = _script(ctx, json.dumps({"session_id": "s", "prompt": "hello", "promptSource": "typed"}))
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert out.startswith("[Time:")
    if kind == "regular":
        assert "session started" in out and "hook-error" not in out


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_loop_arm_stop(tmp_path, kind):
    ctx = Ctx(tmp_path, kind, "engram-loop-arm-stop-hook.py")
    (ctx.eh / "loop-mode.json").write_text(json.dumps({"pacer": "scheduleWakeup"}))
    t = tmp_path / "t.jsonl"
    t.write_text("not a record\n")
    ctx.special(ctx.eh / ".loop-arm-nudge-last-fire.json",
                json.dumps({"transcript_path": str(t), "byte_offset": 0}))
    done = _script(ctx, json.dumps({"session_id": "s", "transcript_path": str(t)}))
    assert done.returncode == 0, done.stderr
    assert "Loop Arm Check" in done.stdout   # no ScheduleWakeup in the delta: the nudge fires


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_stop_hook(tmp_path, kind):
    ctx = Ctx(tmp_path, kind, "engram-stop-hook.py")
    t = tmp_path / "t.jsonl"
    t.write_text("x" * 600 + "\n")
    ctx.special(ctx.eh / "config.json", "{}")
    ctx.special(ctx.eh / ".write-nudge-last-fire.json",
                json.dumps({"transcript_path": str(t), "byte_offset": 0}))
    done = _script(ctx, json.dumps({"session_id": "s", "transcript_path": str(t)}))
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("hook_file", ["engram-baton-prompt-hook.py",
                                       "engram-inter-agent-prompt-hook.py",
                                       "engram-forum-prompt-hook.py"])
@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_prompt_hooks_config_json(tmp_path, kind, hook_file):
    """A FIFO at config.json: the hook returns inside the guard (config UNKNOWN,
    reported, exit 0) instead of hanging in its prologue."""
    ctx = Ctx(tmp_path, kind, hook_file)
    ctx.special(ctx.eh / "config.json", "{}")
    done = _script(ctx, json.dumps({"session_id": "s", "prompt": "hello"}))
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_whole_hook_user_identity_sticky_marker(tmp_path, kind):
    ctx = Ctx(tmp_path, kind, "engram-user-identity-hook.py")
    db = sqlite3.connect(ctx.eh / "knowledge.db")
    db.execute("CREATE TABLE nodes (id TEXT, type TEXT, claim TEXT, logical_chain TEXT, "
               "metadata TEXT, is_current INT)")
    db.execute("INSERT INTO nodes VALUES ('pn-x','person','Zed','','{}',1)")
    db.commit()
    db.close()
    ctx.special(ctx.eh / "current_user.json", json.dumps({"name": "Zed", "person_id": "pn-x"}))
    ctx.special(ctx.eh / "config.json", "{}")
    done = _script(ctx, json.dumps({"session_id": "s", "prompt": "plain message no prefix"}))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) is not None


# --------------------------------------------------------------------------
# Source guard: no raw blocking open anywhere in the in-scope files
# --------------------------------------------------------------------------

_IN_SCOPE = [
    "_harness.py", "_hooklib.py", "_prompthooklib.py", "_stale_tree_hooklib.py",
    "_stophooklib.py", "context_tracker.py", "engram-baton-prompt-hook.py",
    "engram-deference-detector-prompt.py", "engram-deference-detector-stop.py",
    "engram-end-of-day-hook.py", "engram-forum-prompt-hook.py",
    "engram-inter-agent-prompt-hook.py", "engram-loop-arm-stop-hook.py",
    "engram-stop-hook.py", "engram-time-bar-hook.py", "engram-user-identity-hook.py",
    "engram-utility-credit-mention-stop.py",
]

# Functions that ARE the non-blocking helpers (they call os.open themselves).
# The loop-arm hook's two are its import-failure fallback, pinned identical to
# _hooklib's by tests/test_regular_io_parity_3628.py.
_HELPER_DEFS = {
    # write_route_ledger: origin/dev's own already-hardened open (O_NONBLOCK +
    # fstat S_ISREG, silent return on a non-regular target); deliberately NOT
    # routed through the append helper, which would change its behaviour.
    # append_shown_record (#3613): the shown record's own hardened opens -- the append
    # open is O_NONBLOCK + fstat S_ISREG (a non-regular target is never written), the trim
    # tmp is created O_EXCL|O_NOFOLLOW after an unlink (it cannot be a FIFO), and the file
    # is 0o600. Like write_route_ledger it is deliberately NOT routed through the append /
    # write helpers, which would change the mode and drop the no-follow create.
    # atomic_write_text (#4013): its one os.open is O_CREAT|O_EXCL on a fresh random temp name
    # beside the target, so it cannot be a FIFO; the target itself is only stat'd and replaced.
    "_hooklib.py": {"_open_regular_fd", "open_regular_text", "open_regular_text_write",
                    "open_regular_text_append", "write_route_ledger",
                    "append_shown_record", "atomic_write_text"},
    # _reply_gate_fd (#4193): the reply-gate log's own hardened open (O_NONBLOCK + fstat S_ISREG,
    # a non-regular target raises before anything is read or written).
    "engram-loop-arm-stop-hook.py": {"open_regular_text", "_reply_gate_fd"},
    # #3658: the utility hook's byte-identical copy of the state-lock helper (its one
    # os.open is the O_CREAT lock-file open; the surface hook is allowed the same one
    # by test_surface_hook_nonblocking_opens_3628s2c.py, "the state lock").
    "engram-utility-credit-mention-stop.py": {"_with_state_lock"},
}
_RAW = {"read_text", "write_text", "read_bytes", "write_bytes"}


def _raw_blocking_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = _HELPER_DEFS.get(path.name, set())
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in allowed:
            skip.update(id(c) for c in ast.walk(n))
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or id(n) in skip:
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == "open":
            hits.append((n.lineno, "open()"))
        elif isinstance(f, ast.Attribute):
            if f.attr in _RAW:
                hits.append((n.lineno, f".{f.attr}()"))
            elif f.attr == "open" and isinstance(f.value, ast.Name) and f.value.id == "os":
                hits.append((n.lineno, "os.open()"))
    return hits


@pytest.mark.parametrize("name", _IN_SCOPE)
def test_no_raw_blocking_open_in_scope_file(name):
    hits = _raw_blocking_calls(HOOK_DIR / name)
    assert hits == [], (
        f"{name}: raw blocking opens remain (route them through the _hooklib "
        f"open_regular_text* helpers): {hits}")


def test_source_guard_flags_a_planted_raw_open(tmp_path):
    """The guard itself can fail: a planted raw open / read_text / os.open is found."""
    p = tmp_path / "planted.py"
    p.write_text("import os\n"
                 "def f(path):\n"
                 "    open(path)\n"
                 "    path.read_text()\n"
                 "    os.open(path, 0)\n")
    assert [h[1] for h in _raw_blocking_calls(p)] == ["open()", ".read_text()", "os.open()"]


# --------------------------------------------------------------------------
# hooklib_io(): the prompt hooks keep running without the _hooklib helpers
# --------------------------------------------------------------------------

def _load_prompthooklib():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prompthooklib_3628s2a", HOOK_DIR / "_prompthooklib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hooklib_io_raises_oserror_when_hooklib_cannot_be_imported(monkeypatch):
    mod = _load_prompthooklib()
    monkeypatch.setitem(sys.modules, "_hooklib", None)   # `import _hooklib` -> ImportError
    with pytest.raises(OSError, match="unavailable"):
        mod.hooklib_io()


def test_hooklib_io_raises_oserror_when_hooklib_predates_the_helpers(monkeypatch):
    import types
    mod = _load_prompthooklib()
    monkeypatch.setitem(sys.modules, "_hooklib", types.ModuleType("_hooklib"))
    with pytest.raises(OSError, match="lacks"):
        mod.hooklib_io()


def test_load_config_checked_reports_unknown_not_a_crash_without_hooklib(tmp_path, monkeypatch):
    mod = _load_prompthooklib()
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setitem(sys.modules, "_hooklib", None)
    assert mod.load_config_checked(str(tmp_path)) == (None, "unreadable: OSError")


# --------------------------------------------------------------------------
# The libs still load (and work) when loaded BY PATH ALONE -- the way many
# tests load them -- i.e. with nothing having put hooks/claude on sys.path.
# --------------------------------------------------------------------------

_STANDALONE = {
    "_harness.py": ("result = mod.detect_harness()", None),
    "context_tracker.py": ("result = mod.find_active_jsonl(None)", None),
    "_stale_tree_hooklib.py": ("result = mod.load_watch_list(P)", []),
    "_stophooklib.py": ("result = mod.resolve_transcript_path(None)", None),
    "_prompthooklib.py": ("result = list(mod.load_config_checked(P))", [{}, None]),
}


@pytest.mark.parametrize("name", sorted(_STANDALONE))
def test_lib_loaded_by_path_alone_finds_hooklib(tmp_path, name):
    (tmp_path / "config.json").write_text("{}")
    call, expected = _STANDALONE[name]
    code = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('lib_3628s2a', {str(HOOK_DIR / name)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        f"P = {str(tmp_path)!r}\n"
        f"{call}\n"
        "print('RESULT:' + json.dumps(result))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["ENGRAM_HOME"] = str(tmp_path)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=GUARD_S, env=env, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    assert json.loads(line[len("RESULT:"):]) == expected
