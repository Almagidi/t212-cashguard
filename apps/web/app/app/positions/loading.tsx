export default function PositionsLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-2">
          <div className="h-7 w-28 rounded-lg bg-muted/40" />
          <div className="h-4 w-48 rounded bg-muted/30" />
        </div>
        <div className="h-8 w-24 rounded-md bg-muted/30" />
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {[1, 2, 3].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-4 space-y-2">
            <div className="h-3.5 w-24 rounded bg-muted/40" />
            <div className="h-7 w-28 rounded-md bg-muted/30" />
            <div className="h-3 w-16 rounded bg-muted/20" />
          </div>
        ))}
      </div>

      {/* Positions table */}
      <div className="rounded-xl border border-border/60 bg-card overflow-hidden">
        <div className="divide-y divide-border/40">
          {[1, 2, 3, 4, 5].map(i => (
            <div key={i} className="flex items-center gap-4 px-5 py-3.5">
              <div className="flex items-center gap-2.5">
                <div className="h-8 w-8 rounded-lg bg-muted/30" />
                <div className="space-y-1.5">
                  <div className="h-3.5 w-12 rounded bg-muted/40" />
                  <div className="h-3 w-8 rounded bg-muted/20" />
                </div>
              </div>
              <div className="h-4 w-14 rounded bg-muted/20 ml-auto" />
              <div className="h-4 w-16 rounded bg-muted/20 hidden sm:block" />
              <div className="h-4 w-16 rounded bg-muted/20" />
              <div className="h-4 w-20 rounded bg-muted/20 hidden md:block" />
              <div className="h-4 w-16 rounded bg-muted/30" />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
