"""Repo-root pytest configuration: the service-manager guard (#3519), shared by
EVERY test root.

This file is the ONE definition of the service-manager guard shims (`launchctl`,
`systemctl`, `systemd-run`, `loginctl`).
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
# The invariant is the CLASS -- "no test may mutate the real service manager" --
# not a list of tool names. A guard keyed on two basenames missed `systemd-run
# --user`, which from a test created REAL transient units (12 leaked over four
# suite runs, two looping). The shimmed tools are `launchctl`, `systemctl`,
# `systemd-run` and `loginctl`; SERVICE_MANAGER_TOOL_CANDIDATES below is the one
# place to add the next mutator, and a cell enforces it.
#
# Every test runs with a shim directory FIRST on PATH holding a shim for each of
# those four tools. A shim passes an ALLOWLIST of read-only subcommands to the
# real tool (found at call time by searching PATH with the shim directory
# removed; exit 127 if there is none, as for any missing command) and REFUSES
# everything else (exit 99, touches nothing; fail closed, so an unknown or
# missing subcommand is refused). A test that stubs one of these tools itself
# prepends its stub dir to PATH AFTER this fixture ran, so its stub still wins.
# Shims use bash builtins only, so they work under a minimal PATH, on bash 3.2.
# `launchctl examine` is NOT allowlisted: it executes a tool against launchd.
#
# SIDE EFFECT: the shims make `launchctl`, `systemctl`, `systemd-run` and
# `loginctl` exist on EVERY host's PATH, including a Mac, where `systemctl`,
# `systemd-run` and `loginctl` were absent. `command -v systemctl`
# (start-engram-daemon.sh) and `shutil.which("systemctl")`
# (engram-session-start-hook.py) now find a command that exits 127 on read-only
# calls, and the same holds for `systemd-run` and `loginctl`. A future "this host
# has no systemctl / systemd-run / loginctl" test must account for that.
#
# NOT COVERED (unmeasured routes; the guard is PATH-shim based): an absolute-path
# invocation of a real tool (`/usr/bin/systemctl ...`, which bypasses PATH); writing
# unit files into ~/.config/systemd/user/ (no tool is called); D-Bus calls from
# Python bindings (no tool is called).
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

# systemd-run: it has NO read-only verbs (every call other than help/version
# creates a transient unit or scope), so the finder accepts exactly ONE form: a
# single argument that is --version, --help or -h. Anything else (no args, `--user`
# alone, a help flag mixed with other tokens, `--versions`) leaves the subcommand
# empty and is refused.
_SYSTEMD_RUN_FIND_SUB = r"""if [ $# -eq 1 ]; then
    case "$1" in
        --version|--help|-h) sub=ok ;;
    esac
