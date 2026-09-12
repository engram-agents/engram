"""Tests for the PR-2 tier build-filter: predicate logic, gap-closure, import coverage.

These tests verify:
1. Predicate unit tests — ships() predicate returns the right boolean for
   representative paths across (tier × multi_agent) combos.
2. Gap-closure test — --tier dev --multi-agent produces a set that INCLUDES
   the previously-dropped surfaces (engram-baton-prompt-hook, forum.py,
   engram-letter). This directly tests the migration-gap fix (ob_2162/ob_2163).
3. Import-coverage invariant — every `from tools.X` / `import tools.X` in
   server.py is tagged essential in the manifest (so it ships at every tier).
4. Cumulative test — convenience set ⊇ essential set.
"""

from __future__ import annotations

import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")
SERVER_PY = os.path.join(REPO_ROOT, "src", "engram", "server.py")

TIER_RANKS = {"essential": 0, "convenience": 1, "dev": 2}


# ---------------------------------------------------------------------------
# helpers — the predicate as a Python function, mirrors build-plugin.sh logic
# ---------------------------------------------------------------------------


def load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def ships(
    manifest: dict,
    path: str,
    tier: str,
    multi_agent: bool = False,
) -> bool:
    """Return True iff the mechanism at `path` ships under (tier, multi_agent).

    Predicate (spec §2):
      ships(m) := rank(m.tier) <= rank(chosen_tier)
                  AND (m.multi_agent != true OR multi_agent_chosen)
    where rank(essential)=0, rank(convenience)=1, rank(dev)=2.

    Returns False if path is not in the manifest at all.
    """
    chosen_rank = TIER_RANKS[tier]
    for entry in manifest["mechanisms"]:
        if entry["path"] == path:
            entry_rank = TIER_RANKS.get(entry.get("tier", "dev"), 2)
            if entry_rank > chosen_rank:
                return False
            if entry.get("multi_agent") is True and not multi_agent:
                return False
            return True
    return False  # not in manifest


def shipping_set(
    manifest: dict,
    tier: str,
    multi_agent: bool = False,
) -> set[str]:
    """Return the full set of paths that ship under (tier, multi_agent)."""
    chosen_rank = TIER_RANKS[tier]
    result: set[str] = set()
    for entry in manifest["mechanisms"]:
        entry_rank = TIER_RANKS.get(entry.get("tier", "dev"), 2)
        if entry_rank > chosen_rank:
            continue
        if entry.get("multi_agent") is True and not multi_agent:
            continue
        result.add(entry["path"])
    return result


# ---------------------------------------------------------------------------
# 1. Predicate unit tests — (tier × multi_agent) combos
# ---------------------------------------------------------------------------


