"""#3255: every ``projects.py`` write-body reads, decides and writes inside ONE
allocator critical section (no lost updates).

Contract (stated by the issue, asserted here before the implementation): a
write-body whose read happened BEFORE the lock let a competing write that landed
between that read and the lock be silently clobbered by the whole-document
overwrite. With the read moved inside the lock, the second writer sees the first
writer's committed state, so the final document reflects BOTH operations
(verbs that always write) or the second is a no-op / refused on the fresh state
(``add_participant`` dedup).

The interleaving is deterministic: ``_RacyAllocator`` (mirrors #3433's) runs a
hook in ``locked()`` / ``allocate()`` BEFORE delegating, i.e. after any read the
function made without the lock and before it acquires it. The hook is one-shot
and disarms itself so the competing write does not recurse.

Scope caveat (same as #3433): the allocator lock is a ``threading.Lock``, so
this serializes writers within ONE forum process only.

Identities are SYNTHETIC.
"""

import pytest

from forum.coordination import (
    FileStore,
    SeqAllocator,
    add_participant,
    anchor,
    claim,
    flip,
    merge,
    release,
    rename,
    set_colleague,
    set_status,
)
from forum.coordination import markdown as md

PID = "PR-1"
SENTINEL = "pool"


def _baton():
    return (
        "---\n"
        f"project_id: {PID}\n"
        "title: a title\n"
        "status: in-progress\n"
        "turn: ariadne\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "start"\n'
        "participants: [ariadne, borges]\n"
        "---\n"
        f"# {PID}\n\nsome body\n"
    )


class _RacyAllocator(SeqAllocator):
    """Runs a one-shot ``before_lock`` hook before acquiring the lock."""

    def __init__(self, recover, hook=None):
        super().__init__(recover=recover)
        self.hook = hook

    def _fire(self):
        hook, self.hook = self.hook, None  # one-shot: the competitor must not recurse
        if hook is not None:
            hook()

    def locked(self):
        self._fire()
        return super().locked()

    def allocate(self):
        self._fire()
        return super().allocate()


@pytest.fixture
def env(tmp_path):
    store = FileStore(tmp_path)
    store.write_project(PID, _baton(), seq=0)
    alloc = _RacyAllocator(store.recover_max_seq)
    return store, alloc, tmp_path


def _fields(store):
    content = store.read_project(PID)
    return md.parse_frontmatter(content)[0], content


def _race(store, alloc, competitor, victim):
    alloc.hook = lambda: competitor(store, alloc)
    return victim(store, alloc)


def _comp_rename(store, alloc):
    rename(store, alloc, PID, new_title="competitor title", ts="2026-06-27T00:00:00Z")


def _comp_flip(store, alloc):
    flip(store, alloc, PID, to_agent="borges", reason="competitor", ts="2026-06-27T00:00:00Z")


T = "2026-06-28T00:00:00Z"

# (id, competitor, victim, extra assertion on (fields, content))
_CASES = [
    ("flip",
     _comp_rename,
     lambda s, a: flip(s, a, PID, to_agent="borges", reason="victim", ts=T),
     lambda f, c: f["turn"].strip() == "borges" and "ariadne → borges: victim" in c),
    ("claim",
     _comp_rename,
     lambda s, a: claim(s, a, PID, claimer="borges", pool_sentinel=SENTINEL, ts=T),
     lambda f, c: f["turn"].strip() == "borges" and "pool → borges: claimed" in c),
    ("release",
     _comp_rename,
     lambda s, a: release(s, a, PID, holder="ariadne", pool_sentinel=SENTINEL, reason="done", ts=T),
     lambda f, c: f["turn"].strip() == SENTINEL and "ariadne → pool: done" in c),
    ("set_status",
     _comp_rename,
     lambda s, a: set_status(s, a, PID, new_status="in-review", reason="r", ts=T),
     lambda f, c: f["status"].strip() == "in-review"),
    ("anchor",
     _comp_rename,
     lambda s, a: anchor(s, a, PID, github_anchor="pr/9", ts=T),
     lambda f, c: f["github"].strip() == "pr/9"),
    ("set_colleague",
     _comp_rename,
     lambda s, a: set_colleague(s, a, PID, agent="ariadne", colleague="borges", ts=T),
     lambda f, c: f["colleague"].strip() == "borges"),
    ("add_participant",
     _comp_rename,
     lambda s, a: add_participant(s, a, PID, agent="ariadne", participant="carol", ts=T),
     lambda f, c: "carol" in f["participants"]),
]


@pytest.mark.parametrize("name,competitor,victim,check", _CASES, ids=[c[0] for c in _CASES])
def test_competing_rename_is_not_lost(env, name, competitor, victim, check):
    """A rename lands between the victim's (old) pre-lock read and its lock.
    Both effects must survive in the final document."""
    store, alloc, _ = env
    _race(store, alloc, competitor, victim)
    fields, content = _fields(store)
    assert fields["title"].strip() == "competitor title", "competitor's write was clobbered"
    assert "renamed: title from" in content  # its turn-log line survived too
    assert check(fields, content), "victim's own effect missing"


def test_rename_does_not_clobber_competing_flip(env):
    store, alloc, _ = env
    _race(store, alloc, _comp_flip,
          lambda s, a: rename(s, a, PID, new_title="new", ts=T))
    fields, content = _fields(store)
    assert fields["title"].strip() == "new"
    assert fields["turn"].strip() == "borges", "competing flip was clobbered"
    assert "ariadne → borges: competitor" in content


def test_merge_does_not_clobber_competing_flip(env):
    store, alloc, tmp_path = env
    _race(store, alloc, _comp_flip,
          lambda s, a: merge(s, a, PID, merged_by="ariadne", ts=T))
    archived = tmp_path / "archive" / f"{PID}.md"
    content = archived.read_text(encoding="utf-8")
    fields, _ = md.parse_frontmatter(content)
    assert fields["status"].strip() == "merged"
    assert fields["turn"].strip() == "borges", "competing flip was clobbered"
    assert "ariadne → borges: competitor" in content


def test_add_participant_dedup_decided_on_fresh_state(env):
    """Competitor adds 'carol' first; the victim's add of 'carol' must see that
    and no-op (no duplicate entry, no new seq), not decide on its stale read."""
    store, alloc, _ = env

    def comp(s, a):
        add_participant(s, a, PID, agent="ariadne", participant="carol", ts="2026-06-27T00:00:00Z")

    seq_after_comp = {}
    alloc.hook = lambda: (comp(store, alloc), seq_after_comp.setdefault("seq", alloc.current()))
    seq, added = add_participant(store, alloc, PID, agent="ariadne", participant="carol", ts=T)
    fields, content = _fields(store)
    assert added is False
    assert seq == seq_after_comp["seq"]  # the baton's last committed seq; none burned
    assert md_participants(fields).count("carol") == 1
    assert content.count("added participant: carol") == 1


def md_participants(fields):
    return [p.strip() for p in fields["participants"].strip("[] ").split(",")]


def test_missing_project_still_raises_and_burns_no_seq(env):
    from forum.coordination import ProjectNotFound
    store, alloc, _ = env
    before = alloc.current()
    for call in (
        lambda: flip(store, alloc, "nope", to_agent="x", reason="r"),
        lambda: claim(store, alloc, "nope", claimer="x", pool_sentinel=SENTINEL),
        lambda: release(store, alloc, "nope", holder="x", pool_sentinel=SENTINEL, reason="r"),
        lambda: rename(store, alloc, "nope", new_title="t"),
    ):
        with pytest.raises(ProjectNotFound):
            call()
    assert alloc._seq == before  # no refusal burned a seq
