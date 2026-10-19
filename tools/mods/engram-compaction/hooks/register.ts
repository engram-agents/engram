import type { EngineInterface, Register } from 'claude-code'
import { COMPACT_SHAPE, completeHook, compactHook, type Io } from './compaction.ts'

/**
 * engram-compaction (part of #3640): the nap leaves a request under $ENGRAM_HOME/compact/;
 * at the end of the next answered turn this mod consumes it once and compacts, and the
 * resulting compaction carries the context the nap assembled.
 *
 * On by default (Lei, 2026-10-18); switched off, it registers NOTHING: no request is consumed, no compaction is
 * started or answered, every classic behaviour is the engine's. Every registration
 * carries a `.catch` that replays `next`, so a failure here never changes what the
 * model or the person sees. The hooks do bounded `$.fs` work only: no `$.model`, no
 * `$.process`.
 */
export const register: Register = (on, options) => {
  // Anything but a real `true` is off (fail closed); the default in the manifest is true.
  if (options.enabled !== true) return

  // The engine reads each hook as a function literal and follows `$` only into functions
  // declared in THIS file, so each hook builds an `Io` here and hands that (never `$`) on.
  on('turn.complete', ($, e, next) => completeHook(makeIo($))(e, x => next(x))).catch(($, e, next) => next(e))

  // No matcher: the test engine does not stamp `trigger` on a compaction a plugin starts,
  // so the trigger is checked INSIDE the hook (and a fresh in-flight token is required).
  on('session.compact', ($, e, next) => compactHook(makeIo($), COMPACT_SHAPE)(e, x => next(x))).catch(($, e, next) => next(e))
}

/** Every member spells `$.noun.event(...)` at its call site (the validator's rule). */
function makeIo($: EngineInterface): Io {
  return {
    now: () => $.clock.now(),
    read: path => $.fs.read(path),
    write: (path, text) => $.fs.write(path, text),
    exists: path => $.fs.exists(path),
    sessionId: () => $.session.id(),
    compact: args => $.session.compact(args),
    log: text => $.ui.log(text, { to: 'debug' }),
    home: async () => {
      const own = await $.env.get('ENGRAM_HOME')
      if (own !== undefined && own !== '') return own
      const home = await $.env.get('HOME')
      return home === undefined || home === '' ? undefined : `${home.replace(/\/+$/, '')}/.engram`
    },
  }
}
