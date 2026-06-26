"""Tests for coordination.markdown — the pure baton/board transforms.

Byte-fidelity with the original tools/baton.py helpers is load-bearing: a relocated
write must produce the SAME on-disk text the CLI did, or live batons drift. These
pin the exact output shapes.
"""

import re

from forum.coordination import markdown as md


def _baton(turn="ariadne", extra_body=""):
    return (
        "---\n"
        "project_id: PR-1\n"
        "title: a title\n"
        "status: in-progress\n"
        f"turn: {turn}\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "start"\n'
        "participants: [ariadne, borges]\n"
        "---\n"
        f"# PR-1\n\nsome body\n{extra_body}"
    )


# ---------------------------------------------------------------------------
# now_iso
# ---------------------------------------------------------------------------
def test_now_iso_format():
    s = md.now_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", s), s


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------
def test_parse_frontmatter_fields_and_body():
    fields, body = md.parse_frontmatter(_baton())
    assert fields["turn"] == "ariadne"
    assert fields["status"] == "in-progress"
    assert fields["title"] == "a title"
    assert body.startswith("# PR-1")


def test_parse_frontmatter_no_frontmatter():
    fields, body = md.parse_frontmatter("# no frontmatter\njust body\n")
    assert fields == {}
    assert body == "# no frontmatter\njust body\n"


# ---------------------------------------------------------------------------
# update_frontmatter
# ---------------------------------------------------------------------------
def test_update_frontmatter_replaces_in_place_preserving_order():
    out = md.update_frontmatter(_baton(), {"turn": "borges", "status": "in-review"})
    fields, _ = md.parse_frontmatter(out)
    assert fields["turn"] == "borges"
    assert fields["status"] == "in-review"
    # order preserved: project_id still first, participants still last
    block = md.FRONTMATTER_RE.match(out).group(1)
    keys = [l.split(":")[0] for l in block.split("\n")]
    assert keys[0] == "project_id"
    assert keys[-1] == "participants"


def test_update_frontmatter_appends_missing_key():
    out = md.update_frontmatter(_baton(), {"seq": "42"})
    fields, _ = md.parse_frontmatter(out)
    assert fields["seq"] == "42"


def test_update_frontmatter_no_frontmatter_returns_unchanged():
    text = "no frontmatter here\n"
    assert md.update_frontmatter(text, {"turn": "x"}) == text


# ---------------------------------------------------------------------------
# append_turn_log
# ---------------------------------------------------------------------------
def test_append_turn_log_creates_section_when_absent():
    body = "# PR-1\n\nsome body\n"
    out = md.append_turn_log(body, "- 2026-06-26T00:00:00Z ariadne → borges: r")
    assert md.TURN_LOG_HEADER in out
    assert out.rstrip().endswith("ariadne → borges: r")


def test_append_turn_log_appends_to_existing_section():
    body = "# PR-1\n\n## Turn log\n\n- 2026-06-26T00:00:00Z a → b: first\n"
    out = md.append_turn_log(body, "- 2026-06-26T01:00:00Z b → c: second")
    # only one header, both entries present, in order
    assert out.count(md.TURN_LOG_HEADER) == 1
    i_first = out.index("first")
    i_second = out.index("second")
    assert 0 < i_first < i_second


# ---------------------------------------------------------------------------
# reattach_frontmatter + full flip-shaped composition (byte-fidelity)
# ---------------------------------------------------------------------------
def test_flip_shaped_composition_byte_exact():
    # Replays exactly what baton.cmd_flip's write body does, asserting the
    # precise on-disk text. This is the golden shape a relocated flip must match.
    content = _baton(turn="ariadne")
    now = "2026-06-26T12:00:00Z"
    updated = md.update_frontmatter(
        content, {"turn": "borges", "turn_since": now, "turn_reason": '"please review"'}
    )
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} ariadne → borges: please review")
    final = md.reattach_frontmatter(updated, body)

    expected = (
        "---\n"
        "project_id: PR-1\n"
        "title: a title\n"
        "status: in-progress\n"
        "turn: borges\n"
        f"turn_since: {now}\n"
        'turn_reason: "please review"\n'
        "participants: [ariadne, borges]\n"
        "---\n"
        # baton._append_turn_log produces TWO blank lines before the section
        # (rstrip + "\n\n" separator + "\n## Turn log") — pinned for fidelity.
        "# PR-1\n\nsome body\n\n\n"
        "## Turn log\n\n"
        f"- {now} ariadne → borges: please review\n"
    )
    assert final == expected


def test_reattach_no_frontmatter_returns_unchanged():
    assert md.reattach_frontmatter("no fm\n", "ignored body") == "no fm\n"
