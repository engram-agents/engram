"""Tests for equivalence-group normalization in quote verification (#1287).

Exercises _normalize_for_equivalence (unit) and _verify_quote_in_source
(integration) from engram_observation.

Coverage:
  A. Equivalence cases — presentational differences that SHOULD now pass.
  B. Integrity cases — differences that MUST still fail (no false positives).
  C. Regression cases — things that passed before still pass.

Import-pattern contract (#948 wave — family-internals test):
    This file imports engram_observation directly and contains no ``import
    server``. Selected precisely when engram_observation.py changes.
"""

import os
import sys
import tempfile
import unicodedata
from pathlib import Path

import pytest

import engram_observation as eo


# ---------------------------------------------------------------------------
# A. Unit tests on _normalize_for_equivalence
# ---------------------------------------------------------------------------

class TestNormalizeForEquivalence:
    """Unit tests for the _normalize_for_equivalence helper."""

    def test_nfc_normalization(self):
        """NFC decomposed form should round-trip to NFC."""
        # 'a' + combining acute -> 'á' in NFC
        decomposed = "café"   # 'cafe' + combining acute on the e
        nfc_form = unicodedata.normalize("NFC", decomposed)
        assert eo._normalize_for_equivalence(decomposed) == eo._normalize_for_equivalence(nfc_form)

    def test_curly_double_quotes_folded(self):
        """“ and ” should fold to straight double-quote."""
        curly = "“hello world”"
        straight = '"hello world"'
        assert eo._normalize_for_equivalence(curly) == eo._normalize_for_equivalence(straight)

    def test_curly_single_quotes_folded(self):
        """‘ and ’ should fold to straight single-quote."""
        curly = "‘it’s"
        straight = "'it's"
        assert eo._normalize_for_equivalence(curly) == eo._normalize_for_equivalence(straight)

    def test_em_dash_folded_to_hyphen(self):
        """Em-dash — should fold to hyphen-minus."""
        with_em = "word—word"
        with_hyphen = "word-word"
        assert eo._normalize_for_equivalence(with_em) == eo._normalize_for_equivalence(with_hyphen)

    def test_en_dash_folded_to_hyphen(self):
        """En-dash – should fold to hyphen-minus."""
        with_en = "word–word"
        with_hyphen = "word-word"
        assert eo._normalize_for_equivalence(with_en) == eo._normalize_for_equivalence(with_hyphen)

    def test_no_break_space_folded(self):
        """NO-BREAK SPACE (U+00A0) should fold to regular space."""
        with_nbsp = "hello world"
        with_space = "hello world"
        assert eo._normalize_for_equivalence(with_nbsp) == eo._normalize_for_equivalence(with_space)

    def test_narrow_no_break_space_folded(self):
        """NARROW NO-BREAK SPACE (U+202F) should fold to regular space."""
        with_nnbs = "hello world"
        with_space = "hello world"
        assert eo._normalize_for_equivalence(with_nnbs) == eo._normalize_for_equivalence(with_space)

    def test_thin_space_folded(self):
        """THIN SPACE (U+2009) should fold to regular space."""
        with_thin = "hello world"
        with_space = "hello world"
        assert eo._normalize_for_equivalence(with_thin) == eo._normalize_for_equivalence(with_space)

    def test_zero_width_space_removed(self):
        """ZERO WIDTH SPACE (U+200B) should be removed entirely."""
        with_zwsp = "hel​lo"
        without = "hello"
        assert eo._normalize_for_equivalence(with_zwsp) == eo._normalize_for_equivalence(without)

    def test_whitespace_run_collapsed(self):
        """Multiple spaces should collapse to one."""
        double_space = "a  b"
        single_space = "a b"
        assert eo._normalize_for_equivalence(double_space) == eo._normalize_for_equivalence(single_space)

    def test_newline_treated_as_space(self):
        """Newline in source should be treated as whitespace (collapsed to space)."""
        with_newline = "A\nB"
        with_space = "A B"
        assert eo._normalize_for_equivalence(with_newline) == eo._normalize_for_equivalence(with_space)

    def test_tab_treated_as_space(self):
        """Tab should collapse to single space."""
        with_tab = "A\tB"
        with_space = "A B"
        assert eo._normalize_for_equivalence(with_tab) == eo._normalize_for_equivalence(with_space)

    def test_strip_leading_trailing(self):
        """Leading/trailing whitespace should be stripped."""
        assert eo._normalize_for_equivalence("  hello  ") == "hello"

    def test_paraphrase_not_collapsed(self):
        """Different words are NOT made equal by normalization."""
        assert eo._normalize_for_equivalence("the cat sleeps") != eo._normalize_for_equivalence("the cat sat")

    def test_ellipsis_literal_not_skipped(self):
        """Three dots are matched literally — NOT treated as a skip/elision."""
        # The normalizer does not implement elision (per #1287 scope decision).
        with_dots = "A ... B"
        # After normalization, "A ... B" stays as "A ... B" (spaces collapsed)
        normalized = eo._normalize_for_equivalence(with_dots)
        assert "..." in normalized  # dots survive
        # And the long middle is NOT reachable just via normalization
        long_source = "A big long middle passage B"
        assert eo._normalize_for_equivalence(with_dots) not in eo._normalize_for_equivalence(long_source)


