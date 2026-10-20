"""#3962 PR-A: every core store-path user x every class of store path.

The core users of the store-path classifier (`engram_regular_io.check_store_path`):

  * `viz/data/sqlite_ro.connect_ro`  -- boundary adapter: EVERY class, not found
    included, becomes `sqlite3.OperationalError(<classifier message>)` raised
    `from` the classifier's exception (the nine callers do not change);
  * `health/series.export`           -- not found keeps the `no_db` dict byte for
    byte; any `StoreUnusable` becomes `{"reason": "unreadable", ...}`;
  * `engram_telemetry_verdicts.summary_line` -- not found keeps the "not
    aggregated" string; any `StoreUnusable` is re-raised (engram_stats turns it
    into `telemetry: unavailable (...)`).

Classes: ENOENT, chmod-000 parent, ENOTDIR, symlink loop, dangling link, FIFO,
directory, symlink to a real store, real store.

Written BEFORE the fix (contract-first). The expected messages are spelled out
literally here, never computed through the classifier, so a cell cannot go green
by importing the thing it checks. Every call runs in a child python under a hard
timeout: a hang (a FIFO opened by sqlite on macOS) FAILS the cell, never skips it.
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

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

SRC = Path(__file__).resolve().parent.parent / "src" / "engram"
GUARD_S = 10

NOT_AGGREGATED = "telemetry: not aggregated (records.db missing)"

KINDS = ["enoent", "chmod000", "enotdir", "loop", "dangling", "fifo", "dir", "link_to_store", "store"]
REFUSED = ["chmod000", "enotdir", "loop", "dangling", "fifo", "dir"]
OPENABLE = ["link_to_store", "store"]


def _expect(kind: str, p: str):
    """(classifier class name, message prefix) for a refused/missing class, spelled out literally."""
    if kind == "enoent":
        return "FileNotFoundError", f"ENGRAM database not found: {p}"
    if kind == "fifo":
        return "StoreNotAStore", f"the store path {p} is a FIFO, not a regular file"
    if kind == "dir":
        return "StoreNotAStore", f"the store path {p} is a directory, not a regular file"
    symbol = {"chmod000": "EACCES", "enotdir": "ENOTDIR", "loop": "ELOOP", "dangling": "ENOENT"}[kind]
    return "StoreUncheckable", f"cannot check the store at {p} ({symbol}:"


def _make_sqlite(path: Path) -> None:
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()


@pytest.fixture
def plant():
    """plant(kind) -> path string for that class under a fresh temp dir."""
    made: list[Path] = []

    def _plant(kind: str) -> str:
        if kind == "chmod000" and hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        base = Path(tempfile.mkdtemp(prefix="sp3962-"))
        made.append(base)
        if kind == "enoent":
            return str(base / "knowledge.db")
        if kind == "chmod000":
            d = base / "locked"
            d.mkdir()
            (d / "knowledge.db").write_bytes(b"")
            d.chmod(0)
            return str(d / "knowledge.db")
        if kind == "enotdir":
            (base / "afile").write_bytes(b"")
            return str(base / "afile" / "knowledge.db")
        if kind == "loop":
            os.symlink("b", base / "a")
            os.symlink("a", base / "b")
            os.symlink("a", base / "knowledge.db")
            return str(base / "knowledge.db")
        if kind == "dangling":
            os.symlink(str(base / "nowhere"), base / "knowledge.db")
            return str(base / "knowledge.db")
        if kind == "fifo":
            os.mkfifo(base / "knowledge.db")
            return str(base / "knowledge.db")
        if kind == "dir":
            (base / "knowledge.db").mkdir()
            return str(base / "knowledge.db")
        if kind == "link_to_store":
            _make_sqlite(base / "real.db")
            os.symlink(str(base / "real.db"), base / "knowledge.db")
            return str(base / "knowledge.db")
        if kind == "store":
            _make_sqlite(base / "knowledge.db")
            return str(base / "knowledge.db")
        raise AssertionError(kind)

    yield _plant
    for base in made:
        for d in base.rglob("*"):
            try:
                if d.is_dir() and not d.is_symlink():
                    d.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(base, ignore_errors=True)


_PRELUDE = f"""
import sys, json, os, sqlite3
sys.path.insert(0, {str(SRC)!r})
P = sys.argv[1]
"""


def _child(body: str, path: str, *extra: str):
    """Run `body` in a child python with P bound to `path`; it sets `result` (JSON-able).
    A hang is a FAILURE (the child is killed at GUARD_S), never a skip."""
    code = _PRELUDE + body + "\nprint('RESULT:' + json.dumps(result))\n"
    try:
        done = subprocess.run([sys.executable, "-c", code, path, *extra], capture_output=True, text=True,
                              timeout=GUARD_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"did not return within {GUARD_S}s on {path} (blocked on the store path)")
    assert done.returncode == 0, done.stderr
    line = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


# ------------------------------------------------------------------ connect_ro

_CONNECT_RO = r"""
from viz.data.sqlite_ro import connect_ro
try:
    c = connect_ro(P)
    c.execute("SELECT 1").fetchone()
    c.close()
    result = {"opened": True}
except sqlite3.OperationalError as e:
    cause = e.__cause__
    result = {"opened": False, "msg": str(e),
              "cause": type(cause).__name__ if cause is not None else None,
              "cause_msg": str(cause) if cause is not None else None}
"""


@pytest.mark.parametrize("kind", ["enoent"] + REFUSED)
def test_connect_ro_maps_every_refusal_to_operationalerror_from_the_classifier(kind, plant):
    p = plant(kind)
    cls, prefix = _expect(kind, p)
    r = _child(_CONNECT_RO, p)
    assert r["opened"] is False, r
    assert r["cause"] == cls, r
    assert r["msg"].startswith(prefix), r
    assert r["msg"] == r["cause_msg"], r


@pytest.mark.parametrize("kind", OPENABLE)
def test_connect_ro_opens_a_real_store_and_a_link_to_one(kind, plant):
    assert _child(_CONNECT_RO, plant(kind)) == {"opened": True}


# ------------------------------------------------- two viz callers, end to end

_HEALTH = r"""
from viz.data.health import get_health_data
result = get_health_data(P)
"""

_MOMENTS = r"""
from viz.data import moments
try:
    result = {"returned": moments.has_moments(P)}
except sqlite3.OperationalError as e:
    result = {"raised": "OperationalError", "msg": str(e)}
"""


def test_viz_health_missing_behaves_as_before(plant):
    p = plant("enoent")
    assert _child(_HEALTH, p) == {"error": f"Database not found: {p}"}


def test_viz_moments_missing_behaves_as_before(plant):
    assert _child(_MOMENTS, plant("enoent")) == {"returned": False}


@pytest.mark.parametrize("kind,word", [("fifo", "FIFO"), ("dir", "directory")])
def test_viz_health_returns_promptly_with_the_classifier_message(kind, word, plant):
    p = plant(kind)
    r = _child(_HEALTH, p)
    assert r["error"].startswith(_expect(kind, p)[1]), r
    assert word in r["error"]


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_viz_moments_raises_promptly_with_the_classifier_message(kind, plant):
    p = plant(kind)
    r = _child(_MOMENTS, p)
    assert r.get("raised") == "OperationalError", r
    assert r["msg"].startswith(_expect(kind, p)[1]), r


# ---------------------------------------------------------------- series.export

_EXPORT = r"""
from health import series
try:
    result = {"returned": series.export(P)}
except BaseException as e:
    result = {"raised": type(e).__name__}
"""

_EXPORT_REAL = r"""
from health import series
real = P + ".real"
series.open_store(real).close()
if sys.argv[2] == "link":
    os.symlink(real, P)
else:
    os.rename(real, P)
result = {"returned": series.export(P)}
"""


def test_series_export_enoent_keeps_the_no_db_dict_byte_identical(plant):
    p = plant("enoent")
    r = _child(_EXPORT, p)
    assert r == {"returned": {"error": f"series store not found at {p}", "reason": "no_db", "db_path": p}}
    assert json.dumps(r["returned"], sort_keys=True) == json.dumps(
        {"error": f"series store not found at {p}", "reason": "no_db", "db_path": p}, sort_keys=True)


@pytest.mark.parametrize("kind", REFUSED)
def test_series_export_reports_unreadable_with_the_classifier_message(kind, plant):
    p = plant(kind)
    r = _child(_EXPORT, p)
    assert "returned" in r, r
    got = r["returned"]
    assert got.get("reason") == "unreadable", got
    assert got["error"].startswith(_expect(kind, p)[1]), got
    assert got["db_path"] == p, got


@pytest.mark.parametrize("kind", OPENABLE)
def test_series_export_reads_a_real_store_and_a_link_to_one(kind, plant):
    p = plant("enoent")  # a fresh, empty slot: the child builds the series store itself
    mode = "link" if kind == "link_to_store" else "plain"
    r = _child(_EXPORT_REAL, p, mode)
    assert r["returned"].get("reason") not in ("no_db", "unreadable"), r


# ----------------------------------------------------------------- summary_line

_SUMMARY = r"""
import engram_telemetry_verdicts as v
try:
    result = {"returned": v.summary_line(P)}
