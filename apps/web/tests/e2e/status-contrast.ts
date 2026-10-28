import type { Page } from './test'

export async function textContrast(page: Page, selector = 'main') {
  await page.evaluate(() => {
    for (const animation of document.getAnimations()) {
      if (animation.effect?.getTiming().iterations !== Infinity) animation.finish()
    }
  })
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
