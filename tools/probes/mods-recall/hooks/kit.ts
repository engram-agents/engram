// Shared scaffolding for the *.test.ts files (not loaded by the mod itself).
//
// A test body gets the engine's `$` and an `on` whose hooks sit BENEATH every
// plugin and stand for the engine: whatever the plugin calls on `$` (fs.write,
// mcp.call, ...) lands on the bottom hook registered here, and an event with no
// bottom hook throws naming itself. So `rig` registers one for every event the
// probe touches, recording what it saw.
import { mock } from 'claude-code/testing'
import type { On, TurnStepChunk, TurnStepResult } from 'claude-code'

/** Node-id-shaped test ids are built, never spelled: the repo's no-new-shipped-node-ids gate scans tools/. */
export const nid = (prefix: string, n: number) => `${prefix}_${n}`
export const OB = nid('ob', 1001)
export const LX = nid('lx', 2002)

export const SESSION = 'sess-1'
export const PROBE_HOME = '/probe-home'
export const PROBE_ROOT = `${PROBE_HOME}/.engram/probe/mods-recall/${SESSION}`
export const TURN = 'turn1234-aaaa-bbbb-cccc-dddddddddddd'
export const ENGRAM_TOOL = 'mcp__plugin_engram_engram__engram_query'
export const ENGRAM_SERVER = 'plugin_engram_engram'

export type RigOptions = {
  /** ENGRAM_HOME the mock env answers; default: unset */
  engramHome?: string
  /** tool names `$.tool.list()` answers; default: one engram_query tool */
  tools?: string[]
  /** `$.mcp.call` behaviour per query text; default: two candidates */
  recall?: (query: string) => Promise<unknown> | unknown
  /** `$.fs.write` throws when set */
  failWrites?: boolean
  /** the test's `session.append` hook refuses the row (`{ deny }`) when set; default: it relays the row and the engine keeps it. Either way the row is recorded in `seen.appends` */
  appendRejects?: boolean
  /** extra rows `$.session.messages()` answers */
  messages?: { role: 'user' | 'assistant'; text: string; toolUses: never[] }[]
  /** `$.process.run` stdout; default: one novel term */
  novel?: { terms: string[]; novel: string[]; db?: string }
  /** the answer `$.model.complete` gives; default answered */
  complete?: () => unknown
  /** `$.session.model` throws when set (provokes a handler failure) */
  modelThrows?: boolean
  /** HOME the env answers (undefined = unset) */
  home?: string | undefined
  /** files `$.fs.read` answers, by absolute path (a path in neither this nor the writes throws, like a missing file) */
  files?: Record<string, string>
}

export function candidateRow(id: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    type: 'observation',
    claim: `claim of ${id}`,
    confidence: 0.85,
    composite_score: 0.5,
    similarity: 0.4,
    match_type: 'semantic',
    utility_score: 0.1,
    importance_score: 0.2,
    recall_turn: 7,
    ...extra,
  }
}

export function mcpText(body: unknown) {
  return { content: [{ type: 'text' as const, text: JSON.stringify(body) }], isError: false }
}

