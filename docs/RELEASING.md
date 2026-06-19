# Releasing ENGRAM

## Version scheme

ENGRAM uses semver pre-1.0 with an `-alpha` suffix while in alpha:

- `v0.1.0-alpha` — first tagged release
- `v0.MINOR.0-alpha` — significant feature waves (e.g. new node type, new hook architecture, new MCP-tool category)
- `v0.MINOR.PATCH-alpha` — bugfixes + small improvements within a feature wave

When alpha graduates to beta or stable:
- Drop the `-alpha` suffix when promoting from alpha → beta
- Bump to `v1.0.0` when promoting to stable (the major version commits to backward compatibility)

## Branch model

- **`dev`** is the active development branch. All PRs merge here. CI runs against dev.
- **`master`** holds tagged release points only. Fast-forwarded to specific dev SHAs at release-cut time. Routine PRs do NOT target master (see `CLAUDE.md` Branching convention section).
- **release tags** (`v0.MINOR.PATCH-alpha`) point at the same SHA master is fast-forwarded to.

## Cutting a release

These steps assume `dev` is at the SHA you want to release and CI is green.

### 1. Choose the version

Look at the changes since the previous release tag:

```bash
git log --oneline <previous-tag>..dev
```

Group by theme. Pick the next version per the scheme above (MINOR for feature waves, PATCH for fixes).

### 2. Update CHANGELOG.md

Move the `Unreleased` section to a new versioned section with the chosen tag + today's date in `YYYY-MM-DD` format. Add a fresh `Unreleased` heading above it for the next cycle. See `CHANGELOG.md` for the keep-a-changelog format.

Commit the changelog update directly to dev (or via PR if you prefer review).

From engram-alpha root:

```bash
git commit -am "docs: CHANGELOG for v0.MINOR.PATCH-alpha"
```

*__Note on the initial cut:__ when cutting v0.1.0-alpha, the `Unreleased` section starts empty. Survey the historical commits since project inception (e.g. `git log --oneline --no-merges`) and populate `Unreleased` with notable changes before moving to step 3. Subsequent cuts accumulate the changelog incrementally as PRs land.*

### 3. Fast-forward master + tag

From engram-alpha root:

```bash
DEV_SHA=$(git rev-parse dev)
git checkout master
git merge --ff-only "$DEV_SHA"
git tag -a "v0.MINOR.PATCH-alpha" -m "v0.MINOR.PATCH-alpha — short summary"
git push origin master --tags
git checkout dev
```

If `--ff-only` fails, master has diverged from dev — investigate before forcing.

### 4. Publish GitHub Release

GitHub auto-creates a Releases entry when you push a tag. Edit it to:
- Copy the relevant CHANGELOG section into the release notes
- Mark as "Pre-release" while in alpha
- Confirm the auto-generated source-code tarball link is present (this is what zip-download users grab)

### 5. Notify users

For alpha-tester pool (agents, users, or counterparts on other hosts):
- Brief release announcement summarizing what's in the version
- Pointer to the GitHub Release page
- Upgrade instruction: `git pull origin dev && tools/install-local-marketplace.sh`, then `/plugin marketplace update engram-local`, then /plugin -> Installed -> engram plugin -> Update now, inside Claude Code (or git checkout master at the tag if they prefer pinning)

For on-host agents, surface in `ask-{{USER_NAME}}.md` after the cut.

## After the cut

- Record the release in ENGRAM via `engram_add_observation` citing `.deployed-version`.
- Continue normal dev work on `dev`. `master` sits at the new tag until the next cut.

## Anti-patterns

- **Don't tag from master after master is stale.** Tags should always point at SHAs reachable from dev's history — fast-forward master first, then tag.
- **Don't backport fixes to master without a tag bump.** Master only moves at release-cut time; routine fixes go to dev and ship in the next cut.
- **Don't skip the CHANGELOG update.** Users read this; future-self also reads this to remember what was in each version.
- **Don't tag while CI is red.** A tag points at a specific SHA; if that SHA's tests fail, users hit broken state.
