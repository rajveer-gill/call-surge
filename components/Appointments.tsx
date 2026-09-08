'use client'

import { useState, useEffect, useMemo, useCallback } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import {
  Calendar,
  Plus,
  RefreshCw,
  LayoutGrid,
  List,
  Sparkles,
  Inbox,
  CheckCircle2,
  AlertCircle,
  Bell,
  BellOff,
} from 'lucide-react'
import { useApiClient } from '@/lib/api'
import { chimeEnabled, setChimeEnabled } from '@/lib/newRequestAlert'
import { useNewRequestChime } from '@/lib/useNewRequestChime'
import AppointmentCalendar from '@/components/AppointmentCalendar'
import { AppointmentCard, apiDetail } from '@/components/appointments/AppointmentCard'
import { ImportFromZenoti } from '@/components/appointments/ImportFromZenoti'
import { needsResponse, isHiddenAppointmentStatus, appointmentDateTimeSortKey } from '@/components/appointments/appointmentStatus'
import type { Appointment } from '@/components/appointments/types'
import { staggerContainer } from '@/components/motion/variants'

export type { Appointment } from '@/components/appointments/types'

// JS getDay() is 0=Sun..6=Sat; staff working_days use these keys (see staff_schedule.DAY_ORDER).
const DOW_KEYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'] as const
const DOW_LABEL: Record<string, string> = {
  sun: 'Sundays',
  mon: 'Mondays',
  tue: 'Tuesdays',
  wed: 'Wednesdays',
  thu: 'Thursdays',
  fri: 'Fridays',
  sat: 'Saturdays',
}

