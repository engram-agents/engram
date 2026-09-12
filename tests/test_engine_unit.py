"""Unit tests for tools.engine.manifest.select_shippables and
tools.engine.build.filter_hooks_json.

Tests:
  1. select_shippables — happy path + edges
     - Essential tier returns only essential entries
     - Convenience tier is a superset of essential
     - Dev tier is a superset of convenience
     - MA-gated entries excluded without --multi-agent
     - MA-gated entries included with --multi-agent
     - excluded[] entries are never in select_shippables (they aren't in mechanisms[])
  2. filter_hooks_json — happy path + edges
     - MA-flagged hook dropped when hook isn't in shipped set
     - MA-flagged hook kept when hook IS in shipped set
     - Entry with no hook-file reference always kept
     - Empty group pruned
     - Empty event pruned
     - Hook wired but not shipped → consistency failure detected by check_hooks_consistency
"""

from __future__ import annotations

import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# select_shippables unit tests
# ---------------------------------------------------------------------------


class TestSelectShippables:
    """Unit tests for tools.engine.manifest.select_shippables."""

    def setup_method(self):
        from tools.engine.manifest import select_shippables, load_manifest
        self.select_shippables = select_shippables
        self.manifest = load_manifest(MANIFEST_PATH)

    def test_essential_only_returns_essential_entries(self):
        """Essential tier: only entries tagged essential should be returned."""
        result = self.select_shippables(self.manifest, "essential", multi_agent=False)
        paths = {e["path"] for e in result}
        # All returned entries must be tagged essential
        for entry in result:
            assert entry["tier"] == "essential", (
                f"select_shippables(essential) returned non-essential entry: "
                f"{entry['path']!r} (tier={entry['tier']!r})"
            )
        # Known essential entry must be present
        assert "server.py" in paths
        assert "hooks/claude/engram-surface-hook.py" in paths

    def test_essential_excludes_convenience_entries(self):
        """Essential tier must NOT include convenience-tier entries."""
        result = self.select_shippables(self.manifest, "essential", multi_agent=False)
        paths = {e["path"] for e in result}
        # Known convenience entry must be absent
        assert "skills/claude/engram-loop" not in paths
        assert "hooks/claude/engram-deference-detector-prompt.py" not in paths

    def test_convenience_is_superset_of_essential(self):
        """Convenience set must include everything essential ships."""
        essential = {e["path"] for e in self.select_shippables(self.manifest, "essential", False)}
        convenience = {e["path"] for e in self.select_shippables(self.manifest, "convenience", False)}
        missing = essential - convenience
        assert not missing, (
            f"convenience set is not a superset of essential: {sorted(missing)}"
        )

    def test_dev_is_superset_of_convenience(self):
        """Dev set (no MA) must include everything convenience (no MA) ships."""
        conv = {e["path"] for e in self.select_shippables(self.manifest, "convenience", False)}
        dev = {e["path"] for e in self.select_shippables(self.manifest, "dev", False)}
        missing = conv - dev
        assert not missing, (
            f"dev set is not a superset of convenience: {sorted(missing)}"
        )

    def test_ma_gated_entry_excluded_without_flag(self):
        """MA-gated entries must not appear when multi_agent=False.

        #2857 step 1 un-gated every genuinely forum-backed hook/skill
        (baton/inter-agent hooks, engram-letter, etc.) — they ship
        regardless of --multi-agent now. The remaining MA-gated entries in
        tiers.json are the 6 NOT-forum-backed ones (session.py /
        _channel.py / _session_context.py / agents.json.example / the two
        collab-*-monitor.sh scripts); use two of those as the "known
        MA-gated" fixtures instead.
        """
        result = self.select_shippables(self.manifest, "convenience", multi_agent=False)
        paths = {e["path"] for e in result}
        # Known MA-gated entries (still gated post-#2857 step 1)
        assert "tools/session.py" not in paths, (
            "tools/session.py must not ship without --multi-agent"
        )
        assert "tools/collab-baton-monitor.sh" not in paths, (
            "tools/collab-baton-monitor.sh must not ship without --multi-agent"
        )
        # Forum-backed entries are un-gated now — they DO ship without --multi-agent
        assert "hooks/claude/engram-baton-prompt-hook.py" in paths, (
            "engram-baton-prompt-hook.py should ship without --multi-agent "
            "now that it is forum-backed and un-gated (#2857 step 1)"
        )
        assert "hooks/claude/engram-inter-agent-prompt-hook.py" in paths, (
            "engram-inter-agent-prompt-hook.py should ship without --multi-agent "
            "now that it is forum-backed and un-gated (#2857 step 1)"
        )
        assert "skills/claude/engram-letter" in paths, (
            "engram-letter skill should ship without --multi-agent "
            "now that it is forum-backed and un-gated (#2857 step 1)"
        )

    def test_ma_gated_entry_included_with_flag(self):
        """MA-gated entries at the right tier must appear when multi_agent=True."""
        result = self.select_shippables(self.manifest, "convenience", multi_agent=True)
        paths = {e["path"] for e in result}
        assert "tools/session.py" in paths
        assert "tools/collab-baton-monitor.sh" in paths

    def test_ma_gated_blocked_by_tier_even_with_flag(self):
        """An MA-gated convenience entry must not ship at essential tier, even with MA flag."""
        result = self.select_shippables(self.manifest, "essential", multi_agent=True)
        paths = {e["path"] for e in result}
        assert "tools/session.py" not in paths, (
            "MA-gated convenience tool must not ship at essential tier even with --multi-agent"
        )

    def test_dev_tier_ships_dev_entries(self):
        """Dev tier must include entries tagged dev."""
        result = self.select_shippables(self.manifest, "dev", multi_agent=False)
        paths = {e["path"] for e in result}
        assert "tools/scan-leaks.py" in paths
        assert "tools/surgical.py" in paths

    def test_dev_entries_absent_at_convenience(self):
        """Dev-tagged entries must not ship at convenience tier."""
        result = self.select_shippables(self.manifest, "convenience", multi_agent=False)
        paths = {e["path"] for e in result}
        assert "tools/scan-leaks.py" not in paths
        assert "tools/surgical.py" not in paths

    def test_excluded_entries_never_returned(self):
        """Entries in manifest['excluded'] are not in mechanisms[] and never returned."""
        from tools.engine.manifest import select_shippables
        excluded_paths = {e["path"] for e in self.manifest.get("excluded", [])}
        result = self.select_shippables(self.manifest, "dev", multi_agent=True)
        returned_paths = {e["path"] for e in result}
        overlap = excluded_paths & returned_paths
        assert not overlap, (
            f"select_shippables returned excluded paths: {sorted(overlap)}"
        )

    def test_preserves_manifest_order(self):
        """select_shippables returns entries in manifest order (same as mechanisms[])."""
        result = self.select_shippables(self.manifest, "dev", multi_agent=True)
        # All result paths must appear in the same relative order as in mechanisms[]
        mechanism_paths = [e["path"] for e in self.manifest["mechanisms"]]
        result_paths = [e["path"] for e in result]
        # result_paths is a subsequence of mechanism_paths
        pos = 0
        for rp in result_paths:
            while pos < len(mechanism_paths) and mechanism_paths[pos] != rp:
                pos += 1
            assert pos < len(mechanism_paths), (
                f"result path {rp!r} not found in mechanisms[] at expected position"
            )
            pos += 1


