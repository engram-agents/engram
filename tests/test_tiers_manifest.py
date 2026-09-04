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

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")
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
    for fname in [
        "server.py",
        "bootstrap.py",
        "launch-engram-server.sh",
        "viz_server.py",
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


def test_import_dep_tier_consistency():
    """If A ships at (tier, topology) T and imports B, B must also ship at T.

    Prevents the #1087/#1088 class: an importer shipping in contexts where
    the importee doesn't, causing ImportError at runtime.

    Only checks top-level module-scope imports in Python mechanism files.
    Resolves module names to mechanism paths by checking the same directory
    as the importing file (the pattern used by the scripts in this repo).
    Stdlib and external packages are automatically excluded (they won't
    resolve to any mechanism entry).

    TWO KNOWN GAPS (engram-alpha#2537, health/ package-layout discussion,
    2026-09-04 — measured, not theoretical; both verified against this
    function's own resolution code, both load-bearing for any future
    package-shaped subdirectory, not specific to health/):

    1. This resolver reads ``ast.ImportFrom.module`` only — it never reads
       ``.level``, the attribute that actually carries a relative import's
       leading dot(s). So a single-level explicit relative import
       (``from .curves import CurveSpec``, ``module='curves'``, ``level=1``)
       is textually indistinguishable here from a same-directory absolute
       import, and stays checked — while a dotted absolute import of the
       identical target (``from health.curves import CurveSpec``,
       ``module='health.curves'``) fails the ``"." not in module`` gate and
       is silently skipped. This is why a package under this repo can keep
       its cross-module imports checked by this function, but ONLY if it
       uses explicit relative imports for same-directory siblings — a
       dotted-absolute style loses coverage with no warning. If this
       function is ever made ``.level``-aware (a reasonable-looking fix, since
       ignoring ``level`` also means a genuinely cross-package relative
       import misresolves as same-directory) — that change must re-establish
       coverage for every subdirectory currently depending on this gap, in
       the same commit, or their imports go dark silently.

    2. This resolver has NO subdirectory-path notion at all — candidate
       paths are always ``{importer's own dirname}/{single-segment module
       name}.py``. So an import that crosses INTO a subpackage from a
       SIBLING file — e.g. a root file doing ``from health import engine``,
       ``import health.engine``, or ``from health.engine import X`` — can
       never resolve to ``health/engine.py`` under ANY spelling: the first
       two resolve to a nonexistent ``health.py`` (silently unmatched, since
       ``health`` is a directory, not a file); the third fails the dot gate
       outright. **This means the tier-closure guarantee this function
       exists to provide is NOT enforced at a subpackage's entry point,
       regardless of the importing file's own style** — an essential-tier
       file importing a dev-tier module from inside a mis-tiered subpackage
       would break at runtime on a convenience/essential-only install with
       nothing here catching it. Fixing this needs real subdirectory-aware
       resolution (walking a dotted module name into a nested candidate
       path), not a one-line patch; tracked separately rather than folded
       into this docstring note — see engram-alpha#2549.
    """
    import ast

    manifest = _load_manifest()
    mechanisms = manifest["mechanisms"]
    src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))

    # Build path→entry lookup for Python files
    path_to_entry = {e["path"]: e for e in mechanisms}

    violations = []

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

            # Resolve the module name(s) to potential mechanism paths
            module_names = []
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # Only handle simple names (no dots) — same-dir resolution
                if "." not in module and module:
                    dir_part = os.path.dirname(path)
                    candidate = f"{dir_part}/{module}.py" if dir_part else f"{module}.py"
                    module_names.append(candidate)
            else:  # ast.Import
                for alias in node.names:
                    name = alias.name.split(".")[0]  # top-level package only
                    dir_part = os.path.dirname(path)
                    candidate = f"{dir_part}/{name}.py" if dir_part else f"{name}.py"
                    module_names.append(candidate)

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

    assert not violations, (
        f"Import-dep tier/flag inconsistencies found ({len(violations)}):\n"
        + "\n".join(f"  {v}" for v in violations)
    )
