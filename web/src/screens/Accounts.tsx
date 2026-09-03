import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAccounts, renameAccount, type AccountSummary } from '../api'
import { money, pnlClass, signedMoney } from '../format'

export default function Accounts() {
  const [accounts, setAccounts] = useState<AccountSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Kept SEPARATE from `error`, which is the load failure and short-circuits
  // the whole screen with an early return below. Reusing it for a rename
  // meant a rejected name replaced the entire table with "accounts failed to
  // load", which is both destructive and untrue -- the accounts had loaded
  // fine. Found by driving the form, not by reading it.
  const [renameError, setRenameError] = useState<string | null>(null)
  // Which row is being renamed, and the in-progress text. Held here rather
  // than per-row so only one rename can be open at a time.
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const navigate = useNavigate()

  async function commitRename(id: string) {
    if (saving) return
    setSaving(true)
    setRenameError(null)
    try {
      const r = await renameAccount(id, draft)
      // Patch the one row from the server's answer rather than refetching the
      // list: the server trims, so what it returns is not always what was
      // typed, and showing the typed value would briefly lie.
      setAccounts((prev) =>
        prev ? prev.map((a) => (a.id === id ? { ...a, name: r.name } : a)) : prev,
      )
      setEditing(null)
    } catch (err) {
      setRenameError(String(err instanceof Error ? err.message : err))
    } finally {
      setSaving(false)
    }
  }

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

      {/* Rendered ABOVE the table, never instead of it: a rejected rename
          must not take the accounts off screen. */}
      {renameError && <div className="error">rename failed — {renameError}</div>}

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
                {/* Every interactive control in this cell stops propagation:
                    the whole ROW is a link to the account detail, so without
                    it a click meant for the rename field navigates away and
                    the edit is lost. */}
                {editing === a.id ? (
                  <span className="rename" onClick={(e) => e.stopPropagation()}>
                    <input
                      autoFocus
                      value={draft}
                      disabled={saving}
                      aria-label={`new name for ${a.name}`}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        // The row itself navigates on Enter; this must not
                        // reach it.
                        e.stopPropagation()
                        if (e.key === 'Enter') void commitRename(a.id)
                        if (e.key === 'Escape') setEditing(null)
                      }}
                    />
                    <button type="button" disabled={saving} onClick={() => void commitRename(a.id)}>
                      {saving ? 'saving…' : 'save'}
                    </button>
                    <button type="button" disabled={saving} onClick={() => setEditing(null)}>
                      cancel
                    </button>
                  </span>
                ) : (
                  <>
                    {a.name}
                    <button
                      type="button"
                      className="rename-open"
                      aria-label={`rename ${a.name}`}
                      onClick={(e) => {
                        e.stopPropagation()
                        setEditing(a.id)
                        setDraft(a.name)
                        setRenameError(null)
                      }}
                    >
                      rename
                    </button>
                  </>
                )}
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
