/** Expand Number's shortest round-trip representation without scientific notation or rounding. */
export function decimalSeconds(value: number): string {
  if (!Number.isFinite(value)) return ''
  const text=String(value)
  if (!/[eE]/.test(text)) return text
  const [coefficient,exponentText]=text.toLowerCase().split('e')
  const sign=coefficient.startsWith('-')?'-':''
  const unsigned=sign?coefficient.slice(1):coefficient
  const [whole,fraction='']=unsigned.split('.')
  const digits=whole+fraction,point=whole.length+Number(exponentText)
  return sign+(point<=0?'0.'+'0'.repeat(-point)+digits:point>=digits.length?digits+'0'.repeat(point-digits.length):digits.slice(0,point)+'.'+digits.slice(point))
}

export function formatElapsedSeconds(seconds: number, duration=false): string {
  if (!Number.isFinite(seconds) || seconds<=0) return ''
  const whole=Math.floor(seconds),fraction=decimalSeconds(seconds).split('.')[1]
  const tail=String(whole%60).padStart(2,'0')+(fraction?'.'+fraction:'')
  return duration?`${Math.floor(whole/3600)}:${String(Math.floor(whole/60)%60).padStart(2,'0')}:${tail}`:`${Math.floor(whole/60)}:${tail}`
}

/** User-entered current ability and aspiration stay distinct. All values are metric. */
export function parsePace(input: string): number | null {
  const match = /^(\d{1,2}):([0-5]\d(?:\.\d+)?)$/.exec(input.trim())
  if (!match) return null
  const seconds = Number(match[1]) * 60 + Number(match[2])
  return seconds > 0 ? seconds : null
}

export function parseGoalTime(input: string): number | null {
  const value = input.trim()
  const minutes = /^(\d{1,3}):([0-5]\d(?:\.\d+)?)$/.exec(value)
  const hours = /^(\d{1,2}):([0-5]\d):([0-5]\d(?:\.\d+)?)$/.exec(value)
  const seconds = hours ? Number(hours[1]) * 3600 + Number(hours[2]) * 60 + Number(hours[3])
    : minutes ? Number(minutes[1]) * 60 + Number(minutes[2]) : 0
  return seconds > 0 ? seconds : null
}

export function formatPrescriptionValue(field: string, value: number): string {
  if (!Number.isFinite(value)) return 'Unknown'
  if (field === 'pace_seconds_per_km') {
    const rounded = Math.round(value)
    return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')} /km`
  }
  if (field === 'distance_meters') return `${Number((value / 1000).toFixed(2))} km`
  if (field === 'duration_seconds') return `${Number((value / 60).toFixed(1))} min`
  return String(value)
}

const GOAL_KEYS = new Set(['type', 'target', 'unit', 'by_week', 'description', 'detail', 'note', 'race_date', 'target_seconds'])

/**
 * Goals/guardrails are LLM-authored with an open schema — usually strings or
 * {type, target?, unit?, by_week?, detail?|description?} objects. Compose a
 * readable line from the known keys ("Weekly volume: 20 km by week 4") and
 * never let an object hit String() ("[object Object]").
 */
export function formatGoal(g: unknown): string {
  if (typeof g === 'string') return g
  if (typeof g !== 'object' || g == null) return String(g)
  const o = g as Record<string, unknown>

  const bits: string[] = []
  if (typeof o.race_date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(o.race_date)) {
    const [year, month, day] = o.race_date.split('-').map(Number)
    bits.push(`Event ${new Date(year, month - 1, day).toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric' })}`)
  }
  if (typeof o.target_seconds === 'number' && Number.isFinite(o.target_seconds) && o.target_seconds > 0) {
    const seconds = Math.round(o.target_seconds)
    bits.push(`Target ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} finish`)
  }
  const target =
    o.target !== undefined ? String(o.target) + (typeof o.unit === 'string' ? ` ${o.unit}` : '') : null
  const numeric = [target, o.by_week !== undefined ? `by week ${String(o.by_week)}` : null]
    .filter(Boolean)
    .join(' ')
  if (numeric) bits.push(numeric)
  for (const key of ['description', 'detail', 'note']) {
    if (typeof o[key] === 'string') bits.push(o[key] as string)
  }
  // Unknown keys still show up rather than getting silently dropped.
  for (const [key, value] of Object.entries(o)) {
    if (!GOAL_KEYS.has(key)) bits.push(`${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`)
  }

  const type = typeof o.type === 'string' ? o.type.replace(/_/g, ' ') : null
  const title = type ? type.charAt(0).toUpperCase() + type.slice(1) : null
  if (title && bits.length > 0) return `${title}: ${bits.join(' · ')}`
  if (title) return title
  if (bits.length > 0) return bits.join(' · ')
  return JSON.stringify(g)
}
