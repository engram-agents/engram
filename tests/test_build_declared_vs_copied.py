"""Tests for the declared-vs-copied reconciliation gate in build_plugin() (#1962).

History: at the time #1962 was filed, build_plugin() copied shipped manifest
entries through seven hardcoded, path-prefix-dispatched sections (root-level,
tools/, templates/, hooks/, skills/, agents/, output-styles/), each a fixed
set of prefixes with no catch-all — a manifest entry matching none of them
was silently never copied, no error, no warning, no skip line, even though
ship_set (derived from tiers.json) declared it correctly and the build
receipt (shipped_paths) looked plausible. engram-alpha#2547 (merged before
this PR) replaced three of those seven sections (root-level, tools/,
templates/) with ONE generic manifest-driven loop that copies ANY shipped
entry not claimed by the four remaining SPECIAL_COPY_PREFIXES sections
(hooks/, skills/, agents/, output-styles/) — and that generic loop now dies
loudly on its own (`_copy_manifest_entry`'s `os.path.exists` check) for any
path it can't find on disk, closing the "matches no section at all" gap
#1962 was filed for. What #2547 does NOT close: the four SPECIAL_COPY_PREFIXES
sections still enumerate their source directory with `os.scandir`/
`os.listdir` and only copy what that scan finds AND ship_set declares — a
manifest entry under one of those four prefixes whose real file/directory is
absent from disk is still silently skipped, no error. That residual is what
this PR's reconciliation gate (`missing_from_copy = ship_set -
set(copied_paths)`, checked once, after every section has run) exists to
catch regardless of WHICH section's mechanism produced the gap.

This file covers three properties:

1. TestReconciliationFiresOnGap — the positive-control regression test: a
   manifest entry under one of the four SPECIAL_COPY_PREFIXES sections
   (e.g. "skills/claude/totally-fake-nonexistent-skill-1962") whose real
   directory doesn't exist on disk is silently skipped by that section's own
   scandir-based enumeration (never reaching `_copy_manifest_entry`'s
   existence check, which only the generic loop goes through) — this test
   confirms build_plugin() still dies loudly, naming the missing path, via
   the reconciliation gate. A path under no known prefix at all (e.g.
   "weird_subdir/thing.py") is now caught earlier and independently, by the
   generic loop's own existence check (see engram-alpha#2547) — that shape no
   longer exercises this gate specifically, so it isn't used here.

2. TestReconciliationPassesOnFullCoverage — the real, unmodified tiers.json
   must NOT trip the new gate at any real tier — i.e. every declared entry
   really does land in one of the five copy mechanisms (the generic loop +
   the four special sections). Regression guard against the check firing
   false-positives.

3. TestLegitimateExtrasDoNotTriggerFalsePositive — several build-generated
   meta files (plugin.json, hooks/hooks.json, .mcp.json / packaging/mcp.json,
   platform.json, .codex-plugin/plugin.json) are appended to copied_paths but
   are deliberately NOT manifest-tracked (absent from ship_set) — confirmed by
   direct inspection of tiers.json's "mechanisms" list.  The reconciliation
   check must be one-directional (ship_set - set(copied_paths)) so these
   legitimate extras never trigger a false failure.  Reuses the same real
   build as class 2 to confirm this cheaply, without a second full build.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. Positive control — an unrecognized-subdirectory entry must die loudly
# ---------------------------------------------------------------------------


class TestReconciliationFiresOnGap:
    """A manifest entry that no copy mechanism actually reaches must fail the build.

    This mirrors Borges's real-build falsifier from the #2537 discussion
    (referenced in #1962): a manifest entry under a subdirectory none of the
    (then seven, now five — see module docstring) copy mechanisms recognize
    was declared correctly, the build exited 0, and the file was simply
    absent from the bundle. This test proves the reconciliation gate still
    catches that shape at the one prefix family (SPECIAL_COPY_PREFIXES) whose
    scandir-based enumeration can still silently skip a declared-but-absent
    entry post-#2547 (the generic loop's own existence check now closes the
    other gap independently — see test_generic_loop_gap_dies_via_its_own_check
    below, which documents that composition rather than re-testing this gate).
    """

    def test_unrecognized_subdirectory_entry_dies_loudly(self, tmp_path):
        """A path under a SPECIAL_COPY_PREFIXES section (here skills/claude/)
        whose real directory doesn't exist on disk is silently skipped by
        that section's own os.scandir enumeration — never reaching
        _copy_manifest_entry's existence check, which only the generic loop
        goes through — so only the reconciliation gate this PR adds can
        catch it. Must cause build_plugin() to raise, naming the missing
        path.
        """
        from tools.engine.build import build_plugin

        manifest = _load_manifest()
        # Deep-copy so we never mutate the real tiers.json in memory in a way
        # that could leak into other tests via aliasing.
        augmented = copy.deepcopy(manifest)
        planted_path = "skills/claude/totally-fake-nonexistent-skill-1962"
        augmented["mechanisms"].append(
            {
                "path": planted_path,
                "tier": "essential",
                "reason": "test-planted entry under a real prefix section with no matching dir on disk (#1962 regression test)",
            }
        )

        bundle_dir = str(tmp_path / "gap_bundle")

        with pytest.raises(RuntimeError) as excinfo:
            build_plugin(
                repo_root=REPO_ROOT,
                plugin_root=bundle_dir,
                tier="essential",
                manifest=augmented,
            )

        assert planted_path in str(excinfo.value), (
            f"build_plugin() raised, but the error does not name the planted "
            f"missing path {planted_path!r}: {excinfo.value}"
        )

    def test_unrecognized_subdirectory_entry_message_names_mechanism(self, tmp_path):
        """The RuntimeError message explains WHY (dispatch-by-prefix, needs a section)."""
        from tools.engine.build import build_plugin

        manifest = _load_manifest()
        augmented = copy.deepcopy(manifest)
        augmented["mechanisms"].append(
            {
                "path": "agents/claude/totally-fake-nonexistent-agent-1962.md",
                "tier": "essential",
                "reason": "test-planted entry under a real prefix section with no matching file on disk (#1962 regression test)",
            }
        )

        bundle_dir = str(tmp_path / "gap_bundle_2")

        with pytest.raises(RuntimeError) as excinfo:
            build_plugin(
                repo_root=REPO_ROOT,
                plugin_root=bundle_dir,
                tier="essential",
                manifest=augmented,
            )

        msg = str(excinfo.value)
        assert "no copy section handled" in msg, (
            f"Error message does not explain the mechanism (dispatch-by-prefix): {msg}"
        )

    def test_generic_loop_gap_dies_via_its_own_check(self, tmp_path):
        """Documents the OTHER failure mode, so a future reader doesn't
        mistake this file's silence on it for a regression: a path under NO
        known prefix at all (not one of the four SPECIAL_COPY_PREFIXES) now
        goes through the generic manifest-driven loop (engram-alpha#2547),
        which dies loudly on its own via _copy_manifest_entry's
        os.path.exists check — a DIFFERENT message than this PR's
        reconciliation gate produces, and it fires earlier, before the
        reconciliation gate is ever reached. This is not this PR's own
        mechanism (it belongs to #2547), so this test only confirms the
        composition still holds — that a gap here is caught SOMEWHERE, loud,
        by exactly one of the two mechanisms, not silently dropped."""
        from tools.engine.build import build_plugin

        manifest = _load_manifest()
        augmented = copy.deepcopy(manifest)
        planted_path = "weird_subdir/thing.py"
        augmented["mechanisms"].append(
            {
                "path": planted_path,
                "tier": "essential",
                "reason": "test-planted entry matching no prefix at all (generic-loop existence-check regression test)",
            }
        )

        bundle_dir = str(tmp_path / "gap_bundle_generic")

        with pytest.raises(RuntimeError) as excinfo:
            build_plugin(
                repo_root=REPO_ROOT,
                plugin_root=bundle_dir,
                tier="essential",
                manifest=augmented,
            )

        msg = str(excinfo.value)
        assert planted_path in msg, (
            f"build_plugin() raised, but the error does not name the planted "
            f"missing path {planted_path!r}: {msg}"
        )
        assert "does not exist at" in msg, (
            f"Expected the generic loop's own existence check to fire (a "
            f"different message than this PR's reconciliation gate), got: {msg}"
        )


# ---------------------------------------------------------------------------
# 2. Negative control — a normal, fully-covered build must NOT false-positive
# ---------------------------------------------------------------------------


class TestReconciliationPassesOnFullCoverage:
    """The real tiers.json at the default (--tier convenience) tier must not
    trip the gate.

    HISTORY (was a live scope note, now resolved — kept as provenance): the
    spec's own suggested scenario for this test was "the maximal shipping
    set" (--tier dev --multi-agent). At the time this PR was first built,
    --tier dev genuinely had TEN declared-but-never-copied mechanism entries
    (.githooks/pre-push, packaging/release-gates.json,
    packaging/test-map.json, and seven src/build/instruction_manifestation/*
    entries) — a second, real, pre-existing instance of the #1962 defect
    class, independent of this PR's own change, and explicitly out of this
    PR's scope to fix. This test targeted --tier convenience instead, to
    avoid baking in an assertion that would fail against that known,
    separate bug. engram-alpha#2547 (merged before this PR landed) closed
    that gap as a side effect of its own, unrelated fix (replacing the
    tools/-prefix-only copy section with a generic manifest-driven loop) —
    confirmed empirically post-rebase (0 missing entries at --tier dev
    --multi-agent, run directly against the real tiers.json). See
    TestReconciliationPassesOnFullCoverageAtDevTier below for the now-valid
    maximal-scope test the spec originally asked for.
    """

    @pytest.fixture(scope="class")
    def convenience_ma_build(self, tmp_path_factory):
        """Build the real manifest at --tier convenience --multi-agent once for this class."""
        from tools.engine.build import build_plugin
        from tools.engine.manifest import load_manifest, resolve_tier

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "convenience")
        bundle_dir = str(tmp_path_factory.mktemp("convenience_ma_reconciliation"))

        copied_paths = build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            manifest=manifest,
        )
        return manifest, tier, bundle_dir, copied_paths

    def test_convenience_multi_agent_build_does_not_raise(self, convenience_ma_build):
        """build_plugin() at --tier convenience --multi-agent completes (fixture didn't raise)."""
        manifest, tier, bundle_dir, copied_paths = convenience_ma_build
        assert os.path.isfile(os.path.join(bundle_dir, "server.py")), (
            "convenience+MA build did not produce server.py — build did not actually run"
        )

    def test_every_declared_mechanism_was_copied(self, convenience_ma_build):
        """Directly re-derive ship_set and confirm it is a subset of copied_paths.

        This duplicates the reconciliation check's own logic against the real
        manifest, independent of whether build_plugin() itself raised — a
        second, independent confirmation that the one-directional comparison
        holds for the real, current tiers.json at this tier.
        """
        from tools.engine.build import select_shippables_with_identity, _build_ship_set

        manifest, tier, bundle_dir, copied_paths = convenience_ma_build
        shipped_entries = select_shippables_with_identity(manifest, tier, "self")
        ship_set = _build_ship_set(shipped_entries)

        missing = ship_set - set(copied_paths)
        assert not missing, (
            f"Real tiers.json declares path(s) that landed in none of the five "
            f"copy mechanisms at --tier convenience --multi-agent: {sorted(missing)}"
        )


class TestReconciliationPassesOnFullCoverageAtDevTier:
    """The real tiers.json at --tier dev --multi-agent (the maximal shipping
    set — the spec's original suggested scenario) must not trip the gate.

    Deferred from TestReconciliationPassesOnFullCoverage above until
    engram-alpha#2547 closed the ten-entry dev-tier gap that made this
    assertion fail against a known, separate, out-of-scope bug (see that
    class's docstring for the full history). #2547 is now merged to dev, so
    this is the real, currently-passing maximal-scope test the spec asked
    for from the start."""

    @pytest.fixture(scope="class")
    def dev_ma_build(self, tmp_path_factory):
        """Build the real manifest at --tier dev --multi-agent once for this class."""
        from tools.engine.build import build_plugin
        from tools.engine.manifest import load_manifest, resolve_tier

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "dev")
        bundle_dir = str(tmp_path_factory.mktemp("dev_ma_reconciliation"))

        copied_paths = build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            manifest=manifest,
        )
        return manifest, tier, bundle_dir, copied_paths

    def test_dev_multi_agent_build_does_not_raise(self, dev_ma_build):
        """build_plugin() at --tier dev --multi-agent completes (fixture didn't raise)."""
        manifest, tier, bundle_dir, copied_paths = dev_ma_build
        assert os.path.isfile(os.path.join(bundle_dir, "server.py")), (
            "dev+MA build did not produce server.py — build did not actually run"
        )

    def test_every_declared_mechanism_was_copied_at_dev_tier(self, dev_ma_build):
        """Directly re-derive ship_set and confirm it is a subset of copied_paths,
        at the maximal (dev, multi-agent) shipping set — the scenario most
        likely to surface a declared-but-unreachable entry, since it ships
        the largest set of mechanisms."""
        from tools.engine.build import select_shippables_with_identity, _build_ship_set

        manifest, tier, bundle_dir, copied_paths = dev_ma_build
        shipped_entries = select_shippables_with_identity(manifest, tier, "self")
        ship_set = _build_ship_set(shipped_entries)

        missing = ship_set - set(copied_paths)
        assert not missing, (
            f"Real tiers.json declares path(s) that landed in none of the five "
            f"copy mechanisms at --tier dev --multi-agent: {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# 3. Legitimate extras (generated, unmanifested) must not false-positive
# ---------------------------------------------------------------------------


class TestLegitimateExtrasDoNotTriggerFalsePositive:
    """Generated meta files ride in copied_paths without being in ship_set.

    plugin.json, hooks/hooks.json, the .mcp.json / packaging/mcp.json path,
    platform.json, and (codex target) .codex-plugin/plugin.json are written
    directly by build_plugin() and appended to copied_paths, but are
    deliberately NOT manifest-tracked mechanisms (see tiers.json's separate
    "build_inputs" list, which select_shippables() never reads).  The
    reconciliation check must be one-directional — extras in copied_paths that
    aren't in ship_set are fine — never strict set-equality.
    """

    # The five extra paths named in #1962 as build-generated, never
    # manifest-tracked as a *mechanism* (shippable) entry.
    _EXTRA_PATHS = [
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "packaging/mcp.json",
        "platform.json",
        "hooks/hooks.json",
    ]

    def test_extra_paths_absent_from_mechanisms(self):
        """None of the five generated-extra paths is a tracked mechanism (ship_set member).

        Confirms the direction of the reconciliation check: these paths are
        legitimately absent from ship_set (they may or may not appear
        elsewhere in tiers.json, e.g. under "build_inputs", but that list is
        never read by select_shippables()).
        """
        manifest = _load_manifest()
        mechanism_paths = {e["path"] for e in manifest["mechanisms"]}
        present = [p for p in self._EXTRA_PATHS if p in mechanism_paths]
        assert not present, (
            f"Expected all of {self._EXTRA_PATHS} to be absent from tiers.json's "
            f"mechanisms[] (ship_set), but found: {present}. If this fails, the "
            f"one-directional reconciliation check's premise no longer holds for "
            f"these paths and must be revisited."
        )

    def test_claude_code_build_copies_extras_but_they_are_not_in_ship_set(
        self, tmp_path
    ):
        """A real claude-code build's copied_paths carries the extras; ship_set does not.

        Directly exercises the scenario the one-directional check exists to
        tolerate: copied_paths ⊋ ship_set (extras present), and the build
        must still succeed (no RuntimeError) because the check only looks for
        ship_set - copied_paths, never the reverse.
        """
        from tools.engine.build import (
            build_plugin,
            select_shippables_with_identity,
            _build_ship_set,
        )
        from tools.engine.manifest import load_manifest, resolve_tier

        manifest = load_manifest(MANIFEST_PATH)
        tier = resolve_tier(manifest, "essential")
        bundle_dir = str(tmp_path / "extras_bundle")

        copied_paths = build_plugin(
            repo_root=REPO_ROOT,
            plugin_root=bundle_dir,
            tier=tier,
            manifest=manifest,
            target="claude-code",
        )

        shipped_entries = select_shippables_with_identity(manifest, tier, "self")
        ship_set = _build_ship_set(shipped_entries)

        # At least one of the known extras must actually show up in
        # copied_paths (sanity: the scenario we're testing actually occurs).
        extras_present_in_copied = [p for p in self._EXTRA_PATHS if p in copied_paths]
        assert extras_present_in_copied, (
            "None of the expected generated-extra paths appeared in copied_paths "
            "for a claude-code build — the fixture no longer exercises the "
            "legitimate-extras scenario; update _EXTRA_PATHS or investigate."
        )

        # None of those extras is in ship_set (already confirmed above, but a
        # combined check that build_plugin() didn't raise IS the assertion
        # that the one-directional comparison tolerates this).
        extras_in_ship_set = [p for p in extras_present_in_copied if p in ship_set]
        assert not extras_in_ship_set, (
            f"Extras {extras_in_ship_set} unexpectedly present in ship_set — "
            "the one-directional check's premise for these paths no longer holds."
        )
