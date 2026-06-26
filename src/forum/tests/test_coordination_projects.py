"""Tests for coordination.projects — the relocated baton write-fns (flip).

Exercises the full write body against a real FileStore + SeqAllocator: read →
transform → atomic-write under the allocator lock (fork-4), seq embedded + returned.
"""

import pytest

from forum.coordination import (
    FileStore,
    ProjectNotFound,
    SeqAllocator,
    flip,
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
