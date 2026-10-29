"""Tests for the hooks.json ↔ shipped-files consistency gate (Fix 3, #713).

Round-2 update: hooks.json is now FILTERED at build time (step 3 of
build-plugin.sh) rather than copied wholesale.  The gate in step 6b is
therefore all-or-nothing: every file referenced in the built hooks.json MUST
be present on disk — no skipping of "intentionally excluded" entries.

Round-3 update: engram-baton-prompt-hook.py is now registered in
hooks/hooks.json (completeness sweep found it was silently dropped during
scatter→plugin migration).  Tests updated to assert baton appears in the
built hooks.json for MA+dev builds and is absent for non-MA builds.

Round-4 update (#2857 step 1): engram-inter-agent-prompt-hook.py and
engram-baton-prompt-hook.py are no longer multi_agent-gated in tiers.json —
they are genuinely forum-backed (HTTP to the LAN forum) and symmetric for
any agent, alone on its host or not, once a forum is configured. They now
ship in the DEFAULT (non-MA) build too, not only at --tier dev --multi-agent.
Test (a) below is inverted accordingly.

Two test classes:

1. TestHooksConsistencyGate
   - Runs build-plugin.sh (default + --multi-agent --tier dev) and asserts:
       (a) Default (convenience, no MA) build: hooks.json DOES reference
           engram-inter-agent-prompt-hook.py / engram-baton-prompt-hook.py
           (un-gated as of #2857 step 1), AND every referenced file is
           present.
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
    cmd = ["bash", BUILD_PLUGIN_SH, "--allow-branch", "--allow-untagged"] + extra_args
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
      (a) default build must have inter-agent/baton entries in hooks.json too,
          as of #2857 step 1 (they are forum-backed and un-gated)
      (b) dev+MA build must have inter-agent+baton entries AND their files present
      (c) all-or-nothing gate fires when a referenced file is absent
    """

    # Forum-backed hooks — un-gated as of #2857 step 1 (were MA-only before).
    INTER_AGENT_HOOK = "engram-inter-agent-prompt-hook.py"
    BATON_HOOK = "engram-baton-prompt-hook.py"

    def test_default_build_has_forum_backed_hooks_in_hooks_json(self):
        """Default build (convenience, no --multi-agent): hooks.json has the
        forum-backed hooks too, and every referenced file is present.

        Acceptance criterion (a), inverted by #2857 step 1: the built
        hooks.json now DOES reference engram-inter-agent-prompt-hook.py and
        engram-baton-prompt-hook.py in the DEFAULT build, since neither is
        multi_agent-gated any longer (both are genuinely forum-backed and
        symmetric for any agent once a forum is configured). Pre-#2857 this
        test asserted the opposite (their ABSENCE) as the root-cause
        regression test for #713: the round-1 gate produced a false pass
        because hooks.json was copied wholesale while the inter-agent hook
        file was tier-filtered out. The all-or-nothing check (a2) below is
        the part of #713's regression coverage that still applies unchanged.
        """
        result = _run_build([])
        assert result.returncode == 0, (
            f"build-plugin.sh (default) exited non-zero:\n{result.stderr}"
        )

        referenced = _hooks_referenced_in_built_json(PLUGIN_ROOT)

        # (a1) Forum-backed hooks now DO appear in the default build's hooks.json.
        assert self.INTER_AGENT_HOOK in referenced, (
            f"Default build: hooks.json does not reference '{self.INTER_AGENT_HOOK}'. "
            "It should now ship un-gated (#2857 step 1) — it is genuinely forum-backed."
        )
        assert self.BATON_HOOK in referenced, (
            f"Default build: hooks.json does not reference '{self.BATON_HOOK}'. "
            "It should now ship un-gated (#2857 step 1) — it is genuinely forum-backed."
        )

        # (a2) Every hook that IS referenced must be present (all-or-nothing).
        # This is the surviving #713 regression coverage: the round-1 gate
        # produced a false pass because hooks.json was copied wholesale while
        # a referenced hook file was tier-filtered out from disk.
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
        """Build manifest records tier=dev and no multi_agent key after a build (#4255)."""
        manifest_path = os.path.join(PLUGIN_ROOT, ".engram-build-manifest.json")
        assert os.path.exists(manifest_path), (
            ".engram-build-manifest.json missing after build."
        )
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        assert manifest.get("tier") == "dev", (
            f"Build manifest tier is {manifest.get('tier')!r}, expected 'dev'."
        )
        assert "multi_agent" not in manifest, (
            f"Build manifest still carries multi_agent={manifest.get('multi_agent')!r}."
        )

    def test_hooks_consistency_after_dev_ma_build(self):
        """All-or-nothing consistency passes on the --tier dev --multi-agent build."""
        missing = _check_hooks_consistency_all_or_nothing(PLUGIN_ROOT)
        assert not missing, (
            "--tier dev --multi-agent build: hooks.json references hook file(s) not present:\n"
            + "\n".join(f"  MISSING: hooks/{f}" for f in missing)
        )


