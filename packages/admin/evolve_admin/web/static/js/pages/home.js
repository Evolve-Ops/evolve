// ════════════════════════════════════════════════════════════════════════
// Page: Home (evo conversation landing — Carpentry pass)
//
// The dashboard. Three regions stacked top-to-bottom:
//   1. Narrative card + chat surface — bot-led conversation with
//      tier-aware response (ask / auto-small / auto-power).
//   2. Ribbon — the "since you were here" counts (proposals, signals,
//      drift) keyed off a localStorage-backed last-seen timestamp.
//   3. Rail (right side on desktop, bottom sheet on mobile) — host-
//      health card + per-bot chips + collapsible Pod Report.
//
// State (top of the file):
//   _homeLastSeenAtRender    — snapshot captured at render-time so the
//                              ribbon counts don't reset between in-
//                              session refreshes
//   _homeAutoActInFlight     — guard against re-entrant auto-act
//   HOME_LAST_SEEN_KEY,
//   HOME_TIER_KEY            — localStorage key constants
//
// Lookup tables:
//   _HOME_VALID_TIERS, _HOME_TIER_MIGRATIONS
//   _HOME_VALID_MODEL_TIERS
//   _HOME_EVO_OPEN_RE, _HOME_EVO_CLOSE_RE
//   _HOME_SEV_RANK, _HOME_URGENCY_RANK
//
// Loaders + renderers dispatched via onPageActivate('home') +
// nav-leaving hook:
//   loadHome()                 — entry point; renders chat, ribbon, rail
//   _homeWriteLastSeen()       — called from core/router.js's nav() on
//                                navigate-away-from-home (typeof guard)
//
// Chat surface (Home-tier; separate from the evo-drawer):
//   _homeChat* family — Load/Save/Append/Render/Send/Restore/Clear,
//   plus the keydown + composer auto-resize handlers.
//   _homeBuildOcSessionId     — derives per-session OC session id
//   _evoChatPendingIndicator  — shared helper for the "…still thinking"
//                               live indicator (also used by the evo-
//                               drawer; lives here because its first
//                               caller is the home composer).
//
// Ribbon + proposals/signals:
//   _homeRenderTopProposals, _homeRenderProposalRow
//   _homeProposalAct / _homeProposalSnooze / _homeProposalDismiss
//   _homeSignalDismiss / _homeProposalsBreakdown
//   _homeRenderBigProse / _homeRenderBigBucketRow (the "everything
//                                                  not in the inline
//                                                  cards" overflow)
//
// Rail / host-health:
//   _homeHostIsExpanded / _homeHostToggle / _homeHostRestoreState
//   _homeReportIsCollapsed / _homeReportToggle / _homeReportRestoreState
//   _mobileRailToggle + the Escape-key dismiss for the phone bottom sheet
//
// Auto-act flow:
//   _homeTierAllows, _homeAutoActIfTierAllows — runs when the rendered
//   ribbon includes auto-eligible items and the operator is on the
//   "auto" tier; respects in-flight guard.
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - renderProposalCard, openProposalDetail — self-improvement.js
//   - _alLoadCount / _alLoadLane — alerts.js (signal counts)
//   - _evoDrawer* family — evo-drawer (Phase 3n target)
//
// Out of scope (separate clusters):
//   _evoDrawer* family (~lines 8060 + 34427+) — chat surface available
//     from every page, target of Phase 3n (widgets/evo-drawer.js).
//   onPageActivate / onSubTabActivate — dispatch tables; stay inline
//     per the Phase 2a precedent (tightly coupled to all per-page
//     loaders, resolved at call time).
// ════════════════════════════════════════════════════════════════════════


// ══ HOME — evo conversation landing ═════════════════════════════════════════
// Carpentry pass. The narrative card and prompt input are stubs; the ribbon,
// bot chips, and mini host-health card are live data from existing endpoints.
// "Since you were here" counts come from a localStorage-backed last-seen
// timestamp that gets written when the user navigates away from Home.
const HOME_LAST_SEEN_KEY = 'evolve_home_last_seen';
const HOME_TIER_KEY      = 'evolve_home_tier';
let _homeLastSeenAtRender = null;   // snapshot captured at render-time so the
                                    // ribbon counts don't reset between
                                    // refresh-within-session.

function _homeReadLastSeen() {
  try {
    const v = localStorage.getItem(HOME_LAST_SEEN_KEY);
    if (v) return new Date(v);
  } catch (_) {}
  return null;
}
function _homeWriteLastSeen() {
  try { localStorage.setItem(HOME_LAST_SEEN_KEY, new Date().toISOString()); } catch (_) {}
}
// Tier vocabulary: 'ask' (default; always click), 'auto-small' (handle
// hygiene silently), 'auto' (handle anything with a clear answer). The
// older 'quiet'/'suggest' values map onto 'ask' — they were two ways
// of saying "don't auto-fire" and merging them tracks the simplified
// authority-only axis we settled on.
const _HOME_VALID_TIERS = new Set(['ask', 'auto-small', 'auto']);
const _HOME_TIER_MIGRATIONS = {
  // Legacy values from the earlier 4-tier dropdown — migrate to closest
  // authority-only equivalent.
  quiet: 'ask',
  suggest: 'ask',
};
function _homeNormalizeTier(v) {
  if (!v) return 'ask';
  if (_HOME_VALID_TIERS.has(v)) return v;
  return _HOME_TIER_MIGRATIONS[v] || 'ask';
}
function _homeSaveTier(v) {
  try { localStorage.setItem(HOME_TIER_KEY, _homeNormalizeTier(v)); } catch (_) {}
  // Reflect the new state on the button group immediately so the highlight
  // tracks the click without waiting for the next loadHome cycle.
  _homeRestoreTier();
  // Propagate to the drawer's authority badge — the drawer shares this
  // global tier, so the badge text must update the moment the operator
  // picks a different button on the Chat page. Function-typeof guard
  // because _homeSaveTier is reachable before the drawer JS block lower
  // in the file evaluates (early page load).
  if (typeof _updateDrawerAuthorityBadge === 'function') {
    _updateDrawerAuthorityBadge();
  }
}
function _homeReadTier() {
  try { return _homeNormalizeTier(localStorage.getItem(HOME_TIER_KEY)); }
  catch (_) { return 'ask'; }
}
function _homeRestoreTier() {
  // Highlight the active button in the three-button authority group.
  // Replaces the old dropdown selection path; safe to call before the
  // group renders (querySelectorAll just returns empty).
  try {
    const v = _homeReadTier();
    // Only the AUTHORITY buttons — model-tier buttons are queried by
    // data-model-tier and handled separately in _homeRestoreModelTier.
    const btns = document.querySelectorAll('.home-tier-btn[data-tier]');
    btns.forEach(b => {
      b.classList.toggle('active', b.dataset.tier === v);
    });
    // Phase 5 — keep the phone-fallback <select> in sync. The select is
    // rendered into the same .home-tier-bar so a width-switch from desktop
    // to phone doesn't lose the state. style-guide §10.1.
    const sel = document.getElementById('home-tier-mobile');
    if (sel && sel.value !== v) sel.value = v;
  } catch (_) {}
}

// Map an OC model name to its tier label for the meta footer
// underneath each reply bubble — e.g. "claude-sonnet-4-6" → "tier 2".
// Lets the operator see WHICH tier ran without having to keep the
// chip in their peripheral vision. The chip itself is "what I asked
// for"; this footer is "what actually ran" (they can differ when the
// cap downgraded Power, or when the classifier picked something
// different on Auto). Returns empty string for unknown models so
// the renderer falls back to "(<model> · <tokens> tok)".
function _modelTierLabel(model) {
  if (!model) return '';
  const m = String(model).toLowerCase();
  if (m.includes('fable'))  return 'max';
  if (m.includes('opus'))   return 'power';
  if (m.includes('sonnet')) return 'standard';
  if (m.includes('haiku'))  return 'fast';
  // judge / cross-provider (gpt-4o or gemini in default config).
  if (m.includes('gpt-4o') || m.startsWith('openai/') || m.includes('gemini')) {
    return 'judge';
  }
  return '';
}

// Display label for a tier id ('max' → 'Max'). Falls back to a sensible
// default for unknown/empty values so the capped line never renders blank.
function _homeTierDisplay(tier) {
  const map = { fast: 'Fast', standard: 'Standard', power: 'Power', max: 'Max' };
  return map[tier] || 'Standard';
}

// Build the "capped today" line from the requested tier + the tier that
// actually ran. Used when resp.tier_capped is set. Covers the max path
// (Max → Power / Max → Standard) the old hardcoded "Power → Standard"
// string got wrong.
function _homeCappedMessage(requested, effective) {
  // requested defaults to the per-conversation override when the server
  // didn't echo it (older payloads); effective defaults to standard, the
  // floor of the degrade chain.
  const reqLabel = _homeTierDisplay(requested || (typeof _homeReadModelTier === 'function' ? _homeReadModelTier() : 'power'));
  const effLabel = _homeTierDisplay(effective || 'standard');
  return `${reqLabel} capped today — used ${effLabel} for this turn.`;
}

// ── Model tier preference (Auto / Fast / Standard / Power) ─────────────────
//
// Per-conversation override of which model evo runs the turn with. Distinct
// from authority (which controls tool-execution gating) and from the
// classifier-driven default routing. Spec:
// docs/spec-user-tier-control-2026-05-26.md.
//
// Storage: sessionStorage, not localStorage — the spec calls this "per-
// conversation" stickiness. A browser reload preserves the choice; a new
// tab / new browser session resets to Auto. localStorage would have made
// the choice global, which doesn't match the mental model of "Power for
// THIS hard question, then back to Auto."
const HOME_MODEL_TIER_KEY = 'evolve_home_model_tier';
const _HOME_VALID_MODEL_TIERS = new Set(['auto', 'fast', 'standard', 'power', 'max']);
function _homeNormalizeModelTier(v) {
  if (!v) return 'auto';
  v = String(v).toLowerCase();
  return _HOME_VALID_MODEL_TIERS.has(v) ? v : 'auto';
}
function _homeReadModelTier() {
  // sessionStorage can be absent (private mode in some browsers) — fall
  // through to localStorage as a defensive secondary, then to 'auto'.
  try {
    const v = sessionStorage.getItem(HOME_MODEL_TIER_KEY);
    if (v) return _homeNormalizeModelTier(v);
  } catch (_) {}
  return 'auto';
}
function _homeSaveModelTier(v) {
  const norm = _homeNormalizeModelTier(v);
  try { sessionStorage.setItem(HOME_MODEL_TIER_KEY, norm); } catch (_) {}
  _homeRestoreModelTier();
}
function _homeRestoreModelTier() {
  try {
    const v = _homeReadModelTier();
    const btns = document.querySelectorAll('.home-model-btn[data-model-tier]');
    btns.forEach(b => {
      b.classList.toggle('active', b.dataset.modelTier === v);
    });
    // Phase 5 — phone-fallback select sync (see _homeRestoreTier).
    const sel = document.getElementById('home-model-mobile');
    if (sel && sel.value !== v) sel.value = v;
  } catch (_) {}
}

// Fetch the per-bot model-tier config and:
//  - hide the whole .home-model-bar if userTierOverride.enabled === false
//  - hide just the Power button if dailyCap === 0
//  - reflect the daily cap in the Power tooltip ("N/M used today")
//  - reflect the max daily cap in the Max tooltip ("N/M today")
// Spec: docs/spec-user-tier-control-2026-05-26.md §"Per-bot opt-out"
// and spec-model-rungs-and-roles §max semantics.
// Best-effort: any failure leaves the chip at its default (visible, all
// five buttons). The server enforces the caps regardless.
async function _homeFetchTierConfig() {
  try {
    const cfg = await api('GET', '/api/home/chat/tier-config');
    if (!cfg || typeof cfg !== 'object') return;
    const bar = document.querySelector('.home-model-bar');
    if (!bar) return;
    if (cfg.enabled === false) {
      bar.style.display = 'none';
      // If the chip was hidden after the operator had picked something
      // non-Auto, fall back to Auto for sends so the disabled-but-cached
      // sessionStorage value doesn't keep routing them off-default.
      const cur = _homeReadModelTier();
      if (cur !== 'auto') _homeSaveModelTier('auto');
      return;
    }
    bar.style.display = '';

    // ── Power button ───────────────────────────────────────────────
    const powerBtn = bar.querySelector('.home-model-btn[data-model-tier="power"]');
    // Phase 5 — Power option on the phone-fallback <select>. Mirror the
    // button-hide logic so the mobile picker stays consistent with the
    // desktop button group.
    const powerOpt = bar.querySelector('#home-model-mobile option[value="power"]');
    if (powerBtn) {
      if (cfg.dailyCap === 0) {
        powerBtn.style.display = 'none';
        if (powerOpt) powerOpt.disabled = true;
        // Same fallback if Power was the active choice.
        if (_homeReadModelTier() === 'power') _homeSaveModelTier('auto');
      } else {
        powerBtn.style.display = '';
        if (powerOpt) powerOpt.disabled = false;
        const used = Number.isFinite(cfg.used) ? cfg.used : 0;
        const cap = Number.isFinite(cfg.dailyCap) ? cfg.dailyCap : 10;
        powerBtn.title =
          `Power — Opus-class. High-complexity work, capped per day (${used}/${cap} today). Use Max for the frontier model.`;
      }
    }

    // ── Max button ────────────────────────────────────────────────
    // The tier-config endpoint returns maxDailyCap + maxUsed when the
    // pod has roleCaps.max configured (default cap=5). The button stays
    // visible as long as cap > 0; when cap=0 the operator has disabled
    // Max on this bot entirely (analogous to Power's dailyCap=0 path).
    const maxBtn = bar.querySelector('.home-model-btn[data-model-tier="max"]');
    const maxOpt = bar.querySelector('#home-model-mobile option[value="max"]');
    if (maxBtn) {
      const maxCap = Number.isFinite(cfg.maxDailyCap) ? cfg.maxDailyCap : 5;
      const maxUsed = Number.isFinite(cfg.maxUsed) ? cfg.maxUsed : 0;
      if (maxCap === 0) {
        maxBtn.style.display = 'none';
        if (maxOpt) maxOpt.disabled = true;
        if (_homeReadModelTier() === 'max') _homeSaveModelTier('auto');
      } else {
        maxBtn.style.display = '';
        if (maxOpt) maxOpt.disabled = false;
        maxBtn.title =
          `Max — Fable-class. Frontier model, ~2× Power cost (${maxUsed}/${maxCap} today). Only on explicit request.`;
      }
    }
  } catch (_) { /* fail open */ }
}

