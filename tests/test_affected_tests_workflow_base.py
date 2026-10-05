"""affected-tests.yml must diff against a real base on a manual dispatch (#3410).

Contract
--------
1. A dispatch with an empty ``github.base_ref`` falls back to the repository's
   default branch, so the tests affected by the branch's changes are selected.
2. A dispatch whose branch changes a test file runs that test file.
3. When ``base_ref`` IS set (a pull_request event) its value is used unchanged.

Why it exists
-------------
The workflow is ``workflow_dispatch``-only, and ``github.base_ref`` is set only
on ``pull_request`` events. The old ``--base origin/${{ github.base_ref }}``
rendered ``--base origin/`` on every manual dispatch, nothing resolved as
changed, and the tool exited 0 with "no tests selected": a vacuous green.

Method
------
(a) parse the workflow YAML and extract the ``--base`` expression of the step
that runs ``run_touched_tests.py``; (b) evaluate the ``${{ A || B }}`` form with
a small evaluator (only the contexts this workflow uses); (c) run the REAL
selection tool in a temporary git repo with an ``origin`` remote, using the
rendered base, and assert the changed test is selected -- with ``--base
origin/`` as the control that selects nothing.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "affected-tests.yml"
TOOL = REPO_ROOT / "src" / "engram" / "tools" / "run_touched_tests.py"

_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _base_argument() -> str:
    """Return the raw ``--base`` argument text of the run_touched_tests step."""
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    runs = [
        step["run"]
        for job in wf["jobs"].values()
        for step in job["steps"]
        if "run_touched_tests.py" in step.get("run", "")
    ]
    assert len(runs) == 1, f"expected exactly one run_touched_tests step, got {len(runs)}"
    # Join shell line continuations, then take the token after --base.
    flat = runs[0].replace("\\\n", " ")
    # The argument is a run of non-space chars and/or whole ${{ ... }} groups
    # (an expression may itself contain spaces).
    m = re.search(r"--base\s+((?:\$\{\{.*?\}\}|[^\s$])+)", flat)
    assert m, f"no --base argument in step: {runs[0]!r}"
    return m.group(1)


def _eval_operand(operand: str, ctx: dict[str, str]) -> str:
    operand = operand.strip()
    if len(operand) >= 2 and operand[0] == operand[-1] and operand[0] in "'\"":
        return operand[1:-1]
    assert operand in ctx, f"unsupported context in expression: {operand!r}"
    return ctx[operand]


def _render(arg: str, base_ref: str, default_branch: str) -> str:
    """Render ``${{ A || B || ... }}``: ``||`` yields the first non-empty operand."""
    ctx = {
        "github.base_ref": base_ref,
        "github.event.repository.default_branch": default_branch,
    }

    def sub(m: re.Match) -> str:
        value = ""
        for operand in m.group(1).split("||"):
            value = _eval_operand(operand, ctx)
            if value:
                break
        return value

    return _EXPR.sub(sub, arg)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo_with_origin(tmp_path: Path) -> Path:
    """A repo whose origin has ``dev``; a feature branch changes tests/test_x.py."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "dev", str(origin))
    repo = tmp_path / "work"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (repo / "tests" / "test_y.py").write_text("def test_y():\n    pass\n", encoding="utf-8")
    pm = repo / "src" / "engram" / "packaging"
    pm.mkdir(parents=True)
    (pm / "test-map.json").write_text(
        json.dumps({"schema_version": 1, "mappings": [], "unmapped": []}), encoding="utf-8"
    )
    _git(repo, "init", "-b", "dev")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "dev")
    _git(repo, "fetch", "origin")
    _git(repo, "checkout", "-b", "feature")
    (repo / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "commit", "-am", "change a test")
    return repo


def _selection(repo: Path, base: str) -> dict:
    out = subprocess.run(
        [sys.executable, str(TOOL), "--repo-root", str(repo), "--base", base, "--json"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_expression_falls_back_to_default_branch_when_base_ref_empty():
    """Contract 1: empty base_ref on a dispatch renders origin/<default branch>."""
    arg = _base_argument()
    assert _render(arg, base_ref="", default_branch="dev") == "origin/dev"


def test_expression_uses_base_ref_unchanged_when_set():
    """Contract 3: a pull_request event's base_ref wins over the default branch."""
    arg = _base_argument()
    assert _render(arg, base_ref="main", default_branch="dev") == "origin/main"


def test_dispatch_selects_the_changed_test(repo_with_origin: Path):
    """Contract 2: the rendered dispatch base selects the changed test file."""
    base = _render(_base_argument(), base_ref="", default_branch="dev")
    sel = _selection(repo_with_origin, base)
    assert sel["changed_files"] == ["tests/test_x.py"]
    assert sel["selected"] == ["tests/test_x.py"]


def test_empty_base_selects_nothing_control(repo_with_origin: Path):
    """Control: the defective ``--base origin/`` selects nothing (the #3410 defect)."""
    sel = _selection(repo_with_origin, "origin/")
    assert sel["selected"] == []
    assert sel["changed_files"] == []
