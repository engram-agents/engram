"""#3628 slice 2d: every hook `mode=ro` sqlite connect, and the surface daemon's
three file opens, must never block on a FIFO planted at the path.

`sqlite3.connect("file:<p>?mode=ro", uri=True)` on a FIFO HANGS (measured > 5 s,
macOS, Python 3.12); a plain `sqlite3.connect(p)` fails at once with "disk I/O
error", so plain connects are out of scope. `_hooklib.connect_regular_ro` stats
the path first and refuses a non-regular file with a class that is BOTH a
sqlite3.OperationalError and an OSError. Every site keeps its existing handler:
the refusal lands in it as a sqlite error exactly as a directory did on dev.

Residual, not closed and not tested here (see #3577): the stat-then-connect pair
is check-then-act. A regular file swapped for a FIFO between the stat and the
connect still hangs; SQLite takes a path, not a descriptor. The helper narrows
the window, it does not close it, and no cell below claims otherwise.

Layout:

  * helper cells         -- connect_regular_ro itself, in a child python;
  * site cells           -- the real hook function per site, kinds
                            fifo / directory (refused, no hang) and regular
                            (positive control), each in a SUBPROCESS with a hard
                            timeout so a regression fails instead of hanging;
  * dev-parity cells     -- for the kinds a regular-file user can hit (missing,
                            dangling, regular, regular mode 000) the SAME recipe
                            is run against origin/dev's copy of the hook (git
                            archive) and must give the identical result;
  * daemon cells         -- tombstone write, PID read in cleanup, PID write;
  * AST guards           -- no raw mode=ro connect, no builtin open() in the
                            daemon, each with a planted-snippet control.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = Path(os.environ.get("SQLITE_3628S2D_HOOK_DIR") or SRC / "hooks" / "claude")
GUARD_S = int(os.environ.get("SQLITE_3628S2D_GUARD_S", "15"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# --------------------------------------------------------------------------
# child-process harness
# --------------------------------------------------------------------------

_CHILD = r"""
import importlib.util, json, os, sqlite3, sys, stat
spec_in = json.loads(sys.argv[1])
sys.path.insert(0, spec_in["hook_dir"])
CALLS = []          # (uri, kwargs) of every sqlite3.connect that REALLY ran
_real_connect = sqlite3.connect
def _counting_connect(*a, **k):
    CALLS.append([str(a[0]), {kk: vv for kk, vv in k.items()}])
    return _real_connect(*a, **k)
