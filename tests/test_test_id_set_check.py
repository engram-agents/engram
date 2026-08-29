"""Tests for tools/test_id_set_check.py — the test-id-set shrink gate (#2350).

THE POSITIVE CONTROL IS THE POINT OF THIS SUITE. A gate that reports "nothing
disappeared" because it is mis-wired looks identical to a healthy one, forever
— so every FAIL-expecting case below asserts the *verdict* (report.passed /
report.unaccounted / exit code), never merely that the code ran without
raising.

Coverage (spec table P1-E4, #2350):
  P1 — plain deletion                                        -> FAIL, names id
  P2 — count-equal (instrument-blindness) deletion+addition   -> FAIL, names id
  P3 — rename (delete old name, add new name)                 -> FAIL, names old
  P4 — deleted method on a Test* class                        -> FAIL, names Cls::method
  P5 — deleted async def test_*                                -> FAIL
  N1 — no change                                                -> PASS
  N2 — pure addition                                            -> PASS
  N3 — deletion + matching full-id marker                       -> PASS
  N4 — deletion + matching bare-name marker (unambiguous)       -> PASS
  N5 — def test_foo inside a string literal / a comment         -> NOT collected
  N6 — def test_x nested inside another function                -> NOT collected
  E1 — marker with empty reason                                 -> NOT accounted -> FAIL
  E2 — ambiguous bare-name marker (two files, same function)    -> NOT accounted -> FAIL
  E3 — stale marker (id did not disappear)                      -> PASS, with a warning
  E4 — unparseable source                                       -> ParseError naming the file

Everything below is driven through the pure functions (collect_ids,
parse_removed_test_markers, evaluate) with inline source strings — no git, no
tmp repos, per the spec's testability-seam design.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "test_id_set_check.py"

_spec = importlib.util.spec_from_file_location("test_id_set_check", TOOL_PATH)
_mod = importlib.util.module_from_spec(_spec)
# Register in sys.modules BEFORE exec_module: the tool module uses @dataclass,
# and dataclasses' internal type-resolution looks the module up via
# sys.modules[cls.__module__] — without this it raises AttributeError on a
# None module during class creation.
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

collect_ids = _mod.collect_ids
parse_removed_test_markers = _mod.parse_removed_test_markers
evaluate = _mod.evaluate
ParseError = _mod.ParseError
Marker = _mod.Marker
format_report_text = _mod.format_report_text


# ---------------------------------------------------------------------------
# P1 — plain deletion
# ---------------------------------------------------------------------------

def test_p1_plain_deletion_fails_and_names_the_id():
    base_ids = {"a", "b"}
    head_ids = {"a"}
    report = evaluate(base_ids, head_ids, markers=[])
    assert not report.passed
    assert report.unaccounted == ("b",)


# ---------------------------------------------------------------------------
# P2 — count-equal (instrument-blindness) case
# ---------------------------------------------------------------------------

def test_p2_count_equal_deletion_plus_addition_fails_and_names_the_id():
    """The instrument-blindness case: base {a,b} -> head {a,c}. Count delta is
    zero (2 -> 2), so a length-comparison gate sees nothing — but 'b'
    genuinely disappeared and 'c' is unrelated to it.

    This shape is not synthetic: it is the SECOND real disappearance in the
    commit that motivated #2350 (2ecf88c). tests/test_supported_by.py went
    from 12 ids to 12 ids (count delta zero) while
    test_cascade_relations_is_derives_from_only disappeared and
    test_cascade_relations_membership_is_pinned appeared in its place — a
    perfect 1:1 replacement, invisible to any count-based check, in a file a
    count-based sweep would certify as clean.
    """
    base_ids = {"a", "b"}
    head_ids = {"a", "c"}
    report = evaluate(base_ids, head_ids, markers=[])
    assert not report.passed
    assert report.unaccounted == ("b",)
    assert report.base_count == report.head_count == 2  # the count is blind here


# ---------------------------------------------------------------------------
# P3 — rename
# ---------------------------------------------------------------------------

def test_p3_rename_fails_and_names_the_old_name():
    base_src = "def test_x():\n    assert True\n"
    head_src = "def test_y():\n    assert True\n"
    base_ids = collect_ids({"tests/test_mod.py": base_src})
    head_ids = collect_ids({"tests/test_mod.py": head_src})
    report = evaluate(base_ids, head_ids, markers=[])
    assert not report.passed
    assert report.unaccounted == ("tests/test_mod.py::test_x",)


# ---------------------------------------------------------------------------
# P4 — deleted method on a Test* class
# ---------------------------------------------------------------------------

def test_p4_deleted_class_method_fails_and_names_cls_double_colon_method():
    base_src = (
        "class TestThing:\n"
        "    def test_one(self):\n"
        "        assert True\n"
        "    def test_two(self):\n"
        "        assert True\n"
    )
    head_src = (
        "class TestThing:\n"
        "    def test_one(self):\n"
        "        assert True\n"
    )
    base_ids = collect_ids({"tests/test_mod.py": base_src})
    head_ids = collect_ids({"tests/test_mod.py": head_src})
    report = evaluate(base_ids, head_ids, markers=[])
    assert not report.passed
    assert report.unaccounted == ("tests/test_mod.py::TestThing::test_two",)


# ---------------------------------------------------------------------------
# P5 — deleted async def test_*
# ---------------------------------------------------------------------------

def test_p5_deleted_async_test_fails():
    base_src = "async def test_async_thing():\n    assert True\n"
    head_src = ""
    base_ids = collect_ids({"tests/test_mod.py": base_src})
    head_ids = collect_ids({"tests/test_mod.py": head_src})
    report = evaluate(base_ids, head_ids, markers=[])
    assert not report.passed
    assert report.unaccounted == ("tests/test_mod.py::test_async_thing",)


# ---------------------------------------------------------------------------
# N1 — no change
# ---------------------------------------------------------------------------

def test_n1_no_change_passes():
    ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    report = evaluate(ids, ids, markers=[])
    assert report.passed
    assert report.unaccounted == ()
    assert report.disappeared == ()


# ---------------------------------------------------------------------------
# N2 — pure addition
# ---------------------------------------------------------------------------

def test_n2_pure_addition_passes():
    base_ids = {"tests/test_mod.py::test_a"}
    head_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    report = evaluate(base_ids, head_ids, markers=[])
    assert report.passed
    assert report.appeared == ("tests/test_mod.py::test_b",)


# ---------------------------------------------------------------------------
# N3 — deletion + matching full-id marker
# ---------------------------------------------------------------------------

def test_n3_deletion_with_matching_full_id_marker_passes():
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}
    pr_body = "Removed-test: tests/test_mod.py::test_b — obsolete after refactor\n"
    markers = parse_removed_test_markers(pr_body)
    report = evaluate(base_ids, head_ids, markers)
    assert report.passed
    assert report.unaccounted == ()


# ---------------------------------------------------------------------------
# N4 — deletion + matching bare-name marker (unambiguous)
# ---------------------------------------------------------------------------

def test_n4_deletion_with_unambiguous_bare_name_marker_passes():
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}
    pr_body = "Removed-test: test_b — obsolete after refactor\n"
    markers = parse_removed_test_markers(pr_body)
    report = evaluate(base_ids, head_ids, markers)
    assert report.passed
    assert report.unaccounted == ()


# ---------------------------------------------------------------------------
# N5 — the AST-over-grep control: def test_foo inside a string / a comment
# ---------------------------------------------------------------------------

def test_n5_test_foo_inside_string_and_comment_is_not_collected():
    """A grep-based implementation of `def test_` matching would find both of
    these occurrences and wrongly collect test_foo as a real test id. AST
    parsing sees neither — this is the control that would fail a grep-based
    collector and pass an AST-based one."""
    src = (
        '# def test_foo(): pass  -- not a real test, this is a comment\n'
        'DOCSTRING = """\n'
        'def test_foo():\n'
        '    pass\n'
        '"""\n'
        'def test_real():\n'
        '    assert True\n'
    )
    ids = collect_ids({"tests/test_mod.py": src})
    assert ids == {"tests/test_mod.py::test_real"}


