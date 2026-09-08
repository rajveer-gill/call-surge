import { describe, it, expect } from 'vitest'
import { localDayString, shiftDay } from '@/lib/localDay'

// The stylist view's "Today" and the calendar's fetch range are built from these.
// They used to go through toISOString(), which converts to UTC first — so a Pacific
// salon looking at the dashboard after 5pm got tomorrow's day. Every assertion below
// is timezone-independent: the Dates are built from local parts, so their local
// calendar day is fixed by construction no matter where the test runs.
describe('localDayString', () => {
  it('reads the local calendar day, not the UTC one', () => {
    // 11:30pm local — the case that broke. In any zone west of UTC the old
    // toISOString().slice(0, 10) on this instant returns the 14th.
    expect(localDayString(new Date(2026, 8, 13, 23, 30))).toBe('2026-09-13')
  })

  it('holds at the other end of the day', () => {
    // 12:30am local — where east-of-UTC zones used to slip a day backwards.
    expect(localDayString(new Date(2026, 8, 13, 0, 30))).toBe('2026-09-13')
  })

  it('zero-pads month and day', () => {
    expect(localDayString(new Date(2026, 0, 5, 12, 0))).toBe('2026-01-05')
  })

  it('defaults to now, and agrees with the runtime local date', () => {
    const now = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    expect(localDayString()).toBe(
      `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
    )
  })
})

describe('shiftDay', () => {
  it('steps forward and back by whole days', () => {
    expect(shiftDay('2026-09-13', 1)).toBe('2026-09-14')
    expect(shiftDay('2026-09-13', -1)).toBe('2026-09-12')
    expect(shiftDay('2026-09-13', 0)).toBe('2026-09-13')
  })

  it('crosses month and year boundaries', () => {
    expect(shiftDay('2026-09-30', 1)).toBe('2026-10-01')
    expect(shiftDay('2026-01-01', -1)).toBe('2025-12-31')
  })

  it('crosses a DST transition without losing a day', () => {
    // US DST ends Nov 1 2026; the 1st is 25 hours long in Pacific.
    expect(shiftDay('2026-10-31', 1)).toBe('2026-11-01')
    expect(shiftDay('2026-11-01', 1)).toBe('2026-11-02')
  })
})
