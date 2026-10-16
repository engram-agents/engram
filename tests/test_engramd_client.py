"""Contract tests for src/engram/engramd_client.py -- #2652 Phase-1 Unit 1
(U1), issue #3094.

This unit is a pure client library with no owner process to talk to yet
(U3 builds the real `engramd` owner later) -- so this file builds its own
small fake AF_UNIX socket server (`FakeEngramdServer` below) as a test
fixture, standing in for the not-yet-built owner.

The 8 numbered tests below are issue #3094's body, verbatim -- this is the
authoritative list (see the spec at engram-alpha#3094 / the U1 fairy spec
for the full text each test derives from):

  1. Three states, distinct at the TYPE level.
  2. Socket missing or refused -> Unavailable, naming the socket path and
     the bound, with non-empty agent_steps AND non-empty human_steps.
  3. Owner accepts but never answers -> Unavailable within the bound.
  4. Owner replies with a refusal -> Rejected(reason, detail, remedy),
     never Unavailable.
  5. Contract version: MAJOR mismatch -> Rejected(reason="contract_version");
     MINOR mismatch -> accepted.
  6. request_id on every write; read() structurally cannot take one.
  7. No fallback, ever -- sqlite3.connect must never be invoked, and
     engramd_client.py must contain zero references to sqlite3 in its
     source.
  8. No manufactured null -- Unavailable().value / Rejected().value raise
     AttributeError, never return None.

Plus the discrimination control (contract-first can-it-fail, per this
repo's CLAUDE.md): mutating the `except socket.timeout:` branch in
engramd_client.py to `return Ok(...)` instead of `Unavailable(...)` must
turn tests 3 and 8 red while leaving every other test green. That mutation
is run by hand (not part of this automated file) -- see the fairy handoff
for the actual mutated-run pytest output.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402

ENGRAMD_CLIENT_PATH = REPO_ROOT / "src" / "engram" / "engramd_client.py"


# ---------------------------------------------------------------------------
# Fake owner socket server -- stands in for the not-yet-built engramd owner.
# ---------------------------------------------------------------------------


class _Trickle:
    """A `handler` return value meaning "send these byte pieces one at a
    time, sleeping `interval` seconds between each" -- recognized by
    `FakeEngramdServer._handle` (see there). Used to simulate a
    slowly-trickling owner: individual writes are fast, but the response
    as a whole never completes a `\\n`-terminated line within the test's
    timeout budget (round-5 should-fix 1 -- end-to-end deadline, not
    per-socket-op)."""

    def __init__(self, pieces: list[bytes], interval: float):
        self.pieces = pieces
        self.interval = interval


class FakeEngramdServer:
    """A minimal AF_UNIX accept loop. Each test supplies its own `handler`
    callable: `handler(request: dict) -> dict | None`. A `None` return means
    "receive the request, then send nothing" -- used to simulate a hung
    owner (the handler itself sleeps past the client's timeout before
    returning None).

    A ~30-line manual accept-loop, per the spec's own steer: the per-test
    behaviors here are varied enough (refuse to bind, accept-then-hang,
    accept-then-ok, accept-then-rejected, accept-then-check-version) that
    reaching for socketserver.UnixStreamServer's framework machinery would
    add ceremony without buying clarity.
    """

    def __init__(self, socket_path: Path, handler):
        self.socket_path = socket_path
        self.handler = handler
        self.received: list[dict] = []
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.socket_path))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        if not data:
            return
        request = json.loads(data.decode("utf-8").strip())
        self.received.append(request)
        response = self.handler(request)
        if response is None:
            return
        if isinstance(response, bytes):
            # Sent as-is -- no JSON encoding, no trailing '\n' added. Used
            # by the round-5 response-size-cap test to send a fast, huge,
            # never-terminated payload.
            conn.sendall(response)
            return
        if isinstance(response, _Trickle):
            # Sent one piece at a time, sleeping `interval` seconds between
            # each -- used by the round-5 end-to-end-deadline test to
            # simulate a slowly-trickling owner (small successful ops,
            # repeated, never completing a '\n'-terminated line).
            for piece in response.pieces:
                conn.sendall(piece)
                time.sleep(response.interval)
            return
        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)


@pytest.fixture
def sock_dir():
    """A short-path temp directory for AF_UNIX socket files.

    AF_UNIX socket paths are constrained by the kernel's sockaddr_un
    buffer (sun_path -- ~104 bytes on macOS/BSD, 108 on Linux). pytest's
    own `tmp_path` fixture nests under a long per-test directory name
    (.../pytest-of-<user>/pytest-<N>/<test-name><N>/) that routinely
    exceeds this limit on macOS, where TMPDIR is already a long per-user
    sandboxed path -- `socket.bind(...)` then raises `OSError: AF_UNIX
    path too long` (measured live on this host while writing this file:
    a 129-char tmp_path-derived path failed `.bind()` outright).
    tests/test_surface_daemon_pid_reap.py works around the identical
    constraint the same way -- a short `tempfile.TemporaryDirectory()`,
    never pytest's nested `tmp_path`, for anything that binds an AF_UNIX
    socket -- so this fixture follows that established repo convention
    rather than inventing a new one."""
    d = tempfile.mkdtemp(prefix="ecd-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_server(sock_dir):
    """Factory fixture: call it with a handler to get a started
    FakeEngramdServer bound at a sock_dir-scoped socket (see sock_dir's
    own docstring for why this is not pytest's tmp_path). All servers
    created via the factory are stopped at teardown."""
    servers: list[FakeEngramdServer] = []

    def _make(handler, name: str = "engramd.sock") -> FakeEngramdServer:
        server = FakeEngramdServer(sock_dir / name, handler)
        server.start()
        servers.append(server)
        return server

    yield _make

    for server in servers:
        server.stop()


# ---------------------------------------------------------------------------
# Contract test 7 support -- no fallback, ever. sqlite3.connect must never
# be invoked by engramd_client, on ANY path, success or failure. Applied to
# every test in this file via autouse.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forbid_sqlite3_connect(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise AssertionError(
            "sqlite3.connect must never be called by engramd_client -- "
            "no fallback, ever (docs/2652-engrammanager-design.md SS5.1)."
        )

    monkeypatch.setattr(sqlite3, "connect", _raise)


# ---------------------------------------------------------------------------
# Test 1 -- three states, distinct at the TYPE level.
# ---------------------------------------------------------------------------


def test_three_states_distinct_at_type_level():
    ok = ec.Ok(result={"a": 1})
    unavailable = ec.Unavailable(
        socket_path="/tmp/does-not-matter.sock",
        bound=1.0,
        agent_steps=["retry"],
        human_steps=["restart session"],
    )
    rejected = ec.Rejected(reason="r", detail="d", remedy="m")

    assert isinstance(ok, ec.Ok)
    assert not isinstance(ok, ec.Unavailable)
    assert not isinstance(ok, ec.Rejected)

    assert isinstance(unavailable, ec.Unavailable)
    assert not isinstance(unavailable, ec.Ok)
    assert not isinstance(unavailable, ec.Rejected)

    assert isinstance(rejected, ec.Rejected)
    assert not isinstance(rejected, ec.Ok)
    assert not isinstance(rejected, ec.Unavailable)

    # Unavailable and Rejected must never be the same class with a
    # discriminator field -- they are genuinely distinct classes.
    assert ec.Unavailable is not ec.Rejected
    assert type(unavailable) is not type(rejected)


# ---------------------------------------------------------------------------
# Test 2 -- socket missing or refused -> Unavailable, naming the socket path
# and the bound, with non-empty agent_steps AND non-empty human_steps.
# ---------------------------------------------------------------------------


def test_socket_missing_returns_unavailable(sock_dir):
    missing = sock_dir / "genuinely-never-created.sock"
    result = ec.read("engram_query", {}, timeout=0.3, socket_path=missing)

    assert isinstance(result, ec.Unavailable)
    assert result.socket_path == str(missing)
    assert result.bound == 0.3
    assert len(result.agent_steps) > 0
    assert len(result.human_steps) > 0


def test_socket_refused_returns_unavailable(sock_dir):
    refused = sock_dir / "refused.sock"
    # Bind but never listen, then close -- leaves a socket file on disk
    # with no active listener, so a connect() attempt gets
    # ConnectionRefusedError (verified live before writing this test).
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dummy.bind(str(refused))
    dummy.close()

    result = ec.read("engram_query", {}, timeout=0.3, socket_path=refused)

    assert isinstance(result, ec.Unavailable)
    assert result.socket_path == str(refused)
    assert result.bound == 0.3
    assert len(result.agent_steps) > 0
    assert len(result.human_steps) > 0


# ---------------------------------------------------------------------------
# Test 3 -- owner accepts but never answers -> Unavailable within the bound.
# ---------------------------------------------------------------------------


def test_owner_accepts_but_never_answers_returns_unavailable_within_bound(
    fake_server,
):
    bound = 0.3

    def handler(_request):
        time.sleep(bound * 3)  # sleep well past the client's own bound
        return None  # never send a response

    server = fake_server(handler)

    start = time.monotonic()
    result = ec.read("engram_query", {}, timeout=bound, socket_path=server.socket_path)
    elapsed = time.monotonic() - start

    assert isinstance(result, ec.Unavailable)
    assert elapsed < bound * 2  # returns in ~the bound, not a hang


# ---------------------------------------------------------------------------
# Round 5, should-fix 1 -- _call()'s timeout is an END-TO-END deadline, not
# a per-socket-op budget. socket.settimeout() applies its configured value
# to EACH blocking call independently (connect/sendall/each recv), so the
# OLD single settimeout(timeout) at entry let a slowly-trickling owner
# (small successful ops, repeated, never completing a '\n'-terminated line)
# make the total call take an unbounded multiple of the requested bound --
# Borges measured 15x on a fake owner that eventually closed. Test 3 above
# does NOT catch this: under total silence there is exactly one blocking
# recv() that never returns, so its single fresh timeout correctly fires at
# the bound -- the gap is a TRICKLING owner, never exercised by test 3.
# ---------------------------------------------------------------------------


def test_trickling_owner_is_bounded_by_end_to_end_deadline(fake_server):
    """A slowly-trickling owner: individual writes (one byte every 0.1s)
    are each comfortably within the timeout budget, so no SINGLE recv()
    call itself times out -- only ACCUMULATED elapsed time can be what
    trips the result. The response never completes a '\\n'-terminated
    line within the test's run (20 pieces * 0.1s = 2.0s of trickle, well
    past timeout * 1.6 = 1.6s), so this must be bounded by the OVERALL
    deadline, not given a fresh per-op allowance forever."""
    timeout = 1.0
    trickle_interval = 0.1  # fast enough that no individual recv() itself times out
    num_pieces = 20  # 20 * 0.1s = 2.0s of trickle -- safely past timeout * 1.6 = 1.6s

    def handler(_request):
        # Content is irrelevant (never parsed -- it's never a complete
        # '\n'-terminated line within this test's window); only that no
        # single piece contains '\n', so accumulation never completes.
        return _Trickle(pieces=[b"x"] * num_pieces, interval=trickle_interval)

    server = fake_server(handler)

    start = time.monotonic()
    result = ec.read("engram_query", {}, timeout=timeout, socket_path=server.socket_path)
    elapsed = time.monotonic() - start

    assert isinstance(result, ec.Unavailable)
    # Generous enough to avoid flakiness, tight enough to prove it isn't
    # the old 15x-style overshoot (Borges measured 30.0s against a 2.0s
    # bound on the unrelated-hook analog of this bug).
    assert timeout <= elapsed < timeout * 1.6, f"elapsed={elapsed}, timeout={timeout}"
    # Not "unparseable response" -- that was the OLD bug's misleading
    # symptom when the fake eventually closed after the client had already
    # overshot the bound waiting on a fresh per-op timeout every recv().
    assert "unparseable" not in result.detail.lower(), result.detail
    assert "within the bound" in result.detail


def test_response_exceeding_size_cap_returns_unavailable(fake_server):
    """A fast-but-huge owner response (no '\\n' anywhere, so it never
    completes a line) must be capped at MAX_RESPONSE_BYTES rather than
    accumulated without bound -- Borges's own aside in the should-fix-1
    finding ("data grows without a cap"). Sent as a single large, fast
    write -- well within the timeout budget -- so this test proves the
    SIZE cap fires, not the timeout."""
    oversized = b"x" * (ec.MAX_RESPONSE_BYTES + 1024)  # comfortably over the cap; no '\n' anywhere

    def handler(_request):
        return oversized  # raw bytes, sent as-is -- no JSON wrapping, no trailing '\n'

    server = fake_server(handler)

    result = ec.read("engram_query", {}, timeout=5.0, socket_path=server.socket_path)

    assert isinstance(result, ec.Unavailable)
    # Distinct from the timeout detail text -- a size-cap failure is a
    # different failure reason, not to be merged into the timeout wording.
    assert "within the bound" not in result.detail
    assert str(ec.MAX_RESPONSE_BYTES) in result.detail
    assert "byte" in result.detail.lower()


# ---------------------------------------------------------------------------
# Test 4 -- owner replies with a refusal -> Rejected(reason, detail, remedy),
# never Unavailable.
# ---------------------------------------------------------------------------


def test_owner_refusal_returns_rejected_not_unavailable(fake_server):
    def handler(_request):
        return {
            "status": "rejected",
            "reason": "schema_violation",
            "detail": "params.node_id is required",
            "remedy": "supply a valid node_id and retry",
        }

    server = fake_server(handler)

    result = ec.read("engram_query", {}, timeout=1.0, socket_path=server.socket_path)

    assert isinstance(result, ec.Rejected)
    assert not isinstance(result, ec.Unavailable)
    assert result.reason == "schema_violation"
    assert result.detail == "params.node_id is required"
    assert result.remedy == "supply a valid node_id and retry"


# ---------------------------------------------------------------------------
# Test 5 -- contract version: MAJOR mismatch -> Rejected(contract_version);
# MINOR mismatch -> accepted. The fake server enacts the policy by calling
# the SHIPPED library function ec.check_contract_version() -- not a
# reimplementation -- so this test genuinely exercises library code (round-5
# should-fix 2: the old inline reimplementation meant no possible change to
# engramd_client.py could ever fail this test).
# ---------------------------------------------------------------------------


def _make_version_checking_handler(owner_version: str):
    """Fake-server handler enacting the MAJOR/MINOR contract-version policy
    under test via ec.check_contract_version() -- the owner's own version is
    `owner_version`; the client's sent version is whatever's on the wire."""

    def handler(request):
        sent = request.get("contract_version", "")
        rejection = ec.check_contract_version(sent, owner=owner_version)
        if rejection is not None:
            return {
                "status": "rejected",
                "reason": rejection.reason,
                "detail": rejection.detail,
                "remedy": rejection.remedy,
            }
        return {"status": "ok", "result": {"echo": sent}}

    return handler


def test_contract_version_major_mismatch_is_rejected(fake_server):
    # Owner is on "2.0"; this client sends "1.1" (CONTRACT_VERSION) -- MAJOR mismatch.
    server = fake_server(_make_version_checking_handler(owner_version="2.0"))

    result = ec.read("engram_query", {}, timeout=1.0, socket_path=server.socket_path)

    assert isinstance(result, ec.Rejected)
    assert result.reason == "contract_version"
    assert result.remedy


def test_contract_version_minor_mismatch_is_accepted(fake_server):
    # A GENUINELY different MINOR: client sends "1.1" (CONTRACT_VERSION),
    # owner is "1.3" -- MAJOR matches ("1" == "1") but MINOR differs (1 vs
    # 3), so this proves the MINOR-skew-is-accepted branch, not just "same
    # version passes trivially" (the old version of this test's own gap,
    # per Borges's finding: it sent the identical version string, so MINOR
    # skew was never actually exercised).
    assert ec.CONTRACT_VERSION == "1.1"
    server = fake_server(_make_version_checking_handler(owner_version="1.3"))

    result = ec.read("engram_query", {}, timeout=1.0, socket_path=server.socket_path)

    assert isinstance(result, ec.Ok)
    assert not isinstance(result, ec.Rejected)


# ---------------------------------------------------------------------------
# Round 5, should-fix 2 -- unit-level tests for check_contract_version()
# itself, no socket/fake-server involved. These are what makes test 5 above
# exercise REAL library code rather than a test-fixture reimplementation.
# ---------------------------------------------------------------------------


def test_check_contract_version_major_mismatch_is_rejected():
    result = ec.check_contract_version("2.0", owner="1.0")
    assert isinstance(result, ec.Rejected)
    assert result.reason == "contract_version"
    assert result.remedy


def test_check_contract_version_minor_skew_same_major_is_accepted():
    result = ec.check_contract_version("1.0", owner="1.3")
    assert result is None


def test_check_contract_version_identical_version_is_accepted():
    result = ec.check_contract_version("1.0", owner="1.0")
    assert result is None


def test_check_contract_version_malformed_sent_is_rejected():
    for malformed in ("not-a-version", "1", "", "1.0.0", "a.b", "1."):
        result = ec.check_contract_version(malformed, owner="1.0")
        assert isinstance(result, ec.Rejected), f"expected Rejected for sent={malformed!r}"
        assert result.reason == "contract_version"


def test_check_contract_version_none_sent_is_rejected_not_raised():
    # Round 6 -- reviewer-fairy finding on round 5: check_contract_version()'s
    # own docstring promises "never raises", but a non-string sent value (e.g.
    # JSON null on the wire, which a plain dict.get(key, default) does NOT
    # substitute a default for) crashes _parse_major_minor's None.split(".")
    # with an uncaught AttributeError instead of returning the documented
    # Rejected(reason="contract_version") malformed-input outcome. This must
    # not raise, and must return Rejected (not None -- a None sent-version is
    # malformed input, not a valid one).
    result = ec.check_contract_version(None, owner="1.0")
    assert isinstance(result, ec.Rejected), f"expected Rejected for sent=None, got {result!r}"
    assert result.reason == "contract_version"


# ---------------------------------------------------------------------------
# Test 6 -- request_id on every write; read() structurally cannot take one.
# ---------------------------------------------------------------------------


def test_write_generates_request_id_and_echoes_it_back(fake_server):
    def handler(_request):
        return {"status": "ok", "result": {"created": True}}

    server = fake_server(handler)

    result = ec.write("engram_add_observation", {"claim": "x"}, timeout=1.0, socket_path=server.socket_path)

    assert isinstance(result, ec.Ok)
    assert result.request_id is not None
    assert len(server.received) == 1
    assert server.received[0]["request_id"] == result.request_id


def test_write_reuses_explicit_request_id_verbatim(fake_server):
    def handler(_request):
        return {"status": "ok", "result": {"created": True}}

    server = fake_server(handler)

    explicit_id = "retry-of-a-prior-attempt-1234"
    result1 = ec.write(
        "engram_add_observation",
        {"claim": "x"},
        request_id=explicit_id,
        timeout=1.0,
        socket_path=server.socket_path,
    )
    result2 = ec.write(
        "engram_add_observation",
        {"claim": "x"},
        request_id=explicit_id,
        timeout=1.0,
        socket_path=server.socket_path,
    )

    assert result1.request_id == explicit_id
    assert result2.request_id == explicit_id
    assert len(server.received) == 2
    assert server.received[0]["request_id"] == explicit_id
    assert server.received[1]["request_id"] == explicit_id


def test_write_unavailable_and_rejected_also_echo_request_id(fake_server, sock_dir):
    # Unavailable path.
    missing = sock_dir / "no-owner-here.sock"
    explicit_id = "explicit-retry-id"
    unavailable_result = ec.write(
        "engram_add_observation",
        {"claim": "x"},
        request_id=explicit_id,
        timeout=0.3,
        socket_path=missing,
    )
    assert isinstance(unavailable_result, ec.Unavailable)
    assert unavailable_result.request_id == explicit_id

    # Rejected path.
    def handler(_request):
        return {
            "status": "rejected",
            "reason": "schema_violation",
            "detail": "bad params",
            "remedy": "fix params",
        }

    server = fake_server(handler)
    rejected_result = ec.write(
        "engram_add_observation",
        {"claim": "x"},
        request_id=explicit_id,
        timeout=1.0,
        socket_path=server.socket_path,
    )
    assert isinstance(rejected_result, ec.Rejected)
    assert rejected_result.request_id == explicit_id


def test_read_has_no_request_id_parameter():
    # Enforced by the function signature itself, not a runtime check --
    # read(..., request_id="x") must raise TypeError.
    with pytest.raises(TypeError):
        ec.read("engram_query", {}, request_id="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Test 7 -- no fallback, ever. sqlite3.connect must never be invoked, on
# every failure path AND the success path. The autouse _forbid_sqlite3_connect
# fixture above already guards every test in this file; these tests below
# exercise the specific failure/success paths explicitly so the guard is
# demonstrably exercised, plus the static source check.
# ---------------------------------------------------------------------------


def test_no_sqlite3_on_missing_socket_path(sock_dir):
    missing = sock_dir / "still-missing.sock"
    result = ec.read("engram_query", {}, timeout=0.3, socket_path=missing)
    assert isinstance(result, ec.Unavailable)  # sqlite3.connect raising would have failed the test instead


def test_no_sqlite3_on_refused_path(sock_dir):
    refused = sock_dir / "still-refused.sock"
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dummy.bind(str(refused))
    dummy.close()
    result = ec.read("engram_query", {}, timeout=0.3, socket_path=refused)
    assert isinstance(result, ec.Unavailable)


def test_no_sqlite3_on_timeout_path(fake_server):
    def handler(_request):
        time.sleep(0.6)
        return None

    server = fake_server(handler)
    result = ec.read("engram_query", {}, timeout=0.2, socket_path=server.socket_path)
    assert isinstance(result, ec.Unavailable)


def test_no_sqlite3_on_rejected_path(fake_server):
    def handler(_request):
        return {"status": "rejected", "reason": "x", "detail": "y", "remedy": "z"}

    server = fake_server(handler)
    result = ec.read("engram_query", {}, timeout=1.0, socket_path=server.socket_path)
    assert isinstance(result, ec.Rejected)


def test_no_sqlite3_on_success_path(fake_server):
    def handler(_request):
        return {"status": "ok", "result": {"nodes": []}}

    server = fake_server(handler)
    result = ec.read("engram_query", {}, timeout=1.0, socket_path=server.socket_path)
    assert isinstance(result, ec.Ok)


def test_engramd_client_source_has_zero_sqlite3_references():
    source = ENGRAMD_CLIENT_PATH.read_text(encoding="utf-8")
    assert "sqlite3" not in source


# ---------------------------------------------------------------------------
# Test 8 -- no manufactured null. Unavailable().value / Rejected().value
# raise AttributeError, never return None.
# ---------------------------------------------------------------------------


def test_unavailable_value_raises_attribute_error(fake_server):
    """Sourced via the TIMEOUT branch specifically (not the missing-socket
    branch) -- this is what makes this test discriminate against the
    discrimination control's mutation of that exact branch (see module
    docstring). An Unavailable constructed directly, or sourced via a
    DIFFERENT branch, would stay green under that mutation: it would keep
    testing the dataclass's own .value property in isolation rather than
    testing what _call() actually hands back on a real timeout, and would
    silently fail to catch the manufactured-null regression the mutation
    simulates (a timeout silently reported as Ok(result=None) instead of
    Unavailable -- .value would then return None without raising)."""
    bound = 0.3

    def handler(_request):
        time.sleep(bound * 3)  # sleep well past the client's own bound
        return None  # never send a response

    server = fake_server(handler)
    result = ec.read("engram_query", {}, timeout=bound, socket_path=server.socket_path)

    assert isinstance(result, ec.Unavailable)
    with pytest.raises(AttributeError):
        result.value


def test_rejected_value_raises_attribute_error(fake_server):
    def handler(_request):
        return {"status": "rejected", "reason": "r", "detail": "d", "remedy": "m"}

    server = fake_server(handler)
    result = ec.read("engram_query", {}, timeout=1.0, socket_path=server.socket_path)

    assert isinstance(result, ec.Rejected)
    with pytest.raises(AttributeError):
        result.value


# ---------------------------------------------------------------------------
# Round 6 (#3194): Rejected carries the owner's optional `commit` marker.
# ---------------------------------------------------------------------------


def test_rejected_commit_marker_is_populated_from_the_wire_and_defaults_to_none(fake_server):
    def handler(request):
        if request["method"] == "with_commit":
            return {
                "status": "rejected",
                "reason": "deadline_exceeded",
                "commit": "indeterminate",
                "detail": "d",
                "remedy": "r",
            }
        return {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"}

    server = fake_server(handler)
    marked = ec.read("with_commit", {}, timeout=1.0, socket_path=server.socket_path)
    plain = ec.read("without_commit", {}, timeout=1.0, socket_path=server.socket_path)

    assert isinstance(marked, ec.Rejected) and marked.commit == "indeterminate"
    assert isinstance(plain, ec.Rejected) and plain.commit is None


def test_rejected_positional_construction_and_equality_unchanged_by_commit_field():
    # existing positional shape still works; `commit` is last, defaulted
    a = ec.Rejected("r", "d", "m", "rid")
    assert (a.reason, a.detail, a.remedy, a.request_id, a.commit) == ("r", "d", "m", "rid", None)
    assert a == ec.Rejected(reason="r", detail="d", remedy="m", request_id="rid")
    assert a != ec.Rejected("r", "d", "m", "rid", commit="indeterminate")