fi
"""

# loginctl: the same fail-closed discipline as _SYSTEMCTL_FIND_SUB. Every dash-token
# must be an exact arity-0 flag (skipped), an exact value-taking option (consumes
# the next token), `--long=value` for a value-taking long option, or the bare
# `--version` as the subcommand. Anything else is refused.
_LOGINCTL_FIND_SUB = r"""find_sub() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --version) sub=--version; return ;;
            --no-pager|--no-legend|--value|-a|--all|-l|--full|--no-ask-password|-q|--quiet)
                shift ;;
            -p|-H|-M|-s|-n|-o|--property|--host|--machine|--signal|--lines|--output|--kill-whom)
                shift
                [ $# -gt 0 ] && shift ;;
            --property=*|--host=*|--machine=*|--signal=*|--lines=*|--output=*|--kill-whom=*)
                shift ;;
            -*) refuse ;;
            *) sub="$1"; return ;;
        esac
    done
}
find_sub "$@"
"""

# THE place to add the next service-manager mutator. A cell
# (test_conftest_service_manager_guard_3519.py::test_k6_*) scans src/, tests/,
# tools/ and deploy/ for each name here (word-boundary match, code files only) and
# fails if one is INVOKED anywhere but has no entry in _SERVICE_SHIMS below. To
# resolve a failure: add a shim to _SERVICE_SHIMS -- or, if the tool truly cannot
# mutate the service manager, remove it from this tuple and record the one-line
# reason here. (None removed so far.)
# The class is "mutates the real service manager" (launchd/systemd units and the
# user manager's lifetime). Tools that change host settings or logs instead
# (hostnamectl, timedatectl, localectl, journalctl) are deliberately outside it.
SERVICE_MANAGER_TOOL_CANDIDATES = (
    "launchctl",
    "systemctl",
    "systemd-run",
    "loginctl",
    "busctl",
    "dbus-send",
    "gdbus",
    "systemd-mount",
    "machinectl",
)

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
    "systemd-run": (
        _SYSTEMD_RUN_FIND_SUB,
        "ok",
        # No token is a "mutating word": the finder already refuses every call
        # but the three help/version forms. A pattern that never matches a real
        # argument keeps the template's belt-and-braces `case` well-formed.
        "--never-a-real-systemd-run-token",
    ),
    "loginctl": (
        _LOGINCTL_FIND_SUB,
        "show-user|show-session|list-sessions|list-users|user-status|session-status|"
        "--version|help",
        "enable-linger|disable-linger|terminate-user|terminate-session|kill-user|"
        "kill-session|lock-session|unlock-session|lock-sessions|unlock-sessions|"
        "terminate-seat|activate|attach|flush-devices",
    ),
}


@pytest.fixture(scope="session")
def service_manager_shim_dir(tmp_path_factory):
    """The directory (outside the repo, created once per session) holding the
    guard shims (one per tool in _SERVICE_SHIMS)."""
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
# #4475 -- suite-wide guard: no test may connect to the LIVE forum.
#
# Origin: a whole-suite measurement run reached the real forum service on the host (tokenless
# requests, five of them `POST /api/agents/status`) through test cells whose stub configs carry
# the live URL. With the live port refused every one of those cells still passes (the code reads a
# refused connect as "unreachable"), so the guard needs no change to any cell.
#
# `socket.socket.connect` and `connect_ex` are wrapped from `pytest_configure` to `pytest_unconfigure`
# (a session-long install; `pytest_configure` is below), so the guard covers collection (module import
# time), `setup_module`, `setUpClass`, module/session/class-scoped fixtures and the test functions
# themselves. A function-scoped autouse fixture installs it again per test, a no-op while it is in
# place. A connect to a live forum endpoint raises a ConnectionRefusedError subclass naming this guard
# (`connect_ex` returns ECONNREFUSED); everything else passes to the real method (other loopback ports,
# unix sockets).
# The live endpoints are those the CODE resolves: `forum.url` of $ENGRAM_HOME/config.json,
# `$FORUM_URL`, and the default constant READ from the two source files that define it. A loopback
# endpoint refuses every loopback spelling (localhost, 127.0.0.0/8, ::1, the unspecified address, their
# IPv4-mapped forms and the inet_aton forms such as 127.1) on its port; a non-loopback endpoint refuses
# that host and port EXACTLY AS WRITTEN: stdlib clients resolve a name first and connect by IP, so a host
# NAME in forum.url is matched only by a direct connect by that name, an IP literal is matched either way.
#
# "My source files are not here": the guard FAILS CLOSED. The two source files are read relative to this file's
# own directory, so a COPY of this conftest must carry them (tests/test_service_manager_guard_roots_3519.py's
# throwaway repo copies the files named by DEFAULT_URL_SOURCES, never a retyped list). A source that is missing,
# not a regular file, unreadable, not UTF-8 or without the constant raises ONE failure,
# `RuntimeError("#4475 guard: <relative path>: <reason>")`, at import (collection fails); the endpoint functions
# never return an empty set for it, and a preset ENGRAM_LIVE_FORUM_ENDPOINTS_4475 cannot stand in for the
# missing sources. Cells: the "fold 2" section of tests/test_live_forum_guard_4475.py and its two copies.
#
# This definition is shared: src/forum/tests/conftest.py loads THIS file by path, assigns the same
# `pytest_configure` / `pytest_unconfigure` hooks into its own namespace and installs the same guard (the
# forum tests may run on their own), and the cell files load it by path too. Installing twice is a no-op.
# No opt-out: a test that needs the live forum is a finding.
#
# NOT COVERED (unmeasured routes): a C-level `_socket.socket` used directly, connects made by a
# subprocess (curl, a child python), and non-forum services (the world servers on 5003 / 5005).
# Also not covered, by construction of the matcher:
#   - the host's OWN non-loopback address on the forum port (the forum binds 0.0.0.0, but only the
#     loopback spellings are widened, so `connect(("<LAN ip>", 5002))` passes through);
#   - a raw `sock.connect((<this machine's hostname>, port))` by NAME (stdlib clients resolve first and
#     connect by IP, which is refused; a hand-made connect by hostname is not matched);
#   - an ENGRAM_LIVE_FORUM_ENDPOINTS_4475 already set in the environment may ADD endpoints (it is the
#     snapshot a second load reuses); it never removes the default pair, and a malformed value is
#     replaced by a fresh resolve.
#   - a test that monkeypatches `socket.socket.connect` / `connect_ex` ITSELF replaces the guard for that
#     test's duration (three existing cells do, in tests/test_engram_pkg_render.py,
#     tests/test_engram_signature.py and tests/test_engram_signature_codex.py; all install deny stubs,
#     stricter than the guard, so nothing is open today; a pass-through patch would be).
#
# REPORT: the session install keeps one list of its refusals (host, port, running test). At
# `pytest_sessionfinish` a non-empty list prints one line (count, first targets and tests) and an empty
# one prints nothing. Report only: it never fails the run. The guard cells use their own forced
# installs, so a clean suite reports nothing.
import errno
import ipaddress
import json
import re
import socket
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

_LFG_ATTR = "_live_forum_guard_4475"
_LFG_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_URL_SOURCES = (
    "src/engram/hooks/claude/engram-forum-prompt-hook.py",
    "src/engram/tools/_status_derive.py",
)
_DEFAULT_URL_RE = re.compile(r"""^_DEFAULT_FORUM_URL\b[^=\n]*=\s*["']([^"'\n]+)["']""", re.M)


class LiveForumConnectionRefused(ConnectionRefusedError):
    """Raised by the #4475 guard in place of a connect to the live forum."""


