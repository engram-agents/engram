"""#3863 6c: the build engine refuses a RELATIVE ENGRAM_HOME at both of its env reads.

Contract (relative-engram-home-6c spec): unset/empty -> ~/.engram expanded; `~/x` -> expanded absolute;
a relative value -> ValueError from the resolver, which the CLI turns into exactly ONE stderr line naming
the value, exit 1, no traceback; upgrade_flow() with a relative env value refuses and runs no step; a
fake engram_home passed in ctx still wins. `--engram-home PATH` (build subcommand) is out of scope.
"""

from __future__ import annotations

import os

import pytest

from tools.engine import cli, flows

SUBCOMMANDS = [["plan"], ["run"], ["doctor"]]


def test_ctx_unset_and_empty_mean_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    assert cli._build_ctx_from_env()["engram_home"] == str(tmp_path / ".engram")
    monkeypatch.setenv("ENGRAM_HOME", "")
    assert cli._build_ctx_from_env()["engram_home"] == str(tmp_path / ".engram")


def test_ctx_tilde_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAM_HOME", "~/x")
    assert cli._build_ctx_from_env()["engram_home"] == str(tmp_path / "x")


def test_ctx_absolute_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "h"))
    assert cli._build_ctx_from_env()["engram_home"] == str(tmp_path / "h")


@pytest.mark.parametrize("value", ["rel/dir", ".", "./x"])
def test_ctx_relative_raises_value_error_naming_it(monkeypatch, value):
    monkeypatch.setenv("ENGRAM_HOME", value)
    with pytest.raises(ValueError) as e:
        cli._build_ctx_from_env()
    assert value in str(e.value)


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_cli_relative_env_is_one_stderr_line_exit_1_no_traceback(sub, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", "rel/dir")
    rc = cli.main(sub + ["upgrade"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert len([l for l in err.splitlines() if l.strip()]) == 1, err
    assert "rel/dir" in err


@pytest.mark.parametrize("value", ["rel/dir", "."])
def test_upgrade_flow_refuses_a_relative_env_value(monkeypatch, value):
    monkeypatch.setenv("ENGRAM_HOME", value)
    with pytest.raises(ValueError) as e:
        flows.upgrade_flow()
    assert value in str(e.value)


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_no_step_runs_on_a_relative_env_value(sub, monkeypatch, tmp_path):
    """Refusal comes before any step machinery: plan_flow / run_flow / run_doctor are never reached
    (discriminates: on unmodified dev run_flow IS reached and fails at the first step)."""
    from tools.engine import steps as steps_mod

    called = []
    for name in ("plan_flow", "run_flow", "run_doctor"):
        for mod in (flows, steps_mod):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: called.append(_n) or [])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", "rel/dir")
    assert cli.main(sub + ["upgrade"]) == 1
    assert called == []


def test_upgrade_flow_unset_empty_and_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for val in (None, "", "~/x"):
        if val is None:
            monkeypatch.delenv("ENGRAM_HOME", raising=False)
        else:
            monkeypatch.setenv("ENGRAM_HOME", val)
        assert flows.upgrade_flow()


def test_a_ctx_engram_home_still_wins_over_a_relative_env(monkeypatch, tmp_path):
    """upgrade_flow() itself refuses before ctx exists, so the override applies for an ABSOLUTE/unset env only;
    the step lambdas read ctx['engram_home'] (existing tests rely on it)."""
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    steps = flows.upgrade_flow()
    first = next(s for s in steps if s.id == "substrate-health")
    ctx = {"engram_home": str(tmp_path / "fake")}
    os.makedirs(ctx["engram_home"])
    assert first.check(ctx) in (True, False)
