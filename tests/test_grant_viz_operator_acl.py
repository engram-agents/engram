"""engram-alpha#2720 — tools/grant_viz_operator_acl.py.

Coverage:
  1. discover_seats: globs seat home dirs, sorted, directories only.
  2. plan_for_seat: PURE (no subprocess, no writes) -- skips a seat with no
     health-snapshots dir; builds the expected setfacl argv (named entry +
     explicit mask, access + default) for a seat that has one.
  3. apply_seat_plan dry-run: never shells out, reports the plan without
     changing anything.
  4. apply_seat_plan --apply, real setfacl (no mocking): end-to-end against
     a throwaway directory tree, verified independently via getfacl.
  5. Privilege gate: GrantError raised (not a silent no-op, not a bare
     setfacl permission error) when the caller is neither root nor the
     owner of the target seat directory.
  6. main()/run(): fleet-wide report shape, exit code reflects errors,
     skips are never counted as errors.
  7. Never touches real /home/agent-* -- every test below uses tmp_path.
  8. No default operator (engram-alpha#2720): plan_for_seat/run require an
     explicit viz_operator (TypeError if omitted); resolve_viz_operator's
     CLI > env > config-file precedence; main() refuses loudly (non-zero
     exit, naming VIZ_OPERATOR and the config path) when none resolves.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "grant_viz_operator_acl", str(TOOLS_DIR / "grant_viz_operator_acl.py")
    )
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: the module uses `@dataclass` on a
    # class whose field types are postponed-eval string annotations (this
    # repo's `from __future__ import annotations` convention), and
    # dataclasses' own ClassVar-detection resolves module globals via
    # sys.modules.get(cls.__module__) -- if the module was never registered,
    # that lookup returns None and dataclass field processing crashes with
    # "'NoneType' object has no attribute '__dict__'" (measured: not merely
    # a theoretical concern -- this is exactly what happens without this
    # line, for THIS module specifically, unlike reconcile_lesson_
    # demotions.py's own loader, which doesn't need it because that module
    # has no dataclass).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def g():
    return _load_module()


def _make_seat(root: Path, name: str, with_snapshots: bool = True) -> Path:
    seat = root / name
    engram_dir = seat / ".engram"
    if with_snapshots:
        (engram_dir / "health-snapshots").mkdir(parents=True)
    else:
        engram_dir.mkdir(parents=True)
    return seat


# ---------------------------------------------------------------------------
# 1. discover_seats
# ---------------------------------------------------------------------------


class TestDiscoverSeats:
    def test_finds_directories_matching_glob(self, tmp_path, g):
        _make_seat(tmp_path, "agent-alice")
        _make_seat(tmp_path, "agent-bob")
        (tmp_path / "not-an-agent-dir").mkdir()

        found = g.discover_seats(str(tmp_path / "agent-*"))
        assert found == [str(tmp_path / "agent-alice"), str(tmp_path / "agent-bob")]

    def test_sorted_deterministically(self, tmp_path, g):
        _make_seat(tmp_path, "agent-zeta")
        _make_seat(tmp_path, "agent-alpha")
        found = g.discover_seats(str(tmp_path / "agent-*"))
        assert found == sorted(found)

    def test_empty_when_nothing_matches(self, tmp_path, g):
        found = g.discover_seats(str(tmp_path / "agent-*"))
        assert found == []

    def test_ignores_non_directory_matches(self, tmp_path, g):
        (tmp_path / "agent-file").write_text("not a dir")
        found = g.discover_seats(str(tmp_path / "agent-*"))
        assert found == []


# ---------------------------------------------------------------------------
# 2. plan_for_seat -- PURE
# ---------------------------------------------------------------------------


class TestPlanForSeat:
    def test_skips_seat_without_health_snapshots_dir(self, tmp_path, g):
        seat = _make_seat(tmp_path, "agent-bob", with_snapshots=False)
        plan = g.plan_for_seat(str(seat), viz_operator="testop")
        assert plan.skip_reason is not None
        assert plan.commands == []
        assert not plan.actionable

    def test_skips_seat_with_no_engram_at_all(self, tmp_path, g):
        seat = tmp_path / "agent-nothing"
        seat.mkdir()
        plan = g.plan_for_seat(str(seat), viz_operator="testop")
        assert plan.skip_reason is not None
        assert not plan.actionable

    def test_builds_expected_setfacl_command_for_a_real_seat(self, tmp_path, g):
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="spt")
        assert plan.actionable
        assert len(plan.commands) == 1
        cmd = plan.commands[0]
        assert cmd[0] == "setfacl"
        assert "-R" in cmd
        assert "-m" in cmd
        entries = cmd[cmd.index("-m") + 1]
        # Named access entry + explicit access mask + named default entry +
        # explicit default mask -- engram-alpha#2720 item 2.
        assert "u:spt:rX" in entries
        assert "m::rX" in entries
        assert "d:u:spt:rX" in entries
        assert "d:m::rX" in entries
        assert cmd[-1] == str(seat / ".engram" / "health-snapshots")

    def test_respects_custom_viz_operator(self, tmp_path, g):
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="custom_op")
        entries = plan.commands[0][plan.commands[0].index("-m") + 1]
        assert "u:custom_op:rX" in entries
        assert "spt" not in entries

    def test_is_pure_no_subprocess_calls(self, tmp_path, g, monkeypatch):
        """PURE: calling plan_for_seat must never shell out."""
        seat = _make_seat(tmp_path, "agent-alice")

        def _boom(*a, **k):
            raise AssertionError("plan_for_seat must not call subprocess.run")

        monkeypatch.setattr(g.subprocess, "run", _boom)
        g.plan_for_seat(str(seat), viz_operator="testop")  # must not raise

    def test_is_pure_no_filesystem_writes(self, tmp_path, g):
        """PURE: calling plan_for_seat must not create/modify anything."""
        seat = _make_seat(tmp_path, "agent-alice")
        snapshots_dir = seat / ".engram" / "health-snapshots"
        before = set(os.listdir(snapshots_dir))
        g.plan_for_seat(str(seat), viz_operator="testop")
        after = set(os.listdir(snapshots_dir))
        assert before == after


# ---------------------------------------------------------------------------
# 3. apply_seat_plan -- dry-run
# ---------------------------------------------------------------------------


class TestApplySeatPlanDryRun:
    def test_dry_run_never_calls_subprocess(self, tmp_path, g, monkeypatch):
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="testop")

        def _boom(*a, **k):
            raise AssertionError("dry-run must not call subprocess.run")

        monkeypatch.setattr(g.subprocess, "run", _boom)
        result = g.apply_seat_plan(plan, dry_run=True)
        assert result["dry_run"] is True
        assert result["applied"] is False
        assert result["error"] is None

    def test_dry_run_reports_the_commands_it_would_run(self, tmp_path, g):
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="spt")
        result = g.apply_seat_plan(plan, dry_run=True)
        assert len(result["commands"]) == 1
        assert "setfacl" in result["commands"][0]
        assert "u:spt:rX" in result["commands"][0]

    def test_dry_run_on_a_skipped_seat_reports_skip_reason(self, tmp_path, g):
        seat = _make_seat(tmp_path, "agent-bob", with_snapshots=False)
        plan = g.plan_for_seat(str(seat), viz_operator="testop")
        result = g.apply_seat_plan(plan, dry_run=True)
        assert result["skip_reason"] is not None
        assert result["commands"] == []


# ---------------------------------------------------------------------------
# 4. apply_seat_plan --apply, REAL setfacl, no mocking
# ---------------------------------------------------------------------------


def _require_acl_tools():
    if not (shutil.which("setfacl") and shutil.which("getfacl")):
        pytest.skip("setfacl/getfacl not available in this environment")


class TestApplySeatPlanRealEndToEnd:
    def test_real_apply_grants_a_readable_acl(self, tmp_path, g):
        """No mocking: real setfacl against a real throwaway directory,
        verified independently via a real getfacl call. The test process
        owns tmp_path, so this exercises the "self-repair, no sudo needed"
        privilege branch -- see TestPrivilegeGate below for the "not
        privileged" branch."""
        _require_acl_tools()
        import getpass
        operator = getpass.getuser()

        seat = _make_seat(tmp_path, "agent-alice")
        snapshots_dir = seat / ".engram" / "health-snapshots"
        pre_existing = snapshots_dir / "1-nap-x.json"
        pre_existing.write_text("{}")

        plan = g.plan_for_seat(str(seat), viz_operator=operator)
        result = g.apply_seat_plan(plan, dry_run=False)

        assert result["error"] is None, result["error"]
        assert result["applied"] is True

        acl = subprocess.run(
            ["getfacl", "--absolute-names", str(snapshots_dir)],
            capture_output=True, text=True, timeout=10,
        )
        assert acl.returncode == 0
        assert f"user:{operator}:r-x" in acl.stdout
        assert f"default:user:{operator}:r-x" in acl.stdout
        assert "mask::r-x" in acl.stdout

        # setfacl -R is recursive -- the pre-existing file is covered too.
        file_acl = subprocess.run(
            ["getfacl", "--absolute-names", str(pre_existing)],
            capture_output=True, text=True, timeout=10,
        )
        assert f"user:{operator}:" in file_acl.stdout

    def test_real_apply_is_idempotent(self, tmp_path, g):
        """Running the same plan twice must not error and must leave the
        same ACL state (setfacl's own idempotency, exercised for real)."""
        _require_acl_tools()
        import getpass
        operator = getpass.getuser()

        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator=operator)

        first = g.apply_seat_plan(plan, dry_run=False)
        second = g.apply_seat_plan(plan, dry_run=False)
        assert first["applied"] is True
        assert second["applied"] is True
        assert second["error"] is None


