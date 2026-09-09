"""Unit tests for src/engram/tools/fairy_report.py (#2760).

Synthetic JSONL fixtures under tmp_path, monkeypatching
`~/.claude/projects/` (via a tmp_path override, see the `projects_dir`
fixture) rather than reading this developer's own real, private session
transcripts. Mirrors tests/test_supervision_probe.py's import convention:
sys.path append + direct module import, no package install needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _REPO_ROOT / "src" / "engram" / "tools"
sys.path.append(str(_TOOLS))

import fairy_report as fr  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """A fresh, empty ~/.claude/projects/ under tmp_path, wired via
    monkeypatching Path.expanduser so `_resolve_session_files`'s glob
    against "~/.claude/projects" resolves here instead of the real one."""
    fake_home_projects = tmp_path / "claude-projects"
    fake_home_projects.mkdir()

    real_expanduser = Path.expanduser

    def fake_expanduser(self):
        if str(self) == "~/.claude/projects":
            return fake_home_projects
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", fake_expanduser)
    return fake_home_projects


def _envelope(task_id: str, summary: str, status: str, result: str | None) -> str:
    """Build one <task-notification> envelope's plain-text body, matching
    the real (undocumented, empirically observed) shape: <result> is
    omitted entirely when there's no result (e.g. a task that errored)."""
    result_block = f"<result>{result}</result>\n" if result is not None else ""
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        "<tool-use-id>toolu_fake</tool-use-id>\n"
        f"<summary>{summary}</summary>\n"
        f"<status>{status}</status>\n"
        f"{result_block}"
        "</task-notification>"
    )


