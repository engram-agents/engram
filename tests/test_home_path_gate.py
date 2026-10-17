"""Contract tests for tools/home_path_gate.py [part of #3863].

The gate is LINE-level (R5, the design seat's ruling on #3863), with a counted
baseline (schema 2) and a classed allowlist (R4). The contract, written before
the line-level implementation (contract-first, #3045):

  C1 a line carrying any SPELLINGS match is clean iff that same line contains
     the substring ENGRAM_HOME, or a call to the shared resolver
     (`engram_home(`), or its file is allowlisted. ENGRAM_HOME on ANOTHER line
     of the file no longer rescues a bare spelling line.
  C2 baseline schema 2: {"schema": 2, "baseline": {file: violating-line
     count}, "allowlist": {file: {"class": "resolver"|"foreign-home",
     "reason": non-empty}}}. Rules: (a) violator absent from the baseline,
     (b) count above baseline, (c) count below baseline ("lower it to N") or a
     zero-count entry ("remove it"), (d) dangling entry, (e) with --base-sha:
     growth against the base (a schema-1 base is the transition and is read as
     a seed), (f) allowlist shape rules, (g) schema 1 on the head is red.
  C6 the verdicts are asserted on PLANTED scratch git repos, never on counts
     read from the real tree; one test runs the real tree and asserts only
     that the ratchet is green.

The fixtures use real `git init` scratch repos because the gate lists files
with `git ls-files`.
"""

import importlib.util
import json
import re
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


