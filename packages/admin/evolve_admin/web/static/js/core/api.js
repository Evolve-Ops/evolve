// ════════════════════════════════════════════════════════════════════════
// Core: API client (fetch wrapper + SSE stream parser)
//
// All same-origin /api/ traffic flows through these two helpers. They're
// loaded before the main inline script so any handler defined in the
// shell can `await api(...)` or call `apiStream(...)` without worrying
// about load order. logError() is looked up at call time (still in the
// main script body), per the same pattern as router.js's onPageActivate.
// ════════════════════════════════════════════════════════════════════════

// ── CSRF double-submit (roadmap 2.7) ────────────────────────────────────
// The server sets a readable `evolve_csrf` cookie and requires its value
// echoed in the `X-CSRF-Token` header on every mutating request. We wrap
// the global `fetch` ONCE so api(), apiStream(), and every ad-hoc
// `fetch(url, {method:'POST'})` across the page modules get the header
// without each call site remembering to add it. Same-origin requests only;
// GET/HEAD/OPTIONS are exempt server-side, so we only stamp mutating verbs.
(function installCsrfFetch() {
  if (window.__evolveCsrfFetchInstalled) return;
  window.__evolveCsrfFetchInstalled = true;
  const SAFE = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE']);
  const readCsrf = () => {
    const m = document.cookie.match(/(?:^|;\s*)evolve_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  };
  const isSameOrigin = (url) => {
    try {
      // Relative URLs (the common case — '/api/…') are same-origin.
      return new URL(url, location.href).origin === location.origin;
    } catch (_e) { return false; }
  };
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    const method = (init.method || (typeof input !== 'string' && input && input.method) || 'GET').toUpperCase();
    const url = (typeof input === 'string') ? input : (input && input.url) || '';
    if (!SAFE.has(method) && isSameOrigin(url)) {
      const token = readCsrf();
      if (token) {
        const headers = new Headers(init.headers || (typeof input !== 'string' && input && input.headers) || {});
        if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
        init = Object.assign({}, init, { headers });
      }
    }
    return nativeFetch(input, init);
  };
})();

