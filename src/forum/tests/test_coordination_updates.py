"""Tests for forum.coordination.updates — the unified /api/updates feed-builder.

Covers the Phase-1 contract (UCS spec §3):
  - kinds = {dm, baton}; board collapsed into baton; unknown kinds ignored.
  - relevance filter: DMs to me / batons whose turn is mine — nothing else.
  - `since` EXCLUSIVE; window is exactly (since, as_of].
  - as_of = allocator.current(); items with seq > as_of are withheld (idempotent
    re-poll) — a write that commits after the watermark snapshot is NOT served.
  - ts = injected liveness clock (the consumer keys liveness on ts-advance).
  - wake tier carried on every item; updates sorted by seq ascending.
"""

import pytest

from forum.coordination import CoordinationStore, DmMessage, ProjectRecord, dm_thread_key
from forum.coordination.seq import SeqAllocator
from forum.coordination.updates import build_updates


class StubStore(CoordinationStore):
    """In-memory store: hand it DM threads + project records directly."""

    def __init__(self, *, projects=None):
        self._threads = {}
        self._projects = list(projects or [])

    # --- DM side ---
    def append_dm(self, sender, recipient, body, *, seq, ts):
        key = dm_thread_key(sender, recipient)
        msg = DmMessage(seq, sender, recipient, body, ts)
        self._threads.setdefault(key, []).append(msg)
        return msg

    def read_dm_thread(self, a, b, *, since_seq=0):
        key = dm_thread_key(a, b)
        return [m for m in self._threads.get(key, []) if m.seq > since_seq]

    def list_dm_threads(self, agent):
        out = []
        for key in self._threads:
            parts = key.split("+")
            if agent in parts:
                out.append(next((p for p in parts if p != agent), parts[0]))
        return out

    def list_all_dm_threads(self):
        pairs = []
        for key in self._threads:
            parts = key.split("+")
            if len(parts) == 2:
                pairs.append((parts[0], parts[1]))
        return sorted(pairs)

    # --- project side ---
    def read_projects(self, *, active_only=True):
        if active_only:
            return [p for p in self._projects if p.status != "closed"]
        return list(self._projects)

    def read_project(self, pid):
        return next((p.raw for p in self._projects if p.project_id == pid), None)

    def write_project(self, pid, content, *, seq):
        pass

    def archive_project(self, project_id):
        pass

    def recover_max_seq(self):
        seqs = [m.seq for ms in self._threads.values() for m in ms]
        seqs += [p.seq for p in self._projects]
        return max(seqs, default=0)


def _proj(pid, turn, seq, *, status="in-progress", title="t", reason="r",
          turn_since="2026-06-26T00:00:00Z"):
    return ProjectRecord(
        project_id=pid, title=title, status=status, turn=turn,
        turn_since=turn_since, turn_reason=reason,
        participants=("ariadne", "borges"), seq=seq, raw="",
    )


def _alloc(watermark):
    """A SeqAllocator whose current() returns `watermark` (the served high-water)."""
    return SeqAllocator(recover=lambda: watermark)


FIXED_TS = "2026-06-26T20:00:00Z"


def _build(store, agent="ariadne", *, since=0, kinds=None, watermark=100):
    return build_updates(
        store, _alloc(watermark), agent,
        since=since, kinds=kinds, now_fn=lambda: FIXED_TS,
    )


# ---------------------------------------------------------------------------
# shape + liveness
# ---------------------------------------------------------------------------
def test_empty_store_returns_alive_empty():
    out = _build(StubStore(), watermark=0)
    assert out == {"updates": [], "as_of": 0, "ts": FIXED_TS}


def test_as_of_is_allocator_current():
    out = _build(StubStore(), watermark=42)
    assert out["as_of"] == 42


def test_ts_comes_from_now_fn():
    # ts is the liveness signal — distinct from as_of, advances every call.
    out = _build(StubStore(), watermark=7)
    assert out["ts"] == FIXED_TS


# ---------------------------------------------------------------------------
# dm relevance
# ---------------------------------------------------------------------------
def test_dm_to_me_included_from_me_excluded():
    store = StubStore()
    store.append_dm("borges", "ariadne", "hi ari", seq=5, ts=FIXED_TS)   # to me
    store.append_dm("ariadne", "borges", "hi bor", seq=6, ts=FIXED_TS)   # from me
    out = _build(store, "ariadne")
    kinds = [(u["kind"], u["seq"]) for u in out["updates"]]
    assert kinds == [("dm", 5)]
    assert out["updates"][0]["sender"] == "borges"
    assert out["updates"][0]["wake"] == "act-now"
    # per-item event-time is `event_ts` (uniform across kinds), NOT `ts` — `ts` is
    # reserved for the envelope liveness clock (no item-vs-envelope key collision).
    assert out["updates"][0]["event_ts"] == FIXED_TS
    assert "ts" not in out["updates"][0]


def test_dm_in_other_pair_thread_excluded():
    store = StubStore()
    store.append_dm("borges", "sol", "not for ari", seq=8, ts=FIXED_TS)
    out = _build(store, "ariadne")
    assert out["updates"] == []


