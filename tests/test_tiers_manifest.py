"""Tag-once gate: every tracked file in the repository must have exactly one
declared role in packaging/tiers.json — default-deny, not default-ignore.

Two complementary scopes
========================

**Whole-tree gate** (``test_whole_tree_every_tracked_file_has_a_role``):
  Enumerates ALL files tracked by git (``git ls-files``) and asserts that every
  one of them is accounted for in EXACTLY ONE of four manifest buckets:

  * ``mechanisms[]`` — tiered shippable (the plugin install universe).
  * ``excluded[]`` — exists in a candidate dir but intentionally not shipped
    (each entry carries a ``reason``).
  * ``repo_only[]`` — tracked but not a shippable artifact at all (tests, CI
    config, docs, root dev files); each entry carries a ``reason``.
  * ``build_inputs[]`` — consumed by the build/install process and not a
    depth-tier-gated mechanism: the plugin/hooks manifests copied or filtered
    into the bundle, and the packaging sources baked into bundle artifacts
    (``.mcp.json``, ``platform.json``); each entry carries a ``reason``.

  Matching is prefix-aware: a manifest entry ``skills/claude/engram-sleep``
  (a directory) covers the tracked file ``skills/claude/engram-sleep/SKILL.md``
  (a file inside it).  This mirrors the granularity used by build-plugin.sh
  (per-directory for skills, per-directory for tools sub-packages like
  tools/engine, tools/migration, tools/engram-pkg).

  Any tracked file in NONE of the four buckets → test FAILS, naming the
  unaccounted path(s).  Any tracked file matched by MORE than one bucket
  (overlap/ambiguity) → test ALSO FAILS.

**Plugin-universe gate** (``test_every_shippable_path_is_tagged``):
  Enumerates only the files that ``build-plugin.sh`` draws from (the original
  hand-coded surface), and asserts each is in ``mechanisms[]`` or
  ``excluded[]``.  This gate is more precise within its narrow scope (same
  granularity as the manifest for hooks, agents, skills, etc.) and is kept
  alongside the whole-tree gate as the authoritative shippable-universe check.

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
    ``templates/template.CLAUDE.md`` and ``templates/config.json.example``.

The plugin-universe test enumerates each surface at the SAME granularity the
manifest uses, so the cross-check is 1:1 with no false mismatches.

Scope boundary (plugin-universe gate)
======================================
The CI test only checks paths that build-plugin.sh ships (the plugin install
path). The manifest intentionally covers MORE than this — scatter-install paths
(tools/baton.py, viz_server.py, etc.) and operator tools (tools/agentctl,
tools/agent-bootstrap) also have manifest entries. Those extra entries are valid
(the dangling-entry check only fires if a manifest path does NOT exist anywhere
in the repo, not if it falls outside the plugin-copy scope). This gives the
manifest broader coverage while keeping the CI gate mechanical and offline.

The ``excluded`` list in packaging/tiers.json is the explicit accounting of
candidate-dir files that are intentionally NOT shipped, each carrying a short
``reason``.  It replaces the deleted HOOKS_EXCLUDE concept by moving the
"do not ship" intent INTO the manifest where it is auditable.

The ``repo_only`` list accounts for tracked files that are NEVER candidates for
shipping through any deploy path — test suite, CI config, docs, root dev files,
per-developer local config, build infrastructure.  Each entry carries a
``reason`` so the intent is auditable.

Removed integration lanes are DELETED from the repo, not excluded via
the ``excluded`` list — removed lanes are deleted from the tree rather
than excluded; the Gemini lane removal decision (issue #865, PR #873) is
the canonical precedent.  An ``excluded`` entry is correct only for
a file that EXISTS in the repo's candidate dirs but is intentionally
withheld from the plugin bundle.

Untiered drift WITHIN the plugin universe fails ``test_every_shippable_path_is_tagged``.
Unaccounted drift anywhere in the tree fails ``test_whole_tree_every_tracked_file_has_a_role``.
Manifest entries pointing at deleted or renamed files fail
``test_no_dangling_manifest_entries``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")
BUILD_PLUGIN_SH = os.path.join(REPO_ROOT, "tools", "build-plugin.sh")

sys.path.insert(0, REPO_ROOT)
from tools import telemetry_manifest_gate as telemetry_gate  # noqa: E402

VALID_TIERS = {"essential", "convenience", "dev"}

TIER_RANKS = {"essential": 0, "convenience": 1, "dev": 2}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _manifest_path_set(manifest: dict) -> set[str]:
    """Return the set of paths (and source paths) for all mechanism entries.

    When an entry carries a "source" field (repo-root-relative override used
    when the real source lives outside source_root, e.g. src/build/engine ships
    as bundle tools/engine), include that source path directly — it IS the
    tracked file and must count as covered.
    """
    paths: set[str] = set()
    for entry in manifest["mechanisms"]:
        paths.add(entry["path"])
        if "source" in entry:
            paths.add(entry["source"])
    return paths


def _excluded_path_set(manifest: dict) -> set[str]:
    """Return the set of paths in manifest['excluded'] (intentionally-not-shipped)."""
    return {entry["path"] for entry in manifest.get("excluded", [])}


def _repo_only_path_set(manifest: dict) -> set[str]:
    """Return the set of paths in manifest['repo_only'] (tracked but never shipped)."""
    return {entry["path"] for entry in manifest.get("repo_only", [])}


def _build_inputs_path_set(manifest: dict) -> set[str]:
    """Return the set of paths in manifest['build_inputs'].

    Build/install-consumed files that are not depth-tier-gated mechanisms:
    the plugin/hooks manifests copied or filtered into the bundle, and the
    packaging sources baked into bundle artifacts (.mcp.json, platform.json).
    """
    return {entry["path"] for entry in manifest.get("build_inputs", [])}


def _enumerate_all_tracked_paths() -> set[str]:
    """Enumerate every file tracked by git in the repository.

    Returns paths relative to the repo root, using ``git ls-files``.
    This is the whole-tree universe for the default-deny gate.
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {p for p in result.stdout.splitlines() if p}


