import type { TurnCompleteInput, TurnCompleteResult } from 'claude-code'
import {
  SAFE_ID,
  logOnce,
  stepAbsPath,
  writeUnderRoot,
  type Io,
  type Probe,
  type StepDigest,
} from './record.ts'

/** Leads every rating request; step.ts skips rows containing it (inContext). */
export const REQUEST_MARKER = '[engram-probe · turn '
export const RATE_TOOL = 'mcp__engram-probe__rate'
export const NODE_ID = /^[a-z]{2}_\d{4,}$/
const VERDICTS = ['better', 'same', 'worse'] as const

/** How long turn.complete waits for the last step's arms (inside the 10 s hook budget). */
export const WAIT_PENDING_MS = 5000

// ---------------------------------------------------------------- the request

function words(text: string, n: number): string {
  const w = text.replace(/"/g, "'").split(/\s+/).filter(s => s.length > 0)
  return w.length <= n ? w.join(' ') : w.slice(0, n).join(' ') + '…'
}

type Level = { limit: number; summaries: boolean }

function stepLine(d: StepDigest, letters: string[], level: Level): string {
  const cells = letters.map(letter => {
    const cands = (d.byLetter[letter] ?? []).slice(0, level.limit)
    if (cands.length === 0) return `${letter}: -`
    const shown = cands.map(c => {
      const s = level.summaries ? words(c.summary, 8) : ''
      return s.length > 0 ? `${c.id} "${s}"` : c.id
    })
    return `${letter}: ${shown.join(' · ')}`
  })
  return `s${d.index}  ${cells.join(' | ')}`
}

export type RequestBuild = {
  text: string
  /** index -> letter -> ids actually shown */
  shown: Record<string, Record<string, string[]>>
  omittedSteps: number
}

/**
 * The one rating-request row. Hard cap `maxChars` (never exceeded); steps are
 * included newest-first; at most `topK` ids per arm, clipped to 3 once more
 * than 2 steps are shown; summaries clipped to 8 words. If one step does not
 * fit, its ids go without summaries, and (not in the spec, to honour the hard
 * cap) with fewer ids, down to one per arm.
 */
export function buildRequest(args: {
  turnId: string
  digests: StepDigest[]
  letters: string[]
  topK: number
  maxChars: number
  dir: string
}): RequestBuild {
  const { turnId, letters, topK, maxChars, dir } = args
  const newestFirst = [...args.digests].sort((a, b) => b.index - a.index)
  const head = [
    `${REQUEST_MARKER}${turnId.slice(0, 8)}] Rate what thinking-recall surfaced this turn — one call:`,
    `${RATE_TOOL}({"turnId":"${turnId}","steps":[{"index":N,"good":[ids],"noise":[ids],"pair":{"A":"better|same|worse"}}]})`,
    'good = worth reading at that step · noise = would have cost attention · unmarked = neutral · pair: optional, compare A vs B',
  ]
  const render = (n: number, level: (shownCount: number) => Level) => {
    const chosen = newestFirst.slice(0, n)
    const lvl = level(n)
    const lines = [...head, ...chosen.map(d => stepLine(d, letters, lvl))]
    const omitted = newestFirst.length - n
    if (omitted > 0) {
      lines.push(`(+${omitted} earlier step${omitted === 1 ? '' : 's'} omitted — rate from the files if you want them: ${dir})`)
    }
    const shown: RequestBuild['shown'] = {}
    for (const d of chosen) {
      shown[String(d.index)] = Object.fromEntries(
        letters.map(l => [l, (d.byLetter[l] ?? []).slice(0, lvl.limit).map(c => c.id)]),
      )
    }
    return { text: lines.join('\n'), shown, omittedSteps: omitted }
  }

  // Richest first: as many steps as fit, with summaries.
  const spec = (count: number): Level => ({ limit: count > 2 ? Math.min(topK, 3) : topK, summaries: true })
  for (let n = newestFirst.length; n >= 1; n--) {
    const built = render(n, spec)
    if (built.text.length <= maxChars) return built
  }
  // Even one step does not fit: ids only, then fewer ids per arm.
  for (const limit of [topK, 3, 2, 1]) {
    if (limit > topK) continue
    const built = render(1, () => ({ limit, summaries: false }))
    if (built.text.length <= maxChars) return built
  }
  // The header alone can exceed an absurdly small cap: truncate, never exceed.
  const last = render(1, () => ({ limit: 1, summaries: false }))
  return { ...last, text: last.text.slice(0, Math.max(0, maxChars)) }
}

/** turn.complete: append ONE rating request when the main loop answered and arms ran. */
export type CompleteNext = (e: TurnCompleteInput) => Promise<TurnCompleteResult>

export function turnCompleteHook(probe: Probe) {
  return async (io: Io, e: Readonly<TurnCompleteInput>, next: CompleteNext): Promise<TurnCompleteResult> => {
    const result = await next(e as TurnCompleteInput)
    if (e.agentId !== undefined || probe.root === null || probe.disabled !== null) return result
    const turn = probe.turns.get(e.turnId)
    if (turn === undefined) return result
    probe.turns.delete(e.turnId)
    if (e.reason !== 'answer') return result

    // Let the last step's arms finish (bounded) before reading the digests.
    const pendingAtComplete = turn.pending.length - turn.jobsDone
    const waitStart = await io.now()
    let timer: { cancel: () => void } | undefined
    const timeout = new Promise<void>(resolve => {
      timer = io.after(WAIT_PENDING_MS, resolve)
    })
    await Promise.race([Promise.allSettled(turn.pending), timeout])
    timer?.cancel()
    const waitedMs = Math.max(0, (await io.now()) - waitStart)
    const pendingTimedOut = turn.pending.length - turn.jobsDone > 0

    if (turn.digests.length === 0) return result
    const letters = Object.keys(turn.blind).sort()
    const built = buildRequest({
      turnId: e.turnId,
      digests: turn.digests,
      letters,
      topK: probe.cfg.topK,
      maxChars: probe.cfg.maxRatingChars,
      dir: probe.root,
    })
    // A failed append is logged ONCE per attempt (there is one attempt per turn,
    // never a retry) together with the row text, so the debug log shows what the
    // model did not get.
    let isAppended = false
    try {
      const appended = await io.append({
        message: { type: 'user', content: [{ type: 'text', text: built.text }] },
      })
      if (appended.deny !== undefined) {
        io.log(`engram-probe: rating request append refused: ${appended.deny}; row=${JSON.stringify(built.text)}`)
      } else {
        isAppended = true
      }
    } catch (err) {
      io.log(`engram-probe: rating request append rejected: ${String(err).slice(0, 160)}; row=${JSON.stringify(built.text)}`)
    }
    // Written whether or not the append landed (`appended` says which), so the
    // wait cost is recorded for every attempt and `shown` is never mistaken for
    // what the model actually received.
    await writeUnderRoot(
      io,
      probe,
      `requests/${e.turnId}.json`,
      JSON.stringify({
        turnId: e.turnId,
        shown: built.shown,
        chars: built.text.length,
        omittedSteps: built.omittedSteps,
        appended: isAppended,
        waitedMs,
        pendingAtComplete,
        ...(pendingTimedOut ? { pendingTimedOut: true } : {}),
      }),
    )
    return result
  }
}

// ------------------------------------------------------------------- the tool

export const RATE_DESCRIPTION =
  'Rate what the engram-probe recall arms surfaced, in ONE call, when a "[engram-probe · turn …]" request appears. ' +
  'For each step: good = node ids worth reading at that step; noise = ids that would have cost attention; ids you leave out are neutral. ' +
  'Optional pair: pair {"A": "better"} compares A against B (verdicts better, same, worse); only A or B. ' +
  'An id is two lowercase letters, an underscore and 4+ digits, as listed in the request. Answer once per turn; never call it otherwise.'

export const RATE_SCHEMA = {
  type: 'object',
  properties: {
    turnId: { type: 'string' },
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          index: { type: 'integer' },
          good: { type: 'array', items: { type: 'string' }, default: [] },
          noise: { type: 'array', items: { type: 'string' }, default: [] },
          pair: {
            type: 'object',
            properties: {
              A: { type: 'string', enum: ['better', 'same', 'worse'] },
              B: { type: 'string', enum: ['better', 'same', 'worse'] },
            },
            additionalProperties: false,
          },
        },
        required: ['index'],
      },
    },
  },
  required: ['turnId', 'steps'],
}

