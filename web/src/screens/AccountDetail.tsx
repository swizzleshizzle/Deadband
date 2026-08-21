import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchAccountDetail, NotFound, type AccountDetail as Payload } from '../api'
import { money, pnlClass, qty, shortDate, signedMoney } from '../format'

export default function AccountDetailScreen() {
  const { id } = useParams()
  const [data, setData] = useState<Payload | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    setData(null)
    fetchAccountDetail(id)
      .then(setData)
      .catch((e) => setError(e instanceof NotFound ? 'no such account' : String(e)))
  }, [id])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="loading">loading…</div>

  const a = data.account

  return (
    <>
      <p className="eyebrow">
        <Link to="/accounts">accounts</Link> · {a.venue}
      </p>
      <h1>{a.name}</h1>

      <div className="figures">
        <span>
          <span className="lbl">cash</span>
          <span className="val num">{money(a.cash)}</span>
        </span>
        <span>
          <span className="lbl">realized p&l</span>
          <span className={`val num ${pnlClass(a.realized_pnl)}`}>
            {signedMoney(a.realized_pnl)}
          </span>
        </span>
        <span>
          <span className="lbl">trades</span>
          <span className="val num">
            {a.open_trades} open · {a.closed_trades} closed
          </span>
        </span>
      </div>

      {a.cash == null && (
        <div className="why">
          cash withheld — this account's movements span more than one currency, and v1 does
          not model FX
        </div>
      )}

      <section className="section">
        <p className="eyebrow">configuration</p>
        <dl className="config">
          <dt>type</dt>
          <dd>{a.account_type}</dd>
          <dt>base currency</dt>
          <dd>{a.base_currency}</dd>
          <dt>default intent</dt>
          <dd>{a.default_intent}</dd>
          <dt>status</dt>
          <dd>{a.is_active ? 'active' : 'inactive'}</dd>
          <dt>opened</dt>
          <dd>{a.opened_at ? shortDate(a.opened_at) : '—'}</dd>
          {a.closed_at && (
            <>
              <dt>closed</dt>
              <dd>{shortDate(a.closed_at)}</dd>
            </>
          )}
          {a.ignore_on_import && (
            <>
              <dt>imports</dt>
              <dd>ignored</dd>
            </>
          )}
        </dl>
      </section>

      {/* Rules are shown exactly as recorded. Headroom -- distance to
          drawdown breach, to the daily loss limit, progress to target -- is
          milestone C and needs marks, so nothing here computes against these
          numbers. Showing them raw beats showing a headroom bar built on an
          equity figure that does not exist. */}
      {data.funded_rule && (
        <section className="section">
          <p className="eyebrow">funded-account rules</p>
          <dl className="config">
            <dt>max drawdown</dt>
            <dd className="num">
              {money(data.funded_rule.max_drawdown)}
              {data.funded_rule.drawdown_type && (
                <span className="muted"> · {data.funded_rule.drawdown_type}</span>
              )}
            </dd>
            <dt>daily loss limit</dt>
            <dd className="num">{money(data.funded_rule.daily_loss_limit)}</dd>
            <dt>profit target</dt>
            <dd className="num">{money(data.funded_rule.profit_target)}</dd>
            <dt>payout split</dt>
            <dd className="num">
              {data.funded_rule.payout_split == null ? '—' : qty(data.funded_rule.payout_split)}
            </dd>
            {data.funded_rule.consistency_rule && (
              <>
                <dt>consistency</dt>
                <dd>{data.funded_rule.consistency_rule}</dd>
              </>
            )}
          </dl>
          <div className="why">
            headroom against these rules needs current equity, which needs marks — not
            computed yet
          </div>
        </section>
      )}

      <section className="section">
        <p className="eyebrow">open positions</p>
        {data.open_positions.length === 0 ? (
          <div className="empty">nothing open</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>symbol</th>
                <th className="right">qty</th>
                {/* Named "avg cost", not "cost basis": the API's cost_basis is
                    per-unit, and the longer name reads as a position total. */}
                <th className="right">avg cost</th>
              </tr>
            </thead>
            <tbody>
              {data.open_positions.map((p) => (
                <tr key={p.instrument.id}>
                  <td className="num">
                    {p.instrument.symbol}
                    {p.is_estimated && <span className="muted"> ≈</span>}
                    {p.unvaluable_reason && (
                      <span className="muted"> · {p.unvaluable_reason}</span>
                    )}
                  </td>
                  <td className="num right">{qty(p.quantity)}</td>
                  <td className="num right">{money(p.cost_basis)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="why">
          positions are not valued here — <Link to="/">the dashboard</Link> owns valuation, so
          the two screens cannot disagree
        </p>
      </section>

      <section className="section">
        <Link to={`/trades?account=${a.id}`}>see this account's trades →</Link>
      </section>
    </>
  )
}
