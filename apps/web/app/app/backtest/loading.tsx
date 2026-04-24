export default function BacktestLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Header */}
      <div className="space-y-2">
        <div className="h-7 w-44 rounded-lg bg-muted/40" />
        <div className="h-4 w-80 rounded bg-muted/30" />
      </div>
      {/* Risk banner */}
      <div className="h-16 rounded-lg bg-amber-500/5 border border-amber-500/10" />
      {/* Main grid */}
      <div className="grid gap-6 lg:grid-cols-3">
        {/* Form card */}
        <div className="rounded-xl border border-border/60 bg-card p-5 space-y-4">
          {[1,2,3,4,5,6].map(i => (
            <div key={i} className="space-y-1.5">
              <div className="h-3.5 w-20 rounded bg-muted/40" />
              <div className="h-9 rounded-md bg-muted/30" />
            </div>
          ))}
          <div className="h-9 rounded-md bg-primary/20" />
        </div>
        {/* Context card */}
        <div className="lg:col-span-2 rounded-xl border border-border/60 bg-card p-5 space-y-4">
          <div className="h-5 w-36 rounded bg-muted/40" />
          <div className="h-4 w-full rounded bg-muted/30" />
          <div className="h-4 w-3/4 rounded bg-muted/30" />
          <div className="grid grid-cols-2 gap-3 mt-4">
            <div className="h-24 rounded-lg bg-muted/20 border border-border/40" />
            <div className="h-24 rounded-lg bg-muted/20 border border-border/40" />
          </div>
        </div>
      </div>
    </div>
  )
}
