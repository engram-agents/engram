"""Tests for the coordination store interface contract (Slice A — the seam).

Covers the pure, impl-independent parts of the interface that Slice D builds
against:
1. dm_thread_key is order-independent (a+b == b+a) and normalizes case/space.
2. CoordinationStore is abstract — cannot be instantiated, enforces the methods.
3. The record dataclasses are frozen and carry the documented fields.
"""

import pytest

from forum.coordination import (
    CoordinationStore,
    DmMessage,
    ProjectRecord,
    dm_thread_key,
)


# ---------------------------------------------------------------------------
# 1. dm_thread_key — canonical per-pair key
# ---------------------------------------------------------------------------
def test_dm_thread_key_is_order_independent():
    assert dm_thread_key("ariadne", "sol") == dm_thread_key("sol", "ariadne")


def test_dm_thread_key_format_and_normalization():
    assert dm_thread_key("Sol", "  Ariadne ") == "ariadne+sol"
    assert dm_thread_key("zeta", "alpha") == "alpha+zeta"


def test_dm_thread_key_self_thread():
    # A self-addressed thread is degenerate but must be stable, not crash.
    assert dm_thread_key("ariadne", "ariadne") == "ariadne+ariadne"


# ---------------------------------------------------------------------------
# 2. CoordinationStore is an abstract contract
# ---------------------------------------------------------------------------
def test_coordination_store_is_abstract():
    with pytest.raises(TypeError):
        CoordinationStore()  # type: ignore[abstract]


def test_partial_impl_still_abstract():
    # Implementing only some methods must NOT yield a concrete class.
    class Partial(CoordinationStore):
        def read_projects(self, *, active_only: bool = True):
            return []

    with pytest.raises(TypeError):
        Partial()  # type: ignore[abstract]


def test_full_impl_is_concrete():
    # A class overriding every abstract method instantiates cleanly — confirms
    # the abstract method set is exactly the documented interface.
    class Stub(CoordinationStore):
        def read_projects(self, *, active_only: bool = True):
            return []

        def read_project(self, project_id):
            return None

        def write_project(self, project_id, content, *, seq):
            pass

        def archive_project(self, project_id):
            pass

        def read_dm_thread(self, a, b, *, since_seq=0):
            return []

        def append_dm(self, sender, recipient, body, *, seq, ts):
            return DmMessage(seq, sender, recipient, body, ts)

        def list_dm_threads(self, agent):
            return []

        def list_all_dm_threads(self):
            return []

        def recover_max_seq(self):
            return 0

    store = Stub()
    assert store.recover_max_seq() == 0
    msg = store.append_dm("a", "b", "hi", seq=1, ts="2026-06-25T00:00:00Z")
    assert (msg.seq, msg.sender, msg.recipient, msg.body) == (1, "a", "b", "hi")


# ---------------------------------------------------------------------------
# 3. Record dataclasses
# ---------------------------------------------------------------------------
def test_dm_message_is_frozen():
    msg = DmMessage(seq=5, sender="a", recipient="b", body="x", ts="t")
    with pytest.raises(Exception):
        msg.seq = 6  # type: ignore[misc]


def test_project_record_fields():
    rec = ProjectRecord(
        project_id="PR-1",
        title="t",
        status="in-progress",
        turn="ariadne",
        turn_since="2026-06-25T00:00:00Z",
        turn_reason="r",
        participants=("ariadne", "borges"),
        seq=42,
        raw="---\n...\n---\n",
    )
    assert rec.seq == 42
    assert rec.participants == ("ariadne", "borges")
