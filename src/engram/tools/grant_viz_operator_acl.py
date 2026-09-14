#!/usr/bin/env python3
"""tools/grant_viz_operator_acl.py — engram-alpha#2720

One-shot, idempotent, fleet-wide repair: grant the cross-user operator viz
account a readable POSIX ACL on every EXISTING seat's
``~/.engram/health-snapshots/`` directory. Productionizes the issue's own
one-liner (below, using ``spt`` only as an illustrative example — this tool
carries NO default operator account; see "No default operator" further
down):

    for h in /home/agent-*; do
      d="$h/.engram/health-snapshots"
      [ -d "$d" ] && sudo setfacl -R -m u:spt:rX,m::rX,d:u:spt:rX,d:m::rX "$d"
    done

Why this is a SEPARATE tool from the SessionStart hook's
``_apply_pending_viz_acl`` (``hooks/claude/engram-session-start-hook.py``):
that hook only fires for a NEWLY-FINALIZED agent (it consumes a deferred
marker written by agentctl's finalize-name at rename time) and grants on
the WHOLE ``~/.engram`` tree. This tool targets EXISTING seats that the
deferred-marker path was never going to reach: seats whose ``~/.engram``
predates the marker mechanism entirely, or whose ACL was set up before
``health-snapshots/`` existed, or set up without ``-d`` (no default/
inherited ACL for future files). No marker was ever written for those
seats, so nothing will ever consume one for them — this tool is the
retroactive repair. Scoped to ``health-snapshots/`` specifically (narrower
than the hook's whole-``~/.engram`` grant), matching the issue's own
one-liner and Lei's "read-only for spt on snapshots, rw only on the config
file" framing — this tool does not touch ``config.json`` at all; that grant
belongs to the (already-existing, in-repo) deferred-marker consumer, which
handles config.json's own read-write grant separately.

Mask fix (engram-alpha#2720 item 2): every ``-m`` entry list here carries
an explicit ``m::rX`` (and ``d:m::rX``) mask alongside the named grant, not
just the named entry alone — see ``engram-session-start-hook.py``'s
``_apply_pending_viz_acl`` for the measured caveat: GNU setfacl already
auto-recomputes an unclipped mask on ``-m`` by default, so this is
belt-and-suspenders explicitness rather than a fix for an observed
clipped-mask bug in the bare form. What genuinely clips a NEW file's
effective permissions is the writing process's own umask at file-creation
time (fixed at the writer in ``health/engine.py``'s ``write_snapshot``, not
here or in the SessionStart hook).

Discovery: globs ``/home/agent-*`` (this repo's seat-home convention, used
throughout its docs and the issue's own one-liner) for a ``.engram/
health-snapshots`` subdirectory. No other fleet-wide seat-discovery helper
exists in this repo to reuse for this purpose (searched ``tools/*.py`` and
``src/engram/tools/*.py`` for a comparable glob; the only other ``agent-*``
reference, ``worktree-gc.py``, discovers git WORKTREES, an unrelated
concept, not seat home directories). ``--root-glob`` lets a caller — and
every test in this suite — point discovery at a throwaway directory tree
instead of the real filesystem; NEVER point this tool at real ``/home/
agent-*`` directories from a test.

Safety posture (this repo's destructive-adjacent convention — see
``worktree-gc.py``): DRY-RUN BY DEFAULT. Nothing is changed unless
``--apply`` is passed. Privilege is checked explicitly, per seat, with a
loud and specific error naming exactly what's missing — never a silent
no-op on a permission failure partway through a fleet-wide run.

Testable-logic split (this repo's general preference — see ``health/
engine.py``'s ``build_snapshot`` vs ``write_snapshot``, or
``reconcile_lesson_demotions.py``'s ``find_candidates`` vs ``main``):
``plan_for_seat`` is a PURE function (only ``os.path`` existence checks,
no subprocess calls, no filesystem writes) that computes what WOULD be
done; ``apply_seat_plan`` is the side-effecting half that actually shells
out to the real ``setfacl`` binary. The "what would I change" logic is
therefore fully unit-testable without invoking ``setfacl`` at all.

No default operator (engram-alpha#2720, Lei's ruling): a hardcoded ``spt``
default here was a functional leak of one host's personal setting into
every other install. There is no built-in operator account; ``--viz-operator``
resolves in this order and refuses loudly (non-zero exit, before touching
anything) if none of the three is set:

    1. ``--viz-operator NAME`` on the command line.
    2. The ``VIZ_OPERATOR`` environment variable — the SAME setting
       ``tools/agentctl`` consumes for its own ``spawn`` / ``finalize-name``
       preflight, so one export covers both tools.
    3. A ``VIZ_OPERATOR=NAME`` line in ``agentctl``'s own shared config file
       (``AGENTCTL_CONFIG_PATH`` below — the canonical persistent location;
       ``tools/agentctl`` sources the same file at startup).

Usage:
    python3 tools/grant_viz_operator_acl.py                  # dry-run (default)
    python3 tools/grant_viz_operator_acl.py --apply           # apply (needs sudo, usually)
    python3 tools/grant_viz_operator_acl.py --viz-operator X  # explicit operator account
    python3 tools/grant_viz_operator_acl.py --root-glob '/tmp/fake-seats/agent-*'  # testing
    python3 tools/grant_viz_operator_acl.py --json             # machine-readable report

Output: a per-seat report to stdout (human-readable by default, ``--json``
for machine-readable), and a non-zero exit code if any targeted seat
failed to apply (skipped seats — no health-snapshots dir — are NOT
failures), or if VIZ_OPERATOR could not be resolved at all.
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field

DEFAULT_SEAT_GLOB = "/home/agent-*"

# The canonical persistent location for VIZ_OPERATOR (engram-alpha#2720):
# tools/agentctl (bash) parses this same file at startup as
# $SHARED_ROOT/agentctl.conf (a restricted, non-executing KEY=VALUE read --
# NOT sourced, per engram-alpha#2734: $SHARED_ROOT is group-writable by
# every agent account, so `source`ing it was an unauthenticated
# root-code-execution vector); a plain `VIZ_OPERATOR=<name>` line here is
# consumed by both tools, so one persistent setting covers both. No default
# operator account is baked into either tool — see resolve_viz_operator().
AGENTCTL_CONFIG_PATH = "/home/agents-shared/agentctl.conf"


def _read_viz_operator_from_config(config_path: str | None = None) -> str | None:
    """Read a ``VIZ_OPERATOR=<value>`` line from agentctl's shared KEY=VALUE
    config file. Returns None if the file is absent, unreadable, or has no
    VIZ_OPERATOR entry. Tolerates blank lines, ``#``-prefixed comments, and
    single/double-quoted values. Does NOT evaluate the file as shell (no
    variable expansion, no command substitution) — unlike agentctl's own
    ``source``, this is a plain KEY=VALUE reader only.

    ``config_path`` defaults to the module-level ``AGENTCTL_CONFIG_PATH`` —
    looked up at call time (not bound as a default-argument value) so tests
    can monkeypatch the module attribute and have it take effect."""
    if config_path is None:
        config_path = AGENTCTL_CONFIG_PATH
    try:
        with open(config_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "VIZ_OPERATOR":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


# _viz_operator_conf_provenance_ok: PURE ownership/permission check over an
# EXPLICIT (uid, mode) pair -- not over any real file. Returns True iff
# uid == 0 (root-owned) AND mode grants no group or other write bit.
#
# engram-alpha#2720/PR #2734, colleague-review residual gap: parsing (not
# sourcing) agentctl.conf closed the unauthenticated-code-execution vector,
# but VIZ_OPERATOR's VALUE was still trusted as AUTHORIZATION (it becomes
# the setfacl ACL grantee in plan_for_seat) with no check on who controls
# the file it came from. $SHARED_ROOT (agentctl.conf's directory) is
# group-writable by every agent account, so any agent could still plant
# this file and choose who gets ACL read access to every seat's ENGRAM
# data.
#
# Deliberately does NOT compare against ``os.getuid()`` (the CALLER's own
# uid): this tool is typically run as root (it grants ACLs on other users'
# home directories), so a same-uid-as-caller test would be trivially
# satisfied by an attacker-planted file too -- root can read (and this
# function would then trust) anything. Comparing against uid 0 explicitly
# is the fix. Mirrors the OpenSSH StrictModes pattern (refuse a config/key
# file that isn't owned by the expected principal and isn't group/other-
# writable), and the sibling check in ``tools/agentctl``'s
# ``_viz_operator_conf_provenance_ok`` (same contract, same two consumers
# of ``AGENTCTL_CONFIG_PATH``).
#
# Usage: _viz_operator_conf_provenance_ok(uid, mode)  (mode: full st_mode,
# or just the permission bits -- only the low bits are inspected)
def _viz_operator_conf_provenance_ok(uid: int, mode: int) -> bool:
    return uid == 0 and not (mode & (stat.S_IWGRP | stat.S_IWOTH))


def _config_provenance_ok(config_path: str) -> bool:
    """Stats ``config_path`` and checks its actual owning uid + permission
    bits via ``_viz_operator_conf_provenance_ok``. Returns False if the path
    cannot be stat'd (missing, unreadable, or any other OSError) as well as
    if the file exists but fails the ownership/permission check -- callers
    that need to distinguish those two cases (see
    ``_read_viz_operator_from_config_with_provenance_check`` below) check
    existence separately before calling this."""
    try:
        st = os.stat(config_path)
    except OSError:
        return False
    return _viz_operator_conf_provenance_ok(st.st_uid, st.st_mode)


def _read_viz_operator_from_config_with_provenance_check(
    config_path: str | None = None,
) -> str | None:
    """The real entry point for the config-file resolution step: checks the
    file's provenance BEFORE trusting its contents, then delegates to
    ``_read_viz_operator_from_config`` for the actual value extraction.

    Returns None (the same as "unset") both when the file doesn't exist and
    when it exists but fails provenance -- but the two cases are NOT
    silently identical: a provenance failure prints a specific WARNING to
    stderr naming exactly why the file was rejected, distinguishing it from
    the plain "doesn't exist" case (which prints nothing here; the generic
    "VIZ_OPERATOR is not set" refusal in ``main()`` covers that case
    downstream). An operator debugging "why isn't my config being read"
    needs a different answer than "you never set it."

    ``config_path`` defaults to the module-level ``AGENTCTL_CONFIG_PATH`` —
    looked up at call time (not bound as a default-argument value) so tests
    can monkeypatch the module attribute and have it take effect, mirroring
    ``_read_viz_operator_from_config``'s own convention."""
    if config_path is None:
        config_path = AGENTCTL_CONFIG_PATH
    try:
        st = os.stat(config_path)
    except OSError:
        return None
    if not _viz_operator_conf_provenance_ok(st.st_uid, st.st_mode):
        print(
            f"WARNING: {config_path} exists but failed its ownership/permission check.",
            file=sys.stderr,
        )
        print(
            "         It must be owned by root (uid 0) and not writable by group or",
            file=sys.stderr,
        )
        print(
            "         other -- otherwise any agent account on this shared-writable",
            file=sys.stderr,
        )
        print(
            "         host could plant it and choose who this tool grants ACL read",
            file=sys.stderr,
        )
        print(
            "         access to on every seat's ENGRAM data (VIZ_OPERATOR's value is",
            file=sys.stderr,
        )
        print(
            "         trusted as AUTHORIZATION, not just as text).",
            file=sys.stderr,
        )
        print(
            "         Refusing to trust its contents; treating VIZ_OPERATOR as if",
            file=sys.stderr,
        )
        print("         the file were absent.", file=sys.stderr)
        print(
            f"         Fix: sudo chown root:root {config_path} && sudo chmod go-w {config_path}",
            file=sys.stderr,
        )
        return None
    return _read_viz_operator_from_config(config_path)


