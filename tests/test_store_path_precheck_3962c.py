"""engram-alpha#3962 PR-C (part of #2652) -- the store-path precheck, mapped by five repo-root dev tools.

`tools/_owner_read.check_store_path(db_path)` (#3960) classifies a store path with `lstat`, then `stat`, then
`S_ISREG`, and NEVER opens it (a `mode=ro` open of a FIFO blocks forever on macOS).  It raises the built-in
FileNotFoundError for "no directory entry", `StoreUncheckable` for any other lstat/stat failure and `StoreNotAStore`
for a FIFO, directory, socket or device.  The helper never picks an exit code; each tool maps the classes itself:

  A  propose-generalizes-migration.py   not found: the existing 2-line message, exit 2   others: classifier line, exit 2
  B  engramd_a6_latency.py --store      every class: `engramd_a6_latency: error -- <msg>`, exit 2 (before any build)
  C  perf/trace_from_telemetry.py       not found: uniform-mix fallback (exit 0)   others: `[trace_from_telemetry] <msg>`, 1
  D  probes/mods-recall/novel_terms.py  every class: `novel_terms: <msg>`, exit 1 (was a traceback / a hang)
  E  ws4/hook_bridge.py                 every class: `{}` (the documented "any DB problem" answer); this PR only
                                        removes the FIFO hang
  F  engramd_stage_profile.py --store   every class: `refusing: <msg>`, exit 2 (before any build; reuses B's check)
  G  engramd_a6_latency.py --real-store the existing "is not a file" line for what is_file() answers False; an
                                        unsearchable parent (was a PermissionError traceback): `error -- <msg>`, exit 2

The contract, for every tool x every class: (1) it returns promptly, a FIFO never hangs; (2) a real store and a
symlink to a real store reach the tool's normal path; (3) uncheckable / not-a-store never say "not found" and never
suggest creating a store.  Each cell drives the real entry point as a subprocess with a 15 s timeout; a timeout FAILS
the cell, never skips it.  Fixture shapes are copied from test_store_path_precheck_3962b.py.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOLS_DIR = REPO_ROOT / "tools"
TIMEOUT = 15

VARIANTS = ["missing", "noaccess", "notdir", "loop", "dangling", "fifo", "dir", "linkstore", "store"]
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


# ---- harness -----------------------------------------------------------------------------------------

def run(argv, env, cwd=None, stdin=""):
    """Run a command with the 15 s timeout.  A timeout FAILS the cell (a hang is the bug under test)."""
    try:
        return subprocess.run([sys.executable, *map(str, argv)], capture_output=True, text=True, env=env,
                              cwd=cwd, timeout=TIMEOUT, input=stdin)
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
conn.execute("INSERT INTO nodes (id, type, claim, created_at, is_current, status, memory_status, metadata) "
             "VALUES ('ls_x', 'lesson', 'l', '2026-01-01T00:00:00', 1, 'active', 'active', "
             "'{\"situation_pattern\": \"foo\"}')")
conn.commit()
import engram_idf
engram_idf.ensure_vocab_table(conn)  # novel_terms reads the fts5vocab table; the server creates it lazily
conn.commit()
conn.close()
'''


@pytest.fixture(scope="session")
def store_template():
    d = Path(tempfile.mkdtemp(prefix="sp3962c-tpl-", dir="/tmp")).resolve()
    db = d / "knowledge.db"
    r = subprocess.run([sys.executable, "-c", BUILD_STORE, str(SRC_ENGRAM), str(db)], capture_output=True,
                       text=True, env=make_env(d), timeout=120)
    assert r.returncode == 0 and db.is_file(), r.stderr
    try:
        yield db
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _reap(marker: str) -> list:
    """Kill every process whose argv mentions this cell's temp dir; return their pids.  A cell that times out
    kills only the CLI it started: a6's layout child runs in its own process group and would stay blocked on
    the FIFO forever, so the teardown finds it by the path it was handed."""
    # -ww: unlimited width. Without it procps ps (Linux) caps each line at 80 columns when not on a terminal,
    # so the temp-dir marker deep in a layout child's argv is never seen (measured inside pytest on Linux).
    out = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True).stdout
    reaped = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if marker in args and pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGKILL)
                reaped.append(int(pid))
            except OSError:
                pass
    return reaped


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="sp3962c-", dir="/tmp")).resolve()
    try:
        yield d
    finally:
        _reap(str(d))
        for name in ("sub", "home", "locked"):
            try:
                (d / name).chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


def install(variant: str, slot: Path, template: Path) -> None:
    """Make `slot` the given kind of store path, damaging the slot's own parent where the class needs it."""
    if variant == "missing":
        return
    if variant == "noaccess":
        shutil.copy(template, slot)  # a real store, behind a directory that cannot be searched
        slot.parent.chmod(0)
    elif variant == "notdir":
        slot.parent.rmdir()  # a parent COMPONENT of the store path is a regular file: lstat raises ENOTDIR
        slot.parent.write_text("not a directory", encoding="utf-8")
    elif variant == "linkstore":
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


