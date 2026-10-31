"""Contract tests for tools/engramd_batch_a_acceptance.py, the Batch A acceptance script
(#2652: the flag-batch plan, Batch A = the read-side tool flags).

Written BEFORE the script (contract-first). The contract, from the design seat's ruling on #2652:

  C1  Never the live store. The script works on a backup-API copy under a scratch ENGRAM_HOME and
      REFUSES (non-zero exit, named reason) when a store it would open resolves to the seat's live one,
      before any tool runs.
  C2  Per tool: the OFF and ON outputs are equal (byte-identical, or a NAMED normalisation) AND the ON
      run makes 0 direct opens of the store copy, counted inside the child process.
  C3  Positive verdicts only: PASS / FAIL / SKIPPED-with-reason. A flag absent from the tree under test
      is SKIPPED, never PASS. A crash is FAIL with its stderr tail. Exit 0 only if every non-skipped
      tool is PASS.
  C4  One report: seat, SQLite build, deployed tag, the flag list read from the live seat config, and one
      line per tool (verdict, equality basis, open count).

The five cells the ruling names are 1-5 below; 6-8 are the three the build spec adds; 9 and up pin the
design decisions (fresh copy per run, an instrument that can say no, a named normalisation, the live store
left untouched). Every test docstring names the plausible-wrong script it must turn red under.

Fake tools are tiny Python/bash files in a fake tree, listed in a test-only `--table` JSON. They read their
flag the way the real tools do: `engramd.<key>` is the JSON boolean true in $ENGRAM_HOME/config.json.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# BATCH_A_ACCEPTANCE_PATH points the suite at a mutated copy of the script (the red-run evidence).
TOOL_PATH = Path(os.environ.get("BATCH_A_ACCEPTANCE_PATH") or REPO_ROOT / "tools" / "engramd_batch_a_acceptance.py")

PRELUDE = '''
import json, os, sqlite3, sys
HOME = os.environ["ENGRAM_HOME"]
DB = os.path.join(HOME, "knowledge.db")

def flag_on(key):
    try:
        with open(os.path.join(HOME, "config.json")) as f:
            return json.load(f).get("engramd", {}).get(key) is True
    except Exception:
        return False

def read_nodes():
    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    n = c.execute("select count(*) from nodes").fetchone()[0]
    c.close()
    return n
'''


def _load():
    spec = importlib.util.spec_from_file_location("engramd_batch_a_acceptance", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _load()


@pytest.fixture
def live(tmp_path):
    """A stand-in for the seat's live home: a real SQLite store, a config with one flag ON and one OFF,
    and a deployed-version marker."""
    home = tmp_path / "seat-home"
    home.mkdir()
    db = sqlite3.connect(str(home / "knowledge.db"))
    db.execute("create table nodes (id text primary key, type text, is_current integer, claim text, metadata text)")
    db.executemany("insert into nodes values (?,?,?,?,?)", [
        ("n_000001", "observation_factual", 1, "first claim", None),
        ("p_000001", "person", 1, "a person", json.dumps({"name": "Person One"})),
    ])
    db.commit()
    db.close()
    (home / "config.json").write_text(json.dumps(
        {"engramd": {"fake_good_reads_via_owner": True, "fake_off_reads_via_owner": False}}))
    (home / ".deployed-version").write_text(
        "alpha_sha=abc12345\nalpha_tag=deploy/2026-01-01-v9.9.9\ndeployed_from=/Users/seat/GitHub/engram-alpha\n")
    return home


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    (root / "tools").mkdir(parents=True)
    return root


def write_tool(tree: Path, name: str, body: str, prelude: bool = True) -> str:
    path = tree / "tools" / name
    path.write_text((PRELUDE if prelude else "") + body)
    return f"tools/{name}"


def entry(flag: str, rel: str, **kw) -> dict:
    d = {"flag": flag, "tool": [rel], "argv": [], "needs_owner": False}
    d.update(kw)
    return d


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Run:
    """One invocation of the script's main() with its outputs gathered."""

    def __init__(self, script, tmp_path, live, tree, table, extra=()):
        self.tmp = tmp_path
        table_path = tmp_path / "table.json"
        table_path.write_text(json.dumps(table))
        self.report_path = tmp_path / "report.json"
        self.argv = ["--live-home", str(live), "--tree", str(tree), "--table", str(table_path),
                     "--report", str(self.report_path), "--python", sys.executable,
                     "--scratch-base", str(tmp_path / "scratch")] + list(extra)
        (tmp_path / "scratch").mkdir(exist_ok=True)
        self.script = script
        self.rc = None
        self.out = ""
        self.err = ""

    def go(self, capsys):
        self.rc = self.script.main(self.argv)
        cap = capsys.readouterr()
        self.out, self.err = cap.out, cap.err
        return self

    @property
    def report(self) -> dict:
        return json.loads(self.report_path.read_text())

    def tool(self, flag: str) -> dict:
        (hit,) = [t for t in self.report["tools"] if t["flag"] == flag]
        return hit


def run(script, tmp_path, live, tree, table, capsys, extra=()):
    return Run(script, tmp_path, live, tree, table, extra).go(capsys)


GOOD = PRELUDE + '''
KEY = "fake_good_reads_via_owner"
if flag_on(KEY):
    print("nodes=2")
else:
    print("nodes=%d" % read_nodes())
'''


# --- cell 1 ------------------------------------------------------------------------------------

def test_cell1_on_output_differs_is_fail(script, tmp_path, live, tree, capsys):
    """A tool whose ON output differs from its OFF output is FAIL. Red under a script that compares only
    exit codes (both runs exit 0)."""
    rel = write_tool(tree, "differs.py", '''
KEY = "fake_differs_reads_via_owner"
if flag_on(KEY):
    print("nodes=3")
else:
    print("nodes=%d" % read_nodes())
''')
    r = run(script, tmp_path, live, tree, [entry("fake_differs_reads_via_owner", rel)], capsys)
    t = r.tool("fake_differs_reads_via_owner")
    assert t["verdict"] == "FAIL"
    assert "differ" in t["reason"]
    assert t["on_opens"] == 0, "the output differed, not the opens: the failure must name the right cause"
    assert r.rc != 0


# --- cell 2 ------------------------------------------------------------------------------------

def test_cell2_on_path_opening_the_store_is_fail(script, tmp_path, live, tree, capsys):
    """A tool whose ON path opens the store copy directly is FAIL with an open count of 1 or more, even
    though both outputs are identical. Red under a script that counts opens in the PARENT process (a tool
    runs in a child, so the parent never sees the open)."""
    rel = write_tool(tree, "direct.py", '''
KEY = "fake_direct_reads_via_owner"
flag_on(KEY)
print("nodes=%d" % read_nodes())
''')
    r = run(script, tmp_path, live, tree, [entry("fake_direct_reads_via_owner", rel)], capsys)
    t = r.tool("fake_direct_reads_via_owner")
    assert t["verdict"] == "FAIL"
    assert t["on_opens"] >= 1
    assert t["off_opens"] >= 1
    assert "open" in t["reason"]
    assert r.rc != 0


# --- cell 3 ------------------------------------------------------------------------------------

def test_cell3_unshipped_flag_is_skipped_not_pass(script, tmp_path, live, tree, capsys):
    """A flag whose tool file is missing, or whose file does not carry the flag key, is SKIPPED with a
    reason, never PASS, and the tool never runs. Red under a script that treats a missing file as PASS."""
    marker = tmp_path / "ran.marker"
    write_tool(tree, "nokey.py", f'open({str(marker)!r}, "w").close()\nprint("x")\n')
    table = [
        entry("fake_missing_reads_via_owner", "tools/not-there.py"),
        entry("fake_nokey_reads_via_owner", "tools/nokey.py"),
        entry("fake_good_reads_via_owner", write_tool(tree, "good.py", GOOD[len(PRELUDE):])),
    ]
    r = run(script, tmp_path, live, tree, table, capsys)
    missing = r.tool("fake_missing_reads_via_owner")
    nokey = r.tool("fake_nokey_reads_via_owner")
    assert missing["verdict"] == "SKIPPED" and "not found" in missing["reason"]
    assert nokey["verdict"] == "SKIPPED" and "fake_nokey_reads_via_owner" in nokey["reason"]
    assert not marker.exists(), "a SKIPPED tool must not run"
    assert r.tool("fake_good_reads_via_owner")["verdict"] == "PASS"
    assert r.rc == 0, "skipped tools do not fail the run when every other tool passes"


# --- cell 4 ------------------------------------------------------------------------------------

def test_cell4a_scratch_root_overlapping_the_live_home_is_refused_before_any_tool(script, tmp_path, live, tree, capsys):
    """A scratch root that resolves to (or inside, or around) the live home is refused: non-zero exit, a
    named reason, and a marker-writing fake tool never ran. Red under a script that checks the live path
    only after it has run the first tool."""
    marker = tmp_path / "ran.marker"
    rel = write_tool(tree, "marker.py", f'''
open({str(marker)!r}, "w").close()
flag_on("fake_marker_reads_via_owner")
print(read_nodes())
''')
    r = Run(script, tmp_path, live, tree, [entry("fake_marker_reads_via_owner", rel)])
    # point the scratch base AT the live home: the scratch root would be created inside it
    r.argv[r.argv.index("--scratch-base") + 1] = str(live)
    before = sorted(p.name for p in live.iterdir())
    r.go(capsys)
    assert r.rc == 2
    assert "refus" in r.err.lower() and "live" in r.err.lower()
    assert not marker.exists(), "the refusal must come BEFORE any tool runs"
    assert sorted(p.name for p in live.iterdir()) == before, "nothing may be created in the live home"


