# UI Design Pass — Inner Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Roll the new design system (TerminalCard stat rows + consistent icon page headers) across all 10 inner pages that were not touched in the initial Dashboard/Settings redesign.

**Architecture:** Create a shared `PageHeader` component, export it from the UI barrel, then apply it to every inner page. Three pages (Positions, Broker, Reports) additionally swap their `StatCard` summary rows to `TerminalCard`. All other pages get header-only treatment. The `StatCard` in Backtest's dense results grids stays as-is.

**Tech Stack:** Next.js 14 App Router, React, TypeScript, Tailwind CSS, Lucide React, Jest + React Testing Library.

---

## File Map

| File | Action |
|------|--------|
| `apps/web/components/layout/page-header.tsx` | **Create** — PageHeader component |
| `apps/web/components/ui/index.tsx` | **Modify** — add PageHeader export |
| `apps/web/tests/unit/components/page-header.test.tsx` | **Create** — unit tests |
| `apps/web/app/app/instruments/page.tsx` | **Modify** — swap inline header → PageHeader |
| `apps/web/app/app/orders/page.tsx` | **Modify** — add PageHeader |
| `apps/web/app/app/alerts/page.tsx` | **Modify** — add PageHeader |
| `apps/web/app/app/risk/page.tsx` | **Modify** — add PageHeader |
| `apps/web/app/app/emergency/page.tsx` | **Modify** — add PageHeader |
| `apps/web/app/app/strategies/page.tsx` | **Modify** — add PageHeader |
| `apps/web/app/app/journal/page.tsx` | **Modify** — swap inline header → PageHeader |
| `apps/web/app/app/audit/page.tsx` | **Modify** — add PageHeader |
| `apps/web/app/app/backtest/page.tsx` | **Modify** — add PageHeader |
| `apps/web/app/app/positions/page.tsx` | **Modify** — PageHeader + TerminalCard summary row |
| `apps/web/app/app/broker/page.tsx` | **Modify** — PageHeader + TerminalCard summary row |
| `apps/web/app/app/reports/page.tsx` | **Modify** — PageHeader + TerminalCard top stats |

---

## Task 1: Create the PageHeader component

**Files:**
- Create: `apps/web/components/layout/page-header.tsx`
- Create: `apps/web/tests/unit/components/page-header.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `apps/web/tests/unit/components/page-header.test.tsx`:

```tsx
import '@testing-library/jest-dom/jest-globals'
import { describe, expect, it } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import { PageHeader } from '@/components/layout/page-header'
import { Database } from 'lucide-react'

