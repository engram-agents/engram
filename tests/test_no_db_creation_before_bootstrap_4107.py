"""#4107 (second half): nothing but bootstrap creates knowledge.db.

On a fresh home (no knowledge.db) a failed first tool call used to leave an
EMPTY knowledge.db behind: server._get_timing_conn() opened the DB path with a
plain sqlite3.connect (which CREATES a missing file) and made `tool_timing`
in it, from the finally-block of every tool call, including the first call,
which had just failed because _get_db() refused the missing file. The next
call then found a file, ran the full one-time schema setup on a 0-node store,
and bootstrap.py refused ('knowledge.db already exists').

Contract (stated by the spec; written before the fix):
  C1  fresh home: a failed first tool call leaves NO knowledge.db (nor -wal/-shm)
      behind, and no exception reaches the caller beyond the tool's own error.
  C2  fresh home: the engramd owner refuses to start and creates nothing
      (the existing 'knowledge.db not found' path; a regression guard).
  C3  after bootstrap.py seeds the home (6 nodes, 2 edges), tool timing
      records normally again, in the SAME server process that failed first.
  C4  a healthy, seeded home: unchanged (timing recorded).
  C5  the utility-credit Stop hook's bump_utility is non-creating. (This is ONE
      site, not a closure claim: the class is enforced by C7.)
  C6  the surface hook's `_advance_person_moment_cursor` (a WRITE path, direct /
      flag-OFF) on a fresh home leaves NO knowledge.db/-wal/-shm. The property is
      "never creates", not "returns False" (it already returned False before the
      fix; its `except` swallowed the error and left the stray file). Healthy
      control: on a seeded home the direct path still opens read-write.
  C7  the CLASS, as far as a static scan sees it: every connect under
      src/engram/hooks/ (all subdirectories, found with `ast`) reached through
      `sqlite3.connect`, any alias of the sqlite3 module (`import sqlite3 as sq`,
      `s = sqlite3`), any name bound to its connect (`from sqlite3 import connect
      [as Y]`, `c = sq.connect`, a parameter defaulting to it), or
      `connect_regular_ro(`, is classified by its TARGET database. A connect
      whose path can resolve to knowledge.db must be a URI open whose query part
      (after the first '?', before any '#') has only mode=ro / mode=rw
      parameters, at least one (never mode=rwc, never no mode; the first
      argument is read as its string constants joined in order, so a mode token
      in a wrong parameter, a fragment, or a dead branch does not count). For
      connect_regular_ro the uri_params argument, when given, must pass the same
      test; the default is mode=ro. Every other connect must be listed in
      ALLOWED_CONNECTS, keyed "relative/path.py:function", with the database it
      opens, why creating that database is intended, and the expected
      first-argument shape; only a connect with exactly that first argument is
      exempt, so any other connect in the same function is classified normally
      (other-db sites are NOT forced to mode=rw: that would break a fresh home
      the other way). Unlisted sites fail, naming file:line. Dynamic access
      (`getattr(<sqlite3 alias>, ...)`, `vars(...)`, `.__dict__`) and
      `from sqlite3 import *` cannot be classified and fail closed. Planted
      controls run through the same scan (escape forms are flagged; legitimate
      forms pass; a listed function with an extra knowledge.db connect is
      flagged; a stale entry is detected).
      Also fail closed: any `<sqlite3 alias>.Connection` / `.dbapi2` (outside a pure
      type annotation) and any from-import of them or of a sqlite3 submodule; an
      assignment storing the sqlite3 module or its connect into an attribute or
      subscript, or in a tuple/list value; any non-constant piece in a knowledge.db
      URI query; `*args`/`**kwargs` or a non-constant uri_params to connect_regular_ro.
      The helper's own default (uri_params="mode=ro", uri=True, first argument) is
      pinned by a separate test, because the helper is listed and so skipped.
      WHAT IT DOES NOT SEE, so no "every connect" claim: connects built
      dynamically (`importlib`, `__import__`, `exec`), other drivers, anything
      outside src/engram/hooks/, a knowledge path held in a name outside
      _KNOWLEDGE_NAMES (treated as non-knowledge, so it must be listed), two
      same-named functions sharing one ALLOWED_CONNECTS entry, string annotations
      (never parsed, so `'sqlite3.Connection(p)'` passes; it only opens if
      something evaluates the annotation), a connection built from an existing
      one (`c.__class__(p)`, `type(c)(p)`), and the sqlite3 module escaping by
      any route other than an attribute/subscript/tuple rebind (passed as an
      argument, returned, looped over). The helper pin checks connect_regular_ro's
      declared `uri_params` default and its inner call shape, not a reassignment
      of `uri_params` inside the helper body; and it accepts a mode=rw default,
      like every other site, so it does not mean "must be ro". KNOWN RESIDUAL, not closed
      here: connect_regular_ro does not quote the path, so a '#' (or '?') in the
      path drops mode=ro and creates an empty db; tracked in engram-alpha#4171.

Each server cell runs in a child process so module state (the cached timing
connection, the once-per-path schema trigger) is exactly what a real MCP
server process has. The temp ENGRAM_HOME/HOME keep the live seat untouched.
"""

from __future__ import annotations

import ast
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

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-utility-credit-mention-stop.py"

