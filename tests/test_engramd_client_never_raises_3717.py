"""Contract tests for engram-alpha#3717: `engramd_client._call` returns Ok /
Unavailable / Rejected on every path and never raises.

Written BEFORE the change (contract-first). Two statements ran outside any guard:
- `socket.socket(...)` -> OSError (EMFILE / ENFILE): the CLIENT process's fact, so
  `Unavailable(reason="local")`, never a code that says something about the owner.
- `json.dumps(request)` -> TypeError / ValueError (and NaN / Infinity encoded to
  non-JSON tokens): the CALLER's error, so `Rejected(reason="request_not_serializable")`,
  decided before any socket exists.

Cell map: (a) EMFILE, (a2) ENFILE, (b) a set, (b2) a circular dict, (c) NaN
(and Infinity), (d) the steps table has a `local` row with no owner claim.

Round 2 (the class enumerated by EXCEPTION, not by input): (e) request-side
RecursionError, (f) response-side RecursionError, (g) invalid timeout, (h) the
last-resort `internal` guard (a signal, with its own steps row), (i) a seeded
generative cell that fuzzes params, timeouts and owner replies and asserts every
result is a result type and NEVER `internal`. `hypothesis` is not a dependency
here, so the generator is a seeded `random.Random` with fixed seeds.
"""

from __future__ import annotations

import errno
import itertools
import json
import os
import random
import socket as _socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402

DOWN_RESTART_PREFIX = "Check whether the engramd process is running and (re)start it"


class _SocketSpy:
    """Replaces `socket.socket` in the client module; records calls."""

    def __init__(self, exc: BaseException | None = None):
        self.calls = 0
        self.exc = exc

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        raise AssertionError("socket.socket reached when it must not be")


def _patch_socket(monkeypatch, exc=None) -> _SocketSpy:
    spy = _SocketSpy(exc)
    monkeypatch.setattr(ec.socket, "socket", spy)
    return spy


def _sp() -> Path:
    # Short and fixed: pytest's tmp_path can exceed the AF_UNIX limit (103 bytes), which
    # would return `down` before socket creation. The path need not exist (no connect).
    return Path("/tmp/e3717/run/engramd.sock")


def _all_steps(res) -> list[str]:
    return list(res.agent_steps) + list(res.human_steps)


# --- (a) / (a2) socket creation fails -----------------------------------------


@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENFILE])
@pytest.mark.parametrize("via", ["read", "write", "_call"])
def test_a_socket_creation_oserror_is_local_unavailable(monkeypatch, code, via):
    spy = _patch_socket(monkeypatch, OSError(code, os.strerror(code)))
    sp = _sp()
    if via == "read":
        res = ec.read("snapshot", {}, socket_path=sp)
    elif via == "write":
        res = ec.write("commit", {}, socket_path=sp)
    else:
        res = ec._call("m", {}, request_id="r1", timeout=1.0, socket_path=sp)
    assert spy.calls == 1
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "local"
    assert os.strerror(code) in res.detail or str(code) in res.detail
    assert res.owner_pid is None
    assert res.agent_steps and res.human_steps
    assert not any(s.startswith(DOWN_RESTART_PREFIX) for s in _all_steps(res))


# --- (b) / (b2) / (c) the request cannot be serialized ----------------------------


def _circular():
    d: dict = {}
    d["self"] = d
    return d


@pytest.mark.parametrize(
    "params",
    [
        {"x": {1, 2}},
        {"x": b"bytes"},
        {"x": object()},
        {"x": _circular()},
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": float("-inf")},
    ],
    ids=["set", "bytes", "object", "circular", "nan", "inf", "-inf"],
)
def test_b_unserializable_request_is_rejected_with_no_socket(monkeypatch, params):
    spy = _patch_socket(monkeypatch)
    res = ec._call("m", params, request_id="rid-7", timeout=1.0, socket_path=_sp())
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "request_not_serializable"
    assert res.request_id == "rid-7"
    assert res.detail  # exception type and message
    assert "serializable" in res.remedy
    assert spy.calls == 0


