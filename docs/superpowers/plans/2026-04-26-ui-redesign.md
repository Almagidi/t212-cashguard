# UI Redesign — Full Visual Identity Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current blue-indigo palette with a cyan/teal fintech identity, overhaul the sidebar with brand mark, gradient active states, live badges, and collapse rail, add a dark/light mode toggle to topbar and settings, and upgrade stat cards to a terminal-card style.

**Architecture:** All changes are contained to the frontend (`apps/web`). The Zustand `ui-store` owns sidebar collapsed state (persisted to localStorage). `next-themes` (already installed and wired) owns theme state. A new `MainContent` client wrapper allows the app layout to respond to sidebar collapse without making the Server Component layout a client component.

**Tech Stack:** Next.js 14, Tailwind CSS, Zustand 5 (persist middleware), next-themes 0.4.4, React Query, lucide-react, TypeScript strict mode.

**Working directory for all commands:** `apps/web`

---

## File Map

| Status | File | Role |
|---|---|---|
| Modify | `app/providers.tsx` | Add `enableSystem`, `disableTransitionOnChange` to ThemeProvider |
| Create | `stores/ui-store.ts` | Sidebar collapse state (localStorage-persisted) |
| Create | `tests/unit/stores/ui-store.test.ts` | Unit tests for ui-store |
| Modify | `tailwind.config.js` | Ensure teal/cyan tokens available, add sidebar width vars |
| Modify | `styles/globals.css` | Full palette swap + design-system component layer |
| Create | `components/layout/shield-logo.tsx` | Inline SVG shield brand mark |
| Create | `components/layout/main-content.tsx` | Client wrapper: dynamic sidebar margin |
| Modify | `components/layout/sidebar.tsx` | Full rebuild: brand, gradient pill, grouping, badges, collapse |
| Modify | `components/layout/topbar.tsx` | UTC clock, theme toggle, live-data border, refined cluster |
| Modify | `app/app/layout.tsx` | Swap `<main>` for `<MainContent>` |
| Create | `components/ui/terminal-card.tsx` | TerminalCard primitive (replaces GlassStat) |
| Create | `tests/unit/components/terminal-card.test.tsx` | Unit tests for TerminalCard |
| Modify | `components/ui/index.tsx` | Export TerminalCard |
| Modify | `app/app/dashboard/page.tsx` | Replace GlassStat with TerminalCard |
| Modify | `app/app/settings/page.tsx` | Add Appearance card (next-themes toggle) |

---

## Task 1: Update ThemeProvider config in providers.tsx

`next-themes` is already installed (0.4.4) and ThemeProvider is already wired. This task enables system-theme support and prevents colour-flash on theme switch.

**Files:**
- Modify: `app/providers.tsx`

- [ ] **Step 1: Change ThemeProvider props**

In `app/providers.tsx`, find the ThemeProvider line and replace:
```tsx
<ThemeProvider attribute="class" defaultTheme="dark" enableSystem={false}>
```
with:
```tsx
<ThemeProvider attribute="class" defaultTheme="dark" enableSystem disableTransitionOnChange>
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add app/providers.tsx
git commit -m "feat: enable system theme + disableTransitionOnChange in ThemeProvider"
```

---

## Task 2: Create ui-store with tests

Zustand store for sidebar collapsed state, persisted to localStorage.

**Files:**
- Create: `stores/ui-store.ts`
- Create: `tests/unit/stores/ui-store.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/stores/ui-store.test.ts`:

```typescript
import { beforeEach, describe, expect, it } from '@jest/globals'

// Reset module between tests so store state doesn't leak
beforeEach(() => {
  jest.resetModules()
  localStorage.clear()
})

describe('useUIStore', () => {
  it('has sidebarCollapsed = false by default', async () => {
    const { useUIStore } = await import('@/stores/ui-store')
    const state = useUIStore.getState()
    expect(state.sidebarCollapsed).toBe(false)
  })

  it('toggleSidebar flips sidebarCollapsed', async () => {
    const { useUIStore } = await import('@/stores/ui-store')
    useUIStore.getState().toggleSidebar()
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    useUIStore.getState().toggleSidebar()
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })

  it('setSidebarCollapsed sets an explicit value', async () => {
    const { useUIStore } = await import('@/stores/ui-store')
    useUIStore.getState().setSidebarCollapsed(true)
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    useUIStore.getState().setSidebarCollapsed(false)
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
npx jest tests/unit/stores/ui-store.test.ts --no-coverage
```
Expected: FAIL — "Cannot find module '@/stores/ui-store'"

- [ ] **Step 3: Create the store**

Create `stores/ui-store.ts`:

```typescript
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface UIStore {
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  setSidebarCollapsed: (v: boolean) => void
}

export const useUIStore = create<UIStore>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarCollapsed: (v) => set({ sidebarCollapsed: v }),
    }),
    { name: 'ui-prefs' },
  ),
)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
npx jest tests/unit/stores/ui-store.test.ts --no-coverage
```
Expected: PASS — 3 tests passing.

- [ ] **Step 5: Commit**

```bash
git add stores/ui-store.ts tests/unit/stores/ui-store.test.ts
git commit -m "feat: add ui-store with sidebar collapse state (localStorage-persisted)"
```

---

## Task 3: Overhaul styles/globals.css

Replace the current blue-indigo palette with the cyan/teal fintech palette and formalise the design system component layer. This is the largest single-file change — it touches every visual token.

**Files:**
- Modify: `styles/globals.css`

- [ ] **Step 1: Replace the CSS variable blocks**

