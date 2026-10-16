// The route join (#3458 section B): the two live recall routes' ledger lines, joined
// onto the turn's steps so the agent can rate their ids beside the arms'.
//
// Contract with section A (the hooks writing the ledger): one JSON line per route
// fire at `<probeDir>/<sessionId>/routes/<route>.jsonl`, fields `ts` (ms epoch),
// `route`, `session_id`, `fire_key`, `query`, `candidates[]`, `missing_signals`,
// `shown` (ids in render order), `cut`, `source`.
//
// Pure functions only: no `$`, no Io (rating.ts reads through Io.read).
import type { StepDigest } from './record.ts'

export const ROUTES = ['prompt_surface', 'tool_recall'] as const
export type RouteName = (typeof ROUTES)[number]
export const ROUTE_LETTER: Record<RouteName, string> = { prompt_surface: 'R', tool_recall: 'T' }

/** Newest lines kept per ledger file (a session ledger is small; this bounds what a runaway one can cost). */
export const MAX_LEDGER_LINES = 500

export type RouteLine = {
  ts: number
  route: RouteName
  fire_key: string
  shown: string[]
  [k: string]: unknown
}

/** One ledger line attached to one step. */
export type RouteAttach = {
  index: number
  route: RouteName
  letter: string
  fire_key: string
  unmatched?: true
  line: RouteLine
}

// -------------------------------------------------------------------- sha256

const K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]

function utf8(text: string): number[] {
  const out: number[] = []
  for (const ch of text) {
    const c = ch.codePointAt(0) as number
    if (c < 0x80) out.push(c)
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63))
    else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
    else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
  }
  return out
}

/**
 * sha256 in plain synchronous JS. Not crypto.subtle: its digest completes on a real
 * macrotask the test clock cannot settle (#3378, see arms.ts), and the step hook must
 * not wait on one.
 */
export function sha256Hex(text: string): string {
  const bytes = utf8(text)
  const bitLen = bytes.length * 8
  bytes.push(0x80)
  while (bytes.length % 64 !== 56) bytes.push(0)
  const hi = Math.floor(bitLen / 0x100000000)
  const lo = bitLen >>> 0
  for (const w of [hi, lo]) bytes.push((w >>> 24) & 255, (w >>> 16) & 255, (w >>> 8) & 255, w & 255)
  const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
  const w = new Array<number>(64)
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n))
  for (let off = 0; off < bytes.length; off += 64) {
    for (let i = 0; i < 16; i++) {
      const j = off + i * 4
      w[i] = (((bytes[j] as number) << 24) | ((bytes[j + 1] as number) << 16) | ((bytes[j + 2] as number) << 8) | (bytes[j + 3] as number)) >>> 0
    }
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15] as number
      const b = w[i - 2] as number
      const s0 = rotr(a, 7) ^ rotr(a, 18) ^ (a >>> 3)
      const s1 = rotr(b, 17) ^ rotr(b, 19) ^ (b >>> 10)
      w[i] = ((w[i - 16] as number) + s0 + (w[i - 7] as number) + s1) >>> 0
    }
    let [a, b, c, d, e, f, g, hh] = h as [number, number, number, number, number, number, number, number]
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
      const ch = (e & f) ^ (~e & g)
      const t1 = (hh + S1 + ch + (K[i] as number) + (w[i] as number)) >>> 0
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
      const maj = (a & b) ^ (a & c) ^ (b & c)
      const t2 = (S0 + maj) >>> 0
      hh = g
      g = f
      f = e
      e = (d + t1) >>> 0
      d = c
      c = b
      b = a
      a = (t1 + t2) >>> 0
    }
    h[0] = ((h[0] as number) + a) >>> 0
    h[1] = ((h[1] as number) + b) >>> 0
    h[2] = ((h[2] as number) + c) >>> 0
    h[3] = ((h[3] as number) + d) >>> 0
    h[4] = ((h[4] as number) + e) >>> 0
    h[5] = ((h[5] as number) + f) >>> 0
    h[6] = ((h[6] as number) + g) >>> 0
    h[7] = ((h[7] as number) + hh) >>> 0
  }
  return h.map(x => x.toString(16).padStart(8, '0')).join('')
}

// ------------------------------------------------------------------ fire_key

type Ser = { sort: boolean; spaced: boolean; ascii: boolean }

function ser(value: unknown, o: Ser): string {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'string') {
      const s = JSON.stringify(value)
      return o.ascii ? s.replace(/[\u0080-￿]/g, ch => '\\u' + ch.charCodeAt(0).toString(16).padStart(4, '0')) : s
    }
    return JSON.stringify(value) ?? 'null'
  }
  if (Array.isArray(value)) return '[' + value.map(v => ser(v, o)).join(o.spaced ? ', ' : ',') + ']'
  let keys = Object.keys(value as object)
  if (o.sort) keys = keys.sort()
  const items = keys.map(k => ser(k, o) + (o.spaced ? ': ' : ':') + ser((value as Record<string, unknown>)[k], o))
  return '{' + items.join(o.spaced ? ', ' : ',') + '}'
}

/**
 * The fire_key a step's tool call may carry in the ledger: `tool_name:` + the first 16
 * hex of sha256 over the tool_input JSON. Section A names the hash and the 16 hex but
 * not the serialization, so every plausible one is returned (key order as emitted or
 * sorted, compact or python-default separators, raw or \uXXXX-escaped non-ASCII); a
 * ledger line matches the call when its fire_key is any of them.
 */
export function fireKeyVariants(toolName: string, argText: string): string[] {
  let parsed: unknown
  try {
    parsed = argText.trim() === '' ? {} : JSON.parse(argText)
  } catch {
    return [`${toolName}:${sha256Hex(argText).slice(0, 16)}`] // unparseable: only the raw text can be hashed
  }
  const out = new Set<string>()
  for (const sort of [false, true]) {
    for (const spaced of [false, true]) {
      for (const ascii of [false, true]) {
        out.add(`${toolName}:${sha256Hex(ser(parsed, { sort, spaced, ascii })).slice(0, 16)}`)
      }
    }
  }
  return [...out]
}

// -------------------------------------------------------------------- ledger

/** Tolerant: a malformed line, or one lacking the fields the join needs, is skipped. */
export function parseLedger(text: string, route: RouteName): RouteLine[] {
  const out: RouteLine[] = []
  const lines = text.split('\n').filter(l => l.trim().length > 0)
  for (const raw of lines.slice(-MAX_LEDGER_LINES)) {
    try {
      const o = JSON.parse(raw) as Record<string, unknown>
      if (o === null || typeof o !== 'object') continue
      if (typeof o.ts !== 'number' || typeof o.fire_key !== 'string') continue
      if (!Array.isArray(o.shown) || !o.shown.every(i => typeof i === 'string')) continue
      out.push({ ...o, route, ts: o.ts, fire_key: o.fire_key, shown: o.shown as string[] })
    } catch {
      // a half-written last line is expected while the hook is mid-append
    }
  }
  return out
}

export type ToolKeys = { index: number; keys: string[] }

/**
 * Where each line goes. prompt_surface -> `firstIndex` (the turn's first rated step).
 * tool_recall -> the step with a tool call whose fire_key variants include the line's;
 * none -> step 0, `unmatched: true`.
 */
export function attachRoutes(lines: RouteLine[], toolKeys: ToolKeys[], firstIndex: number): RouteAttach[] {
  const out: RouteAttach[] = []
  for (const line of [...lines].sort((a, b) => a.ts - b.ts)) {
    const letter = ROUTE_LETTER[line.route]
    if (line.route === 'prompt_surface') {
      out.push({ index: firstIndex, route: line.route, letter, fire_key: line.fire_key, line })
      continue
    }
    const hit = toolKeys.find(t => t.keys.includes(line.fire_key))
    if (hit !== undefined) out.push({ index: hit.index, route: line.route, letter, fire_key: line.fire_key, line })
    else out.push({ index: 0, route: line.route, letter, fire_key: line.fire_key, unmatched: true, line })
  }
  return out
}

export type MergeResult = {
  digests: StepDigest[]
  /** index -> ids only a route showed on that step */
  routeOnlyIds: Record<string, string[]>
  /** index -> id -> arm letters that also showed it (the id is listed once, in the route cell) */
  alsoByArm: Record<string, Record<string, string[]>>
}

/**
 * Fold the attached route lines into copies of the digests. A route cell lists the
 * line's shown ids; an id an arm also showed on that step is listed ONCE, in the route
 * cell, marked `also <arm letters>`, and removed from those arm cells.
 */
export function mergeRoutes(digests: StepDigest[], attaches: RouteAttach[], armLetters: string[]): MergeResult {
  const byIndex = new Map<number, StepDigest>()
  for (const d of digests) byIndex.set(d.index, { index: d.index, byLetter: { ...d.byLetter } })
  const routeOnlyIds: Record<string, string[]> = {}
  const alsoByArm: Record<string, Record<string, string[]>> = {}
  for (const a of attaches) {
    let d = byIndex.get(a.index)
    if (d === undefined) {
      d = { index: a.index, byLetter: {} }
      byIndex.set(a.index, d)
    }
    const cell = d.byLetter[a.letter] ?? []
    const seen = new Set(cell.map(c => c.id))
    const key = String(a.index)
    for (const id of a.line.shown) {
      if (seen.has(id)) continue
      seen.add(id)
      const also = armLetters.filter(l => (d.byLetter[l] ?? []).some(c => c.id === id)).sort()
      cell.push(also.length > 0 ? { id, summary: '', also } : { id, summary: '' })
      if (also.length > 0) {
        ;(alsoByArm[key] ??= {})[id] = also
      } else {
        const only = (routeOnlyIds[key] ??= [])
        if (!only.includes(id)) only.push(id)
      }
    }
    d.byLetter[a.letter] = cell
  }
  // an id claimed by a route cell leaves the arm cells it was listed in
  for (const [key, claimed] of Object.entries(alsoByArm)) {
    const d = byIndex.get(Number(key)) as StepDigest
    for (const l of armLetters) {
      if (d.byLetter[l] !== undefined) d.byLetter[l] = d.byLetter[l]!.filter(c => claimed[c.id] === undefined)
    }
  }
  return { digests: [...byIndex.values()].sort((x, y) => x.index - y.index), routeOnlyIds, alsoByArm }
}
