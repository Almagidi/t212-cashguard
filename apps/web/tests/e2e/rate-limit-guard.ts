const DEFAULT_REQUEST_INTERVAL_MS = 150

interface StatusResponse {
  status(): number
}

interface FetchingRequestContext<Response extends StatusResponse> {
  fetch(
    target: string | { method(): string; url(): string },
    options?: { method?: string; [key: string]: unknown },
  ): Promise<Response>
}

export function assertNoUnexpectedRateLimit(status: number, method: string, pathname: string): void {
  if (status === 429) {
    throw new Error(`Unexpected HTTP 429 for ${method} ${pathname}`)
  }
}

export function createBrowserApiScheduler(intervalMs = DEFAULT_REQUEST_INTERVAL_MS): () => Promise<void> {
  let queue = Promise.resolve()
  let nextRequestAt = 0

  return () => {
    const scheduled = queue.then(async () => {
      const delay = Math.max(0, nextRequestAt - Date.now())
      if (delay > 0) {
        await new Promise((resolve) => setTimeout(resolve, delay))
      }
      nextRequestAt = Date.now() + intervalMs
    })
    queue = scheduled.catch(() => undefined)
    return scheduled
  }
}

export const waitForBrowserApiSlot = createBrowserApiScheduler()

export async function guardedApiFetch<Response extends StatusResponse>(
  request: FetchingRequestContext<Response>,
  target: string | { method(): string; url(): string },
  options?: { method?: string; [key: string]: unknown },
): Promise<Response> {
  const method = options?.method ?? (typeof target === 'string' ? 'GET' : target.method())
  const url = typeof target === 'string' ? target : target.url()

  await waitForBrowserApiSlot()
  const response = await request.fetch(target, options)
  assertNoUnexpectedRateLimit(response.status(), method, new URL(url).pathname)
  return response
}
