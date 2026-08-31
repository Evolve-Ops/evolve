// ════════════════════════════════════════════════════════════════════════
// Page: Self-Improvement (Arbiter Proposals + Generators + Profile + Observations)
//
// The RSI surface — Phase 1 of UI rationalization unified four
// originally-separate panels:
//   - Proposals subtab    — pending/snoozed/archived proposal lanes
//                           with severity grouping + bulk action bar
//   - Generators subtab   — per-generator track-record cards + the
//                           rate-limit configuration drawer
//   - Profile subtab      — per-bot profile editor + lessons strip
//                           (renders the YAML frontmatter weights and
//                           the markdown body together)
//   - Observations subtab — per-bot observation tuple browser
//                           (noun × verb × mood × engagement)
//   - History tab         — proposal history with detail viewer
//
// State (top of the file):
//   URGENCY_COLORS                  — severity → CSS color map
//   _propGroupSimilar               — toggle for "group similar proposals"
//   _arbProposalsCache              — last fetched proposal list
//   _arbFilterBot                   — bot filter currently applied
//   _arbiterGeneratorsCache         — last fetched generator list
//   _obsBot, _obsBucket, _obsDays   — observation browser filter state
//   _obsDebounce                    — debounce handle for filter typing
//   _profileBot, _profileEditOpen   — profile editor state
//
// Loaders dispatched via onPageActivate('self-improvement') +
// onSubTabActivate('self-improvement', ...):
//   loadArbiterProposals()         — Proposals subtab + grouping
//   loadArbiterGenerators()        — Generators subtab + track-records
//   loadArbiterRateLimit()         — rate-limit drawer
//   initProposalHistory()          — History tab init + filters
//   loadProposalHistory()          — History tab fetch
//   initObsBotSelector() +
//     loadObservations()           — Observations subtab
//
// Cross-file linkages (resolved at call time, all free-variable lookups):
//   - api(), toast(), escHtml(), botLabel() — core/ helpers
//   - openProposalDetail / renderProposalCard called from Home page +
//     elsewhere via global lookup
//
// Out of scope (separate clusters, future phases):
//   loadAiOptimization (~line 34607) — AI-Optimization subtab; lives
//     under the Improve bucket but in a separate cluster. Phase 3j+.
//   Recovery (~line 45547) — sibling of Backup, not Self-Improvement.
// ════════════════════════════════════════════════════════════════════════

// Arbiter Proposals + Profile (Phase 1 of UI rationalization)
// ══════════════════════════════════════════════════════

const URGENCY_COLORS = {
  security_critical: 'var(--red)',
  operational_urgent: '#ff8c42',
  cost_alert: '#ffb347',
  substrate_warn: 'var(--yellow)',
  improvement: 'var(--blue)',
  hygiene: 'var(--text2)',
  whimsy: 'var(--purple)',
};

const VERIFY_LABELS = {
  unknown: { text: '—', title: 'Not yet applied' },
  pending: { text: '⏳', title: 'Applied; verification in progress' },
  confirmed: { text: '✓', title: 'Claim confirmed at its check-in (typically ~1 week)' },
  refuted: { text: '✗', title: 'Claim refuted; reverted or flagged' },
};

// Adjacency-type vocabulary → human distance word + tooltip
const ADJACENCY_LABELS = {
  extend_same_cell: { word: 'near', tip: 'Extends what this bot already does' },
  adjacent_noun: { word: 'medium', tip: 'A neighboring noun — close to today\'s scope' },
  adjacent_verb: { word: 'medium', tip: 'A neighboring verb — close to today\'s scope' },
  chain_completion: { word: 'far', tip: 'Completes a workflow chain — further from today\'s scope' },
};

// Action kinds whose proposals require explicit operator completion (vs.
// running an applier and immediately succeeding). These land in the In
// Process queue after Accept and stay there until the operator clicks
// Mark complete. Mirrors arbiter.apply._MANUAL_COMPLETION_KINDS.
const _MANUAL_COMPLETION_KINDS = ['Investigation', 'WorkflowInstruction', 'AddSignalCollection'];

// Action kinds whose proposals close out via an external sweep (e.g.
// forge_sweep watching a forge job) rather than operator action.
// Mirrors arbiter.apply._EXTERNAL_COMPLETION_KINDS.
const _EXTERNAL_COMPLETION_KINDS = ['BuildApp'];

// Both manual + external completion kinds belong in the In Process
// queue: the proposal has been accepted but final close-out is pending
// (operator click for manual, sweep for external). Mirrors
// arbiter.apply.is_deferred_completion_kind.
const _IN_PROCESS_KINDS = [..._MANUAL_COMPLETION_KINDS, ..._EXTERNAL_COMPLETION_KINDS];

async function loadArbiterProposals() {
  const listEl = document.getElementById('arbiter-proposals-list');
  if (!listEl) return;
  // Visible loading state so refresh feels responsive even when the
  // returned proposal list is unchanged.
  listEl.innerHTML = `<div class="subtle" style="padding:18px;text-align:center">Loading…</div>`;
  const params = new URLSearchParams();
  const botSel = document.getElementById('arbiter-filter-bot');
  const bot = botSel ? botSel.value : '';
  const dim = document.getElementById('arbiter-filter-dimension').value;
  const urg = document.getElementById('arbiter-filter-urgency').value;
  const aud = document.getElementById('arbiter-filter-audience').value;
  const gen = document.getElementById('arbiter-filter-generator').value;
  const includeSnoozed = document.getElementById('arbiter-include-snoozed').checked;
  if (bot) params.set('bot_id', bot);
  if (dim) params.set('dimension', dim);
  if (urg) params.set('urgency', urg);
  if (aud) params.set('audience', aud);
  if (gen) params.set('generator_id', gen);
  // Operator-UI proposals belong in the activity log, not the review
  // queue. The defensive filter keeps the Self-Improvement page focused
  // on LLM-generated work even if a future code path leaks an
  // operator-origin proposal into pending/.
  if (!gen) params.set('exclude_generator_id', 'operator_ui');
  // Always include applied so the In Process queue surfaces. Snoozed only
  // when the operator opts in.
  const subdirs = ['pending', 'applied'];
  if (includeSnoozed) subdirs.push('snoozed');
  params.set('include', subdirs.join(','));
  try {
    const r = await fetch('/api/arbiter/proposals?' + params.toString());
    const j = await r.json();
    if (!j.ok) {
      listEl.innerHTML = `<div class="card" style="padding:14px">Error: ${escHtml(j.error || 'unknown')}</div>`;
      return;
    }
    const all = j.proposals || [];
    // Split: In Process = applied-status manual-completion kinds. Inbox =
    // pending/snoozed. Auto-applied proposals awaiting verify (status
    // applied + has claim) don't need operator attention; they show up
    // in the History tab once verify resolves them.
    const inProcess = all.filter(p =>
      p.status === 'applied' && _IN_PROCESS_KINDS.includes(p._action_kind)
    );
    // Surface-based routing (Slice 1B catchall flip 2026-06-04):
    // Recommendations shows ONLY proposals whose charter surface is
    // 'improvement' — high-level, app-side, material-impact
    // proposals. Everything else (firing/drift/cleanup AND null/
    // unclassified) routes to Alerts. This includes audit_poller's
    // app_audit_tier3 findings, which emit without a charter and
    // therefore carry surface=null — they are broken-install
    // forge-emitted findings, not improvements, and belong with
    // sysadmin hygiene on Alerts.
    // CANONICAL PREDICATE MIRROR: this `_isRecommendable` + the `_inPendingInbox`
    // filter below are the JS rendering mirror of the Python source of
    // truth in packages/analyzer/proposal_routing.py
    // (is_recommendable / proposal_surfaces_in_pending_inbox). The notification
    // emitters (analyze.send_telegram_alert, review._alert_reviewed) gate the
    // "proposal ready → Pending" push on that same predicate so the push never
    // points at an inbox a proposal can't appear in. Keep the two in sync.
    const _isRecommendable = p => p.surface === 'improvement';
    const inboxAll = all.filter(p => p.status === 'pending' || p.status === 'snoozed');
    const recommendable = inboxAll.filter(_isRecommendable);
    // Effectiveness-Layer triage (§11): pull observation/FYI proposals ("look
    // into it" — Investigation, VetoAnnotation, WorkflowInstruction, …) out of
    // the actionable inbox into a calmer Observations section, so a pile of them
    // doesn't bury the proposals that actually ask for a decision.
    // ALTITUDE CARVE-OUT (mirror of proposal_surfaces_in_pending_inbox in
    // proposal_routing.py): a high-altitude capability idea (altitude >= 2)
    // stays in the Inbox even when informational, so a capability generator's
    // Investigation-kind idea reaches the actionable inbox. altitude defaults
    // to 0, so generic Investigations still fall through to Observations.
    const _inPendingInbox = p => (p.altitude || 0) >= 2 || !p.informational;
    const inbox = recommendable.filter(_inPendingInbox);
    const observations = recommendable.filter(p => !_inPendingInbox(p));
    const routedToAlerts = inboxAll.length - recommendable.length;
    renderArbiterProposals(inbox, inProcess, { routedToAlerts, observations });
    // Nav badge + Overview tile count the surface-filtered Inbox
    // (pending only — snoozed items are tucked away on the Snoozed tab
    // and shouldn't inflate the badge). This matches what the operator
    // sees on the Recommendations page; the previous full-pending count
    // was a defensive hedge from PR #2048 that confused operators when
    // the badge showed 42 while the Inbox listed 10.
    const pending = inbox.filter(p => p.status === 'pending').length;
    const navBadge = document.getElementById('badge-proposals');
    if (navBadge) navBadge.textContent = pending;
    const ovTile = document.getElementById('ov-proposals');
    if (ovTile) ovTile.textContent = pending;
    // Snapshot for the evo chat drawer. The Recommendations page-context
    // pack reads this so evo answers "help me mark this proposal complete"
    // with WHATEVER the operator is currently looking at — including the
    // In Process tab (previously invisible to evo because the only writer
    // was loadBetterStrip on the Home page, which queries a different
    // endpoint that excludes applied-status items). Covers the
    // 2026-05-20 transcript where the operator was on In Process and evo
    // couldn't find the proposal it was being asked about.
    window._evoContextSnapshots = window._evoContextSnapshots || {};
    const _projectProposal = (p) => ({
      id: p.id,
      title: p.human_title || p.admin_surface_summary || p.problem || '(untitled)',
      bot: p.scope_id || p.bot_id || 'pod-wide',
      score: p.score ?? null,
      status: p.status,
      action_kind: p._action_kind || null,
      urgency: p.urgency || null,
      generator: p.generator_id || null,
      sub_findings_count: Array.isArray(p.sub_findings) ? p.sub_findings.length : 0,
    });
    window._evoContextSnapshots['self-improvement'] = {
      inbox_total: inbox.length,
      inbox_top: inbox.slice(0, 8).map(_projectProposal),
      in_process_total: inProcess.length,
      in_process_top: inProcess.slice(0, 8).map(_projectProposal),
      active_subtab: _evoActiveSubtabForSelfImprovement(),
      // Back-compat: keep the loadBetterStrip-written shape (``total`` +
      // ``top``) so any pre-existing readers don't break mid-PR. New
      // readers should prefer inbox_top / in_process_top.
      total: inbox.length,
      top: inbox.slice(0, 8).map(_projectProposal),
    };
    if (typeof _evoDrawerUpdateContextChip === 'function') {
      _evoDrawerUpdateContextChip();
    }
  } catch (e) {
    listEl.innerHTML = `<div class="card" style="padding:14px">Request failed: ${escHtml(String(e))}</div>`;
  }
}

// Helper: resolve which subtab on the Recommendations page is currently
// active. Used by the page-context snapshot so evo knows what the
// operator is looking at — Inbox? In Process? History? Each has
// different action paths.
function _evoActiveSubtabForSelfImprovement() {
  try {
    const pageEl = document.getElementById('page-self-improvement');
    if (!pageEl) return null;
    const activeSub = pageEl.querySelector('.subtab-page.active');
    if (activeSub && activeSub.id && activeSub.id.startsWith('self-improvement-')) {
      return activeSub.id.slice('self-improvement-'.length);
    }
    return localStorage.getItem('evolve_subtab_self-improvement') || null;
  } catch (_) {
    return null;
  }
}

// Populate the Bot filter dropdown from network.json (authoritative pod
// membership). Sourced from the network — not from the loaded proposals —
// so bots with zero open proposals are still selectable (operator can
// confirm "Team_bot_a's queue is empty" rather than wonder why Team_bot_a doesn't appear).
async function populateBotFilter() {
  const sel = document.getElementById('arbiter-filter-bot');
  if (!sel) return;
  const previous = sel.value;
  try {
    let members = [];
    if (_networkData && (_networkData.members || _networkData.bots)) {
      members = _networkData.members || Object.keys(_networkData.bots || {});
    } else {
      const net = await api('GET', '/api/network').catch(() => null);
      if (net) members = net.members || Object.keys(net.bots || {});
    }
    members = [...new Set(members.filter(Boolean))].sort();
    sel.innerHTML =
      `<option value="">All bots</option>` +
      members.map(b => `<option value="${escHtml(b)}">${escHtml(botLabel(b))}</option>`).join('');
    if (previous && members.includes(previous)) sel.value = previous;
  } catch (e) {
    // Leave the dropdown with just the default "All bots" option.
  }
}

// Click-handler for the `bot:` link on each proposal card. Filters the
// queue to that bot in place (no nav, no scroll-reset) and falls back to
// jumpToProposals if we're somehow not on Self-Improvement.
function _arbiterFilterByBot(botId) {
  const botSel = document.getElementById('arbiter-filter-bot');
  if (!botSel) {
    jumpToProposals('', botId);
    return;
  }
  // If the option isn't there yet (network not loaded), insert it so the
  // selection sticks; populateBotFilter will overwrite the list on next run.
  if (!Array.from(botSel.options).some(o => o.value === botId)) {
    const opt = document.createElement('option');
    opt.value = botId;
    opt.textContent = botLabel(botId);
    botSel.appendChild(opt);
  }
  botSel.value = botId;
  if (typeof loadArbiterProposals === 'function') loadArbiterProposals();
}

// Populate the Generator filter dropdown from the live registry. Runs
// on each Self-Improvement page activation so a newly-deployed generator
// shows up without a hard reload.
async function populateGeneratorFilter() {
  const sel = document.getElementById('arbiter-filter-generator');
  if (!sel) return;
  const previous = sel.value;
  try {
    const r = await fetch('/api/arbiter/generators');
    const j = await r.json();
    if (!j.ok) return;
    const ids = (j.generators || []).map(g => g.id).filter(Boolean).sort();
    sel.innerHTML =
      `<option value="">All</option>` +
      ids.map(id => `<option value="${escHtml(id)}">${escHtml(id)}</option>`).join('');
    // Preserve selection if the prior value still exists.
    if (previous && ids.includes(previous)) sel.value = previous;
  } catch (e) {
    // Leave the dropdown with just the default "All" option.
  }
}