def test_b_public_wrappers_also_reject(monkeypatch):
    spy = _patch_socket(monkeypatch)
    r = ec.read("snapshot", {"x": {1}}, socket_path=_sp())
    w = ec.write("commit", {"x": float("nan")}, request_id="rid-9", socket_path=_sp())
    assert isinstance(r, ec.Rejected) and r.reason == "request_not_serializable"
    assert isinstance(w, ec.Rejected) and w.reason == "request_not_serializable"
    assert w.request_id == "rid-9"
    assert spy.calls == 0


def test_b_nan_detail_names_the_value_error(monkeypatch):
    _patch_socket(monkeypatch)
    res = ec._call("m", {"x": float("nan")}, request_id=None, timeout=1.0, socket_path=_sp())
    assert isinstance(res, ec.Rejected)
    assert res.detail.startswith("ValueError: ")


def test_b_size_check_still_runs_on_a_serializable_payload(monkeypatch):
    spy = _patch_socket(monkeypatch)
    big = {"x": "a" * (ec.MAX_REQUEST_BYTES + 1)}
    res = ec._call("m", big, request_id=None, timeout=1.0, socket_path=_sp())
    assert isinstance(res, ec.Rejected) and res.reason == "request_too_large"
    assert spy.calls == 0


# --- (d) the steps table ------------------------------------------------------------


def _mk(reason: str):
    return ec._unavailable(
        socket_path="/x/run/engramd.sock", bound=2.0, detail="d", request_id=None,
        reason=reason,
    )


def test_d_local_is_a_reason_with_a_row():
    assert "local" in ec.UNAVAILABLE_REASONS
    assert "local" in ec._REMEDY_STEPS
    for reason in ec.UNAVAILABLE_REASONS:
        if reason not in ec._BUILDER_REASONS:
            assert reason in ec._REMEDY_STEPS, reason
    assert set(ec._REMEDY_STEPS) | set(ec._BUILDER_REASONS) == set(ec.UNAVAILABLE_REASONS)


def test_d_local_steps_have_no_owner_restart_and_no_owner_claim():
    r = _mk("local")
    assert r.reason == "local"
    assert r.agent_steps and r.human_steps
    assert not any(s.startswith(DOWN_RESTART_PREFIX) for s in _all_steps(r))
    assert r.agent_steps != _mk("down").agent_steps
    assert r.human_steps != _mk("down").human_steps
    text = " ".join(_all_steps(r)).lower()
    for claim in ("it is up", "is healthy", "is down", "is running", "has crashed"):
        assert claim not in text, claim
    assert "owner" in text and "nothing is known about the owner" in " ".join(r.agent_steps).lower()
    # the only owner mention that tells the caller to act is a refusal to restart it
    assert "do not restart the owner" in " ".join(r.agent_steps).lower()
    assert "restart the claude code session" in " ".join(r.human_steps).lower()


# --- (e) request-side RecursionError ---------------------------------------------------


def _nested(depth: int, kind: str = "list"):
    """Nest `depth` deep ITERATIVELY (building it recursively would hit the limit
    in the test itself)."""
    node: object = []
    for _ in range(depth):
        node = [node] if kind == "list" else {"k": node}
    return node


@pytest.mark.parametrize("kind", ["list", "dict"])
def test_e_request_nested_200000_deep_is_rejected_with_no_socket(monkeypatch, kind):
    spy = _patch_socket(monkeypatch)
    res = ec._call(
        "m", {"x": _nested(200_000, kind)}, request_id="r", timeout=1.0, socket_path=_sp()
    )
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "request_not_serializable"
    assert res.detail.startswith("RecursionError")
    assert spy.calls == 0


def test_e_int_beyond_the_digit_limit_is_rejected(monkeypatch):
    # Measured on 3.12: json.dumps raises ValueError ("Exceeds the limit (4300
    # digits)"), already inside the (TypeError, ValueError) clause.
    spy = _patch_socket(monkeypatch)
    res = ec._call("m", {"x": 10**5000}, request_id=None, timeout=1.0, socket_path=_sp())
    assert isinstance(res, ec.Rejected) and res.reason == "request_not_serializable"
    assert spy.calls == 0


