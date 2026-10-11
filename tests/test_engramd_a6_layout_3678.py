"""Contract tests for the A6 tool's measurement LAYOUT (#3678, part of #2652).

Written BEFORE the change (contract-first). The finding that motivates it: the
A6 tool loaded the hooks and the owner from the SOURCE checkout, where a hook's
per-call `_import_engramd_client` -> `_hooklib.resolve_runtime_dir` falls
through to a walk over parent directories. An installed plugin (flat `hooks/`,
runtime modules at the plugin root) resolves on its first check and never
walks. Only the owner path re-resolves per call, so the source layout charged
the owner for a lookup production never does: 1.43x vs 1.17x on one host, same
session.

The contract (Borges, design seat; Kepler, merge seat; both on #3678):

  * the tool can measure an INSTALLED layout: a plugin built by
    tools/build-plugin.sh from the tree under test into a scratch directory
    that is never installed; hooks and owner both load from that build;
  * two runtime ASSERTS, not prints: the owner process's real command line
    resolves under the build directory, and the hook's client import takes the
    plugin-root branch -- counted as zero parent-walk calls, not inferred from
    timing;
  * the built runtime files are byte-identical to the tree under test, so a
    PR's own change is what gets measured;
  * both layouts are reported every run; the installed one is labelled the
    gate cell; the source one is labelled a development-time number that is
    not production;
  * the default `measure()` (no layout) is unchanged, so existing callers and
    tests keep their meaning.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_a6_latency.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("engramd_a6_latency_layout", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def installed(tool, tmp_path_factory):
    return tool.build_installed_layout(tmp_path_factory.mktemp("a6-build"))


@pytest.fixture(scope="module")
def installed_run(tool, installed):
    return tool.measure_in_layout(installed, hook="stop", n=20, nodes=300, warmup=5)


@pytest.fixture(scope="module")
def source_run(tool):
    return tool.measure_in_layout(tool.source_layout(), hook="stop", n=20, nodes=300, warmup=5)


# --- the build ---------------------------------------------------------------------

def test_the_installed_layout_is_flat_like_a_real_install(tool, installed):
    """Mutation: point the 'installed' layout at the source tree, or build with
    a layout that keeps hooks/claude/ -> red."""
    root = installed.root
    assert (root / "engramd_owner.py").is_file()
    assert (root / "engramd_client.py").is_file()
    assert installed.stop_hook == root / "hooks" / "engram-stop-hook.py"
    assert installed.stop_hook.is_file()
    assert not (root / "hooks" / "claude").exists()
    assert REPO_ROOT not in root.resolve().parents and root.resolve() != REPO_ROOT


def test_the_build_is_byte_identical_to_the_tree_under_test(tool, installed):
    """The measured runtime is the PR's own code. Mutation: build from a
    different ref (or a stale cached build) -> red."""
    assert tool.layout_matches_tree(installed) == []


def test_a_build_that_differs_from_the_tree_is_reported(tool, installed, tmp_path):
    """The byte check can fail. Control: corrupt one copied file in a scratch
    copy of the build -> its name is reported. Mutation: make the check always
    return [] -> red."""
    import shutil
    copy = tmp_path / "copy"
    shutil.copytree(installed.root, copy, symlinks=True)
    target = copy / "engramd_owner.py"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    drifted = tool.Layout(name="installed", root=copy,
                          stop_hook=copy / "hooks" / "engram-stop-hook.py",
                          user_hook=copy / "hooks" / "engram-user-identity-hook.py")
    assert "engramd_owner.py" in tool.layout_matches_tree(drifted)


# --- the two runtime asserts ---------------------------------------------------------

@requires_linux_owner
def test_the_installed_runs_owner_command_line_is_under_the_build(installed, installed_run):
    """Read from /proc by the tool, not from the argv it built. Mutation:
    launch the owner from the source tree in installed mode -> red (and the
    tool's own assert refuses the run)."""
    cmd = installed_run["owner_argv"]
    owner_file = Path(cmd[1]).resolve()
    assert owner_file.name == "engramd_owner.py"
    assert installed.root.resolve() in owner_file.parents


@requires_linux_owner
def test_the_installed_run_never_walks_parent_directories(installed_run):
    """Borges's assert: counted, not timed. Mutation: load the hook (or its
    _hooklib) from the source tree in installed mode -> the walk runs -> red."""
    assert installed_run["parent_walk_calls"] == 0


@requires_linux_owner
def test_the_source_run_does_walk_so_the_counter_can_see_a_walk(source_run):
    """Positive control for the counter: in the source layout the resolver
    falls through to the parent walk on every call. Mutation: a counter that
    never increments -> this goes red (the zero above would otherwise be
    vacuous)."""
    assert source_run["parent_walk_calls"] > 0


def test_the_installed_check_refuses_a_run_that_escaped_the_build(tool, installed):
    """The tool's own asserts, as a unit. Mutation: drop either check -> red."""
    good = {"owner_argv": [sys.executable, str(installed.root / "engramd_owner.py")],
            "parent_walk_calls": 0}
    tool.check_installed_run(good, installed)
    escaped = dict(good, owner_argv=[sys.executable, str(REPO_ROOT / "src" / "engram" / "engramd_owner.py")])
    with pytest.raises(tool.LayoutViolation):
        tool.check_installed_run(escaped, installed)
    walked = dict(good, parent_walk_calls=3)
    with pytest.raises(tool.LayoutViolation):
        tool.check_installed_run(walked, installed)


# --- reporting -------------------------------------------------------------------------

@requires_linux_owner
def test_both_layouts_are_reported_and_labelled(tool, installed_run, source_run):
    """Mutation: drop either table, or label the source table as the gate ->
    red."""
    text = tool.format_layouts([source_run, installed_run])
    low = text.lower()
    assert "installed layout" in low and "source checkout" in low
    headers = [ln.lower() for ln in text.splitlines() if ln.startswith("### ")]
    assert len(headers) == 2
    installed_h = [h for h in headers if "installed layout" in h]
    source_h = [h for h in headers if "source checkout" in h]
    assert installed_h and "gate" in installed_h[0]
    assert source_h and "gate" not in source_h[0] and "not production" in source_h[0]


@requires_linux_owner
def test_both_runs_answer_like_the_direct_read(installed_run, source_run):
    """Mutation: an installed run that measures a stub or a different store ->
    red."""
    assert installed_run["answers_match"] is True
    assert source_run["answers_match"] is True


def test_measure_without_a_layout_is_unchanged(tool):
    """Existing callers keep their meaning: the default is the source checkout,
    exactly as before. Mutation: switch measure()'s default layout -> red."""
    import inspect
    assert "layout" not in inspect.signature(tool.measure).parameters
    assert tool.source_layout().stop_hook == tool.STOP_HOOK_PATH


# --- the store's journal mode (Kepler's ruling on #3678) -------------------------------

def test_the_synthetic_store_is_wal_like_production(tool):
    """Real ENGRAM stores are journal_mode=WAL; a delete-mode synthetic store
    skips the owner's per-request displacement check (no -wal/-shm fds to
    inspect), so it prices a path production never runs. Mutation: build the
    synthetic store without switching to WAL -> red."""
    import sqlite3
    home = tool.make_test_store(nodes=50)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


@requires_linux_owner
def test_the_report_names_the_store_journal_mode(tool, installed_run):
    """The header names the journal mode the run measured. Mutation: drop it
    from the result or the table -> red."""
    assert installed_run["journal_mode"] == "wal"
    assert "journal_mode=wal" in tool.format_table(installed_run)
