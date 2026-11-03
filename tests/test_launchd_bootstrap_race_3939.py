"""alpha #3939 -- setup-surface-daemon-service.sh on macOS: the bootout-then-bootstrap race, and
unknown arguments running a real install.

Contract (the cells below assert it; every one runs against a STUB `launchctl` first on PATH, never
the real one, #3519):
  1. After `bootout`, wait until `launchctl print gui/<uid>/<label>` FAILS (service gone) before
     `bootstrap`; bounded; at the deadline still attempt bootstrap, and a final error says the
     service was still loaded.
  2. bootstrap rc 0 -> success; rc 125 -> UNCHANGED (named no-GUI-domain message, exit 3, one call);
     any other rc -> up to 5 attempts, 1 s apart; all failed -> exit with launchctl's LAST rc and one
     loud stderr message (label, attempt count, last launchctl stderr, manual recovery command).
  3. success after a retry prints one line with the attempt count.
  4. Env overrides ENGRAM_SETUP_LAUNCHD_POLL_S / _RETRY_S / _WAIT_S keep the tests from sleeping.
  5. -h/--help: usage, rc 0, nothing written, no service-manager call; any other unknown argument:
     rc 2, names it, before anything is written or any service-manager call.

Both the --owner install and the plain surface-daemon install share the one launchctl block, so the
bootstrap cells are parametrized over both.
"""
from __future__ import annotations

import time

import pytest

from test_owner_launchd_3709 import DSandbox, UID, label_for
from test_owner_launcher_3663 import _stub
from test_surface_label_keyed_4305 import key_of

# #4305: the surface daemon's label is keyed on the home, like the owner's. The script now also asks
# `launchctl print` about the OLD fixed label once (the legacy-transition check, answered "not found" here);
# that one read-only call is filtered out of what these cells count, so they still pin the KEYED label's
# bootout / print-poll / bootstrap sequence exactly as before.
LEGACY_SURFACE_LABEL = "com.engram.surface-daemon"
TINY = {"ENGRAM_SETUP_LAUNCHD_POLL_S": "0.01", "ENGRAM_SETUP_LAUNCHD_RETRY_S": "0.01",
        "ENGRAM_SETUP_LAUNCHD_WAIT_S": "2"}


class Rig:
    """DSandbox + a counting launchctl stub.
    print_present: the first K `print` calls answer "present" (rc 0), later ones "gone" (rc 113).
    boot_fail_first / boot_rc: the first M `bootstrap` calls fail with boot_rc, later ones succeed."""

    def __init__(self, tmp, mode: str, os_name: str = "Darwin", print_present: int = 0,
                 boot_fail_first: int = 0, boot_rc: int = 5):
        self.sb = DSandbox(tmp / "i", os_name=os_name)
        self.mode = mode
        self.home = self.sb.tmp / "seat-a"
        cnt = self.sb.tmp / "cnt"
        cnt.mkdir()
        _stub(self.sb.stubs, "launchctl", f"""#!/usr/bin/env bash
echo "$@" >> "{self.sb.launchlog}"
bump() {{ f="{cnt}/$1"; n=$(cat "$f" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$f"; echo "$n"; }}
case "$1" in
  print)
    case "$2" in */{LEGACY_SURFACE_LABEL}) echo "Could not find service" >&2; exit 113 ;; esac
    n=$(bump print)
    if [ "$n" -le {print_present} ]; then echo "service = {{ state = running }}"; exit 0; fi
    echo "Could not find service" >&2; exit 113 ;;
  bootstrap)
    n=$(bump bootstrap)
    if [ "$n" -le {boot_fail_first} ]; then echo "Bootstrap failed: {boot_rc}: Input/output error" >&2; exit {boot_rc}; fi
    exit 0 ;;
esac
exit 0
""")

    @property
    def label(self) -> str:
        return label_for(self.home) if self.mode == "owner" else f"{LEGACY_SURFACE_LABEL}.{key_of(self.home)}"

    @property
    def plist(self):
        return self.sb.agents_dir / f"{self.label}.plist"

    def run(self, *args: str, extra: dict | None = None):
        env = {**TINY, **(extra or {})}
        a = (["--owner"] if self.mode == "owner" else []) + list(args)
        return self.sb.run_env(*a, engram_home=str(self.home), extra=env)

    def verbs(self) -> list:
        return [c[0] for c in self.calls()]

    def calls(self) -> list:
        return [c for c in self.sb.launchctl_calls()
                if not (c[0] == "print" and c[1].endswith("/" + LEGACY_SURFACE_LABEL))]


