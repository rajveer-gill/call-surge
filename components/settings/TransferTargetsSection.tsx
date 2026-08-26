'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import type { AxiosInstance } from 'axios'
import {
  ArrowRightLeft,
  ChevronRight,
  Pencil,
  Phone,
  Plus,
  Sparkles,
  Trash2,
  UserPlus,
  X,
} from 'lucide-react'
import { fadeUpChild, staggerContainer } from '@/components/motion'
import type { StaffRow } from '@/components/settings/StaffMembersSection'

export type TransferRow = {
  id: string
  staff_id: string | null
  name: string
  phone: string
}

export function normalizeTransferFromApi(raw: unknown): TransferRow[] {
  if (!Array.isArray(raw)) return []
  return raw.map((item) => {
    const o = item as Record<string, unknown>
    const id = typeof o.id === 'string' && o.id.trim() ? o.id.trim() : crypto.randomUUID()
    const staffIdRaw = o.staff_id
    const staff_id =
      typeof staffIdRaw === 'string' && staffIdRaw.trim() ? staffIdRaw.trim() : null
    return {
      id,
      staff_id,
      name: String(o.name ?? '').trim(),
      phone: String(o.phone ?? '').trim(),
    }
  })
}

function maskPhone(phone: string): string {
  const d = phone.replace(/\D/g, '')
  if (d.length < 4) return phone ? '••••' : ''
  return `••••${d.slice(-4)}`
}

type Notify = (msg: { type: 'success' | 'error'; text: string } | null) => void

type AddMode = 'pick_staff' | 'custom'

