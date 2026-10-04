import { expect, test } from 'claude-code/testing'
import { DEFAULTS } from './config.ts'
import { newProbe, writeUnderRoot, type Io } from './record.ts'
import {
  COMPLETE,
  LX,
  OB,
  candidateRow,
  PROBE_ROOT,
  START,
  STEP_RESULT,
  TURN,
  chunksFor,
  drain,
  engineStep,
  flush,
  rig,
  stepFile,
  stepFiles,
} from './kit.ts'

const STEP_INPUT = { turnId: TURN, index: 0, model: 'test-model', messageCount: 3 }
const ALL_ARMS = ['thinking_full', 'thinking_tail', 'extract_model', 'tool_input', 'novel_terms']

test('T1 passthrough: 7 chunks in order, payloads and result unchanged', async ($, on) => {
  const { clock, seen } = rig(on)
  const engine = chunksFor()
  engineStep(on, engine)
  await $.session.start(START)
  const { chunks, result } = await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(chunks).toEqual(engine)
  expect(result).toEqual(STEP_RESULT)
  expect(chunks.length).toBe(7)
  // not vacuous: the observer really ran behind the passthrough
  expect(stepFiles(seen).length).toBe(1)
})

test('T2 minQueryChars gate: short material -> skipped row and ZERO recall calls', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor('too short', ''))
  await $.session.start(START)
  const { chunks } = await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(chunks.length).toBe(7)
  const row = stepFile(seen, TURN, 0)
  expect(row?.skipped).toBe('short-material')
  expect(row?.arms).toBeUndefined()
  expect(seen.recallCalls.length).toBe(0)
  expect(seen.completeCalls).toBe(0)
  expect(seen.processCalls.length).toBe(0)
})

test('T3 qualifying step: exactly one file, armBlind permutation, every arm accounted for', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(stepFiles(seen)).toEqual([`${PROBE_ROOT}/steps/${TURN}-000.json`])
  const row = stepFile(seen, TURN, 0)!
  expect([...Object.values(row.armBlind)].sort()).toEqual([...ALL_ARMS].sort())
  expect(Object.keys(row.armBlind).sort()).toEqual(['A', 'B', 'C', 'D', 'E'])
  for (const name of ALL_ARMS) {
    const arm = row.arms[name]
    expect(arm).toBeDefined()
    expect(arm.ms >= 0).toBe(true)
    expect(Boolean(arm.candidates || arm.skipped || arm.error)).toBe(true)
  }
  expect(row.arms.thinking_full.candidates.map((c: any) => c.id)).toEqual([OB, LX])
  expect(row.arms.thinking_full.candidates[1].principle).toBe(true) // a lesson
  expect(row.arms.thinking_full.candidates[0].armsHit.length).toBeGreaterThan(1)
  expect(row.arms.tool_input.query).toBe('{"file_path":"/a.md"}')
  expect(row.arms.novel_terms.query).toBe('zymurgy')
  expect(row.arms.extract_model.query).toBe('recall phrase one phrase two phrase three')
  expect(row.engram_server).toBe('plugin_engram_engram')
  expect(row.toolCalls).toEqual([{ name: 'Read' }])
  expect(row.thinkingBlocks).toBe(1)
  expect(row.answerChars).toBe('Looking now.'.length)
  expect(seen.completeCalls).toBe(1) // at most one model.complete per step
  // every retrieval is engram_query via the discovered server, read-only usage
  expect(seen.recallCalls.every(c => c.tool === 'engram_query' && c.server === 'plugin_engram_engram')).toBe(true)
  const payload = JSON.parse(String(seen.recallCalls[0]!.args.payload_json))
  expect(payload.return_debug).toBe(true)
  expect(payload.top_k).toBe(5)
})

test('T3b joins thinking blocks with a blank line and clips the full-thinking query to 6000 chars', async ($, on) => {
  const { clock, seen } = rig(on)
  const huge = 'x'.repeat(7000)
  engineStep(on, [
    { kind: 'thinking', index: 0, text: huge },
    { kind: 'thinking', index: 1, text: 'second block.' },
    { kind: 'stop', stopReason: 'end_turn', usage: null },
  ])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.thinkingBlocks).toBe(2)
  expect(row.thinkingChars).toBe(7000 + 2 + 'second block.'.length)
  expect(row.arms.thinking_full.clipped).toBe(true)
  expect(row.arms.thinking_full.query.length).toBe(6000)
  expect(row.arms.thinking_full.query.endsWith('second block.')).toBe(true)
  expect(row.arms.tool_input.skipped).toBe('no-tool-call')
})

