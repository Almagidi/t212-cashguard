export default function ReportsLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="space-y-2">
          <div className="h-7 w-28 rounded-lg bg-muted/40" />
          <div className="h-4 w-72 rounded bg-muted/30" />
        </div>
        <div className="h-8 w-28 rounded-md bg-muted/30" />
      </div>

      {/* Stat cards row 1 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-4 space-y-2">
            <div className="h-3.5 w-20 rounded bg-muted/40" />
            <div className="h-7 w-24 rounded-md bg-muted/30" />
            <div className="h-3 w-16 rounded bg-muted/20" />
          </div>
        ))}
      </div>

      {/* Stat cards row 2 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-4 space-y-2">
            <div className="h-3.5 w-20 rounded bg-muted/40" />
            <div className="h-7 w-24 rounded-md bg-muted/30" />
          </div>
        ))}
      </div>

      {/* Charts row */}
      <div className="grid lg:grid-cols-2 gap-6">
        {/* Equity curve skeleton */}
        <div className="rounded-xl border border-border/60 bg-card p-5 space-y-3">
          <div className="h-5 w-32 rounded bg-muted/40" />
          <div className="h-[200px] rounded-lg bg-muted/20" />
        </div>
        {/* P&L bar chart skeleton */}
        <div className="rounded-xl border border-border/60 bg-card p-5 space-y-3">
          <div className="h-5 w-36 rounded bg-muted/40" />
          <div className="h-[200px] rounded-lg bg-muted/20" />
        </div>
      </div>

      {/* Trade history table skeleton */}
      <div className="rounded-xl border border-border/60 bg-card overflow-hidden">
        <div className="p-5 border-b border-border/60 flex items-center justify-between">
          <div className="h-5 w-28 rounded bg-muted/40" />
          <div className="h-4 w-20 rounded bg-muted/30" />
        </div>
        <div className="divide-y divide-border/40">
          {[1, 2, 3, 4, 5, 6].map(i => (
            <div key={i} className="flex items-center gap-4 px-5 py-3">
              <div className="h-4 w-14 rounded bg-muted/30" />
              <div className="h-4 w-8 rounded bg-muted/30" />
              <div className="h-4 w-12 rounded bg-muted/20 hidden sm:block" />
              <div className="h-4 w-10 rounded bg-muted/20 ml-auto" />
              <div className="h-4 w-16 rounded bg-muted/20 hidden md:block" />
              <div className="h-5 w-14 rounded-full bg-muted/30" />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