# ---------------------------------------------------------------------------
# filter_hooks_json unit tests
# ---------------------------------------------------------------------------


class TestFilterHooksJson:
    """Unit tests for tools.engine.build.filter_hooks_json."""

    def setup_method(self):
        from tools.engine.build import filter_hooks_json, check_hooks_consistency
        self.filter_hooks_json = filter_hooks_json
        self.check_hooks_consistency = check_hooks_consistency

    def _make_hooks_cfg(self, entries: list[dict]) -> dict:
        """Helper: wrap entries into a minimal hooks.json structure."""
        return {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": entries}
                ]
            }
        }

    def test_shipped_hook_kept(self):
        """An entry whose hook file is in shipped_paths is kept."""
        shipped = {"hooks/claude/engram-surface-hook.py"}
        hooks_cfg = self._make_hooks_cfg([
            {
                "type": "command",
                "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\"",
            }
        ])
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        groups = filtered["hooks"]["UserPromptSubmit"]
        assert len(groups) == 1
        assert len(groups[0]["hooks"]) == 1

    def test_unshipped_hook_dropped(self):
        """An entry whose hook file is NOT in shipped_paths is dropped."""
        shipped = set()  # nothing ships
        hooks_cfg = self._make_hooks_cfg([
            {
                "type": "command",
                "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-baton-prompt-hook.py\"",
            }
        ])
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        assert filtered == {"hooks": {}}, (
            "Event should be pruned when all its hooks are dropped"
        )

    def test_entry_without_hook_ref_always_kept(self):
        """An entry with no hooks/<file> reference in command is always kept."""
        shipped = set()
        hooks_cfg = self._make_hooks_cfg([
            {
                "type": "command",
                "command": "echo hello",  # no hooks/ reference
            }
        ])
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        groups = filtered["hooks"]["UserPromptSubmit"]
        assert len(groups[0]["hooks"]) == 1, (
            "Entry with no hooks/<file> ref must not be dropped even if shipped set is empty"
        )

    def test_mixed_entries_partial_drop(self):
        """When only some entries in a group are dropped, the group is kept with remaining."""
        shipped = {"hooks/claude/engram-surface-hook.py"}
        hooks_cfg = self._make_hooks_cfg([
            {
                "type": "command",
                "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\"",
            },
            {
                "type": "command",
                "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-baton-prompt-hook.py\"",
            },
        ])
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        groups = filtered["hooks"]["UserPromptSubmit"]
        assert len(groups) == 1
        assert len(groups[0]["hooks"]) == 1
        kept_cmd = groups[0]["hooks"][0]["command"]
        assert "engram-surface-hook.py" in kept_cmd

    def test_empty_group_pruned(self):
        """When all entries in a group are dropped, the group itself is pruned."""
        shipped = set()
        hooks_cfg = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [
                        {"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-baton-prompt-hook.py\""},
                    ]},
                ]
            }
        }
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        assert "UserPromptSubmit" not in filtered["hooks"], (
            "Event key must be omitted when all its groups are empty"
        )

    def test_empty_event_pruned(self):
        """When all groups for an event are pruned, the event key is omitted."""
        shipped = set()
        hooks_cfg = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-baton-prompt-hook.py\""}]},
                ],
                "Stop": [
                    {"hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\""}]},
                ],
            }
        }
        # ship only the surface hook
        shipped_hooks = {"hooks/claude/engram-surface-hook.py"}
        filtered = self.filter_hooks_json(hooks_cfg, shipped_hooks)
        assert "UserPromptSubmit" not in filtered["hooks"], (
            "UserPromptSubmit must be pruned (its hook is not shipped)"
        )
        assert "Stop" in filtered["hooks"], (
            "Stop must be kept (its hook is shipped)"
        )

    def test_group_metadata_preserved(self):
        """Non-hooks keys in a group (e.g. matcher) are preserved."""
        shipped = {"hooks/claude/engram-surface-hook.py"}
        hooks_cfg = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "mcp__engram__.*",
                        "hooks": [
                            {"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\""},
                        ]
                    }
                ]
            }
        }
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        group = filtered["hooks"]["PreToolUse"][0]
        assert group.get("matcher") == "mcp__engram__.*", (
            "Group metadata (matcher) must be preserved after filtering"
        )

    def test_ma_hook_dropped_when_not_in_shipped_set(self):
        """MA-gated hook is dropped when shipped_paths does not include it.

        This is the core regression from #713: MA hooks were registered in
        hooks.json but not in the shipped files, causing FileNotFoundError.
        """
        # shipped_paths has no MA hooks
        shipped = {
            "hooks/claude/engram-surface-hook.py",
            "hooks/claude/engram-session-start-hook.py",
        }
        hooks_cfg = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [
                        {"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\""},
                        {"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-inter-agent-prompt-hook.py\""},
                        {"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-baton-prompt-hook.py\""},
                    ]}
                ]
            }
        }
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        kept_cmds = [
            e["command"]
            for group in filtered["hooks"]["UserPromptSubmit"]
            for e in group["hooks"]
        ]
        assert len(kept_cmds) == 1
        assert "engram-surface-hook.py" in kept_cmds[0]
        # MA hooks must not appear
        assert not any("inter-agent" in c for c in kept_cmds)
        assert not any("baton" in c for c in kept_cmds)

    def test_wired_but_not_shipped_triggers_consistency_failure(self):
        """check_hooks_consistency catches a hook referenced in hooks.json but absent on disk.

        This verifies the all-or-nothing gate (build-plugin.sh step 6b).
        """
        import tempfile
        import json as _json

        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_dir = os.path.join(tmpdir, "hooks")
            os.makedirs(hooks_dir, exist_ok=True)

            # Write a hooks.json that references a hook file that doesn't exist
            hooks_cfg = {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [
                            {"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/missing-hook.py\""},
                        ]}
                    ]
                }
            }
            hooks_json_path = os.path.join(hooks_dir, "hooks.json")
            with open(hooks_json_path, "w") as f:
                _json.dump(hooks_cfg, f, indent=2)
                f.write("\n")

            # No hook files on disk → consistency check must flag the missing file
            missing = self.check_hooks_consistency(tmpdir)
            assert "missing-hook.py" in missing, (
                "check_hooks_consistency must flag 'missing-hook.py' as missing "
                "when it is referenced in hooks.json but absent on disk"
            )

    def test_consistency_check_passes_when_all_hooks_present(self):
        """check_hooks_consistency passes when all referenced hooks are on disk."""
        import tempfile
        import json as _json

        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_dir = os.path.join(tmpdir, "hooks")
            os.makedirs(hooks_dir, exist_ok=True)

            hook_fname = "engram-surface-hook.py"
            hooks_cfg = {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [
                            {"type": "command", "command": f"python3 \"${{CLAUDE_PLUGIN_ROOT}}/hooks/{hook_fname}\""},
                        ]}
                    ]
                }
            }
            hooks_json_path = os.path.join(hooks_dir, "hooks.json")
            with open(hooks_json_path, "w") as f:
                _json.dump(hooks_cfg, f, indent=2)
                f.write("\n")

            # Create the hook file on disk
            with open(os.path.join(hooks_dir, hook_fname), "w") as f:
                f.write("# dummy hook\n")

            # Consistency check must pass
            missing = self.check_hooks_consistency(tmpdir)
            assert not missing, (
                f"check_hooks_consistency reported missing hooks when all are present: {missing}"
            )

    def test_multiple_events_all_dropped_gives_empty_hooks(self):
        """When all events have all hooks dropped, result is {'hooks': {}}."""
        shipped = set()
        hooks_cfg = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/hook-a.py\""}]}
                ],
                "Stop": [
                    {"hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/hook-b.py\""}]}
                ],
            }
        }
        filtered = self.filter_hooks_json(hooks_cfg, shipped)
        assert filtered == {"hooks": {}}, (
            "When all events are pruned, result must be {'hooks': {}}"
        )


