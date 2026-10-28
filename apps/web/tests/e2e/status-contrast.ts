import type { Page } from './test'

/** Wait for natural finite animation completion and consecutive stable paint frames.
 * Visibility alone includes opacity-zero elements; do not force animation.finish().
 */
export async function waitForStablePaint(page: Page, selector = 'main') {
  await page.evaluate(async selector => {
    const deadline = performance.now() + 5_000
    let previous = '', stable = 0
    await new Promise<void>((resolve, reject) => {
      const sample = () => {
        const roots = [...document.querySelectorAll(selector)]
        const relevant = (element: Element) => roots.some(root => root.contains(element) || element.contains(root))
        const unfinished = document.getAnimations().some(animation => {
          const target = animation.effect instanceof KeyframeEffect ? animation.effect.target : null
          return target instanceof Element && relevant(target)
            && animation.effect?.getTiming().iterations !== Infinity
            && (animation.pending || !['finished', 'idle'].includes(animation.playState))
        })
        // Decorative pulse/spinner descendants can run forever. Measure only
        // rendered text and its paint ancestors; pulsing text remains a failure.
        const measured = new Set<Element>()
        for (const root of roots) {
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
          while (walker.nextNode()) {
            const element = walker.currentNode.parentElement
            if (!walker.currentNode.textContent?.trim() || !element || ['SCRIPT', 'STYLE'].includes(element.tagName)
              || !element.getClientRects().length || getComputedStyle(element).visibility !== 'visible') continue
            const closed = element.closest('details:not([open])')
            if (closed && !closed.querySelector(':scope > summary')?.contains(element)) continue
            for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) measured.add(ancestor)
          }
        }
        const paint = [...measured].map(element => {
          const style = getComputedStyle(element), rect = element.getBoundingClientRect()
          return [style.color, style.backgroundColor, style.opacity, style.transform, style.boxShadow,
            rect.x, rect.y, rect.width, rect.height]
        })
        const signature = JSON.stringify(paint)
        stable = roots.length && !unfinished && signature === previous ? stable + 1 : 0
        previous = signature
        if (stable >= 2) return resolve()
        if (performance.now() >= deadline) return reject(new Error('Relevant paint did not naturally settle within 5 seconds'))
        requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
    })
  }, selector)
}

export async function selectTheme(page: Page, theme: 'light' | 'dark') {
  const current = await page.locator('html').evaluate(element => element.classList.contains('dark') ? 'dark' : element.classList.contains('light') ? 'light' : null)
  if (!current) throw new Error('Resolved application theme is unknown')
  if (current !== theme) await page.getByRole('button', { name: 'Toggle theme', exact: true }).click()
  await page.waitForFunction(theme => document.documentElement.classList.contains(theme), theme)
}

export async function textContrast(page: Page, selector = 'main') {
  await waitForStablePaint(page, selector)
  return page.locator(selector).evaluateAll(roots => {
    const ctx = document.createElement('canvas').getContext('2d', { willReadFrequently: true })!
    const rgba = (css: string) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].map((n, i) => i === 3 ? n / 255 : n) }
    const over = (a: number[], b: number[]) => [0, 1, 2].map(i => a[i] * a[3] + b[i] * (1 - a[3])).concat(1)
    const luminance = (a: number[]) => a.slice(0, 3).map(n => n / 255).map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4).reduce((s, n, i) => s + n * [.2126, .7152, .0722][i], 0)
    const rows: Array<{ text: string; ratio: number; threshold: number; unsupported: string[]; disabled: boolean; color: string; background: number[] }> = []
    for (const root of roots) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
      let node: Node | null
      while ((node = walker.nextNode())) {
        const text = node.textContent?.trim(), element = node.parentElement
        if (!text || !element || ['SCRIPT', 'STYLE'].includes(element.tagName) || !element.getClientRects().length || getComputedStyle(element).visibility !== 'visible') continue
        const closed = element.closest('details:not([open])')
        if (closed && !closed.querySelector(':scope > summary')?.contains(element)) continue
        const range = document.createRange(); range.selectNodeContents(node)
        if (!range.getClientRects().length) continue
        const ancestors: Element[] = [], unsupported: string[] = []
        let current: Element | null = element
        while (current) {
          ancestors.unshift(current); const style = getComputedStyle(current)
          if (style.backgroundImage !== 'none') unsupported.push('background-image')
          if (style.backdropFilter !== 'none') unsupported.push('backdrop-filter')
          if (+style.opacity !== 1) unsupported.push('group-opacity')
          current = current.parentElement
        }
        let background = [255, 255, 255, 1]
        for (const ancestor of ancestors) background = over(rgba(getComputedStyle(ancestor).backgroundColor), background)
        const style = getComputedStyle(element), foreground = over(rgba(style.color), background), a = luminance(foreground), b = luminance(background)
        const size = parseFloat(style.fontSize), weight = parseInt(style.fontWeight)
        rows.push({ text, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05), threshold: size >= 24 || (size >= 18.6666667 && weight >= 700) ? 3 : 4.5, unsupported: [...new Set(unsupported)], disabled: !!element.closest(':disabled,[aria-disabled="true"]'), color: style.color, background })
      }
    }
    return rows
  })
}
