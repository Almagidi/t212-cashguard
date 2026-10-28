import Link from 'next/link'

/** Describes examples without asserting that different feeds share an account. */
export function AccountSourceNotice({ streamSource, hasSnapshot, accountMode }: {
  streamSource?: string
  hasSnapshot: boolean
  accountMode?: string
}) {
  const animation = streamSource === 'mock_market_animation'
  if (!animation && accountMode && accountMode !== 'mock') return null
  const title = animation ? 'Mock animation example' : hasSnapshot ? 'Stream source unverified' : accountMode === 'mock' ? 'Mock adapter example' : 'Account source unverified'

  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3 text-sm text-foreground" data-testid="account-source-notice">
      <p className="font-semibold">{title}</p>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
        {animation
          ? 'The streamed account, positions, signals and orders are illustrative and not reconciled with the adapter examples. This is not a paper ledger balance.'
          : hasSnapshot
            ? 'The stream has no verified source label. Do not treat its displayed balances as reconciled account or paper ledger values.'
            : accountMode === 'mock'
              ? 'Account cards use adapter fixtures, not a paper ledger balance. Position rows can also include separate paper holdings; these totals are not reconciled.'
              : 'Wait for an identified account source before relying on displayed balances.'}
      </p>
      {(animation || accountMode === 'mock') && (
        <Link href="/app/operator" className="mt-1 inline-block text-xs font-medium text-primary underline underline-offset-2 focus-ring rounded-sm">
          View separate paper execution history
        </Link>
      )}
    </div>
  )
}
