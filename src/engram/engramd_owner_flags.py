#!/usr/bin/env python3
"""The ONE definition of "is an engramd owner flag on" (engram-alpha#3663, U5 of #2652).

Every `engramd.*_via_owner` flag lives under the `engramd` object of
<ENGRAM_HOME>/config.json. Two questions, one rule:

  * `flag_on(key, config_path)` -- is that ONE key on? The rule (the one U4d's
    `user_identity_reads_via_owner` has always used): the JSON boolean `true` and
    nothing else. A string "true", 1, null, a missing key or section, a config that
    is absent, malformed, deeply nested (json raises RecursionError), not an object, or not
    a regular file are all OFF.
  * `any_owner_flag_on(config_path)` -- is ANY key matching `*_via_owner` under
    `engramd` on (by the rule above)? Matched by PATTERN, so a future hook's flag is
    covered with no edit here. The owner launcher's SessionStart fallback asks this
    ("does anything on this seat need an owner?").

The config read never blocks: a FIFO or other non-regular file is OFF, not a hang (a
hook runs under a timeout). This module imports only the standard library, so it
resolves wherever the plugin root is (never import hook code from here).

CLI (used by hooks/start-engramd-owner.sh): `engramd_owner_flags.py <ENGRAM_HOME>`
prints `on` or `off` on stdout and exits 0. A caller that gets anything else (a crash,
no python) cannot tell, and must treat that as ambiguous rather than as off.

U4d's `_owner_reads_enabled` (engram-user-identity-hook.py) calls `flag_on`. New readers
(the U6a stop hook, #3664) must call flag_on; do not write a per-hook reader.
"""

from __future__ import annotations

import json
import os
import stat
import sys

OWNER_FLAG_SUFFIX = "_via_owner"


def _load_engramd(config_path) -> dict:
    """The `engramd` object of the config at `config_path`, or {} for anything unusable."""
    try:
        fd = os.open(os.fspath(config_path), os.O_RDONLY | os.O_NONBLOCK)
    except (OSError, TypeError, ValueError):
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = -1  # the file object owns it now
            config = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}  # RecursionError: valid but absurdly nested JSON is unusable, not a crash
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(config, dict):
        return {}
    section = config.get("engramd")
    return section if isinstance(section, dict) else {}


def flag_on(key: str, config_path) -> bool:
    """True iff `engramd.<key>` in the config at `config_path` is the boolean `true`."""
    return _load_engramd(config_path).get(key) is True


def any_owner_flag_on(config_path) -> bool:
    """True iff any `engramd.*_via_owner` key is the boolean `true`."""
    return any(
        isinstance(k, str) and k.endswith(OWNER_FLAG_SUFFIX) and v is True
        for k, v in _load_engramd(config_path).items()
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: engramd_owner_flags.py <ENGRAM_HOME>", file=sys.stderr)
        return 2
    print("on" if any_owner_flag_on(os.path.join(argv[1], "config.json")) else "off")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
