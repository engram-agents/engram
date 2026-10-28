"""engram-alpha#3963 -- cohort_dispatch `prepare` / `verify-in` on a regular file that is not a usable store.

`open_reader` runs `check_store_path` first (#3960), so a missing / uncheckable / not-a-store path is refused
before any open.  A REGULAR file passes that check and then fails inside SQLite with an `sqlite3.Error`:

  text file                    -> DatabaseError  "file is not a database"
  SQLite file, no `nodes`      -> OperationalError "no such table: nodes"
  regular file, mode 000       -> OperationalError "unable to open database file"

Contract, flag OFF (the default):
  prepare    one stderr line `error: cannot read the store <path>: <sqlite message>`, exit 1, no artifact,
             no traceback.
  verify-in  the single named failure {"chunk": "all", "check": "db_access", "error": <names path + sqlite
             error>}; `passed` false; NO per-node "not found in DB" verdicts (an unusable store is not an
             empty answer); no traceback.

Every cell runs the real CLI as a subprocess with a 15 s timeout; a timeout FAILS the cell.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOL = SRC_ENGRAM / "tools" / "cohort_dispatch.py"
TIMEOUT = 15

SHAPES = ["text", "no_nodes_table", "mode000"]
# the sqlite message each shape must surface in the error text (the contract names path AND sqlite error)
SQLITE_TEXT = {
    "text": "not a database",
    "no_nodes_table": "no such table",
    "mode000": "unable to open",
}
needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file mode 000")


def shape_param(name):
    marks = [needs_nonroot] if name == "mode000" else []
    return pytest.param(name, marks=marks, id=name)


SHAPE_PARAMS = [shape_param(s) for s in SHAPES]


# ---- harness ---------------------------------------------------------------------------------------

def run(argv, env):
    try:
        return subprocess.run([sys.executable, *map(str, argv)], capture_output=True, text=True, env=env,
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"timed out after {TIMEOUT} s (a hang): {argv}\nstdout={exc.stdout!r}\nstderr={exc.stderr!r}")


def make_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    return env


BUILD_STORE = r'''
import sqlite3, sys
sys.path.insert(0, sys.argv[1])
import engram_core
conn = sqlite3.connect(sys.argv[2])
conn.row_factory = sqlite3.Row
engram_core._run_db_one_time_setup(conn)
conn.row_factory = None
conn.execute("INSERT INTO nodes (id, type, claim, created_at, is_current, status, recall_summary, "
             "recall_keywords, embedding) VALUES ('ob_x', 'observation_factual', 'c', '2026-01-01T00:00:00', "
             "1, 'active', 's', '[\"k\"]', '[0.1]')")
conn.commit()
conn.close()
'''


@pytest.fixture(scope="session")
def store_template():
    d = Path(tempfile.mkdtemp(prefix="sp3963-tpl-", dir="/tmp")).resolve()
    db = d / "knowledge.db"
    r = subprocess.run([sys.executable, "-c", BUILD_STORE, str(SRC_ENGRAM), str(db)], capture_output=True,
                       text=True, env=make_env(d), timeout=120)
    assert r.returncode == 0 and db.is_file(), r.stderr
    try:
        yield db
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="sp3963-", dir="/tmp")).resolve()
    try:
        yield d
    finally:
        for p in d.rglob("*"):
            try:
                p.chmod(0o644 if p.is_file() else 0o755)
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


def install(shape: str, slot: Path, template: Path) -> None:
    """Make `slot` the given kind of REGULAR file (it must not exist yet)."""
    if shape == "text":
        slot.write_text("this is not a database, just text\n", encoding="utf-8")
    elif shape == "no_nodes_table":
        conn = sqlite3.connect(slot)
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
        conn.close()
    elif shape == "mode000":
        shutil.copy(template, slot)
        slot.chmod(0)
    elif shape == "store":
        shutil.copy(template, slot)
    else:  # pragma: no cover
        raise AssertionError(shape)


def stderr_lines(text: str) -> list:
    """Non-blank stderr lines without the import-time `[engram]` notices some tools print."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("[engram]")]


def prepare_argv(slot: Path, out: Path):
    return [TOOL, "prepare", "--ids", "ob_x", "--out", out, "--db", slot]


def verify_argv(slot: Path, out: Path):
    return [TOOL, "verify-in", "--out", out, "--db", slot]


def make_prepared(base: Path, template: Path):
    """Prepare a cohort ({ob_x}, claim 'c') against a real store; return (out_dir, env)."""
    home = base / "home"
    home.mkdir()
    real = base / "real.db"
    shutil.copy(template, real)
    out = base / "out"
    env = make_env(home)
    r = run(prepare_argv(real, out), env)
    assert r.returncode == 0, ("prepare for verify-in failed", r.stdout, r.stderr)
    real.unlink()
    return out, env


