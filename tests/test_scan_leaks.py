"""Tests for tools/scan-leaks.py — roster-externalization contract.

Coverage:
  1. config-absent: advisory line printed, structural patterns still run, exit 0 on clean tree.
  2. config-present: personal-name patterns load and match correctly.
  3. example file (.scan-leaks-local.json.example) contains no real personal names —
     only obviously-fake placeholders from the known-fake allowlist.
  4. Mutation check: with config present, a planted fake name is detected.
  5. SKIP_FILES contains 'scan-leaks.py' — structural self-skip because the scanner's
     own source always contains the literal patterns it checks.
  6. load_local_patterns unit tests (absent / present).
  7. Real-repo integration: scanner exits 0 on REPO_ROOT with local config absent.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_LEAKS = REPO_ROOT / "src" / "engram" / "tools" / "scan-leaks.py"
EXAMPLE_CONFIG = REPO_ROOT / "docs" / "internal" / ".scan-leaks-local.json.example"

# append (not insert(0)) -- scan-leaks.py is loaded below by explicit file
# path via importlib, so this dir doesn't need to win any name resolution;
# inserting it at position 0 would needlessly outrank src/forum for the
# rest of the pytest run (src/engram/tools/ also holds forum.py), shadowing
# the real `forum` package for every test collected afterward (#2037/#2063).
sys.path.append(str(REPO_ROOT / "src" / "engram" / "tools"))

# Import the module. The file is named with a hyphen so we use importlib.
import importlib.util

_spec = importlib.util.spec_from_file_location("scan_leaks", SCAN_LEAKS)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tree(files: dict[str, str]) -> Path:
    """Create a temporary directory tree with the given file contents."""
    tmp = Path(tempfile.mkdtemp(prefix="scan_leaks_test_"))
    for relpath, content in files.items():
        fpath = tmp / relpath
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)
    return tmp


# ---------------------------------------------------------------------------
# Test 1: config-absent — advisory message, structural patterns only, exit 0
# ---------------------------------------------------------------------------

def test_config_absent_prints_advisory_and_exits_clean(tmp_path):
    """When .scan-leaks-local.json is absent the scanner prints advisory and exits 0."""
    # Minimal clean tree — no structural-pattern hits either.
    (tmp_path / "clean.py").write_text("# nothing suspicious here\n")

    result = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"expected exit 0, got {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    assert ".scan-leaks-local.json absent" in result.stdout, (
        f"expected advisory line in stdout; got: {result.stdout!r}"
    )
    assert "personal-name scan skipped" in result.stdout


def test_config_absent_structural_pattern_still_fires(tmp_path):
    """Structural patterns (committed) fire even without the local config."""
    # Split the structural literal so this test file itself is not flagged by
    # a real-repo scanner run (the scanner self-skips its own source file but
    # not this test file — see BLOCKER #2 / item 2 fix rationale).
    structural_hit = "kg_protocol" + "_for_ai"
    (tmp_path / "bad.py").write_text(f"path = '{structural_hit}/stuff'\n")

    result = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, (
        f"expected exit 1 (leak found), got {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "main repo path" in result.stderr


# ---------------------------------------------------------------------------
# Test 2: config-present — personal-name patterns load and match
# ---------------------------------------------------------------------------

def test_config_present_patterns_load_and_match(tmp_path):
    """When .scan-leaks-local.json is present its patterns are applied."""
    # Write a local config using obviously-fake names.
    config = {
        "patterns": [
            {"regex": r"\b(Alice|Bob)\b", "label": "personal name"},
        ]
    }
    (tmp_path / ".scan-leaks-local.json").write_text(json.dumps(config))
    (tmp_path / "offending.py").write_text("# Alice wrote this\n")

    result = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, (
        f"expected exit 1 (personal name detected), got {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "personal name" in result.stderr


def test_config_present_no_match_exits_clean(tmp_path):
    """With config present and no matching content, exit 0."""
    config = {
        "patterns": [
            {"regex": r"\b(Alice|Bob)\b", "label": "personal name"},
        ]
    }
    (tmp_path / ".scan-leaks-local.json").write_text(json.dumps(config))
    (tmp_path / "clean.py").write_text("# nothing personal here\n")

    result = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    # Advisory should NOT appear when config is present.
    assert ".scan-leaks-local.json absent" not in result.stdout


# ---------------------------------------------------------------------------
# Test 3: example file contains only obviously-fake names
# ---------------------------------------------------------------------------

# The canonical set of obviously-fake placeholder names we allow in the example.
FAKE_NAME_ALLOWLIST = {"Alice", "Bob", "Carol", "Dave", "Eve", "example-user"}


def test_example_config_contains_no_real_names():
    """The committed example config must reference only obviously-fake placeholder names."""
    assert EXAMPLE_CONFIG.exists(), f"example config not found at {EXAMPLE_CONFIG}"
    raw = json.loads(EXAMPLE_CONFIG.read_text())

    for entry in raw.get("patterns", []):
        regex_str = entry.get("regex", "")
        # Extract word-like tokens from the regex (capture-group contents,
        # alternation arms, literal path segments).
        # Strategy: strip regex metacharacters and split on non-word boundaries.
        # This is intentionally conservative — catches literal names inside
        # \b(...)\b alternations and path literals.
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", regex_str)
        for token in tokens:
            # Allow regex keywords and structural words
            if token in {"home", "regex", "label", "example"}:
                continue
            assert token in FAKE_NAME_ALLOWLIST, (
                f"Example config pattern contains token {token!r} which is not in "
                f"the fake-name allowlist {FAKE_NAME_ALLOWLIST}. "
                f"The example must use only obviously-fake placeholder names."
            )


# ---------------------------------------------------------------------------
# Test 4: mutation check — planted fake name is detected with local config
# ---------------------------------------------------------------------------

def test_mutation_planted_name_detected(tmp_path):
    """Planting a fake name that matches the local config triggers a leak report."""
    config = {
        "patterns": [
            {"regex": r"\b(Alice)\b", "label": "personal name"},
        ]
    }
    (tmp_path / ".scan-leaks-local.json").write_text(json.dumps(config))
    # Clean file first
    target = tmp_path / "scratch.py"
    target.write_text("# clean\n")

    clean_result = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert clean_result.returncode == 0, "pre-plant should be clean"

    # Plant the fake name
    target.write_text("# Alice was here\n")

    dirty_result = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert dirty_result.returncode == 1, (
        f"planted name should trigger leak; returncode={dirty_result.returncode}\n"
        f"stderr: {dirty_result.stderr}"
    )
    assert "personal name" in dirty_result.stderr

    # Remove the planted file (cleanup)
    target.write_text("# clean again\n")


# ---------------------------------------------------------------------------
# Test 5: SKIP_FILES self-skips scan-leaks.py for structural reasons
# ---------------------------------------------------------------------------

def test_skip_files_self_skip_is_structural():
    """scan-leaks.py must appear in SKIP_FILES.

    The self-skip is STRUCTURAL: the scanner's own source always contains the
    literal patterns it checks — both in the module docstring and in the
    COMMITTED_PATTERNS definition.  Skipping it prevents a permanent false
    positive on every real-repo run, regardless of whether personal-name
    literals are present.
    """
    assert "scan-leaks.py" in _mod.SKIP_FILES, (
        "scan-leaks.py must be in SKIP_FILES: the scanner's source contains "
        "the committed structural patterns verbatim (docstring + "
        "COMMITTED_PATTERNS definition), so it always self-matches without "
        "the skip."
    )


# ---------------------------------------------------------------------------
# Test 6: load_local_patterns returns ([], False) when config absent
# ---------------------------------------------------------------------------

def test_load_local_patterns_absent(tmp_path):
    patterns, present = _mod.load_local_patterns(tmp_path)
    assert patterns == []
    assert present is False


def test_load_local_patterns_present(tmp_path):
    config = {"patterns": [{"regex": r"\bAlice\b", "label": "test"}]}
    (tmp_path / ".scan-leaks-local.json").write_text(json.dumps(config))
    patterns, present = _mod.load_local_patterns(tmp_path)
    assert present is True
    assert len(patterns) == 1
    regex_obj, label = patterns[0]
    assert label == "test"
    assert regex_obj.search("Hello Alice world")


# ---------------------------------------------------------------------------
# Test 7: malformed JSON config → advisory printed, structural-only run
# ---------------------------------------------------------------------------

def test_malformed_config_fires_advisory(tmp_path):
    """Malformed JSON in the local config triggers the advisory (personal-name
    net didn't run), and the scanner still exits 0 on a clean tree."""
    (tmp_path / ".scan-leaks-local.json").write_text("{not valid json")
    (tmp_path / "clean.py").write_text("# nothing suspicious here\n")

    result = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"expected exit 0 on clean tree with malformed config, "
        f"got {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "personal-name scan skipped" in result.stdout, (
        f"expected advisory in stdout when local patterns end up empty; "
        f"got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: real-repo integration — scanner exits 0 on REPO_ROOT, config absent
# ---------------------------------------------------------------------------

def test_real_repo_scan_no_config_exits_clean(tmp_path):
    """Running the scanner against the real repo root with no local config
    exits 0 — the committed codebase must be clean of structural-pattern hits
    (the scanner self-skips its own source via SKIP_FILES).

    Uses --config-path pointing at a non-existent file inside tmp_path to
    force the 'config absent' path regardless of any real local config the
    developer may have installed.
    """
    absent_config = tmp_path / "no-config.json"  # guaranteed not to exist

    result = subprocess.run(
        [
            sys.executable, str(SCAN_LEAKS),
            "--root", str(REPO_ROOT),
            "--config-path", str(absent_config),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Real-repo scan with structural patterns only must exit 0.\n"
        f"returncode: {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}\n"
        "If this fails, a committed file contains the structural pattern "
        "(currently: 'kg_protocol' + '_for_ai') and must be fixed or added "
        "to SKIP_FILES."
    )
    assert "personal-name scan skipped" in result.stdout, (
        "Expected advisory confirming personal-name net didn't run; "
        f"got stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 9: git-tracking — untracked file with a structural hit is invisible
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo with a committer identity."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def test_repo_mode_ignores_untracked_hit(tmp_path):
    """Repo-mode scan must NOT report a structural-pattern hit in an untracked file.

    Fixture:
      - git repo with one committed clean file (tracked.py)
      - one gitignored/untracked file (dirty.py) containing the structural
        hit (the committed pattern — written split in code to avoid self-match)

    Expected: exit 0, 0 hits reported.

    Mutation note: if the scanner used os.walk instead of git ls-files, it
    would find dirty.py and exit 1 on the structural hit.  The git-enumeration
    path makes untracked dirty files invisible.
    """
    _init_git_repo(tmp_path)

    # Commit a clean tracked file
    tracked = tmp_path / "tracked.py"
    tracked.write_text("# nothing suspicious\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    # Add an untracked (gitignored) file that contains the structural-pattern hit.
    # Use concatenation so this test file itself does not trip the real-repo scan.
    dirty = tmp_path / "dirty.py"
    dirty.write_text("path = '" + "kg_protocol" + "_for_ai" + "/stuff'\n")

    # Also .gitignore it so it's explicitly excluded
    (tmp_path / ".gitignore").write_text("dirty.py\n")

    absent_config = tmp_path / "no-config.json"
    result = subprocess.run(
        [
            sys.executable, str(SCAN_LEAKS),
            "--root", str(tmp_path),
            "--config-path", str(absent_config),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Repo-mode scan must exit 0: untracked hit in dirty.py must be invisible.\n"
        f"returncode: {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "main repo path" not in result.stderr, (
        "Structural hit in untracked file must not appear in stderr output; "
        f"got stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 10: git-tracking inverse — tracked file with a hit is still detected
# ---------------------------------------------------------------------------

def test_repo_mode_detects_tracked_hit(tmp_path):
    """Repo-mode scan MUST report a structural-pattern hit in a tracked file.

    Fixture:
      - git repo with one committed file (tracked.py) containing
        the structural hit (the committed pattern — written split in code to avoid self-match)
      - one clean untracked file (untracked.py) that is NOT hit

    Expected: exit 1, hit reported from tracked.py.

    Non-vacuity inverse of test_repo_mode_ignores_untracked_hit: proves that
    switching to git ls-files enumeration does not silently drop real leaks
    that ARE in tracked files.
    """
    _init_git_repo(tmp_path)

    # Commit the file that contains the structural-pattern hit
    bad = tmp_path / "bad.py"
    bad.write_text("path = '" + "kg_protocol" + "_for_ai" + "/stuff'\n")
    subprocess.run(["git", "add", "bad.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init with leak"], cwd=tmp_path, check=True)

    # Untracked clean file (must NOT trigger a false positive)
    (tmp_path / "untracked.py").write_text("# nothing here\n")

    absent_config = tmp_path / "no-config.json"
    result = subprocess.run(
        [
            sys.executable, str(SCAN_LEAKS),
            "--root", str(tmp_path),
            "--config-path", str(absent_config),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, (
        f"Repo-mode scan must exit 1 when a tracked file contains the structural hit.\n"
        f"returncode: {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "main repo path" in result.stderr, (
        "Expected 'main repo path' label in stderr for the tracked hit; "
        f"got stderr: {result.stderr!r}"
    )
    assert "bad.py" in result.stderr, (
        "Expected the filename bad.py to appear in the leak report; "
        f"got stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 11: the archived development repository is a legitimate link target
# ---------------------------------------------------------------------------

def test_archived_dev_repo_url_is_not_flagged(tmp_path):
    """A tracked file naming the archived development repository is clean.

    The old private dev-repo URL guard was retired: the repository is public
    and the former development repository is archived, frozen history that
    docs may link to.  End-to-end, so it fails if any pattern (or a future
    refactor of enumeration) starts flagging that URL again.
    """
    _init_git_repo(tmp_path)
    doc = tmp_path / "history.md"
    doc.write_text("Earlier history: https://github.com/engram-agents/engram-alpha\n")
    subprocess.run(["git", "add", "history.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "link archive"], cwd=tmp_path, check=True)

    result = subprocess.run(
        [
            sys.executable, str(SCAN_LEAKS),
            "--root", str(tmp_path),
            "--config-path", str(tmp_path / "no-config.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Scanner must not flag a link to the archived development repository.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "history.md" not in result.stderr


# ---------------------------------------------------------------------------
# Test 12: the absolute-home-path class (default-deny, whole segments)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - class: /(home|Users)/<seg>/ with <seg> = [A-Za-z][A-Za-z0-9._-]*
#   - default-deny: a segment outside the declared allowed set is a finding
#   - whole segments only: an allowed name that prefixes a longer name is
#     still a finding
#   - domain: every git-tracked text file whatever its extension; a file whose
#     first 8 KiB holds a NUL byte is skipped
#   - a finding exits 1 and names file:line and the segment
# Planted names are fictional; verdicts are asserted on scratch git repos,
# never on counts read from the real tree.

def _commit_files(path: Path, files: dict, mode: dict | None = None) -> None:
    _init_git_repo(path)
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            f.write_bytes(content)
        else:
            f.write_text(content)
        if mode and rel in mode:
            f.chmod(mode[rel])
        subprocess.run(["git", "add", rel], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "plant"], cwd=path, check=True)


def _run_scan(root: Path):
    return subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(root),
         "--config-path", str(root / "no-config.json")],
        capture_output=True, text=True,
    )


def _home(seg: str, base: str = "home") -> str:
    # assembled so this file's own text never carries a complete path
    return "/" + base + "/" + seg + "/"


def test_home_path_in_md_is_a_finding(tmp_path):
    _commit_files(tmp_path, {"doc.md": f"data in {_home('zorblax')}.engram\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "doc.md:1" in r.stderr
    assert "zorblax" in r.stderr


def test_home_path_in_extensionless_executable_is_a_finding(tmp_path):
    _commit_files(
        tmp_path,
        {"tools/runner": f"#!/bin/sh\necho {_home('zorblax')}x\n"},
        mode={"tools/runner": 0o755},
    )
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tools/runner:2" in r.stderr
    assert "zorblax" in r.stderr


def test_home_path_in_yml_is_a_finding(tmp_path):
    _commit_files(tmp_path, {".github/workflows/ci.yml": f"run: ls {_home('zorblax', 'Users')}\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ci.yml:1" in r.stderr
    assert "zorblax" in r.stderr


def test_home_path_in_yaml_is_a_finding(tmp_path):
    _commit_files(tmp_path, {"c.yaml": f"p: {_home('zorblax')}\n"})
    assert _run_scan(tmp_path).returncode == 1


def test_allowed_placeholders_and_system_dirs_are_clean(tmp_path):
    _commit_files(tmp_path, {
        "a.md": f"{_home('alice')} and {_home('Shared', 'Users')} and {_home('agent-bob')}\n",
        "b.txt": f"{_home('agents-shared')}forum\n",
    })
    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_prefix_of_allowed_name_is_still_a_finding(tmp_path):
    _commit_files(tmp_path, {"a.md": f"see {_home('alice2')}x\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "alice2" in r.stderr


def test_binary_file_with_home_path_is_skipped(tmp_path):
    payload = b"\x00\x01" + _home("zorblax").encode() + b"\x00"
    _commit_files(tmp_path, {"blob.bin": payload})
    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_allowed_set_entries_carry_reasons():
    allowed = _mod._home_path_class().ALLOWED_HOME_SEGMENTS
    assert {"someone", "agent-alice", "Shared", "agents-shared"} <= set(allowed)
    assert all(isinstance(why, str) and why.strip() for why in allowed.values())
    assert "open decision" in allowed["agents-shared"]


def test_real_tree_has_no_home_path_findings():
    """The whole-tree gate: the committed tree carries no real home path."""
    findings, unreadable = _mod.scan_home_paths(REPO_ROOT)
    assert findings == [], "\n".join(f"{f}:{n} {seg}" for f, n, seg in findings)
    assert unreadable == [], unreadable


# ---------------------------------------------------------------------------
# Test 13: segment-boundary forms (no trailing slash needed), exemption scope,
# non-UTF-8 input.  Each form is a finding: the class is the WHOLE segment
# after /home/ or /Users/, ended by any non-segment character or end of string.
# ---------------------------------------------------------------------------

def _bare(seg: str, base: str = "home") -> str:
    # no trailing slash; assembled so this file's own text carries no path
    return "/" + base + "/" + seg


_BOUNDARY_FORMS = {
    "end_of_string": lambda: "cd " + _bare("zed"),
    "double_quoted": lambda: 'p = "' + _bare("zed") + '"',
    "before_colon": lambda: "PATH=" + _bare("zed") + ":/bin",
    "sentence_end_dot": lambda: "see " + _bare("zed") + ".",
    "users_no_slash": lambda: "cd " + _bare("zed", "Users"),
    "users_lowercase": lambda: "cd " + _bare("zed", "users") + "/x",
    "digit_start": lambda: "cd " + _bare("9zed") + "/x",
    "underscore_start": lambda: "cd " + _bare("_zed") + "/x",
}


@pytest.mark.parametrize("form", sorted(_BOUNDARY_FORMS))
def test_boundary_form_is_a_finding(tmp_path, form):
    _commit_files(tmp_path, {"doc.md": _BOUNDARY_FORMS[form]() + "\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, (form, r.stdout + r.stderr)
    assert "doc.md:1" in r.stderr


def test_sentence_end_dot_is_stripped_before_the_allowed_check(tmp_path):
    _commit_files(tmp_path, {
        "a.md": "see " + _bare("someone") + ". and " + _bare("Shared", "Users") + ",\n",
    })
    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    (tmp_path / "b.md").write_text("see " + _bare("zed") + ".\n")
    subprocess.run(["git", "add", "b.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "more"], cwd=tmp_path, check=True)
    r = _run_scan(tmp_path)
    assert r.returncode == 1
    assert "segment: zed" in r.stderr and "segment: zed." not in r.stderr


def test_whole_segment_without_trailing_slash_is_not_prefix_allowed(tmp_path):
    _commit_files(tmp_path, {"a.md": "cd " + _bare("someone2") + "\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "someone2" in r.stderr


def test_real_looking_name_in_an_ordinary_tests_file_is_a_finding(tmp_path):
    """Pins the exemption scope: only declared files are exempt, never a directory."""
    _commit_files(tmp_path, {"tests/test_ordinary.py": 'p = "' + _home("zed") + '"\n'})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tests/test_ordinary.py:1" in r.stderr


def test_non_utf8_file_is_still_scanned(tmp_path):
    payload = b"caf\xe9 " + _home("zed").encode() + b"x\n"
    _commit_files(tmp_path, {"latin1.md": payload})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "latin1.md:1" in r.stderr


def test_no_file_is_exempt_from_the_home_path_check():
    """Pins the exemption scope: nothing is exempt today, and any entry must carry a reason."""
    ex = _mod.HOME_PATH_EXEMPT_FILES
    assert ex == {}, f"a new exemption needs review: {sorted(ex)}"
    assert all(why.strip() for why in ex.values())


def test_seat_home_convention_prefixes_are_clean_but_a_real_name_is_not(tmp_path):
    """/home/agent-* and /home/agent-$tag are the seat-home convention (a prefix
    before a glob or placeholder, declared in the allowed set); agent-<name> is a name."""
    _commit_files(tmp_path, {
        "a.sh": "for h in " + _bare("agent-") + "*; do :; done\ncd " + _bare("agent-") + "$tag/x\n",
    })
    assert _run_scan(tmp_path).returncode == 0
    (tmp_path / "b.sh").write_text("cd " + _bare("agent-zed") + "\n")
    subprocess.run(["git", "add", "b.sh"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "name"], cwd=tmp_path, check=True)
    r = _run_scan(tmp_path)
    assert r.returncode == 1 and "agent-zed" in r.stderr


@pytest.mark.parametrize("seg", ["someone.x", "alice.smith"])
def test_allowed_name_plus_dot_plus_more_is_a_different_segment(tmp_path, seg):
    """A dot inside the segment is a segment character: an allowed name followed by
    a dot and more characters is a different, undeclared name."""
    _commit_files(tmp_path, {"a.md": "cd " + _home(seg) + "x\n" + "cd " + _bare(seg) + "\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "a.md:1" in r.stderr and "a.md:2" in r.stderr
    assert "segment: " + seg in r.stderr


def test_allowed_name_before_a_sentence_end_dot_is_clean(tmp_path):
    _commit_files(tmp_path, {"a.md": "see " + _bare("someone") + ".\n" + "see " + _bare("alice") + ". Next.\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_home_prefix_is_case_sensitive_but_users_is_not(tmp_path):
    """Documented design: /home/ is case-sensitive, /Users/ is not."""
    _commit_files(tmp_path, {"a.md": "cd " + _home("zed", "HOME") + "x\n" + "cd " + _bare("zed", "Home") + "\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    (tmp_path / "b.md").write_text("cd " + _home("zed", "USERS") + "x\n")
    subprocess.run(["git", "add", "b.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "users"], cwd=tmp_path, check=True)
    r = _run_scan(tmp_path)
    assert r.returncode == 1 and "b.md:1" in r.stderr


def test_allowed_home_segment_set_is_pinned():
    """The allowed set is the one hole in a default-deny check, so it is pinned.

    Any addition turns this red, including a real person's or seat's folder name.
    """
    expected = {
        # fictional placeholders
        "someone", "someone01", "user", "you", "x", "alice", "bob", "test",
        "testuser", "example-user", "dev", "agent", "g", "zzz", "other", "seat",
        "PLACEHOLDER_USER", "agent-alice", "agent-bob", "agent-chosenone",
        # seat-home convention prefixes before a glob or placeholder
        "agent-", "agent-NAME", "agent-newborn-", "agent-newborn-XXX",
        # system and shared directories
        "Shared", "agents-shared",
    }
    actual = set(_mod._home_path_class().ALLOWED_HOME_SEGMENTS)
    assert actual == expected, (
        "The allowed home-path segment set changed "
        f"(added: {sorted(actual - expected)}, removed: {sorted(expected - actual)}). "
        "Adding an entry needs a security-seat read, and a real person's or seat's "
        "folder name must never be added; the PR adding it must say why."
    )
    assert all(isinstance(why, str) and why.strip() for why in _mod._home_path_class().ALLOWED_HOME_SEGMENTS.values()), (
        "every allowed home-path segment needs a non-empty reason"
    )


# ---------------------------------------------------------------------------
# A bad local-config entry is reported by position, never echoed
# (the local roster is a personal-name list: regex and label can be names).
# Tokens below are invented, carry no real name, and appear nowhere else.
# ---------------------------------------------------------------------------

_TOK_RE = "zqxtok7731"
_TOK_LABEL = "vwjlab5519"


def _load_with_entries(tmp_path, entries, capsys):
    cfg = tmp_path / "roster.json"
    cfg.write_text(json.dumps({"patterns": entries}))
    patterns, present = _mod.load_local_patterns(tmp_path, cfg)
    return patterns, present, capsys.readouterr().err


def _good(label):
    return {"regex": r"\b" + label + r"\b", "label": label}


def _assert_reported_by_position(err, index, cls, entries):
    assert f"skipping bad pattern entry {index}: {cls}" in err
    assert _TOK_RE not in err
    assert _TOK_LABEL not in err
    for entry in entries:
        assert repr(entry) not in err


def test_bad_entry_missing_regex_reported_by_position(tmp_path, capsys):
    bad = {"label": _TOK_LABEL}
    entries = [_good("okone"), bad, _good("oktwo")]
    patterns, present, err = _load_with_entries(tmp_path, entries, capsys)
    assert present is True
    assert [lbl for _, lbl in patterns] == ["okone", "oktwo"]
    _assert_reported_by_position(err, 2, "KeyError", entries)
    assert "missing key 'regex'" in err


def test_bad_entry_missing_label_names_the_key_only(tmp_path, capsys):
    bad = {"regex": _TOK_RE}
    entries = [bad, _good("oktwo")]
    patterns, _, err = _load_with_entries(tmp_path, entries, capsys)
    assert [lbl for _, lbl in patterns] == ["oktwo"]
    _assert_reported_by_position(err, 1, "KeyError", entries)
    assert "missing key 'label'" in err


def test_bad_entry_re_error_does_not_echo_exception_text(tmp_path, capsys):
    pattern = f"(?P={_TOK_RE})"
    # Precondition: the re.error text really embeds the token, so a loader
    # that appends str(exc) would leak it.
    with pytest.raises(re.error) as info:
        re.compile(pattern)
    assert _TOK_RE in str(info.value)
    # "error" on 3.12, "PatternError" on 3.13+.
    cls = type(info.value).__name__
    bad = {"regex": pattern, "label": "okbad"}
    entries = [_good("okone"), bad, _good("oktwo")]
    patterns, _, err = _load_with_entries(tmp_path, entries, capsys)
    assert [lbl for _, lbl in patterns] == ["okone", "oktwo"]
    _assert_reported_by_position(err, 2, cls, entries)


def test_bad_entry_label_is_not_echoed(tmp_path, capsys):
    # Derive the class name ("error" on 3.12, "PatternError" on 3.13+).
    with pytest.raises(re.error) as info:
        re.compile(f"(?P={_TOK_RE})")
    cls = type(info.value).__name__
    bad = {"regex": f"(?P={_TOK_RE})", "label": _TOK_LABEL}
    entries = [_good("okone"), bad]
    patterns, _, err = _load_with_entries(tmp_path, entries, capsys)
    assert [lbl for _, lbl in patterns] == ["okone"]
    _assert_reported_by_position(err, 2, cls, entries)


def test_bad_entry_not_a_dict_reported_by_position(tmp_path, capsys):
    entries = [_TOK_LABEL, _good("oktwo")]
    patterns, _, err = _load_with_entries(tmp_path, entries, capsys)
    assert [lbl for _, lbl in patterns] == ["oktwo"]
    _assert_reported_by_position(err, 1, "TypeError", entries)


@pytest.mark.parametrize("exc_cls", [OverflowError, RecursionError])
def test_bad_entry_other_compile_failures_are_skipped(tmp_path, capsys, monkeypatch, exc_cls):
    real_compile = re.compile

    def fake_compile(pat, *a, **k):
        if pat == _TOK_RE:
            raise exc_cls(_TOK_LABEL)
        return real_compile(pat, *a, **k)

    monkeypatch.setattr(_mod.re, "compile", fake_compile)
    entries = [{"regex": _TOK_RE, "label": "okbad"}, _good("oktwo")]
    patterns, _, err = _load_with_entries(tmp_path, entries, capsys)
    assert [lbl for _, lbl in patterns] == ["oktwo"]
    _assert_reported_by_position(err, 1, exc_cls.__name__, entries)


# ---------------------------------------------------------------------------
# Test 14: the pattern scan reads every tracked text file (#3732)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - scan()'s candidate set is every git-tracked file, honouring SKIP_DIRS and
#     SKIP_FILES; the use_git=False os.walk fallback follows the same policy.
#     An extension no longer selects a file.
#   - a file is binary iff its first 8192 bytes hold a NUL byte; it is skipped
#     and counted as skipped-binary.  Non-UTF-8 text is decoded with
#     errors="replace" and IS scanned.
#   - main() prints exactly one summary line
#     "scan-leaks: scanned N, skipped-binary M", on clean and dirty runs, and
#     N + M equals the number of files the scan considered.
#   - findings and exit codes are unchanged.

_ALICE_ROSTER = {"patterns": [{"regex": r"\bAlice\b", "label": "personal name"}]}

_SUMMARY_RE = re.compile(r"^scan-leaks: scanned (\d+), skipped-binary (\d+)$", re.M)


def _write_roster(root: Path) -> Path:
    """Plant a local name roster in the fixture repo root (gitignored by name:
    LOCAL_CONFIG_NAME is in SKIP_FILES, so it is not itself a candidate)."""
    cfg = root / ".scan-leaks-local.json"
    cfg.write_text(json.dumps(_ALICE_ROSTER))
    return cfg


def _run_scan_with_roster(root: Path):
    """Run the scanner with the fixture's own roster (default config path)."""
    return subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(root)],
        capture_output=True, text=True,
    )


def _summary(stdout: str) -> tuple[int, int]:
    """Return (scanned, skipped-binary) from the single summary line."""
    matches = _SUMMARY_RE.findall(stdout)
    assert len(matches) == 1, (
        f"expected exactly one 'scan-leaks: scanned N, skipped-binary M' line; "
        f"stdout={stdout!r}"
    )
    return int(matches[0][0]), int(matches[0][1])


def test_roster_hit_in_js_file_is_reported(tmp_path):
    _commit_files(tmp_path, {"web/app.js": "// Alice wrote this\n"})
    _write_roster(tmp_path)
    r = _run_scan_with_roster(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "web/app.js:1" in r.stderr
    assert "personal name" in r.stderr


def test_roster_hit_in_html_file_is_reported(tmp_path):
    _commit_files(tmp_path, {"site/index.html": "<p>Alice</p>\n"})
    _write_roster(tmp_path)
    r = _run_scan_with_roster(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "site/index.html:1" in r.stderr
    assert "personal name" in r.stderr


def test_roster_hit_in_extensionless_executable_is_reported(tmp_path):
    _commit_files(
        tmp_path,
        {"tools/runner": "#!/bin/sh\n# Alice\n"},
        mode={"tools/runner": 0o755},
    )
    _write_roster(tmp_path)
    r = _run_scan_with_roster(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tools/runner:2" in r.stderr
    assert "personal name" in r.stderr


def test_roster_hit_in_non_utf8_text_file_is_reported(tmp_path):
    """Non-UTF-8 text is decoded with errors="replace" and still scanned."""
    _commit_files(tmp_path, {"notes.txt": b"caf\xe9 Alice\n"})
    _write_roster(tmp_path)
    r = _run_scan_with_roster(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "notes.txt:1" in r.stderr
    assert "personal name" in r.stderr


def test_binary_files_are_skipped_and_counted(tmp_path):
    _commit_files(tmp_path, {
        "fonts/ui.ttf": b"\x00\x01\x00\x00Alice",
        "img/logo.png": b"\x89PNG\r\n\x1a\n\x00Alice",
        "web/app.js": "// a clean text file\n",
    })
    _write_roster(tmp_path)
    r = _run_scan_with_roster(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    scanned, skipped = _summary(r.stdout)
    assert (scanned, skipped) == (1, 2), (
        f"one text file scanned, two binaries skipped; stdout={r.stdout!r}"
    )


def test_summary_counts_partition_the_files_considered(tmp_path):
    """N + M equals the candidate population: tracked files minus SKIP_DIRS
    and SKIP_FILES entries."""
    _commit_files(tmp_path, {
        "app.js": "// js\n",
        "page.html": "<p>x</p>\n",
        "blob.bin": b"\x00binary",
        "LICENSE": "MIT\n",
        "node_modules/pkg/index.js": "// vendored\n",
    })
    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    scanned, skipped = _summary(r.stdout)
    # app.js + page.html scanned; blob.bin binary; LICENSE (SKIP_FILES) and
    # node_modules/ (SKIP_DIRS) are not candidates at all.
    assert (scanned, skipped) == (2, 1), f"stdout={r.stdout!r}"


def test_summary_line_prints_on_a_dirty_run(tmp_path):
    _commit_files(tmp_path, {"bad.js": "path = '" + "kg_protocol" + "_for_ai" + "/x'\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "main repo path" in r.stderr
    assert _summary(r.stdout) == (1, 0), f"stdout={r.stdout!r}"


def test_use_git_false_fallback_scans_all_text_files(tmp_path):
    """The os.walk fallback follows the same all-text policy (SKIP_DIRS and
    SKIP_FILES still honoured)."""
    (tmp_path / "app.js").write_text("Alice\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00Alice")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "vendored.js").write_text("Alice\n")
    (tmp_path / "LICENSE").write_text("Alice\n")

    report = _mod.scan(
        tmp_path, [(re.compile(r"\bAlice\b"), "personal name")], use_git=False,
    )
    assert [rel for rel, _, _, _ in report.findings] == ["app.js"]
    assert (report.scanned, report.skipped_binary) == (1, 1)


def test_tracked_directory_symlink_is_scanned_by_its_link_text(tmp_path):
    """A git-tracked symlink to a directory is scanned, by its link text.

    Since #4805 round 4, classification uses lstat and a symlink's published
    content is its link text (never followed): the symlink counts in
    ``scanned`` and is not a not-a-file candidate.  The frozen summary line
    therefore reports two scanned files (the regular file and the link).
    """
    (tmp_path / "real-dir").mkdir()
    _commit_files(tmp_path, {"real/member.py": "// clean\n"})
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "engine").symlink_to("../real-dir")
    subprocess.run(["git", "add", "tools/engine"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "symlink"], cwd=tmp_path, check=True)

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == (2, 0), f"stdout={r.stdout!r}"


# ---------------------------------------------------------------------------
# Test 15: --roster-file and --redact (whole-tree sweep support)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - --roster-file takes newline-separated regexes in the SAME format as the
#     NAME_LEAK_ROSTER secret: blank lines and first-non-space-'#' lines are
#     ignored; valid lines are scanned alongside the committed patterns and the
#     local roster.
#   - an invalid line is reported by 1-based LINE NUMBER, exception class and
#     (for re.error) offset — never by its text.
#   - the "personal-name scan skipped" advisory fires only when BOTH roster
#     sources produced no pattern.
#   - --redact prints every finding as repo-relative-path:line only, for BOTH
#     families (pattern hits and absolute-home-path hits), with neither the
#     matched text nor a label/segment.
#   - exit codes are unchanged (0 clean, 1 leaks, 2 usage/root error).
#
# The tokens below are invented for these cells, appear nowhere else, and the
# home-path fixture is assembled by _home() so the committed test file never
# carries a complete path.

_EXT_TOKEN = "qlemvort8427"
_EXT_LABEL = "plavrisk6013"
_BAD_ROSTER_TOKEN = "ztokq9241"


def _external_roster(tmp_path: Path, text: str) -> Path:
    """Write a roster file OUTSIDE the scanned root (as CI does: RUNNER_TEMP)."""
    roster = tmp_path / "external-roster.txt"
    roster.write_text(text, encoding="utf-8")
    return roster


def test_roster_file_supplies_patterns_and_suppresses_the_skip_advisory(tmp_path):
    """The external roster finds what the local roster would not, and no skip
    is claimed while it is supplying patterns."""
    root = tmp_path / "root"
    root.mkdir()
    _commit_files(root, {"doc.md": f"hello {_EXT_TOKEN} world\n"})
    roster = _external_roster(tmp_path, "# a comment\n\n" + _EXT_TOKEN + "\n")

    # Control: local roster absent, no external file -> clean + skip advisory.
    without = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(root),
         "--config-path", str(root / "no-config.json")],
        capture_output=True, text=True,
    )
    assert without.returncode == 0, without.stdout + without.stderr
    assert "personal-name scan skipped" in without.stdout

    # With the external roster the hit is found and the skip claim is gone.
    with_roster = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(root),
         "--config-path", str(root / "no-config.json"),
         "--roster-file", str(roster)],
        capture_output=True, text=True,
    )
    assert with_roster.returncode == 1, with_roster.stdout + with_roster.stderr
    assert "doc.md:1" in with_roster.stderr
    assert "personal-name scan skipped" not in with_roster.stdout
    assert "personal-name scan skipped" not in with_roster.stderr


def test_redact_prints_path_and_line_only(tmp_path):
    """--redact prints path:line for a pattern hit and neither the matched text
    nor the roster label. The unredacted run is the control: it carries both."""
    _commit_files(tmp_path, {"doc.md": f"hello {_EXT_TOKEN} world\n"})
    (tmp_path / ".scan-leaks-local.json").write_text(json.dumps(
        {"patterns": [{"regex": _EXT_TOKEN, "label": _EXT_LABEL}]}))

    plain = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert plain.returncode == 1, plain.stdout + plain.stderr
    # Control: the unredacted form really does carry both, so their absence
    # below is a property of --redact and not of the fixture.
    assert _EXT_TOKEN in plain.stderr and _EXT_LABEL in plain.stderr

    redacted = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path), "--redact"],
        capture_output=True, text=True,
    )
    assert redacted.returncode == 1, redacted.stdout + redacted.stderr
    assert "doc.md:1" in redacted.stderr
    combined = redacted.stdout + redacted.stderr
    assert _EXT_TOKEN not in combined, combined
    assert _EXT_LABEL not in combined, combined


def test_redact_hides_the_home_path_segment(tmp_path):
    """--redact applies to the absolute-home-path family too: the username
    segment must not reach the log."""
    _commit_files(tmp_path, {"doc.md": "data in " + _home("zorblax") + "x\n"})

    plain = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert plain.returncode == 1, plain.stdout + plain.stderr
    assert "segment: zorblax" in plain.stderr  # control

    redacted = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path), "--redact"],
        capture_output=True, text=True,
    )
    assert redacted.returncode == 1, redacted.stdout + redacted.stderr
    assert "doc.md:1" in redacted.stderr
    assert "zorblax" not in (redacted.stdout + redacted.stderr)


def test_invalid_roster_line_reports_line_and_class_without_text(tmp_path):
    """An invalid external roster line is reported by raw 1-based line number,
    exception class and offset — never by its text; the valid lines still run."""
    bad_line = f"(?P={_BAD_ROSTER_TOKEN})"
    with pytest.raises(re.error) as probe:
        re.compile(bad_line)
    # Precondition: this CPython embeds the token in str(exc), so a loader that
    # appended str(exc) would leak it.
    assert _BAD_ROSTER_TOKEN in str(probe.value)

    root = tmp_path / "root"
    root.mkdir()
    _commit_files(root, {"doc.md": f"hello {_EXT_TOKEN} world\n"})
    # Line 1 is valid, 2 a comment, 3 blank, 4 the bad line.
    roster = _external_roster(
        tmp_path, _EXT_TOKEN + "\n# a comment\n\n" + bad_line + "\n"
    )

    r = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(root),
         "--config-path", str(root / "no-config.json"),
         "--roster-file", str(roster), "--redact"],
        capture_output=True, text=True,
    )
    assert r.returncode == 1, r.stdout + r.stderr  # the valid line still fires
    assert "doc.md:1" in r.stderr
    assert "bad roster line 4" in r.stderr, r.stderr
    assert type(probe.value).__name__ in r.stderr, r.stderr
    assert f"at offset {probe.value.pos}" in r.stderr, r.stderr
    assert _BAD_ROSTER_TOKEN not in r.stderr, r.stderr
    assert probe.value.msg not in r.stderr and "unknown group name" not in r.stderr


def test_absent_roster_file_is_a_usage_error(tmp_path):
    """An explicitly named roster that cannot be read must not silently shrink
    the scan: it is a usage error (exit 2), never a clean run."""
    (tmp_path / "clean.py").write_text("# nothing suspicious\n")
    r = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(tmp_path),
         "--roster-file", str(tmp_path / "no-such-roster.txt")],
        capture_output=True, text=True,
    )
    assert r.returncode == 2, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# Test 16: type-scoped advice (R3-5) and an all-invalid roster (R3-7)
# ---------------------------------------------------------------------------

_ROSTER_ADVICE = "do not widen or edit the roster to silence it"
_HOME_ADVICE = "The roster does not apply to this finding."


def test_pattern_hit_gets_the_roster_sentence(tmp_path):
    """Control: a pattern hit IS told not to edit the roster — so the absence
    of that sentence for a home-path hit below is a property of the hit type,
    not of the fixture."""
    root = tmp_path / "root"
    root.mkdir()
    _commit_files(root, {"doc.md": f"hello {_EXT_TOKEN} world\n"})
    roster = _external_roster(tmp_path, _EXT_TOKEN + "\n")
    r = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(root),
         "--config-path", str(root / "no-config.json"),
         "--roster-file", str(roster), "--redact"],
        capture_output=True, text=True,
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert _ROSTER_ADVICE in r.stderr, r.stderr
    assert _HOME_ADVICE not in r.stderr, r.stderr


def test_home_path_hit_gets_its_own_sentence_not_the_roster_one(tmp_path):
    """R3-5: an absolute-home-path hit has nothing to do with the roster. It
    must not be told to leave the roster alone, and it gets its own sentence."""
    _commit_files(tmp_path, {"doc.md": "data in " + _home("zorblax") + "x\n"})
    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _HOME_ADVICE in r.stderr, r.stderr
    assert _ROSTER_ADVICE not in r.stderr, r.stderr


def test_all_invalid_roster_file_is_a_usage_error(tmp_path):
    """R3-7: an explicitly requested roster that supplies no usable pattern is
    a usage error (exit 2), not a clean run; the count of rejected lines is
    named and the pattern text never is.

    CI pre-validates its roster before invoking this flag, so this is the
    standalone flag's contract, not CI's.
    """
    root = tmp_path / "root"
    root.mkdir()
    _commit_files(root, {"doc.md": "hello world\n"})
    bad = f"(?P={_BAD_ROSTER_TOKEN})"
    roster = _external_roster(tmp_path, bad + "\n" + bad + "x\n")
    r = subprocess.run(
        [sys.executable, str(SCAN_LEAKS), "--root", str(root),
         "--config-path", str(root / "no-config.json"),
         "--roster-file", str(roster)],
        capture_output=True, text=True,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "all 2 roster line(s) were rejected" in r.stderr, r.stderr
    assert _BAD_ROSTER_TOKEN not in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# Test 16: coverage accounting — every candidate is accounted for (#4752)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - scan() returns a ScanReport: findings, scanned, skipped_binary_paths,
#     unreadable_paths, candidates.  The invariant
#     scanned + len(skipped_binary_paths) + len(unreadable_paths) == candidates
#     holds and is asserted in the scanner itself.
#   - a candidate whose read raises OSError lands in unreadable_paths (sorted,
#     repo-relative posix) and in neither of the other two buckets.
#   - an empty candidate set prints "scan-leaks: no coverage: 0 candidate files
#     under <root>; nothing was scanned", does NOT print `clean`, and exits 3.
#   - unreadable files print one named line and exit 3 when nothing was found;
#     findings keep exit 1 precedence (the dirty tree is the primary fact).
#   - skipped binaries are named (up to 20, then "(+K more)") on their own
#     line after the FROZEN "scan-leaks: scanned N, skipped-binary M" line.
#   - scan_home_paths() returns (findings, unreadable_paths) and classifies
#     every candidate with the same lstat seam (since #4805 round 4 a symlink
#     is scanned by its link text; an unreadable path is reported).

_SKIPPED_NAMES_RE = re.compile(r"^scan-leaks: skipped-binary files: (.*)$", re.M)
_UNREADABLE_LINE_RE = re.compile(
    r"^scan-leaks: unreadable (\d+) \(coverage incomplete\): (.*)$", re.M
)
_NO_COVERAGE_RE = re.compile(
    r"^scan-leaks: no coverage: 0 candidate files under .*; nothing was scanned$",
    re.M,
)
_NAMED_TAIL_RE = re.compile(r" \(\+\d+ more\)$")


def _named_paths(body: str) -> list[str]:
    """Return the paths named on a capped line, dropping a '(+K more)' tail."""
    body = body.strip()
    tail = _NAMED_TAIL_RE.search(body)
    if tail:
        body = body[:tail.start()]
    return [p.strip() for p in body.split(",") if p.strip()]


def _lock(root: Path, rel: str) -> None:
    """Lock a tracked file after its commit: a chmod-0000 file cannot be
    `git add`ed by a non-root user, so the mode is applied afterwards."""
    (root / rel).chmod(0o000)


def test_scan_report_reconciles_every_candidate(tmp_path):
    """One text file, one NUL file, one unreadable file: 1 + 1 + 1 == 3.

    Pre-fix: scan() returns a 3-tuple, so there is no ScanReport to read; and
    an OSError is a silent `continue`, counted in neither bucket.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-0000 file; the unreadable arm is unreachable")

    _commit_files(tmp_path, {
        "ok.txt": "clean text\n",
        "blob.bin": b"\x00binary",
        "locked.txt": "secret\n",
    })
    _lock(tmp_path, "locked.txt")

    report = _mod.scan(tmp_path, [], use_git=True)

    assert report.scanned == 1
    assert report.skipped_binary_paths == ["blob.bin"]
    assert report.unreadable_paths == ["locked.txt"]
    assert report.missing_paths == []
    assert report.not_a_file_paths == []
    assert report.candidates == 3
    # The spec's invariant, closed with a LITERAL: three enumerated candidates
    # (the fixture's tracked files).  Written as the literal rather than as
    # report.candidates so this line cannot move with the implementation's own
    # arithmetic — the bucket assertions above are what discriminate.
    assert (report.scanned + len(report.skipped_binary_paths)
            + len(report.unreadable_paths) + len(report.missing_paths)
            + len(report.not_a_file_paths)) == 3
    assert report.skipped_binary == 1


def test_cli_unreadable_file_exits_3_and_is_named(tmp_path):
    """Pre-fix: exit 0 and `clean` — the OSError arm is a silent `continue`."""
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-0000 file; the unreadable arm is unreachable")

    _commit_files(tmp_path, {"ok.txt": "clean\n", "locked.txt": "quiet\n"})
    _lock(tmp_path, "locked.txt")

    r = _run_scan(tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 0)
    m = _UNREADABLE_LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group(1) == "1", r.stdout
    assert _named_paths(m.group(2)) == ["locked.txt"], r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_cli_empty_git_repo_exits_3_with_no_coverage_line(tmp_path):
    """Nothing tracked is evidence of nothing scanned, not of a clean tree.

    Pre-fix: exit 0 and `clean`.
    """
    _init_git_repo(tmp_path)  # no files, nothing tracked

    r = _run_scan(tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (0, 0)
    assert _NO_COVERAGE_RE.search(r.stdout), r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_cli_repo_of_only_skip_files_exits_3(tmp_path):
    """Every tracked file filtered out by SKIP_FILES is the empty-candidate case.

    Pre-fix: exit 0 and `clean`.
    """
    _commit_files(tmp_path, {"LICENSE": "MIT\n", "scan-leaks.py": "# self-skip\n"})

    r = _run_scan(tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (0, 0)
    assert _NO_COVERAGE_RE.search(r.stdout), r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_skipped_binary_files_are_named(tmp_path):
    """A UTF-16 file and a Markdown file with a stray NUL are both named.

    Pre-fix: counted in the summary line but never named, so a real binary is
    indistinguishable from a text file the scanner stopped reading.
    """
    _commit_files(tmp_path, {
        "utf16.txt": "hello".encode("utf-16"),
        "README.md": b"# title\x00\n",
        "clean.txt": "clean\n",
    })

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 2)
    m = _SKIPPED_NAMES_RE.search(r.stdout)
    assert m, r.stdout
    assert _named_paths(m.group(1)) == ["README.md", "utf16.txt"], r.stdout


def test_skipped_binary_naming_caps_at_20(tmp_path):
    """Twenty paths named, then '(+5 more)' for the rest.

    Pre-fix: no naming line at all.
    """
    _commit_files(tmp_path, {f"bin/b{i:02d}.bin": b"\x00bin" for i in range(25)})

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == (0, 25)
    m = _SKIPPED_NAMES_RE.search(r.stdout)
    assert m, r.stdout
    assert _named_paths(m.group(1)) == [f"bin/b{i:02d}.bin" for i in range(20)], r.stdout
    assert m.group(1).rstrip().endswith("(+5 more)"), r.stdout


def test_positive_control_clean_tree_exits_0_with_line_a_once(tmp_path):
    """A clean, non-empty tree exits 0 and prints line A exactly once.

    Positive control: green on both the pre-fix and the fixed scanner. Its
    discriminating mutation is M5 (line A reformatted).
    """
    _commit_files(tmp_path, {"clean.py": "x = 1\n"})

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("scan-leaks: scanned 1, skipped-binary 0\n") == 1, r.stdout
    assert "scan-leaks: clean" in r.stdout
    # "named only when non-zero": a clean tree prints no not-a-file line.
    assert not _NOT_A_FILE_LINE_RE.search(r.stdout), r.stdout


def test_positive_control_leak_takes_precedence_over_coverage(tmp_path):
    """A planted pattern exits 1 even when coverage is incomplete.

    Exit 3 is only for a run with no findings; the dirty tree is the primary
    fact and the coverage lines still print. Pre-fix: exits 1 too (the
    exit-code half is a control); the coverage-line half reddens pre-fix,
    because no unreadable line exists.
    """
    _commit_files(tmp_path, {
        "hit.js": "path = '" + "kg_protocol" + "_for_ai" + "/x'\n",
        "locked.txt": "quiet\n",
    })
    is_root = os.geteuid() == 0
    if not is_root:
        _lock(tmp_path, "locked.txt")

    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    if not is_root:
        assert _UNREADABLE_LINE_RE.search(r.stdout), r.stdout


def test_tracked_directory_symlink_is_scanned_not_unreadable(tmp_path):
    """A tracked symlink to a directory is scanned by its link text.

    It is not unreadable and not a coverage hole: readlink() text is scanned
    in both passes, so the symlink counts in ``scanned`` and no not-a-file
    line is printed.  Pre-#4805-round-4 it was classified not-a-file and named
    on that line.  M2 (lstat reverted to stat) classifies it by its target
    again, so it lands in not_a_file_paths and the CLI prints the not-a-file
    line -> this reddens.
    """
    (tmp_path / "real-dir").mkdir()
    _commit_files(tmp_path, {"real/member.py": "// clean\n"})
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "engine").symlink_to("../real-dir")
    subprocess.run(["git", "add", "tools/engine"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "symlink"], cwd=tmp_path, check=True)

    report = _mod.scan(tmp_path, [], use_git=True)
    assert report.candidates == 2
    assert report.not_a_file_paths == []
    assert report.unreadable_paths == []
    assert report.scanned == 2

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not _UNREADABLE_LINE_RE.search(r.stdout), r.stdout
    assert not _NOT_A_FILE_LINE_RE.search(r.stdout), r.stdout
    assert _summary(r.stdout) == (2, 0), r.stdout


def test_scan_home_paths_reports_unreadable_but_not_symlinks(tmp_path):
    """scan_home_paths returns (findings, unreadable_paths); a symlink is
    scanned by its link text (clean here), and an unreadable candidate is
    reported by path.

    Pre-#4805-round-4 the directory symlink was filtered as not-a-file; now it
    is readlink()-scanned, so a clean relative link text produces no finding
    and no unreadable entry.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-0000 file; the unreadable arm is unreachable")

    (tmp_path / "real-dir").mkdir()
    _commit_files(tmp_path, {"real/member.py": "clean\n"})
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "engine").symlink_to("../real-dir")
    (tmp_path / "locked.txt").write_text("quiet\n")
    subprocess.run(["git", "add", "tools/engine", "locked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "symlink and locked"],
                   cwd=tmp_path, check=True)
    _lock(tmp_path, "locked.txt")

    findings, unreadable = _mod.scan_home_paths(tmp_path, use_git=True)
    assert findings == []
    assert unreadable == ["locked.txt"]


# ---------------------------------------------------------------------------
# Test 17: enumeration and classification accounting (#4805 round 3)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - one classifier, `_classify(path)`, gates BOTH passes: stat follows
#     symlinks; FileNotFoundError -> missing; any other OSError -> unreadable;
#     a stat that succeeds but is not a regular file -> not-a-file;
#     otherwise a file.  A path the scanner cannot inspect is a coverage hole,
#     never a crash and never a silent drop.
#   - candidates is the ENUMERATED set (git ls-files, or os.walk output) minus
#     SKIP_DIRS components and SKIP_FILES names — BEFORE the file-type filter.
#   - the invariant scans every bucket:
#         scanned + len(skipped_binary_paths) + len(unreadable_paths)
#                 + len(missing_paths) + len(not_a_file_paths) == candidates
#   - os.walk takes an onerror that records the failing directory (posix,
#     trailing "/") as unreadable and counts it as one enumerated candidate —
#     the subtree is unknown, so it is one coverage hole, not zero.
#   - coverage is incomplete when unreadable or missing is non-empty, or
#     candidates == 0.  not-a-file alone is NOT incomplete (a symlink to a
#     directory carries no text), but it is named when non-zero.
#   - stdout gains "scan-leaks: missing K (coverage incomplete): <paths>" and
#     "scan-leaks: not-a-file N (not scanned): <paths>", 20-path cap.  The
#     frozen "scan-leaks: scanned N, skipped-binary M" line is unchanged.

_MISSING_LINE_RE = re.compile(
    r"^scan-leaks: missing (\d+) \(coverage incomplete\): (.*)$", re.M
)
_NOT_A_FILE_LINE_RE = re.compile(
    r"^scan-leaks: not-a-file (\d+) \(not scanned\): (.*)$", re.M
)


def test_cli_eacces_on_a_non_skip_candidate_exits_3_without_traceback(tmp_path):
    """Cell 1: a non-SKIP_FILES tracked candidate under a chmod-000 directory.

    Pre-fix: Path.is_file() re-raises PermissionError -> traceback, exit 1,
    which every consumer reads as "leaks found".  Post-fix: the path is
    classified unreadable, named, and the run exits 3 with no traceback.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-000 directory; the EACCES arm is unreachable")

    _commit_files(tmp_path, {
        "ok.txt": "clean\n",
        "lockeddir/secret.txt": "quiet\n",
    })
    (tmp_path / "lockeddir").chmod(0o000)
    try:
        r = _run_scan(tmp_path)
    finally:
        (tmp_path / "lockeddir").chmod(0o755)

    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 0)
    m = _UNREADABLE_LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group(1) == "1", r.stdout
    assert _named_paths(m.group(2)) == ["lockeddir/secret.txt"], r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_cli_eacces_on_a_skip_files_member_scanned_by_the_home_pass(tmp_path):
    """Cell 2: a SKIP_FILES member whose STAT is denied by a 000 parent dir.

    The pattern pass drops LICENSE by name before touching the filesystem, so
    the home pass is the only one that can see it — a coverage hole the pattern
    pass cannot report.  Pre-fix: traceback at scan_home_paths' is_file().
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-000 directory; the EACCES arm is unreachable")

    _commit_files(tmp_path, {
        "ok.txt": "clean\n",
        "lockeddir/LICENSE": "MIT\n",
    })
    (tmp_path / "lockeddir").chmod(0o000)
    try:
        r = _run_scan(tmp_path)
    finally:
        (tmp_path / "lockeddir").chmod(0o755)

    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 0)
    m = _UNREADABLE_LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group(1) == "1", r.stdout
    assert _named_paths(m.group(2)) == ["lockeddir/LICENSE"], r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_locked_license_is_reported_only_by_the_home_pass(tmp_path):
    """Item 3's discriminating cell: the ONLY unreadable file is a SKIP_FILES
    member, in an otherwise clean tree.

    The pattern pass never sees LICENSE by name; the home pass is its sole
    reporter.  This cell is GREEN pre-fix (the home pass already has a
    read_bytes() OSError arm) — its discriminating mutation is M3, dropping
    `home_unreadable` from the union, which turns this into `clean`, exit 0.
    That is the half-branch the review found no cell for.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-0000 file; the unreadable arm is unreachable")

    _commit_files(tmp_path, {"ok.txt": "clean\n", "LICENSE": "MIT\n"})
    _lock(tmp_path, "LICENSE")

    r = _run_scan(tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 0)
    m = _UNREADABLE_LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group(1) == "1", r.stdout
    assert _named_paths(m.group(2)) == ["LICENSE"], r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_tracked_but_missing_is_named_and_exits_3(tmp_path):
    """Cell 3: a tracked file absent from the worktree, deletion not staged.

    The pre-commit path reads the index, so the committed tree still carries
    this file's content.  Pre-fix: dropped by is_file(), `scanned 1`, `clean`,
    exit 0 — the file is neither scanned, named, nor counted.
    """
    _commit_files(tmp_path, {"ok.txt": "clean\n", "gone.txt": "staged content\n"})
    (tmp_path / "gone.txt").unlink()  # deliberately NOT staged

    r = _run_scan(tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 0)
    m = _MISSING_LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group(1) == "1", r.stdout
    assert _named_paths(m.group(2)) == ["gone.txt"], r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_not_a_file_is_counted_and_named_not_counted_as_incomplete(tmp_path):
    """Cell 4: a tracked path that is not a regular file on disk (a FIFO).

    Git tracks only regular files, symlinks and gitlinks, so the fixture
    commits a regular file and replaces it with a FIFO after the commit
    (deliberately unstaged).  lstat sees S_ISFIFO: counted as a candidate,
    classified not-a-file, named, and it does NOT make an otherwise clean tree
    exit 3.  A symlink no longer lands here: since round 4 a symlink's link
    text is scanned (see the symlink cells below).
    """
    _commit_files(tmp_path, {"real/member.py": "// clean\n", "pipe": "placeholder\n"})
    (tmp_path / "pipe").unlink()
    os.mkfifo(tmp_path / "pipe")

    report = _mod.scan(tmp_path, [], use_git=True)
    assert report.candidates == 2
    assert report.not_a_file_paths == ["pipe"]
    assert report.unreadable_paths == []
    assert report.missing_paths == []
    assert report.scanned == 1
    assert (report.scanned + len(report.skipped_binary_paths)
            + len(report.unreadable_paths) + len(report.missing_paths)
            + len(report.not_a_file_paths)) == 2

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 0)
    m = _NOT_A_FILE_LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group(1) == "1", r.stdout
    assert _named_paths(m.group(2)) == ["pipe"], r.stdout
    assert "scan-leaks: clean" in r.stdout


def test_cli_use_git_false_unsearchable_directory_is_reported(tmp_path):
    """Cell 5: the os.walk path (no git repo) with a chmod-000 directory.

    os.walk without onerror prunes the directory silently: the subtree is never
    seen and the counts reconcile against a candidate set that already lost it.
    Post-fix: the directory is enumerated as one candidate, recorded unreadable
    with a trailing "/", named, and the run exits 3.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-000 directory; the EACCES arm is unreachable")

    # Deliberately NOT a git repo: both passes fall back to os.walk.
    (tmp_path / "ok.txt").write_text("clean\n")
    (tmp_path / "lockeddir").mkdir()
    (tmp_path / "lockeddir" / "f.txt").write_text("clean\n")
    (tmp_path / "lockeddir").chmod(0o000)
    try:
        report = _mod.scan(tmp_path, [], use_git=False)
        r = _run_scan(tmp_path)
    finally:
        (tmp_path / "lockeddir").chmod(0o755)

    assert report.scanned == 1
    assert report.unreadable_paths == ["lockeddir/"]
    assert report.candidates == 2  # ok.txt + the one unenumerated subtree
    assert (report.scanned + len(report.skipped_binary_paths)
            + len(report.unreadable_paths) + len(report.missing_paths)
            + len(report.not_a_file_paths)) == report.candidates

    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 0)
    m = _UNREADABLE_LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group(1) == "1", r.stdout
    assert _named_paths(m.group(2)) == ["lockeddir/"], r.stdout
    assert "scan-leaks: clean" not in r.stdout


def test_invariant_across_all_five_buckets(tmp_path):
    """Cell 6: one tree, every bucket — scanned, binary, unreadable, missing,
    not-a-file — and the invariant closes on the fixture's literal count.

    Candidates: ok.txt, blob.bin, locked.txt, gone.txt, pipe = 5.  The
    not-a-file candidate is a committed regular file replaced on disk by a
    FIFO (git cannot track a FIFO); a symlink no longer lands in that bucket
    since #4805 round 4.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a chmod-0000 file; the unreadable arm is unreachable")

    _commit_files(tmp_path, {
        "ok.txt": "clean\n",
        "blob.bin": b"\x00binary",
        "locked.txt": "quiet\n",
        "gone.txt": "staged content\n",
        "pipe": "placeholder\n",
    })
    (tmp_path / "pipe").unlink()
    os.mkfifo(tmp_path / "pipe")
    _lock(tmp_path, "locked.txt")
    (tmp_path / "gone.txt").unlink()

    report = _mod.scan(tmp_path, [], use_git=True)
    assert report.scanned == 1
    assert report.skipped_binary_paths == ["blob.bin"]
    assert report.unreadable_paths == ["locked.txt"]
    assert report.missing_paths == ["gone.txt"]
    assert report.not_a_file_paths == ["pipe"]
    assert report.candidates == 5
    assert (report.scanned + len(report.skipped_binary_paths)
            + len(report.unreadable_paths) + len(report.missing_paths)
            + len(report.not_a_file_paths)) == 5

    r = _run_scan(tmp_path)
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 3, r.stdout + r.stderr
    assert _summary(r.stdout) == (1, 1)
    assert _UNREADABLE_LINE_RE.search(r.stdout), r.stdout
    assert _MISSING_LINE_RE.search(r.stdout), r.stdout
    assert _NOT_A_FILE_LINE_RE.search(r.stdout), r.stdout


# ---------------------------------------------------------------------------
# Test 18: symlink link text, internal errors, path escaping (#4805 round 4)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - classification uses os.lstat, never stat: ("symlink", None) for a
#     symlink, ("file", None) for a regular file, ("not-a-file", None) for
#     anything else non-regular (FIFO, socket, device, gitlink), ("missing",
#     path) only when the path itself is gone, ("unreadable", path) for any
#     other OSError.
#   - a symlink's published content is its LINK TEXT: os.readlink(path),
#     scanned by BOTH passes (the pattern pass and the absolute-home-path
#     pass).  The link is NEVER followed.
#   - consequences: a dangling symlink is a covered scan (not `missing`); a
#     directory symlink is scanned (not not-a-file); a symlink and its target
#     both tracked are not double-counted, because the link contributes its
#     text and the target contributes its content once, at its own path.
#   - an unexpected exception is caught at the top of main(), printed as ONE
#     stderr line `scan-leaks: internal error: <ExceptionType>`, and exits 2 —
#     never the exit 1 every consumer reads as "a leak was found".
#   - every printed path is escaped: `\n` -> `\\n`, `\r` -> `\\r`, other C0
#     controls -> `\\xNN`, so a newline in a tracked name cannot forge a
#     second frozen summary line.  The frozen line itself is byte-identical.

_SUMMARY_ANCHORED_RE = re.compile(
    r"^scan-leaks: scanned (\d+), skipped-binary (\d+)$", re.M
)


def _commit_symlinks(root: Path, links: dict[str, str]) -> None:
    """Create each {link_path: link_text} as a symlink and commit it."""
    for rel, text in links.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(text, p)
    subprocess.run(["git", "add", *links], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "symlinks"], cwd=root, check=True)


def _home_target_dir() -> Path:
    """The seat's home directory, when it is in the scanner's disallowed class.

    A directory symlink whose link text is an ABSOLUTE home path needs a real
    directory at that path; the only such path that exists on every seat is
    the home directory itself.  Skips on a seat whose home is not of the form
    /home/<name> or /Users/<name> (or whose segment is in the allowed set).
    """
    home = Path.home()
    if not _mod._home_path_class().disallowed_home_path_regex().search(str(home)):
        pytest.skip("the seat's home path is not in the scanner's home-path class")
    return home


def test_cell1_directory_symlink_home_link_text_is_a_finding(tmp_path):
    """Cell 1: a directory symlink whose published link text is a home path.

    Pre-fix: stat() follows the link, the directory target is classified
    not-a-file, and the run exits 0 with `clean` having read no link text.
    """
    home = _home_target_dir()
    _commit_files(tmp_path, {"real/member.py": "// clean\n"})
    _commit_symlinks(tmp_path, {"desk-link": str(home)})

    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "desk-link:1" in r.stderr, r.stderr
    assert "absolute home path" in r.stderr, r.stderr
    assert not _NOT_A_FILE_LINE_RE.search(r.stdout), r.stdout


def test_cell2_file_symlink_home_link_text_is_a_finding(tmp_path):
    """Cell 2: a file symlink whose published link text is a home path.

    Pre-fix: the link is followed to its target, the link text is never read,
    and the run exits 0 (`clean`) — or, for a dangling target, exits 3
    (`missing`).  Either way the published bytes read as no leak.
    """
    _commit_files(tmp_path, {"clean.txt": "clean\n"})
    _commit_symlinks(
        tmp_path, {"report-link": _home("zorblax") + "desk/report.txt"},
    )

    r = _run_scan(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "report-link:1" in r.stderr, r.stderr
    assert "absolute home path" in r.stderr, r.stderr
    assert "clean.txt" not in r.stderr, r.stderr


def test_cell3_relative_symlink_is_scanned_not_not_a_file(tmp_path):
    """Cell 3: a symlink with a clean relative link text is scanned.

    It counts in `scanned` and is NOT a not-a-file candidate.  Pre-fix: the
    directory target makes it not-a-file, so scanned is 1 and the not-a-file
    line names it.
    """
    (tmp_path / "real-dir").mkdir()
    _commit_files(tmp_path, {"real/member.py": "// clean\n"})
    _commit_symlinks(tmp_path, {"tools/engine": "../real-dir"})

    report = _mod.scan(tmp_path, [], use_git=True)
    assert report.scanned == 2
    assert report.not_a_file_paths == []
    assert report.unreadable_paths == []
    assert report.missing_paths == []

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == (2, 0)
    assert not _NOT_A_FILE_LINE_RE.search(r.stdout), r.stdout


def test_cell4_dangling_symlink_is_scanned_and_clean(tmp_path):
    """Cell 4: a dangling tracked symlink is a COVERED scan of its link text.

    It is not `missing`, does not make coverage incomplete, and the run exits
    0.  (This replaces the round-3 cell that asserted exit 3.)  Pre-fix: the
    path is `missing`, scanned is 1, and the run exits 3.
    """
    _commit_files(tmp_path, {"ok.txt": "clean\n"})
    _commit_symlinks(tmp_path, {"broken": "no-such-target.txt"})

    report = _mod.scan(tmp_path, [], use_git=True)
    assert report.scanned == 2
    assert report.missing_paths == []

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == (2, 0)
    assert not _MISSING_LINE_RE.search(r.stdout), r.stdout
    assert "scan-leaks: clean" in r.stdout


_TOKEN5 = "vrelq8104"


def test_cell5_symlink_target_is_not_double_counted(tmp_path):
    """Cell 5: a symlink and its target both tracked are scanned once each.

    The link contributes its (clean) link text; the target contributes its
    content at its own real path.  A planted pattern in the target therefore
    produces EXACTLY ONE finding, at the target's path.  Pre-fix: the link is
    followed, so the same bytes produce a second finding at the link's path.
    """
    _commit_files(tmp_path, {"target.py": f"{_TOKEN5}\n"})
    _commit_symlinks(tmp_path, {"link.py": "target.py"})
    (tmp_path / ".scan-leaks-local.json").write_text(json.dumps(
        {"patterns": [{"regex": _TOKEN5, "label": "planted token"}]}
    ))

    r = _run_scan_with_roster(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    hit_lines = [l for l in r.stderr.splitlines() if l.startswith("  ")]
    assert len(hit_lines) == 1, r.stderr
    assert "target.py:1" in hit_lines[0], r.stderr


def test_cell6_internal_error_exits_2_not_1(tmp_path):
    """Cell 6: an unexpected exception is exit 2 with one `internal error` line.

    Pre-fix: the exception escapes main(), Python prints a traceback, and the
    process exits 1 — which every consumer translates as "a leak was found".
    """
    _commit_files(tmp_path, {"ok.txt": "clean\n"})
    wrapper = tmp_path.parent / f"{tmp_path.name}-crash-wrapper.py"
    wrapper.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('sl_crash', {str(SCAN_LEAKS)!r})\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "def boom(path):\n"
        "    raise RuntimeError('injected for cell 6')\n"
        "m._classify = boom\n"
        f"sys.argv = ['scan-leaks.py', '--root', {str(tmp_path)!r}, "
        f"'--config-path', {str(tmp_path / 'no-config.json')!r}]\n"
        "m.main()\n"
    )
    try:
        r = subprocess.run([sys.executable, str(wrapper)],
                           capture_output=True, text=True)
    finally:
        wrapper.unlink()

    assert r.returncode == 2, r.stdout + r.stderr
    assert "scan-leaks: internal error: RuntimeError" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "potential leak" not in r.stderr, r.stderr


def test_cell7_newline_in_filename_is_escaped_and_line_once(tmp_path):
    """Cell 7: a newline in a tracked name cannot split a printed line.

    The frozen line appears exactly once on stdout and the name is escaped.
    Pre-fix: the skipped-binary naming line carries the raw newline and splits
    into two lines.
    """
    evil = "weird\nname.bin"
    _commit_files(tmp_path, {"ok.txt": "clean\n", evil: b"\x00binary"})

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("scan-leaks: scanned ") == 1, r.stdout
    assert "weird\\nname.bin" in r.stdout, r.stdout
    assert "weird\nname.bin" not in r.stdout, r.stdout


def test_cell7b_forged_summary_in_filename_cannot_add_a_line(tmp_path):
    """Cell 7b: the threat B3 names — a name that IS a frozen summary line.

    The escaped name keeps the text mid-line, so the anchored regex the sweep
    uses (and the scanner's own frozen-line freeze) still sees exactly one
    summary.  Pre-fix: the raw newline puts a second anchored match on stdout.
    """
    forged = "x\nscan-leaks: scanned 9999, skipped-binary 0"
    _commit_files(tmp_path, {"ok.txt": "clean\n", forged: b"\x00binary"})

    r = _run_scan(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_SUMMARY_ANCHORED_RE.findall(r.stdout)) == 1, r.stdout
    assert "x\\nscan-leaks: scanned 9999" in r.stdout, r.stdout
    assert not any(line.startswith("scan-leaks: scanned 9999")
                   for line in r.stdout.splitlines()), r.stdout


def test_cell8_positive_controls(tmp_path):
    """Cell 8: an ordinary clean tree exits 0 with the frozen line once; a
    tree with a leaked pattern exits 1.  (Green before and after — a control,
    not a red.)"""
    clean = tmp_path / "clean-tree"
    clean.mkdir()
    _commit_files(clean, {"ok.txt": "clean\n"})
    r_clean = _run_scan(clean)
    assert r_clean.returncode == 0, r_clean.stdout + r_clean.stderr
    assert r_clean.stdout.count("scan-leaks: scanned ") == 1, r_clean.stdout

    leaky = tmp_path / "leaky-tree"
    leaky.mkdir()
    _commit_files(leaky, {"doc.md": f"{_TOKEN5}\n"})
    (leaky / ".scan-leaks-local.json").write_text(json.dumps(
        {"patterns": [{"regex": _TOKEN5, "label": "planted token"}]}
    ))
    r_leaky = _run_scan_with_roster(leaky)
    assert r_leaky.returncode == 1, r_leaky.stdout + r_leaky.stderr
    assert _summary(r_leaky.stdout) == (1, 0)
    assert "doc.md:1" in r_leaky.stderr, r_leaky.stderr


# ---------------------------------------------------------------------------
# Test 19: the class module loads inside main()'s guard, the os.walk fallback
#          scans a directory symlink, and C1/U+2028/U+2029 escape (#4805 r5)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - the shared home-path class module is loaded from INSIDE main()'s try
#     (through a call made from _run()), never at import time: a scanner
#     deployed without its sibling `engine/home_path_class.py` exits 2 with
#     one `scan-leaks: internal error: ImportError` line on stderr, prints no
#     summary line and no traceback.  The loader's search order and message
#     are unchanged.
#   - in BOTH os.walk loops, after the SKIP_DIRS filter and BEFORE descent,
#     each `dirs` entry that is a symlink becomes a candidate (classified
#     `symlink` and scanned by its os.readlink text) and is removed from
#     `dirs`, so the walk neither descends it nor loses it.  A real directory
#     is untouched.
#   - `_escape_path` also escapes C1 controls (U+0080-U+009F -> \\xNN) and
#     U+2028/U+2029 (-> \\u2028/\\u2029): every one of them is a line break
#     to str.splitlines(), so a tracked name carrying one cannot split a
#     printed line.  The frozen summary line is byte-identical.


def _non_git_tree_with_dir_link(root: Path, link_text: str) -> None:
    """A non-git tree: one real file plus a symlink to a sibling directory.

    The link target is a real directory inside the tree, so os.walk lists the
    link in ``dirs`` (and, with followlinks=False, does not descend it).  The
    tree is deliberately not a git repository: both passes fall back to
    os.walk, which is the enumeration path these cells exercise.
    """
    (root / "real").mkdir(parents=True)
    (root / "real" / "member.txt").write_text("clean\n")
    os.symlink(link_text, root / "dir-link")


def test_r5_item1_missing_sibling_module_exits_2_not_1(tmp_path):
    """Cell: a scanner deployed without its sibling home-path class module.

    Pre-fix: `_hpc = _load_home_path_class()` runs at import, before main()'s
    try, so the process dies with a traceback and exit 1 -- which every
    consumer reads as "leaks found", and which the module docstring says
    cannot happen.  Post-fix: the load is reached from inside main()'s try,
    so it is one stderr line and exit 2.
    """
    # Nested one level deeper than needed so the loader's second candidate
    # (<here>.parent.parent / "build" / "engine" / "home_path_class.py)
    # lands under tmp_path/a/build, which nothing creates: the "missing
    # sibling" condition is provable rather than merely likely.
    lone = tmp_path / "a" / "b" / "lone"
    lone.mkdir(parents=True)
    (lone / "scan-leaks.py").write_bytes(SCAN_LEAKS.read_bytes())

    r = subprocess.run(
        [sys.executable, str(lone / "scan-leaks.py"),
         "--root", str(lone), "--config-path", str(lone / "no-config.json")],
        capture_output=True, text=True,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "scan-leaks: internal error: ImportError" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "scan-leaks: scanned" not in r.stdout, r.stdout


def test_r5_item2a_walk_fallback_scans_a_directory_symlink_clean_text(tmp_path):
    """Cell: a directory symlink with clean relative link text is scanned.

    Pre-fix: os.walk lists it in ``dirs``, followlinks=False does not descend
    it, and it is never a file candidate -- so its link text is never scanned
    (scanned 1, not 2): the hole this cell closes.  A walk that DID descend
    the link would enumerate member.txt twice (candidates 3).
    """
    root = tmp_path / "walk-clean"
    _non_git_tree_with_dir_link(root, "real")

    report = _mod.scan(root, [], use_git=False)
    assert report.scanned == 2, report
    assert report.candidates == 2, report
    assert report.not_a_file_paths == [], report
    assert report.unreadable_paths == [], report

    r = _run_scan(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == (2, 0), r.stdout
    assert "scan-leaks: clean" in r.stdout, r.stdout


def test_r5_item2b_walk_fallback_scans_a_directory_symlink_home_text(tmp_path):
    """Cell: a directory symlink whose link text is an absolute home path.

    Pre-fix: the walk never enumerates it, so the host path sits in the
    published link text and the run exits 0 `clean` (scanned 1, not 2).
    Post-fix: both passes scan the link text and the finding is at the link's
    own path.  A descended link would inflate the scanned count instead.
    """
    home = _home_target_dir()
    root = tmp_path / "walk-home"
    _non_git_tree_with_dir_link(root, str(home))

    r = _run_scan(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "dir-link:1" in r.stderr, r.stderr
    assert "absolute home path" in r.stderr, r.stderr
    assert _summary(r.stdout) == (2, 0), r.stdout


# U+2028 is a line break to str.splitlines() (as is the C1 U+0085, written
# \x85 below); a name carrying either must not split a printed line.
_R5_LINE_SEP = chr(0x2028)


def test_r5_item3_c1_and_unicode_line_breaks_are_escaped(tmp_path):
    """Cell: U+2028 and C1 U+0085 in tracked names cannot split stdout.

    Both are line breaks to str.splitlines(), and pre-fix neither is escaped,
    so the one skipped-binary naming line splits into three: stdout gains two
    lines and a consumer splitting stdout can read a forged second line.
    Post-fix the count is the four lines this fixture prints anyway: the
    advisory, the frozen summary, one naming line, and `clean`.
    """
    root = tmp_path / "escape-tree"
    root.mkdir()
    _commit_files(root, {
        "ok.txt": "clean\n",
        "evil" + _R5_LINE_SEP + "name.bin": b"\x00binary",
        "evil\x85name.bin": b"\x00binary",
    })

    r = _run_scan(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_SUMMARY_ANCHORED_RE.findall(r.stdout)) == 1, repr(r.stdout)
    assert len(r.stdout.splitlines()) == 4, repr(r.stdout)
    assert "evil\\u2028name.bin" in r.stdout, repr(r.stdout)
    assert "evil\\x85name.bin" in r.stdout, repr(r.stdout)
    assert _R5_LINE_SEP not in r.stdout, repr(r.stdout)
    assert "\x85" not in r.stdout, repr(r.stdout)

