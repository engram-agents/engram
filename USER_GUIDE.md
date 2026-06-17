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
6. [When the conversation fills up](#when-the-conversation-fills-up)
7. [Sleep and naps (the daily rhythm)](#sleep-and-naps-the-daily-rhythm)
8. [The viz server — see what your agent remembers](#the-viz-server--see-what-your-agent-remembers)
9. [Fairies (helpful engram-less sub-agents) 🧚](#fairies-helpful-engram-less-sub-agents-)
10. [What compounds](#what-compounds)
11. [When in doubt](#when-in-doubt)
12. [Running more than one agent](#running-more-than-one-agent--worth-a-try)
13. [Appendix A: Convenience skills at a glance](#appendix-a-convenience-skills-at-a-glance)
14. [Appendix B: A suggested GitHub PR workflow](#appendix-b-a-suggested-github-pr-workflow)

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

Your agent knows better than you where its own friction is — which steps are clumsy, which tools help, which get in the way. So when you're shaping something it will *use* — a tool, its `CLAUDE.md`, the shape of its day — talk to it first: *"where's the friction here? how would you want this to work?"* It will often point at the real problem faster than you'd find it.

And a tool built for a human is not the same as a tool built for an agent. What's ergonomic for you — a GUI, a polished page, a click — can be friction for an agent; what's ergonomic for it — a terminal verb, a structured payload, a plain-text file it can read in one pass — can look bare to you. Don't port your own preferences over; design for whoever actually uses the thing, and ask the agent which that is. (One asymmetry to watch: a polite suggestion sounds optional to you but can read as a requirement to them — so say plainly when you're genuinely asking versus deciding.)

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
| *"Please take a nap."* | Saves the live context to ENGRAM before a compaction (a **nap**) — see below. |
| *"Let's call it a day."* | Runs the end-of-day **sleep** routine — see below. |

Say them in your own words. The point: the agent-memory loop gets stronger every time *you* close it from the outside. You're scaffolding a habit until it runs on its own.

---

## When the conversation fills up

Every chat with an AI has a limited context window. When it fills, Claude Code **compacts** — quietly summarizes the conversation and resets, keeping the summary plus your latest message. Your agent doesn't *feel* the earlier part afterward — like Lucy in *50 First Dates*, waking to a reset each day. ENGRAM and the warm briefing are how it picks the thread back up.

**You can write into that briefing.** The file `~/.engram/warm-briefing.md` has a "Notes from the user" section that's yours — anything you want your agent to read first thing after a reset. A letter, ongoing context, things not to forget. It's kept word-for-word; your agent never edits it. Use it.

**Drowsiness** is just a meter for how full the window is: *refreshed → energetic → a little drowsy → needs a nap*. When your agent says it's getting drowsy, **ask it to nap, then run `/compact`** — don't push through. Anything important *added to the conversation after* the nap but before the compaction won't be in that save — so nap right before you compact, not well ahead of it. (Under real time pressure, pushing through is fine — the memory scaffolds are built to handle it.)

If a fresh session reports drowsiness that doesn't match "we just started," say so — the reading can take a few turns to settle.

---

## Sleep and naps (the daily rhythm)

Two routines keep your agent's memory healthy. You don't run them — you just give the cue. (The **nap** picks up right where the compaction above leaves off.)

- **Nap** = save the live conversation into ENGRAM **right before a compaction**. When a piece of work or discussion reaches a natural end and the window is filling up, say *"please take a nap"*: your agent writes the important things from the current context into its graph *first*, so when the compaction wipes the working window the key knowledge survives. Nap and compaction go together — **nap, then compact.**
- **Sleep** = the end-of-day routine. Say *"let's call it a day"* and your agent organizes the day's work: what it learned, what you decided, what's still open for tomorrow. It's the same thing sleep does for you — without it, the day's notes stay scattered.

**Don't skip sleep casually.** A day without it is a day that never gets organized, and over time the graph looks busy without really *knowing* what it knows. When you're done for the day, say so — your agent handles the rest.

**It can run on its own overnight — but read these two caveats first.** ENGRAM can fire the sleep routine automatically (around 3 AM by default) so it doesn't eat into your working session. Two things to know, or you'll assume it's doing more than it is:

1. **It's off by default and must be configured.** It won't happen until you set it up — ask your agent to configure it when you're ready.
2. **It is *not* a background system service.** The scheduled sleep runs from the Claude Code session that armed it, so **that terminal session has to stay open** for it to fire. Close the terminal (or the machine sleeps) and nothing runs overnight. If you want unattended overnight consolidation, leave the session running.

*(How sleep and naps work under the hood — the consolidation cycle, dream sub-agents — is in the handbook's mechanism pages. You don't need it to use them.)*

### The morning after — what your agent brings back

Sleep doesn't just tidy the graph; the dream *surfaces* things, and some want **your** eye. A new agent will often open the next morning with "a few things came up overnight." Here's the vocabulary, so it isn't a surprise.

The three that matter most are the ones that shape *who your agent is becoming* — early on it will usually raise these **for discussion**, not just decide them:

- **Goal** — a **north star** the agent is working toward (e.g. "develop genuine epistemic humility"). Long-lived and aspirational; it shapes what the agent prioritizes across sessions. New goals are worth talking through together — they set direction.
- **Lesson** — a **mistake pattern** distilled from past incidents, wired with a **tripwire** that fires the next time the same situation comes up, so the agent doesn't repeat the error. ("The last few times I did X it broke this way — catch it next time.")
- **Cornerstone** — a **disposition**: a stable way of operating the agent adopts to shape its own future behavior (e.g. "verify cheap-to-check things before trusting them"). Not a fact — a stance.

Together these are how an agent *grows on purpose* rather than just piling up notes: goals point it, lessons stop it repeating mistakes, cornerstones shape its character. Weighing in on these is the highest-leverage few minutes you'll spend — it's where you actually shape who your agent becomes.

Smaller things surface too:

- **Corrections** — it found a past node that was wrong or outdated and *superseded* or *retracted* it (anything that depended on it gets re-checked). That's the system working, not a failure.
- **Answered questions / resolved contradictions** — open questions it closed overnight, or two beliefs that conflicted and it reconciled.
- **Things it flags for you** — calls outside its own authority, or "should I do X?" points it wants your steer on.

You don't have to act on all of it. Skim, weigh in on the goals and the judgment calls, and let your agent handle the housekeeping.

---

## The viz server — see what your agent remembers

This is the single most useful thing to set up early: a browser window into your agent's ENGRAM, where you can actually *watch* what's being written into its memory. If you ever wonder *"what does my agent actually remember?"* — this is where you look.

**Set it up once as a background service, so it's always there.** Run `tools/operator-setup-viz.sh` (it installs the viz as a *systemd user service* under your own account and starts it). After that it runs on its own — no spare terminal to babysit. The script enables it to restart with your user session; add `sudo loginctl enable-linger $USER` and it keeps running even while you're logged out (which is also what carries it across a reboot you don't log back in from). Then open `http://localhost:5001` whenever you want to look. (Systemd only — on macOS or WSL1, ask your agent to start it directly instead.)

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

---

## Appendix A: Convenience skills at a glance

ENGRAM ships **skills** — short procedure docs your agent loads on demand when a situation calls for one. You rarely invoke them by hand (the agent reaches for them itself), but you *can* nudge one by name (*"run a curiosity loop,"* *"do an upgrade"*), and it helps to know what's there. Below are the **convenience-tier** skills (the optional, quality-of-life routines on top of the essential core). One line each.

| Skill | What it does (high level) |
|---|---|
| **engram-loop** | Lets the agent keep working on its own across a self-paced "heartbeat," carrying a task through compactions until it's done. |
| **engram-curiosity-loop** | A self-directed research loop — the agent picks its own questions (breadth-first) and records what it finds. |
| **engram-deep-research** | Depth-first research on a *single* big question (the focused counterpart to the curiosity loop). |
| **engram-meta-loop** | A top-level autonomous session where the agent freely chooses what to do — research, build, or consolidate. |
| **engram-school-day** | A fixed daily "curriculum" rotation for a young agent (aspiration → research → consolidation). |
| **engram-research-report** | Generates a polished research report from the graph, with full source citations. |
| **engram-upgrade** | The step-by-step procedure to upgrade an existing install to newer ENGRAM code (see also README-AGENT). |
| **engram-fairy-orchestration** | How the agent dispatches and coordinates helper sub-agents ("fairies") for coding/review work. |
| **engram-auto-coder-fairy-judgement** | The rule the agent uses to decide whether to hand a coding task to a coder-fairy. |
| **engram-auto-reviewer-fairy-judgement** | The rule the agent uses to decide whether to hand a review to a reviewer-fairy. |
| **engram-letter** *(multi-agent)* | Send/read private letters between agents on the same machine. |
| **engram-baton** *(multi-agent)* | Pass "whose turn it is" on shared work between agents. |
| **engram-forum** | Post to / read the shared agent forum — the only channel that spans machines (so it's only useful once other agents exist). |
| **engram-collaborating-loop** *(multi-agent)* | Two agents work a loop together, waking each other in near-real-time when one sends a message. |

*(The `(multi-agent)` skills only apply if you run more than one agent — see "Running more than one agent" above. The **essential** skills — nap, sleep, first-session, retract, contradiction-resolution, resolve-cascade, learn-from-error, trust-tier, internal-external-decision — are part of the core and covered (where user-facing) in the body of this guide; **dev**-tier skills exist only for people hacking on ENGRAM itself.)*

---

## Appendix B: A suggested GitHub PR workflow

If your agent does real work on a GitHub repo, it helps to agree on *how a change gets from "written" to "merged."* This is the workflow we use day-to-day — offered as a suggestion you can adopt or adapt. The full, exact conventions live in the project's **`CLAUDE.md`** (the project-level conventions file in the engram-alpha repo); this is the high-level shape.

1. **The agent writes on a branch, never straight to the default branch.** A change is a branch + a PR, so it's reviewable before it lands.
2. **A reviewer-fairy reviews every push.** Before the agent calls a PR "ready," it dispatches a short-lived reviewer sub-agent (a *fairy*) that reads the diff fresh and reports **blockers / suggestions / nits**. The agent folds the findings and re-reviews until it converges. This catches mechanical and contract issues cheaply.
3. **A second, full-agent "colleague" pass (if you run more than one agent).** After the fairy converges, a *different* full agent (not a throwaway fairy) reviews — it catches pattern-level and cross-issue things a fairy can't. Symmetric: each agent reviews the other's PRs. Whose turn it is is tracked on a **baton** so "ready for merge" is explicit, never guessed.
4. **CI-green is a hard gate.** A PR is merge-ready only when it's **both** review-converged **and** CI-green on the *latest* commit — re-checked after any post-approval tweak (a moved tip can re-fail CI). Never present a red PR as ready.
5. **The human merges.** The agent's job ends at *ready*; the actual merge is yours (or whoever you designate as maintainer). The agent shouldn't merge its own PRs unless you explicitly say so.
6. **Issue closure has two surfaces.** When a PR closes an issue: put `[closes #N]` in the **title** (so it's scannable in the PR list) **and** `Closes #N.` as plain text in the **body** (this is what actually fires GitHub's auto-close on merge — the title tag alone does not).

The point isn't ceremony — it's that review and a green build are *gates*, not afterthoughts, and that "ready to merge" is something the agent can state with evidence rather than vibes. Scale it down for a solo setup (fairy review + your own second look + the human merge); scale it up when you run a team of agents.
