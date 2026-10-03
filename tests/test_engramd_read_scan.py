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
    assert _by_key(r)[_key(sql)]["sites"] == ["hook.py:2"]


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
    assert r["authorizer_governed"] == ["hook.py:2"]


def test_r10_pragma_journal_mode_write_is_a_write_site_not_governed(tool, tmp_path):
    """Design owner (2026-10-02): `PRAGMA journal_mode=WAL` is a mode WRITE and
    belongs in write_sites, never in authorizer_governed."""
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n"
        "    conn.execute('PRAGMA journal_mode=WAL')\n"})
    r = tool.scan(pkg, ["hook.py:run"], [])
    assert r["failures"] == []
    assert r["write_sites"] == ["hook.py:2"]
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
    assert r["write_sites"] == ["hook.py:2"]


def test_r10_declared_exclusion_is_listed_with_its_reason(tool, tmp_path):
    pkg = _pkg(tmp_path, {"hook.py":
        "def run(conn):\n"
        "    return conn.execute('SELECT path FROM nodes_fts WHERE nodes_fts MATCH ?', ('x',))\n"})
    ex = [{"site": "hook.py:2", "reason": "package-DB read, out of scope (design SS3.5)"}]
    r = tool.scan(pkg, ["hook.py:run"], ex)
    assert r["failures"] == []
    assert r["templates"] == []
    assert r["declared_out_of_scope"] == ex


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
    assert ts["sites"] == ["hook.py:2", "hook.py:4"]
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
