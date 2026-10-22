"""#3863 step 1 (round 2): the raw-read gate, tools/raw_home_read_gate.py.

Ruling: Borges's ruling on #3863, 2026-10-21.

The gate fails on any RAW read of ENGRAM_HOME (`os.environ[...]`, `os.environ.get(...)`, `os.getenv(...)`,
`environ.get(...)`) in a tracked, non-test, non-docs `.py` file under `src/` or `tools/`, unless the file is
one of the pinned resolver files. Today's sites sit in tools/raw_home_read_baseline.json as per-file counts that
may only shrink. Same shape as tools/home_path_gate.py + tests/test_home_path_gate.py.

Contract pinned here:
  (a) a new raw read in a non-allowlisted file fails, naming file:line;
  (b) a baseline entry whose site is gone is reported as shrinkable and the check PASSES;
  (c) a baseline that GREW against the base (new entry, or a higher count) fails;
  (d) the pinned resolver files are exempt;
  (e) test files, docs and markdown are not scanned;
  (f) a raw read that abspath/resolves the value is NOT exempt (the silent version of the defect).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "raw_home_read_gate.py"
HOME_GATE = REPO / "tools" / "home_path_gate.py"
WORKFLOW = REPO / ".github" / "workflows" / "pr-checks.yml"
TIERS = REPO / "src" / "build" / "packaging" / "tiers.json"


def _load(path=TOOL, name="raw_home_read_gate"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load()


RAW = 'import os\nhome = os.environ.get("ENGRAM_HOME")\n'
A = "src/engram/a.py"
B = "tools/b.py"


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )


def _write_baseline(root, gate, baseline, allowlist=None, prefixes=None):
    p = root / gate.BASELINE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    if allowlist is None:
        allowlist = {rel: {"class": "resolver", "reason": "a pinned resolver layer"} for rel in gate.PINNED_RESOLVERS}
    doc = {
        "schema": 1,
        "scan_prefixes": list(gate.SCAN_PREFIXES if prefixes is None else prefixes),
        "baseline": dict(baseline),
        "allowlist": allowlist,
    }
    p.write_text(json.dumps(doc), encoding="utf-8")


def _repo(tmp_path, gate, files=None, baseline=None, allowlist=None, prefixes=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    for rel, text in (files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if baseline is not None:
        _write_baseline(tmp_path, gate, baseline, allowlist, prefixes)
    _git(tmp_path, "add", "-A")
    return tmp_path


def _commit(root, msg="c"):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


# -- the matcher ----------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    'h = os.environ.get("ENGRAM_HOME")',
    "h = os.environ.get('ENGRAM_HOME') or '~/.engram'",
    'h = os.environ["ENGRAM_HOME"]',
    'h = os.getenv("ENGRAM_HOME")',
    'h = os.getenv("ENGRAM_HOME", "~/.engram")',
    'h = environ.get("ENGRAM_HOME")',
    'h = os.environ.get( "ENGRAM_HOME" )',
    'h = Path(os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram"))',
    'raw = os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram"))',
])
def test_raw_read_spellings_are_matched(gate, line):
    assert gate.line_is_raw_read(line), line


@pytest.mark.parametrize("line", [
    'os.environ["ENGRAM_HOME"] = str(home)',          # a write, not a read
    'os.environ["ENGRAM_HOME"]=home',
    'os.environ.setdefault("ENGRAM_HOME", home)',
    'os.environ.pop("ENGRAM_HOME", None)',
    'env = dict(os.environ, ENGRAM_HOME=str(home))',
    'env = {"ENGRAM_HOME": str(home)}',
    'os.environ.get("ENGRAM_INVOCATION_LOG", str(ENGRAM_HOME / "x"))',
    'h = os.environ.get("MY_ENGRAM_HOME")',
    'h = os.environ.get("ENGRAM_HOME_X")',
    'h = engram_home()',
    '# Honors ENGRAM_HOME environment variable',
])
def test_non_reads_are_not_matched(gate, line):
    assert not gate.line_is_raw_read(line), line


def test_an_equality_compare_of_the_subscript_is_a_read(gate):
    assert gate.line_is_raw_read('if os.environ["ENGRAM_HOME"] == "x": pass')


# -- (a) a new raw read fails, naming file:line ---------------------------------------------

def test_a_new_raw_read_in_a_non_allowlisted_file_fails_with_file_and_line(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: RAW}, baseline={})
    problems = gate.check(root)
    assert len(problems) == 1, problems
    assert f"{A}:2" in problems[0]


def test_a_new_raw_read_in_a_baselined_file_above_its_count_fails(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: RAW + RAW}, baseline={A: 1})
    problems = gate.check(root)
    assert len(problems) == 1 and A in problems[0], problems


def test_a_file_at_its_baselined_count_is_green(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: RAW, B: "x = 1\n"}, baseline={A: 1})
    assert gate.check(root) == []


@pytest.mark.parametrize("rel", ["src/engram/engram_core.py", "tools/x.py", "src/build/engine/y.py", "src/z.py"])
def test_scope_is_src_and_tools(tmp_path, gate, rel):
    root = _repo(tmp_path, gate, {rel: RAW}, baseline={})
    assert gate.check(root), rel


@pytest.mark.parametrize("rel", ["scripts/x.py", "docs/x.py", "README.py", "plugins/x/y.py"])
def test_outside_src_and_tools_is_not_scanned(tmp_path, gate, rel):
    root = _repo(tmp_path, gate, {rel: RAW}, baseline={})
    assert gate.check(root) == [], rel


def test_only_a_physical_line_counts_a_multiline_call_is_a_known_bound(tmp_path, gate):
    text = 'import os\nh = os.environ.get(\n    "ENGRAM_HOME",\n)\n'
    root = _repo(tmp_path, gate, {A: text}, baseline={})
    assert gate.check(root) == []  # documented bound: the unit is the physical line


@pytest.mark.parametrize("line", [
    'env = os.environ; env.get("ENGRAM_HOME")',
    'from os import getenv as ge; ge("ENGRAM_HOME")',
    'os.environ.get ("ENGRAM_HOME")',
    'os.environ.copy().get("ENGRAM_HOME")',
    'dict(os.environ)["ENGRAM_HOME"]',
    'os.getenv(key="ENGRAM_HOME")',
    'os.environ.get("ENGRAM_" "HOME")',
    'os.environ.get("ENGRAM_" + "HOME")',
    'os.environb[b"ENGRAM_HOME"]',
])
def test_an_unmatched_spelling_is_a_documented_known_bound(gate, line):
    assert gate.line_is_raw_read(line) is False  # documented bound: the line pattern does not see it


def test_an_extensionless_python_tool_is_not_scanned_a_known_bound(tmp_path, gate):
    root = _repo(tmp_path, gate, {"tools/some-tool": RAW}, baseline={})
    assert gate.check(root) == []  # documented bound: only tracked .py files are read


# -- (b) a gone site is shrinkable, and the check passes -----------------------------------------

def test_b_a_baseline_count_above_the_tree_is_shrinkable_and_passes(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: RAW}, baseline={A: 3})
    problems, shrink = gate.analyse(root)
    assert problems == []
    assert gate.check(root) == []
    assert len(shrink) == 1 and A in shrink[0] and "1" in shrink[0], shrink


def test_b_a_fixed_file_still_listed_is_shrinkable_and_passes(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: "x = 1\n"}, baseline={A: 1})
    problems, shrink = gate.analyse(root)
    assert problems == [] and len(shrink) == 1 and A in shrink[0], (problems, shrink)


def test_b_the_cli_prints_the_shrinkable_line_and_exits_0(tmp_path, gate, capsys):
    root = _repo(tmp_path, gate, {A: "x = 1\n"}, baseline={A: 1})
    assert gate.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "shrinkable" in out and A in out


def test_b_a_baseline_entry_naming_a_missing_file_is_shrinkable_not_red(tmp_path, gate):
    root = _repo(tmp_path, gate, {B: "x = 1\n"}, baseline={A: 2})
    problems, shrink = gate.analyse(root)
    assert problems == [] and A in " ".join(shrink)


# -- (c) a baseline that GREW fails ---------------------------------------------------------

def _two_commits(tmp_path, gate, base_baseline, head_baseline, files):
    root = _repo(tmp_path, gate, files, baseline=base_baseline)
    base = _commit(root, "base")
    _write_baseline(root, gate, head_baseline)
    _git(root, "add", "-A")
    return root, base


def test_c_a_new_entry_added_to_the_baseline_without_a_removal_is_red(tmp_path, gate):
    root, base = _two_commits(tmp_path, gate, {A: 1}, {A: 1, B: 1}, {A: RAW, B: RAW})
    problems = gate.check(root, base_sha=base)
    assert len(problems) == 1 and "grew" in problems[0] and B in problems[0], problems


def test_c_a_higher_count_than_the_base_is_red(tmp_path, gate):
    root, base = _two_commits(tmp_path, gate, {A: 1}, {A: 2}, {A: RAW + RAW})
    problems = gate.check(root, base_sha=base)
    assert len(problems) == 1 and "grew" in problems[0] and A in problems[0], problems


def test_c_a_shrunk_baseline_is_green(tmp_path, gate):
    root, base = _two_commits(tmp_path, gate, {A: 2, B: 1}, {A: 1}, {A: RAW, B: "x = 1\n"})
    assert gate.check(root, base_sha=base) == []


def test_c_the_same_baseline_is_green(tmp_path, gate):
    root, base = _two_commits(tmp_path, gate, {A: 1}, {A: 1}, {A: RAW})
    assert gate.check(root, base_sha=base) == []


def test_c_a_site_swapped_in_after_a_removal_elsewhere_is_red(tmp_path, gate):
    """Fix one file (remove its entry), add a raw read in another: the new file's entry is new vs the base."""
    root = _repo(tmp_path, gate, {A: RAW, B: "x = 1\n"}, baseline={A: 1})
    base = _commit(root, "base")
    (root / A).write_text("x = 1\n")
    (root / B).write_text(RAW)
    _write_baseline(root, gate, {B: 1})
    _git(root, "add", "-A")
    problems = gate.check(root, base_sha=base)
    assert len(problems) == 1 and "grew" in problems[0] and B in problems[0], problems