def _path_is_covered(tracked_path: str, bucket_paths: set[str]) -> bool:
    """Return True if ``tracked_path`` is covered by any entry in ``bucket_paths``.

    Matching is prefix-aware: a bucket entry ending with ``/`` (or matching a
    directory prefix of the tracked path) covers all files inside it.  This
    mirrors the build-plugin.sh granularity for skills/, tools/engine/,
    tools/migration/, tools/engram-pkg/, etc. — where the manifest carries a
    directory-level entry but ``git ls-files`` yields individual files.

    Exact match also counts (for file-level manifest entries).
    """
    # Exact match
    if tracked_path in bucket_paths:
        return True
    # Prefix match: bucket entry is a directory prefix of the tracked path
    for bp in bucket_paths:
        # Normalise: treat "foo/" and "foo" both as directory prefixes
        prefix = bp if bp.endswith("/") else bp + "/"
        if tracked_path.startswith(prefix):
            return True
    return False


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

    # Resolve actual source root from the manifest (Phase 1 #1093 moved sources
    # to src/engram/; root-level compat symlinks removed in Phase 4).
    src_root = os.path.join(REPO_ROOT, _load_manifest().get("source_root", "."))

    # hooks/claude: per .py / .sh file
    hooks_dir = os.path.join(src_root, "hooks", "claude")
    if os.path.isdir(hooks_dir):
        for fname in os.listdir(hooks_dir):
            if fname.endswith(".py") or fname.endswith(".sh"):
                candidates.add(f"hooks/claude/{fname}")

    # skills/claude: per subdirectory
    skills_dir = os.path.join(src_root, "skills", "claude")
    if os.path.isdir(skills_dir):
        for dname in os.listdir(skills_dir):
            if os.path.isdir(os.path.join(skills_dir, dname)):
                candidates.add(f"skills/claude/{dname}")

    # agents/claude: per .md file (excluding README.md)
    agents_dir = os.path.join(src_root, "agents", "claude")
    if os.path.isdir(agents_dir):
        for fname in os.listdir(agents_dir):
            if fname.endswith(".md") and fname != "README.md":
                candidates.add(f"agents/claude/{fname}")

    # output-styles/claude: per file
    styles_dir = os.path.join(src_root, "output-styles", "claude")
    if os.path.isdir(styles_dir):
        for fname in os.listdir(styles_dir):
            full = os.path.join(styles_dir, fname)
            if os.path.isfile(full):
                candidates.add(f"output-styles/claude/{fname}")

    # tools/*: per top-level item — derived from git-tracked files only.
    # Using git ls-files (via _enumerate_all_tracked_paths) avoids false-positives
    # from empty dirs left over after file archival (e.g. tools/migration/ after #984).
    # An empty untracked directory has no tracked files and is not a shippable candidate.
    all_tracked = _enumerate_all_tracked_paths()
    for p in all_tracked:
        if p.startswith("tools/"):
            parts = p.split("/")
            if len(parts) >= 2 and parts[1] != "__pycache__":
                candidates.add(f"tools/{parts[1]}")

    # Top-level named files (relative to source_root, not repo root — Phase 4)
    # viz_server.py removed here (engram-alpha#2555 PR A): it no longer exists
    # as a root-level file -- it split into the viz/ package. No replacement
    # walker was added for viz/ specifically, matching the existing precedent
    # that health/ (a prior package-ification, #2537) also has no walker here
    # — package-level directories are covered by the separate, more thorough
    # whole-tree gate (test_whole_tree_every_tracked_file_has_a_role below),
    # not by this narrower candidate enumerator.
    for fname in [
        "server.py",
        "bootstrap.py",
        "launch-engram-server.sh",
    ]:
        if os.path.exists(os.path.join(src_root, fname)):
            candidates.add(fname)

    # engram_*.py runtime modules (live in source_root after Phase 1 #1093)
    for fname in os.listdir(src_root):
        if fname.startswith("engram_") and fname.endswith(".py"):
            candidates.add(fname)

    # templates: per top-level item (files and directories)
    templates_dir = os.path.join(src_root, "templates")
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


def test_whole_tree_every_tracked_file_has_a_role():
    """Every git-tracked file must have exactly one declared role in packaging/tiers.json.

    Four buckets, each carrying ``reason`` strings:
      - ``mechanisms[]`` — tiered shippable (plugin install universe)
      - ``excluded[]`` — exists in a candidate dir but intentionally not shipped
      - ``repo_only[]`` — tracked but not a shippable artifact (tests, CI, docs, etc.)
      - ``build_inputs[]`` — build/install-consumed, not a depth-tier mechanism
        (plugin/hooks manifests, packaging sources baked into bundle artifacts)

    Any tracked file in NONE of the four buckets → FAIL (unaccounted).
    Any tracked file matched by MORE than one bucket → FAIL (ambiguous).

    Matching is prefix-aware: a manifest entry for a directory covers all
    tracked files inside it (see ``_path_is_covered``).

    This is the whole-tree default-deny gate (issue #968).
    """
    manifest = _load_manifest()
    source_root = manifest.get("source_root", ".")

    def _with_prefix(paths: set[str], skip_dir_symlinks: bool = False) -> set[str]:
        """Expand each path to cover both the bare form and source-root-prefixed form.

        When skip_dir_symlinks=True, directory symlinks are not expanded: git
        tracks them as single blob entries (not directory prefixes pointing into
        their targets), so adding src/engram/<symlink> would prefix-match real
        files in src/engram/<symlink>/ already covered by mechanisms[].
        Pass skip_dir_symlinks=True only for repo_only[] expansion.
        """
        if source_root == ".":
            return paths
        expanded: set[str] = set(paths)
        for p in paths:
            if skip_dir_symlinks:
                full = os.path.join(REPO_ROOT, p)
                if os.path.islink(full) and os.path.isdir(os.path.realpath(full)):
                    continue  # directory symlink — keep bare form only
            expanded.add(source_root + "/" + p)
        return expanded

    tiered_paths = _with_prefix(_manifest_path_set(manifest))
    excluded_paths = _with_prefix(_excluded_path_set(manifest))
    repo_only_paths = _with_prefix(_repo_only_path_set(manifest), skip_dir_symlinks=True)
    build_inputs_paths = _with_prefix(_build_inputs_path_set(manifest))

    tracked = _enumerate_all_tracked_paths()

    unaccounted: list[str] = []
    ambiguous: list[str] = []

    for p in sorted(tracked):
        in_tiered = _path_is_covered(p, tiered_paths)
        in_excluded = _path_is_covered(p, excluded_paths)
        in_repo_only = _path_is_covered(p, repo_only_paths)
        in_build_inputs = _path_is_covered(p, build_inputs_paths)

        matches = sum([in_tiered, in_excluded, in_repo_only, in_build_inputs])
        if matches == 0:
            unaccounted.append(p)
        elif matches > 1:
            ambiguous.append(p)

    errors: list[str] = []
    if unaccounted:
        errors.append(
            "Tracked files with no declared role in packaging/tiers.json "
            "(add to mechanisms[], excluded[], repo_only[], or build_inputs[]):\n"
            + "\n".join(f"  {p}" for p in unaccounted)
        )
    if ambiguous:
        errors.append(
            "Tracked files matched by more than one bucket (overlap — "
            "each file must have exactly one role):\n"
            + "\n".join(f"  {p}" for p in ambiguous)
        )

    assert not errors, "\n\n".join(errors)


