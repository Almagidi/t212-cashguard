import '@testing-library/jest-dom'
import { expect, jest, test } from '@jest/globals'
import { useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { OrderDetailDialog } from '@/components/orders/order-detail-dialog'

function Inspector() {
  const [open, setOpen] = useState(false)
  return <><button onClick={() => setOpen(true)}>Inspect saved order</button><button>Background action</button><OrderDetailDialog open={open} onClose={() => setOpen(false)} order={null} loading /></>
}

test('inspector has modal semantics, named close, contained keyboard focus and repeated Escape return', async () => {
  const user = userEvent.setup()
  render(<Inspector />)
  const opener = screen.getByRole('button', { name: 'Inspect saved order' })
  // jsdom has no layout; represent this connected, visible opener accurately.
  jest.spyOn(opener, 'getClientRects').mockReturnValue([new DOMRect(0, 0, 120, 32)] as unknown as DOMRectList)
  await user.tab()
  for (let cycle = 0; cycle < 2; cycle++) {
    expect(opener).toHaveFocus()
    await user.keyboard('{Enter}')
    const dialog = await screen.findByRole('dialog', { name: 'Order Inspector' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    expect(screen.getByRole('button', { name: 'Close order inspector' })).toBeVisible()
    for (let i = 0; i < 4; i++) { await user.tab(); expect(dialog).toContainElement(document.activeElement as HTMLElement) }
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(opener).toHaveFocus())
  }
})
