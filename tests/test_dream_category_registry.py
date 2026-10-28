"""The dream-category registry spans three files that nothing previously tied together.

`tools/dream_lottery.py` numbers the lottery-rotated scan categories. The
dream-fairy agent spec numbers its *heuristic* items in the SAME visual
sequence, continuing past the last lottery category. The sleep skill names
the heuristic numbers literally, to tell a per-category fairy what NOT to run.

Nothing checked that those three agree. Adding a lottery category silently
pushes it into the heuristic numbering, and the sleep skill then instructs a
fairy dispatched FOR that category not to run it -- an instruction that is
wrong in a way no existing test, and no amount of reading one file, exposes.

These tests are the gate. They fail on the three-way mismatch, not on any
single file's contents.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
FAIRY_MD = REPO / "src/engram/agents/claude/engram-dream-fairy.md"
SLEEP_MD = REPO / "src/engram/skills/claude/engram-sleep/SKILL.md"
LOTTERY_PY = REPO / "tools/dream_lottery.py"

# Categories with no "## Category N —" output-contract section, each with its
# OWN reason. Category 9 used to be listed here as a pre-existing gap
# (engram-alpha#2879); it now has its section and is enforced like the rest.
# Never re-add an entry for a category this fairy scans: filing a real gap as
# intentional is worse than having no exemption at all.
NO_OUTPUT_CONTRACT_SECTION = {
    10: "Owned by engram-person-node-fairy in the shared lottery namespace — "
        "correctly absent from THIS agent's spec.",
}


def _lottery_categories() -> dict[int, str]:
    spec = importlib.util.spec_from_file_location("_dream_lottery", LOTTERY_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod.CATEGORIES)


def _heuristic_item_numbers() -> set[int]:
    """Numbers of the agent spec's heuristic items (its own numbered list)."""
    text = FAIRY_MD.read_text(encoding="utf-8")
    marker = "## Heuristic (medium-confidence"
    assert marker in text, "heuristic section heading moved; this gate needs updating"
    tail = text[text.index(marker):]
    return {int(n) for n in re.findall(r"^(\d+)\.\s+\*\*", tail, re.M)}


def _sleep_skill_excluded_numbers() -> set[int]:
    text = SLEEP_MD.read_text(encoding="utf-8")
    m = re.search(r"heuristic categories \(([\d,\s]+)\)", text)
    assert m, "sleep skill no longer names the heuristic categories numerically"
    return {int(x) for x in m.group(1).replace(" ", "").split(",") if x}


def test_lottery_categories_do_not_collide_with_heuristic_item_numbers():
    """The collision that motivated this file.

    A lottery category sharing a number with a heuristic item makes every
    downstream 'do not run the heuristic categories (N, M)' instruction
    ambiguous at best and self-contradicting at worst.
    """
    collisions = set(_lottery_categories()) & _heuristic_item_numbers()
    assert not collisions, (
        f"lottery category number(s) {sorted(collisions)} also number a heuristic "
        f"item in {FAIRY_MD.name}. Renumber the heuristic items past the last "
        f"lottery category and update the sleep skill's exclusion line to match."
    )


def test_sleep_skill_exclusion_line_matches_the_agent_specs_heuristic_numbers():
    """The actual bug this gate exists for.

    The sleep skill hardcodes the heuristic numbers. When the agent spec
    renumbers, a stale line here tells a fairy dispatched for a real category
    to skip itself -- and the fairy obeys, reporting an honest empty scan.
    """
    assert _sleep_skill_excluded_numbers() == _heuristic_item_numbers(), (
        f"{SLEEP_MD.name} excludes {sorted(_sleep_skill_excluded_numbers())} but "
        f"{FAIRY_MD.name} numbers its heuristic items "
        f"{sorted(_heuristic_item_numbers())}. These must agree exactly."
    )


def test_every_scanned_lottery_category_has_an_output_contract_section():
    """A category the lottery can draw but the agent cannot report is a silent
    no-op: the fairy is dispatched, finds no section, and returns nothing."""
    text = FAIRY_MD.read_text(encoding="utf-8")
    documented = {int(n) for n in re.findall(r"^## Category (\d+) —", text, re.M)}
    expected = set(_lottery_categories()) - set(NO_OUTPUT_CONTRACT_SECTION)
    missing = expected - documented
    assert not missing, (
        f"lottery categories {sorted(missing)} are drawable but have no "
        f"'## Category N —' output-contract section in {FAIRY_MD.name}"
    )


def test_output_contract_sections_do_not_outrun_the_lottery():
    """The reverse drift: a documented category the lottery can never draw."""
    text = FAIRY_MD.read_text(encoding="utf-8")
    documented = {int(n) for n in re.findall(r"^## Category (\d+) —", text, re.M)}
    orphaned = documented - set(_lottery_categories())
    assert not orphaned, (
        f"{FAIRY_MD.name} documents category/categories {sorted(orphaned)} that "
        f"tools/dream_lottery.py cannot draw"
    )