# --- a stand-in owner (a real AF_UNIX listener on a short /tmp path) ----------------


class _FakeOwner:
    """Accepts one connection, reads the request line, sends `reply` (bytes), closes.
    `reply=None` closes without sending. Never raises into the test."""

    def __init__(self, reply: bytes | None):
        self.dir = tempfile.mkdtemp(prefix="e3", dir="/tmp")
        self.path = Path(self.dir) / "o.sock"
        self.reply = reply
        self.srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self.srv.bind(str(self.path))
        self.srv.listen(1)
        self.srv.settimeout(10)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        try:
            conn.settimeout(10)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if self.reply is not None:
                conn.sendall(self.reply)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        try:
            self.srv.close()
        finally:
            try:
                os.unlink(self.path)
                os.rmdir(self.dir)
            except OSError:
                pass


# --- (f) response-side RecursionError ----------------------------------------------


def test_f_response_nested_200000_deep_is_protocol():
    reply = ("[" * 200_000 + "]" * 200_000 + "\n").encode()
    assert len(reply) < ec.MAX_RESPONSE_BYTES
    owner = _FakeOwner(reply)
    try:
        res = ec._call("m", {}, request_id="r", timeout=3.0, socket_path=owner.path)
    finally:
        owner.close()
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "protocol"
    assert "unparseable" in res.detail


@pytest.mark.parametrize(
    "reply",
    [b'{"status":"ok","result":' + b"9" * 5000 + b"}\n", b'{"status":' + b"9" * 5000 + b"}\n"],
    ids=["ok-result", "status"],
)
def test_f_response_with_an_int_over_the_digit_limit_is_protocol(reply):
    """An integer over Python's int-from-str digit limit (4300) makes json.loads
    raise a plain ValueError, not JSONDecodeError (round-2 review). The owner
    answered, but not validly: protocol. Mutation: catch only JSONDecodeError at
    the json.loads site -> reaches `internal` -> red."""
    owner = _FakeOwner(reply)
    try:
        res = ec._call("m", {}, request_id="r", timeout=3.0, socket_path=owner.path)
    finally:
        owner.close()
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "protocol", res
    assert "unparseable" in res.detail


# --- (g) the timeout argument --------------------------------------------------------


@pytest.mark.parametrize(
    "timeout",
    [None, float("nan"), float("inf"), float("-inf"), "5", True, False, [1]],
    ids=["None", "nan", "inf", "-inf", "str", "True", "False", "list"],
)
def test_g_invalid_timeout_is_rejected_with_no_socket(monkeypatch, timeout):
    spy = _patch_socket(monkeypatch)
    res = ec._call("m", {}, request_id="rid-t", timeout=timeout, socket_path=_sp())
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "invalid_timeout"
    assert res.request_id == "rid-t"
    assert type(timeout).__name__ in res.detail and repr(timeout) in res.detail
    assert res.remedy == f"pass a finite number of seconds, at most {ec.MAX_CALL_TIMEOUT_SECONDS}"
    assert spy.calls == 0


@pytest.mark.parametrize(
    "timeout",
    [3601, 3600.5, 1e10, 2**63, 10**400],
    ids=["3601", "3600.5", "1e10", "2**63", "10**400"],
)
def test_g_timeout_over_the_cap_is_rejected_with_no_socket(monkeypatch, timeout):
    """#3717 (Borges): a finite timeout above MAX_CALL_TIMEOUT_SECONDS is refused
    before any I/O. Above about 9.2e9 s the platform's settimeout overflows, which
    without this check lands on `internal`. Mutation: drop the cap comparison ->
    red (1e10 / 2**63 / 10**400 reach the guard; 3601 / 3600.5 reach the socket)."""
    spy = _patch_socket(monkeypatch)
    res = ec._call("m", {}, request_id="rid-cap", timeout=timeout, socket_path=_sp())
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "invalid_timeout"
    assert spy.calls == 0


