// Route join cells (#3458 section B). Written BEFORE route.ts / the rating.ts join.
//
// The ledger is the one #3458 section A defines: one JSON line per route fire at
// `<session>/routes/<route>.jsonl`. The mod reads it through `$.fs.read` (Io.read).
import { expect, test } from 'claude-code/testing'
import { fireKeyVariants, sha256Hex } from './route.ts'
import {
  COMPLETE,
  LX,
  OB,
  READ_FIRE_KEY,
  PROBE_ROOT,
  START,
  STEP_RESULT,
  TURN,
  appended,
  drain,
  flush,
  ledgerFiles,
  nid,
  rig,
  routeLine,
  stepFile,
} from './kit.ts'
import type { TurnStepChunk } from 'claude-code'

const RATE = 'mcp__engram-probe__rate'
const RO1 = nid('ob', 4001)
const RO2 = nid('ob', 4002)
const RO3 = nid('ob', 4003)
const MISS_KEY = 'Read:0000000000000000'

function chunksWithInput(json: string, name = 'Read'): TurnStepChunk[] {
  return [
    { kind: 'thinking', index: 0, text: 'I should look at the recall probe ranking before deciding what to do next here. '.repeat(3) },
    { kind: 'tool', index: 2, id: 'toolu_1', name },
    { kind: 'input', index: 2, json },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ]
}

const INPUT_BY_STEP: Record<number, string> = { 0: '{"file_path":"/b.md"}', 1: '{"file_path":"/a.md"}' }

/** Two steps: step 0 reads /b.md, step 1 reads /a.md (the call READ_FIRE_KEY names). */
async function twoSteps($: any, on: any, options: Record<string, unknown> = {}) {
  const r = rig(on, options)
  on('turn.step', async function* (_$: any, e: any) {
    for (const c of chunksWithInput(INPUT_BY_STEP[e.index] ?? '{}')) yield c
    return { ...STEP_RESULT, index: e.index }
  })
  await $.session.start(START)
  for (const index of [0, 1]) {
    await drain($.turn.step({ turnId: TURN, index, model: 'test-model', messageCount: 3 }))
  }
  await flush(r.clock)
  return r
}

function joinFile(seen: { writes: Map<string, string> }) {
  const text = seen.writes.get(`${PROBE_ROOT}/route-joins/${TURN}.json`)
  return text === undefined ? undefined : (JSON.parse(text) as Record<string, any>)
}

function rowOf(text: string, index: number): string {
  return text.split('\n').find(l => l.startsWith(`s${index}  `)) ?? ''
}

// ---------------------------------------------------------------- hashing

test('R0 sha256Hex matches the known answer for "abc"', () => {
  expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
})

test('R0 fire_key = tool_name:sha256(tool_input JSON)[:16], measured against python hashlib', () => {
  expect(fireKeyVariants('Read', '{"file_path":"/a.md"}')).toContain(READ_FIRE_KEY)
  // key order and spacing as the host emitted them do not matter: python's json.dumps defaults are covered too
  expect(fireKeyVariants('Read', '{ "file_path": "/a.md" }')).toContain(READ_FIRE_KEY)
  // python's default json.dumps({"b":1,"a":"é"}) is '{"b": 1, "a": "é"}' -> 9d5df31df2744dd5
  expect(fireKeyVariants('Edit', '{"b":1,"a":"é"}')).toContain('Edit:9d5df31df2744dd5')
  // blank arguments = an empty tool_input
  expect(fireKeyVariants('Stop', '')).toContain(`Stop:${sha256Hex('{}').slice(0, 16)}`)
})

// ---------------------------------------------------------------- attach

