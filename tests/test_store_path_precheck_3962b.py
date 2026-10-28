"""engram-alpha#3962 PR-B (part of #2652) -- the store-path precheck, mapped by three more `src/engram/tools` tools.

#3960/#3961 added `tools/_owner_read.check_store_path(db_path)`: `lstat`, then `stat`, then `S_ISREG`, and it NEVER
opens the path (a `mode=ro` open of a FIFO blocks forever on macOS).  It raises the built-in FileNotFoundError for
"no directory entry", `StoreUncheckable` for any other lstat/stat failure, `StoreNotAStore` for a FIFO, directory,
socket or device.  The helper never picks an exit code: each tool maps the classes to its own documented codes.
This file covers the tools that gated on `exists()` and then opened the store `mode=ro`:

  backup_knowledge_db (flag OFF)   not found: `error: database not found: <path>`, exit 1   others: `error: <msg>`, 1
  cites_step3                      not found: `REFUSED: no knowledge.db under <home>`, 2     others: `REFUSED: <msg>`, 2
  engram-pkg scope-export          not found: `ERROR: scope-export: source graph not found ...`  others: `ERROR: scope-export: <msg>`, 1
  engram-pkg render                not found: `ERROR: render: package DB not found ...`           others: `ERROR: render: <msg>`, 1

The contract (Borges's rulings on #3962), for every tool x every class:
  1. not found keeps the tool's existing message and exit code byte-for-byte;
  2. uncheckable / not-a-store: the tool's failure code, ONE stderr line carrying the classifier's message (it names
     the path and the errno or file kind), no traceback, never "not found", never a create-a-store hint;
  3. every class returns promptly (a FIFO never hangs);
  4. a real store and a symlink to a real store reach the tool's normal path.

Each cell runs the real CLI as a subprocess with a 15 s timeout; a timeout FAILS the cell, never skips it.

Where a tool reads other inputs from the store's parent directory (cites_step3 reads `<home>/config.json` before the
store check; engram-pkg render needs `<pkg>/` to be a directory), the "parent cannot be searched" and "parent
component is a file" classes are reached through a symlink `<parent>/knowledge.db` whose TARGET sits behind an
unsearchable directory / a regular file.  The classifier sees the same errno (EACCES / ENOTDIR), on the `stat` leg.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOLS_DIR = SRC_ENGRAM / "tools"
TIMEOUT = 15

VARIANTS = ["missing", "noaccess", "notdir", "loop", "dangling", "fifo", "dir", "linkstore", "store"]
BAD = ["noaccess", "notdir", "loop", "dangling", "fifo", "dir"]
WORDS = {
    "noaccess": ("EACCES", "Permission denied"),
    "notdir": ("ENOTDIR", "Not a directory"),
    "loop": ("ELOOP", "Too many levels of symbolic links"),
    "dangling": ("ENOENT", "No such file or directory"),
    "fifo": ("is a FIFO",),
    "dir": ("is a directory",),
}
#: wording that belongs to the not-found / create-a-store family and must not appear for the other classes
NOT_FOUND_MARKERS = ("not found", "no knowledge.db", "Run: sqlite3", "Set $ENGRAM_HOME", "engram-pkg build")

needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the directory permission")


# ---- harness (fixture shapes copied from test_store_path_precheck_3960.py) ---------------------------

def run(argv, env, cwd=None):
    """Run a command with the 15 s timeout.  A timeout FAILS the cell (a hang is the bug under test)."""
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
    d = Path(tempfile.mkdtemp(prefix="sp3962-tpl-", dir="/tmp")).resolve()
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
    d = Path(tempfile.mkdtemp(prefix="sp3962-", dir="/tmp")).resolve()
    try:
        yield d
    finally:
        for name in ("sub", "home", "pkg", "locked"):
            try:
                (d / name).chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


def install_direct(variant: str, slot: Path, template: Path) -> None:
    """Make `slot` the given kind of store path, damaging the slot's own parent where the class needs it."""
    if variant == "missing":
        return
    if variant == "noaccess":
        shutil.copy(template, slot)  # a real store, behind a directory that cannot be searched
        slot.parent.chmod(0)
    elif variant == "notdir":
        slot.parent.rmdir()  # a parent COMPONENT of the store path is a regular file: lstat raises ENOTDIR
        slot.parent.write_text("not a directory", encoding="utf-8")
    else:
        install_common(variant, slot, template)