class TestPredicate:
    """Unit tests for the ships() predicate across representative paths."""

    # An essential hook — ships at every tier, MA or not
    ESSENTIAL_HOOK = "hooks/claude/engram-surface-hook.py"

    # A convenience skill — ships at convenience+, not at essential
    CONVENIENCE_SKILL = "skills/claude/engram-loop"

    # A dev tool — ships only at dev tier
    DEV_TOOL = "tools/scan-leaks.py"

    # An MA-gated entry (convenience+MA) — needs --multi-agent.
    # NOTE (#2857 step 1): no hook or skill is MA-gated any longer — every
    # forum-backed hook/skill (baton/inter-agent hooks, engram-letter/
    # engram-baton/engram-collaborating-loop skills) was un-gated, since
    # baton/letter/forum are symmetric for any agent once a forum is
    # configured. The two remaining MA-gated fixtures below are plain
    # tools/scripts, not a hook or skill — see tiers.json's 6-item "leave
    # gated" list (session.py / _channel.py / _session_context.py /
    # agents.json.example / the two collab-*-monitor.sh scripts), none of
    # which are forum-backed.
    MA_GATED_ENTRY_1 = "tools/session.py"

    # A second MA-gated entry (convenience+MA) — needs --multi-agent.
    MA_GATED_ENTRY_2 = "tools/collab-baton-monitor.sh"

    # A convenience tool with no MA flag (forum.py) — ships at convenience, no MA needed
    FORUM_TOOL = "tools/forum.py"

    def setup_method(self):
        self.manifest = load_manifest()

    # --- essential hook ships at all tiers / both MA values ---

    def test_essential_hook_ships_at_essential(self):
        assert ships(self.manifest, self.ESSENTIAL_HOOK, "essential", multi_agent=False)

    def test_essential_hook_ships_at_essential_ma(self):
        assert ships(self.manifest, self.ESSENTIAL_HOOK, "essential", multi_agent=True)

    def test_essential_hook_ships_at_convenience(self):
        assert ships(self.manifest, self.ESSENTIAL_HOOK, "convenience", multi_agent=False)

    def test_essential_hook_ships_at_dev(self):
        assert ships(self.manifest, self.ESSENTIAL_HOOK, "dev", multi_agent=False)

    # --- convenience skill blocked at essential tier ---

    def test_convenience_skill_blocked_at_essential(self):
        assert not ships(self.manifest, self.CONVENIENCE_SKILL, "essential", multi_agent=False)

    def test_convenience_skill_ships_at_convenience(self):
        assert ships(self.manifest, self.CONVENIENCE_SKILL, "convenience", multi_agent=False)

    def test_convenience_skill_ships_at_dev(self):
        assert ships(self.manifest, self.CONVENIENCE_SKILL, "dev", multi_agent=False)

    # --- dev tool blocked below dev tier ---

    def test_dev_tool_blocked_at_essential(self):
        assert not ships(self.manifest, self.DEV_TOOL, "essential", multi_agent=False)

    def test_dev_tool_blocked_at_convenience(self):
        assert not ships(self.manifest, self.DEV_TOOL, "convenience", multi_agent=False)

    def test_dev_tool_ships_at_dev(self):
        assert ships(self.manifest, self.DEV_TOOL, "dev", multi_agent=False)

    def test_dev_tool_ships_at_dev_ma(self):
        assert ships(self.manifest, self.DEV_TOOL, "dev", multi_agent=True)

    # --- MA-gated entry 1: needs right tier AND --multi-agent ---

    def test_ma_gated_entry_1_blocked_without_ma_flag(self):
        assert not ships(self.manifest, self.MA_GATED_ENTRY_1, "convenience", multi_agent=False)

    def test_ma_gated_entry_1_blocked_without_ma_flag_dev(self):
        assert not ships(self.manifest, self.MA_GATED_ENTRY_1, "dev", multi_agent=False)

    def test_ma_gated_entry_1_blocked_with_ma_but_too_low_tier(self):
        assert not ships(self.manifest, self.MA_GATED_ENTRY_1, "essential", multi_agent=True)

    def test_ma_gated_entry_1_ships_at_convenience_with_ma(self):
        assert ships(self.manifest, self.MA_GATED_ENTRY_1, "convenience", multi_agent=True)

    def test_ma_gated_entry_1_ships_at_dev_with_ma(self):
        assert ships(self.manifest, self.MA_GATED_ENTRY_1, "dev", multi_agent=True)

    # --- MA-gated entry 2: same pattern ---

    def test_ma_gated_entry_2_blocked_without_ma_flag(self):
        assert not ships(self.manifest, self.MA_GATED_ENTRY_2, "convenience", multi_agent=False)

    def test_ma_gated_entry_2_ships_at_convenience_with_ma(self):
        assert ships(self.manifest, self.MA_GATED_ENTRY_2, "convenience", multi_agent=True)

    def test_ma_gated_entry_2_ships_at_dev_with_ma(self):
        assert ships(self.manifest, self.MA_GATED_ENTRY_2, "dev", multi_agent=True)

    # --- forum.py: convenience, no MA flag → ships at convenience without --multi-agent ---

    def test_forum_ships_at_convenience_without_ma(self):
        assert ships(self.manifest, self.FORUM_TOOL, "convenience", multi_agent=False)

    def test_forum_ships_at_dev_without_ma(self):
        assert ships(self.manifest, self.FORUM_TOOL, "dev", multi_agent=False)

    def test_forum_blocked_at_essential(self):
        assert not ships(self.manifest, self.FORUM_TOOL, "essential", multi_agent=False)

    # --- path not in manifest → always False ---

    def test_unknown_path_never_ships(self):
        assert not ships(self.manifest, "tools/nonexistent_tool.py", "dev", multi_agent=True)


# ---------------------------------------------------------------------------
# 2. Gap-closure test — the previously-dropped surfaces must be PRESENT at
#    --tier dev --multi-agent (ob_2162 / ob_2163 migration gap fix)
# ---------------------------------------------------------------------------


