import { useEffect, useState } from 'react'
import { fetchMarks, setMarks, type MarkRow } from '../api'
import { toInstant } from '../datetime'
import { money, qty } from '../format'

// The `datetime-local` input wants "YYYY-MM-DDTHH:MM" in LOCAL time, which is
// what the user is thinking in. It is converted back to a true UTC instant by
// toInstant() at submit -- never by stamping "Z", which would be wrong by the
// browser's offset. See web/src/datetime.ts.
function localNow(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function ageDays(asOf: string): number {
  return Math.floor((Date.now() - new Date(asOf).getTime()) / 86_400_000)
}

export default function Marks() {
  const [rows, setRows] = useState<MarkRow[] | null>(null)
  const [prices, setPrices] = useState<Record<string, string>>({})
  const [asOf, setAsOf] = useState(localNow)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  // Not `async`/`await`: oxlint's react(set-state-in-effect) rule traces into
  // an async function's setState calls when it is invoked from useEffect
  // below (even via `void`), but not through a plain function that defers to
  // .then()/.catch() -- same outcome (rows or error set, never a rejection
  // escaping to the caller), structured so the mount effect doesn't trip it.
  function load(): Promise<void> {
    return fetchMarks()
      .then((page) => setRows(page.marks))
      .catch((err) => setError(String(err instanceof Error ? err.message : err)))
  }

  useEffect(() => {
    void load()
  }, [])

  // A blank input means "leave this instrument alone"; "0" means "record a
  // price of zero". These are DIFFERENT, all the way down: mark_price_chk
  // permits 0, latest_marks omits an unmarked instrument rather than
  // zero-filling it (a genuine 0 mark is legal), and the API accepts "0".
  // Collapsing them here -- `Number(p) || skip`, say -- would make it
  // impossible to mark an expired option worthless.
  const filled = Object.entries(prices).filter(([, p]) => p.trim() !== '')

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy || filled.length === 0) return
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      // Inside the try: toInstant throws on an unparseable value, and it must
      // land in this catch or a bad date leaves the button wedged on
      // "saving…".
      const at = toInstant(asOf, 'as of')
      const r = await setMarks({
        as_of: at,
        marks: filled.map(([instrument_id, price]) => ({ instrument_id, price: price.trim() })),
      })
      setSaved(`${r.marks_set} mark${r.marks_set === 1 ? '' : 's'} recorded`)
      setPrices({})
      // Reload rather than patching state: the server is the authority on
      // what the stored mark and its age now are, and a mark written at an
      // as_of EARLIER than an existing one does not become "the latest"
      // (latest_marks orders by as_of, not by insertion).
      await load()
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  if (rows === null) {
    return error ? <div className="error">marks failed to load — {error}</div> : <div>loading…</div>
  }

  if (rows.length === 0) {
    return (
      <div className="empty">
        nothing held — there is nothing to mark until the ledger has an open position
      </div>
    )
  }

  return (
    <form className="marks" onSubmit={submit}>
      <label className="asof">
        as of
        <input
          type="datetime-local"
          value={asOf}
          onChange={(e) => setAsOf(e.target.value)}
          required
        />
      </label>

      <table className="marks-table">
        <thead>
          <tr>
            <th>symbol</th>
            <th className="num">held</th>
            <th>last mark</th>
            <th className="num">price</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.instrument_id}>
              <td>
                {row.symbol}
                {/* instrument.symbol is NOT unique -- two instruments can
                    legitimately share a ticker (the same symbol quoted in two
                    currencies). Only natural_key is unique, so without it
                    these are two identical-looking rows and there is no way
                    to tell which one is being priced. */}
                <span className="muted"> {row.natural_key}</span>
              </td>
              <td className="num">{qty(row.quantity)}</td>
              <td>
                {row.last_mark === null ? (
                  <span className="muted">never marked</span>
                ) : (
                  <>
                    {money(row.last_mark.price)}{' '}
                    <span className="muted">({ageDays(row.last_mark.as_of)}d old)</span>
                  </>
                )}
              </td>
              <td className="num">
                <input
                  type="text"
                  inputMode="decimal"
                  value={prices[row.instrument_id] ?? ''}
                  onChange={(e) =>
                    setPrices((p) => ({ ...p, [row.instrument_id]: e.target.value }))
                  }
                  aria-label={`price for ${row.symbol} (${row.natural_key})`}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="actions">
        <span className="muted">
          {filled.length} of {rows.length} filled · blank rows are left untouched
        </span>
        <button type="submit" disabled={busy || filled.length === 0}>
          {busy ? 'saving…' : 'Save marks'}
        </button>
      </div>

      {error && <div className="error">{error}</div>}
      {saved && <div className="ok">{saved}</div>}
    </form>
  )
}