# ---------------------------------------------------------------------------
# 5. Privilege gate
# ---------------------------------------------------------------------------


class TestPrivilegeGate:
    def test_raises_grant_error_when_not_owner_and_not_root(self, tmp_path, g, monkeypatch):
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="testop")

        monkeypatch.setattr(g.os, "geteuid", lambda: 12345)  # not root
        # Simulate "not the owner": stat().st_uid different from getuid()
        monkeypatch.setattr(g.os, "getuid", lambda: 1)
        real_stat = os.stat

        class _FakeStat:
            st_uid = 2  # different from getuid() above

        monkeypatch.setattr(g.os, "stat", lambda p: _FakeStat())

        run_mock_called = []
        monkeypatch.setattr(
            g.subprocess, "run",
            lambda *a, **k: run_mock_called.append(True) or (_ for _ in ()).throw(
                AssertionError("must not shell out when privilege check fails")
            ),
        )

        with pytest.raises(g.GrantError):
            g.apply_seat_plan(plan, dry_run=False)
        assert not run_mock_called, "must fail BEFORE attempting the real setfacl call"

    def test_error_message_names_the_missing_privilege(self, tmp_path, g, monkeypatch):
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="testop")

        monkeypatch.setattr(g.os, "geteuid", lambda: 12345)

        class _FakeStat:
            st_uid = 999999

        monkeypatch.setattr(g.os, "stat", lambda p: _FakeStat())
        monkeypatch.setattr(g.os, "getuid", lambda: 1)

        with pytest.raises(g.GrantError, match="insufficient privilege"):
            g.apply_seat_plan(plan, dry_run=False)

    def test_root_euid_passes_the_privilege_check(self, tmp_path, g, monkeypatch):
        """geteuid()==0 must pass _can_write_acl regardless of ownership."""
        monkeypatch.setattr(g.os, "geteuid", lambda: 0)
        assert g._can_write_acl(str(tmp_path)) is True

    def test_owner_match_passes_the_privilege_check(self, tmp_path, g):
        """The test process owns tmp_path -- the self-repair branch."""
        assert g._can_write_acl(str(tmp_path)) is True

    def test_dry_run_never_triggers_the_privilege_check(self, tmp_path, g, monkeypatch):
        """A dry-run must succeed even with zero privilege -- nothing is
        actually attempted, so nothing should be gated."""
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="testop")

        def _boom(*a, **k):
            raise AssertionError("_can_write_acl must not be consulted on a dry-run")

        monkeypatch.setattr(g, "_can_write_acl", _boom)
        result = g.apply_seat_plan(plan, dry_run=True)  # must not raise
        assert result["dry_run"] is True


