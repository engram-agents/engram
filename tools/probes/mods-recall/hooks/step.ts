import { REQUEST_MARKER } from './rating.ts'
import type { HookStream, TurnStepChunk, TurnStepInput, TurnStepResult } from 'claude-code'
import { runArms, type ArmResult } from './arms.ts'
import type { ArmName } from './config.ts'
import { discoverServer, type Candidate } from './recall.ts'
import {
  SAFE_ID,
  logOnce,
  stepRelPath,
  turnState,
  writeUnderRoot,
  type Io,
  type Probe,
  type StepDigest,
  type TurnState,
} from './record.ts'

const ID_PATTERN = /\b[a-z]{2}_\d{4,}\b/g
const PRINCIPLE_TYPES = new Set(['lesson', 'cornerstone', 'axiom'])

/** What the observer collects from the chunks it watches go by. */
type Observed = {
  thinkingByBlock: Map<number, string>
  answerChars: number
  answerByBlock: Map<number, string>
  tools: { index: number; id: string; name: string }[]
  inputByBlock: Map<number, string>
  stopReason: string | null
  usage: unknown
  stopMs: number | null
}

function newObserved(): Observed {
  return {
    thinkingByBlock: new Map(),
    answerChars: 0,
    answerByBlock: new Map(),
    tools: [],
    inputByBlock: new Map(),
    stopReason: null,
    usage: null,
    stopMs: null,
  }
}

function observe(obs: Observed, chunk: TurnStepChunk): void {
  switch (chunk.kind) {
    case 'thinking':
      obs.thinkingByBlock.set(chunk.index, (obs.thinkingByBlock.get(chunk.index) ?? '') + chunk.text)
      break
    case 'text':
      obs.answerChars += chunk.text.length
      obs.answerByBlock.set(chunk.index, (obs.answerByBlock.get(chunk.index) ?? '') + chunk.text)
      break
    case 'tool':
      obs.tools.push({ index: chunk.index, id: chunk.id, name: chunk.name })
      break
    case 'input':
      obs.inputByBlock.set(chunk.index, (obs.inputByBlock.get(chunk.index) ?? '') + chunk.json)
      break
    case 'stop':
      obs.stopReason = chunk.stopReason
      obs.usage = chunk.usage
      break
    default:
      break
  }
}

/** The step's answer text only: its text blocks in block order, blanks dropped. */
function answerTextOf(obs: Observed): string {
  const keys = [...obs.answerByBlock.keys()].sort((a, b) => a - b)
  return keys.map(k => obs.answerByBlock.get(k) ?? '').filter(s => s.length > 0).join('\n\n')
}

/** The step's visible material: its answer text and every tool call's arguments JSON. */
function visibleMaterial(obs: Observed, toolArgs: string[]): string {
  const keys = [...obs.answerByBlock.keys()].sort((a, b) => a - b)
  return [...keys.map(k => obs.answerByBlock.get(k) ?? ''), ...toolArgs].filter(s => s.length > 0).join('\n\n')
}

function joinedThinking(obs: Observed): { text: string; blocks: number } {
  const keys = [...obs.thinkingByBlock.keys()].sort((a, b) => a - b)
  return { text: keys.map(k => obs.thinkingByBlock.get(k) ?? '').join('\n\n'), blocks: keys.length }
}

/** Node ids already in the conversation (regex over every row's text). */
async function inContextIds(io: Io): Promise<{ ids: Set<string>; scanned: number }> {
  const ids = new Set<string>()
  try {
    const rows = await io.messages()
    if (!Array.isArray(rows)) return { ids, scanned: 0 }
    for (const row of rows as { text?: unknown }[]) {
      if (typeof row.text !== 'string') continue
      // the probe's own rating requests list ids; counting them would make every
      // surfaced id look "already in context" on later turns
      if (row.text.includes(REQUEST_MARKER)) continue
      for (const m of row.text.matchAll(ID_PATTERN)) ids.add(m[0])
    }
    return { ids, scanned: rows.length }
  } catch {
    return { ids, scanned: 0 }
  }
}

