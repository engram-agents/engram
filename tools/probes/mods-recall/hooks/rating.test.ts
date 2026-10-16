import { expect, test } from 'claude-code/testing'
import { REQUEST_MARKER, buildRequest, parseRating } from './rating.ts'
import {
  COMPLETE,
  LONG_THINKING,
  STEP_RESULT,
  LX,
  OB,
  appended,
  blankStepChunks,
  PROBE_ROOT,
  START,
  TURN,
  chunksFor,
  drain,
  engineStep,
  flush,
  rig,
  stepFile,
} from './kit.ts'

const RATE = 'mcp__engram-probe__rate'
const STEP_INPUT = { turnId: TURN, index: 0, model: 'test-model', messageCount: 3 }

async function oneStep($: any, on: any, options = {}) {
  const r = rig(on, options)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(r.clock)
  return r
}

// ------------------------------------------------------------------ T5

test('T5 turn.complete appends exactly one user row within the cap, naming the tool and the letters', async ($, on) => {
  const { seen } = await oneStep($, on)
  expect(appended(seen).length).toBe(0) // nothing is ever appended DURING a turn
  const done = await $.turn.complete(COMPLETE)
  expect(done.text).toBe('done') // the turn result is passed through unchanged
  expect(appended(seen).length).toBe(1)
  const row = appended(seen)[0]!
  expect(row.type).toBe('user')
  const text = row.content[0]!.text
  expect(text.length <= 1200).toBe(true)
  expect(text).toContain(RATE)
  expect(text).toContain(`[engram-probe · turn ${TURN.slice(0, 8)}]`)
  expect(text).toContain(`"turnId":"${TURN}"`)
  for (const letter of ['A', 'B', 'C', 'D', 'E']) expect(text).toContain(`${letter}: `)
  expect(text).toContain('s0  ')
  // a second turn.complete for the same turn has nothing left to append
  await $.turn.complete(COMPLETE)
  expect(appended(seen).length).toBe(1)
})

test('T5 a subagent turn appends nothing', async ($, on) => {
  const { seen } = await oneStep($, on)
  await $.turn.complete({ ...COMPLETE, agentId: 'agent-7' })
  expect(appended(seen).length).toBe(0)
})

test('T5 a turn that did not end in an answer appends nothing', async ($, on) => {
  const { seen } = await oneStep($, on)
  await $.turn.complete({ ...COMPLETE, reason: 'aborted', isAborted: true })
  expect(appended(seen).length).toBe(0)
})

test('T5 a turn whose steps were all skipped appends nothing', { options: { minQueryChars: 200 } }, async ($, on) => {
  const r = rig(on)
  engineStep(on, blankStepChunks())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(r.clock)
  expect(stepFile(r.seen, TURN, 0)?.skipped).toBe('short-material')
  await $.turn.complete(COMPLETE)
  expect(appended(r.seen).length).toBe(0)
})

test('T5 enabled=false', { options: { enabled: false } }, async ($, on) => {
  const r = rig(on)
  const engine = chunksFor()
  engineStep(on, engine)
  await $.session.start(START)
  const { chunks } = await drain($.turn.step(STEP_INPUT))
  await flush(r.clock)
  await $.turn.complete(COMPLETE)
  expect(chunks).toEqual(engine)
  expect(appended(r.seen).length).toBe(0)
  expect(r.seen.writes.size).toBe(0)
  expect(r.seen.registered.length).toBe(0)
  expect(r.seen.recallCalls.length).toBe(0)
})

test('an append that rejects: the turn result is unchanged and the failure is logged', async ($, on) => {
  const { seen } = await oneStep($, on) // no store beneath the hooks: the append rejects
  const done = await $.turn.complete(COMPLETE)
  expect(done.text).toBe('done')
  expect(appended(seen).length).toBe(1)
  expect(seen.logs.filter(l => l.includes('append rejected')).length).toBe(1)
})

// ------------------------------------------------------------------ the cap (pure)

function digest(index: number, perArm: number, summaryWords = 14) {
  const summary = Array.from({ length: summaryWords }, (_, i) => `word${i}`).join(' ')
  const byLetter: Record<string, { id: string; summary: string }[]> = {}
  for (const [k, letter] of ['A', 'B', 'C', 'D', 'E'].entries()) {
    byLetter[letter] = Array.from({ length: perArm }, (_, n) => ({
      id: `ob_${String(1000 + index * 100 + k * 10 + n)}`,
      summary,
    }))
  }
  return { index, byLetter }
}

