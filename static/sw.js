// Installable-PWA service worker.
//
// The server stamps every response `Cache-Control: no-cache` on purpose
// (see app/main.py's index() and _RevalidatingStaticFiles - assets aren't
// content-hashed), so this worker keeps its own versioned cache rather than
// trusting HTTP caching, and stays network-first: an online visit always
// gets the live file, and the cache only rescues an offline or slow-cold-
// start load. Bump CACHE_NAME on any deploy that changes a listed asset.
//
// request.mode === 'navigate' is always passed straight through - this is
// the one line that matters most, since it guarantees this worker never
// intercepts the Cloudflare Access login redirect chain.
const CACHE_NAME = 'memory-agent-shell-v1';
const SHELL_ASSETS = [
  '/static/app.css',
  '/static/app.js',
  '/static/view.js',
  '/static/vendor/marked.min.js',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (
    request.mode === 'navigate' ||
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/mcp/') ||
    url.pathname === '/healthz'
  ) {
    return;
  }
  if (!SHELL_ASSETS.includes(url.pathname)) {
    return;
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request))
  );
});
