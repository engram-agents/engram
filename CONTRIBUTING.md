# Contributing to ENGRAM

Thank you for helping. This page is short on purpose.

## Branches

- Open pull requests against `dev`.
- `main` holds releases. It is GitHub's default branch, so a plain clone lands
  on a tagged release. Do not open routine pull requests against it.

## Before you start

- For anything large, open an issue first and say what you plan to change.
- Small fixes and doc corrections can go straight to a pull request.
- Run the tests that touch your change and say what you ran in the pull request.

## Pull requests from a fork

- A fork pull request gets a red check named
  `name-leak-gate (NOT SCANNED - fork PR)`. This is expected. Fork pull requests
  never receive the secret that check needs, so it cannot run.
  This check is not a required status check (the `dev` ruleset requires only
  `pr-checks`), so on its own it does not block a merge.
- Workflow runs on pull requests from forks may wait for a maintainer to
  approve them before they start.
- It does not mean your change is wrong. A maintainer reads your branch's
  workflow and code changes, then re-pushes it as a branch in this repository
  so the check can run.
- Why this is done, and how: `docs/internal/name-leak-gate-trusted-run.md`.

## Review and merge

- A pull request needs one approval and a passing `pr-checks`.
- A maintainer merges it. Squash merges use the pull request title.
- If your pull request closes an issue and the issue stays open after the
  merge, the maintainer closes it by hand.
