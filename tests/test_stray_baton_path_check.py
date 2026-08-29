"""Tests for tools/stray_baton_path_check.py — stale pre-cutover baton-path
warn-gate (#2316).

Coverage:
  1. The stale path (no /forum/ segment) fires when not allowlisted.
  2. The live path (/forum/ segment present) never fires — false-positive
     avoidance is the whole point, since the two strings share a long prefix.
  3. An allowlisted (file, line-fragment) pair is suppressed.
  4. The allowlist does NOT blanket-exempt the whole file — a stale
     reference in the SAME file, on a DIFFERENT line not matching the
     allowlisted fragment, still fires.
  5. JSON format shape.
  6. Only TRACKED files are scanned — an untracked/gitignored file (e.g. a
     fairy-dispatch worktree under .claude/worktrees/, or a scratch
     artifact under .claude/agent-scratch/, both real and present during
     normal work) is never scanned, even though it sits on disk under the
     repo root. This is the fix for a real bug this test suite itself
     caught: an earlier draft walked the raw filesystem and picked up
     stale-path hits from exactly those two gitignored directories.
  7. This scanner's own source and its own test fixture file are excluded
     from the scan wholesale — both inherently carry the literal stale
     string throughout (as regex source, documentation, and synthetic test
     data). Real bug this test suite itself caught, post-commit: these two
     files were untracked during local pre-commit verification (`git
     ls-files` doesn't see untracked files), so the hygiene test below
     passed against a scope that excluded them BY ACCIDENT rather than by
     design — reviewer-fairy caught it once `git add` made them tracked,
     the exact self-referential blind spot this test now covers directly.
  8. Hygiene: running the real scan against this repo currently reports
     zero matches beyond the three known, deliberate historical notes
     (regression guard — a stray ref should never quietly creep back in).
"""

import importlib.util
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "stray_baton_path_check.py"

_spec = importlib.util.spec_from_file_location("stray_baton_path_check", TOOL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

scan_file = _mod.scan_file
find_target_files = _mod.find_target_files
format_json = _mod.format_json
ALLOWLIST = _mod.ALLOWLIST


# ---------------------------------------------------------------------------
# Test 1 + 2: stale path fires, live path does not
# ---------------------------------------------------------------------------

def test_stale_path_fires(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("batons live at /home/agents-shared/projects/pool-x.md\n")
    matches = scan_file(f, tmp_path)
    assert len(matches) == 1
    assert matches[0]["file"] == "x.md"
    assert matches[0]["line_number"] == 1


def test_live_path_does_not_fire(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("batons live at /home/agents-shared/forum/projects/pool-x.md\n")
    assert scan_file(f, tmp_path) == []


# ---------------------------------------------------------------------------
# Test 3 + 4: allowlist is exact (file, line-fragment), not whole-file
# ---------------------------------------------------------------------------

def test_allowlisted_line_is_suppressed():
    rel_path, frag = ALLOWLIST[0]
    real_file = REPO_ROOT / rel_path
    assert real_file.is_file(), f"allowlist references a nonexistent file: {rel_path}"
    matches = scan_file(real_file, REPO_ROOT)
    assert not any(frag in m["line"] for m in matches), (
        f"the allowlisted line ({frag!r}) should be suppressed, but appears in matches: {matches}"
    )


def test_allowlist_does_not_blanket_exempt_the_whole_file(tmp_path):
    rel_path, frag = ALLOWLIST[0]
    (tmp_path / Path(rel_path).parent).mkdir(parents=True, exist_ok=True)
    f = tmp_path / rel_path
    f.write_text(
        f"{frag} -- this line is allowlisted.\n"
        "a totally different line: /home/agents-shared/projects/other.md\n"
    )
    matches = scan_file(f, tmp_path)
    assert len(matches) == 1, f"expected exactly the non-allowlisted line to fire, got {matches}"
    assert matches[0]["line_number"] == 2


# ---------------------------------------------------------------------------
# Test 5: JSON format shape
# ---------------------------------------------------------------------------

def test_format_json_shape():
    matches = [{"file": "x.md", "line_number": 3, "line": "..."}]
    parsed = json.loads(format_json(matches))
    assert parsed == {"matches": matches}


# ---------------------------------------------------------------------------
# Test 6: only tracked files are scanned (the bug this suite itself caught)
# ---------------------------------------------------------------------------

def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def test_only_tracked_files_are_scanned(tmp_path):
    _run(["git", "init", "-q"], tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], tmp_path)
    _run(["git", "config", "user.name", "Test"], tmp_path)

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "tracked.md").write_text("/home/agents-shared/projects/tracked.md\n")
    _run(["git", "add", "docs/tracked.md"], tmp_path)
    _run(["git", "commit", "-q", "-m", "init"], tmp_path)

    # A worktree-shaped gitignored dir, same as .claude/worktrees/ in the
    # real repo -- present on disk, never tracked.
    (tmp_path / ".claude" / "worktrees" / "agent-x").mkdir(parents=True)
    (tmp_path / ".claude" / "worktrees" / "agent-x" / "stale.md").write_text(
        "/home/agents-shared/projects/stale.md\n"
    )
    # A genuinely untracked file at the top level too (not yet `git add`ed).
    (tmp_path / "untracked.md").write_text("/home/agents-shared/projects/untracked.md\n")

    found = find_target_files(tmp_path)
    rels = sorted(str(f.relative_to(tmp_path)) for f in found)
    assert rels == ["docs/tracked.md"], (
        f"only the committed file should be in scope; got {rels}"
    )


# ---------------------------------------------------------------------------
# Test 7: this scanner's own source + test file are excluded from itself
# ---------------------------------------------------------------------------

def test_scanner_excludes_its_own_source_and_test_file():
    found = find_target_files(REPO_ROOT)
    rels = {str(f.relative_to(REPO_ROOT)) for f in found}
    assert "tools/stray_baton_path_check.py" not in rels
    assert "tests/test_stray_baton_path_check.py" not in rels


# ---------------------------------------------------------------------------
# Test 8: hygiene — the real repo's scoped surfaces are currently clean
# ---------------------------------------------------------------------------

def test_repo_currently_has_no_unexplained_stray_baton_paths():
    all_matches = []
    for f in find_target_files(REPO_ROOT):
        all_matches.extend(scan_file(f, REPO_ROOT))
    assert all_matches == [], f"stray pre-cutover baton-path reference(s) found in the live repo: {all_matches}"
