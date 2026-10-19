// Shared scaffolding for the *.test.ts files (not loaded by the mod itself).
//
// A test body gets the engine's `$` and an `on` whose hooks sit BENEATH every
// plugin and stand for the engine. `rig` registers one for every event the mod
// touches, recording what it saw: the order of writes and compact calls is what
// the consume-first cells assert.
import { mock } from 'claude-code/testing'
import type { On } from 'claude-code'

export const SESSION = 'sess-1'
export const EH = '/eh'
export const DIR = `${EH}/compact`
export const REQUEST = `${DIR}/request.json`
export const INFLIGHT = `${DIR}/inflight`
export const CONTEXT_MD = `${DIR}/context.md`
export const CONTEXT_SHA = `${DIR}/context.sha256`
export const T0 = Date.parse('2026-10-11T12:00:00Z')
export const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')

/** What the engine's own compaction answers (a summary), so a pass-through is recognisable. */
export const ENGINE_MSG = { role: 'user' as const, text: 'engine-summary', toolUses: [] as never[] }
/** The transcript the engine hands a session.compact hook (never empty). */
export const TRANSCRIPT = [{ role: 'user' as const, text: 'hi', toolUses: [] as never[] }]
export const CONTEXT =
  '# Post-compaction context\n\n## Focus list\n- [focus-1] (reason) FOCUS-LINE-VERBATIM\n\n## Warm briefing arc\nARC-LINE-VERBATIM\n'

export async function sha256(text: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('')
}

export type RigOptions = {
  /** what the engine's `session.compact` does: answer a summary (default), reject, or veto */
  compact?: 'answer' | 'reject' | 'skip'
  /** `fs.write` throws for any path that ends with one of these */
  failWritesTo?: string[]
  /** `session.id` throws */
  idThrows?: boolean
  /** `fs.read` throws for every path */
  readThrows?: boolean
  /** env the mod sees; default ENGRAM_HOME=/eh */
  env?: Record<string, string>
}

export type Seen = {
  writes: Map<string, string>
  /** the inputs the engine's `session.compact` received, oldest first */
  compactCalls: { trigger?: string; instructions?: string }[]
  logs: string[]
  /** ordered: `write:<basename>[:consumed]` and `compact` */
  events: string[]
}

export function rig(on: On, options: RigOptions = {}) {
  const clock = mock.clock(on, { now: T0 })
  mock.env(on, options.env ?? { ENGRAM_HOME: EH })
  const seen: Seen = { writes: new Map(), compactCalls: [], logs: [], events: [] }

  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('turn.complete', async (_$, e) => ({ text: e.answer }))
  on('session.id', async () => {
    if (options.idThrows) throw new Error('no id')
    return { value: SESSION }
  })
  on('session.messages', async () => ({ value: [{ role: 'user', text: 'hi', toolUses: [] }] as never }))
  on('ui.log', async (_$, e) => {
    seen.logs.push(e.text)
    return { value: undefined }
  })
  on('fs.write', async (_$, e) => {
    if ((options.failWritesTo ?? []).some(suffix => e.path.endsWith(suffix))) throw new Error('disk full')
    seen.writes.set(e.path, e.text)
    const base = e.path.slice(e.path.lastIndexOf('/') + 1)
    seen.events.push(`write:${base}${e.text.includes('"consumed":true') ? ':consumed' : ''}`)
    return { value: undefined }
  })
  on('fs.exists', async (_$, e) => ({ value: seen.writes.has(e.path) }))
  on('fs.read', async (_$, e) => {
    if (options.readThrows) throw new Error('unreadable')
    const text = seen.writes.get(e.path)
    if (text === undefined) throw new Error(`ENOENT: ${e.path}`)
    return { value: text as never }
  })
  on('session.compact', async (_$, e) => {
    seen.events.push('compact')
    seen.compactCalls.push({ trigger: e.trigger, instructions: e.instructions })
    if (options.compact === 'reject') throw new Error('refused while a turn runs')
    if (options.compact === 'skip') return { skip: 'vetoed by another hook' } as never
    return { messages: [ENGINE_MSG] } as never
  })
  return { clock, seen }
}

/** A request the Python half would have left, plus its context files. Every field can be overridden. */
export async function seedRequest(seen: Seen, over: Record<string, unknown> = {}, context = CONTEXT) {
  const digest = await sha256(context)
  seen.writes.set(
    REQUEST,
    JSON.stringify({ requested_at: iso(T0 - 5_000), session_id: SESSION, loop: false, context_sha256: digest, ...over }),
  )
  seen.writes.set(CONTEXT_MD, context)
  seen.writes.set(CONTEXT_SHA, digest + '\n')
  return digest
}

/** An in-flight token as the turn.complete hook would have left it. */
export async function seedToken(seen: Seen, over: Record<string, unknown> = {}, context = CONTEXT) {
  const digest = await sha256(context)
  seen.writes.set(INFLIGHT, JSON.stringify({ request_id: digest, at: T0 - 1_000, ...over }))
  return digest
}

/** The test options that pin the mod on explicitly (its shipped default is also on). */
export const ON = { options: { enabled: true } }

export const START = { cwd: '/w', surface: null, isInteractive: false } as const
export const COMPLETE = { answer: 'done', durationMs: 5, isAborted: false, turnId: 'turn-1', reason: 'answer' } as const

export function readJson(seen: Seen, path: string): Record<string, any> {
  const text = seen.writes.get(path)
  if (text === undefined) throw new Error(`no file at ${path}`)
  return JSON.parse(text)
}
