import { beforeEach, describe, expect, it } from '@jest/globals'

// Reset module between tests so store state doesn't leak
beforeEach(() => {
  jest.resetModules()
  localStorage.clear()
})

describe('useUIStore', () => {
  it('has sidebarCollapsed = false by default', async () => {
    const { useUIStore } = await import('@/stores/ui-store')
    const state = useUIStore.getState()
    expect(state.sidebarCollapsed).toBe(false)
  })

  it('toggleSidebar flips sidebarCollapsed', async () => {
    const { useUIStore } = await import('@/stores/ui-store')
    useUIStore.getState().toggleSidebar()
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    useUIStore.getState().toggleSidebar()
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })

  it('setSidebarCollapsed sets an explicit value', async () => {
    const { useUIStore } = await import('@/stores/ui-store')
    useUIStore.getState().setSidebarCollapsed(true)
    expect(useUIStore.getState().sidebarCollapsed).toBe(true)
    useUIStore.getState().setSidebarCollapsed(false)
    expect(useUIStore.getState().sidebarCollapsed).toBe(false)
  })
})
