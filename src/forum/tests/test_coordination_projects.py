"""Tests for coordination.projects — the relocated baton write-fns (flip, claim, release, set_status).

Exercises the full write body against a real FileStore + SeqAllocator: read →
transform → atomic-write under the allocator lock (fork-4), seq embedded + returned.
"""

import pytest

from forum.coordination import (
    FileStore,
    ProjectNotFound,
    SeqAllocator,
    claim,
    flip,
    release,
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