def test_c_an_unresolvable_base_is_red_never_a_seed(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: RAW}, baseline={A: 1})
    problems = gate.check(root, base_sha="0" * 40)
    assert problems and "base" in problems[0]


def test_c_a_base_with_no_baseline_file_is_the_seed(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: RAW})
    base = _commit(root, "base")
    _write_baseline(root, gate, {A: 1})
    _git(root, "add", "-A")
    assert gate.check(root, base_sha=base) == []


# -- (d) the pinned resolver files are exempt ----------------------------------------------------

def test_d_the_pinned_resolvers_are_the_four_layers_plus_the_build_engine_plus_the_runtime_layer(gate):
    home = _load(HOME_GATE, "home_path_gate_for_pins")
    four = {layer["path"] for layer in home.RESOLVER_LAYERS}
    assert len(four) == 4
    # #3863 step 2 added the runtime layer (Borges's step-2 rulings on #3863, 2026-10-21)
    # #3863 step 3a added the four standalone hooks that carry an inline verbatim copy; step 3b added baton_gate.py
    hooks = {"src/engram/hooks/claude/" + h for h in (
        "engram-lesson-tripwire-hook.py", "engram-loop-arm-stop-hook.py",
        "engram-stale-tree-warn-hook.py", "engram-time-bar-hook.py")}
    assert set(gate.PINNED_RESOLVERS) == four | {"src/build/engine/flows.py", "src/engram/engram_home_resolver.py", "src/engram/tools/baton_gate.py"} | hooks


