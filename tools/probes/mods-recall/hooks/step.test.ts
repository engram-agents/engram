import { expect, test } from 'claude-code/testing'
import { ALL_ARMS as CONFIG_ARMS, DEFAULTS, parseConfig } from './config.ts'
import { newProbe, writeUnderRoot, type Io } from './record.ts'
import {
  COMPLETE,
  LONG_THINKING,
  LX,
  OB,
  candidateRow,
  PROBE_HOME,
  PROBE_ROOT,
  START,
  STEP_RESULT,
  TURN,
  appended,
  blankStepChunks,
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

test('T2 minQueryChars gate: short material -> skipped row and ZERO recall calls', { options: { minQueryChars: 200 } }, async ($, on) => {
  const { clock, seen } = rig(on)
  // a tool call with NO arguments text: blank proxy material AND blank tool arguments
  engineStep(on, blankStepChunks('too short'))
  await $.session.start(START)
  const { chunks } = await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(chunks.length).toBe(4)
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

// alpha #3863 6b: the default probeDir follows ENGRAM_HOME, exactly as the hook's
// _hooklib.route_probe_dir() does (pinned from the Python side by tests/test_route_probe_dir_parity_3863.py).
test('default probeDir follows an absolute ENGRAM_HOME', async ($, on) => {
  const { seen } = rig(on, { engramHome: '/eh-store' })
  await $.session.start(START)
  expect(seen.writes.has(`/eh-store/probe/mods-recall/sess-1/session.json`)).toBe(true)
  expect([...seen.writes.keys()].some(k => k.startsWith(`${PROBE_HOME}/.engram/`))).toBe(false)
})

test('default probeDir ignores an EMPTY ENGRAM_HOME and uses HOME', async ($, on) => {
  const { seen } = rig(on, { engramHome: '' })
  await $.session.start(START)
  expect(seen.writes.has(`${PROBE_ROOT}/session.json`)).toBe(true)
})

test('a RELATIVE ENGRAM_HOME is refused: probe inactive, nothing written, one log line naming the value', async ($, on) => {
  const { seen } = rig(on, { engramHome: 'rel/store' })
  await $.session.start(START)
  expect(seen.writes.size).toBe(0)
  expect(seen.registered.length).toBe(0)
  expect(seen.logs.filter(l => l.includes('ENGRAM_HOME must be an absolute path') && l.includes('rel/store')).length).toBe(1)
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
  engineStep(on, blankStepChunks('short but non-empty thinking text'))
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  expect(seen.logs.some(l => l.includes('topK=99') && l.includes('using default 5'))).toBe(true)
  expect(seen.logs.some(l => l.includes('minQueryChars=-5') && l.includes('using default 0'))).toBe(true)
  expect(seen.logs.some(l => l.includes('unknown arm'))).toBe(true)
  // minQueryChars fell back to the default 0, so this short thinking is NOT skipped; arms = only the valid one
  const row = stepFile(seen, TURN, 0)!
  expect(row.skipped).toBeUndefined()
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

// the legacy default arms plus narration_full, for cells that assert the answer-text channel
const WITH_NARRATION = { arms: [...CONFIG_ARMS].join(',') }

// ---- query material (live finding): some lineages emit thinking blocks with no text
const VISIBLE_ANSWER = 'Visible working text. '.repeat(20) // 440 chars
const visibleChunks = (thinking: string) => [
  ...(thinking === '' ? [] : [{ kind: 'thinking' as const, index: 0, text: thinking }]),
  { kind: 'text' as const, index: 1, text: VISIBLE_ANSWER },
  { kind: 'tool' as const, index: 2, id: 'toolu_1', name: 'Read' },
  { kind: 'input' as const, index: 2, json: '{"file_path":"/' + 'a'.repeat(200) + '"}' },
  { kind: 'stop' as const, stopReason: 'tool_use' as const, usage: null },
]

// changed by design: thinking_full never falls back to answer text (design owner, engram-alpha#3549)
test('zero-thinking step with 600+ chars of answer and tool args: thinking_full skips no-thinking, narration_full uses the answer text only, D the tool arguments', { options: WITH_NARRATION }, async ($, on) => {
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
  expect(row.proxyChars).toBe(VISIBLE_ANSWER.length)
  expect(row.arms.thinking_full.skipped).toBe('no-thinking')
  expect(row.arms.narration_full.query).toContain('Visible working text.')
  expect(row.arms.narration_full.query).not.toContain('/aaaa') // tool arguments are arm D's material only
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

test('both thinking and answer short, zero tool calls (visible mode): terminal, zero recall calls', { options: { minQueryChars: 200 } }, async ($, on) => {
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
  expect(row.skipped).toBe('terminal')
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
  // arms A and B query the local ENGRAM server with the proxy material (answer text): no args
  expect(row.arms.thinking_full.query).not.toContain(SENTINEL)
  expect(row.arms.thinking_tail.query).not.toContain(SENTINEL)
  // only arm D's query carries the arguments
  const withSentinel = seen.recallCalls.filter(c => String(c.args.payload_json).includes(SENTINEL))
  expect(withSentinel.length).toBe(1)
  // arm E's local helper (python, no side model) is fed the proxy material on stdin
  expect(seen.processCalls[0]!.stdin).not.toContain(SENTINEL)
  expect(seen.processCalls[0]!.stdin).toContain('Plain answer text')
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

// ---- #3398: proxy material, terminal steps, arm C blank input and reply body
const BIG_ARGS = '{"command":"' + 'b'.repeat(700) + '"}'
const TOOL_ARGS_TEXT = 'ZZ_TOOL_ARG_ZZ'
const LONG_ANSWER = 'The probe should measure recall against the visible answer text. '.repeat(12) // ~770 chars

// changed by design: thinking_full never falls back to answer text (design owner, engram-alpha#3549)
test('T-N1 visible mode, empty answer text, tool arguments only: D alone runs; B/C/E skip short-proxy-material and A skips no-thinking; one recall, zero complete', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, [
    { kind: 'tool', index: 0, id: 'toolu_1', name: 'Bash' },
    { kind: 'input', index: 0, json: BIG_ARGS },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.skipped).toBeUndefined()
  expect(row.terminal).toBe(false)
  expect(row.querySource).toBe('visible')
  expect(row.proxyChars).toBe(0)
  expect(row.arms.thinking_full.skipped).toBe('no-thinking')
  expect(row.arms.thinking_full.candidates).toBeUndefined()
  for (const name of ['thinking_tail', 'extract_model', 'novel_terms']) {
    expect(row.arms[name].skipped).toBe('short-proxy-material')
    expect(row.arms[name].candidates).toBeUndefined()
  }
  expect(row.arms.tool_input.candidates.length).toBeGreaterThan(0)
  expect(seen.recallCalls.length).toBe(1)
  expect(seen.completeCalls).toBe(0)
  expect(seen.processCalls.length).toBe(0)
})

test('T-N2 visible mode, long answer, zero tool calls: terminal row, no arm, no call of any kind, no rating request', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, [
    { kind: 'text', index: 0, text: LONG_ANSWER },
    { kind: 'stop', stopReason: 'end_turn', usage: null },
  ])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.terminal).toBe(true)
  expect(row.skipped).toBe('terminal')
  expect(row.arms).toBeUndefined()
  expect(seen.recallCalls.length).toBe(0)
  expect(seen.completeCalls).toBe(0)
  expect(seen.processCalls.length).toBe(0)
  await $.turn.complete(COMPLETE)
  expect(appended(seen).length).toBe(0)
})

test('T-N2b visible mode, no thinking, no answer text, no tool calls: still terminal (the rule never needs text), no call of any kind', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, [{ kind: 'stop', stopReason: 'end_turn', usage: null }])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.terminal).toBe(true)
  expect(row.skipped).toBe('terminal')
  expect(row.arms).toBeUndefined()
  expect(seen.recallCalls.length).toBe(0)
  expect(seen.completeCalls).toBe(0)
})

test('T-N3 thinking mode, zero tool calls: not terminal; A/B/C/E run, D skips no-tool-call', async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, [
    { kind: 'thinking', index: 0, text: LONG_THINKING },
    { kind: 'stop', stopReason: 'end_turn', usage: null },
  ])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.querySource).toBe('thinking')
  expect(row.terminal).toBe(false)
  expect(row.skipped).toBeUndefined()
  for (const name of ['thinking_full', 'thinking_tail', 'extract_model', 'novel_terms']) {
    expect(row.arms[name].skipped).toBeUndefined()
    expect(Boolean(row.arms[name].candidates)).toBe(true)
  }
  expect(row.arms.tool_input.skipped).toBe('no-tool-call')
  expect(row.proxyChars).toBe(LONG_THINKING.length)
})

