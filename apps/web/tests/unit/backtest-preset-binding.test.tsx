import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals'
import BacktestPage from '@/app/app/backtest/page'
import api from '@/services/api'

for (const method of ['listBacktestStrategies', 'listPortfolioBacktestStrategies', 'runBacktest', 'getBacktestResult'] as const) {
  jest.spyOn(api, method)
}

const keys = ['orb', 'opening_fade', 'vwap_reclaim', 'closing_momentum', 'intraday_periodicity'] as const
const catalog = keys.map((key, index) => ({
  type: key, preset_key: key, label: `Preset ${key}`, description: `Synthetic ${key}`,
  strategy_params: { min_rvol: 1 + index / 10, allow_short: false },
}))
const mocked = jest.mocked(api)
beforeEach(() => {
  localStorage.clear()
  mocked.listBacktestStrategies.mockResolvedValue(catalog)
  mocked.listPortfolioBacktestStrategies.mockResolvedValue([])
  mocked.runBacktest.mockResolvedValue({ job_id: 'synthetic-job', status: 'running', message: 'started' })
  mocked.getBacktestResult.mockResolvedValue({ status: 'running' })
})
afterEach(() => { jest.clearAllMocks(); jest.useRealTimers() })

test.each(keys)('the website sends the complete selected %s preset', async key => {
  render(<BacktestPage />)
  await screen.findByRole('option', { name: `Preset ${key}` })
  fireEvent.change(screen.getByLabelText('Strategy', { selector: '#strategy_type' }), { target: { value: key } })
  fireEvent.click(screen.getByRole('button', { name: 'Run Backtest' }))
  await waitFor(() => expect(mocked.runBacktest).toHaveBeenCalledTimes(1))
  expect(mocked.runBacktest.mock.calls[0][0]).toMatchObject({
    strategy_type: key, preset_key: key, strategy_params: catalog.find(item => item.type === key)!.strategy_params,
  })
})

test('unloaded catalog cannot silently submit class defaults', async () => {
  mocked.listBacktestStrategies.mockReturnValue(new Promise(() => {}))
  render(<BacktestPage />)
  const button = screen.getByRole('button', { name: 'Run Backtest' })
  expect(button).toBeDisabled()
  fireEvent.submit(button.closest('form')!)
  await act(async () => {})
  expect(mocked.runBacktest).not.toHaveBeenCalled()
})

test('a mismatched catalog record cannot dispatch another preset', async () => {
  mocked.listBacktestStrategies.mockResolvedValue([{ ...catalog[0], preset_key: 'vwap_reclaim' }])
  render(<BacktestPage />)
  await screen.findByRole('option', { name: 'Preset orb' })
  const button = screen.getByRole('button', { name: 'Run Backtest' })
  expect(button).toBeDisabled()
  fireEvent.submit(button.closest('form')!)
  await act(async () => {})
  expect(mocked.runBacktest).not.toHaveBeenCalled()
})

test.each(['invalid', [1], { min_rvol: null }, { min_rvol: { nested: 1 } }, { min_rvol: Infinity }])('a malformed parameter snapshot is unavailable', async params => {
  mocked.listBacktestStrategies.mockResolvedValue([{ ...catalog[0], strategy_params: params as unknown as Record<string, unknown> }])
  render(<BacktestPage />)
  await screen.findByRole('option', { name: 'Preset orb' })
  const button = screen.getByRole('button', { name: 'Run Backtest' })
  expect(button).toBeDisabled()
  fireEvent.submit(button.closest('form')!)
  await act(async () => {})
  expect(mocked.runBacktest).not.toHaveBeenCalled()
})

test('switching presets does not relabel the captured submitted run', async () => {
  render(<BacktestPage />)
  await screen.findByRole('option', { name: 'Preset orb' })
  fireEvent.click(screen.getByRole('button', { name: 'Run Backtest' }))
  await waitFor(() => expect(mocked.runBacktest).toHaveBeenCalledTimes(1))
  fireEvent.change(screen.getByLabelText('Strategy', { selector: '#strategy_type' }), { target: { value: 'closing_momentum' } })
  const run = screen.getByTestId('backtest-run-configuration')
  expect(run).toHaveTextContent('Submitted primary configuration: Preset orb')
  expect(run).not.toHaveTextContent('Preset closing_momentum')
})

test('server stale-preset rejection is visible and never retries with defaults', async () => {
  mocked.runBacktest.mockRejectedValue(new Error('Preset parameters differ; reload the catalog'))
  render(<BacktestPage />)
  await screen.findByRole('option', { name: 'Preset orb' })
  fireEvent.click(screen.getByRole('button', { name: 'Run Backtest' }))
  await screen.findByText('Preset parameters differ; reload the catalog')
  expect(mocked.runBacktest).toHaveBeenCalledTimes(1)
  expect(mocked.runBacktest.mock.calls[0][0]).toHaveProperty('preset_key', 'orb')
})

test('a delayed second submission never polls or accepts the previous job', async () => {
  jest.useFakeTimers()
  render(<BacktestPage />)
  await act(async () => {})
  fireEvent.click(screen.getByRole('button', { name: 'Run Backtest' }))
  await act(async () => {})
  mocked.getBacktestResult.mockResolvedValueOnce({ status: 'error', error: 'Synthetic first run stopped' })
  await act(async () => { jest.advanceTimersByTime(2000) })
  expect(mocked.getBacktestResult).toHaveBeenCalledWith('synthetic-job')
  let resolve!: (value: { job_id: string; status: string; message: string }) => void
  mocked.runBacktest.mockReturnValueOnce(new Promise(done => { resolve = done }))
  fireEvent.change(screen.getByLabelText('Strategy', { selector: '#strategy_type' }), { target: { value: 'vwap_reclaim' } })
  fireEvent.click(screen.getByRole('button', { name: 'Run Backtest' }))
  await act(async () => {})
  const polls = mocked.getBacktestResult.mock.calls.length
  await act(async () => { jest.advanceTimersByTime(4000) })
  expect(mocked.getBacktestResult).toHaveBeenCalledTimes(polls)
  await act(async () => { resolve({ job_id: 'new-job', status: 'running', message: 'started' }) })
  await act(async () => { jest.advanceTimersByTime(2000) })
  expect(mocked.getBacktestResult).toHaveBeenLastCalledWith('new-job')
  expect(screen.getByTestId('backtest-run-configuration')).toHaveTextContent('Preset vwap_reclaim')
})