async function loadActiveGenerators() {
  const el = document.getElementById('active-generators-tags');
  const headerEl = document.getElementById('active-generators-header');
  if (!el) return;
  try {
    const r = await fetch('/api/arbiter/generators');
    const j = await r.json();
    if (!j.ok) {
      el.innerHTML = `<span class="subtle" style="font-size:0.7rem">Error: ${escHtml(j.error || 'unknown')}</span>`;
      return;
    }
    const generators = j.generators || [];
    const active = generators.filter(g => g.status === 'active');
    if (headerEl) {
      headerEl.textContent = `Active coaches (${active.length})`;
    }
    if (!active.length) {
      el.innerHTML = `<span class="subtle" style="font-size:0.7rem">No active coaches.</span>`;
      return;
    }
    el.innerHTML = active.map(g =>
      `<span class="badge badge-member" title="${escHtml(g.dimension || '')} · ${escHtml(g.purpose || '')}">${escHtml(g.id)}</span>`
    ).join('');
  } catch (e) {
    el.innerHTML = `<span class="subtle" style="font-size:0.7rem">Request failed: ${escHtml(String(e))}</span>`;
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Coalesce proposals sharing a root cause across bots.
//
// Today's queue fans out the same finding per-bot: 7 "primary floor" rows,
// 8 "Add network_egress: X on app Y" rows, 5 "App audit on ea-pack" rows,
// etc. From an operator's standpoint these are *one* root cause that
// happens to surface N times. Grouping by normalized (generator, urgency,
// title-with-bot-stripped) collapses the visible queue without changing
// storage. Same pattern as the Alerts lane's _alGroupSignals (lines
// ~48019-48076) — we mirror that shape so future operators don't have
// to learn two systems.
//
// Storage stays per-Proposal. Bulk actions on a coalesced group fan out
// client-side to the existing per-proposal /api/arbiter/proposals/{id}/{snooze|dismiss}
// endpoints — no new server-side bulk endpoint required.
// ───────────────────────────────────────────────────────────────────────────

let _propGroupSimilar = true;  // default-on; toggle is in the filter row

function _propToggleGroupSimilar(checkbox) {
  _propGroupSimilar = !!checkbox.checked;
  loadArbiterProposals();
}

function _propNormalizeTitle(p) {
  // Title source matches what renderProposalCard renders (line 52612).
  let t = String(p.admin_surface_summary || p.problem || '');
  // Strip bot identifier: bot_id (e.g. "team-bot-a") or scope_id (e.g.
  // "team-bot-a-mini") wherever it appears as a whole token. The literal
  // split-join handles multiple occurrences within the title.
  const tok = p.bot_id || p.scope_id || '';
  if (tok) t = t.split(tok).join('{bot}');
  // Quoted contents (cron names, plugin names, hosts, model IDs) — collapse
  // so "Add network_egress: docs.openclaw.ai" and "Add network_egress:
  // github.com" group as one shape. Both single-quote and backtick variants.
  t = t.replace(/'[^']*'/g, "'X'");
  t = t.replace(/`[^`]*`/g, '`X`');
  return t;
}

function _propGroupKey(p) {
  return [
    p.generator_id || '',
    p.urgency || '',
    _propNormalizeTitle(p),
  ].join('|');
}

function _propGroupProposals(props) {
  // Returns array of group objects: {key, members, template_title,
  // urgency, generator_id, dimension, distinct_bot_count, top_score,
  // latest_created_at}. Order mirrors the input — caller sorts.
  const byKey = new Map();
  const order = [];
  for (const p of props) {
    const key = _propGroupKey(p);
    let g = byKey.get(key);
    if (!g) {
      g = {
        key,
        members: [],
        template_title: _propNormalizeTitle(p),
        urgency: p.urgency || 'improvement',
        generator_id: p.generator_id || '',
        dimension: p.dimension || '',
        action_kind: p._action_kind || '',
        approval_audience: p.approval_audience || 'none',
        distinct_bots: new Set(),
        top_score: p.score ?? -Infinity,
        top_altitude: _propAltitude(p),
        latest_created_at: p.created_at || '',
      };
      byKey.set(key, g);
      order.push(g);
    }
    g.members.push(p);
    if (p.bot_id) g.distinct_bots.add(p.bot_id);
    else if (p.scope_id) g.distinct_bots.add(p.scope_id);
    if ((p.score ?? -Infinity) > g.top_score) g.top_score = p.score ?? -Infinity;
    g.top_altitude = Math.max(g.top_altitude || 0, _propAltitude(p));
    if ((p.created_at || '') > g.latest_created_at) g.latest_created_at = p.created_at;
  }
  for (const g of order) g.distinct_bot_count = g.distinct_bots.size;
  return order;
}

function _propGroupRow(group) {
  // Singletons defer to the per-proposal renderer so visual behavior is
  // identical to ungrouped mode for non-clustered findings.
  if (group.members.length === 1) {
    return renderProposalCard(group.members[0]);
  }

  const count = group.members.length;
  const bots = group.distinct_bot_count;
  const countLabel = bots === count
    ? `${count} bot${count === 1 ? '' : 's'}`
    : (bots > 1
        ? `${count} findings across ${bots} bot${bots === 1 ? '' : 's'}`
        : `${count} findings`);

  const urgency = group.urgency;
  const urgColor = URGENCY_COLORS[urgency] || 'var(--text2)';
  const dim = group.dimension;
  const displayTitle = group.template_title || '(no title)';

  const groupId = 'pg-' + group.key.replace(/[^a-zA-Z0-9_-]+/g, '_').slice(0, 80);
  // Stash for the bulk-action handler so it doesn't have to walk the DOM.
  if (!window._propGroupMembers) window._propGroupMembers = {};
  window._propGroupMembers[groupId] = group.members.map(m => m.id);

  const stopProp = "event.preventDefault();event.stopPropagation();";

  const groupActions = `
    <span style="display:inline-flex;gap:6px;align-items:center" onclick="${stopProp}">
      <button class="btn btn-ghost btn-sm"
              title="Snooze every proposal in this group for 1 week"
              onclick="${stopProp}_propGroupBulk('${groupId}', 'snooze')">Snooze all 1w</button>
      <button class="btn btn-ghost btn-sm"
              title="Dismiss every proposal in this group"
              onclick="${stopProp}_propGroupBulk('${groupId}', 'dismiss')">Dismiss all</button>
    </span>`;

  // Cluster-header checkbox. Toggles every member's per-row checkbox to
  // match, so the operator can build a multi-cluster selection and act
  // through the page-level bulk bar. Click is stopPropagation'd so it
  // doesn't also toggle the <details>.
  const groupCheckbox = `<input type="checkbox" class="prop-group-select"
            data-group-id="${escHtml(groupId)}"
            onclick="event.stopPropagation()"
            onchange="_propGroupToggleAll('${groupId}', this.checked)"
            style="margin:0;cursor:pointer;flex:0 0 auto"
            title="Select every proposal in this group">`;

  const memberHtml = group.members
    .map(p => renderProposalCard(p, { insideGroup: true }))
    .join('');

  const summary = `
    <summary style="padding:10px 12px;cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--bg2);border-left:3px solid ${urgColor}">
      <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      ${groupCheckbox}
      <strong style="flex:1 1 auto;min-width:200px">${escHtml(displayTitle)}</strong>
      <span class="badge" style="background:${urgColor};color:var(--on-accent);font-size:0.7rem;padding:1px 6px">${escHtml(urgency)}</span>
      ${dim ? `<span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);font-size:0.7rem;padding:1px 6px">${escHtml(dim)}</span>` : ''}
      <span class="badge" style="background:var(--accent);color:white" title="${count} member proposal${count === 1 ? '' : 's'} grouped by normalized title">${escHtml(countLabel)}</span>
      <span class="badge badge-muted" title="generator that emitted these">${escHtml(group.generator_id)}</span>
      ${groupActions}
      <span style="color:var(--text3);font-size:0.78rem;min-width:70px;text-align:right">${ago(group.latest_created_at)}</span>
    </summary>`;

  const body = `
    <div style="padding:4px 0 4px 14px;background:var(--bg);border-left:3px solid ${urgColor}">
      ${memberHtml}
    </div>`;

  return `<details class="prop-group" data-group-id="${escHtml(groupId)}" style="margin-bottom:10px;border:1px solid var(--border);border-radius:4px">${summary}${body}</details>`;
}

async function _propGroupBulk(groupId, action) {
  const ids = (window._propGroupMembers || {})[groupId] || [];
  if (!ids.length) {
    if (typeof toast === 'function') toast('Group has no member ids — try reloading the page', 'error');
    return;
  }
  if (action === 'dismiss' && !await confirmModal({body: `Dismiss all ${ids.length} proposals in this group?`, danger: true})) return;
  const payload = { proposal_ids: ids, action };
  if (action === 'snooze') payload.duration = '1w';
  try {
    const resp = await _propBulkPost(payload);
    const applied = (resp && resp.applied) || 0;
    if (typeof toast === 'function') {
      toast(`${action}: ${applied}/${ids.length} succeeded`,
            applied === ids.length ? 'success' : 'warning');
    }
  } catch (e) {
    if (typeof toast === 'function') toast(`Bulk ${action} failed: ${e}`, 'error');
  }
  loadArbiterProposals();
  loadArbiterRateLimit();
}

// ───────────────────────────────────────────────────────────────────────────
// Page-level multi-select + bulk actions (mirror of alerts-extended.js)
//
// 138 pending proposals on the pod 2026-06-09 made per-row click-through
// unworkable. The checkbox on each pending proposal card (rendered in
// renderProposalCard) feeds the page-level bulk-action bar at the top of
// the Inbox subtab. Pattern matches Reports → Alerts so muscle memory
// carries over.
// ───────────────────────────────────────────────────────────────────────────

// Containers that host proposal rows. Recommendations → Inbox and
// Reports → Proposals both render via renderProposalCard / _propGroupRow,
// so the selection model and bulk bar work uniformly across both. The
// matching bulk-bar container ids stay in sync via the same suffix.
const _PROP_LIST_CONTAINERS = [
  '#arbiter-proposals-list',  // Recommendations → Inbox
  '#reports-proposals-body',  // Reports → Proposals
];

function _propAllRowSelectors() {
  return _PROP_LIST_CONTAINERS
    .map(c => `${c} .prop-row-select`).join(', ');
}
function _propAllRowSelectorsChecked() {
  return _PROP_LIST_CONTAINERS
    .map(c => `${c} .prop-row-select:checked`).join(', ');
}

function _propSelectedIds() {
  return Array.from(
    document.querySelectorAll(_propAllRowSelectorsChecked())
  )
    .map(cb => cb.dataset.propId)
    .filter(Boolean);
}

function _propSelectionChanged() {
  // Update every visible bulk bar — Recommendations → Inbox and
  // Reports → Proposals each carry one. We update both regardless of
  // which surface the operator is on so a tab switch doesn't drop state.
  const bars = document.querySelectorAll('.prop-bulk-bar');
  if (!bars.length) return;
  const boxes = Array.from(
    document.querySelectorAll(_propAllRowSelectorsChecked())
  );
  const ids = boxes.map(cb => cb.dataset.propId).filter(Boolean);
  const hasSelection = ids.length > 0;
  bars.forEach(bar => {
    const countEl = bar.querySelector('.prop-bulk-count');
    if (countEl) {
      countEl.textContent = `${ids.length} selected`;
      countEl.style.display = hasSelection ? '' : 'none';
    }
    bar.querySelectorAll('.prop-bulk-action').forEach(btn => {
      btn.disabled = !hasSelection;
    });
    // Apply all: only autonomous proposals (no dispatch_target, not a
    // manual/external completion kind) can be bulk-applied. If any
    // selected proposal needs operator confirmation, disable Apply and
    // explain why on hover. Note that Snooze/Dismiss are enabled
    // regardless — those operations work uniformly across all kinds.
    const applyBtn = bar.querySelector('.prop-bulk-apply');
    if (applyBtn) {
      if (!hasSelection) {
        applyBtn.disabled = true;
        applyBtn.title = 'Select one or more proposals to apply';
        return;
      }
      const nonAutonomous = boxes.filter(cb =>
        cb.dataset.dispatchTarget || cb.dataset.inProcess === '1'
      );
      if (nonAutonomous.length === boxes.length) {
        applyBtn.disabled = true;
        applyBtn.title = (
          `None of the ${boxes.length} selected proposals can be applied `
          + `autonomously — they need operator confirmation or follow-through. `
          + `Use the per-proposal action button instead.`
        );
      } else if (nonAutonomous.length > 0) {
        applyBtn.disabled = false;
        applyBtn.title = (
          `Apply ${boxes.length - nonAutonomous.length} autonomous proposals. `
          + `${nonAutonomous.length} selected proposal(s) require operator `
          + `confirmation and will be skipped.`
        );
      } else {
        applyBtn.disabled = false;
        applyBtn.title = `Apply all ${boxes.length} selected proposals autonomously.`;
      }
    }
  });
  // Cluster checkbox state: indeterminate (mixed) / checked (all) /
  // unchecked (none). Reflects the underlying member selection.
  _propRefreshGroupCheckboxes();
}

function _propSelectAllVisible(checked) {
  const cards = document.querySelectorAll(_propAllRowSelectors());
  cards.forEach(cb => {
    // Skip checkboxes whose parent card is explicitly hidden by chip
    // filters etc. Open/closed <details> state is fine — the inputs are
    // still in the DOM and still selectable, matching operator intent of
    // "select everything I could see / expand to".
    const card = cb.closest('.card');
    if (card && card.style.display === 'none') return;
    cb.checked = !!checked;
  });
  _propSelectionChanged();
}

function _propGroupToggleAll(groupId, checked) {
  const ids = (window._propGroupMembers || {})[groupId] || [];
  if (!ids.length) return;
  const idSet = new Set(ids);
  document.querySelectorAll(_propAllRowSelectors()).forEach(cb => {
    if (idSet.has(cb.dataset.propId)) cb.checked = !!checked;
  });
  _propSelectionChanged();
}

function _propRefreshGroupCheckboxes() {
  // For each cluster checkbox, reflect underlying-member state:
  //   all checked → checked
  //   none        → unchecked
  //   some        → indeterminate
  document.querySelectorAll('.prop-group-select').forEach(cb => {
    const groupId = cb.dataset.groupId;
    const ids = (window._propGroupMembers || {})[groupId] || [];
    if (!ids.length) {
      cb.checked = false;
      cb.indeterminate = false;
      return;
    }
    const idSet = new Set(ids);
    const total = ids.length;
    let checked = 0;
    document.querySelectorAll(_propAllRowSelectors()).forEach(member => {
      if (idSet.has(member.dataset.propId) && member.checked) checked++;
    });
    if (checked === 0) {
      cb.checked = false;
      cb.indeterminate = false;
    } else if (checked === total) {
      cb.checked = true;
      cb.indeterminate = false;
    } else {
      cb.checked = false;
      cb.indeterminate = true;
    }
  });
}

function _propToggleBulkMenu(kind, anchorBtn) {
  // The popover sits next to the triggering button (inside the same
  // .prop-bulk-menu wrapper). Resolve relative to the click target so
  // each bulk bar's menu opens locally — no id collision when two bars
  // render on the page.
  let target = null;
  if (anchorBtn && anchorBtn.parentElement) {
    target = anchorBtn.parentElement.querySelector(
      `.prop-bulk-menu-pop[data-menu-kind="${kind}"]`
    );
  }
  if (!target) {
    // Fallback for older callers that don't pass the anchor.
    target = document.querySelector(
      `.prop-bulk-menu-pop[data-menu-kind="${kind}"]`
    );
  }
  if (!target) return;
  const wasOpen = target.style.display === 'block';
  document.querySelectorAll('.prop-bulk-menu-pop').forEach(el => el.style.display = 'none');
  if (!wasOpen) target.style.display = 'block';
}

// Close bulk menus on outside click.
document.addEventListener('click', (ev) => {
  const inside = ev.target && ev.target.closest && ev.target.closest('.prop-bulk-menu');
  if (!inside) {
    document.querySelectorAll('.prop-bulk-menu-pop').forEach(el => el.style.display = 'none');
  }
}, true);

async function _propBulkPost(payload) {
  // Centralized bulk call. Returns the parsed JSON response or throws.
  return await api('POST', '/api/arbiter/proposals/bulk-action', payload);
}

async function _propBulkSnooze(duration) {
  document.querySelectorAll('.prop-bulk-menu-pop').forEach(el => el.style.display = 'none');
  const ids = _propSelectedIds();
  if (!ids.length) return;
  try {
    const resp = await _propBulkPost({
      proposal_ids: ids, action: 'snooze', duration,
    });
    _propBulkToast(`Snoozed ${resp.applied}/${resp.total} proposals for ${duration}.`,
                   resp.failed);
    loadArbiterProposals();
    loadArbiterRateLimit();
  } catch (e) {
    if (typeof toast === 'function') toast(`Bulk snooze failed: ${e}`, 'error');
    else toast(`Bulk snooze failed: ${e}`, 'err');
  }
}

// Bulk dismiss + apply go through a confirm modal — both are
// destructive enough that one-click is too easy a foot-gun.
let _propBulkPendingAction = null;
let _propBulkPendingIds = null;

function _propBulkDismissConfirm() {
  const ids = _propSelectedIds();
  if (!ids.length) return;
  _propBulkPendingAction = 'dismiss';
  _propBulkPendingIds = ids;
  const titleEl = document.getElementById('prop-bulk-confirm-title');
  const bodyEl = document.getElementById('prop-bulk-confirm-body');
  const submitEl = document.getElementById('prop-bulk-confirm-submit');
  if (titleEl) titleEl.innerHTML = `Dismiss <span id="prop-bulk-confirm-count">${ids.length}</span> proposals?`;
  if (bodyEl) bodyEl.textContent = (
    `They'll move to archived/ and the same fingerprint will be cooled-down `
    + `so the next generator cycle doesn't re-emit it.`
  );
  if (submitEl) submitEl.textContent = `Dismiss ${ids.length}`;
  document.getElementById('prop-bulk-confirm-modal').classList.add('open');
}

function _propBulkApplyConfirm() {
  const boxes = Array.from(
    document.querySelectorAll('#arbiter-proposals-list .prop-row-select:checked')
  );
  // Filter to autonomous-applicable proposals — match the server-side
  // skip rule so we only confirm what will actually run.
  const applicableIds = boxes
    .filter(cb => !cb.dataset.dispatchTarget && cb.dataset.inProcess !== '1')
    .map(cb => cb.dataset.propId)
    .filter(Boolean);
  if (!applicableIds.length) return;
  const skipped = boxes.length - applicableIds.length;
  _propBulkPendingAction = 'apply';
  _propBulkPendingIds = applicableIds;
  const titleEl = document.getElementById('prop-bulk-confirm-title');
  const bodyEl = document.getElementById('prop-bulk-confirm-body');
  const submitEl = document.getElementById('prop-bulk-confirm-submit');
  if (titleEl) titleEl.innerHTML = `Apply <span id="prop-bulk-confirm-count">${applicableIds.length}</span> proposals?`;
  if (bodyEl) {
    const skippedNote = skipped
      ? ` ${skipped} selected proposal(s) require operator confirmation and will be skipped.`
      : '';
    bodyEl.textContent = (
      `Each proposal's applier runs sequentially. Failures land in the In Process / `
      + `failed_flagged queue for retry.${skippedNote}`
    );
  }
  if (submitEl) submitEl.textContent = `Apply ${applicableIds.length}`;
  document.getElementById('prop-bulk-confirm-modal').classList.add('open');
}

function _propBulkConfirmClose() {
  _propBulkPendingAction = null;
  _propBulkPendingIds = null;
  const modal = document.getElementById('prop-bulk-confirm-modal');
  if (modal) modal.classList.remove('open');
}

async function _propBulkConfirmSubmit() {
  if (!_propBulkPendingAction || !_propBulkPendingIds) return;
  const action = _propBulkPendingAction;
  const ids = _propBulkPendingIds;
  _propBulkConfirmClose();
  try {
    const resp = await _propBulkPost({
      proposal_ids: ids, action,
    });
    const verb = action === 'dismiss' ? 'Dismissed' : 'Applied';
    _propBulkToast(`${verb} ${resp.applied}/${resp.total} proposals.`,
                   resp.failed);
    loadArbiterProposals();
    loadArbiterRateLimit();
  } catch (e) {
    if (typeof toast === 'function') toast(`Bulk ${action} failed: ${e}`, 'error');
    else toast(`Bulk ${action} failed: ${e}`, 'err');
  }
}

function _propBulkToast(message, failed) {
  // Light in-page toast for the bulk-op summary. Auto-fades. Matches
  // _alBulkToast so the two surfaces feel the same. Style uses token
  // variables to stay theme-correct.
  const existing = document.getElementById('prop-bulk-toast');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.id = 'prop-bulk-toast';
  div.style.cssText = (
    'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:90;'
    + 'background:var(--bg2);border:1px solid var(--border);border-radius:6px;'
    + 'padding:10px 16px;font-size:0.85rem;color:var(--text);'
    + 'box-shadow:var(--shadow-popover)'
  );
  const failMsg = failed ? ` — ${failed} skipped` : '';
  div.textContent = message + failMsg;
  document.body.appendChild(div);
  setTimeout(() => { try { div.remove(); } catch (_) {} }, 3500);
}

// Effectiveness-Layer triage (§11): observation/FYI proposals ("look into it")
// render in a calm, collapsed section below the actionable queue — present but
// out of the way, so they don't bury proposals that need a decision.
function _renderObservationsBlock(observations) {
  if (!observations || !observations.length) return '';
  const cards = observations.map(p => renderProposalCard(p)).join('');
  return `<details class="card" style="margin-top:14px;padding:0">
    <summary style="padding:12px 14px;cursor:pointer;color:var(--text2);font-size:0.85rem;display:flex;align-items:center;gap:8px">
      <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      Observations (${observations.length}) — things to look into; no action required
    </summary>
    <div style="padding:0 8px 8px">${cards}</div>
  </details>`;
}

// ───────────────────────────────────────────────────────────────────────────
// Altitude rail (Fit Reviewer Bite 2) — rank by altitude, fold L0.
//
// Altitude is the value/ambition tier, orthogonal to urgency: L0 hygiene /
// substrate · L1 optimization · L2 capability · L3 strategic. The server
// resolves each generator's charter default onto the view (``p.altitude``);
// default/missing → 0 (L0). The rail leads with the highest-altitude cards and
// folds ALL L0 into one collapsed "Maintenance" digest, so a config nudge
// never buries an app-capability idea.
// Spec: internal/spec-fit-reviewer-2026-06-12.md §5.
// ───────────────────────────────────────────────────────────────────────────

function _propAltitude(p) {
  const a = Number(p && p.altitude);
  if (!Number.isFinite(a) || a < 0) return 0;
  return Math.min(3, Math.floor(a));
}

// Referee score for the altitude tiebreak — mirrors the server breakdown;
// missing → -Infinity so unscored items sort last within their tier.
function _propScore(p) {
  const bd = p && p._score_breakdown;
  if (bd && typeof bd.score === 'number') return bd.score;
  if (p && typeof p.score === 'number') return p.score;
  return -Infinity;
}

// Comparator: (−altitude, −score). Higher altitude leads; score breaks ties.
// The server already returns views in this order; we re-apply it defensively
// after the client-side partition so a stale order can't bury a capability.
function _propAltitudeScoreCmp(a, b) {
  const d = _propAltitude(b) - _propAltitude(a);
  if (d) return d;
  return _propScore(b) - _propScore(a);
}

// L1+ tier labels for the card badge. L0 carries no badge — it folds into the
// Maintenance digest, which is itself the label.
const _ALTITUDE_LABELS = {
  1: { label: 'L1 · optimize', tip: 'Optimization — value-grounded tuning' },
  2: { label: 'L2 · capability', tip: 'Capability — install or build something the bot needs' },
  3: { label: 'L3 · strategic', tip: 'Strategic — purpose expansion or a new bot' },
};

function _altitudeBadge(p) {
  const info = _ALTITUDE_LABELS[_propAltitude(p)];
  if (!info) return '';
  return `<span class="badge" title="${escHtml(info.tip)}" style="background:var(--accent);color:var(--on-accent);font-size:0.7rem;padding:1px 6px">${escHtml(info.label)}</span>`;
}

// Pull multi-member conflict groups out of a proposal list. Conflicts need a
// decision regardless of altitude, so they surface at the top of the rail and
// are never folded. Returns { conflictGroups: [[p,…],…], rest: [p,…] }.
function _propExtractConflictGroups(proposals) {
  const inConflictGroup = new Set();
  const conflictGroups = [];
  const byId = new Map(proposals.map(p => [p.id, p]));
  for (const p of proposals) {
    if (inConflictGroup.has(p.id)) continue;
    if (!p.conflicts_with || !p.conflicts_with.length) continue;
    const group = [p];
    inConflictGroup.add(p.id);
    for (const c of p.conflicts_with) {
      const other = byId.get(c.with_proposal_id);
      if (other && !inConflictGroup.has(other.id)) {
        group.push(other);
        inConflictGroup.add(other.id);
      }
    }
    if (group.length > 1) conflictGroups.push(group);
    else inConflictGroup.delete(p.id);
  }
  const rest = proposals.filter(p => !inConflictGroup.has(p.id));
  return { conflictGroups, rest };
}

// Render a list of (non-conflicting) proposals honoring the group-similar
// preference. Returns an array of HTML parts. The CALLER owns resetting
// window._propGroupMembers, so the leading list and the Maintenance digest
// both accumulate into it instead of clobbering each other.
function _renderInboxBody(proposals) {
  if (!proposals || !proposals.length) return [];
  const parts = [];
  if (_propGroupSimilar) {
    const groups = _propGroupProposals(proposals);
    // Lead the groups by altitude, then keep the existing multi-member-first
    // + top-score ordering as the within-tier tiebreak.
    groups.sort((a, b) => {
      const da = (b.top_altitude || 0) - (a.top_altitude || 0);
      if (da) return da;
      const am = a.members.length, bm = b.members.length;
      if ((am > 1) !== (bm > 1)) return bm > 1 ? 1 : -1;
      return (b.top_score ?? 0) - (a.top_score ?? 0);
    });
    for (const g of groups) parts.push(_propGroupRow(g));
  } else {
    for (const p of proposals) parts.push(renderProposalCard(p));
  }
  return parts;
}

// Fold ALL L0 (hygiene/substrate) proposals into ONE collapsed card. Mirrors
// the Observations block's <details> chrome (native disclosure marker — no
// Unicode glyph, per style-guide §9.13). Empty list → no card.
function _renderMaintenanceDigest(maintenance) {
  if (!maintenance || !maintenance.length) return '';
  const count = maintenance.length;
  const dims = [...new Set(maintenance.map(p => p.dimension).filter(Boolean))];
  const dimNote = dims.length
    ? ` — ${escHtml(dims.slice(0, 4).join(', '))}${dims.length > 4 ? ', …' : ''}`
    : '';
  const body = _renderInboxBody(maintenance).join('');
  return `<details class="card prop-maintenance" style="margin-top:14px;padding:0;border:1px solid var(--border);border-radius:4px">
    <summary style="padding:12px 14px;cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--bg2)">
      <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      <strong style="flex:1 1 auto;min-width:160px;color:var(--text)">Maintenance</strong>
      <span class="badge badge-muted" title="${count} hygiene / substrate item${count === 1 ? '' : 's'} (L0) folded here" style="font-size:0.72rem">${count} item${count === 1 ? '' : 's'}</span>
      <span class="subtle" style="font-size:0.78rem;color:var(--text3)">${dimNote}</span>
    </summary>
    <div style="padding:4px 8px 8px;background:var(--bg)">${body}</div>
  </details>`;
}

function renderArbiterProposals(inbox, inProcess, opts) {
  opts = opts || {};
  const inboxEl = document.getElementById('arbiter-proposals-list');
  const inProcessEl = document.getElementById('arbiter-in-process-list');
  // Surface-routing note: if proposals were filtered out by
  // surface (sysadmin/config/cleanup) they now show on Alerts.
  // Render a thin link above the list so the operator knows where
  // they went. The link triggers the Reports nav-item via the
  // existing nav() handler.
  const routedToAlerts = Number(opts.routedToAlerts || 0);
  const routedNoteEl = document.getElementById('arbiter-routed-note');
  if (routedNoteEl) {
    if (routedToAlerts > 0) {
      routedNoteEl.innerHTML = `<div style="font-size:0.78rem;color:var(--text3);margin:0 0 10px 2px">${routedToAlerts} sysadmin / hygiene proposal${routedToAlerts === 1 ? '' : 's'} routed to <a href="#" onclick="event.preventDefault();document.querySelector('.nav-item[data-page=&quot;reports&quot;]')?.click()" style="color:var(--accent)">Alerts</a> by surface.</div>`;
      routedNoteEl.style.display = '';
    } else {
      routedNoteEl.style.display = 'none';
    }
  }

  // Stash by id so the detail modal can re-render from the most recent
  // load without a separate fetch round-trip. Both queues stash into the
  // same map — proposal ids are unique across status.
  const all = [...(inbox || []), ...(inProcess || [])];
  window._proposalsById = new Map(all.map(p => [p.id, p]));

  // ── Inbox (pending + snoozed) ───────────────────────────────────────────
  if (inboxEl) {
    if (!inbox || !inbox.length) {
      inboxEl.innerHTML = `<div class="card" style="padding:18px;text-align:center;color:var(--muted)">No proposals need a decision right now.</div>` + _renderObservationsBlock(opts.observations);
    } else {
      // Altitude rail (Fit Reviewer Bite 2): lead with capability/strategic
      // ideas (L1+), fold ALL hygiene/substrate (L0) into one collapsed
      // Maintenance digest so a config nudge never buries an app suggestion.
      // Reset the coalesce-group stash ONCE for the whole render — both the
      // leading list and the Maintenance digest accumulate into it.
      window._propGroupMembers = {};
      // Conflicts need a decision regardless of altitude — detect on the full
      // inbox and surface them at the top, never folded into Maintenance.
      const { conflictGroups, rest } = _propExtractConflictGroups(inbox);
      // Partition the remainder by altitude. Each side keeps the server's
      // (−score) order; _propAltitudeScoreCmp re-applies (−altitude, −score)
      // defensively.
      const leading = rest.filter(p => _propAltitude(p) >= 1).sort(_propAltitudeScoreCmp);
      const maintenance = rest.filter(p => _propAltitude(p) === 0).sort(_propAltitudeScoreCmp);
      const parts = [];
      for (const group of conflictGroups) parts.push(renderConflictGroup(group));
      parts.push(..._renderInboxBody(leading));
      const digest = _renderMaintenanceDigest(maintenance);
      if (digest) parts.push(digest);
      inboxEl.innerHTML = parts.join('') + _renderObservationsBlock(opts.observations);
    }
  }

  // ── In Process ──────────────────────────────────────────────────────────
  if (inProcessEl) {
    if (!inProcess || !inProcess.length) {
      inProcessEl.innerHTML = `<div class="card" style="padding:18px;text-align:center;color:var(--muted)">Nothing in process. Recommendations you accept that need follow-through (manual investigation, workflow instructions, app builds via forge) will appear here until they close out.</div>`;
    } else {
      inProcessEl.innerHTML = inProcess.map(p => renderProposalCard(p)).join('');
    }
  }

  // ── Subtab count badges ─────────────────────────────────────────────────
  _updateSubtabCount('si-tab-count-inbox', (inbox || []).length);
  _updateSubtabCount('si-tab-count-in-process', (inProcess || []).length);

  // Reset the page-level bulk-action bar: selection state is wiped on
  // re-render (the underlying proposal ids may have moved subdirs).
  if (typeof _propSelectionChanged === 'function') _propSelectionChanged();
}

function _updateSubtabCount(badgeId, count) {
  const el = document.getElementById(badgeId);
  if (!el) return;
  if (count > 0) {
    el.textContent = String(count);
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}

function renderConflictGroup(group) {
  const cards = group.map(p => renderProposalCard(p, { insideConflict: true })).join('');
  return `
    <div class="card" style="padding:8px 10px;margin-bottom:10px;border-left:3px solid var(--orange)">
      <div style="font-size:0.8rem;color:var(--orange);margin-bottom:6px;font-weight:600">
        ⚡ Conflict group — these ${group.length} proposals touch the same thing; pick one
      </div>
      ${cards}
    </div>`;
}

// Coalesced sub-findings badge — surfaces "(N findings)" next to the
// title when arbiter.store folded N related proposals into one parent
// via coalesce_key. Count is the rolled-up sub_findings list (each
// representing a per-finding proposal that DIDN'T get written) plus
// the parent itself; reads as "this card covers N issues." Backward-
// compat: pre-coalesce proposals have no sub_findings and no badge.
function _subFindingsBadge(p) {
  const subs = Array.isArray(p.sub_findings) ? p.sub_findings.length : 0;
  if (!subs) return '';
  const total = subs + 1;
  return `<span class="badge badge-muted" title="${total} related findings rolled up under this proposal" style="font-size:0.72rem">${total} findings</span>`;
}

// Expanded sub-findings list for the proposal-detail view. One short
// line per sub-finding so the operator can see what the parent rolls
// up. Hidden when no coalescing happened.
function _renderSubFindingsBlock(p) {
  const subs = Array.isArray(p.sub_findings) ? p.sub_findings : [];
  if (!subs.length) return '';
  const rows = subs.map(sf => {
    const label = sf.admin_surface_summary || sf.problem || sf.trigger_observation || '(no detail)';
    return `<li style="margin-bottom:4px;font-size:0.82rem;color:var(--text2)">${escHtml(label)}</li>`;
  }).join('');
  return `
    <div style="margin-bottom:18px;padding:10px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:6px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:6px">Other findings rolled up here (${subs.length})</div>
      <ul style="margin:0;padding-left:18px">${rows}</ul>
    </div>`;
}

// Roles that carry a per-day cap seed; the cap input is shown only for these.
const _ADOPT_CAP_ROLES = new Set(['max', 'power']);
const _ADOPT_DEFAULT_MAX_CAP = 5;

// Operator role/cap picker for an AdoptModel proposal (spec §Addendum A).
// Shows the suggested rung + position + a role-mapping select (default
// `none` — never pre-select max) and a cap input revealed only when
// max/power is chosen. Returns '' for any non-AdoptModel proposal.
function _adoptModelPicker(p) {
  if ((p._action_kind || '') !== 'AdoptModel') return '';
  const a = p.action || {};
  // No tier yet (Addendum 13 retired the "new-rung" placeholder) — say so
  // plainly rather than render the internal slug. Bite 2 owns the card copy.
  const rungBit = a.rung_slug
    ? `Adopt into rung <code style="font-size:0.74rem">${escHtml(a.rung_slug)}</code>`
    : 'No suggested tier';
  const pos = (a.position == null) ? '' : ` · position ${escHtml(String(a.position))}`;
  const cost = a.cost_class ? ` · ${escHtml(a.cost_class)}` : '';
  const ev = a.evidence || {};
  const evBits = [];
  if (ev.context_window) evBits.push(`ctx ${Number(ev.context_window).toLocaleString()}`);
  if (ev.max_output_tokens) evBits.push(`out ${Number(ev.max_output_tokens).toLocaleString()}`);
  const evLine = evBits.length
    ? `<span style="color:var(--text3);font-size:0.74rem">${escHtml(evBits.join(' · '))}</span>` : '';
  const pid = escHtml(p.id);
  const roleOpts = ['none', 'fast', 'standard', 'power', 'max', 'judge']
    .map(r => `<option value="${r}">${r === 'none' ? 'none (adopt as dormant rung)' : r}</option>`)
    .join('');
  return `
    <div class="card" style="margin-top:8px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border)">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px">
        <span style="font-size:0.78rem;color:var(--text2)">${rungBit}${cost}${pos}</span>
        ${evLine}
      </div>
      <div style="font-size:0.72rem;color:var(--text3);margin-bottom:6px">
        Adoption is for models <strong>outside Evolve's defaults</strong> — the blessed ladder (Haiku/Sonnet/Opus/Fable classes) ships built in and never appears here.
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <label style="font-size:0.78rem;color:var(--text2)">Map role
          <select id="adopt-role-${pid}" class="input-w-md" onchange="_adoptRoleChanged('${pid}')" style="margin-left:6px">
            ${roleOpts}
          </select>
        </label>
        <label id="adopt-cap-wrap-${pid}" style="font-size:0.78rem;color:var(--text2);display:none">Daily cap / bot
          <input id="adopt-cap-${pid}" class="input-w-sm" type="number" min="1" step="1" value="${_ADOPT_DEFAULT_MAX_CAP}" style="margin-left:6px">
        </label>
      </div>
    </div>`;
}

// Toggle the cap input visibility when the role select changes. Seed the
// cap to the max default when switching to max with an empty field.
function _adoptRoleChanged(pid) {
  const sel = document.getElementById('adopt-role-' + pid);
  const wrap = document.getElementById('adopt-cap-wrap-' + pid);
  if (!sel || !wrap) return;
  const role = sel.value;
  const show = _ADOPT_CAP_ROLES.has(role);
  wrap.style.display = show ? '' : 'none';
  if (show) {
    const cap = document.getElementById('adopt-cap-' + pid);
    if (cap && !cap.value) cap.value = String(_ADOPT_DEFAULT_MAX_CAP);
  }
}

// ── Coalesced AdoptModel group: per-model + batch adoption (Bite 2) ──────────
//
// Spec: internal/design-recommendation-legibility-2026-06-12.md (Bite 2). A
// coalesced model_discovery card carries its own head AdoptModel plus N folded
// sub-findings (one model each). Bite 1 collapsed the N fat cards into one but
// left only the head adoptable. These render a per-model adopt control for
// EVERY model in the group (drill-down) plus a one-click "Adopt all as
// dormant" — each backed by the /adopt-model and /adopt-all-dormant endpoints,
// which reconcile the parent so the rest of the group stays on the card.
//
// NB: this is the server-side `sub_findings` coalesced group, NOT the
// client-side `_propGroupSimilar` visual grouping — distinct features.

// True for a coalesced AdoptModel parent (≥1 folded sub-finding). Single-model
// AdoptModel proposals keep the existing inline picker + Act path unchanged.
function _isCoalescedAdopt(p) {
  const kind = p._action_kind || (p.action && p.action.kind) || '';
  return kind === 'AdoptModel'
    && Array.isArray(p.sub_findings) && p.sub_findings.length > 0;
}

// Normalize one group member (head or folded sub) to the fields the row needs.
// Prefers the member's serialized action; falls back to provenance signals for
// legacy folds written before the sub-finding record carried its action.
function _adoptEntry(key, action, ps) {
  action = action || {};
  ps = ps || {};
  const provider = action.provider || ps.provider || '';
  let modelId = action.model_id || '';
  let qualified = ps.qualified_id
    || (provider && modelId ? `${provider}/${modelId}` : modelId);
  if (!modelId && qualified && qualified.includes('/')) {
    modelId = qualified.split('/').slice(1).join('/');
  }
  // Empty = no placeable tier (Addendum 13 retired the "new-rung" placeholder);
  // the row renderer shows "no suggested tier" rather than the dead slug.
  const rung = action.rung_slug || ps.suggested_rung_slug || '';
  const cost = action.cost_class || ps.suggested_cost_class || '';
  const position = (action.position != null)
    ? action.position
    : (ps.suggested_position != null ? ps.suggested_position : null);
  const evidence = action.evidence || ps.evidence || {};
  return { key, qualified: qualified || modelId || key, rung, cost, position, evidence };
}

// Ordered adoptable models in the group: the parent's own head model first,
// then each folded sub-finding.
function _adoptGroupEntries(p) {
  const entries = [];
  const head = p.action || {};
  const headKey = (Array.isArray(p.trigger_observations) && p.trigger_observations[0]) || '';
  if (head.kind === 'AdoptModel') {
    entries.push(_adoptEntry(headKey, head, p.provenance && p.provenance.signals));
  }
  const subs = Array.isArray(p.sub_findings) ? p.sub_findings : [];
  for (const sf of subs) {
    const a = (sf.action && sf.action.kind === 'AdoptModel') ? sf.action : null;
    entries.push(_adoptEntry(sf.trigger_observation || '', a, sf.provenance_signals));
  }
  return entries;
}

// One adopt row: model id + suggested rung/cost/position + listing evidence,
// a role select (default none = dormant) and a cap input shown only for
// max/power, and an Adopt button.
function _renderAdoptGroupRow(parentId, e, idx) {
  const pid = escHtml(parentId);
  const rid = `${pid}-${idx}`;
  const cost = e.cost ? ` · ${escHtml(e.cost)}` : '';
  const pos = (e.position == null) ? '' : ` · position ${escHtml(String(e.position))}`;
  const ev = e.evidence || {};
  const evBits = [];
  if (ev.context_window) evBits.push(`ctx ${Number(ev.context_window).toLocaleString()}`);
  if (ev.max_output_tokens) evBits.push(`out ${Number(ev.max_output_tokens).toLocaleString()}`);
  const evLine = evBits.length
    ? `<span style="color:var(--text3);font-size:0.74rem"> · ${escHtml(evBits.join(' · '))}</span>`
    : '';
  const rungBit = e.rung
    ? `→ rung <code style="font-size:0.72rem">${escHtml(e.rung)}</code>`
    : 'no suggested tier';
  const roleOpts = ['none', 'fast', 'standard', 'power', 'max', 'judge']
    .map(r => `<option value="${r}">${r === 'none' ? 'none (dormant)' : r}</option>`)
    .join('');
  return `
    <div class="card" style="margin-bottom:6px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border)">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px">
        <code style="font-size:0.8rem">${escHtml(e.qualified)}</code>
        <span style="font-size:0.74rem;color:var(--text2)">${rungBit}${cost}${pos}</span>
        ${evLine}
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <label style="font-size:0.78rem;color:var(--text2)">Map role
          <select id="adopt-grp-role-${rid}" class="input-w-md" onchange="_adoptGroupRoleChanged('${pid}', ${idx})" style="margin-left:6px">
            ${roleOpts}
          </select>
        </label>
        <label id="adopt-grp-cap-wrap-${rid}" style="font-size:0.78rem;color:var(--text2);display:none">Daily cap / bot
          <input id="adopt-grp-cap-${rid}" class="input-w-sm" type="number" min="1" step="1" value="${_ADOPT_DEFAULT_MAX_CAP}" style="margin-left:6px">
        </label>
        <span style="flex:1"></span>
        <button class="btn btn-sm" onclick="_adoptOneModel('${pid}', ${idx})">Adopt</button>
      </div>
    </div>`;
}

// The full per-model adopt section for a coalesced AdoptModel group, rendered
// in the proposal-detail drill-down. Stashes the per-model keys on window so
// the handlers look them up by index (avoids escaping colons in onclick).
function _renderAdoptGroupBlock(p) {
  const entries = _adoptGroupEntries(p);
  if (!entries.length) return '';
  window._adoptGroups = window._adoptGroups || {};
  window._adoptGroups[p.id] = entries.map(e => ({ key: e.key }));
  const pid = escHtml(p.id);
  const rows = entries.map((e, i) => _renderAdoptGroupRow(p.id, e, i)).join('');
  return `
    <div style="margin-bottom:18px">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px">
        <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em">Models available to adopt (${entries.length})</div>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" onclick="_adoptAllDormant('${pid}')" title="Adopt every model below as a dormant catalog entry — no role mapped">Adopt all as dormant</button>
      </div>
      <div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">
        Each adopts as a <strong>dormant rung</strong> by default (no role mapped); mapping max/power is a separate deliberate choice. Adopting one leaves the rest here.
      </div>
      ${rows}
    </div>`;
}

// Card-level actionable body. For a coalesced AdoptModel group, the per-model
// pickers live behind the drill-down, so the card offers "Review & adopt" +
// "Adopt all as dormant"; every other actionable proposal keeps the inline
// single-model picker (when AdoptModel) + its Act button.
function _renderActionableCardBody(p, acceptHandler, acceptLabel) {
  const pid = escHtml(p.id);
  if (_isCoalescedAdopt(p)) {
    const n = (Array.isArray(p.sub_findings) ? p.sub_findings.length : 0) + 1;
    return `
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        <button class="btn btn-sm" onclick="openProposalDetail('${pid}')">Review &amp; adopt ${n} models</button>
        <button class="btn btn-ghost btn-sm" onclick="_adoptAllDormant('${pid}')">Adopt all as dormant</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterSnooze('${pid}')">Snooze 1w</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${pid}')">Dismiss</button>
      </div>`;
  }
  return `
    ${_adoptModelPicker(p)}
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      <button class="btn btn-sm" onclick="${acceptHandler}">${escHtml(acceptLabel)}</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterSnooze('${pid}')">Snooze 1w</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${pid}')">Dismiss</button>
    </div>`;
}

// Toggle a group row's cap input when its role select changes.
function _adoptGroupRoleChanged(parentId, idx) {
  const rid = `${parentId}-${idx}`;
  const sel = document.getElementById('adopt-grp-role-' + rid);
  const wrap = document.getElementById('adopt-grp-cap-wrap-' + rid);
  if (!sel || !wrap) return;
  const show = _ADOPT_CAP_ROLES.has(sel.value);
  wrap.style.display = show ? '' : 'none';
  if (show) {
    const cap = document.getElementById('adopt-grp-cap-' + rid);
    if (cap && !cap.value) cap.value = String(_ADOPT_DEFAULT_MAX_CAP);
  }
}

// Adopt ONE model from a coalesced group. Reads the row's role/cap, POSTs to
// /adopt-model, then refreshes — re-opening the drill-down if the group still
// has models left, or closing it if the card drained.
async function _adoptOneModel(parentId, idx) {
  const group = (window._adoptGroups || {})[parentId];
  const entry = group && group[idx];
  if (!entry) { loadArbiterProposals(); return; }
  const rid = `${parentId}-${idx}`;
  const roleSel = document.getElementById('adopt-grp-role-' + rid);
  const role = (roleSel && roleSel.value) || 'none';
  const payload = { model_key: entry.key, role };
  if (_ADOPT_CAP_ROLES.has(role)) {
    const capEl = document.getElementById('adopt-grp-cap-' + rid);
    const cap = capEl && capEl.value ? parseInt(capEl.value, 10) : null;
    if (cap != null && Number.isFinite(cap)) payload.cap = cap;
  }
  const r = await fetch(
    `/api/arbiter/proposals/${encodeURIComponent(parentId)}/adopt-model`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
  );
  const j = await r.json();
  if (!j.ok) { toast('Adopt failed: ' + (j.message || j.error || 'unknown'), 'err'); return; }
  await loadArbiterProposals();
  loadArbiterRateLimit();
  if (j.group_disposition === 'kept'
      && window._proposalsById && window._proposalsById.has(parentId)) {
    openProposalDetail(parentId);
  } else {
    closeProposalDetail();
  }
}

// Adopt EVERY model in a coalesced group as a dormant catalog entry.
async function _adoptAllDormant(parentId) {
  if (!await confirmModal('Adopt every model in this group as a dormant catalog entry (no role mapped)? You can map roles later from Settings → Models.')) return;
  const r = await fetch(
    `/api/arbiter/proposals/${encodeURIComponent(parentId)}/adopt-all-dormant`,
    { method: 'POST' },
  );
  const j = await r.json();
  if (!j.ok && !j.adopted) {
    toast('Adopt all failed: ' + (j.message || j.error || 'unknown'), 'err');
    return;
  }
  if (j.failed) {
    toast(`Adopted ${j.adopted} model(s); ${j.failed} could not be applied and remain on the card.`, 'ok');
  }
  closeProposalDetail();
  await loadArbiterProposals();
  loadArbiterRateLimit();
}

function renderProposalCard(p, opts = {}) {
  const urgency = p.urgency || 'improvement';
  const urgColor = URGENCY_COLORS[urgency] || 'var(--text2)';
  const dim = p.dimension || '';
  const adj = p.adjacency_type;
  const verify = p._verify_status || 'unknown';
  const verifyBadge = VERIFY_LABELS[verify] || VERIFY_LABELS.unknown;

  const annotations = (p.guardian_annotations || []).map(a => {
    const sev = a.severity || 'low';
    return `<span class="badge" title="${escHtml(a.reason || '')}" style="background:rgba(255,140,66,0.15);color:#ff8c42;margin-right:4px;font-size:0.72rem">⚠ ${escHtml(a.guardian_id || '')} · ${escHtml(sev)}</span>`;
  }).join('');

  const adjInfo = adj ? ADJACENCY_LABELS[adj] : null;
  const adjacencyBadge = adjInfo
    ? `<span class="badge" title="${escHtml(adjInfo.tip)} (${escHtml(adj)})" style="background:rgba(127,200,255,0.15);color:var(--blue);margin-right:4px;font-size:0.72rem">adjacency: ${escHtml(adjInfo.word)}</span>`
    : (adj ? `<span class="badge" style="background:rgba(127,200,255,0.15);color:var(--blue);margin-right:4px;font-size:0.72rem">${escHtml(adj)}</span>` : '');

  const approval = p.approval_audience || 'none';
  const approvalBadge = `<span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);margin-right:4px;font-size:0.72rem">${escHtml(approval)}</span>`;

  // Estimated-savings chip (PR H). Generator-supplied dollar-per-week
  // estimate; null on most proposals (only cache_ttl_tuner populates
  // today). Floor at $0.50 to avoid cluttering the card with rounding
  // noise. Tooltip explains the heuristic since this is an estimate,
  // not a measurement.
  const savingsChip = _renderSavingsChip(p.estimated_savings_usd);

  const actionKind = p._action_kind || '';
  const claim = p.claim
    ? `<div style="font-size:0.78rem;color:var(--muted);margin-top:4px"><b>Claim:</b> ${escHtml(claim_to_string(p.claim))}</div>`
    : '';
  const revert = p.revert_on_failure
    ? `<div style="font-size:0.78rem;color:var(--muted);margin-top:2px"><b>Revert:</b> on failure, snapshot restored</div>`
    : '';

  const conflictLine = (p.conflicts_with || []).length && !opts.insideConflict
    ? `<div style="font-size:0.78rem;color:#ff8c42;margin-top:4px">⚡ Conflicts with ${p.conflicts_with.length} other proposal(s)</div>`
    : '';

  const wrapperStyle = opts.insideConflict ? 'margin-bottom:8px' : 'margin-bottom:10px';

  const actionable = p.status === 'pending';
  const failed = p.status === 'failed_flagged';
  const dispatched = p.status === 'dispatched';
  // approved_human / approved_auto in the pending subdir means apply
  // was attempted but never completed. The post-fix Act handler can no
  // longer leave a proposal here, but legacy stuck proposals from before
  // the fix still need a way out — treat them like a soft failure.
  const stuck = p.status === 'approved_human' || p.status === 'approved_auto';
  // In Process: applied-status proposals whose close-out is deferred.
  // Manual-completion kinds (Investigation, WorkflowInstruction) wait for
  // the operator's Mark complete click; external-completion kinds
  // (BuildApp) wait for forge_sweep to read the forge job status.
  const inProcess = p.status === 'applied'
    && _IN_PROCESS_KINDS.includes(p._action_kind);
  const isManualKind = _MANUAL_COMPLETION_KINDS.includes(p._action_kind);
  const isExternalKind = _EXTERNAL_COMPLETION_KINDS.includes(p._action_kind);
  // Slice 3 (2026-06-04): proposals with a dispatch_target route the
  // accept button through the dispatch confirmation modal instead of
  // moving the proposal to In Process. Operator-only proposals
  // (dispatch_target == null) keep today's "Take this on" behavior.
  const dispatchTarget = p.dispatch_target || null;
  const isDispatchable = !!dispatchTarget;
  const dispatchResult = (p.dispatch_state && p.dispatch_state.result) || null;
  const dispatchFailed = failed && dispatchResult && dispatchResult.outcome === 'failed';
  // Per-action-kind verb on the inbox card. Deferred-completion kinds
  // get "Take this on" because Act here moves them to In Process rather
  // than executing immediately.
  let acceptLabel;
  if (isDispatchable) {
    if (dispatchTarget === 'evo') {
      acceptLabel = 'Have evo fix this';
    } else if (dispatchTarget === 'forge') {
      acceptLabel = 'Have forge handle this';
    } else {
      acceptLabel = `Send to ${dispatchTarget}`;
    }
  } else {
    acceptLabel = (isManualKind || isExternalKind) ? 'Take this on' : 'Act';
  }
  const acceptHandler = isDispatchable
    ? `openDispatchConfirmModal('${escHtml(p.id)}')`
    : `arbiterAct('${escHtml(p.id)}')`;

  let failureBanner = '';
  if (dispatchFailed) {
    const reason = (dispatchResult && dispatchResult.message) || 'Dispatch failed (no message reported)';
    const failedTarget = (p.dispatch_state && p.dispatch_state.target) || dispatchTarget || 'target';
    failureBanner = `
      <div style="margin-top:6px;padding:8px 10px;background:color-mix(in srgb, var(--red) 12%, transparent);border-left:3px solid var(--red);border-radius:3px">
        <div style="font-size:0.78rem;color:var(--red);font-weight:600;margin-bottom:2px">Dispatch to ${escHtml(failedTarget)} failed</div>
        <div style="font-size:0.78rem;color:var(--text2);font-family:monospace;white-space:pre-wrap;word-break:break-word">${escHtml(reason)}</div>
      </div>`;
  } else if (failed) {
    const hist = Array.isArray(p.history) ? p.history : [];
    const lastFail = [...hist].reverse().find(h => h.to_status === 'failed_flagged');
    const reason = (lastFail && lastFail.reason) || 'Apply failed (no reason recorded)';
    failureBanner = `
      <div style="margin-top:6px;padding:8px 10px;background:color-mix(in srgb, var(--red) 12%, transparent);border-left:3px solid var(--red);border-radius:3px">
        <div style="font-size:0.78rem;color:var(--red);font-weight:600;margin-bottom:2px">Apply failed</div>
        <div style="font-size:0.78rem;color:var(--text2);font-family:monospace;white-space:pre-wrap;word-break:break-word">${escHtml(reason)}</div>
      </div>`;
  } else if (stuck) {
    failureBanner = `
      <div style="margin-top:6px;padding:8px 10px;background:color-mix(in srgb, var(--yellow) 12%, transparent);border-left:3px solid var(--yellow);border-radius:3px">
        <div style="font-size:0.78rem;color:var(--yellow);font-weight:600;margin-bottom:2px">Apply did not complete</div>
        <div style="font-size:0.78rem;color:var(--text2)">The proposal was approved but apply never finished (status: ${escHtml(p.status)}). Retry to attempt again, or dismiss to clear it from the queue.</div>
      </div>`;
  }

  const borderColor = failed ? 'var(--red)' : (stuck ? 'var(--yellow)' : (dispatched ? 'var(--blue)' : urgColor));

  // Slice 3 dispatched-state banner. Rendered at top of the card so the
  // operator sees the in-flight state before reading the body.
  let dispatchedBanner = '';
  if (dispatched && p.dispatch_state) {
    const target = p.dispatch_state.target || dispatchTarget || 'target';
    const when = p.dispatch_state.dispatched_at ? ago(p.dispatch_state.dispatched_at) : 'just now';
    dispatchedBanner = `
      <div style="margin-bottom:6px;font-size:0.78rem;color:var(--blue)">
        ⚙ Dispatched to ${escHtml(target)} · ${escHtml(when)}
      </div>`;
  }

  // Multi-select checkbox for the page-level bulk-action bar. Only
  // actionable (pending) proposals get a selectable checkbox — selecting
  // a dispatched/in-process/terminal proposal can't dismiss it through
  // the bulk endpoint anyway. Click on the checkbox is stopPropagation'd
  // so it never opens the proposal detail modal.
  // ``data-dispatch-target`` lets the bulk bar's Apply enable/disable
  // logic read whether this proposal needs operator confirmation
  // without re-walking the proposals cache.
  const bulkCheckbox = actionable
    ? `<input type="checkbox" class="prop-row-select"
              data-prop-id="${escHtml(p.id)}"
              data-action-kind="${escHtml(actionKind || '')}"
              data-dispatch-target="${escHtml(p.dispatch_target || '')}"
              data-in-process="${(isManualKind || isExternalKind) ? '1' : '0'}"
              onclick="event.stopPropagation()"
              onchange="_propSelectionChanged()"
              style="margin:0;margin-top:4px;cursor:pointer;flex:0 0 auto"
              title="Select for bulk action">`
    : '';

  return `
    <div class="card" style="padding:12px 14px;${wrapperStyle};border-left:3px solid ${borderColor}">
      <div style="display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap">
        ${bulkCheckbox}
        <span title="${escHtml(verifyBadge.title)}" style="font-size:1rem;margin-top:2px">${verifyBadge.text}</span>
        <div style="flex:1;min-width:0">
          ${dispatchedBanner}
          <div style="display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;margin-bottom:4px">
            <a href="#" onclick="event.preventDefault();openProposalDetail('${escHtml(p.id)}')" style="color:inherit;text-decoration:none;border-bottom:1px dashed rgba(255,255,255,0.25)" title="View full proposal"><strong style="font-size:0.95rem">${escHtml(p.human_title || p.admin_surface_summary || p.problem || '(no title)')}</strong></a>
            ${_altitudeBadge(p)}
            ${_subFindingsBadge(p)}
            <span class="badge" style="background:${urgColor};color:var(--on-accent);font-size:0.7rem;padding:1px 6px">${escHtml(urgency)}</span>
            ${dim ? `<span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);font-size:0.7rem;padding:1px 6px">${escHtml(dim)}</span>` : ''}
            ${adjacencyBadge}
            ${approvalBadge}
            ${actionKind ? `<span class="badge" style="background:rgba(200,200,200,0.08);color:var(--text2);font-size:0.7rem;padding:1px 6px">${escHtml(actionKind)}</span>` : ''}
            ${actionKind === 'AddSignalCollection' ? `<span class="badge" title="Proposal-synthesizer signal-gap: extend evolve's observation layer" style="background:rgba(180,127,255,0.15);color:var(--accent);font-size:0.7rem;padding:1px 6px">observability gap</span>` : ''}
            ${savingsChip}
          </div>
          ${p.value_line ? `<div style="font-size:0.82rem;color:var(--text2);margin-bottom:4px" title="Pod-grounded value — derived from this pod's tier usage × cited model pricing. Full derivation in the proposal detail.">${_mdInline(p.value_line)}</div>` : ''}
          ${(p.admin_surface_summary && p.problem && p.admin_surface_summary !== p.problem) ? `<div style="font-size:0.82rem;color:var(--muted, #aaa);margin-bottom:4px">${_mdInline(p.problem)}</div>` : ''}
          ${_lineageSummary(p._lineage)}
          ${annotations ? `<div style="margin:4px 0">${annotations}</div>` : ''}
          ${claim}
          ${revert}
          ${failureBanner}
          ${conflictLine}
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;align-items:center">
            <span style="font-size:0.72rem;color:var(--text3)">bot: ${p.bot_id ? `<a href="#" onclick="event.preventDefault();_arbiterFilterByBot('${escHtml(p.bot_id)}')" title="Filter the queue to this bot">${escHtml(botLabel(p.bot_id))}</a>` : ''} · gen: ${escHtml(p.generator_id || '')} · ${escHtml(ago(p.created_at))}</span>
            ${scoreBreakdownChip(p)}
          </div>
          ${actionable ? _renderActionableCardBody(p, acceptHandler, acceptLabel) : (dispatched ? `
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            <button class="btn btn-ghost btn-sm" onclick="arbiterCancelDispatch('${escHtml(p.id)}')">Cancel dispatch</button>
          </div>` : (inProcess && isManualKind ? `
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            <button class="btn btn-sm" onclick="arbiterComplete('${escHtml(p.id)}')">Mark complete</button>
            <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>
          </div>` : (inProcess && isExternalKind ? `
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;align-items:center">
            ${_forgeStatusBadge(p._forge_job)}
            <span style="flex:1"></span>
            <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>
          </div>` : (dispatchFailed ? `
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            <button class="btn btn-sm" onclick="arbiterRetryDispatch('${escHtml(p.id)}')">Retry</button>
            <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>
          </div>` : ((failed || stuck) ? `
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            <button class="btn btn-sm" onclick="arbiterRetry('${escHtml(p.id)}')">Retry</button>
            <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>
          </div>` : `<div style="font-size:0.72rem;color:var(--text3);margin-top:6px">status: ${escHtml(p.status)}</div>`)))))}
        </div>
      </div>
    </div>`;
}

function claim_to_string(c) {
  if (!c) return '';
  const arrow = c.direction === 'down' ? '↓' : c.direction === 'up' ? '↑' : '=';
  return `${c.metric || '?'} ${arrow} by ~${c.magnitude} over ${c.window_days}d (fallback: ${c.fallback})`;
}

// Forge job status badge for BuildApp proposals in the In Process queue.
// Shows the current forge state with a step counter when running, or a
// "queued / awaiting…" hint when forge hasn't started yet. Color matches
// the urgency of the situation (running = blue/info, awaiting_approval =
// amber, anything else = muted).
const _FORGE_STATUS_COLOR = {
  queued: 'var(--text2)',
  running: 'var(--blue)',
  awaiting_approval: '#ffb83c',
  approved: 'var(--blue)',
  complete: 'var(--green)',  // shouldn't be visible (sweep would have closed)
  failed: 'var(--red)',
  rejected: '#ff8c42',
};

function _forgeStatusBadge(job) {
  if (!job || !job.status) {
    return `<span class="subtle" style="font-size:0.78rem;color:var(--text3)">forge handed off; status pending…</span>`;
  }
  const color = _FORGE_STATUS_COLOR[job.status] || 'var(--text2)';
  const stepLabel = job.current_step && job.step_count
    ? ` (step ${job.current_step} / ${job.step_count})`
    : '';
  const idLabel = job.job_id
    ? ` <code style="font-size:0.7rem;color:var(--text3)">${escHtml(job.job_id)}</code>`
    : '';
  return `<span style="font-size:0.78rem"><span style="color:${color}">forge: ${escHtml(job.status)}${escHtml(stepLabel)}</span>${idLabel}</span>`;
}

// Compact lineage summary chip rendered on each proposal card. Hidden when
// there's no prior history of this fingerprint. Aggregates entries by
// terminal status so the operator sees "2× dismissed" rather than two
// separate lines.
const _LINEAGE_STATUS_COLORS = {
  succeeded: 'var(--green)',
  failed_reverted: '#ff8c42',
  failed_flagged: 'var(--red)',
  failed_revert_failed: 'var(--red)',
  rejected: '#ff8c42',
  dismissed: 'var(--text2)',
  superseded: 'var(--text2)',
  resolved_externally: 'var(--blue)',
};

function _lineageSummary(lineage) {
  if (!lineage || !lineage.length) return '';
  // Group by terminal status; keep the newest terminal_at per group.
  const groups = new Map();
  for (const e of lineage) {
    const g = groups.get(e.status) || { count: 0, last_at: '' };
    g.count += 1;
    const ts = e.terminal_at || e.created_at || '';
    if (!g.last_at || ts > g.last_at) g.last_at = ts;
    groups.set(e.status, g);
  }
  const parts = [];
  for (const [status, g] of groups) {
    const color = _LINEAGE_STATUS_COLORS[status] || 'var(--text2)';
    const label = `${g.count}× ${status.replace(/_/g, ' ')}`;
    const lastAgo = g.last_at ? ` (last ${ago(g.last_at)})` : '';
    parts.push(`<span style="color:${color}">${escHtml(label)}</span><span style="color:var(--text3)">${escHtml(lastAgo)}</span>`);
  }
  return `<div style="font-size:0.74rem;color:var(--text3);margin:2px 0 4px 0">
    <span style="color:var(--text3)">Previously:</span> ${parts.join('<span style="color:var(--text3)"> · </span>')}
  </div>`;
}

function _lineageDetail(lineage) {
  // Full lineage card for the detail modal — one row per past entry.
  if (!lineage || !lineage.length) return '';
  const rows = lineage.map(e => {
    const color = _LINEAGE_STATUS_COLORS[e.status] || 'var(--text2)';
    const when = e.terminal_at || e.created_at || '';
    return `<li style="margin-bottom:4px;font-size:0.82rem">
      <span style="color:${color};font-family:var(--font-mono);font-size:0.74rem">${escHtml(e.status.replace(/_/g, ' '))}</span>
      <span class="subtle" style="font-size:0.72rem;margin-left:6px">${when ? ago(when) : '—'}</span>
      <span style="color:var(--text2);font-size:0.78rem;margin-left:8px">${escHtml(e.problem || '')}</span>
    </li>`;
  }).join('');
  return `
    <div class="card" style="padding:10px 14px;margin-top:10px">
      <div class="card-title" style="margin-bottom:6px">Lineage — past occurrences of this fingerprint</div>
      <div class="subtle" style="font-size:0.78rem;margin-bottom:6px">
        Same-fingerprint proposals that have already been resolved one way or another. Useful context for "didn't I already approve this?" or "this keeps coming back."
      </div>
      <ul style="margin:0;padding-left:18px">${rows}</ul>
    </div>`;
}

async function arbiterAct(id) {
  if (!await confirmModal('Apply this proposal?')) return;
  // AdoptModel proposals carry the operator's role-mapping + cap choices in
  // the request body (spec §Addendum A). The picker only exists for those;
  // a plain Act on any other proposal sends an empty body, unchanged.
  let opts = { method: 'POST' };
  const roleSel = document.getElementById('adopt-role-' + id);
  if (roleSel) {
    const role = roleSel.value || 'none';
    const payload = { role };
    if (_ADOPT_CAP_ROLES.has(role)) {
      const capEl = document.getElementById('adopt-cap-' + id);
      const cap = capEl && capEl.value ? parseInt(capEl.value, 10) : null;
      if (cap != null && Number.isFinite(cap)) payload.cap = cap;
    }
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(payload);
  }
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/act`, opts);
  const j = await r.json();
  if (!j.ok) toast('Act failed: ' + (j.message || j.error || 'unknown'), 'err');
  closeProposalDetail();
  loadArbiterProposals();
  loadArbiterRateLimit();
}