# ---------------------------------------------------------------------------
# N6 — def test_x nested inside another function is not collected
# ---------------------------------------------------------------------------

def test_n6_nested_function_is_not_collected():
    """pytest never collects a function defined inside another function's
    body; the collector must not either."""
    src = (
        "def make_fixture():\n"
        "    def test_x():\n"
        "        assert True\n"
        "    return test_x\n"
        "\n"
        "def test_top_level():\n"
        "    assert True\n"
    )
    ids = collect_ids({"tests/test_mod.py": src})
    assert ids == {"tests/test_mod.py::test_top_level"}


# ---------------------------------------------------------------------------
# E1 — marker with empty reason
# ---------------------------------------------------------------------------

def test_e1_marker_with_empty_reason_is_not_accounted_and_fails():
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}
    pr_body = "Removed-test: tests/test_mod.py::test_b —\n"
    markers = parse_removed_test_markers(pr_body)
    # The marker is parsed (id extracted), but reason is empty after stripping.
    assert len(markers) == 1
    assert markers[0].reason == ""
    report = evaluate(base_ids, head_ids, markers)
    assert not report.passed
    assert report.unaccounted == ("tests/test_mod.py::test_b",)


# ---------------------------------------------------------------------------
# E2 — ambiguous bare-name marker (two files, same function name)
# ---------------------------------------------------------------------------

