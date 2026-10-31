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
import {
  ROUTES,
  attachRoutes,
  mergeRoutes,
  parseLedger,
  type RouteAttach,
  type RouteLine,
} from './route.ts'

/** Leads every rating request; step.ts skips rows containing it (inContext). */
export const REQUEST_MARKER = '[engram-probe · turn '
/**
 * The empty ENGRAM marker (#3613): U+27E6 / U+27E7, the real characters. The row has no
 * meaningful text for recall to search, so its LAST line is an empty marker and the recall
 * hooks skip it. Counted inside `maxChars`.
 */
export const EMPTY_ENGRAM_MARKER = '\u27e6ENGRAM\u27e7\u27e6/ENGRAM\u27e7'
export const RATE_TOOL = 'mcp__engram-probe__rate'
export const NODE_ID = /^[a-z]{2}_\d{4,}$/
const VERDICTS = ['better', 'same', 'worse'] as const

/**
 * The rater's wording for the exclusive `in_context` mark. NOT the automatic candidate flag
 * `inContext` (step.ts annotateCandidates, from a regex over the conversation): this is the
 * rater's own mark, written to the ratings file.
 */
export const IN_CONTEXT_WORDING = 'already in my context window when it was shown; would not have helped whatever its relevance'

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
      const also = c.also !== undefined && c.also.length > 0 ? ` ·also ${c.also.join('/')}` : ''
      return (s.length > 0 ? `${c.id} "${s}"` : c.id) + also
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
    `${RATE_TOOL}({"turnId":"${turnId}","steps":[{"index":N,"good":[ids],"noise":[ids],"in_context":[ids],"pair":{"A":"better|same|worse"}}]})`,
    `good = worth reading at that step · noise = would have cost attention · in_context = ${IN_CONTEXT_WORDING} · unmarked = neutral · pair: optional, compare A vs B`,
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
    return { text: [...lines, EMPTY_ENGRAM_MARKER].join('\n'), shown, omittedSteps: omitted }
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
  // Reserve the marker (plus its newline) so the cut row still ends with it. But the row's
  // head REQUEST_MARKER must always survive (step.ts skips the rating row by it), so the
  // trailing marker is only kept when maxChars - reserve >= REQUEST_MARKER.length (maxChars >= 39);
  // below that, fall back to the plain hard cut with no trailing marker: the cap still wins.
  const reserve = EMPTY_ENGRAM_MARKER.length + 1
  if (maxChars - reserve < REQUEST_MARKER.length) return { ...last, text: last.text.slice(0, Math.max(0, maxChars)) }
  const body = last.text.slice(0, last.text.length - reserve)
  return { ...last, text: `${body.slice(0, maxChars - reserve)}\n${EMPTY_ENGRAM_MARKER}` }
}

/** A ledger whose newest line is further than this from the turn's end is probably a different clock domain. */
const CLOCK_SKEW_MS = 6 * 3_600_000

/**
 * Seed the lower bound of the route-ledger window, once per loaded copy of the module.
 * `newProbe` starts at 0, so without this a mod reload or a resumed session would hand
 * the next turn every historical ledger line. Source of truth: `bound.json`
 * (`{"ts": <turn end ms>}`), written at EVERY main-loop turn.complete, attach-less turns
 * included. Only when it is absent (a session recorded before bound.json existed) fall back
 * to the max `ts` among `route-joins/*.json`; neither -> stays 0.
 */
async function loadBound(io: Io, probe: Probe): Promise<void> {
  if (probe.boundLoaded || probe.root === null) return
  probe.boundLoaded = true
  const tsOf = async (path: string): Promise<number | null> => {
    const raw = await io.read(path)
    const text = typeof raw === 'string' ? raw : typeof (raw as { text?: unknown } | null)?.text === 'string' ? (raw as { text: string }).text : ''
    const ts = (JSON.parse(text) as { ts?: unknown }).ts
    return typeof ts === 'number' && Number.isFinite(ts) ? ts : null
  }
  try {
    const ts = await tsOf(`${probe.root}/bound.json`)
    if (ts !== null) {
      if (ts > probe.lastCompleteMs) probe.lastCompleteMs = ts
      return
    }
  } catch {
    // absent or unreadable bound.json: fall back to the join files
  }
  let entries: { name: string; kind: string }[]
  try {
    entries = await io.list(`${probe.root}/route-joins`)
  } catch {
    return // no route-joins directory yet: a fresh session
  }
  for (const entry of entries) {
    if (!entry.name.endsWith('.json')) continue
    try {
      const ts = await tsOf(`${probe.root}/route-joins/${entry.name}`)
      if (ts !== null && ts > probe.lastCompleteMs) probe.lastCompleteMs = ts
    } catch {
      // an unreadable join file contributes no bound
    }
  }
}

/**
 * The route ledger lines (#3458) stamped inside this turn: after the last turn.complete and
 * not after now. Read through Io.read (`$.fs.read`); a missing file is an empty ledger.
 *
 * Clock domain: the ledger `ts` is Python `int(time.time()*1000)` (epoch ms) and `toMs` is
 * `$.clock.now()`. MEASURED 2026-10-06 13:47Z on the live seat: a live step file's `ts` =
 * 1791294457121 while epoch now = 1791294503984 (47 s later), so `$.clock.now()` IS epoch
 * ms and the two compare directly. The guard below still logs once if a ledger has lines
 * and none are in the window while its newest `ts` is > 6 h from now.
 */
