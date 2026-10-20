"""Contract tests for alpha #3403: sleep-end triage of the dream's awake-seat findings.

Written before the implementation. Pins the surfaces that make the dream
record's awake-seat items reach a reader: a numbered block in the dream
master's output contract, the parent's Step 9.5 triage in engram-sleep, and
the morning disposition check in the session-start-reading component (plus its
regenerated template).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
DREAM_MASTER = SRC / "agents" / "claude" / "engram-dream-master.md"
SLEEP_SKILL = SRC / "skills" / "claude" / "engram-sleep" / "SKILL.md"
COMPONENT = SRC / "templates" / "components" / "session-start-reading.md"
TEMPLATE = SRC / "templates" / "template.CLAUDE.md"


def _read(p):
    return p.read_text(encoding="utf-8")


def _line_index(lines, pattern):
    for i, ln in enumerate(lines):
        if re.search(pattern, ln):
            return i
    return None


def test_t1_awake_seat_header_sits_between_flagged_and_open():
    lines = _read(DREAM_MASTER).splitlines()
    awake = _line_index(lines, r"^## For the awake seat$")
    flagged = _line_index(lines, r"^## Flagged for the user")
    open_ = _line_index(lines, r"^## Open for tomorrow")
    assert awake is not None, "missing '## For the awake seat' header"
    assert flagged is not None and open_ is not None
    assert flagged < awake < open_


def test_t2_old_subbullet_gone_and_checklist_box_present():
    text = _read(DREAM_MASTER)
    assert "For the awake seat (not " not in text
    assert '"For the awake seat" written as a numbered block' in text


def test_t3_sleep_skill_has_step_9_5():
    text = _read(SLEEP_SKILL)
    lines = text.splitlines()
    head = _line_index(lines, r"^### Step 9\.5 — ")
    assert head is not None, "missing '### Step 9.5 — ' heading"
    preflight = _line_index(lines, r"^## Pre-flight")
    listed = [i for i, ln in enumerate(lines) if "Step 9.5" in ln and i < preflight]
    assert listed, "no Step 9.5 line in the step list before '## Pre-flight'"
    nxt = _line_index(lines[head + 1:], r"^(---|## )")
    body = "\n".join(lines[head: head + 1 + nxt])
    for token in ("do now", "file", "defer", "## Sleep-end triage (parent)"):
        assert token in body, f"Step 9.5 body lacks {token!r}"


def test_t4_component_revised_and_morning_check():
    text = _read(COMPONENT)
    fm = text.split("---")[1]
    m = re.search(r"^version: (\d+)$", fm, re.M)
    assert m and int(m.group(1)) >= 2
    assert "history/dream/" in text
    assert "Sleep-end triage" in text


def test_t5_template_regenerated():
    assert "Sleep-end triage" in _read(TEMPLATE)


def _step_body(text, start_pat, end_pat):
    s = re.search(start_pat, text, re.M)
    assert s, f"missing section start {start_pat!r}"
    e = re.search(end_pat, text[s.end():], re.M)
    assert e, f"missing section end {end_pat!r}"
    return text[s.start(): s.end() + e.start()]


def test_t6_exact_header_strings_in_all_three_sources():
    strings = (
        "## For the awake seat",
        "## Open for tomorrow",
        "## Sleep-end triage (parent)",
    )
    for path in (SLEEP_SKILL, DREAM_MASTER, COMPONENT):
        text = _read(path)
        for s in strings:
            assert s in text, f"{path.name} lacks exact string {s!r}"


def test_t7_step_9_5_literal_forms():
    body = _step_body(_read(SLEEP_SKILL), r"^### Step 9\.5 — ", r"^(---|## )")
    for token in ("file (#N)", "defer — ", "none (0 items)"):
        assert token in body, f"Step 9.5 body lacks {token!r}"


def test_t8_step_9_points_at_step_9_5():
    body = _step_body(_read(SLEEP_SKILL), r"^### Step 9 ", r"^### Step 9\.5")
    assert "Step 9.5" in body


def test_t9_awake_seat_items_in_both_return_lists():
    assert "awake-seat items" in _read(DREAM_MASTER)
    body = _step_body(_read(SLEEP_SKILL), r"^### Step 9 ", r"^### Step 9\.5")
    assert "awake-seat items" in body


def test_t10_step_9_5_keys_open_items_by_position():
    body = _step_body(_read(SLEEP_SKILL), r"^### Step 9\.5 — ", r"^(---|## )")
    assert "o1" in body, "open-for-tomorrow items must be keyed by position (o1, o2, ...)"
    assert "keyed by their `qu_` id" not in body


def test_t11_component_and_template_use_position_keys_and_suffix():
    for path in (COMPONENT, TEMPLATE):
        text = _read(path)
        assert "o1" in text, f"{path.name} lacks position key 'o1'"
        assert "suffix" in text, f"{path.name} lacks heading-suffix tolerance"


def test_t12_step_9_5_ships_no_concrete_node_id():
    body = _step_body(_read(SLEEP_SKILL), r"^### Step 9\.5 — ", r"^(---|## )")
    hits = re.findall(r"\b[a-z]{2}_[0-9]{4,}\b", body)
    assert not hits, f"concrete node ids in Step 9.5: {hits}"


def test_t13_master_note_qualifies_and_states_one_list():
    text = _read(DREAM_MASTER)
    assert "one list" in text
    assert "own authority" in text


def test_t14_step_9_5_tolerates_heading_suffix():
    body = _step_body(_read(SLEEP_SKILL), r"^### Step 9\.5 — ", r"^(---|## )")
    assert "suffix" in body
