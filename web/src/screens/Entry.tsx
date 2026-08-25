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
// An empty or unparseable value must throw HERE rather than produce
// `Invalid Date` silently -- the caller relies on this landing in its own
// try/catch so a bad date can never wedge the busy flag.
function toInstant(local: string): string {
  const d = new Date(local)
  if (Number.isNaN(d.getTime())) throw new Error('executed at: enter a date and time')
  return d.toISOString()
}

// The 422 for a bad leg names it positionally, e.g. "fills[2].symbol: must
// not be blank" (api/fills.py). Pulling the index back out is enough to
// point the user at the right row -- no need to parse which field.
function legIndexFromError(msg: string): number | null {
  const m = msg.match(/fills\[(\d+)\]/)
  return m ? Number(m[1]) : null
}

export default function Entry() {
  const [mode, setMode] = useState<'fill' | 'multileg'>('fill')
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [account, setAccount] = useState('')
  const [executedAt, setExecutedAt] = useState('')
  const [leg, setLeg] = useState({ ...EMPTY })
  const [legs, setLegs] = useState<LegState[]>([{ ...EMPTY }, { ...EMPTY }])
  const [legError, setLegError] = useState<number | null>(null)
  const [added, setAdded] = useState<{ id: string; label: string }[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const symbolRef = useRef<HTMLInputElement>(null)
  const firstLegRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    fetchAccounts()
      .then((r) => { setAccounts(r.accounts); setAccount((a) => a || r.accounts[0]?.id || '') })
      .catch(() => setAccounts([]))
  }, [])

  function switchMode(m: 'fill' | 'multileg') {
    // Switching mid-typing shouldn't leave a stale error from the other
    // form's last attempt pointing at a row that no longer applies.
    setMode(m)
    setError(null)
    setLegError(null)
  }

  function updateLeg(i: number, patch: Partial<LegState>) {
    setLegs((prev) => prev.map((l, idx) => (idx === i ? { ...l, ...patch } : l)))
  }

  function addLeg() {
    setLegs((prev) => [...prev, { ...EMPTY }])
  }

  function removeLeg(i: number) {
    // Always keep at least one row -- an empty legs array would submit a
    // request with fills: [], which is a confusing way to do nothing.
    setLegs((prev) => (prev.length > 1 ? prev.filter((_, idx) => idx !== i) : prev))
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    // a double-click must not create two fills; nor a submit before accounts
    // have loaded, nor one with no date entered
    if (busy || !account || !executedAt) return
    setBusy(true)
    setError(null)
    try {
      // toInstant can throw (empty/unparseable date) -- it MUST run inside
      // this try, or an uncaught throw here skips `finally` and leaves the
      // button stuck on "saving…" forever with no error shown.
      const body: FillLegIn = {
        symbol: leg.symbol.trim(), side: leg.side,
        quantity: leg.quantity, price: leg.price, fee: leg.fee || '0',
        fee_currency: 'USD', executed_at: toInstant(executedAt),
      }
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

  async function submitMultileg(e: React.FormEvent) {
    e.preventDefault()
    if (busy || !account || !executedAt) return
    setBusy(true)
    setError(null)
    setLegError(null)
    try {
      // toInstant can throw -- same reason as submit(): it must stay inside
      // this try, or a bad date leaves the button wedged on "saving…".
      // Every leg shares this ONE instant: multi-leg mode is for legs of the
      // same trade executed together, not a batch of unrelated fills.
      const at = toInstant(executedAt)
      const body: FillLegIn[] = legs.map((l) => ({
        symbol: l.symbol.trim(), side: l.side,
        quantity: l.quantity, price: l.price, fee: l.fee || '0',
        fee_currency: 'USD', executed_at: at,
      }))
      // ONE createFills call with every leg -- the server writes the whole
      // list in a single transaction, so a four-leg position lands together
      // or (via the catch below) not at all.
      const r = await createFills({ account_id: account, fills: body })
      setAdded((prev) => [
        ...r.fill_ids.map((id, i) => ({
          id, label: `${body[i].side} ${body[i].quantity} ${body[i].symbol} @ ${body[i].price}`,
        })).reverse(),
        ...prev,
      ])
      setLegs([{ ...EMPTY }, { ...EMPTY }])
      firstLegRef.current?.focus()
    } catch (err) {
      const msg = String(err instanceof Error ? err.message : err)
      setError(msg)
      setLegError(legIndexFromError(msg))
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

      {/* Fixed two-way switch -- D11: no rearrangeable or configurable
          panes. This is the only way to change modes; the layout of either
          mode itself is likewise fixed. */}
      <div className="segmented" role="tablist" aria-label="Entry mode">
        <button
          type="button" role="tab" aria-selected={mode === 'fill'}
          className={mode === 'fill' ? 'active' : undefined}
          onClick={() => switchMode('fill')}
        >
          single fill
        </button>
        <button
          type="button" role="tab" aria-selected={mode === 'multileg'}
          className={mode === 'multileg' ? 'active' : undefined}
          onClick={() => switchMode('multileg')}
        >
          multi-leg
        </button>
      </div>

      {error && <div className="error">{error}</div>}

      {mode === 'fill' ? (
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
            type="datetime-local" value={executedAt} aria-label="Executed at" required
            onChange={(e) => setExecutedAt(e.target.value)}
          />
          <button type="submit" disabled={busy || !account || !executedAt}>
            {busy ? 'saving…' : 'add fill'}
          </button>
        </form>
      ) : (
        <form className="entry entry-multileg" onSubmit={submitMultileg}>
          <div className="entry-header">
            {accounts.length === 0 ? (
              <span className="muted">loading accounts…</span>
            ) : (
              <select value={account} onChange={(e) => setAccount(e.target.value)} aria-label="Account">
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            )}
            <input
              type="datetime-local" value={executedAt} aria-label="Executed at" required
              onChange={(e) => setExecutedAt(e.target.value)}
            />
            <span className="muted">shared by every leg below</span>
          </div>

          <table className="legs-table">
            <thead>
              <tr>
                <th>symbol</th><th>side</th><th className="right">qty</th>
                <th className="right">price</th><th className="right">fee</th><th />
              </tr>
            </thead>
            <tbody>
              {legs.map((l, i) => (
                <tr key={i} className={legError === i ? 'leg-error' : undefined}>
                  <td>
                    <input
                      ref={i === 0 ? firstLegRef : undefined}
                      value={l.symbol} aria-label={`Leg ${i + 1} symbol`} placeholder="symbol" size={8}
                      onChange={(e) => updateLeg(i, { symbol: e.target.value })}
                    />
                  </td>
                  <td>
                    <select
                      value={l.side} aria-label={`Leg ${i + 1} side`}
                      onChange={(e) => updateLeg(i, { side: e.target.value as 'buy' | 'sell' })}
                    >
                      <option value="buy">buy</option>
                      <option value="sell">sell</option>
                    </select>
                  </td>
                  {/* text + inputMode, never number -- same float-truncation
                      trap as the single-fill form above. */}
                  <td>
                    <input
                      type="text" inputMode="decimal" value={l.quantity} aria-label={`Leg ${i + 1} quantity`}
                      placeholder="qty" size={7}
                      onChange={(e) => updateLeg(i, { quantity: e.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="text" inputMode="decimal" value={l.price} aria-label={`Leg ${i + 1} price`}
                      placeholder="price" size={9}
                      onChange={(e) => updateLeg(i, { price: e.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="text" inputMode="decimal" value={l.fee} aria-label={`Leg ${i + 1} fee`}
                      placeholder="fee" size={6}
                      onChange={(e) => updateLeg(i, { fee: e.target.value })}
                    />
                  </td>
                  <td className="right">
                    <button type="button" onClick={() => removeLeg(i)} disabled={legs.length <= 1}>
                      remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="entry-actions">
            <button type="button" onClick={addLeg}>add leg</button>
            <button type="submit" disabled={busy || !account || !executedAt}>
              {busy ? 'saving…' : `add ${legs.length} legs`}
            </button>
          </div>
        </form>
      )}

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
