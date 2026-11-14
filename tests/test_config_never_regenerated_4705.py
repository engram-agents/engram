"""Contract-first tests for engram#4705: a lost config.json is never regenerated.

Written BEFORE the fix, from the design contract (C1-C10, cells 1-16).

THE HARNESS RULE.  tests/conftest.py sets ENGRAM_NO_DB_GUARDS=1 for the whole
pytest session, and ``_get_db`` counts that as bootstrap mode, so a guard cell run
in-process bypasses the guard and passes vacuously.  Every guard cell (1, 2, 5, 7,
8) therefore runs its call in a SUBPROCESS with ENGRAM_NO_DB_GUARDS and
ENGRAM_BOOTSTRAP popped (precedent: test_no_db_creation_before_bootstrap_4107.py,
"The guards must be ON"), and carries a paired CONTROL: the same call with
ENGRAM_NO_DB_GUARDS=1 set, which must NOT raise the guard's error, so a later
conftest change that re-arms the bypass turns the cell red instead of quietly
green.  Cell 3 is the opposite: it SETS ENGRAM_BOOTSTRAP=1.

Contract (design file 4705-design.md, draft 7)
  The default config is written ONLY when knowledge.db does not exist, or in
  bootstrap mode (ENGRAM_BOOTSTRAP=1 / ENGRAM_NO_DB_GUARDS=1).  Otherwise, with
  config.json absent:
  C2  a seeded DB: ``_get_db`` raises ConfigMissingError (path, backups, new-seat
      command); every tool refuses; nothing is written.
  C3  a DB that exists but holds no usable graph: refuse, write nothing, never the
      config-missing text.  C3a (readable SQLite, a ``nodes`` table, zero axioms):
      the seed-missing text plus the knowledge-pack sentence.  C3b (zero bytes, not
      SQLite, or no ``nodes`` table): the damaged-DB message.
  C4  DB and config both present: unchanged.
  C5  the sandbox follows the same write rule (and keeps its own literal).
  C6  the new-seat command: human-invoked, confirmation flag, writes only when absent.
  C7  fresh install (no DB) and bootstrap: the default is written as today.
  C8  install.sh refuses before the buffer build (config absent, DB present).
  C9  the session-start hook says so on one line.
  C10 the remedy text lists backups, leaves the choice to the user, labels them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_install_sh_4097 import Sandbox  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-session-start-hook.py"
NEW_SEAT_TOOL = SRC_ENGRAM / "tools" / "write-default-config.py"
INSTALL_SH = SRC_ENGRAM / "tools" / "install.sh"

PREFIX = "config.json.engram-backup."

# The key remedy phrases (cell 12): each must appear in BOTH the Python
# ConfigMissingError message and install.sh's ERR line.
PHRASES = [
    "config.json is missing while knowledge.db exists",
    "user decides which backup to restore",
    "may itself be a regenerated default",
    "cp -p <the backup you chose>",
    "write-default-config.py --confirm-new-seat",
]
NEW_SEAT_FLAG = "--confirm-new-seat"
C9_LINE = (
    "⚠️  ENGRAM substrate health: config.json is missing while knowledge.db exists; "
    "ENGRAM tools refuse until it is restored from a backup you choose "
    "(see the tool error for candidates)."
)
DAMAGED_PHRASE = "not a readable ENGRAM database"
SEED_PHRASE = "has no seed nodes"
PACK_SENTENCE = "if this is a knowledge pack, its config.json is missing too; restore it from backup"


# ----------------------------------------------------------------------------- harness

def _env(home: Path, *, guards_on: bool = True, bootstrap: bool = False, extra: dict | None = None) -> dict:
    env = os.environ.copy()
    env.update({
        "ENGRAM_HOME": str(home),
        "HOME": str(home.parent),
        "CLAUDE_HOME": str(home.parent / ".claude"),
        "SRC_ENGRAM": str(SRC_ENGRAM),
        "ENGRAM_NO_EMBEDDINGS": "1",
        "ENGRAM_NO_POLARITY": "1",
    })
    # The guards must be ON: that is the production posture this contract is about.
    env.pop("ENGRAM_NO_DB_GUARDS", None)
    env.pop("ENGRAM_BOOTSTRAP", None)
    if not guards_on:
        env["ENGRAM_NO_DB_GUARDS"] = "1"  # the paired control
    if bootstrap:
        env["ENGRAM_BOOTSTRAP"] = "1"  # cell 3 only
    env.update(extra or {})
    return env


_PRELUDE = r"""
import json, os, sys, hashlib
sys.path.insert(0, os.environ["SRC_ENGRAM"])
import server
import engram_core as core
import engram_config_io as cio
home = os.environ["ENGRAM_HOME"]
cfg = os.path.join(home, "config.json")
db = os.path.join(home, "knowledge.db")
out = {}
def attempt(fn):
    try:
        r = fn()
        return {"raised": None, "result": repr(r)[:300]}
    except BaseException as exc:  # the refusal is the answer; the process must live on
        return {"raised": type(exc).__name__, "msg": str(exc)}
