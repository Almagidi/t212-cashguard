export default function RiskLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Header */}
      <div className="space-y-2">
        <div className="h-7 w-36 rounded-lg bg-muted/40" />
        <div className="h-4 w-72 rounded bg-muted/30" />
      </div>

      {/* Risk stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-4 space-y-2">
            <div className="h-3.5 w-24 rounded bg-muted/40" />
            <div className="h-7 w-20 rounded-md bg-muted/30" />
            <div className="h-3 w-16 rounded bg-muted/20" />
          </div>
        ))}
      </div>

      {/* Risk profile form card */}
      <div className="rounded-xl border border-border/60 bg-card p-5 space-y-5">
        <div className="h-5 w-32 rounded bg-muted/40" />
        <div className="grid sm:grid-cols-2 gap-4">
          {[1, 2, 3, 4, 5, 6].map(i => (
            <div key={i} className="space-y-1.5">
              <div className="h-3.5 w-28 rounded bg-muted/40" />
              <div className="h-9 rounded-md bg-muted/30" />
            </div>
          ))}
        </div>
        <div className="h-9 w-28 rounded-md bg-primary/20 mt-2" />
      </div>

      {/* Gauges / charts row */}
      <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {[1, 2, 3].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-5 space-y-3">
            <div className="h-4 w-24 rounded bg-muted/40" />
            <div className="h-[120px] rounded-lg bg-muted/20" />
          </div>
        ))}
      </div>
    </div>
  )
}
