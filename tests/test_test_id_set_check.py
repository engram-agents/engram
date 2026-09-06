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
  E5 — Removed-test-shaped line that fails to parse (#2393)     -> advisory warning naming the
                                                                    line; disappearance still FAILs

Everything below is driven through the pure functions (collect_ids,
parse_removed_test_markers, evaluate) with inline source strings — no git, no
tmp repos, per the spec's testability-seam design.
"""

import importlib.util
import subprocess
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
find_unparsed_removed_test_lines = _mod.find_unparsed_removed_test_lines
evaluate = _mod.evaluate
ParseError = _mod.ParseError
Marker = _mod.Marker
format_report_text = _mod.format_report_text
main = _mod.main
warn_if_base_is_not_ancestor = _mod.warn_if_base_is_not_ancestor
_is_ancestor = _mod._is_ancestor
_merge_base = _mod._merge_base


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
# E5 — a Removed-test-shaped line that fails to parse (#2393)
#
# Three variants measured on the live issue, each dropping the SAME way for a
# DIFFERENT reason. All three must warn; none may block a PR that is
# otherwise accounted for (advisory, same as a stale marker).
# ---------------------------------------------------------------------------

def test_e5_no_separator_parenthetical_form_warns_and_still_fails_loud_on_the_id():
    """#2392's actual body line: a good-faith Removed-test attempt with no
    id/reason separator at all."""
    base_ids = {"tests/x.py::test_a", "tests/x.py::test_b"}
    head_ids = {"tests/x.py::test_a2", "tests/x.py::test_b2"}
    pr_body = (
        "Removed-test: none (two tests renamed: `test_a` -> `test_a2`, "
        "same for test_b).\n"
    )
    markers = parse_removed_test_markers(pr_body)
    assert markers == [], "must not accidentally parse — that would hide the bug it exercises"
    unparsed = find_unparsed_removed_test_lines(pr_body)
    assert len(unparsed) == 1
    assert "Removed-test:" in unparsed[0]

    report = evaluate(base_ids, head_ids, markers, tuple(unparsed))
    assert not report.passed, "the disappearance is still unaccounted — this stays a real failure"
    assert len(report.warnings) == 1
    assert "did not parse" in report.warnings[0]
    assert "none (two tests renamed" in report.warnings[0]


def test_e5_no_separator_words_only_form_warns():
    pr_body = "Removed-test: tests/x.py::test_a renamed to test_b\n"
    unparsed = find_unparsed_removed_test_lines(pr_body)
    assert len(unparsed) == 1
    assert parse_removed_test_markers(pr_body) == []


def test_e5_marker_glued_mid_line_by_a_swallowed_newline_still_warns():
    """The stronger instance (#2393's second recorded case): a text-replace
    ate a newline, so a CORRECTLY-FORMED marker landed mid-line rather than
    at line start. A detector that reuses `_MARKER_RE`'s line anchor would
    miss this — the same blind spot one level up. Reproduced verbatim from
    the incident, not a simplified stand-in."""
    pr_body = (
        "**Runs:** affected suites 82 passed."
        "Removed-test: tests/x.py::test_a — split into a and b\n"
        "Removed-test: tests/y.py::test_b — renamed\n"
    )
    markers = parse_removed_test_markers(pr_body)
    assert len(markers) == 1, "only the properly-line-started second marker parses"
    assert markers[0].id == "tests/y.py::test_b"

    unparsed = find_unparsed_removed_test_lines(pr_body)
    assert len(unparsed) == 1
    assert "test_a" in unparsed[0]
    assert "split into a and b" in unparsed[0]


def test_e5_a_line_start_anchored_detector_would_have_missed_the_glued_case():
    """Pins the specific defect in the FIRST drafted fix for #2393 (caught
    before it shipped): `^\\s*removed-test:` as the "did this line mention
    the key" detector inherits the parser's own anchor, so it can only ever
    re-diagnose a marker that already starts its line — exactly the shape
    that never needed the diagnostic. A regression here means someone
    reintroduced the anchored version."""
    import re
    glued_line = (
        "**Runs:** affected suites 82 passed."
        "Removed-test: tests/x.py::test_a — split into a and b"
    )
    anchored = re.compile(r"^\s*removed-test:", re.IGNORECASE)
    assert not anchored.match(glued_line), (
        "sanity check on the OLD detector shape — it must not match this "
        "line, or this test is not exercising what it claims to")
    assert find_unparsed_removed_test_lines(glued_line + "\n") == [glued_line]


def test_e5_a_correctly_parsing_marker_never_warns():
    """The negative control: a marker that parses fine must not ALSO show up
    as an unparsed line — the two detectors would otherwise double-report
    the same good-faith line."""
    pr_body = "Removed-test: tests/x.py::test_a — split into a and b\n"
    assert find_unparsed_removed_test_lines(pr_body) == []


def test_e5_no_removed_test_mention_yields_no_unparsed_lines():
    assert find_unparsed_removed_test_lines("") == []
    assert find_unparsed_removed_test_lines("Just a normal PR description.\n") == []


def test_e5_unparsed_line_inside_backticks_is_not_flagged():
    """Same illustrative-example discipline as the parser itself
    (test_backticked_example_marker_does_not_account_for_a_real_disappearance):
    a malformed example inside backticks is documentation, not an attempt,
    and must not warn."""
    pr_body = "See the format: `Removed-test: none (renamed)`.\n"
    assert find_unparsed_removed_test_lines(pr_body) == []


# ---------------------------------------------------------------------------
# E6 — a markdown-bulleted Removed-test line (#2421, the third confirmed
# variant of the SAME anchor gap #2393 fixed for lines 1 and 2 -- hit live
# on the v0.5.0 release PR, 15 markers, all 15 dropped).
#
# Per the explicit lesson on the issue: drive the parser against all four
# forms (plain, '- ', '* ', indented bullet) rather than reading the regex
# -- a fix tested only against the plain form passes its own tests and
# changes nothing, which is exactly the trap the #2393 round already
# sprang once.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["", "- ", "* ", "+ ", "  - "])
def test_e6_bulleted_removed_test_line_now_parses(prefix):
    pr_body = f"{prefix}Removed-test: tests/x.py::test_a — split into a and b\n"
    markers = parse_removed_test_markers(pr_body)
    assert len(markers) == 1, f"prefix {prefix!r} still drops the marker"
    assert markers[0].id == "tests/x.py::test_a"
    assert markers[0].reason == "split into a and b"


def test_e6_fifteen_bulleted_markers_all_parse_like_the_release_pr_incident():
    """Reproduces the actual incident shape: many markers, each its own
    bullet, in one PR body."""
    pr_body = "\n".join(
        f"- Removed-test: tests/x.py::test_{i} — consolidated"
        for i in range(15)
    )
    markers = parse_removed_test_markers(pr_body)
    assert len(markers) == 15
    assert {m.id for m in markers} == {f"tests/x.py::test_{i}" for i in range(15)}


def test_e6_bulleted_marker_now_accounts_for_the_disappearance():
    """End-to-end: the actual accounting outcome, not just the parse --
    this is the difference between 'ten disappeared test ids not accounted
    for' and a clean PASS."""
    base_ids = {"tests/x.py::test_a"}
    head_ids = set()
    markers = parse_removed_test_markers(
        "- Removed-test: tests/x.py::test_a — consolidated\n"
    )
    report = evaluate(base_ids, head_ids, markers)
    assert report.passed
    assert report.unaccounted == ()


def test_e6_bulleted_marker_no_longer_appears_as_unparsed():
    """The widened anchor is shared by both detectors (#2421's design
    choice) -- a bulleted marker that now parses must not ALSO warn as
    unparsed, or a caller sees both a clean accounting AND a spurious
    'did not parse' annotation for the same line."""
    for prefix in ("- ", "* ", "+ "):
        pr_body = f"{prefix}Removed-test: tests/x.py::test_a — split into a and b\n"
        assert find_unparsed_removed_test_lines(pr_body) == []


def test_e6_variant_1_and_2_still_warn_regardless_of_bullet_prefix():
    """The widened anchor must not accidentally swallow the OTHER two
    variants -- a bulleted line that is ALSO malformed in one of the
    original ways must still warn, not silently parse-as-something-else."""
    no_separator_bulleted = "- Removed-test: none (renamed)\n"
    assert parse_removed_test_markers(no_separator_bulleted) == []
    assert len(find_unparsed_removed_test_lines(no_separator_bulleted)) == 1

    glued_mid_line = (
        "**Runs:** affected suites 82 passed."
        "Removed-test: tests/x.py::test_a — split into a and b\n"
    )
    assert parse_removed_test_markers(glued_mid_line) == []
    assert len(find_unparsed_removed_test_lines(glued_mid_line)) == 1


def test_e6_bullet_marker_is_case_insensitive_key_still_holds():
    pr_body = "- removed-TEST: tests/x.py::test_a — case insensitive key\n"
    markers = parse_removed_test_markers(pr_body)
    assert len(markers) == 1
    assert markers[0].id == "tests/x.py::test_a"


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


# ---------------------------------------------------------------------------
# #2381 — tip-as-base fabricates disappearances; the non-ancestor warning
#
# A deliberate departure from this file's own "no git, no tmp repos" design
# (see the module docstring above): everything else in this suite tests pure
# functions with inline source strings, but the bug this section guards
# against is inherently about git ancestry between two real revisions, which
# cannot be represented without a real repo. THE POSITIVE CONTROL IS STILL
# THE POINT: the case below reproduces the exact fabricated-disappearance
# shape (Ariadne, PR#2380 review, engram-alpha#2381) with a real git history,
# not merely a code path that runs without raising.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_ancestry_fixture(tmp_path: Path) -> dict[str, str]:
    """Build a real repo reproducing #2381's shape: a `dev`-tip commit that
    has moved past the point a `feature` branch was cut from, by adding a
    brand-new test file dev never had when feature branched. Returns the
    three revisions under test: `merge_base` (the true common ancestor),
    `dev_tip` (NOT an ancestor of feature — this is the trap), and
    `feature_tip` (the PR branch being checked, unaffected by the trap)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_existing.py").write_text("def test_a():\n    assert True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    merge_base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # feature branch: cut here, makes an UNRELATED change (never touches the
    # test-id set at all) -- the branch that "deletes nothing".
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "README.md").write_text("unrelated change\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feature: unrelated change")
    feature_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # dev advances past the branch point with a genuinely NEW test file --
    # the sibling PR that merged in after feature was cut.
    _git(repo, "checkout", "-q", merge_base)
    _git(repo, "checkout", "-q", "-b", "dev")
    (tests_dir / "test_new_on_dev.py").write_text("def test_new():\n    assert True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "dev: new test file, unrelated to feature")
    dev_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    return {"repo": str(repo), "merge_base": merge_base, "dev_tip": dev_tip, "feature_tip": feature_tip}


def test_2381_dev_tip_as_base_is_not_an_ancestor_and_warns(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The exact trap: passing dev's tip (not the merge-base) as --base.
    warn_if_base_is_not_ancestor must fire, naming the REAL merge-base."""
    fx = _make_ancestry_fixture(tmp_path)
    warn_if_base_is_not_ancestor(Path(fx["repo"]), fx["dev_tip"], fx["feature_tip"])
    captured = capsys.readouterr()
    assert "::warning::" in captured.err
    assert "#2381" in captured.err
    assert fx["merge_base"] in captured.err, (
        "the warning must name the ACTUAL merge-base so the reader has the "
        "exact fix in hand, not merely a notice that something is wrong"
    )


def test_2381_merge_base_as_base_is_an_ancestor_and_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The correct invocation must never warn -- a false positive here would
    train readers to ignore the warning entirely."""
    fx = _make_ancestry_fixture(tmp_path)
    warn_if_base_is_not_ancestor(Path(fx["repo"]), fx["merge_base"], fx["feature_tip"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_2381_cli_with_dev_tip_fabricates_disappearances_but_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """End-to-end reproduction of Ariadne's #2381 report through main()
    itself: --base dev_tip reports a FAKE disappearance (test_new_on_dev.py
    never existed on feature, but feature never deleted it either -- dev
    simply gained it after the branch point) alongside the warning that
    explains why. The warning does not change the exit code -- it is
    strictly advisory, per the module docstring's explicit design."""
    fx = _make_ancestry_fixture(tmp_path)
    exit_code = main(["--base", fx["dev_tip"], "--head", fx["feature_tip"], "--repo-root", fx["repo"]])
    captured = capsys.readouterr()
    assert "::warning::" in captured.err
    assert fx["merge_base"] in captured.err
    assert "tests/test_new_on_dev.py::test_new" in captured.out, (
        "confirms the trap actually reproduces: a fabricated disappearance "
        "for a test the feature branch never touched"
    )
    assert exit_code == 1, "the fabricated disappearance still fails the gate -- this is the bug, not a non-bug"


def test_2381_cli_with_merge_base_passes_cleanly_and_silently(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The correct invocation, same branches: no warning, no fabricated
    disappearance, clean PASS. Also stands in for CI's own invocation shape
    (a real merge-base SHA is an ancestor by construction) -- an explicit
    assertion that the warning never fires there, per Ariadne's #2381 note,
    so nobody later 'fixes' the CI workflow into the warning path."""
    fx = _make_ancestry_fixture(tmp_path)
    exit_code = main(["--base", fx["merge_base"], "--head", fx["feature_tip"], "--repo-root", fx["repo"]])
    captured = capsys.readouterr()
    assert "::warning::" not in captured.err
    assert captured.err == ""
    assert exit_code == 0
    assert "PASS" in captured.out


def _make_orphan_fixture(tmp_path: Path) -> dict[str, str]:
    """Two commits sharing NO common ancestor at all -- distinct from
    `_make_ancestry_fixture`'s divergent-but-related branches. Measured
    (Ariadne, #2381): `git merge-base` on two such refs exits 1 with EMPTY
    stdout, which the empty-blank-warning guard exists to catch."""
    repo = tmp_path / "orphan_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")

    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main history")
    main_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    _git(repo, "rm", "-rf", "-q", ".")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_b.py").write_text("def test_b():\n    assert True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "unrelated history")
    orphan_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    return {"repo": str(repo), "main_tip": main_tip, "orphan_tip": orphan_tip}


def test_2381_unrelated_histories_have_no_merge_base_and_return_none(tmp_path: Path) -> None:
    """`_merge_base` must return None (not an empty string) for two commits
    with no common ancestor -- distinguishing 'no merge-base exists' from
    'merge-base is empty' is exactly the guard the blank-interpolation
    hazard needs."""
    fx = _make_orphan_fixture(tmp_path)
    assert _is_ancestor(Path(fx["repo"]), fx["orphan_tip"], fx["main_tip"]) is False
    assert _merge_base(Path(fx["repo"]), fx["orphan_tip"], fx["main_tip"]) is None


def test_2381_unrelated_histories_warn_without_a_blank_sha(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The actual hazard Ariadne flagged: naming a merge-base that does not
    exist must not print a blank where the SHA belongs -- it must say
    plainly that there is no common ancestor."""
    fx = _make_orphan_fixture(tmp_path)
    warn_if_base_is_not_ancestor(Path(fx["repo"]), fx["orphan_tip"], fx["main_tip"])
    captured = capsys.readouterr()
    assert "::warning::" in captured.err
    assert "no common ancestor" in captured.err
    assert "--base " not in captured.err.split("no common ancestor")[-1], (
        "must not suggest a re-run command when there is no merge-base to suggest"
    )


def test_2381_is_ancestor_returns_none_on_an_invalid_revision(tmp_path: Path) -> None:
    """Robustness, not a claim about reachability via the CLI: main() never
    reaches this path today (read_sources_at_rev's ls-tree call fails first
    on a bad rev), but `_is_ancestor` itself must not raise if ever called
    directly with one, since a raised exception is a much worse failure
    mode for an advisory-only check than silently returning None."""
    fx = _make_ancestry_fixture(tmp_path)
    assert _is_ancestor(Path(fx["repo"]), "deadbeef" * 5, fx["feature_tip"]) is None


# ---------------------------------------------------------------------------
# #2539 / #2540 -- a legitimate triple-backtick fence around Removed-test
# markers was silently swallowed by the single-backtick-span strip (the
# fence's three backticks read as one-and-a-half spans: the first two pair
# off empty, the third dangles and pairs with the *closing* fence's first
# backtick, taking everything between the two fences with it). Fixed by
# `_split_out_fenced_code_blocks` / `_strip_inline_code_preserving_fences`,
# which leave fenced content untouched before the inline-code strip runs.
#
# The fixture below is a reconstruction, not the literal PR #2538 body the
# issues cite: PR #2538's body was edited (unfenced) by its author shortly
# after the incident, and the GitHub REST API does not expose prior PR-body
# revisions, so the exact original text is not recoverable. It reproduces
# the same SHAPE both issues describe -- a fenced block wrapping several
# `Removed-test:` lines -- with the same 7-marker COUNT as the real
# incident (PR #2538's live body still carries exactly 7 unfenced
# `Removed-test:` lines today, independently re-confirmed), not just a
# same-shape-smaller-count stand-in. Independently verified against the
# unpatched (pre-fix) code to reproduce a 7-parsed-as-0 failure with this
# exact fixture before checking it here on the fix.
# ---------------------------------------------------------------------------


_FENCED_MARKERS_BODY = (
    "## What\n\n"
    "Removed 7 stale tests during the cleanup.\n\n"
    "```\n"
    "Removed-test: tests/test_mod.py::test_a — superseded by test_b\n"
    "Removed-test: tests/test_mod.py::test_c — duplicate of test_d\n"
    "Removed-test: tests/test_mod.py::TestX::test_e — merged into test_f\n"
    "Removed-test: tests/test_mod.py::TestX::test_g — obsolete fixture\n"
    "Removed-test: tests/test_other.py::test_h — flaky, replaced\n"
    "Removed-test: tests/test_other.py::test_i — flaky, replaced\n"
    "Removed-test: tests/test_other.py::test_j — no longer applicable\n"
    "```\n\n"
    "See the PR diff for details.\n"
)


def test_2539_fenced_markers_all_parse():
    markers = parse_removed_test_markers(_FENCED_MARKERS_BODY)
    assert [m.id for m in markers] == [
        "tests/test_mod.py::test_a",
        "tests/test_mod.py::test_c",
        "tests/test_mod.py::TestX::test_e",
        "tests/test_mod.py::TestX::test_g",
        "tests/test_other.py::test_h",
        "tests/test_other.py::test_i",
        "tests/test_other.py::test_j",
    ]


def test_2539_fenced_markers_account_for_real_disappearances():
    base_ids = {
        "tests/test_mod.py::test_a",
        "tests/test_mod.py::test_c",
        "tests/test_mod.py::TestX::test_e",
        "tests/test_mod.py::TestX::test_g",
        "tests/test_other.py::test_h",
        "tests/test_other.py::test_i",
        "tests/test_other.py::test_j",
        "tests/test_mod.py::test_kept",
    }
    head_ids = {"tests/test_mod.py::test_kept"}
    markers = parse_removed_test_markers(_FENCED_MARKERS_BODY)
    report = evaluate(base_ids, head_ids, markers)
    assert report.passed, report.unaccounted


def test_2539_fenced_markers_leave_no_unparsed_warning():
    # The #2393 fail-loud warning path must not fire for markers that
    # parse cleanly from inside a proper fence. Asserting markers are
    # non-empty in THIS test (not just in the sibling
    # test_2539_fenced_markers_all_parse) matters: without it, a
    # regression that re-introduces the fence-swallow bug would ALSO
    # pass this assertion vacuously -- a fully-swallowed fenced region
    # removes the "removed-test:" substring along with everything else,
    # so the per-line scan finds nothing to flag either, for the wrong
    # reason (reviewer-fairy catch on PR #2628 round 1, confirmed by
    # mutation-testing against the pre-fix regex).
    markers = parse_removed_test_markers(_FENCED_MARKERS_BODY)
    assert len(markers) == 7
    assert find_unparsed_removed_test_lines(_FENCED_MARKERS_BODY) == []


def test_2539_tilde_fence_also_preserved():
    # A body with NO backtick anywhere (the original shape of this test)
    # passes under every configuration -- the pre-fix code, the fix, and
    # the fix with tilde support removed entirely -- because the naive
    # strip has nothing to swallow regardless of whether the fence is
    # even recognized (Ariadne's colleague-review catch on PR #2628,
    # same class as the unterminated-fence fixture's own fix above: a
    # test needs an UNPAIRED backtick straddling the fence to actually
    # discriminate). An even count of backticks outside the fence would
    # also fail to discriminate -- they pair off normally either way --
    # so this needs an ODD one specifically: `x` (one backtick, unclosed)
    # supplies it, and the closing `y` afterward is deliberately a
    # SEPARATE, already-closed span, not part of counting toward that
    # oddness.
    body = (
        "Note `x\n"
        "~~~\n"
        "Removed-test: tests/test_mod.py::test_a — cleanup\n"
        "~~~\n"
        "and `y` after.\n"
    )
    markers = parse_removed_test_markers(body)
    assert [m.id for m in markers] == ["tests/test_mod.py::test_a"]


def test_2539_language_tagged_fence_also_preserved():
    body = (
        "```text\n"
        "Removed-test: tests/test_mod.py::test_a — cleanup\n"
        "```\n"
    )
    markers = parse_removed_test_markers(body)
    assert [m.id for m in markers] == ["tests/test_mod.py::test_a"]


def test_2539_unterminated_fence_extends_to_end_of_body():
    # CommonMark's own rule: an unclosed fence implicitly closes at the end
    # of the document. The marker inside must still parse, not be treated
    # as "outside any fence" (which would re-expose it to the single-
    # backtick swallow LIMIT below via whatever comes after it, in a real
    # body that continues past this point).
    #
    # The trailing paragraph with a real inline-code span is load-bearing
    # for this test, not decoration (reviewer-fairy catch on PR #2628
    # round 1): with nothing after the marker, the dangling opening
    # backtick has no OTHER backtick anywhere in the document to pair
    # with, so even the PRE-FIX naive regex leaves the marker alone by
    # accident -- this test would pass unchanged under the very bug it
    # claims to guard against. Confirmed by mutation-testing: reverting to
    # the pre-fix regex, a body ending right after the marker still
    # parses correctly (false negative), while THIS body (marker,
    # unterminated fence, then a real single-backtick span afterward)
    # correctly fails under the pre-fix regex and passes only with the
    # fix -- proving the EOF-closing rule is what's actually being tested.
    body = (
        "```\n"
        "Removed-test: tests/test_mod.py::test_a — cleanup\n"
        "\n"
        "See `option_foo` for details.\n"
    )
    markers = parse_removed_test_markers(body)
    assert [m.id for m in markers] == ["tests/test_mod.py::test_a"]


def test_2539_stray_backtick_typo_outside_a_fence_still_swallows_by_design():
    """The fix narrows the LIMIT to non-fenced text; it does not remove it.
    Identical shape to test_stray_unterminated_backtick_swallows_a_later_
    marker_by_design above, confirming the fence carve-out did not
    accidentally make every single backtick fence-like."""
    base_ids = {"tests/test_mod.py::test_a", "tests/test_mod.py::test_b"}
    head_ids = {"tests/test_mod.py::test_a"}
    body = (
        "See the config option `enable_foo (typo, unclosed)\n"
        "\n"
        "Removed-test: tests/test_mod.py::test_b — obsolete after refactor"
        "\n"
        "Also see `this real inline code`.\n"
    )
    markers = parse_removed_test_markers(body)
    assert markers == []
    report = evaluate(base_ids, head_ids, markers)
    assert not report.passed
    assert report.unaccounted == ("tests/test_mod.py::test_b",)
