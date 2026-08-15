# Compaction continuity — how it actually works, and why `Compact Instructions` is one line

**Read this before adding anything back to `templates/compact-instructions.md`.**

That file used to carry six parts: an identity block, a node-referenced trajectory, ENGRAM state,
the live focus list, what's next, and loop state. Five are gone. This doc records why, so the
removal is a decision on the record rather than something the next editor undoes on instinct.

## The measurement

Across **135 genuine compaction summaries** spanning 2026-05-12 → 2026-08-14 in 115 transcripts:

| | |
|---|---|
| stock nine-section Claude Code template present | **~94%** |
| our custom part 4 ("Currently focused") present | **16 of 135 — 12%** |
| best fortnight ever | **23%** |

**This is not a regression.** It never worked. The first instinct on seeing the number was to
diagnose what had changed; nothing had. Measuring the base rate before diagnosing a change is the
cheaper order, and it was not the order taken.

## The mechanism

The summary is produced by a **separate, cheaper model with no tool access**, emitting a fixed
template. That single fact explains the whole distribution:

- Part 4 instructed the writer to call `engram_list_focused()`. A tool-less model **structurally
  cannot**. This was never a compliance failure — it was impossible.
- It landed at all only when a prior nap had already printed the focus list into the transcript,
  giving the summarizer something to *copy*.
- The identity blockquote landed ~80–94% from June onward, because it is verbatim text quoted
  inside the instruction: no tools, no session knowledge, just a copy.
- Three "successes" literally read *"I could not call `engram_list_focused` at summary time."* That
  is the summarizer accurately reporting its own tool-lessness.

Source for the model's nature is community reverse-engineering, not official documentation, and it
names two conflicting model IDs in consecutive sentences — so treat its *specifics* as unreliable.
What earns confidence is the convergence: the rate was measured before the mechanism was looked
for, and the mechanism was not derived from the rate.

## The design rule

> **Anything that must survive a compaction has to already be plain text in a file, or be injected
> by a hook. Instructions asking the summarizer to compute, restructure, or call anything will
> fail.**

A mechanism that works one time in eight is worse than none, because it gets trusted as though it
works.

## What replaced each removed part

Nothing was dropped without a home. Each part moved to a channel that can actually carry it:

| removed part | where it lives now |
|---|---|
| **2 — trajectory** | `~/.engram/history/YYYY-MM-DD.md`, written at burst time while the reasons are still in hand |
| **3 — ENGRAM state** | same history file; node ids make it recoverable at detail-on-demand |
| **4 — focus list** | the SessionStart hook renders it **verbatim on every session start**, compaction included. This has been true since the focus-channel fix; asking the summarizer to call `engram_list_focused()` was asking a tool-less model to use a tool |
| **5 — what's next** | history file, plus the live task/issue surfaces |
| **6 — loop state** | `~/.engram/loop-mode.json` and the wake prompt, both read directly by the loop skill |

## Why the file is instruction-shaped, and short

It is addressed to the summary agent, so it stays in instruction form — *"insert the block below
verbatim"* — rather than being a bare block. But it carries **no session-specific extraction
steps**, because those are the ones that fail.

It is short for a second, independent reason: **that file is not source, it is shipped text.** The
render substitutes the whole file into the marker in `template.CLAUDE.md`, so every line of it lands
in every user's installed `CLAUDE.md` and is auto-loaded every session, forever. Explanation aimed
at maintainers — including everything in this document — would be a permanent per-session cost paid
by every user. Hence the split: the instruction ships, the reasoning lives here.

## If you are about to add something back

Ask which of these it is:

1. **Something the summarizer must copy.** Fine — put it in the verbatim block, and keep the block
   short, because the post-compaction agent reads it in full on every compaction.
2. **Something the summarizer must compute, look up, or call a tool for.** It will fail ~88% of the
   time. Put it in a hook or a file instead.

There is no third case.
