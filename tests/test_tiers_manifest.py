"""Tag-once gate: every path in the build-plugin.sh shippable universe must appear
exactly once in packaging/tiers.json, with no dangling manifest entries.

Enumeration unit choices (load-bearing design decision)
=======================================================
build-plugin.sh uses DIFFERENT copy granularities for different surfaces:

  * Top-level files (server.py, bootstrap.py, SKILL.md, launch-engram-server.sh,
    engram_*.py, RUNTIME_TOOLS): copied per-file → manifest unit = per-file.

  * skills/claude: ``cp -r skills/claude/.`` = whole dir contents; each skill is
    a named sub-directory. Manifest unit = per-skill-dir, e.g.
    ``skills/claude/engram-sleep``.

  * hooks/claude: ``find hooks/claude -maxdepth 1 -name '*.py' -o -name '*.sh'``
    = per-file, minus HOOKS_EXCLUDE. Manifest unit = per-hook-file, e.g.
    ``hooks/claude/engram-surface-hook.py``.

  * agents/claude: ``find agents/claude -maxdepth 1 -name '*.md' ! -name README.md``
    = per-file. Manifest unit = per-agent-file, e.g.
    ``agents/claude/engram-dream-fairy.md``.

  * output-styles/claude: ``cp -r output-styles/claude/.`` = whole dir contents;
    each item is a flat file. Manifest unit = per-file, e.g.
    ``output-styles/claude/proactive-with-carveouts.md``.

  * templates: ``cp -a templates/.`` = whole dir contents; items may be files OR
    sub-directories (e.g. heartbeat-queue/). Manifest unit = per-top-level-item
    inside templates/, e.g. ``templates/CLAUDE.md.template`` and
    ``templates/heartbeat-queue``.

The test enumerates each surface at the SAME granularity the manifest uses, so
the cross-check is 1:1 with no false mismatches.

Scope boundary
==============
The CI test only checks paths that build-plugin.sh ships (the plugin install
path). The manifest intentionally covers MORE than this — scatter-install paths
(tools/deploy.sh, tools/baton.py, viz_server.py, etc.), the Gemini integration,
and operator tools also have manifest entries. Those extra entries are valid
(the dangling-entry check only fires if a manifest path does NOT exist anywhere
in the repo, not if it falls outside the plugin-copy scope). This gives the
manifest broader coverage while keeping the CI gate mechanical and offline.
"""

from __future__ import annotations

import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "packaging", "tiers.json")
BUILD_PLUGIN_SH = os.path.join(REPO_ROOT, "tools", "build-plugin.sh")

VALID_TIERS = {"essential", "convenience", "dev"}


# ---------------------------------------------------------------------------
# helpers — enumerate the build-plugin.sh shippable universe
# ---------------------------------------------------------------------------


