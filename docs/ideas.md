# Parked ideas

Things worth doing that are not in a spec yet. Logged so they survive the gap between
having the idea and being ready to build it. Nothing here is committed to.

---

## TradingView MCP as a data source for D

**Logged:** 2026-08-05 · **Relevant to:** D (market data & screeners) · **Status:** parked

`https://github.com/atilaahmettaner/tradingview-mcp` — an MCP server exposing ~37 tools
across screening, technical analysis, quotes, news, and sentiment.

### What it actually provides

| Area | Tools | Underlying source |
|---|---|---|
| Quotes | `yahoo_price`, `market_snapshot`, `stock_prices` | Yahoo Finance |
| Screening | `stock_screener`, `screen_stocks`, `scan_by_signal` | TradingView public screener endpoint (60s cache) |
| Technicals | `get_technical_analysis`, `get_multi_timeframe_analysis`, `get_bollinger_band_analysis`, `get_candlestick_patterns` | `tradingview_ta` library |
| News | `financial_news` | RSS: Yahoo, MarketWatch, CNBC, CoinDesk, CoinTelegraph |
| Sentiment | `market_sentiment`, `combined_analysis` | Reddit; optional Marketaux (free tier 100 req/day) |
| Backtesting | `backtest_strategy`, `compare_strategies`, `walk_forward_backtest_strategy` | 9 built-in strategies |

Covers crypto, stocks, ETFs, indices, forex, and futures. **No TradingView account or API
key required** — it does not log into or automate a TradingView session.

### Honest assessment

**The name oversells it.** This is not TradingView's data. It is Yahoo Finance quotes plus
the `tradingview_ta` library plus TradingView's *public* screener endpoint. Useful, but do
not expect parity with a TradingView Premium subscription, and do not expect access to
anything in a personal TradingView account.

**The backtesting tools are redundant here.** Deadband already delegates strategy testing
to QuantConnect (subsystem E), which runs LEAN against regime-tagged historical data. Nine
canned indicator strategies with a Sharpe number are strictly weaker. Ignore that third of
the surface.

**The screening, quotes, and news thirds are genuinely interesting for D.** That is most of
what the "blink view" needs, from one dependency, with no credentials to manage.

### Conditions if it is adopted

1. **Behind an interface, never coupled to.** D defines `QuoteSource`, `ScreenerSource`,
   and `NewsSource`; this becomes one implementation. Same discipline A uses for `MarkSource`
   and B uses for `AssertionEvaluator`. An unofficial third-party server sitting on public
   endpoints can break or be rate-limited without notice, and nothing in Deadband should
   care when it does.
2. **Marks carry their source.** `mark.source` already exists in A's schema. A price from a
   provider that documents its data as possibly "delayed, inaccurate, or incomplete" must be
   labelled as such, so unrealized P&L is never mistaken for a broker-confirmed number.
3. **Never a reconciliation input.** Statement reconciliation compares against broker
   statements. Third-party quotes are for display and for evaluating B's assertions, not for
   deciding whether the ledger is correct.
4. **Prompt-injection surface.** It returns Reddit posts and RSS headlines — attacker-writable
   text. If that content ever reaches an LLM context with tools attached, it is an injection
   vector. Treat retrieved news and sentiment as untrusted data, never as instructions.
5. **Evaluate self-hosting vs. the hosted tier.** The hosted version rate-limits at
   2,500–10,000 requests/month. Self-hosting removes that and removes a third party from the
   path, at the cost of running it.

### Open question

Whether the screener is expressive enough for the scans actually wanted, or whether D needs
a direct provider (Polygon, Finnhub, Tradier) for the screening path and uses this only for
news and sentiment. Answer that during D's brainstorm by listing the scans wanted first,
then checking them against what this exposes — not the other way around.
