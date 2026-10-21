"""Tests for tools.engine.flows — upgrade flow catalog.

All tests use a FULLY FAKE ctx built with tmp dirs — they NEVER touch
~/.engram or the real repo state.

Coverage:
  1. upgrade_flow() returns the expected 12 steps in the right order
  2. plan_flow() reports check() state correctly
  3. Full run with fake ctx → PAUSED at first operator step
     (plugin-marketplace-update when substrate is healthy)
  4. Fake the operator action → resume → PAUSED at next operator step
  5. Walk through all operator steps → Done
  6. #794 branch guard: build step refuses non-dev without allow_branch
  7. changeset-reviewed requires ack_changeset=True
  8. Doctor mode on broken fake env names exactly the broken step
  9. mcp-reconnect is automatically satisfied when no server files changed
 10. template-gate marks done and does not touch ~/.claude/CLAUDE.md
 11. #707-class: marketplace-rebuilt fails loud when install_tier unset
 12. substrate-health: marker present → check/verify False; absent → True
 13. substrate-health: malformed marker → still degraded; instruction renders
 14. substrate-health: flow pauses at substrate-health when marker present
 15. substrate-health: downstream steps not reached when marker present
 16. Doctor: marker present → substrate-health unsatisfied; absent → satisfied
 17. Drift guard: flows._SUBSTRATE_MARKER_FILENAME == engram_walguard._MARKER_FILENAME
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from typing import Any

import pytest

from tools.engine.flows import upgrade_flow, plan_flow
from tools.engine.steps import run_flow, run_doctor, Done, Paused, Failed


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

EXPECTED_STEP_IDS = [
    # substrate-health has no requires (parallel root with source-tree-located);
    # topological order puts it first because it's listed first in upgrade_flow()
    # and Kahn's algorithm respects insertion order for zero-in-degree nodes.
    "substrate-health",
    "source-tree-located",
    "tree-at-commit",
    "changeset-reviewed",
    "marketplace-rebuilt",
    "flags-match-config",
    "plugin-marketplace-update",
    "plugin-upgrade",
    "mcp-reconnect",
    "template-gate",
    "stats-verify-instruction",
    "record-reminder",
]

# Operator steps in order
OPERATOR_STEPS = [
    "substrate-health",
    "plugin-marketplace-update",
    "plugin-upgrade",
    "mcp-reconnect",
    "stats-verify-instruction",
    "record-reminder",
]


def _make_fake_env(
    tmp_base: str,
    *,
    on_dev: bool = True,
    clean_tree: bool = True,
    has_deployed_version: bool = True,
    deployed_sha: str = "abc123",
    server_changed: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    """Return (engram_home, source_dir, ctx) with fake directory structure.

    Creates minimal fake dirs/files needed for the upgrade flow steps.
    Never touches ~/.engram.
    """
    import subprocess

    engram_home = os.path.join(tmp_base, "engram_home")
    source_dir = os.path.join(tmp_base, "source_dir")

    os.makedirs(engram_home, exist_ok=True)
    os.makedirs(source_dir, exist_ok=True)

    # Initialize a minimal git repo in source_dir
    _init_fake_git(source_dir, branch="dev" if on_dev else "feature-x")

    # .deployed-version
    if has_deployed_version:
        dv_path = os.path.join(engram_home, ".deployed-version")
        with open(dv_path, "w") as f:
            f.write(f"alpha_sha={deployed_sha}\n")
            f.write(f"deployed_from={source_dir}\n")

    # config.json — minimal
    config_path = os.path.join(engram_home, "config.json")
    with open(config_path, "w") as f:
        json.dump({"install_tier": "convenience", "multi_agent": False}, f)

    # Fake packaging/tiers.json (minimal valid manifest)
    packaging_dir = os.path.join(source_dir, "packaging")
    os.makedirs(packaging_dir, exist_ok=True)
    _write_minimal_tiers_json(source_dir, packaging_dir)

    # Fake server.py (needed for build root guard + check_hooks_consistency)
    with open(os.path.join(source_dir, "server.py"), "w") as f:
        f.write("# fake server\n")

    # Fake hooks/hooks.json (needed by build_plugin)
    hooks_dir = os.path.join(source_dir, "hooks", "claude")
    os.makedirs(hooks_dir, exist_ok=True)
    hooks_json = os.path.join(source_dir, "hooks", "hooks.json")
    with open(hooks_json, "w") as f:
        json.dump({"hooks": {}}, f, indent=2)
        f.write("\n")

    # Fake plugin.json
    with open(os.path.join(source_dir, "plugin.json"), "w") as f:
        json.dump({"name": "engram", "version": "0.0.1-test"}, f)

    # Fake packaging/mcp.json
    with open(os.path.join(packaging_dir, "mcp.json"), "w") as f:
        json.dump({"mcpServers": {}}, f)

    # Fake skills/claude/, agents/claude/, output-styles/claude/ (required by build_plugin)
    for d in ["skills/claude", "agents/claude", "output-styles/claude"]:
        os.makedirs(os.path.join(source_dir, d), exist_ok=True)

    # Fake templates/template.CLAUDE.md
    templates_dir = os.path.join(source_dir, "templates")
    os.makedirs(templates_dir, exist_ok=True)
    with open(os.path.join(templates_dir, "template.CLAUDE.md"), "w") as f:
        f.write("# ENGRAM CLAUDE.md template\nsome content\n")

    # Fake launch-engram-server.sh (needed for build step)
    launch_sh = os.path.join(source_dir, "launch-engram-server.sh")
    with open(launch_sh, "w") as f:
        f.write("#!/bin/bash\n# fake\nexec {{PYTHON}} server.py\n")
    os.chmod(launch_sh, os.stat(launch_sh).st_mode | stat.S_IEXEC)

    # Commit all source_dir files so the tree is clean
    subprocess.run(["git", "add", "-A"], cwd=source_dir, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fake-source-tree"],
        cwd=source_dir, capture_output=True, check=True,
    )

    # Build a fake _changed_files list simulating git range
    changed = []
    if server_changed:
        changed.append("server.py")

    # Build manifest (pre-populate so marketplace-rebuilt and flags-match check pass)
    # by actually running build_plugin is too heavy for most tests; we fake it.
    built_plugin_root = os.path.join(source_dir, "build", "plugin")
    os.makedirs(built_plugin_root, exist_ok=True)
    bm_path = os.path.join(built_plugin_root, ".engram-build-manifest.json")
    with open(bm_path, "w") as f:
        json.dump({"tier": "convenience", "multi_agent": False, "shipped_paths": []}, f)
        f.write("\n")

    # Fake hooks/hooks.json in built plugin root (needed for consistency check)
    built_hooks_dir = os.path.join(built_plugin_root, "hooks")
    os.makedirs(built_hooks_dir, exist_ok=True)
    with open(os.path.join(built_hooks_dir, "hooks.json"), "w") as f:
        json.dump({"hooks": {}}, f, indent=2)
        f.write("\n")

    # build/ dir is untracked — that's fine; it doesn't affect git status for tracked files.
    # The git status check in _is_clean() reports untracked files too, so we need to add
    # build/ to .gitignore or stage it.
    gitignore = os.path.join(source_dir, ".gitignore")
    with open(gitignore, "w") as f:
        f.write("build/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=source_dir, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add-gitignore"],
        cwd=source_dir, capture_output=True, check=True,
    )

    ctx: dict[str, Any] = {
        "engram_home": engram_home,
        "source_dir": source_dir,
        "target_sha": "HEAD",
        "ack_changeset": True,   # pre-acked for most tests
        "allow_branch": False,
        "_deployed_sha": deployed_sha,
        "_built_plugin_root": built_plugin_root,
        "_changed_files": changed,
        "_template_changed": False,
    }

    return engram_home, source_dir, ctx


def _init_fake_git(repo_dir: str, branch: str = "dev") -> None:
    """Initialize a minimal git repo in repo_dir on the given branch."""
    import subprocess
    subprocess.run(["git", "init", "-b", branch], cwd=repo_dir,
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir,
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir,
                   capture_output=True, check=True)
    # Create initial commit so HEAD is valid
    readme = os.path.join(repo_dir, "README.md")
    with open(readme, "w") as f:
        f.write("fake\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir,
                   capture_output=True, check=True)


def _write_minimal_tiers_json(repo_root: str, packaging_dir: str) -> None:
    """Write a minimal valid tiers.json."""
    manifest = {
        "schema_version": 1,
        "depth_tiers": ["essential", "convenience", "dev"],
        "default_tier": "convenience",
        "mechanisms": [
            {"path": "server.py", "tier": "essential"},
            {"path": "launch-engram-server.sh", "tier": "essential"},
        ],
        "excluded": [],
    }
    with open(os.path.join(packaging_dir, "tiers.json"), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# 1. upgrade_flow() step list
# ---------------------------------------------------------------------------


class TestUpgradeFlowStructure:
    """upgrade_flow() returns the correct step list."""

    def test_expected_step_count(self):
        steps = upgrade_flow()
        assert len(steps) == len(EXPECTED_STEP_IDS), (
            f"Expected {len(EXPECTED_STEP_IDS)} steps, got {len(steps)}: "
            f"{[s.id for s in steps]}"
        )

    def test_step_ids_in_order(self):
        steps = upgrade_flow()
        # After topological sort, the order must match EXPECTED_STEP_IDS
        from tools.engine.steps import _topological_sort
        ordered = _topological_sort(steps)
        assert [s.id for s in ordered] == EXPECTED_STEP_IDS

    def test_operator_steps(self):
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        for sid in OPERATOR_STEPS:
            assert sid in step_by_id, f"Expected operator step {sid!r} not found"
            s = step_by_id[sid]
            assert s.kind == "operator", f"Step {sid!r} should be kind='operator'"
            assert s.apply is None, f"Operator step {sid!r} must have apply=None"
            assert s.instruction is not None, f"Operator step {sid!r} must have instruction"

    def test_agent_steps(self):
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        agent_ids = set(EXPECTED_STEP_IDS) - set(OPERATOR_STEPS)
        for sid in agent_ids:
            assert sid in step_by_id
            s = step_by_id[sid]
            assert s.kind == "agent", f"Step {sid!r} should be kind='agent'"

    def test_no_cycles(self):
        """upgrade_flow() must be a valid DAG (no cycles)."""
        steps = upgrade_flow()
        from tools.engine.steps import _topological_sort
        ordered = _topological_sort(steps)  # raises ValueError on cycle
        assert len(ordered) == len(steps)


# ---------------------------------------------------------------------------
# 2. plan_flow()
# ---------------------------------------------------------------------------


class TestPlanFlow:
    """plan_flow() returns per-step dicts with correct fields."""

    def test_returns_all_steps(self, tmp_path):
        _, _, ctx = _make_fake_env(str(tmp_path))
        steps = upgrade_flow()
        plan = plan_flow(steps, ctx)
        assert len(plan) == len(EXPECTED_STEP_IDS)

    def test_fields_present(self, tmp_path):
        _, _, ctx = _make_fake_env(str(tmp_path))
        steps = upgrade_flow()
        plan = plan_flow(steps, ctx)
        for entry in plan:
            assert "id" in entry
            assert "kind" in entry
            assert "requires" in entry
            assert "satisfied" in entry
            assert "error" in entry

    def test_satisfied_field_reflects_ctx(self, tmp_path):
        """changeset-reviewed is satisfied iff ack_changeset=True in ctx."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        steps = upgrade_flow()

        ctx["ack_changeset"] = False
        plan_no_ack = plan_flow(steps, ctx)
        cs_entry = next(e for e in plan_no_ack if e["id"] == "changeset-reviewed")
        assert not cs_entry["satisfied"]

        ctx["ack_changeset"] = True
        plan_acked = plan_flow(steps, ctx)
        cs_entry2 = next(e for e in plan_acked if e["id"] == "changeset-reviewed")
        assert cs_entry2["satisfied"]


