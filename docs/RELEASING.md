# Releasing ENGRAM

engram-alpha is a **private** repo. There is a separate **public** repo,
[`engram-agents/engram`](https://github.com/engram-agents/engram), that holds
user-facing release snapshots — one squashed commit per version. A release
has two stages:

1. **Private rc cut** — `dev → main` in this (private) repo, full CI gate,
   tagged `vMAJOR.MINOR.PATCH-rc<N>` on `main`.
2. **Public release** — after a dogfood window on the rc, `tools/release-cutover.py`
   publishes a clean, leak-scrubbed squashed snapshot to the public repo,
   tagged with the **clean** `vMAJOR.MINOR.PATCH` (no `-rc`).

Both stages are documented below.

> **This doc was rewritten 2026-07-02 from the first real end-to-end `dev→master`
> cut (`v0.2.0-rc2`).** That cut surfaced nine process gaps the previous
> dev-only doc didn't cover — each is now folded in below. The load-bearing
> lesson: the full CI gate runs *only* on the release PR, so the release cut is
> the first time many things are exercised end-to-end. Expect it to catch real
> issues (the rc2 cut caught two pre-existing test failures latent on `dev`);
> that is the gate working, not a setback.

## Version scheme

Semver pre-1.0, split by which repo the tag lives in:

- **Private (`main`, this repo)**: `vMAJOR.MINOR.PATCH-rc<N>` — e.g.
  `v0.2.0-rc1`, `v0.2.0-rc2`. `<N>` increments if a fix round is needed before
  the cut is ready to promote publicly (e.g. `v0.1.3-rc1` → `v0.1.3-rc2`).
  Applies to any release regardless of MAJOR/MINOR/PATCH size — the `-rc`
  suffix marks "cut to main, under dogfood validation," not a version-size
  class.
- **Public (`engram-agents/engram`)**: clean `vMAJOR.MINOR.PATCH` — e.g.
  `v0.2.0`, `v0.1.3`. **No `-rc` suffix ever reaches the public repo.**
  `tools/release-cutover.py`'s `VERSION_RE` (`^v\d+\.\d+\.\d+$`) enforces this
  mechanically — it will reject a `--version` argument carrying `-rc` or any
  other suffix.

Historical private tags (for calibration — no `-alpha` anywhere): `v0.1.0`,
`v0.1.2-rc1`, `v0.1.2-rc2`, `v0.1.3`, `v0.1.3-rc1`, `v0.1.3-rc2`, `v0.1.4`,
`v0.2.0-rc1`, `v0.2.0-rc2`. *(`v0.1.3` is a clean, no-`-rc` private tag —
it predates this two-repo model; a `git tag -l` will show it. Since the split,
private `main` tags always carry `-rc<N>`.)*

When MINOR/MAJOR bumps happen: MINOR for feature waves (new node type, new
hook architecture, new MCP-tool category), PATCH for bugfixes + small
improvements within a wave. MAJOR bump to `v1.0.0` marks promotion out of
alpha to a backward-compatibility commitment — not yet reached.

## Branch model

- **`dev`** is the active development branch. All feature/fix PRs merge here.
  Dev PRs run only the cheap `pr-checks` (combined static scrubs) — the **full pytest suite does NOT run on dev PRs**
  (run affected-tests **locally** as a review requirement; at agent-PR
  velocity, per-PR test CI is too expensive). See `CLAUDE.md`.
- **`dev` is a protected branch — direct pushes are rejected in practice.**
  Every change to `dev`, including release bookkeeping (CHANGELOG updates,
  version bumps), goes through a PR like any other change. There is no "just
  commit it directly to dev" shortcut for release prep.
  *(Mechanism note, 2026-07-02: a direct push to `dev` is declined
  ("protected branch hook declined"), but the repo-level ruleset "Protect dev
  branch" shows `enforcement: disabled` and `rules/branches/dev` returns no
  active rules — so the enforcement is almost certainly an **org-level ruleset**
  on `engram-agents`, invisible without org-admin. Don't be misled by the
  repo-level API showing "disabled": the empirical behavior is what governs —
  always PR.)*
- **`main`** holds tagged release points only. A release is cut by opening a
  **PR from `dev` → `main`**, which fires the **full CI gate** (`tests.yml`
  full suite + `pr-checks`) — the thorough pre-release
  validation. Routine PRs do NOT target main (see `CLAUDE.md` Branching
  convention section).
- **The `dev→main` PR merges as a MERGE COMMIT, not a fast-forward or a
  squash.** The merge commit on `main` has two parents (previous `main` +
  the `dev` tip), so `dev`'s content — and its tip as a parent — is captured on
  `main`, and the release tag points at that merge commit. This requires
  **"Allow merge commits" to be enabled** in the repo's merge settings (see the
  merge-settings section below); a squash-merge would collapse the history and
  drop the ancestry link, and is wrong for a release cut.
