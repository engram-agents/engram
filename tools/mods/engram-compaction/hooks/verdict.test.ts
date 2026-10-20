// Cells for the verdict in the tombstone (part of #3640): the file says WHY a request ended.
// Contract-first: written before the verdict was added to compaction.ts.
//
// Most cells drive the hooks directly with a fake Io (the engine is not needed to read what a
// tombstone says); the M-V1 cells go through the harness's `$` once to cover the real wiring.
import { expect, test } from 'claude-code/testing'
import { compactHook, completeHook } from './compaction.ts'
import type { Io } from './compaction.ts'
import { COMPLETE, ENGINE_MSG, INFLIGHT, ON, REQUEST, START, T0, TRANSCRIPT, iso, readJson, rig, seedRequest, sha256, CONTEXT } from './kit.ts'

type Opts = {
  compact?: Io['compact']
  /** write throws when this returns true (path, 1-based count of writes to that path) */
  failWrite?: (path: string, n: number) => boolean
  /** every io.log line lands here, so a cell can see what the hook said (the default sink drops it) */
  logs?: string[]
}

function fakeIo(files: Map<string, string>, opts: Opts = {}): Io {
  const counts = new Map<string, number>()
  return {
    now: async () => T0,
    read: async (p: string) => {
      const t = files.get(p)
      if (t === undefined) throw new Error('ENOENT')
      return t
    },
    write: async (p: string, t: string) => {
      const n = (counts.get(p) ?? 0) + 1
      counts.set(p, n)
      if (opts.failWrite?.(p, n)) throw new Error('disk full')
      files.set(p, t)
    },
    exists: async (p: string) => files.has(p),
    sessionId: async () => 'sess-1',
    compact: opts.compact ?? (async () => ({ messages: [ENGINE_MSG] }) as never),
    log: (line: string) => { opts.logs?.push(line) },
    home: async () => '/eh',
  }
}

const next = (async () => ({ text: 'done' })) as never

async function run(files: Map<string, string>, opts: Opts = {}) {
  const hook = completeHook(fakeIo(files, opts))
  return hook(COMPLETE as never, next)
}

async function seeded(over: Record<string, unknown> = {}) {
  const files = new Map<string, string>()
  const digest = await seedRequest({ writes: files } as never, over)
  return { files, digest }
}

const veto = (skip: unknown) => ({ compact: (async () => ({ skip })) as never })

function inflight(files: Map<string, string>) {
  return JSON.parse(files.get(INFLIGHT)!)
}

// ---- 1-3: the three outcomes of the compact call

test('M-V1 compacted: the in-flight tombstone says `compacted`', async () => {
  const { files, digest } = await seeded()
  await run(files)
  expect(inflight(files)).toEqual({ consumed: true, request_id: digest, verdict: 'compacted' })
})

test('M-V2 vetoed: the verdict carries the skip text', async () => {
  const { files } = await seeded()
  await run(files, veto('x'))
  expect(inflight(files).verdict).toBe('vetoed: x')
  expect(inflight(files).consumed).toBe(true)
})

test('M-V2 a skip that is not a string is rendered, never thrown on', async () => {
  for (const [skip, shown] of [
    [{ why: 'busy' }, 'vetoed: {"why":"busy"}'],
    [7, 'vetoed: 7'],
    [true, 'vetoed: true'],
  ] as const) {
    const { files } = await seeded()
    await run(files, veto(skip))
    expect(inflight(files).verdict).toBe(shown)
  }
  // an object that cannot be stringified (a cycle) still yields a one-line verdict
  const cyc: any = {}
  cyc.self = cyc
  const { files } = await seeded()
  await run(files, veto(cyc))
  expect(inflight(files).verdict.startsWith('vetoed: ')).toBe(true)
  expect(inflight(files).consumed).toBe(true)
})

test('M-V3 refused: the verdict carries the error text', async () => {
  const { files } = await seeded()
  await run(files, { compact: (async () => { throw new Error('boom') }) as never })
  expect(inflight(files).verdict).toBe('refused: Error: boom')
  expect(inflight(files).consumed).toBe(true)
})

