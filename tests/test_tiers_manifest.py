"""Tag-once gate: every path in the build-plugin.sh shippable universe must appear
in packaging/tiers.json (either as a tiered mechanism or in the explicit excluded list),
with no dangling manifest entries.

Enumeration unit choices (load-bearing design decision)
=======================================================
build-plugin.sh uses DIFFERENT copy granularities for different surfaces:

  * Top-level files (server.py, bootstrap.py, SKILL.md, launch-engram-server.sh,
    engram_*.py): copied per-file → manifest unit = per-file.

  * tools/*: manifest-driven per-item copy — each tools/ entry in the manifest
    is a file or sub-directory with its own tier tag.

  * skills/claude: per-skill-dir granularity (one entry per named sub-directory).
    Manifest unit = per-skill-dir, e.g. ``skills/claude/engram-sleep``.

  * hooks/claude: per-file granularity, filtered entirely through the manifest
    (HOOKS_EXCLUDE array deleted; manifest tier tags are the sole authority).
    Manifest unit = per-hook-file, e.g. ``hooks/claude/engram-surface-hook.py``.

  * agents/claude: per-file granularity (*.md excluding README.md).
    Manifest unit = per-agent-file, e.g. ``agents/claude/engram-dream-fairy.md``.

  * output-styles/claude: per-file granularity.
    Manifest unit = per-file, e.g. ``output-styles/claude/proactive-with-carveouts.md``.

  * templates: per-top-level-item (files AND sub-directories).
    Manifest unit = per-top-level-item inside templates/, e.g.
    ``templates/CLAUDE.md.template`` and ``templates/heartbeat-queue``.

The test enumerates each surface at the SAME granularity the manifest uses, so
the cross-check is 1:1 with no false mismatches.

Scope boundary
==============
The CI test only checks paths that build-plugin.sh ships (the plugin install
path). The manifest intentionally covers MORE than this — scatter-install paths
(tools/baton.py, viz_server.py, etc.), the Gemini integration, and operator
tools also have manifest entries. Those extra entries are valid
(the dangling-entry check only fires if a manifest path does NOT exist anywhere
in the repo, not if it falls outside the plugin-copy scope). This gives the
manifest broader coverage while keeping the CI gate mechanical and offline.

Completeness gate (filesystem → manifest)
=========================================
``test_every_shippable_path_is_tagged`` enumerates candidate files from the
FILESYSTEM (the actual repo dirs that build-plugin.sh draws from), then asserts
that every candidate file appears in the manifest EITHER as a tiered mechanism
OR in the manifest's ``excluded`` list.  A candidate that is neither tiered
nor excluded FAILS the test — this is the "forgot-to-tag" catch.

The complementary ``test_no_dangling_manifest_entries`` runs in the other
direction (manifest → filesystem) and catches mechanisms that were removed or
renamed without updating the manifest.

The ``excluded`` list in packaging/tiers.json is the explicit accounting of
candidate-dir files that are intentionally NOT shipped, each carrying a short
``reason``.  It replaces the deleted HOOKS_EXCLUDE concept by moving the
"do not ship" intent INTO the manifest where it is auditable.
"""

from __future__ import annotations

import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "packaging", "tiers.json")
BUILD_PLUGIN_SH = os.path.join(REPO_ROOT, "tools", "build-plugin.sh")

VALID_TIERS = {"essential", "convenience", "dev"}

TIER_RANKS = {"essential": 0, "convenience": 1, "dev": 2}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _manifest_path_set(manifest: dict) -> set[str]:
    return {entry["path"] for entry in manifest["mechanisms"]}


def _excluded_path_set(manifest: dict) -> set[str]:
    """Return the set of paths in manifest['excluded'] (intentionally-not-shipped)."""
    return {entry["path"] for entry in manifest.get("excluded", [])}


def _shipping_set_for_tier(
    manifest: dict,
    tier: str = "convenience",
    multi_agent: bool = False,
) -> set[str]:
    """Return the set of manifest paths that ship at (tier, multi_agent).

    ships(m) := rank(m.tier) <= rank(chosen_tier)
                AND (m.multi_agent != true OR multi_agent_chosen)
    """
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


