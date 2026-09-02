#!/usr/bin/env python3
"""_proc_identity.py -- shared process-identity verification for the forum
monitor scripts (#2321).

Extracted from a 1629-byte block that was byte-identical, hand-synced, in
`forum-mention-monitor.sh` and `forum-updates-monitor.sh` (a third variant of
the same pattern lives in umwelt's `umwelt-bell.py`, not touched by this
change). The duplication predated #2314; that PR grew the block from ~18 to
~45 lines and added a second confidence class (rc=0 /proc-verified vs rc=3
ps-fallback) plus a new exit code that both bash copies then had to keep in
sync BY HAND -- two drifting copies of a two-class detector means one monitor
could silently downgrade its warnings while the other does not, which is the
exact alarm-channel failure #2314 itself was written to avoid. This module
collapses the duplication and makes the detector directly unit-testable
without a subprocess round-trip (import `check_process_identity` in tests;
the bash callers still shell out to this file as a script, unchanged
interface from their side).

Verifies a CANDIDATE pid is genuinely a duplicate instance of `script`
(a Bash shell whose argv actually names that script), not just "some process
happens to have this pid" -- callers find the candidate pid via `pgrep`
first; this module is the second, stronger layer, not the only guard.

## Three outcomes, not two

A process that vanishes mid-read (ENOENT/ESRCH -- the file or pid is simply
gone) is correctly silent: it lost the race, not a duplicate. But any OTHER
read failure (e.g. EACCES -- /proc read denied) means verification could NOT
be completed, which is a DIFFERENT claim than "verified not a match" and must
not be silently folded into it -- callers must warn, not silently skip.

## Portability (#2134)

/proc does not exist on macOS, so a naive first `open()` would raise ENOENT
and land in the "process gone" branch -- dead and SILENT, byte-identical to
"checked, found no duplicates." Mirrors umwelt-bell.py's `_is_bell_process`
fix (umwelt #205): when /proc is absent, fall back to the portable
`ps -p <pid> -o args=`. That fallback is WEAKER than the /proc read and is
reported as such: `ps -o args=` returns a space-JOINED string, not true
NUL-separated argv, so a wrapper whose script path sits inside a single
quoted argument (e.g. `bash -c "... forum-mention-monitor.sh ..."`) can
manufacture a false token boundary and over-match -- measured on Linux
(issue #2134 comments) against a `bash -c` wrapper. Kept anyway because a
weaker-but-labeled detector beats no detection at all on the one platform
that has no /proc to fall back on. rc 3 marks this weaker verdict distinctly
from rc 0's /proc-verified one so the two warning classes don't get
conflated into a single alarm-fatigue channel.

Related platform trap, deliberately NOT fixed here (out of this extraction's
scope, per #2321's own framing -- a separate defect class): #2133, `pgrep -a`
meaning opposite-in-kind things on Darwin vs procps-ng. That affects the
CANDIDATE-FINDING `pgrep` call in each bash caller, upstream of this module;
this module only verifies a candidate pid it's already been handed. The
shared module is a natural home for that fix too, once someone picks it up.

Exit codes (unchanged from the two pre-extraction copies -- this is a pure
extraction, no behavior change):
    0 = confirmed duplicate, /proc-verified (strong) -- stdout is the
        cmdline (argv joined on spaces, truncated to ~100 chars -- the boot
        charge alone runs ~2.5KB and must not flood the Monitor event
        stream).
    1 = not a duplicate, including "process gone mid-read" (a race, not an
        error) -- silent, nothing on stdout.
    2 = could NOT verify (a non-ENOENT/ESRCH OSError, e.g. permission
        denied, or the `ps` fallback itself erroring) -- stdout is a short
        reason string; caller must warn, not silently skip.
    3 = probable duplicate, ps-fallback-verified (weak -- /proc absent,
        string-split match only) -- stdout is the ps `args=` output,
        truncated to ~100 chars.

Usage (as a script, from a bash caller):
    python3 _proc_identity.py <pid> <script-basename>
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys

_SHELL_INTERPRETERS = ("bash", "sh", "dash", "ksh", "zsh", "ash")
_TRUNCATE_LEN = 100


def check_process_identity(pid: str, script: str) -> tuple[int, str]:
    """Verify `pid` is a shell process whose argv names `script`.

    Returns `(exit_code, stdout_text)` -- `stdout_text` is `""` for exit
    code 1 (nothing to print), matching the two pre-extraction bash copies'
    own contract exactly. See the module docstring for the full exit-code
    table.

    `ENGRAM_PROC_DIR` is a true optional, read from the environment: it
    exists only so tests can force the ps-fallback branch on Linux (point it
    at an empty dir). Its ABSENCE means "use the real system procfs" and
    must produce no warning.
    """
    proc_root = os.environ.get("ENGRAM_PROC_DIR", "/proc")

    if os.path.exists(proc_root + "/self/cmdline"):
        try:
            with open(proc_root + "/" + pid + "/cmdline", "rb") as f:
                raw = f.read()
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ESRCH):
                return 1, ""  # process gone mid-read: not a match, not an error
            return 2, str(e) or type(e).__name__  # could not verify

        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv:
            return 1, ""
        interp = os.path.basename(argv[0]).lstrip("-")  # login shell: argv[0] == "-bash"
        if interp not in _SHELL_INTERPRETERS:
            return 1, ""
        if not any(a == script or a.endswith("/" + script) for a in argv[1:]):
            return 1, ""
        cmd = " ".join(argv)
        return 0, cmd[:_TRUNCATE_LEN]

    # No /proc (macOS): portable fallback, mirrors umwelt-bell.py.
    try:
        r = subprocess.run(
            ["ps", "-p", pid, "-o", "args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001 - subprocess failure surface is not enumerable in advance
        return 2, str(e) or type(e).__name__

    cmd = r.stdout.strip()
    if not cmd:
        return 1, ""  # ps found no such pid: gone, not a match
    tokens = cmd.split()
    if not tokens:
        return 1, ""
    interp = os.path.basename(tokens[0]).lstrip("-")
    if interp not in _SHELL_INTERPRETERS:
        return 1, ""
    if not any(t == script or t.endswith("/" + script) for t in tokens[1:]):
        return 1, ""
    return 3, cmd[:_TRUNCATE_LEN]  # weak match -- ps-fallback only, no /proc to verify against


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: _proc_identity.py <pid> <script-basename>", file=sys.stderr)
        return 2
    pid, script = argv
    code, text = check_process_identity(pid, script)
    if text:
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
