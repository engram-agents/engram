"""Contract tests for tools/engramd_read_scan.py, the U4c build-time scanner
(#3196, part of #2652): R10 (the build fails on a site that is neither a
readable literal nor declared), R11 (reuse U2's call walker; U2 unchanged),
R12 (the committed template list is drift-gated), R13 (discrimination).

Written BEFORE the implementation, against small synthetic fixture packages
(like tests/test_write_op_census.py, whose root syntax this reuses:
"<file>:<function_name>" relative to --package).

The API these tests pin (`tools/engramd_read_scan.py`):

  scan(package_dir: Path, roots: list[str], exclusions: list[dict]) -> dict
      {"schema": 1,
       "templates":            [{"key", "sql", "sites": ["file:line", ...]}],  sorted by key
       "authorizer_governed":  ["file:line", ...],      # PRAGMA user_version-style reads
       "write_sites":          ["file:line", ...],      # INSERT/UPDATE/DELETE/DDL and PRAGMA writes
       "declared_out_of_scope":[{"site", "reason"}],    # from `exclusions`
       "failures":             [{"site", "kind"}]}      # anything else; non-empty => build fails
  main(argv) -> int     --package DIR --config FILE (--out FILE | --check FILE)
      exit 0 + writes --out when failures == []; exit 1 and writes NOTHING when
      failures != []; --check exits 1 on drift between FILE and a fresh scan.
  config: {"roots": [...], "exclusions": [{"site": "f.py:12", "reason": "..."}]}

Every test docstring names its mutation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_read_scan.py"
U2_PATH = REPO_ROOT / "tools" / "write_op_census.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load("engramd_read_scan", TOOL_PATH)


def _pkg(tmp_path: Path, files: Dict[str, str]) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        full = pkg / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return pkg


def _key(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _by_key(result: dict) -> dict:
    return {t["key"]: t for t in result["templates"]}


# --- R10: kinds ---------------------------------------------------------------

def test_r10_literal_select_is_registered_with_its_site(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n"
        "    return conn.execute('SELECT id FROM notes WHERE title = ?', ('x',))\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    sql = "SELECT id FROM notes WHERE title = ?"
    assert r["failures"] == []
    assert _by_key(r)[_key(sql)]["sql"] == sql
    assert _by_key(r)[_key(sql)]["sites"] == ["hook.py:run"]


def test_r10_module_level_literal_select_is_registered(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "Q = 'SELECT id FROM notes WHERE id = ?'\n"
        "def run(conn):\n"
        "    return conn.execute(Q, (1,))\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == []
    assert _key("SELECT id FROM notes WHERE id = ?") in _by_key(r)


def test_r10_adjacent_literal_concatenation_registers_the_joined_bytes(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n"
        "    return conn.execute('SELECT id '\n"
        "                        'FROM notes WHERE id = ?', (1,))\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == []
    assert _key("SELECT id FROM notes WHERE id = ?") in _by_key(r)


def test_r10_pragma_read_is_authorizer_governed_not_a_template(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n"
        "    return conn.execute('PRAGMA user_version').fetchone()\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == [] and r["templates"] == []
    assert r["authorizer_governed"] == ["hook.py:run"]


def test_r10_pragma_journal_mode_write_is_a_write_site_not_governed(tool, tmp_path):
    """Design owner (2026-10-02): `PRAGMA journal_mode=WAL` is a mode WRITE and
    belongs in write_sites, never in authorizer_governed."""
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n"
        "    conn.execute('PRAGMA journal_mode=WAL')\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == []
    assert r["write_sites"] == ["hook.py:run"]
    assert r["authorizer_governed"] == []


@pytest.mark.parametrize("stmt", [
    "UPDATE notes SET title = ? WHERE id = ?",
    "INSERT INTO notes (title) VALUES (?)",
    "DELETE FROM notes WHERE id = ?",
    "CREATE TABLE t (x)",
])
def test_r10_write_statements_are_reported_not_registered(tool, tmp_path, stmt):
    pkg = _pkg(tmp_path, {"hook.py":
        f"def run(conn):\n    conn.execute({stmt!r}, ())\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == [] and r["templates"] == []
    assert r["write_sites"] == ["hook.py:run"]


def test_r10_declared_exclusion_is_listed_with_its_reason(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n"
        "    return conn.execute('SELECT path FROM nodes_fts WHERE nodes_fts MATCH ?', ('x',))\n"})
    ex = [{"site": "hook.py:run", "reason": "package-DB read, out of scope (design SS3.5)", "sites": 1}]
    r = tool.scan(pkg, ["hook.py:run"], ex)
    assert r["failures"] == []
    assert r["templates"] == []
    # the count pin is a config-side check, not serialised (#3345)
    assert r["declared_out_of_scope"] == [{"site": "hook.py:run", "reason": ex[0]["reason"]}]


@pytest.mark.parametrize("body,kind", [
    ("    return conn.execute(f'SELECT id FROM notes WHERE id IN ({ph})', ids)", "fstring"),
    ("    return conn.execute(sql, ())", "variable"),
    ("    return conn.execute('SELECT id FROM ' + t, ())", "concat"),
    ("    return conn.execute(build(), ())", "call"),
    ("    return conn.execute('SELECT id FROM notes WHERE id = %s' % 1, ())", "concat"),
])
def test_r10_unreadable_site_fails_the_build(tool, tmp_path, body, kind):
    """Mutation: make unknown kinds default to 'ignore' -> these go red."""
    pkg = _pkg(tmp_path, {"hook.py": f"def run(conn, sql=None, ph='?', ids=(), t='t'):\n{body}\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert len(r["failures"]) == 1
    assert r["failures"][0]["site"] == "hook.py:2"
    assert r["failures"][0]["kind"] == kind


def test_r10_failure_in_a_reached_helper_outside_the_root_file_fails_the_build(tool, tmp_path):
    """The reached surface, not only the root's own file (Sol's 10-01 measurement)."""
    pkg = _pkg(tmp_path, {
        "hook.py": "from core import chain\ndef run(conn):\n    return chain(conn)\n",
        "core.py": "def chain(conn, ph='?'):\n    return conn.execute(f'SELECT 1 WHERE 1 IN ({ph})')\n",
    })
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert [f["site"] for f in r["failures"]] == ["core.py:2"]


def test_r10_unreached_unreadable_site_does_not_fail_the_build(tool, tmp_path):
    pkg = _pkg(tmp_path, {
        "hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n",
        "other.py": "def lonely(conn, s):\n    return conn.execute(s)\n",
    })
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == []


def test_r10_exclusion_naming_a_site_the_scan_never_reaches_is_an_error(tool, tmp_path):
    """A stale exclusion must not linger silently."""
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n"})
    ex = [{"site": "hook.py:99", "reason": "stale"}]
    r = tool.scan(pkg, ["hook.py:run"], ex)
    assert any(f["kind"] == "stale_exclusion" and f["site"] == "hook.py:99"
               for f in r["failures"])


def test_r10_two_sites_with_identical_sql_share_one_template_with_both_sites(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def a(conn):\n    return conn.execute('SELECT 1 FROM notes WHERE id = ?', (1,))\n"
        "def b(conn):\n    return conn.execute('SELECT 1 FROM notes WHERE id = ?', (2,))\n"})
    r = tool.scan(pkg, ["hook.py:a", "hook.py:b"], [])
    ts = _by_key(r)[_key("SELECT 1 FROM notes WHERE id = ?")]
    assert ts["sites"] == ["hook.py:a", "hook.py:b"]
    assert len(r["templates"]) == 1


def test_r10_scan_output_is_deterministic(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def a(conn):\n    return conn.execute('SELECT 2')\n"
        "def b(conn):\n    return conn.execute('SELECT 1')\n"})
    one = tool.scan(pkg, ["hook.py:a", "hook.py:b"], [])
    two = tool.scan(pkg, ["hook.py:b", "hook.py:a"], [])
    assert json.dumps(one, sort_keys=False) == json.dumps(two, sort_keys=False)


# --- CLI: failure writes NOTHING ---------------------------------------------

def _cfg(tmp_path, roots, exclusions=()):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"roots": roots, "exclusions": list(exclusions)}))
    return p


def test_r10_cli_exit_nonzero_and_no_file_written_on_failure(tool, tmp_path):
    """Mutation: write the file before checking failures -> red."""
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn, s):\n    return conn.execute(s)\n"})
    out = tmp_path / "templates.json"
    rc = tool.main(["--package", str(pkg), "--config", str(_cfg(tmp_path, ["hook.py:run"])),
                    "--out", str(out)])
    assert rc == 1
    assert not out.exists()


def test_r10_cli_failure_does_not_clobber_an_existing_committed_file(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn, s):\n    return conn.execute(s)\n"})
    out = tmp_path / "templates.json"
    out.write_text("PREVIOUS")
    rc = tool.main(["--package", str(pkg), "--config", str(_cfg(tmp_path, ["hook.py:run"])),
                    "--out", str(out)])
    assert rc == 1 and out.read_text() == "PREVIOUS"


def test_r10_cli_success_writes_the_file_and_exits_zero(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n"})
    out = tmp_path / "templates.json"
    rc = tool.main(["--package", str(pkg), "--config", str(_cfg(tmp_path, ["hook.py:run"])),
                    "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["schema"] == 1 and data["templates"][0]["sql"] == "SELECT 1"
    assert "failures" not in data or data["failures"] == []


def test_r10_cli_missing_package_argument_exits_2(tool, tmp_path):
    with pytest.raises(SystemExit) as e:
        tool.main(["--config", str(_cfg(tmp_path, [])), "--out", str(tmp_path / "o.json")])
    assert e.value.code == 2


def test_r10_cli_nonexistent_package_dir_exits_2_and_writes_nothing(tool, tmp_path):
    out = tmp_path / "o.json"
    rc = tool.main(["--package", str(tmp_path / "nope"), "--config",
                    str(_cfg(tmp_path, [])), "--out", str(out)])
    assert rc == 2 and not out.exists()


def test_r10_cli_unresolvable_root_exits_2_and_writes_nothing(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n"})
    out = tmp_path / "o.json"
    rc = tool.main(["--package", str(pkg), "--config", str(_cfg(tmp_path, ["hook.py:nope"])),
                    "--out", str(out)])
    assert rc == 2 and not out.exists()


# --- R11: reuse U2; U2 unchanged ----------------------------------------------

def test_r11_scanner_reuses_the_u2_call_walker_rather_than_reimplementing_it(tool):
    """Mutation: copy a private call-graph walker into the scanner -> red."""
    src = TOOL_PATH.read_text(encoding="utf-8")
    assert "write_op_census" in src
    for helper in ("_scan_package", "_bfs_reach"):
        assert helper in src, f"scanner must call U2's {helper}"
    assert "def _bfs_reach" not in src and "def _scan_package" not in src


def test_r11_u2_output_is_byte_identical_after_the_scanner_is_imported(tool, tmp_path):
    u2 = _load("write_op_census_r11", U2_PATH)
    pkg = _pkg(tmp_path, {"root.py":
        "def root_x(conn):\n    conn.execute('INSERT INTO widgets (a) VALUES (?)', (1,))\n"})
    before = json.dumps(u2.run_census(pkg, ["root.py:root_x"], ["widgets"]), sort_keys=True)
    tool.scan(pkg, ["root.py:root_x"], [])
    after = json.dumps(u2.run_census(pkg, ["root.py:root_x"], ["widgets"]), sort_keys=True)
    assert before == after


# --- R12: the committed list is drift-gated -----------------------------------

def test_r12_check_mode_passes_when_file_equals_a_fresh_scan(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n"})
    cfg = _cfg(tmp_path, ["hook.py:run"])
    out = tmp_path / "t.json"
    assert tool.main(["--package", str(pkg), "--config", str(cfg), "--out", str(out)]) == 0
    assert tool.main(["--package", str(pkg), "--config", str(cfg), "--check", str(out)]) == 0


def test_r12_hand_added_template_fails_the_drift_check(tool, tmp_path):
    """Mutation: skip the comparison -> red."""
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n"})
    cfg = _cfg(tmp_path, ["hook.py:run"])
    out = tmp_path / "t.json"
    tool.main(["--package", str(pkg), "--config", str(cfg), "--out", str(out)])
    data = json.loads(out.read_text())
    sql = "SELECT secret FROM keys"
    data["templates"].append({"key": _key(sql), "sql": sql, "sites": []})
    out.write_text(json.dumps(data))
    assert tool.main(["--package", str(pkg), "--config", str(cfg), "--check", str(out)]) == 1


def test_r12_source_change_without_regeneration_fails_the_drift_check(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n"})
    cfg = _cfg(tmp_path, ["hook.py:run"])
    out = tmp_path / "t.json"
    tool.main(["--package", str(pkg), "--config", str(cfg), "--out", str(out)])
    (pkg / "hook.py").write_text("def run(conn):\n    return conn.execute('SELECT 2')\n")
    assert tool.main(["--package", str(pkg), "--config", str(cfg), "--check", str(out)]) == 1


def test_r12_check_mode_on_a_missing_file_fails(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py": "def run(conn):\n    return conn.execute('SELECT 1')\n"})
    assert tool.main(["--package", str(pkg), "--config", str(_cfg(tmp_path, ["hook.py:run"])),
                      "--check", str(tmp_path / "absent.json")]) == 1


def test_r12_every_committed_key_is_the_sha256_of_its_sql():
    """Applies to the real committed file once it exists."""
    p = REPO_ROOT / "src" / "engram" / "engramd_read_templates.json"
    assert p.exists(), "the committed template list must exist"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    for t in data["templates"]:
        assert t["key"] == _key(t["sql"])
    assert [t["key"] for t in data["templates"]] == sorted(t["key"] for t in data["templates"])


def test_r12_committed_file_equals_a_fresh_scan_of_the_real_tree(tool):
    """The drift gate on the real tree. NOTE: this stays RED until U4c-0 (the
    two f-string reads in engram_core.generalization_chain rewritten to the
    json_each form) has merged; there is deliberately no allow-list or
    'pending' bucket that would make it green early (design SS3.4 / test 14)."""
    cfg = REPO_ROOT / "tools" / "engramd_read_scan.config.json"
    assert cfg.exists(), "the real-tree scan config must be committed"
    committed = REPO_ROOT / "src" / "engram" / "engramd_read_templates.json"
    rc = tool.main(["--package", str(REPO_ROOT / "src" / "engram"),
                    "--config", str(cfg), "--check", str(committed)])
    assert rc == 0


# === Round-1 reviewer-fairy folds (PR #3321, suggestions 5, 6) ==============

def test_r10_a_parameter_that_shadows_a_module_constant_is_a_variable_not_a_template(tool, tmp_path):
    """The scanner resolved a bare name by module constant alone: with
    SQL = 'SELECT 1 FROM a' at module level and `def run(conn, SQL):
    conn.execute(SQL)` it returned the module literal as a template, though
    the real function executes a caller-supplied variable. Mutation: resolve
    the name without checking the enclosing function's params/locals -> red."""
    pkg = _pkg(tmp_path, {"hook.py":
        "SQL = 'SELECT 1 FROM a'\n"
        "def run(conn, SQL):\n"
        "    return conn.execute(SQL)\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["templates"] == []
    assert [(f["site"], f["kind"]) for f in r["failures"]] == [("hook.py:3", "variable")]


def test_r10_a_local_assignment_that_shadows_a_module_constant_is_a_variable(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "SQL = 'SELECT 1 FROM a'\n"
        "def run(conn, other):\n"
        "    SQL = other\n"
        "    return conn.execute(SQL)\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["templates"] == []
    assert [f["site"] for f in r["failures"]] == ["hook.py:4"]


def test_r10_the_unshadowed_module_constant_is_still_registered(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "SQL = 'SELECT 1 FROM a'\n"
        "def run(conn):\n"
        "    return conn.execute(SQL)\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == [] and _key("SELECT 1 FROM a") in _by_key(r)


def test_r10_executescript_fails_the_build_even_when_its_first_statement_is_a_read(tool, tmp_path):
    """Mutation: classify executescript by its first verb -> registered -> red."""
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n    conn.executescript('SELECT 1; SELECT 2')\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["templates"] == []
    assert [f["site"] for f in r["failures"]] == ["hook.py:2"]


def test_r10_roots_config_covers_every_hook_function_and_no_module_level_execute_site():
    """Pins U2's hole (b) closed: a NEW hook function or module-level
    `.execute*` site must fail this test instead of being silently unscanned.
    Mutation: drop one root from tools/engramd_read_scan.config.json -> red."""
    import ast
    cfg = json.loads((REPO_ROOT / "tools" / "engramd_read_scan.config.json").read_text())
    roots = set(cfg["roots"])
    hooks = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
    wanted, module_level = set(), []
    for py in sorted(hooks.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        rel = f"hooks/claude/{py.name}"
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                wanted.add(f"{rel}:{node.name}")
        for stmt in tree.body:
            for sub in ast.walk(stmt):
                if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))):
                    break
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr.startswith("execute")):
                    module_level.append(f"{rel}:{sub.lineno}")
    assert wanted <= roots, sorted(wanted - roots)[:10]
    assert module_level == [], module_level


def test_r10_a_lambda_parameter_that_shadows_a_module_constant_is_a_variable(tool, tmp_path):
    """Mutation: read only the root function's args in _bound_names -> the
    lambda's parameter is missed and the module literal registers -> red."""
    pkg = _pkg(tmp_path, {"hook.py":
        "SQL = 'SELECT 1 FROM a'\n"
        "def run(conn):\n"
        "    g = lambda SQL: conn.execute(SQL)\n"
        "    return g\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["templates"] == []
    assert [(f["site"], f["kind"]) for f in r["failures"]] == [("hook.py:3", "variable")]


def test_r10_a_match_capture_that_shadows_a_module_constant_is_a_variable(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "SQL = 'SELECT 1 FROM a'\n"
        "def run(conn, x):\n"
        "    match x:\n"
        "        case str(SQL):\n"
        "            return conn.execute(SQL)\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["templates"] == []
    assert [(f["site"], f["kind"]) for f in r["failures"]] == [("hook.py:5", "variable")]


# === #3342: sites are keyed <file>:<function>, never <file>:<line> =========
#
# Contract (stated before the implementation):
#  C1  every committed `sites` / `write_sites` / `authorizer_governed` /
#      `declared_out_of_scope` entry is `<relfile>:<qualname>` (`<module>` for a
#      module-level site); N sites of one template in one function = N entries.
#  C2  inserting blank lines above registered sites in a COPY of the real tree
#      leaves the scan byte-identical to the committed file.
#  C3  a new template / changed SQL / removed read / read moved to another
#      function each make a scan of a COPY differ from the committed file.
#  C5  template keys + SQL are unchanged versus the pre-#3342 file.
#  C6  the registry loader never parses `sites`.

import ast as _ast
import re as _re
import shutil as _shutil

_COMMITTED = REPO_ROOT / "src" / "engram" / "engramd_read_templates.json"
_REAL_CFG = REPO_ROOT / "tools" / "engramd_read_scan.config.json"
_LINE_KEY = _re.compile(r":\d+$")

# Frozen at #3342 (== origin/dev's file before the change): the sha256 of every
# registered SQL. A site re-key must not move any of them. (#3485 re-froze the
# two lesson-tripwire-hook keys when their SQL gained the json_valid guard: a
# SQL change legitimately moves its own key; a re-key must still move none.)
# #3551 added a new template (the focused-ids read in _hooklib._focused_ids): its own key.
# #3761 (U9b): the checkpoint reader's one statement (11d79fc4...) is retired for a COUNT (931a3e96...)
# and a capped list (eb98e5fd...).
# #3665: extract_keywords batches its df lookup (new key 9d938f92...), so the per-token
# idf template (cbfeed19...) is no longer reachable from any hook root and is gone.
# #3742: the focused-ids read gained ORDER BY focused_at (087e3f55... -> 8fd6a2ad...); a SQL
# change legitimately moves its own key.
# #3824 (U12): the first tools/ template (33b71137...), tools/reconcile_lesson_demotions.py:find_candidates.
# #3862 (U14): the second, tools/cohort_dispatch.py:_fetch_nodes (6b8a8373...), the registry's one json_each form.
_FROZEN_TEMPLATE_KEYS = frozenset({
    "6b8a8373e14ff561366c89e57e0e982e4d416d952e467ae44ef2e13e9eb0cd42",
    "33b7113738f7310b25d3b9c6e109b18c3a790ba8c54879c25e8a774757703e3e",
    "8fd6a2adcab8ffb85efcf1e3b22c7433632f94a0d820c60cf37d4ba22ebd154e",
    "0280b9b584a3522d55be45f09ec258c28ec2d8b8e0e3cf80267097f593c71462",
    "931a3e96f918cd7e53e396133a92bfb3330843c4041d026c78b99ce950be6ab7",
    "eb98e5fd2e29cdedca963c60ecac97ab41292f87fe87d6c1f5b7575db3871e26",
    "2743db24b60c27d0ad4e70e440219d2904a12aa08d4fdca11760b8445b3fdc9c",
    "276dce2997ab333e1583a542b4099dbb52e431db42c9d272ad0dbef63dfb3437",
    "bca59f0784091d50dfa65e9ee9d35288defe5503524b0e43437bb701e1dc7e80",
    "4795866d2d19b5085970ad79dd2705359930118747cd19d1ec6eb405627befb9",
    "49ac1236d8081aa987ee1eafa9b3f4f10e48249f3f4c29446b8be5c73bd20412",
    "5961b023ce8247394b6d859ca4d0959c3ae34d2e9be481a8fd726899c795ef8e",
    "5ca245030193bc0e9811e8af365843b66ab4c4d999bbf2d13b79eabc0df37ee8",
    "6b93753474b01e7fc71620df964ee442e9b3b6a7c9d679d00c1b24c29c65b76e",
    "6fccf1c54a61e35899981e3f084277c3dbe091705d6609ab3abb46e73972551b",
    "9f6da2c7f988f5d2d40b7cdf6afe3d1fef5abb993b47a72b16fe9070cc0c3c05",
    "a3df78eb81c6fb751522f885a0748cd416c1e268e35b3669cf24f84da1591b65",
    "a81aff5ee1cfc444b6044abc808bcd8573b6e94d771e505c5119ca6678b91046",
    "b1f125b644a0e5d0a8d1e81628d17e7b876b5dbce632cfbb7a4ba48732623765",
    "c7ebb686c91c35f89f5bff3b5f1e3b9a746e2568d35a35899fc868a0d8d99223",
    "9d938f92196ce5c30108d90b0c926ae6f8d10b89e963219bcf12415dce414a39",
    "cefc616a2876d56a7b84ead98f75fa894f5a3678510f4d5c5bbac0b78061feb8",
    "d8ae87a01e31523ab8daab4fc634e227cfdb00339c30995e50a0fe82f6b67be8",
    "755fd8686d5feac4204716df355f3c6656f9505332e06102735d02ab15135c23",
    "dc3d1b24f7b5d31cfb36abb9646c778d1dd42055b81cd0c99da5f206f23bc643",
    "e3872e7780af9a2fb346d7019adb7f9bd36fd49794c534d74c2f96e364c9d129",
    "f3fb3410d99851f0e833a131cbdc574e9e676b124dd85d9841250a21f8e05c0a",
    "f80855b47d7d7e10bf3fc0b5d045906133299e4433da52de0ea75c0fa5a851fe",
})


def _real_scan(tool, package: Path) -> dict:
    cfg = json.loads(_REAL_CFG.read_text())
    r = tool.scan(package, cfg["roots"], cfg["exclusions"])
    assert r["failures"] == [], r["failures"]
    return {k: v for k, v in r.items() if k != "failures"}


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2) + "\n"


@pytest.fixture()
def tree_copy(tmp_path):
    dst = tmp_path / "engram_copy"
    _shutil.copytree(REPO_ROOT / "src" / "engram", dst,
                     ignore=_shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dst


def _prepend_blank_lines(path: Path, n: int = 7) -> None:
    path.write_text("\n" * n + path.read_text(encoding="utf-8"), encoding="utf-8")


def test_3342_site_is_keyed_on_function_not_line(tool, tmp_path):
    """Mutation: key on file:line again -> red."""
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n    return conn.execute('SELECT 1 FROM t')\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert _by_key(r)[_key("SELECT 1 FROM t")]["sites"] == ["hook.py:run"]


def test_3342_module_level_site_is_keyed_module(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n    return 1\n"
        "ROOT_CONN.execute('SELECT 1 FROM t')\n"})
    r = tool.scan(pkg, ["hook.py:3"], [])
    assert _by_key(r)[_key("SELECT 1 FROM t")]["sites"] == ["hook.py:<module>"]


def test_3342_two_sites_of_one_template_in_one_function_are_listed_twice(tool, tmp_path):
    """Count is carried by repetition: adding/removing a same-function site must
    change the output. Mutation: de-duplicate (set) -> red."""
    one = _pkg(tmp_path / "one", {"hook.py":
        "def run(conn):\n    conn.execute('SELECT 1 FROM t')\n"})
    two = _pkg(tmp_path / "two", {"hook.py":
        "def run(conn):\n    conn.execute('SELECT 1 FROM t')\n"
        "    return conn.execute('SELECT 1 FROM t')\n"})
    r1 = tool.scan(one, ["hook.py:run"], [])
    r2 = tool.scan(two, ["hook.py:run"], [])
    k = _key("SELECT 1 FROM t")
    assert _by_key(r1)[k]["sites"] == ["hook.py:run"]
    assert _by_key(r2)[k]["sites"] == ["hook.py:run", "hook.py:run"]
    assert r1 != r2


def test_3342_methods_and_nested_functions_get_a_qualname(tool, tmp_path):
    """Two same-named methods in different classes must not collapse.
    Mutation: use the bare function name -> red."""
    pkg = _pkg(tmp_path, {"hook.py":
        "class A:\n    def get(self, c):\n        return c.execute('SELECT 1 FROM a')\n"
        "class B:\n    def get(self, c):\n        return c.execute('SELECT 1 FROM a')\n"
        "def outer(c):\n    def inner():\n        return c.execute('SELECT 1 FROM a')\n    return inner\n"})
    r = tool.scan(pkg, ["hook.py:get", "hook.py:outer", "hook.py:inner"], [])
    assert _by_key(r)[_key("SELECT 1 FROM a")]["sites"] == [
        "hook.py:A.get", "hook.py:B.get", "hook.py:outer.inner"]


def test_3342_write_and_governed_sites_are_function_keyed(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n    conn.execute('PRAGMA user_version')\n"
        "    conn.execute('DELETE FROM t')\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["authorizer_governed"] == ["hook.py:run"]
    assert r["write_sites"] == ["hook.py:run"]


def test_3342_exclusion_may_name_the_function_and_is_reported_function_keyed(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n    return conn.execute('SELECT 1 FROM t')\n"})
    ex = [{"site": "hook.py:run", "reason": "r", "sites": 1}]
    r = tool.scan(pkg, ["hook.py:run"], ex)
    assert r["failures"] == [] and r["templates"] == []
    assert r["declared_out_of_scope"] == [{"site": "hook.py:run", "reason": "r"}]


def test_3342_function_exclusion_naming_nothing_reached_is_stale(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n    return conn.execute('SELECT 1 FROM t')\n"})
    r = tool.scan(pkg, ["hook.py:run"], [{"site": "hook.py:gone", "reason": "r", "sites": 1}])
    assert [(f["site"], f["kind"]) for f in r["failures"]] == [("hook.py:gone", "stale_exclusion")]


def test_3342_synthetic_line_shift_leaves_the_scan_unchanged(tool, tmp_path):
    a = _pkg(tmp_path / "a", {"hook.py":
        "def run(conn):\n    return conn.execute('SELECT 1 FROM t')\n"})
    b = _pkg(tmp_path / "b", {"hook.py":
        "\n\n\n# moved down\ndef run(conn):\n    return conn.execute('SELECT 1 FROM t')\n"})
    assert tool.scan(a, ["hook.py:run"], []) == tool.scan(b, ["hook.py:run"], [])


def test_3342_c1_committed_file_has_no_line_keyed_site():
    """Mutation: regenerate with the old line-keyed scanner -> red."""
    data = json.loads(_COMMITTED.read_text(encoding="utf-8"))
    entries = [s for t in data["templates"] for s in t["sites"]]
    entries += data["authorizer_governed"] + data["write_sites"]
    entries += [e["site"] for e in data["declared_out_of_scope"]]
    assert entries, "expected registered sites"
    assert [e for e in entries if _LINE_KEY.search(e)] == []
    for e in entries:
        assert ":" in e and not e.endswith(":")


def test_3342_c5_template_keys_and_sql_are_unchanged_by_the_site_rekey():
    data = json.loads(_COMMITTED.read_text(encoding="utf-8"))
    assert {t["key"] for t in data["templates"]} == _FROZEN_TEMPLATE_KEYS
    for t in data["templates"]:
        assert t["key"] == _key(t["sql"])


def test_3342_c4_real_tree_scan_is_deterministic(tool):
    pkg = REPO_ROOT / "src" / "engram"
    assert _dump(_real_scan(tool, pkg)) == _dump(_real_scan(tool, pkg))


def test_3342_c2_line_shift_in_the_real_tree_copy_is_invisible(tool, tree_copy):
    """Blank lines above every registered site in engram_core.py and in two
    hook files (one holding an exclusion, one a write/governed site).
    Mutation: key sites on line -> red."""
    committed = _COMMITTED.read_text(encoding="utf-8")
    assert _dump(_real_scan(tool, tree_copy)) == committed  # control: copy == real
    for rel in ("engram_core.py",
                "hooks/claude/engram-session-start-hook.py",
                "hooks/claude/engram-surface-hook.py"):
        _prepend_blank_lines(tree_copy / rel)
    assert _dump(_real_scan(tool, tree_copy)) == committed


def _single_literal_read_site(tree_copy: Path):
    """-> (path, Call node, sql) for a registered single-site literal template
    whose call appears exactly once in its file, on one line."""
    data = json.loads(_COMMITTED.read_text(encoding="utf-8"))
    for t in data["templates"]:
        if len(t["sites"]) != 1 or "\n" in t["sql"]:
            continue
        rel = t["sites"][0].rpartition(":")[0]
        path = tree_copy / rel
        src = path.read_text(encoding="utf-8")
        hits = [n for n in _ast.walk(_ast.parse(src))
                if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
                and n.func.attr.startswith("execute") and n.args
                and isinstance(n.args[0], _ast.Constant) and n.args[0].value == t["sql"]]
        if len(hits) == 1 and hits[0].lineno == hits[0].end_lineno:
            return path, hits[0], t["sql"]
    raise AssertionError("no suitable single-line literal read site found")


def _splice(path: Path, call, replacement: str) -> None:
    lines = path.read_text(encoding="utf-8").split("\n")
    ln = call.lineno - 1
    raw = lines[ln].encode("utf-8")
    lines[ln] = (raw[:call.col_offset] + replacement.encode("utf-8")
                 + raw[call.end_col_offset:]).decode("utf-8")
    path.write_text("\n".join(lines), encoding="utf-8")


def _line_seg(path: Path, call, a: int, b: int) -> str:
    raw = path.read_text(encoding="utf-8").split("\n")[call.lineno - 1].encode("utf-8")
    return raw[a:b].decode("utf-8")


@pytest.mark.parametrize("mutation", ["new_template", "changed_sql", "removed_read", "moved_read"])
def test_3342_c3_a_real_change_still_reddens_the_scan(tool, tree_copy, mutation):
    """Each real change must differ from the committed file (control: the
    unmutated copy equals it). Mutation: compare only template keys -> the
    moved_read case goes green -> red."""
    committed = _COMMITTED.read_text(encoding="utf-8")
    assert _dump(_real_scan(tool, tree_copy)) == committed  # control
    path, call, sql = _single_literal_read_site(tree_copy)
    src_call = _line_seg(path, call, call.col_offset, call.end_col_offset)
    recv = _line_seg(path, call, call.func.value.col_offset, call.func.value.end_col_offset)
    if mutation == "new_template":
        _splice(path, call, f"[{src_call}, {recv}.execute('SELECT brand_new_col FROM brand_new_tbl')]")
    elif mutation == "changed_sql":
        _splice(path, call, src_call.replace(sql, sql + " ", 1))
    elif mutation == "removed_read":
        _splice(path, call, "None")
    else:
        _splice(path, call, "_moved_read_3342()")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\ndef _moved_read_3342():\n    return {src_call}\n")
    assert _dump(_real_scan(tool, tree_copy)) != committed


def test_3342_c6_registry_loader_ignores_site_strings(tmp_path):
    """No consumer parses `sites`: the loader reads only key + sql."""
    import sqlite3
    rr = _load("engramd_read_registry_3342", REPO_ROOT / "src" / "engram" / "engramd_read_registry.py")
    sql = "SELECT id FROM nodes WHERE id = ?"
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT)")
    for sites in (["engram_core.py:somefunc"], ["engram_core.py:1530"], [], ["x:<module>"]):
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"schema": 1, "templates": [
            {"key": rr.template_key(sql), "sql": sql, "sites": sites}]}))
        assert rr.load_registry(p, conn).templates


# --- #3345: a function-form exclusion pins an exact site count -----------------

_EXCL_BODY = "def run(conn):\n    return conn.execute('SELECT 1 FROM t')\n"


def _fx(n=1):
    return [{"site": "hook.py:run", "reason": "r", "sites": n}]


def _mismatch(r):
    return [f for f in r["failures"] if f["kind"] == "exclusion_site_count_mismatch"]


def test_3345_exact_count_is_clean(tool, tmp_path):
    """Mutation: reject a matching count -> red."""
    pkg = _pkg(tmp_path, {"hook.py": _EXCL_BODY})
    assert tool.scan(pkg, ["hook.py:run"], _fx(1))["failures"] == []


def test_3345_a_new_read_in_an_excluded_function_is_a_failure(tool, tmp_path):
    """Mutation: drop the count check -> red (the new read was silently covered)."""
    pkg2 = _pkg(tmp_path / "b", {"hook.py":
        "def run(conn):\n    conn.execute('SELECT 1 FROM t')\n    return conn.execute('SELECT 2 FROM t')\n"})
    r = tool.scan(pkg2, ["hook.py:run"], _fx(1))
    m = _mismatch(r)
    assert len(m) == 1
    assert m[0]["site"] == "hook.py:run" and m[0]["expected"] == 1 and m[0]["actual"] == 2


def test_3345_a_removed_read_is_a_failure_not_just_a_shortfall_pass(tool, tmp_path):
    """Mutation: compare actual >= expected instead of == -> red."""
    pkg = _pkg(tmp_path, {"hook.py": _EXCL_BODY})
    r = tool.scan(pkg, ["hook.py:run"], _fx(2))
    m = _mismatch(r)
    assert len(m) == 1 and m[0]["expected"] == 2 and m[0]["actual"] == 1


@pytest.mark.parametrize("extra", [
    "conn.execute(sql)",
    "conn.execute('SELECT id FROM ' + t)",
    "conn.execute(f'SELECT {c}')",
    "conn.executescript('SELECT 1')",
])
def test_3345_an_unclassifiable_site_added_to_an_excluded_function_is_counted(tool, tmp_path, extra):
    """Mutation: skip unclassifiable sites when counting -> red."""
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn, sql, t, c):\n    conn.execute('SELECT 1 FROM t')\n    " + extra + "\n"})
    r = tool.scan(pkg, ["hook.py:run"], _fx(1))
    m = _mismatch(r)
    assert len(m) == 1 and m[0]["actual"] == 2
    # and with the right count the excluded function is clean (suppressed, not dropped from the count)
    assert tool.scan(pkg, ["hook.py:run"], _fx(2))["failures"] == []


@pytest.mark.parametrize("cmd_mode", ["--out", "--check"])
def test_3345_cli_exits_nonzero_on_count_mismatch_in_both_modes(tool, tmp_path, cmd_mode):
    """Mutation: CLI ignores the new failure kind -> red."""
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n    conn.execute('SELECT 1 FROM t')\n    return conn.execute('SELECT 2 FROM t')\n"})
    cfg = _cfg(tmp_path, ["hook.py:run"], _fx(1))
    out = tmp_path / "t.json"
    if cmd_mode == "--check":
        # a committed file that equals the scan output, so only the count pin can fail it
        good = tmp_path / "good.json"
        good.write_text(json.dumps({"roots": ["hook.py:run"], "exclusions": _fx(2)}))
        assert tool.main(["--package", str(pkg), "--config", str(good), "--out", str(out)]) == 0
    rc = tool.main(["--package", str(pkg), "--config", str(cfg), cmd_mode, str(out)])
    assert rc == 1
    if cmd_mode == "--out":
        assert not out.exists()


@pytest.mark.parametrize("bad", [None, 0, -1, "1", 1.0, True])
def test_3345_function_form_without_a_valid_sites_count_is_a_config_error(tool, tmp_path, bad):
    """Mutation: accept a missing/invalid count silently -> red."""
    pkg = _pkg(tmp_path, {"hook.py": _EXCL_BODY})
    entry = {"site": "hook.py:run", "reason": "r"}
    if bad is not None:
        entry["sites"] = bad
    with pytest.raises(tool._load_u2().CensusInputError):
        tool.scan(pkg, ["hook.py:run"], [entry])
    out = tmp_path / "t.json"
    rc = tool.main(["--package", str(pkg), "--config", str(_cfg(tmp_path, ["hook.py:run"], [entry])),
                    "--out", str(out)])
    assert rc == 2 and not out.exists()


def test_3345_line_form_exclusion_needs_no_sites_field(tool, tmp_path):
    """Mutation: demand `sites` on line-form too -> red."""
    pkg = _pkg(tmp_path, {"hook.py": _EXCL_BODY})
    r = tool.scan(pkg, ["hook.py:run"], [{"site": "hook.py:2", "reason": "r"}])
    assert r["failures"] == []


def test_3345_real_config_function_exclusions_pin_exactly_one_site():
    cfg = json.loads(_REAL_CFG.read_text())
    fn = [e for e in cfg["exclusions"] if not e["site"].rpartition(":")[2].isdigit()]
    assert len(fn) == 2
    for e in fn:
        assert e["sites"] == 1 and "exactly 1 site" in e["reason"], e


def test_3345_same_qualname_defs_share_a_key_so_moving_a_read_between_them_is_invisible(tool, tmp_path):
    """Pins the documented caveat on _qualname: two defs with one qualname in a
    file (e.g. under if/else) share a key. Mutation: disambiguate by line -> red."""
    read = "        return c.execute('SELECT 1 FROM t')\n"
    empty = "        return 0\n"
    head = "import os\nif os.name:\n    def run(c):\n"
    mid = "else:\n    def run(c):\n"
    a = _pkg(tmp_path / "a", {"hook.py": head + read + mid + empty})
    b = _pkg(tmp_path / "b", {"hook.py": head + empty + mid + read})  # the read moved to the other def
    ra = tool.scan(a, ["hook.py:run"], [])
    rb = tool.scan(b, ["hook.py:run"], [])
    assert ra["templates"] and ra["templates"][0]["sites"] == ["hook.py:run"]
    assert ra == rb
