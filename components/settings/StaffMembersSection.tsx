'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import type { AxiosInstance } from 'axios'
import { Calendar, CheckCircle2, ChevronRight, Clock, Mail, Pencil, Phone, Plus, Tag, Trash2, User, Users, X } from 'lucide-react'
import type { ServiceRow } from '@/components/settings/StructuredListEditors'
import { StaffServicePicker } from '@/components/settings/StaffServicePicker'
import { fadeUpChild, staggerContainer } from '@/components/motion'

export type DayHours = { start: string; end: string }
export type StaffRow = {
  id: string
  name: string
  phone: string
  email: string
  notes: string
  service_ids: string[]
  working_days: string[]
  working_hours: Record<string, DayHours>
  time_off: string[]
}

export const WORKING_DAYS: { code: string; label: string }[] = [
  { code: 'mon', label: 'Mon' },
  { code: 'tue', label: 'Tue' },
  { code: 'wed', label: 'Wed' },
  { code: 'thu', label: 'Thu' },
  { code: 'fri', label: 'Fri' },
  { code: 'sat', label: 'Sat' },
  { code: 'sun', label: 'Sun' },
]
const _WORKING_DAY_ORDER = WORKING_DAYS.map((d) => d.code)

export function normalizeStaffFromApi(raw: unknown): StaffRow[] {
  if (!Array.isArray(raw)) return []
  return raw.map((item) => {
    const o = item as Record<string, unknown>
    const id = typeof o.id === 'string' && o.id.trim() ? o.id.trim() : crypto.randomUUID()
    const rawSvc = o.service_ids
    const service_ids = Array.isArray(rawSvc)
      ? rawSvc.map((x) => String(x).trim()).filter(Boolean)
      : []
    const rawDays = o.working_days
    const daySet = new Set(
      Array.isArray(rawDays) ? rawDays.map((x) => String(x).trim().toLowerCase()) : [],
    )
    const working_days = _WORKING_DAY_ORDER.filter((d) => daySet.has(d))
    const working_hours: Record<string, DayHours> = {}
    const rawHours = o.working_hours
    if (rawHours && typeof rawHours === 'object') {
      for (const d of _WORKING_DAY_ORDER) {
        const w = (rawHours as Record<string, unknown>)[d]
        if (w && typeof w === 'object') {
          const start = String((w as Record<string, unknown>).start ?? '').trim()
          const end = String((w as Record<string, unknown>).end ?? '').trim()
          if (start && end) working_hours[d] = { start, end }
        }
      }
    }
    const rawOff = o.time_off
    const time_off = Array.isArray(rawOff)
      ? Array.from(new Set(rawOff.map((x) => String(x).trim()).filter((s) => /^\d{4}-\d{2}-\d{2}$/.test(s)))).sort()
      : []
    return {
      id,
      name: String(o.name ?? '').trim(),
      phone: String(o.phone ?? '').trim(),
      email: String(o.email ?? '').trim(),
      notes: String(o.notes ?? ''),
      service_ids,
      working_days,
      working_hours,
      time_off,
    }
  })
}

function maskPhone(phone: string): string {
  const d = phone.replace(/\D/g, '')
  if (d.length < 4) return phone ? '••••' : ''
  return `••••${d.slice(-4)}`
}

function hasStaffPhone(phone: string): boolean {
  return phone.replace(/\D/g, '').length >= 10
}

/** Empty is OK; if provided, must be at least 10 digits. */
function isValidOptionalStaffPhone(phone: string): boolean {
  const t = phone.trim()
  if (!t) return true
  return hasStaffPhone(t)
}

/** "HH:MM" -> minutes since midnight, or null if unparseable. */
function hhmmToMinutes(hhmm: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim())
  if (!m) return null
  const h = parseInt(m[1], 10)
  const min = parseInt(m[2], 10)
  if (h < 0 || h > 23 || min < 0 || min > 59) return null
  return h * 60 + min
}

const _DAY_LABEL: Record<string, string> = Object.fromEntries(WORKING_DAYS.map((d) => [d.code, d.label]))