_SERVER_CHILD = r"""
import json, os, sqlite3, subprocess, sys
sys.path.insert(0, os.environ["SRC_ENGRAM"])
import server

home = os.environ["ENGRAM_HOME"]
db = os.path.join(home, "knowledge.db")
out = {}

def call():
    try:
        server.engram_stats("{}")
        return "ok"
    except Exception as exc:  # the tool's own error, e.g. the not-found RuntimeError
        return "raised:" + type(exc).__name__

def listing():
    return sorted(n for n in os.listdir(home) if n.startswith("knowledge.db"))

def timing_rows():
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        return c.execute("SELECT count(*) FROM tool_timing").fetchone()[0]
    finally:
        c.close()

mode = os.environ["CHILD_MODE"]
if mode == "fail_only":
    out["first"] = call()
    out["after_first"] = listing()
    out["second"] = call()
    out["after_second"] = listing()
elif mode in ("fail_then_bootstrap", "seeded"):
    if mode == "fail_then_bootstrap":
        out["first"] = call()
        out["after_first"] = listing()
    env = dict(os.environ)
    env.pop("ENGRAM_BOOTSTRAP", None)
    r = subprocess.run([sys.executable, os.path.join(os.environ["SRC_ENGRAM"], "bootstrap.py")],
                       env=env, capture_output=True, text=True, timeout=120)
    out["bootstrap_rc"] = r.returncode
    out["bootstrap_tail"] = (r.stdout + r.stderr)[-600:]
    if os.path.exists(db):
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        out["nodes"] = c.execute("SELECT count(*) FROM nodes").fetchone()[0]
        out["edges"] = c.execute("SELECT count(*) FROM edges").fetchone()[0]
        c.close()
    out["after_call"] = call()
    out["call_again"] = call()
    try:
        out["timing_rows"] = timing_rows()
    except Exception as exc:
        out["timing_rows"] = "err:" + type(exc).__name__
print("CHILD_JSON " + json.dumps(out))
"""


def _env(home: Path, claude_home: Path, mode: str) -> dict:
    env = os.environ.copy()
    env.update({
        "ENGRAM_HOME": str(home),
        "HOME": str(home.parent),
        "CLAUDE_HOME": str(claude_home),
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(SRC_ENGRAM / "templates"),
        "PYTHON_BIN": sys.executable,
        "TARGET": "claude",
        "SRC_ENGRAM": str(SRC_ENGRAM),
        "CHILD_MODE": mode,
        "ENGRAM_NO_EMBEDDINGS": "1",
    })
    # The guards must be ON: that is the production posture this contract is about.
    for k in ("ENGRAM_NO_DB_GUARDS", "ENGRAM_BOOTSTRAP"):
        env.pop(k, None)
    return env


@pytest.fixture
def fresh_home():
    work = Path(tempfile.mkdtemp(prefix="g4107-"))
    home = work / ".engram"
    claude_home = work / ".claude"
    home.mkdir()
    claude_home.mkdir()
    try:
        yield home, claude_home
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_child(home: Path, claude_home: Path, mode: str) -> dict:
    r = subprocess.run([sys.executable, "-c", _SERVER_CHILD], env=_env(home, claude_home, mode),
                       capture_output=True, text=True, timeout=240)
    for line in r.stdout.splitlines():
        if line.startswith("CHILD_JSON "):
            return json.loads(line[len("CHILD_JSON "):])
    raise AssertionError(f"child produced no result (rc={r.returncode})\nstdout={r.stdout}\nstderr={r.stderr}")


# --- C1 -----------------------------------------------------------------------------------------

def test_c1_failed_first_tool_call_leaves_no_knowledge_db(fresh_home):
    home, claude_home = fresh_home
    out = _run_child(home, claude_home, "fail_only")
    assert out["first"].startswith("raised:"), out  # the not-found refusal is still the tool's answer
    assert out["after_first"] == [], f"a failed first call left files behind: {out['after_first']}"
    # A second failed call must not create it either (the once-per-path trigger already fired).
    assert out["second"].startswith("raised:"), out
    assert out["after_second"] == [], out["after_second"]


