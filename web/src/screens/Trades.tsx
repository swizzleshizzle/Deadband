import { useEffect, useMemo, useState } from 'react'

// [sort key, right-aligned]. The keys are the ones api/trades.py's `sort`
// Literal accepts -- an unlisted one is a 422, so these must stay in step.
const SORT_COLUMNS: [string, boolean][] = [
  ['opened', false], ['symbol', false], ['dir', false], ['status', false],
  ['qty', true], ['entry', true], ['exit', true], ['realized', true],
  ['r', true], ['tag', false],
]
import { useNavigate, useSearchParams } from 'react-router-dom'
import { fetchAccounts, fetchTrades, type AccountSummary, type TradesPage } from '../api'
import { money, pnlClass, qty, shortDate, signedMoney } from '../format'

const PAGE = 50
const FILTERS = ['account', 'status', 'intent', 'instrument', 'tag', 'from', 'to'] as const

export default function Trades() {
  const [params, setParams] = useSearchParams()
  const [page, setPage] = useState<TradesPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const navigate = useNavigate()

  // Names, not ids: /api/trades filters by account id, but a uuid in a
  // dropdown is unreadable. Failure is silent and leaves the filter absent
  // rather than breaking the log over a secondary control.
  useEffect(() => {
    fetchAccounts()
      .then((r) => setAccounts(r.accounts))
      .catch(() => setAccounts([]))
  }, [])

  const offset = Number(params.get('offset') ?? '0')
  // Sort lives in the URL beside the filters, not in component state: a
  // sorted view is then shareable and survives a refresh, and the two cannot
  // disagree about what the table is showing.
  const sortKey = params.get('sort') ?? 'opened'
  const sortDir = params.get('dir') ?? 'desc'
  const query = useMemo(() => {
    const q = new URLSearchParams()
    for (const key of FILTERS) {
      const v = params.get(key)
      if (v) q.set(key, v)
    }
    q.set('sort', sortKey)
    q.set('dir', sortDir)
    q.set('limit', String(PAGE))
    q.set('offset', String(offset))
    return q
  }, [params, offset, sortKey, sortDir])

  useEffect(() => {
    setPage(null)
    fetchTrades(query).then(setPage).catch((e) => setError(String(e)))
  }, [query])

  // Column order is SERVER-side (api/trades.py). Sorting the 50 rows already
  // on screen would answer "the biggest win on this page", which looks
  // identical to the real answer and is not it.
  function toggleSort(key: string) {
    const next = new URLSearchParams(params)
    next.set('sort', key)
    // Same column flips direction; a new column starts descending, which is
    // what "show me the biggest" means for every numeric column here.
    next.set('dir', key === sortKey && sortDir === 'desc' ? 'asc' : 'desc')
    next.delete('offset') // a re-sort restarts paging, same as a new filter
    setParams(next, { replace: true })
  }

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('offset') // a new filter restarts paging
    setParams(next, { replace: true })
  }

  return (
    <>
      <p className="eyebrow">the log</p>
      <h1>Trades</h1>

      <div className="filters">
        {accounts.length > 0 && (
          <select
            value={params.get('account') ?? ''}
            onChange={(e) => setFilter('account', e.target.value)}
            aria-label="Account"
          >
            <option value="">any account</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        )}
        <select
          value={params.get('status') ?? ''}
          onChange={(e) => setFilter('status', e.target.value)}
          aria-label="Status"
        >
          <option value="">any status</option>
          <option value="open">open</option>
          <option value="closed">closed</option>
        </select>
        <select
          value={params.get('intent') ?? ''}
          onChange={(e) => setFilter('intent', e.target.value)}
          aria-label="Intent"
        >
          <option value="">any intent</option>
          <option value="trade">trade</option>
          <option value="investment">investment</option>
          <option value="unassigned">unassigned</option>
        </select>
        <input
          placeholder="underlying"
          defaultValue={params.get('instrument') ?? ''}
          onKeyDown={(e) => {
            if (e.key === 'Enter') setFilter('instrument', e.currentTarget.value.trim())
          }}
          onBlur={(e) => setFilter('instrument', e.target.value.trim())}
          size={9}
          aria-label="Underlying symbol"
        />
        <input
          placeholder="tag"
          defaultValue={params.get('tag') ?? ''}
          onKeyDown={(e) => {
            if (e.key === 'Enter') setFilter('tag', e.currentTarget.value.trim())
          }}
          onBlur={(e) => setFilter('tag', e.target.value.trim())}
          size={9}
          aria-label="Strategy tag"
        />
        <input
          type="date"
          value={params.get('from') ?? ''}
          onChange={(e) => setFilter('from', e.target.value)}
          aria-label="Opened from"
        />
        <input
          type="date"
          value={params.get('to') ?? ''}
          onChange={(e) => setFilter('to', e.target.value)}
          aria-label="Opened to"
        />
        {page && (
          <span className="count">
            {page.total} trade{page.total === 1 ? '' : 's'}
          </span>
        )}
      </div>

      {error ? (
        <div className="error">trades failed to load — {error}</div>
      ) : !page ? (
        <div className="loading">loading…</div>
      ) : page.trades.length === 0 ? (
        <div className="empty">no trades match these filters</div>
      ) : (
        <table>
          <thead>
            <tr>
              {SORT_COLUMNS.map(([key, right]) => (
                <th key={key} className={right ? 'right' : undefined} aria-sort={
                  sortKey === key ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'
                }>
                  <button type="button" className="sort" onClick={() => toggleSort(key)}>
                    {key}
                    <span className="arrow">
                      {sortKey === key ? (sortDir === 'asc' ? '▲' : '▼') : ''}
                    </span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {page.trades.map((t) => (
              <tr
                key={t.id}
                className="rowlink"
                tabIndex={0}
                onClick={() => navigate(`/trades/${t.id}`)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') navigate(`/trades/${t.id}`)
                }}
              >
                <td className="num muted">{shortDate(t.opened_at)}</td>
                <td className="num">
                  {t.instrument_symbol ?? t.primary_underlying ?? '—'}
                  {t.is_estimated && <span className="muted"> ≈</span>}
                </td>
                <td>
                  <span className={`chip ${t.direction}`}>{t.direction}</span>
                </td>
                <td>
                  <span className={`chip ${t.status === 'open' ? 'open' : ''}`}>
                    {t.status}
                    {t.qty_transferred != null ? ' · xfer' : ''}
                  </span>
                </td>
                <td className="num right">{qty(t.qty_opened)}</td>
                <td className="num right">{money(t.avg_entry)}</td>
                <td className="num right">{money(t.avg_exit)}</td>
                <td className={`num right ${pnlClass(t.realized_pnl)}`}>
                  {signedMoney(t.realized_pnl)}
                </td>
                <td className="num right">{t.r_multiple == null ? '—' : qty(t.r_multiple)}</td>
                <td className="muted">{t.strategy_tag ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {page && page.total > PAGE && (
        <div className="pager">
          <button
            disabled={offset === 0}
            onClick={() => {
              const next = new URLSearchParams(params)
              next.set('offset', String(Math.max(0, offset - PAGE)))
              setParams(next, { replace: true })
            }}
          >
            ← newer
          </button>
          <span className="num muted">
            {offset + 1}–{Math.min(offset + PAGE, page.total)} of {page.total}
          </span>
          <button
            disabled={offset + PAGE >= page.total}
            onClick={() => {
              const next = new URLSearchParams(params)
              next.set('offset', String(offset + PAGE))
              setParams(next, { replace: true })
            }}
          >
            older →
          </button>
        </div>
      )}
    </>
  )
}