sqlite3.connect = _counting_connect
spec = importlib.util.spec_from_file_location("hook_3628s2d_child", spec_in["hook"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = spec_in["paths"]
def kind_of(p):
    try:
        m = os.stat(p).st_mode
    except OSError:
        return None
    return 'fifo' if stat.S_ISFIFO(m) else ('reg' if stat.S_ISREG(m) else 'other')
ns = {"mod": mod, "P": P, "os": os, "json": json, "sqlite3": sqlite3, "sys": sys,
      "CALLS": CALLS, "kind_of": kind_of, "result": None}
exec(spec_in["code"], ns)
print("RESULT:" + json.dumps(ns["result"]))
"""

HANG = "HANG"


def run_child(hook_dir: Path, hook_name: str, paths: dict, code: str, env_extra: dict,
              timeout=GUARD_S, allow_hang=False):
    """Returns (result, stderr). On a hang: pytest.fail, or ("HANG", "") when
    allow_hang (used to tabulate what origin/dev does)."""
    payload = json.dumps({"hook_dir": str(hook_dir), "hook": str(hook_dir / hook_name),
                          "paths": paths, "code": code})
    env = dict(os.environ)
    env.pop("ENGRAM_RUNTIME_DIR", None)
    env.update(env_extra)
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD, payload], capture_output=True,
                              text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        if allow_hang:
            return HANG, ""
        pytest.fail(f"blocked for more than {timeout}s on a non-regular file")
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):]), done.stderr


# --------------------------------------------------------------------------
# fixture database + kinds
# --------------------------------------------------------------------------

KINDS_BAD = ["fifo", "directory"]
KINDS_ABSENT = ["missing", "dangling"]
KINDS_PARITY = ["missing", "dangling", "regular", "regular000"]


def make_db(path: Path):
    """A small knowledge.db carrying just the tables/columns the sites read."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, confidence REAL,
            recall_summary TEXT, recall_keywords TEXT, created_at TEXT,
            is_current INTEGER, status TEXT, memory_status TEXT, focused_at TEXT,
            focus_reason TEXT, metadata TEXT);
        CREATE TABLE embedding_restore_state (singleton_key INTEGER PRIMARY KEY,
            not_fully_restored INTEGER, null_count INTEGER, cause TEXT);
        CREATE TABLE focus_state (singleton_key INTEGER PRIMARY KEY, active_set_name TEXT);
        CREATE TABLE focus_sets (name TEXT, node_ids TEXT, description TEXT,
            created_at TEXT, last_loaded_at TEXT);
    """)
    meta = json.dumps({"situation_pattern": "widget", "scaffolding_nudge": "mind the widget",
                       "prevention_type": "shadow"})
    conn.execute(
        "INSERT INTO nodes VALUES ('fx-1','lesson','a widget claim',0.9,'widget summary','widget',"
        "'2026-10-01T00:00:00Z',1,'active','active','2026-10-01T00:00:00Z','because',?)", (meta,))
    conn.execute("INSERT INTO embedding_restore_state VALUES (1,1,3,'test-cause')")
    conn.execute("INSERT INTO focus_state VALUES (1,'main')")
    conn.execute("INSERT INTO focus_sets VALUES ('dormant','[\"fx-1\"]','d',"
                 "'2026-10-01T00:00:00Z','2026-10-02T00:00:00Z')")
    conn.commit()
    conn.close()


def plant(path: Path, kind: str):
    """Put a `kind` object at `path` (a would-be knowledge.db)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "missing":
        return
    if kind == "dangling":
        os.symlink(str(path.parent / "nowhere.db"), path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind in ("regular", "regular000"):
        make_db(path)
        if kind == "regular000":
            os.chmod(path, 0)
    else:
        raise ValueError(kind)


def _skip_if_unreadable_impossible(kind):
    if kind == "regular000" and (IS_ROOT or os.access("/dev/null", os.R_OK) is False):
        pytest.skip("mode 000 does not stop root from reading")


class Ctx:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.eh = self.home / ".engram"
        self.eh.mkdir(parents=True)
        (self.eh / "sessions").mkdir()
        self.db = self.eh / "knowledge.db"
        self.pack = tmp / "pack" / "knowledge.db"
        # stop-hook maturity threshold of 1 so a one-node DB reads as mature
        (self.eh / "config.json").write_text(json.dumps({
            "cadence": {"write_nudge_node_threshold": 1},
            "attached_packs": [{"id": "pk", "path": str(tmp / "pack"), "enabled": True}],
        }))

    def paths(self):
        return {"home": str(self.home), "eh": str(self.eh), "db": str(self.db),
                "pack": str(self.pack), "tmp": str(self.tmp)}

    def env(self):
        return {"HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
                "ENGRAM_SESSION_ROLE": "interactive", "ENGRAM_RUNTIME_DIR": str(SRC)}


# --------------------------------------------------------------------------
# site recipes
# --------------------------------------------------------------------------

_STUB_CHAIN = (
    "_chain_calls = []\n"
    "def _chain(conn, node_id):\n"
    "    _chain_calls.append(node_id)\n"
    "    return {'nodes': {}, 'heads': [], 'start_resolved_id': node_id}\n"
    "mod._GENERALIZATION_CHAIN_FN = _chain\n"
    "mod._GENERALIZATION_CHAIN_IMPORT_ATTEMPTED = True\n")

_STUB_CHAIN_SURFACE = (
    "_chain_calls = []\n"
    "def _chain(conn, node_id):\n"
    "    _chain_calls.append(node_id)\n"
    "    return {'nodes': {}, 'heads': [], 'start_resolved_id': node_id}\n"
    "mod._import_generalization_chain = lambda: _chain\n")

_PATHLIB_DB = "__import__('pathlib').Path(P['db'])"

SITES = {}


def site(name, hook, code, *, path="db", **expect):
    """expect: kind -> predicate(result). Kinds without one are only parity-checked."""
    SITES[name] = {"hook": hook, "code": code, "path": path, "expect": expect}


def _bad(pred):
    return {"fifo": pred, "directory": pred}


# _hooklib._focused_ids  (:1187)
site("hooklib_focused_ids", "_hooklib.py",
     "r = mod._focused_ids()\nresult = [sorted(r[0]), r[1]]",
     regular=lambda r: r == [["fx-1"], True],
     missing=lambda r: r == [[], False], dangling=lambda r: r == [[], False],
     **_bad(lambda r: r == [[], False]))

# lesson-tripwire  (:298, :550, :834, :1432)
site("tripwire_load_tripwires", "engram-lesson-tripwire-hook.py",
     f"result = [[list(t)[:4] for t in mod.load_tripwires({_PATHLIB_DB})], len(CALLS)]",  # [:4]: alpha#3920 added a 5th element (match_scope)
     regular=lambda r: r[1] == 1 and len(r[0]) == 1 and r[0][0][2] == "fx-1",
     missing=lambda r: r == [[], 0], dangling=lambda r: r == [[], 0],
     **_bad(lambda r: r == [[], 0]))

site("tripwire_render_triggered_principles", "engram-lesson-tripwire-hook.py",
     _STUB_CHAIN + f"out = mod.render_triggered_principles([{{'node_id': 'fx-1'}}], {_PATHLIB_DB})\n"
     "result = [out, len(_chain_calls), len(CALLS)]",
     regular=lambda r: r == ["", 1, 1],
     **_bad(lambda r: r == ["", 0, 0]))

site("tripwire_load_prevention_lessons", "engram-lesson-tripwire-hook.py",
     f"result = [[dict(d) for d in mod.load_prevention_lessons({_PATHLIB_DB})], len(CALLS)]",
     regular=lambda r: r[1] == 1 and len(r[0]) == 1 and r[0][0]["id"] == "fx-1",
     missing=lambda r: r == [[], 0], dangling=lambda r: r == [[], 0],
     **_bad(lambda r: r == [[], 0]))

site("tripwire_in_turn_recall", "engram-lesson-tripwire-hook.py",
     "ret = mod.check_in_turn_recall('grep widget gadget', {})\n"
     "result = [ret, len(CALLS)]",
     regular=lambda r: r[0] == "" and r[1] == 1,
     missing=lambda r: r == ["", 1], dangling=lambda r: r == ["", 1],
     **_bad(lambda r: r == ["", 0]))

# session-start  (:457, :1416, :1619, :1867) -- precheck is os.path.exists now
site("sessionstart_embedding_restore_flag", "engram-session-start-hook.py",
     "r = mod._check_embedding_restore_flag(P['eh'])\nresult = [r[0], (r[1] or '')[:60], len(CALLS)]",
     regular=lambda r: r[0] is False and "3 node(s) lack embeddings" in r[1] and r[2] == 1,
     missing=lambda r: r == [True, "", 0], dangling=lambda r: r == [True, "", 0],
     **_bad(lambda r: r[0] is None and "not a regular file" in r[1] and r[2] == 0))

site("sessionstart_focus_block", "engram-session-start-hook.py",
     "out = mod.focus_block(P['eh'])\nresult = [len(out) > 0, len(CALLS)]",
     regular=lambda r: r == [True, 1],
     missing=lambda r: r == [False, 0], dangling=lambda r: r == [False, 0],
     **_bad(lambda r: r == [False, 0]))

site("sessionstart_saved_focus_sets_block", "engram-session-start-hook.py",
     "out = mod.saved_focus_sets_block(P['eh'])\nresult = [len(out) > 0, len(CALLS)]",
     regular=lambda r: r == [True, 1],
     missing=lambda r: r == [False, 0], dangling=lambda r: r == [False, 0],
     **_bad(lambda r: r == [False, 0]))

site("sessionstart_session_checkpoint_block", "engram-session-start-hook.py",
     "out = mod.session_checkpoint_block(P['eh'], '2020-01-01T00:00:00Z')\n"
     "result = [len(out) > 0, len(CALLS)]",
     regular=lambda r: r == [True, 1],
     missing=lambda r: r == [False, 0], dangling=lambda r: r == [False, 0],
     **_bad(lambda r: r == [False, 0]))

# stop hook  (:377, no timeout kwarg)
site("stop_maturity_muted", "engram-stop-hook.py",
     "result = [mod._maturity_muted(), len(CALLS), [c[1] for c in CALLS]]",
     regular=lambda r: r == [True, 1, [{"uri": True}]],
     missing=lambda r: r[0] is False, dangling=lambda r: r[0] is False,
     **_bad(lambda r: r == [False, 0, []]))

# surface hook  (:144, :303, :1373, :1513, :1669, :3508)
site("surface_check_db_liveness", "engram-surface-hook.py",
     "r = mod._check_db_liveness(P['db'])\nresult = [r[0], str(r[1])]",
     regular=lambda r: r == [True, "ok"],
     missing=lambda r: r == [False, "knowledge.db not found"],
     dangling=lambda r: r == [False, "knowledge.db not found"],
     regular000=lambda r: r[0] is False,
     # The refusal must be a sqlite3.DatabaseError: a plain OSError would be
     # caught by the later `except Exception` and report the FIFO DB HEALTHY.
     **_bad(lambda r: r[0] is False and "not a regular file" in r[1]))

site("surface_check_embedding_restore_flag", "engram-surface-hook.py",
     "r = mod._check_embedding_restore_flag(P['db'])\nresult = [r[0], (r[1] or '')[:60], len(CALLS)]",
     regular=lambda r: r[0] is False and "3 node(s) lack embeddings" in r[1] and r[2] == 1,
     missing=lambda r: r == [True, "", 0], dangling=lambda r: r == [True, "", 0],
     **_bad(lambda r: r[0] is None and "not a regular file" in r[1] and r[2] == 0))

site("surface_dormant_focus_set_candidates", "engram-surface-hook.py",
     "c, n = mod._dormant_focus_set_candidates({'fx-1'})\nresult = [len(c), n, len(CALLS)]",
     regular=lambda r: r == [1, 1, 1],
     missing=lambda r: r == [0, 0, 1], dangling=lambda r: r == [0, 0, 1],
     **_bad(lambda r: r == [0, 0, 0]))

site("surface_render_triggered_principles_section", "engram-surface-hook.py",
     _STUB_CHAIN_SURFACE + f"out = mod._render_triggered_principles_section(['fx-1'], P['db'])\n"
     "result = [out, len(_chain_calls), len(CALLS)]",
     regular=lambda r: r == ["", 1, 1],
     **_bad(lambda r: r == ["", 0, 0]))

site("surface_query_attached_packs", "engram-surface-hook.py",
     "mod._not_search_excluded_sql = mod._not_search_excluded_sql or (lambda a: '1=1')\n"
     "out = mod._query_attached_packs('widget gadget summary')\n"
     "result = [out, [c[0].split('?', 1)[1] for c in CALLS]]",
     path="pack",
     regular=lambda r: r[1] == ["mode=ro&immutable=1"],
     **_bad(lambda r: r == [[], []]))

# surface main()'s IDF gate  (:3508): driven in-process with the earlier probes stubbed
site("surface_main_idf_gate", "engram-surface-hook.py",
     "import io\n"
     "mod._check_db_liveness = lambda p: (True, 'ok')\n"
     "sys.stdin = io.StringIO(json.dumps({'prompt': 'what about the widget gadget design?', "
     "'session_id': 'sess-d', 'transcript_path': P['tmp'] + '/t.jsonl', 'cwd': P['tmp']}))\n"
     "buf = io.StringIO(); _o = sys.stdout; sys.stdout = buf\n"
     "try:\n"
     "    mod.main()\n"
     "except SystemExit:\n"
     "    pass\n"
     "finally:\n"
     "    sys.stdout = _o\n"
     "result = [bool(buf.getvalue().strip()), [c[0].replace(P['eh'], '<EH>') for c in CALLS if c[0].startswith('file:') and '.engram' in c[0] and 'mode=ro' in c[0]]]",
     regular=lambda r: r[0] is True and len(r[1]) >= 1,
     **_bad(lambda r: r[0] is True and r[1] == []))


def _check_site(name, kind, tmp_path, hook_dir=HOOK_DIR, allow_hang=False):
    _skip_if_unreadable_impossible(kind)
    s = SITES[name]
    ctx = Ctx(tmp_path)
    plant(ctx.pack if s["path"] == "pack" else ctx.db, kind)
    out = run_child(hook_dir, s["hook"], ctx.paths(), s["code"], ctx.env(), allow_hang=allow_hang)
    return s, out[0]


@pytest.mark.parametrize("kind", KINDS_BAD + ["regular"])
@pytest.mark.parametrize("name", sorted(SITES))
def test_site_cell(tmp_path, name, kind):
    s, result = _check_site(name, kind, tmp_path)
    assert s["expect"][kind](result), f"{name}/{kind}: unexpected result {result!r}"


@pytest.mark.parametrize("kind", KINDS_ABSENT)
@pytest.mark.parametrize("name", sorted(n for n in SITES if "missing" in SITES[n]["expect"]))
def test_site_absent_unchanged(tmp_path, name, kind):
    """Missing and dangling-symlink DBs keep exactly the early return / failure
    they had (the SessionStart `isfile` -> `exists` fold must not move them)."""
    s, result = _check_site(name, kind, tmp_path)
    assert s["expect"][kind](result), f"{name}/{kind}: unexpected result {result!r}"


# --------------------------------------------------------------------------
# dev parity: a regular-file user sees the SAME result as origin/dev
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def dev_hook_dir(tmp_path_factory):
    override = os.environ.get("SQLITE_3628S2D_DEV_HOOK_DIR")
    if override:
        return Path(override)
    dest = tmp_path_factory.mktemp("dev_hooks")
    tar = dest / "hooks.tar"
    # The merge-base with origin/dev, not its moving tip: the parity claim is
    # "this change did not alter a regular file's behaviour", measured against
    # the code this branch was cut from.
    mb = subprocess.run(["git", "-C", str(PROJECT_ROOT), "merge-base", "origin/dev", "HEAD"],
                        capture_output=True, text=True)
    head = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if mb.returncode != 0 or head.returncode != 0:
        pytest.skip(f"origin/dev is not available for the parity cells: {mb.stderr.strip()[:120]}")
    if mb.stdout.strip() == head.stdout.strip():
        pytest.skip("HEAD is already contained in origin/dev: nothing to compare against")
    r = subprocess.run(["git", "-C", str(PROJECT_ROOT), "archive", "-o", str(tar),
                        mb.stdout.strip(), "src/engram/hooks/claude"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot archive the merge-base for the parity cells: {r.stderr.strip()[:120]}")
    subprocess.run(["tar", "-xf", str(tar), "-C", str(dest)], check=True)
    return dest / "src" / "engram" / "hooks" / "claude"


# Sites whose recipe output includes how the connect was reached are compared on
# the result only for kinds where dev and HEAD must agree. `surface_main_idf_gate`
# has no missing/dangling variant that is meaningful in isolation but still
# compares equal, so it is included.
@pytest.mark.parametrize("kind", KINDS_PARITY)
@pytest.mark.parametrize("name", sorted(SITES))
def test_regular_file_parity_with_dev(tmp_path, dev_hook_dir, name, kind):
    """Rule: NO behaviour change for a regular file, error paths included. A
    missing path, a dangling symlink, a valid DB, and an unreadable (mode 000)
    regular DB each give the same result at HEAD as at origin/dev."""
    _skip_if_unreadable_impossible(kind)
    s = SITES[name]
    outs = []
    for hook_dir in (HOOK_DIR, dev_hook_dir):
        ctx = Ctx(tmp_path / ("head" if hook_dir == HOOK_DIR else "dev"))
        plant(ctx.pack if s["path"] == "pack" else ctx.db, kind)
        outs.append(run_child(hook_dir, s["hook"], ctx.paths(), s["code"], ctx.env())[0])
    head, dev = outs
    assert head == dev, f"{name}/{kind}: HEAD {head!r} != origin/dev {dev!r}"


# --------------------------------------------------------------------------
# helper cells: _hooklib.connect_regular_ro
# --------------------------------------------------------------------------

HOOKLIB = "_hooklib.py"


def _helper(tmp_path, code, kind=None, allow_hang=False):
    ctx = Ctx(tmp_path)
    if kind:
        plant(ctx.db, kind)
    return run_child(HOOK_DIR, HOOKLIB, ctx.paths(), code, ctx.env(), allow_hang=allow_hang)[0]


_RAW_VS_HELPER = (
    "def err(f):\n"
    "    try:\n"
    "        f(); return None\n"
    "    except Exception as e:\n"
    "        return [type(e).__name__, str(e)]\n"
    "raw = err(lambda: sqlite3.connect('file:' + P['db'] + '?mode=ro', uri=True))\n"
    "got = err(lambda: mod.connect_regular_ro(P['db']))\n"
    "result = [raw, got]\n")


@pytest.mark.parametrize("kind", KINDS_BAD)
def test_helper_refuses_non_regular_with_dual_class(tmp_path, kind):
    r = _helper(tmp_path, (
        "try:\n"
        "    mod.connect_regular_ro(P['db']); result = 'no-error'\n"
        "except Exception as e:\n"
        "    result = [isinstance(e, sqlite3.OperationalError), isinstance(e, OSError),\n"
        "              isinstance(e, sqlite3.DatabaseError), type(e).__name__, str(e),\n"
        "              type(e).__mro__[1].__name__, len(CALLS)]\n"), kind)
    assert r[:4] == [True, True, True, "NotRegularDatabaseError"], r
    assert "not a regular file" in r[4] and r[5] == "OperationalError", r
    assert r[6] == 0, "the raw connect must not run for a non-regular file"


def test_helper_refusal_is_one_class_object_per_process(tmp_path):
    r = _helper(tmp_path, (
        "def grab():\n"
        "    try:\n"
        "        mod.connect_regular_ro(P['db'])\n"
        "    except Exception as e:\n"
        "        return type(e)\n"
        "result = [grab() is grab(), grab() is not sqlite3.OperationalError]\n"), "fifo")
    assert r == [True, True]


@pytest.mark.parametrize("kind", KINDS_ABSENT)
def test_helper_missing_and_dangling_fall_through_to_the_raw_error(tmp_path, kind):
    raw, got = _helper(tmp_path, _RAW_VS_HELPER, kind)
    assert raw is not None and raw == got, (raw, got)
    assert got[0] == "OperationalError" and "unable to open database file" in got[1]


@pytest.mark.skipif(IS_ROOT, reason="needs an unstatable path; root can stat anything")
def test_helper_unstatable_path_falls_through_to_the_raw_error(tmp_path):
    ctx = Ctx(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "k.db").write_text("x")
    os.chmod(locked, 0)
    try:
        paths = dict(ctx.paths(), db=str(locked / "k.db"))
        raw, got = run_child(HOOK_DIR, HOOKLIB, paths, _RAW_VS_HELPER, ctx.env())[0]
    finally:
        os.chmod(locked, 0o700)
    assert raw is not None and raw == got, (raw, got)


def test_helper_regular_file_connects_and_queries(tmp_path):
    r = _helper(tmp_path, (
        "c = mod.connect_regular_ro(P['db'])\n"
        "n = c.execute('SELECT count(*) FROM nodes').fetchone()[0]\n"
        "try:\n"
        "    c.execute(\"INSERT INTO focus_state VALUES (9,'x')\"); w = 'wrote'\n"
        "except sqlite3.OperationalError as e:\n"
        "    w = str(e)\n"
        "result = [n, w, CALLS[0][0].split('?', 1)[1]]\n"), "regular")
    assert r[0] == 1 and "readonly" in r[1].replace(" ", "").lower() and r[2] == "mode=ro", r


def test_helper_symlink_to_regular_file_works(tmp_path):
    ctx = Ctx(tmp_path)
    make_db(tmp_path / "real.db")
    os.symlink(str(tmp_path / "real.db"), ctx.db)
    r = run_child(HOOK_DIR, HOOKLIB, ctx.paths(),
                  "c = mod.connect_regular_ro(P['db'])\n"
                  "result = c.execute('SELECT count(*) FROM nodes').fetchone()[0]\n", ctx.env())[0]
    assert r == 1


def test_helper_passes_kwargs_and_uri_params_through_untouched(tmp_path):
    r = _helper(tmp_path, (
        "mod.connect_regular_ro(P['db']).close()\n"
        "mod.connect_regular_ro(P['db'], timeout=0.2).close()\n"
        "mod.connect_regular_ro(P['db'], uri_params='mode=ro&immutable=1').close()\n"
        "result = CALLS\n"), "regular")
    uri = "file:" + str(Path(tmp_path) / "home" / ".engram" / "knowledge.db")
    assert r == [[uri + "?mode=ro", {"uri": True}],
                 [uri + "?mode=ro", {"uri": True, "timeout": 0.2}],
                 [uri + "?mode=ro&immutable=1", {"uri": True}]], r


def test_hooklib_does_not_import_sqlite3_at_module_level():
    """Every hook imports _hooklib; sqlite3 is imported lazily inside the helper."""
    tree = ast.parse((HOOK_DIR / "_hooklib.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name.split(".")[0] for n in top for a in n.names] + \
            [n.module.split(".")[0] for n in top if isinstance(n, ast.ImportFrom) and n.module]
    assert "sqlite3" not in names, names


# --------------------------------------------------------------------------
# surface daemon: tombstone write, PID read (cleanup), PID write
# --------------------------------------------------------------------------

DAEMON = "engram-surface-daemon.py"


def _dplant(path: Path, kind: str, text="123\n"):
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind in ("regular", "regular000"):
        path.write_text(text)
        if kind == "regular000":
            os.chmod(path, 0)


def _stderr_lines(stderr: str, needle="is not a regular file"):
    return [l for l in stderr.splitlines() if needle in l]


_TOMBSTONE = (
    "mod._last_activity = 0\n"
    "mod.idle_watchdog(1)\n"
    "p = os.path.join(P['eh'], 'daemon-idle-shutdown')\n"
    "result = [kind_of(p), open(p).read().isdigit() if kind_of(p) == 'reg' else None]\n")


@pytest.mark.parametrize("kind", KINDS_BAD)
def test_daemon_tombstone_non_regular_logs_once_and_continues(tmp_path, kind):
    ctx = Ctx(tmp_path)
    _dplant(ctx.eh / "daemon-idle-shutdown", kind)
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _TOMBSTONE, ctx.env())
    assert result[0] == ("fifo" if kind == "fifo" else "other")  # left untouched, no crash
    assert len(_stderr_lines(err)) == 1, err
    assert "Idle for 1s, shutting down." in err


def test_daemon_tombstone_regular_file_is_written(tmp_path):
    ctx = Ctx(tmp_path)
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _TOMBSTONE, ctx.env())
    assert result == ["reg", True] and not _stderr_lines(err), (result, err)


@pytest.mark.skipif(IS_ROOT, reason="mode 000 does not stop root")
def test_daemon_tombstone_regular_file_error_is_still_silent(tmp_path):
    """The existing `except OSError: pass` for a REGULAR file is unchanged: no
    extra stderr line, no crash."""
    ctx = Ctx(tmp_path)
    _dplant(ctx.eh / "daemon-idle-shutdown", "regular000", text="1")
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(),
                            "mod._last_activity = 0\nmod.idle_watchdog(1)\nresult = 'returned'",
                            ctx.env())
    assert result == "returned" and not _stderr_lines(err), (result, err)


_CLEANUP = (
    "try:\n"
    "    mod.cleanup()\n"
    "except SystemExit as e:\n"
    "    code = e.code\n"
    "p = os.path.join(P['eh'], 'recall-daemon.pid')\n"
    "result = [code, kind_of(p)]\n")


@pytest.mark.parametrize("kind", KINDS_BAD)
def test_daemon_cleanup_non_regular_pid_path_is_left_alone(tmp_path, kind):
    ctx = Ctx(tmp_path)
    _dplant(ctx.eh / "recall-daemon.pid", kind)
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _CLEANUP, ctx.env())
    assert result == [0, "fifo" if kind == "fifo" else "other"], result  # not unlinked
    assert len(_stderr_lines(err)) == 1, err


@pytest.mark.parametrize("text,gone", [
    ("OWN", True),          # names this process -> unlinked (needs the live pid)
    ("999999999\n", False),  # names another process -> kept
    ("garbage", False),     # #4068: unparseable is NOT confirmed identity -> kept
    ("", False),            # #4068: the ftruncate/O_TRUNC-then-write window -> kept
])
def test_daemon_cleanup_unlinks_only_on_confirmed_identity(tmp_path, text, gone):
    """Mutation: restore the garbage-to-unlink fallback -> the garbage and
    empty rows redden."""
    ctx = Ctx(tmp_path)
    code = ("open(os.path.join(P['eh'], 'recall-daemon.pid'), 'w').write("
            + ("str(os.getpid())" if text == "OWN" else repr(text)) + ")\n" + _CLEANUP)
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), code, ctx.env())
    assert result == [0, None if gone else "reg"], result
    # one warning for every kept file; the own-pid unlink is silent
    assert len(_stderr_lines(err, "leaving it in place")) == (0 if gone else 1), err