def test_e2_ambiguous_bare_name_marker_is_not_accounted_and_names_the_ambiguity():
    base_ids = {
        "tests/test_a.py::test_dup",
        "tests/test_b.py::test_dup",
    }
    head_ids = set()
    pr_body = "Removed-test: test_dup — duplicated helper removed\n"
    markers = parse_removed_test_markers(pr_body)
    report = evaluate(base_ids, head_ids, markers)
    assert not report.passed
    assert set(report.unaccounted) == base_ids
    assert len(report.ambiguous) == 1
    assert "ambigu" in report.ambiguous[0].lower()
    assert "test_dup" in report.ambiguous[0]
    # The failure message must say the full id is needed.
    assert "full id" in report.ambiguous[0]


# ---------------------------------------------------------------------------
# E3 — stale marker (id did not disappear)
# ---------------------------------------------------------------------------

def test_e3_stale_marker_passes_with_a_warning():
    base_ids = {"tests/test_mod.py::test_a"}
    head_ids = {"tests/test_mod.py::test_a"}  # nothing actually disappeared
    pr_body = "Removed-test: tests/test_mod.py::test_a — thought this was gone\n"
    markers = parse_removed_test_markers(pr_body)
    report = evaluate(base_ids, head_ids, markers)
    assert report.passed
    assert len(report.warnings) == 1
    assert "test_a" in report.warnings[0]
    assert "stale" in report.warnings[0].lower() or "did not disappear" in report.warnings[0]


# ---------------------------------------------------------------------------
# E4 — unparseable source
# ---------------------------------------------------------------------------

def test_e4_unparseable_source_raises_parse_error_naming_the_file():
    bad_src = "def test_broken(:\n    pass\n"  # syntax error
    with pytest.raises(ParseError) as excinfo:
        collect_ids({"tests/test_broken.py": bad_src})
    assert excinfo.value.path == "tests/test_broken.py"
    assert "tests/test_broken.py" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Marker parsing details (supporting P1-E4, not in the required table but
# load-bearing for the accounting logic above)
# ---------------------------------------------------------------------------

def test_marker_separator_accepts_em_dash_and_hyphen():
    em_dash_body = "Removed-test: tests/test_mod.py::test_a — reason one\n"
    hyphen_body = "Removed-test: tests/test_mod.py::test_b - reason two\n"
    em_markers = parse_removed_test_markers(em_dash_body)
    hyphen_markers = parse_removed_test_markers(hyphen_body)
    assert em_markers[0].id == "tests/test_mod.py::test_a"
    assert em_markers[0].reason == "reason one"
    assert hyphen_markers[0].id == "tests/test_mod.py::test_b"
    assert hyphen_markers[0].reason == "reason two"


def test_marker_key_is_case_insensitive_and_leading_whitespace_allowed():
    pr_body = "  removed-TEST: tests/test_mod.py::test_a — case insensitive key\n"
    markers = parse_removed_test_markers(pr_body)
    assert len(markers) == 1
    assert markers[0].id == "tests/test_mod.py::test_a"


def test_marker_absent_in_pr_body_yields_no_markers():
    assert parse_removed_test_markers("") == []
    assert parse_removed_test_markers("Just a normal PR description, no markers here.\n") == []


# ---------------------------------------------------------------------------
# Text formatting sanity — the ::error::/::warning:: annotations the CI step
# depends on for GitHub Actions log annotation
# ---------------------------------------------------------------------------

def test_format_report_text_emits_error_annotations_for_unaccounted_ids():
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}
    report = evaluate(base_ids, head_ids, markers=[])
    text = format_report_text(report)
    assert "::error::" in text
    assert "tests/test_mod.py::test_b" in text
    assert "FAIL" in text


