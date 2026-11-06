"""Contract tests for docs/internal/METRIC-GATE-POLICY.md and the one CLAUDE.md template line (A5 of the
metricgate adoption PR). Written BEFORE the doc and the component existed.

The contract (spec A5):

  * the doc carries the policy's section 0 (the case) and section 1 (R1 to R12), a section 2 pointing at the
    library repo and tag with the install line and the gate-file convention, and a section 3 Status;
  * no agent names in the rules; the measured Welch-vs-paired table sits under R7 attributed to "the
    EngramManager design owner's measurement on real A6 cells, 2026-10-31";
  * the template gets ONE line, through the component mechanism, with no path, no attribution and no date;
  * the doc carries the failed-control paragraph under R7 and the verdict-carry paragraph under R1 (#4400).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "internal" / "METRIC-GATE-POLICY.md"
TEMPLATES = REPO_ROOT / "src" / "engram" / "templates"
COMPONENT = TEMPLATES / "components" / "metric-gate-rule.md"
MANIFEST = TEMPLATES / "manifests" / "claude-code.CLAUDE.md.json"
TEMPLATE = TEMPLATES / "template.CLAUDE.md"

LINE = "A metric gate is an interval against a pre-registered band, never a point against a line."
FRESH_DIR = ("one fresh directory per announced cell (one build, one SQLite build, one exclusion setting, one "
             "planned n); appending a 7th run to a planned-6 cell makes the cell NO_VERDICT by design")
INSTALL_LINE = 'pip install "metricgate @ git+ssh://git@github.com/engram-agents/metricgate@v0.1.0"'
AGENT_NAMES = ("Kepler", "Borges", "Ariadne", "Sol", "Clio", "Luria", "Aleph", "Mira", "Zephyr", "Halley", "Lei")


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(text: str, n: int) -> str:
    parts = re.split(r"(?m)^## ", text)
    for p in parts:
        if p.startswith(f"{n}."):
            return p
    raise AssertionError(f"no section {n}")


def test_the_doc_has_the_four_sections():
    """Mutation: drop a section -> red."""
    text = _text()
    for n in range(4):
        assert re.search(rf"(?m)^## {n}\. ", text), n


def test_section_one_carries_r1_to_r12_in_order():
    """Mutation: drop or renumber a rule -> red."""
    s1 = _section(_text(), 1)
    found = re.findall(r"(?m)^\*\*R(\d+)\.", s1)
    assert found == [str(i) for i in range(1, 13)]


def test_section_one_and_section_zero_name_no_agent():
    """Mutation: leave a colleague's name in a rule -> red."""
    text = _text()
    for n in (0, 1):
        sec = _section(text, n)
        for name in AGENT_NAMES:
            assert not re.search(rf"\b{name}\b", sec), (n, name)


def test_section_zero_is_the_case():
    s0 = _section(_text(), 0)
    assert "#3740" in s0 and "seven times in eight" in s0


def test_the_paired_vs_welch_table_sits_under_r7_with_its_attribution():
    """Mutation: move the table out of R7, drop a ratio, or drop the attribution -> red."""
    s1 = _section(_text(), 1)
    r7 = s1[s1.index("**R7."):s1.index("**R8.")]
    for ratio in ("0.8642", "0.1091", "0.0766"):
        assert ratio in r7, ratio
    assert "the EngramManager design owner's measurement on real A6 cells, 2026-10-31" in r7
    assert "Welch" in r7 and "paired" in r7


def test_section_two_points_at_the_library_tag_the_install_line_and_the_gate_file_convention():
    s2 = _section(_text(), 2)
    assert "engram-agents/metricgate" in s2 and "v0.1.0" in s2
    assert INSTALL_LINE in s2
    assert "tools/gates/a6-" in s2 and "gate_sha256" in s2 and "preregister" in s2
    assert "dev tier" in s2 or "T3" in s2


def test_section_three_is_the_status():
    s3 = _section(_text(), 3)
    assert "Status" in s3 and "DESIGNED" in s3 and "BUILT" in s3 and "ADOPTED" in s3
    assert "v0.1.0" in s3


def test_the_template_component_is_exactly_the_one_line():
    """One line, no attribution, no date. Mutation: add a date, a name, or a second paragraph -> red."""
    text = COMPONENT.read_text(encoding="utf-8")
    body = text.split("---\n", 2)[2]
    assert body.strip() == LINE
    assert "\n" not in body.strip()
    assert not re.search(r"\b20\d\d\b", body)
    assert "docs/" not in body and ".md" not in body
    for name in AGENT_NAMES:
        assert name not in body


def test_the_component_is_listed_once_in_the_claude_code_manifest():
    ids = [c["id"] for c in json.loads(MANIFEST.read_text(encoding="utf-8"))["components"]]
    assert ids.count("metric-gate-rule") == 1


def test_the_rendered_template_carries_the_line_once():
    """The byte-identity of render == template is test_template_components.py's; this pins the content."""
    assert TEMPLATE.read_text(encoding="utf-8").count(LINE) == 1


