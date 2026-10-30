import { afterEach, describe, expect, jest, test } from '@jest/globals'

const SECRET = 'SYNTHETIC_FRONTEND_NOT_REAL_SECRET'
const DSN = 'https://0123456789abcdef0123456789abcdef@sentry.invalid/1'

function errorEvent() {
  return {
    event_id: 'a'.repeat(32),
    level: 'error',
    exception: {
      values: [
        {
          type: 'TypeError',
          value: SECRET,
          stacktrace: {
            frames: [
              {
                filename: 'webpack-internal:///./services/api.ts',
                lineno: 12,
                function: SECRET,
                vars: { password: SECRET },
              },
            ],
          },
        },
      ],
    },
    request: {
      headers: { Authorization: SECRET, cookie: SECRET },
      data: [{ nested: { password: SECRET } }],
      query_string: SECRET,
    },
    extra: { raw: SECRET },
    user: { email: SECRET },
    breadcrumbs: [
      {
        category: 'fetch',
        message: SECRET,
        data: { nested: [{ password: SECRET }] },
      },
    ],
  }
}

function transactionEvent() {
  return {
    ...errorEvent(),
    type: 'transaction',
    transaction: SECRET,
    start_timestamp: 100,
    timestamp: 101,
    contexts: {
      trace: {
        trace_id: 'b'.repeat(32),
        span_id: 'c'.repeat(16),
        op: 'http.server',
        status: 'ok',
        data: { raw: SECRET },
      },
    },
    spans: [
      {
        trace_id: 'b'.repeat(32),
        span_id: 'd'.repeat(16),
        op: 'http.client',
        description: SECRET,
        data: { raw: SECRET },
      },
    ],
  }
}

function loadConfig(runtime: string, enabled = true) {
  jest.resetModules()
  const send = jest
    .fn<(envelope: unknown) => Promise<Record<string, never>>>()
    .mockResolvedValue({})
  const flush = jest
    .fn<(timeout?: number) => Promise<boolean>>()
    .mockResolvedValue(true)
  const transport = { send, flush }
  const init = jest.fn<(options: Record<string, unknown>) => void>()
  const replayIntegration = jest
    .fn<() => { name: string }>()
    .mockReturnValue({ name: 'Replay' })
  jest.doMock('@sentry/nextjs', () => ({ init, replayIntegration }))
  if (enabled) {
    process.env.SENTRY_DSN = DSN
    process.env.NEXT_PUBLIC_SENTRY_DSN = DSN
  } else {
    delete process.env.SENTRY_DSN
    delete process.env.NEXT_PUBLIC_SENTRY_DSN
  }
  require(`../../sentry.${runtime}.config`)
  return {
    options: init.mock.calls[0]?.[0] ?? {},
    init,
    transport,
    send,
    flush,
    replayIntegration,
  }
}

afterEach(() => {
  delete process.env.SENTRY_DSN
  delete process.env.NEXT_PUBLIC_SENTRY_DSN
  jest.resetModules()
})

describe.each(['client', 'server', 'edge'])(
  '%s optional SDK privacy contract',
  (runtime) => {
    test.each(['beforeSend', 'beforeSendTransaction'])(
      '%s withholds sensitive data and keeps diagnostics',
      (callback) => {
        const { options } = loadConfig(runtime)
        const event =
          callback === 'beforeSend' ? errorEvent() : transactionEvent()
        const sanitize = options[callback] as
          ((input: unknown) => unknown) | undefined
        const result = sanitize ? sanitize(event) : event
        expect(result).not.toBeNull()
        expect(JSON.stringify(result)).not.toContain(SECRET)
        expect(JSON.stringify(event)).toContain(SECRET)
        expect(result).toHaveProperty('event_id', 'a'.repeat(32))
        if (callback === 'beforeSend')
          expect(result).toHaveProperty('exception.values.0.type', 'TypeError')
        else
          expect(result).toHaveProperty(
            'contexts.trace.trace_id',
            'b'.repeat(32),
          )
      },
    )

    test('final transport re-sanitizes headers/items and withholds unqualified channels', async () => {
      const { options, transport, send, flush } = loadConfig(runtime)
      expect(typeof options.integrations).toBe('function')
      const integrations = (
        options.integrations as (defaults: unknown[]) => Array<{
          name: string
          setup?: (client: {
            getTransport: () => typeof transport
            getOptions: () => { enabled?: boolean }
          }) => void
        }>
      )([{ name: 'DefaultControl' }])
      expect(integrations[1]).toEqual({ name: 'DefaultControl' })
      integrations[0].setup?.({
        getTransport: () => transport,
        getOptions: () => ({}),
      })
      const envelope = [
        { event_id: 'a'.repeat(32), trace: { raw: SECRET } },
        [
          [{ type: 'event', filename: SECRET }, errorEvent()],
          [{ type: 'attachment' }, SECRET],
          [{ type: 'session' }, { user: SECRET }],
          [{ type: 'replay_recording' }, SECRET],
          [{ type: 'log' }, { body: SECRET }],
        ],
      ]
      await transport.send(envelope)
      expect(send).toHaveBeenCalledTimes(1)
      expect(JSON.stringify(send.mock.calls[0][0])).not.toContain(SECRET)
      expect((send.mock.calls[0][0] as unknown[][])[1]).toHaveLength(1)
      await expect(Promise.resolve(transport.flush(2))).resolves.toBe(true)
      expect(flush).toHaveBeenCalledWith(2)
    })

    test('empty DSNs leave initialization disabled', () => {
      const { init, replayIntegration } = loadConfig(runtime, false)
      expect(init).not.toHaveBeenCalled()
      expect(replayIntegration).not.toHaveBeenCalled()
    })
  },
)

