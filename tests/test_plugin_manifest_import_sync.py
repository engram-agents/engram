"""Drift test: every project-local module imported by server.py must be
shipped by tools/build-plugin.sh.

The incident this test prevents: PR #83 (2026-05-11) shipped a new module
`engram_filter.py` that `server.py` imports unqualified. The ``tools/deploy.sh``
MANIFEST was not updated to include it. First post-merge deploy left the
running ENGRAM tree without the module — ``from engram_filter import ...``
inside engram_list would have raised ModuleNotFoundError the moment any
agent invoked the structured filter. Manual copy unblocked; PR #89 +
``test_deploy_manifest_sync.py`` (now retired alongside deploy.sh) prevented
recurrence via the scatter-era MANIFEST check.

This test is the plugin-era replacement, filed upon ``tools/deploy.sh``
retirement as part of the scatter-retirement EPIC. It asserts the same
invariant through the plugin-era copy mechanism:

    tools/build-plugin.sh is the single copy authority for the plugin
    layout. Its shipping predicate is: rank(mechanism.tier) <=
    rank(chosen_tier), excluding multi_agent-only entries when building
    without --multi-agent. The default tier is "convenience" (ships
    essential + convenience mechanisms). All modules that server.py imports
    at runtime must be covered by a non-multi-agent mechanism in
    packaging/tiers.json.

Replaces: ``tests/test_deploy_manifest_sync.py`` (retired with deploy.sh).
See the PR #83 lineage note in that file for the root incident history.

The test mirrors test_tiers_manifest.py's discipline: parse JSON/AST
offline, assert sets agree, fast (<1s), no DB.

Granularity note
================
build-plugin.sh copies project-local modules via three distinct rules:

  * Top-level named files (server.py, bootstrap.py,
    launch-engram-server.sh): per-file manifest check, copied if
    ships(path).

  * engram_*.py runtime modules: glob at repo root, each file copied
    if ships(basename).

  * tools/X: manifest-driven per-item copy; tools/recall_summary_validator.py
    ships if "tools/recall_summary_validator.py" appears as a mechanism
    with appropriate tier.

The test enumerates the same three path shapes FOR THE server.py IMPORT
SURFACE (named files build-plugin.sh copies but server.py never imports —
e.g. launch-engram-server.sh — are out of scope here; the tag-once gate in
test_tiers_manifest.py covers those), so the coverage check is 1:1 with
the build script's actual copy logic where the two surfaces overlap.
"""

from __future__ import annotations

import ast
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(REPO_ROOT, "src", "engram", "server.py")
TIERS_JSON = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")

# Tier ranking mirrors build-plugin.sh's tier_rank() function.
_TIER_RANKS: dict[str, int] = {"essential": 0, "convenience": 1, "dev": 2}

# Default tier matches packaging/tiers.json "default_tier": "convenience".
# This is what a plain `tools/build-plugin.sh` (no --tier flag) produces.
_DEFAULT_TIER = "convenience"
_DEFAULT_TIER_RANK = _TIER_RANKS[_DEFAULT_TIER]


