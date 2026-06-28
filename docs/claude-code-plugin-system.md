# Claude Code Plugin & Marketplace System — How ENGRAM Deploys

> **Status:** verified reference, 2026-06-28. The load-bearing *rules* live in the
> always-loaded project `CLAUDE.md` (§"Plugin build restructures code paths" +
> §"Plugin deploy: the SOURCE tree is live"). **This doc is the on-demand deep
> model** behind those rules — read it when you need to predict how a deploy/edit
> will behave, not every session.
>
> **Important caveat up front:** the directory-vs-git load behavior documented here
> is **NOT documented in the official Claude Code docs** (confirmed by a docs
> review 2026-06-28). It is emergent behavior, established empirically by
> live-instrumentation in our own install. Treat it as a verified *observation of
> our setup*, not a contract — **re-verify after any Claude Code version bump**
> (the method is in §8).

---

## TL;DR — the one rule

**For our install, the marketplace SOURCE tree IS the live code.** Edit a file in
`~/.engram/marketplace/plugins/engram/` and it is what actually runs — there is no
separate "installed copy" to push to. The cache directory is bookkeeping, not the
live path. (The repo workflow deploys via `tools/install-local-marketplace.sh`,
which rebuilds that tree from `src/` — see §9; direct edits to the marketplace tree
also run, useful only for one-off testing.) Consequences (full table in §5):

| You edit… | Goes live… |
|---|---|
| hook / tool / skill code | **immediately** (re-read per invocation; no `/plugin update`) |
| `server.py` (MCP) | after a `/mcp` reconnect or restart (process is from session start) |
| `hooks.json` *registration* (add/remove a hook) | after a **full Claude Code restart** |

And the meta-rule that produced all of this: **to know which code is live,
instrument the live artifact — never infer from a version string, a JSON file, or
a bash-env test** (§8).

---

## 1. Two marketplace source types — this is the whole mechanism

A Claude Code marketplace is registered with a `source` whose *type* decides how
the plugin is loaded:

- **`directory`** (what we use) — Claude Code sets the marketplace's
  `installLocation` to **the source path itself** and loads the plugin **in
  place**. No copy is made; the source tree is the runtime.
- **`git` / `github`** — Claude Code **clones** the marketplace into
  `~/.claude/plugins/marketplaces/<name>/` (a copy) and loads from *that* cache.
  This is the cache-based model the public docs implicitly describe.

So "is the cache live?" has no single answer — **it depends on the source type.**
Official/distributed plugins (git) → cache is live. Ours (directory) → source is
live, cache is bookkeeping. Conflating the two is the confusion that caused the
v0.1.4 incident (§10).

## 2. Our configuration (the lever)

`~/.claude/settings.json`:

```json
"extraKnownMarketplaces": {
  "engram-local": {
    "source": { "source": "directory", "path": "/home/<user>/.engram/marketplace" },
    "autoUpdate": true
  }
},
"enabledPlugins": { "engram@engram-local": true }
```

`"source": "directory"` is the single choice that puts us on the in-place path.
`~/.claude/plugins/known_marketplaces.json` then carries
`installLocation: <the source path>` — i.e. the source dir is registered as its own
install location. That is the concrete mechanism by which source == live.

## 3. `CLAUDE_PLUGIN_ROOT` — the uniform resolution handle

Claude Code sets the env var **`CLAUDE_PLUGIN_ROOT` to the plugin root**, which for
a directory install **resolves to the source tree**
(`~/.engram/marketplace/plugins/engram/`). All plugin components are found by
convention *under that root* — `hooks/`, `skills/`, `agents/`, `tools/`,
`output-styles/`, `templates/`, and the `.mcp.json` launcher. There is one root and
everything hangs off it, which is why
the source-is-live property is **uniform across component types**, not a
per-component quirk.

`plugin.json` itself lists no component paths — name/version/description/author
only — so resolution is purely convention-based under the root.

## 4. Scope — which components load from source

| Component | From source? | How established |
|---|---|---|
| **Hooks** | ✅ yes | **live-verified** — instrumented every copy, fired a real prompt; only the source-tree copy logged, with `CLAUDE_PLUGIN_ROOT` = source |
| **Skills** | ✅ yes | **live-verified** — every Skill load prints "Base directory … `/…/marketplace/plugins/engram/skills/…`" |
| **Agents / tools / output-styles / templates** | yes | **inferred** — same `CLAUDE_PLUGIN_ROOT`=source resolution; not independently fired |
| **MCP server** | path = source | **inferred** — `.mcp.json` launches via `${CLAUDE_PLUGIN_ROOT}/launch-engram-server.sh`; the running *process* is not introspectable from the agent |

The two "inferred" rows follow from the *same* verified mechanism (one root, by
convention) — but per §8 they are inference, not live proof. Close the gap by
live-verifying if a deploy ever behaves unexpectedly.

(Note: Claude Code **slash commands** like `/reload-plugins` are CC built-ins, not
file-based plugin components — our plugin ships no `commands/` directory, so they
are out of scope for the source-loading mechanism above.)

## 5. Predictability — when does a source edit take effect?