def new_slot(base: Path, variant: str, template: Path) -> Path:
    sub = base / "sub"
    sub.mkdir()
    slot = sub / "knowledge.db"
    install(variant, slot, template)
    return slot


# ---- A: propose-generalizes-migration.py ---------------------------------------------------------------

def setup_a(variant, base, template):
    slot = new_slot(base, variant, template)
    out = base / "report.md"
    return [TOOLS_DIR / "propose-generalizes-migration.py", "--db", slot, "--out", out], make_env(base / "home"), slot, out


def check_a(variant, r, slot, out):
    if variant == "missing":
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert stderr_lines(r.stderr) == [
            f"ENGRAM database not found: {slot}",
            "Refusing to emit a report — an empty report and an unreadable database must not look alike."], r.stderr
        assert r.stdout == "" and not out.exists()
    elif variant in ("store", "linkstore"):
        # normal path: the store was read and the (empty) report written
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr and "fix the path" not in r.stderr, r.stderr
        assert "proposed 0 candidate(s)" in r.stdout and out.is_file(), (r.stdout, r.stderr)
    else:
        refusal_line(r, slot, variant, "", 2)
        assert r.stdout == "" and not out.exists(), "a refusal must emit no report"


# ---- B: engramd_a6_latency.py --store ------------------------------------------------------------------

def setup_b(variant, base, template):
    slot = new_slot(base, variant, template)
    tmp = base / "tmp"
    tmp.mkdir()
    # Only a usable slot gets ENGRAM_HOME = its directory, so it IS "the live store" and stops at the live-store
    # refusal (see check_b).  Every other shape keeps an unrelated home, so without the precheck it would go on
    # past `_is_live_store` into the build / layout child, as a real `--store` run does.
    env = make_env(slot.parent if variant in ("store", "linkstore") else base / "home")
    env["TMPDIR"] = str(tmp)
    return [TOOLS_DIR / "engramd_a6_latency.py", "--store", slot], env, slot, tmp


def check_b(variant, r, slot, tmp):
    prefix = "engramd_a6_latency: error -- "
    assert not list(tmp.glob("a6-*")), "a refusal must leave no a6-* temp dir"
    if variant == "missing":
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert stderr_lines(r.stderr) == [f"{prefix}ENGRAM database not found: {slot}"], r.stderr
    elif variant in ("store", "linkstore"):
        # Past the store check: the next step in `main` is the live-store refusal.  ENGRAM_HOME is the slot's
        # directory, so a usable slot (or a link to one) resolves to the live store and is refused HERE, before the
        # plugin build.  Reaching this line (and not a store-check line) is the proof the precheck let it through.
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        lines = stderr_lines(r.stderr)
        assert len(lines) == 1 and "is this seat's live store" in lines[0], r.stderr
        assert "fix the path" not in r.stderr and "Traceback" not in r.stderr, r.stderr
    else:
        refusal_line(r, slot, variant, prefix, 2)
        assert r.stdout == ""


