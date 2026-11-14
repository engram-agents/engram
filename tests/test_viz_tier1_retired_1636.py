"""Memory tier 1 is retired (#1220); the viz and the handbook must not show it (#1636).

Contract:
C1. graph.js has no "Tier 1 (working" text; each of the two tier-label sites
    (the ``tierLabel`` / ``tierColor`` consts) has no ``tier === 1`` branch and
    still yields Tier 2 (searchable) / Tier 3 (archive) with the existing
    colours; ``tierCounts`` is ``{2: 0, 3: 0}``; the stats panel emits exactly
    two tier rows (Tier 2 (search), Tier 3 (archive)) and none for tier 1.
C2. Handbook section 5 (docs/handbook/01-substrate.md) has no ``tier1_max_nodes``
    and no "Tier 1 (working memory" text, says tier 1 is retired (#1220) and
    that two tiers remain; the ``_get_tier_threshold()`` bullet names
    tier2_max_nodes only; the config-keys line and key-constants line no longer
    list tier 1.
C3. ``computeTiers`` never assigns tier 1, so the removed UI cannot become
    reachable again.

Not touched (same word, other concepts): the retrieval-pipeline Tier 1, the
recall-summary Tier 1, trust tiers, edge tiers.  These are source-text cells.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GRAPH_JS = (ROOT / "src/engram/viz/static/graph.js").read_text(encoding="utf-8")
HANDBOOK = (ROOT / "docs/handbook/01-substrate.md").read_text(encoding="utf-8")


def _label_sites():
    """(tierLabel line, tierColor line) for each of the two label sites."""
    labels = re.findall(r"^[ \t]*const tierLabel = .*$", GRAPH_JS, re.M)
    colors = re.findall(r"^[ \t]*const tierColor = .*$", GRAPH_JS, re.M)
    assert len(labels) == 2 and len(colors) == 2, (labels, colors)
    return list(zip(labels, colors))


def _stats_block():
    i = GRAPH_JS.index("const tierCounts")
    j = GRAPH_JS.index("// Edge relation counts", i)
    return GRAPH_JS[i:j]


def _section5():
    m = re.search(r"## \*\*5\. Memory Tiers.*?(?=\n---\n)", HANDBOOK, re.S)
    assert m, "section 5 not found"
    return m.group(0)


def _compute_tiers():
    i = GRAPH_JS.index("function computeTiers")
    return GRAPH_JS[i:GRAPH_JS.index("\n}\n", i)]


# ---- C1 ----
def test_c1_no_tier1_working_text():
    assert "Tier 1 (working" not in GRAPH_JS


@pytest.mark.parametrize("site", [0, 1])
def test_c1_label_site_has_only_tiers_2_and_3(site):
    label, color = _label_sites()[site]
    assert "tier === 1" not in label and "tier === 1" not in color
    assert "Tier 2 (searchable)" in label and "Tier 3 (archive)" in label
    assert "#f0ad4e" in color and "#e05252" in color
    assert "#5cb85c" not in color


def test_c1_tiercounts_has_no_tier1_key():
    assert re.search(r"const tierCounts = \{2: 0, 3: 0\};", GRAPH_JS)


def test_c1_stats_panel_has_exactly_two_tier_rows():
    block = _stats_block()
    rows = re.findall(r'<div class="stat-row">.*?</div>', block)
    assert len(rows) == 2, rows
    assert "Tier 2 (search)" in rows[0] and "tierCounts[2]" in rows[0]
    assert "Tier 3 (archive)" in rows[1] and "tierCounts[3]" in rows[1]
    assert "tierCounts[1]" not in block


# ---- C2 ----
def test_c2_no_tier1_max_nodes_in_section5():
    assert "tier1_max_nodes" not in _section5()


def test_c2_no_tier1_working_memory_text():
    assert "Tier 1 (working memory" not in _section5()


def test_c2_says_retired_and_two_tiers():
    s = _section5()
    assert "#1220" in s and "retired" in s
    assert "searchable" in s and "archive" in s
    assert "Two-tier" in s or "two tiers" in s.lower()


def test_c2_threshold_bullet_names_tier2_only():
    bullet = next(l for l in _section5().splitlines() if "`_get_tier_threshold()`" in l)
    assert "tier2_max_nodes" in bullet
    assert "tier1" not in bullet.lower()


def test_c2_config_keys_line_has_no_tier1():
    line = next(l for l in _section5().splitlines() if l.startswith("Columns:"))
    assert "tier2_max_nodes" in line and "tier1" not in line.lower()


def test_c2_key_constants_line_has_no_tier1_max():
    line = next(l for l in _section5().splitlines() if l.startswith("**key constants**"))
    assert "Tier 2 max nodes 1000" in line
    assert "Tier 1 max" not in line


# ---- C3 ----
def test_c3_compute_tiers_never_assigns_tier1():
    body = _compute_tiers()
    assert "tierMap" in body
    assert not re.search(r"tierMap\s*(\[[^\]]*\]|\.\w+)?\s*=\s*1\b(?!\.)", body)
    assert not re.search(r"\]\s*=\s*1\s*[;\n]", body)