def test_c1_record_tool_timing_alone_does_not_create_the_file(fresh_home):
    home, claude_home = fresh_home
    code = (
        "import os, sys; sys.path.insert(0, os.environ['SRC_ENGRAM']); import server\n"
        "server._record_tool_timing('engram_stats', 3, 'error')\n"
        "print('LEFT', sorted(n for n in os.listdir(os.environ['ENGRAM_HOME']) if n.startswith('knowledge.db')))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], env=_env(home, claude_home, "none"),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "LEFT []" in r.stdout, r.stdout + r.stderr


# --- C3 / C4 ------------------------------------------------------------------------------------

def test_c3_after_bootstrap_timing_records_in_the_same_process(fresh_home):
    home, claude_home = fresh_home
    out = _run_child(home, claude_home, "fail_then_bootstrap")
    assert out["first"].startswith("raised:"), out
    assert out["after_first"] == [], out["after_first"]
    assert out["bootstrap_rc"] == 0, out["bootstrap_tail"]  # bootstrap is not refused: nothing pre-created the file
    assert (out["nodes"], out["edges"]) == (6, 2), out
    assert out["after_call"] == "ok", out
    assert isinstance(out["timing_rows"], int) and out["timing_rows"] >= 1, out  # C4 rides along: healthy path records


def test_c4_healthy_seeded_home_records_timing(fresh_home):
    home, claude_home = fresh_home
    out = _run_child(home, claude_home, "seeded")
    assert out["bootstrap_rc"] == 0, out["bootstrap_tail"]
    assert (out["nodes"], out["edges"]) == (6, 2), out
    assert out["after_call"] == "ok", out
    assert isinstance(out["timing_rows"], int) and out["timing_rows"] >= 1, out


# --- C2 -----------------------------------------------------------------------------------------

@pytest.mark.owner_schema_gate_real  # the real gate refuses the absent store; nothing is stamped (child process)
def test_c2_owner_start_on_a_fresh_home_creates_no_knowledge_db(fresh_home):
    import platform
    if platform.system() != "Linux":
        pytest.skip("owner runs on Linux only")
    home, _ = fresh_home
    code = (
        "import os, sys; sys.path.insert(0, os.environ['SRC_ENGRAM'])\n"
        "import engramd_owner as eo\n"
        "from pathlib import Path\n"
        "h = Path(os.environ['ENGRAM_HOME'])\n"
        "o = eo.EngramdOwner(h, request_deadline=0.6)\n"
        "try:\n"
        "    o.start(install_signal_handlers=False)\n"
        "    print('STARTED')\n"
        "    o.stop()\n"
        "except eo.OwnerStoreBehindSchema as e:\n"
        "    print('REFUSED', 'no knowledge.db' in str(e))\n"
        "print('LEFT', sorted(n for n in os.listdir(h) if n.startswith('knowledge.db')))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], env=_env(home, home.parent / ".claude", "none"),
                       capture_output=True, text=True, timeout=120)
    assert "REFUSED True" in r.stdout and "LEFT []" in r.stdout, r.stdout + r.stderr


# --- C5 -----------------------------------------------------------------------------------------

def test_c5_utility_credit_hook_bump_does_not_create_a_missing_db(fresh_home):
    home, claude_home = fresh_home
    code = (
        "import importlib.util, os, sys\n"
        "sys.path.insert(0, os.environ['SRC_ENGRAM'])\n"
        "spec = importlib.util.spec_from_file_location('ucm', os.environ['HOOK_PATH'])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "db = os.path.join(os.environ['ENGRAM_HOME'], 'knowledge.db')\n"
        "try:\n"
        "    m.bump_utility(['ob_0001'], None, db, restamp=True, config_path=os.path.join(os.environ['ENGRAM_HOME'], 'config.json'))\n"
        "    print('NO-RAISE')\n"
        "except Exception as e:\n"
        "    print('RAISED', type(e).__name__)\n"
        "print('LEFT', sorted(n for n in os.listdir(os.environ['ENGRAM_HOME']) if n.startswith('knowledge.db')))\n"
    )
    env = _env(home, claude_home, "none")
    env["HOOK_PATH"] = str(HOOK_PATH)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert "LEFT []" in r.stdout, r.stdout + r.stderr


def test_c5_utility_credit_hook_bump_still_works_on_an_existing_db(fresh_home):
    """Healthy-path control: the non-creating connect still opens a real, seeded store read-write."""
    home, claude_home = fresh_home
    out = _run_child(home, claude_home, "seeded")
    assert out["bootstrap_rc"] == 0, out
    db = home / "knowledge.db"
    code = (
        "import importlib.util, os, sys\n"
        "sys.path.insert(0, os.environ['SRC_ENGRAM'])\n"
        "spec = importlib.util.spec_from_file_location('ucm', os.environ['HOOK_PATH'])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "import sqlite3\n"
        "c = sqlite3.connect(os.environ['DBP']); nid = c.execute(\"SELECT id FROM nodes WHERE type='axiom' LIMIT 1\").fetchone()[0]; c.close()\n"
        "n = m.bump_utility([nid], None, os.environ['DBP'], restamp=True, config_path=os.path.join(os.environ['ENGRAM_HOME'], 'config.json'))\n"
        "print('UPDATED', n)\n"
    )
    env = _env(home, claude_home, "none")
    env.update({"HOOK_PATH": str(HOOK_PATH), "DBP": str(db)})
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert "UPDATED 1" in r.stdout, r.stdout + r.stderr


# --- C6 -----------------------------------------------------------------------------------------

SURFACE_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-surface-hook.py"

_C6_CODE = (
    "import importlib.util, os, sys\n"
    "sys.path.insert(0, os.environ['SRC_ENGRAM'])\n"
    "sys.path.insert(0, os.path.dirname(os.environ['HOOK_PATH']))\n"
    "spec = importlib.util.spec_from_file_location('sh', os.environ['HOOK_PATH'])\n"
    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "assert m._OWNER_RUN is None  # flag-OFF / direct path\n"
    "def spy(conn, pid, frm):\n"
    "    conn.execute('CREATE TABLE IF NOT EXISTS c6_probe(x)')\n"
    "    return {'applied': True}\n"
    "m._advance_moment_cursor = spy\n"
    "print('RESULT', m._advance_person_moment_cursor('pn_x', 0))\n"
    "print('LEFT', sorted(n for n in os.listdir(os.environ['ENGRAM_HOME']) if n.startswith('knowledge.db')))\n"
)


def _run_c6(home, claude_home):
    env = _env(home, claude_home, "none")
    env["HOOK_PATH"] = str(SURFACE_HOOK_PATH)
    return subprocess.run([sys.executable, "-c", _C6_CODE], env=env, capture_output=True, text=True, timeout=120)


def test_c6_person_moment_cursor_advance_does_not_create_a_missing_db(fresh_home):
    home, claude_home = fresh_home
    r = _run_c6(home, claude_home)
    assert "LEFT []" in r.stdout, r.stdout + r.stderr  # never creates (not: "returns False")
    assert "RESULT False" in r.stdout, r.stdout + r.stderr


def test_c6_person_moment_cursor_advance_still_opens_rw_on_a_seeded_db(fresh_home):
    """Healthy control: the non-creating connect still opens the real store read-write.
    Weaker than a real applied advance (that needs a person node with moments): a spy stands in for
    the shared CAS and writes a probe table; True + the committed table prove the RW open + commit."""
    home, claude_home = fresh_home
    out = _run_child(home, claude_home, "seeded")
    assert out["bootstrap_rc"] == 0, out
    r = _run_c6(home, claude_home)
    assert "RESULT True" in r.stdout, r.stdout + r.stderr
    c = sqlite3.connect(f"file:{home / 'knowledge.db'}?mode=ro", uri=True)
    try:
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='c6_probe'").fetchone()[0] == 1
    finally:
        c.close()


# --- C7 -----------------------------------------------------------------------------------------

HOOKS_ROOT = SRC_ENGRAM / "hooks"

# Names that can resolve to knowledge.db (traced by reading each hook: every db_path / DB_PATH /
# KNOWLEDGE_DB_PATH / ENGRAM_DB_PATH in hooks/ is built from ENGRAM_HOME + "knowledge.db", or from an
# attached pack's "knowledge.db"). A literal "knowledge.db" in the first argument also classifies it.
_KNOWLEDGE_NAMES = {"db_path", "DB_PATH", "KNOWLEDGE_DB_PATH", "ENGRAM_DB_PATH", "KNOWLEDGE_DB"}

# key "relative/path.py:function" -> (database opened, why a plain/creating open is intended or safe,
# expected FIRST-ARGUMENT shape as a Python expression). Only a connect whose first argument is exactly
# that expression is exempt; any other connect in the same function is classified normally.
ALLOWED_CONNECTS = {
    "claude/_hooklib.py:connect_regular_ro": (
        "knowledge.db (caller-supplied)",
        "the helper itself: it builds f'file:{db_path}?{uri_params}' with uri_params defaulting to "
        "mode=ro; each CALLER is checked at its own call site by this scan",
        'f"file:{db_path}?{uri_params}"',
    ),
    "claude/engram-session-start-hook.py:_check_json1": (
        ":memory: (a capability probe; an injectable factory defaulting to sqlite3.connect)",
        "opens no file at all, so it creates nothing on disk",
        '":memory:"',
    ),
}


def _static_strings(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


_HOLE = "\x00"  # stands for any part of the first argument that is not a string constant


def _flatten(node):
    """The first argument as text: string constants and f-string constants in order, `+` tails joined,
    everything else (names, calls, conditionals, formatted values) as an opaque hole."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_flatten(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten(node.left) + _flatten(node.right)
    return _HOLE


def _query_ok(query):
    """Every `mode=` parameter of the query is ro or rw, there is at least one, and no piece is non-constant."""
    if _HOLE in query:  # a non-constant piece could carry anything (another mode=, rwc): fails closed
        return False
    modes = [p.partition("=")[2] for p in query.split("&") if p.partition("=")[0] == "mode"]
    return bool(modes) and all(m in ("ro", "rw") for m in modes)


def _mode_ok(first):
    """The URI's QUERY part (after the first '?', before any '#') carries mode=ro|rw."""
    text = _flatten(first).split("#", 1)[0]
    return "?" in text and _query_ok(text.split("?", 1)[1])


def _uri_params_ok(node):
    return _query_ok(_flatten(node).split("#", 1)[0])


def _arg_is_knowledge(arg):
    idents = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
    idents |= {n.attr for n in ast.walk(arg) if isinstance(n, ast.Attribute)}
    return bool(idents & _KNOWLEDGE_NAMES) or any("knowledge.db" in s for s in _static_strings(arg))


def _shape_dump(src):
    return ast.dump(ast.parse(src, mode="eval").body)


def _is_connect_ref(node, mods, names):
    """`<sqlite3 alias>.connect`, or a bare name bound to sqlite3's connect."""
    return ((isinstance(node, ast.Attribute) and node.attr == "connect"
             and isinstance(node.value, ast.Name) and node.value.id in mods)
            or (isinstance(node, ast.Name) and node.id in names))


def _resolve_names(tree):
    """Per-file: names bound to the sqlite3 module (`import sqlite3 [as X]`, `s = <alias>`) and to its
    connect (`from sqlite3 import connect [as Y]`, `c = <alias>.connect`, a parameter defaulting to it).
    Returns (module names, connect names, star-import nodes). Tuple-unpacking assignments are not followed."""
    mods, names, stars = set(), set(), []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.asname or "sqlite3" for a in n.names if a.name == "sqlite3"}
        elif isinstance(n, ast.ImportFrom) and n.module == "sqlite3" and not n.level:
            for a in n.names:
                if a.name == "connect":
                    names.add(a.asname or "connect")
                elif a.name == "*":
                    stars.append(n)
    bindings = []  # (target names, value node)
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            bindings.append(([t.id for t in n.targets if isinstance(t, ast.Name)], n.value))
        elif isinstance(n, ast.AnnAssign) and n.value is not None and isinstance(n.target, ast.Name):
            bindings.append(([n.target.id], n.value))
        elif isinstance(n, ast.arguments):
            pos = n.posonlyargs + n.args
            pairs = list(zip(pos[len(pos) - len(n.defaults):], n.defaults)) + list(zip(n.kwonlyargs, n.kw_defaults))
            for arg, dflt in pairs:
                if dflt is not None:
                    bindings.append(([arg.arg], dflt))
    changed = True
    while changed:
        changed = False
        for targets, value in bindings:
            if isinstance(value, ast.Name) and value.id in mods:
                bucket = mods
            elif _is_connect_ref(value, mods, names):
                bucket = names
            else:
                continue
            for t in targets:
                if t not in bucket:
                    bucket.add(t)
                    changed = True
    return mods, names, stars


_EQUIV = ("Connection", "dbapi2")  # sqlite3 attributes/submodules that open a database like connect does


def _is_sqlite_ref(node, mods, names):
    return (isinstance(node, ast.Name) and node.id in mods) or _is_connect_ref(node, mods, names)


def _is_rebind(node, mods, names):
    """An assignment that stores the sqlite3 module or its connect where this scan cannot follow it: into an
    attribute or subscript, or via a tuple/list value that contains one."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    value = node.value
    if any(isinstance(t, (ast.Attribute, ast.Subscript)) for t in targets) and _is_sqlite_ref(value, mods, names):
        return True
    return isinstance(value, (ast.Tuple, ast.List)) and any(_is_sqlite_ref(n, mods, names) for n in ast.walk(value))


def _annotation_only_ids(tree):
    """ids of every node inside a type annotation (parameter, return, AnnAssign) that holds no call, lambda or
    walrus: `conn: sqlite3.Connection` names a type and cannot open anything."""
    ann = []
    for n in ast.walk(tree):
        if isinstance(n, ast.arg) and n.annotation is not None:
            ann.append(n.annotation)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None:
            ann.append(n.returns)
        elif isinstance(n, ast.AnnAssign):
            ann.append(n.annotation)
    ids = set()
    for a in ann:
        sub = list(ast.walk(a))
        if not any(isinstance(x, (ast.Call, ast.Lambda, ast.NamedExpr)) for x in sub):
            ids |= {id(x) for x in sub}
    return ids


def _iter_sites(tree):
    """Yield (enclosing function name, node, kind) for every connect-like site: kind is 'connect' (a call
    through any sqlite3 connect name), 'equiv' (sqlite3.Connection / .dbapi2 or an import of them), 'rebind'
    (the module or its connect stored in an attribute/subscript/tuple), 'regular_ro', 'alias' (a connect reference that is not itself a call),
    'dynamic' (getattr/vars/__dict__ on the sqlite3 module: cannot be classified) or 'star'."""
    mods, names, stars = _resolve_names(tree)
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    type_only = _annotation_only_ids(tree)
    for s in stars:
        yield "<module>", s, "star"

    def visit(node, func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        kind = None
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlite3" and not node.level \
                and (node.module != "sqlite3" or any(a.name in _EQUIV for a in node.names)):
            kind = "equiv"  # from sqlite3 import Connection/dbapi2, or from sqlite3.<sub> import ...
        elif isinstance(node, ast.Import) and any(a.name.startswith("sqlite3.") for a in node.names):
            kind = "equiv"  # import sqlite3.dbapi2
        elif isinstance(node, ast.Attribute) and node.attr in _EQUIV and id(node) not in type_only \
                and isinstance(node.value, ast.Name) and node.value.id in mods:
            kind = "equiv"  # <sqlite3 alias>.Connection / .dbapi2
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and _is_rebind(node, mods, names):
            kind = "rebind"
        elif isinstance(node, ast.Call):
            f = node.func
            if _is_connect_ref(f, mods, names):
                kind = "connect"
            elif (isinstance(f, ast.Name) and f.id == "connect_regular_ro") or (
                    isinstance(f, ast.Attribute) and f.attr == "connect_regular_ro"):
                kind = "regular_ro"
            elif (isinstance(f, ast.Name) and f.id in ("getattr", "vars") and node.args
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in mods):
                kind = "dynamic"
        elif isinstance(node, ast.Attribute) and node.attr == "__dict__" \
                and isinstance(node.value, ast.Name) and node.value.id in mods:
            kind = "dynamic"
        elif _is_connect_ref(node, mods, names) and id(node) not in call_funcs \
                and isinstance(getattr(node, "ctx", None), ast.Load):
            kind = "alias"
        if kind:
            yield func, node, kind
        for child in ast.iter_child_nodes(node):
            yield from visit(child, func)

    yield from visit(tree, "<module>")


def _first_arg(call):
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    return (call.args[0] if call.args else kw.get("database")), kw


def scan_connects(path, rel, allowed):
    """Return a list of 'rel:line: reason' violations for one file."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    out = []
    for func, node, kind in _iter_sites(tree):
        key = f"{rel}:{func}"
        line = node.lineno
        if kind == "star":
            out.append(f"{rel}:{line}: `from sqlite3 import *` hides which names are connects")
        elif kind == "dynamic":
            out.append(f"{rel}:{line}: dynamic access to the sqlite3 module cannot be classified (fails closed)")
        elif kind in ("equiv", "rebind"):
            if key not in allowed:
                what = ("sqlite3.Connection/dbapi2 is a connect-equivalent" if kind == "equiv"
                        else "the sqlite3 module or its connect is stored where the scan cannot follow it")
                out.append(f"{rel}:{line}: {what}, not listed in ALLOWED_CONNECTS ({key})")
        elif kind == "alias":
            if key not in allowed:
                out.append(f"{rel}:{line}: sqlite3.connect referenced without a call, not listed in ALLOWED_CONNECTS ({key})")
        else:
            first, kw = _first_arg(node)
            knowledge = first is not None and _arg_is_knowledge(first)
            if kind == "connect":
                entry = allowed.get(key)
                if entry and first is not None and ast.dump(first) == _shape_dump(entry[2]):
                    continue  # exactly the listed shape; any other connect in this function is classified below
                if knowledge:
                    uri = kw.get("uri")
                    uri_true = isinstance(uri, ast.Constant) and uri.value is True
                    if not (uri_true and _mode_ok(first)):
                        out.append(f"{rel}:{line}: connect of knowledge.db is not a mode=ro/mode=rw URI open")
                else:
                    out.append(f"{rel}:{line}: connect of a non-knowledge (or unresolved) target is not exempt: "
                               f"not listed in ALLOWED_CONNECTS, or not the listed first-argument shape ({key})")
            elif knowledge:  # connect_regular_ro: default mode=ro; an explicit uri_params must name ro/rw
                up = kw.get("uri_params") or (node.args[1] if len(node.args) > 1 else None)
                if any(k.arg is None for k in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args):
                    out.append(f"{rel}:{line}: connect_regular_ro with *args/**kwargs cannot be classified (fails closed)")
                elif up is not None and not _uri_params_ok(up):
                    out.append(f"{rel}:{line}: connect_regular_ro uri_params lacks mode=ro/mode=rw")
            else:
                out.append(f"{rel}:{line}: connect_regular_ro of a non-knowledge (or unresolved) target is not listed ({key})")
    return out


def _scan_tree(allowed):
    out = []
    for p in sorted(HOOKS_ROOT.rglob("*.py")):
        out += scan_connects(p, p.relative_to(HOOKS_ROOT).as_posix(), allowed)
    return out


def _allowed_problems(allowed, root):
    """Stale-entry check: the file and function exist and still hold a connect with the listed shape."""
    problems = []
    for key, (db, why, shape) in allowed.items():
        rel, _, fn = key.partition(":")
        p = Path(root) / rel
        if not (db and len(why) > 20 and shape):
            problems.append(f"{key}: entry lacks a database, a justification, or a shape")
        if not p.is_file():
            problems.append(f"{key}: file does not exist")
            continue
        tree = ast.parse(p.read_text(), filename=str(p))
        if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn for n in ast.walk(tree)):
            problems.append(f"{key}: function does not exist")
            continue
        want = _shape_dump(shape)
        if not any(f == fn and k == "connect" and _first_arg(n)[0] is not None and ast.dump(_first_arg(n)[0]) == want
                   for f, n, k in _iter_sites(tree)):
            problems.append(f"{key}: no connect with first argument {shape} remains in the function")
    return problems