function shortSummary(c: Candidate): string {
  const text = (c.recall_summary ?? c.claim ?? '').replace(/\s+/g, ' ').trim()
  return text
}

/** Cross-arm fields onto every candidate; mutates the arm results in place. */
export function annotateCandidates(arms: Record<string, ArmResult>, inContext: Set<string>): void {
  const hits = new Map<string, string[]>()
  for (const [name, arm] of Object.entries(arms)) {
    for (const c of arm.candidates ?? []) {
      const list = hits.get(c.id) ?? []
      list.push(name)
      hits.set(c.id, list)
    }
  }
  for (const arm of Object.values(arms)) {
    for (const c of arm.candidates ?? []) {
      Object.assign(c, {
        armsHit: hits.get(c.id) ?? [],
        inContext: inContext.has(c.id),
        principle: PRINCIPLE_TYPES.has(c.type ?? ''),
      })
    }
  }
}

function digestOf(index: number, blind: Record<string, ArmName>, arms: Record<string, ArmResult>): StepDigest {
  const byLetter: StepDigest['byLetter'] = {}
  for (const [letter, name] of Object.entries(blind)) {
    byLetter[letter] = (arms[name]?.candidates ?? []).map(c => ({ id: c.id, summary: shortSummary(c) }))
  }
  return { index, byLetter }
}

/**
 * The step row's JSON, as is. Candidates carry only the selected fields
 * recall.ts names; the raw debug row (a full node dict: claim, quotes,
 * interpretation, source) is never kept, so step files do not duplicate
 * private graph content.
 */
export function serializeRow(row: Record<string, unknown>): string {
  return JSON.stringify(row)
}

type StepJob = {
  io: Io
  probe: Probe
  turn: TurnState
  input: Pick<TurnStepInput, 'turnId' | 'index' | 'model'>
  obs: Observed
  stepMs: number | null
  toolArgs: string[]
}

/** The post-step work: runs from a timer, never inside the turn's own dispatch. */
async function stepJob(job: StepJob): Promise<void> {
  const { io, probe, turn, input, obs } = job
  const { cfg } = probe
  const { text: thinking, blocks } = joinedThinking(obs)
  const visible = visibleMaterial(obs, job.toolArgs)
  // Query source: thinking, or the step's visible text. `auto` (default) takes
  // thinking when it is long enough, else visible: some lineages emit thinking
  // blocks with (almost) no text.
  const useThinking =
    cfg.querySource === 'thinking' ||
    (cfg.querySource === 'auto' && thinking.length >= cfg.minQueryChars)
  // Proxy material: what the thinking-proxy arms (A, B, C, E) query: the thinking
  // (thinking mode) or the step's answer text ONLY (visible mode). Tool-call
  // arguments are arm D's material, never part of it. That also keeps arm C (the
  // only arm that leaves the machine, a side model) free of tool arguments, which
  // can carry tokens or file contents.
  const answerText = answerTextOf(obs)
  const proxyMaterial = useThinking ? thinking : answerText
  // equals proxyMaterial by construction (arm C's blank check is proxyTooShort); the model-boundary test pins that the tool-argument sentinel is absent
  const extractInput = proxyMaterial
  const hasToolArgs = job.toolArgs.some(a => a.trim().length > 0)
  // Terminal: visible mode and no tool call. With thinking hidden, the only
  // material of such a step is the final answer, emitted after recall could help.
  const terminal = !useThinking && obs.tools.length === 0
  const row: Record<string, unknown> = {
    sessionId: probe.sessionId,
    turnId: input.turnId,
    index: input.index,
    model: input.model,
    stopReason: obs.stopReason,
    usage: obs.usage,
    thinkingChars: thinking.length,
    thinkingBlocks: blocks,
    visibleChars: visible.length,
    extractChars: extractInput.length,
    proxyChars: proxyMaterial.length,
    answerChars: obs.answerChars,
    toolCalls: obs.tools.map(t => ({ name: t.name })),
    stepMs: job.stepMs,
    engram_server: probe.server,
    armBlind: turn.blind,
    ts: await io.now(),
  }

  let server = probe.server
  if (server === null) {
    // MCP servers may connect after session.start: look again, cheaply.
    try {
      server = discoverServer(await io.toolNames()) ?? null
    } catch {
      server = null
    }
    if (server !== null) {
      probe.server = server
      row.engram_server = server
    }
  }

  row.querySource = useThinking ? 'thinking' : 'visible'
  row.terminal = terminal
  if (terminal) {
    row.skipped = 'terminal'
  } else if ((proxyMaterial.trim().length === 0 || proxyMaterial.length < cfg.minQueryChars) && !hasToolArgs) {
    // no proxy material AND nothing for arm D either: nothing to measure
    row.skipped = 'short-material'
  } else if (server === null) {
    row.skipped = 'no-engram-server'
  } else if (probe.root === null || probe.sessionId === null) {
    row.skipped = 'probe-inactive'
  } else {
    const { ids, scanned } = await inContextIds(io)
    row.messagesScanned = scanned
    const arms = await runArms({
      io,
      probe,
      cfg,
      server,
      material: proxyMaterial,
      extractInput,
      toolArgs: job.toolArgs,
      sessionId: probe.sessionId,
    })
    annotateCandidates(arms, ids)
    for (const arm of Object.values(arms)) {
      (arm as ArmResult & { msOverStep?: number | null }).msOverStep =
        job.stepMs !== null && job.stepMs > 0 ? arm.ms / job.stepMs : null
    }
    row.arms = arms
    turn.digests.push(digestOf(input.index, turn.blind, arms))
  }
  await writeUnderRoot(io, probe, stepRelPath(input.turnId, input.index), serializeRow(row))
}

