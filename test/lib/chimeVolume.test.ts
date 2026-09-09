import { describe, it, expect, beforeEach } from 'vitest'
import {
  CHIME_VOLUME_DEFAULT_INDEX,
  CHIME_VOLUME_KEY,
  CHIME_VOLUME_LABELS,
  CHIME_VOLUME_STEPS,
  chimeVolume,
  chimeVolumeIndex,
  setChimeVolumeIndex,
} from '@/lib/newRequestAlert'

// Lana Anderberg, go-live day: "The sound the site makes when a request comes in is too
// quiet. Salons are loud places, music is playing, people are talking, blowdryers are
// running. Could we have customization over the sound setting and volume?"
//
// The old value was a single hardcoded 0.18 picked at a desk. What matters here is that
// the default is now loud enough for a salon floor, and that a bad stored value can never
// leave the shop silent — silence is what the bell toggle is for.

describe('chime volume', () => {
  beforeEach(() => window.localStorage.clear())

  it('defaults to Loud, because the room has blowdryers in it', () => {
    expect(chimeVolumeIndex()).toBe(CHIME_VOLUME_DEFAULT_INDEX)
    expect(CHIME_VOLUME_LABELS[chimeVolumeIndex()]).toBe('Loud')
  })

  it('is louder than the fixed value it replaces', () => {
    expect(chimeVolume()).toBeGreaterThan(0.18)
  })

  it('round trips a choice', () => {
    setChimeVolumeIndex(0)
    expect(chimeVolumeIndex()).toBe(0)
    expect(chimeVolume()).toBe(CHIME_VOLUME_STEPS[0])
    setChimeVolumeIndex(3)
    expect(chimeVolume()).toBe(CHIME_VOLUME_STEPS[3])
  })

  it('clamps out-of-range values rather than storing them', () => {
    setChimeVolumeIndex(99)
    expect(chimeVolumeIndex()).toBe(CHIME_VOLUME_STEPS.length - 1)
    setChimeVolumeIndex(-5)
    expect(chimeVolumeIndex()).toBe(0)
  })

  it('falls back to the default on junk, rather than going silent', () => {
    for (const junk of ['', 'loud', '2.5', 'null', '-1', '999']) {
      window.localStorage.setItem(CHIME_VOLUME_KEY, junk)
      expect(chimeVolumeIndex()).toBe(CHIME_VOLUME_DEFAULT_INDEX)
    }
  })

  it('never returns a gain of zero — muting is the bell toggle, not the volume', () => {
    for (let i = 0; i < CHIME_VOLUME_STEPS.length; i++) {
      setChimeVolumeIndex(i)
      expect(chimeVolume()).toBeGreaterThan(0)
    }
  })

  it('survives blocked storage', () => {
    const original = window.localStorage.getItem
    window.localStorage.getItem = () => {
      throw new Error('blocked')
    }
    try {
      expect(chimeVolumeIndex()).toBe(CHIME_VOLUME_DEFAULT_INDEX)
    } finally {
      window.localStorage.getItem = original
    }
  })

  it('has a label for every step', () => {
    expect(CHIME_VOLUME_LABELS).toHaveLength(CHIME_VOLUME_STEPS.length)
  })
})
