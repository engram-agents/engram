"""Tests for tools/stray_master_ref_check.py — stray 'master' ref warn-gate (#1884).

Coverage:
  1. Workflow inline `branches:`/`base:` filter containing master fires; `main` doesn't.
  2. Workflow block-list `branches:` form (`- master`) fires at the right indent.
  3. `origin/master` fires anywhere in a workflow or shell file.
  4. git checkout/fetch/push targeting master fires in shell scripts.
  5. False-positive avoidance: substring hits like `dream_master`/`sqlite_master`
     and unrelated git commands do NOT fire (this is the whole point of the
     issue — a naive grep produces noise).
  6. JSON format shape.
  7. find_target_files scopes to workflows + the two ops-shell dirs only —
     docs/** (which legitimately narrates `master` as history) is never scanned.
  8. Hygiene: running the real scan against this repo's actual scoped surfaces
     currently reports zero matches (regression guard — a stray ref should
     never quietly creep back in and go unnoticed by this test suite).
"""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "stray_master_ref_check.py"

_spec = importlib.util.spec_from_file_location("stray_master_ref_check", TOOL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

scan_workflow_yaml = _mod.scan_workflow_yaml
scan_shell_script = _mod.scan_shell_script
find_target_files = _mod.find_target_files
format_json = _mod.format_json


# ---------------------------------------------------------------------------
# Test 1: inline branches:/base: filter
# ---------------------------------------------------------------------------

def test_inline_branches_master_fires():
    matches = scan_workflow_yaml("on:\n  pull_request:\n    branches: [dev, master]\n")
    assert len(matches) == 1
    assert matches[0]["reason"] == "branches: filter value contains master"


def test_inline_base_master_fires():
    matches = scan_workflow_yaml("on:\n  pull_request:\n    base: master\n")
    assert len(matches) == 1
    assert matches[0]["reason"] == "base: filter value contains master"


def test_inline_branches_main_does_not_fire():
    matches = scan_workflow_yaml("on:\n  pull_request:\n    branches: [dev, main]\n")
    assert matches == [], f"main-only filter must not fire; got {matches}"


# ---------------------------------------------------------------------------
# Test 2: block-list branches: form
# ---------------------------------------------------------------------------

def test_block_list_master_fires():
    text = (
        "on:\n"
        "  pull_request:\n"
        "    branches:\n"
        "      - dev\n"
        "      - master\n"
        "  push:\n"
        "    branches: [main]\n"
    )
    matches = scan_workflow_yaml(text)
    reasons = [m["reason"] for m in matches]
    assert reasons == ["branches: list item is master"], f"got {matches}"
    # The unrelated `push: branches: [main]` block must not also fire.
    assert not any("main]" in m["line"] for m in matches)


def test_block_list_main_only_does_not_fire():
    text = "on:\n  pull_request:\n    branches:\n      - dev\n      - main\n"
    matches = scan_workflow_yaml(text)
    assert matches == [], f"main-only block list must not fire; got {matches}"


def test_block_list_survives_interleaved_comment():
    """A comment nested inside the list (deeper than the key's own indent) is
    a common, valid YAML idiom and must NOT terminate block-tracking — a
    list item after it still has to be scanned. Regression for the exact
    false-negative the reviewer found: this used to silently return []."""
    text = (
        "on:\n"
        "  pull_request:\n"
        "    branches:\n"
        "      - dev\n"
        "      # release branches only\n"
        "      - master\n"
    )
    matches = scan_workflow_yaml(text)
    reasons = [m["reason"] for m in matches]
    assert reasons == ["branches: list item is master"], f"got {matches}"


def test_block_list_ends_at_dedented_comment():
    """A comment at or above the key's own indent (e.g. introducing the next
    top-level key) genuinely ends the block — a list item after it belongs
    to a different context and must not be attributed to the prior key."""
    text = (
        "on:\n"
        "  pull_request:\n"
        "    branches:\n"
        "      - dev\n"
        "  # unrelated top-level comment, same indent as 'branches:' itself\n"
        "  master_unrelated_field: true\n"
    )
    matches = scan_workflow_yaml(text)
    assert matches == [], f"dedented comment must end the block; got {matches}"


# ---------------------------------------------------------------------------
# Test 3: origin/master anywhere
# ---------------------------------------------------------------------------

def test_origin_master_fires_in_workflow():
    text = "jobs:\n  x:\n    steps:\n      - run: git fetch origin/master\n"
    matches = scan_workflow_yaml(text)
    assert len(matches) == 1
    assert matches[0]["reason"] == "origin/master reference"


def test_origin_master_fires_in_shell():
    matches = scan_shell_script("#!/bin/bash\ngit fetch origin/master\n")
    assert len(matches) == 1
    assert matches[0]["reason"] == "origin/master reference"


# ---------------------------------------------------------------------------
# Test 4: git checkout/fetch/push master in shell
# ---------------------------------------------------------------------------

def test_git_checkout_master_fires():
    matches = scan_shell_script("git checkout master\n")
    assert len(matches) == 1
    assert matches[0]["reason"] == "git checkout/fetch/push targets master"


def test_git_fetch_origin_master_fires():
    matches = scan_shell_script("git fetch origin master\n")
    assert len(matches) == 1


def test_git_push_origin_master_fires():
    matches = scan_shell_script("git push origin master\n")
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Test 5: false-positive avoidance
# ---------------------------------------------------------------------------

def test_dream_master_substring_does_not_fire():
    text = (
        "#!/bin/bash\n"
        "# dream-master iterates over findings\n"
        "echo 'the dream_master reads sqlite_master too'\n"
    )
    matches = scan_shell_script(text)
    assert matches == [], f"substring/comment mentions must not fire; got {matches}"


def test_unrelated_git_command_does_not_fire():
    matches = scan_shell_script("git checkout dev\ngit fetch origin dev\ngit push origin dev\n")
    assert matches == [], f"non-master git commands must not fire; got {matches}"


def test_docs_prose_style_text_not_scanned_by_yaml_scanner():
    # Prose narrating master as history has no branches:/base: key and no
    # origin/master or git-command shape — the scanner correctly ignores it
    # even if handed the text directly (defense in depth; the real exclusion
    # is find_target_files never globbing docs/** at all, tested below).
    text = "This doc was rewritten from the first real end-to-end dev->master cut.\n"
    assert scan_workflow_yaml(text) == []
    assert scan_shell_script(text) == []


# ---------------------------------------------------------------------------
# Test 6: JSON format shape
# ---------------------------------------------------------------------------

def test_json_format_shape():
    matches = scan_workflow_yaml("branches: [dev, master]\n")
    for m in matches:
        m["file"] = "fake.yml"
    output = format_json(matches)
    data = json.loads(output)
    assert "matches" in data
    m = data["matches"][0]
    for key in ("file", "line_number", "reason", "line"):
        assert key in m, f"required key {key!r} missing from match dict"


def test_json_format_empty_matches():
    assert json.loads(format_json([])) == {"matches": []}


# ---------------------------------------------------------------------------
# Test 7: scope — workflows + the two ops-shell dirs only, never docs/**
# ---------------------------------------------------------------------------

def test_scope_excludes_docs(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "src" / "engram" / "tools").mkdir(parents=True)
    (tmp_path / "src" / "forum" / "deploy").mkdir(parents=True)
    (tmp_path / "docs").mkdir()

    (tmp_path / ".github" / "workflows" / "x.yml").write_text("branches: [master]\n")
    (tmp_path / "src" / "engram" / "tools" / "x.sh").write_text("git checkout master\n")
    (tmp_path / "src" / "forum" / "deploy" / "x.sh").write_text("git checkout master\n")
    (tmp_path / "docs" / "RELEASING.md").write_text("dev->master history, master, master\n")

    found = find_target_files(tmp_path)
    rels = sorted(str(f.relative_to(tmp_path)) for f in found)
    assert rels == [
        ".github/workflows/x.yml",
        "src/engram/tools/x.sh",
        "src/forum/deploy/x.sh",
    ], f"docs/** must never be in scope; got {rels}"


# ---------------------------------------------------------------------------
# Test 8: hygiene — the real repo's scoped surfaces are currently clean
# ---------------------------------------------------------------------------

def test_repo_currently_has_no_stray_master_refs():
    all_matches = []
    for f in find_target_files(REPO_ROOT):
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix in (".yml", ".yaml"):
            raw = scan_workflow_yaml(text)
        else:
            raw = scan_shell_script(text)
        for m in raw:
            m["file"] = str(f.relative_to(REPO_ROOT))
        all_matches.extend(raw)
    assert all_matches == [], f"stray master ref(s) found in the live repo: {all_matches}"
