"""Baton/board project write-fns — the relocated ``baton.py`` ``cmd_*`` write bodies.

Each fn is the PURE write body of a baton operation: read the project's markdown
via the store, transform it (frontmatter + turn-log via :mod:`coordination.markdown`),
and write it back through the store under the allocator lock, so seq-assignment and
the atomic write are co-atomic (fork-4). The CLI arg-parsing, participant/status
validation, GitHub-anchor resolution, and printing stay in ``tools/baton.py`` (and
later the HTTP route layer) — only the write body lives here, so every front-end
(CLI, web, the ``/api/updates`` feed) goes through the one writer (fork-4: no write
path bypasses the module).

This slice lands ``flip`` (the most-used op) and the shared pattern; claim / release /
close / gc / init / reopen / rename / anchor / merge follow the same shape in
follow-up slices.
"""

from __future__ import annotations

from typing import Optional

from . import markdown as md
from .seq import SeqAllocator
from .store import CoordinationStore


class ProjectNotFound(Exception):
    """Raised when a write targets a ``project_id`` with no existing baton file.

    The CLI/route layer maps this to its own not-found error + exit code; the
    write-fn does not assume how the caller reports it.
    """


def flip(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    to_agent: str,
    reason: str,
    ts: Optional[str] = None,
) -> int:
    """Flip a baton's turn to ``to_agent``; return the module-assigned seq.

    The pure write body of ``baton cmd_flip``: reads the current ``turn`` (the
    ``from``), updates ``turn`` / ``turn_since`` / ``turn_reason``, appends a
    ``from → to`` turn-log line, and writes atomically under the allocator lock
    (fork-4 — :meth:`SeqAllocator.allocate` holds the lock across the write, and
    :meth:`CoordinationStore.write_project` embeds the passed-in ``seq``).

    Validation (is ``to_agent`` a participant, status/colleague gates) is the
    caller's job and is NOT performed here — this is the write body only.

    Args:
        ts: timestamp override (for tests / replay); defaults to ``now_iso()``.

    Returns:
        The seq assigned to this mutation.

    Raises:
        ProjectNotFound: if ``project_id`` has no existing baton file.
    """
    content = store.read_project(project_id)
    if content is None:
        raise ProjectNotFound(project_id)

    now = ts or md.now_iso()
    fields, _ = md.parse_frontmatter(content)
    # `from` is the baton's current holder; matches cmd_flip's
    # fields.get("turn", ...).strip().lower(). Fallback fires on a malformed baton
    # with no turn field OR an empty one (a valid baton always carries a real one);
    # `or "unknown"` yields "unknown" for both, friendlier than cmd_flip's "".
    from_agent = (fields.get("turn") or "unknown").strip().lower()
    to = to_agent.strip().lower()

    updated = md.update_frontmatter(
        content,
        {"turn": to, "turn_since": now, "turn_reason": f'"{reason}"'},
    )
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {from_agent} → {to}: {reason}")
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq
