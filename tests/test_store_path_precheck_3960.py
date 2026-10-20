"""engram-alpha#3960 (part of #2652) -- one store-path precheck in the shared tools helper, mapped per tool.

Eight tools gate their flag-OFF read on `not db_path.exists()` and then open the store `mode=ro` through
`owner_read.open_reader`.  That precondition is wrong for four kinds of store path: a parent directory that
cannot be searched (`exists()` RAISES), a symlink loop and a dangling link (`exists()` says False, i.e. "not
found", which invites creating a store through a link someone made on purpose), and a FIFO (the `mode=ro`
open BLOCKS on macOS).  The contract is Borges's ruling on #3960 (2026-10-19 19:44Z and 19:52Z):

  the helper `check_store_path(db_path)` classifies, by `lstat` then `stat` and NEVER an open:
      no directory entry at all           -> the built-in FileNotFoundError, the text the callers raise today
      any other lstat failure             -> StoreUncheckable
      lstat ok, stat fails (ELOOP, dangling link, EACCES on the target) -> StoreUncheckable
      stat ok, not a regular file         -> StoreNotAStore (FIFO, directory, socket, device)
  both subclass StoreUnusable(Exception), which is neither an OSError nor a FileNotFoundError.
  `open_reader`'s flag-OFF branch calls the check first, so no caller can forget it.
  Each of the nine entry points maps the classes to its own documented codes (see the per-tool checks).

Every per-caller cell runs the tool as a real subprocess with a 15 s timeout, flag OFF; a timeout is a FAILED
cell, never a skip.  Each helper cell runs in a subprocess too, so a helper that opens a FIFO hangs the cell
and not the run.

Out of scope, by ruling: a file swapped for a FIFO between the stat and the open (TOCTOU); the flag-ON paths.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOLS_DIR = SRC_ENGRAM / "tools"
OWNER_READ = TOOLS_DIR / "_owner_read.py"
TIMEOUT = 15

VARIANTS = ["missing", "noaccess", "notdir", "loop", "dangling", "fifo", "dir", "store", "linkstore"]
BAD = ["noaccess", "notdir", "loop", "dangling", "fifo", "dir"]
#: variant -> the class the helper raises
CLASS = {"noaccess": "StoreUncheckable", "notdir": "StoreUncheckable", "loop": "StoreUncheckable", "dangling": "StoreUncheckable",
         "fifo": "StoreNotAStore", "dir": "StoreNotAStore"}
#: variant -> the words the message must carry (all of them)
WORDS = {
    "noaccess": ("EACCES", "Permission denied"),
    "notdir": ("ENOTDIR", "Not a directory"),
    "loop": ("ELOOP", "Too many levels of symbolic links"),
    "dangling": ("ENOENT", "No such file or directory"),
    "fifo": ("is a FIFO",),
    "dir": ("is a directory",),
}

needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the directory permission")


# ---- harness ---------------------------------------------------------------------------------------

def run(argv, env, cwd=None):
    """Run a command with the 15 s timeout.  A timeout FAILS the cell (a hang is the bug under test)."""
    try:
        return subprocess.run([sys.executable, *map(str, argv)], capture_output=True, text=True, env=env,
                              cwd=cwd, timeout=TIMEOUT)
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
    d = Path(tempfile.mkdtemp(prefix="sp3960-tpl-", dir="/tmp")).resolve()
    db = d / "knowledge.db"
    env = make_env(d)
    r = subprocess.run([sys.executable, "-c", BUILD_STORE, str(SRC_ENGRAM), str(db)], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0 and db.is_file(), r.stderr
    try:
        yield db
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="sp3960-", dir="/tmp")).resolve()
    try:
        yield d
    finally:
        for name in ("sub", "d", "locked"):
            try:
                (d / name).chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


def install(variant: str, slot: Path, template: Path) -> None:
    """Make `slot` (= <dir>/knowledge.db, the dir exists) the given kind of store path."""
    if variant == "missing":
        return
    if variant == "noaccess":
        shutil.copy(template, slot)  # a real store, behind a directory that cannot be searched
        slot.parent.chmod(0)
    elif variant == "notdir":
        # a parent COMPONENT of the store path is a regular file: lstat raises ENOTDIR, not ENOENT
        slot.parent.rmdir()
        slot.parent.write_text("not a directory", encoding="utf-8")
    elif variant == "linkstore":
        # the store is a symlink to a real regular store elsewhere: usable, exactly like a plain store
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


def assert_words(variant: str, text: str, slot: Path) -> None:
    assert str(slot) in text, (str(slot), text)
    for w in WORDS[variant]:
        assert w in text, (w, text)
    assert "fix the path" in text, text
    for forbidden in ("owner", "flag"):
        assert forbidden not in text.lower(), (forbidden, text)


# ---- the helper itself -------------------------------------------------------------------------------

DRIVER = r'''
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("owner_read_3960", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
mode, path = sys.argv[2], Path(sys.argv[3])
out = {}
try:
    if mode == "check":
        out["ret"] = repr(m.check_store_path(path))
    elif mode == "reader":
        s = m.OwnerReadSpec(flag_key="x_reads_via_owner", error_cls=RuntimeError, home=path.parent,
                            never_empty="n")
        m.open_reader(s, path, False)
        out["ret"] = "opened"
    elif mode == "types":
        out["all"] = sorted(getattr(m, "__all__", []))
        out["classes"] = {n: [c.__name__ for c in getattr(m, n).__mro__] for n in
                          ("StoreUnusable", "StoreUncheckable", "StoreNotAStore")}
        out["ret"] = "types"
except BaseException as e:
    out["type"] = type(e).__name__
    out["module"] = type(e).__module__
    out["mro"] = [c.__name__ for c in type(e).__mro__]
    out["msg"] = str(e)
print(json.dumps(out))
'''


def probe(mode: str, path: Path) -> dict:
    r = run(["-c", DRIVER, OWNER_READ, mode, path], make_env(path.parent))
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture
def slot(base):
    (base / "d").mkdir()
    return base / "d" / "knowledge.db"


def test_h1_no_entry_is_the_builtin_file_not_found_with_todays_text(slot):
    got = probe("check", slot)
    assert got["type"] == "FileNotFoundError" and got["module"] == "builtins", got
    assert got["msg"] == f"ENGRAM database not found: {slot}"


@needs_nonroot
def test_h2_a_parent_that_cannot_be_searched_is_uncheckable(slot, store_template):
    install("noaccess", slot, store_template)
    got = probe("check", slot)
    assert got["type"] == "StoreUncheckable", got
    assert_words("noaccess", got["msg"], slot)


def test_h2b_a_parent_component_that_is_a_regular_file_is_uncheckable_not_not_found(slot):
    """ENOTDIR from lstat: the other `OSError` row. Mapping it to not-found would invite creating a store there."""
    install("notdir", slot, slot)
    got = probe("check", slot)
    assert got["type"] == "StoreUncheckable", got
    assert_words("notdir", got["msg"], slot)


@needs_nonroot
def test_h2c_a_link_whose_target_directory_cannot_be_searched_is_uncheckable(slot, store_template, base):
    """lstat succeeds (the link is in a searchable directory), stat fails with EACCES on the target."""
    locked = base / "locked"
    locked.mkdir()
    shutil.copy(store_template, locked / "real.db")
    os.symlink(locked / "real.db", slot)
    locked.chmod(0)
    got = probe("check", slot)
    assert got["type"] == "StoreUncheckable", got
    assert "EACCES" in got["msg"] and "Permission denied" in got["msg"] and str(slot) in got["msg"], got


def test_h3_a_symlink_loop_is_uncheckable_not_not_found(slot):
    install("loop", slot, slot)
    got = probe("check", slot)
    assert got["type"] == "StoreUncheckable", got
    assert_words("loop", got["msg"], slot)


def test_h4_a_dangling_link_is_uncheckable_not_not_found(slot):
    install("dangling", slot, slot)
    got = probe("check", slot)
    assert got["type"] == "StoreUncheckable", got
    assert_words("dangling", got["msg"], slot)


def test_h5_a_fifo_is_not_a_store_and_the_check_does_not_block(slot):
    install("fifo", slot, slot)
    got = probe("check", slot)
    assert got["type"] == "StoreNotAStore", got
    assert_words("fifo", got["msg"], slot)


def test_h6_a_directory_is_not_a_store(slot):
    install("dir", slot, slot)
    got = probe("check", slot)
    assert got["type"] == "StoreNotAStore", got
    assert_words("dir", got["msg"], slot)


def test_h7_a_socket_is_not_a_store(slot):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(slot))
        got = probe("check", slot)
    finally:
        s.close()
    assert got["type"] == "StoreNotAStore", got
    assert str(slot) in got["msg"] and "is a socket" in got["msg"] and "fix the path" in got["msg"], got


def test_h7b_a_link_to_a_device_is_not_a_store(slot):
    os.symlink("/dev/null", slot)
    got = probe("check", slot)
    assert got["type"] == "StoreNotAStore", got
    assert str(slot) in got["msg"] and "is a device" in got["msg"] and "fix the path" in got["msg"], got


def test_h8_a_regular_file_returns_none(slot, store_template):
    install("store", slot, store_template)
    assert probe("check", slot) == {"ret": "None"}


def test_h8b_a_symlink_to_a_regular_file_returns_none(slot, store_template, base):
    real = base / "elsewhere.db"
    shutil.copy(store_template, real)
    os.symlink(real, slot)
    assert probe("check", slot) == {"ret": "None"}


def test_h9_the_class_family_is_not_an_oserror_and_not_a_file_not_found():
    got = probe("types", Path("/tmp/x/knowledge.db"))
    assert got.get("ret") == "types", got
    for name in ("StoreUncheckable", "StoreNotAStore"):
        assert got["classes"][name][:3] == [name, "StoreUnusable", "Exception"], got["classes"][name]
    for name, mro in got["classes"].items():
        assert "OSError" not in mro and "FileNotFoundError" not in mro, (name, mro)
    assert got["classes"]["StoreUnusable"][:2] == ["StoreUnusable", "Exception"]
    assert {"check_store_path", "StoreUnusable", "StoreUncheckable", "StoreNotAStore"} <= set(got["all"]), got["all"]


def test_h10_every_message_is_one_line(slot, store_template):
    install("dir", slot, store_template)
    got = probe("check", slot)
    assert got["type"] == "StoreNotAStore" and "\n" not in got["msg"], got
    slot.rmdir()
    install("loop", slot, store_template)
    got = probe("check", slot)
    assert got["type"] == "StoreUncheckable" and "\n" not in got["msg"], got


def test_h11_open_reader_flag_off_refuses_a_fifo_promptly(slot):
    """The structural guard: even a caller that forgets the precheck cannot hang on a FIFO."""
    install("fifo", slot, slot)
    got = probe("reader", slot)  # run() fails the cell on the 15 s timeout
    assert got["type"] == "StoreNotAStore", got


def test_h11b_open_reader_flag_off_refuses_a_loop_and_a_missing_store_with_the_typed_classes(slot):
    got = probe("reader", slot)
    assert got["type"] == "FileNotFoundError", got
    install("loop", slot, slot)
    assert probe("reader", slot)["type"] == "StoreUncheckable"


# ---- the nine entry points ---------------------------------------------------------------------------

def tool_cmd(tool: str, slot: Path, home_ok: Path, base: Path):
    """(argv, env) for one entry point aimed at `slot`; `home_ok` is a healthy ENGRAM_HOME."""
    d = slot.parent
    t = TOOLS_DIR
    if tool == "inspect_raw":
        return [t / "inspect_raw.py", "ob_x"], make_env(d)
    if tool == "dream_master_batch":
        return [t / "dream_master_batch.py", "safety-row", "ob_x"], make_env(d)
    if tool == "study_queue":
        return [t / "study_queue.py", "--home", home_ok, "--db", slot], make_env(home_ok)
    if tool == "weakest_load_bearing":
        return [t / "weakest_load_bearing.py", "--db", slot], make_env(home_ok)
    if tool == "reconcile_lesson_demotions":
        return [t / "reconcile_lesson_demotions.py", "--db", slot, "--ledger", base / "ledger.jsonl"], \
            make_env(home_ok)
    if tool == "regenerate_embeddings":
        return [t / "engram-regenerate-embeddings.py", "--engram-home", d], make_env(home_ok)
    if tool == "cohort_prepare":
        return [t / "cohort_dispatch.py", "prepare", "--ids", "ob_x", "--out", base / "out", "--db", slot], \
            make_env(home_ok)
    if tool == "cohort_verify_in":
        return [t / "cohort_dispatch.py", "verify-in", "--out", base / "out", "--db", slot], make_env(home_ok)
    raise AssertionError(tool)  # pragma: no cover


TOOLS = ["inspect_raw", "dream_master_batch", "study_queue", "weakest_load_bearing",
         "reconcile_lesson_demotions", "regenerate_embeddings", "cohort_prepare", "cohort_verify_in"]


def setup_case(tool: str, variant: str, base: Path, template: Path):
    """Build the scene and return (argv, env, slot)."""
    home_ok = base / "home"
    home_ok.mkdir()
    sub = base / "sub"
    sub.mkdir()
    slot = sub / "knowledge.db"
    (base / "ledger.jsonl").write_text("", encoding="utf-8")
    if tool == "cohort_verify_in":
        # verify-in needs a prepared cohort: prepare against a real store, then swap the store for the case
        shutil.copy(template, slot)
        argv, env = tool_cmd("cohort_prepare", slot, home_ok, base)
        r = run(argv, env)
        assert r.returncode == 0, ("prepare for verify-in failed", r.stdout, r.stderr)
        slot.unlink()
    install(variant, slot, template)
    argv, env = tool_cmd(tool, slot, home_ok, base)
    return argv, env, slot


def check_inspect_raw(variant, r, slot):
    if variant == "missing":
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert r.stdout == ""
        # byte-identical to 7dbd0857
        assert r.stderr == (f"ERROR: DB not found at {slot}\n"
                            "  Set ENGRAM_HOME to point at your engram data directory.\n"), r.stderr
    elif variant == "store":
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert r.stdout.strip() != ""
        assert "Traceback" not in r.stderr
    else:
        # OWNER_READ_FAILED_EXIT: could not be read; 1 stays "not found"
        assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
        assert r.stdout == ""
        assert "Traceback" not in r.stderr, r.stderr
        lines = stderr_lines(r.stderr)
        assert len(lines) == 1, r.stderr
        prefix = "ERROR: InspectRawOwnerReadError: "
        assert lines[0].startswith(prefix), lines[0]
        message = lines[0][len(prefix):]
        assert_words(variant, message, slot)
        assert CLASS[variant] not in message  # the sentence, not the class name


def make_check_exit1_class_line(missing_text):
    """dream_master_batch / study_queue / weakest_load_bearing: exit 1 either way, a distinct class line."""
    def check(variant, r, slot):
        assert r.stdout == "" or variant == "store"
        assert "Traceback" not in r.stderr, r.stderr
        if variant == "missing":
            assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
            assert stderr_lines(r.stderr) == [f"FileNotFoundError: ENGRAM database not found: {slot}"], r.stderr
        elif variant == "store":
            assert r.returncode == 0, (r.stdout, r.stderr)
            assert r.stdout.strip() != ""
        else:
            assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
            lines = stderr_lines(r.stderr)
            assert len(lines) == 1, r.stderr
            assert lines[0].startswith(f"{CLASS[variant]}: "), lines[0]
            assert_words(variant, lines[0], slot)
    return check


def check_reconcile(variant, r, slot):
    assert "Traceback" not in r.stderr, r.stderr
    if variant == "store":
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert json.loads(r.stdout) == {"candidates": []}
        return
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    payload = json.loads(r.stdout)
    assert set(payload) == {"error", "candidates"} and payload["candidates"] is None, payload
    if variant == "missing":
        # byte-identical to 7dbd0857
        assert r.stdout == ('{\n  "error": "knowledge.db not found at ' + str(slot) + '",\n'
                            '  "candidates": null\n}\n'), r.stdout
    else:
        assert_words(variant, payload["error"], slot)


def check_regen(variant, r, slot):
    assert r.stdout == ""
    markers = ("knowledge.db not found", "Run: sqlite3")
    if variant == "missing":
        assert r.returncode == 1, (r.returncode, r.stderr)
        # byte-identical to 7dbd0857
        assert r.stderr == (f"ERROR: knowledge.db not found at {slot}\n"
                            '  Run: sqlite3 "${ENGRAM_HOME:-$HOME/.engram}/knowledge.db" < '
                            '"${ENGRAM_HOME:-$HOME/.engram}/knowledge.sql"  first.\n'), r.stderr
    elif variant == "store":
        # The real-store control cannot run the embedding stack in a scratch home (no server.py there), so it
        # asserts only that the process got PAST the precheck: whatever it fails with later, it is not the
        # precheck's message and not a store-class line.
        for m in markers + ("StoreUncheckable", "StoreNotAStore", "fix the path"):
            assert m not in r.stderr, (m, r.stderr)
    else:
        # exit 1 and never EXIT_OWNER_FAILURE (3): the flag is OFF, the owner did not fail
        assert r.returncode == 1, (r.returncode, r.stderr)
        assert "Traceback" not in r.stderr, r.stderr
        lines = stderr_lines(r.stderr)
        assert len(lines) == 1, r.stderr
        assert lines[0].startswith("ERROR: "), lines[0]
        assert_words(variant, lines[0], slot)
        for m in markers:
            assert m not in r.stderr, (m, r.stderr)  # the create-the-store remedy is wrong for these classes


def check_cohort_prepare(variant, r, slot):
    assert "Traceback" not in r.stderr, r.stderr
    if variant == "store":
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert json.loads(r.stdout)["chunks"]
        return
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)  # prepare's error code
    assert r.stdout == ""
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    if variant == "missing":
        # the one deliberate not-found change: a traceback at 7dbd0857, one line now
        assert lines[0] == f"error: ENGRAM database not found: {slot}", lines[0]
    else:
        assert lines[0].startswith("error: "), lines[0]
        assert_words(variant, lines[0], slot)


def check_cohort_verify_in(variant, r, slot):
    assert "Traceback" not in r.stderr, r.stderr
    if variant == "store":
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert json.loads(r.stdout)["passed"] is True
        return
    if variant == "missing":
        # unchanged from 7dbd0857: the skip warning, the run still passes
        assert (f"warning: DB path {slot} not found — content-integrity check skipped\n") in r.stderr, r.stderr
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert json.loads(r.stdout)["passed"] is True
        return
    # an uncheckable store must NEVER silently skip the integrity check
    assert "content-integrity check skipped" not in r.stderr, r.stderr
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    out = json.loads(r.stdout)
    assert out["passed"] is False, out
    errs = [e for e in out["errors"] if e["check"] == "db_access"]
    assert len(errs) == 1 and errs[0]["chunk"] == "all", out["errors"]
    assert_words(variant, errs[0]["error"], slot)


CHECKS = {
    "inspect_raw": check_inspect_raw,
    "dream_master_batch": make_check_exit1_class_line(None),
    "study_queue": make_check_exit1_class_line(None),
    "weakest_load_bearing": make_check_exit1_class_line(None),
    "reconcile_lesson_demotions": check_reconcile,
    "regenerate_embeddings": check_regen,
    "cohort_prepare": check_cohort_prepare,
    "cohort_verify_in": check_cohort_verify_in,
}


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("tool", TOOLS)
def test_c_each_caller_maps_the_store_path_classes(tool, variant, base, store_template):
    if variant == "noaccess" and os.geteuid() == 0:
        pytest.skip("root bypasses the directory permission")
    argv, env, slot = setup_case(tool, variant, base, store_template)
    r = run(argv, env, cwd=base)
    # a symlink to a real store must behave exactly like the plain-store control
    CHECKS[tool]("store" if variant == "linkstore" else variant, r, slot)


@pytest.mark.parametrize("variant", ["missing", "fifo"])
def test_b1_weakest_load_bearing_names_the_store_before_it_reads_config(variant, base, store_template):
    """weakest_load_bearing reads config.json (`core._get_current_turn()`) between the explicit precheck and the
    open. A torn config raises its own error there, so the explicit precheck must run first: the operator is told
    about the store, not about a config file the store problem hides."""
    argv, env, slot = setup_case("weakest_load_bearing", variant, base, store_template)
    (base / "home" / "config.json").write_text("{ torn", encoding="utf-8")
    r = run(argv, env, cwd=base)
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 1 and r.stdout == "", (r.returncode, r.stdout, r.stderr)
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    if variant == "missing":
        assert lines[0] == f"FileNotFoundError: ENGRAM database not found: {slot}", lines
    else:
        assert lines[0].startswith("StoreNotAStore: ") and "is a FIFO" in lines[0], lines
