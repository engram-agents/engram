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


HOOKS_CLAUDE_DIR = os.path.join(REPO_ROOT, "src", "engram", "hooks", "claude")


def _single_underscore_import_names(py_path: str) -> set[str]:
    """Module names imported by py_path with exactly one leading underscore.

    Walks the whole AST (not just top-level) so imports nested inside
    ``try:`` blocks or function bodies are caught regardless of
    indentation -- all of the real hooks/claude/ sibling imports are inside
    try: guards, and a column-0-anchored regex would false-pass on every
    one of them. ``__future__`` and other dunder-prefixed names (two
    leading underscores) are excluded on purpose: a naive ``_[a-z_]+``
    regex also matches ``__future__`` and produces false positives.
    """
    with open(py_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=py_path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top.startswith("_") and not top.startswith("__"):
                    names.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top.startswith("_") and not top.startswith("__"):
                    names.add(top)
    return names


def _hook_sibling_tier_violations(manifest: dict) -> list[str]:
    """Return tier-rank violations among hooks/claude/ sibling imports.

    For every mechanism entry under "hooks/claude/" (i.e. every hooks/claude/
    file that itself ships as a tiered mechanism), find its single-underscore
    imports and, for each import that resolves to an actual sibling file on
    disk in hooks/claude/ (this is what distinguishes a real sibling import
    from e.g. ``from _status_derive import ...``, which is a tools/ module
    reached via a runtime sys.path bootstrap rather than a hooks/claude/
    sibling), require:

      1. the sibling module itself has a mechanisms[] entry, and
      2. rank(sibling.tier) <= rank(importer.tier)
         (essential=0 outranks convenience=1 outranks dev=2, i.e. a
         stricter/lower-rank-numbered tier must not depend on a looser one).

    This check is intentionally strict about guarded-vs-unguarded imports:
    it applies to every single-underscore sibling import found via AST,
    whether or not it sits inside a try/except ImportError guard. A guarded
    import of a missing module still degrades the hook's behavior at
    runtime (see the docstrings in e.g. engram-baton-prompt-hook.py,
    engram-forum-prompt-hook.py), so the same-or-stricter-tier invariant is
    applied uniformly rather than relaxed for guarded call sites.
    """
    mechanisms_by_path = {m["path"]: m for m in manifest["mechanisms"]}
    tier_ranks = _TIER_RANKS

    hook_mechanisms = [
        m
        for m in manifest["mechanisms"]
        if m["path"].startswith("hooks/claude/") and m["path"].endswith(".py")
    ]

    violations: list[str] = []

    for mech in hook_mechanisms:
        rel_path = mech["path"]  # e.g. "hooks/claude/engram-stop-hook.py"
        importer_tier = mech.get("tier", "dev")
        importer_rank = tier_ranks.get(importer_tier, 2)

        abs_path = os.path.join(REPO_ROOT, "src", "engram", rel_path)
        if not os.path.isfile(abs_path):
            continue  # dangling-source is a separate, existing check's job

        imported_names = _single_underscore_import_names(abs_path)

        for name in sorted(imported_names):
            sibling_disk_path = os.path.join(HOOKS_CLAUDE_DIR, name + ".py")
            if not os.path.isfile(sibling_disk_path):
                # Not a physical sibling in hooks/claude/ (e.g. _status_derive
                # is a tools/ module reached via a runtime sys.path bootstrap
                # rather than an on-disk hooks/claude/ sibling) -- out of
                # scope for this check. NOTE (reviewer, #2107): this also means
                # a DANGLING sibling import (module deleted from disk but still
                # imported) vanishes from this tier check rather than flagging;
                # this test's invariant is narrowly tier-rank sync. An UNGUARDED
                # dangling import fails loudly the moment any test imports that
                # hook module; a try:-guarded one degrades by design.
                continue

            sibling_rel_path = f"hooks/claude/{name}.py"
            sibling_mech = mechanisms_by_path.get(sibling_rel_path)
            if sibling_mech is None:
                violations.append(
                    f"{rel_path} imports sibling {name} "
                    f"({sibling_rel_path}), which has no mechanisms[] "
                    f"entry in packaging/tiers.json"
                )
                continue

            sibling_tier = sibling_mech.get("tier", "dev")
            sibling_rank = tier_ranks.get(sibling_tier, 2)
            if sibling_rank > importer_rank:
                violations.append(
                    f"{rel_path} (tier={importer_tier!r}) imports "
                    f"{sibling_rel_path} (tier={sibling_tier!r}), but "
                    f"{sibling_tier!r} ranks looser than {importer_tier!r} "
                    f"-- an install at {importer_tier!r} could ship the "
                    f"importer without the sibling it imports"
                )

    return violations


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


def test_hook_sibling_imports_tier_rank_ok():
    """Every hooks/claude/ sibling import must not outrank its importer.

    src/engram/hooks/claude/ modules import each other directly (not via
    build-plugin.sh's copy rules but via a shared install directory), e.g.
    ``from _hooklib import atomic_write_json``. If an importer ships at a
    stricter tier than the sibling module it imports unconditionally
    (essential outranks convenience outranks dev, per depth_tiers order in
    packaging/tiers.json), an essential-tier install can end up shipping the
    importer without the module it needs at import time.

    Post-#2041 state: this must FAIL if someone re-tiers _hooklib.py below
    the essential hooks that import it unguarded — see
    ``test_hook_sibling_import_tier_rank_mutation_self_test`` below, which
    exercises exactly that mutation against the checker itself.
    """
    manifest = _load_manifest()
    violations = _hook_sibling_tier_violations(manifest)
    assert not violations, (
        "hooks/claude/ sibling imports violate the tier-rank invariant "
        "(imported module's tier must rank <= importer's tier; essential "
        "outranks convenience outranks dev per packaging/tiers.json "
        "depth_tiers):\n" + "\n".join(f"  {v}" for v in violations)
    )


def test_hook_sibling_import_tier_rank_mutation_self_test():
    """Self-test: the checker must catch a mis-tiered _hooklib.py.

    Mutates a copy of the loaded manifest so that
    ``hooks/claude/_hooklib.py`` is tiered "dev" (lowest rank) while several
    essential-tier hooks (e.g. engram-postcompact-hook.py) import it
    unconditionally. If the checker doesn't flag this, it isn't actually
    enforcing the invariant it claims to.
    """
    manifest = _load_manifest()
    mutated = json.loads(json.dumps(manifest))  # deep copy
    found = False
    for entry in mutated["mechanisms"]:
        if entry["path"] == "hooks/claude/_hooklib.py":
            entry["tier"] = "dev"
            found = True
            break
    assert found, "expected hooks/claude/_hooklib.py in tiers.json mechanisms"

    violations = _hook_sibling_tier_violations(mutated)
    assert violations, (
        "mutation self-test failed: lowering hooks/claude/_hooklib.py to "
        "tier 'dev' should have produced at least one tier-rank violation "
        "(it is imported unconditionally by essential-tier hooks), but the "
        "checker reported none. The checker is not enforcing the invariant."
    )
    assert any("_hooklib" in v for v in violations), (
        f"mutation self-test produced violations, but none mention "
        f"_hooklib.py: {violations}"
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