def test_default_state_covers_every_category():
    """Weight-aging state is keyed by category; a missing key means the new
    category never ages and is effectively never drawn."""
    spec = importlib.util.spec_from_file_location("_dream_lottery2", LOTTERY_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state = mod.default_state()
    missing = {n for n in mod.CATEGORIES if str(n) not in state["types"]}
    assert not missing, f"default_state() omits categories {sorted(missing)}"


def _sleep_skill_dispatch_table_rows() -> dict[int, str]:
    """Category number -> slug, from the sleep skill's Step 6b dispatch table.

    Delimited by the TABLE'S OWN ROWS: the slice starts at the header row and
    ends at the first line that is not a table row. Nothing in the surrounding
    document can widen it.

    Earned six times. Every earlier revision scoped by naming one more literal
    the document happened to use -- the prose exclusion line rather than the
    canonical table; the whole file rather than the section; "### ", missing
    the bold sub-step; "**Step ", missing bold NON-Step lead-ins; a trailing
    blank line, which does not fail loud when removed because find() locates
    the NEXT one and widens silently. Each was correct about the case in front
    of it and narrowed the hole (261 lines of exposure to 38) while leaving
    the class intact.

    TWO honest bounds, both found by mutating rather than by reasoning:

    1. The ANCHOR is itself a literal, so the property earned is "no literal
       AFTER the anchor", not "no literal to miss".
    2. A VERBATIM stale copy of this table planted earlier in the file
       captures the slice and is NOT caught -- not by the uniqueness assert
       (the copy replaces rather than duplicates the header) and not by the
       caller's slug check (an exact copy carries the correct slugs too).
       Verified by planting one. Only POSITION distinguishes two byte-identical
       tables, and position means anchoring on section literals, which is the
       approach whose five successive failures produced this design.

    So the slug check closes the WRONG-CONTENT decoy, not the EXACT-COPY one.
    That residual is recorded rather than closed, because closing it trades a
    demonstrated-narrow hole for a class with five demonstrated instances.
    """
    text = SLEEP_MD.read_text(encoding="utf-8")
    header = "| `{N}` | `{CATEGORY_NAME}` | `{SLUG}` |"
    assert text.count(header) == 1, (
        f"expected exactly one dispatch-table header row in {SLEEP_MD.name}, "
        f"found {text.count(header)} -- the anchor is ambiguous, re-scope it"
    )
    rows: list[str] = []
    for line in text[text.index(header):].split("\n"):
        if not line.startswith("|"):
            break
        rows.append(line)
    assert len(rows) >= 3, (
        f"dispatch table has {len(rows)} contiguous table lines; expected the "
        f"header, its delimiter and at least one category row -- re-scope it"
    )
    out: dict[int, str] = {}
    for line in rows:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].isdigit():
            out[int(cells[0])] = cells[-1]
    return out


def test_every_lottery_category_has_a_dispatch_table_row():
    """The gap that got past the first version of this file.

    The sleep skill's Step 6b table is the CANONICAL source for a dispatched
    fairy's category name and slug — the prose above it says so explicitly and
    tells the reader not to derive them from memory. A lottery category with no
    row cannot be dispatched: there is nothing to substitute for
    {CATEGORY_NAME} and {SLUG}.

    The original fix here edited the heuristic-exclusion line two lines BELOW
    this table and left the table itself alone — exactly the drift the rest of
    this file exists to prevent. Caught by a colleague, not by the gate, so the
    gate now covers it.
    """
    lottery = _lottery_categories()
    table = _sleep_skill_dispatch_table_rows()
    missing = set(lottery) - set(table)
    assert not missing, (
        f"lottery categories {sorted(missing)} have no row in "
        f"{SLEEP_MD.name}'s Step 6b dispatch table, so they cannot be "
        f"dispatched with a name and slug"
    )
    # SLUGS only. Two bounds, both stated wrongly in earlier drafts of this
    # same comment and both corrected here after being measured:
    #
    # 1. It does NOT check the NAME column -- but NOT, as a previous draft
    #    claimed, because no authority exists. One does: engram-dream-fairy.md
    #    carries `## Category N -- <name>` for 10 of 12 categories, and the
    #    prose directly above this table in SKILL.md already designates that
    #    file canonical for exactly this column. This test file ALREADY parses
    #    that heading regex twice, capturing the number and discarding the
    #    name. So the column is unchecked because nobody wired it, not for
    #    want of a source. Tracked as engram-alpha#2880. It matters because
    #    SKILL.md interpolates {CATEGORY_NAME} into every dispatched fairy's
    #    prompt -- and the drift is already live: category 2 reads "ripe" in
    #    the table and "ready" in the agent spec, both on dev, so wiring the
    #    check requires reconciling that row first.
    #
    # 2. A DUPLICATED copy of this table IS caught -- the header count goes to
    #    2 and the uniqueness assert fires. The uncaught case is narrower: a
    #    copy that REPLACES the header, leaving the count at 1. The helper
    #    docstring states this precisely; a previous draft of this comment
    #    dropped the qualifier and was false as written.
    wrong = {n: (table[n], lottery[n]) for n in lottery if table.get(n) != lottery[n]}
    assert not wrong, (
        f"dispatch-table slug mismatch {wrong} (table, lottery) -- the gate may "
        f"be anchored on a stale copy of the table rather than the live one"
    )