def resolve_viz_operator(cli_value: str | None) -> str | None:
    """Resolution order: ``--viz-operator`` CLI flag > ``VIZ_OPERATOR`` env
    var > agentctl.conf's ``VIZ_OPERATOR`` entry (gated by a provenance
    check -- see ``_read_viz_operator_from_config_with_provenance_check``)
    > None (caller must refuse loudly — no default operator account, per
    engram-alpha#2720)."""
    if cli_value:
        return cli_value
    env_value = os.environ.get("VIZ_OPERATOR", "").strip()
    if env_value:
        return env_value
    return _read_viz_operator_from_config_with_provenance_check()


# Injection-safe charset gate for VIZ_OPERATOR before it is embedded in a
# setfacl ACL-entry string (e.g. ``u:{viz_operator}:rX`` in plan_for_seat).
# setfacl parses its entry-list argument as COMMA-separated ACL entries, so
# an unvalidated value containing a comma could inject an additional,
# unintended ACL entry -- e.g. ``spt,u:evil:rwx`` would grant "evil" rwx
# too, not merely fail to match "spt" (engram-alpha#2734 suggestion 2).
# Identical regex to hooks/claude/engram-session-start-hook.py's
# ``_apply_pending_viz_acl`` charset check and tools/agentctl's
# ``_viz_operator_charset_ok`` -- kept in sync by hand across all three
# consumers of the same VIZ_OPERATOR value so they agree on what's legal.
_VIZ_OPERATOR_CHARSET_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")


