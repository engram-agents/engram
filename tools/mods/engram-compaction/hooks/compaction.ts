import type { SessionCompactInput, SessionCompactResult, SessionMessage, TurnCompleteInput, TurnCompleteResult } from 'claude-code'

/**
 * How the context message joins the compaction (ONE named constant; flip it here).
 *   'beside'  : [our context message] + the engine's own messages (its summary and the kept ones)
 *   'replace' : [our context message] alone; the engine's summarizer is not asked
 */
export type Shape = 'beside' | 'replace'
export const COMPACT_SHAPE: Shape = 'beside'

/** A request older than this is stale: tombstoned, never acted on. */
export const REQUEST_MAX_AGE_MS = 30 * 60_000
/** The in-flight token answers only a compaction this fresh. */
export const TOKEN_MAX_AGE_MS = 60_000
/** Larger than the Python half's cap, so a context it wrote always fits; anything bigger is not ours. */
export const MAX_CONTEXT_CHARS = 200_000

/** Under 'beside' the engine summarizes; this narrows it to what the context cannot carry. */
export const COMPACT_INSTRUCTIONS =
  'Summarize only the in-flight technical state: open pull requests and their heads, files mid-edit, ' +
  'replies still owed, background tasks and monitors. Everything else was persisted before this compaction.'

const LOG_PREFIX = 'engram-compaction: '

/** Everything the handlers touch, so a test can stand in for the engine. Built from `$` in register.ts. */
export type Io = {
  now: () => Promise<number>
  read: (path: string) => Promise<string>
  write: (path: string, text: string) => Promise<void>
  exists: (path: string) => Promise<boolean>
  sessionId: () => Promise<string>
  compact: (args: { instructions?: string }) => Promise<SessionCompactResult>
  log: (text: string) => void
  /** the ENGRAM data directory, or undefined when it cannot be placed */
  home: () => Promise<string | undefined>
}

type Dir = { request: string; inflight: string; context: string; digest: string }

async function paths(io: Io): Promise<Dir | undefined> {
  const home = await io.home()
  if (home === undefined || home === '') return undefined
  const dir = `${home.replace(/\/+$/, '')}/compact`
  return { request: `${dir}/request.json`, inflight: `${dir}/inflight`, context: `${dir}/context.md`, digest: `${dir}/context.sha256` }
}

async function sha256(text: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('')
}