In `styles/globals.css`, replace the entire `@layer base` block (the `:root` and `.dark` sections) with:

```css
@layer base {
  :root {
    /* Light mode — cool pale surfaces, cyan primary */
    --background: 210 20% 96%;
    --foreground: 220 20% 10%;
    --card: 0 0% 100%;
    --card-foreground: 220 20% 10%;
    --border: 210 14% 88%;
    --input: 210 14% 88%;
    --ring: 192 85% 40%;
    --primary: 192 85% 40%;
    --primary-foreground: 0 0% 100%;
    --secondary: 210 14% 94%;
    --secondary-foreground: 220 20% 20%;
    --muted: 210 14% 94%;
    --muted-foreground: 215 12% 45%;
    --accent: 210 14% 92%;
    --accent-foreground: 220 20% 10%;
    --destructive: 0 84% 55%;
    --destructive-foreground: 0 0% 100%;
    --radius: 0.625rem;

    --surface-0: 210 20% 96%;
    --surface-1: 0 0% 100%;
    --surface-2: 210 18% 98%;
    --elev-1: 0 1px 2px 0 rgb(0 0 0 / 0.06);
    --elev-2: 0 4px 16px -4px rgb(0 0 0 / 0.1);
    --elev-3: 0 12px 32px -8px rgb(0 0 0 / 0.14);
  }

  .dark {
    /* Dark mode — deep cool charcoal, cyan primary */
    --background: 220 20% 7%;
    --foreground: 210 20% 92%;
    --card: 220 18% 10%;
    --card-foreground: 210 20% 92%;
    --border: 220 14% 16%;
    --input: 220 14% 16%;
    --ring: 192 85% 48%;
    --primary: 192 85% 48%;
    --primary-foreground: 220 20% 7%;
    --secondary: 220 14% 14%;
    --secondary-foreground: 210 18% 82%;
    --muted: 220 14% 14%;
    --muted-foreground: 215 15% 52%;
    --accent: 220 14% 16%;
    --accent-foreground: 210 20% 92%;
    --destructive: 0 75% 58%;
    --destructive-foreground: 0 0% 100%;

    --surface-0: 220 20% 7%;
    --surface-1: 220 18% 10%;
    --surface-2: 220 16% 13%;
    --elev-1: 0 1px 3px 0 rgb(0 0 0 / 0.5);
    --elev-2: 0 6px 24px -6px rgb(0 0 0 / 0.6);
    --elev-3: 0 16px 48px -12px rgb(0 0 0 / 0.75);
  }
}
```

- [ ] **Step 2: Replace the COMPONENTS layer**

Find the entire `@layer components { ... }` block and replace it with the following design system layer. Keep everything BEFORE `@layer components` (base elements, scrollbar styles, etc.) and everything AFTER it (utilities) untouched.

```css
/* ── DESIGN SYSTEM ────────────────────────────────────────────────────────── */

@layer components {

  /* ── 1. Surfaces & Elevation ────────────────────────────────────────────── */

  .surface-1 { background-color: hsl(var(--surface-1)); }
  .surface-2 { background-color: hsl(var(--surface-2)); }

  .panel {
    @apply bg-card border border-border rounded-xl;
    box-shadow: var(--elev-1);
  }

  /* ── 2. Typography Scale ─────────────────────────────────────────────────── */

  .mono-value {
    @apply font-mono text-xl font-semibold tabular-nums tracking-tight text-foreground;
  }

  .stat-label {
    @apply text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground;
  }

  .stat-sub {
    @apply text-[11px] text-muted-foreground/70 leading-tight;
  }

  /* stat-value kept for backwards compat with non-terminal pages */
  .stat-value {
    @apply text-2xl font-semibold tracking-tight tabular-nums text-foreground;
  }

  /* ── 3. Navigation Primitives ────────────────────────────────────────────── */

  .nav-section-header {
    @apply text-[10px] font-semibold uppercase tracking-[0.1em];
    color: hsl(var(--primary) / 0.55);
  }

  .nav-badge {
    @apply inline-flex items-center justify-center min-w-[18px] h-[18px] px-1
           rounded-full text-[10px] font-bold leading-none;
  }

  .nav-link {
    @apply flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px]
           text-muted-foreground font-medium transition-colors duration-100
           hover:text-foreground relative;
  }

  .nav-link:hover {
    background: linear-gradient(to right, hsl(var(--primary) / 0.06), transparent);
  }

  .nav-link-active {
    @apply text-foreground;
    background: linear-gradient(to right, hsl(var(--primary) / 0.12), transparent);
  }

  .nav-link-active::before {
    content: '';
    @apply absolute left-0 top-[20%] bottom-[20%] w-[3px] rounded-r-full;
    background-color: hsl(var(--primary));
  }

  /* ── 4. Card Primitives ──────────────────────────────────────────────────── */

  .terminal-card {
    @apply bg-card border border-border rounded-xl p-3 border-l-2 transition-shadow duration-150;
    box-shadow: var(--elev-1);
  }

  .terminal-card-cyan {
    border-left-color: hsl(192 85% 48% / 0.6);
  }

  .terminal-card-teal {
    border-left-color: hsl(172 70% 42% / 0.6);
  }

  .terminal-card-red {
    border-left-color: hsl(0 75% 58% / 0.6);
  }

  .terminal-card:hover {
    box-shadow: var(--elev-1), 0 0 0 1px hsl(var(--primary) / 0.2);
  }

  /* stat-card kept for pages not yet migrated to terminal-card */
  .stat-card {
    @apply bg-card border border-border rounded-xl p-4 flex flex-col gap-1.5;
    box-shadow: var(--elev-1);
    transition: border-color .15s ease;
  }

  /* ── 5. Badge Primitives ─────────────────────────────────────────────────── */

  .badge-base {
    @apply inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded-full
           uppercase tracking-[0.09em] border;
  }

  .badge-live {
    @apply badge-base bg-red-500/10 text-red-400 border-red-500/25;
  }
  .badge-live::before {
    content: '';
    @apply w-1.5 h-1.5 rounded-full bg-red-400 animate-pulse-slow inline-block;
  }

  .badge-demo {
    @apply badge-base bg-cyan-500/10 text-cyan-400 border-cyan-500/25;
  }
  .badge-demo::before {
    content: '';
    @apply w-1.5 h-1.5 rounded-full bg-cyan-400 inline-block;
  }

  .badge-mock {
    @apply badge-base bg-teal-500/10 text-teal-400 border-teal-500/25;
  }
  .badge-mock::before {
    content: '';
    @apply w-1.5 h-1.5 rounded-full bg-teal-400 inline-block;
  }

  /* ── 6. P&L Colours ──────────────────────────────────────────────────────── */

  .pnl-positive { @apply text-emerald-400 font-medium tabular-nums; }
  .pnl-negative { @apply text-red-400 font-medium tabular-nums; }
  .pnl-neutral  { @apply text-muted-foreground font-medium tabular-nums; }

  /* ── 7. Table Primitives ─────────────────────────────────────────────────── */

  .table-header-cell {
    @apply text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground
           px-4 py-2 text-left;
  }

  .table-row-hover {
    @apply transition-colors duration-100;
  }
  .table-row-hover:hover {
    background-color: hsl(var(--primary) / 0.04);
  }

  /* ── 8. Form Controls ────────────────────────────────────────────────────── */

  .kv-row {
    @apply flex items-center justify-between py-2.5 border-b border-border/50 last:border-0 text-[13px];
  }

  /* ── 9. Status / Section Labels ──────────────────────────────────────────── */

  .section-title {
    @apply text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground;
  }

}
```

