"""#2652 unit 04 (part of #2652, ledger row 81) -- the scripted installer's step-6 node count reads through
`tools/store_read.py node-count` instead of its own `PY_NODE_COUNT` heredoc.

Why a new subcommand and not `axiom-count`: the installer asks "is this store empty?". A store with nodes but no
axioms reads 0 axioms, and 0 means re-seed, over real data.

Contract (each cell written before the change, red first):

  C1  flag ON, an owner answering: stderr `store_read: path=owner`, stdout the count, exit 0, ZERO direct opens
  C2  flag ON, nothing listening (`down`): `path=direct-ro (owner down)`, the same count as C1 on the same store
  C3  flag OFF: `path=direct-ro (flag off ...)`, the same count
  C4  the owner refuses (any non-`down` failure, `path_too_long` included): exit 3, nothing on stdout, the store is
      never opened; and the INSTALLER stops with a message naming the owner refusal, distinct from the "release any
      lock" message, and never re-seeds (a refusal must never read as 0: 0 means re-seed)
  C5  a store file with no `nodes` table (a zero-byte file, or other tables only), direct path: prints 0. This is
      the ONE read problem that yields 0, kept on purpose: such a file is an empty store, and re-seeding it is
      what the installer has always done
  C6  a path that does not read as a store (garbage bytes, a FIFO, a directory): exit 1, never 0; the installer
      fails with its existing "release any lock / restore from backup" message
  C7  DB absent: the installer does not call store_read at all
  C8  the tool runs under `python3 -I` with no venv on PATH, owner down, and imports nothing from site-packages
  C9  the `store_read: path=...` line reaches the installer's output, so a person reading it can tell the path

Mutation control (run by hand, recorded in the PR): make `node-count` print 0 and exit 0 on an owner refusal.
The C4 cells go red; C1-C3 stay green.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner  # noqa: E402
from test_install_sh_4097 import Sandbox, sb  # noqa: E402,F401  (`sb` is a fixture)
from test_store_read_2652_unit02 import (  # noqa: E402,F401  (`home` is a fixture)
    TEMPLATES,
    TOOL,
    GarbageOwner,
    _in_proc_ping,
    _unavailable,
    build_store,
    home,
    run,
    set_flag,
    stderr_lines,
)

NODE_SQL = "SELECT COUNT(*) FROM nodes"
INSTALL_SH = TOOL.parent / "install.sh"
# build_store's default fixture: 7 rows plus 3 axioms.
ALL_NODES = 10


# ------------------------------------------------------------------------------------------ C1, C2, C3

def test_c1_flag_on_with_an_owner_counts_through_the_owner_with_zero_direct_opens(home, capsys):
    build_store(home)
    set_flag(home)
    with FakeOwner(home) as owner:
        rc, out, err, opens = run(home, ["node-count"], capsys)
        reads = [r for r in owner.requests if r.get("method") == "read"]
    assert rc == 0, err
    assert out.strip() == str(ALL_NODES)
    assert stderr_lines(err) == ["store_read: path=owner"], err
    assert [r["params"]["sql"] for r in reads] == [NODE_SQL]
    assert opens == [], "flag ON and a live owner: no direct open of the store"


def test_c2_flag_on_owner_down_counts_direct_ro_and_names_the_reason(home, capsys):
    build_store(home)
    set_flag(home)  # nothing is listening: the ping is Unavailable, reason `down`
    rc, out, err, opens = run(home, ["node-count"], capsys)
    assert rc == 0, err
    assert out.strip() == str(ALL_NODES)
    lines = stderr_lines(err)
    assert len(lines) == 1 and lines[0] == "store_read: path=direct-ro (owner down)", err
    assert len(opens) == 1 and "mode=ro" in opens[0], opens


def test_c3_flag_off_counts_direct_ro(home, capsys):
    build_store(home)
    rc, out, err, opens = run(home, ["node-count"], capsys)
    assert rc == 0, err
    assert out.strip() == str(ALL_NODES)
    lines = stderr_lines(err)
    assert len(lines) == 1 and lines[0].startswith("store_read: path=direct-ro (flag off"), err
    assert len(opens) == 1 and "mode=ro" in opens[0], opens


def test_node_count_is_not_the_axiom_count(home, capsys):
    """The reason for the subcommand: nodes but no axioms must not read as empty."""
    build_store(home, axioms=0)
    rc, out, err, _ = run(home, ["node-count"], capsys)
    assert rc == 0 and out.strip() == str(ALL_NODES - 3), err


# ------------------------------------------------------------------------------------------ C4 (tool)

@pytest.mark.parametrize("reason", ["path_too_long", "timeout", "busy", "busy_or_stuck", "protocol", "local", "internal"])
def test_c4_every_unavailable_reason_but_down_is_refused_never_zero(home, capsys, monkeypatch, reason):
    build_store(home, rows=[], axioms=0)  # an EMPTY store: a refusal read as 0 would look exactly right
    set_flag(home)
    _in_proc_ping(monkeypatch, _unavailable(reason))
    rc, out, err, opens = run(home, ["node-count"], capsys)
    assert rc == 3 and out == "", (rc, out)
    lines = stderr_lines(err)
    assert len(lines) == 1 and lines[0].startswith("store_read: refused (") and reason in lines[0], err
    assert opens == []


def test_c4_a_rejected_ping_is_refused(home, capsys, monkeypatch):
    build_store(home, rows=[], axioms=0)
    set_flag(home)
    _in_proc_ping(monkeypatch, lambda c: c.Rejected(reason="contract_mismatch", detail="old", remedy="restart it"))
    rc, out, err, opens = run(home, ["node-count"], capsys)
    assert rc == 3 and out == "" and opens == []


def test_c4_a_malformed_owner_answer_is_refused(home, capsys):
    build_store(home, rows=[], axioms=0)
    set_flag(home)
    with GarbageOwner(home):
        rc, out, err, opens = run(home, ["node-count"], capsys)
    assert rc == 3 and out == "" and opens == []


@pytest.mark.parametrize("answer", [[(True,)], [("3",)], [(3, 4)], [], [(3,), (4,)], [(None,)]])
def test_c4_the_node_reader_refuses_any_answer_that_is_not_one_integer(answer):
    from test_store_read_2652_unit02 import load_tool
    mod = load_tool()

    class Con:
        def execute(self, sql):
            assert sql == NODE_SQL
            return self

        def fetchall(self):
            return answer

    with pytest.raises(ValueError):
        mod.read_node_count(Con())


# ------------------------------------------------------------------------------------------ C5, C6 (tool)

def test_c5_a_zero_byte_store_counts_zero(home, capsys):
    (home / "knowledge.db").write_bytes(b"")
    rc, out, err, _ = run(home, ["node-count"], capsys)
    assert rc == 0 and out.strip() == "0", (rc, out, err)


def test_c5_a_store_with_other_tables_but_no_nodes_counts_zero(home, capsys):
    con = sqlite3.connect(str(home / "knowledge.db"))
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    rc, out, err, _ = run(home, ["node-count"], capsys)
    assert rc == 0 and out.strip() == "0", (rc, out, err)


def test_c5_the_no_table_case_is_the_only_operational_error_read_as_zero(home, capsys):
    """A store whose `nodes` is not a table we can count (a view over a missing table) is an error, not 0."""
    con = sqlite3.connect(str(home / "knowledge.db"))
    con.execute("CREATE TABLE gone (x)")
    con.execute("CREATE VIEW nodes AS SELECT x FROM gone")
    con.execute("DROP TABLE gone")
    con.commit()
    con.close()
    rc, out, err, _ = run(home, ["node-count"], capsys)
    assert rc == 1 and out == "", (rc, out, err)


def test_c6_garbage_bytes_exit_1_never_zero(home, capsys):
    (home / "knowledge.db").write_bytes(b"this is not a sqlite database at all" * 200)
    rc, out, err, _ = run(home, ["node-count"], capsys)
    assert rc == 1 and out == "", (rc, out, err)


def test_c6_a_directory_exits_1(home, capsys):
    (home / "knowledge.db").mkdir()
    rc, out, err, opens = run(home, ["node-count"], capsys)
    assert rc == 1 and out == "" and "StoreNotAStore" in err and opens == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_c6_a_fifo_exits_1_without_opening_it(home, capsys):
    os.mkfifo(home / "knowledge.db")
    rc, out, err, opens = run(home, ["node-count"], capsys)
    assert rc == 1 and out == "" and "StoreNotAStore" in err and opens == []


# ------------------------------------------------------------------------------------------ C8

_C8_DRIVER = r"""
import runpy, sys
before = set(sys.modules)  # site.py may already hold a .pth-loaded module; only the tool's imports count
sys.argv = [sys.argv[1], "node-count"]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
    rc = 0
