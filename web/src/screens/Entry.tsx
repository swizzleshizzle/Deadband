import { useEffect, useRef, useState } from 'react'
import {
  commitImport, createFills, deleteFill, fetchAccounts, previewImport,
  type AccountSummary, type FillLegIn, type ImportCommitReport, type PreviewReport,
} from '../api'
import { toInstant } from '../datetime'

// The importer registry (importers/registry.py) also lists "coinbase-api",
// but that importer takes a JSON fills export from the Advanced Trade API,
// not a broker CSV -- it has no place in a file-picker built for uploads.
// No endpoint exposes "which venues take a CSV upload" as its own list, so
// this is hardcoded to the two the repo actually ships CSV importers for.
// GAP: if a third CSV venue is ever added, this array silently misses it
// until someone remembers to update it here too -- worth an API-exposed
// list (e.g. importers/registry.py's own CSV/API distinction) instead.
const IMPORT_VENUES = ['coinbase', 'fidelity']

// blocking is (account_ref | null, message) pairs, precisely so *this*
// account's rows can be shown blocking while *that* account's are fine.
// Grouped here rather than at render time so the render stays a plain map.
function groupBlocking(pairs: readonly (readonly [string | null, string])[]): [string | null, string[]][] {
  const order: (string | null)[] = []
  const byRef = new Map<string | null, string[]>()
  for (const [ref, msg] of pairs) {
    if (!byRef.has(ref)) {
      byRef.set(ref, [])
      order.push(ref)
    }
    byRef.get(ref)!.push(msg)
  }
  return order.map((ref) => [ref, byRef.get(ref)!])
}

const DUPLICATE_SKIP_TEXT: Record<string, string> = {
  no_connection: 'this preview ran with no database connection to check against.',
  needs_account: 'rows carry no account column, and no account has been chosen yet.',
  unknown_refs: 'a money-carrying row references an account that is not registered yet.',
}

interface LegState {
  symbol: string
  side: 'buy' | 'sell'
  quantity: string
  price: string
  fee: string
}

const EMPTY: LegState = { symbol: '', side: 'buy', quantity: '', price: '', fee: '0' }

// The 422 for a bad leg names it positionally, e.g. "fills[2].symbol: must
// not be blank" (api/fills.py). Pulling the index back out is enough to
// point the user at the right row -- no need to parse which field.
function legIndexFromError(msg: string): number | null {
  const m = msg.match(/fills\[(\d+)\]/)
  return m ? Number(m[1]) : null
}

