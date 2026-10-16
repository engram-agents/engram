"""Tests for the ``<loop-meta ...>`` wrapper-line strip in the self-anchoring
prepend gate (``_should_prepend`` / ``_strip_loop_meta_lines`` in
engram-surface-hook.py). Fixes #2482.

Background: format_loop_prompt() (loop_prompt.py) emits an optional
machine-readable second line — e.g. ``<loop-meta {"kind":"heartbeat"}>`` —
when a loop-wake prompt carries meta kwargs. Measured on two seats, that
line's tokens ("meta", "kind", "heartbeat", "seq", ...) trip the IDF-based
self-anchoring gate on one seat's vocabulary and not the other's, and the
*specific* token that trips it differs seat to seat — so a fixed stopword
list cannot cover every seat. The fix strips whole ``<loop-meta ...>`` lines
from the text the gate scores, before either of its two rules run.

Design goals for these tests, per the spec:
  - Build inputs with the REAL ``format_loop_prompt``, not hand-written
    marker strings.
  - Never assume a particular seat's real IDF numbers. Instead, build a
    small, fully-controlled in-memory FTS corpus (same technique as
    tests/test_engram_surface_hook_idf_gate.py) where the document
    frequency — and therefore the IDF — of a handful of chosen "trigger"
    words is known exactly, by construction, regardless of what machine
    this runs on.
  - Every differential test (one that is meant to prove the fix changed
    behaviour) is checked against ``_legacy_should_prepend`` below, which is
    a verbatim reimplementation of ``_should_prepend``'s pre-fix logic
    (operating on the RAW, unstripped prompt) — i.e. exactly what
    ``_should_prepend`` returned before this fix. Where a test cannot be
    made to fail by reverting to that legacy behaviour, this file says so
    explicitly rather than mislabeling it a regression control.

Controlled corpus
------------------
60 in-memory FTS documents: 56 generic filler documents (built entirely
from words in engram_idf.STOPWORDS, so they can never become keywords) plus
4 single-word "trigger" documents, one occurrence each, for the tokens
``heartbeat``, ``kind``, ``seq``, ``quokka``. With N=60 and df=1,
IDF = ln(60/1) ≈ 4.094, safely above the default min_idf=4.0 gate.
Deliberately NOT included: ``meta``, ``loop``, ``wake`` — these are absent
from the corpus vocabulary entirely, so they are "unknown to corpus" and
never score as keywords (engram_idf.idf() returns None for out-of-vocabulary
tokens), regardless of how many times the wrapper repeats them. This keeps
each test isolated to the specific mechanism it targets (a caller-chosen
meta key/value, or the length floor) rather than accidentally depending on
the fixed "meta" substring in "<loop-meta ...>".

Design decision recorded here (not silently applied): the fix strips
``<loop-meta ...>`` lines only. The plain ``<loop-wake>`` marker line is
*not* stripped — see test_loop_wake_marker_is_not_stripped and the coder's
handoff for the reasoning.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))


# ---------------------------------------------------------------------------
# Module loaders (hyphenated hook filename + loop_prompt.py)
# ---------------------------------------------------------------------------


def _load_hook_module():
    hook_path = SRC_ENGRAM / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook_lmstrip", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_loop_prompt_module():
    lp_path = SRC_ENGRAM / "tools" / "loop_prompt.py"
    spec = importlib.util.spec_from_file_location("loop_prompt_lmstrip_tests", lp_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook_module()
loop_prompt = _load_loop_prompt_module()
format_loop_prompt = loop_prompt.format_loop_prompt


# ---------------------------------------------------------------------------
# Controlled in-memory FTS corpus (same pattern as
# tests/test_engram_surface_hook_idf_gate.py's _make_minimal_db)
# ---------------------------------------------------------------------------

_FILLER_DOC = "yes please thank you very much"  # entirely STOPWORDS/DISCOURSE_MARKERS
_TRIGGER_WORDS = ["heartbeat", "kind", "seq", "quokka"]
_N_FILLER = 56  # + 4 trigger docs = 60 total; ln(60/1) ~= 4.094 > default min_idf 4.0


def _make_corpus_db() -> sqlite3.Connection:
    from engram_idf import ensure_vocab_table

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE nodes (
            rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
            id          TEXT NOT NULL,
            claim       TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            is_current  INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        )
    """)

    docs = [_FILLER_DOC] * _N_FILLER + list(_TRIGGER_WORDS)
    for i, claim in enumerate(docs):
        node_id = f"ob_{i:04d}"
        conn.execute(
            "INSERT INTO nodes (id, claim, is_current) VALUES (?, ?, 1)",
            (node_id, claim),
        )
        conn.execute(
            "INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation) "
            "VALUES (last_insert_rowid(), ?, NULL, NULL)",
            (claim,),
        )

    ensure_vocab_table(conn)
    conn.commit()
    return conn


