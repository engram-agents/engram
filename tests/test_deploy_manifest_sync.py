"""Drift test: every project-module import in server.py must be in deploy.sh MANIFEST.

The incident this test prevents: PR #83 (2026-05-11) shipped a new module
`engram_filter.py` that `server.py` imports unqualified. The `tools/deploy.sh`
MANIFEST was not updated to include it. First post-merge deploy left the
running ENGRAM tree without the module — `from engram_filter import ...`
inside engram_list would have raised ModuleNotFoundError the moment any
agent invoked the structured filter. Manual copy unblocked; PR #89 +
this test prevent recurrence.

The test mirrors PR #74's `test_skill_server_sync.py` discipline: AST-parse
the source, regex-parse the config, assert sets agree, fast (<1s), no DB.

Symmetric drift check: every alpha-root `.py` file is EITHER in MANIFEST
OR explicitly allowlisted (with a reason). Allowlist entries get audited
in their own test — see `test_manifest_orphan_allowlist_entries_have_reasons`.
"""

from __future__ import annotations

import ast
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(REPO_ROOT, "server.py")
DEPLOY_SH = os.path.join(REPO_ROOT, "tools", "deploy.sh")


# Python files at alpha root that are NOT runtime modules deployed via
# deploy.sh — install-time entry points or developer tools that only ever
# run from the alpha working tree.
#
# Scope: .py files only. The orphan check (test_no_orphan_project_modules)
# operates on `_project_root_py_files()` which is .py-scoped, so .sh / .md
# entries would be inert here — keep this list .py-only to match.
#
# Each entry MUST have a reason. The orphan-allowlist audit test asserts
# this so future-me can't silently add a file to the skip-list without
# justifying it.
MANIFEST_ORPHAN_ALLOWLIST: dict[str, str] = {
    "bootstrap.py": "install-time seed-graph creator, not a runtime module",
    "selftest.py": "developer pre-deploy pytest runner; not part of the runtime tree",
    "engram_log_indexer.py": "Phase 3 deliverable; deploy.sh MANIFEST integration deferred to Phase 4 (alpha #175)",
}


def _project_root_py_files() -> set[str]:
    """Names (without .py) of every Python file at alpha root."""
    return {
        f[:-3]
        for f in os.listdir(REPO_ROOT)
        if f.endswith(".py") and os.path.isfile(os.path.join(REPO_ROOT, f))
    }


def _project_module_names() -> set[str]:
    """Project-module name set: alpha-root .py files we consider importable."""
    return _project_root_py_files()


def _extract_imports_from_file(path: str) -> set[str]:
    """Return the set of top-level module names imported by a Python file.

    Catches both `import X` and `from X import Y` shapes, and walks into
    function-body imports too (server.py's engram_list uses a lazy local
    import for engram_filter — that's exactly the import that PR #83
    introduced and that this test must catch).
    """
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                imports.add(root)
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for relative imports — server.py uses
            # absolute imports throughout, so we expect node.module truthy.
            if node.module:
                root = node.module.split(".")[0]
                imports.add(root)
    return imports