def install_indirect(variant: str, slot: Path, template: Path) -> None:
    """Like install_direct, but the slot's parent stays a healthy directory: the unsearchable directory / the
    regular-file component sits in the symlink TARGET (lstat of the slot succeeds, stat fails)."""
    if variant == "noaccess":
        locked = slot.parent.parent / "locked"
        locked.mkdir()
        shutil.copy(template, locked / "real.db")
        os.symlink(locked / "real.db", slot)
        locked.chmod(0)
    elif variant == "notdir":
        afile = slot.parent.parent / "afile"
        afile.write_text("not a directory", encoding="utf-8")
        os.symlink(afile / "knowledge.db", slot)
    else:
        install_direct(variant, slot, template)


def install_common(variant: str, slot: Path, template: Path) -> None:
    if variant == "linkstore":
        real = slot.parent.parent / "real-store.db"
        shutil.copy(template, real)
        os.symlink(real, slot)
    elif variant == "loop":
        os.symlink(slot.name, slot)
    elif variant == "dangling":
        os.symlink("no-such-target.db", slot)
    elif variant == "fifo":
        os.mkfifo(slot)
    elif variant == "dir":
        slot.mkdir()
    elif variant == "store":
        shutil.copy(template, slot)
    else:  # pragma: no cover
        raise AssertionError(variant)


