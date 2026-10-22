"""#3882 (U19 of #2652): the offline repair tools and the engramd owner exclude each other through ONE
store-maintenance lock, `<db>.maint.lockdir`, built from the #3791 setup-lock primitive (generalised in
place: no second implementation). Contract cells, written BEFORE the implementation.

The contract (the issue's six cells, plus the design seat's two rulings):

  C1  exclusion both ways, REAL processes: with a real owner running, each of the six tools exits non-zero
      before ANY store open (opens are counted by an audit-hook `sitecustomize`; the same tool with no
      owner is the positive control and logs >= 1 open); with a tool holding the lock, a real owner start
      exits 6 and touches nothing; with an owner holding it, a second owner start exits 1.
  C2  no race window: a tool is paused inside its write transaction; a real owner started meanwhile exits
      6; the tool's write then completes and is intact.
  C3  crash safety: `kill -9` a tool that holds the lock; an owner then starts cleanly.
  C4  co-tenant: another uid cannot block the lock or plant the directory (the #3791 cells, against the new
      name). The cell that needs a second real account is skipped, with the reason, as #3791 did.
  C5  read-only paths refuse too while an owner runs: `cites_step3`'s `mode=ro` paths, the
      `migrate_person_nodes` report.
  C7  setup-lock parity: the generalised primitive is still ONE implementation, and the setup wrapper keeps
      its signature (the existing setup-lock test files run UNCHANGED; they are run, not re-pinned here).
  C8  holder-role race: lock held but no holder file yet -> owner exit 6 (not 1); once the holder file
      says `owner`, a retry exits 1.
  (C6, the ledger completeness cell, lives in test_2652_exclusive_maintenance_completeness_3882.py.)

Real-process cells run the shipped owner script and the shipped tool scripts as child processes against a
real-schema store in a throwaway ENGRAM_HOME. No node id of any real graph appears in this file.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import inspect
import json
import logging
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
OWNER_PY = SRC / "engramd_owner.py"
TOOLS = SRC / "tools"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import engram_core as core  # noqa: E402
import engramd_owner as eo  # noqa: E402

PY = sys.executable
SIX = (
    "ack_recompute_sweep",
    "cites_step3",
    "migrate_person_nodes",
    "migrate_prediction_pairing_edges",
    "migrate_stranded_supported_by",
    "surgical",
)
BUG_REF = "ob" + "_" + "9999"  # shape only; this file carries no real node id


# ---------------------------------------------------------------------------
# throwaway homes, stores, and the two instruments
# ---------------------------------------------------------------------------

@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="u19-"))  # short: the owner's AF_UNIX path must fit
    os.chmod(d, 0o700)
    try:
        yield d
    finally:
        os.chmod(d, 0o700)
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ctl():
    d = Path(tempfile.mkdtemp(prefix="u19c-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_user_home():
    d = Path(tempfile.mkdtemp(prefix="u19h-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


_BUILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, os.environ["U19_SRC"])
    import engram_core as c
    c._configure_paths(os.environ["ENGRAM_HOME"])
    c._get_db().close()
    """
)

# The "zero opens" instrument. An audit hook sees EVERY sqlite3.connect in the process, whichever spelling
# the tool uses; opens of the store (a path naming knowledge.db) are appended to U19_OPEN_LOG. With
# U19_PAUSE_MARKER set, the first commit of a knowledge.db connection pauses INSIDE the write transaction.
_SITECUSTOMIZE = textwrap.dedent(
    """
    import os, sys, sqlite3, time
    _log = os.environ.get("U19_OPEN_LOG")
    _stall = os.environ.get("U19_STALL_AT_OPEN")
    if _log or _stall:
        def _hook(ev, args):
            if ev == "sqlite3.connect":
                try:
                    if _log:
                        with open(_log, "a") as f:
                            f.write(str(args[0]) + "\\n")
                    if _stall and "knowledge.db" in str(args[0]) and not os.path.exists(_stall):
                        # the audit event fires BEFORE the open: stall here, so a cell can look at the lock
                        # at the very moment of the tool's first open of the store
                        open(_stall, "w").close()
                        time.sleep(30)
                except Exception:
                    pass
        sys.addaudithook(_hook)
    _pause = os.environ.get("U19_PAUSE_MARKER")
    if _pause:
        _real = sqlite3.connect
        class _PauseConn(sqlite3.Connection):
            def commit(self):
                if not os.path.exists(_pause):
                    open(_pause, "w").close()
                    time.sleep(float(os.environ.get("U19_PAUSE_S", "8")))
                return super().commit()
        def _connect(*a, **k):
            k.setdefault("factory", _PauseConn)
            return _real(*a, **k)
        sqlite3.connect = _connect
    """
)


def _base_env(home: Path, fake_user_home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ENGRAM_", "CLAUDE_PLUGIN"))}
    env.update(
        ENGRAM_HOME=str(home),
        ENGRAM_NO_EMBEDDINGS="1",
        HOME=str(fake_user_home),
        U19_SRC=str(SRC),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def build_store(home: Path, fake_user_home: Path, *, pairing: bool = False) -> Path:
    """A real-schema store (engram_core's own setup, in a child so this process's globals stay put) and a
    config the tools accept. `pairing` plants one old-style prediction pairing edge for the migration tool."""
    env = _base_env(home, fake_user_home)
    env["ENGRAM_NO_DB_GUARDS"] = "1"
    subprocess.run([PY, "-c", _BUILD], env=env, check=True, capture_output=True, timeout=120)
    db = home / "knowledge.db"
    (home / "config.json").write_text(json.dumps({"agent_name": "u19-seat", "self_lineage": "test:lineage"}))
    conn = sqlite3.connect(str(db))
    try:
        if pairing:
            for nid, typ in (("pred-1", "prediction"), ("obs-1", "observation_predictive")):
                conn.execute(
                    "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current) "
                    "VALUES (?, ?, ?, ?, '{}', 1)", (nid, typ, "claim " + nid, "2026-01-01T00:00:00Z"))
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) "
                "VALUES ('pred-1', 'obs-1', 'supported_by', '2026-01-01T00:00:00Z')")
        conn.commit()
    finally:
        conn.close()
    return db


class Owner:
    """A REAL engramd owner: the shipped script as a child process."""

    def __init__(self, home: Path, fake_user_home: Path, ctl: Path, tag: str = "o"):
        self.home = home
        self.err_path = ctl / f"{tag}.stderr"
        self._err = open(self.err_path, "w")
        self.proc = subprocess.Popen(
            [PY, str(OWNER_PY)], env=_base_env(home, fake_user_home),
            stdout=subprocess.DEVNULL, stderr=self._err)

    def stderr(self) -> str:
        self._err.flush()
        return self.err_path.read_text()

    def wait_serving(self, timeout: float = 30.0) -> None:
        end = time.time() + timeout
        sock = self.home / "run" / "engramd.sock"
        while time.time() < end:
            if self.proc.poll() is not None:
                raise AssertionError(f"owner exited rc={self.proc.returncode}: {self.stderr()}")
            if sock.exists() and (self.home / "engramd.pid").exists():
                return
            time.sleep(0.05)
        raise AssertionError("owner never served: " + self.stderr())

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._err.close()


@pytest.fixture
def owners():
    started: list[Owner] = []
    yield started
    for o in started:
        o.stop()


def start_owner(owners, home, fake_user_home, ctl, tag="o") -> Owner:
    o = Owner(home, fake_user_home, ctl, tag)
    owners.append(o)
    o.wait_serving()
    return o


def owner_start_attempt(home, fake_user_home, ctl, tag="try", timeout=25):
    """Run a REAL owner start that is expected to REFUSE. (rc, stderr). If it instead starts serving (so it
    never exits) it is killed and the cell fails: that is the red a dropped acquisition produces."""
    err_path = ctl / f"{tag}.stderr"
    with open(err_path, "w") as err:
        proc = subprocess.Popen([PY, str(OWNER_PY)], env=_base_env(home, fake_user_home),
                                stdout=subprocess.DEVNULL, stderr=err)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            pytest.fail("the owner STARTED (it never refused) although the store was held for maintenance: "
                        + err_path.read_text())
    return rc, err_path.read_text()