test('T4 arm failure isolation: one retrieval rejects, the others keep candidates', async ($, on) => {
  const { clock, seen } = rig(on, {
    recall: query => {
      if (query === 'zymurgy') throw new Error('retrieval exploded')
      return undefined
    },
  })
  const engine = chunksFor()
  engineStep(on, engine)
  await $.session.start(START)
  const { chunks, result } = await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(chunks).toEqual(engine)
  expect(result).toEqual(STEP_RESULT)
  const row = stepFile(seen, TURN, 0)!
  expect(typeof row.arms.novel_terms.error).toBe('string')
  expect(row.arms.novel_terms.candidates).toBeUndefined()
  expect(row.arms.thinking_full.candidates.length).toBe(2)
  expect(row.arms.thinking_tail.candidates.length).toBe(2)
})

test('T4b an engram_query error body is the arm error, not a crash', async ($, on) => {
  const { clock, seen } = rig(on, { recall: query => (query.startsWith('{') ? { error: 'bad payload' } : undefined) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.arms.tool_input.error).toContain('bad payload')
  expect(row.arms.thinking_full.candidates.length).toBe(2)
})

test('arm C answered:false -> that arm errors, no retrieval for it, others unaffected', async ($, on) => {
  const { clock, seen } = rig(on, { complete: () => ({ isAnswered: false, reason: 'aborted', usage: {} }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.arms.extract_model.error).toContain('aborted')
  expect(row.arms.extract_model.extract.isAnswered).toBe(false)
  expect(row.arms.extract_model.candidates).toBeUndefined()
  expect(row.arms.thinking_tail.candidates.length).toBe(2)
})

test('T7a a failing $.fs.write never reaches the chunks or the result', async ($, on) => {
  const { clock } = rig(on, { failWrites: true })
  const engine = chunksFor()
  engineStep(on, engine)
  await $.session.start(START)
  const { chunks, result } = await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(chunks).toEqual(engine)
  expect(result).toEqual(STEP_RESULT)
})

test('subagent steps pass through untouched: no file, no recall', async ($, on) => {
  const { clock, seen } = rig(on)
  const engine = chunksFor()
  engineStep(on, engine)
  await $.session.start(START)
  const { chunks } = await drain($.turn.step({ ...STEP_INPUT, agentId: 'agent-7' }))
  await flush(clock)
  expect(chunks).toEqual(engine)
  expect(stepFiles(seen).length).toBe(0)
  expect(seen.recallCalls.length).toBe(0)
})

test('no engram_query tool: the step row says so and no arm runs', async ($, on) => {
  const { clock, seen } = rig(on, { tools: ['Read'] })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(stepFile(seen, TURN, 0)?.skipped).toBe('no-engram-server')
  expect(seen.recallCalls.length).toBe(0)
  const note = seen.writes.get(`${PROBE_ROOT}/session-sess-1.json`)
  expect(JSON.parse(note ?? '{}').disabled).toBe('no engram_query tool')
})

test('HOME unset with a ~ probeDir: probe inactive, nothing written anywhere', async ($, on) => {
  const { clock, seen } = rig(on, { home: undefined })
  const engine = chunksFor()
  engineStep(on, engine)
  await $.session.start(START)
  const { chunks } = await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(chunks).toEqual(engine)
  expect(seen.writes.size).toBe(0)
  expect(seen.registered.length).toBe(0)
  expect(seen.logs.some(l => l.includes('HOME is unset'))).toBe(true)
})

test('session.start registers the rate tool and writes session.json', async ($, on) => {
  const { seen } = rig(on)
  await $.session.start(START)
  expect(seen.registered.length).toBe(1)
  expect((seen.registered[0] as { name: string }).name).toBe('rate')
  const session = JSON.parse(seen.writes.get(`${PROBE_ROOT}/session.json`) ?? '{}')
  expect(session.sessionId).toBe('sess-1')
  expect(session.engineVersion).toBe('2.1.289')
  expect(session.model).toBe('test-model')
  expect(session.arms).toEqual(ALL_ARMS)
})

test('inContext marks candidates whose ids already appear in the conversation, and messagesScanned says how many rows were read', async ($, on) => {
  const { clock, seen } = rig(on, {
    messages: [
      { role: 'user', text: 'earlier we cited ' + OB + ' here', toolUses: [] },
      { role: 'assistant', text: 'no ids in this one', toolUses: [] },
    ],
  })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.messagesScanned).toBe(2)
  const byId = Object.fromEntries(row.arms.thinking_full.candidates.map((c: any) => [c.id, c]))
  expect(byId[OB].inContext).toBe(true)
  expect(byId[LX].inContext).toBe(false)
})

test('invalid options fall back to the default for that key only, and say so', { options: { topK: 99, minQueryChars: -5, arms: 'thinking_tail,bogus' } }, async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor('short but non-empty thinking text', ''))
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(seen.logs.some(l => l.includes('topK=99') && l.includes('using default 5'))).toBe(true)
  expect(seen.logs.some(l => l.includes('minQueryChars=-5') && l.includes('using default 200'))).toBe(true)
  expect(seen.logs.some(l => l.includes('unknown arm'))).toBe(true)
  // minQueryChars fell back to 200, so this short thinking is skipped; arms = only the valid one
  const row = stepFile(seen, TURN, 0)!
  expect(row.skipped).toBe('short-material')
  expect(Object.values(row.armBlind)).toEqual(['thinking_tail'])
})

