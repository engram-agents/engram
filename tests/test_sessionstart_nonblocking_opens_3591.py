"""#3591: SessionStart must never block on a non-regular file (FIFO).

The SessionStart hook runs under a 5 s harness timeout. A plain `open()` of a
path where a FIFO sits waits for a writer (read) or a reader (write), so the
hook hangs and is killed silently. #3576 fixed one site (the session marker
read in `write_marker`); this file covers the whole class: every config /
marker / platform / sidecar / registry read in the hook, the sidecar WRITE,
and the read-only SQLite opens of `knowledge.db`.

Each cell drives the REAL function in a SUBPROCESS with a hard timeout, so a
regression fails (timeout) instead of hanging the suite. Every cell runs twice:

  * kind="fifo"    -- a FIFO at the path; must return promptly with the
                      documented fallback (file treated as absent);
  * kind="regular" -- a regular file at the path; positive control that the
                      real behaviour is unchanged (green before and after).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK_NAME = "engram-session-start-hook.py"

GUARD_S = int(os.environ.get("SESSIONSTART_3591_GUARD_S", "8"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")

# Runs inside the child: load the hook module, exec the cell's code, print JSON.
_CHILD = r"""
import importlib.util, json, os, sys
from unittest.mock import MagicMock
spec_in = json.loads(sys.argv[1])
sys.modules["context_tracker"] = MagicMock()
sys.path.insert(0, os.path.dirname(spec_in["hook"]))
spec = importlib.util.spec_from_file_location("hook_3591_child", spec_in["hook"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = spec_in["paths"]
ns = {"mod": mod, "P": P, "os": os, "json": json, "result": None}
exec(spec_in["code"], ns)
print("RESULT:" + json.dumps(ns["result"]))
"""


class Ctx:
    def __init__(self, tmp: Path, kind: str):
        self.kind = kind
        self.tmp = tmp
        self.home = tmp / "home"
        self.eh = self.home / ".engram"
        self.shared = tmp / "shared"
        self.registry = self.shared / "agents"
        self.sessions = self.eh / "sessions"
        self.rt = tmp / "rt"  # copy of the hooks dir laid out as a plugin runtime
        for d in (self.eh, self.sessions, self.registry):
            d.mkdir(parents=True, exist_ok=True)
        self.hook = str(HOOK_DIR / HOOK_NAME)
        self.extra_env = {}

    def special(self, path: Path, regular_text: str):
        """Put a FIFO (kind=fifo) or a regular file with `regular_text` at path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_text(regular_text)

    def use_runtime_copy(self):
        """Copy the hooks dir to <rt>/hooks so runtime_dir (= <rt>) is a tmp dir."""
        shutil.copytree(HOOK_DIR, self.rt / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.hook = str(self.rt / "hooks" / HOOK_NAME)

    def paths(self):
        return {"home": str(self.home), "eh": str(self.eh), "shared": str(self.shared),
                "registry": str(self.registry), "sessions": str(self.sessions),
                "rt": str(self.rt), "tmp": str(self.tmp)}


def _run(ctx: Ctx, code: str, timeout=GUARD_S):
    env = dict(os.environ)
    env.update({"HOME": str(ctx.home), "ENGRAM_HOME": str(ctx.eh),
                "AGENTS_SHARED_DIR": str(ctx.shared), "ENGRAM_PIECE_C_REPOS": "",
                "ENGRAM_SESSION_ROLE": "interactive"})
    env.pop("ENGRAM_RUNTIME_DIR", None)
    env.update(ctx.extra_env)
    payload = json.dumps({"hook": ctx.hook, "paths": ctx.paths(), "code": code})
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD, payload],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"blocked for more than {timeout}s on a non-regular file (kind={ctx.kind})")
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _old(path: Path, days: float):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


# --------------------------------------------------------------------------
# Cells: (id, prepare(ctx), code, expect(kind, ctx_state) -> predicate)
# `expect` is a function (result) -> bool for each kind.
# --------------------------------------------------------------------------

CELLS = {}


def cell(name):
    def deco(fn):
        CELLS[name] = fn
        return fn
    return deco


@cell("platform_json")  # _runtime_platform: <runtime>/platform.json
def _c_platform(ctx):
    ctx.use_runtime_copy()
    ctx.special(ctx.rt / "platform.json", json.dumps({"platform": "codex"}))
    return ("result = mod._runtime_platform()",
            {"fifo": lambda r: r is None, "regular": lambda r: r == "codex"})


@cell("mcp_marker_dead_pid")  # _dead_marker_pid: mcp-tools-ready.json
def _c_dead_pid(ctx):
    pid = _dead_pid()
    ctx.special(ctx.eh / "mcp-tools-ready.json", json.dumps({"pid": pid}))
    return ("result = mod._dead_marker_pid(P['eh'])",
            {"fifo": lambda r: r is None, "regular": lambda r: r == pid})


@cell("mcp_marker_write_tool")  # _check_mcp_write_tool_marker: mcp-tools-ready.json
def _c_write_tool(ctx):
    ctx.special(ctx.eh / "mcp-tools-ready.json", json.dumps({"pid": 0}))
    return ("result = list(mod._check_mcp_write_tool_marker(P['eh']))",
            {"fifo": lambda r: r == [True, None],  # advisory: any error -> healthy
             "regular": lambda r: r[0] is False and "no valid pid" in r[1]})


@cell("backup_threshold_config")  # _check_backup_staleness: config.json read
def _c_backup(ctx):
    (ctx.eh / "knowledge.db").write_bytes(b"x" * 16)
    (ctx.eh / "knowledge.sql").write_bytes(b"x")
    _old(ctx.eh / "knowledge.sql", 3 / 24)  # 3h behind the db
    ctx.special(ctx.eh / "config.json",
                json.dumps({"backup": {"stale_threshold_hours": 1000}}))
    return ("result = list(mod._check_backup_staleness(P['eh']))",
            # config unreadable -> default 2h threshold -> stale
            {"fifo": lambda r: r[0] is False,
             "regular": lambda r: r == [True, None]})


@cell("focus_staleness_config")  # _get_focus_staleness_days: config.json
def _c_focus_days(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"focus": {"focus_staleness_days": 3}}))
    return ("result = mod._get_focus_staleness_days(P['eh'])",
            {"fifo": lambda r: r == 7, "regular": lambda r: r == 3})


