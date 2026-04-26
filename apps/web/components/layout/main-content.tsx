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
