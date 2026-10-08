// ════════════════════════════════════════════════════════════════════════
// iMessage connection card (Settings → Bots → <bot>)
//
// The one-page wizard from internal/design-imessage-channel-2026-09-29.md
// (D-IM3): 1) sign in to Messages (the only by-hand step), 2) who may text
// it, 3) connect. The address is READ BACK from Messages — there is no
// field to type it. Backed by /api/connections/<bot>/imessage[/…]
// (web/imessage_routes.py). Called from pod-config.js loadConfigBot().
// ════════════════════════════════════════════════════════════════════════

let _imsgPoll = null;

// api() returns the error body instead of throwing; the wizard wants a throw
// carrying the operator-legible detail (a refused connect is ``ok: false``
// with a ``detail``, a platform refusal carries ``error``).
async function _imsgApi(method, path, body) {
  const res = await api(method, path, body);
  if (!res || res.error || res.ok === false) {
    const e = new Error((res && (res.detail || res.error)) || 'Request failed');
    e.code = res && res.error;
    throw e;
  }
  return res;
}

function _imsgStopPoll() {
  if (_imsgPoll) { clearInterval(_imsgPoll); _imsgPoll = null; }
}

async function renderBotImessageCard(botId) {
  _imsgStopPoll();
  const el = document.getElementById('botcfg-imessage');
  if (!el) return;
  if (!botId) { el.innerHTML = '<div class="empty">Select a bot above…</div>'; return; }
  let st;
  try {
    st = await _imsgApi('GET', `/api/connections/${encodeURIComponent(botId)}/imessage`);
  } catch (e) {
    const msg = String((e && e.message) || e);
    el.innerHTML = (e && e.code === 'skill_unavailable_on_platform')
      ? '<div class="subtle">iMessage needs a Mac pod. This bot\'s iMessage would live on the Mac pod.</div>'
      : `<div class="subtle" style="color:var(--red)">Could not load: ${escHtml(msg)}</div>`;
    return;
  }
  if (st.open_policy && st.open_policy.length) {
    el.innerHTML = `<div class="subtle" style="color:var(--red)">This bot's config lets anyone message it (${escHtml(st.open_policy.join('; '))}). Fix that first — Evolve never connects an open channel.</div>`;
    return;
  }
  if (st.row) { _imsgRenderConnected(el, botId, st); return; }
  _imsgRenderWizard(el, botId, st);
}

function _imsgRenderConnected(el, botId, st) {
  const r = st.row;
  const b = escHtml(botId);
  const who = (r.allow_from || []).length
    ? r.allow_from.map(escHtml).join(', ')
    : 'nobody yet — it receives nothing';
  el.innerHTML = `
    <div style="margin-bottom:8px"><strong>${escHtml(r.handle)}</strong> · ${escHtml(r.health_display)}</div>
    <div class="subtle" style="margin-bottom:8px">Who may text it: ${who}</div>
    <div id="imsg-result-${b}" class="subtle" style="margin-bottom:8px"></div>
    <button class="btn btn-sm" onclick="imessageRecheck('${b}')">Check again</button>
    <button class="btn btn-sm" onclick="imessageEditPeople('${b}')">Change who may text it</button>
    <button class="btn btn-sm" onclick="imessageDisconnect('${b}')">Disconnect</button>`;
}

