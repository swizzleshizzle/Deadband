// The deadband: reconcile's verdict as a band gauge. The shaded band is the
// tolerance zone; the needle is the equity drift. Position is display-only
// and log-scaled so a small drift is visibly off-centre without a large one
// pinning the needle -- the NUMBER next to it is the datum.
import { money } from '../format'

export default function DriftGauge({
  verdict,
  amount,
}: {
  verdict: string
  amount: string | null
}) {
  const n = amount == null ? 0 : Number(amount)
  const mag = Math.min(Math.log10(1 + Math.abs(n)) / 4, 1) // $10k ≈ full scale
  const pos = 50 + (n < 0 ? -1 : 1) * mag * 48
  return (
    <div className={`gauge ${verdict}`}>
      <div className="track">
        <div className="band" />
        <div className="needle" style={{ left: `calc(${pos}% - 1px)` }} />
      </div>
      <div className="legend">
        <span>{verdict}</span>
        <span>{amount == null ? '—' : `${money(amount)} drift`}</span>
      </div>
    </div>
  )
}