// ── Slice 3 (2026-06-04): dispatch flow ─────────────────────────────────────
//
// "Have evo fix this" / "Send to {bot}" / "Have forge handle this" route
// through a small confirmation modal so the operator sees the verbatim
// dispatch_message before firing. The POST goes to the Phase 3.1 endpoint
// /api/arbiter/proposals/<id>/dispatch with an empty body — the server
// reads proposal.dispatch_target and proposal.dispatch_message.
// Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md.

function openDispatchConfirmModal(id) {
  const p = (window._proposalsById && window._proposalsById.get(id));
  if (!p) {
    toast('Proposal no longer in the loaded list — refreshing.', 'err');
    loadArbiterProposals();
    return;
  }
  const target = p.dispatch_target || 'target';
  const message = p.dispatch_message || p.admin_surface_summary || p.problem || '(no message)';
  let modal = document.getElementById('dispatch-confirm-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'dispatch-confirm-modal';
    modal.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9000;align-items:center;justify-content:center;padding:24px';
    modal.innerHTML = `
      <div class="card" id="dispatch-confirm-card" style="max-width:560px;width:100%;padding:18px 20px">
        <div id="dispatch-confirm-title" style="font-size:1rem;font-weight:600;margin-bottom:10px"></div>
        <div style="font-size:0.82rem;color:var(--muted, #aaa);margin-bottom:6px">The target will receive:</div>
        <div id="dispatch-confirm-message" style="font-size:0.85rem;color:var(--text2);background:var(--bg3, rgba(255,255,255,0.04));border-left:3px solid var(--blue);padding:8px 10px;border-radius:3px;white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto;margin-bottom:10px"></div>
        <div style="font-size:0.78rem;color:var(--text3, #888);margin-bottom:14px">The target will report back when done. You can cancel from the proposal at any time.</div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn btn-ghost btn-sm" onclick="closeDispatchConfirmModal()">Cancel</button>
          <button class="btn btn-sm" id="dispatch-confirm-send"></button>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  const title = document.getElementById('dispatch-confirm-title');
  const msgEl = document.getElementById('dispatch-confirm-message');
  const sendBtn = document.getElementById('dispatch-confirm-send');
  if (title) title.textContent = `Send this to ${target}?`;
  if (msgEl) msgEl.textContent = message;
  if (sendBtn) {
    sendBtn.textContent = `Send to ${target}`;
    sendBtn.onclick = () => sendDispatch(id);
  }
  modal.style.display = 'flex';
}

function closeDispatchConfirmModal() {
  const modal = document.getElementById('dispatch-confirm-modal');
  if (modal) modal.style.display = 'none';
}

async function sendDispatch(id) {
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/dispatch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const j = await r.json();
  if (!j.ok) {
    toast('Dispatch failed: ' + (j.message || j.error || 'unknown'), 'err');
    return;
  }
  closeDispatchConfirmModal();
  closeProposalDetail();
  loadArbiterProposals();
}

async function arbiterCancelDispatch(id) {
  if (!await confirmModal({body: 'Cancel this dispatch? The proposal returns to the inbox; if the target already applied changes you\'ll still see them on the next refresh.', danger: true})) return;
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/dispatch/cancel`, { method: 'POST' });
  const j = await r.json();
  if (!j.ok) toast('Cancel failed: ' + (j.message || j.error || 'unknown'), 'err');
  closeProposalDetail();
  loadArbiterProposals();
}

