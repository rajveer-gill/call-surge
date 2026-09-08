/** A calendar day as YYYY-MM-DD, read in the viewer's own timezone.
 *
 * Deliberately not `toISOString().slice(0, 10)`: that converts to UTC first, so the
 * answer is wrong for most of the day outside UTC. A salon in Gig Harbor at 5pm PDT
 * is already tomorrow in UTC, so a "today" built that way opens on the wrong day
 * every evening; east of UTC the same expression reads as yesterday each morning.
 */
export function localDayString(date: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** Step a YYYY-MM-DD by whole days without going through a timezone. */
export function shiftDay(day: string, delta: number): string {
  const [y, m, d] = day.split('-').map((n) => parseInt(n, 10))
  const dt = new Date(y, (m || 1) - 1, d || 1)
  dt.setDate(dt.getDate() + delta)
  return localDayString(dt)
}
