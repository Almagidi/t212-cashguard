/** A cached payload alone does not prove that the latest observation succeeded. */
export function hasCurrentQueryData(query: {
  data: unknown
  isPending?: boolean
  isLoading?: boolean
  isError?: boolean
  isFetching?: boolean
  isPlaceholderData?: boolean
  fetchStatus?: string
  failureCount?: number
  failureReason?: unknown
  dataUpdatedAt?: number
}): boolean {
  return query.data != null
    && !query.isPending && !query.isLoading && !query.isError && !query.isFetching
    && !query.isPlaceholderData && query.fetchStatus !== 'paused'
    && !query.failureCount && !query.failureReason
    && (query.dataUpdatedAt === undefined || query.dataUpdatedAt > 0)
}
