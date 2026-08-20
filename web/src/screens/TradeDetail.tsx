import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { NotFound, fetchTradeDetail, type TradeDetail } from '../api'
import { age, money, pnlClass, qty, shortDate, signedMoney } from '../format'

function timelineLabel(e: { type: string } & Record<string, unknown>): string {
  switch (e.type) {
    case 'opened':
      return 'opened'
    case 'closed':
      return 'closed'
    case 'fill':
    case 'derived_fill':
      return `${e.side} ${qty(e.quantity as string)} @ ${money(e.price as string)}${
        e.type === 'derived_fill' ? ' (derived)' : ''
      }`
    case 'transfer':
      return `transferred out ${qty(e.quantity as string)} — basis left with the shares`
    case 'corporate_action':
      return `${String(e.action_type).replace('_', ' ')} ${qty(e.ratio_numerator as string)}:${qty(
        e.ratio_denominator as string,
      )}`
    default:
      return e.type
  }
}

export default function TradeDetailScreen() {
  const { id } = useParams()
  const [data, setData] = useState<TradeDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    fetchTradeDetail(id)
      .then(setData)
      .catch((e) => setError(e instanceof NotFound ? 'notfound' : String(e)))
  }, [id])

  if (error === 'notfound')
    return (
      <div className="error">
        No trade with this id. <Link to="/trades">Back to the log</Link>
      </div>
    )
  if (error) return <div className="error">trade failed to load — {error}</div>
  if (!data) return <div className="loading">loading…</div>

  const t = data.trade
  const symbol =
    data.effective_instrument?.symbol ?? data.instrument?.symbol ?? t.primary_underlying ?? '—'

  return (
    <>
      <p className="eyebrow">
        <Link to="/trades">trades</Link> / {String(t.id).slice(0, 8)}
      </p>
      <div className="detail-head">
        <span className="sym">{symbol}</span>
        {data.effective_instrument && data.instrument &&
          data.effective_instrument.symbol !== data.instrument.symbol && (
            <span className="muted">né {data.instrument.symbol}</span>
          )}
        <span className={`chip ${t.direction}`}>{t.direction}</span>
        <span className={`chip ${t.status === 'open' ? 'open' : ''}`}>{t.status}</span>
        <span className="muted num">
          {shortDate(t.opened_at)}
          {t.closed_at ? ` → ${shortDate(t.closed_at)}` : ' → open'}
        </span>
      </div>

      <div className="statgrid">
        <div>
          <span className="lbl">realized p&l</span>
          <span className={`val ${pnlClass(data.pnl.realized)}`}>
            {signedMoney(data.pnl.realized)}
          </span>
          <div className="sub">gross {money(data.pnl.gross_realized)}</div>
        </div>
        <div>
          <span className="lbl">unrealized</span>
          <span className={`val ${pnlClass(data.pnl.unrealized)}`}>
            {data.pnl.unrealized == null ? '—' : signedMoney(data.pnl.unrealized)}
          </span>
          <div className="sub">
            {data.pnl.mark
              ? `mark ${money(data.pnl.mark.price)} · ${age(data.pnl.mark.as_of)}`
              : t.status === 'open'
                ? 'no usable mark'
                : 'closed'}
          </div>
        </div>
        <div>
          <span className="lbl">quantity</span>
          <span className="val">
            {qty(t.qty_opened)} <span className="muted">opened</span>
          </span>
          <div className="sub">
            {qty(t.qty_closed ?? '0')} closed
            {t.qty_transferred != null ? ` · ${qty(t.qty_transferred)} transferred out` : ''}
          </div>
        </div>
        <div>
          <span className="lbl">entry / exit</span>
          <span className="val">
            {money(t.avg_entry)} <span className="muted">/</span> {money(t.avg_exit)}
          </span>
          <div className="sub">fees {money(data.pnl.fees_total)} ({money(data.pnl.fees_realized)} realized)</div>
        </div>
        <div>
          <span className="lbl">r-multiple</span>
          <span className="val">{data.r_multiple == null ? '—' : qty(data.r_multiple)}</span>
          <div className="sub">
            {data.r_multiple == null ? 'no planned risk recorded' : 'vs planned risk'}
          </div>
        </div>
      </div>

      {t.qty_transferred != null && (
        <ul className="warnings">
          <li>
            {qty(t.qty_transferred)} shares left via ACAT transfer: the position closed at cost,
            realizing nothing — the equity dip at that date is not a loss.
          </li>
        </ul>
      )}

      <section className="section">
        <p className="eyebrow">timeline</p>
        <ul className="timeline">
          {data.timeline.map((e, i) => (
            <li key={i} className={e.type}>
              <span className="t">{shortDate(e.at as string)}</span>
              {timelineLabel(e)}
            </li>
          ))}
        </ul>
      </section>

      <section className="section">
        <p className="eyebrow">fills</p>
        <table>
          <thead>
            <tr>
              <th>executed</th>
              <th>side</th>
              <th className="right">qty (this trade)</th>
              <th className="right">price</th>
              <th className="right">fee</th>
              <th>source</th>
            </tr>
          </thead>
          <tbody>
            {data.fills.map((f) => (
              <tr key={f.id}>
                <td className="num muted">{shortDate(f.executed_at)}</td>
                <td className="num">{f.side}</td>
                <td className="num right">
                  {qty(f.allocated_quantity)}
                  {f.allocated_quantity !== f.quantity && (
                    <span className="muted"> of {qty(f.quantity)}</span>
                  )}
                </td>
                <td className="num right">{money(f.price)}</td>
                <td className="num right">{money(f.fee)}</td>
                <td className="muted">
                  {f.source === 'derived_fill' ? 'derived' : 'broker'}
                  {f.is_estimated ? ' · estimated' : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {data.notes && (
        <section className="section">
          <p className="eyebrow">notes</p>
          <div className="notes">{data.notes}</div>
        </section>
      )}
    </>
  )
}