async function arbiterRetryDispatch(id) {
  if (!await confirmModal('Retry the dispatch?')) return;
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/dispatch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const j = await r.json();
  if (!j.ok) toast('Retry failed: ' + (j.message || j.error || 'unknown'), 'err');
  closeProposalDetail();
  loadArbiterProposals();
}

async function arbiterRetry(id) {
  if (!await confirmModal('Retry applying this proposal?')) return;
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/retry`, { method: 'POST' });
  const j = await r.json();
  if (!j.ok) toast('Retry failed: ' + (j.message || j.error || 'unknown'), 'err');
  closeProposalDetail();
  loadArbiterProposals();
  loadArbiterRateLimit();
}

async function arbiterComplete(id) {
  if (!await confirmModal('Mark this proposal complete? Use this once you\'ve done the offline work the proposal describes.')) return;
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/complete`, { method: 'POST' });
  const j = await r.json();
  if (!j.ok) toast('Mark complete failed: ' + (j.message || j.error || 'unknown'), 'err');
  closeProposalDetail();
  loadArbiterProposals();
}

// ── Proposal detail drawer ──────────────────────────────────────────────
//
// Opens when the operator clicks a proposal's title. Shows the FULL action
// payload (rendered per kind), claim, revert plan, motivating signals,
// guardian annotations, and history — so the operator can review what
// they're actually approving rather than just title + claim string.

function openProposalDetail(id) {
  const p = (window._proposalsById && window._proposalsById.get(id));
  if (!p) {
    toast('Proposal no longer in the loaded list — refreshing.', 'err');
    loadArbiterProposals();
    return;
  }
  const modal = document.getElementById('proposal-detail-modal');
  const body = document.getElementById('proposal-detail-body');
  if (!modal || !body) return;
  body.innerHTML = renderProposalDetail(p);
  modal.style.display = 'flex';
}

function closeProposalDetail() {
  const modal = document.getElementById('proposal-detail-modal');
  if (modal) modal.style.display = 'none';
}

// ─────────────────────────────────────────────────────────────────────────
// Phase A operator-first content rendering (spec:
// internal/spec-proposal-drafting-protocol-2026-06-04.md).
//
// When a proposal has been migrated to the new shape (Summary set),
// render the five-section layout: Title / Summary / Proposed Action
// / Explanation / Details (collapsed). Pre-migration proposals fall
// through to renderProposalDetail's legacy layout unchanged.
//
// The Reports-page proposal-render path is being reworked in a
// separate session — this only touches the Recommendations modal.
// ─────────────────────────────────────────────────────────────────────────

function _isPhaseAContent(p) {
  // The Summary field is the canonical signal that the generator has
  // migrated to the new content shape. Even if other Phase A fields
  // are null, a non-empty summary means "treat this as new shape."
  return p && typeof p.summary === 'string' && p.summary.trim().length > 0;
}

function _renderProposalFallbackPaths(p) {
  // Tier 2-5 supplemental action paths from the new schema. The
  // primary action button is rendered by the existing actionBar code
  // below. These are the "or you can do it this way" alternatives
  // for operators who prefer a different path.
  const parts = [];
  if (p.manual_path && p.manual_path.trim()) {
    parts.push(`
      <div style="font-size:0.82rem;color:var(--text2);margin-top:8px;line-height:1.5">
        Or open <strong>${escHtml(p.manual_path)}</strong> and change the setting yourself.
      </div>`);
  }
  if (p.cli_command && p.cli_command.trim()) {
    const cmd = p.cli_command.trim();
    parts.push(`
      <div style="margin-top:10px">
        <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Or run from the command line</div>
        <pre style="background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:8px 10px;font-size:0.78rem;color:var(--text);overflow-x:auto;margin:0">${escHtml(cmd)}</pre>
        <button class="btn btn-ghost btn-sm" style="margin-top:4px;font-size:0.72rem" onclick="navigator.clipboard.writeText(${JSON.stringify(cmd)});this.textContent='Copied ✓';setTimeout(()=>{this.textContent='Copy command';},1500)">Copy command</button>
      </div>`);
  }
  if (p.manual_instruction && p.manual_instruction.trim()) {
    const instr = p.manual_instruction.trim();
    parts.push(`
      <div style="margin-top:10px">
        <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Or tell the bot to do this</div>
        <pre style="background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:8px 10px;font-size:0.78rem;color:var(--text);white-space:pre-wrap;word-break:break-word;margin:0">${escHtml(instr)}</pre>
        <button class="btn btn-ghost btn-sm" style="margin-top:4px;font-size:0.72rem" onclick="navigator.clipboard.writeText(${JSON.stringify(instr)});this.textContent='Copied ✓';setTimeout(()=>{this.textContent='Copy instruction';},1500)">Copy instruction</button>
      </div>`);
  }
  return parts.join('');
}

function _renderProposalExplanation(text) {
  // Light-touch markdown: paragraphs (double newline → <p>), single
  // newlines preserved as soft breaks, no inline code / headings /
  // bullets in v1 to keep the renderer surface small. Trade-off:
  // generators that write prose-only Explanations work cleanly;
  // generators that want richer formatting can extend later.
  if (!text || typeof text !== 'string') return '';
  const paragraphs = text.trim().split(/\n\s*\n/);
  const html = paragraphs.map(p =>
    `<p style="margin:0 0 10px 0;line-height:1.55">${_mdInline(p).replace(/\n/g, '<br>')}</p>`
  ).join('');
  return `
    <div style="margin-bottom:16px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:8px">Explanation</div>
      <div style="font-size:0.85rem;color:var(--text);max-width:680px">${html}</div>
    </div>`;
}

function _renderProposalSummary(text) {
  if (!text) return '';
  return `
    <div class="stripe-card is-info" style="margin-bottom:14px;padding:10px 14px;background:color-mix(in srgb, var(--blue) 6%, transparent);border-radius:0 6px 6px 0">
      <div style="font-size:0.94rem;color:var(--text);line-height:1.5">${_mdInline(text)}</div>
    </div>`;
}

function renderProposalDetail(p) {
  // Phase A branch: new content shape → new five-section layout.
  // Pre-migration proposals (summary null) fall through to the
  // legacy renderer below unchanged.
  if (_isPhaseAContent(p)) {
    return _renderProposalDetailV2(p);
  }
  return _renderProposalDetailLegacy(p);
}

