"""Process-level monotonic sequence allocator — the cursor core (§3).

The unified coordination feed (``GET /api/updates?since=<seq>``) is keyed on a
**server-assigned monotonic sequence**, NOT raw mtime. This module owns that
sequence.

Why a process-level allocator (not a per-store counter): the forum is the single
writer of record for everything it commits — baton/board, DMs, and (later) forum
posts/mentions. One ``SeqAllocator`` per forum process stamps every mutation with
the next ``seq`` under one lock, so the timeline is global across all of them.
Because the allocator is a property of the *process*, not of any one store, the
file→DB store swap (fork-1) leaves it untouched, and folding in forum mentions
later is purely additive (give the post-create path the same allocator).

Why ``seq`` beats raw mtime at unified scale (spec §3):
  1. Collision-free by construction — a strictly-increasing integer cannot
     collide, even when DM + baton + board writes land in the same mtime tick.
  2. FS-impl-independent — survives the file→DB swap; the cursor stops caring
     what backs the store.
  3. Dissolves the capture-before-read window — ``since > N`` is gap-free by
     definition: a mutation committing mid-read gets a ``seq`` above the read's
     ``as_of`` and is simply caught on the next poll.

Fork-4 (the load-bearing invariant): seq-assignment and the write that uses it
must be **co-atomic** — the seq is assigned under the same lock that is held
across the write, so the cursor key *is* the write-completion stamp, never a
client-supplied logical time. ``allocate()`` enforces this: it yields the new
seq while still holding the lock, and the caller performs its atomic-write
inside the ``with`` block. A seq handed out but never committed (write raised)
simply leaves an unused integer — harmless, since the feed contract depends only
on monotonicity, not on a dense/contiguous sequence.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


class SeqAllocator:
    """A thread-safe, monotonically increasing sequence source.

    One instance per forum process. Recovers its high-water-mark at startup so
    the timeline resumes correctly across restarts (the persisted store is the
    source of truth; the in-memory counter is the live allocator).
    """

    def __init__(self, recover: Optional[Callable[[], int]] = None) -> None:
        """Create the allocator.

        Args:
            recover: Zero-arg callable returning the current high-water-mark —
                ``max(seq)`` across the persisted store — so the counter resumes
                above every seq already written. Called once, now, under no lock
                (construction is single-threaded). Defaults to a fresh ``0``
                start (no prior store) when omitted. A ``recover`` that raises is
                a hard error: starting below the persisted max would re-issue
                live seqs and corrupt the cursor, so we must not silently fall
                back to ``0``.
        """
        self._lock = threading.Lock()
        if recover is None:
            start = 0
        else:
            recovered = recover()
            # Type-guard first: a buggy store impl returning None (or a str, or a
            # bool) would otherwise surface as a cryptic "'<' not supported" crash
            # at forum startup, far from the real cause. (bool is an int subclass,
            # so exclude it explicitly — a True/False high-water-mark is a bug.)
            if not isinstance(recovered, int) or isinstance(recovered, bool):
                raise TypeError(
                    "SeqAllocator recover() must return an int, got "
                    f"{type(recovered).__name__}."
                )
            if recovered < 0:
                raise ValueError(
                    f"SeqAllocator recover() returned {recovered}; "
                    "the high-water-mark cannot be negative."
                )
            start = recovered
        # ``_seq`` is the ASSIGNMENT counter (bumped at allocate-start); ``_committed``
        # is the PUBLISHED watermark (bumped only after the caller's write commits).
        # current() returns _committed, so as_of never names an in-flight write —
        # see current()/allocate() for the visibility contract. At startup every
        # recovered seq is already committed on disk, so they begin equal.
        self._seq = start
        self._committed = start

    def current(self) -> int:
        """Return the published high-water-mark — the latest **committed** seq.

        This is the ``as_of`` value a read serves up to: the feed returns
        mutations with ``seq > since`` and echoes ``as_of = current()`` captured
        at read-start, which the client persists as its next ``since``.

        **Returns ``_committed``, NOT ``_seq``** — the watermark of seqs whose
        write has finished, not of seqs merely *assigned*. This is the visibility
        contract (§3): ``allocate()`` bumps ``_seq`` at the START of its block, so
        between assignment and the caller's write committing there is a window
        where seq N is assigned but record N is not yet readable. If ``current()``
        returned ``_seq`` it would publish ``as_of = N`` during that window; a
        reader echoing ``since = N`` would then never see record N (it is not
        ``> N``) — the #1445 silent-miss, reincarnated as a pre-commit watermark.
        Returning ``_committed`` closes it: an in-flight write commits at
        ``_committed + 1 > as_of`` and is re-fired on the next poll
        (safe-duplicate > silent-miss).

        Deliberately **lock-free**. ``_committed`` is mutated only by the single
        ``self._committed = seq`` at the end of an ``allocate()`` block, under the
        lock — so a concurrent reader sees either the pre- or post-bump value,
        never a torn one, and a momentarily-stale (behind, never ahead) read is
        safe by the contract above. Lock-free also lets ``current()`` be called
        from *within* an ``allocate()`` body without deadlocking the non-reentrant
        lock (there it reads the PREVIOUS committed value — the in-flight seq is
        deliberately not yet published).

        Holds on free-threaded (no-GIL) CPython 3.13+ too: under PEP 703 an
        attribute load/store goes through a per-object critical section, so the
        read still observes a complete pre- or post-bump integer, never torn.
        """
        return self._committed

    @contextmanager
    def allocate(self) -> Iterator[int]:
        """Allocate the next seq and hold the lock across the caller's write.

        Usage (fork-4 — seq and write are co-atomic)::

            with allocator.allocate() as seq:
                content = embed_seq(template, seq)
                atomic_write(path, content)   # runs while the lock is held
            # lock released here; `seq` is now the new high-water-mark

        The lock is held for the entire ``with`` body, serializing all writers
        through this allocator. Keep the body to the **single ``os.replace``
        atomic-write** that consumes the seq (sub-millisecond) — do not do
        unbounded work (network, gh calls) inside it, or you serialize the whole
        coordination surface behind it.

        **Publish-after-commit:** ``_seq`` is bumped *before* the yield (assigning
        the seq the caller embeds in its write); ``_committed`` is bumped *after*
        the yield returns — i.e. only once the caller's write has finished without
        raising. So ``current()`` (which returns ``_committed``) never names a seq
        whose record isn't yet readable (the visibility contract — see
        ``current()``). The lock serializes writers and commits land in seq order,
        so ``_committed`` is always the contiguous high-water of fully-written
        records. If the write raises, the ``self._committed = seq`` line is skipped
        — the assigned seq becomes a harmless hole (monotonicity, not density, is
        what the feed contract needs), and the watermark stays at the last record
        that actually committed.

        Yields:
            The newly assigned sequence number (``current() + 1`` at entry).
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
            yield seq
            # Reached only if the caller's write (the with-body) returned without
            # raising — publish the watermark now that record `seq` is readable.
            self._committed = seq