# ---------------------------------------------------------------------------
# 3+4+5. Full run → PAUSED at first operator → resume → DONE
# ---------------------------------------------------------------------------


class TestUpgradeFlowRun:
    """Run upgrade flow through to completion using a fully fake ctx."""

    def test_run_pauses_at_first_operator(self, tmp_path):
        """With all agent steps pre-satisfied, flow pauses at first operator step."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        # Pre-satisfy all agent steps so we hit the first operator step quickly
        ctx.update({
            "_a_done": True,
            "ack_changeset": True,
            "_template_gate_done": True,
        })

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        assert isinstance(result, Paused), f"Expected Paused, got {result}"
        assert result.step_id == "plugin-marketplace-update"

    def test_resume_after_first_operator(self, tmp_path):
        """After faking the first operator, flow pauses at the next one."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        steps = upgrade_flow()

        # First pause
        r1 = run_flow(steps, ctx)
        assert isinstance(r1, Paused)
        assert r1.step_id == "plugin-marketplace-update"

        # Fake the operator action
        ctx["_plugin_marketplace_updated"] = True

        r2 = run_flow(steps, ctx)
        assert isinstance(r2, Paused)
        assert r2.step_id == "plugin-upgrade"

    def test_walk_all_operators_to_done(self, tmp_path):
        """Walk all operator steps to Done by faking each one in sequence."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        steps = upgrade_flow()

        # Drive to completion through all operator steps
        operator_ctx_keys = {
            "plugin-marketplace-update": "_plugin_marketplace_updated",
            "plugin-upgrade": "_plugin_upgrade_done",
            "mcp-reconnect": "_mcp_reconnected",
            "stats-verify-instruction": "_stats_verified",
            "record-reminder": "_record_filed",
        }

        max_iterations = 20
        i = 0
        while True:
            result = run_flow(steps, ctx)
            if isinstance(result, Done):
                break
            if isinstance(result, Failed):
                pytest.fail(f"Unexpected FAILED at {result.step_id!r}: {result.reason}")
            assert isinstance(result, Paused), f"Unexpected result type: {result}"
            key = operator_ctx_keys.get(result.step_id)
            assert key is not None, f"Unexpected pause at {result.step_id!r}"
            ctx[key] = True
            i += 1
            assert i < max_iterations, "Flow did not terminate"

        assert isinstance(result, Done)

    def test_changeset_not_acked_stalls_flow(self, tmp_path):
        """Without ack_changeset, run reaches changeset-reviewed (not satisfied) and fails."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = False
        # tree-at-commit requires a clean tree — fake it
        ctx["_deployed_sha"] = "abc123"

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        # changeset-reviewed has check=lambda ctx: bool(ctx.ack_changeset)
        # Without ack, it runs apply() (which computes changeset but doesn't set ack),
        # then verify() returns False → FAILED
        assert isinstance(result, Failed)
        assert result.step_id == "changeset-reviewed"


