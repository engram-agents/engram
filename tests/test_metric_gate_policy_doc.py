"""Contract tests for docs/internal/METRIC-GATE-POLICY.md and the one CLAUDE.md template line (A5 of the
metricgate adoption PR). Written BEFORE the doc and the component existed.

The contract (spec A5):

  * the doc carries the policy's section 0 (the case) and section 1 (R1 to R12), a section 2 pointing at the
    library repo and tag with the install line and the gate-file convention, and a section 3 Status;
  * no agent names in the rules; the measured Welch-vs-paired table sits under R7 attributed to "the
    EngramManager design owner's measurement on real A6 cells, 2026-10-31";
  * the template gets ONE line, through the component mechanism, with no path, no attribution and no date;
"""
from __future__ import annotations

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