test('R1 a prompt_surface line attaches to the turn\'s FIRST rated step as letter R', async ($, on) => {
  const files = ledgerFiles([routeLine('prompt_surface', 'abc123', [RO1, RO2])])
  const { seen } = await twoSteps($, on, { files })
  await $.turn.complete(COMPLETE)
  const text = appended(seen)[0]!.content[0]!.text
  expect(rowOf(text, 0)).toContain(`R: ${RO1} · ${RO2}`)
  expect(rowOf(text, 1)).toContain('R: -')
  const join = joinFile(seen)!
  expect(join.steps['0'].routes.map((r: any) => [r.route, r.letter, r.fire_key])).toEqual([['prompt_surface', 'R', 'abc123']])
  expect(join.steps['0'].routeOnlyIds).toEqual([RO1, RO2])
  expect(join.steps['1']?.routes ?? []).toEqual([])
})

test('R2 a tool_recall line whose fire_key matches a step\'s tool call attaches to THAT step as letter T', async ($, on) => {
  const files = ledgerFiles([routeLine('tool_recall', READ_FIRE_KEY, [RO3])])
  const { seen } = await twoSteps($, on, { files })
  await $.turn.complete(COMPLETE)
  const text = appended(seen)[0]!.content[0]!.text
  expect(rowOf(text, 1)).toContain(`T: ${RO3}`)
  expect(rowOf(text, 0)).toContain('T: -')
  const join = joinFile(seen)!
  const entry = join.steps['1'].routes[0]
  expect(entry.letter).toBe('T')
  expect(entry.fire_key).toBe(READ_FIRE_KEY)
  expect(entry.unmatched).toBeUndefined()
  expect(entry.line.shown).toEqual([RO3]) // the whole ledger line rides along for the converter
  expect(join.steps['0']?.routes ?? []).toEqual([])
})

test('R3 a tool_recall line matching no step attaches to step 0 with unmatched: true', async ($, on) => {
  const files = ledgerFiles([routeLine('tool_recall', MISS_KEY, [RO3])])
  const { seen } = await twoSteps($, on, { files })
  await $.turn.complete(COMPLETE)
  const text = appended(seen)[0]!.content[0]!.text
  expect(rowOf(text, 0)).toContain(`T: ${RO3}`)
  const join = joinFile(seen)!
  expect(join.steps['0'].routes[0].unmatched).toBe(true)
  expect(join.steps['1']?.routes ?? []).toEqual([])
})

test('R3b a line stamped after turn.complete is outside this turn: nothing attaches', async ($, on) => {
  const files = ledgerFiles([routeLine('prompt_surface', 'abc123', [RO1], { ts: 9_999_999 })])
  const { seen } = await twoSteps($, on, { files })
  await $.turn.complete(COMPLETE)
  expect(joinFile(seen)).toBeUndefined()
  expect(appended(seen)[0]!.content[0]!.text).not.toContain('R: ' + RO1)
})

test('R3c no ledger at all: no join file, and the request has no route cells', async ($, on) => {
  const { seen } = await twoSteps($, on, {})
  await $.turn.complete(COMPLETE)
  expect(joinFile(seen)).toBeUndefined()
  const text = appended(seen)[0]!.content[0]!.text
  expect(text.includes('R: ') || text.includes('T: ')).toBe(false)
})

// ---------------------------------------------------------------- dedup

test('R4 an id shown by an arm AND by route T appears once, in the route cell, marked ·also <arm letters>', async ($, on) => {
  const files = ledgerFiles([routeLine('tool_recall', READ_FIRE_KEY, [OB, RO3])])
  const { seen } = await twoSteps($, on, { files })
  await $.turn.complete(COMPLETE)
  const text = appended(seen)[0]!.content[0]!.text
  const row = rowOf(text, 1)
  expect(row.split(OB).length - 1).toBe(1) // never listed twice
  const step = stepFile(seen, TURN, 1)!
  const armLetters = Object.entries(step.armBlind as Record<string, string>)
    .filter(([, arm]) => (step.arms[arm]?.candidates ?? []).some((c: any) => c.id === OB))
    .map(([letter]) => letter)
    .sort()
  expect(armLetters.length > 0).toBe(true)
  expect(row).toContain(`T: ${OB} ·also ${armLetters.join('/')} · ${RO3}`)
  const entry = joinFile(seen)!.steps['1']
  expect(entry.routeOnlyIds).toEqual([RO3]) // the route-only count the issue asks to report
  expect(entry.alsoByArm[OB]).toEqual(armLetters)
  // the arm cells still list their other candidate
  expect(row).toContain(LX)
})

