"""Unit tests for engram_anchoring.py (engram-alpha#2602 facet 1/2, the
shared hard-token extractor + anchoring computation).

One positive + one negative case per hard-token class, plus the
documented edge cases: zero-tokens ratio is None (not 1.0), the
comma-grouping bound (5,121 vs 5121 counts as unanchored — the module's
own accepted imprecision), and case-insensitive hex-sha anchoring.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# #2565: append (not insert(0)) -- putting src/engram at position 0
# outranks the repo-root tools/ package for the rest of the process,
# a live instance of the tools/-package-name-clash class found while
# building #2565's durable guard (tests/conftest.py).
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))

from engram_anchoring import AnchoringResult, anchor, hard_tokens  # noqa: E402


# ── hard_tokens: one positive + one negative per class ─────────────────────


def test_node_id_positive():
    assert "ob_1234" in hard_tokens("cites ob_1234 for support")


def test_node_id_negative_wrong_shape_not_matched():
    # Three letters, or a 3-digit suffix, is not the xx_dddd node-id shape.
    assert "abc_1234" not in hard_tokens("mentions abc_1234 in passing")
    assert "ob_123" not in hard_tokens("mentions ob_123 in passing")


def test_issue_ref_positive():
    assert "#2609" in hard_tokens("filed as #2609")


def test_issue_ref_negative_bare_number_not_matched_as_ref():
    # Without the '#', it's just a number token, not an issue-ref token.
    toks = hard_tokens("filed as issue 2609")
    assert "#2609" not in toks
    assert "2609" in toks


def test_hex_sha_positive():
    assert "60754dcc" in hard_tokens("merged as 60754dcc today")


def test_hex_sha_negative_too_short_not_matched():
    # 6 hex chars is below the 7-40 range.
    assert "60754d" not in hard_tokens("merged as 60754d today")


def test_number_positive_three_plus_digits():
    assert "247" in hard_tokens("saw 247 events")


def test_number_negative_below_three_digits_excluded():
    toks = hard_tokens("2 apples, 12 oranges, 0.5 kg")
    assert "2" not in toks
    assert "12" not in toks
    assert "0.5" not in toks


def test_date_positive():
    assert "2026-09-06" in hard_tokens("filed on 2026-09-06")


def test_date_negative_wrong_shape_not_matched():
    assert "2026/09/06" not in hard_tokens("filed on 2026/09/06")


def test_time_positive():
    assert "12:19:00Z" in hard_tokens("stamped 12:19:00Z")


def test_time_negative_missing_colon_not_matched():
    assert "1219" not in [t for t in hard_tokens("stamped 1219 today") if ":" in t]


# ── documented bounds ───────────────────────────────────────────────────────


def test_glued_units_stripped_from_numbers():
    # #2602's own worked examples.
    assert hard_tokens("finished in 19.3s") == ["19.3"]
    assert hard_tokens("wrote 82089B") == ["82089"]


def test_trailing_punctuation_not_swallowed_into_number():
    # Deviation from the build spec's literal `\d[\d,]*\.?\d*` regex, which
    # would produce "2609," and "247." — see engram_anchoring.py's comment
    # on _NUMBER_RE for the full reasoning. "60754dcc" also independently
    # surfaces its embedded digit run "60754" as its own number token —
    # the documented class-overlap, not a bug in this specific assertion.
    assert hard_tokens("in #2609, sha 60754dcc") == ["#2609", "2609", "60754dcc", "60754"]
    assert hard_tokens("count 247. Next sentence") == ["247"]


def test_comma_grouped_number_matches_as_one_token():
    assert hard_tokens("total 5,121 nodes") == ["5,121"]


def test_ordered_and_deduplicated_by_first_appearance():
    toks = hard_tokens("ob_1234 appears twice: ob_1234 and then #99")
    assert toks[0] == "ob_1234"
    assert toks.count("ob_1234") == 1
    assert toks[-1] == "#99"


# ── anchor(): the AnchoringResult contract ──────────────────────────────────


def test_zero_hard_tokens_ratio_is_none_not_one():
    result = anchor("no hard facts here at all", "an unrelated quote")
    assert isinstance(result, AnchoringResult)
    assert result.hard_tokens == 0
    assert result.anchored == 0
    assert result.ratio is None
    assert result.unanchored == []
    assert result.to_dict()["ratio"] is None


def test_fully_anchored_claim():
    # "ob_1234" and "#2609" each also surface their embedded digit run
    # ("1234", "2609") as an independent number token — 4 hard tokens
    # total, all anchored since both digit runs are substrings of the
    # tokens that contain them.
    result = anchor("cites ob_1234 and #2609", "source discusses ob_1234 in #2609 detail")
    assert result.hard_tokens == 4
    assert result.anchored == 4
    assert result.ratio == 1.0
    assert result.unanchored == []


def test_partially_anchored_claim_lists_unanchored():
    result = anchor("cites ob_1234 and #2609", "source discusses ob_1234 only")
    assert result.hard_tokens == 4
    assert result.anchored == 2
    assert result.ratio == 0.5
    assert result.unanchored == ["#2609", "2609"]


def test_comma_thousands_mismatch_counts_as_unanchored():
    # The module's own accepted bound: a literal-substring check does not
    # normalize "5,121" against "5121" — a real fact stated in a different
    # surface form is scored as unanchored, not as a false success.
    result = anchor("saw 5,121 total", "the count was 5121 exactly")
    assert "5,121" in result.unanchored
    assert result.ratio == 0.0


def test_hex_sha_anchoring_is_case_insensitive():
    result = anchor("merged as ABCDEF1", "the commit abcdef1 landed today")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.unanchored == []


def test_node_id_anchoring_is_case_sensitive():
    # A node id is lowercase by construction; anchoring for that class is
    # a plain substring check (no case-folding), unlike hex shas. The
    # embedded digit run ("1234") is a separate number token and anchors
    # independently of the node-id string's own case — digits have no
    # case, so it correctly matches regardless; this test isolates the
    # node-id token itself, not the overlap.
    result = anchor("cites ob_1234", "quote mentions OB_1234 only")
    assert "ob_1234" in result.unanchored
    assert "1234" not in result.unanchored


def test_empty_quote_leaves_every_token_unanchored():
    result = anchor("cites ob_1234", "")
    assert result.hard_tokens == 2  # "ob_1234" plus its embedded "1234"
    assert result.anchored == 0
    assert result.ratio == 0.0
    assert result.unanchored == ["ob_1234", "1234"]