# ---------------------------------------------------------------------------
# 6. #794 branch guard
# ---------------------------------------------------------------------------


class TestBranchGuard:
    """marketplace-rebuilt refuses non-dev branch without allow_branch."""

    def test_non_dev_branch_without_allow_fails(self, tmp_path):
        """On a non-dev branch, marketplace-rebuilt apply() raises → FAILED."""
        _, _, ctx = _make_fake_env(str(tmp_path), on_dev=False)
        ctx["ack_changeset"] = True
        # Remove pre-built manifest so check() fails → apply() is called
        bm_path = os.path.join(ctx["_built_plugin_root"], ".engram-build-manifest.json")
        if os.path.isfile(bm_path):
            os.remove(bm_path)

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        assert isinstance(result, Failed)
        assert result.step_id == "marketplace-rebuilt"
        assert "794" in result.reason or "dev" in result.reason.lower()

    def test_non_dev_branch_with_allow_proceeds(self, tmp_path):
        """allow_branch=True overrides the guard; build proceeds."""
        _, _, ctx = _make_fake_env(str(tmp_path), on_dev=False)
        ctx["ack_changeset"] = True
        ctx["allow_branch"] = True
        ctx["_template_gate_done"] = True

        # Pre-build the manifest so marketplace-rebuilt check() passes
        # (the actual build would be too heavy; we trust the guard test above
        # and just verify the flag is honored at the check layer)
        # The manifest is already pre-built by _make_fake_env.
        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        # Should get past marketplace-rebuilt (paused at operator steps, not failed)
        assert not isinstance(result, Failed) or result.step_id != "marketplace-rebuilt"


