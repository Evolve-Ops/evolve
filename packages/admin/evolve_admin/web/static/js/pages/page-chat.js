// ════════════════════════════════════════════════════════════════════════
// Page-mounted chat surfaces (Feedback + Help)
//
// The Feedback and Help sidebar pages render an embedded evo chat as
// their primary surface — no form on Feedback, no docs-first layout on
// Help. The chat reuses /api/home/chat (same backend, same page_context
// plumbing), the same .home-msg-* render classes, and the same
// localStorage key convention as the side-drawer (evolve_chat_<chatId>).
// On these two pages the ✦ FAB drawer is hidden (_evoDrawerOnPageChange)
// so the operator sees exactly one evo prompt.
//
// Each chatId ("feedback" / "help") is one fixed thread per page — the
// home-chat multi-session UX is overkill here.
//
// 14 functions covering: DOM element selectors (Thread/Input/SendBtn),
// page label + context pack derivation, bubble + thread rendering,
// append + keydown + run-suggested + retry-send + start-with helpers,
// and the main _pageChatSend.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), apiStream(), _parseSseBlock() — core/api.js
//   - toast(), escHtml(), attrJsLiteral() — core/dom-utils.js
//   - _evoChatPendingIndicator — pages/home.js (Phase 3m; shared chat helper)
//   - _evoDrawerFormatActivity / _evoSetPendingActivity / _evoDrawerLoad /
//     _evoDrawerSave / _evoDrawerSyncFabBadge — pages/evo-drawer.js
//     (Phase 3n; SSE activity formatters + storage helpers + FAB badge
//     refresh are shared between drawer and page-chat surfaces)
//
// Out of scope (still inline):
//   The Boot DOMContentLoaded that paints the FAB badge + sets the
//   evo-on-chat body class — it touches drawer state, page-chat detection,
//   AND subtab scroll-fade init; it's not page-chat-specific.
// ════════════════════════════════════════════════════════════════════════

function _pageChatThreadEl(chatId)  { return document.getElementById(`${chatId}-thread`); }
function _pageChatInputEl(chatId)   { return document.getElementById(`${chatId}-prompt-input`); }
function _pageChatSendBtn(chatId)   { return document.getElementById(`${chatId}-prompt-send`); }

function _pageChatPageLabel(chatId) {
  return chatId === 'feedback' ? 'Feedback' : chatId === 'help' ? 'Help' : chatId;
}

function _pageChatContextPack(chatId) {
  const pack = {
    page_id: chatId,
    page_label: _pageChatPageLabel(chatId),
    surface: 'admin_ui',
    surface_type: (typeof _evoSurfaceType === 'function') ? _evoSurfaceType() : 'laptop',
  };
  const builder = _EVO_CONTEXT_PACKS[chatId];
  if (builder) {
    try {
      const summary = builder();
      if (summary && Object.keys(summary).length) pack.summary = summary;
    } catch (_) {}
  }
  return pack;
}

function _pageChatRenderBubble(chatId, msg) {
  const thread = _pageChatThreadEl(chatId);
  if (!thread) return;
  const empty = thread.querySelector('.page-chat-empty');
  if (empty) empty.remove();
  const isUser = msg.role === 'user';
  const cls = isUser ? 'home-msg home-msg-user' : 'home-msg home-msg-evo';
  const errCls = msg.error ? ' home-msg-error' : (msg.warn ? ' home-msg-warn' : '');
  const pendingCls = msg.pending ? ' home-msg-pending' : '';
  const name = isUser ? 'you' : 'evo';
  const avatar = isUser
    ? '<span class="home-evo-avatar" style="background:rgba(124,92,255,0.28)">●</span>'
    : '<span class="home-evo-avatar">✦</span>';
  const tsStr = msg.ts ? ago(msg.ts) : '';
  let suggestedHtml = '';
  if (!isUser && Array.isArray(msg.suggested_actions) && msg.suggested_actions.length) {
    suggestedHtml = `<div class="home-msg-suggested">${
      msg.suggested_actions.map(a => {
        const label = escHtml(a.label || `Run evo ${a.subcommand}`);
        const cmd = escHtml(a.subcommand || '');
        return `<button class="btn btn-ghost btn-sm" onclick="_pageChatRunSuggested('${chatId}','${cmd}')">${label}</button>`;
      }).join('')
    }</div>`;
  }
  let retryHtml = '';
  if (!isUser && msg.error && typeof msg.retry_text === 'string' && msg.retry_text) {
    retryHtml = `<div class="home-msg-suggested">` +
      `<button class="btn btn-ghost btn-sm" onclick="_pageChatRetrySend('${chatId}', ${attrJsLiteral(msg.retry_text)})">↻ Retry</button>` +
      `</div>`;
  }
  // Gateway-down affordance — when the proxy detected that evo's gateway
  // isn't responding, offer a one-click switch to the diagnostic LLM
  // so the operator can still get help during the outage. ``chatId``
  // ('feedback' / 'help') routes the re-send back through this same
  // page composer rather than the home-chat composer.
  let diagnosticHtml = '';
  if (!isUser && msg.gateway_down) {
    const txt = typeof msg.retry_text === 'string' ? msg.retry_text : '';
    diagnosticHtml = `<div class="home-msg-suggested">` +
      `<button class="btn btn-ghost btn-sm" onclick="_enterDiagnosticMode(${attrJsLiteral(txt)}, '${chatId}')">` +
      `Talk to diagnostic LLM →` +
      `</button></div>`;
  }
  const bubble = document.createElement('div');
  bubble.className = cls + errCls + pendingCls;
  if (msg.dom_id) bubble.id = msg.dom_id;
  bubble.innerHTML = `
    <div class="home-msg-meta">
      ${avatar}
      <span class="home-evo-name">${escHtml(name)}</span>
      <span class="home-evo-time" data-ts="${escHtml(msg.ts || '')}">${escHtml(tsStr)}</span>
    </div>
    <div class="home-msg-body">${(typeof _homeChatFormatBody === 'function') ? _homeChatFormatBody(msg.text) : escHtml(String(msg.text || '')).replace(/\n/g, '<br>')}</div>
    ${suggestedHtml}
    ${retryHtml}
    ${diagnosticHtml}
  `;
  thread.appendChild(bubble);
  thread.scrollTop = thread.scrollHeight;
}

