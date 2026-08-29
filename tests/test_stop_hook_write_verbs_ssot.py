"""SSoT binding: the stop hook's write-verb set must equal server.py's real
write-capable tool set (issue #2273).

`_ENGRAM_WRITE_VERBS` in src/engram/hooks/claude/engram-stop-hook.py is a
hand-maintained mirror of "which MCP tools write to ENGRAM" — used by
`_is_engram_write_tool` to decide whether a turn's tool calls should count
as an ENGRAM write for the write-check nudge. Hand-maintained mirrors drift:
#2273 found the hook's set 7 tools behind the real one (activate_goal,
deactivate_goal, add_special_moment, set_person_lineage, update_lesson,
update_person, reflect) — a turn whose only ENGRAM write was one of those 7
silently read as zero writes.

This mirrors the SHAPE of #2252 Stage 1.0b's cascade/provenance derivation
tests (tests/test_cascade_derived_sets.py): don't just assert a hardcoded
"expected" set matches by coincidence — derive the write-capable set from
the actual ground truth (server.py's TOOL_ANNOTATIONS, populated at
@mcp.tool() decoration time — see server.py's TOOL_ANNOTATIONS docstring)
and assert EQUALITY (not a superset/subset check) against the hook's set,
so any future tool addition/removal/renaming that isn't mirrored into the
hook is caught immediately rather than silently drifting again.

Ground truth: server.TOOL_ANNOTATIONS[name]["readOnlyHint"] is False for
every write-capable tool (captured verbatim from each @mcp.tool()
call site's annotations= kwarg, at module-import time — before any tool is
ever invoked). A tool name's "verb" is the part after the "engram_" prefix
(all 56 real ENGRAM tool names carry it — asserted below as a precondition,
so a future tool renamed off this convention fails loudly here rather than
silently vanishing from the comparison).

Hook-module import: engram-stop-hook.py lives outside the normal package
import path (hyphenated filename, not a package member) and is invoked as a
script by Claude Code, not imported by any other test today. Loaded here via
importlib.util.spec_from_file_location — the same by-path mechanism this
repo's own test_tool_capability_index.py already uses to load
tools/engram-generate-tool-capability-index.py (see that file's
_load_generator_module()). Importing the module (as opposed to running it)
is safe: main() is guarded by `if __name__ == "__main__":`, so import-time
side effects are limited to the hook's own module-level setup (ENGRAM_HOME
resolution, sys.path insertion for its local `_hooklib` import) — none of
which touches the real ~/.engram install or performs any I/O.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-stop-hook.py"


def _fresh_server_module():
    """Import server.py fresh (mirrors test_mcp_tool_capability_enforcement.py's
    _fresh_server_module() / tests/conftest.py's fresh_module() pattern)."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    for p in (str(SRC_ENGRAM), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")
    import server
    return server


def _load_stop_hook_module():
    """Load engram-stop-hook.py by file path (it is not a package member —
    hyphenated filename, invoked as a script, not importable via a normal
    `import` statement). Mirrors test_tool_capability_index.py's
    _load_generator_module() pattern for loading a hyphenated script file."""
    spec = importlib.util.spec_from_file_location(
        "engram_stop_hook_under_test", str(HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def server_mod():
    return _fresh_server_module()


@pytest.fixture(scope="module")
def stop_hook_mod():
    return _load_stop_hook_module()


def _real_write_capable_verbs(server_mod) -> set[str]:
    """Derive the ground-truth write-capable verb set from TOOL_ANNOTATIONS.

    A tool is write-capable iff its recorded annotations dict has
    readOnlyHint is False (strict `is False`, not falsy — an absent/None
    readOnlyHint is a data problem, not evidence of a write tool, and should
    surface as a missing-precondition failure rather than silently counting
    or silently excluding).
    """
    verbs = set()
    for name, annotations in server_mod.TOOL_ANNOTATIONS.items():
        assert name.startswith("engram_"), (
            f"Tool {name!r} does not follow the engram_<verb> naming "
            f"convention this test's verb-stripping relies on — update the "
            f"stripping logic before trusting this comparison."
        )
        if annotations.get("readOnlyHint") is False:
            verbs.add(name[len("engram_"):])
    return verbs


def test_every_real_tool_has_a_recorded_annotations_entry(server_mod):
    """Precondition: TOOL_ANNOTATIONS must be fully populated (one entry per
    real @mcp.tool()-decorated tool) — a partially-populated registry would
    make the equality check below vacuously pass on a shrunken set."""
    assert server_mod.TOOL_ANNOTATIONS, "TOOL_ANNOTATIONS is empty — server.py import failed to populate it."
    assert set(server_mod.TOOL_ANNOTATIONS) == set(server_mod.TOOL_CAPABILITIES), (
        "TOOL_ANNOTATIONS and TOOL_CAPABILITIES are both populated at the same "
        "decoration site and must cover the identical tool-name set; a "
        "mismatch means one of the two registries is not being written for "
        "every real tool."
    )


def test_hook_write_verb_set_matches_server_write_capable_set(server_mod, stop_hook_mod):
    """The binding SSoT check: the hook's hand-maintained _ENGRAM_WRITE_VERBS
    must equal the real write-capable verb set derived from server.py's
    TOOL_ANNOTATIONS. Equality (not a superset/subset check) in BOTH
    directions — a verb present in one set but not the other is a drift bug
    regardless of which direction it points.
    """
    real_verbs = _real_write_capable_verbs(server_mod)
    hook_verbs = set(stop_hook_mod._ENGRAM_WRITE_VERBS)

    missing_from_hook = sorted(real_verbs - hook_verbs)
    extra_in_hook = sorted(hook_verbs - real_verbs)

    if missing_from_hook or extra_in_hook:
        lines = [
            "_ENGRAM_WRITE_VERBS has drifted from server.py's real "
            "write-capable tool set (issue #2273).",
            "",
            "WRITE-CAPABLE, ABSENT FROM THE HOOK SET "
            f"({len(missing_from_hook)}):",
        ]
        if missing_from_hook:
            lines.extend(f"  - {v}" for v in missing_from_hook)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append(
            f"IN HOOK SET, NOT WRITE-CAPABLE ({len(extra_in_hook)}):"
        )
        if extra_in_hook:
            lines.extend(f"  - {v}" for v in extra_in_hook)
        else:
            lines.append("  (none)")
        pytest.fail("\n".join(lines))

    assert real_verbs == hook_verbs
