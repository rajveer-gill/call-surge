'use client'

import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { m, AnimatePresence, useReducedMotion } from 'framer-motion'
import { Clock, Copy, Sparkles, Sun, X } from 'lucide-react'
import {
  DAYS_FULL,
  DAYS_SHORT,
  type DayIndex,
  type WeeklySchedule,
  defaultWeeklySchedule,
  formatTimeLabel,
  parseHoursToWeekly,
  summarizeSchedule,
  weeklyScheduleToString,
} from '@/lib/businessHours'

function timeToMinutes(t: string): number {
  const [h, m] = t.split(':').map((x) => parseInt(x, 10))
  if (Number.isNaN(h) || Number.isNaN(m)) return -1
  return h * 60 + m
}

function cloneSchedule(s: WeeklySchedule): WeeklySchedule {
  return s.map((d) => ({ ...d }))
}

export interface BusinessHoursModalProps {
  isOpen: boolean
  onClose: () => void
  /** Current `hours` field from settings */
  hoursText: string
  /** Called with serialized hours string when user saves */
  /** Applies the new hours. Return false to keep the modal open — a button that says
   *  it applied something has to have applied it. */
  onApply: (nextHours: string) => void | boolean | Promise<boolean>
}

export function BusinessHoursModal({ isOpen, onClose, hoursText, onApply }: BusinessHoursModalProps) {
  const [saving, setSaving] = useState(false)
  const reduceMotion = useReducedMotion()
  const titleId = useId()
  const overlayScrollRef = useRef<HTMLDivElement>(null)
  /** Single scroll region: presets + day rows + preview + footer (fixes flex child not shrinking → maxScroll 0 → rubberband). */
  const modalBodyScrollRef = useRef<HTMLDivElement>(null)
  const [mounted, setMounted] = useState(false)
  const [schedule, setSchedule] = useState<WeeklySchedule>(() => defaultWeeklySchedule())
  const [parseWarning, setParseWarning] = useState<string | null>(null)
  const [timeError, setTimeError] = useState<string | null>(null)

  useEffect(() => setMounted(true), [])

  useEffect(() => {
    if (!isOpen) return
    const r = parseHoursToWeekly(hoursText)
    setSchedule(cloneSchedule(r.schedule))
    setParseWarning(r.warning ?? null)
    setTimeError(null)
  }, [isOpen, hoursText])

  useEffect(() => {
    if (!isOpen) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [isOpen])

  useEffect(() => {
    if (!isOpen) return
    overlayScrollRef.current?.scrollTo(0, 0)
  }, [isOpen])

  /**
   * Chrome uses wheel on `<input type="time">` for stepping; React onWheel is passive.
   * Non-passive capture on the modal body scroll container scrolls the whole white area.
   */
  useEffect(() => {
    if (!isOpen) return
    let attachedEl: HTMLDivElement | null = null
    let onWheel: ((e: WheelEvent) => void) | null = null
    let raf1 = 0
    let raf2 = 0

    const tryAttach = () => {
      const el = modalBodyScrollRef.current
      if (!el || onWheel) return

      onWheel = (e: WheelEvent) => {
        if (!el.contains(e.target as Node)) return

        let dy = e.deltaY
        if (e.deltaMode === 1) dy *= 16
        if (e.deltaMode === 2) dy *= el.clientHeight

        const maxScroll = Math.max(0, el.scrollHeight - el.clientHeight)
        if (maxScroll <= 0) {
          e.preventDefault()
          e.stopPropagation()
          return
        }

        const top = el.scrollTop
        const atTop = top <= 0.5
        const atBottom = top >= maxScroll - 0.5
        if (atTop && dy < 0) return
        if (atBottom && dy > 0) return

        el.scrollTop = Math.min(maxScroll, Math.max(0, top + dy))
        e.preventDefault()
        e.stopPropagation()
      }
      el.addEventListener('wheel', onWheel, { passive: false, capture: true })
      attachedEl = el
    }

    raf1 = requestAnimationFrame(() => {
      tryAttach()
      raf2 = requestAnimationFrame(tryAttach)
    })

    return () => {
      cancelAnimationFrame(raf1)
      cancelAnimationFrame(raf2)
      if (attachedEl && onWheel) {
        // removeEventListener matches on capture only; `passive` is not part of
        // its options and was silently ignored. Removal worked, the type was right
        // to object.
        attachedEl.removeEventListener('wheel', onWheel, { capture: true })
      }
    }
  }, [isOpen])

  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isOpen, onClose])

  const updateDay = (idx: DayIndex, patch: Partial<WeeklySchedule[number]>) => {
    setSchedule((prev) => {
      const next = cloneSchedule(prev)
      next[idx] = { ...next[idx], ...patch }
      return next
    })
    setTimeError(null)
  }

  const presetClassicOffice = () => {
    setSchedule(defaultWeeklySchedule())
    setParseWarning(null)
    setTimeError(null)
  }

  const presetIncludeSaturday = () => {
    const base = defaultWeeklySchedule()
    base[5] = { closed: false, open: '10:00', close: '14:00' }
    setSchedule(base)
    setParseWarning(null)
    setTimeError(null)
  }

  const preset247 = () => {
    setSchedule(
      Array.from({ length: 7 }, () => ({
        closed: false,
        open: '00:00',
        close: '23:59',
      }))
    )
    setParseWarning(null)
    setTimeError(null)
  }

  const copyMondayToWeekdays = () => {
    setSchedule((prev) => {
      const next = cloneSchedule(prev)
      const src = next[0]
      for (let i = 1; i <= 4; i++) {
        next[i] = { closed: src.closed, open: src.open, close: src.close }
      }
      return next
    })
    setTimeError(null)
  }

  const validateTimes = (): boolean => {
    for (let i = 0; i < 7; i++) {
      const d = schedule[i]
      if (d.closed) continue
      const a = timeToMinutes(d.open)
      const b = timeToMinutes(d.close)
      if (a < 0 || b < 0) {
        setTimeError(`Invalid time on ${DAYS_FULL[i]}.`)
        return false
      }
      if (b <= a) {
        setTimeError(`${DAYS_FULL[i]}: closing time must be after opening time (same day).`)
        return false
      }
    }
    setTimeError(null)
    return true
  }

  const handleSave = () => {
    if (!validateTimes()) return
    void (async () => {
      setSaving(true)
      try {
        const ok = await onApply(weeklyScheduleToString(schedule))
        // Only close when it actually stuck. `void` (the older local-only callers)
        // counts as success; an explicit false means the write failed and the error
        // is on the page behind this modal.
        if (ok !== false) onClose()
      } finally {
        setSaving(false)
      }
    })()
  }

  const dur = reduceMotion ? 0 : 0.22
  const spring = reduceMotion ? { duration: 0 } : { type: 'spring', stiffness: 380, damping: 28 }

  if (!mounted) return null

  const modal = (
    <AnimatePresence>
      {isOpen && (
        <div
          ref={overlayScrollRef}
          className="fixed inset-0 z-[100] overflow-y-auto overscroll-contain"
        >
          <m.button
            type="button"
            aria-label="Close"
            className="fixed inset-0 z-0 bg-gray-950/60 backdrop-blur-sm"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: dur }}
            onClick={onClose}
          />
          {/*
            Never vertically center with items-center: tall modals overflow above the viewport and clip the header.
            Top padding + max-height reserves space; dialog stays below the browser chrome / notch.
          */}
          <div className="relative z-[1] flex w-full flex-col items-center px-4 pb-16 pt-[max(2.25rem,calc(env(safe-area-inset-top,0px)+32px))] sm:px-6 sm:pb-24 sm:pt-[max(3rem,calc(env(safe-area-inset-top,0px)+40px))]">
          <m.div
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            className="relative z-[101] flex h-[min(920px,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)-7rem))] min-h-0 w-full max-w-3xl flex-col overflow-hidden rounded-3xl border border-gray-200/80 bg-white shadow-2xl shadow-primary-900/15 max-h-[min(920px,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)-7rem))]"
            initial={{ opacity: 0, y: reduceMotion ? 0 : 28, scale: reduceMotion ? 1 : 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: reduceMotion ? 0 : 16, scale: reduceMotion ? 1 : 0.98 }}
            transition={spring}
          >
            <div className="relative shrink-0 overflow-hidden bg-gradient-to-br from-primary-600 via-primary-600 to-cyan-600 px-6 pb-8 pt-7 text-white sm:px-8 sm:pb-10 sm:pt-8">
              <div className="pointer-events-none absolute -right-16 -top-24 h-48 w-48 rounded-full bg-white/15 blur-3xl" />
              <div className="pointer-events-none absolute -bottom-20 -left-10 h-40 w-40 rounded-full bg-cyan-300/20 blur-3xl" />
              <div className="relative flex items-start justify-between gap-3">
                <div className="flex items-center gap-3">
                  <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-white/20 backdrop-blur-sm">
                    <Clock className="h-6 w-6 text-white" aria-hidden />
                  </div>
                  <div className="min-w-0 pr-2">
                    <h2 id={titleId} className="font-display text-xl font-semibold tracking-tight sm:text-2xl">
                      Business hours
                    </h2>
                    <p className="mt-1 max-w-xl text-sm leading-relaxed text-white/85 sm:text-[15px]">
                      Toggle days, set open and close. Your receptionist reads this on calls and texts.
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={onClose}
                  className="rounded-xl p-2 text-white/90 transition hover:bg-white/15 hover:text-white"
                  aria-label="Close dialog"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>
            </div>

            {/* One scroll column below header: flex-1 min-h-0 so inner overflow-y-auto gets a real maxScroll */}
            <div
              ref={modalBodyScrollRef}
              className="relative z-[2] flex min-h-0 flex-1 flex-col overflow-y-auto overflow-x-hidden overscroll-contain border-t border-gray-200/95 bg-white [-webkit-overflow-scrolling:touch]"
            >
              {parseWarning && (
                <m.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  className="mx-5 mb-0 mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-900 sm:mx-8"
                >
                  {parseWarning}
                </m.div>
              )}

              <div
                className={`shrink-0 border-b border-gray-200 bg-gray-50 px-5 py-4 sm:px-8 ${parseWarning ? 'mt-4' : ''}`}
              >
                <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-gray-500">Quick presets</p>
                <div className="flex flex-wrap gap-2.5">
                <button
                  type="button"
                  onClick={presetClassicOffice}
                  className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-800 shadow-sm transition hover:border-primary-300 hover:bg-primary-50"
                >
                  <Sparkles className="h-4 w-4 text-primary-600" />
                  Mon–Fri 9–5
                </button>
                <button
                  type="button"
                  onClick={presetIncludeSaturday}
                  className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-800 shadow-sm transition hover:border-primary-300 hover:bg-primary-50"
                >
                  + Sat hours
                </button>
                <button
                  type="button"
                  onClick={preset247}
                  className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-800 shadow-sm transition hover:border-primary-300 hover:bg-primary-50"
                >
                  <Sun className="h-4 w-4 text-amber-500" />
                  24/7
                </button>
                <button
                  type="button"
                  onClick={copyMondayToWeekdays}
                  className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-800 shadow-sm transition hover:border-primary-300 hover:bg-primary-50"
                >
                  <Copy className="h-4 w-4 text-gray-600" />
                  Copy Mon → weekdays
                </button>
                </div>
              </div>

              <div className="space-y-3 px-5 pb-2 pr-1 pt-5 sm:space-y-4 sm:px-8">
                {DAYS_FULL.map((label, idx) => {
                  const i = idx as DayIndex
                  const row = schedule[i]
                  return (
                    <div
                      key={label}
                      className="rounded-2xl border border-gray-100 bg-gradient-to-b from-gray-50/90 to-white p-4 shadow-sm sm:p-5"
                    >
                      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between lg:gap-6">
                        <div className="flex flex-wrap items-center justify-between gap-3 lg:min-w-[220px] lg:justify-start lg:gap-4">
                          <span className="min-w-[7rem] text-base font-semibold text-gray-900">{label}</span>
                          <div
                            className="inline-flex shrink-0 rounded-full border border-gray-200 bg-white p-1 shadow-inner"
                            role="group"
                            aria-label={`${DAYS_SHORT[i]} open or closed`}
                          >
                            <button
                              type="button"
                              onClick={() => updateDay(i, { closed: false })}
                              className={`rounded-full px-4 py-2 text-sm font-semibold transition ${
                                !row.closed
                                  ? 'bg-primary-600 text-white shadow-sm'
                                  : 'text-gray-600 hover:text-gray-900'
                              }`}
                            >
                              Open
                            </button>
                            <button
                              type="button"
                              onClick={() => updateDay(i, { closed: true })}
                              className={`rounded-full px-4 py-2 text-sm font-semibold transition ${
                                row.closed
                                  ? 'bg-gray-800 text-white shadow-sm'
                                  : 'text-gray-600 hover:text-gray-900'
                              }`}
                            >
                              Closed
                            </button>
                          </div>
                        </div>
                        {!row.closed && (
                          <div className="grid flex-1 grid-cols-1 gap-4 sm:grid-cols-2 lg:max-w-md lg:gap-6 xl:max-w-lg">
                            <label className="flex min-w-0 flex-col gap-1.5">
                              <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                Opens
                              </span>
                              <input
                                type="time"
                                value={row.open}
                                step={300}
                                onChange={(e) => updateDay(i, { open: e.target.value })}
                                className="cs-field min-h-[44px] w-full min-w-[10rem] font-mono text-base"
                              />
                              <span className="text-xs text-gray-500">{formatTimeLabel(row.open)}</span>
                            </label>
                            <label className="flex min-w-0 flex-col gap-1.5">
                              <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                Closes
                              </span>
                              <input
                                type="time"
                                value={row.close}
                                step={300}
                                onChange={(e) => updateDay(i, { close: e.target.value })}
                                className="cs-field min-h-[44px] w-full min-w-[10rem] font-mono text-base"
                              />
                              <span className="text-xs text-gray-500">{formatTimeLabel(row.close)}</span>
                            </label>
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>

              {timeError && (
                <p className="mx-5 mt-2 text-center text-xs font-medium text-red-600 sm:mx-8" role="alert">
                  {timeError}
                </p>
              )}

              <div className="mx-5 mt-4 rounded-xl border border-gray-100 bg-gray-50 px-4 py-3 text-sm leading-relaxed text-gray-600 sm:mx-8">
                <span className="font-semibold text-gray-800">Preview </span>
                {summarizeSchedule(schedule, 280)}
              </div>

              <div className="mx-5 mt-6 flex flex-col-reverse gap-3 border-t border-gray-100 pb-6 pt-6 sm:mx-8 sm:flex-row sm:justify-end sm:pb-8">
                <button
                  type="button"
                  onClick={onClose}
                  className="rounded-xl px-5 py-3 text-sm font-medium text-gray-700 transition hover:bg-gray-100"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={saving}
                  className="rounded-xl bg-primary-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-primary-600/25 transition hover:bg-primary-700 disabled:opacity-60"
                >
                  {saving ? 'Saving…' : 'Apply hours'}
                </button>
              </div>
            </div>
          </m.div>
          </div>
        </div>
      )}
    </AnimatePresence>
  )

  return createPortal(modal, document.body)
}
