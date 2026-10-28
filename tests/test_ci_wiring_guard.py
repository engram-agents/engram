"""CI wiring guard (#3346): every test file must be run by SOME CI job.

Rule enforced
-------------
Every git-tracked file named ``test_*.py`` that sits OUTSIDE the directories
``tests.yml`` collects must be named, by path, in at least one
``.github/workflows/*.yml`` -- or be listed in ``EXEMPT`` below with a
non-empty reason.

Why it exists
-------------
``tests.yml`` runs ``pytest <dir> <dir>``. A ``test_*.py`` placed anywhere else
(``tools/``, ``src/engram/tools/``, ...) is collected by nothing unless a
workflow names it. #3346 found eleven such files: nine healthy ones that had
simply never run in CI, and two dead ones that rotted for months
(``AttributeError`` at fixture setup, 34 errors) because nothing ever ran them
to say so. A test that no job runs cannot fail, so it cannot protect anything
and it cannot warn that it is stale either.

What it checks, and what it does not
------------------------------------
It checks that a path is NAMED in a workflow (full-line comments excluded). It
does NOT check that the step naming it actually runs it: a step that is
`if:`-skipped, `continue-on-error`, or lists the path as an argument to
something other than pytest still satisfies the guard. The collected-directory
set comes only from tests.yml's single-line `run: pytest <paths>` step; other
tests.yml steps (e.g. a multi-line `run: |` block) contribute no directories.

Design notes
------------
* The collected-directory set is PARSED from the pytest invocation line in
  ``tests.yml`` -- never hardcoded -- so a change to that line moves the guard
  with it. If the parse finds no paths the guard FAILS LOUDLY (a vacuous pass
  is the failure mode this file exists to prevent).
* "Named by path" is checked against workflow text with FULL-LINE COMMENTS
  STRIPPED: a comment that merely mentions a file does not run it.
* Stdlib + pytest only, so it can run in pr-checks' cheap stdlib-only step.
* Tracked files come from ``git ls-files``. Without git the real-tree tests
  SKIP (an ``os.walk`` cannot tell tracked files from stray ones such as a
  local ``.venv``, so it would assert about the wrong population).
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TESTS_YML = WORKFLOWS_DIR / "tests.yml"

# path (repo-relative, POSIX) -> reason it is deliberately not run by CI.
# A reason must be a non-empty string; a path that is no longer tracked is a
# stale entry and fails the guard (dead entries must not accumulate).
EXEMPT: dict[str, str] = {
    "docs/internal/archive/test_migrate_db_trust_tier.py": (
        "archived, retired one-off migration test kept for history under docs/internal/archive/"
    ),
    "docs/internal/archive/test_migrate_trust_tier_self_backfill.py": (
        "archived, retired one-off migration test kept for history under docs/internal/archive/"
    ),
}

# --- the guard -----------------------------------------------------------------

# A genuine YAML `run:` step invoking pytest (optionally via `python -m`),
# anchored at the line start so a COMMENT (`# run: pytest decoy/`) or pytest
# text inside another command (`run: echo "run: pytest decoy/"`) never matches.
# Same shape and reasoning as tools/test_id_set_check.py's
# _WORKFLOW_PYTEST_RUN_RE (#2950).
_PYTEST_RUN_RE = re.compile(
    r"^[ \t]*(?:-[ \t]+)?run:[ \t]*(?:python3?[ \t]+-m[ \t]+)?pytest[ \t]+(.+)"
)


def parse_collected_dirs(tests_yml_text: str) -> tuple[str, ...]:
    """Positional path arguments of every single-line `run: pytest ...` step.

    Raises ValueError when no path is found at all: a guard that cannot see
    what tests.yml collects must fail, not pass vacuously.
    """
    roots: list[str] = []
    for line in tests_yml_text.splitlines():
        m = _PYTEST_RUN_RE.match(line)
        if not m:
            continue
        for token in m.group(1).split():
            if token.startswith("-"):
                break
            roots.append(token)
    if not roots:
        raise ValueError(
            "no `run: pytest <paths> ...` line with positional paths found in "
            "tests.yml -- cannot tell which directories CI collects; the "
            "workflow was probably restructured, update parse_collected_dirs"
        )
    return tuple(roots)


def git_tracked_files(root: Path):
    """Tracked files via `git ls-files`, or None when git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [f for f in out.decode("utf-8", "surrogateescape").split("\0") if f]


