// ════════════════════════════════════════════════════════════════════════
// Page subtab: Pod Config — Config → Bot + Security Audit
//
// Two Pod-Config subtabs as one contiguous block:
//
//   1. Config → Bot subtab — per-bot setup + read-only openclaw view.
//      State: _configBot (currently selected)
//      Functions: initConfigBotSelector + switchConfigBot + loadConfigBot
//        + per-card renderers (_renderBotModelsCard,
//        _renderBotCompactionCard, _applyChannelCardVisibility) +
//        Slack policy editor (renderSlackPolicy, slackPolicyInit,
//        slackPolicyApply, renderClassifierKeywords).
//
//   2. Security Audit subtab (also under Pod Config) — per-bot audit
//      results + mute flow + V1.5-3 chip cluster + safety dimension
//      proposals.
//      State: _auditData, _secMutedData, _secSafetyData,
//             _secProposalsByBot, audit-polling timer state.
//      Functions: loadSecurityAudit + runSecurityAudit +
//        _loadSecurityAuditSignals + _isAuditErrorResponse +
//        _auditStartPolling / _auditStopPolling / _auditPollRefresh +
//        _secLoadProposals / _secJumpToSecurityProposals +
//        _secLoadMuted / _secMutedSet / _secMute / _secUnmuteAll +
//        _secScrollToBot + _secRerun.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - openProposalDetail — self-improvement.js
//   - nav() / window.nav — core/router.js
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Config → Bot subtab (per-bot setup + read-only openclaw view)
// ══════════════════════════════════════════════════════
let _configBot = null;

async function initConfigBotSelector() {
  const tabsEl = document.getElementById('config-bot-tabs');
  if (!tabsEl) return;
  try {
    const r = await fetch('/api/network');
    const j = await r.json();
    // Cache the network data first so botLabel() + orderedBotIds() resolve the
    // primary correctly (the bot tabs may render before the global
    // _networkData cache is populated by overview/init).
    if (j && j.bots) _networkData = j;
    const members = orderedBotIds(j.bots || {});
    if (!_configBot || !members.includes(_configBot)) _configBot = members[0] || null;
    tabsEl.innerHTML = members.map(b =>
      `<div class="subtab ${b === _configBot ? 'active' : ''}" data-bot="${escHtml(b)}" onclick="switchConfigBot('${escHtml(b)}')">${escHtml(botLabel(b))}</div>`
    ).join('');
    if (_configBot) loadConfigBot();
  } catch (e) {
    document.getElementById('config-bot-profile-body').innerHTML = `<div class="card" style="padding:14px">Failed to load bot list: ${escHtml(String(e))}</div>`;
  }
}

function switchConfigBot(botId) {
  _configBot = botId;
  // Match the active tab by data-bot rather than text — display names
  // mean .textContent may differ from the bot id after a rename.
  document.querySelectorAll('#config-bot-tabs .subtab').forEach(t =>
    t.classList.toggle('active', t.dataset.bot === botId)
  );
  loadConfigBot();
}