export default function Appointments() {
  const api = useApiClient()
  const reduceMotion = useReducedMotion()
  const [appointments, setAppointments] = useState<Appointment[]>([])
  const [loading, setLoading] = useState(true)
  // Starts true to match the server render; the stored preference is read after mount.
  const [soundOn, setSoundOn] = useState(true)
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [staffFilter, setStaffFilter] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [updatingId, setUpdatingId] = useState<number | null>(null)
  const [clearing, setClearing] = useState(false)
  const [acceptRejectMsg, setAcceptRejectMsg] = useState<{
    id: number
    msg: string
    ok?: boolean
  } | null>(null)
  const [view, setView] = useState<'list' | 'calendar'>('list')
  const [ownerActionModal, setOwnerActionModal] = useState<{
    id: number
    kind: 'reject' | 'cancel'
  } | null>(null)
  const [ownerActionReason, setOwnerActionReason] = useState('')
  const [declinePreview, setDeclinePreview] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [staffOptions, setStaffOptions] = useState<{ id: string; name: string }[]>([])
  // True when this store's real calendar lives in another system (Zenoti). Enables the
  // paste-in importer; false (the default) for every other customer.
  const [externalBooking, setExternalBooking] = useState(false)
  const [bookingProvider, setBookingProvider] = useState('')
  // Full schedule (time off + working days) keyed by staff id, plus shop closures and
  // configured service names — used to warn before saving a conflicting manual booking
  // and to power the service picker.
  const [staffSchedule, setStaffSchedule] = useState<
    Record<string, { name: string; time_off: string[]; working_days: string[] }>
  >({})
  const [closures, setClosures] = useState<string[]>([])
  const [serviceNames, setServiceNames] = useState<string[]>([])
  const [calendarHolds, setCalendarHolds] = useState<
    { date: string; time: string; appointment_id?: number; status: string; name: string }[]
  >([])
  const [diagnostics, setDiagnostics] = useState<{
    total?: number
    likely_mismatch?: boolean
    env_client_id?: string | null
    env_client_id_appointment_count?: number | null
  } | null>(null)
  const [twilioPhone, setTwilioPhone] = useState<string | null>(null)
  const [calendarRefresh, setCalendarRefresh] = useState(0)
  const [form, setForm] = useState({
    name: '',
    email: '',
    phone: '',
    date: '',
    time: '',
    reason: '',
    staff_id: '',
  })

  const fetchAppointments = useCallback(async () => {
    try {
      const res = await api.get('/api/appointments')
      setAppointments(res.data.appointments || [])
      setCalendarHolds(res.data.calendar_holds || [])
      setDiagnostics(res.data.diagnostics || null)
      setTwilioPhone(res.data.twilio_phone_number || null)
    } catch (e) {
      console.error('Failed to fetch appointments', e)
    } finally {
      setLoading(false)
      setCalendarRefresh((n) => n + 1)
    }
  }, [api])

  useEffect(() => {
    fetchAppointments()
    const interval = setInterval(fetchAppointments, 30000)
    return () => clearInterval(interval)
  }, [fetchAppointments])

  useEffect(() => setSoundOn(chimeEnabled()), [])

  // Rings when a request lands while this page is open. Rides the poll above, so it
  // adds no requests of its own. `!loading` holds the baseline until real data arrives.
  useNewRequestChime(appointments, !loading)

  useEffect(() => {
    api
      .get('/api/business-info')
      .then((r) => {
        // External-booking stores (calendar lives in Zenoti etc.) get the paste-in
        // importer; internal stores — every other customer — never see it.
        setExternalBooking((r.data?.booking_mode || 'internal') === 'external')
        setBookingProvider((r.data?.booking_provider_name || '').trim())
        const st = (r.data?.staff || []) as {
          id?: string
          name?: string
          time_off?: string[]
          working_days?: string[]
        }[]
        const named = st.filter((s) => s.id && s.name)
        setStaffOptions(named.map((s) => ({ id: s.id as string, name: s.name as string })))
        setStaffSchedule(
          Object.fromEntries(
            named.map((s) => [
              s.id as string,
              {
                name: s.name as string,
                time_off: Array.isArray(s.time_off) ? s.time_off : [],
                working_days: Array.isArray(s.working_days) ? s.working_days : [],
              },
            ])
          )
        )
        setClosures(Array.isArray(r.data?.closures) ? r.data.closures : [])
        const svcs = (r.data?.services || []) as { name?: string }[]
        setServiceNames(svcs.map((s) => (s?.name || '').trim()).filter(Boolean))
      })
      .catch(() => {
        setStaffOptions([])
        setStaffSchedule({})
        setClosures([])
        setServiceNames([])
      })
  }, [api])

  const staffNameById = Object.fromEntries(staffOptions.map((s) => [s.id, s.name]))

  // Non-blocking heads-up for the New appointment form: flags when the chosen date is a
  // shop closure or the selected stylist is off / doesn't work that weekday. The owner can
  // still save (manual override) — this mirrors staff_schedule.appointment_conflict so the
  // form agrees with how booked appointments are highlighted.
  const createConflict = useMemo(() => {
    const d = form.date.trim()
    if (!d) return null
    if (closures.includes(d)) return 'The shop is closed that day.'
    const sid = form.staff_id.trim()
    const sched = sid ? staffSchedule[sid] : undefined
    if (sched) {
      if (sched.time_off.includes(d)) return `${sched.name} is off that day.`
      const [y, m, day] = d.split('-').map(Number)
      if (y && m && day) {
        const dow = DOW_KEYS[new Date(y, m - 1, day).getDay()]
        if (sched.working_days.length && !sched.working_days.includes(dow)) {
          return `${sched.name} doesn't usually work ${DOW_LABEL[dow]}.`
        }
      }
    }
    return null
  }, [form.date, form.staff_id, closures, staffSchedule])

  const visibleAppointments = useMemo(
    () => appointments.filter((a) => !isHiddenAppointmentStatus(a.status)),
    [appointments]
  )

  const archivedAppointments = useMemo(
    () =>
      appointments
        .filter((a) => isHiddenAppointmentStatus(a.status))
        .sort((a, b) =>
          appointmentDateTimeSortKey(b.date, b.time).localeCompare(
            appointmentDateTimeSortKey(a.date, a.time)
          )
        ),
    [appointments]
  )

  const filtered = visibleAppointments
    .filter((a) => {
      if (staffFilter && (a.staff_id || '') !== staffFilter) return false
      if (statusFilter === 'needs_response' && !needsResponse(a.status)) return false
      if (statusFilter === 'accepted' && !['accepted', 'confirmed', 'completed'].includes(a.status))
        return false
      if (dateFrom && a.date < dateFrom) return false
      if (dateTo && a.date > dateTo) return false
      return true
    })
    .sort((a, b) =>
      appointmentDateTimeSortKey(a.date, a.time).localeCompare(
        appointmentDateTimeSortKey(b.date, b.time)
      )
    )

  // Local "today" (YYYY-MM-DD) to split the active list into upcoming vs already-passed, so old
  // appointments collapse into a "Past" section instead of cluttering what still needs attention.
  const todayStr = useMemo(() => {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }, [])
  const upcomingAppointments = filtered.filter((a) => (a.date || '') >= todayStr)
  const pastAppointments = filtered.filter((a) => (a.date || '') < todayStr)

  const stats = useMemo(() => {
    const needs = visibleAppointments.filter((a) => needsResponse(a.status)).length
    const accepted = visibleAppointments.filter((a) =>
      ['accepted', 'confirmed', 'completed'].includes(a.status)
    ).length
    return { total: visibleAppointments.length, needs, accepted }
  }, [visibleAppointments])

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!form.name.trim() || !form.date.trim() || !form.time.trim()) return
    setSubmitting(true)
    try {
      await api.post('/api/appointments', {
        name: form.name.trim(),
        email: form.email.trim() || '',
        phone: form.phone.trim() || '',
        date: form.date,
        time: form.time,
        reason: form.reason.trim() || '—',
        ...(form.staff_id.trim() ? { staff_id: form.staff_id.trim() } : {}),
      })
      setForm({ name: '', email: '', phone: '', date: '', time: '', reason: '', staff_id: '' })
      setShowForm(false)
      await fetchAppointments()
    } catch (e) {
      console.error('Failed to create appointment', e)
    } finally {
      setSubmitting(false)
    }
  }

  const handleAccept = async (id: number) => {
    setUpdatingId(id)
    setAcceptRejectMsg(null)
    try {
      const res = await api.post(`/api/appointments/${id}/accept`)
      await fetchAppointments()
      const sent = res?.data?.confirmation_sms_sent !== false
      setAcceptRejectMsg(
        sent
          ? { id, msg: 'Accepted — confirmation text sent.', ok: true }
          : { id, msg: "Accepted, but the confirmation text didn't send. Call the customer to confirm.", ok: false }
      )
      setTimeout(() => setAcceptRejectMsg(null), sent ? 4000 : 8000)
    } catch (e) {
      console.error('Failed to accept', e)
      setAcceptRejectMsg({ id, msg: apiDetail(e), ok: false })
      setTimeout(() => setAcceptRejectMsg(null), 5000)
    } finally {
      setUpdatingId(null)
    }
  }

  const confirmOwnerAction = async () => {
    if (ownerActionModal == null || !ownerActionReason.trim()) return
    const { id, kind } = ownerActionModal
    setUpdatingId(id)
    setAcceptRejectMsg(null)
    try {
      const res =
        kind === 'cancel'
          ? await api.post(`/api/appointments/${id}/cancel`, { reason: ownerActionReason.trim() })
          : await api.post(`/api/appointments/${id}/reject`, { reason: ownerActionReason.trim() })
      await fetchAppointments()
      const sent = res?.data?.confirmation_sms_sent !== false
      const verb = kind === 'cancel' ? 'Cancelled' : 'Declined'
      setAcceptRejectMsg(
        sent
          ? { id, msg: `${verb} — customer notified.`, ok: true }
          : { id, msg: `${verb}, but the text didn't send. Call the customer to let them know.`, ok: false }
      )
      setOwnerActionModal(null)
      setOwnerActionReason('')
      setDeclinePreview(null)
      setTimeout(() => setAcceptRejectMsg(null), sent ? 4000 : 8000)
    } catch (e) {
      console.error(`Failed to ${kind}`, e)
      setAcceptRejectMsg({ id, msg: apiDetail(e), ok: false })
      setTimeout(() => setAcceptRejectMsg(null), 5000)
    } finally {
      setUpdatingId(null)
    }
  }

  const handleDelete = async (id: number) => {
    if (typeof window !== 'undefined' && !window.confirm('Remove this appointment? This only clears it from your dashboard and cannot be undone.')) {
      return
    }
    setUpdatingId(id)
    try {
      await api.delete(`/api/appointments/${id}`)
      await fetchAppointments()
    } catch (e) {
      console.error('Failed to delete appointment', e)
      setAcceptRejectMsg({ id, msg: apiDetail(e), ok: false })
      setTimeout(() => setAcceptRejectMsg(null), 5000)
    } finally {
      setUpdatingId(null)
    }
  }

  const handleClearMany = async (ids: number[], label: string) => {
    if (!ids.length) return
    if (typeof window !== 'undefined' && !window.confirm(`Remove ${ids.length} ${label} appointment${ids.length === 1 ? '' : 's'}? This can't be undone.`)) {
      return
    }
    setClearing(true)
    try {
      await api.post('/api/appointments/bulk-delete', { ids })
      await fetchAppointments()
    } catch (e) {
      console.error('Failed to clear appointments', e)
    } finally {
      setClearing(false)
    }
  }

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-6xl space-y-4 p-2">
        {[0, 1, 2].map((i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0.4 }}
            animate={{ opacity: [0.4, 0.7, 0.4] }}
            transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.15 }}
            className="h-36 rounded-2xl border border-white/10 bg-zinc-800/50"
          />
        ))}
      </div>
    )
  }

  return (
    <div
      className={`mx-auto w-full space-y-6 text-zinc-100 ${
        view === 'calendar' ? 'max-w-6xl' : 'max-w-5xl'
      }`}
    >
      <motion.div
        initial={reduceMotion ? false : { opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        className="relative overflow-hidden rounded-3xl border border-white/10 bg-zinc-900/80 p-6 shadow-2xl shadow-cyan-500/5 backdrop-blur-xl"
      >
        <div
          className="pointer-events-none absolute inset-0 bg-gradient-to-br from-cyan-500/5 via-transparent to-indigo-600/10"
          aria-hidden
        />

        <div className="relative mb-6 flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <motion.div
              animate={reduceMotion ? undefined : { rotate: [0, 8, -8, 0] }}
              transition={{ duration: 4, repeat: Infinity, ease: 'easeInOut' }}
              className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-cyan-500 to-indigo-600 shadow-lg shadow-cyan-500/30"
            >
              <Calendar className="h-6 w-6 text-white" />
            </motion.div>
            <div>
              <h2 className="text-xl font-bold text-white">Appointments</h2>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="flex rounded-xl border border-white/10 bg-zinc-950/60 p-1">
              {(['list', 'calendar'] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  className={`relative flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                    view === v ? 'text-white' : 'text-zinc-400 hover:text-zinc-200'
                  }`}
                >
                  {view === v && (
                    <motion.span
                      layoutId="apt-view-pill"
                      className="absolute inset-0 rounded-lg bg-gradient-to-r from-cyan-600/80 to-indigo-600/80"
                      transition={{ type: 'spring', stiffness: 400, damping: 32 }}
                    />
                  )}
                  <span className="relative z-10 flex items-center gap-1.5">
                    {v === 'list' ? <List className="h-4 w-4" /> : <LayoutGrid className="h-4 w-4" />}
                    {v === 'list' ? 'List' : 'Calendar'}
                  </span>
                </button>
              ))}
            </div>
            <motion.button
              type="button"
              whileTap={reduceMotion ? undefined : { scale: 0.96 }}
              onClick={() => setShowForm((s) => !s)}
              className="flex items-center gap-2 rounded-xl bg-gradient-to-r from-cyan-500 to-indigo-600 px-4 py-2.5 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20"
            >
              <Plus className="h-4 w-4" />
              New
            </motion.button>
            <motion.button
              type="button"
              whileTap={reduceMotion ? undefined : { scale: 0.92 }}
              onClick={() => {
                const next = !soundOn
                setSoundOn(next)
                setChimeEnabled(next)
              }}
              className={`rounded-xl border border-white/10 p-2.5 hover:bg-white/5 ${
                soundOn ? 'text-cyan-300' : 'text-zinc-500'
              }`}
              aria-pressed={soundOn}
              title={
                soundOn
                  ? 'Sound on — plays a chime when a new request arrives'
                  : 'Sound off — new requests arrive silently'
              }
            >
              {soundOn ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
            </motion.button>
            <motion.button
              type="button"
              whileTap={reduceMotion ? undefined : { scale: 0.92, rotate: 180 }}
              onClick={() => {
                setLoading(true)
                void fetchAppointments()
              }}
              className="rounded-xl border border-white/10 p-2.5 text-zinc-300 hover:bg-white/5"
              title="Refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </motion.button>
          </div>
        </div>

        {externalBooking && (
          <div className="mb-6">
            <ImportFromZenoti
              providerName={bookingProvider}
              onImported={() => void fetchAppointments()}
            />
          </div>
        )}

        <motion.div
          variants={staggerContainer}
          initial="hidden"
          animate="visible"
          className={`relative mb-6 ${
            view === 'calendar'
              ? 'grid grid-cols-1 gap-3 sm:grid-cols-3'
              : 'grid grid-cols-3 gap-3'
          }`}
        >
          {[
            { label: 'Total', value: stats.total, icon: Inbox, color: 'from-zinc-600 to-zinc-700' },
            {
              label: 'Needs response',
              value: stats.needs,
              icon: AlertCircle,
              color: 'from-amber-500 to-orange-600',
            },
            {
              label: 'Confirmed',
              value: stats.accepted,
              icon: CheckCircle2,
              color: 'from-emerald-500 to-teal-600',
            },
          ].map((s, i) => (
            <motion.div
              key={s.label}
              custom={i}
              variants={{
                hidden: { opacity: 0, y: 12 },
                visible: (idx: number) => ({
                  opacity: 1,
                  y: 0,
                  transition: { delay: idx * 0.08 },
                }),
              }}
              className={`rounded-2xl border border-white/10 bg-gradient-to-br ${s.color} shadow-lg ${
                view === 'calendar'
                  ? 'flex items-center gap-4 px-5 py-3.5'
                  : 'p-4'
              }`}
            >
              <s.icon
                className={`shrink-0 text-white/90 ${view === 'calendar' ? 'h-8 w-8' : 'mb-2 h-5 w-5'}`}
              />
              <div className={view === 'calendar' ? 'min-w-0' : undefined}>
                <p className={`font-bold text-white ${view === 'calendar' ? 'text-2xl leading-none' : 'text-2xl'}`}>
                  {s.value}
                </p>
                <p className="text-xs font-medium text-white/80 sm:text-sm">{s.label}</p>
              </div>
            </motion.div>
          ))}
        </motion.div>

        {diagnostics?.likely_mismatch && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            className="mb-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100"
          >
            {twilioPhone ? (
              <>
                Some of your appointments may not be appearing here because of a setup issue on our
                side. Please contact support so we can fix the connection for your account.
              </>
            ) : (
              <>
                Your business phone number isn&rsquo;t linked to this account yet, so new appointments
                may not show up here. Add it under{' '}
                <strong>Settings → Phone</strong> to finish setup.
              </>
            )}
          </motion.div>
        )}

        {calendarHolds.length > 0 && (
          <motion.details
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="mb-4 rounded-xl border border-amber-500/20 bg-amber-500/5 px-4 py-3 text-sm text-amber-100"
          >
            <summary className="cursor-pointer font-semibold text-amber-200">
              AI-blocked times ({calendarHolds.length})
            </summary>
            <ul className="mt-2 list-disc pl-5 space-y-0.5 text-amber-100/90">
              {calendarHolds.slice(0, 6).map((h, i) => (
                <li key={`${h.date}-${h.time}-${i}`}>
                  {h.date} · {h.name || 'Held slot'} ({h.status})
                </li>
              ))}
            </ul>
          </motion.details>
        )}

        {view === 'calendar' ? (
          <AppointmentCalendar
            api={api}
            staffNameById={staffNameById}
            updatingId={updatingId}
            acceptRejectMsg={acceptRejectMsg}
            onAccept={handleAccept}
            onDecline={(id) => {
              setOwnerActionModal({ id, kind: 'reject' })
              setOwnerActionReason('')
              setDeclinePreview(null)
            }}
            onCancel={(id) => {
              setOwnerActionModal({ id, kind: 'cancel' })
              setOwnerActionReason('')
              setDeclinePreview(null)
            }}
            refreshSignal={calendarRefresh}
            onActionComplete={() => void fetchAppointments()}
          />
        ) : (
          <>
            <div className="mb-4 flex flex-wrap gap-3 rounded-2xl border border-white/10 bg-zinc-950/40 p-4">
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/30"
              >
                <option value="all">All statuses</option>
                <option value="needs_response">Needs response</option>
                <option value="accepted">Accepted</option>
              </select>
              {staffOptions.length > 0 ? (
                <select
                  value={staffFilter}
                  onChange={(e) => setStaffFilter(e.target.value)}
                  className="rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/30"
                  aria-label="Filter by stylist"
                >
                  <option value="">All stylists</option>
                  {staffOptions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              ) : null}
              <input
                type="date"
                value={dateFrom}
                onChange={(e) => setDateFrom(e.target.value)}
                className="date-input-dark rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200"
                placeholder="From"
              />
              <input
                type="date"
                value={dateTo}
                onChange={(e) => setDateTo(e.target.value)}
                className="date-input-dark rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200"
                placeholder="To"
              />
            </div>

            <AnimatePresence mode="popLayout">
              {showForm && (
                <motion.form
                  key="create-form"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  onSubmit={handleCreate}
                  className="mb-6 overflow-hidden rounded-2xl border border-cyan-500/20 bg-cyan-500/5 p-5"
                >
                  <h3 className="mb-4 flex items-center gap-2 font-semibold text-white">
                    <Sparkles className="h-4 w-4 text-cyan-400" />
                    New appointment
                  </h3>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    {[
                      { key: 'name', label: 'Name *', type: 'text', required: true },
                      { key: 'phone', label: 'Phone', type: 'tel' },
                      { key: 'email', label: 'Email', type: 'email' },
                      { key: 'date', label: 'Date *', type: 'date', required: true },
                      { key: 'time', label: 'Time *', type: 'time', required: true },
                    ].map((field) => (
                      <div key={field.key}>
                        <label className="mb-1 block text-xs font-medium text-zinc-400">
                          {field.label}
                        </label>
                        <input
                          type={field.type}
                          required={field.required}
                          value={form[field.key as keyof typeof form]}
                          onChange={(e) =>
                            setForm((f) => ({ ...f, [field.key]: e.target.value }))
                          }
                          className="w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-white focus:border-cyan-500 focus:outline-none"
                        />
                      </div>
                    ))}

                    {/* Stylist — assigning lets us flag this booking if the stylist is off
                        that day, and ties it to the right person on the calendar. */}
                    {staffOptions.length > 0 && (
                      <div>
                        <label className="mb-1 block text-xs font-medium text-zinc-400">Stylist</label>
                        <select
                          value={form.staff_id}
                          onChange={(e) => setForm((f) => ({ ...f, staff_id: e.target.value }))}
                          className="w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-white focus:border-cyan-500 focus:outline-none"
                        >
                          <option value="">Any stylist</option>
                          {staffOptions.map((s) => (
                            <option key={s.id} value={s.id}>
                              {s.name}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}

                    {/* Service — type freely or pick from the configured menu. */}
                    <div>
                      <label className="mb-1 block text-xs font-medium text-zinc-400">Service</label>
                      <input
                        type="text"
                        list="apt-service-options"
                        value={form.reason}
                        onChange={(e) => setForm((f) => ({ ...f, reason: e.target.value }))}
                        placeholder={serviceNames.length ? 'Type or pick a service' : undefined}
                        className="w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-white focus:border-cyan-500 focus:outline-none"
                      />
                      {serviceNames.length > 0 && (
                        <datalist id="apt-service-options">
                          {serviceNames.map((n) => (
                            <option key={n} value={n} />
                          ))}
                        </datalist>
                      )}
                    </div>
                  </div>

                  {createConflict && (
                    <div className="mt-4 flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-200">
                      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      <span>{createConflict} You can still save this if it&rsquo;s intentional.</span>
                    </div>
                  )}

                  <div className="mt-4 flex gap-2">
                    <button
                      type="submit"
                      disabled={submitting}
                      className="rounded-xl bg-cyan-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                    >
                      {submitting ? 'Saving…' : 'Save'}
                    </button>
                    <button
                      type="button"
                      onClick={() => setShowForm(false)}
                      className="rounded-xl border border-white/10 px-4 py-2 text-sm text-zinc-300"
                    >
                      Cancel
                    </button>
                  </div>
                </motion.form>
              )}
            </AnimatePresence>

            {upcomingAppointments.length === 0 && pastAppointments.length === 0 ? (
              <motion.div
                initial={{ opacity: 0, scale: 0.98 }}
                animate={{ opacity: 1, scale: 1 }}
                className="py-16 text-center"
              >
                <motion.div
                  animate={reduceMotion ? undefined : { y: [0, -6, 0] }}
                  transition={{ duration: 2.5, repeat: Infinity }}
                >
                  <Calendar className="mx-auto h-14 w-14 text-zinc-600" />
                </motion.div>
                <p className="mt-4 text-zinc-400">No appointments match your filters.</p>
              </motion.div>
            ) : (
              <motion.div
                layout
                className="space-y-4"
                variants={staggerContainer}
                initial="hidden"
                animate="visible"
              >
                <AnimatePresence mode="popLayout">
                  {upcomingAppointments.map((apt, i) => (
                    <AppointmentCard
                      key={apt.id}
                      apt={apt}
                      index={i}
                      reduceMotion={!!reduceMotion}
                      staffLabel={(apt.staff_id && staffNameById[apt.staff_id]) || ''}
                      updatingId={updatingId}
                      acceptRejectMsg={acceptRejectMsg}
                      onAccept={handleAccept}
                      onDecline={(id) => {
                        setOwnerActionModal({ id, kind: 'reject' })
                        setOwnerActionReason('')
                        setDeclinePreview(null)
                      }}
                      onCancel={(id) => {
                        setOwnerActionModal({ id, kind: 'cancel' })
                        setOwnerActionReason('')
                        setDeclinePreview(null)
                      }}
                      onDelete={handleDelete}
                    />
                  ))}
                </AnimatePresence>
              </motion.div>
            )}

            {pastAppointments.length > 0 && (
              <details className="mt-8 rounded-2xl border border-white/10 bg-zinc-950/40 p-4">
                <summary className="flex cursor-pointer select-none items-center justify-between gap-3 text-sm font-medium text-zinc-400 hover:text-zinc-200">
                  <span>Past ({pastAppointments.length})</span>
                  <button
                    type="button"
                    disabled={clearing}
                    onClick={(e) => {
                      e.preventDefault()
                      void handleClearMany(pastAppointments.map((a) => a.id), 'past')
                    }}
                    className="rounded-lg border border-white/10 px-2.5 py-1 text-xs font-medium text-zinc-400 hover:border-red-500/40 hover:text-red-300 disabled:opacity-50"
                  >
                    Clear all
                  </button>
                </summary>
                <motion.div layout className="mt-4 space-y-4">
                  {pastAppointments.map((apt, i) => (
                    <AppointmentCard
                      key={apt.id}
                      apt={apt}
                      index={i}
                      reduceMotion={!!reduceMotion}
                      staffLabel={(apt.staff_id && staffNameById[apt.staff_id]) || ''}
                      updatingId={updatingId}
                      acceptRejectMsg={acceptRejectMsg}
                      onAccept={handleAccept}
                      onDecline={(id) => {
                        setOwnerActionModal({ id, kind: 'reject' })
                        setOwnerActionReason('')
                        setDeclinePreview(null)
                      }}
                      onCancel={(id) => {
                        setOwnerActionModal({ id, kind: 'cancel' })
                        setOwnerActionReason('')
                        setDeclinePreview(null)
                      }}
                      onDelete={handleDelete}
                    />
                  ))}
                </motion.div>
              </details>
            )}

            {archivedAppointments.length > 0 && (
              <details className="mt-8 rounded-2xl border border-white/10 bg-zinc-950/40 p-4">
                <summary className="flex cursor-pointer select-none items-center justify-between gap-3 text-sm font-medium text-zinc-400 hover:text-zinc-200">
                  <span>Cancelled &amp; declined ({archivedAppointments.length})</span>
                  <button
                    type="button"
                    disabled={clearing}
                    onClick={(e) => {
                      e.preventDefault()
                      void handleClearMany(archivedAppointments.map((a) => a.id), 'cancelled')
                    }}
                    className="rounded-lg border border-white/10 px-2.5 py-1 text-xs font-medium text-zinc-400 hover:border-red-500/40 hover:text-red-300 disabled:opacity-50"
                  >
                    Clear all
                  </button>
                </summary>
                <motion.div layout className="mt-4 space-y-4">
                  {archivedAppointments.map((apt, i) => (
                    <AppointmentCard
                      key={apt.id}
                      apt={apt}
                      index={i}
                      reduceMotion={!!reduceMotion}
                      staffLabel={(apt.staff_id && staffNameById[apt.staff_id]) || ''}
                      updatingId={updatingId}
                      acceptRejectMsg={acceptRejectMsg}
                      onAccept={handleAccept}
                      onDecline={(id) => {
                        setOwnerActionModal({ id, kind: 'reject' })
                        setOwnerActionReason('')
                        setDeclinePreview(null)
                      }}
                      onCancel={(id) => {
                        setOwnerActionModal({ id, kind: 'cancel' })
                        setOwnerActionReason('')
                        setDeclinePreview(null)
                      }}
                      onDelete={handleDelete}
                    />
                  ))}
                </motion.div>
              </details>
            )}
          </>
        )}
      </motion.div>

      <AnimatePresence>
        {ownerActionModal != null && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          >
            <motion.div
              initial={reduceMotion ? false : { opacity: 0, scale: 0.9, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={reduceMotion ? undefined : { opacity: 0, scale: 0.95 }}
              transition={{ type: 'spring', stiffness: 380, damping: 28 }}
              className="w-full max-w-md rounded-2xl border border-white/10 bg-zinc-900 p-6 shadow-2xl"
            >
              <h3 className="text-lg font-semibold text-white">
                {ownerActionModal.kind === 'cancel'
                  ? 'Cancel appointment'
                  : 'Decline appointment'}
              </h3>
              <p className="mt-1 text-sm text-zinc-400">
                {ownerActionModal.kind === 'cancel'
                  ? 'This frees the time slot and texts the customer.'
                  : "We'll polish this and text the customer."}
              </p>
              <textarea
                className="mt-4 w-full min-h-[100px] rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm text-white focus:border-cyan-500 focus:outline-none"
                placeholder={
                  ownerActionModal.kind === 'cancel'
                    ? 'e.g. Need to clear test bookings — please rebook online'
                    : 'e.g. That time is full — can we do 3 PM?'
                }
                value={ownerActionReason}
                onChange={(e) => {
                  setOwnerActionReason(e.target.value)
                  setDeclinePreview(null)
                }}
              />
              {declinePreview != null && (
                <motion.div
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  className="mt-3 rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm text-zinc-300"
                >
                  <span className="text-xs font-medium text-zinc-500">Preview</span>
                  <p className="mt-1 whitespace-pre-wrap">{declinePreview}</p>
                </motion.div>
              )}
              <div className="mt-4 flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className="rounded-xl border border-white/10 px-4 py-2 text-sm text-zinc-300"
                  onClick={() => {
                    setOwnerActionModal(null)
                    setOwnerActionReason('')
                    setDeclinePreview(null)
                  }}
                >
                  Close
                </button>
                <button
                  type="button"
                  disabled={!ownerActionReason.trim() || previewLoading}
                  className="rounded-xl border border-cyan-500/40 px-4 py-2 text-sm text-cyan-300 disabled:opacity-50"
                  onClick={async () => {
                    if (!ownerActionReason.trim() || ownerActionModal == null) return
                    setPreviewLoading(true)
                    try {
                      const res = await api.post('/api/appointments/preview-decline-sms', {
                        reason: ownerActionReason.trim(),
                        appointment_id: ownerActionModal.id,
                        event: ownerActionModal.kind,
                      })
                      setDeclinePreview(String(res.data?.polished_message || '').trim() || null)
                    } catch {
                      setDeclinePreview('Could not generate preview.')
                    } finally {
                      setPreviewLoading(false)
                    }
                  }}
                >
                  {previewLoading ? '…' : 'Preview SMS'}
                </button>
                <motion.button
                  type="button"
                  whileTap={{ scale: 0.96 }}
                  disabled={!ownerActionReason.trim() || updatingId === ownerActionModal.id}
                  onClick={() => void confirmOwnerAction()}
                  className="rounded-xl bg-red-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                >
                  {ownerActionModal.kind === 'cancel' ? 'Send cancellation' : 'Send decline'}
                </motion.button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