# ── test class 4: {{PYTHON}} substitution in built hooks.json ─────────────────


class TestHooksJsonPythonSubstitution:
    """Every hook command in the built hooks.json must invoke the pinned venv
    python, not a bare/unsubstituted interpreter (#1831 — bare python3 on a
    fresh macOS install resolves to Apple's CLT-bundled 3.9.6, too old for
    hooks using PEP 604 union-type annotations).

    Guards the exact regression this fixes and the one it could reintroduce:
    a future hook added with a literal 'python3' typed out of habit, bypassing
    the {{PYTHON}} -> pinned-venv substitution.
    """

    def setup_method(self):
        result = _run_build([])
        assert result.returncode == 0, (
            f"build-plugin.sh (default) failed in setup:\n{result.stderr}"
        )

    def _all_commands(self) -> list[str]:
        hooks_json_path = os.path.join(PLUGIN_ROOT, "hooks", "hooks.json")
        with open(hooks_json_path, encoding="utf-8") as f:
            data = json.load(f)
        commands = []
        for groups in data.get("hooks", {}).values():
            for group in groups:
                for entry in group.get("hooks", []):
                    cmd = entry.get("command", "")
                    if cmd:
                        commands.append(cmd)
        return commands

    def test_no_literal_python_template_token_remains(self):
        """No command string still contains the unsubstituted '{{PYTHON}}' token."""
        commands = self._all_commands()
        assert commands, "Built hooks.json has no command-type hook entries to check."
        offenders = [c for c in commands if "{{PYTHON}}" in c]
        assert not offenders, (
            "Built hooks.json still contains a literal '{{PYTHON}}' token — the "
            "build-time substitution did not run on every entry:\n"
            + "\n".join(f"  {c}" for c in offenders)
        )

    def test_every_python_command_invokes_pinned_venv(self):
        """Every hook command that runs python invokes the pinned venv, not bare python3.

        Not every hook command is python (e.g. start-engram-daemon.sh runs via
        `bash "..."`) — only commands that actually invoke a python interpreter
        are in scope here.
        """
        commands = self._all_commands()
        assert commands, "Built hooks.json has no command-type hook entries to check."
        python_commands = [c for c in commands if "python3" in c]
        assert python_commands, (
            "No command in the built hooks.json invokes python3 at all — "
            "expected at least the session-start/prompt-submit hooks to."
        )
        offenders = [
            c for c in python_commands
            if not c.startswith("$HOME/.engram/venv/bin/python3 ")
        ]
        assert not offenders, (
            "Found hook command(s) invoking python3 without the pinned venv "
            "($HOME/.engram/venv/bin/python3) — likely a new hook added with a "
            "bare 'python3' typed out of habit instead of the {{PYTHON}} template:\n"
            + "\n".join(f"  {c}" for c in offenders)
        )