def test_c7_every_hook_connect_is_non_creating_or_listed():
    found = _scan_tree(ALLOWED_CONNECTS)
    assert not found, "creating/unlisted sqlite connects under src/engram/hooks/:\n" + "\n".join(found)


def test_c7_allowed_entries_are_live_and_justified():
    assert _allowed_problems(ALLOWED_CONNECTS, HOOKS_ROOT) == []


def test_c7_scan_covers_real_sites():
    """The scan must actually see the hook tree (a vacuous scan would pass trivially)."""
    seen = 0
    for p in HOOKS_ROOT.rglob("*.py"):
        seen += len(re.findall(r"connect_regular_ro\(|sqlite3\.connect", p.read_text()))
    assert seen >= 20, seen


def test_c7_planted_controls_prove_the_scan_can_fail(tmp_path):
    kn = tmp_path / "planted_kn.py"
    kn.write_text(
        "import sqlite3\n"
        "def f(KNOWLEDGE_DB_PATH):\n"
        "    return sqlite3.connect(KNOWLEDGE_DB_PATH)\n"           # line 3: plain open of knowledge.db
        "def g(KNOWLEDGE_DB_PATH):\n"
        "    return sqlite3.connect(\n"                              # line 5: multi-line plain open
        "        KNOWLEDGE_DB_PATH,\n"
        "        timeout=2.0)\n"
        "def h(db_path):\n"
        "    return sqlite3.connect(f'file:{db_path}?mode=rwc', uri=True)\n"  # line 9: mode=rwc
    )
    got = scan_connects(kn, "planted_kn.py", {})
    assert sorted(int(g.split(":")[1]) for g in got) == [3, 5, 9], got

    other = tmp_path / "planted_other.py"
    other.write_text("import sqlite3\nimport os\ndef cache(home):\n    return sqlite3.connect(os.path.join(home, 'cache.db'))\n")
    assert len(scan_connects(other, "planted_other.py", {})) == 1  # unlisted other-db: flagged
    listed = {"planted_other.py:cache": ("cache.db", "the cache is meant to be created on first use", "os.path.join(home, 'cache.db')")}
    assert scan_connects(other, "planted_other.py", listed) == []  # listed: passes

    good = tmp_path / "planted_good.py"
    good.write_text(
        "import sqlite3\n"
        "def f(db_path):\n"
        "    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)\n"
        "def g(db_path):\n"
        "    return sqlite3.connect(Path(db_path).absolute().as_uri() + '?mode=rw', uri=True)\n"
    )
    assert scan_connects(good, "planted_good.py", {}) == [], "a mode=ro/rw URI open must pass"


