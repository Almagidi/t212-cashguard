import { test, expect, type Page } from './test'
import { ensureAppPage } from './helpers'
import { blocked, recorded } from '../unit/fixtures/paper-operation'

// These receipt fixtures exercise UI presentation only; the real backend paper
// journey is covered separately. No broker or financial mutation is dispatched.
async function prepare(page: Page) {
  await page.setViewportSize({ width: 320, height: 800 })
}

async function resultContrast(page: Page) {
  return page.getByTestId('paper-order-status-message').locator('p').evaluateAll(elements => {
    const canvas = new OffscreenCanvas(1, 1), ctx = canvas.getContext('2d')!
    const rgba = (css: string) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].map((n, i) => i === 3 ? n / 255 : n) }
    const blend = (fg: number[], bg: number[]) => fg.slice(0, 3).map((v, i) => v * fg[3] + bg[i] * (1 - fg[3]))
    const luminance = (rgb: number[]) => rgb.map(v => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }).reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0)
    return elements.map(element => {
      const chain: Element[] = []; for (let e: Element | null = element; e; e = e.parentElement) chain.push(e)
      let background = [255, 255, 255]
      for (const e of chain.reverse()) background = blend(rgba(getComputedStyle(e).backgroundColor), background)
      const style = getComputedStyle(element), fg = rgba(style.color); fg[3] *= Number(style.opacity)
      const a = luminance(blend(fg, background)), b = luminance(background)
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
    })
  })
}

test('paper receipts remain readable in both themes and never substitute default inputs', async ({ page }) => {
  await prepare(page)
  let refuse = false
  await page.route('**/v1/orders/paper', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    const operation = route.request().postDataJSON().operation_id
    await route.fulfill({ status: refuse ? 422 : 201, json: refuse ? blocked(operation) : { ...recorded(operation), ticker: 'ACCESSIBLE' } })
  })
  await ensureAppPage(page, '/app/orders', 'Orders')
  for (const outcome of ['filled', 'refused']) {
    if (outcome === 'refused') { refuse = true; await page.getByRole('button', { name: 'New Paper Order', exact: true }).click() }
    await page.getByLabel('Ticker', { exact: true }).fill('ACCESSIBLE')
    await page.getByTestId('paper-order-submit-button').click()
    await expect(page.getByTestId('paper-order-status-message')).toContainText(outcome === 'filled' ? 'Paper order filled' : 'blocked by safety controls')
    await expect(page.getByLabel('Ticker', { exact: true })).toHaveCount(0)
    await expect(page.getByTestId('paper-operation-inputs-unavailable')).toBeVisible()
    for (const light of [true, false]) {
      if (await page.evaluate(() => document.documentElement.classList.contains('light')) !== light) {
        await page.getByRole('button', { name: 'Toggle theme' }).focus()
        await page.keyboard.press('Enter')
      }
      await expect.poll(() => page.evaluate(() => document.documentElement.classList.contains('light'))).toBe(light)
      await expect.poll(async () => Math.min(...await resultContrast(page))).toBeGreaterThanOrEqual(4.5)
    }
  }
  await page.reload()
  await expect(page.getByTestId('paper-operation-inputs-unavailable')).toBeVisible()
  await expect(page.getByLabel('Ticker', { exact: true })).toHaveCount(0)
})

test('settings loading and failure are unknown, then only a current result supplies inactive copy', async ({ page }) => {
  await prepare(page)
  let fail = true
  await page.route('**/v1/settings', async route => {
    if (fail) return route.fulfill({ status: 503, json: { detail: 'Synthetic settings unavailable' } })
    const response = await route.fetch()
    return route.fulfill({ response, json: { ...await response.json(), kill_switch_active: false } })
  })
  await ensureAppPage(page, '/app/orders', 'Orders')
  await expect(page.getByText(/Kill switch status unknown/)).toBeVisible()
  await expect(page.getByText('Activate it to demonstrate a blocked paper order.')).toHaveCount(0)
  fail = false
  await page.reload()
  await expect(page.getByText('Activate it to demonstrate a blocked paper order.')).toBeVisible()
})

test('order inspector repeatedly contains focus and returns to its named opener', async ({ page }) => {
  await prepare(page)
  const order = recorded('00000000-0000-4000-8000-000000000010')
  await page.route('**/v1/orders?*', route => route.fulfill({ json: [order] }))
  await page.route(`**/v1/orders/${order.id}`, route => route.fulfill({ json: { ...order, events: [], broker_request: null, broker_response: null, signal_snapshot: null } }))
  await ensureAppPage(page, '/app/orders', 'Orders')
  const opener = page.getByRole('button', { name: 'Inspect order', exact: true })
  for (let cycle = 0; cycle < 3; cycle++) {
    await opener.focus(); await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'Order Inspector', exact: true })
    await expect(dialog).toBeVisible()
    await expect(dialog).toHaveAttribute('aria-modal', 'true')
    await expect(page.getByRole('button', { name: 'Close order inspector' })).toBeVisible()
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press(i % 2 ? 'Shift+Tab' : 'Tab')
      expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true)
    }
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(opener).toBeFocused()
  }
})

