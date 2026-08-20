// The read-only API contract (docs/superpowers/specs/2026-08-19-read-only-api-design.md).
// Money and quantities arrive as STRINGS and stay strings until display.

export interface Health {
  db: boolean
  migrations_current: boolean
  pending_migrations: string[]
}

export interface AccountTile {
  id: string
  name: string
  venue: string
  account_type: string
  base_currency: string
  is_active: boolean
  cash: string | null
  equity: string | null
  snapshot: { as_of: string; total_equity: string; cash_balance: string } | null
  drift: { verdict: string; amount: string | null } | null
}

export interface OpenPosition {
  account_id: string
  instrument: { id: string; symbol: string; multiplier: string }
  quantity: string
  cost_basis: string | null
  mark: { price: string; as_of: string } | null
  market_value: string | null
  unrealized_pnl: string | null
  is_estimated: boolean
}

export interface ActivityEvent {
  type: 'fill' | 'cash_movement' | 'transfer'
  at: string
  account_id: string
  symbol: string | null
  side: string | null
  quantity: string | null
  price: string | null
  kind: string | null
  amount: string | null
}

export interface Dashboard {
  generated_at: string
  equity: { total: string | null; basis: string }
  accounts: AccountTile[]
  open_positions: OpenPosition[]
  recent_activity: ActivityEvent[]
  unvaluable: { instrument: { symbol: string }; account_id: string; reason: string }[]
  drift_warnings: { account_id: string; verdict: string; detail: string }[]
}

export interface TradeRow {
  id: string
  account_id: string
  primary_underlying: string | null
  instrument_symbol: string | null
  direction: string
  status: string
  intent: string
  opened_at: string
  closed_at: string | null
  qty_opened: string | null
  qty_closed: string | null
  qty_transferred: string | null
  avg_entry: string | null
  avg_exit: string | null
  realized_pnl: string | null
  fees_total: string | null
  r_multiple: string | null
  strategy_tag: string | null
  is_estimated: boolean
  notes: string | null
}

export interface TradesPage {
  trades: TradeRow[]
  total: number
  limit: number
  offset: number
}

export interface TradeDetail {
  trade: TradeRow & Record<string, unknown>
  instrument: { symbol: string } | null
  effective_instrument: { symbol: string } | null
  fills: {
    source: 'fill' | 'derived_fill'
    id: string
    executed_at: string
    side: string
    quantity: string
    price: string
    fee: string
    is_estimated: boolean
    allocated_quantity: string
  }[]
  timeline: ({ type: string; at: string } & Record<string, unknown>)[]
  pnl: {
    realized: string | null
    gross_realized: string | null
    fees_total: string | null
    fees_realized: string | null
    unrealized: string | null
    mark: { price: string; as_of: string } | null
  }
  r_multiple: string | null
  notes: string | null
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (r.status === 404) throw new NotFound()
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return (await r.json()) as T
}

export class NotFound extends Error {}

export const fetchHealth = () => get<Health>('/api/health')
export const fetchDashboard = () => get<Dashboard>('/api/dashboard')
export const fetchTrades = (params: URLSearchParams) =>
  get<TradesPage>(`/api/trades?${params.toString()}`)
export const fetchTradeDetail = (id: string) => get<TradeDetail>(`/api/trades/${id}`)
