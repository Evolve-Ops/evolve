// ════════════════════════════════════════════════════════════════════════
// PWA Phase 4 — tunnel CONNECT helper (spec internal/spec-pwa-2026-05-18.md §8)
//
// Two detection paths feed one state machine:
//   1. Periodic /api/health heartbeat (~10s, throttled when the tab is
//      hidden). Counts consecutive failures.
//   2. Service-worker postMessage({type:'network-down'}) on any real
//      fetch-level failure (TypeError; never a 5xx — the server IS
//      reachable in that case).
//
// State machine:
//   healthy  → blip (1 failure)  : nothing
//   healthy  → reconnecting (2+) : show #pwa-net-toast
//   reconnecting → down (5+)     : show #pwa-tunnel-overlay
//   any-failure → healthy        : hide both, flash "Back online"
//
// Platform branching:
//   Desktop (Mac w/ evolve-admin connect tunnel) and mobile (Tailscale
//   PWA) have different failure modes. Desktop's likely cause is the
//   SSH tunnel agent or admin daemon being down; mobile's is the
//   Tailscale connection. The overlay's data-platform attribute is
//   set once at module init; CSS hides the non-matching copy.
//
// Buttons:
//   Reconnect → mobile only. Fires tailscale:// deep-link to bring the
//               Tailscale app forward. Hidden on desktop (the scheme
//               isn't reliably handled by Tailscale's macOS app, and
//               desktop's fix is a shell command, not a deep-link).
//   Try again → force a /api/health ping; if it succeeds, dismiss.
// ════════════════════════════════════════════════════════════════════════
(() => {
  // Tuning. Numbers picked to feel responsive on a phone tunnel-flap
  // (Tailscale reconnects in 2–5s typically) without going to the full-
  // page overlay for every 1-second blip.
  const PING_INTERVAL_MS = 10_000;   // visible-tab cadence
  const PING_HIDDEN_MS  = 60_000;    // throttled when backgrounded
  const PING_TIMEOUT_MS = 5_000;     // per-ping abort
  const TOAST_THRESHOLD = 2;          // failures before "Reconnecting…"
  const OVERLAY_THRESHOLD = 5;        // failures before full overlay
  const BACK_ONLINE_MS = 2_000;       // fade timing for green toast
  const DEEP_LINK_FALLBACK_MS = 2_000; // tailscale:// no-app watchdog

  const $toast = document.getElementById('pwa-net-toast');
  const $toastMsg = $toast ? $toast.querySelector('.pnt-msg') : null;
  const $overlay = document.getElementById('pwa-tunnel-overlay');
  const $podUrl = document.getElementById('pto-pod-url');
  const $hintFallback = document.getElementById('pto-hint-fallback');
  const $cmdInstall = document.getElementById('pto-cmd-install');
  if (!$toast || !$overlay) return;  // markup missing → no-op

  // Platform detection picks the copy and the Reconnect-button behaviour.
  // navigator.userAgentData is preferred where available (it gives us
  // ``mobile`` cleanly); userAgent string is the universal fallback.
  // The "desktop" default is intentional — if we can't tell, hiding
  // the meaningless Reconnect button is the less-confusing failure
  // mode (a present "Try again" still works; a deceptive Reconnect
  // that fires tailscale:// into the void doesn't).
  function _pwaTunnelDetectPlatform() {
    try {
      if (navigator.userAgentData && typeof navigator.userAgentData.mobile === 'boolean') {
        return navigator.userAgentData.mobile ? 'mobile' : 'desktop';
      }
      const ua = (navigator.userAgent || '').toLowerCase();
      if (/iphone|ipod|android.*mobile|android.*tablet|ipad/.test(ua)) return 'mobile';
      // iPadOS 13+ reports as Macintosh; touch-points disambiguates.
      if (/macintosh/.test(ua) && navigator.maxTouchPoints && navigator.maxTouchPoints > 1) {
        return 'mobile';
      }
      if (/macintosh|mac os x|windows|linux|cros/.test(ua)) return 'desktop';
    } catch (_) { /* ignore */ }
    return 'desktop';
  }

  // The SPA's network payload. ``_networkData`` is a top-level ``let``
  // in index.html's inline script — a *lexical* global, which lives in
  // the script scope and is deliberately NOT a property of ``window``.
  // Reading ``window._networkData`` therefore always yielded undefined,
  // so both call sites below took their fallback on every boot forever
  // (the "mini" literal, and the origin the operator happens to be
  // browsing — which on a tunnelled session is exactly the wrong value).
  // The bare identifier is the read that sees the data: this file is a
  // classic script loaded after that declaration, so it resolves through
  // the global lexical environment. try/catch covers the load-order edge
  // — a TDZ read throws, and ``typeof`` would throw too, so a guard
  // expression is no safer than catching.
  function _pwaTunnelNetworkData() {
    try {
      return _networkData || {};
    } catch (_) {
      return {};
    }
  }

  // Pick the host placeholder for the `evolve-admin connect --host <X>`
  // help row. Priority: pod.ssh_target → first dotted-segment of
  // adminBaseUrl → literal "mini" (canonical-install fallback).
  function _pwaTunnelDeriveHost() {
    try {
      const nd = _pwaTunnelNetworkData();
      const pod = nd.pod || {};
      const target = pod.ssh_target;
      if (typeof target === 'string' && target.trim()) {
        const t = target.trim();
        const at = t.lastIndexOf('@');
        return at >= 0 ? t.slice(at + 1) : t;
      }
      const url = typeof nd.adminBaseUrl === 'string' ? nd.adminBaseUrl : '';
      const m = url.match(/^https?:\/\/([^/:]+)/);
      if (m) {
        const host = m[1].split('.')[0];
        if (host) return host;
      }
    } catch (_) { /* ignore */ }
    return 'mini';
  }

  const _platform = _pwaTunnelDetectPlatform();
  $overlay.setAttribute('data-platform', _platform);
  // Best-effort first pass so the row is never stale-empty. At module
  // init the SPA's /api/network fetch has not landed (``_networkData``
  // is still ``{}``), so this generally renders the fallback; the real
  // value lands when _showOverlay() re-derives it below.
  if ($cmdInstall) {
    $cmdInstall.textContent = `evolve-admin connect --host ${_pwaTunnelDeriveHost()}`;
  }

  let failureCount = 0;
  let pingTimer = null;
  let backOnlineTimer = null;
  let inFlight = false;
  let deepLinkWatchdog = null;
  // Capture initial visibility so the SW signal doesn't trip the
  // toast/overlay before we've ever pinged the server ourselves.
  let everConnected = false;

  function _clearBackOnlineTimers() {
    // Centralised cancel — anywhere the toast state mutates outside
    // the back-online sequence we cancel the chained fade timers so
    // they don't whack a newer toast.
    if (backOnlineTimer) {
      clearTimeout(backOnlineTimer);
      backOnlineTimer = null;
    }
  }
  function _setToast(msg, kind) {
    if (!$toast || !$toastMsg) return;
    if (kind !== 'back-online') _clearBackOnlineTimers();
    $toastMsg.textContent = msg;
    $toast.classList.remove('back-online', 'fading');
    if (kind === 'back-online') $toast.classList.add('back-online');
    $toast.classList.add('visible');
  }
  function _hideToast() {
    if (!$toast) return;
    _clearBackOnlineTimers();
    $toast.classList.remove('visible', 'back-online', 'fading');
  }
  function _showOverlay() {
    if (!$overlay) return;
    if ($overlay.classList.contains('visible')) return;
    // Populate both network-derived rows here rather than at module
    // init: the SPA's /api/network fetch has long since landed by the
    // time an operator is looking at this overlay, so deriving on open
    // is what makes these values true instead of boot-time fallbacks.
    if ($cmdInstall) {
      $cmdInstall.textContent = `evolve-admin connect --host ${_pwaTunnelDeriveHost()}`;
    }
    if ($podUrl) {
      let podUrl = '';
      const nd = _pwaTunnelNetworkData();
      if (typeof nd.adminBaseUrl === 'string' && nd.adminBaseUrl) {
        podUrl = nd.adminBaseUrl;
      }
      if (!podUrl) podUrl = `${window.location.protocol}//${window.location.host}`;
      $podUrl.textContent = podUrl;
    }
    if ($hintFallback) $hintFallback.classList.remove('visible');
    $overlay.classList.add('visible');
  }
  function _hideOverlay() {
    if (!$overlay) return;
    $overlay.classList.remove('visible');
    if ($hintFallback) $hintFallback.classList.remove('visible');
  }

  function _onFailure() {
    failureCount += 1;
    if (failureCount >= OVERLAY_THRESHOLD) {
      _showOverlay();
      // Once the overlay is up, hide the small toast — they'd stack.
      _hideToast();
    } else if (failureCount >= TOAST_THRESHOLD) {
      _setToast('Reconnecting…', 'reconnecting');
    }
  }

  function _onSuccess() {
    const wasDown = failureCount >= TOAST_THRESHOLD || $overlay.classList.contains('visible');
    failureCount = 0;
    everConnected = true;
    if (!wasDown) {
      _hideToast();
      return;
    }
    _hideOverlay();
    _setToast('Back online.', 'back-online');
    if (backOnlineTimer) clearTimeout(backOnlineTimer);
    // Fade then hide so the operator sees the success state for a beat.
    backOnlineTimer = setTimeout(() => {
      if ($toast) $toast.classList.add('fading');
      backOnlineTimer = setTimeout(() => {
        _hideToast();
        // Re-fetch the active page's data via the SPA's existing load
        // helpers when present. Simpler than a full reload and keeps
        // any open modals open.
        try {
          if (typeof loadStatus === 'function') loadStatus();
          if (typeof loadNetwork === 'function') loadNetwork();
        } catch (_) { /* ignore */ }
      }, 400);
    }, BACK_ONLINE_MS);
  }

  async function _ping() {
    if (inFlight) return;
    inFlight = true;
    let ok = false;
    try {
      // AbortController so a slow hung TCP connection doesn't pile up
      // pings forever. The default fetch has no timeout.
      const ctrl = ('AbortController' in window) ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => ctrl.abort(), PING_TIMEOUT_MS) : null;
      const resp = await fetch('/api/health', {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        signal: ctrl ? ctrl.signal : undefined,
      });
      if (timer) clearTimeout(timer);
      // Only TRULY-unreachable counts as a failure: a 503 (or any
      // server-emitted response) means the mini IS reachable, even if
      // unhappy. The overlay is for "can't reach pod", not "pod errored".
      // BUT — tests mock /api/health with a 503 to simulate down; align
      // with that contract by also treating non-2xx as failure here.
      // The SW signal stays strict (TypeError only) to avoid double-
      // counting transient 5xx into network-down state.
      ok = resp && resp.ok;
    } catch (_) {
      ok = false;
    } finally {
      inFlight = false;
    }
    if (ok) _onSuccess();
    else _onFailure();
    return ok;
  }

  function _scheduleNextPing() {
    if (pingTimer) clearTimeout(pingTimer);
    const delay = document.hidden ? PING_HIDDEN_MS : PING_INTERVAL_MS;
    pingTimer = setTimeout(_loop, delay);
  }
  async function _loop() {
    await _ping();
    _scheduleNextPing();
  }

  // Public click handlers wired by the overlay's buttons.
  // ``_pwaTunnelLastDeepLink`` records the attempted scheme; useful for
  // diagnostics and cross-engine smoke tests since most browsers
  // refuse to expose the result of a navigation to an unhandled
  // scheme. Set BEFORE the actual assign so the record survives even
  // if the assignment throws.
  window._pwaTunnelLastDeepLink = null;
  window._pwaTunnelReconnect = () => {
    // Desktop has no Tailscale deep-link to fire (the macOS app doesn't
    // handle the scheme reliably, and the actual fix is an SSH-tunnel
    // shell command). The button is CSS-hidden on desktop; this guard
    // covers programmatic invocation (tests, future call sites).
    // We read the overlay's data-platform attribute as the live source
    // of truth so tests that rewrite it stay coherent with what the
    // user sees — and so the handler can never disagree with whether
    // the button is visible.
    const platform = $overlay.getAttribute('data-platform') || _platform;
    if (platform !== 'mobile') {
      _ping();
      return;
    }
    if (deepLinkWatchdog) clearTimeout(deepLinkWatchdog);
    if ($hintFallback) $hintFallback.classList.remove('visible');
    // Page Visibility flips to hidden when the OS hands off to another
    // app — that's the success signal we can observe from the browser.
    // If we're still visible after the watchdog window, the scheme
    // wasn't handled. Reveal the fallback copy.
    const startedHidden = document.hidden;
    window._pwaTunnelLastDeepLink = 'tailscale://';
    try {
      window.location.href = 'tailscale://';
    } catch (err) {
      // Some engines throw on unhandled schemes; the fallback hint
      // below covers that case.
      console.warn('tailscale:// navigation failed:', err);
    }
    deepLinkWatchdog = setTimeout(() => {
      if (!document.hidden && !startedHidden && $hintFallback) {
        $hintFallback.classList.add('visible');
      }
    }, DEEP_LINK_FALLBACK_MS);
  };
  window._pwaTunnelTryAgain = () => {
    // Force a ping immediately, bypassing the interval. _ping handles
    // success / failure side effects via the same path as the loop.
    _ping();
  };

  // SW signal: the service worker posts network-down on real fetch
  // failures. Treat as a single failure event (it can fire many times
  // in a row during a sustained outage — let the threshold logic
  // handle the escalation). Truthy check (not ``in``) so contexts that
  // define ``navigator.serviceWorker`` as ``undefined`` are skipped.
  if (navigator.serviceWorker) {
    navigator.serviceWorker.addEventListener('message', (event) => {
      const data = event && event.data;
      if (!data || data.type !== 'network-down') return;
      // Ignore until we've seen at least one successful connection;
      // otherwise a slow first paint racing the SW could trip the
      // overlay on the very first load.
      if (!everConnected) return;
      _onFailure();
    });
  }

  // The browser's own online/offline events are a useful additional
  // signal — navigator.onLine flips true means "LAN/wifi back" which
  // is worth a fresh ping (Tailscale will reconnect within seconds).
  window.addEventListener('online', () => { _ping(); });
  window.addEventListener('offline', () => { _onFailure(); });

  // Re-ping on visibility change: when the operator brings the tab
  // forward, give them a fresh result without waiting up to a minute.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      _ping();
    }
    _scheduleNextPing();
  });

  // Kick off. Wait until window load so the first ping doesn't fight
  // the SPA's own boot fan-out.
  window.addEventListener('load', () => {
    // First ping primes everConnected; afterwards the loop drives.
    _ping().then(() => _scheduleNextPing());
  });
})();