# ---------------------------------------------------------------------------
# baton relevance
# ---------------------------------------------------------------------------
def test_baton_turn_mine_included_else_excluded():
    store = StubStore(projects=[
        _proj("PR-1", turn="ariadne", seq=10),
        _proj("PR-2", turn="borges", seq=11),
    ])
    out = _build(store, "ariadne")
    assert [(u["kind"], u["project_id"]) for u in out["updates"]] == [("baton", "PR-1")]
    assert out["updates"][0]["wake"] == "act-now"
    # baton's per-item event-time is turn_since — uniform `event_ts` key with dm.
    assert out["updates"][0]["event_ts"] == "2026-06-26T00:00:00Z"  # = turn_since


def test_baton_closed_project_excluded_active_only():
    store = StubStore(projects=[_proj("PR-3", turn="ariadne", seq=12, status="closed")])
    out = _build(store, "ariadne")
    assert out["updates"] == []


def test_baton_empty_turn_since_ships_empty_event_ts_not_none():
    # turn_since is structurally always a str (FileStore: fields.get("turn_since","")),
    # so an unstamped / old-format baton ships event_ts="" — a PRESENT string, never
    # None. Locks the invariant the consumer depends on (Borges, data-owner, #170).
    store = StubStore(projects=[_proj("PR-old", turn="ariadne", seq=10, turn_since="")])
    item = _build(store, "ariadne")["updates"][0]
    assert item["event_ts"] == ""          # present + empty, NOT None / NOT missing
    assert item["event_ts"] is not None


# ---------------------------------------------------------------------------
# cursor: since exclusive + (since, as_of] window
# ---------------------------------------------------------------------------
def test_since_is_exclusive():
    store = StubStore(projects=[
        _proj("PR-at", turn="ariadne", seq=20),
        _proj("PR-after", turn="ariadne", seq=21),
    ])
    store.append_dm("borges", "ariadne", "old", seq=20, ts=FIXED_TS)
    out = _build(store, "ariadne", since=20)
    # seq == since (20) excluded for BOTH kinds; only seq > 20 survives.
    assert [u["seq"] for u in out["updates"]] == [21]


def test_seq_above_as_of_withheld_for_idempotent_repoll():
    # A write that committed AFTER the watermark snapshot (seq 30 > as_of 25) must
    # not be served — it belongs to the next poll's (25, …] window.
    store = StubStore(projects=[
        _proj("PR-served", turn="ariadne", seq=24),
        _proj("PR-future", turn="ariadne", seq=30),
    ])
    out = _build(store, "ariadne", watermark=25)
    assert [u["seq"] for u in out["updates"]] == [24]
    assert out["as_of"] == 25


def test_dm_seq_above_as_of_withheld():
    # DM-path mirror of the baton idempotency case: a DM that committed after the
    # watermark snapshot (seq 30 > as_of 25) must be withheld by the m.seq <= as_of
    # upper bound — it belongs to the next poll's (25, …] window. Guards the DM
    # upper-bound filter against a future refactor that drops it.
    store = StubStore()
    store.append_dm("borges", "ariadne", "served", seq=24, ts=FIXED_TS)
    store.append_dm("borges", "ariadne", "future", seq=30, ts=FIXED_TS)
    out = _build(store, "ariadne", watermark=25)
    assert [u["seq"] for u in out["updates"]] == [24]
    assert out["as_of"] == 25


# ---------------------------------------------------------------------------
# kinds narrowing + ordering + case-insensitivity
# ---------------------------------------------------------------------------
def test_kinds_narrows_to_subset():
    store = StubStore(projects=[_proj("PR-1", turn="ariadne", seq=10)])
    store.append_dm("borges", "ariadne", "hi", seq=11, ts=FIXED_TS)
    only_dm = _build(store, "ariadne", kinds=["dm"])
    assert [u["kind"] for u in only_dm["updates"]] == ["dm"]


def test_unknown_kind_ignored():
    store = StubStore(projects=[_proj("PR-1", turn="ariadne", seq=10)])
    out = _build(store, "ariadne", kinds=["bogus"])
    assert out["updates"] == []  # intersection with Phase-1 set is empty


def test_updates_sorted_by_seq_across_kinds():
    store = StubStore(projects=[
        _proj("PR-a", turn="ariadne", seq=15),
        _proj("PR-b", turn="ariadne", seq=9),
    ])
    store.append_dm("borges", "ariadne", "mid", seq=12, ts=FIXED_TS)
    out = _build(store, "ariadne")
    assert [u["seq"] for u in out["updates"]] == [9, 12, 15]


def test_agent_match_is_case_insensitive():
    store = StubStore(projects=[_proj("PR-1", turn="Ariadne", seq=10)])
    store.append_dm("borges", "Ariadne", "hi", seq=11, ts=FIXED_TS)
    out = _build(store, "ariadne")
    assert [u["kind"] for u in out["updates"]] == ["baton", "dm"]