test('M-V3 the error text is cut at 160 characters before the verdict is built', async () => {
  const { files } = await seeded()
  await run(files, { compact: (async () => { throw new Error('e'.repeat(400)) }) as never })
  const v: string = inflight(files).verdict
  expect(v).toBe('refused: ' + String(new Error('e'.repeat(400))).slice(0, 160))
  expect(v.length).toBe('refused: '.length + 160)
})

test('M-V3 an error whose toString throws is still a refused verdict', async () => {
  const { files } = await seeded()
  const bad = { toString() { throw new Error('nope') } }
  await run(files, { compact: (async () => { throw bad }) as never })
  expect(inflight(files).verdict.startsWith('refused: ')).toBe(true)
  expect(inflight(files).consumed).toBe(true)
})

// ---- 4: exits where nothing was attempted leave a reason in the REQUEST tombstone

test('M-V4 a stale request: the request tombstone says `dropped: stale or malformed request`', async () => {
  const { files, digest } = await seeded({ requested_at: iso(T0 - 31 * 60_000) })
  await run(files)
  expect(JSON.parse(files.get(REQUEST)!)).toEqual({ consumed: true, request_id: digest, verdict: 'dropped: stale or malformed request' })
  expect(files.has(INFLIGHT)).toBe(false)
})

test('M-V4 a malformed request (no usable digest): the same reason, request_id null', async () => {
  const { files } = await seeded({ context_sha256: undefined })
  await run(files)
  expect(JSON.parse(files.get(REQUEST)!)).toEqual({ consumed: true, request_id: null, verdict: 'dropped: stale or malformed request' })
})

test('M-V4 the in-flight token cannot be written: the request tombstone says `not attempted: ...` and nothing compacts', async () => {
  const { files, digest } = await seeded()
  let compacts = 0
  await run(files, {
    failWrite: p => p.endsWith('inflight'),
    compact: (async () => { compacts++; return { messages: [] } }) as never,
  })
  expect(compacts).toBe(0)
  expect(JSON.parse(files.get(REQUEST)!)).toEqual({
    consumed: true,
    request_id: digest,
    verdict: 'not attempted: could not write the in-flight token',
  })
})

test('M-V4 the request cannot be marked consumed: log-only, nothing written, nothing compacted', async () => {
  const { files } = await seeded()
  const before = files.get(REQUEST)
  let compacts = 0
  await run(files, {
    failWrite: p => p.endsWith('request.json'),
    compact: (async () => { compacts++; return { messages: [] } }) as never,
  })
  expect(compacts).toBe(0)
  expect(files.get(REQUEST)).toBe(before)
  expect(files.has(INFLIGHT)).toBe(false)
})

// ---- 5: one line, no control characters, capped

test('M-V5 a skip with a newline, ESC and 500 characters becomes ONE capped line', async () => {
  const { files } = await seeded()
  await run(files, veto('first\nsecond\r\n\x1b[31mred\x00\x07\t' + 'z'.repeat(500)))
  const v: string = inflight(files).verdict
  expect(v.length).toBeLessThanOrEqual(200)
  expect(v.length).toBe(200)
  expect(/[\u0000-\u001f\u007f]/.test(v)).toBe(false)
  expect(v.startsWith('vetoed: first second [31mred z')).toBe(true)
  // the file itself holds no raw newline either: it is one JSON line
  expect(files.get(INFLIGHT)!.includes('\n')).toBe(false)
})

test('M-V5 whitespace runs collapse and the ends are trimmed', async () => {
  const { files } = await seeded()
  await run(files, veto('  a \n\n  b\t\tc  '))
  expect(inflight(files).verdict).toBe('vetoed: a b c')
})

test('M-V5 a short, clean verdict is left exactly as built', async () => {
  const { files } = await seeded()
  await run(files, veto('x'.repeat(100)))
  expect(inflight(files).verdict).toBe('vetoed: ' + 'x'.repeat(100))
})

// ---- 6: a tombstone with a verdict still reads as absent, for both guards

