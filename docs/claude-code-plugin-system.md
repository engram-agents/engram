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

**For our install, the marketplace SOURCE tree is the live code — for hooks,
skills, and tools.** Edit a file in `~/.engram/marketplace/plugins/engram/` and it
is what actually runs for those; the cache directory is bookkeeping for them. **The
one exception is AGENTS (sub-agents / fairies): they load from the cache snapshot,
NOT source** (live-verified #1639) — an agent-spec edit is inert until a cache
refresh. (The repo workflow deploys via `tools/install-local-marketplace.sh`, which
rebuilds that tree from `src/` — see §9; direct edits to the marketplace tree also
run, useful only for one-off testing.) Consequences (full table in §5):

| You edit… | Goes live… |
|---|---|
| hook / tool / skill code | **immediately** (re-read per invocation; no `/plugin update`) |
| **agent spec (`agents/*.md`)** | after **`/plugin marketplace update` + `/reload-plugins`** (agents load from the cache snapshot, not source — #1639) |
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
  `installLocation` to **the source path itself** and loads *most* components in
  place: no copy is made for hooks/skills/tools; the source tree is their runtime.
  **Agents are the exception** — the plugin's separate `installPath`
  (`installed_plugins.json`) still points at a **cache snapshot**, and agent specs
  load from *there*, not source (§4/§6, live-verified #1639).
- **`git` / `github`** — Claude Code **clones** the marketplace into
  `~/.claude/plugins/marketplaces/<name>/` (a copy) and loads *everything* from
  *that* cache. This is the cache-based model the public docs implicitly describe.

So "is the cache live?" has no single answer — **it depends on the source type
AND the component.** Official/distributed plugins (git) → cache is live for all.
Ours (directory) → source is live for hooks/skills/tools, but the **cache is still
live for agents**. Conflating "directory = source-live for everything" is the
confusion that caused the v0.1.4 incident (§10) *and* the #1639 agent-MCP outage.

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
`output-styles/`, `templates/`, and the `.mcp.json` launcher. `CLAUDE_PLUGIN_ROOT`
governs loading for hooks/skills/tools (source-live). **But `CLAUDE_PLUGIN_ROOT` is
NOT the load path for agents** (§4): sub-agent definitions are registered from the
plugin's `installPath` cache snapshot, so the source-is-live property is **NOT
uniform across component types** — agents are a genuine per-component exception
(live-verified #1639). Root-by-convention determines where a component *lives*, not
whether Claude Code loads it from source vs. cache.

`plugin.json` itself lists no component paths — name/version/description/author
only — so resolution is purely convention-based under the root.

## 4. Scope — which components load from source

| Component | From source? | How established |
|---|---|---|
| **Hooks** | ✅ yes | **live-verified** — instrumented every copy, fired a real prompt; only the source-tree copy logged, with `CLAUDE_PLUGIN_ROOT` = source |
| **Skills** | ✅ yes | **live-verified** — every Skill load prints "Base directory … `/…/marketplace/plugins/engram/skills/…`" |
| **Agents (sub-agents / fairies)** | ❌ **NO — cache-loaded** | **live-verified (#1639, 2026-07-02)** — agent definitions are registered from the plugin **`installPath`** (a `~/.claude/plugins/cache/.../<version>/` snapshot), **NOT** from source. A source-only edit never activates: a fresh dream-fairy carried the stale-cache spec (pre-#1551, single-prefix) until `/plugin marketplace update` + `/reload-plugins`, after which a probe confirmed the new spec live. **This is the one component where `CLAUDE_PLUGIN_ROOT`=source does NOT govern loading.** |
| **Tools / output-styles / templates** | yes | **inferred** — same `CLAUDE_PLUGIN_ROOT`=source resolution; not independently fired |
| **MCP server** | path = source | **inferred** — `.mcp.json` launches via `${CLAUDE_PLUGIN_ROOT}/launch-engram-server.sh`; the running *process* is not introspectable from the agent |

The remaining "inferred" rows (tools/output-styles/templates, MCP-server-path)
follow from the *same* verified mechanism (one root, by convention) — but per §8
they are inference, not live proof. Close the gap by live-verifying if a deploy
ever behaves unexpectedly. **The Agents row was exactly such a case:** it was
"inferred: yes-from-source" until #1639 live-verified it is actually
cache-loaded — the opposite of the inference. Treat the remaining inferred rows
with that cautionary precedent.

(Note: Claude Code **slash commands** like `/reload-plugins` are CC built-ins, not
file-based plugin components — our plugin ships no `commands/` directory, so they
are out of scope for the source-loading mechanism above.)

## 5. Predictability — when does a source edit take effect?

| You change… | Detected at | Live after | Reload needed? |
|---|---|---|---|
| Hook code (`hooks/*.py`) | next prompt | next prompt | **No** — hot-reloads (re-read per invocation). **live-proven** |
| Skill code | next invocation | next invocation | No — hot-reloads from source (re-read per invocation). **live-proven** |
| **Agent code (`agents/*.md`)** | — | **after cache refresh + reload** | **Yes — `/plugin marketplace update` + `/reload-plugins`.** Agents are cache-loaded (§4), so a source edit is inert until the cache snapshot is refreshed; a full restart also works but is heavier. **live-proven (#1639)** |
| Tool code (`tools/*.py`) | next call | next call | No |
| `server.py` / MCP code | — | next `/mcp` reconnect or session | **Yes** — `/mcp` reconnect (lighter) or restart; the process is from session start, so its path is source but it doesn't hot-reload |
| `.mcp.json` launcher | session start | next session | Yes — restart |
| **`hooks.json` registration** (add/remove a hook) | **session start** | **next session** | **Yes — full Claude Code restart.** This is the v0.1.4 lever (§10) |
| Shared-bin CLIs (`/home/agents-shared/bin/{baton,ia,forum}`) | — | on refresh | **Separate copies** — not the plugin tree; refresh independently |

**`/reload-plugins`** applies plugin changes without a full restart. For **agents**
it is **required** (paired with `/plugin marketplace update`, which refreshes the
cache snapshot agents load from — §4/§6), not merely a forcing function. For
skills/tools (source-live) it is only a forcing function if a hot-reload ever fails.
It does **not** substitute for the MCP reconnect; for a `hooks.json` *registration*
change it reloaded hooks in-session in the #1639 case, but a full restart remains
the conservative fallback (that specific case is not independently re-verified).

## 6. The cache & `installed_plugins.json` — bookkeeping for MOST, live for AGENTS

Two different fields, two different targets — do not conflate them (this conflation
was the root of the #1639 mis-inference):

- **Marketplace `installLocation`** (`known_marketplaces.json`) for a `directory`
  source = **the source dir itself** (verified: `/home/<agent>/.engram/marketplace`).
  This is what `CLAUDE_PLUGIN_ROOT` resolves to, and hooks/skills/tools load from it —
  **source-live.**
- **Plugin `installPath`** (`installed_plugins.json`) = a **cache snapshot dir**
  (verified: `~/.claude/plugins/cache/engram-local/engram/<version>/`), **NOT** the
  source dir. It advances only on `/plugin marketplace update`.

For hooks/skills/tools the cache snapshot is **version bookkeeping, NOT what loads**
(they load from `installLocation`=source). **But AGENTS load from the `installPath`
cache snapshot** (§4, live-verified #1639) — so for agents the cache is the live path,
exactly as it is for a *git*-source plugin. That is why a stale cache silently served
a pre-fix agent spec until the snapshot was refreshed. The earlier claim here —
"`installPath` for a directory source points back at the source dir" — was **wrong**:
it points at a cache snapshot, and for agents that snapshot is authoritative.

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
- **Shipped an agent-spec fix (`agents/*.md`)?** → rebuild, then **`/plugin
  marketplace update engram-local` + `/reload-plugins`** — agents load from the cache
  snapshot, NOT source (§4/§6), so a rebuild alone is inert. Verify by dispatching the
  agent and confirming its tools; a stale-cache agent fails *silently* (#1639).
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
