"""tools.engine.build — build transforms for the ENGRAM plugin tree.

Port of tools/build-plugin.sh semantics into testable Python functions.
Pure Python 3 stdlib; ZERO imports from ENGRAM runtime modules.

Each section mirrors the corresponding numbered section in build-plugin.sh.
Comments referencing bash line numbers are included where the mapping is
non-obvious.

REPLICATION NOTES (bash oddities / edge cases faithfully ported):
  - The tools/ copy loop in build-plugin.sh iterates SHIPPED_PATHS that match
    '^tools/', and for each entry, if the source is a directory it does
    ``cp -r src/. dest/`` (copy contents, not the dir itself).  Ported exactly
    using shutil.copytree(dirs_exist_ok=True).
  - templates/ uses ``cp -a`` (archive mode, preserving permissions/timestamps)
    while tools/ uses ``cp -r``.  Python's shutil.copytree does not replicate
    mtime/owner, so both are ported as copytree with the same semantics — any
    timestamp difference between legacy and engine is a nondeterminism that the
    golden test normalizes (stat skipping; only content + names are compared).
  - hooks/claude enumeration uses ``find hooks/claude -maxdepth 1 \\( -name
    '*.py' -o -name '*.sh' \\) | sort``.  Ported with os.scandir + sort.
  - agents/claude enumeration uses ``find agents/claude -maxdepth 1 -name '*.md'
    ! -name 'README.md' | sort``.  Ported identically.
  - skills/claude enumeration iterates ``skills/claude/*/`` (glob pattern).
    Ported with sorted(os.scandir()).
  - output-styles/claude iterates ``output-styles/claude/*`` files only.
  - The {{PYTHON}} substitution in start-engram-daemon.sh is replicated exactly:
    sed 's|{{PYTHON}}|$HOME/.engram/venv/bin/python3|g'
    Note: the literal '$HOME' is preserved in the file (not expanded at build
    time), matching the legacy script comment and behavior.
  - hooks.json is filtered BEFORE hook scripts are copied (same ordering as
    build-plugin.sh step 3).
  - The hooks.json filter uses PLUGIN_ROOT variable to write the filtered result
    — same output path and indent/newline-trailing behavior as the embedded
    Python in build-plugin.sh.
  - The consistency gate (step 6b) is all-or-nothing: every reference in the
    built hooks.json must exist on disk.
  - plugin.json is copied from repo_root/plugin.json.
  - .mcp.json is written from packaging/mcp.json (NOT repo-root .mcp.json) and
    renamed to .mcp.json at the plugin root.
  - Build manifest paths list is ordered by the copy sequence (not alphabetical),
    matching COPIED_PATHS accumulation order in the bash script.

FLAG: The bash script's COPIED_PATHS list records "packaging/mcp.json" as the
path for the .mcp.json source — this is an artifact of how the bash script
tracks source paths, not destination paths.  We replicate this in
shipped_paths of the build manifest.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any

from .manifest import select_shippables, load_manifest, resolve_tier


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

HOOK_REF_RE = re.compile(r'\bhooks/([A-Za-z0-9_\-]+\.(?:py|sh))\b')

PYTHON_SUBSTITUTION_TARGET = "{{PYTHON}}"
PYTHON_SUBSTITUTION_VALUE = "$HOME/.engram/venv/bin/python3"


# ---------------------------------------------------------------------------
# log helpers (print-based, matching build-plugin.sh log() format)
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Print a [build-plugin] prefixed message, matching build-plugin.sh log()."""
    print(f"[build-plugin] {msg}")


def _die(msg: str) -> None:
    """Print an error and raise RuntimeError, matching build-plugin.sh die()."""
    import sys
    print(f"[build-plugin] ERROR: {msg}", file=sys.stderr)
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# shipping-set helpers
# ---------------------------------------------------------------------------


def _build_ship_set(shipped_entries: list[dict[str, Any]]) -> set[str]:
    """Return a set of paths for O(1) membership testing."""
    return {e["path"] for e in shipped_entries}


def _ships(ship_set: set[str], path: str) -> bool:
    return path in ship_set


# ---------------------------------------------------------------------------
# filter_hooks_json — step 3a (pure transform, testable independently)
# ---------------------------------------------------------------------------


