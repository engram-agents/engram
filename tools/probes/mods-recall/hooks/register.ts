import type { Register } from 'claude-code'
import { parseConfig } from './config.ts'
import { discoverServer } from './recall.ts'
import {
  RATE_DESCRIPTION,
  RATE_SCHEMA,
  rateHook,
  turnCompleteHook,
} from './rating.ts'
import {
  SAFE_ID,
  logOnce,
  newProbe,
  resolveProbeDir,
  writeUnderRoot,
  type Eng,
  type Io,
  type Probe,
} from './record.ts'
import { stepHook } from './step.ts'

/**
 * engram-probe (part of #3362): observes every model step, runs retrieval arms
 * over its thinking, records ranking metadata + latency to files, and at the end
 * of a turn appends ONE blinded rating request. It changes nothing the model
 * reads DURING a turn, and every non-streaming registration carries a `.catch`
 * that replays `next` (the streaming hook relies on the engine's own mid-stream
 * continuation), so a probe failure never changes what the model or the person sees.
 */
export const register: Register = (on, options) => {
  const configLines: string[] = []
  const cfg = parseConfig(options, line => configLines.push(line))
  const probe = newProbe(cfg)

  // One session.start registration for both cases (the engine refuses a second
  // un-matched one): disabled -> a no-op that only flushes the config log.
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    flushConfigLog(makeIo($), configLines)
    if (!cfg.enabled) return started
    try {
      await initSession(makeIo($), probe)
    } catch (err) {
      probe.disabled = 'init-failed'
      logOnce(makeIo($), probe, 'init', `session init failed; probe inactive: ${String(err).slice(0, 160)}`)
    }
    return started
  }).catch(($, e, next) => next(e))

  if (!cfg.enabled) return

  // The engine reads each hook as a function literal and follows `$` only into
  // functions declared in THIS file, so each hook builds an `Io` here and hands
  // that (never `$`) to the handlers built over `probe`.
  const step = stepHook(probe)
  const complete = turnCompleteHook(probe)
  const rate = rateHook(probe)

  // Deliberately NO `.catch` on the streaming hook. The declaration says a chunk
  // yielded stays: "a hook that fails mid-stream is left, the rest from beneath it"
  // (claude-code.d.ts, StreamHook doc, ~line 11586), and a hook past its budget is
  // "absent (its .catch asked, else next(e) run on its behalf)" (HookBudget.ms,
  // ~line 4927). So a failure before the first yield is run again by the engine
  // itself, and a failure mid-stream is continued from beneath; a `.catch` that
  // replays next(e) after partial chunks risks a SECOND model request. Observation
  // inside the hook is wrapped in its own try/catch and cannot throw.
  on('turn.step', async function* ($, e, next) {
    return yield* step(makeIo($), e, x => next(x))
  })

  on('turn.complete', ($, e, next) => complete(makeIo($), e, x => next(x))).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'mcp__engram-probe__rate' }, ($, e, next) => rate(makeIo($), e, x => next(x as typeof e))).catch(
    ($, e, next) => next(e),
  )
}

/** Every member spells `$.noun.event(...)` at its call site (the validator's rule). */
function makeIo($: Eng): Io {
  return {
    now: () => $.clock.now(),
    after: (ms, fn) => $.clock.after(ms, fn),
    write: (path, text) => $.fs.write(path, text),
    exists: path => $.fs.exists(path),
    log: text => $.ui.log(text, { to: 'debug' }),
    notice: text => $.ui.log(text),
    mcpCall: (server, tool, args) => $.mcp.call(server, tool, args),
    complete: req => $.model.complete(req),
    run: (argv, init) => $.process.run(argv, init),
    messages: () => $.session.messages(),
    toolNames: async () => (await $.tool.list()).map(t => t.name),
    registerTool: spec => $.tool.register(spec),
    append: args => $.session.append(args),
    sessionId: () => $.session.id(),
    model: () => $.session.model(),
    engineVersion: async () => (await $.session.version()).version,
    homeDir: () => $.env.get('HOME'),
    pluginRoot: $.plugin.root,
  }
}

function flushConfigLog(io: Io, lines: string[]): void {
  for (const line of lines.splice(0)) {
    try {
      io.notice(line)
    } catch {
      // logging never fails a hook
    }
  }
}

async function initSession(io: Io, probe: Probe): Promise<void> {
  const rawId = await io.sessionId()
  const sessionId = SAFE_ID.test(rawId) ? rawId : rawId.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 100) || 'session'
  const dir = await resolveProbeDir(io, probe.cfg.probeDir)
  if (dir === null) {
    probe.disabled = 'no-home'
    logOnce(io, probe, 'no-home', 'HOME is unset and probeDir starts with ~; probe inactive (never writing to a guessed path)')
    return
  }
  probe.sessionId = sessionId
  probe.root = `${dir.replace(/\/+$/, '')}/${sessionId}`

  await io.registerTool({
    name: 'rate',
    description: RATE_DESCRIPTION,
    inputSchema: RATE_SCHEMA,
  })

  probe.server = discoverServer(await io.toolNames()) ?? null

  const startedAtMs = await io.now()
  await writeUnderRoot(
    io,
    probe,
    'session.json',
    JSON.stringify({
      sessionId,
      startedAt: new Date(startedAtMs).toISOString(),
      model: await io.model(),
      engineVersion: await io.engineVersion(),
      options: probe.cfg,
      arms: probe.cfg.arms,
      engram_server: probe.server,
    }),
  )
  if (probe.server === null) {
  await writeUnderRoot(
      io,
      probe,
      `session-${sessionId}.json`,
      JSON.stringify({ disabled: 'no engram_query tool' }),
    )
  }
}