def _enumerate_candidate_paths() -> set[str]:
    """Enumerate every file that is a candidate for shipping.

    Returns paths relative to the repo root, using the same granularity as
    packaging/tiers.json (described in the module docstring above).

    These are the paths that build-plugin.sh draws from.  Every one of them
    must be either tiered (in mechanisms[]) or explicitly excluded (in
    excluded[]) — no silent omissions.
    """
    candidates: set[str] = set()

    # hooks/claude: per .py / .sh file
    hooks_dir = os.path.join(REPO_ROOT, "hooks", "claude")
    if os.path.isdir(hooks_dir):
        for fname in os.listdir(hooks_dir):
            if fname.endswith(".py") or fname.endswith(".sh"):
                candidates.add(f"hooks/claude/{fname}")

    # skills/claude: per subdirectory
    skills_dir = os.path.join(REPO_ROOT, "skills", "claude")
    if os.path.isdir(skills_dir):
        for dname in os.listdir(skills_dir):
            if os.path.isdir(os.path.join(skills_dir, dname)):
                candidates.add(f"skills/claude/{dname}")

    # agents/claude: per .md file (excluding README.md)
    agents_dir = os.path.join(REPO_ROOT, "agents", "claude")
    if os.path.isdir(agents_dir):
        for fname in os.listdir(agents_dir):
            if fname.endswith(".md") and fname != "README.md":
                candidates.add(f"agents/claude/{fname}")

    # output-styles/claude: per file
    styles_dir = os.path.join(REPO_ROOT, "output-styles", "claude")
    if os.path.isdir(styles_dir):
        for fname in os.listdir(styles_dir):
            full = os.path.join(styles_dir, fname)
            if os.path.isfile(full):
                candidates.add(f"output-styles/claude/{fname}")

    # tools/*: per top-level item (files and directories), excluding __pycache__
    # build-plugin.sh copies only manifest-listed entries, never __pycache__
    tools_dir = os.path.join(REPO_ROOT, "tools")
    if os.path.isdir(tools_dir):
        for fname in os.listdir(tools_dir):
            if fname == "__pycache__" or fname.endswith(".pyc"):
                continue
            candidates.add(f"tools/{fname}")

    # Top-level named files
    for fname in [
        "server.py",
        "bootstrap.py",
        "SKILL.md",
        "launch-engram-server.sh",
        "viz_server.py",
    ]:
        if os.path.exists(os.path.join(REPO_ROOT, fname)):
            candidates.add(fname)

    # engram_*.py runtime modules
    for fname in os.listdir(REPO_ROOT):
        if fname.startswith("engram_") and fname.endswith(".py"):
            candidates.add(fname)

    # templates: per top-level item (files and directories)
    templates_dir = os.path.join(REPO_ROOT, "templates")
    if os.path.isdir(templates_dir):
        for fname in os.listdir(templates_dir):
            candidates.add(f"templates/{fname}")

    return candidates


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_every_shippable_path_is_tagged():
    """Every candidate file from the build-plugin.sh source dirs must be
    accounted for in packaging/tiers.json.

    Fails naming any candidate path that is neither:
      - listed as a tiered mechanism in mechanisms[], nor
      - explicitly listed in excluded[] as intentionally not shipped.

    This is the primary completeness gate: adding a new skill, hook, or agent
    without tiering it (or explicitly excluding it) is caught here before CI
    merges.  The test enumerates from the FILESYSTEM — not from the manifest —
    so it cannot be defeated by simply omitting an entry.
    """
    manifest = _load_manifest()
    tiered_paths = _manifest_path_set(manifest)
    excluded_paths = _excluded_path_set(manifest)
    accounted_for = tiered_paths | excluded_paths

    candidates = _enumerate_candidate_paths()

    unaccounted: list[str] = []
    for p in sorted(candidates):
        if p not in accounted_for:
            unaccounted.append(p)

    assert not unaccounted, (
        "packaging/tiers.json is missing an accounting for these candidate paths "
        "(add each as a tiered mechanism in mechanisms[] or as an intentionally-excluded "
        "entry in excluded[]):\n"
        + "\n".join(f"  {p}" for p in unaccounted)
    )