test('M-V6 the answer-side guard: a verdict-bearing tombstone is NOT a live token', async () => {
  const digest = await sha256(CONTEXT)
  for (const extra of [{}, { at: T0 - 1_000 }]) {
    const { files } = await seeded()
    files.set(INFLIGHT, JSON.stringify({ consumed: true, request_id: digest, verdict: 'compacted', ...extra }))
    const hook = compactHook(fakeIo(files), 'beside')
    const out: any = await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => ({ messages: [ENGINE_MSG] })) as never)
    expect(out.messages).toEqual([ENGINE_MSG]) // not reused: the engine's own summary stands
  }
})

test('M-V6 a compaction the mod itself requested spends its token: the second attempt is not answered', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  await $.session.start(START)
  await $.turn.complete(COMPLETE) // consume: leaves a verdict-bearing tombstone in the in-flight file
  expect(readJson(seen, INFLIGHT).verdict).toBe('compacted')
  const out: any = await $.session.compact({ trigger: 'plugin', messages: TRANSCRIPT } as never)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M-V6 consume: a request tombstone with a verdict is never consumed again', async () => {
  const { files, digest } = await seeded()
  files.set(REQUEST, JSON.stringify({
    requested_at: iso(T0 - 5_000), session_id: 'sess-1', loop: false, context_sha256: digest,
    consumed: true, request_id: digest, verdict: 'dropped: stale or malformed request',
  }))
  let compacts = 0
  const compact = (async () => { compacts++; return { messages: [] } }) as never
  await run(files, { compact })
  await run(files, { compact })
  expect(compacts).toBe(0)
  expect(files.has(INFLIGHT)).toBe(false)
})

test('M-V6 a request is consumed once even though the run leaves a verdict behind', async () => {
  const { files } = await seeded()
  let compacts = 0
  const compact = (async () => { compacts++; return { messages: [ENGINE_MSG] } }) as never
  await run(files, { compact })
  await run(files, { compact })
  await run(files, { compact })
  expect(compacts).toBe(1)
})

// ---- 7: a failed verdict write never throws out of the hook

test('M-V7 the verdict write fails: the turn result still passes through, nothing throws', async () => {
  const { files } = await seeded()
  // the first inflight write (the live token) succeeds; the second (the verdict) fails
  const logs: string[] = []
  const out: any = await run(files, { failWrite: (p, n) => p.endsWith('inflight') && n === 2, logs })
  expect(out.text).toBe('done')
  // completeHook swallows anything consume throws, so the result alone cannot tell whether the tombstone's own
  // catch held; a throw escaping consume would be logged as `consume failed`
  expect(logs.filter(l => l.includes('consume failed'))).toEqual([])
  // the live token is left as written, which is the pre-existing posture when the tombstone write fails
  expect(JSON.parse(files.get(INFLIGHT)!).at).toBe(T0)
})

test('M-V7 the verdict write fails after a refusal: still no throw', async () => {
  const { files } = await seeded()
  const logs: string[] = []
  const out: any = await run(files, {
    failWrite: (p, n) => p.endsWith('inflight') && n === 2,
    compact: (async () => { throw new Error('boom') }) as never,
    logs,
  })
  expect(out.text).toBe('done')
  expect(logs.filter(l => l.includes('consume failed'))).toEqual([])
  expect(logs.some(l => l.includes('refused: Error: boom'))).toBe(true) // the refusal itself was still reported
})

// ---- C1 controls, hostile skips

test('M-V5 C1 controls (NEL, CSI) and U+2028 do not survive into the verdict file', async () => {
  const { files } = await seeded()
  await run(files, { compact: (async () => ({ skip: 'a\u0085b\u009bc\u2028d' })) as never })
  const v: string = JSON.parse(files.get(INFLIGHT)!).verdict
  expect(v).toBe('vetoed: a b c d')
  expect(files.get(INFLIGHT)!).not.toContain('\u0085')
  expect(files.get(INFLIGHT)!).not.toContain('\u009b')
})

