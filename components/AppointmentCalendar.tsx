'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AxiosInstance } from 'axios'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { CalendarDays, Filter, Loader2 } from 'lucide-react'
import FullCalendar from '@fullcalendar/react'
import dayGridPlugin from '@fullcalendar/daygrid'
import timeGridPlugin from '@fullcalendar/timegrid'
import interactionPlugin from '@fullcalendar/interaction'
import type { EventClickArg, EventContentArg } from '@fullcalendar/core'
import { AppointmentDetailModal } from '@/components/appointments/AppointmentDetailModal'
import { StylistDayView, type StylistDayItem } from '@/components/appointments/StylistDayView'
import type { Appointment } from '@/components/appointments/types'
import {
  calendarHeightFromSlotBounds,
  calendarSlotBoundsForDay,
  calendarSlotBoundsForWeek,
  defaultWeeklySchedule,
  jsDayToScheduleIndex,
  parseHoursToWeekly,
  type CalendarSlotBounds,
  type WeeklySchedule,
} from '@/lib/businessHours'
import { localDayString, shiftDay } from '@/lib/localDay'
import './appointments/calendar-theme.css'

const LEGEND = [
  { label: 'Needs approval', color: '#d97706' },
  { label: 'Awaiting text confirm', color: '#0ea5e9' },
  { label: 'Confirmed', color: '#16a34a' },
] as const

function eventColor(status: string): string {
  if (status === 'accepted' || status === 'confirmed' || status === 'completed') return '#16a34a'
  if (status === 'pending_review') return '#d97706'
  if (status === 'pending_customer') return '#0ea5e9'
  return '#6366f1'
}

function EventContent({ arg }: { arg: EventContentArg }) {
  const time = arg.timeText
  const title = arg.event.title
  return (
    <div className="flex h-full min-h-[2.25rem] flex-col justify-center overflow-hidden px-1.5 py-1 leading-snug">
      {time ? <span className="text-[11px] font-bold opacity-90">{time}</span> : null}
      <span className="truncate text-xs font-semibold">{title}</span>
    </div>
  )
}

