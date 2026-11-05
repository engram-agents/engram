"""Shared by the two #4375 teardowns: which start-engram-daemon.sh processes still carry THIS test's home in
their environment. One implementation, because the first version (`ps eww -axo`) was blind on Linux.

`ps axeww -o pid=,command=` is all BSD-style options, which procps and macOS both accept and which print the
environment after the command. A listing that cannot be trusted is an error, never an empty list: nonzero
exit, empty output, or no row for THIS process (a listing that does not even contain the process asking is
not a full listing) each raise AssertionError (#4374's `_ps_listing_text` rule, reviewer finding on #4386).
"""

import os
import subprocess


def surviving_launchers(home_marker: str) -> list[str]:
    r = subprocess.run(
        ["ps", "axeww", "-o", "pid=,command="], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, f"ps exited {r.returncode}: {r.stderr.strip()[:200]!r}; cannot tell what survived"
    rows = [line for line in r.stdout.splitlines() if line.strip()]
    own = str(os.getpid())
    assert any(line.split(None, 1)[0] == own for line in rows), (
        "ps listing does not contain this process (own pid); it is not a full listing, so it proves nothing"
    )
    return [
        line.split(None, 1)[0]
        for line in rows
        if "start-engram-daemon.sh" in line and home_marker in line
    ]