async function loadHome() {
  // Snapshot the prior last-seen BEFORE this render so re-renders within a
  // visit don't reset the "since you were here" counts. The new timestamp
  // gets written when the user navigates away (see nav()).
  _homeLastSeenAtRender = _homeReadLastSeen();
  _homeRestoreTier();
  _homeRestoreModelTier();
  // Pod-setup banner (Phase C 2026-06-02). Fire-and-forget — chip is
  // decorative and renders empty until the counter resolves. Hidden by
  // default in the HTML so there's no "flash of empty chip" before the
  // fetch lands.
  loadPodSetupChip();
  // Async; don't await — chip starts visible w/ defaults, gets
  // updated when the config arrives. No "flash of hidden chip" risk.
  _homeFetchTierConfig();
  renderHomeBotTiles();
  _renderHomeNarrativeLoading();
  // Single batch of read-only fetches. The narrative renderer consumes all
  // of these; the mini-health card reuses the host-health payload so the
  // browser doesn't fetch it twice. Each promise catches its own errors so
  // one slow/down endpoint doesn't blank the whole page.
  //
  //  - signals (firing only)        → narrative + ribbon
  //  - arbiter proposals (pending + snoozed) → narrative + ribbon
  //  - oc version (cheap; cached on server) → narrative OC-upgrade line
  //  - candidates/watchlist          → narrative "RSI is watching X" line
  //  - pod-health (CACHED variant)   → narrative pod-health summary
  //  - host-health                   → narrative disk-borderline + mini card
  const [firingResp, propsResp, ocVerResp, watchResp, podHealthResp, hostHealthResp] = await Promise.all([
    api('GET', '/api/signals?state=firing&limit=1000').catch(() => ({ signals: [] })),
    api('GET', '/api/arbiter/proposals?include=pending,snoozed&exclude_generator_id=operator_ui').catch(() => ({ proposals: [] })),
    api('GET', '/api/oc/version').catch(() => null),
    api('GET', '/api/candidates/watchlist?limit=200').catch(() => ({ candidates: [], total: 0 })),
    fetch('/api/pod-health/cached', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch('/api/host-health', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null),
  ]);
  const firing = (firingResp && firingResp.signals) || [];
  const proposals = (propsResp && propsResp.proposals) || [];
  const ocVersion = ocVerResp || null;
  const watchlist = watchResp || { candidates: [], total: 0 };
  const podHealth = podHealthResp || null;
  const hostHealth = hostHealthResp || null;
  // Narrative banner + mini host-health render off the shared feed.
  // (The old top-of-page ribbon was retired in favor of the report banner
  // inside the Evo's-report card — counts now live in the narrative prose.)
  await Promise.all([
    renderHomeNarrative({ firing, proposals, ocVersion, watchlist, podHealth, hostHealth }),
    _renderHomeMiniHealthFromData(hostHealth),
  ]);
  // Tier-c auto-act loop fires AFTER render so the operator sees the
  // current state for a moment before any silent actions run. Skipped
  // when tier is 'ask' (default). Re-entry guard prevents double-fire
  // on the 60s background refresh from `load()`.
  _homeAutoActIfTierAllows({ firing, proposals });
  // Restore the persisted chat thread once per first load. The 60s
  // background refresh re-runs loadHome but should NOT re-paint the
  // chat history (it's still in the DOM). The guard below makes
  // restore idempotent.
  if (!loadHome._chatRestored) {
    loadHome._chatRestored = true;
    _homeChatRestore();
  }
}

// ══ HOME CHAT ════════════════════════════════════════════════════════════════
// Wires the prompt input into /api/evo/dispatch. The dispatcher already
// resolves role + parses subcommands + runs handlers — we just push user
// messages through it and render the rendered response (DispatchResult
// .direct_send_message or .direct_message) as an evo-side bubble.
//
// Bot target: "evolve" (the pod's primary bot). Without sender_external_id
// the dispatcher defaults to role="primary", same as the operator on the
// admin UI would have.
//
// Conversation persists in localStorage under HOME_CHAT_KEY. The latest
// rule-based narrative still renders as the *first* bubble on every page
// load — it's regenerated fresh each visit. Subsequent user/evo messages
// append below and survive reloads.

// Legacy single-thread key — preserved for one-way migration. Once
// migrated into the sessions store on first load post-upgrade, this
// key is left in place (rather than deleted) as a safety net for
// rollbacks. _homeMigrateOrInit reads it lazily and never writes it.
const HOME_CHAT_KEY = 'evolve_home_chat';
const HOME_CHAT_BOT = 'evolve';                  // primary bot for dispatch routing

// ── Multi-session model ─────────────────────────────────────────────────────
// Each chat on the Chat page is its own session: independent thread,
// independent title, switchable from the session strip above the
// thread. Pattern lifted from Claude Code's session list but pared
// down — the Chat page is general-purpose (no per-page context-pack
// anchor), so sessions are the natural unit.
//
// Storage shape:
//   localStorage["evolve_home_sessions"]        = {sid: {id, title,
//                                                  created_at, updated_at,
//                                                  turns[], trimmed[]}}
//   localStorage["evolve_home_active_session"]  = sid (string)
//
// Migration: on first load post-upgrade, any prior single-thread blob
// at HOME_CHAT_KEY is moved into a session titled "Resumed
// conversation" so operators don't lose their history.

const HOME_SESSIONS_KEY = 'evolve_home_sessions';
const HOME_ACTIVE_SESSION_KEY = 'evolve_home_active_session';
const HOME_SESSION_MAX_TURNS = 50;               // per-session cap — drawer-parity
const HOME_SESSIONS_MAX = 20;                    // soft cap; oldest evicts on overflow
const HOME_SESSION_TITLE_MAX = 40;               // chip-truncated above this
// If a chat send doesn't get a response within this window, abort the
// fetch and flip the pending placeholder into a 'took too long —
// retry?' error so the operator isn't stuck staring at a spinner
// forever. 5 minutes covers long multi-tool turns; the proxy's
// subprocess cap (270 s, see evo/proxy.py::_SUBPROCESS_TIMEOUT_S)
// fires ~30 s earlier so the server's own timeout text reaches the
// chat before the client aborts. Operators reported the prior 60 s
// cap firing on real workloads.
const HOME_CHAT_PENDING_TIMEOUT_MS = 300_000;
// After this many ms with no response, swap the pending bubble's
// '…thinking…' text for a live elapsed-time indicator ("still
// thinking… (Xs)") that updates every second. Tells the operator the
// turn is alive while waiting through model + multi-tool latency.
const HOME_CHAT_SLOW_INDICATOR_MS = 10_000;

// Shared between the home composer and the per-page evo drawer. Drives
// the "still thinking… (Xs)" body update on the pending bubble while a
// long turn is in flight, so the operator can tell the request is
// alive vs. wedged. Returns a stop() function that clears the
// interval — call it on response/error/abort.
//
// The bubble's body text is replaced wholesale, including any prior
// indicator text. Lookup is by `pendingId` (the bubble's DOM id); a
// missing element is treated as "operator switched threads" and we
// skip the update silently — the next tick will hit the same path.
function _evoChatPendingIndicator(pendingId, slowAfterMs) {
  const startedAt = Date.now();
  const update = () => {
    const elapsedMs = Date.now() - startedAt;
    if (elapsedMs < slowAfterMs) return;
    const elapsedS = Math.round(elapsedMs / 1000);
    const bubble = document.getElementById(pendingId);
    if (!bubble) return;
    const bodyEl = bubble.querySelector('.home-msg-body');
    if (!bodyEl) return;
    // Plain text — no markdown — so the indicator is always visible
    // even if the formatter would mangle the dots.
    bodyEl.textContent = `…still thinking (${elapsedS}s)…`;
  };
  const id = setInterval(update, 1000);
  return () => clearInterval(id);
}

// Wrapper around the evo delimiter format. The dispatcher wraps the
// user-facing body in `═══ evo ═══ ... ═══ end evo ═══` so it looks
// distinct in Telegram; in the chat-bubble UI the bubble itself is
// the delimiter, so we strip them on render.
const _HOME_EVO_OPEN_RE  = /^═══[^═]*═══\s*/;
const _HOME_EVO_CLOSE_RE = /\s*═══\s*end evo\s*═══\s*$/;

function _homeUnwrapEvoText(s) {
  if (!s) return '';
  return String(s).replace(_HOME_EVO_OPEN_RE, '').replace(_HOME_EVO_CLOSE_RE, '').trim();
}

// ── Session storage primitives ───────────────────────────────────────────────

function _homeSessionsLoad() {
  // Returns the whole dict; empty {} when none stored.
  try {
    const raw = localStorage.getItem(HOME_SESSIONS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
      ? parsed : {};
  } catch (_) { return {}; }
}

function _homeSessionsSave(sessions) {
  // Persist the session dict. On QuotaExceededError (5-10MB limit per
  // origin — usually only hit when a session is laden with pasted
  // tool output), evict oldest sessions one at a time and retry. If
  // we end up with only the active session and still can't fit, give
  // up loudly so the operator can clear browser data or shrink the
  // active turn.
  const tryWrite = (payload) => {
    localStorage.setItem(HOME_SESSIONS_KEY, JSON.stringify(payload));
  };
  try {
    tryWrite(sessions);
    return;
  } catch (e) {
    // Continue to the eviction loop below if this looks like a quota
    // error. Other errors (denied access, broken storage) are
    // unrecoverable — drop the write rather than thrash.
    const isQuota = e && (
      e.name === 'QuotaExceededError'
      || e.name === 'NS_ERROR_DOM_QUOTA_REACHED'   // firefox
      || e.code === 22
      || e.code === 1014
    );
    if (!isQuota) return;
  }
  // Eviction loop. Sort sessions by updated_at ascending so the
  // oldest go first, but NEVER evict the active session — losing the
  // operator's current conversation would be worse than the write
  // failing.
  let active = '';
  try { active = localStorage.getItem(HOME_ACTIVE_SESSION_KEY) || ''; }
  catch (_) {}
  const work = Object.assign({}, sessions);
  const candidates = Object.keys(work)
    .filter(sid => sid !== active)
    .sort((a, b) =>
      (work[a].updated_at || '').localeCompare(work[b].updated_at || '')
    );
  for (const sid of candidates) {
    delete work[sid];
    try {
      tryWrite(work);
      // Surface a one-time toast so the operator knows what happened.
      // Don't spam: only when the live save retried at least once.
      _homeChatNotifyQuotaEvicted(candidates.indexOf(sid) + 1);
      return;
    } catch (_) {
      // Keep evicting.
    }
  }
  // Couldn't fit even with everything but the active session evicted.
  // This means the active session ITSELF is larger than the quota —
  // very rare (would require ~5MB in one session, ~100x typical).
  // We drop the write silently and rely on the user noticing the
  // page didn't update. A future PR could trim the active session's
  // oldest turns instead; out of scope here.
}

// Operator-facing toast for the rare quota-eviction case. Renders
// once per page-load (debounced via a window-level guard) so a
// runaway loop of saves can't spam.
function _homeChatNotifyQuotaEvicted(numEvicted) {
  if (window._homeQuotaNoticeShown) return;
  window._homeQuotaNoticeShown = true;
  try {
    // Best-effort: log + show a non-modal banner if there's a known
    // toast surface. We don't depend on a specific toast lib being
    // present.
    console.warn(
      `[evo] localStorage quota hit; evicted ${numEvicted} oldest ` +
      `session(s) to make room. Your current conversation is safe.`,
    );
  } catch (_) {}
}

function _homeGenSessionId() {
  // Cheap unique id — no UUID dep; collision-free at our cadence.
  return 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// Compute the OC-side session id for a browser session. Each browser
// session needs its OWN OC session so multi-session memory doesn't
// collide on the server. Format: ``admin-ui-home-<browser-sid>`` so
// per-page derivation still works, but with per-conversation isolation
// on top.
//
// The legacy server-side derivation (``derive_session_id("home")`` →
// ``admin-ui-home``) is still the fallback when this PR's clients
// send no ``session_id`` in the body — see home_chat_routes.py.
function _homeBuildOcSessionId(browserSid) {
  return 'admin-ui-home-' + String(browserSid || 'unknown');
}

function _homeNewSessionRecord(opts) {
  const o = opts || {};
  const now = new Date().toISOString();
  const id = _homeGenSessionId();
  return {
    id,
    // Per-browser-session OC id — overrides the legacy
    // page_id-derived ``admin-ui-home`` (which would collide across
    // every browser-session on this page). Callers may override
    // (migration uses the bare ``admin-ui-home`` so prior OC memory
    // is preserved into the "Resumed conversation" session).
    oc_session_id: o.oc_session_id || _homeBuildOcSessionId(id),
    title: o.title || 'New conversation',
    created_at: now,
    updated_at: now,
    turns: Array.isArray(o.turns) ? o.turns : [],
    trimmed: Array.isArray(o.trimmed) ? o.trimmed : [],
  };
}

function _homeActiveSessionId() {
  try {
    const sid = localStorage.getItem(HOME_ACTIVE_SESSION_KEY);
    if (sid) return sid;
  } catch (_) {}
  return _homeMigrateOrInit();
}

function _homeSetActiveSessionId(sid) {
  try { localStorage.setItem(HOME_ACTIVE_SESSION_KEY, sid); } catch (_) {}
}

// Migrate any prior single-thread storage into a session, or bootstrap
// a fresh empty session. Returns the resulting active sid.
function _homeMigrateOrInit() {
  const sessions = _homeSessionsLoad();
  const ids = Object.keys(sessions);
  if (ids.length > 0) {
    // No active id but sessions exist — pick the most recent.
    const sid = ids.sort((a, b) =>
      (sessions[b].updated_at || '').localeCompare(sessions[a].updated_at || '')
    )[0];
    _homeSetActiveSessionId(sid);
    return sid;
  }
  // Try migration from the legacy single-thread blob.
  let oldTurns = [];
  try {
    const raw = localStorage.getItem(HOME_CHAT_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) oldTurns = parsed;
    }
  } catch (_) {}
  const seedTitle = oldTurns.length > 0
    ? 'Resumed conversation'
    : 'New conversation';
  // When migrating prior turns, preserve continuity with the legacy
  // OC session ('admin-ui-home') so evo still remembers what it just
  // said. Fresh-bootstrap sessions get a unique OC id (see
  // _homeBuildOcSessionId default in _homeNewSessionRecord).
  const fresh = _homeNewSessionRecord({
    title: seedTitle,
    turns: oldTurns,
    oc_session_id: oldTurns.length > 0 ? 'admin-ui-home' : undefined,
  });
  sessions[fresh.id] = fresh;
  _homeSessionsSave(sessions);
  _homeSetActiveSessionId(fresh.id);
  return fresh.id;
}

function _homeGetActiveSession() {
  const sid = _homeActiveSessionId();
  const sessions = _homeSessionsLoad();
  if (!sessions[sid]) {
    // Active id points at a deleted session — recover by creating one.
    const fresh = _homeNewSessionRecord();
    sessions[fresh.id] = fresh;
    _homeSessionsSave(sessions);
    _homeSetActiveSessionId(fresh.id);
    return fresh;
  }
  // Back-fill oc_session_id for sessions created under PR #1339
  // (which didn't have the field). Pin them to unique per-browser-sid
  // OC sessions so they stop colliding on 'admin-ui-home'.
  if (!sessions[sid].oc_session_id) {
    sessions[sid].oc_session_id = _homeBuildOcSessionId(sid);
    _homeSessionsSave(sessions);
  }
  return sessions[sid];
}

// Atomic-ish read-modify-write of the active session. Caller mutates
// the session object in the callback; we persist + bump updated_at.
function _homeUpdateActiveSession(updateFn) {
  const sid = _homeActiveSessionId();
  const sessions = _homeSessionsLoad();
  if (!sessions[sid]) return;
  updateFn(sessions[sid]);
  sessions[sid].updated_at = new Date().toISOString();
  _homeSessionsSave(sessions);
}

// ── Title generation ─────────────────────────────────────────────────────────
// Slug-from-first-user-message: cheap, deterministic, no API call. The
// title becomes the user's first prompt truncated to ~40 chars. Good
// enough to navigate by — users recognize their own questions. A
// followup PR will refine titles with a one-shot LLM call after a few
// turns; until then this is honest and instant.

function _homeSlugTitleFromMessage(text) {
  const clean = String(text || '').trim().replace(/\s+/g, ' ');
  if (!clean) return 'New conversation';
  if (clean.length <= HOME_SESSION_TITLE_MAX) return clean;
  // Cut at a word boundary near the cap so titles don't end mid-word.
  const slice = clean.slice(0, HOME_SESSION_TITLE_MAX);
  const lastSpace = slice.lastIndexOf(' ');
  return (lastSpace > 20 ? slice.slice(0, lastSpace) : slice) + '…';
}

function _homeMaybeAutoTitle(userText) {
  // Re-titles the session if it still has the default placeholder. Once
  // a session has been manually-titled (future feature) or auto-titled
  // by an earlier message, this is a no-op.
  _homeUpdateActiveSession(s => {
    if (!s.title || s.title === 'New conversation') {
      s.title = _homeSlugTitleFromMessage(userText);
    }
  });
}

// ── Public chat-load/save (session-aware) ────────────────────────────────────
// Same names + signatures as pre-sessions so _homeChatSend + the rest
// of the chat plumbing don't need to know about sessions.

function _homeChatLoad() {
  return _homeGetActiveSession().turns || [];
}

function _homeChatSave(messages) {
  _homeUpdateActiveSession(s => {
    if (messages.length > HOME_SESSION_MAX_TURNS) {
      // Cap overflow — spill head turns into a per-session trimmed
      // archive (matches the drawer pattern: nothing silently lost).
      const overflow = messages.slice(0, messages.length - HOME_SESSION_MAX_TURNS);
      s.trimmed = (s.trimmed || []).concat(overflow);
      s.turns = messages.slice(-HOME_SESSION_MAX_TURNS);
    } else {
      s.turns = messages;
    }
  });
}

function _homeChatAppendToSession(targetSid, msg) {
  // Append a message to a SPECIFIC session by browser-sid — NOT
  // always the currently-active one. ``_homeChatSend`` captures the
  // sending sid at the start of the turn and uses this helper so the
  // user message + evo reply land on the originating session even if
  // the operator clicked away to a different session while the
  // response was in flight.
  //
  // (Previously this codepath used ``_homeChatAppend`` which routed
  // via ``_homeUpdateActiveSession`` — so an in-flight send + thread
  // switch would route the evo response to the wrong session. That
  // was visible to operators as Thread B suddenly showing an evo
  // reply to a question they typed in Thread A.)
  const sessions = _homeSessionsLoad();
  const session = sessions[targetSid];
  if (!session) {
    // Session was deleted while the send was in flight — silently
    // drop. The operator no longer has a UI for this conversation,
    // and surfacing an error in a different session would be worse.
    return;
  }
  const turns = (Array.isArray(session.turns) ? session.turns : []).slice();
  turns.push(msg);
  if (turns.length > HOME_SESSION_MAX_TURNS) {
    // Cap overflow — spill head turns into the per-session trimmed
    // archive (matches the drawer pattern: nothing silently lost).
    const overflow = turns.slice(0, turns.length - HOME_SESSION_MAX_TURNS);
    session.trimmed = (session.trimmed || []).concat(overflow);
    session.turns = turns.slice(-HOME_SESSION_MAX_TURNS);
  } else {
    session.turns = turns;
  }
  session.updated_at = new Date().toISOString();
  _homeSessionsSave(sessions);

  // Auto-title on the user's first turn in a default-titled session.
  if (msg.role === 'user' && (!session.title || session.title === 'New conversation')) {
    const userTurns = session.turns.filter(m => m.role === 'user').length;
    if (userTurns === 1) {
      // Re-load to merge the title write (the auto-title is a
      // separate write from the turn append so a concurrent session
      // mutation can't drop one of them).
      const reread = _homeSessionsLoad();
      if (reread[targetSid]) {
        reread[targetSid].title = _homeSlugTitleFromMessage(msg.text);
        _homeSessionsSave(reread);
      }
    }
  }

  // Render to DOM only if the target session is currently displayed.
  // If the operator has switched away, the DOM has already been
  // cleared by ``_homeChatRestore`` and writing to it would land in
  // the wrong thread.
  if (_homeActiveSessionId() === targetSid) {
    _homeChatRenderBubble(msg);
  }
  _homeSessionStripRender();
  _homeSessionStripSyncCounter();
}

function _homeChatAppend(msg) {
  // Back-compat shim — route to the active session. Kept for any
  // future callers that don't need to pin the target session
  // explicitly (currently none; ``_homeChatSend`` uses
  // ``_homeChatAppendToSession`` directly).
  _homeChatAppendToSession(_homeActiveSessionId(), msg);
}

// Diagnostic LLM fallback — outage-only chat that bypasses evo's gateway.
// Activated when the proxy returns ``source === 'gateway_down'`` AND the
// operator clicks "Talk to diagnostic LLM" on the resulting bubble.
// While active, every evo send (home, drawer, feedback, help) routes POSTs
// to /api/diagnostic/chat instead of /api/home/chat. A banner above each
// surface's composer makes the switched mode obvious; "Switch back to evo"
// clears the flag from any surface and removes every banner instance.
let _diagnosticMode = false;

// Per-surface anchor targets — banner is inserted as a sibling immediately
// before the composer. Composers in non-active pages (display:none) hide
// their banner automatically; the drawer composer's banner only shows when
// the drawer is open. Keeping one entry per surface lets ``_exitDiagnosticMode``
// clean up every banner instance regardless of which surface fired the
// transition.
const _DIAGNOSTIC_BANNER_TARGETS = [
  { bannerId: 'home-chat-diagnostic-banner',
    findComposer: () => document.querySelector('.home-prompt-card')
      || document.getElementById('home-prompt-input')?.parentElement },
  { bannerId: 'evo-drawer-diagnostic-banner',
    findComposer: () => document.getElementById('evo-drawer-prompt') },
  { bannerId: 'feedback-chat-diagnostic-banner',
    findComposer: () => document.getElementById('feedback-prompt-input')?.parentElement },
  { bannerId: 'help-chat-diagnostic-banner',
    findComposer: () => document.getElementById('help-prompt-input')?.parentElement },
];

function _diagnosticBannerHtml(bannerId) {
  return (
    `<div id="${bannerId}" ` +
    'style="background:rgba(231,76,60,0.18);border:1px solid ' +
    'rgba(231,76,60,0.45);border-radius:8px;padding:8px 12px;' +
    'margin:0 12px 8px 12px;display:flex;align-items:center;' +
    'gap:10px;font-size:13px;color:#f0d4d0;">' +
    '<span>⚠ Talking to Evolve diagnostic LLM — evo is down. ' +
    'No bot tools available; this LLM is read-only.</span>' +
    '<button class="btn btn-ghost btn-sm" ' +
    'onclick="_exitDiagnosticMode()">Switch back to evo</button>' +
    '</div>'
  );
}

function _updateDiagnosticBanner() {
  for (const target of _DIAGNOSTIC_BANNER_TARGETS) {
    const existing = document.getElementById(target.bannerId);
    if (_diagnosticMode) {
      if (existing) continue;
      const composer = target.findComposer();
      if (composer && composer.parentElement) {
        composer.parentElement.insertBefore(
          new DOMParser()
            .parseFromString(_diagnosticBannerHtml(target.bannerId), 'text/html')
            .body.firstChild,
          composer,
        );
      }
    } else if (existing) {
      existing.remove();
    }
  }
}

// Surface key → (input id, send fn). Default 'home' preserves PR #2064 —
// the home-chat bubble button still works without an explicit surface arg.
const _DIAGNOSTIC_SURFACE_HANDLERS = {
  home: { inputId: 'home-prompt-input',
          send: () => (typeof _homeChatSend === 'function') && _homeChatSend() },
  drawer: { inputId: 'evo-drawer-input',
            send: () => (typeof _evoDrawerSend === 'function') && _evoDrawerSend() },
  feedback: { inputId: 'feedback-prompt-input',
              send: () => (typeof _pageChatSend === 'function') && _pageChatSend('feedback') },
  help: { inputId: 'help-prompt-input',
          send: () => (typeof _pageChatSend === 'function') && _pageChatSend('help') },
};

function _enterDiagnosticMode(originalText, surface) {
  _diagnosticMode = true;
  _updateDiagnosticBanner();
  if (!originalText) return;
  const handler = _DIAGNOSTIC_SURFACE_HANDLERS[surface || 'home']
    || _DIAGNOSTIC_SURFACE_HANDLERS.home;
  const input = document.getElementById(handler.inputId);
  if (input) input.value = String(originalText);
  handler.send();
}

function _exitDiagnosticMode() {
  _diagnosticMode = false;
  _updateDiagnosticBanner();
}

function _homeChatRenderBubble(msg) {
  const thread = document.getElementById('home-thread');
  if (!thread) return;
  // Drop the empty-state placeholder as soon as the first bubble lands.
  const empty = document.getElementById('home-thread-empty');
  if (empty) empty.remove();
  const isUser = msg.role === 'user';
  // .home-msg-evo / .home-msg-user are the new left-accent classes (no
  // more boxed bubbles) — pick the right one based on role.
  const cls = isUser ? 'home-msg home-msg-user' : 'home-msg home-msg-evo';
  // .home-msg-warn renders the yellow informational bubble for the
  // proxy_warn (empty-reply-with-synthesized-confirmation) case. Spec:
  // surface-aware help-style spec §8 Phase 1 + diagnosis-empty-reply-
  // after-successful-tool-calls-2026-05-21.md. ``error`` (red) wins
  // when both are set — they shouldn't both fire today, but defensive.
  const errCls = msg.error
    ? ' home-msg-error'
    : (msg.warn ? ' home-msg-warn' : '');
  const pendingCls = msg.pending ? ' home-msg-pending' : '';
  const name = isUser ? 'you' : 'evo';
  const avatar = isUser
    ? '<span class="home-evo-avatar" style="background:rgba(124,92,255,0.28)">●</span>'
    : '<span class="home-evo-avatar">✦</span>';
  const tsStr = msg.ts ? ago(msg.ts) : '';
  // Suggested actions — server-extracted backticked `evo X` mentions
  // from the LLM reply, validated against the known-subcommand
  // registry. Render as a row of one-click buttons under the body.
  // Only present on evo bubbles from the LLM path; never on user
  // turns or dispatch-path replies (where the user typed the command
  // themselves so a "Run X" button would be redundant).
  let suggestedHtml = '';
  if (!isUser && Array.isArray(msg.suggested_actions) && msg.suggested_actions.length) {
    suggestedHtml = `<div class="home-msg-suggested">${
      msg.suggested_actions.map(a => {
        const label = escHtml(a.label || `Run evo ${a.subcommand}`);
        const cmd = escHtml(a.subcommand || '');
        return `<button class="btn btn-ghost btn-sm" onclick="_homeChatRunSuggested('${cmd}')">${label}</button>`;
      }).join('')
    }</div>`;
  }
  // Retry affordance — appears on error/timeout bubbles that carry the
  // original message text under ``retry_text``. Lets the operator
  // recover from a network glitch / OC stall with one click instead
  // of re-typing what they just said.
  let retryHtml = '';
  if (!isUser && msg.error && typeof msg.retry_text === 'string' && msg.retry_text) {
    retryHtml = `<div class="home-msg-suggested">` +
      `<button class="btn btn-ghost btn-sm" onclick="_homeChatRetrySend(${attrJsLiteral(msg.retry_text)})">↻ Retry</button>` +
      `</div>`;
  }
  // Gateway-down affordance — when the proxy detected that evo's
  // gateway isn't responding, offer a one-click switch to the
  // diagnostic LLM so the operator can still get help during the
  // outage. The original message text rides along under retry_text
  // so the diagnostic LLM picks up the same question (no re-typing).
  let diagnosticHtml = '';
  if (!isUser && msg.gateway_down) {
    const txt = typeof msg.retry_text === 'string' ? msg.retry_text : '';
    diagnosticHtml = `<div class="home-msg-suggested">` +
      `<button class="btn btn-ghost btn-sm" onclick="_enterDiagnosticMode(${attrJsLiteral(txt)})">` +
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
    <div class="home-msg-body">${_homeChatFormatBody(msg.text)}</div>
    ${suggestedHtml}
    ${retryHtml}
    ${diagnosticHtml}
  `;
  thread.appendChild(bubble);
  // Scroll the thread to the latest message.
  thread.scrollTop = thread.scrollHeight;
}

// Retry-send handler — called from the ↻ button on an error/timeout
// bubble. Drops the original message back into the input + fires
// send, identical to the operator retyping the message.
function _homeChatRetrySend(text) {
  const input = document.getElementById('home-prompt-input');
  if (!input) return;
  input.value = String(text || '');
  _homeChatSend();
}

async function _homeChatCopyCode(btn) {
  // Click handler for the per-code-block "copy" button. Reads the
  // sibling <pre>'s textContent (browser has already HTML-decoded the
  // escape sequences) and writes to the clipboard. Brief visual confirm
  // by swapping label + colour so the operator knows the copy landed.
  const pre = btn.previousElementSibling;
  if (!pre || pre.tagName !== 'PRE') return;
  try {
    await navigator.clipboard.writeText(pre.textContent);
    btn.textContent = 'copied';
    btn.classList.add('done');
  } catch (_) {
    btn.textContent = 'copy failed';
  }
  setTimeout(() => {
    btn.textContent = 'copy';
    btn.classList.remove('done');
  }, 1200);
}

function _homeChatRunSuggested(subcommand) {
  // Click handler for a suggested-action button. Stuffs the subcommand
  // into the prompt input and invokes the normal send path so it goes
  // through history persistence + cap accounting just like a typed turn.
  const input = document.getElementById('home-prompt-input');
  if (!input) return;
  input.value = `evo ${subcommand}`;
  _homeChatSend();
}

function _homeChatFormatBody(text) {
  // Lightweight markdown: headings (##), **bold**, *italics*, _italics_,
  // `code`, ```fenced```, `- item` / `1. item` lists, `---` rules, pipe
  // tables, and newlines → <br>. Order matters: inline + fenced code
  // are stashed under placeholder tokens FIRST so block-level passes
  // (tables, headings, lists) see clean line structure. A row like
  //   | `ping-calendar-monitor` | uncapped | $1.00 |
  // would otherwise be shredded across <code>-tag splits and never
  // match the |…| table pattern.
  const escaped = escHtml(String(text || ''));
  const stash = [];
  // Placeholder uses private-use unicode (U+E000/U+E001) so it survives
  // .trim() inside table-cell parsing and contains nothing that the
  // block-level patterns below would treat as syntax.
  const stashOne = (html) => {
    const tok = "\uE000" + stash.length + "\uE001";
    stash.push(html);
    return tok;
  };
  let out = escaped.replace(/```[a-z]*\n([\s\S]*?)```/g, function (_m, body) {
    return stashOne('<div class="home-msg-code"><pre>' + body + '</pre><button class="home-msg-copy" onclick="_homeChatCopyCode(this)" title="Copy">copy</button></div>');
  });
  out = out.replace(/`([^`\n]+)`/g, function (_m, body) {
    return stashOne('<code style="background:var(--bg3);padding:1px 4px;border-radius:3px;font-size:0.85em">' + body + '</code>');
  });
  // Tables: consecutive | lines. The LLM sometimes splits the header
  // (+ |---|---| separator) from the body with a blank line, and
  // sometimes omits the separator row entirely. Pre-merge stray blank
  // lines that sit between two well-formed `| … |` rows so the run
  // matches as one block, then tolerate a missing separator by
  // rendering a header-less data table instead of dropping back to
  // literal-pipe text.
  out = out.replace(/(\|[^\n]*\|[ \t]*)\n[ \t]*\n(?=\|[^\n]*\|)/g, '$1\n');
  out = out.replace(/(^|\n)((?:\|[^\n]+(?:\n|$))+)/g, function (_m, lead, block) {
    const rawRows = block.trimEnd().split('\n');
    if (rawRows.length < 2) return _m;
    const isSep = function (r) { return /^\s*\|[\s|:-]+\|\s*$/.test(r); };
    const hasSeparator = isSep(rawRows[1]);
    const dataRows = rawRows.filter(function (r) { return r.trim() && !isSep(r); });
    if (dataRows.length === 0) return _m;
    const parse = function (row) { return row.replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); }); };
    let thHtml = '';
    let bodyStart = 0;
    if (hasSeparator) {
      const cells = parse(dataRows[0]).map(function (h) {
        return '<th style="padding:3px 8px;border:1px solid var(--border);background:var(--bg2);text-align:left">' + h + '</th>';
      }).join('');
      thHtml = '<thead><tr>' + cells + '</tr></thead>';
      bodyStart = 1;
    }
    const tdHtml = dataRows.slice(bodyStart).map(function (r) {
      const cells = parse(r).map(function (c) {
        return '<td style="padding:3px 8px;border:1px solid var(--border);vertical-align:top">' + c + '</td>';
      }).join('');
      return '<tr>' + cells + '</tr>';
    }).join('');
    return lead + '<table style="border-collapse:collapse;margin:4px 0;font-size:0.9em;width:100%">' + thHtml + '<tbody>' + tdHtml + '</tbody></table>';
  });
  // Headings: ## Heading → bold section label (h1–h4 supported)
  out = out.replace(/(^|\n)(#{1,4}) ([^\n]+)/g, function (_m, lead, hashes, content) {
    const styles = [
      'font-size:1.15em;font-weight:700;margin:10px 0 4px',
      'font-size:1.05em;font-weight:700;margin:8px 0 3px',
      'font-size:0.95em;font-weight:700;margin:6px 0 2px',
      'font-size:0.9em;font-weight:600;margin:4px 0 2px',
    ];
    return lead + '<div style="' + styles[hashes.length - 1] + '">' + content + '</div>';
  });
  // Horizontal rules: --- on its own line
  out = out.replace(/(^|\n)---+(?=\n|$)/g, '$1<hr style="border:none;border-top:1px solid var(--border);margin:8px 0">');
  out = out.replace(/(^|\n)((?:[-*] [^\n]+(?:\n|$))+)/g, function (_m, lead, block) {
    const items = block.trim().split(/\n/).map(function (line) {
      return '<li>' + line.replace(/^[-*] /, '') + '</li>';
    }).join('');
    return lead + '<ul style="margin:4px 0;padding-left:20px">' + items + '</ul>';
  });
  out = out.replace(/(^|\n)((?:\d+\. [^\n]+(?:\n|$))+)/g, function (_m, lead, block) {
    const items = block.trim().split(/\n/).map(function (line) {
      return '<li>' + line.replace(/^\d+\. /, '') + '</li>';
    }).join('');
    return lead + '<ol style="margin:4px 0;padding-left:22px">' + items + '</ol>';
  });
  // Bold: allow a single embedded newline (wrapped long lines) but not
  // a paragraph break (\n\n), which would mean an unclosed **.
  out = out.replace(/\*\*((?:[^*\n]|\n(?!\n))+)\*\*/g, '<strong>$1</strong>');
  // Italics: require non-whitespace on both sides of * so bullet
  // markers (`* item`) and stray asterisks don’t match.
  out = out.replace(/\*(\S(?:[^*\n]*?\S)?)\*/g, '<em>$1</em>');
  // Model/token footer: _((...))_ → small dim annotation. Must run
  // before the general underscore-italic pass so it doesn’t fall
  // through to <em>.
  out = out.replace(/(^|[^\w])_(\([^_\n]+\))_(?=[^\w]|$)/g,
    '$1<span style="font-size:0.78em;color:var(--text3)">$2</span>');
  // Underscore-italics: skip identifiers like pod_admin, plugin_monitor.
  out = out.replace(/(^|[^\w])_([^_\n]+)_(?=[^\w]|$)/g, '$1<em>$2</em>');
  out = out.replace(/\n/g, '<br>');
  // Restore stashed code spans last so they land unscathed inside table
  // cells, headings, list items, and prose alike. <br> insertion above
  // doesn’t touch placeholders since they contain no newline.
  out = out.replace(/\uE000(\d+)\uE001/g, function (_m, i) { return stash[Number(i)]; });
  return out;
}

function _homeChatRestore() {
  // Re-paint the active session's thread. Called once on first home
  // load (via loadHome._chatRestored guard), and again whenever the
  // operator switches/creates/deletes sessions. Also renders the
  // session strip + counter to match the new active session.
  _homeSessionStripRender();
  const thread = document.getElementById('home-thread');
  if (!thread) return;
  thread.querySelectorAll('.home-msg').forEach(b => b.remove());
  const existingEmpty = document.getElementById('home-thread-empty');
  const history = _homeChatLoad();
  if (history.length === 0) {
    if (!existingEmpty) {
      const empty = document.createElement('div');
      empty.className = 'home-thread-empty';
      empty.id = 'home-thread-empty';
      empty.innerHTML = 'Ask evo anything below — or try a quick command like <code>cost</code>, <code>alerts</code>, or <code>summary</code>.';
      thread.appendChild(empty);
    }
  } else {
    if (existingEmpty) existingEmpty.remove();
    for (const msg of history) _homeChatRenderBubble(msg);
  }
  _homeSessionStripSyncCounter();
}

// ── Session strip render + actions ──────────────────────────────────────────
// Renders the chip row above the thread. Sessions sorted by recency
// (most-recently-updated leftmost) so the operator's current train of
// thought is reachable at the first chip past "+ New".

function _homeSessionStripRender() {
  const bar = document.getElementById('home-session-bar');
  if (!bar) return;
  const sessions = _homeSessionsLoad();
  // Defensive: ensure at least one session exists. _homeActiveSessionId
  // bootstraps a fresh one on its own when storage is empty.
  const activeSid = _homeActiveSessionId();
  const sorted = Object.values(sessions).sort((a, b) =>
    (b.updated_at || '').localeCompare(a.updated_at || '')
  );

  const newBtn =
    `<button class="home-session-new" onclick="_homeSessionNew()" ` +
    `title="Start a new conversation (this one stays in the list)">+ New</button>`;

  const chips = sorted.map(s => {
    const isActive = s.id === activeSid;
    const title = escHtml(s.title || 'New conversation');
    const sidEsc = escHtml(s.id);
    const cls = 'home-session-chip' + (isActive ? ' active' : '');
    return (
      `<div class="${cls}" data-sid="${sidEsc}" ` +
      `onclick="_homeSessionSwitch('${sidEsc}')" title="${title}">` +
        `<span class="home-session-chip-title">${title}</span>` +
        `<button class="home-session-chip-close" ` +
                `onclick="event.stopPropagation(); _homeSessionDelete('${sidEsc}')" ` +
                `title="Delete this conversation">×</button>` +
      `</div>`
    );
  }).join('');

  bar.innerHTML =
    newBtn +
    `<div class="home-session-chips">${chips}</div>` +
    `<span class="home-session-counter" id="home-session-counter"></span>`;
  _homeSessionStripSyncCounter();
}

function _homeSessionStripSyncCounter() {
  const counter = document.getElementById('home-session-counter');
  if (!counter) return;
  const turns = _homeChatLoad().length;
  counter.classList.remove('near-cap', 'at-cap');
  if (turns === 0) {
    counter.style.display = 'none';
    counter.textContent = '';
    return;
  }
  counter.style.display = 'inline-block';
  counter.textContent = `${turns}/${HOME_SESSION_MAX_TURNS}`;
  if (turns >= HOME_SESSION_MAX_TURNS) {
    counter.classList.add('at-cap');
    counter.title =
      `At the ${HOME_SESSION_MAX_TURNS}-turn cap. Older turns are auto-` +
      `archived as the conversation grows. Click "+ New" to start fresh.`;
  } else if (turns >= HOME_SESSION_MAX_TURNS - 5) {
    counter.classList.add('near-cap');
    counter.title =
      `${turns} of ${HOME_SESSION_MAX_TURNS} turns — approaching the cap. ` +
      `Older turns will be archived after that.`;
  } else {
    counter.title =
      `${turns} of ${HOME_SESSION_MAX_TURNS} turns in this conversation. ` +
      `Click "+ New" to start a separate one.`;
  }
}

function _homeSessionNew() {
  // Cap session count — oldest evicts when the user starts the 21st.
  // Deletion here doesn't ask: at the cap we assume the oldest is the
  // least missed, and the operator can re-create a session for any
  // topic at any time. (The active session is never evicted; the
  // sort places it later in the eviction order since it's most-recent.)
  const sessions = _homeSessionsLoad();
  const ids = Object.keys(sessions);
  if (ids.length >= HOME_SESSIONS_MAX) {
    const sorted = ids.sort((a, b) =>
      (sessions[a].updated_at || '').localeCompare(sessions[b].updated_at || '')
    );
    delete sessions[sorted[0]];
  }
  const fresh = _homeNewSessionRecord();
  sessions[fresh.id] = fresh;
  _homeSessionsSave(sessions);
  _homeSetActiveSessionId(fresh.id);
  _homeChatRestore();
  // Park focus in the prompt so the operator can type immediately.
  const input = document.getElementById('home-prompt-input');
  if (input) input.focus();
}

function _homeSessionSwitch(sid) {
  const sessions = _homeSessionsLoad();
  if (!sessions[sid]) return;
  if (_homeActiveSessionId() === sid) return;  // already active — no-op
  _homeSetActiveSessionId(sid);
  _homeChatRestore();
}

async function _homeSessionDelete(sid) {
  const sessions = _homeSessionsLoad();
  if (!sessions[sid]) return;
  const titleSnap = sessions[sid].title || 'this conversation';
  if (!await confirmModal({ body: `Delete "${titleSnap}"? This can't be undone.`, danger: true })) return;
  const wasActive = _homeActiveSessionId() === sid;
  delete sessions[sid];
  _homeSessionsSave(sessions);
  if (Object.keys(sessions).length === 0) {
    // Last session deleted — bootstrap a fresh one so the chat surface
    // always has somewhere to land.
    const fresh = _homeNewSessionRecord();
    sessions[fresh.id] = fresh;
    _homeSessionsSave(sessions);
    _homeSetActiveSessionId(fresh.id);
  } else if (wasActive) {
    // Active session was deleted — promote the most-recent remaining.
    const newActive = Object.keys(sessions).sort((a, b) =>
      (sessions[b].updated_at || '').localeCompare(sessions[a].updated_at || '')
    )[0];
    _homeSetActiveSessionId(newActive);
  }
  _homeChatRestore();
}

// Back-compat alias — console / debug invocations of the old name still
// do something sensible (start a new session instead of wiping the
// thread silently). Not bound to any UI element after this change.
function _homeChatClear() { _homeSessionNew(); }

function _homeChatKeydown(ev) {
  // Enter sends; Shift+Enter inserts a newline (standard chat-app
  // convention). The textarea handles the newline insertion itself
  // when we don't preventDefault.
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    _homeChatSend();
  }
}