// ---------------------------------------------------------------- budget

test('R5 the rating request still never exceeds maxRatingChars with route rows in it', async ($, on) => {
  const many = Array.from({ length: 12 }, (_, i) => nid('ob', 5000 + i))
  const files = ledgerFiles([routeLine('prompt_surface', 'abc123', many), routeLine('tool_recall', READ_FIRE_KEY, many)])
  const { seen } = await twoSteps($, on, { files })
  await $.turn.complete(COMPLETE)
  const text = appended(seen)[0]!.content[0]!.text
  expect(text.length <= 1200).toBe(true)
})

// ---------------------------------------------------------------- rating round trip

test('R6 a rated route id lands in the ratings file and the join file carries its shown ids + fire_key', async ($, on) => {
  const files = ledgerFiles([routeLine('tool_recall', READ_FIRE_KEY, [RO3, RO1])])
  const { seen } = await twoSteps($, on, { files })
  await $.turn.complete(COMPLETE)
  const res = await $.tool.call({ tool: RATE, turnId: TURN, steps: [{ index: 1, good: [RO3], noise: [RO1] }] })
  expect(res.deny).toBeUndefined()
  const ratingKey = [...seen.writes.keys()].find(k => k.startsWith(`${PROBE_ROOT}/ratings/`))!
  const rating = JSON.parse(seen.writes.get(ratingKey)!)
  expect(rating.steps[0].good).toEqual([RO3])
  expect(rating.steps[0].noise).toEqual([RO1])
  expect(rating.unknownSteps).toBeUndefined()
  const entry = joinFile(seen)!.steps['1'].routes[0]
  expect(entry.fire_key).toBe(READ_FIRE_KEY)
  expect(entry.line.shown).toEqual([RO3, RO1])
  const request = JSON.parse(seen.writes.get(`${PROBE_ROOT}/requests/${TURN}.json`)!)
  expect(request.shown['1'].T).toEqual([RO3, RO1])
})

// ------------------------------------------- the turn window (round-2 fold)

const TURN2 = 'turn5678-aaaa-bbbb-cccc-dddddddddddd'
const TURN3 = 'turn9abc-aaaa-bbbb-cccc-dddddddddddd'
const RO4 = nid('ob', 4004)

/** One step of `turnId`, then settle: the turn has a step ready to complete. */
async function stepOnce($: any, turnId: string, r: { clock: any }, index = 0) {
  await drain($.turn.step({ turnId, index, model: 'test-model', messageCount: 3 }))
  await flush(r.clock)
}

function stepper(on: any) {
  on('turn.step', async function* (_$: any, e: any) {
    for (const c of chunksWithInput('{"file_path":"/b.md"}')) yield c
    return { ...STEP_RESULT, turnId: e.turnId, index: e.index }
  })
}

function rowText(seen: { logs: string[] }, which: number): string {
  return appended(seen as never)[which]!.content[0]!.text
}

function joinFor(seen: { writes: Map<string, string> }, turnId: string) {
  return seen.writes.get(`${PROBE_ROOT}/route-joins/${turnId}.json`)
}