@cell("auto_sleep_config")  # _auto_sleep_settings: config.json
def _c_auto_sleep(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps(
        {"cadence": {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"}}))
    return ("result = mod._auto_sleep_settings() is not None",
            {"fifo": lambda r: r is False, "regular": lambda r: r is True})


@cell("fairy_policy_config")  # fairy_policy_block: config.json
def _c_fairy(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps(
        {"coder_fairy_policy": "explicit", "primary_user": "Zed"}))
    return ("result = mod.fairy_policy_block()",
            # unreadable config -> the function's documented silent ""
            {"fifo": lambda r: r == "",
             "regular": lambda r: "Coder fairy policy: explicit" in r and "Zed" in r})


@cell("forum_monitor_config")  # forum_monitor_block: config.json
def _c_forum(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"forum": {"url": "http://x"}}))
    return ("result = mod.forum_monitor_block()",
            {"fifo": lambda r: r == "",
             "regular": lambda r: r.startswith("[Forum mention monitor]")})


@cell("forum_updates_config")  # forum_updates_monitor_block: config.json
def _c_forum_updates(ctx):
    ctx.special(ctx.eh / "config.json", json.dumps({"forum": {"url": "http://x"}}))
    return ("result = mod.forum_updates_monitor_block()",
            {"fifo": lambda r: r == "",
             "regular": lambda r: r.startswith("[Forum updates monitor]")})


def _peer(ctx, name, host):
    (ctx.registry / f"{name}.json").write_text(
        json.dumps({"agent_name": name, "hostname": host}))


@cell("counterparts_registry_entry")  # update_counterparts_from_registry: registry read_text
def _c_cp_registry(ctx):
    _peer(ctx, "peer", socket.gethostname())
    if ctx.kind == "fifo":
        os.mkfifo(ctx.registry / "zz-fifo.json")
    (ctx.eh / "config.json").write_text(json.dumps({"agent_name": "me"}))
    return ("mod.update_counterparts_from_registry('me', P['eh'] + '/config.json', P['registry'])\n"
            "result = json.load(open(P['eh'] + '/config.json')).get('counterparts')",
            {"fifo": lambda r: r == ["peer"], "regular": lambda r: r == ["peer"]})


@cell("counterparts_config")  # update_counterparts_from_registry: config.json open
def _c_cp_config(ctx):
    _peer(ctx, "peer", socket.gethostname())
    ctx.special(ctx.eh / "config.json", json.dumps({"agent_name": "me"}))
    return ("mod.update_counterparts_from_registry('me', P['eh'] + '/config.json', P['registry'])\n"
            "import stat\n"
            "m = os.stat(P['eh'] + '/config.json').st_mode\n"
            "result = ['fifo' if stat.S_ISFIFO(m) else json.load(open(P['eh'] + '/config.json')).get('counterparts')]",
            {"fifo": lambda r: r == ["fifo"], "regular": lambda r: r == [["peer"]]})


@cell("peer_topology_registry")  # peer_topology_block: registry read_text
def _c_peer_topology(ctx):
    _peer(ctx, "peer", "some-other-host.invalid")
    if ctx.kind == "fifo":
        os.mkfifo(ctx.registry / "zz-fifo.json")
    return ("result = mod.peer_topology_block('me')",
            {"fifo": lambda r: "cross-host (forum): peer" in r,
             "regular": lambda r: "cross-host (forum): peer" in r})


@cell("starred_block_file")  # starred_block: inter-agent-starred.json read_text
def _c_starred(ctx):
    ctx.special(ctx.eh / "inter-agent-starred.json", json.dumps(
        [{"filename": "a.md", "from": "x", "title": "t"}]))
    return ("result = mod.starred_block(P['eh'])",
            {"fifo": lambda r: r == "", "regular": lambda r: "ia read a.md" in r})


@cell("history_arc_filter")  # history_arc_block: isfile() already filters a FIFO (control)
def _c_history_filter(ctx):
    ctx.special(ctx.eh / "history" / "2026-10-01.md", "hello arc")
    return ("result = mod.history_arc_block(P['eh'])",
            {"fifo": lambda r: "No history entries" in r,
             "regular": lambda r: "hello arc" in r})


@cell("history_arc_open")  # history_arc_block: the open() itself, reached past isfile
def _c_history_open(ctx):
    ctx.special(ctx.eh / "history" / "2026-10-01.md", "hello arc")
    return ("os.path.isfile = lambda p: True  # TOCTOU: file swapped after the check\n"
            "result = mod.history_arc_block(P['eh'])",
            {"fifo": lambda r: r == "", "regular": lambda r: "hello arc" in r})


@cell("tool_capability_index")  # tool_capability_index_block / short_block: <runtime>/…txt
def _c_tools(ctx):
    ctx.use_runtime_copy()
    ctx.special(ctx.rt / "tool_capability_index.txt",
                "engram_alpha: does a\nengram_beta: does b\n")
    return ("result = [mod.tool_capability_index_block(), mod.tool_capability_short_block()]",
            {"fifo": lambda r: r == ["", ""],
             "regular": lambda r: "engram_alpha: does a" in r[0] and "alpha, beta" in r[1]})


@cell("persist_injected_through")  # _persist_injected_through: marker re-read
def _c_persist(ctx):
    sid = "sess-3591"
    ctx.special(ctx.sessions / f"{sid}.json", json.dumps({"session_id": sid}))
    return (f"mod._persist_injected_through(P['eh'], {sid!r}, 'NEW')\n"
            "import stat\n"
            f"m = os.stat(P['sessions'] + '/{sid}.json').st_mode\n"
            f"result = 'fifo' if stat.S_ISFIFO(m) else json.load(open(P['sessions'] + '/{sid}.json')).get('injected_through')",
            {"fifo": lambda r: r == "fifo", "regular": lambda r: r == "NEW"})


@cell("prune_stale_markers")  # prune_stale_session_markers: marker read before delete
def _c_prune(ctx):
    name = "0123abcd-0123-4abc-8def-0123456789ab.json"
    p = ctx.sessions / name
    ctx.special(p, json.dumps({"role": "interactive"}))
    _old(p, 9)
    return (f"mod.prune_stale_session_markers('other')\n"
            f"result = os.path.lexists(P['sessions'] + '/{name}')",
            {"fifo": lambda r: r is False, "regular": lambda r: r is False})


@cell("sleep_status_marker")  # sleep_status_block: last-sleep-success.json
def _c_sleep(ctx):
    ctx.special(ctx.sessions / "last-sleep-success.json",
                json.dumps({"completed_at": "2020-01-01T00:00:00Z"}))
    return ("result = mod.sleep_status_block()",
            # unreadable marker -> "no baseline" path -> no DB -> ""
            {"fifo": lambda r: r == "",
             "regular": lambda r: r.startswith("[SLEEP STATUS: last coordinated sleep")})


def _git_repo(ctx):
    repo = ctx.tmp / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.invalid",
               HOME=str(ctx.home))
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false",
                    "commit", "-q", "-m", "c1"], check=True, env=env)
    return repo


