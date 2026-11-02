#!/usr/bin/env python3
"""away_night_wake.py -- decide whether engram-sleep should arm a morning wake.

Host left on overnight (family away): the 08:15 autostart does not fire, and
``engram-sleep`` Step 3.5 stops every Monitor and removes ``loop-mode.json``,
so after sleep nothing wakes the seat.  If the host flag
``<shared-dir>/away-night.json`` says tonight is an away night, the skill arms a
one-shot ``CronCreate`` job at the next ``wake_at`` whose prompt is the seat's
own resolved boot charge plus one first line.

This tool only DECIDES; the skill calls CronCreate.  Read-only, stdlib only
(Python 3.9 safe).  Prints exactly ONE JSON object on stdout, always::

    {"status": "none"|"arm"|"error", "cron": str|null, "prompt": str|null,
     "fire_at": "YYYY-MM-DDTHH:MM"|null, "final_line": str}

Exit 0 for ``none`` and ``arm``; 2 for ``error`` (JSON still printed).

Flag file: ``{"night_of": "YYYY-MM-DD", "wake_at": "HH:MM", ...}``; extra keys
are ignored.  "Tonight" is the local date of ``now``, or the previous date when
local time is before 06:00 (after-midnight sleep).
``night_of`` is the EVENING date of the night the host stays up (Friday night =
Friday's date, even if the flag is written after midnight).

A VALID flag for another night (past or future) is still ``none``, but its
``final_line`` is the informational ``away-night flag is for <night_of>, tonight
is <tonight>: not armed`` (both YYYY-MM-DD).  ``final_line`` is the empty string
for ``none`` only when the flag is ABSENT (file missing, or shared dir missing or
not a directory).  A malformed flag is ``error``, before the night comparison.

Charge resolution mirrors ``_resolve_charge`` in
``deploy/autostart/agent-session-run.sh`` for platform ``claude``:
``<engram-home>/boot-charge-claude.md`` (if readable) wins; else legacy
``<engram-home>/boot-charge.md`` (if readable); else the shared template
``<charge-dir>/boot-charge-<profile>.md`` with the line
``{{HARNESS_BOOT_INSTRUCTIONS}}`` replaced by
``<charge-dir>/boot-charge-<profile>-claude.md``.  The charge text is returned
byte-for-byte (no strip, no added trailing newline).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

# The ONE ENGRAM_HOME resolution lives beside this file (#3863); resolve the package root through the symlink
# so `from tools import ...` works run as a script, loaded by path, or from the flattened plugin layout.
_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from tools import _owner_read as owner_read  # noqa: E402

PREFIX = "AWAY-NIGHT MORNING WAKE: the host was not rebooted; run this boot charge now.\n"
PLACEHOLDER = b"{{HARNESS_BOOT_INSTRUCTIONS}}"
NOT_ARMED = "AWAY-NIGHT WAKE NOT ARMED: "
DEFAULT_SHARED = "/home/agents-shared"
DEFAULT_CHARGE_DIR = "/usr/local/lib/agentctl/autostart"
FLAG_NAME = "away-night.json"
CUTOFF = dt.time(6, 0)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_NOW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


class WakeError(Exception):
    """A loud, reasoned refusal; becomes status=error."""


def _out(status, cron=None, prompt=None, fire_at=None, final_line=""):
    return {"status": status, "cron": cron, "prompt": prompt,
            "fire_at": fire_at, "final_line": final_line}


def _error(reason):
    return _out("error", final_line=NOT_ARMED + reason)


def _readable(path):
    """Shell ``-r``: exists and the effective user may read it."""
    return os.path.exists(path) and os.access(path, os.R_OK)


def _read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise WakeError("cannot read %s: %s" % (path, exc.strerror or exc))


def _parse_now(text):
    if not _NOW_RE.match(text):
        raise WakeError("--now must be YYYY-MM-DDTHH:MM, got %r" % text)
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise WakeError("--now is not a real date/time: %r" % text)


def _load_flag(shared_dir):
    """Return the validated (night_of: date, wake_at: time, set_by) or None."""
    path = os.path.join(shared_dir, FLAG_NAME)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise WakeError("flag %s cannot be read: %s" % (path, exc.strerror or exc))
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise WakeError("flag %s is not valid JSON" % path)
    if not isinstance(obj, dict):
        raise WakeError("flag %s is not a JSON object" % path)
    for key in ("night_of", "wake_at"):
        if key not in obj:
            raise WakeError("flag %s lacks key %s" % (path, key))
    night = obj["night_of"]
    if not isinstance(night, str) or not _DATE_RE.match(night):
        raise WakeError("flag night_of is not a YYYY-MM-DD date: %r" % (night,))
    try:
        night_d = dt.datetime.strptime(night, "%Y-%m-%d").date()
    except ValueError:
        raise WakeError("flag night_of is not a real date: %r" % (night,))
    wake = obj["wake_at"]
    if not isinstance(wake, str) or not _TIME_RE.match(wake):
        raise WakeError("flag wake_at is not an HH:MM time: %r" % (wake,))
    try:
        wake_t = dt.datetime.strptime(wake, "%H:%M").time()
    except ValueError:
        raise WakeError("flag wake_at is not a real 24h time: %r" % (wake,))
    set_by = obj.get("set_by")
    if not isinstance(set_by, str) or not set_by:
        set_by = "unknown"
    return night_d, wake_t, set_by


def _profile(engram_home):
    """Profile word per the runner: first line, whitespace stripped, else default."""
    pf = os.path.join(engram_home, "boot-charge-profile")
    if not _readable(pf):
        return "default"
    try:
        with open(pf, "rb") as fh:
            first = fh.read().split(b"\n", 1)[0]
    except OSError:
        return "default"
    word = b"".join(first.split()).decode("utf-8", "replace")
    return word if word in ("default", "quiet") else "default"


def _render_template(charge_dir, profile):
    base_p = os.path.join(charge_dir, "boot-charge-%s.md" % profile)
    comp_p = os.path.join(charge_dir, "boot-charge-%s-claude.md" % profile)
    for p in (base_p, comp_p):
        if not _readable(p) or not os.path.isfile(p) or os.path.getsize(p) == 0:
            raise WakeError("no readable boot-charge template %s" % p)
    base = _read_bytes(base_p)
    comp = _read_bytes(comp_p)
    lines = base.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # the file ended with a newline: no phantom last line
    out = []
    inserted = False
    for line in lines:
        if line == PLACEHOLDER:
            out.append(comp)  # cat: verbatim, no added newline
            inserted = True
        else:
            out.append(line + b"\n")  # printf '%s\n': always newline-terminated
    if not inserted:
        raise WakeError("boot-charge template %s is missing the harness instruction placeholder" % base_p)
    return b"".join(out)


def _resolve_charge(engram_home, charge_dir):
    for name in ("boot-charge-claude.md", "boot-charge.md"):
        path = os.path.join(engram_home, name)
        if _readable(path):
            data = _read_bytes(path)
            if not data:
                raise WakeError("boot charge %s is unreadable or empty" % path)
            return data
    return _render_template(charge_dir, _profile(engram_home))


def decide(shared_dir, engram_home, charge_dir, platform, now):
    flag = _load_flag(shared_dir)
    if flag is None:
        return _out("none")
    night_d, wake_t, set_by = flag
    tonight = now.date() if now.time() >= CUTOFF else now.date() - dt.timedelta(days=1)
    if night_d != tonight:
        return _out("none", final_line="away-night flag is for %s, tonight is %s: not armed"
                    % (night_d.isoformat(), tonight.isoformat()))
    if platform != "claude":
        raise WakeError("platform %s is not covered by this wake path" % platform)
    if now.time() < CUTOFF:
        fire = dt.datetime.combine(now.date(), wake_t)
        if fire <= now:
            raise WakeError("wake_at already passed (%s is not after %s)"
                            % (wake_t.strftime("%H:%M"), now.strftime("%H:%M")))
    else:
        fire = dt.datetime.combine(now.date() + dt.timedelta(days=1), wake_t)
    charge = _resolve_charge(engram_home, charge_dir)
    cron = "%d %d %d %d *" % (fire.minute, fire.hour, fire.day, fire.month)
    fire_at = fire.strftime("%Y-%m-%dT%H:%M")
    return _out(
        "arm", cron=cron, prompt=PREFIX + charge.decode("utf-8"), fire_at=fire_at,
        final_line="Away-night wake: one-shot at %s local (set_by %s)." % (fire_at, set_by))


def main(argv=None):
    try:
        ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
        ap.add_argument("--shared-dir", default=DEFAULT_SHARED)
        ap.add_argument("--engram-home", default=None)
        ap.add_argument("--charge-dir", default=DEFAULT_CHARGE_DIR)
        ap.add_argument("--platform", default="claude")
        ap.add_argument("--now", default=None)
        try:
            args = ap.parse_args(argv)
        except SystemExit as exc:
            if exc.code in (0, None):
                raise
            result = _error("bad arguments")
            print(json.dumps(result))
            return 2
        try:
            home = args.engram_home or str(owner_read.engram_home())
        except ValueError as exc:
            raise WakeError(str(exc))
        now = _parse_now(args.now) if args.now else dt.datetime.now().replace(second=0, microsecond=0)
        result = decide(args.shared_dir, home, args.charge_dir, args.platform, now)
    except WakeError as exc:
        result = _error(str(exc))
    except Exception as exc:  # noqa: BLE001 -- never a traceback
        result = _error("unexpected %s: %s" % (type(exc).__name__, exc))
    print(json.dumps(result))
    return 2 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