@pytest.mark.parametrize("rel", [
    "src/engram/tools/_owner_read.py",
    "src/engram/hooks/claude/_hooklib.py",
    "src/engram/hooks/claude/_prompthooklib.py",
    "src/engram/tools/forum_api.py",
    "src/build/engine/flows.py",
    "src/engram/engram_home_resolver.py",
])
def test_d_a_pinned_resolver_file_is_exempt(tmp_path, gate, rel):
    root = _repo(tmp_path, gate, {rel: RAW + RAW}, baseline={})
    assert gate.check(root) == [], rel
    assert gate.analyse(root)[1] == []


def test_d_a_pinned_file_may_not_be_in_the_baseline(tmp_path, gate):
    rel = gate.PINNED_RESOLVERS[0]
    root = _repo(tmp_path, gate, {rel: RAW}, baseline={rel: 1})
    problems = gate.check(root)
    assert problems and rel in problems[0], problems


def test_d_the_allowlist_is_exactly_the_pinned_set_with_reasons(tmp_path, gate):
    extra = {rel: {"class": "resolver", "reason": "r"} for rel in gate.PINNED_RESOLVERS}
    extra[A] = {"class": "resolver", "reason": "sneaked in"}
    root = _repo(tmp_path, gate, {A: RAW}, baseline={}, allowlist=extra)
    problems = gate.check(root)
    assert any(A in p and "allowlist" in p for p in problems), problems
    bad = {rel: {"class": "resolver", "reason": " "} for rel in gate.PINNED_RESOLVERS}
    root2 = _repo(tmp_path / "two", gate, {B: "x = 1\n"}, baseline={}, allowlist=bad)
    assert any("reason" in p for p in gate.check(root2))
    root3 = _repo(tmp_path / "three", gate, {B: "x = 1\n"}, baseline={}, allowlist={})
    assert any("allowlist" in p for p in gate.check(root3))


