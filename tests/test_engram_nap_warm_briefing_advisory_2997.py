"""Tests for engram_nap's warm-briefing advisory (#2997, anchor fixed in #3014).

engram_nap()'s return grows a `warm_briefing` block surfacing when the
engram-nap skill's Step 5b (warm-briefing "From this session" sync with
the current context-window arc) was NOT performed since the last
checkpoint -- that step is agent-executed prose, not code, so nothing
fails when it's skipped and the omission is otherwise invisible until a
LATER checkpoint reads a stale/empty warm briefing.

## The #3014 anchor fix

The ORIGINAL (#2997) design anchored its session-scoped boolean signal on
`session_started_at`, read from a per-session marker file keyed by
`session_id`. That anchor never changes across a long session -- so ONE
early write of warm-briefing.md satisfied it for the rest of the session,
even when Step 5b was skipped on every subsequent nap, and compaction
(which does not change `session_id`) never reset it either (#3014 issue
body: a section stale by four naps / ten hours, and a section that should
have been replaced post-compaction, both read as silently non-stale).

The FIX anchors the renamed signal, `written_since_last_nap`, on the
timestamp of the most recent nap-or-sleep checkpoint already present in
`session_log.md` -- both `_checkpoint_internal` modes write one
("## Nap (turn N) — <ts>" for a nap, "## Turn N — <ts>" for a
sleep/advance_turn), so this needs ZERO new state: `_last_checkpoint_timestamp`
just reads the file the caller is about to append to. The caller
(`engram_nap`) MUST snapshot this timestamp BEFORE `_nap_impl` runs --
snapshotting after would return the CURRENT checkpoint's own just-written
timestamp instead of the PREVIOUS one, making the signal structurally
always non-stale and defeating the whole fix. This file's core regression
test (the "two-nap falsifier" section, near the bottom) pins exactly this
ordering against the real `engram_nap()` MCP tool end-to-end.

`session_id` is no longer involved in this computation at all (it remains
an accepted-but-unused payload_json field for backward compatibility --
see engram_nap's own docstring). This file's unit tests below call
`server._compute_warm_briefing_advisory(wb_path, last_checkpoint_at)`
directly with a plain timestamp string (or None) as the second argument --
no session marker fixtures needed anymore, which is a real simplification
over the pre-#3014 version of this file.

Two signals, computed independently and never allowed to raise:
  - mtime                    -- warm_briefing_path's last-write time (or
                                 None if the file doesn't exist).
  - written_since_last_nap   -- True/False/None; None when there is no
                                 prior checkpoint yet in session_log.md, or
                                 its timestamp could not be parsed.
  - session_section_empty    -- True/False/None; anchored on the LAST
                                 `^## From this session` line (never a
                                 substring match -- same rule as
                                 #2986/#2989's drift-fix discipline), None
                                 when the file carries no such heading at
                                 all.

(Three bullets, two independent SIGNALS -- mtime is a raw field the other
two are derived from, not a third independent staleness signal.)

`note` is three-way, not binary (#2998 round 3, carried forward under the
#3014 anchor): silent (no `note` key) requires BOTH signals to be
confidently non-stale (written_since_last_nap is True AND
session_section_empty is False); a CONFIRMED-stale note ("Step 5b not
performed since the last checkpoint") fires when written_since_last_nap
is False OR session_section_empty is True; an UNKNOWN note (states which signal(s)
couldn't be determined, e.g. "could not determine whether Step 5b ran (no
prior checkpoint found in session_log.md)") fires for everything else --
i.e. when neither signal confirms staleness but at least one is None.
Confirmed-stale takes precedence over unknown when both are simultaneously
present. Any unknown (None) signal still emits SOME note -- this never
produces a false "all clear" -- but the note's wording no longer asserts a
fact the code declined to determine (issue #2997 leaves the exact
predicate to the implementer; see _compute_warm_briefing_advisory's own
docstring in server.py for the reasoning restated as a code comment).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_nap_warm_briefing_advisory_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Mirrors tests/test_nap_payload.py's fresh_server() -- test-isolated
    DATA_DIR via _configure_paths (never patch DATA_DIR by hand; see the
    test-isolation lesson referenced in engram_core.py's own docstring)."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _touch_at(path, when: datetime) -> None:
    """Sets path's mtime to a specific point in time -- deterministic
    control over the written_since_last_nap before/after comparison rather
    than relying on real wall-clock timing between statements. `when` MUST
    be tz-aware (a naive datetime's .timestamp() is silently interpreted
    as LOCAL time, not UTC -- the exact bug class #3014's own fix guards
    against in the production code; keep test fixtures honest about it
    too)."""
    assert when.tzinfo is not None, "_touch_at requires a tz-aware datetime"
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _ts(dt: datetime) -> str:
    """Renders a tz-aware datetime the same way _checkpoint_internal's own
    `core._now()[:19]` truncation does -- a naive-looking, always-UTC ISO
    string with no offset suffix (e.g. "2026-09-16T12:00:00"). Replaces
    the pre-#3014 _write_session_marker helper -- last_checkpoint_at is
    just a plain timestamp string now, no marker-file fixture needed."""
    assert dt.tzinfo is not None, "_ts requires a tz-aware datetime (state the UTC instant explicitly)"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# _last_checkpoint_timestamp -- new in #3014. Reads the MOST RECENT
# nap-or-sleep checkpoint header already in session_log.md, or None.
# ---------------------------------------------------------------------------

def test_last_checkpoint_timestamp_returns_the_last_of_multiple_headers(tmp_path):
    """Mixes Nap and Turn (sleep) headers -- a sleep is just as valid a
    checkpoint anchor as a nap (engram-sleep also legitimately
    rotates/erases the "From this session" section). The LAST header in
    the file (by position, matching how _checkpoint_internal always
    appends -- so position order IS chronological order) must win,
    regardless of which of the two header shapes it is."""
    server = fresh_server()
    log_path = tmp_path / "session_log.md"
    log_path.write_text(
        "# KG Memory Session Log\n\n---\n"
        "\n## Nap (turn 3) — 2026-09-16T12:00:00\n\n"
        "First nap of the day.\n\n---\n"
        "\n## Turn 4 — 2026-09-16T20:00:00\n\n"
        "End-of-day sleep.\n\n---\n"
        "\n## Nap (turn 4) — 2026-09-17T09:30:00\n\n"
        "Morning nap, the most recent checkpoint.\n\n---\n",
        encoding="utf-8",
    )
    result = server._last_checkpoint_timestamp(log_path)
    assert result == "2026-09-17T09:30:00", result


def test_last_checkpoint_timestamp_missing_file_is_none(tmp_path):
    server = fresh_server()
    result = server._last_checkpoint_timestamp(tmp_path / "does-not-exist.md")
    assert result is None, result


def test_last_checkpoint_timestamp_no_matching_headers_is_none(tmp_path):
    """File exists and has real content, but none of it is a checkpoint
    header -- must read None, not mistake ordinary prose for a header."""
    server = fresh_server()
    log_path = tmp_path / "session_log.md"
    log_path.write_text(
        "# KG Memory Session Log\n\n---\n\nJust some prose, no headers yet.\n",
        encoding="utf-8",
    )
    result = server._last_checkpoint_timestamp(log_path)
    assert result is None, result


def test_last_checkpoint_timestamp_empty_file_is_none(tmp_path):
    server = fresh_server()
    log_path = tmp_path / "session_log.md"
    log_path.write_text("", encoding="utf-8")
    result = server._last_checkpoint_timestamp(log_path)
    assert result is None, result


def test_last_checkpoint_timestamp_never_raises_on_directory(tmp_path):
    """A plausible misconfiguration (log_path pointing at a directory) must
    degrade to None, not raise -- same advisory-only contract as the rest
    of this module."""
    server = fresh_server()
    dir_path = tmp_path / "a-directory-not-a-file"
    dir_path.mkdir()
    result = server._last_checkpoint_timestamp(dir_path)
    assert result is None, result


# ---------------------------------------------------------------------------
# (a) file written AFTER the last checkpoint -> written_since_last_nap: true
# ---------------------------------------------------------------------------

def test_written_after_last_checkpoint_is_true(tmp_path):
    server = fresh_server()
    last_checkpoint = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nSomething written.\n", encoding="utf-8")
    _touch_at(wb_path, last_checkpoint + timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, _ts(last_checkpoint))
    assert result["written_since_last_nap"] is True, result


# ---------------------------------------------------------------------------
# (b) file NOT written since the last checkpoint -> written_since_last_nap: false
# ---------------------------------------------------------------------------

def test_written_before_last_checkpoint_is_false(tmp_path):
    server = fresh_server()
    last_checkpoint = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nStale content.\n", encoding="utf-8")
    _touch_at(wb_path, last_checkpoint - timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, _ts(last_checkpoint))
    assert result["written_since_last_nap"] is False, result


# ---------------------------------------------------------------------------
# (c) no prior checkpoint (last_checkpoint_at=None) -> written_since_last_nap: null
# ---------------------------------------------------------------------------

def test_no_prior_checkpoint_is_null(tmp_path):
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nContent.\n", encoding="utf-8")

    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["written_since_last_nap"] is None, result
    # session_section_empty is confidently False (real content) here, so
    # the note must read as UNKNOWN ("can't tell"), never confirmed-stale.
    assert result["session_section_empty"] is False, result
    assert result.get("note") == (
        "could not determine whether Step 5b ran (no prior checkpoint found in session_log.md)"
    ), result


# ---------------------------------------------------------------------------
# (d) malformed / non-string last_checkpoint_at -- never a crash, never a
#     guess. Merges the pre-#3014 file's separate "unparseable timestamp"
#     and "non-string session_id" sections: last_checkpoint_at is now an
#     internal-only value (engram_nap's caller-facing payload_json no
#     longer exposes it at all -- unlike the retired session_id field,
#     which was #2998 round 1's actual finding through that exact
#     boundary), but a function this defensive-by-design should not rely
#     on caller discipline regardless of exposure.
# ---------------------------------------------------------------------------

def test_malformed_or_non_string_last_checkpoint_at_degrades_to_none_not_crash(tmp_path):
    """THE regression-shaped test for this class of bug: a bad
    last_checkpoint_at must not take session_section_empty (a
    structurally independent signal) down with it, and the note key must
    never silently go missing.

    Uses a warm-briefing.md WITH a real "## From this session" heading AND
    real trailing content, so session_section_empty should independently
    resolve to False (confidently non-stale) -- NOT None -- regardless of
    what happens with last_checkpoint_at. written_since_last_nap must
    degrade to None, which per the documented note condition means note
    must still be present -- and, since neither signal is a CONFIRMED
    negative here (session_section_empty is False, not True;
    written_since_last_nap is None, not False), it must be the UNKNOWN
    wording, not the confirmed-stale one (#2998 round 3's own falsifying
    demonstration, carried forward: a None written_since_last_nap must
    never render byte-identically to a confirmed False).
    """
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nReal, non-empty content.\n",
        encoding="utf-8",
    )

    # Every value here is truthy and non-None, so all take the
    # "could not be parsed" branch of the note wording (not the
    # "no prior checkpoint" branch, which is reserved for None).
    bad_values = (
        123, [1, 2, 3], {"a": 1}, True, 4.5,
        "not-a-real-timestamp", "2026-13-45T99:99:99",
    )
    for bad_value in bad_values:
        result = server._compute_warm_briefing_advisory(wb_path, bad_value)
        assert result["session_section_empty"] is False, (
            f"session_section_empty must be independently computed despite "
            f"a bad last_checkpoint_at={bad_value!r}; got {result}"
        )
        assert result["written_since_last_nap"] is None, (
            f"written_since_last_nap must degrade to None (can't determine) "
            f"for a bad last_checkpoint_at={bad_value!r}; got {result}"
        )
        assert "note" in result, (
            f"REGRESSION: note key silently dropped for a bad "
            f"last_checkpoint_at={bad_value!r} -- this is exactly the false "
            f"'all clear' the function's docstring says never happens; "
            f"got {result}"
        )
        assert result["note"] == (
            "could not determine whether Step 5b ran "
            "(the last checkpoint's timestamp could not be parsed)"
        ), (
            f"a bad last_checkpoint_at={bad_value!r} must render as an "
            f"UNKNOWN note, never the confirmed-stale text -- got {result}"
        )


# ---------------------------------------------------------------------------
# (d.1) THE off-by-timezone correctness point (#3014 spec, load-bearing):
#     last_checkpoint_at is a naive-looking string that IS always UTC
#     (core._now()[:19] drops the offset suffix but never the UTC-ness).
#     datetime.fromisoformat() on it returns a NAIVE datetime; calling
#     .timestamp() on that naive value directly is silently interpreted as
#     LOCAL time, not UTC -- correct only by accident on a UTC-TZ machine.
#     This test forces a non-UTC local TZ and picks an mtime that a
#     correct (UTC-anchored) comparison reads as True, while a buggy
#     (local-time-misinterpreted) comparison would read False -- a real
#     discriminating case, not just a changed TZ with no behavioral
#     difference.
# ---------------------------------------------------------------------------

def test_written_since_last_nap_comparison_is_utc_correct_under_non_utc_tz(tmp_path):
    server = fresh_server()
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()

        # last_checkpoint_at represents 2026-09-16T12:00:00 UTC -- the
        # exact string shape _checkpoint_internal writes (naive, no
        # offset suffix).
        last_checkpoint_at = "2026-09-16T12:00:00"
        # wb mtime = 2026-09-16T14:00:00 UTC -- strictly AFTER the
        # correct UTC reading of last_checkpoint_at (12:00 UTC), but
        # strictly BEFORE the buggy local-time reading (America/New_York
        # is UTC-4 in September, so "12:00" read as EDT wall-clock is
        # 16:00 UTC).
        wb_mtime = datetime(2026, 9, 16, 14, 0, 0, tzinfo=timezone.utc)

        wb_path = tmp_path / "warm-briefing.md"
        wb_path.write_text("# Warm Briefing\n\n## From this session\n\nContent.\n", encoding="utf-8")
        _touch_at(wb_path, wb_mtime)

        result = server._compute_warm_briefing_advisory(wb_path, last_checkpoint_at)
        assert result["written_since_last_nap"] is True, (
            f"REGRESSION: under TZ=America/New_York, a naive "
            f"last_checkpoint_at must still be read as UTC, not local "
            f"time -- a buggy comparison would read False here; got {result}"
        )
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


# ---------------------------------------------------------------------------
# (e) heading present with trailing content -> session_section_empty: false
#     (this signal is untouched by #3014 -- last_checkpoint_at is
#     irrelevant to it, so None is passed throughout).
# ---------------------------------------------------------------------------

def test_section_with_content_is_not_empty(tmp_path):
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\nSome preamble.\n\n## From this session\n\nReal content here.\n",
        encoding="utf-8",
    )
    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["session_section_empty"] is False, result


# ---------------------------------------------------------------------------
# (f) heading present with nothing after it -> session_section_empty: true
# ---------------------------------------------------------------------------

def test_section_with_nothing_after_is_empty(tmp_path):
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\nSome preamble.\n\n## From this session\n",
        encoding="utf-8",
    )
    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["session_section_empty"] is True, result


def test_section_with_only_whitespace_after_is_empty(tmp_path):
    """Trailing blank lines / whitespace after the heading still count as
    empty -- only real (non-whitespace) content makes it non-empty."""
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\n\n   \n",
        encoding="utf-8",
    )
    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["session_section_empty"] is True, result


# ---------------------------------------------------------------------------
# (g) TWO "## From this session" headings (the known drift case, per
#     #2986/#2989's discipline) -- confirm the LAST one is used, not the
#     first, with a pairing where they give different answers.
# ---------------------------------------------------------------------------

def test_drift_two_headings_uses_last_not_first(tmp_path):
    """First (drifted-in) heading has real content directly below it; the
    LAST (real) heading has nothing after it. A first-match implementation
    would report False (content found); the correct last-match
    implementation must report True (empty)."""
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n"
        "## From this session\n\n"
        "Drifted-in earlier occurrence with real content below it.\n\n"
        "## From this session\n",
        encoding="utf-8",
    )
    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["session_section_empty"] is True, (
        f"must use the LAST heading (empty), not the first (has content); got {result}"
    )


def test_drift_two_headings_first_empty_last_has_content(tmp_path):
    """Inverse pairing -- the first heading has nothing after it (before
    the second heading starts) and the LAST heading has real content
    following it. A first-match implementation would report True (empty);
    the correct last-match implementation must report False."""
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n"
        "## From this session\n\n"
        "## From this session\n\n"
        "Real trailing content on the LAST heading.\n",
        encoding="utf-8",
    )
    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["session_section_empty"] is False, (
        f"must use the LAST heading (has content), not the first (empty); got {result}"
    )


# ---------------------------------------------------------------------------
# (h) mid-line/quoted occurrence -- must NOT be mistaken for the real
#     line-anchored heading (regression-style, mirrors #2680's own
#     test_regression_old_predicate_misreported...() pattern: reproduce
#     the naive-implementation failure as a real, discriminating case).
# ---------------------------------------------------------------------------

def test_regression_midline_occurrence_not_mistaken_for_heading(tmp_path):
    """A naive substring search (`"## From this session" in content`) would
    find the quoted occurrence embedded in this prose line. The anchored
    regex (^, MULTILINE) must skip it entirely and correctly report 'no
    real heading' (None) -- there is no line-anchored
    '## From this session' anywhere in this file."""
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n"
        'A note: the phrase "## From this session" appears here mid-line, '
        "quoted, and must not be treated as the real heading.\n",
        encoding="utf-8",
    )
    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["session_section_empty"] is None, (
        f"a mid-line/quoted occurrence must not be mistaken for the real "
        f"line-anchored heading; got {result}"
    )


def test_regression_midline_occurrence_does_not_shadow_real_heading(tmp_path):
    """A naive substring search using str.find()/str.index() (which returns
    the FIRST occurrence position regardless of line-anchoring) would find
    the mid-line quoted phrase BEFORE the real heading below it, and treat
    everything from that wrong offset onward as 'content'. The anchored
    regex must skip the mid-line occurrence and correctly anchor on the
    real (later) line-start heading, reporting it as empty."""
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n"
        'A note: the phrase "## From this session" appears here mid-line.\n\n'
        "## From this session\n",
        encoding="utf-8",
    )
    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["session_section_empty"] is True, (
        f"the mid-line occurrence must not shadow the real line-anchored "
        f"heading below it; got {result}"
    )


# ---------------------------------------------------------------------------
# (i) `note` present exactly when the block is stale/unknown -- never a
#     false "all clear". Silence (no note) reserved for the case where
#     BOTH signals are confidently non-stale.
# ---------------------------------------------------------------------------

def test_note_absent_when_both_signals_confidently_non_stale(tmp_path):
    server = fresh_server()
    last_checkpoint = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nFresh content.\n",
        encoding="utf-8",
    )
    _touch_at(wb_path, last_checkpoint + timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, _ts(last_checkpoint))
    assert result["written_since_last_nap"] is True, result
    assert result["session_section_empty"] is False, result
    assert "note" not in result, (
        f"note must be ABSENT when both signals are confidently non-stale; got {result}"
    )


def test_note_present_when_written_since_last_nap_false(tmp_path):
    server = fresh_server()
    last_checkpoint = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nStale content, but not empty.\n",
        encoding="utf-8",
    )
    _touch_at(wb_path, last_checkpoint - timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, _ts(last_checkpoint))
    assert result["written_since_last_nap"] is False, result
    assert result["session_section_empty"] is False, result
    assert result.get("note") == "Step 5b not performed since the last checkpoint", result


def test_note_present_when_session_section_empty_true(tmp_path):
    server = fresh_server()
    last_checkpoint = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n", encoding="utf-8")
    _touch_at(wb_path, last_checkpoint + timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, _ts(last_checkpoint))
    assert result["written_since_last_nap"] is True, result
    assert result["session_section_empty"] is True, result
    assert result.get("note") == "Step 5b not performed since the last checkpoint", result


def test_note_present_when_both_signals_unknown():
    """No prior checkpoint (written_since_last_nap -> None) AND no
    warm_briefing file at all (session_section_empty -> None): both
    unknown, note must still be present -- unknown is never treated as
    'all clear'. Neither signal is a CONFIRMED negative here (both are
    None, not False/True), so this is the UNKNOWN branch (#2998 round 3),
    not the confirmed-stale one -- the note must say what's unknown, not
    assert a definite claim the code never actually confirmed."""
    server = fresh_server()
    nonexistent_path = Path(tempfile.mkdtemp()) / "does-not-exist.md"
    result = server._compute_warm_briefing_advisory(nonexistent_path, None)
    assert result["written_since_last_nap"] is None, result
    assert result["session_section_empty"] is None, result
    assert result.get("note") == (
        "could not determine whether Step 5b ran "
        "(no prior checkpoint found in session_log.md; no '## From this session' heading found)"
    ), result


def test_note_omitted_last_checkpoint_on_genuinely_updated_file_is_unknown_not_stale():
    """#2998 round-3 colleague review's own falsifying demonstration,
    re-pinned under the #3014 anchor: a warm-briefing.md that DOES have
    real trailing content after "## From this session"
    (session_section_empty -> False, genuinely NOT stale) with no prior
    checkpoint yet (written_since_last_nap -> None, can't determine --
    e.g. this is the very first nap ever) must NOT read as confirmed-stale
    -- nothing here has been confirmed stale; the honest note is "can't
    tell", not "Step 5b not performed since the last checkpoint". Pre-fix
    (under the OLD session-start anchor), the analogous case rendered
    byte-identically to a confirmed False; the same invariant is pinned
    here for the new anchor."""
    server = fresh_server()
    wb_path = Path(tempfile.mkdtemp()) / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nGenuinely updated just now.\n",
        encoding="utf-8",
    )

    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["written_since_last_nap"] is None, result
    assert result["session_section_empty"] is False, result
    assert "note" in result, (
        f"unknown must still emit SOME note -- never a silent 'all clear'; got {result}"
    )
    assert result["note"] != "Step 5b not performed since the last checkpoint", (
        f"REGRESSION: an undetermined written_since_last_nap must not render "
        f"as the confirmed-stale claim; got {result}"
    )
    assert result["note"] == (
        "could not determine whether Step 5b ran (no prior checkpoint found in session_log.md)"
    ), result


# ---------------------------------------------------------------------------
# mtime field itself -- basic shape checks (untouched by #3014).
# ---------------------------------------------------------------------------

def test_mtime_present_and_iso8601_z_suffixed(tmp_path):
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n", encoding="utf-8")

    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["mtime"] is not None, result
    # Round-trips through fromisoformat after the Z->+00:00 substitution --
    # same convention engram_core.py itself uses to parse its own
    # 'Z'-suffixed timestamps (see its fromisoformat(...replace("Z", "+00:00"))
    # call sites).
    parsed = datetime.fromisoformat(result["mtime"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, result


def test_mtime_null_when_file_missing(tmp_path):
    server = fresh_server()
    missing_path = tmp_path / "does-not-exist.md"
    result = server._compute_warm_briefing_advisory(missing_path, None)
    assert result["mtime"] is None, result


# ---------------------------------------------------------------------------
# Never raises, even on a directory instead of a file (a plausible
# misconfiguration the advisory-only contract must survive).
# ---------------------------------------------------------------------------

def test_never_raises_on_directory_path(tmp_path):
    server = fresh_server()
    dir_path = tmp_path / "a-directory-not-a-file"
    dir_path.mkdir()
    # Must not raise -- degrade gracefully instead.
    result = server._compute_warm_briefing_advisory(dir_path, None)
    assert isinstance(result, dict), result
    # os.path.getmtime succeeds on a directory (mtime is set), but neither
    # written_since_last_nap (no prior checkpoint passed) nor
    # session_section_empty (open() on a directory raises, caught by the
    # broad except) can be confirmed -- both None, neither a CONFIRMED
    # negative, so this is the UNKNOWN branch (#2998 round 3), not
    # confirmed-stale.
    assert result["written_since_last_nap"] is None, result
    assert result["session_section_empty"] is None, result
    assert result.get("note") == (
        "could not determine whether Step 5b ran "
        "(no prior checkpoint found in session_log.md; no '## From this session' heading found)"
    ), result


# ---------------------------------------------------------------------------
# Integration: engram_nap()'s own return carries the warm_briefing block,
# and never raises whether or not session_id is present in the payload --
# session_id must be consumed by the wrapper before reaching _nap_impl,
# which has no such parameter (unchanged by #3014 -- session_id is still
# accepted, just no longer load-bearing for this advisory).
# ---------------------------------------------------------------------------

def test_engram_nap_return_carries_warm_briefing_block():
    server = fresh_server()
    r = json.loads(server.engram_nap(payload_json=json.dumps({
        "message": "Test nap for warm_briefing block presence.",
    })))
    assert "error" not in r, f"engram_nap returned error: {r}"
    assert "warm_briefing" in r, f"engram_nap missing warm_briefing block: {r}"
    wb = r["warm_briefing"]
    assert isinstance(wb, dict), wb
    assert "written_since_last_nap" in wb
    assert "session_section_empty" in wb
    # This is the FIRST-EVER nap in this fresh test dir -- no prior
    # checkpoint exists in session_log.md yet, so written_since_last_nap
    # must read None (can't determine), not a guessed True/False.
    assert wb["written_since_last_nap"] is None, wb


def test_engram_nap_accepts_session_id_field_without_reaching_nap_impl():
    """session_id must be popped from params by the wrapper before calling
    _nap_impl(**params) -- _nap_impl's signature is def _nap_impl(message:
    str), which has no session_id parameter, so passing session_id through
    unmodified would raise TypeError: _nap_impl() got an unexpected keyword
    argument 'session_id'. As of #3014, session_id no longer drives the
    warm_briefing advisory at all -- this test only pins that the field
    remains ACCEPTED (backward compatibility) and consumed correctly."""
    server = fresh_server()
    r = json.loads(server.engram_nap(payload_json=json.dumps({
        "message": "Test nap with session_id in payload.",
        "session_id": "sess-integration-test",
    })))
    assert "error" not in r, f"engram_nap returned error with session_id set: {r}"
    assert "warm_briefing" in r, r


# ---------------------------------------------------------------------------
# THE core regression test this issue (#3014) exists to add -- Borges's
# 2-nap falsifier (issue body), run against the REAL engram_nap() MCP tool
# end-to-end, not just the bare predicate function. Pre-fix
# (session-start anchored), BOTH naps in this sequence would have read the
# old session-scoped signal True off the same session-start marker and
# stayed silent throughout, even with the section stale between them -- the
# exact bug #3014 reports (Borges: four naps, ten hours, still silent;
# Kepler: a post-compaction nap, still silent). Post-fix, the anchor is
# the LAST CHECKPOINT rather than session start, so nap #2 must re-detect
# staleness even though nap #1 already fired once.
# ---------------------------------------------------------------------------

def test_two_nap_falsifier_second_nap_detects_stale_section_left_untouched():
    """Borges's falsifier (#3014 issue body): nap twice with the "From this
    session" section left stale between them -- the SECOND nap must not
    stay silent just because the FIRST nap already happened."""
    server = fresh_server()
    wb_path = server.core.DATA_DIR / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nStale content from before any checkpoint.\n",
        encoding="utf-8",
    )
    # Force the mtime safely into the past so it unambiguously predates
    # BOTH naps below regardless of real wall-clock execution speed.
    _touch_at(wb_path, datetime.now(timezone.utc) - timedelta(hours=1))

    # --- Nap #1: no prior checkpoint exists yet in session_log.md. ---
    r1 = json.loads(server.engram_nap(payload_json=json.dumps({"message": "first nap"})))
    assert "error" not in r1, f"nap #1 returned an error: {r1}"
    wb1 = r1["warm_briefing"]
    assert wb1["written_since_last_nap"] is None, (
        f"nap #1 has no PRIOR checkpoint to compare against -- must read "
        f"None (can't determine), not a guessed True/False; got {wb1}"
    )

    # --- warm-briefing.md is deliberately left untouched between naps
    #     (simulating: Step 5b was skipped after nap #1). ---

    # --- Nap #2: now there IS a prior checkpoint (nap #1's own header). ---
    r2 = json.loads(server.engram_nap(payload_json=json.dumps({"message": "second nap"})))
    assert "error" not in r2, f"nap #2 returned an error: {r2}"
    wb2 = r2["warm_briefing"]
    assert wb2["written_since_last_nap"] is False, (
        f"REGRESSION (#3014): warm-briefing.md's mtime predates nap #1's "
        f"checkpoint, so nap #2 must read written_since_last_nap=False -- "
        f"pre-fix, this stayed silently True (session-start anchor); got {wb2}"
    )
    assert wb2.get("note") == "Step 5b not performed since the last checkpoint", (
        f"THE bug this issue exists to fix: the SECOND nap must not stay "
        f"silent just because Step 5b ran (or didn't) earlier in the same "
        f"session -- got {wb2}"
    )


def test_two_nap_falsifier_positive_control_second_nap_stays_silent_on_real_update():
    """Positive control for the falsifier above: confirms the fix doesn't
    just always report stale -- a genuine Step-5b rewrite between naps
    must still read as confidently non-stale (silent, no note) on the
    SECOND nap."""
    server = fresh_server()
    wb_path = server.core.DATA_DIR / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nStale content from before any checkpoint.\n",
        encoding="utf-8",
    )
    _touch_at(wb_path, datetime.now(timezone.utc) - timedelta(hours=1))

    r1 = json.loads(server.engram_nap(payload_json=json.dumps({"message": "first nap"})))
    assert "error" not in r1, f"nap #1 returned an error: {r1}"

    # Real Step 5b: rewrite the section with fresh content, mtime strictly
    # AFTER nap #1's own checkpoint timestamp -- read that timestamp back
    # off session_log.md (the same value nap #2's own
    # _last_checkpoint_timestamp call will see) and touch a few seconds
    # past it, guaranteeing ordering deterministically (checkpoint
    # timestamps truncate to whole seconds, so real wall-clock sleep isn't
    # a reliable way to guarantee this).
    nap1_ts_str = server._last_checkpoint_timestamp(server.core.LOG_PATH)
    assert nap1_ts_str is not None, "nap #1 must have written a checkpoint header"
    nap1_ts = datetime.fromisoformat(nap1_ts_str).replace(tzinfo=timezone.utc)
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nGenuinely updated after nap #1.\n",
        encoding="utf-8",
    )
    _touch_at(wb_path, nap1_ts + timedelta(seconds=5))

    r2 = json.loads(server.engram_nap(payload_json=json.dumps({"message": "second nap"})))
    assert "error" not in r2, f"nap #2 returned an error: {r2}"
    wb2 = r2["warm_briefing"]
    assert wb2["written_since_last_nap"] is True, (
        f"a genuine Step-5b rewrite after nap #1 must read as written since "
        f"the last checkpoint; got {wb2}"
    )
    assert "note" not in wb2, (
        f"both signals are confidently non-stale -- note must be ABSENT "
        f"(silent success), not present; got {wb2}"
    )


# ---------------------------------------------------------------------------
# #3030 regression: the note wording, in the ONE case that makes it
# observable. Neither the unit tests above (no session concept at all,
# post-#3014) nor the two-nap falsifier/positive-control pair just above
# (both force warm-briefing's mtime an hour into the past -- stale from
# BEFORE the session as well as before the checkpoint) exercise this case.
# ---------------------------------------------------------------------------

def test_three_nap_regression_note_wording_true_under_both_anchors():
    """#3030: warm-briefing rotated ONCE, mid-session, then a further nap
    happens with no further rotation -- the ordinary steady state in real
    use, and the one region this file's existing tests never reach.

    Sequence (modeled on
    test_two_nap_falsifier_second_nap_detects_stale_section_left_untouched
    above, but instead of forcing mtime an hour into the past before ANY
    checkpoint, the rotation happens strictly AFTER the first checkpoint):
    nap #1 -> rotate warm-briefing (mtime just after nap #1's own
    checkpoint) -> nap #2 -> touch nothing -> nap #3.

    At nap #3, warm-briefing.md's mtime is AFTER nap #1's checkpoint (so
    the file genuinely WAS written "this session") but BEFORE nap #2's
    checkpoint (the LAST one) -- so written_since_last_nap must read
    False. This is precisely the case where the two candidate note strings
    diverge: "Step 5b not performed this session" is FALSE (it manifestly
    was performed, right after nap #1), while "Step 5b not performed since
    the last checkpoint" is true. Pre-#3030, the code emitted the former
    unconditionally on this branch; this test pins the corrected string
    and, via the note below, documents why the old one would have failed
    here."""
    server = fresh_server()
    wb_path = server.core.DATA_DIR / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nPre-session stale content.\n",
        encoding="utf-8",
    )
    _touch_at(wb_path, datetime.now(timezone.utc) - timedelta(hours=1))

    # --- Nap #1: no prior checkpoint yet. ---
    r1 = json.loads(server.engram_nap(payload_json=json.dumps({"message": "first nap"})))
    assert "error" not in r1, f"nap #1 returned an error: {r1}"

    # --- Step 5b performed once, immediately after nap #1 (mtime = real
    #     "now", which postdates nap #1's own just-written checkpoint). A
    #     real (not simulated) sleep follows, crossing at least one whole
    #     second boundary -- checkpoint timestamps truncate to whole
    #     seconds (core._now()[:19]), so nap #2's checkpoint must be
    #     forced to land in a LATER second than this write, or nap #2's
    #     real-time checkpoint could tie or precede it and defeat the
    #     ordering this test depends on. ---
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nRotated once, right after nap #1.\n",
        encoding="utf-8",
    )
    time.sleep(1.1)

    # --- Nap #2: sanity check -- the rotation above must register as
    #     written-since-nap-#1 (confirms the fixture is set up correctly
    #     before we move on to the actual discriminating nap). ---
    r2 = json.loads(server.engram_nap(payload_json=json.dumps({"message": "second nap"})))
    assert "error" not in r2, f"nap #2 returned an error: {r2}"
    wb2 = r2["warm_briefing"]
    assert wb2["written_since_last_nap"] is True, (
        f"warm-briefing was rotated after nap #1's checkpoint -- nap #2 "
        f"must read written_since_last_nap=True; got {wb2}"
    )
    assert "note" not in wb2, (
        f"nap #2 sees a genuine post-checkpoint rotation -- must stay "
        f"silent; got {wb2}"
    )

    # --- warm-briefing.md is deliberately left untouched between nap #2
    #     and nap #3 (Step 5b skipped after nap #2). ---

    # --- Nap #3: THE discriminating case. warm-briefing's mtime is after
    #     nap #1's checkpoint (written "this session") but before nap #2's
    #     checkpoint (the LAST one) -- the increment #3014's anchor change
    #     added, and the one place the pre-#3030 note text was false. ---
    r3 = json.loads(server.engram_nap(payload_json=json.dumps({"message": "third nap"})))
    assert "error" not in r3, f"nap #3 returned an error: {r3}"
    wb3 = r3["warm_briefing"]
    assert wb3["written_since_last_nap"] is False, (
        f"warm-briefing's mtime predates nap #2's checkpoint (the LAST "
        f"one) even though it postdates nap #1's -- nap #3 must read "
        f"written_since_last_nap=False; got {wb3}"
    )
    assert wb3.get("note") == "Step 5b not performed since the last checkpoint", (
        f"#3030 REGRESSION: the pre-fix string 'Step 5b not performed "
        f"this session' would have been FALSE in exactly this case -- the "
        f"file WAS written this session (right after nap #1); it merely "
        f"predates the LAST checkpoint (nap #2's). The corrected wording "
        f"must be true under both anchors; got {wb3}"
    )
