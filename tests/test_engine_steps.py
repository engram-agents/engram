"""Unit tests for tools.engine.steps — the convergent step-graph framework.

Coverage:
  1. DAG topological ordering (multi-level dependency chain)
  2. Skip-on-satisfied (check() True → apply/verify not called)
  3. Resume-after-failure (re-run skips already-satisfied steps)
  4. PAUSED semantics (operator step pauses; re-run resumes past it)
  5. Cycle detection (hard error)
  6. FAILED names the failing step
  7. Doctor mode — broken invariant reports the right step
  8. Missing requires reference raises ValueError
"""

from __future__ import annotations

import pytest

from tools.engine.steps import (
    Done,
    DoctorResult,
    Failed,
    Paused,
    Step,
    run_doctor,
    run_flow,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_counter_step(
    sid: str,
    requires: list[str] | None = None,
    *,
    check_val: bool = False,
    verify_val: bool = True,
) -> tuple[Step, list[str]]:
    """Return a (Step, calls_log) pair.

    The Step records its call sequence in calls_log so tests can assert order.
    """
    calls: list[str] = []

    def check(ctx: dict) -> bool:
        calls.append(f"check:{sid}")
        return ctx.get(f"_sat_{sid}", check_val)

    def apply(ctx: dict) -> None:
        calls.append(f"apply:{sid}")
        ctx[f"_applied_{sid}"] = True

    def verify(ctx: dict) -> bool:
        calls.append(f"verify:{sid}")
        return ctx.get(f"_verify_{sid}", verify_val)

    step = Step(
        id=sid,
        requires=requires or [],
        kind="agent",
        check=check,
        apply=apply,
        verify=verify,
    )
    return step, calls


# ---------------------------------------------------------------------------
# 1. DAG ordering
# ---------------------------------------------------------------------------


class TestDagOrdering:
    """Steps run in topological order regardless of list order."""

    def test_chain_order(self):
        """A → B → C chain: C must run after B, B after A."""
        order: list[str] = []

        def make(sid: str, requires: list[str]) -> Step:
            def apply(ctx: dict) -> None:
                order.append(sid)

            return Step(id=sid, requires=requires, kind="agent", apply=apply)

        steps = [
            make("C", ["B"]),
            make("A", []),
            make("B", ["A"]),
        ]
        result = run_flow(steps, {})
        assert isinstance(result, Done)
        assert order == ["A", "B", "C"]

    def test_diamond_order(self):
        """Diamond: A → B, A → C, B+C → D.  D must run after both B and C."""
        order: list[str] = []

        def make(sid: str, requires: list[str]) -> Step:
            def apply(ctx: dict) -> None:
                order.append(sid)

            return Step(id=sid, requires=requires, kind="agent", apply=apply)

        steps = [
            make("D", ["B", "C"]),
            make("C", ["A"]),
            make("B", ["A"]),
            make("A", []),
        ]
        result = run_flow(steps, {})
        assert isinstance(result, Done)
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")
        assert order.index("B") < order.index("D")
        assert order.index("C") < order.index("D")


# ---------------------------------------------------------------------------
# 2. Skip-on-satisfied
# ---------------------------------------------------------------------------


class TestSkipOnSatisfied:
    """check() returning True → apply() and verify() are NOT called."""

    def test_already_satisfied_skips_apply(self):
        apply_called: list[bool] = []

        def apply(ctx: dict) -> None:
            apply_called.append(True)

        step = Step(
            id="already",
            kind="agent",
            check=lambda ctx: True,
            apply=apply,
            verify=lambda ctx: True,
        )
        result = run_flow([step], {})
        assert isinstance(result, Done)
        assert not apply_called, "apply() must not be called when check() is True"

    def test_partially_satisfied_flow(self):
        """Only unsatisfied steps run."""
        applied: list[str] = []

        def make(sid: str, already_done: bool) -> Step:
            def apply(ctx: dict) -> None:
                applied.append(sid)

            return Step(
                id=sid,
                kind="agent",
                check=lambda ctx, d=already_done: d,
                apply=apply,
                verify=lambda ctx: True,
            )

        steps = [make("A", True), make("B", False), make("C", True)]
        result = run_flow(steps, {})
        assert isinstance(result, Done)
        assert applied == ["B"], f"Only B should have run; got {applied}"


# ---------------------------------------------------------------------------
# 3. Resume-after-failure
# ---------------------------------------------------------------------------


class TestResumeAfterFailure:
    """Re-running after a FAILED result skips already-satisfied steps."""

    def test_resume_skips_completed_steps(self):
        """First run: A passes, B fails verify.  Second run: A is skipped."""
        applied: list[str] = []

        def make_a() -> Step:
            def apply(ctx: dict) -> None:
                applied.append("A")
                ctx["_a_done"] = True

            return Step(
                id="A",
                kind="agent",
                check=lambda ctx: ctx.get("_a_done", False),
                apply=apply,
                verify=lambda ctx: ctx.get("_a_done", False),
            )

        def make_b(fail_first: list[bool]) -> Step:
            def apply(ctx: dict) -> None:
                applied.append("B")
                ctx["_b_done"] = True

            def verify(ctx: dict) -> bool:
                if fail_first and fail_first[0]:
                    fail_first[0] = False
                    return False
                return ctx.get("_b_done", False)

            return Step(
                id="B",
                requires=["A"],
                kind="agent",
                check=lambda ctx: False,
                apply=apply,
                verify=verify,
            )

        fail_flag = [True]
        ctx: dict = {}
        steps = [make_a(), make_b(fail_flag)]

        # First run: A passes, B fails verify
        r1 = run_flow(steps, ctx)
        assert isinstance(r1, Failed), f"Expected Failed, got {r1}"
        assert r1.step_id == "B"
        assert ctx.get("_a_done") is True

        # Second run: A is check()-satisfied (skipped); B runs and passes
        r2 = run_flow(steps, ctx)
        assert isinstance(r2, Done), f"Expected Done on resume, got {r2}"
        # A was only applied once (first run)
        assert applied.count("A") == 1
        assert applied.count("B") == 2  # applied on both runs (check=False always)


# ---------------------------------------------------------------------------
# 4. PAUSED semantics
# ---------------------------------------------------------------------------


class TestPausedSemantics:
    """Operator step: emits instruction + PAUSED; re-run resumes."""

    def _make_operator_step(self, sid: str, requires: list[str] | None = None) -> Step:
        return Step(
            id=sid,
            requires=requires or [],
            kind="operator",
            check=lambda ctx, s=sid: ctx.get(f"_op_{s}_done", False),
            apply=None,
            verify=lambda ctx, s=sid: ctx.get(f"_op_{s}_done", False),
            instruction=f"Operator: please complete {sid!r}",
        )

    def test_paused_at_first_operator(self):
        """Run pauses at the first operator step, returning PAUSED with step_id + instruction."""
        agent_step = Step(
            id="prep",
            kind="agent",
            apply=lambda ctx: ctx.update({"_prep": True}),
            verify=lambda ctx: ctx.get("_prep", False),
        )
        op_step = self._make_operator_step("op1", requires=["prep"])

        ctx: dict = {}
        result = run_flow([agent_step, op_step], ctx)

        assert isinstance(result, Paused)
        assert result.step_id == "op1"
        assert "op1" in result.instruction

    def test_resume_after_operator(self):
        """After operator marks ctx, re-run continues past the operator step."""
        op_step = self._make_operator_step("op1")
        after_step = Step(
            id="after",
            requires=["op1"],
            kind="agent",
            apply=lambda ctx: ctx.update({"_after": True}),
            verify=lambda ctx: ctx.get("_after", False),
        )

        ctx: dict = {}
        r1 = run_flow([op_step, after_step], ctx)
        assert isinstance(r1, Paused)
        assert r1.step_id == "op1"

        # Simulate operator action
        ctx["_op_op1_done"] = True

        r2 = run_flow([op_step, after_step], ctx)
        assert isinstance(r2, Done)
        assert ctx.get("_after") is True

    def test_paused_at_second_operator(self):
        """Two operator steps: first PAUSED at op1, then at op2 after op1 resolved."""
        op1 = self._make_operator_step("op1")
        op2 = self._make_operator_step("op2", requires=["op1"])

        ctx: dict = {}
        r1 = run_flow([op1, op2], ctx)
        assert isinstance(r1, Paused)
        assert r1.step_id == "op1"

        ctx["_op_op1_done"] = True
        r2 = run_flow([op1, op2], ctx)
        assert isinstance(r2, Paused)
        assert r2.step_id == "op2"

        ctx["_op_op2_done"] = True
        r3 = run_flow([op1, op2], ctx)
        assert isinstance(r3, Done)

    def test_operator_apply_must_be_none(self):
        """Constructing an operator Step with apply != None must raise ValueError."""
        with pytest.raises(ValueError, match="apply=None"):
            Step(
                id="bad",
                kind="operator",
                apply=lambda ctx: None,
                instruction="do something",
            )

    def test_operator_instruction_required(self):
        """Constructing an operator Step with instruction=None must raise ValueError."""
        with pytest.raises(ValueError, match="instruction"):
            Step(
                id="bad",
                kind="operator",
                apply=None,
                instruction=None,
            )

    def test_agent_instruction_must_be_none(self):
        """Constructing an agent Step with instruction != None must raise ValueError."""
        with pytest.raises(ValueError, match="agent steps must have instruction=None"):
            Step(
                id="bad",
                kind="agent",
                apply=lambda ctx: None,
                instruction="should not have this",
            )


# ---------------------------------------------------------------------------
# 5. Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """A cycle in the DAG raises ValueError with 'Cycle detected'."""

    def test_simple_cycle(self):
        """A → B → A should raise ValueError."""
        a = Step(id="A", requires=["B"], kind="agent")
        b = Step(id="B", requires=["A"], kind="agent")
        with pytest.raises(ValueError, match="Cycle detected"):
            run_flow([a, b], {})

    def test_self_loop(self):
        """A step requiring itself is a cycle."""
        a = Step(id="A", requires=["A"], kind="agent")
        with pytest.raises(ValueError, match="Cycle detected"):
            run_flow([a], {})

    def test_three_node_cycle(self):
        """A → B → C → A."""
        a = Step(id="A", requires=["C"], kind="agent")
        b = Step(id="B", requires=["A"], kind="agent")
        c = Step(id="C", requires=["B"], kind="agent")
        with pytest.raises(ValueError, match="Cycle detected"):
            run_flow([a, b, c], {})

    def test_missing_requires_raises(self):
        """A requires reference to a step not in the list raises ValueError."""
        a = Step(id="A", requires=["nonexistent"], kind="agent")
        with pytest.raises(ValueError, match="nonexistent"):
            run_flow([a], {})


# ---------------------------------------------------------------------------
# 6. FAILED names the step
# ---------------------------------------------------------------------------


class TestFailedNamingStep:
    """FAILED result must carry the correct step_id."""

    def test_first_step_fails(self):
        step = Step(
            id="fragile",
            kind="agent",
            verify=lambda ctx: False,
        )
        result = run_flow([step], {})
        assert isinstance(result, Failed)
        assert result.step_id == "fragile"

    def test_middle_step_fails(self):
        good = Step(id="good", kind="agent", verify=lambda ctx: True)
        bad = Step(id="bad", requires=["good"], kind="agent", verify=lambda ctx: False)
        last = Step(id="last", requires=["bad"], kind="agent")

        result = run_flow([good, bad, last], {})
        assert isinstance(result, Failed)
        assert result.step_id == "bad"
        # 'last' must NOT have run
        assert "last" not in result.reason

    def test_apply_raises_is_failed(self):
        """apply() raising an exception produces FAILED with the step id."""

        def bad_apply(ctx: dict) -> None:
            raise RuntimeError("boom")

        step = Step(id="exploding", kind="agent", apply=bad_apply)
        result = run_flow([step], {})
        assert isinstance(result, Failed)
        assert result.step_id == "exploding"
        assert "boom" in result.reason

    def test_check_raises_is_failed(self):
        """check() raising is treated as FAILED, not as unsatisfied."""

        def bad_check(ctx: dict) -> bool:
            raise RuntimeError("check-boom")

        step = Step(id="check-exploding", kind="agent", check=bad_check)
        result = run_flow([step], {})
        assert isinstance(result, Failed)
        assert result.step_id == "check-exploding"


# ---------------------------------------------------------------------------
# 7. Doctor mode
# ---------------------------------------------------------------------------


class TestDoctorMode:
    """run_doctor() calls verify() on all steps without calling apply()."""

    def test_all_passing(self):
        steps = [
            Step(id="s1", kind="agent", verify=lambda ctx: True),
            Step(id="s2", requires=["s1"], kind="agent", verify=lambda ctx: True),
        ]
        results = run_doctor(steps, {})
        assert all(r.satisfied for r in results)
        assert [r.step_id for r in results] == ["s1", "s2"]

    def test_broken_step_named(self):
        """Doctor names the broken step."""
        steps = [
            Step(id="ok", kind="agent", verify=lambda ctx: True),
            Step(id="broken", requires=["ok"], kind="agent", verify=lambda ctx: False),
            Step(id="also-ok", requires=["broken"], kind="agent", verify=lambda ctx: True),
        ]
        results = run_doctor(steps, {})
        broken = [r for r in results if not r.satisfied]
        assert len(broken) == 1
        assert broken[0].step_id == "broken"

    def test_doctor_does_not_call_apply(self):
        """Doctor mode must not call apply()."""
        apply_called: list[bool] = []

        def apply(ctx: dict) -> None:
            apply_called.append(True)

        steps = [Step(id="s", kind="agent", apply=apply, verify=lambda ctx: True)]
        run_doctor(steps, {})
        assert not apply_called, "doctor must not call apply()"

    def test_doctor_does_not_pause_on_operator(self):
        """Doctor mode treats operator steps like agent steps — verify only, no pause."""
        op_step = Step(
            id="op",
            kind="operator",
            check=lambda ctx: False,
            apply=None,
            verify=lambda ctx: ctx.get("_op_done", False),
            instruction="do the thing",
        )
        results = run_doctor([op_step], {})
        # Should return a DoctorResult, not raise or return a Paused
        assert len(results) == 1
        assert not results[0].satisfied

    def test_verify_raises_reported_as_error(self):
        """If verify() raises, DoctorResult.satisfied is False and error is set."""

        def bad_verify(ctx: dict) -> bool:
            raise RuntimeError("verify-boom")

        steps = [Step(id="noisy", kind="agent", verify=bad_verify)]
        results = run_doctor(steps, {})
        assert len(results) == 1
        assert not results[0].satisfied
        assert results[0].error is not None
        assert "verify-boom" in results[0].error


# ---------------------------------------------------------------------------
# 8. Empty flow
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases: empty flow, single step."""

    def test_empty_flow_is_done(self):
        result = run_flow([], {})
        assert isinstance(result, Done)

    def test_single_satisfied_step(self):
        step = Step(id="s", kind="agent", check=lambda ctx: True)
        result = run_flow([step], {})
        assert isinstance(result, Done)

    def test_single_agent_step_done(self):
        ctx: dict = {}
        step = Step(
            id="s",
            kind="agent",
            apply=lambda c: c.update({"x": 1}),
            verify=lambda c: c.get("x") == 1,
        )
        result = run_flow([step], ctx)
        assert isinstance(result, Done)
        assert ctx["x"] == 1
