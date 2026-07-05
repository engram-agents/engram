"""Tests for the coordination SeqAllocator (Slice A — the cursor core).

Covers:
1. Fresh allocator starts at 0; first allocate yields 1; sequential allocates
   are monotonic.
2. current() publishes only AFTER the write commits (returns _committed, not the
   in-flight assigned seq) — the §3 visibility contract that closes #1445.
3. recover() seeds the starting point; allocation resumes above it.
4. recover() returning a negative high-water-mark is a hard error.
5. recover=None defaults to a fresh 0 start.
6. An exception inside the allocate() body releases the lock (no deadlock), does
   NOT advance the committed watermark, and the consumed seq is a harmless hole.
7. Thread-safety: concurrent allocate() calls yield a unique, contiguous,
   monotonic set with no duplicates.
8. current() under concurrency is monotonic non-decreasing and ends at the max.
9. current() does NOT advance during an in-flight (uncommitted) write — the
   direct race the blocker named.
"""

import threading

import pytest

from forum.coordination.seq import SeqAllocator


# ---------------------------------------------------------------------------
# 1. Fresh start + monotonicity
# ---------------------------------------------------------------------------
def test_fresh_allocator_starts_at_zero_and_increments():
    alloc = SeqAllocator()
    assert alloc.current() == 0

    seqs = []
    for _ in range(5):
        with alloc.allocate() as seq:
            seqs.append(seq)

    assert seqs == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# 2. current() publishes only AFTER the write commits (the visibility contract)
# ---------------------------------------------------------------------------
def test_current_publishes_only_after_commit():
    alloc = SeqAllocator()
    with alloc.allocate() as seq:
        # publish-after-commit: inside the body seq is ASSIGNED but the write has
        # not committed, so current() still reports the PREVIOUS watermark (0).
        # If current() returned the assigned seq here it would publish as_of=1
        # before record 1 is readable — the #1445 silent-miss.
        assert seq == 1
        assert alloc.current() == 0
    # body returned cleanly → watermark advances to the committed seq.
    assert alloc.current() == 1
    with alloc.allocate():
        assert alloc.current() == 1  # still previous until this body commits
    assert alloc.current() == 2


# ---------------------------------------------------------------------------
# 3. recover() seeds the starting point
# ---------------------------------------------------------------------------
def test_recover_seeds_starting_point():
    alloc = SeqAllocator(recover=lambda: 42)
    assert alloc.current() == 42
    with alloc.allocate() as seq:
        assert seq == 43
    assert alloc.current() == 43


# ---------------------------------------------------------------------------
# 4. negative recovery is a hard error (would re-issue live seqs)
# ---------------------------------------------------------------------------
def test_negative_recovery_raises():
    with pytest.raises(ValueError):
        SeqAllocator(recover=lambda: -1)


@pytest.mark.parametrize("bad", [None, "5", 5.0, True, False])
def test_non_int_recovery_raises_typeerror(bad):
    # A buggy store impl returning the wrong type must fail loudly at construction
    # with a clear message, not crash cryptically on the first `< 0` comparison.
    # bool is excluded too (it is an int subclass but a True/False seq is a bug).
    with pytest.raises(TypeError):
        SeqAllocator(recover=lambda: bad)


# ---------------------------------------------------------------------------
# 5. recover=None → fresh 0
# ---------------------------------------------------------------------------
def test_recover_none_defaults_to_zero():
    alloc = SeqAllocator(recover=None)
    assert alloc.current() == 0