# --- C7 round 2: escape-class controls (written red against the round-1 scanner, then fixed) ---

_ESCAPES = {
    "module_alias": "import sqlite3 as sq\ndef f(KNOWLEDGE_DB_PATH):\n    return sq.connect(KNOWLEDGE_DB_PATH)\n",
    "from_import": "from sqlite3 import connect\ndef f(KNOWLEDGE_DB_PATH):\n    return connect(KNOWLEDGE_DB_PATH)\n",
    "from_import_as": "from sqlite3 import connect as cx\ndef f(KNOWLEDGE_DB_PATH):\n    return cx(KNOWLEDGE_DB_PATH)\n",
    "getattr_module": "import sqlite3\ndef f(p):\n    return getattr(sqlite3, 'connect')(p)\n",
    "getattr_alias": "import sqlite3 as sq\ndef f(p):\n    return getattr(sq, 'conn' + 'ect')(p)\n",
    "local_var_via_alias": "import sqlite3 as sq\ndef f(KNOWLEDGE_DB_PATH):\n    c = sq.connect\n    return c(KNOWLEDGE_DB_PATH)\n",
    "module_rebound": "import sqlite3\ns = sqlite3\ndef f(KNOWLEDGE_DB_PATH):\n    return s.connect(KNOWLEDGE_DB_PATH)\n",
    "mode_in_wrong_param": "import sqlite3\ndef f(db_path):\n    return sqlite3.connect(f'file:{db_path}?vfs=mode=ro', uri=True)\n",
    "mode_in_dead_branch": "import sqlite3\ndef f(db_path):\n    return sqlite3.connect(db_path if 'mode=ro' else db_path, uri=True)\n",
    "mode_in_fragment": "import sqlite3\ndef f(db_path):\n    return sqlite3.connect(f'file:{db_path}?x=1#mode=ro', uri=True)\n",
    "mode_dup_rwc": "import sqlite3\ndef f(db_path):\n    return sqlite3.connect(f'file:{db_path}?mode=ro&mode=rwc', uri=True)\n",
    "ro_helper_wrong_param": "def f(db_path):\n    return connect_regular_ro(db_path, uri_params='vfs=mode=ro')\n",
    "ro_helper_rwc": "def f(db_path):\n    return connect_regular_ro(db_path, uri_params='mode=rwc')\n",
    # round 3, item 2: Connection / dbapi2 are connect-equivalents
    "connection_attr": "import sqlite3\ndef f(KNOWLEDGE_DB_PATH):\n    return sqlite3.Connection(KNOWLEDGE_DB_PATH)\n",
    "connection_attr_alias": "import sqlite3 as sq\ndef f(KNOWLEDGE_DB_PATH):\n    return sq.Connection(KNOWLEDGE_DB_PATH)\n",
    "connection_from_import": "from sqlite3 import Connection\ndef f(KNOWLEDGE_DB_PATH):\n    return Connection(KNOWLEDGE_DB_PATH)\n",
    "dbapi2_attr": "import sqlite3\ndef f(KNOWLEDGE_DB_PATH):\n    return sqlite3.dbapi2.connect(KNOWLEDGE_DB_PATH)\n",
    "dbapi2_from_import": "from sqlite3 import dbapi2\ndef f(KNOWLEDGE_DB_PATH):\n    return dbapi2.connect(KNOWLEDGE_DB_PATH)\n",
    "connection_call_in_annotation": "import sqlite3\ndef f(c: sqlite3.Connection(KNOWLEDGE_DB_PATH)):\n    return c\n",
    # round 3, item 3: attribute / subscript / tuple rebinds
    "rebind_attr_module": "import sqlite3\nclass K:\n    def __init__(self):\n        self.s = sqlite3\n",
    "rebind_subscript_connect": "import sqlite3\nd = {}\nd['c'] = sqlite3.connect\n",
    "rebind_attr_connect_name": "from sqlite3 import connect\nclass K:\n    def __init__(self):\n        self.c = connect\n",
    "rebind_tuple_module": "import sqlite3\na, b = sqlite3, 1\n",
    "rebind_tuple_connect": "import sqlite3\na, b = (sqlite3.connect, 1)\n",
    "rebind_tuple_connect_name": "from sqlite3 import connect\na, b = [connect, 1]\n",
    # round 3, item 4: any non-constant piece in the URI query
    "query_hole_after": "import sqlite3\ndef f(db_path, extra):\n    return sqlite3.connect(f'file:{db_path}?mode=ro&{extra}', uri=True)\n",
    "query_hole_before": "import sqlite3\ndef f(db_path, extra):\n    return sqlite3.connect(f'file:{db_path}?{extra}&mode=ro', uri=True)\n",
    "ro_helper_kwargs": "def f(db_path, kw):\n    return connect_regular_ro(db_path, **kw)\n",
    "ro_helper_nonconst_params": "def f(db_path, up):\n    return connect_regular_ro(db_path, uri_params=up)\n",
    "ro_helper_hole_in_params": "def f(db_path, e):\n    return connect_regular_ro(db_path, uri_params=f'mode=ro&{e}')\n",
}

