"""Tests for the hooks.json ↔ shipped-files consistency gate (Fix 3, #713).

Round-2 update: hooks.json is now FILTERED at build time (step 3 of
build-plugin.sh) rather than copied wholesale.  The gate in step 6b is
therefore all-or-nothing: every file referenced in the built hooks.json MUST
be present on disk — no skipping of "intentionally excluded" entries.

Round-3 update: engram-baton-prompt-hook.py is now registered in
hooks/hooks.json (completeness sweep found it was silently dropped during
scatter→plugin migration).  Tests updated to assert baton appears in the
built hooks.json for MA+dev builds and is absent for non-MA builds.

Two test classes:

1. TestHooksConsistencyGate
   - Runs build-plugin.sh (default + --multi-agent --tier dev) and asserts:
       (a) Default (convenience, no MA) build: hooks.json has NO
           engram-inter-agent-prompt-hook.py / engram-baton-prompt-hook.py
           entries, AND every referenced file is present.
       (b) --tier dev --multi-agent build: hooks.json references inter-agent +
           baton AND both files are present.
       (c) All-or-nothing gate: removing a referenced hook file after a dev+MA
           build causes the consistency check to flag it as missing.

2. TestMultiAgentHooksShip
   - Asserts that multi-agent hooks (engram-inter-agent-prompt-hook.py and
     engram-baton-prompt-hook.py) are present in build/plugin/hooks/ after a
     --tier dev --multi-agent build — the acceptance criterion for #713.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_PLUGIN_SH = os.path.join(REPO_ROOT, "tools", "build-plugin.sh")
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")

HOOK_REF_RE = re.compile(r'\bhooks/([A-Za-z0-9_\-]+\.(?:py|sh))\b')

TIER_RANKS = {"essential": 0, "convenience": 1, "dev": 2}

# ── helpers ───────────────────────────────────────────────────────────────────


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _check_hooks_consistency_all_or_nothing(
    plugin_root: str,
) -> list[str]:
    """Return list of hook filenames referenced in hooks.json but MISSING on disk.

    All-or-nothing semantics: every entry in the built hooks.json is expected
    to have a corresponding file present.  The filtering step in build-plugin.sh
    step 3 already removed entries for non-shipped hooks, so any reference that
    remains IS expected.  No skipping.

    Mirrors the logic in build-plugin.sh step 6b gate (round-2, #713).
    """
    hooks_json_path = os.path.join(plugin_root, "hooks", "hooks.json")
    with open(hooks_json_path, encoding="utf-8") as f:
        data = json.load(f)

    referenced: set[str] = set()
    for event_name, groups in data.get("hooks", {}).items():
        for group in groups:
            for entry in group.get("hooks", []):
                cmd = entry.get("command", "")
                for m in HOOK_REF_RE.finditer(cmd):
                    referenced.add(m.group(1))

    missing: list[str] = []
    for fname in sorted(referenced):
        fpath = os.path.join(plugin_root, "hooks", fname)
        if not os.path.exists(fpath):
            missing.append(fname)

    return missing


def _hooks_referenced_in_built_json(plugin_root: str) -> set[str]:
    """Return set of hook filenames referenced in the BUILT hooks.json."""
    hooks_json_path = os.path.join(plugin_root, "hooks", "hooks.json")
    with open(hooks_json_path, encoding="utf-8") as f:
        data = json.load(f)

    referenced: set[str] = set()
    for event_name, groups in data.get("hooks", {}).items():
        for group in groups:
            for entry in group.get("hooks", []):
                cmd = entry.get("command", "")
                for m in HOOK_REF_RE.finditer(cmd):
                    referenced.add(m.group(1))
    return referenced


def _run_build(extra_args: list[str]) -> subprocess.CompletedProcess:
    """Run build-plugin.sh from repo root with given extra args."""
    # --allow-branch: tests may run on feature branches, not just dev (#794 guard override)
    cmd = ["bash", BUILD_PLUGIN_SH, "--allow-branch"] + extra_args
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


PLUGIN_ROOT = os.path.join(REPO_ROOT, "build", "plugin")


# ── test class 1: consistency gate ────────────────────────────────────────────


class TestHooksConsistencyGate:
    """Verify the hooks.json ↔ shipped-files consistency gate in build-plugin.sh.

    Round-2: hooks.json is filtered at build time, so:
      (a) default build must have NO inter-agent/baton entries in hooks.json
      (b) dev+MA build must have inter-agent+baton entries AND their files present
      (c) all-or-nothing gate fires when a referenced file is absent
    """

    # MA-only hooks that must be absent from non-MA builds.
    INTER_AGENT_HOOK = "engram-inter-agent-prompt-hook.py"
    BATON_HOOK = "engram-baton-prompt-hook.py"

    def test_default_build_no_ma_entries_in_hooks_json(self):
        """Default build (convenience, no --multi-agent): hooks.json has no MA entries.

        Acceptance criterion (a): the built hooks.json must NOT reference
        engram-inter-agent-prompt-hook.py or engram-baton-prompt-hook.py, and
        every file that IS referenced must be present on disk.
        This is the root-cause regression test for #713: the round-1 gate
        produced a false pass because hooks.json was copied wholesale while
        the inter-agent hook file was tier-filtered out.
        """
        result = _run_build([])
        assert result.returncode == 0, (
            f"build-plugin.sh (default) exited non-zero:\n{result.stderr}"
        )

        referenced = _hooks_referenced_in_built_json(PLUGIN_ROOT)

        # (a1) MA-only hooks must NOT appear in the built hooks.json.
        assert self.INTER_AGENT_HOOK not in referenced, (
            f"Default build: hooks.json still references '{self.INTER_AGENT_HOOK}' "
            "even though it is a multi-agent-only hook.  The hooks.json filter step "
            "did not drop this entry.  (#713 regression)"
        )
        assert self.BATON_HOOK not in referenced, (
            f"Default build: hooks.json still references '{self.BATON_HOOK}' "
            "even though it is a multi-agent-only hook.  (#713 regression)"
        )

        # (a2) Every hook that IS referenced must be present (all-or-nothing).
        missing = _check_hooks_consistency_all_or_nothing(PLUGIN_ROOT)
        assert not missing, (
            "Default build: hooks.json references hook file(s) not present in "
            "build/plugin/hooks/:\n"
            + "\n".join(f"  MISSING: hooks/{f}" for f in missing)
        )

    def test_dev_multi_agent_build_hooks_consistency(self):
        """--tier dev --multi-agent build: inter-agent AND baton in hooks.json AND both files present.

        Acceptance criterion (b):
          - The built hooks.json references engram-inter-agent-prompt-hook.py AND
            that file is present on disk.
          - The built hooks.json references engram-baton-prompt-hook.py (registered
            in hooks.json source as of round-3) AND that file is present on disk.
          - Every file referenced in hooks.json is present on disk (all-or-nothing).
        """
        result = _run_build(["--tier", "dev", "--multi-agent"])
        assert result.returncode == 0, (
            f"build-plugin.sh --tier dev --multi-agent exited non-zero:\n{result.stderr}"
        )

        referenced = _hooks_referenced_in_built_json(PLUGIN_ROOT)

        # Inter-agent hook must appear in the built hooks.json (it is registered).
        assert self.INTER_AGENT_HOOK in referenced, (
            f"--tier dev --multi-agent build: hooks.json does NOT reference "
            f"'{self.INTER_AGENT_HOOK}'.  Expected it to be present at dev+MA."
        )

        # Inter-agent file must be on disk.
        assert os.path.exists(os.path.join(PLUGIN_ROOT, "hooks", self.INTER_AGENT_HOOK)), (
            f"'{self.INTER_AGENT_HOOK}' is NOT present in build/plugin/hooks/ "
            "after --tier dev --multi-agent build."
        )

        # Baton hook must appear in the built hooks.json (registered in source as of round-3).
        assert self.BATON_HOOK in referenced, (
            f"--tier dev --multi-agent build: hooks.json does NOT reference "
            f"'{self.BATON_HOOK}'.  Expected it to be present at dev+MA after round-3 fix."
        )

        # Baton hook file must be on disk.
        assert os.path.exists(os.path.join(PLUGIN_ROOT, "hooks", self.BATON_HOOK)), (
            f"'{self.BATON_HOOK}' is NOT present in build/plugin/hooks/ "
            "after --tier dev --multi-agent build."
        )

        # Every file referenced in hooks.json must be on disk (all-or-nothing).
        missing = _check_hooks_consistency_all_or_nothing(PLUGIN_ROOT)
        assert not missing, (
            "--tier dev --multi-agent build: hooks.json references hook file(s) not present:\n"
            + "\n".join(f"  MISSING: hooks/{f}" for f in missing)
        )

    def test_consistency_check_detects_missing_hook(self):
        """All-or-nothing gate fires when a shipped+referenced hook file is removed.

        Acceptance criterion (c): after a dev+MA build, temporarily removing a
        referenced hook file must cause the all-or-nothing consistency check to
        flag it as missing.  This validates the failure-mode path.
        """
        result = _run_build(["--tier", "dev", "--multi-agent"])
        assert result.returncode == 0, (
            f"build-plugin.sh --tier dev --multi-agent failed before mutation test:\n{result.stderr}"
        )

        referenced = _hooks_referenced_in_built_json(PLUGIN_ROOT)

        # Pick any hook that is referenced AND exists on disk.
        target_fname = None
        for fname in sorted(referenced):
            fpath = os.path.join(PLUGIN_ROOT, "hooks", fname)
            if os.path.exists(fpath):
                target_fname = fname
                break

        assert target_fname is not None, (
            "Could not find any hook file that is referenced in hooks.json AND "
            "present on disk.  Check the build or hooks.json structure."
        )

        target_path = os.path.join(PLUGIN_ROOT, "hooks", target_fname)
        tmp_path = target_path + ".test-removed"
        try:
            os.rename(target_path, tmp_path)
            missing = _check_hooks_consistency_all_or_nothing(PLUGIN_ROOT)
            assert target_fname in missing, (
                f"All-or-nothing consistency check did NOT flag the removed hook "
                f"file '{target_fname}'.  The gate is not working correctly."
            )
        finally:
            if os.path.exists(tmp_path):
                os.rename(tmp_path, target_path)


# ── test class 2: multi-agent hooks ship at dev+MA build ──────────────────────


class TestMultiAgentHooksShip:
    """Verify multi-agent hooks are present in the --tier dev --multi-agent build.

    Acceptance criterion for #713: engram-inter-agent-prompt-hook.py and
    engram-baton-prompt-hook.py must ship at --tier dev --multi-agent.  Their
    absence while registered in hooks.json caused UserPromptSubmit FileNotFoundError
    on first-seat scatter→plugin migration.
    """

    INTER_AGENT_HOOK = "engram-inter-agent-prompt-hook.py"
    BATON_HOOK = "engram-baton-prompt-hook.py"

    def setup_method(self):
        """Build with --tier dev --multi-agent before each test."""
        result = _run_build(["--tier", "dev", "--multi-agent"])
        assert result.returncode == 0, (
            f"build-plugin.sh --tier dev --multi-agent failed in setup:\n{result.stderr}"
        )

    def test_inter_agent_hook_present(self):
        """engram-inter-agent-prompt-hook.py ships at --tier dev --multi-agent."""
        hook_path = os.path.join(PLUGIN_ROOT, "hooks", self.INTER_AGENT_HOOK)
        assert os.path.exists(hook_path), (
            f"{self.INTER_AGENT_HOOK} is NOT present in build/plugin/hooks/ after "
            "--tier dev --multi-agent build. This is the #713 regression: "
            "the hook is registered in hooks.json but absent from the shipped files."
        )

    def test_baton_hook_present(self):
        """engram-baton-prompt-hook.py ships at --tier dev --multi-agent."""
        hook_path = os.path.join(PLUGIN_ROOT, "hooks", self.BATON_HOOK)
        assert os.path.exists(hook_path), (
            f"{self.BATON_HOOK} is NOT present in build/plugin/hooks/ after "
            "--tier dev --multi-agent build."
        )

    def test_inter_agent_hook_in_hooks_json(self):
        """engram-inter-agent-prompt-hook.py is referenced in the built hooks.json."""
        referenced = _hooks_referenced_in_built_json(PLUGIN_ROOT)
        assert self.INTER_AGENT_HOOK in referenced, (
            f"--tier dev --multi-agent build: hooks.json does NOT reference "
            f"'{self.INTER_AGENT_HOOK}'.  Expected the MA hook to appear."
        )

    def test_baton_hook_in_hooks_json(self):
        """engram-baton-prompt-hook.py is referenced in the built hooks.json at dev+MA.

        Round-3: baton is now registered in the source hooks.json (completeness sweep
        found it was silently dropped during scatter→plugin migration).  This is now a
        hard assertion — baton MUST appear in the built hooks.json for --tier dev
        --multi-agent builds, not just if it happens to be present.
        """
        referenced = _hooks_referenced_in_built_json(PLUGIN_ROOT)
        assert self.BATON_HOOK in referenced, (
            f"--tier dev --multi-agent build: hooks.json does NOT reference "
            f"'{self.BATON_HOOK}'.  Round-3 added baton to source hooks.json; "
            "the built json must reflect this registration."
        )
        hook_path = os.path.join(PLUGIN_ROOT, "hooks", self.BATON_HOOK)
        assert os.path.exists(hook_path), (
            f"hooks.json references '{self.BATON_HOOK}' but the file is absent."
        )

    def test_build_manifest_reflects_dev_ma(self):
        """Build manifest records tier=dev, multi_agent=true after dev+MA build."""
        manifest_path = os.path.join(PLUGIN_ROOT, ".engram-build-manifest.json")
        assert os.path.exists(manifest_path), (
            ".engram-build-manifest.json missing after build."
        )
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        assert manifest.get("tier") == "dev", (
            f"Build manifest tier is {manifest.get('tier')!r}, expected 'dev'."
        )
        assert manifest.get("multi_agent") is True, (
            f"Build manifest multi_agent is {manifest.get('multi_agent')!r}, expected true."
        )

    def test_hooks_consistency_after_dev_ma_build(self):
        """All-or-nothing consistency passes on the --tier dev --multi-agent build."""
        missing = _check_hooks_consistency_all_or_nothing(PLUGIN_ROOT)
        assert not missing, (
            "--tier dev --multi-agent build: hooks.json references hook file(s) not present:\n"
            + "\n".join(f"  MISSING: hooks/{f}" for f in missing)
        )