def test_whole_tree_synthetic_undeclared_path_is_flagged():
    """Synthetic RED-detector: a new tracked file in an undeclared location must fail.

    Injects a fake path ``newfeature/foo.py`` into the git-ls-files result and
    asserts the whole-tree gate would flag it as unaccounted.  Verifies the gate
    is non-vacuous — it cannot be trivially satisfied by a path that isn't in
    any bucket.
    """
    manifest = _load_manifest()
    tiered_paths = _manifest_path_set(manifest)
    excluded_paths = _excluded_path_set(manifest)
    repo_only_paths = _repo_only_path_set(manifest)
    build_inputs_paths = _build_inputs_path_set(manifest)

    fake_path = "newfeature/foo.py"

    # Confirm the fake path is not covered by any bucket (it shouldn't be)
    assert not _path_is_covered(fake_path, tiered_paths), (
        f"Test setup error: {fake_path!r} unexpectedly covered by mechanisms[]"
    )
    assert not _path_is_covered(fake_path, excluded_paths), (
        f"Test setup error: {fake_path!r} unexpectedly covered by excluded[]"
    )
    assert not _path_is_covered(fake_path, repo_only_paths), (
        f"Test setup error: {fake_path!r} unexpectedly covered by repo_only[]"
    )
    assert not _path_is_covered(fake_path, build_inputs_paths), (
        f"Test setup error: {fake_path!r} unexpectedly covered by build_inputs[]"
    )

    # Inject the fake path into the real tracked set and run the gate's
    # accounting logic over it — the fake must come out unaccounted.
    real_tracked = _enumerate_all_tracked_paths()
    injected = real_tracked | {fake_path}

    unaccounted: list[str] = []
    for p in sorted(injected):
        in_tiered = _path_is_covered(p, tiered_paths)
        in_excluded = _path_is_covered(p, excluded_paths)
        in_repo_only = _path_is_covered(p, repo_only_paths)
        in_build_inputs = _path_is_covered(p, build_inputs_paths)
        if not (in_tiered or in_excluded or in_repo_only or in_build_inputs):
            unaccounted.append(p)

    assert fake_path in unaccounted, (
        f"RED-detector failure: {fake_path!r} was NOT flagged as unaccounted. "
        "The whole-tree gate is not detecting undeclared paths."
    )


