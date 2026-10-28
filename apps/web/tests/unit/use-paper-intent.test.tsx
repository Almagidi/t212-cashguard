import { beforeEach, expect, jest, test } from '@jest/globals'
import { act, renderHook, waitFor } from '@testing-library/react'
import { usePaperIntent } from '@/hooks/use-paper-intent'
import { api } from '@/services/api'

const owner = '00000000-0000-4000-8000-000000000001'
import { recorded, blocked } from './fixtures/paper-operation'
const payload = { ticker: 'EXAMPLE', side: 'buy' as const, quantity: '1', estimated_price: '100', source: 'manual_demo_ui', venue: 'paper' as const, paper_only: true as const, order_type: 'market' as const }


beforeEach(() => { sessionStorage.clear(); jest.restoreAllMocks() })

test('uncertain result survives reload and retries the exact stored identity/payload', async () => {
  const send = jest.fn<(value: unknown) => Promise<unknown>>().mockRejectedValueOnce(new Error('connection lost'))
  const first = renderHook(() => usePaperIntent(owner, send))
  await waitFor(() => expect(first.result.current.ready).toBe(true))
  await act(() => first.result.current.submit(payload))
  const operation = first.result.current.intent!.operationId
  expect(first.result.current.result?.tone).toBe('unknown')
  first.unmount()
  send.mockImplementationOnce(async () => recorded(operation, true))
  const reload = renderHook(() => usePaperIntent(owner, send))
  await waitFor(() => expect(reload.result.current.ready).toBe(true))
  await act(() => reload.result.current.submit({ ...payload, quantity: '2' }))
  expect(send).toHaveBeenNthCalledWith(1, { ...payload, operation_id: operation, expected_owner_id: owner })
  expect(send).toHaveBeenNthCalledWith(2, { ...payload, operation_id: operation, expected_owner_id: owner })
  expect(reload.result.current.result?.title).toBe('Paper order already recorded')
  expect(reload.result.current.intent?.payload).toBeUndefined()
})

test('rapid double submit has one identity and completed reload cannot submit again implicitly', async () => {
  let finish!: (result: unknown) => void
  const send = jest.fn<(value: unknown) => Promise<unknown>>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const hook = renderHook(() => usePaperIntent(owner, send))
  await waitFor(() => expect(hook.result.current.ready).toBe(true))
  let first!: Promise<void>
  act(() => { first = hook.result.current.submit(payload); void hook.result.current.submit(payload) })
  expect(send).toHaveBeenCalledTimes(1)
  const operation = hook.result.current.intent!.operationId
  await act(async () => { finish(recorded(operation)); await first })
  hook.unmount()
  const reload = renderHook(() => usePaperIntent(owner, send))
  await waitFor(() => expect(reload.result.current.intent?.state).toBe('recorded'))
  await act(() => reload.result.current.submit(payload))
  expect(send).toHaveBeenCalledTimes(1)
  act(() => reload.result.current.startNew())
  expect(reload.result.current.intent).toBeNull()
})

test.each([422, 409, 503])('HTTP %s without recorded metadata and GET404 never clear an unresolved intent', async code => {
  const send = jest.fn<(value: unknown) => Promise<unknown>>().mockRejectedValue({ response: { status: code, data: { detail: 'synthetic failure' } } })
  jest.spyOn(api, 'getPaperOperation').mockRejectedValueOnce({ response: { status: 404 } })
  const hook = renderHook(() => usePaperIntent(owner, send))
  await waitFor(() => expect(hook.result.current.ready).toBe(true))
  await act(() => hook.result.current.submit(payload))
  const operation = hook.result.current.intent!.operationId
  await act(() => hook.result.current.check())
  expect(api.getPaperOperation).toHaveBeenCalledWith(operation, owner)
  act(() => hook.result.current.startNew())
  expect(hook.result.current.intent?.operationId).toBe(operation)
  expect(hook.result.current.intent?.state).toBe('pending')
  expect(hook.result.current.result?.tone).toBe('unknown')
})

test('typed recorded refusal removes payload and stays a refusal', async () => {
  const send = jest.fn<(value: any) => Promise<unknown>>().mockImplementation(async value => {
    throw { response: { status: 422, data: blocked(value.operation_id) } }
  })
  const hook = renderHook(() => usePaperIntent(owner, send))
  await waitFor(() => expect(hook.result.current.ready).toBe(true))
  await act(() => hook.result.current.submit(payload))
  expect(hook.result.current.result?.tone).toBe('blocked')
  expect(hook.result.current.intent?.state).toBe('recorded')
  expect(hook.result.current.intent?.payload).toBeUndefined()
})