def filter_hooks_json(
    hooks_cfg: dict[str, Any],
    shipped_paths: set[str],
) -> dict[str, Any]:
    """Filter a hooks.json config dict to include only entries whose hook file ships.

    Mirrors the embedded Python in build-plugin.sh step 3.

    Algorithm:
      1. For each hook entry, extract hooks/<file> from the command string.
      2. Drop the entry if <file> is NOT in the shipped hook basenames.
         Entries with no hooks/<file> reference are always kept.
      3. Prune event-arrays / group-arrays that became empty.

    Parameters
    ----------
    hooks_cfg:
        Parsed hooks/hooks.json dict.
    shipped_paths:
        Set of manifest paths that ship (e.g. "hooks/claude/engram-surface-hook.py").
        Used to derive the shipped hook basenames.

    Returns
    -------
    dict
        Filtered hooks config with the same structure as hooks_cfg, minus
        dropped entries and empty groups/events.
    """
    # Build set of shipped hook basenames from shipped paths.
    shipped_hook_names: set[str] = set()
    for p in shipped_paths:
        if p.startswith("hooks/claude/"):
            shipped_hook_names.add(os.path.basename(p))

    def hook_ships(entry: dict[str, Any]) -> bool:
        """True iff this hook entry should be kept at the current build config."""
        cmd = entry.get("command", "")
        m = HOOK_REF_RE.search(cmd)
        if m is None:
            return True  # no hook-file reference → keep unconditionally
        return m.group(1) in shipped_hook_names

    filtered_hooks: dict[str, Any] = {}
    for event_name, groups in hooks_cfg.get("hooks", {}).items():
        filtered_groups = []
        for group in groups:
            raw_entries = group.get("hooks", [])
            kept_entries = [e for e in raw_entries if hook_ships(e)]
            if not kept_entries:
                # All entries in this group were dropped → prune the group.
                continue
            filtered_group = {k: v for k, v in group.items() if k != "hooks"}
            filtered_group["hooks"] = kept_entries
            filtered_groups.append(filtered_group)
        if not filtered_groups:
            # All groups for this event were pruned → omit the event key.
            continue
        filtered_hooks[event_name] = filtered_groups

    return {"hooks": filtered_hooks}


def _count_filter_stats(
    original: dict[str, Any],
    filtered: dict[str, Any],
    shipped_paths: set[str],
) -> tuple[int, int]:
    """Return (kept, dropped) entry counts for logging."""
    total = sum(
        len(group.get("hooks", []))
        for groups in original.get("hooks", {}).values()
        for group in groups
    )
    kept = sum(
        len(group.get("hooks", []))
        for groups in filtered.get("hooks", {}).values()
        for group in groups
    )
    dropped = total - kept
    return kept, dropped


# ---------------------------------------------------------------------------
# post-build consistency check — step 6b
# ---------------------------------------------------------------------------


def check_hooks_consistency(plugin_root: str) -> list[str]:
    """Check hooks.json ↔ shipped-files consistency (all-or-nothing).

    Every file referenced in the BUILT hooks/hooks.json must exist on disk in
    plugin_root/hooks/.

    Parameters
    ----------
    plugin_root:
        Path to the assembled plugin root directory.

    Returns
    -------
    list[str]
        Sorted list of hook filenames that are referenced but missing.
        Empty list means the check passed.
    """
    hooks_json_path = os.path.join(plugin_root, "hooks", "hooks.json")
    with open(hooks_json_path, encoding="utf-8") as f:
        data = json.load(f)

    referenced: set[str] = set()
    for event_name, groups in data.get("hooks", {}).items():
        for group in groups:
            for entry in group.get("hooks", []):
                cmd = entry.get("command", "")
                for m in HOOK_REF_RE.finditer(cmd):
                    referenced.add(m.group(1))

    missing: list[str] = []
    for fname in sorted(referenced):
        fpath = os.path.join(plugin_root, "hooks", fname)
        if not os.path.exists(fpath):
            missing.append(fname)

    return missing


# ---------------------------------------------------------------------------
# file-copy plan + execution — the main build orchestrator
# ---------------------------------------------------------------------------