// ─────────────────────────────────────────────────────────────────────────────
// PR K (Bot Config UX overhaul, 2026-06-01) — helpers extracted from
// loadConfigBot() so each card answers "what is this, why do I care,
// what's set, and how do I change it" without a hover.
//
// Each helper takes the card's container element + the API payload and
// renders content that augments the visible card subtitle (see the HTML
// mount-points). The cards remain read-only here; edit affordances are
// inline links / buttons that jump to the page where the value lives.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Render the Primary Model + Fallbacks read-only view.
 *
 * "How to change this": inline "Edit on AI Optimization" button — the
 * AI Optimization page writes ~/.openclaw/evolve-tiers.json which is
 * the canonical source for per-bot primary/fallback model assignment
 * after the 2026-05-25 simplification (PR #1544).
 *
 * Reads from /api/bot/models which surfaces openclaw.json's
 * agents.defaults.model.primary and modelFallbacks. The tier-resolution
 * detail (workhorse default vs grunt fallback) is surfaced as a small
 * footnote rather than a heavy table — that table lives on AI
 * Optimization itself.
 */
function _renderBotModelsCard(el, models, botId) {
  if (!el) return;
  if (models && models.error) {
    el.innerHTML = `<div class="empty">${escHtml(models.error)}</div>`;
    return;
  }
  const primary = models && models.primary ? models.primary : '';
  const fallbacks = (models && models.fallbacks) || [];
  const perAgent = (models && models.per_agent) || {};
  const safeBot = escHtml(botId);
  const primaryDisplay = primary
    ? badge(primary, 'inline')
    : `<span style="color:var(--text3);font-style:italic">not set — OpenClaw will use its own default</span>`;
  const fallbacksBlock = fallbacks.length
    ? `<div style="margin-bottom:12px">
         <div style="font-size:0.78rem;color:var(--text2);margin-bottom:4px">Fallbacks (tried in order if the primary is rate-limited or errors)</div>
         <div style="display:flex;gap:6px;flex-wrap:wrap">${fallbacks.map(m => badge(m, 'member')).join('')}</div>
       </div>`
    : `<div style="margin-bottom:12px">
         <div style="font-size:0.78rem;color:var(--text2);margin-bottom:4px">Fallbacks</div>
         <div class="subtle" style="font-size:0.82rem">None configured. The bot stops if the primary model errors.</div>
       </div>`;
  const perAgentBlock = Object.keys(perAgent).length
    ? `<div style="margin-bottom:12px">
         <div style="font-size:0.78rem;color:var(--text2);margin-bottom:4px">Per-agent overrides</div>
         <div style="display:flex;flex-wrap:wrap;gap:6px">${Object.entries(perAgent).map(([a, m]) => `<span style="font-size:0.82rem"><span style="color:var(--text2)">${escHtml(a)}:</span> ${badge(m || '—', 'inline')}</span>`).join('')}</div>
       </div>`
    : '';
  // The "how to change this" affordance: the writer for primary/
  // fallbacks lives on AI Optimization (evolve-tiers.json). The CLI
  // alternative is documented for operators who prefer the host.
  el.innerHTML = `
    <div style="margin-bottom:12px">
      <div style="font-size:0.78rem;color:var(--text2);margin-bottom:4px">Primary model (the LLM used for every turn by default)</div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        ${primaryDisplay}
      </div>
    </div>
    ${fallbacksBlock}
    ${perAgentBlock}
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
      <button class="btn btn-ghost btn-sm" onclick="tierResolutionJumpToAiOptimization('${safeBot}')" style="font-size:0.82rem">→ Change on AI Optimization</button>
      <span class="subtle" style="font-size:0.74rem">or on the host: <code style="background:var(--bg3);padding:1px 5px;border-radius:3px">sudo -u &lt;bot&gt; openclaw config set agents.defaults.model.primary &lt;model&gt;</code></span>
    </div>
  `;
}

/**
 * Render the Compaction Settings read-only view with plain-English
 * labels per key.
 *
 * "How to change this": values are part of agents.defaults.compaction
 * in openclaw.json. The Customizations card auto-records any deviation
 * from defaults — operators can edit on the host with `openclaw config
 * set …` and the next deploy promotes it to a tracked override. There
 * is no inline writer here today (documented as a follow-up in the PR).
 */
const _COMPACTION_KEY_HELP = {
  mode: 'Strategy used when context fills up — "auto" (default) lets OpenClaw decide; "always" rewrites every turn; "never" disables.',
  reserveTokensFloor: 'Minimum free tokens to keep available for the next response. Lower = riskier overflow; higher = compacts sooner.',
  memoryFlush: 'How aggressively old turns get folded into a summary (low / medium / high).',
  recencyTurns: 'Number of recent turns kept verbatim even after compaction.',
  rounds: 'Maximum compaction passes per turn before giving up.',
};

function _renderBotCompactionCard(el, compaction, botId) {
  if (!el) return;
  if (!compaction || compaction.error || !Object.keys(compaction).length) {
    el.innerHTML = `<div class="subtle" style="font-size:0.85rem">
        ${compaction && compaction.error ? escHtml(compaction.error) : 'No compaction settings recorded — this bot uses OpenClaw\'s built-in defaults.'}
      </div>
      <div style="margin-top:10px;font-size:0.74rem;color:var(--text2)">
        Change on the host: <code style="background:var(--bg3);padding:1px 5px;border-radius:3px">sudo -u &lt;bot&gt; openclaw config set agents.defaults.compaction.&lt;key&gt; &lt;value&gt;</code>.
        Any deviation from the shipped default is auto-recorded in the Customizations card above on the next deploy.
      </div>`;
    return;
  }
  const safeBot = escHtml(botId);
  const rows = Object.entries(compaction).map(([k, v]) => {
    const help = _COMPACTION_KEY_HELP[k] || '';
    const display = typeof v === 'object' ? JSON.stringify(v) : String(v);
    return `
      <div style="display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-bottom:1px solid var(--border);align-items:flex-start;flex-wrap:wrap">
        <div style="flex:1;min-width:200px">
          <div style="font-family:monospace;font-size:0.85rem;color:var(--text)">${escHtml(k)}</div>
          ${help ? `<div class="subtle" style="font-size:0.76rem;margin-top:2px;line-height:1.4">${escHtml(help)}</div>` : ''}
        </div>
        <div style="font-family:monospace;font-size:0.85rem;color:var(--teal);text-align:right;min-width:80px">${escHtml(display)}</div>
      </div>`;
  }).join('');
  el.innerHTML = `
    <div>${rows}</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px;padding-top:6px">
      <span class="subtle" style="font-size:0.74rem">To change a setting on the host: <code style="background:var(--bg3);padding:1px 5px;border-radius:3px">sudo -u &lt;bot&gt; openclaw config set agents.defaults.compaction.&lt;key&gt; &lt;value&gt;</code>. Any change is auto-recorded in the Customizations card above on the next deploy.</span>
    </div>
  `;
}

/**
 * Show or hide channel-specific cards based on whether the bot has
 * the integration configured. Today only Slack is in this group; the
 * group header (#botcfg-section-channels) is hidden iff every card
 * inside it is hidden, so adding a future channel (Discord policy
 * card, Telegram policy card) needs only the same hide/show logic
 * for the new card mount-point.
 *
 * Slack hide rule: hide when the doctor's view of openclaw.json has
 * NO slack block (slack_enabled is null/undefined) AND there's no
 * Slack policy file recorded. We do NOT hide just on
 * slack_enabled=false — that's the "administratively disabled"
 * state (SLK012) where the operator might still want to flip it
 * back on, so leaving the card visible is the right call.
 */
function _applyChannelCardVisibility(slackPolicy, botId) {
  const slackCard = document.getElementById('botcfg-slack-card');
  if (slackCard) {
    const doc = (slackPolicy && slackPolicy.doctor) || {};
    const hasSlack = (
      doc.slack_enabled === true ||
      doc.slack_enabled === false ||  // explicitly disabled = still a slack bot
      (slackPolicy && slackPolicy.policy_exists) ||
      !!doc.bot_token_source
    );
    slackCard.style.display = hasSlack ? '' : 'none';
    if (hasSlack) {
      renderSlackPolicy(slackPolicy, botId);
    } else {
      // Empty out the inner mount-point so a previously-visible state
      // for a different bot can't leak through if the operator
      // switches bots while the API is in-flight.
      const inner = document.getElementById('botcfg-slack');
      if (inner) inner.innerHTML = '';
    }
  }
  // The section header tracks the union of every channel-card's
  // visibility. Pattern is forward-compatible: when a second channel
  // card lands, add its `.botcfg-card[data-channel-card]` selector to
  // this scan and the header logic doesn't change.
  const section = document.getElementById('botcfg-section-channels');
  if (section) {
    const anyVisible = !!(slackCard && slackCard.style.display !== 'none');
    section.style.display = anyVisible ? '' : 'none';
  }
}

async function loadConfigBot() {
  const setupBody = document.getElementById('config-bot-setup-body');
  const profileBody = document.getElementById('config-bot-profile-body');
  const modelsEl = document.getElementById('botcfg-models');
  const compEl = document.getElementById('botcfg-compaction');
  const keywordsEl = document.getElementById('botcfg-keywords');
  const fullEl = document.getElementById('botcfg-full');
  const botId = _configBot;
  if (!botId) {
    setupBody.innerHTML = '';
    profileBody.innerHTML = `<div class="subtle">Select a bot…</div>`;
    renderBotRenameCard(null);
    renderBotPurposeCard(null);
    renderBotHandoverCard(null);
    renderBotSetupChecklistCard(null);
    renderBotHealCard(null);
    renderBotContinuityCard(null);
    renderBotCustomizationsCard(null);
    return;
  }
  renderBotRenameCard(botId);
  renderBotPurposeCard(botId);
  renderBotHandoverCard(botId);
  renderBotSetupChecklistCard(botId);
  renderBotHealCard(botId);
  renderBotContinuityCard(botId);
  renderBotCustomizationsCard(botId);
  const q = `?bot=${encodeURIComponent(botId)}`;
  try {
    const [setupResp, profileResp, models, compaction, full, keywords, slackPolicy] = await Promise.all([
      fetch(`/api/arbiter/bot-setup/${encodeURIComponent(botId)}`),
      fetch(`/api/arbiter/profile/${encodeURIComponent(botId)}`),
      api('GET', `/api/bot/models${q}`),
      api('GET', `/api/bot/compaction${q}`),
      api('GET', `/api/bot/config${q}`),
      api('GET', `/api/classifier/keywords${q}`),
      api('GET', `/api/bot/slack-policy${q}`),
    ]);
    const setupJson = await setupResp.json();
    const profileJson = await profileResp.json();

    if (!setupJson.ok) {
      setupBody.innerHTML = `<div class="card" style="padding:14px">Setup load error: ${escHtml(setupJson.error || 'unknown')}</div>`;
    } else {
      renderBotSetup(setupJson, 'config-bot-setup-body');
    }
    if (!profileJson.ok) {
      profileBody.innerHTML = `<div class="card" style="padding:14px">Profile load error: ${escHtml(profileJson.error || 'unknown')}</div>`;
    } else {
      renderProfile(profileJson, 'config-bot-profile-body');
    }

    // PR K (2026-06-01): renderers extracted to helpers so the model +
    // compaction cards can carry plain-language meaning + an explicit
    // "how to change this" affordance. Both editors live elsewhere
    // (AI Optimization for primary/fallbacks; Customizations / CLI
    // for compaction) — the cards here are read-with-jump-link.
    _renderBotModelsCard(modelsEl, models, botId);
    _renderBotCompactionCard(compEl, compaction, botId);

    if (keywords && !keywords.error) {
      renderClassifierKeywords(keywordsEl, keywords);
    } else {
      keywordsEl.innerHTML = `<div class="empty">${escHtml((keywords && keywords.error) || 'Keyword data unavailable.')}</div>`;
    }

    fullEl.textContent = full.error ? full.error : JSON.stringify(full, null, 2);

    // PR K (2026-06-01): hide channel-specific cards when this bot
    // doesn't have that integration. For Slack: rendering the policy
    // card on a Telegram-only bot is just noise (the prior renderer
    // dumped an "empty" or error state into the card). The card mount
    // point is `#botcfg-slack-card`; the section header is shared
    // across all channel cards and shows iff any card in it is visible.
    _applyChannelCardVisibility(slackPolicy, botId);

    // Snapshot for the evo chat drawer's settings page-context pack.
    // Captures whichever bot the operator has selected on Settings →
    // Pod Config → Bot, plus the cards that just rendered: bot setup
    // (archetype/cadence/caps/timezone), models (primary + fallbacks),
    // compaction settings, and the slack-policy doctor state. Closes
    // the gap where an operator on (say) team_bot_a's bot-detail page asks
    // evo "tell me about team_bot_a's setup" and evo has no structured
    // awareness of which bot is on screen.
    //
    // Spread `_prev` per the test_security_context_snapshot.py
    // sibling-preservation pattern — any future writer onto
    // `_evoContextSnapshots.settings` can co-exist without clobbering.
    try {
      window._evoContextSnapshots = window._evoContextSnapshots || {};
      const _prev = window._evoContextSnapshots.settings || {};
      // After the 2026-06-01 restructure this loader only fires from
      // Settings → Bots, so the active-subtab probe always returns
      // "bot". Field name kept as ``config_subtab`` for snapshot-shape
      // compatibility with the consumer in ``settings`` pack; value is
      // canonical "bot".
      const configSubtab = 'bot';
      const slackDoctor = (slackPolicy && slackPolicy.doctor) || {};
      window._evoContextSnapshots.settings = {
        ..._prev,
        bot_detail: {
          bot_id: botId,
          archetype: setupJson?.archetype || null,
          surfacing_cadence: setupJson?.surfacing_cadence || null,
          monthly_cap_usd: setupJson?.monthly_cap_usd ?? null,
          daily_warn_usd: setupJson?.daily_warn_usd ?? null,
          daily_hard_usd: setupJson?.daily_hard_usd ?? null,
          timezone: setupJson?.timezone || null,
          primary_model: models && !models.error ? (models.primary || null) : null,
          fallback_models: models && !models.error ? (models.fallbacks || []) : [],
          per_agent_models: models && !models.error ? (models.per_agent || {}) : {},
          compaction: (compaction && !compaction.error) ? compaction : null,
          slack_enabled: slackDoctor.slack_enabled ?? null,
          slack_transport_mode: slackDoctor.transport_mode || null,
          slack_drift_state: slackPolicy?.drift_state || null,
          slack_listening_channels: (slackDoctor.listening_channels || []).length,
          has_user_profile: !!(profileJson && profileJson.has_content),
        },
        config_subtab: configSubtab,
      };
      if (typeof _evoDrawerUpdateContextChip === 'function') _evoDrawerUpdateContextChip();
    } catch (_) {}
  } catch (e) {
    profileBody.innerHTML = `<div class="card" style="padding:14px">Request failed: ${escHtml(String(e))}</div>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Slack Policy card — renders the slack-doctor state + init/apply/save controls.
// Backend: packages/admin/evolve_admin/web/slack_routes.py
// ─────────────────────────────────────────────────────────────────────────────

function renderSlackPolicy(data, botId) {
  const el = document.getElementById('botcfg-slack');
  if (!el) return;
  if (!data || data.error) {
    el.innerHTML = `<div class="empty">${escHtml(data && data.error || 'Slack policy load failed')}</div>`;
    return;
  }
  const d = data.doctor || {};
  const sevColor = { fail: 'var(--red)', warn: 'var(--orange)', info: 'var(--teal)' };
  const sevMarker = { fail: '✗', warn: '⚠', info: 'ℹ' };

  // Provider state line
  const enabledHtml = d.slack_enabled === false
    ? `<span style="color:var(--red);font-weight:600">DISABLED</span>`
    : d.slack_enabled === true ? `<span style="color:var(--teal)">enabled</span>` : 'unknown';
  const providerBits = [
    enabledHtml,
    d.transport_mode ? `mode=${escHtml(d.transport_mode)}` : null,
    d.group_policy ? `groupPolicy=${escHtml(d.group_policy)}` : null,
    d.dm_policy ? `dmPolicy=${escHtml(d.dm_policy)}` : null,
    (d.transport_mode === 'http' && !d.has_signing_secret) ? `<span style="color:var(--orange)">signingSecret missing</span>` : null,
    (d.transport_mode === 'socket' && !d.has_app_token) ? `<span style="color:var(--orange)">appToken missing</span>` : null,
    d.streaming_mode ? `<span style="color:${d.streaming_mode === 'partial' ? 'var(--red)' : 'var(--text2)'}">streaming=${escHtml(d.streaming_mode)}${d.streaming_native_transport ? ' native' : ''}</span>` : null,
  ].filter(Boolean).join(' · ');

  // Drift / policy state banner
  let driftBanner = '';
  if (!data.policy_exists) {
    driftBanner = `<div style="background:var(--bg3);border-left:3px solid var(--teal);padding:8px 12px;margin-bottom:10px;font-size:0.85rem">No policy file yet — Phase 1 mode (openclaw.json is the source of truth). Click <b>Initialize policy</b> to bootstrap from current openclaw.json.</div>`;
  } else if (data.policy_load_error) {
    driftBanner = `<div style="background:var(--bg3);border-left:3px solid var(--red);padding:8px 12px;margin-bottom:10px;font-size:0.85rem;color:var(--red)">Policy file is malformed: ${escHtml(data.policy_load_error)}</div>`;
  } else if (data.drift_state === 'drifted') {
    driftBanner = `<div style="background:var(--bg3);border-left:3px solid var(--orange);padding:8px 12px;margin-bottom:10px;font-size:0.85rem"><b>openclaw.json has drifted from slack-policy.json.</b> Click <b>Apply</b> to re-render.</div>`;
  } else if (data.drift_state === 'in_sync') {
    driftBanner = `<div style="background:var(--bg3);border-left:3px solid var(--teal);padding:8px 12px;margin-bottom:10px;font-size:0.85rem"><span style="color:var(--teal)">✓ Policy and openclaw.json are in sync.</span></div>`;
  }

  // Listening channels
  const lc = d.listening_channels || [];
  let listeningHtml;
  if (lc.length === 0) {
    listeningHtml = `<div class="subtle" style="font-size:0.85rem">No channels configured.</div>`;
  } else {
    listeningHtml = `<table style="width:100%;font-size:0.85rem;border-collapse:collapse">
      <tr style="color:var(--text2);text-align:left"><th style="padding:4px 6px">Channel</th><th style="padding:4px 6px">@-mention</th><th style="padding:4px 6px">Joined?</th></tr>
      ${lc.map(c => `<tr>
        <td style="padding:3px 6px;font-family:monospace">${escHtml(c.display_name ? '#' + c.display_name : c.channel_id)} <span style="color:var(--text2)">(${escHtml(c.channel_id)})</span></td>
        <td style="padding:3px 6px">${c.require_mention ? 'required' : '<span style="color:var(--orange)">listens-all</span>'}</td>
        <td style="padding:3px 6px">${c.is_joined ? '<span style="color:var(--teal)">✓</span>' : '<span style="color:var(--red)">✗ not joined</span>'}</td>
      </tr>`).join('')}
    </table>`;
  }

  const jnl = d.joined_not_listening || [];
  const joinedNotListeningHtml = jnl.length
    ? `<div style="margin-top:8px;font-size:0.85rem;color:var(--text2)"><b>Joined but not in policy (${jnl.length}):</b> ${jnl.map(c => escHtml('#' + (c.display_name || c.channel_id))).join(', ')}</div>`
    : '';

  // Feature bundles
  const bundles = d.feature_bundles || [];
  const enabledBundles = bundles.filter(b => b.enabled);
  const partialBundles = bundles.filter(b => !b.enabled && b.scopes.some(s => (d.oauth_scopes || []).includes(s)));
  const featureHtml = bundles.length === 0
    ? `<div class="subtle" style="font-size:0.85rem">Scopes unavailable (auth.test didn't return).</div>`
    : `
      <div style="font-size:0.85rem;color:var(--text2);margin-bottom:4px">Features enabled (${enabledBundles.length}/${bundles.length}):</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px">
        ${enabledBundles.map(b => `<span style="background:var(--bg3);padding:2px 8px;border-radius:3px;font-size:0.78rem"><span style="color:var(--teal)">✓</span> ${escHtml(b.name)}</span>`).join('')}
      </div>
      ${partialBundles.length ? `
        <div style="font-size:0.85rem;color:var(--orange);margin-top:8px;margin-bottom:4px"><b>Missing scopes for ${partialBundles.length} feature(s):</b></div>
        ${partialBundles.map(b => `<div style="font-size:0.78rem;margin-left:8px">
          <span style="color:var(--orange)">✗</span> ${escHtml(b.name)} <span style="color:var(--text2)">(add: ${b.missing.map(s => `<code style="background:var(--bg3);padding:1px 4px;border-radius:3px">${escHtml(s)}</code>`).join(' ')})</span>
        </div>`).join('')}
      ` : ''}
      ${(d.elevated_scopes_granted || []).length ? `
        <div style="font-size:0.85rem;color:var(--text2);margin-top:8px"><b>Elevated scopes granted (${d.elevated_scopes_granted.length}):</b> ${d.elevated_scopes_granted.map(s => `<code style="background:var(--bg3);padding:1px 4px;border-radius:3px;font-size:0.75rem">${escHtml(s)}</code>`).join(' ')}<br><span style="color:var(--text2);font-size:0.78rem">These widen the blast radius if the token leaks. Review whether each is required.</span></div>
      ` : ''}
    `;

  // Findings
  const findings = d.findings || [];
  const findingsHtml = findings.length === 0
    ? ''
    : `<div style="margin-top:10px"><div style="font-size:0.85rem;color:var(--text2);margin-bottom:4px">Findings:</div>
        ${findings.map(f => `<div style="font-size:0.85rem;margin:3px 0;padding:4px 8px;background:var(--bg3);border-left:2px solid ${sevColor[f.severity] || 'var(--text2)'}">
          <span style="color:${sevColor[f.severity] || 'var(--text2)'}">${sevMarker[f.severity] || '•'} ${escHtml(f.code)} (${escHtml(f.severity)})</span> ${escHtml(f.title)}
          <div style="color:var(--text2);font-size:0.78rem;margin-top:2px">${escHtml(f.detail)}</div>
        </div>`).join('')}
      </div>`;

  // Other providers warning
  const otherProvHtml = (d.other_provider_keys || []).length
    ? `<div style="margin-top:8px;font-size:0.85rem;color:var(--text2)"><b>Other providers in openclaw.json:</b> ${d.other_provider_keys.map(escHtml).join(', ')}</div>`
    : '';

  // Buttons
  const hasFail = d.has_fail;
  const initBtn = !data.policy_exists
    ? `<button class="btn primary" onclick="slackPolicyInit('${escHtml(botId)}')">Initialize policy from openclaw.json</button>`
    : '';
  const applyBtn = data.policy_exists && data.drift_state === 'drifted'
    ? `<button class="btn primary" onclick="slackPolicyApply('${escHtml(botId)}')" ${hasFail ? 'disabled title="Doctor reports FAIL findings; resolve those first"' : ''}>Apply to openclaw.json</button>`
    : '';
  const refreshBtn = `<button class="btn" onclick="loadConfigBot()">Refresh</button>`;
  const buttonRow = (initBtn || applyBtn || refreshBtn)
    ? `<div style="display:flex;gap:8px;margin-top:14px">${initBtn}${applyBtn}${refreshBtn}</div>`
    : '';

  // Workspace identity line
  const workspaceLine = d.workspace_name
    ? `<div style="font-size:0.85rem;margin-bottom:4px"><b>Workspace:</b> ${escHtml(d.workspace_name)} <span style="color:var(--text2)">(team_id ${escHtml(d.workspace_team_id || '?')})</span></div>`
    : '';

  el.innerHTML = `
    ${driftBanner}
    ${workspaceLine}
    <div style="font-size:0.85rem;margin-bottom:12px"><b>Provider:</b> ${providerBits || 'no slack block in openclaw.json'}</div>

    <div style="margin-bottom:12px">
      <div style="font-size:0.85rem;color:var(--text2);margin-bottom:4px">Listening (${lc.length}):</div>
      ${listeningHtml}
      ${joinedNotListeningHtml}
    </div>

    ${d.allow_from_count ? `<div style="font-size:0.85rem;margin-bottom:12px"><b>User allowlist (allowFrom):</b> ${d.allow_from_count} user(s)</div>` : ''}

    <div style="margin-bottom:12px">${featureHtml}</div>

    ${otherProvHtml}

    ${findingsHtml}

    ${buttonRow}
  `;
}

async function slackPolicyInit(botId) {
  if (!await confirmModal(`Initialize slack-policy.json for ${botLabel(botId)} from its current openclaw.json?\n\nThis will create a new policy file at shared_dir/bots/${botId}/slack-policy.json that captures the bot's current channel allowlist + user allowlist.`)) return;
  try {
    const resp = await api('POST', `/api/bot/slack-policy/init?bot=${encodeURIComponent(botId)}`, {});
    if (resp.error) {
      toast(`Init failed: ${resp.error}\n\n${resp.detail || ''}${resp.fails ? '\n\nFAIL findings:\n' + resp.fails.map(f => `  - ${f.code}: ${f.title}`).join('\n') : ''}`, 'err');
    } else {
      toast(`Policy created at ${resp.path}.\n\nThe bot's openclaw.json is unchanged — click "Apply" if you want to re-render after editing the policy.`, 'ok');
      loadConfigBot();
    }
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

async function slackPolicyApply(botId) {
  if (!await confirmModal(`Apply slack-policy.json to ${botLabel(botId)}'s openclaw.json?\n\nThis will re-render the channels.slack section. Other providers (telegram, etc.) and non-Slack fields are preserved.`)) return;
  try {
    const resp = await api('POST', `/api/bot/slack-policy/apply?bot=${encodeURIComponent(botId)}`, {});
    if (resp.error) {
      toast(`Apply failed: ${resp.error}\n\n${resp.detail || ''}${resp.fails ? '\n\nFAIL findings:\n' + resp.fails.map(f => `  - ${f.code}: ${f.title}`).join('\n') : ''}`, 'err');
    } else if (!resp.written) {
      toast('Already up to date — no changes needed.', 'ok');
    } else {
      const parts = [];
      if (resp.added_channel_ids.length) parts.push(`added: ${resp.added_channel_ids.join(', ')}`);
      if (resp.updated_channel_ids.length) parts.push(`updated: ${resp.updated_channel_ids.join(', ')}`);
      if (resp.removed_channel_ids.length) parts.push(`removed: ${resp.removed_channel_ids.join(', ')}`);
      toast(`openclaw.json updated.\n\n${parts.join('\n') || 'No channel-level changes.'}`, 'ok');
      loadConfigBot();
    }
  } catch (e) {
    toast(`Request failed: ${e}`, 'err');
  }
}

function renderClassifierKeywords(el, data) {
  if (!el) return;
  const base = data.base || {};
  const cal = data.calibration || {};
  const hints = data.network_hints || {};
  const calibrationWired = !!data.calibration_writer_implemented;
  const scope = data.scope || 'pod-wide';

  function chips(words, cls) {
    if (!Array.isArray(words) || !words.length) return '<span class="subtle" style="font-size:0.78rem">none</span>';
    return words.map(w => `<span class="${cls}" style="font-family:monospace;font-size:0.74rem;padding:2px 6px;border-radius:3px;margin:2px 4px 2px 0;display:inline-block;background:var(--bg3)">${escHtml(w)}</span>`).join('');
  }

  function section(title, kws, calAdd, calRemove, hintExtra, helpText) {
    const calHtml = (calAdd && calAdd.length) || (calRemove && calRemove.length)
      ? `<div style="margin-top:6px;font-size:0.78rem"><span style="color:var(--text2)">calibration deltas:</span> ${chips(calAdd, 'key-tag')}${calRemove && calRemove.length ? ` <span style="color:var(--text2)">remove:</span> ${chips(calRemove, 'key-tag')}` : ''}</div>`
      : '';
    const hintHtml = hintExtra && hintExtra.length
      ? `<div style="margin-top:6px;font-size:0.78rem"><span style="color:var(--text2)">network.json hints:</span> ${chips(hintExtra, 'key-tag')}</div>`
      : '';
    return `<div style="margin-bottom:14px">
      <div style="font-size:0.82rem;margin-bottom:6px;color:var(--text)"><strong>${escHtml(title)}</strong> <span class="subtle" style="font-size:0.74rem">${escHtml(helpText)}</span></div>
      <div>${chips(kws, 'key-tag')}</div>
      ${calHtml}
      ${hintHtml}
    </div>`;
  }

  const note = !calibrationWired
    ? `<div class="subtle" style="font-size:0.78rem;margin-bottom:10px">Scope: <strong>${escHtml(scope)}</strong>. The keyword-calibration writer is not yet implemented — the lists below are the hardcoded base from <code style="background:var(--bg3);padding:1px 4px;border-radius:3px">${escHtml(data.source_file || 'TierClassifier.ts')}</code>. Per-bot calibration is a planned follow-up.</div>`
    : `<div class="subtle" style="font-size:0.78rem;margin-bottom:10px">Scope: <strong>${escHtml(scope)}</strong>. Lists below are base + RSI-learned calibration deltas.</div>`;

  el.innerHTML = note +
    section('Productive (TIER1)', base.productive || [], cal.productive_add, cal.productive_remove, hints.productive_extra, '— maps to no override; bot keeps its default model') +
    section('Maintenance (TIER2)', base.maintenance || [], cal.maintenance_add, cal.maintenance_remove, hints.maintenance_extra, '— maps to tier3 (Haiku) for the next assistant turn') +
    section('Correction patterns', base.correction || [], cal.correction_add, cal.correction_remove, [], '— signals the user is correcting the assistant');
}

// ══════════════════════════════════════════════════════
// Pod Config
// ══════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Security Audit
// ══════════════════════════════════════════════════════
let _auditData = null;
let _secMutedData = {};       // {bot_id: [msg, ...]} — loaded from server
// V1.5-3 chip data: {bot_id: {upstream_version, exec_policy_compliant, exec_policy_reason}}
// Loaded lazily alongside the audit so the chips appear above each bot's findings.
let _secSafetyData = null;
// Per-bot count of open safety-dimension RSI proposals, surfaced on each
// bot tile so security work-in-flight is visible alongside audit score.
let _secProposalsByBot = {};

async function _secLoadProposals() {
  try {
    const r = await fetch('/api/arbiter/proposals?include=pending,snoozed&dimension=safety');
    const j = await r.json();
    const counts = {};
    for (const p of ((j && j.ok && j.proposals) || [])) {
      const b = p.bot_id;
      if (!b) continue;
      counts[b] = (counts[b] || 0) + 1;
    }
    _secProposalsByBot = counts;
  } catch (_e) {
    _secProposalsByBot = {};
  }
}

function _secJumpToSecurityProposals(botId) {
  // Every safety-dimension generator carries surface in
  // {cleanup, drift, firing} (no improvement-surface safety
  // generators exist on this pod), so the proposals live on
  // Reports → Alerts after the 2026-06-04 surface flip. Pass a
  // non-improvement surface hint so jumpToProposals routes there
  // instead of defaulting to the (now empty) Recommendations page.
  jumpToProposals('safety', botId, 'cleanup');
}

async function _secLoadMuted() {
  try { _secMutedData = await api('GET', '/api/security/muted') || {}; }
  catch { /* keep existing */ }
}

function _secMutedSet(bot) {
  return new Set(_secMutedData[bot] || []);
}

async function _secMute(bot, msg) {
  if (!_secMutedData[bot]) _secMutedData[bot] = [];
  if (!_secMutedData[bot].includes(msg)) _secMutedData[bot].push(msg);
  renderSecurityAudit();
  api('POST', '/api/security/muted', {action: 'mute', bot, message: msg});
}

async function _secUnmuteAll(bot) {
  if (bot) delete _secMutedData[bot]; else _secMutedData = {};
  renderSecurityAudit();
  api('POST', '/api/security/muted', bot ? {action: 'clear', bot} : {action: 'clear'});
}

// Detect a Flask 500 error response masquerading as audit data.
// The global error handler returns {error, type, trace} — a real audit
// result never has a "trace" key at the top level.
function _isAuditErrorResponse(d) {
  return d && typeof d === 'object' && 'trace' in d && 'type' in d && 'error' in d;
}

let _auditPollTimer = null;

function _auditStartPolling() {
  if (_auditPollTimer) return;
  _auditPollTimer = setInterval(_auditPollRefresh, 2000);
}

function _auditStopPolling() {
  if (_auditPollTimer) { clearInterval(_auditPollTimer); _auditPollTimer = null; }
}

async function _auditPollRefresh() {
  const [snap, status] = await Promise.all([
    api('GET', '/api/security/audit'),
    api('GET', '/api/security/audit/refresh/status'),
  ]);
  if (snap && snap.data && Object.keys(snap.data).length) {
    _auditData = snap.data;
    renderSecurityAudit();
  }
  const done = status?.done ?? 0;
  const total = status?.total ?? 0;
  const running = status?.running || snap?.running || false;
  if (running) {
    const progressEl = document.getElementById('audit-progress');
    if (progressEl) {
      progressEl.style.display = '';
      progressEl.textContent = `Auditing… ${done}/${total}`;
    }
  } else {
    _auditStopPolling();
    const progressEl = document.getElementById('audit-progress');
    if (progressEl) progressEl.style.display = 'none';
    const btn = document.getElementById('audit-run-btn');
    if (btn) { btn.textContent = 'Run Audit'; btn.disabled = false; }
    if (snap?.cached_at) {
      document.getElementById('audit-last-run').textContent =
        'Last run: ' + new Date(snap.cached_at * 1000).toLocaleTimeString();
    }
  }
}

async function _loadSecurityAuditSignals() {
  // Phase 3 of the alerts/signal-store consolidation
  // (docs/spec-alerts-signal-store-2026-05-07.md): audit findings live
  // in the Signal store. This contextual strip reads firing audit
  // signals via /api/signals and renders one chip per finding. Click
  // jumps to Alerts/Maintenance for snooze/dismiss/resolve.
  const strip = document.getElementById('security-audit-signals-strip');
  if (!strip) return;
  try {
    const d = await api('GET', '/api/signals?producer=audit&limit=50');
    const sigs = (d && d.signals) || [];
    if (!sigs.length) {
      strip.style.display = 'none';
      strip.innerHTML = '';
      return;
    }
    // Collapse duplicates by title: the same finding often fires on
    // multiple bots, and 20 near-identical chips are unreadable.
    const groups = new Map();
    for (const s of sigs) {
      const key = s.title || '(untitled)';
      const g = groups.get(key);
      if (!g) {
        groups.set(key, { title: key, severity: s.severity, last: s.last_observed_at, count: 1, body: s.body || '' });
      } else {
        g.count += 1;
        if ((s.last_observed_at || 0) > (g.last || 0)) g.last = s.last_observed_at;
        const order = { alert: 3, warn: 2, info: 1 };
        if ((order[s.severity] || 0) > (order[g.severity] || 0)) g.severity = s.severity;
      }
    }
    const sevOrder = { alert: 3, warn: 2, info: 1 };
    const grouped = [...groups.values()].sort((a, b) =>
      (sevOrder[b.severity] || 0) - (sevOrder[a.severity] || 0) || (b.last || 0) - (a.last || 0)
    );
    const counts = sigs.reduce((acc, s) => { acc[s.severity] = (acc[s.severity] || 0) + 1; return acc; }, {});
    const sevSummary = ['alert', 'warn', 'info']
      .filter(k => counts[k])
      .map(k => `${counts[k]} ${k}`)
      .join(' · ');
    // Alerts live under Reports · Alerts.
    const jumpJs = "document.querySelector('.nav-item[data-page=\\'reports\\']').click(); setTimeout(()=>{document.querySelector('#page-reports .subtab[data-subtab=\\'alerts\\']')?.click();}, 80); return false;";
    const dot = (sev) => `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${sev==='alert'?'#e54':sev==='warn'?'#eb4':'#888'};flex-shrink:0"></span>`;
    const chip = (g) => `
      <a href="#" onclick="${jumpJs}"
         title="${escHtml(g.body)}"
         style="display:inline-flex;align-items:center;gap:6px;padding:3px 9px;background:var(--bg2,#1c1c1c);border:1px solid var(--border,#333);border-radius:999px;font-size:0.76rem;color:var(--text2,#ccc);text-decoration:none;max-width:340px">
        ${dot(g.severity)}
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(g.title)}</span>
        ${g.count > 1 ? `<span style="color:var(--text3);font-size:0.7rem;flex-shrink:0">×${g.count}</span>` : ''}
      </a>`;
    const TOP_N = 5;
    const top = grouped.slice(0, TOP_N);
    const moreCount = grouped.length - top.length;
    const moreLink = moreCount > 0
      ? `<a href="#" onclick="${jumpJs}" style="font-size:0.76rem;color:#7fc8ff;text-decoration:none;align-self:center">+${moreCount} more →</a>`
      : `<a href="#" onclick="${jumpJs}" style="font-size:0.76rem;color:#7fc8ff;text-decoration:none;align-self:center">Manage in Alerts →</a>`;
    strip.innerHTML = `
      <div class="card stripe-card is-warn" style="padding:10px 14px">
        <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px;gap:12px">
          <div style="font-size:0.82rem;font-weight:600;color:#eb4">⚠ Active audit signals (${sigs.length})${sevSummary ? ` — ${escHtml(sevSummary)}` : ''}</div>
          <div style="font-size:0.72rem;color:var(--text3)">from the signal store</div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
          ${top.map(chip).join('')}
          ${moreLink}
        </div>
      </div>`;
    strip.style.display = '';
  } catch (_e) {
    strip.style.display = 'none';
  }
}

async function loadSecurityAudit() {
  loadRelatedProposalsStrip('safety');
  await _secLoadMuted();
  // Render the canonical bot list immediately — bots without audit data
  // appear as "Pending" rather than being absent. Parallel loaders below
  // re-render once their data lands.
  renderSecurityAudit();
  // V1.5-3 chips: fetch in parallel; re-render once it lands so the chips
  // appear above each bot card without blocking the audit on the summary.
  api('GET', '/api/security/safety-summary').then(r => {
    if (r && r.bots) {
      _secSafetyData = r.bots;
      renderSecurityAudit();
    }
  }).catch(() => {});
  // Per-bot open safety-dimension proposal counts, surfaced on each tile.
  _secLoadProposals().then(() => {
    renderSecurityAudit();
  });
  const snap = await api('GET', '/api/security/audit');
  if (snap && snap.data && Object.keys(snap.data).length) {
    _auditData = snap.data;
    renderSecurityAudit();
    if (snap.cached_at) {
      document.getElementById('audit-last-run').textContent =
        'Last run: ' + new Date(snap.cached_at * 1000).toLocaleTimeString();
    }
    // Snapshot for the evo chat drawer's security pack. Captures the
    // per-bot score + advisory counts + top findings so evo can answer
    // operator questions about specific bot advisories with the same
    // data the operator sees on screen (spec §3.4, reliability lever
    // #3 — closes the second failure mode the operator surfaced
    // 2026-05-19, where evo confused team_bot_a's plugin advisories with team_bot_b).
    try {
      window._evoContextSnapshots = window._evoContextSnapshots || {};
      const perBot = Object.entries(_auditData).map(([bot_id, d]) => {
        const findings = (d?.findings || []).slice(0, 5).map(f => ({
          code: f.code, severity: f.severity, title: f.title,
        }));
        return {
          bot_id,
          score: d?.score ?? null,
          critical: (d?.findings || []).filter(f => f.severity === 'critical').length,
          warn: (d?.findings || []).filter(f => f.severity === 'warn').length,
          info: (d?.findings || []).filter(f => f.severity === 'info').length,
          top_findings: findings,
        };
      });
      // Spread `_prev` so sibling fields (e.g. `backup_drift` written by
      // loadBackupConfig on the Backups subtab) survive this poll. Without
      // the spread, the periodic audit refresh clobbers backup-drift state
      // every few seconds — the 2026-05-20 transcript where evo reported
      // "drifted_bots=0" while the page clearly showed drift on multiple
      // bots was exactly this race.
      const _prev = window._evoContextSnapshots.security || {};
      window._evoContextSnapshots.security = {
        ..._prev,
        cached_at: snap?.cached_at || null,
        per_bot: perBot,
      };
      if (typeof _evoDrawerUpdateContextChip === 'function') _evoDrawerUpdateContextChip();
    } catch (_) {}
  } else if (!snap?.running) {
    // First-ever audit: kick off the refresh. The canonical bot list is
    // already rendered (with all bots in "Pending" state) from the eager
    // renderSecurityAudit() call above, so no panel-replacement needed.
    await api('POST', '/api/security/audit/refresh');
  }
  if (snap?.running || !snap?.cached_at) {
    const btn = document.getElementById('audit-run-btn');
    if (btn) { btn.textContent = 'Running...'; btn.disabled = true; }
    const progressEl = document.getElementById('audit-progress');
    if (progressEl) progressEl.style.display = '';
    _auditStartPolling();
  }
}

async function runSecurityAudit() {
  const btn = document.getElementById('audit-run-btn');
  if (btn) { btn.textContent = 'Running...'; btn.disabled = true; }
  const progressEl = document.getElementById('audit-progress');
  if (progressEl) { progressEl.style.display = ''; progressEl.textContent = 'Starting…'; }
  await api('POST', '/api/security/audit/refresh');
  _auditStartPolling();
}

function _secScrollToBot(bot) {
  const card = document.getElementById(`sec-card-${bot}`);
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSecurityAudit() {
  const el = document.getElementById('security-audit-panel');
  // Bot list comes from the canonical pod state (status/network) — NOT from
  // _auditData. A newly-added bot should appear in the tabs with a "Pending —
  // not yet run" state even before its first audit completes, rather than
  // being invisible until Run Audit is clicked.
  const canonicalBots = orderedBotIds(
    (_statusData && _statusData.bots) || (_networkData && _networkData.bots)
  );
  if (!canonicalBots.length) {
    el.innerHTML = `<div class="empty" style="padding:20px 0">
      No bots in this pod yet.
      <div style="font-size:0.78rem;color:var(--text3);margin-top:6px">
        Add a bot to see its security audit here.
      </div>
    </div>`;
    return;
  }

  const credKeywords = ['credentials', 'chmod', '700', 'permission', '.secrets', '.env'];

  // V1.5-3: per-bot chips for OpenClaw version + exec-policy compliance.
  // Floor matches DEFAULT_MINIMUM_VERSION in packages/admin/evolve_admin/upstream_version.py
  // (v2026.4.12 — the release that shipped `openclaw exec-policy`).
  const _SEC_VER_FLOOR = [2026, 4, 12];
  function _secVerCmp(v) {
    const m = /^(\d+)\.(\d+)\.(\d+)/.exec(String(v || ''));
    return m ? [+m[1], +m[2], +m[3]] : null;
  }
  function _secVerAtFloor(v) {
    const p = _secVerCmp(v);
    if (!p) return null;
    for (let i = 0; i < 3; i++) {
      if (p[i] > _SEC_VER_FLOOR[i]) return true;
      if (p[i] < _SEC_VER_FLOOR[i]) return false;
    }
    return true;
  }
  function _secChipsFor(bot) {
    const s = _secSafetyData && _secSafetyData[bot];
    if (!s) return '';
    const chips = [];
    if (s.upstream_version) {
      const above = _secVerAtFloor(s.upstream_version);
      const cls = above === false ? 'badge-yellow' : (above === true ? 'badge-gray' : 'badge-gray');
      const tip = above === false
        ? `Below the v${_SEC_VER_FLOOR.join('.')} floor — upgrade recommended (enables openclaw exec-policy)`
        : 'OpenClaw runtime version (meta.lastTouchedVersion)';
      chips.push(`<span class="badge ${cls}" title="${escHtml(tip)}">OC ${escHtml(s.upstream_version)}</span>`);
    }
    if (s.exec_policy_compliant === true) {
      const tip = s.exec_policy_reason || 'tools.exec scoped (compatible with openclaw exec-policy)';
      chips.push(`<span class="badge badge-green" title="${escHtml(tip)}">exec-policy: scoped</span>`);
    } else if (s.exec_policy_compliant === false) {
      const tip = s.exec_policy_reason || 'tools.exec.security="full" with no allowlist — permissive';
      chips.push(`<span class="badge badge-yellow" title="${escHtml(tip)}">exec-policy: permissive</span>`);
    }
    return chips.length
      ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin:0 0 10px 0">${chips.join('')}</div>`
      : '';
  }

  function _fixCmd(f) {
    const text = (f.message + ' ' + (f.recommendation||'')).toLowerCase();
    if (text.includes('credentials') && (text.includes('chmod') || text.includes('700'))) return `chmod 700 ~/.openclaw/credentials`;
    if (text.includes('permission') && text.includes('.openclaw')) return `chmod 700 ~/.openclaw`;
    return null;
  }

  // "What apps collect & who can reach them" — structured projection from
  // each manifest's privacy{} / audience_scoping{} blocks (manifest-v7
  // Slice 2; U4.3's data source). Renders the structured fields verbatim —
  // no prose synthesis.
  function _dataBoundaryHtml(d) {
    const bounds = d.app_data_boundaries || [];
    if (!bounds.length) return '';
    const declaredCount = bounds.filter(b => b.privacy_declared || b.audience_declared).length;
    const rows = bounds.map(b => {
      const name = escHtml(b.name || b.app_id);
      const statusChip = (b.status && b.status !== 'active')
        ? ` <span style="font-size:0.7rem;color:var(--text3)">(${escHtml(b.status)})</span>` : '';
      if (!b.privacy_declared && !b.audience_declared) {
        return `<div style="padding:5px 0;border-bottom:1px solid var(--border)">
          <span style="font-size:0.8rem;font-weight:600">${name}</span>${statusChip}
          <span style="font-size:0.75rem;color:var(--text3);margin-left:8px">privacy / audience not yet declared</span>
        </div>`;
      }
      const collects = (b.collects || []).length
        ? (b.collects || []).map(c => `<span style="font-size:0.72rem;background:var(--bg3);border-radius:4px;padding:1px 6px;margin-right:4px">${escHtml(c)}</span>`).join('')
        : `<span style="font-size:0.72rem;color:var(--text3)">nothing declared as collected</span>`;
      const reachParts = [];
      if (b.operator) reachParts.push(escHtml(b.operator.replace(/_/g, ' ')));
      if ((b.approved_surfaces || []).length) reachParts.push('surfaces: ' + b.approved_surfaces.map(escHtml).join(', '));
      if ((b.roles || []).length) reachParts.push('roles: ' + b.roles.map(escHtml).join(', '));
      if (b.group_trigger_count) reachParts.push(`${b.group_trigger_count} group-surface trigger${b.group_trigger_count === 1 ? '' : 's'}`);
      const handlingParts = [];
      if (b.consent_notice) handlingParts.push(`consent notice: “${escHtml(b.consent_notice)}”`);
      if (b.opt_out_command) handlingParts.push(`opt-out: <code>${escHtml(b.opt_out_command)}</code>`);
      if (b.retention_days != null) handlingParts.push(`retention: ${escHtml(String(b.retention_days))}d`);
      handlingParts.push(`lessons sharing: ${b.shareable_in_lessons ? 'allowed' : 'off'}`);
      return `<div style="padding:5px 0;border-bottom:1px solid var(--border)">
        <div><span style="font-size:0.8rem;font-weight:600">${name}</span>${statusChip}</div>
        <div style="margin-top:2px">${collects}</div>
        ${reachParts.length ? `<div style="font-size:0.72rem;color:var(--text2);margin-top:2px">Reach: ${reachParts.join(' · ')}</div>` : ''}
        <div style="font-size:0.72rem;color:var(--text3);margin-top:2px">${handlingParts.join(' · ')}</div>
      </div>`;
    }).join('');
    return `
      <details style="margin-bottom:8px">
        <summary style="display:flex;align-items:center;gap:8px;font-size:0.78rem;color:var(--text2);cursor:pointer"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>What apps collect &amp; who can reach them <span style="color:var(--text3)">— ${declaredCount}/${bounds.length} declared</span></summary>
        <div style="margin-top:6px">${rows}</div>
      </details>`;
  }

  function _botCard(bot, d) {
    const muted = _secMutedSet(bot);
    const isUnavailable = d.unavailable || d.error;
    const isPending = !d.run_at && !isUnavailable;
    const sc = d.score ?? null;
    const scoreColor = sc == null ? 'var(--text3)' : sc >= 80 ? 'var(--green)' : sc >= 60 ? 'var(--yellow)' : 'var(--red)';
    const findings = (d.findings || []).filter(f => f.severity !== 'info');
    const critFindings = findings.filter(f =>
      f.severity === 'critical' || credKeywords.some(k => (f.message || '').toLowerCase().includes(k))
    );
    const advFindings = findings.filter(f => !critFindings.includes(f));
    const visibleAdvisories = advFindings.filter(f => !muted.has(f.message));
    const mutedCount = advFindings.length - visibleAdvisories.length;

    const statusBadge = isUnavailable
      ? `<span style="color:var(--text3);font-size:0.78rem">Unavailable</span>`
      : isPending
        ? `<span style="color:var(--blue);font-size:0.78rem">🔵 Pending — not yet run</span>`
        : critFindings.length
          ? `<span style="color:var(--red);font-size:0.78rem">⛔ ${critFindings.length} critical</span>`
          : advFindings.length
            ? `<span style="color:var(--yellow);font-size:0.78rem">⚠ ${advFindings.length} advisory</span>`
            : `<span style="color:var(--green);font-size:0.78rem">✓ Healthy</span>`;

    const lastRun = d.run_at ? `<span style="font-size:0.72rem;color:var(--text3);margin-left:10px">${ago(d.run_at)}</span>` : '';
    const rerunBtn = `<button class="btn btn-ghost btn-sm" style="font-size:0.7rem" onclick="_secRerun('${escHtml(bot)}')">Re-run audit</button>`;

    const critHtml = critFindings.length ? `
      <div style="background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.25);border-radius:6px;padding:10px;margin-bottom:8px">
        <div style="font-size:0.7rem;font-weight:700;letter-spacing:.05em;color:var(--red);margin-bottom:6px">⛔ CRITICAL — ACTION REQUIRED</div>
        ${critFindings.map(f => {
          const cmd = _fixCmd(f);
          const copyBtn = cmd ? `<button class="btn btn-sm" style="margin-top:5px;background:var(--red);color:#fff;font-size:0.7rem" onclick="navigator.clipboard.writeText('${escHtml(cmd)}').then(()=>toast('Copied','ok'))">📋 Copy fix command</button>` : '';
          return `<div style="padding:5px 0;border-bottom:1px solid rgba(248,113,113,0.15)">
            <div style="font-size:0.82rem">${escHtml(f.message)}</div>
            ${f.recommendation ? `<div style="font-size:0.75rem;color:var(--text2);margin-top:2px">${escHtml(f.recommendation)}</div>` : ''}
            ${copyBtn}
          </div>`;
        }).join('')}
      </div>` : '';

    const advHtml = visibleAdvisories.length ? `
      <div style="margin-bottom:6px">
        <div style="font-size:0.7rem;font-weight:700;letter-spacing:.05em;color:var(--yellow);margin-bottom:6px">⚠ ADVISORIES <span style="font-weight:400;color:var(--text3)">— informational only</span></div>
        ${visibleAdvisories.map(f => {
          const muteBtn = `<button onclick="_secMute('${escHtml(bot)}','${escHtml(f.message)}')" style="font-size:0.68rem;color:var(--text3);background:none;border:none;cursor:pointer;padding:0;margin-left:8px" title="Mute this advisory">Mute</button>`;
          return `<div style="padding:5px 0;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:8px">
            <span style="flex-shrink:0;color:var(--yellow)">⚠</span>
            <div style="flex:1">
              <div style="font-size:0.8rem">${escHtml(f.message)}</div>
              ${f.recommendation ? `<div style="font-size:0.72rem;color:var(--text2);margin-top:2px">${escHtml(f.recommendation)}</div>` : ''}
            </div>
            <div style="display:flex;flex-direction:column;align-items:flex-end;flex-shrink:0">
              <span style="font-size:0.65rem;color:var(--text3)">advisory</span>
              ${muteBtn}
            </div>
          </div>`;
        }).join('')}
        ${mutedCount > 0 ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">${mutedCount} muted. <button onclick="_secUnmuteAll('${escHtml(bot)}')" style="font-size:0.68rem;color:var(--text3);background:none;border:none;cursor:pointer;padding:0;text-decoration:underline">Show all</button></div>` : ''}
      </div>` : (mutedCount > 0 ? `<div style="font-size:0.72rem;color:var(--text3);margin-bottom:6px">${mutedCount} muted. <button onclick="_secUnmuteAll('${escHtml(bot)}')" style="font-size:0.68rem;color:var(--text3);background:none;border:none;cursor:pointer;padding:0;text-decoration:underline">Show all</button></div>` : '');

    const _auditErrType = d.error_type || '';
    const _auditHint = _auditErrType === 'config_invalid'
      ? `The bot's openclaw.json contains keys that are no longer valid in the current openclaw schema (e.g. top-level <code>heartbeat</code>, <code>compaction</code>, <code>contextPruning</code>). An auto-repair via <code>openclaw doctor --fix</code> was attempted but did not fully resolve the issue. Run it manually: <code>sudo -u ${escHtml(bot)} env HOME=/Users/${escHtml(bot)} sh -c 'cd /Users/${escHtml(bot)} && openclaw doctor --fix'</code>, then click "Re-run audit".`
      : _auditErrType === 'timeout'
        ? `The audit command ran but took too long (openclaw security audit typically takes ~20s per bot; with many bots the server timed out waiting). Click "Re-run audit" to retry this bot individually, or run manually: <code>sudo -H -u ${escHtml(bot)} /opt/homebrew/bin/openclaw security audit --json</code>`
        : `This usually means: openclaw isn't in PATH for this bot user, permissions prevent running the audit, or the bot hasn't been set up yet. Run the audit manually as that user, or check Maintenance → Cron for PATH/permissions errors.`;
    const unavailHtml = isUnavailable ? `
      <div style="padding:10px;background:var(--bg2);border-radius:6px;font-size:0.8rem;color:var(--text2)">
        <strong>Audit unavailable for ${escHtml(botLabel(bot))}.</strong>
        ${d.error ? `<div style="margin-top:4px;color:var(--text3);font-size:0.75rem">${escHtml(String(d.error))}</div>` : ''}
        <div style="margin-top:6px;font-size:0.75rem;color:var(--text3)">${_auditHint}</div>
      </div>` : '';

    const pendingHtml = isPending && !isUnavailable ? `
      <div style="padding:8px 10px;background:var(--bg2);border-radius:6px;font-size:0.78rem;color:var(--text2)">
        🔵 <strong>Not yet run.</strong> Click "Re-run audit" to start the first security scan for this bot.
      </div>` : '';

    const noFindingsHtml = !isUnavailable && !isPending && !findings.length
      ? `<div style="font-size:0.8rem;color:var(--green)">✓ No warnings or critical findings.</div>` : '';

    const debugContent = [
      d.command ? `Command: ${d.command}` : '',
      d.error ? `Error: ${d.error}` : '',
      d.run_at ? `Run at: ${d.run_at}` : '',
      d.env_note ? `Env: ${d.env_note}` : '',
    ].filter(Boolean).join('\n');
    const debugPane = debugContent ? `
      <details style="margin-top:8px">
        <summary style="display:flex;align-items:center;gap:8px;font-size:0.72rem;color:var(--text3);cursor:pointer"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Debug / raw output</summary>
        <pre style="font-size:0.72rem;color:var(--text3);background:var(--bg3);padding:8px;border-radius:4px;margin-top:6px;overflow:auto;white-space:pre-wrap">${escHtml(debugContent)}</pre>
      </details>` : '';

    return `
      <div class="card" id="sec-card-${escHtml(bot)}" style="margin-bottom:12px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
          <div style="display:flex;align-items:center;gap:10px">
            ${sc != null ? `<span style="font-size:1.6rem;font-weight:700;color:${scoreColor}">${sc}</span><span style="font-size:0.72rem;color:var(--text3)">/100</span>` : ''}
            <div>
              <div style="font-weight:600">${escHtml(botLabel(bot))}</div>
              <div style="display:flex;align-items:center;gap:6px">${statusBadge}${lastRun}</div>
            </div>
          </div>
          ${rerunBtn}
        </div>
        ${_secChipsFor(bot)}${unavailHtml}${pendingHtml}${critHtml}${advHtml}${noFindingsHtml}${_dataBoundaryHtml(d)}${debugPane}
      </div>`;
  }

  // Bots without an audit entry yet get an empty record so _botCard falls
  // through to its existing isPending path ("Pending — not yet run").
  const audit = _auditData || {};
  const botEntries = canonicalBots.map(b => [b, audit[b] || {}]);

  const summaryGrid = `
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px">
      ${botEntries.map(([bot, d]) => {
        const sc = d.score ?? null;
        const sc_color = sc == null ? 'var(--text3)' : sc >= 80 ? 'var(--green)' : sc >= 60 ? 'var(--yellow)' : 'var(--red)';
        const isPending = !d.run_at && !d.unavailable && !d.error;
        const crit = (d.findings||[]).filter(f => f.severity !== 'info' && (f.severity === 'critical' || credKeywords.some(k => (f.message || '').toLowerCase().includes(k)))).length;
        const propCount = (_secProposalsByBot && _secProposalsByBot[bot]) || 0;
        const propLine = propCount > 0
          ? `<div style="font-size:0.72rem;color:#7fc8ff;margin-top:4px;cursor:pointer" onclick="event.stopPropagation();_secJumpToSecurityProposals('${escHtml(bot)}')" title="View open security proposals for this bot">⬦ ${propCount} security proposal${propCount === 1 ? '' : 's'} →</div>`
          : '';
        const statusLine = isPending
          ? `<div style="font-size:0.72rem;color:var(--blue);margin-top:2px">🔵 Not yet audited</div>`
          : (crit > 0
              ? `<div style="font-size:0.75rem;color:var(--red);margin-top:2px">⛔ ${crit} critical</div>`
              : `<div style="font-size:0.75rem;color:var(--green);margin-top:2px">✓ No criticals</div>`);
        return `<div class="card" style="cursor:pointer;flex:1;min-width:120px;padding:10px 14px" onclick="_secScrollToBot('${escHtml(bot)}')" title="Jump to ${escHtml(botLabel(bot))}'s findings below">
          <div style="font-weight:600;margin-bottom:4px">${escHtml(botLabel(bot))}</div>
          <div style="font-size:1.3rem;font-weight:700;color:${sc_color}">${sc ?? '—'}<span style="font-size:0.7rem;color:var(--text3)">/100</span></div>
          ${statusLine}
          ${propLine}
        </div>`;
      }).join('')}
    </div>`;

  el.innerHTML = summaryGrid + botEntries.map(([bot, d]) => _botCard(bot, d)).join('');
}

async function _secRerun(botId) {
  toast(`Re-running audit for ${escHtml(botLabel(botId))}…`, 'ok');
  await api('POST', `/api/security/audit/refresh?bot=${encodeURIComponent(botId)}`);
  const btn = document.getElementById('audit-run-btn');
  if (btn) { btn.textContent = 'Running...'; btn.disabled = true; }
  const progressEl = document.getElementById('audit-progress');
  if (progressEl) { progressEl.style.display = ''; progressEl.textContent = `Auditing ${escHtml(botLabel(botId))}…`; }
  _auditStartPolling();
}
