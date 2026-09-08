'use client'

/** Paste-in import for stores whose calendar lives in another system (e.g. Zenoti).
 *
 * Zenoti refused API access, so the bridge is the one their own UI allows: copy the
 * day out of the Queue view, paste it here, an AI reads it, and the manager confirms
 * before anything is saved. The preview step is deliberate — an LLM does the parsing,
 * so a human checks it before it reaches the calendar. */

import { useCallback, useMemo, useState } from 'react'
import { ClipboardPaste, Check, AlertTriangle, Loader2, X } from 'lucide-react'
import { useApiClient } from '@/lib/api'
import { localDayString } from '@/lib/localDay'

type ParsedRow = {
  customer_name: string
  service: string
  stylist: string
  date: string
  time: string
  is_request: boolean
  price?: string
  notes: string
  already_imported?: boolean
}

type PreviewResponse = {
  appointments: ParsedRow[]
  warnings: string[]
  found: number
  new: number
  already_imported: number
  /** Stylists on this paste who aren't on the roster. Their appointments would import
   *  unassigned, so this is shown before committing, while it can still be fixed. */
  unmatched_stylists?: string[]
  analyzed_at: string
}

const apiDetail = (e: unknown): string | null => {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : null
}

export function ImportFromZenoti({
  providerName,
  onImported,
}: {
  providerName?: string
  onImported?: () => void
}) {
  const api = useApiClient()
  const label = providerName?.trim() || 'your booking system'

  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [day, setDay] = useState(() => localDayString())
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [addingStylists, setAddingStylists] = useState(false)
  const [stylistNote, setStylistNote] = useState<string | null>(null)
  // Rows live separately from `preview` because they're editable — the AI's reading is
  // a starting point the manager corrects, not a verdict.
  const [rows, setRows] = useState<ParsedRow[]>([])
  const [analyzing, setAnalyzing] = useState(false)
  const [importing, setImporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<{ created: number; updated: number } | null>(null)

  const newRows = useMemo(() => rows.filter((r) => !r.already_imported), [rows])
  const invalidCount = useMemo(
    () => rows.filter((r) => !r.customer_name.trim() || !r.time || !r.date).length,
    [rows]
  )

  const updateRow = useCallback((i: number, patch: Partial<ParsedRow>) => {
    setRows((prev) =>
      prev.map((r, idx) => {
        if (idx !== i) return r
        const next = { ...r, ...patch }
        // Duplicate detection was keyed on date+time+name, so editing any of those
        // makes the server's "already imported" verdict stale. Clear it and let the
        // commit re-check — it's the authority either way.
        if ('date' in patch || 'time' in patch || 'customer_name' in patch) {
          next.already_imported = false
        }
        return next
      })
    )
  }, [])

  const removeRow = useCallback((i: number) => {
    setRows((prev) => prev.filter((_, idx) => idx !== i))
  }, [])

  const reset = useCallback(() => {
    setText('')
    setPreview(null)
    setRows([])
    setError(null)
    setDone(null)
  }, [])

  const analyze = async () => {
    if (!text.trim()) return
    setAnalyzing(true)
    setError(null)
    setDone(null)
    try {
      const { data } = await api.post<PreviewResponse>('/api/appointments/import/preview', {
        text,
        date: day,
      })
      setPreview(data)
      setRows(data.appointments || [])
    } catch (e) {
      setError(apiDetail(e) || 'Could not analyze that text. Please try again.')
    } finally {
      setAnalyzing(false)
    }
  }

  /** Add the pasted stylists to the roster so their appointments land in their own
   *  column instead of Unassigned. Appends to the existing staff list rather than
   *  replacing it — the PATCH takes the whole array, so reading first is what stops
   *  this from wiping the team. */
  const addMissingStylists = async (names: string[]) => {
    if (!names.length) return
    setAddingStylists(true)
    setStylistNote(null)
    try {
      const { data } = await api.get('/api/business-info')
      const current = Array.isArray(data?.staff) ? data.staff : []
      const taken = new Set(
        current.map((m: { name?: string }) => (m?.name || '').trim().toLowerCase())
      )
      const additions = names
        .map((n) => n.trim())
        .filter((n) => n && !taken.has(n.toLowerCase()))
        .map((n) => ({ id: crypto.randomUUID(), name: n, phone: '', email: '', notes: '' }))
      if (!additions.length) {
        setStylistNote('They were already on your team list.')
        return
      }
      await api.patch('/api/business-info', { staff: [...current, ...additions] })
      setStylistNote(
        `Added ${additions.length} to your team. Import now and they'll land in their own column.`
      )
    } catch (e) {
      setStylistNote(apiDetail(e) || 'Could not add them — add them in Settings instead.')
    } finally {
      setAddingStylists(false)
    }
  }

  const commit = async () => {
    if (!newRows.length) return
    setImporting(true)
    setError(null)
    try {
      const { data } = await api.post<{ created: number; updated: number }>(
        '/api/appointments/import/commit',
        { appointments: newRows }
      )
      setDone({ created: data.created, updated: data.updated })
      setPreview(null)
      setRows([])
      setText('')
      onImported?.()
    } catch (e) {
      setError(apiDetail(e) || 'Could not import those appointments.')
    } finally {
      setImporting(false)
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-zinc-950/40 px-4 py-2 text-sm font-medium text-zinc-200 motion-safe-transition hover:border-white/30 hover:text-white"
      >
        <ClipboardPaste className="h-4 w-4" aria-hidden />
        Paste from {label}
      </button>
    )
  }

  return (
    <div className="rounded-2xl border border-white/10 bg-zinc-900/70 p-5">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-base font-semibold text-white">
            Paste appointments from {label}
          </h3>
          <p className="mt-1 text-xs text-zinc-500">
            Open your day in {label}, select the appointments, copy, and paste below. We&rsquo;ll
            read them and show you what we found before saving anything.
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setOpen(false)
            reset()
          }}
          aria-label="Close"
          className="rounded-full p-1 text-zinc-500 hover:text-white"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      {done && (
        <div className="mb-3 flex items-start gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">
          <Check className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span>
            Imported {done.created} appointment{done.created === 1 ? '' : 's'}
            {done.updated ? `, updated ${done.updated}` : ''}.
          </span>
        </div>
      )}

      {error && (
        <div className="mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {error}
        </div>
      )}

      {!preview && (
        <>
          <div className="mb-2 flex flex-wrap items-end gap-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-zinc-400">
                Which day is this?
              </label>
              <input
                type="date"
                value={day}
                onChange={(e) => setDay(e.target.value)}
                className="rounded-lg border border-white/10 bg-zinc-950/60 px-3 py-1.5 text-sm text-white focus:border-cyan-500 focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-zinc-600">
                Used when the copied text doesn&rsquo;t include a date.
              </p>
            </div>
          </div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={8}
            placeholder="Paste the copied appointments here…"
            className="w-full rounded-xl border border-white/10 bg-zinc-950/60 px-3 py-2 font-mono text-xs text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
          />
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={analyze}
              disabled={analyzing || !text.trim()}
              className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 motion-safe-transition hover:brightness-110 disabled:opacity-50"
            >
              {analyzing && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
              {analyzing ? 'Reading…' : 'Read appointments'}
            </button>
            {text.trim() && !analyzing && (
              <button
                type="button"
                onClick={reset}
                className="text-xs text-zinc-500 hover:text-zinc-300"
              >
                Clear
              </button>
            )}
          </div>
        </>
      )}

      {preview && (
        <>
          <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <span className="text-zinc-300">
              Found <span className="font-semibold text-white">{rows.length}</span>
            </span>
            <span className="text-emerald-300">{newRows.length} new</span>
            {rows.length - newRows.length > 0 && (
              <span className="text-zinc-500">
                {rows.length - newRows.length} already imported
              </span>
            )}
            <span className="text-zinc-600">
              as of{' '}
              {new Date(preview.analyzed_at).toLocaleTimeString('en-US', {
                hour: 'numeric',
                minute: '2-digit',
              })}
            </span>
          </div>

          {preview.unmatched_stylists && preview.unmatched_stylists.length > 0 && (
            <div className="mb-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                <div className="min-w-0">
                  <p>
                    <strong>
                      {preview.unmatched_stylists.length} stylist
                      {preview.unmatched_stylists.length === 1 ? '' : 's'}
                    </strong>{' '}
                    on this paste {preview.unmatched_stylists.length === 1 ? "isn't" : "aren't"} on
                    your team list: {preview.unmatched_stylists.join(', ')}. Their appointments will
                    import as <em>Unassigned</em>, so the day won&rsquo;t show who&rsquo;s busy.
                  </p>
                  {stylistNote ? (
                    <p className="mt-1.5 font-medium">{stylistNote}</p>
                  ) : (
                    <button
                      type="button"
                      disabled={addingStylists}
                      onClick={() => void addMissingStylists(preview.unmatched_stylists || [])}
                      className="mt-1.5 rounded-md border border-amber-400/40 px-2.5 py-1 font-medium text-amber-100 hover:bg-amber-500/15 disabled:opacity-60"
                    >
                      {addingStylists
                        ? 'Adding…'
                        : `Add ${preview.unmatched_stylists.length === 1 ? 'them' : 'them all'} to my team`}
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

          {preview.warnings.map((w) => (
            <div
              key={w}
              className="mb-2 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>{w}</span>
            </div>
          ))}

          {rows.length > 0 ? (
            <>
              <p className="mb-1.5 text-[11px] text-zinc-500">
                Anything read wrong? Edit it here before importing, or remove the row.
              </p>
              <div className="max-h-80 overflow-y-auto overflow-x-auto rounded-xl border border-white/10">
                <table className="w-full min-w-[680px] text-left text-xs">
                  <thead className="sticky top-0 bg-zinc-900/95 text-zinc-500">
                    <tr>
                      <th className="px-2 py-2 font-medium">Date</th>
                      <th className="px-2 py-2 font-medium">Time</th>
                      <th className="px-2 py-2 font-medium">Customer</th>
                      <th className="px-2 py-2 font-medium">Service</th>
                      <th className="px-2 py-2 font-medium">Stylist</th>
                      <th className="px-2 py-2 text-right font-medium">Total</th>
                      <th className="w-8 px-2 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => {
                      const bad = !r.customer_name.trim() || !r.time || !r.date
                      return (
                        <tr
                          key={i}
                          className={`border-t border-white/5 ${
                            r.already_imported ? 'opacity-50' : ''
                          }`}
                        >
                          <td className="px-2 py-1.5">
                            <input
                              type="date"
                              value={r.date}
                              onChange={(e) => updateRow(i, { date: e.target.value })}
                              className={`w-32 rounded border bg-zinc-950/60 px-1.5 py-1 text-xs text-white focus:border-cyan-500 focus:outline-none ${
                                r.date ? 'border-white/10' : 'border-amber-500/60'
                              }`}
                            />
                          </td>
                          <td className="px-2 py-1.5">
                            <input
                              type="time"
                              value={r.time}
                              onChange={(e) => updateRow(i, { time: e.target.value })}
                              className={`w-24 rounded border bg-zinc-950/60 px-1.5 py-1 text-xs text-white focus:border-cyan-500 focus:outline-none ${
                                r.time ? 'border-white/10' : 'border-amber-500/60'
                              }`}
                            />
                          </td>
                          <td className="px-2 py-1.5">
                            <input
                              type="text"
                              value={r.customer_name}
                              onChange={(e) => updateRow(i, { customer_name: e.target.value })}
                              placeholder="Name required"
                              className={`w-36 rounded border bg-zinc-950/60 px-1.5 py-1 text-xs text-white placeholder-amber-500/60 focus:border-cyan-500 focus:outline-none ${
                                r.customer_name.trim() ? 'border-white/10' : 'border-amber-500/60'
                              }`}
                            />
                            {r.already_imported && (
                              <span className="ml-1 block text-[10px] uppercase tracking-wide text-zinc-600">
                                already added
                              </span>
                            )}
                          </td>
                          <td className="px-2 py-1.5">
                            <input
                              type="text"
                              value={r.service}
                              onChange={(e) => updateRow(i, { service: e.target.value })}
                              className="w-40 rounded border border-white/10 bg-zinc-950/60 px-1.5 py-1 text-xs text-white focus:border-cyan-500 focus:outline-none"
                            />
                          </td>
                          <td className="px-2 py-1.5">
                            <input
                              type="text"
                              value={r.stylist}
                              onChange={(e) => updateRow(i, { stylist: e.target.value })}
                              placeholder="first available"
                              className="w-28 rounded border border-white/10 bg-zinc-950/60 px-1.5 py-1 text-xs text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
                            />
                          </td>
                          <td className="whitespace-nowrap px-2 py-1.5 text-right text-zinc-400">
                            {r.price ? `$${r.price}` : '—'}
                          </td>
                          <td className="px-2 py-1.5 align-middle">
                            <button
                              type="button"
                              onClick={() => removeRow(i)}
                              title="Remove this row"
                              aria-label={`Remove ${r.customer_name || 'row'}`}
                              className="rounded p-1 text-zinc-600 motion-safe-transition hover:bg-white/5 hover:text-red-300"
                            >
                              <X className="h-3.5 w-3.5" aria-hidden />
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {invalidCount > 0 && (
                <p className="mt-1.5 text-[11px] text-amber-400">
                  {invalidCount} row{invalidCount === 1 ? '' : 's'} need a date, time and name
                  before importing — fill them in or remove the row.
                </p>
              )}
            </>
          ) : (
            <p className="rounded-xl border border-white/10 bg-zinc-950/40 px-4 py-6 text-center text-sm text-zinc-500">
              No appointments found in that text.
            </p>
          )}

          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={commit}
              disabled={importing || newRows.length === 0 || invalidCount > 0}
              className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 motion-safe-transition hover:brightness-110 disabled:opacity-50"
            >
              {importing && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
              {importing
                ? 'Importing…'
                : invalidCount > 0
                  ? 'Fix the highlighted rows'
                  : newRows.length
                    ? `Import ${newRows.length} appointment${newRows.length === 1 ? '' : 's'}`
                    : 'Nothing new to import'}
            </button>
            <button
              type="button"
              onClick={() => setPreview(null)}
              className="text-xs text-zinc-500 hover:text-zinc-300"
            >
              Back to paste
            </button>
          </div>
        </>
      )}
    </div>
  )
}
