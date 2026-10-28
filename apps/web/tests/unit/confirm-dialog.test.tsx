import '@testing-library/jest-dom'

import { describe, expect, it, jest } from '@jest/globals'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ConfirmDialog } from '@/components/shared/confirm-dialog'

describe('ConfirmDialog', () => {
  it('keeps initial focus inside a busy modal when both buttons are disabled', () => {
    render(<ConfirmDialog open loading onClose={jest.fn()} onConfirm={jest.fn()} title="Working" description="Awaiting the result." />)
    expect(screen.getByRole('dialog')).toHaveFocus()
  })
  it('names the modal and starts focus on the least destructive choice', () => {
    render(<ConfirmDialog open onClose={jest.fn()} onConfirm={jest.fn()} title="Review flatten" description="Close positions only after review." dangerous />)
    expect(screen.getByRole('dialog', { name: 'Review flatten' })).toHaveAccessibleDescription('Close positions only after review.')
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()
  })
  it('does not render when closed', () => {
    const { container } = render(
      <ConfirmDialog
        open={false}
        onClose={jest.fn()}
        onConfirm={jest.fn()}
        title="Dangerous action"
        description="This should not be visible."
      />,
    )

    expect(container.innerHTML).toBe('')
  })

  it('renders title and description when open', () => {
    render(
      <ConfirmDialog
        open
        onClose={jest.fn()}
        onConfirm={jest.fn()}
        title="Kill switch"
        description="All automated trading will halt."
        dangerous
      />,
    )

    expect(screen.getByText('Kill switch')).toBeTruthy()
    expect(screen.getByText('All automated trading will halt.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeTruthy()
  })

  it('calls onConfirm when confirm is clicked', async () => {
    const user = userEvent.setup()
    const onConfirm = jest.fn()

    render(
      <ConfirmDialog
        open
        onClose={jest.fn()}
        onConfirm={onConfirm}
        title="Flatten all"
        description="Close every open position."
        confirmLabel="Flatten"
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Flatten' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when escape is pressed', () => {
    const onClose = jest.fn()

    render(
      <ConfirmDialog
        open
        onClose={onClose}
        onConfirm={jest.fn()}
        title="Cancel all"
        description="Remove every pending order."
      />,
    )

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when the backdrop is clicked', async () => {
    const user = userEvent.setup()
    const onClose = jest.fn()

    render(
      <ConfirmDialog
        open
        onClose={onClose}
        onConfirm={jest.fn()}
        title="Pause strategy"
        description="No new trades will be opened."
      />,
    )

    const backdrop = document.querySelector('[data-testid=confirmation-overlay]')
    expect(backdrop).not.toBeNull()
    await user.click(backdrop!)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('disables both buttons while an async action is in flight', () => {
    render(
      <ConfirmDialog
        open
        onClose={jest.fn()}
        onConfirm={jest.fn()}
        title="Activating kill switch"
        description="Halting all automated trading…"
        loading
      />,
    )

    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
