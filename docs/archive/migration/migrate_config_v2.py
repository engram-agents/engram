#!/usr/bin/env python3
"""migrate_config_v2 — one-shot migration of ~/.engram/config.json to schema v2.

Schema v2 adds two top-level fields:
  - schema_version: 2  (sentinel for re-run guard)
  - cadence: {...}     (drowsiness meter + in-session auto-sleep tunables)

Hard-cut migration with prod-safety guards (per active-work/
engram-config-surface-2026-05-04.md decisions):
  - Dry-run mode prints the planned diff; nothing is written.
  - Live mode writes config.json.pre-migration-<UTC-timestamp> as a
    sibling backup BEFORE replacing config.json. Rollback is one cp.
  - Idempotent: if schema_version is already 2, exits cleanly without
    touching the file or writing a backup.
  - Adds cadence with defaults for drowsiness meter (80/90) and in-session
    auto-sleep scheduler (disabled, 03:00).

Other top-level keys (trust_pool, confidence_map, memory, embedding,
user, primary_user, etc.) are passed through unchanged. This is a
SCHEMA EXTENSION migration, not a SHAPE REORGANIZATION — code that
reads existing keys keeps working.

Usage:
    python tools/migration/migrate_config_v2.py [--config PATH] [--dry-run] [--force]

Flags:
    --config PATH   Path to config.json (default: $ENGRAM_HOME/config.json
                    or ~/.engram/config.json).
    --dry-run       Show planned diff; don't write anything.
    --force         Re-run even if schema_version is already 2 (overwrites
                    cadence with defaults from this script). Use only if
                    cadence got corrupted somehow.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

# Defaults for the drowsiness meter + in-session auto-sleep scheduler.
# The external-cron heartbeat/sleep keys (heartbeat_minutes, sleep_hour,
# sleep_minute, sleep_protected_window_minutes) were removed in #785.
DEFAULT_CADENCE = {
    # Drowsiness threshold percentages (relative to mean auto-compaction
    # JSONL size). Calibrated 2026-05-04 per PR #9.
    "drowsiness_caution_pct": 80,
    "drowsiness_urgent_pct": 90,
    # In-session auto-sleep scheduler — consumed by the SessionStart hook.
    "auto_sleep_enabled": False,
    "auto_sleep_time": "03:00",
}


def default_config_path() -> Path:
    base = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    return Path(base) / "config.json"


def migrate(config: dict, force: bool = False) -> tuple[dict, bool]:
    """Return (new_config, did_migrate). Idempotent unless force=True."""
    current_version = config.get("schema_version", 1)
    if current_version >= SCHEMA_VERSION and not force:
        return config, False

    new_config = dict(config)
    new_config["schema_version"] = SCHEMA_VERSION

    # Add cadence if missing. If force=True and cadence already exists,
    # overwrite with defaults (deliberate per --force semantics).
    if "cadence" not in new_config or force:
        new_config["cadence"] = dict(DEFAULT_CADENCE)
    else:
        # Schema bump but cadence already present — fill in any keys the
        # user lacks (in case a partial-cadence config was hand-written).
        merged = dict(DEFAULT_CADENCE)
        merged.update(new_config["cadence"])
        new_config["cadence"] = merged

    return new_config, True


def render_diff(old: dict, new: dict) -> str:
    """Compute a minimal added-keys diff. We don't reorganize, only add."""
    lines = []
    for key in new:
        if key not in old:
            lines.append(f"  + {key}: {json.dumps(new[key], indent=2)}")
        elif old[key] != new[key]:
            lines.append(f"  ~ {key}:")
            lines.append(f"      old: {json.dumps(old[key])}")
            lines.append(f"      new: {json.dumps(new[key])}")
    return "\n".join(lines) if lines else "  (no changes)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=default_config_path(),
                        help="Path to config.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show planned diff; don't write")
    parser.add_argument("--force", action="store_true",
                        help="Re-migrate even if already v2")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"ERROR: {args.config} does not exist", file=sys.stderr)
        return 1

    try:
        original = json.loads(args.config.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {args.config} is not valid JSON: {e}", file=sys.stderr)
        return 1

    new_config, did_migrate = migrate(original, force=args.force)

    if not did_migrate:
        v = original.get("schema_version", 1)
        print(f"✓ {args.config} is already at schema v{v} (target: v{SCHEMA_VERSION})")
        print("  Nothing to do. Re-run with --force to overwrite cadence with defaults.")
        return 0

    print(f"== Config migration: v{original.get('schema_version', 1)} → v{SCHEMA_VERSION} ==")
    print(f"  File:    {args.config}")
    print(f"  Mode:    {'dry-run' if args.dry_run else 'live'}")
    print()
    print("Planned changes:")
    print(render_diff(original, new_config))
    print()

    if args.dry_run:
        print("Dry-run complete. Re-run without --dry-run to apply.")
        return 0

    # Backup original. with_name preserves the full filename ("config.json")
    # and appends the suffix; with_suffix would REPLACE the .json extension.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.config.with_name(args.config.name + f".pre-migration-{stamp}.bak")
    backup.write_text(json.dumps(original, indent=2) + "\n")
    print(f"  ✓ Backup: {backup}")

    # Write new config
    args.config.write_text(json.dumps(new_config, indent=2) + "\n")
    print(f"  ✓ Wrote:  {args.config}")
    print()
    print(f"Migration complete. Rollback: cp {backup} {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
