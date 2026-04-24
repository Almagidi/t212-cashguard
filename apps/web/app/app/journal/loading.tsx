export default function JournalLoading() {
  return (
    <div className="space-y-5 animate-pulse">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="space-y-2">
          <div className="h-7 w-28 rounded-lg bg-muted/40" />
          <div className="h-4 w-52 rounded bg-muted/30" />
        </div>
        <div className="h-8 w-28 rounded-md bg-muted/30" />
      </div>
      <div className="grid gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="rounded-xl border border-border/60 bg-card p-5 space-y-3">
            <div className="flex items-center justify-between">
              <div className="h-4 w-32 rounded bg-muted/40" />
              <div className="h-4 w-20 rounded bg-muted/30" />
            </div>
            <div className="h-3 w-full rounded bg-muted/20" />
            <div className="h-3 w-5/6 rounded bg-muted/20" />
            <div className="h-3 w-4/6 rounded bg-muted/20" />
          </div>
        ))}
      </div>
    </div>
  )
}
