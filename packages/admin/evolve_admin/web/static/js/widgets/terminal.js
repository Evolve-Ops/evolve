(function () {
  'use strict';

  // ── Nav hide-when-disabled ─────────────────────────────────────────────
  // /api/terminal/info reports { enabled, pod_name, now }. If the server
  // says disabled, hide the nav row entirely so the operator doesn't
  // land on a page that can't connect.
  async function _refreshTerminalNav() {
    const navItem = document.getElementById('nav-terminal');
    if (!navItem) return;
    try {
      const r = await fetch('/api/terminal/info', { cache: 'no-store' });
      if (!r.ok) { navItem.style.display = 'none'; return; }
      const info = await r.json();
      navItem.style.display = info.enabled ? '' : 'none';
    } catch (_e) {
      navItem.style.display = 'none';
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _refreshTerminalNav);
  } else {
    _refreshTerminalNav();
  }

  // ── First-use modal ────────────────────────────────────────────────────
  const LS_KEY = 'terminalAcceptedAt';
  let _pendingActivate = false;       // set when nav() landed us on the
                                      // page but we're waiting on modal accept

  function _firstUseAccepted() {
    try { return !!localStorage.getItem(LS_KEY); } catch (_e) { return false; }
  }
  function _showFirstUseModal() {
    const m = document.getElementById('terminal-firstuse-modal');
    if (m) m.classList.add('open');
  }
  function _hideFirstUseModal() {
    const m = document.getElementById('terminal-firstuse-modal');
    if (m) m.classList.remove('open');
  }
  window.terminalFirstUseAccept = function () {
    try { localStorage.setItem(LS_KEY, new Date().toISOString()); } catch (_e) {}
    _hideFirstUseModal();
    if (_pendingActivate) {
      _pendingActivate = false;
      _terminalActivate();
    }
  };
  window.terminalFirstUseCancel = function () {
    _hideFirstUseModal();
    _pendingActivate = false;
    // Bounce back to Home — the operator declined, so the Terminal page
    // shouldn't be the foreground surface.
    const home = document.querySelector('.nav-item[data-page="home"]');
    if (home && typeof window.nav === 'function') window.nav(home);
  };

  // ── xterm.js lifecycle ─────────────────────────────────────────────────
  let _term = null;          // xterm.js Terminal instance
  let _fit = null;           // FitAddon
  let _ws = null;            // WebSocket
  let _ready = false;        // server sent {type:'ready'}
  let _resizeObserver = null;
  let _onWindowResize = null;
  let _autoRetryUsed = false;   // one-shot transient-drop retry, reset on manual reconnect / page re-enter
  let _intentionalClose = false; // true when teardown initiated the close — suppresses retry + status flip

  function _wsUrl() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${location.host}/api/terminal/ws`;
  }

  // Update the header status pill + reconnect-button visibility. Centralized
  // so the various ws lifecycle hooks all flow through one place; keeps
  // the meta line and the button in lockstep.
  function _setStatus(state, detail) {
    const meta = document.getElementById('terminal-session-meta');
    const btn = document.getElementById('terminal-reconnect-btn');
    if (meta) {
      meta.classList.remove('is-connecting', 'is-connected', 'is-disconnected');
      let label = '';
      if (state === 'connecting') { meta.classList.add('is-connecting'); label = 'connecting…'; }
      else if (state === 'connected') { meta.classList.add('is-connected'); label = detail || 'connected'; }
      else if (state === 'disconnected') { meta.classList.add('is-disconnected'); label = detail || 'disconnected'; }
      else { label = detail || 'idle'; }
      meta.innerHTML = '<span class="dot"></span>' + label.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'})[c]);
    }
    if (btn) btn.hidden = (state !== 'disconnected');
  }

  // Pre-populate the pod name from /api/terminal/info so the header
  // reads correctly even if the WS never connects. The ready frame
  // will overwrite this with the authoritative value if/when it arrives.
  async function _populatePodName() {
    const podEl = document.getElementById('terminal-pod-name');
    if (!podEl) return;
    try {
      const r = await fetch('/api/terminal/info', { cache: 'no-store' });
      if (!r.ok) return;
      const info = await r.json();
      if (info && info.pod_name) podEl.textContent = info.pod_name;
    } catch (_e) { /* fall through — placeholder stays */ }
  }

  // Manual reconnect handler exposed on window for the header button.
  window.terminalReconnect = function () {
    _autoRetryUsed = false;
    _terminalActivate();
  };

  function _terminalActivate() {
    if (typeof window.Terminal !== 'function') {
      const fb = document.getElementById('terminal-fallback');
      if (fb) {
        fb.style.display = '';
        fb.textContent = 'xterm.js failed to load (network blocked the CDN?). The Terminal page can\'t initialize.';
      }
      return;
    }
    const container = document.getElementById('terminal-container');
    if (!container) return;

    // Reuse an existing instance if one is already mounted. The page is
    // a long-lived DOM node; we only initialize once per navigation
    // session unless the WS dropped.
    if (_term && _ws && _ws.readyState === WebSocket.OPEN) {
      try { _fit && _fit.fit(); } catch (_e) {}
      try { _term.focus(); } catch (_e) {}
      return;
    }

    // Header pod name + status pill — independent of WS success.
    _populatePodName();
    _setStatus('connecting');

    // Fresh init (or reinit after a closed WS).
    _intentionalClose = true;  // teardown's close(1000) should not trigger retry
    _teardownTerminal();
    _intentionalClose = false;
    _term = new window.Terminal({
      cursorBlink: true,
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      fontSize: 13,
      theme: { background: '#0a0d10', foreground: '#e6edf3' },
      scrollback: 5000,
      convertEol: false,
    });
    _fit = new window.FitAddon.FitAddon();
    _term.loadAddon(_fit);
    if (window.WebLinksAddon && window.WebLinksAddon.WebLinksAddon) {
      try { _term.loadAddon(new window.WebLinksAddon.WebLinksAddon()); } catch (_e) {}
    }
    _term.open(container);
    try { _fit.fit(); } catch (_e) {}

    _openWebSocket();

    // Resize tracking — both window resize *and* container resize (the
    // sidebar drawer collapsing on mobile changes the container width
    // without a window-level event). The ResizeObserver is the modern
    // path; window 'resize' covers older edge cases.
    if (window.ResizeObserver && !_resizeObserver) {
      _resizeObserver = new ResizeObserver(() => _safeFit());
      _resizeObserver.observe(container);
    }
    _onWindowResize = () => _safeFit();
    window.addEventListener('resize', _onWindowResize);
  }

  function _safeFit() {
    if (!_term || !_fit) return;
    try {
      _fit.fit();
      const dims = _term.rows && _term.cols
        ? { rows: _term.rows, cols: _term.cols } : null;
      if (dims && _ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({
          type: 'resize', rows: dims.rows, cols: dims.cols,
        }));
      }
    } catch (_e) {}
  }

  function _openWebSocket() {
    let ws;
    _setStatus('connecting');
    try {
      ws = new WebSocket(_wsUrl());
    } catch (err) {
      if (_term) _term.write(`\r\n\x1b[31mterminal: ws open failed: ${err.message}\x1b[0m\r\n`);
      _setStatus('disconnected', `open failed: ${err && err.message ? err.message : 'unknown error'}`);
      return;
    }
    _ws = ws;
    _ready = false;
    let _lastError = '';

    ws.onopen = function () {
      // Send the initial size so the shell renders at the right width.
      if (_term && _term.rows && _term.cols) {
        try {
          ws.send(JSON.stringify({
            type: 'resize', rows: _term.rows, cols: _term.cols,
          }));
        } catch (_e) {}
      }
    };
    ws.onmessage = function (evt) {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (_e) { return; }
      if (!msg || !msg.type) return;
      if (msg.type === 'ready') {
        _ready = true;
        _autoRetryUsed = false;  // a successful connection earns a fresh transient-retry budget
        const podEl = document.getElementById('terminal-pod-name');
        if (podEl && msg.pod_name) podEl.textContent = msg.pod_name;
        const dt = msg.started_at ? new Date(msg.started_at) : new Date();
        _setStatus('connected', `session started ${dt.toLocaleTimeString()}`);
      } else if (msg.type === 'output') {
        if (_term) _term.write(msg.data || '');
      } else if (msg.type === 'error') {
        _lastError = msg.message || msg.code || 'unknown';
        if (_term) _term.write(`\r\n\x1b[31mterminal: ${_lastError}\x1b[0m\r\n`);
      } else if (msg.type === 'pong') {
        // ignore
      }
    };
    ws.onerror = function () {
      if (_term) _term.write('\r\n\x1b[31mterminal: ws error\x1b[0m\r\n');
      // Don't flip status here — onclose always follows and carries the
      // close code, which is the more useful signal for the operator.
    };
    ws.onclose = function (evt) {
      const code = evt && typeof evt.code === 'number' ? evt.code : 0;
      const reasonText = (evt && evt.reason) || _lastError || '';
      const wasReady = _ready;
      _ws = null;
      _ready = false;
      if (_intentionalClose) return;  // teardown — leave status alone
      if (_term) {
        const reasonSuffix = reasonText ? ` (${reasonText})` : '';
        _term.write(`\r\n\x1b[33mterminal: disconnected${reasonSuffix}\x1b[0m\r\n`);
      }
      // Auto-retry exactly once for unexpected drops after a previously
      // working session — covers laptop-sleep / Tailscale-NAT-collapse
      // style transient failures without bouncing the operator through
      // the manual reconnect button every time.
      const isTransient = wasReady && code !== 1000 && code !== 1008;
      if (isTransient && !_autoRetryUsed) {
        _autoRetryUsed = true;
        const hint = code ? `code ${code}${reasonText ? ' · ' + reasonText : ''}` : reasonText || 'unknown';
        _setStatus('disconnected', `${hint} · retrying…`);
        setTimeout(() => {
          // Only retry if the user is still on the Terminal page and
          // we haven't been torn down in the meantime.
          if (_term && !_ws) _openWebSocket();
        }, 1500);
        return;
      }
      const detail = code
        ? `disconnected (code ${code}${reasonText ? ' · ' + reasonText : ''})`
        : (reasonText ? `disconnected (${reasonText})` : 'disconnected');
      _setStatus('disconnected', detail);
    };

    if (_term && !_term._termWsBound) {
      _term.onData(function (data) {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
          try { _ws.send(JSON.stringify({ type: 'input', data })); } catch (_e) {}
        }
      });
      _term._termWsBound = true;
    }
  }

  function _teardownTerminal() {
    try { _onWindowResize && window.removeEventListener('resize', _onWindowResize); } catch (_e) {}
    _onWindowResize = null;
    try { _resizeObserver && _resizeObserver.disconnect(); } catch (_e) {}
    _resizeObserver = null;
    if (_ws) {
      _intentionalClose = true;  // suppress the retry/status path on this close
      try { _ws.close(1000, 'page exit'); } catch (_e) {}
      _ws = null;
    }
    if (_term) {
      try { _term.dispose(); } catch (_e) {}
      _term = null;
      _fit = null;
      _ready = false;
    }
    _intentionalClose = false;
  }

  // ── Hook into the SPA's onPageActivate ─────────────────────────────────
  // onPageActivate() is defined later in the main script; we extend it
  // with a wrapping pattern that survives if the function is reassigned
  // (the SPA never does, but defensive). The page-leave teardown is
  // similarly wrapped on nav().
  function _hookSpaLifecycle() {
    if (typeof window.onPageActivate === 'function') {
      const prev = window.onPageActivate;
      window.onPageActivate = function (page) {
        const result = prev.apply(this, arguments);
        if (page === 'terminal') {
          if (!_firstUseAccepted()) {
            _pendingActivate = true;
            _showFirstUseModal();
          } else {
            _terminalActivate();
          }
        } else if (_term || _ws) {
          // Left the Terminal page — tear the PTY down. A reload
          // reconnects from scratch (spec: no tmux/session persistence).
          _teardownTerminal();
        }
      };
    }
    // Also wrap nav() so the modal can intercept the very first nav
    // before onPageActivate fires? Not necessary — the modal is shown
    // *after* the page activates, which keeps the layout consistent
    // when the operator cancels (we navigate them home).
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _hookSpaLifecycle);
  } else {
    _hookSpaLifecycle();
  }
})();