def build_plugin(
    repo_root: str,
    plugin_root: str,
    tier: str,
    multi_agent: bool,
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    """Assemble the ENGRAM plugin tree.

    Mirrors build-plugin.sh sections 1–7 exactly.

    Parameters
    ----------
    repo_root:
        Absolute path to the engram-alpha repo root.
    plugin_root:
        Absolute path to the output plugin directory.
    tier:
        Depth tier (e.g. "convenience").
    multi_agent:
        Whether to include multi-agent-gated mechanisms.
    manifest:
        Pre-loaded manifest dict (optional; loaded from repo_root if None).

    Returns
    -------
    list[str]
        The COPIED_PATHS list in accumulation order, matching the bash script's
        COPIED_PATHS array.
    """
    # Load manifest if not supplied
    if manifest is None:
        manifest_path = os.path.join(repo_root, "packaging", "tiers.json")
        manifest = load_manifest(manifest_path)

    chosen_rank_str = tier
    tier_rank_val = {"essential": 0, "convenience": 1, "dev": 2}.get(tier, -1)
    ma_str = "yes" if multi_agent else "no"

    _log(
        f"Tier: {tier} (rank={tier_rank_val}), "
        f"multi-agent: {ma_str}"
    )

    # Build shipping set
    shipped_entries = select_shippables(manifest, tier, multi_agent)
    ship_set = _build_ship_set(shipped_entries)

    # ── Clean and create plugin root ─────────────────────────────────────────

    _log(f"Cleaning {plugin_root}")
    if os.path.exists(plugin_root):
        shutil.rmtree(plugin_root)
    os.makedirs(plugin_root, exist_ok=True)

    copied_paths: list[str] = []

    # ── 1. MCP server + runtime modules ──────────────────────────────────────

    _log("Copying runtime modules → plugin root")
    for p in ["server.py", "bootstrap.py", "SKILL.md", "launch-engram-server.sh"]:
        if not _ships(ship_set, p):
            _log(f"  (tier-filtered) {p}")
            continue
        src = os.path.join(repo_root, p)
        dst = os.path.join(plugin_root, p)
        shutil.copy2(src, dst)
        copied_paths.append(p)

    # Ensure launch-engram-server.sh is executable after copy
    launch_sh = os.path.join(plugin_root, "launch-engram-server.sh")
    if os.path.isfile(launch_sh):
        os.chmod(launch_sh, os.stat(launch_sh).st_mode | 0o111)

    # engram_*.py — all are essential per manifest; copy if they ship
    engram_py_files = sorted(
        f for f in os.listdir(repo_root)
        if f.startswith("engram_") and f.endswith(".py")
        and os.path.isfile(os.path.join(repo_root, f))
    )
    if not engram_py_files:
        _die(f"No engram_*.py files found at {repo_root}")
    for bname in engram_py_files:
        if not _ships(ship_set, bname):
            _log(f"  (tier-filtered) {bname}")
            continue
        src = os.path.join(repo_root, bname)
        dst = os.path.join(plugin_root, bname)
        shutil.copy2(src, dst)
        copied_paths.append(bname)

    # ── 1b. tools/ — manifest-driven filtering ───────────────────────────────

    _log("Copying tools/ → plugin tools/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "tools"), exist_ok=True)

    # Iterate SHIPPED_PATHS that match '^tools/'
    for p in [e["path"] for e in shipped_entries if e["path"].startswith("tools/")]:
        src = os.path.join(repo_root, p)
        if not os.path.exists(src):
            _die(f"Manifest entry {p} does not exist at {src}")
        dst = os.path.join(plugin_root, p)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            # cp -r src/. dest/ — copy contents into dest
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied_paths.append(p)
        _log(f"  {p}")

    # ── 1c. templates/ — manifest-filtered ───────────────────────────────────

    _log("Copying templates → plugin templates/")
    os.makedirs(os.path.join(plugin_root, "templates"), exist_ok=True)

    for p in [e["path"] for e in shipped_entries if e["path"].startswith("templates/")]:
        src = os.path.join(repo_root, p)
        if not os.path.exists(src):
            _die(f"Manifest entry {p} does not exist at {src}")
        dst = os.path.join(plugin_root, p)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            # cp -a src/. dest/ — copy contents (archive mode in bash; copytree here)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied_paths.append(p)
        _log(f"  {p}")

    # ── 2. Manifests ─────────────────────────────────────────────────────────

    _log("Copying manifests")
    shutil.copy2(
        os.path.join(repo_root, "plugin.json"),
        os.path.join(plugin_root, "plugin.json"),
    )
    copied_paths.append("plugin.json")

    # Source: packaging/mcp.json → destination: .mcp.json at plugin root
    shutil.copy2(
        os.path.join(repo_root, "packaging", "mcp.json"),
        os.path.join(plugin_root, ".mcp.json"),
    )
    copied_paths.append("packaging/mcp.json")

    # ── 3. Hooks — manifest-filtered ─────────────────────────────────────────

    _log("Copying hooks → plugin hooks/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "hooks"), exist_ok=True)

    _log(
        f"Filtering hooks.json for tier={tier} "
        f"multi-agent={'yes' if multi_agent else 'no'}"
    )

    # Read source hooks.json
    src_hooks_path = os.path.join(repo_root, "hooks", "hooks.json")
    with open(src_hooks_path, encoding="utf-8") as f:
        hooks_data = json.load(f)

    # Filter
    filtered_hooks = filter_hooks_json(hooks_data, ship_set)

    # Count stats for logging (mirrors bash script output)
    kept, dropped = _count_filter_stats(hooks_data, filtered_hooks, ship_set)

    # Write filtered hooks.json
    dest_hooks_json = os.path.join(plugin_root, "hooks", "hooks.json")
    with open(dest_hooks_json, "w", encoding="utf-8") as f:
        json.dump(filtered_hooks, f, indent=2)
        f.write("\n")

    print(
        f"[build-plugin] hooks.json filtered: {kept} entries kept, "
        f"{dropped} dropped (tier/MA excluded)"
    )

    copied_paths.append("hooks/hooks.json")

    # Copy hook scripts filtered through the manifest predicate
    hooks_dir = os.path.join(repo_root, "hooks", "claude")
    # Enumerate: find hooks/claude -maxdepth 1 \( -name '*.py' -o -name '*.sh' \) | sort
    hook_sources = sorted(
        os.path.join(hooks_dir, fname)
        for fname in os.listdir(hooks_dir)
        if (fname.endswith(".py") or fname.endswith(".sh"))
        and os.path.isfile(os.path.join(hooks_dir, fname))
    )
    for src in hook_sources:
        fname = os.path.basename(src)
        manifest_key = f"hooks/claude/{fname}"
        if _ships(ship_set, manifest_key):
            shutil.copy2(src, os.path.join(plugin_root, "hooks", fname))
            copied_paths.append(manifest_key)
            _log(f"  {manifest_key}")
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # Substitute {{PYTHON}} → $HOME/.engram/venv/bin/python3 in start-engram-daemon.sh
    daemon_sh = os.path.join(plugin_root, "hooks", "start-engram-daemon.sh")
    if os.path.isfile(daemon_sh):
        with open(daemon_sh, encoding="utf-8") as f:
            content = f.read()
        new_content = content.replace(PYTHON_SUBSTITUTION_TARGET, PYTHON_SUBSTITUTION_VALUE)
        with open(daemon_sh, "w", encoding="utf-8") as f:
            f.write(new_content)
        _log(
            f"  Substituted {{{{PYTHON}}}} → "
            f"{PYTHON_SUBSTITUTION_VALUE} in start-engram-daemon.sh"
        )

    # ── 4. Skills — manifest-filtered ────────────────────────────────────────

    _log("Copying skills → plugin skills/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "skills"), exist_ok=True)

    skills_dir = os.path.join(repo_root, "skills", "claude")
    if not os.path.isdir(skills_dir):
        _die(f"skills/claude/ not found at {skills_dir}")

    # Enumerate: for skill_dir in skills/claude/*/
    skill_dirs = sorted(
        entry.name
        for entry in os.scandir(skills_dir)
        if entry.is_dir()
    )
    for skill_name in skill_dirs:
        manifest_key = f"skills/claude/{skill_name}"
        if _ships(ship_set, manifest_key):
            src = os.path.join(skills_dir, skill_name)
            dst = os.path.join(plugin_root, "skills", skill_name)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            copied_paths.append(manifest_key)
            _log(f"  {manifest_key}")
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # ── 5. Agents — manifest-filtered ────────────────────────────────────────

    _log("Copying agents → plugin agents/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "agents"), exist_ok=True)

    agents_dir = os.path.join(repo_root, "agents", "claude")
    if not os.path.isdir(agents_dir):
        _die(f"agents/claude/ not found at {agents_dir}")

    # Enumerate: find agents/claude -maxdepth 1 -name '*.md' ! -name 'README.md' | sort
    agent_sources = sorted(
        os.path.join(agents_dir, fname)
        for fname in os.listdir(agents_dir)
        if fname.endswith(".md") and fname != "README.md"
        and os.path.isfile(os.path.join(agents_dir, fname))
    )
    for src in agent_sources:
        fname = os.path.basename(src)
        manifest_key = f"agents/claude/{fname}"
        if _ships(ship_set, manifest_key):
            shutil.copy2(src, os.path.join(plugin_root, "agents", fname))
            copied_paths.append(manifest_key)
            _log(f"  {manifest_key}")
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # ── 6. Output styles — manifest-filtered ─────────────────────────────────

    _log("Copying output-styles → plugin output-styles/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "output-styles"), exist_ok=True)

    styles_dir = os.path.join(repo_root, "output-styles", "claude")
    if not os.path.isdir(styles_dir):
        _die(f"output-styles/claude/ not found at {styles_dir}")

    # Enumerate: for src in output-styles/claude/* (files only)
    for fname in sorted(os.listdir(styles_dir)):
        src = os.path.join(styles_dir, fname)
        if not os.path.isfile(src):
            continue
        manifest_key = f"output-styles/claude/{fname}"
        if _ships(ship_set, manifest_key):
            shutil.copy2(src, os.path.join(plugin_root, "output-styles", fname))
            copied_paths.append(manifest_key)
            _log(f"  {manifest_key}")
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # ── 6b. hooks.json ↔ shipped-files consistency gate ──────────────────────

    _log("Checking hooks.json ↔ shipped-files consistency (all-or-nothing)")
    missing = check_hooks_consistency(plugin_root)
    if missing:
        import sys
        print(
            "[build-plugin] ERROR: built hooks.json references hook file(s) that are "
            "MISSING from the plugin:",
            file=sys.stderr,
        )
        for f in sorted(missing):
            print(f"[build-plugin]   MISSING: hooks/{f}", file=sys.stderr)
        print(
            "[build-plugin] The hooks.json filter step should have dropped these entries.",
            file=sys.stderr,
        )
        print(
            "[build-plugin] Check the filtering logic in build-plugin.sh step 3.",
            file=sys.stderr,
        )
        raise RuntimeError(
            f"hooks.json consistency check failed: missing {missing}"
        )

    n_referenced = len(
        set(
            m.group(1)
            for groups in json.loads(
                open(os.path.join(plugin_root, "hooks", "hooks.json")).read()
            ).get("hooks", {}).values()
            for group in groups
            for entry in group.get("hooks", [])
            for m in HOOK_REF_RE.finditer(entry.get("command", ""))
        )
    )
    _log(
        f"hooks.json ↔ shipped-files: OK ({n_referenced} referenced, all present)"
    )

    # ── 7. Build manifest ─────────────────────────────────────────────────────

    build_manifest_path = os.path.join(plugin_root, ".engram-build-manifest.json")
    _log(f"Writing build manifest → {build_manifest_path}")

    build_manifest = {
        "tier": tier,
        "multi_agent": multi_agent,
        "shipped_paths": copied_paths,
    }
    with open(build_manifest_path, "w", encoding="utf-8") as f:
        json.dump(build_manifest, f, indent=2)
        f.write("\n")

    # ── Done ──────────────────────────────────────────────────────────────────

    _log("")
    _log(f"Plugin tree assembled at: {plugin_root}")
    _log(f"  tier={tier}  multi_agent={'true' if multi_agent else 'false'}")
    _log("")
    _log("Contents:")
    # Enumerate tree, skipping __pycache__ and .pyc — mirrors build-plugin.sh
    all_paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(plugin_root):
        # Filter __pycache__ from traversal
        dirnames[:] = [d for d in sorted(dirnames) if d != "__pycache__"]
        for fname in sorted(filenames):
            if fname.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, plugin_root)
            all_paths.append(rel)
    for rel in sorted(all_paths):
        _log(f"  {rel}")

    _log("")
    _log("To verify manifest JSON:")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/plugin.json'))\" && echo OK")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/.mcp.json'))\" && echo OK")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/hooks/hooks.json'))\" && echo OK")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/.engram-build-manifest.json'))\" && echo OK")

    return copied_paths


# ---------------------------------------------------------------------------
# plugin manifest emission helpers (testable individually)
# ---------------------------------------------------------------------------


def emit_plugin_json(repo_root: str, plugin_root: str) -> None:
    """Copy plugin.json from repo root to plugin root."""
    shutil.copy2(
        os.path.join(repo_root, "plugin.json"),
        os.path.join(plugin_root, "plugin.json"),
    )


def emit_mcp_json(repo_root: str, plugin_root: str) -> None:
    """Copy packaging/mcp.json → plugin_root/.mcp.json."""
    shutil.copy2(
        os.path.join(repo_root, "packaging", "mcp.json"),
        os.path.join(plugin_root, ".mcp.json"),
    )


def write_build_manifest(
    plugin_root: str,
    tier: str,
    multi_agent: bool,
    shipped_paths: list[str],
) -> None:
    """Write .engram-build-manifest.json to plugin root."""
    manifest = {
        "tier": tier,
        "multi_agent": multi_agent,
        "shipped_paths": shipped_paths,
    }
    path = os.path.join(plugin_root, ".engram-build-manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
