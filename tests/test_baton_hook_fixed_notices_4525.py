"""#4525: the baton hook's OWN fixed notices reach the context byte-identical.

Written BEFORE the fix (contract-first). The hook's notices (``_NO_OWNER_NOTICE``,
``_NEEDS_QUALIFIED_ANCHOR``) carry ``<owner>``-style placeholders; running them through
the #4453 forum-data sanitizer turned every angle bracket into U+FFFD. Only the hook's
own constants are trusted; every other live status is still data and still sanitized.

Cells: C3 (class cell, enumerated from the source, not hand-listed) and C4 (control: a
forum-sourced ``<`` is still neutralised).
"""

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_baton_hook import (  # noqa: E402
    HOOK_PATH, _call_main, _load_hook_module, _make_project_dict, _parse_ctx, _write_config,
)

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _notice_constants():
    """Every module-level str constant named ``*_NOTICE`` or ``_NEEDS_*``, read off the
    hook's source by AST walk (never hand-listed)."""
    tree = ast.parse(Path(HOOK_PATH).read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for t in node.targets:
                if isinstance(t, ast.Name) and (t.id.endswith("_NOTICE")
                                                or t.id.startswith("_NEEDS_")):
                    found[t.id] = node.value.value
    return found


@pytest.fixture
def mod(tmp_path):
    home = tmp_path / "engram_home"
    home.mkdir()
    _write_config(home)
    return _load_hook_module(home)


def _ctx_with_status(mod, status):
    projects = [_make_project_dict("pool-x", turn="borges", status="in-progress",
                                   github="project/4")]
    with patch.object(mod, "_get_live_status", return_value=status):
        stdout, _, _ = _call_main(mod, projects=projects, gh_ok=True)
    return _parse_ctx(stdout)


def test_c3_floor_enumeration_finds_the_known_notices():
    names = set(_notice_constants())
    assert {"_NO_OWNER_NOTICE", "_NEEDS_QUALIFIED_ANCHOR"} <= names, names


def test_c3_trusted_set_equals_enumerated_set(mod):
    """The set the hook trusts (runtime globals) and the set C3 drives (AST walk) must be
    the same set, both directions: a notice the hook trusts but the walk cannot see
    (annotated, computed) would otherwise escape C3 silently."""
    assert set(mod._FIXED_NOTICES) == set(_notice_constants().values())


@pytest.mark.parametrize("name", sorted(_notice_constants()))
def test_c3_every_fixed_notice_reaches_context_byte_identical(mod, name):
    text = _notice_constants()[name]
    assert getattr(mod, name) == text  # the AST read agrees with the live module
    ctx = _ctx_with_status(mod, text)
    assert f" [{text}]" in ctx, (name, ctx)
    assert "�" not in ctx, ctx


def test_c4_forum_sourced_angle_brackets_are_still_neutralised(mod):
    ctx = _ctx_with_status(mod, "x <owner> y")
    assert "<owner>" not in ctx, ctx
    assert "x �owner� y" in ctx, ctx


def test_c4_a_near_miss_of_a_notice_is_still_data(mod):
    """Only the exact constants are trusted, not anything shaped like one."""
    text = mod._NO_OWNER_NOTICE + " <extra>"
    ctx = _ctx_with_status(mod, text)
    assert "<extra>" not in ctx and "<owner>" not in ctx, ctx
