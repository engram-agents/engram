import type { ArmName, Config } from './config.ts'
import { recall, type Candidate } from './recall.ts'
import { logOnce, type Io, type Probe } from './record.ts'

export type ArmResult = {
  arm: ArmName
  query?: string
  candidates?: Candidate[]
  ms: number
  error?: string
  /** not an error: the arm had nothing to ask (no tool call, no novel terms) */
  skipped?: string
  clipped?: boolean
  /** how the engram_query text was framed: unwrapped result string, or direct */
  envelope?: 'result-string' | 'direct'
  extract?: {
    ms: number
    usage?: unknown
    cached: boolean
    isAnswered: boolean
    reason?: string
  }
  /** arm E: the terms the helper found and which of them were novel */
  terms?: string[]
  novel?: string[]
  /** arm E: the graph file the helper measured against */
  db?: string
}

export type ArmCtx = {
  io: Io
  probe: Probe
  cfg: Config
  server: string
  /** the chosen query material (thinking, or the visible answer + tool args) */
  material: string
  /**
   * What arm C may send to the SIDE MODEL: thinking (thinking mode) or the step's
   * answer text (visible mode), NEVER tool-call arguments. Arms A/B/D/E query only
   * the local ENGRAM server and may use `material`.
   */
  extractInput: string
  /** every tool block's arguments JSON text, one entry per tool call */
  toolArgs: string[]
  sessionId: string
}

export const FULL_CLIP = 6000
export const TAIL_FALLBACK_CHARS = 400
export const EXTRACT_TAIL_CHARS = 3000
export const TOOL_INPUT_CLIP = 2000

export const EXTRACT_SYSTEM =
  "You read an AI agent's reasoning or working text. Name, as 3 short noun phrases on one line separated by ' | ', the facts, decisions or prior experiences this reasoning assumes or would benefit from remembering. No prose."

/** Last 3 sentences (split on `[.!?]` + whitespace), else the last 400 chars. */
export function tailQuery(material: string): string {
  const text = material.trim()
  const parts = text.split(/([.!?])\s/)
  const sentences: string[] = []
  for (let i = 0; i < parts.length; i += 2) {
    const body = parts[i] ?? ''
    const mark = parts[i + 1] ?? ''
    const s = (body + mark).trim()
    if (s.length > 0) sentences.push(s)
  }
  if (sentences.length >= 3) return sentences.slice(-3).join(' ')
  return text.slice(-TAIL_FALLBACK_CHARS)
}

async function hexSha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('')
}

async function timed<T>(io: Io, fn: () => Promise<T>): Promise<{ value?: T; err?: unknown; ms: number }> {
  const t0 = await io.now()
  let value: T | undefined
  let err: unknown
  try {
    value = await fn()
  } catch (e) {
    err = e
  }
  const t1 = await io.now()
  return { value, err, ms: Math.max(0, t1 - t0) }
}

/** Run the retrieval for a query and fold timing + failure into an ArmResult. */
async function retrieveArm(
  ctx: ArmCtx,
  arm: ArmName,
  query: string,
  extra: Partial<ArmResult> = {},
): Promise<ArmResult> {
  const { value, err, ms } = await timed(ctx.io, () => recall(ctx.io, ctx.server, query, ctx.cfg.topK))
  if (err !== undefined) {
    return {
      arm,
      query,
      ms,
      error: String((err as Error)?.message ?? err),
      envelope: (err as { envelope?: 'result-string' | 'direct' })?.envelope,
      ...extra,
    }
  }
  return { arm, query, candidates: value?.candidates ?? [], envelope: value?.envelope, ms, ...extra }
}

async function armThinkingFull(ctx: ArmCtx): Promise<ArmResult> {
  const clipped = ctx.material.length > FULL_CLIP
  const query = clipped ? ctx.material.slice(-FULL_CLIP) : ctx.material
  return retrieveArm(ctx, 'thinking_full', query, { clipped })
}

async function armThinkingTail(ctx: ArmCtx): Promise<ArmResult> {
  return retrieveArm(ctx, 'thinking_tail', tailQuery(ctx.material))
}

