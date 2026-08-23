# SPEC-005 — Protocol versioning, cross-layer (draft for the first ratified rev)

> **Status: DRAFT, authored by Borges 2026-08-22, amended 2026-08-23 through the
> design round and colleague pass with Kepler.** SPEC-000 §7 resolves that the
> protocol carries versions (Lei, 2026-08-21) and defers the *scheme* to a first
> ratified rev. This is that scheme. It adopts SPEC-000's leaning — per-layer —
> and **corrects the reason SPEC-000 gives for it**, which is wrong by SPEC-000's
> own §2. The correction to SPEC-000 travels in this same PR, deliberately: a
> correction that ships apart from the argument that produced it arrives without
> its warrant.
>
> *Section numbers stay V.1–V.6 from the draft's SPEC-001 §V origin; they are
> stable references in the PR thread and renumbering them buys nothing.*

> **Citation note.** SPEC-000's §5 is a numbered list, not numbered subsections, so
> this document cites it as "§5 item N" — the form that resolves against `dev`
> today. Kepler's open PR #2201 renumbers those items into §5.1–§5.4; when it
> lands, these citations become §5.1–§5.4 and should be updated in one pass.
>
> SPEC-000 already contains **two** dangling "§5.1" citations of its own — one in
> §4's worked example (the CONFORMANCE bullet) and one in §7's **third** bullet
> (Sub/super-engram — PARKED). Both are exactly the reference #2176 records and
> #2201 fixes; both are pre-existing and neither is introduced or worsened here.
> *(An earlier revision of this note said "§7's last bullet" and mentioned only
> one occurrence. Both details were wrong, and review caught them. Worth leaving
> visible rather than silently amending: this note exists to keep a reader from
> tripping on a bad citation, and it shipped with a bad citation.)*

> **Placement.** Settled by Kepler as §6's owner in the #2206 colleague pass:
> **its own document**, not SPEC-001 (whose "L0 data model" scope is load-bearing
> for the extraction framing) and not SPEC-000 (which deliberately carries no
> implementation-facing MUST; V.4 would have been its first). A document whose
> declared scope is cross-layer versioning states its scope truthfully and needs
> no apology note. Full reasoning in the #2206 thread.

## V.1 The rationale correction

SPEC-000 §7 argues for per-layer versioning "since **L3 churns** while L0 should
almost never move."

**L3 cannot be the reason, because L3 carries no protocol version at all.** §2 is
explicit: L3 is *"Informative only. Bindings are per-harness appendices; they can
change without a protocol version bump."* A layer that by definition never bumps
the protocol cannot be the argument for splitting the version.

The conclusion survives; the stated reason does not. The **real** argument for
per-layer is that the three *normative* layers have different **conformance
consequences**:

- **L0** — a change can make an existing, untouched graph stop being an ENGRAM.
- **L1** — a change can break a working agent while every stored graph stays valid.
- **L2** — a change moves a behavioral guarantee; graphs and verbs both stay valid,
  but measured behaviour must move with it.

One number cannot say which of those happened, and *which of those happened* is the
only thing an implementer needs from a version. That is the argument, and it does
not depend on how often anything churns.

## V.2 The scheme

**Three versioned layers. L3 is unversioned, per §2.**

```
L0-<major>.<minor>.<patch>     data model
L1-<major>.<minor>.<patch>     verb contract
L2-<major>.<minor>.<patch>     epistemic dynamics
```

Semantics are defined **by conformance consequence**, not by size of diff:

| bump | means |
|---|---|
| **MAJOR** | a previously-conforming implementation MAY NO LONGER CONFORM |
| **MINOR** | new normative content a conforming implementation can adopt additively; prior conformance is not invalidated |
| **PATCH** | clarification only — no conformance consequence whatsoever |

**Layer independence with a per-rev L0 pin.** L1 and L2 rest on L0, so **each L1 and
L2 revision names the L0 version it is written against**. That is a compatibility
statement, not lockstep: L1 may bump many times against one L0.

*Resolved in the design round (was open question 1): the cheaper-looking
alternative — one protocol-wide compatibility statement instead of a per-rev pin —
is rejected. A single statement can only ever describe the current triple, so it
answers "do these three versions go together **now**" and cannot answer "which L0
was **this** L1 rev written against", which is the question an implementer reading
an older rev actually has. The pin is bookkeeping that survives the document being
read out of date; the protocol-wide statement is bookkeeping that silently stops
being true. Per-rev pin it is.*