- [ ] **Step 3: Verify the build compiles (Tailwind processes correctly)**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add styles/globals.css
git commit -m "feat: overhaul design tokens to cyan/teal palette and formalise design system layer"
```

---

## Task 4: Create ShieldLogo component

Inline SVG brand mark — a geometric shield with a double-chevron interior.

**Files:**
- Create: `components/layout/shield-logo.tsx`

- [ ] **Step 1: Create the component**

```tsx
import { cn } from '@/lib/utils'

interface ShieldLogoProps {
  className?: string
}

export function ShieldLogo({ className }: ShieldLogoProps) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={cn('text-primary', className)}
      aria-hidden="true"
    >
      <path
        d="M16 3L5 7.5V16c0 6.627 4.477 12.5 11 14.5C22.523 28.5 27 22.627 27 16V7.5L16 3z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        fill="currentColor"
        fillOpacity="0.1"
      />
      <path
        d="M11 12l3.5 4-3.5 4"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M17 12l3.5 4-3.5 4"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity="0.6"
      />
    </svg>
  )
}
```

- [ ] **Step 2: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add components/layout/shield-logo.tsx
git commit -m "feat: add ShieldLogo SVG brand mark component"
```

---

## Task 5: Create MainContent client wrapper

The app layout is a Server Component so it can't read Zustand state. This client wrapper manages the dynamic left-margin for the collapsible sidebar.

**Files:**
- Create: `components/layout/main-content.tsx`

- [ ] **Step 1: Create the component**

```tsx
'use client'
import { cn } from '@/lib/utils'
import { useUIStore } from '@/stores/ui-store'

export function MainContent({ children }: { children: React.ReactNode }) {
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)

  return (
    <main
      className={cn(
        'pt-14 pb-7 min-h-screen relative transition-[margin-left] duration-200',
        sidebarCollapsed ? 'md:ml-16' : 'md:ml-56',
      )}
    >
      {children}
    </main>
  )
}
```

- [ ] **Step 2: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add components/layout/main-content.tsx
git commit -m "feat: add MainContent client wrapper for dynamic sidebar offset"
```

---

## Task 6: Update app/app/layout.tsx

Swap the hardcoded `<main>` for the new `MainContent` client wrapper.

**Files:**
- Modify: `app/app/layout.tsx`

- [ ] **Step 1: Update the layout**

Replace the full contents of `app/app/layout.tsx` with:

```tsx
import { AuthGate } from '@/components/layout/auth-gate'
import { MainContent } from '@/components/layout/main-content'
import { Sidebar } from '@/components/layout/sidebar'
import { StatusBar } from '@/components/layout/status-bar'
import { TopBar } from '@/components/layout/topbar'
import { ErrorBoundary } from '@/components/shared/error-boundary'

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <div className="min-h-screen bg-background">
        <Sidebar />
        <TopBar />
        <MainContent>
          <div className="p-5 md:p-7 max-w-[1600px] mx-auto animate-fade-in">
            <ErrorBoundary label="Page">
              {children}
            </ErrorBoundary>
          </div>
        </MainContent>
        <StatusBar />
      </div>
    </AuthGate>
  )
}
```

- [ ] **Step 2: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add app/app/layout.tsx
git commit -m "feat: swap main element for dynamic MainContent wrapper"
```

---

## Task 7: Rebuild sidebar.tsx

Full rebuild: new brand mark, four nav groups, gradient active pill, live count badges, collapse toggle.

**Files:**
- Modify: `components/layout/sidebar.tsx`

