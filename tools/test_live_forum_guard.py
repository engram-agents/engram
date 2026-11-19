"""Cells for the live-forum connect guard (#4475), tools/ tree. The guard's ONE definition is the
repo-root conftest.py; this file runs its contract in this tree so a missing wiring here is red.

Safety: the connect cells really try port 5002, so each first fails (no network) unless the guard
wrapper is installed. Run mutants of the guard LOGIC only under a recorder that refuses 5002 beneath it.
"""

from __future__ import annotations

import asyncio
import errno
import http.client
import importlib.util
import json
import shutil
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFTEST = REPO_ROOT / "conftest.py"
_WRAPPER_ATTR = "_live_forum_guard_4475"


def _load_guard_module():
    """The ONE definition lives in the root conftest.py; load it by file path (never sys.path)."""
    spec = importlib.util.spec_from_file_location("_live_forum_guard_probe_4475", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _load_guard_module()


def _require_guard():
    """Fail (no network) when this tree is not wired. The connect cells below really try the live
    forum port, so they must never run with the guard absent: a missing wiring is red HERE."""
    if getattr(socket.socket.connect, _WRAPPER_ATTR, None) is None or getattr(
        socket.socket.connect_ex, _WRAPPER_ATTR, None
    ) is None:
        pytest.fail("#4475: the live-forum guard is not installed in this test tree")


def _refused_count():
    return len(getattr(socket.socket.connect, _WRAPPER_ATTR))


# ---------------------------------------------------------------- pure function (G1, G2)

def test_G1_env_forum_url_is_in_the_endpoint_set(tmp_path):
    eps = G.live_forum_endpoints({"FORUM_URL": "http://localhost:5099"}, tmp_path / "absent.json")
    assert ("localhost", 5099) in eps


def test_G1_default_pair_always_present_and_read_from_the_source_files(tmp_path):
    eps = G.live_forum_endpoints({}, tmp_path / "absent.json")
    assert ("localhost", 5002) in eps
    # the default is READ from the code's constant, not retyped: a repo file whose constant says 5077 moves it
    fake = tmp_path / "repo"
    for rel in G.DEFAULT_URL_SOURCES:
        p = fake / rel
        p.parent.mkdir(parents=True)
        p.write_text('_DEFAULT_FORUM_URL = "http://localhost:5077"\n')
    assert ("localhost", 5077) in G.live_forum_endpoints({}, tmp_path / "absent.json", repo_root=fake)


def test_G2_config_url_on_a_non_loopback_host_is_in_the_set_and_matches(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"forum": {"url": "http://forum.example.test:5002/"}}))
    eps = G.live_forum_endpoints({}, cfg)
    assert ("forum.example.test", 5002) in eps
    assert ("localhost", 5002) in eps
    assert G.is_live_forum_target(("forum.example.test", 5002), eps)
    assert G.is_live_forum_target(("FORUM.example.test", 5002), eps)  # host case is not a way round
    assert not G.is_live_forum_target(("forum.example.test", 5003), eps)
    assert not G.is_live_forum_target(("other.example.test", 5002), eps)


