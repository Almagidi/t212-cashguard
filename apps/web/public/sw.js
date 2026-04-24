const CACHE_NAME = 'cashguard-static-v3'
const PRECACHE_URLS = ['/manifest.json', '/icon.svg', '/icon-192.png', '/icon-512.png']
const CACHEABLE_ASSET = /\.(png|svg|ico|woff2|json)$/i

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)),
  )
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) =>
      Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME && cacheName.startsWith('cashguard-')) {
            return caches.delete(cacheName)
          }
          return Promise.resolve(false)
        }),
      ),
    ),
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const { request } = event

  if (request.method !== 'GET') return

  const url = new URL(request.url)
  const isSameOrigin = url.origin === self.location.origin

  if (!isSameOrigin) return
  if (url.pathname.startsWith('/_next/')) return
  if (url.pathname.startsWith('/api/')) return
  if (url.pathname.startsWith('/v1/')) return
  if (!CACHEABLE_ASSET.test(url.pathname)) return

  event.respondWith(
    caches.match(request).then((cachedResponse) => {
      if (cachedResponse) return cachedResponse

      return fetch(request).then((response) => {
        if (!response.ok) return response

        const responseClone = response.clone()
        void caches.open(CACHE_NAME).then((cache) => cache.put(request, responseClone))
        return response
      })
    }),
  )
})