- [ ] **Step 1: Replace the full file**

Replace the entire contents of `components/layout/sidebar.tsx` with:

```tsx
'use client'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import {
  LayoutDashboard, Building2, LineChart, ListOrdered, Briefcase,
  ShieldAlert, Bell, FileBarChart, Settings, AlertOctagon,
  ScrollText, Activity, LogOut, FlaskConical, BookOpen,
  Menu, X, ChevronLeft, ChevronRight,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/auth'
import { useSettings, useOrders, usePositions, useAlerts } from '@/hooks/use-api'
import { useUIStore } from '@/stores/ui-store'
import { ShieldLogo } from './shield-logo'
import api from '@/services/api'
import { useState, useEffect } from 'react'

// ── Nav groups ────────────────────────────────────────────────────────────────

const NAV_GROUPS = [
  {
    label: 'Trading',
    items: [
      { href: '/app/dashboard',   icon: LayoutDashboard, label: 'Dashboard' },
      { href: '/app/broker',      icon: Building2,       label: 'Broker' },
      { href: '/app/instruments', icon: Activity,        label: 'Instruments' },
      { href: '/app/strategies',  icon: LineChart,       label: 'Strategies' },
    ],
  },
  {
    label: 'Operations',
    items: [
      { href: '/app/orders',    icon: ListOrdered, label: 'Orders',        badge: 'orders' as const },
      { href: '/app/positions', icon: Briefcase,   label: 'Positions',     badge: 'positions' as const },
      { href: '/app/risk',      icon: ShieldAlert, label: 'Risk Controls' },
      { href: '/app/backtest',  icon: FlaskConical, label: 'Backtest' },
    ],
  },
  {
    label: 'Monitoring',
    items: [
      { href: '/app/alerts',  icon: Bell,         label: 'Alerts',        badge: 'alerts' as const },
      { href: '/app/reports', icon: FileBarChart,  label: 'Reports' },
      { href: '/app/journal', icon: BookOpen,      label: 'Trade Journal' },
      { href: '/app/audit',   icon: ScrollText,    label: 'Audit Log' },
    ],
  },
] as const

const SYSTEM_ITEMS = [
  { href: '/app/settings',  icon: Settings,      label: 'Settings' },
  { href: '/app/emergency', icon: AlertOctagon,  label: 'Emergency', danger: true },
]

// ── Badge counts hook ─────────────────────────────────────────────────────────

function useNavBadges() {
  const { data: orders }    = useOrders()
  const { data: positions } = usePositions()
  const { data: alerts }    = useAlerts({ is_read: false, limit: 99 })
  return {
    orders:    orders?.length    ?? 0,
    positions: positions?.length ?? 0,
    alerts:    alerts?.length    ?? 0,
  }
}

// ── Nav link ──────────────────────────────────────────────────────────────────

function NavLink({
  href, icon: Icon, label, active, danger, badgeCount, collapsed, onClick,
}: {
  href: string
  icon: React.ElementType
  label: string
  active: boolean
  danger?: boolean
  badgeCount?: number
  collapsed: boolean
  onClick?: () => void
}) {
  return (
    <Link
      href={href}
      onClick={onClick}
      title={collapsed ? label : undefined}
      className={cn(
        'nav-link',
        danger && !active && 'text-red-400/80 hover:!text-red-300',
        danger && active  && 'text-red-400 !bg-red-500/10',
        !danger && active && 'nav-link-active text-primary',
        collapsed && 'justify-center px-0',
      )}
    >
      <Icon
        className={cn(
          'flex-shrink-0',
          collapsed ? 'w-5 h-5' : 'w-[15px] h-[15px]',
          active && !danger && 'text-primary',
          danger && 'text-current',
        )}
      />
      {!collapsed && <span className="flex-1 truncate">{label}</span>}
      {!collapsed && badgeCount !== undefined && badgeCount > 0 && (
        <span
          className={cn(
            'nav-badge',
            href.includes('alerts')
              ? 'bg-primary text-primary-foreground'
              : 'bg-muted text-muted-foreground',
          )}
        >
          {badgeCount > 99 ? '99+' : badgeCount}
        </span>
      )}
    </Link>
  )
}

// ── Sidebar content ───────────────────────────────────────────────────────────

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const pathname   = usePathname()
  const router     = useRouter()
  const { logout } = useAuthStore()
  const { data: settings } = useSettings()
  const { sidebarCollapsed, toggleSidebar } = useUIStore()
  const badges     = useNavBadges()

  const getBadge = (key?: 'orders' | 'positions' | 'alerts') =>
    key ? badges[key] : undefined

  const handleLogout = async () => {
    await api.logout()
    logout()
    router.push('/auth/login')
    onNavigate?.()
  }

  return (
    <>
      {/* ── Logo ─────────────────────────────────────────────────────────── */}
      <div
        className={cn(
          'h-14 border-b border-border flex items-center flex-shrink-0',
          sidebarCollapsed ? 'justify-center px-0' : 'px-4 gap-3',
        )}
      >
        <ShieldLogo className="w-8 h-8 flex-shrink-0" />
        {!sidebarCollapsed && (
          <div className="min-w-0">
            <p className="text-[13px] font-semibold leading-tight tracking-tight">CashGuard</p>
            <p className="text-[10px] leading-tight mt-0.5 font-medium" style={{ color: 'hsl(var(--primary) / 0.6)' }}>
              Trading 212
            </p>
          </div>
        )}
      </div>

      {/* ── Kill switch warning ───────────────────────────────────────────── */}
      {settings?.kill_switch_active && !sidebarCollapsed && (
        <div className="mx-3 mt-3 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-[11px] text-red-400 flex items-center gap-2 flex-shrink-0 font-medium">
          <AlertOctagon className="w-3.5 h-3.5 flex-shrink-0 animate-pulse-slow" />
          Kill Switch Active
        </div>
      )}

      {/* ── Nav groups ───────────────────────────────────────────────────── */}
      <nav className="flex-1 overflow-y-auto py-3 min-h-0 scrollbar-none space-y-4"
           style={{ padding: sidebarCollapsed ? '12px 8px' : '12px' }}>
        {NAV_GROUPS.map((group) => (
          <div key={group.label}>
            {!sidebarCollapsed && (
              <p className="nav-section-header px-3 mb-1">{group.label}</p>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <NavLink
                  key={item.href}
                  href={item.href}
                  icon={item.icon}
                  label={item.label}
                  active={pathname.startsWith(item.href)}
                  badgeCount={getBadge((item as { badge?: 'orders' | 'positions' | 'alerts' }).badge)}
                  collapsed={sidebarCollapsed}
                  onClick={onNavigate}
                />
              ))}
            </div>
          </div>
        ))}

        {/* ── System group ───────────────────────────────────────────────── */}
        <div className="pt-2 border-t border-border/50">
          {!sidebarCollapsed && (
            <p className="nav-section-header px-3 mb-1">System</p>
          )}
          <div className="space-y-0.5">
            {SYSTEM_ITEMS.map((item) => (
              <NavLink
                key={item.href}
                href={item.href}
                icon={item.icon}
                label={item.label}
                active={pathname.startsWith(item.href)}
                danger={item.danger}
                collapsed={sidebarCollapsed}
                onClick={onNavigate}
              />
            ))}
          </div>
        </div>
      </nav>

      {/* ── Footer: collapse toggle + logout ─────────────────────────────── */}
      <div className="p-3 border-t border-border flex-shrink-0 space-y-1">
        <button
          onClick={toggleSidebar}
          title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className={cn('nav-link w-full', sidebarCollapsed && 'justify-center px-0')}
        >
          {sidebarCollapsed
            ? <ChevronRight className="w-[15px] h-[15px]" />
            : <><ChevronLeft className="w-[15px] h-[15px]" /><span className="truncate">Collapse</span></>
          }
        </button>
        <button
          onClick={handleLogout}
          title={sidebarCollapsed ? 'Logout' : undefined}
          className={cn('nav-link w-full text-left', sidebarCollapsed && 'justify-center px-0')}
        >
          <LogOut className="w-[15px] h-[15px]" />
          {!sidebarCollapsed && <span className="truncate">Logout</span>}
        </button>
      </div>
    </>
  )
}

// ── Desktop sidebar ───────────────────────────────────────────────────────────

export function Sidebar() {
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)

  return (
    <aside
      className={cn(
        'fixed left-0 top-0 h-full surface-1 border-r border-border flex-col z-30 hidden md:flex transition-[width] duration-200',
        sidebarCollapsed ? 'w-16' : 'w-56',
      )}
    >
      <SidebarContent />
    </aside>
  )
}

// ── Mobile hamburger button ───────────────────────────────────────────────────

export function MobileMenuButton({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      onClick={onToggle}
      className="md:hidden flex items-center justify-center w-9 h-9 rounded-lg hover:bg-accent transition-colors"
      aria-label="Toggle menu"
    >
      {open ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
    </button>
  )
}

// ── Mobile drawer ─────────────────────────────────────────────────────────────

export function MobileDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  useEffect(() => {
    document.body.style.overflow = open ? 'hidden' : ''
    return () => { document.body.style.overflow = '' }
  }, [open])

  if (!open) return null

  return (
    <>
      <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden" onClick={onClose} />
      <aside className="fixed left-0 top-0 h-full w-64 bg-card border-r border-border flex flex-col z-50 md:hidden animate-slide-in">
        <SidebarContent onNavigate={onClose} />
      </aside>
    </>
  )
}
```

