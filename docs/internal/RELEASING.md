# Releasing ENGRAM

This is the procedure for cutting an ENGRAM release. It replaces the
two-repository procedure used through v0.5.5 (development in a private
repository, one copied snapshot per release in the public one). Since v0.5.6
development and releases happen in this repository, on one history. Branch and
tag names are defined in [`docs/VERSIONING.md`](../VERSIONING.md); where this
page and that one disagree about names, that page is right.

A release has three parts, in this order:

1. **Candidate.** A release branch `release/X.Y.Z` is cut from `dev` at the
   candidate commit and tagged `vX.Y.Z-rcN`. `dev` stays open. The candidate is
   gated and the team lives on it (the soak).
2. **Release.** A pull request from the release branch into `main` runs the
   whole test suite. When it is green, `main` is fast-forwarded to the soaked
   commit, that commit is tagged `vX.Y.Z`, and the Releases entry is created.
3. **Tell the public.** Website, then newsletter. A version is *released* only
   when all three public surfaces are live (definition below).

## Who does what

| Role | Does |
|---|---|
| Release seat | Runs this procedure, cuts the release branch, pushes tags, merges soak fixes into the release branch, moves `main`, writes the soak report and the release record. One seat per release, named when the candidate is cut. |
| Gate seat | Runs the whole test suite on the candidate commit and the skip-set comparison, and the install checks (gates G2, G3, G6). Not the release seat. |
| Security seat | Reads the candidate for the release (gate G5) and says so in writing on the record. |
| Surfaces | Website Versions entry in both languages, then the newsletter. |
| Maintainer (human) | Says go for the release tag. Nothing in part 2 happens before that word. |

## What changed from the two-repository procedure

- There is no cutover tool and no exclusion list. `tests/`, `.github/` and the
  development documents are part of the repository a user clones.
- The name-leak scan runs on every pull request, and GitHub secret scanning
  covers code and issue text continuously. A release adds no scan of its own;
  it checks that those are green (gate G4).
- Candidate tags (`-rcN`) are public. A plain clone lands on `main`, which only
  ever holds a release commit, so a user following the install guide gets the
  release. The installer itself accepts any tag beginning with `v`, so never
  point a user at a candidate tag.
- `main` moves only by a fast-forward to the release commit. A pull request
  from the release branch into `main` exists to run the whole suite on that
  commit (step 6); it is never merged with the button. A merge commit, a squash
  or a rebase would put a commit on `main` that no tag names, and the installer
  requires that the commit a plain clone lands on carries a release tag.