def _write_session(
    projects_dir: Path,
    project_slug: str,
    session_id: str,
    records: list[dict],
) -> Path:
    project_path = projects_dir / project_slug
    project_path.mkdir(parents=True, exist_ok=True)
    session_file = project_path / f"{session_id}.jsonl"
    with open(session_file, "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    return session_file


def _queue_op_record(content: str) -> dict:
    """A minimal record shaped like a real queue-operation entry."""
    return {"type": "queue-operation", "operation": "enqueue", "content": content}


def _attachment_record(prompt: str) -> dict:
    """A minimal record shaped like a real attachment entry."""
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": prompt}}


# ---------------------------------------------------------------------------
# extraction correctness
# ---------------------------------------------------------------------------


class TestIterEnvelopes:
    """Round-3 reviewer-fairy finding on this tool's own PR (#2766): a raw
    text field is not guaranteed to hold exactly one envelope. These test
    the outer-boundary delimiting step directly, independent of the inner
    (result/usage) extraction logic."""

    def test_single_envelope(self):
        env = _envelope("task-a", "s", "completed", "one result")
        found = list(fr._iter_envelopes(env))
        assert found == [env]

    def test_two_concatenated_envelopes_yield_two_distinct_slices(self):
        env1 = _envelope("task-a", "s", "completed", "result ONE")
        env2 = _envelope("task-b", "s", "completed", "result TWO")
        concatenated = env1 + "\n" + env2
        found = list(fr._iter_envelopes(concatenated))
        assert len(found) == 2
        assert found[0] == env1
        assert found[1] == env2

    def test_no_envelope_yields_nothing(self):
        assert list(fr._iter_envelopes("plain text, no envelope here")) == []

    def test_truncated_final_envelope_yields_the_remainder_once(self):
        """A reboot can cut a transcript mid-write -- the tail has an
        opening tag but no matching close. Recover the partial text rather
        than silently dropping it."""
        text = "<task-notification>\n<task-id>truncated</task-id>\n<result>partial rep"
        found = list(fr._iter_envelopes(text))
        assert found == [text]

    def test_result_discussing_the_outer_tags_themselves_is_not_truncated(self):
        """Self-found (by testing the docstring's own claim directly, not
        by a 4th review round): the initial fix for round 3 used a plain
        forward find() for the OUTER close tag too, reasoning that
        '<task-notification>'/'</task-notification>' are too unusual for a
        fairy report to quote as prose -- falsified by this very PR's own
        commit history, which discusses that exact tag name repeatedly.
        A result that quotes BOTH the opening and closing outer tags as
        literal text (the closing one appearing before the genuine
        structural close) must not truncate the envelope early."""
        result_text = (
            "This bug involves the <task-notification> wrapper and "
            "specifically the </task-notification> closing tag being "
            "found too early. The real content continues after this."
        )
        envelope = (
            "<task-notification>\n<task-id>task-x</task-id>\n"
            "<status>completed</status>\n<summary>s</summary>\n"
            f"<result>{result_text}</result>\n"
            "<usage><subagent_tokens>100</subagent_tokens></usage>\n"
            "</task-notification>"
        )
        found = list(fr._iter_envelopes(envelope))
        assert found == [envelope]

    def test_outer_tag_prose_collision_alongside_a_genuine_second_envelope(self):
        """The boundary-validation heuristic must handle BOTH hazards at
        once: a first envelope whose result discusses the outer tags as
        prose, immediately followed by a second, genuine envelope -- the
        heuristic must skip the prose false-positive and land on the real
        close, then correctly find the second envelope too."""
        result_text = (
            "Discusses <task-notification> and </task-notification> as "
            "literal text, then the report continues normally."
        )
        env1 = (
            "<task-notification>\n<task-id>a</task-id>\n"
            f"<result>{result_text}</result>\n</task-notification>"
        )
        env2 = "<task-notification>\n<task-id>b</task-id>\n<result>Result TWO</result>\n</task-notification>"
        found = list(fr._iter_envelopes(env1 + "\n" + env2))
        assert found == [env1, env2]

    def test_result_quoting_close_then_open_tag_back_to_back_is_not_split(self):
        """Round-4 reviewer-fairy finding on this PR: the round-3 fix's
        acceptance check only verified the text after a candidate close
        STARTS WITH the open-tag substring -- satisfied by prose alone, no
        second envelope required. A single, well-formed envelope whose own
        result quotes both tags back-to-back (this file's own docstrings do
        exactly this) followed by ordinary prose must round-trip as ONE
        envelope with its full result intact, not split into a truncated
        first piece and a dropped second piece. Minimal repro from the
        reviewer's live report, reproduced here."""
        prose = (
            "The tags look like this back to back:\n"
            "</task-notification>\n<task-notification>\n"
            "and the REAL content continues here: SECRET_TAIL_CONTENT."
        )
        envelope = (
            "<task-notification>\n<task-id>task-x</task-id>\n"
            f"<result>{prose}</result>\n</task-notification>"
        )
        found = list(fr._iter_envelopes(envelope))
        assert found == [envelope]
        parsed = fr._extract_task_notification(envelope)
        assert parsed is not None
        assert "SECRET_TAIL_CONTENT" in parsed["result"]


class TestFindByTaskId:
    def test_two_envelopes_in_one_record_do_not_cross_contaminate(self, projects_dir):
        """Round-3 reviewer-fairy's headline finding: when one record's raw
        text field genuinely concatenates two task-notification envelopes,
        the round-3-pre-fix code's rfind for <usage> would find the SECOND
        envelope's tag and bleed its content into the FIRST envelope's
        result. Confirmed as a strict regression (correct at round 2's
        head, corrupted at round 3's pre-fix head) before this fix."""
        env1 = _envelope("task-first", "s", "completed", "Result ONE text")
        env2 = _envelope("task-second", "s", "completed", "Result TWO text, unrelated")
        record = _queue_op_record(env1 + "\n" + env2)
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [record])

        first = fr.find_by_task_id("task-first", session="sess-1")
        assert first is not None
        assert first.result == "Result ONE text"
        assert "Result TWO" not in first.result

        second = fr.find_by_task_id("task-second", session="sess-1")
        assert second is not None
        assert second.result == "Result TWO text, unrelated"


    def test_extracts_verbatim_result_text(self, projects_dir):
        envelope = _envelope("task-1", "Review PR #100", "completed", "**APPROVE**, no blockers.")
        _write_session(
            projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)]
        )
        found = fr.find_by_task_id("task-1", session="sess-1")
        assert found is not None
        assert found.result == "**APPROVE**, no blockers."
        assert found.summary == "Review PR #100"
        assert found.status == "completed"

    def test_result_can_contain_markdown_with_angle_brackets(self, projects_dir):
        """A real fairy report routinely quotes code containing angle
        brackets (e.g. `/proc/<pid>/cgroup`) -- must not be mistaken for a
        tag boundary."""
        text = "Checked `/proc/<pid>/cgroup` and confirmed the guard."
        envelope = _envelope("task-2", "s", "completed", text)
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        found = fr.find_by_task_id("task-2", session="sess-1")
        assert found.result == text

    def test_result_containing_literal_closing_result_tag_is_not_truncated(self, projects_dir):
        """Reviewer-fairy finding on this tool's own PR (#2766 round 1):
        a naive first-<result>/first-</result> search silently truncates
        when the result text itself contains the literal substring
        '</result>' -- highly plausible for exactly this tool's use case
        (fairy reports routinely quote tag syntax verbatim). No <usage>
        block in this envelope -- exercises the </task-notification>
        fallback boundary."""
        text = "The bug: a naive search for </result> truncates here, but the real text continues past this point and must all be recovered."
        envelope = _envelope("task-2b", "s", "completed", text)
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        found = fr.find_by_task_id("task-2b", session="sess-1")
        assert found.result == text
        assert "must all be recovered" in found.result  # would be truncated before this under the old bug

    def test_result_containing_literal_closing_result_tag_with_usage_sibling(self, projects_dir):
        """Same bug, but with a real <usage> sibling block following
        <result> (the actual shape live task-notifications carry) -- the
        <usage> anchor must not itself get swallowed by the literal
        '</result>' text, and must not falsely terminate the result early
        either."""
        text = "Discusses </result> as literal text, then a usage tag: <usage>not real, just prose</usage> mid-report."
        envelope = (
            "<task-notification>\n"
            "<task-id>task-2c</task-id>\n"
            "<status>completed</status>\n"
            "<summary>s</summary>\n"
            f"<result>{text}</result>\n"
            "<usage><subagent_tokens>100</subagent_tokens><tool_uses>5</tool_uses><duration_ms>1000</duration_ms></usage>\n"
            "<worktree><worktreePath>/tmp/x</worktreePath></worktree>\n"
            "</task-notification>"
        )
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        found = fr.find_by_task_id("task-2c", session="sess-1")
        assert found.result == text

    def test_result_containing_the_usage_anchor_string_itself_as_prose(self, projects_dir):
        """Round-2 reviewer-fairy finding on this tool's own PR (#2766):
        the fix for the round-1 bug reproduced the SAME bug class at a
        different anchor -- a FORWARD (first-occurrence) search for
        '<usage><subagent_tokens>' matches inside free-form result text
        that happens to discuss that exact literal string (plausible for a
        report about this very extraction logic), truncating before the
        genuine, later usage block. The genuine block is always the LAST
        occurrence before </task-notification> -- this locks that in."""
        text = (
            "The anchor this tool searches for is the literal string "
            "<usage><subagent_tokens>, which a report about this bug must "
            "quote verbatim, and the real content continues past it."
        )
        envelope = (
            "<task-notification>\n"
            "<task-id>task-2e</task-id>\n"
            "<status>completed</status>\n"
            "<summary>s</summary>\n"
            f"<result>{text}</result>\n"
            "<usage><subagent_tokens>100</subagent_tokens><tool_uses>5</tool_uses><duration_ms>1000</duration_ms></usage>\n"
            "<worktree><worktreePath>/tmp/x</worktreePath></worktree>\n"
            "</task-notification>"
        )
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        found = fr.find_by_task_id("task-2e", session="sess-1")
        assert found.result == text
        assert "the real content continues past it" in found.result

    def test_result_html_entities_are_unescaped(self, projects_dir):
        """The stored transcript HTML-escapes '<'/'>' inside a result's own
        text (observed live: /proc/<pid>/cgroup is stored as
        /proc/&lt;pid&gt;/cgroup) -- must be restored so the extracted text
        matches what a human would actually want piped into a PR comment."""
        stored_text = "Checked /proc/&lt;pid&gt;/cgroup and confirmed &amp; verified the guard."
        envelope = _envelope("task-2d", "s", "completed", stored_text)
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        found = fr.find_by_task_id("task-2d", session="sess-1")
        assert found.result == "Checked /proc/<pid>/cgroup and confirmed & verified the guard."

    def test_unescape_is_narrow_not_full_html5_legacy_table(self):
        """Round-2 reviewer-fairy suggestion: html.unescape() follows
        HTML5's ~2000+ semicolon-optional legacy entity names, so genuine
        prose like 'Fish & Chips &amp Co' (a real, never-escaped ampersand
        immediately followed by 'Co', no space, no semicolon) would
        silently mangle under the full table. This tool's narrower,
        exact-three-entity replace must NOT touch it."""
        text = "Fish & Chips &amp Co"
        assert fr._unescape_observed_entities(text) == text

    def test_unescape_ordering_handles_escaped_ampersand_before_lt(self):
        """A stored '&amp;lt;' represents a literal ampersand followed by
        the plain text 'lt;' (NOT an escaped '<') -- unescaping &amp; LAST
        (after &lt;/&gt;) is what keeps this correct; the wrong order would
        double-decode it into '<'."""
        assert fr._unescape_observed_entities("&amp;lt;") == "&lt;"

    def test_finds_result_in_attachment_record_shape(self, projects_dir):
        """The real transcript carries the SAME envelope under different
        keys depending on record type -- attachment.prompt must work too,
        not just queue-operation.content."""
        envelope = _envelope("task-3", "s", "completed", "attachment-shaped result")
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_attachment_record(envelope)])
        found = fr.find_by_task_id("task-3", session="sess-1")
        assert found is not None
        assert found.result == "attachment-shaped result"

    def test_returns_none_when_task_id_not_present(self, projects_dir):
        envelope = _envelope("task-1", "s", "completed", "result")
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        assert fr.find_by_task_id("task-does-not-exist", session="sess-1") is None

    def test_result_none_when_envelope_has_no_result_block(self, projects_dir):
        """A task that errored before producing output -- a real, distinct
        case from 'task-id not found at all'."""
        envelope = _envelope("task-4", "Errored task", "error", result=None)
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        found = fr.find_by_task_id("task-4", session="sess-1")
        assert found is not None
        assert found.result is None

    def test_duplicate_envelopes_same_task_id_take_the_last(self, projects_dir):
        """The harness's own documented behavior: 'the same task-id may
        notify more than once' -- enqueue/remove duplicates, or a genuinely
        re-fired task. Latest (last in scan order) wins."""
        first = _envelope("task-5", "s", "completed", "first result")
        second = _envelope("task-5", "s", "completed", "second, later result")
        _write_session(
            projects_dir,
            "-Users-x-repo",
            "sess-1",
            [_queue_op_record(first), _queue_op_record(second)],
        )
        found = fr.find_by_task_id("task-5", session="sess-1")
        assert found.result == "second, later result"

    def test_searches_across_sessions_when_session_omitted(self, projects_dir):
        envelope = _envelope("task-6", "s", "completed", "found across sessions")
        _write_session(projects_dir, "-Users-x-repo", "sess-a", [_queue_op_record(envelope)])
        _write_session(projects_dir, "-Users-x-repo", "sess-b", [])
        found = fr.find_by_task_id("task-6", session=None)
        assert found is not None
        assert found.result == "found across sessions"

    def test_malformed_line_without_the_marker_does_not_abort_the_scan(self, projects_dir):
        """A reboot can leave a transcript file with a truncated final
        line -- one bad line must not sink the whole recovery attempt.
        This particular malformed line lacks the task-notification marker,
        so it's actually skipped by the cheap pre-filter before json.loads
        is ever called -- see the sibling test below for the case that
        genuinely exercises the JSONDecodeError handler."""
        good = _envelope("task-7", "s", "completed", "the real result")
        session_file = projects_dir / "-Users-x-repo" / "sess-1.jsonl"
        session_file.parent.mkdir(parents=True)
        with open(session_file, "w") as f:
            f.write('{"type": "queue-operation", "content": "not json ]]]\n')
            f.write(json.dumps(_queue_op_record(good)) + "\n")
        found = fr.find_by_task_id("task-7", session="sess-1")
        assert found is not None
        assert found.result == "the real result"

    def test_malformed_line_containing_the_marker_hits_jsondecodeerror_and_is_skipped(self, projects_dir):
        """Reviewer-fairy finding on this tool's own PR (#2766 round 1):
        the sibling test above never actually exercises the
        `except (json.JSONDecodeError, ValueError)` handler, since its
        malformed line is filtered out by the cheap marker pre-check
        before json.loads() runs. This test's malformed line DOES contain
        the marker (a realistic reboot-truncation shape: the JSON was cut
        off mid-write, right after emitting readable task-notification
        text) -- it must reach json.loads(), fail there, and still not
        abort the scan for the good line that follows."""
        good = _envelope("task-7b", "s", "completed", "recovered despite the truncated neighbor")
        session_file = projects_dir / "-Users-x-repo" / "sess-1.jsonl"
        session_file.parent.mkdir(parents=True)
        with open(session_file, "w") as f:
            # Contains <task-notification> (passes the pre-filter) but is
            # truncated mid-string -- invalid JSON, must raise
            # JSONDecodeError when parsed, not something a pre-filter can
            # catch cheaply.
            f.write('{"type": "queue-operation", "content": "<task-notification>\\n<task-id>truncated\n')
            f.write(json.dumps(_queue_op_record(good)) + "\n")
        found = fr.find_by_task_id("task-7b", session="sess-1")
        assert found is not None
        assert found.result == "recovered despite the truncated neighbor"


