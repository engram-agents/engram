import type { PluginOptions } from 'claude-code'

export const ALL_ARMS = [
  'thinking_full',
  'thinking_tail',
  'extract_model',
  'tool_input',
  'novel_terms',
] as const
export type ArmName = (typeof ALL_ARMS)[number]

export type Config = {
  enabled: boolean
  topK: number
  minThinkingChars: number
  arms: ArmName[]
  extractModel: string
  extractTimeoutMs: number
  maxRatingChars: number
  /** As given (may start with `~`); expanded against HOME by record.ts. */
  probeDir: string
}

export const DEFAULTS: Config = {
  enabled: true,
  topK: 5,
  minThinkingChars: 200,
  arms: [...ALL_ARMS],
  extractModel: 'haiku',
  extractTimeoutMs: 6000,
  maxRatingChars: 1200,
  probeDir: '~/.engram/probe/mods-recall',
}

type Log = (line: string) => void

function pickNumber(
  options: PluginOptions,
  key: 'topK' | 'minThinkingChars' | 'extractTimeoutMs' | 'maxRatingChars',
  log: Log,
  lo = 0,
  hi = Infinity,
): number {
  const raw = options[key]
  if (raw === undefined) return DEFAULTS[key]
  if (typeof raw === 'number' && Number.isFinite(raw) && raw >= lo && raw <= hi) return raw
  log(`engram-probe: option ${key}=${JSON.stringify(raw)} is invalid (need a finite number in [${lo}, ${hi}]); using default ${DEFAULTS[key]} for ${key} only`)
  return DEFAULTS[key]
}

function pickString(options: PluginOptions, key: 'extractModel' | 'probeDir', log: Log): string {
  const raw = options[key]
  if (raw === undefined) return DEFAULTS[key]
  if (typeof raw === 'string' && raw.trim().length > 0) return raw.trim()
  log(`engram-probe: option ${key}=${JSON.stringify(raw)} is invalid (need a non-empty string); using default ${JSON.stringify(DEFAULTS[key])} for ${key} only`)
  return DEFAULTS[key]
}

function pickArms(options: PluginOptions, log: Log): ArmName[] {
  const raw = options.arms
  if (raw === undefined) return [...DEFAULTS.arms]
  if (typeof raw !== 'string') {
    log(`engram-probe: option arms=${JSON.stringify(raw)} is invalid (need a comma list); using the default arms for arms only`)
    return [...DEFAULTS.arms]
  }
  const wanted = raw.split(',').map(s => s.trim()).filter(s => s.length > 0)
  const known = wanted.filter((s): s is ArmName => (ALL_ARMS as readonly string[]).includes(s))
  const unknown = wanted.filter(s => !(ALL_ARMS as readonly string[]).includes(s))
  if (unknown.length > 0) {
    log(`engram-probe: option arms names unknown arm(s) ${JSON.stringify(unknown)}; ignoring them (known: ${ALL_ARMS.join(', ')})`)
  }
  const unique = [...new Set(known)]
  if (unique.length === 0) {
    log('engram-probe: option arms names no known arm; using the default arms for arms only')
    return [...DEFAULTS.arms]
  }
  return unique
}

/**
 * Options -> typed config. Every invalid value is logged once (naming the key
 * and the fallback) and replaced by that key's default ONLY.
 */
export function parseConfig(options: PluginOptions, log: Log): Config {
  let enabled = DEFAULTS.enabled
  if (options.enabled !== undefined) {
    if (typeof options.enabled === 'boolean') enabled = options.enabled
    else log(`engram-probe: option enabled=${JSON.stringify(options.enabled)} is invalid (need true/false); using default ${DEFAULTS.enabled} for enabled only`)
  }
  return {
    enabled,
    topK: Math.floor(pickNumber(options, 'topK', log, 1, 20)),
    minThinkingChars: pickNumber(options, 'minThinkingChars', log),
    arms: pickArms(options, log),
    extractModel: pickString(options, 'extractModel', log),
    extractTimeoutMs: pickNumber(options, 'extractTimeoutMs', log),
    maxRatingChars: pickNumber(options, 'maxRatingChars', log),
    probeDir: pickString(options, 'probeDir', log),
  }
}