# --- Category 9 contract (engram-alpha#2879) --------------------------------

def _category_9_output_section() -> str:
    """Text of the '## Category 9 —' section, up to the next '## ' heading."""
    text = FAIRY_MD.read_text(encoding="utf-8")
    m = re.search(r"^## Category 9 — .*?(?=^## )", text, re.M | re.S)
    assert m, f"{FAIRY_MD.name} has no '## Category 9 —' output-contract section"
    return m.group(0)


def _scan_item_9() -> str:
    """Text of scan item 9, up to scan item 11."""
    text = FAIRY_MD.read_text(encoding="utf-8")
    m = re.search(r"^9\. \*\*Cornerstone auto-load.*?(?=^11\. \*\*)", text, re.M | re.S)
    assert m, "scan item 9 not found (or item 11 no longer follows it)"
    return m.group(0)


def test_cat9_section_names_the_count_of_record():
    sec = _category_9_output_section()
    assert "cornerstone_coverage" in sec
    assert "engram_diagnose" in sec


def test_cat9_c1_denominator_line_required_even_with_no_gaps():
    sec = _category_9_output_section()
    assert "count of record (engram_diagnose.cornerstone_coverage):" in sec
    assert "<covered>/<total> covered, <uncovered> uncovered; surfaces_checked:" in sec
    assert "REQUIRED" in sec and "even when" in sec


def test_cat9_c2_gap_lines_come_only_from_uncovered_list():
    sec = _category_9_output_section()
    assert "uncovered_list" in sec
    assert "(verified)" in sec
    assert "suggested action: add an anchor for cs_XXXX on an auto-load surface" in sec


def test_cat9_c3_instrument_failure_is_reported_as_failure():
    sec = _category_9_output_section()
    assert "instrument failed: <error or 'key missing'>" in sec
    assert "NO gaps" in sec
    assert "never" in sec and "all covered" in sec


def test_cat9_c3_section_forbids_substituting_a_self_computed_count():
    # Exact phrase, so a rewrite that keeps the words "never"/"all covered"
    # but turns the ban into a fallback instruction still goes red.
    assert "never substitute a self-computed count" in _category_9_output_section()


def test_cat9_c3_item_forbids_substituting_a_count_of_your_own():
    assert "never substitute a count of your own" in _scan_item_9()


def test_cat9_c3_no_fallback_instruction_in_section_or_item():
    # Negative assertion: the plausible-wrong rewrite is "if diagnose fails, fall
    # back to grepping the surfaces yourself" (PR #4243 round-1 review).
    for name, text in (("section", _category_9_output_section()), ("item 9", _scan_item_9())):
        low = text.lower()
        assert "fall back" not in low, f"cat-9 {name} tells the fairy to fall back"
        assert "fallback" not in low, f"cat-9 {name} tells the fairy to fall back"


def test_cat9_c3_empty_surfaces_checked_is_an_instrument_failure():
    # An empty surfaces_checked means no surface file was read, so every
    # cornerstone lands in uncovered_list with no `error` key (#2879).
    for name, text in (("section", _category_9_output_section()), ("item 9", _scan_item_9())):
        # The condition phrase itself, not any mention: the section also names
        # the case in its output-template parenthetical, which must not mask a
        # deleted condition.
        assert "or has an empty `surfaces_checked`" in text, (
            f"cat-9 {name} does not name the empty-surfaces_checked failure case"
        )


def test_cat9_c4_no_slot_for_a_second_count():
    sec = _category_9_output_section().lower()
    assert "no second count" in sec


def test_cat9_c4_section_has_no_grep():
    assert "grep" not in _category_9_output_section().lower()


def test_cat9_c4_item_only_mentions_grep_in_the_forbidding_sentence():
    item = _scan_item_9()
    forbid = "do not re-derive coverage by grepping the surfaces yourself"
    assert forbid in item
    assert "grep" not in item.replace(forbid, "").lower()
    assert "```" not in item  # no fenced bash block to run


def test_cat9_item_states_null_tag_renders_as_empty_string():
    item = _scan_item_9()
    assert 'tag: ""' in item
    assert "not null" in item


def test_cat9_scan_item_uses_diagnose_not_engram_list_or_grep():
    item = _scan_item_9()
    assert "engram_diagnose" in item
    assert "engram_list(node_type='cornerstone')" not in item
    assert "grep -l" not in item