def _strip_full_line_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _is_named(path: str, text: str) -> bool:
    # Boundary-aware: `tools/test_x.py` must not match inside
    # `src/engram/tools/test_x.py` or as a prefix of `tools/test_x.pyc`.
    return re.search(r"(?<![\w./-])" + re.escape(path) + r"(?![\w.-])", text) is not None


def find_problems(tracked, tests_yml_text, workflow_texts, exempt) -> list[str]:
    """Every wiring problem, as human-readable strings naming the file."""
    collected = parse_collected_dirs(tests_yml_text)  # ValueError if vacuous
    tracked_set = set(tracked)
    named_in = "\n".join(_strip_full_line_comments(t) for t in workflow_texts)
    problems: list[str] = []

    for path in sorted(tracked_set):
        if not fnmatch.fnmatch(os.path.basename(path), "test_*.py"):
            continue
        if path.startswith(collected):
            continue
        if path in exempt:
            continue
        if not _is_named(path, named_in):
            problems.append(
                f"{path}: test file is outside the directories tests.yml collects "
                f"({', '.join(collected)}) and no .github/workflows/*.yml names it "
                "by path, so no CI job runs it. Wire it into a workflow or add it "
                "to EXEMPT with a reason."
            )

    for path, reason in sorted(exempt.items()):
        if not isinstance(reason, str) or not reason.strip():
            problems.append(
                f"{path}: EXEMPT entry has an empty reason; state why CI does not run it."
            )
        if path not in tracked_set:
            problems.append(
                f"{path}: stale EXEMPT entry -- the path is no longer tracked; remove it."
            )
    return problems


def _real_workflow_texts() -> list[str]:
    return [p.read_text() for p in sorted(WORKFLOWS_DIR.glob("*.yml"))]


# --- fixtures ------------------------------------------------------------------

TESTS_YML_SAMPLE = """\
jobs:
  pytest:
    steps:
      - name: Run pytest
        run: pytest tests/ src/forum/tests/ -v --tb=short
"""


def _problems(tracked, *, tests_yml=TESTS_YML_SAMPLE, extra_workflows=(), exempt=None):
    texts = [tests_yml, *extra_workflows]
    return find_problems(tracked, tests_yml, texts, exempt or {})


# --- contract cell 1: the real tree -------------------------------------------


def test_real_tree_every_test_file_is_wired_or_exempt():
    tracked = git_tracked_files(REPO_ROOT)
    if tracked is None:
        pytest.skip(
            "git is unavailable here; os.walk cannot distinguish tracked test "
            "files from stray untracked ones (e.g. a local .venv), so the "
            "wiring guard would assert about the wrong population"
        )
    problems = find_problems(
        tracked, TESTS_YML.read_text(), _real_workflow_texts(), EXEMPT
    )
    assert problems == [], "\n".join(problems)


# --- contract cell 2: a synthetic unwired file fails, naming it ---------------


def test_unwired_test_file_fails_and_names_it():
    problems = _problems(["tests/test_a.py", "tools/test_orphan.py"])
    assert any("tools/test_orphan.py" in p for p in problems), problems
    assert not any("tests/test_a.py" in p for p in problems), problems


def test_file_named_in_a_workflow_passes():
    wf = "      - run: python3 -m pytest tools/test_wired.py -q\n"
    assert _problems(["tools/test_wired.py"], extra_workflows=[wf]) == []


def test_comment_mention_does_not_count_as_wiring():
    wf = "      # tools/test_commented.py would be run here someday\n"
    problems = _problems(["tools/test_commented.py"], extra_workflows=[wf])
    assert any("tools/test_commented.py" in p for p in problems), problems


@pytest.mark.parametrize(
    "tracked_path,named_in_workflow",
    [
        # SHORT tracked path, workflow names only the LONGER one (lookbehind).
        ("tools/test_foo.py", "src/engram/tools/test_foo.py"),
        # Trailing-suffix variants of the tracked path (lookahead).
        ("tools/test_foo.py", "tools/test_foo.py.bak"),
        ("tools/test_foo.py", "tools/test_foo.pyc"),
        ("tools/test_foo.py", "tools/test_foo_extra.py"),
        # The mirror direction: tracked LONG path, text names the short one.
        ("src/engram/tools/test_foo.py", "tools/test_foo.py"),
    ],
)
def test_path_match_respects_boundaries(tracked_path, named_in_workflow):
    wf = f"      - run: python3 -m pytest {named_in_workflow} -q\n"
    problems = _problems([tracked_path], extra_workflows=[wf])
    assert any(p.startswith(tracked_path + ":") for p in problems), problems


