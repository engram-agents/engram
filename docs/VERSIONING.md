# Versions, branches and repository history

This page explains how ENGRAM versions are named, which branch holds what, and
why this repository changed shape with v0.5.6. Read it if you cloned the
repository before v0.5.6, if `git pull` stopped working for you, or if you
wonder what the tags ending in `-history-twin` are.

## The short version

- Up to v0.5.5, this repository received **one commit per release**: a copy of
  the release's files, made from a separate development repository.
- With v0.5.6, the **development history itself** is published here (more than
  2,000 commits), and releases are cut directly from it.
- The old one-commit-per-release line is kept, unchanged, on the branch
  `release-snapshots`.
- Each old release also has a tag ending in `-history-twin`, on the commit in
  the history that the release came from.
- Because the history behind `main` was replaced, **a clone made before v0.5.6
  cannot `git pull`**. One short command line fixes it; see
  [If you cloned before v0.5.6](#if-you-cloned-before-v056).

## Branches

| Branch | What it holds |
|---|---|
| `main` | Releases. From v0.5.6 on, every release is a tagged commit on `main`. This is the default branch. |
| `dev` | Development. Changes are merged here first; `main` moves forward only when a release is made. |
| `release-snapshots` | The archive: the 17 commits that were `main` up to v0.5.5 (one per release for the 16 releases, and one extra commit from the v0.4.0 release). We do not add to it or change it. |

`main` and `dev` share one history. `release-snapshots` shares no commit with
them: it was made by copying files, so git sees it as a separate line.

## Tags

| Tag | Example | Meaning |
|---|---|---|
| `vX.Y.Z` | `v0.5.6` | A release. From v0.5.6 on, it is a commit on `main`. For v0.5.5 and earlier, it is a commit on `release-snapshots`. |
| `vX.Y.Z-rcN` | `v0.5.6-rc1` | A release candidate: a commit on `dev` that is being tested before it becomes the release. A release tag can sit on the same commit as its last candidate. |
| `vX.Y.Z-history-twin` | `v0.5.5-history-twin` | For each release up to v0.5.5: the commit in the development history that the release was made from. Defined exactly below. |

Version numbers follow [Semantic Versioning](https://semver.org/) below 1.0:
the middle number changes for a wave of new features, the last number for
fixes and small improvements. ENGRAM is in alpha; 1.0.0 will mark a
backward-compatibility commitment and has not been reached.

Release tags are protected on this repository: they cannot be moved or deleted.
That is why the old tags `v0.1.0` to `v0.5.5` still point at the archive
commits, and why the commits they correspond to in the history carry a separate
tag name.

The install guide picks the newest tag of the plain form `vX.Y.Z`. It never
picks a candidate or a twin.

## What "twin" means

`vX.Y.Z-history-twin` points at the development commit from which release
`vX.Y.Z` was made. For every one of the 16 releases, we checked the following,
and you can check it yourself with the commands in the next section:

1. **Every file of the release is in the twin**, at the same path.
2. **Nearly all of them are byte-for-byte identical.** At most two files per
   release differ (the last column of the table). They differ because the
   history was edited once before it was published; see
   [What was changed in the history](#what-was-changed-in-the-history).
   Across all 16 releases only three paths are affected:
   `src/engram/tools/forum_api.py`, `src/forum/tests/test_forum_template.py`
   and `docs/rc-upgrade-runbook.md`.
3. **The twin also contains files the release left out.** Releases up to
   v0.5.5 did not include the main test suite, internal design notes and
   development tools. The twin has them, so it holds more files than the release.

So a twin is not a second copy of the release. It is the place in the history
where that release came from.

The file check alone does not single out one commit. For most releases a few
neighbouring commits also contain every file of the release, because they
changed only files the release left out. The twin is the commit the release
was made from: the one that carried that release's candidate tag during
development. Four early releases (v0.1.0, v0.1.1, v0.1.3 and v0.1.4) have no
candidate tag that matches; for each of them exactly one commit in the
development history contains every file, and that commit is the twin. (The old
release commit on `release-snapshots` contains every file too, by construction;
it is not part of the development history.)

| Release | Release commit date | Release tag (on `release-snapshots`) | Twin tag (on `main` and `dev`) | Files in the release | Of those, changed at the twin |
|---|---|---|---|---|---|
| 0.1.0 | 2026-06-18 | `v0.1.0` → `b1eb1eac8` | `v0.1.0-history-twin` → `bfb020d84` | 331 | 0 |
| 0.1.1 | 2026-06-19 | `v0.1.1` → `116a3aab0` | `v0.1.1-history-twin` → `942b7734e` | 329 | 0 |
| 0.1.2 | 2026-06-24 | `v0.1.2` → `91000f51d` | `v0.1.2-history-twin` → `460db4b25` | 339 | 0 |
| 0.1.3 | 2026-06-25 | `v0.1.3` → `ee3b0dfd4` | `v0.1.3-history-twin` → `2943ec2ba` | 343 | 0 |
| 0.1.4 | 2026-06-26 | `v0.1.4` → `c0fda16b1` | `v0.1.4-history-twin` → `8843609b6` | 343 | 0 |
| 0.2.0 | 2026-07-05 | `v0.2.0` → `2f8c27ca8` | `v0.2.0-history-twin` → `07f753106` | 397 | 1 |
| 0.3.0 | 2026-07-11 | `v0.3.0` → `3f4e438ce` | `v0.3.0-history-twin` → `b7752c9ae` | 420 | 1 |
| 0.3.1 | 2026-07-19 | `v0.3.1` → `097330341` | `v0.3.1-history-twin` → `116e0d3b9` | 421 | 1 |
| 0.3.2 | 2026-08-05 | `v0.3.2` → `f9eaeeed5` | `v0.3.2-history-twin` → `ea866df7a` | 430 | 2 |
| 0.4.0 | 2026-08-20 | `v0.4.0` → `3daf71edb` | `v0.4.0-history-twin` → `21b615523` | 443 | 2 |
| 0.5.0 | 2026-09-02 | `v0.5.0` → `8dfc6112d` | `v0.5.0-history-twin` → `2a65a6952` | 487 | 2 |
| 0.5.1 | 2026-09-19 | `v0.5.1` → `3f1405697` | `v0.5.1-history-twin` → `00d5803bb` | 556 | 2 |
| 0.5.2 | 2026-10-01 | `v0.5.2` → `7ce537759` | `v0.5.2-history-twin` → `5fe8e102a` | 534 | 1 |
| 0.5.3 | 2026-10-21 | `v0.5.3` → `274716a95` | `v0.5.3-history-twin` → `ca15490f2` | 620 | 1 |
| 0.5.4 | 2026-10-23 | `v0.5.4` → `dd67530dd` | `v0.5.4-history-twin` → `b393e686f` | 626 | 2 |
| 0.5.5 | 2026-10-29 | `v0.5.5` → `6d8a76419` | `v0.5.5-history-twin` → `9b23ea4f9` | 570 | 1 |

The date is that of the release commit in this repository. For some releases
the heading in [CHANGELOG.md](CHANGELOG.md) carries a date a few days earlier.
All 16 twins are commits on both `main` and `dev`.

## How to compare versions

See what changed between two releases, as development commits:

```bash
git log --oneline v0.5.4-history-twin..v0.5.5-history-twin
```

See the same change as a diff:

```bash
git diff v0.5.4-history-twin v0.5.5-history-twin
```

From the last old-style release to the first new-style one:

```bash
git log --oneline v0.5.5-history-twin..v0.5.6
```

Check a twin against its release yourself. `M` lines are files that differ,
`A` lines are files only the twin has; there are no `D` lines, because no file
of the release is missing from its twin:

```bash
git diff --no-renames --name-status v0.5.5 v0.5.5-history-twin | grep -v '^A'
```

Do not compare an old release tag directly with a new one
(`git diff v0.5.5 v0.5.6`). It works, but it also lists every file the old
releases left out as "added", which hides the real changes. Use the twin.

For a description of each release in words, read
[CHANGELOG.md](CHANGELOG.md).

## If you cloned before v0.5.6

Your clone's `main` points at the old line of release commits. After
`git fetch`, git reports that your branch and `origin/main` have diverged, and
`git pull` stops with:

```
fatal: Need to specify how to reconcile divergent branches.
```

That is the message with git's default settings. With other pull settings you
may see something else:

- **Fast-forward only:** `fatal: Not possible to fast-forward, aborting.`
- **Merge** (`pull.rebase` set to `false`):
  `fatal: refusing to merge unrelated histories`. Do not add
  `--allow-unrelated-histories`; the two lines have nothing in common and
  should not be merged.
- **Rebase:** the pull may simply succeed and leave you on the new `main`, with
  your own commits (if you had any) on top; then there is nothing more to do.
  Or it may stop part-way with `CONFLICT` and `Could not apply` lines. If it
  does, run `git rebase --abort` before anything else.

Whatever your setting, these three commands move your `main` to the new one.
The first gives a name to anything you committed yourself, so that nothing is
left behind; it does no harm if you committed nothing:

```bash
git branch my-old-main
git fetch origin
git checkout -B main origin/main
```

Uncommitted edits to files that are the same on both lines are carried along.
If git refuses, read which list it prints:

- **Files git does not track** ("untracked working tree files would be
  overwritten"): move them somewhere else and run the command again. The new
  line has many files the old one did not, so a file of your own can sit where
  a new file now belongs.
- **Your own edits to tracked files** ("your local changes ... would be
  overwritten"): run `git stash`, run the command again, then `git stash pop`
  to get the edits back.

Do not use `--force` or `checkout -f`: that discards your files.

Commits of your own stay on `my-old-main`, on top of the old release line,
which remains available as `origin/release-snapshots`. If you ran the last
command without the first, they are not lost: `git reflog` lists them, and
`git branch my-old-main <hash>` gives them a name.

Then continue the upgrade as usual (ask your agent to upgrade ENGRAM, or run
the upgrade steps in [README-AGENT.md](../README-AGENT.md)). One thing looks
different this once: the step that lists what changed since your installed
version will list the whole history, more than 2,000 commits, because your old
version is not part of the new line. To see what really changed, use
`git log --oneline v0.5.5-history-twin..v0.5.6`, or read the v0.5.6 section of
[CHANGELOG.md](CHANGELOG.md). Your graph, history
and settings live outside the clone, in your ENGRAM home directory, and are not
touched by any of this.

If your clone sits on a tag instead of a branch, fetch and check out the new
release tag:

```bash
git fetch origin --tags
git checkout v0.5.6
```

## Installing an older release

We support installing the current release. The old release tags stay in the
repository for reading and comparing.

The install script of v0.5.5 (`tools/install.sh`) checks that the commit it
runs from is a release tag on `main` or `dev`. After the change the `v0.5.5`
tag is on neither, so the script stops at that check and says the commit "is
not a deployable tag merged into origin/dev or origin/main". The script has an
option that skips the check, `--sandbox-untagged`; its own help describes it as
for sandbox rigs only, not a release. Older releases have older install scripts
that may behave differently. We have not tested any old release installed after
the change.

## What was changed in the history

The development history was written in a private repository. Before it was
published here it was rewritten once, to replace a small number of private
values (for example machine names and personal mail addresses) with neutral
ones, in files, in commit messages and in author fields, and to remove one file
that should not be public. Three consequences are
visible:

- **Commit hashes here differ** from the hashes of the same commits in the
  private repository. A hash quoted in an old discussion may not exist here.
- **A few files differ from the old releases**, as listed above: the same
  values were replaced in them.
- **Numbers such as `(#4572)` in commit messages up to commit `80108889`
  refer to pull requests and issues of the private repository.** They are not
  this repository's issue or pull-request numbers, and a link GitHub draws from
  such a number points at an unrelated item or at nothing. Numbers in later
  commits refer to this repository.

## From v0.5.6 on

- Development happens on `dev`, in this repository.
- A release candidate is a tag `vX.Y.Z-rcN` on `dev`.
- A release moves `main` forward to the tested commit and tags it `vX.Y.Z`.
  `main` is protected on this repository against being rewritten or deleted,
  so from here on `git pull` on `main` keeps working from one release to the
  next.
