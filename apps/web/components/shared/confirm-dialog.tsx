'use client'
import * as React from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { AlertTriangle } from 'lucide-react'
import { Button, Card } from '@/components/ui'
import { cn } from '@/lib/utils'

interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: string
  description: string
  confirmLabel?: string
  cancelLabel?: string
  dangerous?: boolean
  loading?: boolean
  confirmButtonTestId?: string
  confirmDisabled?: boolean
}

export function ConfirmDialog({
  open, onClose, onConfirm, title, description,
  confirmLabel = 'Confirm', cancelLabel = 'Cancel',
  dangerous = false, loading = false, confirmButtonTestId, confirmDisabled = false,
}: ConfirmDialogProps) {
  const cancelRef = React.useRef<HTMLButtonElement>(null)
  const contentRef = React.useRef<HTMLDivElement>(null)
  const openerRef = React.useRef<HTMLElement | null>(null)

  if (!open) return null

  return (
    <Dialog.Root open={open} onOpenChange={value => { if (!value && !loading) onClose() }}>
      <Dialog.Portal>
      <Dialog.Overlay data-testid="confirmation-overlay" className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
      <Dialog.Content asChild
        onOpenAutoFocus={event => { event.preventDefault(); openerRef.current = document.activeElement as HTMLElement; (loading ? contentRef.current : cancelRef.current)?.focus() }}
        onCloseAutoFocus={event => { event.preventDefault(); const opener = openerRef.current; (opener?.isConnected && !opener.hasAttribute('disabled') && opener.getClientRects().length ? opener : document.querySelector<HTMLElement>('main'))?.focus() }}
        onEscapeKeyDown={event => { if (loading) event.preventDefault() }}
        onPointerDownOutside={event => { if (loading) event.preventDefault() }}
      >
      <Card ref={contentRef} className={cn('fixed left-1/2 top-1/2 z-50 -translate-x-1/2 -translate-y-1/2 w-[calc(100%_-_2rem)] max-w-md max-h-[90dvh] overflow-y-auto p-6', dangerous && 'border-red-500/40')}>
        <div className="flex gap-3 mb-4">
          {dangerous && (
            <div className="flex-shrink-0 w-10 h-10 rounded-full bg-red-500/15 flex items-center justify-center">
              <AlertTriangle className="w-5 h-5 text-red-400" />
            </div>
          )}
          <div>
            <Dialog.Title className={cn('text-base font-semibold', dangerous && 'text-red-700 dark:text-red-400')}>{title}</Dialog.Title>
            <Dialog.Description className="text-sm text-muted-foreground mt-1">{description}</Dialog.Description>
          </div>
        </div>
        <div className="flex gap-2 justify-end">
          <Button ref={cancelRef} variant="outline" size="sm" onClick={onClose} disabled={loading}>{cancelLabel}</Button>
          <Button
            variant={dangerous ? 'danger' : 'default'}
            size="sm"
            onClick={onConfirm}
            loading={loading}
            disabled={confirmDisabled || loading}
            data-testid={confirmButtonTestId}
          >
            {confirmLabel}
          </Button>
        </div>
      </Card>
      </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