@pytest.fixture(params=["owner", "plain"])
def mode(request):
    return request.param


# ---- (a) bootstrap only after the service is gone ---------------------------------------------

def test_a_bootstrap_waits_until_print_reports_the_service_gone(tmp_path, mode):
    rig = Rig(tmp_path, mode, print_present=3)
    r = rig.run()
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    # 3 "present" answers, the 4th reports gone, and only THEN bootstrap
    assert rig.verbs() == ["bootout", "print", "print", "print", "print", "bootstrap"], rig.calls()
    printed = [c for c in rig.calls() if c[0] == "print"]
    assert all(c == ["print", f"gui/{UID}/{rig.label}"] for c in printed), printed


# ---- (b) transient bootstrap failures are retried ----------------------------------------------

def test_b_two_failures_then_success_is_rc0_with_three_attempts_and_the_attempt_line(tmp_path, mode):
    rig = Rig(tmp_path, mode, boot_fail_first=2)
    r = rig.run()
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert rig.verbs().count("bootstrap") == 3, rig.calls()
    assert "launchctl bootstrap succeeded on attempt 3 of 5" in [ln.strip() for ln in r.stdout.splitlines()], r.stdout
    assert "Loaded." in r.stdout


def test_b_first_try_success_prints_no_attempt_line(tmp_path, mode):
    rig = Rig(tmp_path, mode)
    r = rig.run()
    assert r.returncode == 0 and "attempt" not in r.stdout, (r.stdout, r.stderr)
    assert rig.verbs().count("bootstrap") == 1


# ---- (c) every attempt fails: rc 5, one loud message, 5 attempts --------------------------------

def test_c_always_rc5_exits_5_after_five_attempts_with_the_loud_message(tmp_path, mode):
    rig = Rig(tmp_path, mode, boot_fail_first=999, boot_rc=5)
    r = rig.run()
    assert r.returncode == 5, (r.returncode, r.stdout, r.stderr)
    assert rig.verbs().count("bootstrap") == 5, rig.calls()
    assert "Loaded." not in r.stdout and "Setup complete" not in r.stdout
    assert rig.label in r.stderr, r.stderr
    assert "5 attempts" in r.stderr, r.stderr
    assert "Bootstrap failed: 5: Input/output error" in r.stderr, r.stderr   # launchctl's last stderr text
    assert f"launchctl bootstrap gui/{UID} {rig.plist}" in r.stderr.splitlines()[-1], r.stderr
    assert "still loaded" not in r.stderr, "the service was gone here: the message must not claim otherwise"


def test_c_the_final_exit_is_launchctls_LAST_rc_not_the_first(tmp_path):
    rig = Rig(tmp_path, "plain", boot_fail_first=999, boot_rc=37)
    r = rig.run()
    assert r.returncode == 37, (r.returncode, r.stderr)
    assert rig.verbs().count("bootstrap") == 5


# ---- (d) rc 125 is unchanged --------------------------------------------------------------------

def test_d_rc125_is_the_existing_exit3_message_with_exactly_one_bootstrap(tmp_path, mode):
    rig = Rig(tmp_path, mode, boot_fail_first=999, boot_rc=125)
    r = rig.run()
    subject = "The owner" if mode == "owner" else "The surface daemon"
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert rig.verbs().count("bootstrap") == 1, rig.calls()
    # the lines recorded from the pre-fix script (stub stderr first, then the named message), verbatim
    err = r.stderr.splitlines()
    assert err[-2:] == [
        "Bootstrap failed: 125: Input/output error",
        f"ERROR: launchd has no GUI domain for uid {UID} (no GUI login session). {rig.plist} was written but "
        f"is not active now; whether launchd loads it at this account's next GUI login has not been "
        f"measured. {subject} is not supervised on this seat."], err
    assert r.stdout.endswith("Step 2: Loading via launchctl\n"), r.stdout   # nothing printed after the step line
    assert rig.plist.exists()