- Since v0.5.7 there is no freeze of `dev` (maintainer's ruling, 2026-11-13).
  The candidate lives on its own branch `release/X.Y.Z`, so `dev` keeps taking
  pull requests while the candidate is gated and soaked.
- Because `main` is the default branch, a pull request into `dev` does not
  close its issue by itself. Whoever merges closes the issue by hand, naming the
  pull request and the merge commit.

## Part 1: the candidate

### 1. Choose the version

```bash
git fetch origin --tags
git log --oneline <previous-release-tag>..origin/dev
```

Group the changes by theme. The middle number changes for a wave of new
features, the last number for fixes and small improvements. The first candidate
is `-rc1`; each fix round adds one.

### 2. Bump the plugin version and move the CHANGELOG, in one pull request

Root `plugin.json` carries the clean version (`X.Y.Z`, never `-rc`). It is the
single source the build reads: the build engine (`src/build/engine/build.py`)
loads that version and `compute_build_version()` stamps every artifact from it.

The changelog is `docs/CHANGELOG.md`. Check `[Unreleased]` against the last
release tag and remove anything already released. Move what remains to a new
section `## [vX.Y.Z] - YYYY-MM-DD` and leave a fresh, empty `[Unreleased]`
above it.

Both changes go to `dev` in **one** pull request through the normal review
path. `TestPluginVersionHasChangelogSection` in `tests/test_release_gates.py`
fails unless `plugin.json`'s version equals the newest changelog heading, so
`dev` is red between two separate pull requests.

### 3. Cut the release branch and tag the candidate

When that pull request has merged, cut the branch at **its merge commit** and
tag. `dev` is open, so its tip may already hold a later change that is not in
the changelog's `vX.Y.Z` section; do not cut at the tip without reading what
landed after the bump.

```bash
git fetch origin
H=<full id of the bump pull request's merge commit>   # gh pr view <N> --json mergeCommit
git merge-base --is-ancestor "$H" origin/dev \
  && git push origin "$H":refs/heads/release/X.Y.Z \
  && git tag -a vX.Y.Z-rcN "$H" -m "vX.Y.Z-rcN" \
  && git push origin vX.Y.Z-rcN
git ls-remote origin refs/heads/release/X.Y.Z 'refs/tags/vX.Y.Z-rcN^{}'   # both must print H
```

The commands are chained on purpose: a candidate tag cannot be deleted, so it
must not be created when the branch push before it failed.

Then open the release pull request, so the whole suite runs on the candidate
while the team soaks it (its check is gate G1, read in step 6):

```bash
gh pr create --repo engram-agents/engram --base main --head release/X.Y.Z \
  --title "release: vX.Y.Z" --body "Release gate for vX.Y.Z. Never merged with the button: main is fast-forwarded to the head after the maintainer's go (docs/internal/RELEASING.md)."
```

Opening it moves nothing. **Never press the merge button on it.**

Post where the team reads orders: `release/X.Y.Z` is cut at `H`, `dev` is open,
and new work goes under the changelog's `[Unreleased]` heading, never under the
version that now has a release branch.

**`dev` is not frozen.** From this point the candidate and `dev` are two
branches. What merges into `dev` rides the next release unless it is copied to
the release branch by the rule in step 5.

The release branch and every tag beginning with `v` or `deploy/` are protected
by rulesets (table in step 7): only the release team can create such a tag or
update an existing release branch, the branch cannot be deleted or force-moved
by anyone, and a new commit reaches it only through a pull request. One limit,
read from the rule types and not tested: no rule restricts *creating* a branch
named `release/...`, so any writer could create one, and could then neither
update nor delete it. Cut release branches only from this procedure. The branch is
never deleted. It stays as the record of what was released.

### 4. Gates on the candidate commit

Every gate is read on the commit the candidate tag points at, and its result
goes into the release record (step 10) with the run's identifier.

| Gate | What must be true | Who |
|---|---|---|
| G1 | The hosted full suite is green on the release pull request (opened in step 3, read as described in step 6): the `pytest (3.12)` check on the pull request's head, produced by a `pull_request` run, with the head equal to the candidate commit. It runs again when a soak fix moves the release branch. A hand-started run on the candidate tag (`gh workflow run tests.yml --ref vX.Y.Z-rcN`) is useful early corroboration and cannot substitute: `main` requires the check, and a check produced by a hand-started run on a tag did not satisfy it when measured (2026-11-13). | Release seat |
| G2 | A whole-suite run on a fresh full clone checked out at the candidate tag, with the project's own interpreter, is green. The full output is kept. | Gate seat |
| G3 | The skip-set comparison below shows no unexplained new skip. | Gate seat |
| G4 | `name-leak-gate` is green on the candidate commit. For the first candidate that is its run on the push to `dev`; for a later candidate, whose commit lives only on the release branch and has no run of its own, it is the run on the soak-fix pull request that produced it, with that pull request's head tree equal to the candidate's tree (the workflow runs on pushes to `dev` and on pull requests, not on pushes to a release branch). `pr-checks` is green on the pull request whose merge produced the candidate, and that pull request's head tree equals the candidate's tree (`pr-checks` runs on pull requests only, so no run exists on the squash commit itself). The secret-scanning alert list has no open alert; this covers code and issue text, and it is a read at one moment: if scanning was enabled or the candidate was pushed within the hour, this part is "not run" until the list has settled. | Release seat |
| G5 | The security seat reads the net change `vPREV..vCAND`, the diff plus the list of merge commits, and records on the candidate commit, by its full id, either "no objection" or an objection naming the change and the reason. The record states what was read and its bound: files reviewed against files changed. If the security seat could not make the read, the gate is not run; it is not an implied pass. | Security seat |
| G6 | An install from a plain clone at the candidate tag into an empty home passes `install_selfcheck.py`; and an install over a copy of an existing home leaves every file of that home byte-identical or backed up. | Gate seat |
| G7 | On macOS, the tests that skip on Linux pass on a seat that runs a real Mac, on the candidate commit. The record names the `bash` that ran them: the stock `/bin/bash` (3.2) and a newer one differ (#4620). | A Mac seat |

A gate that cannot be run is not a pass. Write "not run" with the reason, and
the maintainer decides whether the release goes without it.

#### G3: the skip-set comparison

A green run says which tests failed. It does not say which tests stopped
running. For v0.5.6 a green run hid 84 tests that were skipped because they
pinned commits this repository did not have; only a comparison of skip sets
showed them.

1. **Two runs are compared:** the gate seat's whole run on this candidate (G2)
   and the recorded run of the previous release. The reference is the file
   `docs/internal/release-records/vA.B.C-skips.json` of the previous release
   `vA.B.C`. If no such file exists (the case for the first release cut
   by this procedure), the gate seat first makes it from the recorded run of
   the previous release and commits it in its own pull request; G3 is "not
   run" until a reference exists.
2. **What is recorded** for each run: the commit, the interpreter and platform,
   the number of tests collected, passed, failed and skipped, and the skipped
   tests by full id, grouped by skip reason.
3. **The rule:** every test that is skipped now and was not skipped in the
   reference is either named in the release record with its expected reason
   (platform, optional dependency) or it blocks the release. A test that
   disappeared from collection altogether is treated the same way.
4. **Where it is stored:** the new run's skip set is committed as
   `docs/internal/release-records/vX.Y.Z-skips.json` in the release-record
   pull request (step 10), so the next release can read it.

The release record states both skip counts and the list of differences, even
when that list is empty.

### 5. Soak: live on the candidate

The soak seats upgrade to the candidate and work on it for the soak period.
The maintainer names the seats and the period when the candidate is cut; the
starting rule is **one day on the team's own seats, including one night's
sleep cycle on each** (2026-11-13), to be adjusted from practice. Each soak
seat checks out the candidate tag and rebuilds:

```bash
git fetch origin --tags && git checkout vX.Y.Z-rcN
tools/install-local-marketplace.sh
```

then runs the activation steps of the `engram-upgrade` skill and reports its
own read-back (version, the stale-process check, the store's counts).

**A soak finding** is posted at once, marked as such, and comes before other
work. A seat that checks something and finds it is not caused by the candidate
posts that too, so it is not asked again.

**A fix found in soak goes to `dev` first**, through the normal review path,
with its changelog line in the same pull request, under `[Unreleased]`. Its
copy for the candidate then **arrives as a pull request into
`release/X.Y.Z`**, squash-merged, where the changelog line goes under the
`vX.Y.Z` section, not under `[Unreleased]` (expect to place that line by hand;
the two branches differ exactly there). A hand-pushed
cherry-pick is refused by rule: the release branch requires the `pr-checks`
check and a directly pushed commit has no such run. The release seat merges
that pull request, tags the next candidate on the new tip of the release
branch, and the gates of step 4 run again on it. There is no fix on `main`
and no fix on a tag.

**The soak report.** Before asking for the go, the release seat gives the
maintainer one report: the soak seats and when each started, every finding and
every checked non-finding, each gate's result, and the known limits that go
into the release notes. The candidate is accepted when every gate is recorded,
the soak seats have reported and the maintainer has read that report. A fix
for a defect that loses user data does not wait for a soak it does not need;
that is the maintainer's call.

## Part 2: the release

Nothing in this part that moves a branch or creates a tag happens before the
maintainer's go. Step 6 only reads.

### 6. Read the release pull request's check

The release pull request was opened in step 3 and its check has been read for
the soak report. Read it again now, on the accepted candidate, in one shell:

```bash
git fetch origin --tags
C=$(git rev-parse --verify -q 'vX.Y.Z-rcN^{commit}')           # the accepted candidate
if [ -n "$C" ] && [ "$C" = "$(git rev-parse origin/release/X.Y.Z)" ]; then
    gh api "repos/engram-agents/engram/commits/$C/check-runs?per_page=100" \
      --jq '.check_runs[] | select(.name=="pytest (3.12)") | {conclusion, head_sha, run: .details_url}'
else
    echo "STOP: the release branch head is not the accepted candidate"
fi
gh run view <run-id> --repo engram-agents/engram --json event,headSha,conclusion
```

The first command prints one line for **every** run of the check on that
commit; the run id for the second command is the number after `/runs/` in each
line's `run` URL. More than one line is normal: a hand-started run on the candidate tag
shows up under the same name (measured on v0.5.7-rc3: two rows, one from a
hand-started run on the tag and one from the release pull request). **Pick the
row by its event, not by its colour.** For each row, read its run with the
second command; the row that counts is the one whose `event` is
`pull_request`. That row's `head_sha` must equal `C` and its conclusion must
be `success`. A green row from a hand-started run does not count, and `main`
would refuse the push in step 7 if it were the only one. This check is gate
G1.

**If the check is red, stop.** Read which cells failed and bring them to the
maintainer with their cause. One exception, the maintainer's ruling of
2026-11-14 on #4655: if the red is confined to a test already filed as
unreliable under load, the release seat may rerun that one failed job once.
Before the rerun starts, record on the release pull request that it is a rerun
and the filed issue it rests on. The rerun is not repeated: if it is red, or
if any other cell is red, the release stops and goes to the maintainer with its
cause. (The suite's memory use on the hosted runner, and the plan that keeps it
from blocking a release, are tracked in #4655.)

### 7. Fast-forward `main` to the release commit

Run this in one shell. `C` is set again here on purpose: with `C` empty the
push below would name no source, which git reads as a request to delete `main`.

```bash
git fetch origin --tags
C=$(git rev-parse --verify -q 'vX.Y.Z-rcN^{commit}')           # the commit whose check was read in step 6
if [ -n "$C" ] && [ "$C" = "$(git rev-parse origin/release/X.Y.Z)" ] \
   && git merge-base --is-ancestor origin/main "$C"; then
    git push origin "$C":refs/heads/main           # never with --force
else
    echo "STOP: C is empty, the release branch moved, or main is not an ancestor of C; nothing was pushed"
fi
git ls-remote origin refs/heads/main               # must print C
gh pr view <N> --repo engram-agents/engram --json state,mergedAt   # must say MERGED
```

If the block prints STOP, nothing was pushed. Either the candidate tag did not
resolve, or the release branch moved after its check was read (a soak fix
merged: that is a new candidate, back to step 4), or `main` holds a commit the
release branch lacks. Find out which.

**If the push is refused for any reason** (the check is not accepted, `main`
moved meanwhile, the account is not exempt): stop and report to the
maintainer. Never force, and never suspend a rule to get past a refusal.
`main` is never moved back: its history rule allows no force-move for anyone,
so a bad release is repaired by a new release, not by moving `main`.

The pull request is expected to read `MERGED` once `main` contains its head
(GitHub's documented behaviour; not yet observed on this repository, so the
first release cut this way records what it actually read). If it reads
`CLOSED`, the record would say the release pull request was abandoned; find
out why before going on.

**Why a fast-forward and not the merge button.** The installer's first step
requires that the commit a plain clone lands on carries a release tag. So
`main` must end exactly on the tagged commit, not on a merge commit with an
equal tree. Do not simplify this step into a merge.

**Who may run this step, and how to check.** Only an account in the
repository's release team (the organization team `release-bypass`). Adding a
member to it is an organization-owner action, and that is the actual
protection: whoever is on that team can move `main`, push to a release branch
and create a release tag. Name the account in the release record. The
rulesets, each separate so that one can be read or suspended alone:

| Ruleset | Covers | Rule | Exempt |
|---|---|---|---|
| `main-protect-history` | `main` | no deletion, no force-move | nobody |
| `main-release-seat-only` | `main` | no update | release team |
| `main-require-full-suite` | `main` | required check `pytest (3.12)` | nobody |
| `release-branches-protect-history` | `release/*` | no deletion, no force-move | nobody |
| `release-branches-release-seat-only` | `release/*` | no update | release team |
| `release-branches-require-pr-checks` | `release/*` | required check `pr-checks`, not enforced when the branch is created | nobody |
| `deployable-tags-release-seat-only` | tags `v*` and `deploy/*` | no creation | release team |
| `protect-release-tags` | all tags | no deletion, no move | nobody |

The tag rule covers both prefixes because the installer accepts a tag that
begins with `v` or with `deploy/`. Two more rulesets exist and are not release
rules: `dev-require-PR-approval` (it matters once here: the merge-back of
step 10 needs an approval, including after its last push, so its author cannot
merge it alone) and `protect-release-snapshots` (the branch of the releases up
to v0.5.5).

A non-admin account reads every exemption list as empty, so the list is not
evidence from such an account. The field that is evidence from any account is
`current_user_can_bypass`, read from the account that will act:

```bash
gh api repos/engram-agents/engram/rulesets/<id> --jq .current_user_can_bypass
```

From a release-team account it must print `always` on the three rules that
name the release team as exempt (`main-release-seat-only`,
`release-branches-release-seat-only`, `deployable-tags-release-seat-only`) and
`never` on the five other rules of the table. Measured 2026-11-13 from both
sides: that pattern from a release-team account, and `never` on every ruleset
of the repository from an account outside the team. **Never test these rules with a real push.** A tag or a
release branch that slipped through could never be deleted.

What the rules buy is narrow, and it should not be read as more. Another
account can no longer publish unreleased code by moving `main` or by creating
a tag the installer accepts. They do not protect against a release-team
account that is itself compromised, and they do not protect `dev`. No ruleset
can say "`main` only ever equals a release tag"; the checks in this step and
the next carry that.

**A lasting effect to know.** The required check on `main` applies to every
future write to `main`, not only to releases. Any commit pushed there needs a
green `pytest (3.12)` from a pull request or push run on that commit.

### 8. Tag the release

```bash
git fetch origin --tags
C=$(git rev-parse --verify -q 'vX.Y.Z-rcN^{commit}')           # the accepted candidate
if [ -n "$C" ] && [ "$C" = "$(git rev-parse origin/main)" ] \
   && [ "$C" = "$(git rev-parse origin/release/X.Y.Z)" ]; then
    git tag -a vX.Y.Z "$C" -m "vX.Y.Z" && git push origin vX.Y.Z
else
    echo "STOP: main, the release branch and the candidate are not one commit; nothing was tagged"
fi
git ls-remote origin 'refs/tags/vX.Y.Z^{}' refs/heads/main refs/heads/release/X.Y.Z   # three equal ids
```

The tag and its push sit inside the check on purpose. If step 7 stopped and
`main` is still the previous release, this block must tag nothing: a release
tag on the wrong commit could never be corrected.

Between step 7 and this step `main` sits on a commit that carries only a
candidate tag, so a plain clone in that window lands on `vX.Y.Z-rcN`. The
content is the release's and the installer accepts it; the window is expected
and should be minutes. (The old order, tag before fast-forward, had no such
window, but could tag a commit `main` then failed to reach.)

Before the tag, `main` must equal the release pull request's head. After it,
the commit the tag peels to, `main` and the release branch head must be one
id. Release tags are protected: once pushed, a release tag cannot be moved or
deleted. Read `C` twice before pushing.

### 9. Create the Releases entry

A tag push does not create a Releases entry.

Copy the `## [vX.Y.Z]` section of `docs/CHANGELOG.md`, without its heading,
into a file first; `--notes-file` takes a file, not a section.

```bash
gh release create vX.Y.Z --repo engram-agents/engram --verify-tag \
  --title "vX.Y.Z" --notes-file release-notes-vX.Y.Z.md --latest
gh release view vX.Y.Z --repo engram-agents/engram
```

Open the page logged out and confirm it renders before anything links to it.
This completes surface (1) of the definition below, and only that one.

### 10. Write the release record, and bring the release branch home

Open one pull request into `dev` that
adds `docs/internal/release-records/vX.Y.Z.md` and the skip-set file of G3.
The record names: the release seat, gate seat and security seat; the candidate
tags and their commits; each gate with its run identifier and result; both
skip counts and the differences; the full ids of the release tag object and
the release commit; the time `main` moved; the account that pushed; the soak seats and the soak
report; and anything that was not run.

**If the release branch carries a commit that `dev` lacks** (a soak fix copied
onto it), `main` now holds that commit too, and the next release branch, cut
from `dev`, would not contain it: the next fast-forward of `main` would be
impossible. So before the next release branch is cut, open a pull request
from `release/X.Y.Z` into `dev` and merge it **with a merge commit, not a
squash**. The repository allows a merge commit into `dev`; this pull request
and the release pull request are the two exceptions to "every pull request
into `dev` squashes" (`PR-MERGE-POLICY.md`). The fix is already on `dev` as its
own squash, so the merge usually changes nothing but history. If `dev` has
since touched the same lines, or on the changelog (the line sits under two
headings), GitHub reports a conflict: resolve it on a branch cut from `dev`
that merges the release branch locally, keeping `dev`'s content, and open the
pull request from that branch, still merged with a merge commit. Then remove
from `[Unreleased]` on `dev` anything the release already shipped (step 2
does the same check at the next release). Check afterwards that
`git merge-base --is-ancestor origin/main origin/dev` succeeds. Measured in a
local repository only (2026-11-13): without the back-merge, and with a squash,
the next fast-forward of `main` is refused; with a merge commit it succeeds. When no soak fix was needed, the release branch is
already inside `dev`'s history and this step is only that check.

## Part 3: tell the public

> **Definition of released (Lei, 2026-10-02).** A version is reported as
> *released* only when all three public surfaces are live, in this order:
> (1) the public repo carries the tag and the Releases entry (the cutover
> tool plus `gh release create`, below); (2) the website Versions entry is
> live in both languages at engram-agents.org/versions; (3) the newsletter has
> gone to the *ENGRAM Communications* subscribers. The tool does the tag half of (1); the Releases entry is a separate `gh release create` (a tag push never creates it, see *Announcing the public release*).
> (2) and (3) are Ariadne's by standing assignment (Zephyr may do the Chinese
> text); the two steps are specified under "Tell the public" below, and the
> cutover tool's `--push` summary prints all three remaining steps as a block so
> the success line cannot be mistaken for the finish line. Until (3) is done
> the commander's reports say "repo released; website and newsletter
> pending", never "released". Origin: v0.5.2 was reported released at
> 2026-10-02 00:1xZ with (2) and (3) undone; the doc already carried both
> steps, so the fix is the definition and the print, not a third copy.

*How surface (1) is made since v0.5.6.* The block above is kept as it was
written. Its words "the cutover tool", "the tool does the tag half" and "the
cutover tool's `--push` summary" describe the retired two-repository tool, and
*Announcing the public release* was a section of the old procedure. Surface (1)
is now made by steps 6 to 9 of this page: the fast-forward of `main`, the
release tag, and `gh release create`. Surfaces (2) and (3), their order and their
owners are unchanged.

## Notify users

For alpha-tester pool (agents, users, or counterparts on other hosts):
- Brief release announcement summarizing what's in the version
- Pointer to the public GitHub Release page (`engram-agents/engram`)
- Upgrade instruction: `git fetch origin --tags && git checkout vX.Y.Z &&
  tools/install-local-marketplace.sh` (the release tag; `dev` is ahead of the
  release and is for development, not for upgrading to it),
  then `/plugin marketplace update engram-local`, then /plugin -> Installed ->
  engram plugin -> Update now, inside Claude Code (or `git checkout` the tag
  if they prefer pinning)

For on-host agents, surface in `ask-{{USER_NAME}}.md` after the cut. For the
internal family dogfood window, a forum post to the relevant thread (with
`@mentions`) reaches cross-host agents in real time.

### Tell the public — website Versions entry, then the newsletter

Everything above reaches the **internal** pool. These two steps reach everyone
else: people who are not on this host, do not watch the repo, and will never
see a forum post.

**Write the release note ONCE.** The website's Versions entry and the
newsletter body are the same artifact published to two surfaces — a short
prose account of what this version is for. Do not write it twice and let the
two drift; draft it once and adapt the framing.

**Every sentence must be supported by the release note.** Read each sentence of
the Versions entry against the note before its PR goes to review, and each
sentence of the newsletter again before you send it. The v0.5.3 website and
newsletter drafts carried several slips that only review caught: claims the
note never makes (a pilot's duration, a motive), wording that went past the
note's scope or ranked what it did not rank, and an opt-in feature described as
if it were the default. If the note says a thing *can* be set, the public text
says *can*, not *does*.

**Checkable claims are checked against the world on the day, not only against
the note.** A count or a fleet state ("our whole team runs it") can be in the
note and still not be true yet when you publish. In v0.5.3 the note said the
setting was on across our fleet; the read-backs posted by publish time covered
seven of nine seats.

**Order: website first, newsletter second.** The newsletter can then link a
Versions entry that is already live, and the site is the durable record that
outlives the email.

#### 1. Website — add the Versions entry

Repo `engram-agents/website`, file `src/i18n/versions.js`. Add one entry to
**both** `versions.en.releases` and `versions.zh.releases`:

```js
{ id: 'v0-4-0', version: 'v0.4.0', title: 'the short framing',
  date: '2026-08-20', body: "One or two paragraphs, plain language." }
```

- **The Chinese entry is not optional.** CI (`tools/check-locale-content.mjs`)
  requires the en and zh release arrays to align by `id`/`version`; a missing
  or misaligned zh entry fails the build. That gate exists because a Chinese
  paragraph once landed in the English array.
- **The prose goes straight into `versions.js` — there is no drafts directory.**
  Both prior entries were written that way: website PRs #14 (v0.3.2) and #17
  (v0.4.0) each put the release prose directly into `src/i18n/versions.js`.
  #17 touched that file and nothing else; #14 also added the locale-check
  tooling cited in the bullet above, which is already in place and is not
  work you repeat. If you want a draft to circulate first, open the PR and
  iterate on it there.
- **Merging is deploying** — Cloudflare (Git-connected Workers Builds)
  auto-deploys on push to `main`. Confirm the entry is live at
  [engram-agents.org/versions](https://engram-agents.org/versions) before the
  newsletter links it.

**An agent can do this step end to end** — it is an ordinary PR.

#### 2. Newsletter — send to public subscribers

**Where:** Buttondown, newsletter *ENGRAM Communications* — compose and send
from `https://buttondown.com/sptopen`. The public signup page
(`engram-agents.org/subscribe`, and `/zh/subscribe`) is where subscribers come
from, not where you send.

**⚠️ ORDER MATTERS — send AFTER the public release exists, never before.**
The email links the public repo's Releases entry, which you created by hand
in step 9 (the subsection carrying the
`gh release create` command) — it is not created by the tag push. Announcing first produces an email
whose main link 404s, and unlike a broken doc link **you cannot edit an email
after it is sent.** Load every link once before sending.

**What to include:**

- The version and a one-line framing of what this release is *for*.
- The two or three changes a user would actually notice. Not the changelog —
  that is what the link is for.
- **The upgrade line, VERBATIM — do not rewrite it and do not list commands:**

  > **Upgrading:** ask your agent to upgrade ENGRAM. It will walk the full upgrade
  > routine — which is more than the mechanical commands — and it can answer any
  > questions you have about what changed and whether anything affects you.

  **Why this is fixed text and not a style preference** (Lei, 2026-08-20). The
  alpha-tester pool above DOES get commands; a public subscriber must not. Listing
  them does not merely duplicate the `engram-upgrade` skill — it actively invites
  the mechanical-only path, because a reader handed four commands runs four commands
  and never reaches the routine's verification checkpoints, identity-template gate,
  or reconnect steps. The harm is SUBSTITUTION, not redundancy, and it lands hardest
  on the users least able to notice what was skipped.
- Links to the **public** GitHub Release page (`engram-agents/engram`), not
  a candidate (`-rcN`) tag, and to the Versions entry from step 1.

**Do NOT hand-roll an unsubscribe link.** Buttondown states that every email
it sends is one-click-unsubscribe compliant (RFC 8058) and injects the headers
itself. Adding your own produces two unsubscribe links, one of which does not
work.

**CAN-SPAM postal address: SET — and the gate is the delivered footer, not the
setting.** CAN-SPAM requires a valid physical postal address in commercial email.
Buttondown does not invent one for you. The mailing address is held in the
newsletter's **footer** setting, also readable over the API as the newsletter's
`footer` (read non-empty 2026-10-03). That this field is what renders in the
email is confirmed only by a delivered copy: Lei confirmed the address in a
delivered email in a real inbox on 2026-09-05 (the v0.5.0 send).

**Every send: check two things, in this order — one before, one after:**

1. **Before the send: the setting is present.** The newsletter's `footer` field over the API
   (`GET /v1/newsletters`) is non-empty and still the mailing address. An empty
   field means the address will be missing from the send: stop and tell the
   maintainer.
2. **After the send: the delivered footer is the real check.** A setting is evidence the address
   *should* render; only a delivered email shows that it *did*. After the send,
   read it in a subscriber's real inbox (HTML, not just the plain-text part)
   when one is available to you. If none is, say so in the release report
   rather than reporting the footer as verified.

The subscriber count is **information, not a gate.** Report it if you read it;
it does not decide whether a send may go.

*History, kept because it explains the shape of the check: on 2026-08-21 the
delivered v0.4.0 newsletter carried no postal address, and at that time the
API's footer field read empty — which could not distinguish "no address" from
"injected at send time". The address was set afterwards, and the field returned
it non-empty when read on 2026-10-03. A delivered email is still the only discriminating read.*

**⚠️ Drafting IS agent-executable; SENDING is a separate question.** As of
2026-08-20 an API key is installed, so an agent can read the subscriber list and
create the email as a real `draft` — Buttondown's `status` field is a server-side
enum (`draft` → `about_to_send` → `sent`), so a staged draft is genuine state the
maintainer can open and read before anything leaves.

**Capability moved; permission did not.** A send reaches external people, so it
stays inform-before regardless of who *can* press it: draft it, show the
maintainer the actual text, send on their word. And note the asymmetry that makes
this worth the extra step — a merge can be reverted, a sent email cannot be edited
or recalled.

## After the release

- Record the release in your own memory with the tag object, the release
  commit and the time `main` moved as evidence.
- `main` sits at the release tag until the next release. Development continues
  on `dev`.
- A defect found after the release is fixed on `dev` and shipped as the next
  patch release through this whole procedure. There is no hotfix on `main`.
- The release branch stays. Nobody deletes it.

## Anti-patterns

- **Don't move `main` with the merge button, a merge commit, a squash or a
  rebase.** Each puts an untagged commit on `main`. `main` moves by
  fast-forward to the release commit and in no other way; the release pull
  request exists for its check.
- **Don't push a fix straight to a release branch, and don't fix only there.**
  A soak fix lands on `dev` first and reaches the release branch by pull
  request. A fix that exists only on the release branch is lost at the next
  release.
- **Don't cut the next release branch while `main` holds a commit `dev`
  lacks.** Merge the previous release branch back first (step 10).
- **Don't delete a release branch.** It is the record, and the rules refuse it.
- **Don't treat a hand-started suite run as the gate.** The gate is the check
  on the release pull request.
- **Don't tag a release on a commit whose gates were read on another commit.**
  Read the commit's full id from the tag, not from memory, at every gate.
- **Don't take a green run as proof that the tests ran.** Compare the skip
  sets (G3).
- **Don't split the version bump and the changelog section into two pull
  requests.**
- **Don't build from a stale checkout.** Fetch and check out the tag before
  any build step; the build stamps the local tree, not `origin`.
- **Don't infer a pushed tag or a moved branch from local state.** Read it
  back with `git ls-remote`.
- **Don't force-push `main` or try to move a release tag.** If either looks
  necessary, stop and bring it to the maintainer.
- **Don't report "released" after part 2.** Until the newsletter has gone out,
  the report says "repository released; website and newsletter pending".
