import { useEffect, useRef, useState } from 'react'
import {
  createFills, deleteFill, fetchAccounts,
  type AccountSummary, type FillLegIn,
} from '../api'

interface LegState {
  symbol: string
  side: 'buy' | 'sell'
  quantity: string
  price: string
  fee: string
}

const EMPTY: LegState = { symbol: '', side: 'buy', quantity: '', price: '', fee: '0' }

// datetime-local yields the browser's LOCAL wall-clock time with no offset,
// e.g. "2026-06-01T15:30" for 3:30pm in whatever zone the user is sitting
// in. The column is TIMESTAMPTZ. The trap: stamping a bare "Z" onto that
// string is unambiguous but WRONG -- it reinterprets "15:30 local" as
// "15:30 UTC", silently shifting every hand-entered fill by the browser's
// UTC offset (4-5 hours for US Eastern), which can reorder which fill opens
// a position once trades are grouped by executed_at. `new Date(local)`
// parses an offset-less string as LOCAL time (this also handles the
// with-seconds form), so `.toISOString()` yields the true UTC instant.
function toInstant(local: string): string {
  return new Date(local).toISOString()
}

export default function Entry() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [account, setAccount] = useState('')
  const [executedAt, setExecutedAt] = useState('')
  const [leg, setLeg] = useState({ ...EMPTY })
  const [added, setAdded] = useState<{ id: string; label: string }[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const symbolRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    fetchAccounts()
      .then((r) => { setAccounts(r.accounts); setAccount((a) => a || r.accounts[0]?.id || '') })
      .catch(() => setAccounts([]))
  }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy || !account) return  // a double-click must not create two fills,
                                   // nor a submit before accounts have loaded
    setBusy(true)
    setError(null)
    const body: FillLegIn = {
      symbol: leg.symbol.trim(), side: leg.side,
      quantity: leg.quantity, price: leg.price, fee: leg.fee || '0',
      fee_currency: 'USD', executed_at: toInstant(executedAt),
    }
    try {
      const r = await createFills({ account_id: account, fills: [body] })
      // account and date are RETAINED: entering N fills is N passes.
      setAdded((prev) => [
        { id: r.fill_ids[0], label: `${body.side} ${body.quantity} ${body.symbol} @ ${body.price}` },
        ...prev,
      ])
      setLeg({ ...EMPTY })
      symbolRef.current?.focus()
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    try {
      await deleteFill(id)
      setAdded((prev) => prev.filter((f) => f.id !== id))
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    }
  }

  return (
    <>
      <p className="eyebrow">by hand</p>
      <h1>Entry</h1>
      {error && <div className="error">{error}</div>}

      <form className="entry" onSubmit={submit}>
        {accounts.length === 0 ? (
          // Rendering the select before fetchAccounts() resolves would leave
          // `account` at its '' default; submitting in that window sends
          // account_id: "" and fails UUID parsing server-side. Hold the slot
          // with a placeholder instead (same gate Trades.tsx uses).
          <span className="muted">loading accounts…</span>
        ) : (
          <select value={account} onChange={(e) => setAccount(e.target.value)} aria-label="Account">
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        )}
        <input
          ref={symbolRef} value={leg.symbol} aria-label="Symbol" placeholder="symbol" size={8}
          onChange={(e) => setLeg({ ...leg, symbol: e.target.value })}
        />
        <select
          value={leg.side} aria-label="Side"
          onChange={(e) => setLeg({ ...leg, side: e.target.value as 'buy' | 'sell' })}
        >
          <option value="buy">buy</option>
          <option value="sell">sell</option>
        </select>
        {/* type="text" + inputMode, NEVER type="number": a number input
            round-trips through a float and silently destroys a
            small-magnitude quantity. Same reason format.ts exists. */}
        <input
          type="text" inputMode="decimal" value={leg.quantity} aria-label="Quantity"
          placeholder="qty" size={7}
          onChange={(e) => setLeg({ ...leg, quantity: e.target.value })}
        />
        <input
          type="text" inputMode="decimal" value={leg.price} aria-label="Price"
          placeholder="price" size={9}
          onChange={(e) => setLeg({ ...leg, price: e.target.value })}
        />
        <input
          type="text" inputMode="decimal" value={leg.fee} aria-label="Fee"
          placeholder="fee" size={6}
          onChange={(e) => setLeg({ ...leg, fee: e.target.value })}
        />
        <input
          type="datetime-local" value={executedAt} aria-label="Executed at"
          onChange={(e) => setExecutedAt(e.target.value)}
        />
        <button type="submit" disabled={busy || !account}>{busy ? 'saving…' : 'add fill'}</button>
      </form>

      {added.length > 0 && (
        <section className="section">
          <p className="eyebrow">added this session</p>
          <table>
            <tbody>
              {added.map((f) => (
                <tr key={f.id}>
                  <td className="num">{f.label}</td>
                  <td className="right">
                    <button onClick={() => remove(f.id)}>delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  )
}