def test_cell4b_a_tool_argv_naming_the_live_store_is_refused_before_the_first_tool(script, tmp_path, live, tree, capsys):
    """The refusal is a pre-flight over EVERY tool, not a check made as each tool comes up: the first tool
    here is innocent and must not run because the SECOND one names the live store. Red under a script that
    checks each tool just before it runs."""
    marker = tmp_path / "ran.marker"
    first = write_tool(tree, "first.py", f'open({str(marker)!r}, "w").close()\nflag_on("fake_first_reads_via_owner")\nprint(read_nodes())\n')
    second = write_tool(tree, "second.py", 'flag_on("fake_second_reads_via_owner")\nprint(1)\n')
    table = [
        entry("fake_first_reads_via_owner", first),
        entry("fake_second_reads_via_owner", second, argv=["--db", "${live_db}"]),
    ]
    r = run(script, tmp_path, live, tree, table, capsys)
    assert r.rc == 2
    assert "refus" in r.err.lower() and "fake_second_reads_via_owner" in r.err
    assert not marker.exists()


def test_cell4c_the_live_store_is_left_exactly_as_it_was(script, tmp_path, live, tree, capsys):
    """After a full run the live store's bytes and the live home's file list are unchanged (the snapshot is
    read through the backup API, read side only)."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    before_sha = sha(live / "knowledge.db")
    before_files = sorted(p.name for p in live.iterdir())
    run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)], capsys)
    assert sha(live / "knowledge.db") == before_sha
    assert sorted(p.name for p in live.iterdir()) == before_files


# --- cell 5 ------------------------------------------------------------------------------------

def test_cell5_crashing_tool_is_fail_with_stderr_tail(script, tmp_path, live, tree, capsys):
    """A tool that crashes is FAIL carrying the tail of its stderr, and the run exits non-zero. Red under a
    script that swallows a crash as SKIPPED (or as an empty-output PASS)."""
    rel = write_tool(tree, "crash.py", '''
KEY = "fake_crash_reads_via_owner"
if flag_on(KEY):
    raise RuntimeError("boom-from-the-on-run")
print("nodes=%d" % read_nodes())
''')
    r = run(script, tmp_path, live, tree, [entry("fake_crash_reads_via_owner", rel)], capsys)
    t = r.tool("fake_crash_reads_via_owner")
    assert t["verdict"] == "FAIL"
    assert "boom-from-the-on-run" in t["stderr_tail"]
    assert t["exit_on"] != 0 and t["exit_off"] == 0
    assert r.rc != 0


def test_cell5b_a_tool_that_crashes_in_the_off_run_is_fail_not_a_vacuous_equal(script, tmp_path, live, tree, capsys):
    """Both runs crashing the same way would 'agree'; the OFF run is the baseline and a baseline that does
    not run is FAIL, not PASS."""
    rel = write_tool(tree, "crashboth.py", 'raise RuntimeError("boom-everywhere")\n# fake_both_reads_via_owner\n')
    r = run(script, tmp_path, live, tree, [entry("fake_both_reads_via_owner", rel)], capsys)
    t = r.tool("fake_both_reads_via_owner")
    assert t["verdict"] == "FAIL" and "boom-everywhere" in t["stderr_tail"]
    assert r.rc != 0


# --- cell 6 ------------------------------------------------------------------------------------

def test_cell6_all_pass_set_exits_zero(script, tmp_path, live, tree, capsys):
    """Two tools that satisfy C2 give PASS twice and exit 0, and the OFF run read the COPY (its output
    names the live store's two rows). Red under a script that exits non-zero on any SKIPPED/PASS mix or
    never reads the copy."""
    a = write_tool(tree, "a.py", GOOD[len(PRELUDE):])
    b = write_tool(tree, "b.py", GOOD[len(PRELUDE):].replace("fake_good", "fake_good2"))
    r = run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", a),
                                          entry("fake_good2_reads_via_owner", b)], capsys)
    assert [t["verdict"] for t in r.report["tools"]] == ["PASS", "PASS"]
    assert r.rc == 0
    for t in r.report["tools"]:
        assert t["on_opens"] == 0 and t["off_opens"] >= 1


# --- cell 7 ------------------------------------------------------------------------------------

def test_cell7_off_run_write_is_invisible_to_the_on_run(script, tmp_path, live, tree, capsys):
    """The OFF run of a writing tool must not change the ON run's input: each run gets a FRESH copy of the
    one snapshot. The fake leaves a marker next to the store on its OFF run and reports whether it found one
    when it started. Red under a script that reuses one scratch home for both runs (the ON run finds the
    marker and its output differs)."""
    rel = write_tool(tree, "writer.py", '''
KEY = "fake_writer_reads_via_owner"
marker = os.path.join(HOME, "off-ran.marker")
seen = os.path.exists(marker)
if flag_on(KEY):
    print("marker=%s" % seen)
else:
    n = read_nodes()
    open(marker, "w").close()
    print("marker=%s" % seen)
''')
    r = run(script, tmp_path, live, tree, [entry("fake_writer_reads_via_owner", rel)], capsys)
    t = r.tool("fake_writer_reads_via_owner")
    assert t["verdict"] == "PASS", t


# --- cell 8 ------------------------------------------------------------------------------------

def test_cell8_report_carries_the_c4_fields(script, tmp_path, live, tree, capsys):
    """The report names the seat (hostname), the SQLite build, the deployed tag from the live seat's
    .deployed-version, the ON flags read from the live seat config, and one line per tool with verdict,
    equality basis and open count, on stdout and in the JSON. Red under a script that omits any of them."""
    import platform
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)], capsys)
    rep = r.report
    assert rep["seat"] == platform.node() or rep["seat"]
    assert rep["sqlite_version"] == sqlite3.sqlite_version
    assert rep["deployed_tag"] == "deploy/2026-01-01-v9.9.9"
    assert rep["flags_on"] == ["fake_good_reads_via_owner"], "ON flags only, from the LIVE seat config"
    (t,) = rep["tools"]
    for key in ("verdict", "basis", "on_opens", "off_opens", "reason"):
        assert key in t
    assert "PASS" in r.out and "fake_good_reads_via_owner" in r.out
    assert sqlite3.sqlite_version in r.out and "deploy/2026-01-01-v9.9.9" in r.out
    line = [ln for ln in r.out.splitlines() if "fake_good_reads_via_owner" in ln and "PASS" in ln][0]
    assert "opens" in line and "stdout" in line, "one line per tool: verdict, equality basis, open count"


def test_cell8b_deployed_tag_is_unknown_when_the_marker_is_absent(script, tmp_path, live, tree, capsys):
    """No .deployed-version means the tag is 'unknown', not an empty string or a crash."""
    (live / ".deployed-version").unlink()
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)], capsys)
    assert r.report["deployed_tag"] == "unknown"


# --- the design decisions ----------------------------------------------------------------------

def test_cell9_a_tool_whose_opens_the_instrument_cannot_see_is_fail_never_pass(script, tmp_path, live, tree, capsys):
    """A non-Python tool that starts no Python process cannot be counted: the open counter never arms, and
    the verdict is FAIL 'opens not countable by the instrument', never PASS on a count of zero. Red under a
    script that reads an unarmed counter as zero opens."""
    p = tree / "tools" / "shell-only.sh"
    p.write_text('#!/bin/bash\n# fake_shell_reads_via_owner\necho "nodes=2"\n')
    r = run(script, tmp_path, live, tree, [entry("fake_shell_reads_via_owner", "tools/shell-only.sh", runner="bash")], capsys)
    t = r.tool("fake_shell_reads_via_owner")
    assert t["verdict"] == "FAIL"
    assert "not countable" in t["reason"]
    assert r.rc != 0


def test_cell9b_a_shell_tool_calling_the_sqlite_cli_is_fail_before_it_runs(script, tmp_path, live, tree, capsys):
    """A shell tool that reads the store with the sqlite3 command-line binary is invisible to the audit
    hook even if it also starts Python elsewhere: FAIL 'not countable', and it does not run."""
    marker = tmp_path / "ran.marker"
    p = tree / "tools" / "cli.sh"
    p.write_text(f'#!/bin/bash\n# fake_cli_reads_via_owner\ntouch {marker}\nsqlite3 "$ENGRAM_HOME/knowledge.db" "select count(*) from nodes"\n')
    r = run(script, tmp_path, live, tree, [entry("fake_cli_reads_via_owner", "tools/cli.sh", runner="bash")], capsys)
    t = r.tool("fake_cli_reads_via_owner")
    assert t["verdict"] == "FAIL" and "not countable" in t["reason"] and "sqlite3" in t["reason"]
    assert not marker.exists()


def test_cell10_a_tool_the_instrument_never_saw_open_the_store_off_is_control_silent(script, tmp_path, live, tree, capsys):
    """The OFF run is the instrument's positive control. A tool with equal output that opens nothing in
    EITHER run cannot show the counter would have seen an open: the verdict is CONTROL-SILENT (look at it by
    hand), not PASS and not FAIL, and the exit code is non-zero and distinct from FAIL's (1) and from the
    harness failure code (4). Red under a script that passes any tool with ON opens == 0 (PASS), and under
    one that still calls it FAIL."""
    rel = write_tool(tree, "silent.py", 'flag_on("fake_silent_reads_via_owner")\nprint("nodes=2")\n')
    r = run(script, tmp_path, live, tree, [entry("fake_silent_reads_via_owner", rel)], capsys)
    t = r.tool("fake_silent_reads_via_owner")
    assert t["verdict"] == "CONTROL-SILENT" and t["off_opens"] == 0 and t["on_opens"] == 0
    assert "OFF run made no direct open" in t["reason"] and "by hand" in t["reason"]
    assert r.rc == 5 and r.report["exit"] == 5
    assert r.report["summary"] == {"PASS": 0, "FAIL": 0, "CONTROL-SILENT": 1, "SKIPPED": 0}
    assert "0 PASS, 0 FAIL, 1 CONTROL-SILENT, 0 SKIPPED" in r.out


def test_cell10b_control_silent_beside_a_pass_is_still_not_a_clean_exit(script, tmp_path, live, tree, capsys):
    """One PASS and one CONTROL-SILENT: exit is the CONTROL-SILENT code, never 0. Red under a script that
    exits 0 once any tool PASSed."""
    good = write_tool(tree, "a.py", GOOD[len(PRELUDE):])
    rel = write_tool(tree, "silent.py", 'flag_on("fake_silent_reads_via_owner")\nprint("nodes=2")\n')
    r = run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", good),
                                          entry("fake_silent_reads_via_owner", rel)], capsys)
    assert [t["verdict"] for t in r.report["tools"]] == ["PASS", "CONTROL-SILENT"]
    assert r.rc == 5


def test_cell10c_a_fail_beside_a_control_silent_exits_as_fail(script, tmp_path, live, tree, capsys):
    """FAIL outranks CONTROL-SILENT in the exit code. Red under a script that lets the CONTROL-SILENT code
    win."""
    bad = write_tool(tree, "bad.py", 'KEY = "fake_bad_reads_via_owner"\nflag_on(KEY)\nprint("nodes=%d" % read_nodes())\n')
    rel = write_tool(tree, "silent.py", 'flag_on("fake_silent_reads_via_owner")\nprint("nodes=2")\n')
    r = run(script, tmp_path, live, tree, [entry("fake_silent_reads_via_owner", rel),
                                          entry("fake_bad_reads_via_owner", bad)], capsys)
    assert {t["flag"]: t["verdict"] for t in r.report["tools"]} == {
        "fake_silent_reads_via_owner": "CONTROL-SILENT", "fake_bad_reads_via_owner": "FAIL"}
    assert r.rc == 1


def test_cell10d_silent_off_with_unequal_output_is_fail_not_control_silent(script, tmp_path, live, tree, capsys):
    """Precedence: a silent OFF run does not soften a real failure. Output differs (and neither run opens
    the store) -> FAIL naming the difference. Red under a script that returns CONTROL-SILENT whenever the
    OFF run opened nothing, before comparing outputs."""
    rel = write_tool(tree, "silent_differs.py", '''
if flag_on("fake_sd_reads_via_owner"):
    print("nodes=3")
else:
    print("nodes=2")
''')
    r = run(script, tmp_path, live, tree, [entry("fake_sd_reads_via_owner", rel)], capsys)
    t = r.tool("fake_sd_reads_via_owner")
    assert t["verdict"] == "FAIL" and "differ" in t["reason"] and t["off_opens"] == 0 and t["on_opens"] == 0
    assert r.rc == 1


def test_cell10e_silent_off_with_on_opens_is_fail_not_control_silent(script, tmp_path, live, tree, capsys):
    """Precedence: the ON run opens the store directly while the OFF run does not (equal output). FAIL
    naming the ON opens. Red under a script that returns CONTROL-SILENT whenever the OFF run opened nothing,
    before looking at the ON count."""
    rel = write_tool(tree, "on_opens.py", '''
if flag_on("fake_oo_reads_via_owner"):
    read_nodes()
print("nodes=2")
''')
    r = run(script, tmp_path, live, tree, [entry("fake_oo_reads_via_owner", rel)], capsys)
    t = r.tool("fake_oo_reads_via_owner")
    assert t["verdict"] == "FAIL" and t["off_opens"] == 0 and t["on_opens"] >= 1
    assert "ON run opened" in t["reason"]
    assert r.rc == 1


def test_cell10f_off_opens_required_false_disables_the_control(script, tmp_path, live, tree, capsys):
    """`off_opens_required: false` turns the control off entirely: the same silent tool then PASSes and
    exits 0. Red under a script that ignores the per-entry switch."""
    rel = write_tool(tree, "silent.py", 'flag_on("fake_silent_reads_via_owner")\nprint("nodes=2")\n')
    r = run(script, tmp_path, live, tree, [entry("fake_silent_reads_via_owner", rel, off_opens_required=False)],
            capsys)
    assert r.tool("fake_silent_reads_via_owner")["verdict"] == "PASS"
    assert r.rc == 0


def test_cell10g_control_silent_with_an_unarmed_counter_is_fail(script, tmp_path, live, tree, capsys):
    """Precedence: CONTROL-SILENT needs the counter to have armed in the child. A tool whose child never
    armed it stays FAIL 'not countable'. Red under a script that checks silence before arming."""
    (tree / "tools" / "unarmed.sh").write_text('#!/bin/bash\n# fake_unarmed_reads_via_owner\necho "nodes=2"\n')
    r = run(script, tmp_path, live, tree, [entry("fake_unarmed_reads_via_owner", "tools/unarmed.sh", runner="bash")],
            capsys)
    t = r.tool("fake_unarmed_reads_via_owner")
    assert t["verdict"] == "FAIL" and "not countable" in t["reason"]


def test_cell11_every_tool_skipped_is_not_a_clean_exit(script, tmp_path, live, tree, capsys):
    """A run that verified nothing (a wrong --tree makes every tool SKIPPED) must not exit 0: that would
    read as 'Batch A passed'. Red under a script that exits 0 whenever nothing FAILed."""
    r = run(script, tmp_path, live, tree, [entry("fake_missing_reads_via_owner", "tools/not-there.py")], capsys)
    assert r.tool("fake_missing_reads_via_owner")["verdict"] == "SKIPPED"
    assert r.rc == 3


def test_cell12_normalisation_is_applied_and_named(script, tmp_path, live, tree, capsys):
    """Outputs that differ only where a NAMED normalisation applies compare equal, and the report names the
    normalisation and why. The same tool without it is FAIL (so the normalisation is what made it equal)."""
    rel = write_tool(tree, "clock.py", '''
KEY = "fake_clock_reads_via_owner"
if flag_on(KEY):
    print("nodes=2 at 11:22:33")
else:
    print("nodes=%d at 01:02:03" % read_nodes())
''')
    plain = run(script, tmp_path, live, tree, [entry("fake_clock_reads_via_owner", rel)], capsys)
    assert plain.tool("fake_clock_reads_via_owner")["verdict"] == "FAIL"
    norm = {"name": "clock", "pattern": r"\d\d:\d\d:\d\d", "replacement": "<time>", "why": "the tool prints the wall clock"}
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    r = run(script, tmp2, live, tree, [entry("fake_clock_reads_via_owner", rel, normalise=[norm])], capsys)
    t = r.tool("fake_clock_reads_via_owner")
    assert t["verdict"] == "PASS"
    assert t["normalisations"] == [{"name": "clock", "why": "the tool prints the wall clock"}]
    assert "clock" in r.out


def test_cell13_ids_are_picked_from_the_copy_and_the_same_for_off_and_on(script, tmp_path, live, tree, capsys):
    """${node_id} is a current observation read from the snapshot, identical in both runs (a tool is run
    with the same argument OFF and ON)."""
    rel = write_tool(tree, "ident.py", '''
KEY = "fake_ident_reads_via_owner"
if flag_on(KEY):
    print("arg=" + sys.argv[1])
else:
    read_nodes()
    print("arg=" + sys.argv[1])
''')
    r = run(script, tmp_path, live, tree, [entry("fake_ident_reads_via_owner", rel, argv=["${node_id}"])], capsys)
    assert r.tool("fake_ident_reads_via_owner")["verdict"] == "PASS"


def test_cell14_owner_that_cannot_serve_makes_the_on_requiring_tool_fail(script, tmp_path, live, tree, capsys):
    """A tool that needs the scratch owner is FAIL 'scratch owner did not serve' when the tree under test
    has no owner to start, the run exits non-zero, and the tool is not run against a guess."""
    marker = tmp_path / "ran.marker"
    rel = write_tool(tree, "needs.py", f'open({str(marker)!r}, "w").close()\nflag_on("fake_needs_reads_via_owner")\nprint(read_nodes())\n')
    r = run(script, tmp_path, live, tree, [entry("fake_needs_reads_via_owner", rel, needs_owner=True)], capsys)
    t = r.tool("fake_needs_reads_via_owner")
    assert t["verdict"] == "FAIL" and "scratch owner did not serve" in t["reason"]
    assert r.rc != 0
    assert not marker.exists()


def test_cell15_the_scratch_directory_is_removed(script, tmp_path, live, tree, capsys):
    """The scratch root (a full store copy) does not outlive the run."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)], capsys)
    assert list((tmp_path / "scratch").iterdir()) == []


def test_cell16_the_only_flag_limits_the_run(script, tmp_path, live, tree, capsys):
    """--only runs the named flags and nothing else (the rest are not in the report as PASS)."""
    a = write_tool(tree, "a.py", GOOD[len(PRELUDE):])
    b = write_tool(tree, "b.py", GOOD[len(PRELUDE):].replace("fake_good", "fake_good2"))
    r = run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", a), entry("fake_good2_reads_via_owner", b)],
            capsys, extra=["--only", "fake_good2_reads_via_owner"])
    assert [t["flag"] for t in r.report["tools"]] == ["fake_good2_reads_via_owner"]


