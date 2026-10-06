"""#3283 G6 -- the forum server wires the gate's dedicated GitHub identity.

Contract-first: written before the implementation. Cell-level behaviour of the
three `gh` call sites is in tests/test_baton_gate_service_identity_3283.py; this
file covers only the SERVER side:

  * ``main()`` configures the gate (service-identity mode) and creates the 0700
    ``gh-config`` dir beside the service data, with a token present AND absent;
  * absent token: the server still STARTS, and an `advance` on an anchored PR
    baton gets the existing unknown-class refusal with ZERO gh calls;
  * the token appears in neither /health, the refusal body, nor the audit log.

Tokens are fake and built at runtime.
"""

import os
import stat
import subprocess
import sys

import pytest

from forum import server as server_mod
from forum.tests.test_baton_advance_api import World

TOKEN = "fake-token-for-test-" + "0000"


@pytest.fixture(autouse=True)
def _reset_gate():
    gate = server_mod._load_baton_gate()
    assert gate is not None
    gate._service_identity_reset()
    yield
    gate._service_identity_reset()


def _read_all(tmp_path):
    parts = []
    for p in tmp_path.rglob("*.jsonl"):
        parts.append(p.read_text())
    return "\n".join(parts)


class TestConfigureHelper:
    def test_token_present_configures_and_creates_0700_dir(self, tmp_path):
        data = tmp_path / "svc"
        data.mkdir()
        cfg = server_mod._configure_gate_github_identity(str(data), environ={"GH_TOKEN": TOKEN})
        assert os.path.isdir(cfg)
        assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o700
        assert os.path.dirname(cfg) == str(data)
        assert os.listdir(cfg) == []
        assert server_mod._load_baton_gate()._service_identity_is_on() is True

    def test_token_absent_still_configures_and_does_not_raise(self, tmp_path):
        data = tmp_path / "svc"
        data.mkdir()
        cfg = server_mod._configure_gate_github_identity(str(data), environ={})
        assert os.path.isdir(cfg)
        assert server_mod._load_baton_gate()._service_identity_is_on() is True

    def test_existing_dir_is_tolerated_and_forced_to_0700(self, tmp_path):
        data = tmp_path / "svc"
        (data / "gh-config").mkdir(parents=True, mode=0o755)
        cfg = server_mod._configure_gate_github_identity(str(data), environ={"GH_TOKEN": TOKEN})
        assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o700


class TestMainWiring:
    def _run_main(self, tmp_path, monkeypatch, env_token, forum_home=None):
        monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
        if forum_home is None:
            monkeypatch.delenv("FORUM_HOME", raising=False)
        else:
            monkeypatch.setenv("FORUM_HOME", str(forum_home))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        if env_token:
            monkeypatch.setenv("GH_TOKEN", TOKEN)
        monkeypatch.setattr(sys, "argv", [
            "forum.server", "--db", str(tmp_path / "svc" / "forum.db"),
            "--audit", str(tmp_path / "svc" / "audit.jsonl")])
        monkeypatch.setattr(server_mod, "_run_boot_verify", lambda *a, **k: {})
        ran = []
        monkeypatch.setattr(server_mod.Flask, "run", lambda self, *a, **k: ran.append(1))
        server_mod.main()
        assert ran == [1], "main() must reach app.run even without a token"

    @pytest.mark.parametrize("env_token", [True, False], ids=["token", "no-token"])
    def test_main_configures_gate_and_makes_config_dir(self, tmp_path, monkeypatch, env_token):
        self._run_main(tmp_path, monkeypatch, env_token)
        assert server_mod._load_baton_gate()._service_identity_is_on() is True
        cfg = tmp_path / "svc" / "gh-config"   # no FORUM_HOME: beside the DB
        assert cfg.is_dir() and stat.S_IMODE(cfg.stat().st_mode) == 0o700

    def test_main_puts_config_dir_under_forum_home_when_set(self, tmp_path, monkeypatch):
        home = tmp_path / "forum-home"
        self._run_main(tmp_path, monkeypatch, True, forum_home=home)
        cfg = home / "gh-config"
        assert cfg.is_dir() and stat.S_IMODE(cfg.stat().st_mode) == 0o700
        assert not (tmp_path / "svc" / "gh-config").exists()

    def test_create_app_alone_does_not_switch_service_mode_on(self, tmp_path, monkeypatch):
        World(tmp_path, monkeypatch)
        assert server_mod._load_baton_gate()._service_identity_is_on() is False


class TestUnusableConfigDir:
    """Round-1 F2: an unwritable gh-config dir must not crash startup; the gate
    stays in service mode with NO identity and refuses at call time."""

    def test_readonly_parent_does_not_raise_and_gate_refuses(self, tmp_path, monkeypatch, capsys):
        if os.geteuid() == 0:
            pytest.skip("root ignores directory modes")
        parent = tmp_path / "ro"
        parent.mkdir()
        os.chmod(parent, 0o500)
        try:
            server_mod._configure_gate_github_identity(str(parent), environ={"GH_TOKEN": TOKEN})
        finally:
            os.chmod(parent, 0o700)
        gate = server_mod._load_baton_gate()
        assert gate._service_identity_is_on() is True
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        state, detail = gate._pr_ci_state("9001", "example-org/example-repo")
        assert state == "unknown" and "#3283" in detail
        assert calls == []
        err = capsys.readouterr().err
        assert TOKEN not in err
        assert len([ln for ln in err.splitlines() if "gh-config" in ln]) == 1

    def test_control_writable_dir_unchanged(self, tmp_path):
        cfg = server_mod._configure_gate_github_identity(str(tmp_path), environ={"GH_TOKEN": TOKEN})
        assert os.path.isdir(cfg) and stat.S_IMODE(os.stat(cfg).st_mode) == 0o700


class TestRefusalEndToEnd:
    def _configure(self, tmp_path, environ):
        (tmp_path / "svc").mkdir(exist_ok=True)
        return server_mod._configure_gate_github_identity(str(tmp_path / "svc"), environ=environ)

    def test_absent_token_advance_refuses_unknown_with_zero_gh_calls(self, tmp_path, monkeypatch):
        w = World(tmp_path, monkeypatch)
        self._configure(tmp_path, {})
        w.seed()
        resp = w.advance()
        assert resp.status_code != 200, resp.get_data(as_text=True)
        assert "unknown" in resp.get_json()["error"]
        assert "#3283" in resp.get_json()["error"]
        assert w.gh.calls == [], f"gh was invoked with no token: {w.gh.calls}"
        assert w.fields()[0]["phase"] == "peer"

    def test_token_never_in_refusal_health_or_audit(self, tmp_path, monkeypatch):
        w = World(tmp_path, monkeypatch)
        self._configure(tmp_path, {"GH_TOKEN": TOKEN})
        w.seed()
        w.gh.review_mode = "nonzero"
        inner = w.gh

        def leaky(cmd, *a, **kw):
            res = inner(cmd, *a, **kw)
            if res.stderr:
                res.stderr = f"{res.stderr} token={TOKEN}"
            return res

        monkeypatch.setattr(subprocess, "run", leaky)
        resp = w.advance()
        assert resp.status_code != 200
        body = resp.get_data(as_text=True)
        assert TOKEN not in body
        assert "***" in body, "the leaked stderr must reach the response scrubbed, proving the path ran"
        assert inner.calls, "token configured: gh must have been called"
        health = w.client.get("/health").get_data(as_text=True)
        assert TOKEN not in health
        assert TOKEN not in _read_all(tmp_path)
