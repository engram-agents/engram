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
def test_G1_connect_to_the_live_port_is_refused_and_names_the_guard(host, monkeypatch):
    _require_guard()
    G.install_live_forum_guard(monkeypatch, force=True)  # own wrapper, own list: the session list stays clean
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
def test_G1_connect_ex_to_the_live_port_returns_econnrefused(host, monkeypatch):
    _require_guard()
    G.install_live_forum_guard(monkeypatch, force=True)  # own wrapper, own list: the session list stays clean
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


def test_G4_unmocked_urlopen_of_the_default_forum_url_is_refused_by_the_guard(monkeypatch):
    _require_guard()
    G.install_live_forum_guard(monkeypatch, force=True)  # own wrapper, own list: the session list stays clean
    before = _refused_count()
    with pytest.raises(urllib.error.URLError) as ei:
        urllib.request.urlopen("http://localhost:5002/api/agents/status", timeout=2)
    reason = ei.value.reason
    assert isinstance(reason, ConnectionRefusedError)
    assert "#4475" in str(reason) and "guard" in str(reason)
    assert _refused_count() == before + 1  # the guard refused it; no connect ever ran


def test_other_stdlib_connect_paths_are_refused(monkeypatch):
    _require_guard()
    G.install_live_forum_guard(monkeypatch, force=True)  # own wrapper, own list: the session list stays clean
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
for _name in ("pytest_configure", "pytest_sessionfinish", "_live_forum_guard"):
    if hasattr(_mod, _name):
        globals()[_name] = getattr(_mod, _name)


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
    if os.environ.get("LFG_MODE") != "quiet":
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


def test_zz_session_list(request):
    lst = getattr(request.config, "_live_forum_guard_refused_4475", None)
    with open(os.environ["LFG_RES"], "a") as fh:
        fh.write(json.dumps({"where": "session_list", "entries": None if lst is None else [list(e) for e in lst]}) + "\\n")