def test_g_timeout_at_the_cap_is_accepted():
    """The cap itself is a valid timeout: the call reaches the owner."""
    owner = _FakeOwner(b'{"status":"ok","result":1}\n')
    try:
        res = ec._call(
            "m", {}, request_id="r", timeout=ec.MAX_CALL_TIMEOUT_SECONDS, socket_path=owner.path
        )
    finally:
        owner.close()
    assert isinstance(res, ec.Ok), res


@pytest.mark.parametrize("via", ["read", "write"])
def test_g_public_wrappers_reject_invalid_timeout(monkeypatch, via):
    spy = _patch_socket(monkeypatch)
    fn = ec.read if via == "read" else ec.write
    res = fn("m", {}, timeout=None, socket_path=_sp())
    assert isinstance(res, ec.Rejected) and res.reason == "invalid_timeout"
    assert spy.calls == 0


@pytest.mark.parametrize(
    "timeout",
    [0, -1, -0.5, 0.0, -10**400, -1e308, -10**30],
    ids=["0", "-1", "-0.5", "0.0", "-10**400", "-1e308", "-10**30"],
)
def test_g_zero_or_negative_timeout_is_still_unavailable_timeout(timeout):
    owner = _FakeOwner(b'{"status":"ok","result":1}\n')
    try:
        res = ec._call("m", {}, request_id="r", timeout=timeout, socket_path=owner.path)
    finally:
        owner.close()
    assert isinstance(res, ec.Unavailable) and res.reason == "timeout", res


# --- (h) the last-resort guard: `internal` --------------------------------------------


def test_h_internal_is_a_reason_with_a_row_and_no_owner_claim():
    assert "internal" in ec.UNAVAILABLE_REASONS
    assert "internal" in ec._REMEDY_STEPS
    assert set(ec._REMEDY_STEPS) | set(ec._BUILDER_REASONS) == set(ec.UNAVAILABLE_REASONS)
    r = _mk("internal")
    assert r.reason == "internal"
    assert r.agent_steps and r.human_steps
    assert not any(s.startswith(DOWN_RESTART_PREFIX) for s in _all_steps(r))
    text = " ".join(_all_steps(r)).lower()
    assert "client" in text and "report" in text
    for claim in ("it is up", "is healthy", "is down", "is running", "has crashed"):
        assert claim not in text, claim
    assert "restart the owner" not in text and "(re)start" not in text


class _BoomSocket:
    def __init__(self):
        self.closed = False

    def settimeout(self, t):
        pass

    def connect(self, sp):
        raise RuntimeError("boom")

    def close(self):
        self.closed = True


def test_h_unmapped_exception_returns_internal_and_closes_the_socket(monkeypatch):
    made = []

    def factory(*a, **k):
        made.append(_BoomSocket())
        return made[0]

    monkeypatch.setattr(ec.socket, "socket", factory)
    res = ec._call("m", {}, request_id="rid-i", timeout=1.0, socket_path=_sp())
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "internal"
    assert res.request_id == "rid-i"
    assert "RuntimeError" in res.detail and "boom" in res.detail
    assert made and made[0].closed


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_h_base_exceptions_still_propagate(monkeypatch, exc):
    def factory(*a, **k):
        raise exc()

    monkeypatch.setattr(ec.socket, "socket", factory)
    with pytest.raises(exc):
        ec._call("m", {}, request_id=None, timeout=1.0, socket_path=_sp())


def test_h_exception_with_a_failing_str_still_returns_internal(monkeypatch):
    class Nasty(Exception):
        def __str__(self):
            raise RuntimeError("str failed")

    def factory(*a, **k):
        raise Nasty()

    monkeypatch.setattr(ec.socket, "socket", factory)
    res = ec._call("m", {}, request_id=None, timeout=1.0, socket_path=_sp())
    assert isinstance(res, ec.Unavailable) and res.reason == "internal"
    assert "Nasty" in res.detail


# --- (i) the generative cell -------------------------------------------------------------

SEEDS = (3717, 20261011, 42)
CASES_PER_SEED = 90