- [ ] **Step 2: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Run existing tests**

```bash
npx jest --no-coverage
```
Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add components/layout/sidebar.tsx
git commit -m "feat: rebuild sidebar with shield logo, grouped nav, gradient pill, live badges, collapse rail"
```

---

## Task 8: Rebuild topbar.tsx

Add UTC clock, theme toggle, live-data page title border, and refined right cluster.

**Files:**
- Modify: `components/layout/topbar.tsx`

- [ ] **Step 1: Replace the full file**

Replace the entire contents of `components/layout/topbar.tsx` with:

```tsx
'use client'
import { AlertOctagon, Wifi, WifiOff, Sun, Moon } from 'lucide-react'
import { usePathname } from 'next/navigation'
import { useTheme } from 'next-themes'
import { cn } from '@/lib/utils'
import { useSettings, useHealth } from '@/hooks/use-api'
import { useUIStore } from '@/stores/ui-store'
import { MobileMenuButton, MobileDrawer } from './sidebar'
import { useState, useEffect } from 'react'

const PAGE_META: Record<string, { title: string; subtitle?: string; live?: boolean }> = {
  '/app/dashboard':   { title: 'Dashboard',          subtitle: 'Realtime portfolio overview',         live: true },
  '/app/broker':      { title: 'Broker Account',     subtitle: 'Trading 212 connection and account' },
  '/app/instruments': { title: 'Instruments',        subtitle: 'Tradable symbols and market data' },
  '/app/strategies':  { title: 'Strategies',         subtitle: 'Automation rules and signal pipelines' },
  '/app/orders':      { title: 'Orders',             subtitle: 'Open, filled, and cancelled orders',  live: true },
  '/app/positions':   { title: 'Positions',          subtitle: 'Current holdings and unrealised P&L', live: true },
  '/app/risk':        { title: 'Risk Controls',      subtitle: 'Limits, guards, and circuit breakers' },
  '/app/alerts':      { title: 'Alerts',             subtitle: 'Realtime notifications and routing',  live: true },
  '/app/reports':     { title: 'Reports',            subtitle: 'Performance, exposure, and P&L' },
  '/app/journal':     { title: 'Trade Journal',      subtitle: 'Trade log and post-trade review' },
  '/app/settings':    { title: 'Settings',           subtitle: 'Application preferences' },
  '/app/emergency':   { title: 'Emergency Controls', subtitle: 'Kill switch and rapid unwind' },
  '/app/audit':       { title: 'Audit Log',          subtitle: 'Immutable record of sensitive actions' },
  '/app/backtest':    { title: 'Backtest',           subtitle: 'Historical strategy simulation' },
}

