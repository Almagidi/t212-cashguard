export default function InstrumentsLoading() {
  return (
    <div className="space-y-5 animate-pulse">
      {/* Header */}
      <div className="flex items-center gap-4">
        <div className="h-10 w-10 rounded-lg bg-muted/30" />
        <div className="space-y-2">
          <div className="h-7 w-32 rounded-lg bg-muted/40" />
          <div className="h-4 w-48 rounded bg-muted/30" />
        </div>
      </div>

      {/* Search bar */}
      <div className="h-9 max-w-md rounded-md bg-muted/30" />

      {/* Table */}
      <div className="rounded-xl border border-border/60 bg-card overflow-hidden">
        {/* thead skeleton */}
        <div className="flex gap-4 px-5 py-3 border-b border-border/60">
          {[48, 80, 64, 56, 72, 80].map((w, i) => (
            <div key={i} className="h-3.5 rounded bg-muted/40" style={{ width: w }} />
          ))}
        </div>
        <div className="divide-y divide-border/40">
          {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(i => (
            <div key={i} className="flex items-center gap-4 px-5 py-3">
              <div className="h-4 w-20 rounded-md bg-muted/40 font-mono" />
              <div className="h-4 w-36 rounded bg-muted/30" />
              <div className="h-5 w-20 rounded-md bg-muted/30" />
              <div className="h-5 w-14 rounded-full bg-muted/30 hidden sm:block" />
              <div className="h-4 w-10 rounded bg-muted/20 ml-auto hidden md:block" />
              <div className="h-4 w-16 rounded bg-muted/20 hidden lg:block" />
            </div>
          ))}
        </div>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between">
        <div className="h-4 w-40 rounded bg-muted/30" />
        <div className="flex gap-2">
          <div className="h-8 w-20 rounded-md bg-muted/30" />
          <div className="h-8 w-16 rounded-md bg-muted/30" />
        </div>
      </div>
    </div>
  )
}