function _pageChatEmptyHTML(chatId) {
  if (chatId === 'feedback') {
    return `<div style="font-size:0.85rem;color:var(--text2);line-height:1.5;text-align:left;max-width:560px;margin:0 auto">` +
      `Type whatever's bothering you and I'll ask follow-up questions. ` +
      `If it's a config or local-environment issue, we'll often fix it without filing anything. ` +
      `If it's worth tracking, I'll point you at the form above to file it.` +
      `</div>`;
  }
  if (chatId === 'help') {
    return `<div style="font-size:0.85rem;color:var(--text2);line-height:1.5;text-align:left;max-width:560px;margin:0 auto">` +
      `Ask me anything about Evolve — your bots, what a chip means, how a feature works, where to find something. ` +
      `I can also walk you to the right page if you'd rather click than read.` +
      `</div>`;
  }
  return `Ask evo anything.`;
}

function _pageChatRenderThread(chatId) {
  const thread = _pageChatThreadEl(chatId);
  if (!thread) return;
  thread.innerHTML = '';
  // Use the drawer's storage helpers — same on-disk shape, same caps, same
  // archive semantics. The drawer's storage key for ``chatId`` matches the
  // key our send path writes to, so the page-chat thread IS the drawer
  // thread for that page (the drawer is just hidden here).
  const history = (typeof _evoDrawerLoad === 'function') ? _evoDrawerLoad(chatId) : [];
  if (history.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'page-chat-empty';
    empty.innerHTML = _pageChatEmptyHTML(chatId);
    thread.appendChild(empty);
    return;
  }
  for (const msg of history) _pageChatRenderBubble(chatId, msg);
}

function _pageChatAppend(chatId, msg) {
  // Mirror _evoDrawerAppend: persist + render. The persistence helpers
  // already cap thread length + archive overflow.
  const history = _evoDrawerLoad(chatId);
  history.push(msg);
  _evoDrawerSave(chatId, history);
  _pageChatRenderBubble(chatId, msg);
  if (typeof _evoDrawerSyncFabBadge === 'function') _evoDrawerSyncFabBadge();
}

function _pageChatKeydown(chatId, ev) {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    _pageChatSend(chatId);
  }
}

function _pageChatRunSuggested(chatId, subcommand) {
  const input = _pageChatInputEl(chatId);
  if (!input) return;
  input.value = `evo ${subcommand}`;
  _pageChatSend(chatId);
}

function _pageChatRetrySend(chatId, text) {
  const input = _pageChatInputEl(chatId);
  if (!input) return;
  input.value = String(text || '');
  _pageChatSend(chatId);
}

// Intent-button handler — drops a pre-seed message into the input and
// immediately sends. Used by the Help page chips. On the Feedback page
// the structured form is now inline alongside the chat (no separate
// affordance needed).
function _pageChatStartWith(chatId, text) {
  const input = _pageChatInputEl(chatId);
  if (!input) return;
  input.value = String(text || '');
  _pageChatSend(chatId);
}