function modeBadgeClass(mode: string) {
  switch (mode) {
    case 'live': return 'badge-live'
    case 'demo': return 'badge-demo'
    default:     return 'badge-mock'
  }
}

function UtcClock() {
  const [time, setTime] = useState('')

  useEffect(() => {
    const fmt = () => {
      const now = new Date()
      const hh = String(now.getUTCHours()).padStart(2, '0')
      const mm = String(now.getUTCMinutes()).padStart(2, '0')
      const ss = String(now.getUTCSeconds()).padStart(2, '0')
      setTime(`${hh}:${mm}:${ss}`)
    }
    fmt()
    const id = setInterval(fmt, 1000)
    return () => clearInterval(id)
  }, [])

  if (!time) return null

  return (
    <span className="hidden sm:block font-mono text-[12px] text-primary/70 tabular-nums tracking-tight select-none">
      {time} UTC
    </span>
  )
}

export function TopBar() {
  const pathname = usePathname()
  const { data: settings } = useSettings()
  const { data: health }   = useHealth()
  const { theme, setTheme } = useTheme()
  const sidebarCollapsed   = useUIStore((s) => s.sidebarCollapsed)
  const [drawerOpen, setDrawerOpen] = useState(false)

  const mode = (process.env.NEXT_PUBLIC_APP_MODE || health?.mode || 'mock') as string
  const meta = Object.entries(PAGE_META).find(([p]) => pathname.startsWith(p))?.[1] ?? { title: 'CashGuard' }

  return (
    <>
      <header
        className={cn(
          'fixed top-0 right-0 h-14 bg-background/80 backdrop-blur-xl border-b border-border',
          'flex items-center justify-between px-4 md:px-6 z-20 transition-[left] duration-200',
          sidebarCollapsed ? 'left-0 md:left-16' : 'left-0 md:left-56',
        )}
      >
        {/* Left: hamburger + UTC clock + page title */}
        <div className="flex items-center gap-3 min-w-0">
          <MobileMenuButton open={drawerOpen} onToggle={() => setDrawerOpen(v => !v)} />
          <UtcClock />
          <div
            className={cn(
              'min-w-0',
              meta.live && 'border-l-2 pl-3',
            )}
            style={meta.live ? { borderColor: 'hsl(var(--primary) / 0.6)' } : undefined}
          >
            <h1 className="text-[15px] font-semibold text-foreground tracking-tight leading-tight truncate">
              {meta.title}
            </h1>
            {meta.subtitle && (
              <p className="text-[11px] text-muted-foreground/80 leading-tight mt-0.5 hidden sm:block truncate">
                {meta.subtitle}
              </p>
            )}
          </div>
        </div>

        {/* Right: kill switch + mode badge + theme toggle + connection */}
        <div className="flex items-center gap-2">
          {settings?.kill_switch_active && (
            <span className="hidden sm:inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-1 rounded-full bg-red-500/10 border border-red-500/30 text-red-400 uppercase tracking-[0.08em] shadow-sm shadow-red-500/10">
              <AlertOctagon className="w-3 h-3 animate-pulse-slow" />
              Kill Switch
            </span>
          )}

          <span className={cn(modeBadgeClass(mode), 'hidden sm:inline-flex')}>
            {mode}
          </span>

          {/* Theme toggle */}
          <button
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            className="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-accent transition-colors"
            aria-label="Toggle theme"
          >
            {theme === 'dark'
              ? <Sun  className="w-4 h-4 text-muted-foreground hover:text-foreground transition-colors" />
              : <Moon className="w-4 h-4 text-muted-foreground hover:text-foreground transition-colors" />
            }
          </button>

          {/* Connection indicator */}
          <div
            className={cn(
              'flex items-center gap-1.5 text-[11px] font-medium px-2 py-1 rounded-md border',
              health?.status === 'ok'
                ? 'text-cyan-400 border-cyan-500/20 bg-cyan-500/5'
                : 'text-red-400 border-red-500/20 bg-red-500/5',
            )}
          >
            {health?.status === 'ok'
              ? <Wifi    className="w-3 h-3" />
              : <WifiOff className="w-3 h-3" />
            }
            <span className="hidden sm:inline">
              {health?.status === 'ok' ? 'Connected' : 'Offline'}
            </span>
          </div>
        </div>
      </header>

      <MobileDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  )
}
```

- [ ] **Step 2: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Run existing tests**

```bash
npx jest --no-coverage
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add components/layout/topbar.tsx
git commit -m "feat: rebuild topbar with UTC clock, theme toggle, live-data border, cyan connection state"
```

---

## Task 9: Create TerminalCard component with tests

The new card primitive that replaces `GlassStat` on the dashboard.

**Files:**
- Create: `components/ui/terminal-card.tsx`
- Create: `tests/unit/components/terminal-card.test.tsx`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/components/terminal-card.test.tsx`:

