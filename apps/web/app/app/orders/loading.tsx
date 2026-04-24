export default function OrdersLoading() {
  return (
    <div className="space-y-5 animate-pulse">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="space-y-2">
          <div className="h-7 w-24 rounded-lg bg-muted/40" />
          <div className="h-4 w-40 rounded bg-muted/30" />
        </div>
        <div className="flex gap-2">
          <div className="h-8 w-24 rounded-md bg-muted/30" />
        </div>
      </div>

      {/* Tab strip */}
      <div className="inline-flex gap-0.5 p-1 bg-muted/40 border border-border rounded-lg">
        {[80, 64, 56, 72].map((w, i) => (
          <div key={i} className="h-7 rounded-md bg-muted/30" style={{ width: w }} />
        ))}
      </div>

      {/* Orders table */}
      <div className="rounded-xl border border-border/60 bg-card overflow-hidden">
        <div className="divide-y divide-border/40">
          {[1, 2, 3, 4, 5, 6, 7].map(i => (
            <div key={i} className="flex items-center gap-4 px-5 py-3">
              <div className="space-y-1.5 min-w-[80px]">
                <div className="h-3.5 w-16 rounded bg-muted/40" />
                <div className="h-3 w-24 rounded bg-muted/20" />
              </div>
              <div className="h-5 w-10 rounded-md bg-muted/30" />
              <div className="h-3.5 w-14 rounded bg-muted/20 hidden sm:block" />
              <div className="h-4 w-10 rounded bg-muted/20 ml-auto" />
              <div className="h-4 w-16 rounded bg-muted/20 hidden md:block" />
              <div className="h-5 w-16 rounded-full bg-muted/30" />
              <div className="h-4 w-14 rounded bg-muted/20 hidden md:block" />
              <div className="h-4 w-12 rounded bg-muted/20 hidden sm:block" />
              <div className="h-7 w-7 rounded-md bg-muted/20" />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
