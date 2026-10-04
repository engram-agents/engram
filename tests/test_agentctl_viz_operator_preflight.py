"""Tests for agentctl's VIZ_OPERATOR preflight gate (engram-alpha#2720).

Lei's ruling (relayed by Kepler, 2026-09-07): no default operator account
("spt" was a functional leak of one host's personal setting into shipped
code). VIZ_OPERATOR must have NO default; cmd_spawn and cmd_finalize_name
must refuse loudly -- before any useradd/usermod -- when it is unset, with
a message that names the variable and shows how to set it (persistently
via agentctl's shared config file, or per-invocation via env var). Every
other subcommand (health, session, attach, ...) must keep working with no
VIZ_OPERATOR set at all.

Two layers of coverage, matching this repo's established pattern for
testing agentctl (see test_agentctl_chosen_name_validation.py):

  1. Black-box: invoke the hidden `__require-viz-operator` verb (extracted
     from cmd_spawn/cmd_finalize_name's shared preflight logic) via
     subprocess -- no sudo, no real users, no filesystem side effects.
  2. Structural: cmd_spawn and cmd_finalize_name's own source must call
     _require_viz_operator strictly BEFORE any mutating call (bootstrap_path
     invocation / usermod) -- a preflight can be correctly implemented as a
     helper yet wired in AFTER the mutation it's meant to guard, which the
     black-box layer alone cannot catch (it only proves the helper itself
     is correct in isolation, not that callers invoke it early enough).

Caveat on the black-box "unset" tests: they control VIZ_OPERATOR via the
environment only. If a real /home/agents-shared/agentctl.conf happens to
exist and set VIZ_OPERATOR on the host running these tests, the "unset"
case would not actually be unset (agentctl sources that file when the env
var is empty). This is a real gap in test isolation, not fixed here --
agentctl hardcodes SHARED_ROOT (and therefore the config path) with no
override hook, and adding one is out of this fix's scope. Documented so a
failure here is investigated as "does this host have that file", not
assumed-flaky.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

AGENTCTL = Path(__file__).resolve().parent.parent / "tools" / "agentctl"
AGENTCTL_SOURCE = AGENTCTL.read_text()


def _run(*args: str, viz_operator: str | None) -> subprocess.CompletedProcess:
    """Run agentctl with a controlled VIZ_OPERATOR (None = deleted from env)."""
    import os

    env = dict(os.environ)
    if viz_operator is None:
        env.pop("VIZ_OPERATOR", None)
    else:
        env["VIZ_OPERATOR"] = viz_operator
    return subprocess.run(
        ["bash", str(AGENTCTL), *args],
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# 1. Black-box: __require-viz-operator
# ---------------------------------------------------------------------------


class TestRequireVizOperatorVerb:
    def test_unset_refuses_before_mutating_anything(self):
        """VIZ_OPERATOR unset -> non-zero exit, no mutation attempted (the
        verb itself never mutates anything -- it's pure preflight)."""
        proc = _run("__require-viz-operator", "spawn", viz_operator=None)
        assert proc.returncode != 0, f"expected non-zero exit; got {proc.returncode}, stderr={proc.stderr!r}"
        assert proc.returncode == 3, f"expected exit 3; got {proc.returncode}"

    def test_unset_message_names_the_variable(self):
        proc = _run("__require-viz-operator", "spawn", viz_operator=None)
        assert "VIZ_OPERATOR" in proc.stderr, f"stderr must name VIZ_OPERATOR; got: {proc.stderr!r}"

    def test_unset_message_names_the_setting_path(self):
        proc = _run("__require-viz-operator", "spawn", viz_operator=None)
        assert "/home/agents-shared/agentctl.conf" in proc.stderr, (
            f"stderr must name the persistent setting path; got: {proc.stderr!r}"
        )

    def test_unset_message_names_the_calling_command(self):
        proc = _run("__require-viz-operator", "finalize-name", viz_operator=None)
        assert "finalize-name" in proc.stderr, f"stderr must name the caller; got: {proc.stderr!r}"

    def test_unset_message_shows_a_one_off_env_var_form(self):
        proc = _run("__require-viz-operator", "spawn", viz_operator=None)
        assert "VIZ_OPERATOR=" in proc.stderr and "sudo" in proc.stderr, (
            f"stderr must show the one-off env-var form; got: {proc.stderr!r}"
        )

    def test_set_proceeds(self):
        """VIZ_OPERATOR set -> exit 0, no error output."""
        proc = _run("__require-viz-operator", "spawn", viz_operator="testop")
        assert proc.returncode == 0, f"expected exit 0; got {proc.returncode}, stderr={proc.stderr!r}"
        assert proc.stdout.strip() == "OK"
        assert proc.stderr == ""

    def test_empty_string_is_treated_as_unset(self):
        """An explicitly-empty VIZ_OPERATOR="" must refuse too (matches the
        old Step 7 guard's own `[[ -z ]]` semantics -- empty is not a value)."""
        proc = _run("__require-viz-operator", "spawn", viz_operator="")
        assert proc.returncode == 3, f"expected exit 3 for empty string; got {proc.returncode}"


# ---------------------------------------------------------------------------
# 2. Structural: preflight must run BEFORE any mutation in the real callers
# ---------------------------------------------------------------------------


def _function_body(name: str) -> str:
    """Extract one `cmd_X() { ... }` function's source text by slicing
    between its own header and the next top-level `cmd_` header (this
    file's functions are never nested, so this is a safe, simple slice)."""
    start_marker = f"\n{name}() {{"
    start = AGENTCTL_SOURCE.index(start_marker)
    rest = AGENTCTL_SOURCE[start + 1:]
    next_cmd = rest.find("\ncmd_", len(start_marker) - 1)
    return rest[:next_cmd] if next_cmd != -1 else rest


class TestPreflightOrdering:
    def test_cmd_spawn_calls_require_viz_operator(self):
        body = _function_body("cmd_spawn")
        assert '_require_viz_operator "spawn"' in body

    def test_cmd_spawn_gates_before_bootstrap_invocation(self):
        """The bootstrap_path invocation is what actually does useradd (via
        agent-bootstrap) -- the preflight must precede it."""
        body = _function_body("cmd_spawn")
        gate_pos = body.index('_require_viz_operator "spawn"')
        bootstrap_pos = body.index('"$bootstrap_path" "${bootstrap_args[@]}"')
        assert gate_pos < bootstrap_pos, (
            "_require_viz_operator must run BEFORE the bootstrap_path "
            "invocation (which performs useradd)"
        )

    def test_cmd_finalize_name_calls_require_viz_operator(self):
        body = _function_body("cmd_finalize_name")
        assert '_require_viz_operator "finalize-name"' in body

    def test_cmd_finalize_name_gates_before_usermod(self):
        body = _function_body("cmd_finalize_name")
        gate_pos = body.index('_require_viz_operator "finalize-name"')
        usermod_pos = body.index('usermod -l "$chosen_user"')
        assert gate_pos < usermod_pos, (
            "_require_viz_operator must run BEFORE the usermod rename"
        )

    def test_cmd_finalize_name_gates_before_process_termination(self):
        """Step 1.5 (loginctl/pkill against the newborn's processes) is
        itself a side effect on shared state -- the gate must precede it
        too, not just the usermod line, per 'refuses before mutating
        anything'."""
        body = _function_body("cmd_finalize_name")
        gate_pos = body.index('_require_viz_operator "finalize-name"')
        term_pos = body.index('loginctl disable-linger')
        assert gate_pos < term_pos, (
            "_require_viz_operator must run BEFORE Step 1.5's process termination"
        )


# ---------------------------------------------------------------------------
# 3. No default anywhere -- the control the issue names explicitly
# ---------------------------------------------------------------------------


class TestNoDefaultSurvives:
    def test_no_bash_parameter_expansion_default_for_viz_operator(self):
        """grep -n ':-spt' across tools/agentctl must find nothing -- the
        exact control named in engram-alpha#2720's addendum."""
        assert ":-spt" not in AGENTCTL_SOURCE

    def test_unrelated_subcommands_do_not_reference_require_viz_operator(self):
        """health/session/attach/bash/share/unshare/share-status must NOT
        gate on VIZ_OPERATOR -- only spawn and finalize-name do."""
        for name in (
            "cmd_health", "cmd_session", "cmd_bash", "cmd_attach",
            "cmd_share", "cmd_unshare", "cmd_share_status",
        ):
            body = _function_body(name)
            assert "_require_viz_operator" not in body, (
                f"{name} must not gate on VIZ_OPERATOR (unrelated function "
                f"must keep working with no operator set)"
            )


# ---------------------------------------------------------------------------
# 4. _read_viz_operator_from_config -- restricted, non-executing config read
#    (engram-alpha#2734: `source "$AGENTCTL_CONFIG"` was an unauthenticated
#    root-code-execution vector, live-demonstrated by a reviewer-fairy --
#    $SHARED_ROOT is mode 3775 group "agents", writable by every agent
#    account, so any agent could plant a command in agentctl.conf that would
#    execute with the privilege of whoever next ran agentctl with
#    VIZ_OPERATOR unset, including root via sudo).
# ---------------------------------------------------------------------------


def _run_read_config(config_path) -> subprocess.CompletedProcess:
    """Invoke the hidden `__read-viz-operator-config` verb directly -- no
    sudo, no real users, no mutation. Never points at the real
    /home/agents-shared/agentctl.conf; callers always pass a tmp_path file."""
    return subprocess.run(
        ["bash", str(AGENTCTL), "__read-viz-operator-config", str(config_path)],
        capture_output=True,
        text=True,
    )


class TestReadVizOperatorFromConfig:
    def test_legit_plain_assignment(self, tmp_path):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=somename\n")
        proc = _run_read_config(cfg)
        assert proc.returncode == 0
        assert proc.stdout.strip() == "somename"

    def test_legit_quoted_assignment(self, tmp_path):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text('VIZ_OPERATOR="somename"\n')
        proc = _run_read_config(cfg)
        assert proc.stdout.strip() == "somename"

    def test_ignores_comments_and_blank_lines(self, tmp_path):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("\n# a comment\n\nVIZ_OPERATOR=quietop\n")
        proc = _run_read_config(cfg)
        assert proc.stdout.strip() == "quietop"

    def test_missing_file_prints_nothing_exits_zero(self, tmp_path):
        proc = _run_read_config(tmp_path / "nope.conf")
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""

    def test_empty_value_prints_nothing(self, tmp_path):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=\n")
        proc = _run_read_config(cfg)
        assert proc.stdout.strip() == ""

    def test_injection_semicolon_is_inert_and_extracted_as_literal(self, tmp_path):
        """The reviewer-fairy's PoC shape: a shell metacharacter/second
        command in the same VIZ_OPERATOR= line. Must NOT execute -- only the
        literal string value is extracted (which then fails the charset gate
        downstream, see TestVizOperatorCharsetGate)."""
        cfg = tmp_path / "agentctl.conf"
        marker = tmp_path / "PWNED_MARKER"
        cfg.write_text(f"VIZ_OPERATOR=spt; touch {marker}\n")
        proc = _run_read_config(cfg)
        assert not marker.exists(), (
            "injection must NOT execute -- source-based parsing would have "
            "created this marker"
        )
        assert proc.stdout.strip() == f"spt; touch {marker}", (
            "the literal (unexecuted) text must still be extracted verbatim"
        )

    def test_injection_command_substitution_is_inert(self, tmp_path):
        cfg = tmp_path / "agentctl.conf"
        marker = tmp_path / "PWNED_MARKER"
        cfg.write_text(f"VIZ_OPERATOR=$(touch {marker})\n")
        proc = _run_read_config(cfg)
        assert not marker.exists(), "command substitution must NOT execute"
        assert proc.stdout.strip() == f"$(touch {marker})"

    def test_injection_standalone_command_line_is_ignored(self, tmp_path):
        """A line with no '=' (e.g. a bare command) must be skipped entirely,
        never executed -- `source` would have run it as a statement."""
        cfg = tmp_path / "agentctl.conf"
        marker = tmp_path / "PWNED_MARKER"
        cfg.write_text(f"touch {marker}\nVIZ_OPERATOR=spt\n")
        proc = _run_read_config(cfg)
        assert not marker.exists()
        assert proc.stdout.strip() == "spt"

    def test_no_source_or_eval_in_agentctl_source(self):
        """Control (engram-alpha#2734): the fixed `source "$AGENTCTL_CONFIG"`
        line must not survive anywhere in the script, and `eval` must not be
        introduced as a substitute."""
        assert 'source "$AGENTCTL_CONFIG"' not in AGENTCTL_SOURCE
        assert "eval " not in AGENTCTL_SOURCE


# ---------------------------------------------------------------------------
# 4b. _viz_operator_conf_provenance_ok -- the residual gap a colleague
#     reviewer found on PR #2734: the file is no longer trusted as CODE, but
#     its VALUE was still trusted as AUTHORIZATION (the setfacl ACL grantee)
#     with no check on who controls the file it came from. $SHARED_ROOT is
#     group-writable by every agent account, so any agent could still plant
#     agentctl.conf and pick who gets ACL read access to every seat's ENGRAM
#     data. This provenance check must be explicit about comparing the
#     file's ACTUAL owning uid against uid 0 -- NOT a bash `[ -O ]`-style
#     test, which agentctl (usually run via sudo, i.e. as root) would pass
#     trivially even for an attacker-planted file (root owns everything it
#     can read).
# ---------------------------------------------------------------------------


def _run_provenance_check(uid: str, mode: str) -> subprocess.CompletedProcess:
    """Invoke the hidden `__viz-operator-conf-provenance-ok` verb directly --
    a PURE check over an explicit (uid, mode) pair, no real file, no stat
    call, no root needed."""
    return subprocess.run(
        ["bash", str(AGENTCTL), "__viz-operator-conf-provenance-ok", uid, mode],
        capture_output=True,
        text=True,
    )


class TestVizOperatorConfProvenanceGate:
    def test_root_owned_not_writable_accepted(self):
        assert _run_provenance_check("0", "644").returncode == 0

    def test_root_owned_read_only_accepted(self):
        assert _run_provenance_check("0", "400").returncode == 0

    def test_root_owned_group_writable_rejected(self):
        assert _run_provenance_check("0", "664").returncode != 0

    def test_root_owned_other_writable_rejected(self):
        assert _run_provenance_check("0", "646").returncode != 0

    def test_root_owned_group_and_other_writable_rejected(self):
        assert _run_provenance_check("0", "666").returncode != 0

    def test_non_root_owned_rejected_even_with_restrictive_mode(self):
        """The load-bearing case the reviewer named explicitly: a
        restrictive mode (600, no group/other bits at all) must NOT
        substitute for actual root ownership."""
        assert _run_provenance_check("1000", "600").returncode != 0

    def test_non_root_owned_rejected_even_at_mode_400(self):
        assert _run_provenance_check("1000", "400").returncode != 0


# ---------------------------------------------------------------------------
# 4c. _viz_operator_config_value_if_provenance_ok -- the decision + read,
#     taking (uid, mode) as explicit args so the "legitimate read still goes
#     through once provenance passes" case can be tested WITHOUT needing to
#     chown a fixture to root (impossible in this test sandbox). This is a
#     simulation, stated explicitly: the (uid=0, mode) pair is supplied by
#     the test, not read from the real file via stat(1) -- the real stat(1)
#     call is exercised separately in TestResolveVizOperatorFromConfigReal
#     below, which cannot simulate root ownership and so only covers the
#     rejection path (a real, test-owned file is never root-owned here).
# ---------------------------------------------------------------------------


def _run_value_if_provenance_ok(config_path, uid: str, mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash", str(AGENTCTL),
            "__viz-operator-config-value-if-provenance-ok",
            str(config_path), uid, mode,
        ],
        capture_output=True,
        text=True,
    )


