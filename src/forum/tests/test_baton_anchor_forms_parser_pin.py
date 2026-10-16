"""#3394: pin ``baton_gate.GITHUB_ANCHOR_ACCEPTED_FORMS`` to what the parser accepts.

Written BEFORE any check was green (contract-first). The sibling file
test_baton_anchor_forms_constant.py pins that every refusal surface *uses* the
constant; it does not pin that the constant *says what the parser accepts*
(mutating the constant to drop the owner/repo form left all 17 of its tests green).

Contract:
  1. every form the constant names, instantiated with synthetic values, is accepted
     by BOTH ``_GITHUB_ANCHOR_RE`` and ``_parse_github_anchor``. Instances are derived
     from the constant's text, so a form added to or dropped from it is noticed.
     (``pr/<N>`` is named by the constant and accepted at the PARSE layer; callers
     refuse-and-count it afterwards -- that refusal is out of scope here.)
  2. a table of rejected shapes is refused by BOTH, and none is named by the constant.
  3. regex and parser agree on every row of both tables.
"""

import re
import sys
from pathlib import Path

import pytest

# append (not insert(0)): tools/ also holds a flat forum.py that must not shadow the package (#2063).
_TOOLS = str(Path(__file__).resolve().parents[3] / "src" / "engram" / "tools")
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)

import baton_gate  # noqa: E402

_PLACEHOLDERS = {"<owner>": "o", "<repo>": "r", "<N>": "1"}
# Boundary values: max digit count (15), and every character the owner/repo class allows
# (word chars, '.', '-'; dots are real, e.g. foo.github.io). Minimal values alone leave a
# narrowed digit limit or character class invisible.
_BOUNDARY_PLACEHOLDERS = {"<owner>": "a-b_c.d", "<repo>": "x.y-z_1", "<N>": "9" * 15}
# #3568: the owner of ``project/<owner>/<N>`` follows the GitHub LOGIN rule (1-39 chars,
# alnum or hyphen, no leading hyphen) -- narrower than a PR's owner/repo class (no '.'
# or '_'). Its boundary instance is the 39-char maximum with an inner hyphen.
_PROJECT_BOUNDARY_PLACEHOLDERS = {
    "<owner>": "a-" + "b" * 37, "<repo>": "x.y-z_1", "<N>": "9" * 15,
}


def _named_forms():
    """Split the constant's prose ('A, B, or C') into its form templates."""
    text = baton_gate.GITHUB_ANCHOR_ACCEPTED_FORMS
    parts = [p.strip() for p in re.split(r",|\bor\b", text)]
    return [p for p in parts if p]


def _instantiate(template, placeholders=_PLACEHOLDERS):
    unknown = set(re.findall(r"<[^>]*>", template)) - set(placeholders)
    assert not unknown, f"constant uses placeholder(s) this pin cannot instantiate: {unknown}"
    out = template
    for ph, val in placeholders.items():
        out = out.replace(ph, val)
    return out


def _accepted_instances():
    return [_instantiate(t) for t in _named_forms()]


def _boundary_instances():
    return [
        _instantiate(t, _PROJECT_BOUNDARY_PLACEHOLDERS if t.startswith("project/")
                     else _BOUNDARY_PLACEHOLDERS)
        for t in _named_forms()
    ]


REJECTED = [
    "",
    "pr/",
    "pr",
    "pr/abc",
    "pr/o/r/",
    "pr/o/1",
    "pr/o/r/x/1",
    "project/",
    "project/abc",
    "project/o/r/1",
    "project/o/",
    "project//1",
    "project/-o/1",
    "project/o.x/1",
    "project/o_x/1",
    "project/" + "a" * 40 + "/1",
    "project/o/" + "1" * 16,
    "issue/1",
    "/pr/1",
    "pr/1/",
    "project/1/",
    " pr/1",
    "pr/1 ",
    "pr /1",
    "PR/1",
    "pr/" + "1" * 16,
    "pr/١",  # Arabic-Indic digit
]


def _regex_ok(v):
    return baton_gate._GITHUB_ANCHOR_RE.match(v) is not None


def _parser_ok(v):
    return baton_gate._parse_github_anchor(v) is not None


def test_constant_is_machine_readable_and_names_the_known_shapes():
    # Guards the derivation itself: if the prose stops splitting into forms, fail loudly
    # rather than silently pinning nothing.
    forms = _accepted_instances()
    assert forms, "derived zero forms from the constant"
    assert {"pr/1", "pr/o/r/1", "project/1", "project/o/1"} <= set(forms), forms


def test_every_named_form_is_accepted_by_regex_and_parser():
    bad = [v for v in _accepted_instances() if not (_regex_ok(v) and _parser_ok(v))]
    assert not bad, f"named by the constant but not accepted by both regex and parser: {bad}"


def test_every_named_form_is_accepted_at_boundary_values():
    # Second instantiation with boundary values (15 digits; '.', '-', '_' in owner/repo).
    insts = _boundary_instances()
    assert insts and insts != _accepted_instances(), "boundary instantiation is not distinct"
    for v in insts:
        assert _regex_ok(v), f"regex rejects boundary instance {v!r}"
        assert _parser_ok(v), f"parser rejects boundary instance {v!r}"


@pytest.mark.parametrize("value", REJECTED, ids=repr)
def test_rejected_shape_is_refused_by_regex_and_parser_and_not_named(value):
    assert not _regex_ok(value), f"regex accepts {value!r}"
    assert not _parser_ok(value), f"parser accepts {value!r}"
    assert value not in _accepted_instances(), f"constant names rejected shape {value!r}"


# Known quirk, deliberately NOT asserted as accept-or-reject: both patterns end in ``$``,
# which also matches before a single trailing newline, so ``pr/1\n`` is accepted by
# regex and parser alike. Only their AGREEMENT is pinned (contract 3), not the quirk.
AGREEMENT_ONLY = ["pr/1\n", "project/1\n", "pr/o/r/1\n"]


@pytest.mark.parametrize(
    "value", sorted(set(REJECTED) | set(_accepted_instances()) | set(_boundary_instances()) | set(AGREEMENT_ONLY)), ids=repr
)
def test_regex_and_parser_agree(value):
    assert _regex_ok(value) == _parser_ok(value), (
        f"regex={_regex_ok(value)} parser={_parser_ok(value)} for {value!r}"
    )