# --- the shipped table -------------------------------------------------------------------------

def test_cell17_the_shipped_table_names_the_batch_a_flags_and_each_resolves_in_the_dev_tree(script):
    """The shipped table lists the thirteen Batch A flags in fourteen rows (anchoring_report and
    quote_quality_survey share one flag, so they are two rows told apart by label), and every row resolves to
    a tool file in this checkout that carries its flag key, with every named normalisation defined. A table
    that drifted from the tree would SKIP silently (a SKIPPED is not a failure), so this is where drift turns
    red. cell17b is the reverse direction."""
    tree = REPO_ROOT / "src" / "engram"
    flags = [e["flag"] for e in script.TOOLS]
    labels = [script._label(e) for e in script.TOOLS]
    assert len(flags) == 14 and len(set(flags)) == 13 and len(set(labels)) == 14
    for e in script.TOOLS:
        path = script._resolve_tool(tree, e)
        assert path is not None, e["flag"]
        assert e["flag"] in path.read_text(errors="replace"), e["flag"]
        for item in e.get("normalise", []) + e.get("artifact_normalise", []):
            assert item in script.NORMALISERS
        assert e.get("compare_stdout", True) or e.get("stdout_excluded_why"), "an excluded stdout must say why"


# The owner flags the shipped HOOKS carry belong to Batch B (the per-prompt hooks), not to this script's table.
# `user_identity_reads_via_owner` is Batch A per the plan and stays in the table, so it is NOT listed here.
BATCH_B_HOOK_FLAGS = {
    "lesson_tripwire_reads_via_owner",    # engram-lesson-tripwire-hook.py (per prompt)
    "session_start_reads_via_owner",      # engram-session-start-hook.py
    "stop_reads_via_owner",               # engram-stop-hook.py (and start-engramd-owner.sh)
    "surface_reads_via_owner",            # engram-surface-hook.py (per prompt)
    "surface_writes_via_owner",           # engram-surface-hook.py (per prompt)
    "utility_credit_writes_via_owner",    # engram-utility-credit-mention-stop.py
}

