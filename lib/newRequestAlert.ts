/**
 * Deciding when a new booking request has arrived, so the dashboard can make a sound.
 *
 * Lana Anderberg, 2026-09-03, on the Gig Harbor pilot:
 *
 *     "Is there any way for a notification sound to happen when a request goes to
 *      the dashboard?"
 *
 * The front desk is cutting hair, not watching a screen. Email covers the case where
 * nobody is at the computer; this covers the case where somebody is, and the tab has
 * been open since morning.
 *
 * The logic is here rather than in the component because getting it wrong is loud and
 * public: a chime on first page load would fire for every request already sitting in
 * the list, and a chime on every poll would fire every thirty seconds forever. Both
 * end with the salon muting it on day one.
 */

import { needsResponse } from '@/components/appointments/appointmentStatus'

export const CHIME_STORAGE_KEY = 'nuvatra.requestChime'

/** The appointment shape this module needs — kept minimal so callers can pass anything. */
type StatusRow = { id: number; status: string }

/**
 * Requests currently waiting on the shop. Anything already accepted, declined or
 * cancelled is not waiting for anyone and must never make a noise.
 */
export function awaitingRequestIds(appointments: readonly StatusRow[]): Set<number> {
  const out = new Set<number>()
  for (const a of appointments || []) {
    if (typeof a?.id === 'number' && needsResponse(a?.status || '')) out.add(a.id)
  }
  return out
}

/**
 * Which ids are new since the last poll.
 *
 * `seen === null` means we have not polled yet. That case returns nothing on purpose:
 * the first fetch establishes the baseline, so opening the dashboard is silent no
 * matter how many requests are already waiting. Only something that arrives while
 * somebody is watching is worth a sound.
 */
export function newlyArrived(
  seen: ReadonlySet<number> | null,
  current: ReadonlySet<number>,
): number[] {
  if (seen === null) return []
  const out: number[] = []
  // forEach rather than for..of: this project's tsconfig targets ES5, where iterating a
  // Set needs downlevelIteration.
  current.forEach((id) => {
    if (!seen.has(id)) out.push(id)
  })
  return out.sort((a, b) => a - b)
}

/** Sound on unless this browser has turned it off. Opt-out, not opt-in — see above. */
export function chimeEnabled(): boolean {
  try {
    return window.localStorage.getItem(CHIME_STORAGE_KEY) !== '0'
  } catch {
    // Private browsing, blocked storage. Defaulting to on matches the no-storage case.
    return true
  }
}

export function setChimeEnabled(on: boolean): void {
  try {
    window.localStorage.setItem(CHIME_STORAGE_KEY, on ? '1' : '0')
  } catch {
    /* ignore — the toggle still works for this page's lifetime */
  }
}
