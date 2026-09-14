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
# OWN reason. Split deliberately: an earlier revision lumped both under
# "owned by other agents", which is true of 10 and FALSE of 9 — category 9 IS
# this fairy's scan (it has a scan item, and the spec's own header line reads
# "numbered 1–9, 11 and 12"), it simply never got a section. Filing them under
# one false reason encoded a real pre-existing gap as intentional, which is
# worse than having no exemption at all.
NO_OUTPUT_CONTRACT_SECTION = {
    9: "PRE-EXISTING GAP, not intentional: category 9 (coverage-gaps) is "
       "dispatched to this fairy and has a scan item, but no output-contract "
       "section. Predates this file; tracked as engram-alpha#2879. Do not read "
       "this entry as approval.",
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