function _imsgRenderWizard(el, botId, st, prefillOverride) {
  const b = escHtml(botId);
  const people = (prefillOverride || st.prefill_allow_from || []).join('\n');
  const signedIn = !!st.signed_in_handle;
  el.innerHTML = `
    <div class="subtle" style="margin-bottom:4px"><strong>1. Sign in</strong> — the only step you do by hand.</div>
    <div style="margin-bottom:8px">${escHtml(st.signin.text)}</div>
    <div style="margin-bottom:8px">
      <label><input type="radio" name="imsg-mode-${b}" value="own" checked> Its own Apple ID (recommended — its own inbox, kept separate)</label><br>
      <label><input type="radio" name="imsg-mode-${b}" value="shared"> The pod's shared Apple ID (no extra Apple ID to babysit; each person may text only one bot)</label>
    </div>
    <div id="imsg-signin-${b}" style="margin-bottom:12px"></div>
    <div class="subtle" style="margin-bottom:4px"><strong>2. Who may text it</strong> — one phone number (with +country code) or email per line. Anyone not listed is ignored.</div>
    <textarea id="imsg-allow-${b}" class="input-w-text" rows="4">${escHtml(people)}</textarea>
    <div id="imsg-tg-${b}" style="margin:8px 0"></div>
    <div class="subtle" style="margin:12px 0 4px"><strong>3. Connect</strong></div>
    <button class="btn btn-primary btn-sm" id="imsg-go-${b}" onclick="imessageConnect('${b}')" ${signedIn ? '' : 'disabled'}>Connect and say hello</button>
    <div id="imsg-result-${b}" class="subtle" style="margin-top:8px"></div>`;
  _imsgShowSignin(botId, st.signed_in_handle);
  if (st.telegram_binding) {
    document.getElementById(`imsg-tg-${botId}`).innerHTML =
      `<label><input type="checkbox" id="imsg-tg-retire-${b}"> This bot also has a Telegram connection. Remove it now that iMessage is set up? (Leave unchecked to keep it.)</label>`;
  }
  if (!signedIn) {
    _imsgApi('POST', `/api/connections/${encodeURIComponent(botId)}/imessage/keeper`, {}).catch(() => {});
    _imsgPoll = setInterval(async () => {
      try {
        const s = await _imsgApi('GET', `/api/connections/${encodeURIComponent(botId)}/imessage`);
        if (s.signed_in_handle) { _imsgStopPoll(); _imsgShowSignin(botId, s.signed_in_handle); }
      } catch (_) { /* keep polling */ }
    }, 3000);
  }
}

function _imsgShowSignin(botId, handle) {
  const s = document.getElementById(`imsg-signin-${botId}`);
  const go = document.getElementById(`imsg-go-${botId}`);
  if (!s) return;
  s.innerHTML = handle
    ? `<span style="color:var(--green)">Signed in as ${escHtml(handle)}</span>`
    : '<span class="subtle">Waiting for the sign-in… this page notices when it is done.</span>';
  if (go) go.disabled = !handle;
}

async function imessageConnect(botId) {
  const out = document.getElementById(`imsg-result-${botId}`);
  const go = document.getElementById(`imsg-go-${botId}`);
  const allow = document.getElementById(`imsg-allow-${botId}`).value
    .split(/[\n,;]+/).map(s => s.trim()).filter(Boolean);
  const modeEl = document.querySelector(`input[name="imsg-mode-${botId}"]:checked`);
  const retire = document.getElementById(`imsg-tg-retire-${botId}`);
  go.disabled = true;
  out.textContent = 'Connecting… this takes about a minute.';
  try {
    const res = await _imsgApi('POST', `/api/connections/${encodeURIComponent(botId)}/imessage/connect`, {
      allow_from: allow,
      apple_id_mode: modeEl ? modeEl.value : 'own',
      retire_telegram: !!(retire && retire.checked),
    });
    toast('iMessage connected' + (res.first_message_to ? ` — said hello to ${res.first_message_to}` : ''));
    renderBotImessageCard(botId);
  } catch (e) {
    out.style.color = 'var(--red)';
    out.textContent = String((e && e.message) || e);
    go.disabled = false;
  }
}

async function imessageRecheck(botId) {
  const out = document.getElementById(`imsg-result-${botId}`);
  out.textContent = 'Checking…';
  try {
    const res = await _imsgApi('POST', `/api/connections/${encodeURIComponent(botId)}/imessage/probe`, {});
    out.textContent = res.probe.ok ? 'All good.' : (res.probe.reason || 'Something is off.');
    out.style.color = res.probe.ok ? 'var(--green)' : 'var(--red)';
  } catch (e) {
    out.textContent = String((e && e.message) || e);
  }
}

async function imessageEditPeople(botId) {
  const st = await _imsgApi('GET', `/api/connections/${encodeURIComponent(botId)}/imessage`);
  _imsgRenderWizard(document.getElementById('botcfg-imessage'), botId,
    Object.assign({}, st, { signed_in_handle: st.signed_in_handle || (st.row && st.row.handle) }),
    (st.row && st.row.allow_from) || []);
}

async function imessageDisconnect(botId) {
  if (!await confirmModal('Disconnect iMessage from this bot? It will stop receiving texts.')) return;
  try {
    await _imsgApi('POST', `/api/connections/${encodeURIComponent(botId)}/imessage/disconnect`, {});
    renderBotImessageCard(botId);
  } catch (e) {
    toast(String((e && e.message) || e), 'err');
  }
}

window.renderBotImessageCard = renderBotImessageCard;
window.imessageConnect = imessageConnect;
window.imessageRecheck = imessageRecheck;
window.imessageEditPeople = imessageEditPeople;
window.imessageDisconnect = imessageDisconnect;