@pytest.mark.skipif(IS_ROOT, reason="mode 000 does not stop root")
def test_daemon_cleanup_unreadable_regular_pidfile_is_left_with_a_warning(tmp_path):
    """#4068: a regular pidfile that cannot be READ is not confirmed ours, so
    it is kept (it used to be unlinked by the open-failure fallback)."""
    ctx = Ctx(tmp_path)
    _dplant(ctx.eh / "recall-daemon.pid", "regular000", text="1")
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _CLEANUP, ctx.env())
    assert result == [0, "reg"], (result, err)
    assert len(_stderr_lines(err, "leaving it in place")) == 1, err


_WRITE_PID = (
    "try:\n"
    "    mod._write_pidfile(); out = 'returned'\n"
    "except OSError as e:\n"
    "    out = 'raised:' + type(e).__name__\n"
    "p = os.path.join(P['eh'], 'recall-daemon.pid')\n"
    "ok = kind_of(p) == 'reg' and os.access(p, os.R_OK)\n"
    "result = [out, kind_of(p), open(p).read() == str(os.getpid()) if ok else None]\n")


@pytest.mark.parametrize("kind", KINDS_BAD)
def test_daemon_pid_write_non_regular_logs_once_and_runs_without_pidfile(tmp_path, kind):
    ctx = Ctx(tmp_path)
    _dplant(ctx.eh / "recall-daemon.pid", kind)
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _WRITE_PID, ctx.env())
    assert result[:2] == ["returned", "fifo" if kind == "fifo" else "other"], result
    assert len(_stderr_lines(err)) == 1, err


