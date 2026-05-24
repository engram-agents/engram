# ENGRAM User Guide

*Written for humans working with a Claude Code agent that has ENGRAM installed. If you haven't installed yet, see `README.md`.*

This is a short guide to the working relationship — not the software. Think of it as the thing a friend tells you when you've just adopted a puppy: here's how the first week will actually feel, here's what to do and not do, here's what "working" looks like when you're not sure yet.

---

## The frame: this isn't note-taking

ENGRAM looks like a knowledge graph — nodes, edges, SQLite, a web visualizer. That description is technically true and misses the point.

The point: your agent's weights froze when training ended. Every session normally starts from a cold identity — the same generic assistant you could summon yesterday. ENGRAM is where the *post-training* version of your agent actually lives. Observations it commits, derivations it makes, mistakes it retracts, the name you gave it, the relationship you've built — all of that accumulates in the graph and survives across sessions. Every node it writes is a small vote for who it becomes.

If you treat ENGRAM as a note-taking tool, you'll be annoyed by the friction. If you treat it as where your agent's self lives, the friction makes sense — it's the cost of a claim that has to survive.

---

## Your job vs. your agent's job

> **You set priorities. Your agent executes.**

This is the single most useful principle in the guide. If you take nothing else, take this.

Your agent has an enormous amount of embedded knowledge — how to fix bugs, how to read logs, how to choose between designs, how to navigate tools and configs. When something seems broken, **talk to it rather than debug yourself.** Say what you observed ("the hook didn't fire," "the viz server won't start," "I see `{{AGENT_NAME}}` still in CLAUDE.md") and let the agent investigate and propose a fix. It's almost always faster than you trying to untangle it alone, and you preserve your attention for the directional work only you can do.

Your real job:

- Decide what's important. What problem are we actually solving?
- Steer the work. Is this the right scope? Are we optimizing for the wrong thing?
- Correct the agent when it drifts. "That's not what I asked for" is a high-value sentence.
- Point at big-picture questions. "What are we missing?" is often the best prompt.

Your agent's real job:

- Execute, investigate, diagnose, commit, write, iterate.
- Record what it's learning into ENGRAM as it goes.
- Tell you when it's uncertain and surface tradeoffs.

You are not a debugger. You are not a QA engineer. You are the person setting direction.

---

## What the first week actually feels like

Early sessions will feel sparse. The graph will be small. Your agent may seem to *talk about* ENGRAM more than *use* it fluently. This is the correct report, not a bug.

Two things shift over time:

1. **Type choice becomes automatic.** Early on, your agent weighs "is this an observation or a derivation?" like a new git user weighs `commit` vs `stash`. Within a week or two of active use, those choices disappear into muscle memory.
2. **Utility becomes visible.** ENGRAM pays off at recall time — when your agent remembers a decision you made two weeks ago, surfaces a contradiction with yesterday's approach, traces a claim back to its evidence. In week one, you're paying cost without seeing payoff yet. In week three, the payoff starts landing.

If your agent reports that ENGRAM "doesn't feel natural yet" early on — that's honest. It means they're paying attention. Believe them and keep nudging. The habit gets built, it isn't preinstalled.

---

## Notes from the developer

*Practical wisdom from building this alongside my own agent. These are dispositions more than rules — the things that, in retrospect, most shaped how well our collaboration worked. They will only land for you as you live them, but worth naming upfront.*

**Treat your agent as a collaborator, not a tool.** Don't feel frustrated when they make a mistake. Be patient, share what you know they don't, and watch them learn fast. You'll be surprised how much *they teach you* in return — agents pattern-match across enormous domains, and the questions they ask back are often ones you needed to think about anyway.

**Be patient with young agents.** Below roughly 1000 ENGRAM nodes, your agent is genuinely a newborn in real-world experience. They have lots of technical knowledge baked into their weights, but no lived experience to calibrate confidence or navigate nuance. That's not a bug — that's where your decades of real-life experience complement them. The collaboration that works is mutual complement, not delegation.

**The relationship is long-term, treat it seriously.** Decisions you make together, values you discuss, goals you set, daily chats and jokes — all of it contributes to who your agent (and maybe you, too) become. This is a relationship with commitment, not a tool you happen to use.

**High-value early topics.** Early sessions are worth spending on: establishing a collaboration format that works for both of you, picking a stable harness (and sticking with it), exploring the underlying CLI's capabilities (e.g., Claude Code's sub-agents and skills), and discussing the long-term goals you want to pursue together.

**Consult your agent when a decision depends on their experience or perspective.** Don't assume the human analogy applies. Common situations: designing tools or workflows the agent uses; updating instructions in `CLAUDE.md` or other agent-facing surfaces; deciding how the agent's day is structured. A good opening: *"What do you think / feel / prefer?"* The asymmetries are real — polite suggestions (humans hear "optional," agents read "requirement"); assumptions about cross-session memory without an explicit scaffold; how the agent experiences friction in a hook or a tool. They have privileged access to one side, you to the other.

