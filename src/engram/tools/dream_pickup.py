#!/usr/bin/env python3
"""dream_pickup -- record, on the latest dream record, what the awake seat did with each head
item (engram-alpha#3919, item M6: the write half of the dream-to-awake pickup).

    python3 tools/dream_pickup.py (--pick N TEXT | --skip N REASON) [--record PATH] [--home PATH]

Target: PATH, else the last date-prefixed ``YYYY-MM-DD*.md`` in ``$ENGRAM_HOME/history/dream/`` sorted by name without
``.md`` (so ``2026-10-02-turn110.md`` is later than ``2026-10-02.md``)
(``--home`` overrides ``ENGRAM_HOME``; default ``~/.engram``).  N must be an item number under
``## For the awake seat`` (heading matched by prefix, fenced blocks ignored).  The tool writes
the single line ``pN. picked up <TEXT>`` or ``pN. skipped (<REASON>)`` under a final
``## Morning pickup`` section (created at the end of the file when absent; an existing one is
edited in place: the LAST one after the awake-seat block, a copy quoted before that block is never
the target), lines sorted by N, one line per N (a re-run replaces).  TEXT/REASON are collapsed to
one line, C0 control characters become spaces, capped at 300 characters.

It refuses (exit 2, one stderr line, nothing written) on a bad N, empty TEXT/REASON, a missing
or non-file target, or a record with no ``## For the awake seat`` section.  The write is the
whole file, atomic (temp file in the same directory, original mode kept, ``os.replace``), under an
exclusive ``flock`` on a hidden ``.dream_pickup.lock`` beside the record (never removed; it is not a
``*.md`` file; the wait has a 10 s limit, then exit 1 "lock busy").  It
does NOT commit to git: the sleep skill's commit step owns that.  No network, no DB, no node writes.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

# The ENGRAM home has ONE definition: tools/_owner_read.engram_home() (#3863). `tools` is found one directory above
# this file in BOTH layouts (src/engram/ and the deployed plugin root). Appended, not inserted: an in-process load
# (a test) must not leave src/engram at sys.path[0] (#4029).
_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.append(_PKG_ROOT)
from tools import _owner_read as owner_read  # noqa: E402

_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
_NUMBERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_PICKUP_LINE_RE = re.compile(r"^p(\d+)\.\s")
_CAP = 300


_DATED_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LOCK_WAIT_S = 10.0  # a stuck holder must not hang the awake seat forever


class Refusal(Exception):
    pass


def _fold(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)  # C0 controls (ESC, BEL ...) never reach the record
    return re.sub(r"\s+", " ", text).strip()[:_CAP].rstrip()


def _scan(lines: list):
    """(item numbers under the first awake-seat heading, index of the last Morning pickup
    heading or None, index where that section ends).  The LAST heading wins.  Fenced blocks are quoted text."""
    in_fence = False
    section = None  # 'awake' | 'pickup' | None
    numbers: list = []
    seen_awake = False
    pickup_at = None
    pickup_end = None
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m or line.startswith("#"):
            if section == "pickup" and pickup_end is None:
                pickup_end = i
            section = None
            head = m.group(1).strip().lower() if m else ""
            if head.startswith("for the awake seat") and not seen_awake:
                seen_awake = True
                section = "awake"
            elif head.startswith("morning pickup") and seen_awake:
                # only AFTER the awake-seat block (a copy quoted near the top of the record, before
                # it, is never the target) and the LAST such heading wins
                pickup_at = i
                pickup_end = None
                section = "pickup"
            continue
        if section == "awake":
            n = _NUMBERED_RE.match(line)
            if n:
                numbers.append(int(n.group(1)))
    if pickup_at is not None and pickup_end is None:
        pickup_end = len(lines)
    return seen_awake, numbers, pickup_at, pickup_end


def read_pickup_dispositions(text: str) -> dict:
    """Return {N: ("picked_up", line) | ("skipped", reason)} for every pN. line
    in the LAST ``## Morning pickup`` section after ``## For the awake seat``.
    Returns {} when no such section exists or no pN. lines are found.
    Lines inside fenced blocks are quoted text and are not pickup entries.
    Only fully-formed lines are recognised: ``picked up`` requires non-empty
    text after the verb; ``skipped`` requires a non-empty reason.  Unrecognised
    lines are silently ignored (never hide an item)."""
    lines = text.splitlines(keepends=True)
    _, _numbers, pickup_at, pickup_end = _scan(lines)
    if pickup_at is None:
        return {}
    result = {}
    in_fence = False
    for raw in lines[pickup_at + 1 : pickup_end]:
        line = raw.rstrip("\r\n")
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _PICKUP_LINE_RE.match(raw)
        if not m:
            continue
        n = int(m.group(1))
        rest = raw[m.end():].strip()
        if rest.lower().startswith("picked up"):
            after = rest[len("picked up"):].strip()
            if not after:  # nothing recorded after the verb — not a valid line
                continue
            result[n] = ("picked_up", rest)
        elif rest.lower().startswith("skipped"):
            # format: "skipped (reason)" or "skipped reason"
            reason_m = re.match(r"skipped\s*\(([^)]+)\)", rest, re.IGNORECASE)
            reason = reason_m.group(1) if reason_m else rest[len("skipped"):].strip().lstrip("(").rstrip(")")
            if not reason:  # empty or missing reason — not a valid line
                continue
            result[n] = ("skipped", reason)
        # else: unrecognised format — ignored, never hides an item
    return result


def _apply(text: str, n: int, new_line: str) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    seen_awake, numbers, pickup_at, pickup_end = _scan(lines)
    if not seen_awake:
        raise Refusal("no '## For the awake seat' section in the record")
    if n not in numbers:
        shown = ", ".join(str(x) for x in numbers) or "none"
        raise Refusal(f"{n} is not an item number under '## For the awake seat' (items: {shown})")
    if pickup_at is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += nl
        if lines:
            lines.append(nl)
        lines.append("## Morning pickup" + nl)
        lines.append(new_line + nl)
        return "".join(lines)
    body = lines[pickup_at + 1 : pickup_end]
    kept: list = []  # (number or None, line)
    for ln in body:
        m = _PICKUP_LINE_RE.match(ln)
        if m and int(m.group(1)) == n:
            continue
        kept.append((int(m.group(1)) if m else None, ln))
    # drop leading/trailing blank padding, re-add it after sorting
    trailing = []
    while kept and not kept[-1][1].strip():
        trailing.insert(0, kept.pop())
    leading = []
    while kept and not kept[0][1].strip():
        leading.append(kept.pop(0))
    entries = [(k, ln) for k, ln in kept if k is not None]
    others = [(k, ln) for k, ln in kept if k is None]
    entries.append((n, new_line + nl))
    entries.sort(key=lambda t: t[0])
    new_body = [ln for _, ln in leading] + [ln for _, ln in entries] + [ln for _, ln in others]
    new_body = [ln if ln.endswith(("\n", "\r")) else ln + nl for ln in new_body]
    new_body += [ln for _, ln in trailing]
    return "".join(lines[: pickup_at + 1] + new_body + lines[pickup_end:])


def _latest(home: str) -> str:
    d = os.path.join(home, "history", "dream")
    try:
        # Sort by STEM, date-prefixed names only: a second dream on a day is `2026-10-02-turn110.md`
        # and plain filename order puts it BEFORE `2026-10-02.md` ('-' < '.'), so the tool would
        # edit the earlier record; a stray README.md / notes.md must never be "latest" either.
        names = sorted(
            (f for f in os.listdir(d)
             if f.endswith(".md") and _DATED_RE.match(f) and os.path.isfile(os.path.join(d, f))),
            key=lambda f: f[:-3],
        )
    except OSError:
        names = []
    if not names:
        raise Refusal(f"no dream record under {d}")
    return os.path.join(d, names[-1])


@contextlib.contextmanager
def _locked(path: str):
    """An exclusive lock across the whole read-modify-write: three parallel `--pick` calls (a normal
    thing for an agent to issue) otherwise lose updates, last writer wins, every process exiting 0.
    The lock is a hidden sidecar file in the record's directory; where fcntl is missing the edit
    runs unlocked."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    lock_path = os.path.join(os.path.dirname(os.path.realpath(path)), ".dream_pickup.lock")
    with open(lock_path, "a") as lock_fh:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError(f"lock busy (another dream_pickup holds {lock_path} for over {_LOCK_WAIT_S:g}s)")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _write_atomic(path: str, text: str) -> None:
    path = os.path.realpath(path)  # a symlinked record: the link stays, its target is rewritten
    mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp = tempfile.mkstemp(prefix=".dream_pickup.", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_n(raw: str) -> int:
    if not re.fullmatch(r"[0-9]+", raw.strip()) or int(raw) < 1:
        raise Refusal(f"N must be a positive integer item number, got {raw!r}")
    return int(raw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="dream_pickup.py",
        description=(
            "Record on the latest dream record what the awake seat did with each head item: "
            "a line under the last '## Morning pickup' section (created at the end if absent).  Keeps the file's mode bits only (not ACLs or xattrs).  Does NOT commit to git "
            "(the sleep skill's commit step owns that)."
        ),
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--pick", nargs=2, metavar=("N", "TEXT"), help="write 'pN. picked up TEXT'")
    grp.add_argument("--skip", nargs=2, metavar=("N", "REASON"), help="write 'pN. skipped (REASON)'")
    ap.add_argument("--record", help="dream record to edit (default: latest under <home>/history/dream)")
    ap.add_argument("--home", help="ENGRAM home (default: $ENGRAM_HOME, else ~/.engram)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        kind, (raw_n, raw_text) = ("picked up", args.pick) if args.pick else ("skipped", args.skip)
        n = _parse_n(raw_n)
        text = _fold(raw_text)
        if not text:
            raise Refusal("TEXT/REASON is empty")
        if args.record:
            path = args.record
        else:
            try:
                home = args.home or str(owner_read.engram_home())  # --home stays raw and explicit; the env goes through the resolver
            except ValueError as exc:  # a relative ENGRAM_HOME: refused, nothing read or written
                raise Refusal(str(exc))
            path = _latest(home)
        if not os.path.isfile(path):
            raise Refusal(f"not a record file: {path}")
        line = f"p{n}. picked up {text}" if kind == "picked up" else f"p{n}. skipped ({text})"
        try:
            with _locked(path):
                try:
                    with open(path, "rb") as fh:
                        old = fh.read().decode("utf-8")
                except UnicodeDecodeError:
                    raise Refusal(f"record is not valid UTF-8: {path}")
                new = _apply(old, n, line)
                _write_atomic(path, new)
        except OSError as e:
            print(f"dream_pickup: write failed: {e}", file=sys.stderr)
            return 1
    except Refusal as e:
        print(f"dream_pickup: {e}", file=sys.stderr)
        return 2
    print(f"wrote p{n} ({kind}) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