@cell("piece_c_sidecar_write")  # piece_c_git_log: the sidecar WRITE open
def _c_sidecar(ctx):
    repo = _git_repo(ctx)
    sid = "sess-3591-sc"
    if ctx.kind == "fifo":
        os.mkfifo(ctx.sessions / f"{sid}-piece-c.txt")
    return (f"mod.PIECE_C_REPOS = [{str(repo)!r}]\n"
            f"out = mod.piece_c_git_log({sid!r})\n"
            f"p = P['sessions'] + '/{sid}-piece-c.txt'\n"
            "import stat\n"
            "result = [out.startswith('[Piece C'), stat.S_ISREG(os.stat(p).st_mode) if os.path.exists(p) else None,"
            " open(p).read() if os.path.isfile(p) else None]",
            {"fifo": lambda r: r[0] is False and r[1] is False,
             "regular": lambda r: r[0] is True and r[1] is True and "c1" in r[2]})


@cell("finalize_pending_marker")  # _apply_pending_viz_acl: ~/.engram-finalize-pending.json
def _c_finalize(ctx):
    ctx.special(ctx.home / ".engram-finalize-pending.json", json.dumps({"viz_operator": ""}))
    return ("mod._apply_pending_viz_acl(P['eh'])\n"
            "result = os.path.lexists(P['home'] + '/.engram-finalize-pending.json')",
            # existing semantics: an unreadable / unusable marker is deleted
            {"fifo": lambda r: r is False, "regular": lambda r: r is False})


