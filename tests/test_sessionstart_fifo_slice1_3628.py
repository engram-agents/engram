"""#3628 slice 1: a whole SessionStart survives a FIFO at config.json / knowledge.db
and at the other ENGRAM_HOME inputs main() reaches through core code.

#3627 (closes #3591) made every open inside the SessionStart hook FILE
non-blocking. Its end-to-end `main` cell had to be narrowed because main() also
runs core code: the hook builds an in-process `EngramClient` (the engram_stats
tool is called on the SERVER code in this very process, there is no subprocess
and no MCP hop), and that code, plus the log emitter, read config.json and a
few markers with a plain open().

Measured stacks that blocked at dev (FIFO at the path, faulthandler dump):

  config.json    main -> EngramClient.call -> server.engram_stats
                 -> engram_stats._stats_impl -> engram_core._get_current_turn
                 -> engram_restamp.read_memory_config
                 -> engram_config_io.read_config_json (Path.read_text)
                 (and, on a DB without seed nodes, _get_db -> read_config_json)
  config.json    engram_log_emitter.Emitter._read_turn_from_db (open)
  .substrate-degraded.json
                 main -> ... -> _stats_impl -> engram_core._walguard_degraded_banner
                 -> engram_walguard.read_degraded_marker (Path.read_text)
  knowledge.db   did NOT block: sqlite3 fails fast with "disk I/O error" on a FIFO.

Each cell drives the real function in a SUBPROCESS with a hard timeout, so a
regression fails (timeout) instead of hanging the suite. Every cell runs as
kind="fifo" (documented fallback) and kind="regular" (positive control).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC / "hooks" / "claude"
HOOK_NAME = "engram-session-start-hook.py"

GUARD_S = int(os.environ.get("SESSIONSTART_3628_GUARD_S", "8"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")

_PRELUDE = f"""
import sys, os, json
sys.path.insert(0, {str(HOOK_DIR)!r})
sys.path.insert(0, {str(SRC)!r})
ROOT = sys.argv[1]
EH = os.path.join(ROOT, ".engram")
"""


def _child(tmp: Path, body: str, timeout=GUARD_S):
    """Run `body` in a child python; it sets `result`, which comes back as JSON."""
    eh = tmp / ".engram"
    eh.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(tmp), ENGRAM_HOME=str(eh))
    env.pop("ENGRAM_RUNTIME_DIR", None)
    code = _PRELUDE + body + "\nprint('RESULT:' + json.dumps(result))\n"
    try:
        done = subprocess.run([sys.executable, "-c", code, str(tmp)],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"blocked for more than {timeout}s on a non-regular file")
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


def _put(tmp: Path, rel: str, kind: str, regular_text: str):
    p = tmp / ".engram" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if kind == "fifo":
        os.mkfifo(p)
    else:
        p.write_text(regular_text)
    return p


KINDS = ["fifo", "regular"]


# --------------------------------------------------------------------------
# core site: engram_config_io.read_config_json
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_read_config_json(tmp_path, kind):
    _put(tmp_path, "config.json", kind, json.dumps({"memory": {"current_turn": 7}}))
    r = _child(tmp_path, r"""
import engram_config_io as c
try:
    cfg = c.read_config_json(os.path.join(EH, "config.json"))
    result = ["ok", cfg]
except OSError as e:
    result = ["oserror", type(e).__name__]
""")
    if kind == "fifo":
        assert r[0] == "oserror"
    else:
        assert r == ["ok", {"memory": {"current_turn": 7}}]


def test_read_config_json_missing_is_still_empty(tmp_path):
    r = _child(tmp_path, r"""
import engram_config_io as c
result = c.read_config_json(os.path.join(EH, "config.json"))
""")
    assert r == {}


# --------------------------------------------------------------------------
# core site: engram_walguard.read_degraded_marker  (handler: except Exception)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_read_degraded_marker(tmp_path, kind):
    _put(tmp_path, ".substrate-degraded.json", kind, json.dumps({"detected_at": "t0"}))
    r = _child(tmp_path, r"""
import engram_walguard as w
result = w.read_degraded_marker(EH)
""")
    assert r == (None if kind == "fifo" else {"detected_at": "t0"})


# --------------------------------------------------------------------------
# core site: engram_log_emitter config.json read  (handler: except Exception)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_emitter_reads_turn_from_config(tmp_path, kind):
    _put(tmp_path, "config.json", kind, json.dumps({"memory": {"current_turn": 41}}))
    r = _child(tmp_path, r"""
from pathlib import Path
import engram_log_emitter as e
em = e.Emitter("sid-3628", "/tmp/t.jsonl", None, Path(EH) / "logs")
result = em._turn
""")
    assert r == (None if kind == "fifo" else 41)


# --------------------------------------------------------------------------
# core site: engram_log_emitter append of its log target (emit() catches all)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_emitter_append_target(tmp_path, kind):
    sessions = tmp_path / ".engram" / "logs" / "sessions"
    sessions.mkdir(parents=True)
    target = sessions / "sid-3628.l1.jsonl"
    if kind == "fifo":
        os.mkfifo(target)
    r = _child(tmp_path, r"""
import stat
from pathlib import Path
import engram_log_emitter as e
em = e.Emitter("sid-3628", "/tmp/t.jsonl", 3, Path(EH) / "logs")
uuid = em.emit("engram.test.slice", 1, {"k": "v"})
t = os.path.join(EH, "logs", "sessions", "sid-3628.l1.jsonl")
result = [uuid is not None, stat.S_ISREG(os.stat(t).st_mode),
          open(t).read() if stat.S_ISREG(os.stat(t).st_mode) else None]
