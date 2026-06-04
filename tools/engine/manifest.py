"""tools.engine.manifest — load, validate, and select from packaging/tiers.json.

Pure Python 3 stdlib; ZERO imports from ENGRAM runtime modules.

Axis model
----------
tier       : depth tier — cumulative (essential ⊂ convenience ⊂ dev)
multi_agent: orthogonal flag — composes with any depth tier

select_shippables() is the canonical predicate, mirroring build-plugin.sh's
ships() function.  The predicate is:

    ships(m) := rank(m.tier) <= rank(chosen_tier)
                AND (m.multi_agent != true OR multi_agent_chosen)

where rank(essential)=0, rank(convenience)=1, rank(dev)=2.
"""

from __future__ import annotations

import json
import os
from typing import Any

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

TIER_RANKS: dict[str, int] = {
    "essential": 0,
    "convenience": 1,
    "dev": 2,
}

VALID_TIERS = frozenset(TIER_RANKS.keys())


# ---------------------------------------------------------------------------
# load + validate
# ---------------------------------------------------------------------------


def load_manifest(manifest_path: str) -> dict[str, Any]:
    """Load and validate packaging/tiers.json.

    Parameters
    ----------
    manifest_path:
        Absolute or relative path to tiers.json.

    Returns
    -------
    dict
        The parsed manifest contents.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If required top-level fields are missing or tier values are invalid.
    json.JSONDecodeError
        If the file is not valid JSON.
    """
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(
            f"packaging/tiers.json not found at {manifest_path} — is the manifest present?"
        )

    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)

    # Required top-level fields
    required = {"schema_version", "depth_tiers", "default_tier", "mechanisms"}
    missing_fields = required - set(data.keys())
    if missing_fields:
        raise ValueError(
            f"packaging/tiers.json is missing required top-level fields: "
            f"{sorted(missing_fields)}"
        )

    # Validate tier values
    declared_tiers = set(data["depth_tiers"])
    invalid: list[str] = []
    for entry in data["mechanisms"]:
        t = entry.get("tier")
        if t not in declared_tiers:
            invalid.append(f"  {entry['path']!r}: tier={t!r}")
    if invalid:
        raise ValueError(
            "packaging/tiers.json entries with invalid tier values:\n"
            + "\n".join(invalid)
        )

    return data


# ---------------------------------------------------------------------------
# tier resolution
# ---------------------------------------------------------------------------


def resolve_tier(manifest: dict[str, Any], tier_arg: str | None) -> str:
    """Return the chosen tier, falling back to manifest's default_tier.

    Parameters
    ----------
    manifest:
        Parsed tiers.json dict.
    tier_arg:
        The --tier argument value, or None to use the manifest default.

    Returns
    -------
    str
        The resolved tier string.

    Raises
    ------
    ValueError
        If tier_arg is not a valid tier.
    """
    default = manifest["default_tier"]
    chosen = tier_arg if tier_arg is not None else default

    valid = set(manifest["depth_tiers"])
    if chosen not in valid:
        raise ValueError(
            f"Invalid tier {chosen!r}. Valid tiers: {', '.join(sorted(valid))}"
        )

    return chosen


# ---------------------------------------------------------------------------
# selection predicate
# ---------------------------------------------------------------------------


def select_shippables(
    manifest: dict[str, Any],
    tier: str,
    multi_agent: bool,
) -> list[dict[str, Any]]:
    """Return the list of mechanism entries that ship under (tier, multi_agent).

    Predicate (mirrors build-plugin.sh):
        ships(m) := rank(m.tier) <= rank(chosen_tier)
                    AND (m.multi_agent != true OR multi_agent_chosen)

    Parameters
    ----------
    manifest:
        Parsed tiers.json dict (from load_manifest).
    tier:
        The chosen depth tier string (e.g. "convenience").
    multi_agent:
        Whether --multi-agent was requested.

    Returns
    -------
    list[dict]
        Mechanism entries (with "path", "tier", and optional "multi_agent" keys)
        that satisfy the predicate, in manifest order.
    """
    chosen_rank = TIER_RANKS.get(tier, 2)
    result: list[dict[str, Any]] = []
    for entry in manifest["mechanisms"]:
        entry_rank = TIER_RANKS.get(entry.get("tier", "dev"), 2)
        if entry_rank > chosen_rank:
            continue
        if entry.get("multi_agent") is True and not multi_agent:
            continue
        result.append(entry)
    return result


def get_identity_coupled_paths(manifest: dict[str, Any]) -> set[str]:
    """Return the set of paths that have identity_coupled=true.

    Used by the tag-once gate test and build validation.

    Parameters
    ----------
    manifest:
        Parsed tiers.json dict (from load_manifest).

    Returns
    -------
    set[str]
        Paths with identity_coupled=true.
    """
    return {
        entry["path"]
        for entry in manifest["mechanisms"]
        if entry.get("identity_coupled") is True
    }