test('client does not collect unqualified replay or log data', () => {
  const { options, replayIntegration } = loadConfig('client')
  expect(options.replaysSessionSampleRate).toBe(0)
  expect(options.replaysOnErrorSampleRate).toBe(0)
  expect(options.enableLogs).toBe(false)
  expect(options.enableMetrics).toBe(false)
  expect(options.sendClientReports).toBe(false)
  expect(replayIntegration).not.toHaveBeenCalled()
})

describe('structured diagnostic policy', () => {
  function policy() {
    return require('../../lib/telemetry-privacy') as typeof import('../../lib/telemetry-privacy')
  }

  test('reconstructs useful source locations without functions, input paths or locals', () => {
    const { sanitizeTelemetryEvent } = policy()
    const original = errorEvent()
    const result = sanitizeTelemetryEvent(original)
    expect(result).toHaveProperty('exception.values.0.stacktrace.frames.0', {
      filename: 'app:///services/api.ts',
      lineno: 12,
      in_app: true,
    })
    expect(JSON.stringify(result)).not.toContain(SECRET)
    expect(JSON.stringify(original)).toContain(SECRET)
    expect(sanitizeTelemetryEvent(result)).toEqual(result)
  })

  test('unknown frames, fields and non-finite metadata are withheld', () => {
    const { sanitizeTelemetryEvent } = policy()
    const event = {
      ...errorEvent(),
      timestamp: Infinity,
      environment: SECRET,
      release: SECRET,
      contexts: { private: { value: SECRET } },
      tags: { private: SECRET },
      exception: {
        values: [
          {
            type: SECRET,
            value: SECRET,
            stacktrace: {
              frames: [
                { filename: SECRET, lineno: 12 },
                { filename: 'services/api.ts', lineno: Infinity },
              ],
            },
          },
        ],
      },
    }
    const result = sanitizeTelemetryEvent(event)
    expect(result).toHaveProperty('exception.values.0.type', 'Error')
    expect(JSON.stringify(result)).not.toContain(SECRET)
    expect(result).not.toHaveProperty('timestamp')
    expect(result).not.toHaveProperty('exception.values.0.stacktrace')
  })

  test('untrusted prototypes and accessors cannot supply raw content', () => {
    const { sanitizeTelemetryEvent } = policy()
    let calls = 0
    const input = Object.defineProperty({}, 'message', {
      get() {
        calls += 1
        return SECRET
      },
    })
    expect(sanitizeTelemetryEvent(input)).toBeNull()
    expect(calls).toBe(0)
    class Untrusted {
      message = SECRET
    }
    expect(sanitizeTelemetryEvent(new Untrusted())).toBeNull()
    expect(sanitizeTelemetryEvent({ unknown: SECRET })).toBeNull()
    expect(sanitizeTelemetryEvent({ type: SECRET, message: SECRET })).toBeNull()
  })

  test('cyclic unknown data is ignored without mutating input', () => {
    const { sanitizeTelemetryEvent } = policy()
    const event = { ...errorEvent(), extra: {} as Record<string, unknown> }
    event.extra.cycle = event
    const result = sanitizeTelemetryEvent(event)
    expect(result).not.toBeNull()
    expect(JSON.stringify(result)).not.toContain(SECRET)
    expect(event.extra.cycle).toBe(event)
  })

  test.each([
    'attachment',
    'session',
    'sessions',
    'client_report',
    'replay_event',
    'replay_recording',
    'profile',
    'log',
    'trace_metric',
    'metric_buckets',
    'check_in',
    'span',
    'feedback',
    'unknown',
  ])('%s cannot bypass event policy', (kind) => {
    expect(
      policy().sanitizeTelemetryEnvelope([
        {},
        [[{ type: kind }, { message: SECRET }]],
      ]),
    ).toBeNull()
  })

  test('malformed bytes, boxed types, subtype hooks and mismatched payloads are dropped', () => {
    const { sanitizeTelemetryEnvelope } = policy()
    let calls = 0
    const kind = Object(SECRET)
    kind.toString = () => {
      calls += 1
      return 'event'
    }
    const malformed = [
      {},
      [
        [{ type: kind }, errorEvent()],
        [{ type: 'event' }, new Uint8Array([1, 2])],
        [{ type: 'transaction' }, errorEvent()],
        [{ type: 'event' }, transactionEvent()],
      ],
    ]
    expect(sanitizeTelemetryEnvelope(malformed)).toBeNull()
    expect(calls).toBe(0)
    expect(sanitizeTelemetryEnvelope(Object.freeze([{}, []]))).toBeNull()
  })

  test('integration guards early, is idempotent, preserves flush/options and drops malformed sends', async () => {
    const { confidentialTelemetryIntegration } = policy()
    const send = jest
      .fn<(value: unknown) => Promise<Record<string, never>>>()
      .mockResolvedValue({})
    const flush = jest.fn<() => Promise<boolean>>().mockResolvedValue(true)
    const transport = { send, flush }
    const options: { enabled?: boolean } = {}
    const client = { getTransport: () => transport, getOptions: () => options }
    const integration = confidentialTelemetryIntegration()
    integration.beforeSetup?.(client)
    const guarded = transport.send
    integration.setup?.(client)
    expect(transport.send).toBe(guarded)
    expect(transport.flush).toBe(flush)
    expect(options).toEqual({})
    await transport.send([
      { event_id: SECRET },
      [[{ type: 'attachment' }, SECRET]],
    ])
    expect(send).not.toHaveBeenCalled()
    await transport.send([{}, [[{ type: 'event' }, errorEvent()]]])
    expect(send).toHaveBeenCalledTimes(1)
    expect(JSON.stringify(send.mock.calls[0][0])).not.toContain(SECRET)
  })

  test('an unguardable transport leaves client delivery disabled', () => {
    const { confidentialTelemetryIntegration } = policy()
    const send = jest
      .fn<(value: unknown) => Promise<Record<string, never>>>()
      .mockResolvedValue({})
    const transport = Object.freeze({
      send,
      flush: () => Promise.resolve(true),
    })
    const options = { enabled: true }
    confidentialTelemetryIntegration().beforeSetup?.({
      getTransport: () => transport,
      getOptions: () => options,
    })
    expect(options.enabled).toBe(false)
    expect(send).not.toHaveBeenCalled()
  })

  test('a previously disabled client stays disabled after a successful guard install', () => {
    const options = { enabled: false }
    const transport = {
      send: () => Promise.resolve({}),
      flush: () => Promise.resolve(true),
    }
    policy()
      .confidentialTelemetryIntegration()
      .beforeSetup?.({
        getTransport: () => transport,
        getOptions: () => options,
      })
    expect(options.enabled).toBe(false)
  })

  test('tunnel routing uses the configured public DSN rather than untrusted envelope headers', async () => {
    const send = jest
      .fn<(value: unknown) => Promise<Record<string, never>>>()
      .mockResolvedValue({})
    const transport = { send, flush: () => Promise.resolve(true) }
    const options = { enabled: true, tunnel: '/monitoring' }
    const client = {
      getTransport: () => transport,
      getOptions: () => options,
      getDsn: () => ({
        protocol: 'https',
        publicKey: '0123456789abcdef0123456789abcdef',
        host: 'sentry.invalid',
        projectId: '1',
        pass: SECRET,
      }),
    }
    policy().confidentialTelemetryIntegration().beforeSetup?.(client)
    await transport.send([
      { dsn: SECRET, trace: { user: SECRET } },
      [[{ type: 'event' }, errorEvent()]],
    ])
    expect((send.mock.calls[0][0] as unknown[])[0]).toEqual({ dsn: DSN })
    expect(JSON.stringify(send.mock.calls[0][0])).not.toContain(SECRET)
    expect(options).toEqual({ enabled: true, tunnel: '/monitoring' })
  })

  test.each(['callback', 'final-send'])(
    'a changing Proxy descriptor cannot launder a handled value through %s',
    async (boundary) => {
      let reads = 0
      const mechanism = new Proxy(
        {},
        {
          getOwnPropertyDescriptor(_target, key) {
            if (key !== 'handled') return undefined
            reads += 1
            return {
              configurable: true,
              enumerable: true,
              value: reads === 1 ? true : SECRET,
            }
          },
        },
      )
      const event = {
        ...errorEvent(),
        exception: {
          values: [{ type: 'TypeError', value: SECRET, mechanism }],
        },
      }
      let result: unknown
      if (boundary === 'callback') {
        result = policy().sanitizeTelemetryEvent(event)
      } else {
        const send = jest
          .fn<(value: unknown) => Promise<Record<string, never>>>()
          .mockResolvedValue({})
        const transport = { send, flush: () => Promise.resolve(true) }
        policy()
          .confidentialTelemetryIntegration()
          .beforeSetup?.({
            getTransport: () => transport,
            getOptions: () => ({}),
          })
        await transport.send([{}, [[{ type: 'event' }, event]]])
        expect(send).toHaveBeenCalledTimes(1)
        result = send.mock.calls[0][0]
      }
      expect(JSON.stringify(result)).not.toContain(SECRET)
      expect(reads).toBe(1)
    },
  )
})
