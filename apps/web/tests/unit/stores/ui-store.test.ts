import { beforeEach, describe, expect, it, jest } from '@jest/globals'

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

  it('persists sidebarCollapsed to localStorage and restores on reimport', async () => {
    const { useUIStore: store1 } = await import('@/stores/ui-store')
    store1.getState().setSidebarCollapsed(true)
    expect(localStorage.getItem('ui-prefs')).toBeTruthy()

    jest.resetModules()
    const { useUIStore: store2 } = await import('@/stores/ui-store')
    expect(store2.getState().sidebarCollapsed).toBe(true)
  })
})
