"""Contract tests for moving the shared endpoint helper into engram_core
(engram-alpha#3631, tests first).

`_relation_endpoint_error` is the ONE guarded writer of the relation-specific
endpoint-type rules (`instantiates`, `generalizes`, `bears_on`). It lives in
engram_cornerstone today, so engram_derive and engram_revision reach it with a
call-time `import engram_cornerstone`, a bend of the house pattern that family
modules import only engram_core. This is a PURE MOVE: no behaviour changes.

The behaviour itself is pinned by the EXISTING suites, which must stay green and
BYTE-IDENTICAL (they are the contract for "no behaviour change"):
  tests/test_bears_on_3615.py, tests/test_bears_on_hints_3625.py,
  tests/test_edge_classifications_ssot.py, tests/test_retype_cites_widening_2243.py,
  tests/test_generalizes_edge.py, and the closure/tier tests
  (tests/test_closure_checker.py, tests/test_tier_closure_subdirectory_resolution.py,
  tests/test_tiers_manifest.py). The issue names tests/test_import_dep_tier_consistency.py
  as the import-tier test; no such file exists, and those three stand in for it.

This file pins only what is NEW and structural, and is RED on current dev:
  A. the helper is defined in engram_core;
  B. engram_cornerstone keeps the name, bound to the function defined in core (so add_edge,
     retype_edge and the existing tests that call cs._relation_endpoint_error
     are unchanged);
  C. engram_derive and engram_revision no longer import engram_cornerstone at all.
"""

import ast
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "engram"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import engram_core as core  # noqa: E402
import engram_cornerstone as cs  # noqa: E402


def _row(i, t):
    return {"id": i, "type": t}


def test_helper_is_defined_in_core():
    assert hasattr(core, "_relation_endpoint_error")
    assert core._relation_endpoint_error.__module__ == "engram_core"


def test_cornerstone_name_is_core_helper_not_a_copy():
    """A re-export, never a second copy: two copies would drift (#2243 step 3
    exists to keep ONE guarded writer).

    Asserted by WHERE the function is defined, not by `is`: other suites
    purge/reload engram_* modules (an in-place reload of engram_core leaves
    cornerstone's alias pointing at the pre-reload function object, so `is`
    flakes in a full run while still being right in production). A function
    defined in engram_cornerstone has that module's globals and __module__; the
    re-export has engram_core's, and reload reuses the module dict.

    BOUND: this proves the function is DEFINED in engram_core, not that it is
    core's current function object (a wrapper defined in core would pass)."""
    fn = cs._relation_endpoint_error
    assert fn.__module__ == "engram_core"
    assert fn.__globals__ is vars(cs.core)


def test_moved_helper_still_enforces_each_family_rule():
    f = core._relation_endpoint_error
    assert f("bears_on", _row("qu_1", "question"), _row("cj_1", "conjecture")) is None
    assert f("bears_on", _row("ob_1", "observation_factual"), _row("cj_1", "conjecture"))
    assert f("instantiates", _row("ob_1", "observation_factual"), _row("ls_1", "lesson"))
    assert f("generalizes", _row("ob_1", "observation_factual"), _row("cs_1", "cornerstone"))
    assert f("supports", _row("a", "person"), _row("b", "person")) is None


def _imported_modules(path: Path) -> set:
    tree = ast.parse(path.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_derive_and_revision_import_only_core_not_cornerstone():
    """BOUND: sees `import X` / `from X import` statements only; an
    importlib.import_module("engram_cornerstone") or __import__ call would pass
    unnoticed. None exists today."""
    for name in ("engram_derive.py", "engram_revision.py"):
        assert "engram_cornerstone" not in _imported_modules(SRC / name), name