'''

_LFG_WHERE = ["import", "setup_module", "module_fixture", "session_fixture", "setUpClass", "test_function"]
_LFG_CHILD_CACHE = {}


def _forum_conftest():
    return REPO_ROOT / "src" / "forum" / "tests" / "conftest.py"


def _child_run(conftest_path, mode="live"):
    """Run the probe under a child pytest; returns (results by place, stub-recorded live attempts, the run)."""
    import os
    import subprocess
    import sys

    key = (str(conftest_path), mode)
    if key in _LFG_CHILD_CACHE:
        return _LFG_CHILD_CACHE[key]
    work = Path(tempfile.mkdtemp(prefix="lfg4475child"))
    try:
        (work / "conftest.py").write_text(_CHILD_CONFTEST)
        (work / "test_probe.py").write_text(_CHILD_PROBE)
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
        env.update(LFG_REC=str(work / "rec.jsonl"), LFG_RES=str(work / "res.jsonl"), LFG_CONFTEST=str(conftest_path), LFG_MODE=mode)
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


# ---------------------------------------------------------------- fold 1b on #4903 (A): the endpoint set ALWAYS holds the
# default pair, whatever the snapshot variable carries; (B): refusals under the session install are reported.


def _guard_module_with_snapshot(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))  # an empty home: a fresh resolve yields the default pair only
    monkeypatch.delenv("FORUM_URL", raising=False)
    monkeypatch.setenv(G._LFG_SNAPSHOT_KEY, raw)
    return _load_guard_module()  # importing it takes the snapshot: a malformed value must not crash this


@pytest.mark.parametrize(
    "raw",
    ["[]", '[["forum.example.test", 5055]]', "{not json", '{"a": 1}', '"text"', "null", '[["only-one"]]', '[["localhost", "x"]]', "[[1, 5002]]"],
)
def test_A_whatever_the_snapshot_variable_holds_the_default_pair_is_still_refused(monkeypatch, tmp_path, raw):
    mod = _guard_module_with_snapshot(monkeypatch, tmp_path, raw)
    eps = mod.session_live_forum_endpoints()
    assert ("localhost", 5002) in eps, eps
    for host in ("localhost", "127.0.0.1", "::1"):
        assert mod.is_live_forum_target((host, 5002), eps), host


def test_A_a_preset_may_add_endpoints_and_the_default_stays(monkeypatch, tmp_path):
    mod = _guard_module_with_snapshot(monkeypatch, tmp_path, '[["forum.example.test", 5055]]')
    eps = mod.session_live_forum_endpoints()
    assert mod.is_live_forum_target(("forum.example.test", 5055), eps)
    assert mod.is_live_forum_target(("localhost", 5002), eps)
    assert not mod.is_live_forum_target(("forum.example.test", 5056), eps)


def test_A_the_default_pair_is_read_from_the_source_files_not_retyped(monkeypatch, tmp_path):
    fake = tmp_path / "repo"
    for rel in G.DEFAULT_URL_SOURCES:
        p = fake / rel
        p.parent.mkdir(parents=True)
        p.write_text('_DEFAULT_FORUM_URL = "http://localhost:5077"\n')
    assert ("localhost", 5077) in G.default_forum_endpoints(repo_root=fake)


def _own_session_list(request):
    return getattr(request.config, "_live_forum_guard_refused_4475", None)


@pytest.mark.parametrize("conftest_path", _LFG_CONFTESTS)
def test_B_a_refusal_under_the_session_install_is_recorded_in_the_session_list(conftest_path):
    by_where, stub_hits, _ = _child_run(conftest_path)
    entries = by_where["session_list"]["entries"]
    assert entries is not None, "no session-level list on the config"
    # 7 places probed (import, setup_module, module fixture, session fixture, setUpClass, the unittest method,
    # the function), each one connect and one connect_ex
    assert len(entries) == 14, entries
    assert all(e[0] == "127.0.0.1" and e[1] == 5002 for e in entries), entries
    assert any("test_probe.py" in str(e[2]) for e in entries), entries  # the running test name is recorded
    assert stub_hits == []


@pytest.mark.parametrize("conftest_path", _LFG_CONFTESTS)
def test_B_sessionfinish_prints_one_line_naming_count_targets_and_tests(conftest_path):
    _, stub_hits, r = _child_run(conftest_path)
    out = r.stdout + r.stderr
    lines = [ln for ln in out.splitlines() if "[live-forum guard #4475]" in ln]
    assert len(lines) == 1, out[-1500:]
    assert "14" in lines[0] and "127.0.0.1:5002" in lines[0] and "test_probe.py" in lines[0], lines[0]
    assert r.returncode == 0, out[-1500:]  # report only: it does not fail the run
    assert stub_hits == []


@pytest.mark.parametrize("conftest_path", _LFG_CONFTESTS)
def test_B_sessionfinish_prints_nothing_when_nothing_was_refused(conftest_path):
    by_where, stub_hits, r = _child_run(conftest_path, mode="quiet")
    assert by_where["session_list"]["entries"] == []
    assert "#4475" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-1500:]
    assert r.returncode == 0
    assert stub_hits == []


def test_zz_B_these_cells_leave_the_session_list_empty(request):
    """Cells that provoke refusals use their own forced install and list; a clean run shows an empty session list.
    (Entries from OTHER files in a whole-suite session are theirs to answer for, so only this file's are counted.)"""
    lst = _own_session_list(request)
    assert lst is not None, "the session-level install did not run (no list on the config)"
    mine = [e for e in lst if Path(__file__).name in str(e[2])]
    assert mine == [], mine


# ---------------------------------------------------------------- fold 2 on #4903: a default-URL source that is not usable
# fails CLOSED with ONE named failure. The guard reads the files in DEFAULT_URL_SOURCES relative to the conftest's own
# directory; a copy of the conftest in a tree without them used to die with a bare FileNotFoundError at import (the
# #3519 throwaway repo did), while a missing constant raised the guard's own RuntimeError: two shapes for one problem.
# G1: five ways a source can be unusable, one `RuntimeError("#4475 guard: <relative path>: <reason>")`.
# G2: with the sources absent the endpoint functions raise (never an empty set), and a preset snapshot cannot stand in.
# G3: a copied conftest in a tree WITHOUT the sources fails at collection; the same copy WITH them guards.

import os
import subprocess
import sys

_GOOD_SOURCE = '_DEFAULT_FORUM_URL = "http://localhost:5077"\n'


def _source_tree(root):
    """A tree carrying every default-URL source with a good constant."""
    for rel in G.DEFAULT_URL_SOURCES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_GOOD_SOURCE)
    return root


def _break_source(root, rel, case):
    """Make the source at `rel` unusable in the way `case` names; returns the reason text the guard must give."""
    p = root / rel
    if case == "missing":
        p.unlink()
        return "file missing"
    if case == "directory":  # a directory in place of the file (not a regular file)
        p.unlink()
        p.mkdir()
        return "not a regular file"
    if case == "permission":  # chmod 000 (skipped when running as root, or when the mode does not bite)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("running as root: chmod 000 does not make a file unreadable")
        p.chmod(0)
        try:
            p.read_bytes()
        except PermissionError:
            return "unreadable (permission denied)"
        pytest.skip("chmod 000 did not make the file unreadable on this platform")
    if case == "not-utf8":
        p.write_bytes(b'\xff\xfe_DEFAULT_FORUM_URL = "http://localhost:5077"\n')
        return "not valid UTF-8"
    if case == "no-constant":
        p.write_text("_OTHER = 1\n")
        return "no _DEFAULT_FORUM_URL constant"
    raise AssertionError(case)


_G1_CASES = ["missing", "directory", "permission", "not-utf8", "no-constant"]


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("case", _G1_CASES)
def test_G1_every_unusable_default_url_source_gives_one_named_runtime_error(tmp_path, case, which):
    root = _source_tree(tmp_path / "repo")
    rel = G.DEFAULT_URL_SOURCES[which]
    reason = _break_source(root, rel, case)
    with pytest.raises(Exception) as ei:
        G.live_forum_endpoints({}, tmp_path / "absent.json", repo_root=root)
    exc = ei.value
    assert type(exc) is RuntimeError, (type(exc), exc)  # not OSError / FileNotFoundError / UnicodeDecodeError
    assert not isinstance(exc, OSError)
    msg = str(exc)
    assert msg.startswith(f"#4475 guard: {rel}: "), msg
    assert reason in msg, (reason, msg)


def test_G2_default_forum_endpoints_raises_when_the_sources_are_absent_and_never_returns_an_empty_set(tmp_path):
    with pytest.raises(RuntimeError, match=r"^#4475 guard: .*engram-forum-prompt-hook\.py: file missing"):
        G.default_forum_endpoints(repo_root=tmp_path / "no-sources")


_G2_PRESETS = [None, "[]", '[["localhost", 5002]]', '[["forum.example.test", 5055]]']


@pytest.mark.parametrize("preset", _G2_PRESETS, ids=["no-preset", "empty-list", "default-pair", "other-pair"])
def test_G2_session_endpoints_raise_without_sources_whatever_the_preset_says(tmp_path, monkeypatch, preset):
    """A preset snapshot (even a valid non-empty one) cannot stand in for missing sources; a failed
    attempt leaves the session set unset and the environment as it found it."""
    monkeypatch.setattr(G, "_LFG_REPO_ROOT", tmp_path / "no-sources")
    monkeypatch.setattr(G, "_LFG_SESSION_ENDPOINTS", None)
    monkeypatch.delenv("FORUM_URL", raising=False)
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "home"))
    if preset is None:
        monkeypatch.delenv(G._LFG_SNAPSHOT_KEY, raising=False)
    else:
        monkeypatch.setenv(G._LFG_SNAPSHOT_KEY, preset)
    with pytest.raises(RuntimeError, match=r"^#4475 guard: "):
        G.session_live_forum_endpoints()
    assert G._LFG_SESSION_ENDPOINTS is None  # not an empty or partial set
    assert os.environ.get(G._LFG_SNAPSHOT_KEY) == preset  # no half-written snapshot either


_G3_CHILD_STUB = '''
import json, os, socket