class TestGrepTaskNotifications:
    def test_matches_on_summary(self, projects_dir):
        envelope = _envelope("task-8", "Review PR #2758 supervision probe", "completed", "r")
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        matches = fr.grep_task_notifications("supervision probe", session="sess-1")
        assert len(matches) == 1
        assert matches[0].task_id == "task-8"

    def test_matches_on_result_text(self, projects_dir):
        envelope = _envelope("task-9", "s", "completed", "the word ZEBRA appears here")
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        matches = fr.grep_task_notifications("zebra", session="sess-1")  # case-insensitive
        assert len(matches) == 1

    def test_no_match_returns_empty_list(self, projects_dir):
        envelope = _envelope("task-10", "s", "completed", "result")
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        assert fr.grep_task_notifications("nonexistent-pattern-xyz", session="sess-1") == []

    def test_dedupes_by_task_id_keeping_latest(self, projects_dir):
        first = _envelope("task-11", "PR review", "completed", "old")
        second = _envelope("task-11", "PR review", "completed", "new")
        _write_session(
            projects_dir,
            "-Users-x-repo",
            "sess-1",
            [_queue_op_record(first), _queue_op_record(second)],
        )
        matches = fr.grep_task_notifications("PR review", session="sess-1")
        assert len(matches) == 1
        assert matches[0].result == "new"


