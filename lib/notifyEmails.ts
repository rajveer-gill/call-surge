/**
 * Where booking requests get emailed, edited as rows but stored as one string.
 *
 * The business record keeps `notification_email` as a single comma-separated value, which
 * the backend splits when a request comes in. Settings edits it as a list with add/remove
 * buttons, so these two functions are the boundary between those shapes. Keeping the
 * stored format unchanged means an address a shop already saved keeps working.
 */

/** One stored string becomes editable rows. Always at least one, so the field is never empty. */
export function splitEmails(raw: string | undefined | null): string[] {
  const parts = (raw || '')
    .split(/[,;]/)
    .map((e) => e.trim())
    .filter(Boolean)
  return parts.length ? parts : ['']
}

/**
 * Rows become the stored string. Returns '' when nothing usable is left, which is how a
 * shop turns these emails off — the caller sends `undefined` rather than an empty string.
 * Blank rows are dropped (an untouched "Add email" row is not an address) and duplicates
 * collapse, so nobody gets the same request twice for pasting into two boxes.
 */
export function joinEmails(rows: string[]): string {
  const seen = new Set<string>()
  const out: string[] = []
  for (const row of rows) {
    const value = (row || '').trim()
    if (!value) continue
    const key = value.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push(value)
  }
  return out.join(', ')
}