test('buildRequest never exceeds the cap, across steps, ids and caps', async () => {
  for (const maxChars of [1200, 900, 600, 450, 300, 120, 40]) {
    for (const steps of [1, 2, 3, 8]) {
      for (const perArm of [1, 3, 5]) {
        const built = buildRequest({
          turnId: TURN,
          digests: Array.from({ length: steps }, (_, i) => digest(i + 3, perArm)),
          letters: ['A', 'B', 'C', 'D', 'E'],
          topK: 5,
          maxChars,
          dir: PROBE_ROOT,
        })
        expect(built.text.length <= maxChars).toBe(true)
      }
    }
  }
})

test('buildRequest: newest first; 3 ids per arm once >2 steps are shown; summaries clipped to 8 words; omission line', async () => {
  const letters = ['A', 'B', 'C', 'D', 'E']
  // roomy cap: all 4 steps fit, so each arm is clipped to 3 ids
  const many = buildRequest({ turnId: TURN, digests: [3, 4, 5, 6].map(i => digest(i, 5)), letters, topK: 5, maxChars: 4300, dir: PROBE_ROOT })
  expect(many.text.length <= 4300).toBe(true)
  expect(Object.keys(many.shown).sort()).toEqual(['3', '4', '5', '6'])
  for (const idx of Object.keys(many.shown)) expect(many.shown[idx]!.A!.length).toBe(3)
  expect(many.omittedSteps).toBe(0)
  expect(many.text).toContain('word7…') // 8 words kept, then clipped
  expect(many.text).not.toContain('word8')
  expect(many.text.indexOf('s6  ')).toBeLessThan(many.text.indexOf('s5  ')) // newest first

  // two steps shown: topK ids per arm
  const two = buildRequest({ turnId: TURN, digests: [3, 4].map(i => digest(i, 5)), letters, topK: 5, maxChars: 4300, dir: PROBE_ROOT })
  expect(two.shown['4']!.A!.length).toBe(5)

  // tighter cap: the oldest steps drop out, newest stay, and the line says how many
  const tight = buildRequest({ turnId: TURN, digests: [3, 4, 5, 6].map(i => digest(i, 5)), letters, topK: 5, maxChars: 2000, dir: PROBE_ROOT })
  expect(tight.text.length <= 2000).toBe(true)
  expect(Object.keys(tight.shown)).toContain('6')
  expect(tight.omittedSteps).toBe(4 - Object.keys(tight.shown).length)
  expect(tight.omittedSteps).toBeGreaterThan(0)
  expect(tight.text).toContain(`(+${tight.omittedSteps} earlier step`)
  expect(tight.text).toContain(PROBE_ROOT)
})

test('buildRequest: when one step cannot fit, ids go without summaries', async () => {
  const built = buildRequest({
    turnId: TURN,
    digests: [digest(3, 5)],
    letters: ['A', 'B', 'C', 'D', 'E'],
    topK: 5,
    maxChars: 560,
    dir: PROBE_ROOT,
  })
  expect(built.text.length <= 560).toBe(true)
  expect(built.text).not.toContain('word0')
  expect(built.text).toContain('A: ob_')
  expect(Object.keys(built.shown)).toEqual(['3'])
})

// ------------------------------------------------------------------ T6

async function callRate($: any, input: Record<string, unknown>) {
  return $.tool.call({ tool: RATE, ...input })
}

const GOOD = { turnId: TURN, steps: [{ index: 0, good: [OB], noise: [LX], pair: { A: 'better' } }] }

test('T6 rate: a valid call writes one ratings file and answers rated 1 step(s)', async ($, on) => {
  const { seen } = await oneStep($, on)
  const res = await callRate($, GOOD)
  expect(res.deny).toBeUndefined()
  expect(typeof res.result).toBe('string') // the engine's output shape: string | array | undefined
  expect(String(res.result).startsWith('rated ')).toBe(true)
  expect(res.result).toBe(`rated 1 step(s) for turn ${TURN.slice(0, 8)}`)
  const files = [...seen.writes.keys()].filter(k => k.startsWith(`${PROBE_ROOT}/ratings/`))
  expect(files.length).toBe(1)
  expect(files[0]!.startsWith(`${PROBE_ROOT}/ratings/${TURN}-`)).toBe(true)
  const file = JSON.parse(seen.writes.get(files[0]!)!)
  expect(file.turnId).toBe(TURN)
  expect(file.sessionId).toBe('sess-1')
  expect(file.ratedBy).toBe('test-model')
  expect(file.steps).toEqual(GOOD.steps)
  expect(file.unknownSteps).toBeUndefined()
})