_PASSES = {
    "ro_alias": "import sqlite3 as sq\ndef f(db_path):\n    return sq.connect(f'file:{db_path}?mode=ro&immutable=1', uri=True)\n",
    "from_import_rw": "from sqlite3 import connect\ndef f(db_path):\n    return connect(db_path.as_uri() + '?mode=rw', uri=True)\n",
    "type_annotation_only": "import sqlite3\ndef f(c: sqlite3.Connection, d: 'sqlite3.Connection' = None) -> sqlite3.Connection:\n    x: sqlite3.Connection = c\n    return x\n",
    "ro_helper_default": "def f(db_path):\n    return connect_regular_ro(db_path, timeout=1.0)\n",
    "ro_helper_immutable": "def f(db_path):\n    return connect_regular_ro(db_path, uri_params='mode=ro&immutable=1')\n",
}

_PROBE_WHY = "an in-memory capability probe, creates nothing"


@pytest.mark.parametrize("name", sorted(_ESCAPES))
def test_c7_round2_escape_is_flagged(tmp_path, name):
    p = tmp_path / "planted.py"
    p.write_text(_ESCAPES[name])
    assert scan_connects(p, "planted.py", {}), f"escape not flagged: {name}"


@pytest.mark.parametrize("name", sorted(_PASSES))
def test_c7_round2_legitimate_forms_still_pass(tmp_path, name):
    p = tmp_path / "planted.py"
    p.write_text(_PASSES[name])
    assert scan_connects(p, "planted.py", {}) == []