class TestVizOperatorConfigValueIfProvenanceOk:
    def test_legitimate_value_reads_through_once_provenance_simulated_ok(self, tmp_path):
        """SIMULATED: uid=0/mode=644 is supplied directly by the test (not
        obtained from a real chown), standing in for 'this file really is
        root-owned and not group/other-writable'. Confirms the existing
        legitimate-read behavior (VIZ_OPERATOR=<value> extracted correctly)
        still works once the provenance gate is satisfied."""
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        proc = _run_value_if_provenance_ok(cfg, "0", "644")
        assert proc.returncode == 0
        assert proc.stdout.strip() == "someop"
        assert proc.stderr == ""

    def test_root_owned_group_writable_rejects_with_no_value_on_stdout(self, tmp_path):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        proc = _run_value_if_provenance_ok(cfg, "0", "664")
        assert proc.returncode == 0  # always returns 0; caller decides
        assert proc.stdout.strip() == "", "no value must leak through on a rejected config"

    def test_non_root_owned_rejects_regardless_of_restrictive_mode(self, tmp_path):
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        proc = _run_value_if_provenance_ok(cfg, "1000", "600")
        assert proc.stdout.strip() == ""

    def test_rejection_message_names_provenance_specifically(self, tmp_path):
        """The rejection warning must be distinguishable from the generic
        'VIZ_OPERATOR is not set' message _require_viz_operator prints --
        an operator debugging 'why isn't my config being read' needs a
        different answer than 'you never set it'."""
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        proc = _run_value_if_provenance_ok(cfg, "0", "664")
        assert "ownership" in proc.stderr.lower() or "permission" in proc.stderr.lower(), (
            f"rejection must name ownership/permission specifically; got: {proc.stderr!r}"
        )
        assert str(cfg) in proc.stderr, "rejection message should name the offending path"


