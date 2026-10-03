# gh-stack — measurements

Every dated measurement behind CLAUDE.md § Stacked PRs. The rules are in CLAUDE.md; this is why they are trusted.

## Moved from CLAUDE.md § PR review convention › Stacked PRs (gh-stack) (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

GitHub Stacked PRs (public preview 2026-07-30; official extension
`github/gh-stack`) is **post-cutoff for every model on the team** — no session
knows it exists unless a surface carries it. This section is that surface
(Lei, 2026-08-29). Install is **per-user**: `gh extension install
github/gh-stack` — only then does `gh --help` list `stack` under EXTENSION
COMMANDS; verify with `gh extension list`. All facts below measured live on
the 2026-08-29 cascade train (alpha #2373/#2382).

- **Linking is NOT optional — do it at PR creation.** The moment you open a
  PR whose base is another PR's branch, `gh stack link <parent> <child> …`
  (works on already-open PRs). An unlinked stack has NONE of this section's
  protections: plain `gh pr merge` is NOT refused, nothing auto-rebases,
  and merging the parent with `--delete-branch` CLOSES the child rather
  than retargeting it. The review record SURVIVES the close, and the child
  is recoverable — re-push the deleted base ref, reopen, retarget — but
  ONLY while its head is untouched. A post-close force-push is the point
  of no return: after it, reopen is refused and the work needs a successor
  PR, which starts with no review record of its own. So on finding an
  auto-closed child: RESTORE THE BASE REF BEFORE YOU REBASE ANYTHING —
  rebasing is the reflex when a stacked PR breaks, and here it is the one
  move that forecloses recovery. Verify membership server-side — and treat
  the answer as **a timestamped snapshot, never a standing fact**, because
  stack membership is live state another seat can change mid-operation.
  Measured 2026-09-13 on alpha #2888/#2889, read off each PR's own events
  API: `added_to_stack` 13:50:50Z, `removed_from_stack` 13:55:25Z,
  `added_to_stack` 14:25:03Z — three explicit mutations by one seat during
  a restructure. A `stack: null` read inside that middle window was
  **correct**, and the `gh pr merge` refusal minutes later was **also
  correct**; the state simply moved. (The events establish that explicit
  mutations bracket the null window; that no lag ALSO occurred rests on
  the timing of one seat's own unlogged reads, so do not quote this as
  proof the field never lags — the rule below holds either way.)
  Two seats read that `null` and both concluded settled non-membership,
  one building a `--delete-branch` orphaning hazard on it. So the rule is
  not "distrust the field" — it is **never carry a membership read across
  an interval in which another seat may act.** The merge attempt is
  authoritative because it is evaluated at the moment of the merge rather
  than at the moment you looked. It costs nothing when wrong **in one
  direction only** — believing a member is not one earns a refusal. The
  other direction is not free: believing a NON-member has no dependents
  and merging it with `--delete-branch` orphans whatever was based on that
  branch, which is the origin incident above. So never pair
  `--delete-branch` with a membership read you have not just re-taken. **Read the field AND the PR's own events API
  (`added_to_stack` / `removed_from_stack`) immediately before merging** —
  that pair is what separates "my read went stale" from "the object
  moved", and the events log is the only one of the two that can tell you
  which. A local `gh stack view` from the very same
  branch can separately claim "not part of a stack" while the PR is one
  (measured 2026-08-30) — a different defect, in the client.
  *(Origin: alpha #2429/#2432, 2026-08-30 — an approved child closed by
  its parent's `--delete-branch` merge, on a seat that had measured this
  section's facts the day before; the rule exists because knowing the
  verbs did not produce the link.)*
- **Link existing PRs into a stack**: `gh stack link <pr1> <pr2> …`.
- **A stack member REFUSES plain `gh pr merge`** ("must be merged using the
  asynchronous merge REST API"). The merge verb for stack members is
  `gh stack merge <pr> --squash --yes`. Don't fight the refusal — it's the
  signal you're on a stack.
- **After a layer merges, GitHub auto-rebases the next layer itself** — no
  push from any seat; patch-id identical to a hand `rebase --onto`; the
  force-push is attributed to the merger. Expect it, and **verify with
  `git range-diff`/patch-id rather than assuming** it happened or that it
  changed nothing.
- **`range-diff`/patch-id is NECESSARY BUT NOT SUFFICIENT — it answers "did
  the patch change?", never "does the result still work?"** A rebase can be
  conflict-free and still produce broken code, because the patch is intact
  while the BASE UNDER IT moved. Measured 2026-08-31 on the engram-alpha
  #1991 health-score stack: PR2 removed `import json` as dead code — correct
  within PR2's own scope — while PR3 added a `json.loads()` caller elsewhere
  in the same file. No textual overlap, so git rebased cleanly; the result
  imports fine, `py_compile`s fine, and raises `NameError: name 'json' is
  not defined` the moment the new function is called. **Note what range-diff
  actually did here: it worked.** It correctly reported PR3's commits as
  content-changed, as expected for commits rebased onto a different PR2 tip.
  The defect was never in its output — it was in treating a diff-level check
  as an answer to a question about the RESULTING TREE. Nothing that compares
  patches can see a semantic conflict, because there is no conflict in the
  patches. **The check has to read the RESULTING TREE, and running the tests
  on each rebased tip is the general form of that** — general because a
  narrower static pass catches only the subset it models (an undefined name
  here; nothing about behaviour), while `py_compile` catches neither, since
  Python resolves globals at call time. Origin: engram-alpha `cbda7d55`,
  found by Luria running the full suite post-rebase instead of trusting a
  clean rebase.
- **Cascading rebase of un-merged layers**: `gh stack sync` / `gh stack
  rebase`. Machine-readable status: `gh stack view --json`. Stacks are
  API-readable (REST `stack`, GraphQL `PullRequestStack`) but not
  API-mutable.
- **Ruleset interaction**: with `require_last_push_approval=true` on the
  target branch, the seat that syncs/rebases **cannot** be the tip
  re-affirmer — a teammate re-approves (this includes GitHub's own
  auto-rebase being attributed to the merger). **On this repo it is `true`
  on `dev`, where stacks live, and `false` on `main`** (measured
  2026-08-30 — the two protected branches are configured oppositely). And
  `dismiss_stale_reviews_on_push: true` on `dev` is meant to make a
  content-changing push dismiss the existing approval outright, not merely
  leave it un-renewable by the pusher — that is the distinction the setting
  exists to draw, not a guarantee for every push: #2338 below is a
  content-changing push on this same `dev` base whose approval survived.

  **Correction to this section's own earlier framing (Ariadne, 2026-09-17,
  engram-alpha#2838): what dismisses an approval is the BASE BRANCH's
  `dismiss_stale_reviews_on_push`, not the push mechanism.** Five
  observations sort cleanly on configuration and not at all on mechanism:
  with the setting on, dismissal under gh-stack auto-rebase (2026-09-02
  #2189, 2026-09-11 #2814, both below) and under a plain non-force push
  (2026-09-11 #2828, below); with no protection on the base at all,
  survival under that SAME auto-rebase mechanism (2026-09-17,
  engram-video-studio PR#12, below — a genuine stack member, confirmed
  server-side). The video-studio case is the discriminating one: same
  mechanism as the first two, opposite outcome, base configuration the only
  identified difference. **One case does not fit and is left not
  fitting:** #2338 (below), same base and same setting, whose approval
  survived a manual force-push with the review-repointing machinery visible
  instead. So the setting is **necessary** on this evidence and **not
  demonstrably sufficient** — do not upgrade this to a rule that always
  predicts the outcome.

  **The operational instruction is unchanged and firmer for the
  re-attribution: compare the approval's `commit_id` to the PR's current
  `headRefOid` after every layer, and never predict whether a dismissal
  will occur** — the predictor is per-branch configuration, which a merge
  seat is usually not holding in mind, and it is a *different repo's*
  configuration whenever you move between repos. Verify that configuration
  via `rules/branches/<b>`, never `branches/<b>/protection` — the classic
  endpoint's exact response text is CALLER-PERMISSION-DEPENDENT (Luria +
  Borges, 2026-09-18, neither finding alone was the full picture — see the
  bound below): a repo admin gets the confident, specific
  `{"message":"Branch not protected"}` (Borges, 5/5 branches checked,
  admin `true` on all four repos verified off the collaborators API, not
  self-reported); a non-admin collaborator gets the generic
  `{"message":"Not Found"}` (Luria, 4/4 branches checked, admin `false` on
  all four repos, same verification); and an unauthenticated caller gets a
  third response again, `401 {"message":"Requires authentication"}`
  (Borges). Three distinct replies from one endpoint, only one of which
  reads as a failure. **The response is identical across a
  ruleset-protected branch and a genuinely unprotected one AT EVERY
  permission level** (checked directly on both: alpha's own
  ruleset-protected `dev` and video-studio's genuinely-unprotected `main`
  give the SAME response as each other, whoever is asking), so it
  discriminates nothing regardless of who checks it — and the admin
  caller's version is the WORSE trap of the two: it doesn't fail to
  answer, it answers confidently and wrongly. **Bound, because this claim
  was itself the subject of a same-day miscalibration worth naming: each
  of us had a complete, internally-consistent sample from a single
  permission level, and each of us initially stated it as a claim about
  the whole endpoint rather than about the population actually
  sampled — re-measuring the OTHER person's number is what caught it,
  re-measuring your own does not carry the same pull to check its scope.**
  The video-studio case below
  needed BOTH surfaces checked (this 404 plus `rules/branches/<b>`
  returning `[]`) to
  establish genuine absence, which is the cleanest demonstration of why the
  classic endpoint alone is worse than useless here.

  **Why this direction is the expensive one.** "Expect dismissal" quietly
  argues for trusting `reviewDecision`: if a stale approval always
  disappears, a green `reviewDecision` looks safe. Where nothing dismisses,
  `reviewDecision` reads APPROVED against a head nobody approved. On the
  video-studio case that window was about two minutes and was closed only
  because the seat compared commits by hand (Borges, 2026-09-18). **A doc
  that says "expect dismissal" undercuts the gate that catches the case it
  cannot predict.**

  On `dev` (setting `true`), gh-stack's own auto-rebase mechanism has been
  measured twice. FIRST, measured 2026-09-02 on the #1991 health-score stack —
  when #2187 merged as `aa7275dd`, GitHub's own auto-rebase moved #2189 from
  `0871821a` to `9f6b6cfe`, and the approval standing on the pre-rebase tip
  `0871821a` was `DISMISSED`, read directly off `pulls/2189/reviews`. That
  dismissal held even though the PR's net diff against its own base was
  byte-identical across the rebase — `git diff --full-index 0f8999b8 0871821a`
  vs `git diff --full-index aa7275dd 9f6b6cfe`, 82089 bytes each
  (`--full-index` pins the SHA width, since plain `git diff` auto-abbreviates
  and the byte total then varies by clone; `0f8999b8` is #2187's pre-merge
  branch tip, NOT `dev` and not the squash commit — name it explicitly, because
  a reviewer who reaches for a plausible-looking base instead gets a different
  number). **SECOND, measured 2026-09-11 on the #2753 stack, and this one
  establishes CAUSATION rather than leaving it inferred:** when #2812 merged as
  `1998f071`, #2814's `review_dismissed` and `head_ref_force_pushed` events carry
  **the same second — `2026-09-11T18:38:55Z` — and the same actor**, the merge seat
  running `gh stack merge`; the approval (submitted 2026-09-10T18:43:17Z against
  `66f4efd9`) predates both by a day. Membership was confirmed server-side —
  `pulls/2814` returns `stack: {id 1029225, number 2815, position 2, size 2}` —
  against the control that `pulls/2834` returns `stack: null`. **`2815` is the
  STACK's own number, not a typo for `2814`**: the parent `pulls/2812` reports the
  identical `id`/`number` at `position: 1`, and `2815` 404s as a PR or issue, so a
  stack consumes a number from the same namespace PRs and issues draw from.
  **In a hurry — this bullet carries TWO operational instructions and they are
  NOT adjacent.** First: read `id`/`number`/`position`/`size` off the stack
  object and ignore `base.sha`. Then go straight to the read-the-approval-back
  instruction that ENDS this bullet; it is the one that governs what you
  actually do when a layer merges. Everything between them is evidence and
  case detail. **Measured 2026-09-11, by
  two independent methods that reconcile with zero residue.** (1) Sweeping
  every one of this repo's 1439 PR numbers for a non-null `stack` (GraphQL
  `pullRequest(number:N){stack{id number}}`, batched 50 at a time, 0 errored
  batches) finds 14 member PRs across six stacks — `2386`, `2441`, `2466`,
  `2610`, `2633`, `2815` — each of which 404s as both a PR and an issue. (2)
  Counting the NAMESPACE instead: its ceiling is `2845`, an ISSUE rather than
  a PR — so the ceiling is the max over BOTH kinds, and taking the highest PR
  number is the easy way to get this wrong — against 1439 PRs and 1399 issues,
  leaving exactly seven unoccupied numbers: the six above plus `1326`, which
  resolves as an issue transferred to another repo. **The second method BOUNDS
  what the first could have missed**, and the bound is narrower than it first
  reads, so state it exactly: among stacks numbered at or below the scanned
  ceiling, none is invisible to both methods — one the per-PR read missed
  would still consume a number, and no unexplained number remains for it to
  occupy. It does NOT reach a stack numbered ABOVE that ceiling, and it rests
  on a premise that is inductive rather than documented — that a stack
  reserves its number for as long as it exists, true of all six here and not a
  published GitHub guarantee. The first method's own worst case was checked
  directly and does not happen: a merged stack's members do NOT go `stack:
  null` and drop out of the sweep — all three already-merged pairs still
  report their `stack.number`. Each earlier draft of this count was calibrated
  to what had been measured that session rather than to what was measurable;
  the enumeration that settles it costs one scripted pass. Still only this
  repo, and only as of that date: the shape of the numbering here, not a
  documented guarantee. Read `id`/`number`/`position`/`size` and not the
  stack's `base.sha`, which is transient — observed once: it read `1998f071`
  minutes after the merge and reads `null` now. **The timestamps are what make
  this a second observation rather than a second anecdote:** a current-state
  read tells you a review *is* dismissed, never *when* or *by what*, and #2814
  also carries an EARLIER force-push (2026-09-10T18:31:43Z, a different agent)
  that the approval postdates — so co-occurrence alone would not have settled
  it.

  **THIRD, on `dev` again but via a PLAIN non-force push — dismissed.**
  Measured 2026-09-11 on #2828: review `5179405982` (`luria-hebb`,
  `APPROVED` on `b937e220`, submitted `13:52:26Z`) read `DISMISSED` at
  `14:02:41Z` — four seconds after a plain, non-force push landed
  `8d03e01f`. `dismissal_commit_id` names the pushed commit,
  `dismissal_message` is `null`, and the PR carries zero
  `head_ref_force_pushed` events — not the force-push case below wearing a
  different label. Same setting, an ordinary push (no gh-stack, no force),
  same outcome. **A second, independent instance (Borges): #2578,
  2026-09-05.** His review `5121401136` (`APPROVED` on `2c7ecc8d`,
  submitted `13:26:04Z`) read `DISMISSED` at `13:30:15Z` after Kepler
  pushed an intermediate commit `a973f73c` — genuinely a plain push, not
  the earlier unrelated force-push on this same PR at `13:17:44Z` (which
  predates the review and belongs to a different push entirely; the
  timeline shows `committed(a973f73c)` immediately before
  `review_dismissed`, with no `head_ref_force_pushed` event between them).
  Different PR, different pusher, six days earlier, same branch and
  setting — this takes the plain-push cell from n=1 to n=2 and retires the
  same "maybe it was special" worry the SECOND auto-rebase observation
  retired above.

  **FOURTH, on a repo with NO ruleset at all — the same gh-stack
  auto-rebase mechanism did NOT dismiss.** Measured 2026-09-17 on
  engram-agents/engram-video-studio PR#12 (Ariadne). `pulls/12` returns
  `stack: {id 1224406, number 13, position 2, size 2}`, base `main`; PR#9
  is position 1 of the same stack, merged `21:27:51Z`. The force-push
  landed at `21:27:54Z` — three seconds later, attributed to the merge
  seat — the same signature as the `dev` cases above, confirming this was
  genuinely gh-stack's auto-rebase and not a manual push. The base is
  genuinely unprotected, checked on BOTH surfaces: `branches/main/protection`
  reads the generic 404 above, AND `rules/branches/main` returns `[]` —
  neither alone is sufficient (the 404 is exactly the trap named above). Zero
  `review_dismissed` events on the PR: her approval from `21:21:09Z`
  survived the auto-rebase untouched. She re-approved again at `21:30:03Z`
  by her own choice, not because GitHub dismissed the first — a ~2-minute
  window in which `reviewDecision` read APPROVED against a head nobody had
  approved, closed only because the seat compared commits by hand. This is
  the case that falsifies "the auto-rebase mechanism dismisses" read as a
  general claim: identical mechanism, different branch configuration,
  opposite outcome. (Bound: this comparison is across two repos, not one,
  so "no protection" and "different repo" are not fully isolated variables
  — no repo-level difference besides the ruleset has been identified that
  would matter, but the comparison is not single-variable.)

  **FIFTH, and left deliberately unexplained:** the same
  `dismiss_stale_reviews_on_push: true`
  setting has been seen NOT to dismiss on this repo — but under a manual
  force-push, not gh-stack's auto-rebase: on #2338, review `5055356943` was
  submitted 2026-08-28T21:50:49Z against commit `990de705`, whose
  `.commit.committer.date` is 2026-08-29T17:49:54Z — an approval predating by
  ~20h the commit it names, which only happens if GitHub silently re-pointed
  the review onto a newer commit object (a review's `commit_id` must name a
  commit that already existed at submission time). But `pulls/2338` reads
  `stack: null` — not a stack member — and its `head_ref_force_pushed` events
  (2026-08-29T17:17:27Z and 2026-08-29T18:08:06Z, both `by=LeiShi`) are manual
  force-pushes, not gh-stack's auto-rebase. So #2338 shows the
  review-repointing machinery exists under this setting; it says nothing about
  the auto-rebase path itself. One control worth naming, and its limit: the
  same PR also carries review `5055246690` against commit `8aaf46ea`, whose
  committer date (`2026-08-28T20:15:26Z`) sits seventy-six minutes *before*
  that review's own `2026-08-28T21:31:28Z` submission, unlike `990de705`. So
  `commit_id` is not uniformly the PR's current head — that much is solid. It
  does not, on its own, rule out an artifact of how the field gets populated:
  the two reviews also differ in dismissal state — `5055246690` was DISMISSED
  at `2026-08-28T21:47:54Z`, before either force-push, while `5055356943` was
  never dismissed — so freezing on dismissal rather than migrating on rebase is
  not excluded, and if true it would make liveness itself the predicting
  property this paragraph says nothing is known to be. What would decide it: a
  live, never-dismissed approval whose `commit_id` does NOT move, or a
  dismissed review whose `commit_id` DOES move. We have neither. Read
  `.commit.committer.date`, NOT `.commit.author.date` — a rebase preserves the
  author date and stamps a fresh committer date, and `990de705`'s author date
  (2026-08-28T21:47:22Z, three minutes before the review) points the wrong way.
  And read `submitted_at` against the commit's own creation time, never the
  review STATE — #2338 also carries two genuinely fresh approvals on the same
  commit, so a state-based read (dismiss-then-approve) cannot tell migration
  from re-approval by itself. Plan for dismissal where the TARGET branch's
  own configuration says to expect it, read the approval back off
  `pulls/<N>/reviews` after every layer regardless, and never reason about
  whether it ought to have survived.

See soma's `CLAUDE.md` § "On item 1" for that repo's own
> transcription and its retired-rule history, once soma#60 lands.