def test_daemon_pid_write_regular_file(tmp_path):
    ctx = Ctx(tmp_path)
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _WRITE_PID, ctx.env())
    assert result == ["returned", "reg", True] and not _stderr_lines(err)


@pytest.mark.skipif(IS_ROOT, reason="mode 000 does not stop root")
def test_daemon_pid_write_regular_file_permission_error_is_one_named_line_and_exit_77(tmp_path):
    """#4068 point 4: a PermissionError on a REGULAR pidfile used to propagate
    as a traceback out of main(). It is now one named stderr line (path and
    remedy), the refusal back-off, then EXIT_PIDFILE_UNWRITABLE (77). Mutation:
    drop the mapping -> PermissionError escapes and this cell reddens."""
    ctx = Ctx(tmp_path)
    _dplant(ctx.eh / "recall-daemon.pid", "regular000", text="1")
    code = ("try:\n"
            "    mod._write_pidfile(); result = ['returned']\n"
            "except SystemExit as e:\n"
            "    result = ['exit', e.code]\n")
    env = dict(ctx.env(), ENGRAM_DAEMON_REFUSAL_BACKOFF_S="0.01")
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), code, env)
    assert result == ["exit", 77], (result, err)
    named = [l for l in err.splitlines() if "recall-daemon.pid" in l]
    assert len(named) == 1 and "writable" in named[0] and "remove" in named[0], err
    assert "Traceback" not in err