describe('PageHeader', () => {
  it('renders label', () => {
    render(<PageHeader icon={<Database />} label="Instruments" />)
    expect(screen.getByText('Instruments')).toBeTruthy()
  })

  it('renders sub text when provided', () => {
    render(<PageHeader icon={<Database />} label="Instruments" sub="1,234 instruments" />)
    expect(screen.getByText('1,234 instruments')).toBeTruthy()
  })

  it('does not render sub when omitted', () => {
    render(<PageHeader icon={<Database />} label="Instruments" />)
    expect(screen.queryByRole('paragraph')).toBeNull()
  })

  it('renders actions slot', () => {
    render(<PageHeader icon={<Database />} label="X" actions={<button>Sync</button>} />)
    expect(screen.getByRole('button', { name: 'Sync' })).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
cd apps/web && npx jest tests/unit/components/page-header.test.tsx --no-coverage
```

Expected: FAIL — `Cannot find module '@/components/layout/page-header'`

- [ ] **Step 3: Create the component**

Create `apps/web/components/layout/page-header.tsx`:

```tsx
import { cn } from '@/lib/utils'

interface PageHeaderProps {
  icon: React.ReactNode
  label: string
  sub?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}

export function PageHeader({ icon, label, sub, actions, className }: PageHeaderProps) {
  return (
    <div className={cn('flex items-start justify-between gap-4', className)}>
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-border/60 bg-primary/10 text-primary flex-shrink-0">
          {icon}
        </div>
        <div>
          <h2 className="text-xl font-semibold tracking-tight">{label}</h2>
          {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
        </div>
      </div>
      {actions && <div className="flex-shrink-0">{actions}</div>}
    </div>
  )
}
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
cd apps/web && npx jest tests/unit/components/page-header.test.tsx --no-coverage
```

Expected: PASS — 4 tests pass

- [ ] **Step 5: Export from the UI barrel**

In `apps/web/components/ui/index.tsx`, add after the `TerminalCard` export line (line 308):

```ts
export { PageHeader } from '../layout/page-header'
```

- [ ] **Step 6: Commit**

```bash
git add apps/web/components/layout/page-header.tsx \
        apps/web/components/ui/index.tsx \
        apps/web/tests/unit/components/page-header.test.tsx
git commit -m "feat: add PageHeader layout component"
```

---

## Task 2: Refactor Instruments page to use PageHeader

The Instruments page already has the correct icon-header pattern inline — swap it to `PageHeader`.

**Files:**
- Modify: `apps/web/app/app/instruments/page.tsx`

- [ ] **Step 1: Update the import line**

Replace:
```tsx
import { Search, RefreshCw, CheckCircle2, XCircle, Database } from 'lucide-react'
import { useInstruments, useSyncInstruments } from '@/hooks/use-api'
import { Button, Card, CardContent, Input, Badge, Spinner, EmptyState } from '@/components/ui'
```

With:
```tsx
import { Search, RefreshCw, CheckCircle2, XCircle, Database } from 'lucide-react'
import { useInstruments, useSyncInstruments } from '@/hooks/use-api'
import { Button, Card, CardContent, Input, Badge, Spinner, EmptyState, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace the entire inline header `<div>` (lines 24–42 in the file):

```tsx
      {/* Page header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-border/60 bg-primary/10 text-primary">
            <Database className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-xl font-semibold tracking-tight">Instruments</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {total > 0
                ? <><span className="tnum">{total.toLocaleString()}</span> instruments · <span className="tnum">{enabledCount}</span> trading enabled</>
                : 'Sync from your broker to populate the instrument list'}
            </p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={() => syncMutation.mutate()} loading={syncMutation.isPending}>
          <RefreshCw className="h-3.5 w-3.5" />
          Sync from Broker
        </Button>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<Database className="h-5 w-5" />}
        label="Instruments"
        sub={total > 0
          ? <><span className="tnum">{total.toLocaleString()}</span> instruments · <span className="tnum">{enabledCount}</span> trading enabled</>
          : 'Sync from your broker to populate the instrument list'}
        actions={
          <Button variant="outline" size="sm" onClick={() => syncMutation.mutate()} loading={syncMutation.isPending}>
            <RefreshCw className="h-3.5 w-3.5" />
            Sync from Broker
          </Button>
        }
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/instruments/page.tsx
git commit -m "refactor(instruments): use PageHeader component"
```

---

## Task 3: Add PageHeader to Orders page

**Files:**
- Modify: `apps/web/app/app/orders/page.tsx`

- [ ] **Step 1: Add PageHeader and ClipboardList to imports**

Replace:
```tsx
import { Eye, RefreshCw, X } from 'lucide-react'
import { useOrder, useOrders, useCancelOrder, useCancelAllPending } from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState } from '@/components/ui'
```

With:
```tsx
import { Eye, RefreshCw, X, ClipboardList } from 'lucide-react'
import { useOrder, useOrders, useCancelOrder, useCancelAllPending } from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Orders</h2>
          <p className="text-[13px] text-muted-foreground mt-1">
            {allOrders.length} total
            {pendingCount > 0 && <span> · <span className="text-amber-400 font-medium">{pendingCount} pending</span></span>}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => qc.invalidateQueries({ queryKey: ['orders'] })}>
            <RefreshCw className="w-3.5 h-3.5" />
            Refresh
          </Button>
          {pendingCount > 0 && (
            <Button variant="danger" size="sm" onClick={() => setShowCancelAll(true)}>
              <X className="w-3.5 h-3.5" />
              Cancel All ({pendingCount})
            </Button>
          )}
        </div>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<ClipboardList className="h-5 w-5" />}
        label="Orders"
        sub={<>
          {allOrders.length} total
          {pendingCount > 0 && <> · <span className="text-amber-400 font-medium">{pendingCount} pending</span></>}
        </>}
        actions={
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => qc.invalidateQueries({ queryKey: ['orders'] })}>
              <RefreshCw className="w-3.5 h-3.5" />
              Refresh
            </Button>
            {pendingCount > 0 && (
              <Button variant="danger" size="sm" onClick={() => setShowCancelAll(true)}>
                <X className="w-3.5 h-3.5" />
                Cancel All ({pendingCount})
              </Button>
            )}
          </div>
        }
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/orders/page.tsx
git commit -m "feat(orders): add PageHeader"
```

---

## Task 4: Add PageHeader to Alerts page

**Files:**
- Modify: `apps/web/app/app/alerts/page.tsx`

- [ ] **Step 1: Update imports**

Replace:
```tsx
import { Bell, CheckCheck } from 'lucide-react'
import { useAlerts } from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState } from '@/components/ui'
```

With:
```tsx
import { Bell, CheckCheck } from 'lucide-react'
import { useAlerts } from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Alerts</h2>
          <p className="text-[13px] text-muted-foreground mt-1">
            {unread > 0 ? (
              <span><span className="text-primary font-medium">{unread} unread</span> · {alerts.length} total</span>
            ) : (
              <span>{alerts.length} total · All caught up</span>
            )}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={sendTest}>
          <Bell className="w-3.5 h-3.5" />
          Send Test
        </Button>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<Bell className="h-5 w-5" />}
        label="Alerts"
        sub={unread > 0
          ? <><span className="text-primary font-medium">{unread} unread</span> · {alerts.length} total</>
          : <>{alerts.length} total · All caught up</>}
        actions={
          <Button variant="outline" size="sm" onClick={sendTest}>
            <Bell className="w-3.5 h-3.5" />
            Send Test
          </Button>
        }
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/alerts/page.tsx
git commit -m "feat(alerts): add PageHeader"
```

---

## Task 5: Add PageHeader to Risk page

**Files:**
- Modify: `apps/web/app/app/risk/page.tsx`

- [ ] **Step 1: Update imports**

Replace:
```tsx
import { ShieldAlert, ShieldOff, Save, AlertTriangle } from 'lucide-react'
import { useRiskProfile, useUpdateRiskProfile, useRiskEvents, useKillSwitch, useSettings } from '@/hooks/use-api'
import { Button, Card, CardHeader, CardTitle, CardContent, Input, Label, Spinner } from '@/components/ui'
```

With:
```tsx
import { ShieldAlert, ShieldOff, Save, AlertTriangle } from 'lucide-react'
import { useRiskProfile, useUpdateRiskProfile, useRiskEvents, useKillSwitch, useSettings } from '@/hooks/use-api'
import { Button, Card, CardHeader, CardTitle, CardContent, Input, Label, Spinner, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Risk Controls</h2>
        <p className="text-[13px] text-muted-foreground mt-1">
          Limits, guards, and circuit breakers
        </p>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<ShieldAlert className="h-5 w-5" />}
        label="Risk Controls"
        sub="Limits, guards, and circuit breakers"
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/risk/page.tsx
git commit -m "feat(risk): add PageHeader"
```

---

## Task 6: Add PageHeader to Emergency page

**Files:**
- Modify: `apps/web/app/app/emergency/page.tsx`

- [ ] **Step 1: Update imports**

Replace:
```tsx
import { AlertOctagon, XCircle, TrendingDown, Power, PowerOff } from 'lucide-react'
import {
  useSettings, useEmergencyKillSwitch, useEmergencyAutoTradingOff,
  useEmergencyAutoTradingOn, useEmergencyCancelAll, useEmergencyFlattenAll,
} from '@/hooks/use-api'
import { Button, Card, CardContent, Spinner } from '@/components/ui'
```

With:
```tsx
import { AlertOctagon, XCircle, TrendingDown, Power, PowerOff } from 'lucide-react'
import {
  useSettings, useEmergencyKillSwitch, useEmergencyAutoTradingOff,
  useEmergencyAutoTradingOn, useEmergencyCancelAll, useEmergencyFlattenAll,
} from '@/hooks/use-api'
import { Button, Card, CardContent, Spinner, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Emergency Controls</h2>
        <p className="text-[13px] text-muted-foreground mt-1">
          Kill switch and rapid unwind · All actions are audit-logged
        </p>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<AlertOctagon className="h-5 w-5" />}
        label="Emergency Controls"
        sub="Kill switch and rapid unwind · All actions are audit-logged"
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/emergency/page.tsx
git commit -m "feat(emergency): add PageHeader"
```

---

## Task 7: Add PageHeader to Strategies page

**Files:**
- Modify: `apps/web/app/app/strategies/page.tsx`

- [ ] **Step 1: Update imports**

The `Zap` icon is already imported. Add `PageHeader` to the UI import:

Replace:
```tsx
import { Button, Card, CardContent, Badge, Spinner, EmptyState } from '@/components/ui'
```

With:
```tsx
import { Button, Card, CardContent, Badge, Spinner, EmptyState, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Strategies</h2>
          <p className="text-[13px] text-muted-foreground mt-1">
            {active.length} active · {strategies.length} total
            {totalTrades > 0 && ` · ${totalTrades} trades (30d)`}
          </p>
        </div>
        <div className="flex gap-2">
          {totalPnl !== 0 && (
            <span className={cn('text-sm font-bold self-center', pnlClass(totalPnl))}>
              {formatPnL(totalPnl)} 30d
            </span>
          )}
          <Button variant="outline" size="sm" onClick={createPortfolioDemo} loading={createMutation.isPending}>
            <TrendingUp className="w-3.5 h-3.5" />
            Add Core Portfolio
          </Button>
          <Button variant="outline" size="sm" onClick={createDemo} loading={createMutation.isPending}>
            <FlaskConical className="w-3.5 h-3.5" />
            Add Demo ORB
          </Button>
```

With:
```tsx
      <PageHeader
        icon={<Zap className="h-5 w-5" />}
        label="Strategies"
        sub={<>
          {active.length} active · {strategies.length} total
          {totalTrades > 0 && ` · ${totalTrades} trades (30d)`}
        </>}
        actions={
          <div className="flex gap-2">
            {totalPnl !== 0 && (
              <span className={cn('text-sm font-bold self-center', pnlClass(totalPnl))}>
                {formatPnL(totalPnl)} 30d
              </span>
            )}
            <Button variant="outline" size="sm" onClick={createPortfolioDemo} loading={createMutation.isPending}>
              <TrendingUp className="w-3.5 h-3.5" />
              Add Core Portfolio
            </Button>
            <Button variant="outline" size="sm" onClick={createDemo} loading={createMutation.isPending}>
              <FlaskConical className="w-3.5 h-3.5" />
              Add Demo ORB
            </Button>
```

Then close with `</div>} />` (replacing the old closing `</div></div>`).

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/strategies/page.tsx
git commit -m "feat(strategies): add PageHeader"
```

---

## Task 8: Add PageHeader to Journal page

The Journal page already has an inline icon-header block — swap it to `PageHeader`.

**Files:**
- Modify: `apps/web/app/app/journal/page.tsx`

- [ ] **Step 1: Update imports**

`BookOpen` is already imported. Add `PageHeader` to the UI import:

Replace:
```tsx
import {
  Card, CardContent, CardHeader, CardTitle,
  Button, Badge, Spinner, EmptyState,
} from '@/components/ui'
```

With:
```tsx
import {
  Card, CardContent, CardHeader, CardTitle,
  Button, Badge, Spinner, EmptyState, PageHeader,
} from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-primary/10 border border-primary/25 flex items-center justify-center flex-shrink-0">
            <BookOpen className="w-4 h-4 text-primary" />
          </div>
          <div>
            <h2 className="text-xl font-semibold tracking-tight">Trade Journal</h2>
            <p className="text-[13px] text-muted-foreground mt-1 tabular-nums">
              {total} trades · {journaledCount} journaled this page
            </p>
          </div>
        </div>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<BookOpen className="h-5 w-5" />}
        label="Trade Journal"
        sub={`${total} trades · ${journaledCount} journaled this page`}
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/journal/page.tsx
git commit -m "feat(journal): use PageHeader component"
```

---

## Task 9: Add PageHeader to Audit page

**Files:**
- Modify: `apps/web/app/app/audit/page.tsx`

- [ ] **Step 1: Update imports**

Replace:
```tsx
import { Search, ChevronDown, ChevronRight } from 'lucide-react'
import { useAuditLogs } from '@/hooks/use-api'
import { Button, Card, CardContent, Input, Spinner, EmptyState } from '@/components/ui'
```

With:
```tsx
import { Search, ChevronDown, ChevronRight, ScrollText } from 'lucide-react'
import { useAuditLogs } from '@/hooks/use-api'
import { Button, Card, CardContent, Input, Spinner, EmptyState, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Audit Log</h2>
          <p className="text-[13px] text-muted-foreground mt-1 tabular-nums">
            {total.toLocaleString()} events recorded
          </p>
        </div>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<ScrollText className="h-5 w-5" />}
        label="Audit Log"
        sub={<span className="tabular-nums">{total.toLocaleString()} events recorded</span>}
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/audit/page.tsx
git commit -m "feat(audit): add PageHeader"
```

---

## Task 10: Add PageHeader to Backtest page

**Files:**
- Modify: `apps/web/app/app/backtest/page.tsx`

- [ ] **Step 1: Update imports**

`FlaskConical` is already imported. Add `PageHeader` to the UI import. The existing UI import in backtest is:

```tsx
import {
  Badge,
  Button,
  Card,
```

Find the full UI import block and add `PageHeader` to it.

- [ ] **Step 2: Replace the inline header block**

Replace:
```tsx
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Backtest Engine</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Research strategies in demo-first mode with execution costs, drawdown controls, and walk-forward checks.
        </p>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<FlaskConical className="h-5 w-5" />}
        label="Backtest Engine"
        sub="Research strategies in demo-first mode with execution costs, drawdown controls, and walk-forward checks."
      />
```

- [ ] **Step 3: Commit**

```bash
git add apps/web/app/app/backtest/page.tsx
git commit -m "feat(backtest): add PageHeader"
```

---

## Task 11: Positions — PageHeader + TerminalCard summary row

**Files:**
- Modify: `apps/web/app/app/positions/page.tsx`

- [ ] **Step 1: Update imports**

Replace:
```tsx
import { RefreshCw, TrendingUp, TrendingDown } from 'lucide-react'
import { usePositions } from '@/hooks/use-api'
import { Button, Card, CardContent, Spinner, EmptyState, StatCard } from '@/components/ui'
```

With:
```tsx
import { RefreshCw, TrendingUp, TrendingDown } from 'lucide-react'
import { usePositions } from '@/hooks/use-api'
import { Button, Card, CardContent, Spinner, EmptyState, TerminalCard, PageHeader } from '@/components/ui'
```

- [ ] **Step 2: Replace header**

Replace:
```tsx
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Positions</h2>
          <p className="text-[13px] text-muted-foreground mt-1">
            {positions.length} open position{positions.length !== 1 ? 's' : ''}
            {positions.length > 0 && ` · ${formatCurrency(totalValue)} invested`}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh}>
          <RefreshCw className="w-3.5 h-3.5" />
          Refresh
        </Button>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<TrendingUp className="h-5 w-5" />}
        label="Positions"
        sub={<>
          {positions.length} open position{positions.length !== 1 ? 's' : ''}
          {positions.length > 0 && <> · {formatCurrency(totalValue)} invested</>}
        </>}
        actions={
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCw className="w-3.5 h-3.5" />
            Refresh
          </Button>
        }
      />
```

- [ ] **Step 3: Replace the StatCard summary row**

Replace:
```tsx
      {positions.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <StatCard
            label="Position Value"
            value={formatCurrency(totalValue)}
            sub="Current market value"
          />
          <StatCard
            label="Unrealized P&L"
            value={<span className={pnlClass(totalPnl)}>{formatPnL(totalPnl)}</span>}
            trend={totalPnl > 0 ? 'up' : totalPnl < 0 ? 'down' : 'neutral'}
            sub={totalPnl > 0 ? 'Profitable' : totalPnl < 0 ? 'In drawdown' : 'Flat'}
          />
          <StatCard
            label="Open Positions"
            value={positions.length.toString()}
            sub="Across all symbols"
          />
        </div>
      )}
```

With:
```tsx
      {positions.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <TerminalCard
            label="Position Value"
            value={formatCurrency(totalValue)}
            sub="Current market value"
            variant="cyan"
          />
          <TerminalCard
            label="Unrealized P&L"
            value={formatPnL(totalPnl)}
            sub={totalPnl > 0 ? 'Profitable' : totalPnl < 0 ? 'In drawdown' : 'Flat'}
            variant={totalPnl > 0 ? 'teal' : totalPnl < 0 ? 'red' : 'cyan'}
          />
          <TerminalCard
            label="Open Positions"
            value={positions.length.toString()}
            sub="Across all symbols"
            variant="cyan"
          />
        </div>
      )}
```

- [ ] **Step 4: Commit**

```bash
git add apps/web/app/app/positions/page.tsx
git commit -m "feat(positions): PageHeader + TerminalCard summary row"
```

---

## Task 12: Broker — PageHeader + TerminalCard summary row

**Files:**
- Modify: `apps/web/app/app/broker/page.tsx`

- [ ] **Step 1: Update imports**

Replace:
```tsx
import { Button, Card, CardHeader, CardTitle, CardContent, Input, Label, Badge, StatCard, Spinner } from '@/components/ui'
```

With:
```tsx
import { Button, Card, CardHeader, CardTitle, CardContent, Input, Label, Badge, TerminalCard, Spinner, PageHeader } from '@/components/ui'
```

Add `PlugZap` is already imported. No icon changes needed — `PlugZap` will be used for the PageHeader icon.

- [ ] **Step 2: Replace the inline header**

Replace:
```tsx
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Broker Account</h2>
        <p className="text-[13px] text-muted-foreground mt-1">
          Trading 212 connection and account details
        </p>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<PlugZap className="h-5 w-5" />}
        label="Broker Account"
        sub="Trading 212 connection and account details"
      />