function _renderProposalDetailV2(p) {
  // Five-section layout per the spec. The bottom action bar reuses
  // the legacy logic (status-driven Snooze/Dismiss/Cancel/Mark
  // complete etc.) so dispatch + lifecycle stay consistent.
  const urgency = p.urgency || 'improvement';
  const urgColor = URGENCY_COLORS[urgency] || 'var(--text2)';
  const dim = p.dimension || '';
  const status = p.status || '';
  const audience = p.approval_audience || 'none';
  const actionKind = p._action_kind || (p.action && p.action.kind) || '';

  // Action button label: action_label override > actSemantics.verb >
  // dispatch verb when dispatch_target set.
  const actSemantics = _actSemantics(actionKind);
  const isManualKind = _MANUAL_COMPLETION_KINDS.includes(actionKind);
  const isExternalKind = _EXTERNAL_COMPLETION_KINDS.includes(actionKind);
  const dispatchTargetDetail = p.dispatch_target || null;
  const isDispatchable = !!dispatchTargetDetail;
  const dispatchResultDetail = (p.dispatch_state && p.dispatch_state.result) || null;
  const dispatchFailedDetail = status === 'failed_flagged'
    && dispatchResultDetail && dispatchResultDetail.outcome === 'failed';
  let dispatchVerb = '';
  if (isDispatchable) {
    if (dispatchTargetDetail === 'evo') dispatchVerb = 'Have evo fix this';
    else if (dispatchTargetDetail === 'forge') dispatchVerb = 'Have forge handle this';
    else dispatchVerb = `Send to ${dispatchTargetDetail}`;
  }
  // Per spec: dispatch_target proposals get the auto-derived verb;
  // action_label override only applies when no dispatch_target.
  const buttonVerb = isDispatchable
    ? dispatchVerb
    : (p.action_label || actSemantics.verb);

  let actionBar = '';
  let actionNotice = '';
  if (status === 'pending') {
    if (isDispatchable) {
      actionNotice = `Click "${buttonVerb}" to hand this off to ${dispatchTargetDetail}. You'll see the exact message before it goes; the target reports back when done.`;
      actionBar = `
        <button class="btn btn-sm" onclick="openDispatchConfirmModal('${escHtml(p.id)}')">${escHtml(buttonVerb)}</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterSnooze('${escHtml(p.id)}')">Snooze 1w</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
    } else if (_isCoalescedAdopt(p)) {
      // Coalesced AdoptModel group: the per-model Adopt controls live in the
      // "Models available to adopt" block above (a single Act would adopt only
      // the head and strand the rest). The action bar is just snooze/dismiss.
      actionNotice = 'Adopt models individually above, or use "Adopt all as dormant". Snooze or dismiss the card to set it aside without adopting.';
      actionBar = `
        <button class="btn btn-ghost btn-sm" onclick="arbiterSnooze('${escHtml(p.id)}')">Snooze 1w</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
    } else {
      actionNotice = actSemantics.notice;
      actionBar = `
        <button class="btn btn-sm" onclick="arbiterAct('${escHtml(p.id)}')">${escHtml(buttonVerb)}</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterSnooze('${escHtml(p.id)}')">Snooze 1w</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
    }
  } else if (status === 'dispatched') {
    const targetForNotice = (p.dispatch_state && p.dispatch_state.target) || dispatchTargetDetail || 'target';
    actionNotice = `Dispatched to ${targetForNotice}. The target will report back; you can cancel to bring the proposal back to the inbox.`;
    actionBar = `
      <button class="btn btn-ghost btn-sm" onclick="arbiterCancelDispatch('${escHtml(p.id)}')">Cancel dispatch</button>`;
  } else if (status === 'applied' && isManualKind) {
    actionNotice = 'This proposal has been accepted and is in process. Once you\'ve done the offline work the proposal describes, click Mark complete to close it out. Dismiss if you decided not to proceed after all.';
    actionBar = `
      <button class="btn btn-sm" onclick="arbiterComplete('${escHtml(p.id)}')">Mark complete</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  } else if (status === 'applied' && isExternalKind) {
    actionNotice = 'This proposal has been accepted and is being built by the forge. The proposal will close out automatically when the build completes (or fails). Dismiss only if you want to abandon the build.';
    actionBar = `
      <div style="font-size:0.85rem;padding:6px 10px;background:var(--bg3);border-radius:4px">${_forgeStatusBadge(p._forge_job)}</div>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  } else if (dispatchFailedDetail) {
    actionNotice = 'The dispatched target reported failure. Retry sends the same message again, or dismiss to take over manually.';
    actionBar = `
      <button class="btn btn-sm" onclick="arbiterRetryDispatch('${escHtml(p.id)}')">Retry</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  } else if (status === 'failed_flagged' || status === 'approved_human' || status === 'approved_auto') {
    actionBar = `
      <button class="btn btn-sm" onclick="arbiterRetry('${escHtml(p.id)}')">Retry</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  }

  // Details block — everything in the legacy layout, collapsed.
  // Reuses the legacy renderer's section helpers so the technical
  // depth stays intact; only the visual hierarchy changes.
  const actionRendered = renderProposalAction(p.action || {});
  const claimBlock = renderProposalClaim(p.claim);
  const revertBlock = renderProposalRevert(p.revert_on_failure);
  const riskBlock = renderProposalRisk(p.risk_tag);
  const attributionBlock = renderProposalRootCauseAttribution(p.provenance);
  const motivatingBlock = renderProposalMotivatingSignals(p.motivating_signals);
  const annotationsBlock = renderProposalAnnotations(p.guardian_annotations);
  const historyBlock = _renderProposalDetailHistory(p.history);
  const dispatchBlock = _renderProposalDispatchState(p);

  return `
    <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px;padding-right:32px">
      <h2 style="margin:0;font-size:1.05rem">${escHtml(p.human_title || p.admin_surface_summary || p.problem || '(no title)')}</h2>
      ${_subFindingsBadge(p)}
      <span class="badge" style="background:${urgColor};color:var(--on-accent);font-size:0.7rem;padding:1px 6px">${escHtml(urgency)}</span>
      ${dim ? `<span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);font-size:0.7rem;padding:1px 6px">${escHtml(dim)}</span>` : ''}
      <span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);font-size:0.7rem;padding:1px 6px">${escHtml(audience)}</span>
    </div>
    <div style="font-size:0.78rem;color:var(--text2);margin-bottom:14px">
      bot: ${escHtml(p.bot_id ? botLabel(p.bot_id) : '—')} · gen: ${escHtml(p.generator_id || '—')} · created ${escHtml(ago(p.created_at))}
    </div>

    ${_renderProposalSummary(p.summary)}
    ${_isCoalescedAdopt(p) ? _renderAdoptGroupBlock(p) : _renderSubFindingsBlock(p)}

    ${actionBar ? `
      <div style="margin-bottom:18px;padding:12px 14px;background:var(--bg2);border:1px solid var(--border);border-radius:6px">
        <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:8px">Proposed Action</div>
        ${actionNotice ? `<div style="font-size:0.82rem;color:var(--text2);margin-bottom:10px;line-height:1.5">${escHtml(actionNotice)}</div>` : ''}
        <div style="display:flex;gap:6px;flex-wrap:wrap">${actionBar}</div>
        ${_renderProposalFallbackPaths(p)}
      </div>` : ''}

    ${_renderProposalExplanation(p.explanation)}

    <details style="margin-top:18px;border-top:1px solid var(--border);padding-top:12px">
      <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:0.78rem;color:var(--text2);user-select:none;padding:4px 0">
        <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
        Show technical details
      </summary>
      <div style="margin-top:14px;padding-left:6px;border-left:2px solid var(--border)">
        <div style="font-size:0.72rem;color:var(--text3);margin-bottom:10px">
          action_kind: <code>${escHtml(actionKind)}</code> · status: <code>${escHtml(status)}</code> · id: <code>${escHtml(p.id)}</code>
        </div>
        ${p.problem ? `<div style="margin-bottom:14px"><div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Problem</div><div style="font-size:0.82rem;color:var(--text2)">${_mdInline(p.problem)}</div></div>` : ''}
        ${attributionBlock}
        <div style="margin-bottom:14px">
          <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Action — ${escHtml(actionKind)}</div>
          ${actionRendered}
        </div>
        ${dispatchBlock}
        ${claimBlock}
        ${revertBlock}
        ${riskBlock}
        ${motivatingBlock}
        ${annotationsBlock}
        ${historyBlock}
        ${_lineageDetail(p._lineage)}
        ${_renderRevisions(p)}
        ${_renderRefineForm(p)}
      </div>
    </details>
  `;
}

function _renderProposalDetailLegacy(p) {
  const urgency = p.urgency || 'improvement';
  const urgColor = URGENCY_COLORS[urgency] || 'var(--text2)';
  const dim = p.dimension || '';
  const status = p.status || '';
  const audience = p.approval_audience || 'none';
  const actionKind = p._action_kind || (p.action && p.action.kind) || '';
  const action = p.action || {};

  const actionRendered = renderProposalAction(action);
  const claimBlock = renderProposalClaim(p.claim);
  const revertBlock = renderProposalRevert(p.revert_on_failure);
  const riskBlock = renderProposalRisk(p.risk_tag);
  const attributionBlock = renderProposalRootCauseAttribution(p.provenance);
  const motivatingBlock = renderProposalMotivatingSignals(p.motivating_signals);
  const annotationsBlock = renderProposalAnnotations(p.guardian_annotations);
  const historyBlock = _renderProposalDetailHistory(p.history);
  const dispatchBlock = _renderProposalDispatchState(p);

  // Bottom action bar — same routing as the card buttons but operates from
  // the drawer so the operator can act after reviewing the payload.
  // Per-kind notice clarifies what Act will actually do — necessary
  // because Investigation/VetoAnnotation/MemoryCurate have non-obvious
  // semantics (Investigation Act = close with no automated change).
  const actSemantics = _actSemantics(actionKind);
  const isManualKind = _MANUAL_COMPLETION_KINDS.includes(actionKind);
  const isExternalKind = _EXTERNAL_COMPLETION_KINDS.includes(actionKind);
  // Slice 3 dispatch routing for the detail-modal action bar.
  const dispatchTargetDetail = p.dispatch_target || null;
  const isDispatchable = !!dispatchTargetDetail;
  const dispatchResultDetail = (p.dispatch_state && p.dispatch_state.result) || null;
  const dispatchFailedDetail = status === 'failed_flagged'
    && dispatchResultDetail && dispatchResultDetail.outcome === 'failed';
  let dispatchVerb = '';
  if (isDispatchable) {
    if (dispatchTargetDetail === 'evo') dispatchVerb = 'Have evo fix this';
    else if (dispatchTargetDetail === 'forge') dispatchVerb = 'Have forge handle this';
    else dispatchVerb = `Send to ${dispatchTargetDetail}`;
  }
  let actionBar = '';
  let actionNotice = '';
  if (status === 'pending') {
    if (isDispatchable) {
      actionNotice = `Click "${dispatchVerb}" to hand this off to ${dispatchTargetDetail}. You'll see the exact message before it goes; the target reports back when done.`;
      actionBar = `
        <button class="btn btn-sm" onclick="openDispatchConfirmModal('${escHtml(p.id)}')">${escHtml(dispatchVerb)}</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterSnooze('${escHtml(p.id)}')">Snooze 1w</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
    } else {
      actionNotice = actSemantics.notice;
      actionBar = `
        <button class="btn btn-sm" onclick="arbiterAct('${escHtml(p.id)}')">${escHtml(actSemantics.verb)}</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterSnooze('${escHtml(p.id)}')">Snooze 1w</button>
        <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
    }
  } else if (status === 'dispatched') {
    const targetForNotice = (p.dispatch_state && p.dispatch_state.target) || dispatchTargetDetail || 'target';
    actionNotice = `Dispatched to ${targetForNotice}. The target will report back; you can cancel to bring the proposal back to the inbox.`;
    actionBar = `
      <button class="btn btn-ghost btn-sm" onclick="arbiterCancelDispatch('${escHtml(p.id)}')">Cancel dispatch</button>`;
  } else if (status === 'applied' && isManualKind) {
    // In Process: applier already ran (Investigation no-op or
    // WorkflowInstruction file write). Operator does the offline work and
    // clicks Mark complete to close out.
    actionNotice = 'This proposal has been accepted and is in process. Once you\'ve done the offline work the proposal describes, click Mark complete to close it out. Dismiss if you decided not to proceed after all.';
    actionBar = `
      <button class="btn btn-sm" onclick="arbiterComplete('${escHtml(p.id)}')">Mark complete</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  } else if (status === 'applied' && isExternalKind) {
    // In Process (external): forge owns close-out. Operator can only
    // observe progress and dismiss as an escape hatch.
    actionNotice = 'This proposal has been accepted and is being built by the forge. The proposal will close out automatically when the build completes (or fails). Dismiss only if you want to abandon the build.';
    actionBar = `
      <div style="font-size:0.85rem;padding:6px 10px;background:var(--bg3);border-radius:4px">${_forgeStatusBadge(p._forge_job)}</div>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  } else if (dispatchFailedDetail) {
    actionNotice = 'The dispatched target reported failure. Retry sends the same message again, or dismiss to take over manually.';
    actionBar = `
      <button class="btn btn-sm" onclick="arbiterRetryDispatch('${escHtml(p.id)}')">Retry</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  } else if (status === 'failed_flagged' || status === 'approved_human' || status === 'approved_auto') {
    actionBar = `
      <button class="btn btn-sm" onclick="arbiterRetry('${escHtml(p.id)}')">Retry</button>
      <button class="btn btn-ghost btn-sm" onclick="arbiterDismiss('${escHtml(p.id)}')">Dismiss</button>`;
  }

  return `
    <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px;padding-right:32px">
      <h2 style="margin:0;font-size:1.05rem">${escHtml(p.human_title || p.admin_surface_summary || p.problem || '(no title)')}</h2>
      ${_subFindingsBadge(p)}
      <span class="badge" style="background:${urgColor};color:var(--on-accent);font-size:0.7rem;padding:1px 6px">${escHtml(urgency)}</span>
      ${dim ? `<span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);font-size:0.7rem;padding:1px 6px">${escHtml(dim)}</span>` : ''}
      <span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);font-size:0.7rem;padding:1px 6px">${escHtml(audience)}</span>
      <span class="badge" style="background:rgba(200,200,200,0.08);color:var(--text2);font-size:0.7rem;padding:1px 6px">${escHtml(actionKind)}</span>
      <span class="badge" style="background:rgba(200,200,200,0.08);color:var(--text2);font-size:0.7rem;padding:1px 6px">status: ${escHtml(status)}</span>
    </div>
    <div style="font-size:0.78rem;color:var(--text2);margin-bottom:14px">
      bot: ${escHtml(p.bot_id ? botLabel(p.bot_id) : '—')} · gen: ${escHtml(p.generator_id || '—')} · created ${escHtml(ago(p.created_at))} · id <code style="font-size:0.72rem">${escHtml(p.id)}</code>
    </div>
    ${p.problem ? `<div style="margin-bottom:14px"><div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Problem</div><div style="font-size:0.85rem;color:var(--text2)">${_mdInline(p.problem)}</div></div>` : ''}
    ${attributionBlock}
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Action — ${escHtml(actionKind)}</div>
      ${actionRendered}
    </div>
    ${dispatchBlock}
    ${claimBlock}
    ${revertBlock}
    ${riskBlock}
    ${motivatingBlock}
    ${annotationsBlock}
    ${historyBlock}
    ${_lineageDetail(p._lineage)}
    ${_renderRevisions(p)}
    ${_renderRefineForm(p)}
    ${actionBar ? `
      <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border)">
        ${actionNotice ? `<div style="font-size:0.78rem;color:var(--muted);margin-bottom:10px;line-height:1.5">${escHtml(actionNotice)}</div>` : ''}
        <div style="display:flex;gap:6px">${actionBar}</div>
      </div>` : ''}
  `;
}

// Refine is allowed in the inbox (pending) and in process (applied). Pod-wide
// proposals (bot_id === "<pod>" sentinel) are excluded — the refine endpoint
// refuses them because there's no bot account to bill the LLM call against.
function _isRefinable(p) {
  if (!p.bot_id || p.bot_id === '<pod>') return false;
  return p.status === 'pending' || p.status === 'applied';
}

function _renderRevisions(p) {
  const revs = p.revisions || [];
  if (!revs.length) return '';
  // Newest first for display.
  const ordered = [...revs].reverse();
  const items = ordered.map((r, i) => {
    const idx = revs.length - i;
    return `<li style="margin-bottom:10px;font-size:0.82rem">
      <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:2px">
        <strong style="font-size:0.78rem">v${idx} → v${idx + 1}</strong>
        <span class="subtle" style="font-size:0.7rem">${ago(r.at)}</span>
        <span class="subtle" style="font-size:0.7rem">· ${escHtml(r.actor || '')}</span>
      </div>
      <div style="font-size:0.78rem;color:var(--text2);font-style:italic;margin-bottom:4px">"${escHtml(r.feedback || '')}"</div>
      <div style="font-size:0.74rem;color:var(--text3)">
        Prior: ${escHtml((r.prior_problem || '').slice(0, 140))}
      </div>
    </li>`;
  }).join('');
  return `
    <div class="card" style="padding:10px 14px;margin-top:10px">
      <div class="card-title" style="margin-bottom:6px">Iteration history (${revs.length})</div>
      <div class="subtle" style="font-size:0.78rem;margin-bottom:8px">
        Each entry is a refine cycle: the operator's feedback and the prior text it replaced. Bot's LLM did the rewrite.
      </div>
      <ul style="margin:0;padding-left:18px;list-style:none">${items}</ul>
    </div>`;
}

function _renderRefineForm(p) {
  if (!_isRefinable(p)) return '';
  const inputId = `refine-input-${p.id}`;
  const statusId = `refine-status-${p.id}`;
  return `
    <div class="card" style="padding:10px 14px;margin-top:10px">
      <div class="card-title" style="margin-bottom:6px">Refine</div>
      <div class="subtle" style="font-size:0.78rem;margin-bottom:8px">
        Tell ${escHtml(botLabel(p.bot_id))} what to change about this proposal. Its LLM (using the bot's own API account) will rewrite the prose. Structural fields stay fixed so the proposal's identity is preserved.
      </div>
      <textarea id="${inputId}" rows="3" placeholder="e.g. \&quot;less aggressive\&quot;, \&quot;explain why this matters\&quot;, \&quot;mention the cron schedule too\&quot;" style="width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:8px;font-family:inherit;font-size:0.82rem;color:var(--text);box-sizing:border-box"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-sm" onclick="arbiterRefine('${escHtml(p.id)}')">Send refine</button>
        <span id="${statusId}" class="subtle" style="font-size:0.74rem"></span>
      </div>
    </div>`;
}

async function arbiterRefine(id) {
  const inputEl = document.getElementById(`refine-input-${id}`);
  const statusEl = document.getElementById(`refine-status-${id}`);
  if (!inputEl || !statusEl) return;
  const feedback = (inputEl.value || '').trim();
  if (!feedback) {
    statusEl.style.color = '#ff8c42';
    statusEl.textContent = 'Enter feedback first.';
    return;
  }
  statusEl.style.color = 'var(--muted)';
  statusEl.textContent = 'Sending to bot…';
  try {
    const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/refine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ feedback }),
    });
    const j = await r.json();
    if (!j.ok) {
      statusEl.style.color = '#ff8c42';
      statusEl.textContent = `Error: ${j.error || 'unknown'}`;
      return;
    }
    statusEl.style.color = 'var(--green)';
    statusEl.textContent = `Revised. Now on revision ${j.revision_count}.`;
    // Refresh the modal so the operator sees the new text + revision entry.
    setTimeout(() => {
      loadArbiterProposals().then(() => {
        // Re-open the same proposal's detail to show the revised version.
        if (window._proposalsById && window._proposalsById.has(id)) {
          openProposalDetail(id);
        }
      });
    }, 600);
  } catch (e) {
    statusEl.style.color = '#ff8c42';
    statusEl.textContent = `Request failed: ${e}`;
  }
}

// Per-kind semantics: what does "Act" actually mean? The button label and
// notice come from this map so the operator isn't surprised. Investigation
// is the most important case — Act closes the proposal with no automated
// change; the operator handles whatever the context describes.
function _actSemantics(kind) {
  const map = {
    Investigation:    { verb: 'Take this on', notice: 'This is an investigation — there is no automated action. Take this on moves the proposal to In Process; do whatever the context above describes, then click Mark complete to close it out. Dismiss if it turned out to be a non-issue.' },
    VetoAnnotation:   { verb: 'Acknowledge',   notice: 'A guardian raised a concern. Act records that you saw it; nothing else changes.' },
    SoulEdit:         { verb: 'Apply edit',    notice: 'Act writes the content above into the bot\'s SOUL.md or AGENTS.md. The change is reversible via the revert plan.' },
    AgentsAppend:     { verb: 'Append',        notice: 'Act appends the content above to AGENTS.md. Additive only — does not replace existing sections.' },
    WorkflowInstruction: { verb: 'Take this on', notice: 'A markdown instruction file is written into the bot\'s workspace at the path above. The proposal moves to In Process; do whatever the file describes, then click Mark complete to close it out.' },
    MemoryCurate:     { verb: 'Apply',         notice: 'Act prunes or rewrites entries in the bot\'s memory file as described above.' },
    ManifestUpdate:   { verb: 'Apply',         notice: 'Act patches the named app\'s manifest with the fields above.' },
    InstallApp:       { verb: 'Install',       notice: 'Act installs the named gallery app for the bot.' },
    DeprecateApp:     { verb: 'Deprecate',     notice: 'Act marks the app as deprecated. The bot stops surfacing it; data is preserved.' },
    ConfigPatch:      { verb: 'Apply patch',   notice: 'Act writes the value above to the named target_path in the bot\'s openclaw.json. Reversible via the revert plan.' },
    TierAdjustment:   { verb: 'Adjust tier',   notice: 'Act rewrites the routing tier for the named target_class. The bot will use the new tier on its next session.' },
    BuildApp:         { verb: 'Take this on',  notice: 'Act dispatches the manifest to the forge. The proposal moves to In Process; the forge builds and tests the app over the next few minutes to hours. The proposal closes automatically when the build resolves — succeeded if the forge completes cleanly, failed if it errors out.' },
    ThrottleGenerator:{ verb: 'Throttle',      notice: 'Act reduces the named generator\'s capacity (cadence / budget / confidence). Auto-reverts after the configured days if set.' },
    PauseGenerator:   { verb: 'Pause',         notice: 'Act sets the named generator to paused — it stops emitting proposals until resumed.' },
  };
  return map[kind] || { verb: 'Act', notice: '' };
}

// Per-kind renderer for the action payload. Falls through to a generic
// key/value table for kinds that don't have a custom renderer yet.
function renderProposalAction(a) {
  if (!a || typeof a !== 'object') {
    return `<div style="font-size:0.85rem;color:var(--muted)">(no action payload)</div>`;
  }
  const kind = a.kind || '';
  const rest = Object.fromEntries(Object.entries(a).filter(([k]) => k !== 'kind'));

  // Code-block kinds: payload contains a content/value field worth rendering verbatim
  if (kind === 'ConfigPatch') {
    return _kvCard([
      ['target_path', a.target_path || ''],
      ['operation', a.operation || ''],
      ['value', _codeBlock(a.value)],
    ]);
  }
  if (kind === 'WorkflowInstruction') {
    return _kvCard([
      ['bot_id', a.bot_id || ''],
      ['path', a.path || ''],
      ['content', _codeBlock(a.content || '', 'text')],
    ]);
  }
  if (kind === 'AgentsAppend') {
    return _kvCard([
      ['bot_id', a.bot_id || ''],
      ['section', a.section || ''],
      ['content', _codeBlock(a.content || '', 'markdown')],
    ]);
  }
  if (kind === 'SoulEdit') {
    return _kvCard([
      ['bot_id', a.bot_id || ''],
      ['target', a.target || ''],
      ['operation', a.operation || ''],
      ['anchor', a.anchor || '(none)'],
      ['content', _codeBlock(a.content || '', 'markdown')],
      ['rationale', escHtml(a.rationale || '')],
    ]);
  }
  if (kind === 'TierAdjustment') {
    return _kvCard([
      ['bot_id', a.bot_id || ''],
      ['target_class', a.target_class || ''],
      ['new_tier', `<code>${escHtml(a.new_tier || '')}</code>`],
    ]);
  }
  if (kind === 'Investigation') {
    return `<div style="padding:10px 12px;background:color-mix(in srgb, var(--blue) 10%, transparent);border-left:3px solid var(--blue);border-radius:3px;font-size:0.85rem;white-space:pre-wrap">${escHtml(a.context || '(no context)')}</div>`;
  }
  if (kind === 'ManifestUpdate') {
    return _kvCard([
      ['app_id', a.app_id || ''],
      ['operation', a.operation || ''],
      ['fields', _codeBlock(a.fields || {})],
    ]);
  }
  if (kind === 'MemoryCurate') {
    return _kvCard([
      ['bot_id', a.bot_id || ''],
      ['operation', a.operation || ''],
      ['target_selector', a.target_selector || '(all)'],
      ['replacement', _codeBlock(a.replacement || '', 'text')],
    ]);
  }
  if (kind === 'InstallApp') {
    return _kvCard([
      ['app_id', a.app_id || ''],
      ['source', a.source || 'gallery'],
    ]);
  }
  if (kind === 'DeprecateApp') {
    return _kvCard([
      ['app_id', a.app_id || ''],
      ['reason', a.reason || ''],
      ['days_inactive', String(a.days_inactive ?? 0)],
      ['replacement_app_id', a.replacement_app_id || '(none)'],
    ]);
  }
  if (kind === 'ThrottleGenerator') {
    return _kvCard([
      ['generator_id', a.generator_id || ''],
      ['throttle_type', a.throttle_type || ''],
      ['new_value', `<code>${escHtml(String(a.new_value ?? ''))}</code>`],
      ['reason', escHtml(a.reason || '')],
      ['revert_after_days', a.revert_after_days != null ? String(a.revert_after_days) : '(no auto-revert)'],
    ]);
  }
  if (kind === 'PauseGenerator') {
    return _kvCard([
      ['generator_id', a.generator_id || ''],
      ['reason', escHtml(a.reason || '')],
      ['resume_after_days', a.resume_after_days != null ? String(a.resume_after_days) : '(no auto-resume)'],
    ]);
  }
  if (kind === 'VetoAnnotation') {
    return _kvCard([
      ['severity', a.severity || 'medium'],
      ['reason', escHtml(a.reason || '')],
    ]);
  }

  // Generic fallback: dump every non-kind field
  const rows = Object.entries(rest).map(([k, v]) => [k, _codeBlock(v)]);
  return _kvCard(rows.length ? rows : [['(empty payload)', '']]);
}

function _kvCard(rows) {
  const html = rows.map(([k, v]) => `
    <tr>
      <td style="padding:6px 12px 6px 0;color:var(--text2);font-size:0.78rem;vertical-align:top;white-space:nowrap"><code>${escHtml(k)}</code></td>
      <td style="padding:6px 0;font-size:0.85rem;color:var(--text2);width:100%">${v}</td>
    </tr>`).join('');
  return `<div style="padding:8px 12px;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:4px"><table style="width:100%;border-collapse:collapse">${html}</table></div>`;
}

function _codeBlock(value, lang = 'json') {
  if (value == null) return `<span style="color:var(--text3)">null</span>`;
  let text;
  if (typeof value === 'string') {
    text = value;
  } else {
    try { text = JSON.stringify(value, null, 2); }
    catch (e) { text = String(value); }
  }
  if (text.length > 4000) text = text.slice(0, 4000) + '\n… (truncated)';
  return `<pre style="margin:0;padding:8px 10px;background:rgba(0,0,0,0.35);border-radius:3px;font-size:0.78rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto">${escHtml(text)}</pre>`;
}

function renderProposalClaim(claim) {
  if (!claim) return '';
  const arrow = claim.direction === 'down' ? '↓' : claim.direction === 'up' ? '↑' : '=';
  return `
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Claim — measurable promise</div>
      ${_kvCard([
        ['metric', `<code>${escHtml(claim.metric || '?')}</code>`],
        ['direction', `${arrow} ${escHtml(claim.direction || '')}`],
        ['magnitude', String(claim.magnitude ?? '')],
        ['window_days', String(claim.window_days ?? '')],
        ['fallback', escHtml(claim.fallback || '')],
      ])}
    </div>`;
}

function renderProposalRevert(revert) {
  if (!revert) return '';
  return `
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Revert plan — runs on verify failure</div>
      ${_kvCard([
        ['kind', escHtml(revert.kind || '')],
        ['snapshot', _codeBlock(revert.snapshot || revert)],
      ])}
    </div>`;
}

function renderProposalRisk(risk) {
  if (!risk) return '';
  const touches = Array.isArray(risk.touches) ? risk.touches.join(', ') : '';
  return `
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Risk</div>
      ${_kvCard([
        ['blast_radius', escHtml(risk.blast_radius || '')],
        ['reversibility', escHtml(risk.reversibility || '')],
        ['touches', escHtml(touches || '(none)')],
      ])}
    </div>`;
}

// Render the root_cause_attribution block from Provenance.signals.
// Spec: internal/spec-smarter-generators-2026-05-28.md §"What 'propose' produces".
// Only the bloat_investigator (and future investigating generators)
// populate this block; returns empty string when absent so existing
// proposals render unchanged.
function renderProposalRootCauseAttribution(provenance) {
  if (!provenance || typeof provenance !== 'object') return '';
  const sigs = provenance.signals || {};
  const rca = sigs.root_cause_attribution;
  if (!rca || typeof rca !== 'object') return '';

  const causeKey = rca.cause_key || 'unknown';
  const headline = rca.headline || '';
  const confidence = typeof rca.confidence === 'number' ? rca.confidence : null;
  const primaryTarget = rca.primary_target || '';
  const evidence = (rca.evidence && typeof rca.evidence === 'object') ? rca.evidence : {};
  const sigTypes = Array.isArray(sigs.primary_signal_types) ? sigs.primary_signal_types : [];
  const topFiles = Array.isArray(sigs.top_files_kb) ? sigs.top_files_kb : [];
  const history = (sigs.history && typeof sigs.history === 'object') ? sigs.history : null;

  // Cause-key badge color reflects ambiguity. Ambiguous = muted gray
  // (caller doesn't know yet); named cause = blue accent.
  const isAmbiguous = causeKey === 'ambiguous';
  const causeBadgeColor = isAmbiguous ? 'var(--text2)' : 'var(--blue)';
  const causeBadgeBg = isAmbiguous
    ? 'rgba(200,200,200,0.1)'
    : 'rgba(127,200,255,0.12)';

  const confidenceLabel = confidence === null
    ? ''
    : confidence >= 0.8 ? 'high'
    : confidence >= 0.5 ? 'medium'
    : 'low';
  const confidencePct = confidence === null ? '' : `${Math.round(confidence * 100)}%`;

  const sigChips = sigTypes.map(t =>
    `<span class="badge" style="background:rgba(127,200,255,0.08);color:var(--blue);font-size:0.68rem;padding:1px 6px;margin-right:4px">${escHtml(t)}</span>`
  ).join('');

  const topFilesRows = topFiles.slice(0, 5).map(f => {
    const path = (f && f.path) || '';
    const kb = (f && typeof f.size_kb === 'number') ? f.size_kb : 0;
    return `<li style="font-size:0.78rem;color:var(--text2);font-family:monospace;margin-bottom:2px"><code>${escHtml(path)}</code> · ${kb} KB</li>`;
  }).join('');

  const historyLine = history && history.total
    ? `<div style="font-size:0.78rem;color:var(--muted);margin-top:6px">
         Prior proposals for this cause: <strong>${history.total}</strong>
         (declined ${history.declined || 0}, approved ${history.approved || 0})
         ${history.most_recent_status ? `· most recent <span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);font-size:0.68rem;padding:1px 5px">${escHtml(history.most_recent_status)}</span>` : ''}
       </div>`
    : '';

  // Evidence is freeform per attribution rule. Render flat key/value pairs
  // when the evidence dict is shallow; collapse arrays of strings to
  // comma-joined; skip dicts/objects beyond depth 1 to keep the block scannable.
  const evidenceRows = Object.entries(evidence).map(([k, v]) => {
    let display;
    if (Array.isArray(v)) {
      display = v.map(String).join(', ');
    } else if (v === null || v === undefined) {
      display = '—';
    } else if (typeof v === 'object') {
      // Object — skip rather than dump JSON. The proposal body has the
      // detail; this block stays scannable.
      return '';
    } else {
      display = String(v);
    }
    return `<div style="display:flex;gap:8px;font-size:0.78rem;margin-bottom:2px">
              <span style="color:var(--muted);min-width:160px;font-family:monospace">${escHtml(k)}</span>
              <span style="color:var(--text2)">${escHtml(display)}</span>
            </div>`;
  }).filter(Boolean).join('');

  return `
    <div style="margin-bottom:14px;padding:10px 12px;background:rgba(127,200,255,0.04);border-left:2px solid ${causeBadgeColor};border-radius:3px">
      <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:6px">
        <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em">Root-cause attribution</div>
        <span class="badge" style="background:${causeBadgeBg};color:${causeBadgeColor};font-size:0.7rem;padding:1px 6px;font-family:monospace">${escHtml(causeKey)}</span>
        ${confidence !== null ? `<span class="badge" style="background:rgba(200,200,200,0.08);color:var(--text2);font-size:0.68rem;padding:1px 5px">${escHtml(confidenceLabel)} · ${confidencePct}</span>` : ''}
      </div>
      ${headline ? `<div style="font-size:0.85rem;color:var(--text2);margin-bottom:8px">${escHtml(headline)}</div>` : ''}
      ${primaryTarget ? `<div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">Primary target: <code style="font-size:0.78rem">${escHtml(primaryTarget)}</code></div>` : ''}
      ${sigChips ? `<div style="margin-bottom:8px">${sigChips}</div>` : ''}
      ${evidenceRows ? `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed rgba(255,255,255,0.06)">${evidenceRows}</div>` : ''}
      ${topFilesRows ? `<div style="margin-top:8px">
                          <div style="font-size:0.68rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:3px">Largest workspace files</div>
                          <ul style="margin:0;padding-left:18px">${topFilesRows}</ul>
                        </div>` : ''}
      ${historyLine}
    </div>`;
}

function renderProposalMotivatingSignals(signals) {
  if (!Array.isArray(signals) || !signals.length) return '';
  const items = signals.map(sid =>
    `<li style="font-family:monospace;font-size:0.78rem;margin-bottom:2px"><code>${escHtml(sid)}</code></li>`
  ).join('');
  return `
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Motivating signals</div>
      <ul style="margin:0;padding-left:20px">${items}</ul>
    </div>`;
}

function renderProposalAnnotations(annotations) {
  if (!Array.isArray(annotations) || !annotations.length) return '';
  const items = annotations.map(a => `
    <li style="margin-bottom:6px;font-size:0.82rem">
      <span class="badge" style="background:rgba(255,140,66,0.15);color:#ff8c42;font-size:0.7rem;padding:1px 6px">${escHtml(a.guardian_id || '')} · ${escHtml(a.severity || 'low')}</span>
      ${escHtml(a.reason || '')}
    </li>`).join('');
  return `
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Guardian annotations</div>
      <ul style="margin:0;padding-left:18px">${items}</ul>
    </div>`;
}

// Modal-only history block. Named with the underscore-prefix convention
// other detail-modal helpers use (_renderRefineForm, _renderRevisions,
// _lineageDetail) so it doesn't collide with the page-level
// renderProposalHistory(all) below — JS function hoisting silently picks
// the later declaration, and the resulting `undefined` return value
// stringifies into a literal "undefined" heading in the modal output.
// Slice 3 (2026-06-04): dispatch_state info block for the detail modal.
// Renders the verbatim dispatch_message, the in-flight badge, and (when
// available) the target's result message + applied_changes. Hidden when
// the proposal has no dispatch_state.
function _renderProposalDispatchState(p) {
  if (!p.dispatch_state) return '';
  const ds = p.dispatch_state;
  const target = ds.target || '—';
  const dispatchedAt = ds.dispatched_at ? ago(ds.dispatched_at) : '—';
  const cancelled = ds.cancelled_at
    ? `<div style="font-size:0.78rem;color:var(--text3);margin-top:6px">Cancelled ${escHtml(ago(ds.cancelled_at))}.</div>`
    : '';
  const result = ds.result || null;
  let resultBlock = '';
  if (result) {
    const outcomeColor = result.outcome === 'failed' ? 'var(--red)'
      : (result.outcome === 'applied' ? 'var(--green)' : 'var(--blue)');
    const changesRows = (result.applied_changes || []).map(c => {
      const summary = typeof c === 'string' ? c : (c.summary || JSON.stringify(c));
      return `<li style="margin-bottom:2px;font-size:0.8rem;color:var(--text2)">${escHtml(summary)}</li>`;
    }).join('');
    resultBlock = `
      <div style="margin-top:10px;padding:8px 10px;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:4px">
        <div style="font-size:0.78rem;font-weight:600;margin-bottom:4px"><span style="color:${outcomeColor}">${escHtml(result.outcome)}</span> · reported ${escHtml(result.reported_at ? ago(result.reported_at) : '—')}</div>
        ${result.message ? `<div style="font-size:0.82rem;color:var(--text2);white-space:pre-wrap;word-break:break-word">${escHtml(result.message)}</div>` : ''}
        ${changesRows ? `<div style="font-size:0.74rem;color:var(--text3);margin-top:6px;text-transform:uppercase;letter-spacing:0.05em">Applied changes</div><ul style="margin:4px 0 0;padding-left:18px">${changesRows}</ul>` : ''}
      </div>`;
  }
  return `
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">Dispatch</div>
      <div style="font-size:0.82rem;color:var(--text2)">
        ⚙ Dispatched to <b>${escHtml(target)}</b> · ${escHtml(dispatchedAt)}
      </div>
      ${ds.message ? `<div style="margin-top:6px;font-size:0.82rem;color:var(--text2);background:var(--bg3, rgba(255,255,255,0.04));border-left:3px solid var(--blue);padding:8px 10px;border-radius:3px;white-space:pre-wrap;word-break:break-word">${escHtml(ds.message)}</div>` : ''}
      ${resultBlock}
      ${cancelled}
    </div>`;
}

function _renderProposalDetailHistory(history) {
  if (!Array.isArray(history) || !history.length) return '';
  const rows = history.map(h => `
    <tr>
      <td style="padding:3px 10px 3px 0;color:var(--text2);font-size:0.76rem;white-space:nowrap">${escHtml(ago(h.at))}</td>
      <td style="padding:3px 10px;font-size:0.78rem"><code>${escHtml(h.from_status || '?')} → ${escHtml(h.to_status || '?')}</code></td>
      <td style="padding:3px 10px;font-size:0.78rem;color:var(--text2)">${escHtml(h.actor || '')}</td>
      <td style="padding:3px 0;font-size:0.78rem;color:var(--text2);width:100%">${escHtml(h.reason || '')}</td>
    </tr>`).join('');
  return `
    <div style="margin-bottom:14px">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text2);letter-spacing:0.05em;margin-bottom:4px">History</div>
      <div style="padding:8px 12px;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:4px">
        <table style="width:100%;border-collapse:collapse">${rows}</table>
      </div>
    </div>`;
}

async function arbiterDismiss(id) {
  if (!await confirmModal({body: 'Dismiss this proposal?', danger: true})) return;
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/dismiss`, { method: 'POST' });
  const j = await r.json();
  if (!j.ok) toast('Dismiss failed: ' + (j.error || 'unknown'), 'err');
  closeProposalDetail();
  loadArbiterProposals();
  loadArbiterRateLimit();
}

