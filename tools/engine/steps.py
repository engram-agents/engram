"""tools.engine.steps — convergent step-graph framework for ENGRAM flows.

Pure Python 3 stdlib; ZERO imports from ENGRAM runtime modules.

Overview
--------
A *flow* is a list of Step objects forming a directed acyclic graph (DAG)
where edges express ordering constraints (``requires``).  The executor walks
the DAG in topological order; for each step it:

  1. Calls ``check(ctx)`` — if True, the step is already satisfied, skip.
  2. Calls ``apply(ctx)`` (for agent steps) or emits ``instruction`` and
     returns PAUSED (for operator steps).
  3. Calls ``verify(ctx)`` — if False, fails loud with the step id.

Because ``check`` is re-evaluated on every run, a flow is **idempotent and
resumable by construction**: re-running after a pause or failure re-walks the
DAG and skips already-satisfied steps.

Result statuses
---------------
- ``DONE`` — all steps satisfied.
- ``PAUSED(step_id, instruction)`` — execution reached an operator step; the
  human must act, then re-run.
- ``FAILED(step_id, reason)`` — a step's verify() returned False (or raised).

Exit-code mapping (used by cli.py):
  DONE   → 0
  PAUSED → 3
  FAILED → 1
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

# ---------------------------------------------------------------------------
# Context type alias
# ---------------------------------------------------------------------------

# Ctx is an arbitrary dict-like object passed through the flow.  We use Any
# here so that flows.py can define their own typed Ctx without a circular dep.
Ctx = Any


# ---------------------------------------------------------------------------
# Step dataclass
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """One node in the step DAG.

    Parameters
    ----------
    id:
        Unique identifier for this step within the flow.  Used in error
        messages and PAUSED/FAILED results.
    requires:
        List of step ids that must complete before this step is attempted.
        Defines DAG edges; a cycle raises ``ValueError`` at plan-time.
    kind:
        ``"agent"`` — the step calls ``apply(ctx)`` automatically.
        ``"operator"`` — the step emits ``instruction`` and pauses; ``apply``
        must be None.
    check:
        ``check(ctx) -> bool`` — already satisfied?  If True, the step is
        skipped (apply + verify are not called).
    apply:
        ``apply(ctx) -> None`` — performs the work.  Must be None for
        operator steps (the framework enforces this at construction time).
    verify:
        ``verify(ctx) -> bool`` — did the work take?  Called by ``run_flow``
        after ``apply()`` for **agent steps only**.  Operator steps are never
        verified by ``run_flow``; on resume, ``check()`` gates the skip, and
        ``verify()`` is reserved for ``run_doctor`` (diagnostic mode).
        Failure raises a FAILED result.  **Contract**: verify callbacks must
        be side-effect-free; doctor mode runs verify() on every step,
        including never-applied ones.
    instruction:
        Human-facing instruction emitted when an operator step is reached.
        Required for operator steps; must be None for agent steps.
    """

    id: str
    requires: list[str] = field(default_factory=list)
    kind: Literal["agent", "operator"] = "agent"
    check: Callable[[Ctx], bool] = field(default=lambda _ctx: False)
    apply: Callable[[Ctx], None] | None = None
    verify: Callable[[Ctx], bool] = field(default=lambda _ctx: True)
    instruction: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "operator":
            if self.apply is not None:
                raise ValueError(
                    f"Step {self.id!r}: operator steps must have apply=None "
                    "(they emit instruction + pause; the agent never auto-executes them)"
                )
            if self.instruction is None:
                raise ValueError(
                    f"Step {self.id!r}: operator steps must have a non-None instruction"
                )
        else:
            if self.instruction is not None:
                raise ValueError(
                    f"Step {self.id!r}: agent steps must have instruction=None"
                )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Done:
    """All steps satisfied."""

    status: str = "DONE"


@dataclass(frozen=True)
class Paused:
    """Execution paused at an operator step."""

    step_id: str
    instruction: str
    status: str = "PAUSED"


@dataclass(frozen=True)
class Failed:
    """A step failed verification."""

    step_id: str
    reason: str
    status: str = "FAILED"


FlowResult = Done | Paused | Failed


# ---------------------------------------------------------------------------
# DAG utilities
# ---------------------------------------------------------------------------


def _topological_sort(steps: list[Step]) -> list[Step]:
    """Return steps in a valid topological order.

    Raises
    ------
    ValueError
        If the step graph contains a cycle, or if a ``requires`` entry
        references a step id that is not present in ``steps``.
    """
    step_by_id: dict[str, Step] = {s.id: s for s in steps}

    # Validate all requires references
    for s in steps:
        for dep in s.requires:
            if dep not in step_by_id:
                raise ValueError(
                    f"Step {s.id!r} requires {dep!r}, which is not in the flow"
                )

    # Kahn's algorithm
    in_degree: dict[str, int] = {s.id: 0 for s in steps}
    dependents: dict[str, list[str]] = {s.id: [] for s in steps}
    for s in steps:
        for dep in s.requires:
            in_degree[s.id] += 1
            dependents[dep].append(s.id)

    queue: collections.deque[str] = collections.deque(
        sid for sid, deg in in_degree.items() if deg == 0
    )
    ordered: list[Step] = []

    while queue:
        sid = queue.popleft()
        ordered.append(step_by_id[sid])
        for child in dependents[sid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(ordered) != len(steps):
        # Cycle detected — find the participants
        in_cycle = [sid for sid, deg in in_degree.items() if deg > 0]
        raise ValueError(
            f"Cycle detected in step DAG; participants: {sorted(in_cycle)}"
        )

    return ordered


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def run_flow(steps: list[Step], ctx: Ctx) -> FlowResult:
    """Execute the flow DAG against ``ctx``.

    The flow is **idempotent and resumable**: every call walks the full
    topological order, skipping steps whose ``check(ctx)`` is satisfied.
    A PAUSED result means the caller should have the operator act, then
    re-call ``run_flow`` (re-walk skips already-satisfied steps naturally).

    Parameters
    ----------
    steps:
        The list of Step objects forming the flow.
    ctx:
        The mutable context object passed through every step callback.

    Returns
    -------
    FlowResult
        ``Done``, ``Paused``, or ``Failed``.
    """
    ordered = _topological_sort(steps)

    for step in ordered:
        # Already satisfied — skip
        try:
            if step.check(ctx):
                continue
        except Exception as exc:
            return Failed(step_id=step.id, reason=f"check() raised: {exc}")

        if step.kind == "operator":
            # Operator steps: emit instruction and pause.  On the next run_flow
            # call, check() is re-evaluated — if the operator completed their
            # action and check() now returns True, the step is skipped.  If
            # check() is still False the flow pauses here again.  verify() is
            # never called by run_flow for operator steps; it is only called by
            # run_doctor.
            return Paused(step_id=step.id, instruction=step.instruction)  # type: ignore[arg-type]

        # Agent step: apply then verify
        try:
            if step.apply is not None:
                step.apply(ctx)
        except Exception as exc:
            return Failed(step_id=step.id, reason=f"apply() raised: {exc}")

        try:
            ok = step.verify(ctx)
        except Exception as exc:
            return Failed(step_id=step.id, reason=f"verify() raised: {exc}")

        if not ok:
            # A step's verify (or check) may store a human-readable delta in
            # ctx["_last_failure_detail"] to surface why it failed.  Append it
            # to the generic reason when present.
            detail = ctx.get("_last_failure_detail") if isinstance(ctx, dict) else None
            base = "verify() returned False after apply()"
            return Failed(
                step_id=step.id,
                reason=f"{base}: {detail}" if detail else base,
            )

    return Done()


# ---------------------------------------------------------------------------
# Doctor mode
# ---------------------------------------------------------------------------


@dataclass
class DoctorResult:
    """Per-step result from doctor mode."""

    step_id: str
    satisfied: bool
    error: str | None = None


def run_doctor(steps: list[Step], ctx: Ctx) -> list[DoctorResult]:
    """Read-only audit: run verify() on every step and return results.

    Doctor mode does NOT call apply() or emit operator instructions — it is
    purely diagnostic.  A step is reported as ``satisfied=True`` if
    ``verify(ctx)`` returns True (regardless of whether check() also passes).

    Parameters
    ----------
    steps:
        The list of Step objects forming the flow.
    ctx:
        The context to verify against.  Doctor mode does not mutate ctx.

    Returns
    -------
    list[DoctorResult]
        One entry per step, in topological order.
    """
    ordered = _topological_sort(steps)
    results: list[DoctorResult] = []

    for step in ordered:
        try:
            ok = step.verify(ctx)
            results.append(DoctorResult(step_id=step.id, satisfied=ok))
        except Exception as exc:
            results.append(
                DoctorResult(step_id=step.id, satisfied=False, error=str(exc))
            )

    return results