// changed by design: thinking_full never falls back to answer text (design owner, engram-alpha#3549)
test('T-N4 visible mode, answer + tool arguments: thinking_full skips no-thinking, narration_full queries the answer only, D the arguments only, C prompt has the answer only', { options: WITH_NARRATION }, async ($, on) => {
  const { clock, seen } = rig(on)
  const args = `{"command":"${TOOL_ARGS_TEXT}"}`
  engineStep(on, [
    { kind: 'text', index: 0, text: LONG_ANSWER },
    { kind: 'tool', index: 1, id: 'toolu_1', name: 'Bash' },
    { kind: 'input', index: 1, json: args },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ])
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.terminal).toBe(false)
  expect(row.arms.thinking_full.skipped).toBe('no-thinking')
  expect(row.arms.narration_full.query).toBe(LONG_ANSWER)
  expect(row.arms.narration_full.query).not.toContain(TOOL_ARGS_TEXT)
  expect(row.arms.tool_input.query).toBe(args)
  expect(seen.completePrompts.length).toBe(1)
  expect(seen.completePrompts[0]).toContain(LONG_ANSWER.slice(0, 80))
  expect(seen.completePrompts[0]).not.toContain(TOOL_ARGS_TEXT)
  expect(row.proxyChars).toBe(LONG_ANSWER.length)
  expect(row.extractChars).toBe(LONG_ANSWER.length)
})