export default function Entry() {
  const [mode, setMode] = useState<'fill' | 'multileg' | 'import'>('fill')
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

  // Import wizard. `report` is cleared whenever the file or venue changes so
  // a stale preview can never be mistaken for a description of the
  // currently-selected upload -- `commitReport` and `previewAccountId` are
  // cleared alongside it for the same reason: a commit result or a
  // needs_account selection describes ONE specific preview, and must not
  // survive into a description of a different one.
  const [importFile, setImportFile] = useState<File | null>(null)
  const [importVenue, setImportVenue] = useState(IMPORT_VENUES[0])
  const [importAccount, setImportAccount] = useState('')
  const [importBusy, setImportBusy] = useState(false)
  const [importError, setImportError] = useState<string | null>(null)
  const [report, setReport] = useState<PreviewReport | null>(null)
  // The account id that PRODUCED `report`, not whatever `importAccount` holds
  // right now -- the needs_account selector can be changed after a preview
  // without re-previewing, and committing against a different account than
  // the one that was actually previewed would defeat the point of a preview.
  const [previewAccountId, setPreviewAccountId] = useState<string | undefined>(undefined)
  const [commitBusy, setCommitBusy] = useState(false)
  const [commitReport, setCommitReport] = useState<ImportCommitReport | null>(null)
  // Whether THIS FILE, on THIS VENUE, has no per-row account column -- tracked
  // separately from `report` on purpose. `report` is invalidated the moment
  // the chosen account changes (it described routing for the OLD account),
  // but "does this file need an account picked at all" is a fact about the
  // file and the venue's export format, not about which account was picked.
  // If the account selector's own visibility depended on `report` (as it did
  // before this was split out), changing the selector would unmount the
  // selector itself mid-interaction -- a dead end with no way to preview the
  // newly-chosen account. Cleared only where the underlying fact can change:
  // a new file or a new venue. Left untouched by an account change, and left
  // untouched on a failed preview (nothing about the file/venue changed,
  // so the last known answer is still the best one available).
  const [needsAccount, setNeedsAccount] = useState(false)

  useEffect(() => {
    fetchAccounts()
      .then((r) => { setAccounts(r.accounts); setAccount((a) => a || r.accounts[0]?.id || '') })
      .catch(() => setAccounts([]))
  }, [])

  function switchMode(m: 'fill' | 'multileg' | 'import') {
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

  // Read-only: this never writes anything, so unlike submit()/submitMultileg()
  // there is nothing here that must not double-fire -- only the busy guard so
  // two clicks don't race two previews. `accountId` is passed once the
  // needs_account selector has been used; omitted on the first call, since
  // whether it is even needed is exactly what this call finds out.
  async function runPreview(accountId?: string) {
    if (importBusy || commitBusy || !importFile) return
    setImportBusy(true)
    setImportError(null)
    // A fresh preview retires whatever commit result described the LAST one --
    // otherwise a stale success summary could sit above a newly-changed
    // preview and read as if it described the file now on screen.
    setCommitReport(null)
    try {
      const r = await previewImport(importFile, importVenue, accountId)
      setReport(r)
      setPreviewAccountId(accountId)
      setNeedsAccount(r.needs_account)
    } catch (err) {
      setImportError(String(err instanceof Error ? err.message : err))
      setReport(null)
      setPreviewAccountId(undefined)
    } finally {
      setImportBusy(false)
    }
  }

  // The commit step (step 3). Guarded the same way submit()/submitMultileg()
  // are: everything that can throw -- including a stale/renamed file the
  // browser may refuse to re-read -- runs INSIDE this try, so the busy flag
  // set just above can never be left permanently wedged. Double-click safety
  // comes from the `commitBusy` check below, same as `busy` guards the two
  // write forms above.
  async function runCommit() {
    if (
      commitBusy || importBusy || !importFile || !report ||
      report.blocking.length > 0 || (report.needs_account && previewAccountId === undefined)
    ) return
    setCommitBusy(true)
    setImportError(null)
    try {
      const r = await commitImport(importFile, importVenue, previewAccountId)
      setCommitReport(r)
    } catch (err) {
      setImportError(String(err instanceof Error ? err.message : err))
    } finally {
      setCommitBusy(false)
    }
  }

  return (
    <>
      <p className="eyebrow">by hand</p>
      <h1>Entry</h1>

      {/* Fixed three-way switch -- D11: no rearrangeable or configurable
          panes. This is the only way to change modes; the layout of any
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
        <button
          type="button" role="tab" aria-selected={mode === 'import'}
          className={mode === 'import' ? 'active' : undefined}
          onClick={() => switchMode('import')}
        >
          import
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
      ) : mode === 'multileg' ? (
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
      ) : (
        <>
          {/* Step 1: pick. No form/onSubmit -- "preview" is a read GET-like
              action, not a write, so there is nothing here for Enter to
              submit and nothing that needs the busy-wedge discipline the two
              write forms above require (see runPreview: everything that can
              throw is already inside its own try/finally). */}
          <div className="entry">
            <input
              type="file" aria-label="Import file" accept=".csv,.txt"
              onChange={(e) => {
                setImportFile(e.target.files?.[0] ?? null)
                setReport(null)
                setImportError(null)
                setCommitReport(null)
                setPreviewAccountId(undefined)
                // A new file may not need an account at all (or need one for
                // a different reason) -- the old answer is stale the moment
                // the file itself changes.
                setNeedsAccount(false)
              }}
            />
            <select
              value={importVenue} aria-label="Venue"
              onChange={(e) => {
                setImportVenue(e.target.value)
                setReport(null)
                setImportError(null)
                setCommitReport(null)
                setPreviewAccountId(undefined)
                // Same file, different venue's importer can classify rows
                // differently -- whether it needs an account is a fact of
                // (file, venue) together, not of the file alone.
                setNeedsAccount(false)
              }}
            >
              {IMPORT_VENUES.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
            <button
              type="button" onClick={() => runPreview()}
              disabled={importBusy || commitBusy || !importFile}
            >
              {importBusy ? 'reading…' : 'preview'}
            </button>
          </div>

          {importError && <div className="error">{importError}</div>}

          {/* Gated on `needsAccount`, NOT on `report.needs_account` --
              rendered here, OUTSIDE `{report && ...}`, so this selector stays
              mounted even after picking an account clears `report` below.
              Gating it on `report` (as an earlier round did) unmounted this
              exact control the moment it was used: picking an account
              cleared `report`, which hid the selector along with the routing
              table, leaving no way to preview the newly-chosen account
              without abandoning the selection and starting over. */}
          {needsAccount && (
            <section className="section">
              <p className="eyebrow">this file has no per-row account column</p>
              <div className="entry">
                <span className="muted">choose one account for the whole file, then preview again</span>
                {accounts.length === 0 ? (
                  <span className="muted">loading accounts…</span>
                ) : (
                  <select
                    value={importAccount || accounts[0]?.id || ''} aria-label="Import account"
                    onChange={(e) => {
                      // Changing the account invalidates the routing the
                      // last preview computed -- "will import to" was built
                      // from the OLD account, so leaving it on screen next to
                      // a changed dropdown would show two different
                      // destinations for one write. Clearing
                      // report/commitReport/previewAccountId (same pattern as
                      // the file and venue handlers above) forces a
                      // re-preview before commit is possible again, so a
                      // stale routing table can never sit beside a dropdown
                      // it no longer describes. `needsAccount` itself is
                      // deliberately NOT cleared here -- see its declaration.
                      setImportAccount(e.target.value)
                      setReport(null)
                      setCommitReport(null)
                      setPreviewAccountId(undefined)
                    }}
                  >
                    {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                )}
                <button
                  type="button"
                  onClick={() => runPreview(importAccount || accounts[0]?.id)}
                  disabled={importBusy || commitBusy || accounts.length === 0}
                >
                  {importBusy ? 'reading…' : 'preview with this account'}
                </button>
              </div>
            </section>
          )}

          {/* Step 2: an honest preview. Nothing here is invented -- every
              value below is a field PreviewReport actually returned. */}
          {report && (
            <>
              <section className="section">
                <p className="eyebrow">preview</p>
                <div className="statgrid">
                  <div>
                    <span className="lbl">fills</span>
                    <span className="val num">{report.fill_count}</span>
                  </div>
                  <div>
                    <span className="lbl">cash movements</span>
                    <span className="val num">{report.cash_count}</span>
                  </div>
                  <div>
                    <span className="lbl">transfers</span>
                    <span className="val num">{report.transfer_count}</span>
                  </div>
                  <div>
                    <span className="lbl">unmapped rows</span>
                    <span className="val num">{report.unmapped_row_count}</span>
                  </div>
                </div>
                {report.warnings.length > 0 && (
                  <ul className="warnings">
                    {report.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                )}
              </section>

              {/* The positive case: where rows actually land when nothing is
                  wrong. Every other section below this point is about a
                  problem (blocking, unregistered refs) or a deliberate
                  no-op (ignored refs) -- without this, a clean multi-account
                  file previews as silence about the one question a preview
                  exists to answer: is this going where I think it's going. */}
              {report.routing &&
                (report.routing.mapped.length > 0 ||
                  report.routing.unclassified_refs.length > 0) && (
                <section className="section">
                  <p className="eyebrow">will import to</p>
                  {report.routing.mapped.length > 0 && (
                    <table>
                      <tbody>
                        {report.routing.mapped.map(([accountId, count]) => (
                          <tr key={accountId}>
                            <td>{accounts.find((a) => a.id === accountId)?.name ?? accountId}</td>
                            <td className="right num">{count} row{count === 1 ? '' : 's'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {report.routing.unclassified_refs.length > 0 && (
                    // Distinct from "unknown" (unregistered): these accounts
                    // ARE registered, route_batch reached them, and they
                    // simply produced nothing committable -- an empty
                    // statement period, say. Independent of `mapped` on
                    // purpose -- a file whose only ref is registered but
                    // produced no rows has an empty `mapped`, and nesting
                    // this under it (as an earlier round did) silently
                    // dropped the account from the report instead of naming
                    // it, where the CLI's equivalent path prints
                    // "X: 0 row(s) mapped" regardless.
                    <p className="why">
                      registered, but nothing on this file for them to import (no fills, cash
                      movements, or blocking rows): {report.routing.unclassified_refs.join(', ')}
                    </p>
                  )}
                </section>
              )}

              {report.blocking.length > 0 && (
                <section className="section">
                  <p className="eyebrow">blocking -- must be resolved before this can commit</p>
                  {groupBlocking(report.blocking).map(([ref, msgs]) => (
                    <div key={ref ?? '·unassigned·'}>
                      <p className="eyebrow">{ref ?? 'rows with no account ref'}</p>
                      <ul className="warnings">
                        {msgs.map((m, i) => <li key={i}>{m}</li>)}
                      </ul>
                    </div>
                  ))}
                </section>
              )}

              {(report.unknown_money_refs.length > 0 ||
                report.unknown_refs.length > report.unknown_money_refs.length) && (
                <section className="section">
                  <p className="eyebrow">accounts not registered</p>
                  {report.unknown_money_refs.length > 0 && (
                    <div className="error">
                      carrying money -- blocks a commit: {report.unknown_money_refs.join(', ')}.
                      There is no per-account selector here; the fix is to register
                      an account with this external ref first (<span className="num">deadband accounts add --venue {importVenue} --external-ref &lt;ref&gt; --name &lt;name&gt;</span>),
                      then preview again.
                    </div>
                  )}
                  {report.unknown_refs
                    .filter((r) => !report.unknown_money_refs.includes(r))
                    .length > 0 && (
                    <p className="why">
                      also seen, not registered, but carry no money on these rows so they
                      will not block a commit -- still worth registering:{' '}
                      {report.unknown_refs
                        .filter((r) => !report.unknown_money_refs.includes(r))
                        .join(', ')}
                    </p>
                  )}
                </section>
              )}

              {report.ignored_refs.length > 0 && (
                <p className="why">
                  dropped on purpose (registered ignore-on-import), not a failure:{' '}
                  {report.ignored_refs.join(', ')}
                </p>
              )}

              {report.corporate_proposals.length > 0 && (
                <section className="section">
                  <p className="eyebrow">corporate actions detected</p>
                  <ul className="warnings">
                    {report.corporate_proposals.map((p, i) => <li key={i}>{p}</li>)}
                  </ul>
                </section>
              )}

              <section className="section">
                <p className="eyebrow">duplicates</p>
                {report.duplicates ? (
                  <p className="muted">
                    already on file, will be skipped rather than re-inserted on commit:{' '}
                    <span className="num">{report.duplicates.fill_dupes}</span> fill(s),{' '}
                    <span className="num">{report.duplicates.cash_dupes}</span> cash movement(s),{' '}
                    <span className="num">{report.duplicates.transfer_dupes}</span> transfer(s).
                  </p>
                ) : (
                  <p className="muted">
                    not checked -- {DUPLICATE_SKIP_TEXT[report.duplicates_skipped_reason ?? 'no_connection']}
                  </p>
                )}
              </section>

              {/* Step 3: commit. Disabled for exactly the reasons the sections
                  above exist to surface -- a blocking row, or a needs_account
                  file that has not yet been re-previewed with an account
                  chosen -- so this button can never fire a request the server
                  is guaranteed to refuse (import_flow.commit raises
                  BlockingRowsError / UnroutableRowsError for precisely these
                  two cases). `commitBusy` guards the double-click the same
                  way `busy` does for the two write forms above. */}
              <section className="section">
                <button
                  type="button"
                  onClick={runCommit}
                  disabled={
                    importBusy || commitBusy || report.blocking.length > 0 ||
                    (report.needs_account && previewAccountId === undefined)
                  }
                >
                  {commitBusy ? 'committing…' : 'commit import'}
                </button>
                {report.blocking.length > 0 && (
                  <p className="why">resolve the blocking rows above before this can commit.</p>
                )}
                {!report.blocking.length && report.needs_account && previewAccountId === undefined && (
                  <p className="why">preview with an account chosen above before this can commit.</p>
                )}
              </section>

              {/* Step 3 result. The single most important line here is the
                  duplicate-skip case: on a re-import of a file already on
                  file, fills_inserted is legitimately 0 and fills_skipped is
                  the WHOLE file -- content_hash dedupe working exactly as
                  designed, not a failure. Worded and headlined distinctly
                  from the normal case so "0 inserted" reads as the expected,
                  safe outcome of re-dropping in a statement rather than as a
                  bug to go chase. */}
              {commitReport && (
                <section className="section">
                  <p className="eyebrow">
                    {commitReport.fills_inserted === 0 && commitReport.fills_skipped > 0
                      ? 'already imported -- nothing new, nothing failed'
                      : 'commit result'}
                  </p>
                  <div className="statgrid">
                    <div>
                      <span className="lbl">fills inserted</span>
                      <span className="val num">{commitReport.fills_inserted}</span>
                    </div>
                    <div>
                      <span className="lbl">fills skipped (duplicates)</span>
                      <span className="val num">{commitReport.fills_skipped}</span>
                    </div>
                    <div>
                      <span className="lbl">cash movements inserted</span>
                      <span className="val num">{commitReport.cash_inserted}</span>
                    </div>
                    <div>
                      <span className="lbl">transfers inserted</span>
                      <span className="val num">{commitReport.transfers_inserted}</span>
                    </div>
                    <div>
                      <span className="lbl">transfers skipped (duplicates)</span>
                      <span className="val num">{commitReport.transfers_skipped}</span>
                    </div>
                    <div>
                      <span className="lbl">trades regrouped</span>
                      <span className="val num">{commitReport.trades_regrouped}</span>
                    </div>
                  </div>

                  {commitReport.fills_inserted === 0 && commitReport.fills_skipped > 0 && (
                    <p className="why">
                      every one of the {commitReport.fills_skipped} fill(s) in this file matched a
                      fill already on record and was safely skipped as a duplicate -- that is
                      exactly what re-importing a file you have already imported is supposed to
                      do. Zero inserted here means this succeeded with nothing new to add, not
                      that it failed.
                    </p>
                  )}
                  {commitReport.fills_inserted > 0 && commitReport.fills_skipped > 0 && (
                    <p className="why">
                      {commitReport.fills_skipped} of the fills in this file were already on
                      record and were skipped as duplicates rather than re-inserted -- not a
                      failure, just overlap with an earlier import.
                    </p>
                  )}

                  {commitReport.warnings.length > 0 && (
                    <ul className="warnings">
                      {commitReport.warnings.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                  )}

                  {commitReport.ignored_refs.length > 0 && (
                    <p className="why">
                      dropped on purpose (registered ignore-on-import), not a failure:{' '}
                      {commitReport.ignored_refs.join(', ')}
                    </p>
                  )}
                </section>
              )}
            </>
          )}
        </>
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
