"""engram-alpha#2855 round-2 -- tests for dump_mcp_schema.py's --summary mode.

Round 1 of the #2855 pilot (PR #2962) shipped before/after character totals
in prose with no committed script backing them (reviewer-fairy suggestion,
round 1). This is the regression suite for the fix: the `--summary` mode
added to `src/engram/tools/dump_mcp_schema.py`.

Scope, deliberately narrow: this file tests the pure computation helpers
(`_sum_description_lengths`, `_tool_metrics`) directly, loaded by file path
-- it does NOT invoke `_dump_schema()` / `main()`, which import `server` and
require an `ENGRAM_HOME`. Exercising the full CLI (server import + argparse
+ stdout) is covered manually in the PR body's before/after measurement
commands, not by this suite, to keep collection fast and hermetic. The
formula itself is what regresses silently if left untested -- these tests
pin it down.

The module's own top-level `sys.path.insert(0, str(Path(__file__).resolve()
.parent.parent))` is a no-op side effect here (it prepends `src/engram/` to
sys.path, which does not collide with anything else this test needs), so a
plain importlib file-path load is safe without needing a `server` shim --
same pattern as tests/test_anchor_regex_parity.py's `_load_board_projects`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
_MODULE_PATH = REPO_ROOT / "src" / "engram" / "tools" / "dump_mcp_schema.py"


def _load_dump_mcp_schema():
    """Load dump_mcp_schema.py by file path, WITHOUT letting its own
    top-level `sys.path.insert(0, .../src/engram)` leak into the shared
    sys.path for the rest of the pytest run.

    That insert is real production behavior (it's how the script finds
    `server` when run standalone) but colliding it into pytest's sys.path
    would shadow the repo-root `tools` package with `src/engram/tools`,
    tripping conftest.py's own #2565 sys.path-collision guard for every
    test module collected afterward. Save/restore sys.path around the
    exec_module call so this test file's collection has no side effect on
    later test files -- same hygiene concern that guard exists to catch,
    applied at load time instead of relying on the guard to catch it.
    """
    saved_path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "dump_mcp_schema_test_load", _MODULE_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved_path


_mod = _load_dump_mcp_schema()


# --- _sum_description_lengths -------------------------------------------


def test_sum_description_lengths_flat():
    obj = {"type": "object", "description": "abcde"}
    assert _mod._sum_description_lengths(obj) == 5


def test_sum_description_lengths_nested_properties():
    obj = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "12345"},
            "b": {"type": "string", "description": "1234567"},
        },
    }
    assert _mod._sum_description_lengths(obj) == 5 + 7


def test_sum_description_lengths_nested_in_list_items():
    # Covers array-schema shapes ("items": {...}) via list recursion.
    obj = {
        "type": "array",
        "items": {"type": "string", "description": "xy"},
    }
    assert _mod._sum_description_lengths(obj) == 2


def test_sum_description_lengths_none_and_missing():
    assert _mod._sum_description_lengths(None) == 0
    assert _mod._sum_description_lengths({"type": "string"}) == 0
    assert _mod._sum_description_lengths({}) == 0


def test_sum_description_lengths_ignores_non_string_description_value():
    # Defensive path: a "description" key whose value isn't a string
    # (shouldn't occur in a real JSON schema) must not raise or be counted.
    obj = {"description": 12345}
    assert _mod._sum_description_lengths(obj) == 0


def test_sum_description_lengths_multiple_levels_deep():
    obj = {
        "properties": {
            "outer": {
                "description": "AAA",
                "properties": {
                    "inner": {"description": "BB"},
                },
            }
        }
    }
    assert _mod._sum_description_lengths(obj) == 3 + 2


# --- _tool_metrics --------------------------------------------------------


def test_tool_metrics_matches_hand_computed_formula():
    entry = {
        "name": "engram_example",
        "description": "A short tool description.",
        "parameters": {
            "type": "object",
            "properties": {
                "payload_json": {
                    "type": "string",
                    "description": "field one description here",
                }
            },
            "required": ["payload_json"],
        },
    }
    metrics = _mod._tool_metrics(entry)

    expected_params_json = json.dumps(
        entry["parameters"], separators=(",", ":"), default=str
    )
    expected_injected = (
        len(entry["name"]) + len(entry["description"]) + len(expected_params_json)
    )

    assert metrics["name"] == "engram_example"
    assert metrics["injected_chars"] == expected_injected
    assert metrics["description_chars"] == len(entry["description"])
    assert metrics["param_desc_chars"] == len("field one description here")


def test_tool_metrics_handles_missing_name_description_parameters():
    # Defensive path: a schema entry with no description/parameters (e.g. a
    # tool with a bare-minimum signature) must not raise.
    entry = {"name": "bare_tool", "description": None, "parameters": None}
    metrics = _mod._tool_metrics(entry)
    assert metrics["name"] == "bare_tool"
    assert metrics["description_chars"] == 0
    assert metrics["param_desc_chars"] == 0
    # injected_chars = len(name) + 0 + len(json.dumps(None)) == len(name) + len("null")
    assert metrics["injected_chars"] == len("bare_tool") + len("null")


def test_tool_metrics_ranking_is_stable_by_injected_chars_desc_then_name():
    # Mirrors _print_summary's own sort key -- pinned here so a change to
    # the ranking rule is a visible, deliberate diff rather than a silent
    # reorder of the report.
    entries = [
        {"name": "z_tool", "description": "", "parameters": {"description": "x" * 10}},
        {"name": "a_tool", "description": "", "parameters": {"description": "x" * 10}},
        {"name": "big_tool", "description": "y" * 100, "parameters": None},
    ]
    metrics = [_mod._tool_metrics(e) for e in entries]
    metrics.sort(key=lambda m: (-m["injected_chars"], m["name"]))

    assert [m["name"] for m in metrics] == ["big_tool", "a_tool", "z_tool"]