# ---- C: perf/trace_from_telemetry.py -------------------------------------------------------------------

def setup_c(variant, base, template):
    slot = new_slot(base, variant, template)
    out = base / "outdir" / "trace.jsonl"
    return ([TOOLS_DIR / "perf" / "trace_from_telemetry.py", "--data-dir", slot.parent, "--out", out,
             "--n-calls", "5", "--seed", "1"], make_env(base / "home"), slot, out)


def check_c(variant, r, slot, out):
    if variant == "missing":
        # not found keeps the documented fallback: a uniform mix over the tools that need no node id
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "WARNING: no real node ids" in r.stderr and "Traceback" not in r.stderr, r.stderr
        assert len(out.read_text().splitlines()) == 5
    elif variant in ("store", "linkstore"):
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr and "fix the path" not in r.stderr, r.stderr
        assert "no real node ids" not in r.stderr, "the store's node ids must have been read"
        assert len(out.read_text().splitlines()) == 5
    else:
        refusal_line(r, slot, variant, "[trace_from_telemetry] ", 1)
        assert not out.exists() and not out.parent.exists(), "a refusal must write no trace file"


# ---- D: probes/mods-recall/novel_terms.py --------------------------------------------------------------

def setup_d(variant, base, template):
    slot = new_slot(base, variant, template)
    state = base / "state" / "seen.json"
    state.parent.mkdir()
    return ([TOOLS_DIR / "probes" / "mods-recall" / "novel_terms.py", "--state", state, "--db", slot],
            make_env(base / "home"), slot, state)


def check_d(variant, r, slot, state):
    if variant == "missing":
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert stderr_lines(r.stderr) == [f"novel_terms: ENGRAM database not found: {slot}"], r.stderr
        assert not state.exists()
    elif variant in ("store", "linkstore"):
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr and "fix the path" not in r.stderr, r.stderr
        assert json.loads(r.stdout)["db"] == str(slot) and state.is_file(), (r.stdout, r.stderr)
    else:
        refusal_line(r, slot, variant, "novel_terms: ", 1)
        assert r.stdout == "" and not state.exists(), "a refusal must not write the state file"


# ---- E: ws4/hook_bridge.py load_situation_patterns (a function call, in a subprocess) -------------------

E_DRIVER = ("import json, sys; sys.path.insert(0, sys.argv[1]); from tools.ws4 import hook_bridge; "
            "print(json.dumps(hook_bridge.load_situation_patterns(sys.argv[2])))")


def setup_e(variant, base, template):
    slot = new_slot(base, variant, template)
    return ["-c", E_DRIVER, REPO_ROOT, slot], make_env(base / "home"), slot, None


def check_e(variant, r, slot, _extra):
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    if variant in ("store", "linkstore"):
        assert got == {"ls_x": "foo"}, got  # normal path: the lesson's situation_pattern was read
    else:
        assert got == {}, got  # documented contract: {} on any DB problem; this PR only removes the FIFO hang


# ---- F: engramd_stage_profile.py --store (round 3: the same shape as B, through a6's `_check_store_arg`) --

def setup_f(variant, base, template):
    slot = new_slot(base, variant, template)
    tmp = base / "tmp"
    tmp.mkdir()
    # same device as B: a usable slot is "the live store" (ENGRAM_HOME = its directory) and stops at the live-store
    # refusal; every other shape keeps an unrelated home, so without the precheck it would go on into make_test_store
    env = make_env(slot.parent if variant in ("store", "linkstore") else base / "home")
    env["TMPDIR"] = str(tmp)
    return [TOOLS_DIR / "engramd_stage_profile.py", "--store", slot], env, slot, tmp


