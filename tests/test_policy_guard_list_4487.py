"""Every file named in the merge policy's Owner-merge item 4 exists (#4487).

``docs/internal/PR-MERGE-POLICY.md``, section "Owner merge", item 4, lists the
guard tests and scanners the merge seat runs on every PR, and closes with
"a guard that moved is not a guard that ran".  Nothing checked that.  This
file reads the document, takes the ``tests/*.py`` and ``tools/*.py`` paths
named inside item 4 and only item 4, and asserts each one is on disk.

Contract (one cell each; stdlib only; it reads this document and loads only the parser module):

- C1. On the real document every path item 4 names exists; a failure names
  each missing path.
- C2. The real document yields at least 20 paths, and this file is one of them.
- C3. Paths named outside item 4 (other items, or a later section with its own
  "4." line) are not returned.
- C4. A document missing the section heading, the item-4 start or the item-5
  line fails loudly with the missing anchor named; an item 4 holding no paths
  fails too.  A reformatted document never passes by finding zero paths.
- C5. The message the existence check prints names a missing path, only that
  one, and the document; it is None when nothing is missing.
- C6. A later section's item 4 is not read when Owner merge has none.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "docs" / "internal" / "PR-MERGE-POLICY.md"

_PARSER_PATH = REPO_ROOT / "src" / "engram" / "tools" / "policy_guard_list.py"
_pspec = importlib.util.spec_from_file_location("policy_guard_list_4487", str(_PARSER_PATH))
_parser = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(_parser)

# The parser moved out of this file into src/engram/tools/policy_guard_list.py
# (#4358: run_touched_tests.py reads the same item 4); the cells below stay.
_item4_paths = _parser.item4_paths


def _missing(paths: list[str], root: Path) -> list[str]:
    return [p for p in paths if not (root / p).is_file()]


def _missing_message(paths: list[str], root: Path) -> str | None:
    """The exact failure message for missing paths, or None when all exist."""
    missing = _missing(paths, root)
    if not missing:
        return None
    return (
        "Owner-merge item 4 in docs/internal/PR-MERGE-POLICY.md names files that "
        "are not in the tree: " + ", ".join(missing)
    )


def _real_paths() -> list[str]:
    return _item4_paths(POLICY.read_text(encoding="utf-8"))


def test_every_item4_path_exists():
    msg = _missing_message(_real_paths(), REPO_ROOT)
    assert msg is None, msg


def test_item4_extraction_is_not_empty_and_includes_this_file():
    paths = _real_paths()
    assert len(paths) >= 20, f"only {len(paths)} paths extracted from item 4"
    assert "tests/test_policy_guard_list_4487.py" in paths, (
        "item 4 does not name tests/test_policy_guard_list_4487.py"
    )


_SYNTH_OK = """\
## Intro
4. **The seat's own run, decoy before the section** `tests/test_not_a_real_file_0.py`
5. **The PR's changed decoy**

## Owner merge (title)
3. **Third item** `tests/test_not_a_real_file_3.py`
4. **The seat's own run, on a scratch merge**
   `tests/test_policy_guard_list_4487.py`
5. **The PR's changed or added tests** `tests/test_not_a_real_file_5.py`

## Later section
4. **The seat's own run, again** `tests/test_not_a_real_file_9.py`
5. **The PR's changed again**
"""


def test_paths_outside_item4_do_not_bind():
    assert _item4_paths(_SYNTH_OK) == ["tests/test_policy_guard_list_4487.py"]


@pytest.mark.parametrize(
    "text, anchor",
    [
        (
            "## Other\n4. **The seat's own run** `tests/a.py`\n5. **The PR's changed\n",
            "## Owner merge",
        ),
        (
            "## Owner merge\n3. **Third** `tests/a.py`\n5. **The PR's changed\n",
            "item-4 start",
        ),
        (
            "## Owner merge\n4. **The seat's own run** `tests/a.py`\n",
            "item-5",
        ),
        (
            "## Owner merge\n4. **The seat's own run** no paths here\n5. **The PR's changed\n",
            "held no paths",
        ),
    ],
    ids=["no-heading", "no-item4", "no-item5", "no-paths"],
)
def test_missing_anchors_fail_loudly(text, anchor):
    with pytest.raises(AssertionError) as exc:
        _item4_paths(text)
    assert anchor in str(exc.value)


def test_a_missing_path_is_reported_by_name(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_real_one.py").write_text("")
    paths = ["tests/test_real_one.py", "tests/test_bogus_one.py"]
    message = _missing_message(paths, tmp_path)
    assert message is not None
    assert "tests/test_bogus_one.py" in message
    assert "tests/test_real_one.py" not in message
    assert "PR-MERGE-POLICY.md" in message
    (tmp_path / "tests" / "test_bogus_one.py").write_text("")
    assert _missing_message(paths, tmp_path) is None


_SYNTH_NO_ITEM4_HERE = """\
## Owner merge
3. **Third item** `tests/test_not_a_real_file_3.py`
5. **The PR's changed or added tests** `tests/test_not_a_real_file_5.py`

