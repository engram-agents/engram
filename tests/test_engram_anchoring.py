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
    # on _NUMBER_RE for the full reasoning. Since #4206 a NUMBER token that
    # lies inside another emitted token is part of that token and is not
    # emitted separately: "2609" sits inside "#2609", and "60754" inside the
    # hex sha "60754dcc".
    assert hard_tokens("in #2609, sha 60754dcc") == ["#2609", "60754dcc"]
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


def test_verbatim_claim_with_embedded_digit_runs_is_fully_anchored():
    # #4206: the digit runs "1234" (inside the node id "ob_1234") and
    # "2609" (inside the issue ref "#2609") are part of those tokens, not
    # separate hard tokens, so a verbatim quote anchors every token. Two
    # tokens, both anchored -- not four, and not a partial ratio.
    result = anchor("cites ob_1234 and #2609", "source discusses ob_1234 in #2609 detail")
    assert result.hard_tokens == 2
    assert result.anchored == 2
    assert result.ratio == 1.0
    assert result.unanchored == []


def test_partially_anchored_claim_lists_unanchored():
    # #4206: only the outer tokens count ("ob_1234", "#2609"); the quote
    # carries the node id but not the issue ref.
    result = anchor("cites ob_1234 and #2609", "source discusses ob_1234 only")
    assert result.hard_tokens == 2
    assert result.anchored == 1
    assert result.ratio == 0.5
    assert result.unanchored == ["#2609"]


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
    # a plain substring check (no case-folding), unlike hex shas, so the
    # differently-cased "OB_1234" does not anchor it. Since #4206 the node
    # id is the only token here: its embedded digit run "1234" is part of
    # that token and is not emitted separately.
    result = anchor("cites ob_1234", "quote mentions OB_1234 only")
    assert result.hard_tokens == 1
    assert result.unanchored == ["ob_1234"]


def test_empty_quote_leaves_every_token_unanchored():
    result = anchor("cites ob_1234", "")
    assert result.hard_tokens == 1  # "ob_1234"; its digit run is part of it
    assert result.anchored == 0
    assert result.ratio == 0.0
    assert result.unanchored == ["ob_1234"]


# ── #4206: a hard token anchors on a boundary, not as a raw substring ──────


# Cell 1 — the issue's four rows.


def test_4206_pr_ref_inside_a_longer_number_is_unanchored():
    result = anchor("PR #404 merged", "PR #4040 merged")
    # #4206: "#404" is the only token -- the embedded "404" is part of it.
    assert result.unanchored == ["#404"]
    assert result.anchored == 0
    assert result.ratio == 0.0


def test_4206_number_inside_a_longer_number_is_unanchored():
    result = anchor("error 404", "error 4040")
    assert result.unanchored == ["404"]
    assert result.ratio == 0.0


def test_4206_exact_number_still_anchored_control():
    result = anchor("error 404", "error 404")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.unanchored == []
    assert result.ratio == 1.0


def test_4206_different_number_still_unanchored_control():
    result = anchor("error 404", "error 505")
    assert result.unanchored == ["404"]
    assert result.ratio == 0.0


# Cell 2 — boundary neighbours, both directions.


def test_4206_punctuation_is_a_boundary():
    result = anchor("error 404", "error:404")
    assert result.unanchored == []
    assert result.ratio == 1.0


def test_4206_letter_before_is_not_a_boundary():
    result = anchor("error 404", "x404")
    assert result.unanchored == ["404"]
    assert result.ratio == 0.0


def test_4206_letter_after_is_not_a_boundary():
    result = anchor("error 404", "404x")
    assert result.unanchored == ["404"]
    assert result.ratio == 0.0


# Cell 3 — the '#' shape: the fix must not pass by refusing every '#' token.


def test_4206_hash_token_inside_a_longer_number_is_unanchored():
    result = anchor("PR #404 merged", "PR #4040 merged")
    assert result.unanchored == ["#404"]
    assert result.ratio == 0.0


def test_4206_hash_token_anchored_when_exact():
    result = anchor("PR #404 merged", "PR #404 merged")
    assert result.hard_tokens == 1  # "#404"; its embedded "404" is part of it
    assert result.anchored == 1
    assert result.unanchored == []
    assert result.ratio == 1.0


# Cell 4 — a 7-char sha anchors as the prefix of a longer sha, but not when
# it sits inside a longer NON-hex alphanumeric run. The two quotes differ
# in exactly that way: "abcdef1c0ffee9" (all hex) vs "abcdef1zz" (not).


def test_4206_hex_prefix_of_a_longer_sha_is_anchored():
    result = anchor("merged as abcdef1", "the commit abcdef1c0ffee9 landed")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.unanchored == []
    assert result.ratio == 1.0


def test_4206_hex_token_inside_a_nonhex_alphanumeric_run_is_unanchored():
    result = anchor("merged as abcdef1", "the ref abcdef1zz landed")
    assert result.unanchored == ["abcdef1"]
    assert result.ratio == 0.0


