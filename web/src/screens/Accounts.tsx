import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAccounts, type AccountSummary } from '../api'
import { money, pnlClass, signedMoney } from '../format'

export default function Accounts() {
  const [accounts, setAccounts] = useState<AccountSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    fetchAccounts()
      .then((r) => setAccounts(r.accounts))
      .catch((e) => setError(String(e)))
  }, [])

  if (error) return <div className="error">accounts failed to load — {error}</div>
  if (!accounts) return <div className="loading">loading…</div>
  if (accounts.length === 0) return <div className="empty">no accounts yet</div>

  return (
    <>
      <p className="eyebrow">where the money lives</p>
      <h1>Accounts</h1>

      <table>
        <thead>
          <tr>
            <th>account</th>
            <th>venue</th>
            <th>type</th>
            <th className="right">cash</th>
            <th className="right">realized</th>
            <th className="right">open</th>
            <th className="right">closed</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr
              key={a.id}
              className="rowlink"
              tabIndex={0}
              onClick={() => navigate(`/accounts/${a.id}`)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') navigate(`/accounts/${a.id}`)
              }}
            >
              <td>
                {a.name}
                {!a.is_active && <span className="muted"> · inactive</span>}
                {a.ignore_on_import && <span className="muted"> · ignored on import</span>}
              </td>
              <td className="muted">{a.venue}</td>
              <td>
                <span className="chip">{a.account_type}</span>
                {a.has_rule && <span className="chip"> rules</span>}
              </td>
              {/* An empty cash cell is not zero. account_cash refuses an
                  account whose movements span currencies, because v1 has no
                  FX; the dash means "no single number is true here". */}
              <td className="num right">{money(a.cash)}</td>
              <td className={`num right ${pnlClass(a.realized_pnl)}`}>
                {signedMoney(a.realized_pnl)}
              </td>
              <td className="num right">{a.open_trades}</td>
              <td className="num right muted">{a.closed_trades}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