# ---------------------------------------------------------------------------
# CLI (main())
# ---------------------------------------------------------------------------


class TestMain:
    def test_task_mode_prints_only_the_result(self, projects_dir, capsys):
        envelope = _envelope("task-12", "s", "completed", "clean pipeable output")
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        rc = fr.main(["--task", "task-12", "--session", "sess-1"])
        assert rc == 0
        out = capsys.readouterr()
        assert out.out.strip() == "clean pipeable output"
        assert out.err == ""

    def test_task_mode_not_found_exits_nonzero_with_stderr_reason(self, projects_dir, capsys):
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [])
        rc = fr.main(["--task", "does-not-exist", "--session", "sess-1"])
        assert rc == 1
        out = capsys.readouterr()
        assert out.out == ""  # never a silent partial success on stdout
        assert "does-not-exist" in out.err

    def test_task_mode_no_result_block_exits_nonzero(self, projects_dir, capsys):
        envelope = _envelope("task-13", "s", "error", result=None)
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        rc = fr.main(["--task", "task-13", "--session", "sess-1"])
        assert rc == 1
        out = capsys.readouterr()
        assert out.out == ""

    def test_grep_mode_lists_matches(self, projects_dir, capsys):
        envelope = _envelope("task-14", "Review PR #99", "completed", "APPROVE")
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [_queue_op_record(envelope)])
        rc = fr.main(["--grep", "PR #99", "--session", "sess-1"])
        assert rc == 0
        out = capsys.readouterr()
        assert "task-14" in out.out

    def test_grep_mode_no_matches_exits_nonzero(self, projects_dir, capsys):
        _write_session(projects_dir, "-Users-x-repo", "sess-1", [])
        rc = fr.main(["--grep", "nothing-here", "--session", "sess-1"])
        assert rc == 1

    def test_unknown_session_exits_nonzero(self, projects_dir, capsys):
        rc = fr.main(["--task", "x", "--session", "no-such-session-id"])
        assert rc == 1
        out = capsys.readouterr()
        assert "no-such-session-id" in out.err

    def test_task_and_grep_are_mutually_exclusive(self, projects_dir):
        with pytest.raises(SystemExit):
            fr.main(["--task", "x", "--grep", "y", "--session", "sess-1"])