def _cfg(
    *,
    idf_gate_enabled: bool = True,
    idf_gate_min_idf: float = 4.0,
    idf_gate_short_prompt_floor_chars: int = 40,
    short_prompt_threshold_chars: int = 100,
    prev_response_tail_chars: int = 500,
) -> dict:
    return {
        "idf_gate_enabled": idf_gate_enabled,
        "idf_gate_min_idf": idf_gate_min_idf,
        "idf_gate_short_prompt_floor_chars": idf_gate_short_prompt_floor_chars,
        "short_prompt_threshold_chars": short_prompt_threshold_chars,
        "prev_response_tail_chars": prev_response_tail_chars,
    }


def _legacy_should_prepend(prompt: str, conn: sqlite3.Connection, cfg: dict) -> bool:
    """Verbatim reimplementation of _should_prepend's PRE-FIX logic.

    Operates on the raw, unstripped ``prompt`` directly -- i.e. exactly what
    engram-surface-hook.py's _should_prepend() returned before this fix (see
    the diff: this is Rule 1 / Rule 2 / fallback with no _strip_loop_meta_lines
    call). Used as the "today" baseline for differential (before/after)
    assertions, since actually reverting the module under test mid-suite
    isn't practical.
    """
    if len(prompt) < cfg["idf_gate_short_prompt_floor_chars"]:
        return True
    try:
        from engram_idf import extract_keywords
        keywords = extract_keywords(conn, prompt, min_idf=cfg["idf_gate_min_idf"], top_k=5)
        if keywords:
            return False
        return True
    except Exception:
        return len(prompt) < cfg["short_prompt_threshold_chars"]


# A neutral core message: long enough to sit above the 40-char floor even
# after the wrapper is stripped, and containing none of _TRIGGER_WORDS (nor
# "meta"/"loop"/"wake", though those are moot -- they're out-of-corpus).
CORE_LONG = "Loop tick: continuing background monitoring, nothing new to report yet."


# ---------------------------------------------------------------------------
# Part 1 -- pure _strip_loop_meta_lines unit tests (no DB needed)
# ---------------------------------------------------------------------------