// Reject was a separate "not a fit" signal but the calibration loop that
// would consume it was never wired (TrackRecord.proposals_rejected_human
// is declared but never incremented). Dismiss is now the single
// queue-clearing action; the *fact* of dismissal is itself signal we can
// mine retroactively if a calibration loop catches up later.

async function arbiterSnooze(id) {
  const r = await fetch(`/api/arbiter/proposals/${encodeURIComponent(id)}/snooze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ duration: '1w' }),
  });
  const j = await r.json();
  if (!j.ok) toast('Snooze failed: ' + (j.error || 'unknown'), 'err');
  closeProposalDetail();
  loadArbiterProposals();
  loadArbiterRateLimit();
}

async function loadArbiterRateLimit() {
  const banner = document.getElementById('arbiter-rate-banner');
  const text = document.getElementById('arbiter-rate-text');
  if (!banner || !text) return;
  try {
    // The banner lives on the Self-Improvement (Recommendations) page,
    // which renders only proposals with charter surface=improvement
    // (other surfaces route to Alerts). Pass the same filter to the
    // API so the numbers describe what's rendered below — pre-fix,
    // the banner could claim "N ready to surface" while showing zero
    // rows because the other surfaces were on the Alerts page.
    const r = await fetch('/api/arbiter/rate-limit-state?surface=improvement');
    const j = await r.json();
    if (!j.ok) { banner.style.display = 'none'; return; }
    // Three numbers, each a statement about the surface-filtered
    // queue:
    //   arrivals_this_week  — newly added to pending this ISO week
    //   ready_to_surface_now — first-cap-slice of current pending
    //   held                — pending above the cap
    // Empty queue → "0/7 new · 0 ready · 0 held". The "/7" is the
    // pod-wide weekly arrival cap (informational; the underlying
    // rate limit uses 7 as a display-batch ceiling too).
    const arrivals = j.arrivals_this_week ?? j.surfaced_this_week ?? 0;
    const ready = j.ready_to_surface_now ?? j.surfaceable_now ?? 0;
    const cap = j.cap || 7;
    const held = (j.held || []).length;
    text.innerHTML = `<b>${arrivals}/${cap}</b> new this week · <b>${ready}</b> ready to surface · <b>${held}</b> held (will surface next week)`;
    banner.style.display = '';
  } catch (e) {
    banner.style.display = 'none';
  }
}

// ── Bot setup helpers (rendered inside Config → Bot subtab) ─────────

const _ARCHETYPE_LABELS = {
  primary: 'Just me (this is my personal bot)',
  single_user_member: 'A family member or single user',
  multi_user_member: 'A small team or shared bot',
};

const _CADENCE_LABELS = {
  as_it_arises: 'As it arises (default)',
  daily: 'Daily — show up to 7 at once',
  weekly: 'Weekly — at most 1 non-urgent item',
  urgent_only: 'Only urgent items (security / operational)',
};

function renderBotSetup(s, targetId) {
  const setupBody = document.getElementById(targetId || 'config-bot-setup-body');
  const archetypes = s.archetypes || [];
  const cadences = s.cadences || [];
  const archetype = s.archetype || '';
  const cadence = s.surfacing_cadence || '';
  const tz = s.timezone || '';
  const podTz = s.pod_timezone || '';

  const archetypeOptions = ['<option value="">(unset — defaults to Family member)</option>']
    .concat(archetypes.map(a =>
      `<option value="${escHtml(a)}"${a === archetype ? ' selected' : ''}>${escHtml(_ARCHETYPE_LABELS[a] || a)}</option>`
    )).join('');

  const cadenceOptions = ['<option value="">(unset — As it arises)</option>']
    .concat(cadences.map(c =>
      `<option value="${escHtml(c)}"${c === cadence ? ' selected' : ''}>${escHtml(_CADENCE_LABELS[c] || c)}</option>`
    )).join('');

  const tzPlaceholder = podTz ? `pod default: ${podTz}` : 'IANA name, e.g. America/Los_Angeles';
  // Cost/cap-related variables (monthly_cap_usd, daily_warn_usd, …)
  // are no longer needed here — the per-bot Cost & Caps editor moved to
  // Cost Optimization (per-bot tab). This card just shows a deep link.

  setupBody.innerHTML = `
    <div class="card" style="padding:14px 16px">
      <div class="card-title" style="margin-bottom:10px">Bot setup</div>
      <div style="display:grid;grid-template-columns:max-content 1fr;gap:10px 14px;align-items:center;font-size:0.85rem">

        <label for="bot-setup-archetype" style="color:var(--text2)">Who uses this bot?</label>
        <select id="bot-setup-archetype" class="input-w-lg">${archetypeOptions}</select>

        <label for="bot-setup-cadence" style="color:var(--text2)">How often to hear from me?</label>
        <select id="bot-setup-cadence" class="input-w-lg">${cadenceOptions}</select>

        <label for="bot-setup-tz" style="color:var(--text2)">Timezone</label>
        <div>
          <input id="bot-setup-tz" type="text" placeholder="${escHtml(tzPlaceholder)}" value="${escHtml(tz)}" style="width:240px">
          <span class="subtle" style="margin-left:8px;font-size:0.78rem">leave blank to use the pod-wide default</span>
        </div>

      </div>
      <div style="margin-top:14px;display:flex;gap:8px;align-items:center">
        <button class="btn" onclick="saveBotSetup('${escHtml(s.bot_id)}')">Save</button>
        <span id="bot-setup-status" class="subtle" style="font-size:0.78rem"></span>
      </div>
    </div>

    <div class="card" id="bot-cost-caps-card" style="padding:14px 16px;margin-top:14px">
      <div class="card-title" style="margin-bottom:4px">Cost &amp; caps</div>
      <div class="subtle" style="font-size:0.85rem;margin-bottom:14px;line-height:1.5">
        Per-bot cost caps (monthly tolerance, daily/weekly warn, L1/L2 breakers, per-session cap, prompt-cache TTL) now live on the
        <a href="#" onclick="_jumpToCostOptForBot('${escHtml(s.bot_id)}'); return false;" style="color:var(--accent)">Cost Optimization</a>
        page. The canonical Cost &amp; Caps matrix on that bot's tab edits the same fields with chip-based <em>Default · Off · Custom</em> tristates and shows what the pod default is for each row.
      </div>
      <button class="btn" onclick="_jumpToCostOptForBot('${escHtml(s.bot_id)}')">Configure cost &amp; caps →</button>
    </div>`;
}

// Switch to the Cost Optimization page and select a specific bot's tab.
// Used by the Settings page deep link that replaces the per-bot Cost &
// Caps form. The nav() click runs synchronously but the page's bot tabs
// render on the first paint of the cost-measures page, so we re-assert
// the selection after a short tick to make sure _cmSelectBot finds the
// .subtab element it needs to mark active.
function _jumpToCostOptForBot(botId) {
  const navEl = document.querySelector('.nav-item[data-page="cost-measures"]');
  if (navEl) nav(navEl);
  if (!botId) return;
  // First-paint assertion + one retry. The cost-measures page lazy-loads
  // its bot tab list; one short retry catches the race without a busy loop.
  let tries = 0;
  const apply = () => {
    if (typeof _cmSelectBot !== 'function') {
      if (tries++ < 10) setTimeout(apply, 50);
      return;
    }
    const tabExists = document.querySelector(`#cm-bot-tabs .subtab[data-bot="${CSS.escape(botId)}"]`);
    if (!tabExists && tries++ < 10) {
      setTimeout(apply, 50);
      return;
    }
    _cmSelectBot(botId);
  };
  apply();
}

async function saveBotSetup(botId) {
  // After the Cost & caps card on Settings was replaced by a deep link to
  // Cost Optimization, this function only handles the "Bot setup" card:
  // archetype, surfacing cadence, timezone. Per-bot cost caps and prompt-
  // cache TTL are edited in the Cost Optimization matrices and committed
  // there; we don't send those fields from this surface anymore.
  const statusEl = document.getElementById('bot-setup-status');
  function setStatus(color, text) {
    if (statusEl) { statusEl.style.color = color; statusEl.textContent = text; }
  }

  const archetypeEl = document.getElementById('bot-setup-archetype');
  const cadenceEl = document.getElementById('bot-setup-cadence');
  const tzEl = document.getElementById('bot-setup-tz');
  if (!archetypeEl || !cadenceEl || !tzEl) return;

  const body = {
    archetype: archetypeEl.value || null,
    surfacing_cadence: cadenceEl.value || null,
  };
  const tzRaw = tzEl.value.trim();
  body.timezone = tzRaw === '' ? null : tzRaw;

  setStatus('var(--muted)', 'Saving…');
  try {
    const r = await fetch(`/api/arbiter/bot-setup/${encodeURIComponent(botId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!j.ok) {
      // Phase 5 ladder validation: the backend returns
      // kind=remediation_ladder_inverted with a multi-line error string
      // identifying the inverted pair. Make the error stand out so the
      // operator sees which two thresholds to swap.
      if (j.kind === 'remediation_ladder_inverted') {
        setStatus('#ff8c42', `Ladder inverted: ${j.error}`);
      } else {
        setStatus('#ff8c42', `Error: ${j.error || 'unknown'}`);
      }
      return;
    }
    setStatus('var(--green)', 'Saved.');
    // Reload to show canonical state
    setTimeout(loadConfigBot, 400);
  } catch (e) {
    setStatus('#ff8c42', `Request failed: ${e}`);
  }
}

function renderProfile(p, targetId) {
  const body = document.getElementById(targetId || 'config-bot-profile-body');
  const heading = `<div class="card-title" style="margin:18px 0 10px">User profile</div>`;
  const status = p.has_content
    ? `<div class="subtle">User has a profile.</div>`
    : `<div class="subtle">No profile yet.</div>`;
  const note = `<div class="subtle" style="margin-top:6px;font-size:0.78rem">Contents are private to the bot and not surfaced here.</div>`;

  body.innerHTML = `
    <div class="card" style="padding:12px 14px">
      ${heading}
      ${status}
      ${note}
    </div>`;
}

// PR H: Estimated-weekly-savings chip on the proposal card. Generators
// that have a clean money model (today: cache_ttl_tuner's cacheRetention
// flip) populate Proposal.estimated_savings_usd. The chip is shown when
// the estimate is above the $0.50 display floor — small estimates are
// noisier than they are informative. Tooltip explains the heuristic so
// the operator knows this is an estimate, not a measurement.
const _SAVINGS_DISPLAY_FLOOR_USD = 0.5;

function _renderSavingsChip(estimatedSavingsUsd) {
  if (estimatedSavingsUsd == null) return '';
  const raw = Number(estimatedSavingsUsd);
  if (!isFinite(raw) || raw <= _SAVINGS_DISPLAY_FLOOR_USD) return '';
  // Round to nearest dollar for the chip itself. The tooltip carries the
  // un-rounded estimate for operators who want the exact number.
  const display = Math.max(1, Math.round(raw));
  const tooltip = (
    'Estimated weekly savings if this proposal is applied. '
    + 'Heuristic based on the bot\'s 7d cost × cache-invalidation rate × '
    + 'an assumed cacheWrite share + remediable fraction. '
    + 'Treat as an order-of-magnitude estimate, not a forecast. '
    + `Raw estimate: $${raw.toFixed(2)}/wk.`
  );
  return `<span class="badge" title="${escHtml(tooltip)}" style="background:rgba(127,255,158,0.12);color:var(--green);font-size:0.7rem;padding:1px 6px">est. ~$${display}/wk</span>`;
}

function scoreBreakdownChip(p) {
  const bd = p._score_breakdown;
  const rank = p._rank;
  if (!bd) return '';
  const score = bd.score.toFixed(1);
  const tipId = 'score-tip-' + p.id;
  // PR H: savings_bonus is optional on the breakdown (older proposals
  // re-scored after the field arrives don't have it yet on disk; the
  // server fills it from the live breakdown). Only render the row when
  // it's a meaningful contribution.
  const savingsBonus = (bd.savings_bonus != null) ? Number(bd.savings_bonus) : 0;
  const savingsRow = savingsBonus > 0
    ? `<span>+ savings bonus</span><span style="font-family:monospace">${savingsBonus.toFixed(1)}</span>`
    : '';
  return `
    <span class="score-chip" style="position:relative;font-size:0.72rem;color:var(--blue);cursor:pointer;padding:1px 6px;border:1px solid var(--border);border-radius:3px"
          onclick="toggleScorePopover('${escHtml(tipId)}', event)">
      rank #${rank || '?'} · score ${score} ▾
      <span id="${escHtml(tipId)}" class="score-popover" style="display:none;position:absolute;bottom:100%;left:0;margin-bottom:4px;background:var(--bg2);border:1px solid var(--border);padding:8px 10px;min-width:220px;z-index:100;border-radius:4px;box-shadow:0 4px 16px rgba(0,0,0,0.5);color:var(--text2);font-size:0.75rem">
        <div style="margin-bottom:4px;color:var(--blue);font-weight:600">How this was ranked</div>
        <div style="display:grid;grid-template-columns:1fr auto;gap:2px 10px">
          <span>urgency</span><span style="font-family:monospace">${bd.urgency}</span>
          <span>× authority</span><span style="font-family:monospace">${bd.authority.toFixed(2)}</span>
          ${savingsRow}
          <span>+ tiebreak</span><span style="font-family:monospace">${bd.tiebreak.toFixed(4)}</span>
          <span style="border-top:1px solid var(--border);padding-top:2px">= score</span><span style="font-family:monospace;border-top:1px solid var(--border);padding-top:2px">${bd.score.toFixed(3)}</span>
        </div>
      </span>
    </span>`;
}

function toggleScorePopover(id, ev) {
  if (ev) ev.stopPropagation();
  // Close all other popovers
  document.querySelectorAll('.score-popover').forEach(el => {
    if (el.id !== id) el.style.display = 'none';
  });
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', () => {
  document.querySelectorAll('.score-popover').forEach(el => { el.style.display = 'none'; });
});

// ── Generators page ─────────────────────────────────────

async function loadArbiterGenerators() {
  const listEl = document.getElementById('arbiter-generators-list');
  if (!listEl) return;
  try {
    const r = await fetch('/api/arbiter/generators');
    const j = await r.json();
    if (!j.ok) {
      listEl.innerHTML = `<div class="card" style="padding:14px">Error: ${escHtml(j.error || 'unknown')}</div>`;
      return;
    }
    renderGenerators(j);
  } catch (e) {
    listEl.innerHTML = `<div class="card" style="padding:14px">Request failed: ${escHtml(String(e))}</div>`;
  }
}

function renderGenerators(data) {
  const listEl = document.getElementById('arbiter-generators-list');
  const gens = data.generators || [];
  const errors = data.load_errors || {};

  if (!gens.length && Object.keys(errors).length === 0) {
    listEl.innerHTML = `<div class="card" style="padding:18px;text-align:center;color:var(--muted)">No coaches registered yet. They load from <code>packages/analyzer/generators/&lt;id&gt;/charter.yaml</code>.</div>`;
    return;
  }

  const errRows = Object.entries(errors).map(([id, msg]) =>
    `<div class="card" style="padding:10px 14px;margin-bottom:8px;border-left:3px solid var(--red)">
       <b style="color:var(--orange)">Load error:</b> <code>${escHtml(id)}</code> — ${escHtml(msg)}
     </div>`).join('');

  const statusColor = {
    active: 'var(--green)',
    paused: 'var(--yellow)',
    quarantined: 'var(--red)',
  };
  const typeIcon = {
    optimizer: '↑',
    guardian: '🛡',
    meta_guardian: '◈',
  };

  const rows = gens.map(g => {
    const tr = g.track_record || {};
    const status = g.status || 'active';
    const authority = (g.authority || 1.0).toFixed(2);
    const succRate = g.success_rate == null ? '—' : (g.success_rate * 100).toFixed(0) + '%';
    const emitted = tr.proposals_emitted || 0;
    const succ = tr.proposals_verified_success || 0;
    const failed = tr.proposals_verified_failed || 0;
    const lastVerif = tr.last_verification_at ? ago(tr.last_verification_at) : 'never';
    // Background writers (emits_proposals: false) don't produce proposals —
    // they write directly to a bot-local datastore at session_end. The
    // track-record / emit / verif columns don't apply, so render em-dashes
    // with a hint instead of zeros that would make a working coach look
    // broken. Pause/Resume still works (operator can stop a runaway extractor).
    const isWriter = g.emits_proposals === false;
    const writerBadge = isWriter
      ? `<span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);font-size:0.66rem;padding:0 5px;margin-left:6px" title="Runs per-session inside each bot and writes directly to its own datastore. Does not emit proposals.">background writer</span>`
      : '';
    const writerDash = `<span class="subtle" title="Not applicable — this coach writes directly to its own datastore instead of emitting proposals">—</span>`;
    const trackCell = isWriter ? writerDash : authority;
    const winsCell = isWriter ? writerDash : `${succ}/${failed} <span style="color:var(--text3)">(${succRate})</span>`;
    const emittedCell = isWriter ? writerDash : String(emitted);
    const verifCell = isWriter ? writerDash : escHtml(lastVerif);
    // Inline pause/resume button per row. Quarantined coaches don't
    // get a row-level toggle — they need explicit admin review of
    // the quarantine_reason; the detail modal owns that flow. The
    // onclick stops propagation so it doesn't fire the row's
    // openGeneratorDetail handler.
    let actionCell;
    if (status === 'active') {
      actionCell = `<button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:1px 8px" onclick="event.stopPropagation();pauseGenerator('${escHtml(g.id)}')" title="Pause this coach — it stops emitting proposals until resumed">Pause</button>`;
    } else if (status === 'paused') {
      actionCell = `<button class="btn btn-sm" style="font-size:0.72rem;padding:1px 8px" onclick="event.stopPropagation();resumeGenerator('${escHtml(g.id)}')" title="Resume this coach — it starts emitting proposals again on its next scheduled run">Resume</button>`;
    } else {
      actionCell = `<span class="subtle" style="font-size:0.72rem" title="Quarantined coaches require admin investigation before resume. Click the row for the quarantine reason and unblock flow.">—</span>`;
    }
    return `
      <tr style="cursor:pointer" onclick="openGeneratorDetail('${escHtml(g.id)}')">
        <td style="padding:8px 10px">
          <span title="${escHtml(g.type)}">${typeIcon[g.type] || '•'}</span>
          <strong style="margin-left:4px">${escHtml(g.id)}</strong>${writerBadge}
        </td>
        <td style="padding:8px 10px">
          <span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);font-size:0.72rem;padding:1px 6px">${escHtml(g.dimension)}</span>
        </td>
        <td style="padding:8px 10px">
          <span class="badge" style="background:${statusColor[status] || 'var(--text2)'};color:#000;font-size:0.72rem;padding:1px 6px">${escHtml(status)}</span>
        </td>
        <td style="padding:8px 10px;font-family:monospace;font-size:0.82rem">${trackCell}</td>
        <td style="padding:8px 10px;font-size:0.82rem">${winsCell}</td>
        <td style="padding:8px 10px;font-size:0.82rem;color:var(--muted)">${emittedCell}</td>
        <td style="padding:8px 10px;font-size:0.82rem;color:var(--muted)">${verifCell}</td>
        <td style="padding:8px 10px;text-align:right">${actionCell}</td>
      </tr>`;
  }).join('');

  listEl.innerHTML = `
    ${errRows}
    <div class="card" style="padding:4px 0;overflow:auto">
      <table style="width:100%;font-size:0.85rem;border-collapse:collapse">
        <thead>
          <tr style="color:var(--text2);border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:8px 10px;font-weight:500">Coach</th>
            <th style="text-align:left;padding:8px 10px;font-weight:500">Dimension</th>
            <th style="text-align:left;padding:8px 10px;font-weight:500">Status</th>
            <th style="text-align:left;padding:8px 10px;font-weight:500">Track record</th>
            <th style="text-align:left;padding:8px 10px;font-weight:500">Wins / Losses</th>
            <th style="text-align:left;padding:8px 10px;font-weight:500">Emitted</th>
            <th style="text-align:left;padding:8px 10px;font-weight:500">Last verif.</th>
            <th style="text-align:right;padding:8px 10px;font-weight:500">Actions</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="subtle" style="margin-top:10px;font-size:0.78rem">Pause stops a coach from emitting new proposals; Resume restarts it on the next scheduled run. Click a row for charter detail and the quarantine flow.</div>`;
}