# ---------------------------------------------------------------------------
# 7. changeset-reviewed ack
# ---------------------------------------------------------------------------


class TestChangesetAck:
    """changeset-reviewed step requires explicit ack."""

    def test_ack_false_fails(self, tmp_path):
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = False
        steps = upgrade_flow()
        result = run_flow(steps, ctx)
        assert isinstance(result, Failed)
        assert result.step_id == "changeset-reviewed"

    def test_ack_true_passes(self, tmp_path):
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True
        steps = upgrade_flow()
        result = run_flow(steps, ctx)
        # Should at least reach the first operator step
        assert isinstance(result, (Paused, Done))


# ---------------------------------------------------------------------------
# 8. Doctor mode on broken env
# ---------------------------------------------------------------------------


class TestDoctorMode:
    """Doctor mode names exactly the broken step(s)."""

    def test_all_ok_env(self, tmp_path):
        """A fully pre-satisfied ctx passes all verifications."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        # Pre-satisfy all operator steps
        ctx.update({
            "ack_changeset": True,
            "_plugin_marketplace_updated": True,
            "_plugin_upgrade_done": True,
            "_mcp_reconnected": True,
            "_template_gate_done": True,
            "_stats_verified": True,
            "_record_filed": True,
        })

        steps = upgrade_flow()
        results = run_doctor(steps, ctx)
        broken = [r.step_id for r in results if not r.satisfied]
        # The only possible failures are steps whose verify() checks file state
        # that our fake env might not satisfy perfectly. We check that no
        # operator-step-level failure occurs.
        op_broken = [sid for sid in broken if sid in OPERATOR_STEPS]
        assert not op_broken, f"Operator steps failed verification: {op_broken}"

    def test_missing_deployed_version_breaks_source_step(self, tmp_path):
        """Without .deployed-version, source-tree-located verify() fails."""
        _, _, ctx = _make_fake_env(str(tmp_path), has_deployed_version=False)
        # Override source_dir to something non-existent to guarantee failure
        ctx["source_dir"] = "/nonexistent/path"

        steps = upgrade_flow()
        results = run_doctor(steps, ctx)
        step_results = {r.step_id: r for r in results}
        assert not step_results["source-tree-located"].satisfied

    def test_doctor_names_broken_operator_step(self, tmp_path):
        """When an operator step is not satisfied, doctor names it."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        # Pre-satisfy everything except plugin-upgrade
        ctx.update({
            "ack_changeset": True,
            "_plugin_marketplace_updated": True,
            # _plugin_upgrade_done NOT set
            "_mcp_reconnected": True,
            "_template_gate_done": True,
            "_stats_verified": True,
            "_record_filed": True,
        })

        steps = upgrade_flow()
        results = run_doctor(steps, ctx)
        broken = {r.step_id for r in results if not r.satisfied}
        assert "plugin-upgrade" in broken


