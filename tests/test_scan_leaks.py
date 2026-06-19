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
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_LEAKS = REPO_ROOT / "src" / "engram" / "tools" / "scan-leaks.py"
EXAMPLE_CONFIG = REPO_ROOT / "docs" / ".scan-leaks-local.json.example"

# Insert tools/ parent so we can import scan-leaks as a module.
sys.path.insert(0, str(REPO_ROOT / "src" / "engram" / "tools"))

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
# Test 11: COMMITTED_PATTERNS includes the private dev-repo URL guard
# ---------------------------------------------------------------------------

def test_committed_patterns_includes_dev_repo_url_guard():
    """The private dev-repo URL pattern must be in COMMITTED_PATTERNS.

    Verifies two properties:
      (1) A line containing the org-qualified private-repo URL is matched by
          the pattern (the guard fires on the broken public install link form).
      (2) A bare dev-path like ~/engram-alpha is NOT matched (RULE C paths are
          intentionally kept and must not false-positive).

    Literal strings that would trigger the scanner on the dev repo are
    constructed from parts so no single line contains the raw pattern.
    Same technique used elsewhere in this file for the structural pattern
    (see test_config_absent_structural_pattern_still_fires).
    """
    # The guard label — assembled from parts so no single line in this file
    # contains the raw org/repo pattern the scanner checks for.
    _org = "engram-agents"
    _private = "engram-alpha"
    _org_repo = _org + "/" + _private   # noqa: assembled below the split boundary

    guard_label_prefix = "private dev-repo URL — must be "
    guard_label = guard_label_prefix + _org + "/engram in public snapshot"
    matching_patterns = [
        (pat, label) for pat, label in _mod.COMMITTED_PATTERNS
        if label == guard_label
    ]
    assert matching_patterns, (
        f"No COMMITTED_PATTERN with label {guard_label!r} found. "
        "The private dev-repo URL guard must be added to COMMITTED_PATTERNS."
    )
    pat, _ = matching_patterns[0]

    # (1) Must match the org-qualified URL form (the broken public install link).
    # Assembled from parts so this line itself doesn't trip the scanner on dev.
    sample_url_line = "git clone https://github.com/" + _org_repo + ".git"
    assert pat.search(sample_url_line), (
        "Pattern must match a line containing the org-qualified private-repo URL; "
        f"line: {sample_url_line!r}"
    )

    # (2) Must NOT match a bare dev-path (RULE C — these are intentionally kept).
    sample_dev_path = "return os.path.expanduser('~/" + _private + "')"
    assert not pat.search(sample_dev_path), (
        "Pattern must NOT match a bare ~/engram-alpha dev path (RULE C); "
        f"line: {sample_dev_path!r}"
    )


# ---------------------------------------------------------------------------
# Test 12: end-to-end — dev-repo-URL guard fires on a committed tracked file
# ---------------------------------------------------------------------------

def test_dev_repo_url_guard_end_to_end_tracked_commit(tmp_path):
    """End-to-end: committing a file with the private dev-repo URL causes
    the scanner to exit non-zero and report the leak.

    This exercises the full file-enumeration + pattern-matching pipeline so
    that a future refactor of how files are enumerated cannot break the gate
    while the pattern-only test (test_committed_patterns_includes_dev_repo_url_guard)
    stays green.

    Modeled on test_repo_mode_detects_tracked_hit (Test 10).
    The URL literal is assembled from parts to avoid self-tripping the scanner
    on this test file — same technique used throughout this file.
    """
    _org = "engram-agents"
    _private = "engram-alpha"
    dev_repo_url = "https://github.com/" + _org + "/" + _private

    _init_git_repo(tmp_path)

    # Commit a file that contains the private dev-repo URL (the leak to detect).
    bad = tmp_path / "bad_install.sh"
    bad.write_text("git clone " + dev_repo_url + ".git\n")
    subprocess.run(["git", "add", "bad_install.sh"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "plant dev-repo-url leak"], cwd=tmp_path, check=True)

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
    assert result.returncode != 0, (
        "Scanner must exit non-zero when a tracked committed file contains the "
        "private dev-repo URL.\n"
        f"returncode: {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    # The label from COMMITTED_PATTERNS — assembled from parts to avoid self-trip.
    expected_label = "private dev-repo URL — must be " + _org + "/engram in public snapshot"
    assert expected_label in result.stderr, (
        f"Expected leak label {expected_label!r} in stderr.\n"
        f"stderr: {result.stderr!r}"
    )
    assert "bad_install.sh" in result.stderr, (
        "Expected the filename bad_install.sh to appear in the leak report.\n"
        f"stderr: {result.stderr!r}"
    )
