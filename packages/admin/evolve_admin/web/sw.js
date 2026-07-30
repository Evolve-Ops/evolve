/* Evolve PWA service worker — Phase 1.1.A.
 *
 * Responsibilities (spec docs/spec-pwa-2026-05-18.md §3, §5.2):
 *   1. Offline shell cache. Cache the SPA shell + modules lazily on fetch;
 *      network-first with cache fallback so the app opens to last-known
 *      UI when the mini is unreachable.
 *   2. Version banner. On `updatefound` the client surfaces an in-app
 *      toast; clicking it posts SKIP_WAITING here and reloads.
 *   3. Push handler stub. Scaffolded but no-op until Phase 1.2 wires
 *      VAPID + dispatcher.
 *   4. Notification-click stub. Same — scaffolded for Phase 1.2.
 *
 * Vanilla JS, no Workbox.
 *
 * ── Deploy-safety contract (the reason this handler is written the way
 *    it is — see PR "[META:ui] PWA service-worker deploy-safety") ───────
 *
 * A fleet promote replaces the admin static assets (index.html + the
 * extracted js/{core,pages,widgets} + css modules). An operator with an
 * open or cached admin session must NEVER be locked out by the SW when
 * that happens. Two invariants enforce this:
 *
 *   A. FAIL-OPEN. The fetch handler must never hand `respondWith` a
 *      value that blanks the page — no `undefined`, no `Response.error()`
 *      for a navigation. If we can't satisfy a request from network and
 *      have nothing safe in cache, we DON'T call `respondWith` at all and
 *      let the browser do its native fetch/error. A blank shell that only
 *      "clear site data" fixes is the failure mode this guards against.
 *
 *   B. EVERY BUILD GETS A FRESH CACHE. `CACHE_VERSION` is stamped per
 *      build by the server (it substitutes `__PWA_CACHE_VERSION__` in the
 *      served sw.js with a hash of all SPA assets). `activate` then
 *      deletes EVERY cache whose name differs from the current build's —
 *      so a deploy can never serve a previous build's cached shell that
 *      references assets the new release renamed or removed. Paired with
 *      `skipWaiting()` + `clients.claim()`, a poisoned browser self-heals
 *      on the very next reload, with no manual cache-clearing.
 *
 * If the server didn't substitute the token (unit tests, file://, an
 * older server), the literal `__PWA_CACHE_VERSION__` is still a valid,
 * stable cache name — the SW degrades to a single fixed cache rather
 * than breaking.
 */

const CACHE_VERSION = '__PWA_CACHE_VERSION__';  // server stamps this per-build (hash of all SPA assets); see invariant B above

// Small, fixed-size assets only — the SPA shell (``/``) is ~1.9MB
// today and gets cached lazily by the network-first fetch handler on
// the very first navigation. Precaching the heavy shell across every
// fresh context (browser smoke matrix opens dozens of them per run)
// overwhelmed Flask's dev server on CI Linux + webkit + firefox; the
// fetch handler is enough since the offline-fallback shell will be
// present after any successful visit.
const SHELL_ASSETS = [
  '/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-512-maskable.png',
  '/static/icons/apple-touch-icon-180.png',
  '/favicon.ico',
  '/favicon-16x16.png',
  '/favicon-32x32.png',
];

self.addEventListener('install', (event) => {
  // Take over immediately rather than waiting for every controlled tab to
  // close (invariant B). Without skipWaiting, a browser that landed on a
  // poisoned build would keep the old SW controlling — and a BLANK tab
  // can't click the "New version available" toast to trigger the swap, so
  // it would stay broken until the operator cleared site data. skipWaiting
  // + clients.claim (below) means the fixed/new SW activates on the very
  // next reload and the page self-heals.
  self.skipWaiting();
  // cache.addAll is all-or-nothing — a single 404 / network blip would
  // mark the SW install as failed, leaving the user with no SW at all.
  // Loop with individual put() so partial precache succeeds; the fetch
  // handler will fill missing entries on the first real visit anyway.
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      Promise.all(
        SHELL_ASSETS.map((url) =>
          fetch(url, { credentials: 'same-origin' })
            .then((resp) => (resp && resp.ok ? cache.put(url, resp) : null))
            .catch(() => null),
        ),
      ),
    ),
  );
});

self.addEventListener('activate', (event) => {
  // Delete EVERY cache that isn't this build's (invariant B). Because the
  // server stamps CACHE_VERSION with a per-build hash, this fires on every
  // asset-changing deploy and flushes the prior build's shell + modules —
  // so ONCE THIS SW HAS ACTIVATED, a navigation is never served a prior
  // build's cached shell. (The one remaining window is a pure-offline
  // reload before the new SW can install — the network is down, so the new
  // build's /sw.js can't be fetched; the old SW may serve its old cached
  // shell. That's the fail-open path: recoverable on the next reload once
  // the network returns and this activate runs, never a permanent blank.)
  // clients.claim() makes the freshly-activated SW control already-open
  // tabs so they pick up the new behaviour on this load, not the next.
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))),
    ).then(() => self.clients.claim()),
  );
});