def _lfg_endpoint(url):
    """(host, port) of a forum URL, host lower-cased; None when it has none."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (host.lower(), port)


def _lfg_source_failure(rel, reason):
    """The ONE failure for an unusable default-URL source: `#4475 guard: <relative path>: <reason>`."""
    return RuntimeError(f"#4475 guard: {rel}: {reason}")


def _lfg_default_url(root, rel):
    """The `_DEFAULT_FORUM_URL` constant of the source file `root / rel`. Fails CLOSED: a file that is missing, not
    a regular file, unreadable or not UTF-8, or one without the constant, raises `RuntimeError` in the one shape
    above (never a bare OSError); the caller never gets an empty answer for a source it could not read."""
    path = root / rel
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _lfg_source_failure(rel, "file missing") from exc
    except PermissionError as exc:
        raise _lfg_source_failure(rel, "unreadable (permission denied)") from exc
    except OSError as exc:
        raise _lfg_source_failure(rel, f"unreadable ({exc.strerror or type(exc).__name__})") from exc
    if not stat.S_ISREG(st.st_mode):
        raise _lfg_source_failure(rel, "not a regular file")
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise _lfg_source_failure(rel, "file missing") from exc
    except PermissionError as exc:
        raise _lfg_source_failure(rel, "unreadable (permission denied)") from exc
    except OSError as exc:
        raise _lfg_source_failure(rel, f"unreadable ({exc.strerror or type(exc).__name__})") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _lfg_source_failure(rel, "not valid UTF-8") from exc
    m = _DEFAULT_URL_RE.search(text)
    if m is None:
        raise _lfg_source_failure(rel, "no _DEFAULT_FORUM_URL constant found")
    return m.group(1)


def live_forum_endpoints(environ, config_json_path, repo_root=None):
    """The set of (host, port) pairs the code may resolve as the forum: the config file's
    `forum.url` (when readable), `$FORUM_URL`, and the `_DEFAULT_FORUM_URL` constant read from the
    source files in DEFAULT_URL_SOURCES (never retyped). Pure: reads only what it is handed."""
    urls = []
    try:
        data = json.loads(Path(config_json_path).read_text(encoding="utf-8"))
        forum_cfg = data.get("forum") if isinstance(data, dict) else None
        if isinstance(forum_cfg, dict):
            urls.append(forum_cfg.get("url"))
    except (OSError, ValueError, TypeError):
        pass
    urls.append((environ or {}).get("FORUM_URL"))
    root = Path(repo_root) if repo_root is not None else _LFG_REPO_ROOT
    for rel in DEFAULT_URL_SOURCES:
        urls.append(_lfg_default_url(root, rel))
    return {ep for ep in map(_lfg_endpoint, urls) if ep is not None}


def _lfg_ip(h):
    """An ipaddress object for a host spelling, or None. Besides canonical literals this accepts the
    spellings `socket.connect` itself accepts through inet_aton (`127.1`, `0x7f.0.0.1`, `2130706433`)."""
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(h))
    except (OSError, ValueError, UnicodeError):
        return None


def _lfg_is_loopback(host):
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return False
    h = host.strip().lower().split("%", 1)[0].rstrip(".")
    if h in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    if h == "":
        return True  # connect(("", port)) means the unspecified address, which reaches this host
    ip = _lfg_ip(h)
    if ip is None:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or ip.is_unspecified or bool(mapped is not None and (mapped.is_loopback or mapped.is_unspecified))


def is_live_forum_target(addr, endpoints):
    """True when a socket address (a (host, port, ...) tuple; unix addresses never) is a live forum."""
    if not isinstance(addr, tuple) or len(addr) < 2:
        return False
    host, port = addr[0], addr[1]
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str) or not isinstance(port, int):
        return False
    if _lfg_is_loopback(host):
        return any(p == port for h, p in endpoints if _lfg_is_loopback(h))
    return (host.lower(), port) in endpoints


_LFG_SESSION_ENDPOINTS = None
_LFG_SNAPSHOT_KEY = "ENGRAM_LIVE_FORUM_ENDPOINTS_4475"


def resolve_endpoints_from_environ(environ):
    """The live endpoints for the environment `environ` describes: its ENGRAM_HOME (or ~/.engram)
    config.json, its FORUM_URL, and the default constant."""
    home = environ.get("ENGRAM_HOME") or os.path.join(os.path.expanduser("~"), ".engram")
    return live_forum_endpoints(environ, os.path.join(home, "config.json"))


def default_forum_endpoints(repo_root=None):
    """The default pair(s): the `_DEFAULT_FORUM_URL` constants READ from DEFAULT_URL_SOURCES. Always part of the
    endpoint set the guard uses, whatever the environment says."""
    root = Path(repo_root) if repo_root is not None else _LFG_REPO_ROOT
    return live_forum_endpoints({}, os.devnull, repo_root=root)


def _snapshot_from_raw(raw):
    """The endpoint set a preset snapshot value describes, or None when it is malformed (not JSON, not a list,
    or an item that is not a [host, port] pair)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    out = set()
    for item in data:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str)
                and isinstance(item[1], int) and not isinstance(item[1], bool)):
            return None
        out.add((item[0].lower(), item[1]))
    return out