- **Private release tags** (`vMAJOR.MINOR.PATCH-rc<N>`) point at that `main`
  merge commit. **Public release tags** (`vMAJOR.MINOR.PATCH`, no `-rc`) live in
  the separate `engram-agents/engram` repo, cut from a validated rc — see
  "Publishing to the public repo" below.

### Repo merge settings — must allow merge commits

The `dev→main` release model needs a **merge commit** (above). GitHub merge
settings are **repo-wide**, so confirm **"Allow merge commits"** is enabled in
Settings → General → Pull Requests before cutting:

```bash
gh api repos/engram-agents/engram-alpha --jq '{merge: .allow_merge_commit, squash: .allow_squash_merge, rebase: .allow_rebase_merge}'
```

`merge: true` is required. *(Gap hit live on the `v0.2.0-rc2` cut: the repo was
**squash-only** (`allow_merge_commit: false`), which is fine for keeping `dev`
history linear but silently incompatible with the release model — GitHub offered
only squash for the `dev→master` PR, which would have dropped `master`'s ancestry
and left the next cut still conflicting. Fix: an admin enabled merge commits.
Policy after: squash for routine `dev` PRs (discipline — keeps `dev` linear),
merge-commit for the release PR.)*

## Keeping `main` and `dev` from diverging

`main` and `dev` must not accumulate **independent** history, or the
`dev→main` release PR conflicts. The failure mode is a fix authored *separately*
on each branch (two different commits touching the same lines) — that is a real
merge conflict. The disciplines that prevent it:

- **All changes land on `dev` first**, via the normal PR workflow — including
  fixes. `main` only advances via the `dev→main` release merge. If you never
  commit directly to `main`, `main` stays an ancestor of `dev` and the
  release PR never conflicts. This is the default; prefer it.