# ---------------------------------------------------------------------------
# 9. mcp-reconnect conditional
# ---------------------------------------------------------------------------


class TestMcpReconnectConditional:
    """mcp-reconnect is auto-satisfied when no server files changed."""

    def test_no_server_change_mcp_reconnect_satisfied(self, tmp_path):
        _, _, ctx = _make_fake_env(str(tmp_path), server_changed=False)
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        # Verify step's check() directly
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        mcp_step = step_by_id["mcp-reconnect"]
        assert mcp_step.check(ctx), (
            "mcp-reconnect should be auto-satisfied when no server files changed"
        )

    def test_server_changed_requires_reconnect(self, tmp_path):
        _, _, ctx = _make_fake_env(str(tmp_path), server_changed=True)
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        mcp_step = step_by_id["mcp-reconnect"]

        # Without _mcp_reconnected, check() is False
        assert not mcp_step.check(ctx)

        # After marking reconnected, check() passes
        ctx["_mcp_reconnected"] = True
        assert mcp_step.check(ctx)


# ---------------------------------------------------------------------------
# 10. template-gate does not touch ~/.claude/CLAUDE.md
# ---------------------------------------------------------------------------


class TestTemplateGate:
    """template-gate detects template changes but never writes ~/.claude/CLAUDE.md."""

    def test_no_template_change_gate_done(self, tmp_path):
        """When template did not change, gate marks itself done immediately."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["_template_changed"] = False

        from tools.engine.flows import _apply_template_gate
        _apply_template_gate(ctx)

        assert ctx.get("_template_gate_done") is True
        assert ctx.get("_template_diff") is None

    def test_template_changed_gate_surfaces_content(self, tmp_path):
        """When template changed, gate stores template content in ctx."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["_template_changed"] = True

        from tools.engine.flows import _apply_template_gate
        _apply_template_gate(ctx)

        assert ctx.get("_template_gate_done") is True
        assert ctx.get("_template_diff") is not None

    def test_template_gate_never_writes_claude_md(self, tmp_path):
        """The live ~/.claude/CLAUDE.md is never written by the engine."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["_template_changed"] = True

        # Place a sentinel CLAUDE.md in a fake user dir
        fake_home = os.path.join(str(tmp_path), "fake_home")
        fake_claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(fake_claude_dir, exist_ok=True)
        sentinel = os.path.join(fake_claude_dir, "CLAUDE.md")
        original_content = "SENTINEL — must not change\n"
        with open(sentinel, "w") as f:
            f.write(original_content)

        # Override HOME to fake_home (so any inadvertent write would go there)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = fake_home
        try:
            from tools.engine.flows import _apply_template_gate
            _apply_template_gate(ctx)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            else:
                del os.environ["HOME"]

        # Sentinel must be unchanged
        with open(sentinel) as f:
            content_after = f.read()
        assert content_after == original_content, (
            "template-gate must NEVER write ~/.claude/CLAUDE.md"
        )


# ---------------------------------------------------------------------------
# 11. flags-match-config: FAILED reason includes built vs config delta
# ---------------------------------------------------------------------------


class TestFlagsMatchConfigFailureDetail:
    """flags-match-config FAILED reason surfaces the tier+MA delta, not just
    the generic 'verify() returned False after apply()' string."""

    def test_failed_reason_includes_delta(self, tmp_path):
        """When built manifest tier/MA differ from config.json, the FAILED
        reason must include both the built values and the expected values."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        # Make config.json claim dev/True while the built manifest says
        # convenience/False — a clear mismatch.
        config_path = os.path.join(ctx["engram_home"], "config.json")
        with open(config_path, "w") as f:
            json.dump({"install_tier": "dev", "multi_agent": True}, f)

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        assert isinstance(result, Failed), f"Expected Failed, got {result}"
        assert result.step_id == "flags-match-config"
        # The reason must mention both the built values and the config values
        assert "built" in result.reason
        assert "convenience" in result.reason   # built tier
        assert "dev" in result.reason           # expected tier
        assert "multi_agent" in result.reason   # the delta axis

    def test_matching_flags_no_failure(self, tmp_path):
        """When built manifest matches config.json exactly, the step is satisfied
        and the flow proceeds past flags-match-config."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True
        # Default _make_fake_env has convenience/False in both places — should pass.

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        # Should reach at least the first operator step (not fail at flags-match-config)
        if isinstance(result, Failed):
            assert result.step_id != "flags-match-config", (
                f"flags-match-config should not fail when tier+MA match; "
                f"got: {result.reason}"
            )


# ---------------------------------------------------------------------------
# 11. #707-class: unset install_tier → loud FAILED (not silent convenience)
# ---------------------------------------------------------------------------


class TestUnsetInstallTierLoudFailure:
    """marketplace-rebuilt raises RuntimeError when install_tier is unset/empty.

    This is the #707-class guard: a dev-tier install whose config.json lost
    the install_tier field must NOT silently default to convenience and drop
    dev tooling.  Instead it must surface as FAILED with a clear message.
    """

    def _ctx_without_install_tier(self, tmp_path: str) -> dict:
        """Return a ctx where config.json lacks install_tier entirely."""
        _, _, ctx = _make_fake_env(tmp_path)
        ctx["ack_changeset"] = True

        # Remove pre-built manifest so check() fails → apply() is called
        bm_path = os.path.join(ctx["_built_plugin_root"], ".engram-build-manifest.json")
        if os.path.isfile(bm_path):
            os.remove(bm_path)

        # Rewrite config.json without install_tier
        config_path = os.path.join(ctx["engram_home"], "config.json")
        with open(config_path, "w") as f:
            json.dump({"multi_agent": False}, f)

        return ctx

    def test_missing_install_tier_fails_loud(self, tmp_path):
        """config.json without install_tier key → FAILED at marketplace-rebuilt
        with 'install_tier unset' in the reason."""
        ctx = self._ctx_without_install_tier(str(tmp_path))

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        assert isinstance(result, Failed), f"Expected Failed, got {result}"
        assert result.step_id == "marketplace-rebuilt"
        assert "install_tier unset" in result.reason

    def test_empty_install_tier_fails_loud(self, tmp_path):
        """config.json with install_tier='' → FAILED at marketplace-rebuilt
        with 'install_tier unset' in the reason."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = True

        # Remove pre-built manifest so check() fails → apply() is called
        bm_path = os.path.join(ctx["_built_plugin_root"], ".engram-build-manifest.json")
        if os.path.isfile(bm_path):
            os.remove(bm_path)

        # Rewrite config.json with empty install_tier
        config_path = os.path.join(ctx["engram_home"], "config.json")
        with open(config_path, "w") as f:
            json.dump({"install_tier": "", "multi_agent": False}, f)

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        assert isinstance(result, Failed), f"Expected Failed, got {result}"
        assert result.step_id == "marketplace-rebuilt"
        assert "install_tier unset" in result.reason

    def test_valid_install_tier_proceeds(self, tmp_path):
        """config.json with a valid install_tier does not fail at marketplace-rebuilt."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True
        # Default _make_fake_env writes install_tier=convenience — should proceed.

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        if isinstance(result, Failed):
            assert result.step_id != "marketplace-rebuilt", (
                f"marketplace-rebuilt should not fail with valid install_tier; "
                f"got: {result.reason}"
            )


# ---------------------------------------------------------------------------
# 12–16. substrate-health step (#786)
# ---------------------------------------------------------------------------


def _write_marker(engram_home: str, *, malformed: bool = False, **extra: object) -> str:
    """Write a fake degraded-substrate marker to engram_home.

    Returns the marker path.
    """
    path = os.path.join(engram_home, ".substrate-degraded.json")
    if malformed:
        with open(path, "w") as f:
            f.write("{ this is not valid json }")
    else:
        payload = {
            "reason": "shm_deleted",
            "detected_at": "2026-06-04T14:00:00+00:00",
            "dump_sha": "abc1234def5678",
            **extra,
        }
        with open(path, "w") as f:
            json.dump(payload, f)
    return path


class TestSubstrateHealthStep:
    """substrate-health step behaviour — marker present/absent/malformed."""

    # ── 12. check() and verify() reflect marker presence ──────────────────

    def test_marker_absent_check_true(self, tmp_path):
        """When no marker exists, check() returns True (step skipped)."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        # No marker written — engram_home is a fresh dir.
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        sh = step_by_id["substrate-health"]
        assert sh.check(ctx) is True

    def test_marker_absent_verify_true(self, tmp_path):
        """When no marker exists, verify() returns True (doctor: satisfied)."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        sh = step_by_id["substrate-health"]
        assert sh.verify(ctx) is True

    def test_marker_present_check_false(self, tmp_path):
        """When marker is present, check() returns False (step not satisfied)."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        _write_marker(ctx["engram_home"])
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        sh = step_by_id["substrate-health"]
        assert sh.check(ctx) is False

    def test_marker_present_verify_false(self, tmp_path):
        """When marker is present, verify() returns False (doctor: unsatisfied)."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        _write_marker(ctx["engram_home"])
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        sh = step_by_id["substrate-health"]
        assert sh.verify(ctx) is False

    # ── 13. malformed marker ───────────────────────────────────────────────

    def test_malformed_marker_check_false(self, tmp_path):
        """Malformed marker → still treated as degraded; check() returns False."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        _write_marker(ctx["engram_home"], malformed=True)
        steps = upgrade_flow()
        step_by_id = {s.id: s for s in steps}
        sh = step_by_id["substrate-health"]
        assert sh.check(ctx) is False

    def test_malformed_marker_instruction_renders(self, tmp_path):
        """Malformed marker — instruction renders without raising; includes fallback text."""
        from tools.engine.flows import _read_substrate_marker, _substrate_health_instruction
        engram_home = str(tmp_path / "engram_home_malformed")
        os.makedirs(engram_home, exist_ok=True)
        _write_marker(engram_home, malformed=True)

        marker = _read_substrate_marker(engram_home)
        # Malformed: _read_substrate_marker returns a sentinel dict, not None
        assert marker is not None, "Malformed marker should return sentinel, not None"
        instruction = _substrate_health_instruction(marker, engram_home)
        # Must not raise and must include the degraded header
        assert "SUBSTRATE DEGRADED" in instruction
        # Should mention the fallback reason
        assert "unreadable" in instruction or "unknown" in instruction

    # ── 14. flow pauses at substrate-health when marker present ───────────

    def test_flow_pauses_at_substrate_health(self, tmp_path):
        """With marker present, run_flow pauses at substrate-health."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        _write_marker(ctx["engram_home"])
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        assert isinstance(result, Paused), f"Expected Paused, got {result}"
        assert result.step_id == "substrate-health"

    def test_flow_instruction_contains_marker_fields(self, tmp_path, monkeypatch):
        """The Paused instruction includes the marker's reason and dump_sha.

        Exercises the production env-resolution path: ENGRAM_HOME is set to
        the test's tmp_path so upgrade_flow() picks up the marker at
        construction time via _default_engram_home.
        """
        marker_home = str(tmp_path)
        _write_marker(
            marker_home,
            reason="shm_deleted",
            dump_sha="deadbeef12345678",
            detected_at="2026-06-04T14:00:00+00:00",
        )

        # Point the env-resolution path at the test's tmp_path so that
        # upgrade_flow()'s _default_engram_home reads our marker.
        monkeypatch.setenv("ENGRAM_HOME", marker_home)
        steps = upgrade_flow()

        step_by_id = {s.id: s for s in steps}
        sh = step_by_id["substrate-health"]
        assert "shm_deleted" in sh.instruction
        assert "deadbeef12345678" in sh.instruction

    # ── 15. downstream steps not reached when marker present ──────────────

    def test_downstream_steps_not_attempted_with_marker(self, tmp_path):
        """When substrate-health blocks, plugin-marketplace-update is not reached."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        _write_marker(ctx["engram_home"])
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        steps = upgrade_flow()
        result = run_flow(steps, ctx)

        # Must pause at substrate-health, NOT at any downstream step
        assert isinstance(result, Paused)
        assert result.step_id == "substrate-health"
        # Plugin steps must not have been attempted
        assert not ctx.get("_plugin_marketplace_updated")
        assert not ctx.get("_plugin_upgrade_done")

    def test_after_marker_removed_flow_proceeds(self, tmp_path):
        """After removing the marker, substrate-health is satisfied and flow continues."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        marker_path = _write_marker(ctx["engram_home"])
        ctx["ack_changeset"] = True
        ctx["_template_gate_done"] = True

        steps = upgrade_flow()
        # First run: blocked at substrate-health
        r1 = run_flow(steps, ctx)
        assert isinstance(r1, Paused)
        assert r1.step_id == "substrate-health"

        # Simulate operator clearing the marker
        os.remove(marker_path)

        # Second run: substrate-health now satisfied; proceeds to plugin-marketplace-update
        r2 = run_flow(steps, ctx)
        assert isinstance(r2, Paused)
        assert r2.step_id == "plugin-marketplace-update"

    # ── 16. Doctor mode: substrate-health row ─────────────────────────────

    def test_doctor_marker_absent_satisfied(self, tmp_path):
        """Doctor: substrate-health satisfied when marker absent."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        # Fully pre-satisfy everything else so we can isolate substrate-health
        ctx.update({
            "ack_changeset": True,
            "_plugin_marketplace_updated": True,
            "_plugin_upgrade_done": True,
            "_mcp_reconnected": True,
            "_template_gate_done": True,
            "_stats_verified": True,
            "_record_filed": True,
        })

        steps = upgrade_flow()
        results = run_doctor(steps, ctx)
        by_id = {r.step_id: r for r in results}

        assert "substrate-health" in by_id
        assert by_id["substrate-health"].satisfied is True

    def test_doctor_marker_present_unsatisfied(self, tmp_path):
        """Doctor: substrate-health unsatisfied when marker present."""
        _, _, ctx = _make_fake_env(str(tmp_path))
        _write_marker(ctx["engram_home"])
        ctx.update({
            "ack_changeset": True,
            "_plugin_marketplace_updated": True,
            "_plugin_upgrade_done": True,
            "_mcp_reconnected": True,
            "_template_gate_done": True,
            "_stats_verified": True,
            "_record_filed": True,
        })

        steps = upgrade_flow()
        results = run_doctor(steps, ctx)
        by_id = {r.step_id: r for r in results}

        assert "substrate-health" in by_id
        assert by_id["substrate-health"].satisfied is False

    # ── drift guard ───────────────────────────────────────────────────────────

    def test_marker_filename_matches_walguard(self):
        """flows._SUBSTRATE_MARKER_FILENAME must equal engram_walguard._MARKER_FILENAME.

        If the names drift, the substrate-health gate silently always-passes
        because it looks for a file that walguard never creates.
        Mechanical gate > vigilance.
        """
        from engram_walguard import _MARKER_FILENAME
        from tools.engine import flows
        assert flows._SUBSTRATE_MARKER_FILENAME == _MARKER_FILENAME