def test_d_rc125_on_a_later_attempt_also_takes_the_unchanged_path(tmp_path):
    rig = Rig(tmp_path, "plain")
    # first attempt rc 5, second rc 125: the 125 exits 3 at once
    _stub(rig.sb.stubs, "launchctl", f"""#!/usr/bin/env bash
echo "$@" >> "{rig.sb.launchlog}"
if [ "$1" = bootstrap ]; then
  n=$(grep -c '^bootstrap' "{rig.sb.launchlog}")
  if [ "$n" -le 1 ]; then echo "Bootstrap failed: 5" >&2; exit 5; fi
  echo "Bootstrap failed: 125" >&2; exit 125
fi
if [ "$1" = print ]; then exit 113; fi
exit 0
""")
    r = rig.run()
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert rig.verbs().count("bootstrap") == 2
    assert "no GUI domain" in r.stderr


# ---- (e) the service never goes away: bounded, still attempted ---------------------------------

def test_e_service_never_gone_still_attempts_bootstrap_and_stays_bounded(tmp_path, mode):
    rig = Rig(tmp_path, mode, print_present=10**6, boot_fail_first=999, boot_rc=5)
    t0 = time.monotonic()
    r = rig.run(extra={"ENGRAM_SETUP_LAUNCHD_WAIT_S": "0.05", "ENGRAM_SETUP_LAUNCHD_POLL_S": "0.01"})
    elapsed = time.monotonic() - t0
    assert r.returncode == 5, (r.returncode, r.stdout, r.stderr)
    assert rig.verbs().count("bootstrap") == 5, "bootstrap must still be attempted at the deadline"
    n_print = rig.verbs().count("print")
    assert 1 <= n_print <= 8, f"poll must be bounded by WAIT_S/POLL_S, got {n_print} print calls"
    assert elapsed < 20, elapsed
    assert "still loaded" in r.stderr, r.stderr
    assert rig.verbs().index("bootstrap") > max(i for i, v in enumerate(rig.verbs()) if v == "print")


def test_e_service_never_gone_but_bootstrap_works_is_still_success(tmp_path):
    rig = Rig(tmp_path, "plain", print_present=10**6)
    r = rig.run(extra={"ENGRAM_SETUP_LAUNCHD_WAIT_S": "0.05", "ENGRAM_SETUP_LAUNCHD_POLL_S": "0.01"})
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


# ---- (f)/(g) arguments --------------------------------------------------------------------------

def _no_side_effects(rig: Rig):
    assert not rig.sb.launchlog.exists(), rig.sb.launchlog.read_text()
    assert not rig.sb.syslog.exists(), rig.sb.syslog.read_text()
    assert not (rig.sb.home / "Library").exists(), "a LaunchAgents dir/plist was created"
    assert not (rig.sb.home / ".config").exists(), "a systemd unit dir was created"
    assert not rig.home.exists(), "the ENGRAM_HOME dir was created"


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_f_help_prints_usage_exits_0_and_touches_nothing(tmp_path, flag, os_name):
    rig = Rig(tmp_path, "plain", os_name=os_name)
    r = rig.sb.run_env(flag, engram_home=str(rig.home), extra=TINY)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "Usage:" in r.stdout and "--owner" in r.stdout and "--allow-path" in r.stdout, r.stdout
    _no_side_effects(rig)


def test_f_help_wins_even_next_to_owner(tmp_path):
    rig = Rig(tmp_path, "owner")
    r = rig.sb.run_env("--owner", "--help", engram_home=str(rig.home), extra=TINY)
    assert r.returncode == 0 and "Usage:" in r.stdout, (r.returncode, r.stdout, r.stderr)
    _no_side_effects(rig)


@pytest.mark.parametrize("args", [("--bogus",), ("--owner", "--bogus"), ("--bogus", "--owner"), ("bogus",), ("--owner=1",)])
@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_g_unknown_argument_exits_2_names_it_and_touches_nothing(tmp_path, args, os_name):
    rig = Rig(tmp_path, "plain", os_name=os_name)
    r = rig.sb.run_env(*args, engram_home=str(rig.home), extra=TINY)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    bad = [a for a in args if a != "--owner"][0]
    assert bad in r.stderr, r.stderr
    _no_side_effects(rig)


def test_g_known_arguments_still_run_a_real_install(tmp_path):
    """Control: --owner and --allow-path (the only two) are not 'unknown'."""
    rig = Rig(tmp_path, "owner")
    r = rig.sb.run_env("--owner", "--allow-path", engram_home=str(rig.home), extra=TINY)
    assert r.returncode == 0 and "Setup complete" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert rig.verbs().count("bootstrap") == 1