def test_no_dangling_manifest_entries():
    """Every manifest entry must correspond to a path that actually exists in the repo.

    Fails naming the specific dangling path(s). This catches the inverse drift:
    a mechanism was removed/renamed but the manifest entry wasn't cleaned up.

    For entries with a "source" field (repo-root-relative override), the source
    path is checked instead of source_root + path.
    """
    manifest = _load_manifest()
    source_root = manifest.get("source_root", ".")
    dangling: list[str] = []
    for entry in manifest["mechanisms"]:
        p = entry["path"]
        if "source" in entry:
            # source field is repo-root-relative; check it directly
            full = os.path.join(REPO_ROOT, entry["source"])
        else:
            full = os.path.join(REPO_ROOT, source_root, p)
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
    """The 4 identity-coupled hooks from spec #778 must all be tagged identity_coupled=true.

    Ensures the tag-once gate covers the load-bearing set.
    (engram-github-notifications-hook.py removed — hook deleted, low-utility.)
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
        "hooks/claude/engram-end-of-day-hook.py",
    }
    missing = expected - ic_paths
    assert not missing, (
        "Expected identity_coupled=true on these hooks per spec #778, but it is missing:\n"
        + "\n".join(f"  {p}" for p in sorted(missing))
    )


def _resolve_import_candidates(node: "ast.Import | ast.ImportFrom", importer_path: str) -> list[str]:
    """Resolve an import AST node to candidate mechanism paths.

    Subdirectory-aware (engram-alpha#2549): a DOTTED module name is walked
    into a nested path (``a.b.c`` → ``a/b/c.py``), not just resolved as a
    same-directory single-segment name — this is what closes the gap where
    an import crossing INTO a subpackage from a sibling file (``import
    health.engine``, ``from health.engine import X``) could never resolve
    to ``health/engine.py`` under the old same-directory-only logic.

    Only checks top-level module-scope imports in Python mechanism files.
    Stdlib and external packages are automatically excluded (they won't
    resolve to any mechanism entry).

    Level-aware for ``ast.ImportFrom`` (the old code silently ignored
    ``node.level`` entirely, which happened to still work for the common
    single-level relative case by accident): a plain ``ast.Import`` is
    ALWAYS absolute (Python has no relative form for it), so it resolves
    from the manifest's source root regardless of the importer's own
    location. An ``ast.ImportFrom`` with ``level == 0`` is likewise
    absolute (resolved from source root); with ``level >= 1`` it's
    explicitly relative, resolved within the importer's own package
    directory — ``level=1`` is "this package" (the importer's own
    dirname), each additional level strips one more trailing directory
    component, mirroring Python's own relative-import semantics.

    ``from X import Y`` — TWO candidates per imported name, not one, because
    the AST alone cannot tell whether ``Y`` is a plain attribute defined in
    ``X`` (module ``X`` is the real target — e.g. ``from health.curves
    import CurveSpec``) or ``Y`` is itself a SUBMODULE of package ``X``
    (``X/Y`` is the real target — e.g. ``from health import engine`` when
    ``engine`` is ``health/engine.py``, not an attribute of
    ``health/__init__.py``). Caught by this function's own test suite: an
    earlier version of this fix resolved only the module-level candidate
    and silently missed the ``from health import engine`` entry-point
    spelling, one of the three shapes engram-alpha#2549 was filed to fix.
    Both candidates are always emitted (module-level AND
    module/name-as-submodule); a wrong guess simply fails to match any real
    mechanism entry in ``path_to_entry`` and is silently ignored — same
    mechanism that already excludes stdlib/external imports today, so this
    costs nothing when the guess is wrong. This also incidentally resolves
    ``from . import x`` (``node.module is None``, no module-level candidate
    possible) via the submodule-per-name candidate alone.

    ALSO same-directory, for absolute imports (``level == 0`` ``ImportFrom``,
    and every plain ``ast.Import``) — caught by reviewer-fairy round 1 on
    this fix (engram-alpha#2550): this repo extensively uses its OWN
    `sys.path.insert(0, dirname(__file__))` + bare-sibling-import
    convention in ``tools/`` and ``hooks/claude/`` (e.g. ``tools/baton.py``
    doing ``from forum_api import (...)`` — a level-0 ``ImportFrom`` — where
    ``forum_api.py`` lives in the SAME ``tools/`` directory as
    ``baton.py``, resolvable only because that convention puts the
    importer's own directory on `sys.path` at runtime, not because
    `forum_api` sits at the manifest source root). Treating ``level ==
    0``/bare `import` as "always resolve from source root, never
    same-directory" is correct for a genuine absolute import but silently
    dropped same-directory resolution for every file using this repo's own
    sibling-import convention — measured on the real tree as 20 real
    import statements across 16 files that the pre-fix resolver checked
    and a source-root-only resolver would silently stop checking. So a
    same-directory candidate is ALWAYS added alongside the source-root
    candidate for every absolute import, same "wrong guess costs nothing"
    reasoning as the X/X.Y case above — one of the two will usually be
    spurious and fail to match `path_to_entry`, but which one is spurious
    depends on which convention the importing file actually uses, and this
    function has no way to know that up front.

    Known dormant residual (round-2 reviewer-fairy, engram-alpha#2550): the
    same-directory candidate above is only generated for the MODULE-level
    candidate of a ``from X import Y``, never for the submodule-per-name
    candidate — i.e. for ``level == 0``, ``{importer_dir}/{X}/{Y}.py`` is
    never emitted, only ``{source_root}/{X}/{Y}.py``. So a hypothetical
    ``from X import Y`` where ``Y`` is a submodule reachable only via this
    repo's same-directory sys.path convention (rather than from the source
    root) would not resolve. Confirmed dormant, not a live false negative:
    a full scan of the real manifest found zero instances of this shape.
    Left unfixed deliberately (no real case to regression-test against);
    revisit if a future import actually needs it.

    History (engram-alpha#2549, 2026-09-04): until this fix, the resolver
    ignored ``.level`` entirely and had no subdirectory-path notion at all
    — an import crossing INTO a subpackage from a sibling file
    (``from health import engine``, ``import health.engine``, ``from
    health.engine import X``) could never resolve under any spelling, so
    the tier-closure guarantee wasn't enforced at a subpackage's entry
    point. Both gaps documented in detail in PR #2548 (now historical —
    see that PR's diff for the full original write-up) are closed here.
    """
    import ast

    candidates: list[str] = []
    importer_dir = os.path.dirname(importer_path)

    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        module_as_path = module.replace(".", "/") if module else ""

        if node.level == 0:
            base_dir = ""
        else:
            dir_part = importer_dir
            for _ in range(node.level - 1):
                dir_part = os.path.dirname(dir_part)
            base_dir = dir_part

        if module_as_path:
            module_path = f"{base_dir}/{module_as_path}" if base_dir else module_as_path
            candidates.append(f"{module_path}.py")
            submodule_base = module_path
            if node.level == 0 and importer_dir:
                # Also try same-directory resolution for this absolute
                # import — see the docstring's "ALSO same-directory" note.
                candidates.append(f"{importer_dir}/{module_as_path}.py")
        else:
            # "from . import x" (or "from .. import x", etc.) — no module
            # name at all; every imported name can only be a submodule of
            # the resolved relative directory.
            submodule_base = base_dir

        for alias in node.names:
            candidates.append(f"{submodule_base}/{alias.name}.py" if submodule_base else f"{alias.name}.py")
    else:  # ast.Import — always absolute, no "level" attribute exists on this node type
        for alias in node.names:
            module_as_path = alias.name.replace(".", "/")
            candidates.append(f"{module_as_path}.py")
            if importer_dir:
                candidates.append(f"{importer_dir}/{module_as_path}.py")

    return candidates


def _find_tier_violations(mechanisms: list[dict], src_root: str) -> list[str]:
    """Scan every Python mechanism's top-level imports and return a list of
    tier/multi_agent inconsistency violation strings (empty = clean).

    Factored out of ``test_import_dep_tier_consistency`` (engram-alpha#2549)
    so a synthetic-manifest falsifier test can call the exact same scan
    logic the real gate uses, over a fake mechanisms list + fake files,
    rather than re-implementing (and risking silently drifting from) the
    real detection logic.
    """
    import ast

    path_to_entry = {e["path"]: e for e in mechanisms}
    violations: list[str] = []

    for entry in mechanisms:
        path = entry["path"]
        if not path.endswith(".py"):
            continue
        full_path = os.path.join(src_root, path)
        if not os.path.isfile(full_path):
            continue

        try:
            with open(full_path, encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=full_path)
        except (SyntaxError, OSError):
            continue

        # Collect only top-level module-scope imports (direct Module children)
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue

            module_names = _resolve_import_candidates(node, path)

            for importee_path in module_names:
                if importee_path not in path_to_entry:
                    continue  # not a mechanism — skip (stdlib, external, or not tracked)

                importee = path_to_entry[importee_path]
                importer_rank = TIER_RANKS.get(entry.get("tier", "dev"), 2)
                importee_rank = TIER_RANKS.get(importee.get("tier", "dev"), 2)

                # Violation 1: importee has a HIGHER tier rank than importer
                # (importee ships at a more restrictive threshold)
                if importee_rank > importer_rank:
                    violations.append(
                        f"TIER: {path!r} (tier={entry.get('tier')}) imports "
                        f"{importee_path!r} (tier={importee.get('tier')}) — "
                        f"importee ships at a higher tier; importer will ship without it"
                    )

                # Violation 2: importee is multi_agent-only but importer ships single-agent
                if importee.get("multi_agent") is True and entry.get("multi_agent") is not True:
                    violations.append(
                        f"MULTI_AGENT: {path!r} (multi_agent not set → ships single-agent) "
                        f"imports {importee_path!r} (multi_agent=True → single-agent build drops it)"
                    )

    return violations


def test_import_dep_tier_consistency():
    """If A ships at (tier, topology) T and imports B, B must also ship at T.

    Prevents the #1087/#1088 class: an importer shipping in contexts where
    the importee doesn't, causing ImportError at runtime.

    Only checks top-level module-scope imports in Python mechanism files.
    Resolution (engram-alpha#2549) is subdirectory- and level-aware — see
    ``_resolve_import_candidates``'s own docstring for the exact rules and
    its one known-dormant residual gap (a same-directory ``from X import Y``
    submodule case, confirmed to have zero live instances on the real
    manifest). Stdlib and external packages are automatically excluded
    (they won't resolve to any mechanism entry).
    """
    manifest = _load_manifest()
    mechanisms = manifest["mechanisms"]
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))

    violations = _find_tier_violations(mechanisms, src_root)

    assert not violations, (
        f"Import-dep tier/flag inconsistencies found ({len(violations)}):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Telemetry manifest gate (issue #2574, workstream WS2 — §4a)
#
# See docs/2574-telemetry-design.md §1 (PARTICIPATION DEFINITION) and §4a
# (the registry) for the design. tools/telemetry_manifest_gate.py holds the
# extraction/walk/scan logic these tests exercise; see its own module
# docstring and `--self-test` for the synthetic positive/negative fixtures
# that pin the extraction logic itself (independent of these pytest tests,
# which focus on the MANIFEST-facing contract).
# ---------------------------------------------------------------------------


def test_telemetry_key_required_on_emitting_mechanisms():
    """Any manifest entry whose path is a hook or tool that CAN emit must
    carry a `telemetry` key — either `telemetry: []` (declaring, explicitly,
    that it emits from nowhere) or one record per call site.

    The required-declaration population is MECHANICAL (AST-derived from the
    real source tree), not a hand-maintained list — a newly added emit call
    automatically enters this test's population, same discipline as
    ``test_every_shippable_path_is_tagged``'s filesystem-derived candidate
    enumeration above.
    """
    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))
    sites = telemetry_gate.find_emit_call_sites(src_root)
    emitting_paths = telemetry_gate.emitting_mechanism_paths(manifest, src_root, sites)

    path_to_entry = {e["path"]: e for e in manifest["mechanisms"]}
    missing = sorted(p for p in emitting_paths if "telemetry" not in path_to_entry.get(p, {}))

    assert not missing, (
        "packaging/tiers.json is missing a `telemetry` key for these "
        "emitting mechanism paths (add `telemetry: []` or per-call-site "
        "records — see docs/2574-telemetry-design.md §4a):\n"
        + "\n".join(f"  {p}" for p in missing)
    )


def test_telemetry_gate_clean_on_real_tree():
    """The §4a gate (NOT_WIRED / UNDECLARED / unresolved dynamic-dispatch)
    must report clean on the real manifest + real source tree.

    Today L = M = ∅ everywhere (no `site=` kwarg exists in source yet — that
    is WS3's job), so NOT_WIRED and UNDECLARED are trivially empty; this test
    would catch either set becoming non-empty (WS3 wiring a site whose id
    doesn't match a manifest record, or vice versa) as well as a REGRESSION
    in the dynamic-dispatch exclusion set (a new dynamic-dispatch call site
    appearing in the tree with no matching declared exclusion).
    """
    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))
    report = telemetry_gate.run_gate(src_root, manifest)

    assert not report.not_wired, f"NOT WIRED (manifest declares, source never emits): {sorted(report.not_wired)}"
    assert not report.undeclared, f"UNDECLARED (source emits, manifest has no record): {sorted(report.undeclared)}"
    assert not report.unresolved_dynamic_hits, (
        "Unresolved dynamic-dispatch hits (declare an exclusion with a reason "
        "in packaging/tiers.json's telemetry.dynamic_dispatch_exclusions, or "
        "show the backward walk cannot reach a declared site):\n"
        + "\n".join(f"  {h.file}:{h.lineno} (shape {h.shape}) — {h.snippet}" for h in report.unresolved_dynamic_hits)
    )


def test_telemetry_participation_static_not_wired_control():
    """§9 test 2 — the participation/static test ('round-6 correction').

    Add a manifest site record whose literal id has NO matching
    `emit(..., site=...)` call anywhere in source; assert the §4a gate
    reports it under NOT_WIRED (M - L). Purely static — no window is run
    and no trigger is fired, per the design's own bound: conflating this
    with a runtime test made an earlier draft's stored observables
    identical to the time-axis test's, which made the design unfalsifiable
    exactly where it claimed to be falsifiable (§9 note on test 2).

    Uses synthetic manifest data over the REAL source tree's L (which is
    ∅ today) rather than mutating the real manifest file.
    """
    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))
    sites = telemetry_gate.find_emit_call_sites(src_root)
    L = telemetry_gate.compute_L(sites)

    phantom_manifest = {
        "mechanisms": [
            {
                "path": "hooks/claude/engram-postcompact-hook.py",
                "telemetry": [
                    {"site": "phantom.site.never_emitted", "trigger": "PostCompact"},
                ],
            },
        ],
    }
    M = telemetry_gate.compute_M(phantom_manifest)

    nw = telemetry_gate.not_wired(L, M)
    assert nw == {"phantom.site.never_emitted"}, (
        f"expected NOT_WIRED == {{'phantom.site.never_emitted'}}, got {nw}"
    )


def test_telemetry_manifest_gate_test_missing_telemetry_key():
    """§9 test 3 — the manifest gate test.

    Add a hook path with no `telemetry` key at all; assert the manifest-gate
    logic (the same logic ``test_telemetry_key_required_on_emitting_mechanisms``
    runs on the real manifest) fails, naming the offending path.

    Built as a synthetic fixture tree + synthetic manifest (same pattern as
    ``test_whole_tree_synthetic_undeclared_path_is_flagged`` above) rather
    than mutating the real manifest.
    """
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-test3-") as tmp:
        hook_path = os.path.join(tmp, "hooks", "claude", "fake-emitting-hook.py")
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(
                "def emit_if_initialized(event_type, level, data, site=None):\n"
                "    pass\n\n"
                "def main():\n"
                '    emit_if_initialized("engram.fake.fired", 1, {})\n\n'
                'if __name__ == "__main__":\n'
                "    main()\n"
            )

        fake_manifest = {
            "mechanisms": [
                {"path": "hooks/claude/fake-emitting-hook.py"},  # no "telemetry" key — the defect
            ],
        }

        sites = telemetry_gate.find_emit_call_sites(tmp)
        emitting_paths = telemetry_gate.emitting_mechanism_paths(fake_manifest, tmp, sites)
        path_to_entry = {e["path"]: e for e in fake_manifest["mechanisms"]}
        missing = sorted(p for p in emitting_paths if "telemetry" not in path_to_entry.get(p, {}))

        assert missing == ["hooks/claude/fake-emitting-hook.py"], (
            f"expected the manifest gate to flag the missing telemetry key, got missing={missing}"
        )


def test_telemetry_dynamic_dispatch_discriminating_control():
    """§9 test 3b's SECOND control — 'on the gate rather than the runtime'.

    The discriminating case (round 11's correction) is a site with ONE
    DIRECT caller (a real dispatch target) PLUS one dynamic caller elsewhere
    in the tree — NOT a site reachable only through a dynamic call, which
    reddens via the zero-target disjunct alone and never exercises the
    dynamic-dispatch-scan clause specifically (round 10's non-discriminating
    version). With one direct caller the walk returns a NON-EMPTY target
    set, so only the static dynamic-dispatch scan can fail this fixture.

    Uses a synthetic fixture tree (mirrors
    ``telemetry_manifest_gate.py``'s own ``--self-test`` discriminating
    fixture, exposed here as a first-class pytest test per the design's
    §9/§10 assignment of this control to WS2).
    """
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-test3b-") as tmp:
        with open(os.path.join(tmp, "disc_impl.py"), "w", encoding="utf-8") as f:
            f.write(
                "def emit_if_initialized(event_type, level, data, site=None):\n"
                "    pass\n\n"
                "def _disc_impl():\n"
                '    emit_if_initialized("engram.disc.fired", 1, {}, site="engram.disc.fired")\n'
            )
        with open(os.path.join(tmp, "disc_server.py"), "w", encoding="utf-8") as f:
            f.write(
                "import disc_impl as _impl_mod\n\n"
                "class mcp:\n"
                "    @staticmethod\n"
                "    def tool():\n"
                "        def deco(f):\n"
                "            return f\n"
                "        return deco\n\n"
                "@mcp.tool()\n"
                "def disc_tool():\n"
                "    return _impl_mod._disc_impl()\n"
            )
        with open(os.path.join(tmp, "disc_dynamic_caller.py"), "w", encoding="utf-8") as f:
            f.write(
                "def maybe_call_it():\n"
                "    table = {'x': None}\n"
                "    table['x']()\n"
            )

        graph = telemetry_gate.build_call_graph(tmp)
        sites = telemetry_gate.find_emit_call_sites(tmp)
        site = next(s for s in sites if s.file == "disc_impl.py")
        found = telemetry_gate.derive_dispatch_targets(graph, telemetry_gate.enclosing_key_for_site(site))

        # The direct-caller path IS resolved -- the zero-target disjunct
        # must stay silent, so this control actually discriminates.
        assert found == {("disc_server.py", "disc_tool")}, (
            f"expected a NON-EMPTY dispatch-target set (the discriminating "
            f"precondition), got {found}"
        )

        manifest = {
            "mechanisms": [
                {
                    "path": "disc_impl.py",
                    "telemetry": [{"site": "engram.disc.fired", "trigger": "MCP tool body"}],
                },
            ],
        }
        report = telemetry_gate.run_gate(tmp, manifest)

        # The gate must still FAIL CI -- on the unresolved dynamic-dispatch
        # hit alone, since NOT_WIRED/UNDECLARED are clean and the target set
        # is non-empty.
        assert not report.not_wired
        assert not report.undeclared
        assert report.unresolved_dynamic_hits, (
            "expected an UNRESOLVED dynamic-dispatch hit in "
            "disc_dynamic_caller.py; finding none means the static-scan "
            "clause is silently non-discriminating (the round-10 defect)."
        )
        assert not telemetry_gate.check_ok(report), (
            "expected check_ok() == False (unresolved dynamic-dispatch hit "
            "must fail CI even though the target set is non-empty)"
        )


# ---------------------------------------------------------------------------
# Reached-literal <-> derived-dispatch-target join-key check (issue #3136,
# WS7). §1: "The fix is to DERIVE it" gives R its join key; these tests
# exercise the STATIC half of that join, added to the gate by WS7 --
# tools/telemetry_manifest_gate.py's find_reached_literals /
# derived_target_ids_over_declared_sites / reached_literal_mismatches.
# ---------------------------------------------------------------------------


def test_dispatch_target_id_matches_real_manifest_id_literal():
    """`dispatch_target_id` (issue #3136's promoted-public rename of this
    module's former private `_format_key`) must produce EXACTLY the literal
    shape every real reached-write call carries in source -- verified here
    against engram-surface-hook.py's own `_MAIN_MANIFEST_ID` constant, the
    example the issue itself cites."""
    assert telemetry_gate.dispatch_target_id(
        ("hooks/claude/engram-surface-hook.py", "main")
    ) == "hooks/claude/engram-surface-hook.py:main"


def test_reached_join_key_clean_on_real_tree():
    """The real manifest + real source tree must satisfy the reached
    join-key check in BOTH directions: every hand-written `reached` literal
    (each hook's `_MAIN_MANIFEST_ID`, every server.py inline literal) must
    equal, as a set, the union of every declared site's own derived
    dispatch-target set."""
    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))
    report = telemetry_gate.run_gate(src_root, manifest)

    assert not report.reached_not_derived, (
        f"REACHED_NOT_DERIVED (a `reached` literal in source names no "
        f"derived dispatch target -- typo'd or orphaned): {sorted(report.reached_not_derived)}"
    )
    assert not report.derived_not_reached, (
        f"DERIVED_NOT_REACHED (a declared site's derived dispatch target has "
        f"no matching `reached` literal in source): {sorted(report.derived_not_reached)}"
    )
    assert telemetry_gate.check_ok(report), (
        "expected check_ok()==True on the real tree with a clean reached "
        f"join-key check; not_wired={report.not_wired} undeclared={report.undeclared} "
        f"unresolved_dynamic_hits={report.unresolved_dynamic_hits} "
        f"reached_not_derived={report.reached_not_derived} "
        f"derived_not_reached={report.derived_not_reached}"
    )


def test_reached_join_key_typo_control():
    """Issue #3136's own contract test: plant a `reached` literal with a
    typo (it does not match its own site's derived dispatch target), and
    assert the static check fails NAMING it in REACHED_NOT_DERIVED."""
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-join-typo-") as tmp:
        with open(os.path.join(tmp, "join1_impl.py"), "w", encoding="utf-8") as f:
            f.write(
                "def emit_if_initialized(event_type, level, data, site=None):\n"
                "    pass\n\n"
                "def _join1_impl():\n"
                '    emit_if_initialized("engram.join1.fired", 1, {}, site="join1.fired")\n'
            )
        with open(os.path.join(tmp, "join1_server.py"), "w", encoding="utf-8") as f:
            f.write(
                "import join1_impl as _impl_mod\n\n"
                "def write_telemetry_record(manifest_id, event_type, fields=None):\n"
                "    pass\n\n"
                "class mcp:\n"
                "    @staticmethod\n"
                "    def tool():\n"
                "        def deco(f):\n"
                "            return f\n"
                "        return deco\n\n"
                "@mcp.tool()\n"
                "def join1_tool():\n"
                '    write_telemetry_record(manifest_id="join1_server.py:join1_toolTYPO", '
                'event_type="engram.telemetry.reached", fields={})\n'
                "    return _impl_mod._join1_impl()\n"
            )

        manifest = {
            "mechanisms": [
                {"path": "join1_impl.py", "telemetry": [{"site": "join1.fired", "trigger": "MCP tool body"}]},
            ],
        }
        report = telemetry_gate.run_gate(tmp, manifest)

        assert not report.not_wired
        assert not report.undeclared
        assert "join1_server.py:join1_toolTYPO" in report.reached_not_derived, (
            f"expected the typo'd literal named in REACHED_NOT_DERIVED, got {report.reached_not_derived}"
        )
        assert not telemetry_gate.check_ok(report), (
            "expected check_ok() == False on a typo'd reached literal"
        )


def test_reached_join_key_missing_control():
    """Issue #3136's own contract test: plant a declared site whose derived
    dispatch target has NO reached literal at all, and assert the static
    check fails 'the other way' -- DERIVED_NOT_REACHED names the target,
    while REACHED_NOT_DERIVED stays empty (isolating the OTHER direction
    from the typo control above)."""
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-join-missing-") as tmp:
        with open(os.path.join(tmp, "join2_impl.py"), "w", encoding="utf-8") as f:
            f.write(
                "def emit_if_initialized(event_type, level, data, site=None):\n"
                "    pass\n\n"
                "def _join2_impl():\n"
                '    emit_if_initialized("engram.join2.fired", 1, {}, site="join2.fired")\n'
            )
        with open(os.path.join(tmp, "join2_server.py"), "w", encoding="utf-8") as f:
            f.write(
                "import join2_impl as _impl_mod\n\n"
                "class mcp:\n"
                "    @staticmethod\n"
                "    def tool():\n"
                "        def deco(f):\n"
                "            return f\n"
                "        return deco\n\n"
                "@mcp.tool()\n"
                "def join2_tool():\n"
                "    return _impl_mod._join2_impl()\n"
            )

        manifest = {
            "mechanisms": [
                {"path": "join2_impl.py", "telemetry": [{"site": "join2.fired", "trigger": "MCP tool body"}]},
            ],
        }
        report = telemetry_gate.run_gate(tmp, manifest)

        assert not report.not_wired
        assert not report.undeclared
        assert not report.reached_not_derived, (
            f"expected REACHED_NOT_DERIVED empty (no reached literal exists "
            f"at all in this fixture), got {report.reached_not_derived}"
        )
        assert "join2_server.py:join2_tool" in report.derived_not_reached, (
            f"expected 'join2_server.py:join2_tool' named in DERIVED_NOT_REACHED, "
            f"got {report.derived_not_reached}"
        )
        assert not telemetry_gate.check_ok(report), (
            "expected check_ok() == False on a declared site with no reached literal"
        )


# ---------------------------------------------------------------------------
# Dispatch map builder + drift gate (issue #3136 D1 fix, "REVISION
# 2026-09-27"): the backward call-graph walk moved from a runtime import
# (broken on a deployed, flattened plugin install) to a build-time-ish walk
# whose result ships as the committed src/engram/telemetry_targets.json.
# `build_dispatch_map` is the walk; the drift-gate test below is what keeps
# the committed file from silently going stale.
# ---------------------------------------------------------------------------

SRC_ENGRAM = os.path.join(REPO_ROOT, "src", "engram")
TELEMETRY_TARGETS_PATH = os.path.join(SRC_ENGRAM, "telemetry_targets.json")


def test_build_dispatch_map_shape_and_correctness_on_real_tree():
    """`build_dispatch_map` must return one key per DECLARED site (every
    site literal in compute_M(manifest)), each value the SORTED list of
    that site's own derived-target-id strings -- matching, site-for-site,
    what `run_gate`'s own per-site `dispatch_targets` computes (just
    captured as JSON-serializable data instead of a set of (file, name)
    tuples)."""
    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))

    dispatch_map = telemetry_gate.build_dispatch_map(src_root, manifest)

    assert dispatch_map["schema"] == 1
    assert dispatch_map["degraded_threshold"] == manifest["telemetry"]["degraded_threshold"]

    report = telemetry_gate.run_gate(src_root, manifest)
    M = telemetry_gate.compute_M(manifest)
    assert set(dispatch_map["sites"].keys()) == M, (
        "expected one dispatch-map entry per declared site literal"
    )

    sites = telemetry_gate.find_emit_call_sites(src_root)
    for site_literal in M:
        expected_ids = sorted(
            telemetry_gate.dispatch_target_id(t)
            for key, targets in report.dispatch_targets.items()
            for t in targets
            if any(s.key == key and s.site_literal == site_literal for s in telemetry_gate.declarable_sites(sites))
        )
        assert dispatch_map["sites"][site_literal] == expected_ids, (
            f"site {site_literal!r}: dispatch map says {dispatch_map['sites'][site_literal]}, "
            f"run_gate's own per-site walk says {expected_ids}"
        )


def test_build_dispatch_map_raises_on_missing_degraded_threshold():
    """Issue #3136 D5: a missing `telemetry.degraded_threshold` key must be
    a LOUD failure at map-generation time, never a silent runtime default."""
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-map-thr-") as tmp:
        manifest_no_thr = {"mechanisms": [], "telemetry": {}}
        with pytest.raises(KeyError):
            telemetry_gate.build_dispatch_map(tmp, manifest_no_thr)

        manifest_no_telemetry_key = {"mechanisms": []}
        with pytest.raises(KeyError):
            telemetry_gate.build_dispatch_map(tmp, manifest_no_telemetry_key)


def test_emit_dispatch_map_writes_matching_json(tmp_path):
    """`emit_dispatch_map` writes exactly what `build_dispatch_map` returns
    (round-tripped through JSON)."""
    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))
    out_path = tmp_path / "emitted_map.json"

    built = telemetry_gate.build_dispatch_map(src_root, manifest)
    returned = telemetry_gate.emit_dispatch_map(str(out_path), src_root, manifest)
    assert returned == built

    with open(out_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == built


def test_committed_telemetry_targets_json_matches_fresh_walk():
    """THE DRIFT GATE (issue #3136 D1 item 3 / "REVISION 2026-09-27" item
    3): the committed `src/engram/telemetry_targets.json` must equal a
    FRESH walk of the current tree (`build_dispatch_map`), naming any
    differing sites. Same default-deny discipline as §4a's own
    NOT_WIRED/UNDECLARED check -- a PR that changes a call path must show
    the map change in its own diff, or this test fails.
    """
    assert os.path.isfile(TELEMETRY_TARGETS_PATH), (
        f"src/engram/telemetry_targets.json does not exist -- generate it via "
        f"`python3 tools/telemetry_manifest_gate.py --emit-map {TELEMETRY_TARGETS_PATH}` "
        f"from the repo root."
    )
    with open(TELEMETRY_TARGETS_PATH, encoding="utf-8") as f:
        committed = json.load(f)

    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))
    fresh = telemetry_gate.build_dispatch_map(src_root, manifest)

    assert committed == fresh, _describe_dispatch_map_drift(committed, fresh)


def _describe_dispatch_map_drift(committed: dict, fresh: dict) -> str:
    """Name the differing sites for a failed drift-gate assertion (rather
    than dumping two full dicts) -- both top-level scalar drift
    (schema/degraded_threshold) and per-site drift are named explicitly."""
    lines = ["committed telemetry_targets.json has drifted from a fresh walk:"]
    if committed.get("schema") != fresh.get("schema"):
        lines.append(f"  schema: committed={committed.get('schema')!r} fresh={fresh.get('schema')!r}")
    if committed.get("degraded_threshold") != fresh.get("degraded_threshold"):
        lines.append(
            f"  degraded_threshold: committed={committed.get('degraded_threshold')!r} "
            f"fresh={fresh.get('degraded_threshold')!r}"
        )
    committed_sites = committed.get("sites", {})
    fresh_sites = fresh.get("sites", {})
    for site in sorted(set(committed_sites) | set(fresh_sites)):
        if committed_sites.get(site) != fresh_sites.get(site):
            lines.append(
                f"  site {site!r}: committed={committed_sites.get(site)!r} fresh={fresh_sites.get(site)!r}"
            )
    return "\n".join(lines)


def test_committed_telemetry_targets_json_drift_gate_mutation_control():
    """Can-it-fail control for the drift gate above: mutate ONE target id in
    a COPY of the committed map (never the real file) and confirm the SAME
    comparison the drift gate runs fails, naming that site -- then the
    unmutated re-comparison passes, proving the mutation (not some other
    difference) was what failed it."""
    with open(TELEMETRY_TARGETS_PATH, encoding="utf-8") as f:
        committed = json.load(f)

    manifest = _load_manifest()
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))
    fresh = telemetry_gate.build_dispatch_map(src_root, manifest)

    assert committed == fresh, "precondition: the real committed file must already match a fresh walk"

    mutated = json.loads(json.dumps(committed))  # deep copy
    some_site = next(s for s, ids in mutated["sites"].items() if ids)
    mutated["sites"][some_site] = ["totally-bogus-mutated-target-id"]

    assert mutated != fresh, "mutation control: the mutated copy must differ from the fresh walk"
    drift_msg = _describe_dispatch_map_drift(mutated, fresh)
    assert some_site in drift_msg, (
        f"expected the mutated site {some_site!r} named in the drift message, got:\n{drift_msg}"
    )

    # Revert (redundant given `mutated` was a deep copy of `committed`, not
    # the file itself -- asserted explicitly so a future refactor that
    # accidentally starts mutating the real file in place is caught here).
    assert committed == fresh, "the real committed file must be untouched by this mutation control"