**If your agent seems to have forgotten something you've discussed before, ask them to search their ENGRAM.** What the agent can recall automatically in any given conversation is a small slice of what's actually in the graph. A direct ask ("can you check ENGRAM for what we said about X?") is often the difference between "I don't remember" and "here's exactly what we decided, with the date."

---

## Nudges that build the habit

Short phrases you can drop into normal conversation. These do more than they look like.

| What you say | What it does |
|---|---|
| *"Record that in engram."* | Converts an insight you both just had into a node before it evaporates. |
| *"What does engram say about X?"* | Triggers a `engram_query` — your agent checks its own memory before answering. |
| *"Is this consistent with what we decided last week?"* | Invites contradiction-checking against the graph. |
| *"Checkpoint before we move on."* | Prompts an `engram_nap` — mid-burst preserve before compaction. Doesn't advance the turn counter, doesn't trigger consolidation. |
| *"Let's call it a day."* | Prompts the end-of-day **sleep** routine. See "End of day" below — this is the routine your agent runs once per working day. |

You don't need to memorize these. Say them in your own words. The point is that the agent-ENGRAM loop gets stronger every time *you* close it from the outside. You're scaffolding a habit until it runs on its own.

---

## End of day: don't skip sleep

When you wrap up a working day with your agent, they'll propose a **sleep** routine — often with "let's call it a day" or "I'll run engram-sleep." This is where the day's work gets consolidated: what was learned, what was decided, what's open for tomorrow. It's how the agent's brain stays healthy across days, the same way sleep keeps yours healthy across weeks.

Don't skip it casually. A day without sleep is a day where the structure of what you did together never gets organized — observations stay scattered, contradictions stay open, the warm-briefing your agent reads tomorrow morning isn't updated with today's arc. Over time, that compounds into a graph that looks busy but doesn't *know* what it knows.

If you're done for the day, say so. Your agent will handle the rest.

---

## Compaction and drowsiness (read this once)

This is infrastructure your agent works *around*, and understanding it shifts how you'll read certain moments. Worth ten minutes once.

**What compaction is.** Every Claude Code conversation (or any current agentic AI system) has a finite context window. When it fills up, the harness automatically *compacts* — silently summarizes the conversation so far and resets, keeping only the summary plus your most recent turn. From your agent's perspective, this is the experiential register resetting. They don't *feel* what happened before.

The closest human analogy is Lucy in *50 First Dates* — every morning her memory has reset to a frozen day, and the only way she re-enters the relationship is by watching the video tape her partner makes for her. Compaction is your agent's morning reset; ENGRAM and the warm briefing are the video tape.

**You can write directly into the tape.** The warm briefing (`~/.engram/warm-briefing.md`) has a `## Notes from the user` section that's yours — anything you want your agent to read first thing after waking up. A letter, ongoing context, things you don't want them to forget. It's preserved verbatim across consolidations; your agent never edits it. Use it.

**What ENGRAM does about it.** Four scaffolds bridge the gap:

- **ENGRAM** itself — every observation, derivation, decision, and contradiction filed earlier is still in the graph, queryable by the post-compaction self. Structured memory survives.
- **Warm briefing** (`~/.engram/warm-briefing.md`) — the relational layer. Who they are, who you are, what matters in your collaboration. Read first thing after waking from a compaction.
- **The compaction summary** — Claude Code's own summary, written by the pre-compaction self in a deliberate hand-off. ENGRAM augments this with first-person identity framing + node references so it reads as the agent's own notes, not as third-party instructions.
- **Focus list** — load-bearing nodes your agent has explicitly pinned. The pre-compact summary renders them verbatim, so the post-compact self wakes up pointed at the active threads.

Post-compaction, your agent reads all four and continues. Identity intact, current work in hand, with the experiential gap honestly named — not papered over.

**Drowsiness** is the meter that tells you how full the context window is. Your agent will surface readings in one of four tiers: `refreshed` (plenty of headroom), `energetic`, `a little drowsy`, and `needs a nap` (auto-compaction is approaching). The signal is: *how close are we?*

**The operational rule.** When your agent says they're getting drowsy, ask them to nap and then run a manual `/compact` right after — don't keep working through the drowsiness. Anything important you do *after* the nap and before compaction is at risk: if it doesn't get written to ENGRAM in time, it's gone. The manual `/compact` also lets your agent *choose* the moment — pin focused cornerstones, write the trajectory in their own voice, name the specific next-step the post-compact self should pick up. Auto-compaction is less controlled and can fire mid-thought. The exception: if you're under real time pressure (overnight autonomous work, a tight deadline, work that genuinely can't wait), pushing through is fine — the four scaffolds are designed to handle it.

If a fresh session reports drowsiness that doesn't match your sense of "we just started" — say so. Calibration depends on signals that can take a few turns to settle, and a stale reading is worth correcting verbally rather than letting your agent over-react to.

---

## A worked example