def _site_dir(ctl: Path) -> Path:
    d = ctl / "site"
    d.mkdir(exist_ok=True)
    (d / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    return d


def tool_args(name: str, home: Path, ctl: Path, *, apply: bool = False, ro_extra: bool = False) -> list:
    db = str(home / "knowledge.db")
    if name == "ack_recompute_sweep":
        return ["--db", db] + (["--apply"] if apply else [])
    if name == "cites_step3":
        report = ctl / "report.json"
        report.write_text(json.dumps({"seat": "u19-seat", "lineage": "test:lineage",
                                      "frame": {"seed": 1}, "strata": [], "edges": []}))
        args = ["--report", str(report)]
        if ro_extra:
            labels = ctl / "labels.json"
            labels.write_text(json.dumps({"edges": []}))
            args += ["--labels", str(labels)]
        return args
    if name in ("migrate_person_nodes",):
        return ["--db", db]
    if name in ("migrate_prediction_pairing_edges", "migrate_stranded_supported_by"):
        return ["--db", db] + (["--apply"] if apply else [])
    if name == "surgical":
        return ["preview", "field_patch", "--node-id", "no-such-node", "--field", "claim",
                "--value", "v", "--reason", "u19 cell", "--bug-ref", BUG_REF]
    raise AssertionError(name)


class ToolRun:
    def __init__(self, rc, out, err, opens):
        self.rc, self.out, self.err, self.opens = rc, out, err, opens


def _env_for_tool(home, fake_user_home, ctl, log: Path | None, pause: Path | None):
    env = _base_env(home, fake_user_home)
    env["PYTHONPATH"] = str(_site_dir(ctl))
    if log is not None:
        env["U19_OPEN_LOG"] = str(log)
    if pause is not None:
        env["U19_PAUSE_MARKER"] = str(pause)
        env["U19_PAUSE_S"] = "8"
    return env


def _store_opens(log: Path) -> list:
    if not log.exists():
        return []
    return [ln for ln in log.read_text().splitlines() if "knowledge.db" in ln]


def run_tool(name, home, fake_user_home, ctl, args, *, tag="t", timeout=90) -> ToolRun:
    log = ctl / f"{tag}.opens"
    env = _env_for_tool(home, fake_user_home, ctl, log, None)
    p = subprocess.run([PY, str(TOOLS / f"{name}.py"), *args], env=env, capture_output=True, text=True,
                       timeout=timeout)
    return ToolRun(p.returncode, p.stdout, p.stderr, _store_opens(log))


def spawn_stalled_tool(name, home, fake_user_home, ctl, args, *, tag="s"):
    """A REAL tool, stalled at the audit event of its FIRST open of the store (the open has not happened
    yet). Returns (Popen, stall_marker). The caller kills it."""
    stall = ctl / f"{tag}.stalled"
    env = _env_for_tool(home, fake_user_home, ctl, None, None)
    env["U19_STALL_AT_OPEN"] = str(stall)
    proc = subprocess.Popen([PY, str(TOOLS / f"{name}.py"), *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.time() + 60
    while time.time() < end and not stall.exists():
        if proc.poll() is not None:
            raise AssertionError(f"{name} exited before opening the store: " + proc.stderr.read())
        time.sleep(0.02)
    assert stall.exists(), f"{name} never reached its first open"
    return proc, stall


def spawn_paused_tool(home, fake_user_home, ctl, *, tag="p"):
    """The REAL migrate_prediction_pairing_edges --apply, paused inside its write transaction (after its
    INSERTs, before its commit). Returns (Popen, pause_marker_path, opens_log)."""
    pause = ctl / f"{tag}.paused"
    log = ctl / f"{tag}.opens"
    env = _env_for_tool(home, fake_user_home, ctl, log, pause)
    proc = subprocess.Popen(
        [PY, str(TOOLS / "migrate_prediction_pairing_edges.py"), *tool_args(
            "migrate_prediction_pairing_edges", home, ctl, apply=True)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.time() + 40
    while time.time() < end and not pause.exists():
        if proc.poll() is not None:
            raise AssertionError("tool exited before the pause: " + proc.stderr.read())
        time.sleep(0.02)
    assert pause.exists(), "the tool never reached its write"
    return proc, pause, log


def fingerprint(home: Path) -> dict:
    """What 'touches nothing' means for a refused owner start: no new entry besides the lock directory
    itself, and every pre-existing regular file byte-identical."""
    out = {}
    for p in sorted(home.rglob("*")):
        rel = str(p.relative_to(home))
        if ".maint.lockdir" in rel:
            continue
        out[rel] = p.read_bytes() if p.is_file() and not p.is_symlink() else None
    return out


def _stop_hint_platform_word() -> str:
    return "launchctl" if platform.system() == "Darwin" else "systemctl"


# ---------------------------------------------------------------------------
# the typed primitive (unit level): one implementation, non-blocking, role in the holder file
# ---------------------------------------------------------------------------

def _holder_child(db: Path, role: str, ctl: Path):
    """A separate process holding the maintenance lock for `role` until `<ctl>/release` appears."""
    code = textwrap.dedent(
        """
        import os, sys, time
        sys.path.insert(0, os.environ["U19_SRC"])
        import engram_core as c
        fd = c.acquire_store_maint_lock(sys.argv[1], sys.argv[2])
        open(os.path.join(sys.argv[3], "locked"), "w").close()
        while not os.path.exists(os.path.join(sys.argv[3], "release")):
            time.sleep(0.02)
        c.release_store_maint_lock(fd)
        """
    )
    env = dict(os.environ, U19_SRC=str(SRC))
    proc = subprocess.Popen([PY, "-c", code, str(db), role, str(ctl)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    end = time.time() + 30
    while time.time() < end and not (ctl / "locked").exists():
        if proc.poll() is not None:
            raise AssertionError("holder child died: " + proc.stderr.read().decode())
        time.sleep(0.02)
    assert (ctl / "locked").exists(), "holder child never locked"
    return proc


def test_the_names_exist():
    assert core.STORE_MAINT_LOCK_SUFFIX == ".maint.lockdir"
    for n in ("StoreMaintLockError", "StoreMaintLockHeld", "StoreMaintLockUnusable",
              "acquire_store_maint_lock", "release_store_maint_lock", "store_maint_lock_path",
              "store_maint_refusal_text", "acquire_store_maint_lock_or_report"):
        assert hasattr(core, n), n
    assert core.STORE_MAINT_REFUSED_EXIT == 6
    assert eo.EXIT_STORE_MAINTENANCE == 6 == core.STORE_MAINT_REFUSED_EXIT
    assert issubclass(eo.OwnerStoreMaintenance, RuntimeError)


def test_typed_errors_are_not_setup_lock_errors():
    """A handler written for the setup lock (`except DbSetupLockError`) must not swallow a maintenance refusal."""
    for cls in (core.StoreMaintLockHeld, core.StoreMaintLockUnusable):
        assert issubclass(cls, core.StoreMaintLockError)
        assert not issubclass(cls, core.DbSetupLockError), cls
    assert not issubclass(core.StoreMaintLockError, core.DbSetupLockError)


def test_holder_file_carries_pid_and_role_and_the_lock_is_non_blocking(home, ctl):
    db = home / "knowledge.db"
    db.write_bytes(b"")
    holder = _holder_child(db, "migrate_person_nodes", ctl)
    try:
        lockdir = Path(core.store_maint_lock_path(str(db)))
        assert lockdir.name == "knowledge.db.maint.lockdir" and lockdir.is_dir()
        assert (lockdir.stat().st_mode & 0o777) == 0o700
        assert (lockdir / "holder").read_text() == f"{holder.pid} migrate_person_nodes\n"
        t0 = time.monotonic()
        with pytest.raises(core.StoreMaintLockHeld) as ei:
            core.acquire_store_maint_lock(str(db), "second")
        # ONE attempt plus the inode recheck: never the setup lock's 2 s / 600 s wait loop.
        assert time.monotonic() - t0 < 1.0
        assert ei.value.holder_pid == str(holder.pid)
        assert ei.value.holder_role == "migrate_person_nodes"
        assert str(lockdir) in str(ei.value)
    finally:
        (ctl / "release").write_text("")
        holder.wait(timeout=20)
    fd = core.acquire_store_maint_lock(str(db), "after")  # the holder released at exit: no stale lock
    core.release_store_maint_lock(fd)


def test_unreadable_holder_file_reads_as_unknown_role_and_never_blocks(home, ctl):
    db = home / "knowledge.db"
    db.write_bytes(b"")
    holder = _holder_child(db, "surgical", ctl)
    try:
        hp = Path(core.store_maint_lock_path(str(db))) / "holder"
        hp.unlink()
        os.mkfifo(hp)  # a FIFO planted at the holder name must neither stall the reader nor be read
        t0 = time.monotonic()
        with pytest.raises(core.StoreMaintLockHeld) as ei:
            core.acquire_store_maint_lock(str(db), "second")
        assert time.monotonic() - t0 < 2.0
        assert ei.value.holder_role is None and ei.value.holder_pid is None
    finally:
        (ctl / "release").write_text("")
        holder.wait(timeout=20)


def test_setup_lock_parity_one_implementation_and_the_wrapper_keeps_its_shape():
    """C7. The generalised primitive is ONE implementation: exactly one function in engram_core
    flocks with LOCK_EX, the maintenance entry points hold no flock of their own, and the setup wrapper
    keeps its one-argument signature, suffix and wait classes."""
    tree = ast.parse(inspect.getsource(core))
    nonblocking = set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            if any(isinstance(n, ast.Attribute) and n.attr == "LOCK_NB" for n in ast.walk(fn)):
                nonblocking.add(fn.name)
    # exactly ONE module-level function takes the non-blocking directory flock, and neither entry point is it
    assert len(nonblocking) == 1, nonblocking
    shared = next(iter(nonblocking))
    assert shared not in ("_acquire_db_setup_lock", "acquire_store_maint_lock")
    for name in ("acquire_store_maint_lock", "_acquire_db_setup_lock"):
        src = inspect.getsource(getattr(core, name))
        assert "LOCK_NB" not in src and shared in src, f"{name} must delegate to {shared}"
    assert list(inspect.signature(core._acquire_db_setup_lock).parameters) == ["setup_wait"]
    assert core.SETUP_LOCK_SUFFIX == ".setup.lockdir"
    assert (core.SETUP_WAIT_HOOK, core.SETUP_WAIT_SERVER) == ("hook", "server")
    assert (core.SETUP_LOCK_HOOK_WAIT_S, core.SETUP_LOCK_SERVER_WAIT_S) == (2, 600)


# ---------------------------------------------------------------------------
# C4: co-tenant (the #3791 cells against the new name)
# ---------------------------------------------------------------------------

def _lock_path(home: Path) -> Path:
    return Path(core.store_maint_lock_path(str(home / "knowledge.db")))


def test_c4_regular_file_at_the_lock_name_is_refused_and_untouched(home):
    lp = _lock_path(home)
    lp.write_bytes(b"a file squatting on the lock directory name\n")
    os.chmod(lp, 0o644)
    with pytest.raises(core.StoreMaintLockUnusable) as ei:
        core.acquire_store_maint_lock(str(home / "knowledge.db"), "t")
    assert "not a directory" in str(ei.value) and str(lp) in str(ei.value)
    assert lp.read_bytes() == b"a file squatting on the lock directory name\n"
    assert (lp.stat().st_mode & 0o777) == 0o644


def test_c4_symlink_to_a_usable_directory_is_refused_nothing_written_through_it(home):
    """The discriminating symlink cell: the target is a directory we own at 0700, so a FOLLOW would lock it
    and write the holder into it. Only O_NOFOLLOW refuses."""
    lp = _lock_path(home)
    target = home / "elsewhere"
    target.mkdir(mode=0o700)
    lp.symlink_to(target)
    with pytest.raises(core.StoreMaintLockUnusable) as ei:
        core.acquire_store_maint_lock(str(home / "knowledge.db"), "t")
    assert "symbolic link" in str(ei.value)
    assert lp.is_symlink() and list(target.iterdir()) == []


def test_c4_directory_owned_by_another_uid_is_refused_and_not_written_into(home, monkeypatch):
    lp = _lock_path(home)
    lp.mkdir(mode=0o700)
    real = os.geteuid()
    monkeypatch.setattr(core.os, "geteuid", lambda: real + 1)  # the directory then looks foreign
    with pytest.raises(core.StoreMaintLockUnusable) as ei:
        core.acquire_store_maint_lock(str(home / "knowledge.db"), "t")
    assert f"owned by uid {real}" in str(ei.value)
    assert list(lp.iterdir()) == [], "a holder was written into a directory we do not own"


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="needs root: a real directory owned by another uid can only be made with chown, which an "
           "unprivileged account cannot do; the unit cell above covers the logic (as #3791's j5/j6 do)",
)
def test_c4_real_directory_owned_by_another_uid_is_refused(home):
    lp = _lock_path(home)
    lp.mkdir(mode=0o700)
    os.chown(lp, 65534, -1)
    with pytest.raises(core.StoreMaintLockUnusable):
        core.acquire_store_maint_lock(str(home / "knowledge.db"), "t")
    assert lp.stat().st_uid == 65534 and list(lp.iterdir()) == []


def test_c4_lock_directory_is_born_0700_and_a_wider_one_is_tightened(home):
    db = str(home / "knowledge.db")
    fd = core.acquire_store_maint_lock(db, "t")
    try:
        assert (_lock_path(home).stat().st_mode & 0o777) == 0o700
    finally:
        core.release_store_maint_lock(fd)
    os.chmod(_lock_path(home), 0o755)
    fd = core.acquire_store_maint_lock(db, "t")
    core.release_store_maint_lock(fd)
    assert (_lock_path(home).stat().st_mode & 0o777) == 0o700


@requires_linux_owner
def test_c4_owner_refuses_a_planted_symlink_at_the_lock_name_and_writes_nothing_through_it(
    home, ctl, fake_user_home
):
    build_store(home, fake_user_home)
    lp = _lock_path(home)
    target = home / "elsewhere"
    target.mkdir(mode=0o700)
    lp.symlink_to(target)
    before = fingerprint(home)
    rc, err = owner_start_attempt(home, fake_user_home, ctl)
    # an UNUSABLE lock directory is a planted path, not a held lock: exit 4 (the unit does not restart it),
    # with the lock directory's path in the message
    assert rc == eo.EXIT_PATH_NOT_REGULAR == 4, (rc, err)
    assert "symbolic link" in err and str(lp) in err, err
    assert lp.is_symlink() and list(target.iterdir()) == []
    assert fingerprint(home) == before
    assert not (home / "run").exists() and not (home / "engramd.pid").exists()


# ---------------------------------------------------------------------------
# C1: exclusion both ways, real processes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SIX)
def test_c1_control_without_an_owner_the_tool_opens_the_store_and_leaves_no_lock(
    name, home, ctl, fake_user_home, owners
):
    """The positive control of the zero-opens instrument: the SAME tool, no owner, logs >= 1 open of the
    store and is not refused. An owner then starts cleanly (the tool released at exit)."""
    build_store(home, fake_user_home)
    run = run_tool(name, home, fake_user_home, ctl, tool_args(name, home, ctl))
    assert len(run.opens) >= 1, f"the instrument saw no open ({name}); rc={run.rc} err={run.err[-600:]}"
    assert run.rc == 0, (name, run.rc, run.err[-800:])
    assert "maintenance" not in run.err.lower()
    if sys.platform != "win32":
        o = start_owner(owners, home, fake_user_home, ctl)
        assert o.proc.poll() is None


@requires_linux_owner
@pytest.mark.parametrize("name", SIX)
def test_c1_with_a_real_owner_running_each_tool_exits_non_zero_before_any_store_open(
    name, home, ctl, fake_user_home, owners
):
    build_store(home, fake_user_home)
    o = start_owner(owners, home, fake_user_home, ctl)
    run = run_tool(name, home, fake_user_home, ctl, tool_args(name, home, ctl))
    assert run.rc != 0, (name, run.out[-400:], run.err[-400:])
    assert run.opens == [], f"{name} opened the store {len(run.opens)} time(s) while an owner ran: {run.opens}"
    # it names the holder (pid + role) and how to proceed on this platform
    assert str(o.proc.pid) in run.err and "owner" in run.err, run.err
    assert _stop_hint_platform_word() in run.err, run.err
    assert o.proc.poll() is None, "the refused tool disturbed the owner"


@requires_linux_owner
@pytest.mark.parametrize("name", SIX)
def test_c1_with_a_tool_holding_the_lock_a_real_owner_start_exits_6_and_touches_nothing(
    name, home, ctl, fake_user_home
):
    build_store(home, fake_user_home)
    db = home / "knowledge.db"
    holder = _holder_child(db, name, ctl)
    try:
        before = fingerprint(home)
        rc, err = owner_start_attempt(home, fake_user_home, ctl)
        assert rc == 6, (rc, err)
        assert str(holder.pid) in err and name in err, err
        assert fingerprint(home) == before, "a refused owner start changed the home"
        assert not (home / "run").exists() and not (home / "engramd.pid").exists()
    finally:
        (ctl / "release").write_text("")
        holder.wait(timeout=20)


@requires_linux_owner
@pytest.mark.parametrize("name", SIX)
def test_c1_each_tool_already_holds_the_lock_at_the_moment_of_its_first_store_open(
    name, home, ctl, fake_user_home
):
    """The tool takes the lock BEFORE its first open and still holds it then: at the audit event of that
    open (which fires before the open itself) the holder file names this very process and the tool's name,
    and a real owner start is refused with exit 6."""
    build_store(home, fake_user_home)
    proc, _ = spawn_stalled_tool(name, home, fake_user_home, ctl, tool_args(name, home, ctl))
    try:
        holder = (home / "knowledge.db.maint.lockdir" / "holder").read_text()
        assert holder == f"{proc.pid} {name}\n", holder
        rc, err = owner_start_attempt(home, fake_user_home, ctl)
        assert rc == 6 and name in err and str(proc.pid) in err, (rc, err)
    finally:
        proc.kill()
        proc.wait(timeout=20)


@requires_linux_owner
def test_c1_with_an_owner_holding_it_a_second_owner_start_exits_1(home, ctl, fake_user_home, owners):
    build_store(home, fake_user_home)
    a = start_owner(owners, home, fake_user_home, ctl, "a")
    pid_before = (home / "engramd.pid").read_text()
    rc, err = owner_start_attempt(home, fake_user_home, ctl, "b")
    assert rc == 1, (rc, err)
    assert a.proc.poll() is None and (home / "engramd.pid").read_text() == pid_before
    assert (home / "knowledge.db.maint.lockdir" / "holder").read_text() == f"{a.proc.pid} owner\n"


# ---------------------------------------------------------------------------
# C2 / C3: no race window; crash safety
# ---------------------------------------------------------------------------

@requires_linux_owner
def test_c2_an_owner_started_during_a_tools_write_exits_6_and_the_write_completes_intact(
    home, ctl, fake_user_home, owners
):
    build_store(home, fake_user_home, pairing=True)
    proc, pause, _ = spawn_paused_tool(home, fake_user_home, ctl)
    try:
        rc, err = owner_start_attempt(home, fake_user_home, ctl)
        assert rc == 6, (rc, err)
        assert str(proc.pid) in err and "migrate_prediction_pairing_edges" in err, err
        out, terr = proc.communicate(timeout=60)
        assert proc.returncode == 0, terr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM edges WHERE relation='predicted_by' AND source_id='pred-1'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM edit_history WHERE action='predicted_by_migrated'").fetchone()[0] == 1
    finally:
        conn.close()
    o = start_owner(owners, home, fake_user_home, ctl)  # and the owner starts once the tool is gone
    assert o.proc.poll() is None


@requires_linux_owner
def test_c3_kill_9_of_a_tool_holding_the_lock_leaves_no_stale_lock(home, ctl, fake_user_home, owners):
    build_store(home, fake_user_home, pairing=True)
    proc, pause, _ = spawn_paused_tool(home, fake_user_home, ctl)
    rc, err = owner_start_attempt(home, fake_user_home, ctl, "while-held")
    assert rc == 6, (rc, err)  # the lock really was held when it died
    proc.kill()  # SIGKILL
    proc.wait(timeout=20)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE relation='predicted_by'").fetchone()[0] == 0
    finally:
        conn.close()
    o = start_owner(owners, home, fake_user_home, ctl)
    assert o.proc.poll() is None


# ---------------------------------------------------------------------------
# C5: the read-only paths refuse too
# ---------------------------------------------------------------------------

@requires_linux_owner
@pytest.mark.parametrize(
    "name,ro_extra",
    [("cites_step3", False), ("cites_step3", True), ("migrate_person_nodes", False)],
)
def test_c5_read_only_paths_refuse_while_an_owner_runs(name, ro_extra, home, ctl, fake_user_home, owners):
    build_store(home, fake_user_home)
    control = run_tool(name, home, fake_user_home, ctl, tool_args(name, home, ctl, ro_extra=ro_extra),
                       tag="ctl")
    assert control.rc == 0 and len(control.opens) >= 1, (name, control.rc, control.err[-500:])
    o = start_owner(owners, home, fake_user_home, ctl)
    run = run_tool(name, home, fake_user_home, ctl, tool_args(name, home, ctl, ro_extra=ro_extra), tag="ro")
    assert run.rc != 0 and run.opens == [], (name, run.rc, run.opens, run.err[-400:])
    assert str(o.proc.pid) in run.err


# ---------------------------------------------------------------------------
# C8: the holder-role race (lock held, holder file not written yet)
# ---------------------------------------------------------------------------

_RAW_HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys, time
    d = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(d, fcntl.LOCK_EX | fcntl.LOCK_NB)
    open(sys.argv[2], "w").close()
    while not os.path.exists(sys.argv[3]):
        time.sleep(0.02)
    """
)


@requires_linux_owner
def test_c8_held_but_no_holder_file_yet_is_exit_6_and_the_retry_that_sees_owner_is_exit_1(
    home, ctl, fake_user_home
):
    build_store(home, fake_user_home)
    lp = _lock_path(home)
    lp.mkdir(mode=0o700)
    locked, release = ctl / "raw.locked", ctl / "raw.release"
    proc = subprocess.Popen([PY, "-c", _RAW_HOLDER, str(lp), str(locked), str(release)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        end = time.time() + 20
        while time.time() < end and not locked.exists():
            assert proc.poll() is None, proc.stderr.read()
            time.sleep(0.02)
        assert locked.exists() and not (lp / "holder").exists()
        rc, err = owner_start_attempt(home, fake_user_home, ctl, "no-holder-file")
        assert rc == 6, (rc, err)  # unreadable -> the retryable refusal, never "an owner is running"
        (lp / "holder").write_text(f"{proc.pid} owner\n")
        rc, err = owner_start_attempt(home, fake_user_home, ctl, "owner-holder-file")
        assert rc == 1, (rc, err)
    finally:
        release.write_text("")
        proc.wait(timeout=20)


# ---------------------------------------------------------------------------
# the owner's side, in-process: order, derivation, release
# ---------------------------------------------------------------------------

@pytest.fixture
def inproc_home():
    d = Path(tempfile.mkdtemp(prefix="u19i-"))
    os.chmod(d, 0o700)
    conn = sqlite3.connect(str(d / "knowledge.db"))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)")
    conn.execute(f"PRAGMA user_version = {core.LATEST_SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@requires_linux_owner
def test_the_owner_takes_the_lock_before_the_schema_gate_and_a_failed_start_releases_it(
    inproc_home, monkeypatch
):
    db = str(inproc_home / "knowledge.db")
    seen = {}

    def gate(store_path):
        try:
            fd = core.acquire_store_maint_lock(db, "probe")
        except core.StoreMaintLockHeld as exc:
            seen["held_at_gate"] = True
            seen["role"] = exc.holder_role
        else:
            core.release_store_maint_lock(fd)
            seen["held_at_gate"] = False
        raise eo.OwnerStoreBehindSchema("planted refusal after the gate was reached")

    monkeypatch.setattr(eo, "_require_store_schema_current", gate)
    owner = eo.EngramdOwner(inproc_home)
    with pytest.raises(eo.OwnerStoreBehindSchema):
        owner.start(install_signal_handlers=False)
    assert seen == {"held_at_gate": True, "role": "owner"}, seen
    fd = core.acquire_store_maint_lock(db, "after")  # a failed start released what it took
    core.release_store_maint_lock(fd)


# Round 4: a start that fails AFTER both store connections are open closes them, THEN releases the
# lock (never the other way round), so an embedder that keeps the process alive does not leave a repair
# tool with a free lock and an open owner connection.

def _conn_closed(conn) -> bool:
    try:
        conn.execute("select 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def _fail_bind(monkeypatch, exc):
    def boom(self):
        raise exc

    monkeypatch.setattr(eo.EngramdOwner, "_bind_socket_or_raise", boom)


def _fail_chmod_on_socket(monkeypatch, owner, exc):
    real = os.chmod

    def chmod(path, *a, **kw):
        if str(path) == str(owner.socket_path):
            raise exc
        return real(path, *a, **kw)

    monkeypatch.setattr(eo.os, "chmod", chmod)


def _capture_conns(monkeypatch):
    """Keep the connection objects, which a failed start may set back to None."""
    seen = {}
    real = eo.EngramdOwner._start_inner

    def wrapped(self, **kw):
        try:
            return real(self, **kw)
        finally:
            seen["owner"], seen["client"] = self._owner_conn, self._client_read_conn

    monkeypatch.setattr(eo.EngramdOwner, "_start_inner", wrapped)
    return seen


@requires_linux_owner
@pytest.mark.parametrize("how", ["bind_eacces", "chmod_eperm"])
def test_a_start_that_fails_after_the_connections_open_closes_them_and_frees_the_lock(
    inproc_home, monkeypatch, how
):
    db = str(inproc_home / "knowledge.db")
    owner = eo.EngramdOwner(inproc_home)
    if how == "bind_eacces":
        _fail_bind(monkeypatch, OSError(errno.EACCES, "planted bind failure"))
        expected = errno.EACCES
    else:
        _fail_chmod_on_socket(monkeypatch, owner, OSError(errno.EPERM, "planted chmod failure"))
        expected = errno.EPERM
    seen = _capture_conns(monkeypatch)
    with pytest.raises(OSError) as ei:
        owner.start(install_signal_handlers=False)
    assert ei.value.errno == expected
    assert seen["owner"] is not None and seen["client"] is not None, "the failure came before the connections opened"
    assert _conn_closed(seen["owner"]), "owner connection left open"
    assert _conn_closed(seen["client"]), "client-read connection left open"
    fd = core.acquire_store_maint_lock(db, "x")
    core.release_store_maint_lock(fd)


@requires_linux_owner
def test_a_start_that_fails_between_the_two_connection_opens_closes_the_first_and_frees_the_lock(
    inproc_home, monkeypatch
):
    """The one-connection window: `_owner_conn` is open, `_client_read_conn` is still None. The failure
    handler must still close the open one (its `had_connections` test is an `or`) before it releases."""
    db = str(inproc_home / "knowledge.db")
    owner = eo.EngramdOwner(inproc_home)
    real_connect = sqlite3.connect
    planted = []

    def connect(target, *a, **kw):
        # The first open of the store is the owner connection; any open made while it exists is the
        # client-read one, which is where this failure is planted.
        if str(target) == str(owner.store_path) and owner._owner_conn is not None:
            planted.append(target)
            raise sqlite3.OperationalError("planted failure opening the client-read connection")
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", connect)
    seen = _capture_conns(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        owner.start(install_signal_handlers=False)
    assert len(planted) == 1, planted
    assert seen["owner"] is not None and seen["client"] is None, "not the one-connection window"
    assert _conn_closed(seen["owner"]), "the one open connection was left open"
    fd = core.acquire_store_maint_lock(db, "x")
    core.release_store_maint_lock(fd)


@requires_linux_owner
def test_a_failed_start_releases_the_lock_only_after_both_connections_are_closed(inproc_home, monkeypatch):
    owner = eo.EngramdOwner(inproc_home)
    _fail_bind(monkeypatch, OSError(errno.EACCES, "planted bind failure"))
    seen = _capture_conns(monkeypatch)
    at_release = []
    real_release = core.release_store_maint_lock

    def spy(fd):
        at_release.append((_conn_closed(seen["owner"]), _conn_closed(seen["client"])))
        return real_release(fd)

    monkeypatch.setattr(core, "release_store_maint_lock", spy)
    with pytest.raises(OSError):
        owner.start(install_signal_handlers=False)
    assert at_release == [(True, True)], at_release


@requires_linux_owner
def test_a_start_refused_before_it_took_the_lock_touches_nothing_and_leaves_the_holders_lock_alone(inproc_home):
    """A refusal that comes BEFORE any connection or lock exists must not run the connection-close path:
    it would unlink a pidfile it did not write (here an unreadable one), and must never free the lock
    another process holds."""
    db = str(inproc_home / "knowledge.db")
    holder_fd = core.acquire_store_maint_lock(db, "tool")
    try:
        owner = eo.EngramdOwner(inproc_home)
        owner.pid_path.parent.mkdir(parents=True, exist_ok=True)
        owner.pid_path.write_text("not-a-pid\n")
        with pytest.raises(eo.OwnerStoreMaintenance):
            owner.start(install_signal_handlers=False)
        assert owner.pid_path.read_text() == "not-a-pid\n"
        with pytest.raises(core.StoreMaintLockHeld):
            core.acquire_store_maint_lock(db, "x")
    finally:
        core.release_store_maint_lock(holder_fd)


@requires_linux_owner
def test_a_failed_start_after_the_connections_open_does_not_remove_a_pidfile_it_did_not_write(
    inproc_home, monkeypatch
):
    """The close path shares _cleanup() with stop(); a pidfile naming ANOTHER process is not ours to unlink."""
    owner = eo.EngramdOwner(inproc_home)
    _fail_bind(monkeypatch, OSError(errno.EACCES, "planted bind failure"))
    owner.pid_path.parent.mkdir(parents=True, exist_ok=True)
    other = os.getpid() + 1
    owner.pid_path.write_text(f"{other}\n")
    with pytest.raises(OSError):
        owner.start(install_signal_handlers=False)
    assert owner.pid_path.read_text() == f"{other}\n"


@requires_linux_owner
def test_a_displaced_owner_never_releases_the_lock_in_the_failed_start_handler_even_on_the_pidfile_refusal(
    inproc_home, monkeypatch
):
    """The pidfile refusal calls _cleanup() BEFORE raising. On a displaced substrate that parks both
    connections OPEN (and sets the attributes to None), so the handler must not release the lock behind
    them: the process ends through main()'s os._exit and the kernel drops the lock then."""
    import engram_walguard

    db = str(inproc_home / "knowledge.db")
    owner = eo.EngramdOwner(inproc_home)

    def displaced(path):
        raise engram_walguard.SubstrateDisplaced({"reason": "planted"}, "planted remedy")

    def refuse(path, data):
        raise eo.NotRegularFile(path, stat.S_IFIFO)

    monkeypatch.setattr(engram_walguard, "assert_not_displaced", displaced)
    monkeypatch.setattr(eo, "write_pidfile", refuse)
    keep_before = len(eo._DISPLACED_CONNECTIONS_KEEPALIVE)
    seen = _capture_conns(monkeypatch)
    try:
        with pytest.raises(eo.OwnerPidfileNotRegular):
            owner.start(install_signal_handlers=False)
        parked = eo._DISPLACED_CONNECTIONS_KEEPALIVE[keep_before:]
        assert owner._displaced is True and len(parked) == 2
        for conn in parked:
            assert not _conn_closed(conn), "a parked connection must stay open"
        with pytest.raises(core.StoreMaintLockHeld):
            core.acquire_store_maint_lock(db, "x")
    finally:
        for conn in eo._DISPLACED_CONNECTIONS_KEEPALIVE[keep_before:]:
            conn.close()
        del eo._DISPLACED_CONNECTIONS_KEEPALIVE[keep_before:]
        owner._release_maint_lock()


@requires_linux_owner
def test_a_failing_cleanup_in_the_failed_start_handler_neither_replaces_the_start_error_nor_releases_the_lock(
    inproc_home, monkeypatch, caplog
):
    """#4073: the handler's `_cleanup()` raising (here a PermissionError unlinking our own pidfile) must
    not replace the start error and must not release the lock: a connection may still be open, so the
    lock is held until main()'s exit drops it. The cleanup error is logged."""
    db = str(inproc_home / "knowledge.db")
    owner = eo.EngramdOwner(inproc_home)
    _fail_bind(monkeypatch, OSError(errno.EACCES, "planted bind failure"))
    owner.pid_path.parent.mkdir(parents=True, exist_ok=True)
    owner.pid_path.write_text(f"{os.getpid()}\n")
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if str(self) == str(owner.pid_path):
            raise PermissionError(errno.EACCES, "planted unlink failure")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    seen = _capture_conns(monkeypatch)
    try:
        with caplog.at_level("WARNING", logger="engramd_owner"):
            with pytest.raises(OSError) as ei:
                owner.start(install_signal_handlers=False)
        assert "planted bind failure" in str(ei.value), f"the start error was replaced by: {ei.value!r}"
        assert "planted unlink failure" not in str(ei.value)
        assert seen["owner"] is not None and seen["client"] is not None, "the failure came before the connections opened"
        with pytest.raises(core.StoreMaintLockHeld):
            core.acquire_store_maint_lock(db, "x")
        warned = [r for r in caplog.records if r.name == "engramd_owner" and "planted unlink failure" in r.getMessage()]
        assert len(warned) == 1 and warned[0].levelno == logging.WARNING, [r.getMessage() for r in caplog.records]
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)
        owner._cleanup()  # the real close path, now that the unlink works: frees the lock for the next test
        owner._release_maint_lock()


@requires_linux_owner
def test_a_cleanup_failing_with_a_non_oserror_in_the_failed_start_handler_still_keeps_the_start_error_and_the_lock(
    inproc_home, monkeypatch, caplog
):
    """#4073: the handler must catch every `Exception` from `_cleanup()`, not only OSError: a connection close
    can raise other types. Here `_cleanup()` raises a RuntimeError; the bind error still leaves start(), the lock
    stays held, and one warning names the cleanup failure."""
    db = str(inproc_home / "knowledge.db")
    owner = eo.EngramdOwner(inproc_home)

    def failing_cleanup():
        raise RuntimeError("planted cleanup failure")

    try:
        # A scoped context, not monkeypatch.undo(): undo() would also revert the conftest autouse patches
        # that share this monkeypatch instance.
        with monkeypatch.context() as mp:
            _fail_bind(mp, OSError(errno.EACCES, "planted bind failure"))
            mp.setattr(owner, "_cleanup", failing_cleanup)
            seen = _capture_conns(mp)
            with caplog.at_level("WARNING", logger="engramd_owner"):
                with pytest.raises(OSError) as ei:
                    owner.start(install_signal_handlers=False)
            # OSError(EACCES, ...) is built as its PermissionError subclass; it must not be the RuntimeError.
            assert type(ei.value) is PermissionError and "planted bind failure" in str(ei.value), (
                f"the start error was replaced by: {ei.value!r}")
            assert "planted cleanup failure" not in str(ei.value)
            assert seen["owner"] is not None and seen["client"] is not None, "the failure came before the connections opened"
            with pytest.raises(core.StoreMaintLockHeld):
                core.acquire_store_maint_lock(db, "x")
            warned = [r for r in caplog.records if r.name == "engramd_owner" and "planted cleanup failure" in r.getMessage()]
            assert len(warned) == 1 and warned[0].levelno == logging.WARNING, [r.getMessage() for r in caplog.records]
    finally:
        # The context has exited: the real _cleanup is back, and the autouse patches are still in force.
        owner._cleanup()  # the real close path: frees the lock for the next test
        owner._release_maint_lock()
    # The scoped restore above must not have reverted the autouse patches that share this monkeypatch
    # (tests/conftest.py's _no_real_git_config_leak): its env var is still in force.
    assert "_2126_no_real_global_gitconfig" in os.environ.get("GIT_CONFIG_GLOBAL", ""), \
        "an early monkeypatch.undo() reverted the conftest autouse patches"


def _plant_holder(tmp_path, content: bytes):
    lockdir = tmp_path / "planted.lockdir"
    lockdir.mkdir()
    (lockdir / "holder").write_bytes(content)
    return os.open(str(lockdir), os.O_RDONLY | os.O_DIRECTORY)


@pytest.mark.parametrize(
    "content",
    [b"4242 \x1b[31mowner", b"4242 own/er\n", b"4242 ../x\n", b"4242 own$er\n"],
    ids=["ansi-escape", "slash", "dotdot-slash", "dollar"],
)
def test_a_holder_role_that_is_not_a_plain_word_reads_as_unknown(tmp_path, content):
    dir_fd = _plant_holder(tmp_path, content)
    try:
        assert core._read_store_maint_holder(dir_fd) == ("4242", None)
    finally:
        os.close(dir_fd)


def test_a_plain_holder_role_is_kept(tmp_path):
    """Control for the cell above: the same reader keeps a plain word, so the rejection is the role's, not the reader's."""
    dir_fd = _plant_holder(tmp_path, b"4242 owner\n")
    try:
        assert core._read_store_maint_holder(dir_fd) == ("4242", "owner")
    finally:
        os.close(dir_fd)


@requires_linux_owner
def test_the_lock_path_is_derived_from_the_owners_own_store_not_from_engram_cores_db_path(
    inproc_home, monkeypatch, tmp_path
):
    elsewhere = tmp_path / "elsewhere" / "knowledge.db"
    elsewhere.parent.mkdir()
    monkeypatch.setattr(core, "DB_PATH", elsewhere)
    owner = eo.EngramdOwner(inproc_home)
    owner.start(install_signal_handlers=False)
    try:
        own = Path(str(inproc_home / "knowledge.db") + core.STORE_MAINT_LOCK_SUFFIX)
        assert own.is_dir() and (own / "holder").read_text() == f"{os.getpid()} owner\n"
        assert not Path(str(elsewhere) + core.STORE_MAINT_LOCK_SUFFIX).exists()
        with pytest.raises(core.StoreMaintLockHeld):
            core.acquire_store_maint_lock(str(inproc_home / "knowledge.db"), "tool")
    finally:
        owner.stop()


@requires_linux_owner
def test_stop_releases_the_lock_only_after_both_connections_are_closed(inproc_home, monkeypatch):
    events = []
    real_connect = sqlite3.connect

    class Rec(sqlite3.Connection):
        def close(self):
            events.append(("close", id(self)))
            return super().close()

    monkeypatch.setattr(eo.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Rec, **k))
    real_release = core.release_store_maint_lock

    def spy(fd):
        events.append(("release", fd))
        return real_release(fd)

    monkeypatch.setattr(core, "release_store_maint_lock", spy)
    owner = eo.EngramdOwner(inproc_home)
    owner.start(install_signal_handlers=False)
    both = {id(owner._owner_conn), id(owner._client_read_conn)}
    assert len(both) == 2 and None not in (owner._owner_conn, owner._client_read_conn)
    del events[:]  # the start-time probes close their own short-lived connections; only stop() matters here
    owner.stop()
    kinds = [k for k, _ in events]
    assert kinds.count("release") == 1 and kinds[-1] == "release", events
    closed_before = {i for k, i in events[:-1] if k == "close"}
    assert both <= closed_before, f"the lock was released before both owner connections closed: {events}"
    fd = core.acquire_store_maint_lock(str(inproc_home / "knowledge.db"), "after")
    core.release_store_maint_lock(fd)


def test_the_exit_table_maps_the_new_refusal_to_6_and_the_message_names_holder_and_role():
    table = {cls.__name__: code for cls, code in eo._REFUSAL_EXIT}
    assert table["OwnerStoreMaintenance"] == 6
    assert 6 not in (0, 1, 2, eo.EXIT_DISPLACED, eo.EXIT_PATH_NOT_REGULAR, eo.EXIT_STORE_BEHIND_SCHEMA)


@requires_linux_owner
def test_a_second_owner_object_on_the_same_home_still_gets_owner_already_running(inproc_home):
    a = eo.EngramdOwner(inproc_home)
    a.start(install_signal_handlers=False)
    try:
        b = eo.EngramdOwner(inproc_home)
        with pytest.raises(eo.OwnerAlreadyRunning):
            b.start(install_signal_handlers=False)
    finally:
        a.stop()


# ---------------------------------------------------------------------------
# round 2 (#4037 review): second acquisition, unusable-directory text and exit code
# ---------------------------------------------------------------------------

@requires_linux_owner
def test_the_owner_takes_the_lock_again_after_the_gate_when_the_store_appeared_in_between(monkeypatch):
    """The store is absent at the first `_take_maint_lock` (so it skips) and appears while the schema gate
    runs. The second call is the only thing that then locks it: without it the owner serves for its whole
    life with no lock, and a repair tool runs beside it."""
    d = Path(tempfile.mkdtemp(prefix="u19a-"))
    os.chmod(d, 0o700)
    db = d / "knowledge.db"
    real_gate = eo._require_store_schema_current
    seen = {}

    def gate(store_path):
        seen["lock_before_gate"] = os.path.lexists(core.store_maint_lock_path(str(db)))
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)")
        conn.execute(f"PRAGMA user_version = {core.LATEST_SCHEMA_VERSION}")
        conn.commit()
        conn.close()
        return real_gate(store_path)

    monkeypatch.setattr(eo, "_require_store_schema_current", gate)
    assert not db.exists()
    owner = eo.EngramdOwner(d)
    try:
        owner.start(install_signal_handlers=False)
        try:
            assert seen == {"lock_before_gate": False}, "the first acquisition must have skipped the absent store"
            with pytest.raises(core.StoreMaintLockHeld) as ei:
                core.acquire_store_maint_lock(str(db), "tool")
            assert ei.value.holder_role == "owner"
        finally:
            owner.stop()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _assert_unusable_wording(err: str, lock_path: Path) -> None:
    assert "store-maintenance lock directory" in err and str(lock_path) in err, err
    assert "writable directory beside the store" in err and "copy the store" in err, err
    assert "-wal" in err and "-shm" in err, err
    assert "no owner and no other repair" not in err, err
    for word in ("stop:", "start:", "systemctl", "launchctl", "stop the owner"):
        assert word not in err, (word, err)


def _assert_planted_wording(err: str, lock_path: Path) -> None:
    """A planted path (symlink, not a directory, foreign owner, mode): fix the path, NOT copy the store."""
    assert "store-maintenance lock directory" in err and str(lock_path) in err, err
    assert "Fix or remove that path, then run again" in err, err
    assert "copy the store" not in err and "writable directory" not in err, err
    assert "no owner and no other repair" not in err, err
    for word in ("stop:", "start:", "systemctl", "launchctl", "stop the owner"):
        assert word not in err, (word, err)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_r2_a_read_only_directory_refuses_with_the_cause_and_the_remedy_not_the_owner_text(
    home, ctl, fake_user_home
):
    db = build_store(home, fake_user_home)
    os.chmod(home, 0o555)
    try:
        run = run_tool("migrate_person_nodes", home, fake_user_home, ctl, ["--db", str(db)])
    finally:
        os.chmod(home, 0o700)
    assert run.rc == core.STORE_MAINT_REFUSED_EXIT == 6, (run.rc, run.err)
    _assert_unusable_wording(run.err, _lock_path(home))
    assert "PermissionError" in run.err, run.err
    assert run.opens == []


def test_r2_a_planted_symlink_gets_the_fix_the_path_wording_and_no_owner_commands(home, ctl, fake_user_home):
    db = build_store(home, fake_user_home)
    target = home / "elsewhere"
    target.mkdir(mode=0o700)
    _lock_path(home).symlink_to(target)
    run = run_tool("migrate_person_nodes", home, fake_user_home, ctl, ["--db", str(db)])
    assert run.rc == 6, (run.rc, run.err)
    _assert_planted_wording(run.err, _lock_path(home))
    assert "symbolic link" in run.err, run.err
    assert list(target.iterdir()) == []


def test_r2_a_held_lock_keeps_its_stop_the_owner_text(home, fake_user_home):
    """The held-lock text is unchanged: the unusable branch must not swallow it."""
    db = build_store(home, fake_user_home)
    text = core.store_maint_refusal_text(
        core.StoreMaintLockHeld("held", lock_path="x", holder_pid="1", holder_role="owner"), str(db))
    assert "no owner and no other repair" in text and "stop the owner" in text and "stop:" in text, text
    assert "writable directory" not in text


@requires_linux_owner
def test_r6_an_unusable_lock_directory_makes_the_owner_exit_4_with_the_path_and_a_held_lock_stays_6(inproc_home):
    lp = Path(core.store_maint_lock_path(str(inproc_home / "knowledge.db")))
    lp.write_bytes(b"a file squatting on the lock directory name\n")
    owner = eo.EngramdOwner(inproc_home)
    with pytest.raises(Exception) as ei:
        owner.start(install_signal_handlers=False)
    assert eo._refusal_exit_code(ei.value) == eo.EXIT_PATH_NOT_REGULAR == 4, repr(ei.value)
    assert str(lp) in str(ei.value), str(ei.value)
    lp.unlink()
    fd = core.acquire_store_maint_lock(str(inproc_home / "knowledge.db"), "repair")
    try:
        held = eo.EngramdOwner(inproc_home)
        with pytest.raises(Exception) as ei2:
            held.start(install_signal_handlers=False)
        assert eo._refusal_exit_code(ei2.value) == 6, repr(ei2.value)
    finally:
        core.release_store_maint_lock(fd)


@requires_linux_owner
def test_a_displaced_owner_keeps_the_lock_while_its_parked_connections_are_open(inproc_home):
    """On a displaced substrate _cleanup() parks both connections OPEN and main() ends the process with
    os._exit. Releasing the lock before that exit would let a repair tool in beside open owner connections;
    the kernel drops the flock at os._exit, so _cleanup() must not release it."""
    db = str(inproc_home / "knowledge.db")
    owner = eo.EngramdOwner(inproc_home)
    owner.start(install_signal_handlers=False)
    conns = [owner._owner_conn, owner._client_read_conn]
    assert None not in conns
    parked_before = len(eo._DISPLACED_CONNECTIONS_KEEPALIVE)
    owner._displaced = True
    try:
        owner.stop()
        assert owner._owner_conn is None and owner._client_read_conn is None
        assert all(any(c is p for p in eo._DISPLACED_CONNECTIONS_KEEPALIVE) for c in conns), "not parked"
        for c in conns:  # parked means still open: a closed connection raises ProgrammingError here
            c.execute("SELECT 1").fetchone()
        with pytest.raises(core.StoreMaintLockHeld) as ei:
            core.acquire_store_maint_lock(db, "repair")
        assert ei.value.holder_role == "owner"
    finally:
        owner._release_maint_lock()  # the test process stands in for the os._exit that frees it in production
        for c in conns:
            for i in range(len(eo._DISPLACED_CONNECTIONS_KEEPALIVE) - 1, -1, -1):
                if eo._DISPLACED_CONNECTIONS_KEEPALIVE[i] is c:
                    del eo._DISPLACED_CONNECTIONS_KEEPALIVE[i]
            c.close()
    assert len(eo._DISPLACED_CONNECTIONS_KEEPALIVE) == parked_before
    core.release_store_maint_lock(core.acquire_store_maint_lock(db, "after"))


# ---------------------------------------------------------------------------
# round 3 (#4037, design seat's ruling): split the unusable class by "can a retry clear it?"
# ---------------------------------------------------------------------------

import io  # noqa: E402
import re  # noqa: E402

_NAMED_RETRYABLE = ("EMFILE", "ENFILE", "ENOSPC", "ENOLCK", "EAGAIN")
# EAGAIN means "held" at flock (BlockingIOError), so at flock only the other four are resource failures.
_OPEN_STAGE_CASES = [(s, n) for s in ("mkdir", "open") for n in _NAMED_RETRYABLE]
_FLOCK_STAGE_CASES = [("flock", n) for n in _NAMED_RETRYABLE if n != "EAGAIN"]


def _inject_errno(monkeypatch, stage: str, name: str) -> None:
    """Make ONE step of taking the maintenance lock fail with errno `name`; every other call is the real one."""
    code = getattr(errno, name)
    suffix = core.STORE_MAINT_LOCK_SUFFIX

    def boom():
        return OSError(code, os.strerror(code))

    if stage == "mkdir":
        real_mkdir = os.mkdir

        def mkdir(path, *a, **k):
            if str(path).endswith(suffix):
                raise boom()
            return real_mkdir(path, *a, **k)

        monkeypatch.setattr(core.os, "mkdir", mkdir)
    elif stage == "open":
        real_open = os.open

        def open_(path, *a, **k):
            if str(path).endswith(suffix) and "dir_fd" not in k:
                raise boom()
            return real_open(path, *a, **k)

        monkeypatch.setattr(core.os, "open", open_)
    elif stage == "flock":
        real_flock = fcntl.flock

        def flock(fd, op):
            if op & fcntl.LOCK_NB:
                raise boom()
            return real_flock(fd, op)

        monkeypatch.setattr(fcntl, "flock", flock)
    else:  # pragma: no cover
        raise AssertionError(stage)


def _owner_refusal(inproc_home):
    """(exit code the owner's table gives, message) for an owner start that is refused."""
    owner = eo.EngramdOwner(inproc_home)
    with pytest.raises(Exception) as ei:
        owner.start(install_signal_handlers=False)
    return eo._refusal_exit_code(ei.value), str(ei.value)


def _tool_refusal(inproc_home):
    buf = io.StringIO()
    fd = core.acquire_store_maint_lock_or_report(str(inproc_home / "knowledge.db"), "t", file=buf)
    assert fd is None, "the tool was not refused"
    return buf.getvalue()


@requires_linux_owner
@pytest.mark.parametrize("stage,name", _OPEN_STAGE_CASES + _FLOCK_STAGE_CASES)
def test_r3_a_named_resource_errno_makes_the_owner_exit_6_and_names_the_errno(
    inproc_home, monkeypatch, stage, name
):
    _inject_errno(monkeypatch, stage, name)
    code, msg = _owner_refusal(inproc_home)
    assert code == eo.EXIT_STORE_MAINTENANCE == 6, (code, msg)
    assert name in msg and "retries" in msg, msg


@pytest.mark.parametrize("stage,name", _OPEN_STAGE_CASES + _FLOCK_STAGE_CASES)
def test_r3_a_named_resource_errno_makes_a_tool_say_it_is_transient_and_to_retry(
    inproc_home, monkeypatch, stage, name
):
    _inject_errno(monkeypatch, stage, name)
    text = _tool_refusal(inproc_home)
    assert name in text and "transient" in text and "retry" in text.lower(), text
    assert "copy the store" not in text and "Fix or remove" not in text, text
    for word in ("stop:", "start:", "systemctl", "launchctl"):
        assert word not in text, (word, text)


def test_r3_eagain_at_flock_is_the_held_path_not_a_resource_failure(inproc_home, monkeypatch):
    _inject_errno(monkeypatch, "flock", "EAGAIN")
    with pytest.raises(core.StoreMaintLockHeld):
        core.acquire_store_maint_lock(str(inproc_home / "knowledge.db"), "t")


@requires_linux_owner
@pytest.mark.parametrize("stage", ["mkdir", "open", "flock"])
def test_r3_an_unlisted_errno_fails_closed_exit_4_with_the_errno_named(inproc_home, monkeypatch, stage):
    _inject_errno(monkeypatch, stage, "EIO")
    code, msg = _owner_refusal(inproc_home)
    assert code == eo.EXIT_PATH_NOT_REGULAR == 4, (code, msg)
    assert "EIO" in msg, msg


@pytest.mark.parametrize("stage", ["mkdir", "open", "flock"])
def test_r3_an_unlisted_errno_names_itself_in_a_tools_refusal_and_is_not_called_transient(
    inproc_home, monkeypatch, stage
):
    _inject_errno(monkeypatch, stage, "EIO")
    text = _tool_refusal(inproc_home)
    assert "EIO" in text and "transient" not in text, text


@requires_linux_owner
@pytest.mark.parametrize("name", ["EACCES", "EPERM", "EROFS"])
def test_r3_permission_and_read_only_creation_failures_stay_exit_4(inproc_home, monkeypatch, name):
    _inject_errno(monkeypatch, "mkdir", name)
    code, msg = _owner_refusal(inproc_home)
    assert code == 4 and name in msg, (code, msg)


@requires_linux_owner
def test_r3_the_planted_path_class_stays_exit_4(inproc_home, monkeypatch):
    lp = Path(core.store_maint_lock_path(str(inproc_home / "knowledge.db")))
    target = inproc_home / "elsewhere"
    target.mkdir(mode=0o700)
    lp.symlink_to(target)
    assert _owner_refusal(inproc_home)[0] == 4  # symlink
    lp.unlink()
    lp.write_bytes(b"x")
    assert _owner_refusal(inproc_home)[0] == 4  # not a directory
    lp.unlink()
    lp.mkdir(mode=0o700)
    with monkeypatch.context() as m:
        m.setattr(core.os, "geteuid", lambda: os.getuid() + 1)
        assert _owner_refusal(inproc_home)[0] == 4  # foreign owner
    os.chmod(lp, 0o755)

    def no_fchmod(fd, mode):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(core.os, "fchmod", no_fchmod)
    assert _owner_refusal(inproc_home)[0] == 4  # a mode that cannot be tightened


def test_r3_the_retryable_set_is_one_constant_not_an_if_chain_at_each_site():
    assert core.STORE_MAINT_RETRYABLE_ERRNOS == frozenset(getattr(errno, n) for n in _NAMED_RETRYABLE)
    src = Path(core.__file__).parent
    for mod in ("engram_core.py", "engramd_owner.py"):
        spelled = re.findall(r"errno\.(?:EMFILE|ENFILE|ENOSPC|ENOLCK)\b", (src / mod).read_text())
        assert len(spelled) == (4 if mod == "engram_core.py" else 0), (mod, spelled)


# ---------------------------------------------------------------------------
# round 3, T2: the "copy the store" remedy only for a permission or creation failure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["EACCES", "EPERM", "EROFS", "ENOENT"])
def test_r3_a_permission_or_creation_failure_keeps_the_copy_the_store_remedy(inproc_home, monkeypatch, name):
    _inject_errno(monkeypatch, "mkdir", name)
    text = _tool_refusal(inproc_home)
    lp = Path(core.store_maint_lock_path(str(inproc_home / "knowledge.db")))
    _assert_unusable_wording(text, lp)
    assert "Fix or remove" not in text, text


def _plant(inproc_home, monkeypatch, how: str) -> Path:
    lp = Path(core.store_maint_lock_path(str(inproc_home / "knowledge.db")))
    if how == "symlink":
        target = inproc_home / "elsewhere"
        target.mkdir(mode=0o700)
        lp.symlink_to(target)
    elif how == "not-a-directory":
        lp.write_bytes(b"x")
    elif how == "foreign-owner":
        lp.mkdir(mode=0o700)
        monkeypatch.setattr(core.os, "geteuid", lambda: os.getuid() + 1)
    elif how == "mode":
        lp.mkdir(mode=0o700)
        os.chmod(lp, 0o755)

        def no_fchmod(fd, mode):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(core.os, "fchmod", no_fchmod)
    else:  # pragma: no cover
        raise AssertionError(how)
    return lp


@pytest.mark.parametrize("how", ["symlink", "not-a-directory", "foreign-owner", "mode"])
def test_r3_a_planted_path_gets_fix_or_remove_that_path_not_the_copy_remedy(inproc_home, monkeypatch, how):
    lp = _plant(inproc_home, monkeypatch, how)
    _assert_planted_wording(_tool_refusal(inproc_home), lp)


def test_r3_an_unknown_error_names_the_errno_and_guesses_no_remedy(inproc_home, monkeypatch):
    _inject_errno(monkeypatch, "mkdir", "EIO")
    text = _tool_refusal(inproc_home)
    assert "EIO" in text, text
    for phrase in ("copy the store", "writable directory", "Fix or remove", "transient"):
        assert phrase not in text, (phrase, text)


def test_r3_a_retryable_errno_gets_neither_remedy(inproc_home, monkeypatch):
    _inject_errno(monkeypatch, "mkdir", "ENOSPC")
    text = _tool_refusal(inproc_home)
    assert "transient" in text and "copy the store" not in text and "Fix or remove" not in text, text


# ---------------------------------------------------------------------------
# #4068 points 1-2: one EngramdOwner object, one lifetime. A second start() on an object whose start() has
# already run (finished, stopped, or raised partway) is refused with OwnerRestartRefused BEFORE anything is
# touched. A fresh object is the way to start again.
# ---------------------------------------------------------------------------


def _tree_snapshot(root: Path) -> dict:
    """Names, modes and bytes of everything under `root` (bytes only for regular files; a socket has none)."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            st = p.lstat()
            data = p.read_bytes() if stat.S_ISREG(st.st_mode) else None
            snap[str(p.relative_to(root))] = (stat.S_IMODE(st.st_mode), stat.S_IFMT(st.st_mode), data)
    return snap


def _count_acquires(monkeypatch) -> list:
    calls = []
    real = core.acquire_store_maint_lock

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(core, "acquire_store_maint_lock", spy)
    return calls


def _assert_refused_and_touched_nothing(owner, home, monkeypatch):
    db = str(home / "knowledge.db")
    before = _tree_snapshot(home)
    # A scoped context, not monkeypatch.undo(): undo() would also revert the conftest autouse patches.
    with monkeypatch.context() as mp:
        acquires = _count_acquires(mp)
        with pytest.raises(eo.OwnerRestartRefused):
            owner.start(install_signal_handlers=False)
        assert acquires == [], f"the refused start took the lock: {acquires}"
        assert _tree_snapshot(home) == before, "the refused start changed the home directory"
        assert owner._maint_lock_fd is None and owner._owner_conn is None and owner._client_read_conn is None
        assert not os.path.lexists(owner.socket_path) and not os.path.lexists(owner.pid_path)
    # The scoped restore above must not have reverted the autouse patches that share this monkeypatch
    # (tests/conftest.py's _no_real_git_config_leak): its env var is still in force.
    assert "_2126_no_real_global_gitconfig" in os.environ.get("GIT_CONFIG_GLOBAL", ""), \
        "an early monkeypatch.undo() reverted the conftest autouse patches"
    fd = core.acquire_store_maint_lock(db, "x")  # the lock is free, and stays free
    core.release_store_maint_lock(fd)


@requires_linux_owner
def test_a_second_start_after_a_successful_start_and_stop_is_refused_and_touches_nothing(inproc_home, monkeypatch):
    owner = eo.EngramdOwner(inproc_home)
    owner.start(install_signal_handlers=False)
    owner.stop()
    _assert_refused_and_touched_nothing(owner, inproc_home, monkeypatch)


@requires_linux_owner
def test_a_second_start_after_a_failed_start_is_refused_and_holds_nothing(inproc_home, monkeypatch):
    owner = eo.EngramdOwner(inproc_home)
    _fail_bind(monkeypatch, OSError(errno.EACCES, "planted bind failure"))
    with pytest.raises(OSError):
        owner.start(install_signal_handlers=False)
    _assert_refused_and_touched_nothing(owner, inproc_home, monkeypatch)


@requires_linux_owner
def test_a_start_that_raised_before_taking_anything_still_counts_as_started(inproc_home, monkeypatch):
    """The flag is the FIRST thing start() does: a start that failed on the very first refusal check
    (before any lock, file, connection or socket) is still refused on retry, not given a second life."""

    def refuse(self):
        raise eo.OwnerHomeTccProtected("planted refusal on the first check")

    monkeypatch.setattr(eo.EngramdOwner, "check_start_refusals", refuse)
    owner = eo.EngramdOwner(inproc_home)
    with pytest.raises(eo.OwnerHomeTccProtected):
        owner.start(install_signal_handlers=False)
    _assert_refused_and_touched_nothing(owner, inproc_home, monkeypatch)


@requires_linux_owner
def test_a_second_start_on_a_running_owner_is_refused_and_leaves_the_running_owner_untouched(inproc_home, monkeypatch):
    """The restart guard must sit BEFORE start()'s failure handler: raised inside it, the refusal would run
    `_cleanup()` and close the LIVE connections of an owner that is still serving."""
    import engramd_client as ec

    db = str(inproc_home / "knowledge.db")
    owner = eo.EngramdOwner(inproc_home)
    owner.start(install_signal_handlers=False)
    try:
        oc, cc, fd = owner._owner_conn, owner._client_read_conn, owner._maint_lock_fd
        assert oc is not None and cc is not None and fd is not None
        # A scoped context, not monkeypatch.undo(): undo() would also revert the conftest autouse patches.
        with monkeypatch.context() as mp:
            acquires = _count_acquires(mp)
            with pytest.raises(eo.OwnerRestartRefused):
                owner.start(install_signal_handlers=False)
            assert acquires == [], f"the refused start took the lock: {acquires}"
        assert "_2126_no_real_global_gitconfig" in os.environ.get("GIT_CONFIG_GLOBAL", ""), \
            "an early monkeypatch.undo() reverted the conftest autouse patches"
        assert owner._owner_conn is oc and owner._client_read_conn is cc, "a connection object was replaced"
        for conn in (oc, cc):
            conn.execute("select 1")  # still open and usable
        assert owner._maint_lock_fd == fd
        with pytest.raises(core.StoreMaintLockHeld):
            core.acquire_store_maint_lock(db, "x")
        assert os.path.lexists(owner.socket_path) and os.path.lexists(owner.pid_path)
        assert isinstance(ec.read("ping", {}, timeout=3.0, socket_path=owner.socket_path), ec.Ok)
    finally:
        owner.stop()


@requires_linux_owner
def test_a_fresh_owner_object_still_starts_normally_after_a_refused_restart(inproc_home):
    first = eo.EngramdOwner(inproc_home)
    first.start(install_signal_handlers=False)
    first.stop()
    with pytest.raises(eo.OwnerRestartRefused):
        first.start(install_signal_handlers=False)
    second = eo.EngramdOwner(inproc_home)
    second.start(install_signal_handlers=False)
    try:
        assert second._owner_conn is not None and second._client_read_conn is not None
        assert os.path.lexists(second.socket_path)
        with pytest.raises(core.StoreMaintLockHeld):
            core.acquire_store_maint_lock(str(inproc_home / "knowledge.db"), "x")
    finally:
        second.stop()


def test_owner_restart_refused_is_a_caller_error_outside_the_operator_refusals(inproc_home, monkeypatch):
    """A second start is a programming error, not an environment condition: it is a RuntimeError, it is
    in no class of the operator-refusal table main() maps to exit codes, and main() does not handle it."""
    exc = eo.OwnerRestartRefused("x")
    assert isinstance(exc, RuntimeError)
    mapped = [cls for cls, _code in eo._REFUSAL_EXIT]
    assert eo.OwnerRestartRefused not in mapped
    assert not any(isinstance(exc, cls) for cls in mapped), "under an operator-refusal class"
    assert eo._refusal_exit_code(exc) is None

    def restart(self, **kw):
        raise eo.OwnerRestartRefused("planted")

    monkeypatch.setenv("ENGRAM_HOME", str(inproc_home))
    monkeypatch.setattr(eo.EngramdOwner, "start", restart)
    with pytest.raises(eo.OwnerRestartRefused):
        eo.main([])