# ---- prepare ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("shape", SHAPE_PARAMS)
def test_prepare_refuses_unusable_regular_store(shape, base, store_template):
    home = base / "home"
    home.mkdir()
    slot = base / "knowledge.db"
    install(shape, slot, store_template)
    out = base / "out"
    r = run(prepare_argv(slot, out), make_env(home))
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == ""
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    assert lines[0].startswith(f"error: cannot read the store {slot}: "), lines[0]
    assert SQLITE_TEXT[shape] in lines[0], lines[0]
    assert "not found" not in lines[0]
    assert not (out / "manifest.json").exists()  # no artifact written


def test_prepare_valid_store_control(base, store_template):
    home = base / "home"
    home.mkdir()
    slot = base / "knowledge.db"
    install("store", slot, store_template)
    out = base / "out"
    r = run(prepare_argv(slot, out), make_env(home))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert json.loads(r.stdout)["chunks"]
    assert (out / "manifest.json").is_file()


# ---- verify-in -------------------------------------------------------------------------------------

@pytest.mark.parametrize("shape", SHAPE_PARAMS)
def test_verify_in_names_unusable_regular_store(shape, base, store_template):
    out, env = make_prepared(base, store_template)
    slot = base / "knowledge.db"
    install(shape, slot, store_template)
    r = run(verify_argv(slot, out), env)
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    res = json.loads(r.stdout)
    assert res["passed"] is False, res
    errs = [e for e in res["errors"] if e["check"] == "db_access"]
    assert len(errs) == 1 and errs[0]["chunk"] == "all", res["errors"]
    assert str(slot) in errs[0]["error"], errs[0]
    assert SQLITE_TEXT[shape] in errs[0]["error"], errs[0]  # names the sqlite error, not just the path
    # an unusable store is not an empty answer: no per-node verdict, and the only error is db_access
    assert not [e for e in res["errors"] if e["check"] == "content_integrity"], res["errors"]
    assert "not found in DB" not in r.stdout, r.stdout
    assert len(res["errors"]) == 1, res["errors"]


def test_verify_in_valid_store_control(base, store_template):
    out, env = make_prepared(base, store_template)
    slot = base / "knowledge.db"
    install("store", slot, store_template)
    r = run(verify_argv(slot, out), env)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert json.loads(r.stdout)["passed"] is True


def test_verify_in_reaches_content_check_proof(base, store_template):
    """Proves the cohort above reaches check 3 (content integrity) with a non-empty id set: against a VALID
    store, tamper the payload claim and the run reports a content_integrity mismatch.  The unusable-store cells
    use the very same prepared cohort, so the lone db_access error there is the check-3 read failing."""
    out, env = make_prepared(base, store_template)
    slot = base / "knowledge.db"
    install("store", slot, store_template)
    payload = next(out.glob("chunk-*/payload.json"))
    entries = json.loads(payload.read_text())
    assert [e["id"] for e in entries] == ["ob_x"]
    entries[0]["claim"] = "a different claim"
    payload.write_text(json.dumps(entries))
    r = run(verify_argv(slot, out), env)
    assert r.returncode == 1, (r.stdout, r.stderr)
    res = json.loads(r.stdout)
    assert [e["check"] for e in res["errors"]] == ["content_integrity"], res["errors"]


# ---- the catch stays narrow (Luria's round-2 cells) -------------------------------------------------
# In-process (monkeypatch `_fetch_nodes`, call `cohort_dispatch.main([...])`): the file's subprocess style cannot
# swap a function, and an in-process call is the simplest way to inject a non-sqlite failure.  The prepared
# cohort for verify-in is still built by the real CLI in a subprocess (make_prepared).

def load_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cohort_dispatch_3963_narrow", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def boom(*_a, **_k):
    raise ValueError("boom")


def test_prepare_non_sqlite_error_propagates(base, store_template, monkeypatch):
    """A non-sqlite exception from `_fetch_nodes` must NOT become `error: cannot read the store ...`: the catch is
    `sqlite3.Error` only.  The --db is a VALID store, so the store precheck passes and the call is reached."""
    mod = load_tool()
    monkeypatch.setattr(mod, "_fetch_nodes", boom)
    monkeypatch.setenv("ENGRAM_HOME", str(base))
    slot = base / "knowledge.db"
    install("store", slot, store_template)
    with pytest.raises(ValueError, match="boom"):
        mod.main(["prepare", "--ids", "ob_x", "--out", str(base / "out"), "--db", str(slot)])
    assert not (base / "out" / "manifest.json").exists()


def test_verify_in_non_sqlite_error_propagates(base, store_template, monkeypatch):
    """Same for verify-in: reaching check 3 (the prepared cohort has id ob_x, a valid store is present) the
    ValueError propagates rather than becoming a `db_access` error."""
    out, _env = make_prepared(base, store_template)
    slot = base / "knowledge.db"
    install("store", slot, store_template)
    mod = load_tool()
    monkeypatch.setattr(mod, "_fetch_nodes", boom)
    monkeypatch.setenv("ENGRAM_HOME", str(base / "home"))
    with pytest.raises(ValueError, match="boom"):
        mod.main(["verify-in", "--out", str(out), "--db", str(slot)])