except SystemExit as exc:
    rc = exc.code
bad = sorted(n for n, m in list(sys.modules.items())
             if n not in before and "site-packages" in (getattr(m, "__file__", None) or ""))
sys.stderr.write("C8 rc=%s site-packages=%s\n" % (rc, ",".join(bad)))
"""


def test_c8_runs_isolated_with_no_venv_and_imports_nothing_from_site_packages():
    d = Path(tempfile.mkdtemp(prefix="sr4-", dir="/tmp")).resolve()
    try:
        build_store(d)
        set_flag(d)  # flag ON, nothing listening: the owner-down path, which imports the runtime client
        env = {"PATH": "/usr/bin:/bin", "HOME": str(d), "ENGRAM_HOME": str(d)}
        r = subprocess.run([sys.executable, "-I", "-c", _C8_DRIVER, str(TOOL)], env=env,
                           capture_output=True, text=True, timeout=60)
        assert "C8 rc=0 site-packages=\n" in r.stderr, r.stderr
        assert "store_read: path=direct-ro (owner down)" in r.stderr, r.stderr
        assert r.stdout.strip() == str(ALL_NODES), r.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------------------------------ installer cells

def _install(sb, *args, **kw):
    r = sb.run("--yes", *args, **kw)
    return r, sb.calls_starting("bootstrap ")


def test_c4_installer_stops_on_an_owner_refusal_and_never_reseeds(tmp_path):
    # A sandbox deep enough that the owner socket path is over the AF_UNIX limit on every platform: the
    # client's own `path_too_long`, a real refusal with nothing faked.
    sb = Sandbox(tmp_path / ("p" * 120))
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text(json.dumps({"engramd": {"store_read_reads_via_owner": True}}))
    sb.make_db(0)  # an EMPTY store: reading the refusal as 0 would re-seed it, the dangerous direction
    r, boot = _install(sb)
    assert r.returncode != 0, r.all
    assert "FAIL: [step 6]" in r.stderr, r.all
    fail_line = [l for l in r.stderr.splitlines() if l.startswith("FAIL: [step 6]")][0]
    assert "owner" in fail_line and "refused" in fail_line, fail_line
    assert "release any lock" not in fail_line, fail_line
    assert "store_read: refused (" in r.stderr and "path_too_long" in r.stderr, r.all
    assert boot == [], "a refused count must never lead to a bootstrap run"


def test_c6_installer_fails_with_the_lock_message_on_a_store_that_does_not_read(sb):
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text("{}\n")
    (sb.engram / "knowledge.db").write_bytes(b"this is not a sqlite database at all" * 200)
    r, boot = _install(sb)
    assert r.returncode != 0
    fail_line = [l for l in r.stderr.splitlines() if l.startswith("FAIL: [step 6]")][0]
    assert "release any lock" in fail_line, fail_line
    assert boot == []


def test_c5_installer_reseeds_a_store_with_no_nodes_table(sb):
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text("{}\n")
    (sb.engram / "knowledge.db").write_bytes(b"")
    r, boot = _install(sb)
    assert r.returncode == 0, r.all
    assert len(boot) == 1 and "FORCE_RESEED_EMPTY=1" in boot[0], boot


def test_c7_db_absent_never_calls_store_read(sb):
    # A recording stand-in: if the installer called it, the call log would say so.
    (sb.tools_src / "store_read.py").write_text(
        "import os, sys\nopen(os.environ['CALLS_LOG'], 'a').write('store_read ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(1)\n")
    r, boot = _install(sb)
    assert r.returncode == 0, r.all
    assert sb.calls_starting("store_read") == []
    assert len(boot) == 1


@pytest.mark.parametrize("printed", ["", "ERR not a number"])
def test_installer_refuses_an_answer_that_is_not_a_count(sb, printed):
    """Exit 0 with no count is not 'non-empty': the installer fails instead of skipping or re-seeding."""
    (sb.tools_src / "store_read.py").write_text("import sys\nprint(%r)\nsys.exit(0)\n" % printed)
    sb.make_db(0)
    r, boot = _install(sb)
    assert r.returncode != 0, r.all
    fail_line = [l for l in r.stderr.splitlines() if l.startswith("FAIL: [step 6]")][0]
    assert "no node count" in fail_line, fail_line
    assert boot == []


def test_c9_the_path_line_reaches_the_installer_output(sb):
    sb.make_db(4)
    r, boot = _install(sb)
    assert r.returncode == 0, r.all
    assert "store_read: path=direct-ro (flag off or not this home's store)" in r.stderr, r.all
    assert boot == [] and "4 nodes" in r.stdout, r.all


def test_c9_installer_calls_the_tool_and_holds_no_connect_of_its_own():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "PY_NODE_COUNT" not in text
    assert "sqlite3.connect(" not in text
    assert '"$TOOLS_DIR/store_read.py" node-count --db "$DB_PATH"' in text


def test_the_node_count_select_is_a_registered_template_sited_at_the_tool():
    templates = json.loads(TEMPLATES.read_text())["templates"]
    by_sql = {t["sql"]: t for t in templates}
    assert NODE_SQL in by_sql
    assert "tools/store_read.py:read_node_count" in by_sql[NODE_SQL]["sites"], by_sql[NODE_SQL]
