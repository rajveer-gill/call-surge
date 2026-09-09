'use client'

/**
 * Plays the request chime once, on demand, at a given volume.
 *
 * Lana Anderberg asked for volume control because "salons are loud places, music is
 * playing, people are talking, blowdryers are running". A number on a screen cannot tell
 * her whether a setting is loud enough for her own room — only hearing it can, standing
 * where she'll be standing. So changing the volume plays it.
 *
 * Deliberately its own short-lived AudioContext rather than borrowing the dashboard's:
 * this runs from a click, which is exactly when a browser will allow audio to start, and
 * it must not disturb the long-lived context the live alert depends on.
 */

type AudioContextCtor = typeof AudioContext

export function playChimePreview(peak: number): void {
  if (typeof window === 'undefined') return
  const Ctor: AudioContextCtor | null =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext ||
    null
  if (!Ctor) return
  let ctx: AudioContext
  try {
    ctx = new Ctor()
  } catch {
    return
  }
  const note = (freq: number, startAt: number, seconds: number) => {
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    gain.gain.setValueAtTime(0.0001, startAt)
    gain.gain.exponentialRampToValueAtTime(peak, startAt + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + seconds)
    osc.connect(gain).connect(ctx.destination)
    osc.start(startAt)
    osc.stop(startAt + seconds + 0.02)
  }
  try {
    void ctx.resume?.()
    const now = ctx.currentTime
    note(880, now, 0.18)
    note(1174.66, now + 0.16, 0.28)
    // Free the audio device once the notes have finished rather than leaking a context
    // per click. Comfortably after the last note ends.
    window.setTimeout(() => void ctx.close?.().catch(() => {}), 1200)
  } catch {
    void ctx.close?.().catch(() => {})
  }
}
