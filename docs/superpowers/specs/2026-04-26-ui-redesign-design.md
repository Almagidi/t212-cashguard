# UI Redesign — Full Visual Identity Overhaul

**Date:** 2026-04-26  
**Status:** Approved  
**Scope:** Frontend only — zero backend impact

---

## 1. Goals

- Make the app feel unique and professional rather than generic-dashboard
- Introduce a cyan/teal fintech palette that mirrors across dark and light modes
- Overhaul the sidebar: stronger brand mark, gradient active states, live badges, collapsible rail
- Add dark/light mode toggle in topbar and settings
- Upgrade data surfaces to a terminal-card style with monospace values
- Formalise all component primitives into a single design-system CSS layer

---

## 2. Architecture & File Changes

| File | Change |
|---|---|
| `styles/globals.css` | Full palette swap, new design-system component layer |
| `tailwind.config.js` | Cyan/teal token additions, sidebar width tokens |
| `components/layout/sidebar.tsx` | New brand mark, gradient active pill, live badges, grouping, collapse toggle |
| `components/layout/topbar.tsx` | Theme toggle button, live UTC clock, refined right cluster |
| `components/ui/index.tsx` | `TerminalCard` primitive, updated badge/table primitives |
| `app/app/dashboard/page.tsx` | `GlassStat` replaced by `TerminalCard` |
| `app/app/settings/page.tsx` | Appearance section with theme toggle and system-default option |
| `stores/ui-store.ts` | New Zustand store: `sidebarCollapsed` (localStorage-persisted) |
| `app/providers.tsx` | `ThemeProvider` from `next-themes` wired in |

**New dependency:** `next-themes` — theme persistence and SSR flash prevention.  
**No other new dependencies.**

---

## 3. Palette & Theme System

### Primary accent
- Cyan primary: `hsl(192 85% 48%)`
- Teal secondary: `hsl(172 70% 42%)`

### Dark mode (default)
- Background: `hsl(220 20% 7%)` — deep cool charcoal
- Card surface: `hsl(220 18% 10%)`
- Surface-2: `hsl(220 16% 13%)`
- Border: `hsl(220 14% 16%)`
- Foreground: `hsl(210 20% 92%)`
- Muted foreground: `hsl(215 15% 52%)`
- Primary (cyan): `hsl(192 85% 48%)`
- Primary foreground: `hsl(220 20% 7%)`
- Shadows: deep blue-black, heavier than current

### Light mode (mirrored structure)
- Background: `hsl(210 20% 96%)` — pale cool grey
- Card surface: `hsl(0 0% 100%)`
- Surface-2: `hsl(210 18% 98%)`
- Border: `hsl(210 14% 88%)`
- Foreground: `hsl(220 20% 10%)`
- Muted foreground: `hsl(215 12% 45%)`
- Primary (cyan): `hsl(192 85% 40%)` — slightly darkened for contrast on light
- Same structural logic as dark, just flipped surfaces

### Semantic colours (unchanged — trading-critical)
- Profit: `hsl(142 71% 45%)`
- Loss: `hsl(0 84% 60%)`
- Warning: `hsl(38 92% 50%)`
- Live badge: red (unchanged)
- Demo badge: cyan (was blue)
- Mock badge: teal/muted (was purple)

### Theme persistence
- `next-themes` with `attribute="class"`, `defaultTheme="dark"`, `enableSystem`
- Preference stored in `localStorage`
- System preference used as initial default when no preference is set
- Script-tag SSR injection prevents flash-of-incorrect-theme on load

---

## 4. Sidebar Redesign

### Brand mark
- Custom inline SVG: geometric shield outline with a diagonal slash in cyan
- "CashGuard" in `font-semibold`, tight `tracking-tight`
- "Trading 212" in cyan-tinted muted colour (not plain grey)
- Replaces the current Zap icon + text

### Active nav state (gradient pill)
- Full-width rounded rect
- Left-side cyan vertical bar: `3px` wide, full item height, `bg-cyan-400`
- Horizontal gradient fill: `bg-gradient-to-r from-cyan-500/12 to-transparent`
- Label: `text-cyan-400 font-medium`
- Icon: `text-cyan-400`
- Inactive: cool muted grey, hover at `from-cyan-500/6 to-transparent`

### Nav grouping (four named sections)
| Section | Items |
|---|---|
| Trading | Dashboard, Broker, Instruments, Strategies |
| Operations | Orders, Positions, Risk Controls, Backtest |
| Monitoring | Alerts, Reports, Journal, Audit Log |
| System (bottom) | Settings, Emergency |

Section headers: small-caps, `text-[10px]`, `tracking-[0.1em]`, `text-cyan-500/60 font-semibold uppercase`

### Live count badges
- Alerts, Orders, Positions: small pill badge when count > 0
- Alerts: `bg-cyan-500 text-background` pill
- Orders / Positions: `bg-muted text-muted-foreground` pill
- Data sourced from existing React Query hooks — no new API calls
- Badge hidden when count is 0

### Collapse toggle
- Chevron button at sidebar bottom (above system section)
- Collapsed state: `w-16` icon-rail with tooltips on hover
- Expanded state: `w-56` (current)
- State in `ui-store.ts` (`sidebarCollapsed: boolean`, `localStorage`-persisted)
- Main content `ml-*` adjusts via class swap — no layout recalculation

---

## 5. Topbar Redesign