# ---------------------------------------------------------------------------
# 4d. _resolve_viz_operator_from_config -- the real end-to-end flow, using
#     an actual stat(1) call against a real file. Cannot simulate root
#     ownership (this test process is never root), so this class covers
#     only the two cases reachable without it: a missing file (silent) and
#     a real, test-owned file (always non-root here, so always rejected --
#     which is itself a genuine, non-simulated demonstration of the
#     rejection path end-to-end through the real stat(1) call).
# ---------------------------------------------------------------------------


def _run_resolve_from_config(config_path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(AGENTCTL), "__resolve-viz-operator-from-config", str(config_path)],
        capture_output=True,
        text=True,
    )


class TestResolveVizOperatorFromConfigReal:
    def test_missing_file_is_silent_and_treated_as_unset(self, tmp_path):
        """'Doesn't exist' and 'exists but fails provenance' must NOT read
        the same -- this case prints nothing at all (neither stdout nor
        stderr), distinct from the warning in the next test."""
        proc = _run_resolve_from_config(tmp_path / "nope.conf")
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert proc.stderr.strip() == ""

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="agentctl is Linux-only by design (see its header; macOS uses "
        "agentctl-mac); this test exercises GNU `stat -c`, which BSD stat rejects",
    )
    def test_real_test_owned_file_is_rejected_with_warning_not_silence(self, tmp_path):
        """GENUINE (not simulated) exercise of the real stat(1) call: a real
        file owned by whatever account runs this test suite is never
        root-owned, so this is a true end-to-end demonstration of the
        rejection + warning path -- distinct from the silent missing-file
        case above."""
        cfg = tmp_path / "agentctl.conf"
        cfg.write_text("VIZ_OPERATOR=someop\n")
        proc = _run_resolve_from_config(cfg)
        assert proc.returncode == 0
        assert proc.stdout.strip() == "", "a non-root-owned file's value must never leak through"
        assert proc.stderr.strip() != "", "rejection must warn, not silently mimic the missing-file case"
        assert "ownership" in proc.stderr.lower() or "permission" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# 5. _viz_operator_charset_ok -- charset gate before embedding VIZ_OPERATOR
