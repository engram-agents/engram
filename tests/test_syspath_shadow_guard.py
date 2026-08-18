"""Durable guard for #2063: a top-level tools/*.py must never be able to
shadow a same-named src/ package via `sys.path.insert(0, ...)`.

## Background

`tools/forum.py` (a CLI script) and `src/forum/` (the real package with
`db.py` etc.) share the bare importable name `forum`. Several test files
used to do `sys.path.insert(0, <tools-ish dir>)` -- unconditionally, at
position 0 -- to reach their own tools/-only target (`forum_backup.py`,
`baton.py`, `config_schema.py`, ...). That made the tools/-ish directory a
sys.path *root* ranked ahead of `src/forum` for the rest of the pytest
process, so any test collected afterward that does `from forum import db`
silently resolved to the CLI script instead and failed with a confusing
`ImportError: cannot import name 'db' from 'forum'` -- or, worse, aborted
collection for the ENTIRE suite (#2037). #2063 named the general class:
tools/*.py can shadow ANY same-named src/ package this way, `forum` is
just the pair that collided first.

The concrete fix (this issue's own PR) changed every offending
`insert(0, ...)` to `append(...)` (or dropped it where it was already
redundant) so a tools/-ish directory never outranks a real src/ package.
This file is the "durable guard" the issue itself asks for: it does not
merely check today's known collision, it fails loudly the moment ANY test
file (present or future) reintroduces the anti-pattern, naming the file,
line, and the colliding names -- so file #8 (the issue's own phrase) gets
caught before it ever reaches the confusing collection-time failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "src" / "engram" / "tools"  # canonical; top-level
                                                     # tools/ symlinks here
SRC_DIR = REPO_ROOT / "src"

# Every test file this repo's suite actually collects. Scoped to the two
# roots CI runs (`pytest tests/ src/forum/tests/`, per
# .github/workflows/tests.yml) rather than every *.py in the tree, so this
# stays a guard on the *suite*, not an unbounded repo-wide lint.
_TEST_ROOTS = [REPO_ROOT / "tests", REPO_ROOT / "src" / "forum" / "tests"]


_THIS_FILE = Path(__file__).resolve()


def _all_test_files() -> list[Path]:
    files: list[Path] = []
    for root in _TEST_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.glob("*.py")))
    # Exclude this guard file itself -- its own docstrings/comments
    # necessarily discuss the `sys.path.insert(0, ...)` anti-pattern in
    # prose, which trips the same regex the guard uses to detect it.
    return [f for f in files if f.resolve() != _THIS_FILE]


# ---------------------------------------------------------------------------
# Part 1: static regression guard -- no test file may `insert(0, ...)` a
# tools-ish directory. Parametrized per file so a violation names exactly
# which file (and which line) reintroduced the anti-pattern.
# ---------------------------------------------------------------------------

_INSERT0_RE = re.compile(r"sys\.path\.insert\(\s*0\s*,\s*(?P<arg>[^)]*)\)")
_ASSIGN_RE_TMPL = r"^\s*{name}\s*=\s*(?P<rhs>.+)$"

# A directory argument counts as "tools-ish" if its literal text names a
# `tools` path component -- this catches both the top-level `tools/` dir
# and its canonical `src/engram/tools/` target (the symlink farm), which is
# the actual defect surface (it's where `forum.py` physically lives).
_TOOLS_ISH_RE = re.compile(r'["\']tools["\']')


def _resolve_arg_text(file_lines: list[str], call_line_idx: int, arg: str) -> str:
    """Best-effort: if *arg* is a bare identifier, trace it back to its
    nearest preceding assignment in the same file and return that RHS
    text instead, so `sys.path.insert(0, TOOLS_DIR)` is checked against
    `TOOLS_DIR = ... "tools" ...`, not the opaque identifier `TOOLS_DIR`."""
    arg = arg.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", arg):
        pattern = re.compile(_ASSIGN_RE_TMPL.format(name=re.escape(arg)))
        for i in range(call_line_idx, -1, -1):
            m = pattern.match(file_lines[i])
            if m:
                return m.group("rhs")
        return arg  # no assignment found; check the bare name itself
    return arg


@pytest.mark.parametrize("test_file", _all_test_files(), ids=lambda p: p.name)
def test_no_tools_dir_insert_at_position_zero(test_file: Path) -> None:
    """Regression guard for #2037/#2063: no test file may put a tools-ish
    directory at sys.path position 0. `append(...)` (or a conditional
    insert further back) is fine -- only rank-0 insertion can outrank
    src/ packages for the rest of the pytest process."""
    text = test_file.read_text()
    lines = text.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        m = _INSERT0_RE.search(line)
        if not m:
            continue
        resolved = _resolve_arg_text(lines, i, m.group("arg"))
        if _TOOLS_ISH_RE.search(resolved):
            offenders.append((i + 1, line.strip(), resolved.strip()))
    assert not offenders, (
        f"{test_file.relative_to(REPO_ROOT)} does sys.path.insert(0, ...) "
        f"with a tools-ish directory -- this outranks src/ packages "
        f"(e.g. src/forum/) for every test collected afterward in the "
        f"same pytest process (#2037/#2063). Use sys.path.append(...) "
        f"instead, or import the target module directly by file path via "
        f"importlib.util.spec_from_file_location. Offending line(s): "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# Part 2: forward-looking collision detector. Enumerates every top-level
# tools/*.py script name alongside every top-level importable package
# under src/ (a directory with __init__.py directly under src/), and for
# any name present in BOTH -- `forum` today, whatever collides next
# tomorrow -- asserts that importing the bare name in the worst-case
# ordering (tools-ish dir ranked ahead of src/) still resolves to the
# real package once part 1's discipline is followed for every remaining
# insert(0) site, and independently reports which physical files are in
# collision so the failure message names both paths per #2063's ask.
# ---------------------------------------------------------------------------

def _tools_script_names() -> set[str]:
    top_level_tools = REPO_ROOT / "tools"
    if not top_level_tools.is_dir():
        return set()
    return {
        p.stem
        for p in top_level_tools.glob("*.py")
        if p.stem != "__init__"
    }


def _src_top_level_package_names() -> set[str]:
    if not SRC_DIR.is_dir():
        return set()
    return {
        p.name
        for p in SRC_DIR.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file()
    }


def _collision_names() -> list[str]:
    return sorted(_tools_script_names() & _src_top_level_package_names())


@pytest.mark.parametrize("name", _collision_names() or pytest.param("forum", marks=pytest.mark.xfail(
    reason="no live tools/*.py vs src/<pkg> collision detected -- see test body",
    strict=False,
)))
def test_colliding_name_resolves_to_src_package(name: str) -> None:
    """For every name that exists as BOTH tools/<name>.py and a top-level
    src/<name>/ package, assert the "last mutation wins" pattern this
    repo's real fix relies on (tests/test_forum_db_hostname.py, #2064:
    insert src/ unconditionally, always at position 0, as the LAST
    sys.path mutation before the import) correctly resolves `import
    <name>` to the src/ package even in the worst-case ordering -- some
    earlier-collected test file has already put tools/ at position 0.
    Names both physical files in the failure message so a future
    collision (the issue's "file #8") fails loudly and legibly rather
    than as a confusing downstream ImportError."""
    import importlib
    import sys as _sys

    tools_file = REPO_ROOT / "tools" / f"{name}.py"
    src_pkg = SRC_DIR / name
    assert tools_file.is_file(), f"expected collision file missing: {tools_file}"
    assert (src_pkg / "__init__.py").is_file(), f"expected collision package missing: {src_pkg}"

    saved_path = list(_sys.path)
    # Save (don't just delete) any already-cached module(s) under this
    # name -- other test files collected earlier or later may hold direct
    # references to objects living inside them (e.g.
    # src/forum/tests/test_coordination_names.py asserts `forum.db.X is
    # <the X it imported at its own module top>`). Deleting-and-not-
    # restoring would force everyone downstream to re-import fresh,
    # silently breaking those `is` identity checks -- a self-inflicted
    # repeat of the exact defect class this file exists to guard against.
    saved_modules = {
        mod_name: _sys.modules[mod_name]
        for mod_name in list(_sys.modules)
        if mod_name == name or mod_name.startswith(f"{name}.")
    }
    try:
        # Worst case: some earlier-collected file already polluted tools/
        # to the front.
        _sys.path.insert(0, str(REPO_ROOT / "tools"))
        # The production invariant (test_forum_db_hostname.py, #2064):
        # whoever actually needs the src/ package inserts it
        # unconditionally, always at position 0, as the LAST mutation
        # before the import -- so it always wins regardless of what an
        # earlier file already put at the front.
        _sys.path.insert(0, str(SRC_DIR))

        for mod_name in saved_modules:
            del _sys.modules[mod_name]

        mod = importlib.import_module(name)
        resolved_file = Path(getattr(mod, "__file__", "") or "")
    finally:
        _sys.path[:] = saved_path
        for mod_name in list(_sys.modules):
            if mod_name == name or mod_name.startswith(f"{name}."):
                del _sys.modules[mod_name]
        _sys.modules.update(saved_modules)

    assert str(src_pkg) in str(resolved_file.parent), (
        f"import {name!r} resolved to {resolved_file} instead of the real "
        f"package at {src_pkg} -- tools/{name}.py at {tools_file} is "
        f"shadowing it even under the last-mutation-wins pattern. This is "
        f"the #2037/#2063 defect: re-check part 1's static guard above and "
        f"any file it may have missed, and confirm whichever file needs "
        f"{name!r} still inserts src/ unconditionally, at position 0, as "
        f"the LAST sys.path mutation before its own import."
    )