_SPY_EXIT = ("events = []\n"
             "mod._wait_refusal_backoff = lambda s: events.append(['backoff', s])\n"
             "try:\n"
             "    mod._write_pidfile(); events.append(['returned'])\n"
             "except SystemExit as e:\n"
             "    events.append(['exit', e.code])\n"
             "result = events\n")


@pytest.mark.skipif(IS_ROOT, reason="mode 000 does not stop root")
def test_daemon_unwritable_pidfile_backs_off_once_before_the_exit(tmp_path):
    """#4068 round 2: the back-off is the hot-loop guard under Restart=always /
    KeepAlive, so pin it: called exactly once, with the configured seconds,
    BEFORE the exit 77. Mutation: delete the `_wait_refusal_backoff(...)` call
    from `_write_pidfile` -> the events list has no 'backoff' and this reddens."""
    ctx = Ctx(tmp_path)
    _dplant(ctx.eh / "recall-daemon.pid", "regular000", text="1")
    env = dict(ctx.env(), ENGRAM_DAEMON_REFUSAL_BACKOFF_S="0.25")
    result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _SPY_EXIT, env)
    assert result == [["backoff", 0.25], ["exit", 77]], (result, err)


@pytest.mark.skipif(IS_ROOT, reason="mode 500 does not stop root")
def test_daemon_absent_pidfile_in_unwritable_dir_names_the_directory_and_exits_77(tmp_path):
    """#4068 round 2: EACCES on the write also happens when the pidfile does NOT
    exist and its directory is not writable. The line must say so (not "exists
    but"), keep exit 77 and keep the back-off. Mutation: use the exists-case
    text for both -> the wording asserts redden."""
    ctx = Ctx(tmp_path)
    os.chmod(ctx.eh, 0o500)
    try:
        env = dict(ctx.env(), ENGRAM_DAEMON_REFUSAL_BACKOFF_S="0.25")
        result, err = run_child(HOOK_DIR, DAEMON, ctx.paths(), _SPY_EXIT, env)
    finally:
        os.chmod(ctx.eh, 0o700)
    assert result == [["backoff", 0.25], ["exit", 77]], (result, err)
    named = [l for l in err.splitlines() if "recall-daemon.pid" in l]
    assert len(named) == 1, err
    assert "exists but" not in named[0]
    assert "directory" in named[0] and str(ctx.eh) in named[0], named[0]
    assert "writable by the user that runs the daemon" in named[0], named[0]
    assert "Traceback" not in err


