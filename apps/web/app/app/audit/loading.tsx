export default function AuditLoading() {
  return (
    <div className="space-y-5 animate-pulse">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="space-y-2">
          <div className="h-7 w-28 rounded-lg bg-muted/40" />
          <div className="h-4 w-40 rounded bg-muted/30" />
        </div>
      </div>

      {/* Search */}
      <div className="h-9 max-w-sm rounded-md bg-muted/30" />

      {/* Table */}
      <div className="rounded-xl border border-border/60 bg-card overflow-hidden">
        <div className="divide-y divide-border/40">
          {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(i => (
            <div key={i} className="flex items-center gap-4 px-5 py-3">
              <div className="h-4 w-32 rounded-md bg-muted/40 font-mono" />
              <div className="h-4 w-28 rounded bg-muted/30 hidden sm:block" />
              <div className="h-4 w-20 rounded bg-muted/20" />
              <div className="h-4 w-24 rounded bg-muted/20 hidden md:block" />
              <div className="h-4 w-20 rounded bg-muted/20 hidden lg:block ml-auto" />
              <div className="h-4 w-28 rounded bg-muted/20" />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
