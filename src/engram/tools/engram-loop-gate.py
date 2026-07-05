#!/usr/bin/env python3
"""engram-loop-gate — Loop self-gate: should an autonomous wake run or re-defer?

Prints one line to stdout and exits 0 in all cases (fail-open).

Default output:
  "defer <seconds>" when the user is currently engaged (recent activity
  within cadence.engaged_window_seconds).
  "proceed" when the loop may safely run (no recent activity, or any error).

--json: prints {"decision": ..., "defer_seconds": ..., "reason": ...}.

Part of #1456 v1 — loop-side engaged-defer self-gate.
"""

import argparse
import json
import sys
from pathlib import Path

# _status_derive is a sibling module (stdlib-only). Insert the script's own
# directory so the import works whether the script is run as a CLI or
# imported from outside tools/. Matches the pattern used by forum.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _status_derive import loop_gate_decision  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decide whether an autonomous loop-wake should run or re-defer."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output instead of plain text.",
    )
    args = parser.parse_args()

    try:
        decision, defer_seconds, reason = loop_gate_decision()
    except Exception:
        # Fail-open at the CLI layer: any unhandled exception → proceed.
        if args.json:
            print(json.dumps(
                {"decision": "proceed", "defer_seconds": 0, "reason": "cli-error"}
            ))
        else:
            print("proceed")
        sys.exit(0)

    if args.json:
        print(json.dumps(
            {"decision": decision, "defer_seconds": defer_seconds, "reason": reason}
        ))
    else:
        if decision == "defer":
            print(f"defer {defer_seconds}")
        else:
            print("proceed")

    sys.exit(0)


if __name__ == "__main__":
    main()
