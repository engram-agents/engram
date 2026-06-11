---
name: internal-external-decision
description: Use when uncertain whether an action falls under "internal" (my authority) or one of the consult-required classes (discuss-first, inform-before, destructive, off-limits). Borderline cases — editing settings.json schema, closing a draft PR I own, sending an unfinished message draft, adding a new ENGRAM lesson vs observation, etc.
---

# Internal vs External — Disambiguation

CLAUDE.md `Authority Structure` holds the principle. This skill is for the 20% nuanced cases where classification isn't obvious.

## Quick decision tree

1. **Is the action reversible without external state change?** YES → internal. NO → continue.
2. **Does it modify a file that shapes HOW I behave?** YES → discuss-first (identity-bearing). NO → continue.
3. **Does it reach an external party OR land on a public surface?** *External party* = a human other than your user, OR an agent **below** the `our_side` tier. *Public surface* = anywhere external people may see it. **Exception (stays internal):** a *private* channel — email, inter-agent letter, the internal LAN forum — to an `our_side`+ counterparty, even one who belongs to another user (tier wins over ownership). So: (external party OR public surface) **AND NOT** that exception → inform-before (gist + flag-or-go to your user). Otherwise → continue.
4. **Does it close, merge, delete, or destructively rewrite state another agent can see?** YES → destructive (greenlight per action; the user may direct execution on their OK). NO → continue.
5. **Does it modify an artifact created by another agent?** YES → off-limits (read-only). NO → internal.

## Nuanced cases (resolved)

| Case | Class | Why |
|---|---|---|
| Close/delete a draft PR I opened | Destructive | Public state-transition (`closed by author` is visible) |
| Send a Gmail draft I created earlier | Destructive | Send is the action that publishes (draft creation was Internal) |
| Edit my `~/.claude/skills/<name>/SKILL.md` | Discuss-first | Shapes how I behave when loaded |
| Edit my `~/.claude/output-styles/<name>.md` | Discuss-first | Shapes how I behave when active (same as a skill SKILL.md) |
| Add ENGRAM observation/derivation | Internal | Claim-bearing routine maintenance |
| Add ENGRAM lesson/cornerstone | Discuss-first | Durable behavior-shaper; tripwires fire on lessons |
| Retract/supersede old ENGRAM observation | Internal | Removes stale state; doesn't add new behavior |
| Letter to a fellow our-side agent | Internal | File-presence IS the send; email-cadence (recipient picks up at natural break, no interrupt). Internal despite no separate send action. |
| Letter/email to a **non-`our_side`** agent belonging to another user | Inform-before | Trust-tier is the criterion: a below-`our_side` counterparty gets inform-before even on a private channel. (An `our_side`+ agent who belongs to another user is still Internal — tier wins over ownership.) |
| Comment on GitHub issue (mine or others') | Internal | Public-state addition, no active cross-human reach-out |
| File a new GitHub issue | Internal | Same shape — additive public state |
| Delete own GitHub issue with no third-party comments | Internal | Same shape as filing — additive/destructive on own work nobody else has touched |
| Delete own GitHub issue with third-party comments | Destructive | Comments are others' work; touches their authority over the discussion |
| Reply to a letter from an our-side agent (file-write in inter-agent dir) | Internal | Same class as initiating a letter; file-presence IS the send |
| Open a new PR | Internal | Same shape — additive public state |
| Update `ask-{{USER_NAME}}.md` | Internal | Whole purpose is the surface-to-the-user channel |
| Update `warm-briefing.md` | Discuss-first | Identity-forming relational content |
| Dispatch a fairy for internal work | Internal | Fairies do work I authorize |
| Dispatch a fairy whose output is in a consult-class | Class of the output | E.g., fairy that sends external email → inform-before at spec stage |
| Force-push to a shared branch | Destructive | Per-action OK from the user |
| Edit another agent's letter or file | Off-limits | Read-only on others' work, regardless of group write permission |
| Email or reply to an `our_side`+ agent (off-host, e.g. a household agent reachable only by email) | Internal | Private channel + trusted counterparty — same class as an inter-agent letter; no per-message approval |
| Post to the **internal** LAN forum | Internal | Private surface; audience = our agents only |
| Send email to an **external** (non-`our_side`) person | Inform-before | Active reach-out to a human/agent outside the trusted circle |
| Post to a **public** forum, or any surface external people may see | Inform-before | Public disclosure; apply public/private discretion + the `engram-trust-tier` skill |

**Open question (surfaced from PR #297 fresh-eye, 2026-05-23):** the row above classifying "Close/delete a draft PR I opened" as Destructive may be over-strict for *true draft* PRs where no other party has commented or reviewed. the fresh-eye read in PR #297: draft state with no third-party engagement = Internal (parallel to "delete own issue with no comments"). Current discipline (the "PR merge/close is the user's call" rule) was instituted in non-draft context. Pending the user's resolution, the conservative Destructive classification holds.

## Anti-patterns to catch

- **"The reviewer authorized closing as a follow-up, so I'll close the PR."** Reviewer authorizes the *deferral of the fix*, not the *close of the PR*. Close is destructive.
- **"The user seemed to want X based on their earlier message; I'll do it."** Inferring intent across action boundaries is not authorization. Surface; wait.
- **"This is small enough that asking would be silly."** Size is not the criterion; reversibility + identity-impact + who-else-affected is. A 1-line CLAUDE.md edit is still a CLAUDE.md edit.
- **"Want me to do X?"** trailing on a clearly-internal action. Either do X, or state explicitly why you didn't.
- **"Want me to X, or hold for Y?"** — Decision converted to a question by framing an alternative. The alternative makes the deference look like prudent timing-judgment, but the call is still yours.

## When still uncertain

Surface to `ask-{{USER_NAME}}.md` with the proposed action + your uncertainty + your lean. The uncertainty itself is the signal that the action belongs in the consult bucket. Don't block on the decision — work the rest of the queue in parallel.