def session_live_forum_endpoints():
    """The endpoints for the REAL environment, resolved ONCE, at the first import of this file.
    tests/conftest.py ALWAYS repoints ENGRAM_HOME at a throwaway directory (#4906; the repoint is
    unconditional, even when ENGRAM_HOME is exported), so a later resolution would miss the real config.
    This root conftest loads first and takes the snapshot before that repoint. The snapshot is also kept in the environment so a second
    load of this file (src/forum/tests/conftest.py loads it by path) reuses it.

    A preset value is trusted only to ADD endpoints: the default pair read from DEFAULT_URL_SOURCES is always
    in the set (`[]`, a preset without the default, or a malformed value cannot switch the guard off; a
    malformed value falls back to a fresh resolve). The merged set is written back to the environment."""
    global _LFG_SESSION_ENDPOINTS
    if _LFG_SESSION_ENDPOINTS is None:
        preset = _snapshot_from_raw(os.environ.get(_LFG_SNAPSHOT_KEY) or "")
        if preset is None:
            preset = resolve_endpoints_from_environ(os.environ)
        _LFG_SESSION_ENDPOINTS = set(preset) | default_forum_endpoints()
        os.environ[_LFG_SNAPSHOT_KEY] = json.dumps(sorted(_LFG_SESSION_ENDPOINTS))
    return _LFG_SESSION_ENDPOINTS


session_live_forum_endpoints()  # take the snapshot now, before tests/conftest.py touches ENGRAM_HOME


_LFG_SESSION_REFUSED = []  # the record the session-level install keeps (entries: (host, port, running test))


