export default function SettingsLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      <div className="space-y-2">
        <div className="h-7 w-28 rounded-lg bg-muted/40" />
        <div className="h-4 w-56 rounded bg-muted/30" />
      </div>
      {[1, 2, 3].map(section => (
        <div key={section} className="rounded-xl border border-border/60 bg-card p-5 space-y-5">
          <div className="h-5 w-36 rounded bg-muted/40" />
          <div className="grid sm:grid-cols-2 gap-4">
            {[1, 2, 3, 4].map(i => (
              <div key={i} className="space-y-1.5">
                <div className="h-3.5 w-24 rounded bg-muted/40" />
                <div className="h-9 rounded-md bg-muted/30" />
              </div>
            ))}
          </div>
          <div className="h-9 w-28 rounded-md bg-primary/20" />
        </div>
      ))}
    </div>
  )
}
