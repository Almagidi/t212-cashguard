'use client'

import { OperatorDashboard } from '@/components/operator/operator-dashboard'
import { useOperatorStatus } from '@/hooks/use-api'

export default function OperatorPage() {
  const { data, isLoading, isError } = useOperatorStatus()

  return (
    <OperatorDashboard
      status={data}
      isLoading={isLoading}
      isError={isError}
    />
  )
}
