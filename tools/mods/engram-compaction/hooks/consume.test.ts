// Cells for the turn.complete half: consume a request ONCE (tombstone first), then compact.
// Contract-first (part of #3640): written before compaction.ts / register.ts existed.
import { expect, test } from 'claude-code/testing'
import { COMPACT_INSTRUCTIONS } from './compaction.ts'
import {
  COMPLETE,
  INFLIGHT,
  REQUEST,
  ON,
  START,
  T0,
  iso,
  readJson,
  rig,
  seedRequest,
} from './kit.ts'

const MIN = 60_000

test('M1 OFF: a request present is NOT consumed and nothing compacts', { options: { enabled: false } }, async ($, on) => {
  const { seen } = rig(on)
  const digest = await seedRequest(seen)
  const before = seen.writes.get(REQUEST)
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done')
  expect(seen.compactCalls.length).toBe(0)
  expect(seen.writes.get(REQUEST)).toBe(before)
  expect(seen.writes.has(INFLIGHT)).toBe(false)
  expect(readJson(seen, REQUEST).context_sha256).toBe(digest)
})

test('M2 ON happy path: tombstone and token are written BEFORE the one compact call', ON, async ($, on) => {
  const { seen } = rig(on)
  const digest = await seedRequest(seen)
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done') // the turn result passes through
  expect(seen.compactCalls.length).toBe(1)
  expect(seen.compactCalls[0]!.instructions).toBe(COMPACT_INSTRUCTIONS)
  const tomb = seen.events.indexOf('write:request.json:consumed')
  const token = seen.events.indexOf('write:inflight')
  const compact = seen.events.indexOf('compact')
  expect(tomb).toBeGreaterThanOrEqual(0)
  expect(token).toBeGreaterThanOrEqual(0)
  expect(tomb).toBeLessThan(compact)
  expect(token).toBeLessThan(compact)
  expect(readJson(seen, REQUEST)).toEqual({ consumed: true, request_id: digest })
})

test('M2 a second turn.complete with no live request calls nothing', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  await $.turn.complete(COMPLETE)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(1)
})

test('M2 a consumed request is never re-acted on, even one written by hand', ON, async ($, on) => {
  const { seen } = rig(on)
  seen.writes.set(REQUEST, JSON.stringify({ consumed: true, request_id: 'abc' }))
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
  // and a consumed file that still carries the old request's other fields is still consumed
  await seedRequest(seen, { consumed: true })
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
})

test('M3 a turn that did not end in an answer consumes nothing', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  const before = seen.writes.get(REQUEST)
  await $.session.start(START)
  await $.turn.complete({ ...COMPLETE, reason: 'aborted', isAborted: true })
  await $.turn.complete({ ...COMPLETE, reason: 'error' })
  await $.turn.complete({ ...COMPLETE, reason: 'refusal', refusal: { category: null, explanation: null } } as never)
  expect(seen.compactCalls.length).toBe(0)
  expect(seen.writes.get(REQUEST)).toBe(before)
  // control: the same request IS consumed by an answer
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(1)
})

test('M3 a subagent turn.complete (agentId) does not burn the request; the main loop still consumes it', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  const before = seen.writes.get(REQUEST)
  await $.session.start(START)
  const out = await $.turn.complete({ ...COMPLETE, agentId: 'agent-7' } as never)
  expect(out.text).toBe('done')
  expect(seen.compactCalls.length).toBe(0)
  expect(seen.writes.get(REQUEST)).toBe(before)
  expect(seen.writes.has(INFLIGHT)).toBe(false)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(1)
  expect(readJson(seen, REQUEST).consumed).toBe(true)
})

test('M4 a request for another session is not consumed', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen, { session_id: 'someone-else' })
  const before = seen.writes.get(REQUEST)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
  expect(seen.writes.get(REQUEST)).toBe(before)
})

test('M5 a request older than 30 minutes is tombstoned, not compacted', ON, async ($, on) => {
  const { seen } = rig(on)
  const digest = await seedRequest(seen, { requested_at: iso(T0 - 31 * MIN) })
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
  expect(readJson(seen, REQUEST)).toEqual({ consumed: true, request_id: digest })
})

test('M5 control: a 29 minute old request still compacts', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen, { requested_at: iso(T0 - 29 * MIN) })
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(1)
})

test('M5 a request without a usable time or digest is tombstoned, not compacted', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen, { requested_at: 'not a time' })
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
  expect(readJson(seen, REQUEST).consumed).toBe(true)
  await seedRequest(seen, { context_sha256: undefined })
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
  expect(readJson(seen, REQUEST).consumed).toBe(true)
})