test('T-N5 arm C not answered: the reply body (without text) is recorded in extract.detail, truncated to 400 + ellipsis', async ($, on) => {
  const { clock, seen } = rig(on, {
    complete: () => ({ isAnswered: false, reason: 'api-error', status: 529, message: 'overloaded' }),
  })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const arm = stepFile(seen, TURN, 0)!.arms.extract_model
  expect(arm.error.startsWith('extract not answered: api-error')).toBe(true)
  expect(typeof arm.extract.detail).toBe('string')
  expect(arm.extract.detail).toContain('529')
  expect(arm.extract.detail).toContain('overloaded')
  expect(arm.extract.detail).not.toContain('"text"')
})

test('T-N5b arm C detail: 1000-char field cut to 400 + ellipsis; a text field never rides along', async ($, on) => {
  const long = rig(on, {
    complete: () => ({ isAnswered: false, reason: 'api-error', status: 500, message: 'm'.repeat(1000), text: 'SHOULD_NOT_APPEAR' }),
  })
  engineStep(on, chunksFor())
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(long.clock)
  const detail = stepFile(long.seen, TURN, 0)!.arms.extract_model.extract.detail as string
  expect(detail.length).toBe(401)
  expect(detail.endsWith('…')).toBe(true)
  expect(detail).not.toContain('SHOULD_NOT_APPEAR')
})