def test_G2_unreadable_or_malformed_config_is_ignored(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    assert ("localhost", 5002) in G.live_forum_endpoints({}, bad)
    bad.write_text(json.dumps({"forum": {"url": 12}}))
    assert ("localhost", 5002) in G.live_forum_endpoints({}, bad)


def test_matcher_covers_loopback_names_and_leaves_other_ports_alone():
    eps = {("localhost", 5002)}
    for host in ("localhost", "127.0.0.1", "::1"):
        assert G.is_live_forum_target((host, 5002), eps), host
        assert not G.is_live_forum_target((host, 5999), eps), host
    assert not G.is_live_forum_target("/tmp/some.sock", eps)  # a unix address


# ---------------------------------------------------------------- the installed guard (G1, G4, G7)

def test_guard_is_active_in_this_tree():
    _require_guard()


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_G1_connect_to_the_live_port_is_refused_and_names_the_guard(host):
    _require_guard()
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("no IPv6 sockets on this host")
    before = _refused_count()
    with s:
        with pytest.raises(ConnectionRefusedError) as ei:
            s.connect((host, 5002))
        assert "#4475" in str(ei.value) and "guard" in str(ei.value)
    assert _refused_count() == before + 1


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_G1_connect_ex_to_the_live_port_returns_econnrefused(host):
    _require_guard()
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("no IPv6 sockets on this host")
    before = _refused_count()
    with s:
        assert s.connect_ex((host, 5002)) == errno.ECONNREFUSED
    # ECONNREFUSED alone cannot tell the guard from a refusing recorder beneath it: the guard counts its own
    assert _refused_count() == before + 1


def test_G4_unmocked_urlopen_of_the_default_forum_url_is_refused_by_the_guard():
    _require_guard()
    before = _refused_count()
    with pytest.raises(urllib.error.URLError) as ei:
        urllib.request.urlopen("http://localhost:5002/api/agents/status", timeout=2)
    reason = ei.value.reason
    assert isinstance(reason, ConnectionRefusedError)
    assert "#4475" in str(reason) and "guard" in str(reason)
    assert _refused_count() == before + 1  # the guard refused it; no connect ever ran


def test_other_stdlib_connect_paths_are_refused():
    _require_guard()
    with pytest.raises(ConnectionRefusedError, match="#4475"):
        socket.create_connection(("127.0.0.1", 5002), timeout=2)
    conn = http.client.HTTPConnection("localhost", 5002, timeout=2)
    with pytest.raises(ConnectionRefusedError, match="#4475"):
        conn.request("GET", "/")
    conn.close()
    sconn = http.client.HTTPSConnection("localhost", 5002, timeout=2)
    with pytest.raises(ConnectionRefusedError, match="#4475"):
        sconn.connect()
    sconn.close()

    async def _go():
        await asyncio.open_connection("127.0.0.1", 5002)

    with pytest.raises(ConnectionRefusedError, match="#4475"):
        asyncio.run(_go())


def test_G3_a_loopback_server_on_an_ephemeral_port_is_not_disturbed():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert port != 5002

    def _echo():
        c, _ = srv.accept()
        with c:
            c.sendall(c.recv(1))

    t = threading.Thread(target=_echo, daemon=True)
    t.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as cli:
            cli.sendall(b"x")
            assert cli.recv(1) == b"x"
    finally:
        t.join(5)
        srv.close()


def test_G3_connect_ex_to_a_non_forum_loopback_port_is_the_real_answer():
    # nothing listens on this port once the probe's server is closed: the REAL connect_ex answers
    # ECONNREFUSED itself, which the guard must not be the source of (refusal counter unmoved).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    before = _refused_count() if getattr(socket.socket.connect, _WRAPPER_ATTR, None) is not None else 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        assert s.connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED
    after = _refused_count() if getattr(socket.socket.connect, _WRAPPER_ATTR, None) is not None else 0
    assert after == before


def test_G7_a_unix_socket_is_not_refused():
    d = tempfile.mkdtemp(prefix="lfg")
    try:
        path = str(Path(d) / "s")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)

        def _echo():
            c, _ = srv.accept()
            with c:
                c.sendall(c.recv(1))

        t = threading.Thread(target=_echo, daemon=True)
        t.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
            cli.settimeout(5)
            cli.connect(path)
            cli.sendall(b"y")
            assert cli.recv(1) == b"y"
        t.join(5)
        srv.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)



# ---------------------------------------------------------------- fairy round 1 on #4903: pins for the config path,
# the real connect_ex answer, non-canonical spellings and teardown


def test_endpoints_resolved_from_an_environment_read_its_config_and_its_forum_url(tmp_path):
    """The session endpoints come from the ENVIRONMENT's config.json and FORUM_URL (the real ones, taken at import)."""
    import json as _json

    (tmp_path / "config.json").write_text(_json.dumps({"forum": {"url": "http://localhost:5099"}}))
    eps = G.resolve_endpoints_from_environ({"ENGRAM_HOME": str(tmp_path), "FORUM_URL": "http://127.0.0.1:5088"})
    assert ("localhost", 5099) in eps and ("127.0.0.1", 5088) in eps and ("localhost", 5002) in eps


def test_a_second_load_of_the_conftest_reuses_the_snapshot_kept_in_the_environment(tmp_path, monkeypatch):
    """The forum tree loads the root conftest by path a second time; it must reuse the first snapshot, not re-resolve
    from whatever ENGRAM_HOME is by then (tests/conftest.py repoints it at a throwaway directory)."""
    import json as _json

    planted = [["localhost", 5002], ["forum.example.test", 5055]]
    monkeypatch.setenv(G._LFG_SNAPSHOT_KEY, _json.dumps(planted))
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))  # an empty home: a fresh resolution could not produce 5055
    again = _load_guard_module().session_live_forum_endpoints()
    assert again == {("localhost", 5002), ("forum.example.test", 5055)}


