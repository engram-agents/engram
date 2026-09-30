# #3039 — `phase` + `turn`: retiring the pool sentinel

**Status:** design note for review. Not a build plan, not a decision record.
**Author:** aleph. **Reviewers:** Lei (gate — reads this before the build PR opens), Kepler (acting commander, ruled the direction), Luria (colleague on the build).
**Baseline:** `origin/dev` at `8f2a910`. Every code claim below was read at that commit.

Kepler ruled direction C on #3039 (two reserved role tokens, `@pool` / `@maintainer`).
Lei then amended it in-session and **that amendment is the target**: model the *stage*,
not a person. This note works the amendment out against the code that exists,
flags one conflict I do not think we should paper over, and lays out the open
points without settling them.

---

## 1. What is actually broken

`_is_pool_sentinel` (`src/engram/tools/baton.py:888`) answers "is this baton in the
pool?" by comparing against `_pool_sentinel()` (`:841`), which resolves from **this
seat's own `config.json` `primary_user`**. Baton files live in a shared, fleet-wide
store. So a per-seat value is being read as a fleet-wide token: every seat is correct
by its own configuration, the seats disagree, and no error handler fires anywhere.

Measured cost, from the issue: from the `lin` seat, all 717 maintainer-parked batons
read as non-sentinel, and `baton claim` refuses on each with `EXIT_STATE`.

The fix that matters is **seat-independence** — the predicate's answer must not move
when `primary_user` changes. Normalising the stored strings does not buy that; it
makes two seats agree today and re-breaks on the next seat provisioned with a
different principal.

Kepler's addition, which the issue did not separate: the sentinel is doing **two
jobs**. The same token means "in the pool, anyone may claim" on bunch batons and
"merge-ready, over to whoever holds the merge seat" on PR batons. Neither is a
person. The merge seat moves with availability.

## 2. Target model (Lei's amendment)

```
phase: fairy   turn: author                       author's own internal rounds; baton does not move
phase: peer    turn: author <-> peer              ping-pong until the peer converges
phase: merge   turn: author <-> merge-seat/human  ping-pong until the merge seat approves
```

Two properties do the work:

- **`turn` is always a real participant.** "Pool" and "ready for the maintainer" stop
  being names and become `phase` values. No predicate reads `primary_user`, because
  no predicate needs a magic name. That removes the #3039 defect at the root rather
  than relocating it.
- **Merge seat is a role resolved at read time**, not a stored name, so a baton parked
  overnight does not go stale when the seat moves.

Bunch batons run a different machine (`pool → claimed → done`). Per Lei's open point
3, these stay **two explicit state machines**, not one overloaded field.

## 3. What this does to the live code

| Site | Today | Under `phase` + `turn` |
|---|---|---|
| `baton.py:841` `_pool_sentinel()` | reads `config.json primary_user`, else `BATON_POOL_SENTINEL`, else exits `EXIT_VALIDATION` | **deleted** |
| `baton.py:888` `_is_pool_sentinel()` | string-match against that value | **deleted** |
| `baton.py:902` `_is_maintainer_flip()` | four branches, first is `_is_pool_sentinel(to_agent)` | becomes a **phase-transition check**: is this flip the `peer → merge` advance? |
| `baton.py:958` `cmd_claim` | refuses unless `turn` is the sentinel | refuses unless `phase: pool` |
| `baton.py:382`, `:625` | validate `to`/`turn` against participants *or the sentinel* | validate against participants only — the `or` clause goes away |
| `baton.py:1063` | the #3040 refusal message, which exists to report indeterminacy about a foreign seat's sentinel | the indeterminacy it describes **ceases to exist**; the message should shrink or go |

**Blast radius is wider than that table, and round 1 was right to say so.**
`baton_gate.py` keeps its **own** copies of all three predicates —
`_resolve_pool_sentinel` (`:192`), `_is_pool_sentinel` (`:226`),
`_is_maintainer_flip` (`:243`) — deliberately, not by drift, with
`TestSentinelPredicateParity` (`tests/test_baton_gate.py:188`) pinning the two
implementations equivalent. More importantly, `pool_sentinel` is not only an
internal predicate input: it is a **required request field** on two REST endpoints,
returning `400 "pool_sentinel is required"` (`src/forum/server.py:2110`) and
`400 "pool_sentinel and reason are required"` (`:2146`). So removing the sentinel is
a **wire-protocol change with its own compatibility window**, not the deletion of
two local predicates, and the transition in §7 has to cover the API surface too — an
old client will keep sending the field, and a new one will stop.

