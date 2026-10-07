"""#3283 -- the forum service's baton gate calls `gh` under a dedicated token.

Contract-first: written before the implementation.

tools/baton_gate.py is imported by TWO callers: the forum service and the agent
CLI. The fail-closed token requirement is SERVICE-ONLY, switched on by one
public entry point, ``configure_service_github_identity``, which only the forum
server calls (cells G6 / G7 live under src/forum/tests/). Until it is called the
three `gh` call sites behave exactly as before (G4).

Cells here: G1 explicit minimal env at all three sites (every page of the paged
one), G2 no token -> unknown with zero invocations, G3 ambient hosts.yml is not
a fallback, G4 default-off, G5 the token is scrubbed from every detail string.

All token values are fake and built at runtime.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_TOOLS_DIR = _ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton_gate  # noqa: E402

TOKEN = "fake-token-for-test-" + "0000"
HEAD = "a" * 40
REPO = "example-org/example-repo"
SITES = ["ci", "approval", "reviewer"]


def _call(site):
    if site == "ci":
        return baton_gate._pr_ci_state("9001", REPO)
    if site == "approval":
        return baton_gate._pr_approval_state("9001", REPO)
    return baton_gate._reviewer_approval_state("9001", REPO, "example-gh-colleague")


class FakeRun:
    """Stands in for subprocess.run; records every call's cmd and kwargs."""

    def __init__(self, *, stderr="", returncode=0, raises=None, forbid=False):
        self.calls = []
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.forbid = forbid

    def __call__(self, cmd, *a, **kw):
        self.calls.append((list(cmd), kw))
        if self.forbid:
            raise AssertionError(f"gh must not be invoked, got {cmd!r}")
        if self.raises is not None:
            raise self.raises
        if self.returncode != 0:
            return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr=self.stderr)
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid,author" in cmd:
            out = {"headRefOid": HEAD, "author": {"login": "someone-else"}}
        elif cmd[:3] == ["gh", "pr", "view"]:
            out = {"mergeStateStatus": "CLEAN", "statusCheckRollup": []}
        elif cmd[:2] == ["gh", "api"] and "/reviews?" in cmd[2]:
            # reviewer / approval sites: a full first page, then an empty one
            out = [] if cmd[2].endswith("page=2") else [
                {"id": i, "state": "COMMENTED", "user": {"login": "someone-else"},
                 "submitted_at": "2026-10-01T10:00:00Z", "commit_id": HEAD}
                for i in range(100)]
        else:
            out = {}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")


@pytest.fixture(autouse=True)
def _reset_identity():
    baton_gate._service_identity_reset()
    yield
    baton_gate._service_identity_reset()


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "gh-config"
    d.mkdir(mode=0o700)
    return str(d)


def _patch(monkeypatch, fake):
    monkeypatch.setattr(baton_gate.subprocess, "run", fake)
    return fake


# ---------------------------------------------------------------------------
# G1 -- explicit minimal env
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site", SITES)
def test_G1_explicit_minimal_env_on_every_call(site, monkeypatch, cfg_dir):
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token-xxxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token-xxxx")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "ambient-ent-token-xxxx")
    monkeypatch.setenv("HOME", "/ambient/home")
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    fake = _patch(monkeypatch, FakeRun())
    _call(site)
    assert fake.calls, "gate made no gh call"
    if site == "reviewer":
        assert len(fake.calls) >= 2, "the 2-page fake must drive two gh calls"
    for cmd, kw in fake.calls:
        env = kw.get("env")
        assert isinstance(env, dict), f"no explicit env= on {cmd[:3]}"
        assert env["GH_TOKEN"] == TOKEN
        assert env["GH_CONFIG_DIR"] == cfg_dir
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
        assert "PATH" in env
        for ambient in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "HOME"):
            assert ambient not in env, f"ambient {ambient} leaked into the gh env"
        assert env["GH_TOKEN"] != "ambient-gh-token-xxxx"
        assert set(env) == {"PATH", "GH_TOKEN", "GH_CONFIG_DIR",
                            "GH_PROMPT_DISABLED", "GH_NO_UPDATE_NOTIFIER"}


# ---------------------------------------------------------------------------
# G2 / G3 -- no token: unknown, zero invocations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("environ", [{}, {"GH_TOKEN": ""}, {"GH_TOKEN": "   "}],
                         ids=["missing", "empty", "whitespace"])
def test_G2_no_token_is_unknown_with_zero_invocations(site, environ, monkeypatch, cfg_dir):
    baton_gate.configure_service_github_identity(environ=environ, config_dir=cfg_dir)
    fake = _patch(monkeypatch, FakeRun(forbid=True))
    state, detail = _call(site)
    assert state == "unknown"
    assert "#3283" in detail and "GitHub" in detail
    assert fake.calls == []