```

- [ ] **Step 3: Replace the StatCard account summary row**

Replace:
```tsx
                <StatCard label="Total Value" value={formatCurrency(account.total_value, account.currency)} />
                <StatCard label="Cash" value={formatCurrency(account.cash, account.currency)} />
                <StatCard label="Available to Trade" value={formatCurrency(account.free_funds, account.currency)} />
```

With:
```tsx
                <TerminalCard label="Total Value" value={formatCurrency(account.total_value, account.currency)} variant="cyan" />
                <TerminalCard label="Cash" value={formatCurrency(account.cash, account.currency)} variant="cyan" />
                <TerminalCard label="Available to Trade" value={formatCurrency(account.free_funds, account.currency)} variant="teal" />
```

- [ ] **Step 4: Commit**

```bash
git add apps/web/app/app/broker/page.tsx
git commit -m "feat(broker): PageHeader + TerminalCard summary row"
```

---

## Task 13: Reports — PageHeader + TerminalCard top stats

**Files:**
- Modify: `apps/web/app/app/reports/page.tsx`

- [ ] **Step 1: Update imports**

Replace:
```tsx
import { Card, CardHeader, CardTitle, CardContent, StatCard, Spinner, EmptyState, Button, Badge } from '@/components/ui'
```

With:
```tsx
import { Card, CardHeader, CardTitle, CardContent, TerminalCard, Spinner, EmptyState, Button, Badge, PageHeader } from '@/components/ui'
```

Add `BarChart2` is already imported — use it for the PageHeader icon.

- [ ] **Step 2: Replace the existing header with PageHeader**

Replace (lines 48–57, the existing header div):
```tsx
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Reports</h2>
          <p className="text-[13px] text-muted-foreground mt-1">Performance analytics and trade history</p>
        </div>
        <Button variant="outline" size="sm" onClick={exportCSV} disabled={!trades.length}>
          <Download className="w-3.5 h-3.5" />
          Export CSV
        </Button>
      </div>