# ---------------------------------------------------------------------------
# 6. main()/run() -- fleet-wide report shape + exit code
# ---------------------------------------------------------------------------


class TestRunAndMain:
    def test_run_dry_run_reports_every_seat(self, tmp_path, g):
        _make_seat(tmp_path, "agent-alice")
        _make_seat(tmp_path, "agent-bob", with_snapshots=False)

        results = g.run(root_glob=str(tmp_path / "agent-*"), viz_operator="testop", dry_run=True)
        assert len(results) == 2
        by_home = {r["seat_home"]: r for r in results}
        assert by_home[str(tmp_path / "agent-alice")]["skip_reason"] is None
        assert by_home[str(tmp_path / "agent-bob")]["skip_reason"] is not None

    def test_main_dry_run_exit_zero_no_json(self, tmp_path, g, capsys):
        _make_seat(tmp_path, "agent-alice")
        rc = g.main(["--root-glob", str(tmp_path / "agent-*"), "--viz-operator", "testop"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "WOULD RUN" in out

    def test_main_json_output_is_valid_json(self, tmp_path, g, capsys):
        _make_seat(tmp_path, "agent-alice")
        rc = g.main(["--root-glob", str(tmp_path / "agent-*"), "--viz-operator", "testop", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "results" in payload
        assert len(payload["results"]) == 1

    def test_main_fails_loudly_when_setfacl_missing(self, tmp_path, g, monkeypatch, capsys):
        monkeypatch.setattr(g.shutil, "which", lambda cmd: None)
        rc = g.main(["--root-glob", str(tmp_path / "agent-*"), "--viz-operator", "testop"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "setfacl not found" in err

    def test_main_apply_real_end_to_end(self, tmp_path, g, capsys):
        """Full CLI round-trip with --apply, real setfacl, self-owned tmp
        seat (no sudo needed)."""
        _require_acl_tools()
        import getpass
        operator = getpass.getuser()
        seat = _make_seat(tmp_path, "agent-alice")

        rc = g.main([
            "--root-glob", str(tmp_path / "agent-*"),
            "--viz-operator", operator,
            "--apply",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "OK" in out

    def test_skip_is_never_counted_as_an_error(self, tmp_path, g):
        _make_seat(tmp_path, "agent-bob", with_snapshots=False)
        results = g.run(root_glob=str(tmp_path / "agent-*"), viz_operator="testop", dry_run=True)
        assert results[0]["skip_reason"] is not None
        assert results[0]["error"] is None


# ---------------------------------------------------------------------------
# 7. Falsification control -- proves the "must not shell out" assertions
#    above are real checks, not vacuously-passing no-ops.
# ---------------------------------------------------------------------------


class TestFalsificationRealSubprocessIsReachable:
    def test_apply_seat_plan_DOES_call_subprocess_when_not_dry_run(
        self, tmp_path, g, monkeypatch
    ):
        """Control for TestApplySeatPlanDryRun's 'must not call subprocess'
        assertions: confirms subprocess.run IS reachable on the non-dry-run
        path (with privilege forced true), so those dry-run tests are
        actually distinguishing dry-run from real, not just never calling
        through in either case."""
        seat = _make_seat(tmp_path, "agent-alice")
        plan = g.plan_for_seat(str(seat), viz_operator="testop")
        monkeypatch.setattr(g, "_can_write_acl", lambda seat_home: True)

        called = []

        def _fake_run(cmd, **kwargs):
            called.append(cmd)
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()

        monkeypatch.setattr(g.subprocess, "run", _fake_run)
        result = g.apply_seat_plan(plan, dry_run=False)
        assert called, "subprocess.run must be reached on the real-apply path"
        assert result["applied"] is True


# ---------------------------------------------------------------------------
# 8. No default operator (engram-alpha#2720, Lei's ruling) -- resolution
#    precedence + loud refusal when nothing resolves.
# ---------------------------------------------------------------------------


class TestNoDefaultVizOperator:
    def test_plan_for_seat_requires_viz_operator_explicitly(self, tmp_path, g):
        """No default -- omitting viz_operator is a TypeError, not a silent
        'spt'. This is the control proving the old default is actually gone
        (not merely unused by every existing call site)."""
        seat = _make_seat(tmp_path, "agent-alice")
        with pytest.raises(TypeError):
            g.plan_for_seat(str(seat))  # type: ignore[call-arg]

    def test_run_requires_viz_operator_as_keyword(self, tmp_path, g):
        """No default on run() either -- and it's keyword-only."""
        with pytest.raises(TypeError):
            g.run(root_glob=str(tmp_path / "agent-*"))  # type: ignore[call-arg]

    def test_module_has_no_default_viz_operator_constant(self, g):
        """The old DEFAULT_VIZ_OPERATOR = 'spt' constant must not survive
        anywhere in the module."""
        assert not hasattr(g, "DEFAULT_VIZ_OPERATOR")

    def test_resolve_prefers_cli_value_over_env_and_config(self, tmp_path, g, monkeypatch):
        monkeypatch.setenv("VIZ_OPERATOR", "envop")
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=cfgop\n")
        monkeypatch.setattr(g, "AGENTCTL_CONFIG_PATH", str(cfg))
        assert g.resolve_viz_operator("cliop") == "cliop"

    def test_resolve_falls_back_to_env_when_no_cli_value(self, tmp_path, g, monkeypatch):
        monkeypatch.setenv("VIZ_OPERATOR", "envop")
        monkeypatch.setattr(g, "AGENTCTL_CONFIG_PATH", str(tmp_path / "absent.conf"))
        assert g.resolve_viz_operator(None) == "envop"

    def test_resolve_falls_back_to_config_when_no_cli_or_env(self, tmp_path, g, monkeypatch):
        monkeypatch.delenv("VIZ_OPERATOR", raising=False)
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("# comment\nVIZ_OPERATOR=cfgop\n")
        monkeypatch.setattr(g, "AGENTCTL_CONFIG_PATH", str(cfg))
        # SIMULATED: this tmp_path file is owned by whatever account runs
        # this test suite, never root, so the real provenance check
        # (engram-alpha#2720 colleague-review residual gap) would reject
        # it. Monkeypatched to pass here so this test can keep verifying
        # resolve_viz_operator's config-fallback WIRING specifically -- the
        # provenance check itself (including its real, non-simulated
        # rejection path) is covered exhaustively by
        # TestVizOperatorConfProvenanceGate and
        # TestReadVizOperatorFromConfigWithProvenanceCheck below.
        monkeypatch.setattr(g, "_viz_operator_conf_provenance_ok", lambda uid, mode: True)
        assert g.resolve_viz_operator(None) == "cfgop"

    def test_resolve_returns_none_when_nothing_set(self, tmp_path, g, monkeypatch):
        monkeypatch.delenv("VIZ_OPERATOR", raising=False)
        monkeypatch.setattr(g, "AGENTCTL_CONFIG_PATH", str(tmp_path / "absent.conf"))
        assert g.resolve_viz_operator(None) is None

    def test_config_reader_ignores_comments_and_blank_lines(self, tmp_path, g):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("\n# a comment\n\nVIZ_OPERATOR=quietop\n")
        assert g._read_viz_operator_from_config(str(cfg)) == "quietop"

    def test_config_reader_strips_quotes(self, tmp_path, g):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text('VIZ_OPERATOR="quotedop"\n')
        assert g._read_viz_operator_from_config(str(cfg)) == "quotedop"

    def test_config_reader_returns_none_for_missing_file(self, tmp_path, g):
        assert g._read_viz_operator_from_config(str(tmp_path / "nope.conf")) is None

    def test_main_refuses_loudly_when_viz_operator_unresolved(self, tmp_path, g, monkeypatch, capsys):
        """Preflight, not mid-way: refuses before even the setfacl-presence
        check, with a message naming BOTH the variable and the setting path."""
        monkeypatch.delenv("VIZ_OPERATOR", raising=False)
        monkeypatch.setattr(g, "AGENTCTL_CONFIG_PATH", str(tmp_path / "absent.conf"))
        rc = g.main(["--root-glob", str(tmp_path / "agent-*")])
        assert rc != 0
        err = capsys.readouterr().err
        assert "VIZ_OPERATOR" in err
        assert g.AGENTCTL_CONFIG_PATH in err

    def test_main_proceeds_when_viz_operator_set_via_env(self, tmp_path, g, monkeypatch, capsys):
        _make_seat(tmp_path, "agent-alice")
        monkeypatch.setenv("VIZ_OPERATOR", "envop")
        monkeypatch.setattr(g, "AGENTCTL_CONFIG_PATH", str(tmp_path / "absent.conf"))
        rc = g.main(["--root-glob", str(tmp_path / "agent-*"), "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "u:envop:rX" in payload["results"][0]["commands"][0]


# ---------------------------------------------------------------------------
# 8b. _viz_operator_conf_provenance_ok -- the residual gap a colleague
#     reviewer found on PR #2734: agentctl.conf is no longer trusted as
#     CODE, but its VALUE was still trusted as AUTHORIZATION (the setfacl
#     ACL grantee) with no check on who controls the file it came from.
#     $SHARED_ROOT is group-writable by every agent account, so any agent
#     could still plant agentctl.conf and pick who gets ACL read access to
#     every seat's ENGRAM data. This check must compare the file's ACTUAL
#     owning uid against uid 0 explicitly -- NOT ``os.getuid()`` (the
#     caller's own uid), which this tool (usually run as root, since it
#     grants ACLs on other users' directories) would satisfy trivially even
#     for an attacker-planted file.
# ---------------------------------------------------------------------------


class TestVizOperatorConfProvenanceGate:
    def test_root_owned_not_writable_accepted(self, g):
        assert g._viz_operator_conf_provenance_ok(0, 0o644) is True

    def test_root_owned_read_only_accepted(self, g):
        assert g._viz_operator_conf_provenance_ok(0, 0o400) is True

    def test_root_owned_group_writable_rejected(self, g):
        assert g._viz_operator_conf_provenance_ok(0, 0o664) is False

    def test_root_owned_other_writable_rejected(self, g):
        assert g._viz_operator_conf_provenance_ok(0, 0o646) is False

    def test_root_owned_group_and_other_writable_rejected(self, g):
        assert g._viz_operator_conf_provenance_ok(0, 0o666) is False

    def test_non_root_owned_rejected_even_with_restrictive_mode(self, g):
        """The load-bearing case the reviewer named explicitly: a
        restrictive mode (0o600, no group/other bits at all) must NOT
        substitute for actual root ownership."""
        assert g._viz_operator_conf_provenance_ok(1000, 0o600) is False

    def test_non_root_owned_rejected_even_at_mode_400(self, g):
        assert g._viz_operator_conf_provenance_ok(1000, 0o400) is False


class TestConfigProvenanceOkRealFile:
    """_config_provenance_ok against a REAL file -- genuinely exercises
    os.stat, no mocking. This test process is never root, so a real
    tmp_path file always demonstrates the rejection path end-to-end."""

    def test_real_test_owned_file_rejected(self, tmp_path, g):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        assert g._config_provenance_ok(str(cfg)) is False

    def test_missing_file_rejected(self, tmp_path, g):
        assert g._config_provenance_ok(str(tmp_path / "nope.conf")) is False


class TestReadVizOperatorFromConfigWithProvenanceCheck:
    def test_missing_file_is_silent_none_no_warning(self, tmp_path, g, capsys):
        """'Doesn't exist' and 'exists but fails provenance' must NOT read
        the same -- this case must print nothing at all."""
        result = g._read_viz_operator_from_config_with_provenance_check(
            str(tmp_path / "nope.conf")
        )
        assert result is None
        err = capsys.readouterr().err
        assert err == "", "a missing file must not warn -- that's the plain 'unset' case"

    def test_real_non_root_owned_file_rejected_with_warning(self, tmp_path, g, capsys):
        """GENUINE (not simulated): a real file owned by whoever runs this
        test suite is never root-owned, so this exercises the rejection +
        warning path end-to-end via the real os.stat call -- distinct from
        the silent missing-file case above."""
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        result = g._read_viz_operator_from_config_with_provenance_check(str(cfg))
        assert result is None
        err = capsys.readouterr().err
        assert err.strip() != "", "rejection must warn, distinct from the silent missing-file case"
        assert "ownership" in err.lower() or "permission" in err.lower()
        assert str(cfg) in err

    def test_legitimate_value_reads_through_once_provenance_simulated_ok(
        self, tmp_path, g, monkeypatch
    ):
        """SIMULATED: _viz_operator_conf_provenance_ok is monkeypatched to
        always return True, standing in for 'this file really is root-owned
        and not group/other-writable' -- impossible to produce for real in
        an unprivileged test sandbox (there is no way to chown a fixture to
        root here). Confirms the existing legitimate-read behavior
        (VIZ_OPERATOR=<value> extracted correctly) still works once the
        provenance gate is satisfied."""
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        monkeypatch.setattr(g, "_viz_operator_conf_provenance_ok", lambda uid, mode: True)
        result = g._read_viz_operator_from_config_with_provenance_check(str(cfg))
        assert result == "someop"

    def test_rejection_message_distinguishes_from_generic_unset(self, tmp_path, g, capsys):
        """The rejection warning must be distinguishable from the generic
        'VIZ_OPERATOR is not set' message main() prints -- an operator
        debugging 'why isn't my config being read' needs a different
        answer than 'you never set it.'"""
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        g._read_viz_operator_from_config_with_provenance_check(str(cfg))
        err = capsys.readouterr().err
        assert "provenance" in err.lower() or "ownership" in err.lower() or "permission" in err.lower()
        assert "is not set" not in err.lower()


# ---------------------------------------------------------------------------
# 9. Charset gate (engram-alpha#2734 suggestion 2) -- VIZ_OPERATOR must be
#    validated before being embedded in a setfacl ACL-entry string. setfacl
#    parses its entry-list argument as COMMA-separated ACL entries, so an
#    unvalidated value containing a comma could inject an additional,
#    unintended ACL entry.
# ---------------------------------------------------------------------------


class TestVizOperatorCharsetGate:
    def test_plain_lowercase_name_ok(self, g):
        assert g._viz_operator_charset_ok("spt") is True

    def test_name_with_underscore_and_hyphen_ok(self, g):
        assert g._viz_operator_charset_ok("some_op-name") is True

    def test_uppercase_rejected(self, g):
        assert g._viz_operator_charset_ok("Spt") is False

    def test_comma_injection_rejected(self, g):
        """The reviewer-fairy's suggested attack: a comma lets the value
        inject an ADDITIONAL, unintended setfacl ACL entry
        (e.g. 'spt,u:evil:rwx' grants 'evil' rwx too)."""
        assert g._viz_operator_charset_ok("spt,u:evil:rwx") is False

    def test_semicolon_rejected(self, g):
        assert g._viz_operator_charset_ok("spt; rm -rf /") is False

    def test_space_rejected(self, g):
        assert g._viz_operator_charset_ok("spt evil") is False

    def test_empty_string_rejected(self, g):
        assert g._viz_operator_charset_ok("") is False

    def test_main_refuses_loudly_on_invalid_charset_before_setfacl_check(
        self, tmp_path, g, monkeypatch, capsys
    ):
        """Preflight, not mid-way: refuses before the setfacl-presence check
        (proven by monkeypatching shutil.which to blow up if reached), with
        a message naming VIZ_OPERATOR and its offending value."""
        _make_seat(tmp_path, "agent-alice")

        def _boom(cmd):
            raise AssertionError("must refuse on charset BEFORE checking for setfacl")

        monkeypatch.setattr(g.shutil, "which", _boom)
        rc = g.main([
            "--root-glob", str(tmp_path / "agent-*"),
            "--viz-operator", "spt,u:evil:rwx",
        ])
        assert rc != 0
        err = capsys.readouterr().err
        assert "VIZ_OPERATOR" in err
        assert "spt,u:evil:rwx" in err

    def test_main_never_reaches_subprocess_on_invalid_charset(
        self, tmp_path, g, monkeypatch, capsys
    ):
        """End-to-end confirmation that an injection-shaped VIZ_OPERATOR
        never reaches a real setfacl invocation via the CLI entrypoint."""
        _make_seat(tmp_path, "agent-alice")

        def _boom(*a, **k):
            raise AssertionError("subprocess.run must not be reached")

        monkeypatch.setattr(g.subprocess, "run", _boom)
        rc = g.main([
            "--root-glob", str(tmp_path / "agent-*"),
            "--viz-operator", "spt,u:evil:rwx",
            "--apply",
        ])
        assert rc != 0

    def test_main_proceeds_with_valid_charset(self, tmp_path, g, capsys):
        _make_seat(tmp_path, "agent-alice")
        rc = g.main([
            "--root-glob", str(tmp_path / "agent-*"),
            "--viz-operator", "valid_op-1",
            "--json",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "u:valid_op-1:rX" in payload["results"][0]["commands"][0]
