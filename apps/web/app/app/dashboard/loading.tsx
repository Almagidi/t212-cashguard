export default function DashboardLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="space-y-2">
          <div className="h-7 w-36 rounded-lg bg-muted/40" />
          <div className="h-4 w-56 rounded bg-muted/30" />
        </div>
        <div className="flex gap-2">
          <div className="h-8 w-24 rounded-md bg-muted/30" />
          <div className="h-8 w-24 rounded-md bg-muted/30" />
        </div>
      </div>

      {/* Stat cards row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="stat-card space-y-3">
            <div className="flex items-center justify-between">
              <div className="h-3.5 w-20 rounded bg-muted/40" />
              <div className="h-5 w-5 rounded bg-muted/30" />
            </div>
            <div className="h-8 w-28 rounded-md bg-muted/30" />
            <div className="h-3 w-16 rounded bg-muted/20" />
          </div>
        ))}
      </div>

      {/* Second stat row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="stat-card space-y-3">
            <div className="h-3.5 w-20 rounded bg-muted/40" />
            <div className="h-8 w-28 rounded-md bg-muted/30" />
          </div>
        ))}
      </div>

      {/* Equity curve + strategies */}
      <div className="grid lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 rounded-xl border border-border/60 bg-card p-5 space-y-3">
          <div className="h-5 w-32 rounded bg-muted/40" />
          <div className="h-[220px] rounded-lg bg-muted/20" />
        </div>
        <div className="rounded-xl border border-border/60 bg-card p-5 space-y-3">
          <div className="h-5 w-28 rounded bg-muted/40" />
          {[1, 2, 3].map(i => (
            <div key={i} className="flex items-center gap-3 py-2 border-b border-border/40 last:border-0">
              <div className="h-8 w-8 rounded-lg bg-muted/30" />
              <div className="flex-1 space-y-1.5">
                <div className="h-3.5 w-24 rounded bg-muted/40" />
                <div className="h-3 w-16 rounded bg-muted/30" />
              </div>
              <div className="h-5 w-12 rounded-full bg-muted/30" />
            </div>
          ))}
        </div>
      </div>

      {/* Positions + orders row */}
      <div className="grid lg:grid-cols-2 gap-6">
        {[1, 2].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-5 space-y-3">
            <div className="h-5 w-24 rounded bg-muted/40" />
            {[1, 2, 3].map(j => (
              <div key={j} className="flex items-center gap-3 py-2 border-b border-border/40 last:border-0">
                <div className="h-3.5 w-16 rounded bg-muted/30" />
                <div className="h-3.5 w-12 rounded bg-muted/20 ml-auto" />
                <div className="h-3.5 w-14 rounded bg-muted/20" />
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
