import { useEffect, useState } from 'react'
import {
  createSnapshot,
  fetchAccounts,
  fetchDashboard,
  fetchSnapshot,
  type AccountSummary,
  type AccountTile,
  type StoredSnapshot,
} from '../api'
import { money } from '../format'

function todayLocal(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// The difference the panel reports, or null when either side is unavailable.
// Returns the RAW difference, not a pre-rounded one: money() (format.ts)
// already applies this codebase's sub-cent display policy -- rounding here
// first (`.toFixed(2)`) would flatten a genuine sub-cent difference to
// "0.00" before money() ever sees it, reporting agreement on the one input
// where the two sides actually disagree.
function diff(typed: string, ledger: string | null): string | null {
  if (ledger == null || typed.trim() === '') return null
  const d = Number(ledger) - Number(typed)
  if (!Number.isFinite(d)) return null
  return String(d)
}

export default function Snapshot() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [tiles, setTiles] = useState<AccountTile[]>([])
  const [account, setAccount] = useState('')
  const [asOf, setAsOf] = useState(todayLocal)
  const [cash, setCash] = useState('')
  const [equity, setEquity] = useState('')
  const [note, setNote] = useState('')
  const [existing, setExisting] = useState<StoredSnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  useEffect(() => {
    fetchAccounts()
      .then((r) => setAccounts(r.accounts))
      .catch((err) => setError(String(err instanceof Error ? err.message : err)))
    // The dashboard already computes per-account cash and equity; this panel
    // reads them rather than adding a second endpoint that would compute the
    // same figures a second way and be free to disagree.
    fetchDashboard()
      .then((d) => setTiles(d.accounts))
      .catch(() => setTiles([]))
  }, [])

  // Whether saving REPLACES an existing row. add_snapshot's ON CONFLICT fires
  // on an exact (account_id, as_of) match, so this asks for exactly that date
  // -- not the latest on or before it, which would warn about replacing July's
  // statement while entering August's.
  useEffect(() => {
    let current = true
    // Not a synchronous setState guard: an empty account/asOf resolves
    // through the same .then() path as a real lookup, rather than calling
    // setState directly in the effect body (oxlint's react(set-state-in-effect)
    // -- see Marks.tsx's `load()` for the same shape of workaround).
    const lookup = account && asOf ? fetchSnapshot(account, asOf) : Promise.resolve({ snapshot: null })
    lookup
      .then((r) => {
        if (current) setExisting(r.snapshot)
      })
      .catch(() => {
        if (current) setExisting(null)
      })
    return () => {
      current = false
    }
  }, [account, asOf])

  const tile = tiles.find((t) => t.id === account) ?? null
  // The ledger side is computed as of NOW -- account_cash and open_positions
  // take no as_of. The raw difference is ALWAYS shown, for both dates: a
  // transposed cash/equity pair appears as two large offsetting differences
  // either way, which is what this panel is for. No verdict or tick is ever
  // rendered -- this flag only gates the caveat below, which explains that a
  // difference against a past statement date is not a reconciliation, since
  // the ledger column would be today's position, not the statement's.
  const comparable = asOf === todayLocal()

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy || !account || !asOf || cash.trim() === '' || equity.trim() === '') return
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      const r = await createSnapshot({
        account_id: account,
        as_of: asOf,
        cash_balance: cash.trim(),
        total_equity: equity.trim(),
        note: note.trim() === '' ? null : note.trim(),
      })
      setSaved(
        r.replaced
          ? `snapshot for ${asOf} replaced`
          : `snapshot stored for ${asOf}`,
      )
      setCash('')
      setEquity('')
      setNote('')
      const again = await fetchSnapshot(account, asOf)
      setExisting(again.snapshot)
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="snapshot" onSubmit={submit}>
      <label>
        account
        <select value={account} onChange={(e) => setAccount(e.target.value)} required>
          <option value="">choose an account…</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} — {a.venue}
            </option>
          ))}
        </select>
      </label>

      <label>
        as of
        {/* A DATE, not a datetime: `snapshot add` runs its as_of through
            _parse_as_of, where a bare date becomes midnight UTC. That also
            sidesteps the datetime-local offset hazard entirely -- there is no
            wall-clock time here to misinterpret. */}
        <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} required />
        <span className="muted"> the statement date, not today</span>
      </label>

      <table className="compare">
        <thead>
          <tr>
            <th />
            <th className="num">you type</th>
            <th className="num">ledger, as of now</th>
            <th className="num">difference</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">cash</th>
            <td className="num">
              <input
                type="text" inputMode="decimal" value={cash} required
                onChange={(e) => setCash(e.target.value)} aria-label="cash balance"
              />
            </td>
            <td className="num">{money(tile?.cash ?? null)}</td>
            <td className="num">{money(diff(cash, tile?.cash ?? null))}</td>
          </tr>
          <tr>
            <th scope="row">total equity</th>
            <td className="num">
              <input
                type="text" inputMode="decimal" value={equity} required
                onChange={(e) => setEquity(e.target.value)} aria-label="total equity"
              />
            </td>
            <td className="num">{money(tile?.equity ?? null)}</td>
            <td className="num">{money(diff(equity, tile?.equity ?? null))}</td>
          </tr>
        </tbody>
      </table>

      {!comparable && (
        <div className="muted">
          the ledger column is today's position, not {asOf}'s — treat the difference as a
          sanity check on what you typed, not as a reconciliation
        </div>
      )}
      {tile !== null && tile.equity == null && (
        <div className="muted">
          ledger equity is unavailable for this account — something it holds has no usable
          mark. Record marks first if you want the comparison.
        </div>
      )}

      <label>
        note
        <input type="text" value={note} onChange={(e) => setNote(e.target.value)} />
      </label>

      {existing !== null && (
        <div className="warn">
          a snapshot already exists for {asOf} — saving replaces it (cash{' '}
          {money(existing.cash_balance)}, equity {money(existing.total_equity)})
        </div>
      )}

      <button type="submit" disabled={busy}>
        {busy ? 'saving…' : 'Save snapshot'}
      </button>

      {error && <div className="error">{error}</div>}
      {saved && <div className="ok">{saved}</div>}
    </form>
  )
}