"""


def _child(home: Path, body: str, **kw) -> dict:
    code = _PRELUDE + body + "\nprint('CHILD_JSON ' + json.dumps(out))\n"
    r = subprocess.run([sys.executable, "-c", code], env=_env(home, **kw), capture_output=True,
                       text=True, timeout=240, cwd=str(home.parent))
    for line in r.stdout.splitlines():
        if line.startswith("CHILD_JSON "):
            return json.loads(line[len("CHILD_JSON "):])
    raise AssertionError(f"child produced no result (rc={r.returncode})\nstdout={r.stdout[-1500:]}\nstderr={r.stderr[-1500:]}")


def _seed(home: Path) -> None:
    """A real, seeded home (bootstrap.py: 6 nodes, config.json written), built under the guards' own rules."""
    home.mkdir(parents=True, exist_ok=True)
    (home.parent / ".claude").mkdir(exist_ok=True)
    env = _env(home, extra={
        "ALPHA_DIR": str(REPO_ROOT), "ALPHA_TEMPLATES_DIR": str(SRC_ENGRAM / "templates"),
        "PYTHON_BIN": sys.executable, "TARGET": "claude",
    })
    r = subprocess.run([sys.executable, str(SRC_ENGRAM / "bootstrap.py")], env=env, capture_output=True,
                       text=True, timeout=240)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert (home / "knowledge.db").exists() and (home / "config.json").exists()