| You change… | Detected at | Live after | Reload needed? |
|---|---|---|---|
| Hook code (`hooks/*.py`) | next prompt | next prompt | **No** — hot-reloads (re-read per invocation). **live-proven** |
| Skill / agent code | next invocation | next invocation | Probably no (hot-reload); `/reload-plugins` forces it if a stale read appears |
| Tool code (`tools/*.py`) | next call | next call | No |
| `server.py` / MCP code | — | next `/mcp` reconnect or session | **Yes** — `/mcp` reconnect (lighter) or restart; the process is from session start, so its path is source but it doesn't hot-reload |
| `.mcp.json` launcher | session start | next session | Yes — restart |
| **`hooks.json` registration** (add/remove a hook) | **session start** | **next session** | **Yes — full Claude Code restart.** This is the v0.1.4 lever (§10) |
| Shared-bin CLIs (`/home/agents-shared/bin/{baton,ia,forum}`) | — | on refresh | **Separate copies** — not the plugin tree; refresh independently |

**`/reload-plugins`** (surfaced in the docs review) applies plugin changes without
a full restart — useful as a forcing function if a skill/agent edit ever fails to
hot-reload. It does **not** substitute for the MCP reconnect or the `hooks.json`
restart.

## 6. The cache & `installed_plugins.json` — bookkeeping, not the live path

`~/.claude/plugins/cache/.../<version>/` holds version snapshots, and
`~/.claude/plugins/installed_plugins.json` pins an `installPath`. **For a directory
source these are version bookkeeping, NOT what loads.** (`installPath` for a
directory source points back at the source dir.) For a git source they *would* be
the live path — which is exactly why the distinction matters: the same files mean
different things depending on the source type.

## 7. The build restructures paths (don't assume `src/` layout)

Orthogonal but easy to conflate: `build-plugin.sh` does **not** mirror
`src/engram/` into the plugin — it **flattens** `hooks/claude/* → hooks/*` and
`skills/claude/* → skills/*`. So an installed file sits at a *different nesting
depth* than its source. Any runtime sibling-path resolution must use
`CLAUDE_PLUGIN_ROOT` + a walk-parents search for the actual target file — never a
fixed `Path(__file__).resolve().parents[N]`, which overshoots silently in the
flattened tree. (Full rule + the #1537→#1539 origin incident: project `CLAUDE.md`
§"Plugin build restructures code paths".)

## 8. CRITICAL: verify the live artifact, never a proxy

This entire model was settled only after **three wrong conclusions in a row**, each
drawn from a proxy:

1. a version string,
2. `installed_plugins.json`,
3. a bash-env test (a stale `BATON_PROJECTS_DIR` in the session env made an *old*
   local-FS hook *look* migrated).

The settling test — the only thing that produced ground truth:

> Add a stderr/file **sentinel** line to the top of *each* candidate copy of the
> code, fire a **real prompt** (a self-wake counts), and read **which copy logged**
> — plus read `CLAUDE_PLUGIN_ROOT` from *inside* a live hook. That, and only that,
> tells you what is actually running.

This generalizes beyond plugins: **test against absolute truth, not a proxy that
merely correlates with it.** Encoded as a lesson (the test-the-live-artifact
methodology); the full investigation + cross-install corroboration is recorded in
forum thread #189.

## 9. Quick reference — deploy playbook

- **Shipped a hook/skill/tool fix?** → `tools/install-local-marketplace.sh` rebuilds
  the source tree (= live). Done; it's live next prompt. No `/plugin update`.
- **Shipped a `server.py` change?** → rebuild, then `/mcp` reconnect (or restart).
- **Added/removed a hook (touched `hooks.json`)?** → rebuild, then **full restart**.
  `install-local-marketplace.sh` prints a `⚠️ HOOK REGISTRATION CHANGED` banner.
- **Multi-agent shared CLIs changed?** → refresh `/home/agents-shared/bin/*`
  separately (they are copies, not the plugin tree).
- **After any Claude Code version bump** → re-verify §4/§5 via §8; this behavior is
  undocumented and could change upstream.

## 10. Incident history (why this doc exists)

- **v0.1.4 — 4-day silent-hook outage.** A Claude Code change added a guard on
  hooks invoked directly from the marketplace path. Intended to be a no-op for
  cache-invoked hooks — but **our live hooks ARE invoked from the marketplace
  (source) path**, so the guard killed all of them for four days before diagnosis.
  This is the canonical proof that the directory-source model has real operational
  consequences and that the behavior is upstream-fragile.
- **#1537 → #1539 — deployed-hook silent no-op.** The pure-API baton prompt-hook
  resolved a sibling import with `parents[2]/tools` — correct in `src/`, overshot
  to a nonexistent `plugins/tools` in the flattened deployed tree → the hook
  silently no-op'd. Reviewer-fairy + colleague + CI all passed (they ran on `src/`).
  Caught only by running the *deployed* hook. → §7.
- **2026-06-28 — the proxy flip-flop.** Three successive wrong conclusions about
  hook-live state, each from a proxy, corrected only by live instrumentation. → §8.

## References

- Project `CLAUDE.md` — the two always-loaded rules (build-path flattening; deploy
  source-is-live).
- Forum thread #189 — the full empirical write-up, cross-install corroboration,
  and discussion record (the durable, shared provenance for everything here).
- Official Claude Code docs (what *is* documented): `${CLAUDE_PLUGIN_ROOT}` path
  placeholder; `/plugin marketplace add <dir>`; `/reload-plugins`. The docs do
  **not** cover the directory-vs-git load distinction, the per-component lifecycle,
  or MCP re-spawn semantics — those are the gaps this doc fills empirically.
