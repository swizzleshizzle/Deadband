import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchDashboard, type Dashboard as Payload } from '../api'
import DriftGauge from '../components/DriftGauge'
import { age, money, pnlClass, qty, shortDate, signedMoney } from '../format'

export default function Dashboard() {
  const [data, setData] = useState<Payload | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchDashboard().then(setData).catch((e) => setError(String(e)))
  }, [])

  if (error) return <div className="error">dashboard failed to load — {error}</div>
  if (!data) return <div className="loading">loading…</div>

  const names = new Map(data.accounts.map((a) => [a.id, a.name]))

  return (
    <>
      <p className="eyebrow">aggregate equity · marked</p>
      <div className="hero">
        <span className="total num">
          {data.equity.total == null ? '—' : money(data.equity.total)}
        </span>
        <span className="basis">
          {data.equity.total == null
            ? 'not totalled: at least one holding is unvalued (a partial sum is never a total)'
            : `cash + positions at latest marks · ${shortDate(data.generated_at)}`}
        </span>
      </div>

      {data.drift_warnings.length > 0 && (
        <ul className="warnings">
          {data.drift_warnings.map((w, i) => (
            <li key={i}>
              {names.get(w.account_id) ?? w.account_id}: {w.verdict} — {w.detail}
            </li>
          ))}
        </ul>
      )}

      <section className="section">
        <p className="eyebrow">accounts</p>
        <div className="tiles">
          {data.accounts.map((a) => (
            <div className="tile" key={a.id}>
              <div className="name">{a.name}</div>
              <div className="venue">
                {a.venue} · {a.account_type}
                {a.is_active ? '' : ' · inactive'}
              </div>
              <div className="figures">
                <span>
                  <span className="lbl">cash</span>
                  <span className="val num">{money(a.cash)}</span>
                </span>
                <span>
                  <span className="lbl">equity</span>
                  <span className="val num">{money(a.equity)}</span>
                </span>
                {a.snapshot && (
                  <span>
                    <span className="lbl">statement {age(a.snapshot.as_of)}</span>
                    <span className="val num muted">{money(a.snapshot.total_equity)}</span>
                  </span>
                )}
              </div>
              {a.equity == null && (
                <div className="why">equity withheld — a holding here has no usable mark</div>
              )}
              {a.drift ? (
                <DriftGauge verdict={a.drift.verdict} amount={a.drift.amount} />
              ) : (
                <div className="why">no statement snapshot to reconcile against</div>
              )}
            </div>
          ))}
        </div>
      </section>

      <section className="section">
        <p className="eyebrow">open positions</p>
        {data.open_positions.length === 0 ? (
          <div className="empty">nothing held — the book is flat</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>account</th>
                <th>symbol</th>
                <th className="right">qty</th>
                <th className="right">basis /u</th>
                <th className="right">mark</th>
                <th className="right">value</th>
                <th className="right">unrealized</th>
              </tr>
            </thead>
            <tbody>
              {data.open_positions.map((p, i) => (
                <tr key={i}>
                  <td className="muted">{names.get(p.account_id) ?? '?'}</td>
                  <td className="num">{p.instrument.symbol}</td>
                  <td className="num right">{qty(p.quantity)}</td>
                  <td className="num right">{money(p.cost_basis)}</td>
                  <td className="num right">
                    {p.mark ? (
                      <>
                        {money(p.mark.price)} <span className="muted">{age(p.mark.as_of)}</span>
                      </>
                    ) : (
                      <span className="muted">no mark</span>
                    )}
                  </td>
                  <td className="num right">{money(p.market_value)}</td>
                  <td className={`num right ${pnlClass(p.unrealized_pnl)}`}>
                    {signedMoney(p.unrealized_pnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data.unvaluable.length > 0 && (
          <ul className="warnings">
            {data.unvaluable.map((u, i) => (
              <li key={i}>
                {u.instrument.symbol}: {u.reason}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="section">
        <p className="eyebrow">recent activity</p>
        {data.recent_activity.length === 0 ? (
          <div className="empty">no activity recorded yet — import an export to begin</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>date</th>
                <th>account</th>
                <th>event</th>
                <th className="right">amount</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_activity.map((e, i) => (
                <tr key={i}>
                  <td className="num muted">{shortDate(e.at)}</td>
                  <td className="muted">{names.get(e.account_id) ?? '?'}</td>
                  <td className="num">
                    {e.type === 'fill'
                      ? `${e.side} ${qty(e.quantity)} ${e.symbol ?? ''} @ ${money(e.price)}`
                      : e.type === 'transfer'
                        ? `transfer out ${qty(e.quantity)} ${e.symbol ?? ''}`
                        : `${e.kind}${e.symbol ? ` · ${e.symbol}` : ''}`}
                  </td>
                  <td className="num right">
                    {e.type === 'fill' ? '' : money(e.amount)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <p className="section muted" style={{ fontSize: 11.5 }}>
        Read-only view. Marks are manual — every valuation shows the mark's age rather than
        pretending to be live. <Link to="/trades">Browse trades →</Link>
      </p>
    </>
  )
}
