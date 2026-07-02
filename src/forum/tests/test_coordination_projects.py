"""Tests for coordination.projects — the relocated baton write-fns (flip, claim, release, set_status, close, reopen, rename, anchor, init, merge).

Exercises the full write body against a real FileStore + SeqAllocator: read →
transform → atomic-write under the allocator lock (fork-4), seq embedded + returned.
"""

import pytest

from forum.coordination import (
    FileStore,
    NotAParticipant,
    ProjectAlreadyExists,
    ProjectNotFound,
    SeqAllocator,
    add_participant,
    anchor,
    claim,
    close,
    flip,
    init,
    merge,
    release,
    rename,
    reopen,
    set_status,
)
from forum.coordination import markdown as md


def _baton(turn="ariadne"):
    return (
        "---\n"
        "project_id: PR-1\n"
        "title: a title\n"
        "status: in-progress\n"
        f"turn: {turn}\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "start"\n'
        "participants: [ariadne, borges]\n"
        "---\n"
        "# PR-1\n\nsome body\n"
    )


@pytest.fixture
def store(tmp_path):
    s = FileStore(tmp_path)
    # seed an initial baton (seq 0 — pre-flip)
    s.write_project("PR-1", _baton(turn="ariadne"), seq=0)
    return s


@pytest.fixture
def allocator(store):
    # recover seeds from the seeded baton (seq 0) → first allocate yields 1
    return SeqAllocator(recover=store.recover_max_seq)


def test_flip_updates_turn_and_appends_log(store, allocator):
    seq = flip(store, allocator, "PR-1", to_agent="borges", reason="please review",
               ts="2026-06-26T12:00:00Z")
    assert seq == 1  # first allocation above the seeded seq 0

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["turn"] == "borges"
    assert fields["turn_since"] == "2026-06-26T12:00:00Z"
    assert fields["turn_reason"] == '"please review"'
    # the from→to turn-log line (from = the prior holder, ariadne)
    assert "- 2026-06-26T12:00:00Z ariadne → borges: please review" in raw
    # seq embedded by the store
    assert fields["seq"] == "1"


def test_flip_returns_monotonic_seq(store, allocator):
    s1 = flip(store, allocator, "PR-1", to_agent="borges", reason="one")
    s2 = flip(store, allocator, "PR-1", to_agent="ariadne", reason="two")
    assert s2 > s1
    # the second flip's `from` is borges (the holder the first flip set)
    raw = store.read_project("PR-1")
    assert "borges → ariadne: two" in raw


def test_flip_normalizes_to_agent_case(store, allocator):
    flip(store, allocator, "PR-1", to_agent="  BORGES ", reason="r")
    fields, _ = md.parse_frontmatter(store.read_project("PR-1"))
    assert fields["turn"] == "borges"


def test_flip_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        flip(store, allocator, "PR-nope", to_agent="borges", reason="r")


def test_flip_seq_recoverable_after_restart(tmp_path):
    # the embedded seq is durable: a fresh allocator recovers above it.
    s = FileStore(tmp_path)
    s.write_project("PR-1", _baton(), seq=0)
    a1 = SeqAllocator(recover=s.recover_max_seq)
    flip(s, a1, "PR-1", to_agent="borges", reason="r")  # seq 1

    # "restart": new store + allocator on the same root
    s2 = FileStore(tmp_path)
    a2 = SeqAllocator(recover=s2.recover_max_seq)
    seq2 = flip(s2, a2, "PR-1", to_agent="ariadne", reason="r2")
    assert seq2 == 2  # recovered above the persisted seq 1


def test_flip_does_not_double_append_turn_log_section(store, allocator):
    # two flips → still exactly one "## Turn log" header, two entries.
    flip(store, allocator, "PR-1", to_agent="borges", reason="one")
    flip(store, allocator, "PR-1", to_agent="ariadne", reason="two")
    raw = store.read_project("PR-1")
    assert raw.count(md.TURN_LOG_HEADER) == 1
    assert "one" in raw and "two" in raw


# ---------------------------------------------------------------------------
# claim() tests
# ---------------------------------------------------------------------------