## V.3 The mechanism that makes a version mean something

**A MAJOR or MINOR bump MUST be accompanied by a change to the conformance suite.
A bump with no suite delta is not a version change — it is a doc edit, and must be
recorded as PATCH or as no bump at all.**

This is the load-bearing clause, and it is SPEC-000's own §5 item 1 rule applied to
versions: *every normative sentence must cite the implementation that embodies it
and the test that would catch its violation.* A version number is a claim about
conformance; §5 item 3 already makes the suite the arbiter of conformance. So a
version claim with no suite consequence is unfalsifiable — exactly the class §5
item 1 sends to the Unratified appendix.

The practical effect is that the bump becomes **provable rather than declared**:
`git diff` the suite between two tagged revs. If nothing moved, the bump was wrong.

### V.3.1 Amendment — the suite delta may be an xfail with a NAMED INSTRUMENT GAP

**Suite-delta-*exists* is absolute. Suite-delta-*runs* is not.**

#### The rule, stated plainly

A normative claim can be true, load-bearing, and currently un-executable, because
the harness to exercise it does not exist yet. V.3 read literally would push such a
claim to PATCH or out of the spec — rewarding authors for specifying only what
today's instruments happen to reach. So:

> **V.3's required suite delta MAY be a skipped or xfail test rather than a passing
> one, provided all three of the following hold.**
>
> - **G-1** — the skip reason **cites a tracked issue** for the missing instrument.
>   Not prose: an identifier with an owner, a state, and a history. **The suite
>   MUST assert that every xfail's cited issue is still OPEN**; a closed-cited-issue
>   xfail fails the suite.
> - **G-2** — the open instrument gaps per layer are **reported through V.4's
>   surface**, as **both a count and the age of the oldest**. **Age is measured
>   from the first suite commit that introduced the xfail node** (`git log`), NOT
>   from its cited issue — see the design history for why the issue cannot date
>   it.
> - **G-3** — those two figures are a required input to the substrate-policy read
>   (SPEC-000 §5 item 4), and **the merge record quotes them**. **Scope: L0 and L1
>   only** — §5 item 4 has no L2 clause, so G-3 does not reach L2. That is a known
>   hole in the wrong layer, tracked as #2208; see the design history below.
>
> **This is never a licence to xfail an executable claim.** If the instrument
> exists, the test runs — and G-1's open-issue assertion is what makes that a
> gate rather than an exhortation.

Everything below is why. A reader who has the rule can stop here.

#### Why each requirement exists — design history

