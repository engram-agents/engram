"""Contract cells (RED first) for engram-alpha #3767 / closes #3771: a
cross-process `fcntl.flock` around engram_core's one-time DB setup.

Spec: cohort-specs-2026-10-12/a3767-spec.md; design ruling: Borges,
#3767 issuecomment-6153966436.  These cells are written BEFORE the
implementation; on dev 16e8bf88 they fail because the lock behaviour is
missing, not because of fixture bugs.

Real child processes are used on purpose: the per-process
`_db_setup_lock` (threading.Lock) already serializes threads, so only
separate processes can see the cross-process race.

Cells (labels follow the spec AMENDMENT: Borges's f/g/h/l, Aleph's i/j/k):
  (f) HOOK-class waiter facing a held lock returns within ~2 s, skips, names holder pid, writes nothing
  (g) SERVER-class waiter logs the holder pid and proceeds once the holder releases
  (h) the pid in the lock file matches the holder's
  (i) mutual exclusion, deterministic (the primary red cell)
  (j) lock path unusable: named error naming the path, setup did not run
  (k) serial behaviour unchanged
  (l) every hook path into the setup passes the hook class
  (a) statistical N=5 x 100 trials on the data-bearing store (slow)
  (b) a waiter that finds the work done runs no rung / writes no backup
  (c) holder SIGKILLed mid-setup releases the lock; next process completes
  (d) SERVER-class timeout: named error, store byte-unchanged, nothing ran
      (the hook-class timeout is cell (f))
  (n) the names below exist (guards against an implementation that
      silently picks other names)
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Names the implementation must provide.  Change them HERE if the
# implementation chooses differently; nothing else in this file hardcodes them.
# ---------------------------------------------------------------------------
LOCK_SUFFIX = ".setup.lock"                  # lock file = <db path> + LOCK_SUFFIX; holder pid written into it
SETUP_WAIT_KWARG = "setup_wait"              # engram_core._get_db(setup_wait=...); default (absent) = server class
SETUP_WAIT_HOOK = "hook"                     # value selecting the hook class
HOOK_WAIT_CONSTANT = "SETUP_LOCK_HOOK_WAIT_S"      # engram_core.<name> == 2 (seconds)
SERVER_WAIT_CONSTANT = "SETUP_LOCK_SERVER_WAIT_S"  # engram_core.<name> == 600 (seconds)
BUSY_ERROR_NAME = "DbSetupLockBusy"          # engram_core.<name>: HOOK class, lock still held after the hook wait;
BUSY_MSG_FRAGMENT = "ENGRAM is upgrading its store in another process (pid "   # its message carries this text + holder pid
TIMEOUT_ERROR_NAME = "DbSetupLockTimeout"    # engram_core.<name>: SERVER class deadline passed (store untouched)
UNUSABLE_ERROR_NAME = "DbSetupLockUnusable"  # engram_core.<name>: lock file cannot be opened/flocked (message names the path)
WAITING_LOG_FRAGMENT = "waiting for setup lock held by pid "   # server-class stderr line, then the pid
# (the tests patch the two *_WAIT_S module constants in the child; no env var seam)
# Hook call sites (cell l) must pass setup_wait=<hook class> to EngramClient(...)/_get_db(...).

# The latest rung is NOT a literal here: engram_core keeps it only as a function-local
# (_run_db_one_time_setup's _PRE_MIGRATION_LATEST_VERSION), so it is read off the code under
# test -- the build child makes a fresh store with the CURRENT engram_core and reports its
# user_version, which is the latest rung by construction (a fresh store runs every rung).
# The template_home fixture assigns it; every cell that reads it depends on that fixture.
LATEST_UV = None
# The cells model a store from BEFORE the metadata-quarantine rung (rung 7: it quarantines the
# invalid-JSON rows the build inserts). That rung's number is a fixed historical fact, not
# "latest - 1": rolling back only one rung stops exercising it as soon as a later rung lands
# (#3770 added rung 8). Every rung after it is pending too, so a future rung is simply one
# more rung under the lock.
# Rolling a current-schema store back to 6 re-runs every later rung on it, so a future rung
# that is not safe to re-run will fail these cells. That would look like a lock bug, so check the rung first.
QUARANTINE_RUNG = 7
PRE_UV = QUARANTINE_RUNG - 1   # store is rolled back to just before the quarantine rung
N_NODES = 20
N_BAD_META = 8

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "engram"

# ---------------------------------------------------------------------------
# Child process
# ---------------------------------------------------------------------------
CHILD = textwrap.dedent(
    r'''
    import json, os, sys, time
    sys.path.insert(0, os.environ["SRC_DIR"])
    import engram_core as ec

    MODE = os.environ["MODE"]
    CTL = os.environ.get("CTL", "")
    ROLE = os.environ.get("ROLE", "x")

    def touch(name, text=""):
        with open(os.path.join(CTL, ROLE + "." + name), "a") as f:
            f.write(text)

    if MODE == "build":
        conn = ec._get_db()
        latest = conn.execute("PRAGMA user_version").fetchone()[0]   # fresh store => latest rung
        # A pre-rung-7 store has neither the metadata write guard nor the
        # quarantine table; drop them so invalid-JSON rows can be inserted.
        for t in ec._METADATA_GUARD_TRIGGER_NAMES:
            conn.execute("DROP TRIGGER IF EXISTS " + t)
        conn.execute("DROP TABLE IF EXISTS metadata_quarantine")
        for i in range(int(os.environ["N_NODES"])):
            meta = "{not json" if i < int(os.environ["N_BAD_META"]) else "{}"
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, confidence, recall_turn)"
                " VALUES (?, 'observation', ?, ?, ?, 0.85, 0)",
                ("ob_%04d" % (i + 1), "claim %d" % i, "2026-01-01T00:00:%02dZ" % i, meta),
            )
        conn.execute("DELETE FROM edit_history")
        conn.commit()
        conn.execute("PRAGMA user_version = %s" % os.environ["PRE_UV"])
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        print("RESULT " + json.dumps({"ok": True, "latest": latest}))
        sys.exit(0)

    if MODE == "client":
        # #3767 round 2: the client constructor, hook class (no instrumentation).
        from engram_client import EngramClient
        res = {"ok": True, "error_type": None, "error_msg": None}
        t0 = time.time()
        try:
            EngramClient(db_dir=os.environ["ENGRAM_HOME"], setup_wait=os.environ["WAIT_HOOK"])
        except BaseException as e:
            res.update(ok=False, error_type=type(e).__name__, error_msg=str(e))
        res["elapsed"] = time.time() - t0
        print("RESULT " + json.dumps(res))
        sys.exit(0)

    # MODE == "run": instrument the setup body, then call _get_db().
    rung_true = []
    _orig_rung = ec._rung_ready
    _orig_backup = ec._take_pre_migration_backup_if_pending

    def backup_spy(*a, **k):
        touch("entered")          # B-style "entered the setup body" marker
        return _orig_backup(*a, **k)

    def rung_spy(conn, target, backup_ok):
        if os.environ.get("PAUSE") == "1" and not os.path.exists(os.path.join(CTL, ROLE + ".inside")):
            touch("inside")
            deadline = time.time() + 90
            while not os.path.exists(os.path.join(CTL, ROLE + ".release")):
                if time.time() > deadline:
                    raise SystemExit("pause was never released")
                time.sleep(0.02)
        r = _orig_rung(conn, target, backup_ok)
        if r:
            rung_true.append(target)
        return r

    ec._take_pre_migration_backup_if_pending = backup_spy
    ec._rung_ready = rung_spy
    if os.environ.get("NOLOCK") == "1":
        # Reference run: the same setup with the cross-process lock taken out (a no-op fd).
        ec._acquire_db_setup_lock = lambda setup_wait: os.open(os.devnull, os.O_RDONLY)
    if os.environ.get("HOOK_WAIT_S"):
        setattr(ec, os.environ["HOOK_WAIT_CONSTANT"], float(os.environ["HOOK_WAIT_S"]))
    if os.environ.get("SERVER_WAIT_S"):
        setattr(ec, os.environ["SERVER_WAIT_CONSTANT"], float(os.environ["SERVER_WAIT_S"]))

    if os.environ.get("PATCH_FCHMOD_EPERM") == "1":
        # #3791: make "cannot tighten the lock file to 0600" reproducible without root
        # or a second uid. Opt-in only; off for every other cell.
        import errno
        def _fchmod_eperm(fd, mode):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        os.fchmod = _fchmod_eperm

    touch("started")
    if os.environ.get("GO") == "1":
        while not os.path.exists(os.path.join(CTL, "go")):
            pass

    res = {"ok": True, "error_type": None, "error_msg": None}
    t0 = time.time()
    try:
        if os.environ.get("WAIT_CLASS") == "hook":
            conn = ec._get_db(**{os.environ["WAIT_KWARG"]: os.environ["WAIT_HOOK"]})
        else:
            conn = ec._get_db()
        conn.close()
    except BaseException as e:  # report, never crash silently
        res.update(ok=False, error_type=type(e).__name__, error_msg=str(e))
    res["elapsed"] = time.time() - t0
    res["rung_true"] = rung_true
    print("RESULT " + json.dumps(res))
    '''
)


def _child_env(home, ctl, role, **extra):
    env = dict(os.environ)
    for k in ("ENGRAM_SKIP_PRE_MIGRATION_BACKUP", "ENGRAM_BOOTSTRAP"):
        env.pop(k, None)
    env.update(
        ENGRAM_HOME=str(home),
        ENGRAM_NO_DB_GUARDS="1",
        ENGRAM_NO_EMBEDDINGS="1",
        SRC_DIR=str(SRC_DIR),
        CTL=str(ctl),
        ROLE=role,
        HOOK_WAIT_CONSTANT=HOOK_WAIT_CONSTANT,
        SERVER_WAIT_CONSTANT=SERVER_WAIT_CONSTANT,
        WAIT_KWARG=SETUP_WAIT_KWARG,
        WAIT_HOOK=SETUP_WAIT_HOOK,
    )
    env.update({k: str(v) for k, v in extra.items()})
    return env


class Child:
    def __init__(self, script, home, ctl, role, **extra):
        self.role = role
        self.ctl = Path(ctl)
        self.out = self.ctl / (role + ".stdout")
        self.err = self.ctl / (role + ".stderr")
        self._o = open(self.out, "w")
        self._e = open(self.err, "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(script)],
            env=_child_env(home, ctl, role, **extra),
            stdout=self._o,
            stderr=self._e,
        )

    def marker(self, name):
        return (self.ctl / (self.role + "." + name)).exists()

    def touch(self, name):
        (self.ctl / (self.role + "." + name)).write_text("")

    def wait_marker(self, name, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            if self.marker(name):
                return True
            if self.proc.poll() is not None and not self.marker(name):
                return False
            time.sleep(0.02)
        return False

    def finish(self, timeout=90):
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"child {self.role} did not finish in {timeout}s")
        finally:
            self._o.close()
            self._e.close()
        return self.result()

    def result(self):
        for line in reversed(self.out.read_text().splitlines()):
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        return {"ok": False, "error_type": "NoResult",
                "error_msg": "rc=%s stderr=%s" % (self.proc.returncode, self.err.read_text()[-800:]),
                "rung_true": []}

    def kill(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=10)
        finally:
            for f in (self._o, self._e):
                if not f.closed:
                    f.close()


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------
def _db(home):
    return Path(home) / "knowledge.db"


def _backups(home):
    return sorted((Path(home) / "db-backup").glob("pre-migration-*.db"))


def _count_or_none(conn, table):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return None


def snapshot(home):
    """Logical state of the store (plus the backup count), for comparisons."""
    conn = sqlite3.connect(str(_db(home)))
    try:
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        eh = conn.execute(
            "SELECT action, node_id, node_type, details FROM edit_history ORDER BY action, node_id, details"
        ).fetchall()
        return {
            "user_version": uv,
            "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "node_meta": conn.execute("SELECT id, metadata FROM nodes ORDER BY id").fetchall(),
            "edit_history_count": len(eh),
            "created_node_ids": [r[1] for r in eh if r[0] == "created"],
            "node_ids": [r[0] for r in conn.execute("SELECT id FROM nodes ORDER BY id")],
            "edit_history_digest": hashlib.sha256(repr(eh).encode()).hexdigest()[:16],
            "quarantine": _count_or_none(conn, "metadata_quarantine"),
            "backups": len(_backups(home)),
        }
    finally:
        conn.close()


def raw_store_bytes(home):
    out = []
    for suffix in ("", "-wal"):
        p = Path(str(_db(home)) + suffix)
        out.append(p.read_bytes() if p.exists() else None)
    return out


@pytest.fixture(scope="module")
def child_script(tmp_path_factory):
    p = tmp_path_factory.mktemp("child") / "child.py"
    p.write_text(CHILD)
    return p


@pytest.fixture(scope="module")
def template_home(tmp_path_factory, child_script):
    """A data-bearing store built by the CURRENT engram_core: 20 nodes (8 with
    invalid-JSON metadata), empty edit_history, user_version one below latest."""
    home = tmp_path_factory.mktemp("template")
    ctl = tmp_path_factory.mktemp("buildctl")
    c = Child(child_script, home, ctl, "build", MODE="build", N_NODES=N_NODES,
              N_BAD_META=N_BAD_META, PRE_UV=PRE_UV)
    try:
        r = c.finish(120)
    finally:
        c.kill()
    assert r["ok"], r
    global LATEST_UV
    LATEST_UV = r["latest"]
    assert LATEST_UV > PRE_UV, (LATEST_UV, PRE_UV)
    shutil.rmtree(home / "db-backup", ignore_errors=True)
    # Every test home starts like a pre-lock store, as real seats do today, so
    # each cell also exercises the first creation of the lock file.
    Path(str(_db(home)) + LOCK_SUFFIX).unlink(missing_ok=True)
    s = snapshot(home)
    assert s["user_version"] == PRE_UV and s["nodes"] == N_NODES and s["edit_history_count"] == 0, s
    return home


@pytest.fixture
def home(tmp_path, template_home):
    h = tmp_path / "home"
    shutil.copytree(template_home, h)
    return h


@pytest.fixture
def ctl(tmp_path):
    d = tmp_path / "ctl"
    d.mkdir()
    return d


@pytest.fixture
def children():
    spawned = []
    yield spawned
    for c in spawned:
        c.kill()


def _spawn(children, script, home, ctl, role, **extra):
    c = Child(script, home, ctl, role, MODE="run", **extra)
    children.append(c)
    return c


def _assert_done_once(home, what):
    s = snapshot(home)
    assert s["user_version"] == LATEST_UV, f"{what}: store not at latest: {s}"
    assert s["backups"] == 1, f"{what}: expected exactly 1 pre-migration backup, got {s['backups']}"
    assert s["edit_history_count"] == N_NODES, (
        f"{what}: edit_history {s['edit_history_count']} rows, expected one set of {N_NODES}"
    )
    return s


def _serial_reference(tmp_path, template_home, child_script, nolock=False):
    h = tmp_path / ("serial_ref_nolock" if nolock else "serial_ref")
    shutil.copytree(template_home, h)
    d = tmp_path / ("serial_ctl_nolock" if nolock else "serial_ctl")
    d.mkdir()
    c = Child(child_script, h, d, "S", MODE="run", **({"NOLOCK": 1} if nolock else {}))
    try:
        r = c.finish(120)
    finally:
        c.kill()
    assert r["ok"], r
    return snapshot(h)


# ---------------------------------------------------------------------------
# (n) names exist
# ---------------------------------------------------------------------------
def test_n_names_exist():
    os.environ.setdefault("ENGRAM_NO_EMBEDDINGS", "1")
    import inspect
    import engram_core as ec

    assert getattr(ec, HOOK_WAIT_CONSTANT, None) == 2, f"engram_core.{HOOK_WAIT_CONSTANT} must be 2"
    assert getattr(ec, SERVER_WAIT_CONSTANT, None) == 600, f"engram_core.{SERVER_WAIT_CONSTANT} must be 600"
    for n in (BUSY_ERROR_NAME, TIMEOUT_ERROR_NAME, UNUSABLE_ERROR_NAME):
        assert isinstance(getattr(ec, n, None), type) and issubclass(getattr(ec, n), Exception), (
            f"engram_core.{n} missing or not an exception class"
        )
    params = inspect.signature(ec._get_db).parameters
    assert SETUP_WAIT_KWARG in params, f"_get_db has no {SETUP_WAIT_KWARG}= parameter"
    assert params[SETUP_WAIT_KWARG].default != SETUP_WAIT_HOOK, "default must be the server class"


# ---------------------------------------------------------------------------
# (k) serial behaviour unchanged (guard: green on dev by design)
# ---------------------------------------------------------------------------
def test_k_serial_run_final_store(tmp_path, template_home, child_script):
    s = _serial_reference(tmp_path, template_home, child_script)
    assert s["user_version"] == LATEST_UV
    assert s["backups"] == 1
    assert s["nodes"] == N_NODES
    assert s["edit_history_count"] == N_NODES
    assert s["quarantine"] == N_BAD_META
    # rung 7 NULLs the invalid-JSON metadata (raw blob quarantined), valid kept
    assert sum(m is None for _, m in s["node_meta"]) == N_BAD_META, s["node_meta"]
    # Absolute content check (version-independent; does not lean on the no-lock reference, which
    # would pass a content bug present in both runs): exactly one 'created' row per node.
    created = s["created_node_ids"]
    assert set(created) <= set(s["node_ids"]), "a created row names a node that does not exist"
    assert len(created) == len(set(created)), "a node has more than one created row"
    assert sorted(created) == sorted(s["node_ids"]), "created rows are not exactly one per node"
    # The lock does not change serial behaviour: the same construction run serially on the code
    # under test with the lock taken out (a live reference, not a digest pinned on some dev)
    # must end in the identical logical store: version, nodes, metadata, edit_history rows
    # (action/node/type/details), quarantine, backup count.
    ref = _serial_reference(tmp_path, template_home, child_script, nolock=True)
    assert s == ref, f"locked serial run differs from the no-lock serial reference:\n{s}\n{ref}"


# ---------------------------------------------------------------------------
# (i) mutual exclusion, deterministic: the primary red cell
# ---------------------------------------------------------------------------
def test_i_second_process_does_not_enter_setup_while_first_holds_it(
    home, ctl, child_script, children
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside"), "A never reached the setup body: " + A.err.read_text()[-500:]

    B = _spawn(children, child_script, home, ctl, "B")
    assert B.wait_marker("started"), "B never started"
    time.sleep(1.5)  # fixed window in which B must stay out
    assert not B.marker("entered"), (
        "B entered the setup body (backup/rungs) while A was paused inside it: no cross-process lock"
    )

    A.touch("release")
    ra, rb = A.finish(), B.finish()
    assert ra["ok"] and rb["ok"], (ra, rb)
    _assert_done_once(home, "(i) after release")


# ---------------------------------------------------------------------------
# (b) waiter finds work done
# ---------------------------------------------------------------------------
def test_b_waiter_that_finds_work_done_runs_no_rung_and_no_backup(
    home, ctl, child_script, children
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    B = _spawn(children, child_script, home, ctl, "B")
    assert B.wait_marker("started")
    time.sleep(1.0)
    A.touch("release")
    ra, rb = A.finish(), B.finish()
    assert ra["ok"] and rb["ok"], (ra, rb)
    assert rb["rung_true"] == [], f"waiter B ran rungs {rb['rung_true']} that A had already done"
    _assert_done_once(home, "(b) waiter")


def test_b2_store_already_at_latest_runs_no_rung_and_no_backup(
    home, ctl, child_script, children
):
    first = _spawn(children, child_script, home, ctl, "A")
    assert first.finish()["ok"]
    before = _assert_done_once(home, "(b2) setup")
    second = _spawn(children, child_script, home, ctl, "B")
    r = second.finish()
    assert r["ok"], r
    assert r["rung_true"] == []
    assert snapshot(home) == before


# ---------------------------------------------------------------------------
# (c) holder SIGKILLed mid-setup
# ---------------------------------------------------------------------------
def test_c_sigkilled_holder_releases_lock_and_next_process_completes(
    home, ctl, child_script, children
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    B = _spawn(children, child_script, home, ctl, "B")
    assert B.wait_marker("started")
    time.sleep(1.0)
    assert not B.marker("entered"), "B entered the setup body while A (still alive) held it"

    A.proc.kill()  # SIGKILL: no finally, no atexit
    A.proc.wait(timeout=10)

    rb = B.finish(timeout=45)  # bounded: a stale lock would hang here
    assert rb["ok"], rb
    s = snapshot(home)
    assert s["user_version"] == LATEST_UV, s
    assert s["edit_history_count"] == N_NODES, s
    assert s["backups"] >= 1, s


# ---------------------------------------------------------------------------
# (d) SERVER-class deadline path (patched small; never a real 600 s wait)
# ---------------------------------------------------------------------------
def test_d_server_deadline_raises_named_error_and_leaves_store_untouched(
    home, ctl, child_script, children
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    before_bytes = raw_store_bytes(home)
    before_backups = len(_backups(home))

    B = _spawn(children, child_script, home, ctl, "B", SERVER_WAIT_S=0.5)
    rb = B.finish(timeout=30)
    try:
        assert rb["error_type"] == TIMEOUT_ERROR_NAME, (
            f"B should raise {TIMEOUT_ERROR_NAME} when the lock is held past the deadline, got {rb}"
        )
        assert LOCK_SUFFIX in (rb["error_msg"] or ""), "error should name the lock path"
        assert not B.marker("entered"), "B entered the setup body despite not holding the lock"
        assert rb["rung_true"] == []
        assert raw_store_bytes(home) == before_bytes, "B changed the store"
        assert len(_backups(home)) == before_backups, "B wrote a backup"
    finally:
        A.touch("release")
        A.finish()
    _assert_done_once(home, "(d) after A completes")


# ---------------------------------------------------------------------------
# (j) lock file unusable
# ---------------------------------------------------------------------------
def test_j_unusable_lock_path_raises_named_error_naming_path_and_setup_not_run(
    home, ctl, child_script, children
):
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    lock_path.mkdir()  # a directory at the lock path: open(O_RDWR) fails (EISDIR)

    B = _spawn(children, child_script, home, ctl, "B")
    rb = B.finish(timeout=30)
    assert rb["error_type"] == UNUSABLE_ERROR_NAME, (
        f"expected {UNUSABLE_ERROR_NAME} for an unusable lock path, got {rb}"
    )
    assert str(lock_path) in (rb["error_msg"] or ""), "error must name the lock path"
    assert not B.marker("entered"), "setup body ran without the lock"
    assert rb["rung_true"] == []
    s = snapshot(home)
    assert s["user_version"] == PRE_UV and s["backups"] == 0 and s["edit_history_count"] == 0, s


# ---------------------------------------------------------------------------
# (j2) a symlink at the lock path must not be followed (O_NOFOLLOW)
# ---------------------------------------------------------------------------
def test_j2_symlink_at_lock_path_is_refused_and_target_untouched(
    home, ctl, child_script, children
):
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    sentinel = Path(home) / "sentinel.bin"
    payload = b"SENTINEL-bytes-that-must-survive\n" * 5
    sentinel.write_bytes(payload)
    lock_path.symlink_to(sentinel)

    B = _spawn(children, child_script, home, ctl, "B")
    rb = B.finish(timeout=30)
    assert rb["error_type"] == UNUSABLE_ERROR_NAME, (
        f"expected {UNUSABLE_ERROR_NAME} for a symlink at the lock path, got {rb}"
    )
    assert str(lock_path) in (rb["error_msg"] or ""), "error must name the lock path"
    assert sentinel.read_bytes() == payload, "the symlink target was modified"
    assert lock_path.is_symlink(), "the symlink was replaced"
    assert not B.marker("entered"), "setup body ran without the lock"
    assert rb["rung_true"] == []
    s = snapshot(home)
    assert s["user_version"] == PRE_UV and s["backups"] == 0 and s["edit_history_count"] == 0, s


# ---------------------------------------------------------------------------
# (j3) a hard link at the lock path shares an inode with its target: O_NOFOLLOW
#      and S_ISREG both pass, so the holder's fchmod / ftruncate / pid write
#      would land on the target.  Refuse on st_nlink != 1, before any write.
# ---------------------------------------------------------------------------
def test_j3_hardlink_at_lock_path_is_refused_and_target_untouched(
    home, ctl, child_script, children
):
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    sentinel = Path(home) / "sentinel.bin"
    payload = b"SENTINEL-bytes-that-must-survive\n" * 5
    sentinel.write_bytes(payload)
    os.chmod(sentinel, 0o640)  # not 0600: an fchmod on the shared inode would show
    os.link(sentinel, lock_path)

    B = _spawn(children, child_script, home, ctl, "B")
    rb = B.finish(timeout=30)
    assert rb["error_type"] == UNUSABLE_ERROR_NAME, (
        f"expected {UNUSABLE_ERROR_NAME} for a hard-linked lock file, got {rb}"
    )
    assert str(lock_path) in (rb["error_msg"] or ""), "error must name the lock path"
    assert "hard link" in (rb["error_msg"] or ""), "error must say the file has hard links"
    # The message must say how to recover: the remedy verb AND the path it
    # applies to, adjacent ("delete <lock_path>"), not merely the path.
    assert f"delete {lock_path}" in (rb["error_msg"] or ""), (
        "error must tell the user to delete the lock file: " + repr(rb["error_msg"])
    )
    assert "with ENGRAM stopped" in (rb["error_msg"] or ""), (
        "error must say to stop ENGRAM before deleting: " + repr(rb["error_msg"])
    )
    assert sentinel.read_bytes() == payload, "the hard-link target's bytes were modified"
    assert (sentinel.stat().st_mode & 0o777) == 0o640, (
        "the hard-link target's mode was changed: " + oct(sentinel.stat().st_mode & 0o777)
    )
    assert not B.marker("entered"), "setup body ran without the lock"
    assert rb["rung_true"] == []
    s = snapshot(home)
    assert s["user_version"] == PRE_UV and s["backups"] == 0 and s["edit_history_count"] == 0, s


def test_j4_hardlink_to_the_store_is_refused_and_store_intact(
    home, ctl, child_script, children
):
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    before_bytes = raw_store_bytes(home)
    before_mode = _db(home).stat().st_mode & 0o777
    os.link(_db(home), lock_path)  # the lock path now names the store's own inode

    B = _spawn(children, child_script, home, ctl, "B")
    try:
        rb = B.finish(timeout=30)
    except AssertionError as exc:
        # Unguarded, the holder flocks + truncates the store's own inode, and the
        # setup that follows then hangs on the wrecked store.
        raise AssertionError(f"B did not refuse the store-linked lock file: {exc}") from exc
    assert rb["error_type"] == UNUSABLE_ERROR_NAME, (
        f"expected {UNUSABLE_ERROR_NAME} for a lock file hard-linked to the store, got {rb}"
    )
    assert str(lock_path) in (rb["error_msg"] or ""), "error must name the lock path"
    assert raw_store_bytes(home) == before_bytes, "the store's bytes were modified"
    assert (_db(home).stat().st_mode & 0o777) == before_mode, "the store's mode was changed"
    conn = sqlite3.connect(str(_db(home)))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not B.marker("entered"), "setup body ran without the lock"
    assert rb["rung_true"] == []
    s = snapshot(home)
    assert s["user_version"] == PRE_UV and s["backups"] == 0 and s["edit_history_count"] == 0, s


# ---------------------------------------------------------------------------
# (m) the lock file is always 0600, whatever the store's mode
# ---------------------------------------------------------------------------
def test_m_lock_file_mode_is_0600_even_when_store_is_world_readable(
    home, ctl, child_script, children
):
    os.chmod(_db(home), 0o666)  # mirrored-mode code would give 0666 & ~umask
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    assert (lock_path.stat().st_mode & 0o777) == 0o600, oct(lock_path.stat().st_mode & 0o777)
    A.touch("release")
    assert A.finish()["ok"]
    assert (lock_path.stat().st_mode & 0o777) == 0o600, oct(lock_path.stat().st_mode & 0o777)


def test_m2_existing_wider_lock_file_is_tightened_to_0600(home, ctl, child_script, children):
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    lock_path.write_text("")
    os.chmod(lock_path, 0o644)
    B = _spawn(children, child_script, home, ctl, "B")
    assert B.finish(timeout=60)["ok"]
    assert (lock_path.stat().st_mode & 0o777) == 0o600, oct(lock_path.stat().st_mode & 0o777)


# ---------------------------------------------------------------------------
# (m3) an existing wider lock file we cannot tighten is REFUSED, not locked under (#3791)
# ---------------------------------------------------------------------------
LOCK_SENTINEL = "stale-pid-not-ours\n"


def test_m3_untightenable_wider_lock_file_is_refused_unchanged_and_setup_not_run(
    home, ctl, child_script, children
):
    """F1 + F3: fchmod fails (EPERM, injected in the child) on a 0644 lock file."""
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    lock_path.write_text(LOCK_SENTINEL)
    os.chmod(lock_path, 0o644)
    B = _spawn(children, child_script, home, ctl, "B", PATCH_FCHMOD_EPERM=1)
    rb = B.finish(timeout=30)
    # F3: exactly the Unusable class, not a Busy / Timeout sibling.
    assert rb["error_type"] == UNUSABLE_ERROR_NAME, (
        f"expected {UNUSABLE_ERROR_NAME} when the lock file cannot be tightened, got {rb}"
    )
    assert rb["error_type"] not in (BUSY_ERROR_NAME, TIMEOUT_ERROR_NAME), rb
    assert str(lock_path) in (rb["error_msg"] or ""), "error must name the lock path"
    assert not B.marker("entered"), "setup body ran under a lock file other accounts can hold"
    assert rb["rung_true"] == []
    s = snapshot(home)
    assert s["user_version"] == PRE_UV and s["backups"] == 0 and s["edit_history_count"] == 0, s
    # the refusal must not have changed the file it refused
    assert (lock_path.stat().st_mode & 0o777) == 0o644, oct(lock_path.stat().st_mode & 0o777)
    assert lock_path.read_text() == LOCK_SENTINEL


def test_m4_fchmod_is_not_attempted_when_the_lock_file_is_already_0600(
    home, ctl, child_script, children
):
    """F2 positive control: same injected fchmod failure, but a 0600 file needs no fchmod."""
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    lock_path.write_text("")
    os.chmod(lock_path, 0o600)
    B = _spawn(children, child_script, home, ctl, "B", PATCH_FCHMOD_EPERM=1)
    rb = B.finish(timeout=60)
    assert rb["ok"], rb
    assert B.marker("entered")
    assert (lock_path.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# (f3) EngramClient(setup_wait="hook") propagates Busy from its constructor
# ---------------------------------------------------------------------------
def test_f3_client_constructor_hook_class_raises_busy_within_budget(
    home, ctl, child_script, children
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    before_bytes = raw_store_bytes(home)
    C = Child(child_script, home, ctl, "C", MODE="client")
    children.append(C)
    rc = C.finish(timeout=30)
    try:
        assert rc["error_type"] == BUSY_ERROR_NAME, (
            f"EngramClient(setup_wait='hook') must raise {BUSY_ERROR_NAME}, got {rc}"
        )
        assert BUSY_MSG_FRAGMENT + str(A.proc.pid) in (rc["error_msg"] or ""), rc["error_msg"]
        # 10 s, not ~4: the claim is 'hook class does not fall into the 600 s server wait', not a tight time.
        # Idle ~2.7 s; loaded runs reached 4.2 s. 10 s keeps real headroom and is still far below 600 s.
        assert rc["elapsed"] <= 10.0, f"constructor waited {rc['elapsed']:.1f}s; hook class is ~2 s"
        assert raw_store_bytes(home) == before_bytes, "the client constructor wrote to the store"
    finally:
        A.touch("release")
        A.finish()
    _assert_done_once(home, "(f3) after A completes")


# ---------------------------------------------------------------------------
# (f) HOOK class: returns within ~2 s, skips, names holder pid, writes nothing
# ---------------------------------------------------------------------------
def test_f_hook_class_waiter_degrades_within_wait_and_writes_nothing(
    home, ctl, child_script, children
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    before_bytes = raw_store_bytes(home)
    before_backups = len(_backups(home))

    B = _spawn(children, child_script, home, ctl, "B", WAIT_CLASS="hook")
    rb = B.finish(timeout=30)
    try:
        assert rb["error_type"] == BUSY_ERROR_NAME, (
            f"hook-class B must raise {BUSY_ERROR_NAME} when the lock stays held, got {rb}"
        )
        assert BUSY_MSG_FRAGMENT + str(A.proc.pid) in (rb["error_msg"] or ""), rb["error_msg"]
        # 10 s, not ~4: the claim is 'hook class does not fall into the 600 s server wait', not a tight time.
        # Idle ~2.1 s; 10 s keeps real headroom under load and is still far below 600 s.
        assert rb["elapsed"] <= 10.0, f"hook class must not wait past ~2 s, took {rb['elapsed']:.1f}s"
        assert not B.marker("entered"), "hook-class B entered the setup body"
        assert rb["rung_true"] == []
        assert raw_store_bytes(home) == before_bytes, "hook-class B wrote to the store"
        assert len(_backups(home)) == before_backups, "hook-class B wrote a backup"
    finally:
        A.touch("release")
        A.finish()
    _assert_done_once(home, "(f) after A completes")


# ---------------------------------------------------------------------------
# (g) SERVER class: logs the holder pid, proceeds once the holder releases
# ---------------------------------------------------------------------------
def test_g_server_class_waiter_logs_holder_pid_then_proceeds_after_release(
    home, ctl, child_script, children
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    B = _spawn(children, child_script, home, ctl, "B")  # default class = server
    assert B.wait_marker("started")
    needle = WAITING_LOG_FRAGMENT + str(A.proc.pid)
    end = time.time() + 25  # log is once per 15 s; bounded
    while time.time() < end and needle not in B.err.read_text() and B.proc.poll() is None:
        time.sleep(0.1)
    assert needle in B.err.read_text(), (
        f"server-class waiter never logged {needle!r}; B stderr tail: {B.err.read_text()[-400:]}"
    )
    assert B.proc.poll() is None, "B finished while A still held the lock"
    A.touch("release")
    ra, rb = A.finish(), B.finish(timeout=60)
    assert ra["ok"] and rb["ok"], (ra, rb)
    assert rb["rung_true"] == []
    _assert_done_once(home, "(g)")


# ---------------------------------------------------------------------------
# (h) the pid in the lock file matches the holder's
# ---------------------------------------------------------------------------
def test_h_lock_file_carries_holder_pid(home, ctl, child_script, children):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside")
    lock_path = Path(str(_db(home)) + LOCK_SUFFIX)
    assert lock_path.is_file(), f"no lock file at {lock_path} while A holds the lock"
    assert lock_path.read_text().split() and str(A.proc.pid) in lock_path.read_text().split(), (
        f"lock file content {lock_path.read_text()!r} does not carry holder pid {A.proc.pid}"
    )
    A.touch("release")
    assert A.finish()["ok"]


# ---------------------------------------------------------------------------
# (l) every hook path into the setup passes the hook class
# ---------------------------------------------------------------------------
HOOKS_DIR = SRC_DIR / "hooks" / "claude"
# Hook files whose EngramClient()/_get_db() call sites run INSIDE a harness-budgeted hook fire
# (enumerated by AST scan of src/engram/hooks, dev 16e8bf88):
#   engram-surface-hook.py    query_fts_fallback()      EngramClient()
#   engram-session-start-hook.py  flag-OFF calibration   EngramClient()
# Deliberately NOT in this list: engram-surface-daemon.py (EngramClient() at startup). It is a
# long-lived background process with no harness budget, so by the spec's definition ("anything
# without a harness budget") it is the server class. Flagged in the hand-off for the parent.
HOOK_FIRE_FILES = ("engram-surface-hook.py", "engram-session-start-hook.py")


def _setup_reaching_calls(path):
    import ast

    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in ("EngramClient", "_get_db"):
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                out.append((node.lineno, name, kw.get(SETUP_WAIT_KWARG)))
    return out


def test_l_every_hook_fire_path_into_setup_passes_the_hook_class():
    import ast

    sites = []
    for fname in HOOK_FIRE_FILES:
        found = _setup_reaching_calls(HOOKS_DIR / fname)
        assert found, f"scan found no EngramClient()/_get_db() call in {fname}: list is stale"
        sites += [(fname,) + t for t in found]
    bad = []
    for fname, lineno, name, val in sites:
        ok = val is not None and (
            (isinstance(val, ast.Constant) and val.value == SETUP_WAIT_HOOK)
            or "HOOK" in ast.unparse(val).upper()
        )
        if not ok:
            bad.append(f"{fname}:{lineno} {name}(...) passes {SETUP_WAIT_KWARG}="
                       f"{ast.unparse(val) if val is not None else '<default = server class>'}")
    assert not bad, "hook paths reaching the setup with the default (server) class:\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------------------
# (f2) the REAL session-start hook, flag-OFF path, against a held setup lock
# ---------------------------------------------------------------------------
HOOK_RUNNER = r'''
import importlib.util, io, json, os, sys, types
hook_path = sys.argv[1]
sys.modules["context_tracker"] = types.SimpleNamespace(write_baseline=lambda **k: None)
spec = importlib.util.spec_from_file_location("ss_hook_under_test", hook_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# Hermetic: stub every block that probes the machine; the calibration block is left REAL.
mod.detect_harness = lambda: "claude-code"
mod.tool_capability_index_block = lambda: ""
mod.tool_capability_short_block = lambda: ""
mod._check_mcp_health = lambda h: (True, None)
mod._check_viz_server_health = lambda h: ("not-configured", None)
mod.piece_c_git_log = lambda sid: ""
mod.forum_monitor_block = lambda: ""
mod.forum_updates_monitor_block = lambda: ""
mod.peer_topology_block = lambda a: ""
mod.starred_block = lambda h: ""
sys.stdin = io.StringIO(json.dumps({"session_id": "s1", "transcript_path": "t.jsonl", "source": "startup"}))
try:
    mod.main()
except SystemExit as e:
    sys.exit(e.code or 0)
'''


def test_f2_session_start_hook_emits_the_one_busy_line_and_writes_nothing(
    home, ctl, child_script, children, tmp_path
):
    A = _spawn(children, child_script, home, ctl, "A", PAUSE=1)
    assert A.wait_marker("inside"), "A never reached the setup body: " + A.err.read_text()[-500:]
    before_bytes = raw_store_bytes(home)
    before_backups = len(_backups(home))

    runner = tmp_path / "hook_runner.py"
    runner.write_text(HOOK_RUNNER)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "ENGRAM_SESSION_ROLE")}
    (tmp_path / "subhome").mkdir()
    env.update(ENGRAM_HOME=str(home), HOME=str(tmp_path / "subhome"), ENGRAM_RUNTIME_DIR=str(SRC_DIR),
               ENGRAM_NO_EMBEDDINGS="1", ENGRAM_NO_DB_GUARDS="1")  # flag-OFF: no config.json in the home
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, str(runner), str(HOOKS_DIR / "engram-session-start-hook.py")],
                           capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=30)
        wall = time.time() - t0
        assert r.returncode == 0, (r.returncode, r.stderr[-800:])
        assert wall < 5.0, f"hook took {wall:.1f}s; it must degrade inside its budget"
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        assert ctx.count(BUSY_MSG_FRAGMENT + str(A.proc.pid)) == 1, ctx[-1500:]
        assert raw_store_bytes(home) == before_bytes, "the hook wrote to the store"
        assert len(_backups(home)) == before_backups, "the hook wrote a backup"
    finally:
        A.touch("release")
        A.finish()
    _assert_done_once(home, "(f2) after A completes")


# ---------------------------------------------------------------------------
# (a) statistical cell
# ---------------------------------------------------------------------------
def test_a_five_racers_100_trials_no_doubled_history_one_backup(
    tmp_path, template_home, child_script, children
):
    N, TRIALS = 5, 100
    ref = _serial_reference(tmp_path, template_home, child_script)
    assert ref["backups"] == 1 and ref["edit_history_count"] == N_NODES

    doubled, bad_backups, diverged, errors = [], [], [], []
    for t in range(TRIALS):
        h = tmp_path / f"trial{t}"
        shutil.copytree(template_home, h)
        d = tmp_path / f"trialctl{t}"
        d.mkdir()
        procs = [Child(child_script, h, d, f"R{i}", MODE="run", GO=1) for i in range(N)]
        children.extend(procs)
        assert all(p.wait_marker("started", 60) for p in procs), "racers did not all start"
        (d / "go").write_text("")  # release all racers at once (no stagger)
        results = [p.finish(120) for p in procs]
        for r in results:
            if not r["ok"]:
                errors.append((t, r["error_type"], r["error_msg"]))
        s = snapshot(h)
        if s["edit_history_count"] != ref["edit_history_count"]:
            doubled.append((t, s["edit_history_count"]))
        if s["backups"] != 1:
            bad_backups.append((t, s["backups"]))
        if s != ref:
            diverged.append(t)
        shutil.rmtree(h, ignore_errors=True)
    assert not (doubled or bad_backups or diverged or errors), (
        f"{TRIALS} trials x {N} racers: doubled edit_history={doubled}, "
        f"backups!=1={bad_backups}, store!=serial reference in trials {diverged}, errors={errors[:3]}"
    )
