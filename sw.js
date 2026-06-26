/**
 * CaseOS Service Worker — cache offline do shell
 * Estratégia: cache-first para assets estáticos, network-first para API
 */
const CACHE = 'caseos-shell-v2';

const SHELL_ASSETS = [
  '/',
  '/login',
  '/dashboard',
  '/novo-relato',
  '/review-studio',
  '/assets/favicon.svg',
  '/assets/logo-caseos-light.png',
  '/assets/logo-caseos.png',
  '/assets/pixel-icons.svg',
  '/manifest.json',
];

// ── Install: pré-cacheia o shell ──────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(SHELL_ASSETS))
      .catch(() => {})   // falha silenciosa — não bloqueia instalação
  );
  self.skipWaiting();
});

// ── Activate: remove caches antigos ──────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch ─────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const url = event.request.url;

  // Sempre busca na rede: API, autenticação, formulários
  if (
    url.includes('onrender.com') ||
    url.includes('/auth/') ||
    url.includes('/api/') ||
    event.request.method !== 'GET'
  ) {
    return;
  }

  // Requests de navegação: network-first com fallback de cache
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .catch(() => caches.match('/'))
    );
    return;
  }

  // Assets estáticos: cache-first
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        // Só cacheia respostas válidas de mesma origem
        if (
          response.ok &&
          response.type === 'basic' &&
          event.request.url.startsWith(self.location.origin)
        ) {
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, clone));
        }
        return response;
      });
    })
  );
});
