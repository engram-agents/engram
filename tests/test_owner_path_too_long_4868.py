"""#4868 (part of #2652, unit 03) -- an over-long owner socket path is its own Unavailable reason,
`path_too_long`, not `down`.

Why: `down` tells an owner-read caller "nothing is listening, a direct `mode=ro` open is safe". An over-long
path says nothing of the kind. An owner started under a SHORTER alias of the same ENGRAM_HOME (a symlink) is
live, while a client resolving the longer alias computes a socket path the kernel cannot address. Read as
`down`, that client opened the store directly beside the live owner.

Contract (each cell written before the change):

  K1  the client: an over-long path gives Unavailable(reason="path_too_long") for read and write, with the
      same detail text as before, no socket opened, no owner pid, and no restart-the-owner step; a path that
      fits with nothing listening is still `down` (control)
  K1b `path_too_long` is in UNAVAILABLE_REASONS and has its own remedy row, distinct from `down`'s
  K2  store_read, the alias case end to end: a live owner on the short spelling, ENGRAM_HOME the long
      spelling, flag ON -> exit 3, `refused` naming path_too_long, ZERO direct opens, and the owner was never
      asked (the client sent nothing). Positive control: the same alias with the flag OFF opens directly once.
  K3  install_selfcheck, the same alias case: both graph checks FAIL naming path_too_long, ZERO direct opens
  K4  _owner_read.write: path_too_long says nothing was written (not INDETERMINATE), names the code, and tags
      owner_reason="path_too_long", owner_commit=None
  K4b _owner_read.snapshot: path_too_long says nothing was archived (not INDETERMINATE) and names the code
  K5  a DEAD owner (a stale socket file, a pidfile naming a dead pid) over an UNCHECKPOINTED WAL: the ping is
      `down`, and the direct `mode=ro` read of each caller sees the rows committed only in the WAL
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path
from urllib.parse import unquote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import engramd_client as ec  # noqa: E402

from tools import _owner_read as owner_read  # noqa: E402

STORE_READ = SRC / "tools" / "store_read.py"
SELFCHECK = SRC / "tools" / "install_selfcheck.py"
SR_FLAG = "store_read_reads_via_owner"
ISC_FLAG = "install_selfcheck_reads_via_owner"
SOCKET_TAIL = "/" + ec.SOCKET_DIR_NAME + "/" + ec.DEFAULT_SOCKET_NAME
RESTART_STEP_PREFIX = "Check whether the engramd process is running and (re)start it"


# ------------------------------------------------------------------------------------------ fixtures

def _mkdtemp(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp")).resolve()


@pytest.fixture
def dirs():
    """(real, alias): `real` is a short ENGRAM_HOME whose socket path fits; `alias` is a symlink to it whose
    socket path is one byte over the limit. Cleanup removes both trees."""
    real = _mkdtemp("ptl-")
    holder = _mkdtemp("ptl-alias-")
    pad = ec.SOCKET_PATH_MAX_BYTES + 1 - len(os.fsencode(str(holder))) - 1 - len(SOCKET_TAIL)
    if pad < 1 or pad > 255:
        shutil.rmtree(real, ignore_errors=True)
        shutil.rmtree(holder, ignore_errors=True)
        pytest.skip("the tmp base is too long to build a one-byte-over alias here (pad %d)" % pad)
    alias = holder / ("a" * pad)
    alias.symlink_to(real, target_is_directory=True)
    try:
        yield real, alias
    finally:
        shutil.rmtree(holder, ignore_errors=True)
        shutil.rmtree(real, ignore_errors=True)


def test_the_fixture_builds_exactly_the_alias_case(dirs):
    real, alias = dirs
    assert ec.socket_path_too_long_reason(ec.socket_path_for_home(real)) is None
    assert len(os.fsencode(str(ec.socket_path_for_home(alias)))) == ec.SOCKET_PATH_MAX_BYTES + 1
    assert os.path.realpath(alias) == str(real)


def _set_flag(home: Path, key: str, value=True):
    (home / "config.json").write_text(json.dumps({"engramd": {key: value}}), encoding="utf-8")


# Every sqlite3.connect of the watched store made by the thread running the tool in this process.
_OPENS = []
_WATCH = {"store": None, "thread": None}


def _audit(event, args):
    if event == "sqlite3.connect" and _WATCH["store"] and threading.get_ident() == _WATCH["thread"]:
        target = str(args[0])
        bare = unquote(target[5:].split("?", 1)[0]) if target.startswith("file:") else target
        if os.path.realpath(bare) == _WATCH["store"]:
            _OPENS.append(target)


sys.addaudithook(_audit)


# ------------------------------------------------------------------------------------------ K1, K1b

def _no_socket(*a, **k):
    raise AssertionError("the client opened a socket for an over-long path")


def test_k1_an_over_long_path_is_path_too_long_for_read_and_write(dirs, monkeypatch):
    _real, alias = dirs
    sp = ec.socket_path_for_home(alias)
    monkeypatch.setattr(ec.socket, "socket", _no_socket)
    for res in (ec.read("ping", {}, timeout=1.0, socket_path=sp), ec.write("noop", {}, timeout=1.0, socket_path=sp)):
        assert isinstance(res, ec.Unavailable), res
        assert res.reason == "path_too_long", res
        assert res.detail == ec.socket_path_too_long_reason(sp)
        assert res.owner_pid is None
        steps = list(res.agent_steps) + list(res.human_steps)
        assert not any(s.startswith(RESTART_STEP_PREFIX) for s in steps), steps
        assert any("ENGRAM_HOME" in s for s in res.agent_steps), res.agent_steps


def test_k1_control_a_path_that_fits_with_nothing_listening_is_still_down(dirs):
    real, _alias = dirs
    res = ec.read("ping", {}, timeout=1.0, socket_path=ec.socket_path_for_home(real))
    assert isinstance(res, ec.Unavailable) and res.reason == "down", res


def test_k1b_path_too_long_is_a_reason_with_its_own_remedy_row():
    assert "path_too_long" in ec.UNAVAILABLE_REASONS
    assert "path_too_long" in ec._REMEDY_STEPS
    assert ec._REMEDY_STEPS["path_too_long"] != ec._REMEDY_STEPS["down"]


# ------------------------------------------------------------------------------------------ K2: store_read

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sr_store(home: Path, ids=("obs_0001", "obs_0002")) -> Path:
    db = home / "knowledge.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, claim TEXT, "
                "is_current INTEGER DEFAULT 1, status TEXT DEFAULT 'active', created_at TEXT)")
    for nid in ids:
        con.execute("INSERT INTO nodes (id, type, claim, created_at) VALUES (?, 'observation_factual', 'c', "
                    "'2026-01-01T00:00:00')", (nid,))
    con.commit()
    con.close()
    return db


def _run_store_read(home: Path, argv, capsys, monkeypatch):
    """(exit code, stdout, stderr, direct opens of the store) of one in-process store_read run with
    ENGRAM_HOME spelled `home`."""
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    mod = _load(STORE_READ, "store_read_4868")
    del _OPENS[:]
    _WATCH["store"] = os.path.realpath(str(home / "knowledge.db"))
    _WATCH["thread"] = threading.get_ident()
    capsys.readouterr()
    try:
        try:
            rc = mod.main(list(argv))
        except SystemExit as exc:
            rc = exc.code
    finally:
        _WATCH["store"] = None
    out = capsys.readouterr()
    return (0 if rc is None else rc), out.out, out.err, list(_OPENS)


def _sr_lines(err):
    return [ln for ln in err.splitlines() if ln.startswith("store_read:")]


def test_k2_store_read_refuses_beside_a_live_owner_on_a_shorter_alias(dirs, capsys, monkeypatch):
    real, alias = dirs
    _sr_store(real)
    _set_flag(real, SR_FLAG)
    with FakeOwner(real) as owner:
        rc, out, err, opens = _run_store_read(alias, ["ids", "--since", ""], capsys, monkeypatch)
        asked = list(owner.requests)
    assert rc == 3 and out == "", (rc, out, err)
    lines = _sr_lines(err)
    assert len(lines) == 1 and lines[0].startswith("store_read: refused (") and "path_too_long" in lines[0], err
    assert opens == [], "a live owner listens on the short spelling: no direct open"
    assert asked == [], "the client sends nothing on an over-long path"


def test_k2_positive_control_the_same_alias_flag_off_opens_directly_once(dirs, capsys, monkeypatch):
    real, alias = dirs
    _sr_store(real)
    _set_flag(real, SR_FLAG, False)
    rc, out, err, opens = _run_store_read(alias, ["ids", "--since", ""], capsys, monkeypatch)
    assert rc == 0, err
    assert out.splitlines() == ["obs_0001", "obs_0002"]
    assert len(opens) == 1, "the instrument counts a direct open through the alias"


# ------------------------------------------------------------------------------------------ K3: install_selfcheck

ISC_RUNNER = textwrap.dedent('''
    import importlib.util, json, os, sys
    from pathlib import Path
    from urllib.parse import unquote
    store = os.path.realpath(sys.argv[3])
    opens = []
    def hook(event, args):
        if event == "sqlite3.connect":
            target = str(args[0])
            if target.startswith("file:"):
                target = unquote(target[5:].split("?", 1)[0])
            if os.path.realpath(target) == store:
                opens.append(target)
    sys.addaudithook(hook)
    spec = importlib.util.spec_from_file_location("install_selfcheck_4868", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = Path(sys.argv[2])
    ctx = mod.Ctx("installed", home, home, home / "claude", 5)
    checks = [(n, f) for n, f in mod.PHASE_CHECKS["installed"] if n in ("graph-opens", "seed-ids-present")]
    results = mod.run_checks(checks, ctx)
    print(json.dumps({"results": results, "opens": opens}))
''')

SEED_IDS = {"AX_HONESTY": "ax_0001", "AX_PROVENANCE": "ax_0002", "AX_DISCRETION": "ax_0003",
            "DF_ENGRAM": "df_0001", "DF_EPISTEMIC": "df_0002", "GL_HUMILITY": "gl_0001"}
SEED_NODES = [("ax_0001", "axiom"), ("ax_0002", "axiom"), ("ax_0003", "axiom"),
              ("df_0001", "definition"), ("df_0002", "definition"), ("gl_0001", "goal")]


def _isc_store(home: Path, nodes) -> Path:
    import engram_core
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    engram_core._run_db_one_time_setup(conn)
    conn.row_factory = None
    for nid, ntype in nodes:
        conn.execute("INSERT INTO nodes (id, type, claim, created_at) VALUES (?, ?, 'a claim', '2026-01-01T00:00:00')",
                     (nid, ntype))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    (home / "seed-manifest.json").write_text(json.dumps({"seed_ids": SEED_IDS}))
    return db


def _run_selfcheck(home: Path):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    r = subprocess.run([sys.executable, "-c", ISC_RUNNER, str(SELFCHECK), str(home), str(home / "knowledge.db")],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_k3_install_selfcheck_refuses_beside_a_live_owner_on_a_shorter_alias(dirs):
    real, alias = dirs
    _isc_store(real, SEED_NODES)
    _set_flag(real, ISC_FLAG)
    with FakeOwner(real) as owner:
        out = _run_selfcheck(alias)
        asked = list(owner.requests)
    statuses = {r["name"]: r["status"] for r in out["results"]}
    assert statuses == {"graph-opens": "FAIL", "seed-ids-present": "FAIL"}, out
    assert "path_too_long" in out["results"][0]["detail"], out["results"][0]
    assert out["opens"] == [], "a live owner listens on the short spelling: no direct open"
    assert asked == [], "the client sends nothing on an over-long path"


# ------------------------------------------------------------------------------------------ K4, K4b: _owner_read

class Err(Exception):
    pass


def _spec(home=Path("/h")):
    return owner_read.OwnerReadSpec(flag_key="k", error_cls=Err, home=home, never_empty="")


def _un(reason):
    return ec.Unavailable(socket_path="x", bound=1.0, agent_steps=["a"], human_steps=["h"], detail="d", reason=reason)


def test_k4_write_path_too_long_reached_nothing_and_is_tagged_so(monkeypatch):
    monkeypatch.setattr(ec, "write", lambda *a, **k: _un("path_too_long"))
    with pytest.raises(Err) as e:
        owner_read.write(_spec(), "m", {}, request_id="r")
    msg = str(e.value)
    assert "path_too_long" in msg and "nothing was written" in msg, msg
    assert "INDETERMINATE" not in msg, msg
    assert (e.value.owner_reason, e.value.owner_commit) == ("path_too_long", None)


def test_k4b_snapshot_path_too_long_reached_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "read", lambda *a, **k: _un("path_too_long"))
    with pytest.raises(Err) as e:
        owner_read.snapshot(_spec(tmp_path), tmp_path / "snap.db")
    msg = str(e.value)
    assert "path_too_long" in msg and "nothing was archived" in msg, msg
    assert "INDETERMINATE" not in msg, msg


# ------------------------------------------------------------------------------------------ K5: dead owner, WAL

WAL_WRITER = textwrap.dedent('''
    import os, sqlite3, sys
    con = sqlite3.connect(sys.argv[1])
    assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute(sys.argv[2], tuple(sys.argv[3:]))
    con.commit()
    os._exit(0)  # die like a killed owner: no close, so no checkpoint and the -wal stays
''')


def _die_over_a_wal(home: Path, sql: str, *args):
    """A process commits `sql` into the WAL and dies without a checkpoint; then the dead owner's traces are
    left the way a killed owner leaves them: its socket file (bound, never listening) and its pidfile."""
    p = subprocess.Popen([sys.executable, "-c", WAL_WRITER, str(home / "knowledge.db"), sql, *args],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _out, errout = p.communicate(timeout=60)
    assert p.returncode == 0, errout
    wal = Path(str(home / "knowledge.db") + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "precondition: the committed rows sit in an uncheckpointed WAL"
    sp = ec.socket_path_for_home(home)
    sp.parent.mkdir(mode=0o700, exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sp))
    s.close()
    ec.pid_path_for_home(home).write_text("%d\n" % p.pid)
    assert ec.read("ping", {}, timeout=1.0, socket_path=sp).reason == "down", "precondition: the dead owner reads down"


def test_k5_store_read_direct_ro_sees_rows_only_in_a_dead_owners_wal(capsys, monkeypatch):
    home = _mkdtemp("ptl-wal-")
    try:
        _sr_store(home)
        _set_flag(home, SR_FLAG)
        _die_over_a_wal(home, "INSERT INTO nodes (id, type, claim, created_at) VALUES "
                              "(?, 'observation_factual', 'c', '2026-01-02T00:00:00')", "obs_wal1")
        rc, out, err, opens = _run_store_read(home, ["ids", "--since", ""], capsys, monkeypatch)
        assert rc == 0, err
        assert out.splitlines() == ["obs_0001", "obs_0002", "obs_wal1"], out
        assert _sr_lines(err) == ["store_read: path=direct-ro (owner down)"], err
        assert len(opens) == 1
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_k5_install_selfcheck_direct_ro_sees_rows_only_in_a_dead_owners_wal():
    home = _mkdtemp("ptl-wal-")
    try:
        _isc_store(home, SEED_NODES[:-1])  # gl_0001 is committed only in the WAL below
        _set_flag(home, ISC_FLAG)
        _die_over_a_wal(home, "INSERT INTO nodes (id, type, claim, created_at) VALUES "
                              "(?, 'goal', 'a claim', '2026-01-01T00:00:00')", "gl_0001")
        out = _run_selfcheck(home)
        statuses = {r["name"]: r["status"] for r in out["results"]}
        assert statuses == {"graph-opens": "PASS", "seed-ids-present": "PASS"}, out
        assert "(path: direct (no live owner: down))" in out["results"][0]["detail"], out["results"][0]
        assert out["opens"], "the dead owner reads down: the direct read-only open ran"
    finally:
        shutil.rmtree(home, ignore_errors=True)
