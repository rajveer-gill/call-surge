'use client'

/**
 * Plays a short two-note chime when a booking request appears while the dashboard is open.
 * See newRequestAlert.ts for why, and for the rules about when NOT to play.
 *
 * The tone is synthesised rather than loaded from an .mp3 so there is no asset to 404,
 * nothing to download before the first alert can play, and no binary in the repo. It is
 * two soft sine notes — this fires in a room with customers in it, so it should read as
 * a doorbell, not an alarm.
 *
 * Browsers refuse to play audio until the user has interacted with the page, and a
 * refusal is silent. So the AudioContext is created on the first click or keypress and
 * kept; a dashboard nobody has touched since loading stays quiet, which is the one
 * failure mode we cannot fix from here and why email remains the real notification.
 */

import { useCallback, useEffect, useRef } from 'react'
import { awaitingRequestIds, chimeEnabled, newlyArrived } from '@/lib/newRequestAlert'

type StatusRow = { id: number; status: string }

type AudioContextCtor = typeof AudioContext

function audioContextCtor(): AudioContextCtor | null {
  if (typeof window === 'undefined') return null
  return (
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext ||
    null
  )
}

/** One doorbell-ish note. Envelope ramps rather than hard starts, which would click. */
function playNote(ctx: AudioContext, freq: number, startAt: number, seconds: number): void {
  const osc = ctx.createOscillator()
  const gain = ctx.createGain()
  osc.type = 'sine'
  osc.frequency.value = freq
  gain.gain.setValueAtTime(0.0001, startAt)
  gain.gain.exponentialRampToValueAtTime(0.18, startAt + 0.02)
  gain.gain.exponentialRampToValueAtTime(0.0001, startAt + seconds)
  osc.connect(gain).connect(ctx.destination)
  osc.start(startAt)
  osc.stop(startAt + seconds + 0.02)
}

/**
 * Watch a list of appointments and chime when a new request shows up in it.
 *
 * Pass the same array the page already renders — this adds no fetching of its own, so it
 * inherits whatever poll interval the caller uses and cannot double the API load.
 *
 * `ready` guards the baseline: pass false while the first fetch is still in flight, or an
 * empty initial array would be recorded as the baseline and every existing request would
 * chime the moment the real data landed.
 */
export function useNewRequestChime(appointments: readonly StatusRow[], ready: boolean): void {
  const seen = useRef<Set<number> | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)

  // Create the context on a real user gesture, which is the only moment a browser will
  // allow it to start running. One listener, removed as soon as it fires.
  useEffect(() => {
    const Ctor = audioContextCtor()
    if (!Ctor) return
    const unlock = () => {
      if (!ctxRef.current) {
        try {
          ctxRef.current = new Ctor()
        } catch {
          return
        }
      }
      void ctxRef.current.resume?.().catch(() => {})
    }
    window.addEventListener('pointerdown', unlock, { once: true })
    window.addEventListener('keydown', unlock, { once: true })
    return () => {
      window.removeEventListener('pointerdown', unlock)
      window.removeEventListener('keydown', unlock)
    }
  }, [])

  // Close the context on unmount so navigating away doesn't leak an audio device.
  useEffect(() => {
    return () => {
      void ctxRef.current?.close?.().catch(() => {})
      ctxRef.current = null
    }
  }, [])

  const chime = useCallback(() => {
    const ctx = ctxRef.current
    if (!ctx || ctx.state !== 'running') return
    try {
      const now = ctx.currentTime
      playNote(ctx, 880, now, 0.18) // A5
      playNote(ctx, 1174.66, now + 0.16, 0.28) // D6
    } catch {
      /* never let a sound break the dashboard */
    }
  }, [])

  useEffect(() => {
    if (!ready) return
    const current = awaitingRequestIds(appointments)
    const fresh = newlyArrived(seen.current, current)
    // Record before playing: if the chime throws, the next poll must not replay it.
    seen.current = current
    if (fresh.length > 0 && chimeEnabled()) chime()
  }, [appointments, ready, chime])
}