# Shapes are enumerated and CYCLED, so every shape is reached on every run and the
# seeded RNG varies the details inside a shape. A failure message names the shape,
# the seed and the case index, which reproduce it exactly.


def _gen_params(rng: random.Random, kind: str):
    if kind == "valid":
        return {"a": rng.randint(-5, 5), "b": [rng.random(), "s", None, True], "c": {"d": "e"}}
    if kind == "empty":
        return {}
    if kind == "deep_list":
        return {"x": _nested(rng.choice([2_000, 50_000, 200_000, 300_000]), "list")}
    if kind == "deep_dict":
        return {"x": _nested(rng.choice([2_000, 200_000, 300_000]), "dict")}
    if kind == "set":
        return {"x": {rng.randint(0, 9) for _ in range(3)}}
    if kind == "bytes":
        return {"x": bytes(rng.randrange(256) for _ in range(8))}
    if kind == "nan":
        return {"x": rng.choice([float("nan"), float("inf"), float("-inf")])}
    if kind == "huge_str":
        return {"x": "a" * (2 * 1024 * 1024)}
    if kind == "big_int":
        return {"x": 10 ** rng.choice([4400, 5000])}
    if kind == "circular":
        return {"x": _circular()}
    if kind == "circular_list":
        lst: list = []
        lst.append(lst)
        return {"x": [lst]}
    if kind == "tuple_key":
        return {(1, 2): "v"}
    if kind == "surrogate":
        return {"x": "\ud800 lone"}
    if kind == "mixture":
        return {"deep": _nested(200_000), "set": {1}, "nan": float("nan"), "ok": [1, 2]}
    raise AssertionError(kind)


PARAM_KINDS = (
    "valid", "empty", "deep_list", "deep_dict", "set", "bytes", "nan", "huge_str",
    "big_int", "circular", "circular_list", "tuple_key", "surrogate", "mixture",
)


def _gen_timeout(rng: random.Random, kind: str):
    return {
        "float": lambda: rng.uniform(1.0, 4.0),
        "int": lambda: rng.randint(1, 4),
        "zero": lambda: 0,
        "negative": lambda: -rng.uniform(0.001, 5.0),
        # far below any float deadline: -10**400 overflows `monotonic() + timeout`
        # unless the deadline arithmetic clamps (round-2 review)
        # always beyond float range, so every draw exercises the overflow path
        "huge_negative": lambda: -(10 ** rng.choice([400, 500, 1000])),
        "none": lambda: None,
        "nan": lambda: float("nan"),
        "inf": lambda: rng.choice([float("inf"), float("-inf")]),
        "str": lambda: rng.choice(["5", "", "abc"]),
        "bool": lambda: rng.choice([True, False]),
        # finite but over MAX_CALL_TIMEOUT_SECONDS, including values the platform's
        # settimeout cannot represent (about 9.2e9 s and above)
        "huge": lambda: rng.choice([3601, 1e10, 1e300, 2**63, 10**400]),
    }[kind]()


TIMEOUT_KINDS = (
    "float", "int", "zero", "negative", "none", "nan", "inf", "str", "bool", "huge",
    "huge_negative",
)