# ---------------------------------------------------------------------------
# B + C. Integration tests through _verify_quote_in_source
# ---------------------------------------------------------------------------

def _write_plain(tmp_path, content: str, filename="source.txt") -> str:
    """Write a plain text file and return its absolute path."""
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return str(p)


def _write_jsonl(tmp_path, content: str, filename="source.jsonl") -> str:
    """Write a .jsonl file containing the content as a JSON string value."""
    import json
    p = tmp_path / filename
    # Store as a simple JSON object so quote verification has realistic content.
    p.write_text(json.dumps({"text": content}) + "\n", encoding="utf-8")
    return str(p)


def _verify(file_path: str, quoted_text: str):
    """Call _verify_quote_in_source and return the result."""
    url = f"file://{file_path}"
    return eo._verify_quote_in_source(url, quoted_text)


class TestVerifyQuoteEquivalenceCases:
    """Integration — presentational differences that should now VERIFY (return None)."""

    def test_curly_vs_straight_double_quotes_source_has_straight(self, tmp_path):
        """Quote uses curly quotes, source has straight quotes — should verify."""
        file_path = _write_plain(tmp_path, 'He said "hello world" clearly.')
        result = _verify(file_path, "“hello world”")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_curly_vs_straight_double_quotes_source_has_curly(self, tmp_path):
        """Quote uses straight quotes, source has curly quotes — should verify."""
        file_path = _write_plain(tmp_path, "He said “hello world” clearly.")
        result = _verify(file_path, '"hello world"')
        assert result is None, f"Expected None (verified), got: {result}"

    def test_curly_single_quote_apostrophe(self, tmp_path):
        """Curly apostrophe in quote matches straight apostrophe in source."""
        file_path = _write_plain(tmp_path, "It's a fine day.")
        result = _verify(file_path, "It’s a fine day.")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_double_space_in_source(self, tmp_path):
        """Source has double space, quote has single space — should verify."""
        file_path = _write_plain(tmp_path, "The  quick brown fox")
        result = _verify(file_path, "The quick brown fox")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_double_space_in_quote(self, tmp_path):
        """Quote has double space, source has single space — should verify."""
        file_path = _write_plain(tmp_path, "The quick brown fox")
        result = _verify(file_path, "The  quick brown fox")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_newline_in_source_matches_space_in_quote(self, tmp_path):
        """Source has newline where quote has space — should verify."""
        file_path = _write_plain(tmp_path, "A\nB")
        result = _verify(file_path, "A B")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_non_breaking_space_in_source(self, tmp_path):
        """Non-breaking space in source, regular space in quote — should verify."""
        file_path = _write_plain(tmp_path, "hello world")
        result = _verify(file_path, "hello world")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_non_breaking_space_in_quote(self, tmp_path):
        """Non-breaking space in quote, regular space in source — should verify."""
        file_path = _write_plain(tmp_path, "hello world")
        result = _verify(file_path, "hello world")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_em_dash_in_quote_hyphen_in_source(self, tmp_path):
        """Em-dash in quote, hyphen in source — should verify."""
        file_path = _write_plain(tmp_path, "well-known result")
        result = _verify(file_path, "well—known result")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_em_dash_in_source_hyphen_in_quote(self, tmp_path):
        """Em-dash in source, hyphen in quote — should verify."""
        file_path = _write_plain(tmp_path, "well—known result")
        result = _verify(file_path, "well-known result")
        assert result is None, f"Expected None (verified), got: {result}"