```

With:
```tsx
      <PageHeader
        icon={<BarChart2 className="h-5 w-5" />}
        label="Reports"
        sub="Performance analytics and trade history"
        actions={
          <Button variant="outline" size="sm" onClick={exportCSV} disabled={!trades.length}>
            <Download className="w-3.5 h-3.5" />
            Export CSV
          </Button>
        }
      />
```

- [ ] **Step 3: Replace first StatCard grid (perf stats — 4 cards)**

Replace:
```tsx
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Total Trades" value={perf.total_trades.toString()} />
            <StatCard label="Win Rate"
              value={<span className={perf.win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400'}>{(perf.win_rate * 100).toFixed(1)}%</span>}
              sub={`${perf.winning_trades}W · ${perf.losing_trades}L`} />
            <StatCard label="Total P&L"
              value={<span className={pnlClass(perf.total_pnl)}>{perf.total_pnl >= 0 ? '+' : ''}{formatCurrency(perf.total_pnl)}</span>}
              trend={perf.total_pnl > 0 ? 'up' : perf.total_pnl < 0 ? 'down' : 'neutral'} />
            <StatCard label="Profit Factor"
              value={<span className={perf.profit_factor >= 1 ? 'text-emerald-400' : 'text-red-400'}>{perf.profit_factor.toFixed(2)}</span>}
              sub={perf.profit_factor >= 1 ? 'Profitable' : 'Unprofitable'} />
          </div>
```

With:
```tsx
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <TerminalCard label="Total Trades" value={perf.total_trades.toString()} variant="cyan" />
            <TerminalCard
              label="Win Rate"
              value={`${(perf.win_rate * 100).toFixed(1)}%`}
              sub={`${perf.winning_trades}W · ${perf.losing_trades}L`}
              variant={perf.win_rate >= 0.5 ? 'teal' : 'red'}
            />
            <TerminalCard
              label="Total P&L"
              value={`${perf.total_pnl >= 0 ? '+' : ''}${formatCurrency(perf.total_pnl)}`}
              variant={perf.total_pnl > 0 ? 'teal' : perf.total_pnl < 0 ? 'red' : 'cyan'}
            />
            <TerminalCard
              label="Profit Factor"
              value={perf.profit_factor.toFixed(2)}
              sub={perf.profit_factor >= 1 ? 'Profitable' : 'Unprofitable'}
              variant={perf.profit_factor >= 1 ? 'teal' : 'red'}
            />
          </div>
```

- [ ] **Step 4: Replace second StatCard grid (Avg Win/Loss/Drawdown/Sharpe)**

Replace:
```tsx
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Avg Win" value={<span className="text-emerald-400">{formatCurrency(perf.avg_win)}</span>} />
            <StatCard label="Avg Loss" value={<span className="text-red-400">{formatCurrency(perf.avg_loss)}</span>} />
            <StatCard label="Max Drawdown" value={<span className="text-red-400">{formatCurrency(perf.max_drawdown)}</span>} />
            <StatCard label="Sharpe Ratio"
              value={perf.sharpe_ratio !== null
                ? <span className={perf.sharpe_ratio >= 1 ? 'text-emerald-400' : 'text-muted-foreground'}>{perf.sharpe_ratio.toFixed(2)}</span>
                : <span className="text-muted-foreground">—</span>}
              sub={perf.sharpe_ratio !== null ? 'Annualised' : 'Need more data'} />
          </div>
```

With:
```tsx
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <TerminalCard label="Avg Win" value={formatCurrency(perf.avg_win)} variant="teal" />
            <TerminalCard label="Avg Loss" value={formatCurrency(perf.avg_loss)} variant="red" />
            <TerminalCard label="Max Drawdown" value={formatCurrency(perf.max_drawdown)} variant="red" />
            <TerminalCard
              label="Sharpe Ratio"
              value={perf.sharpe_ratio !== null ? perf.sharpe_ratio.toFixed(2) : '—'}
              sub={perf.sharpe_ratio !== null ? 'Annualised' : 'Need more data'}
              variant={perf.sharpe_ratio !== null && perf.sharpe_ratio >= 1 ? 'teal' : 'cyan'}
            />
          </div>
```

Note: The four StatCards in the Execution Quality section (Execution Score, Adverse Slippage, First Ack, Reject/Error) remain as `StatCard` — they are inside a sub-section, not top-level summary stats.

- [ ] **Step 5: Commit**

```bash
git add apps/web/app/app/reports/page.tsx
git commit -m "feat(reports): PageHeader + TerminalCard top stats"
```

---

## Task 14: Type-check and visual verification

- [ ] **Step 1: Run TypeScript check**

```bash
cd apps/web && npx tsc --noEmit
```

Expected: 0 errors

- [ ] **Step 2: Run the full test suite**

```bash
cd apps/web && npm test -- --passWithNoTests
```

Expected: all tests pass (including the new `page-header.test.tsx`)

- [ ] **Step 3: Start dev server and do visual pass**

```bash
cd apps/web && npm run dev
```

Visit each page in the browser and confirm:
- Every page has an icon box + title + subtitle header
- Positions, Broker, Reports top stats show TerminalCard styling (dark bordered card with cyan/teal/red accent)
- Backtest dense result grids still look compact (StatCard unchanged)
- No layout breakage on narrow viewport (resize to ~375px)

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: complete UI design-pass across inner pages"
```
