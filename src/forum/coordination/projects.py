"""Baton/board project write-fns — the relocated ``baton.py`` ``cmd_*`` write bodies.

Each fn is the PURE write body of a baton operation: read the project's markdown
via the store, transform it (frontmatter + turn-log via :mod:`coordination.markdown`),
and write it back through the store under the allocator lock, so seq-assignment and
the atomic write are co-atomic (fork-4). The CLI arg-parsing, participant/status
validation, GitHub-anchor resolution, and printing stay in ``tools/baton.py`` (and
later the HTTP route layer) — only the write body lives here, so every front-end
(CLI, web, the ``/api/updates`` feed) goes through the one writer (fork-4: no write
path bypasses the module).

This slice lands ``flip`` (the most-used op) and the shared pattern; ``claim``,
``release``, and ``set_status`` follow the same shape. gc / merge follow in further slices.
"""

from __future__ import annotations

import re
from typing import Optional

from . import markdown as md
from .seq import SeqAllocator
from .store import CoordinationStore

# Participants list format: [alice, bob] or alice,bob — the same on-disk
# grammar tools/baton.py and store_file.py already parse. A third private copy
# here (rather than a cross-module import) mirrors the deliberate
# self-containment store_file.py's docstring already calls out for this exact
# grammar duplication.
_PARTICIPANTS_LIST_RE = re.compile(r"^\[?(.*?)\]?$")


class ProjectNotFound(Exception):
    """Raised when a write targets a ``project_id`` with no existing baton file.

    The CLI/route layer maps this to its own not-found error + exit code; the
    write-fn does not assume how the caller reports it.
    """


class ProjectAlreadyExists(Exception):
    """Raised by ``init`` when the target ``project_id`` already has a baton file.

    The CLI/route layer maps this to its own already-exists error + exit code;
    the write-fn does not assume how the caller reports it.
    """


class NotAParticipant(Exception):
    """Raised by ``add_participant`` when ``agent`` is not a current participant.

    Server-side authorization guard — LOAD-BEARING, unlike the validation in
    every other write-fn in this module (documented there as "the caller's
    job"). This check lives HERE, not in the CLI, because a CLI-only check is
    bypassable by a direct API call. The CLI/route layer maps this to its own
    not-authorized error + exit code (typically HTTP 403); the write-fn does
    not assume how the caller reports it.
    """


def _parse_participants(participants_str: str) -> list:
    """Parse ``[borges, ariadne]`` / ``borges,ariadne`` → lowercase name list.

    Mirrors ``store_file.FileStore._parse_participants`` / ``tools/baton.py``
    ``_parse_participants`` (the identical on-disk grammar, kept self-contained
    per the existing triplication convention rather than a cross-package import).
    """
    m = _PARTICIPANTS_LIST_RE.match(participants_str.strip())
    inner = m.group(1) if m else participants_str
    return [p.strip().lower() for p in inner.split(",") if p.strip()]


def _format_participants(participants: list) -> str:
    """Format a participants list for frontmatter: ``[borges, ariadne]``."""
    return "[" + ", ".join(participants) + "]"


def _coerce_seq(raw: str) -> int:
    """Parse a frontmatter ``seq:`` value to int; 0 when absent/garbage.

    Mirrors ``store_file._coerce_seq`` — used by ``add_participant`` to report
    the baton's last-committed seq on its idempotent no-op path (no new write
    happens, so no new seq is allocated).
    """
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return 0


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


def set_status(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    new_status: str,
    reason: str,
    caller: str = "set_status",
    ts: Optional[str] = None,
) -> int:
    """Set a project baton's ``status`` field; return the assigned seq.

    The pure write body of any status-transition operation (close, reopen, gc,
    merge): updates ``status`` in frontmatter, appends a turn-log entry, and
    writes atomically under the allocator lock (fork-4).  CLI-layer validation
    (is ``new_status`` a valid value? is the transition permitted?) is the
    caller's job — only the write body lives here.

    Args:
        new_status: the target status string (e.g. ``"merged"``, ``"in-review"``).
        reason: human-readable explanation logged in the turn-log entry.
        caller: operation label used in the log entry (e.g. ``"close"``,
            ``"reopen"``).  Defaults to ``"set_status"`` for bare calls.
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

    updated = md.update_frontmatter(content, {"status": new_status})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {caller}: {new_status} — {reason}")
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq


def close(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    new_status: str,
    reason: str,
    ts: Optional[str] = None,
) -> int:
    """Pure write body of baton cmd_close / gc. Validation (new_status ∈ CLOSED_STATUSES, not already closed) is the caller's job.

    Updates ``status`` in frontmatter (turn fields are NOT touched), appends a
    close turn-log entry, and writes atomically under the allocator lock (fork-4).

    Args:
        new_status: the target closed-status string (e.g. ``"merged"``, ``"abandoned"``).
        reason: human-readable explanation logged in the turn-log entry.
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

    updated = md.update_frontmatter(content, {"status": new_status})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} close: {new_status} — {reason}")
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq


def merge(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    merged_by: str,
    forced: bool = False,
    ts: Optional[str] = None,
) -> int:
    """Mark a project baton as merged and archive it; return the assigned seq.

    Pure write body of baton cmd_merge (the post-gh-merge FS-write only). The
    gh-orchestration (PR-state queries, CI/approval gates, the gh pr merge call)
    stays client-side in baton.py — only this post-merge write lives here.

    Log formats:
      - Normal: ``- {ts} merged via baton merge by {merged_by}``
      - Forced:  ``- {ts} merged via baton merge by {merged_by} (FORCED past gates 3-4)``

    OQ-4 archive relocation: after the seq-bearing write, calls
    ``store.archive_project(project_id)`` to move the baton from
    ``projects/<pid>.md`` to ``archive/<pid>.md``, keeping the active board clean.
    The archive move happens OUTSIDE the allocator lock (the lock covers only the
    seq-bearing write, per fork-4). If the process crashes between write and
    archive, the baton is in projects/ with status=merged — correct data, just
    not archived; the next run will see a closed project in the active dir (benign;
    ``read_projects(active_only=True)`` already filters by CLOSED_STATUSES).

    Args:
        merged_by: agent performing the merge (normalised to lowercase).
        forced: if True, append ``(FORCED past gates 3-4)`` to the log entry.
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
    merged_by_n = merged_by.strip().lower()

    if forced:
        log_line = (
            f"- {now} merged via baton merge by {merged_by_n} "
            "(FORCED past gates 3-4)"
        )
    else:
        log_line = f"- {now} merged via baton merge by {merged_by_n}"

    updated = md.update_frontmatter(content, {"status": "merged"})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, log_line)
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)

    # OQ-4 archive relocation (outside lock — seq-bearing write is complete)
    store.archive_project(project_id)
    return seq


def reopen(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    invoker: str,
    new_status: str,
    ts: Optional[str] = None,
) -> int:
    """Pure write body of baton cmd_reopen. Validation (new_status ∈ ACTIVE_STATUSES, current_status ∈ CLOSED_STATUSES) is the caller's job. Reads current status internally for the log entry.

    Updates ``status`` in frontmatter (turn fields are NOT touched), appends a
    reopened turn-log entry recording the prior status, and writes atomically
    under the allocator lock (fork-4).

    Args:
        invoker: agent performing the reopen (normalised to lowercase).
        new_status: the target active-status string (e.g. ``"in-progress"``).
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
    invoker_name = invoker.strip().lower()
    fields, _ = md.parse_frontmatter(content)
    current_status = fields.get("status", "")

    updated = md.update_frontmatter(content, {"status": new_status})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(
        body,
        f"- {now} reopened → {invoker_name}: status was {current_status} → {new_status}",
    )
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq


def rename(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    new_title: str,
    ts: Optional[str] = None,
) -> int:
    """Rename a project baton's title field; return the assigned seq.

    Pure write body of ``baton cmd_rename``: reads old title, updates ``title``
    in frontmatter, appends a turn-log entry recording the rename, and writes
    atomically under the allocator lock (fork-4). Validation (non-empty title,
    length limit, no newlines) is the caller's job.

    Log format: ``- {ts} renamed: title from "{old}" to "{new}"``

    Args:
        new_title: the new title string (caller should strip whitespace).
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
    old_title = fields.get("title", "").strip()

    updated = md.update_frontmatter(content, {"title": new_title})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f'- {now} renamed: title from "{old_title}" to "{new_title}"')
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq


def anchor(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    github_anchor: str,
    ts: Optional[str] = None,
) -> int:
    """Set or update the ``github:`` anchor on a project baton; return the assigned seq.

    Pure write body of ``baton cmd_anchor``: reads the current ``github:`` field
    (may be absent), updates (or adds) it in frontmatter, appends a turn-log
    entry that distinguishes new-set from update, and writes atomically under
    the allocator lock (fork-4). Anchor format validation (``pr/<N>`` or
    ``project/<N>``) is the caller's job.

    Log formats:
      - First set: ``- {ts} anchor set: github → {anchor}``
      - Update:    ``- {ts} anchor updated: github from "{old}" to "{new}"``

    Args:
        github_anchor: the anchor string (e.g. ``"pr/490"``).
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
    old_anchor = fields.get("github", "").strip()

    updated = md.update_frontmatter(content, {"github": github_anchor})
    _, body = md.parse_frontmatter(updated)
    if old_anchor:
        log_entry = f'- {now} anchor updated: github from "{old_anchor}" to "{github_anchor}"'
    else:
        log_entry = f"- {now} anchor set: github → {github_anchor}"
    body = md.append_turn_log(body, log_entry)
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq


def add_participant(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    agent: str,
    participant: str,
    ts: Optional[str] = None,
) -> tuple[int, bool]:
    """Add ``participant`` to a project baton's participants list; return ``(seq, added)``.

    Pure write body of ``baton cmd_add_participant``: reads the current
    ``participants`` list, appends ``participant`` (lowercased) if not already
    present, appends a turn-log entry documenting the addition, and writes
    atomically under the allocator lock (fork-4).

    Server-side authorization (LOAD-BEARING — unlike the other write-fns in
    this module, whose validation is documented as "the caller's job"):
    ``agent`` MUST already be a current participant of the baton. This check
    lives HERE, not in the CLI, because a CLI-only check is bypassable by a
    direct API call.

    Dedup / idempotency: if ``participant`` (compared case-insensitively) is
    already a participant, this is a no-op — no duplicate list entry, no
    turn-log line, no new seq allocated, no error. The baton's last-committed
    seq (parsed from its current ``seq:`` frontmatter field) is returned
    instead, with ``added=False``.

    Log format: ``- {ts} {agent} added participant: {participant}``

    Args:
        agent: the agent performing the add; must already be a participant.
        participant: the agent name to add (normalised to lowercase).
        ts: timestamp override (for tests / replay); defaults to ``now_iso()``.

    Returns:
        ``(seq, added)`` — ``seq`` is the seq of this mutation, or the
        baton's last-committed seq if this call was a no-op; ``added`` is
        ``True`` iff ``participant`` was newly added.

    Raises:
        ProjectNotFound: if ``project_id`` has no existing baton file.
        NotAParticipant: if ``agent`` is not a current participant of the baton.
    """
    content = store.read_project(project_id)
    if content is None:
        raise ProjectNotFound(project_id)

    fields, _ = md.parse_frontmatter(content)
    current_participants = _parse_participants(fields.get("participants", ""))

    agent_n = agent.strip().lower()
    if agent_n not in current_participants:
        raise NotAParticipant(agent_n)

    participant_n = participant.strip().lower()
    if participant_n in current_participants:
        return _coerce_seq(fields.get("seq", "")), False

    now = ts or md.now_iso()
    new_participants = list(current_participants) + [participant_n]
    participants_str = _format_participants(new_participants)

    updated = md.update_frontmatter(content, {"participants": participants_str})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {agent_n} added participant: {participant_n}")
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq, True


def init(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    title: str,
    status: str,
    turn: str,
    participants: list,
    turn_reason: str,
    github: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Create a new project baton from scratch; return the assigned seq.

    Pure write body of ``baton cmd_init``: builds the initial baton content
    (YAML frontmatter + markdown body with a turn-log section) and writes it
    atomically under the allocator lock (fork-4). Raises ``ProjectAlreadyExists``
    if the project already exists — callers must check before calling.
    All validation (project_id format, status validity, turn in participants,
    title constraints) is the caller's job.

    Frontmatter field order mirrors ``baton cmd_init``:
      project / title / status / turn / turn_since / turn_reason / participants / github (optional)

    Log format: ``- {ts} initialized → {turn}: {turn_reason}``

    Args:
        title: project title.
        status: initial status (e.g. ``"in-progress"``, ``"planning"``).
        turn: initial turn holder (lowercase, already validated by caller).
        participants: list of participant names (lowercase, already validated).
        turn_reason: human-readable reason for initial turn assignment.
        github: optional GitHub anchor (e.g. ``"pr/490"`` or ``"project/4"``).
        ts: timestamp override (for tests / replay); defaults to ``now_iso()``.

    Returns:
        The seq assigned to this mutation.

    Raises:
        ProjectAlreadyExists: if ``project_id`` already has a baton file.
    """
    existing = store.read_project(project_id)
    if existing is not None:
        raise ProjectAlreadyExists(project_id)

    now = ts or md.now_iso()
    participants_str = "[" + ", ".join(participants) + "]"

    fm_lines = [
        "---",
        f"project: {project_id}",
        f"title: {title}",
        f"status: {status}",
        f"turn: {turn}",
        f"turn_since: {now}",
        f'turn_reason: "{turn_reason}"',
        f"participants: {participants_str}",
    ]
    if github:
        fm_lines.append(f"github: {github}")
    fm_lines.append("---")
    fm_lines.append("")

    body_lines = [
        "",
        f"# {project_id} — {title}",
        "",
        "## Turn log",
        "",
        f"- {now} initialized → {turn}: {turn_reason}",
        "",
    ]

    content = "\n".join(fm_lines) + "\n".join(body_lines)

    with allocator.allocate() as seq:
        store.write_project(project_id, content, seq=seq)
    return seq