except BaseException as e:
    result = {"raised": type(e).__name__, "mro": [c.__name__ for c in type(e).__mro__], "msg": str(e)}
"""


def test_summary_line_enoent_returns_the_existing_not_aggregated_string(plant):
    assert _child(_SUMMARY, plant("enoent")) == {"returned": NOT_AGGREGATED}


@pytest.mark.parametrize("kind", REFUSED)
def test_summary_line_raises_the_classifier_exception_for_an_unusable_path(kind, plant):
    p = plant(kind)
    cls, prefix = _expect(kind, p)
    r = _child(_SUMMARY, p)
    assert r.get("raised") == cls, r
    assert "StoreUnusable" in r["mro"], r
    assert r["msg"].startswith(prefix), r


@pytest.mark.parametrize("kind", OPENABLE)
def test_summary_line_on_a_real_store_and_a_link_is_unchanged(kind, plant):
    # a bare sqlite file has no verdicts table: today's answer, reached through a normal open
    assert _child(_SUMMARY, plant(kind)) == {"returned": NOT_AGGREGATED}


def test_engram_stats_diagnose_says_unavailable_for_an_unusable_telemetry_path():
    from conftest import fresh_module
    import engram_stats

    test_dir = tempfile.mkdtemp(prefix="engram_test_3962a_")
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        stats = fresh_module(engram_stats)
        core = sys.modules["engram_core"]
        core._configure_paths(test_dir)
        core._ensure_data_dir()
        import engram_telemetry_verdicts as tv

        planted = Path(test_dir) / "records.db"
        planted.mkdir()  # a directory where the store should be
        from unittest import mock

        with mock.patch.object(tv, "_default_db_path", return_value=planted):
            data = json.loads(stats._diagnose_impl())
        assert data["telemetry"].startswith("telemetry: unavailable ("), data["telemetry"]
        assert "not a regular file" in data["telemetry"], data["telemetry"]
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


# --- src-only layout: only `<repo>/src` on PYTHONPATH (the mode viz/server.py documents) ---------
# There the flat name `engram_regular_io` does not resolve; `engram.engram_regular_io` does. The viz
# opener must fall back to the package-qualified import of the SAME classifier, never to a
# classifier-less open.

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
SRC_ONLY_GUARD_S = 15


def _run_src_only(code: str):
    """Run *code* in a child python: cwd a fresh tmp dir, PYTHONPATH = only `<repo>/src`."""
    cwd = tempfile.mkdtemp(prefix="sp3962-srconly-")
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ENGRAM_HOME")}
        env["PYTHONPATH"] = str(SRC_ROOT)
        return subprocess.run(
            [sys.executable, "-c", code], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=SRC_ONLY_GUARD_S,
        )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def test_viz_server_imports_with_only_src_on_pythonpath():
    r = _run_src_only("import engram.viz.server")
    assert r.returncode == 0, r.stderr[-800:]


def test_connect_ro_classifies_a_fifo_with_only_src_on_pythonpath(plant):
    fifo = plant("fifo")
    code = (
        "import sqlite3, sys\n"
        "from engram.viz.data.sqlite_ro import connect_ro\n"
        f"try:\n    connect_ro({fifo!r})\n"
        "except sqlite3.OperationalError as e:\n"
        "    print(type(e.__cause__).__name__)\n"
        "    sys.exit(0)\n"
        "sys.exit(3)\n"
    )
    r = _run_src_only(code)  # a timeout raises TimeoutExpired: the cell FAILS
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.strip() == "StoreNotAStore", r.stdout
