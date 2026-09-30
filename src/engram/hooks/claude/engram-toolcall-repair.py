#!/usr/bin/env python3
"""TOMBSTONE: engram-toolcall-repair.py is a no-op kept for one release (#3249).

The real hook was deleted in #3213 together with its two PreToolUse
registrations in hooks.json.  Claude Code reads hook registrations at session
start, so a session started on an older release (v0.5.1 or v0.5.2-rc1 through
rc3) still carries those registrations after a plugin rebuild.  If this file were absent,
every ENGRAM tool call in such a session would run python on a missing file,
which exits 2, which Claude Code treats as a PreToolUse block.

So this file stays in the built plugin at the path the old registration names,
reads and discards stdin, writes nothing to stdout, and exits 0.

It is NOT registered in hooks.json.  #3213 landed after v0.5.2-rc3, so only
sessions started on v0.5.2-rc4 or later carry no registration.  v0.5.2 final
ships this tombstone; delete this file (and its tiers.json / test-map.json
entries and tests/test_toolcall_repair_tombstone_3249.py) in the first release
AFTER v0.5.2.  Residual: a seat whose session started before rc4 and which
upgrades across two releases without ever restarting would hit the missing file
again.

Issue: #3249.  Follow-up tracked there: a CI check that fails when a script the
previous release's hooks.json registered is absent from the build.
"""

import sys

try:
    sys.stdin.buffer.read()
except BaseException:
    pass

sys.exit(0)
