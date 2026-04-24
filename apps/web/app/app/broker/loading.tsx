export default function BrokerLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      <div className="space-y-2">
        <div className="h-7 w-40 rounded-lg bg-muted/40" />
        <div className="h-4 w-64 rounded bg-muted/30" />
      </div>
      <div className="rounded-xl border border-border/60 bg-card p-6 space-y-5">
        <div className="flex items-center gap-4">
          <div className="h-12 w-12 rounded-xl bg-muted/30" />
          <div className="space-y-2">
            <div className="h-5 w-36 rounded bg-muted/40" />
            <div className="h-5 w-20 rounded-full bg-muted/30" />
          </div>
        </div>
        <div className="grid sm:grid-cols-2 gap-4">
          {[1, 2, 3, 4].map(i => (
            <div key={i} className="space-y-1.5">
              <div className="h-3.5 w-24 rounded bg-muted/40" />
              <div className="h-9 rounded-md bg-muted/30" />
            </div>
          ))}
        </div>
        <div className="flex gap-3 pt-2">
          <div className="h-9 w-32 rounded-md bg-primary/20" />
          <div className="h-9 w-28 rounded-md bg-muted/30" />
        </div>
      </div>
    </div>
  )
}