def _parse_manifest_from_deploy_sh() -> set[str]:
    """Extract MANIFEST entries (file-or-dir names) from tools/deploy.sh.

    The MANIFEST is a bash array literal:
        MANIFEST=(
            "hooks/$TARGET:hooks"
            "server.py"
            ...
        )
    Each entry may be plain (server.py) or src:dst remapped
    (hooks/$TARGET:hooks). For drift-detection we care about the SRC side
    of the colon — that's the alpha-tree path.
    """
    with open(DEPLOY_SH, encoding="utf-8") as f:
        text = f.read()

    m = re.search(r"^MANIFEST=\((.*?)^\)\s*$", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise AssertionError(
            "Could not locate MANIFEST=( ... ) block in tools/deploy.sh. "
            "Did the array syntax change?"
        )
    block = m.group(1)
    entries: set[str] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip surrounding quotes
        entry = line.strip('"').strip("'")
        # src:dst remap — take SRC side
        if ":" in entry:
            entry = entry.split(":", 1)[0]
        # Trailing slash normalization
        entry = entry.rstrip("/")
        entries.add(entry)
    return entries


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_server_imports_all_in_manifest():
    """Every project-module import in server.py is covered by deploy.sh MANIFEST.

    Catches the engram_filter.py incident (PR #83 / PR #89 root cause):
    server.py imported a new module that wasn't in MANIFEST, so deploy
    left the running tree without it.
    """
    imports = _extract_imports_from_file(SERVER_PY)
    project_modules = _project_module_names()
    manifest = _parse_manifest_from_deploy_sh()

    # Names that are both (a) imported by server.py and (b) project modules
    # are what MANIFEST must cover.
    project_imports = imports & project_modules

    # Compare against MANIFEST entries — strip .py to match name set.
    manifest_module_names = {e[:-3] if e.endswith(".py") else e for e in manifest}

    missing = project_imports - manifest_module_names
    assert not missing, (
        f"server.py imports project modules NOT in tools/deploy.sh MANIFEST: "
        f"{sorted(missing)}. Add them to MANIFEST or add the file to "
        f"MANIFEST_ORPHAN_ALLOWLIST with a reason."
    )


def test_no_phantom_manifest_entries():
    """Every MANIFEST entry (for top-level .py files) corresponds to a real file.

    Catches the inverse drift: MANIFEST lists a module that's been renamed
    or removed. The deploy would silently skip the missing entry; this
    test makes the drift loud.
    """
    manifest = _parse_manifest_from_deploy_sh()
    project_files = _project_root_py_files()

    phantoms = []
    for entry in manifest:
        # Only check entries that look like top-level .py files. Directories
        # (hooks, tools) and SKILL.md are out of scope for this test.
        if entry.endswith(".py") and "/" not in entry:
            module_name = entry[:-3]
            if module_name not in project_files:
                phantoms.append(entry)

    assert not phantoms, (
        f"MANIFEST lists files that don't exist at alpha root: {sorted(phantoms)}. "
        f"Update MANIFEST or restore the file."
    )


def test_no_orphan_project_modules():
    """Every alpha-root .py file is either in MANIFEST or in the allowlist.

    Catches drift in the other direction: a module exists at alpha root
    but isn't deployed AND isn't on the explicit not-runtime list. Either
    it should be deployed or it should be allowlisted with a reason.

    Without this test, a future agent could ship a new runtime module
    that's silently never deployed (the inverse of today's incident).
    """
    project_files = _project_root_py_files()
    manifest = _parse_manifest_from_deploy_sh()

    manifest_module_names = {e[:-3] if e.endswith(".py") else e for e in manifest}
    allowlisted_names = {f[:-3] for f in MANIFEST_ORPHAN_ALLOWLIST.keys() if f.endswith(".py")}

    orphans = project_files - manifest_module_names - allowlisted_names
    assert not orphans, (
        f"Alpha-root .py files NOT in MANIFEST and NOT in the orphan allowlist: "
        f"{sorted(orphans)}. Add them to MANIFEST in tools/deploy.sh, OR add to "
        f"MANIFEST_ORPHAN_ALLOWLIST in this test file with a reason."
    )


def test_manifest_orphan_allowlist_entries_have_reasons():
    """Every MANIFEST_ORPHAN_ALLOWLIST entry has a non-empty reason string.

    Mirrors PR #74's discipline (test_allowlist_entries_have_reasons in
    test_skill_server_sync.py): you can't silently grow the skip-list;
    each entry has to justify itself.
    """
    for filename, reason in MANIFEST_ORPHAN_ALLOWLIST.items():
        assert reason and reason.strip(), (
            f"MANIFEST_ORPHAN_ALLOWLIST entry {filename!r} has empty reason. "
            f"Document why this file is NOT in deploy.sh MANIFEST."
        )


def test_manifest_orphan_allowlist_entries_exist():
    """Every MANIFEST_ORPHAN_ALLOWLIST entry references a real file.

    Prevents the allowlist itself from drifting — if a file is renamed
    or removed, the allowlist entry should be cleaned up.
    """
    missing = []
    for filename in MANIFEST_ORPHAN_ALLOWLIST.keys():
        if not os.path.exists(os.path.join(REPO_ROOT, filename)):
            missing.append(filename)
    assert not missing, (
        f"MANIFEST_ORPHAN_ALLOWLIST references files that don't exist: "
        f"{sorted(missing)}. Remove the allowlist entry."
    )


# ---------------------------------------------------------------------------
# install.sh ↔ deploy.sh MANIFEST symmetry helpers + test
# ---------------------------------------------------------------------------


def _install_sh_py_files() -> set[str]:
    """Return the set of .py filenames install.sh ships to $ENGRAM_HOME.

    Reads install.sh, finds the denylist `case "$runtime_file" in <names>) continue ;;`
    statement, computes (glob *.py in alpha root) - (denylist).
    """
    install_sh_path = os.path.join(REPO_ROOT, "install.sh")
    with open(install_sh_path) as f:
        content = f.read()

    # Find the denylist case statement. The pattern is:
    #   case "$runtime_file" in
    #     name1.py|name2.py|name3.py) continue ;;
    #   esac
    # (Note: there's also a `case "$runtime_file" in *.py) chmod +x ...` later
    # in the same loop — only the `continue` case is the denylist; the chmod
    # case is unrelated to the shipping decision.)
    pattern = re.compile(
        r'case\s+"\$runtime_file"\s+in\s+'   # case "$runtime_file" in
        r'([^)]+)\)\s+continue\s*;;',          # name1.py|name2.py) continue ;;
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        # Fallback: if the parser breaks, fail loudly rather than return wrong set.
        raise AssertionError(
            "install.sh parser could not find the denylist case statement. "
            "This usually means install.sh's loop body was edited in a way the "
            "test parser doesn't recognize. Update _install_sh_py_files() in "
            f"{__file__} to match install.sh's new shape."
        )

    denylist = {name.strip() for name in match.group(1).split("|")}

    # Effective shipping set: globbed *.py at alpha root minus denylist.
    # _project_root_py_files returns stem names (without .py); rejoin for set match.
    py_filenames = {name + ".py" for name in _project_root_py_files()}
    return py_filenames - denylist


def _deploy_manifest_py_files() -> set[str]:
    """Return the set of .py filenames in deploy.sh's MANIFEST array.

    Reuses the existing _parse_manifest_from_deploy_sh() parser and filters
    to top-level .py entries only (no subpath entries like hooks/$TARGET:hooks).
    """
    manifest = _parse_manifest_from_deploy_sh()
    return {
        entry
        for entry in manifest
        if entry.endswith(".py") and "/" not in entry
    }


def test_install_sh_ships_same_py_set_as_deploy_manifest():
    """install.sh and deploy.sh must ship the same set of .py runtime files.

    install.sh uses glob + denylist; deploy.sh uses an enumerated MANIFEST.
    Both must converge on the same set: a future top-level .py added to alpha
    must either ship from BOTH paths (runtime module) or NEITHER (install-time
    scaffolding, documented in MANIFEST_ORPHAN_ALLOWLIST).
    """
    install_sh_py = _install_sh_py_files()
    deploy_manifest_py = _deploy_manifest_py_files()
    assert install_sh_py == deploy_manifest_py, (
        f"install.sh ships {install_sh_py - deploy_manifest_py} that deploy.sh does NOT, "
        f"and deploy.sh ships {deploy_manifest_py - install_sh_py} that install.sh does NOT. "
        f"Either:\n"
        f"  - install.sh's denylist needs updating (likely if a new runtime module was added), or\n"
        f"  - deploy.sh's MANIFEST needs updating, or\n"
        f"  - MANIFEST_ORPHAN_ALLOWLIST needs a new entry (install-time scaffolding), "
        f"in which case BOTH install.sh's denylist AND this test pass once allowlist updated."
    )