def _viz_operator_charset_ok(value: str) -> bool:
    return bool(_VIZ_OPERATOR_CHARSET_RE.match(value))


@dataclass
class SeatPlan:
    """Pure description of what WOULD be done for one seat — no side
    effects flow from constructing one. ``skip_reason`` is non-None when
    this seat should be left alone entirely (no health-snapshots dir);
    ``commands`` is the ordered list of setfacl argv lists this plan would
    run, always empty when ``skip_reason`` is set."""

    seat_home: str
    snapshots_dir: str
    skip_reason: str | None = None
    commands: list[list[str]] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.skip_reason is None


def discover_seats(root_glob: str = DEFAULT_SEAT_GLOB) -> list[str]:
    """Every seat home dir matching ``root_glob`` that is a real directory,
    sorted for deterministic output. A seat missing ENGRAM entirely is
    still discovered here (its ``.engram`` may not exist at all) — the
    "nothing to do here" decision is ``plan_for_seat``'s job, not this
    function's, so a fleet-wide report can show every seat considered,
    not just the ones with something to fix."""
    return sorted(p for p in _glob.glob(root_glob) if os.path.isdir(p))


def plan_for_seat(seat_home: str, viz_operator: str) -> SeatPlan:
    """PURE: only ``os.path.isdir`` (a read-only existence check), no
    subprocess calls, no writes. Mirrors the issue's own one-liner exactly:
    a single recursive ``setfacl -R -m`` call carrying the named access
    entry, an explicit access mask, the named DEFAULT entry (inheritance
    for files created later), and an explicit default mask — see the
    module docstring's "Mask fix" section for what this buys and doesn't."""
    snapshots_dir = os.path.join(seat_home, ".engram", "health-snapshots")
    if not os.path.isdir(snapshots_dir):
        return SeatPlan(
            seat_home=seat_home,
            snapshots_dir=snapshots_dir,
            skip_reason=(
                "no .engram/health-snapshots directory (ENGRAM not "
                "installed here, or no health checkpoint has run yet)"
            ),
        )
    entries = f"u:{viz_operator}:rX,m::rX,d:u:{viz_operator}:rX,d:m::rX"
    return SeatPlan(
        seat_home=seat_home,
        snapshots_dir=snapshots_dir,
        commands=[["setfacl", "-R", "-m", entries, snapshots_dir]],
    )


