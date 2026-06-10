"use client";

import {
  AlertTriangle,
  Clock3,
  Eye,
  FileText,
  ListChecks,
  Lock,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  PageHeader,
  Skeleton,
  StatCard,
} from "@/components/ui";
import { cn, formatCurrency, formatDate } from "@/lib/utils";
import { BrokerStatusPanel } from "./broker-status-panel";
import {
  useBrokerStatus,
  useDemoReconciliationSchedulerStatus,
  useDemoReconciliationStatus,
  usePaperExecutionHistory,
} from "@/hooks/use-api";
import type {
  DemoReconciliationSchedulerStatus,
  DemoReconciliationWorkerStatus,
  OperatorOverallStatus,
  PaperExecutionHistoryItem,
  OperatorRecentActivity,
  OperatorStatus,
  OperatorVenueStatus,
  OperatorWorkerHealth,
} from "@/types";

type OperatorDashboardProps = {
  status?: OperatorStatus;
  isLoading?: boolean;
  isError?: boolean;
};

const BOOL_LABEL: Record<"true" | "false", string> = {
  true: "Yes",
  false: "No",
};

function statusBadgeVariant(status: OperatorOverallStatus) {
  if (status === "ok") return "success";
  if (status === "blocked") return "destructive";
  return "warning";
}

function healthBadgeVariant(health: OperatorWorkerHealth) {
  if (health === "healthy") return "success";
  if (health === "stale" || health === "missing") return "destructive";
  return "warning";
}

function boolTone(value: boolean | null | undefined, riskyWhenTrue = false) {
  if (value === null || value === undefined) return "text-muted-foreground";
  if (riskyWhenTrue) return value ? "text-red-400" : "text-emerald-400";
  return value ? "text-emerald-400" : "text-muted-foreground";
}

function boolLabel(value: boolean | null | undefined) {
  if (value === null || value === undefined) return "Unknown";
  return BOOL_LABEL[String(value) as "true" | "false"];
}

function TextBadge({
  children,
  tone = "outline",
  testId,
}: {
  children: React.ReactNode;
  tone?: "outline" | "success" | "warning" | "destructive" | "info";
  testId?: string;
}) {
  return <Badge variant={tone} data-testid={testId}>{children}</Badge>;
}

function InfoRow({
  label,
  value,
  valueClassName,
}: {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;
}) {
  return (
    <div className="kv-row">
      <dt className="text-muted-foreground">{label}</dt>
      <dd
        className={cn("text-right font-medium text-foreground", valueClassName)}
      >
        {value}
      </dd>
    </div>
  );
}

function FlagRow({
  label,
  value,
  riskyWhenTrue = false,
}: {
  label: string;
  value: boolean;
  riskyWhenTrue?: boolean;
}) {
  return (
    <InfoRow
      label={label}
      value={value ? "True" : "False"}
      valueClassName={boolTone(value, riskyWhenTrue)}
    />
  );
}

