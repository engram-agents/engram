#!/usr/bin/env python3
"""Write the default ``config.json`` for a NEW ENGRAM seat -- a human-invoked recovery command (#4705).

ENGRAM never regenerates a lost ``config.json`` beside an existing ``knowledge.db`` (a default would
blank ``self_lineage``, which turns the model-family gate dormant with no message, and restart the
turn counter).  The tool error says so and offers two remedies: restore a backup the user chooses, or,
if the user confirms the seat is genuinely new, run this command.

Usage::

    write-default-config.py --confirm-new-seat

* Without the flag it writes nothing, says so, and exits 2.
* It writes only when ``config.json`` is absent.  It never overwrites: when the file exists it
  refuses, leaves the bytes untouched, and exits 3.
* It prints the path it wrote and a content summary.
* The content is the default ``_ensure_data_dir`` writes (one shared function,
  ``engram_core.default_config_dict``), so the two writers cannot drift.
* It is never called from a hook or any automatic path.

The ENGRAM home is ``$ENGRAM_HOME`` (default ``~/.engram``).

Exit codes: 0 written; 2 refused (no confirmation flag, or the write failed); 3 refused (config.json
exists).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# engram_core sits one directory above tools/ in BOTH layouts (src/engram/ and the deployed plugin
# root). Appended, not inserted: an in-process load must not leave it at sys.path[0] (#4029).
_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.append(_PKG_ROOT)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_EXISTS = 3

FLAG = "--confirm-new-seat"

_FLAG_HELP = (
    "The human's word that this seat is NEW, not the agent's: pass it only after the user has said so "
    "in their own words. This tool cannot tell who typed it; the flag records an attestation, it does "
    "not enforce one."
)


def _summary(cfg: dict) -> str:
    keys = ", ".join(sorted(cfg))
    return (f"{len(cfg)} top-level keys ({keys}); self_lineage={json.dumps(cfg.get('self_lineage'))} "
            f"(blank until the first-session skill sets it); memory.current_turn="
            f"{cfg.get('memory', {}).get('current_turn')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="write-default-config.py",
        description="Write the default config.json for a NEW ENGRAM seat. Human-invoked recovery command: "
                    "never overwrites an existing config.json; never called automatically.",
    )
    ap.add_argument(FLAG, dest="confirm", action="store_true", help=_FLAG_HELP)
    args = ap.parse_args(argv)

    import engram_core as core  # heavy import last: --help and the flag check stay cheap

    path = core.CONFIG_PATH
    if not args.confirm:
        print(f"refused: nothing written. This seat's config.json would be created at {path} only when "
              f"you pass {FLAG} (the user's confirmation that the seat is new).", file=sys.stderr)
        return EXIT_REFUSED
    if os.path.lexists(path):
        print(f"refused: {path} exists; this command never overwrites it. Left unchanged.", file=sys.stderr)
        return EXIT_EXISTS
    cfg = core.default_config_dict()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        core._config_io.write_config_json(path, cfg)
    except OSError as exc:
        print(f"refused: could not write {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"wrote {path}: {_summary(cfg)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