def stderr_lines(text: str) -> list:
    """Non-blank stderr lines without the import-time `[engram]` notices some tools print."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("[engram]")]


def refusal_line(r, slot: Path, variant: str, prefix: str, code: int) -> str:
    """The shared assertions for an uncheckable / not-a-store cell; returns the classifier message."""
    assert r.returncode == code, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    line = lines[0]
    assert line.startswith(prefix), line
    msg = line[len(prefix):]
    assert str(slot) in msg, (str(slot), msg)
    for w in WORDS[variant]:
        assert w in msg, (w, msg)
    assert "fix the path" in msg, msg
    for m in NOT_FOUND_MARKERS:
        assert m not in line, (m, line)
    return msg


# ---- the four entry points ---------------------------------------------------------------------------

def setup_backup(variant, base, template):
    sub = base / "sub"
    sub.mkdir()
    slot = sub / "knowledge.db"
    install_direct(variant, slot, template)
    out = base / "out"
    return [TOOLS_DIR / "backup_knowledge_db.py", "--db", slot, "--out-dir", out], make_env(base / "home"), slot, out


def check_backup(variant, r, slot, out):
    if variant == "missing":
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert r.stderr == f"error: database not found: {slot}\n", r.stderr
        assert not out.exists()
    elif variant in ("store", "linkstore"):
        # normal path: the backup ran, wrote the dated archive into the out-dir it created
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr and "fix the path" not in r.stderr, r.stderr
        assert out.is_dir() and list(out.glob("knowledge-*.sql")), (r.stdout, r.stderr)
    else:
        refusal_line(r, slot, variant, "error: ", 1)
        assert not out.exists(), "a refusal must not create the out-dir"
        assert r.stdout == ""


def setup_cites(variant, base, template):
    home = base / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent_name": "borges", "self_lineage": "anthropic:opus"}),
                                      encoding="utf-8")
    (base / "report.json").write_text(json.dumps(
        {"seat": "borges", "lineage": "anthropic:opus", "frame": {"seed": 1}, "strata": [], "edges": []}),
        encoding="utf-8")
    slot = home / "knowledge.db"
    install_indirect(variant, slot, template)
    return [TOOLS_DIR / "cites_step3.py", "--report", base / "report.json"], make_env(home), slot, home


def check_cites(variant, r, slot, home):
    if variant == "missing":
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert r.stderr == f"REFUSED: no knowledge.db under {home}\n", r.stderr
    elif variant in ("store", "linkstore"):
        # Past the store check: the config / report / store refusals are all behind us, the engine is loaded and
        # the read-only labels/ancestry phases run to the plan ("status:" line), exit 0.
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "REFUSED" not in r.stderr and "Traceback" not in r.stderr, r.stderr
        assert "status:" in r.stdout, r.stdout
    else:
        refusal_line(r, slot, variant, "REFUSED: ", 2)


def setup_scope_export(variant, base, template):
    sub = base / "sub"  # = ENGRAM_HOME, so the slot is <home>/knowledge.db
    sub.mkdir()
    slot = sub / "knowledge.db"
    install_direct(variant, slot, template)
    return ([TOOLS_DIR / "engram-pkg" / "engram-pkg", "scope-export", "--nodes", "ob_x", "--out", base / "pkg-out"],
            make_env(sub), slot, base / "pkg-out")


def check_scope_export(variant, r, slot, out):
    if variant == "missing":
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert r.stderr == (f"ERROR: scope-export: source graph not found at {slot}. "
                            f"Set $ENGRAM_HOME to point at your ENGRAM data directory.\n"), r.stderr
        assert not out.exists()
    elif variant in ("store", "linkstore"):
        # Past the store check: the next known step prints the closure line before it opens the store.
        assert f"scope-export: computing closure from {slot} ..." in r.stdout, (r.stdout, r.stderr)
        assert "Traceback" not in r.stderr and "fix the path" not in r.stderr, r.stderr
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    else:
        refusal_line(r, slot, variant, "ERROR: scope-export: ", 1)
        assert r.stdout == "" and not out.exists()


def setup_render(variant, base, template):
    pkg = base / "pkg"
    pkg.mkdir()
    slot = pkg / "knowledge.db"
    install_indirect(variant, slot, template)
    (base / "manifest.json").write_text(json.dumps(
        {"manifest_version": 1, "title": "t", "authors": [], "abstract_node_id": None,
         "sections": [{"title": "s", "node_ids_in_order": ["ob_x"]}]}), encoding="utf-8")
    (base / "prose.json").write_text("[]", encoding="utf-8")  # no sourced block for ob_x: validation fails AFTER the read
    return ([TOOLS_DIR / "engram-pkg" / "engram-pkg", "--pkg", pkg, "render", "--manifest", base / "manifest.json",
             "--prose", base / "prose.json"], make_env(base / "home"), slot, pkg)


def check_render(variant, r, slot, pkg):
    if variant == "missing":
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert r.stderr == f"ERROR: render: package DB not found at {slot}. Run `engram-pkg build` first.\n", r.stderr
    elif variant in ("store", "linkstore"):
        # Past the store check: the package DB was read (ob_x resolved, so there is NO "not found in package DB"
        # line) and the run stopped at the next known step, the prose-coverage validation.
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert r.stderr == ("ERROR: render: validation failed — nothing written.\n"
                            "  - manifest node 'ob_x' has no sourced block in --prose\n"), r.stderr
    else:
        refusal_line(r, slot, variant, "ERROR: render: ", 1)
        assert r.stdout == ""


TOOLS = {
    "backup": (setup_backup, check_backup),
    "cites_step3": (setup_cites, check_cites),
    "scope_export": (setup_scope_export, check_scope_export),
    "render": (setup_render, check_render),
}


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("tool", list(TOOLS))
def test_c_each_tool_maps_the_store_path_classes(tool, variant, base, store_template):
    if variant == "noaccess" and os.geteuid() == 0:
        pytest.skip("root bypasses the directory permission")
    setup, check = TOOLS[tool]
    argv, env, slot, extra = setup(variant, base, store_template)
    r = run(argv, env, cwd=base)  # a FIFO that hangs fails here, on the timeout
    check(variant, r, slot, extra)


# ---- cites_step3: every read-only connect reads the path the precheck checked -------------------------

def test_d_cites_step3_every_ro_connect_reads_the_checked_path_or_a_file_it_made():
    """The precheck covers `<home>/knowledge.db`; `core._configure_paths(home)` makes `core.DB_PATH` exactly that.
    The four `_ro_uri(...)` connects must open only `core.DB_PATH` (directly, as `self.core.DB_PATH`, or as
    `_backup_db`'s `src_path` / `_edge_count`'s `path`, whose callers pass `core.DB_PATH` or the backup file this run
    created).  A fifth connect, or one aimed anywhere else, makes this fail and needs its own review."""
    src = (TOOLS_DIR / "cites_step3.py").read_text(encoding="utf-8")
    args = re.findall(r"sqlite3\.connect\(_ro_uri\(([^)]*)\)", src)
    assert sorted(args) == sorted(["self.core.DB_PATH", "src_path", "path", "core.DB_PATH"]), args
    # the callers of the two parameterised helpers
    assert re.findall(r"_backup_db\(([^)]*)\)", src.split("def take_backup", 1)[1]) == ["db_path, dest"]
    assert re.findall(r"take_backup\(([^)]*)\)", src.split("def _main", 1)[1]) == ["core.DB_PATH, out_dir, ts"]
    assert re.findall(r"_edge_count\(([^)]*)\)", src.split("def take_backup", 1)[1]) == ["db_path", "dest"]
    driver = ("import sys; sys.path.insert(0, sys.argv[1]); import engram_core as c; from pathlib import Path; "
              "c._configure_paths(Path(sys.argv[2])); print(c.DB_PATH)")
    r = subprocess.run([sys.executable, "-c", driver, str(SRC_ENGRAM), "/tmp/h"], capture_output=True, text=True,
                       timeout=60, env=make_env(Path("/tmp/h")))
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == str(Path("/tmp/h") / "knowledge.db"), r


# ---- engram-pkg: the classifier import must not widen the startup failure surface ---------------------

def test_e_engram_pkg_help_survives_a_foreign_tools_package(base):
    """The classifier is imported only by the two subcommands that use it. A foreign `tools` package earlier on
    sys.path must not break every other subcommand (here `--help`) at startup."""
    fake = base / "fake"
    (fake / "tools").mkdir(parents=True)
    (fake / "tools" / "__init__.py").write_text("", encoding="utf-8")
    env = make_env(base / "home")
    env["PYTHONPATH"] = str(fake)
    r = run([TOOLS_DIR / "engram-pkg" / "engram-pkg", "--help"], env, cwd=base)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr and "scope-export" in r.stdout, (r.stdout, r.stderr)


@needs_nonroot
def test_f_render_with_an_unsearchable_package_dir(base, store_template):
    """The direct (non-symlink) form: the package directory itself is chmod 000, so `<pkg>/knowledge.db` cannot be
    looked up (lstat EACCES). One level up (the package dir's PARENT unsearchable) is refused earlier, by
    `resolve_pkg`'s own `is_dir()`, a pre-existing traceback shared by every subcommand and outside #3962."""
    pkg = base / "pkg"
    pkg.mkdir()
    shutil.copy(store_template, pkg / "knowledge.db")
    (base / "manifest.json").write_text("{}", encoding="utf-8")
    (base / "prose.json").write_text("[]", encoding="utf-8")
    pkg.chmod(0)
    r = run([TOOLS_DIR / "engram-pkg" / "engram-pkg", "--pkg", pkg, "render", "--manifest", base / "manifest.json",
             "--prose", base / "prose.json"], make_env(base / "home"), cwd=base)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1 and lines[0].startswith("ERROR: render: "), r.stderr
    assert str(pkg / "knowledge.db") in lines[0] and "EACCES" in lines[0] and "Permission denied" in lines[0], lines[0]
