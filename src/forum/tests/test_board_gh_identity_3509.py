"""#3509 -- the board's PR-state read runs under the baton gate's GitHub identity.

Contract-first: written before the implementation.

``board_projects._gh_pr_state`` was a fourth ambient ``gh`` call. It now takes
its env from an injected provider (``board_projects.configure_gh_identity``), which
the server wires to the gate's public ``gh_call_identity`` helper from ``main()``
only. Default (no provider) is today's behaviour, byte for byte.

Cells: B1 default-off, B2 explicit env on every call incl. the thread pool,
B3 no identity -> unknown, zero calls, nothing cached, B4 server wiring,
P1 ``GH_TOKEN`` leaves the process environment once the gate captured it,
B5 a sibling-grep: no fifth ambient gh call.

All token values are fake and built at runtime.
"""

import json
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

from forum import board_projects, server as server_mod

TOKEN = "fake-token-for-test-" + "0000"
REPO = "example-org/example-repo"
_SRC_FORUM = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean():
    gate = server_mod._load_baton_gate()
    assert gate is not None
    gate._service_identity_reset()
    board_projects.configure_gh_identity(None)
    board_projects._clear_gh_cache()
    yield
    gate._service_identity_reset()
    board_projects.configure_gh_identity(None)
    board_projects._clear_gh_cache()


@pytest.fixture
def gate():
    return server_mod._load_baton_gate()


class FakeRun:
    def __init__(self, *, forbid=False):
        self.calls = []
        self.forbid = forbid
        self._lock = threading.Lock()

    def __call__(self, cmd, *a, **kw):
        with self._lock:
            self.calls.append((list(cmd), kw))
        if self.forbid:
            raise AssertionError(f"gh must not be invoked: {cmd!r}")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"state": "OPEN"}), stderr="")


def _patch_run(monkeypatch, fake):
    monkeypatch.setattr(board_projects.subprocess, "run", fake)
    return fake


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "gh-config"
    d.mkdir(mode=0o700)
    return str(d)


# ---------------------------------------------------------------------------
# gate side: a public helper
# ---------------------------------------------------------------------------

def test_gate_exposes_a_public_identity_helper(gate, cfg_dir):
    assert gate.gh_call_identity() == ({}, None)  # default-off
    gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    kw, refusal = gate.gh_call_identity()
    assert refusal is None and kw["env"]["GH_TOKEN"] == TOKEN
    gate.configure_service_github_identity(environ={}, config_dir=cfg_dir)
    kw, refusal = gate.gh_call_identity()
    assert kw == {} and refusal[0] == "unknown"


# ---------------------------------------------------------------------------
# B1 -- default off
# ---------------------------------------------------------------------------

def test_B1_no_provider_passes_no_env_kwarg(monkeypatch):
    fake = _patch_run(monkeypatch, FakeRun())
    assert board_projects._gh_pr_state("9001", REPO) == "open"
    assert len(fake.calls) == 1
    assert "env" not in fake.calls[0][1]


# ---------------------------------------------------------------------------
# B2 -- explicit minimal env, every call, thread pool included
# ---------------------------------------------------------------------------

def test_B2_provider_env_on_every_call_through_the_thread_pool(monkeypatch, gate, cfg_dir):
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token-xxxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token-xxxx")
    monkeypatch.setenv("HOME", "/ambient/home")
    gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    board_projects.configure_gh_identity(gate.gh_call_identity)
    fake = _patch_run(monkeypatch, FakeRun())
    refs = [(REPO, "101"), (REPO, "102"), ("example-org/other-repo", "101")]
    out = board_projects._batch_gh_reconcile(refs)
    assert out == {r: "open" for r in refs}
    assert len(fake.calls) == 3
    for cmd, kw in fake.calls:
        env = kw.get("env")
        assert isinstance(env, dict), f"no env= on {cmd[:4]}"
        assert env["GH_TOKEN"] == TOKEN and env["GH_CONFIG_DIR"] == cfg_dir
        assert env["GH_PROMPT_DISABLED"] == "1" and env["GH_NO_UPDATE_NOTIFIER"] == "1"
        assert "PATH" in env
        for ambient in ("GITHUB_TOKEN", "HOME"):
            assert ambient not in env
        assert set(env) == {"PATH", "GH_TOKEN", "GH_CONFIG_DIR",
                            "GH_PROMPT_DISABLED", "GH_NO_UPDATE_NOTIFIER"}