def test_format_report_text_emits_pass_with_no_error_annotations():
    ids = {"tests/test_mod.py::test_a"}
    report = evaluate(ids, ids, markers=[])
    text = format_report_text(report)
    assert "::error::" not in text
    assert "PASS" in text


# ---------------------------------------------------------------------------
# Round-2 (#2350 follow-up) — the collector's own blind spot: a
# unittest.TestCase subclass whose name does not start with "Test" is
# collected by pytest regardless of name, and was invisible to the
# name-prefix-only rule. Four required controls, per the round-2 spec.
# ---------------------------------------------------------------------------

def test_real_file_control_test_person_node_render_is_29_ids_across_five_classes():
    """tests/test_person_node_render.py is the real, live exposure this
    blocker fixes: pytest collects 29 tests across 5 unittest.TestCase
    subclasses named without the 'Test' prefix. Before the fix,
    collect_ids() returned 0 for this file (measured); this is the control
    that makes that regression impossible to reintroduce silently."""
    src = (REPO_ROOT / "tests" / "test_person_node_render.py").read_text(encoding="utf-8")
    ids = collect_ids({"tests/test_person_node_render.py": src})
    assert len(ids) == 29
    expected_one_per_class = {
        "tests/test_person_node_render.py::RenderOneNodeLinePersonFieldsTest::"
        "test_pronouns_and_relation_render_when_populated",
        "tests/test_person_node_render.py::HookMomentRotationTest::"
        "test_zero_moments_never_attempts_a_moment",
        "tests/test_person_node_render.py::QueryInspectPersonFieldsTest::"
        "test_query_tiered_default_includes_pronouns_relation_trust_tier",
        "tests/test_person_node_render.py::PersonMomentCooldownConfigTest::"
        "test_default_is_3600_when_config_absent",
        "tests/test_person_node_render.py::PersonMomentLedgerCrossImplementationParityTest::"
        "test_hook_and_server_writes_produce_same_bucket_shape",
    }
    assert expected_one_per_class <= ids


def test_unittest_testcase_subclass_without_test_prefix_deleted_method_fails_gate():
    """The spec's motivating example: class WidgetRenderingTest(unittest.TestCase)
    is invisible to a name-prefix-only rule. A deleted method on it must
    still fail the gate and name the full id."""
    base_src = (
        "import unittest\n"
        "class WidgetRenderingTest(unittest.TestCase):\n"
        "    def test_x(self):\n"
        "        assert True\n"
        "    def test_y(self):\n"
        "        assert True\n"
    )
    head_src = (
        "import unittest\n"
        "class WidgetRenderingTest(unittest.TestCase):\n"
        "    def test_y(self):\n"
        "        assert True\n"
    )
    base_ids = collect_ids({"tests/test_mod.py": base_src})
    head_ids = collect_ids({"tests/test_mod.py": head_src})
    report = evaluate(base_ids, head_ids, markers=[])
    assert not report.passed
    assert report.unaccounted == ("tests/test_mod.py::WidgetRenderingTest::test_x",)


def test_direct_testcase_base_both_ast_shapes_are_collected():
    """TestCase (from-import) and unittest.TestCase (attribute access) are
    different AST shapes (ast.Name vs. ast.Attribute) — a fix can easily
    catch one and miss the other. Both must be collected for a
    non-Test-prefixed class."""
    from_import_src = (
        "from unittest import TestCase\n"
        "class HelperCheckTest(TestCase):\n"
        "    def test_a(self):\n"
        "        assert True\n"
    )
    attribute_src = (
        "import unittest\n"
        "class WidgetRenderingTest(unittest.TestCase):\n"
        "    def test_x(self):\n"
        "        assert True\n"
    )
    from_import_ids = collect_ids({"tests/test_mod.py": from_import_src})
    attribute_ids = collect_ids({"tests/test_mod.py": attribute_src})
    assert from_import_ids == {"tests/test_mod.py::HelperCheckTest::test_a"}
    assert attribute_ids == {"tests/test_mod.py::WidgetRenderingTest::test_x"}


def test_non_testcase_class_with_test_named_method_collects_nothing():
    """Negative control: a plain class with a test_-named method and NO
    TestCase base (direct or otherwise) must collect ZERO ids. Without this
    the fix could over-collect and inflate the base set, producing phantom
    disappearances."""
    src = (
        "class Helper:\n"
        "    def test_thing(self):\n"
        "        assert True\n"
    )
    ids = collect_ids({"tests/test_mod.py": src})
    assert ids == set()