function addMinutesToIsoLocal(isoStart: string, minutes: number): string {
  const d = new Date(isoStart)
  if (Number.isNaN(d.getTime())) return isoStart
  d.setMinutes(d.getMinutes() + minutes)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

type CalendarEvent = {
  id: string
  title: string
  start: string
  end: string
  backgroundColor?: string
  borderColor?: string
  extendedProps: { appointment: Appointment }
}

export default function AppointmentCalendar({
  api,
  staffNameById = {},
  updatingId = null,
  acceptRejectMsg = null,
  onAccept,
  onDecline,
  onCancel,
  onActionComplete,
  refreshSignal = 0,
}: {
  api: AxiosInstance
  staffNameById?: Record<string, string>
  updatingId?: number | null
  acceptRejectMsg?: { id: number; msg: string; ok?: boolean } | null
  onAccept: (id: number) => Promise<void>
  onDecline: (id: number) => void
  onCancel: (id: number) => void
  onActionComplete?: () => void
  refreshSignal?: number
}) {
  const reduceMotion = useReducedMotion()
  const [events, setEvents] = useState<CalendarEvent[]>([])
  const [staffList, setStaffList] = useState<{ id: string; name: string }[]>([])
  const [staffFilter, setStaffFilter] = useState('')
  // "By stylist" is a different question from the calendar's — not "what is on at 3pm"
  // but "who is free at 3pm" — so it gets its own day rather than sharing the grid's range.
  const [layout, setLayout] = useState<'calendar' | 'stylist'>('calendar')
  const [stylistDay, setStylistDay] = useState(() => localDayString())
  const [loading, setLoading] = useState(true)
  const [selectedApt, setSelectedApt] = useState<Appointment | null>(null)
  const visibleRangeRef = useRef({ from: '', to: '' })
  const hoursScheduleRef = useRef<WeeklySchedule>(defaultWeeklySchedule())
  const [slotBounds, setSlotBounds] = useState<CalendarSlotBounds>(() =>
    calendarSlotBoundsForWeek(defaultWeeklySchedule())
  )
  const calendarHeight = useMemo(() => calendarHeightFromSlotBounds(slotBounds), [slotBounds])

  const applySlotBoundsForView = useCallback((viewType: string, rangeStart: Date) => {
    const schedule = hoursScheduleRef.current
    if (viewType === 'timeGridDay') {
      setSlotBounds(calendarSlotBoundsForDay(schedule, jsDayToScheduleIndex(rangeStart.getDay())))
    } else if (viewType === 'timeGridWeek') {
      setSlotBounds(calendarSlotBoundsForWeek(schedule))
    }
  }, [])

  useEffect(() => {
    api.get('/api/business-info').then((r) => {
      const st = (r.data?.staff || []) as { id?: string; name?: string }[]
      setStaffList(
        st
          .filter((s) => s.id && s.name)
          .map((s) => ({ id: s.id as string, name: s.name as string }))
      )
      const { schedule } = parseHoursToWeekly((r.data?.hours as string) || '')
      hoursScheduleRef.current = schedule
      setSlotBounds(calendarSlotBoundsForWeek(schedule))
    })
  }, [api])

  const load = useCallback(
    (from: string, to: string) => {
      setLoading(true)
      const params: Record<string, string> = { date_from: from, date_to: to }
      if (staffFilter) params.staff_id = staffFilter
      api
        .get('/api/appointments/calendar', { params })
        .then((r) => {
          const list = (r.data?.events || []) as (Appointment & { duration_minutes?: number })[]
          setEvents(
            list.map((a) => {
              const raw = (a.time || '09:00').trim()
              const t = raw.length >= 5 ? raw.slice(0, 5) : '09:00'
              const color = eventColor(a.status)
              const reason = (a.reason || '').trim()
              const subtitle = reason && reason !== '—' ? reason : 'Booking'
              const duration = Math.max(5, Math.min(Number(a.duration_minutes) || 30, 480))
              const start = `${a.date}T${t}`
              const appointment: Appointment = {
                id: a.id,
                name: a.name,
                email: a.email || '',
                phone: a.phone || '',
                date: a.date,
                time: a.time || t,
                reason: a.reason || '',
                status: a.status,
                created_at: a.created_at || '',
                source: a.source,
                staff_id: a.staff_id,
                owner_decline_reason: a.owner_decline_reason,
              }
              return {
                id: String(a.id),
                title: `${a.name} — ${subtitle}`,
                start,
                end: addMinutesToIsoLocal(start, duration),
                backgroundColor: color,
                borderColor: color,
                extendedProps: { appointment },
              }
            })
          )
        })
        .catch(() => setEvents([]))
        .finally(() => setLoading(false))
    },
    [api, staffFilter]
  )

  const minutesOf = (iso: string) => {
    const t = iso.slice(11, 16)
    const [h, m] = t.split(':').map((n) => parseInt(n, 10))
    return (h || 0) * 60 + (m || 0)
  }

  const stylistItems: StylistDayItem[] = useMemo(
    () =>
      events
        .filter((e) => e.start.slice(0, 10) === stylistDay)
        .map((e) => ({
          appointment: e.extendedProps.appointment,
          startMinutes: minutesOf(e.start),
          endMinutes: minutesOf(e.end),
          color: e.backgroundColor || '#6366f1',
        })),
    [events, stylistDay]
  )

  const stylistBounds = useMemo(() => {
    const toMin = (hhmmss: string) => {
      const [h, m] = hhmmss.split(':').map((n) => parseInt(n, 10))
      return (h || 0) * 60 + (m || 0)
    }
    let lo = toMin(slotBounds.slotMinTime)
    let hi = toMin(slotBounds.slotMaxTime)
    // Never hide an appointment because it sits outside opening hours — an imported
    // day can legitimately run past close, and a silently clipped row is worse than
    // a slightly taller grid.
    for (const it of stylistItems) {
      lo = Math.min(lo, Math.floor(it.startMinutes / 60) * 60)
      hi = Math.max(hi, Math.ceil(it.endMinutes / 60) * 60)
    }
    return { lo, hi }
  }, [slotBounds, stylistItems])

  const reloadVisibleRange = useCallback(() => {
    const { from, to } = visibleRangeRef.current
    if (from && to) load(from, to)
  }, [load])

  const handleEventClick = useCallback((arg: EventClickArg) => {
    const apt = arg.event.extendedProps?.appointment as Appointment | undefined
    if (apt?.id) setSelectedApt(apt)
  }, [])

  useEffect(() => {
    if (refreshSignal > 0) reloadVisibleRange()
  }, [refreshSignal, reloadVisibleRange])

  useEffect(() => {
    if (layout === 'stylist') {
      visibleRangeRef.current = { from: stylistDay, to: stylistDay }
      load(stylistDay, stylistDay)
    }
  }, [layout, stylistDay, load])

  const handleAccept = useCallback(
    async (id: number) => {
      await onAccept(id)
      setSelectedApt(null)
      reloadVisibleRange()
      onActionComplete?.()
    },
    [onAccept, onActionComplete, reloadVisibleRange]
  )

  const handleDecline = useCallback(
    (id: number) => {
      setSelectedApt(null)
      onDecline(id)
    },
    [onDecline]
  )

  const handleCancel = useCallback(
    (id: number) => {
      setSelectedApt(null)
      onCancel(id)
    },
    [onCancel]
  )

  return (
    <motion.div
      initial={reduceMotion ? false : { opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      className="appointment-calendar space-y-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-2.5 text-zinc-300">
          <CalendarDays className="h-5 w-5 text-cyan-400" />
          <span className="text-base font-semibold text-white">Schedule</span>
          {loading && <Loader2 className="h-4 w-4 animate-spin text-cyan-400" aria-label="Loading" />}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex rounded-lg border border-white/10 bg-zinc-900 p-0.5">
            {(['calendar', 'stylist'] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setLayout(v)}
                className={`rounded-md px-3 py-1.5 text-sm font-medium motion-safe-transition ${
                  layout === v ? 'bg-cyan-500/15 text-cyan-200' : 'text-zinc-400 hover:text-zinc-200'
                }`}
              >
                {v === 'calendar' ? 'Calendar' : 'By stylist'}
              </button>
            ))}
          </div>
        {staffList.length > 0 && layout === 'calendar' ? (
          <div className="flex flex-wrap items-center gap-2">
            <Filter className="h-4 w-4 text-zinc-500" aria-hidden />
            <label htmlFor="cal-staff-filter" className="text-sm text-zinc-400">
              Stylist
            </label>
            <select
              id="cal-staff-filter"
              value={staffFilter}
              onChange={(e) => setStaffFilter(e.target.value)}
              className="min-w-[12rem] rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/30"
            >
              <option value="">All stylists</option>
              {staffList.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        </div>
      </div>

      <div className="relative overflow-hidden rounded-2xl border border-white/10 bg-zinc-950/80 p-4 shadow-inner shadow-black/20 sm:p-5">
        <div
          className="pointer-events-none absolute inset-0 bg-gradient-to-br from-cyan-500/5 via-transparent to-indigo-600/5"
          aria-hidden
        />
        {layout === 'stylist' ? (
          <div className="relative">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setStylistDay((d) => shiftDay(d, -1))}
                className="rounded-lg border border-white/10 px-2.5 py-1.5 text-sm text-zinc-300 hover:bg-white/5"
              >
                ‹
              </button>
              <input
                type="date"
                value={stylistDay}
                onChange={(e) => setStylistDay(e.target.value || stylistDay)}
                className="rounded-lg border border-white/10 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              />
              <button
                type="button"
                onClick={() => setStylistDay((d) => shiftDay(d, 1))}
                className="rounded-lg border border-white/10 px-2.5 py-1.5 text-sm text-zinc-300 hover:bg-white/5"
              >
                ›
              </button>
              <button
                type="button"
                onClick={() => setStylistDay(localDayString())}
                className="rounded-lg border border-white/10 px-3 py-1.5 text-sm text-zinc-300 hover:bg-white/5"
              >
                Today
              </button>
            </div>
            <StylistDayView
              items={stylistItems}
              staff={staffList}
              dayStartMinutes={stylistBounds.lo}
              dayEndMinutes={stylistBounds.hi}
              onSelect={(apt) => setSelectedApt(apt)}
            />
          </div>
        ) : (
        <div className="relative appointment-calendar-grid">
          <FullCalendar
            plugins={[dayGridPlugin, timeGridPlugin, interactionPlugin]}
            initialView="timeGridWeek"
            headerToolbar={{
              left: 'prev,next today',
              center: 'title',
              right: 'dayGridMonth,timeGridWeek,timeGridDay',
            }}
            buttonText={{
              today: 'Today',
              month: 'Month',
              week: 'Week',
              day: 'Day',
            }}
            slotMinTime={slotBounds.slotMinTime}
            slotMaxTime={slotBounds.slotMaxTime}
            scrollTime={slotBounds.scrollTime}
            slotDuration="00:30:00"
            allDaySlot={false}
            nowIndicator
            height={calendarHeight}
            stickyHeaderDates
            dayHeaderFormat={{ weekday: 'short', month: 'numeric', day: 'numeric' }}
            dayMaxEvents={4}
            events={events}
            eventClick={handleEventClick}
            eventContent={(arg) => <EventContent arg={arg} />}
            eventClassNames={() => ['appointment-cal-event']}
            slotLabelFormat={{
              hour: 'numeric',
              minute: '2-digit',
              meridiem: 'short',
            }}
            eventTimeFormat={{
              hour: 'numeric',
              minute: '2-digit',
              meridiem: 'short',
            }}
            datesSet={(arg) => {
              const from = arg.startStr.slice(0, 10)
              const endDay = new Date(arg.end)
              endDay.setMilliseconds(endDay.getMilliseconds() - 1)
              const to = localDayString(endDay)
              visibleRangeRef.current = { from, to }
              applySlotBoundsForView(arg.view.type, arg.start)
              load(from, to)
            }}
          />
        </div>
        )}
      </div>

      <div className="flex flex-wrap gap-3">
        {LEGEND.map((item) => (
          <span
            key={item.label}
            className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-zinc-900/80 px-3 py-1 text-xs text-zinc-300"
          >
            <span
              className="h-2.5 w-2.5 rounded-full shadow-sm"
              style={{ backgroundColor: item.color, boxShadow: `0 0 8px ${item.color}66` }}
            />
            {item.label}
          </span>
        ))}
      </div>

      <AnimatePresence>
        {selectedApt ? (
          <AppointmentDetailModal
            key={selectedApt.id}
            apt={selectedApt}
            staffLabel={(selectedApt.staff_id && staffNameById[selectedApt.staff_id]) || ''}
            reduceMotion={!!reduceMotion}
            updatingId={updatingId}
            acceptRejectMsg={acceptRejectMsg}
            onClose={() => setSelectedApt(null)}
            onAccept={handleAccept}
            onDecline={handleDecline}
            onCancel={handleCancel}
          />
        ) : null}
      </AnimatePresence>
    </motion.div>
  )
}