# ---------------------------------------------------------------------------
# B3 -- provider without an identity
# ---------------------------------------------------------------------------

def test_B3_no_identity_is_unknown_with_zero_calls_and_nothing_cached(monkeypatch, gate, cfg_dir):
    gate.configure_service_github_identity(environ={}, config_dir=cfg_dir)
    board_projects.configure_gh_identity(gate.gh_call_identity)
    fake = _patch_run(monkeypatch, FakeRun(forbid=True))
    assert board_projects._gh_pr_state("9001", REPO) == "unknown"
    refs = [(REPO, "101"), (REPO, "102"), (REPO, "103")]
    assert board_projects._batch_gh_reconcile(refs) == {r: "unknown" for r in refs}
    assert fake.calls == []
    assert board_projects._GH_STATE_CACHE == {}


# ---------------------------------------------------------------------------
# B4 -- server wiring
# ---------------------------------------------------------------------------

def _probe(monkeypatch):
    fake = _patch_run(monkeypatch, FakeRun())
    state = board_projects._gh_pr_state("9001", REPO)
    return state, fake


def test_B4_create_app_alone_leaves_the_provider_unset(tmp_path, monkeypatch, gate, cfg_dir):
    from forum.tests.test_baton_advance_api import World
    # Service mode WITH an identity: a provider wired by create_app would show up as env=.
    gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    World(tmp_path, monkeypatch)  # create_app()
    fake = FakeRun()
    monkeypatch.setattr(board_projects.subprocess, "run", fake)
    board_projects._gh_pr_state("9001", REPO)
    assert "env" not in fake.calls[0][1]


def test_B4_configure_with_token_sets_the_provider(tmp_path, monkeypatch):
    server_mod._configure_gate_github_identity(str(tmp_path), environ={"GH_TOKEN": TOKEN})
    state, fake = _probe(monkeypatch)
    assert state == "open"
    assert fake.calls[0][1]["env"]["GH_TOKEN"] == TOKEN


def test_B4_configure_without_token_still_sets_it_and_refuses(tmp_path, monkeypatch):
    server_mod._configure_gate_github_identity(str(tmp_path), environ={})
    fake = _patch_run(monkeypatch, FakeRun(forbid=True))
    result = board_projects._gh_pr_state("9001", REPO)
    assert result == "unknown" and TOKEN not in result
    assert fake.calls == []


