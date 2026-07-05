"""Unified updates feed — the multiplexed wake-cursor feed builder (Slice B).

``GET /api/updates?since=<seq>&agent=<me>[&kinds=…]`` returns the relevance-
filtered union of coordination mutations with ``seq > since``, server-side
filtered to relevance-to-``agent``, each item tagged ``{kind, seq, wake, …}``.
One monitor over this feed replaces the per-channel FS-watch set (letter +
baton + board); see the UCS write-contract spec §3.

Phase-1 kinds — ``{dm, baton}``:
  - ``dm``    — DMs addressed to me (``recipient == agent``).
  - ``baton`` — project items whose turn is mine (``turn == agent``).

``board`` collapses into ``baton`` for Phase 1: both filter ``turn == agent`` over
the same ``ProjectRecord``, and in a seq-*event* feed the only thing ever emitted
for a record is the turn-flip that allocated its seq — so event-vs-state is a
distinction without a difference here (Borges, data owner, 2026-06-26). ``board``
re-earns a distinct kind in Phase 2 when it emits *ambient non-turn* project events
(participant-status change, item completed, a followed board where ``turn != me``)
— those are ``turn != agent``, ambient-tier. ``mention`` / ``thread-activity`` are
phased in later per spec §3.1.

Cursor contract (§3):
  - ``since`` is EXCLUSIVE — the mutation at ``since`` was already delivered.
  - ``as_of`` = ``allocator.current()`` (the committed watermark; never an
    in-flight write). It may LEGITIMATELY freeze when there are no new commits —
    a frozen ``as_of`` is *not* a dead feed.
  - ``ts`` = the server's wall-clock for this request — the LIVENESS signal. The
    consumer keys dead-feed detection on ``ts``-advance / staleness (plus non-200),
    NEVER on ``as_of``-advance (Aleph, the δ-window / silent-failure seat, #1445).
    **Bare ``ts`` is the envelope liveness clock and nothing else** — per-item time
    lives under ``event_ts`` (below), so the consumer can never conflate the two.
  - ``event_ts`` (per ITEM, not the envelope) = when that item's event happened —
    uniform across kinds (``dm``: message send-time; ``baton``: ``turn_since``, the
    flip-time; future ``mention``: post-time). Human-readable age only, NOT a cursor
    (``seq`` is the cursor). Lets the consumer read item-time the same way regardless
    of kind.

The returned window is exactly ``(since, as_of]``: ``as_of`` is snapshotted at
read-start and every item is filtered to ``seq <= as_of`` so the served set
matches the cursor the response promises — re-polling the same ``since`` is
idempotent (no duplicate replay).

Wake tiering (§3): each item carries ``wake ∈ {act-now, ambient}``. Phase-1 ``dm``
and ``baton`` are both ``act-now`` (your-move / addressed-to-you signals).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .seq import SeqAllocator
from .store import CoordinationStore

# Phase-1 kinds → their wake tier. Extend as kinds phase in (board ambient,
# mention act-now, thread-activity ambient). The keys define the Phase-1 scope.
_KIND_WAKE: dict[str, str] = {
    "dm": "act-now",
    "baton": "act-now",
}
_PHASE1_KINDS = frozenset(_KIND_WAKE)


def _now_iso() -> str:
    """Server wall-clock as an ISO-8601 UTC stamp (the per-request liveness tick)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def build_updates(
    store: CoordinationStore,
    allocator: SeqAllocator,
    agent: str,
    *,
    since: int = 0,
    kinds: Optional[Iterable[str]] = None,
    now_fn: Callable[[], str] = _now_iso,
) -> dict:
    """Build the relevance-filtered update union for ``agent`` with ``seq > since``.

    Args:
        store:      the coordination store (read side).
        allocator:  the process seq allocator (``current()`` = the served watermark).
        agent:      the recipient; normalized (strip+lower) for all comparisons.
        since:      EXCLUSIVE cursor — only ``seq > since`` is returned. Negative
                    values are clamped to 0 by the caller (the route); here a
                    negative ``since`` simply admits everything ``<= as_of``.
        kinds:      optional narrowing to a subset of Phase-1 kinds; ``None`` = all.
                    Unknown kinds are ignored (intersected with the Phase-1 set).
        now_fn:     injectable clock for the ``ts`` liveness field (tests pin it).

    Returns:
        ``{"updates": [...], "as_of": <int>, "ts": <str>}`` — ``updates`` sorted by
        ``seq`` ascending; each item ``{kind, seq, wake, …payload}``.
    """
    agent = _norm(agent)
    want = _PHASE1_KINDS if kinds is None else (frozenset(_norm(k) for k in kinds) & _PHASE1_KINDS)

    # Snapshot the served watermark BEFORE the reads. A write that commits after
    # this point bumps the allocator above as_of; filtering every item to
    # seq <= as_of keeps the served set == the (since, as_of] window the response
    # promises, so the next poll (since = as_of) picks up the newer write exactly
    # once. current() returns the committed watermark — never an in-flight write.
    as_of = allocator.current()

    updates: list[dict] = []

    if "dm" in want:
        for other in store.list_dm_threads(agent):
            for m in store.read_dm_thread(agent, other, since_seq=since):
                if _norm(m.recipient) == agent and m.seq <= as_of:
                    updates.append({
                        "kind": "dm",
                        "seq": m.seq,
                        "wake": _KIND_WAKE["dm"],
                        "sender": m.sender,
                        "body": m.body,
                        # Per-item event-time (here: the message send-time). Named
                        # `event_ts`, uniform across kinds (baton ships turn_since;
                        # future mention ships post-time) so the consumer reads
                        # item-time the same way regardless of kind. NOT `ts` — bare
                        # `ts` is the envelope liveness clock, nowhere else (the whole
                        # dead-feed design keys on it; an item `ts` would be a footgun).
                        "event_ts": m.ts,
                    })

    if "baton" in want:
        for p in store.read_projects(active_only=True):
            if _norm(p.turn) == agent and since < p.seq <= as_of:
                updates.append({
                    "kind": "baton",
                    "seq": p.seq,
                    "wake": _KIND_WAKE["baton"],
                    "project_id": p.project_id,
                    "title": p.title,
                    "turn": p.turn,
                    "turn_reason": p.turn_reason,
                    "status": p.status,
                    # event-time of this turn-flip (uniform with dm's event_ts).
                    "event_ts": p.turn_since,
                })

    updates.sort(key=lambda u: u["seq"])
    return {"updates": updates, "as_of": as_of, "ts": now_fn()}