def install_live_forum_guard(monkeypatch, endpoints=None, force=False, refused=None):
    """Wrap socket.socket.connect / connect_ex (undone by `monkeypatch`). Idempotent: a second
    install while the wrappers are in place does nothing, unless `force` (the cells use it to wrap
    on top with a custom endpoint set)."""
    if not force and getattr(socket.socket.connect, _LFG_ATTR, None) is not None and getattr(
        socket.socket.connect_ex, _LFG_ATTR, None
    ) is not None:
        return
    eps = session_live_forum_endpoints() if endpoints is None else endpoints
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    if refused is None:
        refused = []  # a forced install (the cells) keeps its own record

    def _refusal(addr):
        refused.append((addr[0], addr[1], os.environ.get("PYTEST_CURRENT_TEST", "unknown")))
        return LiveForumConnectionRefused(
            errno.ECONNREFUSED,
            f"[conftest.py guard #4475] refused connect to {addr[0]}:{addr[1]}: tests must not "
            "reach the live forum (running test: "
            f"{os.environ.get('PYTEST_CURRENT_TEST', 'unknown')})",
        )

    def connect(self, address):
        if is_live_forum_target(address, eps):
            raise _refusal(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if is_live_forum_target(address, eps):
            _refusal(address)
            return errno.ECONNREFUSED
        return real_connect_ex(self, address)

    connect.__doc__, connect_ex.__doc__ = real_connect.__doc__, real_connect_ex.__doc__
    setattr(connect, _LFG_ATTR, refused)
    setattr(connect_ex, _LFG_ATTR, refused)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


_LFG_MP_ATTR = "_live_forum_guard_monkeypatches_4475"
_LFG_REFUSED_ATTR = "_live_forum_guard_refused_4475"
_LFG_REPORTED_ATTR = "_live_forum_guard_reported_4475"


def pytest_configure(config):
    """#4475: put the guard in place for the whole session, from here to `pytest_unconfigure`, so a connect
    made at module import (collection), in `setup_module`, `setUpClass` or a module/session-scoped fixture is
    refused too. The undo handle is KEPT on the config (a list: a second plugin registering these same hooks,
    src/forum/tests/conftest.py, appends its own, which holds nothing because installing twice is a no-op)."""
    mp = pytest.MonkeyPatch()
    if getattr(config, _LFG_REFUSED_ATTR, None) is None:
        setattr(config, _LFG_REFUSED_ATTR, _LFG_SESSION_REFUSED)
    install_live_forum_guard(mp, refused=getattr(config, _LFG_REFUSED_ATTR))
    if getattr(config, _LFG_MP_ATTR, None) is None:
        setattr(config, _LFG_MP_ATTR, [])
    getattr(config, _LFG_MP_ATTR).append(mp)


def pytest_sessionfinish(session, exitstatus):
    """#4475: REPORT (never fail) the refusals the session install recorded: one line, only when there are any."""
    config = session.config
    refused = getattr(config, _LFG_REFUSED_ATTR, None)
    if not refused or getattr(config, _LFG_REPORTED_ATTR, False):
        return
    setattr(config, _LFG_REPORTED_ATTR, True)  # the forum conftest registers this hook too: print once
    targets = list(dict.fromkeys(f"{h}:{p}" for h, p, _ in refused))
    tests = list(dict.fromkeys(str(t).split(" (")[0] for _, _, t in refused))
    line = (f"[live-forum guard #4475] refused {len(refused)} connect(s) to the live forum; "
            f"targets: {', '.join(targets[:5])}{' ...' if len(targets) > 5 else ''}; "
            f"running tests: {', '.join(tests[:5])}{' ...' if len(tests) > 5 else ''}")
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line)
    else:
        print(line, file=sys.stderr)


def pytest_unconfigure(config):
    """#4475: undo what `pytest_configure` installed (in reverse order)."""
    for mp in reversed(getattr(config, _LFG_MP_ATTR, None) or []):
        mp.undo()
    setattr(config, _LFG_MP_ATTR, [])


@pytest.fixture(autouse=True)
def _live_forum_guard(monkeypatch):
    """#4475: the guard again around each test (see the block comment above). A no-op while the session
    install from `pytest_configure` is in place; it brings the guard back if something restored the originals."""
    install_live_forum_guard(monkeypatch)


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