class GrantError(RuntimeError):
    """A seat's plan could not even be ATTEMPTED — as opposed to attempted
    and rejected by the OS. Raised only for the "not privileged enough"
    precondition, per the issue's explicit ask to fail loudly rather than
    silently no-op when privilege is insufficient."""


def _can_write_acl(seat_home: str) -> bool:
    """ADVISORY pre-check for a clear, specific error message instead of a
    bare setfacl stderr blob — the real enforcement is always the OS's own
    permission check when setfacl actually runs. True iff the caller is
    root (can setfacl any seat), or the caller already owns the seat
    directory (the self-repair case: an agent granting the ACL on its own
    ``~/.engram`` needs no sudo, matching ``engram-session-start-hook.py``'s
    own comment: "the agent owns ~/.engram, so no sudo required")."""
    try:
        if os.geteuid() == 0:
            return True
        return os.stat(seat_home).st_uid == os.getuid()
    except OSError:
        return False


def apply_seat_plan(plan: SeatPlan, dry_run: bool = True) -> dict:
    """Side-effecting: shells out to the real ``setfacl`` binary unless
    ``dry_run``. Returns a stable-shaped per-seat result dict for every
    case except one: raises ``GrantError`` when ``--apply`` was requested
    but the caller isn't privileged enough for this specific seat (the
    issue's explicit ask — a silent no-op here would be worse than a loud
    failure). An ordinary setfacl failure (tool present, ran, but rejected
    for some OTHER reason) is reported in the result dict, not raised."""
    result: dict = {
        "seat_home": plan.seat_home,
        "snapshots_dir": plan.snapshots_dir,
        "skip_reason": plan.skip_reason,
        "commands": [" ".join(c) for c in plan.commands],
        "dry_run": dry_run,
        "applied": False,
        "error": None,
    }
    if plan.skip_reason is not None or dry_run:
        return result

    if not _can_write_acl(plan.seat_home):
        raise GrantError(
            f"insufficient privilege to grant ACLs on {plan.snapshots_dir} "
            f"(not root, and not the owner of {plan.seat_home}). Re-run "
            f"this tool with sudo, e.g.: sudo python3 {os.path.basename(sys.argv[0])} --apply"
        )

    for cmd in plan.commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except OSError as exc:
            result["error"] = f"{cmd[0]} could not be run: {exc}"
            return result
        if proc.returncode != 0:
            result["error"] = (
                f"{' '.join(cmd)} exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
            return result

    result["applied"] = True
    return result


def run(
    root_glob: str = DEFAULT_SEAT_GLOB,
    *,
    viz_operator: str,
    dry_run: bool = True,
) -> list[dict]:
    """Discover + plan + (optionally) apply, for every matching seat.
    Re-raises GrantError from apply_seat_plan — main() is responsible for
    turning that into a loud, non-zero-exit failure rather than letting it
    silently truncate the report."""
    seats = discover_seats(root_glob)
    results = []
    for seat_home in seats:
        plan = plan_for_seat(seat_home, viz_operator=viz_operator)
        results.append(apply_seat_plan(plan, dry_run=dry_run))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grant_viz_operator_acl",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually grant the ACLs (default: dry-run, nothing is changed). "
             "Almost always needs to be run with sudo — this tool grants ACLs "
             "on OTHER users' home directories, which requires either root or "
             "ownership of each target directory.",
    )
    parser.add_argument(
        "--viz-operator", default=None,
        help=(
            "Account to grant read access to. No default (engram-alpha#2720) — "
            "resolves from this flag, else the VIZ_OPERATOR environment "
            f"variable, else a VIZ_OPERATOR=<name> line in {AGENTCTL_CONFIG_PATH}; "
            "refuses loudly if none is set."
        ),
    )
    parser.add_argument(
        "--root-glob", default=DEFAULT_SEAT_GLOB,
        help=f"Glob pattern for seat home directories (default: {DEFAULT_SEAT_GLOB!r}). "
             "Override for testing against a throwaway directory tree — never point "
             "this at anything other than the real fleet in production use.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable report on stdout instead of the human-readable form.",
    )
    args = parser.parse_args(argv)

    # Preflight, not mid-way (engram-alpha#2720): refuse loudly, before
    # touching anything (including the setfacl-availability check below),
    # when no VIZ_OPERATOR can be resolved from any of the three sources.
    viz_operator = resolve_viz_operator(args.viz_operator)
    if not viz_operator:
        print("ERROR: VIZ_OPERATOR is not set.", file=sys.stderr)
        print(
            "       VIZ_OPERATOR names the human operator account that runs the",
            file=sys.stderr,
        )
        print(
            "       viz dashboard and needs read-only access to every seat's",
            file=sys.stderr,
        )
        print(
            "       ENGRAM health-snapshots data. This tool will not proceed",
            file=sys.stderr,
        )
        print("       without it — there is no default.", file=sys.stderr)
        print("", file=sys.stderr)
        print("       Set it persistently for this host:", file=sys.stderr)
        print(
            f"         echo 'VIZ_OPERATOR=<operator-username>' | sudo tee -a {AGENTCTL_CONFIG_PATH}",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print("       Or set it for a single invocation:", file=sys.stderr)
        print(
            "         VIZ_OPERATOR=<operator-username> python3 "
            f"{os.path.basename(sys.argv[0])} ... "
            "(or pass --viz-operator <operator-username>)",
            file=sys.stderr,
        )
        return 2

    # Charset gate, also preflight (engram-alpha#2734 suggestion 2): refuse
    # loudly, before touching anything, when the resolved VIZ_OPERATOR would
    # be unsafe to embed in a setfacl ACL-entry string (see
    # _viz_operator_charset_ok above for why a comma is dangerous here).
    if not _viz_operator_charset_ok(viz_operator):
        print(
            f"ERROR: VIZ_OPERATOR '{viz_operator}' has an invalid charset.",
            file=sys.stderr,
        )
        print(
            "       Must be lowercase letters, digits, underscore, or hyphen,",
            file=sys.stderr,
        )
        print(
            "       starting with a letter or underscore (POSIX username shape).",
            file=sys.stderr,
        )
        print(
            "       Refusing to embed it in a setfacl ACL-entry string, where a",
            file=sys.stderr,
        )
        print(
            "       comma could inject an additional, unintended ACL grant.",
            file=sys.stderr,
        )
        return 2

    if not shutil.which("setfacl"):
        print(
            "ERROR: setfacl not found on PATH. This tool requires POSIX ACL "
            "tooling (the 'acl' package on Debian/Ubuntu: apt install acl).",
            file=sys.stderr,
        )
        return 1

    try:
        results = run(
            root_glob=args.root_glob,
            viz_operator=viz_operator,
            dry_run=not args.apply,
        )
    except GrantError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump({"results": results}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for r in results:
            if r["skip_reason"]:
                print(f"SKIP  {r['seat_home']}: {r['skip_reason']}")
                continue
            verb = "WOULD RUN" if r["dry_run"] else ("OK" if r["applied"] else "FAILED")
            print(f"{verb:9s} {r['snapshots_dir']}")
            for cmd in r["commands"]:
                print(f"          {cmd}")
            if r["error"]:
                print(f"          ERROR: {r['error']}")
        if not args.apply:
            print(
                "\nDRY-RUN — nothing changed. Re-run with --apply (and "
                "likely sudo) to actually grant the ACLs."
            )

    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
