/* Service worker: offline shell only.

   The version rides on this script's own ?v= (index.html registers
   '/sw.js' + the ?v= off its app.js tag), so one bump in index.html rolls the
   cache name, the precache URLs and the SW byte-content together. Cache keys are
   query-sensitive, so the precached URLs must be the exact ones the page asks for. */
const V = new URLSearchParams(self.location.search).get('v') || '0';
const CACHE = `laser-shell-${V}`;
const SHELL = [
  '/',
  `/static/style.css?v=${V}`,
  `/static/app.js?v=${V}`,
  `/static/wall.js?v=${V}`,
  `/static/i18n/en.json?v=${V}`,
  `/static/i18n/ar.json?v=${V}`,
  `/static/i18n/tr.json?v=${V}`,
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

/* Drop every older cache, then take over open tabs immediately so an update
   lands without the user hunting for a "reload to update" prompt. */
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);

  /* Hands off entirely — no respondWith — for anything that is not a same-origin
     GET, and for the API. /api/* is cookie-authenticated factory data on personal
     phones: it must never reach disk. This guard runs before the navigation branch
     on purpose, because downloadReport() uses window.open, so an xlsx/pdf export
     arrives as a NAVIGATION and would otherwise be answered with the cached shell. */
  if (req.method !== 'GET' || url.origin !== self.location.origin ||
      url.pathname.startsWith('/api/') || url.pathname === '/health') return;

  /* Network-first for the document. The shell is deliberately no-store so a phone
     can never run yesterday's script tags; cache-first here would undo that. The
     cached copy is a last resort for offline. */
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() =>
      caches.match('/').then(hit => hit || Response.error())));
    return;
  }

  /* Versioned static assets: answer from cache at once, but always revalidate in the
     background, which also fills the cache on a miss so icons and the manifest come
     along without being listed in SHELL. Plain cache-first would pin the bundle for
     good on any deploy that ships new bytes under an unchanged ?v= — the "worse than
     no service worker" failure, unfixable from the shop floor without clearing site
     data. This way a missed bump costs one reload. A ?v= mismatch is never a 404
     either: StaticFiles ignores the query. */
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(req).then(hit => {
      const net = fetch(req).then(res => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
        return res;
      });
      if (hit) net.catch(() => {});   // offline: the cached copy already answered
      return hit || net;
    }));
  }
});