# Flags carried by shipped tools that are deliberately NOT in any batch, one entry each with its reason.
# Not BATCH_B_HOOK_FLAGS (that set is the per-prompt hooks). An entry needs a non-empty reason and must
# still be carried by the tree (stale check in cell17b).
NOT_BATCHED_FLAGS = {
    "install_selfcheck_reads_via_owner": (
        "install-time diagnostic, not on any hook path; its ON output differs from OFF by design (it names "
        "the path taken and FAILs on an unhealthy owner); flips as its own one-flag step after #4118 lands"
    ),
}


def _flag_decl():
    """A flag key is DECLARED by an `OWNER_FLAG_KEY = "x"` style assignment (tools, hooks, the shell script),
    a `flag_key="x"` keyword, or a `flag_on("x", ...)` call. A key that only appears in a docstring is not one."""
    import re
    return re.compile(r"""(?:FLAG_KEY\s*=\s*|flag_key\s*=\s*|flag_on\(\s*)["']([a-z0-9_]+_via_owner)["']""")


def _shipped_owner_flags(tree):
    found = {}
    for sub in ("tools", "hooks"):
        for path in sorted((tree / sub).rglob("*")):
            if not path.is_file() or path.suffix in {".pyc", ".json", ".md"} or "__pycache__" in path.parts:
                continue
            for key in _flag_decl().findall(path.read_text(errors="replace")):
                found.setdefault(key, set()).add(str(path.relative_to(tree)))
    return found


def test_cell17b_the_shipped_table_is_the_flags_the_shipped_tools_carry_minus_batch_b(script):
    """The reverse of cell 17: every owner flag the shipped tools and hooks declare is either a table row or
    on the explicit Batch B hook list. A tool that gained a flag with no row would otherwise never be run by
    this script and never turn anything red. Turns red under: a table missing a shipped tool's flag."""
    tree = REPO_ROOT / "src" / "engram"
    carried = _shipped_owner_flags(tree)
    # the scan can say no: a planted declaration of each shape is found by the SAME pattern
    planted = ('OWNER_FLAG_KEY = "planted_a_via_owner"\n_OWNER_WRITE_FLAG_KEY = "planted_b_via_owner"\n'
               'x = flag_on("planted_c_via_owner", p)\nflag_key="planted_d_via_owner",\n'
               '"""docs: engramd.planted_e_via_owner"""\n')
    assert sorted(_flag_decl().findall(planted)) == [f"planted_{c}_via_owner" for c in "abcd"]
    assert BATCH_B_HOOK_FLAGS <= set(carried), "a Batch B exclusion that no hook carries any more is stale"
    assert set(NOT_BATCHED_FLAGS) <= set(carried), "a not-batched entry that no tool carries any more is stale"
    table = {e["flag"] for e in script.TOOLS}
    unbatched = set(carried) - BATCH_B_HOOK_FLAGS - set(NOT_BATCHED_FLAGS)
    assert unbatched == table, (
        f"in the tree but not the table: {sorted(unbatched - table)}; "
        f"in the table but not carried: {sorted(table - unbatched)}")


def test_cell17d_every_not_batched_flag_has_a_reason_and_is_disjoint_from_the_other_sets(script):
    """A not-batched flag is a named exception, so it must say why, and it must not also sit in Batch B or in
    the table (one home per flag). Turns red under: an empty reason, or a flag listed in two places."""
    for flag, reason in NOT_BATCHED_FLAGS.items():
        assert isinstance(reason, str) and reason.strip(), f"{flag} needs a reason"
    table = {e["flag"] for e in script.TOOLS}
    assert not set(NOT_BATCHED_FLAGS) & BATCH_B_HOOK_FLAGS
    assert not set(NOT_BATCHED_FLAGS) & table


def test_cell17c_the_anchoring_report_and_the_survey_are_two_rows_on_one_flag_with_the_survey_pinned(script):
    """The row shape from the follow-up spec: anchoring_report.py and quote_quality_survey.py are compared
    separately. The survey writes files, so its sheet and key go under the scratch work directory, the draw
    uses the tool's own seed in both runs (the argv is one list, used for both), and the key's clock is masked.
    Red under: a single row that runs only one of the two tools, or a survey argv that names a path outside
    ${work}."""
    rows = {script._label(e): e for e in script.TOOLS if e["flag"] == "anchoring_report_reads_via_owner"}
    assert sorted(rows) == ["anchoring_report_reads_via_owner",
                            "anchoring_report_reads_via_owner:quote_quality_survey"]
    assert rows["anchoring_report_reads_via_owner"]["tool"] == ["tools/anchoring_report.py"]
    survey = rows["anchoring_report_reads_via_owner:quote_quality_survey"]
    assert survey["tool"] == ["tools/quote_quality_survey.py"]
    argv = survey["argv"]
    assert argv[argv.index("--seed") + 1] == "4050"
    for opt in ("--judging-sheet", "--judging-key"):
        assert argv[argv.index(opt) + 1].startswith("${work}/"), opt
    assert all(a.startswith("${work}/") for a in survey["artifacts"])
    assert "generated_at_field" in survey["normalise"] and "generated_at_field" in survey["artifact_normalise"]


# --- two rows on one flag, and masked artifacts --------------------------------------------------------

_SHARED = "fake_shared_reads_via_owner"


def _stamped_tool(tree, name, off_n="2", on_n="2"):
    """Writes ${work}/<name>.json (the tool's cwd is the work directory) whose generated_at differs between
    the runs; n is the compared field."""
    return write_tool(tree, name, f'''
import time
on = flag_on("{_SHARED}")
if not on:
    read_nodes()
n = "{on_n}" if on else "{off_n}"
open("{name}.json", "w").write('{{"generated_at": "%s-%f", "n": %s}}' % ("ON" if on else "OFF", time.time(), n))
print("n=" + n)
''')


def _two_rows(tree, second_on_n="2"):
    a = _stamped_tool(tree, "first", on_n="2")
    b = _stamped_tool(tree, "second", on_n=second_on_n)
    mask = ["generated_at_field"]
    return [
        entry(_SHARED, a, artifacts=["${work}/first.json"], artifact_normalise=mask),
        entry(_SHARED, b, label=_SHARED + ":second", artifacts=["${work}/second.json"], artifact_normalise=mask),
    ]


def test_cell32a_two_rows_on_one_flag_are_judged_separately_and_a_fault_in_the_second_alone_is_caught(
        script, tmp_path, live, tree, capsys):
    """The survey rides the report's flag through its own call site, so a survey-only fault must turn ONLY the
    survey's row red. Red under a script that keys rows by flag (one row hides or replaces the other)."""
    table = _two_rows(tree, second_on_n="3")
    r = run(script, tmp_path, live, tree, table, capsys)
    assert r.tool(_SHARED)["verdict"] == "PASS"
    second = r.tool(_SHARED + ":second")
    assert second["verdict"] == "FAIL" and "second.json" in second["reason"]
    assert r.rc == 1


