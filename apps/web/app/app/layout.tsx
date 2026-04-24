import { AuthGate } from '@/components/layout/auth-gate'
import { Sidebar } from '@/components/layout/sidebar'
import { TopBar } from '@/components/layout/topbar'
import { StatusBar } from '@/components/layout/status-bar'
import { ErrorBoundary } from '@/components/shared/error-boundary'

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <div className="min-h-screen bg-background">
        {/* Desktop sidebar — hidden on mobile, visible md+ */}
        <Sidebar />
        {/* TopBar includes the mobile hamburger + mobile drawer */}
        <TopBar />
        {/* Main content — full width on mobile, offset by sidebar on md+ */}
        <main className="md:ml-56 pt-14 pb-7 min-h-screen relative">
          <div className="p-5 md:p-7 max-w-[1600px] mx-auto animate-fade-in">
            <ErrorBoundary label="Page">
              {children}
            </ErrorBoundary>
          </div>
        </main>
        <StatusBar />
      </div>
    </AuthGate>
  )
}