async function _homeChatSend() {
  const input = document.getElementById('home-prompt-input');
  const sendBtn = document.getElementById('home-prompt-send');
  if (!input) return;
  const text = (input.value || '').trim();
  // PWA Phase 1.1.B: allow attachment-only sends (no text). The chip
  // strip above the composer holds whatever the operator dropped /
  // pasted; flush-pending uploads at send-time, below.
  const hasPending = !!(window._pwaPending && window._pwaPending['home-chat']
                        && window._pwaPending['home-chat'].length);
  if (!text && !hasPending) return;

  // Capture the ORIGINATING session at send time. If the operator
  // switches threads while the response is in flight, the user
  // message + evo reply still land on THIS session (the one they
  // actually typed in), not whichever session happens to be active
  // when the JS callback runs. Without this pin, an in-flight
  // switch routes the evo response to the wrong thread — the
  // operator-visible failure mode this fix addresses.
  const sendingSid = _homeActiveSessionId();
  const sendingSession = _homeGetActiveSession();
  // Unconditional OC session id (#1367 follow-up). Previously this
  // was a conditional ``sendingSession.oc_session_id || null`` and
  // we'd omit ``session_id`` from the POST body when the session
  // record lacked the field. On the server, missing ``session_id``
  // falls through to ``derive_session_id(None)`` which mints a
  // FRESH ``admin-ui-anon-<uuid>`` per request — fragmenting one
  // operator-perceived thread across many OC sessions, each
  // starting blank.
  //
  // Defense: always send a stable id. If the session record carries
  // ``oc_session_id`` (the common case post-#1339), use that.
  // Otherwise compute a deterministic id from the local session id
  // (matches the format ``_homeBuildOcSessionId`` would have written
  // had the session been created on a current build). Same browser
  // session always lands in the same OC thread either way.
  const ocSessionId = (
    sendingSession.oc_session_id
    || _homeBuildOcSessionId(sendingSid)
  );

  // User bubble first, then pending evo placeholder.
  const userMsg = { role: 'user', text, ts: new Date().toISOString() };
  _homeChatAppendToSession(sendingSid, userMsg);
  input.value = '';
  autoResizeComposer(input);  // collapse multi-line textarea back to 1 row
  if (sendBtn) sendBtn.disabled = true;
  input.disabled = true;
  const pendingId = 'home-msg-pending-' + Date.now();
  // Render the pending placeholder ONLY if the operator is still on
  // the sending session. If they've already switched, the placeholder
  // would land in the wrong thread's DOM; we'd then orphan it when
  // the response arrives.
  if (_homeActiveSessionId() === sendingSid) {
    _homeChatRenderBubble({
      role: 'evo', text: '…thinking…', pending: true, dom_id: pendingId,
      ts: new Date().toISOString(),
    });
  }
  // Stalled-bubble guards. Three pieces:
  //   (a) AbortController — when the hard ceiling fires we abort the
  //       fetch so the request is actually canceled, not just hidden.
  //       Mobile Safari especially benefits — backgrounded tabs can
  //       leave the fetch dangling indefinitely otherwise.
  //   (b) Slow-indicator interval — after HOME_CHAT_SLOW_INDICATOR_MS
  //       (10 s) we start updating the pending bubble's body text to
  //       "still thinking (Xs)" so the operator can tell the turn is
  //       alive across long multi-tool flows.
  //   (c) Hard timeout — at HOME_CHAT_PENDING_TIMEOUT_MS (5 min) we
  //       abort + flip the placeholder to an error bubble with a ↻
  //       Retry affordance. The proxy's subprocess cap (270 s) fires
  //       a bit earlier so the server's own timeout text usually
  //       reaches us first; the client-side ceiling is the
  //       last-resort guard.
  // All three are cleared in the try/catch/finally once a response
  // arrives.
  const abortCtl = new AbortController();
  const stopIndicator = _evoChatPendingIndicator(
    pendingId, HOME_CHAT_SLOW_INDICATOR_MS,
  );
  let hardTimeoutFired = false;
  const pendingTimeoutId = setTimeout(() => {
    hardTimeoutFired = true;
    try { abortCtl.abort(); } catch (_) {}
    stopIndicator();
    if (_homeActiveSessionId() === sendingSid) {
      const stale = document.getElementById(pendingId);
      if (stale) stale.remove();
    }
    const ceilingS = Math.round(HOME_CHAT_PENDING_TIMEOUT_MS / 1000);
    _homeChatAppendToSession(sendingSid, {
      role: 'evo',
      text: `(no reply after ${ceilingS}s — server may be busy)`,
      error: true,
      retry_text: text,
      ts: new Date().toISOString(),
    });
  }, HOME_CHAT_PENDING_TIMEOUT_MS);
  try {
    // /api/home/chat handles BOTH the keyword dispatch AND the LLM
    // fallback internally. Pass the user's prose verbatim along with
    // the prior conversation so the LLM stays coherent across turns.
    // The server caps history to its configured limit (default 20).
    //
    // CRITICAL: build history from the SENDING session's stored
    // turns, NOT from whatever's currently active. Otherwise an
    // in-flight switch would send the wrong session's history.
    const sendingSessions = _homeSessionsLoad();
    const sendingTurns = (sendingSessions[sendingSid] || {}).turns || [];
    const history = sendingTurns
      .filter(m => !m.pending && !m.error)
      .filter(m => m.role === 'user' || m.role === 'evo')
      // Drop the just-appended user message — server reads it from the
      // top-level `message` field; sending it again as history would
      // duplicate the prompt.
      .slice(0, -1);
    // Send the operator's current authority tier so the LLM can shape
    // its action offers — describe-only for "ask", propose-to-act for
    // "auto". Reads the same localStorage key the tier buttons write.
    const authority = (typeof _homeReadTier === 'function') ? _homeReadTier() : 'ask';
    // local_time anchors temporal references in evo's reply against
    // the OPERATOR's clock (not the admin server's, which may differ).
    // Threaded through the proxy into the <session-context> block.
    const local_time = new Date().toISOString();
    // Each browser session needs its own OC session so multi-session
    // memory doesn't collide on the server. The originating session's
    // oc_session_id was captured at the top of _homeChatSend (set at
    // session create time, or back-filled by _homeGetActiveSession for
    // sessions from PR #1339). Capturing at the top of the function
    // — rather than reading after the await — is what makes the
    // in-flight-switch fix in #1343 hold: a session change mid-flight
    // doesn't reroute the request to the wrong OC session.
    // PWA Phase 1.1.B — upload any dropped/pasted files BEFORE chat
    // POST. Failure here propagates to the catch below so the
    // operator sees a normal error bubble instead of a silent send.
    let attachments = [];
    if (hasPending && typeof window._pwaFlushPending === 'function') {
      attachments = await window._pwaFlushPending('home-chat');
    }
    // #1367 follow-up: session_id is now an unconditional field of
    // the POST body. ``ocSessionId`` is computed above as either the
    // session record's stored ``oc_session_id`` or a deterministic
    // ``admin-ui-home-<browserSid>`` — never null. Sending it on
    // every turn closes the fragmentation bug where missing
    // ``session_id`` caused the proxy to mint fresh anon UUIDs.
    //
    // page_context: home-chat carries the home page pack so the
    // server-side surface_type plumb-through (Phase 1 of the
    // surface-aware help-style spec) reaches the proxy. The drawer
    // already sent page_context on every turn; the home composer now
    // matches.
    const page_context = (typeof _evoDrawerContextPack === 'function')
      ? _evoDrawerContextPack()
      : { page_id: 'home', surface: 'admin_ui', surface_type: 'laptop' };
    // Model tier preference (Auto / Fast / Standard / Power) — per
    // conversation. Server validates and forwards via the subprocess
    // EVOLVE_TIER_PREFERENCE env var to the plugin's ModelRouter.
    // Spec: docs/spec-user-tier-control-2026-05-26.md.
    const tier = (typeof _homeReadModelTier === 'function') ? _homeReadModelTier() : 'auto';
    const postBody = {
      message: text, history, authority, tier, local_time, attachments,
      session_id: ocSessionId, page_context,
    };
    // Diagnostic mode override — when evo's gateway is down and the
    // operator clicked "Talk to diagnostic LLM" on the gateway-down
    // bubble, every subsequent send routes to the pod-wide Haiku
    // diagnostic endpoint instead of evo's OC gateway. _diagnosticMode
    // stays true until the operator clicks "Switch back to evo" in the
    // banner above the composer. The diagnostic endpoint accepts a
    // subset of the post body — message + history are sufficient.
    const endpoint = _diagnosticMode ? '/api/diagnostic/chat' : '/api/home/chat';
    const sendBody = _diagnosticMode
      ? { message: text, history }
      : postBody;
    const resp = await api('POST', endpoint, sendBody, {
      signal: abortCtl.signal,
    });
    // Reply arrived (or was aborted) — clear the slow-indicator and
    // the hard timeout. Order matters: if the hard timeout fired
    // first it already rendered the error bubble; skip the
    // success/error rendering below so we don't double-render.
    clearTimeout(pendingTimeoutId);
    stopIndicator();
    if (hardTimeoutFired || resp?.aborted) {
      return;
    }
    // network_error means the fetch itself failed (Safari's bare
    // "Load failed", DNS/TLS, admin-ui daemon bouncing during a
    // kickstart). Render a friendly evo-voice bubble + retry
    // affordance instead of the raw browser error string. The
    // browser error is still in the error log (logError already
    // ran inside api()) for diagnostic purposes.
    if (resp?.network_error) {
      const pending = document.getElementById(pendingId);
      if (pending) pending.remove();
      _homeChatAppendToSession(sendingSid, {
        role: 'evo',
        text: "Sorry — something went wrong and I didn't get a response. Please try again.",
        error: true,
        retry_text: text,
        ts: new Date().toISOString(),
      });
      return;
    }
    const reply = (resp && resp.reply) || resp?.error || '(no response)';
    // Drop the pending placeholder. ``getElementById`` returns null if
    // the operator switched threads (DOM re-rendered without us);
    // that's fine — the remove is a no-op in that case.
    const pending = document.getElementById(pendingId);
    if (pending) pending.remove();
    const meta = { role: 'evo', text: reply, ts: new Date().toISOString() };
    // Phase 4.1 proxy: source === 'evo' on success, 'proxy_error' on
    // subprocess/timeout failure. ``proxy_warn`` is the empty-reply
    // case (Phase 1 of the surface-aware help-style spec §8 +
    // diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md) —
    // OC ran but produced no closing text turn; the proxy synthesized a
    // confirmation from the session-jsonl tool calls. Rendered as a
    // yellow informational bubble so the operator can tell "work
    // probably succeeded, verify below" from "subprocess crashed".
    // Legacy 'llm'/'cap_exceeded'/'llm_error' branches kept for
    // rollback safety during deploys.
    if (resp?.source === 'evo' && resp.model) {
      const usage = resp.usage || {};
      const tokens = (usage.input || 0) + (usage.output || 0);
      const tokenStr = tokens ? ` · ${tokens} tok` : '';
      const tierLabel = (typeof _modelTierLabel === 'function') ? _modelTierLabel(resp.model) : '';
      const tierPrefix = tierLabel ? `${tierLabel}: ` : '';
      meta.text += `\n\n_(${tierPrefix}${resp.model}${tokenStr})_`;
    }
    // tier_capped — the requested role was downgraded because its daily
    // cap is exhausted for the primary bot today. Build the message from
    // the requested tier (resp.tier) + what actually ran
    // (resp.effective_tier) so the max path reads correctly:
    //   Max → Power     "Max capped today — used Power for this turn."
    //   Max → Standard  "Max capped today — used Standard for this turn."
    //   Power → Standard"Power capped today — used Standard for this turn."
    // Spec: docs/spec-user-tier-control-2026-05-26.md "Power capped surface"
    // + spec-model-rungs-and-roles §max semantics. Italics, never a banner.
    if (resp?.tier_capped) {
      meta.text += `\n\n_${_homeCappedMessage(resp.tier, resp.effective_tier)}_`;
    }
    if (resp?.source === 'proxy_warn') {
      meta.warn = true;
    } else if (resp?.source === 'gateway_down') {
      // Evo's gateway is down — the proxy already confirmed via probe.
      // Render as an error bubble with the diagnostic-LLM affordance
      // (button injected by _homeChatRenderBubble when msg.gateway_down).
      // ``retry_text`` carries the operator's original message so the
      // one-click switch can re-send it through the diagnostic LLM
      // without retyping.
      meta.error = true;
      meta.gateway_down = true;
      meta.retry_text = text;
    } else if (resp?.source === 'proxy_error') {
      meta.error = true;
    } else if (resp?.source === 'llm' && typeof resp.cost_usd === 'number') {
      const cap = resp.cap_status || {};
      meta.text +=
        `\n\n_(${resp.model} · ${resp.input_tokens}+${resp.output_tokens} tok` +
        ` · ~$${resp.cost_usd.toFixed(4)} ·` +
        ` ${cap.used ?? '?'}/${cap.daily_cap ?? '?'} today)_`;
    } else if (resp?.source === 'cap_exceeded' || resp?.source === 'llm_error') {
      meta.error = true;
    }
    // Server-extracted suggested actions ride along on the bubble so a
    // click can submit them as a new chat turn. Empty array when none.
    if (Array.isArray(resp?.suggested_actions) && resp.suggested_actions.length) {
      meta.suggested_actions = resp.suggested_actions;
    }
    // Append to the ORIGINATING session, not "active". The session
    // strip will re-render (chip moves to most-recent slot), but the
    // current thread DOM only updates if the operator is still on
    // the sending session.
    _homeChatAppendToSession(sendingSid, meta);
  } catch (e) {
    clearTimeout(pendingTimeoutId);
    stopIndicator();
    // If the hard timeout already fired, the timeout handler rendered
    // the error bubble and we'd just be double-rendering. Skip.
    if (hardTimeoutFired || (e && e.name === 'AbortError')) {
      return;
    }
    const pending = document.getElementById(pendingId);
    if (pending) pending.remove();
    // Reached only on truly unexpected client-side errors — fetch
    // failures are intercepted by the resp.network_error check above,
    // and aborts return early. So this is JSON-parse / unexpected-shape
    // / DOM-race territory. Keep the message friendly + retryable; the
    // raw error string is in the error log via api()'s logError.
    _homeChatAppendToSession(sendingSid, {
      role: 'evo',
      text: "Sorry — something went wrong on my end. Please try again.",
      error: true,
      retry_text: text,
      ts: new Date().toISOString(),
    });
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

// ── Bot tile stack (right rail) ─────────────────────────────────────────────
// Each rail tile has two views: a compact chip head (default) and the
// full `pod-node` expansion (same component the Dashboard uses). State
// persists per-bot in localStorage so the operator's choices survive
// reloads and the 60s background refresh.

const HOME_TILE_KEY_PREFIX = 'evolve_home_tile_';

function _homeTileIsExpanded(botId) {
  try { return localStorage.getItem(HOME_TILE_KEY_PREFIX + botId) === '1'; }
  catch (_) { return false; }
}

function _homeTileToggle(botId) {
  const tile = document.querySelector(
    `.home-rail-tile[data-bot-id="${CSS.escape(botId)}"]`
  );
  if (!tile) return;
  const expanded = tile.dataset.expanded === '1';
  const next = expanded ? '0' : '1';
  tile.dataset.expanded = next;
  try { localStorage.setItem(HOME_TILE_KEY_PREFIX + botId, next); } catch (_) {}
}

function _homeBotStatusClass(b) {
  if (b.gateway_status_fresh === true && b.gateway_reachable === false) return 'offline';
  if (b.live) return 'online';
  if (b.last_metric_date) return 'active';
  return 'offline';
}

function _homeRailTileWrapper(id, b) {
  // Wraps one renderPodNode tile in the rail-collapse shell:
  //   - compact chip head (visible when collapsed)
  //   - the pod-node (visible when expanded)
  //   - an .expand-icon chevron collapse button in the top-right of the
  //     expanded view (§9.13)
  // CSS controls which is shown via the data-expanded attribute.
  const expanded = _homeTileIsExpanded(id) ? '1' : '0';
  const label = botLabel ? botLabel(id) : id;
  const status = _homeBotStatusClass(b);
  const chips = (b.tile && b.tile.health_chips) || [];
  const bad = chips.find(c => c.severity === 'crit')
    || chips.find(c => c.severity === 'warn');
  const warnHtml = bad
    ? `<span class="home-rail-tile-warn ${escHtml(bad.severity)}" title="${escHtml(bad.label || '')}">⚠</span>`
    : '';
  const escId = escHtml(id);
  return `<div class="home-rail-tile" data-bot-id="${escId}" data-expanded="${expanded}">
    <div class="home-rail-tile-chip" onclick="_homeTileToggle('${escId}')">
      <span class="home-rail-tile-dot ${escHtml(status)}"></span>
      <span class="home-rail-tile-name">${escHtml(label)}</span>
      ${warnHtml}
      <span class="home-rail-tile-caret expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
    </div>
    <button class="home-rail-tile-collapse" onclick="_homeTileToggle('${escId}')" title="Collapse"><span class="expand-icon is-open" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span></button>
    ${renderPodNode(id, b)}
  </div>`;
}

function renderHomeBotTiles() {
  const el = document.getElementById('home-bot-tiles');
  if (!el) return;
  if (!_statusData) {
    el.innerHTML = '<div class="loading"><span class="spinner"></span>Loading bots…</div>';
    return;
  }
  const bots = _statusData.bots || {};
  const ordered = orderedBotIds(bots).map(id => [id, bots[id]]);

  if (ordered.length === 0) {
    el.innerHTML = '<div class="empty">No bots yet. <button class="btn btn-ghost btn-sm" onclick="openAddBotModal()">Add one →</button></div>';
    return;
  }
  el.innerHTML = ordered.map(([id, b]) => _homeRailTileWrapper(id, b)).join('');
}

// ── Structured narrative (rule-based, zero tokens) ──────────────────────────
// Composes a conversational paragraph + collapsed "smaller stuff" from the
// live signal + proposal feed. Producers craft signal.title to be
// human-readable; this renderer's job is selection, grouping, and tone —
// not generating new text. LLM synthesis is a separate, opt-in layer.

function _renderHomeNarrativeLoading() {
  // Paints a "reading the feed…" placeholder into the report banner while
  // the parallel fetches resolve. Splits the prose body from the extras
  // (top proposals + smaller-stuff toggle) so the LLM-swap path can update
  // just the prose without clobbering the action surface.
  const prose = document.getElementById('home-narr-prose');
  const extras = document.getElementById('home-narr-extras');
  const actions = document.getElementById('home-evo-actions');
  if (prose) prose.innerHTML = `<p style="color:var(--text3)">Reading the signal feed…</p>`;
  if (extras) extras.innerHTML = '';
  if (actions) actions.innerHTML = '';
  const src = document.getElementById('home-report-source');
  if (src) src.textContent = '';
}

// Severity ranking — legacy fallback when severity_framework is absent
// from the signal payload. Once every producer is retrofitted, this
// stays as a defensive fallback only.
const _HOME_SEV_RANK = { alert: 3, warn: 2, info: 1 };

// Server-side severity framework injection lives at
// signal.severity_framework = { vector, magnitude, priority, bucket }.
// Spec: docs/spec-severity-framework-2026-05-18.md. When present, the
// Home renderer uses these for sort + big/small split; otherwise it
// falls through to the legacy severity-rank logic below.
function _homePriority(sig) {
  if (sig && sig.severity_framework && typeof sig.severity_framework.priority === 'number') {
    return sig.severity_framework.priority;
  }
  return null;
}

function _homeIsBigSignal(sig) {
  // Preferred path: server-computed bucket. "lead" + "in_narrative"
  // both count as BIG; "small" collapses.
  const fw = sig && sig.severity_framework;
  if (fw && fw.bucket) {
    return fw.bucket === 'lead' || fw.bucket === 'in_narrative';
  }
  // Legacy fallback: alert severity OR new since last visit OR
  // recent warn within 24h.
  if (sig.severity === 'alert') return true;
  if (_homeLastSeenAtRender && sig.created_at) {
    if (new Date(sig.created_at) > _homeLastSeenAtRender) return true;
  }
  if (sig.severity === 'warn' && sig.last_observed_at) {
    const age = (Date.now() - new Date(sig.last_observed_at).getTime()) / 1000;
    if (age < 86400) return true;
  }
  return false;
}

function _homeSortSignals(sigs) {
  // When the severity framework is on the payload, sort by composed
  // priority desc. Falls back to legacy severity-rank sort for any
  // signal still emitting under the old shape.
  return sigs.slice().sort((a, b) => {
    const pa = _homePriority(a);
    const pb = _homePriority(b);
    if (pa != null && pb != null) {
      if (pb !== pa) return pb - pa;
    } else if (pa != null) {
      return -1;
    } else if (pb != null) {
      return 1;
    }
    const da = (_HOME_SEV_RANK[b.severity] || 0) - (_HOME_SEV_RANK[a.severity] || 0);
    if (da !== 0) return da;
    return String(b.last_observed_at || '').localeCompare(String(a.last_observed_at || ''));
  });
}

function _homeGroupByIncident(sigs) {
  // Coalesce signals sharing an incident_key into one bucket. Producers
  // set this slug when multiple signals are symptoms of the same root
  // cause. Returns [{ key, signals[], severity }] — one bucket per
  // unkeyed signal too (so the caller just iterates buckets uniformly).
  const buckets = [];
  const byKey = new Map();
  for (const s of sigs) {
    if (s.incident_key && byKey.has(s.incident_key)) {
      byKey.get(s.incident_key).signals.push(s);
    } else if (s.incident_key) {
      const b = { key: s.incident_key, signals: [s] };
      byKey.set(s.incident_key, b);
      buckets.push(b);
    } else {
      buckets.push({ key: null, signals: [s] });
    }
  }
  // Severity = the max within the bucket; sort by that.
  for (const b of buckets) {
    b.severity = b.signals.reduce(
      (acc, s) => Math.max(acc, _HOME_SEV_RANK[s.severity] || 0), 0
    );
  }
  buckets.sort((a, b) => b.severity - a.severity);
  return buckets;
}

function _homeSignalLabel(sig) {
  // Producer-supplied title is the authoritative human-readable string.
  // Fall back to a synthesized form if a producer slipped without one.
  if (sig.title && sig.title.trim()) return sig.title.trim();
  if (sig.bot_id) return `${botLabel(sig.bot_id)} — ${sig.type}`;
  return sig.type || sig.id;
}

function _homeBucketHeadline(bucket) {
  // One human-readable sentence per incident-bucket. If the bucket has
  // multiple symptoms (incident_key match), prefix with the symptom count.
  const top = bucket.signals[0];
  const label = _homeSignalLabel(top);
  const when = top.last_observed_at ? ago(top.last_observed_at) : '';
  const botBit = top.bot_id ? ` (${escHtml(botLabel(top.bot_id))})` : '';
  const symptomBit = bucket.signals.length > 1
    ? ` <span style="color:var(--text3);font-size:0.82rem">+ ${bucket.signals.length - 1} related</span>`
    : '';
  const sevDot = bucket.severity >= 3
    ? '<span style="color:var(--red)">●</span> '
    : bucket.severity >= 2 ? '<span style="color:var(--yellow)">●</span> ' : '';
  // Severity-framework vector tag (security / cost / operations / quality)
  // — shown as a small chip when the producer has been retrofitted to
  // emit explicit (vector, magnitude). Tooltip carries the priority
  // breakdown for operators who want to know why it's ranked here.
  const fw = top.severity_framework;
  let vecTag = '';
  if (fw && fw.vector) {
    const tip = `${fw.vector}:${fw.magnitude} → priority ${fw.priority}`;
    vecTag = ` <span class="home-narr-vec home-narr-vec-${escHtml(fw.vector)}" title="${escHtml(tip)}">${escHtml(fw.vector)}</span>`;
  }
  return `${sevDot}<strong>${escHtml(label)}</strong>${botBit}${vecTag}${symptomBit} <span style="color:var(--text3);font-size:0.82rem">— ${escHtml(when)}</span>`;
}

function _homeNarrativeHeadline({ bigBuckets, smallCount, proposals, newProposals }) {
  // One-sentence opening. Tone target: friendly, factual, no jargon.
  // Avoid "you have N things" — say what kind of things.
  const pendingCount = proposals.filter(p => p.status === 'pending').length;
  if (bigBuckets.length === 0 && pendingCount === 0 && smallCount === 0) {
    return `All quiet. Nothing firing and nothing in the proposal queue.`;
  }
  if (bigBuckets.length === 0 && pendingCount > 0) {
    const newBit = newProposals > 0 ? ` (${newProposals} new since you were here)` : '';
    return `Nothing firing. ${pendingCount} pending proposal${pendingCount === 1 ? '' : 's'} waiting on you${newBit}.`;
  }
  if (bigBuckets.length === 1) {
    return `One thing to look at.`;
  }
  if (bigBuckets.length === 2) {
    return `Two things worth a look.`;
  }
  // Distinct bots involved — useful framing when many things are firing.
  const bots = new Set();
  bigBuckets.forEach(b => b.signals.forEach(s => s.bot_id && bots.add(s.bot_id)));
  if (bots.size > 1) {
    return `${bigBuckets.length} things firing across ${bots.size} bots.`;
  }
  if (bots.size === 1) {
    const only = [...bots][0];
    return `${bigBuckets.length} things firing on ${escHtml(botLabel(only))}.`;
  }
  return `${bigBuckets.length} things firing.`;
}

function _homeNarrativeSmallLine(buckets) {
  if (buckets.length === 0) return '';
  // Group by producer for a compact summary line.
  const byProducer = new Map();
  let total = 0;
  for (const b of buckets) {
    for (const s of b.signals) {
      const k = s.producer || 'other';
      byProducer.set(k, (byProducer.get(k) || 0) + 1);
      total++;
    }
  }
  const parts = [...byProducer.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([k, v]) => `${v} from <code>${escHtml(k)}</code>`);
  return `${total} smaller signal${total === 1 ? '' : 's'} — ${parts.join(', ')}.`;
}

async function renderHomeNarrative({
  firing = [], proposals = [],
  ocVersion = null, watchlist = null, podHealth = null, hostHealth = null,
} = {}) {
  // Two render targets:
  //   #home-narr-prose   — the headline + big-bucket prose (LLM may swap it later)
  //   #home-narr-extras  — top-N proposals + "smaller stuff" toggle (rule-based always)
  // Keeping them separate means the LLM swap doesn't disturb the action surface.
  const prose = document.getElementById('home-narr-prose');
  const extras = document.getElementById('home-narr-extras');
  const actions = document.getElementById('home-evo-actions');
  if (!prose || !extras || !actions) return;
  const src = document.getElementById('home-report-source');
  if (src) src.textContent = 'rule-based · just now';

  // Split firing signals into "big" vs "small" buckets.
  const sorted = _homeSortSignals(firing);
  const bigSigs = sorted.filter(_homeIsBigSignal);
  const smallSigs = sorted.filter(s => !_homeIsBigSignal(s));
  const bigBuckets = _homeGroupByIncident(bigSigs).slice(0, 5);  // cap surface
  const smallBuckets = _homeGroupByIncident(smallSigs);

  const pending = proposals.filter(p => p.status === 'pending');
  const snoozed = proposals.filter(p => p.status === 'snoozed');
  const pendingCount = pending.length;

  const newProposals = _homeLastSeenAtRender
    ? pending.filter(p => {
        const c = p.created_at ? new Date(p.created_at) : null;
        return c && c > _homeLastSeenAtRender;
      }).length
    : 0;

  const headline = _homeNarrativeHeadline({
    bigBuckets, smallCount: smallSigs.length, proposals, newProposals
  });

  // BIG items — flatten into one prose paragraph instead of a bullet
  // list. The producer-supplied ``title`` is already operator-readable;
  // we just stitch them with timing into a sentence so the narrative
  // reads like a friendly briefing rather than a technical to-do list.
  // Per-signal Snooze/Dismiss/Run-fix actions deliberately live on the
  // Alerts page only — Home is the summary; Alerts is the work surface.
  const bigHtml = _homeRenderBigProse(bigBuckets);

  // Version drift, proposal-breakdown, and OC upgrade — these were
  // top-level lines before but they crowd the narrative when the
  // operator's already looking at active alerts. Roll them into the
  // smaller-stuff bucket so they're available on demand without
  // crowding the briefing.
  const propBreakdownHtml = "";   // moved into smallParts below

  // ── Top-N pending proposals with inline actions ───────────────────────
  // Show the highest-urgency 2-3 proposals as actionable rows so the
  // operator can Take/Snooze/Dismiss without leaving Home. Skipped when
  // there are big alerts in view — those compete for the same eye-flow
  // and the user can still drill via "Review proposals".
  const propTopHtml = bigBuckets.length === 0
    ? _homeRenderTopProposals(pending)
    : '';

  // ── "Smaller stuff" composite ─────────────────────────────────────────
  // One collapsed section that holds: low-severity signals, pod-health
  // summary, watchlist trend, disk-borderline mention, and held-proposal
  // count. Keeping these in one place stops them from cluttering the
  // primary narrative when the operator already has alerts to deal with.
  const smallParts = [];
  const smallSignalsLine = _homeNarrativeSmallLine(smallBuckets);
  if (smallSignalsLine) smallParts.push(smallSignalsLine);

  // Version drift — softened from a top-level yellow warning to a
  // smaller-stuff entry. Still surfaced; just not alarming.
  const versionDriftLine = _homeVersionDriftLine({ inline: true });
  if (versionDriftLine) smallParts.push(versionDriftLine);

  // Proposal breakdown — same treatment. Ribbon already shows the
  // bare count; the breakdown lives in smaller stuff for operators
  // who want the per-dimension split without clicking through.
  const propLine = _homeProposalsBreakdown({
    pending, snoozed, newProposals, bigBucketsExist: bigBuckets.length > 0, inline: true,
  });
  if (propLine) smallParts.push(propLine);

  const phLine = _homePodHealthLine(podHealth);
  if (phLine) smallParts.push(phLine);

  const wlLine = _homeWatchlistLine(watchlist);
  if (wlLine) smallParts.push(wlLine);

  const diskLine = _homeDiskBorderlineLine(hostHealth);
  if (diskLine) smallParts.push(diskLine);

  const heldLine = _homeHeldProposalsLine(snoozed);
  if (heldLine) smallParts.push(heldLine);

  const ocLine = _homeOcUpgradeLine(ocVersion);
  if (ocLine) smallParts.push(ocLine);

  const smallHtml = smallParts.length > 0
    ? `<div class="home-evo-small">
        Smaller stuff:
        <span class="home-evo-small-toggle" onclick="_homeToggleSmall()" id="home-evo-small-toggle-label">show <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span></span>
        <div id="home-evo-small-body" style="display:none;margin-top:8px;color:var(--text2);font-size:0.85rem">
          <ul style="margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px">
            ${smallParts.map(p => `<li>${p}</li>`).join('')}
          </ul>
        </div>
      </div>`
    : '';

  // Prose lives in the report banner's body; extras (top proposals +
  // smaller-stuff toggle) live in the banner's extras slot so the LLM
  // swap can update only the prose without disturbing the action surface.
  prose.innerHTML = `<p>${headline}</p>${bigHtml}`;
  extras.innerHTML = `${propTopHtml}${smallHtml}`;

  // Fire-and-forget LLM narrative refresh. Replaces the prose-only
  // portion (#home-narr-prose) with the friendlier model-generated
  // summary when the response comes back. Rule-based prose stays as
  // the fallback when no key, cap exceeded, or LLM error.
  _homeFetchLLMNarrative();

  // Action banner stays empty by design now. The chat surface offers
  // specific suggested-action buttons per evo reply (extracted from
  // backticked subcommand mentions), and the sidebar handles navigation
  // — a row of generic "Open alerts / Review proposals / Sync bots"
  // shortcuts duplicated both and read as noise when the operator is
  // mid-conversation. Empty content + the `:empty { display: none }`
  // CSS rule collapses the row.
  actions.innerHTML = '';
}

// ── LLM-driven narrative (cached, fallback to rule-based) ──────────────────
// Fired after the rule-based narrative lands so the operator gets a fast
// first paint either way. The endpoint returns a cached entry when the
// pod-state digest hasn't changed (5-min TTL by default), or generates
// fresh prose via Haiku. Rule-based stays in the DOM as fallback when
// the LLM path is unavailable.
//
// Re-entrancy guard: `loadHome._narrativeFetchInflight` prevents the
// 60s background refresh from stacking concurrent fetches if one is
// still in flight. Manual refresh (the ↻ button in the narrative
// footer) explicitly clears the guard before re-firing.

async function _homeFetchLLMNarrative(opts) {
  if (loadHome._narrativeFetchInflight) return;
  const refresh = !!(opts && opts.refresh);
  loadHome._narrativeFetchInflight = true;
  try {
    const path = refresh ? '/api/home/narrative?refresh=1' : '/api/home/narrative';
    const resp = await api('GET', path);
    if (!resp || !resp.text) {
      // no_llm / cap_exceeded / llm_error — leave the rule-based
      // narrative in place. We don't render error banners; the
      // operator sees the perfectly readable fallback prose.
      return;
    }
    _homeSwapNarrativeProse(resp);
  } catch (_) {
    // Network blip or transient 500 — silent fallback to rule-based.
  } finally {
    loadHome._narrativeFetchInflight = false;
  }
}

function _homeSwapNarrativeProse(resp) {
  // Replace the rule-based prose with the LLM summary. The source/cost
  // metadata moves to the report-head source span (next to the title);
  // the refresh button is the ↻ already living in the report head, so we
  // don't render a separate footer button here anymore.
  const prose = document.getElementById('home-narr-prose');
  if (!prose) return;
  const safeText = _homeChatFormatBody(resp.text || '');
  prose.innerHTML = safeText;
  const src = document.getElementById('home-report-source');
  if (src) {
    const source = resp.source || 'llm';
    const sourceLabel = source === 'cache' ? 'cached' : 'fresh';
    const model = resp.model || 'haiku';
    const generatedAt = resp.generated_at ? ` · ${ago(resp.generated_at)}` : '';
    const costBit = (source === 'llm' && typeof resp.cost_usd === 'number')
      ? ` · ~$${resp.cost_usd.toFixed(4)}`
      : '';
    src.textContent = `${model} · ${sourceLabel}${generatedAt}${costBit}`;
  }
}

function _homeRefreshReport() {
  // Click handler for the ↻ in the report header. Forces a fresh LLM
  // narrative (skips cache, counts against the daily cap). Clears the
  // inflight guard first so a stale in-flight request doesn't block.
  loadHome._narrativeFetchInflight = false;
  _homeFetchLLMNarrative({ refresh: true });
}

function _homeChatToggleHelp() {
  // Toggle the inline help popover beneath the prompt input. Replaces
  // the noisy "ask evo — try `help` for…" placeholder text.
  const pop = document.getElementById('home-prompt-help-pop');
  if (!pop) return;
  pop.style.display = pop.style.display === 'none' ? 'block' : 'none';
}


// ── Tier-1 narrative additions ──────────────────────────────────────────────
// Pure-Python-shaped helpers (no LLM, no extra fetches per render) that pull
// data already in _statusData or the parallel-fetched payloads.

function _homeAnyVersionDrift() {
  if (!_statusData) return false;
  if (_statusData.any_bots_out_of_sync === true) return true;
  return false;
}

function _homeVersionDriftLine(opts) {
  // ``opts.inline`` requests the bare prose suitable for the smaller-
  // stuff list (no wrapping <p>). When omitted, returns the standalone
  // yellow-warning paragraph format that older callers expect.
  if (!_homeAnyVersionDrift()) return '';
  const bots = (_statusData && _statusData.bots) || {};
  // Exclude primary bots — their version reflects local code, not deploy
  // state (mirrors release_notes.min_deployed_version handling).
  const member = Object.entries(bots).filter(([, b]) => b.role !== 'primary');
  const out = member.filter(([, b]) => b.evolve_synced === false).length;
  const total = member.length;
  if (out === 0) return '';
  // Date-led label, not the raw version — the PR-number tail reads as an
  // ordinal even though recency lives in the date (D-2). "Jun 14, 2026".
  const current = _statusData.evolve_current_version || '';
  const curLabel = current ? verDateLabel(current) : '';
  const prose = `${out}/${total} bots behind the current Evolve release${curLabel ? ` (admin on ${escHtml(curLabel)})` : ''}.`;
  if (opts && opts.inline) return prose;
  return `<p style="color:var(--yellow);font-size:0.85rem;margin-top:10px">${prose}</p>`;
}

function _homeProposalsBreakdown({ pending, snoozed: _snoozed, newProposals, bigBucketsExist, inline }) {
  if (pending.length === 0) return '';
  // Group by dimension. Empty dimension falls into "other".
  const byDim = new Map();
  for (const p of pending) {
    const d = (p.dimension || 'other').toLowerCase();
    byDim.set(d, (byDim.get(d) || 0) + 1);
  }
  const parts = [...byDim.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([d, n]) => `${n} ${escHtml(d)}`);
  const intro = bigBucketsExist
    ? `${pending.length} proposal${pending.length === 1 ? '' : 's'} in your queue — ${parts.join(', ')}.`
    : `Queue: ${parts.join(', ')}${newProposals ? `; ${newProposals} new since you were here` : ''}.`;
  if (inline) return intro;
  return `<p style="color:var(--text2);font-size:0.85rem;margin-top:10px">${intro}</p>`;
}

// ── Friendlier prose for the "big" signal buckets ────────────────────────────
// Replaces the bullet list that used to render here. Producer-supplied
// signal.title is already operator-readable; we stitch them into a
// sentence with timing so the briefing reads like one paragraph
// instead of a checklist.
function _homeRenderBigProse(buckets) {
  if (!buckets || buckets.length === 0) return '';
  const titles = buckets.map(b => {
    const top = b.signals[0];
    const title = _homeSignalLabel(top);
    const when = top.last_observed_at ? ago(top.last_observed_at) : '';
    const symptomBit = b.signals.length > 1
      ? ` (+${b.signals.length - 1} related)`
      : '';
    return { title, when, symptomBit, severity: b.severity };
  });
  // 1 bucket → "Title — when."
  // 2 buckets → "Title1 (when1), and Title2 (when2)."
  // 3+ buckets → "Title1 (when1), Title2 (when2), and N more."
  let sentence;
  if (titles.length === 1) {
    const t = titles[0];
    sentence = `${escHtml(t.title)}${t.symptomBit ? escHtml(t.symptomBit) : ''}` +
      (t.when ? ` — <span style="color:var(--text3)">${escHtml(t.when)}</span>` : '') + '.';
  } else if (titles.length === 2) {
    sentence = titles
      .map(t => `${escHtml(t.title)}${t.symptomBit ? escHtml(t.symptomBit) : ''}` +
        (t.when ? ` <span style="color:var(--text3)">(${escHtml(t.when)})</span>` : ''))
      .join(', and ') + '.';
  } else {
    const lead = titles.slice(0, 2).map(t =>
      `${escHtml(t.title)}${t.symptomBit ? escHtml(t.symptomBit) : ''}` +
      (t.when ? ` <span style="color:var(--text3)">(${escHtml(t.when)})</span>` : '')
    ).join(', ');
    const rest = titles.length - 2;
    sentence = `${lead}, and ${rest} more.`;
  }
  return `<p style="margin-top:8px">${sentence}</p>`;
}

function _homePodHealthLine(podHealth) {
  if (!podHealth || !podHealth.summary) return '';
  const s = podHealth.summary;
  if (s.fail > 0) {
    return `Pod-health: <span style="color:var(--red)">${s.fail} failing</span>, ${s.warn} warn, ${s.pass} passing.`;
  }
  if (s.warn > 0) {
    return `Pod-health: <span style="color:var(--yellow)">${s.warn} warn</span>, ${s.pass} passing.`;
  }
  return `Pod-health: ${s.pass} checks passing, all green.`;
}

function _homeWatchlistLine(watchlist) {
  if (!watchlist || !watchlist.total || watchlist.total === 0) return '';
  // Group by generator_id (the "coach" name) to spot trends. Top one only.
  const cands = watchlist.candidates || [];
  const byGen = new Map();
  for (const c of cands) {
    const g = c.generator_id || 'unknown';
    byGen.set(g, (byGen.get(g) || 0) + 1);
  }
  const top = [...byGen.entries()].sort((a, b) => b[1] - a[1])[0];
  const trend = top ? ` (top: ${escHtml(top[0])} ×${top[1]})` : '';
  return `RSI watchlist: ${watchlist.total} candidate${watchlist.total === 1 ? '' : 's'} tracking${trend}.`;
}

function _homeDiskBorderlineLine(hostHealth) {
  if (!hostHealth || !hostHealth.disk) return '';
  const pct = hostHealth.disk.percent;
  // Host-health emits a Signal at its own warn threshold (typically 90%+).
  // Surface a heads-up in the narrative for the borderline range so 80%
  // doesn't slide past the operator unnoticed even though it's not yet
  // signal-worthy. Below 75% is silent.
  if (pct == null || pct < 75) return '';
  const sev = pct >= 90 ? 'red' : pct >= 85 ? 'yellow' : 'text2';
  const color = sev === 'red' ? 'var(--red)' : sev === 'yellow' ? 'var(--yellow)' : 'var(--text2)';
  return `Disk at <span style="color:${color}">${pct.toFixed(0)}%</span>${hostHealth.disk.free_bytes != null ? ` (${_fmtBytes(hostHealth.disk.free_bytes)} free)` : ''}.`;
}

function _homeHeldProposalsLine(snoozed) {
  if (!snoozed || snoozed.length === 0) return '';
  // Held = snoozed and snoozed_until is still in the future. If the
  // server already filtered to active snoozes the length is already
  // correct; double-check just in case.
  const now = Date.now();
  const active = snoozed.filter(p => {
    if (!p.snoozed_until) return true;
    return new Date(p.snoozed_until).getTime() > now;
  });
  if (active.length === 0) return '';
  return `${active.length} proposal${active.length === 1 ? '' : 's'} snoozed (will resurface later).`;
}

function _homeOcUpgradeLine(ocVersion) {
  if (!ocVersion || !ocVersion.bots) return '';
  // Bot-level update_available flag is the authoritative signal here.
  const bots = Object.values(ocVersion.bots);
  const available = bots.filter(b => b && b.update_available).length;
  if (available === 0) return '';
  // Find the latest version string (any bot will do).
  const latest = bots.find(b => b && b.latest);
  const latestVer = latest && latest.latest ? latest.latest : '';
  return `OpenClaw upgrade available${latestVer ? ` (latest: ${escHtml(latestVer)})` : ''} — ${available} bot${available === 1 ? '' : 's'} eligible.`;
}

function _homeOpenSignal(_signalId) {
  // First-pass drill: jump to the Alerts surface. A future pass can deep-
  // link to the specific signal detail.
  nav(document.querySelector('[data-page=maintenance]'));
}

// ── Per-signal bucket row ────────────────────────────────────────────────────
// Renders one row per incident-bucket with inline Snooze / Dismiss / Run fix
// buttons. The leader signal of the bucket drives the actions (snooze a
// gateway-down + auth-failed pair as one incident, in line with the
// incident_key coalescing). Clicking the headline drills to Alerts;
// clicking the action row stops propagation so it doesn't drill.
function _homeRenderBigBucketRow(bucket) {
  const leader = bucket.signals[0];
  const sigId = escHtml(leader.id || '');
  const headline = _homeBucketHeadline(bucket);
  const actions = [];
  actions.push(`<button class="btn btn-ghost btn-sm" onclick="_homeSignalSnooze('${sigId}', this)">Snooze 1h</button>`);
  actions.push(`<button class="btn btn-ghost btn-sm" onclick="_homeSignalDismiss('${sigId}', this)">Dismiss</button>`);
  if (leader.remediation && leader.remediation.kind) {
    // Reuse the alerts page's confirm-modal flow so Home, Alerts, and the
    // signal-detail surface all share one Run-Fix path. The payload is
    // URL-encoded JSON; _alOpenRemediation parses it on click.
    const payload = encodeURIComponent(JSON.stringify(leader.remediation));
    const label = escHtml(leader.remediation.label || `Run ${leader.remediation.kind}`);
    actions.push(`<button class="btn btn-primary btn-sm" onclick="_alOpenRemediation('${sigId}', '${payload}')">${label}</button>`);
  }
  return `<li class="home-narr-row" id="home-sig-row-${sigId}">
    <div class="home-narr-head" onclick="_homeOpenSignal('${sigId}')">${headline}</div>
    <div class="home-narr-acts" onclick="event.stopPropagation()">${actions.join('')}</div>
  </li>`;
}

// ── Per-signal action dispatchers ────────────────────────────────────────────
// Each posts to the same endpoint the Alerts page uses; the response is
// just the new state. After success we re-fire loadHome() so the row
// vanishes (snooze → out of firing list; dismiss → out of active set)
// and the headline ribbon counts update.

const _HOME_URGENCY_RANK = {
  security_critical: 6,
  operational_urgent: 5,
  cost_alert: 4,
  substrate_warn: 3,
  improvement: 2,
  hygiene: 1,
  whimsy: 0,
};

async function _homeSignalSnooze(sigId, btn) {
  const row = document.getElementById(`home-sig-row-${sigId}`);
  if (row) row.classList.add('busy');
  try {
    const r = await api('POST', `/api/signals/${encodeURIComponent(sigId)}/snooze`, { duration: '1h' });
    if (r && r.error) throw new Error(r.error);
    toast('Snoozed for 1h', 'ok');
    await loadHome();
  } catch (e) {
    if (row) row.classList.remove('busy');
    toast(`Snooze failed: ${String(e && e.message || e)}`, 'err');
  }
}

async function _homeSignalDismiss(sigId, btn) {
  const row = document.getElementById(`home-sig-row-${sigId}`);
  if (row) row.classList.add('busy');
  try {
    const r = await api('POST', `/api/signals/${encodeURIComponent(sigId)}/dismiss`, { verdict: 'not_actionable' });
    if (r && r.error) throw new Error(r.error);
    toast('Dismissed', 'ok');
    await loadHome();
  } catch (e) {
    if (row) row.classList.remove('busy');
    toast(`Dismiss failed: ${String(e && e.message || e)}`, 'err');
  }
}

// ── Top-N pending proposals (inline) ────────────────────────────────────────
// Sort by urgency desc, then created_at desc. Cap at 3 — beyond that the
// Recommendations page is the right surface. Each row carries inline
// Take this on / Snooze 1w / Dismiss buttons; clicking the headline
// drills to Recommendations.
function _homeRenderTopProposals(pending) {
  if (!pending || pending.length === 0) return '';
  const sorted = pending.slice().sort((a, b) => {
    const ua = _HOME_URGENCY_RANK[a.urgency] || 0;
    const ub = _HOME_URGENCY_RANK[b.urgency] || 0;
    if (ub !== ua) return ub - ua;
    return String(b.created_at || '').localeCompare(String(a.created_at || ''));
  });
  const top = sorted.slice(0, 3);
  const rest = sorted.length - top.length;
  const restNote = rest > 0
    ? `<div style="font-size:0.78rem;color:var(--text3);margin-top:6px"><a href="javascript:void(0)" onclick="nav(document.querySelector('[data-page=self-improvement]'))" style="color:var(--accent)">+${rest} more in Recommendations →</a></div>`
    : '';
  const rows = top.map(p => _homeRenderProposalRow(p)).join('');
  return `<div style="margin-top:14px">
    <div style="font-size:0.74rem;text-transform:uppercase;letter-spacing:0.08em;color:var(--text3);margin-bottom:6px">Top recommendations</div>
    <ul class="home-prop-list">${rows}</ul>
    ${restNote}
  </div>`;
}

function _homeRenderProposalRow(p) {
  const id = escHtml(p.id || '');
  const label = escHtml(p.admin_surface_summary || p.problem || p.draft_headline || '(untitled proposal)');
  const bot = escHtml(botLabel(p.bot_id));
  const gen = escHtml(p.generator_id || '');
  const urgency = (p.urgency || 'improvement');
  const urgencyClass = escHtml(urgency);
  const urgencyLabel = escHtml(urgency.replace(/_/g, ' '));
  return `<li class="home-prop-row" id="home-prop-row-${id}">
    <div class="home-prop-head">
      <span class="home-prop-urgency ${urgencyClass}">${urgencyLabel}</span>
      ${label}
    </div>
    <div class="home-prop-meta">bot: <code>${bot}</code> · gen: <code>${gen}</code></div>
    <div class="home-prop-acts">
      <button class="btn btn-primary btn-sm" onclick="_homeProposalAct('${id}', this)">Take this on</button>
      <button class="btn btn-ghost btn-sm" onclick="_homeProposalSnooze('${id}', this)">Snooze 1w</button>
      <button class="btn btn-ghost btn-sm" onclick="_homeProposalDismiss('${id}', this)">Dismiss</button>
    </div>
  </li>`;
}

async function _homeProposalAct(propId, btn) {
  const row = document.getElementById(`home-prop-row-${propId}`);
  if (row) row.classList.add('busy');
  try {
    const r = await api('POST', `/api/arbiter/proposals/${encodeURIComponent(propId)}/act`);
    if (r && r.error) throw new Error(r.error);
    toast(r && r.applied ? 'Applied' : (r && r.message) || 'Submitted', 'ok');
    await loadHome();
  } catch (e) {
    if (row) row.classList.remove('busy');
    toast(`Action failed: ${String(e && e.message || e)}`, 'err');
  }
}

async function _homeProposalSnooze(propId, btn) {
  const row = document.getElementById(`home-prop-row-${propId}`);
  if (row) row.classList.add('busy');
  try {
    const r = await api('POST', `/api/arbiter/proposals/${encodeURIComponent(propId)}/snooze`, { duration: '1w' });
    if (r && r.error) throw new Error(r.error);
    toast('Snoozed for 1w', 'ok');
    await loadHome();
  } catch (e) {
    if (row) row.classList.remove('busy');
    toast(`Snooze failed: ${String(e && e.message || e)}`, 'err');
  }
}

async function _homeProposalDismiss(propId, btn) {
  const row = document.getElementById(`home-prop-row-${propId}`);
  if (row) row.classList.add('busy');
  try {
    const r = await api('POST', `/api/arbiter/proposals/${encodeURIComponent(propId)}/dismiss`);
    if (r && r.error) throw new Error(r.error);
    toast('Dismissed', 'ok');
    await loadHome();
  } catch (e) {
    if (row) row.classList.remove('busy');
    toast(`Dismiss failed: ${String(e && e.message || e)}`, 'err');
  }
}

function _homeToggleSmall() {
  const el = document.getElementById('home-evo-small-body');
  const lbl = document.getElementById('home-evo-small-toggle-label');
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  // Phase 9 — was 'show ▾' / 'hide ▴' (tiny Unicode triangles). Now uses
  // .expand-icon SVG; .is-open rotates it 90° between show/hide states.
  // style-guide §9.13.
  if (lbl) {
    const verb = open ? 'show' : 'hide';
    lbl.innerHTML = `${verb} <span class="expand-icon${open ? '' : ' is-open'}" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>`;
    lbl.style.display = 'inline-flex';
    lbl.style.alignItems = 'center';
    lbl.style.gap = '4px';
  }
}

// ── Tier-c auto-act loop ─────────────────────────────────────────────────────
// Walks the current page's firing signals + pending proposals, fires
// each one whose server-computed tier_floor is at or below the
// operator's chosen tier, and emits an aggregate report toast. Spec:
// docs/spec-severity-framework-2026-05-18.md §1 (authority axis) + §8.
//
// Safety rails layered on the server-side classifier:
//   * Re-entry guard — refuses to fire if a previous loop is still in flight.
//   * Per-load token — only fires once per loadHome() call, so the 60s
//     background refresh doesn't re-fire the same item if it's still
//     listed (the action handlers themselves dedupe via signal/proposal
//     IDs, but this is a belt-and-suspenders guard).
//   * Tier 'ask' short-circuits before any classification work.

let _homeAutoActInFlight = false;
const _homeAutoActFiredIds = new Set();  // signal/proposal IDs fired this session

function _homeTierAllows(currentTier, tierFloor) {
  // ask     → never auto-fires (the floor itself is 'ask')
  // auto-small → fires auto-small only
  // auto    → fires auto-small + auto
  if (tierFloor === 'ask') return false;
  if (currentTier === 'ask') return false;
  if (currentTier === 'auto-small') return tierFloor === 'auto-small';
  if (currentTier === 'auto') return tierFloor === 'auto-small' || tierFloor === 'auto';
  return false;
}

async function _homeAutoActIfTierAllows({ firing = [], proposals = [] } = {}) {
  const tier = _homeReadTier();
  if (tier === 'ask') return;
  if (_homeAutoActInFlight) return;

  // Build the eligible lists. Skipping anything we already auto-fired
  // in this session keeps the 60s background refresh idempotent.
  const eligibleSignals = [];
  const eligibleSnoozes = [];     // separate path: low-priority noise
                                  // we quiet without dispatching a fix
  for (const sig of firing) {
    const e = sig && sig.auto_eligibility;
    if (!e) continue;
    if (!sig.id || _homeAutoActFiredIds.has(sig.id)) continue;
    // Path A — remediation-eligible: run the fix.
    if (e.tier_floor && _homeTierAllows(tier, e.tier_floor)
        && sig.remediation && sig.remediation.kind) {
      eligibleSignals.push(sig);
      continue;
    }
    // Path B — auto-snooze noise. Independent of tier_floor; only
    // fires under auto-small or auto. Skips signals already handled
    // by Path A so we don't snooze something we just remediated.
    if (e.auto_snooze && (tier === 'auto-small' || tier === 'auto')) {
      eligibleSnoozes.push(sig);
    }
  }

  const eligibleProposals = [];
  for (const p of proposals) {
    if (p.status !== 'pending') continue;
    const e = p && p.auto_eligibility;
    if (!e || !e.tier_floor) continue;
    if (!_homeTierAllows(tier, e.tier_floor)) continue;
    if (!p.id || _homeAutoActFiredIds.has(p.id)) continue;
    eligibleProposals.push(p);
  }

  const total = eligibleSignals.length + eligibleProposals.length
              + eligibleSnoozes.length;
  if (total === 0) return;

  _homeAutoActInFlight = true;
  let acted = 0;
  let failed = 0;
  let snoozed = 0;
  const actedLabels = [];

  try {
    for (const sig of eligibleSignals) {
      _homeAutoActFiredIds.add(sig.id);
      try {
        const r = await fetch('/api/admin/remediation/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            kind: sig.remediation.kind,
            params: sig.remediation.params || {},
            signal_id: sig.id,
            actor: 'home_auto_act',
          }),
        });
        if (r.status !== 202) throw new Error(`HTTP ${r.status}`);
        acted++;
        actedLabels.push(sig.remediation.kind);
      } catch (e) {
        failed++;
      }
    }
    for (const p of eligibleProposals) {
      _homeAutoActFiredIds.add(p.id);
      try {
        const r = await api('POST', `/api/arbiter/proposals/${encodeURIComponent(p.id)}/act`);
        if (r && r.error) throw new Error(r.error);
        acted++;
        actedLabels.push(p._action_kind || 'proposal');
      } catch (e) {
        failed++;
      }
    }
    // Path B — auto-snooze. POST snooze with the eligibility module's
    // canonical 7d duration (mirrored at AUTO_SNOOZE_DURATION). Failures
    // here are non-blocking (a missed snooze just shows up next visit
    // and the operator can dismiss manually).
    for (const sig of eligibleSnoozes) {
      _homeAutoActFiredIds.add(sig.id);
      try {
        const r = await api(
          'POST',
          `/api/signals/${encodeURIComponent(sig.id)}/snooze`,
          { duration: '7d', reason: 'home auto-snooze (low-priority noise)' },
        );
        if (r && r.error) throw new Error(r.error);
        snoozed++;
      } catch (e) {
        failed++;
      }
    }
  } finally {
    _homeAutoActInFlight = false;
  }

  if (acted > 0 || snoozed > 0 || failed > 0) {
    const parts = [];
    if (acted > 0) {
      parts.push(`Auto-acted on ${acted}: ${actedLabels.slice(0, 3).join(', ')}${actedLabels.length > 3 ? '…' : ''}`);
    }
    if (snoozed > 0) {
      parts.push(`auto-snoozed ${snoozed} noisy signal${snoozed === 1 ? '' : 's'}`);
    }
    if (failed > 0) {
      parts.push(`${failed} failed`);
    }
    const msg = parts.join(' · ');
    toast(msg, failed > 0 ? 'err' : 'ok');
    // Refresh after a moment so the operator sees the toast first and
    // then the resolved/applied items disappear from the narrative.
    setTimeout(() => loadHome(), 1800);
  }
}