def test_cell32b_a_masked_clock_field_in_an_artifact_is_equal_but_a_real_difference_is_not(
        script, tmp_path, live, tree, capsys):
    """The control for 32a: with both rows faithful, the differing generated_at is masked and both PASS, so
    32a is red because of the n field and not because the mask failed. Red under a script that hashes the
    artifact raw (it would FAIL here) or masks too much (it would PASS 32a)."""
    r = run(script, tmp_path, live, tree, _two_rows(tree), capsys)
    assert [t["verdict"] for t in r.report["tools"]] == ["PASS", "PASS"] and r.rc == 0


def test_cell32c_without_the_artifact_mask_the_clock_field_is_a_fail(script, tmp_path, live, tree, capsys):
    """The mask is what made 32b equal: the same tool unmasked is FAIL."""
    table = _two_rows(tree)
    for e in table:
        e.pop("artifact_normalise")
    r = run(script, tmp_path, live, tree, table, capsys)
    assert [t["verdict"] for t in r.report["tools"]] == ["FAIL", "FAIL"]


def test_cell32d_only_selects_a_row_by_label(script, tmp_path, live, tree, capsys):
    """--only takes the label, so the survey row can be run alone (and the bare flag selects the report row)."""
    table = _two_rows(tree)
    r = run(script, tmp_path, live, tree, table, capsys, extra=["--only", _SHARED + ":second"])
    assert [t["flag"] for t in r.report["tools"]] == [_SHARED + ":second"]


def test_cell18_no_node_ids_or_example_home_in_the_script_or_its_tests():
    """Hygiene the repo's name/leak scans enforce: no ENGRAM node id and no example-user home path."""
    import re
    pattern = re.compile(r"\b(?:ob|dv|ax|cs|ls|gl|pn|qu|cj|df|ev)_\d{4}\b|/Users/" + "exam" + "ple")
    for path in (TOOL_PATH, Path(__file__)):
        assert pattern.findall(path.read_text()) == [], path


# --- round 2: --report guard, missing artifacts, signals, harness exit 4, documented limits -----

def _no_scratch_left(tmp_path) -> bool:
    return list((tmp_path / "scratch").iterdir()) == []


def _marker_tool(tree, marker, flag):
    return write_tool(tree, "marker.py", f'''
open({str(marker)!r}, "w").close()
flag_on("{flag}")
print(read_nodes())
''')


def _set_report(r, path):
    r.argv[r.argv.index("--report") + 1] = str(path)


def test_cell19a_report_naming_the_live_store_is_refused_and_the_store_is_untouched(script, tmp_path, live, tree, capsys):
    """C1 for --report: a report path that IS the live knowledge.db is refused (exit 2, named reason) before
    any tool runs and before a scratch directory exists; the store's bytes are unchanged. Red under a script
    that writes the JSON report wherever it is told (it overwrites the store)."""
    marker = tmp_path / "ran.marker"
    rel = _marker_tool(tree, marker, "fake_marker_reads_via_owner")
    r = Run(script, tmp_path, live, tree, [entry("fake_marker_reads_via_owner", rel)])
    _set_report(r, live / "knowledge.db")
    before = sha(live / "knowledge.db")
    r.go(capsys)
    assert r.rc == 2
    assert "refus" in r.err.lower() and "--report" in r.err and "live" in r.err.lower()
    assert sha(live / "knowledge.db") == before
    assert not marker.exists(), "the refusal must come BEFORE any tool runs"
    assert _no_scratch_left(tmp_path), "the refusal must come before the scratch directory is made"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_cell19b_report_naming_a_store_sibling_is_refused(script, tmp_path, live, tree, capsys, suffix):
    """The store's -wal / -shm / -journal siblings are the store too. Red under a script that guards only the
    exact knowledge.db name."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, live / ("knowledge.db" + suffix))
    r.go(capsys)
    assert r.rc == 2 and "--report" in r.err
    assert not (live / ("knowledge.db" + suffix)).exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_cell19h_siblings_of_a_symlinked_live_store_outside_the_home_are_refused(script, tmp_path, live, tree, capsys,
                                                                                  suffix):
    """When the live knowledge.db is a symlink to a store outside the home, the store and its -wal/-shm/-journal
    siblings sit outside the live home too, so only the sibling rule (not the 'inside the live home' rule)
    can refuse them. Red under a script that guards the exact store name only."""
    elsewhere = tmp_path / "store-elsewhere"
    elsewhere.mkdir()
    (live / "knowledge.db").rename(elsewhere / "store.db")
    (live / "knowledge.db").symlink_to(elsewhere / "store.db")
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, elsewhere / ("store.db" + suffix))
    before = sha(elsewhere / "store.db")
    r.go(capsys)
    assert r.rc == 2 and "--report" in r.err and "live store" in r.err
    assert sha(elsewhere / "store.db") == before


def test_cell19c_report_anywhere_inside_the_live_home_is_refused(script, tmp_path, live, tree, capsys):
    """Any path inside the live ENGRAM_HOME is refused, including a not-yet-existing file in a subdirectory.
    Red under a script that guards only the store file names."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    before = sorted(p.name for p in live.iterdir())
    for target in (live / "report.json", live / "sub" / "dir" / "report.json"):
        r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
        _set_report(r, target)
        r.go(capsys)
        assert r.rc == 2 and "--report" in r.err and "live home" in r.err, target
    assert sorted(p.name for p in live.iterdir()) == before


def test_cell19d_report_through_a_symlink_to_the_live_store_is_refused(script, tmp_path, live, tree, capsys):
    """The path is resolved with realpath first: a symlink to the store is the store. Red under a script that
    compares the path as written."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    link = tmp_path / "innocent.json"
    link.symlink_to(live / "knowledge.db")
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, link)
    before = sha(live / "knowledge.db")
    r.go(capsys)
    assert r.rc == 2 and sha(live / "knowledge.db") == before


def test_cell19e_report_naming_an_existing_non_regular_file_is_refused(script, tmp_path, live, tree, capsys):
    """An existing directory or FIFO is not a report file. Red under a script that writes to whatever exists."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    fifo = tmp_path / "pipe"
    os.mkfifo(str(fifo))
    adir = tmp_path / "adir"
    adir.mkdir()
    for target in (adir, fifo):
        r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
        _set_report(r, target)
        r.go(capsys)
        assert r.rc == 2 and "--report" in r.err and "regular" in r.err, target


def test_cell19f_report_inside_the_scratch_base_the_run_deletes_is_refused(script, tmp_path, live, tree, capsys):
    """A report inside a scratch directory (<base>/bA-*) would be deleted by the run's own cleanup. Red under
    a script that does not look."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, tmp_path / "scratch" / "bA-guess" / "report.json")
    r.go(capsys)
    assert r.rc == 2 and "--report" in r.err and "scratch" in r.err


def test_cell19g_report_in_an_unrelated_directory_still_works(script, tmp_path, live, tree, capsys):
    """The guard must not refuse an ordinary report: an unrelated tmp dir (and a plain overwrite of an
    existing regular file there) works. Red under a guard that refuses everything."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "old.json").write_text("{}")
    for target in (other / "new.json", other / "old.json"):
        r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
        _set_report(r, target)
        r.go(capsys)
        assert r.rc == 0, r.err
        assert json.loads(target.read_text())["summary"]["PASS"] == 1


# The artifact fake writes ./out.txt (its cwd is the run's work dir) in the runs named by WHEN.
ARTIFACT_TOOL = PRELUDE + '''
KEY = "fake_art_reads_via_owner"
WHEN = "@WHEN@"
out = os.path.join(os.getcwd(), "out.txt")
if flag_on(KEY):
    if WHEN in ("both", "on"):
        open(out, "w").write("same")
    print("nodes=2")
else:
    if WHEN in ("both", "off"):
        open(out, "w").write("same")
    print("nodes=%d" % read_nodes())
'''


def _artifact_run(script, tmp_path, live, tree, capsys, when):
    (tree / "tools" / "art.py").write_text(ARTIFACT_TOOL.replace("@WHEN@", when))
    return run(script, tmp_path, live, tree,
               [entry("fake_art_reads_via_owner", "tools/art.py", artifacts=["${work}/out.txt"])], capsys)


def test_cell20a_declared_artifact_written_in_neither_run_is_fail(script, tmp_path, live, tree, capsys):
    """Both runs missing the artifact used to read '<absent>' == '<absent>' and PASS. The reason names the
    artifact and both runs. Red under a script that compares the digests only."""
    r = _artifact_run(script, tmp_path, live, tree, capsys, "neither")
    t = r.tool("fake_art_reads_via_owner")
    assert t["verdict"] == "FAIL" and r.rc == 1
    assert "out.txt" in t["reason"] and "missing" in t["reason"]
    assert "OFF" in t["reason"] and "ON" in t["reason"]


@pytest.mark.parametrize("when,missing_run", [("off", "ON"), ("on", "OFF")])
def test_cell20b_declared_artifact_written_in_only_one_run_is_fail_naming_the_run(script, tmp_path, live, tree, capsys,
                                                                                   when, missing_run):
    """Missing on either side is FAIL and the reason names the run that lacks it. Red under a script that
    reports only 'differ'."""
    r = _artifact_run(script, tmp_path, live, tree, capsys, when)
    t = r.tool("fake_art_reads_via_owner")
    assert t["verdict"] == "FAIL"
    assert f"missing in the {missing_run} run" in t["reason"]