test('M6 compact REJECTS: one log line, no throw, no retry', ON, async ($, on) => {
  const { seen } = rig(on, { compact: 'reject' })
  await seedRequest(seen)
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done')
  expect(seen.compactCalls.length).toBe(1)
  expect(seen.logs.filter(l => l.includes('engram-compaction')).length).toBe(1)
  await $.turn.complete(COMPLETE)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(1) // no retry loop
})

test('M6 compact returns { skip }: one log line, no throw, no retry', ON, async ($, on) => {
  const { seen } = rig(on, { compact: 'skip' })
  await seedRequest(seen)
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done')
  expect(seen.logs.filter(l => l.includes('engram-compaction')).length).toBe(1)
  expect(seen.logs.join('\n')).toContain('vetoed by another hook')
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(1)
})

test('M8 the request is tombstoned when compact REJECTS (consume-first is the no-retry guarantee)', ON, async ($, on) => {
  const { seen } = rig(on, { compact: 'reject' })
  const digest = await seedRequest(seen)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(readJson(seen, REQUEST)).toEqual({ consumed: true, request_id: digest })
  // the tombstone was written before the call, never after: exactly one request write, and it precedes `compact`
  const requestWrites = seen.events.filter(e => e.startsWith('write:request.json'))
  expect(requestWrites).toEqual(['write:request.json:consumed'])
  expect(seen.events.indexOf('write:request.json:consumed')).toBeLessThan(seen.events.indexOf('compact'))
  // the in-flight token is tombstoned too, on this path
  expect(readJson(seen, INFLIGHT).consumed).toBe(true)
})

for (const mode of ['skip', 'answer'] as const) {
  test(`M8 the in-flight token is tombstoned after compact ${mode}`, ON, async ($, on) => {
    const { seen } = rig(on, { compact: mode })
    await seedRequest(seen)
    await $.session.start(START)
    await $.turn.complete(COMPLETE)
    expect(readJson(seen, INFLIGHT).consumed).toBe(true)
  })
}

test('M6 a tombstone that cannot be written means NO compact call', ON, async ($, on) => {
  const { seen } = rig(on, { failWritesTo: ['request.json'] })
  await seedRequest(seen)
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done')
  expect(seen.compactCalls.length).toBe(0)
})

test('M9 an unreadable session id or file system never breaks the turn', ON, async ($, on) => {
  const a = rig(on, { idThrows: true })
  await seedRequest(a.seen)
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done')
  expect(a.seen.compactCalls.length).toBe(0)
})

test('M9 an unreadable file system never breaks the turn', ON, async ($, on) => {
  const b = rig(on, { readThrows: true })
  await seedRequest(b.seen)
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done')
  expect(b.seen.compactCalls.length).toBe(0)
})

test('M9 a malformed request file is left alone and never breaks the turn', ON, async ($, on) => {
  const { seen } = rig(on)
  seen.writes.set(REQUEST, '{not json')
  await $.session.start(START)
  const out = await $.turn.complete(COMPLETE)
  expect(out.text).toBe('done')
  expect(seen.compactCalls.length).toBe(0)
})

test('M1 the shipped default is OFF: a request present is not consumed', async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  const before = seen.writes.get(REQUEST)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
  expect(seen.writes.get(REQUEST)).toBe(before)
})

test('M2 without ENGRAM_HOME the data directory is $HOME/.engram', ON, async ($, on) => {
  const { seen } = rig(on, { env: { HOME: '/h' } })
  const digest = await seedRequest(seen)
  for (const name of ['request.json', 'context.md', 'context.sha256']) {
    seen.writes.set(`/h/.engram/compact/${name}`, seen.writes.get(`/eh/compact/${name}`)!)
  }
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(1)
  expect(JSON.parse(seen.writes.get('/h/.engram/compact/request.json')!)).toEqual({ consumed: true, request_id: digest })
})

test('M2 with neither ENGRAM_HOME nor HOME nothing is touched', ON, async ($, on) => {
  const { seen } = rig(on, { env: {} })
  await seedRequest(seen)
  await $.session.start(START)
  await $.turn.complete(COMPLETE)
  expect(seen.compactCalls.length).toBe(0)
})

for (const [label, text] of [['empty', ''], ['non-JSON text', 'garbage{']] as const) {
  test(`M9 a request.json that is ${label} compacts nothing, writes no token, throws nothing`, ON, async ($, on) => {
    const { seen } = rig(on)
    seen.writes.set(REQUEST, text)
    await $.session.start(START)
    const out = await $.turn.complete(COMPLETE)
    expect(out.text).toBe('done')
    expect(seen.compactCalls.length).toBe(0)
    expect(seen.writes.has(INFLIGHT)).toBe(false)
    expect(seen.writes.get(REQUEST)).toBe(text) // left as found: not rewritten, not tombstoned
    expect(seen.events).toEqual([])
    expect(seen.logs.length).toBe(0)
  })
}