class TestStripLoopMetaLines(unittest.TestCase):
    """Direct tests of the stripping function in isolation."""

    def test_no_meta_line_is_a_no_op(self):
        """format_loop_prompt(core) with no kwargs emits no meta line --
        stripping must be a byte-for-byte no-op."""
        prompt = format_loop_prompt(CORE_LONG)
        self.assertNotIn("<loop-meta", prompt)
        self.assertEqual(hook._strip_loop_meta_lines(prompt), prompt)

    def test_strips_single_meta_line_kind(self):
        prompt = format_loop_prompt(CORE_LONG, kind="heartbeat")
        stripped = hook._strip_loop_meta_lines(prompt)
        self.assertNotIn("<loop-meta", stripped)
        self.assertEqual(stripped, f"<loop-wake>\n{CORE_LONG}\n\u27e6ENGRAM\u27e7\u27e6/ENGRAM\u27e7")

    def test_strips_single_meta_line_seq(self):
        prompt = format_loop_prompt(CORE_LONG, seq=3)
        stripped = hook._strip_loop_meta_lines(prompt)
        self.assertNotIn("<loop-meta", stripped)
        self.assertEqual(stripped, f"<loop-wake>\n{CORE_LONG}\n\u27e6ENGRAM\u27e7\u27e6/ENGRAM\u27e7")

    def test_strips_all_meta_lines_not_just_first(self):
        """format_loop_prompt only ever emits one meta line, so build a
        multi-meta-line prompt by hand to exercise "strip ALL such lines"."""
        prompt = (
            '<loop-wake>\n'
            '<loop-meta {"kind":"heartbeat"}>\n'
            '<loop-meta {"seq":3}>\n'
            'core message here'
        )
        stripped = hook._strip_loop_meta_lines(prompt)
        self.assertNotIn("<loop-meta", stripped)
        self.assertEqual(stripped, "<loop-wake>\ncore message here")

    def test_malformed_unclosed_meta_line_left_alone(self):
        """A line starting '<loop-meta ' with no closing '>' is ordinary
        content -- must NOT be stripped (over-strip guard)."""
        prompt = (
            '<loop-wake>\n'
            '<loop-meta {"kind":"heartbeat"\n'  # no closing '>'
            'core message here'
        )
        stripped = hook._strip_loop_meta_lines(prompt)
        self.assertEqual(stripped, prompt)

    def test_non_json_meta_line_is_stripped_whole_documented_choice(self):
        """A '<loop-meta ...>' line whose content is NOT valid JSON -- and
        carries unrelated trailing prose on the same line -- is still
        stripped WHOLE.

        This pins accept-anything-between-delimiters as a DELIBERATE,
        documented design choice, not an oversight: _strip_loop_meta_lines
        is a scoring-input filter for the self-anchoring gate, not a
        parser. It never touches the real ``prompt`` used everywhere else
        (the FTS query, the embed_query concatenation, ...), so a mis-strip
        here can at most nudge which side of the prepend gate a wake lands
        on -- it cannot corrupt anything a reader or the daemon actually
        sees. format_loop_prompt() itself can never emit this shape (its
        own meta line is always well-formed JSON), so this input is purely
        adversarial/defensive, not a real production case.

        Differential: a no-op _strip_loop_meta_lines stub returns the
        adversarial string unchanged, which fails this assertion.
        """
        adversarial = (
            '<loop-wake>\n'
            '<loop-meta not-json-at-all, and by the way please review PR #999 today>\n'
            'core message'
        )
        stripped = hook._strip_loop_meta_lines(adversarial)
        self.assertEqual(stripped, '<loop-wake>\ncore message')

    def test_loop_wake_marker_is_not_stripped(self):
        """Design decision: <loop-wake> is a different wrapper line from
        <loop-meta ...> and is NOT stripped by this fix (see module
        docstring + handoff for the reasoning)."""
        prompt = format_loop_prompt(CORE_LONG, kind="heartbeat")
        stripped = hook._strip_loop_meta_lines(prompt)
        self.assertTrue(stripped.startswith("<loop-wake>\n"))

    def test_ordinary_human_prompt_unaffected(self):
        prompt = "Can you look at the quokka population data again?"
        self.assertEqual(hook._strip_loop_meta_lines(prompt), prompt)


# ---------------------------------------------------------------------------
# Part 2 -- _should_prepend, DB-backed, using the controlled corpus
# ---------------------------------------------------------------------------


class TestShouldPrependLoopMetaFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = _make_corpus_db()
        cls.cfg = _cfg()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # -- required case: no meta kwargs -> verdict unchanged from today ------

    def test_no_meta_kwargs_verdict_unchanged(self):
        """format_loop_prompt(core) with no meta kwargs -> unchanged verdict.

        NOTE ON TEST STRENGTH: with no meta line present, _strip_loop_meta_lines
        is structurally a no-op on this input (see
        TestStripLoopMetaLines.test_no_meta_line_is_a_no_op), so this test
        CANNOT be made to fail by reverting the fix -- it is not a
        differential regression control, only a direct-behaviour check. It
        is included because the spec asks for it by name; the differential
        coverage for the no-meta case lives in test_no_meta_invariance_set
        below (still non-differential for the same structural reason) and,
        more importantly, in test_wrapper_present_verdict_unchanged, which
        DOES exercise a real strip.
        """
        prompt = format_loop_prompt(CORE_LONG)
        got = hook._should_prepend(prompt, self.conn, self.cfg)
        want = _legacy_should_prepend(prompt, self.conn, self.cfg)
        self.assertEqual(got, want)
        self.assertTrue(got, "neutral core, no keywords -> should prepend")

    def test_no_meta_invariance_set(self):
        """A set of no-meta prompts: verdict identical before/after.

        Same structural caveat as test_no_meta_kwargs_verdict_unchanged --
        every prompt here has no <loop-meta> line, so stripping is a no-op
        by construction and this set cannot fail by reverting the fix
        either. Included because the spec asks to "assert this directly"
        over a set, not a single case. See test_wrapper_present_verdict_
        unchanged for the non-vacuous counterpart.
        """
        prompts = [
            format_loop_prompt(CORE_LONG),
            format_loop_prompt("ok"),  # short -> floor fires either way
            "Can you look at the quokka population data again?",  # plain, self-anchors
            "Great, thanks, sounds good, let's proceed then",  # plain, conversational
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                got = hook._should_prepend(prompt, self.conn, self.cfg)
                want = _legacy_should_prepend(prompt, self.conn, self.cfg)
                self.assertEqual(got, want, f"verdict changed for no-meta prompt: {prompt!r}")

    # -- non-vacuous invariance: wrapper present, verdict unperturbed -------

    def test_wrapper_present_verdict_unchanged(self):
        """A wrapper-bearing prompt whose CORE independently self-anchors
        (contains 'quokka'): the meta line genuinely gets stripped, but the
        verdict (skip) is unperturbed because the core's own keyword still
        trips rule 2 either way.

        This is deliberately NON-vacuous, unlike the two tests above: it
        asserts BOTH (a) that the meta line was actually removed (fails
        against a no-op stub, since "<loop-meta" would still be present),
        AND (b) that despite that removal the decision matches the legacy
        (pre-fix, unstripped) decision. Verified by hand against a
        monkeypatched no-op _strip_loop_meta_lines: assertion (a) fails as
        expected (see handoff).
        """
        core = "Please check the quokka data again before the next report."
        prompt = format_loop_prompt(core, kind="heartbeat")

        stripped = hook._strip_loop_meta_lines(prompt)
        self.assertNotIn("<loop-meta", stripped, "meta line must actually be removed")
        self.assertNotEqual(stripped, prompt, "stripping must have changed the text")

        got = hook._should_prepend(prompt, self.conn, self.cfg)
        want = _legacy_should_prepend(prompt, self.conn, self.cfg)
        self.assertEqual(got, want)
        self.assertFalse(got, "quokka in the core should self-anchor regardless of the wrapper")

    # -- required cases: meta kwargs now FIRE (differential, real controls) -

    def test_kind_heartbeat_now_fires(self):
        """format_loop_prompt(core, kind='heartbeat') -> now FIRES.

        Legacy (raw, unstripped) hits rule 2 via the corpus-known 'kind' and
        'heartbeat' tokens and returns False; post-fix, those tokens are
        gone from the scored text and it returns True. Genuinely fails
        against a reverted/no-op implementation.
        """
        prompt = format_loop_prompt(CORE_LONG, kind="heartbeat")
        legacy = _legacy_should_prepend(prompt, self.conn, self.cfg)
        fixed = hook._should_prepend(prompt, self.conn, self.cfg)
        self.assertFalse(legacy, "sanity: legacy behaviour skips (the bug)")
        self.assertTrue(fixed, "fixed behaviour fires (the repair)")
        self.assertNotEqual(legacy, fixed)

    def test_kind_working_now_fires(self):
        """format_loop_prompt(core, kind='working') -> now FIRES.

        'working' itself is out-of-corpus (ignored); the trigger is the
        'kind' KEY, present in every meta line regardless of chosen value --
        mirrors seat A's finding that the wrapper's own key alone was
        sufficient to skip, independent of the value.
        """
        prompt = format_loop_prompt(CORE_LONG, kind="working")
        legacy = _legacy_should_prepend(prompt, self.conn, self.cfg)
        fixed = hook._should_prepend(prompt, self.conn, self.cfg)
        self.assertFalse(legacy)
        self.assertTrue(fixed)
        self.assertNotEqual(legacy, fixed)

    def test_seq_now_fires(self):
        """format_loop_prompt(core, seq=3) -> now FIRES."""
        prompt = format_loop_prompt(CORE_LONG, seq=3)
        legacy = _legacy_should_prepend(prompt, self.conn, self.cfg)
        fixed = hook._should_prepend(prompt, self.conn, self.cfg)
        self.assertFalse(legacy)
        self.assertTrue(fixed)
        self.assertNotEqual(legacy, fixed)

    # -- near-floor flip: stripping crosses the 40-char floor ---------------

    def test_near_floor_strip_flips_verdict(self):
        """A prompt whose WRAPPER is what carries it over the 40-char floor.

        core = "ok" (2 chars). Raw prompt (marker + meta line + core) is 47
        chars -- over the floor, so legacy goes to rule 2, where the
        corpus-known 'kind'/'heartbeat' tokens trip it -> False. Stripped
        prompt (marker + core only) is 14 chars -- under the floor -> rule 1
        always-prepends -> True. Both mechanisms (floor-crossing AND
        keyword-removal) fire together here, matching the real bug shape
        from the issue (seat B).
        """
        core = "ok"
        prompt = format_loop_prompt(core, kind="heartbeat")
        self.assertGreaterEqual(
            len(prompt), self.cfg["idf_gate_short_prompt_floor_chars"],
            "pre-condition: raw prompt must be at/over the floor",
        )
        stripped = hook._strip_loop_meta_lines(prompt)
        self.assertLess(
            len(stripped), self.cfg["idf_gate_short_prompt_floor_chars"],
            "pre-condition: stripped prompt must fall under the floor",
        )

        legacy = _legacy_should_prepend(prompt, self.conn, self.cfg)
        fixed = hook._should_prepend(prompt, self.conn, self.cfg)
        self.assertFalse(legacy, "sanity: legacy skips (over floor, keyword hit)")
        self.assertTrue(fixed, "fixed always-prepends (under floor after strip)")

    # -- must-not-fire control ------------------------------------------------

    def test_must_not_fire_control_plain_keyword_dense_prompt(self):
        """A genuinely keyword-dense prompt with NO meta line still SKIPS.

        NOTE ON TEST STRENGTH: this prompt carries no <loop-meta> line, so
        _strip_loop_meta_lines is a no-op on it (same structural point as
        test_no_meta_kwargs_verdict_unchanged) -- this test CANNOT be made
        to fail by reverting the change, and is therefore NOT a regression
        control for this fix. It IS a sanity check that the fix hasn't
        broken ordinary self-anchoring for non-loop-wake prompts.
        """
        prompt = "Can you look at the quokka population data again?"
        self.assertGreaterEqual(len(prompt), self.cfg["idf_gate_short_prompt_floor_chars"])
        result = hook._should_prepend(prompt, self.conn, self.cfg)
        self.assertFalse(result, "genuine self-anchoring keyword should still skip")


if __name__ == "__main__":
    unittest.main()