test('operator reflows at 400 percent equivalent without inferring database type from ports', async ({ page }, testInfo) => {
  await prepare(page)
  const actor = `synthetic-operator-${'a'.repeat(64)}@example.invalid`
  const workerTask = 'app.workers.tasks.run_strategy_signals'
  await page.route('**/v1/operator/status', async route => {
    const response = await route.fetch()
    const status = await response.json()
    await route.fulfill({ response, json: { ...status,
      schedulers: { ...status.schedulers, strategy_signals_task_name: workerTask },
      recent_activity: [{
      id: '00000000-0000-4000-8000-000000000020', occurred_at: '2026-10-28T00:00:00Z',
      action: 'auth_login', entity_type: 'user', entity_id: null, actor, payload_summary: {},
    }] } })
  })
  await page.setViewportSize({ width: 320, height: 256 })
  await ensureAppPage(page, '/app/operator', 'Operator')
  await expect(page.getByText('DCA Summary', { exact: true })).toBeVisible()
  await expect(page.getByText('Launcher unknown', { exact: true })).toBeVisible()
  await expect(page.getByText(/database type are not verified/)).toBeVisible()
  await expect(page.getByText(/incompatible with current startup admission/)).toHaveCount(0)
  await expect(page.getByText(actor)).toBeVisible()
  const taskName = page.getByTestId('strategy-signals-scheduler-status').getByText(workerTask, { exact: true })
  await expect(taskName).toBeVisible()
  await expect(taskName).toHaveText(workerTask)
  // Every part of the complete identifier must fit inside its row, even if an
  // ancestor clips overflow. A naturally fitting system font need not wrap.
  expect(await taskName.evaluate(element => {
    const row = element.closest('.kv-row')
    if (!row) throw new Error('Worker task row missing')
    const bounds = row.getBoundingClientRect()
    const range = document.createRange(); range.selectNodeContents(element)
    return [...range.getClientRects()]
      .filter(rect => rect.left < bounds.left - 1 || rect.right > bounds.right + 1 || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1)
      .map(({ left, right, top, bottom }) => ({ left, right, top, bottom }))
  })).toEqual([])
  for (const font of ['default', 'DejaVu Sans']) {
    // Exercise a common wider system fallback as well as the platform default.
    // The geometry contract is identical; no product font or clipping is changed.
    const override = font === 'default' ? null : await page.addStyleTag({ content: 'body { font-family: \"DejaVu Sans\", sans-serif; }' })
    await page.evaluate(() => document.fonts.ready)
    const reflow = await page.evaluate(() => {
      const text: unknown[] = [], walker = document.createTreeWalker(document.querySelector('main')!, NodeFilter.SHOW_TEXT)
      while (walker.nextNode()) {
        const node = walker.currentNode, parent = node.parentElement!
        if (!node.textContent?.trim() || parent.closest('table')) continue
        const range = document.createRange(); range.selectNodeContents(node)
        for (const rect of range.getClientRects()) if (rect.right > innerWidth + 1) text.push({ text: node.textContent, right: rect.right, wrap: getComputedStyle(parent).overflowWrap, classes: parent.className })
      }
      const box = (element: Element) => {
        const bounds = element.getBoundingClientRect(), style = getComputedStyle(element)
        return { tag: element.tagName, classes: typeof element.className === 'string' ? element.className : '',
          left: bounds.left, right: bounds.right, width: bounds.width,
          clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
          cssWidth: style.width, minWidth: style.minWidth, boxSizing: style.boxSizing,
          overflowX: style.overflowX, position: style.position, transform: style.transform }
      }
      const boxes: ReturnType<typeof box>[] = []
      const visit = (root: Document | ShadowRoot) => {
        for (const element of root.querySelectorAll('*')) {
          const bounds = element.getBoundingClientRect()
          if (bounds.width && (bounds.right > innerWidth || bounds.left < 0 || element.scrollWidth > element.clientWidth + 1)) boxes.push(box(element))
          if (element.shadowRoot) visit(element.shadowRoot)
        }
      }
      visit(document)
      return { width: document.documentElement.scrollWidth, text, boxes, root: box(document.documentElement), body: box(document.body),
      viewport: { innerWidth, clientWidth: document.documentElement.clientWidth, devicePixelRatio, scale: visualViewport?.scale,
        fontFamily: getComputedStyle(document.body).fontFamily, fontSize: getComputedStyle(document.body).fontSize },
        animations: document.getAnimations().map(animation => ({ state: animation.playState, currentTime: animation.currentTime, target: (animation.effect as KeyframeEffect | null)?.target?.className })) }
    })
    await testInfo.attach(`operator-reflow-${font}`, { body: JSON.stringify(reflow), contentType: 'application/json' })
    expect(reflow.width, JSON.stringify({ font, ...reflow })).toBeLessThanOrEqual(320)
    expect(reflow.text).toEqual([])
    // The whole card, including padding and borders, must fit even when all
    // individual text ranges happen to end before the viewport edge.
    for (const title of ['Trading212 Summary', 'Kraken Summary', 'Account metadata', 'Safety boundary']) {
      const bounds = await page.getByRole('heading', { name: title, exact: true }).evaluate(heading => {
        const card = heading.parentElement!.classList.contains('flex-col') ? heading.parentElement!.parentElement! : heading.parentElement!
        const bounds = card.getBoundingClientRect()
        return { left: bounds.left, right: bounds.right }
      })
      expect(bounds.left, `${font}: ${title} left edge`).toBeGreaterThanOrEqual(0)
      expect(bounds.right, `${font}: ${title} right edge`).toBeLessThanOrEqual(320)
    }
    await override?.evaluate(element => (element as HTMLStyleElement).remove())
  }
  // Preserve the existing responsive two-column layouts at their own breakpoints.
  for (const width of [1024, 1440]) {
    await page.setViewportSize({ width, height: 800 })
    const columns = await page.evaluate(() => {
      const heading = (title: string) => [...document.querySelectorAll('h3')].find(element => element.textContent === title)!
      const summaryGrid = heading('Trading212 Summary').parentElement!.parentElement!.parentElement!
      const metadataGrid = heading('Account metadata').parentElement!.parentElement!
      return [summaryGrid, metadataGrid].map(grid => getComputedStyle(grid).gridTemplateColumns.split(' ').length)
    })
    expect(columns).toEqual(width === 1024 ? [1, 2] : [2, 2])
  }
})