def _gen_reply(rng: random.Random, kind: str):
    ok = json.dumps({"status": "ok", "result": {"v": rng.randint(0, 9)}}).encode() + b"\n"
    if kind == "ok":
        return ok
    if kind == "rejected":
        return json.dumps(
            {"status": "rejected", "reason": "r", "detail": "d", "remedy": "m"}
        ).encode() + b"\n"
    if kind == "malformed":
        return rng.choice([b"not json\n", b"{\n", b"{'a': 1}\n", b"\n"])
    if kind == "deep":
        n = rng.choice([1_000, 200_000])
        return ("[" * n + "]" * n + "\n").encode()
    if kind == "deep_ok_status":
        n = 200_000
        return b'{"status":"ok","result":' + b"[" * n + b"]" * n + b"}\n"
    if kind == "truncated":
        return ok[: rng.randint(1, len(ok) - 2)]
    if kind == "oversized":
        return b"x" * (ec.MAX_RESPONSE_BYTES + 4096)
    if kind == "empty":
        return b""
    if kind == "closed":
        return None
    if kind == "wrong_status":
        return json.dumps({"status": rng.choice(["weird", 7, None, [1]])}).encode() + b"\n"
    if kind == "not_dict":
        return rng.choice([b"[1,2]\n", b'"str"\n', b"42\n", b"null\n"])
    if kind == "non_utf8":
        return b"\xff\xfe\x80{\n"
    if kind == "bad_fields":
        return json.dumps(
            {"status": "rejected", "reason": [1], "detail": {"a": 1}, "remedy": None}
        ).encode() + b"\n"
    if kind == "big_int":
        # an integer over Python's int-from-str digit limit (4300): json.loads raises
        # a plain ValueError, not JSONDecodeError (round-2 review). Built as bytes,
        # since json.dumps of such an int would itself raise in the generator.
        digits = b"9" * rng.choice([4_301, 5_000, 50_000])
        return rng.choice([
            b'{"status":"ok","result":' + digits + b"}\n",
            b'{"status":' + digits + b"}\n",
        ])
    raise AssertionError(kind)


REPLY_KINDS = (
    "ok", "rejected", "malformed", "deep", "deep_ok_status", "truncated", "oversized",
    "empty", "closed", "wrong_status", "not_dict", "non_utf8", "bad_fields", "big_int",
)


def _rotated(kinds, n):
    n %= len(kinds)
    return itertools.cycle(kinds[n:] + kinds[:n])


@pytest.mark.parametrize("seed", SEEDS)
def test_i_generative_every_result_is_a_result_type_and_never_internal(seed):
    rng = random.Random(seed)
    # Each axis cycles through its shapes with a per-seed rotation, so the three
    # seeds pair shapes differently. Three phases, one axis each, so no axis can
    # shadow another: a bad timeout is refused before params are serialized, and bad
    # params are refused before the deadline arithmetic runs, so varying both in one
    # case hid the huge-negative timeout (round-2 review follow-up), just as a bad
    # request once hid the reply axis. A cycle advances only in its own phase, and
    # each phase gets CASES_PER_SEED / 3 cases (every shape at least twice):
    #   i % 3 == 0: valid request, generated REPLY;
    #   i % 3 == 1: generated PARAMS, valid timeout, ok reply;
    #   i % 3 == 2: valid params, generated TIMEOUT, ok reply.
    params_cycle = _rotated(PARAM_KINDS, seed)
    timeout_cycle = _rotated(TIMEOUT_KINDS, seed)
    reply_cycle = _rotated(REPLY_KINDS, seed)
    seen = set()
    for i in range(CASES_PER_SEED):
        phase = i % 3
        if phase == 0:
            pk, tk, rk = "valid", "float", next(reply_cycle)
        elif phase == 1:
            pk, tk, rk = next(params_cycle), "float", "ok"
        else:
            pk, tk, rk = "valid", next(timeout_cycle), "ok"
        params = _gen_params(rng, pk)
        timeout = _gen_timeout(rng, tk)
        reply = _gen_reply(rng, rk)
        where = f"seed={seed} case={i} params={pk} timeout={timeout!r} reply={rk}"
        owner = _FakeOwner(reply)
        try:
            try:
                res = ec._call(
                    "m", params, request_id="r", timeout=timeout, socket_path=owner.path
                )
            except Exception as exc:  # noqa: BLE001 -- finding this is the point of the cell
                pytest.fail(f"RAISED {type(exc).__name__}: {str(exc)[:200]} for {where}")
        finally:
            owner.close()
        assert isinstance(res, (ec.Ok, ec.Unavailable, ec.Rejected)), (where, res)
        if isinstance(res, ec.Unavailable):
            assert res.reason != "internal", f"reached internal ({res.detail}) for {where}"
        seen.add(type(res).__name__)
    # not vacuous: every result type is produced by this seed's cases
    assert seen == {"Ok", "Unavailable", "Rejected"}, seen