// Floor 0 / auto fallback (#3362 part; Sol, 2026-10-05): with minQueryChars 0 an empty
// thinking must still fall back to the visible narration.
const NARRATION = 'Checking the narration channel. '.repeat(10).slice(0, 300) // 300 chars
const autoChunks = (thinking: string, narration: string) => [
  ...(thinking === '' ? [] : [{ kind: 'thinking' as const, index: 0, text: thinking }]),
  { kind: 'text' as const, index: 1, text: narration },
  { kind: 'tool' as const, index: 2, id: 'toolu_1', name: 'Read' },
  { kind: 'input' as const, index: 2, json: '{"file_path":"/' + 'a'.repeat(200) + '"}' },
  { kind: 'stop' as const, stopReason: 'tool_use' as const, usage: null },
]

// changed by design: thinking_full never falls back to answer text (design owner, engram-alpha#3549)
test('C1 auto, floor 0, EMPTY thinking: the visible narration is the proxy material; narration_full carries it, thinking_full is skipped', { options: { ...WITH_NARRATION, querySource: 'auto', minQueryChars: 0 } }, async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, autoChunks('', NARRATION))
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(NARRATION.length).toBe(300)
  expect(row.querySource).toBe('visible')
  expect(row.proxyChars).toBe(NARRATION.length)
  expect(row.arms.narration_full.query).toContain('Checking the narration channel.')
  expect(row.arms.thinking_full.skipped).toBe('no-thinking')
})

test('C2 auto, floor 0, whitespace-only thinking: still visible (the trim is load-bearing)', { options: { querySource: 'auto', minQueryChars: 0 } }, async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, autoChunks('   \n', NARRATION))
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.querySource).toBe('visible')
  expect(row.proxyChars).toBe(NARRATION.length)
})

test('C3 auto, floor 0, 2-char thinking: non-empty thinking wins even when tiny', { options: { querySource: 'auto', minQueryChars: 0 } }, async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, autoChunks('ok', NARRATION))
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.querySource).toBe('thinking')
  expect(row.proxyChars).toBe(2)
})

test('C4 auto, floor 200, 150-char thinking, 500-char narration: the floor still governs when > 1', { options: { querySource: 'auto', minQueryChars: 200 } }, async ($, on) => {
  const { clock, seen } = rig(on)
  engineStep(on, autoChunks('t'.repeat(150), 'N'.repeat(500)))
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(clock)
  const row = stepFile(seen, TURN, 0)!
  expect(row.querySource).toBe('visible')
  expect(row.proxyChars).toBe(500)
})

// The manifest half of "DEFAULTS and plugin.json agree" cannot run here: the hooks sandbox
// imports only .ts/.js files, so plugin.json is unreadable from a test cell (measured
// 2026-10-06: JSON import and node:fs import are both refused). Keep the two in step by hand.
test('C5 DEFAULTS.minQueryChars is 0 (plugin.json userConfig default is 0 too: not checkable from here)', async () => {
  expect(DEFAULTS.minQueryChars).toBe(0)
})

// ---- #3549 arms: narration_full, thinking_full keyed on THINKING only, `source` on every text-keyed arm
const PILOT = 'thinking_full,narration_full,tool_input'
const THINK_S = 'THINKING_SENTINEL_5c1d'
const ANSWER_S = 'ANSWER_SENTINEL_9e2b'
const TOOL_S = 'TOOLARG_SENTINEL_3a7f'
const thinkingText = `${THINK_S} ` + 'Weighing how the recall should be keyed. '.repeat(8)
const answerText = `${ANSWER_S} ` + 'Now reading the file to check the design. '.repeat(8)
const pilotChunks = (o: { thinking?: string; answer?: string; tool?: boolean }) => [
  ...(o.thinking ? [{ kind: 'thinking' as const, index: 0, text: o.thinking }] : []),
  ...(o.answer ? [{ kind: 'text' as const, index: 1, text: o.answer }] : []),
  ...(o.tool === false
    ? []
    : [
        { kind: 'tool' as const, index: 2, id: 'toolu_1', name: 'Bash' },
        { kind: 'input' as const, index: 2, json: `{"command":"${TOOL_S}"}` },
      ]),
  { kind: 'stop' as const, stopReason: (o.tool === false ? 'end_turn' : 'tool_use') as 'end_turn' | 'tool_use', usage: null },
]

