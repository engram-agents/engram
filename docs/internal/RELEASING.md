# Releasing ENGRAM

This is the procedure for cutting an ENGRAM release. It replaces the
two-repository procedure used through v0.5.5 (development in a private
repository, one copied snapshot per release in the public one). Since v0.5.6
development and releases happen in this repository, on one history. Branch and
tag names are defined in [`docs/VERSIONING.md`](../VERSIONING.md); where this
page and that one disagree about names, that page is right.

A release has three parts, in this order:

1. **Candidate.** A commit on `dev` is tagged `vX.Y.Z-rcN`, gated and lived on.
2. **Release.** The accepted candidate commit is tagged `vX.Y.Z`, `main` is
   fast-forwarded to it, and the Releases entry is created.
3. **Tell the public.** Website, then newsletter. A version is *released* only
   when all three public surfaces are live (definition below).

## Who does what

| Role | Does |
|---|---|
| Release seat | Runs this procedure, declares the freeze, pushes tags, moves `main`, writes the release record. One seat per release, named when the candidate is cut. |
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
- Candidate tags (`-rcN`) are public. The install guide and the installer pick
  only tags of the plain form `vX.Y.Z`, so a candidate is never installed by
  accident.
- There is no `dev` to `main` pull request. `main` moves only by a
  fast-forward to the release commit, so `main` and `dev` share one history and
  the release tag is a commit on both. A merge commit, a squash or a rebase
  would put a commit on `main` that `dev` does not have.
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

### 3. Freeze `dev` and tag the candidate

When that pull request has merged, read the tip and say it:

```bash
git fetch origin && git rev-parse origin/dev      # this is H
```

Post the freeze where the team reads orders: `dev` is frozen at `H` for
`vX.Y.Z`; nothing merges until the release tag exists, except a fix for a
release blocker named by the release seat. Then tag:

```bash
git tag -a vX.Y.Z-rcN H -m "vX.Y.Z-rcN"
git push origin vX.Y.Z-rcN
git ls-remote --tags origin vX.Y.Z-rcN            # read it back; local state proves nothing
```

A fix that merges during the freeze moves the tip. The next candidate is
tagged on the new tip and **every gate below runs again on it**. An approved
change that is not a release blocker waits for the next release.

### 4. Gates on the candidate commit

Every gate is read on the commit the candidate tag points at, and its result
goes into the release record (step 9) with the run's identifier.

| Gate | What must be true | Who |
|---|---|---|
| G1 | The hosted full suite is green on the candidate: `gh workflow run tests.yml --repo engram-agents/engram --ref vX.Y.Z-rcN`, then confirm the run's `headSha` is the candidate commit before reading its result. | Release seat |
| G2 | A whole-suite run on a fresh full clone checked out at the candidate tag, with the project's own interpreter, is green. The full output is kept. | Gate seat |
| G3 | The skip-set comparison below shows no unexplained new skip. | Gate seat |
| G4 | `name-leak-gate` is green on the candidate commit (it runs on the push to `dev`). `pr-checks` is green on the pull request whose merge produced the candidate, and that pull request's head tree equals the candidate's tree (`pr-checks` runs on pull requests only, so no run exists on the squash commit itself). The secret-scanning alert list has no open alert; this covers code and issue text, and it is a read at one moment: if scanning was enabled or the candidate was pushed within the hour, this part is "not run" until the list has settled. | Release seat |
| G5 | The security seat reads the net change `vPREV..vCAND`, the diff plus the list of merge commits, and records on the candidate commit, by its full id, either "no objection" or an objection naming the change and the reason. The record states what was read and its bound: files reviewed against files changed. If the security seat could not make the read, the gate is not run; it is not an implied pass. | Security seat |
| G6 | An install from a plain clone at the candidate tag into an empty home passes `install_selfcheck.py`; and an install over a copy of an existing home leaves every file of that home byte-identical or backed up. | Gate seat |
| G7 | On macOS, the tests that skip on Linux pass on a seat that runs a real Mac, on the candidate commit. | A Mac seat |

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
   pull request (step 9), so the next release can read it.