def test_c7_round2_listed_key_is_not_a_blanket_pass(tmp_path):
    p = tmp_path / "planted.py"
    p.write_text(
        "import sqlite3\n"
        "def probe(KNOWLEDGE_DB_PATH):\n"
        "    a = sqlite3.connect(':memory:')\n"                    # matches the listed shape: exempt
        "    return sqlite3.connect(KNOWLEDGE_DB_PATH)\n")        # line 4: same function, not the shape
    listed = {"planted.py:probe": ("memory", _PROBE_WHY, "':memory:'")}
    got = scan_connects(p, "planted.py", listed)
    assert [int(g.split(":")[1]) for g in got] == [4], got


def test_c7_round2_stale_entries_are_detected(tmp_path):
    (tmp_path / "m.py").write_text("import sqlite3\ndef probe():\n    return sqlite3.connect(':memory:')\n")
    ok = {"m.py:probe": ("memory", _PROBE_WHY, "':memory:'")}
    assert _allowed_problems(ok, tmp_path) == []
    gone_func = {"m.py:renamed": ("memory", _PROBE_WHY, "':memory:'")}
    gone_shape = {"m.py:probe": ("memory", _PROBE_WHY, "'other.db'")}
    gone_file = {"nope.py:probe": ("memory", _PROBE_WHY, "':memory:'")}
    for bad in (gone_func, gone_shape, gone_file):
        assert _allowed_problems(bad, tmp_path), bad


