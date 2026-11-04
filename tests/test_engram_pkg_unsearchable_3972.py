"""engram-alpha#3972 -- engram-pkg prints an `ERROR:` line, not a traceback, when a directory on the package path
cannot be searched (chmod 000).

Two path checks raised a raw PermissionError:
  1. `resolve_pkg` called `pkg_path.is_dir()`; with the package dir's PARENT unsearchable the stat raises.
     Every subcommand goes through it (cells a: stats, list, render).
  2. `_check_stale_build` called `<pkg>/knowledge.db`.exists(); with the package dir ITSELF unsearchable it raises.
     list, query, inspect and stats call it right after resolve_pkg (cells b).
Controls (cells c): a missing package dir keeps its 'is not a directory' message; a normal package still works;
and a foreign `tools` package on sys.path still cannot break the subcommands that go through _check_stale_build.

Each cell runs the real CLI as a subprocess (15 s timeout; a timeout FAILS the cell).  Fixture shapes are copied from
test_store_path_precheck_3962b.py.  chmod 000 does not bind root, so those cells skip as root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
PKG_CLI = SRC_ENGRAM / "tools" / "engram-pkg" / "engram-pkg"
TIMEOUT = 15

needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the directory permission")


def run(argv, env, cwd=None):
    try:
        return subprocess.run([sys.executable, *map(str, argv)], capture_output=True, text=True, env=env,
                              cwd=cwd, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"timed out after {TIMEOUT} s (a hang): {argv}\nstdout={exc.stdout!r}\nstderr={exc.stderr!r}")


def make_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH", "ENGRAM_PKG")}
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
    d = Path(tempfile.mkdtemp(prefix="sp3972-tpl-", dir="/tmp")).resolve()
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
    d = Path(tempfile.mkdtemp(prefix="sp3972-", dir="/tmp")).resolve()
    try:
        yield d
    finally:
        # restore permissions first so the rmtree can descend
        for name in ("locked", "locked/pkg", "pkg"):
            try:
                (d / name).chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


def make_pkg(pkg: Path, template: Path) -> None:
    pkg.mkdir(parents=True, exist_ok=True)
    shutil.copy(template, pkg / "knowledge.db")
    (pkg / "knowledge.sql").write_text("-- placeholder\n", encoding="utf-8")


def write_render_inputs(base: Path) -> list:
    (base / "manifest.json").write_text(json.dumps(
        {"manifest_version": 1, "title": "t", "authors": [], "abstract_node_id": None,
         "sections": [{"title": "s", "node_ids_in_order": ["ob_x"]}]}), encoding="utf-8")
    (base / "prose.json").write_text("[]", encoding="utf-8")
    return ["--manifest", base / "manifest.json", "--prose", base / "prose.json"]


def subcommand_argv(name: str, base: Path) -> list:
    return {
        "stats": ["stats"],
        "list": ["list"],
        "query": ["query", "c"],
        "inspect": ["inspect", "ob_x"],
        "render": ["render", *write_render_inputs(base)],
    }[name]


def assert_clean_error(r, needle=None):
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    out = r.stdout + r.stderr
    assert "ERROR:" in out, out
    assert "Traceback" not in out and "PermissionError" not in out, out
    if needle:
        assert needle in out, out


# (a) the package dir's PARENT is unsearchable: resolve_pkg
@needs_nonroot
@pytest.mark.parametrize("sub", ["stats", "list", "render"])
def test_a_unsearchable_parent_fails_cleanly_in_resolve_pkg(sub, base, store_template):
    pkg = base / "locked" / "pkg"
    make_pkg(pkg, store_template)
    argv = subcommand_argv(sub, base)
    (base / "locked").chmod(0)
    r = run([PKG_CLI, "--pkg", pkg, *argv], make_env(base / "home"), cwd=base)
    assert_clean_error(r, "cannot check the package directory")
    # "cannot check" must stay distinct from "not there"
    assert "is not a directory" not in r.stdout + r.stderr


# (b) the package dir ITSELF is unsearchable: _check_stale_build
@needs_nonroot
@pytest.mark.parametrize("sub", ["list", "query", "inspect", "stats"])
def test_b_unsearchable_package_dir_fails_cleanly_in_check_stale_build(sub, base, store_template):
    pkg = base / "pkg"
    make_pkg(pkg, store_template)
    argv = subcommand_argv(sub, base)
    pkg.chmod(0)
    r = run([PKG_CLI, "--pkg", pkg, *argv], make_env(base / "home"), cwd=base)
    assert_clean_error(r, str(pkg / "knowledge.db"))


# (c) controls
def test_c_missing_package_dir_keeps_the_not_a_directory_message(base):
    pkg = base / "nope"
    r = run([PKG_CLI, "--pkg", pkg, "stats"], make_env(base / "home"), cwd=base)
    assert_clean_error(r, f"ERROR: {pkg} is not a directory.")


def test_c_package_path_under_a_regular_file_keeps_the_not_a_directory_message(base):
    # NotADirectoryError (a file in the middle of the path) is "not there", not "cannot check".
    afile = base / "afile"
    afile.write_text("x", encoding="utf-8")
    pkg = afile / "sub"
    r = run([PKG_CLI, "--pkg", pkg, "stats"], make_env(base / "home"), cwd=base)
    assert_clean_error(r, f"ERROR: {pkg} is not a directory.")
    assert "cannot check" not in r.stdout + r.stderr


@pytest.mark.parametrize("sub", ["list", "stats"])
def test_c_package_without_a_db_is_not_a_stale_build_error(sub, base):
    # A missing knowledge.db must make _check_stale_build return (the build-from-sql path relies on it), not exit.
    pkg = base / "pkg"
    pkg.mkdir()
    (pkg / "knowledge.sql").write_text("-- placeholder\n", encoding="utf-8")
    r = run([PKG_CLI, "--pkg", pkg, *subcommand_argv(sub, base)], make_env(base / "home"), cwd=base)
    out = r.stdout + r.stderr
    assert "cannot check the store" not in out and "Traceback" not in out, out


@needs_nonroot
def test_a_resolve_pkg_exits_on_its_error_line(base, store_template):
    # resolve_pkg must stop at its own ERROR line: nothing after it, so a print-and-continue would be caught.
    pkg = base / "locked" / "pkg"
    make_pkg(pkg, store_template)
    (base / "locked").chmod(0)
    r = run([PKG_CLI, "--pkg", pkg, "stats"], make_env(base / "home"), cwd=base)
    lines = (r.stdout + r.stderr).strip().splitlines()
    assert r.returncode != 0 and lines, (r.returncode, lines)
    assert lines[-1].startswith("ERROR: cannot check the package directory"), lines
    assert len([l for l in lines if l.startswith("ERROR:")]) == 1, lines


def test_c_package_path_that_is_a_file_keeps_the_not_a_directory_message(base):
    pkg = base / "afile"
    pkg.write_text("x", encoding="utf-8")
    r = run([PKG_CLI, "--pkg", pkg, "stats"], make_env(base / "home"), cwd=base)
    assert_clean_error(r, f"ERROR: {pkg} is not a directory.")


@pytest.mark.parametrize("sub", ["list", "inspect", "stats"])
def test_c_foreign_tools_package_does_not_break_stale_build_subcommands(sub, base, store_template):
    # _load_owner_read imports `tools` only for scope-export and render, so a foreign `tools` package earlier on
    # sys.path cannot break the other subcommands. The #3972 check in _check_stale_build must keep that true.
    # query is left out: its happy path loads the search model, and a cold load can pass the 15 s timeout;
    # it goes through the same _check_stale_build as these three.
    pkg = base / "pkg"
    make_pkg(pkg, store_template)
    shadow = base / "shadow" / "tools"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise ImportError('foreign tools package')\n", encoding="utf-8")
    env = make_env(base / "home")
    env["PYTHONPATH"] = str(base / "shadow")
    r = run([PKG_CLI, "--pkg", pkg, *subcommand_argv(sub, base)], env, cwd=base)
    out = r.stdout + r.stderr
    assert "foreign tools package" not in out and "Traceback" not in out, out


def test_c_normal_package_stats_still_works(base, store_template):
    pkg = base / "pkg"
    make_pkg(pkg, store_template)
    r = run([PKG_CLI, "--pkg", pkg, "stats"], make_env(base / "home"), cwd=base)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr and "ERROR:" not in r.stderr, r.stderr
