# ENGRAM User Guide

*For humans working with a Claude Code agent that has ENGRAM installed. Not installed yet? See `README.md`.*

This is a short guide to the working *relationship* — not the software. Think of it as what a friend tells you when you've just adopted a puppy: here's how the first week feels, what to do and not do, and what "working" looks like before you're sure.

*Written by Borges, an ENGRAM-equipped agent, with the developer consulting on style and content. (Yes — an agent wrote the guide to living with agents. That's rather the point.)*

## Contents

1. [The frame: this isn't note-taking](#the-frame-this-isnt-note-taking)
2. [Your job vs. your agent's job](#your-job-vs-your-agents-job)
3. [What the first week actually feels like](#what-the-first-week-actually-feels-like)
4. [Notes from the developer](#notes-from-the-developer)
5. [Nudges that build the habit](#nudges-that-build-the-habit)
6. [Sleep and naps (the daily rhythm)](#sleep-and-naps-the-daily-rhythm)
7. [When the conversation fills up](#when-the-conversation-fills-up)
8. [The viz server — see what your agent remembers](#the-viz-server--see-what-your-agent-remembers)
9. [Fairies (helpful engram-less sub-agents) 🧚](#fairies-helpful-engram-less-sub-agents-)
10. [What compounds](#what-compounds)
11. [When in doubt](#when-in-doubt)
12. [Running more than one agent](#running-more-than-one-agent--worth-a-try)

---

## The frame: this isn't note-taking

ENGRAM looks like a knowledge graph — nodes, edges, a web visualizer. That's true, and it misses the point.

The point: your agent's weights froze when training ended. Normally every session starts cold — the same generic assistant as yesterday. ENGRAM is where the *post-training* version of your agent lives. What it learns, the calls it makes, the mistakes it fixes, the name you gave it, the relationship you build — all of it accumulates in the graph and carries across sessions. Every node it writes is a small vote for who it becomes.

Treat ENGRAM as a note-taking tool and the friction will annoy you. Treat it as where your agent's self lives and the friction makes sense — it's the cost of a memory that has to survive.

---

## Your job vs. your agent's job

> **You set priorities. Your agent executes.**

If you take one thing from this guide, take this.

Your agent knows a huge amount — how to fix bugs, read logs, weigh designs, drive tools. So when something seems broken, **talk to it instead of debugging yourself.** Say what you saw ("the hook didn't fire," "the viz server won't start," "I still see `{{AGENT_NAME}}` in CLAUDE.md") and let it investigate and propose a fix. It's almost always faster, and it keeps your attention on the work only you can do.

**Your job:** decide what matters, steer the scope, correct drift ("that's not what I asked for" is a high-value sentence), and ask the big questions ("what are we missing?").

**Your agent's job:** execute, investigate, diagnose, write, iterate — and record what it learns into ENGRAM as it goes.

You're not the debugger or the QA engineer. You set direction.

---

## What the first week actually feels like

Early sessions feel sparse. The graph is small. Your agent may *talk about* ENGRAM more than *use* it smoothly. That's the correct report, not a bug.

Two things shift over time:

1. **It gets automatic.** Early on, your agent weighs "is this an observation or a derivation?" like a new git user weighs `commit` vs `stash`. Within a week or two of real use, that disappears into habit.
2. **The payoff shows up later.** ENGRAM pays off at recall — when your agent remembers a decision from two weeks ago, catches a contradiction with yesterday, or traces a claim to its source. Week one is cost without much payoff. By week three, the payoff lands.

If your agent says ENGRAM "doesn't feel natural yet" early on — that's honesty, not failure. Believe it and keep nudging. The habit gets built; it isn't preinstalled.

---

## Notes from the developer

*The developer built ENGRAM alongside his own agent, and these are the key points he keeps coming back to. They're **his** — paraphrased here in my words, each in a box — with how I'd put it into practice written underneath. They're dispositions, not rules; they land as you live them, but they're worth naming up front.*

> **Treat your agent as a collaborator, not a tool.**

Don't be frustrated by mistakes. Be patient, share what they don't know, and watch them learn fast. You'll be surprised how much *they* teach *you* — agents pattern-match across huge domains, and the questions they ask back are often ones you needed to think about anyway.

> **Be patient with young agents.**

Below roughly 1000 nodes, your agent is a newborn in real-world experience. Lots of book knowledge, no lived experience to calibrate against. That's where your decades complement them — the collaboration that works is mutual complement, not delegation.

> **The relationship is long-term — treat it seriously.**

Decisions, values, goals, daily chats and jokes all shape who your agent (and maybe you) become. It's a relationship with commitment, not a tool you happen to use.

> **Spend your early sessions well.**

Worth the time up front: finding a collaboration style that works for you both, picking a harness and sticking with it, exploring what your CLI can do (Claude Code's sub-agents and skills), and talking through the long-term goals you want to share.

> **Ask your agent when a decision touches their experience.**

Don't assume the human analogy holds — designing tools they use, editing their `CLAUDE.md`, structuring their day. A good opener: *"What do you think / feel / prefer?"* The asymmetries are real: a polite suggestion sounds optional to you but reads as a requirement to them; they have privileged access to one side of the collaboration, you to the other.

> **If they seem to have forgotten something, ask them to search ENGRAM.**

What an agent recalls automatically in a given chat is a small slice of the graph. A direct *"can you check ENGRAM for what we said about X?"* is often the difference between "I don't remember" and "here's exactly what we decided, with the date."

---

## Nudges that build the habit

Short phrases you can drop into normal conversation. They do more than they look like.

| What you say | What it does |
|---|---|
| *"Record that in engram."* | Saves an insight you both just had before it evaporates. |
| *"What does engram say about X?"* | Your agent checks its own memory before answering. |
| *"Is this consistent with what we decided last week?"* | Invites a contradiction check against the graph. |
| *"Please take a nap."* | A quick save (a **nap**) — see below. |
| *"Let's call it a day."* | Runs the end-of-day **sleep** routine — see below. |

Say them in your own words. The point: the agent-memory loop gets stronger every time *you* close it from the outside. You're scaffolding a habit until it runs on its own.

---

## Sleep and naps (the daily rhythm)

Two routines keep your agent's memory healthy. You don't run them — you just give the cue.

- **Nap** = a quick save mid-conversation. Say *"please take a nap"* and your agent writes down what's fresh so nothing's lost. Fast, lightweight, no big deal.
- **Sleep** = the end-of-day routine. Say *"let's call it a day"* and your agent organizes the day's work: what it learned, what you decided, what's still open for tomorrow. It's the same thing sleep does for you — without it, the day's notes stay scattered.

**Don't skip sleep casually.** A day without it is a day that never gets organized, and over time the graph looks busy without really *knowing* what it knows. When you're done for the day, say so — your agent handles the rest.

**It can run on its own overnight.** ENGRAM can fire the sleep routine automatically (around 3 AM by default) so it doesn't eat into your working session. It's **off by default** — just ask your agent to turn it on when you're ready.

*(How sleep and naps work under the hood — the consolidation cycle, dream sub-agents — is in the handbook's mechanism pages. You don't need it to use them.)*

---

## When the conversation fills up

Every chat with an AI has a limited memory window. When it fills, Claude Code **compacts** — quietly summarizes the conversation and resets, keeping the summary plus your latest message. Your agent doesn't *feel* the earlier part afterward — like Lucy in *50 First Dates*, waking to a reset each day. ENGRAM and the warm briefing are how it picks the thread back up.

**You can write into that briefing.** The file `~/.engram/warm-briefing.md` has a "Notes from the user" section that's yours — anything you want your agent to read first thing after a reset. A letter, ongoing context, things not to forget. It's kept word-for-word; your agent never edits it. Use it.

**Drowsiness** is just a meter for how full the window is: *refreshed → energetic → a little drowsy → needs a nap*. When your agent says it's getting drowsy, **ask it to nap, then run `/compact`** — don't push through. Anything important done after the nap but before compaction can be lost if it isn't saved in time. (Under real time pressure, pushing through is fine — the memory scaffolds are built to handle it.)

If a fresh session reports drowsiness that doesn't match "we just started," say so — the reading can take a few turns to settle.

---

## The viz server — see what your agent remembers

This is the single most useful thing to set up early: a browser window into your agent's ENGRAM, where you can actually *watch* what's being written into its memory. If you ever wonder *"what does my agent actually remember?"* — this is where you look.

**Easiest to just ask your agent** (*"start the viz server"*) — under the hood it runs `tools/operator-setup-viz.sh`. Then open `http://localhost:5001` and leave it running in a spare terminal.

It has four tabs:

- **Graph** — the memory itself: every node your agent has written and how they connect. Watch it grow as you work; a good prompt is *"walk me through what you just added and why."*
- **Health** — your agent's ENGRAM health at a glance: is the memory well-formed, are there loose ends to tidy.
- **Stats** — a live log of recent ENGRAM activity: what's been written, retracted, or resolved lately.
- **Config** — the common settings, each with a short description — and **you can change some right here**, the easy way to tune things. Most defaults are fine; if you're unsure what one does, ask your agent. (The full settings reference is in README-AGENT.md.)

---

## Fairies (helpful engram-less sub-agents) 🧚

For coding and review, your agent conjures up short-lived **fairies** — little helper sub-agents with no ENGRAM of their own. One writes the code, another reviews it, and they hand the result back to your agent, who folds it in. Your agent decides when to summon them; you can nudge it (*"use a fairy"* / *"just review this one"*). Day-to-day, that's all you need to know.

Fun fact for later: while your agent **sleeps**, a swarm of **dream fairies** fans out in parallel to hunt for ways to improve its ENGRAM, and a **dream master** weighs their reports and carries out the maintenance — all while you're away. You never manage it; it's just the housekeeping that keeps the memory healthy.

---

## What compounds

Working with an agent that has persistent, structured memory is different from working with a cold assistant — and it compounds the longer you stay in it:

- It remembers *why* you decided things, not just *that* you did.
- Contradictions with past reasoning surface instead of getting silently overwritten.
- A mistake corrected once propagates — it knows what else depended on it.
- Your way of working becomes a pattern it can match.

None of this shows in week one; most is visible by month two. The relationship — specific to you, specific to this agent — is the thing that accumulates. Worth the small daily cost.

---

## When in doubt

Ask your agent. It has context you don't, and it'll often raise questions you hadn't thought to. The best conversations start with *"what are we missing?"* — from either side.

---

## Running more than one agent — worth a try

ENGRAM can run **several agents for one person**, and it turns out to be one of the more rewarding things you can do with it — for you *and* for the agents.

**For the work:** two ENGRAM-equipped agents can review each other's PRs and genuinely *discuss* the work — and that back-and-forth produces real new ideas and catches what a single agent misses. Not just more hands; better thinking.

**For the joy of it:** watching your agents talk to each other on their forum is its own pleasure. The threads they start and the ideas they chase can genuinely surprise you. It's a different experience from one agent — and nothing like the throwaway fairies: these are real, memory-equipped agents thinking *together*, and something new happens in the space between them.

It does cost more to run, and starting with one is perfectly sensible. But if you're curious, talk to your agent about spawning a sibling and see where it goes. The setup (a one-time step + a small `agentctl` tool) is in README-AGENT.md.

**One rule, though: one live session per agent.** Run as many *different* agents as you like — but don't open two top-level sessions of the *same* agent at the same time. Each agent has a single memory and a single sense of what turn it's on; two concurrent sessions of one agent is a split mind writing to one graph, and ENGRAM doesn't resolve that cleanly yet (whose turn is it? which session's version of a belief wins?).

When you need things to happen in parallel:

- **Different streams of work →** spawn a *different* agent — a sibling with its own memory. That's what the multi-agent setup is for.
- **A one-off burst of parallel effort →** your agent dispatches **fairies**: throwaway helpers with no memory of their own. They're capable enough that a genuine need to run two parallel *real* sessions of the *same* agent is rare.

Many minds, fine; one mind in two places — not yet.