### Live UTC clock
- Positioned left side, after mobile menu button and before page title
- Format: `HH:MM:SS UTC` in JetBrains Mono, `text-[12px]`, `text-cyan-400/70`
- Updates every second via `setInterval` in a `useEffect`, cleared on unmount
- Hidden on mobile (`hidden sm:block`)

### Theme toggle button
- Sun/moon icon, `w-8 h-8` rounded button
- Sits in right cluster between mode badge and connection indicator
- Calls `next-themes` `setTheme` on click — instant CSS variable swap
- Icon: `Sun` when dark mode active, `Moon` when light mode active
- 90° rotate CSS transition on switch (`transition-transform duration-150`)
- Visible on all breakpoints

### Right cluster (refined)
- Consistent pill shape across all items: kill-switch warning, mode badge, connection indicator, theme toggle
- Kill switch: deeper red glow in dark mode (`shadow-red-500/30`), high contrast in light mode
- Connected state: adopts cyan/teal (not emerald) — `text-cyan-400 border-cyan-500/20 bg-cyan-500/5`
- Offline state: red — unchanged

### Live data left border
- Page title area gets a `border-l-2 border-cyan-500/60 pl-3` treatment when on a live-data section (dashboard, positions, orders)
- Static pages (settings, audit, journal) get no border — visual distinction between live and static views

---

## 6. Terminal Card & Data Surface Upgrade

### TerminalCard primitive (replaces GlassStat)
- Flat dark surface, no gradient glow fill
- Left border accent: `border-l-2` colour varies by semantic:
  - Cyan (`border-cyan-500/60`): neutral metrics
  - Teal (`border-teal-500/60`): positive/profit metrics
  - Red (`border-red-500/60`): loss/risk metrics
- Padding: `p-3` (tighter than current `p-4`)
- Stat value: JetBrains Mono, `text-xl font-semibold tabular-nums`
- Label: `text-[10px] uppercase tracking-[0.1em] text-muted-foreground font-semibold`
- Hover: left accent border brightens, `box-shadow: 0 0 0 1px hsl(192 85% 48% / 0.2)` — no movement
- Live pulse dot retained for real-time metrics

### Badge primitives (updated)
- `badge-live`: red — unchanged (trading safety signal)
- `badge-demo`: cyan (`bg-cyan-500/10 text-cyan-400 border-cyan-500/25`)
- `badge-mock`: teal/muted (`bg-teal-500/10 text-teal-400 border-teal-500/25`)

### Table primitives (updated)
- Row hover: `hover:bg-cyan-500/4` instead of generic accent
- Table header: small-caps, `tracking-[0.1em]`, `text-muted-foreground` — matches nav section label register

### Custom SVG shield logo
- Used in: sidebar brand mark
- Same SVG exported as `public/icon-192.png` reference (favicon not changed in this pass)

---

## 7. State & Settings

### `stores/ui-store.ts` (new)
```typescript
interface UIStore {
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  setSidebarCollapsed: (v: boolean) => void
}
```
- Zustand store with `persist` middleware → `localStorage` key `ui-prefs`
- No theme state (owned by `next-themes`)

### Settings page — Appearance section
New section added above existing settings sections:

| Control | Behaviour |
|---|---|
| Dark mode toggle | Calls `next-themes` `setTheme('dark' \| 'light')` |
| System default toggle | Calls `next-themes` `setTheme('system')` — topbar button becomes read-only indicator |

### `providers.tsx` change
```tsx
import { ThemeProvider } from 'next-themes'

// Wrap existing providers:
<ThemeProvider attribute="class" defaultTheme="dark" enableSystem disableTransitionOnChange>
  {/* existing providers */}
</ThemeProvider>
```

---

## 8. Design System CSS Layer

`globals.css` gains a clearly demarcated `/* ── DESIGN SYSTEM ── */` section structured as:

1. **Surfaces & elevation** — `.surface-0/1/2`, `.panel`, `.elev-*`
2. **Typography scale** — `.stat-label`, `.stat-value`, `.stat-sub`, `.section-title`, `.mono-value`
3. **Navigation primitives** — `.nav-link`, `.nav-link-active`, `.nav-section-header`, `.nav-badge`
4. **Card primitives** — `.terminal-card`, `.terminal-card-cyan`, `.terminal-card-teal`, `.terminal-card-red`
5. **Badge primitives** — `.badge-base`, `.badge-live`, `.badge-demo`, `.badge-mock`
6. **Form controls** — `.input`, `.btn-primary`, `.btn-ghost`, `.switch`

Every component class uses CSS variable tokens only — no hardcoded colours. A future palette change is a `globals.css` variable edit, not a component hunt.

---

## 9. What Does Not Change

- All business logic, API hooks, WebSocket handlers
- All Celery tasks, backend endpoints, database schema
- Page routing and URL structure
- `CASH_ONLY_MODE` / `LIVE_TRADING_ENABLED` flags
- CI pipeline (no new test requirements beyond existing coverage gate)
- Playwright E2E tests (layout selectors may need minor updates for new class names)

---

## 10. Success Criteria

- Dark and light modes both look intentional and premium — same brand, flipped palette
- Theme toggle works instantly with no flash on page load
- Sidebar collapse persists across page refreshes
- Live badges on Alerts, Orders, Positions show correct counts
- All existing TypeScript strict-mode checks pass
- All existing unit tests pass (127 Python, 55 frontend)
- Playwright E2E global-setup and smoke tests pass