# -- (e) tests, docs and markdown are not scanned ------------------------------------------------

@pytest.mark.parametrize("rel", [
    "tests/test_x.py",
    "src/engram/tests/helper.py",
    "src/engram/test_thing.py",
    "src/engram/docs/x.py",
    "tools/test_foo.py",
    "src/engram/skills/x/SKILL.md",
    "src/engram/README.md",
    "tools/notes.txt",
    "src/engram/hooks/x.sh",
])
def test_e_tests_docs_markdown_and_non_py_are_not_scanned(tmp_path, gate, rel):
    root = _repo(tmp_path, gate, {rel: RAW}, baseline={})
    assert gate.check(root) == [], rel


def test_e_an_untracked_file_is_not_scanned(tmp_path, gate):
    root = _repo(tmp_path, gate, {B: "x = 1\n"}, baseline={})
    (root / A).parent.mkdir(parents=True, exist_ok=True)
    (root / A).write_text(RAW)
    assert gate.check(root) == []


def test_e_a_tracked_symlink_is_not_scanned_twice(tmp_path, gate):
    """tools/<x> in this repo is a symlink to src/engram/tools/<x>: counting both would double every site."""
    root = _repo(tmp_path, gate, {A: RAW}, baseline={A: 1})
    link = root / "tools" / "a.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(root / A)
    _git(root, "add", "-A")
    assert gate.check(root) == []


# -- (f) abspath/resolve of the raw value is NOT exempt ------------------------------------------

@pytest.mark.parametrize("line", [
    'os.environ["ENGRAM_HOME"] = os.path.abspath(os.path.expanduser(os.environ.get("ENGRAM_HOME")))',
    'home = os.path.abspath(os.path.expanduser(os.environ.get("ENGRAM_HOME") or ""))',
    'home = Path(os.environ.get("ENGRAM_HOME") or "~/.engram").expanduser().resolve()',
    'home = os.path.realpath(os.getenv("ENGRAM_HOME", "x"))',
    'home = Path(os.environ["ENGRAM_HOME"]).absolute()',
])
def test_f_a_read_that_abspaths_or_resolves_the_value_is_still_red(tmp_path, gate, line):
    root = _repo(tmp_path, gate, {A: f"import os\n{line}\n"}, baseline={})
    problems = gate.check(root)
    assert len(problems) == 1 and f"{A}:2" in problems[0], (line, problems)


# Removed-test: tests/test_raw_home_read_gate_3863.py::test_f_the_real_baseline_carries_the_hooks_that_abspath_the_raw_value — inverted by #3863 step 3a: the four hooks left the baseline (they carry the pinned copy now); the replacement below asserts the new state.
def test_f_the_hooks_that_abspathed_the_raw_value_are_now_pinned_not_baselined(gate):
    """#3863 step 3a moved the four inline-guard hooks from the baseline to the pinned allowlist (each now
    carries the verbatim engram_home() copy, AST-pinned by the parity test)."""
    doc = json.loads((REPO / gate.BASELINE_REL).read_text())
    hooks = "src/engram/hooks/claude/"
    for name in ("engram-lesson-tripwire-hook.py", "engram-loop-arm-stop-hook.py",
                 "engram-stale-tree-warn-hook.py", "engram-time-bar-hook.py"):
        assert hooks + name not in doc["baseline"], name
        assert hooks + name in doc["allowlist"], name