# --------------------------------------------------------------------------
# AST guards
# --------------------------------------------------------------------------

def _raw_modero_connects(source: str, filename: str):
    """(lineno, enclosing function) of every sqlite3.connect whose first argument
    mentions mode=ro (covers plain strings and f-strings)."""
    tree = ast.parse(source)
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    hits = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "connect" and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "sqlite3" and n.args):
            continue
        if "mode=ro" not in (ast.get_source_segment(source, n.args[0]) or ""):
            continue
        fn, cur = None, n
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = cur.name
                break
        hits.append((n.lineno, fn))
    return hits


def test_no_hook_keeps_a_raw_modero_connect():
    offenders = []
    for path in sorted(HOOK_DIR.glob("*.py")):
        for lineno, fn in _raw_modero_connects(path.read_text(encoding="utf-8"), path.name):
            offenders.append(f"{path.name}:{lineno} in {fn}")
    assert offenders == [], offenders


@pytest.mark.parametrize("snippet", [
    "import sqlite3\nsqlite3.connect(f'file:{p}?mode=ro', uri=True)\n",
    "import sqlite3\ndef f():\n    return sqlite3.connect('file:x?mode=ro&immutable=1', uri=True, timeout=1)\n",
])
def test_modero_guard_detects_planted_connects(snippet):
    """Control: the scanner flags each planted form, so the guard cannot be
    vacuously green. (The helper's own connect builds its URI from a variable
    query string, so it is not a literal mode=ro site and needs no exemption.)"""
    assert _raw_modero_connects(snippet, "x.py"), "scanner missed a planted mode=ro connect"


