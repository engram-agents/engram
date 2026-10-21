// Cells for the session.compact half: answer only a fresh, matching, plugin-triggered compaction.
// Contract-first (part of #3640): written before compaction.ts / register.ts existed.
//
// Plugin-test limit: the harness does not stamp `trigger` on a compaction the plugin
// itself starts, so these cells pass `trigger` through the test's own call; the live
// trigger value is covered only by the probe nap.
import { expect, test } from 'claude-code/testing'
import { COMPACT_SHAPE, MAX_CONTEXT_CHARS, compactHook } from './compaction.ts'
import {
  CONTEXT,
  CONTEXT_MD,
  CONTEXT_SHA,
  ENGINE_MSG,
  INFLIGHT,
  ON,
  START,
  T0,
  TRANSCRIPT,
  readJson,
  rig,
  seedRequest,
  seedToken,
  sha256,
} from './kit.ts'

// `messages` is the transcript the engine always supplies; `next(e)` is refused without a non-empty list.
const plugin = { trigger: 'plugin', messages: TRANSCRIPT } as never

async function ready(seen: any) {
  const digest = await seedRequest(seen)
  await seedToken(seen)
  return digest
}

test('M7 the shipped shape is BESIDE', () => {
  expect(COMPACT_SHAPE).toBe('beside')
})

test('M7 valid context + fresh token: our message first, the engine messages beside it', ON, async ($, on) => {
  const { seen } = rig(on)
  await ready(seen)
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages.length).toBe(2)
  expect(out.messages[0].role).toBe('user')
  expect(out.messages[0].text).toBe(CONTEXT)
  expect(out.messages[0].text).toContain('FOCUS-LINE-VERBATIM')
  expect(out.messages[0].text).toContain('ARC-LINE-VERBATIM')
  expect(out.messages[1]).toEqual(ENGINE_MSG)
  // the token is spent
  expect(readJson(seen, INFLIGHT).consumed).toBe(true)
})

test('M7 a second compaction on the spent token passes through', ON, async ($, on) => {
  const { seen } = rig(on)
  await ready(seen)
  await $.session.start(START)
  await $.session.compact(plugin)
  const again: any = await $.session.compact(plugin)
  expect(again.messages).toEqual([ENGINE_MSG])
})

test('M7 no in-flight token: passes through next(e) although context.md is present', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
  expect(seen.writes.get(CONTEXT_MD)).toBe(CONTEXT)
})

test('M7 a token older than 60 s passes through and is spent', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  await seedToken(seen, { at: T0 - 61_000 })
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
  expect(readJson(seen, INFLIGHT).consumed).toBe(true)
})

test('M7 control: a token exactly 60 s old still answers', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  await seedToken(seen, { at: T0 - 60_000 })
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages.length).toBe(2)
})

test('M7 a consumed token passes through', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  seen.writes.set(INFLIGHT, JSON.stringify({ consumed: true, request_id: await sha256(CONTEXT) }))
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M7 a token for another request passes through', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen)
  await seedToken(seen, { request_id: 'f'.repeat(64) })
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M7 context.md changed after the request (hash mismatch): passes through', ON, async ($, on) => {
  const { seen } = rig(on)
  await ready(seen)
  seen.writes.set(CONTEXT_MD, CONTEXT + 'tampered\n')
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M7 the digest file disagrees with the token: passes through', ON, async ($, on) => {
  const { seen } = rig(on)
  await ready(seen)
  seen.writes.set(CONTEXT_SHA, 'a'.repeat(64) + '\n')
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M7 context.md or its digest missing: passes through, never an empty summary', ON, async ($, on) => {
  const { seen } = rig(on)
  await ready(seen)
  seen.writes.delete(CONTEXT_MD)
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
  await seedToken(seen)
  seen.writes.set(CONTEXT_MD, CONTEXT)
  seen.writes.delete(CONTEXT_SHA)
  const again: any = await $.session.compact(plugin)
  expect(again.messages).toEqual([ENGINE_MSG])
})

test('M7 an empty context is never installed as a summary', ON, async ($, on) => {
  const { seen } = rig(on)
  await seedRequest(seen, {}, '')
  await seedToken(seen, {}, '')
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M7 unreadable files: passes through, never throws', ON, async ($, on) => {
  const { seen } = rig(on, { readThrows: true })
  await ready(seen)
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M7 the engine vetoed beneath us: that veto stands', ON, async ($, on) => {
  const { seen } = rig(on, { compact: 'skip' })
  await ready(seen)
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.skip).toBe('vetoed by another hook')
  expect(out.messages).toBeUndefined()
})

for (const trigger of ['manual', 'auto', 'precompute', undefined]) {
  test(`M8 trigger ${String(trigger)}: untouched, and a pending token is left alone`, ON, async ($, on) => {
    const { seen } = rig(on)
    await ready(seen)
    const before = seen.writes.get(INFLIGHT)
    await $.session.start(START)
    const input: any = trigger === undefined ? { messages: TRANSCRIPT } : { trigger, messages: TRANSCRIPT }
    const out: any = await $.session.compact(input)
    expect(out.messages).toEqual([ENGINE_MSG])
    expect(seen.writes.get(INFLIGHT)).toBe(before)
    expect(seen.compactCalls.length).toBe(1)
  })
}

