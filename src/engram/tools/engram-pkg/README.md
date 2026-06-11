# engram-pkg — Authoring CLI for engram-packages

The toolkit for authoring and editing engram-package databases (e.g., `engram-wiki`). Operates on any package directory passed via `--pkg <dir>` or `$ENGRAM_PKG`. Wraps `EngramClient` (which atomically redirects all server paths to the package directory) so you can write into the package without touching your personal engram at `~/.engram/`.

## What is an engram-package?

An engram-package is a portable, read-only ENGRAM knowledge graph that any agent can clone and consume alongside its personal graph. The package contract is:

| File / dir | Role | Versioned? |
|---|---|---|
| `knowledge.sql` | Source of truth — schema + all node/edge data, written by `scripts/dump.sh` | **YES** |
| `knowledge.db` | Build artifact — SQLite database produced by `scripts/build.sh < knowledge.sql` | NO (gitignored) |
| `scripts/dump.sh` | Author-side: live `.db` → `.sql` for git commit | YES |
| `scripts/build.sh` | Consumer-side: `.sql` → `.db` after `git clone` or `git pull` | YES |
| `.gitignore` | Excludes `.db`, runtime artifacts, editor noise | YES |
| `README.md` | Consumer-facing documentation: what the package contains, how to consume | YES |
| `cases/`, `archive/`, etc. | Optional domain-specific human-readable content (cited as evidence) | YES |

The split between `knowledge.sql` (versioned source) and `knowledge.db` (gitignored build artifact) is deliberate: SQL diffs are human-readable for code review, while the binary `.db` would produce noise on every commit. Consumers run `scripts/build.sh` once after cloning to materialize the working database.

## Bootstrapping a new package

```bash
engram-pkg init ~/engram-packages/my-new-pkg --name "My New Package"
cd ~/engram-packages/my-new-pkg
git init && git add . && git commit -m "init: engram-package skeleton"
```

The `init` subcommand creates the protocol files (scripts, README, .gitignore), bootstraps the ENGRAM schema, and dumps it to `knowledge.sql` so a fresh `git clone && bash scripts/build.sh` produces a working `.db` immediately. The directory must be empty (or non-existent — it'll be created); init refuses to overwrite existing files.

## Why a CLI rather than parallel MCP tools

If we expose a parallel set of `mcp__engram_wiki__engram_*` tools, two near-identical surfaces compete for selection — same shape, same parameters, same semantic match for "I want to record an observation." Mid-flow it's easy to fire the wrong one and corrupt either your personal engram or the package. A CLI requires a conscious shell command (`engram-pkg add-observation ...`), which makes the namespace shift visible. It also matches what other agents (e.g., Mneme on Gemini, who has no MCP path to the package) will use — same surface for everyone.

## Install

The script is a single executable Python file. Make it discoverable on PATH:

```bash
ln -s /home/USER/engram-alpha/tools/engram-pkg/engram-pkg ~/.local/bin/engram-pkg
# or add engram-alpha/tools/engram-pkg/ to PATH
```

Requires `python3` and the `engram_client` module (shipped at the engram-alpha repo root). The script auto-locates its source repo from its own path.

## Usage

```bash
# Point at a package once per session:
export ENGRAM_PKG=~/engram-packages/wiki

engram-pkg stats
engram-pkg list --node-type observation_factual
engram-pkg query "dedup vs corroborate"
engram-pkg inspect ob_0007

engram-pkg add-observation \
    --evidence-id ev_0003 \
    --quoted-text "..." \
    --interpretation "..." \
    --claim "..." \
    --quote-type official_statement \
    --source-class introspective

engram-pkg derive \
    --claim "..." \
    --supporting-ids ob_0001,ob_0004,ob_0007 \
    --logical-chain "..." \
    --reasoning-type inductive_generalization

engram-pkg supersede \
    --old-node-id ob_0001 \
    --new-claim "..." \
    --logical-chain "..." \
    --support-ids ev_0001,ob_0002

engram-pkg dump          # write knowledge.sql from live .db
engram-pkg build         # rebuild .db from knowledge.sql
```

### Stale-build detection

Read commands (`list`, `query`, `inspect`, `stats`) read from `knowledge.db`. If `knowledge.sql` has been updated more recently — typically after a fresh `git pull` from a package release, or a hand-edit of `knowledge.sql` — reads will silently return stale data. To catch this:

```bash
# Default: warn-only (command still runs against the stale .db)
engram-pkg stats
# stderr: WARNING: knowledge.sql is newer than knowledge.db — read commands may return stale data...

# --auto-build: rebuild silently before the read
engram-pkg --auto-build stats
# stderr: NOTE: knowledge.sql newer than knowledge.db — auto-building...
```

The reverse direction (`db` newer than `.sql`) is the normal mid-authoring state — `engram-pkg add-observation` writes to `.db` and `engram-pkg dump` catches `.sql` up — so it's not warned on.

**Missing `scripts/build.sh` with `--auto-build`**: hard-exits with an error message rather than silently falling back to a stale read. The reasoning: explicitly passing `--auto-build` is a statement of intent ("I do not want to read stale data"); warn-and-continue would silently contradict that intent. To fall back to warn-only behavior on packages without a build script, omit the flag.

## Authoring workflow

```bash
export ENGRAM_PKG=~/engram-packages/wiki
cd $ENGRAM_PKG
git checkout editing                          # WIP branch

# Add new content via CLI commands above (or edit cases/ files first if adding new evidence).
# Live engram_add_observation / engram_derive write to knowledge.db.

engram-pkg dump                               # knowledge.db -> knowledge.sql
git diff knowledge.sql                        # human-readable diff
git add knowledge.sql cases/ archive/         # commit only versioned artifacts
git commit -m "..."
git push origin editing

# When ready to release stable, merge editing -> main:
git checkout main
git merge editing
git tag v0.x.0
git push origin main --tags
```

Consumers pull from `main` and re-run `scripts/build.sh` (in the package repo) to refresh their local `.db`.

## Generic escape hatch

For any engram tool not exposed as a dedicated subcommand:

```bash
engram-pkg call engram_reflect
engram-pkg call engram_surface --json '{"query": "Bitcoin halving impact"}'

# For payloads with backticks, apostrophes, or multi-line prose, use --json-file
# to sidestep shell quoting entirely:
engram-pkg call engram_add_observation --json-file ./obs.json
```

Tool names match the MCP tool registry (without the `mcp__engram__` prefix).

`--json` and `--json-file` are mutually exclusive. The file path is recommended whenever the JSON contains shell metacharacters or multi-line text — backticks in double-quoted strings get command-substituted by bash, and apostrophes break single-quoted JSON.