## Later section
4. **The seat's own run, in a later section** `tests/test_not_a_real_file_9.py`
5. **The PR's changed, in a later section**
"""


def test_a_later_sections_item4_is_not_read_when_owner_merge_has_none():
    with pytest.raises(AssertionError) as exc:
        _item4_paths(_SYNTH_NO_ITEM4_HERE)
    assert "item-4 start" in str(exc.value)


def test_real_item4_names_the_tools_scanners_too():
    paths = _real_paths()
    for expected in ("tools/scan-leaks.py", "tools/test_id_set_check.py"):
        assert expected in paths, f"item 4 no longer yields {expected}"


def test_the_hook_directory_scanner_is_in_the_guard_set():
    """tests/test_hook_injection_bounds.py globs src/engram/hooks/claude/*.py and names only the
    hooks of its own exemption list.

    A PR that adds a hook changes a file that this test does not name, and the test-map rows are
    first-match-wins, so a hook with its own row never reaches it by the map. It is therefore in
    the guard set, which the runner reads from the policy and runs on every non-empty selection.
    Three assertions in one cell: the policy names it, the runner's own reader returns it, and a
    NEW hook file selects it (the claim itself, through select_tests; the runner is what a seat runs).
    """
    name = "tests/test_hook_injection_bounds.py"
    assert name in _real_paths(), f"item 4 does not name {name}"
    rtt_path = REPO_ROOT / "src" / "engram" / "tools" / "run_touched_tests.py"
    rspec = importlib.util.spec_from_file_location("run_touched_tests_4487", str(rtt_path))
    rtt = importlib.util.module_from_spec(rspec)
    rspec.loader.exec_module(rtt)
    guard_tests, _tool_checks = rtt.guard_set(str(REPO_ROOT))
    assert name in guard_tests, f"the runner's guard set does not return {name}"
    # A hook added by a PR: a file no test names and no map row lists.
    new_hook = "src/engram/hooks/claude/engram-a-hook-added-later-hook.py"
    selected, is_full, _reasons, _map = rtt.select_tests([new_hook], str(REPO_ROOT))
    assert not is_full, "a new hook file became a full-suite trigger; the policy line says it selects only the guard set"
    assert name in selected, f"a new hook file does not select {name}"


# The whole-tree enumerators the 2026-11-18 census found outside the guard set (Kepler's ruling, 2026-11-18):
# each enumerates the shipped tree or the tracked tree and asserts something about every file, so a change to
# a file it never names can redden it.  Scope of the census: scans done in-process and through git ls-files /
# git grep; a scan done in any other subprocess is unseen, and 601 test files that list no directory were not read.
WHOLE_TREE_ENUMERATORS = [
    "tests/test_3765_nul_guard.py",
    "tests/test_boot_charge_loop_prompt_4139a.py",
    "tests/test_changed_concurrently_fold_4188.py",
    "tests/test_cites_evidence_only_2243.py",
    "tests/test_clear_stale_daemon_3493.py",
    "tests/test_conftest_service_manager_guard_3519.py",
    "tests/test_edge_liveness_ssot_3542.py",
    "tests/test_engram_client_isolation.py",
    "tests/test_engram_core_seam.py",
    "tests/test_engram_core_sys_bound_3222.py",
    "tests/test_fix_git_backup_canonical_gitignore_4506.py",
    "tests/test_full_length_hashes_resolve.py",
    "tests/test_launchers_relative_engram_home_3863.py",
    "tests/test_metricgate_adapter.py",
    "tests/test_never_clobber_contract.py",
    "tests/test_os_harness_check.py",
    "tests/test_readme_dev_trees_section.py",
    "tests/test_recall_summary_writes.py",
    "tests/test_relative_engram_home_step3a_3863.py",
    "tests/test_retire_runtime_keys_4255s2.py",
    "tests/test_retired_axis_residue_4255s7.py",
    "tests/test_retired_multi_agent_axis_4255s3.py",
    "tests/test_ro_uri_quoting_3514.py",
    "tests/test_run_touched_tests_4358.py",
    "tests/test_scan_leaks.py",
    "tests/test_session_start_no_starred_4272.py",
    "tests/test_sleep_skill_cohort_boundary.py",
    "tests/test_snapshot_worker_orphan_4833.py",
    "tests/test_stray_baton_path_check.py",
    "tests/test_surface_label_keyed_4305.py",
    "tests/test_ws3_telemetry_resolver.py",
]


def _runner_module():
    rtt_path = REPO_ROOT / "src" / "engram" / "tools" / "run_touched_tests.py"
    rspec = importlib.util.spec_from_file_location("run_touched_tests_4487_wt", str(rtt_path))
    rtt = importlib.util.module_from_spec(rspec)
    rspec.loader.exec_module(rtt)
    return rtt


def test_the_whole_tree_enumerator_list_has_thirty_one_distinct_files_that_exist():
    """A silently shortened list must not pass: 31 distinct names, each a file on disk."""
    assert len(WHOLE_TREE_ENUMERATORS) == len(set(WHOLE_TREE_ENUMERATORS)) == 31
    missing = [n for n in WHOLE_TREE_ENUMERATORS if not (REPO_ROOT / n).is_file()]
    assert not missing, f"listed but not on disk: {missing}"


@pytest.mark.parametrize("name", WHOLE_TREE_ENUMERATORS)
def test_each_whole_tree_enumerator_is_named_by_the_policy_and_returned_by_the_runner(name):
    """Policy names it AND the runner's own guard_set() returns it (the runner is what a seat runs)."""
    assert name in _real_paths(), f"item 4 does not name {name}"
    guard_tests, _tool_checks = _runner_module().guard_set(str(REPO_ROOT))
    assert name in guard_tests, f"the runner's guard set does not return {name}"


def test_a_directory_with_a_listed_name_does_not_count_as_a_file(tmp_path):
    (tmp_path / "tests" / "test_dirlike.py").mkdir(parents=True)
    message = _missing_message(["tests/test_dirlike.py"], tmp_path)
    assert message is not None
    assert "tests/test_dirlike.py" in message


_SYNTH_TWICE = """\
## Owner merge
4. **The seat's own run** `tests/test_twice.py`
   and again here `tests/test_twice.py`
5. **The PR's changed or added tests**
"""


def test_a_path_named_twice_is_returned_once():
    assert _item4_paths(_SYNTH_TWICE) == ["tests/test_twice.py"]
