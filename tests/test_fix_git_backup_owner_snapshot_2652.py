"""engram-alpha#2652 (tools-10) -- src/engram/tools/engram-fix-git-backup.sh reads ONE owner snapshot.

Behind `engramd.fix_git_backup_reads_via_owner` (default OFF) the one-time git-backup repair no longer opens
the live knowledge.db. Today it opens it four times: the step-1 `s.backup(d)` into the safety copy (ledger row
55), the step-1 `LIVE_NODES` count (row 56), the step-3 `engram_backup.py dump` (row 3) and the step-4
`engram_backup.py verify` (row 6, which dumps again). Flag ON: ONE owner snapshot lands STRAIGHT at the
operator path `$HOME/.engram-db-safety-<TS>.db`; the live-vs-copy count compare (which would compare the copy
with itself) is replaced by two checks the copy CAN fail (`PRAGMA quick_check` == ok, `count(*) FROM nodes` > 0);
steps 3 and 4 take the snapshot as their source. Any snapshot failure dies before step 2.

Cells (each drives the script as a SUBPROCESS: a temp HOME, a temp ENGRAM_HOME, an explicit PYTHON_BIN):

  T1  a corrupt / truncated / empty-store snapshot -> non-zero at the new check; no knowledge.sql, no
      .gitignore, git untouched.
  T2  flag ON: the live knowledge.db is opened ZERO times across steps 1-5 (a sitecustomize on PYTHONPATH logs
      every sqlite3.connect target, resolved). T2c is its POSITIVE CONTROL: flag OFF logs >= 4 live opens, so a
      zero is not a broken instrument. T2d: flag ON and flag OFF write the same knowledge.sql rows.
  T3  flag OFF: identical to the DEV script (c0e49e0c, `git show`) run on an identical fixture: stdout, stderr
      (modulo the timestamp and the paths), exit code, files written.
  T4  indeterminate snapshot (Rejected(commit="indeterminate"), a dropped connection, a real timeout) -> dies
      before step 3; names the flag; says the file may be incomplete and must not be trusted; the file is never
      opened.
  T5  owner down -> dies, names the flag, no live read.
  T6  flag absent / false / non-boolean / malformed config -> the flag-OFF path, the owner never contacted; the
      flag is read from $ENGRAM_HOME/config.json ONLY (a decoy at $HOME/.engram/config.json is ignored); a
      broken runtime bundle reads OFF with ONE stderr line.
  T7  --dry-run under the flag: no owner call, nothing opened, nothing written.
  T8  a REAL owner (this host's predicate supports it: the cell is gated by the shared marker).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _fake_owner import FakeOwner
from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_owner as eo  # noqa: E402

SCRIPT = REPO_ROOT / "src" / "engram" / "tools" / "engram-fix-git-backup.sh"
ENGRAM_BACKUP = REPO_ROOT / "src" / "engram" / "engram_backup.py"
DEV_COMMIT = "c0e49e0c"
DEV_SCRIPT_REL = "src/engram/tools/engram-fix-git-backup.sh"
FLAG_KEY = "fix_git_backup_reads_via_owner"
FLAG_DOTTED = f"engramd.{FLAG_KEY}"
# The real home of whoever runs this suite, captured at import: a test must never write there.
REAL_HOME = os.path.realpath(os.path.expanduser("~"))
N_NODES = 12

SAFETY_RE = re.compile(r"\.engram-db-safety-\d{8}T\d{6}Z\.db$")


# ---- fixtures ---------------------------------------------------------------------------------------

@pytest.fixture
def sandbox():
    """A short temp base (an AF_UNIX path is <= 104 bytes) holding a temp HOME and a temp ENGRAM_HOME.
    Neither is ever the real home or under it."""
    base = Path(tempfile.mkdtemp(prefix="ft10-", dir="/tmp"))
    home, eh = base / "home", base / "eh"
    home.mkdir()
    eh.mkdir()
    for p in (home, eh):
        real = os.path.realpath(p)
        assert real != REAL_HOME and not real.startswith(REAL_HOME + os.sep), (real, REAL_HOME)
    try:
        yield base, home, eh
    finally:
        shutil.rmtree(base, ignore_errors=True)


def make_store(eh: Path, nodes: int = N_NODES) -> Path:
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(eh)
    try:
        db = eh / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        for i in range(nodes):
            conn.execute(
                "INSERT INTO nodes (id, type, claim, quoted_text, created_at) VALUES (?, 'observation', ?, ?, ?)",
                (f"ob_{i:04d}", f"claim {i}", f"quote {i}" if i % 3 == 0 else None, f"2026-01-01T00:00:{i:02d}"))
        for i in range(1, nodes):
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                (f"ob_{i:04d}", f"ob_{i - 1:04d}", "2026-01-02T00:00:00"))
        for i in range(0, nodes, 4):
            conn.execute(
                "INSERT INTO edit_history (timestamp, turn, action, node_id, node_type) "
                "VALUES (?, 0, 'created', ?, 'observation')", ("2026-01-03T00:00:00", f"ob_{i:04d}"))
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def set_config(eh: Path, flag=True, raw: str | None = None) -> None:
    """flag True/False/<any json value> writes {"engramd": {FLAG_KEY: flag}}; flag None writes no config;
    `raw` writes the text verbatim (a malformed config)."""
    cfg = eh / "config.json"
    if raw is not None:
        cfg.write_text(raw, encoding="utf-8")
    elif flag is None:
        cfg.unlink(missing_ok=True)
    else:
        cfg.write_text(json.dumps({"engramd": {FLAG_KEY: flag}}), encoding="utf-8")


# ---- the opens instrument ---------------------------------------------------------------------------

_SITECUSTOMIZE = '''\
import os, sqlite3, urllib.parse
_LOG = os.environ.get("FT10_OPENS_LOG")
if _LOG:
    def _target(database):
        t = os.fsdecode(database) if isinstance(database, (bytes, os.PathLike)) else str(database)
        if t.startswith("file:"):
            t = urllib.parse.unquote(urllib.parse.urlparse(t).path)
        return t if t == ":memory:" or not t else os.path.realpath(t)
    def _wrap(real):
        def connect(database, *a, **k):
            try:
                with open(_LOG, "a", encoding="utf-8") as fh:
                    fh.write(_target(database) + "\\n")
            except Exception:
                pass
            return real(database, *a, **k)
        return connect
    sqlite3.connect = _wrap(sqlite3.connect)
    sqlite3.dbapi2.connect = sqlite3.connect
'''


@pytest.fixture
def instrument(sandbox):
    base, _home, _eh = sandbox
    d = base / "instr"
    d.mkdir()
    (d / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return d, base / "opens.log"


def opens_of(log: Path, target: Path) -> list:
    want = os.path.realpath(target)
    if not log.exists():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln and os.path.realpath(ln) == want]


def all_opens(log: Path) -> list:
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln] if log.exists() else []


# ---- running the script -----------------------------------------------------------------------------

def run_script(script, home: Path, eh: Path, args=(), *, instr=None, env_extra=None, timeout=120, cwd=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "ENGRAM_HOME": str(eh),
        "PYTHON_BIN": sys.executable,
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if instr is not None:
        d, log = instr
        env["PYTHONPATH"] = str(d)
        env["FT10_OPENS_LOG"] = str(log)
    env.update(env_extra or {})
    return subprocess.run(["bash", str(script), *args], env=env, cwd=str(cwd or home), capture_output=True, text=True,
                          timeout=timeout)


def git(eh: Path, *a) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(eh), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(eh), *a], env=env, capture_output=True, text=True, check=True).stdout


def seed_git(eh: Path) -> str:
    """A pre-existing repo with one commit; returns its HEAD. A run that reaches step 5 replaces it."""
    git(eh, "init", "-q")
    git(eh, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "seed")
    return git(eh, "rev-parse", "HEAD").strip()


def safety_files(home: Path) -> list:
    return sorted(p.name for p in home.iterdir() if SAFETY_RE.search(p.name))


def restore_rows(sql_path: Path) -> dict:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql_path.read_text(encoding="utf-8"))
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: sorted(repr(r) for r in conn.execute(f'SELECT * FROM "{n}"')) for n in names}
    finally:
        conn.close()


# ---- the fake owner's snapshot op -------------------------------------------------------------------

class SnapshotOwner(FakeOwner):
    """FakeOwner + a `snapshot` op. `behavior` picks the answer:

    ok            a consistent copy at dest_path, answered {"dest_path": dest}
    corrupt       dest_path holds bytes that are not a database, answered ok
    truncated     a real copy cut to its first 600 bytes, answered ok
    empty_store   a real, healthy copy with every node deleted, answered ok
    indeterminate the copy written, then Rejected(deadline_exceeded, commit="indeterminate")
    drop          the connection closed with no answer (the client reads it as Unavailable(protocol))
    hang          the connection held open and silent (the client reads it as Unavailable(timeout), after 35 s)
    """

    def __init__(self, home, behavior="ok"):
        super().__init__(home)
        self.behavior = behavior
        self.snapshot_requests = []

    def _copy(self, dest: str) -> None:
        src = sqlite3.connect((self.home / "knowledge.db").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

    def _handle(self, conn):
        # `drop` and `hang` need the transport, which FakeOwner._handle owns: peek at the request first.
        if self.behavior in ("drop", "hang"):
            try:
                conn.settimeout(2)
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                request = json.loads(data.decode("utf-8").strip())
                self.requests.append(request)
                if request.get("method") == "snapshot":
                    self.snapshot_requests.append(request["params"]["dest_path"])
                    if self.behavior == "hang":
                        self._stop.wait(timeout=45)
                    return
                conn.sendall((json.dumps(self._answer(request)) + "\n").encode("utf-8"))
            except OSError:
                pass
            finally:
                conn.close()
            return
        super()._handle(conn)

    def _answer(self, request):
        if request.get("method") != "snapshot":
            return super()._answer(request)
        dest = request["params"]["dest_path"]
        self.snapshot_requests.append(dest)
        b = self.behavior
        if b == "corrupt":
            Path(dest).write_bytes(b"this is not a sqlite database\n" * 40)
        elif b == "truncated":
            self._copy(dest)
            with open(dest, "r+b") as fh:
                fh.truncate(600)
        elif b == "empty_store":
            self._copy(dest)
            c = sqlite3.connect(dest)
            c.execute("PRAGMA foreign_keys=OFF")
            c.execute("DELETE FROM edges")
            c.execute("DELETE FROM edit_history")
            c.execute("DELETE FROM nodes")
            c.commit()
            c.close()
        else:
            self._copy(dest)
            if b == "indeterminate":
                return {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r",
                        "commit": "indeterminate"}
        return {"status": "ok", "result": {"dest_path": dest}}


@contextlib.contextmanager
def real_owner(home: Path):
    owner = eo.EngramdOwner(home, request_deadline=10.0)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        owner.stop()


def flag_on_fixture(eh: Path) -> Path:
    db = make_store(eh)
    set_config(eh, True)
    return db


# ---- T1: a snapshot the copy-checks reject ----------------------------------------------------------

@pytest.mark.parametrize("behavior,needle", [
    ("corrupt", "quick_check"),
    ("truncated", "quick_check"),
    ("empty_store", "nodes"),
])
def test_t1_a_snapshot_that_fails_its_checks_stops_before_step_2_and_leaves_git_alone(sandbox, behavior, needle):
    _base, home, eh = sandbox
    flag_on_fixture(eh)
    head = seed_git(eh)
    with SnapshotOwner(eh, behavior) as owner:
        r = run_script(SCRIPT, home, eh)
    assert owner.snapshot_requests, "the owner was never asked for a snapshot (flag path not taken)"
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert needle in r.stderr, r.stderr
    assert not (eh / "knowledge.sql").exists()
    assert not (eh / ".gitignore").exists(), "step 2 ran"
    assert git(eh, "rev-parse", "HEAD").strip() == head
    assert git(eh, "log", "--format=%s").split() == ["seed"]


def test_t1_control_a_healthy_snapshot_passes_the_same_checks(sandbox):
    _base, home, eh = sandbox
    flag_on_fixture(eh)
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert owner.snapshot_requests and (eh / "knowledge.sql").is_file()


# ---- T2: the live store is opened ZERO times --------------------------------------------------------

def test_t2c_positive_control_flag_off_opens_the_live_store_at_least_four_times(sandbox, instrument):
    _base, home, eh = sandbox
    live = make_store(eh)
    set_config(eh, False)
    r = run_script(SCRIPT, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)
    n = len(opens_of(instrument[1], live))
    assert n >= 4, f"the instrument saw {n} live opens with the flag OFF (>= 4 expected): it is broken, so every zero is void"


def test_t2_flag_on_never_opens_the_live_store_across_steps_1_to_5(sandbox, instrument):
    _base, home, eh = sandbox
    live = flag_on_fixture(eh)
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)
    # the run is vacuous unless it really ran the flag path end to end
    assert len(owner.snapshot_requests) == 1
    snaps = safety_files(home)
    assert len(snaps) == 1 and owner.snapshot_requests[0] == str(home / snaps[0])
    assert opens_of(instrument[1], home / snaps[0]), "the snapshot copy was never opened (instrument or flag path broken)"
    assert (eh / "knowledge.sql").is_file() and (eh / ".git").is_dir()
    assert opens_of(instrument[1], live) == [], all_opens(instrument[1])


def test_t2_step_3_and_4_take_the_snapshot_as_their_source(sandbox, instrument):
    _base, home, eh = sandbox
    flag_on_fixture(eh)
    with SnapshotOwner(eh, "ok"):
        r = run_script(SCRIPT, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)
    snap = home / safety_files(home)[0]
    # check (1 open: quick_check + count), dump (1), verify (>= 1 on the snapshot, plus the in-memory rebuild)
    assert len(opens_of(instrument[1], snap)) >= 3, all_opens(instrument[1])
    assert "live comparison is dropped" in r.stdout and FLAG_DOTTED in r.stdout, r.stdout


def test_t2d_flag_on_and_flag_off_write_the_same_dump(sandbox):
    base, home, eh = sandbox
    make_store(eh)
    base2 = Path(tempfile.mkdtemp(prefix="ft10b-", dir="/tmp"))
    try:
        home2, eh2 = base2 / "home", base2 / "eh"
        home2.mkdir()
        eh2.mkdir()
        shutil.copy(eh / "knowledge.db", eh2 / "knowledge.db")
        set_config(eh, False)
        r_off = run_script(SCRIPT, home, eh)
        set_config(eh2, True)
        with SnapshotOwner(eh2, "ok"):
            r_on = run_script(SCRIPT, home2, eh2)
        assert r_off.returncode == 0 and r_on.returncode == 0, (r_off.stderr, r_on.stderr)
        off, on = restore_rows(eh / "knowledge.sql"), restore_rows(eh2 / "knowledge.sql")
        assert off == on and len(on["nodes"]) == N_NODES
    finally:
        shutil.rmtree(base2, ignore_errors=True)


# ---- T3: flag OFF is byte-identical to dev ----------------------------------------------------------

@pytest.fixture(scope="module")
def dev_script(tmp_path_factory):
    """The script at c0e49e0c, run from a scratch tree where `<tools>/../engram_backup.py` resolves."""
    try:
        text = subprocess.run(["git", "show", f"{DEV_COMMIT}:{DEV_SCRIPT_REL}"], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        pytest.skip(f"the dev commit {DEV_COMMIT} is not reachable in this checkout: {exc}")
    root = tmp_path_factory.mktemp("devtree")
    (root / "tools").mkdir()
    script = root / "tools" / "engram-fix-git-backup.sh"
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    (root / "engram_backup.py").symlink_to(ENGRAM_BACKUP)
    return script


def _normalise(text: str, home: Path, eh: Path, script: Path) -> str:
    backup = str(script.resolve().parent.parent / "engram_backup.py")
    raw_backup = str(script.parent / ".." / "engram_backup.py")
    for needle, tag in ((raw_backup, "<BACKUP>"), (backup, "<BACKUP>"),
                        (str(script), "<SCRIPT>"), (str(script.resolve()), "<SCRIPT>"),
                        (str(os.path.realpath(home)), "<HOME>"), (str(home), "<HOME>"),
                        (str(os.path.realpath(eh)), "<EH>"), (str(eh), "<EH>")):
        text = text.replace(needle, tag)
    text = re.sub(r"\d{8}T\d{6}Z", "<TS>", text)
    return re.sub(r"\b[0-9a-f]{40}\b", "<SHA>", text)


def _tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if rel == ".git" or rel.startswith(".git" + os.sep) or rel.startswith("run" + os.sep) or p.is_dir():
            continue
        rel = re.sub(r"\d{8}T\d{6}Z", "<TS>", rel)
        if p.name == "knowledge.db" or SAFETY_RE.search(p.name):
            out[rel] = "db"  # a db file's bytes are not reproducible; its rows are compared below
        else:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _snapshot_of_run(script, home, eh, args, setup, seed_db: Path):
    shutil.copy(seed_db, eh / "knowledge.db")
    (eh / "warm-briefing.md").write_text("briefing\n", encoding="utf-8")
    (eh / "session_log.md").write_text("log\n", encoding="utf-8")
    (eh / "diary").mkdir()
    (eh / "diary" / "d1.md").write_text("diary\n", encoding="utf-8")
    setup(eh)
    r = run_script(script, home, eh, args)
    result = {
        "rc": r.returncode,
        "stdout": _normalise(r.stdout, home, eh, script),
        "stderr": _normalise(r.stderr, home, eh, script),
        "eh": _tree(eh),
        "home": _tree(home),
    }
    if (eh / ".git").is_dir():
        result["git_files"] = git(eh, "ls-tree", "-r", "--name-only", "HEAD")
        result["git_subject"] = _normalise(git(eh, "log", "--format=%s"), home, eh, script)
    if (eh / "knowledge.sql").exists():
        result["dump_rows"] = restore_rows(eh / "knowledge.sql")
    result["safety_rows"] = [restore_rows_db(home / n) for n in safety_files(home)]
    return result


def restore_rows_db(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: sorted(repr(r) for r in conn.execute(f'SELECT * FROM "{n}"')) for n in names
                if not n.startswith(("nodes_fts", "vec_", "node_embeddings"))}
    except sqlite3.Error as exc:
        return {"error": type(exc).__name__}
    finally:
        conn.close()


T3_VARIANTS = {
    "live-flag-absent": ((), lambda eh: None),
    "live-flag-false": ((), lambda eh: set_config(eh, False)),
    "live-flag-false-malformed-neighbour": ((), lambda eh: set_config(eh, raw="{not json")),
    "dry-run": (("--dry-run",), lambda eh: None),
    "dry-run-flag-false": (("--dry-run",), lambda eh: set_config(eh, False)),
    "unknown-arg": (("--bogus",), lambda eh: None),
    "help": (("--help",), lambda eh: None),
}


@pytest.mark.parametrize("variant", sorted(T3_VARIANTS))
def test_t3_flag_off_is_identical_to_the_dev_script(dev_script, variant):
    args, setup = T3_VARIANTS[variant]
    seed_base = Path(tempfile.mkdtemp(prefix="ft10s-", dir="/tmp"))
    try:
        (seed_base / "seed").mkdir()
        seed = make_store(seed_base / "seed")
        runs = []
        for label, script in (("dev", dev_script), ("new", SCRIPT)):
            base = Path(tempfile.mkdtemp(prefix=f"ft10{label}-", dir="/tmp"))
            try:
                home, eh = base / "home", base / "eh"
                home.mkdir()
                eh.mkdir()
                assert os.path.realpath(home) != REAL_HOME and not os.path.realpath(eh).startswith(REAL_HOME + os.sep)
                runs.append(_snapshot_of_run(script, home, eh, args, setup, seed))
            finally:
                shutil.rmtree(base, ignore_errors=True)
        dev, new = runs
        assert dev["rc"] == new["rc"]
        assert dev["stdout"] == new["stdout"]
        assert dev["stderr"] == new["stderr"]
        assert dev == new
        if variant.startswith("live"):  # not vacuous: the run really did the work
            assert dev["rc"] == 0 and "dump_rows" in dev and dev["safety_rows"]
    finally:
        shutil.rmtree(seed_base, ignore_errors=True)


def test_t3_failure_exit_codes_match_dev_for_a_missing_store_and_a_relative_home(dev_script, sandbox):
    _base, home, eh = sandbox  # no knowledge.db in eh
    for script in (dev_script, SCRIPT):
        r = run_script(script, home, eh)
        assert r.returncode == 1 and "knowledge.db not found" in r.stderr
    for script in (dev_script, SCRIPT):
        r = run_script(script, home, eh, env_extra={"ENGRAM_HOME": "relative/dir"})
        assert r.returncode == 2 and "must be an absolute path" in r.stderr


# ---- T4: an indeterminate snapshot ------------------------------------------------------------------

@pytest.mark.parametrize("behavior", ["indeterminate", "drop"])
def test_t4_indeterminate_dies_before_step_3_names_the_flag_and_never_trusts_the_file(sandbox, instrument, behavior):
    _base, home, eh = sandbox
    live = flag_on_fixture(eh)
    head = seed_git(eh)
    with SnapshotOwner(eh, behavior) as owner:
        r = run_script(SCRIPT, home, eh, instr=instrument)
    assert owner.snapshot_requests, "the owner was never asked (flag path not taken)"
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert FLAG_DOTTED in r.stderr, r.stderr
    assert "may or may not be complete" in r.stderr and "must not be trusted" in r.stderr, r.stderr
    assert str(home / ".engram-db-safety-") in r.stderr  # names WHICH file
    assert not (eh / "knowledge.sql").exists() and not (eh / ".gitignore").exists()
    assert git(eh, "rev-parse", "HEAD").strip() == head
    assert opens_of(instrument[1], live) == []
    for ln in all_opens(instrument[1]):  # the possibly-partial file is never opened
        assert not SAFETY_RE.search(ln), ln


def test_t4_a_real_timeout_is_indeterminate_too(sandbox, instrument):
    # The client waits 35 s past the owner's own deadline before it gives up; there is no knob (and the
    # tool must not grow one), so this one cell takes ~35 s.
    _base, home, eh = sandbox
    live = flag_on_fixture(eh)
    with SnapshotOwner(eh, "hang") as owner:
        r = run_script(SCRIPT, home, eh, instr=instrument, timeout=100)
    assert owner.snapshot_requests
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "timeout" in r.stderr and FLAG_DOTTED in r.stderr, r.stderr
    assert "may or may not be complete" in r.stderr and "must not be trusted" in r.stderr, r.stderr
    assert not (eh / "knowledge.sql").exists()
    assert opens_of(instrument[1], live) == []


# ---- T5: owner down ---------------------------------------------------------------------------------

def test_t5_owner_down_dies_names_the_flag_and_reads_no_live_store(sandbox, instrument):
    _base, home, eh = sandbox
    live = flag_on_fixture(eh)
    head = seed_git(eh)
    r = run_script(SCRIPT, home, eh, instr=instrument)  # no owner is listening
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert FLAG_DOTTED in r.stderr and "DOWN" in r.stderr, r.stderr
    assert opens_of(instrument[1], live) == []
    assert safety_files(home) == []
    assert not (eh / "knowledge.sql").exists() and not (eh / ".gitignore").exists()
    assert git(eh, "rev-parse", "HEAD").strip() == head


# ---- T6: the flag decides the path, and is read in one place only -----------------------------------

@pytest.mark.parametrize("state", ["absent", "false", "string-true", "malformed-json", "wrong-shape"])
def test_t6_a_flag_that_is_not_the_boolean_true_gives_the_flag_off_path(sandbox, instrument, state):
    _base, home, eh = sandbox
    live = make_store(eh)
    if state == "absent":
        set_config(eh, None)
    elif state == "false":
        set_config(eh, False)
    elif state == "string-true":
        set_config(eh, "true")
    elif state == "malformed-json":
        set_config(eh, raw="{not json")
    else:
        set_config(eh, raw='{"engramd": ["not", "a", "dict"]}')
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert owner.requests == [], "the owner was contacted although the flag is not ON"
    assert len(opens_of(instrument[1], live)) >= 4
    assert r.stderr == "", r.stderr  # absent / malformed config is OFF silently


def test_t6_the_flag_is_read_from_engram_home_not_from_the_default_home(sandbox, instrument):
    _base, home, eh = sandbox
    live = make_store(eh)
    set_config(eh, None)
    decoy = home / ".engram"
    decoy.mkdir()
    (decoy / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: True}}), encoding="utf-8")
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert owner.requests == [] and len(opens_of(instrument[1], live)) >= 4


def test_t6_the_flag_in_engram_home_wins_over_a_false_decoy_in_the_default_home(sandbox, instrument):
    _base, home, eh = sandbox
    live = flag_on_fixture(eh)
    decoy = home / ".engram"
    decoy.mkdir()
    (decoy / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: False}}), encoding="utf-8")
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert len(owner.snapshot_requests) == 1 and opens_of(instrument[1], live) == []


def test_t6_a_broken_runtime_bundle_reads_off_with_one_stderr_line(sandbox, instrument):
    base, home, eh = sandbox
    live = flag_on_fixture(eh)  # the config says ON; the runtime that would read it is broken
    rt = base / "brokenrt"
    rt.mkdir()
    (rt / "engramd_client.py").write_text("", encoding="utf-8")
    (rt / "engramd_owner_flags.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh, instr=instrument, env_extra={"CLAUDE_PLUGIN_ROOT": str(rt)})
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert owner.requests == [] and len(opens_of(instrument[1], live)) >= 4
    lines = [ln for ln in r.stderr.splitlines() if ln]
    assert lines == [f"{FLAG_DOTTED}: engramd_owner_flags failed: RuntimeError"], r.stderr


# ---- T6b: the helper is the real one, and a crash reading the flag is one stable line --------------

_DECOY = ("import os\nopen(os.path.join(os.environ.get('FT10_DECOY_DIR', '.'), 'DECOY_RAN'), 'w').write('x')\n"
          "raise RuntimeError('decoy _owner_read imported')\n")


@pytest.mark.parametrize("where", ["cwd", "pythonpath"])
def test_t6b_a_decoy_owner_read_in_the_cwd_or_on_pythonpath_never_shadows_the_real_helper(sandbox, where):
    base, home, eh = sandbox
    live = flag_on_fixture(eh)
    decoy_dir = base / "decoy"
    decoy_dir.mkdir()
    (decoy_dir / "_owner_read.py").write_text(_DECOY, encoding="utf-8")
    env_extra = {"FT10_DECOY_DIR": str(decoy_dir)}
    kwargs = {}
    if where == "cwd":
        kwargs["cwd"] = decoy_dir
    else:
        env_extra["PYTHONPATH"] = str(decoy_dir)
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh, env_extra=env_extra, **kwargs)
    assert not (decoy_dir / "DECOY_RAN").exists(), "the decoy _owner_read.py was imported"
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert len(owner.snapshot_requests) == 1, "the flag path was not taken: the real helper was shadowed"
    assert "decoy" not in r.stderr and "Traceback" not in r.stderr, r.stderr


def test_t6b_a_crash_reading_the_flag_is_one_stable_line_and_reads_off(sandbox, instrument):
    base, home, eh = sandbox
    live = flag_on_fixture(eh)  # the config says ON; the helper that would read it cannot be imported
    tree = base / "tree"
    (tree / "tools").mkdir(parents=True)
    script = tree / "tools" / "engram-fix-git-backup.sh"
    shutil.copy(SCRIPT, script)  # a tools dir with NO _owner_read.py next to it
    (tree / "engram_backup.py").symlink_to(ENGRAM_BACKUP)
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(script, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)  # the flag-OFF path ran to the end
    assert owner.requests == [] and len(opens_of(instrument[1], live)) >= 4
    assert "Traceback" not in r.stderr, r.stderr
    lines = [ln for ln in r.stderr.splitlines() if ln]
    assert lines == [f"{FLAG_DOTTED}: flag read failed (ModuleNotFoundError); reading as OFF"], r.stderr


# ---- T7: --dry-run under the flag -------------------------------------------------------------------

def test_t7_dry_run_under_the_flag_makes_no_owner_call_opens_nothing_and_writes_nothing(sandbox, instrument):
    _base, home, eh = sandbox
    flag_on_fixture(eh)
    before = sorted(p.name for p in eh.iterdir())
    with SnapshotOwner(eh, "ok") as owner:
        r = run_script(SCRIPT, home, eh, ["--dry-run"], instr=instrument)
        before_run = sorted(p.name for p in eh.iterdir())
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert owner.requests == [], owner.requests
    assert all_opens(instrument[1]) == [], all_opens(instrument[1])
    assert list(home.iterdir()) == []
    assert before_run == sorted(before + ["run"])  # only the fake owner's own socket dir
    m = re.search(r"would ask the engramd owner for a snapshot at (\S+)", r.stdout)
    assert m and SAFETY_RE.search(m.group(1)) and m.group(1).startswith(str(home)), r.stdout
    snap = m.group(1)
    assert f"engram_backup.py dump {snap} " in r.stdout and f"engram_backup.py verify {snap}" in r.stdout, r.stdout
    assert str(eh / "knowledge.db") not in "".join(
        ln for ln in r.stdout.splitlines() if "engram_backup.py" in ln), r.stdout
    assert "quick_check" in r.stdout


# ---- T8: a real owner -------------------------------------------------------------------------------

@requires_linux_owner
def test_t8_real_owner_flag_on_zero_live_opens_and_the_dump_matches(sandbox, instrument):
    _base, home, eh = sandbox
    live = flag_on_fixture(eh)
    with real_owner(eh):
        r = run_script(SCRIPT, home, eh, instr=instrument)
    assert r.returncode == 0, (r.stdout, r.stderr)
    snaps = safety_files(home)
    assert len(snaps) == 1
    assert opens_of(instrument[1], home / snaps[0]), "vacuous: the snapshot was never opened"
    assert opens_of(instrument[1], live) == []
    rows = restore_rows(eh / "knowledge.sql")
    assert len(rows["nodes"]) == N_NODES and rows["edit_history"]
    assert (eh / ".git").is_dir()
