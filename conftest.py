"""Repo-root pytest configuration: the service-manager guard (#3519), shared by
EVERY test root.

This file is the ONE definition of the `launchctl` / `systemctl` guard shims.
pytest loads a repo-root conftest.py for every test under the repo, so the three
roots CI runs -- tests/, src/forum/tests/ and tools/test_*.py (invoked from the
repo root as `python3 -m pytest <path>`) -- all get the same shim directory first
on PATH. (#3522 had put it in tests/conftest.py, which only reached tests/.)

Not shippable: it sits at the repo root, outside src/ and tools/, and is
accounted as repo_only in src/build/packaging/tiers.json. An installed forum
layout has no repo root and so no guard; nothing imports this file, so its
absence breaks nothing (src/forum/tests never import it).
"""

import os

import pytest

# ---------------------------------------------------------------------------
# #3519 -- suite-wide guard: no test may mutate the REAL service manager.
#
# Origin: before #3340, a setup-script test ran the real script with PATH =
# <stub dir>:<inherited PATH>; on a Mac the inherited PATH holds the real
# `launchctl`, so `launchctl bootout` / `launchctl bootstrap` ran for real and
# registered a launchd job under the production label
# `com.engram.surface-daemon` that respawned for three days. #3340 fixed that
# one cell; this block is the guard that does not depend on each test author
# remembering.
#
# Every test runs with a shim directory FIRST on PATH holding `launchctl` and
# `systemctl` shims. A shim passes an ALLOWLIST of read-only subcommands to the
# real tool (found at call time by searching PATH with the shim directory
# removed; exit 127 if there is none, as for any missing command) and REFUSES
# everything else (exit 99, touches nothing; fail closed, so an unknown or
# missing subcommand is refused). A test that stubs launchctl/systemctl itself
# prepends its stub dir to PATH AFTER this fixture ran, so its stub still wins.
# Shims use bash builtins only, so they work under a minimal PATH, on bash 3.2.
# `launchctl examine` is NOT allowlisted: it executes a tool against launchd.
#
# SIDE EFFECT: the shims make `launchctl` and `systemctl` exist on EVERY host's
# PATH, including a Mac, where `systemctl` was absent. `command -v systemctl`
# (start-engram-daemon.sh) and `shutil.which("systemctl")`
# (engram-session-start-hook.py) now find a command that exits 127 on read-only
# calls. A future "this host has no systemctl" test must account for that.
# ---------------------------------------------------------------------------

_SERVICE_SHIM_TEMPLATE = r"""#!/bin/bash
# conftest.py guard #3519 -- generated shim for __TOOL__; do not edit.
tool=__TOOL__
args="$*"
self_dir="${0%/*}"
case "$0" in */*) ;; *) self_dir=. ;; esac

refuse() {
    printf '%s\n' "[conftest.py guard #3519] refused $tool $args: tests must not mutate the real service manager; stub $tool first on PATH in the test (running test: ${PYTEST_CURRENT_TEST-unknown})" >&2
    exit 99
}

sub=""
__FIND_SUB__

# Belt and braces: refuse if ANY token is a known mutating verb, so a value-
# taking option the finder does not know ("--kill-whom show kill x") cannot hide
# one. Over-refusal is accepted: a read-only call with a mutating word as an
# argument (a unit literally named `start`) is refused too.
for tok in "$@"; do
    case "$tok" in
        __MUTATING__) refuse ;;
    esac
done

case "$sub" in
    __ALLOWED__) ;;
    *) refuse ;;
esac

set -f
oldifs=$IFS
IFS=:
for d in $PATH; do
    [ -n "$d" ] || d=.
    [ "$d" -ef "$self_dir" ] && continue
    if [ -f "$d/$tool" ] && [ -x "$d/$tool" ]; then
        IFS=$oldifs
        exec "$d/$tool" "$@"
    fi
done
IFS=$oldifs
exit 127
"""

_LAUNCHCTL_FIND_SUB = 'sub="${1-}"'

# systemctl: FAIL CLOSED on any option the finder cannot place. systemd's option
# lists drift between versions and real systemctl lets an unlisted value-taking
# option swallow the next word (`--root is-enabled foo.service` -> the verb is
# `foo.service`), so skipping an unknown option would take the wrong word as the
# subcommand. The rule is "refuse what you cannot parse". Every dash-token must
# be: an exact arity-0 flag (skipped); an exact value-taking option (consumes the
# next token); `--long=value` for a value-taking long option; or the bare
# `--version` as the subcommand. Anything else (combined short forms like `-ap`,
# unlisted long options, `--`) is refused. The subcommand is the first non-dash
# token and must be on the allowlist.
_SYSTEMCTL_FIND_SUB = r"""find_sub() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --version) sub=--version; return ;;
            --user|--system|--no-pager|--quiet|-q|--no-legend|--no-ask-password|--full|-l|--all|-a|--value|--failed|--plain)
                shift ;;
            -p|-n|-t|-M|-H|-o|-s|--property|--lines|--type|--machine|--host|--output|--signal)
                shift
                [ $# -gt 0 ] && shift ;;
            --property=*|--lines=*|--type=*|--machine=*|--host=*|--output=*|--signal=*)
                shift ;;
            -*) refuse ;;
            *) sub="$1"; return ;;
        esac
    done
}
find_sub "$@"
"""

