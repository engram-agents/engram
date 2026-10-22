"""Tests for tools/baton-migrate-to-coord-store.py's frontmatter parser.

#1813 -- the module's own `_FRONTMATTER_FIELD_RE` copy used `\\s*` as the
key/value separator, and `\\s` matches `\\n`. An empty, UNQUOTED field
therefore swallowed the entire next field's line as its own value. Tests go
through the module's real `_parse_frontmatter`, not the bare regex, so they
pin behaviour rather than a string.

No test file previously existed for this script (it has no other test
coverage as of #1813); this file is scoped to the frontmatter-parser
contract only.
"""

import importlib.util
from pathlib import Path

MIGRATE_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "tools" / "baton-migrate-to-coord-store.py"
)

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_migrate_module():
    """Import the migration script as a live module (hyphenated filename)."""
    spec = importlib.util.spec_from_file_location(
        "baton_migrate_to_coord_store", MIGRATE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_1813_empty_unquoted_field_does_not_swallow_next_field():
    mod = _load_migrate_module()
    raw = (
        "---\n"
        "project: PR-1813\n"
        "turn_by: \n"
        "participants: [borges]\n"
        "status: open\n"
        "---\n"
        "body\n"
    )
    fields, _ = mod._parse_frontmatter(raw)
    assert fields["turn_by"] == ""
    assert fields["participants"] == "[borges]"
    assert fields["status"] == "open"


def test_1813_unchanged_cases_still_parse_correctly():
    mod = _load_migrate_module()
    # key: value (space) / key:value (no space) / a value containing ": " /
    # quoted-empty / a trailing field at the end of the block -- all must
    # parse exactly as before the fix.
    raw = (
        "---\n"
        "project: PR-1813\n"
        "title:no-space-value\n"
        "colon_value: a value: with a colon\n"
        'turn_reason: ""\n'
        "participants: [borges, ariadne]\n"
        "---\n"
        "body\n"
    )
    fields, _ = mod._parse_frontmatter(raw)
    assert fields["project"] == "PR-1813"
    assert fields["title"] == "no-space-value"
    assert fields["colon_value"] == "a value: with a colon"
    assert fields["turn_reason"] == '""'
    assert fields["participants"] == "[borges, ariadne]"
