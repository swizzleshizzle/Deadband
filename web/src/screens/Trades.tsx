import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { fetchTrades, type TradesPage } from '../api'
import { money, pnlClass, qty, shortDate, signedMoney } from '../format'

const PAGE = 50
const FILTERS = ['status', 'intent', 'instrument', 'tag', 'from', 'to'] as const

export default function Trades() {
  const [params, setParams] = useSearchParams()
  const [page, setPage] = useState<TradesPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  const offset = Number(params.get('offset') ?? '0')
  const query = useMemo(() => {
    const q = new URLSearchParams()
    for (const key of FILTERS) {
      const v = params.get(key)
      if (v) q.set(key, v)
    }
    q.set('limit', String(PAGE))
    q.set('offset', String(offset))
    return q
  }, [params, offset])

  useEffect(() => {
    setPage(null)
    fetchTrades(query).then(setPage).catch((e) => setError(String(e)))
  }, [query])

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
              <th>opened</th>
              <th>symbol</th>
              <th>dir</th>
              <th>status</th>
              <th className="right">qty</th>
              <th className="right">entry</th>
              <th className="right">exit</th>
              <th className="right">realized</th>
              <th className="right">r</th>
              <th>tag</th>
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