def test_cell20c_declared_artifact_written_in_both_runs_and_equal_is_pass(script, tmp_path, live, tree, capsys):
    """The control: with the artifact present and equal in both runs the verdict is PASS (so cells 20a/20b are
    red because of the artifact, not because of some other fault of the fake). Red under a script that
    treats every artifact tool as FAIL."""
    r = _artifact_run(script, tmp_path, live, tree, capsys, "both")
    assert r.tool("fake_art_reads_via_owner")["verdict"] == "PASS" and r.rc == 0


def test_cell20d_a_declared_empty_directory_artifact_counts_as_missing(script, tmp_path, live, tree, capsys):
    """A declared directory that exists but holds no file gives no digest to compare; it is treated as
    missing. Red under a script that lets an empty directory read as 'nothing differs'."""
    rel = write_tool(tree, "emptydir.py", '''
os.makedirs(os.path.join(os.getcwd(), "outdir"), exist_ok=True)
if not flag_on("fake_ed_reads_via_owner"):
    read_nodes()
print("x")
''')
    r = run(script, tmp_path, live, tree, [entry("fake_ed_reads_via_owner", rel, artifacts=["${work}/outdir"])], capsys)
    t = r.tool("fake_ed_reads_via_owner")
    assert t["verdict"] == "FAIL" and "outdir" in t["reason"] and "missing" in t["reason"]


def _spawn_script(r):
    import subprocess
    env = dict(os.environ)
    env.pop("BATCH_A_ACCEPTANCE_PATH", None)
    return subprocess.Popen([sys.executable, str(TOOL_PATH)] + r.argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, text=True)


def _pid_gone(pid: int, wait: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize("sig_name", ["SIGTERM", "SIGINT"])
def test_cell21_a_signal_runs_the_cleanup(script, tmp_path, live, tree, sig_name):
    """SIGTERM / SIGINT while a tool is running: the script exits non-zero promptly, the tool's process group
    is killed, and the scratch directory under --scratch-base is removed. Red under a script that takes
    SIGTERM with the default action (finally skipped: scratch left, tool still running)."""
    import signal as sg
    import time
    started = tmp_path / "started.pid"
    rel = write_tool(tree, "sleeper.py", f'''
import time
flag_on("fake_sleep_reads_via_owner")
open({str(started)!r}, "w").write(str(os.getpid()))
time.sleep(120)
''')
    r = Run(script, tmp_path, live, tree, [entry("fake_sleep_reads_via_owner", rel)])
    proc = _spawn_script(r)
    pid = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (started.exists() and started.read_text().strip()):
            assert proc.poll() is None, "the script ended before the tool started"
            time.sleep(0.1)
        pid = int(started.read_text())
        t0 = time.monotonic()
        proc.send_signal(getattr(sg, sig_name))
        rc = proc.wait(timeout=20)
        assert time.monotonic() - t0 < 15, "the script must stop promptly"
        assert rc != 0
        assert _pid_gone(pid), "the running tool must be killed"
        assert _no_scratch_left(tmp_path), "the scratch directory must be removed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pid is not None and not _pid_gone(pid, 0.2):
            os.kill(pid, 9)
        proc.stdout.close()
        proc.stderr.close()


def test_cell21b_main_restores_the_previous_signal_handlers(script, tmp_path, live, tree, capsys):
    """main() installs SIGTERM/SIGINT handlers for its run only. Red under a script that leaves them
    installed (a caller's own handling would be replaced for good)."""
    import signal as sg
    seen = {}
    real = script.signal.signal

    def spy(num, handler):
        seen.setdefault(num, []).append(handler)
        return real(num, handler)

    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    before = (sg.getsignal(sg.SIGTERM), sg.getsignal(sg.SIGINT))
    script.signal.signal = spy
    try:
        run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)], capsys)
    finally:
        script.signal.signal = real
    assert (sg.getsignal(sg.SIGTERM), sg.getsignal(sg.SIGINT)) == before
    assert len(seen.get(sg.SIGTERM, [])) >= 2 and len(seen.get(sg.SIGINT, [])) >= 2, \
        "handlers must be installed and then restored"


def test_cell22a_an_oserror_in_the_harness_is_exit_4_with_one_line_and_no_traceback(script, tmp_path, live, tree):
    """An unreadable --tree makes the harness itself fail with an OSError outside any tool run: exit 4, a
    one-line reason, no traceback. Red under a script that lets it escape main (exit 1 with a traceback,
    the code a tool FAIL gets)."""
    import subprocess
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    os.chmod(tree, 0)
    try:
        proc = subprocess.run([sys.executable, str(TOOL_PATH)] + r.argv, capture_output=True, text=True, timeout=120)
    finally:
        os.chmod(tree, 0o755)
    assert proc.returncode == 4, proc.stderr
    assert "Traceback" not in proc.stderr
    assert len([ln for ln in proc.stderr.splitlines() if ln.strip()]) == 1, proc.stderr
    assert _no_scratch_left(tmp_path)


# Python 3.14 made Path.is_file() / Path.is_dir() answer False for a path they cannot check (EACCES under an
# unsearchable directory), where 3.12 raised PermissionError. os.path.isfile / isdir swallow every OSError, which
# is that 3.14 behaviour, so patching the two methods to the os.path forms reproduces it on any interpreter.
_EMULATE_314 = ("import os, pathlib, runpy, sys\n"
                "pathlib.Path.is_file = lambda self: os.path.isfile(self)\n"
                "pathlib.Path.is_dir = lambda self: os.path.isdir(self)\n"
                "sys.argv = sys.argv[1:]\n"
                "runpy.run_path(sys.argv[0], run_name='__main__')\n")


def _run_child(argv, semantics):
    """The script as a child process, under this interpreter's own pathlib ('native') or with 3.14's
    is_file/is_dir answers emulated ('py314')."""
    import subprocess
    cmd = [sys.executable] + (["-c", _EMULATE_314] if semantics == "py314" else []) + [str(TOOL_PATH)] + argv
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("semantics", ["native", "py314"])
def test_cell22a_unreadable_tree_is_exit_4_under_both_pathlib_semantics(script, tmp_path, live, tree, semantics):
    """The same as 22a, run so that Path.is_file() answers False for an unreadable path (Python 3.14). The tool
    must not read 'could not be checked' as 'the tool file is missing' (SKIPPED, exit 3): exit 4, one line, no
    traceback, no scratch left. Red under a script that resolves the tool with Path.is_file() alone."""
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    os.chmod(tree, 0)
    try:
        proc = _run_child(r.argv, semantics)
    finally:
        os.chmod(tree, 0o755)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert len([ln for ln in proc.stderr.splitlines() if ln.strip()]) == 1, proc.stderr
    assert "PermissionError" in proc.stderr
    assert _no_scratch_left(tmp_path)


@pytest.mark.parametrize("semantics", ["native", "py314"])
def test_cell22d_an_owner_file_that_cannot_be_checked_is_not_reported_as_absent(script, tmp_path, live, tree,
                                                                                semantics):
    """The tool file is readable but engramd_owner.py is a symlink into a directory that cannot be searched.
    The harness must say it could not check it (exit 4, the OSError named), never 'the tree has no
    engramd_owner.py'. Red under a script that asks Path.is_file() alone in ScratchOwner.start."""
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "owner.py").write_text("")
    (tree / "engramd_owner.py").symlink_to(locked / "owner.py")
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel, needs_owner=True)])
    os.chmod(locked, 0)
    try:
        proc = _run_child(r.argv, semantics)
    finally:
        os.chmod(locked, 0o755)
    assert "no engramd_owner.py" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr and "PermissionError" in proc.stderr
    assert _no_scratch_left(tmp_path)


@pytest.mark.parametrize("semantics", ["native", "py314"])
def test_cell22e_a_tool_file_that_really_is_absent_stays_skipped(script, tmp_path, live, tree, semantics):
    """Absent is still absent: a tool file that does not exist is SKIPPED (every tool skipped: exit 3) under
    both pathlib semantics. Red under a script that turns every failed check into an error."""
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", "tools/not_there.py")])
    proc = _run_child(r.argv, semantics)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert r.report["tools"][0]["verdict"] == "SKIPPED"
    assert "tool file not found" in r.report["tools"][0]["reason"]


def test_cell22f_a_declared_artifact_that_cannot_be_checked_is_not_digested_as_absent(script, tmp_path,
                                                                                   monkeypatch):
    """A declared artifact under an unsearchable directory raises (run_tool turns it into that tool's FAIL naming
    the OSError); it is never recorded as <absent>, even where Path.is_file()/is_dir() answer False (3.14).
    A path that really is missing is still <absent>. Red under a script that asks Path.is_file() alone."""
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    monkeypatch.setattr(Path, "is_file", lambda self: os.path.isfile(self))
    monkeypatch.setattr(Path, "is_dir", lambda self: os.path.isdir(self))
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "out.txt").write_text("x")
    os.chmod(locked, 0)
    try:
        with pytest.raises(PermissionError):
            script._artifact_digests([str(locked / "out.txt")])
    finally:
        os.chmod(locked, 0o755)
    assert script._artifact_digests([str(tmp_path / "nope.txt")]) == {str(tmp_path / "nope.txt"): script.ABSENT}


