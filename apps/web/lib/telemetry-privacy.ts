/** Confidential metadata only; shared by optional browser, Node and Edge SDKs. */
type Data = Record<string, unknown>
export interface TelemetryTransport {
  send(envelope: unknown): PromiseLike<{ statusCode?: number }>
  flush(timeout?: number): PromiseLike<boolean>
}
export interface TelemetryIntegration {
  name: string
  beforeSetup?: (client: TelemetryClient) => void
  setup?: (client: TelemetryClient) => void
}
interface TelemetryClient {
  getTransport(): TelemetryTransport | undefined
  getOptions(): { enabled?: boolean; tunnel?: unknown }
  getDsn?(): unknown
}

// Explicit source names/line limits from the reviewed source. New files stay
// unqualified until this list is reviewed; arbitrary URLs/functions are withheld.
const SOURCE_LINES: Readonly<Record<string, number>> = {
  'app/app/alerts/loading.tsx': 23,
  'app/app/alerts/page.tsx': 116,
  'app/app/audit/loading.tsx': 32,
  'app/app/audit/page.tsx': 137,
  'app/app/backtest/loading.tsx': 36,
  'app/app/backtest/page.tsx': 1102,
  'app/app/broker/loading.tsx': 31,
  'app/app/broker/page.tsx': 340,
  'app/app/dashboard/loading.tsx': 78,
  'app/app/dashboard/page.tsx': 984,
  'app/app/emergency/loading.tsx': 25,
  'app/app/emergency/page.tsx': 329,
  'app/app/instruments/loading.tsx': 48,
  'app/app/instruments/page.tsx': 142,
  'app/app/journal/loading.tsx': 26,
  'app/app/journal/page.tsx': 402,
  'app/app/layout.tsx': 25,
  'app/app/operator/page.tsx': 20,
  'app/app/orders/loading.tsx': 45,
  'app/app/orders/page.tsx': 570,
  'app/app/positions/loading.tsx': 47,
  'app/app/positions/page.tsx': 147,
  'app/app/reports/loading.tsx': 69,
  'app/app/reports/page.tsx': 396,
  'app/app/risk/loading.tsx': 46,
  'app/app/risk/page.tsx': 195,
  'app/app/settings/loading.tsx': 24,
  'app/app/settings/page.tsx': 426,
  'app/app/strategies/[id]/loading.tsx': 48,
  'app/app/strategies/[id]/page.tsx': 1194,
  'app/app/strategies/loading.tsx': 39,
  'app/app/strategies/page.tsx': 315,
  'app/auth/login/page.tsx': 209,
  'app/layout.tsx': 36,
  'app/page.tsx': 5,
  'app/providers.tsx': 110,
  'components/backtest/portfolio-evidence-card.tsx': 49,
  'components/charts/candlestick.tsx': 299,
  'components/charts/equity-curve.tsx': 157,
  'components/dashboard/regime-badge.tsx': 119,
  'components/layout/auth-gate.tsx': 71,
  'components/layout/main-content.tsx': 18,
  'components/layout/page-header.tsx': 26,
  'components/layout/shield-logo.tsx': 41,
  'components/layout/sidebar.tsx': 299,
  'components/layout/status-bar.tsx': 120,
  'components/layout/topbar.tsx': 163,
  'components/operator/broker-status-panel.tsx': 251,
  'components/operator/dca-activity-panel.tsx': 301,
  'components/operator/operator-dashboard.tsx': 1845,
  'components/operator/runtime-diagnostics.tsx': 344,
  'components/orders/order-detail-dialog.tsx': 715,
  'components/shared/confirm-dialog.tsx': 65,
  'components/shared/error-boundary.tsx': 94,
  'components/shared/query-error.tsx': 82,
  'components/strategies/strategy-preset-grid.tsx': 140,
  'components/ui/index.tsx': 373,
  'components/ui/terminal-card.tsx': 58,
  'hooks/use-api.ts': 829,
  'hooks/use-websocket.ts': 277,
  'lib/utils.ts': 152,
  'services/api.ts': 682,
  'stores/app.ts': 17,
  'stores/auth.ts': 25,
  'stores/dashboard.ts': 71,
  'stores/ui-store.ts': 19,
}
const LEVELS = new Set(['debug', 'info', 'warning', 'error', 'fatal', 'log'])
const PLATFORMS = new Set(['javascript', 'node'])
const ENVIRONMENTS = new Set([
  'production',
  'development',
  'test',
  'mock',
  'paper',
  'demo',
  'live',
])
const ERROR_TYPES = new Set([
  'Error',
  'TypeError',
  'RangeError',
  'ReferenceError',
  'SyntaxError',
  'URIError',
  'EvalError',
  'AggregateError',
  'DOMException',
  'AxiosError',
])
const OPERATIONS = new Set([
  'http.server',
  'http.client',
  'db',
  'db.sql.query',
  'function',
  'navigation',
  'pageload',
  'ui.click',
  'ui.action',
  'middleware.nextjs',
])
const STATUSES = new Set([
  'ok',
  'cancelled',
  'unknown',
  'unknown_error',
  'invalid_argument',
  'deadline_exceeded',
  'not_found',
  'already_exists',
  'permission_denied',
  'resource_exhausted',
  'failed_precondition',
  'aborted',
  'out_of_range',
  'unimplemented',
  'internal_error',
  'unavailable',
  'data_loss',
  'unauthenticated',
])
const CATEGORIES = new Set([
  'fetch',
  'xhr',
  'console',
  'navigation',
  'ui.click',
  'http',
  'sentry',
  'error',
])
const MECHANISMS = new Set([
  'generic',
  'onerror',
  'onunhandledrejection',
  'instrument',
  'react',
  'auto.browser.global_handlers.onerror',
  'auto.browser.global_handlers.onunhandledrejection',
  'auto.http.nextjs.server_component',
  'auto.http.nextjs.route_handler',
])