- **A hotfix needed on an already-released `main`** (can't wait for the next
  full cut): author the fix on `dev` first (normal PR), then **cherry-pick that
  commit onto `main`** via a hotfix PR + a patch-tag bump. Cherry-pick copies
  the fix's content *identically* to `main`, so the next `dev→main` merge
  sees the same change already applied on both sides → **no conflict**. (It does
  create a new SHA on `main`, so the merge still produces a merge commit rather
  than a fast-forward, but it won't conflict — which is the part that hurts.)
  This is strictly better than authoring the hotfix independently on `main`.
- **Fallback — if a fix ever *does* land directly on `main`** (emergency,
  bypassing dev-first): **immediately `git merge main → dev`** (via a PR) to
  re-sync, before `dev` accumulates more and the divergence compounds.

*(One-time reconciliation, 2026-07-02: before the `v0.2.0-rc2` cut, `master` had
diverged from `dev` by 8 un-back-merged hotfixes (the `v0.1.3`/`v0.1.4` line),
authored independently on `master`. The `v0.2.0-rc2` cut reconciled this once —
merged `origin/master` into `dev` (a merge commit preserving `master`'s ancestry),
so `master` became an ancestor of `dev` and the release PR went clean. Future cuts
start from that clean-ancestor state; the disciplines above keep it that way.)*

## Cutting a release (private rc cut)

These steps assume `dev` is at the SHA you want to release and CI is green.

**Before you start:**
- `git pull` (fast-forward your local `dev`/`main` to `origin`) before running
  any build tooling (`tools/install-local-marketplace.sh` etc.) as part of this
  process. The build stamps whatever is in your **local working tree** — it does
  not fetch or check freshness against `origin`. A stale local checkout silently
  ships a build that is behind what was actually merged (hit live during the
  `v0.2.0-rc2` cut: a build went out one commit behind `origin/dev`).
- Confirm **"Allow merge commits"** is enabled (see "Repo merge settings" above).

### 1. Choose the version

Look at the changes since the previous release tag:

```bash
git log --oneline <previous-tag>..dev
```

Group by theme. Pick the next version per the scheme above (MINOR for
feature waves, PATCH for fixes). Decide the rc suffix (`-rc1` for the first
cut attempt at this version; bump to `-rc2`, `-rc3`, ... if a fix round is
needed before the version is ready to promote to the public repo).

### 2. Bump the plugin version (SSoT)

Bump root `plugin.json`'s `"version"` field to the target `MAJOR.MINOR.PATCH`
(no `-rc` suffix here — `plugin.json` carries the clean base version).

```json
{
  "name": "engram",
  "version": "0.2.0",
  ...
}
```

This is the single source of truth the build reads: `compute_build_version()`
(`src/build/engine/build.py`) loads `plugin.json`'s `"version"` as the base
and stamps every build artifact from it. **Skipping this step means every
build in this release cycle mis-stamps its version** — this is the gap that
let a stale `0.1.0` hardcode ship unnoticed for a while (reported in #1607,
fixed by #1623). Bump this in the same PR as the CHANGELOG update (step 3).

### 3. Update the CHANGELOG

The changelog lives at **`docs/CHANGELOG.md`** (not root `CHANGELOG.md`).

Before cutting, **prune `[Unreleased]`**: check it against what's already
shipped in a previous release (diff against `origin/main`'s CHANGELOG
section, or the last release tag) and remove anything that's actually already
released — the `[Unreleased]` section should only ever contain changes that
landed on `dev` since the last cut. (This file has drifted before — stale
already-shipped entries left in `[Unreleased]`, and some patch releases
never got their own `## [vX.Y.Z] - DATE` section. Check for both when you
touch this file. If `main` has release sections `dev` lacks — from hotfixes
— **union** them in when reconciling, don't drop either side.)

Move the `Unreleased` section to a new versioned section: `## [vMAJOR.MINOR.PATCH]
- YYYY-MM-DD` (use the **clean** version here, no `-rc` — the CHANGELOG
describes the release content, not the private rc cycle it took to validate
it). Add a fresh `Unreleased` heading above it for the next cycle.

**This goes to `dev` via a PR, like any other change** — `dev` is a
protected branch; there is no direct-push path. Bundle the `plugin.json`
version bump (step 2) into the same PR:

```bash
git checkout -b docs/changelog-vMAJOR.MINOR.PATCH
git add docs/CHANGELOG.md plugin.json
git commit -m "docs: CHANGELOG + version bump for vMAJOR.MINOR.PATCH"
git push -u origin docs/changelog-vMAJOR.MINOR.PATCH
gh pr create --base dev --head docs/changelog-vMAJOR.MINOR.PATCH \
  --title "docs: CHANGELOG + version bump for vMAJOR.MINOR.PATCH"
```

Get this PR reviewed and merged into `dev` through the normal review path
(reviewer-fairy / colleague, per `CLAUDE.md`) before proceeding to step 4.

*__Note on the initial cut:__ when cutting the very first release, the
`Unreleased` section starts empty. Survey the historical commits since
project inception (e.g. `git log --oneline --no-merges`) and populate
`Unreleased` with notable changes before moving to step 4. Subsequent cuts
accumulate the changelog incrementally as PRs land.*

### 4. Open the release PR (dev → main) — the full CI gate

Cut the release by opening a **PR from `dev` → `main`** (not a direct
push). This PR **is** the release gate: the full `tests.yml` suite +
`pr-checks` run on it — the thorough validation that
deliberately does NOT run per-dev-PR.

```bash
gh pr create --base main --head dev \
  --title "release: vMAJOR.MINOR.PATCH-rc<N> — short summary" \
  --body "Release cut to main. The full CI gate runs on this PR."
```

**If the release PR is CONFLICTING** (`mergeStateStatus: DIRTY`): `main` has
diverged from `dev` (independent commits on `main`). Reconcile it once — merge
`origin/main` into `dev` on a branch (a **merge commit**, so `main` becomes
an ancestor of `dev`; union any CHANGELOG sections), PR that to `dev`, then the
release PR goes clean. See "Keeping `main` and `dev` from diverging" above.

**If CI is red on the release PR**: treat it exactly like any blocked PR — fix
on `dev` via a normal PR, and the release PR re-runs the full gate on the new
tip. Note that the fix PR (a `dev` PR) runs **only `pr-checks`, not the full
suite** — so **run the affected tests locally** and paste the result in the
review before merging; the full-suite verification happens when the release PR
re-runs, not on the fix PR. Never tag on red.

Once the full gate is green **and** a colleague has given a fresh-eye approval,
merge the PR **as a merge commit** and tag the release SHA on `main`:

```bash
gh pr merge <PR-number> --merge        # MERGE COMMIT — not --squash
git fetch origin main
git tag -a "vMAJOR.MINOR.PATCH-rc<N>" -m "vMAJOR.MINOR.PATCH-rc<N> — short summary" origin/main
git push origin "vMAJOR.MINOR.PATCH-rc<N>"
git ls-remote --tags origin "vMAJOR.MINOR.PATCH-rc<N>"   # verify the tag landed on origin (not local HEAD)
```

### 5. Publish the GitHub Release (private repo)

**A tag push does NOT create a Releases entry here either — create it explicitly.**
This section claimed the opposite until 2026-08-27; the public-repo half of the
same mistake is documented under *Announcing the public release* below. Measured
on this private repo 2026-08-27: **16 tags, 3 releases** — eleven version tags
(`v0.1.0`, `v0.1.3`, `v0.1.4`, `v0.2.0-rc1/rc2`, `v0.3.2`, `v0.3.2-rc1` among
them) carry no entry at all. Those are the releases nobody noticed were missing,
which is what the false claim cost. Both figures are a snapshot supporting the
sentence above them, not a live count.

Create it:

```bash
gh release create <tag> --repo engram-agents/engram-alpha \
  --notes-file <changelog-section> --prerelease
```

`--prerelease` is the flag, not a follow-up edit: an rc is a pre-release because
the public repo's clean tag is the non-prerelease surface. Then open the entry and
confirm two things render — the CHANGELOG notes you passed, and the source-code
tarball link GitHub attaches on its own.

## Dogfood-before-public

**Do not cut the public release immediately after the rc tag.** The rc tag on
`main` is the validation window: the whole agent family upgrades to the rc
and dogfoods it (at least a day) before anyone runs `release-cutover.py`
against the public repo. Concretely:

- Every agent (and human) on the rc pool runs
  `git pull origin main && tools/install-local-marketplace.sh` (or checks
  out the rc tag) to get onto the new build.
- Normal work continues on this build; anything that surfaces (a broken
  hook, a mis-stamped version, an unexpected migration issue) is a **fix
  round on `dev`**, followed by a new `-rc<N+1>` cut (repeat from step 1
  above with the incremented rc suffix) — not a hotfix to a public tag that
  doesn't exist yet.
- Only once the rc has held up under real use does it graduate to a public
  cut (below). The clean `vMAJOR.MINOR.PATCH` public tag is the artifact of
  a *validated* rc, not the artifact of a green CI run alone.

## Publishing to the public repo

Once the rc has dogfooded clean, publish it to the public repo,
[`engram-agents/engram`](https://github.com/engram-agents/engram). The public
repo carries **one squashed commit per version** — it is a clean, leak-scrubbed
release snapshot, not a mirror of this repo's full history.

### One-time setup

Clone the public repo locally somewhere outside this checkout — this is the
`--target` for the cutover tool:

```bash
git clone git@github.com:engram-agents/engram.git /path/to/public/engram
```

### Running the cutover

`tools/release-cutover.py` produces the snapshot and publishes it. Per its
own docstring, it:

1. `git-archive`s the source ref into a temp dir (tracked files only).
2. Applies the minus-set (`release-minus-set.txt`) — drops excluded paths.
3. Runs the scan-leaks gate — **fail-closed** on any leak.
4. Runs sanity gates — LICENSE + README disclaimer present.
5. Publishes to the public target repo as **one squashed commit + annotated
   tag**.
6. Optionally pushes (`--push` flag; **default off**).

```bash
python3 tools/release-cutover.py --version v0.2.0 --target /path/to/public/engram
```

Key flags (from `--help`):
- `--version` — the **clean** release version, e.g. `v0.2.0`. Required.
  `VERSION_RE` (`^v\d+\.\d+\.\d+$`) rejects anything with a suffix — no
  `-rc`, no `-alpha`.
- `--target` — path to the local clone of the public repo. Required.
- `--source` — path to the source repo (this repo). Defaults to walking up
  from the script location to the enclosing `.git` root; pass it explicitly
  only if that heuristic misfires (e.g. running from an unusual checkout).
- `--source-ref` — git ref to snapshot (tag/branch/sha); default `HEAD`.
  Point this at the validated **rc tag** on `main`
  (`--source-ref v0.2.0-rc2`), not just whatever `HEAD` happens to be.
- `--minus-set` — path to the minus-set file; default
  `<source>/release-minus-set.txt`. **This file is the only hand-maintained
  input to the whole cutover** — everything else the tool derives. It
  currently excludes the dev workspace (`CLAUDE.md`, `.claude/`),
  persona-specific dev tools, the `tests/` tree, and `.github/` (private CI).
  Review it if new private-only paths appear.
- `--leak-roster` — path to `.scan-leaks-local.json` for the personal-name
  pattern gate; default `<source>/.scan-leaks-local.json` if present.
- `--author` — commit author; defaults to whatever's parsed from the LICENSE
  copyright line.
- `--push` — push the branch + tag to the target's origin after committing.
  **Default off.**
- `--yes` — skip the interactive confirm before writing to target.
- `--force` — if the version tag already exists in target, delete and re-tag
  (does not force-push; push manually after).

### Dry-run first, then push

**Run without `--push` first.** The tool writes the squashed commit + tag to
your local clone of the target repo and prints a summary (file counts,
minus-set removals, the scan-leaks result, LICENSE/README gate results)
without touching the remote. Review that output — especially the scan-leaks
section — before pushing:

```bash
# 1. Dry run — writes locally to the target clone, does not push.
python3 tools/release-cutover.py --version v0.2.0 --target /path/to/public/engram \
  --source-ref v0.2.0-rc2

# 2. Inspect the target clone: git log, git show, review the scan-leaks
#    output printed above for anything unexpected.

# 3. Once satisfied, re-run with --push (or push the target clone manually).
python3 tools/release-cutover.py --version v0.2.0 --target /path/to/public/engram \
  --source-ref v0.2.0-rc2 --push
```

The scan-leaks gate is **fail-closed**: any hit aborts before the target is
touched. Per the tool's own docstring, it is *necessary-but-not-sufficient*
— it skips extensionless files and doesn't carry all personal-name patterns
unless `.scan-leaks-local.json` is configured, so a clean scan-leaks pass is
not a substitute for actually reading the dry-run output.

### Announcing the public release

After `--push`, the public repo has the clean `vMAJOR.MINOR.PATCH` tag.

**⚠️ A tag push does NOT create a Releases entry — you must create it
explicitly.** This document claimed otherwise until 2026-08-20, when the
v0.4.0 cut proved it: the tag pushed and no entry appeared, and the release
had to be created by hand with the CHANGELOG section as its notes. Measured
on the public repo the same day: **10 tags, 9 releases** — `v0.1.0` still
carries a tag with no release, which is the residue of exactly this
mistake. Create it with `gh release create <tag> --repo engram-agents/engram
--notes-file <changelog-section>` and confirm it renders before announcing
anything that links to it.
Notify users (see below) pointing at the **public** repo's release page, not
the private rc.

## Notify users

For alpha-tester pool (agents, users, or counterparts on other hosts):
- Brief release announcement summarizing what's in the version
- Pointer to the public GitHub Release page (`engram-agents/engram`)
- Upgrade instruction: `git pull origin dev && tools/install-local-marketplace.sh`,
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
under *Announcing the public release* above (the subsection carrying the
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
  the private rc, and to the Versions entry from step 1.

**Do NOT hand-roll an unsubscribe link.** Buttondown states that every email
it sends is one-click-unsubscribe compliant (RFC 8058) and injects the headers
itself. Adding your own produces two unsubscribe links, one of which does not
work.

**CAN-SPAM postal address: CHECKED, and the answer is that it is missing.**
CAN-SPAM requires a valid physical postal address in commercial email.
Buttondown's docs say *"Buttondown is compliant with the CAN-SPAM Act"*, but
that is a claim about the platform, **not** a promise that it inserts *your*
address into *your* sends. Measured on the delivered v0.4.0 newsletter
(2026-08-21): the footer carries the issue line, subscribe / unsubscribe /
view-online links, and a "Powered by Buttondown" line — **no postal address
anywhere in the message.** So the unsubscribe half of CAN-SPAM is satisfied and
the postal half is not.

**What that means for you:** nothing today, because the subscriber list is us.
It becomes a **hard blocker on the first send that reaches a genuinely external
subscriber**, and the fix is configuration — set a mailing address in the
Buttondown newsletter settings — not code. Check that it is set before that
send, not after.

*Method note, because it decides how you re-check this: the API's footer field
returns empty, which is equally consistent with "no address" and with "the
platform injects one at send time outside that field." Only a delivered email
in a real inbox discriminates between the two. Re-verify by reading a send, not
by reading the API.*

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

## After the cut

- Record the release in ENGRAM via `engram_add_observation` (the tag SHA +
  `main` state make good `hard_data` evidence).
- Append the milestone to `~/.engram/history/YYYY-MM-DD.md`.
- Continue normal dev work on `dev`. `main` sits at the new tag until the
  next cut.

## Anti-patterns

- **Don't squash-merge the `dev→main` release PR.** Use `--merge` (a merge
  commit). Squash collapses the release history into one commit and drops the
  ancestry link to `dev`, so the *next* cut conflicts. (This needs "Allow merge
  commits" enabled — see Repo merge settings.)
- **Don't author a fix independently on `main`.** That is what makes
  `main` and `dev` diverge. Fixes go to `dev` first; a needed `main`
  hotfix is a **cherry-pick** of the `dev` commit (identical content → no future
  conflict). See "Keeping `main` and `dev` from diverging."
- **Don't skip the CHANGELOG update.** Users read this; future-self also
  reads this to remember what was in each version. When reconciling with
  `main`'s hotfix sections, **union** — never drop a side.
- **Don't tag while CI is red.** A tag points at a specific SHA; if that
  SHA's tests fail, users hit broken state. The full suite runs *only* on the
  release PR, so this is the one place it's checked — take it seriously.
- **Don't trust the fix-PR's green as full-suite green.** A `dev` fix PR runs
  only `pr-checks`; run the affected tests locally, and let the release PR's
  re-run be the full-suite verification.
- **Don't push release bookkeeping directly to `dev`.** It's protected in
  practice — the CHANGELOG update and version bump go through a PR like any
  other change.
- **Don't build from a stale local working tree.** `git pull` before running
  `install-local-marketplace.sh` (or any build step) — the build stamps your
  local checkout, not `origin`.
- **Don't skip the dogfood window.** The rc tag exists to be lived with
  before it's promoted to a public, clean-tagged release — don't run
  `release-cutover.py` the same day as the rc cut.
- **Don't let a `-rc` suffix reach the public repo.** `release-cutover.py`'s
  `VERSION_RE` will reject it, but don't rely on the gate — the version
  passed to `--version` should already be the clean `vMAJOR.MINOR.PATCH` you
  intend to publish.
- **Don't infer a pushed tag from local state.** Verify with
  `git ls-remote --tags origin <tag>` — local `HEAD` / `-q` silence is not
  proof the tag reached `origin`.
