import type { EngineInterface } from 'claude-code'
import type { ArmName, Config } from './config.ts'

export type Eng = EngineInterface

/**
 * The slice of `$` the probe uses, as plain functions.
 *
 * `claude plugin validate` follows `$` only into functions declared in the
 * SAME file as the hook that holds it, never across an import, so `$` never
 * leaves register.ts: each hook builds one of these (every member spells
 * `$.noun.event(...)` at its call site) and hands THAT to the imported modules.
 */
export type Io = {
  now: () => Promise<number>
  after: Eng['clock']['after']
  write: (path: string, text: string) => Promise<void>
  exists: (path: string) => Promise<boolean>
  /** the debug channel only; never the transcript */
  log: (text: string) => void
  /** the transcript line shown to the person (config problems) */
  notice: (text: string) => void
  mcpCall: Eng['mcp']['call']
  complete: Eng['model']['complete']
  /** rejects (or throws a TypeError) where the host has no process noun (non-CLI) */
  run: Eng['process']['run']
  messages: () => Promise<unknown>
  toolNames: () => Promise<string[]>
  registerTool: Eng['tool']['register']
  append: Eng['session']['append']
  sessionId: () => Promise<string>
  model: () => Promise<string>
  engineVersion: () => Promise<string>
  homeDir: () => Promise<string | undefined>
  pluginRoot: string
}

/** What the rating request needs to remember about one step that ran arms. */
export type StepDigest = {
  index: number
  /** letter -> candidates in rank order (empty for a skipped/errored arm) */
  byLetter: Record<string, { id: string; summary: string }[]>
}

export type TurnState = {
  turnId: string
  /** letter -> arm name; drawn once per turn */
  blind: Record<string, ArmName>
  digests: StepDigest[]
  /** one promise per scheduled step job; settled when its file is written */
  pending: Promise<void>[]
  /** steps whose job has been handed to the clock (arms run or skipped row) */
  stepsSeen: number
  /** step jobs that have finished (settled or failed) */
  jobsDone: number
}

/** Everything one loaded copy of the module shares; recreated on hot reload. */
export type Probe = {
  cfg: Config
  /** `probeDir/<sessionId>` once resolved; null = the probe is inactive */
  root: string | null
  sessionId: string | null
  /** discovered `<server>` of `mcp__<server>__engram_query`; null = none found */
  server: string | null
  /** why the probe is inactive, if it is (also written to session notes) */
  disabled: string | null
  turns: Map<string, TurnState>
  /** arm C replies keyed by the prompt text (<= 3000 chars each), for the module's lifetime */
  extractCache: Map<string, string>
  logged: Set<string>
}

export function newProbe(cfg: Config): Probe {
  return {
    cfg,
    root: null,
    sessionId: null,
    server: null,
    disabled: null,
    turns: new Map(),
    extractCache: new Map(),
    logged: new Set(),
  }
}

/** Log a line once per key (the debug channel; never the transcript). */
export function logOnce(io: Io, probe: Probe, key: string, line: string): void {
  if (probe.logged.has(key)) return
  probe.logged.add(key)
  try {
    io.log(`engram-probe: ${line}`)
  } catch {
    // logging must never fail the hook
  }
}

/** A file-name-safe id: the same alphabet the engine's ids and ours use. */
export const SAFE_ID = /^[A-Za-z0-9_-]{1,100}$/

export function pad3(n: number): string {
  return String(Math.max(0, Math.floor(n))).padStart(3, '0')
}

/**
 * `~` and `~/x` -> HOME-relative; null when HOME is needed but unset (the
 * caller then disables the probe: it never writes to a guessed path).
 */
export async function resolveProbeDir(io: Io, probeDir: string): Promise<string | null> {
  if (probeDir !== '~' && !probeDir.startsWith('~/')) return probeDir
  const home = await io.homeDir()
  if (home === undefined || home === '') return null
  return home.replace(/\/+$/, '') + probeDir.slice(1)
}

/** Every write goes through here: relative, no `..`, under `probe.root`. */
export async function writeUnderRoot(
  io: Io,
  probe: Probe,
  relative: string,
  text: string,
): Promise<boolean> {
  if (probe.root === null) return false
  if (relative.startsWith('/') || relative.split('/').includes('..')) {
    logOnce(io, probe, 'bad-path:' + relative, `refused to write outside the session dir: ${relative}`)
    return false
  }
  try {
    await io.write(`${probe.root}/${relative}`, text)
    return true
  } catch (err) {
    logOnce(io, probe, 'write-failed', `write failed (${relative}): ${String(err).slice(0, 160)}`)
    return false
  }
}

export function stepRelPath(turnId: string, index: number): string {
  return `steps/${turnId}-${pad3(index)}.json`
}

export function stepAbsPath(probe: Probe, turnId: string, index: number): string | null {
  return probe.root === null ? null : `${probe.root}/${stepRelPath(turnId, index)}`
}

export function turnState(probe: Probe, turnId: string, arms: readonly ArmName[]): TurnState {
  let turn = probe.turns.get(turnId)
  if (turn === undefined) {
    turn = { turnId, blind: drawBlind(arms), digests: [], pending: [], stepsSeen: 0, jobsDone: 0 }
    probe.turns.set(turnId, turn)
  }
  return turn
}

const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

/** One random permutation of the arms onto A, B, C, ... (Fisher-Yates over crypto bytes). */
export function drawBlind(arms: readonly ArmName[]): Record<string, ArmName> {
  const order = [...arms]
  for (let i = order.length - 1; i > 0; i--) {
    const buf = new Uint32Array(1)
    crypto.getRandomValues(buf)
    const j = (buf[0] ?? 0) % (i + 1)
    const tmp = order[i] as ArmName
    order[i] = order[j] as ArmName
    order[j] = tmp
  }
  const blind: Record<string, ArmName> = {}
  order.forEach((arm, k) => {
    blind[LETTERS[k] as string] = arm
  })
  return blind
}