async function openGeneratorDetail(id) {
  const modal = document.getElementById('generator-detail-modal');
  const body = document.getElementById('generator-detail-body');
  if (!modal || !body) return;
  body.innerHTML = `<div class="subtle">Loading…</div>`;
  modal.style.display = 'flex';
  try {
    const r = await fetch(`/api/arbiter/generators/${encodeURIComponent(id)}`);
    const j = await r.json();
    if (!j.ok) {
      body.innerHTML = `<div>Error: ${escHtml(j.error || 'unknown')}</div>`;
      return;
    }
    renderGeneratorDetail(j.generator);
  } catch (e) {
    body.innerHTML = `<div>Request failed: ${escHtml(String(e))}</div>`;
  }
}

function closeGeneratorDetail() {
  const modal = document.getElementById('generator-detail-modal');
  if (modal) modal.style.display = 'none';
}

function renderGeneratorDetail(g) {
  const body = document.getElementById('generator-detail-body');
  const tr = g.track_record || {};
  const status = g.status || 'active';
  const isWriter = g.emits_proposals === false;
  const actionBtn = status === 'active'
    ? `<button class="btn btn-ghost btn-sm" onclick="pauseGenerator('${escHtml(g.id)}')">Pause</button>`
    : `<button class="btn btn-sm" onclick="resumeGenerator('${escHtml(g.id)}')">Resume</button>`;

  const invariants = (g.invariants || []).map(inv =>
    `<li style="margin-bottom:3px"><code>${escHtml(inv.id || '')}</code> — ${escHtml(inv.check_kind || '')}: <span style="color:var(--text2)">${escHtml(JSON.stringify(inv.params || {}))}</span></li>`).join('');

  const recent = (g.recent_proposals || []).map(rp =>
    `<li style="margin-bottom:4px;font-size:0.82rem">
       <span style="color:var(--text2)">${escHtml(ago(rp.created_at))}</span>
       · <span class="badge" style="font-size:0.7rem;padding:0 4px;background:rgba(200,200,200,0.1)">${escHtml(rp.status || '')}</span>
       · <span class="badge" style="font-size:0.7rem;padding:0 4px;background:rgba(127,200,255,0.12);color:var(--blue)">${escHtml(rp.urgency || '')}</span>
       — ${escHtml(rp.admin_surface_summary || rp.problem || '')}
     </li>`).join('');

  // Background writers run per-session inside each bot and write directly to a
  // bot-local datastore (e.g. user_profile_inferrer → profile .md files).
  // The proposal-count cards (Emitted/Applied/Verified/Rejected/Vetoed) are
  // all zero by design; hide them and show a single explanatory note instead
  // of a grid of misleading zeros.
  const statsBlock = isWriter
    ? `<div class="card" style="padding:10px 14px;margin-bottom:14px;border-left:3px solid var(--blue)">
         <div style="font-size:0.82rem;color:var(--text2)">
           Background writer — runs per-session inside each bot and writes
           directly to its own datastore. Does not emit proposals, so the
           track-record / verification metrics don't apply. Pause to stop
           extraction across all bots.
         </div>
       </div>`
    : `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px">
         <div class="card" style="padding:8px 10px"><div style="font-size:0.72rem;color:var(--text2)">Track record</div><div style="font-family:monospace;font-size:1.1rem">${(g.authority || 1).toFixed(2)}</div></div>
         <div class="card" style="padding:8px 10px"><div style="font-size:0.72rem;color:var(--text2)">Emitted</div><div style="font-family:monospace;font-size:1.1rem">${tr.proposals_emitted || 0}</div></div>
         <div class="card" style="padding:8px 10px"><div style="font-size:0.72rem;color:var(--text2)">Applied</div><div style="font-family:monospace;font-size:1.1rem">${tr.proposals_applied || 0}</div></div>
         <div class="card" style="padding:8px 10px"><div style="font-size:0.72rem;color:var(--text2)">Verified ✓ / ✗</div><div style="font-family:monospace;font-size:1.1rem">${tr.proposals_verified_success || 0} / ${tr.proposals_verified_failed || 0}</div></div>
         <div class="card" style="padding:8px 10px"><div style="font-size:0.72rem;color:var(--text2)">Rejected</div><div style="font-family:monospace;font-size:1.1rem">${tr.proposals_rejected_human || 0}</div></div>
         <div class="card" style="padding:8px 10px"><div style="font-size:0.72rem;color:var(--text2)">Vetoed</div><div style="font-family:monospace;font-size:1.1rem">${tr.proposals_vetoed_guardian || 0}</div></div>
       </div>`;

  body.innerHTML = `
    <div style="margin-bottom:14px">
      <h2 style="margin:0 0 4px 0">${escHtml(g.id)}</h2>
      <div class="subtle" style="margin-bottom:6px">
        <span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);margin-right:4px;font-size:0.72rem">${escHtml(g.type)}</span>
        <span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);margin-right:4px;font-size:0.72rem">${escHtml(g.dimension)}</span>
        <span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);margin-right:4px;font-size:0.72rem">cadence: ${escHtml(g.cadence)}</span>
        <span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);margin-right:4px;font-size:0.72rem">budget: ${escHtml(g.budget_policy)}</span>
        <span class="badge" style="background:${status === 'active' ? 'var(--green)' : status === 'paused' ? 'var(--yellow)' : 'var(--red)'};color:#000;font-size:0.72rem">status: ${escHtml(status)}</span>
        ${isWriter ? `<span class="badge" style="background:rgba(200,200,200,0.1);color:var(--text2);margin-left:4px;font-size:0.72rem">background writer</span>` : ''}
      </div>
      <p style="margin:6px 0;color:var(--muted)">${escHtml(g.purpose || '')}</p>
      ${g.quarantine_reason ? `<div style="padding:8px;background:color-mix(in srgb, var(--red) 12%, transparent);border-left:3px solid var(--red);font-size:0.82rem;margin-top:6px"><b>Paused for review:</b> ${escHtml(g.quarantine_reason)}</div>` : ''}
    </div>

    ${statsBlock}

    <div style="display:flex;gap:8px;margin-bottom:14px">${actionBtn}</div>

    <div class="card" style="padding:10px 14px;margin-bottom:12px">
      <div class="card-title" style="margin-bottom:6px">Charter invariants</div>
      ${invariants ? `<ul style="margin:0;padding-left:18px;font-size:0.82rem">${invariants}</ul>` : '<div class="subtle">(no invariants declared)</div>'}
    </div>

    ${_renderGeneratorTunables(g)}

    ${isWriter ? '' : _renderGeneratorRejections(g)}

    ${isWriter ? '' : `<div class="card" style="padding:10px 14px">
      <div class="card-title" style="margin-bottom:6px">Recent suggestions</div>
      ${recent ? `<ul style="margin:0;padding-left:18px">${recent}</ul>` : '<div class="subtle">(none yet)</div>'}
    </div>`}`;
}

function _renderGeneratorRejections(g) {
  // Show the rejection log as an audit surface so the operator can see
  // which proposals are currently being suppressed by the cooldown. Hidden
  // entirely for generators with no rejection history (the common case).
  const rejections = g.recent_rejections || [];
  const cooldownDays = g.cooldown_days || 14;
  if (!rejections.length) {
    return `
      <div class="card" style="padding:10px 14px;margin-bottom:12px">
        <div class="card-title" style="margin-bottom:6px">Recent rejections</div>
        <div class="subtle" style="font-size:0.78rem">
          No proposals from this generator have been dismissed in the last ${cooldownDays} days.
        </div>
      </div>`;
  }
  const rows = rejections.map(r => {
    const reasonLabel = r.reason
      ? `<span class="badge badge-muted" style="font-size:0.68rem;margin-left:4px">${escHtml(r.reason)}</span>`
      : '';
    const note = r.note
      ? `<div style="font-size:0.74rem;color:var(--text2);margin-top:3px;font-style:italic">“${escHtml(r.note)}”</div>`
      : '';
    const expires = r.cooldown_expires_at
      ? `<span class="subtle" style="font-size:0.7rem;color:var(--text3)">cooldown clears ${ago(r.cooldown_expires_at)}</span>`
      : '';
    const botBadge = r.bot_id
      ? `<span class="badge badge-muted" style="font-size:0.68rem">${escHtml(botLabel(r.bot_id))}</span>`
      : '';
    return `<li style="margin-bottom:8px;font-size:0.82rem">
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span style="color:var(--text3);font-size:0.72rem">${ago(r.ts)}</span>
        ${botBadge}
        ${reasonLabel}
        <span style="flex:1 1 200px">${escHtml(r.problem || '')}</span>
        ${expires}
      </div>
      ${note}
    </li>`;
  }).join('');
  return `
    <div class="card" style="padding:10px 14px;margin-bottom:12px">
      <div class="card-title" style="margin-bottom:6px">Recent rejections</div>
      <div class="subtle" style="font-size:0.78rem;margin-bottom:8px">
        Proposals dismissed in the last ${cooldownDays} days. The generator suppresses re-emission of these fingerprints until each cooldown clears.
      </div>
      <ul style="margin:0;padding-left:18px">${rows}</ul>
    </div>`;
}

function _renderGeneratorTunables(g) {
  const t = g.per_bot_tunables;
  if (!t || !t.params || !t.params.length) return '';
  const params = t.params;
  const bots = t.bots || {};
  const podDefaults = t.pod_defaults || {};
  const botIds = Object.keys(bots).sort();
  if (!botIds.length) return '';

  const fmtNum = (val, p) => (Number(val || 0)).toFixed(p.decimals == null ? 2 : p.decimals);
  const unitOf = p => escHtml(p.unit || '');

  // Header: bot column on the left + one param column per knob.
  // Param header is two lines: label, then "Pod default: <val>" in muted
  // text. The help text moves into an info icon (tooltip) so column widths
  // stay readable instead of stacking 3+ lines of wrapped text.
  const headerCells = params.map(p => {
    const help = p.help ? `<span title="${escHtml(p.help)}" style="display:inline-block;width:14px;height:14px;line-height:14px;border-radius:50%;border:1px solid var(--text3);color:var(--text3);font-size:0.62rem;text-align:center;margin-left:4px;cursor:help">?</span>` : '';
    return `<th style="text-align:right;padding:8px 12px;color:var(--text2);font-size:0.78rem;font-weight:500;min-width:140px">
      <div style="display:flex;align-items:center;justify-content:flex-end;gap:2px">${escHtml(p.label)}${help}</div>
      <div style="font-size:0.68rem;color:var(--text3);font-weight:400;margin-top:2px">Pod default: ${unitOf(p)}${fmtNum(podDefaults[p.id], p)}</div>
    </th>`;
  }).join('');

  const rows = botIds.map(botId => {
    const row = bots[botId] || {};
    const cells = params.map(p => {
      const cell = row[p.id] || {};
      const v = cell.value;
      const isOverride = cell.source === 'override';
      const inputId = `gen-tune-${g.id}-${botId}-${p.id}`;
      const valueAttr = v == null ? '' : String(v);
      // Unit only shown inside the placeholder, never twice. For currency
      // we set a fixed-width left padding and overlay a $ on overridden
      // cells; empty cells fall through to the placeholder which already
      // shows the unit.
      const placeholder = `${unitOf(p)}${fmtNum(podDefaults[p.id], p)}`;
      const overrideTag = isOverride
        ? `<span class="subtle" style="font-size:0.68rem;color:var(--yellow);margin-top:2px">override</span>`
        : '';
      return `<td style="padding:8px 12px;text-align:right;white-space:nowrap">
        <div style="display:inline-flex;flex-direction:column;align-items:flex-end;gap:0">
          <input id="${inputId}" type="number" min="${p.min}" step="${p.step}"
                 placeholder="${escHtml(placeholder)}"
                 value="${escHtml(valueAttr)}"
                 style="width:104px;text-align:right;padding:5px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:0.82rem"
                 data-bot="${escHtml(botId)}" data-param="${escHtml(p.api_field)}"
                 data-write-path="${escHtml(p.write_path || 'bot_setup')}">
          ${overrideTag}
        </div>
      </td>`;
    }).join('');
    const status = `<span id="gen-tune-status-${g.id}-${botId}" class="subtle" style="font-size:0.72rem;margin-left:8px"></span>`;
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px 12px;font-family:var(--font-mono);font-size:0.82rem">${escHtml(botLabel(botId))}</td>
      ${cells}
      <td style="padding:8px 12px;text-align:right">
        <button class="btn btn-ghost btn-sm" onclick="_genSaveBotTunables('${escHtml(g.id)}', '${escHtml(botId)}')">Save</button>
        ${status}
      </td>
    </tr>`;
  }).join('');

  return `
    <div class="card" style="padding:14px 16px;margin-bottom:12px">
      <div class="card-title" style="margin-bottom:6px">Per-bot configuration</div>
      <div class="subtle" style="font-size:0.78rem;margin-bottom:12px">
        Tune this generator's thresholds per bot. Leave a cell blank to inherit the pod default shown in each header. Hover the <span style="display:inline-block;width:12px;height:12px;line-height:12px;border-radius:50%;border:1px solid var(--text3);color:var(--text3);font-size:0.6rem;text-align:center">?</span> for what each knob does.
      </div>
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:8px 12px;color:var(--text2);font-size:0.78rem;font-weight:500">Bot</th>
            ${headerCells}
            <th></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

async function _genSaveBotTunables(genId, botId) {
  const status = document.getElementById(`gen-tune-status-${genId}-${botId}`);
  if (!status) return;
  // Collect every input belonging to this (generator, bot) row, splitting
  // by write_path so each persistence layer gets one POST.
  const inputs = document.querySelectorAll(
    `[id^="gen-tune-${genId}-${botId}-"]`
  );
  const botSetupBody = {};
  const genConfigBody = {};
  let errored = false;
  inputs.forEach(inp => {
    if (errored) return;
    const field = inp.dataset.param;
    if (!field) return;
    const writePath = inp.dataset.writePath || 'bot_setup';
    const raw = (inp.value || '').trim();
    let value;
    if (raw === '') {
      value = null;
    } else {
      const n = parseFloat(raw);
      // Budget caps treat 0 as a "disabled" sentinel server-side, so the
      // UI must reject 0 there. For generator_config, the spec's `min`
      // (rendered as the input's min attr) is authoritative.
      const minAttr = parseFloat(inp.min);
      const minimum = isFinite(minAttr) ? minAttr : 0;
      const tooLow = writePath === 'bot_setup' ? n <= 0 : n < minimum;
      if (!isFinite(n) || tooLow) {
        status.style.color = '#ff8c42';
        status.textContent = writePath === 'bot_setup'
          ? `${field} must be a positive number, or blank to clear.`
          : `${field} must be >= ${minimum}, or blank to clear.`;
        errored = true;
        return;
      }
      value = n;
    }
    if (writePath === 'generator_config') {
      genConfigBody[field] = value;
    } else {
      botSetupBody[field] = value;
    }
  });
  if (errored) return;
  status.style.color = 'var(--muted)';
  status.textContent = 'Saving…';
  try {
    const requests = [];
    if (Object.keys(botSetupBody).length) {
      requests.push(fetch(`/api/arbiter/bot-setup/${encodeURIComponent(botId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(botSetupBody),
      }).then(r => r.json()));
    }
    if (Object.keys(genConfigBody).length) {
      requests.push(fetch(`/api/arbiter/generators/${encodeURIComponent(genId)}/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bot_id: botId, params: genConfigBody }),
      }).then(r => r.json()));
    }
    const results = await Promise.all(requests);
    const failed = results.find(j => !j || !j.ok);
    if (failed) {
      status.style.color = '#ff8c42';
      status.textContent = `Error: ${(failed && failed.error) || 'unknown'}`;
      return;
    }
    status.style.color = 'var(--green)';
    status.textContent = 'Saved.';
    setTimeout(() => openGeneratorDetail(genId), 600);
  } catch (e) {
    status.style.color = '#ff8c42';
    status.textContent = `Request failed: ${e}`;
  }
}

