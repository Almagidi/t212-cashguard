export default function AlertsLoading() {
  return (
    <div className="space-y-5 animate-pulse">
      <div className="space-y-2">
        <div className="h-7 w-24 rounded-lg bg-muted/40" />
        <div className="h-4 w-56 rounded bg-muted/30" />
      </div>
      <div className="grid gap-3">
        {[1, 2, 3, 4, 5].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-4 flex items-start gap-4">
            <div className="h-8 w-8 rounded-full bg-muted/30 mt-0.5" />
            <div className="flex-1 space-y-2">
              <div className="h-4 w-48 rounded bg-muted/40" />
              <div className="h-3 w-full rounded bg-muted/20" />
              <div className="h-3 w-3/4 rounded bg-muted/20" />
            </div>
            <div className="h-4 w-24 rounded bg-muted/20" />
          </div>
        ))}
      </div>
    </div>
  )
}
