"""Regression test for issue #2856.

pytest's ``norecursedirs`` ini option *replaces* the built-in default list
(``*.egg .* _darcs build CVS dist node_modules venv {arch}``) rather than
extending it. This repo's ``pytest.ini`` used to read ``norecursedirs =
docs`` -- which drops the built-in ``.*`` pattern, so a bare ``pytest``
invocation at the repo root recurses into ``.claude/worktrees/`` and
collects every stale fairy-worktree copy of the test tree as a second
(often broken) copy of the suite.

CI (``tests.yml``, ``nightly.yml``) invokes pytest with explicit paths
(``pytest tests/ src/forum/tests/``) and never sees this -- so the defect is
invisible to CI and only bites a bare local ``pytest`` run. This test
exercises the collection behavior directly via a scratch checkout, rather
than relying on the real repo actually containing a stale worktree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# The fixed line: pytest's real documented default norecursedirs list
# (verified against the installed pytest's own addini() default for this
# option), plus this repo's own `docs` entry appended.
FIXED_NORECURSEDIRS = "*.egg .* _darcs build CVS dist node_modules venv {arch} docs"

# The pre-fix line (#2856): replaces the defaults instead of extending them.
OLD_NORECURSEDIRS = "docs"


def _make_scratch_repo(tmp_path: Path, norecursedirs_value: str) -> Path:
    """Build a scratch dir with a pytest.ini and a probe test buried under
    .claude/worktrees/x/tests/, mimicking a stale fairy worktree."""
    root = tmp_path / "scratch"
    root.mkdir()

    (root / "pytest.ini").write_text(
        f"[pytest]\nnorecursedirs = {norecursedirs_value}\n",
        encoding="utf-8",
    )

    worktree_tests = root / ".claude" / "worktrees" / "x" / "tests"
    worktree_tests.mkdir(parents=True)
    (worktree_tests / "test_probe.py").write_text(
        "def test_probe():\n    assert True\n",
        encoding="utf-8",
    )

    return root


def _collect_only_output(root: Path) -> str:
    """Run `pytest --collect-only -q` from root and return combined output."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


class TestNorecursedirsExtendsDefaults:
    """#2856: norecursedirs must extend, not replace, pytest's built-in defaults."""

    def test_fixed_config_does_not_collect_stale_worktree(self, tmp_path: Path):
        """With the fixed norecursedirs line, a bare collect-only run must NOT
        descend into .claude/worktrees/ and pick up the stale probe test."""
        root = _make_scratch_repo(tmp_path, FIXED_NORECURSEDIRS)
        output = _collect_only_output(root)

        assert "test_probe" not in output, (
            "Fixed norecursedirs must NOT collect "
            ".claude/worktrees/x/tests/test_probe.py; got:\n" + output
        )

    def test_old_config_collects_stale_worktree(self, tmp_path: Path):
        """Control: the pre-fix `norecursedirs = docs` line DOES collect the
        stale worktree copy -- proving this test would have caught the
        original bug (a control that never demonstrates the bug is not a
        real regression test)."""
        root = _make_scratch_repo(tmp_path, OLD_NORECURSEDIRS)
        output = _collect_only_output(root)

        assert "test_probe" in output, (
            "Control failed: the OLD `norecursedirs = docs` config should "
            "collect .claude/worktrees/x/tests/test_probe.py (proving the "
            "defect existed); got:\n" + output
        )
