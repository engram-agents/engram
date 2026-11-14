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

   **The stale window can last minutes, until the next edit event.** One case,
   umwelt#710 (2026-08-23): its body first carried `Does not close #699`, an
   adjacent close keyword under negation, the trap documented above. An edit
   34 seconds later removed it. Six minutes after that edit,
   `closingIssuesReferences` still returned #699 (a captured response, not a
   recollection). It appears to have cleared at the next edit event, a title
   rename hours later; no read was captured at the rename itself. So a stale
   answer is not bounded to a few seconds. This is one data
   point (n=1, one repo; public repos not measured), and whether the window is bound to the next
   edit event or to time was not measured.

   **No proximity gap: the grep's adjacency matches the parser.** #710 was
   first read as a proximity blind spot: the keyword and `#699` looked far
   apart in the title, yet the issue would close. Three probe sets on a scratch
   repo (umwelt#744, 2026-08-26) tested that and found the opposite. The parser
   linked a keyword to `#N` only when the reference immediately followed it;
   words between, a bracketed aside, a sentence break or a bare line break in
   the same paragraph all broke the link, and with two `#N` after one keyword
   only the adjacent one linked. Title text alone never populated
   `closingIssuesReferences` before merge, and three replica constructions,
   one of them an exact replica of #710, reproduced 0 of 3. Reading the body's edit history
   (`userContentEdits` in GraphQL) then found the negated adjacent keyword
   above. So the adjacent-only greps (checks 2 and 3 in `CLAUDE.md`) match the
   measured parser and need no widening on this evidence. The
   GraphQL query is still the only real pre-merge check, because of the stale
   window, not because of a distance rule. umwelt's `CLAUDE.md` carries the
   full record; this paragraph is its engram copy (#2213).

3. **No commit-message auto-close** (always): `closingIssuesReferences` reads
   the PR BODY only — a merge commit's own message is a THIRD close surface
   GitHub's parser reads independently, invisible to checks 1 and 2. **This
   repo's exposure is currently LOWER than umwelt's and soma's, and say so
   precisely:** in the development repo as it was (engram-alpha, private, retired
   2026-11-10), `squash_merge_commit_message` was `BLANK`
   (measured 2026-08-22 — three real recent squash merges carry no
   branch-commit content, only PR title + co-author lines; the setting
   re-read `BLANK` on 2026-09-03 and 2026-09-28), so branch commit messages
   did NOT ride into the merge commit there. **Current repo (engram, public):**
   read 2026-11-11 as `squash_merge_commit_message` `BLANK` and
   `squash_merge_commit_title` `PR_TITLE`; they were reset to these values after
   the rebuild. The check stays anyway, as
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

## Closing keywords do not close issues on a merge into `dev` (2026-11-11)

On this repository the default branch is `main` and PRs merge into `dev`. Measured
twice on 2026-11-11: a PR with the title `[closes #N]` and the body `Closes #N.`
merged into `dev`, and the issue stayed open both times. The two cases, read off
GitHub:

- #4606 (body `Closes #4600.`, base `dev`) was squash-merged into `dev` as
  `47e4c615` at 2026-11-11T14:49:33Z. Before the merge, `closingIssuesReferences`
  was empty. Twenty seconds after it, #4600 was still open with only
  `cross-referenced` and `referenced` events.
- #4607 (body `Closes #4522.`, base `dev`) was squash-merged into `dev` as
  `e0c09c74` at 15:16:52Z. #4522 got a `referenced` event at 15:16:54Z and was
  still open until it was closed by hand at 15:17:10Z, 18 seconds after the
  merge, with no commit on the close event.

In both cases the merge seat closed the issue by hand, with a comment naming the
PR and the merge commit.

GitHub applies closing keywords only on a merge into the default branch, which
matches what was observed. The team commander's ruling of the same day, standing
rather than interim: the merge seat closes the issue by hand right after the
press, naming the PR and the merge commit. Titles keep `[closes #N]` and bodies
keep `Closes #N.` as readable intent. No closing workflow is built for now. If
`dev` becomes the default branch, the keywords work again and the rule lapses.