function LoadingState() {
  return (
    <div className="space-y-5" aria-label="Loading operator status">
      <PageHeader
        icon={<Eye className="h-5 w-5" />}
        label="Read-only Operator Dashboard"
        sub="Loading operator status..."
      />
      <div className="grid gap-4 md:grid-cols-3">
        {[0, 1, 2].map((item) => (
          <Card key={item}>
            <CardContent className="space-y-3 pt-5">
              <Skeleton className="h-3 w-24" />
              <Skeleton className="h-8 w-32" />
              <Skeleton className="h-3 w-full" />
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardContent className="space-y-3 pt-5">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-28 w-full" />
        </CardContent>
      </Card>
    </div>
  );
}

function ErrorState() {
  return (
    <div className="space-y-5">
      <PageHeader
        icon={<ShieldAlert className="h-5 w-5" />}
        label="Read-only Operator Dashboard"
        sub="Operator status unavailable. Treat automation state as unknown."
      />
      <Card className="border-amber-500/30 bg-amber-500/5">
        <CardContent className="pt-5">
          <div className="flex items-start gap-3 text-amber-100">
            <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-300" />
            <div>
              <p className="text-sm font-semibold">
                Operator status unavailable
              </p>
              <p className="mt-1 text-xs leading-relaxed text-amber-100/80">
                Treat automation state as unknown. This page is read-only and no
                trading action is available from this page.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function TopSafetySummary({ status }: { status: OperatorStatus }) {
  const generatedMissing = !status.generated_at;

  return (
    <div className="space-y-4">
      <PageHeader
        icon={<Eye className="h-5 w-5" />}
        label="Read-only Operator Dashboard"
        sub="Trading system status from persisted backend state only"
      />
      <Card
        className={cn(
          status.overall_status === "blocked" && "border-red-500/40",
          status.overall_status === "degraded" && "border-amber-500/40",
        )}
        data-testid="live-readiness-status"
      >
        <CardContent className="space-y-4 pt-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={statusBadgeVariant(status.overall_status)}>
              Overall {status.overall_status}
            </Badge>
            <TextBadge tone={status.dca.paper_only ? "success" : "warning"}>
              Paper-only
            </TextBadge>
            <TextBadge
              tone={
                !status.live_trading_enabled_anywhere ? "success" : "warning"
              }
            >
              Live disabled
            </TextBadge>
            <TextBadge tone="info">Read-only</TextBadge>
            {generatedMissing && (
              <TextBadge tone="warning">Timestamp unknown</TextBadge>
            )}
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <StatCard
              label="Live Trading Possible"
              value={status.live_trading_possible ? "Yes" : "No"}
              trend={status.live_trading_possible ? "down" : "neutral"}
              sub="Backend computed gate state"
              icon={<Lock className="h-4 w-4" />}
            />
            <StatCard
              label="Live Enabled Anywhere"
              value={status.live_trading_enabled_anywhere ? "Yes" : "No"}
              trend={status.live_trading_enabled_anywhere ? "down" : "neutral"}
              sub="Includes app, settings, and venue flags"
              icon={<ShieldAlert className="h-4 w-4" />}
            />
            <StatCard
              label="Generated At"
              value={
                status.generated_at
                  ? formatDate(status.generated_at)
                  : "Unknown"
              }
              sub={
                generatedMissing
                  ? "Treat data freshness as unknown"
                  : "Backend status timestamp"
              }
              icon={<Clock3 className="h-4 w-4" />}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            No trading action available from this page.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}


function ExecutionBoundary({ status }: { status: OperatorStatus }) {
  const flags = status.safety_flags;
  const liveLocked =
    !status.live_trading_possible &&
    !status.live_trading_enabled_anywhere &&
    !flags.live_trading_enabled_setting &&
    !flags.app_live_trading_unlocked;

  return (
    <Card
      className="border-emerald-500/25 bg-emerald-500/[0.04]"
      data-testid="operator-execution-boundary"
    >
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle>Execution Boundary</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              This page is visibility-only. It must not create orders, call brokers, trigger schedulers, or run strategies.
            </p>
          </div>
          <div className="flex flex-wrap justify-end gap-1.5">
            <TextBadge tone={flags.endpoint_read_only ? "success" : "destructive"} testId="operator-read-only-badge">
              Read-only endpoint
            </TextBadge>
            <TextBadge tone="success" testId="operator-no-broker-order-badge">
              No broker order sent
            </TextBadge>
            <TextBadge tone={liveLocked ? "success" : "warning"} testId="operator-live-disabled-badge">
              {liveLocked ? "Live locked" : "Live state needs review"}
            </TextBadge>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-6 md:grid-cols-2 xl:grid-cols-4">
          <InfoRow
            label="Creates orders"
            value={String(flags.creates_orders)}
            valueClassName={boolTone(flags.creates_orders, true)}
          />
          <InfoRow
            label="Calls brokers"
            value={String(flags.calls_brokers)}
            valueClassName={boolTone(flags.calls_brokers, true)}
          />
          <InfoRow
            label="Triggers schedulers"
            value={String(flags.triggers_schedulers)}
            valueClassName={boolTone(flags.triggers_schedulers, true)}
          />
          <InfoRow
            label="Runs strategies"
            value={String(flags.runs_strategies)}
            valueClassName={boolTone(flags.runs_strategies, true)}
          />
          <InfoRow
            label="Paper execution"
            value={status.paper_execution.paper_only ? "Local/mock only" : "Review required"}
            valueClassName={status.paper_execution.paper_only ? "text-emerald-400" : "text-red-400"}
          />
          <InfoRow
            label="Cash-only mode"
            value={String(flags.cash_only_mode)}
            valueClassName={boolTone(flags.cash_only_mode)}
          />
          <InfoRow
            label="Live trading possible"
            value={String(status.live_trading_possible)}
            valueClassName={boolTone(status.live_trading_possible, true)}
          />
          <InfoRow
            label="Live enabled anywhere"
            value={String(status.live_trading_enabled_anywhere)}
            valueClassName={boolTone(status.live_trading_enabled_anywhere, true)}
          />
        </dl>
      </CardContent>
    </Card>
  );
}

function VenueCard({ venue }: { venue: OperatorVenueStatus }) {
  const blocked = venue.kill_switch_active === true;
  const degraded = venue.degraded_mode_active === true;

  return (
    <Card
      className={cn(
        blocked && "border-red-500/40 bg-red-500/5",
        degraded && !blocked && "border-amber-500/40 bg-amber-500/5",
      )}
    >
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="uppercase">{venue.venue}</CardTitle>
          <div className="flex flex-wrap justify-end gap-1.5">
            {blocked && (
              <TextBadge tone="destructive">Kill switch active</TextBadge>
            )}
            {degraded && <TextBadge tone="warning">Degraded</TextBadge>}
            {!venue.present && (
              <TextBadge tone="warning">Config missing</TextBadge>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <dl>
          <InfoRow
            label="Kill switch active"
            value={boolLabel(venue.kill_switch_active)}
            valueClassName={boolTone(venue.kill_switch_active, true)}
          />
          <InfoRow
            label="Automation enabled"
            value={boolLabel(venue.auto_trading_enabled)}
            valueClassName={boolTone(venue.auto_trading_enabled, true)}
          />
          <InfoRow
            label="Degraded mode active"
            value={boolLabel(venue.degraded_mode_active)}
            valueClassName={boolTone(venue.degraded_mode_active, true)}
          />
          <InfoRow label="Updated" value={formatDate(venue.updated_at)} />
        </dl>
        {venue.note && (
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            {venue.note}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function Trading212Summary({ status }: { status: OperatorStatus }) {
  const readiness = status.trading212.live_readiness_status;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Trading212 Summary</CardTitle>
      </CardHeader>
      <CardContent>
        <dl>
          <InfoRow
            label="Strategies"
            value={status.trading212.strategies_count}
          />
          <InfoRow
            label="Live-approved strategies"
            value={status.trading212.live_approved_strategies_count}
          />
          <InfoRow
            label="Active orders"
            value={status.trading212.active_orders_count}
          />
          <InfoRow
            label="Recent orders"
            value={status.trading212.recent_orders_count}
          />
          <InfoRow
            label="Latest order status"
            value={status.trading212.latest_order_status ?? "None"}
          />
          <InfoRow
            label="Readiness status"
            value={
              readiness
                ? readiness.ready_for_live
                  ? "Backend reports ready_for_live"
                  : "Blocked or incomplete"
                : "Not available"
            }
            valueClassName={
              readiness?.ready_for_live
                ? "text-amber-300"
                : "text-muted-foreground"
            }
          />
        </dl>
        <ul className="mt-3 space-y-1 text-xs text-muted-foreground">
          {status.trading212.safety_notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function KrakenSummary({ status }: { status: OperatorStatus }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>Kraken Summary</CardTitle>
          <TextBadge tone={status.kraken.live_enabled ? "warning" : "success"}>
            {status.kraken.live_enabled ? "Live enabled" : "Live disabled"}
          </TextBadge>
        </div>
      </CardHeader>
      <CardContent>
        <dl>
          <InfoRow label="Strategies" value={status.kraken.strategies_count} />
          <InfoRow
            label="Paper-only strategies"
            value={status.kraken.paper_only_strategies_count}
          />
          <InfoRow
            label="Recent orders"
            value={status.kraken.recent_orders_count}
          />
          <InfoRow
            label="Active orders"
            value={status.kraken.active_orders_count}
          />
          <InfoRow
            label="Venue status"
            value={
              status.kraken.venue_config?.degraded_mode_active
                ? "Degraded"
                : status.kraken.venue_config
                  ? "Configured"
                  : "Unknown"
            }
          />
        </dl>
        <ul className="mt-3 space-y-1 text-xs text-muted-foreground">
          {status.kraken.safety_notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function DcaSummary({ status }: { status: OperatorStatus }) {
  const dcaActivity = status.recent_activity
    .filter((activity) => activity.action === "dca_paper_decision")
    .slice(0, 4);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>DCA Summary</CardTitle>
          <div className="flex flex-wrap justify-end gap-1.5">
            <TextBadge tone={status.dca.paper_only ? "success" : "warning"}>
              Paper-only
            </TextBadge>
            <TextBadge tone={!status.dca.runnable ? "success" : "destructive"}>
              Runnable {String(status.dca.runnable)}
            </TextBadge>
            <TextBadge
              tone={!status.dca.live_enabled ? "success" : "destructive"}
            >
              Live {status.dca.live_enabled ? "enabled" : "disabled"}
            </TextBadge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl>
          <InfoRow label="Configs" value={status.dca.config_count} />
          <InfoRow
            label="Enabled configs"
            value={status.dca.enabled_config_count}
          />
          <InfoRow
            label="Decision count total"
            value={status.dca.decision_count_total}
          />
          <InfoRow label="BUY_DUE decisions" value={status.dca.buy_due_count} />
          <InfoRow label="Blocked decisions" value={status.dca.blocked_count} />
          <InfoRow label="Skipped decisions" value={status.dca.skipped_count} />
          <InfoRow
            label="Total paper allocated"
            value={formatCurrency(status.dca.total_paper_allocated_usd)}
          />
        </dl>
        {status.dca.config_count === 0 ? (
          <EmptyState
            title="No DCA configs"
            description="No paper-only Kraken DCA configs were reported by operator status."
            className="rounded-lg border border-border/60 py-8"
          />
        ) : (
          <div>
            <p className="text-xs font-medium text-muted-foreground">Tickers</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {status.dca.tickers.map((ticker) => (
                <Badge key={ticker} variant="secondary">
                  {ticker}
                </Badge>
              ))}
            </div>
          </div>
        )}
        <div>
          <p className="text-xs font-medium text-muted-foreground">
            Recent DCA decisions
          </p>
          {dcaActivity.length === 0 ? (
            <p className="mt-2 text-xs text-muted-foreground">
              No recent DCA decision activity in this operator status response.
            </p>
          ) : (
            <div className="mt-2 space-y-2">
              {dcaActivity.map((activity) => (
                <ActivitySummary
                  key={activity.id}
                  activity={activity}
                  compact
                />
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function PaperExecutionSummary({ status }: { status: OperatorStatus }) {
  const paper = status.paper_execution;

  return (
    <Card className="border-emerald-500/25 bg-emerald-500/5">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>Paper Execution</CardTitle>
          <div className="flex flex-wrap justify-end gap-1.5">
            <TextBadge tone={paper.paper_only ? "success" : "destructive"}>
              Paper only
            </TextBadge>
            <TextBadge tone="info">Mock execution</TextBadge>
            <TextBadge tone="success">No broker order sent</TextBadge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl>
          <InfoRow label="Filled paper orders" value={paper.total_paper_orders} />
          <InfoRow
            label="Open paper positions"
            value={paper.open_paper_positions_count}
          />
          <InfoRow
            label="Last paper status"
            value={paper.last_paper_execution_status ?? "None"}
          />
          <InfoRow
            label="Latest paper order"
            value={formatDate(paper.latest_paper_order_timestamp)}
          />
          <InfoRow label="Enabled mode" value={paper.enabled_in_mode} />
        </dl>
        <ul className="space-y-1 text-xs text-muted-foreground">
          {paper.safety_notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function SchedulerWorkerHealth({ status }: { status: OperatorStatus }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>Scheduler / Worker Health</CardTitle>
          <Badge variant={healthBadgeVariant(status.schedulers.worker_health)}>
            Worker heartbeat {status.schedulers.worker_health}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <dl>
          <InfoRow
            label="DCA paper evaluate"
            value={
              status.schedulers.dca_paper_evaluate_registered
                ? "Registered"
                : "Not registered"
            }
            valueClassName={
              status.schedulers.dca_paper_evaluate_registered
                ? "text-emerald-400"
                : "text-amber-400"
            }
          />
          <InfoRow
            label="DCA cadence"
            value={status.schedulers.dca_paper_evaluate_cadence ?? "Unknown"}
          />
          <InfoRow
            label="Heartbeat"
            value={
              status.schedulers.heartbeat_registered
                ? "Registered"
                : "Not registered"
            }
            valueClassName={
              status.schedulers.heartbeat_registered
                ? "text-emerald-400"
                : "text-amber-400"
            }
          />
          <InfoRow
            label="Heartbeat cadence"
            value={status.schedulers.heartbeat_cadence ?? "Unknown"}
          />
          <InfoRow
            label="Heartbeat component"
            value={status.schedulers.heartbeat_component}
          />
          <InfoRow
            label="Last seen"
            value={formatDate(status.schedulers.heartbeat_last_seen_at)}
          />
          <InfoRow
            label="Stale threshold"
            value={`${status.schedulers.heartbeat_stale_after_seconds}s`}
          />
        </dl>
        <p className="mt-3 text-xs text-muted-foreground">
          Scheduler status means registered only. Worker health is based on
          persisted heartbeat rows.
        </p>
      </CardContent>
    </Card>
  );
}

function safeSummaryEntries(summary: Record<string, unknown>) {
  const sensitive =
    /secret|token|password|credential|api[_-]?key|api[_-]?secret/i;
  return Object.entries(summary)
    .filter(([key]) => !sensitive.test(key))
    .slice(0, 5);
}

function summaryValue(value: unknown) {
  if (value === null || value === undefined) return "—";
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  )
    return String(value);
  return JSON.stringify(value).slice(0, 80);
}

function compactDecimal(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return numeric.toLocaleString(undefined, {
    maximumFractionDigits: 8,
  });
}

function paperStatusTone(
  item: PaperExecutionHistoryItem,
): "success" | "warning" | "destructive" | "info" {
  if (item.risk_result === "blocked" || item.status === "rejected") {
    return "destructive";
  }
  if (item.status === "filled") return "success";
  if (item.status === "submitted" || item.status === "accepted") return "info";
  return "warning";
}

function PaperExecutionHistoryPanel() {
  const history = usePaperExecutionHistory({ limit: 25 });
  const items = history.data?.items ?? [];

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle>Paper Execution History</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              Local mock paper orders, risk decisions, fills, positions, and audit review.
            </p>
          </div>
          <div className="flex flex-wrap justify-end gap-1.5">
            <TextBadge tone="success">Paper only</TextBadge>
            <TextBadge tone="success">No broker order sent</TextBadge>
            <TextBadge tone="info">Mock/local execution</TextBadge>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {history.isLoading ? (
          <div className="space-y-3" aria-label="Loading paper execution history">
            <Skeleton className="h-4 w-48" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : history.isError ? (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
            <p className="text-sm font-semibold text-amber-100">
              Paper execution history unavailable
            </p>
            <p className="mt-1 text-xs text-amber-100/80">
              Operator status remains visible. This read-only panel does not create,
              execute, or send broker orders.
            </p>
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            title="No paper execution history"
            description="Paper execution history will appear here after mock paper orders are created. No broker order is sent."
            className="py-8"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="border-b border-border/70 text-muted-foreground">
                <tr>
                  <th className="py-2 pr-3 font-medium">Created</th>
                  <th className="py-2 pr-3 font-medium">Ticker</th>
                  <th className="py-2 pr-3 font-medium">Side</th>
                  <th className="py-2 pr-3 font-medium">Source</th>
                  <th className="py-2 pr-3 font-medium">Venue</th>
                  <th className="py-2 pr-3 font-medium">Qty / notional</th>
                  <th className="py-2 pr-3 font-medium">Fill</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {items.map((item) => (
                  <tr key={item.id} className="align-top">
                    <td className="py-3 pr-3 text-muted-foreground">
                      {formatDate(item.created_at)}
                    </td>
                    <td className="py-3 pr-3 font-mono font-semibold text-foreground">
                      {item.ticker}
                    </td>
                    <td className="py-3 pr-3 uppercase">{item.side ?? "—"}</td>
                    <td className="py-3 pr-3">
                      <div className="max-w-[160px] space-y-1">
                        <p className="truncate text-foreground">
                          {item.source ?? "unknown"}
                        </p>
                        <p className="truncate text-muted-foreground">
                          {item.strategy ?? "no strategy"}
                        </p>
                      </div>
                    </td>
                    <td className="py-3 pr-3">{item.venue ?? "paper"}</td>
                    <td className="py-3 pr-3">
                      <div className="space-y-1">
                        <p>{compactDecimal(item.quantity)}</p>
                        <p className="text-muted-foreground">
                          {item.notional ? formatCurrency(item.notional) : "—"}
                        </p>
                      </div>
                    </td>
                    <td className="py-3 pr-3">
                      <div className="space-y-1">
                        <p>{item.fill_price ? formatCurrency(item.fill_price) : "—"}</p>
                        <p className="text-muted-foreground">
                          filled {compactDecimal(item.filled_quantity)}
                        </p>
                      </div>
                    </td>
                    <td className="py-3 pr-3">
                      <div className="space-y-2">
                        <Badge variant={paperStatusTone(item)}>
                          {item.status}
                        </Badge>
                        <div className="flex flex-wrap gap-1">
                          <TextBadge tone="success">Paper only</TextBadge>
                          <TextBadge tone="success">No broker order sent</TextBadge>
                        </div>
                        <details className="group rounded-md border border-border/60 bg-secondary/20 p-2">
                          <summary className="flex cursor-pointer list-none items-center gap-1.5 text-muted-foreground">
                            <FileText className="h-3.5 w-3.5" />
                            Audit review
                          </summary>
                          <div className="mt-2 space-y-1 text-muted-foreground">
                            <p>Risk: {item.risk_result}</p>
                            <p>Audit events: {item.audit_count}</p>
                            <p>Latest audit: {formatDate(item.latest_audit_at)}</p>
                            <p>Live order sent: {String(item.live_order_sent)}</p>
                            {item.rejection_reason && (
                              <p className="text-amber-200">
                                Rejection: {item.rejection_reason}
                              </p>
                            )}
                          </div>
                        </details>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ActivitySummary({
  activity,
  compact = false,
}: {
  activity: OperatorRecentActivity;
  compact?: boolean;
}) {
  const entries = safeSummaryEntries(activity.payload_summary);

  return (
    <div
      className={cn(
        "rounded-lg border border-border/60 bg-secondary/20 p-3",
        compact && "p-2",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-mono text-xs font-medium text-foreground">
          {activity.action}
        </p>
        <p className="text-xs text-muted-foreground">
          {formatDate(activity.occurred_at)}
        </p>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {activity.entity_type ?? "system"}
        {activity.entity_id
          ? ` · ${activity.entity_id.slice(0, 12)}...`
          : ""} · {activity.actor}
      </p>
      {entries.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {entries.map(([key, value]) => (
            <span
              key={key}
              className="rounded-md border border-border/60 bg-background/50 px-2 py-1 text-[11px] text-muted-foreground"
            >
              {key}:{" "}
              <span className="text-foreground">{summaryValue(value)}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function RecentActivity({ status }: { status: OperatorStatus }) {
  const activity = status.recent_activity.slice(0, 8);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent Activity</CardTitle>
      </CardHeader>
      <CardContent>
        {activity.length === 0 ? (
          <EmptyState
            title="No recent activity"
            description="No bounded audit activity was included in this operator status response."
            className="py-8"
          />
        ) : (
          <div className="space-y-2">
            {activity.map((item) => (
              <ActivitySummary key={item.id} activity={item} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function SafetyFlags({ status }: { status: OperatorStatus }) {
  return (
    <Card data-testid="operator-safety-flags">
      <CardHeader>
        <CardTitle>Safety Flags</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-6 md:grid-cols-2">
          <FlagRow
            label="Endpoint read-only"
            value={status.safety_flags.endpoint_read_only}
          />
          <FlagRow
            label="Creates orders"
            value={status.safety_flags.creates_orders}
            riskyWhenTrue
          />
          <FlagRow
            label="Calls brokers"
            value={status.safety_flags.calls_brokers}
            riskyWhenTrue
          />
          <FlagRow
            label="Triggers schedulers"
            value={status.safety_flags.triggers_schedulers}
            riskyWhenTrue
          />
          <FlagRow
            label="Runs strategies"
            value={status.safety_flags.runs_strategies}
            riskyWhenTrue
          />
          <FlagRow
            label="DCA runnable"
            value={status.safety_flags.dca_runnable}
            riskyWhenTrue
          />
          <FlagRow
            label="DCA live enabled"
            value={status.safety_flags.dca_live_enabled}
            riskyWhenTrue
          />
          <FlagRow
            label="Kraken live enabled"
            value={status.safety_flags.kraken_live_enabled}
            riskyWhenTrue
          />
          <FlagRow
            label="Live trading enabled (env setting)"
            value={status.safety_flags.live_trading_enabled_setting}
            riskyWhenTrue
          />
          <FlagRow
            label="Live trading unlocked (app)"
            value={status.safety_flags.app_live_trading_unlocked}
            riskyWhenTrue
          />
          <FlagRow
            label="Expected venue configs missing"
            value={status.safety_flags.missing_expected_venue_configs}
            riskyWhenTrue
          />
          <FlagRow
            label="Any venue kill switch active"
            value={status.safety_flags.any_venue_kill_switch_active}
            riskyWhenTrue
          />
          <FlagRow
            label="Any venue degraded"
            value={status.safety_flags.any_venue_degraded}
            riskyWhenTrue
          />
          <FlagRow
            label="Worker health known"
            value={status.safety_flags.worker_health_known}
          />
          <FlagRow
            label="Cash-only mode"
            value={status.safety_flags.cash_only_mode}
          />
        </dl>
      </CardContent>
    </Card>
  );
}

function DemoReconciliationStatusCard({
  status,
  schedulerStatus,
  isLoading,
  schedulerLoading,
  error,
  schedulerError,
}: {
  status?: DemoReconciliationWorkerStatus;
  schedulerStatus?: DemoReconciliationSchedulerStatus;
  isLoading?: boolean;
  schedulerLoading?: boolean;
  error?: unknown;
  schedulerError?: unknown;
}) {
  const latest = status?.last_run_summary as
    | {
        outcome?: string;
        candidates_found?: number;
        attempted?: number;
        succeeded?: number;
        failed?: number;
        rate_limited?: number;
      }
    | null
    | undefined;
  const schedulerLatest = schedulerStatus?.last_run_summary as
    | {
        candidates_found?: number;
        attempted?: number;
        succeeded?: number;
        failed?: number;
        rate_limited?: number;
      }
    | null
    | undefined;
  const latestCounts = schedulerLatest ?? latest;
  const backingOff = Boolean(schedulerStatus?.next_run_not_before);
  const rateLimited =
    Number(schedulerLatest?.rate_limited ?? latest?.rate_limited ?? 0) > 0 ||
    schedulerStatus?.last_run_outcome === "rate_limited";
  const schedulerWarnings = [
    ...(schedulerStatus?.enabled === false ? ["Scheduler disabled by config."] : []),
    ...(schedulerStatus?.worker_enabled === false ? ["Worker disabled by config."] : []),
    ...(backingOff ? ["Rate limited/backing off."] : []),
    ...((schedulerStatus?.warnings ?? []).map((warning) => `Unsafe config: ${warning}`)),
  ];

  return (
    <Card data-testid="demo-reconciliation-status">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <RefreshCw className="h-4 w-4 text-cyan-300" />
          Trading 212 Demo Reconciliation
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading || schedulerLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-4 w-36" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : error || schedulerError ? (
          <div className="rounded-md border border-amber-700/50 bg-amber-950/20 p-3 text-sm text-amber-100">
            Reconciliation status unavailable.
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              <TextBadge tone={status?.enabled ? "success" : "warning"}>
                {status?.enabled ? "Worker enabled" : "Worker disabled"}
              </TextBadge>
              <TextBadge tone={schedulerStatus?.enabled ? "success" : "warning"}>
                {schedulerStatus?.enabled ? "Scheduler enabled" : "Scheduler disabled"}
              </TextBadge>
              <TextBadge tone={schedulerStatus?.running ? "info" : "outline"}>
                Running {schedulerStatus?.running ? "Yes" : "No"}
              </TextBadge>
              <TextBadge
                tone={status?.live_trading_enabled ? "destructive" : "success"}
              >
                Live {status?.live_trading_enabled ? "enabled" : "disabled"}
              </TextBadge>
              <TextBadge tone={backingOff || rateLimited ? "warning" : "info"}>
                {backingOff ? "Backing off" : rateLimited ? "Rate limited" : "Read-only"}
              </TextBadge>
            </div>
            <dl className="grid gap-2 text-sm">
              <InfoRow
                label="Broker environment"
                value={status?.broker_environment ?? "Unknown"}
              />
              <InfoRow
                label="Safety state"
                value={status?.safety_state ?? "Unknown"}
                valueClassName={
                  status?.safety_state === "safe"
                    ? "text-emerald-400"
                    : "text-amber-300"
                }
              />
              <InfoRow
                label="Last outcome"
                value={schedulerStatus?.last_run_outcome ?? latest?.outcome ?? "No runs yet"}
              />
              <InfoRow
                label="Last run"
                value={
                  schedulerStatus?.last_run_finished_at
                    ? formatDate(schedulerStatus.last_run_finished_at)
                    : status?.last_run_at
                      ? formatDate(status.last_run_at)
                      : "Never"
                }
              />
              <InfoRow
                label="Next run"
                value={
                  schedulerStatus?.next_run_not_before
                    ? formatDate(schedulerStatus.next_run_not_before)
                    : schedulerStatus?.next_run_at
                      ? formatDate(schedulerStatus.next_run_at)
                      : "Not scheduled"
                }
              />
              <InfoRow
                label="Interval"
                value={`${schedulerStatus?.interval_seconds ?? 0}s`}
              />
              <InfoRow
                label="Consecutive rate limits"
                value={schedulerStatus?.consecutive_rate_limits ?? 0}
              />
              <InfoRow
                label="Total rate-limited runs"
                value={schedulerStatus?.total_rate_limited_runs ?? 0}
              />
            </dl>
            {schedulerWarnings.length > 0 && (
              <div className="rounded-md border border-amber-700/50 bg-amber-950/20 p-3 text-xs text-amber-100">
                <ul className="space-y-1">
                  {schedulerWarnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="grid grid-cols-4 gap-2 text-center text-xs">
              <div className="rounded-md border border-slate-800 bg-slate-900/60 p-2">
                <p className="text-slate-500">Found</p>
                <p className="mt-1 font-semibold text-slate-100">
                  {latestCounts?.candidates_found ?? 0}
                </p>
              </div>
              <div className="rounded-md border border-slate-800 bg-slate-900/60 p-2">
                <p className="text-slate-500">Tried</p>
                <p className="mt-1 font-semibold text-slate-100">
                  {latestCounts?.attempted ?? 0}
                </p>
              </div>
              <div className="rounded-md border border-slate-800 bg-slate-900/60 p-2">
                <p className="text-slate-500">OK</p>
                <p className="mt-1 font-semibold text-emerald-300">
                  {latestCounts?.succeeded ?? 0}
                </p>
              </div>
              <div className="rounded-md border border-slate-800 bg-slate-900/60 p-2">
                <p className="text-slate-500">Limited</p>
                <p className="mt-1 font-semibold text-amber-300">
                  {latestCounts?.rate_limited ?? 0}
                </p>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

export function OperatorDashboard({
  status,
  isLoading = false,
  isError = false,
}: OperatorDashboardProps) {
  const brokerStatusQuery = useBrokerStatus();
  const demoReconciliationQuery = useDemoReconciliationStatus();
  const demoReconciliationSchedulerQuery = useDemoReconciliationSchedulerStatus();
  if (isLoading) return <LoadingState />;
  if (isError || !status) return <ErrorState />;

  return (
    <div className="space-y-5">
      <TopSafetySummary status={status} />
      <ExecutionBoundary status={status} />

      <section className="grid gap-4 xl:grid-cols-2" aria-label="Venue status">
        {status.venues.map((venue) => (
          <VenueCard key={venue.venue} venue={venue} />
        ))}
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        <Trading212Summary status={status} />
        <KrakenSummary status={status} />
      </section>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
        <DcaSummary status={status} />
        <SchedulerWorkerHealth status={status} />
      </section>

      <DemoReconciliationStatusCard
        status={demoReconciliationQuery.data}
        schedulerStatus={demoReconciliationSchedulerQuery.data}
        isLoading={demoReconciliationQuery.isLoading}
        schedulerLoading={demoReconciliationSchedulerQuery.isLoading}
        error={demoReconciliationQuery.error}
        schedulerError={demoReconciliationSchedulerQuery.error}
      />

      <PaperExecutionSummary status={status} />
      <PaperExecutionHistoryPanel />
      <SafetyFlags status={status} />
      <RecentActivity status={status} />

      <Card className="border-blue-500/20 bg-blue-500/5">
        <CardContent className="flex flex-wrap items-center gap-3 pt-5 text-xs text-blue-100/80">
          <ListChecks className="h-4 w-4 text-blue-300" />
          <span>
            Visibility only: no enable, disable, execute, trade, buy, sell,
            scheduler, broker, or live controls are available here.
          </span>
        </CardContent>
      </Card>
      <BrokerStatusPanel
        status={brokerStatusQuery.data}
        isLoading={brokerStatusQuery.isLoading}
        error={brokerStatusQuery.error}
      />
    </div>
  );
}
