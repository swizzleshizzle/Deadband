import { useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { fetchHealth, type Health } from './api'
import AccountDetailScreen from './screens/AccountDetail'
import Accounts from './screens/Accounts'
import Dashboard from './screens/Dashboard'
import TradeDetailScreen from './screens/TradeDetail'
import Trades from './screens/Trades'

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null))
  }, [])

  return (
    <div className="frame">
      <nav className="rail">
        <div className="wordmark">
          dead<span>band</span>
        </div>
        <NavLink to="/" end>
          Dashboard
        </NavLink>
        <NavLink to="/accounts">Accounts</NavLink>
        <NavLink to="/trades">Trades</NavLink>
        <div className="health">
          {health === null ? (
            <span className="bad">api unreachable</span>
          ) : !health.db ? (
            <span className="bad">db down</span>
          ) : !health.migrations_current ? (
            <span className="bad">schema behind: {health.pending_migrations.join(', ')}</span>
          ) : (
            <span>db ok · schema current</span>
          )}
        </div>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/accounts/:id" element={<AccountDetailScreen />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/trades/:id" element={<TradeDetailScreen />} />
        </Routes>
      </main>
    </div>
  )
}