async function loadHomeMiniHealth() {
  let d;
  try {
    const r = await fetch('/api/host-health', { cache: 'no-store' });
    if (!r.ok) return;
    d = await r.json();
  } catch (_) { return; }
  return _renderHomeMiniHealthFromData(d);
}

function _renderHomeMiniHealthFromData(d) {
  if (!d || d.ok === false || d.available === false) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  const setBar = (id, pct, status) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.width = Math.max(0, Math.min(100, pct)) + '%';
    el.className = 'home-hh-bar-fill ' + (status || 'ok');
  };
  // Paints the compact-row status dot (visible when the host tile is
  // collapsed) AND the corresponding tooltip with the live value.
  const setDot = (id, status, pct, label) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'home-host-dot ' + (status || 'ok');
    if (pct != null && el.parentElement) {
      el.parentElement.title = `${label}: ${pct.toFixed(0)}% (${status || 'ok'})`;
    }
  };
  set('home-hh-name', d.hostname || '');
  set('home-hh-cpu', `${d.cpu_percent.toFixed(0)}%`);
  setBar('home-hh-cpu-bar', d.cpu_percent, d.cpu_status);
  setDot('home-hh-cpu-dot', d.cpu_status, d.cpu_percent, 'CPU');
  const mem = d.memory || {};
  set('home-hh-mem', mem.percent != null ? `${mem.percent.toFixed(0)}%` : '—');
  setBar('home-hh-mem-bar', mem.percent || 0, mem.status);
  setDot('home-hh-mem-dot', mem.status, mem.percent, 'Memory');
  const disk = d.disk || {};
  set('home-hh-disk', disk.percent != null ? `${disk.percent.toFixed(0)}%` : '—');
  setBar('home-hh-disk-bar', disk.percent || 0, disk.status);
  setDot('home-hh-disk-dot', disk.status, disk.percent, 'Disk');
  const load = d.load_avg || {};
  set('home-hh-load', load.load1 != null ? load.load1.toFixed(2) : '—');
  if (typeof _fmtUptime === 'function') {
    set('home-hh-uptime', _fmtUptime(d.uptime_seconds));
  } else {
    set('home-hh-uptime', d.uptime_seconds != null ? `${Math.round(d.uptime_seconds/3600)}h` : '—');
  }
}

