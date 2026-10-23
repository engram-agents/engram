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
  C5  every other creating connect that can run before bootstrap is
      non-creating: the utility-credit Stop hook's bump_utility.

Each server cell runs in a child process so module state (the cached timing
connection, the once-per-path schema trigger) is exactly what a real MCP
server process has. The temp ENGRAM_HOME/HOME keep the live seat untouched.
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