async function readRouteLines(io: Io, probe: Probe, fromMs: number, toMs: number): Promise<RouteLine[]> {
  const out: RouteLine[] = []
  for (const route of ROUTES) {
    let raw: unknown
    try {
      raw = await io.read(`${probe.root}/routes/${route}.jsonl`)
    } catch {
      continue // no ledger for this route: the section-A hook never fired (or the probe was not live)
    }
    const text = typeof raw === 'string' ? raw : typeof (raw as { text?: unknown } | null)?.text === 'string' ? (raw as { text: string }).text : ''
    const parsed = parseLedger(text, route)
    let inWindow = 0
    for (const line of parsed) {
      if (line.ts > fromMs && line.ts <= toMs) {
        out.push(line)
        inWindow += 1
      }
    }
    if (parsed.length > 0 && inWindow === 0) {
      const newest = Math.max(...parsed.map(l => l.ts))
      if (Math.abs(toMs - newest) > CLOCK_SKEW_MS) {
        logOnce(io, probe, `route-clock:${route}`, `route ledger clock domain mismatch? newest ts ${newest} vs now ${toMs} (${route})`)
      }
    }
  }
  return out
}

function joinFileText(turnId: string, attaches: RouteAttach[], merged: ReturnType<typeof mergeRoutes>, ts: number): string {
  const steps: Record<string, unknown> = {}
  for (const a of attaches) {
    const key = String(a.index)
    const entry = (steps[key] ??= {
      routeOnlyIds: merged.routeOnlyIds[key] ?? [],
      alsoByArm: merged.alsoByArm[key] ?? {},
      routes: [] as unknown[],
    }) as { routes: unknown[] }
    entry.routes.push({
      route: a.route,
      letter: a.letter,
      fire_key: a.fire_key,
      ...(a.unmatched ? { unmatched: true } : {}),
      line: a.line,
    })
  }
  return JSON.stringify({ turnId, ts, steps })
}

/** turn.complete: append ONE rating request when the main loop answered and arms ran. */
export type CompleteNext = (e: TurnCompleteInput) => Promise<TurnCompleteResult>

export function turnCompleteHook(probe: Probe) {
  return async (io: Io, e: Readonly<TurnCompleteInput>, next: CompleteNext): Promise<TurnCompleteResult> => {
    const result = await next(e as TurnCompleteInput)
    if (e.agentId !== undefined || probe.root === null || probe.disabled !== null) return result
    // the route ledger window: (previous turn's end, this turn's end]. Every main-loop
    // turn.complete advances the bound, including a turn the probe saw no step of (it errored
    // before its first step): its ledger lines must not attach to the NEXT turn.
    await loadBound(io, probe)
    const turnFromMs = probe.lastCompleteMs
    const turnToMs = await io.now()
    probe.lastCompleteMs = turnToMs
    // persisted so a reload or resume keeps the bound even when no join file is ever written
    await writeUnderRoot(io, probe, 'bound.json', JSON.stringify({ ts: turnToMs }))
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

    const armLetters = Object.keys(turn.blind).sort()
    let attaches: RouteAttach[] = []
    try {
      const lines = await readRouteLines(io, probe, turnFromMs, turnToMs)
      const indexes = [...turn.digests.map(d => d.index), ...(turn.digests.length === 0 ? turn.toolKeys.map(t => t.index) : [])]
      attaches = attachRoutes(lines, turn.toolKeys, indexes.length > 0 ? Math.min(...indexes) : 0)
    } catch (err) {
      logOnce(io, probe, 'route-read', `route ledger unreadable: ${String(err).slice(0, 160)}`)
    }
    const merged = mergeRoutes(turn.digests, attaches, armLetters)
    if (merged.digests.length === 0) return result
    const routeLetters = [...new Set(attaches.map(a => a.letter))].sort()
    const letters = [...armLetters, ...routeLetters]
    const built = buildRequest({
      turnId: e.turnId,
      digests: merged.digests,
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
    // The join the converter reads (a sibling of requests/, not a rewrite of step files
    // that are already on disk and that the rate tool checks for existence).
    if (attaches.length > 0) {
      await writeUnderRoot(io, probe, `route-joins/${e.turnId}.json`, joinFileText(e.turnId, attaches, merged, turnToMs))
    }
    return result
  }
}

// ------------------------------------------------------------------- the tool

export const RATE_DESCRIPTION =
  'Rate what the engram-probe recall arms surfaced, in ONE call, when a "[engram-probe · turn …]" request appears. ' +
  'For each step: good = node ids worth reading at that step; noise = ids that would have cost attention; in_context = ids ' + IN_CONTEXT_WORDING + '; an id takes at most one of the three marks; ids you leave out are neutral. ' +
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
          in_context: { type: 'array', items: { type: 'string' }, default: [] },
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
  in_context: string[]
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
    const inContext = idList(s.in_context, `${at}.in_context`)
    if (!Array.isArray(inContext)) return inContext
    // EXCLUSIVE: one mark per id. The error names the field path of the SECOND occurrence (later list wins).
    const marked: [string, string[]][] = [['good', good], ['noise', noise], ['in_context', inContext]]
    for (let k = 1; k < marked.length; k++) {
      const [name, ids] = marked[k]!
      for (let j = 0; j < ids.length; j++) {
        const earlier = marked.slice(0, k).find(([, list]) => list.includes(ids[j]!))
        if (earlier !== undefined) {
          return bad(`${at}.${name}[${j}]`, `${JSON.stringify(ids[j])} is also in ${earlier[0]} (an id takes one mark: good, noise or in_context)`)
        }
      }
    }
    const step: RatedStep = { index: s.index, good, noise, in_context: inContext }
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
