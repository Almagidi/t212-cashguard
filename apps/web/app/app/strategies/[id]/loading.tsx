export default function StrategyDetailLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Breadcrumb + header */}
      <div className="space-y-3">
        <div className="h-4 w-32 rounded bg-muted/30" />
        <div className="flex items-center gap-4">
          <div className="h-12 w-12 rounded-xl bg-muted/30" />
          <div className="space-y-2">
            <div className="h-7 w-48 rounded-lg bg-muted/40" />
            <div className="h-4 w-80 rounded bg-muted/30" />
          </div>
          <div className="ml-auto flex gap-2">
            <div className="h-8 w-20 rounded-md bg-muted/30" />
            <div className="h-8 w-24 rounded-md bg-primary/20" />
          </div>
        </div>
      </div>

      {/* Stat row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-4 space-y-2">
            <div className="h-3.5 w-20 rounded bg-muted/40" />
            <div className="h-7 w-24 rounded-md bg-muted/30" />
          </div>
        ))}
      </div>

      {/* Config + chart */}
      <div className="grid lg:grid-cols-5 gap-6">
        <div className="lg:col-span-2 rounded-xl border border-border/60 bg-card p-5 space-y-4">
          <div className="h-5 w-32 rounded bg-muted/40" />
          {[1, 2, 3, 4, 5].map(i => (
            <div key={i} className="space-y-1.5">
              <div className="h-3.5 w-24 rounded bg-muted/40" />
              <div className="h-9 rounded-md bg-muted/30" />
            </div>
          ))}
        </div>
        <div className="lg:col-span-3 rounded-xl border border-border/60 bg-card p-5 space-y-3">
          <div className="h-5 w-28 rounded bg-muted/40" />
          <div className="h-[220px] rounded-lg bg-muted/20" />
        </div>
      </div>
    </div>
  )
}