def test_a_listed_loopback_listener_is_really_refused_by_connect_and_connect_ex(monkeypatch):
    """Pins the connect_ex ANSWER without any recorder: a live listener reports 0 unless the guard refuses."""
    import errno as _errno

    listed, unlisted = socket.socket(), socket.socket()
    try:
        for srv in (listed, unlisted):
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
        port_listed, port_unlisted = listed.getsockname()[1], unlisted.getsockname()[1]
        G.install_live_forum_guard(monkeypatch, endpoints={("127.0.0.1", port_listed)}, force=True)
        with socket.socket() as c:
            assert c.connect_ex(("127.0.0.1", port_listed)) == _errno.ECONNREFUSED
        with socket.socket() as c, pytest.raises(G.LiveForumConnectionRefused):
            c.connect(("127.0.0.1", port_listed))
        with socket.socket() as c:  # the other listener is not listed: the real answer (0) comes through
            assert c.connect_ex(("127.0.0.1", port_unlisted)) == 0
    finally:
        listed.close()
        unlisted.close()


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "127.1", "0x7f.0.0.1", "2130706433", "localhost.", "ip6-localhost", b"127.0.0.1", "::ffff:127.0.0.1", "::ffff:0.0.0.0"])
def test_non_canonical_loopback_spellings_are_matched(host):
    assert G.is_live_forum_target((host, 5002), {("localhost", 5002)}), host
    assert not G.is_live_forum_target((host, 5003), {("localhost", 5002)}), host


def test_the_wrappers_are_undone_when_their_monkeypatch_context_ends():
    """Teardown is pinned: after the context the original connect and connect_ex are back."""
    before = (socket.socket.connect, socket.socket.connect_ex)
    with pytest.MonkeyPatch.context() as mp:
        G.install_live_forum_guard(mp, endpoints=set(), force=True)
        assert (socket.socket.connect, socket.socket.connect_ex) != before
    assert (socket.socket.connect, socket.socket.connect_ex) == before


# ---------------------------------------------------------------- fold 1 on #4903: the guard must hold OUTSIDE a test
# function's own scope (module import at collection, setup_module, module/session fixtures, setUpClass), from
# pytest_configure to pytest_unconfigure. Each probe runs in a CHILD pytest whose conftest puts a RECORDING STUB beneath
# the guard before it loads the conftest under test: with the guard broken a live-port connect is recorded and refused
# by the stub, and no connect to the real port ever happens. A connect to a non-forum loopback port passes through.

_CHILD_CONFTEST = '''
import importlib.util, json, os, socket, sys

_real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex
_REC = os.environ["LFG_REC"]


def _live(address):
    return isinstance(address, tuple) and len(address) >= 2 and address[1] == 5002


def _note(path, obj):
    with open(path, "a") as fh:
        fh.write(json.dumps(obj) + "\\n")


def _stub_connect(self, address):
    if _live(address):
        _note(_REC, {"via": "connect", "address": list(address[:2])})
        raise ConnectionRefusedError(111, "recording stub: the live port was reached")
    return _real_connect(self, address)


def _stub_connect_ex(self, address):
    if _live(address):
        _note(_REC, {"via": "connect_ex", "address": list(address[:2])})
        return 111
    return _real_connect_ex(self, address)


socket.socket.connect, socket.socket.connect_ex = _stub_connect, _stub_connect_ex

_spec = importlib.util.spec_from_file_location("_lfg_child_conftest_under_test", os.environ["LFG_CONFTEST"])
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
if hasattr(_mod, "pytest_configure"):
    pytest_configure = _mod.pytest_configure


def pytest_unconfigure(config):
    if hasattr(_mod, "pytest_unconfigure"):
        _mod.pytest_unconfigure(config)
    _note(os.environ["LFG_RES"], {
        "where": "after_unconfigure",
        "stub_back": socket.socket.connect is _stub_connect and socket.socket.connect_ex is _stub_connect_ex,
    })
'''

