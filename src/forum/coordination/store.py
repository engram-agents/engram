"""Store abstraction (fork-1) — the persistence layer behind the coordination module.

The store is an implementation detail behind the one writer. File-vs-DB is
reversible because every front-end (CLI, web, the `/api/updates` feed) talks to
the *module*, and the module talks to this interface — never to files directly.

This file defines the **interface contract** (`CoordinationStore`) plus the record
types and the on-disk DM format. The concrete `FileStore` impl lands in a stacked
PR; Slice D (the DM channel + `ia` thin-client) builds against THIS interface, not
against a file layout, so the two can proceed in parallel.

Resolution + portability (Lei's refinement): the file impl roots everything at
``$FORUM_HOME`` (falling back to the forum config), never a hardcoded
``/home/agents-shared/`` or username — so a second install works out of the box.

The seq contract (how this couples to ``seq.SeqAllocator``):
  Writes do NOT allocate their own seq. The module assigns the seq under the
  allocator lock and passes it in, so seq-assignment and the atomic-write are
  co-atomic (fork-4)::

      with allocator.allocate() as seq:
          store.append_dm(sender, recipient, body, seq=seq, ts=now)

  The store's only job re: seq is to **embed** the passed-in seq durably in the
  mutation (so ``recover_max_seq()`` can rebuild the high-water-mark on restart)
  and to expose it on reads (so the feed can filter ``seq > since``).

DM on-disk format (file impl — `$FORUM_HOME/dm/<key>.md`, OQ-2 per-pair thread):
  - Filename key is the two agent names sorted lexicographically and joined with
    ``+`` (e.g. ``ariadne+sol.md``) — see ``dm_thread_key``. One file per pair, so
    one atomic-write target + one clean cursor-bump per message; ``read_dm_thread``
    is a single-file read (bounded, no N-file inbox scan).
  - Append-only. Each message is a header line carrying a body LINE COUNT, then
    exactly that many body lines, verbatim::

        <!-- dm thread: ariadne+sol -->
        --- seq=42 from=ariadne to=sol at=2026-06-25T22:00:00Z lines=2 ---
        first message body,
        possibly multi-line
        --- seq=57 from=sol to=ariadne at=2026-06-25T22:05:11Z lines=1 ---
        reply body

  - The header carries seq + sender + recipient + ISO-8601 ts + ``lines`` (the
    body line count). ``seq`` is the durable cursor key; ``at`` is human-readable
    age only (NOT the cursor — §3).
  - **The reader consumes exactly ``lines`` body lines after each header — it does
    NOT split the file on the ``---`` delimiter.** This makes the body
    collision-proof by construction: a body line that itself looks like a header
    (``--- seq=… ---``) is just one of the N counted lines, never mis-parsed as a
    new message, so NO body escaping is needed. After a message's N body lines the
    reader scans forward to the next header, skipping blank separator lines.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from .names import InvalidAgentName, is_valid_agent_name


# ---------------------------------------------------------------------------
# Record types (transport-neutral; the same shapes back the file + DB impls)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DmMessage:
    """One direct message in a per-pair thread."""

    seq: int
    sender: str
    recipient: str
    body: str
    ts: str  # ISO-8601 UTC, human-readable age only — NOT the cursor key


@dataclass(frozen=True)
class ProjectRecord:
    """A baton/board item's parsed state (the fields the board + feed need)."""

    project_id: str
    title: str
    status: str
    turn: str
    turn_since: str
    turn_reason: str
    participants: tuple[str, ...]
    seq: int  # the seq of this item's last mutation (0 if pre-migration / unseen)
    raw: str  # the full markdown, for callers that re-write it
    github: str = ""  # optional anchor: "project/N" or "pr/N" (from frontmatter github: field)


