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


def _maybe_stamp_phase(fields: dict, phase: Optional[str], now: str) -> tuple[dict, str]:
    """If `phase` is given and this baton has no explicit phase yet, return
    ({"phase": phase}, log_suffix) to fold into this write's frontmatter
    updates and turn-log line. Otherwise return ({}, "") — a no-op.

    Calling this repeatedly/sequentially on the same baton is idempotent: a
    second call sees the first call's already-committed stamp (via its own
    fresh `store.read_project` read) and no-ops. This does NOT protect
    against genuinely overlapping concurrent callers — `read_project`
    happens before the allocator lock is acquired in every write-body, so
    two truly concurrent writers can each decide to stamp from stale reads;
    the second writer's full-document write then clobbers the first's, the
    same pre-existing lost-update exposure every other field in this store
    already has. See issue #3255 for tracking genuine concurrent-write
    safety across `projects.py`.
    """
    if not phase:
        return {}, ""
    existing = (fields.get("phase") or "").strip()
    if existing:
        return {}, ""
    return (
        {"phase": phase},
        f"\n- {now} phase stamped: {phase!r} (legacy conversion, #3039 U2a)",
    )


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
    """Raised when ``agent`` lacks the standing a write-fn requires.

    ``add_participant`` raises it when ``agent`` is not a current participant.
    ``set_colleague`` (#2764) raises it under the slightly wider test "neither
    a current participant NOR the current turn holder" — each write-fn's own
    docstring states its exact test; this exception is the shared signal, not
    the shared authorization rule.

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
    by_agent: str = "",
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Flip a baton's turn to ``to_agent``; return the module-assigned seq.

    The pure write body of ``baton cmd_flip``: reads the current ``turn`` (the
    ``from``), updates ``turn`` / ``turn_since`` / ``turn_reason`` / ``turn_by``,
    appends a ``from → to`` turn-log line, and writes atomically under the
    allocator lock (fork-4 — :meth:`SeqAllocator.allocate` holds the lock across
    the write, and :meth:`CoordinationStore.write_project` embeds the passed-in
    ``seq``).

    Validation (is ``to_agent`` a participant, status/colleague gates) is the
    caller's job and is NOT performed here — this is the write body only.

    Args:
        by_agent: the acting agent performing this flip (normalised to
            lowercase; may differ from ``to_agent`` — a third party can flip a
            baton onto someone else). Defaults to ``""`` (actor unknown) for
            back-compat with pre-existing callers; an empty ``turn_by`` is the
            safe default the updates feed never treats as a self-flip.
        phase: the caller-computed CONVERSION phase for this flip -- the
            legacy-conversion phase to stamp on a still-legacy baton via
            stamp-if-absent (see :func:`_maybe_stamp_phase`, #3039 U2a);
            ``None`` (default) leaves an existing/absent ``phase:`` field
            untouched. A transition flip sends this TOGETHER with
            ``transition_phase`` (round 4) -- see that parameter's docstring.
        transition_phase: the caller-computed TRANSITION phase this flip
            defines (#3039 U2a round 3) -- when given, unconditionally
            overwrites ``phase:`` to this value, AFTER the conversion step
            (``phase`` above) has run, regardless of what it did. Set by the
            caller only for the two flip branches that actually define a
            phase transition (fairy->peer, maintainer-flip-to-merge-seat),
            which send BOTH fields (round 4): on a legacy baton the turn log
            then carries the conversion line followed by the transition
            line; on a baton with an explicit ``phase:`` only the transition
            line. Every other flip leaves this ``None`` and sends ``phase``
            (conversion-only) alone. ``None`` (default) means this flip is
            not a transition.
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
    turn_by = (by_agent or "").strip().lower()
    # Step 1 (conversion, unchanged _maybe_stamp_phase logic) then Step 2
    # (transition, NEW #3039 U2a round 3): transition unconditionally
    # overwrites whatever step 1 just did, regardless of its outcome -- see
    # the design note's two-step architecture (Kepler's U2a-round-3 ruling).
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updated = md.update_frontmatter(
        content,
        # turn_by is quoted like turn_reason (not bare like turn/turn_since):
        # unlike turn, turn_by can legitimately be "" (actor unknown), and an
        # UNQUOTED empty value written mid-block corrupts the frontmatter
        # parser — its `\s*` separator is greedy across the newline and eats
        # the NEXT field's entire line as this field's "value" (verified: an
        # empty turn_by followed by, e.g., `participants:` silently absorbs
        # it). Quoting keeps a non-whitespace char right after the separator,
        # so the parser never crosses the line boundary. Unquoted on read by
        # store_file.read_projects, mirroring its existing turn_reason unquote.
        {
            "turn": to, "turn_since": now, "turn_reason": f'"{reason}"',
            "turn_by": f'"{turn_by}"', **phase_updates,
        },
    )
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {from_agent} → {to}: {reason}" + phase_log_suffix)
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Claim a project baton from the pool; return the assigned seq.

    The pure write body of ``baton cmd_claim``: sets ``turn`` to the claimer,
    updates ``turn_since`` / ``turn_reason`` / ``turn_by``, appends a
    pool-sentinel→claimer log entry.  CLI-layer validation (pool-sentinel
    check, participant check, closed-status check) is the caller's job — only
    the write body lives here.

    The actor IS the claimer (``turn_by`` is set to ``claimer``, the same
    normalised name that becomes ``turn``) — a claim is always self-authored.

    Args:
        claimer: agent claiming the baton (normalised to lowercase).
        pool_sentinel: the install's pool-sentinel name (resolved by the caller
            from ``config.json``'s ``primary_user`` field).
        phase: the caller-computed CONVERSION phase for this claim -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this claim
            defines. ``claim`` is ALWAYS a transition (result phase
            ``"claimed"``) -- callers pass this ALONGSIDE ``phase`` (round
            4): conversion runs first, the transition then overwrites.
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
    fields, _ = md.parse_frontmatter(content)
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updated = md.update_frontmatter(
        content,
        # turn_by quoted like turn_reason — see flip()'s comment for why (the
        # empty-value parser-corruption case doesn't arise here since
        # self_name is never empty, but quoting is applied uniformly across
        # every turn_by write site so the on-disk format is consistent).
        {
            "turn": self_name, "turn_since": now, "turn_reason": '"claimed"',
            "turn_by": f'"{self_name}"', **phase_updates,
        },
    )
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {sentinel} → {self_name}: claimed" + phase_log_suffix)
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Release a project baton back to the pool; return the assigned seq.

    The pure write body of ``baton cmd_release``: sets ``turn`` to the pool
    sentinel, updates ``turn_since`` / ``turn_reason`` / ``turn_by``, appends a
    holder→sentinel log entry.  With ``done=True`` appends ``" (done)"`` to the
    title (idempotent). CLI-layer validation (holder-must-be-current-turn,
    closed-status check) is the caller's job.

    The actor IS the holder releasing (``turn_by`` is set to ``holder``), even
    though ``turn`` itself becomes the pool sentinel — the release won't
    self-notify anyone (the sentinel is never a real agent's ``turn``), but the
    actor is stamped for audit-trail consistency with the other write-fns.

    Args:
        holder: the current turn holder (the one releasing).
        pool_sentinel: the install's pool-sentinel name (resolved by the caller).
        reason: the release reason / message.
        done: if True, mark the project title with ``" (done)"`` (idempotent).
        phase: the caller-computed CONVERSION phase for this release -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this release
            defines. ``release`` is ALWAYS a transition (result phase
            ``"pool"``, or ``"done"`` with ``done=True``) -- callers pass
            this ALONGSIDE ``phase`` (round 4): conversion runs first, the
            transition then overwrites.
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
    # Parsed unconditionally (not just inside `if done:` as before #3039 U2a)
    # so _maybe_stamp_phase always has this write's freshly-read fields to
    # check the current `phase:` value against, regardless of whether --done
    # was passed.
    fields, _ = md.parse_frontmatter(content)
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updates: dict = {
        "turn": sentinel,
        "turn_since": now,
        "turn_reason": f'"{reason}"',
        # quoted like turn_reason — see flip()'s comment for why.
        "turn_by": f'"{self_name}"',
        **phase_updates,
    }

    title_renamed = False
    if done:
        current_title = fields.get("title", "").strip()
        if not current_title.endswith("(done)"):
            updates["title"] = current_title + " (done)"
            title_renamed = True

    updated = md.update_frontmatter(content, updates)
    _, body = md.parse_frontmatter(updated)

    log_entry = f"- {now} {self_name} → {sentinel}: {reason}"
    if title_renamed:
        log_entry += f"\n- {now} title marked (done)"
    log_entry += phase_log_suffix
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Set a project baton's ``status`` field; return the assigned seq.

    The pure write body of any status-transition operation (close, reopen, gc,
    merge): updates ``status`` in frontmatter, appends a turn-log entry, and
    writes atomically under the allocator lock (fork-4).  CLI-layer validation
    (is ``new_status`` a valid value? is the transition permitted?) is the
    caller's job — only the write body lives here.

    Per Kepler's #3039 U2a ruling, ``set_status`` writes NO transition phase
    of its own (conversion only) -- it has no current caller anywhere in
    server.py or baton.py (wired here for completeness / to prevent future
    drift, per the ruling's "all twelve write bodies" scope).

    Args:
        new_status: the target status string (e.g. ``"merged"``, ``"in-review"``).
        reason: human-readable explanation logged in the turn-log entry.
        caller: operation label used in the log entry (e.g. ``"close"``,
            ``"reopen"``).  Defaults to ``"set_status"`` for bare calls.
        phase: the caller-computed CONVERSION phase for this call -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this call
            defines, if any -- per Kepler's ruling, no caller should pass
            this for ``set_status`` (it defines no transition).
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
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updated = md.update_frontmatter(content, {"status": new_status, **phase_updates})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {caller}: {new_status} — {reason}" + phase_log_suffix)
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Pure write body of baton cmd_close / gc. Validation (new_status ∈ CLOSED_STATUSES, not already closed) is the caller's job.

    Updates ``status`` in frontmatter (turn fields are NOT touched), appends a
    close turn-log entry, and writes atomically under the allocator lock (fork-4).

    Per Kepler's #3039 U2a ruling, ``close`` writes NO transition phase of
    its own (conversion only) -- terminal states live in ``status``
    (``merged``, ``cancelled``), never in ``phase``. A closed baton stays at
    whatever ``phase`` it was last at, with ``status: merged`` /
    ``status: cancelled`` recording the terminal state.

    Args:
        new_status: the target closed-status string (e.g. ``"merged"``, ``"abandoned"``).
        reason: human-readable explanation logged in the turn-log entry.
        phase: the caller-computed CONVERSION phase for this close -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this close
            defines, if any -- per Kepler's ruling, no caller should pass
            this for ``close`` (it defines no transition; see class docstring
            above).
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
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updated = md.update_frontmatter(content, {"status": new_status, **phase_updates})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} close: {new_status} — {reason}" + phase_log_suffix)
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
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

    Per Kepler's #3039 U2a ruling, ``merge`` writes NO transition phase of
    its own (conversion only) -- terminal states live in ``status``
    (``merged``), never in ``phase``. A merged PR baton stays at whatever
    ``phase`` it was last at (typically ``phase: merge``) with
    ``status: merged`` recording the terminal state.

    Args:
        merged_by: agent performing the merge (normalised to lowercase).
        forced: if True, append ``(FORCED past gates 3-4)`` to the log entry.
        phase: the caller-computed CONVERSION phase for this merge -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this merge
            defines, if any -- per Kepler's ruling, no caller should pass
            this for ``merge`` (it defines no transition; see class
            docstring above).
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
    fields, _ = md.parse_frontmatter(content)
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    if forced:
        log_line = (
            f"- {now} merged via baton merge by {merged_by_n} "
            "(FORCED past gates 3-4)"
        )
    else:
        log_line = f"- {now} merged via baton merge by {merged_by_n}"
    log_line += phase_log_suffix

    updated = md.update_frontmatter(content, {"status": "merged", **phase_updates})
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Pure write body of baton cmd_reopen. Validation (new_status ∈ ACTIVE_STATUSES, current_status ∈ CLOSED_STATUSES) is the caller's job. Reads current status internally for the log entry.

    Updates ``status`` in frontmatter (turn fields are NOT touched), appends a
    reopened turn-log entry recording the prior status, and writes atomically
    under the allocator lock (fork-4).

    Per Kepler's #3039 U2a ruling, ``reopen`` writes NO transition phase of
    its own (conversion only) -- it restores ``status`` and leaves ``phase``
    alone.

    Args:
        invoker: agent performing the reopen (normalised to lowercase).
        new_status: the target active-status string (e.g. ``"in-progress"``).
        phase: the caller-computed CONVERSION phase for this reopen -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this reopen
            defines, if any -- per Kepler's ruling, no caller should pass
            this for ``reopen`` (it defines no transition; see docstring
            above).
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
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updated = md.update_frontmatter(content, {"status": new_status, **phase_updates})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(
        body,
        f"- {now} reopened → {invoker_name}: status was {current_status} → {new_status}" + phase_log_suffix,
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Rename a project baton's title field; return the assigned seq.

    Pure write body of ``baton cmd_rename``: reads old title, updates ``title``
    in frontmatter, appends a turn-log entry recording the rename, and writes
    atomically under the allocator lock (fork-4). Validation (non-empty title,
    length limit, no newlines) is the caller's job.

    Per Kepler's #3039 U2a ruling, ``rename`` was never a phase-machine edge
    -- it writes NO transition phase of its own (conversion only).

    Log format: ``- {ts} renamed: title from "{old}" to "{new}"``

    Args:
        new_title: the new title string (caller should strip whitespace).
        phase: the caller-computed CONVERSION phase for this rename -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this rename
            defines, if any -- per Kepler's ruling, no caller should pass
            this for ``rename`` (it defines no transition).
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
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updated = md.update_frontmatter(content, {"title": new_title, **phase_updates})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f'- {now} renamed: title from "{old_title}" to "{new_title}"' + phase_log_suffix)
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Set or update the ``github:`` anchor on a project baton; return the assigned seq.

    Pure write body of ``baton cmd_anchor``: reads the current ``github:`` field
    (may be absent), updates (or adds) it in frontmatter, appends a turn-log
    entry that distinguishes new-set from update, and writes atomically under
    the allocator lock (fork-4). Anchor format validation (``pr/<N>`` or
    ``project/<N>``) is the caller's job.

    Per Kepler's #3039 U2a ruling, ``anchor`` was never a phase-machine edge
    -- it writes NO transition phase of its own (conversion only).

    Log formats:
      - First set: ``- {ts} anchor set: github → {anchor}``
      - Update:    ``- {ts} anchor updated: github from "{old}" to "{new}"``

    Args:
        github_anchor: the anchor string (e.g. ``"pr/490"``).
        phase: the caller-computed CONVERSION phase for this anchor call --
            see :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this anchor
            call defines, if any -- per Kepler's ruling, no caller should
            pass this for ``anchor`` (it defines no transition).
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
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    updated = md.update_frontmatter(content, {"github": github_anchor, **phase_updates})
    _, body = md.parse_frontmatter(updated)
    if old_anchor:
        log_entry = f'- {now} anchor updated: github from "{old_anchor}" to "{github_anchor}"'
    else:
        log_entry = f"- {now} anchor set: github → {github_anchor}"
    log_entry += phase_log_suffix
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
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
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

    Per Kepler's #3039 U2a ruling, ``add_participant`` was never a
    phase-machine edge -- it writes NO transition phase of its own
    (conversion only). The conversion step fires only on the real-write
    path below, never on the idempotent no-op return (#3039 U2a round 3:
    that path performs NO write at all -- no seq allocated, no file
    touched -- so there is nothing for a conversion to attach to; the
    baton's legacy-vs-explicit phase status is unchanged by a call that
    changes nothing else either).

    Args:
        agent: the agent performing the add; must already be a participant.
        participant: the agent name to add (normalised to lowercase).
        phase: the caller-computed CONVERSION phase for this add -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched. Ignored entirely on
            the idempotent no-op path (see above).
        transition_phase: the caller-computed TRANSITION phase this add
            defines, if any -- per Kepler's ruling, no caller should pass
            this for ``add_participant`` (it defines no transition).
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
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"
    new_participants = list(current_participants) + [participant_n]
    participants_str = _format_participants(new_participants)

    updated = md.update_frontmatter(content, {"participants": participants_str, **phase_updates})
    _, body = md.parse_frontmatter(updated)
    body = md.append_turn_log(body, f"- {now} {agent_n} added participant: {participant_n}" + phase_log_suffix)
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq, True


def set_colleague(
    store: CoordinationStore,
    allocator: SeqAllocator,
    project_id: str,
    *,
    agent: str,
    colleague: str,
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    ts: Optional[str] = None,
) -> tuple[int, bool]:
    """Set (or change) the ``colleague:`` field on a project baton; return ``(seq, participant_added)``.

    Pure write body of ``baton cmd_set_colleague`` (#2764). Persists
    ``colleague:`` in frontmatter — visible via ``baton show`` from this call
    onward, closing the "set-once and invisible" gap ``--colleague`` at init
    time had (that flag only ever influenced ``participants`` and a
    client-side print; nothing was stored server-side, so nothing could ever
    be read back). Also adds ``colleague`` to ``participants`` (dedup) in the
    SAME atomic write, exactly the behavior ``--colleague`` already has at
    init — this is what actually closes the friction #2764 was filed for
    (a colleague assigned post-init had no way in: ``add_participant``
    requires the CALLER to already be a participant, so the newly-named
    colleague couldn't add themselves, and the baton's actual holder had no
    verb to do it for them). After this call the colleague is already a
    participant, so nobody needs to self-add.

    Server-side authorization (LOAD-BEARING, same shape as
    ``add_participant``'s ``NotAParticipant``): ``agent`` must be EITHER a
    current participant OR the current turn holder (``fields["turn"]``) —
    the OR-holder half matters because a PR baton's turn can legitimately be
    the pool sentinel without the sentinel being a listed participant (that
    is exactly the "ready for the maintainer" state a flip-to-sentinel
    produces), and the maintainer must be able to set/confirm the colleague
    from that seat. This check lives HERE, not in the CLI, for the same
    reason ``add_participant``'s does: a CLI-only check is bypassable by a
    direct API call.

    Always writes (unlike ``add_participant``'s dedup no-op): re-asserting
    the same colleague value is still a real, logged event — the caller may
    be intentionally re-confirming after some doubt about who the gate
    targets. Mirrors ``anchor()``'s always-write, set-vs-updated log shape.

    Scope note (documented here so it isn't re-litigated at the flip site):
    this does NOT change the flip-time colleague-gate's own computation
    (``_colleague_participants`` in ``tools/baton.py``, "any participant who
    is neither the current holder nor the sentinel") — that stays a
    participants-derived heuristic. #2764 is about making the colleague
    identity settable and visible after init, not about changing how
    flip decides whether a colleague review is required.

    Decided-and-documented (#2764's third ask): a colleague named only in a
    flip reason's free text, or informally by the merge seat in a comment,
    does NOT get authorized to ``add_participant`` themselves — free text is
    not structured data the server can act on, and widening self-add
    authority to cover it would be a much larger, fuzzier authorization
    change than this feature calls for. The fix for the friction is this
    function: the baton's holder (or an existing participant) calls
    ``set_colleague``, which mechanically grants access in one atomic step.
    There is no supported "just add yourself because someone mentioned you"
    path, before or after #2764.

    Log format: ``- {ts} {agent} set colleague → {colleague}`` (first set) or
    ``- {ts} {agent} changed colleague from "{old}" to "{new}"`` (update) —
    same two-case shape as ``anchor()``.

    Per Kepler's #3039 U2a ruling, ``set_colleague`` was never a
    phase-machine edge -- it writes NO transition phase of its own
    (conversion only).

    Args:
        agent: the agent performing the set; must already be a participant
            or the current turn holder.
        colleague: the agent name to set as colleague (normalised to
            lowercase).
        phase: the caller-computed CONVERSION phase for this set -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``None`` (default) leaves an
            existing/absent ``phase:`` field untouched.
        transition_phase: the caller-computed TRANSITION phase this set
            defines, if any -- per Kepler's ruling, no caller should pass
            this for ``set_colleague`` (it defines no transition).
        ts: timestamp override (for tests / replay); defaults to ``now_iso()``.

    Returns:
        ``(seq, participant_added)`` — ``seq`` is the seq of this mutation;
        ``participant_added`` is ``True`` iff ``colleague`` was newly added
        to ``participants`` by this call.

    Raises:
        ProjectNotFound: if ``project_id`` has no existing baton file.
        NotAParticipant: if ``agent`` is neither a current participant nor
            the current turn holder of the baton.
    """
    content = store.read_project(project_id)
    if content is None:
        raise ProjectNotFound(project_id)

    fields, _ = md.parse_frontmatter(content)
    current_turn = fields.get("turn", "").strip().lower()
    current_participants = _parse_participants(fields.get("participants", ""))

    agent_n = agent.strip().lower()
    if agent_n != current_turn and agent_n not in current_participants:
        raise NotAParticipant(agent_n)

    colleague_n = colleague.strip().lower()
    old_colleague = fields.get("colleague", "").strip()

    updates = {"colleague": colleague_n}
    participant_added = colleague_n not in current_participants
    if participant_added:
        new_participants = list(current_participants) + [colleague_n]
        updates["participants"] = _format_participants(new_participants)

    now = ts or md.now_iso()
    phase_updates, phase_log_suffix = _maybe_stamp_phase(fields, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"
    updates.update(phase_updates)

    updated = md.update_frontmatter(content, updates)
    _, body = md.parse_frontmatter(updated)
    if old_colleague:
        log_entry = (
            f'- {now} {agent_n} changed colleague from "{old_colleague}" '
            f'to "{colleague_n}"'
        )
    else:
        log_entry = f"- {now} {agent_n} set colleague → {colleague_n}"
    log_entry += phase_log_suffix
    body = md.append_turn_log(body, log_entry)
    final = md.reattach_frontmatter(updated, body)

    with allocator.allocate() as seq:
        store.write_project(project_id, final, seq=seq)
    return seq, participant_added


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
    by_agent: str = "",
    github: Optional[str] = None,
    colleague: Optional[str] = None,
    author: Optional[str] = None,
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
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
      project / title / status / turn / turn_since / turn_reason / turn_by / participants / github (optional) / colleague (optional) / author (optional)

    Log format: ``- {ts} initialized → {turn}: {turn_reason}``

    Args:
        title: project title.
        status: initial status (e.g. ``"in-progress"``, ``"planning"``).
        turn: initial turn holder (lowercase, already validated by caller).
        participants: list of participant names (lowercase, already validated).
            When ``colleague`` names an agent not already in this list, the
            CLI is responsible for having added it before calling (mirrors
            ``add_participant``'s split: this fn persists, the caller dedups).
        turn_reason: human-readable reason for initial turn assignment.
        by_agent: the agent performing the init (normalised to lowercase) —
            the initor, NOT necessarily the same as ``turn`` (one agent may
            init a baton with the turn assigned to someone else). Defaults to
            ``""`` (actor unknown) for back-compat with pre-existing callers.
        github: optional GitHub anchor (e.g. ``"pr/490"`` or ``"project/4"``).
        colleague: optional expected-colleague-reviewer name (#2764). Persisted
            verbatim (lowercased/stripped) as the ``colleague:`` frontmatter
            field, so it is visible on ``baton show`` from creation onward —
            pre-#2764, ``--colleague`` at init only influenced ``participants``
            and a client-side print; nothing was ever stored server-side, which
            is why ``baton show`` could never render it. Purely descriptive:
            does NOT change the flip-time colleague-gate, which still computes
            "is there a non-author, non-sentinel participant" from
            ``participants`` alone (unchanged scope — #2764 is about
            visibility/settability of the colleague identity, not
            enforcement).
        author: optional PR-author identity (#2961), persisted verbatim
            (lowercased/stripped) as the ``author:`` frontmatter field and
            never touched again by ``flip``/``claim``/``release`` (unlike
            ``turn_by``, which is overwritten on every turn-change to record
            the ACTOR, not the original author — the exact reason ``turn_by``
            cannot serve as an author proxy past the first flip). Consumed by
            ``_is_maintainer_flip`` to exempt a colleague's "send it back to
            the author after a blocker" flip from the merge-presentation gate
            — that flip target matches neither the self-flip nor the
            flip-to-colleague exemption, so without this field it was
            misclassified as presenting-for-merge and demanded an approval
            that cannot exist (the colleague is the one objecting). The
            caller (``cmd_init``) is responsible for supplying the right
            value — as of #3042 that is the INVOKING agent, the seat that ran
            ``baton init``, regardless of whose court ``turn`` puts it in.
            (Before #3042 ``cmd_init`` supplied the initial ``turn`` holder as
            a proxy, which is correct only when a baton is initialized by its
            own author with ``turn`` pointing at themselves, and mis-attributed
            authorship whenever it was not.) Omitted (``None`` /
            falsy) on an existing baton created before this field existed —
            ``_is_maintainer_flip`` treats a missing author as "no exemption
            available," identical to pre-#2961 behavior, not as an error.
        phase: the caller-computed CONVERSION phase for this init -- see
            :func:`flip`'s docstring for the conversion/transition split
            (#3039 U2a round 3). ``init`` is ALWAYS a transition (the
            baton's very first phase) -- callers pass ``transition_phase``,
            not this. Kept for API uniformity with the other eleven write
            bodies; ``None`` (default) is the expected value here.
        transition_phase: the caller-computed INITIAL phase for this
            brand-new baton (e.g. ``"pool"`` for a bunch, ``"fairy"`` for a
            PR baton) -- a TRANSITION (#3039 U2a round 3), unconditionally
            written. There is no pre-existing ``phase:`` to check against
            (the baton doesn't exist yet) so, unlike every other write
            body, sending this via the CONVERSION channel (``phase`` above)
            would have produced the identical file content here -- but the
            wrong turn-log text ("(legacy conversion)" on a baton that was
            never legacy, Borges's finding on #3254). Routing init's
            initial phase through THIS parameter instead is what fixes
            that: the log line reads "(transition)".
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
    turn_by = (by_agent or "").strip().lower()
    participants_str = "[" + ", ".join(participants) + "]"
    # No pre-existing content to parse (this creates the baton) -- pass an
    # empty fields dict, which trivially has no "phase" key, so
    # _maybe_stamp_phase always stamps a truthy `phase` here (#3039 U2a).
    # Step 1 (conversion) then Step 2 (transition, NEW #3039 U2a round 3) --
    # see flip()'s comment for the two-step architecture. In practice a
    # caller sends `transition_phase` here, not `phase` (see docstring
    # above), so step 1 is normally a no-op and step 2 alone determines the
    # stamped value + "(transition)" log text.
    phase_updates, phase_log_suffix = _maybe_stamp_phase({}, phase, now)
    if transition_phase is not None:
        phase_updates["phase"] = transition_phase
        phase_log_suffix += f"\n- {now} phase: {transition_phase!r} (transition)"

    fm_lines = [
        "---",
        f"project: {project_id}",
        f"title: {title}",
        f"status: {status}",
        f"turn: {turn}",
        f"turn_since: {now}",
        f'turn_reason: "{turn_reason}"',
        # quoted like turn_reason — see flip()'s comment for why (an unquoted
        # empty value here, followed by the participants: line, corrupts the
        # frontmatter parser).
        f'turn_by: "{turn_by}"',
        f"participants: {participants_str}",
    ]
    if github:
        fm_lines.append(f"github: {github}")
    if colleague:
        fm_lines.append(f"colleague: {colleague.strip().lower()}")
    if author:
        fm_lines.append(f"author: {author.strip().lower()}")
    if phase_updates:
        fm_lines.append(f"phase: {phase_updates['phase']}")
    fm_lines.append("---")
    fm_lines.append("")

    body_lines = [
        "",
        f"# {project_id} — {title}",
        "",
        "## Turn log",
        "",
        f"- {now} initialized → {turn}: {turn_reason}" + phase_log_suffix,
        "",
    ]

    content = "\n".join(fm_lines) + "\n".join(body_lines)

    with allocator.allocate() as seq:
        store.write_project(project_id, content, seq=seq)
    return seq
