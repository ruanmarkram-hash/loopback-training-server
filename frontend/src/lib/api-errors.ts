/** Extract explanations only. Never serialize submitted input or exception context. */
export function describeApiDetail(detail: unknown, fallback: string): string {
 const text = (value: unknown) => typeof value === 'string' ? value.slice(0,500) : ''
 const findings = (value: unknown): string[] => Array.isArray(value) ? value.slice(0,5).flatMap(entry => {
  if (!entry || typeof entry !== 'object') return []
  const item = entry as Record<string,unknown>, message = text(item.msg) || text(item.explanation) || text(item.message)
  if (!message) return []
  const field = Array.isArray(item.loc) ? item.loc.filter(value=>typeof value==='string' && !['body','query','path'].includes(value)).slice(0,4).join(' · ').replaceAll('_',' ') : ''
  return [field ? `${field}: ${message}` : message]
 }) : []
 if (typeof detail === 'string') return text(detail)
 if (Array.isArray(detail)) return findings(detail).join(' ') || fallback
 if (!detail || typeof detail !== 'object') return fallback
 const item = detail as Record<string,unknown>, parts: string[] = []
 if (typeof item.reason === 'string') parts.push(text(item.reason))
 if (item.status === 'onboarding_required') parts.push('Current ability facts are required before a program can be prepared.')
 if (Array.isArray(item.required_fields)) {
  const fields=item.required_fields.filter(value=>typeof value==='string').slice(0,12).map(value=>value.replaceAll('_',' '))
  if (fields.length) parts.push(`Required: ${fields.join(', ')}.`)
 }
 parts.push(...findings(item.validation))
 return parts.join(' ') || fallback
}