// ── HOST tile collapse toggle ──────────────────────────────────────────────
// Collapsed (default): single-row header with three colored status dots
// (CPU/MEM/DISK). Expanded: full bar breakdown + Load + Uptime. State
// persists per-pod in localStorage so the choice survives reloads and
// the 60s background refresh.

const HOME_HOST_KEY = 'evolve_home_host_expanded';

function _homeHostIsExpanded() {
  try { return localStorage.getItem(HOME_HOST_KEY) === '1'; }
  catch (_) { return false; }
}

function _homeHostToggle() {
  const el = document.getElementById('home-host-tile');
  if (!el) return;
  const expanded = !el.classList.contains('collapsed');
  el.classList.toggle('collapsed', expanded);  // flip
  try { localStorage.setItem(HOME_HOST_KEY, expanded ? '0' : '1'); } catch (_) {}
}

function _homeHostRestoreState() {
  const el = document.getElementById('home-host-tile');
  if (!el) return;
  el.classList.toggle('collapsed', !_homeHostIsExpanded());
}

// Apply restored state once the DOM is alive so the host tile paints
// in the operator's preferred state on first render.
document.addEventListener('DOMContentLoaded', _homeHostRestoreState);

// ── Evo's report banner collapse ───────────────────────────────────────────
// The report banner can occupy a meaningful slice of the viewport (up to
// 38vh on desktop). On phone, with the keyboard up, that leaves almost no
// room for the chat. Operators get a tap target (the head) to fold the
// body away; state persists in localStorage. Default behavior when no
// explicit choice has been stored: collapsed on phone-size viewports
// (≤480px), expanded on tablet+desktop.

