import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals'
import axios, { AxiosError, AxiosHeaders } from 'axios'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'

type ApiModule = typeof import('@/services/api')

function okAdapter(seen: InternalAxiosRequestConfig[]): AxiosAdapter {
  return async (config) => {
    seen.push(config)
    const response: AxiosResponse = {
      data: { status: 'ok' },
      status: 200,
      statusText: 'OK',
      headers: new AxiosHeaders(),
      config,
    }
    return response
  }
}

// services/api.ts builds its axios instance at import time, and isolateModules gives it a fresh
// axios module, so the adapter must be installed on that isolated instance before the import.
function loadApi(adapter: AxiosAdapter, verifiedOwner = false): ApiModule {
  let loaded: ApiModule | undefined
  jest.isolateModules(() => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const isolatedAxios = require('axios') as typeof axios
    isolatedAxios.defaults.adapter = adapter
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    loaded = require('@/services/api') as ApiModule
    if (verifiedOwner) {
      const { useAuthStore } = require('@/stores/auth') as typeof import('@/stores/auth')
      useAuthStore.getState().setUser({ id: '11111111-1111-4111-8111-111111111111', email: 'synthetic@example.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
    }
  })
  if (!loaded) throw new Error('services/api failed to load')
  return loaded
}

describe('API client contract with axios', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  describe('isApiUnreachableError', () => {
    const { isApiUnreachableError } = loadApi(okAdapter([]))

    it('reports a refused or dropped connection (ERR_NETWORK) as unreachable', () => {
      const error = new AxiosError('Network Error', AxiosError.ERR_NETWORK)
      expect(isApiUnreachableError(error)).toBe(true)
    })

    it('keeps the legacy message-only form working when no response exists', () => {
      expect(isApiUnreachableError({ message: 'Network Error' })).toBe(true)
    })

    it('does not report an HTTP error response as unreachable', () => {
      const error = new AxiosError('Service Unavailable', AxiosError.ERR_BAD_RESPONSE, undefined, undefined, {
        status: 503,
        statusText: 'Service Unavailable',
        data: {},
        headers: {},
        config: { headers: new AxiosHeaders() },
      })
      expect(isApiUnreachableError(error)).toBe(false)
    })

    it('does not report a navigation-cancelled request (ECONNABORTED, axios >= 1.20) as unreachable', () => {
      const error = new AxiosError('Request aborted', AxiosError.ECONNABORTED)
      expect(isApiUnreachableError(error)).toBe(false)
    })

    it('does not report invalid request options (ERR_BAD_OPTION_VALUE) as unreachable', () => {
      const error = new AxiosError('bad option', AxiosError.ERR_BAD_OPTION_VALUE)
      expect(isApiUnreachableError(error)).toBe(false)
    })
  })

  describe('request interceptor', () => {
    it('attaches the verified session bearer token to outgoing requests', async () => {
      localStorage.setItem('cg_token', 'test-token')
      const seen: InternalAxiosRequestConfig[] = []
      const { api } = loadApi(okAdapter(seen), true)

      await api.getBackendHealth()

      expect(seen).toHaveLength(1)
      expect(seen[0].headers.get('Authorization')).toBe('Bearer test-token')
    })

    it('does not dispatch a stored token before its owner is verified', async () => {
      localStorage.setItem('cg_token', 'test-token')
      const seen: InternalAxiosRequestConfig[] = []
      const { api } = loadApi(okAdapter(seen))
      await expect(api.getBackendHealth()).rejects.toMatchObject({ name: 'OwnerScopeChangedError' })
      expect(seen).toHaveLength(0)
    })

    it('sends no Authorization header when no token is stored', async () => {
      const seen: InternalAxiosRequestConfig[] = []
      const { api } = loadApi(okAdapter(seen))

      await api.getBackendHealth()

      expect(seen).toHaveLength(1)
      expect(seen[0].headers.has('Authorization')).toBe(false)
    })

    it('does not dispatch a request when reading the token throws', async () => {
      const seen: InternalAxiosRequestConfig[] = []
      const { api } = loadApi(okAdapter(seen))
      jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('storage blocked')
      })

      await expect(api.getBackendHealth()).rejects.toThrow('storage blocked')
      expect(seen).toHaveLength(0)
    })
  })
})