test('every retrieval payload carries refresh_recall:false (the probe must not re-stamp what it measures)', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(seen.recallCalls.length).toBeGreaterThan(0)
  for (const call of seen.recallCalls) {
    const raw = String(call.args.payload_json)
    expect(raw).toContain('"refresh_recall":false')
    expect(JSON.parse(raw).refresh_recall).toBe(false)
  }
})

test('an older server rejecting refresh_recall is recorded per arm and NOT retried without the flag', async ($, on) => {
  const { clock, seen } = rig(on, { recall: () => ({ error: "Unknown fields in payload_json: ['refresh_recall']" }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.arms.thinking_full.error).toContain('Unknown fields')
  expect(row.arms.thinking_full.candidates).toBeUndefined()
  // one call per querying arm, every one with the flag: no fallback call exists
  expect(seen.recallCalls.every(c => String(c.args.payload_json).includes('"refresh_recall":false'))).toBe(true)
  expect(seen.recallCalls.length).toBe(Object.values(row.arms).filter((a: any) => a.query).length)
})

test("inContext ignores the probe's own rating requests but still counts the same id in a plain row", async ($, on) => {
  const marked = '[engram-probe · turn ' + 'abcd1234] Rate what thinking-recall surfaced: ' + OB + ' ' + LX
  const first = rig(on, { messages: [{ role: 'user', text: marked, toolUses: [] }] })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(first.clock)
  const row = stepFile(first.seen, TURN, 0)!
  const byId = Object.fromEntries(row.arms.thinking_full.candidates.map((c: any) => [c.id, c]))
  expect(byId[OB].inContext).toBe(false)
  expect(byId[LX].inContext).toBe(false)
  expect(row.messagesScanned).toBe(1) // the row was read, then skipped
})

test('the same id in a plain row IS inContext (control for the marker skip)', async ($, on) => {
  const { clock, seen } = rig(on, { messages: [{ role: 'user', text: 'plain row mentioning ' + OB, toolUses: [] }] })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  const byId = Object.fromEntries(row.arms.thinking_full.candidates.map((c: any) => [c.id, c]))
  expect(byId[OB].inContext).toBe(true)
  expect(byId[LX].inContext).toBe(false)
})

test('writeUnderRoot refuses a path that climbs out of the session dir, or is absolute, and writes nothing', async () => {
  const written: string[] = []
  const logs: string[] = []
  const io = {
    write: async (path: string) => void written.push(path),
    log: (line: string) => void logs.push(line),
  } as unknown as Io
  const probe = newProbe(DEFAULTS)
  probe.root = PROBE_ROOT
  expect(await writeUnderRoot(io, probe, '../escape.json', '{}')).toBe(false)
  expect(await writeUnderRoot(io, probe, 'steps/../../escape.json', '{}')).toBe(false)
  expect(await writeUnderRoot(io, probe, '/etc/outside.json', '{}')).toBe(false)
  expect(written).toEqual([])
  expect(logs.length).toBeGreaterThan(0)
  // control: a plain relative path is written, under the root, exactly once
  expect(await writeUnderRoot(io, probe, 'steps/ok.json', '{}')).toBe(true)
  expect(written).toEqual([`${PROBE_ROOT}/steps/ok.json`])
  // an inactive probe (no root) writes nothing at all
  probe.root = null
  expect(await writeUnderRoot(io, probe, 'steps/ok.json', '{}')).toBe(false)
  expect(written.length).toBe(1)
})

test("arm E's result records which graph the helper measured (db from the helper output)", async ($, on) => {
  const { clock, seen } = rig(on, { novel: { terms: ['zymurgy'], novel: ['zymurgy'], db: '/seat/knowledge.db' } })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(stepFile(seen, TURN, 0)!.arms.novel_terms.db).toBe('/seat/knowledge.db')
})

test('step files carry only the selected candidate fields: no raw row, no quoted_text or interpretation', async ($, on) => {
  const full = (id: string) => ({
    id, type: 'observation', claim: 'c', quoted_text: 'PRIVATE QUOTE', interpretation: 'PRIVATE INTERP',
    source_url: 'https://x', composite_score: 0.5,
  })
  const { clock, seen } = rig(on, { recall: query => ({ query, results: [full(OB), full(LX)] }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const text = [...seen.writes.entries()].find(([k]) => k.includes('/steps/'))![1]
  expect(text).not.toContain('PRIVATE')
  const row = JSON.parse(text)
  const cand = row.arms.thinking_full.candidates[0]
  expect(cand.id).toBe(OB)
  for (const key of ['raw', 'quoted_text', 'interpretation', 'source_url']) expect(key in cand).toBe(false)
  expect(row.rawDropped).toBeUndefined()
})

// ---- envelope (live finding): the engram server wraps its JSON as {"result": "<json>"}
const wrap = (body: unknown) => ({ result: JSON.stringify(body) })

test('envelope: the real wrapped shape with results is unwrapped into candidates', async ($, on) => {
  const { clock, seen } = rig(on, { recall: query => wrap({ query, results: [candidateRow(OB), candidateRow(LX, { type: 'lesson' })] }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const arm = stepFile(seen, TURN, 0)!.arms.thinking_full
  expect(arm.error).toBeUndefined()
  expect(arm.candidates.map((c: any) => c.id)).toEqual([OB, LX])
  expect(arm.envelope).toBe('result-string')
})

test('envelope: a wrapped error keeps the server message under "engram_query error:"', async ($, on) => {
  const { clock, seen } = rig(on, { recall: () => wrap({ error: "Unknown fields in payload_json: ['refresh_recall']" }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const arm = stepFile(seen, TURN, 0)!.arms.thinking_full
  expect(arm.error.startsWith('engram_query error:')).toBe(true)
  expect(arm.error).toContain('Unknown fields')
})

test('envelope: the direct shape still works and says so', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const arm = stepFile(seen, TURN, 0)!.arms.thinking_full
  expect(arm.candidates.length).toBe(2)
  expect(arm.envelope).toBe('direct')
})

// ---- query material (live finding): some lineages emit thinking blocks with no text
const VISIBLE_ANSWER = 'Visible working text. '.repeat(20) // 440 chars
const visibleChunks = (thinking: string) => [
  ...(thinking === '' ? [] : [{ kind: 'thinking' as const, index: 0, text: thinking }]),
  { kind: 'text' as const, index: 1, text: VISIBLE_ANSWER },
  { kind: 'tool' as const, index: 2, id: 'toolu_1', name: 'Read' },
  { kind: 'input' as const, index: 2, json: '{"file_path":"/' + 'a'.repeat(200) + '"}' },
  { kind: 'stop' as const, stopReason: 'tool_use' as const, usage: null },
]

test('zero-thinking step with 600+ chars of answer and tool args: arms run over visible material', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, visibleChunks(''))
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.skipped).toBeUndefined()
  expect(row.querySource).toBe('visible')
  expect(row.thinkingChars).toBe(0)
  expect(row.visibleChars).toBeGreaterThan(600)
  expect(row.arms.thinking_full.query).toContain('Visible working text.')
  expect(row.arms.thinking_full.query).toContain('/aaaa')
  expect(row.arms.tool_input.query).toContain('/aaaa') // arm D unchanged
  expect(seen.recallCalls.length).toBeGreaterThan(0)
})

test('thinking at or above the threshold: querySource thinking', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.querySource).toBe('thinking')
  expect(row.arms.thinking_full.query).toContain('recall probe')
})

test('both thinking and visible material short: skipped short-material, zero recall calls', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, [
    { kind: 'thinking', index: 0, text: 'hmm' },
    { kind: 'text', index: 1, text: 'ok' },
    { kind: 'stop', stopReason: 'end_turn', usage: null },
  ])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.skipped).toBe('short-material')
  expect(seen.recallCalls.length).toBe(0)
})

test('querySource visible ignores long thinking; the old minThinkingChars key is an alias with a log line', { options: { querySource: 'visible', minThinkingChars: 50 } }, async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(stepFile(seen, TURN, 0)!.querySource).toBe('visible')
  expect(seen.logs.some(l => l.includes('minThinkingChars') && l.includes('alias'))).toBe(true)
})

test('envelope: a result string that is not JSON is an arm error naming it, envelope recorded', async ($, on) => {
  const { clock, seen } = rig(on, { recall: () => ({ result: 'this is not json' }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const arm = stepFile(seen, TURN, 0)!.arms.thinking_full
  expect(arm.error).toContain('result string is not JSON')
  expect(arm.envelope).toBe('result-string')
})

test('envelope: a result string that is JSON but not an object is an arm error naming it', async ($, on) => {
  const { clock, seen } = rig(on, { recall: () => ({ result: '42' }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const arm = stepFile(seen, TURN, 0)!.arms.thinking_full
  expect(arm.error).toContain('result string is not an object')
  expect(arm.envelope).toBe('result-string')
})

test('envelope is recorded on a wrapped server error row', async ($, on) => {
  const first = rig(on, { recall: () => ({ result: JSON.stringify({ error: 'boom' }) }) })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(first.clock)
  const arm = stepFile(first.seen, TURN, 0)!.arms.thinking_full
  expect(arm.error).toContain('boom')
  expect(arm.envelope).toBe('result-string')
})

test('model boundary: arm C never sends tool-call arguments to the side model; local arms keep them', async ($, on) => {
  const SENTINEL = 'SENTINEL_TOKEN_7f3a9c'
  const { clock, seen } = rig(on)
  engineStep(on, [
    { kind: 'text', index: 0, text: 'Plain answer text about the probe design. '.repeat(8) }, // ~340 chars
    { kind: 'tool', index: 1, id: 'toolu_1', name: 'Bash' },
    { kind: 'input', index: 1, json: `{"command":"curl -H ${SENTINEL}"}` },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.querySource).toBe('visible')
  // the side model (arm C) saw answer text only
  expect(seen.completePrompts.length).toBe(1)
  expect(seen.completePrompts[0]).not.toContain(SENTINEL)
  expect(seen.completePrompts[0]).toContain('Plain answer text')
  expect(row.extractChars).toBeGreaterThan(0)
  // arm D reads tool arguments (the local control)
  expect(row.arms.tool_input.query).toContain(SENTINEL)
  // arms A and B query the local ENGRAM server with the visible material, args included
  expect(row.arms.thinking_full.query).toContain(SENTINEL)
  expect(row.arms.thinking_tail.query).toContain(SENTINEL)
  const payloads = seen.recallCalls.map(c => String(c.args.payload_json))
  expect(payloads.some(p => p.includes(SENTINEL))).toBe(true)
  // arm E's local helper (python, no side model) is fed the same material on stdin
  expect(seen.processCalls[0]!.stdin).toContain(SENTINEL)
})

test('thinking mode: arm C input is the thinking text, unchanged', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(seen.completePrompts[0]).toContain('recall probe')
  expect(stepFile(seen, TURN, 0)!.extractChars).toBeGreaterThan(0)
})
