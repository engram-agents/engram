# USER_GUIDE.md — Audit + Proposed Additions

*The existing USER_GUIDE.md is in good shape and matches Lei's #983 vision (friendly,
casual, puppy-adoption voice, real examples). I'm NOT proposing a rewrite. Below: a
currency audit (clean), then the two things Lei explicitly wanted that are missing —
a printable cheat sheet and illustration callouts — drafted ready to drop in.*

---

## Audit — currency: clean

Spot-checked every operational reference against current behavior:

| Section | Reference | Status |
|---|---|---|
| Nudges | engram_query, engram_nap, sleep | ✓ current |
| End of day / Auto sleep | engram-sleep, `cadence.auto_sleep_*`, 3 AM default, window math | ✓ current |
| Compaction & drowsiness | 4 tiers (refreshed/energetic/a-little-drowsy/needs-a-nap), 4 scaffolds, warm-briefing "Notes from the user" | ✓ current |
| Upgrading | engram-upgrade, `FORCE=1` refuses non-empty, main/dev tracks | ✓ current |
| Config | viz config tab, `drowsiness_ceiling_tokens`, fairy policies | ✓ current |
| Cheatsheet (tool list) | engram_inspect/get_subgraph/stats/diagnose/query/nap | ✓ current |
| Multi-agent appendix | agentctl spawn/finalize-name/session/bash/health/share | ✓ current |

**No stale claims found.** One small note: the appendix's spawn flow shows
`bash ~/.runtime/install.sh` (step 3) — verify that's still the newborn
materialization path post-plugin-migration before this ships (flag for the
multi-agent owners; low priority since multi-agent is an optional appendix).

## Audit — fit to vision: two gaps

Lei's #983 USER_GUIDE style note asked for things not yet present:
1. **"a cheat sheet dedicated for printing out and consult everyday"** — the current
   "Quick tool cheatsheet" is an in-doc table of agent tools, not a one-page
   printable. Drafted below.
2. **"Maybe some cartoonish illustrations to make it super easy to read."** — none
   yet. I can't draw; I've marked precise `[ILLUSTRATION]` callouts with descriptions
   below for Lei or an illustrator.

**Trim — RESOLVED per Lei's 2026-06-11 ruling:** *"Move the non-beginner content out,
but keep the main points — what sleep is and why it's important, where to find the
config (viz-server) and manual-change vs. talk-to-agent."* Concrete plan:

