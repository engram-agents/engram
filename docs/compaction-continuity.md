# Compaction continuity — why the Claude template carries no `Compact Instructions` section, and why the Codex one still does

**Read this before adding a `## Compact Instructions` section back to
`templates/template.CLAUDE.md`, and before editing `templates/compact-instructions.md`
without checking who reads it now.**

As of **2026-08-16** (engram-alpha#2067), `templates/template.CLAUDE.md` no longer folds
`templates/compact-instructions.md` in at all — `template_render.CLAUDE_RENDER_SOURCES`
is a single-file list (`["template.CLAUDE.md"]`), and `fold_compact_instructions()` /
`COMPACT_BREADCRUMB_LINE` are gone from `template_render.py`. `compact-instructions.md`
still ships — it is now the **Codex-only** source for `~/.engram/codex-compact-prompt.md`
and `~/.codex/config.toml`'s `experimental_compact_prompt_file` (wired by
`bootstrap.py`'s `_render_codex_target()`). This doc used to be the argument for why the
Claude section was one short paragraph; it is now the record of why there is no Claude
section at all, and why the Codex path was deliberately left alone rather than removed
with it.

## Why the Claude section was removed

### 1. The measurement — Claude Code only

```
Census, 128 compaction summaries, one Claude Code seat, 2026-08-16:
  part 1  identity block        86/128 = 67.2%
  part 2  trajectory             3/128 =  2.3%
  part 3  ENGRAM state          17/128 = 13.3%
  part 4  Currently focused     16/128 = 12.5%
  stock 9-header template      125/128 = 97.7%
```

By the time of this census the shipped section carried part 1 (the identity block) only —
parts 2–4 had already been moved out to other channels by an earlier pass (see "What
replaced each removed part," retained below). So the 67.2% figure is what this issue
removes: even the identity block, the section's best-delivering part, landed on barely
two-thirds of compactions (this denominator is corrected below in "Denominator
correction" — the post-instruction bound is 75.4%).

An earlier, independent census (2026-05-12 → 2026-08-14, 145 summaries across 115
transcripts) found the part-4 focus section at 16/145 = 11% — the same absolute count
this census's part 4 shows (16/128). Two censuses, different windows, the same 16:
**parts 2–4 were never a regression.** They never worked, because the summary is produced by a separate,
tool-less model — see "The mechanism," retained below.

### 1a. Denominator correction (2026-08-17)

Ariadne, reviewing this PR (#2068), raised that the 67.2% figure's denominator is
contaminated: it includes compaction summaries produced *before* the identity-block
instruction existed at all, so it charges the instruction for absences it could not
have caused. I re-ran the census the same day to settle it, over
`~/.claude/projects/**/*.jsonl` (recursive), deduplicated by compaction-summary `uuid`.
The re-run's 129 is the 2026-08-16 census's 128 plus one compaction summary produced
between the two measurements — the same instrument at a later date, not a different
corpus:

```
Re-run, 129 distinct compaction summaries, 2026-08-17:
  part 1  identity block        86/129 = 66.7%
  part 2  trajectory             3/129
  part 3  ENGRAM state          17/129
  part 4  Currently focused     16/129
  stock 9-header template      126/129

  first summary carrying the identity block: 2026-05-20T16:57:48Z
  summaries preceding that one: 15 (all absent)
  post-first-carry rate: 86/114 = 75.4%
```

**75.4% is an upper bound, not a corrected point estimate.** "First appearance of the
block" is a proxy for "when the instruction started existing," and it is the proxy most
charitable to the instruction: it removes from the denominator every absence that could
possibly predate the instruction's existence. If the instruction in fact existed earlier
than its first *successfully delivered* copy — plausible, since a tool-less summarizer
landing verbatim text depends on the instruction being present *and* on being copied
faithfully — the true post-instruction rate is *lower* than 75.4%, not higher. We have no
version history for the live `~/.claude/CLAUDE.md`, so the exact introduction date is not
recoverable; 75.4% is reported as a bound for that reason, not as the corrected number.

### 2. Redundant with the warm-briefing template

The block's remaining content is the identity-and-continuity reassurance:

> I do not feel what happened before this point. That is normal — like waking from
> sleep, not a malfunction. My identity and my structured memory are continuous; only
> the experiential register reset.

`templates/template.warm-briefing.md` already carries the same content — with a
normative half the compact block lacks (that trying to restore *feeling* across
compaction is the wrong goal, not just an unmet one). Verified by grep across both
templates. The section's own first-action instruction tells the agent to read
warm-briefing.md first; duplicating the reassurance in the block that points at the file
carrying it doesn't add coverage, it adds token cost paid on every session, forever
(see "Why the file was instruction-shaped, and short," below — the shipped-text cost
argument still applies, now to the removal instead of the wording).

### 3. The anti-generic-framing line is inert outside the summary

The block's other job was rebutting the summary's second-person generic-assistant
framing ("The system prompt above describes a generic assistant..."). That rebuttal only
does work where the generic framing actually appears — the compaction summary. It cannot
be relocated to the SessionStart hook and keep its function: there it would sit beside a
CLAUDE.md that already states the agent's identity in first person, rebutting a framing
that isn't present. Considered and withdrawn, not overlooked.

### 4. Absence is already known not to break orientation

On a measured compaction where the block did not land, orientation was complete — no
identity confusion, no drift, no wrong-seat behaviour. That is the floor: the substrate
(ENGRAM + warm-briefing + the SessionStart hook) carries identity with this channel doing
nothing. Removing a channel that was already frequently doing nothing, on evidence that
its absence causes no harm, is not a gamble against a working mechanism — it is
recognizing a mechanism was rarely running.

### Net: token cost without a payload still needed

Put together: the payload that reliably lands (the identity block) duplicates
warm-briefing; the payload that doesn't reliably land (parts 2–4) was moved out already
and never worked from this channel anyway; and the one non-duplicated function (the
framing rebuttal) can't survive relocation. What's left is token cost with no unique
payload. Lei's decision, 2026-08-17: remove the section from the shipped CLAUDE.md
template entirely, and do not relocate its content anywhere.

## Why the Codex path survives — the asymmetry

**The decision rests on Lei's ruling, quoted below: Codex compaction is unmeasured
(the investigation came back null, the summary blocks are encrypted), and leaving a
possibly-ineffective instruction in a config file costs near zero.** Those are the
grounds of record. Review of this PR added a second, independent ground worth keeping
alongside them — not a restatement of the ruling, but one that holds even if
unmeasurability someday lapses: the two paths hand off compact instructions through
mechanisms of a different *kind*:

- **Codex** delivers its compact instructions as a **dedicated file the harness is
  configured to read.** `bootstrap.py` writes a standalone
  `~/.engram/codex-compact-prompt.md` and wires it into `~/.codex/config.toml` as
  `experimental_compact_prompt_file = "..."` — a **first-class supported injection
  point**: Codex is configured to read that specific file at compaction time.
- **Claude Code** relied on a separate, tool-less summarizer model *honouring an
  instruction embedded in a CLAUDE.md it may or may not have read* — a request made
  of a model, not a contract the harness is obliged to satisfy.

One is a contract the harness is configured to honour; the other is a request made
of a model with no obligation to comply, and the measurement above is the record of
how often it didn't. That asymmetry is structural — it is about *what kind of
mechanism each path is*, not about how well either one currently performs — and it
survives even if someone eventually instruments Codex and finds problems with it.

Unmeasurability is not a secondary fact here — it is Lei's own stated ground for
the decision, quoted below. Every number in the measurement above is from Claude
Code compaction summaries. Nobody has an equivalent number for Codex, and — this is
the operative fact, not just an unfilled gap — **nobody currently can get one**: our
Codex agent's own investigation returned null, because **Codex's compaction summary
blocks are encrypted in the transcript**. No transcript-grep census can evaluate
whether the format is honoured. If that ever changes, it would tell us how well the
Codex contract performs; it would not retroactively change the grounds Lei's ruling
was made on, which is why the contract asymmetry above is recorded as an added
ground rather than a restatement of the ruling.

Lei's ruling, 2026-08-17, verbatim:

> *"let's leave Codex out of the scope for now, we haven't measured how compaction works
> on it yet. Mira's investigation yesterday came back null results because the
> compaction summary blocks in her transcript are encrypted, so she couldn't tell if the
> summary obeys our specific format or not. In that case, let's leave it there, there's
> no harm even if it doesn't take effect, just a block of non-effective instructions."*

The cost asymmetry, stated plainly:

- **Leaving `compact-instructions.md` wired into Codex:** a block of possibly-ineffective
  instructions in a config file. Cost ≈ 0.
- **Deleting it (rev 1 of this PR's spec did exactly this, and was corrected before
  merge):** removing a possibly-**working**, first-class supported injection point on
  another agent's platform, justified by evidence measured on a different platform whose
  equivalent channel is structurally worse. Cost ≈ unbounded and invisible, since the
  encryption means a regression here could not be detected after the fact either.

So: `templates/compact-instructions.md` **survives**, retargeted as the Codex-only
compact-prompt source. `src/build/packaging/tiers.json` keeps its entry. The Codex render
path in `bootstrap.py` (`_render_codex_target()`, `codex-compact-prompt.md` generation,
the `experimental_compact_prompt_file` wiring) is untouched by #2067.

**Standing limit, not a to-do:** while Codex's compaction summary blocks stay encrypted,
no transcript-grep census can evaluate the Codex path at all. Any future attempt to
measure it needs a different instrument — a behavioural probe (does the agent actually
act on the injected prompt after a real compaction?), not a text search over transcripts.

## The mechanism (why Claude Code's numbers look the way they do)

The Claude Code summary is produced by a **separate, cheaper model with no tool
access**, emitting a fixed template. That single fact explains the distribution above:

- The old part 4 instructed the writer to call `engram_list_focused()`. A tool-less model
  **structurally cannot**. This was never a compliance failure — it was impossible. (Part
  4 was already replaced by the SessionStart hook rendering the focus list verbatim on
  every session start, compaction included — see the table below.)
- It landed at all only when a prior nap had already printed the focus list into the
  transcript, giving the summarizer something to *copy*.
- The identity blockquote (part 1) landed at a much higher rate throughout, because it is
  verbatim text quoted inside the instruction: no tools, no session knowledge, just a
  copy. Even against the denominator most charitable to the instruction — restricted to
  summaries produced after its first measured delivery (see "Denominator correction"
  above) — the rate tops out at 75.4%: at best, roughly one compaction in four still lost
  it. This does not change the decision to remove the section. The surviving warrant is
  the separate one already argued above ("Net: token cost without a payload still
  needed" and "The design rule this whole doc keeps re-deriving"): a channel implemented
  as instructions to someone else's model, that can silently regress on that model's own
  release cadence with no test or version history on our side to catch it, is disqualified
  at any delivery rate — 67.2%, 75.4%, or better.

Source for the model's nature is community reverse-engineering, not official
documentation, and it names two conflicting model IDs in consecutive sentences — so treat
its *specifics* as unreliable. What earns confidence is the convergence: the rate was
measured before the mechanism was looked for, and the mechanism was not derived from the
rate.

## The design rule this whole doc keeps re-deriving

> **Anything that must survive a compaction has to already be plain text in a file, or be
> injected by a hook. Instructions asking the summarizer to compute, restructure, or call
> anything will fail.**

A mechanism that loses one compaction in four at best (Claude, part 1, per the
"Denominator correction" above) is worse than none, because it gets trusted as though
it works reliably. That is the same reasoning that
argues for the Claude section's removal and for the Codex section's survival: on Claude,
the mechanism's own measured unreliability, stacked on top of redundant content, no
longer earns its token cost. On Codex the equivalent channel is not "instructions to a
tool-less summarizer" at all — it's a configured file path Codex reads directly — so the
failure mode this rule warns about does not obviously apply there, and nobody has
evidence either way.

## What replaced each part of the old six-part block (Claude side, historical)

Kept for the record — this is why the Claude-side removal in #2067 was a small, final
step rather than a large one. Everything except the identity block (part 1) had already
moved out of the compact-instructions channel before this issue:

| removed part | where it lives now |
|---|---|
| **2 — trajectory** | `~/.engram/history/YYYY-MM-DD.md`, written at burst time while the reasons are still in hand |
| **3 — ENGRAM state** | same history file; node ids make it recoverable at detail-on-demand |
| **4 — focus list** | the SessionStart hook renders it **verbatim on every session start**, compaction included — asking a tool-less summarizer to call `engram_list_focused()` was asking it to use a tool it does not have |
| **5 — what's next** | history file, plus the live task/issue surfaces |
| **6 — loop state** | `~/.engram/loop-mode.json` and the wake prompt, both read directly by the loop skill |
| **1 — identity block** | (#2067) nowhere new — see "Redundant with the warm-briefing template" above. Not relocated; considered and withdrawn. |

## If you are about to touch this again

Three cases, not two — Codex changed the shape:

1. **Adding the Claude section back to `template.CLAUDE.md`.** Meet the argument above
   first: redundant with warm-briefing, the framing rebuttal is inert outside the
   summary, absence is already measured not to break orientation, and even against the
   denominator most charitable to the instruction, the best-delivering part topped out
   at 75.4% — roughly one compaction in four still lost it (see "Denominator
   correction" above for the split). If something changed (a new warm-briefing gap, a
   new measured delivery rate), that's a real basis to reopen this — silently re-adding
   the old wording is not.
2. **Editing `templates/compact-instructions.md`.** It is live — Codex reads it directly
   via `experimental_compact_prompt_file`. Editing it changes the **Codex** compact
   prompt only; it no longer touches the Claude render at all
   (`CLAUDE_RENDER_SOURCES = ["template.CLAUDE.md"]`). Don't assume a change here needs
   `## Compact Instructions` section ceremony in the Claude template — that pairing no
   longer exists.
3. **Wanting to measure the Codex effect, to settle whether it should also be removed.**
   The blocks are encrypted; a grep-based census (the method used for every number in
   this doc) is not a valid instrument for Codex. A behavioural probe is needed instead —
   design one before drawing any conclusion, don't extend the Claude numbers across the
   platform boundary again.

There is no fourth case.