async function _pageChatSend(chatId) {
  const input = _pageChatInputEl(chatId);
  const sendBtn = _pageChatSendBtn(chatId);
  if (!input) return;
  const text = (input.value || '').trim();
  if (!text) return;

  const userMsg = { role: 'user', text, ts: new Date().toISOString() };
  _pageChatAppend(chatId, userMsg);
  input.value = '';
  if (typeof autoResizeComposer === 'function') autoResizeComposer(input);
  if (sendBtn) sendBtn.disabled = true;
  input.disabled = true;

  const pendingId = `${chatId}-chat-pending-` + Date.now();
  _pageChatRenderBubble(chatId, {
    role: 'evo', text: '…thinking…', pending: true, dom_id: pendingId,
    ts: new Date().toISOString(),
  });

  const abortCtl = new AbortController();
  const stopIndicator = (typeof _evoChatPendingIndicator === 'function')
    ? _evoChatPendingIndicator(pendingId, EVO_DRAWER_SLOW_INDICATOR_MS)
    : () => {};
  let hardTimeoutFired = false;
  const pendingTimeoutId = setTimeout(() => {
    hardTimeoutFired = true;
    try { abortCtl.abort(); } catch (_) {}
    stopIndicator();
    const stale = document.getElementById(pendingId);
    if (stale) stale.remove();
    const ceilingS = Math.round(EVO_DRAWER_PENDING_TIMEOUT_MS / 1000);
    _pageChatAppend(chatId, {
      role: 'evo',
      text: `(no reply after ${ceilingS}s — server may be busy)`,
      error: true,
      retry_text: text,
      ts: new Date().toISOString(),
    });
  }, EVO_DRAWER_PENDING_TIMEOUT_MS);

  try {
    const history = _evoDrawerLoad(chatId)
      .filter(m => !m.pending && !m.error)
      .filter(m => m.role === 'user' || m.role === 'evo')
      .slice(0, -1);
    const page_context = _pageChatContextPack(chatId);
    const authority = (typeof _homeReadTier === 'function') ? _homeReadTier() : 'ask';
    const local_time = new Date().toISOString();
    const session_id = (typeof _evoDrawerOcSessionId === 'function')
      ? _evoDrawerOcSessionId(chatId)
      : `admin-ui-${chatId}`;
    // Per-conversation model tier — same sessionStorage value the
    // home composer uses (spec:
    // internal/spec-user-tier-control-2026-05-26.md).
    const tier = (typeof _homeReadModelTier === 'function') ? _homeReadModelTier() : 'auto';
    // Diagnostic mode — when evo's gateway is confirmed down, the
    // Feedback / Help page chats route to the pod-wide diagnostic LLM
    // (same flow as the home + drawer chats). The diagnostic endpoint
    // takes a stripped body — message + history are sufficient.
    const endpoint = _diagnosticMode ? '/api/diagnostic/chat' : '/api/home/chat';
    const sendBody = _diagnosticMode
      ? { message: text, history }
      : { message: text, history, page_context, authority, tier, local_time,
          session_id, attachments: [] };
    const resp = await api('POST', endpoint, sendBody,
      { signal: abortCtl.signal });
    clearTimeout(pendingTimeoutId);
    stopIndicator();
    if (hardTimeoutFired || resp?.aborted) return;
    if (resp?.network_error) {
      const pending = document.getElementById(pendingId);
      if (pending) pending.remove();
      _pageChatAppend(chatId, {
        role: 'evo',
        text: "Sorry — something went wrong and I didn't get a response. Please try again.",
        error: true, retry_text: text,
        ts: new Date().toISOString(),
      });
      return;
    }
    const reply = (resp && resp.reply) || resp?.error || '(no response)';
    const pending = document.getElementById(pendingId);
    if (pending) pending.remove();
    const meta = { role: 'evo', text: reply, ts: new Date().toISOString() };
    if (resp?.source === 'evo' && resp.model) {
      const usage = resp.usage || {};
      const tokens = (usage.input || 0) + (usage.output || 0);
      const tokenStr = tokens ? ` · ${tokens} tok` : '';
      const tierLabel = (typeof _modelTierLabel === 'function') ? _modelTierLabel(resp.model) : '';
      const tierPrefix = tierLabel ? `${tierLabel}: ` : '';
      meta.text += `\n\n_(${tierPrefix}${resp.model}${tokenStr})_`;
    }
    // tier_capped — Power was downgraded because the tier1 daily cap is
    // exhausted for the primary bot today. Spec:
    // internal/spec-user-tier-control-2026-05-26.md "Power capped surface".
    // Italics under the reply, never a banner.
    if (resp?.tier_capped) {
      meta.text += `\n\n_Power capped today — used Standard for this turn._`;
    }
    if (resp?.source === 'proxy_warn') {
      meta.warn = true;
    } else if (resp?.source === 'gateway_down') {
      // Evo's gateway is down — render the diagnostic-LLM affordance on
      // the bubble (added by _pageChatRenderBubble when msg.gateway_down).
      // ``retry_text`` carries the operator's original message so the
      // one-click switch re-sends through the diagnostic LLM without
      // retyping.
      meta.error = true;
      meta.gateway_down = true;
      meta.retry_text = text;
    } else if (resp?.source === 'proxy_error') {
      meta.error = true;
    } else if (resp?.source === 'cap_exceeded' || resp?.source === 'llm_error') {
      meta.error = true;
    }
    if (Array.isArray(resp?.suggested_actions) && resp.suggested_actions.length) {
      meta.suggested_actions = resp.suggested_actions;
    }
    _pageChatAppend(chatId, meta);
  } catch (e) {
    clearTimeout(pendingTimeoutId);
    stopIndicator();
    if (hardTimeoutFired || (e && e.name === 'AbortError')) return;
    const pending = document.getElementById(pendingId);
    if (pending) pending.remove();
    _pageChatAppend(chatId, {
      role: 'evo',
      text: "Sorry — something went wrong on my end. Please try again.",
      error: true, retry_text: text,
      ts: new Date().toISOString(),
    });
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
  }
}
