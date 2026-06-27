"""Tests for the forum.coordination.dm module glue (no file I/O — Stub store)."""
import pytest
from forum.coordination import CoordinationStore, DmMessage, dm_thread_key
from forum.coordination.dm import dm_send, dm_read, dm_list
from forum.coordination.seq import SeqAllocator


class StubStore(CoordinationStore):
    def __init__(self):
        self._threads = {}

    def read_projects(self, *, active_only=True):
        return []

    def read_project(self, pid):
        return None

    def write_project(self, pid, content, *, seq):
        pass

    def archive_project(self, project_id):
        pass

    def read_dm_thread(self, a, b, *, since_seq=0):
        key = dm_thread_key(a, b)
        return [m for m in self._threads.get(key, []) if m.seq > since_seq]

    def append_dm(self, sender, recipient, body, *, seq, ts):
        key = dm_thread_key(sender, recipient)
        msg = DmMessage(seq, sender, recipient, body, ts)
        self._threads.setdefault(key, []).append(msg)
        return msg

    def list_dm_threads(self, agent):
        results = []
        for key in self._threads:
            parts = key.split("+")
            if agent in parts:
                other = next((p for p in parts if p != agent), parts[0])
                results.append(other)
        return results

    def list_all_dm_threads(self):
        pairs = []
        for key in self._threads:
            parts = key.split("+")
            if len(parts) == 2:
                pairs.append((parts[0], parts[1]))
        return sorted(pairs)

    def recover_max_seq(self):
        return 0


@pytest.fixture
def store():
    return StubStore()


@pytest.fixture
def allocator():
    return SeqAllocator()


def test_dm_send_returns_dm_message(store, allocator):
    msg = dm_send(store, allocator, "sol", "ariadne", "hello")
    assert isinstance(msg, DmMessage)
    assert msg.sender == "sol"
    assert msg.recipient == "ariadne"
    assert msg.body == "hello"
    assert msg.seq == 1


def test_dm_send_increments_seq(store, allocator):
    m1 = dm_send(store, allocator, "sol", "ariadne", "first")
    m2 = dm_send(store, allocator, "ariadne", "sol", "reply")
    assert m2.seq > m1.seq


def test_dm_read_returns_all_messages(store, allocator):
    dm_send(store, allocator, "sol", "ariadne", "a")
    dm_send(store, allocator, "ariadne", "sol", "b")
    msgs = dm_read(store, "sol", "ariadne")
    assert len(msgs) == 2


def test_dm_read_since_seq_filters(store, allocator):
    m1 = dm_send(store, allocator, "sol", "ariadne", "old")
    dm_send(store, allocator, "ariadne", "sol", "new")
    msgs = dm_read(store, "sol", "ariadne", since_seq=m1.seq)
    assert len(msgs) == 1
    assert msgs[0].body == "new"


def test_dm_read_order_independent(store, allocator):
    dm_send(store, allocator, "sol", "ariadne", "hi")
    assert dm_read(store, "ariadne", "sol") == dm_read(store, "sol", "ariadne")


def test_dm_list_returns_counterparts(store, allocator):
    dm_send(store, allocator, "sol", "ariadne", "hi")
    dm_send(store, allocator, "sol", "borges", "hello")
    counterparts = dm_list(store, "sol")
    assert set(counterparts) == {"ariadne", "borges"}


def test_dm_list_empty_for_new_agent(store):
    assert dm_list(store, "nobody") == []