/**
 * Hard validation for staff working days/hours against the shop's open window.
 * Returns an error message string, or null when valid. No-op when shopHours is undefined
 * (hours not configured), so tenants aren't blocked before setting business hours.
 */
export function validateWorkingHours(
  workingDays: string[],
  workingHours: Record<string, DayHours>,
  shopHours?: Record<string, DayHours>,
): string | null {
  if (!shopHours || !Object.keys(shopHours).length) return null
  for (const code of workingDays) {
    const label = _DAY_LABEL[code] ?? code
    const shopWin = shopHours[code]
    if (!shopWin) {
      return `${label} is selected, but the shop is closed that day. Remove it or open the shop on ${label}.`
    }
    const win = workingHours[code]
    if (!win || (!win.start && !win.end)) continue // blank = full shop hours, always fine
    if (!win.start || !win.end) {
      return `${label} needs both a start and end time, or leave both blank for full shop hours.`
    }
    const ws = hhmmToMinutes(win.start)
    const we = hhmmToMinutes(win.end)
    const ss = hhmmToMinutes(shopWin.start)
    const se = hhmmToMinutes(shopWin.end)
    if (ws === null || we === null) return `${label} has an invalid time.`
    if (ws >= we) return `${label}: end time must be after the start time.`
    if (ss !== null && se !== null && (ws < ss || we > se)) {
      return `${label} hours (${win.start}–${win.end}) must fall within shop hours (${shopWin.start}–${shopWin.end}).`
    }
  }
  return null
}

type Notify = (msg: { type: 'success' | 'error'; text: string } | null) => void