def dm_thread_key(a: str, b: str) -> str:
    """Return the canonical per-pair thread key: the two names sorted + ``+``-joined.

    Order-independent so ``dm_thread_key("ariadne", "sol")`` and
    ``dm_thread_key("sol", "ariadne")`` resolve to the SAME thread file.
    Names are lowercased + stripped for stability.

    ``+`` is RESERVED in agent names (it is the pair-key separator):
    ``dm_thread_key("a+b", "c")`` and ``dm_thread_key("a", "b+c")`` would both
    key to ``a+b+c`` — a silent collision of two distinct pairs. So this function
    is the **authoritative guard** (#1468): it RAISES :class:`InvalidAgentName` if
    either name fails the allowlist, at the chokepoint every caller passes through
    (HTTP routes, the ``ia dm`` thin-client, reads and writes) — no caller can
    route around it. HTTP routes still pre-validate via ``is_valid_agent_name``
    for a clean 400; this raise is the backstop that makes the invariant
    un-bypassable.

    Raises:
        InvalidAgentName: if ``a`` or ``b`` (after strip+lowercase) fails the
            charset allowlist.
    """
    x, y = sorted((a.strip().lower(), b.strip().lower()))
    for _n in (x, y):
        if not is_valid_agent_name(_n):
            raise InvalidAgentName(f"invalid agent name: {_n!r}")
    return f"{x}+{y}"


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------
class CoordinationStore(abc.ABC):
    """Persistence interface for the coordination module (the one writer).

    All writes are atomic (tmp + rename) and embed the module-assigned ``seq``.
    All reads expose ``seq`` so the feed can filter ``seq > since``. No method
    allocates a seq itself — the module does that under the allocator lock and
    passes it in (fork-4 co-atomicity).
    """

    # --- baton / board (projects) ------------------------------------------
    @abc.abstractmethod
    def read_projects(self, *, active_only: bool = True) -> list[ProjectRecord]:
        """Return all project records (optionally only non-closed ones)."""

    @abc.abstractmethod
    def read_project(self, project_id: str) -> Optional[str]:
        """Return a project's raw markdown, or ``None`` if it does not exist."""

    @abc.abstractmethod
    def write_project(self, project_id: str, content: str, *, seq: int) -> None:
        """Atomically write a project's full markdown, embedding ``seq``.

        ``seq`` is the module-assigned sequence for this mutation (assigned under
        the allocator lock; this call runs inside that lock). The impl embeds it
        durably (e.g. a ``seq:`` frontmatter field) so ``recover_max_seq()`` sees
        it after a restart.
        """

    @abc.abstractmethod
    def archive_project(self, project_id: str) -> None:
        """Move a merged baton from ``projects/<pid>.md`` to ``archive/<pid>.md``.

        Called by the ``merge`` writer-fn after the seq-bearing write to keep the
        active board clean. Idempotent: no-op if the project file is not found
        (already archived or never written). Must not raise on missing project.

        The ``archive/`` directory is under the same root as ``projects/`` so the
        rename is cross-dir but same-filesystem — ``Path.rename`` is atomic on POSIX.
        The archived file retains its ``seq:`` embedding so ``recover_max_seq`` can
        count it (``FileStore.recover_max_seq`` MUST scan both ``projects/`` and
        ``archive/``).
        """

    # --- DM ----------------------------------------------------------------
    @abc.abstractmethod
    def read_dm_thread(
        self, a: str, b: str, *, since_seq: int = 0
    ) -> list[DmMessage]:
        """Return messages in the ``a``↔``b`` thread with ``seq > since_seq``.

        Order-independent in ``a``/``b`` (see ``dm_thread_key``). Returns ``[]``
        if the thread does not exist. Messages are returned in seq order.
        """

    @abc.abstractmethod
    def append_dm(
        self, sender: str, recipient: str, body: str, *, seq: int, ts: str
    ) -> DmMessage:
        """Atomically append one message to the ``sender``↔``recipient`` thread.

        Embeds the module-assigned ``seq``. Creates the thread file on first
        message. Returns the persisted :class:`DmMessage`. The 1:1 ACL is NOT
        enforced here — it is enforced at the API layer, where ``sender`` is the
        authenticated agent (never a client-supplied field). The store trusts its
        caller (the module) to have authorized the write.
        """

    @abc.abstractmethod
    def list_dm_threads(self, agent: str) -> list[str]:
        """Return the other-party names of every thread ``agent`` participates in.

        Backs ``ia read`` with no counterparty (the unread-across-threads view).
        """

    @abc.abstractmethod
    def list_all_dm_threads(self) -> list[tuple[str, str]]:
        """Return every DM pair as sorted ``(a, b)`` tuples — operator-view, all pairs.

        Unlike :meth:`list_dm_threads` (agent-scoped), this returns ALL pairs in the
        store regardless of which agent is requesting. Each tuple is the canonical
        sorted pair produced by :func:`dm_thread_key` — ``a < b`` lexicographically.
        Returns ``[]`` if the dm directory does not exist or is empty.

        Backs the operator oversight view (``GET /dm``) where the human operator
        needs full visibility across all agent conversations for debugging and
        transparency. This method MUST NOT be exposed to agent-facing API routes
        (those remain 1:1-ACL-scoped via :meth:`list_dm_threads`).
        """

    # --- cursor recovery ---------------------------------------------------
    @abc.abstractmethod
    def recover_max_seq(self) -> int:
        """Return ``max(seq)`` across the ENTIRE store (projects + DMs).

        Called once at startup to seed :class:`~forum.coordination.seq.SeqAllocator`
        so the timeline resumes above every seq already written. Returns ``0`` for
        a fresh/empty store. Must never return a value below a seq present on disk
        (that would re-issue a live seq — the allocator guards against negatives,
        but under-counting is the impl's responsibility to avoid).

        ``seq`` is embedded by ``write_project`` (a ``seq:`` frontmatter field) and
        by ``append_dm`` (the ``seq=`` header field) — this method MUST scan BOTH
        surfaces and return the global max. Missing either undercounts and silently
        re-issues live seqs. See those two methods' docstrings for the exact embed
        locations.
        """