async function pauseGenerator(id) {
  const reason = prompt('Reason for pausing ' + id + '? (shown in audit, optional)') || '';
  const r = await fetch(`/api/arbiter/generators/${encodeURIComponent(id)}/pause`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
  const j = await r.json();
  if (!j.ok) { toast('Pause failed: ' + (j.error || 'unknown'), 'err'); return; }
  openGeneratorDetail(id);
  loadArbiterGenerators();
}

// ── Related-proposals strip (spec §6.5) ─────────────────
// A small bridge between the proactive area dashboards (Cost Measures,
// Security, Maintenance, …) and the reactive Proposals page. Each
// dashboard calls loadRelatedProposalsStrip('<dimension>') on load; we
// fetch the ranked pending suggestions in that dimension and render a
// compact header strip.

async function loadRelatedProposalsStrip(dimension) {
  const container = document.getElementById('related-proposals-' + dimension);
  if (!container) return;
  try {
    const r = await fetch(
      '/api/arbiter/proposals?include=pending&dimension=' + encodeURIComponent(dimension)
    );
    const j = await r.json();
    const proposals = (j && j.ok && j.proposals) ? j.proposals : [];
    if (!proposals.length) {
      container.style.display = 'none';
      container.innerHTML = '';
      return;
    }
    container.style.display = '';
    container.innerHTML = renderRelatedProposalsStrip(dimension, proposals);
  } catch (e) {
    // Silent failure — the strip is a nice-to-have; the dashboard still works.
    container.style.display = 'none';
  }
}

function renderRelatedProposalsStrip(dimension, proposals) {
  const dimLabel = dimension.replace(/_/g, ' ');
  const topThree = proposals.slice(0, 3);
  const moreCount = Math.max(0, proposals.length - topThree.length);
  const items = topThree.map(p => {
    const color = URGENCY_COLORS[p.urgency] || 'var(--text2)';
    const summary = p.human_title || p.admin_surface_summary || p.problem || '(no title)';
    const age = ago(p.created_at);
    // Per-proposal routing: the "→ Act" button must land on the page
    // that actually displays this proposal. The Recommendations page
    // shows only surface=improvement; everything else lives on
    // Reports → Alerts. Pre-fix, this always called
    // jumpToProposals(dimension) which always went to Recommendations
    // — so safety-dimension findings (all of which carry
    // surface in {cleanup, drift, firing}) misrouted to an empty
    // queue.
    const surface = p.surface || '';
    return `
      <div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:0.82rem">
        <span class="badge" style="background:${color};color:#000;font-size:0.68rem;padding:1px 5px;flex-shrink:0">${escHtml(p.urgency)}</span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(summary)}">${escHtml(summary)}</span>
        <span style="font-size:0.72rem;color:var(--text3);flex-shrink:0">${escHtml(p.generator_id || '')} · ${escHtml(age)}</span>
        <button class="btn btn-ghost btn-sm" style="flex-shrink:0;font-size:0.72rem;padding:1px 8px" onclick="jumpToProposals('${escHtml(dimension)}', '', '${escHtml(surface)}')">→ Act</button>
      </div>`;
  }).join('');
  // "See all" destination: if any non-improvement proposal is in the
  // result set, default the bulk link to Reports → Alerts (where
  // cleanup/drift/firing findings live). Improvement-only result
  // sets keep the Recommendations destination.
  const anyAlerts = proposals.some(
    p => p.surface && p.surface !== 'improvement'
  );
  const seeAllSurface = anyAlerts ? 'cleanup' : 'improvement';
  const seeAllLabel = anyAlerts ? 'see all in Alerts' : 'see all in Proposals';
  const openLabel = anyAlerts
    ? `Open Alerts (filter: ${escHtml(dimLabel)}) →`
    : `Open Proposals (filter: ${escHtml(dimLabel)}) →`;
  const moreLine = moreCount > 0
    ? `<div style="font-size:0.76rem;color:var(--text2);margin-top:4px">+${moreCount} more — <a href="#" onclick="jumpToProposals('${escHtml(dimension)}', '', '${seeAllSurface}'); return false;">${seeAllLabel}</a></div>`
    : `<div style="font-size:0.76rem;color:var(--text2);margin-top:4px"><a href="#" onclick="jumpToProposals('${escHtml(dimension)}', '', '${seeAllSurface}'); return false;">${openLabel}</a></div>`;
  return `
    <div class="card" style="padding:10px 14px;border-left:3px solid var(--blue)">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px">
        <div style="font-size:0.82rem;font-weight:600;color:var(--blue)">⬦ Open ${escHtml(dimLabel)} proposals (${proposals.length})</div>
        <div style="font-size:0.72rem;color:var(--text2)">from the arbiter</div>
      </div>
      ${items}
      ${moreLine}
    </div>`;
}

function jumpToProposals(dimension, botId, surface) {
  // Route by the proposal's charter surface so the operator lands
  // on the page that actually displays the finding:
  //   surface === 'improvement' (or empty/unknown) → Self-Improvement
  //     (Recommendations) page — this is the historical default and
  //     where ``improvement`` proposals are rendered.
  //   anything else (cleanup / drift / firing) → Reports → Alerts,
  //     which is where the renderArbiterProposals filter routes
  //     non-improvement surfaces. Pre-fix, callers from the Security
  //     and Dashboard tabs always landed on Recommendations and saw
  //     an empty queue because their proposals had been re-routed
  //     away by the Slice 1B surface flip (2026-06-04).
  const goAlerts = !!surface && surface !== 'improvement';
  if (goAlerts) {
    const navEl = document.querySelector('.nav-item[data-page="reports"]');
    if (navEl) nav(navEl);
    setTimeout(() => {
      const tab = document.querySelector('#page-reports .subtab[data-subtab="alerts"]');
      if (tab && typeof tab.click === 'function') tab.click();
    }, 80);
    return;
  }
  // Self-Improvement default path.
  const navEl = document.querySelector('.nav-item[data-page="self-improvement"]');
  if (navEl) nav(navEl);
  setTimeout(() => {
    const dimSel = document.getElementById('arbiter-filter-dimension');
    if (dimSel) dimSel.value = dimension || '';
    // populateBotFilter() may not have resolved yet on first activation;
    // set the bot filter via a small retry so the value sticks once the
    // <option> is in the DOM. Bounded so a missing bot never spins forever.
    const botSel = document.getElementById('arbiter-filter-bot');
    if (botSel) {
      let tries = 0;
      const apply = () => {
        const has = botId ? Array.from(botSel.options).some(o => o.value === botId) : true;
        if (has || tries++ > 10) {
          botSel.value = botId || '';
          if (typeof loadArbiterProposals === 'function') loadArbiterProposals();
        } else {
          setTimeout(apply, 50);
        }
      };
      apply();
    } else if (typeof loadArbiterProposals === 'function') {
      loadArbiterProposals();
    }
  }, 50);
}

// ── Proposal history ────────────────────────────────────────────────────
//
// Closes the loop on the 4-step pipeline: SPOT → DECIDE → APPLY → VERIFY.
// The Proposals tab covers SPOT/DECIDE; this tab covers APPLY/VERIFY by
// showing what's been processed and how it landed.

let _historyInitialized = false;

function initProposalHistory() {
  if (!_historyInitialized) {
    _historyInitialized = true;
    populateHistoryGeneratorFilter();
  }
  loadProposalHistory();
}

async function populateHistoryGeneratorFilter() {
  const sel = document.getElementById('history-filter-generator');
  if (!sel) return;
  try {
    const r = await fetch('/api/arbiter/generators');
    const j = await r.json();
    if (!j.ok) return;
    const ids = (j.generators || []).map(g => g.id).filter(Boolean).sort();
    sel.innerHTML =
      `<option value="">All</option>` +
      ids.map(id => `<option value="${escHtml(id)}">${escHtml(id)}</option>`).join('');
  } catch (e) { /* leave default */ }
}

async function loadProposalHistory() {
  const listEl = document.getElementById('proposal-history-list');
  if (!listEl) return;
  listEl.innerHTML = `<div class="subtle" style="padding:18px;text-align:center">Loading…</div>`;

  // Pull both applied/ and archived/ — these are the post-queue subdirs.
  // Pending/snoozed are intentionally excluded; they live on the Proposals tab.
  const params = new URLSearchParams();
  params.set('include', 'applied,archived');
  const gen = document.getElementById('history-filter-generator').value;
  if (gen) params.set('generator_id', gen);

  try {
    const r = await fetch('/api/arbiter/proposals?' + params.toString());
    const j = await r.json();
    if (!j.ok) {
      listEl.innerHTML = `<div class="card" style="padding:14px">Error: ${escHtml(j.error || 'unknown')}</div>`;
      return;
    }
    const all = j.proposals || [];

    // Stash by id so the detail drawer can look them up — same pattern as
    // the queue's renderArbiterProposals.
    if (!window._proposalsById) window._proposalsById = new Map();
    for (const p of all) window._proposalsById.set(p.id, p);

    // Populate the bot dropdown from the loaded set if it's still empty.
    const botSel = document.getElementById('history-filter-bot');
    if (botSel && botSel.options.length <= 1) {
      const bots = [...new Set(all.map(p => p.bot_id).filter(Boolean))].sort();
      botSel.innerHTML =
        `<option value="">All</option>` +
        bots.map(b => `<option value="${escHtml(b)}">${escHtml(botLabel(b))}</option>`).join('');
    }

    renderProposalHistory(all);
  } catch (e) {
    listEl.innerHTML = `<div class="card" style="padding:14px">Request failed: ${escHtml(String(e))}</div>`;
  }
}

// Map proposal.status → row category for the Status filter. The "refuted"
// option groups all failure modes (reverted / flagged / revert_failed)
// since the operator usually doesn't care about the distinction at a glance.
function _historyCategory(status) {
  if (status === 'applied') return 'applied';
  if (status === 'succeeded') return 'succeeded';
  if (status === 'failed_reverted' || status === 'failed_flagged' || status === 'failed_revert_failed') return 'refuted';
  if (status === 'dismissed' || status === 'rejected' || status === 'superseded' || status === 'resolved_externally') return 'dismissed';
  return 'other';
}

function renderProposalHistory(all) {
  const listEl = document.getElementById('proposal-history-list');
  if (!listEl) return;

  const statusFilter = document.getElementById('history-filter-status').value;
  const botFilter = document.getElementById('history-filter-bot').value;
  const ageFilter = document.getElementById('history-filter-age').value;
  const cutoffMs = ageFilter ? Date.now() - (parseInt(ageFilter, 10) * 86400 * 1000) : 0;

  const filtered = all.filter(p => {
    if (botFilter && p.bot_id !== botFilter) return false;
    if (statusFilter && _historyCategory(p.status) !== statusFilter) return false;
    if (cutoffMs) {
      // Use _apply_time when available (more meaningful for History);
      // fall back to created_at.
      const ts = Date.parse(p._apply_time || p.created_at || '');
      if (!isNaN(ts) && ts < cutoffMs) return false;
    }
    return true;
  });

  // Newest first — by apply_time when present, else created_at.
  filtered.sort((a, b) => {
    const ta = Date.parse(a._apply_time || a.created_at || '') || 0;
    const tb = Date.parse(b._apply_time || b.created_at || '') || 0;
    return tb - ta;
  });

  if (!filtered.length) {
    listEl.innerHTML = `<div class="card" style="padding:18px;text-align:center;color:var(--muted)">No processed proposals match the current filter.</div>`;
    return;
  }

  const rows = filtered.map(renderHistoryRow).join('');
  listEl.innerHTML = `
    <div class="card" style="padding:4px 0;overflow:auto">
      <div class="resp-table-wrap"><table class="resp-table" style="width:100%;font-size:0.85rem;border-collapse:collapse">
        <thead>
          <tr style="color:var(--text2);border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:8px 12px;font-weight:500">When</th>
            <th style="text-align:left;padding:8px 12px;font-weight:500">Proposal</th>
            <th style="text-align:left;padding:8px 12px;font-weight:500">Bot · Coach</th>
            <th style="text-align:left;padding:8px 12px;font-weight:500">Check-in</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table></div>
    </div>`;
  _respTableLabelize(listEl);
}

function renderHistoryRow(p) {
  const title = p.human_title || p.admin_surface_summary || p.problem || '(no title)';
  const actionKind = p._action_kind || (p.action && p.action.kind) || '';
  const ts = p._apply_time || p.created_at || '';
  const verifyCell = renderHistoryVerifyCell(p);
  const dim = p.dimension || '';
  return `
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);cursor:pointer" onclick="openProposalDetail('${escHtml(p.id)}')" title="View full proposal">
      <td style="padding:8px 12px;color:var(--text2);white-space:nowrap;vertical-align:top">${escHtml(ago(ts))}</td>
      <td style="padding:8px 12px;vertical-align:top">
        <div style="font-weight:500;color:var(--text)">${escHtml(title)}</div>
        <div style="display:flex;gap:4px;margin-top:3px;flex-wrap:wrap">
          ${actionKind ? `<span class="badge" style="background:rgba(200,200,200,0.08);color:var(--text2);font-size:0.68rem;padding:1px 6px">${escHtml(actionKind)}</span>` : ''}
          ${dim ? `<span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);font-size:0.68rem;padding:1px 6px">${escHtml(dim)}</span>` : ''}
        </div>
      </td>
      <td style="padding:8px 12px;color:var(--text2);vertical-align:top;font-size:0.82rem;white-space:nowrap">
        ${escHtml(p.bot_id ? botLabel(p.bot_id) : '—')}<br><span style="color:var(--text2)">${escHtml(p.generator_id || '—')}</span>
      </td>
      <td style="padding:8px 12px;vertical-align:top">${verifyCell}</td>
    </tr>`;
}

function renderHistoryVerifyCell(p) {
  const status = p.status;

  if (status === 'applied') {
    // Compute days remaining until verify is due. apply_time + window_days.
    const applyMs = Date.parse(p._apply_time || '');
    const windowDays = (p.claim && p.claim.window_days) || 0;
    if (!applyMs || !windowDays) {
      return `<span style="color:var(--blue)">🔵 Applied — verifying</span>`;
    }
    const dueMs = applyMs + (windowDays * 86400 * 1000);
    const daysLeft = Math.ceil((dueMs - Date.now()) / (86400 * 1000));
    if (daysLeft > 0) {
      return `<span style="color:var(--blue)">🔵 Verifying — ${daysLeft}d remain</span>`;
    }
    return `<span style="color:#ffb83c">🟡 Verify overdue (daemon should pick up next run)</span>`;
  }

  const v = p._verification;
  const fmt = (n) => (n == null ? '—' : (Math.round(n * 1000) / 1000).toString());

  if (status === 'succeeded') {
    if (v) {
      const arrow = v.direction === 'down' ? '↓' : v.direction === 'up' ? '↑' : '=';
      return `<span style="color:var(--green)">✅ Confirmed</span><div style="font-size:0.76rem;color:var(--muted);margin-top:2px"><code>${escHtml(v.metric || '')}</code>: ${fmt(v.baseline)} → ${fmt(v.current_value)} (${arrow} ${fmt(Math.abs(v.delta || 0))})</div>`;
    }
    return `<span style="color:var(--green)">✅ Confirmed</span>`;
  }

  if (status === 'failed_reverted') {
    if (v) {
      return `<span style="color:#ff8c42">↩️ Reverted</span><div style="font-size:0.76rem;color:var(--muted);margin-top:2px"><code>${escHtml(v.metric || '')}</code>: ${fmt(v.baseline)} → ${fmt(v.current_value)} (claim ${escHtml(v.direction || '')} ≥ ${fmt(v.magnitude)})</div>`;
    }
    return `<span style="color:#ff8c42">↩️ Reverted — metric did not meet target</span>`;
  }

  if (status === 'failed_flagged') {
    return `<span style="color:var(--red)">⚠️ Flagged — needs operator attention</span>`;
  }

  if (status === 'failed_revert_failed') {
    return `<span style="color:var(--red)">⚠️ Revert failed — manual cleanup required</span>`;
  }

  if (status === 'dismissed') return `<span style="color:var(--text2)">🚫 Dismissed</span>`;
  if (status === 'rejected') return `<span style="color:var(--text2)">🚫 Rejected</span>`;
  if (status === 'superseded') return `<span style="color:var(--text2)">↪️ Superseded</span>`;
  if (status === 'resolved_externally') return `<span style="color:var(--text2)">✓ Resolved externally</span>`;

  return `<span style="color:var(--text2)">${escHtml(status || 'unknown')}</span>`;
}

// ── Observation browser ─────────────────────────────────

let _obsDebounce = null;
function debouncedLoadObservations() {
  clearTimeout(_obsDebounce);
  _obsDebounce = setTimeout(loadObservations, 250);
}

async function initObsBotSelector() {
  const sel = document.getElementById('obs-filter-bot');
  if (!sel) return;
  if (sel.options.length === 0) {
    try {
      const r = await fetch('/api/network');
      const j = await r.json();
      // Cache for botLabel() — the observations page may render before
      // any other page has populated _networkData.
      if (j && j.bots) _networkData = j;
      const members = (j.members || []);
      sel.innerHTML = members.map(b => `<option value="${escHtml(b)}">${escHtml(botLabel(b))}</option>`).join('');
    } catch (e) {
      // Continue without bots; user sees empty selector
    }
  }
  if (sel.value) loadObservations();
}

async function loadObservations() {
  const body = document.getElementById('observations-body');
  if (!body) return;
  const bot = document.getElementById('obs-filter-bot').value;
  if (!bot) { body.innerHTML = `<div class="subtle">Select a bot.</div>`; return; }
  const days = parseInt(document.getElementById('obs-filter-since-days').value || '7', 10);
  const noun = document.getElementById('obs-filter-noun').value.trim();
  const verb = document.getElementById('obs-filter-verb').value;
  const mood = document.getElementById('obs-filter-mood').value;
  const params = new URLSearchParams({ bot_id: bot });
  if (days > 0) params.set('since', new Date(Date.now() - days * 86400 * 1000).toISOString());
  if (noun) params.set('noun', noun);
  if (verb) params.set('verb', verb);
  if (mood) params.set('mood', mood);
  try {
    const r = await fetch('/api/arbiter/observations?' + params.toString());
    const j = await r.json();
    if (!j.ok) {
      body.innerHTML = `<div class="card" style="padding:14px"><b>Error:</b> ${escHtml(j.error || 'unknown')}${j.note ? `<div style="margin-top:6px;font-size:0.82rem;color:var(--muted)">${escHtml(j.note)}</div>` : ''}</div>`;
      return;
    }
    renderObservations(j);
  } catch (e) {
    body.innerHTML = `<div class="card" style="padding:14px">Request failed: ${escHtml(String(e))}</div>`;
  }
}

function renderObservations(data) {
  const body = document.getElementById('observations-body');
  const summary = `
    <div class="card" style="padding:12px 14px;margin-bottom:12px">
      <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:0.85rem">
        <div><span style="color:var(--text2)">Window:</span> ${escHtml(data.start)} → ${escHtml(data.end)}</div>
        <div><span style="color:var(--text2)">In window:</span> ${data.total_in_window}</div>
        <div><span style="color:var(--text2)">Matched:</span> ${data.matched}</div>
        <div><span style="color:var(--text2)">Engagement total:</span> ${data.engagement_total}</div>
      </div>
    </div>`;

  const moodBars = Object.entries(data.mood_distribution || {}).map(([m, n]) =>
    `<span class="badge" style="background:rgba(127,200,255,0.12);color:var(--blue);margin-right:4px;font-size:0.72rem;padding:1px 6px">${escHtml(m)}: ${n}</span>`
  ).join('');

  const topVerbs = (data.top_verbs || []).map(([v, n]) =>
    `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:0.82rem"><span>${escHtml(v)}</span><span style="color:var(--text2);font-family:monospace">${n}</span></div>`
  ).join('');

  const topNouns = (data.top_nouns || []).map(([n, c]) =>
    `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:0.82rem"><span>${escHtml(n)}</span><span style="color:var(--text2);font-family:monospace">${c}</span></div>`
  ).join('');

  const sample = (data.sample || []).slice(0, 30).map(t =>
    `<tr>
       <td style="padding:5px 8px;font-size:0.78rem;color:var(--text2)">${escHtml(ago(t.timestamp_start))}</td>
       <td style="padding:5px 8px;font-size:0.82rem">${escHtml(t.noun)}</td>
       <td style="padding:5px 8px;font-size:0.82rem">${escHtml(t.verb)}</td>
       <td style="padding:5px 8px;font-size:0.82rem;color:var(--blue)">${escHtml(t.mood || '—')}</td>
       <td style="padding:5px 8px;font-size:0.82rem;font-family:monospace">${t.engagement}</td>
       <td style="padding:5px 8px;font-size:0.72rem;color:var(--text3)">${escHtml((t.session_id || '').slice(0, 8))}</td>
     </tr>`
  ).join('');

  body.innerHTML = `
    ${summary}
    ${moodBars ? `<div class="card" style="padding:10px 14px;margin-bottom:10px"><div class="card-title" style="margin-bottom:6px">Mood distribution</div>${moodBars}</div>` : ''}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px">
      <div class="card" style="padding:10px 14px"><div class="card-title" style="margin-bottom:6px">Top verbs</div>${topVerbs || '<div class="subtle">(none)</div>'}</div>
      <div class="card" style="padding:10px 14px"><div class="card-title" style="margin-bottom:6px">Top nouns</div>${topNouns || '<div class="subtle">(none)</div>'}</div>
    </div>
    <div class="card" style="padding:4px 0;overflow:auto">
      <table style="width:100%;font-size:0.85rem;border-collapse:collapse">
        <thead><tr style="color:var(--text2);border-bottom:1px solid var(--border)">
          <th style="text-align:left;padding:6px 8px;font-weight:500">When</th>
          <th style="text-align:left;padding:6px 8px;font-weight:500">Noun</th>
          <th style="text-align:left;padding:6px 8px;font-weight:500">Verb</th>
          <th style="text-align:left;padding:6px 8px;font-weight:500">Mood</th>
          <th style="text-align:left;padding:6px 8px;font-weight:500">Engagement</th>
          <th style="text-align:left;padding:6px 8px;font-weight:500">Session</th>
        </tr></thead>
        <tbody>${sample || '<tr><td colspan="6" style="padding:12px;text-align:center;color:var(--muted)">No tuples match these filters.</td></tr>'}</tbody>
      </table>
    </div>`;
}

async function resumeGenerator(id) {
  const r = await fetch(`/api/arbiter/generators/${encodeURIComponent(id)}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  const j = await r.json();
  if (!j.ok) { toast('Resume failed: ' + (j.error || 'unknown'), 'err'); return; }
  openGeneratorDetail(id);
  loadArbiterGenerators();
}

// ══════════════════════════════════════════════════════════════════════════
// Value subtab — per-bot utilization table
// (internal/spec-value-baseline-2026-06-10.md §7.2)
//
// Renders the latest nightly rollup served by /api/better/value. The
// rows arrive ranked (state, then days-of-human-use, then scheduled
// runs — the spec §9 key applied server-side) and the in-use / idle /
// not-enough-data judgement arrives as utilization_state — this
// renderer re-implements neither. All visible copy follows the §7.3
// rules: plain words only.
// ══════════════════════════════════════════════════════════════════════════

const _VALUE_STATE_LABELS = {
  active:       { text: 'In use',          cls: 'badge-ok' },
  underused:    { text: 'Idle 4 weeks',    cls: 'badge-warn' },
  unmeasurable: { text: 'Not enough data', cls: 'badge-neutral' },
};

function _valueFmtDay(iso) {
  // "2026-06-09" → "June 9". Parse as local midnight (appending T00:00:00
  // avoids the UTC-date-shifts-a-day-back pitfall of bare date strings).
  const d = new Date(iso + 'T00:00:00');
  if (isNaN(d)) return iso;
  return d.toLocaleDateString(undefined, { month: 'long', day: 'numeric' });
}

async function _loadValueView() {
  const tableEl = document.getElementById('si-value-table');
  const metaEl = document.getElementById('si-value-meta');
  if (!tableEl) return;
  tableEl.innerHTML = `<div class="subtle" style="padding:18px;text-align:center">Loading…</div>`;
  let j;
  try {
    const r = await fetch('/api/better/value');
    j = await r.json();
  } catch (e) {
    tableEl.innerHTML = `<div class="card" style="padding:14px">Couldn't load usage data: ${escHtml(String(e))}</div>`;
    return;
  }
  if (!j.ok) {
    tableEl.innerHTML = `<div class="card" style="padding:14px">Couldn't load usage data: ${escHtml(j.error || 'unknown')}</div>`;
    return;
  }
  if (!j.available || !(j.bots || []).length) {
    if (metaEl) metaEl.style.display = 'none';
    tableEl.innerHTML = `<div class="empty-state-card">No usage summary yet — it's computed overnight. Check back tomorrow.</div>`;
    return;
  }

  // "as of <date>" + out-of-date flag (spec §5.3 — a degraded
  // measurement pipeline must be visible, not silently rendered as last
  // week's numbers).
  if (metaEl) {
    const staleBadge = j.stale
      ? ` <span class="badge badge-sm badge-warn" title="This summary hasn't been updated in more than 2 days — recent activity isn't reflected yet.">out of date</span>`
      : '';
    metaEl.innerHTML = `as of ${escHtml(_valueFmtDay(j.anchor_date || ''))}${staleBadge}`;
    metaEl.style.display = '';
  }

  const rows = j.bots.map(b => {
    const st = _VALUE_STATE_LABELS[b.utilization_state] || _VALUE_STATE_LABELS.unmeasurable;
    const human = b.active_human_days_28d || {};
    const runs = b.proactive_runs_28d || {};
    const cov = b.app_coverage_28d || {};
    const trend = b.value_trend_28d || {};
    // §7.3 reference copy for the can't-tell state; state_reason is
    // already plain language ("usage records cover only 14 of the last
    // 28 days") so it slots into the sentence directly.
    const stateTitle = b.utilization_state === 'unmeasurable'
      ? `Not enough data to tell — ${b.state_reason || 'usage records are incomplete'}.`
      : (b.state_reason || '');
    const numCell = (v, nullTitle) => v == null
      ? `<span class="subtle" title="${escHtml(nullTitle)}">—</span>`
      : `${v}`;
    const covCell = cov.value == null
      ? `<span class="subtle" title="${cov.apps_total === 0 ? 'No apps installed' : 'Not measured'}">—</span>`
      : `<span title="${cov.apps_used} of ${cov.apps_total} installed apps ran in the last 4 weeks">${Math.round(cov.value * 100)}%</span>`;
    const trendCell = (() => {
      if (trend.value == null) {
        return `<span class="subtle" title="Needs about 8 weeks of usage records to compare">—</span>`;
      }
      const tip = `${trend.current} days-used + scheduled runs in the last 4 weeks, vs ${trend.prior} in the 4 weeks before`;
      if (trend.value > 0) return `<span class="pod-trend-up" title="${escHtml(tip)}">▲ +${trend.value}</span>`;
      if (trend.value < 0) return `<span class="pod-trend-down" title="${escHtml(tip)}">▼ −${Math.abs(trend.value)}</span>`;
      return `<span class="pod-trend-flat" title="${escHtml(tip)}">≈ 0</span>`;
    })();
    const records = (human.measurable_days != null && human.window_days != null)
      ? `${human.measurable_days} of ${human.window_days} days`
      : '—';
    const nullNote = 'Not enough usage records in this window to count';
    return `<tr>
      <td data-label="Bot">${escHtml(botLabel(b.bot_id))}</td>
      <td data-label="Status"><span class="badge badge-sm ${st.cls}" title="${escHtml(stateTitle)}">${st.text}</span></td>
      <td data-label="Days used (4 wks)">${numCell(human.value, nullNote)}</td>
      <td data-label="Scheduled runs (4 wks)">${numCell(runs.value, nullNote)}</td>
      <td data-label="App coverage">${covCell}</td>
      <td data-label="Trend">${trendCell}</td>
      <td data-label="Usage records">${escHtml(records)}</td>
    </tr>`;
  }).join('');

  tableEl.innerHTML = `<div class="resp-table-wrap"><table class="resp-table">
    <thead><tr>
      <th>Bot</th>
      <th>Status</th>
      <th title="Days in the last 4 weeks when a person used the bot — one interaction makes the day count; volume doesn't">Days used (4 wks)</th>
      <th title="Scheduled app runs in the last 4 weeks — what the bot delivered without being asked">Scheduled runs (4 wks)</th>
      <th title="Share of installed apps that ran in the last 4 weeks">App coverage</th>
      <th title="Days used + scheduled runs, this 4 weeks vs the 4 weeks before">Trend</th>
      <th title="How many days in the window have usage records — gaps here mean a recording problem, not idle bots">Usage records</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}