```tsx
import '@testing-library/jest-dom'
import { describe, expect, it } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import { TerminalCard } from '@/components/ui/terminal-card'

describe('TerminalCard', () => {
  it('renders label and value', () => {
    render(<TerminalCard label="P&L" value="£1,234.56" />)
    expect(screen.getByText('P&L')).toBeTruthy()
    expect(screen.getByText('£1,234.56')).toBeTruthy()
  })

  it('renders sub text when provided', () => {
    render(<TerminalCard label="Cash" value="£5,000" sub="Available balance" />)
    expect(screen.getByText('Available balance')).toBeTruthy()
  })

  it('applies cyan variant class by default', () => {
    const { container } = render(<TerminalCard label="X" value="Y" />)
    expect(container.firstChild).toHaveClass('terminal-card-cyan')
  })

  it('applies teal variant class when variant=teal', () => {
    const { container } = render(<TerminalCard label="X" value="Y" variant="teal" />)
    expect(container.firstChild).toHaveClass('terminal-card-teal')
  })

  it('applies red variant class when variant=red', () => {
    const { container } = render(<TerminalCard label="X" value="Y" variant="red" />)
    expect(container.firstChild).toHaveClass('terminal-card-red')
  })

  it('shows live pulse dot when live=true', () => {
    const { container } = render(<TerminalCard label="X" value="Y" live />)
    expect(container.querySelector('[aria-label="live"]')).toBeTruthy()
  })

  it('does not render pulse dot when live is not set', () => {
    const { container } = render(<TerminalCard label="X" value="Y" />)
    expect(container.querySelector('[aria-label="live"]')).toBeNull()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
npx jest tests/unit/components/terminal-card.test.tsx --no-coverage
```
Expected: FAIL — "Cannot find module '@/components/ui/terminal-card'"

- [ ] **Step 3: Create the component**

Create `components/ui/terminal-card.tsx`:

```tsx
import { cn } from '@/lib/utils'

interface TerminalCardProps {
  label: string
  value: React.ReactNode
  sub?: string
  variant?: 'cyan' | 'teal' | 'red'
  live?: boolean
  icon?: React.ReactNode
  className?: string
}

const variantDotClass = {
  cyan: 'bg-cyan-400',
  teal: 'bg-teal-400',
  red:  'bg-red-400',
}

export function TerminalCard({
  label,
  value,
  sub,
  variant = 'cyan',
  live,
  icon,
  className,
}: TerminalCardProps) {
  return (
    <div
      className={cn(
        'terminal-card',
        variant === 'cyan' && 'terminal-card-cyan',
        variant === 'teal' && 'terminal-card-teal',
        variant === 'red'  && 'terminal-card-red',
        'relative flex flex-col gap-1.5',
        className,
      )}
    >
      {live && (
        <span
          aria-label="live"
          className="absolute top-3 right-3 flex items-center justify-center"
        >
          <span className={cn('absolute w-2.5 h-2.5 rounded-full opacity-40 animate-ping', variantDotClass[variant])} />
          <span className={cn('relative w-1.5 h-1.5 rounded-full', variantDotClass[variant])} />
        </span>
      )}
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0 space-y-1.5">
          <p className="stat-label">{label}</p>
          <div className="mono-value truncate leading-tight">{value}</div>
          {sub && <p className="stat-sub">{sub}</p>}
        </div>
        {icon && <div className="flex-shrink-0 mt-0.5">{icon}</div>}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
npx jest tests/unit/components/terminal-card.test.tsx --no-coverage
```
Expected: PASS — 7 tests passing.

- [ ] **Step 5: Commit**

```bash
git add components/ui/terminal-card.tsx tests/unit/components/terminal-card.test.tsx
git commit -m "feat: add TerminalCard primitive with variant support and live pulse dot"
```

---

## Task 10: Export TerminalCard from ui/index.tsx

**Files:**
- Modify: `components/ui/index.tsx`

- [ ] **Step 1: Add the export**

At the top of `components/ui/index.tsx`, after the existing imports/exports block, add:

```tsx
export { TerminalCard } from './terminal-card'
export type { } from './terminal-card'
```

Actually, since `terminal-card.tsx` uses a named export (not default), just add this line anywhere near the other component exports in `components/ui/index.tsx`:

```tsx
export { TerminalCard } from './terminal-card'
```

