import type { DcaActivityResponse } from "@/types";

interface DcaActivityPanelProps {
  activity?: DcaActivityResponse;
  isLoading?: boolean;
  error?: unknown;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return value.toLocaleString();
  return String(value);
}

function formatMoney(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (Number.isNaN(numeric)) return String(value);
  return numeric.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function flagLabel(value: unknown): string {
  if (value === true) return "True";
  if (value === false) return "False";
  return formatValue(value);
}

export function DcaActivityPanel({
  activity,
  isLoading = false,
  error,
}: DcaActivityPanelProps) {
  if (isLoading) {
    return (
      <section className="rounded-2xl border border-slate-800 bg-slate-950/60 p-5 shadow-sm">
        <div className="h-5 w-56 animate-pulse rounded bg-slate-800" />
        <div className="mt-4 grid gap-3 md:grid-cols-4">
          <div className="h-20 animate-pulse rounded-xl bg-slate-900" />
          <div className="h-20 animate-pulse rounded-xl bg-slate-900" />
          <div className="h-20 animate-pulse rounded-xl bg-slate-900" />
          <div className="h-20 animate-pulse rounded-xl bg-slate-900" />
        </div>
      </section>
    );
  }

  if (error) {
    return (
      <section className="rounded-2xl border border-amber-700/50 bg-amber-950/20 p-5 shadow-sm">
        <h2 className="text-lg font-semibold text-amber-100">
          DCA paper activity
        </h2>
        <p className="mt-2 text-sm text-amber-200">
          DCA activity report is unavailable. Treat DCA paper activity as
          unknown.
        </p>
      </section>
    );
  }

  if (!activity) {
    return (
      <section className="rounded-2xl border border-slate-800 bg-slate-950/60 p-5 shadow-sm">
        <h2 className="text-lg font-semibold text-slate-100">
          DCA paper activity
        </h2>
        <p className="mt-2 text-sm text-slate-400">
          No DCA activity report is available.
        </p>
      </section>
    );
  }

  const configs = activity.configs ?? [];
  const perTicker = activity.per_ticker_activity ?? [];
  const recent = activity.recent_decisions ?? [];
  const safetyFlags = activity.safety_flags ?? {};

  return (
    <section className="space-y-5 rounded-2xl border border-slate-800 bg-slate-950/60 p-5 shadow-sm">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">
            DCA paper activity
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            Read-only report from persisted DCA config, state, and audit data.
            No orders are created from this panel.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <span className="rounded-full border border-sky-700/60 bg-sky-950/50 px-3 py-1 text-xs font-medium text-sky-200">
            Read-only
          </span>
          <span className="rounded-full border border-emerald-700/60 bg-emerald-950/50 px-3 py-1 text-xs font-medium text-emerald-200">
            Paper-only
          </span>
          <span className="rounded-full border border-rose-700/60 bg-rose-950/40 px-3 py-1 text-xs font-medium text-rose-200">
            Live disabled
          </span>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Total decisions
          </p>
          <p className="mt-2 text-2xl font-semibold text-slate-100">
            {formatValue(activity.decision_count_total)}
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            BUY_DUE
          </p>
          <p className="mt-2 text-2xl font-semibold text-emerald-200">
            {formatValue(activity.buy_due_count)}
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Blocked / skipped
          </p>
          <p className="mt-2 text-2xl font-semibold text-amber-200">
            {formatValue(activity.blocked_count)} /{" "}
            {formatValue(activity.skipped_count)}
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Paper allocated
          </p>
          <p className="mt-2 text-2xl font-semibold text-slate-100">
            {formatMoney(activity.total_paper_allocated_usd)}
          </p>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="text-sm font-semibold text-slate-100">Configs</h3>
          {configs.length === 0 ? (
            <p className="mt-3 text-sm text-slate-400">No DCA configs found.</p>
          ) : (
            <div className="mt-3 space-y-2">
              {configs.map((config) => (
                <div
                  key={config.id}
                  className="rounded-lg border border-slate-800 bg-slate-950/60 p-3"
                >
                  <div className="flex items-center justify-between gap-3">
                    <p className="font-medium text-slate-100">
                      {config.ticker}
                    </p>
                    <span
                      className={
                        config.enabled
                          ? "rounded-full bg-emerald-950 px-2 py-1 text-xs text-emerald-200"
                          : "rounded-full bg-slate-800 px-2 py-1 text-xs text-slate-300"
                      }
                    >
                      {config.enabled ? "Enabled" : "Disabled"}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    Venue: {config.venue} · Cadence:{" "}
                    {formatValue(config.cadence_days)} days · Paper only:{" "}
                    {flagLabel(config.paper_only)}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="text-sm font-semibold text-slate-100">
            Per-ticker state
          </h3>
          {perTicker.length === 0 ? (
            <p className="mt-3 text-sm text-slate-400">No DCA state found.</p>
          ) : (
            <div className="mt-3 space-y-2">
              {perTicker.map((row) => (
                <div
                  key={`${row.venue}-${row.ticker}`}
                  className="rounded-lg border border-slate-800 bg-slate-950/60 p-3"
                >
                  <div className="flex items-center justify-between gap-3">
                    <p className="font-medium text-slate-100">{row.ticker}</p>
                    <p className="text-xs text-slate-400">{row.venue}</p>
                  </div>
                  <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <dt className="text-slate-500">Latest decision</dt>
                      <dd className="text-slate-200">
                        {formatValue(row.latest_decision_code)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Allocated</dt>
                      <dd className="text-slate-200">
                        {formatMoney(row.total_allocated_usd)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Executions</dt>
                      <dd className="text-slate-200">
                        {formatValue(row.executions_count)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Last buy</dt>
                      <dd className="text-slate-200">
                        {formatValue(row.last_buy_at)}
                      </dd>
                    </div>
                  </dl>
                  {row.latest_reason ? (
                    <p className="mt-2 text-xs text-slate-400">
                      {row.latest_reason}
                    </p>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
        <h3 className="text-sm font-semibold text-slate-100">
          Recent DCA decisions
        </h3>
        {recent.length === 0 ? (
          <p className="mt-3 text-sm text-slate-400">
            No recent DCA paper decisions.
          </p>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[680px] text-left text-sm">
              <thead className="text-xs uppercase text-slate-500">
                <tr>
                  <th className="py-2 pr-4">Time</th>
                  <th className="py-2 pr-4">Ticker</th>
                  <th className="py-2 pr-4">Decision</th>
                  <th className="py-2 pr-4">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {recent.slice(0, 8).map((decision, index) => (
                  <tr key={decision.audit_id ?? `${decision.ticker}-${index}`}>
                    <td className="py-2 pr-4 text-slate-300">
                      {formatValue(decision.occurred_at ?? decision.created_at)}
                    </td>
                    <td className="py-2 pr-4 text-slate-100">
                      {formatValue(decision.ticker)}
                    </td>
                    <td className="py-2 pr-4 text-slate-100">
                      {formatValue(decision.decision_code)}
                    </td>
                    <td className="py-2 pr-4 text-slate-400">
                      {formatValue(decision.reason)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
        <h3 className="text-sm font-semibold text-slate-100">
          DCA safety flags
        </h3>
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {Object.entries(safetyFlags).map(([key, value]) => (
            <div
              key={key}
              className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-xs"
            >
              <span className="text-slate-400">{key}</span>
              <span className="font-medium text-slate-100">
                {flagLabel(value)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