def _db_with_nodes(path: Path):
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE nodes (id TEXT, type TEXT, claim TEXT, recall_summary TEXT, "
              "created_at TEXT, is_current INT, status TEXT)")
    c.execute("INSERT INTO nodes VALUES ('n-1','observation','a claim','a summary',"
              "'2026-10-02T00:00:00+00:00',1,'active')")
    c.commit()
    c.close()


@cell("sqlite_session_checkpoint")  # session_checkpoint_block: sqlite mode=ro on knowledge.db
def _c_sq_checkpoint(ctx):
    db = ctx.eh / "knowledge.db"
    if ctx.kind == "fifo":
        os.mkfifo(db)
    else:
        _db_with_nodes(db)
    return ("result = mod.session_checkpoint_block(P['eh'], '2026-10-01T00:00:00+00:00')",
            {"fifo": lambda r: r == "", "regular": lambda r: "n-1" in r})


@cell("sqlite_focus_block")  # focus_block: sqlite mode=ro on knowledge.db
def _c_sq_focus(ctx):
    if ctx.kind == "fifo":
        os.mkfifo(ctx.eh / "knowledge.db")
    else:
        (ctx.eh / "knowledge.db").write_bytes(b"")
    return ("result = mod.focus_block(P['eh'])",
            {"fifo": lambda r: r == "", "regular": lambda r: r == ""})