async function pilotStep(on: any, $: any, chunks: any[], rigOptions = {}) {
  const r = rig(on, rigOptions)
  engineStep(on, chunks)
  await $.session.start(START)
  await drain($.turn.step(STEP_INPUT))
  await flush(r.clock)
  return { ...r, row: stepFile(r.seen, TURN, 0)! }
}

test('B1 config: the pilot arm list parses to exactly those three in order; an unknown arm is ignored and logged; narration_full is known', async () => {
  expect((CONFIG_ARMS as readonly string[]).includes('narration_full')).toBe(true)
  const logs: string[] = []
  const cfg = parseConfig({ arms: PILOT } as any, l => logs.push(l))
  expect(cfg.arms).toEqual(['thinking_full', 'narration_full', 'tool_input'])
  expect(logs).toEqual([])
  const logs2: string[] = []
  const cfg2 = parseConfig({ arms: 'narration_full,bogus_arm' } as any, l => logs2.push(l))
  expect(cfg2.arms).toEqual(['narration_full'])
  expect(logs2.some(l => l.includes('unknown arm') && l.includes('bogus_arm'))).toBe(true)
})

for (const querySource of ['auto', 'visible', 'thinking'] as const) {
  test(`B2 both channels (querySource ${querySource}): thinking_full reads the thinking only, narration_full the answer only, sources recorded`, { options: { arms: PILOT, querySource } }, async ($, on) => {
    const { row, seen } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: answerText }))
    const t = row.arms.thinking_full
    const n = row.arms.narration_full
    expect(t.query).toContain(THINK_S)
    expect(t.query).not.toContain(ANSWER_S)
    expect(n.query).toContain(ANSWER_S)
    expect(n.query).not.toContain(THINK_S)
    expect(t.source).toBe('thinking')
    expect(n.source).toBe('visible')
    expect(row.arms.tool_input.source).toBeUndefined()
    expect(row.arms.tool_input.query).toContain(TOOL_S)
    expect(Object.keys(row.arms).sort()).toEqual(['narration_full', 'thinking_full', 'tool_input'])
    expect(seen.completeCalls).toBe(0)
    expect(seen.processCalls.length).toBe(0)
  })

  test(`B3 no fallback (querySource ${querySource}): empty thinking -> thinking_full skips no-thinking, narration_full runs`, { options: { arms: PILOT, querySource } }, async ($, on) => {
    const { row, seen } = await pilotStep(on, $, pilotChunks({ answer: answerText }))
    expect(row.arms.thinking_full.skipped).toBe('no-thinking')
    expect(row.arms.thinking_full.query).toBeUndefined()
    expect(row.arms.thinking_full.candidates).toBeUndefined()
    expect(row.arms.narration_full.skipped).toBeUndefined()
    expect(row.arms.narration_full.query).toContain(ANSWER_S)
    expect(row.arms.narration_full.source).toBe('visible')
    // one recall for narration_full, one for tool_input: none for thinking_full
    expect(seen.recallCalls.length).toBe(2)
  })
}

test('B3b whitespace-only thinking is blank: no-thinking, never the answer text', { options: { arms: PILOT } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: '  \n ', answer: answerText }))
  expect(row.arms.thinking_full.skipped).toBe('no-thinking')
  expect(row.arms.narration_full.query).toContain(ANSWER_S)
})

test('B4 terminal: no tool call + thinking + answer -> thinking_full runs, narration_full skips terminal, row not terminal', { options: { arms: PILOT } }, async ($, on) => {
  const { row, seen } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: answerText, tool: false }))
  expect(row.querySource).toBe('thinking')
  expect(row.skipped).toBeUndefined()
  expect(row.terminal).toBe(false)
  expect(row.arms.thinking_full.query).toContain(THINK_S)
  expect(row.arms.narration_full.skipped).toBe('terminal')
  expect(row.arms.narration_full.query).toBeUndefined()
  expect(row.arms.tool_input.skipped).toBe('no-tool-call')
  expect(seen.recallCalls.length).toBe(1)
})

