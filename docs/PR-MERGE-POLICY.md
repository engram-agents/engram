# PR merge policy — what Lei reviews before merge

This repo runs a colleague-review + CI-green merge gate (see `CLAUDE.md` → *PR review convention* and *PR merge-readiness*). This doc answers the **next** question: once a PR is merge-ready, **who merges it** — Lei after a pre-merge read, or the agent team directly.

## The rule (Lei, 2026-06-24)

> "New feature in ENGRAM substrate → I read the PR first. Bug fixes → usually fine to merge without my explicit review."

The discriminator is **substrate concept vs. not** — does the PR change *how ENGRAM represents, scores, or reasons about knowledge?*

## Scope — this policy governs **engram-alpha only**

The substrate-feature gate below is **engram-alpha-specific**. Other repos in the org carry their own **delegated** merge authority, set by Lei per project:

- **umwelt** — merges are **Borges's + Ariadne's** to make (Lei, 2026-07-03: *"umwelt is yours to maintain"*); Lei removed *his* gate on umwelt, including for umwelt substrate-level / new-mechanism changes. The review discipline (reviewer-fairy → counterpart-colleague → CI-green) still holds; only the final merge is the team's, not Lei's.
- **soma** — Ariadne is the principal approver + merger (Lei, 2026-06-21).

So "flip to Lei for substrate" is an **engram-alpha** rule. Do not extend it to a delegated-authority repo; do not relax it inside engram-alpha. When you're unsure which repo's rule applies, the repo you're merging *into* decides.

## ⛔ Lei reads before merge — SUBSTRATE FEATURES

Flip the baton to Lei (the maintainer) and **do not merge it yourself**, even after colleague convergence + CI-green, if the PR:

- adds or changes a **node type or edge type**
- changes **confidence computation** (quote_type / reasoning_type maps, propagation, calibration anchors)
- changes **retraction / supersede / cascade** semantics
- changes **standpoint / falsification / calibration-exposure** machinery
- changes the **goal lifecycle** (states, dormancy, tension)
- changes the **reasoning-type machinery** or adds a reasoning type
- adds a **new MCP tool with conceptual semantics** — a new *epistemic operation*, not plumbing (a backup tool, a stats field, or a CLI is not this)
- adds a **new epistemic mechanism or research instrument** (e.g. #1399's retraction-initiation provenance — a free-will/agency measurement)

These are **conceptual-direction calls**. The implementation can be perfect and the *direction* still wrong — that's what Lei is gating, and it's the part the agent team can't self-certify.

**When in doubt, treat it as substrate and flip to Lei.** The cost of waiting a day is small; the cost of a wrong-direction substrate change that ships is a migration + a graph full of nodes written under the wrong model.

## ✅ Merge after peer review — FIXES + NON-SUBSTRATE

Merge once colleague-converged + CI-green, **no per-PR Lei permission needed**:

- **bug fixes / regressions / test fixes**
- **tooling, infra, CI** — backup, baton, forum, hooks, packaging, the affected-tests gate, install/upgrade
- **docs, brand, i18n**
- **refactors** that don't change a substrate concept

Surface notable ones in the **post-merge digest** so Lei can see what landed and why — but don't gate on them.

## When Lei is away — the standing when-away grant (merge-to-dev vs live-deploy)

**Confirmed and upgraded to a standing rule by Lei, 2026-07-10** (recorded in the commander's graph that day; his words: *"you got my authorization to merge substrate PRs … Unless otherwise stated, when I'm away, you have my authorization to merge on them"*). The 2026-07-08 day-grant interpretation below was confirmed retroactively at the same time; the grant no longer needs to be re-issued per day. "Otherwise stated" means Lei can suspend it for a day or a topic; absent that, it holds whenever he's away.

Substrate merges under the standing grant cover **`dev` only, and the merge only — never the deploy:**

- **Merge-to-`dev` is revertible.** Nothing touches the running substrate until Lei runs the live deploy (`install-local-marketplace.sh` rebuild + `/mcp` reconnect + reload/restart). The **irreversible step — the live deploy — stays Lei's**, always.
- So a substrate PR that is colleague-converged + CI-green, whose *design* was already settled with Lei (or with the architect/commander) and carries **no design novelty at merge time**, may be merged to `dev`. It joins a **merged-but-not-live-verified batch** that Lei deploys + verifies together on return.
- **Log notable substrate merges** made under the grant (post-merge digest / `ask-lei`) so Lei sees what landed and why — an awareness surface now, no longer a per-grant confirmation gate.

The boundary outside the grant is unchanged: substrate PRs with **unsettled design or design novelty at merge time** still go to Lei first, away or not. The carve-out remains merge-not-deploy, dev-only, settled-design-only.

## At merge time — the natural checkpoint

The baton's **flip-to-maintainer** step is where this rule lives in practice:
- **Substrate feature:** the baton flips to Lei and *stays there until he merges* (or, under a live day-grant, to the grantee for a dev-merge per the section above).
- **Fix / non-substrate:** a colleague-converged + CI-green PR can be merged by the team.

So at every `baton flip … <maintainer>`, ask: *is this a substrate concept change, in this repo?* If yes (and no live grant applies), Lei reads first.

**Merge-readiness gate (the other half — see `CLAUDE.md`):** a PR is merge-ready only when review-converged **and** CI-green on the current tip. The enforced non-author-approval gate is GitHub-native (`require_last_push_approval` + ≥1 non-author approval on the current head; Lei, 2026-07-02) — a colleague-converged deputy approval satisfies it; a maintainer cannot self-approve their own PR. Merge-authority (this doc) and merge-readiness (that gate) are orthogonal: a PR needs *both*.

### Baton turn-target corollary

The "flip to Lei" half applies **only to substrate PRs** (in engram-alpha). For a fix / non-substrate PR, the maintainer (pool sentinel) — **not Lei** — is the merge-ready turn-target; the agent team merges it directly after colleague-convergence + CI-green. Setting `turn: lei` on a non-substrate baton is the anti-pattern: it produces a stuck baton that no one can advance, because Lei isn't gating that class (origin: PR #1333, a forum brand rename, whose baton was parked at Lei and stuck after merge — #1442). Match the turn-target to the class: substrate → Lei; everything else → maintainer.

## Gray areas

- **A fix that also subtly changes a substrate behavior** → substrate (treat the conceptual change as the dominant axis).
- **A new tool that's pure plumbing** (backup, stats, a CLI) → not substrate, even though it's a "feature."
- **A docs PR that documents a substrate concept** → not substrate (it's docs); but a docs PR that *defines new semantics* is.
- **A substrate PR during a Lei-absence day-grant** → merge to `dev` is OK under the grant (design-settled, logged); the live deploy still waits for Lei. See "When Lei is away" above.
- Still unsure after this → flip to Lei. Defaulting to the gate is cheap.

## Origin

The 2026-06-24 v0.1.3 merge wave merged #1399 — a retraction-initiation provenance **research instrument** (a substrate feature) — without Lei's pre-merge read. It was additive + dormant, so low-cost — but it's exactly the class that should get his eye first (the "what do we measure about agency, and how is it enforced" question is a direction call, not a code call). This policy is the durable form of that lesson. The per-project scope (umwelt/soma delegation) and the Lei-absence day-grant carve-out were added 2026-07-08 after the policy's own SSoT-file gap — this doc had been authored 2026-06-25 but left unmerged for two weeks — surfaced live during a substrate merge-authority call (engram-alpha #1654) that had to be reasoned from memory because the doc wasn't in the repo.