def _load_manifest() -> dict:
    with open(TIERS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _shipped_paths(manifest: dict, multi_agent: bool = False) -> set[str]:
    """Return the set of mechanism paths that ship at default tier.

    Mirrors the ships() predicate in build-plugin.sh:
        ships(m) := rank(m.tier) <= rank(chosen_tier)
                    AND (m.multi_agent != true OR multi_agent_chosen)
    """
    result: set[str] = set()
    for entry in manifest["mechanisms"]:
        rank = _TIER_RANKS.get(entry.get("tier", "dev"), 2)
        if rank > _DEFAULT_TIER_RANK:
            continue
        if entry.get("multi_agent") is True and not multi_agent:
            continue
        result.add(entry["path"])
    return result


def _extract_imports_from_server() -> set[str]:
    """Return the set of top-level module names imported by server.py.

    Walks the entire AST (including function bodies) to catch lazy local
    imports — the engram_filter.py incident (PR #83) introduced a lazy
    import inside engram_list that was invisible to top-level-only parsers.
    """
    with open(SERVER_PY, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=SERVER_PY)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return imports


def _project_root_module_names() -> set[str]:
    """Names (without .py extension) of every Python file at alpha root."""
    return {
        f[:-3]
        for f in os.listdir(REPO_ROOT)
        if f.endswith(".py") and os.path.isfile(os.path.join(REPO_ROOT, f))
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_server_imports_all_covered_by_build_plugin():
    """Every project-local module imported by server.py must ship via build-plugin.sh.

    Catches the engram_filter.py incident (PR #83 / PR #89 root cause):
    server.py imported a new module that wasn't in the shipping manifest,
    so the deployed plugin would lack the module at runtime.

    Coverage path for each imported module shape:
      - engram_X.py → covered if "engram_X.py" is in tiers.json mechanisms
        at tier <= convenience (build-plugin.sh §1b glob copy).
      - Top-level named .py (server.py, bootstrap.py, etc.) → covered if
        the filename is in tiers.json mechanisms at tier <= convenience
        (build-plugin.sh §1 per-file copy).
      - tools.X → covered if "tools/X.py" is in tiers.json mechanisms at
        tier <= convenience (build-plugin.sh §1b manifest-driven copy).

    Excludes multi-agent-only mechanisms (server.py must ship in base
    non-MA builds), matching the build script's default invocation with
    no --multi-agent flag.
    """
    manifest = _load_manifest()
    shipped = _shipped_paths(manifest, multi_agent=False)

    all_imports = _extract_imports_from_server()
    project_modules = _project_root_module_names()

    # Build the set of manifest paths that cover top-level project modules.
    # Strip .py from mechanism paths to get module names for comparison.
    shipped_module_names: set[str] = {
        p[:-3] if p.endswith(".py") else p for p in shipped
    }

    missing: list[str] = []

    for imported_name in sorted(all_imports):
        # Case 1: direct import of a project-root module (engram_*.py or other .py)
        if imported_name in project_modules:
            py_path = imported_name + ".py"
            if py_path not in shipped and imported_name not in shipped_module_names:
                missing.append(
                    f"{imported_name}.py (project-root module, "
                    f"expected as mechanism in packaging/tiers.json)"
                )

        # Case 2: import of tools.X (server.py uses `from tools.X import ...`)
        # Check whether the sub-module resolution is covered.
        # This case is handled by the companion test below; skip here.

    assert not missing, (
        f"server.py imports project-root modules NOT covered by "
        f"build-plugin.sh's shipping rules (packaging/tiers.json at "
        f"tier <= {_DEFAULT_TIER!r}, non-multi-agent):\n"
        + "\n".join(f"  {m}" for m in missing)
        + "\n\nAdd each missing module to packaging/tiers.json as a tiered "
        f"mechanism, or move it to the manifest excluded[] list if it is "
        f"intentionally not shipped."
    )


def test_server_tools_imports_covered_by_build_plugin():
    """Every tools.X sub-module imported by server.py must ship via build-plugin.sh.

    build-plugin.sh §1b copies tools/ items through the manifest predicate:
    only paths listed as "tools/X" in tiers.json mechanisms ship. A new
    ``from tools.new_module import ...`` in server.py that isn't in
    tiers.json would be silently absent from the plugin at runtime.

    Covers the tools-submodule equivalent of the PR #83 engram_filter.py
    incident for the tools/ namespace.
    """
    manifest = _load_manifest()
    shipped = _shipped_paths(manifest, multi_agent=False)

    with open(SERVER_PY, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=SERVER_PY)

    tools_imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("tools."):
                # "tools.recall_summary_validator" → tools/recall_summary_validator.py
                parts = node.module.split(".", 1)
                tools_imports.add(f"tools/{parts[1]}.py")
            elif node.module == "tools":
                # Bare-package form: "from tools import X". X is a submodule
                # iff tools/X.py exists on disk (tools/__init__.py is a stub,
                # so in practice every name here is a submodule); require it
                # shipped just like the dotted form.
                for alias in node.names:
                    if os.path.isfile(
                        os.path.join(REPO_ROOT, "tools", alias.name + ".py")
                    ):
                        tools_imports.add(f"tools/{alias.name}.py")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("tools."):
                    parts = alias.name.split(".", 1)
                    tools_imports.add(f"tools/{parts[1]}.py")

    missing = [p for p in sorted(tools_imports) if p not in shipped]

    assert not missing, (
        f"server.py imports tools/ sub-modules NOT covered by "
        f"build-plugin.sh's shipping rules (packaging/tiers.json at "
        f"tier <= {_DEFAULT_TIER!r}, non-multi-agent):\n"
        + "\n".join(f"  {m}" for m in missing)
        + "\n\nAdd each missing module to packaging/tiers.json as a tiered "
        f"mechanism at tier 'essential' (build-plugin.sh guard: every "
        f"tools.X import in server.py must be tagged essential)."
    )


def test_manifest_default_tier_matches_expected():
    """packaging/tiers.json default_tier must be 'convenience'.

    The shipping predicate in this test file is hardcoded to 'convenience'
    to mirror the plain build-plugin.sh invocation. If the manifest
    changes the default tier, this test catches the drift before the
    test's own assertions become wrong.
    """
    manifest = _load_manifest()
    actual = manifest.get("default_tier")
    assert actual == _DEFAULT_TIER, (
        f"packaging/tiers.json default_tier changed from {_DEFAULT_TIER!r} "
        f"to {actual!r}. Update _DEFAULT_TIER in this test file to match, "
        f"then re-verify the import-coverage assertions are still correct."
    )