export type RatedStep = {
  index: number
  good: string[]
  noise: string[]
  pair?: Record<string, 'better' | 'same' | 'worse'>
}

export type RateParse = { ok: true; turnId: string; steps: RatedStep[] } | { ok: false; field: string; why: string }

function bad(field: string, why: string): RateParse {
  return { ok: false, field, why }
}

function idList(raw: unknown, field: string): string[] | RateParse {
  if (raw === undefined) return []
  if (!Array.isArray(raw)) return bad(field, 'must be an array of node ids')
  for (let j = 0; j < raw.length; j++) {
    const id = raw[j]
    if (typeof id !== 'string' || !NODE_ID.test(id)) {
      return bad(`${field}[${j}]`, `${JSON.stringify(id)} is not a node id (two letters, underscore, 4+ digits)`)
    }
  }
  return raw as string[]
}

/** The first bad field wins; nothing is written for a malformed call. */
export function parseRating(input: Record<string, unknown>): RateParse {
  const turnId = input.turnId
  if (typeof turnId !== 'string' || !SAFE_ID.test(turnId)) {
    return bad('turnId', 'is required (the turn id from the request)')
  }
  const steps = input.steps
  if (!Array.isArray(steps) || steps.length === 0) return bad('steps', 'must be a non-empty array')
  const out: RatedStep[] = []
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i] as Record<string, unknown> | null
    const at = `steps[${i}]`
    if (s === null || typeof s !== 'object') return bad(at, 'must be an object')
    if (typeof s.index !== 'number' || !Number.isInteger(s.index) || s.index < 0) {
      return bad(`${at}.index`, 'must be a non-negative integer')
    }
    const good = idList(s.good, `${at}.good`)
    if (!Array.isArray(good)) return good
    const noise = idList(s.noise, `${at}.noise`)
    if (!Array.isArray(noise)) return noise
    const step: RatedStep = { index: s.index, good, noise }
    if (s.pair !== undefined) {
      if (s.pair === null || typeof s.pair !== 'object' || Array.isArray(s.pair)) {
        return bad(`${at}.pair`, 'must be an object like {"A":"better"}')
      }
      const pair: NonNullable<RatedStep['pair']> = {}
      for (const [letter, verdict] of Object.entries(s.pair as Record<string, unknown>)) {
        if (letter !== 'A' && letter !== 'B') return bad(`${at}.pair.${letter}`, 'is not A or B')
        if (typeof verdict !== 'string' || !(VERDICTS as readonly string[]).includes(verdict)) {
          return bad(`${at}.pair.${letter}`, `${JSON.stringify(verdict)} is not one of better|same|worse`)
        }
        pair[letter] = verdict as 'better' | 'same' | 'worse'
      }
      step.pair = pair
    }
    out.push(step)
  }
  return { ok: true, turnId, steps: out }
}