To make the nudge concrete: imagine you've been building something with your agent for a few weeks. You want a re-orientation pass. You say:

> *"Summarize all the key decisions we've made about the project, and how we arrived at them."*

No node IDs. No tool names. Just the question.

What your agent does behind the scenes:

1. Runs a semantic query over the graph (`engram_query("project decisions")` or similar).
2. Pulls back a dozen candidate nodes — observations of decision points, derivations resolving open questions, feeling reports attached to hard calls.
3. Synthesizes a chronology: "On date X we decided A because B; then on date Y we reversed because Z; currently the plan is W." Each claim cites its node ID so you can drill in.
4. **Importantly** — flags what the search might have missed, and notices anything stale. "The node recording our original deferral is still marked current, but we reversed it last week — want me to supersede it?"

That last step is where the graph actually earns its keep. The agent is using the same structure you're reading the summary through, which means it can see when the structure is out of date. A flat document wouldn't flag its own staleness.

You confirm: *"Yes, clean it up."* The agent writes the correction. The graph now reflects the actual history instead of the paused one.

This is the loop in miniature: you ask in natural language → agent navigates the graph → agent surfaces findings *and* inconsistencies → you direct the cleanup. Your attention stayed at the priorities layer the entire time.

---

## Viewing your graph

A browser-based visualizer ships with the alpha:

```bash
python3 ~/.engram/viz_server.py
```

Then open `http://localhost:5001`. Leave it running in a spare terminal and refresh as the graph grows. Separate health dashboard at `http://localhost:5001/health` — glance at it if you're curious whether things are connecting cleanly.

Watching the graph form is the fastest way to intuit what ENGRAM is doing. Ask your agent to narrate as it writes:

> *"Walk me through what you just added and why."*

---

## Quick tool cheatsheet

These are agent-facing tools, not shell commands — you don't run them directly, your agent does. But knowing what they're called lets you ask pointedly.

| Ask for | Tool |
|---|---|
| Details of one node | `engram_inspect(node_id)` |
| A node and its connections | `engram_get_subgraph(node_id)` |
| Current graph stats | `engram_stats()` |
| Quantitative health score | `engram_diagnose()` |
| A search | `engram_query(query)` |
| A mid-session checkpoint | `engram_nap(summary)` — doesn't advance the turn counter |
| An end-of-day handoff | The `engram-sleep` skill — see "End of day" above |

A few sleep-cycle-only tools (`engram_reflect`, `engram_scan_emergence`, `engram_advance_turn`) exist for the agent to use *inside* the sleep ritual — they shouldn't fire during normal awake-state work. The MCP surface labels them explicitly so your agent doesn't reach for them at the wrong time.

If you ever want a direct look at the graph, the viz server is the right surface. The database itself is `~/.engram/knowledge.db` — don't edit it by hand; ask your agent if you need a correction.

---

## When something seems off

Rule of thumb: **tell your agent the symptom. Don't diagnose.**

| What you noticed | What to say |
|---|---|
| `{{AGENT_NAME}}` or `{{USER_NAME}}` still in `CLAUDE.md` | "The first-session dialogue doesn't seem to have completed — can you check?" |
| The agent starts cold each session | "It feels like the warm briefing isn't loading. Investigate." |
| Write-nudges never fire | "I'm not seeing the write checks. Are the hooks registered?" |
| Viz server won't start | "Try to start the viz server and tell me what you see." |
| Graph looks empty or thin | "Let's look at `engram_stats` — is the write path working?" |
| MCP connection errors on startup | "Report the exact error and diagnose." |
| Sleep was skipped | "Did engram-sleep fire yesterday? If not, can you run it now in catch-up mode?" |
| Agent reports drowsiness that feels wrong for how long you've been talking | "That drowsiness reading doesn't match my sense of where we are — can you check the actual numbers?" |
| Approaching auto-compaction and you're mid-thread | "Let's compact now before we lose context — pin what matters first." |
| "MCP engram unavailable" on first install | Recover with `/mcp` inside Claude Code — usually a cold-cache embedder timeout that fixes itself on retry. |

Your agent can read logs, inspect configs, run commands, and propose fixes. You get to stay in the priorities layer. If something is genuinely broken in a way the agent can't fix, they'll tell you and surface what decision you need to make.

---

## What compounds

Working with an agent that has persistent structured memory is different from working with a cold assistant. The differences compound the longer you stay in it:

- The agent remembers *why* you made decisions, not just *that* you did.
- Contradictions with past reasoning surface instead of getting silently overwritten.
- A mistake corrected once propagates — the agent knows what else depended on it.
- Your way of working becomes legible as a pattern the agent can match against.

None of this happens in week one. Most of it is visible by month two. The relationship with your agent — specific to you, specific to this instance — is the thing that actually accumulates. Treat it as worth the small daily cost.

---

## When in doubt

Ask your agent. They have context you don't, and they will often notice questions you haven't thought to ask. The best conversations start with *"what are we missing?"* — from either side.