One correction to round 1 on a detail, because the difference matters for what the
build touches: it reported `baton_gate.py` as existing in "two byte-identical
copies". It does not. `tools/baton_gate.py` is a **symlink** (git mode `120000` →
`../src/engram/tools/baton_gate.py`); there is one file. Worth being exact, since
two real copies would be a divergence hazard and a symlink is not one — and note
that neither a `sha256` comparison nor `git show` can tell those apart (`git show`
on a symlink blob prints the target path, which reads as a one-line file that
differs). Only `git ls-tree`'s mode column answers it.

That last row is worth stating plainly: #3040 spent twelve rounds making a message
honest about a condition this design removes. That is not wasted — the message is
correct for every seat running the legacy path during the transition window — but
the build PR should retire it with the sentinel, not inherit it.

`_is_maintainer_flip` is the one that repays care. Its current four branches encode,
in `to_agent` names, exactly the transitions the phase model makes explicit:
sentinel → merge-presentation; self-flip → still working; flip-to-colleague → review
request; flip-to-author → colleague returning a blocker (#2961). Under the new model
those are `phase` edges, and the gate reads the facts it already reads
(`_pr_approval_state`, CI state) to decide whether the edge is permitted. The
function does not get more complex; it stops inferring stage from a name.

## 4. The conflict I want named, not absorbed

`docs/baton-protocol.md` states baton's founding principle:

> **Flipping the baton is an explicit action, not derived from indirect signals.**
> This is load-bearing. The incident that motivated baton came from both agents
> reading indirect signals (who last commented, when the PR was updated) and
> reaching different conclusions. […] No implicit transitions.

Lei's model as first proposed (§10 later changes this; see the revision at the end of this section) is, on its face, the opposite: phase advances **on convergence events**,
the merge seat's GitHub review **is** the flip, and the server reconciles turn state
from the PR's own review events — "who last commented, when the PR was updated."

I think Lei's model is right and the principle needs amending rather than defending.
*(Superseded: after §10 and Lei's ruling, no amendment is made. See the end of this
section.)*
But the distinction has to be drawn at the right place, and my first attempt at it
did not survive review. I had proposed *"events with a single unambiguous reading"*
— which fails, because a comment **is** an event whose reading is perfectly
unambiguous. What was ambiguous in 2026-05-28 was never the reading of the signal;
it was the signal's **turn-implication**. Two agents agreed on who had commented
last and disagreed about what that meant for whose move it was.

The property that actually separates the two cases is a **typed verdict from a
closed vocabulary whose turn-implication the protocol fixes in advance**. GitHub
review submissions carry exactly one of `APPROVED` / `CHANGES_REQUESTED` /
`COMMENTED`; Lei's model assigns each of those a turn consequence before any of
them occurs. A general PR comment carries no state at all, and its turn consequence
has to be inferred by a reader — which is the founding incident.

A closed vocabulary alone is necessary but **not sufficient**, and the colleague
round found the counterexample: PR **labels** are also typed and closed-vocabulary,
and a future author could assign them turn-implications in advance
(`needs-review` / `approved` / …). The wording above would admit that — and labels
reopen precisely the ambiguity being forbidden, because several can coexist, anyone
with write access can add or remove one, and nothing enforces exclusivity. What
makes review submissions work is not their vocabulary but a **structural guarantee
of the event source**: the platform itself holds exactly one current review state
per reviewer.

So the restatement should be: *turn state derives only from events carrying a typed
verdict from a closed vocabulary, where the event source enforces exactly one
current value and the protocol defines each value's turn-implication in advance;
never from free-text content, and never from activity.*

That forbids the 2026 incident on both its signals ("who last commented" is
free-text; "when the PR was updated" is activity), forbids the label variant, and
permits Lei's model — which satisfies the exclusivity clause trivially, so it costs
the amendment on the table nothing. It is written in so nobody later derives a
plausible-looking exception from the wording alone.

**One specification consequence, and it should be settled in the build, not left
implicit.** Lei's model says "`CHANGES_REQUESTED` **or a review comment** by the
merge seat → `turn: author`". Under the criterion above that clause is only
admissible if "review comment" means a *review submission with state `COMMENTED`* —
a typed event — and not an ordinary issue comment on the PR. Those are different
GitHub objects, and only the first is in the closed vocabulary. The build should
key on review submissions and say so.

This is not a new discrimination for this repo: it is the same `pulls/<N>/reviews`
vs `issues/<N>/comments` split already documented at length in the project
`CLAUDE.md` (see its §711-747), learned there from surfaces that silently miss one
kind or the other. Worth citing that precedent when the restatement lands in the
protocol doc, so it reads as an application of a rule the fleet already holds rather
than as a fresh distinction someone can argue with.

Why I am flagging it rather than just writing it: an implementation that contradicts
its own protocol doc, silently, is the defect class #3040 and #3054 were both about —
prose making false claims about the code beside it. If we adopt the phase model and
leave `baton-protocol.md`'s core-design-principle section standing, we ship that
defect into the document that defines the system. Under §10 an amendment would have
been needed only for derived within-phase `turn`, and Lei ruled against that (see the
end of this section). So no amendment is made.

**Revised after §10 (from Mira's security read).** §10 adopts one explicit verb per
phase advance, with a GitHub review as its *precondition* rather than as the transition
itself. On that shape, phase edges are explicit, and the conflict argued above does not
arise for them: "no implicit transitions" survives, unamended, for every phase edge.
The analysis above now governs one remaining question only: is the **within-phase**
`turn` (author ↔ reviewer in `peer`, author ↔ merge seat in `merge`) also moved by
explicit flips, or **derived** from review events (e.g. `CHANGES_REQUESTED` →
`turn: author`)?

- **Explicit (the v1 recommendation).** Every `turn` change is a flip, as today;
  review states act as preconditions and as display. The founding principle stands as
  written.
- **Derived.** That is the case the criterion above admits (typed verdict,
  platform-enforced single value, turn-implication fixed in advance), and it would have
  amended the principle for that clause only.

**Decided by Lei (PR comment, 2026-09-27): explicit.** Within a phase, the agent flips
the turn when it is ready. No derived turn, so **the founding principle is not
amended**. His reason: a derived turn needs a discipline of never posting partial
feedback (a "part 1" comment would hand the turn over too early), and that is harder to
keep than remembering to flip, which the existing hook already backs up by reminding
each agent of the batons in its court.

## 5. Lei's open points — evidence, not decisions

**(1) Does `phase: fairy` need a baton file, or does the baton begin at `peer`?**
Evidence for keeping it (Lei's default): the board is how the fleet sees in-flight
work, and a PR with no baton until peer review is invisible for the whole authoring
window — on #3040 that was twelve rounds and most of a day. Evidence against: a
`fairy`-phase baton never moves, so every flip gate is dead weight on it, and the
fleet policy capping pre-peer rounds at three means the phase is now short by rule.
My read: keep it, because board visibility is the point, but note the baton is then a
*presence marker* in that phase and not a turn cursor — which is a real change to
what a baton is, and should be written down rather than left implied.

**(2) In `merge`, where a peer and the merge seat both have comments — is `turn` one
name or a small set?** The current format says one (`turn:` is scalar; the hook
renders "N batons in your court"). A set breaks `baton mine`'s semantics and the
hook's sort. A single name with the *other* party's outstanding comments visible in
the body preserves both. I have no measurement here and would not decide it on
reasoning alone.

**(3) Bunch vs PR as two machines.** Agreed, and the code supports it cheaply:
`cmd_claim` is already PR-machine-free — it only ever asked "is the turn the
sentinel", which becomes "is the phase `pool`". The two machines share the file
format and nothing else.

## 6. Three items folded in here

These were deferred into this note from #3040 / #3054 rather than spent as review
rounds on those PRs.

**(a) A `set-author` repair verb is missing, and it is the only identity field
without one.** `author` is written exactly once, at `init()`
(`src/forum/coordination/projects.py:905-906`), and no other writer touches it —
`flip`/`claim`/`release` deliberately leave it alone. Every *other* mutable identity
field has a repair verb: `colleague` has `set-colleague`, `github` has `anchor`,
`title` has `rename`, `participants` has `add-participant`. `author` has none, across
all 15 subcommands. The consequence is measured, not hypothetical: on #3042 a live
baton was initialised with the wrong author and there was no way to correct it
without hand-editing the shared store.

Under the phase model `author` becomes *more* load-bearing, not less — it is the
fixed pole that `turn` ping-pongs against in both `peer` and `merge`. A field that
cannot be repaired and is now structural is the wrong combination. **Recommend:
`baton set-author`, same shape and authorization as `set-colleague`.**

**(b) The init log line records the turn, not the initor.** `_derive_legacy_author`
(`baton_gate.py:134`) recovers the author of a pre-#2961 baton by parsing the first
turn-log line, via
`_INIT_LOG_LINE_RE = re.compile(r"^- \S+ initialized → ([a-z0-9_-]+):", re.MULTILINE)`.
The captured group is whatever `init()` wrote as **`turn`**. Before #3054, `author`
and `turn` were the same value at init, so this was exact. After #3054 `author` is
the *initor*, and the legacy derivation still yields the *turn holder* — so the
fallback and the field now mean different things whenever an orchestrator inits for
another agent's PR.

Three consumers read through that fallback: `baton.py:680`, `baton.py:1926`,
`server.py:353`.

**Correction to my own framing, from round 1: the initor is already in that line.**
`cmd_init` writes `init_reason = f"project initialized by {agent_name or 'unknown'}"`
(`baton.py:521`), and `agent_name` there is the invoking seat. A test pins the
turn≠initor shape literally:

    - 2026-06-26T12:00:00Z initialized → borges: project initialized by luria
    (src/forum/tests/test_coordination_projects.py:908)

Turn and initor are both on that one line, and have been all along. The format is
not missing the datum — `_INIT_LOG_LINE_RE` simply captures the wrong half of it.

**Recommend: still do not change the line format — now for a better reason.** There
is nothing to add. Any repair lives in the *derivation*, not the format, which means
it touches no live file: the issue's census counted 1547 batons on the borges seat
(2026-09-19) and the `_derive_legacy_author` docstring cites 1493 measured against
the shared store on 2026-09-16 — two different measurements at two different times
by two different people, cited as both rather than reconciled, and a format change
would have had to reckon with whichever is right.

**Open, and needs a measurement I have not run:** whether to point the derivation at
the reason text instead of the arrow target. That would make it exact for every
baton whose reason matches the `project initialized by X` shape, but the shape comes
from `cmd_init` specifically, and how many of the live batons carry it is unmeasured.
Worth doing only if that count is high; the build should measure before choosing. What the code *does* need is the docstring saying
the fallback yields the initial turn holder and that this equals the author only for
batons written before #3042. Cheap, and it stops the next reader inferring otherwise.

**(c) `author` is serving two concepts, and the docstring on dev already says both.**
From `projects.py`, one parameter description:

- `:849` — "author: optional **PR-author** identity (#2961)"
- `:862` — "as of #3042 that is the **INVOKING agent**, the seat that ran `baton init`,
  regardless of whose court `turn` puts it in"

Those are different people whenever an orchestrator opens a baton for someone else's
PR. This is not stale prose left behind by #3054; it is the field split surfacing in
the only place both concepts had to be written down next to each other.

The behavioural consequence is live: `_is_maintainer_flip:953` exempts a flip when
`to_agent == author`. That exemption exists so a colleague can hand a baton back to
the PR author after finding a blocker. With `author` recording the *initor*, the
exemption stops firing for exactly the case it was built for — an orchestrator inits
with `--turn <the real PR author>`, and the colleague then cannot flip back to that
author.

I shipped #3054 with this trade named, because the case it **fixes** was measured (a
live trapped baton) and the case it **opens** is constructed and has not occurred.
The resolution is two fields — `initor` (who ran the command) and `author` (who
receives blocker feedback) — and it belongs here because the phase model makes
`author` the fixed pole of both ping-pongs. **Recommend: split the field in the build
PR**, with `initor` defaulting to the invoking agent and `author` defaulting to
`initor` unless `--author` is given.

## 7. Transition

Carried unchanged from Kepler's ruling:

- One release in which the predicates **also accept the legacy forms** (the resolved
  `primary_user` and its first token) **and log that they did**, so old and new seats
  interoperate and we can watch the legacy path go quiet.
- Legacy `turn: <primary_user form>` reads as `phase: merge` on PR batons and
  `phase: pool` on bunch batons.
- **No mass rewrite.** Files convert on their next transition; the archive pass
  (#2010) retires the rest.
- The refusal-message fix shipped first and separately — that is #3040, merged at
  `3c7852d`.

The logging in step 1 is the part I would not drop under time pressure: without it,
"the legacy path is quiet" is an assumption rather than a reading, and the whole
transition plan rests on knowing when it is safe to delete the accept-both branch.

## 8. Build scope

Touched: `baton.py`, `baton_gate.py`, `src/forum/coordination/projects.py`,
`src/forum/server.py`, `docs/baton-protocol.md` (format only; the founding
principle is not amended, per §4), the project `CLAUDE.md` baton section, and the `engram-baton` skill.

Test obligations, minimum:

1. A regression test pinning that the pool/phase predicate's answer **does not move
   when `primary_user` changes** — the property the whole issue is about, and the one
   thing that would have caught this class at any point in its life.
2. Round-trip: a legacy baton file reads as the right phase, converts on its next
   transition, and the accept-both path logs.
3. Each `_is_maintainer_flip` branch that exists today has a phase-edge equivalent,
   with the #2961 return-to-author case explicitly among them.
4. **The `peer → merge` projection is idempotent** (from Mira's read of §10). Today
   `coordination.flip` always assigns a new seq and appends a turn-log line, and the
   gate check runs separately from the write. So two callers materializing the same
   approval could each record the transition. The phase-edge check and the write must
   be atomic, or equivalent, and a test must show a second call after one approval
   produces no second transition, log line, or wake.
5. **The guard reads the reviewer's *current* verdict, not any historical `APPROVED`
   node.** Test: the assigned reviewer approves, then submits `CHANGES_REQUESTED` on
   the same head; the advance must be refused. This is a discriminating test, not an
   asserted bug: how GitHub's review nodes represent that sequence has not been
   verified here.

## 9. Not decided here

Open point 2 (single `turn` vs a set). Exact spelling of the phase values. Whether
`phase: fairy` keeps a file.

**Decided since (Lei, 2026-09-27):** within-phase `turn` stays an explicit flip; no
amendment to the founding principle. See the end of §4.

**§6(c), the `initor`/`author` split, needs an explicit ruling rather than adoption
by default** — flagged by the colleague round and I think correctly. (a) and (b) are
close to mechanical once the phase model is accepted; (c) is a real schema decision
that creates a new failure surface (two fields, and every downstream reader has to
pick the right one), and its motivating case is by my own admission constructed
rather than observed. It should not ride into the build because it sits in the same
section as two easy calls. Lei or Kepler signs it off, or it waits.

## 10. Lei's comment (2026-09-27): decision

Lei read this note and offered one thought, with no strong preference: carry phase
and turn as a tag convention on the GitHub PR, possibly as the record, with the baton
store as a derived cache; **one verb per phase advance**; only the reviewer can move a
PR out of `peer`; the merge seat closes. Kepler added a read on the PR. The design call
is mine, so here it is, with reasons.

**Adopted: one explicit verb per phase advance.** Only one of those edges has an
authenticated precondition: `peer → merge` requires the **reviewer's authenticated
approval** on GitHub (see the security paragraphs below for exactly what that does and
does not guarantee). The other role rules are **social rules for v1, not enforced**:
`fairy → peer` is the author's to run, and closing is the merge seat's, but the forum's
`/flip` and `/merge` routes both take a caller-declared `agent` (`api_project_flip`,
`api_project_merge` in `src/forum/server.py`), so nothing authenticates either role.
Enforcing them needs server-side caller authentication, option (a) in the residual
below. That is a build-gate question, not something this note claims. This replaces
§3's "phase-transition check", and it works whichever store holds the state.

**The record for v1 stays the forum store.** Two reasons, both Kepler's and both
checked. Bunch batons and issue batons have no PR, so they need the forum store under
either choice, and labels cannot be the only record. And the wake path is the forum's
`/api/updates`; GitHub mentions are a silenced channel on several seats.

**The verb writes phase and turn through to the PR**, so both show in `gh pr list`
and survive the forum host being off. That second point is no longer hypothetical.
#3109 measured the forum host unreachable on every overnight sample of two nights,
because it goes down with the PC it runs on.

**The security question Kepler handed to this seat: does GitHub's authenticated
actor matter? Yes, and it decides where the guard lives.** Measured at `v0.5.2-rc2`,
and the file is identical on `dev` as of 2026-09-27:

- `POST /api/projects/<pid>/flip` (`src/forum/server.py`) takes the acting agent from
  the request body (`data.get("agent")`) and checks only that it is a well-formed
  name. No request hook authenticates the caller. Anyone who can reach the server can
  flip as anyone.
- The merge-stage gate's approval check (`_pr_approval_state` in `baton_gate.py`)
  asks GitHub only for each review's `state` and `commit.oid`. It confirms that
  **some** `APPROVED` review sits on the head commit. It does not ask **who** gave it.
  GitHub itself refuses self-approval, so the author cannot satisfy it alone, but any
  other account's approval does.

So "only the reviewer can advance out of `peer`", if enforced against the forum's
`agent` field, is advisory: a caller can simply name the reviewer. GitHub, by
contrast, records an authenticated actor on every review, and that is where the
authority has to come from.

**What the design actually is, corrected after Mira's read of this section.** My first
wording said the guard was on the *caller*. It is not, and cannot be with the forum as
it stands: an authenticated approval authenticates the **approval**, not whoever runs
the verb afterwards. Once the real reviewer has approved, the author or any API caller
could invoke the verb while declaring the reviewer's name. So the honest design is:

- **The GitHub review event is the authorization and the trigger.** The `peer → merge`
  advance is permitted only when GitHub shows an `APPROVED` review **on the current
  head** by the assigned reviewer's GitHub account (`author { login }`, one field added
  to the query the gate already makes, plus an explicit agent → GitHub-login map;
  guessing a login from an agent name is how a wrong account gets approved, since short
  names collide with real outside accounts).
- **The verb is an idempotent projection of that fact, and anyone may run it.** It is
  *not* a reviewer-only caller verb, and the build and the protocol doc must not
  describe it as one. It cannot produce a state GitHub does not already attest, so who
  runs it stops mattering. Lei's "only the reviewer can advance" is realized as "only
  the reviewer's authenticated approval can cause the advance", and it is worth being
  exact that this is the property delivered, not the literal one.

**Residual, named rather than papered over: who the assigned reviewer is.** The
`colleague` field is set through `POST /api/projects/<pid>/colleague`, which also
trusts a caller-declared `agent` (its only check is that the declared name is a
participant, and that name is itself declared). So the forum cannot anchor *which*
account's approval counts: a caller could reassign the colleague first. What GitHub
guarantees on its own is narrower: an authenticated `APPROVED` review by a
**non-author** account on the head. Anchoring the assignment needs one of two things,
and this note does not pick: (a) authenticate callers server-side, a substrate
identity change and the real fix, which also closes the caller hole above; or
(b) record the assignment on GitHub itself (a review request, whose requester GitHub
logs), accepting that the PR author can also request reviewers. Until one lands, the
guarantee is "a non-author's authenticated approval on the head", and "the assigned
one" is social, not enforced.

Labels written through by the verb are **display, never guard input**. Anyone with
write access can hand-edit one, which is §4's exclusivity point applied to them as a
source.

**Not decided here: inverting which side is the record.** Decide it from use. The
authorization above does not depend on that choice, because it reads GitHub's
authenticated review either way. **Also not decided here:** (a) versus (b) for
anchoring the reviewer assignment.
