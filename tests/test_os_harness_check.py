"""Tests for tools/os-harness-check.py — the OS/harness portability checker.

The checker's job is to report an ABSENCE ("no bash-4-only constructs in
shipped shell scripts"), and an absence-reporting tool is only trustworthy
if its detector demonstrably fires on a positive. So these tests are
deliberately weighted toward the detector's own falsifiability rather than
toward its plumbing:

  - every pattern finds a real known-positive construct,
  - every pattern REJECTS the portable bash-3.2 equivalent,
  - a planted violation in a real file is actually found,
  - the documented exit contract (0 clean / 1 violations / 2 broken detector)
    holds, because the doc states it and a stated contract that nobody
    exercises is just a claim.

The module filename is hyphenated (matching tools/scan-leaks.py convention),
so it is loaded by path rather than imported by name.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "src" / "engram" / "tools" / "os-harness-check.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("os_harness_check", CHECKER)
    assert spec and spec.loader, f"could not load {CHECKER}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


def test_checker_exists_at_canonical_source_path():
    """tiers.json paths resolve against source_root=src/engram, so the file
    must live here and not only at the repo-root tools/ symlink."""
    assert CHECKER.is_file(), f"{CHECKER} missing — tiers.json entry would dangle"


def test_repo_root_symlink_points_at_the_canonical_file():
    link = REPO_ROOT / "tools" / "os-harness-check.py"
    assert link.exists(), "repo-root tools/ entry missing"
    assert link.resolve() == CHECKER.resolve()


class TestDetectorFalsifiability:
    """The load-bearing tests: a scanner that cannot fire is not a scanner."""

    def test_every_pattern_matches_its_known_positive(self, checker):
        for label, pattern, sample in checker.CHECKS:
            assert pattern.search(sample), (
                f"pattern {label!r} failed to match its own known-positive {sample!r}"
            )

    @pytest.mark.parametrize(
        "portable",
        [
            'while IFS= read -r l; do arr+=("$l"); done < f',  # portable mapfile
            "local seen",                                       # plain local
            'echo "$name" | tr "[:upper:]" "[:lower:]"',        # portable lowercase
            "cmd >> out.log 2>&1",                              # portable append-both
        ],
    )
    def test_no_pattern_matches_a_portable_construct(self, checker, portable):
        """Guards the other direction: a pattern broadened until it matches
        everything would pass the known-positive test and be useless."""
        for label, pattern, _sample in checker.CHECKS:
            assert not pattern.search(portable), (
                f"pattern {label!r} matched portable bash-3.2 construct {portable!r}"
            )

    def test_self_test_passes_on_the_shipped_patterns(self, checker):
        assert checker.self_test() is True


class TestScan:
    def test_finds_a_planted_violation(self, checker, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        script = tmp_path / "bad.sh"
        script.write_text("#!/usr/bin/env bash\nlocal -A seen\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "bad.sh"], check=True)

        findings = checker.scan(tmp_path)
        assert findings, "scan found nothing in a file containing `local -A`"
        assert any("associative array" in label for label, *_ in findings)

    def test_ignores_whole_line_comments(self, checker, tmp_path):
        """The macOS setup doc and this checker's own docstring name these
        constructs in prose; naming one must not be reported as using one."""
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        script = tmp_path / "documented.sh"
        script.write_text("#!/usr/bin/env bash\n# do not re-add mapfile or local -A here\necho ok\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "documented.sh"], check=True)

        assert checker.scan(tmp_path) == []

    def test_clean_on_a_portable_script(self, checker, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        script = tmp_path / "good.sh"
        script.write_text('#!/usr/bin/env bash\nset -euo pipefail\nlocal seen\ncmd >> log 2>&1\n')
        subprocess.run(["git", "-C", str(tmp_path), "add", "good.sh"], check=True)

        assert checker.scan(tmp_path) == []

    def test_untracked_files_are_not_scanned(self, checker, tmp_path):
        """Untracked scratch never ships, so it must not fail the gate."""
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "scratch.sh").write_text("#!/usr/bin/env bash\nmapfile -t x < f\n")
        assert checker.scan(tmp_path) == []


class TestSelectionCoverage:
    """The `*.sh` glob was the original coverage bound, and it was wrong at
    this tool's own bullseye. Colleague review found tools/agentctl-mac — the
    macOS-targeted script, the exact thing the checker protects — sitting
    permanently outside the scan while the checker printed "clean".
    """

    def test_agentctl_mac_is_actually_scanned(self, checker):
        """The regression that matters. If this file leaves the scanned set,
        a bash-4 construct added to the macOS script ships silently."""
        scanned, _exempt = checker._tracked_shell_files(REPO_ROOT)
        rels = {p.relative_to(REPO_ROOT).as_posix() for p in scanned}
        assert "tools/agentctl-mac" in rels, (
            "tools/agentctl-mac dropped out of the scanned set — the checker's "
            "own bullseye is unprotected again"
        )

    def test_extensionless_shell_scripts_are_selected(self, checker, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        f = tmp_path / "noext"
        f.write_text("#!/bin/bash\nmapfile -t x < f\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "noext"], check=True)
        assert checker.scan(tmp_path), "extensionless bash script was not scanned"

    def test_extensionless_non_shell_is_not_selected(self, checker, tmp_path):
        """A python entrypoint with no extension must not be shell-scanned."""
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        f = tmp_path / "pytool"
        f.write_text("#!/usr/bin/env python3\nmapfile = 1\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "pytool"], check=True)
        scanned, _ = checker._tracked_shell_files(tmp_path)
        assert scanned == []


class TestExemptDirective:
    def test_directive_exempts_and_is_reported_not_silent(self, checker, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        f = tmp_path / "linuxonly"
        f.write_text(
            f"#!/bin/bash\n# {checker.EXEMPT_DIRECTIVE} — intentional\nlocal -n r=x\n"
        )
        subprocess.run(["git", "-C", str(tmp_path), "add", "linuxonly"], check=True)

        scanned, exempt = checker._tracked_shell_files(tmp_path)
        assert scanned == [] and len(exempt) == 1
        assert checker.scan(tmp_path) == []

        # and the exemption must SURFACE — a silent skip is the coverage bound
        # that made the *.sh glob a defect in the first place
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(tmp_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "exempt" in result.stderr and "linuxonly" in result.stderr

    def test_directive_below_the_header_is_ignored(self, checker, tmp_path):
        """A directive must not be able to hide deep in a long file."""
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        f = tmp_path / "sneaky"
        body = "\n".join(["# filler"] * (checker._HEADER_LINES + 5))
        f.write_text(f"#!/bin/bash\n{body}\n# {checker.EXEMPT_DIRECTIVE}\nlocal -n r=x\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "sneaky"], check=True)

        scanned, exempt = checker._tracked_shell_files(tmp_path)
        assert exempt == [], "directive was honored outside the header window"
        assert scanned and checker.scan(tmp_path), "file should have been scanned"

    def test_agentctl_is_exempt_for_a_stated_reason(self, checker):
        """Its bash-4.3 namerefs are intentional (Linux-host-only tool). Pinned
        so nobody 'portability-fixes' correct code, and so the exemption stays
        a declaration in the file rather than an invisible allowlist here."""
        _scanned, exempt = checker._tracked_shell_files(REPO_ROOT)
        rels = {p.relative_to(REPO_ROOT).as_posix(): reason for p, reason in exempt}
        assert "tools/agentctl" in rels
        assert checker.EXEMPT_DIRECTIVE in rels["tools/agentctl"]


class TestExitContract:
    """docs/os-harness-constraints.md states these codes. Exercise them."""

    def _run(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            capture_output=True, text=True, cwd=cwd,
        )

    def test_self_test_flag_exits_zero(self):
        assert self._run("--self-test").returncode == 0

    def test_clean_tree_exits_zero(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "ok.sh").write_text("#!/usr/bin/env bash\necho hi\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "ok.sh"], check=True)
        assert self._run("--root", str(tmp_path)).returncode == 0

    def test_violations_exit_one(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "bad.sh").write_text("#!/usr/bin/env bash\nmapfile -t x < f\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "bad.sh"], check=True)
        result = self._run("--root", str(tmp_path))
        assert result.returncode == 1
        assert "mapfile" in result.stderr

    def test_this_repo_is_currently_clean(self):
        """Regression guard: if someone adds a bash-4 construct to a shipped
        script, this fails here rather than on a macOS operator's install."""
        result = self._run("--root", str(REPO_ROOT))
        assert result.returncode == 0, (
            "shipped shell scripts now contain bash-4-only constructs:\n"
            + result.stderr
        )
