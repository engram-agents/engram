# PR issue closure — measurements and incidents

Evidence behind CLAUDE.md § PR — issue closure.

## Moved from CLAUDE.md § PR — issue closure (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

**Empirical anchor** (2026-05-31, Borges + Ariadne; #572):
- 7/7 sampled title-only PRs (`[closes #N]` in title, no body keyword): all had
  `closingIssuesReferences=[]` and would not have auto-closed on merge.
- Body-keyword PRs: `closingIssuesReferences=[N]` populates correctly.
- Backwards-compat sweep: 0 issues leaked open — the 7 title-only PRs' issues
  ARE closed, but close events show **human actors** with `closer_pr: null`.
  Maintainers have been manually closing issues during merge waves, which
  silently masked the gap. Risk is purely forward-looking; no remedial cleanup
  needed. **The amendment is preventive, not remedial.**

**For multi-PR / EPIC ships:** earlier PRs may reference the issue number in
title or body, but **do not** use a closing keyword in either surface. Only the
LAST PR in the series uses BOTH `[closes #N]` in title AND `Closes #N.` in
body. Don't rely on a "wrap-up PR I'll file later" — that PR rarely materializes
(empirical: 7 of 11 stale-but-addressed candidates in the 2026-05-28 backlog
scan followed this pattern, leaving their parent issues open indefinitely).

**A `[part of #N]` PR deliberately never auto-closes #N — which means the
epic body is the one artifact in this whole pipeline that nothing points at**:
not the auto-close parser, not CI, not a review gate (#2617, 2026-09-06).
Two epics were found reading as open to-do lists in one sweep, after every
one of their workstreams had already shipped — work units got assigned off
the stale body before anyone checked the actual PR history. The convention,
adopted from that incident: **a `[part of #N]` PR ticks its own workstream
line in the epic body (checkbox, or a `— landed as #M (sha)` suffix) as part
of merging** — the same shape as "a fairy round that isn't transcribed to the
PR didn't happen": a workstream that isn't ticked stays an imperative, no
matter how done the work actually is. **The knowledge of which workstream
line a given PR corresponds to sits with the PR's own author, not with
whoever merges it** — so the author names that line in the PR's own body
(e.g. "ticks #N's `WS3` line"), and the merge seat ticks the line the PR
itself named rather than reverse-engineering it from the epic. This is
discipline, not a mechanical gate (tracked as a follow-up only if the
convention gets walked past).

**Backticks are the one exception** (measured on PR #1191): GitHub's
auto-close parser ignores keywords inside inline-code spans, so a backticked
`` `Closes #M` `` does **not** populate `closingIssuesReferences` and will **not**
auto-close on merge. Two consequences: (a) an *intentional* `Closes #N` must be
**plain text** — a backticked one silently fails to auto-close (caught pre-merge on
#1191 by the `closingIssuesReferences` verify step below); (b) the verification grep
below is a plain regex that **does** match inside backticks, so it conservatively
over-flags backticked refs — a backticked match is safe-from-the-real-parser, so
confirm it rather than treating it as an auto-close risk.

**Fenced code blocks are the SAME exception, measured separately (#2630,
2026-09-06):** a closing keyword inside a triple-backtick fence or a tilde
(`~~~`) fence also does **not** populate `closingIssuesReferences` — a
throwaway PR with `Closes #N.` inside each fence type read empty on 9
consecutive `closingIssuesReferences` queries spanning ~140s (well past the
documented propagation lag in either direction). This settles the question
#2539/#2540's fence-swallow fix left open for the three CI gates that still
run the naive `` sed 's/`[^`]*`//g' `` strip on PR bodies
(`pr-checks.yml:104`, `anti-dup-retro-check.yml:55`/`181`): the strip's
blind spot is **correct**, not a hole — GitHub's own parser is blind to the
same fenced content those gates strip, so there is no accidental-auto-close
case they're failing to catch. No fence-aware port needed there. **The same
two-sided consequence as the backtick case above applies**: an *intentional*
`Closes #N` inside a fence will silently fail to auto-close on merge, exactly
like a backticked one — plain text near the issue number is still the only
form that fires the parser.

**Empirical traps caught (2026-05-30):**
- PR #516 body said `"this PR does NOT close #510"` → auto-closed #510 on merge
  (the NOT is invisible to the regex; caught manually + reopened).
- PR #537 round-1/2 body had two trap occurrences referring to #51 → would
  have auto-closed #51 on merge (caught pre-merge by counterpart-colleague).

   **This query lags a `gh pr edit --body`/push — measured in EITHER
   direction, never trust the very first result after an edit.** Both
   measured live on exam-room's issue 29 / PR#30 (2026-08-23, a different
   repo, same shared `gh`/GraphQL tooling), with different, non-interchangeable
   timing evidence for each direction — stated precisely because an earlier
   draft of this note attributed the tightly-timed measurement to the wrong
   direction, caught by a reviewer-fairy:

   - **False BLOCKER, tightly timed.** A body edit that removed every
     close-keyword (a genuinely GOOD edit) queried as **non-empty** —
     reflecting the stale, pre-edit bad state — immediately after the push.
     A second query exactly **5 seconds later** correctly read empty. This is
     the one pairing with a controlled, measured interval: 5 seconds was
     enough to resolve in this instance, but that is one data point, not a
     guaranteed bound.
   - **False SAFE, loosely timed.** Separately, a body edit that still
     carried a negated close-keyword (a genuinely BAD edit — negation
     doesn't help, per the trap documented above) queried as **empty**
     immediately after its own push. The same untouched text was only later
     found to correctly read non-empty, via a check made after several
     unrelated actions had intervened — the true gap is unmeasured, but was
     clearly much longer than a few seconds.

   Query, wait — at least 5 seconds, and longer if anything else is
   happening in between — query again, and trust only the settled answer.
   A single-shot check right after editing is not evidence either way, and
   do not assume the delay is short just because it resolved quickly once.

3. **No commit-message auto-close** (always): `closingIssuesReferences` reads
   the PR BODY only — a merge commit's own message is a THIRD close surface
   GitHub's parser reads independently, invisible to checks 1 and 2. **This
   repo's exposure is currently LOWER than umwelt's and soma's, and say so
   precisely:** engram-alpha's `squash_merge_commit_message` is `BLANK`
   (measured 2026-08-22 — three real recent squash merges carry no
   branch-commit content, only PR title + co-author lines; the setting
   re-read `BLANK` on 2026-09-03 and 2026-09-28), so branch commit messages
   do NOT ride into the merge commit here today. The check stays anyway, as
   defense-in-depth against the three ways the exposure appears without any
   code change: a repo-settings flip to `COMMIT_MESSAGES` (silent, and
   exactly what makes **umwelt and soma** exposed — both read
   `COMMIT_MESSAGES`, measured 2026-09-03 and re-read 2026-09-28 via
   `gh api repos/engram-agents/<repo> --jq .squash_merge_commit_message`;
   a seat reading only "umwelt" here would skip the commit-message grep on
   a soma PR), a merger hand-editing the squash
   message at merge time, and copy-paste of commit text into a PR body.
   The incident that makes this real is umwelt#674/#632 (2026-08-22): empty
   array there, merge concluded safe, the issue closed **by the commit**,
   silently retiring the half of a two-defect issue the PR didn't fix
   (umwelt#675 is the recovery). The three-surface diagnosis and this remedy
   were first specified in Ariadne's graph on 2026-08-05.

   Every match must reference the SAME `#N` the PR intends to close — anything
   else gets the commit reworded (rebase) or accepted deliberately, never
   discovered post-merge. The pattern covers the `owner/repo#N` cross-repo
   close form (the first draft missed it — exercised five ways by Ariadne,
   2026-08-22); bare `reponame#N` stays unflagged because it never closes
   anything. Two things are deliberately unasserted rather than claimed:
   `Closes: #N` with a colon (unmeasured), and **whether a close keyword in
   the squash-commit SUBJECT fires the parser** — relevant here because this
   repo's `squash_merge_commit_title: PR_TITLE` puts the PR title into the
   subject, a path the commit-range grep cannot see. The 2026-05-31
   measurement (7/7 title-only PRs did not auto-close) suggests subjects
   don't fire, but it predates verification of the current setting — exactly
   one of "the setting changed since May" / "subject keywords don't fire" is
   true, and nobody has separated them. Measure on the next real
   `[closes #N]` merge before claiming either way.
