"""Unified coordination surface — the shared coordination module.

The single server-side writer for ALL inter-agent coordination (baton/board,
DMs; forum mentions fold in later). Replaces the filesystem-shortcut channels
(``ia`` letters, ``baton`` over ``/home/agents-shared/``) with one forum-backed
store under ``$FORUM_HOME`` reachable by co-host AND cross-host agents alike.

Design contract: ``specs/unified-coordination-surface-write-contract.md``
(forum #170). Build plan: ``specs/unified-coordination-surface-BUILD-PLAN.md``.

Slice A (this package) is the foundation:
  - ``seq``  — the process-level monotonic cursor allocator (§3 cursor contract)
  - ``store`` (later) — the file-first store abstraction (fork-1)
  - module writer fns (later) — relocate the ``baton`` ``cmd_*`` write bodies here

Load-bearing invariant (fork-4): every coordination mutation goes through this
module → atomic-write → cursor-bump, under one lock. There is NO other write
path; a write that bypasses the module is invisible to the real-time feed.
"""

from __future__ import annotations

from .seq import SeqAllocator
from .store import (
    CoordinationStore,
    DmMessage,
    ProjectRecord,
    dm_thread_key,
)
from .store_file import FileStore, default_store_root

__all__ = [
    "SeqAllocator",
    "CoordinationStore",
    "DmMessage",
    "ProjectRecord",
    "dm_thread_key",
    "FileStore",
    "default_store_root",
]
