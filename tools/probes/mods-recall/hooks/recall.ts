import type { Io } from './record.ts'

export type Candidate = {
  id: string
  type?: string
  claim?: string
  recall_summary?: string
  confidence?: number
  composite_score?: number
  similarity?: number
  match_type?: string
  utility_score?: number
  importance_score?: number
  recall_turn?: number
  created_at?: string
  rank: number
}

export const CLAIM_CLIP = 160

function clip(text: string, n: number): string {
  return text.length <= n ? text : text.slice(0, n)
}

function pick(row: Record<string, unknown>, key: string): unknown {
  return row[key] === null ? undefined : row[key]
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined
}

/**
 * The ONE retrieval function every arm shares (so the comparison is about the
 * QUERY, never the backend): engram_query with return_debug, top_k.
 *
 * Throws (the caller turns that into the arm's `error`) on an MCP error, an
 * `error` key, a non-JSON body, or a body with no `results` list.
 */
export type Envelope = 'result-string' | 'direct'

/**
 * Wraps recallBody so a failure still says which envelope had been seen by then
 * (`envelope` on the thrown Error; undefined when it failed before any JSON parsed).
 */
export async function recall(
  io: Io,
  server: string,
  query: string,
  topK: number,
): Promise<{ candidates: Candidate[]; envelope: Envelope }> {
  const seen: { envelope?: Envelope } = {}
  try {
    return await recallBody(io, server, query, topK, seen)
  } catch (err) {
    throw Object.assign(err instanceof Error ? err : new Error(String(err)), { envelope: seen.envelope })
  }
}

async function recallBody(
  io: Io,
  server: string,
  query: string,
  topK: number,
  seen: { envelope?: Envelope },
): Promise<{ candidates: Candidate[]; envelope: Envelope }> {
  const res = await io.mcpCall(server, 'engram_query', {
    // refresh_recall:false is unconditional: without it the probe's own queries
    // re-stamp the nodes they surface and perturb what it measures. An older
    // server rejects the unknown field; that error is recorded by the arm and is
    // deliberately NOT retried without the flag (a silent fallback would
    // contaminate the measurement).
    payload_json: JSON.stringify({ query, top_k: topK, return_debug: true, refresh_recall: false }),
  })
  const first = res.content.find(
    (b): b is { type: 'text'; text: string } =>
      (b as { type?: string }).type === 'text' && typeof (b as { text?: unknown }).text === 'string',
  )
  if (res.isError === true) {
    throw new Error(`engram_query isError: ${clip(first?.text ?? '(no text)', 200)}`)
  }
  if (first === undefined) throw new Error('engram_query returned no text block')
  let body: unknown
  try {
    body = JSON.parse(first.text)
  } catch {
    throw new Error(`engram_query returned non-JSON: ${clip(first.text, 120)}`)
  }
  if (body === null || typeof body !== 'object') throw new Error('engram_query body is not an object')
  let obj = body as Record<string, unknown>
  // The MCP text is not always the server JSON: the engram server's result text is
  // an object wrapping it as a STRING, {"result": "<json>"} (measured live, for
  // errors and successes alike). Unwrap exactly one level, then apply the checks.
  let envelope: Envelope = 'direct'
  seen.envelope = 'direct'
  if (typeof obj.result === 'string') {
    seen.envelope = 'result-string'
    let inner: unknown
    try {
      inner = JSON.parse(obj.result)
    } catch {
      throw new Error(`engram_query result string is not JSON: ${clip(obj.result, 120)}`)
    }
    if (inner === null || typeof inner !== 'object') throw new Error('engram_query result string is not an object')
    obj = inner as Record<string, unknown>
    envelope = 'result-string'
  }
  if ('error' in obj) throw new Error(`engram_query error: ${clip(String(obj.error), 200)}`)
  if (!Array.isArray(obj.results)) throw new Error('engram_query body has no results list')

  const out: Candidate[] = []
  for (const item of obj.results as unknown[]) {
    if (item === null || typeof item !== 'object') continue
    const row = item as Record<string, unknown>
    const id = str(row.id)
    if (id === undefined) continue
    const claim = str(row.claim)
    out.push({
      id,
      type: str(row.type),
      claim: claim === undefined ? undefined : clip(claim, CLAIM_CLIP),
      recall_summary: str(pick(row, 'recall_summary')),
      confidence: num(row.confidence),
      composite_score: num(row.composite_score),
      similarity: num(row.similarity),
      match_type: str(row.match_type),
      utility_score: num(row.utility_score),
      importance_score: num(row.importance_score),
      recall_turn: num(row.recall_turn),
      created_at: str(row.created_at),
      rank: out.length + 1,
    })
  }
  return { candidates: out, envelope }
}

/** `mcp__<server>__engram_query` -> `<server>`; undefined when no tool matches. */
export function discoverServer(toolNames: readonly string[]): string | undefined {
  for (const name of toolNames) {
    const m = /^mcp__(.+)__engram_query$/.exec(name)
    if (m !== null && m[1] !== undefined) return m[1]
  }
  return undefined
}