# -- the real tree and the baseline ---------------------------------------------------------------

def test_the_real_tree_is_green_and_the_baseline_is_exact(gate):
    problems, shrink = gate.analyse(REPO)
    assert problems == [], problems
    assert shrink == [], shrink


def test_the_real_baseline_matches_the_tree_counts_exactly(gate):
    counts = gate.violation_counts(REPO)
    base = json.loads((REPO / gate.BASELINE_REL).read_text())["baseline"]
    assert counts == base


def test_the_real_baseline_names_no_pinned_resolver_and_no_test_file(gate):
    base = json.loads((REPO / gate.BASELINE_REL).read_text())["baseline"]
    assert not set(base) & set(gate.PINNED_RESOLVERS)
    assert not [rel for rel in base if "/tests/" in rel or rel.rsplit("/", 1)[-1].startswith("test_")]


def test_the_cli_prints_the_baseline_site_count(gate, capsys):
    assert gate.main(["--root", str(REPO)]) == 0
    out = capsys.readouterr().out
    total = sum(json.loads((REPO / gate.BASELINE_REL).read_text())["baseline"].values())
    assert "ok" in out and f"{total} baselined site(s)" in out, out


def test_the_cli_exits_1_and_says_failed_on_a_new_site(tmp_path, gate, capsys):
    root = _repo(tmp_path, gate, {A: RAW}, baseline={})
    assert gate.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert f"{A}:2" in out and "FAILED" in out


def test_report_lists_every_site_with_a_line_number(gate, capsys):
    assert gate.main(["--root", str(REPO), "--report"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"# \d+ file\(s\), \d+ site\(s\)", out)


def test_seed_refuses_to_overwrite_and_writes_exact_counts(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: RAW + RAW, B: RAW})
    assert gate.main(["--root", str(root), "--seed"]) == 0
    doc = json.loads((root / gate.BASELINE_REL).read_text())
    assert doc["baseline"] == {A: 2, B: 1} and doc["schema"] == 1
    assert set(doc["allowlist"]) == set(gate.PINNED_RESOLVERS)
    assert gate.main(["--root", str(root), "--seed"]) == 1


def test_a_scan_prefixes_declaration_out_of_sync_is_red(tmp_path, gate):
    root = _repo(tmp_path, gate, {A: "x = 1\n"}, baseline={}, prefixes=["src/engram/"])
    assert any("scan_prefixes" in p for p in gate.check(root))


def test_the_gate_is_fast_and_stdlib_only(gate):
    t = time.time()
    gate.check(REPO)
    assert time.time() - t < 5.0
    src = TOOL.read_text()
    assert not re.search(r"^\s*(?:import|from)\s+(?!argparse|json|re|subprocess|sys|pathlib)\w+", src, re.M)


# -- pr-checks, tiers ---------------------------------------------------------------------------

def _step_block(name_fragment):
    text = WORKFLOW.read_text()
    starts = [m.start() for m in re.finditer(r"^      - name: ", text, re.M)]
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        first_line = text[s:text.index("\n", s)]
        if name_fragment in first_line:
            return text[s:e]
    raise AssertionError(f"no pr-checks step named like {name_fragment!r}")


def test_pr_checks_runs_the_gate_with_a_merge_base_and_no_pytest():
    block = _step_block("raw ENGRAM_HOME read")
    assert 'git merge-base "${{ env.BASE_REF }}" HEAD' in block
    assert 'python3 tools/raw_home_read_gate.py --base-sha "$BASE_SHA"' in block
    run = block[block.index("run:"):]
    code_lines = [l for l in run.splitlines() if l.strip() and not l.strip().startswith("#")]
    assert not any("pytest" in l for l in code_lines), "the step must stay a git + regex pass, no pytest"
    assert "macos" not in block.lower()


def test_the_gate_and_its_baseline_are_tiered_dev_with_an_explicit_source():
    flat = []

    def walk(o):
        if isinstance(o, dict):
            if "path" in o and "tier" in o:
                flat.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(TIERS.read_text()))
    entries = {e["path"]: e for e in flat}
    for rel in ("tools/raw_home_read_gate.py", "tools/raw_home_read_baseline.json"):
        assert rel in entries, rel
        assert entries[rel]["tier"] == "dev" and entries[rel]["source"] == rel