/**
 * The `turn.step` hook body (an async generator). Observes only: every chunk
 * is yielded as received, in order, and the result returned as received.
 */
export type StepNext = (e: TurnStepInput) => HookStream<TurnStepChunk, TurnStepResult>

export function stepHook(probe: Probe) {
  return async function* (io: Io, e: Readonly<TurnStepInput>, next: StepNext): AsyncGenerator<TurnStepChunk, TurnStepResult> {
    // Subagent loops and an inactive probe: a pure pass-through.
    if (e.agentId !== undefined || probe.disabled !== null || probe.root === null || !SAFE_ID.test(e.turnId)) {
      return yield* next(e as TurnStepInput)
    }
    let startMs: number | null = null
    try {
      startMs = await io.now()
    } catch {
      startMs = null
    }
    const obs = newObserved()
    const stream = next(e as TurnStepInput)
    for await (const chunk of stream) {
      try {
        observe(obs, chunk)
        if (chunk.kind === 'stop') obs.stopMs = await io.now()
      } catch {
        // observation never affects the stream
      }
      yield chunk
    }
    const result = await stream.result

    try {
      let endMs = obs.stopMs
      if (endMs === null) endMs = await io.now()
      const stepMs = startMs !== null && endMs !== null ? Math.max(0, endMs - startMs) : null
      const turn = turnState(probe, e.turnId, probe.cfg.arms)
      turn.stepsSeen += 1
      const toolArgs = obs.tools.map(t => obs.inputByBlock.get(t.index) ?? '')
      let release: () => void = () => {}
      turn.pending.push(new Promise<void>(resolve => (release = resolve)))
      const job: StepJob = { io, probe, turn, input: e, obs, stepMs, toolArgs }
      io.after(0, () => {
        stepJob(job)
          .catch(err => logOnce(io, probe, 'step-job', `step job failed: ${String(err).slice(0, 160)}`))
          .finally(() => {
            turn.jobsDone += 1
            release()
          })
      })
    } catch (err) {
      logOnce(io, probe, 'step-schedule', `could not schedule the step job: ${String(err).slice(0, 160)}`)
    }
    return result
  }
}

export { ID_PATTERN }
