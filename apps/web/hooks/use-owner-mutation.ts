import { useState } from 'react'
import { useMutation, type DefaultError, type MutateOptions, type UseMutationOptions } from '@tanstack/react-query'
import { assertCurrentOwnerScope, assertVerifiedOwnerScope, captureOwnerScope, isVerifiedOwnerScope, type OwnerScope } from '@/lib/owner-scope'

function currentCallbacks<TData, TError, TVariables, TContext>(
  options: MutateOptions<TData, TError, TVariables, TContext> | undefined,
  scope: OwnerScope,
): MutateOptions<TData, TError, TVariables, TContext> | undefined {
  if (!options) return undefined
  return {
    onSuccess: (...args) => { if (isVerifiedOwnerScope(scope)) options.onSuccess?.(...args) },
    onError: (...args) => { if (isVerifiedOwnerScope(scope)) options.onError?.(...args) },
    onSettled: (...args) => { if (isVerifiedOwnerScope(scope)) options.onSettled?.(...args) },
  }
}

// An already-sent request may still commit for its original account. This guard
// prevents unsent work and old observations/callbacks from crossing a session.
export function useOwnerMutation<TData = unknown, TError = DefaultError, TVariables = void, TContext = unknown>(
  options: UseMutationOptions<TData, TError, TVariables, TContext>,
) {
  const [scope] = useState(captureOwnerScope)
  const result = useMutation<TData, TError, TVariables, TContext>({
    ...options,
    retry: false,
    networkMode: 'always',
    onMutate: async (...args) => {
      assertVerifiedOwnerScope(scope)
      const context = await options.onMutate?.(...args)
      assertCurrentOwnerScope(scope)
      return context as TContext
    },
    mutationFn: async (...args) => {
      assertVerifiedOwnerScope(scope)
      if (!options.mutationFn) throw new Error('A scoped mutation requires an explicit request function')
      try {
        const data = await options.mutationFn(...args)
        assertCurrentOwnerScope(scope)
        return data
      } catch (error) {
        assertCurrentOwnerScope(scope)
        throw error
      }
    },
    onSuccess: async (...args) => {
      if (isVerifiedOwnerScope(scope)) await options.onSuccess?.(...args)
    },
    onError: async (...args) => {
      if (isVerifiedOwnerScope(scope)) await options.onError?.(...args)
    },
    onSettled: async (...args) => {
      if (isVerifiedOwnerScope(scope)) await options.onSettled?.(...args)
    },
  })
  return {
    ...result,
    mutate: (variables: TVariables, callbacks?: MutateOptions<TData, TError, TVariables, TContext>) =>
      result.mutate(variables, currentCallbacks(callbacks, scope)),
    mutateAsync: async (variables: TVariables, callbacks?: MutateOptions<TData, TError, TVariables, TContext>) => {
      const data = await result.mutateAsync(variables, currentCallbacks(callbacks, scope))
      assertCurrentOwnerScope(scope)
      return data
    },
  }
}
