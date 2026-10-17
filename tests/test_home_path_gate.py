"""Contract tests for tools/home_path_gate.py [part of #3863].

Written BEFORE the gate (contract-first, #3045). The contract:

  * a shipped file (tracked, under src/engram, not a test or a doc) that
    contains a hard-coded ENGRAM-home spelling and zero occurrences of the
    token ENGRAM_HOME is a VIOLATION;
  * every spelling in the gate's table has its own planted case: a new file
    carrying it goes RED, and goes green when the file also reads ENGRAM_HOME;
  * the checked-in baseline lists today's violators and may only SHRINK:
      (1) a violator not in the baseline -> red
      (2) a baseline entry that no longer violates -> red ("stale")
      (3) a baseline or allowlist entry naming a missing file -> red
      (4) head baseline entry absent from the base-ref baseline -> red; the
          seed (base has no baseline file) is the only exception
      (5) the allowlist exists and is EMPTY (no placeholder for a resolver)
  * the scanner is live: it finds violators in the real tree, including the 13
    files the issue names, so a pattern that matches nothing cannot pass.

The fixtures use real `git init` scratch repos because the gate lists files
with `git ls-files`.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "home_path_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("home_path_gate", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# name -> (extension, [planted lines]). Every spelling in the gate's table
# must appear here (test_every_table_spelling_has_a_planted_case).
PLANTS = {
    "path_home_div": (".py", ['p = Path.home() / ".engram" / "config.json"']),
    "path_home_joinpath": (".py", ['p = Path.home().joinpath(".engram", "db-backup")']),
    "path_tilde_literal": (".py", ['p = Path("~/.engram").expanduser() / "x.sock"']),
    "path_tilde_div": (".py", ['p = Path("~").expanduser() / ".engram" / "x"']),
    "os_expanduser_tilde_engram": (".py", ['p = os.path.expanduser("~/.engram/engramd.pid")']),
    "expanduser_tilde_then_engram": (
        ".py",
        [
            'p = os.path.join(os.path.expanduser("~"), ".engram")',
            'p = os.path.expanduser("~") + "/.engram/x"',
        ],
    ),
    "path_home_join_arg": (".py", ['p = os.path.join(str(Path.home()), ".engram", "x")']),
    "environ_home_index": (".py", ['p = os.path.join(os.environ["HOME"], ".engram")']),
    "environ_home_get": (".py", ['p = Path(os.environ.get("HOME", "/")) / ".engram"']),
    "getenv_home": (".py", ['p = os.path.join(os.getenv("HOME"), ".engram")']),
    "tilde_slash": (
        ".md",
        [
            "Run: sqlite3 ~/.engram/knowledge.db",
            "Read `~/.engram/config.json` first.",
            "Backups land in ~/.engram/db-backup/ daily.",
            "The socket is ~/.engram/engramd.sock.",
            "rm -f ~/.engram/engramd.pid",
        ],
    ),
    "shell_home_var": (
        ".sh",
        [
            'DB="$HOME/.engram/knowledge.db"',
            'F="${HOME}/.engram/loop-mode.json"',
            'cat "$HOME"/.engram/config.json',
        ],
    ),
    "abs_user_home": (
        ".json",
        [
            '{"db": "/home/agent-alice/.engram/knowledge.db"}',
            '{"db": "/Users/someone/.engram/config.json"}',
        ],
    ),
    "quoted_segment": (".py", ['p = os.path.join(seat_home, ".engram", "health-snapshots")']),
    # `.engram` as a folder name directly after a slash or a quote/backtick:
    # the eight forms no other row sees (found by the colleague review of #3870).
    "engram_after_slash_or_quote": (
        ".py",
        [
            'p = f"{Path.home()}/.engram/x"',
            'p = f"{home}/.engram/x"',
            "p = h + '/.engram/x'",
            "p = os.path.join(h, '.engram/knowledge.db')",
            "p = '~' + '/.engram'",
            "DB=$(echo ~)/.engram/knowledge.db",
            "DB=/home/$USER/.engram/knowledge.db",
            "const p = process.env.HOME + '/.engram'",
        ],
    ),
}

# Text that must NOT count as an ENGRAM-home spelling (measured false-positive
# classes in the real tree: other dirs that merely start with ".engram", python
# attribute names, a launchd label, an MCP tool-name attribute).
NEGATIVES = [
    'p = Path.home() / ".engram-gemini" / "knowledge.db"',
    "x = args.engram_home",
    "label = 'com.engram.viz-server'",
    "server.engram_query(payload)",
    "tmp = Path.home() / '.engram_sandbox'",
    "ls ~/.engram-venv",
    "x = '.engram_home'",
    "x = '/.engram-gemini/db'",
    "import engram",
    "name = 'engram'",
]


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )


def _repo(tmp_path, files=None, baseline=None, allowlist=None):
    """A scratch git repo. files: {relpath: text}; baseline/allowlist: lists
    (None = no baseline file at all)."""
    _git(tmp_path, "init", "-q")
    for rel, text in (files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if baseline is not None:
        _write_baseline(tmp_path, baseline, allowlist or [])
    _git(tmp_path, "add", "-A")
    return tmp_path


def _write_baseline(root, baseline, allowlist=()):
    mod = _load()
    p = root / mod.BASELINE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"schema": 1, "baseline": sorted(baseline), "allowlist": sorted(allowlist)}),
        encoding="utf-8",
    )


def _commit(root, msg="c"):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


PLANT_CASES = [(n, ext, line) for n, (ext, lines) in PLANTS.items() for line in lines]


PLANT_IDS = [f"{c[0]}-{i}" for i, c in enumerate(PLANT_CASES)]


# -- the table and its planted cases ---------------------------------------

def test_every_table_spelling_has_a_planted_case():
    mod = _load()
    names = [n for n, _ in mod.SPELLINGS]
    assert len(names) == len(set(names)), "duplicate spelling name"
    assert set(names) <= set(PLANTS), f"no planted case for: {set(names) - set(PLANTS)}"


@pytest.mark.parametrize("name,ext,line", PLANT_CASES, ids=PLANT_IDS)
def test_planted_spelling_is_red_by_its_own_name(tmp_path, name, ext, line):
    mod = _load()
    rel = f"src/engram/tools/new_tool{ext}"
    root = _repo(tmp_path, {rel: line + "\n"}, baseline=[])
    v = mod.violations(root)
    assert rel in v, f"{name}: planted line was not flagged: {line!r}"
    assert name in v[rel], f"{name}: flagged, but not by its own table row: {sorted(v[rel])}"
    problems = mod.check(root)
    assert any(rel in p for p in problems), problems


@pytest.mark.parametrize("name,ext,line", PLANT_CASES, ids=PLANT_IDS)
def test_planted_spelling_is_green_when_file_reads_engram_home(tmp_path, name, ext, line):
    mod = _load()
    rel = f"src/engram/tools/new_tool{ext}"
    root = _repo(tmp_path, {rel: line + "\n# reads ENGRAM_HOME first\n"}, baseline=[])
    assert rel not in mod.violations(root)
    assert mod.check(root) == []


@pytest.mark.parametrize("line", NEGATIVES)
def test_non_home_spellings_are_not_flagged(tmp_path, line):
    mod = _load()
    rel = "src/engram/tools/other.py"
    root = _repo(tmp_path, {rel: line + "\n"}, baseline=[])
    assert mod.violations(root) == {}
    assert mod.check(root) == []


# -- scope ------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        "src/engram/tools/test_thing.py",
        "src/engram/tests/helper.py",
        "src/engram/docs/guide.md",
        "docs/guide.md",
        "tests/test_x.py",
        "tools/some_tool.py",
        ".github/workflows/x.yml",
    ],
)
def test_files_outside_the_shipped_scope_are_not_scanned(tmp_path, rel):
    mod = _load()
    root = _repo(tmp_path, {rel: 'p = Path.home() / ".engram"\n'}, baseline=[])
    assert mod.violations(root) == {}
    assert mod.check(root) == []


def test_untracked_file_is_not_scanned(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {}, baseline=[])
    p = root / "src/engram/tools/x.py"
    p.parent.mkdir(parents=True)
    p.write_text('p = Path.home() / ".engram"\n')
    assert mod.violations(root) == {}


def test_spelling_in_a_skill_markdown_is_scanned(tmp_path):
    mod = _load()
    rel = "src/engram/skills/claude/engram-x/SKILL.md"
    root = _repo(tmp_path, {rel: "run `~/.engram/venv/bin/python`\n"}, baseline=[])
    assert rel in mod.violations(root)


# -- baseline rules ---------------------------------------------------------

def test_rule1_violator_missing_from_baseline_is_red(tmp_path):
    mod = _load()
    root = _repo(
        tmp_path,
        {"src/engram/tools/a.py": 'p = Path.home() / ".engram"\n'},
        baseline=[],
    )
    problems = mod.check(root)
    assert len(problems) == 1 and "src/engram/tools/a.py" in problems[0]


def test_rule1_violator_in_baseline_is_green(tmp_path):
    mod = _load()
    root = _repo(
        tmp_path,
        {"src/engram/tools/a.py": 'p = Path.home() / ".engram"\n'},
        baseline=["src/engram/tools/a.py"],
    )
    assert mod.check(root) == []


def test_rule2_stale_baseline_entry_is_red(tmp_path):
    mod = _load()
    root = _repo(
        tmp_path,
        {"src/engram/tools/a.py": "import os\nH = os.environ['ENGRAM_HOME']\n"},
        baseline=["src/engram/tools/a.py"],
    )
    problems = mod.check(root)
    assert len(problems) == 1
    assert "stale" in problems[0] and "src/engram/tools/a.py" in problems[0]


def test_rule3_baseline_entry_naming_missing_file_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/x.py": "pass\n"}, baseline=["src/engram/tools/gone.py"])
    problems = mod.check(root)
    assert len(problems) == 1
    assert "dangling" in problems[0] and "src/engram/tools/gone.py" in problems[0]


def test_rule5_allowlist_entry_naming_missing_file_is_red(tmp_path):
    mod = _load()
    root = _repo(
        tmp_path, {"src/engram/x.py": "pass\n"}, baseline=[], allowlist=["src/engram/resolver.py"]
    )
    problems = mod.check(root)
    assert len(problems) == 1
    assert "dangling" in problems[0] and "allowlist" in problems[0]


def test_allowlist_entry_exempts_an_existing_violating_file(tmp_path):
    mod = _load()
    rel = "src/engram/tools/resolver.py"
    root = _repo(tmp_path, {rel: 'p = Path.home() / ".engram"\n'}, baseline=[], allowlist=[rel])
    assert mod.check(root) == []


def test_missing_baseline_file_is_red_without_a_base_ref(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/x.py": "pass\n"}, baseline=None)
    problems = mod.check(root)
    assert problems and "baseline" in problems[0]


def test_malformed_baseline_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/x.py": "pass\n"}, baseline=[])
    (root / mod.BASELINE_REL).write_text("{not json")
    assert mod.check(root)


# -- rule 4: the baseline cannot grow ---------------------------------------

def _two_commit_repo(tmp_path, base_baseline, head_baseline, head_files=None):
    """Base commit with base_baseline (None = no baseline file); then a head
    working tree with head_baseline. Returns (root, base_sha)."""
    files = {
        "src/engram/tools/a.py": 'p = Path.home() / ".engram"\n',
        "src/engram/tools/b.py": 'p = Path.home() / ".engram"\n',
    }
    root = _repo(tmp_path, files, baseline=base_baseline)
    base = _commit(root, "base")
    for rel, text in (head_files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _write_baseline(root, head_baseline)
    _git(root, "add", "-A")
    return root, base


def test_rule4_baseline_growth_is_red(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(
        tmp_path,
        base_baseline=["src/engram/tools/a.py"],
        head_baseline=["src/engram/tools/a.py", "src/engram/tools/b.py"],
    )
    problems = mod.check(root, base_sha=base)
    assert len(problems) == 1
    assert "grew" in problems[0] and "src/engram/tools/b.py" in problems[0]


def test_rule4_unchanged_baseline_is_green(tmp_path):
    mod = _load()
    both = ["src/engram/tools/a.py", "src/engram/tools/b.py"]
    root, base = _two_commit_repo(tmp_path, base_baseline=both, head_baseline=both)
    assert mod.check(root, base_sha=base) == []


def test_rule4_shrinking_baseline_is_green(tmp_path):
    mod = _load()
    both = ["src/engram/tools/a.py", "src/engram/tools/b.py"]
    root, base = _two_commit_repo(
        tmp_path,
        base_baseline=both,
        head_baseline=["src/engram/tools/a.py"],
        head_files={"src/engram/tools/b.py": "H = 'ENGRAM_HOME'\n"},
    )
    assert mod.check(root, base_sha=base) == []


def test_rule4_seed_is_allowed_only_when_base_has_no_baseline_file(tmp_path):
    mod = _load()
    both = ["src/engram/tools/a.py", "src/engram/tools/b.py"]
    root, base = _two_commit_repo(tmp_path, base_baseline=None, head_baseline=both)
    assert mod.check(root, base_sha=base) == []


def test_rule4_unresolvable_base_ref_is_red_not_a_seed(tmp_path):
    mod = _load()
    both = ["src/engram/tools/a.py", "src/engram/tools/b.py"]
    root, _ = _two_commit_repo(tmp_path, base_baseline=both, head_baseline=both)
    problems = mod.check(root, base_sha="0" * 40)
    assert problems and "base" in problems[0]


def test_rule4_unreadable_base_baseline_is_red_not_a_seed(tmp_path):
    mod = _load()
    both = ["src/engram/tools/a.py", "src/engram/tools/b.py"]
    root, base = _two_commit_repo(tmp_path, base_baseline=both, head_baseline=both)
    # a base commit whose baseline file is not valid JSON
    (root / mod.BASELINE_REL).write_text("{not json")
    bad = _commit(root, "bad")
    _write_baseline(root, both)
    _git(root, "add", "-A")
    problems = mod.check(root, base_sha=bad)
    assert problems


def test_rule4_does_not_let_allowlist_growth_hide_in_baseline_check(tmp_path):
    """An allowlist entry added on the head is allowed (the resolver PR adds
    its own), and is not mistaken for baseline growth."""
    mod = _load()
    root = _repo(
        tmp_path,
        {"src/engram/tools/r.py": 'p = Path.home() / ".engram"\n'},
        baseline=[],
    )
    base = _commit(root, "base")
    _write_baseline(root, [], allowlist=["src/engram/tools/r.py"])
    _git(root, "add", "-A")
    assert mod.check(root, base_sha=base) == []


# -- the real tree ----------------------------------------------------------

THE_13 = [
    "src/engram/engram_walguard.py",
    "src/engram/skills/claude/engram-health-exam/SKILL.md",
    "src/engram/skills/claude/engram-sleep/SKILL.md",
    "src/engram/skills/claude/engram-upgrade/SKILL.md",
    "src/engram/tools/backup_knowledge_db.py",
    "src/engram/tools/cohort_dispatch.py",
    "src/engram/tools/dream_lottery.py",
    "src/engram/tools/migrate_person_nodes.py",
    "src/engram/tools/migrate_prediction_pairing_edges.py",
    "src/engram/tools/migrate_stranded_supported_by.py",
    "src/engram/tools/monitor-supervisor.py",
    "src/engram/tools/tripwire.py",
    "src/engram/viz/server.py",
]


# Six of the issue's 13 were fixed after this gate was written, so they are no
# longer violators and left the baseline; the other 7 are still checked as
# violators AND baselined. Two read ENGRAM_HOME since #3869; three more in PR 2
# of #3863 (the walguard remedy text, the health-exam and upgrade skills); one
# since #3872 (the knowledge.db backup tool).
FIXED_SINCE_THE_13 = {
    "src/engram/skills/claude/engram-sleep/SKILL.md": "reads ENGRAM_HOME since #3869",
    "src/engram/tools/cohort_dispatch.py": "reads ENGRAM_HOME since #3869",
    "src/engram/engram_walguard.py": "remedy text uses ${ENGRAM_HOME:-...} (PR 2 of #3863)",
    "src/engram/skills/claude/engram-health-exam/SKILL.md": "uses ${ENGRAM_HOME:-...} (PR 2 of #3863)",
    "src/engram/skills/claude/engram-upgrade/SKILL.md": "uses ${ENGRAM_HOME:-...} (PR 2 of #3863)",
    "src/engram/tools/backup_knowledge_db.py": "reads ENGRAM_HOME since #3872",
}


def test_real_tree_is_green_with_the_checked_in_baseline():
    mod = _load()
    assert mod.check(REPO) == []


def test_scanner_is_live_on_the_real_tree():
    """Vacuous-green guard: a table that matches nothing must not pass."""
    mod = _load()
    v = mod.violations(REPO)
    assert len(v) >= len(THE_13) - len(FIXED_SINCE_THE_13)


def test_the_13_named_files_are_all_violators_and_all_baselined():
    mod = _load()
    v = mod.violations(REPO)
    baseline, _ = mod.load_baseline(REPO)
    assert set(FIXED_SINCE_THE_13) <= set(THE_13)
    for f in FIXED_SINCE_THE_13:
        assert f not in baseline, f"fixed file must stay out of the baseline: {f}"
    still = [f for f in THE_13 if f not in FIXED_SINCE_THE_13]
    assert len(still) == 7
    for f in still:
        assert f in v, f"positive control not detected: {f}"
        assert f in baseline, f"positive control not in baseline: {f}"


def test_real_baseline_is_sorted_and_duplicate_free():
    mod = _load()
    raw = json.loads((REPO / mod.BASELINE_REL).read_text())
    assert raw["baseline"] == sorted(set(raw["baseline"]))


def test_real_allowlist_is_empty():
    """No placeholder for the future resolver (Borges, #3863): the PR that
    creates the resolver adds its own entry in the same commit."""
    mod = _load()
    _, allow = mod.load_baseline(REPO)
    assert allow == set()


def test_cli_exit_codes(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/tools/a.py": 'p = Path.home() / ".engram"\n'}, baseline=[])
    assert mod.main(["--root", str(root)]) == 1
    _write_baseline(root, ["src/engram/tools/a.py"])
    assert mod.main(["--root", str(root)]) == 0


# -- the CI step is pinned (colleague review of #3870: four mutants of the
# step stayed green, so a later workflow edit could switch rule 4 off) --------

WORKFLOW = REPO / ".github" / "workflows" / "pr-checks.yml"


def _gate_step_text():
    """The run lines of the pr-checks step named 'home-path gate'. Plain text,
    not a YAML parse: the step runs in a stdlib + pytest carve-out (no PyYAML).
    Comment lines are dropped so a commented-out call cannot satisfy a test."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("- name: home-path gate"))
    out = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln.lstrip().startswith("- name:") or ln.lstrip().startswith("# ─"):
            break
        out.append(ln)
    return "\n".join(ln for ln in out if not ln.lstrip().startswith("#"))


def test_ci_step_runs_the_gate_with_a_merge_base_sha():
    import re

    step = _gate_step_text()
    assert re.search(
        r'^\s*BASE_SHA=\$\(git merge-base "origin/[^"]+" HEAD\)\s*$', step, re.M
    ), f"BASE_SHA must come from `git merge-base origin/<base> HEAD`:\n{step}"
    call = re.search(r"python3 tools/home_path_gate\.py[^\n]*", step)
    assert call, f"the step does not run tools/home_path_gate.py:\n{step}"
    assert '--base-sha "$BASE_SHA"' in call.group(0), call.group(0)


def test_ci_step_runs_the_gates_own_tests():
    assert "pytest tests/test_home_path_gate.py" in _gate_step_text()