def test_cell22g_an_unreadable_subdirectory_of_a_declared_artifact_dir_raises(script, tmp_path):
    """pathlib's rglob skips a subdirectory it cannot list (3.12 and 3.14 alike), so its files would vanish from
    the digest and an OFF/ON difference could hide there. The walk must raise instead (run_tool turns it into
    that tool's FAIL naming the PermissionError). Red under a script that digests with rglob."""
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    art = tmp_path / "art"
    (art / "locked").mkdir(parents=True)
    (art / "locked" / "hidden.txt").write_text("x")
    (art / "seen.txt").write_text("y")
    os.chmod(art / "locked", 0)
    try:
        with pytest.raises(PermissionError):
            script._artifact_digests([str(art)])
    finally:
        os.chmod(art / "locked", 0o755)


def test_cell22h_a_readable_artifact_tree_digests_with_the_same_keys_in_the_same_order(script, tmp_path):
    """Key parity for the directory walk: nested directories, a dotfile, an empty file, a symlink to a file
    (digested), a symlink to a directory (not descended into) and a dangling symlink (skipped). The expected
    keys and their order are what the rglob-based version produced."""
    art = tmp_path / "art"
    (art / "b" / "deep").mkdir(parents=True)
    (art / "a").mkdir()
    (art / "empty").mkdir()
    (art / "z.txt").write_text("z")
    (art / ".hid").write_text("h")
    (art / "zero.txt").write_text("")
    (art / "a" / "one.txt").write_text("1")
    (art / "b" / "two.txt").write_text("2")
    (art / "b" / "deep" / "three.txt").write_text("3")
    os.symlink(art / "z.txt", art / "lnk_file")
    os.symlink(art / "a", art / "lnk_dir")
    os.symlink(art / "nope", art / "dangling")
    got = script._artifact_digests([str(art)])
    assert [k.split("::", 1)[1] for k in got] == [".hid", "a/one.txt", "b/deep/three.txt", "b/two.txt",
                                                  "lnk_file", "z.txt", "zero.txt"]
    assert got[f"{art}::z.txt"] == got[f"{art}::lnk_file"] == sha_text("z")
    assert got[f"{art}::a/one.txt"] == sha_text("1")
    only_empty = tmp_path / "only_empty"
    (only_empty / "sub").mkdir(parents=True)
    assert script._artifact_digests([str(only_empty)]) == {str(only_empty): script.EMPTY_DIR}


def test_cell22b_any_other_unexpected_exception_in_the_harness_is_exit_4(script, tmp_path, live, tree, capsys, monkeypatch):
    """Not only OSError: an unexpected exception from the harness path is exit 4 with a one-line reason, and
    the scratch directory is still removed. Red under a script that catches only OSError."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])

    def boom(*a, **k):
        raise RuntimeError("synthetic harness fault")
    monkeypatch.setattr(script, "pick_ids", boom)
    r = run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)], capsys)
    assert r.rc == 4
    assert "synthetic harness fault" in r.err and "Traceback" not in r.err
    assert _no_scratch_left(tmp_path)


def test_cell22c_a_tool_crash_stays_fail_not_exit_4(script, tmp_path, live, tree, capsys):
    """A tool that crashes is a verdict (FAIL, exit 1), never the harness code. Red under a script that maps
    every exception to 4."""
    rel = write_tool(tree, "crash.py", 'flag_on("fake_c_reads_via_owner")\nraise RuntimeError("tool-boom")\n')
    r = run(script, tmp_path, live, tree, [entry("fake_c_reads_via_owner", rel)], capsys)
    assert r.rc == 1 and r.tool("fake_c_reads_via_owner")["verdict"] == "FAIL"


def test_cell23_the_stated_limits_are_documented_in_the_docstring_and_help(script, capsys):
    """The three stated limits are in the module docstring, and --help carries them too. Red under a script
    that documents none of them."""
    doc = " ".join(script.__doc__.split())
    for needle in ("stderr is never compared", "-I/-S", "sitecustomize", "resulting store state is not compared"):
        assert needle in doc, needle
    with pytest.raises(SystemExit):
        script.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    for needle in ("stderr is never compared", "-I/-S", "resulting store state is not compared"):
        assert needle in out, needle


# --- round 3: hard links, empty artifacts, per-tool evaluation errors, a second signal, the owner ----

def test_cell24a_report_hard_linked_to_the_live_store_is_refused_and_the_store_is_untouched(script, tmp_path, live, tree,
                                                                                            capsys):
    """A hard link is a second name for the store's inode; realpath does not follow it. Refused (exit 2, named
    reason) before any tool runs, store bytes unchanged. Red under a guard that compares realpath strings only
    (the report would truncate the store)."""
    marker = tmp_path / "ran.marker"
    rel = _marker_tool(tree, marker, "fake_marker_reads_via_owner")
    other = tmp_path / "elsewhere"
    other.mkdir()
    os.link(live / "knowledge.db", other / "r.json")
    r = Run(script, tmp_path, live, tree, [entry("fake_marker_reads_via_owner", rel)])
    _set_report(r, other / "r.json")
    before = sha(live / "knowledge.db")
    r.go(capsys)
    assert r.rc == 2 and "--report" in r.err and "refus" in r.err.lower()
    assert sha(live / "knowledge.db") == before
    assert not marker.exists() and _no_scratch_left(tmp_path)


@pytest.mark.parametrize("sibling", ["knowledge.db-wal", "knowledge.db-shm", "knowledge.db-journal", "config.json"])
def test_cell24b_report_hard_linked_to_a_store_sibling_or_a_live_home_file_is_refused(script, tmp_path, live, tree,
                                                                                       capsys, sibling):
    """The same identity rule covers the -wal/-shm/-journal siblings and any top-level file of the live home
    (config.json). Red under a guard that checks the store inode only."""
    if sibling != "config.json":
        (live / sibling).write_text("sibling bytes")
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    other = tmp_path / "elsewhere"
    other.mkdir()
    os.link(live / sibling, other / "r.json")
    before = (live / sibling).read_bytes()
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, other / "r.json")
    r.go(capsys)
    assert r.rc == 2 and "--report" in r.err
    assert (live / sibling).read_bytes() == before


def test_cell24c_report_whose_parent_does_not_exist_is_refused_before_any_tool_runs(script, tmp_path, live, tree,
                                                                                    capsys):
    """The report is written after the tools ran; a missing parent used to fail at write time (exit 4) after
    all that work. Refused early (exit 2). Red under a guard that does not look at the parent."""
    marker = tmp_path / "ran.marker"
    rel = _marker_tool(tree, marker, "fake_marker_reads_via_owner")
    r = Run(script, tmp_path, live, tree, [entry("fake_marker_reads_via_owner", rel)])
    _set_report(r, tmp_path / "no-such-dir" / "deeper" / "r.json")
    r.go(capsys)
    assert r.rc == 2 and "--report" in r.err and "parent" in r.err
    assert not marker.exists() and _no_scratch_left(tmp_path)


def test_cell25_a_zero_byte_declared_artifact_counts_as_missing(script, tmp_path, live, tree, capsys):
    """A tool that writes an EMPTY declared file in both runs used to hash equal and PASS. FAIL naming the
    artifact. Red under a script that hashes an empty file like any other."""
    rel = write_tool(tree, "emptyfile.py", '''
open(os.path.join(os.getcwd(), "out.txt"), "w").close()
if not flag_on("fake_ef_reads_via_owner"):
    read_nodes()
print("x")
''')
    r = run(script, tmp_path, live, tree, [entry("fake_ef_reads_via_owner", rel, artifacts=["${work}/out.txt"])], capsys)
    t = r.tool("fake_ef_reads_via_owner")
    assert t["verdict"] == "FAIL" and "out.txt" in t["reason"] and "missing" in t["reason"]
    assert r.rc == 1


def test_cell26_an_error_evaluating_one_tool_is_that_tools_fail_and_the_run_continues(script, tmp_path, live, tree,
                                                                                      capsys):
    """A tool makes its own declared artifact unreadable after writing it, so reading it raises PermissionError
    while the verdict is being earned. That is the tool's FAIL (reason given), the next tool still runs and
    PASSes, the exit is 1 (not 4) and the report is written. Red under a script that lets it abort the run."""
    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    bad = write_tool(tree, "unreadable.py", '''
p = os.path.join(os.getcwd(), "out.txt")
open(p, "w").write("data")
os.chmod(p, 0)
if not flag_on("fake_unr_reads_via_owner"):
    read_nodes()
print("x")
''')
    good = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = run(script, tmp_path, live, tree, [entry("fake_unr_reads_via_owner", bad, artifacts=["${work}/out.txt"]),
                                           entry("fake_good_reads_via_owner", good)], capsys)
    assert r.rc == 1, r.err
    t = r.tool("fake_unr_reads_via_owner")
    assert t["verdict"] == "FAIL" and "PermissionError" in t["reason"]
    assert r.tool("fake_good_reads_via_owner")["verdict"] == "PASS"
    assert _no_scratch_left(tmp_path)


def test_cell27_a_second_signal_during_cleanup_does_not_abort_the_cleanup(script, tmp_path, live, tree):
    """A tool leaves many files in the scratch work dir and sleeps. SIGTERM, then a second SIGTERM shortly
    after (while the scratch directory is being removed): the scratch is fully removed and the script exits.
    Red under a script whose handler stays installed during cleanup (rmtree is interrupted, scratch left)."""
    import signal as sg
    import time
    started = tmp_path / "started.pid"
    rel = write_tool(tree, "many.py", f'''
import time
flag_on("fake_many_reads_via_owner")
d = os.path.join(os.getcwd(), "many")
os.makedirs(d)
for i in range(30000):
    open(os.path.join(d, "f%d" % i), "w").close()
open({str(started)!r}, "w").write(str(os.getpid()))
time.sleep(120)
''')
    r = Run(script, tmp_path, live, tree, [entry("fake_many_reads_via_owner", rel)])
    proc = _spawn_script(r)
    pid = None
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not (started.exists() and started.read_text().strip()):
            assert proc.poll() is None, "the script ended before the tool started"
            time.sleep(0.05)
        pid = int(started.read_text())
        proc.send_signal(sg.SIGTERM)
        time.sleep(0.15)
        if proc.poll() is None:
            proc.send_signal(sg.SIGTERM)
        proc.wait(timeout=60)
        assert _pid_gone(pid)
        assert _no_scratch_left(tmp_path), "the second signal must not abort the scratch removal"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pid is not None and not _pid_gone(pid, 0.2):
            os.kill(pid, 9)
        proc.stdout.close()
        proc.stderr.close()


def test_cell28_a_signal_while_the_scratch_owner_runs_stops_the_owner(script, tmp_path, live, tree):
    """The ON run of an owner-needing tool runs under a scratch owner (the REAL engramd_owner.py of this
    checkout, reached through symlinks in the fake tree: the suite has no fake-owner seam). SIGTERM while that
    owner serves: the script exits and the owner pid, read from the scratch home's pidfile, is gone. Red under a
    script whose signal path skips ScratchOwner.stop (the owner outlives the run)."""
    import signal as sg
    import time
    src = REPO_ROOT / "src" / "engram"
    for item in src.iterdir():
        if item.name not in ("tools", "__pycache__") and not (tree / item.name).exists():
            (tree / item.name).symlink_to(item)
    started = tmp_path / "tool.started"
    pidfile_copy = tmp_path / "owner.pid.copy"
    rel = write_tool(tree, "needsowner.py", f'''
import shutil, time
if flag_on("fake_owner_reads_via_owner"):
    shutil.copyfile(os.path.join(HOME, "engramd.pid"), {str(pidfile_copy)!r})
    open({str(started)!r}, "w").write("1")
    time.sleep(120)
else:
    read_nodes()
print("x")
''')
    import shutil
    import tempfile
    # the owner's socket lives at <scratch>/run/h/run/engramd.sock: AF_UNIX allows ~103 bytes, so the scratch
    # base must be short (pytest's tmp_path is not)
    short_base = Path(tempfile.mkdtemp(prefix="bA", dir="/tmp"))
    # the owner starts only on a store whose PRAGMA user_version is at least its latest schema version; the
    # backup-API snapshot carries the value into the scratch store
    stamp = sqlite3.connect(str(live / "knowledge.db"))
    stamp.execute("PRAGMA user_version = 1000")
    stamp.commit()
    stamp.close()
    r = Run(script, tmp_path, live, tree, [entry("fake_owner_reads_via_owner", rel, needs_owner=True)])
    r.argv[r.argv.index("--scratch-base") + 1] = str(short_base)
    proc = _spawn_script(r)
    owner_pid = None
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not started.exists():
            if proc.poll() is not None:
                pytest.skip("the real scratch owner could not start in this environment: "
                            + (proc.stdout.read() or "")[-600:] + (proc.stderr.read() or "")[-300:])
            time.sleep(0.1)
        assert started.exists(), "the ON run never started"
        owner_pid = int(pidfile_copy.read_text().split()[0])
        assert owner_pid != proc.pid
        os.kill(owner_pid, 0)  # it is alive now
        proc.send_signal(sg.SIGTERM)
        proc.wait(timeout=60)
        assert _pid_gone(owner_pid, 10), "the scratch owner must be stopped on the signal path"
        assert list(short_base.iterdir()) == []
    finally:
        shutil.rmtree(short_base, ignore_errors=True)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if owner_pid is not None and not _pid_gone(owner_pid, 0.2):
            os.kill(owner_pid, 9)
        proc.stdout.close()
        proc.stderr.close()


# --- issue 4290 fold: atomic report write, the mktemp hint, no home path in the report ----------

HINT = "use a path from `mktemp -d`"


def test_cell29a_a_link_created_after_the_check_does_not_redirect_the_report_into_the_store(
        script, tmp_path, live, tree, capsys, monkeypatch):
    """The window between check_report_path and the write: a hard link from the report path to the store
    appears after the check passed. The report is written through a temp file and renamed over the path, so
    the link (a directory entry) is replaced and the store's inode is never written. Red under an in-place
    write (it truncates the store through the link). The seam is format_report: it runs after every check
    and before the write."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    other = tmp_path / "elsewhere"
    other.mkdir()
    target = other / "r.json"
    real_format = script.format_report

    def format_then_link(rep):
        os.link(live / "knowledge.db", target)
        return real_format(rep)

    monkeypatch.setattr(script, "format_report", format_then_link)
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, target)
    before = sha(live / "knowledge.db")
    r.go(capsys)
    assert r.rc == 0, r.err
    assert sha(live / "knowledge.db") == before, "the report was written through the link into the store"
    assert json.loads(target.read_text())["summary"]["PASS"] == 1