def _parse_hooks_exclude() -> set[str]:
    """Parse the HOOKS_EXCLUDE array from tools/build-plugin.sh.

    Returns a set of bare filenames (no path prefix) that build-plugin.sh
    explicitly skips when copying hooks/claude/.

    Uses a multiline-aware end-of-array anchor (``^\\)``) so inline comments
    containing parentheses (e.g. ``# persona-specific (multi-agent...)``) do
    not prematurely close the match.
    """
    with open(BUILD_PLUGIN_SH, encoding="utf-8") as f:
        text = f.read()

    m = re.search(
        r"HOOKS_EXCLUDE=\((.*?)^\)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not m:
        raise AssertionError(
            "Could not locate HOOKS_EXCLUDE=( ... ) array in tools/build-plugin.sh. "
            "Did the variable name or syntax change? Update _parse_hooks_exclude()."
        )

    block = m.group(1)
    excludes: set[str] = set()
    for line in block.splitlines():
        # Strip inline comments before splitting on whitespace
        line = line.split("#")[0].strip()
        if not line:
            continue
        token = line.strip('"').strip("'")
        if token:
            excludes.add(token)
    return excludes


def _parse_runtime_tools() -> list[str]:
    """Parse the RUNTIME_TOOLS array from tools/build-plugin.sh.

    Returns a list of bare filenames (no path prefix) in the tools/ subset
    that build-plugin.sh copies to plugin/tools/.
    """
    with open(BUILD_PLUGIN_SH, encoding="utf-8") as f:
        text = f.read()

    m = re.search(
        r"RUNTIME_TOOLS=\((.*?)^\)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not m:
        raise AssertionError(
            "Could not locate RUNTIME_TOOLS=( ... ) array in tools/build-plugin.sh. "
            "Did the variable name or syntax change? Update _parse_runtime_tools()."
        )

    block = m.group(1)
    tools: list[str] = []
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0].strip('"').strip("'")
        if token:
            tools.append(token)
    return tools


def _enumerate_shippable_plugin_paths() -> set[str]:
    """Enumerate every path that build-plugin.sh copies into the plugin tree.

    Returns paths relative to the repo root, using the same granularity as
    packaging/tiers.json (described in the module docstring above).
    """
    shippable: set[str] = set()

    # 1. Explicitly copied top-level files
    for p in ("server.py", "bootstrap.py", "SKILL.md", "launch-engram-server.sh"):
        shippable.add(p)

    # 2. engram_*.py modules (globbed from repo root)
    for fname in os.listdir(REPO_ROOT):
        if fname.startswith("engram_") and fname.endswith(".py"):
            fpath = os.path.join(REPO_ROOT, fname)
            if os.path.isfile(fpath):
                shippable.add(fname)

    # 3. RUNTIME_TOOLS subset of tools/
    for tool_basename in _parse_runtime_tools():
        shippable.add(f"tools/{tool_basename}")

    # 4. hooks/claude/* minus HOOKS_EXCLUDE (per-file granularity)
    hooks_dir = os.path.join(REPO_ROOT, "hooks", "claude")
    excludes = _parse_hooks_exclude()
    for fname in os.listdir(hooks_dir):
        fpath = os.path.join(hooks_dir, fname)
        if not os.path.isfile(fpath):
            continue
        if not (fname.endswith(".py") or fname.endswith(".sh")):
            continue
        if fname in excludes:
            continue
        shippable.add(f"hooks/claude/{fname}")

    # 5. skills/claude/* (per-skill-dir granularity)
    skills_dir = os.path.join(REPO_ROOT, "skills", "claude")
    for dname in os.listdir(skills_dir):
        dpath = os.path.join(skills_dir, dname)
        if os.path.isdir(dpath):
            shippable.add(f"skills/claude/{dname}")

    # 6. agents/claude/*.md excluding README.md (per-file granularity)
    agents_dir = os.path.join(REPO_ROOT, "agents", "claude")
    for fname in os.listdir(agents_dir):
        if fname.endswith(".md") and fname != "README.md":
            fpath = os.path.join(agents_dir, fname)
            if os.path.isfile(fpath):
                shippable.add(f"agents/claude/{fname}")

    # 7. output-styles/claude/* (per-file granularity)
    styles_dir = os.path.join(REPO_ROOT, "output-styles", "claude")
    for fname in os.listdir(styles_dir):
        fpath = os.path.join(styles_dir, fname)
        if os.path.isfile(fpath):
            shippable.add(f"output-styles/claude/{fname}")

    # 8. templates/* (per-top-level-item granularity — files AND sub-dirs)
    templates_dir = os.path.join(REPO_ROOT, "templates")
    for item in os.listdir(templates_dir):
        shippable.add(f"templates/{item}")

    return shippable


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _manifest_path_set(manifest: dict) -> set[str]:
    return {entry["path"] for entry in manifest["mechanisms"]}


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_every_shippable_path_is_tagged():
    """Every path that build-plugin.sh ships must appear in packaging/tiers.json.

    Fails naming the specific path(s) that are shippable but untagged.
    This is the primary drift-prevention gate: adding a new skill/hook/agent
    without tiering it is caught here before CI merges.
    """
    shippable = _enumerate_shippable_plugin_paths()
    manifest_paths = _manifest_path_set(_load_manifest())

    missing = shippable - manifest_paths
    assert not missing, (
        f"Shippable path(s) not tagged in packaging/tiers.json — add them with a tier:\n"
        + "\n".join(f"  {p}" for p in sorted(missing))
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
