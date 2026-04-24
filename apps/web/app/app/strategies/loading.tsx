export default function StrategiesLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="space-y-2">
          <div className="h-7 w-32 rounded-lg bg-muted/40" />
          <div className="h-4 w-64 rounded bg-muted/30" />
        </div>
        <div className="h-8 w-32 rounded-md bg-muted/30" />
      </div>

      {/* Strategy cards grid */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {[1, 2, 3, 4, 5, 6].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-5 space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3">
                <div className="h-10 w-10 rounded-lg bg-muted/30" />
                <div className="space-y-1.5">
                  <div className="h-4 w-28 rounded bg-muted/40" />
                  <div className="h-3 w-16 rounded bg-muted/30" />
                </div>
              </div>
              <div className="h-5 w-14 rounded-full bg-muted/30" />
            </div>
            <div className="h-3 w-full rounded bg-muted/20" />
            <div className="h-3 w-4/5 rounded bg-muted/20" />
            <div className="h-[56px] rounded-lg bg-muted/20" />
            <div className="flex items-center justify-between pt-1">
              <div className="h-8 w-20 rounded-md bg-muted/30" />
              <div className="h-8 w-20 rounded-md bg-muted/30" />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