test('T6 rate: an index with no step file is accepted and recorded as unknownSteps', async ($, on) => {
  const { seen } = await oneStep($, on)
  const res = await callRate($, { turnId: TURN, steps: [{ index: 0, good: [] }, { index: 9, good: [OB] }] })
  expect(typeof res.result).toBe('string')
  expect(String(res.result)).toContain('index 9')
  const path = [...seen.writes.keys()].find(k => k.includes('/ratings/'))!
  expect(JSON.parse(seen.writes.get(path)!).unknownSteps).toEqual([9])
})

test('T6 rate: pair {"A":"best"} is denied naming pair, and nothing is written', async ($, on) => {
  const { seen } = await oneStep($, on)
  const before = seen.writes.size
  const res = await callRate($, { turnId: TURN, steps: [{ index: 0, pair: { A: 'best' } }] })
  expect(typeof res.deny).toBe('string')
  expect(res.deny).toContain('pair')
  expect(seen.writes.size).toBe(before)
})

test('T6 rate: every malformed shape is denied naming the first bad field, writing nothing', async ($, on) => {
  const { seen } = await oneStep($, on)
  const before = seen.writes.size
  const cases: [Record<string, unknown>, string][] = [
    [{ steps: [{ index: 0 }] }, 'turnId'],
    [{ turnId: '../escape', steps: [{ index: 0 }] }, 'turnId'],
    [{ turnId: TURN }, 'steps'],
    [{ turnId: TURN, steps: [] }, 'steps'],
    [{ turnId: TURN, steps: [{ index: 1.5 }] }, 'steps[0].index'],
    [{ turnId: TURN, steps: [{ index: 0, good: ['not-an-id'] }] }, 'steps[0].good[0]'],
    [{ turnId: TURN, steps: [{ index: 0, noise: ['ob_1', OB] }] }, 'steps[0].noise[0]'],
    [{ turnId: TURN, steps: [{ index: 0 }, { index: 'x' }] }, 'steps[1].index'],
  ]
  for (const [input, field] of cases) {
    const res = await callRate($, input)
    expect(typeof res.deny).toBe('string')
    expect(res.deny).toContain(field)
  }
  expect(seen.writes.size).toBe(before)
})

test('T6 rate: pair {"C":"better"} is denied naming C (the request only offers A vs B), nothing written', async ($, on) => {
  const { seen } = await oneStep($, on)
  const before = seen.writes.size
  const res = await callRate($, { turnId: TURN, steps: [{ index: 0, pair: { C: 'better' } }] })
  expect(typeof res.deny).toBe('string')
  expect(res.deny).toContain('steps[0].pair.C')
  expect(res.deny).toContain('not A or B')
  expect(seen.writes.size).toBe(before)
})

test('rate is refused when the probe is inactive (HOME unset)', async ($, on) => {
  const { seen } = rig(on, { home: undefined })
  await $.session.start(START)
  const res = await callRate($, GOOD)
  expect(typeof res.deny).toBe('string')
  expect(seen.writes.size).toBe(0)
})

// ------------------------------------------------------------------ T7b

test('T7b a rate handler that throws is caught: the call falls through to the engine, no error escapes', async ($, on) => {
  // session.model throws inside the handler, after validation and before any
  // write; the registration's `.catch` replays `next`, so the engine's own
  // answer (the test's bottom tool.call hook) reaches the caller.
  const { seen } = rig(on, { modelThrows: true })
  on('tool.call', async () => ({ result: { content: [{ type: 'text', text: 'engine answered' }] } }) as never)
  await $.session.start(START)
  const before = seen.writes.size
  const res = await callRate($, { turnId: TURN, steps: [{ index: 0, good: [] }] })
  expect(res.deny).toBeUndefined()
  expect(JSON.stringify(res.result)).toContain('engine answered')
  expect(seen.writes.size).toBe(before)
})

test('parseRating accepts the documented shape and normalises defaults', async () => {
  const parsed = parseRating({ turnId: TURN, steps: [{ index: 2 }] })
  expect(parsed).toEqual({ ok: true, turnId: TURN, steps: [{ index: 2, good: [], noise: [] }] })
})