// Phase 4 (spec §8): broadcast a network-down hint to every controlled
// client when a fetch reject (true network-level failure — DNS resolution
// fail, TCP refuse, abort, etc.) — NOT 5xx, which means the server IS
// reachable and just unhappy. The client renders the tunnel-down overlay
// on receipt; rendering escalation (toast → overlay) is the client's
// responsibility, the SW just emits the event.
function _broadcastNetworkDown(reason) {
  // ``clients.matchAll`` here covers every tab/PWA window the SW
  // controls. We intentionally don't await this — fire-and-forget so
  // the failing fetch's reject path stays fast.
  self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then((all) => {
      for (const c of all) {
        try { c.postMessage({ type: 'network-down', reason }); } catch (_) { /* ignore */ }
      }
    })
    .catch(() => { /* ignore — best-effort */ });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Only intercept same-origin GETs. Cross-origin (e.g. fonts.googleapis,
  // chart.js CDN) and writes (POST/PATCH/DELETE) go straight to the network
  // — the SW must never cache or stall a mutating request.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Don't cache API responses — they're live data, and stale JSON would
  // confuse the UI more than a clean offline-fallback would help.
  // The client's own /api/health heartbeat still drives the down-state
  // detector for API failures; the SW signal here covers shell asset
  // fetches and SPA navigations.
  if (url.pathname.startsWith('/api/')) return;

  const isNavigate = req.mode === 'navigate';

  event.respondWith(
    fetch(req)
      .then((response) => {
        // Network won — always the freshest asset, so online a deploy's
        // new shell/modules win immediately (network-first). Cache a copy
        // for the offline fallback. For a navigation also store a copy
        // under the stable ``/`` key so there is always exactly ONE
        // coherent offline shell, regardless of which route the operator
        // last loaded. (The original lockout came partly from ``/`` being
        // empty — the shell was only ever cached under the exact navigated
        // URL — so the navigate fallback resolved ``undefined``.)
        if (response && response.ok) {
          const forReq = response.clone();
          const forShell = isNavigate ? response.clone() : null;
          caches.open(CACHE_VERSION).then((cache) => {
            cache.put(req, forReq);
            if (forShell) cache.put('/', forShell);
          }).catch(() => { /* cache write is best-effort */ });
        }
        return response;
      })
      .catch((err) => {
        // True network-level failure (server restarting mid-promote, tunnel
        // down, etc.). Post network-down so any open client can render the
        // tunnel overlay even if its own heartbeat hasn't ticked yet —
        // done BEFORE the cache fallback so the signal goes out promptly.
        _broadcastNetworkDown((err && err.name) || 'fetch-failed');
        // FAIL-OPEN (invariant A): never resolve ``respondWith`` with a
        // value that blanks the page. Serve from cache when we can; for a
        // navigation fall back to the stable shell. When we have NOTHING
        // cached, re-issue the network request and return its result — if
        // that rejects too, ``respondWith`` rejects and the browser shows
        // its native (recoverable) error page, NOT a permanent blank from
        // ``undefined`` / ``Response.error()``.
        return caches.match(req).then((cached) => {
          if (cached) return cached;
          if (isNavigate) {
            return caches.match('/').then((shell) => shell || fetch(req));
          }
          return fetch(req);
        });
      }),
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// ── Phase 1.2 — push + notification handlers ───────────────────────────────
// Spec: docs/spec-pwa-2026-05-18.md §6.6.
//
// Payload shape (matches admin dispatcher's push_sender.fanout_pwa_push):
//   { title, body, url, event_key, severity }
//
// All five fields are optional from a defensive standpoint; we fall
// back to "Evolve" / empty body / "/" / no tag if any are missing so a
// hand-crafted push from a test rig still surfaces something.

self.addEventListener('push', (event) => {
  // event.data is null when the push service delivers a "tickle" with
  // no payload (Firefox / Mozilla do this when our payload exceeds the
  // push service's size cap). Render a generic notification so the
  // operator still knows *something* fired.
  let data = {};
  if (event && event.data) {
    try {
      data = event.data.json();
    } catch (_e) {
      // Non-JSON payload (e.g. dispatcher misconfigured) — fall through
      // with the empty default so the notification still shows.
      data = { title: event.data.text() || 'Evolve' };
    }
  }
  const title = (data && data.title) ? String(data.title) : 'Evolve';
  const body = (data && data.body) ? String(data.body) : '';
  const url = (data && data.url) ? String(data.url) : '/';
  const eventKey = data && data.event_key ? String(data.event_key) : undefined;

  const opts = {
    body: body,
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    data: { url: url, event_key: eventKey },
  };
  // Collapse repeats of the same event type so the user doesn't get a
  // stack of identical "gateway down" notifications. Only set when the
  // dispatcher gave us a key — otherwise leave the field absent and
  // let the OS show every notification.
  if (eventKey) opts.tag = eventKey;

  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : '/';

  // Resolve relative to the SW's origin so deep-links never accidentally
  // open a different host (e.g. dispatcher misconfiguration shipping a
  // full URL from a stale pod).
  const absolute = new URL(target, self.location.origin).href;

  event.waitUntil((async () => {
    const clientList = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    // Reuse an open admin-UI window when one exists — focus + navigate
    // beats opening a fresh window the user has to alt-tab to.
    for (const client of clientList) {
      if (client.url && client.url.startsWith(self.location.origin)) {
        try {
          await client.focus();
        } catch (_e) { /* focus can fail in some browsers; navigate anyway */ }
        if ('navigate' in client) {
          try {
            await client.navigate(absolute);
          } catch (_e) { /* cross-origin or detached — fall through to openWindow */ }
        }
        return;
      }
    }
    await self.clients.openWindow(absolute);
  })());
});