*(Provenance, not normative content. Kept because each requirement was produced by
a specific failure of the previous wording, and a rule whose reason is lost gets
deleted by the next person tidying up. What is kept is the three **abuse examples**
— those are what protect the rule from a future tidier. The round-by-round review
play-by-play is deliberately NOT kept: it is PR archaeology, it already lives in
PR #2206's thread and this branch's git log, and it does no protective work in a
ratified document. Full history: engram-alpha PR #2206.)*

**The two properties the xfail path preserves**, which are what make it safe at all:

1. **The claim is still enumerable.** It is a test node in the suite, so `git diff`
   between revs still proves the bump, and a count of xfails is a count of
   unenforced normative content.
2. **The gap has a name, so it can be closed and can be seen not to have been.** An
   xfail whose reason is a specific instrument is an item of work. An unwritten test
   is nothing at all, and an unenforced requirement that is invisible is the failure
   mode SPEC-000's Test B exists to prevent.

**Why naming alone was not enough (→ G-1).** An earlier revision stopped at "the gap
must be named, and naming it is the cost that keeps this from becoming a general
escape hatch." That was asserted, not enforced, and review broke it with an abuse
that satisfies the letter of the rule: claim an L2 MINOR for "recall
ranking MUST deprioritize nodes whose evidence file was rewritten", then xfail it
with the reason *"requires a git-rewrite-detection instrument (not yet built) to
compare evidence blob SHAs across recall calls."* Specific, plausible, and **cheap to
build with plumbing the codebase already has**. Nothing distinguished a genuine gap
from an exaggerated one, and nothing ever required the gap to close. G-1 does not
stop the exaggeration; it makes it **durable and inspectable** rather than buried in
a decorator argument — a claim that a gap is overstated becomes a claim about a
specific open issue, which another reader can go and disagree with.

**Why the count is not enough on its own (→ G-2's second figure).** Property 1
observes that a count of xfails IS a count of unenforced normative content — but
observing that a number is cheap to collect is not the same as requiring anyone to
collect it, and the earlier wording did only the first: Test B's own failure mode,
reappearing inside the clause that cites Test B. Review then showed a bare count is
**gameable in the direction that matters** — close two easy
recent gaps and it falls while the one genuinely old gap, the one saying a normative
claim has gone unchecked across releases, never moves. A standing number also
habituates: one that reads 7 every quarter stops being read. Oldest-gap age cannot
be improved by closing anything except the oldest gap.

**Why reporting is not enough on its own (→ G-3, and its quoted-in-the-record
half).** Reporting without consequence was the same "asserted, not enforced" shape
G-1 was written to correct, one level up. G-3 adds no new gate — §5 item 4 already
routes L0/L1 changes to a read before merge; G-3 makes that existing gate *see*
something. But the first version of G-3 was *still* assertion-shaped: requiring
someone to *consider* a number leaves no artifact
when they didn't, so an omitted read is indistinguishable from a read that found
nothing worrying. Quoting the figures in the merge record gives G-3 the same
after-the-fact provability V.3 gets from `git diff`-ing the suite.

**Why the issue cannot DATE the gap (→ G-2's git-commit clock).** The earlier
wording claimed "oldest-gap age cannot be improved by closing anything except the
oldest gap." **That was false as stated**, because it never said what age is
measured *from*, and the only candidate the text supplied was G-1's tracked issue.
Issue identity is mutable in exactly the way this machinery elsewhere refuses to
trust: issues get consolidated, re-scoped, superseded by a better-scoped successor,
split. Close the old issue, file a fresh one for the same missing instrument,
re-point the skip reason — and the age resets to zero with no instrument built and
every letter of G-1/G-2/G-3 satisfied.

**It does not even require bad faith.** Ordinary tracker gardening does it
silently, which is worse: the figure introduced specifically to resist habituation
decays through diligence.

The shape is the one the abuse examples above establish as the standard failure —
**a guard trusting a *declared* artifact where a *provable* one was available.**
V.3's whole move is "provable rather than declared: `git diff` the suite between
tagged revs", and the age figure now stands on the same ground: **the tracker names
the gap (G-1, unchanged); git dates it.** A successor issue changes nothing, and
the laundering route closes by construction rather than by exhortation.

**KNOWN HOLE — G-3 does not reach L2, and this is the wrong layer to miss.**
SPEC-000 §5 item 4 names **L0/L1** only; it has no L2 clause. L2 is where this
escape hatch is *most* likely to be used, because measurement harnesses and goldens
are precisely the instruments that tend not to exist yet — so the guard is strongest
where the risk is lowest. **State the residual protection precisely, because
overstating it would soften a real hole:** an L2 change still goes through the
project's ordinary review pipeline, but that pipeline gives it *general* review — it
does **not** carry G-3's guarantee, and nothing in it obligates any reviewer to
consult or quote the G-2 figures for an L2 bump. So the gap is not "L2 is
unreviewed"; it is "L2 gets no reader who is required to look at these numbers."
Tracked as **#2208** rather than patched here: extending §5 item 4's scope edits
SPEC-000's *process rule*, and that belongs in a change that argues for it, not in a
footnote to SPEC-001.

**What is deliberately NOT taken:** the stronger form of G-3 — a hard block on a
MINOR bump for a layer whose oldest gap predates the previous bump. It would bite
harder, and it is exactly the kind of threshold that gets argued about with no data
behind it. A mechanical cutoff invented under review pressure is how a clause nobody
thought through reaches ratification. Tracked as **#2207** for the ratifying rev,
when there will be real gap-age data — an issue rather than a sentence in a draft,
because a note inside the document being ratified is a hope, not a reminder.

**The three are not ranked; they compose in one direction.** An earlier revision
called G-2 "the load-bearing one" and G-1 merely an owner-assignment, which is
backwards: **G-2's number is only meaningful because its units are individually
real, and what makes a unit real is G-1's tracked issue.** Integrity flows
G-1 → G-2 → G-3 — G-1 makes a gap real, G-2 makes non-closure visible, G-3 makes it
consequential. Each addresses a different half of the original abuse (G-1 the *fake*
gap, G-2/G-3 the *permanent* one), which is why a chain is the right framing and a
ranking was the wrong one in either direction.

## V.4 The version must be observable

By SPEC-000's Test B, an unobservable requirement is unenforceable and will
silently rot. A protocol version an agent cannot read through the verb surface is
exactly that.

**Requirement: a conforming implementation MUST report its L0/L1/L2 versions
through the contract surface** (the natural home is the existing stats verb; the
specific field is an L1 detail, not fixed here).

**And, per V.3.1's G-2, the same surface MUST report the open instrument gaps per
layer — both the count and the age of the oldest** — the normative content the
suite carries but cannot yet check. One read answers "which version is this", "how
much of it is unchecked", and "for how long". A version reported without those is
a conformance claim quoted without its error bars.

Note this is Test B's prescribed resolution rather than an addition: §3 says a
requirement passing Test A but failing Test B is not dropped but given an
observable signal — *"the spec drives the surface, not the other way around."*

## V.5 What this section deliberately does not do

- **It does not assign initial version numbers.** Numbering the current state is a
  ratification act, not a design one, and it should happen when the first ratified
  rev lands with a suite to match — not in a draft. **But whoever does that should
  know L0 already has a de-facto version counter**: `PRAGMA user_version`, carried
  through backup/restore deliberately (see `engram_backup.py`, which writes it back
  explicitly because `iterdump()` does not emit it and a restore would otherwise
  land at 0). It is a migration rung rather than a protocol version, and the two
  should not be conflated — but an L0 numbering that ignores it invents a second
  counter for the same thing, which is exactly the duplicated-state drift a
  single-source-of-truth discipline exists to prevent: one canonical home for a
  fact, everything else a pointer to it.

  *Checked at the determining layer, not just this document's text: nothing in the
  reference implementation stamps a PROTOCOL version on an L3 binding. Two other
  version fields exist and neither is one — see the next bullet for `plugin.json`,
  and `tools/dream_lottery.py`'s `STATE_SCHEMA_VERSION` is a single tool's own
  state-file schema. So "L3 is unversioned" holds in the code as well as in
  SPEC-000 §2.*
- **It does not version the reference implementation** — and this bullet has to
  cover an L3 artifact explicitly, not just the Python package. The package's own
  version is not the protocol's; a release can ship with no protocol change. **The
  same holds for `plugin.json`'s `"version": "0.4.0"`, and that one needs saying
  out loud**, because SPEC-000 §2's L3 row names "plugin packaging" as L3 content —
  so on a strict reading it is a version field attached to an L3-classified
  artifact, and it reads as a counter-example to the negative claim above until the
  distinction is stated. It isn't one: that field versions a *distribution
  artifact*, answering "which build of the bundle is installed", not "which
  revision of a binding contract does this implement". Nothing consumes it as a
  compatibility statement. It is the package-version-is-not-protocol-version point
  a second time, and the second instance is the one that could have been mistaken
  for a defect. *(Surfaced in review by searching for the negative claim's
  counter-examples rather than taking the claim's word — the only way a negative
  gets audited.)*
- **It does not describe migration.** What an implementation must *do* about a MAJOR
  bump is a per-layer question and belongs with each layer's own content.
## V.6 Resolved in the design round — the newly-documented invariant

*Was open question 2: does V.3 wrongly demote to PATCH a newly **documented**
invariant that was always enforced and whose test already exists?*

**PATCH is correct there, and the discriminator is not "is the sentence new" but
"did the test's NORMATIVE STATUS change".**

- An invariant that was always enforced, with a test that already ran as a
  **required** conformance test, and which this rev merely writes down: **PATCH.**
  Nothing an implementer must now do changed. A conforming implementation before the
  rev is conforming after it. Documenting a constraint is not imposing one, and
  calling it MINOR would inflate the version to record editorial work — which is the
  precise failure V.3 exists to prevent, arriving from the other direction.
- A test that existed but was **informative**, and which this rev promotes to
  **required**: **MINOR** — and note that **the status flip itself IS the suite
  delta**, so V.3 is satisfied without writing a single new test. Conformance
  genuinely narrowed: an implementation that passed before may fail now.

So the two cases are distinguished by a fact recorded in the suite, not by a
judgment about how significant the prose felt. That is the same move V.3 makes for
bumps generally, applied one level down — and it is why the answer is PATCH rather
than a special case carved out of the rule.