def test_modero_guard_ignores_other_connects():
    assert _raw_modero_connects("import sqlite3\nsqlite3.connect(p, timeout=1)\n"
                                "sock.connect(p)\n", "x.py") == []


def _builtin_open_calls(source: str):
    """(lineno, first-arg source) of every builtin open() call."""
    tree = ast.parse(source)
    return [(n.lineno, ast.get_source_segment(source, n.args[0]) if n.args else "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"]


def test_daemon_has_no_builtin_open_except_the_procfs_read():
    calls = _builtin_open_calls((HOOK_DIR / DAEMON).read_text(encoding="utf-8"))
    assert [a for _l, a in calls] == ["proc_net_unix"], calls  # /proc/net/unix, out of scope


@pytest.mark.parametrize("snippet", [
    "open(PID_PATH)\n", "with open(PID_PATH, 'w') as f:\n    pass\n",
    "open(tombstone_path, 'w')\n",
])
def test_daemon_open_guard_detects_planted_calls(snippet):
    assert _builtin_open_calls(snippet), "scanner missed a planted open()"


# --------------------------------------------------------------------------
# the two `_is_non_regular` copies are pinned to each other
# --------------------------------------------------------------------------
# A hook cannot import from another hook, so engram-surface-hook.py and
# engram-surface-daemon.py each carry a private `_is_non_regular`. These two
# cells keep them one predicate: the AST cell for the code, the behavioural
# cell for the observable result.

SURFACE_HOOK = "engram-surface-hook.py"


def _non_regular_dump(path: Path):
    """(argument-list dump, docstring-less body dump) of the module-level
    `_is_non_regular` in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == "_is_non_regular"]
    assert len(funcs) == 1, f"{path.name}: expected exactly one top-level def"
    fn = funcs[0]
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # drop the docstring: wording may differ, code may not
    assert body, f"{path.name}: _is_non_regular has no code after its docstring"
    return ast.dump(fn.args), "\n".join(ast.dump(s) for s in body)


def test_is_non_regular_copies_have_identical_ast():
    hook = _non_regular_dump(HOOK_DIR / SURFACE_HOOK)
    daemon = _non_regular_dump(HOOK_DIR / DAEMON)
    assert hook[0] == daemon[0], "_is_non_regular argument lists differ"
    assert hook[1] == daemon[1], (
        "_is_non_regular differs between engram-surface-hook.py and "
        "engram-surface-daemon.py: the private copies must stay identical (#3628)")


def _plant_kind(tmp_path: Path, kind: str) -> Path:
    p = tmp_path / kind
    if kind == "regular":
        p.write_text("x")
    elif kind == "dangling":
        os.symlink(tmp_path / "no-such-target", p)
    elif kind == "directory":
        p.mkdir()
    elif kind == "fifo":
        os.mkfifo(p)  # the predicate only stats; it never opens, so no hang
    elif kind == "symlink_to_regular":
        (tmp_path / "real").write_text("x")
        os.symlink(tmp_path / "real", p)
    else:
        assert kind == "missing", kind
    return p


@pytest.mark.parametrize("hook_name", [SURFACE_HOOK, DAEMON])
@pytest.mark.parametrize("kind,expected", [
    ("regular", False), ("missing", False), ("dangling", False),
    ("directory", True), ("fifo", True), ("symlink_to_regular", False),
])
def test_is_non_regular_behaviour_is_the_same_in_both_copies(
        tmp_path, hook_name, kind, expected):
    p = _plant_kind(tmp_path, kind)
    result, _err = run_child(HOOK_DIR, hook_name, {"p": str(p)},
                             "result = mod._is_non_regular(P['p'])", {})
    assert result is expected, (hook_name, kind, result)