def test_no_dangling_manifest_entries():
    """Every manifest entry must correspond to a path that actually exists in the repo.

    Fails naming the specific dangling path(s). This catches the inverse drift:
    a mechanism was removed/renamed but the manifest entry wasn't cleaned up.
    """
    manifest = _load_manifest()
    dangling: list[str] = []
    for entry in manifest["mechanisms"]:
        p = entry["path"]
        full = os.path.join(REPO_ROOT, p)
        if not os.path.exists(full):
            dangling.append(p)

    assert not dangling, (
        f"packaging/tiers.json entries that do not exist in the repo — "
        f"remove or correct them:\n"
        + "\n".join(f"  {p}" for p in sorted(dangling))
    )


def test_valid_tier_values():
    """Every mechanisms[].tier must be one of the declared depth tiers."""
    manifest = _load_manifest()
    declared = set(manifest["depth_tiers"])
    invalid: list[tuple[str, str]] = []
    for entry in manifest["mechanisms"]:
        t = entry.get("tier")
        if t not in declared:
            invalid.append((entry["path"], t))

    assert not invalid, (
        f"packaging/tiers.json entries with invalid tier values "
        f"(must be one of {sorted(declared)}):\n"
        + "\n".join(f"  {p!r}: {t!r}" for p, t in sorted(invalid))
    )


def test_multi_agent_flag_is_bool_when_present():
    """mechanisms[].multi_agent, when present, must be a JSON boolean."""
    manifest = _load_manifest()
    bad: list[str] = []
    for entry in manifest["mechanisms"]:
        if "multi_agent" in entry:
            if not isinstance(entry["multi_agent"], bool):
                bad.append(entry["path"])

    assert not bad, (
        f"packaging/tiers.json entries with non-boolean multi_agent field:\n"
        + "\n".join(f"  {p}" for p in sorted(bad))
    )


def test_no_duplicate_paths():
    """Each path must appear exactly once in mechanisms[].

    A duplicate silently hides the second entry — one of them effectively
    never lands as the authoritative tier assignment.
    """
    manifest = _load_manifest()
    seen: dict[str, int] = {}
    for entry in manifest["mechanisms"]:
        p = entry["path"]
        seen[p] = seen.get(p, 0) + 1

    duplicates = {p: n for p, n in seen.items() if n > 1}
    assert not duplicates, (
        f"packaging/tiers.json has duplicate path entries:\n"
        + "\n".join(f"  {p!r} appears {n} times" for p, n in sorted(duplicates.items()))
    )


def test_manifest_has_required_top_level_fields():
    """The manifest must declare schema_version, depth_tiers, default_tier, mechanisms."""
    manifest = _load_manifest()
    required = {"schema_version", "depth_tiers", "default_tier", "mechanisms"}
    missing = required - set(manifest.keys())
    assert not missing, (
        f"packaging/tiers.json is missing required top-level fields: {sorted(missing)}"
    )


def test_identity_coupled_flag_is_bool_when_present():
    """mechanisms[].identity_coupled, when present, must be a JSON boolean.

    Tag-once gate extension for the identity axis (#778/#791).
    """
    manifest = _load_manifest()
    bad: list[str] = []
    for entry in manifest["mechanisms"]:
        if "identity_coupled" in entry:
            if not isinstance(entry["identity_coupled"], bool):
                bad.append(entry["path"])

    assert not bad, (
        "packaging/tiers.json entries with non-boolean identity_coupled field:\n"
        + "\n".join(f"  {p}" for p in sorted(bad))
    )


def test_identity_coupled_hooks_are_known():
    """The 5 identity-coupled hooks from spec #778 must all be tagged identity_coupled=true.

    Ensures the tag-once gate covers the load-bearing set.
    """
    manifest = _load_manifest()
    ic_paths = {
        entry["path"]
        for entry in manifest["mechanisms"]
        if entry.get("identity_coupled") is True
    }

    expected = {
        "hooks/claude/engram-baton-prompt-hook.py",
        "hooks/claude/engram-inter-agent-prompt-hook.py",
        "hooks/claude/engram-forum-prompt-hook.py",
        "hooks/claude/engram-github-notifications-hook.py",
        "hooks/claude/engram-end-of-day-hook.py",
    }
    missing = expected - ic_paths
    assert not missing, (
        "Expected identity_coupled=true on these hooks per spec #778, but it is missing:\n"
        + "\n".join(f"  {p}" for p in sorted(missing))
    )