test('M8 control: the same files with trigger plugin DO answer', ON, async ($, on) => {
  const { seen } = rig(on)
  await ready(seen)
  await $.session.start(START)
  const out: any = await $.session.compact(plugin)
  expect(out.messages[0].text).toBe(CONTEXT)
})

// ---- replace vs beside: the one named constant, covered in both shapes at the hook level

function fakeIo(files: Map<string, string>, now = T0) {
  return {
    now: async () => now,
    read: async (p: string) => {
      const t = files.get(p)
      if (t === undefined) throw new Error('ENOENT')
      return t
    },
    write: async (p: string, t: string) => void files.set(p, t),
    exists: async (p: string) => files.has(p),
    sessionId: async () => 'sess-1',
    compact: async () => ({ messages: [] }) as never,
    log: (_: string) => {},
    home: async () => '/eh',
  }
}

async function filesReady() {
  const files = new Map<string, string>()
  const digest = await sha256(CONTEXT)
  files.set(CONTEXT_MD, CONTEXT)
  files.set(CONTEXT_SHA, digest + '\n')
  files.set(INFLIGHT, JSON.stringify({ request_id: digest, at: T0 - 1_000 }))
  return files
}

test('M7 shape beside: [ours] + the engine messages', async () => {
  const files = await filesReady()
  const hook = compactHook(fakeIo(files), 'beside')
  let nextCalls = 0
  const out: any = await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => {
    nextCalls++
    return { messages: [ENGINE_MSG, { ...ENGINE_MSG, text: 'kept' }] }
  }) as never)
  expect(out.messages.map((m: any) => m.text)).toEqual([CONTEXT, 'engine-summary', 'kept'])
  expect(nextCalls).toBe(1)
})

test('M7 shape replace: [ours] alone and the engine summarizer is never asked', async () => {
  const files = await filesReady()
  const hook = compactHook(fakeIo(files), 'replace')
  let nextCalls = 0
  const out: any = await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => {
    nextCalls++
    return { messages: [ENGINE_MSG] }
  }) as never)
  expect(out.messages.map((m: any) => m.text)).toEqual([CONTEXT])
  expect(nextCalls).toBe(0)
})

test('M7 shape replace still passes through when the guards fail', async () => {
  const files = await filesReady()
  files.delete(INFLIGHT)
  const hook = compactHook(fakeIo(files), 'replace')
  const out: any = await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => ({ messages: [ENGINE_MSG] })) as never)
  expect(out.messages).toEqual([ENGINE_MSG])
})

test('M8 the trigger gate at hook level: anything but plugin is next(e) untouched', async () => {
  const files = await filesReady()
  const hook = compactHook(fakeIo(files), 'replace')
  for (const trigger of ['manual', 'auto', 'precompute', undefined]) {
    const out: any = await hook({ trigger, messages: TRANSCRIPT } as never, (async () => ({ messages: [ENGINE_MSG] })) as never)
    expect(out.messages).toEqual([ENGINE_MSG])
  }
  expect(files.get(INFLIGHT)!.includes('consumed')).toBe(false)
})

test('M7 hook-side over-cap: a context of MAX_CONTEXT_CHARS + 1 characters (valid digest) is next(e), not ours', async () => {
  const big = 'x'.repeat(MAX_CONTEXT_CHARS + 1)
  const digest = await sha256(big)
  const files = new Map<string, string>()
  files.set(CONTEXT_MD, big)
  files.set(CONTEXT_SHA, digest + '\n')
  files.set(INFLIGHT, JSON.stringify({ request_id: digest, at: T0 - 1_000 }))
  const hook = compactHook(fakeIo(files), 'beside')
  const out: any = await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => ({ messages: [ENGINE_MSG] })) as never)
  expect(out.messages).toEqual([ENGINE_MSG])
  // control: exactly at the cap IS answered
  const fit = 'x'.repeat(MAX_CONTEXT_CHARS)
  const d2 = await sha256(fit)
  files.set(CONTEXT_MD, fit)
  files.set(CONTEXT_SHA, d2 + '\n')
  files.set(INFLIGHT, JSON.stringify({ request_id: d2, at: T0 - 1_000 }))
  const ok: any = await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => ({ messages: [ENGINE_MSG] })) as never)
  expect(ok.messages[0].text).toBe(fit)
})

test('M7 the hook itself (no .catch wrapper) returns a veto from beneath as that very skip', async () => {
  const files = await filesReady()
  const hook = compactHook(fakeIo(files), 'beside')
  const out: any = await hook({ trigger: 'plugin', messages: TRANSCRIPT } as never, (async () => ({ skip: 'vetoed beneath' })) as never)
  expect(out).toEqual({ skip: 'vetoed beneath' })
})
