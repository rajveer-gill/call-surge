import { describe, it, expect, beforeEach } from 'vitest'
import {
  awaitingRequestIds,
  newlyArrived,
  chimeEnabled,
  setChimeEnabled,
  CHIME_STORAGE_KEY,
} from '@/lib/newRequestAlert'

// Lana asked for a sound when a request reaches the dashboard. The two ways to get this
// wrong are both loud: chiming for rows that were already there when the page opened, and
// chiming again for the same row on every 30-second poll. Both of those end with a salon
// that has muted the feature, so they are what these tests are about.

const row = (id: number, status: string) => ({ id, status })

describe('awaitingRequestIds', () => {
  it('counts the statuses that are waiting on the shop', () => {
    const ids = awaitingRequestIds([
      row(1, 'pending'),
      row(2, 'pending_review'),
      row(3, 'pending_customer'),
    ])
    expect(Array.from(ids).sort()).toEqual([1, 2, 3])
  })

  it('ignores anything already dealt with — those are not news', () => {
    const ids = awaitingRequestIds([
      row(1, 'confirmed'),
      row(2, 'accepted'),
      row(3, 'completed'),
      row(4, 'cancelled'),
      row(5, 'rejected'),
    ])
    expect(ids.size).toBe(0)
  })

  it('survives a row with a status we have never seen', () => {
    expect(awaitingRequestIds([row(1, 'wat'), row(2, 'pending')])).toEqual(new Set([2]))
  })

  it('tolerates an empty or junk list rather than throwing on the dashboard', () => {
    expect(awaitingRequestIds([]).size).toBe(0)
    // A partially-loaded row from an in-flight response must not crash the page.
    expect(awaitingRequestIds([{ id: undefined, status: 'pending' } as never]).size).toBe(0)
  })
})

describe('newlyArrived', () => {
  it('says nothing on the first poll, however many requests are already waiting', () => {
    // This is the whole point: opening the dashboard to twelve pending requests must be
    // silent. Only something that lands while a person is watching earns a sound.
    expect(newlyArrived(null, new Set([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]))).toEqual([])
  })

  it('reports a request that appeared since the last poll', () => {
    expect(newlyArrived(new Set([1, 2]), new Set([1, 2, 3]))).toEqual([3])
  })

  it('does not report the same request twice', () => {
    const first = newlyArrived(new Set([1]), new Set([1, 2]))
    expect(first).toEqual([2])
    // The caller stores `current` as the new baseline, so the next identical poll is quiet.
    expect(newlyArrived(new Set([1, 2]), new Set([1, 2]))).toEqual([])
  })

  it('stays quiet when a request is accepted and leaves the list', () => {
    expect(newlyArrived(new Set([1, 2]), new Set([1]))).toEqual([])
  })

  it('reports several at once when two land inside one poll interval', () => {
    expect(newlyArrived(new Set([1]), new Set([1, 5, 3]))).toEqual([3, 5])
  })

  it('does not re-chime for a request that was accepted and then re-opened', () => {
    // Accept 2 (it leaves), then it comes back as pending_review after a customer text.
    // It is genuinely new to the waiting list, so it should chime — the shop has to act
    // on it again.
    expect(newlyArrived(new Set([1]), new Set([1, 2]))).toEqual([2])
  })
})

describe('chime preference', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('is on by default, because a salon that never finds the switch should still hear it', () => {
    expect(chimeEnabled()).toBe(true)
  })

  it('round trips off and back on', () => {
    setChimeEnabled(false)
    expect(window.localStorage.getItem(CHIME_STORAGE_KEY)).toBe('0')
    expect(chimeEnabled()).toBe(false)
    setChimeEnabled(true)
    expect(chimeEnabled()).toBe(true)
  })

  it('treats an unreadable store as sound-on rather than silently disabling it', () => {
    const original = window.localStorage.getItem
    window.localStorage.getItem = () => {
      throw new Error('blocked in private browsing')
    }
    try {
      expect(chimeEnabled()).toBe(true)
    } finally {
      window.localStorage.getItem = original
    }
  })
})
