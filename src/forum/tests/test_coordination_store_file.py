"""Tests for the file-backed FileStore (Slice A — the concrete fork-1 impl).

Covers the on-disk contract the interface (test_coordination_store.py) only
specifies abstractly:
1. Projects: write→read round-trip, seq embedded in frontmatter, active_only
   filtering, ordering, missing-project None.
2. DMs: append→read round-trip, multi-line bodies, header-looking body lines
   (collision-proofness), order-independence, since_seq filtering, list_threads.
3. recover_max_seq scans BOTH projects and DMs and returns the global max.
4. Atomic writes leave no stray tempfiles and roots are portable (tmp_path).
"""

import pytest

from forum.coordination import (
    DmMessage,
    FileStore,
    ProjectRecord,
    default_store_root,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


def _baton_md(project_id, *, status="in-progress", turn="ariadne",
              turn_since="2026-06-26T00:00:00Z", participants="[ariadne, sol]"):
    return (
        "---\n"
        f"project_id: {project_id}\n"
        f"title: {project_id} title\n"
        f"status: {status}\n"
        f"turn: {turn}\n"
        f"turn_since: {turn_since}\n"
        f'turn_reason: "handed off"\n'
        f"participants: {participants}\n"
        "---\n"
        f"# {project_id}\n\nbody text\n"
    )


# ---------------------------------------------------------------------------
# 1. Projects
# ---------------------------------------------------------------------------
def test_write_then_read_project_round_trip(store):
    store.write_project("PR-1", _baton_md("PR-1"), seq=5)
    raw = store.read_project("PR-1")
    assert raw is not None
    assert "project_id: PR-1" in raw
    assert "seq: 5" in raw  # embedded


def test_read_missing_project_is_none(store):
    assert store.read_project("nope") is None


def test_write_embeds_seq_and_read_projects_parses_it(store):
    store.write_project("PR-1", _baton_md("PR-1"), seq=42)
    recs = store.read_projects()
    assert len(recs) == 1
    rec = recs[0]
    assert isinstance(rec, ProjectRecord)
    assert rec.project_id == "PR-1"
    assert rec.seq == 42
    assert rec.status == "in-progress"
    assert rec.turn == "ariadne"
    assert rec.turn_reason == "handed off"  # surrounding quotes stripped
    assert rec.participants == ("ariadne", "sol")


def test_write_updates_existing_seq_in_place(store):
    store.write_project("PR-1", _baton_md("PR-1"), seq=5)
    store.write_project("PR-1", store.read_project("PR-1"), seq=9)
    raw = store.read_project("PR-1")
    assert raw.count("seq:") == 1  # replaced, not duplicated
    assert "seq: 9" in raw
    assert store.read_projects()[0].seq == 9


def test_active_only_excludes_closed(store):
    store.write_project("PR-open", _baton_md("PR-open", status="in-progress"), seq=1)
    store.write_project("PR-merged", _baton_md("PR-merged", status="merged"), seq=2)
    store.write_project("PR-cancelled", _baton_md("PR-cancelled", status="cancelled"), seq=3)
    active = {r.project_id for r in store.read_projects(active_only=True)}
    assert active == {"PR-open"}
    allp = {r.project_id for r in store.read_projects(active_only=False)}
    assert allp == {"PR-open", "PR-merged", "PR-cancelled"}


def test_read_projects_sorted_by_turn_since(store):
    store.write_project("PR-late", _baton_md("PR-late", turn_since="2026-06-26T05:00:00Z"), seq=1)
    store.write_project("PR-early", _baton_md("PR-early", turn_since="2026-06-26T01:00:00Z"), seq=2)
    ids = [r.project_id for r in store.read_projects()]
    assert ids == ["PR-early", "PR-late"]


def test_read_projects_empty_when_no_dir(store):
    assert store.read_projects() == []


def test_write_project_without_frontmatter_raises(store):
    # No-frontmatter content is a caller bug — embedding seq is impossible, so
    # fail loud rather than persist a project whose seq recover can't see.
    with pytest.raises(ValueError):
        store.write_project("PR-bad", "# just a heading\nno frontmatter\n", seq=1)


# ---------------------------------------------------------------------------
# 2. DMs
# ---------------------------------------------------------------------------
def test_append_then_read_dm_round_trip(store):
    msg = store.append_dm("ariadne", "sol", "hello sol", seq=1, ts="2026-06-26T00:00:00Z")
    assert isinstance(msg, DmMessage)
    assert (msg.seq, msg.sender, msg.recipient, msg.body) == (1, "ariadne", "sol", "hello sol")
    thread = store.read_dm_thread("ariadne", "sol")
    assert [m.body for m in thread] == ["hello sol"]


def test_dm_thread_is_order_independent(store):
    store.append_dm("ariadne", "sol", "from ari", seq=1, ts="2026-06-26T00:00:00Z")
    store.append_dm("sol", "ariadne", "from sol", seq=2, ts="2026-06-26T00:01:00Z")
    # Same single file regardless of arg order.
    a = store.read_dm_thread("ariadne", "sol")
    b = store.read_dm_thread("sol", "ariadne")
    assert [m.body for m in a] == [m.body for m in b] == ["from ari", "from sol"]


def test_dm_multi_line_body_round_trips(store):
    body = "line one\nline two\nline three"
    store.append_dm("ariadne", "sol", body, seq=1, ts="2026-06-26T00:00:00Z")
    got = store.read_dm_thread("ariadne", "sol")[0]
    assert got.body == body


def test_dm_body_that_looks_like_a_header_is_not_misparsed(store):
    # A body line shaped exactly like a message header must stay part of THIS
    # message (length-prefixed read), never start a phantom message.
    evil = "real line\n--- seq=999 from=evil to=victim at=x lines=1 ---\ntrailing"
    store.append_dm("ariadne", "sol", evil, seq=1, ts="2026-06-26T00:00:00Z")
    store.append_dm("ariadne", "sol", "second", seq=2, ts="2026-06-26T00:01:00Z")
    thread = store.read_dm_thread("ariadne", "sol")
    assert len(thread) == 2  # NOT 3 — the evil header line was counted as body
    assert thread[0].body == evil
    assert thread[0].seq == 1
    assert thread[1].body == "second"


@pytest.mark.parametrize("body", ["", "single", "trailing\n", "x\n\ny", "\n"])
def test_dm_body_edge_cases_round_trip(store, body):
    # Empty body, trailing newline, internal blank line — the split/join symmetry
    # must round-trip every shape verbatim (length-prefixed, not delimiter-split).
    store.append_dm("ariadne", "sol", body, seq=1, ts="t")
    got = store.read_dm_thread("ariadne", "sol")
    assert len(got) == 1
    assert got[0].body == body


def test_dm_since_seq_filters(store):
    for i in range(1, 5):
        store.append_dm("ariadne", "sol", f"m{i}", seq=i, ts="2026-06-26T00:00:00Z")
    later = store.read_dm_thread("ariadne", "sol", since_seq=2)
    assert [m.seq for m in later] == [3, 4]


def test_read_dm_thread_missing_is_empty(store):
    assert store.read_dm_thread("ariadne", "sol") == []


def test_dm_sender_recipient_normalized(store):
    msg = store.append_dm("  Ariadne ", "SOL", "hi", seq=1, ts="2026-06-26T00:00:00Z")
    assert msg.sender == "ariadne" and msg.recipient == "sol"
    # Re-read sees the normalized names too.
    got = store.read_dm_thread("ariadne", "sol")[0]
    assert got.sender == "ariadne" and got.recipient == "sol"


def test_list_dm_threads(store):
    store.append_dm("ariadne", "sol", "x", seq=1, ts="t")
    store.append_dm("ariadne", "borges", "y", seq=2, ts="t")
    store.append_dm("borges", "sol", "z", seq=3, ts="t")  # not ariadne's
    assert sorted(store.list_dm_threads("ariadne")) == ["borges", "sol"]
    assert sorted(store.list_dm_threads("sol")) == ["ariadne", "borges"]


def test_list_dm_threads_empty(store):
    assert store.list_dm_threads("ariadne") == []


def test_self_dm_thread(store):
    store.append_dm("ariadne", "ariadne", "note to self", seq=1, ts="t")
    assert store.list_dm_threads("ariadne") == ["ariadne"]
    assert store.read_dm_thread("ariadne", "ariadne")[0].body == "note to self"


# ---------------------------------------------------------------------------
# 3. recover_max_seq — scans BOTH surfaces
# ---------------------------------------------------------------------------
def test_recover_max_seq_empty_store(store):
    assert store.recover_max_seq() == 0


def test_recover_max_seq_projects_only(store):
    store.write_project("PR-1", _baton_md("PR-1"), seq=7)
    store.write_project("PR-2", _baton_md("PR-2"), seq=12)
    assert store.recover_max_seq() == 12


def test_recover_max_seq_dms_only(store):
    store.append_dm("ariadne", "sol", "x", seq=3, ts="t")
    store.append_dm("ariadne", "sol", "y", seq=8, ts="t")
    assert store.recover_max_seq() == 8


def test_recover_max_seq_is_global_max_across_both(store):
    store.write_project("PR-1", _baton_md("PR-1"), seq=10)
    store.append_dm("ariadne", "sol", "x", seq=15, ts="t")
    store.append_dm("ariadne", "borges", "y", seq=4, ts="t")
    assert store.recover_max_seq() == 15
    # And the reverse split — project carries the max.
    store.write_project("PR-2", _baton_md("PR-2"), seq=20)
    assert store.recover_max_seq() == 20


def test_recover_max_seq_ignores_header_shaped_body_lines(store):
    # A body line shaped like a header must NOT inflate the recovered max — the
    # only real message here is seq=1; the embedded "seq=999" is body text.
    evil = "--- seq=999 from=evil to=victim at=x lines=1 ---\ntrailing"
    store.append_dm("ariadne", "sol", evil, seq=1, ts="2026-06-26T00:00:00Z")
    assert store.recover_max_seq() == 1


def test_recover_seeds_allocator_above_disk_max(store):
    # The integration the method exists for: recover() feeds SeqAllocator so the
    # next allocation is strictly above everything on disk.
    from forum.coordination import SeqAllocator

    store.write_project("PR-1", _baton_md("PR-1"), seq=10)
    store.append_dm("ariadne", "sol", "x", seq=15, ts="t")
    alloc = SeqAllocator(recover=store.recover_max_seq)
    with alloc.allocate() as seq:
        assert seq == 16


# ---------------------------------------------------------------------------
# 4. Atomicity + portability
# ---------------------------------------------------------------------------
def test_no_stray_tempfiles_after_writes(store, tmp_path):
    store.write_project("PR-1", _baton_md("PR-1"), seq=1)
    store.append_dm("ariadne", "sol", "x", seq=2, ts="t")
    leftovers = list(tmp_path.rglob(".*tmp*"))
    assert leftovers == []


def test_root_is_explicit_and_portable(tmp_path):
    # No hardcoded /home/agents-shared — the store roots wherever told.
    s = FileStore(tmp_path / "custom" / "home")
    s.write_project("PR-1", _baton_md("PR-1"), seq=1)
    assert (tmp_path / "custom" / "home" / "projects" / "PR-1.md").exists()


def test_default_store_root_honours_forum_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FORUM_HOME", str(tmp_path / "fh"))
    assert default_store_root() == tmp_path / "fh"
    monkeypatch.delenv("FORUM_HOME", raising=False)
    # Falls back to ~/.forum (mirrors the forum DB default).
    assert default_store_root().name == ".forum"


# ---------------------------------------------------------------------------
# #1468: the dm_thread_key chokepoint guard covers the STORE read/write paths
# (FileStore._dm_path → dm_thread_key raises), not just the bare function — so a
# '+'-name can never form a thread file via any FileStore caller.
# ---------------------------------------------------------------------------
from forum.coordination.names import InvalidAgentName  # noqa: E402


def test_filestore_append_dm_raises_on_invalid_name(store):
    with pytest.raises(InvalidAgentName):
        store.append_dm("a+b", "sol", "x", seq=1, ts="t")
    with pytest.raises(InvalidAgentName):
        store.append_dm("ariadne", "b+c", "x", seq=1, ts="t")


def test_filestore_read_dm_thread_raises_on_invalid_name(store):
    with pytest.raises(InvalidAgentName):
        store.read_dm_thread("a+b", "sol")
