"""#4180: an injected stage delay is ADDITIVE even when the stage's peer is late.

The stage profiler injects a per-stage delay inside the stage's own timed span.
Sleeping BEFORE the wrapped call lets that sleep run in parallel with a late
peer (the owner's `request_read` waits on the client's `request_send`), so the
injected delay vanished: measured rise -0.008 ms against 2.080 ms with a prompt
client. The sleep goes AFTER the call returns, still inside the span, so a
peer's lateness cannot absorb it.

Exception: `response_send`, the owner's last action, is the one stage slept
BEFORE its call (`_PRECALL_INJECT_STAGES`, #4183): a sleep after it would land
after the client already has its reply and leave end-to-end flat.

Contract:
  1. owner side: with `request_send` slowed in BOTH profiles, injecting on
     `request_read` still rises >= INJECTED_RISE_MIN_MS.
  2. owner side, function-injection path (`inject_owner_fn` patches a delay
     into the real `_read_request_line` under the wrapper): with `request_send`
     slowed in BOTH profiles, `request_read` still rises >= INJECTED_RISE_MIN_MS.
  3. client side: with an owner stage slowed in BOTH profiles, injecting on
     `response_decode` still rises >= INJECTED_RISE_MIN_MS. This is a REGRESSION
     GUARD, NOT discriminating on current code: no client stage has a waiting
     peer inside its wrapped call (`response_decode` is json.loads after `recv`
     returned; connect/sendall complete in the kernel), so the defect cannot be
     made red there and the client wrapper deliberately stays pre-call.
"""

from __future__ import annotations

import pytest

from _owner_platform import requires_linux_owner
from test_engramd_stage_profile_3678 import (  # noqa: F401  (fixtures + constants)
    INJECT_S, INJECTED_RISE_MIN_MS, _restore_sys_path, store, tool,
)

LATE_S = 0.004  # the peer's lateness: twice the injected delay


@pytest.mark.timing
@requires_linux_owner
def test_owner_injection_is_additive_when_the_client_is_late(tool, store):
    late = {"request_send": LATE_S}
    base = tool.profile(n=30, home=store, warmup=5, inject=dict(late))
    out = tool.profile(n=30, home=store, warmup=5, inject={**late, "request_read": INJECT_S})
    rise = out["stages"]["request_read"]["p50_ms"] - base["stages"]["request_read"]["p50_ms"]
    assert rise >= INJECTED_RISE_MIN_MS, rise


@pytest.mark.timing
@requires_linux_owner
def test_owner_function_injection_is_additive_when_the_client_is_late(tool, store):
    fn = "engramd_owner:EngramdOwner._read_request_line"
    late = {"request_send": LATE_S}
    base = tool.profile(n=30, home=store, warmup=5, inject=dict(late))
    out = tool.profile(n=30, home=store, warmup=5, inject=dict(late), inject_owner_fn={fn: INJECT_S})
    rise = out["stages"]["request_read"]["p50_ms"] - base["stages"]["request_read"]["p50_ms"]
    assert rise >= INJECTED_RISE_MIN_MS, rise


@pytest.mark.timing
@requires_linux_owner
def test_client_injection_is_additive_when_the_owner_is_late(tool, store):
    late = {"response_encode": LATE_S}
    base = tool.profile(n=30, home=store, warmup=5, inject=dict(late))
    out = tool.profile(n=30, home=store, warmup=5, inject={**late, "response_decode": INJECT_S})
    rise = out["stages"]["response_decode"]["p50_ms"] - base["stages"]["response_decode"]["p50_ms"]
    assert rise >= INJECTED_RISE_MIN_MS, rise