- [ ] **Step 2: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add components/ui/index.tsx
git commit -m "feat: export TerminalCard from ui index"
```

---

## Task 11: Replace GlassStat with TerminalCard in dashboard

**Files:**
- Modify: `app/app/dashboard/page.tsx`

- [ ] **Step 1: Add TerminalCard to dashboard imports**

In `app/app/dashboard/page.tsx`, find the import line:
```tsx
import {
  Card, CardHeader, CardTitle, CardContent,
  Badge, Button, Spinner, EmptyState,
} from '@/components/ui'
```
Add `TerminalCard` to the import list:
```tsx
import {
  Card, CardHeader, CardTitle, CardContent,
  Badge, Button, Spinner, EmptyState, TerminalCard,
} from '@/components/ui'
```

- [ ] **Step 2: Replace the GlassStat component definition and all its usages**

Find and delete the entire `function GlassStat(...)` component definition and the `GlassStatProps` interface from the dashboard file (it will no longer be needed).

- [ ] **Step 3: Replace GlassStat calls with TerminalCard**

Find every usage of `<GlassStat` in the file and replace it with `<TerminalCard`. Apply this accent-to-variant mapping:

| Old prop | New prop |
|---|---|
| `accent="blue"` | `variant="cyan"` |
| `accent="emerald"` | `variant="teal"` |
| `accent="red"` | `variant="red"` |
| `accent="amber"` | `variant="cyan"` |
| `accent="purple"` | `variant="cyan"` |
| `trend="up"` | _(drop — not used by TerminalCard)_ |
| `trend="down"` | _(drop)_ |
| `trend="neutral"` | _(drop)_ |

All other props (`label`, `value`, `sub`, `live`, `icon`) carry over unchanged.

Example — before:
```tsx
<GlassStat
  label="Portfolio Value"
  value={formatCurrency(account?.portfolio_value)}
  sub="Total holdings"
  accent="blue"
  live
/>
```
After:
```tsx
<TerminalCard
  label="Portfolio Value"
  value={formatCurrency(account?.portfolio_value)}
  sub="Total holdings"
  variant="cyan"
  live
/>
```

- [ ] **Step 4: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 5: Run all frontend tests**

```bash
npx jest --no-coverage
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/app/dashboard/page.tsx
git commit -m "feat: replace GlassStat with TerminalCard on dashboard"
```

---

## Task 12: Add Appearance section to settings page

Adds a new card at the top of the settings page that controls the next-themes theme directly, independent of the existing backend preferences form.

**Files:**
- Modify: `app/app/settings/page.tsx`

- [ ] **Step 1: Add useTheme import**

In `app/app/settings/page.tsx`, add to the existing imports:
```tsx
import { useTheme } from 'next-themes'
```

Also add `Monitor` to the lucide-react imports:
```tsx
import { ..., Monitor } from 'lucide-react'
```

- [ ] **Step 2: Add useTheme hook call inside SettingsPage component**

Inside the `SettingsPage` function body, after the existing hooks, add:
```tsx
const { theme, setTheme } = useTheme()
```

- [ ] **Step 3: Add Appearance card above the Application card**

Insert the following JSX as the very first card inside the `<div className="max-w-2xl space-y-6">` block (before the existing "Application" Card):

```tsx
{/* Appearance */}
<Card>
  <CardHeader>
    <CardTitle>Appearance</CardTitle>
  </CardHeader>
  <CardContent className="space-y-1">
    {(
      [
        { label: 'Light',  value: 'light',  Icon: Sun  },
        { label: 'Dark',   value: 'dark',   Icon: Moon },
        { label: 'System', value: 'system', Icon: Monitor },
      ] as const
    ).map(({ label, value, Icon }) => (
      <button
        key={value}
        type="button"
        onClick={() => setTheme(value)}
        className={cn(
          'kv-row w-full text-left cursor-pointer rounded-lg px-2 transition-colors',
          theme === value
            ? 'text-primary font-medium'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <span className="flex items-center gap-2 text-[13px]">
          <Icon className="w-4 h-4" />
          {label}
        </span>
        {theme === value && (
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-primary">
            Active
          </span>
        )}
      </button>
    ))}
  </CardContent>
</Card>
```

- [ ] **Step 4: Verify TypeScript**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 5: Run all frontend tests**

```bash
npx jest --no-coverage
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/app/settings/page.tsx
git commit -m "feat: add Appearance card to settings with light/dark/system theme toggle"
```

---

## Task 13: Full test suite verification and coverage check

Final gate — ensure all changes pass the existing test suite and coverage threshold.

**Files:** None (verification only)

- [ ] **Step 1: Run the full frontend test suite with coverage**

```bash
npx jest --coverage
```
Expected: all tests pass, coverage at or above the existing threshold.

- [ ] **Step 2: Run TypeScript strict check**

```bash
npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Verify the app builds**

```bash
npx next build 2>&1 | tail -20
```
Expected: build completes with no errors (warnings about image sizes are acceptable).

- [ ] **Step 4: Final commit if any lint fixes were needed**

```bash
git add -p
git commit -m "fix: address any final lint/type issues from UI redesign"
```

---

## Spec Coverage Check

| Spec section | Covered by task(s) |
|---|---|
| Cyan/teal palette — dark + light | Task 3 |
| Theme persistence via next-themes | Task 1 |
| ui-store sidebar state | Task 2 |
| ThemeProvider enableSystem | Task 1 |
| ShieldLogo brand mark | Task 4 |
| MainContent dynamic offset | Task 5 |
| App layout using MainContent | Task 6 |
| Sidebar: brand mark | Task 7 |
| Sidebar: gradient active pill | Task 7 (`.nav-link-active` in Task 3) |
| Sidebar: four nav groups | Task 7 |
| Sidebar: live count badges | Task 7 |
| Sidebar: collapse rail (w-16/w-56) | Task 7 |
| Topbar: UTC clock | Task 8 |
| Topbar: theme toggle button | Task 8 |
| Topbar: live-data border on title | Task 8 |
| Topbar: dynamic left offset | Task 8 |
| Topbar: cyan connection state | Task 8 |
| TerminalCard primitive | Task 9 |
| TerminalCard exported from ui/index | Task 10 |
| Dashboard uses TerminalCard | Task 11 |
| Settings Appearance section | Task 12 |
| badge-demo = cyan, badge-mock = teal | Task 3 |
| Design system CSS layer | Task 3 |
| All tests pass | Task 13 |