# --- C7 round 3, item 1: pin connect_regular_ro's own default (the helper is listed, so the scan skips it) ---

_HELPER_FILE = HOOKS_ROOT / "claude" / "_hooklib.py"
_HELPER_FIRST_ARG = 'f"file:{db_path}?{uri_params}"'


def _helper_problems(src):
    """What the ALLOWED_CONNECTS entry for connect_regular_ro silently assumes: the `uri_params` default is a
    string constant passing the same query check, its inner sqlite3.connect passes uri=True as a literal
    keyword, and its first argument is the listed shape."""
    tree = ast.parse(src)
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "connect_regular_ro"]
    if len(fns) != 1:
        return [f"expected exactly one def connect_regular_ro, found {len(fns)}"]
    fn, out = fns[0], []
    a = fn.args
    pos = a.posonlyargs + a.args
    dflt = dict(zip([x.arg for x in pos[len(pos) - len(a.defaults):]], a.defaults))
    dflt.update({x.arg: d for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None})
    d = dflt.get("uri_params")
    if not (isinstance(d, ast.Constant) and isinstance(d.value, str) and _query_ok(d.value)):
        out.append("uri_params default is not a string constant passing the mode=ro/rw query check")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "connect"]
    if len(calls) != 1:
        out.append(f"expected exactly one inner connect call, found {len(calls)}")
    for c in calls:
        uri = {k.arg: k.value for k in c.keywords if k.arg}.get("uri")
        if not (isinstance(uri, ast.Constant) and uri.value is True):
            out.append("inner connect does not pass uri=True as a literal keyword")
        if not (c.args and ast.dump(c.args[0]) == _shape_dump(_HELPER_FIRST_ARG)):
            out.append(f"inner connect's first argument is not {_HELPER_FIRST_ARG}")
    return out


def test_c7_round3_connect_regular_ro_default_is_pinned():
    assert _helper_problems(_HELPER_FILE.read_text()) == []


@pytest.mark.parametrize("old,new", [
    ('uri_params="mode=ro"', 'uri_params="mode=rwc"'),
    ('uri_params="mode=ro"', 'uri_params=None'),
    ('f"file:{db_path}?{uri_params}", uri=True, **connect_kwargs)', 'f"file:{db_path}?{uri_params}", **connect_kwargs)'),
    ('f"file:{db_path}?{uri_params}", uri=True, **connect_kwargs)', 'f"file:{db_path}", uri=True, **connect_kwargs)'),
])
def test_c7_round3_helper_mutations_are_caught(old, new):
    src = _HELPER_FILE.read_text()
    assert src.count(old) == 1, old
    assert _helper_problems(src.replace(old, new)), f"mutation not caught: {old!r} -> {new!r}"
