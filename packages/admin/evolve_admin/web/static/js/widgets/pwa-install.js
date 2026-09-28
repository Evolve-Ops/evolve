(() => {
  // ── beforeinstallprompt stash + sidebar reveal ──────────────────────
  // The event listener itself is attached in <head> (see the early
  // stash script) so we never miss the fire. Here we just pick up
  // anything already stashed and register a hand-off callback for
  // events that arrive after this point.
  let _deferredInstallPrompt = null;
  const _installItem = document.getElementById('pwa-install-item');
  const _showInstallItem = () => {
    if (_installItem) _installItem.style.display = 'flex';
  };
  const _hideInstallItem = () => {
    if (_installItem) _installItem.style.display = 'none';
  };

  window._pwaOnPromptAvailable = (e) => {
    _deferredInstallPrompt = e;
    _showInstallItem();
  };
  if (window._pwaPendingPrompt) {
    window._pwaOnPromptAvailable(window._pwaPendingPrompt);
  }
  window.addEventListener('appinstalled', () => {
    _deferredInstallPrompt = null;
    _hideInstallItem();
    _hideIosHint();
  });

  window._pwaInstallPromptClick = async () => {
    const evt = _deferredInstallPrompt;
    if (!evt) return;
    _deferredInstallPrompt = null;
    _hideInstallItem();
    try {
      await evt.prompt();
      await evt.userChoice; // resolves "accepted" / "dismissed"
    } catch (err) {
      console.warn('PWA install prompt failed:', err);
    }
  };

  // ── iOS Safari install hint ─────────────────────────────────────────
  // No beforeinstallprompt on iOS Safari, so detect the UA + non-
  // standalone state and surface a one-time tip. ``navigator.standalone``
  // is the Safari-specific (truthy when launched from home screen);
  // ``display-mode: standalone`` is the standards-compliant equivalent
  // for other engines. If either is true the user has already installed.
  const LS_KEY = 'pwa-ios-hint-dismissed';
  const _iosHint = document.getElementById('pwa-ios-hint');
  const _isIosSafari = () => {
    const ua = navigator.userAgent || '';
    // iPadOS 13+ reports as Mac in UA but exposes maxTouchPoints>1.
    const isIos = /iPad|iPhone|iPod/.test(ua) ||
      (ua.includes('Mac') && navigator.maxTouchPoints > 1);
    const isSafari = /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua);
    return isIos && isSafari;
  };
  const _isStandalone = () => {
    if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return true;
    if (navigator.standalone === true) return true;
    return false;
  };
  const _showIosHint = () => {
    if (!_iosHint) return;
    if (_isStandalone()) return;
    let dismissed = false;
    try { dismissed = localStorage.getItem(LS_KEY) === '1'; } catch (_) {}
    if (dismissed) return;
    _iosHint.classList.add('visible');
  };
  const _hideIosHint = () => {
    if (_iosHint) _iosHint.classList.remove('visible');
  };
  window._pwaDismissIosHint = () => {
    _hideIosHint();
    try { localStorage.setItem(LS_KEY, '1'); } catch (_) {}
  };
  if (_isIosSafari()) _showIosHint();

  // ── Service-worker registration + update toast ──────────────────────
  // SW only registers on secure contexts. Loading the SPA over plain
  // http://team_bot_a-mini:5050 (pre-§4.1) just no-ops here — no install UX,
  // no SW. That's deliberate: trying to register over http throws a
  // SecurityError, and Chrome's install criteria reject anyway. The
  // §4.1.d HTTPS-nudge banner already nags the operator about this.
  const _updateToast = document.getElementById('pwa-update-toast');
  let _waitingWorker = null;
  const _showUpdateToast = (worker) => {
    _waitingWorker = worker;
    if (_updateToast) _updateToast.classList.add('visible');
  };
  window._pwaActivateUpdate = () => {
    if (_updateToast) _updateToast.classList.remove('visible');
    if (_waitingWorker) {
      _waitingWorker.postMessage({ type: 'SKIP_WAITING' });
    } else {
      // No queued SW (e.g. user clicked after the activate fired);
      // a plain reload still picks up the newer assets.
      window.location.reload();
    }
  };

  // Truthy check on ``navigator.serviceWorker`` (not ``'serviceWorker' in
  // navigator``): some contexts define the property with an ``undefined``
  // value (Playwright isolated contexts, certain restricted browser
  // modes), and the ``in`` form would let execution fall into the block
  // and crash at ``.controller``/``.addEventListener`` below.
  if (navigator.serviceWorker && window.isSecureContext) {
    // Snapshot whether the page was controlled BEFORE registration. If
    // false, this is a first install — never an "update". Used to gate
    // both the toast and the controllerchange-driven reload so a fresh
    // visitor never sees a "New version available" pill for the only
    // version they have ever loaded. On webkit (CI), ``clients.claim()``
    // races with the ``statechange`` listener and can set
    // ``navigator.serviceWorker.controller`` while ``state === 'installed'``
    // is still firing — reading the live controller there triggered a
    // spurious toast that covered the iOS install hint. The snapshot
    // beats the race.
    const _hadInitialController = !!navigator.serviceWorker.controller;
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').then((reg) => {
        // A waiting worker on first load means a refresh happened while
        // the old SW was still active — surface the toast immediately.
        // ``_hadInitialController`` rules out the first-install case
        // (where ``reg.waiting`` can be set briefly before activate runs).
        if (reg.waiting && _hadInitialController) {
          _showUpdateToast(reg.waiting);
        }
        reg.addEventListener('updatefound', () => {
          const incoming = reg.installing;
          if (!incoming) return;
          incoming.addEventListener('statechange', () => {
            if (incoming.state === 'installed' && _hadInitialController) {
              _showUpdateToast(incoming);
            }
          });
        });
        // Long-lived SPA tabs: poll for SW updates so a tab opened
        // before a deploy still sees the update toast within minutes.
        // Without this, Chrome only re-checks /sw.js on navigation or
        // every 24h — an SPA stays on its launch-time JS forever. The
        // server pairs this with an index.html-hash fingerprint
        // prepended to sw.js, so any HTML-only change (the common
        // case) flips the SW byte stream and triggers updatefound.
        // 5 min is short enough that a deploy is visible promptly,
        // long enough that the poll cost is negligible (one HEAD-ish
        // GET per tab per 5 min). ``reg.update()`` rejects silently
        // if the SW server is down; we don't need to handle it.
        setInterval(() => { reg.update().catch(() => {}); }, 5 * 60 * 1000);
      }).catch((err) => {
        console.error('SW registration failed:', err);
      });

      // Only auto-reload on an UPDATE controllerchange — never on the
      // initial install. ``clients.claim()`` in the SW activate handler
      // fires controllerchange the very first time the SW takes a
      // previously-uncontrolled page; reloading there causes a no-op
      // flash and (worse) breaks any tests / tools observing the page
      // mid-navigation. ``_hadInitialController`` (defined above) gates
      // this: first installs never reload.
      let _reloading = false;
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (!_hadInitialController) return;
        if (_reloading) return;
        _reloading = true;
        window.location.reload();
      });
    });
  }
})();