export function StaffMembersSection({
  staff,
  availableServices,
  shopHours,
  onStaffChange,
  api,
  onNotify,
  onAfterSave,
}: {
  staff: StaffRow[]
  availableServices: ServiceRow[]
  /**
   * Shop open hours keyed by day code (mon..sun). A present key = shop is open that day.
   * When provided, closed days can't be picked and per-day time inputs are bounded to shop hours.
   * Undefined = no restriction (hours not configured yet).
   */
  shopHours?: Record<string, DayHours>
  onStaffChange: (next: StaffRow[]) => void
  api: AxiosInstance
  onNotify: Notify
  onAfterSave?: () => void
}) {
  const openDaySet = shopHours && Object.keys(shopHours).length ? new Set(Object.keys(shopHours)) : null
  const reduceMotion = useReducedMotion()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const firstFieldRef = useRef<HTMLInputElement>(null)

  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'add' | 'edit'>('add')
  const [editId, setEditId] = useState<string | null>(null)
  const [draft, setDraft] = useState({
    name: '',
    phone: '',
    email: '',
    notes: '',
    service_ids: [] as string[],
    working_days: [] as string[],
    working_hours: {} as Record<string, DayHours>,
  })
  const [draftError, setDraftError] = useState<string | null>(null)

  /** People already set up, offered as a starting point for the next one — a salon
   *  runs two or three skill profiles, not ten. Only those with an actual selection:
   *  copying "everything" is what leaving it blank already does. */
  const copySources = useMemo(
    () =>
      staff
        .filter((s) => s.id !== editId && s.service_ids.length > 0)
        .map((s) => ({ id: s.id, name: s.name, service_ids: s.service_ids })),
    [staff, editId]
  )
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const motionProps = reduceMotion
    ? { initial: false, animate: { opacity: 1 }, exit: { opacity: 1 } }
    : {
        initial: { opacity: 0, y: 14, scale: 0.97 },
        animate: { opacity: 1, y: 0, scale: 1 },
        exit: { opacity: 0, y: 12, scale: 0.98 },
        transition: { type: 'spring' as const, stiffness: 380, damping: 30 },
      }

  const openModal = useCallback((opts: { mode: 'add' | 'edit'; row?: StaffRow }) => {
    setDraftError(null)
    setMode(opts.mode)
    if (opts.mode === 'add') {
      setEditId(null)
      setDraft({ name: '', phone: '', email: '', notes: '', service_ids: [], working_days: [], working_hours: {} })
    } else if (opts.row) {
      setEditId(opts.row.id)
      setDraft({
        name: opts.row.name,
        phone: opts.row.phone,
        email: opts.row.email,
        notes: opts.row.notes,
        service_ids: [...opts.row.service_ids],
        working_days: [...(opts.row.working_days || [])],
        working_hours: { ...(opts.row.working_hours || {}) },
      })
    }
    setOpen(true)
  }, [])

  const closeModal = useCallback(() => {
    setOpen(false)
    setEditId(null)
    setDraftError(null)
  }, [])

  useEffect(() => {
    const el = dialogRef.current
    if (!el) return
    if (open) {
      if (!el.open) el.showModal()
      const t = window.setTimeout(() => firstFieldRef.current?.focus(), reduceMotion ? 0 : 80)
      return () => window.clearTimeout(t)
    }
    if (el.open) el.close()
    return undefined
  }, [open, reduceMotion])

  const parseApiError = (e: unknown): string => {
    const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    if (typeof d === 'string') return d
    if (Array.isArray(d)) {
      const first = d[0] as { msg?: string }
      return first?.msg || 'Could not save team member'
    }
    if (d && typeof d === 'object') {
      const msg = (d as { message?: string }).message
      if (typeof msg === 'string') return msg
    }
    return 'Could not save team member'
  }

  const saveDraft = async () => {
    const name = draft.name.trim()
    const phone = draft.phone.trim()
    if (!name) {
      setDraftError('Name is required.')
      return
    }
    if (!isValidOptionalStaffPhone(phone)) {
      setDraftError('If you add a phone, use at least 10 digits (for SMS alerts and call transfers).')
      return
    }
    const hoursError = validateWorkingHours(draft.working_days, draft.working_hours, shopHours)
    if (hoursError) {
      setDraftError(hoursError)
      return
    }
    setDraftError(null)
    setSaving(true)
    try {
      const nextStaff: StaffRow[] =
        mode === 'add'
          ? [
              ...staff,
              {
                id: crypto.randomUUID(),
                name,
                phone,
                email: draft.email.trim(),
                notes: draft.notes,
                service_ids: draft.service_ids,
                working_days: draft.working_days,
                working_hours: draft.working_hours,
                time_off: [],
              },
            ]
          : staff.map((s) =>
              s.id === editId
                ? {
                    ...s,
                    name,
                    phone,
                    email: draft.email.trim(),
                    notes: draft.notes,
                    service_ids: draft.service_ids,
                    working_days: draft.working_days,
                    working_hours: draft.working_hours,
                  }
                : s,
            )

      const { data } = await api.patch<Record<string, unknown>>('/api/business-info', {
        staff: nextStaff.map((s) => ({
          id: s.id,
          name: s.name,
          phone: s.phone || undefined,
          email: s.email || undefined,
          notes: s.notes || undefined,
          service_ids: s.service_ids.length ? s.service_ids : undefined,
          working_days: s.working_days?.length ? s.working_days : undefined,
          working_hours: s.working_hours && Object.keys(s.working_hours).length ? s.working_hours : undefined,
          time_off: s.time_off?.length ? s.time_off : undefined,
        })),
      })
      const next = normalizeStaffFromApi(data.staff)
      onStaffChange(next)
      onNotify({ type: 'success', text: 'Team member saved.' })
      onAfterSave?.()
      closeModal()
    } catch (e) {
      onNotify({ type: 'error', text: parseApiError(e) })
    } finally {
      setSaving(false)
    }
  }

  const confirmDelete = async (row: StaffRow) => {
    if (!window.confirm(`Remove ${row.name.trim() || 'this team member'} from your roster?`)) return
    setDeleting(true)
    try {
      const nextStaff = staff.filter((s) => s.id !== row.id)
      const { data } = await api.patch<Record<string, unknown>>('/api/business-info', {
        staff: nextStaff.map((s) => ({
          id: s.id,
          name: s.name,
          phone: s.phone || undefined,
          email: s.email || undefined,
          notes: s.notes || undefined,
          service_ids: s.service_ids.length ? s.service_ids : undefined,
          working_days: s.working_days?.length ? s.working_days : undefined,
          working_hours: s.working_hours && Object.keys(s.working_hours).length ? s.working_hours : undefined,
          time_off: s.time_off?.length ? s.time_off : undefined,
        })),
      })
      const next = normalizeStaffFromApi(data.staff)
      onStaffChange(next)
      onNotify({ type: 'success', text: 'Team member removed.' })
      onAfterSave?.()
      if (open && editId === row.id) closeModal()
    } catch (e) {
      onNotify({ type: 'error', text: parseApiError(e) })
    } finally {
      setDeleting(false)
    }
  }

  const namedCount = staff.filter((s) => s.name.trim()).length
  const phoneCount = staff.filter((s) => hasStaffPhone(s.phone)).length
  const rosterReady = namedCount > 0

  return (
    <div>
      <div
        className={`relative overflow-hidden rounded-2xl border p-4 mb-4 transition-all duration-300 ${
          rosterReady ? 'border-emerald-200 bg-emerald-50/40' : 'border-amber-300 bg-amber-50/50 ring-1 ring-amber-200'
        }`}
      >
        <div
          aria-hidden
          className={`pointer-events-none absolute -right-10 -top-10 h-28 w-28 rounded-full blur-2xl transition-colors duration-500 ${
            rosterReady ? 'bg-emerald-300/20' : 'bg-amber-300/30'
          }`}
        />
        <div className="relative flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors duration-300 ${
                rosterReady ? 'bg-emerald-100 text-emerald-600' : 'bg-amber-100 text-amber-600'
              }`}
            >
              <Users className="h-5 w-5" aria-hidden />
            </div>
            <div>
              <h3 className="flex items-center gap-1.5 text-sm font-semibold text-gray-900">
                At least one team member
                <span className="text-rose-500" aria-label="required">
                  *
                </span>
              </h3>
              <p className="text-xs text-gray-500">
                Required — callers can&apos;t book or reach your team until someone&apos;s on the roster.
              </p>
            </div>
          </div>
          <span
            className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold transition-colors duration-300 ${
              rosterReady ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'
            }`}
          >
            {rosterReady ? (
              <>
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                {namedCount} added
              </>
            ) : (
              <>
                <span className="relative flex h-2 w-2" aria-hidden>
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
                </span>
                Action needed
              </>
            )}
          </span>
        </div>
        {staff.length > 0 && (
          <div className="relative mt-3">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-teal-100 bg-white/70 px-2.5 py-1 text-xs font-medium text-teal-800">
              <Calendar className="h-3.5 w-3.5" aria-hidden />
              {phoneCount}/{staff.length} with phone for SMS &amp; transfers
            </span>
          </div>
        )}
      </div>

      {staff.some((s) => s.name.trim() && !hasStaffPhone(s.phone)) && (
        <p className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          Team members without a phone can still be booked by name. Add a phone to text them about new bookings and to
          allow call transfers to that person.
        </p>
      )}

      <motion.ul
        className="space-y-2 mb-3 max-h-[min(420px,50vh)] overflow-y-auto pr-1"
        variants={reduceMotion ? undefined : staggerContainer}
        initial="hidden"
        animate="visible"
      >
        <AnimatePresence mode="popLayout">
          {staff.length === 0 ? (
            <motion.li
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-amber-300 bg-amber-50/40 px-4 py-8 text-center"
            >
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-amber-100 text-amber-600">
                <User className="h-5 w-5" aria-hidden />
              </span>
              <span className="text-sm font-medium text-gray-700">No team members yet</span>
              <span className="text-xs text-gray-500">Add at least one so callers can book with a specific person.</span>
            </motion.li>
          ) : (
            staff.map((s, i) => {
              const svcLabels = s.service_ids
                .map((id) => availableServices.find((svc) => svc.id === id)?.name)
                .filter(Boolean) as string[]
              return (
              <motion.li
                key={s.id}
                layout
                variants={fadeUpChild}
                custom={i}
                exit={{ opacity: 0, scale: 0.96 }}
                className="list-none"
              >
                <motion.div
                  className="group flex rounded-xl border border-teal-100 bg-white/95 shadow-sm hover:shadow-md hover:border-teal-300 transition-shadow"
                  whileHover={reduceMotion ? {} : { x: 2 }}
                >
                  <button
                    type="button"
                    onClick={() => openModal({ mode: 'edit', row: s })}
                    className="flex flex-1 min-w-0 items-center gap-3 px-3 py-3 text-left rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
                  >
                    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-teal-100 text-teal-800">
                      <User className="h-5 w-5" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="font-semibold text-gray-900 block truncate">{s.name || 'Unnamed'}</span>
                      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-gray-600 mt-0.5">
                        {s.phone ? (
                          <span className="inline-flex items-center gap-1">
                            <Phone className="w-3.5 h-3.5 shrink-0" />
                            <span>{maskPhone(s.phone)}</span>
                          </span>
                        ) : (
                          <span className="text-gray-400">No phone on file</span>
                        )}
                        {s.email ? (
                          <span className="inline-flex items-center gap-1 text-gray-500 truncate max-w-[180px]">
                            <Mail className="w-3 h-3" />
                            {s.email}
                          </span>
                        ) : null}
                      </span>
                      {svcLabels.length > 0 ? (
                        <span className="flex flex-wrap gap-1 mt-1.5">
                          {svcLabels.map((label) => (
                            <span
                              key={label}
                              className="inline-flex items-center gap-0.5 rounded-full bg-teal-50 px-2 py-0.5 text-[10px] font-medium text-teal-800 border border-teal-100"
                            >
                              <Tag className="w-2.5 h-2.5" />
                              {label}
                            </span>
                          ))}
                        </span>
                      ) : availableServices.length > 0 ? (
                        <span className="text-[10px] text-gray-400 mt-1 block">All services (none selected)</span>
                      ) : null}
                      <span className="mt-1 flex items-center gap-1 text-[10px] text-gray-500">
                        <Clock className="w-2.5 h-2.5 shrink-0" />
                        {s.working_days?.length
                          ? WORKING_DAYS.filter((d) => s.working_days.includes(d.code)).map((d) => d.label).join(', ')
                          : 'Any open day'}
                      </span>
                    </span>
                    <ChevronRight className="w-5 h-5 text-gray-400 group-hover:text-teal-600 shrink-0" />
                  </button>
                  <motion.div className="flex items-center pr-1" layout>
                    <button
                      type="button"
                      onClick={() => openModal({ mode: 'edit', row: s })}
                      className="p-2 rounded-lg text-gray-600 hover:bg-teal-50"
                      title="Edit"
                    >
                      <Pencil className="w-4 h-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => confirmDelete(s)}
                      disabled={deleting}
                      className="p-2 rounded-lg text-red-600 hover:bg-red-50 disabled:opacity-40"
                      title="Remove"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </motion.div>
                </motion.div>
              </motion.li>
              )
            })
          )}
        </AnimatePresence>
      </motion.ul>

      <motion.button
        type="button"
        onClick={() => openModal({ mode: 'add' })}
        className={`inline-flex items-center gap-1.5 px-4 py-2.5 text-sm font-semibold rounded-xl text-white shadow-md ${
          rosterReady
            ? 'bg-gradient-to-r from-teal-600 to-emerald-600 hover:from-teal-700 hover:to-emerald-700'
            : 'bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-600 hover:to-amber-700'
        }`}
        whileHover={reduceMotion ? {} : { scale: 1.02 }}
        whileTap={reduceMotion ? {} : { scale: 0.98 }}
      >
        <Plus className="w-4 h-4" /> {rosterReady ? 'Add team member' : 'Add your first team member'}
      </motion.button>

      <dialog
        ref={dialogRef}
        className="w-[min(100%,28rem)] max-h-[90vh] rounded-2xl border border-gray-200 bg-white p-0 text-gray-900 shadow-2xl backdrop:bg-black/55"
        onCancel={(ev) => {
          ev.preventDefault()
          closeModal()
        }}
      >
        <AnimatePresence>
          {open && (
            <motion.div {...motionProps} className="flex max-h-[90vh] flex-col overflow-hidden rounded-2xl">
              <motion.div
                className="flex items-center justify-between border-b border-teal-100 px-5 py-4 bg-gradient-to-r from-teal-50 to-emerald-50"
                layout
              >
                <h3 className="text-lg font-bold text-gray-900">
                  {mode === 'add' ? 'Add team member' : 'Edit team member'}
                </h3>
                <button type="button" onClick={closeModal} className="rounded-lg p-2 hover:bg-white/80" aria-label="Close">
                  <X className="w-5 h-5" />
                </button>
              </motion.div>
              <div className="space-y-4 overflow-y-auto px-5 py-4">
                {draftError && (
                  <p className="text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">{draftError}</p>
                )}
                <motion.div layout>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
                  <input
                    ref={firstFieldRef}
                    type="text"
                    value={draft.name}
                    onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                    className="cs-field w-full"
                    placeholder="e.g. Alex Rivera"
                    maxLength={120}
                    autoComplete="name"
                  />
                </motion.div>
                <motion.div layout>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Phone (optional)</label>
                  <p className="text-xs text-gray-500 mb-1.5">
                    Used to transfer calls to this person when a caller asks for them.
                  </p>
                  <input
                    type="tel"
                    value={draft.phone}
                    onChange={(e) => setDraft((d) => ({ ...d, phone: e.target.value }))}
                    className="cs-field w-full tabular-nums"
                    placeholder="+1 555 123 4567"
                    maxLength={32}
                    autoComplete="tel"
                  />
                </motion.div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Email (optional)</label>
                  <p className="text-xs text-gray-500 mb-1.5">
                    When set, we email this person whenever a caller asks for them by name.
                    Leave blank and they are not emailed.
                  </p>
                  <input
                    type="email"
                    value={draft.email}
                    onChange={(e) => setDraft((d) => ({ ...d, email: e.target.value }))}
                    className="cs-field w-full"
                    placeholder="you@business.com"
                    maxLength={254}
                  />
                </div>
                <motion.div layout>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Working days (optional)</label>
                  <p className="text-xs text-gray-500 mb-2">
                    Pick the days this person works. Callers won&apos;t be booked with them on other days. Leave all
                    unselected if they work whenever the shop is open.
                    {openDaySet && ' Days the shop is closed are greyed out.'}
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {WORKING_DAYS.map((d) => {
                      const on = draft.working_days.includes(d.code)
                      // Closed shop day: block picking it, but still allow turning off a stale selection.
                      const closedDay = !!openDaySet && !openDaySet.has(d.code)
                      const disabled = closedDay && !on
                      return (
                        <button
                          key={d.code}
                          type="button"
                          aria-pressed={on}
                          disabled={disabled}
                          title={closedDay ? 'Shop is closed this day' : undefined}
                          onClick={() =>
                            setDraft((dr) => {
                              const nextDays = on
                                ? dr.working_days.filter((c) => c !== d.code)
                                : [...dr.working_days, d.code]
                              const nextHours = { ...dr.working_hours }
                              if (on) delete nextHours[d.code] // clear hours when day removed
                              return { ...dr, working_days: nextDays, working_hours: nextHours }
                            })
                          }
                          className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors ${
                            on
                              ? 'border-teal-500 bg-teal-50 text-teal-700'
                              : disabled
                                ? 'cursor-not-allowed border-gray-100 bg-gray-50 text-gray-300'
                                : 'border-gray-200 text-gray-600 hover:border-gray-300'
                          }`}
                        >
                          {d.label}
                        </button>
                      )
                    })}
                  </div>
                  {draft.working_days.length > 0 && (
                    <div className="mt-3 space-y-1.5">
                      <p className="text-xs text-gray-500">
                        Hours per day (optional — leave blank for full shop hours):
                      </p>
                      {WORKING_DAYS.filter((d) => draft.working_days.includes(d.code)).map((d) => {
                        const win = draft.working_hours[d.code] || { start: '', end: '' }
                        const shopWin = shopHours?.[d.code]
                        const setHour = (field: 'start' | 'end', val: string) =>
                          setDraft((dr) => {
                            const cur = dr.working_hours[d.code] || { start: '', end: '' }
                            const next = { ...cur, [field]: val }
                            const wh = { ...dr.working_hours }
                            if (next.start || next.end) wh[d.code] = next
                            else delete wh[d.code]
                            return { ...dr, working_hours: wh }
                          })
                        return (
                          <div key={d.code} className="flex items-center gap-2 text-sm">
                            <span className="w-10 shrink-0 text-gray-600">{d.label}</span>
                            <input
                              type="time"
                              value={win.start}
                              min={shopWin?.start}
                              max={shopWin?.end}
                              onChange={(e) => setHour('start', e.target.value)}
                              className="rounded-lg border border-gray-300 px-2 py-1 text-sm"
                              aria-label={`${d.label} start time`}
                            />
                            <span className="text-gray-400">to</span>
                            <input
                              type="time"
                              value={win.end}
                              min={shopWin?.start}
                              max={shopWin?.end}
                              onChange={(e) => setHour('end', e.target.value)}
                              className="rounded-lg border border-gray-300 px-2 py-1 text-sm"
                              aria-label={`${d.label} end time`}
                            />
                            {shopWin && (
                              <span className="text-[11px] text-gray-400">
                                shop {shopWin.start}–{shopWin.end}
                              </span>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </motion.div>
                {availableServices.length > 0 ? (
                  <motion.div layout>
                    <label className="block text-sm font-medium text-gray-700 mb-2">Services they provide</label>
                    <p className="text-xs text-gray-500 mb-2">
                      Optional — the AI uses this when booking with a specific person. Tick a whole
                      category at once, or leave it all unticked so they can be booked for anything.
                    </p>
                    <StaffServicePicker
                      services={availableServices}
                      selected={draft.service_ids}
                      onChange={(service_ids) => setDraft((d) => ({ ...d, service_ids }))}
                      copyFrom={copySources}
                    />
                  </motion.div>
                ) : (
                  <p className="text-xs text-gray-500 rounded-lg border border-dashed border-gray-200 px-3 py-2">
                    Add services in the <strong>Services</strong> section above to link them to team members here.
                  </p>
                )}
                <motion.div layout>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Notes for the AI</label>
                  <textarea
                    value={draft.notes}
                    onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))}
                    className="cs-field w-full min-h-[100px]"
                    placeholder="Chair, specialties, schedule - helps booking and Q&A"
                    maxLength={4000}
                  />
                </motion.div>
              </div>
              <motion.div
                className="flex flex-wrap items-center justify-end gap-2 border-t border-gray-100 px-5 py-4 bg-gray-50/80"
                layout
              >
                {mode === 'edit' && editId && (
                  <button
                    type="button"
                    disabled={deleting || saving}
                    onClick={() => {
                      const row = staff.find((x) => x.id === editId)
                      if (row) confirmDelete(row)
                    }}
                    className="mr-auto text-sm font-medium text-red-600 hover:text-red-800 disabled:opacity-40"
                  >
                    Remove
                  </button>
                )}
                <button type="button" onClick={closeModal} className="px-4 py-2 rounded-xl text-sm font-medium bg-gray-100">
                  Cancel
                </button>
                <motion.button
                  type="button"
                  disabled={saving}
                  onClick={() => saveDraft()}
                  className="px-5 py-2 rounded-xl text-sm font-semibold bg-teal-600 text-white disabled:opacity-50"
                  whileTap={reduceMotion ? {} : { scale: 0.96 }}
                >
                  {saving ? 'Saving...' : 'Save'}
                </motion.button>
              </motion.div>
            </motion.div>
          )}
        </AnimatePresence>
      </dialog>
    </div>
  )
}