The release record states both skip counts and the list of differences, even
when that list is empty.

### 5. Live on the candidate

The team upgrades to the candidate and works on it. Each seat checks out the
candidate tag and rebuilds:

```bash
git fetch origin --tags && git checkout vX.Y.Z-rcN
tools/install-local-marketplace.sh
```

then runs the activation steps of the `engram-upgrade` skill. Anything that
surfaces is a fix on `dev` through the normal review path, followed by a new
candidate on the new tip and a fresh pass through step 4. There is no fix on
`main` and no fix on a tag.

The candidate is accepted when every gate is recorded and the seats that
upgraded have reported. How long the team lives on it is the maintainer's
call, stated when the candidate is cut; a fix for a defect that loses user data
does not wait for a soak it does not need.

## Part 2: the release

Nothing in this part happens before the maintainer's go.

### 6. Tag the release on the accepted candidate commit

```bash
git fetch origin --tags                            # a stale ref makes the next check pass falsely
C=$(git rev-parse 'vX.Y.Z-rcN^{commit}')          # the accepted candidate
git rev-parse origin/dev                           # must still equal C; if not, stop
git tag -a vX.Y.Z "$C" -m "vX.Y.Z"
git push origin vX.Y.Z
git ls-remote --tags origin vX.Y.Z
```

Release tags are protected: once pushed, a release tag cannot be moved or
deleted. Read `C` twice before pushing.

### 7. Fast-forward `main` to the release commit

Run this in one shell. `C` is set again here on purpose: with `C` empty the
push below would name no source, which git reads as a request to delete `main`.

```bash
git fetch origin --tags
C=$(git rev-parse 'vX.Y.Z^{commit}')               # from the release tag pushed in step 6
test -n "$C" || { echo "C is empty, stop"; exit 1; }
git merge-base --is-ancestor origin/main "$C" && echo "fast-forward is possible"
git push origin "$C":refs/heads/main               # never with --force
git ls-remote origin refs/heads/main               # must print C
```

If the fast-forward line prints nothing, `main` holds a commit `dev` lacks.
Stop and find out why; do not force.

Who may run this step: only an account in the repository's release team. Two
rulesets protect `main`. One forbids deletion and non-fast-forward pushes for
everyone, with no bypass. The other forbids any update of `main` except by the
release team. What the second one buys is narrow, and it should not be read as
more: another account can no longer publish unreleased code by moving `main`
to a commit of `dev`. It does not protect against a release-team account that
is itself compromised, and it does not protect `dev`. No ruleset can say
"`main` only ever equals a release tag"; the fast-forward check above is what
carries that, and the rulesets are the second layer.

The push to `main` starts `tests.yml` on `main`. That run is a confirmation
after the fact, not the gate; G1 was the gate.

### 8. Create the Releases entry

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

### 9. Unfreeze `dev` and write the release record

Post that the freeze is lifted. Then open one pull request into `dev` that
adds `docs/internal/release-records/vX.Y.Z.md` and the skip-set file of G3.
The record names: the release seat, gate seat and security seat; the candidate
tags and their commits; each gate with its run identifier and result; both
skip counts and the differences; the full ids of the release tag object and
the release commit; the time `main` moved; and anything that was not run.

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

*How surface (1) is made since v0.5.6:* steps 6 to 8 above (the release tag,
the fast-forward of `main`, and `gh release create`). Surfaces (2) and (3) are
unchanged and are specified under "Tell the public" below. Where the paragraph
above says "the cutover tool", read "steps 6 to 8"; where it or the sections
below point to *Announcing the public release*, read step 8.

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

## Anti-patterns

- **Don't move `main` by a pull request, a merge commit, a squash or a
  rebase.** Each puts a commit on `main` that `dev` does not have. `main` moves
  by fast-forward to the release commit and in no other way.
- **Don't merge into `dev` during the freeze.** Each merge moves the tip, and
  the candidate and every gate have to be redone on the new tip. An approved
  change that is not a release blocker rides the next release.
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