const HOME_REPORT_KEY = 'evolve_home_report_collapsed';

function _homeReportIsCollapsed() {
  try {
    const v = localStorage.getItem(HOME_REPORT_KEY);
    if (v === '1') return true;
    if (v === '0') return false;
  } catch (_) {}
  return window.innerWidth <= 480;
}

function _homeReportToggle() {
  const el = document.getElementById('home-report');
  if (!el) return;
  const next = !el.classList.contains('collapsed');
  el.classList.toggle('collapsed', next);
  try { localStorage.setItem(HOME_REPORT_KEY, next ? '1' : '0'); } catch (_) {}
  // Layout just shifted — keep the latest chat message in view.
  const thread = document.getElementById('home-thread');
  if (thread) thread.scrollTop = thread.scrollHeight;
}

function _homeReportRestoreState() {
  const el = document.getElementById('home-report');
  if (!el) return;
  el.classList.toggle('collapsed', _homeReportIsCollapsed());
}

document.addEventListener('DOMContentLoaded', _homeReportRestoreState);

// ── Mobile rail slide-up sheet ─────────────────────────────────────────────
// On phone widths the bot/host rail is pulled out of the page flow and
// becomes a bottom sheet. The toggle in the home-report head opens it;
// tapping the backdrop closes it. Esc also closes.
function _mobileRailToggle() {
  const rail = document.querySelector('#page-home .home-rail');
  const backdrop = document.getElementById('mobile-rail-backdrop');
  if (!rail || !backdrop) return;
  const open = rail.classList.toggle('mobile-open');
  backdrop.classList.toggle('open', open);
}
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const rail = document.querySelector('#page-home .home-rail.mobile-open');
  if (rail) _mobileRailToggle();
});
