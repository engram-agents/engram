"""#3962 PR-A part 4: `engramd_owner._refuse_non_regular_store` converged on the core classifier.

The refusal is behaviour-preserving except for ONE deliberate change: a symlink loop.
Today (Python <= 3.12) `Path(p).resolve()` raises RuntimeError on a 2-link loop, which
escapes the `except OSError` and tracebacks at owner start; on 3.14 `resolve()` returns,
`os.stat` raises ELOOP, and the path passes through. The converged function uses
`lstat`/`stat` directly (no `resolve()`), so a loop is uncheckable and passes through
on every version.

The table below was MEASURED on the unchanged code before any edit (macOS, Python 3.12);
every row except `loop` is unchanged by this PR:

  class            _refuse_non_regular_store     --preflight      _connect_store_ro
  ENOENT           None                          rc 0             sqlite OperationalError
  chmod-000 parent None                          (fails earlier)  sqlite OperationalError
  ENOTDIR          None                          (fails earlier)  sqlite OperationalError
  loop             RuntimeError  <- CHANGED      traceback, rc 1  RuntimeError  <- CHANGED
  dangling link    None                          rc 0             sqlite OperationalError
  FIFO             OwnerStorePathNotRegular      rc 4             OwnerStorePathNotRegular
  directory        OwnerStorePathNotRegular      rc 4             OwnerStorePathNotRegular
  link to a store  None                          rc 0             opens
  real store       None                          rc 0             opens

Every call that could block runs in a child python under a hard timeout: a hang FAILS.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SRC_ENGRAM = Path(__file__).resolve().parent.parent / "src" / "engram"
if str(_SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(_SRC_ENGRAM))

OWNER_PY = _SRC_ENGRAM / "engramd_owner.py"
TIMEOUT = 15.0

KINDS = ["enoent", "chmod000", "enotdir", "loop", "dangling", "fifo", "dir", "link_to_store", "store"]
REFUSED = {"fifo": "fifo", "dir": "directory"}


@pytest.fixture
def plant():
    made: list[Path] = []

    def _plant(kind: str):
        """-> (home, store path). The store path is <home>/knowledge.db except where the class needs a
        different shape (ENOTDIR: the 'home' is a file; chmod-000: the home is unsearchable)."""
        if kind == "chmod000" and hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        base = Path(tempfile.mkdtemp(prefix="os3962-"))
        made.append(base)
        if kind == "enotdir":
            (base / "afile").write_bytes(b"")
            return base / "afile", base / "afile" / "knowledge.db"
        home = base / "h"
        home.mkdir()
        s = home / "knowledge.db"
        if kind == "loop":
            os.symlink("b", home / "a")
            os.symlink("a", home / "b")
            os.symlink("a", s)
        elif kind == "dangling":
            os.symlink(str(home / "nowhere"), s)
        elif kind == "fifo":
            os.mkfifo(s)
        elif kind == "dir":
            s.mkdir()
        elif kind in ("link_to_store", "store"):
            real = home / ("real.db" if kind == "link_to_store" else "knowledge.db")
            c = sqlite3.connect(str(real))
            c.execute("CREATE TABLE t (x)")
            c.commit()
            c.close()
            if kind == "link_to_store":
                os.symlink(str(real), s)
        elif kind == "chmod000":
            s.write_bytes(b"")
            home.chmod(0)
        return home, s

    yield _plant
    for base in made:
        for d in [base, *base.rglob("*")]:
            try:
                if d.is_dir() and not d.is_symlink():
                    d.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(base, ignore_errors=True)


_PRELUDE = f"""
import sys, json
sys.path.insert(0, {str(_SRC_ENGRAM)!r})
import engramd_owner as eo
P = sys.argv[1]
"""

_REFUSE = r"""
try:
    eo._refuse_non_regular_store(P)
    result = {"refuse": None}
except BaseException as e:
    result = {"refuse": type(e).__name__, "msg": str(e)}
"""

_CONNECT = r"""
import sqlite3
try:
    eo._connect_store_ro(P).execute("SELECT 1").fetchone()
    result = {"connect": "opened"}
except sqlite3.OperationalError as e:
    result = {"connect": "sqlite.OperationalError"}
except BaseException as e:
    result = {"connect": type(e).__name__}
"""


def _child(body: str, path) -> dict:
    code = _PRELUDE + body + "\nprint('RESULT:' + json.dumps(result))\n"
    try:
        done = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True,
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.fail(f"did not return within {TIMEOUT}s on {path} (blocked on the store path)")
    assert done.returncode == 0, done.stderr
    line = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


@pytest.mark.parametrize("kind", KINDS)
def test_refuse_non_regular_store_matches_the_measured_table(kind, plant):
    _home, store = plant(kind)
    r = _child(_REFUSE, store)
    if kind in REFUSED:
        assert r["refuse"] == "OwnerStorePathNotRegular", r
        # the SAME message the owner always had: the word comes from the owner's own _file_kind
        assert r["msg"] == (
            f"the store path {store} is not a regular file (it is a {REFUSED[kind]}); "
            f"refusing to open it: a read-only open of a FIFO would block forever"
        ), r
    else:
        # not found, uncheckable (incl. the loop: the one permitted change) and usable all pass through
        assert r == {"refuse": None}, r


@pytest.mark.parametrize("kind", KINDS)
def test_connect_store_ro_matches_the_measured_table(kind, plant):
    _home, store = plant(kind)
    r = _child(_CONNECT, store)
    if kind in REFUSED:
        assert r == {"connect": "OwnerStorePathNotRegular"}, r
    elif kind in ("link_to_store", "store"):
        assert r == {"connect": "opened"}, r
    else:
        # not found / uncheckable (incl. the loop: it now fails fast with a sqlite error on EVERY
        # Python version; on <= 3.12 it was a RuntimeError from resolve())
        assert r == {"connect": "sqlite.OperationalError"}, r


def _preflight(home: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM_")}
    env.update({"ENGRAM_HOME": str(home), "ENGRAM_NO_EMBEDDINGS": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        r = subprocess.run([sys.executable, str(OWNER_PY), "--preflight"], capture_output=True, text=True,
                           timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail(f"--preflight did not return within {TIMEOUT}s")
    return r


@pytest.mark.parametrize("kind,rc", [("enoent", 0), ("loop", 0), ("dangling", 0), ("fifo", 4), ("dir", 4),
                                      ("link_to_store", 0), ("store", 0)])
def test_preflight_exit_codes_match_the_measured_table(kind, rc, plant):
    home, _store = plant(kind)
    r = _preflight(home)
    assert r.returncode == rc, (r.returncode, r.stderr[-400:])
    assert "Traceback" not in r.stderr, r.stderr[-400:]