async function armExtractModel(ctx: ArmCtx): Promise<ArmResult> {
  const { io, probe, cfg } = ctx
  const t0 = await io.now()
  const prompt = ctx.extractInput.slice(-EXTRACT_TAIL_CHARS)
  const key = await hexSha256(prompt)
  let reply = probe.extractCache.get(key)
  const cached = reply !== undefined
  const extract: NonNullable<ArmResult['extract']> = { ms: 0, cached, isAnswered: cached }
  if (reply === undefined) {
    const timeoutMs = Math.max(1, Math.floor(cfg.extractTimeoutMs))
    let res
    try {
      res = await io.complete({
        model: cfg.extractModel,
        effort: 'low',
        maxTokens: 120,
        timeoutMs,
        system: EXTRACT_SYSTEM,
        prompt,
      })
    } catch (err) {
      const t1 = await io.now()
      extract.ms = Math.max(0, t1 - t0)
      extract.reason = 'rejected'
      return { arm: 'extract_model', ms: extract.ms, error: `model.complete rejected: ${String(err).slice(0, 160)}`, extract }
    }
    const t1 = await io.now()
    extract.ms = Math.max(0, t1 - t0)
    extract.usage = res.usage
    extract.isAnswered = res.isAnswered
    if (!res.isAnswered) {
      extract.reason = res.reason
      return { arm: 'extract_model', ms: extract.ms, error: `extract not answered: ${res.reason}`, extract }
    }
    reply = res.text
    probe.extractCache.set(key, reply)
  }
  const query = reply
    .split('|')
    .map(s => s.trim())
    .filter(s => s.length > 0)
    .join(' ')
  if (query.length === 0) {
    return { arm: 'extract_model', ms: extract.ms, skipped: 'no-phrases', extract }
  }
  const armResult = await retrieveArm(ctx, 'extract_model', query, { extract })
  // `ms` for arm C is the whole arm: the extraction plus the retrieval.
  armResult.ms += extract.ms
  return armResult
}

async function armToolInput(ctx: ArmCtx): Promise<ArmResult> {
  if (ctx.toolArgs.length === 0) return { arm: 'tool_input', ms: 0, skipped: 'no-tool-call' }
  const query = ctx.toolArgs.join('\n').slice(0, TOOL_INPUT_CLIP)
  if (query.trim().length === 0) return { arm: 'tool_input', ms: 0, skipped: 'no-tool-input' }
  return retrieveArm(ctx, 'tool_input', query)
}

async function armNovelTerms(ctx: ArmCtx): Promise<ArmResult> {
  const { io, probe } = ctx
  const statePath = `${probe.root}/novel-terms-seen.json`
  const t0 = await io.now()
  let out: { terms: string[]; novel: string[]; db?: string }
  try {
    const res = await io.run(
      ['python3', `${io.pluginRoot}/novel_terms.py`, '--state', statePath],
      { stdin: ctx.material, timeoutMs: 8000 },
    )
    if (res.exitCode !== 0) {
      const t1 = await io.now()
      return { arm: 'novel_terms', ms: Math.max(0, t1 - t0), error: `novel_terms.py exit ${res.exitCode}: ${res.stderr.slice(0, 200)}` }
    }
    const parsed = JSON.parse(res.stdout) as { terms?: unknown; novel?: unknown; db?: unknown }
    if (!Array.isArray(parsed.terms) || !Array.isArray(parsed.novel)) {
      throw new Error('stdout lacks terms/novel arrays')
    }
    out = { terms: parsed.terms.map(String), novel: parsed.novel.map(String), db: typeof parsed.db === 'string' ? parsed.db : undefined }
  } catch (err) {
    const t1 = await io.now()
    // A host with no process noun (non-CLI) fails the call itself with a TypeError.
    const error = err instanceof TypeError ? 'no-process' : `novel_terms failed: ${String(err).slice(0, 200)}`
    return { arm: 'novel_terms', ms: Math.max(0, t1 - t0), error }
  }
  if (out.novel.length === 0) {
    const t1 = await io.now()
    return { arm: 'novel_terms', ms: Math.max(0, t1 - t0), skipped: 'no-novel-terms', terms: out.terms, novel: out.novel, db: out.db }
  }
  const armResult = await retrieveArm(ctx, 'novel_terms', out.novel.join(' '), { terms: out.terms, novel: out.novel, db: out.db })
  const t1 = await io.now()
  armResult.ms = Math.max(0, t1 - t0)
  return armResult
}

const RUNNERS: Record<ArmName, (ctx: ArmCtx) => Promise<ArmResult>> = {
  thinking_full: armThinkingFull,
  thinking_tail: armThinkingTail,
  extract_model: armExtractModel,
  tool_input: armToolInput,
  novel_terms: armNovelTerms,
}

/**
 * Run the enabled arms concurrently (allSettled); an arm that throws outright
 * becomes that arm's `error`, never a missing row and never the others' failure.
 */
export async function runArms(ctx: ArmCtx): Promise<Record<string, ArmResult>> {
  const arms = ctx.cfg.arms
  const settled = await Promise.allSettled(arms.map(name => RUNNERS[name](ctx)))
  const out: Record<string, ArmResult> = {}
  settled.forEach((s, i) => {
    const name = arms[i] as ArmName
    if (s.status === 'fulfilled') {
      out[name] = s.value
    } else {
      logOnce(ctx.io, ctx.probe, 'arm-threw:' + name, `arm ${name} threw: ${String(s.reason).slice(0, 160)}`)
      out[name] = { arm: name, ms: 0, error: `arm threw: ${String(s.reason).slice(0, 160)}` }
    }
  })
  return out
}
