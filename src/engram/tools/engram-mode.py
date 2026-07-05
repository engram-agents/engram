#!/usr/bin/env python3
"""engram-mode — Set, clear, or show the presence mode (user/auto).

Usage:
  engram-mode user [--ttl SECONDS]   Set mode to 'user' (human present)
  engram-mode auto [--ttl SECONDS]   Set mode to 'auto' (autonomous)
  engram-mode clear                   Clear override (revert to derived)
  engram-mode show [--json]           Print current mode and bundle

Exit 0 always (fail-open at the CLI layer).

Mirrors the sibling-import + arg pattern of engram-loop-gate.py.
"""

import argparse
import json
import sys
from pathlib import Path

# Sibling module — insert the script's own directory so 'presence' resolves
# whether this script is invoked bare or imported from outside tools/.
# Same pattern as engram-loop-gate.py → _status_derive.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import presence  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect or override the presence mode (user/auto).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # user subcommand
    sp_user = subparsers.add_parser("user", help="Set mode to 'user' (human present)")
    sp_user.add_argument(
        "--ttl",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override expires after this many seconds",
    )

    # auto subcommand
    sp_auto = subparsers.add_parser("auto", help="Set mode to 'auto' (autonomous)")
    sp_auto.add_argument(
        "--ttl",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override expires after this many seconds",
    )

    # clear subcommand
    subparsers.add_parser("clear", help="Clear override (revert to derived mode)")

    # show subcommand
    sp_show = subparsers.add_parser("show", help="Print current mode and bundle")
    sp_show.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print JSON instead of plain text",
    )

    args = parser.parse_args()

    if args.command in ("user", "auto"):
        try:
            presence.set_mode(args.command, ttl_seconds=args.ttl)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        ttl_msg = f" (ttl={args.ttl}s)" if args.ttl is not None else ""
        print(f"mode set to '{args.command}'{ttl_msg}")

    elif args.command == "clear":
        try:
            presence.clear_mode()
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        print("mode override cleared (reverted to derived)")

    elif args.command == "show":
        try:
            mode_name, bundle = presence.get_mode()
        except Exception:
            # Fail-open: if get_mode blows up, show auto.
            mode_name = "auto"
            bundle = presence.MODE_BUNDLES["auto"]
        if args.as_json:
            print(json.dumps({"mode": mode_name, "bundle": bundle}))
        else:
            print(f"mode: {mode_name}")
            for k, v in bundle.items():
                print(f"  {k}: {v}")

    else:
        parser.print_help()

    sys.exit(0)


if __name__ == "__main__":
    main()