def test_the_gate_file_convention_says_one_fresh_directory_per_announced_cell():
    """Round 2: a cell is one directory, one planned n. Mutation: drop the sentence -> red."""
    assert FRESH_DIR in " ".join(_section(_text(), 2).split())


def test_section_three_notes_the_instrument_version_re_registration():
    s3 = " ".join(_section(_text(), 3).split())
    assert "instrument_version" in s3 and "#4297" in s3 and "VERIFIED" in s3


def test_r7_says_what_follows_a_failed_aa_control():
    """Mutation: move the paragraph out of R7, duplicate it, drop or qualify the no-third-run sentence, or drop
    a supporting clause -> red."""
    text = _text()
    assert text.count("When the A/A control fails") == 1
    s1 = _section(text, 1)
    r7 = " ".join(s1[s1.index("**R7."):s1.index("**R8.")].split())
    for needle in (
        "voids that cell-metric in either direction",
        "a printed PASS is not a PASS and a printed FAIL is not a FAIL",
        "Cell-metrics whose own controls passed stand",
        "One repair measurement is allowed, and it is not a second look under R6",
        "announced before it runs",
        "the same registered gate",
        "on the same head",
        "on the same head (the head of the void attempt, even if the branch has moved since, with the verdict then carrying under R1's rider)",
        "a fresh directory",
        "the A/A runs first",
        "host load is recorded at each run",
        "both attempts go into the R12 table, with the void attempt's files kept and named",
        "Only the voided metric takes a verdict from the repair.",
        "A verdict already read on another metric of that cell stays final",
        'labelled "not a verdict"',
        "the design owner decides what follows",
        "**A second failed control ends it: there is no third run.**",
        "redesigned by the design owner under R5 as a new registered gate",
        "#4390",
    ):
        assert needle in r7, needle
    assert r7.index("When the A/A control fails") < r7.index("Why `paired` is a required field")


def test_r1_says_when_a_verdict_carries_to_a_later_head():
    """Mutation: move the paragraph under another rule, weaken "only if" to "if", duplicate it -> red."""
    text = _text()
    assert text.count("A verdict belongs to the head it was measured on") == 1
    s1 = _section(text, 1)
    r1 = " ".join(s1[s1.index("**R1."):s1.index("**R2.")].split())
    for needle in (
        "A verdict belongs to the head it was measured on",
        "It carries to a later head only if the diff between the two heads (tree against tree, so what a rebase brings in counts) touches no line of the measured path",
        "the change states it with its list of hunks, and the reviewer checks it like any other claim",
        "counts as touching the measured path",
        "the later head is a new build",
        "announced again and measured whole",
        "which R6 forbids",
        "#4398",
    ):
        assert needle in r1, needle


def test_r6_and_section_two_point_at_the_repair_rule():
    """Mutation: drop either pointer -> red."""
    text = _text()
    s1 = " ".join(_section(text, 1).split())
    r6 = s1[s1.index("**R6."):s1.index("**R7.")]
    assert "(the paragraph under R7) is not a second look: the voided metric had no verdict to read twice." in r6
    assert 'It is the one stated limit on "every recorded run counts"' in r6
    assert "the void attempt's runs stay in the R12 table and give that metric no verdict." in r6
    s2 = " ".join(_section(text, 2).split())
    assert ("The one repair measurement after a failed control (section 1, under R7) is announced as such and "
            "gets its own fresh directory.") in s2


def test_the_intro_names_the_two_riders():
    """Mutation: drop the mention of the riders from the intro -> red."""
    intro = " ".join(_text().split("## 0.")[0].split())
    assert "R1 to R12, with one rider under R1 and one under R7" in intro


def test_section_three_records_the_amendment():
    text = _text()
    s3 = _section(text, 3)
    assert "AMENDED" in s3 and "#4400" in s3
    last = [ln for ln in text.splitlines() if ln.strip()][-1]
    assert last.startswith("Dates here are alignment facts for humans")


# The phrase cells catch deletions and edits of pinned text, not additions: a clause appended to a rider (for example "then the cell if the first attempt's cell read FAIL", which is repeat-until-pass) passes all of them. These pins make any edit to a rider fail here, so a wording change always shows in review as a changed hash. Update a hash only together with the rider it pins, and say why in the PR.
R7_RIDER_SHA256 = "4b93bf08ef229c9e1a88416049727fbd0ec999169af0d3895ab46e7e60d5fa21"
R1_RIDER_SHA256 = "2019e3524bce4f6aeab32ab70107352a0512f853062db8d0423f198c0fad1515"


def test_the_two_riders_are_pinned_whole():
    """Mutation: append a clause to either rider -> red, where every phrase cell stays green."""
    lines = _text().split("\n")
    for start, want in (
        ("**When the A/A control fails", R7_RIDER_SHA256),
        ("**A verdict belongs to the head it was measured on", R1_RIDER_SHA256),
    ):
        hits = [ln for ln in lines if ln.startswith(start)]
        assert len(hits) == 1, start
        assert hashlib.sha256(hits[0].encode("utf-8")).hexdigest() == want, start