test('R7 turn 2 MUST NOT see turn 1\'s prompt_surface line (the window\'s lower bound is the previous turn\'s end)', async ($, on) => {
  const files = ledgerFiles([
    routeLine('prompt_surface', 'k1', [RO1], { ts: 1_000 }),
    routeLine('prompt_surface', 'k2', [RO2], { ts: 2_500 }),
  ])
  const r = rig(on, { files })
  stepper(on)
  await $.session.start(START)
  await stepOnce($, TURN, r)
  await $.turn.complete(COMPLETE) // now = 1000: takes k1
  await r.clock.advance(2_000)
  await stepOnce($, TURN2, r)
  await $.turn.complete({ ...COMPLETE, turnId: TURN2 }) // now = 3000: takes k2 only
  expect(rowOf(rowText(r.seen, 0), 0)).toContain(`R: ${RO1}`)
  const second = rowOf(rowText(r.seen, 1), 0)
  expect(second).toContain(`R: ${RO2}`)
  expect(second).not.toContain(RO1)
})

test('R8 a reload (a fresh probe, a join file already on disk) does NOT re-attach the historical ledger line', async ($, on) => {
  const files = {
    ...ledgerFiles([
      routeLine('prompt_surface', 'hist', [RO1], { ts: 1_000 }),
      routeLine('prompt_surface', 'new', [RO2], { ts: 1_800 }),
    ]),
    [`${PROBE_ROOT}/route-joins/oldturn.json`]: JSON.stringify({ turnId: 'oldturn', ts: 1_500, steps: {} }),
  }
  const r = rig(on, { files })
  stepper(on)
  await $.session.start(START)
  await r.clock.advance(1_000)
  await stepOnce($, TURN, r)
  await $.turn.complete(COMPLETE) // now = 2000; the bound is the join's ts 1500, not 0
  const row = rowOf(rowText(r.seen, 0), 0)
  expect(row).toContain(`R: ${RO2}`)
  expect(row).not.toContain(RO1)
})

test('R9 a turn the probe never saw (turn.complete with no step state) still advances the bound', async ($, on) => {
  const files = ledgerFiles([routeLine('prompt_surface', 'lost', [RO4], { ts: 2_500 })])
  const r = rig(on, { files })
  stepper(on)
  await $.session.start(START)
  await r.clock.advance(2_000)
  await $.turn.complete({ ...COMPLETE, turnId: TURN2 }) // errored before its first step: no state, now = 3000
  await r.clock.advance(2_000)
  await stepOnce($, TURN3, r)
  await $.turn.complete({ ...COMPLETE, turnId: TURN3 }) // now = 5000: ts 2500 belongs to the unseen turn
  expect(rowText(r.seen, 0)).not.toContain(RO4)
  expect(joinFor(r.seen, TURN3)).toBeUndefined()
})

test('R8b a reload with NO join files but a bound.json does NOT re-attach the historical ledger line', async ($, on) => {
  const files = {
    ...ledgerFiles([
      routeLine('prompt_surface', 'hist', [RO1], { ts: 1_000 }),
      routeLine('prompt_surface', 'new', [RO2], { ts: 1_800 }),
    ]),
    [`${PROBE_ROOT}/bound.json`]: JSON.stringify({ ts: 1_500 }),
  }
  const r = rig(on, { files })
  stepper(on)
  await $.session.start(START)
  await r.clock.advance(1_000)
  await stepOnce($, TURN, r)
  await $.turn.complete(COMPLETE) // now = 2000; the bound is bound.json's 1500, not 0
  const row = rowOf(rowText(r.seen, 0), 0)
  expect(row).toContain(`R: ${RO2}`)
  expect(row).not.toContain(RO1)
})

test('R8c bound.json is written at an attach-less turn.complete (reason != answer), with that turn\'s end', async ($, on) => {
  const r = rig(on, { files: ledgerFiles([routeLine('prompt_surface', 'x', [RO1], { ts: 1_200 })]) })
  stepper(on)
  await $.session.start(START)
  await r.clock.advance(1_000)
  await stepOnce($, TURN, r)
  await $.turn.complete({ ...COMPLETE, reason: 'error' as never }) // no answer: no join file, now = 2000
  expect(joinFor(r.seen, TURN)).toBeUndefined()
  expect(JSON.parse(r.seen.writes.get(`${PROBE_ROOT}/bound.json`)!)).toEqual({ ts: 2_000 })
})

