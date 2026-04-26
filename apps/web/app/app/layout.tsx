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