class TestGapClosure:
    """Proof that --tier dev --multi-agent ships what was previously dropped.

    Prior to PR-2, build-plugin.sh's HOOKS_EXCLUDE array dropped:
      - engram-baton-prompt-hook.py  (labeled "persona-specific multi-agent")
      - engram-time-bar-hook.py      (now ships at convenience, no multi_agent
                                      flag; not asserted here because it is
                                      transitively covered by
                                      test_dev_superset_of_convenience)
    And the tools/forum.py and skills/claude/engram-letter were not shipped
    in the plugin path because they weren't in RUNTIME_TOOLS and skills were
    only filtered by a manual exclusion of engram-letter in install.sh.

    After PR-2, the manifest is the sole authority. This test asserts that
    each of the gap-closure surfaces IS in the --tier dev --multi-agent set.
    """

    # Previously dropped by HOOKS_EXCLUDE (baton workflow, multi-agent)
    BATON_HOOK = "hooks/claude/engram-baton-prompt-hook.py"

    # Previously dropped by HOOKS_EXCLUDE (inter-agent prompt hook, multi-agent)
    INTER_AGENT_HOOK = "hooks/claude/engram-inter-agent-prompt-hook.py"

    # tools/forum.py was never in RUNTIME_TOOLS (plugin path missed it)
    FORUM_TOOL = "tools/forum.py"

    # skills/claude/engram-letter was manually excluded in install.sh Step 4
    LETTER_SKILL = "skills/claude/engram-letter"

    def setup_method(self):
        self.manifest = load_manifest()
        self.dev_ma_set = shipping_set(self.manifest, tier="dev", multi_agent=True)

    def test_baton_hook_present_at_dev_ma(self):
        """engram-baton-prompt-hook.py ships at --tier dev --multi-agent."""
        assert self.BATON_HOOK in self.dev_ma_set, (
            f"{self.BATON_HOOK!r} is NOT in the --tier dev --multi-agent shipping set. "
            "This is the primary gap-closure assertion (ob_2162/ob_2163): "
            "the baton prompt hook was previously dropped by HOOKS_EXCLUDE."
        )

    def test_inter_agent_hook_present_at_dev_ma(self):
        """engram-inter-agent-prompt-hook.py ships at --tier dev --multi-agent."""
        assert self.INTER_AGENT_HOOK in self.dev_ma_set, (
            f"{self.INTER_AGENT_HOOK!r} is NOT in the --tier dev --multi-agent shipping set."
        )

    def test_forum_tool_present_at_dev_ma(self):
        """tools/forum.py ships at --tier dev --multi-agent (and at convenience)."""
        assert self.FORUM_TOOL in self.dev_ma_set, (
            f"{self.FORUM_TOOL!r} is NOT in the --tier dev --multi-agent shipping set. "
            "tools/forum.py was previously absent from the plugin path (not in RUNTIME_TOOLS)."
        )

    def test_letter_skill_present_at_dev_ma(self):
        """skills/claude/engram-letter ships at --tier dev --multi-agent."""
        assert self.LETTER_SKILL in self.dev_ma_set, (
            f"{self.LETTER_SKILL!r} is NOT in the --tier dev --multi-agent shipping set. "
            "engram-letter skill was previously excluded manually from install.sh Step 4 "
            "even when multi-agent infra was present."
        )

    def test_gap_closure_items_present_at_convenience_without_ma(self):
        """Forum-backed gap-closure surfaces now ship in the DEFAULT build too.

        #2857 step 1 dropped the multi_agent gate from every genuinely
        forum-backed mechanism, including the baton hook and the letter
        skill this class exists to guard — baton/letter/forum are symmetric
        for any agent once a forum is configured, so `--multi-agent` is no
        longer required for them. This inverts the pre-#2857 assertion below
        (baton hook / letter skill used to be absent from the convenience,
        no-MA build; they are now present, matching forum.py's un-gated
        shape all along).
        """
        default_set = shipping_set(self.manifest, tier="convenience", multi_agent=False)
        assert self.BATON_HOOK in default_set, (
            f"{self.BATON_HOOK!r} should ship at --tier convenience without --multi-agent "
            "now that it is forum-backed and un-gated (#2857 step 1)"
        )
        assert self.INTER_AGENT_HOOK in default_set, (
            f"{self.INTER_AGENT_HOOK!r} should ship at --tier convenience without --multi-agent "
            "now that it is forum-backed and un-gated (#2857 step 1)"
        )
        assert self.LETTER_SKILL in default_set, (
            f"{self.LETTER_SKILL!r} should ship at --tier convenience without --multi-agent "
            "now that it is forum-backed and un-gated (#2857 step 1)"
        )
        # forum.py never had an MA flag → DOES ship at convenience without --multi-agent
        assert self.FORUM_TOOL in default_set, (
            f"{self.FORUM_TOOL!r} should ship at --tier convenience (no MA flag on this entry)"
        )


# ---------------------------------------------------------------------------
# 3. Import-coverage invariant — every tools.X import in server.py is essential
# ---------------------------------------------------------------------------