_CHILD_PROBE = '''
import errno, json, os, socket, unittest
import pytest


def _probe():
    out = {}
    with socket.socket() as s:
        try:
            s.connect(("127.0.0.1", 5002))
            out["connect"] = "connected"
        except ConnectionRefusedError as e:
            out["connect"] = type(e).__name__
    with socket.socket() as s:
        out["connect_ex"] = s.connect_ex(("127.0.0.1", 5002))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        with socket.socket() as c:
            out["non_forum"] = c.connect_ex(("127.0.0.1", srv.getsockname()[1]))
    finally:
        srv.close()
    return out


def _note(where):
    with open(os.environ["LFG_RES"], "a") as fh:
        fh.write(json.dumps(dict(_probe(), where=where)) + "\\n")


_note("import")


def setup_module(module):
    _note("setup_module")


@pytest.fixture(scope="module")
def _module_fixture():
    _note("module_fixture")


@pytest.fixture(scope="session")
def _session_fixture():
    _note("session_fixture")


class TestInClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _note("setUpClass")

    def test_unittest_cell(self):
        _note("unittest_test_method")


def test_function(_module_fixture, _session_fixture):
    _note("test_function")
'''

_LFG_WHERE = ["import", "setup_module", "module_fixture", "session_fixture", "setUpClass", "test_function"]
_LFG_CHILD_CACHE = {}


def _forum_conftest():
    return REPO_ROOT / "src" / "forum" / "tests" / "conftest.py"


def _child_run(conftest_path):
    """Run the probe under a child pytest; returns (results by place, stub-recorded live attempts, the run)."""
    import os
    import subprocess
    import sys

    key = str(conftest_path)
    if key in _LFG_CHILD_CACHE:
        return _LFG_CHILD_CACHE[key]
    work = Path(tempfile.mkdtemp(prefix="lfg4475child"))
    try:
        (work / "conftest.py").write_text(_CHILD_CONFTEST)
        (work / "test_probe.py").write_text(_CHILD_PROBE)
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
        env.update(LFG_REC=str(work / "rec.jsonl"), LFG_RES=str(work / "res.jsonl"), LFG_CONFTEST=str(conftest_path))
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(work),
             "--confcutdir", str(work), str(work / "test_probe.py")],
            cwd=str(work), env=env, capture_output=True, text=True, timeout=180,
        )

        def _lines(name):
            p = work / name
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

        out = ({x["where"]: x for x in _lines("res.jsonl")}, _lines("rec.jsonl"), r)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _LFG_CHILD_CACHE[key] = out
    return out


_LFG_CONFTESTS = [pytest.param(_CONFTEST, id="root-conftest"), pytest.param(_forum_conftest(), id="forum-conftest")]


@pytest.mark.parametrize("conftest_path", _LFG_CONFTESTS)
@pytest.mark.parametrize("where", _LFG_WHERE)
def test_guard_holds_outside_the_test_function_scope(conftest_path, where):
    by_where, stub_hits, _ = _child_run(conftest_path)
    assert where in by_where, f"the probe at {where} did not run: {sorted(by_where)}"
    got = by_where[where]
    assert got["connect"] == "LiveForumConnectionRefused", got
    assert got["connect_ex"] == errno.ECONNREFUSED, got
    assert got["non_forum"] == 0, got  # a non-forum loopback listener still answers: pass-through
    assert stub_hits == [], "the live-port connect reached the stub beneath the guard"


@pytest.mark.parametrize("conftest_path", _LFG_CONFTESTS)
def test_the_unittest_method_runs_guarded_too(conftest_path):
    by_where, _, _ = _child_run(conftest_path)
    assert by_where["unittest_test_method"]["connect"] == "LiveForumConnectionRefused"


@pytest.mark.parametrize("conftest_path", _LFG_CONFTESTS)
def test_pytest_unconfigure_restores_the_originals(conftest_path):
    by_where, _, r = _child_run(conftest_path)
    assert by_where.get("after_unconfigure", {}).get("stub_back") is True, r.stdout[-1500:]
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-800:]


def test_installing_the_guard_twice_stays_a_no_op():
    with pytest.MonkeyPatch.context() as outer:
        G.install_live_forum_guard(outer, endpoints={("localhost", 5002)}, force=True)
        first = (socket.socket.connect, socket.socket.connect_ex)
        with pytest.MonkeyPatch.context() as inner:
            G.install_live_forum_guard(inner)  # already in place: nothing installed
            assert (socket.socket.connect, socket.socket.connect_ex) == first
        assert (socket.socket.connect, socket.socket.connect_ex) == first
