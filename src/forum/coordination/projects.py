"""Baton/board project write-fns — the relocated ``baton.py`` ``cmd_*`` write bodies.

Each fn is the PURE write body of a baton operation: read the project's markdown
via the store, transform it (frontmatter + turn-log via :mod:`coordination.markdown`),
and write it back through the store under the allocator lock, so seq-assignment and
the atomic write are co-atomic (fork-4). The CLI arg-parsing, participant/status
validation, GitHub-anchor resolution, and printing stay in ``tools/baton.py`` (and
later the HTTP route layer) — only the write body lives here, so every front-end
(CLI, web, the ``/api/updates`` feed) goes through the one writer (fork-4: no write
path bypasses the module).

This slice lands ``flip`` (the most-used op) and the shared pattern; ``claim`` and
``release`` follow the same shape. close / gc / init / reopen / rename / anchor /
merge follow in further slices.
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


def claim(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    claimer: str,
    pool_sentinel: str,
    ts: Optional[str] = None,
) -> int:
    """Claim a project baton from the pool; return the assigned seq.

    The pure write body of ``baton cmd_claim``: sets ``turn`` to the claimer,
    updates ``turn_since`` / ``turn_reason``, appends a pool-sentinel→claimer
    log entry.  CLI-layer validation (pool-sentinel check, participant check,
    closed-status check) is the caller's job — only the write body lives here.

    Args:
        claimer: agent claiming the baton (normalised to lowercase).
        pool_sentinel: the install's pool-sentinel name (resolved by the caller
            from ``config.json``'s ``primary_user`` field).
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
    self_name = claimer.strip().lower()
    sentinel = pool_sentinel.strip().lower()

    updated = md.update_frontmatter(
        content,
        {"turn": self_name, "turn_since": now, "turn_reason": '"claimed"'},
    )
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {sentinel} → {self_name}: claimed")
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq


def release(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    holder: str,
    pool_sentinel: str,
    reason: str,
    done: bool = False,
    ts: Optional[str] = None,
) -> int:
    """Release a project baton back to the pool; return the assigned seq.

    The pure write body of ``baton cmd_release``: sets ``turn`` to the pool
    sentinel, updates ``turn_since`` / ``turn_reason``, appends a holder→sentinel
    log entry.  With ``done=True`` appends ``" (done)"`` to the title (idempotent).
    CLI-layer validation (holder-must-be-current-turn, closed-status check) is
    the caller's job.

    Args:
        holder: the current turn holder (the one releasing).
        pool_sentinel: the install's pool-sentinel name (resolved by the caller).
        reason: the release reason / message.
        done: if True, mark the project title with ``" (done)"`` (idempotent).
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
    self_name = holder.strip().lower()
    sentinel = pool_sentinel.strip().lower()

    updates: dict = {
        "turn": sentinel,
        "turn_since": now,
        "turn_reason": f'"{reason}"',
    }

    title_renamed = False
    if done:
        fields, _ = md.parse_frontmatter(content)
        current_title = fields.get("title", "").strip()
        if not current_title.endswith("(done)"):
            updates["title"] = current_title + " (done)"
            title_renamed = True

    updated = md.update_frontmatter(content, updates)
    _, body = md.parse_frontmatter(updated)

    log_entry = f"- {now} {self_name} → {sentinel}: {reason}"
    if title_renamed:
        log_entry += f"\n- {now} title marked (done)"
    body = md.append_turn_log(body, log_entry)
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq
