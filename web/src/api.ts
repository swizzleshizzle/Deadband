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

// The Accounts screen. Deliberately carries no equity, headroom or drift:
// those need marks and milestone C, and the endpoint computes nothing it
// cannot back (see api/accounts.py).
export interface AccountSummary {
  id: string
  name: string
  venue: string
  account_type: string
  base_currency: string
  default_intent: string
  is_active: boolean
  ignore_on_import: boolean
  opened_at: string | null
  closed_at: string | null
  cash: string | null
  open_trades: number
  closed_trades: number
  realized_pnl: string | null
  has_rule: boolean
}

export interface FundedRule {
  account_id: string
  max_drawdown: string | null
  drawdown_type: string | null
  daily_loss_limit: string | null
  profit_target: string | null
  payout_split: string | null
  consistency_rule: string | null
  evaluated_at: string | null
}

export interface AccountDetail {
  account: AccountSummary
  funded_rule: FundedRule | null
  open_positions: {
    instrument: { id: string; symbol: string; multiplier: string }
    quantity: string
    // PER-UNIT average cost, not the position total (ledger/positions.py).
    cost_basis: string
    is_estimated: boolean
    unvaluable_reason: string | null
  }[]
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

// A message is required, not optional: some callers deliberately catch this
// by `instanceof` and substitute their own copy ("no such account"), but
// others (Entry.tsx) surface `err.message` straight to the user, and a bare
// `new NotFound()` gives Error's default empty string there -- `{error && ...}`
// then renders nothing at all, so a 404 looks like the form silently did
// nothing rather than reporting what happened.
export class NotFound extends Error {
  constructor(message = 'not found') {
    super(message)
    this.name = 'NotFound'
  }
}

export const fetchHealth = () => get<Health>('/api/health')
export const fetchDashboard = () => get<Dashboard>('/api/dashboard')
export const fetchTrades = (params: URLSearchParams) =>
  get<TradesPage>(`/api/trades?${params.toString()}`)
export const fetchTradeDetail = (id: string) => get<TradeDetail>(`/api/trades/${id}`)
export const fetchAccounts = () => get<{ accounts: AccountSummary[] }>('/api/accounts')
export const fetchAccountDetail = (id: string) => get<AccountDetail>(`/api/accounts/${id}`)

// The Entry screen's write path. Money and quantities stay STRINGS end to end.
export interface FillLegIn {
  symbol: string
  side: 'buy' | 'sell'
  quantity: string
  price: string
  fee: string
  fee_currency: string
  executed_at: string
}

export interface CreatedFills {
  fill_ids: string[]
  trades_regrouped: number
}

// FastAPI error bodies are JSON ({"detail": ...}), not plain text. Rendering
// that raw shows the user literal braces and quotes for the single most
// common mistake a write form invites (e.g. a bad quantity). `detail` is a
// string for most 4xx errors; pydantic validation failures instead carry an
// array of {loc, msg, ...} objects. Fall back to the raw text only when the
// body isn't JSON at all (e.g. a proxy error page).
async function errorMessage(r: Response, path: string): Promise<string> {
  const text = await r.text()
  try {
    const body = JSON.parse(text) as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((d) =>
          d && typeof d === 'object' && 'msg' in d
            ? String((d as { msg: unknown }).msg)
            : JSON.stringify(d),
        )
        .join('; ')
    }
  } catch {
    // not JSON -- fall through to the raw text below
  }
  return text || `${path}: ${r.status}`
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (r.status === 404) throw new NotFound()
  if (!r.ok) throw new Error(await errorMessage(r, path))
  return r.status === 204 ? (undefined as T) : ((await r.json()) as T)
}

export const createFills = (body: { account_id: string; fills: FillLegIn[] }) =>
  send<CreatedFills>('/api/fills', 'POST', body)
export const deleteFill = (id: string) => send<void>(`/api/fills/${id}`, 'DELETE')
