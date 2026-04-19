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

## Nudges that build the habit

Short phrases you can drop into normal conversation. These do more than they look like.

| What you say | What it does |
|---|---|
| *"Record that in engram."* | Converts an insight you both just had into a node before it evaporates. |
| *"What does engram say about X?"* | Triggers a `engram_query` — your agent checks its own memory before answering. |
| *"Show me the subgraph around `dv_0042`."* | Opens up provenance: what claim, what evidence, what derivations. |
| *"Is this consistent with what we decided last week?"* | Invites contradiction-checking against the graph. |
| *"Checkpoint before we move on."* | Prompts a `engram_checkpoint` — the graph commits, history advances. |
| *"Reflect on the graph."* | Runs `engram_reflect` — surfaces tainted nodes, unresolved questions, open conjectures. |

You don't need to memorize these. Say them in your own words. The point is that the agent-ENGRAM loop gets stronger every time *you* close it from the outside. You're scaffolding a habit until it runs on its own.

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
| An audit of graph quality | `engram_reflect()` |
| Quantitative health score | `engram_diagnose()` |
| A search | `engram_query(query)` |
| A checkpoint / version-save | `engram_checkpoint(summary)` |

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