test('B4b no tool call + empty thinking + answer: the step-level terminal skip is unchanged', { options: { arms: PILOT } }, async ($, on) => {
  const { row, seen } = await pilotStep(on, $, pilotChunks({ answer: answerText, tool: false }))
  expect(row.skipped).toBe('terminal')
  expect(row.terminal).toBe(true)
  expect(row.arms).toBeUndefined()
  expect(seen.recallCalls.length).toBe(0)
})

test('B5 model boundary (pilot arms + extract_model): a tool-argument string reaches exactly one recall call, never narration_full, thinking_full or the side model', { options: { arms: PILOT + ',extract_model' } }, async ($, on) => {
  const { row, seen } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: answerText }))
  expect(row.arms.narration_full.query).not.toContain(TOOL_S)
  expect(row.arms.thinking_full.query).not.toContain(TOOL_S)
  expect(row.arms.tool_input.query).toContain(TOOL_S)
  expect(seen.completePrompts.length).toBe(1)
  expect(seen.completePrompts[0]).not.toContain(TOOL_S)
  const withSentinel = seen.recallCalls.filter(c => String(c.args.payload_json).includes(TOOL_S))
  expect(withSentinel.length).toBe(1)
  expect(JSON.parse(String(withSentinel[0]!.args.payload_json)).query).toBe(row.arms.tool_input.query)
})

test('B5b model boundary, answer-only step (no thinking): narration_full carries no tool argument', { options: { arms: PILOT, querySource: 'visible' } }, async ($, on) => {
  const { row, seen } = await pilotStep(on, $, pilotChunks({ answer: answerText }))
  expect(row.arms.narration_full.query).toContain(ANSWER_S)
  expect(row.arms.narration_full.query).not.toContain(TOOL_S)
  expect(seen.recallCalls.filter(c => String(c.args.payload_json).includes(TOOL_S)).length).toBe(1)
})

test('B6 short material, pilot arms: empty thinking, empty answer, no tool arguments -> short-material, no arm, no call', { options: { arms: PILOT } }, async ($, on) => {
  const { row, seen } = await pilotStep(on, $, [
    { kind: 'tool', index: 0, id: 'toolu_1', name: 'Bash' },
    { kind: 'stop', stopReason: 'tool_use', usage: null },
  ])
  expect(row.skipped).toBe('short-material')
  expect(row.arms).toBeUndefined()
  expect(seen.recallCalls.length).toBe(0)
})

test('B7a minQueryChars: short thinking -> thinking_full short-proxy-material while a long narration still runs', { options: { arms: PILOT, minQueryChars: 200, querySource: 'visible' } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: `${THINK_S} short`, answer: answerText }))
  expect(row.arms.thinking_full.skipped).toBe('short-proxy-material')
  expect(row.arms.narration_full.query).toContain(ANSWER_S)
})

test('B7b minQueryChars: short narration -> narration_full short-proxy-material while a long thinking still runs', { options: { arms: PILOT, minQueryChars: 200 } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: `${ANSWER_S} short` }))
  expect(row.arms.narration_full.skipped).toBe('short-proxy-material')
  expect(row.arms.thinking_full.query).toContain(THINK_S)
})

test('B7c an empty narration is no-narration, not short-proxy-material', { options: { arms: PILOT } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText }))
  expect(row.arms.narration_full.skipped).toBe('no-narration')
  expect(row.arms.thinking_full.query).toContain(THINK_S)
})

test('B7d both text arms clip to the last 6000 chars and say so', { options: { arms: PILOT } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: 'T'.repeat(7000), answer: 'N'.repeat(7000) }))
  for (const name of ['thinking_full', 'narration_full']) {
    expect(row.arms[name].clipped).toBe(true)
    expect(row.arms[name].query.length).toBe(6000)
  }
})