@pytest.fixture
def work():
    d = Path(tempfile.mkdtemp(prefix="g4705-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def home(work):
    h = work / ".engram"
    h.mkdir()
    return h


@pytest.fixture
def seeded_no_config(home):
    _seed(home)
    (home / "config.json").unlink()
    for p in home.glob(PREFIX + "*"):
        p.unlink()
    return home


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _backup(home: Path, stamp: str, content) -> Path:
    p = home / (PREFIX + stamp)
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    return p


def _assert_config_missing_message(msg: str, home: Path) -> None:
    assert str(home / "config.json") in msg, msg
    assert "cp -p" in msg, msg
    for ph in (PHRASES[0], PHRASES[4]):
        assert ph in msg, (ph, msg)


# ----------------------------------------------------------------------------- cell 1

@pytest.mark.parametrize("with_backup", [False, True])
def test_cell1_seeded_db_no_config_raises_config_missing_and_writes_nothing(seeded_no_config, with_backup):
    home = seeded_no_config
    bk = _backup(home, "20260101T000000Z", {"self_lineage": "anthropic:opus"}) if with_backup else None
    out = _child(home, "out['r'] = attempt(lambda: core._get_db().close())\nout['cfg'] = os.path.exists(cfg)\n")
    assert out["r"]["raised"] == "ConfigMissingError", out
    assert out["cfg"] is False, "config.json was written while knowledge.db exists"
    msg = out["r"]["msg"]
    _assert_config_missing_message(msg, home)
    if bk is not None:
        assert "20260101T000000Z" in msg
    assert not (home / "config.json").exists()


def test_cell1_control_guards_off_does_not_raise_the_guard(seeded_no_config):
    out = _child(seeded_no_config, "out['r'] = attempt(lambda: core._get_db().close())\n", guards_on=False)
    assert out["r"]["raised"] != "ConfigMissingError", out


# ----------------------------------------------------------------------------- cell 2

_CELL2 = r"""
out['read'] = attempt(lambda: server.engram_stats("{}"))
out['write'] = attempt(lambda: server.engram_add_conjecture(json.dumps({"claim": "c4705", "basis": "b4705"})))
out['alive'] = attempt(lambda: server.engram_stats("{}"))  # the same process answers a third time
out['cfg'] = os.path.exists(cfg)
"""


def test_cell2_two_tool_routes_refuse_and_the_process_stays_alive(seeded_no_config):
    home = seeded_no_config
    before = _sha(home / "knowledge.db")
    out = _child(home, _CELL2)
    for route in ("read", "write", "alive"):
        assert out[route]["raised"] == "ConfigMissingError", (route, out[route])
        _assert_config_missing_message(out[route]["msg"], home)
    assert out["cfg"] is False
    assert not (home / "config.json").exists()
    # nothing written: the write tool did not reach the store
    con = sqlite3.connect(f"file:{home / 'knowledge.db'}?mode=ro", uri=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM nodes WHERE claim LIKE '%c4705%'").fetchone()[0]
    finally:
        con.close()
    assert n == 0
    del before


def test_cell2_control_guards_off_does_not_raise_the_guard(seeded_no_config):
    out = _child(seeded_no_config, _CELL2, guards_on=False)
    assert out["read"]["raised"] != "ConfigMissingError", out


# ----------------------------------------------------------------------------- cell 3

def test_cell3_bootstrap_writes_the_default_although_the_guard_exists(home):
    # premise: the guard exists (no bootstrap flag, no DB -> the DB-missing refusal, nothing created)
    out = _child(home, "out['r'] = attempt(lambda: core._get_db().close())\nout['cfg'] = os.path.exists(cfg)\n")
    assert out["r"]["raised"] == "RuntimeError" and "knowledge.db not found" in out["r"]["msg"], out
    assert out["cfg"] is False and not (home / "knowledge.db").exists()
    # the bootstrap flag SET (ENGRAM_NO_DB_GUARDS popped): the default is written
    out = _child(home, "out['r'] = attempt(lambda: core._get_db().close())\nout['cfg'] = os.path.exists(cfg)\n",
                 bootstrap=True)
    assert out["r"]["raised"] is None, out
    assert out["cfg"] is True and (home / "knowledge.db").exists()
    assert json.loads((home / "config.json").read_text())["self_lineage"] == ""


def test_cell3b_bootstrap_flag_bypasses_the_refusal_on_an_existing_db(seeded_no_config):
    """Bound stated in the design: re-running first-session (ENGRAM_BOOTSTRAP=1) bypasses the refusal."""
    out = _child(seeded_no_config, "out['r'] = attempt(lambda: core._get_db().close())\nout['cfg'] = os.path.exists(cfg)\n",
                 bootstrap=True)
    assert out["r"]["raised"] is None, out
    assert out["cfg"] is True


def test_cell3c_fresh_install_no_db_default_written_with_the_guards_on(home):
    """C7: the rule's own DB-absent arm.  No DB, guards ON, no bootstrap flag: _ensure_data_dir writes
    the default (the first run of the MCP server and EngramClient init do exactly this)."""
    out = _child(home, "core._ensure_data_dir()\nout['cfg'] = os.path.exists(cfg)\nout['db'] = os.path.exists(db)\n")
    assert out["cfg"] is True and out["db"] is False, out
    assert json.loads((home / "config.json").read_text())["schema_version"] == 2


# ----------------------------------------------------------------------------- cell 4

def test_cell4_db_and_config_present_config_bytes_unchanged(seeded_no_config):
    home = seeded_no_config
    (home / "config.json").write_text(json.dumps({"self_lineage": "anthropic:opus", "memory": {"current_turn": 41}}, indent=2) + "\n")
    before = _sha(home / "config.json")
    out = _child(home, "out['r'] = attempt(lambda: core._get_db().close())\n")
    assert out["r"]["raised"] is None, out
    assert _sha(home / "config.json") == before


# ----------------------------------------------------------------------------- cell 5

_CELL5 = r"""
target = os.environ["TARGET_HOME"]
with core.engram_sandbox(data_dir=target) as d:
    out['cfg_at_enter'] = os.path.exists(os.path.join(target, "config.json"))
    out['r'] = attempt(lambda: core._get_db().close())
out['cfg_after'] = os.path.exists(os.path.join(target, "config.json"))
"""


def test_cell5_sandbox_on_an_existing_data_dir_refuses_as_in_cell_1(work, seeded_no_config):
    target = seeded_no_config
    other = work / "other" / ".engram"
    other.mkdir(parents=True)
    out = _child(other, _CELL5, extra={"TARGET_HOME": str(target)})
    assert out["cfg_at_enter"] is False, "the sandbox wrote a default config into a data dir that holds a DB"
    assert out["r"]["raised"] == "ConfigMissingError", out
    assert out["cfg_after"] is False
    assert not (target / "config.json").exists()


def test_cell5_control_guards_off_does_not_raise_the_guard(work, seeded_no_config):
    other = work / "other" / ".engram"
    other.mkdir(parents=True)
    out = _child(other, _CELL5, guards_on=False, extra={"TARGET_HOME": str(seeded_no_config)})
    assert out["r"]["raised"] != "ConfigMissingError", out


def test_cell5_sandbox_keeps_its_own_minimal_default_for_a_fresh_dir(work):
    """C5: the sandbox literal stays (trust_pool, confidence_map, memory.tier2_max_nodes 2000; no
    schema_version / cadence / self_lineage), where the dir holds no DB."""
    other = work / "other" / ".engram"
    other.mkdir(parents=True)
    fresh = work / "fresh"
    out = _child(other, "with core.engram_sandbox(data_dir=os.environ['TARGET_HOME']):\n    pass\n",
                 extra={"TARGET_HOME": str(fresh)})
    del out
    got = json.loads((fresh / "config.json").read_text())
    # _ensure_data_dir never overwrites, so the sandbox literal is what lands
    assert got["memory"]["tier2_max_nodes"] == 2000
    assert "schema_version" not in got and "self_lineage" not in got and "cadence" not in got


# ----------------------------------------------------------------------------- cell 6
# #4706's pinned cell (test_pin_4705_db_open_regenerates_default_config_today, in
# test_config_write_guard_1989.py) is REMOVED in this commit: its in-process form is vacuous under
# conftest's ENGRAM_NO_DB_GUARDS=1, and its assertion (the file reappears) is the opposite of the
# contract.  Cell 1 above replaces it, with the guards ON.


# ----------------------------------------------------------------------------- cells 7, 8

_DAMAGED = "out['r'] = attempt(lambda: core._get_db().close())\nout['cfg'] = os.path.exists(cfg)\n"


def _assert_damaged_message(out, home):
    assert out["r"]["raised"] == "RuntimeError", out
    msg = out["r"]["msg"]
    assert DAMAGED_PHRASE in msg, msg
    assert PHRASES[0] not in msg and SEED_PHRASE not in msg, msg
    assert out["cfg"] is False and not (home / "config.json").exists()


def test_cell7_zero_byte_db_no_config_gets_the_damaged_db_message(home):
    (home / "knowledge.db").write_bytes(b"")
    out = _child(home, _DAMAGED)
    _assert_damaged_message(out, home)
    assert (home / "knowledge.db").read_bytes() == b""


def test_cell7_control_guards_off_does_not_raise_the_guard(home):
    (home / "knowledge.db").write_bytes(b"")
    out = _child(home, _DAMAGED, guards_on=False)
    assert DAMAGED_PHRASE not in (out["r"].get("msg") or ""), out


def test_cell8_64_byte_non_sqlite_db_no_config_gets_the_damaged_db_message(home):
    blob = b"this is not a sqlite database file, it is sixty-four bytes of text.\n"[:64].ljust(64, b".")
    assert len(blob) == 64 and not blob.startswith(b"SQLite format 3")
    (home / "knowledge.db").write_bytes(blob)
    out = _child(home, _DAMAGED)
    _assert_damaged_message(out, home)
    assert (home / "knowledge.db").read_bytes() == blob


def test_cell8_control_guards_off_does_not_raise_the_guard(home):
    (home / "knowledge.db").write_bytes(b"x" * 64)
    out = _child(home, _DAMAGED, guards_on=False)
    assert DAMAGED_PHRASE not in (out["r"].get("msg") or ""), out


# ----------------------------------------------------------------------------- cell 9

def test_cell9_readable_sqlite_nodes_table_zero_axioms_gets_the_seed_text_and_pack_sentence(home):
    con = sqlite3.connect(str(home / "knowledge.db"))
    con.execute("CREATE TABLE nodes (id TEXT, type TEXT)")
    con.execute("INSERT INTO nodes VALUES ('ob_1', 'observation')")
    con.commit()
    con.close()
    before = _sha(home / "knowledge.db")
    out = _child(home, _DAMAGED)
    assert out["r"]["raised"] == "RuntimeError", out
    msg = out["r"]["msg"]
    assert SEED_PHRASE in msg and PACK_SENTENCE in msg, msg
    assert PHRASES[0] not in msg and DAMAGED_PHRASE not in msg, msg
    assert out["cfg"] is False and not (home / "config.json").exists()
    assert _sha(home / "knowledge.db") == before, "the refusal wrote to the database file"


# ----------------------------------------------------------------------------- cell 10

def _run_tool(home: Path, *args, check_cfg=True):
    env = _env(home)
    return subprocess.run([sys.executable, str(NEW_SEAT_TOOL), *args], env=env, capture_output=True,
                          text=True, timeout=120, cwd=str(home.parent))


def test_cell10_new_seat_tool_without_the_flag_writes_nothing_and_says_so(home):
    r = _run_tool(home)
    assert r.returncode != 0, r.stdout + r.stderr
    assert not (home / "config.json").exists()
    assert NEW_SEAT_FLAG in (r.stdout + r.stderr)


def test_cell10_new_seat_tool_with_the_flag_writes_the_same_default_and_reports_it(work, home):
    r = _run_tool(home, NEW_SEAT_FLAG)
    assert r.returncode == 0, r.stdout + r.stderr
    cfgp = home / "config.json"
    assert cfgp.exists()
    assert str(cfgp) in r.stdout and "self_lineage" in r.stdout
    # parity with the default _ensure_data_dir writes (one shared default, C6)
    ref = work / "ref" / ".engram"
    ref.mkdir(parents=True)
    out = _child(ref, "core._ensure_data_dir()\n")
    del out
    assert json.loads(cfgp.read_text()) == json.loads((ref / "config.json").read_text())


def test_cell10_new_seat_tool_refuses_and_keeps_bytes_when_config_is_present(home):
    cfgp = home / "config.json"
    cfgp.write_text('{"self_lineage": "anthropic:opus"}\n')
    before = cfgp.read_bytes()
    r = _run_tool(home, NEW_SEAT_FLAG)
    assert r.returncode != 0, r.stdout + r.stderr
    assert cfgp.read_bytes() == before
    assert "exists" in (r.stdout + r.stderr)


def test_cell10_help_says_the_flag_is_the_humans_word_and_cannot_be_checked():
    r = subprocess.run([sys.executable, str(NEW_SEAT_TOOL), "--help"], capture_output=True, text=True,
                       timeout=120, env={**os.environ, "ENGRAM_NO_EMBEDDINGS": "1"})
    text = " ".join(r.stdout.split())
    assert r.returncode == 0, r.stderr
    assert "human" in text and "not the agent" in text
    assert "cannot tell who typed it" in text and "attestation" in text and "does not enforce" in text


# ----------------------------------------------------------------------------- cell 11

def _sandbox_run(sb: Sandbox):
    return sb.run("--yes")


def test_cell11_install_sh_config_absent_db_present_refuses_before_anything_is_written(tmp_path):
    sb = Sandbox(tmp_path)
    sb.make_db(6, config=False)
    r = _sandbox_run(sb)
    assert r.returncode != 0, r.all
    assert "FAIL" in r.all and "[step 3]" in r.all, r.all
    assert not (sb.engram / "config.json").exists(), "the installer created config.json beside a database"
    assert list(sb.engram.glob(PREFIX + "*")) == [] and list(sb.engram.glob("config.json*")) == []
    for ph in PHRASES:
        assert ph in r.all, (ph, r.all)
    # nothing later ran
    assert sb.calls_starting("marketplace") == [] and sb.calls_starting("bootstrap") == []


def test_cell11_control_no_db_the_fresh_create_still_happens(tmp_path):
    sb = Sandbox(tmp_path)
    r = _sandbox_run(sb)
    assert r.returncode == 0, r.all
    cfgd = json.loads((sb.engram / "config.json").read_text())
    assert cfgd["install_tier"]


def test_cell11_control_config_present_the_merge_path_still_writes_install_tier(tmp_path):
    sb = Sandbox(tmp_path)
    sb.make_db(6)
    (sb.engram / "config.json").write_text(json.dumps({"self_lineage": "anthropic:opus", "keep": [1]}, indent=2) + "\n")
    r = _sandbox_run(sb)
    assert r.returncode == 0, r.all
    got = json.loads((sb.engram / "config.json").read_text())
    assert got["install_tier"] and got["self_lineage"] == "anthropic:opus" and got["keep"] == [1]


# ----------------------------------------------------------------------------- cell 12

def test_cell12_remedy_phrases_appear_in_both_the_python_message_and_install_sh(tmp_path):
    sys.path.insert(0, str(SRC_ENGRAM))
    import engram_config_io as cio
    (tmp_path / "knowledge.db").write_bytes(b"")  # the message states "knowledge.db exists" only when it does
    try:
        py_msg = str(cio.ConfigMissingError(tmp_path / "config.json"))
    finally:
        sys.path.remove(str(SRC_ENGRAM))
    sh_text = INSTALL_SH.read_text()
    for ph in PHRASES:
        assert ph in py_msg, ("python", ph, py_msg)
        assert ph in sh_text, ("install.sh", ph)


# ----------------------------------------------------------------------------- cell 13

def _run_hook(home: Path):
    env = _env(home, extra={"AGENTS_SHARED_DIR": str(home.parent / "shared")})
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps({"source": "startup", "session_id": "t4705", "transcript_path": str(home.parent / "t.jsonl")}),
        capture_output=True, text=True, env=env, cwd=str(home.parent), timeout=180,
    )


def _hook_context(r) -> str:
    for line in reversed(r.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)["hookSpecificOutput"]["additionalContext"]
            except (ValueError, KeyError):
                continue
    raise AssertionError("hook emitted no context\n" + r.stdout[-800:] + r.stderr[-800:])


def test_cell13_session_start_hook_says_config_is_missing_once_and_writes_nothing(seeded_no_config):
    home = seeded_no_config
    r = _run_hook(home)
    assert r.returncode == 0, r.stderr[-800:]
    assert _hook_context(r).count(C9_LINE) == 1
    assert not (home / "config.json").exists(), "the hook wrote config.json"


def test_cell13_control_config_present_no_such_line(seeded_no_config):
    home = seeded_no_config
    (home / "config.json").write_text('{"self_lineage": "anthropic:opus"}\n')
    r = _run_hook(home)
    assert r.returncode == 0, r.stderr[-800:]
    assert "config.json is missing while knowledge.db exists" not in _hook_context(r)


# Session-start block markers: copied from BLOCK_MARKERS in tests/test_session_start_owner_read_3735.py
# (the entries are the ones that module asserts on the hook's output).
_BLOCK_MARKERS_3735 = {
    "focus": "Focused nodes",
    "saved": "Saved focus-sets",
    "checkpoint": "Nodes created since checkpoint",
    "sleep": "[SLEEP STATUS",
    "backup": "Git backup: knowledge.sql not found",
}
# The calibration block's marker: CAL_MARK in tests/test_calibration_owner_op_3741.py.
_CAL_MARK = "[ENGRAM Calibration"


def test_cell13b_only_calibration_is_lost_and_c9_added(seeded_no_config):
    """Characterisation of round 1: with config.json absent the hook loses ONLY the calibration block
    (whose EngramClient constructor now refuses) and gains ONLY the C9 line; every other block stays."""
    home = seeded_no_config
    r_absent = _run_hook(home)
    assert r_absent.returncode == 0, r_absent.stderr[-800:]
    absent = _hook_context(r_absent)
    assert not (home / "config.json").exists(), "the hook wrote config.json"
    (home / "config.json").write_text('{"self_lineage": "anthropic:opus"}\n')
    r_present = _run_hook(home)
    assert r_present.returncode == 0, r_present.stderr[-800:]
    present = _hook_context(r_present)
    assert _CAL_MARK in present, ("calibration marker not in the config-present run", present)
    assert _CAL_MARK not in absent, absent
    for key, marker in _BLOCK_MARKERS_3735.items():
        if marker in present:
            assert marker in absent, (key, marker, absent)
    assert C9_LINE in absent and C9_LINE not in present
    assert "config.json is missing while knowledge.db exists" not in present


# ----------------------------------------------------------------------------- cell 14

_CELL14 = r"""
sys.path.insert(0, os.environ["SRC_ENGRAM"])
from engram_client import EngramClient
try:
    c = EngramClient()
    r = c.call("engram_surface", {"query": "anything at all"})
    out["type"] = type(r).__name__
    out["error"] = r.get("error") if isinstance(r, dict) else None
except BaseException as exc:
    out["escaped"] = type(exc).__name__ + ": " + str(exc)[:300]
out["cfg"] = os.path.exists(cfg)
"""


def test_cell14_surface_daemon_shape_returns_an_error_dict_and_raises_nothing(seeded_no_config):
    home = seeded_no_config
    out = _child(home, _CELL14)
    assert "escaped" not in out, out
    assert out["type"] == "dict" and out["error"], out
    assert "config.json" in out["error"], out
    assert out["cfg"] is False


# ----------------------------------------------------------------------------- cells 15, 16

def test_cell15_two_backups_listed_with_stamps_the_user_decides_no_prefilled_path(seeded_no_config):
    home = seeded_no_config
    a = _backup(home, "20260101T000000Z", {"self_lineage": "anthropic:opus"})
    b = _backup(home, "20260202T000000Z", {"self_lineage": "anthropic:opus"})
    out = _child(home, "out['r'] = attempt(lambda: core._get_db().close())\n")
    assert out["r"]["raised"] == "ConfigMissingError", out
    msg = out["r"]["msg"]
    assert "20260101T000000Z" in msg and "20260202T000000Z" in msg
    assert PHRASES[1] in msg and PHRASES[2] in msg
    assert out["r"]["msg"].index("20260202T000000Z") < out["r"]["msg"].index("20260101T000000Z"), "newest first"
    cp_lines = [ln for ln in msg.splitlines() if "cp -p" in ln]
    assert cp_lines, msg
    for ln in cp_lines:
        assert a.name not in ln and b.name not in ln and "20260101T000000Z" not in ln and "20260202T000000Z" not in ln, ln
        assert "<the backup you chose>" in ln


def test_cell16_candidate_labels(seeded_no_config):
    home = seeded_no_config
    _backup(home, "20260101T000000Z", {"self_lineage": "anthropic:opus"})
    _backup(home, "20260202T000000Z", {"self_lineage": "", "x": 1})
    _backup(home, "20260303T000000Z", "{not json")
    out = _child(home, "out['r'] = attempt(lambda: core._get_db().close())\n")
    assert out["r"]["raised"] == "ConfigMissingError", out
    lines = out["r"]["msg"].splitlines()

    def label_lines(stamp):
        return [ln for ln in lines if stamp in ln and "cp -p" not in ln]

    ok, empty, bad = (label_lines(s) for s in ("20260101T000000Z", "20260202T000000Z", "20260303T000000Z"))
    assert ok and empty and bad, out["r"]["msg"]
    assert not any("(no self_lineage)" in ln or "(unreadable)" in ln for ln in ok)
    assert any("(no self_lineage)" in ln for ln in empty) and not any("(unreadable)" in ln for ln in empty)
    assert any("(unreadable)" in ln for ln in bad) and not any("(no self_lineage)" in ln for ln in bad)


# ----------------------------------------------------------------------------- cell 17

_CELL17 = """
out['r'] = attempt(lambda: core._get_db().close())
out['cfg'] = os.path.exists(cfg)
"""


def _delete_mode(home: Path) -> None:
    """The seeded DB is WAL; a WAL reader is never blocked by a writer, so switch to rollback-journal
    mode, where BEGIN EXCLUSIVE makes a read-only reader fail with SQLITE_BUSY."""
    c = sqlite3.connect(str(home / "knowledge.db"))
    try:
        assert c.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
    finally:
        c.close()


def test_cell17_locked_db_with_config_missing_gets_config_missing_not_damaged(seeded_no_config):
    home = seeded_no_config
    _delete_mode(home)
    # Paired control: lock released (never taken) -> ConfigMissingError.
    free = _child(home, _CELL17)
    assert free["r"]["raised"] == "ConfigMissingError", free
    assert free["cfg"] is False
    # The lock is held by THIS process; first prove it really blocks a read-only reader.
    holder = sqlite3.connect(str(home / "knowledge.db"), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        probe = sqlite3.connect(f"{(home / 'knowledge.db').absolute().as_uri()}?mode=ro", uri=True, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError) as ei:
                probe.execute("SELECT COUNT(*) FROM nodes").fetchone()
        finally:
            probe.close()
        assert "locked" in str(ei.value) or "busy" in str(ei.value), ei.value
        locked = _child(home, _CELL17)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert locked["r"]["raised"] == "ConfigMissingError", locked
    assert "delete" not in locked["r"]["msg"].lower(), locked["r"]["msg"]
    assert locked["cfg"] is False


# ----------------------------------------------------------------------------- cells 18, 18b (R2-3)

def _moved_aside_template_ok(msg: str, home: Path) -> bool:
    return "mv " in msg and ".moved-aside." in msg and str(home / "knowledge.db") in msg


def test_cell18_no_refusal_message_says_delete_or_rm(work, tmp_path):
    msgs = {}
    # C2: seeded, config absent
    h = work / "c2" / ".engram"
    h.mkdir(parents=True)
    _seed(h)
    (h / "config.json").unlink()
    msgs["C2"] = _child(h, _CELL17)["r"]
    # C3a: readable SQLite, nodes table, zero axioms (the seed-missing text + pack sentence)
    h = work / "c3a" / ".engram"
    h.mkdir(parents=True)
    con = sqlite3.connect(str(h / "knowledge.db"))
    con.execute("CREATE TABLE nodes (id TEXT, type TEXT)")
    con.commit()
    con.close()
    msgs["C3a"] = _child(h, _CELL17)["r"]
    # C3b: zero-byte DB
    h = work / "c3b" / ".engram"
    h.mkdir(parents=True)
    (h / "knowledge.db").write_bytes(b"")
    msgs["C3b"] = _child(h, _CELL17)["r"]
    # locked DB (R2-2)
    h = work / "lk" / ".engram"
    h.mkdir(parents=True)
    _seed(h)
    (h / "config.json").unlink()
    _delete_mode(h)
    holder = sqlite3.connect(str(h / "knowledge.db"), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        msgs["locked"] = _child(h, _CELL17)["r"]
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    # C8: install.sh's refusal
    sb = Sandbox(tmp_path)
    sb.make_db(6, config=False)
    r = _sandbox_run(sb)
    assert r.returncode != 0, r.all
    msgs["C8"] = {"raised": "install.sh", "msg": r.all}
    for name, res in msgs.items():
        assert res["raised"], (name, res)
        low = res["msg"].lower()
        assert "delete" not in low, (name, res["msg"])
        assert "rm " not in low, (name, res["msg"])
    # the two moved-aside messages carry the exact mv template
    assert _moved_aside_template_ok(msgs["C3b"]["msg"], work / "c3b" / ".engram"), msgs["C3b"]["msg"]
    assert _moved_aside_template_ok(msgs["C3a"]["msg"], work / "c3a" / ".engram"), msgs["C3a"]["msg"]


def test_cell18b_a_moved_aside_database_satisfies_the_guard_the_message_points_at(home):
    blob = b"this is not a sqlite database file, it is sixty-four bytes of text.\n"[:64].ljust(64, b".")
    (home / "knowledge.db").write_bytes(blob)
    aside = home / "knowledge.db.moved-aside.20261114T000000Z"
    (home / "knowledge.db").rename(aside)  # the `mv` the message gives
    out = _child(home, _DAMAGED)
    assert out["r"]["raised"] == "RuntimeError" and "knowledge.db not found" in out["r"]["msg"], out
    assert DAMAGED_PHRASE not in out["r"]["msg"] and PHRASES[0] not in out["r"]["msg"]
    assert out["cfg"] is False and not (home / "knowledge.db").exists()
    # first-session path (bootstrap mode): proceeds and writes the default
    out = _child(home, _DAMAGED, bootstrap=True)
    assert out["r"]["raised"] is None, out
    assert out["cfg"] is True
    assert aside.read_bytes() == blob, "the moved-aside file changed"