test('M-V5 a very long skip is cut before the collapse and the verdict ends in a non-space', async () => {
  const { files } = await seeded()
  await run(files, { compact: (async () => ({ skip: 'x'.repeat(2_000_000) + ' ' })) as never })
  const v: string = JSON.parse(files.get(INFLIGHT)!).verdict
  expect(v.length).toBeLessThanOrEqual(200)
  expect(v.endsWith(' ')).toBe(false)
})

test('M-V5 a verdict whose cap lands exactly on a space is trimmed after the cap', async () => {
  const { files } = await seeded()
  // 'vetoed: ' is 8 characters, so 191 more put a space at index 199, the last character the cap keeps
  await run(files, { compact: (async () => ({ skip: 'y'.repeat(191) + ' z' })) as never })
  const v: string = JSON.parse(files.get(INFLIGHT)!).verdict
  expect(v).toBe('vetoed: ' + 'y'.repeat(191))
  expect(v.endsWith(' ')).toBe(false)
})

test('M-V2 skips that JSON cannot carry (BigInt inside an object, a symbol) are rendered, never thrown on', async () => {
  for (const skip of [{ a: 1n }, Symbol('s')]) {
    const { files } = await seeded()
    const logs: string[] = []
    const out: any = await run(files, { compact: (async () => ({ skip })) as never, logs })
    expect(out.text).toBe('done')
    expect(logs.filter(l => l.includes('consume failed'))).toEqual([])
    expect(JSON.parse(files.get(INFLIGHT)!).verdict).toMatch(/^vetoed: \S/)
  }
})

// ---- 8: with no verdict the tombstone text is byte-identical to the old one

test('M-V8 the request tombstone of a consumed request has no verdict field, byte for byte', async () => {
  const { files, digest } = await seeded()
  await run(files)
  expect(files.get(REQUEST)).toBe(JSON.stringify({ consumed: true, request_id: digest }))
  expect(files.get(REQUEST)).toBe(`{"consumed":true,"request_id":"${digest}"}`)
})

test('M-V8 the guard spending a token writes the old two-field text, byte for byte', async () => {
  const { files, digest } = await seeded()
  files.set(INFLIGHT, JSON.stringify({ request_id: digest, at: T0 - 61_000 })) // too old: spent without being answered
  const hook = compactHook(fakeIo(files), 'beside')
  await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => ({ messages: [ENGINE_MSG] })) as never)
  expect(files.get(INFLIGHT)).toBe(`{"consumed":true,"request_id":"${digest}"}`)
})

test('M-V8 a spent token with no usable id is written with request_id null and no verdict', async () => {
  const { files } = await seeded()
  files.set(INFLIGHT, JSON.stringify({ request_id: 5, at: T0 - 1_000 }))
  const hook = compactHook(fakeIo(files), 'beside')
  await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => ({ messages: [ENGINE_MSG] })) as never)
  expect(files.get(INFLIGHT)).toBe('{"consumed":true,"request_id":null}')
})

// ---- end to end through the harness's `$`

test('M-V1 through the harness: compact answers => `compacted`', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(readJson(seen, INFLIGHT).verdict).toBe('compacted')
})

test('M-V2 through the harness: compact vetoed beneath => `vetoed: <skip>`', ON, async ($, on) => {
  const { seen } = rig(on, { compact: 'skip' })
  await seedRequest(seen)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(readJson(seen, INFLIGHT).verdict).toBe('vetoed: vetoed by another hook')
})

test('M-V3 through the harness: compact refused beneath => `refused: <error>`', ON, async ($, on) => {
  const { seen } = rig(on, { compact: 'reject' })
  await seedRequest(seen)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  // the engine wraps a rejection beneath in its own error type and text, so pin the SHAPE (the exact text is the
  // engine's; the exact-text refused cell is the fake-io one above): one line, prefixed, non-empty, capped
  const verdict = readJson(seen, INFLIGHT).verdict as string
  expect(verdict).toMatch(/^refused: \S[^\n]*$/)
  expect(verdict.length).toBeLessThanOrEqual(200)
})
