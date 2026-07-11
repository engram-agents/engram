#!/usr/bin/env python3
"""migrate_person_nodes.py — dry-run guide for migrating legacy person nodes.

PURPOSE
-------
Existing person nodes (any created before this schema migration) were created with the
old free-form schema: claim text, logical_chain, description columns.

The new typed schema (PR: feat/person-node-typed-schema) stores identity facts
in metadata as typed fields: name, relation, pronouns, trust_tier, aliases,
location_contact, birthday, lineage, architecture, first_session_date.

This script reads each current person node, extracts robust facts from the prose
fields, and prints a HUMAN-REVIEW report showing proposed typed-field values.

It does NOT auto-apply changes. Every extraction requiring judgment is flagged
explicitly for human decision before engram_update_person or direct DB surgery.

Experiential/relational prose (e.g. "I have not met her yet", "she introduced me
to X") is identified as candidate observations to be filed separately with
about-edges — NOT auto-filed by this script (judgment required: stale content
should be skipped, not filed).

USAGE
-----
    python3 migrate_person_nodes.py [--db PATH]

    --db PATH     Path to knowledge.db (default: ~/.engram/knowledge.db)

OUTPUT
------
A HUMAN-REVIEW report to stdout. Format:
  [pn_NNNN] <name>
    CLAIM (original): ...
    LOGICAL_CHAIN (original): ...
    PROPOSED TYPED FIELDS:
      name: ...
      relation: ...     [CONFIDENT / REVIEW: reason]
      trust_tier: ...   [CONFIDENT / REVIEW: reason]
      aliases: ...      [CONFIDENT / REVIEW: reason]
    PROSE REMNANTS (candidate observations):
      - "..." [SKIP: stale] / [FILE: <suggest quote_type>]
    DECISION REQUIRED: <specific question for human>
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Heuristic extractors — lightweight, judgment-flagging
# ---------------------------------------------------------------------------

_TRUST_TIER_KEYWORDS = {
    "self": ["self", "myself", "self-anchor", "own node"],
    "primary_user": ["primary user", "collaborator", "lei", "user"],
    "user_family": ["family", "daughter", "son", "partner", "spouse", "sibling"],
    "our_side": ["our_side", "colleague", "agent", "teammate"],
    "known_external": ["external", "known_external", "acquaintance"],
    "unknown": ["unknown"],
    "suspect": ["suspect", "adversarial"],
}


def _infer_trust_tier(text: str, existing_tier: str) -> tuple[str, str, str]:
    """Return (inferred_tier, confidence, note)."""
    if existing_tier and existing_tier not in ("unknown", ""):
        return (existing_tier, "CONFIDENT", "taken from nodes.trust_tier column")
    text_lower = text.lower()
    for tier, keywords in _TRUST_TIER_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return (tier, "REVIEW", f"inferred from keyword match in prose — verify")
    return ("unknown", "REVIEW", "no keyword match — defaulting to 'unknown', verify")


def _extract_name_from_claim(claim: str) -> tuple[str, str]:
    """Try to extract name from claim like 'Alex — primary collaborator'.
    Returns (name, confidence)."""
    # Pattern: "Name — role" or "Name: role"
    m = re.match(r"^([^—:]+)[—:]", claim)
    if m:
        return (m.group(1).strip(), "CONFIDENT")
    # Fallback: first word(s) before common conjunctions
    words = claim.strip().split()
    if words:
        return (" ".join(words[:2]), "REVIEW")
    return ("", "REVIEW")


def _extract_relation_from_claim(claim: str, logical_chain: str) -> tuple[str, str]:
    """Try to extract relation from 'Name — <relation>' pattern."""
    m = re.match(r"^[^—:]+[—:]\s*(.+)$", claim)
    if m:
        return (m.group(1).strip(), "CONFIDENT")
    if logical_chain:
        return (logical_chain[:80], "REVIEW")
    return ("", "REVIEW")


def _identify_prose_remnants(claim: str, logical_chain: str, description: str) -> list[dict]:
    """Identify experiential/relational prose that should become observations.

    Returns list of {text, disposition, note} where disposition is
    'FILE', 'SKIP', or 'REVIEW'.
    """
    remnants = []

    # Known stale patterns
    stale_patterns = [
        r"have not met",
        r"haven\'t met",
        r"not yet",
        r"don\'t know",
        r"unknown to me",
    ]

    def _classify_sentence(s: str) -> dict:
        s = s.strip()
        if not s:
            return None
        s_lower = s.lower()
        for pat in stale_patterns:
            if re.search(pat, s_lower):
                return {"text": s, "disposition": "SKIP", "note": "likely stale content"}
        if len(s) > 20:  # substantive sentence
            return {"text": s, "disposition": "REVIEW", "note": "may be worth filing as observation"}
        return None

    all_prose = " ".join(filter(None, [
        # Skip the name–relation claim (already extracted)
        logical_chain or "",
        description or "",
    ]))
    if all_prose.strip():
        sentences = re.split(r"[.!?]+", all_prose)
        for sentence in sentences:
            r = _classify_sentence(sentence)
            if r:
                remnants.append(r)

    return remnants


def _get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def run_report(db_path: str) -> None:
    """Generate the human-review report."""
    conn = _get_db(db_path)
    try:
        person_rows = conn.execute(
            "SELECT id, claim, logical_chain, metadata, trust_tier "
            "FROM nodes WHERE type = 'person' AND is_current = 1 "
            "ORDER BY id"
        ).fetchall()

        if not person_rows:
            print("No current person nodes found.")
            return

        print(f"Person Node Migration Report — {len(person_rows)} current person node(s)")
        print("=" * 72)
        print("IMPORTANT: This is a HUMAN-REVIEW guide. Do NOT auto-apply changes.")
        print("Each REVIEW item requires your judgment before updating.")
        print("=" * 72)
        print()

        for row in person_rows:
            meta = json.loads(row["metadata"] or "{}")
            claim = row["claim"] or ""
            logical_chain = row["logical_chain"] or ""
            description = meta.get("description", "")

            print(f"[{row['id']}]")
            print(f"  CLAIM (original):         {claim!r}")
            if logical_chain:
                print(f"  LOGICAL_CHAIN (original): {logical_chain!r}")

            # Check if already migrated (has typed fields)
            if "name" in meta and "field_traces" in meta:
                print(f"  STATUS: Already has typed fields — possibly already migrated.")
                print(f"  METADATA: {json.dumps(meta, indent=4)}")
                print()
                continue

            # Extract fields
            extracted_name, name_conf = meta.get("name", None), "CONFIDENT"
            if not extracted_name:
                extracted_name, name_conf = _extract_name_from_claim(claim)

            extracted_relation, rel_conf = _extract_relation_from_claim(claim, logical_chain)

            # existing role field (old schema)
            existing_role = meta.get("role", "")
            if existing_role and rel_conf != "CONFIDENT":
                extracted_relation = existing_role
                rel_conf = "CONFIDENT"

            extracted_tier, tier_conf, tier_note = _infer_trust_tier(
                claim + " " + logical_chain, row["trust_tier"] or ""
            )

            existing_aliases = meta.get("aliases", [])
            if isinstance(existing_aliases, str):
                existing_aliases = [s.strip() for s in existing_aliases.split(",") if s.strip()]

            is_self = meta.get("is_self", False)

            print(f"  PROPOSED TYPED FIELDS:")
            print(f"    name:       {extracted_name!r} [{name_conf}]")
            print(f"    relation:   {extracted_relation!r} [{rel_conf}]")
            print(f"    trust_tier: {extracted_tier!r} [{tier_conf}: {tier_note}]")
            if existing_aliases:
                print(f"    aliases:    {existing_aliases} [CONFIDENT: taken from existing metadata]")
            if is_self:
                print(f"    is_self:    True [CONFIDENT]")

            # Prose remnants
            remnants = _identify_prose_remnants(claim, logical_chain, description)
            if remnants:
                print(f"  PROSE REMNANTS (review for candidate observations):")
                for r in remnants:
                    disp = r["disposition"]
                    print(f"    [{disp}] {r['text']!r}")
                    print(f"           Note: {r['note']}")

            # Node-specific decisions
            print(f"  DECISIONS REQUIRED:")
            decisions = []
            if name_conf != "CONFIDENT":
                decisions.append(f"Confirm name: extracted {extracted_name!r} — is this correct?")
            if rel_conf != "CONFIDENT":
                decisions.append(
                    f"Confirm relation: extracted {extracted_relation!r} from claim — correct?"
                )
            if tier_conf == "REVIEW":
                decisions.append(
                    f"Confirm trust_tier: inferred '{extracted_tier}' ({tier_note})"
                )
            if not decisions:
                decisions.append("No decisions required — all fields extracted with confidence.")
            for d in decisions:
                print(f"    - {d}")

            print()
            print(f"  MIGRATION COMMAND (after review):")
            update_payload = {
                "node_id": row["id"],
                "updates": {},
                "reason": "typed-schema migration",
            }
            if extracted_relation:
                update_payload["updates"]["relation"] = extracted_relation
            print(f"    engram_update_person({json.dumps(update_payload)})")
            print()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Generate a human-review migration report for legacy person nodes."
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.engram/knowledge.db"),
        help="Path to knowledge.db (default: ~/.engram/knowledge.db)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found at {args.db}", file=sys.stderr)
        print("Use --db PATH to specify the correct path.", file=sys.stderr)
        sys.exit(1)

    run_report(args.db)


if __name__ == "__main__":
    main()
