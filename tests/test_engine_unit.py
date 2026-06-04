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
MANIFEST_PATH = os.path.join(REPO_ROOT, "packaging", "tiers.json")


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
        """MA-gated entries must not appear when multi_agent=False."""
        result = self.select_shippables(self.manifest, "convenience", multi_agent=False)
        paths = {e["path"] for e in result}
        # Known MA-gated entries
        assert "hooks/claude/engram-baton-prompt-hook.py" not in paths, (
            "engram-baton-prompt-hook.py must not ship without --multi-agent"
        )
        assert "hooks/claude/engram-inter-agent-prompt-hook.py" not in paths, (
            "engram-inter-agent-prompt-hook.py must not ship without --multi-agent"
        )
        assert "skills/claude/engram-letter" not in paths, (
            "engram-letter skill must not ship without --multi-agent"
        )

    def test_ma_gated_entry_included_with_flag(self):
        """MA-gated entries at the right tier must appear when multi_agent=True."""
        result = self.select_shippables(self.manifest, "convenience", multi_agent=True)
        paths = {e["path"] for e in result}
        assert "hooks/claude/engram-baton-prompt-hook.py" in paths
        assert "hooks/claude/engram-inter-agent-prompt-hook.py" in paths
        assert "skills/claude/engram-letter" in paths

    def test_ma_gated_blocked_by_tier_even_with_flag(self):
        """An MA-gated convenience entry must not ship at essential tier, even with MA flag."""
        result = self.select_shippables(self.manifest, "essential", multi_agent=True)
        paths = {e["path"] for e in result}
        assert "hooks/claude/engram-baton-prompt-hook.py" not in paths, (
            "MA-gated convenience hook must not ship at essential tier even with --multi-agent"
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
