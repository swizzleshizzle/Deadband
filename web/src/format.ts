// Display formatting for the API's exact-decimal strings. Numbers stay
// strings end to end; these helpers only bound the DISPLAY scale -- the same
// posture as cli.py's _DISPLAY_QUANT (a silently wrong "0.00" for a
// satoshi-scale value is the worst outcome, so small magnitudes keep digits).

const MONEY = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function money(value: string | null | undefined): string {
  if (value == null) return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return value
  if (n !== 0 && Math.abs(n) < 0.01) return trimZeros(value)
  return MONEY.format(n)
}

export function signedMoney(value: string | null | undefined): string {
  if (value == null) return '—'
  const n = Number(value)
  return (n > 0 ? '+' : '') + money(value)
}

export function qty(value: string | null | undefined): string {
  if (value == null) return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return trimZeros(value)
  // Quantities that reach here are often DERIVED, not stored: a corporate
  // action's ratio division under ledger/reconcile.py's `ctx.prec = 50`
  // produces repeating decimals, so a third rendered 50 digits wide and blew
  // the QTY column into its neighbours. No stored fill carries more than 6
  // decimal places, so rounding the display discards arithmetic noise, never
  // data.
  //
  // The small-magnitude escape is the same posture money() takes directly
  // above, and this file's header states why: a satoshi-scale holding
  // flattened to "0.000" would read as nothing held, which is worse than an
  // ugly number. Below the rounding threshold, keep every digit.
  if (n !== 0 && Math.abs(n) < 0.001) return trimZeros(value)
  // toFixed, not Intl.NumberFormat: the latter would introduce thousands
  // separators this column has never had, changing every row to fix a few.
  return trimZeros(n.toFixed(3))
}

function trimZeros(value: string): string {
  if (!value.includes('.')) return value
  return value.replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '')
}

export function pnlClass(value: string | null | undefined): string {
  if (value == null) return ''
  const n = Number(value)
  if (n > 0) return 'gain'
  if (n < 0) return 'loss'
  return ''
}

export function shortDate(iso: string): string {
  return iso.slice(0, 10)
}

export function age(iso: string): string {
  const then = new Date(iso).getTime()
  const days = Math.floor((Date.now() - then) / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return '1d ago'
  if (days < 60) return `${days}d ago`
  return `${Math.floor(days / 30)}mo ago`
}
