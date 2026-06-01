#!/usr/bin/env python3
"""migrate_config_v3 — one-shot migration of ~/.engram/config.json to schema v3.

Schema v3 adds two top-level fields:
  - thresholds: {...}  Empirical similarity thresholds (calibrated 2026-05-09).
  - polarity:   {...}  NLI cross-encoder polarity-dedup config (added 2026-05-10
                       after empirical bake-off).

Like v2, this is a SCHEMA EXTENSION migration — existing keys passed through
unchanged. Code that reads memory/embedding/cadence keeps working.

Hard-cut migration with prod-safety guards (dry-run, atomic backup, idempotent).

Usage:
    python tools/migration/migrate_config_v3.py [--config PATH] [--dry-run] [--force]

Flags:
    --config PATH   Path to config.json (default: $ENGRAM_HOME/config.json
                    or ~/.engram/config.json).
    --dry-run       Show planned diff; don't write anything.
    --force         Re-run even if schema_version is already 3 (overwrites
                    thresholds + polarity with defaults). Use only if those
                    sections got corrupted.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3

# Defaults: v4 calibration values (PR #58) — same as the source-of-truth
# constants in server.py. Keep these in sync if you change either.
DEFAULT_THRESHOLDS = {
    # Dedup at write time
    "dedup_top_k": 15,
    "dedup_min_similarity": 0.40,
    # Action-hint tier labels (used after dedup to label each candidate match)
    "action_hint_corroborate": 0.65,
    "action_hint_related": 0.50,
    # Pattern query "balanced" preset cosine threshold (other presets stay
    # placeholders pending pattern-specific telemetry per ob_1500)
    "pattern_balanced_cosine": 0.55,
}

# Defaults: NLI polarity-dedup config (added 2026-05-10 per issue #56 + bake-off).
# Default model is the empirical bake-off winner (AUC 0.847, F1 0.889).
# Default threshold is the peak-F1 operating point (recall 84%, precision 94%).
# enabled=FALSE as of 2026-05-15 (per Lei): the bake-off winner is a 1.5GB
# GPU model that's slow/unavailable on Macs without a capable GPU, and
# issue #106 documents a high false-positive "wolf-cry" rate on real
# usage. Users with the hardware and the calibration confidence can
# enable explicitly via `polarity.enabled = true` in config.json. Tests
# can use the `ENGRAM_NO_POLARITY` env var to force disable regardless
# of config.
#
# To enable: edit ~/.engram/config.json and set polarity.enabled = true.
# Re-reads happen on every observation write (no server restart needed).
DEFAULT_POLARITY = {
    "enabled": False,
    "model": "dleemiller/ModernCE-large-nli",
    "threshold": 0.46,
    # Skip polarity scoring on candidates with cosine similarity below this floor —
    # truly unrelated pairs aren't worth the NLI inference cost.
    "min_similarity_for_check": 0.30,
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

    if "thresholds" not in new_config or force:
        new_config["thresholds"] = dict(DEFAULT_THRESHOLDS)
    else:
        # Schema bump but thresholds already present — fill in any keys the
        # user lacks (in case a partial config was hand-written).
        merged = dict(DEFAULT_THRESHOLDS)
        merged.update(new_config["thresholds"])
        new_config["thresholds"] = merged

    if "polarity" not in new_config or force:
        new_config["polarity"] = dict(DEFAULT_POLARITY)
    else:
        merged = dict(DEFAULT_POLARITY)
        merged.update(new_config["polarity"])
        new_config["polarity"] = merged

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
                        help="Re-migrate even if already v3 (overwrites thresholds + polarity with defaults)")
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
        print("  Nothing to do. Re-run with --force to overwrite thresholds + polarity with defaults.")
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

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.config.with_name(args.config.name + f".pre-migration-{stamp}.bak")
    backup.write_text(json.dumps(original, indent=2) + "\n")
    print(f"  ✓ Backup: {backup}")

    args.config.write_text(json.dumps(new_config, indent=2) + "\n")
    print(f"  ✓ Wrote:  {args.config}")
    print()
    print(f"Migration complete. Rollback: cp {backup} {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