def test_B4_unusable_config_dir_still_wires_the_provider_and_refuses(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        server_mod._configure_gate_github_identity(str(ro), environ={"GH_TOKEN": TOKEN})
    finally:
        os.chmod(ro, 0o700)
    fake = _patch_run(monkeypatch, FakeRun(forbid=True))
    assert board_projects._gh_pr_state("9001", REPO) == "unknown"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# P1 -- the token leaves os.environ once captured (only when it IS os.environ)
# ---------------------------------------------------------------------------

def test_P1_os_environ_token_is_removed_but_the_gate_keeps_it(tmp_path, monkeypatch, gate):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    server_mod._configure_gate_github_identity(str(tmp_path))
    assert "GH_TOKEN" not in os.environ
    kw, refusal = gate.gh_call_identity()
    assert refusal is None and kw["env"]["GH_TOKEN"] == TOKEN


def test_P1_explicit_os_environ_object_is_also_popped(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    server_mod._configure_gate_github_identity(str(tmp_path), environ=os.environ)
    assert "GH_TOKEN" not in os.environ


def test_P1_own_dict_is_not_mutated_and_os_environ_untouched(tmp_path, monkeypatch, gate):
    monkeypatch.setenv("GH_TOKEN", "ambient-sentinel-xxxx")
    own = {"GH_TOKEN": TOKEN}
    server_mod._configure_gate_github_identity(str(tmp_path), environ=own)
    assert own == {"GH_TOKEN": TOKEN}
    assert os.environ["GH_TOKEN"] == "ambient-sentinel-xxxx"
    assert gate.gh_call_identity()[0]["env"]["GH_TOKEN"] == TOKEN


# ---------------------------------------------------------------------------
# B5 -- sibling grep: no fifth ambient gh call
# ---------------------------------------------------------------------------

# Every non-test source file that spells a "gh" argv literal, and the function
# that spawns it. Adding another one means routing it through the identity helper
# and then extending this list on purpose. (The gate's own three live in
# src/engram/tools/baton_gate.py, outside src/forum; see the second test.)
_ALLOWED_FORUM_GH = {("board_projects.py", "_gh_pr_state")}
_GH_LITERAL = re.compile(r"""\[\s*["']gh["']""")


def _enclosing_def(lines, idx):
    for i in range(idx, -1, -1):
        m = re.match(r"\s*def\s+(\w+)", lines[i])
        if m:
            return m.group(1)
    return None


def test_B5_the_only_gh_argv_literal_under_src_forum_is_the_board_read():
    found = set()
    for p in sorted(_SRC_FORUM.rglob("*.py")):
        if "tests" in p.relative_to(_SRC_FORUM).parts:
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            if _GH_LITERAL.search(ln) and not ln.lstrip().startswith("#"):
                found.add((p.name, _enclosing_def(lines, i)))
    assert found == _ALLOWED_FORUM_GH, f"unexpected gh call site(s): {sorted(found ^ _ALLOWED_FORUM_GH)}"


def test_B5_each_allowed_site_reaches_the_identity_helper():
    board = (_SRC_FORUM / "board_projects.py").read_text(encoding="utf-8")
    body = board.split("def _gh_pr_state", 1)[1].split("\ndef ", 1)[0]
    assert "_GH_IDENTITY_PROVIDER" in body and "**gh_kw" in body
    gate = (_SRC_FORUM.parent / "engram" / "tools" / "baton_gate.py").read_text(encoding="utf-8")
    assert len(re.findall(r"subprocess\.run\(", gate)) == len(re.findall(r"\*\*gh_kw", gate)) == 5


# ---------------------------------------------------------------------------
# Round-1 S2 / S1 / S3
# ---------------------------------------------------------------------------

def test_S2_gate_module_unavailable_makes_the_board_refuse(tmp_path, monkeypatch):
    """No gate module: the board must not fall back to the ambient gh login."""
    monkeypatch.setattr(server_mod, "_load_baton_gate", lambda: None)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    server_mod._configure_gate_github_identity(str(tmp_path))  # must not raise
    fake = _patch_run(monkeypatch, FakeRun(forbid=True))
    assert board_projects._gh_pr_state("9001", REPO) == "unknown"
    assert fake.calls == []
    # nothing captured the token, so nothing may remove it
    assert os.environ.get("GH_TOKEN") == TOKEN


def test_S2_unavailable_stderr_no_longer_claims_the_ambient_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server_mod, "_load_baton_gate", lambda: None)
    server_mod._configure_gate_github_identity(str(tmp_path), environ={})
    err = capsys.readouterr().err
    assert "unavailable" in err and "ambient" not in err and TOKEN not in err


def test_S1_second_environ_none_configure_drops_the_identity_known_limit(tmp_path, monkeypatch, gate):
    """PINS A KNOWN LIMIT, NOT A FEATURE: configuration is once-only per process.

    The server pops GH_TOKEN from os.environ after the gate captured it, so a second
    `configure_service_github_identity()` with environ=None finds no token and puts
    the gate (and, through it, the board) in service mode with NO identity."""
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    cfg = server_mod._configure_gate_github_identity(str(tmp_path))
    assert gate.gh_call_identity()[1] is None  # identity present after the first call
    assert gate.configure_service_github_identity(config_dir=cfg) is False
    kw, refusal = gate.gh_call_identity()
    assert kw == {} and refusal[0] == "unknown"


def test_S3_only_GH_TOKEN_is_removed_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", "other-token-for-test-1111")
    server_mod._configure_gate_github_identity(str(tmp_path))
    assert "GH_TOKEN" not in os.environ
    assert os.environ["GITHUB_TOKEN"] == "other-token-for-test-1111"