test('F1 narration_full skip order: terminal comes before no-narration (no tool call + blank answer + long thinking)', { options: { arms: PILOT } }, async ($, on) => {
  const { row } = await pilotStep(on, $, [
    { kind: 'thinking', index: 0, text: thinkingText },
    { kind: 'text', index: 1, text: '   ' },
    { kind: 'stop', stopReason: 'end_turn', usage: null },
  ])
  expect(row.querySource).toBe('thinking')
  expect(row.terminal).toBe(false)
  expect(row.arms.thinking_full.query).toContain(THINK_S)
  expect(row.arms.narration_full.skipped).toBe('terminal')
})

test('F2a a SKIPPED text-keyed arm result still carries its source', { options: { arms: PILOT } }, async ($, on) => {
  // no thinking, answer + tool call, visible: thinking_full skips no-thinking
  const a = await pilotStep(on, $, pilotChunks({ answer: answerText }))
  expect(a.row.arms.thinking_full.skipped).toBe('no-thinking')
  expect(a.row.arms.thinking_full.source).toBe('thinking')
})

test('F2a2 a SKIPPED narration_full (terminal) still carries source visible', { options: { arms: PILOT } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: answerText, tool: false }))
  expect(row.arms.narration_full.skipped).toBe('terminal')
  expect(row.arms.narration_full.source).toBe('visible')
})

test('F2b an ERRORED text-keyed arm result still carries its source', { options: { arms: PILOT } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: answerText }), {
    recall: (query: string) => (query.includes(THINK_S) || query.includes(ANSWER_S) ? { error: 'bad payload' } : undefined),
  })
  expect(row.arms.thinking_full.error).toContain('bad payload')
  expect(row.arms.thinking_full.source).toBe('thinking')
  expect(row.arms.narration_full.error).toContain('bad payload')
  expect(row.arms.narration_full.source).toBe('visible')
})

for (const [label, json] of [['no input block text', undefined], ['input {}', '{}']] as const) {
  test(`F3 a tool call with EMPTY arguments (${label}) is still a tool call: narration_full is not terminal`, { options: { arms: PILOT, querySource: 'visible' } }, async ($, on) => {
    const { row } = await pilotStep(on, $, [
      { kind: 'text', index: 0, text: answerText },
      { kind: 'tool', index: 1, id: 'toolu_1', name: 'Bash' },
      ...(json === undefined ? [] : [{ kind: 'input' as const, index: 1, json }]),
      { kind: 'stop', stopReason: 'tool_use', usage: null },
    ])
    expect(row.toolCalls).toEqual([{ name: 'Bash' }])
    expect(row.arms.narration_full.skipped).toBeUndefined()
    expect(row.arms.narration_full.query).toContain(ANSWER_S)
  })
}

test('B8 legacy arms keep their material and carry the step querySource as source', { options: { arms: 'thinking_tail,extract_model,novel_terms', querySource: 'visible' } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: answerText }))
  expect(row.querySource).toBe('visible')
  for (const name of ['thinking_tail', 'extract_model', 'novel_terms']) {
    expect(row.arms[name].source).toBe('visible')
  }
  expect(row.arms.thinking_tail.query).not.toContain(THINK_S)
  expect(row.arms.thinking_tail.query).toContain('design')
})

test('B8b legacy arms in thinking mode carry source thinking', { options: { arms: 'thinking_tail,extract_model,novel_terms' } }, async ($, on) => {
  const { row } = await pilotStep(on, $, pilotChunks({ thinking: thinkingText, answer: answerText }))
  expect(row.querySource).toBe('thinking')
  for (const name of ['thinking_tail', 'extract_model', 'novel_terms']) {
    expect(row.arms[name].source).toBe('thinking')
  }
})

test('B1b DEFAULTS.arms is exactly the five legacy arms, in order: the pilot is config only (plugin.json default is pinned to the same list in tests/test_probe_arms_narration_full.py)', async () => {
  expect([...DEFAULTS.arms]).toEqual(['thinking_full', 'thinking_tail', 'extract_model', 'tool_input', 'novel_terms'])
})