# ---------------------------------------------------------------------------
# compute_build_version edge tests
# ---------------------------------------------------------------------------


class TestComputeBuildVersion:
    """Edge tests for tools.engine.build.compute_build_version."""

    def setup_method(self):
        from tools.engine.build import compute_build_version
        self.compute_build_version = compute_build_version

    def test_pure_numeric_sha7_with_leading_zero_yields_valid_semver(
        self, monkeypatch, tmp_path
    ):
        """A pure-numeric sha7 with a leading zero (e.g. '0149257') must produce
        a valid semver prerelease identifier.

        Semver §9 forbids leading zeroes in numeric identifiers; the fix prefixes
        with 'g' to make the identifier alphanumeric (no leading-zero restriction).
        """
        import subprocess as _subprocess

        # Monkeypatch subprocess.check_output to return the problematic SHA
        def _fake_check_output(cmd, **kwargs):
            if cmd[:2] == ["git", "rev-parse"]:
                return "0149257\n"
            return _subprocess.check_output(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "check_output", _fake_check_output)

        version_str = self.compute_build_version(str(tmp_path), "0.1.0")

        # The sha component must be "g0149257", not "0149257".
        # After the timestamp-first reorder, sha is the SECOND prerelease
        # identifier: "0.1.0-dev.<stamp>.g0149257"
        assert version_str.endswith(".g0149257"), (
            f"Expected 'g' prefix on numeric sha7 as second identifier; got: {version_str!r}"
        )

        # The prerelease sha identifier (now second) must be alphanumeric (no
        # leading-zero restriction under semver §9 for non-numeric identifiers).
        # We validate via the semver prerelease identifier rule directly: each
        # dot-separated identifier that is NOT purely numeric is unrestricted;
        # an alphanumeric identifier starting with 'g' satisfies this.
        # Note: the regex requires a letter-first identifier — that is always
        # true here because the guard prefixes 'g' by construction.
        import re
        parts = version_str.split("-dev.")
        assert len(parts) == 2, f"Unexpected version format: {version_str!r}"
        sha_part = parts[1].split(".")[1]  # second identifier (timestamp is first)
        assert re.match(r"^[a-zA-Z][a-zA-Z0-9]*$", sha_part), (
            f"Prerelease sha identifier is not an alphanumeric non-numeric identifier: "
            f"{sha_part!r} (full version: {version_str!r})"
        )

    def test_base_version_is_threaded_through_not_hardcoded(self, monkeypatch, tmp_path):
        """Anti-regression for #1607: the base version must come from the
        caller-supplied ``base_version`` argument, not a hardcoded "0.1.0".

        This is the test that would have caught Mira's bug: the release build
        stamped every rebuild as ``0.1.0-dev...`` regardless of the actual
        ``plugin.json`` version because the base version was hardcoded in the
        return statement instead of threaded through from the SSoT.
        """
        version_str = self.compute_build_version(str(tmp_path), "0.2.0")

        assert version_str.startswith("0.2.0-dev."), (
            f"Expected version to start with '0.2.0-dev.'; got: {version_str!r}"
        )
        assert not version_str.startswith("0.1.0"), (
            f"Version must not carry the old hardcoded '0.1.0' base; got: {version_str!r}"
        )