test('buildRequest leads with REQUEST_MARKER (step.ts skips rows containing it)', async () => {
  const built = buildRequest({ turnId: TURN, digests: [digest(3, 1)], letters: ['A', 'B', 'C', 'D', 'E'], topK: 5, maxChars: 1200, dir: PROBE_ROOT })
  expect(built.text.startsWith(REQUEST_MARKER)).toBe(true)
})

function requestFile(seen: { writes: Map<string, string> }) {
  const text = seen.writes.get(`${PROBE_ROOT}/requests/${TURN}.json`)
  return text === undefined ? undefined : JSON.parse(text)
}

test('requests file records the turn-end wait: nothing pending -> waitedMs 0, pendingAtComplete 0, no timeout flag', async ($, on) => {
  const { seen } = await oneStep($, on)
  await $.turn.complete(COMPLETE)
  const req = requestFile(seen)!
  expect(req.waitedMs).toBe(0)
  expect(req.pendingAtComplete).toBe(0)
  expect(req.pendingTimedOut).toBeUndefined()
  expect(req.appended).toBe(false) // the kit has no store: the append rejects
})

test('requests file records a job still running at turn end (waited, not timed out)', async ($, on) => {
  const r = rig(on)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT)) // the step job is scheduled on the clock, not yet run
  const done = $.turn.complete(COMPLETE)
  await r.clock.advance(10)
  await done
  const req = requestFile(r.seen)!
  expect(req.pendingAtComplete).toBe(1)
  expect(req.pendingTimedOut).toBeUndefined()
  expect(typeof req.waitedMs).toBe('number')
})

test('requests file records pendingTimedOut when the 5000 ms cap fires with a job still pending', async ($, on) => {
  const r = rig(on, { recall: query => (query.includes('HANGME') ? new Promise(() => {}) : undefined) })
  // one engine hook for both steps: step 1's thinking carries HANGME, so every
  // thinking-keyed arm of that step hangs and its job never settles
  on('turn.step', async function* (_$, e) {
    for (const c of chunksFor(e.index === 1 ? 'HANGME ' + LONG_THINKING : LONG_THINKING)) yield c
    return { ...STEP_RESULT, index: e.index }
  })
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT)) // step 0 completes normally
  await flush(r.clock)
  await drain($.turn.step({ ...STEP_INPUT, index: 1 }))
  await r.clock.advance(10)
  const done = $.turn.complete(COMPLETE)
  await r.clock.advance(5000)
  await done
  const req = requestFile(r.seen)!
  expect(req.pendingAtComplete).toBe(1)
  expect(req.pendingTimedOut).toBe(true)
  expect(req.waitedMs >= 5000).toBe(true)
  expect(Object.keys(req.shown)).toEqual(['0']) // only the finished step is shown
})

// ------------------------------------------------------------------ R1 (#3613 producer marker)

const EMPTY_ENGRAM_MARKER = '\u27e6ENGRAM\u27e7\u27e6/ENGRAM\u27e7'

test('R1 the request row ends with the empty ENGRAM marker, also when cut to a tiny cap', async () => {
  const letters = ['A', 'B', 'C', 'D', 'E']
  const whole = buildRequest({ turnId: TURN, digests: [digest(3, 1)], letters, topK: 5, maxChars: 4300, dir: PROBE_ROOT })
  expect(whole.text.startsWith(REQUEST_MARKER)).toBe(true)
  expect(whole.text.endsWith(`\n${EMPTY_ENGRAM_MARKER}`)).toBe(true)
  // Sweep every cap: (a) never exceeded, (b) head REQUEST_MARKER kept for every cap >= its length,
  // (c) trailing empty marker kept once maxChars - (marker + newline) >= REQUEST_MARKER.length.
  const threshold = REQUEST_MARKER.length + EMPTY_ENGRAM_MARKER.length + 1
  for (let maxChars = 0; maxChars <= 4300; maxChars++) {
    const cut = buildRequest({ turnId: TURN, digests: [digest(3, 5), digest(4, 5)], letters, topK: 5, maxChars, dir: PROBE_ROOT })
    expect(cut.text.length <= maxChars).toBe(true)
    if (maxChars >= REQUEST_MARKER.length) expect(cut.text.startsWith(REQUEST_MARKER)).toBe(true)
    if (maxChars >= threshold) expect(cut.text.endsWith(EMPTY_ENGRAM_MARKER)).toBe(true)
  }
})