def check_f(variant, r, slot, tmp):
    prefix = "refusing: "
    assert not list(tmp.glob("a6-*")) and not list(tmp.glob("profile-build-*")), "a refusal must leave no temp dir"
    if variant == "missing":
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert stderr_lines(r.stderr) == [f"{prefix}ENGRAM database not found: {slot}"], r.stderr
    elif variant in ("store", "linkstore"):
        # past the store check: the next step is the live-store refusal (ENGRAM_HOME is the slot's directory)
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        lines = stderr_lines(r.stderr)
        assert len(lines) == 1 and "is this seat's live store" in lines[0], r.stderr
        assert "fix the path" not in r.stderr and "Traceback" not in r.stderr, r.stderr
    else:
        refusal_line(r, slot, variant, prefix, 2)
        assert r.stdout == ""


# ---- G: engramd_a6_latency.py --hook surface --real-store (round 4) -------------------------------------
# `--real-store` is guarded in `main` by `real_src.is_file()` BEFORE `make_test_store` and before any plugin build, so
# the real CLI is cheap to drive.  The shapes that `is_file()` already answers False (missing, notdir, loop, dangling,
# fifo, dir; a FIFO never hangs) keep the existing "is not a file ... Name one with --real-store PATH" line.  Only the
# unsearchable parent ("noaccess") used to escape as a PermissionError traceback on Python 3.12; it now gets the
# classifier's message in this file's `engramd_a6_latency: error -- <msg>` format.

def setup_g(variant, base, template):
    slot = new_slot(base, variant, template)
    tmp = base / "tmp"
    tmp.mkdir()
    env = make_env(base / "home")
    env["TMPDIR"] = str(tmp)
    # `--layout source --layout-timeout 1` keeps a run that passes the check short: it skips the plugin build
    return ([TOOLS_DIR / "engramd_a6_latency.py", "--hook", "surface", "--real-store", slot,
             "--layout", "source", "--layout-timeout", "1"], env, slot, tmp)


def check_g(variant, r, slot, tmp):
    prefix = "engramd_a6_latency: error -- "
    if variant in ("store", "linkstore"):
        # Normal path: the check let the store through, so the run went on past it to the layout step (the copy of
        # the store, then the source-layout child).  Which of the two later outcomes it is depends on whether the
        # owner can start in this environment: "owner exited early" (the child could not launch) or the 1 s
        # --layout-timeout.  Either line is after the check; neither is the not-a-file line or a classifier line.
        assert r.returncode != 2, (r.returncode, r.stdout, r.stderr)
        assert "is not a file" not in r.stderr and "fix the path" not in r.stderr, r.stderr
        assert ("owner exited early" in r.stderr or "exceeded --layout-timeout" in r.stderr), r.stderr
        return
    assert not list(tmp.glob("a6-*")), "a refusal must leave no a6-* temp dir"
    assert r.stdout == ""
    if variant == "noaccess":
        refusal_line(r, slot, variant, prefix, 2)
        return
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    assert lines[0].startswith(f"{prefix}the real-store source {slot} is not a file;"), lines[0]
    assert "Name one with --real-store PATH" in lines[0], lines[0]


TOOLS = {"A": (setup_a, check_a), "B": (setup_b, check_b), "C": (setup_c, check_c),
         "D": (setup_d, check_d), "E": (setup_e, check_e), "F": (setup_f, check_f),
         "G": (setup_g, check_g)}


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("tool", list(TOOLS))
def test_each_tool_maps_the_store_path_classes(tool, variant, base, store_template):
    if variant == "noaccess" and os.geteuid() == 0:
        pytest.skip("root bypasses the directory permission")
    setup, check = TOOLS[tool]
    argv, env, slot, extra = setup(variant, base, store_template)
    r = run(argv, env, cwd=base, stdin="hello world novel terms")  # a FIFO that hangs fails here, on the timeout
    check(variant, r, slot, extra)