# Cell 5 — the hex class stays case-insensitive.


def test_4206_hex_anchoring_stays_case_insensitive_on_boundaries():
    result = anchor("merged as ABCDEF1", "the commit abcdef1 landed")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.unanchored == []
    assert result.ratio == 1.0


# Cell 6 — the no-tokens branch never consults the boundary helper at all.


def test_4206_no_hard_tokens_never_consults_the_boundary_helper(monkeypatch):
    import engram_anchoring

    def _explode(*_args, **_kwargs):
        raise AssertionError("_token_is_anchored must not be called with zero tokens")

    monkeypatch.setattr(engram_anchoring, "_token_is_anchored", _explode)
    result = anchor("no hard facts here at all", "an unrelated quote")
    assert result.hard_tokens == 0
    assert result.anchored == 0
    assert result.ratio is None
    assert result.unanchored == []


# Round-1 review, finding 1 — an embedded digit run is part of its outer
# token, never a separate count (so a verbatim quote stays fully anchored).


def test_4206_embedded_number_token_in_a_node_id_is_part_of_it():
    assert hard_tokens("cites ob_1234 and #2609") == ["ob_1234", "#2609"]
    assert hard_tokens("filed on 2026-09-06") == ["2026-09-06"]


def test_4206_verbatim_node_id_quote_is_fully_anchored():
    result = anchor("cites ob_1234", "cites ob_1234")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.ratio == 1.0
    assert result.unanchored == []


def test_4206_embedded_digit_run_in_a_hex_sha_is_part_of_it():
    assert hard_tokens("commit abcdef1234") == ["abcdef1234"]
    result = anchor("commit abcdef1234", "commit abcdef1234")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.ratio == 1.0
    assert result.unanchored == []


def test_4206_decimal_number_keeps_its_integer_part_embedded():
    # The number regex emits the integer part of a decimal as its own match;
    # span containment drops it, so a verbatim quote is not refused by its
    # own decimal point (round-2 regression: this scored 0.5).
    assert hard_tokens("count 1234567.89") == ["1234567.89"]
    result = anchor("count 1234567.89", "count 1234567.89")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.ratio == 1.0
    assert result.unanchored == []


def test_4206_comma_decimal_number_keeps_its_integer_part_embedded():
    # The tokenizer's comma form is the comma-grouped number ("1234567,89"),
    # one token; its integer part is dropped as contained.
    assert hard_tokens("count 1234567,89") == ["1234567,89"]
    result = anchor("count 1234567,89", "count 1234567,89")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.ratio == 1.0
    assert result.unanchored == []


def test_4206_adjacent_separate_numbers_still_emit_both_control():
    # Containment is span containment, not substring containment: two
    # separate numbers are both tokens.
    assert hard_tokens("404 505") == ["404", "505"]


def test_4206_standalone_number_beside_a_longer_ref_is_not_dropped():
    # The deliberate difference from s2_clause_check._clause_hard_tokens'
    # substring rule: a standalone "404" is NOT inside "#4040"'s span, so
    # both survive. If this cell reddens, someone has "aligned" the span
    # rule to the substring one.
    assert hard_tokens("404 and #4040") == ["404", "#4040"]


# Round-1 review, finding 2 — an all-digit 7+-character run is a NUMBER,
# not a hex sha, so it takes the strict right-boundary rule. (The control
# for the reclassification is test_4206_hex_prefix_of_a_longer_sha_is_anchored:
# a sha with letters still anchors as the prefix of a longer sha.)


def test_4206_all_digit_seven_plus_token_is_a_number_not_a_sha():
    result = anchor("error 1234567", "error 12345670")
    assert result.hard_tokens == 1
    assert result.anchored == 0
    assert result.unanchored == ["1234567"]
    assert result.ratio == 0.0


def test_4206_all_digit_date_shaped_token_is_a_number_not_a_sha():
    result = anchor("date 20260906", "date 202609060")
    assert result.hard_tokens == 1
    assert result.anchored == 0
    assert result.unanchored == ["20260906"]
    assert result.ratio == 0.0


# Round-1 review, finding 3 — a '.' or ',' that touches a digit is part of
# the number, not a boundary (M1's rule: s2_clause_check._token_present).


def test_4206_decimal_separator_before_token_is_not_a_boundary():
    result = anchor("error 404", "error 4.404 seen")
    assert result.unanchored == ["404"]
    assert result.ratio == 0.0


def test_4206_decimal_separator_after_token_is_not_a_boundary():
    result = anchor("error 404", "error 404.5")
    assert result.unanchored == ["404"]
    assert result.ratio == 0.0


def test_4206_thousands_separator_before_token_is_not_a_boundary():
    result = anchor("error 404", "saw 5,404 rows")
    assert result.unanchored == ["404"]
    assert result.ratio == 0.0


def test_4206_sentence_final_period_is_a_boundary():
    result = anchor("error 404", "error 404.")
    assert result.hard_tokens == 1
    assert result.anchored == 1
    assert result.unanchored == []
    assert result.ratio == 1.0
