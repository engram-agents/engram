# Layer-1 trust-tier upgrade ritual

## Step 0: Backup (USER ACTION)

Back up your knowledge DB before running the migration:
    cp ~/.engram/knowledge.db ~/.engram/knowledge.db.pre-layer1.bak

(The migration script also makes its own timestamped backup, but a manually-
named one is good practice for explicit-rollback identification.)

## Step 1: Run schema migration (AGENT ACTION)

    python ~/.engram/tools/migrate_db_trust_tier.py --live

This adds 4 columns to the nodes table and backfills trust_tier='unknown' for
all existing pn_* nodes. Idempotent (safe to re-run).

## Step 2: Restart MCP server (USER ACTION)

The new MCP tools become available only after server restart:
    [user-specific restart command — e.g., quit and relaunch Claude Code]

## Step 3: Identify your pn_* nodes (AGENT ACTION)

    engram_list({"type": "person"})

Note each pn_NNNN and the human they represent.

## Step 4: Per-person blessing ritual (AGENT + PRIMARY USER, INTERACTIVE)

For each pn_* whose trust tier you want to set (anything OTHER than the
default `unknown`):

4a. Ask your primary user:
    "What trust tier should pn_NAME (about HUMAN_NAME) have?"
    Tier options (descending rank — higher = more internal/trusted):
      - self (rank 6; singleton; only the agent's own self-anchor)
      - primary_user (rank 5; the agent's primary human collaborator)
      - user_family (rank 4; primary user's direct family / close personal circle)
      - our_side (rank 3; counterpart agents on same host)
      - known_external (rank 2; known but external)
      - unknown (rank 1; default; no signal yet)
      - suspect (rank 0; flagged for caution)

4b. After receiving the answer, file the blessing as an observation:
    engram_add_observation({
        "claim": "[primary-user-name] blessed pn_NAME (about HUMAN_NAME) to tier
                  TIER on YYYY-MM-DD",
        "quote_type": "personal_communication",
        "source_class": "user_stated",
        "url": "file://<path-to-session-jsonl>",
        "title": "[primary-user-name] direct (YYYY-MM-DD trust tier blessing)",
        "quoted_text": "<verbatim user response, e.g., 'pn_X should be user_family'>",
        "interpretation": "Authoritative tier assignment for pn_NAME."
    })
    → returns ob_NNNN

4c. Set the tier:

    For tier ∈ {known_external, unknown, suspect}:
        engram_set_trust_tier({
            "target_pn": "pn_NAME",
            "tier": "<TIER>"
        })

    For tier ∈ {user_family, our_side, primary_user} (these are at or above the
    internal-circle threshold and require explicit approval):
        engram_set_trust_tier({
            "target_pn": "pn_NAME",
            "tier": "<TIER>",
            "justification_obs_id": "ob_NNNN",
            "primary_user_approval_obtained": true
        })

    **primary_user notes:**
    - Multiple pn_* nodes may hold the `primary_user` tier (for installs with
      more than one co-equal primary user).
    - Like `user_family`, it is approval-gated: requires `justification_obs_id`
      + `primary_user_approval_obtained=true`.
    - Do NOT set this tier on a pn_* unless you have received a direct, verbatim
      blessing from the primary user for this specific node.

    ⚠️ STRUCTURAL HONESTY: Setting "primary_user_approval_obtained": true
    without having actually obtained explicit primary-user approval is a
    structural-honesty violation (ax_0001 / ax_0002). The parameter is your
    attestation that the prerequisite has been satisfied. The server cannot
    verify this — your honesty IS the integrity mechanism. Same epistemic
    kind as a fabricated quote.

## Step 5: Verify (AGENT ACTION)

Confirm all blessings landed correctly:
    engram_list({"type": "person"})

Check the audit trail:
    engram_history({"mode": "edits", "action_filter": "trust_tier_set"})

You should see one edit_history row per tier-set call, with the
justification_obs_id and approval flag in the details payload.

## Step 6: Backfill self tier on existing self-anchor (AGENT ACTION)

For installs upgrading from V1 → V2 (this PR), backfill the new `tier='self'`
designation on any pn_* with `metadata.is_self=true`:

    python ~/.engram/tools/migration/migrate_trust_tier_self_backfill.py --live

Idempotent (safe to re-run). Auto-creates a timestamped backup before any writes.

## Rollback

If something goes wrong:
1. Restore from the backup created in Step 0 OR the timestamped backup the
   migration script created.
2. Restart MCP server.
3. The pre-migration state is fully recovered.