#    in a setfacl ACL-entry string (engram-alpha#2734 suggestion 2).
# ---------------------------------------------------------------------------


def _run_charset_check(value: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(AGENTCTL), "__viz-operator-charset-ok", value],
        capture_output=True,
        text=True,
    )


class TestVizOperatorCharsetGate:
    def test_plain_lowercase_name_ok(self):
        assert _run_charset_check("spt").returncode == 0

    def test_name_with_underscore_and_hyphen_ok(self):
        assert _run_charset_check("some_op-name").returncode == 0

    def test_uppercase_rejected(self):
        assert _run_charset_check("Spt").returncode != 0

    def test_comma_injection_rejected(self):
        """The reviewer-fairy's suggested attack: a comma lets the value
        inject an ADDITIONAL, unintended setfacl ACL entry."""
        assert _run_charset_check("spt,u:evil:rwx").returncode != 0

    def test_semicolon_rejected(self):
        assert _run_charset_check("spt; rm -rf /").returncode != 0

    def test_space_rejected(self):
        assert _run_charset_check("spt evil").returncode != 0

    def test_empty_string_rejected(self):
        assert _run_charset_check("").returncode != 0

    def test_step7_gates_before_any_setfacl_call(self):
        """Structural: cmd_finalize_name's Step 7 must check
        _viz_operator_charset_ok BEFORE its first setfacl invocation."""
        body = _function_body("cmd_finalize_name")
        gate_pos = body.index("_viz_operator_charset_ok")
        setfacl_pos = body.index('setfacl -R -m "u:${VIZ_OPERATOR}:rX"')
        assert gate_pos < setfacl_pos, (
            "_viz_operator_charset_ok must run BEFORE Step 7's first setfacl call"
        )