function record(value: unknown): Data | null {
  if (typeof value !== 'object' || value === null) return null
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
    ? (value as Data)
    : null
}
function read(value: object, key: string): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(value, key)
  return descriptor && 'value' in descriptor ? descriptor.value : undefined
}
function array(value: unknown): unknown[] | null {
  return Array.isArray(value) &&
    Object.getPrototypeOf(value) === Array.prototype
    ? value
    : null
}
function list(value: unknown, limit: number): unknown[] {
  const source = array(value)
  if (!source) return []
  const result: unknown[] = []
  for (let i = 0; i < Math.min(source.length, limit); i += 1)
    result.push(read(source, String(i)))
  return result
}
function known(value: unknown, choices: Set<string>): string | undefined {
  return typeof value === 'string' && choices.has(value) ? value : undefined
}
function identifier(value: unknown, length: number): string | undefined {
  return typeof value === 'string' &&
    new RegExp(`^[0-9a-f]{${length}}$`).test(value)
    ? value
    : undefined
}
function timestamp(value: unknown): number | string | undefined {
  if (
    typeof value === 'number' &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 100_000_000_000
  )
    return value
  if (
    typeof value === 'string' &&
    value.length <= 40 &&
    /^[0-9T:.\-+Z ]+$/.test(value) &&
    Number.isFinite(Date.parse(value))
  )
    return value
  return undefined
}
function put(target: Data, key: string, value: unknown): void {
  if (value !== undefined) target[key] = value
}
function frames(value: unknown): Data[] {
  const stack = record(value)
  if (!stack) return []
  const result: Data[] = []
  for (const raw of list(read(stack, 'frames'), 100)) {
    const frame = record(raw)
    if (!frame) continue
    const filename = read(frame, 'filename')
    const line = read(frame, 'lineno')
    if (
      typeof filename !== 'string' ||
      filename.length > 2048 ||
      typeof line !== 'number' ||
      !Number.isInteger(line) ||
      line < 1
    )
      continue
    const path = filename.split(/[?#]/, 1)[0]
    const source = Object.entries(SOURCE_LINES).find(
      ([name, lines]) =>
        (path === name || path.endsWith('/' + name)) && line <= lines,
    )
    if (!source) continue
    result.push({ filename: 'app:///' + source[0], lineno: line, in_app: true })
  }
  return result
}
function exceptions(value: unknown): Data[] {
  const exception = record(value)
  if (!exception) return []
  const result: Data[] = []
  for (const raw of list(read(exception, 'values'), 10)) {
    const item = record(raw)
    if (
      !item ||
      (typeof read(item, 'type') !== 'string' &&
        typeof read(item, 'value') !== 'string')
    )
      continue
    const clean: Data = {
      type: known(read(item, 'type'), ERROR_TYPES) ?? 'Error',
      value: '[Filtered]',
    }
    const stack = frames(read(item, 'stacktrace'))
    if (stack.length) clean.stacktrace = { frames: stack }
    const mechanism = record(read(item, 'mechanism'))
    if (mechanism) {
      const safe: Data = {}
      put(safe, 'type', known(read(mechanism, 'type'), MECHANISMS))
      const handled = read(mechanism, 'handled')
      if (typeof handled === 'boolean') safe.handled = handled
      if (Object.keys(safe).length) clean.mechanism = safe
    }
    result.push(clean)
  }
  return result
}
function trace(value: unknown): Data {
  const input = record(value)
  const result: Data = {}
  if (!input) return result
  put(result, 'trace_id', identifier(read(input, 'trace_id'), 32))
  put(result, 'span_id', identifier(read(input, 'span_id'), 16))
  put(result, 'parent_span_id', identifier(read(input, 'parent_span_id'), 16))
  put(result, 'op', known(read(input, 'op'), OPERATIONS))
  put(result, 'status', known(read(input, 'status'), STATUSES))
  return result
}

export function sanitizeTelemetryBreadcrumb(value: unknown): Data | null {
  try {
    const input = record(value)
    if (!input) return null
    const category = known(read(input, 'category'), CATEGORIES)
    if (!category) return null
    const result: Data = { category, message: '[Filtered]' }
    put(result, 'level', known(read(input, 'level'), LEVELS))
    put(result, 'timestamp', timestamp(read(input, 'timestamp')))
    return result
  } catch {
    return null
  }
}

export function sanitizeTelemetryEvent(value: unknown): Data | null {
  try {
    const input = record(value)
    if (!input) return null
    const type = read(input, 'type')
    if (type !== undefined && type !== 'transaction') return null
    const exception = exceptions(read(input, 'exception'))
    const message =
      typeof read(input, 'message') === 'string' ||
      record(read(input, 'logentry')) !== null
    if (type !== 'transaction' && !exception.length && !message) return null
    const result: Data = {}
    put(result, 'platform', known(read(input, 'platform'), PLATFORMS))
    put(result, 'event_id', identifier(read(input, 'event_id'), 32))
    put(result, 'timestamp', timestamp(read(input, 'timestamp')))
    put(result, 'level', known(read(input, 'level'), LEVELS))
    put(result, 'environment', known(read(input, 'environment'), ENVIRONMENTS))
    const release = read(input, 'release')
    if (
      typeof release === 'string' &&
      /^cashguard@\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[a-z0-9.]{1,30})?$/.test(release)
    )
      result.release = release
    if (exception.length) result.exception = { values: exception }
    if (message) result.message = '[Filtered]'
    const contexts = record(read(input, 'contexts'))
    const context = trace(contexts ? read(contexts, 'trace') : undefined)
    if (Object.keys(context).length) result.contexts = { trace: context }
    const tags = record(read(input, 'tags'))
    const status = tags ? read(tags, 'http.status_code') : undefined
    if (typeof status === 'string' && /^[1-5][0-9]{2}$/.test(status))
      result.tags = { 'http.status_code': status }
    const breadcrumbs = list(read(input, 'breadcrumbs'), 100)
      .map(sanitizeTelemetryBreadcrumb)
      .filter((item): item is Data => item !== null)
    if (breadcrumbs.length) result.breadcrumbs = breadcrumbs
    if (type === 'transaction') {
      result.type = 'transaction'
      result.transaction = 'diagnostic.' + (context.op ?? 'function')
      put(result, 'start_timestamp', timestamp(read(input, 'start_timestamp')))
      const spans: Data[] = []
      for (const raw of list(read(input, 'spans'), 200)) {
        const span = record(raw)
        if (!span) continue
        const safe = trace(span)
        if (!safe.trace_id || !safe.span_id) continue
        put(safe, 'start_timestamp', timestamp(read(span, 'start_timestamp')))
        put(safe, 'timestamp', timestamp(read(span, 'timestamp')))
        spans.push(safe)
      }
      if (spans.length) result.spans = spans
    }
    return result
  } catch {
    return null
  }
}

export function sanitizeTelemetryEnvelope(
  value: unknown,
): [Data, Array<[Data, Data]>] | null {
  try {
    const input = array(value)
    if (!input || input.length !== 2) return null
    const headers = record(read(input, '0'))
    const safeHeaders: Data = {}
    if (headers) {
      put(safeHeaders, 'event_id', identifier(read(headers, 'event_id'), 32))
      put(safeHeaders, 'sent_at', timestamp(read(headers, 'sent_at')))
    }
    const items: Array<[Data, Data]> = []
    for (const raw of list(read(input, '1'), 100)) {
      const item = array(raw)
      if (!item || item.length !== 2) continue
      const itemHeaders = record(read(item, '0'))
      const kind = itemHeaders ? read(itemHeaders, 'type') : undefined
      if (kind !== 'event' && kind !== 'transaction') continue
      const event = sanitizeTelemetryEvent(read(item, '1'))
      if (!event || (event.type === 'transaction') !== (kind === 'transaction'))
        continue
      items.push([{ type: kind }, event])
    }
    return items.length ? [safeHeaders, items] : null
  } catch {
    return null
  }
}

export function confidentialTelemetryIntegration(): TelemetryIntegration {
  const wrapped = new WeakSet<TelemetryTransport>()
  const install = (client: TelemetryClient) => {
    const options = client.getOptions()
    const enabled = options.enabled
    const hadEnabled = Object.prototype.hasOwnProperty.call(options, 'enabled')
    options.enabled = false
    try {
      const transport = client.getTransport()
      if (!transport || typeof transport.send !== 'function') return
      if (!wrapped.has(transport)) {
        let routingDsn: string | undefined
        if (options.tunnel) {
          // The Next.js tunnel needs a public DSN header. Rebuild it only
          // from the SDK's validated configuration; never copy event headers
          // or the deprecated private DSN password into routing metadata.
          const dsn = record(client.getDsn?.())
          if (!dsn) return
          const protocol = read(dsn, 'protocol')
          const publicKey = read(dsn, 'publicKey')
          const host = read(dsn, 'host')
          const projectId = read(dsn, 'projectId')
          const port = read(dsn, 'port') ?? ''
          const path = read(dsn, 'path') ?? ''
          if (
            (protocol !== 'http' && protocol !== 'https') ||
            typeof publicKey !== 'string' ||
            !/^\w+$/.test(publicKey) ||
            typeof host !== 'string' ||
            !host ||
            typeof projectId !== 'string' ||
            !/^\d+$/.test(projectId) ||
            typeof port !== 'string' ||
            (port !== '' && !/^\d+$/.test(port)) ||
            typeof path !== 'string'
          )
            return
          routingDsn = `${protocol}://${publicKey}@${host}${port ? ':' + port : ''}/${path ? path + '/' : ''}${projectId}`
        }
        const send = transport.send.bind(transport)
        const guard = (envelope: unknown) => {
          const clean = sanitizeTelemetryEnvelope(envelope)
          if (clean && routingDsn) clean[0].dsn = routingDsn
          return clean ? send(clean) : Promise.resolve({})
        }
        transport.send = guard
        if (transport.send !== guard) return
        wrapped.add(transport)
      }
      if (hadEnabled) options.enabled = enabled
      else delete options.enabled
    } catch {
      // Keep this client's delivery disabled if its transport cannot be guarded.
    }
  }
  return {
    name: 'CashGuardConfidentialTransport',
    // Install before default beforeSetup hooks too; setup is an idempotent fallback.
    // Later envelope hooks still pass through this final public send method.
    beforeSetup: install,
    setup: install,
  }
}