@cell("sqlite_saved_focus_sets")  # saved_focus_sets_block: sqlite mode=ro
def _c_sq_sets(ctx):
    if ctx.kind == "fifo":
        os.mkfifo(ctx.eh / "knowledge.db")
    else:
        (ctx.eh / "knowledge.db").write_bytes(b"")
    return ("result = mod.saved_focus_sets_block(P['eh'])",
            {"fifo": lambda r: r == "", "regular": lambda r: r == ""})


@cell("sqlite_embedding_restore_flag")  # _check_embedding_restore_flag: sqlite mode=ro
def _c_sq_embed(ctx):
    if ctx.kind == "fifo":
        os.mkfifo(ctx.eh / "knowledge.db")
    else:
        (ctx.eh / "knowledge.db").write_bytes(b"")
    return ("result = list(mod._check_embedding_restore_flag(P['eh']))",
            # #3628 slice 2d: the precheck is now os.path.exists, so a FIFO
            # reaches connect_regular_ro and is refused into the existing
            # sqlite3.OperationalError handler -> UNKNOWN (None, ...), no longer
            # "absent -> healthy". Deliberate, non-regular targets only.
            # An empty regular file has no table -> unknown (None, ...) as today.
            {"fifo": lambda r: r[0] is None and "not a regular file" in r[1],
             "regular": lambda r: r[0] in (True, None)})


@pytest.mark.parametrize("kind", ["fifo", "regular"])
@pytest.mark.parametrize("name", sorted(CELLS))
def test_cell(tmp_path, name, kind):
    ctx = Ctx(tmp_path, kind)
    code, expect = CELLS[name](ctx)
    result = _run(ctx, code)
    assert expect[kind](result), f"{name}/{kind}: unexpected result {result!r}"


# --- end to end: the real script with a FIFO at every path only THIS hook reads ---
#
# config.json and knowledge.db are deliberately absent from this list: main()
# also calls into other modules (engram_log_emitter, and engram_core via the
# stats call) that open them with a plain open() / sqlite3.connect. That is
# outside this file and tracked separately; the per-function cells above cover
# those two paths inside the SessionStart hook itself.

_STDIN = json.dumps({"session_id": "sess-3591-main", "transcript_path": "/tmp/t3591.jsonl",
                     "source": "startup", "cwd": "/tmp"})


def _run_script(ctx: Ctx, timeout=40):
    env = dict(os.environ)
    env.update({"HOME": str(ctx.home), "ENGRAM_HOME": str(ctx.eh),
                "AGENTS_SHARED_DIR": str(ctx.shared), "ENGRAM_PIECE_C_REPOS": "",
                "ENGRAM_SESSION_ROLE": "interactive"})
    env.pop("ENGRAM_RUNTIME_DIR", None)
    try:
        return subprocess.run([sys.executable, str(HOOK_DIR / HOOK_NAME)], input=_STDIN,
                              capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"SessionStart script blocked for more than {timeout}s (kind={ctx.kind})")


@pytest.mark.parametrize("kind", ["fifo", "regular"])
def test_main_with_hook_only_paths(tmp_path, kind):
    ctx = Ctx(tmp_path, kind)
    for rel in ("mcp-tools-ready.json", "inter-agent-starred.json",
                "sessions/last-sleep-success.json"):
        ctx.special(ctx.eh / rel, "{}")
    ctx.special(ctx.home / ".engram-finalize-pending.json", "{}")
    _peer(ctx, "peer", "h.invalid")
    if kind == "fifo":
        os.mkfifo(ctx.registry / "zz.json")
    done = _run_script(ctx)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)  # the hook still emits its JSON response


# --- the shared helpers keep open()'s exception types ---