# ---------------------------------------------------------------------------
# 5. build_command — #794 branch guard
# ---------------------------------------------------------------------------


class TestBuildCommandBranchGuard:
    """build_command refuses non-dev branch without --allow-branch (#794)."""

    def _run_build_command(self, monkeypatch, tmp_path, extra_args=None):
        """Run build_command with repo_root pointing at tmp_path (has server.py)."""
        import subprocess as _sp
        from tools.engine.cli import build_command

        # Minimal repo layout — after Phase 4 #1093, server.py lives at
        # src/engram/server.py and tiers.json at src/build/packaging/tiers.json.
        (tmp_path / "src" / "engram").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "engram" / "server.py").write_text("")
        (tmp_path / "src" / "build" / "packaging").mkdir(parents=True, exist_ok=True)
        import json as _json
        (tmp_path / "src" / "build" / "packaging" / "tiers.json").write_text(_json.dumps({
            "schema_version": 1,
            "default_tier": "convenience",
            "depth_tiers": {"essential": 1, "convenience": 2, "dev": 3},
            "mechanisms": {},
            "excluded": [],
            "repo_only": [],
            "build_inputs": [],
        }))

        monkeypatch.chdir(tmp_path)
        args = list(extra_args) if extra_args else []
        return build_command(args)

    def test_non_dev_branch_without_allow_branch_fails(self, monkeypatch, tmp_path, capsys):
        """build_command returns 1 and emits #794 error on a non-dev branch."""
        import subprocess as _sp

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "branch"]:
                return _sp.CompletedProcess(cmd, 0, stdout="my-feature-branch\n", stderr="")
            return _sp.run.__wrapped__(cmd, **kwargs) if hasattr(_sp.run, "__wrapped__") else _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(_sp, "run", _fake_run)
        rc = self._run_build_command(monkeypatch, tmp_path)
        assert rc == 1
        captured = capsys.readouterr()
        assert "794" in captured.err
        assert "my-feature-branch" in captured.err

    def test_dev_branch_proceeds_past_guard(self, monkeypatch, tmp_path, capsys):
        """On dev, build_command passes the branch guard (may fail later — NOT on the guard)."""
        import subprocess as _sp

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "branch"]:
                return _sp.CompletedProcess(cmd, 0, stdout="dev\n", stderr="")
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(_sp, "run", _fake_run)
        self._run_build_command(monkeypatch, tmp_path)
        captured = capsys.readouterr()
        # The branch guard must NOT have fired — any failure is from later steps
        assert "794" not in captured.err

    def test_detached_head_blocked_without_allow_branch(self, monkeypatch, tmp_path, capsys):
        """Detached HEAD (empty branch name) is blocked just like a non-dev branch."""
        import subprocess as _sp

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "branch"]:
                return _sp.CompletedProcess(cmd, 0, stdout="\n", stderr="")
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(_sp, "run", _fake_run)
        rc = self._run_build_command(monkeypatch, tmp_path)
        assert rc == 1
        captured = capsys.readouterr()
        assert "794" in captured.err
        assert "detached HEAD" in captured.err

    def test_allow_branch_overrides_guard(self, monkeypatch, tmp_path, capsys):
        """--allow-branch skips the guard even on a non-dev branch."""
        import subprocess as _sp

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "branch"]:
                return _sp.CompletedProcess(cmd, 0, stdout="my-feature-branch\n", stderr="")
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(_sp, "run", _fake_run)
        rc = self._run_build_command(monkeypatch, tmp_path, extra_args=["--allow-branch"])
        # Guard skipped; 794 error must NOT appear
        captured = capsys.readouterr()
        assert "794" not in captured.err