def test_G2_message_is_one_fixed_string_without_secrets(monkeypatch, cfg_dir):
    baton_gate.configure_service_github_identity(environ={}, config_dir=cfg_dir)
    _patch(monkeypatch, FakeRun(forbid=True))
    msgs = {_call(s)[1] for s in SITES}
    assert len(msgs) == 1


@pytest.mark.parametrize("site", SITES)
def test_G2_no_config_dir_is_no_identity(site, monkeypatch):
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=None)
    fake = _patch(monkeypatch, FakeRun(forbid=True))
    assert _call(site)[0] == "unknown"
    assert fake.calls == []


@pytest.mark.parametrize("site", SITES)
def test_G3_ambient_hosts_yml_is_not_a_fallback(site, monkeypatch, tmp_path, cfg_dir):
    home = tmp_path / "realhome"
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text(
        "github.com:\n    user: some-agent\n    oauth_token: not-a-real-token\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    baton_gate.configure_service_github_identity(config_dir=cfg_dir)  # environ=os.environ
    fake = _patch(monkeypatch, FakeRun(forbid=True))
    assert _call(site)[0] == "unknown"
    assert fake.calls == []


def test_token_is_read_once_at_configure_time(monkeypatch, cfg_dir):
    env = {"GH_TOKEN": TOKEN}
    baton_gate.configure_service_github_identity(environ=env, config_dir=cfg_dir)
    env["GH_TOKEN"] = "changed-later-0000"
    fake = _patch(monkeypatch, FakeRun())
    _call("ci")
    assert fake.calls[0][1]["env"]["GH_TOKEN"] == TOKEN


def test_configure_returns_whether_a_token_is_present(cfg_dir):
    assert baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir) is True
    assert baton_gate.configure_service_github_identity(environ={}, config_dir=cfg_dir) is False


# ---------------------------------------------------------------------------
# G4 -- default off
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site", SITES)
def test_G4_unconfigured_inherits_env_and_never_refuses(site, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    fake = _patch(monkeypatch, FakeRun())
    state, detail = _call(site)
    assert fake.calls, "unconfigured gate must still call gh"
    for _cmd, kw in fake.calls:
        assert kw.get("env") is None
    assert "#3283" not in detail


@pytest.mark.parametrize("snippet", [
    "import baton_gate as g",
    "import baton, baton_gate as g",
], ids=["gate-import", "cli-import"])
def test_G4_importing_the_module_does_not_switch_service_mode_on(snippet):
    """A FRESH interpreter: the autouse reset above would mask an import-time configure."""
    code = (f"import sys; sys.path.insert(0, {str(_TOOLS_DIR)!r}); {snippet}; "
            "print(g._service_identity_is_on())")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "False"


# ---------------------------------------------------------------------------
# G5 -- the token never reaches a detail string
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site", SITES)
def test_G5_stderr_text_is_scrubbed(site, monkeypatch, cfg_dir):
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    _patch(monkeypatch, FakeRun(returncode=1, stderr=f"auth failed for {TOKEN} ({TOKEN})"))
    state, detail = _call(site)
    assert state == "unknown"
    assert TOKEN not in detail
    assert "***" in detail


@pytest.mark.parametrize("site", SITES)
def test_G5_exec_error_text_is_scrubbed(site, monkeypatch, cfg_dir):
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    _patch(monkeypatch, FakeRun(raises=OSError(f"cannot exec with {TOKEN}")))
    state, detail = _call(site)
    assert state == "unknown"
    assert TOKEN not in detail
    assert "***" in detail


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("exc", [FileNotFoundError("gh"), subprocess.TimeoutExpired(["gh"], 15)],
                         ids=["not-found", "timeout"])
def test_G5_not_found_and_timeout_details_carry_no_token(site, exc, monkeypatch, cfg_dir):
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    _patch(monkeypatch, FakeRun(raises=exc))
    state, detail = _call(site)
    assert state == "unknown" and TOKEN not in detail


def test_G5_scrub_is_a_noop_when_off_or_tokenless(cfg_dir):
    assert baton_gate._scrub_gh_detail("plain text") == "plain text"
    baton_gate.configure_service_github_identity(environ={}, config_dir=cfg_dir)
    assert baton_gate._scrub_gh_detail("plain text") == "plain text"


def test_token_not_in_repr_or_str_of_module_state(cfg_dir):
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=cfg_dir)
    holder = baton_gate._SERVICE_IDENTITY
    assert TOKEN not in repr(holder) and TOKEN not in str(holder)