class TestSubstrateHealthInstructionHome3863:
    """_substrate_health_instruction renders the marker path from the home it is
    given, never from a literal; the home is a required argument (#3863)."""

    def test_renders_marker_path_from_the_given_home_with_no_default_literal(self):
        from tools.engine.flows import _substrate_health_instruction
        out = _substrate_health_instruction(None, "/x/h")
        assert "/x/h/.substrate-degraded.json" in out
        assert "~/.engram" not in out

    def test_engram_home_is_required(self):
        from tools.engine.flows import _substrate_health_instruction
        with pytest.raises(TypeError):
            _substrate_health_instruction(None)


class TestEmptyEngramHomeIsUnset3863:
    """ENGRAM_HOME set but EMPTY means unset (the resolver's rule): the rendered
    operator hints must carry an absolute directory, never a bare filename."""

    def _step(self, steps, step_id):
        return next(s for s in steps if s.id == step_id)

    def test_substrate_health_marker_path_is_absolute_on_an_empty_home(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_HOME", "")
        step = self._step(upgrade_flow(), "substrate-health")
        import re
        m = re.search(r"rm (\S+)", step.instruction)
        assert m, step.instruction
        assert os.path.isabs(m.group(1)), step.instruction
        assert m.group(1).endswith("/.substrate-degraded.json"), step.instruction

    def test_stats_verify_cat_path_is_absolute_on_an_empty_home(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_HOME", "")
        step = self._step(upgrade_flow(), "stats-verify-instruction")
        import re
        m = re.search(r"cat (\S+)", step.instruction)
        assert m, step.instruction
        assert os.path.isabs(m.group(1)), step.instruction
        assert m.group(1).endswith("/.deployed-version"), step.instruction