# ---------------------------------------------------------------------------
# Round-2 (#2350 follow-up) — marker regex must exempt inline-code spans, so
# a PR body that *illustrates* the Removed-test: syntax inside backticks is
# never parsed as a real marker (the close-keyword trap discipline in
# CLAUDE.md is the documented incident class this mirrors).
# ---------------------------------------------------------------------------

def test_backticked_example_marker_does_not_account_for_a_real_disappearance():
    """The marker regex is line-anchored (^[ \\t]*removed-test:), so a
    same-line inline-code illustration (`` `Removed-test: ...` `` mid-sentence)
    can never match in the first place — the anchor alone blocks it, with or
    without stripping, so it is not a real demonstration of the fix.

    The genuine exposure is an inline-code SPAN that starts on one line and
    closes on a later one (the same `[^`]*` — no DOTALL needed, `[^`]` already
    spans newlines — behavior the sibling anti-dup sed strip relies on): the
    marker line itself sits at true line-start, UNGUARDED by any character on
    its own line, so it matches the raw regex before stripping is applied.
    Only stripping removes it (along with the two backtick-only lines around
    it) before the marker regex ever sees it."""
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}
    pr_body = (
        "Example only, not a real marker:\n"
        "\n"
        "`\n"
        "Removed-test: tests/test_mod.py::test_b — example only\n"
        "`\n"
    )
    markers = parse_removed_test_markers(pr_body)
    assert markers == []
    report = evaluate(base_ids, head_ids, markers)
    assert not report.passed
    assert report.unaccounted == ("tests/test_mod.py::test_b",)


def test_plain_text_marker_for_the_same_id_still_accounts_for_it():
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}
    pr_body = "Removed-test: tests/test_mod.py::test_b — obsolete after refactor\n"
    markers = parse_removed_test_markers(pr_body)
    assert len(markers) == 1
    report = evaluate(base_ids, head_ids, markers)
    assert report.passed
    assert report.unaccounted == ()


# ---------------------------------------------------------------------------
# Round-3 (#2350 follow-up) -- a stray UNTERMINATED backtick earlier in the
# body pairs with the NEXT backtick that appears later, and the strip
# swallows everything between them, including a well-formed marker sitting
# in that gap. This is a documented LIMIT of the regex-over-markdown strip
# (see parse_removed_test_markers's docstring), not a bug to fix here: the
# failure is SAFE (the deletion reads as unaccounted, the gate FAILS
# LOUDLY) rather than a real deletion slipping through silently.
# ---------------------------------------------------------------------------

def test_stray_unterminated_backtick_swallows_a_later_marker_by_design():
    """An ordinary markdown typo -- an inline-code span opened with a
    backtick and never closed -- pairs with the next backtick appearing
    anywhere later in the body (here, the opening backtick of an unrelated,
    genuinely-closed inline-code span two paragraphs on), and the strip in
    parse_removed_test_markers swallows everything in between, including a
    well-formed `Removed-test:` marker sitting in that gap. The control half
    (identical body, stray backtick removed) is what makes this a documented
    limit rather than an assertion of desired behaviour: it proves the
    swallowing is caused by the stray backtick specifically, not by
    something else about the body's shape."""
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}

    body_with_stray_backtick = (
        "See the config option `enable_foo (typo, unclosed)\n"
        "\n"
        "Removed-test: tests/test_mod.py::test_b — obsolete after refactor"
        "\n"
        "Also see `this real inline code`.\n"
    )
    markers_with_stray = parse_removed_test_markers(body_with_stray_backtick)
    assert markers_with_stray == []
    report_with_stray = evaluate(base_ids, head_ids, markers_with_stray)
    assert not report_with_stray.passed
    assert report_with_stray.unaccounted == ("tests/test_mod.py::test_b",)

    # Control: the identical body with the stray backtick removed. Nothing
    # else changes -- the marker line, the trailing genuine inline-code
    # span, and the ids under test are all the same.
    body_without_stray_backtick = (
        "See the config option enable_foo (typo, unclosed)\n"
        "\n"
        "Removed-test: tests/test_mod.py::test_b — obsolete after refactor"
        "\n"
        "Also see `this real inline code`.\n"
    )
    markers_without_stray = parse_removed_test_markers(body_without_stray_backtick)
    assert len(markers_without_stray) == 1
    report_without_stray = evaluate(base_ids, head_ids, markers_without_stray)
    assert report_without_stray.passed
    assert report_without_stray.unaccounted == ()
