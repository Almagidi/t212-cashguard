export default function EmergencyLoading() {
  return (
    <div className="space-y-6 animate-pulse">
      <div className="space-y-2">
        <div className="h-7 w-44 rounded-lg bg-muted/40" />
        <div className="h-4 w-72 rounded bg-muted/30" />
      </div>
      {/* Emergency action cards */}
      <div className="grid gap-4 sm:grid-cols-2">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="rounded-xl border border-red-500/20 bg-red-500/5 p-5 space-y-3">
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-full bg-red-500/20" />
              <div className="space-y-1.5">
                <div className="h-4 w-32 rounded bg-muted/40" />
                <div className="h-3 w-48 rounded bg-muted/30" />
              </div>
            </div>
            <div className="h-10 rounded-lg bg-red-500/20" />
          </div>
        ))}
      </div>
    </div>
  )
}