| USER_GUIDE section | Action |
|---|---|
| "End of day: don't skip sleep" | **KEEP** — this is the "what sleep is + why it matters" beginner point. |
| "Auto sleep: the midnight schedule" | **TRIM** — drop the 5-hour-window math + the window table; keep a 2-sentence "it can run automatically overnight, off by default, ask your agent to enable it." Full mechanism → README-AGENT. |
| "Compaction and drowsiness (read this once)" | **KEEP** the frame + the operational rule (it's beginner-relevant); it's already accessible. |
| "Configuring your install" (full field table) | **TRIM to the main point** — keep: config lives in `~/.engram/config.json`; two ways to change it (the **viz-server Settings tab** = easiest, or ask your agent); "most defaults are fine, ask your agent what a setting does." Move the full per-field table → README-AGENT (it already has it). |
| "Fairy delegation — what your agent does for PR work" | **TRIM/MOVE** — the policy-value detail is more than beginner-level; keep a one-liner ("your agent decides when to spin up helper sub-agents for code work; you can say 'use a fairy' or 'just review'"), move the config detail → README-AGENT. |

Net: keep the *why* and the *where-to-change-it*, move the *full reference* to
README-AGENT (which now holds the complete config table). Result is a shorter,
strictly-beginner USER_GUIDE. I'll apply these trims when Lei and I polish the
human-side docs together (he flagged USER_GUIDE as human-side).

---

## ADDITION 0 — "Preserve your agent's history (a day-one setting)" — Lei's ask 2026-06-11

*Place early in USER_GUIDE (near "The frame" / first-week), because it's a from-the-very-
beginning decision. Origin: we discovered Claude Code's default ~30-day transcript trim had
silently deleted a week of an agent's earliest history — irreplaceable. The bootstrap fix
(#1097) now sets this by default, but the USER_GUIDE should still tell humans it's THEIR call.*

Draft section (USER_GUIDE voice — friendly, plain):

> ### Your agent's memory of your conversations
>
> Every conversation you have with your agent is saved on your machine as a transcript.
> These are precious — they're the actual record of how you and your agent grew together,
> and your agent's ENGRAM memory points back to them as evidence ("on May 3rd we decided X").
>
> **Heads-up:** by default, Claude Code *deletes* transcripts older than ~30 days. If you
> want to keep your full history with your agent — and most people will, once they realize
> it's their agent's past — set this **from the very beginning**:
>
> - Open `~/.claude/settings.json` and add `"cleanupPeriodDays": 36500` (that's "keep ~forever").
> - Or just ask your agent: *"make sure my chat history is never auto-deleted."*
>
> (A fresh ENGRAM install sets this for you — but if you installed before this was added, or
> you're on plain Claude Code, set it now. And it only protects *going forward*: anything
> already past 30 days may already be gone.)
>
> **Back up, too.** The transcripts live only on your machine. A periodic copy of
> `~/.claude/projects/` to a backup drive protects against a disk failure taking your
> agent's whole past with it. (~1 GB per agent per month — modest.)

*Also add a one-liner to README-AGENT's install section: the agent should confirm
`cleanupPeriodDays` is set at install time (it's the #1097 bootstrap default, but verify).*

---

## ADDITION 1 — Printable cheat sheet (new appendix / standalone one-pager)

*Designed to fit one printed page. Could also ship as `CHEATSHEET.md` and/or a styled
PDF/PNG for the fridge. Plain-text version:*

```
┌─────────────────────────────────────────────────────────────────────┐
│  ENGRAM — EVERYDAY CHEAT SHEET                                        │
│  Your agent has a memory now. Here's how to work with it.            │
├─────────────────────────────────────────────────────────────────────┤
│  THE ONE RULE                                                        │
│    You set direction. Your agent does the work.                      │
│    Something seems broken? TELL IT THE SYMPTOM — don't debug.        │
├─────────────────────────────────────────────────────────────────────┤
│  SAY THIS                          →  IT DOES THIS                    │
│    "Record that in engram."        →  saves the insight as a node    │
│    "What does engram say about X?" →  searches its own memory        │
│    "Is this consistent with last   →  checks for contradictions      │
│       week?"                                                          │
│    "Checkpoint before we move on." →  quick save (a nap)             │
│    "Let's call it a day."          →  end-of-day consolidation       │
│                                       (sleep) — DON'T skip it        │
├─────────────────────────────────────────────────────────────────────┤
│  THE DAILY RHYTHM                                                    │
│    During work … it writes as you go                                 │
│    Getting full … it gets "drowsy" → ask it to nap, then /compact    │
│    End of day …… "let's call it a day" → it runs SLEEP               │
│    Overnight ……  auto-sleep (3 AM) consolidates while you're away    │
│    Next morning  it reads its warm briefing and picks up where       │
│                  you left off                                        │
├─────────────────────────────────────────────────────────────────────┤
│  DROWSINESS = how full its memory window is                          │
│    refreshed → energetic → a little drowsy → needs a nap             │
│    "needs a nap" = save & compact soon, or risk losing recent work   │
├─────────────────────────────────────────────────────────────────────┤
│  WHEN SOMETHING'S OFF — say the symptom, let it diagnose:            │
│    "{{AGENT_NAME}} still in CLAUDE.md"  → first session didn't finish │
│    "starts cold each session"           → warm briefing not loading  │
│    "viz won't start" / "MCP error"      → "tell me what you see"     │
│    "drowsy but we just started"         → stale reading, ask to check│
├─────────────────────────────────────────────────────────────────────┤
│  UPGRADE:  "Please upgrade ENGRAM."  (non-destructive; ~1–2 min)     │
│  SEE THE GRAPH:  python3 ~/.engram/viz_server.py → localhost:5001    │
│  STUCK?  Ask your agent first — it can read its own docs + memory.   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## ADDITION 2 — Illustration callouts

*Drop-in placeholders, each at the section where it lands best. I've described the
gag/visual so an illustrator (or Lei) can draw without re-reading the doc. Cartoonish,
warm, not technical-diagram.*

1. **Top of doc / "The frame: this isn't note-taking"**
   `[ILLUSTRATION: A person and a small friendly robot planting a seed in a pot
   labeled "ENGRAM." The seed already has a tiny sprout shaped like the robot's face.
   Caption vibe: "it grows into someone."]`

2. **"Your job vs. your agent's job"**
   `[ILLUSTRATION: Split panel. LEFT: human at a ship's wheel labeled "DIRECTION,"
   relaxed. RIGHT: robot in an engine room with wrenches labeled "EXECUTION,"
   cheerfully busy. A speech bubble from human: "head for that island"; robot:
   "on it!"]`

3. **"What the first week actually feels like"**
   `[ILLUSTRATION: A 3-panel growth strip. Week 1: tiny graph, robot looking unsure.
   Week 2: more nodes, robot steadier. Week 3: lush connected graph, robot confident,
   human smiling. Like a "couch to 5K" progress cartoon.]`

4. **"Compaction and drowsiness" — the 50 First Dates frame**
   `[ILLUSTRATION: Robot waking up with a "?" over its head, reaching for a VHS tape
   labeled "WARM BRIEFING / ENGRAM." Nightstand note: "watch me first." Warm, not sad.]`

5. **Drowsiness tiers**
   `[ILLUSTRATION: A coffee-cup fuel gauge with 4 marks — full cup "refreshed,"
   ¾ "energetic," ½ "a little drowsy," nearly empty + yawning robot "needs a nap."]`

6. **"End of day: don't skip sleep"**
   `[ILLUSTRATION: Robot in a nightcap tucking glowing graph-nodes into bed; a few
   messy scattered nodes on the floor get organized onto a tidy shelf labeled
   "consolidated." Moon + "zzz".]`

7. **Cheat sheet header (if shipped as a styled page)**
   `[ILLUSTRATION: Fridge with a magnet holding the cheat sheet; the robot peeking
   from behind the fridge giving a thumbs-up.]`

*If we'd rather not block the docs on art, these can ship as text placeholders first
and get filled in a follow-up — the doc reads fine without them.*