def _repo(tmp_path, files=None, baseline=None, allowlist=None, prefixes=None):
    """A scratch git repo. files: {relpath: text}. baseline: a dict
    {file: count} (schema 2), a list (schema 1), or None (no baseline file at
    all). allowlist: {file: {"class":..., "reason":...}} (schema 2 only)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    for rel, text in (files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if baseline is not None:
        _write_baseline(tmp_path, baseline, allowlist, prefixes)
    _git(tmp_path, "add", "-A")
    return tmp_path


def _write_baseline(root, baseline, allowlist=None, prefixes=None):
    """prefixes: None = the gate's own SCAN_PREFIXES, False = omit the key,
    else the list to write."""
    mod = _load()
    p = root / mod.BASELINE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(baseline, list):
        doc = {"schema": 1, "baseline": sorted(baseline), "allowlist": []}
    else:
        doc = {"schema": 2, "baseline": dict(baseline), "allowlist": dict(allowlist or {})}
        if prefixes is not False:
            doc["scan_prefixes"] = list(mod.SCAN_PREFIXES if prefixes is None else prefixes)
    p.write_text(json.dumps(doc), encoding="utf-8")


def _commit(root, msg="c"):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _fd(reason="finds another seat's home"):
    return {"class": "foreign-home", "reason": reason}


def _res(reason="a resolver layer"):
    return {"class": "resolver", "reason": reason}


def _layer(path, name="tools", pinned=True):
    return {"path": path, "layer": name, "parity_pinned": pinned}


BARE = 'p = Path.home() / ".engram"\n'
A = "src/engram/tools/a.py"
B = "src/engram/tools/b.py"
C = "src/engram/tools/c.py"
# TWO: violating lines 2 and 4, two clean lines. THREE: lines 2, 4, 5.
TWO = "import os\n" + BARE + "x = 1\n" + 'q = Path.home() / ".engram" / "y"\n'
THREE = TWO + BARE


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
    root = _repo(tmp_path, {rel: line + "\n"}, baseline={})
    v = mod.violations(root)
    assert rel in v, f"{name}: planted line was not flagged: {line!r}"
    assert name in v[rel], f"{name}: flagged, but not by its own table row: {sorted(v[rel])}"
    problems = mod.check(root)
    assert any(rel in p for p in problems), problems


# -- C1: the line is the unit ------------------------------------------------

@pytest.mark.parametrize("name,ext,line", PLANT_CASES, ids=PLANT_IDS)
def test_planted_spelling_is_green_when_its_own_line_names_engram_home(tmp_path, name, ext, line):
    mod = _load()
    rel = f"src/engram/tools/new_tool{ext}"
    root = _repo(tmp_path, {rel: line + "  # ENGRAM_HOME\n"}, baseline={})
    assert rel not in mod.violations(root)
    assert mod.check(root) == []


@pytest.mark.parametrize("name,ext,line", PLANT_CASES, ids=PLANT_IDS)
def test_planted_spelling_stays_red_when_engram_home_is_on_another_line(tmp_path, name, ext, line):
    """The new discriminating case: the token elsewhere in the file does not
    rescue a bare spelling line (the old gate called this file clean)."""
    mod = _load()
    rel = f"src/engram/tools/new_tool{ext}"
    root = _repo(tmp_path, {rel: line + "\n# reads ENGRAM_HOME first\n"}, baseline={})
    assert rel in mod.violations(root), f"{name}: another line's token rescued {line!r}"
    assert any(rel in p for p in mod.check(root))


@pytest.mark.parametrize("name,ext,line", PLANT_CASES, ids=PLANT_IDS)
def test_planted_spelling_is_green_when_its_line_calls_the_resolver(tmp_path, name, ext, line):
    mod = _load()
    rel = f"src/engram/tools/new_tool{ext}"
    root = _repo(tmp_path, {rel: line + "  # engram_home()\n"}, baseline={})
    assert rel not in mod.violations(root)
    assert mod.check(root) == []


BARE_NAME_LINES = [
    'p = engram_home or Path.home() / ".engram"',
    'p = args.engram_home or Path.home() / ".engram"',
    'p = my_engram_home() or Path.home() / ".engram"',
]


@pytest.mark.parametrize("line", BARE_NAME_LINES)
def test_resolver_clause_needs_a_call_not_a_bare_name(tmp_path, line):
    """`engram_home` mentioned without a call (a variable, an attribute, a
    longer function name) does not clean a spelling line."""
    mod = _load()
    rel = "src/engram/tools/new_tool.py"
    root = _repo(tmp_path, {rel: line + "\n"}, baseline={})
    assert rel in mod.violations(root), line


def test_resolver_call_on_a_qualified_name_is_clean(tmp_path):
    mod = _load()
    rel = "src/engram/tools/new_tool.py"
    root = _repo(
        tmp_path, {rel: 'p = _owner_read.engram_home() or Path.home() / ".engram"\n'}, baseline={}
    )
    assert mod.violations(root) == {}


# -- C1 is per PHYSICAL line: nothing but the line itself rescues (designer's
# ruling; no parser). The split idiom is a RED, on purpose: 6b replaces it with
# a resolver call. (name, relpath, text, verdict).
OWN_LINE_CASES = [
    # the token on the previous line of the SAME statement does not rescue
    (
        "same_statement_token_on_previous_line",
        "src/engram/tools/p.py",
        'ENGRAM_HOME = (os.environ.get("ENGRAM_HOME")\n'
        '               or os.path.expanduser("~/.engram"))\n',
        "red",
    ),
    (
        "same_statement_token_on_later_line",
        "src/engram/tools/p.py",
        'p = (os.path.expanduser("~/.engram")\n     if not os.environ.get("ENGRAM_HOME") else None)\n',
        "red",
    ),
    # the token one line above, in a different statement
    (
        "different_statement_above",
        "src/engram/tools/p.py",
        'h = os.environ.get("ENGRAM_HOME")\np = os.path.expanduser("~/.engram")\n',
        "red",
    ),
    # the token only in a comment on the line above
    (
        "comment_above",
        "src/engram/tools/p.py",
        '# ENGRAM_HOME\np = os.path.expanduser("~/.engram")\n',
        "red",
    ),
    # a docstring: the token on another docstring line does not rescue
    (
        "docstring_other_line",
        "src/engram/tools/p.py",
        '"""Tool.\n\nENGRAM_HOME is read elsewhere.\nThe default is ~/.engram/x.\n"""\n',
        "red",
    ),
    (
        "docstring_same_line",
        "src/engram/tools/p.py",
        '"""Tool.\n\nThe default is ~/.engram/x ($ENGRAM_HOME overrides).\n"""\n',
        "green",
    ),
    # shell: a backslash-continued command does not join physical lines
    (
        "sh_continued_command_token_first",
        "src/engram/tools/r.sh",
        'ENGRAM_HOME="${ENGRAM_HOME:-x}" \\\n  run ~/.engram/y\n',
        "red",
    ),
    # the resolver call on the spelling line, and nowhere else, is the rescue
    (
        "resolver_call_same_line",
        "src/engram/tools/p.py",
        'p = engram_home() or os.path.expanduser("~/.engram")\n',
        "green",
    ),
    (
        "resolver_call_on_the_line_above",
        "src/engram/tools/p.py",
        'h = engram_home()\np = os.path.expanduser("~/.engram")\n',
        "red",
    ),
]


@pytest.mark.parametrize(
    "name,rel,text,verdict", OWN_LINE_CASES, ids=[c[0] for c in OWN_LINE_CASES]
)
def test_only_the_physical_line_rescues(tmp_path, name, rel, text, verdict):
    mod = _load()
    root = _repo(tmp_path, {rel: text}, baseline={})
    v = mod.violations(root)
    if verdict == "green":
        assert v == {}, f"{name}: expected clean, got {mod.violating_lines(root)}"
    else:
        assert rel in v, f"{name}: expected a violating line, the file was called clean"


def test_resolver_tokens_is_the_one_place_resolver_names_live():
    """RESOLVER_TOKENS is the single list of resolver-call regexes; today
    exactly the `engram_home(` call. New resolver names are added only there."""
    mod = _load()
    assert isinstance(mod.RESOLVER_TOKENS, tuple) and len(mod.RESOLVER_TOKENS) == 1
    rx = re.compile(mod.RESOLVER_TOKENS[0])
    assert rx.search("x = engram_home()") and rx.search("_owner_read.engram_home( )")
    assert not rx.search("x = engram_home") and not rx.search("my_engram_home()")


def test_count_is_violating_lines_not_spelling_matches(tmp_path):
    """One line matching two rows (path_home_div + quoted_segment) is ONE line."""
    mod = _load()
    root = _repo(tmp_path, {A: THREE}, baseline={})
    assert mod.violation_counts(root) == {A: 3}
    assert [n for n, _, _ in mod.violating_lines(root)[A]] == [2, 4, 5]


@pytest.mark.parametrize("line", NEGATIVES)
def test_non_home_spellings_are_not_flagged(tmp_path, line):
    mod = _load()
    rel = "src/engram/tools/other.py"
    root = _repo(tmp_path, {rel: line + "\n"}, baseline={})
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
    root = _repo(tmp_path, {rel: BARE}, baseline={})
    assert mod.violations(root) == {}
    assert mod.check(root) == []


# -- scope: src/engram/ and src/build/ ------------------------------------------

BP = "src/build/engine/x.py"
BQ = "src/build/engine/y.py"
OLD = ["src/engram/"]


def test_scan_prefixes_are_the_two_trees():
    mod = _load()
    assert tuple(mod.SCAN_PREFIXES) == ("src/engram/", "src/build/")
    assert not hasattr(mod, "SCAN_PREFIX")


@pytest.mark.parametrize(
    "rel,scanned",
    [
        ("src/build/engine/build.py", True),
        ("src/build/packaging/tiers.json", True),
        ("src/build/tests/helper.py", False),
        ("src/build/docs/guide.md", False),
        ("src/build/engine/test_thing.py", False),
        ("src/forum/server.py", False),
        ("src/other/x.py", False),
    ],
)
def test_src_build_is_scanned_and_the_exclusions_hold(tmp_path, rel, scanned):
    mod = _load()
    root = _repo(tmp_path, {rel: BARE}, baseline={rel: 1} if scanned else {})
    assert (rel in mod.violations(root)) == scanned
    assert mod.in_scope(rel) == scanned
    assert mod.check(root) == []


def test_a_src_build_violation_missing_from_the_baseline_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {BP: TWO}, baseline={})
    assert any(BP in p for p in mod.check(root))


def test_dangling_message_names_every_scanned_prefix(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/x.py": "pass\n"}, baseline={"src/build/gone.py": 1})
    msg = next(p for p in mod.check(root) if "dangling" in p)
    assert "src/engram/" in msg and "src/build/" in msg


def test_untracked_file_is_not_scanned(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {}, baseline={})
    p = root / "src/engram/tools/x.py"
    p.parent.mkdir(parents=True)
    p.write_text(BARE)
    assert mod.violations(root) == {}


def test_spelling_in_a_skill_markdown_is_scanned(tmp_path):
    mod = _load()
    rel = "src/engram/skills/claude/engram-x/SKILL.md"
    root = _repo(tmp_path, {rel: "run `~/.engram/venv/bin/python`\n"}, baseline={})
    assert rel in mod.violations(root)


# -- C2 (a): a violator absent from the baseline ------------------------------

def test_a_violator_missing_from_baseline_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={})
    problems = mod.check(root)
    assert len(problems) == 1 and A in problems[0]


def test_a_violator_in_baseline_at_its_count_is_green(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={A: 2})
    assert mod.check(root) == []


# -- C2 (b): a count above the baseline ---------------------------------------

def test_b_count_above_baseline_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: THREE}, baseline={A: 2})
    problems = mod.check(root)
    assert len(problems) == 1
    assert A in problems[0] and "3" in problems[0] and "2" in problems[0]


def test_known_bound_a_swap_inside_one_file_keeps_the_count_green(tmp_path):
    """Known bound, see docstring: a count baseline cannot tell WHICH lines
    violate. Fixing one violating line and adding a different bare-spelling
    line in the same file keeps the count at 2, so the gate stays green. This
    pins the bound: changing it (e.g. comparing line content) edits this test."""
    mod = _load()
    swapped = (
        "import os\n"
        + 'p = Path.home() / ".engram"  # ENGRAM_HOME\n'  # fixed
        + "x = 1\n"
        + 'q = Path.home() / ".engram" / "y"\n'  # still violating
        + 'r = os.path.expanduser("~/.engram/z")\n'  # new bare spelling elsewhere
    )
    root = _repo(tmp_path, {A: swapped}, baseline={A: 2})
    assert mod.violation_counts(root) == {A: 2}
    assert mod.check(root) == []


def test_b_a_new_violating_line_in_a_baselined_file_is_red(tmp_path):
    """The point of the count: a file already baselined cannot take one more."""
    mod = _load()
    root = _repo(tmp_path, {A: TWO + "# ENGRAM_HOME is read elsewhere\n" + BARE}, baseline={A: 2})
    assert len(mod.check(root)) == 1


# -- C2 (c): the ratchet is exact --------------------------------------------

def test_c_count_below_baseline_is_stale_and_says_the_new_number(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={A: 3})
    problems = mod.check(root)
    assert len(problems) == 1
    assert "stale" in problems[0] and "lower it to 2" in problems[0] and A in problems[0]


def test_c_fixed_file_still_listed_is_stale_remove_it(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: "import os\nH = os.environ['ENGRAM_HOME']\n"}, baseline={A: 1})
    problems = mod.check(root)
    assert len(problems) == 1
    assert "stale" in problems[0] and "remove it" in problems[0] and A in problems[0]


def test_c_zero_count_entry_is_stale_remove_it(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: "x = 1\n"}, baseline={A: 0})
    problems = mod.check(root)
    assert len(problems) == 1 and "remove it" in problems[0]


def test_c_a_line_fixed_by_naming_the_token_on_the_same_line_lowers_the_count(tmp_path):
    mod = _load()
    fixed = "import os\n" + BARE + "x = 1\n" + 'q = Path.home() / ".engram" / "y"  # ENGRAM_HOME\n'
    root = _repo(tmp_path, {A: fixed}, baseline={A: 2})
    problems = mod.check(root)
    assert len(problems) == 1 and "lower it to 1" in problems[0]


# -- C2 (d): dangling entries ------------------------------------------------

def test_d_baseline_entry_naming_missing_file_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/x.py": "pass\n"}, baseline={"src/engram/tools/gone.py": 1})
    problems = mod.check(root)
    assert len(problems) == 1
    assert "dangling" in problems[0] and "src/engram/tools/gone.py" in problems[0]


def test_d_allowlist_entry_naming_missing_file_is_red(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer("src/engram/resolver.py")]
    root = _repo(
        tmp_path, {"src/engram/x.py": "pass\n"}, baseline={},
        allowlist={"src/engram/resolver.py": _res()},
    )
    problems = mod.check(root)
    assert len(problems) == 1
    assert "dangling" in problems[0] and "allowlist" in problems[0]


def test_d_baseline_entry_naming_an_out_of_scope_file_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"tools/x.py": BARE, "src/engram/x.py": "pass\n"}, baseline={"tools/x.py": 1})
    problems = mod.check(root)
    assert len(problems) == 1 and "dangling" in problems[0]


# -- C2 (e): growth against the base commit ---------------------------------

def _two_commit_repo(tmp_path, base_baseline, head_baseline, head_files=None, base_allow=None,
                     head_allow=None, base_files=None, base_prefixes=None, head_prefixes=None):
    """Base commit with base_baseline (None = no baseline file); then a head
    working tree with head_baseline. Returns (root, base_sha)."""
    files = base_files or {A: TWO, B: TWO}
    root = _repo(tmp_path, files, baseline=base_baseline, allowlist=base_allow, prefixes=base_prefixes)
    base = _commit(root, "base")
    for rel, text in (head_files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _write_baseline(root, head_baseline, head_allow, head_prefixes)
    _git(root, "add", "-A")
    return root, base


# -- C2/C3: the scan scope is declared, and entering a prefix seeds once ----------

def _widen(tmp_path, head_baseline, head_files=None, base_prefixes=None, head_prefixes=None,
           base_files=None):
    """Base: only src/engram/ (or base_prefixes); head adds src/build files."""
    return _two_commit_repo(
        tmp_path, base_baseline={}, head_baseline=head_baseline,
        head_files=head_files if head_files is not None else {BP: TWO},
        base_files=base_files if base_files is not None else {A: "x = 1\n"},
        base_prefixes=OLD if base_prefixes is None else base_prefixes,
        head_prefixes=head_prefixes,
    )


def test_widen_i_entering_prefix_seeded_at_its_exact_count_is_green(tmp_path):
    mod = _load()
    root, base = _widen(tmp_path, {BP: 2})
    assert mod.check(root, base_sha=base) == []


def test_widen_ii_entering_prefix_with_a_padded_count_is_red(tmp_path):
    mod = _load()
    root, base = _widen(tmp_path, {BP: 3})
    problems = mod.check(root, base_sha=base)
    assert len(problems) == 1 and "lower it to 2" in problems[0]


def test_widen_ii_entering_prefix_with_a_lowered_count_is_red(tmp_path):
    mod = _load()
    root, base = _widen(tmp_path, {BP: 1})
    problems = mod.check(root, base_sha=base)
    assert len(problems) == 1 and BP in problems[0]


def test_widen_iii_entering_prefix_violator_not_baselined_is_red(tmp_path):
    mod = _load()
    root, base = _widen(tmp_path, {})
    assert any(BP in p for p in mod.check(root, base_sha=base))


def test_widen_iv_after_the_prefix_is_in_the_base_a_new_entry_is_red_as_grew(tmp_path):
    mod = _load()
    root, base = _widen(tmp_path, {BP: 2}, base_prefixes=["src/engram/", "src/build/"])
    problems = mod.check(root, base_sha=base)
    assert len(problems) == 1 and "grew" in problems[0] and BP in problems[0]


def test_widen_iv_after_the_prefix_is_in_the_base_a_higher_count_is_red_as_grew(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(
        tmp_path, base_baseline={BP: 2}, head_baseline={BP: 3}, head_files={BP: THREE},
        base_files={BP: TWO}, base_prefixes=["src/engram/", "src/build/"],
    )
    assert any("grew" in p and BP in p for p in mod.check(root, base_sha=base))


def test_widen_v_a_base_without_scan_prefixes_is_read_as_src_engram_only(tmp_path):
    mod = _load()
    root, base = _widen(tmp_path, {BP: 2}, base_prefixes=False)
    assert mod.check(root, base_sha=base) == []


def test_widen_v_a_base_without_scan_prefixes_gives_no_exemption_to_src_engram(tmp_path):
    mod = _load()
    root, base = _widen(tmp_path, {A: 2}, head_files={A: TWO}, base_prefixes=False)
    assert any("grew" in p and A in p for p in mod.check(root, base_sha=base))


def test_widen_vi_head_scan_prefixes_differing_from_the_gate_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: "x = 1\n"}, baseline={}, prefixes=["src/engram/"])
    problems = mod.check(root)
    assert len(problems) == 1 and "out of sync" in problems[0]


def test_widen_vi_head_baseline_without_scan_prefixes_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: "x = 1\n"}, baseline={}, prefixes=False)
    assert any("out of sync" in p for p in mod.check(root))


def test_widen_vi_the_prefixes_must_match_in_content_not_only_in_length(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: "x = 1\n"}, baseline={}, prefixes=["src/engram/", "src/forum/"])
    assert any("out of sync" in p for p in mod.check(root))


def test_widen_vii_a_new_violator_under_the_old_prefix_is_still_red(tmp_path):
    """Only an ENTERING prefix is exempt from the growth rule."""
    mod = _load()
    root, base = _widen(tmp_path, {BP: 2, A: 2}, head_files={BP: TWO, A: TWO})
    problems = mod.check(root, base_sha=base)
    assert len(problems) == 1 and "grew" in problems[0] and A in problems[0]


def test_e_new_baseline_entry_is_red_as_grew(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(tmp_path, base_baseline={A: 2}, head_baseline={A: 2, B: 2})
    problems = mod.check(root, base_sha=base)
    assert len(problems) == 1
    assert "grew" in problems[0] and B in problems[0]


def test_e_a_higher_count_than_the_base_is_red_as_grew(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(
        tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 3, B: 2}, head_files={A: THREE}
    )
    problems = mod.check(root, base_sha=base)
    assert len(problems) == 1
    assert "grew" in problems[0] and A in problems[0]


def test_e_unchanged_baseline_is_green(tmp_path):
    mod = _load()
    both = {A: 2, B: 2}
    root, base = _two_commit_repo(tmp_path, base_baseline=both, head_baseline=both)
    assert mod.check(root, base_sha=base) == []


def test_e_shrinking_baseline_is_green(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(
        tmp_path,
        base_baseline={A: 2, B: 2},
        head_baseline={A: 2},
        head_files={B: "H = 'ENGRAM_HOME'\n"},
    )
    assert mod.check(root, base_sha=base) == []


def test_e_a_lower_count_is_green(tmp_path):
    mod = _load()
    one = "import os\n" + BARE
    root, base = _two_commit_repo(
        tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 1, B: 2}, head_files={A: one}
    )
    assert mod.check(root, base_sha=base) == []


def test_e_seed_is_allowed_only_when_base_has_no_baseline_file(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(tmp_path, base_baseline=None, head_baseline={A: 2, B: 2})
    assert mod.check(root, base_sha=base) == []


def test_e_unresolvable_base_ref_is_red_not_a_seed(tmp_path):
    mod = _load()
    root, _ = _two_commit_repo(tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 2, B: 2})
    problems = mod.check(root, base_sha="0" * 40)
    assert problems and "base" in problems[0]


def test_e_unreadable_base_baseline_is_red_not_a_seed(tmp_path):
    mod = _load()
    both = {A: 2, B: 2}
    root, base = _two_commit_repo(tmp_path, base_baseline=both, head_baseline=both)
    (root / mod.BASELINE_REL).write_text("{not json")
    bad = _commit(root, "bad")
    _write_baseline(root, both)
    _git(root, "add", "-A")
    assert mod.check(root, base_sha=bad)


def test_e_schema1_base_lets_a_listed_file_take_its_true_count_once(tmp_path):
    """The transition: a base baseline in schema 1 (a list of file names) is
    read as {file: unknown}, so the first count for a listed file is free."""
    mod = _load()
    root, base = _two_commit_repo(tmp_path, base_baseline=[A], head_baseline={A: 2}, base_files={A: TWO})
    assert mod.check(root, base_sha=base) == []


def test_e_schema1_base_is_a_seed_for_the_line_level_baseline(tmp_path):
    """The file-level base list cannot name the files that only the line-level
    rule sees, so under a schema-1 base the head baseline is the seed of the
    line-level ratchet: files absent from the old list are accepted once."""
    mod = _load()
    root, base = _two_commit_repo(tmp_path, base_baseline=[A], head_baseline={A: 2, B: 2})
    assert mod.check(root, base_sha=base) == []


def test_e_after_the_transition_growth_is_red_again(tmp_path):
    """The schema-1 leniency is the transition only: once the base is schema 2
    a new entry or a higher count is red."""
    mod = _load()
    root, base = _two_commit_repo(tmp_path, base_baseline={A: 2}, head_baseline={A: 2, B: 2})
    assert any("grew" in p for p in mod.check(root, base_sha=base))


def test_e_schema1_base_still_checks_the_head_against_the_tree(tmp_path):
    """The transition skips the growth comparison, not rules (a)-(d)."""
    mod = _load()
    root, base = _two_commit_repo(tmp_path, base_baseline=[A], head_baseline={A: 9})
    problems = mod.check(root, base_sha=base)
    assert any("stale" in p for p in problems)


# -- C2 (f): the allowlist ------------------------------------------------------

def test_f_foreign_home_entry_with_a_reason_exempts_its_file(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: THREE}, baseline={}, allowlist={A: _fd()})
    assert mod.check(root) == []


def test_f_resolver_entry_for_a_declared_layer_is_green_with_its_lines_baselined(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: THREE}, baseline={A: 3}, allowlist={A: _res()})
    assert mod.check(root) == []


# -- a resolver entry DECLARES a layer; it does not exempt the file -------------

def test_f_resolver_file_with_other_violating_lines_is_green_at_its_exact_count(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: TWO}, baseline={A: 2}, allowlist={A: _res()})
    assert mod.check(root) == []


def test_f_resolver_file_does_not_hide_its_other_violating_lines(tmp_path):
    """The same file with its baseline entry missing is RED: the lines count."""
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: TWO}, baseline={}, allowlist={A: _res()})
    problems = mod.check(root)
    assert len(problems) == 1 and A in problems[0] and "2 line" in problems[0]


def test_f_resolver_file_baselined_too_high_is_stale_lower_it(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: TWO}, baseline={A: 3}, allowlist={A: _res()})
    problems = mod.check(root)
    assert len(problems) == 1 and "lower it to 2" in problems[0]


def test_f_resolver_file_with_a_new_violating_line_is_red(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: THREE}, baseline={A: 2}, allowlist={A: _res()})
    assert len(mod.check(root)) == 1


def test_f_resolver_file_fully_fixed_but_still_baselined_is_stale_remove_it(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: "x = 1\n"}, baseline={A: 2}, allowlist={A: _res()})
    problems = mod.check(root)
    assert len(problems) == 1 and "remove it" in problems[0]


def test_f_resolver_file_may_be_in_both_lists_but_not_the_exempting_classes(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    ok = _repo(tmp_path / "res", {A: TWO}, baseline={A: 2}, allowlist={A: _res()})
    assert mod.check(ok) == []
    for cls_entry in (_fd(), _sc()):
        root = _repo(tmp_path / cls_entry["class"], {A: TWO}, baseline={A: 2}, allowlist={A: cls_entry})
        assert any("both" in p and A in p for p in mod.check(root))


@pytest.mark.parametrize(
    "cls",
    ["", "other", "Resolver", None, "foreign_home", "self-contained", "self_contained_no_lib"],
)
def test_f_class_outside_the_three_is_red(tmp_path, cls):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={}, allowlist={A: {"class": cls, "reason": "r"}})
    problems = mod.check(root)
    assert problems and any("class" in p and "self-contained-no-lib" in p for p in problems)


def _sc(reason="a hook that by contract cannot import a resolver"):
    return {"class": "self-contained-no-lib", "reason": reason}


def test_f_the_classes_are_exactly_the_three():
    mod = _load()
    assert mod.CLASSES == ("resolver", "foreign-home", "self-contained-no-lib")


def test_f_self_contained_no_lib_entry_with_a_reason_is_green_without_a_declared_layer(tmp_path):
    """Only `resolver` is tied to RESOLVER_LAYERS; this class needs none."""
    mod = _load()
    assert A not in {l["path"] for l in mod.RESOLVER_LAYERS}
    root = _repo(tmp_path, {A: THREE}, baseline={}, allowlist={A: _sc()})
    assert mod.check(root) == []


def test_f_self_contained_no_lib_has_no_cap(tmp_path):
    mod = _load()
    rels = [f"src/engram/hooks/h{i}.py" for i in range(len(mod.RESOLVER_LAYERS) + 3)]
    root = _repo(tmp_path, {r: TWO for r in rels}, baseline={}, allowlist={r: _sc() for r in rels})
    assert mod.check(root) == []


@pytest.mark.parametrize("reason", ["", "   ", "\n\t", None])
def test_f_self_contained_no_lib_empty_reason_is_red(tmp_path, reason):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={}, allowlist={A: _sc(reason)})
    assert any("reason" in p and A in p for p in mod.check(root))


def test_f_self_contained_no_lib_entry_is_green_as_new_growth_under_a_schema2_base(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(
        tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 2}, head_allow={B: _sc()}
    )
    assert mod.check(root, base_sha=base) == []


@pytest.mark.parametrize("reason", ["", "   ", "\n\t", None])
def test_f_empty_reason_is_red(tmp_path, reason):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={}, allowlist={A: {"class": "foreign-home", "reason": reason}})
    problems = mod.check(root)
    assert problems and any("reason" in p for p in problems)


def test_f_missing_reason_key_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={}, allowlist={A: {"class": "foreign-home"}})
    assert any("reason" in p for p in mod.check(root))


def test_f_resolver_entry_for_an_undeclared_path_is_red(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: TWO, B: TWO}, baseline={}, allowlist={B: _res()})
    problems = mod.check(root)
    assert any(B in p and "no declared layer" in p for p in problems)


def test_f_one_resolver_entry_per_declared_layer_is_green(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A, "tools"), _layer(B, "hooks")]
    root = _repo(tmp_path, {A: TWO, B: TWO}, baseline={A: 2, B: 2}, allowlist={A: _res(), B: _res()})
    assert mod.check(root) == []


def test_f_a_resolver_copy_beyond_the_declared_layers_is_red(tmp_path):
    """r1 (undeclared path) and r2 (more entries than layers) both speak."""
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A, "tools"), _layer(B, "hooks")]
    root = _repo(
        tmp_path, {A: TWO, B: TWO, C: TWO}, baseline={},
        allowlist={A: _res(), B: _res(), C: _res()},
    )
    problems = mod.check(root)
    assert any(C in p and "no declared layer" in p for p in problems)
    assert any("3 resolver" in p and "2 declared layer" in p for p in problems)


def test_f_the_default_declared_layers_are_the_canonical_one_only():
    mod = _load()
    assert mod.RESOLVER_LAYERS == [
        {"path": "src/engram/tools/_owner_read.py", "layer": "tools", "parity_pinned": True}
    ]
    assert not hasattr(mod, "MAX_RESOLVER_ENTRIES")


@pytest.mark.parametrize(
    "layer",
    [
        {"path": A, "layer": "tools", "parity_pinned": False},
        {"path": A, "layer": "tools", "parity_pinned": 0},
        {"path": A, "layer": "tools"},
        {"path": A, "parity_pinned": True},
        {"layer": "tools", "parity_pinned": True},
        {"path": "", "layer": "tools", "parity_pinned": True},
        {"path": A, "layer": "", "parity_pinned": True},
        "src/engram/tools/a.py",
    ],
    ids=["unpinned", "pinned-0", "no-pin-key", "no-layer-key", "no-path-key", "empty-path",
         "empty-layer", "not-a-dict"],
)
def test_r3_a_layer_declared_without_its_parity_pin_is_refused(tmp_path, layer):
    mod = _load()
    mod.RESOLVER_LAYERS = [layer]
    root = _repo(tmp_path, {A: "x = 1\n"}, baseline={})
    problems = mod.check(root)
    assert problems and any("RESOLVER_LAYERS" in p for p in problems)


def test_r4_duplicate_layer_paths_are_refused(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A, "tools"), _layer(A, "hooks")]
    root = _repo(tmp_path, {A: "x = 1\n"}, baseline={})
    assert any("RESOLVER_LAYERS" in p and "duplicate" in p for p in mod.check(root))


def test_f_one_resolver_plus_foreign_homes_is_green(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(
        tmp_path, {A: TWO, B: TWO, C: TWO}, baseline={A: 2}, allowlist={A: _res(), B: _fd(), C: _fd()}
    )
    assert mod.check(root) == []


def test_f_a_file_in_both_baseline_and_allowlist_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={A: 2}, allowlist={A: _fd()})
    problems = mod.check(root)
    assert problems and any("both" in p and A in p for p in problems)


def test_f_a_new_resolver_entry_for_a_declared_layer_is_green_under_a_schema2_base(tmp_path):
    """The future resolver PR must not be red by construction; growth of the
    allowlist is visible in review."""
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(B)]
    root, base = _two_commit_repo(
        tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 2, B: 2}, head_allow={B: _res()}
    )
    assert mod.check(root, base_sha=base) == []


def test_f_a_new_resolver_entry_for_an_undeclared_path_is_red_under_a_schema2_base(tmp_path):
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root, base = _two_commit_repo(
        tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 2}, head_allow={B: _res()}
    )
    problems = mod.check(root, base_sha=base)
    assert any(B in p and "no declared layer" in p for p in problems)


@pytest.mark.parametrize(
    "entry",
    [
        {"class": "foreign-home", "reason": ""},
        {"class": "foreign-home", "reason": "   "},
        {"class": "resolver", "reason": ""},
        {"class": "nope", "reason": "r"},
    ],
    ids=["fh-empty", "fh-blank", "res-empty", "unknown-class"],
)
def test_f_a_new_invalid_entry_is_red_under_a_schema2_base(tmp_path, entry):
    mod = _load()
    root, base = _two_commit_repo(
        tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 2}, head_allow={B: entry}
    )
    problems = mod.check(root, base_sha=base)
    assert any(B in p and ("class" in p or "reason" in p) for p in problems)


def test_f_allowlist_growth_with_a_foreign_home_is_green(tmp_path):
    mod = _load()
    root, base = _two_commit_repo(
        tmp_path, base_baseline={A: 2, B: 2}, head_baseline={A: 2}, head_allow={B: _fd()}
    )
    assert mod.check(root, base_sha=base) == []


# -- an exempting entry must exempt something ----------------------------------

CLEAN = "import os\nH = os.environ['ENGRAM_HOME']\n"


@pytest.mark.parametrize("entry", [_fd(), _sc()], ids=["foreign-home", "self-contained-no-lib"])
def test_f_exempting_entry_for_a_clean_file_is_red(tmp_path, entry):
    mod = _load()
    root = _repo(tmp_path, {A: CLEAN}, baseline={}, allowlist={A: entry})
    problems = mod.check(root)
    assert len(problems) == 1
    assert "exempts nothing" in problems[0] and "remove it" in problems[0] and A in problems[0]


@pytest.mark.parametrize("entry", [_fd(), _sc()], ids=["foreign-home", "self-contained-no-lib"])
def test_f_exempting_entry_for_a_file_with_violating_lines_is_green(tmp_path, entry):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={}, allowlist={A: entry})
    assert mod.check(root) == []


def test_f_exempting_entry_goes_stale_when_the_last_violating_line_is_fixed(tmp_path):
    mod = _load()
    fixed = "import os\n" + 'p = Path.home() / ".engram"  # ENGRAM_HOME\n'
    root = _repo(tmp_path, {A: fixed}, baseline={}, allowlist={A: _fd()})
    assert any("exempts nothing" in p for p in mod.check(root))


def test_f_resolver_entry_for_a_clean_declared_layer_file_is_green(tmp_path):
    """A resolver entry is a declaration, not an exemption: the file may be clean."""
    mod = _load()
    mod.RESOLVER_LAYERS = [_layer(A)]
    root = _repo(tmp_path, {A: CLEAN}, baseline={}, allowlist={A: _res()})
    assert mod.check(root) == []


def test_f_the_old_allowlist_must_be_empty_rule_is_gone(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={}, allowlist={A: _fd()})
    assert mod.check(root) == []


# -- C2 (g): schema ------------------------------------------------------------

def test_g_schema1_on_the_head_is_red_migrate(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline=[A])
    problems = mod.check(root)
    assert problems and any("schema 2" in p and "migrate" in p for p in problems)


def test_missing_baseline_file_is_red_without_a_base_ref(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/x.py": "pass\n"}, baseline=None)
    problems = mod.check(root)
    assert problems and "baseline" in problems[0]


def test_malformed_baseline_is_red(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {"src/engram/x.py": "pass\n"}, baseline={})
    (root / mod.BASELINE_REL).write_text("{not json")
    assert mod.check(root)


@pytest.mark.parametrize("bad", [-1, "3", 1.5, True, None])
def test_non_integer_or_negative_count_is_red(tmp_path, bad):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={})
    (root / mod.BASELINE_REL).write_text(
        json.dumps({"schema": 2, "baseline": {A: bad}, "allowlist": {}})
    )
    assert mod.check(root)


# -- the real tree: the ratchet is green, nothing about how many ----------------

def test_real_tree_ratchet_is_green():
    mod = _load()
    assert mod.check(REPO) == []


# The real allowlist is PINNED: adding or removing an entry is a deliberate act
# that edits this test in the same commit (PR 6b will). The gate checks the
# SHAPE of an entry; only this pin makes a new, unjustified entry visible.
REAL_ALLOWLIST = {
    "src/engram/tools/grant_viz_operator_acl.py": "foreign-home",
}


def _real_allowlist():
    return json.loads((REPO / "tools" / "home_path_baseline.json").read_text())["allowlist"]


def test_real_allowlist_keys_are_exactly_the_pinned_set():
    assert set(_real_allowlist()) == set(REAL_ALLOWLIST)


def test_real_allowlist_classes_are_the_pinned_ones():
    real = _real_allowlist()
    assert {k: real[k]["class"] for k in real} == REAL_ALLOWLIST


def test_real_allowlist_entries_all_carry_a_reason():
    for k, entry in _real_allowlist().items():
        assert isinstance(entry.get("reason"), str) and entry["reason"].strip(), k


# -- the CLI --------------------------------------------------------------------

def test_cli_exit_codes(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: TWO}, baseline={})
    assert mod.main(["--root", str(root)]) == 1
    _write_baseline(root, {A: 2})
    assert mod.main(["--root", str(root)]) == 0


def test_cli_report_lists_line_numbers_and_counts(tmp_path, capsys):
    mod = _load()
    root = _repo(tmp_path, {A: THREE}, baseline={})
    assert mod.main(["--root", str(root), "--report"]) == 0
    out = capsys.readouterr().out
    assert A in out
    assert re.search(rf"{re.escape(A)}\t3\b", out), out
    for lineno in (2, 4, 5):
        assert re.search(rf"^\s+{lineno}\t", out, re.M), (lineno, out)


def test_cli_seed_writes_schema2_counts_and_refuses_to_overwrite(tmp_path):
    mod = _load()
    root = _repo(tmp_path, {A: THREE, B: "x = 1\n"})
    assert mod.main(["--root", str(root), "--seed"]) == 0
    doc = json.loads((root / mod.BASELINE_REL).read_text())
    assert doc == {
        "schema": 2, "scan_prefixes": list(mod.SCAN_PREFIXES), "baseline": {A: 3}, "allowlist": {},
    }
    assert mod.main(["--root", str(root)]) == 0
    assert mod.main(["--root", str(root), "--seed"]) == 1


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
    step = _gate_step_text()
    assert re.search(
        r'^\s*BASE_SHA=\$\(git merge-base "origin/[^"]+" HEAD\)\s*$', step, re.M
    ), f"BASE_SHA must come from `git merge-base origin/<base> HEAD`:\n{step}"
    call = re.search(r"python3 tools/home_path_gate\.py[^\n]*", step)
    assert call, f"the step does not run tools/home_path_gate.py:\n{step}"
    assert '--base-sha "$BASE_SHA"' in call.group(0), call.group(0)


def test_ci_step_runs_the_gates_own_tests():
    assert "pytest tests/test_home_path_gate.py" in _gate_step_text()