# ---------------------------------------------------------------------------
# 6. exception inside allocate() releases the lock; the hole is harmless
# ---------------------------------------------------------------------------
def test_exception_in_body_releases_lock_and_consumes_seq():
    alloc = SeqAllocator()

    with pytest.raises(RuntimeError):
        with alloc.allocate() as seq:
            assert seq == 1
            raise RuntimeError("simulated write failure")

    # The failed write never reached `self._committed = seq`, so the watermark
    # did NOT advance — current() stays 0, seq 1 is a harmless uncommitted hole.
    assert alloc.current() == 0

    # Lock must be released — a subsequent allocate must not deadlock.
    with alloc.allocate() as seq2:
        # seq 1 was consumed by the failed write; next is 2 (a hole at 1 is fine,
        # the feed contract depends only on monotonicity, not contiguity).
        assert seq2 == 2
    # Only the committed write (seq 2) publishes; watermark jumps 0→2 past the hole.
    assert alloc.current() == 2


# ---------------------------------------------------------------------------
# 7. thread-safety: concurrent allocation yields a unique contiguous set
# ---------------------------------------------------------------------------
def test_concurrent_allocation_is_unique_and_contiguous():
    alloc = SeqAllocator()
    n_threads = 8
    per_thread = 250
    results: list[int] = []
    results_lock = threading.Lock()
    start = threading.Barrier(n_threads)

    def worker():
        start.wait()  # maximize contention
        local = []
        for _ in range(per_thread):
            with alloc.allocate() as seq:
                local.append(seq)
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = n_threads * per_thread
    assert len(results) == total
    # No duplicates, and exactly the contiguous set {1..total}.
    assert len(set(results)) == total
    assert set(results) == set(range(1, total + 1))
    assert alloc.current() == total


# ---------------------------------------------------------------------------
# 8. current() never tears under concurrency (monotonic non-decreasing reads)
# ---------------------------------------------------------------------------
def test_current_is_monotonic_under_concurrency():
    alloc = SeqAllocator()
    stop = threading.Event()
    # Track only the running max + a sampled read count — NOT every spin-loop
    # read (an unbounded append in a tight loop would blow up memory).
    reader_state = {"max": 0, "reads": 0, "backwards": False}

    def reader():
        prev = 0
        reads = 0
        while not stop.is_set():
            cur = alloc.current()
            if cur < prev:  # must never go backwards
                reader_state["backwards"] = True
            prev = cur
            reads += 1
        reader_state["max"] = prev
        reader_state["reads"] = reads

    def writer():
        for _ in range(2000):
            with alloc.allocate():
                pass
        stop.set()

    r = threading.Thread(target=reader)
    w = threading.Thread(target=writer)
    r.start()
    w.start()
    w.join()
    r.join()

    assert alloc.current() == 2000
    assert reader_state["backwards"] is False  # monotonic non-decreasing reads
    assert reader_state["reads"] > 0  # the reader actually ran


# ---------------------------------------------------------------------------
# 9. current() does NOT advance during an in-flight (uncommitted) write
# ---------------------------------------------------------------------------
def test_current_does_not_advance_during_in_flight_write():
    # The direct race the blocker was about: a writer paused mid-body (seq
    # assigned, write not yet committed) must NOT let a concurrent current()
    # advance past the previous committed watermark — the visibility contract
    # that closes the #1445 silent-miss window. With the old `current()→_seq`
    # this assertion would read 2 mid-flight; with publish-after-commit it stays 1.
    alloc = SeqAllocator()
    with alloc.allocate():
        pass
    assert alloc.current() == 1  # baseline committed watermark

    in_body = threading.Event()
    release = threading.Event()
    observed = {}

    def writer():
        with alloc.allocate() as seq:
            observed["seq"] = seq      # assigned 2
            in_body.set()              # we are inside the body, pre-commit
            release.wait()             # hold the write open
        # `self._committed = 2` runs here, after release

    w = threading.Thread(target=writer)
    w.start()
    assert in_body.wait(timeout=5)
    # Writer holds the lock with seq 2 assigned but NOT yet committed.
    assert observed["seq"] == 2
    assert alloc.current() == 1        # must still be the PREVIOUS watermark
    release.set()
    w.join()
    assert alloc.current() == 2        # published only after the body committed
