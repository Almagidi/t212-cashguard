import { test, expect } from './test'
import { ensureAppPage } from './helpers'
import { textContrast } from './status-contrast'

type EntryObservation = { frames: Array<{ opacity: number; theme: string }>; done: boolean; timedOut: boolean }

for (const theme of ['light', 'dark'] as const) {
  test(`operator text stays opaque from its first naturally visible frame ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 })
    // Install before document creation so waiting for application readiness cannot
    // erase the low-contrast entry frames this regression is intended to catch.
    await page.addInitScript(theme => {
      localStorage.setItem('theme', theme)
      if (location.pathname !== '/app/operator') return
      const observation: EntryObservation = { frames: [], done: false, timedOut: false }
      ;(window as Window & { __operatorEntry?: EntryObservation }).__operatorEntry = observation
      const deadline = performance.now() + 15_000
      let stable = 0
      const sample = () => {
        const element = [...document.querySelectorAll('main *')].find(element =>
          element.childElementCount === 0 && element.textContent?.trim() === 'Runtime Diagnostics')
        if (element && element.getClientRects().length && getComputedStyle(element).visibility === 'visible') {
          const box = element.getBoundingClientRect()
          let opacity = 1
          for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) {
            opacity *= Number(getComputedStyle(ancestor).opacity)
          }
          if (box.right > 0 && box.left < innerWidth && box.bottom > 0 && box.top < innerHeight && opacity > 0) {
            observation.frames.push({ opacity, theme: document.documentElement.className })
            stable = opacity === 1 ? stable + 1 : 0
            if (stable >= 3) { observation.done = true; return }
          }
        }
        if (performance.now() >= deadline) { observation.done = true; observation.timedOut = true; return }
        requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
    }, theme)
    await ensureAppPage(page, '/app/operator', 'Operator')
    await page.waitForFunction(() => (window as Window & { __operatorEntry?: EntryObservation }).__operatorEntry?.done)
    const observation = await page.evaluate(() => (window as Window & { __operatorEntry?: EntryObservation }).__operatorEntry!)
    await testInfo.attach(`operator-entry-${theme}`, { body: JSON.stringify(observation), contentType: 'application/json' })
    expect(observation.timedOut, 'Natural operator entry completed').toBe(false)
    expect(observation.frames.length, 'Actually sampled visible Runtime Diagnostics text').toBeGreaterThanOrEqual(3)
    expect(observation.frames.every(frame => frame.theme.split(' ').includes(theme))).toBe(true)
    expect(observation.frames.filter(frame => frame.opacity !== 1), 'Active status text must not fade through low-contrast positive-opacity frames').toEqual([])
    const rows = await textContrast(page)
    expect(rows.map(row => row.text)).toContain('Runtime Diagnostics')
    expect(rows.filter(row => !row.disabled && (row.unsupported.length || row.ratio < row.threshold))).toEqual([])
  })
}
