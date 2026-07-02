"""DM channel module glue — the send/read/list thin layer over CoordinationStore."""

from datetime import datetime, timezone

from .store import CoordinationStore, DmMessage
from .seq import SeqAllocator


def dm_send(
    store: CoordinationStore,
    allocator: SeqAllocator,
    sender: str,
    recipient: str,
    body: str,
) -> DmMessage:
    """Send a DM from sender to recipient. seq is assigned by the allocator (fork-4)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with allocator.allocate() as seq:
        return store.append_dm(sender, recipient, body, seq=seq, ts=ts)


def dm_read(
    store: CoordinationStore,
    a: str,
    b: str,
    *,
    since_seq: int = 0,
) -> list[DmMessage]:
    """Read the thread between a and b (order-independent), messages with seq > since_seq."""
    return store.read_dm_thread(a, b, since_seq=since_seq)


def dm_list(
    store: CoordinationStore,
    agent: str,
) -> list[str]:
    """Return other-party names of every thread agent participates in."""
    return store.list_dm_threads(agent)
