"""Tests for engram_ids — canonical node-id matching SSoT.

Test coverage:
  1. test_node_id_re_is_compiled_pattern
  2. test_finds_standard_4digit_id
  3. test_finds_multiple_ids_preserves_order
  4. test_deduplicates_in_first_occurrence_order
  5. test_handles_future_5digit_ids       — \\d{4,} not \\d{4}
  6. test_handles_future_6digit_ids
  7. test_requires_word_boundary_before
  8. test_requires_word_boundary_after
  9. test_rejects_short_digit_count        — `ob_123` is not valid
  10. test_rejects_uppercase_prefix
  11. test_rejects_three_letter_prefix
  12. test_empty_string
  13. test_no_matches_returns_empty
  14. test_handles_punctuation_separators
  15. test_handles_newlines
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram_ids import NODE_ID_RE, find_node_ids  # noqa: E402


def test_node_id_re_is_compiled_pattern():
    assert isinstance(NODE_ID_RE, re.Pattern)


def test_finds_standard_4digit_id():
    assert find_node_ids("see ob_1234") == ["ob_1234"]


def test_finds_multiple_ids_preserves_order():
    assert find_node_ids("ob_1234 and dv_0567 and ax_0001") == [
        "ob_1234", "dv_0567", "ax_0001"
    ]


def test_deduplicates_in_first_occurrence_order():
    assert find_node_ids("ob_1234 then dv_0001 then ob_1234") == [
        "ob_1234", "dv_0001"
    ]


def test_handles_future_5digit_ids():
    assert find_node_ids("ob_10000 is fine") == ["ob_10000"]


def test_handles_future_6digit_ids():
    assert find_node_ids("dv_123456 and ax_999999") == ["dv_123456", "ax_999999"]


def test_requires_word_boundary_before():
    assert find_node_ids("notob_1234") == []


def test_requires_word_boundary_after():
    assert find_node_ids("ob_1234x") == []


def test_rejects_short_digit_count():
    assert find_node_ids("ob_123") == []
    assert find_node_ids("ob_12") == []
    assert find_node_ids("ob_1") == []


def test_rejects_uppercase_prefix():
    assert find_node_ids("OB_1234") == []


def test_rejects_three_letter_prefix():
    assert find_node_ids("obx_1234") == []


def test_empty_string():
    assert find_node_ids("") == []


def test_no_matches_returns_empty():
    assert find_node_ids("nothing here") == []


def test_handles_punctuation_separators():
    assert find_node_ids("see (ob_1234), [dv_0567]; {ax_0001}!") == [
        "ob_1234", "dv_0567", "ax_0001"
    ]


def test_handles_newlines():
    text = "First: ob_1234\nSecond: dv_0567\nThird: ax_0001"
    assert find_node_ids(text) == ["ob_1234", "dv_0567", "ax_0001"]


def test_finds_id_inside_markdown_link():
    """Markdown link syntax `[text](url)` — `[` and `]` are non-word chars so \\b fires.

    Common pattern in ENGRAM prose where an ID gets wrapped in a markdown link to a
    viz-server detail page (e.g. `[ob_1234](http://localhost:5001/node/ob_1234)`).
    Should match both occurrences (deduplicating to one).
    """
    text = "see [ob_1234](http://localhost:5001/node/ob_1234) for details"
    assert find_node_ids(text) == ["ob_1234"]


def test_finds_ids_in_url_path_segments():
    """URL path segments separated by `/` — `/` is a non-word char so \\b fires on both sides.

    GitHub PR / issue links, viz-server URLs, etc. often embed IDs as path segments.
    """
    text = "https://example.com/dv_0567 and https://other.net/path/ax_0001/edit"
    assert find_node_ids(text) == ["dv_0567", "ax_0001"]


def test_empty_text_returns_empty_without_iteration():
    """Guard against treating empty string as having an iterable of matches."""
    assert find_node_ids("") == []
