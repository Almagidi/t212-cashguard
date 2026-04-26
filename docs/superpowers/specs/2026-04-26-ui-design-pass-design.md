# UI Design Pass — Inner Pages

**Date:** 2026-04-26  
**Status:** Approved

## Goal

Complete the design-system rollout started on the Dashboard. Every inner page should share:
1. A consistent icon-header pattern at the top.
2. `TerminalCard` (cyan/teal/red tokens) for page-level summary stats.
3. No visual regressions on non-stat content.

## What is NOT in scope

- Dashboard — already redesigned with TerminalCard, draggable widgets, WS pill.
- Settings — already consistent.
- Backtest dense result grids (~30 StatCards inside walk-forward/portfolio tables) — these are analytical report rows, not summary stats. `StatCard` stays there.
- Execution-quality table rows in Reports — same reasoning.
- Semantic color variants per stat (cyan vs teal per meaning) — deferred to a follow-up pass.

## Approach: B — Extract PageHeader, then migrate

### 1. New `PageHeader` component

**File:** `apps/web/components/layout/page-header.tsx`

```tsx
interface PageHeaderProps {
  icon: React.ReactNode
  label: string
  sub?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}
```

- Icon rendered in a `h-10 w-10 rounded-xl border border-border/60 bg-primary/10 text-primary` box.
- `label` as `text-xl font-semibold tracking-tight`.
- `sub` as `text-xs text-muted-foreground mt-0.5`.
- `actions` right-aligned via flex justify-between.
- Exported from `apps/web/components/ui/index.tsx`.

### 2. StatCard → TerminalCard migrations

Three pages get their top summary stat rows replaced:

| Page | Stats | Variants |
|------|-------|----------|
| Positions | Total Value, Total P&L, # Open Positions | cyan, cyan (red if loss/teal if gain), cyan |
| Broker | Total Value, Cash, Available to Trade | cyan, cyan, cyan |
| Reports | Total Trades, Win Rate, Total P&L, Profit Factor, Avg Win, Avg Loss, Max Drawdown, Sharpe | cyan (neutral counts/ratios), teal (Avg Win), red (Avg Loss, Max Drawdown), dynamic on Total P&L |

Layout: `grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3` (match dashboard).

For Positions P&L: derive `variant` from the pnl value (`< 0 → 'red'`, `>= 0 → 'teal'`).

### 3. PageHeader rollout — all inner pages

| Page | Icon (lucide) | Sub-text |
|------|--------------|----------|
| Orders | `ClipboardList` | `{n} orders · {n} pending` |
| Positions | `TrendingUp` | `{n} open positions · {value} invested` |
| Strategies | `Zap` | `{n} strategies · {n} active` |
| Alerts | `Bell` | `{n} unread alerts` |
| Risk | `ShieldAlert` | `Risk profile & kill switch` |
| Journal | `BookOpen` | `{n} trades logged` |
| Audit | `ScrollText` | `System audit trail` |
| Backtest | `FlaskConical` | `Walk-forward & portfolio simulation` |
| Emergency | `AlertOctagon` | `Emergency controls` |
| Instruments | `Database` | existing sub-text (refactor inline → PageHeader) |

Instruments currently inlines the icon-header pattern manually — refactor it to use `PageHeader`.

## Files changed

| File | Change |
|------|--------|
| `components/layout/page-header.tsx` | **New** — PageHeader component |
| `components/ui/index.tsx` | Export PageHeader |
| `app/app/positions/page.tsx` | PageHeader + TerminalCard summary row |
| `app/app/broker/page.tsx` | PageHeader + TerminalCard summary row |
| `app/app/reports/page.tsx` | PageHeader + TerminalCard top stats |
| `app/app/orders/page.tsx` | PageHeader only |
| `app/app/strategies/page.tsx` | PageHeader only |
| `app/app/alerts/page.tsx` | PageHeader only |
| `app/app/risk/page.tsx` | PageHeader only |
| `app/app/journal/page.tsx` | PageHeader only |
| `app/app/audit/page.tsx` | PageHeader only |
| `app/app/backtest/page.tsx` | PageHeader only |
| `app/app/emergency/page.tsx` | PageHeader only |
| `app/app/instruments/page.tsx` | Refactor inline header → PageHeader |

## Testing

- Visual: dev server, visit each page and confirm header renders consistently.
- No regressions: check that stat values, sub-text, and actions (buttons) still appear correctly.
- Responsive: confirm grid collapses correctly on narrow viewports.
- No TypeScript errors (`tsc --noEmit`).
