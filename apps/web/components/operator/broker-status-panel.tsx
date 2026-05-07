import type { BrokerStatus } from "@/types";

interface BrokerStatusPanelProps {
  status?: BrokerStatus | null;
  isLoading?: boolean;
  error?: unknown;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value.toLocaleString();
  return String(value);
}

function maskAccountId(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const raw = String(value);
  if (raw.length <= 4) return "••••";
  return `••••${raw.slice(-4)}`;
}

function statusBadgeClass(status: string): string {
  const normalized = status.toLowerCase();

  if (
    normalized.includes("configured") ||
    normalized.includes("connected") ||
    normalized.includes("ok") ||
    normalized.includes("healthy")
  ) {
    return "border-emerald-700/60 bg-emerald-950/50 text-emerald-200";
  }

  if (
    normalized.includes("reconnect") ||
    normalized.includes("missing") ||
    normalized.includes("error") ||
    normalized.includes("failed") ||
    normalized.includes("not_connected")
  ) {
    return "border-amber-700/60 bg-amber-950/40 text-amber-200";
  }

  return "border-slate-700 bg-slate-900 text-slate-200";
}

export function BrokerStatusPanel({
  status,
  isLoading = false,
  error,
}: BrokerStatusPanelProps) {
  if (isLoading) {
    return (
      <section className="rounded-2xl border border-slate-800 bg-slate-950/60 p-5 shadow-sm">
        <div className="h-5 w-64 animate-pulse rounded bg-slate-800" />
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
          Broker connection status
        </h2>
        <p className="mt-2 text-sm text-amber-200">
          Broker status is unavailable. Treat broker connectivity as unknown.
        </p>
      </section>
    );
  }

  const connectionStatus =
    status?.credential_state ?? (status ? "configured" : "unknown");

  return (
    <section className="space-y-5 rounded-2xl border border-slate-800 bg-slate-950/60 p-5 shadow-sm">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">
            Broker connection status
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            Read-only broker visibility. This panel does not connect,
            disconnect, trade, or place orders.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <span className="rounded-full border border-sky-700/60 bg-sky-950/50 px-3 py-1 text-xs font-medium text-sky-200">
            Read-only
          </span>
          <span className="rounded-full border border-rose-700/60 bg-rose-950/40 px-3 py-1 text-xs font-medium text-rose-200">
            No broker actions
          </span>
          <span
            className={`rounded-full border px-3 py-1 text-xs font-medium ${statusBadgeClass(
              connectionStatus,
            )}`}
          >
            {formatValue(connectionStatus)}
          </span>
        </div>
      </div>

      {!status ? (
        <div className="rounded-xl border border-amber-800/60 bg-amber-950/20 p-4">
          <p className="text-sm text-amber-200">
            No broker connection status was returned. Treat broker state as
            unknown.
          </p>
        </div>
      ) : null}

      <div className="grid gap-3 md:grid-cols-4">
        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Broker
          </p>
          <p className="mt-2 text-xl font-semibold text-slate-100">
            {formatValue(status?.broker ?? "trading212")}
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Environment
          </p>
          <p className="mt-2 text-xl font-semibold text-slate-100">
            {formatValue(status?.environment)}
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Real broker configured
          </p>
          <p className="mt-2 text-xl font-semibold text-slate-100">
            {formatValue(status?.credential_state === "configured")}
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Mock broker active
          </p>
          <p className="mt-2 text-xl font-semibold text-slate-100">
            {formatValue(status?.is_active)}
          </p>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="text-sm font-semibold text-slate-100">
            Account metadata
          </h3>
          <dl className="mt-3 grid gap-3 text-sm">
            <div className="flex items-center justify-between gap-4 border-b border-slate-800 pb-2">
              <dt className="text-slate-400">Account ID</dt>
              <dd className="font-medium text-slate-100">
                {maskAccountId(status?.account_id)}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-4 border-b border-slate-800 pb-2">
              <dt className="text-slate-400">Account currency</dt>
              <dd className="font-medium text-slate-100">
                {formatValue(status?.account_currency)}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-4 border-b border-slate-800 pb-2">
              <dt className="text-slate-400">Last connection test</dt>
              <dd className="font-medium text-slate-100">
                {formatValue(status?.last_test_at)}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-slate-400">Last test OK</dt>
              <dd className="font-medium text-slate-100">
                {formatValue(status?.last_test_ok)}
              </dd>
            </div>
          </dl>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="text-sm font-semibold text-slate-100">
            Safety boundary
          </h3>
          <div className="mt-3 space-y-2 text-sm text-slate-300">
            <p>Broker credentials are not displayed.</p>
            <p>
              No connect, reconnect, disconnect, buy, sell, execute, or live
              controls are available here.
            </p>
            <p>
              This panel only reads persisted broker status from the backend.
            </p>
          </div>

          {status?.recovery_hint ? (
            <div className="mt-4 rounded-lg border border-amber-800/60 bg-amber-950/20 p-3 text-sm text-amber-200">
              {status.recovery_hint}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