_HELPERS = f"""
import sys, os, json
sys.path.insert(0, {str(HOOK_DIR)!r})
import _hooklib as h
"""


def _helper_child(tmp_path, body):
    try:
        done = subprocess.run([sys.executable, "-c", _HELPERS + body, str(tmp_path)],
                              capture_output=True, text=True, timeout=GUARD_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"helper blocked for more than {GUARD_S}s")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_helpers_exception_types_match_open(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "r.txt").write_text("héllo", encoding="utf-8")
    os.mkfifo(tmp_path / "f")
    out = _helper_child(tmp_path, r"""
root = sys.argv[1]
def kind(fn, *a, **k):
    try:
        r = fn(*a, **k)
        r.close()
        return "ok"
    except OSError as e:
        return type(e).__name__
print(json.dumps({
  "missing": kind(h.open_regular_text, root + "/nope"),
  "dir": kind(h.open_regular_text, root + "/d"),
  "fifo": kind(h.open_regular_text, root + "/f"),
  "regular": kind(h.open_regular_text, root + "/r.txt"),
  "text": h.read_regular_text(root + "/r.txt", encoding="utf-8"),
  "w_fifo_noreader": kind(h.open_regular_text_write, root + "/f"),
  "w_dir": kind(h.open_regular_text_write, root + "/d"),
}))
""")
    assert out["missing"] == "FileNotFoundError"
    assert out["dir"] == "IsADirectoryError"
    assert out["fifo"] == "OSError"
    assert out["regular"] == "ok"
    assert out["text"] == "héllo"
    assert out["w_fifo_noreader"] == "OSError"
    assert out["w_dir"] == "IsADirectoryError"


def test_write_helper_truncates_and_creates_like_open(tmp_path):
    out = _helper_child(tmp_path, r"""
root = sys.argv[1]
open(root + "/w.txt", "w").write("old old old")
with h.open_regular_text_write(root + "/w.txt", encoding="utf-8") as f:
    f.write("new")
with h.open_regular_text_write(root + "/fresh.txt", encoding="utf-8") as f:
    f.write("x")
print(json.dumps([open(root + "/w.txt").read(), open(root + "/fresh.txt").read()]))
""")
    assert out == ["new", "x"]


# --- the write helper's regular-file check is reached only when os.open SUCCEEDS
# on a non-regular target. A reader-less FIFO fails at os.open (ENXIO) and a
# directory at os.open (EISDIR), so neither pins the fstat check. These do.

def _skip_unless_dev_null_chr():
    import stat
    try:
        if not stat.S_ISCHR(os.stat("/dev/null").st_mode):
            pytest.skip("/dev/null is not a character device here")
    except OSError:
        pytest.skip("/dev/null is absent")


def test_write_helper_rejects_fifo_with_reader_and_writes_nothing(tmp_path):
    os.mkfifo(tmp_path / "f")
    out = _helper_child(tmp_path, r"""
import errno
root = sys.argv[1]
rfd = os.open(root + "/f", os.O_RDONLY | os.O_NONBLOCK)
err = None
got = "unset"
try:
    try:
        h.open_regular_text_write(root + "/f", encoding="utf-8").close()
        err = "no-error"
    except OSError as e:
        err = e.errno
    try:
        got = os.read(rfd, 4096).decode()
    except BlockingIOError:
        got = ""
finally:
    os.close(rfd)
print(json.dumps({"errno": err, "einval": errno.EINVAL, "got": got}))
""")
    assert out["errno"] == out["einval"], out
    assert out["got"] == "", out  # the reader received zero bytes


def test_write_helper_rejects_dev_null(tmp_path):
    _skip_unless_dev_null_chr()
    out = _helper_child(tmp_path, r"""
import errno
try:
    h.open_regular_text_write("/dev/null", encoding="utf-8").close()
    err = "no-error"
except OSError as e:
    err = e.errno
print(json.dumps({"errno": err, "einval": errno.EINVAL}))
""")
    assert out["errno"] == out["einval"], out