class TestImportCoverageInvariant:
    """Every `from tools.X` / `import tools.X` in server.py must be tagged essential.

    Essential tools ship at every tier. If a runtime import is tagged convenience
    or dev, it would be absent from an essential-tier install and cause an
    ImportError at server startup.
    """

    def _find_tools_imports(self) -> list[str]:
        """Return module names from `from tools.X import ...` / `import tools.X` in server.py."""
        with open(SERVER_PY, encoding="utf-8") as f:
            source = f.read()

        modules: list[str] = []
        for line in source.splitlines():
            line = line.strip()
            # Match: from tools.X import ...
            m = re.match(r"from tools\.(\w+)\s+import", line)
            if m:
                modules.append(m.group(1))
                continue
            # Match: import tools.X
            m = re.match(r"import tools\.(\w+)", line)
            if m:
                modules.append(m.group(1))
        return modules

    def test_all_server_tool_imports_are_essential(self):
        """Every tools.X imported by server.py must be tagged essential in the manifest."""
        manifest = load_manifest()
        manifest_by_path = {e["path"]: e for e in manifest["mechanisms"]}

        imported_modules = self._find_tools_imports()
        assert imported_modules, (
            "No tools.X imports found in server.py — "
            "verify the regex or update this test if server.py changed."
        )

        failures: list[str] = []
        for module in imported_modules:
            tools_path = f"tools/{module}.py"
            entry = manifest_by_path.get(tools_path)
            if entry is None:
                failures.append(
                    f"  tools/{module}.py (imported as tools.{module}) is NOT in the manifest — "
                    "add it with tier=essential"
                )
            elif entry.get("tier") != "essential":
                failures.append(
                    f"  tools/{module}.py is tagged tier={entry['tier']!r} but server.py "
                    "imports it at runtime — must be tagged essential so it ships at every tier"
                )

        assert not failures, (
            "Import-coverage invariant violated — these tools.X imports in server.py "
            "are not tagged essential:\n" + "\n".join(failures)
        )

    def test_server_tool_imports_actually_exist(self):
        """Cross-check: every tools.X imported by server.py must exist in the repo."""
        imported_modules = self._find_tools_imports()
        missing: list[str] = []
        for module in imported_modules:
            path = os.path.join(REPO_ROOT, "tools", f"{module}.py")
            if not os.path.exists(path):
                missing.append(f"tools/{module}.py")

        assert not missing, (
            "server.py imports tools.X but the corresponding file does not exist:\n"
            + "\n".join(f"  {p}" for p in missing)
        )


# ---------------------------------------------------------------------------
# 4. Cumulative test — convenience ⊇ essential, dev ⊇ convenience
# ---------------------------------------------------------------------------


class TestCumulative:
    """The shipping sets must be cumulative: essential ⊆ convenience ⊆ dev."""

    def setup_method(self):
        self.manifest = load_manifest()

    def test_convenience_superset_of_essential(self):
        """--tier convenience ships everything --tier essential ships (non-MA)."""
        ess = shipping_set(self.manifest, tier="essential", multi_agent=False)
        conv = shipping_set(self.manifest, tier="convenience", multi_agent=False)
        missing = ess - conv
        assert not missing, (
            "convenience set is NOT a superset of essential set — "
            "these essential paths are missing from convenience:\n"
            + "\n".join(f"  {p}" for p in sorted(missing))
        )

    def test_dev_superset_of_convenience(self):
        """--tier dev ships everything --tier convenience ships (non-MA)."""
        conv = shipping_set(self.manifest, tier="convenience", multi_agent=False)
        dev = shipping_set(self.manifest, tier="dev", multi_agent=False)
        missing = conv - dev
        assert not missing, (
            "dev set is NOT a superset of convenience set — "
            "these convenience paths are missing from dev:\n"
            + "\n".join(f"  {p}" for p in sorted(missing))
        )

    def test_dev_superset_of_essential(self):
        """--tier dev ships everything --tier essential ships (non-MA)."""
        ess = shipping_set(self.manifest, tier="essential", multi_agent=False)
        dev = shipping_set(self.manifest, tier="dev", multi_agent=False)
        missing = ess - dev
        assert not missing, (
            "dev set is NOT a superset of essential set:\n"
            + "\n".join(f"  {p}" for p in sorted(missing))
        )

    def test_ma_convenience_superset_of_nonma_convenience(self):
        """--tier convenience --multi-agent ships everything --tier convenience ships."""
        conv = shipping_set(self.manifest, tier="convenience", multi_agent=False)
        conv_ma = shipping_set(self.manifest, tier="convenience", multi_agent=True)
        missing = conv - conv_ma
        assert not missing, (
            "convenience+MA set is NOT a superset of convenience set — "
            "these paths are in non-MA convenience but not MA convenience:\n"
            + "\n".join(f"  {p}" for p in sorted(missing))
        )

    def test_dev_ma_superset_of_dev(self):
        """--tier dev --multi-agent ships everything --tier dev ships."""
        dev = shipping_set(self.manifest, tier="dev", multi_agent=False)
        dev_ma = shipping_set(self.manifest, tier="dev", multi_agent=True)
        missing = dev - dev_ma
        assert not missing, (
            "dev+MA set is NOT a superset of dev set:\n"
            + "\n".join(f"  {p}" for p in sorted(missing))
        )
