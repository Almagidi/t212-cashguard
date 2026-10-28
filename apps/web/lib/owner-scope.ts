import { useAuthStore } from '@/stores/auth'

// Tokens are compared only in memory; scopes must never be logged or persisted.
export function captureOwnerScope() {
  const state = useAuthStore.getState()
  return {
    owner: state.user?.id ?? null,
    revision: state.sessionRevision,
    verifiedToken: state.verifiedToken,
    token: typeof window === 'undefined' ? null : localStorage.getItem('cg_token'),
  }
}

export type OwnerScope = ReturnType<typeof captureOwnerScope>

export function isCurrentOwnerScope(scope: OwnerScope): boolean {
  const current = captureOwnerScope()
  return scope.owner === current.owner && scope.revision === current.revision && scope.verifiedToken === current.verifiedToken && scope.token === current.token
}

export class OwnerScopeChangedError extends Error {
  constructor() {
    super('Account session changed. Review the original account before retrying an uncertain action.')
    this.name = 'OwnerScopeChangedError'
  }
}

export function assertCurrentOwnerScope(scope: OwnerScope): void {
  if (!isCurrentOwnerScope(scope)) throw new OwnerScopeChangedError()
}

export function assertVerifiedOwnerScope(scope: OwnerScope): void {
  assertCurrentOwnerScope(scope)
  if (scope.token && (!scope.owner || scope.verifiedToken !== scope.token)) throw new OwnerScopeChangedError()
}

export function isVerifiedOwnerScope(scope: OwnerScope): boolean {
  return isCurrentOwnerScope(scope) && (!scope.token || (!!scope.owner && scope.verifiedToken === scope.token))
}