// ══════════════════════════════════════════════════════
// Utilities
// ══════════════════════════════════════════════════════
async function api(method, path, body, extra) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  // Always bypass the browser's HTTP cache for API calls — stale JSON
  // causes toggle buttons (CE, multiUser, etc.) to not reflect the server
  // state after a mutation + re-fetch cycle.
  if (method === 'GET') opts.cache = 'no-store';
  if (body) opts.body = JSON.stringify(body);
  // Optional AbortSignal — chat-send paths use this to enforce a hard
  // client-side ceiling on long-running turns so a stalled server can't
  // keep the operator staring at a spinner forever. See
  // _homeChatSend / _evoDrawerSend.
  if (extra && extra.signal) opts.signal = extra.signal;
  try {
    const r = await fetch(path, opts);
    const data = await r.json();
    if (!r.ok || data?.error) {
      logError(path, `HTTP ${r.status} — ${data?.error || r.statusText}`, `${method} ${path}`);
    }
    return data;
  } catch (e) {
    // Surface aborts distinctly so callers can render the right UX
    // (a timeout-styled bubble with retry) instead of a generic
    // "Could not reach server" error.
    if (e && e.name === 'AbortError') {
      return { error: 'AbortError', aborted: true };
    }
    const errEntry = logError(path, e.message || String(e), `${method} ${path}`);
    // Fire-and-forget /api/health probe — stamps server_up + uptime
    // onto the error log entry once it resolves, so the operator (or
    // a future Claude triaging the Errors page) can tell at a glance
    // whether the fetch failed because the daemon was down (e.g. mid-
    // kickstart) or because the chat endpoint specifically broke. Does
    // not block the caller's await; the error bubble renders right
    // away from the network_error return, and the diagnostic annotation
    // lands in the error log asynchronously.
    _probeServerForErrorLog(errEntry).catch(() => {});
    // network_error flags the "fetch itself failed" case (browser
    // returned a TypeError like Safari's bare "Load failed", DNS/TLS
    // failed, or the daemon was bouncing during a kickstart). Callers
    // that render this object into chat bubbles use the flag to swap
    // the raw browser error string for a user-friendly retry-able
    // message. HTTP non-2xx responses do NOT hit this branch — they
    // come back as parsed JSON above with whatever .error the server
    // set, and never set network_error.
    return { error: e.message || String(e), network_error: true };
  }
}
// SSE-flavored sibling of api() for chat surfaces that want to render
// progress while a long turn is in flight. Wire shape: the backend
// emits ``event: <type>\ndata: <json>\n\n`` blocks (per the SSE spec).
// Events seen here:
//   ``meta``           — opening event with session_id + start_offset
//   ``tool_call``      — one OC tool_use entry showed up in the jsonl
//   ``tool_result``    — paired ``toolResult`` arrived (ok | error)
//   ``assistant_text`` — interim assistant turn between tool fans
//   ``heartbeat``      — backend keep-alive while idle past N seconds
//   ``done``           — terminal event; payload matches the JSON shape
//                        the buffered /api/home/chat would have returned
//
// The caller passes ``onActivity(event)`` to render progress as events
// arrive (e.g. update a pending bubble's body text with "looking up
// pod_state.audit(bot_id=…)"). The returned promise resolves with the
// ``done`` payload — same downstream-handling code as ``api()``.
//
// Fall-through: when the response isn't actually SSE (e.g. the server
// decided to buffer despite the Accept header), the body is parsed as
// JSON and returned directly so the call site doesn't need a branch.
// Errors (network, abort, malformed stream) return the same
// ``{network_error, error}`` / ``{aborted}`` shapes ``api()`` uses.
async function apiStream(path, body, extra) {
  const onActivity = (extra && extra.onActivity) || null;
  const headers = {
    'Content-Type': 'application/json',
    'Accept': 'text/event-stream',
  };
  const opts = { method: 'POST', headers, body: JSON.stringify(body) };
  if (extra && extra.signal) opts.signal = extra.signal;
  try {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let data = null;
      try { data = await r.json(); } catch (_) {}
      logError(path, `HTTP ${r.status} — ${data?.error || r.statusText}`, `POST ${path}`);
      return { error: data?.error || `HTTP ${r.status}`, status: r.status };
    }
    const ct = (r.headers.get('Content-Type') || '').toLowerCase();
    if (!ct.includes('text/event-stream')) {
      // Backend chose to buffer (cache, route variant, etc.) — parse
      // the JSON body so the caller's downstream code still works.
      return await r.json();
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let donePayload = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE messages are separated by a blank line. Walk the buffer
      // for every complete block and parse one event each.
      let sep;
      while ((sep = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const ev = _parseSseBlock(block);
        if (!ev) continue;
        if (ev.event === 'done') {
          donePayload = ev.data;
        } else if (onActivity) {
          try { onActivity(ev); } catch (_) {}
        }
      }
    }
    if (donePayload) return donePayload;
    // Stream closed without a done event — synthesize a network_error
    // so the caller's existing error UX kicks in.
    return { error: 'stream_ended_without_done', network_error: true };
  } catch (e) {
    if (e && e.name === 'AbortError') {
      return { error: 'AbortError', aborted: true };
    }
    const errEntry = logError(path, e.message || String(e), `POST ${path}`);
    _probeServerForErrorLog(errEntry).catch(() => {});
    return { error: e.message || String(e), network_error: true };
  }
}

// Parse one SSE block (``event: x\ndata: {...}``) into ``{event, data}``.
// Tolerates extra blank lines, missing data, and multi-line ``data:``
// per the SSE spec (though our backend only emits single-line data).
function _parseSseBlock(block) {
  let event = 'message';
  let data = '';
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      data += line.slice(5).trim();
    }
  }
  if (!data) return null;
  try {
    return { event, data: JSON.parse(data) };
  } catch (_) {
    return null;
  }
}