def test_exact_path_is_named_even_next_to_a_longer_one():
    wf = (
        "      - run: python3 -m pytest src/engram/tools/test_foo.py "
        "tools/test_foo.py -q\n"
    )
    assert _problems(["tools/test_foo.py"], extra_workflows=[wf]) == []


def test_non_test_files_are_ignored():
    assert _problems(["tools/helper.py", "docs/test_notes.md", "tools/conftest.py"]) == []


# --- contract cell 3: removing the workflow reference fails, naming the file --


def test_removing_the_reference_from_real_workflows_fails_naming_the_file():
    tracked = git_tracked_files(REPO_ROOT)
    if tracked is None:
        pytest.skip("git unavailable; see the real-tree test")
    texts = _real_workflow_texts()
    collected = parse_collected_dirs(TESTS_YML.read_text())
    victims = [
        f
        for f in tracked
        if fnmatch.fnmatch(os.path.basename(f), "test_*.py")
        and f not in EXEMPT
        and not f.startswith(collected)
        and f in "".join(texts)
    ]
    assert victims, "no wired out-of-tree test file found; the control would be vacuous"
    victim = victims[0]
    stripped = [t.replace(victim, "REMOVED") for t in texts]
    problems = find_problems(tracked, TESTS_YML.read_text(), stripped, EXEMPT)
    assert any(victim in p for p in problems), problems


def test_named_in_two_workflows_survives_losing_one():
    p = "tools/test_two.py"
    wf = f"      - run: python3 -m pytest {p} -q\n"
    assert _problems([p], extra_workflows=[wf, wf]) == []
    assert _problems([p], extra_workflows=[wf]) == []
    assert _problems([p], extra_workflows=[]) != []


# --- contract cell 4: an EXEMPT entry needs a real reason --------------------


@pytest.mark.parametrize("reason", ["", "   ", "\t\n", None])
def test_exempt_entry_with_blank_reason_fails(reason):
    path = "tools/test_exempted.py"
    problems = _problems([path], exempt={path: reason})
    assert any(path in p for p in problems), problems


def test_exempt_entry_with_reason_suppresses_the_unwired_finding():
    path = "tools/test_exempted.py"
    assert _problems([path], exempt={path: "retired tool, kept for history"}) == []


# --- contract cell 5: a stale EXEMPT entry fails ----------------------------


def test_stale_exempt_entry_fails_and_names_it():
    problems = _problems(
        ["tests/test_a.py"], exempt={"tools/test_gone.py": "was retired"}
    )
    assert any("tools/test_gone.py" in p for p in problems), problems


# --- contract cell 6: the collected set is PARSED, never hardcoded ------------


def test_parse_reads_the_roots_from_the_pytest_line():
    assert parse_collected_dirs(TESTS_YML_SAMPLE) == ("tests/", "src/forum/tests/")


def test_parse_ignores_comments_and_echoed_text():
    text = (
        "      # run: pytest decoy/ -v\n"
        '      - run: echo "run: pytest decoy2/ -v"\n'
        "      - run: pytest tests/ -q\n"
    )
    assert parse_collected_dirs(text) == ("tests/",)


@pytest.mark.parametrize(
    "text",
    ["", "jobs:\n  x:\n    steps:\n      - run: echo hi\n", "      - run: pytest -v\n"],
)
def test_parse_with_no_paths_fails_loudly(text):
    with pytest.raises(ValueError):
        parse_collected_dirs(text)


def test_guard_with_unparseable_tests_yml_fails_instead_of_passing_vacuously():
    with pytest.raises(ValueError):
        find_problems(["tools/test_a.py"], "no pytest here\n", ["no pytest here\n"], {})


def test_second_collected_dir_is_honoured():
    # A guard that hardcodes `tests/` only would flag the forum file.
    tracked = ["tests/test_a.py", "src/forum/tests/test_b.py"]
    assert _problems(tracked) == []


def test_collected_set_moves_with_tests_yml():
    tracked = ["tests/test_a.py", "src/forum/tests/test_b.py", "tools/test_c.py"]
    only_tests = "      - run: pytest tests/ -v\n"
    problems = _problems(tracked, tests_yml=only_tests)
    assert any("src/forum/tests/test_b.py" in p for p in problems), problems
    assert any("tools/test_c.py" in p for p in problems), problems
    with_tools = "      - run: pytest tests/ tools/ -v\n"
    problems = _problems(tracked, tests_yml=with_tools)
    assert not any("tools/test_c.py" in p for p in problems), problems
    assert any("src/forum/tests/test_b.py" in p for p in problems), problems


def test_real_tests_yml_parses_to_a_nonempty_set():
    assert parse_collected_dirs(TESTS_YML.read_text())