""")
    if kind == "fifo":
        assert r == [False, False, None]  # emit() swallowed the error: no hang, no write
    else:
        assert r[0] is True and r[1] is True and '"engram.test.slice"' in r[2]


# --------------------------------------------------------------------------
# hook-side site: _hooklib.session_started_at  (handler: except Exception)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_session_started_at(tmp_path, kind):
    _put(tmp_path, "sessions/sid-3628.json", kind,
         json.dumps({"started_at": "2026-10-01T00:00:00Z"}))
    r = _child(tmp_path, r"""
import _hooklib as h
d = h.session_started_at("sid-3628")
result = d.isoformat() if d else None
""")
    assert r == (None if kind == "fifo" else "2026-10-01T00:00:00+00:00")


# --------------------------------------------------------------------------
# the core helper module (the hook-side copy is pinned by the parity test)
# --------------------------------------------------------------------------

def test_core_module_does_not_import_from_hooks():
    import ast
    tree = ast.parse((SRC / "engram_regular_io.py").read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert not ({"_hooklib", "hooks", "context_tracker"} & mods)
    assert mods <= {"__future__", "errno", "os", "stat"}


def test_append_regular_file_creates_appends_and_honours_umask(tmp_path):
    r = _child(tmp_path, r"""
import stat
import engram_regular_io as c
os.umask(0o022)
p = os.path.join(ROOT, "a.txt")
with c.open_regular_text_append(p, encoding="utf-8") as f:
    f.write("one\n")
with c.open_regular_text_append(p, encoding="utf-8") as f:
    f.write("two\n")
# the fd carries O_APPEND
f = c.open_regular_text_append(p, encoding="utf-8")
import fcntl
flags_ok = bool(fcntl.fcntl(f.fileno(), fcntl.F_GETFL) & os.O_APPEND)
f.close()
result = [open(p).read(), oct(stat.S_IMODE(os.stat(p).st_mode)), flags_ok]
""")
    assert r == ["one\ntwo\n", "0o644", True]


def test_append_helper_fifo_without_reader_fails_fast(tmp_path):
    os.mkfifo(tmp_path / "f")
    r = _child(tmp_path, r"""
import engram_regular_io as c, errno
try:
    c.open_regular_text_append(os.path.join(ROOT, "f"), encoding="utf-8").close()
    result = "no-error"
except OSError as e:
    result = [e.errno, errno.ENXIO]
""")
    assert r == [r[1], r[1]]  # ENXIO from os.open(O_NONBLOCK), not a hang


def test_append_helper_rejects_fifo_with_reader_and_writes_nothing(tmp_path):
    os.mkfifo(tmp_path / "f")
    r = _child(tmp_path, r"""
import engram_regular_io as c, errno
rfd = os.open(os.path.join(ROOT, "f"), os.O_RDONLY | os.O_NONBLOCK)
try:
    try:
        c.open_regular_text_append(os.path.join(ROOT, "f"), encoding="utf-8").close()
        err = "no-error"
    except OSError as e:
        err = e.errno
    try:
        got = os.read(rfd, 4096).decode()
    except BlockingIOError:
        got = ""
finally:
    os.close(rfd)
result = {"errno": err, "einval": errno.EINVAL, "got": got}
""")
    assert r["errno"] == r["einval"], r
    assert r["got"] == "", r


def test_append_helper_rejects_directory_and_dev_null(tmp_path):
    (tmp_path / "d").mkdir()
    r = _child(tmp_path, r"""
import engram_regular_io as c, errno
out = {}
for name, p in (("dir", os.path.join(ROOT, "d")), ("null", "/dev/null")):
    try:
        c.open_regular_text_append(p, encoding="utf-8").close()
        out[name] = "no-error"
    except OSError as e:
        out[name] = e.errno
out["eisdir"], out["einval"] = errno.EISDIR, errno.EINVAL
result = out
""")
    assert r["dir"] == r["eisdir"], r
    assert r["null"] == r["einval"], r


# --------------------------------------------------------------------------
# end to end: a WHOLE SessionStart main() with a FIFO at the path
# --------------------------------------------------------------------------

_STDIN = json.dumps({"session_id": "sess-3628-main", "transcript_path": "/tmp/t3628.jsonl",
                     "source": "startup", "cwd": "/tmp"})


def _run_script(tmp: Path, timeout=60):
    home = tmp / "home"
    eh = home / ".engram"
    (eh / "sessions").mkdir(parents=True, exist_ok=True)
    shared = tmp / "shared"
    (shared / "agents").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"HOME": str(home), "ENGRAM_HOME": str(eh), "AGENTS_SHARED_DIR": str(shared),
                "ENGRAM_PIECE_C_REPOS": "", "ENGRAM_SESSION_ROLE": "interactive"})
    env.pop("ENGRAM_RUNTIME_DIR", None)
    return eh, env


@pytest.mark.parametrize("rel", ["config.json", "knowledge.db", ".substrate-degraded.json",
                                 "principle_triggers.json"])
@pytest.mark.parametrize("kind", KINDS)
def test_main_survives_special_file_at(tmp_path, rel, kind):
    eh, env = _run_script(tmp_path)
    p = eh / rel
    if kind == "fifo":
        os.mkfifo(p)
    elif rel == "config.json":
        p.write_text("{}")
    elif rel in (".substrate-degraded.json", "principle_triggers.json"):
        p.write_text("{}")
    # (a regular knowledge.db is left to the hook to create: that is the control)
    try:
        done = subprocess.run([sys.executable, str(HOOK_DIR / HOOK_NAME)], input=_STDIN,
                              capture_output=True, text=True, timeout=40, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"SessionStart main() blocked for more than 40s (rel={rel}, kind={kind})")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)  # the hook still emits its JSON response