_SERVICE_SHIMS = {
    "launchctl": (
        _LAUNCHCTL_FIND_SUB,
        "list|print|print-cache|print-disabled|blame|help|version|managerpid|"
        "manageruid|managername|hostinfo|error",
        "bootstrap|bootout|load|unload|submit|remove|enable|disable|kickstart|kill|"
        "start|stop|procinfo|dumpstate|reboot|config|limit|setenv|unsetenv|getenv|"
        "bslist|plist|uncache|attach|debug|runstats|asuser",
    ),
    "systemctl": (
        _SYSTEMCTL_FIND_SUB,
        "show|status|is-active|is-enabled|is-failed|list-units|list-unit-files|"
        "list-timers|list-sockets|list-jobs|cat|help|--version",
        "start|stop|restart|reload|try-restart|reload-or-restart|enable|disable|mask|"
        "unmask|kill|daemon-reload|daemon-reexec|edit|set-property|link|revert|isolate|"
        "reset-failed|preset|preset-all|poweroff|reboot|halt|suspend|hibernate|"
        "hybrid-sleep|set-environment|unset-environment|import-environment|switch-root|"
        "rescue|emergency|default|cancel|add-wants|add-requires|bind|mount-image|clean|"
        "freeze|thaw|service-log-level|service-log-target|kexec|exit|reenable|set-default|"
        "soft-reboot|suspend-then-hibernate|try-reload-or-restart|force-reload|enqueue-marked|"
        "try-reload|reload-or-try-restart|condrestart",
    ),
}


@pytest.fixture(scope="session")
def service_manager_shim_dir(tmp_path_factory):
    """The directory (outside the repo, created once per session) holding the
    `launchctl` / `systemctl` guard shims."""
    shim_dir = tmp_path_factory.mktemp("service_manager_guard_3519")
    for tool, (find_sub, allowed, mutating) in _SERVICE_SHIMS.items():
        body = (
            _SERVICE_SHIM_TEMPLATE.replace("__TOOL__", tool)
            .replace("__FIND_SUB__", find_sub)
            .replace("__ALLOWED__", allowed)
            .replace("__MUTATING__", mutating)
        )
        shim = shim_dir / tool
        shim.write_text(body)
        shim.chmod(0o755)
    return shim_dir


@pytest.fixture(autouse=True)
def _service_manager_guard(service_manager_shim_dir, monkeypatch):
    """#3519: put the shim dir first on PATH for every test (restored after)."""
    monkeypatch.setenv(
        "PATH", f"{service_manager_shim_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
    )


# ---------------------------------------------------------------------------
# #3784 -- the `timing` marker: a cell that asserts a measured ratio or latency
# bound judges the TRACER under coverage (the stage-accounting cell read ~0.118
# against its 10% bound, 0.072-0.076 without). The nightly deselects them from the
# coverage step (-m "not timing") and judges them in a no-coverage step (-m timing);
# this hook is the loud backstop: a timing cell that finds a tracer SKIPS with the
# named reason. It never passes and never silently returns there.
#
# One hook keyed on the marker, not a per-cell fixture: it runs in
# pytest_runtest_setup with tryfirst, i.e. BEFORE any fixture (a module-scoped
# fixture is set up before a function-scoped one whatever the argument order, so
# a guard fixture cannot skip ahead of an expensive module fixture), and a new
# `timing` cell is guarded automatically. This root conftest reaches both test roots.

TRACER_SKIP_REASON = "timing cell under an active tracer; judged in the no-coverage timing step"


def _probe_settrace():
    import sys

    return sys.gettrace() is not None


def _probe_monitoring():
    """The sys.monitoring coverage tool (coverage.py on 3.12+ with COVERAGE_CORE=sysmon)."""
    import sys

    if not hasattr(sys, "monitoring"):
        return False
    return sys.monitoring.get_tool(sys.monitoring.COVERAGE_ID) is not None


def _probe_coverage():
    """An active coverage.Coverage; coverage need not be installed."""
    try:
        import coverage
    except ImportError:
        return False
    return coverage.Coverage.current() is not None


_TRACER_PROBES = (_probe_settrace, _probe_monitoring, _probe_coverage)


def _tracer_active():
    """True when any tracer probe fires. A probe that raises counts as 'no tracer'."""
    for probe in _TRACER_PROBES:
        try:
            if probe():
                return True
        except Exception:
            continue
    return False


def _skip_under_tracer():
    if _tracer_active():
        pytest.skip(TRACER_SKIP_REASON)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if item.get_closest_marker("timing") is not None:
        _skip_under_tracer()