def test_cell29b_no_temp_file_is_left_beside_the_report_on_success(script, tmp_path, live, tree, capsys):
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, other / "r.json")
    r.go(capsys)
    assert r.rc == 0, r.err
    assert sorted(p.name for p in other.iterdir()) == ["r.json"]


def test_cell29c_no_temp_file_is_left_on_a_forced_write_error_and_the_old_report_survives(
        script, tmp_path, live, tree, capsys, monkeypatch):
    """A failure after the temp file exists (here the rename) removes the temp file, leaves the previous
    report intact, and is still the harness failure (exit 4, one line)."""
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "r.json").write_text("old report")

    def boom(*a, **k):
        raise OSError("forced")

    monkeypatch.setattr(script.os, "replace", boom)
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, other / "r.json")
    r.go(capsys)
    assert r.rc == 4 and "cannot write the report" in r.err
    assert sorted(p.name for p in other.iterdir()) == ["r.json"]
    assert (other / "r.json").read_text() == "old report"


def _refusal_cases(tmp_path, live):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.link(live / "knowledge.db", elsewhere / "linked.json")
    (live / "knowledge.db-wal").write_text("w")
    (elsewhere / "adir").mkdir()
    return {
        "live store": live / "knowledge.db",
        "store sibling": live / "knowledge.db-wal",
        "inside live home": live / "sub" / "r.json",
        "not a regular file": elsewhere / "adir",
        "scratch directory": tmp_path / "scratch" / "bA-guess" / "r.json",
        "hard link": elsewhere / "linked.json",
        "missing parent": tmp_path / "no-such-dir" / "r.json",
    }


@pytest.mark.parametrize("reason", ["live store", "store sibling", "inside live home", "not a regular file",
                                    "scratch directory", "hard link", "missing parent"])
def test_cell30a_every_report_refusal_ends_with_the_mktemp_hint(script, tmp_path, live, tree, capsys, reason):
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    r = Run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)])
    _set_report(r, _refusal_cases(tmp_path, live)[reason])
    r.go(capsys)
    assert r.rc == 2 and "--report" in r.err, r.err
    assert r.err.strip().endswith(HINT), r.err


def test_cell30b_help_carries_the_mktemp_hint(script, capsys):
    with pytest.raises(SystemExit):
        script.main(["--help"])
    assert HINT in " ".join(capsys.readouterr().out.split())


def test_cell31a_a_tree_under_home_is_reported_as_tilde_and_no_home_path_is_in_the_report(
        script, tmp_path, live, capsys, monkeypatch):
    """The tree, and every other string that carries it (the crash tool's stderr tail, verbose heads), is
    recorded as ~/... when under $HOME, in the JSON and the printed text. Red when the substitution is
    dropped."""
    home = tmp_path / "Users" / "seat"
    home_tree = home / "work" / "tree"
    (home_tree / "tools").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    good = write_tool(home_tree, "good.py", GOOD[len(PRELUDE):])
    crash = write_tool(home_tree, "crash.py", 'flag_on("fake_c_reads_via_owner")\nraise RuntimeError("boom")\n')
    r = run(script, tmp_path, live, home_tree,
            [entry("fake_good_reads_via_owner", good), entry("fake_c_reads_via_owner", crash)], capsys,
            extra=["--verbose"])
    assert r.rc == 1, r.err
    raw = r.report_path.read_text()
    assert r.report["tree"] == "~/work/tree"
    assert "boom" in r.tool("fake_c_reads_via_owner")["stderr_tail"], "the crash trace must still be there"
    for text in (raw, r.out):
        assert str(home) not in text and os.path.realpath(home) not in text
    assert "tree:          ~/work/tree" in r.out


def test_cell31b_a_tree_outside_home_stays_absolute(script, tmp_path, live, tree, capsys, monkeypatch):
    rel = write_tool(tree, "good.py", GOOD[len(PRELUDE):])
    other_home = tmp_path / "Users" / "seat"
    other_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(other_home))
    r = run(script, tmp_path, live, tree, [entry("fake_good_reads_via_owner", rel)], capsys)
    assert r.rc == 0, r.err
    assert r.report["tree"] == str(tree)
