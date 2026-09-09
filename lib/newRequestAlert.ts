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
export const CHIME_VOLUME_KEY = 'nuvatra.requestChimeVolume'

/**
 * Lana Anderberg, go-live day: "The sound the site makes when a request comes in is too
 * quiet. Salons are loud places, music is playing, people are talking, blowdryers are
 * running. Could we have customization over the sound setting and volume?"
 *
 * The old fixed value was 0.18, chosen at a desk at midnight. Loud is now the default and
 * the range goes well above it, because the failure the salon actually experiences is not
 * hearing a customer's request at all.
 */
export const CHIME_VOLUME_STEPS = [0.15, 0.35, 0.6, 1.0] as const
export const CHIME_VOLUME_LABELS = ['Quiet', 'Medium', 'Loud', 'Loudest'] as const
export const CHIME_VOLUME_DEFAULT_INDEX = 2 // Loud — a salon, not an office

export function chimeVolumeIndex(): number {
  try {
    const raw = window.localStorage.getItem(CHIME_VOLUME_KEY)
    // Number('') is 0, which is a valid index — so an empty stored value would quietly
    // mean "Quiet", the exact complaint this setting exists to fix. Treat blank as unset.
    const i = raw === null || !raw.trim() ? NaN : Number(raw)
    return Number.isInteger(i) && i >= 0 && i < CHIME_VOLUME_STEPS.length
      ? i
      : CHIME_VOLUME_DEFAULT_INDEX
  } catch {
    return CHIME_VOLUME_DEFAULT_INDEX
  }
}

export function setChimeVolumeIndex(i: number): void {
  const clamped = Math.min(Math.max(Math.round(i), 0), CHIME_VOLUME_STEPS.length - 1)
  try {
    window.localStorage.setItem(CHIME_VOLUME_KEY, String(clamped))
  } catch {
    /* ignore — the choice still applies for this page's lifetime */
  }
}

/** Gain for the current setting. Never returns 0: silence is what the toggle is for. */
export function chimeVolume(): number {
  return CHIME_VOLUME_STEPS[chimeVolumeIndex()]
}

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
