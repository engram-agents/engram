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
    allowed = _mod.ALLOWED_HOME_SEGMENTS
    assert {"someone", "agent-alice", "Shared", "agents-shared"} <= set(allowed)
    assert all(isinstance(why, str) and why.strip() for why in allowed.values())
    assert "open decision" in allowed["agents-shared"]


def test_real_tree_has_no_home_path_findings():
    """The whole-tree gate: the committed tree carries no real home path."""
    findings = _mod.scan_home_paths(REPO_ROOT)
    assert findings == [], "\n".join(f"{f}:{n} {seg}" for f, n, seg in findings)


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
