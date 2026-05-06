'use client'

import { OperatorDashboard } from '@/components/operator/operator-dashboard'
import { RuntimeDiagnostics } from '@/components/operator/runtime-diagnostics'
import { useOperatorStatus } from '@/hooks/use-api'

export default function OperatorPage() {
  const { data, isLoading, isError } = useOperatorStatus()

  return (
    <div className="space-y-5">
      <RuntimeDiagnostics />
      <OperatorDashboard
        status={data}
        isLoading={isLoading}
        isError={isError}
      />
    </div>
  )
}