export function rig(on: On, options: RigOptions = {}) {
  const clock = mock.clock(on, { now: 1_000 })
  const home = 'home' in options ? options.home : PROBE_HOME
  mock.env(on, { ...(home === undefined ? {} : { HOME: home }), ...(options.engramHome === undefined ? {} : { ENGRAM_HOME: options.engramHome }) })

  const seen = {
    writes: new Map<string, string>(),
    queries: [] as string[],
    recallCalls: [] as { server: string; tool: string; args: Record<string, unknown> }[],
    logs: [] as string[],
    completeCalls: 0,
    completePrompts: [] as string[],
    processCalls: [] as { argv: readonly string[]; stdin?: string }[],
    registered: [] as unknown[],
    /** every row a plugin's `$.session.append` delivered to the bottom hook, accepted or not */
    appends: [] as AppendedRow[],
  }

  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('turn.complete', async (_$, e) => ({ text: e.answer }))
  on('session.append', async (_$, e, next) => {
    seen.appends.push({ type: e.message.type, content: e.message.content as AppendedRow['content'] })
    if (options.appendRejects) return { deny: 'append refused by the test' }
    return next(e)
  })
  on('session.id', async () => ({ value: SESSION }))
  on('session.model', async () => {
    // session.start also reads the model; only throw once the session is up
    if (options.modelThrows && seen.writes.has(`${PROBE_ROOT}/session.json`)) throw new Error('model unavailable')
    return { value: 'test-model' }
  })
  on('session.version', async () => ({ value: { version: '2.1.289' } }))
  on('session.messages', async () => ({ value: (options.messages ?? []) as never }))
  on('tool.list', async () => ({
    value: (options.tools ?? [ENGRAM_TOOL]).map(name => ({ name, description: '', mcp: true })),
  }))
  on('tool.register', async (_$, e) => {
    seen.registered.push(e)
    return { value: { tool: `mcp__engram-probe__${e.name}` } }
  })
  on('ui.log', async (_$, e) => {
    seen.logs.push(e.text)
    return { value: undefined }
  })
  on('fs.write', async (_$, e) => {
    if (options.failWrites) throw new Error('disk full')
    seen.writes.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.exists', async (_$, e) => ({ value: seen.writes.has(e.path) }))
  on('fs.list', async (_$, e) => {
    const dir = e.path.replace(/\/+$/, '') + '/'
    const names = new Set<string>()
    for (const k of [...Object.keys(options.files ?? {}), ...seen.writes.keys()]) {
      if (k.startsWith(dir) && !k.slice(dir.length).includes('/')) names.add(k.slice(dir.length))
    }
    if (names.size === 0) throw new Error(`ENOENT: ${e.path}`)
    return { value: [...names].map(name => ({ name, kind: 'file' as const })) as never }
  })
  on('fs.read', async (_$, e) => {
    const text = options.files?.[e.path] ?? seen.writes.get(e.path)
    if (text === undefined) throw new Error(`ENOENT: ${e.path}`)
    return { value: text as never }
  })
  on('mcp.call', async (_$, e) => {
    seen.recallCalls.push({ server: e.server, tool: e.tool, args: e.args })
    const query = String(JSON.parse(String(e.args.payload_json)).query)
    seen.queries.push(query)
    const custom = options.recall ? await options.recall(query) : undefined
    const body = custom ?? { query, results: [candidateRow(OB), candidateRow(LX, { type: 'lesson' })] }
    return { value: mcpText(body) as never }
  })
  on('model.complete', async (_$, e) => {
    seen.completeCalls += 1
    seen.completePrompts.push(`${e.system ?? ''}\n${e.prompt}`)
    const custom = options.complete?.()
    return {
      value: (custom ?? {
        isAnswered: true,
        text: 'recall phrase one | phrase two | phrase three',
        usage: { input_tokens: 10, output_tokens: 5, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      }) as never,
    }
  })
  on('process.run', async (_$, e) => {
    seen.processCalls.push({ argv: e.argv, stdin: e.init?.stdin })
    const out = options.novel ?? { terms: ['zymurgy'], novel: ['zymurgy'] }
    return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } as never }
  })
  return { clock, seen }
}

export function stepFile(seen: { writes: Map<string, string> }, turnId: string, index: number) {
  const key = `${PROBE_ROOT}/steps/${turnId}-${String(index).padStart(3, '0')}.json`
  const text = seen.writes.get(key)
  return text === undefined ? undefined : (JSON.parse(text) as Record<string, any>)
}

export function stepFiles(seen: { writes: Map<string, string> }) {
  return [...seen.writes.keys()].filter(k => k.startsWith(`${PROBE_ROOT}/steps/`))
}

export const LONG_THINKING =
  'I need to work out how the recall probe should weigh the tripwire novelty gate against the thinking tail. ' +
  'The key question is whether the tail of the reasoning predicts what I will want to remember next. ' +
  'Let me look at the converter before deciding. '.repeat(3)

export function chunksFor(thinkingA = LONG_THINKING, thinkingB = ' More thought.'): TurnStepChunk[] {
  return [
    { kind: 'thinking', index: 0, text: thinkingA },
    { kind: 'thinking', index: 0, text: thinkingB },
    { kind: 'text', index: 1, text: 'Looking now.' },
    { kind: 'tool', index: 2, id: 'toolu_1', name: 'Read' },
    { kind: 'input', index: 2, json: '{"file_path":' },
    { kind: 'input', index: 2, json: '"/a.md"}' },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ]
}

/** A step with short thinking, a short answer, and a tool call whose arguments text is blank: nothing to measure. */
export function blankStepChunks(thinking = 'short'): TurnStepChunk[] {
  return [
    { kind: 'thinking', index: 0, text: thinking },
    { kind: 'text', index: 1, text: 'Looking now.' },
    { kind: 'tool', index: 2, id: 'toolu_1', name: 'Read' },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ]
}

export const STEP_RESULT: TurnStepResult = {
  turnId: TURN,
  index: 0,
  answer: 'Looking now.',
  toolUses: [{ name: 'Read', input: { file_path: '/a.md' } }],
  stopReason: 'tool_use',
  usage: null,
}

/** The engine under the plugin: streams `chunks`, returns `result`. */
export function engineStep(on: On, chunks: TurnStepChunk[], result: TurnStepResult = STEP_RESULT) {
  on('turn.step', async function* () {
    for (const c of chunks) yield c
    return result
  })
}

export async function drain<C, R>(stream: AsyncGenerator<C, R>): Promise<{ chunks: C[]; result: R }> {
  const chunks: C[] = []
  for (;;) {
    const r = await stream.next()
    if (r.done) return { chunks, result: r.value }
    chunks.push(r.value)
  }
}

export const START = { cwd: '/w', surface: null, isInteractive: false } as const
export const COMPLETE = { answer: 'done', durationMs: 5, isAborted: false, turnId: TURN, reason: 'answer' } as const

/** Let timers (clock.after(0)) fire and the work they started settle. */
export async function flush(clock: { settle: () => Promise<void> }) {
  for (let i = 0; i < 6; i++) await clock.settle()
}

/**
 * The rows the probe appended to the conversation, whatever the kit does with
 * them. `rig` registers a bottom `session.append` hook that records every row a
 * plugin's `$.session.append` delivers (`seen.appends`), then accepts it, or
 * answers `{ deny }` (a refused append) when the rig has `appendRejects`; a hook that throws is skipped by the kit and the row is kept, so no test can make the append throw. From Claude Code 2.1.311 the kit
 * delivers the call to that hook, so the rows are read from there. A kit that
 * never delivered it (the 2.1.289 kit, as the earlier cell comments recorded it; not re-run) made the append reject; the probe logs each
 * failed append with the row text (`rating request append ...; row=<json>`),
 * so with no recorded row the rows are read back from those log lines. The
 * hook's record wins when present: a rejected row is both recorded and logged,
 * and must count once. The function only reads, so two calls agree.
 */
export type AppendedRow = { type: string; content: { type: string; text: string }[] }

export function appended(seen: { logs: string[]; appends: AppendedRow[] }): AppendedRow[] {
  if (seen.appends.length > 0) return [...seen.appends]
  return seen.logs
    .filter(l => l.includes('rating request append'))
    .map(l => {
      const text = JSON.parse(l.slice(l.indexOf('; row=') + 6)) as string
      return { type: 'user', content: [{ type: 'text', text }] }
    })
}

/** The route ledger line shape of #3458 section A (only the fields the probe reads are required). */
export function ledgerCandidate(id: string, similarity: number | null, extra: Record<string, unknown> = {}) {
  return {
    id,
    type: 'observation',
    rank: 1,
    similarity,
    importance_score: 0.2,
    utility: 0.1,
    composite_score: 0.5,
    match_type: 'semantic',
    recall_turn: 7,
    created_at: '2026-10-01T00:00:00Z',
    in_context: false,
    ...extra,
  }
}

export function routeLine(
  route: 'prompt_surface' | 'tool_recall',
  fireKey: string,
  shown: string[],
  extra: Record<string, unknown> = {},
) {
  return {
    ts: 1_000,
    route,
    session_id: SESSION,
    fire_key: fireKey,
    query: `query of ${route}`,
    candidates: shown.map((id, i) => ledgerCandidate(id, 0.6 - i * 0.1, { rank: i + 1 })),
    missing_signals: [],
    shown,
    cut: {},
    source: 'test',
    ...extra,
  }
}

/** `files` entries for a ledger: one JSON line per route line, under `<session>/routes/<route>.jsonl`. */
export function ledgerFiles(lines: ReturnType<typeof routeLine>[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const l of lines) {
    const path = `${PROBE_ROOT}/routes/${l.route}.jsonl`
    out[path] = (out[path] ?? '') + JSON.stringify(l) + '\n'
  }
  return out
}

/** sha256 of {"file_path":"/a.md"} (first 16 hex), measured with python's hashlib, for the Read call chunksFor() streams. */
export const READ_FIRE_KEY = 'Read:b2b3bcf85e6c8ba9'