/** A file's JSON object, or undefined when missing, unreadable or not an object. */
async function readObject(io: Io, path: string): Promise<Record<string, unknown> | undefined> {
  try {
    const parsed: unknown = JSON.parse(await io.read(path))
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

/** A verdict is ONE short line a seat can read from the file: capped in `tombstone`, whatever the caller built it from. */
export const VERDICT_MAX_CHARS = 200

/** Any value as text without ever throwing (a skip need not be a string; an error's toString can throw). */
function render(v: unknown): string {
  if (typeof v === 'string') return v
  try {
    const j = JSON.stringify(v)
    if (typeof j === 'string') return j
  } catch {
    // fall through to String()
  }
  try {
    return String(v)
  } catch {
    return '[unrenderable]'
  }
}

/** An error as `String(err)` (an Error stringifies to `Error: msg`, where JSON would give `{}`), never throwing. */
function errorText(err: unknown): string {
  try {
    return String(err)
  } catch {
    return render(err)
  }
}

/**
 * One line: C0 and C1 control characters (newline, NEL and CSI included) and DEL become spaces, runs of whitespace
 * collapse, capped. The input is cut to a few times the cap BEFORE the regexes run, so a huge skip text costs
 * nothing (the cap is still applied after the collapse, which can only shorten it).
 */
function oneLine(text: string): string {
  return text
    .slice(0, VERDICT_MAX_CHARS * 4)
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, VERDICT_MAX_CHARS)
    .trimEnd()
}

/**
 * The mods API has no file delete: a consumed file is overwritten with a tombstone, and a tombstone reads as absent.
 * A tombstone written WITH a verdict also says why the request ended; `consumed: true` is still the only field a guard reads.
 * With no verdict the text is exactly what it always was.
 */
async function tombstone(io: Io, path: string, id: unknown, verdict?: string): Promise<boolean> {
  try {
    const body: Record<string, unknown> = { consumed: true, request_id: typeof id === 'string' ? id : null }
    if (verdict !== undefined) body.verdict = oneLine(verdict)
    await io.write(path, JSON.stringify(body))
    return true
  } catch {
    return false
  }
}

/**
 * turn.complete: after an ANSWERED turn, consume a live request for this session ONCE
 * (tombstone first, so a failure can never retry), leave an in-flight token, ask for the
 * compaction. Never throws; the turn's own result is returned untouched.
 */
export function completeHook(io: Io) {
  return async (e: TurnCompleteInput, next: (e: TurnCompleteInput) => Promise<TurnCompleteResult>): Promise<TurnCompleteResult> => {
    const base = await next(e)
    try {
      await consume(io, e)
    } catch (err) {
      io.log(`${LOG_PREFIX}consume failed; nothing compacted: ${String(err).slice(0, 160)}`)
    }
    return base
  }
}

async function consume(io: Io, e: TurnCompleteInput): Promise<void> {
  if (e.reason !== 'answer' || e.isAborted) return
  if (e.agentId !== undefined) return // a subagent's turn is not the main loop's
  const p = await paths(io)
  if (p === undefined) return
  if (!(await io.exists(p.request))) return
  const req = await readObject(io, p.request)
  if (req === undefined || req.consumed === true) return
  const sid = await io.sessionId()
  if (req.session_id !== sid) return

  const id = req.context_sha256
  const at = typeof req.requested_at === 'string' ? Date.parse(req.requested_at) : NaN
  const now = await io.now()
  if (typeof id !== 'string' || id === '' || Number.isNaN(at) || now - at > REQUEST_MAX_AGE_MS) {
    await tombstone(io, p.request, id, 'dropped: stale or malformed request')
    io.log(`${LOG_PREFIX}request is stale or malformed; dropped, nothing compacted`)
    return
  }

  // Consume first: if the tombstone cannot be written, do not compact (a retry loop is worse).
  if (!(await tombstone(io, p.request, id))) {
    io.log(`${LOG_PREFIX}could not mark the request consumed; nothing compacted`)
    return
  }
  try {
    await io.write(p.inflight, JSON.stringify({ request_id: id, at: now }))
  } catch {
    io.log(`${LOG_PREFIX}could not write the in-flight token; nothing compacted`)
    await tombstone(io, p.request, id, 'not attempted: could not write the in-flight token') // best effort: already consumed
    return
  }
  let verdict: string | undefined
  try {
    const r = await io.compact({ instructions: COMPACT_INSTRUCTIONS })
    if (r.skip !== undefined) {
      verdict = `vetoed: ${render(r.skip)}`
      io.log(`${LOG_PREFIX}compaction ${verdict}`)
    } else {
      verdict = 'compacted'
    }
  } catch (err) {
    verdict = `refused: ${errorText(err).slice(0, 160)}`
    io.log(`${LOG_PREFIX}compaction ${verdict}`)
  } finally {
    await tombstone(io, p.inflight, id, verdict)
  }
}

/**
 * session.compact: answer ONLY a plugin-triggered compaction that carries a fresh,
 * matching in-flight token and a context whose digest agrees with the token and the
 * digest file. Every other case is `next(e)`: the engine's own summary applies and
 * nothing is lost. Never throws.
 */
export function compactHook(io: Io, shape: Shape) {
  return async (e: SessionCompactInput, next: (e: SessionCompactInput) => Promise<SessionCompactResult>): Promise<SessionCompactResult> => {
    if (e.trigger !== 'plugin') return next(e)
    let context: string | undefined
    try {
      context = await guarded(io)
    } catch {
      context = undefined
    }
    if (context === undefined) return next(e)
    const ours: SessionMessage = { role: 'user', text: context, toolUses: [] }
    if (shape === 'replace') return { messages: [ours] }
    const engine = await next(e)
    if (engine.skip !== undefined) return engine
    return { ...engine, messages: [ours, ...engine.messages] }
  }
}

/** The context text when every guard passes; the token is spent on every path that reads it. */
async function guarded(io: Io): Promise<string | undefined> {
  const p = await paths(io)
  if (p === undefined) return undefined
  const token = await readObject(io, p.inflight)
  if (token === undefined || token.consumed === true) return undefined
  const spend = () => tombstone(io, p.inflight, token.request_id)
  const at = typeof token.at === 'number' ? token.at : NaN
  const id = token.request_id
  if (typeof id !== 'string' || Number.isNaN(at) || (await io.now()) - at > TOKEN_MAX_AGE_MS) {
    await spend()
    return undefined
  }
  await spend()
  const [text, digest] = await Promise.all([io.read(p.context), io.read(p.digest)])
  if (text === '' || text.length > MAX_CONTEXT_CHARS) return undefined
  if (digest.trim() !== id || (await sha256(text)) !== id) return undefined
  return text
}
