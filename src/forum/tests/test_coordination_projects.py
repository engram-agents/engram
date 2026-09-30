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
    set_colleague,
    set_status,
)
from forum.coordination import markdown as md
from forum.coordination import projects as _projects_mod


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


def _baton_with_phase(turn="ariadne", phase=None):
    """Same shape as ``_baton()``, with an optional explicit ``phase:``
    frontmatter field (#3039 U2a) -- used to exercise the "already-explicit,
    don't clobber" half of ``_maybe_stamp_phase``'s contract."""
    phase_line = f"phase: {phase}\n" if phase is not None else ""
    return (
        "---\n"
        "project_id: PR-1\n"
        "title: a title\n"
        "status: in-progress\n"
        f"turn: {turn}\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "start"\n'
        "participants: [ariadne, borges]\n"
        f"{phase_line}"
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


def test_flip_persists_turn_by_round_trip(store, allocator):
    # #1812: flip(by_agent=...) persists turn_by; re-reading the file (a fresh
    # parse via read_projects, not the in-memory value) recovers it lowercased.
    flip(store, allocator, "PR-1", to_agent="borges", reason="please review",
         by_agent="ARIADNE", ts="2026-06-26T12:00:00Z")
    rec = next(r for r in store.read_projects() if r.project_id == "PR-1")
    assert rec.turn_by == "ariadne"


def test_flip_by_agent_defaults_to_empty_when_omitted(store, allocator):
    # Back-compat: existing callers that don't pass by_agent must not crash,
    # and must persist the documented safe default ("" == actor unknown).
    flip(store, allocator, "PR-1", to_agent="borges", reason="r")
    rec = next(r for r in store.read_projects() if r.project_id == "PR-1")
    assert rec.turn_by == ""


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


def test_claim_sets_turn_by_to_claimer(store, allocator):
    # #1812: claim's actor IS the claimer — turn_by mirrors turn.
    claim(store, allocator, "PR-1", claimer="luria", pool_sentinel="lei")
    rec = next(r for r in store.read_projects() if r.project_id == "PR-1")
    assert rec.turn_by == "luria"
    assert rec.turn_by == rec.turn


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


def test_release_sets_turn_by_to_holder(store, allocator):
    # #1812: release's actor IS the releasing holder — turn_by is stamped even
    # though turn itself becomes the pool sentinel (audit-trail consistency).
    release(store, allocator, "PR-1", holder="ariadne", pool_sentinel="lei", reason="done")
    rec = next(r for r in store.read_projects() if r.project_id == "PR-1")
    assert rec.turn_by == "ariadne"
    assert rec.turn == "lei"


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


# ---------------------------------------------------------------------------
# set_colleague() tests (#2764)
# ---------------------------------------------------------------------------
# Seed baton (_baton()) turn=ariadne, participants=[ariadne, borges].


def test_set_colleague_first_set_persists_field_and_adds_participant(store, allocator):
    seq, participant_added = set_colleague(
        store, allocator, "PR-1",
        agent="ariadne", colleague="luria",
        ts="2026-06-26T12:00:00Z",
    )
    assert seq == 1
    assert participant_added is True

    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["colleague"] == "luria"
    assert _participants_list(fields["participants"]) == ["ariadne", "borges", "luria"]
    assert "- 2026-06-26T12:00:00Z ariadne set colleague → luria" in raw


def test_set_colleague_byte_exact_golden(store, allocator):
    set_colleague(
        store, allocator, "PR-1",
        agent="ariadne", colleague="luria",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    assert "- 2026-06-26T12:00:00Z ariadne set colleague → luria" in raw


def test_set_colleague_already_participant_does_not_duplicate(store, allocator):
    """colleague already in participants -> field is still set, but no dup entry."""
    seq, participant_added = set_colleague(
        store, allocator, "PR-1",
        agent="ariadne", colleague="borges",  # already a seeded participant
        ts="2026-06-26T12:00:00Z",
    )
    assert participant_added is False
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["colleague"] == "borges"
    assert _participants_list(fields["participants"]) == ["ariadne", "borges"]


def test_set_colleague_update_existing_value_logs_change(store, allocator):
    set_colleague(store, allocator, "PR-1", agent="ariadne", colleague="luria", ts="2026-06-26T12:00:00Z")
    set_colleague(store, allocator, "PR-1", agent="ariadne", colleague="borges", ts="2026-06-26T13:00:00Z")
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["colleague"] == "borges"
    assert '- 2026-06-26T13:00:00Z ariadne changed colleague from "luria" to "borges"' in raw


def test_set_colleague_normalizes_case(store, allocator):
    set_colleague(store, allocator, "PR-1", agent="ARIADNE", colleague="  LURIA ")
    fields, _ = md.parse_frontmatter(store.read_project("PR-1"))
    assert fields["colleague"] == "luria"
    assert "luria" in _participants_list(fields["participants"])


def test_set_colleague_does_not_touch_turn_fields(store, allocator):
    set_colleague(
        store, allocator, "PR-1",
        agent="ariadne", colleague="luria",
        ts="2026-06-26T12:00:00Z",
    )
    raw = store.read_project("PR-1")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["turn"] == "ariadne"
    assert fields["turn_since"] == "2026-06-26T00:00:00Z"
    assert fields["turn_reason"] == '"start"'


def test_set_colleague_returns_monotonic_seq(store, allocator):
    seq1, _ = set_colleague(store, allocator, "PR-1", agent="ariadne", colleague="luria")
    seq2, _ = set_colleague(store, allocator, "PR-1", agent="ariadne", colleague="borges")
    assert seq2 > seq1


def test_set_colleague_missing_project_raises(store, allocator):
    with pytest.raises(ProjectNotFound):
        set_colleague(store, allocator, "PR-nope", agent="ariadne", colleague="luria")


def test_set_colleague_by_existing_participant_succeeds(store, allocator):
    """agent = a current participant who is NOT the turn holder -- allowed."""
    seq, _ = set_colleague(store, allocator, "PR-1", agent="borges", colleague="luria")
    assert seq == 1


def test_set_colleague_agent_neither_participant_nor_holder_raises(store, allocator):
    """The load-bearing server-side auth guard -- a bystander cannot set colleague."""
    with pytest.raises(NotAParticipant):
        set_colleague(store, allocator, "PR-1", agent="casey", colleague="luria")
    raw = store.read_project("PR-1")
    assert "set colleague" not in raw


def test_set_colleague_by_turn_holder_not_in_participants_succeeds(store, allocator):
    """The OR-holder half: a PR baton legitimately flipped to the pool sentinel
    (not itself listed in participants -- flip's own to_agent validation
    bypasses the participants check for the sentinel) must still let that
    holder set the colleague -- exactly the "maintainer confirms the
    colleague before merge" case #2764 exists for."""
    raw = _baton(turn="kepler")  # kepler is the turn holder but NOT in [ariadne, borges]
    store.write_project("PR-1", raw, seq=0)
    seq, participant_added = set_colleague(
        store, allocator, "PR-1", agent="kepler", colleague="luria",
    )
    assert seq == 1
    assert participant_added is True
    fields, _ = md.parse_frontmatter(store.read_project("PR-1"))
    assert fields["colleague"] == "luria"
    # kepler (the holder) is still not a listed participant -- set_colleague
    # only adds the COLLEAGUE being named, never the caller.
    assert "kepler" not in _participants_list(fields["participants"])


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


def test_init_with_colleague_persists_field(tmp_path):
    """init() with colleague= includes the colleague: field in frontmatter
    (#2764 -- pre-fix, --colleague at init only touched participants and a
    client-side print; nothing was ever persisted, so baton show could never
    render it)."""
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    init(
        s, alloc, "PR-42",
        title="T",
        status="in-progress",
        turn="borges",
        participants=["borges", "ariadne"],
        turn_reason="init",
        colleague="ariadne",
        ts="2026-06-26T12:00:00Z",
    )
    raw = s.read_project("PR-42")
    fields, _ = md.parse_frontmatter(raw)
    assert fields["colleague"] == "ariadne"


def test_init_without_colleague_omits_field(tmp_path):
    """init() without colleague= omits the colleague: field entirely."""
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
    assert "colleague" not in fields


def test_init_sets_turn_by_to_initor(tmp_path):
    # #1812: by_agent is the INITOR, not necessarily the same as turn — one
    # agent may init a baton with the turn assigned to someone else.
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    init(
        s, alloc, "PR-42",
        title="T", status="in-progress", turn="borges",
        participants=["borges", "ariadne"], turn_reason="init",
        by_agent="ARIADNE", ts="2026-06-26T12:00:00Z",
    )
    rec = next(r for r in s.read_projects() if r.project_id == "PR-42")
    assert rec.turn_by == "ariadne"
    assert rec.turn == "borges"


def test_init_by_agent_defaults_to_empty_when_omitted(tmp_path):
    # Back-compat: existing init() callers that don't pass by_agent must not
    # crash, and must persist the documented safe default.
    s = FileStore(tmp_path)
    alloc = SeqAllocator(recover=s.recover_max_seq)
    init(
        s, alloc, "PR-42",
        title="T", status="in-progress", turn="borges",
        participants=["borges"], turn_reason="init",
    )
    rec = next(r for r in s.read_projects() if r.project_id == "PR-42")
    assert rec.turn_by == ""


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


def test_read_projects_turn_by_field_parsed(tmp_path):
    """read_projects populates ProjectRecord.turn_by from the frontmatter turn_by: field."""
    s = FileStore(tmp_path)
    baton_with_turn_by = (
        "---\n"
        "project_id: PR-5\n"
        "title: t\n"
        "status: in-progress\n"
        "turn: ariadne\n"
        "turn_since: 2026-06-29T00:00:00Z\n"
        'turn_reason: "start"\n'
        "turn_by: borges\n"
        "participants: [ariadne, borges]\n"
        "---\n"
        "# PR-5\n\nbody\n"
    )
    s.write_project("PR-5", baton_with_turn_by, seq=1)
    records = s.read_projects()
    assert len(records) == 1
    assert records[0].turn_by == "borges"


def test_read_projects_turn_by_field_absent_legacy_baton(tmp_path):
    """read_projects defaults ProjectRecord.turn_by to "" for a legacy baton with
    no turn_by: frontmatter line — the load-bearing back-compat case (#1812):
    never a crash, never a migration, safe default that still notifies."""
    s = FileStore(tmp_path)
    s.write_project("PR-1", _baton(turn="ariadne"), seq=1)  # no turn_by field
    records = s.read_projects()
    assert len(records) == 1
    assert records[0].turn_by == ""


# ===========================================================================
# _maybe_stamp_phase() + phase= kwarg tests (#3039 U2a -- the WRITE side)
# ===========================================================================
#
# U1 built the READ side only (_resolve_phase, tested in tools/baton.py's
# TestResolvePhase -- there is no projects.py-level read helper). U2a adds
# the write side: every phase-defining transition stamps an explicit
# `phase:` field, and any write to a still-legacy baton (no `phase:` yet)
# also converts it. Contract-first (this repo's CLAUDE.md "Contract-first
# tests"): written against the pre-U2a `dev` code, where every test below
# goes RED -- `_maybe_stamp_phase` does not exist yet, and
# flip/claim/release/init accept no `phase` keyword argument at all.


class TestMaybeStampPhase:
    """`_maybe_stamp_phase(fields, phase, now) -> (updates_dict, log_suffix)`
    -- pure function, no I/O."""

    def test_stamps_when_existing_empty(self):
        updates, suffix = _projects_mod._maybe_stamp_phase(
            {}, "peer", "2026-09-30T12:00:00Z",
        )
        assert updates == {"phase": "peer"}
        assert suffix != ""
        assert "peer" in suffix
        assert "2026-09-30T12:00:00Z" in suffix
        assert "#3039 U2a" in suffix

    def test_stamps_when_phase_field_present_but_blank(self):
        # A "phase:" frontmatter line present but blank/whitespace-only
        # still counts as "no explicit phase yet".
        updates, suffix = _projects_mod._maybe_stamp_phase(
            {"phase": "   "}, "pool", "2026-09-30T12:00:00Z",
        )
        assert updates == {"phase": "pool"}
        assert suffix != ""

    def test_noop_when_existing_non_empty_same_value(self):
        updates, suffix = _projects_mod._maybe_stamp_phase(
            {"phase": "peer"}, "peer", "2026-09-30T12:00:00Z",
        )
        assert updates == {}
        assert suffix == ""

    def test_noop_when_existing_non_empty_disagreeing_value(self):
        # existing "merge" disagrees with the passed "peer" -- still a
        # no-op; a stale client-computed guess must never clobber an
        # already-explicit phase (this write's job is never to resolve
        # that disagreement).
        updates, suffix = _projects_mod._maybe_stamp_phase(
            {"phase": "merge"}, "peer", "2026-09-30T12:00:00Z",
        )
        assert updates == {}
        assert suffix == ""

    def test_noop_when_phase_is_none(self):
        updates, suffix = _projects_mod._maybe_stamp_phase(
            {}, None, "2026-09-30T12:00:00Z",
        )
        assert updates == {}
        assert suffix == ""

    def test_noop_when_phase_is_empty_string(self):
        updates, suffix = _projects_mod._maybe_stamp_phase(
            {}, "", "2026-09-30T12:00:00Z",
        )
        assert updates == {}
        assert suffix == ""


class TestPhaseWrites:
    """flip/claim/release/init, called with a `phase` kwarg (#3039 U2a).

    Test obligation 2: on a baton with NO `phase:` field, the written file
    has `phase: <value>` and the turn log's last line matches the
    conversion-note shape. Test obligation 3: on a baton that ALREADY has
    an explicit (and DIFFERENT) `phase:` field, the written file's
    `phase:` is UNCHANGED, and no conversion note is appended -- only
    whichever ordinary log line that write-body already produces. (Not
    applicable to `init()`: it raises ProjectAlreadyExists on a baton that
    already has a file, so there is no "already-explicit" case to exercise
    for it -- it is exercised only for its own obligation-2 case below.)
    """

    # --- flip() ---

    def test_flip_stamps_phase_on_legacy_baton(self, store, allocator):
        flip(
            store, allocator, "PR-1", to_agent="borges", reason="please review",
            phase="peer", ts="2026-06-26T12:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'peer'" in lines[-1]
        assert "#3039 U2a" in lines[-1]

    def test_flip_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="merge"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        flip(
            s, a, "PR-1", to_agent="borges", reason="please review",
            phase="peer", ts="2026-06-26T12:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # --- claim() ---

    def test_claim_stamps_phase_on_legacy_baton(self, store, allocator):
        claim(
            store, allocator, "PR-1", claimer="luria", pool_sentinel="lei",
            phase="claimed", ts="2026-06-26T12:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "claimed"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'claimed'" in lines[-1]

    def test_claim_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="pool"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        claim(
            s, a, "PR-1", claimer="luria", pool_sentinel="lei",
            phase="claimed", ts="2026-06-26T12:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "pool"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # --- release() ---

    def test_release_stamps_phase_on_legacy_baton(self, store, allocator):
        release(
            store, allocator, "PR-1", holder="ariadne", pool_sentinel="lei",
            reason="all done", phase="pool", ts="2026-06-26T12:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "pool"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'pool'" in lines[-1]

    def test_release_done_stamps_phase_as_the_last_log_line(self, store, allocator):
        # Multi-line log entry (release --done already appends a "title
        # marked (done)" line): the conversion note must be the LAST line,
        # after that one.
        release(
            store, allocator, "PR-1", holder="ariadne", pool_sentinel="lei",
            reason="finished", done=True, phase="done", ts="2026-06-26T12:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "done"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "title marked (done)" in lines[-2]
        assert "phase stamped: 'done'" in lines[-1]

    def test_release_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="claimed"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        release(
            s, a, "PR-1", holder="ariadne", pool_sentinel="lei",
            reason="done", phase="pool", ts="2026-06-26T12:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "claimed"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    def test_release_done_does_not_clobber_existing_different_phase(self, tmp_path):
        # The --done branch has its own, separate code path merging
        # phase_updates into `updates` alongside the title rename -- covered
        # independently from the ordinary-release no-clobber case above.
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="claimed"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        release(
            s, a, "PR-1", holder="ariadne", pool_sentinel="lei",
            reason="done", done=True, phase="done", ts="2026-06-26T12:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "claimed"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # --- init() ---

    def test_init_stamps_phase(self, tmp_path):
        s = FileStore(tmp_path)
        a = SeqAllocator(recover=s.recover_max_seq)
        init(
            s, a, "PR-42",
            title="T", status="in-progress", turn="borges",
            participants=["borges"], turn_reason="init",
            phase="fairy", ts="2026-06-26T12:00:00Z",
        )
        raw = s.read_project("PR-42")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "fairy"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'fairy'" in lines[-1]

    def test_init_without_phase_omits_field(self, tmp_path):
        s = FileStore(tmp_path)
        a = SeqAllocator(recover=s.recover_max_seq)
        init(
            s, a, "PR-42",
            title="T", status="in-progress", turn="borges",
            participants=["borges"], turn_reason="init",
            ts="2026-06-26T12:00:00Z",
        )
        raw = s.read_project("PR-42")
        fields, _ = md.parse_frontmatter(raw)
        assert "phase" not in fields
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # --- sequential idempotency (test obligation 4) ---

    def test_flip_twice_in_a_row_stamps_exactly_once(self, store, allocator):
        """Two SEQUENTIAL calls to flip(), both phase="peer", on a baton
        with no initial `phase:` -- simulating two successive
        legacy-conversion attempts (#3039 U2a obligation 4, adapted for
        U2a). After BOTH calls, the file has exactly ONE conversion-note
        log line (not two): the second call's own freshly-read `fields`
        (via its own `store.read_project` at the top of `flip()`) already
        sees the first call's already-committed write and no-ops -- here,
        both calls pass the same value ("peer"), so the file simply ends
        at "peer" with a single stamp. This pins sequential idempotency,
        not safety under genuinely overlapping concurrent callers -- see
        `_maybe_stamp_phase`'s docstring and issue #3255."""
        flip(
            store, allocator, "PR-1", to_agent="borges", reason="one",
            phase="peer", ts="2026-06-26T12:00:00Z",
        )
        flip(
            store, allocator, "PR-1", to_agent="ariadne", reason="two",
            phase="peer", ts="2026-06-26T12:05:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        assert raw.count("phase stamped") == 1

    def test_claim_then_flip_sequential_stamps_only_the_first(self, store, allocator):
        """Sibling of the above with two DIFFERENT write-bodies (claim then
        flip), called SEQUENTIALLY, and two DIFFERENT phase values, to
        confirm the no-clobber contract holds ACROSS write-body types, not
        just within repeated calls to the same one: the second call's
        phase never overwrites the first's, regardless of which verb
        produced it. This pins sequential idempotency across verbs, not
        safety under genuinely overlapping concurrent callers."""
        claim(
            store, allocator, "PR-1", claimer="luria", pool_sentinel="lei",
            phase="claimed", ts="2026-06-26T12:00:00Z",
        )
        flip(
            store, allocator, "PR-1", to_agent="ariadne", reason="handoff",
            phase="peer", ts="2026-06-26T12:05:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "claimed"
        assert raw.count("phase stamped") == 1


# ---------------------------------------------------------------------------
# #3039 U2a round 3 -- the two-step conversion/transition design (Kepler's
# round-3 ruling, Borges's colleague review on PR #3254: the live
# double-claim race, independently reproduced by the dispatching agent).
#
# Contract-first (this repo's CLAUDE.md "Contract-first tests"): written
# against `2398270` (round 2's stamp-if-absent-everywhere `_maybe_stamp_phase`
# mechanism -- no `transition_phase` parameter existed on any write-body).
# The mutation control (confirming these three sequence tests go RED
# against that mechanism, while everything else in this file stays green)
# is run and reported in the PR handoff, not checked in as a second
# parallel test file -- see Borges's test obligation 5.
# ---------------------------------------------------------------------------

class TestU2aRound3PhaseSequences:
    """Borges's exact contract-first sequence tests (test obligations 1, 2,
    4), exercising the two-step conversion/transition design end-to-end
    against a real FileStore + SeqAllocator."""

    def test_pr_sequence_fairy_peer_peer_peer_merge(self, tmp_path):
        """init -> flip to colleague -> return-to-author (#2961) -> flip
        back to colleague -> flip to merge seat. Expected phase after each
        step: fairy, peer, peer, peer, merge (test obligation 1)."""
        s = FileStore(tmp_path)
        a = SeqAllocator(recover=s.recover_max_seq)
        pid = "PR-9900"

        init(
            s, a, pid,
            title="T", status="in-progress", turn="clio",
            participants=["clio", "ariadne"], turn_reason="start",
            by_agent="clio", author="clio", colleague="ariadne",
            transition_phase="fairy", ts="2026-09-30T00:00:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "fairy"

        # clio -> ariadne: fairy -> peer (send-for-review transition)
        flip(
            s, a, pid, to_agent="ariadne", reason="please review",
            by_agent="clio", transition_phase="peer", ts="2026-09-30T00:01:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "peer"

        # ariadne -> clio: #2961 return-to-author -- conversion-only,
        # preserves "peer" (NOT a phase regression to "fairy").
        flip(
            s, a, pid, to_agent="clio", reason="one blocker",
            by_agent="ariadne", phase="peer", ts="2026-09-30T00:02:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "peer"

        # clio -> ariadne again: still within peer phase -- conversion-only.
        flip(
            s, a, pid, to_agent="ariadne", reason="fixed",
            by_agent="clio", phase="peer", ts="2026-09-30T00:03:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "peer"

        # ariadne -> pool sentinel: maintainer-flip-to-merge-seat transition.
        flip(
            s, a, pid, to_agent="lei shi", reason="approved, ready",
            by_agent="ariadne", transition_phase="merge", ts="2026-09-30T00:04:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "merge"

    def test_bunch_sequence_pool_claimed_pool_claimed_done(self, tmp_path):
        """init -> claim -> release -> claim -> release --done. Expected
        phase after each step: pool, claimed, pool, claimed, done (test
        obligation 2)."""
        s = FileStore(tmp_path)
        a = SeqAllocator(recover=s.recover_max_seq)
        pid = "pool-9901"

        init(
            s, a, pid,
            title="T", status="planning", turn="lei shi",
            participants=["borges", "ariadne"], turn_reason="start",
            by_agent="borges", transition_phase="pool", ts="2026-09-30T00:00:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "pool"

        claim(
            s, a, pid, claimer="borges", pool_sentinel="lei shi",
            transition_phase="claimed", ts="2026-09-30T00:01:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "claimed"

        release(
            s, a, pid, holder="borges", pool_sentinel="lei shi",
            reason="done for now", transition_phase="pool", ts="2026-09-30T00:02:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "pool"

        claim(
            s, a, pid, claimer="ariadne", pool_sentinel="lei shi",
            transition_phase="claimed", ts="2026-09-30T00:03:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "claimed"

        release(
            s, a, pid, holder="ariadne", pool_sentinel="lei shi",
            reason="finished", done=True, transition_phase="done", ts="2026-09-30T00:04:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "done"

    def test_pr_sequence_from_genuinely_legacy_baton(self, tmp_path):
        """Test obligation 4: the PR sequence's post-init steps, replayed
        against a genuinely legacy baton (no `phase:` field at all, seeded
        directly rather than via `init()`) -- confirming conversion-then-
        transition composes correctly on a real legacy baton, not just a
        freshly-`transition_phase`-inited one.

        There is no legacy equivalent of the init() step itself (init's
        `transition_phase` is ALWAYS written when given -- see init()'s own
        docstring: there is no pre-existing `phase:` to check against, so a
        truthy value is always stamped), so this replays only the four
        POST-init steps. A legacy PR baton whose `turn` is NOT at the pool
        sentinel resolves via `_resolve_phase`'s legacy fallback to ""
        (empty/indeterminate) -- cmd_flip's own comment treats that
        identically to an explicit "fairy" for the fairy->peer transition
        gate (`_flip_current_phase in ("fairy", "")`), so the first flip
        below still correctly fires as a TRANSITION, exactly as it would
        against a freshly-inited "fairy" baton. Expected phase after each
        of the four steps: peer, peer, peer, merge -- the same tail
        Borges's 5-value sequence produces after its own "fairy" entry."""
        s = FileStore(tmp_path)
        a = SeqAllocator(recover=s.recover_max_seq)
        pid = "PR-9910"
        s.write_project(
            pid,
            "---\n"
            f"project: {pid}\n"
            "title: legacy PR\n"
            "status: in-progress\n"
            "turn: clio\n"
            "turn_since: 2026-09-29T00:00:00Z\n"
            'turn_reason: "start"\n'
            'turn_by: ""\n'
            "participants: [clio, ariadne]\n"
            "colleague: ariadne\n"
            "author: clio\n"
            "---\n"
            f"\n# {pid}\n\n## Turn log\n\n"
            "- 2026-09-29T00:00:00Z initialized → clio: start\n",
            seq=0,
        )

        flip(
            s, a, pid, to_agent="ariadne", reason="please review",
            by_agent="clio", transition_phase="peer", ts="2026-09-30T00:01:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "peer"

        flip(
            s, a, pid, to_agent="clio", reason="one blocker",
            by_agent="ariadne", phase="peer", ts="2026-09-30T00:02:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "peer"

        flip(
            s, a, pid, to_agent="ariadne", reason="fixed",
            by_agent="clio", phase="peer", ts="2026-09-30T00:03:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "peer"

        flip(
            s, a, pid, to_agent="lei shi", reason="approved, ready",
            by_agent="ariadne", transition_phase="merge", ts="2026-09-30T00:04:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "merge"

    def test_bunch_sequence_from_genuinely_legacy_baton(self, tmp_path):
        """Test obligation 4, bunch half: the SAME 4-step machine (claim ->
        release -> claim -> release --done), replayed against a genuinely
        legacy bunch baton (no `phase:` field, turn at the legacy pool
        sentinel) -- confirming the same final tail as Borges's 5-value
        bunch sequence (claimed, pool, claimed, done) composes correctly
        without ever going through init(). `claim`/`release` are ALWAYS
        transitions regardless of the baton's starting state (see their
        own docstrings), so the legacy-vs-fresh-inited distinction does
        not change which channel any of these four calls use -- only
        whether the FIRST call's stamp-if-absent-shaped predecessor
        (init's transition) ever ran."""
        s = FileStore(tmp_path)
        a = SeqAllocator(recover=s.recover_max_seq)
        pid = "pool-9911"
        s.write_project(
            pid,
            "---\n"
            f"project: {pid}\n"
            "title: legacy bunch\n"
            "status: planning\n"
            "turn: lei shi\n"
            "turn_since: 2026-09-29T00:00:00Z\n"
            'turn_reason: "start"\n'
            'turn_by: ""\n'
            "participants: [borges, ariadne]\n"
            "---\n"
            f"\n# {pid}\n\n## Turn log\n\n"
            "- 2026-09-29T00:00:00Z initialized → lei shi: start\n",
            seq=0,
        )

        claim(
            s, a, pid, claimer="borges", pool_sentinel="lei shi",
            transition_phase="claimed", ts="2026-09-30T00:01:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "claimed"

        release(
            s, a, pid, holder="borges", pool_sentinel="lei shi",
            reason="done for now", transition_phase="pool", ts="2026-09-30T00:02:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "pool"

        claim(
            s, a, pid, claimer="ariadne", pool_sentinel="lei shi",
            transition_phase="claimed", ts="2026-09-30T00:03:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "claimed"

        release(
            s, a, pid, holder="ariadne", pool_sentinel="lei shi",
            reason="finished", done=True, transition_phase="done", ts="2026-09-30T00:04:00Z",
        )
        fields, _ = md.parse_frontmatter(s.read_project(pid))
        assert fields["phase"] == "done"


class TestU2aRound3EightNewlyWiredConversions:
    """Minimal per-function coverage for the eight newly-wired write-bodies
    (#3039 U2a round 3, Kepler's "all twelve write bodies" scope ruling):
    one test per function confirming a still-legacy baton converts (stamps
    the legacy-derived phase) when touched by that verb, and a second test
    confirming an already-explicit-phase baton is left untouched -- mirrors
    the existing TestPhaseWrites pattern pinned for the original four.
    None of these eight ever pass `transition_phase` (per Kepler's ruling,
    none of them defines a transition) -- only the `phase` (conversion)
    channel is exercised here."""

    # -- set_status (no real caller today; wired for completeness) --

    def test_set_status_stamps_phase_on_legacy_baton(self, store, allocator):
        set_status(
            store, allocator, "PR-1", new_status="in-review", reason="review",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'peer'" in lines[-1]

    def test_set_status_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="merge"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        set_status(
            s, a, "PR-1", new_status="in-review", reason="review",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # -- close (per Kepler's ruling: conversion only, writes no transition) --

    def test_close_stamps_phase_on_legacy_baton(self, store, allocator):
        close(
            store, allocator, "PR-1", new_status="merged", reason="closing",
            phase="merge", ts="2026-09-30T00:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'merge'" in lines[-1]

    def test_close_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="peer"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        close(
            s, a, "PR-1", new_status="merged", reason="closing",
            phase="merge", ts="2026-09-30T00:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # -- reopen (per Kepler's ruling: conversion only) --

    def test_reopen_stamps_phase_on_legacy_baton(self, closed_store, closed_allocator):
        reopen(
            closed_store, closed_allocator, "PR-2", invoker="ariadne",
            new_status="in-progress", phase="merge", ts="2026-09-30T00:00:00Z",
        )
        raw = closed_store.read_project("PR-2")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'merge'" in lines[-1]

    def test_reopen_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        raw_seed = _baton_with_phase(turn="ariadne", phase="peer").replace(
            "status: in-progress", "status: merged",
        )
        s.write_project("PR-1", raw_seed, seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        reopen(
            s, a, "PR-1", invoker="ariadne", new_status="in-progress",
            phase="merge", ts="2026-09-30T00:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # -- rename (never a phase-machine edge) --

    def test_rename_stamps_phase_on_legacy_baton(self, store, allocator):
        rename(
            store, allocator, "PR-1", new_title="new title",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'peer'" in lines[-1]

    def test_rename_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="merge"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        rename(
            s, a, "PR-1", new_title="new title",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # -- anchor (never a phase-machine edge) --

    def test_anchor_stamps_phase_on_legacy_baton(self, store, allocator):
        anchor(
            store, allocator, "PR-1", github_anchor="pr/490",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'peer'" in lines[-1]

    def test_anchor_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="merge"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        anchor(
            s, a, "PR-1", github_anchor="pr/490",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # -- add_participant (never a phase-machine edge; conversion only fires
    # on the real-write path, never the idempotent no-op -- see its own
    # docstring) --

    def test_add_participant_stamps_phase_on_legacy_baton(self, store, allocator):
        add_participant(
            store, allocator, "PR-1", agent="ariadne", participant="luria",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'peer'" in lines[-1]

    def test_add_participant_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="merge"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        add_participant(
            s, a, "PR-1", agent="ariadne", participant="luria",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    def test_add_participant_idempotent_no_op_never_touches_phase(self, store, allocator):
        """The idempotent no-op path (participant already present) performs
        NO write at all -- conversion must not fire there either (#3039
        U2a round 3: nothing to attach a stamp to)."""
        seq_before, added = add_participant(
            store, allocator, "PR-1", agent="ariadne", participant="borges",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        assert added is False
        raw = store.read_project("PR-1")
        assert "phase" not in md.parse_frontmatter(raw)[0]
        assert "phase stamped" not in raw

    # -- set_colleague (never a phase-machine edge) --

    def test_set_colleague_stamps_phase_on_legacy_baton(self, store, allocator):
        set_colleague(
            store, allocator, "PR-1", agent="ariadne", colleague="luria",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = store.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "peer"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'peer'" in lines[-1]

    def test_set_colleague_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="merge"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        set_colleague(
            s, a, "PR-1", agent="ariadne", colleague="luria",
            phase="peer", ts="2026-09-30T00:00:00Z",
        )
        raw = s.read_project("PR-1")
        fields, _ = md.parse_frontmatter(raw)
        assert fields["phase"] == "merge"
        lines = [l for l in raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]

    # -- merge (per Kepler's ruling: conversion only; also archives the
    # project after write, so the assertions read from archive/) --

    def test_merge_stamps_phase_on_legacy_baton(self, store, allocator):
        merge(
            store, allocator, "PR-1", merged_by="luria",
            phase="merge", ts="2026-09-30T00:00:00Z",
        )
        archive_raw = (store.root / "archive" / "PR-1.md").read_text()
        fields, _ = md.parse_frontmatter(archive_raw)
        assert fields["phase"] == "merge"
        lines = [l for l in archive_raw.splitlines() if l.startswith("- ")]
        assert "phase stamped: 'merge'" in lines[-1]

    def test_merge_does_not_clobber_existing_different_phase(self, tmp_path):
        s = FileStore(tmp_path)
        s.write_project("PR-1", _baton_with_phase(turn="ariadne", phase="peer"), seq=0)
        a = SeqAllocator(recover=s.recover_max_seq)
        merge(
            s, a, "PR-1", merged_by="luria",
            phase="merge", ts="2026-09-30T00:00:00Z",
        )
        archive_raw = (s.root / "archive" / "PR-1.md").read_text()
        fields, _ = md.parse_frontmatter(archive_raw)
        assert fields["phase"] == "peer"
        lines = [l for l in archive_raw.splitlines() if l.startswith("- ")]
        assert "phase stamped" not in lines[-1]


# ---------------------------------------------------------------------------
# #3039 U2a round 4 -- conversion THEN transition, asserted on the LOG
# ---------------------------------------------------------------------------
#
# Round 3's two-step write-body (conversion = stamp-if-absent, then transition
# = unconditional overwrite) is exercised here through the REAL write-bodies,
# with BOTH `phase` (conversion) and `transition_phase` given on the same call
# -- which is what the CLI now sends for the five transition verbs (claim /
# release / release --done / flip fairy->peer / flip ->merge seat).
#
# CONTRACT (what round 4 fixes; stated here because prose does not execute):
#   * legacy baton (no `phase:`): the turn log carries the conversion line
#     (`phase stamped: '<legacy phase>' (legacy conversion ...`) FOLLOWED BY
#     the transition line (`phase: '<new>' (transition)`), in that order, and
#     the stored `phase:` ends at the transition value.
#   * baton that already has an explicit `phase:`: conversion is a no-op --
#     ONLY the transition line appears, no conversion line, no duplicates.
#
# Earlier tests assert the request body or the stored field, never the log
# SEQUENCE -- that gap is how round 3 shipped legacy transitions with no
# conversion record.

def _phase_log(store, pid):
    """The turn-log lines (``- ...``) that mention phase, in file order."""
    content = store.read_project(pid)
    return [
        l for l in content.splitlines()
        if l.startswith("- ") and "phase" in l
    ]


def _seed_round4(tmp_path, pid, *, turn, phase=None):
    s = FileStore(tmp_path)
    content = _baton_with_phase(turn=turn, phase=phase).replace("PR-1", pid)
    s.write_project(pid, content, seq=0)
    return s, SeqAllocator(recover=s.recover_max_seq)


# (label, call(store, allocator, pid, phase, transition_phase), turn,
#  legacy conversion phase, transition value)
def _r4_claim(s, a, pid, phase, tp):
    claim(s, a, pid, claimer="borges", pool_sentinel="lei shi",
          phase=phase, transition_phase=tp, ts="2026-09-30T01:00:00Z")


def _r4_release(s, a, pid, phase, tp):
    release(s, a, pid, holder="borges", pool_sentinel="lei shi", reason="r",
            phase=phase, transition_phase=tp, ts="2026-09-30T01:00:00Z")


def _r4_release_done(s, a, pid, phase, tp):
    release(s, a, pid, holder="borges", pool_sentinel="lei shi", reason="r",
            done=True, phase=phase, transition_phase=tp, ts="2026-09-30T01:00:00Z")


def _r4_flip(s, a, pid, phase, tp):
    flip(s, a, pid, to_agent="borges", reason="r", by_agent="ariadne",
         phase=phase, transition_phase=tp, ts="2026-09-30T01:00:00Z")


_ROUND4_CASES = [
    # id,              call,             turn,       legacy conv, transition
    ("claim",          _r4_claim,        "lei shi",  "pool",      "claimed"),
    ("release",        _r4_release,      "borges",   "claimed",   "pool"),
    ("release_done",   _r4_release_done, "borges",   "claimed",   "done"),
    ("flip_to_peer",   _r4_flip,         "ariadne",  "fairy",     "peer"),
    ("flip_to_merge",  _r4_flip,         "ariadne",  "peer",      "merge"),
]


class TestU2aRound4ConversionThenTransitionLog:

    @pytest.mark.parametrize(
        "call,turn,conv,trans",
        [c[1:] for c in _ROUND4_CASES], ids=[c[0] for c in _ROUND4_CASES],
    )
    def test_legacy_baton_logs_conversion_line_then_transition_line(
        self, tmp_path, call, turn, conv, trans,
    ):
        s, a = _seed_round4(tmp_path, "PR-1", turn=turn, phase=None)
        call(s, a, "PR-1", conv, trans)

        log = _phase_log(s, "PR-1")
        assert len(log) == 2, log
        assert f"phase stamped: {conv!r} (legacy conversion" in log[0]
        assert f"phase: {trans!r} (transition)" in log[1]
        # ...and they sit in the SAME write, in this order, at the tail.
        all_lines = [l for l in s.read_project("PR-1").splitlines() if l.startswith("- ")]
        assert all_lines[-2:] == log
        fields, _ = md.parse_frontmatter(s.read_project("PR-1"))
        assert fields["phase"] == trans

    @pytest.mark.parametrize(
        "call,turn,conv,trans",
        [c[1:] for c in _ROUND4_CASES], ids=[c[0] for c in _ROUND4_CASES],
    )
    def test_explicit_phase_baton_logs_only_the_transition_line(
        self, tmp_path, call, turn, conv, trans,
    ):
        """The CLI sends conversion = the explicit value (what
        `_conversion_phase` returns when `phase:` is present); the write-body
        no-ops it -- no conversion line, no duplicate, just the transition."""
        explicit = "peer" if trans != "peer" else "fairy"
        s, a = _seed_round4(tmp_path, "PR-1", turn=turn, phase=explicit)
        call(s, a, "PR-1", explicit, trans)

        log = _phase_log(s, "PR-1")
        assert len(log) == 1, log
        assert f"phase: {trans!r} (transition)" in log[0]
        assert "legacy conversion" not in log[0]
        fields, _ = md.parse_frontmatter(s.read_project("PR-1"))
        assert fields["phase"] == trans

    def test_legacy_baton_with_no_conversion_value_still_transitions(self, tmp_path):
        """`_conversion_phase` can return None (pool sentinel unresolvable):
        the CLI then sends `phase: None`. The transition must still apply,
        with no conversion line and no crash."""
        s, a = _seed_round4(tmp_path, "PR-1", turn="lei shi", phase=None)
        _r4_claim(s, a, "PR-1", None, "claimed")
        log = _phase_log(s, "PR-1")
        assert len(log) == 1 and "(transition)" in log[0]
        fields, _ = md.parse_frontmatter(s.read_project("PR-1"))
        assert fields["phase"] == "claimed"

    def test_conversion_only_flip_is_unchanged(self, tmp_path):
        """#2961 return-to-author / self-flip shape: `phase` alone, no
        `transition_phase` -- one conversion line on a legacy baton, nothing
        else, and the stored phase is the conversion value."""
        s, a = _seed_round4(tmp_path, "PR-1", turn="ariadne", phase=None)
        _r4_flip(s, a, "PR-1", "peer", None)
        log = _phase_log(s, "PR-1")
        assert len(log) == 1
        assert "phase stamped: 'peer' (legacy conversion" in log[0]
        assert "(transition)" not in log[0]
        fields, _ = md.parse_frontmatter(s.read_project("PR-1"))
        assert fields["phase"] == "peer"
