"""Tests for engram_nap's warm-briefing advisory (#2997).

engram_nap()'s return grows a `warm_briefing` block surfacing when the
engram-nap skill's Step 5b (warm-briefing "From this session" sync with
the current context-window arc) was NOT performed this session -- that
step is agent-executed prose, not code, so nothing fails when it's
skipped and the omission is otherwise invisible until a LATER session
reads a stale/empty warm briefing.

This file pins the real, importable predicate
(`server._compute_warm_briefing_advisory`) against real files rather than
hand-typed fixtures wherever practical: a real warm-briefing.md written to
tmp_path, and a real per-session marker JSON written to the (test-
isolated) data dir's sessions/ directory -- the same marker shape the
SessionStart hook itself writes (see
src/engram/hooks/claude/engram-session-start-hook.py's write_marker(),
which documents `session_started_at` as written ONCE on the session's
first-ever SessionStart and carried forward verbatim thereafter -- the
correct field for "did this file get touched since the burst actually
began", unlike `started_at`, which is re-written on every SessionStart
for the same session_id including post-compaction reconnects).

Three signals, computed independently and never allowed to raise:
  - mtime                    -- warm_briefing_path's last-write time (or
                                 None if the file doesn't exist).
  - written_this_session     -- True/False/None; None when session_id is
                                 omitted or its marker is unresolvable.
  - session_section_empty    -- True/False/None; anchored on the LAST
                                 `^## From this session` line (never a
                                 substring match -- same rule as
                                 #2986/#2989's drift-fix discipline), None
                                 when the file carries no such heading at
                                 all.

`note` is three-way, not binary (#2998 round 3): silent (no `note` key)
requires BOTH signals to be confidently non-stale (written_this_session is
True AND session_section_empty is False); a CONFIRMED-stale note ("Step 5b
not performed this session") fires when written_this_session is False OR
session_section_empty is True; an UNKNOWN note (states which signal(s)
couldn't be determined, e.g. "could not determine whether Step 5b ran (no
session_id passed)") fires for everything else -- i.e. when neither signal
confirms staleness but at least one is None. Confirmed-stale takes
precedence over unknown when both are simultaneously present. Any unknown
(None) signal still emits SOME note -- this never produces a false "all
clear" -- but the note's wording no longer asserts a fact the code
declined to determine (issue #2997 leaves the exact predicate to the
implementer; see _compute_warm_briefing_advisory's own docstring in
server.py for the reasoning restated as a code comment).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
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


def _write_session_marker(server, session_id: str, session_started_at: str) -> None:
    """Writes a real per-session marker JSON, same shape write_marker() in
    engram-session-start-hook.py produces (only session_started_at is
    load-bearing for this predicate; the rest is filled in for realism)."""
    sessions_dir = server.core.DATA_DIR / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "session_id": session_id,
        "transcript_path": "/tmp/fake-transcript.jsonl",
        "source": "startup",
        "cwd": "/tmp",
        "started_at": session_started_at,
        "role": "interactive",
        "purpose": "test",
        "session_started_at": session_started_at,
        "injected_through": None,
    }
    (sessions_dir / f"{session_id}.json").write_text(json.dumps(marker), encoding="utf-8")


def _touch_at(path, when: datetime) -> None:
    """Sets path's mtime to a specific point in time -- deterministic
    control over the written_this_session before/after comparison rather
    than relying on real wall-clock timing between statements."""
    ts = when.timestamp()
    os.utime(path, (ts, ts))


# ---------------------------------------------------------------------------
# (a) file written AFTER session start -> written_this_session: true
# ---------------------------------------------------------------------------

def test_written_after_session_start_is_true(tmp_path):
    server = fresh_server()
    session_id = "sess-a-1234"
    started = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    _write_session_marker(server, session_id, started.isoformat())

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nSomething written.\n", encoding="utf-8")
    _touch_at(wb_path, started + timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, session_id)
    assert result["written_this_session"] is True, result


# ---------------------------------------------------------------------------
# (b) file NOT written since session start -> written_this_session: false
# ---------------------------------------------------------------------------

def test_written_before_session_start_is_false(tmp_path):
    server = fresh_server()
    session_id = "sess-b-1234"
    started = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    _write_session_marker(server, session_id, started.isoformat())

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nStale content.\n", encoding="utf-8")
    _touch_at(wb_path, started - timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, session_id)
    assert result["written_this_session"] is False, result


# ---------------------------------------------------------------------------
# (c) no session_id passed -> written_this_session: null
# ---------------------------------------------------------------------------

def test_no_session_id_is_null(tmp_path):
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nContent.\n", encoding="utf-8")

    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["written_this_session"] is None, result
    # #2998 round-3 colleague review named THIS exact fixture as the one
    # where the pre-fix code asserted a false definite claim ("Step 5b not
    # performed this session") about a file that was, in fact, updated --
    # session_section_empty is confidently False (real content) here, so
    # the note must read as UNKNOWN ("can't tell"), never confirmed-stale.
    assert result["session_section_empty"] is False, result
    assert result.get("note") == (
        "could not determine whether Step 5b ran (no session_id passed)"
    ), result


# ---------------------------------------------------------------------------
# (d) session marker file missing/corrupt -> null, not a crash
# ---------------------------------------------------------------------------

def test_missing_session_marker_is_null_not_crash(tmp_path):
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nContent.\n", encoding="utf-8")

    result = server._compute_warm_briefing_advisory(wb_path, "sess-does-not-exist")
    assert result["written_this_session"] is None, result


def test_corrupt_session_marker_is_null_not_crash(tmp_path):
    server = fresh_server()
    session_id = "sess-corrupt-1234"
    sessions_dir = server.core.DATA_DIR / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{session_id}.json").write_text("{not valid json", encoding="utf-8")

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nContent.\n", encoding="utf-8")

    result = server._compute_warm_briefing_advisory(wb_path, session_id)
    assert result["written_this_session"] is None, result


def test_path_traversal_session_id_is_null_not_crash(tmp_path):
    """session_id is gated through the same path-traversal allowlist regex
    _resolve_session_transcript uses (engram_observation.py's
    _SESSION_ID_RE) -- an id containing path separators must never reach
    the filesystem, and must degrade to null rather than raising."""
    server = fresh_server()
    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n\nContent.\n", encoding="utf-8")

    result = server._compute_warm_briefing_advisory(wb_path, "../../etc/passwd")
    assert result["written_this_session"] is None, result


# ---------------------------------------------------------------------------
# (e) heading present with trailing content -> session_section_empty: false
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
    session_id = "sess-note-clear"
    started = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    _write_session_marker(server, session_id, started.isoformat())

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nFresh content.\n",
        encoding="utf-8",
    )
    _touch_at(wb_path, started + timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, session_id)
    assert result["written_this_session"] is True, result
    assert result["session_section_empty"] is False, result
    assert "note" not in result, (
        f"note must be ABSENT when both signals are confidently non-stale; got {result}"
    )


def test_note_present_when_written_this_session_false(tmp_path):
    server = fresh_server()
    session_id = "sess-note-stale-1"
    started = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    _write_session_marker(server, session_id, started.isoformat())

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nStale content, but not empty.\n",
        encoding="utf-8",
    )
    _touch_at(wb_path, started - timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, session_id)
    assert result["written_this_session"] is False, result
    assert result["session_section_empty"] is False, result
    assert result.get("note") == "Step 5b not performed this session", result


def test_note_present_when_session_section_empty_true(tmp_path):
    server = fresh_server()
    session_id = "sess-note-stale-2"
    started = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    _write_session_marker(server, session_id, started.isoformat())

    wb_path = tmp_path / "warm-briefing.md"
    wb_path.write_text("# Warm Briefing\n\n## From this session\n", encoding="utf-8")
    _touch_at(wb_path, started + timedelta(minutes=5))

    result = server._compute_warm_briefing_advisory(wb_path, session_id)
    assert result["written_this_session"] is True, result
    assert result["session_section_empty"] is True, result
    assert result.get("note") == "Step 5b not performed this session", result


def test_note_present_when_both_signals_unknown():
    """No session_id (written_this_session -> None) AND no warm_briefing
    file at all (session_section_empty -> None): both unknown, note must
    still be present -- unknown is never treated as 'all clear'. Neither
    signal is a CONFIRMED negative here (both are None, not False/True), so
    this is the UNKNOWN branch (#2998 round 3), not the confirmed-stale
    one -- the note must say what's unknown, not assert a definite claim
    the code never actually confirmed."""
    server = fresh_server()
    nonexistent_path = Path(tempfile.mkdtemp()) / "does-not-exist.md"
    result = server._compute_warm_briefing_advisory(nonexistent_path, None)
    assert result["written_this_session"] is None, result
    assert result["session_section_empty"] is None, result
    assert result.get("note") == (
        "could not determine whether Step 5b ran "
        "(no session_id passed; no '## From this session' heading found)"
    ), result


def test_note_omitted_session_id_on_genuinely_updated_file_is_unknown_not_stale():
    """#2998 round-3 colleague review's own falsifying demonstration,
    pinned as a regression test (Borges, live on his own warm-briefing.md):
    a warm-briefing.md that DOES have real trailing content after
    "## From this session" (session_section_empty -> False, genuinely NOT
    stale) with session_id omitted (written_this_session -> None, can't
    determine) must NOT read as confirmed-stale -- nothing here has been
    confirmed stale; the honest note is "can't tell", not "Step 5b not
    performed this session". Pre-fix, both cases rendered byte-identically:
    a None written_this_session was treated the same as a confirmed False,
    producing a false alarm worded as a definite finding."""
    server = fresh_server()
    wb_path = Path(tempfile.mkdtemp()) / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nGenuinely updated just now.\n",
        encoding="utf-8",
    )

    result = server._compute_warm_briefing_advisory(wb_path, None)
    assert result["written_this_session"] is None, result
    assert result["session_section_empty"] is False, result
    assert "note" in result, (
        f"unknown must still emit SOME note -- never a silent 'all clear'; got {result}"
    )
    assert result["note"] != "Step 5b not performed this session", (
        f"REGRESSION: an undetermined written_this_session must not render "
        f"as the confirmed-stale claim; got {result}"
    )
    assert result["note"] == (
        "could not determine whether Step 5b ran (no session_id passed)"
    ), result


# ---------------------------------------------------------------------------
# (j) non-string session_id -- PR #2998 review round 1's found bug.
#
# session_id is documented as str | None, but nothing upstream enforces
# that at the JSON-payload boundary (payload_json permits any JSON type
# for session_id). Pre-fix, a non-string session_id made
# _observation_mod._SESSION_ID_RE.fullmatch(session_id) raise TypeError --
# not OSError/json.JSONDecodeError, so the narrow except around the
# marker-resolution block didn't catch it -- which propagated to the
# function's single shared outer `except Exception: pass`, silently
# skipping the otherwise-independent session_section_empty computation
# AND the note assignment, producing a note-absent ("all clear") result
# even though both signals read None ("not confidently non-stale"). This
# directly violated the function's own documented invariant ("this never
# produces a false 'all clear'"). See PR #2998's review round-1 comment
# for the original reproduction this section pins (org-private URL
# omitted deliberately -- flagged by this repo's scan-leaks gate).
# ---------------------------------------------------------------------------

def test_non_string_session_id_still_computes_session_section_empty_and_emits_note():
    """THE regression test for the found bug: a non-string session_id must
    not take session_section_empty (a structurally independent signal)
    down with it, and the note key must never silently go missing.

    Uses a warm-briefing.md WITH a real "## From this session" heading
    AND real trailing content, so session_section_empty should
    independently resolve to False (confidently non-stale) -- NOT None --
    regardless of what happens with session_id. written_this_session must
    degrade to None (session_id isn't usable), which per the documented
    note condition means note must still be present -- and, since neither
    signal is a CONFIRMED negative here (session_section_empty is False,
    not True; written_this_session is None, not False), it must be the
    UNKNOWN wording, not the confirmed-stale one (#2998 round 3 -- this is
    the colleague review's own falsifying demonstration: a None
    written_this_session used to render byte-identically to a confirmed
    False).
    """
    server = fresh_server()
    wb_path = tempfile.mkdtemp()
    wb_path = Path(wb_path) / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nReal, non-empty content.\n",
        encoding="utf-8",
    )

    for bad_session_id in (123, [1, 2, 3], {"a": 1}, True, 4.5):
        result = server._compute_warm_briefing_advisory(wb_path, bad_session_id)
        assert result["session_section_empty"] is False, (
            f"session_section_empty must be independently computed despite "
            f"a non-string session_id={bad_session_id!r}; got {result}"
        )
        assert result["written_this_session"] is None, (
            f"written_this_session must degrade to None (can't determine) "
            f"for a non-string session_id={bad_session_id!r}; got {result}"
        )
        assert "note" in result, (
            f"REGRESSION: note key silently dropped for non-string "
            f"session_id={bad_session_id!r} -- this is exactly the false "
            f"'all clear' the function's docstring says never happens; "
            f"got {result}"
        )
        assert result["note"] == (
            "could not determine whether Step 5b ran (no session_id passed)"
        ), (
            f"REGRESSION (#2998 round 3): a None written_this_session (from "
            f"a non-string session_id={bad_session_id!r}) must render as an "
            f"UNKNOWN note, never the confirmed-stale text -- got {result}"
        )


def _buggy_pre_fix_advisory(server, warm_briefing_path, session_id):
    """Reproduces the OLD (pre-#2998-round-1-fix) single-shared-outer-try
    structure exactly as it read before this fix -- used only as a
    mutation-style control demonstration below, never as something to keep
    working. Mirrors this file's own reference convention
    (test_engram_nap_backup_health_check_2680.py's `_buggy_pre_fix_check`):
    a control that has only ever been observed to fail is not yet a
    control, so this reproduction is exercised directly against the same
    real input the fixed function handles correctly.

    The load-bearing defect this reproduces: mtime, written_this_session,
    session_section_empty, and the final note assignment all nested inside
    ONE shared outer `try: ... except Exception: pass` -- so an
    unanticipated exception anywhere upstream (here: TypeError from
    .fullmatch() on a non-str session_id) aborts everything downstream,
    including the otherwise-independent session_section_empty computation
    and the note assignment.
    """
    result = {"mtime": None, "written_this_session": None, "session_section_empty": None}
    try:
        mtime_epoch = None
        try:
            mtime_epoch = os.path.getmtime(warm_briefing_path)
            result["mtime"] = (
                datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except OSError:
            pass

        # OLD shape: NOT independently wrapped -- and the narrow except
        # below only catches OSError/json.JSONDecodeError, not the
        # TypeError .fullmatch() raises on a non-str session_id.
        if session_id and mtime_epoch is not None:
            session_started_at = None
            try:
                if server._observation_mod._SESSION_ID_RE.fullmatch(session_id):
                    marker_path = server.core.DATA_DIR / "sessions" / f"{session_id}.json"
                    with open(marker_path, "r", encoding="utf-8") as f:
                        marker = json.load(f)
                    if isinstance(marker, dict):
                        session_started_at = marker.get("session_started_at")
            except (OSError, json.JSONDecodeError):
                session_started_at = None

            if session_started_at:
                try:
                    started_dt = datetime.fromisoformat(
                        session_started_at.replace("Z", "+00:00")
                    )
                    result["written_this_session"] = mtime_epoch >= started_dt.timestamp()
                except (ValueError, AttributeError, TypeError):
                    pass

        # Unreachable when the TypeError above propagates past this point.
        try:
            with open(warm_briefing_path, "r", encoding="utf-8") as f:
                content = f.read()
            matches = list(server._WARM_BRIEFING_SESSION_HEADING_RE.finditer(content))
            if matches:
                last_match = matches[-1]
                remainder = content[last_match.end():]
                result["session_section_empty"] = not remainder.strip()
        except OSError:
            pass

        # Unreachable when the TypeError above propagates past this point.
        if not (result["written_this_session"] is True and result["session_section_empty"] is False):
            result["note"] = "Step 5b not performed this session"
    except Exception:
        pass
    return result


def test_mutation_control_old_shape_drops_note_on_same_input_fixed_does_not():
    """Mutation-style control (mirrors
    test_engram_nap_backup_health_check_2680.py's
    test_mutation_control_both_defects_independently_break_the_fix): confirms
    the OLD single-shared-outer-try shape genuinely exhibits the reported
    bug on the SAME input the real, fixed function now handles correctly --
    so this is a demonstrated discriminating control, not a check that has
    only ever been observed to pass."""
    server = fresh_server()
    wb_dir = tempfile.mkdtemp()
    wb_path = Path(wb_dir) / "warm-briefing.md"
    wb_path.write_text(
        "# Warm Briefing\n\n## From this session\n\nReal, non-empty content.\n",
        encoding="utf-8",
    )

    buggy_result = _buggy_pre_fix_advisory(server, wb_path, 123)
    assert "note" not in buggy_result, (
        "control failed: the OLD single-shared-outer-try shape must drop "
        "the note key on a non-string session_id -- if it doesn't, this "
        "reproduction isn't actually demonstrating the reported bug; "
        f"got {buggy_result}"
    )
    assert buggy_result["session_section_empty"] is None, (
        "control failed: the OLD shape must also fail to compute "
        f"session_section_empty (never reached); got {buggy_result}"
    )

    fixed_result = server._compute_warm_briefing_advisory(wb_path, 123)
    assert "note" in fixed_result, (
        f"the FIXED function must not exhibit the same defect; got {fixed_result}"
    )
    assert fixed_result["session_section_empty"] is False, fixed_result


# ---------------------------------------------------------------------------
# mtime field itself -- basic shape checks
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
    # written_this_session (no session_id passed) nor session_section_empty
    # (open() on a directory raises, caught by the broad except) can be
    # confirmed -- both None, neither a CONFIRMED negative, so this is the
    # UNKNOWN branch (#2998 round 3), not confirmed-stale.
    assert result["written_this_session"] is None, result
    assert result["session_section_empty"] is None, result
    assert result.get("note") == (
        "could not determine whether Step 5b ran "
        "(no session_id passed; no '## From this session' heading found)"
    ), result


# ---------------------------------------------------------------------------
# Integration: engram_nap()'s own return carries the warm_briefing block,
# and never raises when session_id is omitted from (or present in) the
# payload -- session_id must be consumed by the wrapper before reaching
# _nap_impl, which has no such parameter.
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
    assert "written_this_session" in wb
    assert "session_section_empty" in wb
    # No session_id was passed -- written_this_session must be null.
    assert wb["written_this_session"] is None, wb


def test_engram_nap_accepts_session_id_field_without_reaching_nap_impl():
    """session_id must be popped from params by the wrapper before calling
    _nap_impl(**params) -- _nap_impl's signature is def _nap_impl(message:
    str), which has no session_id parameter, so passing session_id through
    unmodified would raise TypeError: _nap_impl() got an unexpected keyword
    argument 'session_id'."""
    server = fresh_server()
    r = json.loads(server.engram_nap(payload_json=json.dumps({
        "message": "Test nap with session_id in payload.",
        "session_id": "sess-integration-test",
    })))
    assert "error" not in r, f"engram_nap returned error with session_id set: {r}"
    assert "warm_briefing" in r, r