/** tool.call hook for `mcp__engram-probe__rate`. */
export type RateInput = { tool: string; agentId?: string } & Record<string, unknown>

export function rateHook(probe: Probe) {
  return async (io: Io, e: Readonly<RateInput>, next: (e: RateInput) => Promise<unknown>): Promise<unknown> => {
    if (e.tool !== RATE_TOOL) return next(e as RateInput)
    const deny = (field: string, why: string) => ({ deny: `engram-probe rate: ${field} ${why}` })
    if (probe.root === null || probe.disabled !== null) return deny('probe', 'is inactive in this session')
    if (e.agentId !== undefined) return deny('caller', 'must be the main conversation')
    const parsed = parseRating(e)
    if (!parsed.ok) return deny(parsed.field, parsed.why)

    const ts = await io.now()
    const unknownSteps: number[] = []
    for (const s of parsed.steps) {
      const path = stepAbsPath(probe, parsed.turnId, s.index)
      let known = false
      try {
        known = path !== null && (await io.exists(path))
      } catch {
        known = false
      }
      if (!known) unknownSteps.push(s.index)
    }
    const file: Record<string, unknown> = {
      ts,
      sessionId: probe.sessionId,
      turnId: parsed.turnId,
      steps: parsed.steps,
      ratedBy: await io.model(),
    }
    if (unknownSteps.length > 0) file.unknownSteps = unknownSteps
    const wrote = await writeUnderRoot(io, probe, `ratings/${parsed.turnId}-${ts}.json`, JSON.stringify(file))
    if (!wrote) return deny('write', 'failed; nothing was recorded')
    let text = `rated ${parsed.steps.length} step(s) for turn ${parsed.turnId.slice(0, 8)}`
    if (unknownSteps.length > 0) text += ` (no step file for index ${unknownSteps.join(', ')}; recorded as unknownSteps)`
    // Result contract for a plugin-registered tool. claude-code.d.ts types `result`
    // as `ToolResultOf<Name>` = `unknown` for a non-builtin tool (ToolCallResult,
    // ~line 12139; ToolResultOf, ~line 12456), so the declarations do not say what
    // is accepted. The engine validates it at run time and, measured live on 2.1.289,
    // rejected an MCP-shaped object with: "tool.call step resolved
    // mcp__engram-probe__rate with a result that does not match its output shape:
    // invalid_union -- expected string | array | undefined, received object".
    // So `result` is a plain string (or an array of content blocks, or undefined).
    // The ratings file was written above, before this return.
    return { result: text }
  }
}