test('R8d a reload whose bound.json is newer than the last join uses bound.json (attach-less turns after the join)', async ($, on) => {
  const files = {
    ...ledgerFiles([
      routeLine('prompt_surface', 'hist', [RO1], { ts: 1_800 }),
      routeLine('prompt_surface', 'new', [RO2], { ts: 2_500 }),
    ]),
    [`${PROBE_ROOT}/route-joins/oldturn.json`]: JSON.stringify({ turnId: 'oldturn', ts: 1_500, steps: {} }),
    [`${PROBE_ROOT}/bound.json`]: JSON.stringify({ ts: 2_000 }),
  }
  const r = rig(on, { files })
  stepper(on)
  await $.session.start(START)
  await r.clock.advance(2_000)
  await stepOnce($, TURN, r)
  await $.turn.complete(COMPLETE) // now = 3000; bound 2000 (the join says 1500, which would let ts 1800 in)
  const row = rowOf(rowText(r.seen, 0), 0)
  expect(row).toContain(`R: ${RO2}`)
  expect(row).not.toContain(RO1)
})

test('R10 R lands on the first RATED step, not on step 0, when step 0 was skipped (no arms ran)', async ($, on) => {
  const files = ledgerFiles([routeLine('prompt_surface', 'abc123', [RO1])])
  const r = rig(on, { files })
  on('turn.step', async function* (_$: any, e: any) {
    // step 0: a tool call with no arguments and no thinking -> short-material skip, no digest
    const chunks: TurnStepChunk[] =
      e.index === 0
        ? [{ kind: 'tool', index: 2, id: 'toolu_0', name: 'Read' }, { kind: 'stop', stopReason: 'tool_use', usage: null }]
        : chunksWithInput('{"file_path":"/a.md"}')
    for (const c of chunks) yield c
    return { ...STEP_RESULT, index: e.index }
  })
  await $.session.start(START)
  for (const index of [0, 1]) await stepOnce($, TURN, r, index)
  expect(stepFile(r.seen, TURN, 0)?.skipped).toBe('short-material')
  expect(stepFile(r.seen, TURN, 1)?.skipped).toBeUndefined()
  await $.turn.complete(COMPLETE)
  const text = rowText(r.seen, 0)
  expect(rowOf(text, 1)).toContain(`R: ${RO1}`)
  expect(text).not.toContain(`s0  `)
  const join = JSON.parse(joinFor(r.seen, TURN)!)
  expect(Object.keys(join.steps)).toEqual(['1'])
})

test('R11 a ledger with lines but none in the window, newest ts > 6 h from now: logged once as a clock-domain mismatch', async ($, on) => {
  const far = 1_000 + 7 * 3_600_000
  const files = ledgerFiles([routeLine('prompt_surface', 'abc123', [RO1], { ts: far })])
  const r = rig(on, { files })
  stepper(on)
  await $.session.start(START)
  await stepOnce($, TURN, r)
  await $.turn.complete(COMPLETE)
  await stepOnce($, TURN2, r)
  await $.turn.complete({ ...COMPLETE, turnId: TURN2 })
  const hits = r.seen.logs.filter(l => l.includes('route ledger clock domain mismatch?'))
  expect(hits.length).toBe(1) // once, not once per turn
  expect(hits[0]).toContain(String(far))
  expect(hits[0]).toContain('vs now 1000')
})

test('R11b a line a few hours ahead is NOT flagged (only > 6 h)', async ($, on) => {
  const files = ledgerFiles([routeLine('prompt_surface', 'abc123', [RO1], { ts: 9_999_999 })])
  const r = rig(on, { files })
  stepper(on)
  await $.session.start(START)
  await stepOnce($, TURN, r)
  await $.turn.complete(COMPLETE)
  expect(r.seen.logs.some(l => l.includes('clock domain'))).toBe(false)
})