class TestVerifyQuoteIntegrityCases:
    """Integration — differences that MUST still fail (no false positives)."""

    def test_paraphrase_still_rejected(self, tmp_path):
        """A genuine paraphrase with different words MUST be rejected.

        This is the critical no-false-positive test: differing words must
        never pass even after normalization.
        """
        file_path = _write_plain(tmp_path, "The cat sat on the mat.")
        result = _verify(file_path, "the cat sleeps")
        assert result is not None, "Expected rejection of paraphrase, got None (verified)"
        assert isinstance(result, str), f"Expected error string, got {type(result)}"

    def test_elision_not_implemented(self, tmp_path):
        """'A ... B' against 'A big long middle B' MUST fail.

        Elision is explicitly NOT implemented per #1287 scope decision.
        The literal dots are matched literally, not as a skip pattern.
        """
        file_path = _write_plain(tmp_path, "A big long middle passage B")
        result = _verify(file_path, "A ... B")
        assert result is not None, (
            "Expected rejection — elision is not implemented, 'A ... B' "
            "must not match 'A big long middle passage B'"
        )

    def test_completely_different_text_rejected(self, tmp_path):
        """Completely unrelated text is rejected."""
        file_path = _write_plain(tmp_path, "The quick brown fox jumps.")
        result = _verify(file_path, "completely unrelated content here")
        assert result is not None, "Expected rejection, got None (verified)"

    def test_missing_words_rejected(self, tmp_path):
        """Dropping words (not just whitespace) is rejected."""
        file_path = _write_plain(tmp_path, "The rate limit is 1000 per hour.")
        result = _verify(file_path, "The limit is 1000.")
        assert result is not None, "Expected rejection for missing words, got None"


class TestVerifyQuoteRegressionCases:
    """Regression — things that passed before must still pass."""

    def test_exact_verbatim_substring(self, tmp_path):
        """Exact verbatim substring should still verify via primary path."""
        file_path = _write_plain(tmp_path, "Some text. The rate limit is 1000/hr. More text.")
        result = _verify(file_path, "The rate limit is 1000/hr.")
        assert result is None, f"Expected None (verified), got: {result}"

    def test_ascii_escaped_jsonl_quote(self, tmp_path):
        """JSONL file with ASCII-escaped content is verified correctly."""
        import json
        content_with_em = "Rate is 1000—check docs."
        p = tmp_path / "source.jsonl"
        # Simulate a JSON writer that uses ensure_ascii=True (escapes the em-dash).
        p.write_text(json.dumps({"text": content_with_em}, ensure_ascii=True) + "\n", encoding="utf-8")
        # Quote uses the literal em-dash (human-readable form).
        result = _verify(str(p), "Rate is 1000—check docs.")
        assert result is None, f"Expected None (verified via ASCII escape or equivalence), got: {result}"

    def test_nfd_vs_nfc_still_passes(self, tmp_path):
        """NFD-encoded source matched by NFC-encoded quote — covered by normalization."""
        # Source written with NFD composed characters.
        nfd_text = unicodedata.normalize("NFD", "café is good")
        file_path = _write_plain(tmp_path, nfd_text)
        # Quote uses NFC (standard composition).
        nfc_quote = "café is good"
        result = _verify(file_path, nfc_quote)
        assert result is None, f"Expected None (NFC/NFD verified), got: {result}"

    def test_exact_match_https_url_skipped(self):
        """https:// URLs are not verified (remote URLs always return None)."""
        result = eo._verify_quote_in_source("https://example.com/article", "any quoted text")
        assert result is None, "https:// should not be verified"

    def test_nonexistent_file_returns_none(self, tmp_path):
        """Non-existent file:// path returns None (existence checked separately)."""
        url = f"file://{tmp_path}/does_not_exist.txt"
        result = eo._verify_quote_in_source(url, "any text")
        assert result is None, "Non-existent file should return None (checked elsewhere)"


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    # Also collect class-based tests
    import inspect
    passed = failed = 0
    tmp = tempfile.mkdtemp()
    for cls_name, cls in sorted(globals().items()):
        if inspect.isclass(cls) and cls_name.startswith("Test"):
            inst = cls()
            for method_name in sorted(dir(inst)):
                if method_name.startswith("test_"):
                    method = getattr(inst, method_name)
                    try:
                        # Simulate tmp_path with a real tmpdir
                        sig = inspect.signature(method)
                        if "tmp_path" in sig.parameters:
                            import pathlib
                            t = pathlib.Path(tempfile.mkdtemp())
                            method(t)
                        else:
                            method()
                        print(f"  PASS  {cls_name}.{method_name}")
                        passed += 1
                    except Exception as e:
                        print(f"  FAIL  {cls_name}.{method_name}: {e}")
                        failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    if failed:
        sys.exit(1)