export function TransferTargetsSection({
  transfers,
  staff,
  transferMax,
  onTransfersChange,
  api,
  onNotify,
  onAfterSave,
}: {
  transfers: TransferRow[]
  staff: StaffRow[]
  transferMax: number | null
  onTransfersChange: (next: TransferRow[]) => void
  api: AxiosInstance
  onNotify: Notify
  onAfterSave?: () => void
}) {
  const reduceMotion = useReducedMotion()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const firstFieldRef = useRef<HTMLInputElement>(null)

  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'add' | 'edit'>('add')
  const [addMode, setAddMode] = useState<AddMode>('pick_staff')
  const [editId, setEditId] = useState<string | null>(null)
  const [draft, setDraft] = useState({ staff_id: '', name: '', phone: '' })
  const [draftError, setDraftError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [pulseLimit, setPulseLimit] = useState(false)

  const cap = transferMax ?? 999
  const atLimit = transfers.length >= cap
  const canAdd = !atLimit

  const linkedStaffIds = useMemo(
    () => new Set(transfers.map((t) => t.staff_id).filter(Boolean) as string[]),
    [transfers],
  )

  const staffAvailableToLink = useMemo(
    () => staff.filter((s) => s.id && !linkedStaffIds.has(s.id)),
    [staff, linkedStaffIds],
  )

  const motionProps = reduceMotion
    ? { initial: false, animate: { opacity: 1 }, exit: { opacity: 1 } }
    : {
        initial: { opacity: 0, y: 18, scale: 0.96 },
        animate: { opacity: 1, y: 0, scale: 1 },
        exit: { opacity: 0, y: 14, scale: 0.97 },
        transition: { type: 'spring' as const, stiffness: 400, damping: 28 },
      }

  const openModal = useCallback(
    (opts: { mode: 'add' | 'edit'; row?: TransferRow; addMode?: AddMode }) => {
      setDraftError(null)
      setMode(opts.mode)
      if (opts.mode === 'add') {
        setEditId(null)
        const am = opts.addMode ?? (staffAvailableToLink.length > 0 ? 'pick_staff' : 'custom')
        setAddMode(am)
        if (am === 'pick_staff' && staffAvailableToLink[0]) {
          const s = staffAvailableToLink[0]
          setDraft({ staff_id: s.id, name: s.name, phone: s.phone })
        } else {
          setDraft({ staff_id: '', name: '', phone: '' })
        }
      } else if (opts.row) {
        setEditId(opts.row.id)
        setAddMode(opts.row.staff_id ? 'pick_staff' : 'custom')
        setDraft({
          staff_id: opts.row.staff_id || '',
          name: opts.row.name,
          phone: opts.row.phone,
        })
      }
      setOpen(true)
    },
    [staffAvailableToLink],
  )

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
      const t = window.setTimeout(() => firstFieldRef.current?.focus(), reduceMotion ? 0 : 90)
      return () => window.clearTimeout(t)
    }
    if (el.open) el.close()
    return undefined
  }, [open, reduceMotion])

  useEffect(() => {
    if (!atLimit || reduceMotion) return
    setPulseLimit(true)
    const t = window.setTimeout(() => setPulseLimit(false), 1200)
    return () => window.clearTimeout(t)
  }, [atLimit, reduceMotion])

  const parseApiError = (e: unknown): string => {
    const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    if (typeof d === 'string') return d
    return 'Could not save transfer destinations'
  }

  const persist = async (next: TransferRow[]) => {
    const { data } = await api.patch<Record<string, unknown>>('/api/business-info', {
      transfer_targets: next.map((t) => ({
        id: t.id,
        staff_id: t.staff_id || undefined,
        name: t.name,
        phone: t.phone,
      })),
    })
    const normalized = normalizeTransferFromApi(data.transfer_targets)
    onTransfersChange(normalized)
    onNotify({ type: 'success', text: 'Call transfers updated.' })
    onAfterSave?.()
    closeModal()
  }

  const saveDraft = async () => {
    let name = draft.name.trim()
    let phone = draft.phone.trim()
    const staff_id = draft.staff_id.trim() || null

    if (staff_id) {
      const linked = staff.find((s) => s.id === staff_id)
      if (!linked) {
        setDraftError('Selected team member is no longer on your roster.')
        return
      }
      if (!name) name = linked.name.trim()
      if (linked.phone.trim()) phone = linked.phone.trim()
    }

    if (!name) {
      setDraftError('Name is required so callers can ask for this person.')
      return
    }
    if (!phone) {
      setDraftError('A valid phone number is required for live call transfers.')
      return
    }

    setDraftError(null)
    setSaving(true)
    try {
      const next: TransferRow[] =
        mode === 'add'
          ? [
              ...transfers,
              {
                id: crypto.randomUUID(),
                staff_id,
                name,
                phone,
              },
            ]
          : transfers.map((t) =>
              t.id === editId ? { ...t, staff_id, name, phone } : t,
            )
      await persist(next)
    } catch (e) {
      onNotify({ type: 'error', text: parseApiError(e) })
    } finally {
      setSaving(false)
    }
  }

  const confirmDelete = async (row: TransferRow) => {
    if (!window.confirm(`Remove ${row.name || 'this transfer destination'}?`)) return
    setDeleting(true)
    try {
      await persist(transfers.filter((t) => t.id !== row.id))
    } catch (e) {
      onNotify({ type: 'error', text: parseApiError(e) })
    } finally {
      setDeleting(false)
    }
  }

  const onPickStaff = (staffId: string) => {
    const s = staff.find((x) => x.id === staffId)
    if (!s) return
    setDraft({ staff_id: staffId, name: s.name, phone: s.phone })
  }

  return (
    <div>
      <motion.p
        className="text-xs text-violet-800 mb-3 font-medium inline-flex items-center gap-2 rounded-full bg-violet-50 px-3 py-1 border border-violet-100"
        layout
      >
        <Sparkles className="h-3.5 w-3.5" />
        Plan usage: {transfers.length}/{cap} transfer {transfers.length === 1 ? 'slot' : 'slots'}
      </motion.p>

      <motion.ul
        className="space-y-2 mb-4"
        variants={reduceMotion ? undefined : staggerContainer}
        initial="hidden"
        animate="visible"
      >
        <AnimatePresence mode="popLayout">
          {transfers.length === 0 ? (
            <motion.li
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="rounded-xl border border-dashed border-violet-200 bg-white/60 px-4 py-6 text-center text-sm text-gray-500"
            >
              No transfer destinations yet. Add someone from your team or enter a custom number.
            </motion.li>
          ) : (
            transfers.map((t, i) => (
              <motion.li
                key={t.id}
                layout
                variants={fadeUpChild}
                custom={i}
                exit={{ opacity: 0, x: -20, scale: 0.95 }}
                className="list-none"
              >
                <motion.div
                  className="group flex rounded-xl border border-violet-100 bg-white/95 shadow-sm transition-shadow hover:shadow-md hover:border-violet-300"
                  whileHover={reduceMotion ? {} : { scale: 1.01 }}
                  transition={{ type: 'spring', stiffness: 400, damping: 28 }}
                >
                  <button
                    type="button"
                    onClick={() => openModal({ mode: 'edit', row: t })}
                    className="flex flex-1 min-w-0 items-center gap-3 px-3 py-3 text-left rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-500"
                  >
                    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-violet-100 text-violet-700">
                      <ArrowRightLeft className="h-5 w-5" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="font-semibold text-gray-900 block truncate">{t.name}</span>
                      <span className="flex flex-wrap items-center gap-2 text-xs text-gray-600 mt-0.5">
                        <span className="inline-flex items-center gap-1">
                          <Phone className="w-3.5 h-3.5" />
                          {maskPhone(t.phone)}
                        </span>
                        {t.staff_id ? (
                          <span className="rounded-full bg-emerald-50 text-emerald-800 px-2 py-0.5 font-medium">
                            Linked to roster
                          </span>
                        ) : (
                          <span className="rounded-full bg-gray-100 text-gray-600 px-2 py-0.5">Custom line</span>
                        )}
                      </span>
                    </span>
                    <ChevronRight className="w-5 h-5 text-gray-400 group-hover:text-violet-600 shrink-0" />
                  </button>
                  <motion.div className="flex items-center pr-1" layout>
                    <button
                      type="button"
                      onClick={() => openModal({ mode: 'edit', row: t })}
                      className="p-2 rounded-lg text-gray-600 hover:bg-violet-50"
                      title="Edit"
                    >
                      <Pencil className="w-4 h-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => confirmDelete(t)}
                      disabled={deleting}
                      className="p-2 rounded-lg text-red-600 hover:bg-red-50 disabled:opacity-40"
                      title="Remove"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </motion.div>
                </motion.div>
              </motion.li>
            ))
          )}
        </AnimatePresence>
      </motion.ul>

      <div className="flex flex-wrap gap-2">
        <motion.button
          type="button"
          onClick={() => openModal({ mode: 'add', addMode: 'pick_staff' })}
          disabled={!canAdd || staffAvailableToLink.length === 0}
          title={
            !canAdd
              ? `Plan limit (${cap}) reached`
              : staffAvailableToLink.length === 0
                ? 'Add team members in the roster section first'
                : 'Link a roster member for transfers'
          }
          className="inline-flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 text-white shadow-md disabled:opacity-45 disabled:pointer-events-none"
          animate={
            pulseLimit && atLimit && !reduceMotion
              ? { scale: [1, 1.04, 1], boxShadow: ['0 4px 14px rgba(124,58,237,0.25)', '0 8px 24px rgba(124,58,237,0.45)', '0 4px 14px rgba(124,58,237,0.25)'] }
              : {}
          }
          transition={{ duration: 0.5 }}
        >
          <UserPlus className="w-4 h-4" /> From team roster
        </motion.button>
        <motion.button
          type="button"
          onClick={() => openModal({ mode: 'add', addMode: 'custom' })}
          disabled={!canAdd}
          className="inline-flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium rounded-xl border border-violet-200 bg-white text-violet-800 hover:bg-violet-50 disabled:opacity-45"
          whileTap={reduceMotion ? {} : { scale: 0.97 }}
        >
          <Plus className="w-4 h-4" /> Custom number
        </motion.button>
      </div>
      {!canAdd && (
        <motion.p
          className="text-xs text-amber-800 mt-2 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2"
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
        >
          Transfer limit reached for your plan ({cap}). Upgrade to add more destinations or remove one below.
        </motion.p>
      )}

      <dialog
        ref={dialogRef}
        className="w-[min(100%,30rem)] max-h-[90vh] rounded-2xl border border-violet-200 bg-white p-0 text-gray-900 shadow-2xl backdrop:bg-black/55 open:backdrop:backdrop-blur-sm"
        onCancel={(ev) => {
          ev.preventDefault()
          closeModal()
        }}
      >
        <AnimatePresence>
          {open && (
            <motion.div {...motionProps} className="flex max-h-[90vh] flex-col overflow-hidden rounded-2xl">
              <motion.div
                className="flex items-center justify-between border-b border-violet-100 px-5 py-4 bg-gradient-to-r from-violet-50 to-indigo-50"
                layout
              >
                <h3 className="text-lg font-bold text-gray-900">
                  {mode === 'add' ? 'Add transfer destination' : 'Edit transfer destination'}
                </h3>
                <button type="button" onClick={closeModal} className="rounded-lg p-2 hover:bg-white/80" aria-label="Close">
                  <X className="w-5 h-5" />
                </button>
              </motion.div>
              <motion.div className="space-y-4 overflow-y-auto px-5 py-4" layout>
                {draftError && (
                  <motion.p
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    className="text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2"
                  >
                    {draftError}
                  </motion.p>
                )}
                {mode === 'add' && (
                  <motion.div
                    className="flex gap-2 p-1 rounded-xl bg-gray-100"
                    layout
                    role="tablist"
                  >
                    {(['pick_staff', 'custom'] as const).map((tab) => (
                      <button
                        key={tab}
                        type="button"
                        role="tab"
                        aria-selected={addMode === tab}
                        onClick={() => {
                          setAddMode(tab)
                          if (tab === 'pick_staff' && staffAvailableToLink[0]) {
                            onPickStaff(staffAvailableToLink[0].id)
                          } else if (tab === 'custom') {
                            setDraft({ staff_id: '', name: '', phone: '' })
                          }
                        }}
                        className={`flex-1 py-2 text-xs font-semibold rounded-lg transition-all ${
                          addMode === tab ? 'bg-white text-violet-800 shadow-sm' : 'text-gray-600'
                        }`}
                      >
                        {tab === 'pick_staff' ? 'From roster' : 'Custom'}
                      </button>
                    ))}
                  </motion.div>
                )}
                {(addMode === 'pick_staff' || draft.staff_id) && mode === 'add' ? (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Team member</label>
                    <select
                      value={draft.staff_id}
                      onChange={(e) => onPickStaff(e.target.value)}
                      className="cs-field w-full"
                    >
                      {staffAvailableToLink.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name || 'Unnamed'}
                        </option>
                      ))}
                      {/* NOTE: this block used to read `mode === 'edit' && ...`, which
                          could never be true — the enclosing condition already requires
                          mode === 'add'. It was dead, and TypeScript flagged the
                          impossible comparison. Removed rather than enabled: turning it
                          on would show the roster picker during an edit, which is a
                          product decision about whether a target's staff link can be
                          changed after creation, not a type fix. If editing a
                          roster-linked destination looks wrong, this is the place. */}
                    </select>
                    {!draft.phone.trim() && draft.staff_id && (
                      <p className="text-xs text-amber-700 mt-2">
                        This person has no phone on their roster profile. Add one in Team roster, or use Custom with a number.
                      </p>
                    )}
                  </div>
                ) : null}
                {(addMode === 'custom' || mode === 'edit') && (
                  <>
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1">Name callers will say</label>
                      <input
                        ref={firstFieldRef}
                        type="text"
                        value={draft.name}
                        onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                        className="cs-field w-full"
                        placeholder="e.g. Jamie"
                        maxLength={120}
                      />
                    </div>
                    <motion.div layout>
                      <label className="block text-sm font-medium text-gray-700 mb-1">Phone to dial</label>
                      <input
                        type="tel"
                        value={draft.phone}
                        onChange={(e) => setDraft((d) => ({ ...d, phone: e.target.value }))}
                        className="cs-field w-full tabular-nums"
                        placeholder="+1..."
                        maxLength={32}
                        disabled={!!draft.staff_id && !!staff.find((s) => s.id === draft.staff_id)?.phone.trim()}
                      />
                      {draft.staff_id && (
                        <p className="text-xs text-gray-500 mt-1">Uses the phone from their roster profile when linked.</p>
                      )}
                    </motion.div>
                  </>
                )}
              </motion.div>
              <div className="flex flex-wrap justify-end gap-2 border-t border-violet-100 px-5 py-4 bg-violet-50/50">
                <button type="button" onClick={closeModal} className="px-4 py-2 rounded-xl text-sm font-medium bg-gray-100">
                  Cancel
                </button>
                <motion.button
                  type="button"
                  disabled={saving}
                  onClick={() => saveDraft()}
                  className="px-5 py-2 rounded-xl text-sm font-semibold bg-violet-600 text-white disabled:opacity-50"
                  whileTap={reduceMotion ? {} : { scale: 0.96 }}
                >
                  {saving ? 'Saving...' : 'Save'}
                </motion.button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </dialog>
    </div>
  )
}