_PORT, _REC = int(os.environ["LFG_PORT"]), os.environ["LFG_REC"]
_rc, _rcx = socket.socket.connect, socket.socket.connect_ex


def _hit(a):
    return isinstance(a, tuple) and len(a) >= 2 and a[1] == _PORT


def _note(via):
    with open(_REC, "a") as fh:
        fh.write(json.dumps({"via": via}) + "\\n")


def _c(self, a):
    if _hit(a):
        _note("connect")
        raise ConnectionRefusedError(111, "recording stub: the planted port was reached")
    return _rc(self, a)


def _cx(self, a):
    if _hit(a):
        _note("connect_ex")
        return 111
    return _rcx(self, a)


socket.socket.connect, socket.socket.connect_ex = _c, _cx
'''

_G3_CHILD_PROBE = '''
import os, socket


def test_planted_port_is_refused_by_the_guard():
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", int(os.environ["LFG_PORT"])))
    except ConnectionRefusedError as exc:
        assert "guard #4475" in str(exc), str(exc)
    else:
        raise AssertionError("connected")
    finally:
        s.close()
'''


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _copied_conftest_run(tmp_path, carry):
    """A COPY of the real root conftest in a fresh tree carrying the sources named by `carry` (indexes into
    DEFAULT_URL_SOURCES); a child pytest over a probe. A recording stub (stubplug) sits beneath the guard, so a
    broken guard reaches the stub (which refuses and records), never a real connection. Returns (run, records)."""
    tree = tmp_path / "tree"
    tree.mkdir()
    shutil.copy(_CONFTEST, tree / "conftest.py")
    for i in carry:
        rel = G.DEFAULT_URL_SOURCES[i]
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, tree / rel)
    port = _free_port()
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    (home / ".engram" / "config.json").write_text(json.dumps({"forum": {"url": f"http://127.0.0.1:{port}"}}))
    (tree / "stubplug.py").write_text(_G3_CHILD_STUB)
    (tree / "test_probe.py").write_text(_G3_CHILD_PROBE)
    rec = tmp_path / "rec.jsonl"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "FORUM_URL", G._LFG_SNAPSHOT_KEY)}
    env.update(HOME=str(home), ENGRAM_HOME=str(home / ".engram"), PYTHONPATH=str(tree), LFG_PORT=str(port), LFG_REC=str(rec))
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "stubplug", "--rootdir", str(tree),
         "--confcutdir", str(tree), str(tree / "test_probe.py")],
        cwd=str(tree), env=env, capture_output=True, text=True, timeout=180,
    )
    return r, (rec.read_text().splitlines() if rec.exists() else [])


@pytest.mark.parametrize("carry", [(), (0,), (1,)], ids=["neither-source", "only-first", "only-second"])
def test_G3_a_copied_conftest_without_its_sources_fails_at_collection_naming_the_missing_path(tmp_path, carry):
    r, rec = _copied_conftest_run(tmp_path, carry)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "#4475 guard:" in out, out
    missing = [G.DEFAULT_URL_SOURCES[i] for i in range(len(G.DEFAULT_URL_SOURCES)) if i not in carry]
    assert missing[0] in out, (missing, out)
    assert "passed" not in r.stdout, out
    assert rec == [], rec


def test_G3_the_same_copy_in_a_tree_carrying_the_sources_loads_and_refuses_the_planted_port(tmp_path):
    r, rec = _copied_conftest_run(tmp_path, tuple(range(len(G.DEFAULT_URL_SOURCES))))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 passed" in r.stdout, out
    assert rec == [], rec  # the guard refused it before the recording stub beneath it saw the connect


# fold 2b: a failed session resolve leaves os.environ EXACTLY as it found it (no snapshot written before the default pair is
# known), and a source whose PARENT path component is a regular file is "file missing" too.
# `resolve_endpoints_from_environ` is stood in for (it would itself raise first on a tree without sources), so these cells
# reach the line that unions the default pair, which is the one that raises.

_G2B_PRESETS = {
    "absent": None,
    "valid-compact": '[["forum.example.test",5055]]',  # json.dumps would respell it with a space: byte-for-byte matters
    "malformed": "{not json",
}


@pytest.mark.parametrize("preset", list(_G2B_PRESETS), ids=list(_G2B_PRESETS))
def test_G2_a_failed_session_resolve_leaves_os_environ_exactly_as_it_was(tmp_path, monkeypatch, preset):
    raw = _G2B_PRESETS[preset]
    monkeypatch.setattr(G, "_LFG_REPO_ROOT", tmp_path / "no-sources")
    monkeypatch.setattr(G, "_LFG_SESSION_ENDPOINTS", None)
    monkeypatch.setattr(G, "resolve_endpoints_from_environ", lambda environ: {("localhost", 5002)})
    if raw is None:
        monkeypatch.delenv(G._LFG_SNAPSHOT_KEY, raising=False)
    else:
        monkeypatch.setenv(G._LFG_SNAPSHOT_KEY, raw)
    before = dict(os.environ)
    with pytest.raises(RuntimeError, match=r"^#4475 guard: "):
        G.session_live_forum_endpoints()
    assert G._LFG_SESSION_ENDPOINTS is None
    assert dict(os.environ) == before
    assert os.environ.get(G._LFG_SNAPSHOT_KEY) == raw


def test_G1_a_source_whose_parent_component_is_a_regular_file_is_file_missing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    rel = G.DEFAULT_URL_SOURCES[0]
    (root / rel.split("/")[0]).write_text("not a directory\n")  # `src` is a file: os.stat raises NotADirectoryError
    with pytest.raises(Exception) as ei:
        G.live_forum_endpoints({}, tmp_path / "absent.json", repo_root=root)
    assert type(ei.value) is RuntimeError, (type(ei.value), ei.value)
    assert str(ei.value) == f"#4475 guard: {rel}: file missing", str(ei.value)