def test_claim_updates_turn_and_appends_log(store, allocator):
    seq = claim(
        store, allocator, "PR-1",
        claimer="luria", pool_sentinel="lei",
        ts="2026-06-26T12:00:00Z",
    )
    assert seq == 1  # first allocation above the seeded seq 0

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["turn"] == "luria"
    assert fields["turn_since"] == "2026-06-26T12:00:00Z"
    assert fields["turn_reason"] == '"claimed"'
    assert "- 2026-06-26T12:00:00Z lei → luria: claimed" in raw
    assert fields["seq"] == "1"


def test_claim_byte_exact_golden(store, allocator):
    """Byte-exact: turn_reason is the literal string '"claimed"' in the frontmatter,
    and the log line uses the right-arrow character → (not ASCII ->)."""
    claim(
        store, allocator, "PR-1",
        claimer="ariadne", pool_sentinel="lei",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    # frontmatter field appears verbatim
    assert 'turn_reason: "claimed"' in raw
    # log line with unicode arrow
    assert "- 2026-06-26T12:00:00Z lei → ariadne: claimed" in raw


def test_claim_normalizes_claimer_case(store, allocator):
    claim(
        store, allocator, "PR-1",
        claimer="  LURIA  ", pool_sentinel="lei",
    )
    fields, _ = md.parse_frontmatter(store.read_project("PR-1"))
    assert fields["turn"] == "luria"


def test_claim_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        claim(store, allocator, "PR-nope", claimer="luria", pool_sentinel="lei")


# ---------------------------------------------------------------------------
# release() tests
# ---------------------------------------------------------------------------


def test_release_updates_turn_and_appends_log(store, allocator):
    seq = release(
        store, allocator, "PR-1",
        holder="ariadne", pool_sentinel="lei",
        reason="all done",
        ts="2026-06-26T12:00:00Z",
    )
    assert seq == 1  # first allocation above the seeded seq 0

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["turn"] == "lei"
    assert fields["turn_since"] == "2026-06-26T12:00:00Z"
    assert fields["turn_reason"] == '"all done"'
    assert "- 2026-06-26T12:00:00Z ariadne → lei: all done" in raw
    assert fields["seq"] == "1"


def test_release_with_done_marks_title_and_logs(store, allocator):
    release(
        store, allocator, "PR-1",
        holder="ariadne", pool_sentinel="lei",
        reason="finished", done=True,
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["title"] == "a title (done)"
    assert "- 2026-06-26T12:00:00Z ariadne → lei: finished" in raw
    assert "- 2026-06-26T12:00:00Z title marked (done)" in raw


def test_release_done_flag_idempotent_if_already_done(tmp_path):
    # seed a baton whose title already ends with "(done)"
    already_done = (
        "---\n"
        "project_id: PR-2\n"
        "title: a title (done)\n"
        "status: in-progress\n"
        "turn: ariadne\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "start"\n'
        "participants: [ariadne, borges]\n"
        "---\n"
        "# PR-2\n\nsome body\n"
    )
    s = FileStore(tmp_path)
    s.write_project("PR-2", already_done, seq=0)
    a = SeqAllocator(recover=s.recover_max_seq)

    release(
        s, a, "PR-2",
        holder="ariadne", pool_sentinel="lei",
        reason="re-release", done=True,
        ts="2026-06-26T12:00:00Z",
    )
    raw = s.read_project("PR-2")
    fields, _ = md.parse_frontmatter(raw)
    # title must NOT gain a second "(done)" suffix
    assert fields["title"] == "a title (done)"
    # the "title marked (done)" extra log line must NOT appear
    assert "title marked (done)" not in raw


def test_release_byte_exact_golden(store, allocator):
    """Byte-exact: turn_reason is the verbatim reason in double-quotes,
    and the log line uses the unicode right-arrow →."""
    release(
        store, allocator, "PR-1",
        holder="ariadne", pool_sentinel="lei",
        reason="scope complete",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    assert 'turn_reason: "scope complete"' in raw
    assert "- 2026-06-26T12:00:00Z ariadne → lei: scope complete" in raw


def test_release_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        release(
            store, allocator, "PR-nope",
            holder="ariadne", pool_sentinel="lei",
            reason="done",
        )


# ---------------------------------------------------------------------------
# set_status() tests
# ---------------------------------------------------------------------------


def test_set_status_updates_status_and_appends_log(store, allocator):
    seq = set_status(
        store, allocator, "PR-1",
        new_status="in-review",
        reason="submitted for review",
        caller="close",
        ts="2026-06-26T12:00:00Z",
    )
    assert seq == 1

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["status"] == "in-review"
    assert fields["seq"] == "1"
    assert "- 2026-06-26T12:00:00Z close: in-review — submitted for review" in raw


def test_set_status_byte_exact_golden(store, allocator):
    """Byte-exact: log line uses caller label, em-dash separator, and reason verbatim."""
    set_status(
        store, allocator, "PR-1",
        new_status="merged",
        reason="CI green, colleague approved",
        caller="close",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    assert "status: merged" in raw
    assert "- 2026-06-26T12:00:00Z close: merged — CI green, colleague approved" in raw


def test_set_status_default_caller(store, allocator):
    """When caller is omitted, the log entry uses 'set_status' as the verb."""
    set_status(
        store, allocator, "PR-1",
        new_status="planning",
        reason="reset",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    assert "- 2026-06-26T12:00:00Z set_status: planning — reset" in raw


def test_set_status_does_not_touch_turn_fields(store, allocator):
    """set_status only mutates the status field; turn / turn_since / turn_reason are unchanged."""
    set_status(
        store, allocator, "PR-1",
        new_status="merged",
        reason="done",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    # turn fields from the seed baton must survive unchanged
    assert fields["turn"] == "ariadne"
    assert fields["turn_since"] == "2026-06-26T00:00:00Z"
    assert fields["turn_reason"] == '"start"'


def test_set_status_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        set_status(
            store, allocator, "PR-nope",
            new_status="merged",
            reason="done",
        )


# ---------------------------------------------------------------------------
# close() tests
# ---------------------------------------------------------------------------


def test_close_updates_status_and_appends_log(store, allocator):
    seq = close(
        store, allocator, "PR-1",
        new_status="merged",
        reason="scope complete",
        ts="2026-06-26T12:00:00Z",
    )
    assert seq == 1  # first allocation above the seeded seq 0

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["status"] == "merged"
    assert fields["seq"] == "1"
    assert "close: merged — " in raw


def test_close_byte_exact_golden(store, allocator):
    """Byte-exact: log line format is '- {ts} close: {status} — {reason}'."""
    close(
        store, allocator, "PR-1",
        new_status="merged",
        reason="scope complete",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    assert "- 2026-06-26T12:00:00Z close: merged — scope complete" in raw


def test_close_does_not_touch_turn_fields(store, allocator):
    """close() only mutates the status field; turn / turn_since / turn_reason are unchanged."""
    close(
        store, allocator, "PR-1",
        new_status="merged",
        reason="done",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["turn"] == "ariadne"
    assert fields["turn_since"] == "2026-06-26T00:00:00Z"
    assert fields["turn_reason"] == '"start"'


def test_close_returns_monotonic_seq(store, allocator):
    seq1 = close(store, allocator, "PR-1", new_status="abandoned", reason="first")
    seq2 = close(store, allocator, "PR-1", new_status="merged", reason="second")
    assert seq2 > seq1


def test_close_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        close(store, allocator, "PR-nope", new_status="merged", reason="done")


# ---------------------------------------------------------------------------
# reopen() tests
# ---------------------------------------------------------------------------


def _closed_baton():
    return (
        "---\n"
        "project_id: PR-2\n"
        "title: a title\n"
        "status: merged\n"
        "turn: ariadne\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "start"\n'
        "participants: [ariadne, borges]\n"
        "---\n"
        "# PR-2\n\nsome body\n"
    )


@pytest.fixture
def closed_store(tmp_path):
    s = FileStore(tmp_path)
    s.write_project("PR-2", _closed_baton(), seq=0)
    return s


@pytest.fixture
def closed_allocator(closed_store):
    return SeqAllocator(recover=closed_store.recover_max_seq)


def test_reopen_updates_status_and_appends_log(closed_store, closed_allocator):
    seq = reopen(
        closed_store, closed_allocator, "PR-2",
        invoker="luria",
        new_status="in-progress",
        ts="2026-06-26T12:00:00Z",
    )
    assert seq == 1  # first allocation above the seeded seq 0

    raw = closed_store.read_project("PR-2")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["status"] == "in-progress"
    assert fields["seq"] == "1"
    assert "reopened →" in raw


def test_reopen_byte_exact_golden(closed_store, closed_allocator):
    """Byte-exact: log line format is '- {ts} reopened → {invoker}: status was {old} → {new}'."""
    reopen(
        closed_store, closed_allocator, "PR-2",
        invoker="luria",
        new_status="in-progress",
        ts="2026-06-26T12:00:00Z",
    )
    raw = closed_store.read_project("PR-2")
    assert "- 2026-06-26T12:00:00Z reopened → luria: status was merged → in-progress" in raw


def test_reopen_reads_current_status_for_log(closed_store, closed_allocator):
    """The 'status was X' fragment must reflect the pre-reopen status read from the file, not a caller arg."""
    reopen(
        closed_store, closed_allocator, "PR-2",
        invoker="luria",
        new_status="in-progress",
        ts="2026-06-26T12:00:00Z",
    )
    raw = closed_store.read_project("PR-2")
    assert "status was merged" in raw


def test_reopen_does_not_touch_turn_fields(closed_store, closed_allocator):
    """reopen() only mutates the status field; turn / turn_since / turn_reason are unchanged."""
    reopen(
        closed_store, closed_allocator, "PR-2",
        invoker="luria",
        new_status="in-progress",
        ts="2026-06-26T12:00:00Z",
    )
    raw = closed_store.read_project("PR-2")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["turn"] == "ariadne"
    assert fields["turn_since"] == "2026-06-26T00:00:00Z"
    assert fields["turn_reason"] == '"start"'


def test_reopen_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        reopen(store, allocator, "PR-nope", invoker="luria", new_status="in-progress")


# ---------------------------------------------------------------------------
# rename() tests
# ---------------------------------------------------------------------------


def test_rename_updates_title_and_appends_log(store, allocator):
    seq = rename(store, allocator, "PR-1", new_title="New Title", ts="2026-06-26T12:00:00Z")
    assert seq == 1
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["title"] == "New Title"
    assert fields["seq"] == "1"
    assert 'renamed: title from "a title" to "New Title"' in raw


def test_rename_byte_exact_golden(store, allocator):
    rename(store, allocator, "PR-1", new_title="New Title", ts="2026-06-26T12:00:00Z")
    raw = store.read_project("PR-1")
    assert '- 2026-06-26T12:00:00Z renamed: title from "a title" to "New Title"' in raw


def test_rename_does_not_touch_turn_fields(store, allocator):
    rename(store, allocator, "PR-1", new_title="New Title", ts="2026-06-26T12:00:00Z")
    fields, _ = md.parse_frontmatter(store.read_project("PR-1"))
    assert fields["turn"] == "ariadne"
    assert fields["turn_since"] == "2026-06-26T00:00:00Z"
    assert fields["turn_reason"] == '"start"'


def test_rename_returns_monotonic_seq(store, allocator):
    s1 = rename(store, allocator, "PR-1", new_title="Title A")
    s2 = rename(store, allocator, "PR-1", new_title="Title B")
    assert s2 > s1


def test_rename_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        rename(store, allocator, "PR-nope", new_title="X")


# ---------------------------------------------------------------------------
# anchor() tests
# ---------------------------------------------------------------------------


def _baton_with_github(turn="ariadne"):
    return (
        "---\n"
        "project_id: PR-3\n"
        "title: a title\n"
        "status: in-progress\n"
        f"turn: {turn}\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "start"\n'
        "participants: [ariadne, borges]\n"
        "github: pr/490\n"
        "---\n"
        "# PR-3\n\nsome body\n"
    )


@pytest.fixture
def anchored_store(tmp_path):
    s = FileStore(tmp_path)
    s.write_project("PR-3", _baton_with_github(), seq=0)
    return s


@pytest.fixture
def anchored_allocator(anchored_store):
    return SeqAllocator(recover=anchored_store.recover_max_seq)


def test_anchor_set_new_anchor(store, allocator):
    """anchor() with no prior github: field creates the field and logs 'anchor set'."""
    seq = anchor(store, allocator, "PR-1", github_anchor="pr/490", ts="2026-06-26T12:00:00Z")
    assert seq == 1
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["github"] == "pr/490"
    assert "anchor set: github → pr/490" in raw


def test_anchor_set_byte_exact_golden(store, allocator):
    anchor(store, allocator, "PR-1", github_anchor="pr/490", ts="2026-06-26T12:00:00Z")
    raw = store.read_project("PR-1")
    assert "- 2026-06-26T12:00:00Z anchor set: github → pr/490" in raw


def test_anchor_update_existing_anchor(anchored_store, anchored_allocator):
    """anchor() with an existing github: field updates it and logs 'anchor updated from X to Y'."""
    anchor(anchored_store, anchored_allocator, "PR-3", github_anchor="pr/999", ts="2026-06-26T12:00:00Z")
    raw = anchored_store.read_project("PR-3")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["github"] == "pr/999"
    assert 'anchor updated: github from "pr/490" to "pr/999"' in raw


def test_anchor_update_byte_exact_golden(anchored_store, anchored_allocator):
    anchor(anchored_store, anchored_allocator, "PR-3", github_anchor="pr/999", ts="2026-06-26T12:00:00Z")
    raw = anchored_store.read_project("PR-3")
    assert '- 2026-06-26T12:00:00Z anchor updated: github from "pr/490" to "pr/999"' in raw


def test_anchor_does_not_touch_turn_fields(store, allocator):
    anchor(store, allocator, "PR-1", github_anchor="pr/490", ts="2026-06-26T12:00:00Z")
    fields, _ = md.parse_frontmatter(store.read_project("PR-1"))
    assert fields["turn"] == "ariadne"
    assert fields["turn_since"] == "2026-06-26T00:00:00Z"
    assert fields["turn_reason"] == '"start"'


def test_anchor_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        anchor(store, allocator, "PR-nope", github_anchor="pr/490")


# ---------------------------------------------------------------------------
# add_participant() tests
# ---------------------------------------------------------------------------
# Seed baton (_baton()) participants: [ariadne, borges].


def test_add_participant_appends_new_participant(store, allocator):
    seq, added = add_participant(
        store, allocator, "PR-1",
        agent="ariadne", participant="luria",
        ts="2026-06-26T12:00:00Z",
    )
    assert added is True
    assert seq == 1  # first allocation above the seeded seq 0

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    participants = _participants_list(fields["participants"])
    assert participants == ["ariadne", "borges", "luria"]
    assert "- 2026-06-26T12:00:00Z ariadne added participant: luria" in raw
    assert fields["seq"] == "1"


def test_add_participant_byte_exact_golden(store, allocator):
    add_participant(
        store, allocator, "PR-1",
        agent="ariadne", participant="luria",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    assert "- 2026-06-26T12:00:00Z ariadne added participant: luria" in raw


def test_add_participant_normalizes_case(store, allocator):
    add_participant(store, allocator, "PR-1", agent="ARIADNE", participant="  LURIA ")
    fields, _ = md.parse_frontmatter(store.read_project("PR-1"))
    assert "luria" in _participants_list(fields["participants"])


def test_add_participant_idempotent_no_op_on_already_present(store, allocator):
    """Adding an already-present participant is a no-op: no dup, no error, no new seq/log line."""
    seq, added = add_participant(store, allocator, "PR-1", agent="ariadne", participant="borges")
    assert added is False
    assert seq == 0  # no write happened; the seeded baton's last-committed seq

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    # participants list unchanged — no duplicate "borges" entry
    assert _participants_list(fields["participants"]) == ["ariadne", "borges"]
    assert "added participant" not in raw


def test_add_participant_idempotent_case_insensitive(store, allocator):
    """Dedup compares case-insensitively even though the stored list is already lowercase."""
    seq, added = add_participant(store, allocator, "PR-1", agent="ariadne", participant="BORGES")
    assert added is False


def test_add_participant_does_not_duplicate_across_two_calls(store, allocator):
    add_participant(store, allocator, "PR-1", agent="ariadne", participant="luria")
    seq2, added2 = add_participant(store, allocator, "PR-1", agent="ariadne", participant="luria")
    assert added2 is False
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    participants = _participants_list(fields["participants"])
    assert participants.count("luria") == 1


def test_add_participant_agent_not_a_participant_raises(store, allocator):
    """A non-participant cannot add others — the load-bearing server-side auth guard."""
    with pytest.raises(NotAParticipant):
        add_participant(store, allocator, "PR-1", agent="casey", participant="luria")

    # no mutation occurred
    raw = store.read_project("PR-1")
    assert "added participant" not in raw


def test_add_participant_does_not_touch_turn_fields(store, allocator):
    add_participant(
        store, allocator, "PR-1",
        agent="ariadne", participant="luria",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["turn"] == "ariadne"
    assert fields["turn_since"] == "2026-06-26T00:00:00Z"
    assert fields["turn_reason"] == '"start"'


def test_add_participant_returns_monotonic_seq(store, allocator):
    seq1, _ = add_participant(store, allocator, "PR-1", agent="ariadne", participant="luria")
    seq2, _ = add_participant(store, allocator, "PR-1", agent="ariadne", participant="lei")
    assert seq2 > seq1


def test_add_participant_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        add_participant(store, allocator, "PR-nope", agent="ariadne", participant="luria")


def _participants_list(participants_field: str) -> list:
    """Parse the frontmatter participants field ('[a, b]') into a list, for assertions."""
    inner = participants_field.strip().lstrip("[").rstrip("]")
    return [p.strip() for p in inner.split(",") if p.strip()]


# ---------------------------------------------------------------------------
# init() tests
# ---------------------------------------------------------------------------


def test_init_creates_project_with_correct_content(tmp_path):
    """init() creates the baton file with correct frontmatter and turn log."""
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    seq = init(
        s, alloc, "PR-42",
        title="Test Baton",
        status="planning",
        turn="borges",
        participants=["borges", "ariadne"],
        turn_reason="project initialized by luria",
        ts="2026-06-26T12:00:00Z",
    )
    assert seq == 1
    raw = s.read_project("PR-42")
    assert raw is not None
    fields, body = md.parse_frontmatter(raw)
    assert fields["title"] == "Test Baton"
    assert fields["status"] == "planning"
    assert fields["turn"] == "borges"
    assert fields["turn_since"] == "2026-06-26T12:00:00Z"
    assert "initialized → borges" in raw


def test_init_byte_exact_turn_log(tmp_path):
    """Byte-exact: turn log line matches baton.cmd_init format."""
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    init(
        s, alloc, "PR-42",
        title="Test Baton",
        status="in-progress",
        turn="borges",
        participants=["borges", "ariadne"],
        turn_reason="project initialized by luria",
        ts="2026-06-26T12:00:00Z",
    )
    raw = s.read_project("PR-42")
    assert "- 2026-06-26T12:00:00Z initialized → borges: project initialized by luria" in raw


def test_init_with_github_anchor(tmp_path):
    """init() with github= includes the github: field in frontmatter."""
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    init(
        s, alloc, "PR-42",
        title="T",
        status="in-progress",
        turn="borges",
        participants=["borges"],
        turn_reason="init",
        github="pr/42",
        ts="2026-06-26T12:00:00Z",
    )
    raw = s.read_project("PR-42")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["github"] == "pr/42"


def test_init_without_github_anchor(tmp_path):
    """init() without github= omits the github: field."""
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    init(
        s, alloc, "PR-42",
        title="T",
        status="in-progress",
        turn="borges",
        participants=["borges"],
        turn_reason="init",
        ts="2026-06-26T12:00:00Z",
    )
    raw = s.read_project("PR-42")
    fields, _ = md.parse_frontmatter(raw)
    assert "github" not in fields


def test_init_raises_if_project_already_exists(store, allocator):
    """init() raises ProjectAlreadyExists if the project_id already has a baton file."""
    with pytest.raises(ProjectAlreadyExists):
        init(
            store, allocator, "PR-1",
            title="T",
            status="in-progress",
            turn="ariadne",
            participants=["ariadne"],
            turn_reason="init",
        )


def test_init_returns_seq_1_for_fresh_store(tmp_path):
    """init() into an empty store returns seq 1 (allocator starts at 0)."""
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    seq = init(
        s, alloc, "PR-42",
        title="T",
        status="planning",
        turn="luria",
        participants=["luria", "borges"],
        turn_reason="init",
    )
    assert seq == 1


# ---------------------------------------------------------------------------
# merge() tests
# ---------------------------------------------------------------------------


def test_merge_sets_status_and_log(store, allocator):
    seq = merge(store, allocator, "PR-1", merged_by="luria",
                ts="2026-06-27T14:00:00Z")
    assert seq == 1
    raw = store.read_project("PR-1")
    # After merge, the file is archived — read_project returns None
    assert raw is None
    # Read from archive directly
    archive_raw = (store.root / "archive" / "PR-1.md").read_text()
    fields, _ = md.parse_frontmatter(archive_raw)
    assert fields["status"] == "merged"
    assert "- 2026-06-27T14:00:00Z merged via baton merge by luria" in archive_raw


def test_merge_forced_log(store, allocator):
    seq = merge(store, allocator, "PR-1", merged_by="luria", forced=True,
                ts="2026-06-27T14:00:00Z")
    archive_raw = (store.root / "archive" / "PR-1.md").read_text()
    assert "(FORCED past gates 3-4)" in archive_raw


def test_merge_archives_to_archive_dir(store, allocator):
    merge(store, allocator, "PR-1", merged_by="luria")
    # file no longer in projects/
    assert not (store.projects_dir / "PR-1.md").exists()
    # file now in archive/
    assert (store.root / "archive" / "PR-1.md").exists()


def test_merge_not_found(store, allocator):
    with pytest.raises(ProjectNotFound):
        merge(store, allocator, "MISSING", merged_by="luria")


def test_archive_project_idempotent(store):
    # archive a project that exists
    store.archive_project("PR-1")
    assert not (store.projects_dir / "PR-1.md").exists()
    assert (store.root / "archive" / "PR-1.md").exists()
    # archive again (file already in archive) — must not raise
    store.archive_project("PR-1")


def test_archive_project_no_clobber_on_pid_reuse(store, tmp_path):
    """Archiving a new pid that matches an existing archive entry must not clobber (#1512)."""
    # Archive PR-1 once
    store.archive_project("PR-1")
    archive_path = store.root / "archive" / "PR-1.md"
    assert archive_path.exists()
    original_content = archive_path.read_text()

    # Re-create PR-1 in the active dir (simulates pid reuse after earlier archive)
    new_content = "---\nproject: PR-1\nturn: luria\n---\nnew baton content\n"
    (store.projects_dir / "PR-1.md").write_text(new_content)

    # Archive the new PR-1 — must NOT overwrite the existing archive entry
    store.archive_project("PR-1")

    # Original archive still intact
    assert archive_path.read_text() == original_content, "original archive was clobbered"
    # New baton landed in a suffixed file
    suffixed = store.root / "archive" / "PR-1.1.md"
    assert suffixed.exists(), "new baton missing from archive (expected PR-1.1.md)"
    assert "new baton content" in suffixed.read_text()


def test_archive_project_seq_suffix_increments(store):
    """Second clobber produces .2.md, not another .1.md (#1512)."""
    store.archive_project("PR-1")
    (store.projects_dir / "PR-1.md").write_text("second\n")
    store.archive_project("PR-1")
    (store.projects_dir / "PR-1.md").write_text("third\n")
    store.archive_project("PR-1")
    assert (store.root / "archive" / "PR-1.md").exists()
    assert (store.root / "archive" / "PR-1.1.md").exists()
    assert (store.root / "archive" / "PR-1.2.md").exists()


def test_recover_max_seq_counts_archived(store, allocator):
    # merge archives the file; seq must survive in recover_max_seq
    seq = merge(store, allocator, "PR-1", merged_by="luria")
    hi = store.recover_max_seq()
    assert hi >= seq


def test_read_projects_github_field_parsed(tmp_path):
    """read_projects populates ProjectRecord.github from the frontmatter github: field."""
    s = FileStore(tmp_path)
    # Baton with explicit project-anchor
    baton_with_github = (
        "---\n"
        "project_id: pool-ux\n"
        "title: UX work pool\n"
        "status: in-progress\n"
        "turn: luria\n"
        "turn_since: 2026-06-29T00:00:00Z\n"
        "participants: [luria, sol]\n"
        "github: project/7\n"
        "---\n"
        "# pool-ux\n\nbody\n"
    )
    s.write_project("pool-ux", baton_with_github, seq=1)
    records = s.read_projects()
    assert len(records) == 1
    assert records[0].github == "project/7"


def test_read_projects_github_field_absent(tmp_path):
    """read_projects defaults ProjectRecord.github to empty string when field is absent."""
    s = FileStore(tmp_path)
    s.write_project("PR-1", _baton(turn="ariadne"), seq=1)
    records = s.read_projects()
    assert len(records) == 1
    assert records[0].github == ""
