// Shared by every form that reads a `datetime-local` input. Extracted from
// Entry.tsx rather than copied: `datetime-local` yields "2026-08-28T14:02"
// with NO zone, and stamping "Z" on it claims a UTC wall-clock reading that
// is wrong by the browser's offset. That silently shifts a fill's
// executed_at, and grouping orders fills by executed_at, so it can reorder
// trades. `new Date(local)` parses an offset-less string as LOCAL time (this
// also handles the with-seconds form), so `.toISOString()` yields the true
// UTC instant.
//
// This box runs UTC, so the bug is INVISIBLE to any test run here -- a green
// local run proves nothing about it. That is exactly why there is one copy
// of this function and not two.
//
// An empty or unparseable value must throw HERE rather than produce
// `Invalid Date` silently -- callers rely on this landing in their own
// try/catch so a bad date can never wedge a busy flag.
export function toInstant(local: string, label = 'executed at'): string {
  const d = new Date(local)
  if (Number.isNaN(d.getTime())) throw new Error(`${label}: enter a date and time`)
  return d.toISOString()
}
