// ════════════════════════════════════════════════════════════════════════
// Page: Alerts (secondary cluster — filter chips + bulk + subscriptions +
//               dispatcher health + push devices + tracked-candidate lane)
//
// Phase 3f extracted the primary Alerts cluster (loadAlerts + per-row
// rendering at lines 48213-49639 pre-Phase-3f). This file catches the
// secondary cluster Phase 3f missed: it sat AFTER the Identity page
// block, separated from the primary cluster by ~17K lines, so a single
// contiguous Phase 3f extraction couldn't reach it.
//
// Five sub-surfaces ship here:
//
//   1. Tracked-candidate lane — _alCandidateRow + _alLoadTracked render
//      the watchlist / synthesizing / dropped lanes under the Tracked
//      subtab. Synthesizer-produced candidates show up here before they
//      promote to firing signals.
//
//   2. Per-row actions — _alSnooze / _alResolve / _alPairedAct (act on
//      the paired proposal too), plus the dismiss modal flow
//      (_alDismiss / _alDismissClose / _alDismissSubmit). _alRefreshActive
//      is the post-action refresh helper.
//
//   3. Filter chips — _alFilterByCategory + the _AL_CATEGORIES table.
//
//   4. Bulk actions — _alBulkPost / _alBulkSnooze / _alBulkResolve /
//      _alBulkDismissConfirm + _alBulkConfirmClose / _alBulkDismissSubmit /
//      _alBulkToast. The "sticky bulk-action bar" UX at the bottom.
//
//   5. Subscriptions / Dispatcher Health / PWA Push devices —
//      _alLoadActivity (the polling sister of loadAlerts),
//      _alLoadDispatcherHealth, the _alPushDevicesRender +
//      _alPushDeviceLabel + _alPushRemoveDevice + _alPushTestOne devices
//      panel, _alPwaPushChannelToggle, plus the subscriptions
//      family — Phase 2 renders the 13 operator-facing GROUPS:
//      (_alSubsGroupRow / _alSubsGroupToggle / _alSubsDigestHour /
//      _alSubsTest / _alSubsReset),
//      and the manual refresh button (_alRefreshNow).
//
// State (top of the file):
//   _alDismissSignalId, _alActiveCategory, _AL_CATEGORIES (lookup table),
//   _alBulkDismissPendingVerdict / _alBulkDismissPendingIds,
//   _alLoadActivity_inflight, _alLoadDispatcherHealth_inflight
//
// Cross-file linkages (runtime free-variable lookup):
//   - loadAlerts, _alLoadLane, _alLoadCount — pages/alerts.js (Phase 3f);
//     this cluster + the primary cluster cross-call each other
//   - api(), toast(), escHtml() — core/
//   - openProposalDetail / renderProposalCard — self-improvement.js
//     (the per-row "Act on paired proposal" button)
// ════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════

function _alCandidateRow(c) {
  const title = escHtml(c.draft_headline || c.draft_problem || '(no title)');
  const sym = c.draft_problem && c.draft_problem !== c.draft_headline
    ? `<div style="font-size:0.78rem;color:var(--text2);margin-top:4px">${escHtml(c.draft_problem)}</div>`
    : '';
  const aggBadge = c.aggregation && c.aggregation !== 'none'
    ? `<span class="badge badge-muted" title="aggregation">${escHtml(c.aggregation)}</span>` : '';
  const note = c.synthesizer_note
    ? `<div class="stripe-card is-info" style="margin-top:6px;padding:6px 10px;background:color-mix(in srgb, var(--blue) 8%, transparent);border-radius:6px;font-size:0.78rem;color:var(--text2)"><strong style="color:var(--blue)">synthesizer:</strong> ${escHtml(c.synthesizer_note)}</div>`
    : '';
  return `
    <div style="padding:10px 12px;border-bottom:1px solid var(--border)">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
        <strong style="flex:1 1 auto;min-width:200px">${title}</strong>
        <span class="badge badge-muted">bot: ${escHtml(botLabel(c.bot_id))}</span>
        <span class="badge badge-muted">${escHtml(c.variant || '')}</span>
        ${_alMagnitudeChip(c.magnitude)}
        ${aggBadge}
        <span style="color:var(--text3);font-size:0.78rem">${ago(c.created_at)}</span>
      </div>
      ${sym}
      ${note}
    </div>`;
}

async function _alLoadTracked() {
  const wl = document.getElementById('al-watchlist-body');
  const sy = document.getElementById('al-synthesizing-body');
  const dr = document.getElementById('al-dropped-body');
  if (wl) wl.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  if (sy) sy.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  if (dr) dr.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';

  try {
    const [w, s, d] = await Promise.all([
      api('GET', '/api/candidates/watchlist?limit=200'),
      api('GET', '/api/candidates/synthesizing?limit=200'),
      api('GET', '/api/candidates/dropped?days=7&limit=500'),
    ]);

    if (wl) {
      const cands = (w && w.candidates) || [];
      wl.innerHTML = cands.length
        ? cands.map(_alCandidateRow).join('')
        : '<div class="empty">Nothing on the watchlist. Candidates demoted by the concreteness gate (Investigation actions without a named tunable) appear here.</div>';
    }
    if (sy) {
      const cands = (s && s.candidates) || [];
      sy.innerHTML = cands.length
        ? cands.map(_alCandidateRow).join('')
        : '<div class="empty">No candidates awaiting synthesis. Substrate-level aggregates (≥3 bots showing the same condition) land here.</div>';
    }
    if (dr) {
      const drops = (d && d.drops) || [];
      if (!drops.length) {
        dr.innerHTML = '<div class="empty">No drops in the last 7 days.</div>';
      } else {
        const byReason = {};
        for (const rec of drops) {
          const k = rec.reason || 'unknown';
          (byReason[k] = byReason[k] || []).push(rec);
        }
        const sections = Object.keys(byReason).sort().map(reason => {
          const recs = byReason[reason];
          const rows = recs.slice(0, 50).map(r => `
            <tr>
              <td style="white-space:nowrap;color:var(--text3)">${escHtml(r.ts || '')}</td>
              <td>${escHtml(botLabel(r.bot_id))}</td>
              <td><code style="font-size:0.74rem">${escHtml(r.variant || '')}</code></td>
              <td>${r.magnitude ? `${escHtml(String(r.magnitude.value))} ${escHtml(r.magnitude.unit || '')}` : ''}</td>
              <td style="font-size:0.78rem;color:var(--text2)">${escHtml(r.note || '')}</td>
            </tr>`).join('');
          return `
            <div style="margin-bottom:18px">
              <div style="font-size:0.85rem;color:var(--text2);margin-bottom:6px">
                <code>${escHtml(reason)}</code> — ${recs.length} drop${recs.length === 1 ? '' : 's'}
              </div>
              <div class="resp-table-wrap"><table class="resp-table data-table"><thead><tr>
                <th>When</th><th>Bot</th><th>Variant</th><th>Magnitude</th><th>Note</th>
              </tr></thead><tbody>${rows}</tbody></table></div>
            </div>`;
        });
        dr.innerHTML = sections.join('');
        _respTableLabelize(dr);
      }
    }

    // Update the count badge on the subtab.
    const total = ((w && w.total) || 0) + ((s && s.total) || 0);
    const badge = document.getElementById('al-count-tracked');
    if (badge) badge.textContent = total > 0 ? String(total) : '';
  } catch(e) {
    const msg = `<div class="empty">Error: ${escHtml(String(e))}</div>`;
    if (wl) wl.innerHTML = msg;
    if (sy) sy.innerHTML = msg;
    if (dr) dr.innerHTML = msg;
  }
}

async function _alSnooze(id, duration) {
  try {
    await api('POST', `/api/signals/${encodeURIComponent(id)}/snooze`, { duration });
    _alRefreshActive();
  } catch(e) {
    toast(`Snooze failed: ${e}`, 'err');
  }
}

async function _alResolve(id) {
  try {
    await api('POST', `/api/signals/${encodeURIComponent(id)}/resolve`, {});
    _alRefreshActive();
  } catch(e) {
    toast(`Resolve failed: ${e}`, 'err');
  }
}

// _alPairedAct — inline-act on a Proposal that's linked to a Signal via
// Signal.motivated_proposals. The Alerts page renders the linked
// Proposal's "Act" button next to the Signal row (paired-row display)
// so the operator can act on the underlying fix without leaving the
// Alerts surface. On success, the Signal will typically auto-resolve
// (sweep-style monitors do this on the next pass once the underlying
// condition clears) — refreshing the lane redraws without it.
async function _alPairedAct(propId, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('POST', `/api/arbiter/proposals/${encodeURIComponent(propId)}/act`);
    if (r && r.error) throw new Error(r.error);
    const msg = r && r.applied ? 'Applied' : ((r && r.message) || 'Submitted');
    if (typeof toast === 'function') toast(msg, 'ok');
    _alRefreshActive();
  } catch(e) {
    if (btn) btn.disabled = false;
    const msg = String(e && e.message || e);
    if (typeof toast === 'function') toast(`Act failed: ${msg}`, 'err');
    else toast(`Act failed: ${msg}`, 'err');
  }
}

// Currently-open signal id for the dismiss modal — no need for state
// management beyond a single global since the modal is non-stackable.
let _alDismissSignalId = null;

function _alDismiss(id) {
  // Open the dismiss modal. Verdict is a closed enum (dropdown); note
  // is free text. The earlier single-prompt flow conflated the two and
  // silently dropped any input that wasn't exactly an enum value —
  // typing "not_actionable telegram is disabled on team_bot_a" lost both the
  // verdict and the explanation.
  _alDismissSignalId = id;
  const verdictSel = document.getElementById('al-dismiss-verdict');
  const noteEl = document.getElementById('al-dismiss-note');
  const resultEl = document.getElementById('al-dismiss-result');
  if (verdictSel) verdictSel.value = '';
  if (noteEl) noteEl.value = '';
  if (resultEl) resultEl.textContent = '';
  document.getElementById('al-dismiss-modal').classList.add('open');
  // Focus the verdict dropdown so keyboard users land somewhere useful.
  if (verdictSel) verdictSel.focus();
}

function _alDismissClose() {
  _alDismissSignalId = null;
  document.getElementById('al-dismiss-modal').classList.remove('open');
}

async function _alDismissSubmit() {
  if (!_alDismissSignalId) return;
  const verdict = document.getElementById('al-dismiss-verdict').value;
  const note = document.getElementById('al-dismiss-note').value.trim();
  const resultEl = document.getElementById('al-dismiss-result');
  const body = {};
  if (verdict) body.verdict = verdict;
  if (note) body.note = note;
  // The API records `note` only when paired with a verdict (spec §9 —
  // feedback.jsonl rows always have a verdict). Warn rather than
  // silently dropping the user's text.
  if (note && !verdict) {
    resultEl.textContent = 'Pick a verdict to attach this note, or clear the note to dismiss without feedback.';
    return;
  }
  try {
    const id = _alDismissSignalId;
    _alDismissClose();
    await api('POST', `/api/signals/${encodeURIComponent(id)}/dismiss`, body);
    _alRefreshActive();
  } catch(e) {
    resultEl.textContent = `Dismiss failed: ${e}`;
    document.getElementById('al-dismiss-modal').classList.add('open');
  }
}

function _alRefreshActive() {
  // Refresh whichever lane is currently showing, plus the other count badge.
  const activity = document.getElementById('alerts-activity');
  if (activity && activity.classList.contains('active')) {
    _alLoadLane('activity');
    _alLoadCount('maintenance');
  } else {
    _alLoadLane('maintenance');
    _alLoadCount('activity');
  }
  // Reports → Alerts shares the same signal store; refresh that lane
  // too so per-row + bulk actions transparently disappear.
  if (document.getElementById('reports-alerts-body')) _alLoadLane('reports');
}

// ═══════════════════════════════════════════════════════════════════════════
// Filter chips + multi-select + bulk actions (Reports → Alerts page)
//
// Part 3 (chips) and Part 2 (bulk bar) of the per-row/bulk PR. The
// chip filter is purely client-side DOM hiding — the same `sigs`
// the API returned stay in memory, just visually filtered. The bulk
// bar reads checked .al-row-select boxes and posts to
// /api/signals/bulk-action.
// ═══════════════════════════════════════════════════════════════════════════

// Active filter set, structured as { producer: Set, bot: Set, severity: Set }.
// Empty Set in a dimension means "no filter on that dimension" (all rows
// pass). Otherwise the row's value for that dimension must be in the set.
const _alActiveFilters = {
  producer: new Set(),
  bot:      new Set(),
  severity: new Set(),
};

// Active category tab. null = "All" (no category filter applied). One of
// the six Signal categories: security | cost | platform | integrations |
// backup | hygiene. Source of truth lives on the Signal payload's
// ``category`` field; this state just gates which subset gets rendered.
// Added 2026-06-03 as a result of the Alerts review — 33 producers fanned
// into a flat list was too noisy; tabbing by domain reduces the scan cost.
let _alActiveCategory = null;

// Canonical category order + labels for the tab strip. Matches the
// Python-side PRODUCER_CATEGORY_DEFAULT mapping in schema.signal.
const _AL_CATEGORIES = [
  {key: 'security',     label: 'Security'},
  {key: 'cost',         label: 'Cost'},
  {key: 'platform',     label: 'Platform'},
  {key: 'integrations', label: 'Integrations'},
  {key: 'backup',       label: 'Backup'},
  {key: 'hygiene',      label: 'Hygiene'},
];

function _alRenderCategoryTabs(sigs) {
  // Render the top-level category tab strip with per-bucket counts.
  // ``sigs`` is the post-severity-filter signal list (matches what
  // _alLoadLane would render absent the category filter), so the count
  // next to each tab is the number the operator would actually see.
  const wrap = document.getElementById('al-category-tabs');
  if (!wrap) return;
  const counts = new Map();
  for (const s of sigs || []) {
    const c = s.category || 'platform';
    counts.set(c, (counts.get(c) || 0) + 1);
  }
  const total = (sigs || []).length;
  const tabBtn = (key, label, count) => {
    const isActive = (_alActiveCategory === key) || (key === null && _alActiveCategory === null);
    const style = isActive
      ? 'background:var(--accent);color:#fff;border-color:var(--accent)'
      : 'background:var(--bg2);color:var(--text2);border-color:var(--border)';
    const onclick = key === null
      ? `_alSetCategory(null)`
      : `_alSetCategory('${escHtml(key)}')`;
    return `<button class="al-category-tab" data-category="${escHtml(key || '')}"
              onclick="${onclick}"
              style="${style};padding:5px 12px;border-radius:14px;font-size:0.78rem;cursor:pointer;border-width:1px;border-style:solid;line-height:1.5"
              title="${escHtml(label)}">${escHtml(label)} <span style="opacity:0.7">(${count})</span></button>`;
  };
  const tabs = [tabBtn(null, 'All', total)];
  for (const c of _AL_CATEGORIES) {
    tabs.push(tabBtn(c.key, c.label, counts.get(c.key) || 0));
  }
  wrap.innerHTML = tabs.join(' ');
}

function _alSetCategory(category) {
  // Switch the active tab and re-render in place. Avoid a network round
  // trip — the lane fetch already pulled all signals; we just slice
  // here. Matches how _alFilterBySeverity works.
  _alActiveCategory = category;
  _alLoadLane('reports');
}

function _alFilterByCategory(sigs) {
  if (!_alActiveCategory) return sigs;
  return (sigs || []).filter(s => (s.category || 'platform') === _alActiveCategory);
}

function _alRenderTruncationBanner(resp) {
  // Surface the gap between count (returned) and total (matching)
  // when the server cap truncates the signal list. Without this, the
  // client-side chip filter silently lies: bulk-dismissing "all
  // visible compliance_scan" leaves the hidden tail untouched and the
  // operator concludes the action failed. Banner stays hidden when
  // total <= count or either field is absent (older server build).
  const el = document.getElementById('al-truncation-banner');
  if (!el) return;
  const total = (resp && typeof resp.total === 'number') ? resp.total : null;
  const count = (resp && typeof resp.count === 'number') ? resp.count : null;
  if (total === null || count === null || total <= count) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const hidden = total - count;
  el.innerHTML = `<strong>Showing ${count} of ${total} alerts.</strong> `
    + `${hidden} match but were truncated at the ${count}-row cap. `
    + `Filter chips and bulk actions only see the visible ${count}; `
    + `narrow with a producer or bot chip to reach the rest.`;
  el.style.display = '';
}

function _alRenderFilterChips() {
  // Render filter chips from whatever's currently in the Reports →
  // Alerts list. Recomputes on every load so dimension values track
  // the live signal set. Called from _alLoadLane('reports') after the
  // rows are in the DOM.
  const wrap = document.getElementById('al-filter-chips');
  if (!wrap) return;
  const rows = document.querySelectorAll('#reports-alerts-body details.al-signal-row');
  if (!rows.length) {
    wrap.style.display = 'none';
    wrap.innerHTML = '';
    return;
  }
  const producers = new Map();
  const bots = new Map();
  const sevs = new Map();
  rows.forEach(r => {
    const p = r.dataset.producer || '';
    const b = r.dataset.botId || '';
    const s = r.dataset.severity || '';
    if (p) producers.set(p, (producers.get(p) || 0) + 1);
    if (b) bots.set(b, (bots.get(b) || 0) + 1);
    if (s) sevs.set(s, (sevs.get(s) || 0) + 1);
  });
  // Drop dimensions where the count is exhaustive (every row has the
  // same single value) — a chip group with one option doesn't filter
  // anything and just adds noise.
  const dim = (label, key, m) => {
    if (m.size <= 1) return '';
    const entries = Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    const active = _alActiveFilters[key];
    const chips = entries.map(([val, count]) => {
      const isActive = active.has(val);
      const style = isActive
        ? 'background:var(--accent);color:#fff;border-color:var(--accent)'
        : 'background:var(--bg2);color:var(--text2);border-color:var(--border)';
      return `<button class="al-filter-chip" data-dim="${escHtml(key)}" data-val="${escHtml(val)}"
                onclick="_alToggleChip('${escHtml(key)}', '${escHtml(val)}')"
                style="${style};padding:3px 9px;border-radius:12px;font-size:0.74rem;cursor:pointer;border-width:1px;border-style:solid;line-height:1.5"
                title="Filter to ${escHtml(val)}">${escHtml(val)} <span style="opacity:0.7">(${count})</span></button>`;
    }).join(' ');
    return `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px">
              <span style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--text3);min-width:60px">${escHtml(label)}</span>
              ${chips}
            </div>`;
  };
  const anyActive = _alActiveFilters.producer.size + _alActiveFilters.bot.size + _alActiveFilters.severity.size > 0;
  const clearBtn = anyActive
    ? `<div style="margin-top:4px"><button class="btn btn-ghost btn-sm" onclick="_alClearChips()">Clear filters</button></div>`
    : '';
  wrap.innerHTML = `
    <div class="card" style="padding:10px 12px">
      ${dim('producer', 'producer', producers)}
      ${dim('bot', 'bot', bots)}
      ${dim('severity', 'severity', sevs)}
      ${clearBtn}
    </div>`;
  wrap.style.display = '';
  _alApplyChipFilters();
}

function _alToggleChip(dim, val) {
  if (!_alActiveFilters[dim]) return;
  const s = _alActiveFilters[dim];
  if (s.has(val)) s.delete(val); else s.add(val);
  _alRenderFilterChips();
}

function _alClearChips() {
  _alActiveFilters.producer.clear();
  _alActiveFilters.bot.clear();
  _alActiveFilters.severity.clear();
  _alRenderFilterChips();
}

function _alApplyChipFilters() {
  // AND across dimensions, OR within. For each row, hide if any
  // active dimension's set doesn't contain the row's value.
  const rows = document.querySelectorAll('#reports-alerts-body details.al-signal-row');
  rows.forEach(r => {
    const p = r.dataset.producer || '';
    const b = r.dataset.botId || '';
    const s = r.dataset.severity || '';
    let visible = true;
    if (_alActiveFilters.producer.size && !_alActiveFilters.producer.has(p)) visible = false;
    if (_alActiveFilters.bot.size && !_alActiveFilters.bot.has(b)) visible = false;
    if (_alActiveFilters.severity.size && !_alActiveFilters.severity.has(s)) visible = false;
    r.style.display = visible ? '' : 'none';
    // Uncheck hidden rows so the bulk bar's selection only ever shows
    // visible/actionable signals.
    if (!visible) {
      const cb = r.querySelector('.al-row-select');
      if (cb && cb.checked) { cb.checked = false; }
    }
  });
  _alSelectionChanged();
}

// ── Multi-select + sticky bulk-action bar ──────────────────────────────────

function _alSelectedIds() {
  return Array.from(
    document.querySelectorAll('#reports-alerts-body .al-row-select:checked')
  )
    .map(cb => cb.dataset.sigId)
    .filter(Boolean);
}

function _alSelectionChanged() {
  const ids = _alSelectedIds();
  const bar = document.getElementById('al-bulk-bar');
  if (!bar) return;
  const hasSelection = ids.length > 0;
  const countEl = document.getElementById('al-bulk-count');
  if (countEl) {
    countEl.textContent = `${ids.length} selected`;
    countEl.style.display = hasSelection ? '' : 'none';
  }
  bar.querySelectorAll('.al-bulk-action').forEach(btn => {
    btn.disabled = !hasSelection;
  });
}

function _alSelectAllVisible(checked) {
  const rows = document.querySelectorAll('#reports-alerts-body details.al-signal-row');
  rows.forEach(r => {
    if (r.style.display === 'none') return;
    const cb = r.querySelector('.al-row-select');
    if (cb) cb.checked = !!checked;
  });
  _alSelectionChanged();
}

function _alToggleBulkMenu(kind) {
  const target = document.getElementById(`al-bulk-menu-${kind}`);
  if (!target) return;
  const wasOpen = target.style.display === 'block';
  document.querySelectorAll('.al-bulk-menu-pop').forEach(el => el.style.display = 'none');
  if (!wasOpen) target.style.display = 'block';
}

// Close bulk menus on outside click — shared handler with row menus.
document.addEventListener('click', (ev) => {
  const inside = ev.target && ev.target.closest && ev.target.closest('.al-bulk-menu');
  if (!inside) {
    document.querySelectorAll('.al-bulk-menu-pop').forEach(el => el.style.display = 'none');
  }
}, true);

async function _alBulkPost(payload) {
  // Centralized bulk call. Returns the parsed JSON response, or throws.
  return await api('POST', '/api/signals/bulk-action', payload);
}

async function _alBulkSnooze(duration) {
  document.querySelectorAll('.al-bulk-menu-pop').forEach(el => el.style.display = 'none');
  const ids = _alSelectedIds();
  if (!ids.length) return;
  try {
    const resp = await _alBulkPost({ signal_ids: ids, action: 'snooze', duration });
    _alBulkToast(`Snoozed ${resp.applied}/${resp.total} signals for ${duration}.`, resp.failed);
    _alRefreshActive();
  } catch (e) {
    toast(`Bulk snooze failed: ${e}`, 'err');
  }
}

async function _alBulkResolve() {
  const ids = _alSelectedIds();
  if (!ids.length) return;
  try {
    const resp = await _alBulkPost({ signal_ids: ids, action: 'resolve' });
    _alBulkToast(`Resolved ${resp.applied}/${resp.total} signals.`, resp.failed);
    _alRefreshActive();
  } catch (e) {
    toast(`Bulk resolve failed: ${e}`, 'err');
  }
}

// Bulk dismiss routes through a confirm modal (Part 4) since the op
// is irreversible. The verdict comes from the bar's split-button menu;
// the count comes from the live selection at confirm-open time.
let _alBulkDismissPendingVerdict = null;
let _alBulkDismissPendingIds = null;

function _alBulkDismissConfirm(verdict) {
  document.querySelectorAll('.al-bulk-menu-pop').forEach(el => el.style.display = 'none');
  const ids = _alSelectedIds();
  if (!ids.length) return;
  const verdictLabels = {
    false_positive: 'False positive',
    bad_inference:  'Bad inference',
    not_actionable: 'Not actionable',
  };
  _alBulkDismissPendingVerdict = verdict;
  _alBulkDismissPendingIds = ids;
  const countEl = document.getElementById('al-bulk-confirm-count');
  const count2El = document.getElementById('al-bulk-confirm-count-2');
  const labelEl = document.getElementById('al-bulk-confirm-verdict-label');
  if (countEl) countEl.textContent = String(ids.length);
  if (count2El) count2El.textContent = String(ids.length);
  if (labelEl) labelEl.textContent = verdictLabels[verdict] || verdict;
  document.getElementById('al-bulk-confirm-modal').classList.add('open');
}

function _alBulkConfirmClose() {
  _alBulkDismissPendingVerdict = null;
  _alBulkDismissPendingIds = null;
  document.getElementById('al-bulk-confirm-modal').classList.remove('open');
}

async function _alBulkDismissSubmit() {
  if (!_alBulkDismissPendingVerdict || !_alBulkDismissPendingIds) return;
  const verdict = _alBulkDismissPendingVerdict;
  const ids = _alBulkDismissPendingIds;
  _alBulkConfirmClose();
  try {
    const resp = await _alBulkPost({
      signal_ids: ids, action: 'dismiss', verdict,
    });
    _alBulkToast(`Dismissed ${resp.applied}/${resp.total} signals (${verdict}).`, resp.failed);
    _alRefreshActive();
  } catch (e) {
    toast(`Bulk dismiss failed: ${e}`, 'err');
  }
}

function _alBulkToast(message, failed) {
  // Lightweight in-page toast — surfaces the bulk-op summary without
  // pulling in a full notification framework. Auto-fades after ~3s.
  const existing = document.getElementById('al-bulk-toast');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.id = 'al-bulk-toast';
  div.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:90;background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 16px;font-size:0.85rem;color:var(--text1);box-shadow:0 4px 12px rgba(0,0,0,0.3)';
  const failMsg = failed ? ` — ${failed} skipped (already-archived or missing)` : '';
  div.textContent = message + failMsg;
  document.body.appendChild(div);
  setTimeout(() => { try { div.remove(); } catch (_) {} }, 3500);
}

// ── Subscriptions tab (Configure) ──────────────────────────────────────────
//
// Single-fetch render from GET /api/alerts/subscriptions. Phase 2: the
// operator surface is the 13 Subscription GROUPS — each group row binds one
// enable/disable toggle to POST /api/alerts/subscriptions {subscription_id,
// enabled}, gating every member event. The member events render read-only in
// an expandable list. Safety-critical groups get a confirm-warning before
// muting.

async function _alLoadSubscriptions() {
  const body = document.getElementById('al-subs-body');
  if (!body) return;
  body.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  try {
    const data = await api('GET', '/api/alerts/subscriptions');
    body.innerHTML = _alSubsRender(data);
  } catch (e) {
    body.innerHTML = `<div style="color:var(--red);padding:14px">Failed to load subscriptions: ${escHtml(String(e))}</div>`;
  }
}

// Reports → Subscriptions → Messages: surface the dispatcher's send log
// so the operator can see what actually got sent (and to which channel)
// without leaving the UI.
//
// Failed sends are split out into the Dispatcher Health panel — the
// /api/alerts/dispatcher-log endpoint excludes them. Deferred-to-digest
// sends are split out the same way (their own dispatcher-queued.jsonl
// lane), so by default this rendering path shows only what actually
// reached the chat thread. The "include queued" checkbox adds the
// deferred lane back — including rows written before the split, which
// still physically live in dispatcher.jsonl.
//
// Row UI is truncate-and-expand: each message is rendered as a short
// preview (~120 chars) by default; clicking the row swaps in the full
// body. State lives on the <tr>'s data-expanded attribute; CSS does the
// rest. Click delegation: one listener on the tbody, no per-row binding.
//
// Re-entrancy: nav('reports') fires _alLoadActivity via onPageActivate,
// then immediately clicks the persisted subtab which fires it again via
// onSubTabActivate. The two async fetches would race and the second
// innerHTML write could detach DOM under a click in flight. The
// _alLoadActivity_inflight guard short-circuits the duplicate call so
// only one fetch is ever outstanding for the same body. Operator-side
// effect: a Reload button mash also collapses cleanly.
// Subscription-provenance map: catalog_event key → its GROUP metadata
// (group id / label / description / is_safety_critical / current group-enabled
// state, plus the member event's own severity). Phase 2: gating is per-GROUP,
// so a Messages row resolves its catalog_event → the group it belongs to and
// the inline control toggles the whole group. Built from GET
// /api/alerts/subscriptions (the 13 groups + event_index) and consulted when a
// row expands. Kept module-level so the expand handler can read it after the
// table HTML is rendered, and refreshed on every _alLoadActivity so the
// enabled-state stays current.
let _alCatalogMetaByKey = {};

// Build catalog_event → group-metadata map from the Phase-2 subscriptions
// payload. Each group carries its member `events`; we flatten the group's
// label/description/enabled/safety onto every member key so a single
// catalog_event lookup yields everything the provenance block needs to render
// the group and its group-level toggle. event_index (key → group id) is the
// authoritative mapping, but iterating groups[].events also gives us the
// member's own severity for the badge.
function _alBuildCatalogMeta(subsData) {
  const map = {};
  for (const g of (subsData && subsData.subscriptions) || []) {
    for (const e of (g.events || [])) {
      map[e.key] = {
        key:                 e.key,            // the member event key (the "subscription:" line)
        event_label:         e.label || '',
        severity:            e.severity || '',
        subscription_id:     g.id,
        group_label:         g.label || '',
        group_description:   g.description || '',
        is_safety_critical:  !!g.is_safety_critical,
        enabled:             !!g.enabled,       // the GROUP on/off — what the toggle controls
      };
    }
  }
  return map;
}

let _alLoadActivity_inflight = null;
async function _alLoadActivity() {
  const body = document.getElementById('al-activity-body');
  if (!body) return;
  if (_alLoadActivity_inflight) return _alLoadActivity_inflight;
  const includeSuppressed = document.getElementById('al-activity-include-suppressed')?.checked ? '1' : '0';
  const includeQueued = document.getElementById('al-activity-include-queued')?.checked ? '1' : '0';
  body.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  // Health panel can load in parallel — it reads from a different file and
  // its rendering doesn't depend on the messages list.
  _alLoadDispatcherHealth();
  _alLoadActivity_inflight = (async () => {
    try {
      // Fetch the dispatcher log and the subscription catalog in parallel —
      // the catalog resolves each row's catalog_event into its subscription
      // provenance + current enabled state for the expand block. If the
      // catalog fetch fails we still render messages (provenance degrades to
      // the raw key, no toggle).
      const [data, subsData] = await Promise.all([
        api('GET', `/api/alerts/dispatcher-log?limit=200&suppressed=${includeSuppressed}&queued=${includeQueued}`),
        api('GET', '/api/alerts/subscriptions').catch(() => null),
      ]);
      _alCatalogMetaByKey = _alBuildCatalogMeta(subsData);
      const records = (data && data.records) || [];
      if (!records.length) {
        // A pod whose whole feed is deferrals (heal config-drift on the
        // live pod: 1123 rows, zero sends) would otherwise read as "the
        // dispatcher is dead". Say what is actually true — nothing was
        // delivered, and there IS queued traffic behind the toggle.
        const hint = includeQueued === '1'
          ? ''
          : ' Messages queued for a future digest are hidden — tick <strong>include queued</strong> to see them.';
        body.innerHTML = `<div class="empty">No delivered messages yet. Subscription deliveries will appear here as they happen.${hint}</div>`;
        return;
      }
      body.innerHTML = _alActivityTableHtml(records);
      _alWireExpandableMessageRows(body);
    } catch (e) {
      body.innerHTML = `<div style="color:var(--red);padding:14px">Failed to load activity: ${escHtml(String(e))}</div>`;
    } finally {
      _alLoadActivity_inflight = null;
    }
  })();
  return _alLoadActivity_inflight;
}

// Build the resp-table HTML for the Recent Messages list. Pulled out so
// the Dispatcher Health "View failures" expansion can reuse it.
function _alActivityTableHtml(records) {
  const rows = records.map(_alActivityRowHtml).join('');
  return `<div class="resp-table-wrap"><table class="resp-table">
    <thead><tr>
      <th>When</th>
      <th>Source</th>
      <th>Result</th>
      <th>Channel</th>
      <th>Message</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function _alActivityResultPill(result) {
  const v = String(result || '').toLowerCase();
  if (v === 'sent')               return '<span class="badge badge-sm badge-ok">SENT</span>';
  // Queued for a future digest — not delivered yet. Only visible under
  // the "include queued" toggle (its own dispatcher-queued.jsonl lane).
  if (v === 'deferred')           return '<span class="badge badge-sm badge-neutral" title="Queued for a future digest — not yet delivered">QUEUED</span>';
  // Global rate breaker downgraded this alert into the daily digest (not
  // dropped) — the producer-agnostic flood backstop. See rate_breaker.py.
  if (v === 'batched_rate_cap')   return '<span class="badge badge-sm badge-neutral" title="Rate-capped — rolled into the daily digest">🌊 BATCHED</span>';
  if (v.startsWith('suppressed')) return `<span class="badge badge-sm badge-neutral">${escHtml(v.toUpperCase())}</span>`;
  if (v === 'failed')             return '<span class="badge badge-sm badge-crit">FAILED</span>';
  // Terminal, permanently-undeliverable send — surfaced in the audit so a
  // dead delivery channel is visible here, not silently absent.
  if (v === 'dead_letter')        return '<span class="badge badge-sm badge-crit">DEAD-LETTER</span>';
  return `<span class="badge badge-sm badge-neutral">${escHtml(v.toUpperCase())}</span>`;
}

// Render one <tr.alert-msg>. The message column holds two siblings:
//   • .alert-msg-truncated — visible by default, capped at ~120 chars
//   • .alert-msg-full      — hidden by default, full body preserved verbatim
// The truncate-vs-full switch is purely CSS-driven from data-expanded on
// the row. We escape independently so the truncated copy never strips
// content the user might want to see in the expanded view.
function _alActivityRowHtml(r) {
  const when = r.ts ? ago(r.ts) : '—';
  const channel = r.channel
    ? `<code style="font-size:0.74rem">${escHtml(r.channel)}</code>`
    : '<span style="color:var(--text3)">—</span>';
  const sev = (r.severity || '').toLowerCase();
  const sevCls = sev === 'critical' || sev === 'error' ? 'var(--red)'
                : sev === 'warn' || sev === 'warning' ? 'var(--yellow)'
                : sev === 'info' ? 'var(--text3)' : 'var(--text2)';
  const fullText = (r.message_excerpt || '').toString();
  const TRUNC = 120;
  const collapsedText = fullText.replace(/\n/g, ' ');
  const truncatedText = collapsedText.length > TRUNC
    ? collapsedText.slice(0, TRUNC).replace(/\s+\S*$/, '') + '…'
    : collapsedText;
  const errFullLine = r.error
    ? `<span class="alert-msg-error">${escHtml(r.error)}</span>` : '';
  // Provenance block — only meaningful when expanded; it lives inside
  // .alert-msg-full so CSS hides it in the collapsed preview. Resolves the
  // row's catalog_event into its subscription label/category/severity +
  // an inline enable/disable for the whole class.
  const provenance = _alMsgProvenanceHtml(r.catalog_event);
  return `<tr class="alert-msg" data-expanded="false">
    <td data-label="When" style="white-space:nowrap;color:var(--text3);font-size:0.78rem">${when}</td>
    <td data-label="Source" style="font-size:0.78rem;color:${sevCls}">${escHtml(r.source || '')}</td>
    <td data-label="Result">${_alActivityResultPill(r.result)}</td>
    <td data-label="Channel">${channel}</td>
    <td data-label="Message" class="alert-msg-body" style="font-size:0.78rem;color:var(--text2)">
      <span class="alert-msg-truncated">${truncatedText}</span><span class="alert-msg-full">${fullText}${errFullLine}${provenance}</span>
    </td>
  </tr>`;
}

// Severity → badge class, reused for the provenance line. Mirrors the
// Configure tab's severity vocabulary (info / warn / error / critical).
function _alSeverityBadge(severity) {
  const v = String(severity || '').toLowerCase();
  if (!v) return '';
  let cls = 'badge-neutral';
  if (v === 'critical' || v === 'error') cls = 'badge-crit';
  else if (v === 'warn' || v === 'warning') cls = 'badge-warn';
  else if (v === 'ok' || v === 'success') cls = 'badge-ok';
  return `<span class="badge badge-sm ${cls}">${escHtml(v.toUpperCase())}</span>`;
}

// Render the subscription-provenance block shown in an expanded message row.
// Phase 2: a message's catalog_event belongs to one of the 13 operator-facing
// GROUPS — provenance shows the GROUP it belongs to, and the inline control
// toggles the whole group. Resolves `catalogEvent` against the catalog map and
// emits:
//   • the group label + the member event's severity badge,
//   • the group description as secondary text,
//   • the event_key as a code tag (the "subscription:" line on the phone),
//   • an inline enable/disable button wired to the group POST.
// Degrades gracefully when the key is missing or unresolvable: shows the raw
// key (or nothing) and no toggle. Post-Phase-1 every dispatch carries a
// resolvable catalog_event, so the degraded paths should be rare.
function _alMsgProvenanceHtml(catalogEvent) {
  if (!catalogEvent) {
    // No catalog_event on this record at all (pre-workstream-A history).
    return '';
  }
  const meta = _alCatalogMetaByKey[catalogEvent];
  if (!meta) {
    // Key present but unresolvable against the live catalog — show the raw
    // key so provenance is still legible, but offer no toggle. (e.g. an
    // internalized event that's no longer an operator subscription.)
    return `<div class="alert-msg-prov">
      <div class="alert-msg-prov-head">Subscription</div>
      <code class="key-tag">${escHtml(catalogEvent)}</code>
      <div class="alert-msg-prov-note">Not part of an operator subscription &mdash; can't toggle from here.</div>
    </div>`;
  }
  const sevBadge = _alSeverityBadge(meta.severity);
  const safetyBadge = meta.is_safety_critical
    ? `<span class="badge badge-sm badge-warn" title="Disabling this group silences a safety-critical alert">Safety-critical</span>`
    : '';
  const descLine = meta.group_description
    ? `<div class="alert-msg-prov-desc">${escHtml(meta.group_description)}</div>`
    : '';
  const enabled = !!meta.enabled;
  const safetyAttr = meta.is_safety_critical ? ' data-safety-critical="1"' : '';
  // Toggle button reflects the GROUP's current state. Enabled → "Disable",
  // disabled → "Enable". onclick stopPropagation so the button click doesn't
  // also collapse the row via the table-level expand delegation.
  const toggleBtn = `<button class="btn btn-sm ${enabled ? 'btn-ghost' : 'btn-primary'} alert-msg-prov-toggle"
        data-subscription-id="${escHtml(meta.subscription_id)}"${safetyAttr}
        data-enabled="${enabled ? '1' : '0'}"
        onclick="event.stopPropagation(); _alMsgToggleSubscription(this)"
        title="${enabled ? 'Stop sending this subscription group' : 'Resume sending this subscription group'}">
      ${enabled ? 'Disable this subscription' : 'Enable this subscription'}
    </button>`;
  return `<div class="alert-msg-prov">
    <div class="alert-msg-prov-head">
      <span class="alert-msg-prov-label">${escHtml(meta.group_label || meta.subscription_id)}</span>
      ${safetyBadge}${sevBadge}
    </div>
    ${descLine}
    <div class="alert-msg-prov-foot">
      <code class="key-tag" title="Subscription key &mdash; matches the &quot;subscription:&quot; line in your message footer">${escHtml(meta.key)}</code>
      ${toggleBtn}
    </div>
  </div>`;
}

// Delegate the click → toggle so we don't bind a listener per row. Uses
// closest('tr.alert-msg') so clicks inside child elements (codes, pills)
// still hit the row.
function _alWireExpandableMessageRows(container) {
  if (!container || container.dataset.alertMsgWired === '1') return;
  container.dataset.alertMsgWired = '1';
  container.addEventListener('click', (ev) => {
    const tr = ev.target.closest && ev.target.closest('tr.alert-msg');
    if (!tr || !container.contains(tr)) return;
    tr.setAttribute(
      'data-expanded',
      tr.getAttribute('data-expanded') === 'true' ? 'false' : 'true'
    );
  });
}

// Inline enable/disable for the subscription GROUP a message belongs to,
// invoked from the provenance block's toggle button. Phase 2: gating is per
// group, so this POSTs {subscription_id, enabled} (the same surface the
// Configure tab's group toggle uses).
//
// Safety-critical guard: disabling a group flagged is_safety_critical pops a
// confirm warning first (operator decision: toggle-WITH-warning, not block).
// Enabling needs no confirm. This mirrors the Configure tab's
// _alSubsGroupToggle.
//
// On success we update every module-level catalog entry that belongs to the
// same group and re-paint every currently-visible provenance toggle for that
// group, so other expanded rows reflect the new state without a full reload.
async function _alMsgToggleSubscription(btn) {
  const subscriptionId = btn.dataset.subscriptionId;
  if (!subscriptionId) return;
  const currentlyEnabled = btn.dataset.enabled === '1';
  const willEnable = !currentlyEnabled;

  if (!willEnable && btn.dataset.safetyCritical === '1') {
    const ok = await confirmModal({body: (
      'You\'re disabling a safety-critical subscription. You won\'t be ' +
      'notified when alerts in this group fire. Continue?'
    ), danger: true});
    if (!ok) return;
  }

  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = willEnable ? 'Enabling…' : 'Disabling…';
  try {
    // `api()` resolves to `{error}` on an HTTP error rather than throwing, so
    // guard the success path explicitly — otherwise a failed POST would falsely
    // flip the toggle and the repaint would make it sticky until a reload
    // (same idiom as the proposal-act handler above).
    const r = await api('POST', '/api/alerts/subscriptions', {
      subscription_id: subscriptionId,
      enabled: willEnable,
    });
    if (r && r.error) throw new Error(r.error);
    // Keep the provenance map current so freshly-rendered rows + the repaint
    // below agree with what's now on disk. Every member event of the group
    // shares the group's enabled state.
    for (const k of Object.keys(_alCatalogMetaByKey)) {
      if (_alCatalogMetaByKey[k].subscription_id === subscriptionId) {
        _alCatalogMetaByKey[k].enabled = willEnable;
      }
    }
    _alRepaintProvenanceToggles(subscriptionId, willEnable);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    toast(`Failed to update subscription: ${e}`, 'err');
  }
}

// Re-paint every visible provenance toggle for `subscriptionId` to reflect the
// new enabled state — keeps multiple expanded rows of the same group in sync.
function _alRepaintProvenanceToggles(subscriptionId, enabled) {
  const sel = `.alert-msg-prov-toggle[data-subscription-id="${(window.CSS && CSS.escape) ? CSS.escape(subscriptionId) : subscriptionId}"]`;
  document.querySelectorAll(sel).forEach((b) => {
    b.dataset.enabled = enabled ? '1' : '0';
    b.disabled = false;
    b.textContent = enabled ? 'Disable this subscription' : 'Enable this subscription';
    b.title = enabled
      ? 'Stop sending this subscription group'
      : 'Resume sending this subscription group';
    b.classList.toggle('btn-ghost', enabled);
    b.classList.toggle('btn-primary', !enabled);
  });
}

// ── Dispatcher Health panel ─────────────────────────────────────────────────
//
// Reads /api/alerts/delivery-failures (24h window + a digest snapshot) and
// renders a compact summary above the Recent Messages list:
//   • All-green:  "✓ All deliveries succeeded in last 24h." — only when the
//                 server's summary.healthy roll-up is true.
//   • Degraded:   one block per problem class that's present —
//                 - transport failures (per-channel aggregate + "View
//                   failures →" expansion, same .alert-msg pattern),
//                 - non-delivery (no_recipient: the operator subscribed but
//                   no recipient resolved — a silently-dead delivery mode),
//                 - digest pipeline stuck (queue not draining).
//
// The honesty fix (R3): the panel used to key off the failure count alone,
// so it showed "✓ all deliveries succeeded" while every digest silently
// died at no_recipient. summary.healthy now folds in non-delivery + digest
// staleness so a silent non-delivery can't read as green.
//
// Hidden entirely on the (rare) "failures endpoint errored" path so the
// pane doesn't show a confusing red bar when the actual deliveries are
// fine — operators can refresh to retry.
//
// In-flight guard mirrors _alLoadActivity — the two are called as a
// pair from nav() → onPageActivate AND nav() → onSubTabActivate, so the
// health panel races identically. Without this, the "View failures →"
// toggle could be detached under a click in flight.
let _alLoadDispatcherHealth_inflight = null;
async function _alLoadDispatcherHealth() {
  const host = document.getElementById('al-dispatcher-health');
  if (!host) return;
  if (_alLoadDispatcherHealth_inflight) return _alLoadDispatcherHealth_inflight;
  _alLoadDispatcherHealth_inflight = (async () => {
    try {
      let data;
      try {
        data = await api('GET', '/api/alerts/delivery-failures?window_hours=24&limit=200');
      } catch (e) {
        host.innerHTML = '';
        return;
      }
      host.innerHTML = _alDispatcherHealthHtml(data || {});
      const detailHost = host.querySelector('.dispatcher-health-detail');
      if (detailHost) _alWireExpandableMessageRows(detailHost);
    } finally {
      _alLoadDispatcherHealth_inflight = null;
    }
  })();
  return _alLoadDispatcherHealth_inflight;
}

// Truncate an error string for the compact channel/source preview line.
function _alHealthErrPreview(sampleError) {
  const errPreview = (sampleError || '').toString();
  const truncated = errPreview.length > 90
    ? errPreview.slice(0, 90).replace(/\s+\S*$/, '') + '…'
    : errPreview;
  return truncated
    ? ` <span style="color:var(--text3)">${escHtml(truncated)}</span>` : '';
}

function _alDispatcherHealthHtml(data) {
  const summary = data.summary || {};
  const records = data.records || [];
  const windowHours = summary.window_hours || 24;
  const failTotal = summary.total | 0;
  const nonDelivery = summary.non_delivery || {};
  const ndTotal = nonDelivery.total | 0;
  const digest = summary.digest || {};

  // summary.healthy is the server's roll-up (no transport failures, no
  // non-delivery, no stuck digest). Older servers won't send it — fall
  // back to the union of the signals we can see so a partial deploy still
  // renders honestly rather than defaulting to green.
  const healthy = ('healthy' in summary)
    ? !!summary.healthy
    : (failTotal === 0 && ndTotal === 0 && !digest.stuck);

  if (healthy) {
    return `<div class="dispatcher-health ok">
      <div class="dispatcher-health-title">Dispatcher health</div>
      <div class="dispatcher-health-line">✓ All deliveries succeeded in the last ${windowHours}h.</div>
    </div>`;
  }

  // ── Transport-failure block ───────────────────────────────────────────
  let failuresBlock = '';
  if (failTotal > 0) {
    const byChannel = (summary.by_channel || []).map(s =>
      `<div class="dispatcher-health-channel-line">
        • <code>${escHtml(s.channel)}</code> (${s.count} fail${s.count === 1 ? '' : 's'}):${_alHealthErrPreview(s.sample_error)}
      </div>`).join('');
    const mostRecent = summary.most_recent_ts ? ago(summary.most_recent_ts) : '—';
    const failuresTable = records.length ? _alActivityTableHtml(records) : '';
    failuresBlock = `<div class="dispatcher-health-section">${failTotal} deliver${failTotal === 1 ? 'y' : 'ies'} failed in the last ${windowHours}h</div>
      ${byChannel}
      <div class="dispatcher-health-line">Most-recent failure: ${mostRecent}</div>
      <span class="dispatcher-health-toggle" onclick="_alToggleDispatcherHealthDetail(this)">View failures →</span>
      <div class="dispatcher-health-detail">${failuresTable}</div>`;
  }

  // ── Non-delivery block (no_recipient) ─────────────────────────────────
  let nonDeliveryBlock = '';
  if (ndTotal > 0) {
    const bySource = (nonDelivery.by_source || []).map(s =>
      `<div class="dispatcher-health-channel-line">
        • <code>${escHtml(s.source)}</code> (${s.count}):${_alHealthErrPreview(s.sample_error)}
      </div>`).join('');
    const ndRecent = nonDelivery.most_recent_ts ? ago(nonDelivery.most_recent_ts) : '—';
    nonDeliveryBlock = `<div class="dispatcher-health-section">${ndTotal} alert${ndTotal === 1 ? '' : 's'} not delivered — no recipient — in the last ${windowHours}h</div>
      ${bySource}
      <div class="dispatcher-health-line">No alert channel resolved (check network.json → alerts). Most recent: ${ndRecent}</div>`;
  }

  // ── Digest-pipeline block ─────────────────────────────────────────────
  let digestBlock = '';
  if (digest.stuck) {
    // Distinguish the two failure shapes (server-supplied diagnosis):
    //   never_flushed — queue non-empty but NEVER delivered → the flush
    //                   service is down / mis-scheduled / has no recipient.
    //   queue_growing — service HAS flushed but the queue still ages past
    //                   the threshold → throughput-bound: recurring signals
    //                   re-enqueuing faster than one flush coalesces them.
    //                   Point at the producers, not the (healthy) service.
    // Older servers omit `diagnosis`; fall back to the last_flush presence.
    const freqDiag = (key) => {
      const d = digest[key] || {};
      if (!d.stuck) return null;
      return d.diagnosis || (d.last_flush_ts ? 'queue_growing' : 'never_flushed');
    };
    const diags = ['daily', 'weekly'].map(freqDiag).filter(Boolean);
    const anyNeverFlushed = diags.includes('never_flushed');

    const freqLine = (key, label) => {
      const d = digest[key] || {};
      if (!d.stuck) return '';
      const age = (d.oldest_pending_age_hours != null)
        ? `oldest ${_alHumanHours(d.oldest_pending_age_hours)} old`
        : 'oldest unknown';
      // last_flush_ts is null when never delivered (or unparseable — the
      // server now drops malformed stamps). ago() also guards NaN.
      const last = d.last_flush_ts ? ago(d.last_flush_ts) : 'never';
      return `<div class="dispatcher-health-channel-line">
        • ${escHtml(label)}: ${d.pending | 0} pending, ${age}, last delivered ${escHtml(last)}
      </div>`;
    };

    // Honest guidance per shape. OS-agnostic: the flush worker is a
    // LaunchDaemon on macOS and a systemd timer on Linux — name it
    // generically rather than hardcoding the macOS label.
    const guidance = anyNeverFlushed
      ? `Digests have queued but none have ever been delivered — the digest-flush service may be stopped, mis-scheduled, or have no alert recipient. Check that the digest-flush worker (LaunchDaemon on macOS / systemd timer on Linux) is running and that an alert channel is configured.`
      : `The digest-flush service is delivering on schedule, but the queue keeps growing faster than one flush can drain — usually a recurring signal re-firing into the digest. Review the Subscriptions in digest mode and quiet the noisy producer rather than the flush service.`;

    digestBlock = `<div class="dispatcher-health-section">Digest queue not draining</div>
      ${freqLine('daily', 'Daily')}
      ${freqLine('weekly', 'Weekly')}
      <div class="dispatcher-health-line">${guidance}</div>`;
  }

  return `<div class="dispatcher-health degraded" data-failures-expanded="false">
    <div class="dispatcher-health-title">⚠ Dispatcher health: delivery problems detected</div>
    ${failuresBlock}
    ${nonDeliveryBlock}
    ${digestBlock}
  </div>`;
}

// Render an age in hours as a compact "Nh" / "N.Nd" string for the digest
// staleness line (the API gives age in hours; days read better past ~1d).
function _alHumanHours(hours) {
  const h = Number(hours) || 0;
  if (h >= 48) return `${(h / 24).toFixed(1)}d`;
  if (h >= 1) return `${Math.round(h)}h`;
  return '<1h';
}

function _alToggleDispatcherHealthDetail(el) {
  const card = el.closest('.dispatcher-health');
  if (!card) return;
  const open = card.getAttribute('data-failures-expanded') === 'true';
  card.setAttribute('data-failures-expanded', open ? 'false' : 'true');
  el.textContent = open ? 'View failures →' : 'Hide failures';
}

function _alSubsRender(data) {
  const channel = data.channel || '<em>not configured</em>';
  const chatId = data.chat_id || '<em>—</em>';

  // Master kill-switch banner: when the dispatcher is disabled, every
  // send returns suppressed_disabled. Show this above everything else
  // so operators see why nothing is firing. Per-source disables don't
  // need a UI hint — the server filters those events out entirely.
  const dispatcherBanner = data.dispatcher_enabled === false ? `
    <div style="padding:10px 12px;background:var(--bg2);border:1px solid var(--red);border-radius:6px;margin-bottom:14px;font-size:0.82rem;color:var(--red)">
      ⚠️ Dispatcher master switch is off (<code>alerts.dispatcher_enabled</code>). No alerts will be sent until this is turned back on.
    </div>
  ` : '';

  // "Send to:" row — PWA Phase 1.2 §6.4. Today the primary chat is
  // always one of slack/telegram/discord (shown read-only here; channel
  // is configured at the network/wizard layer). PWA Push is the only
  // additive surface, with its own per-device subscriptions below.
  const pwaActive = data.pwa_push_enabled === true;
  const head = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;padding:10px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;margin-bottom:18px">
      <div>
        <div style="font-size:0.8rem;color:var(--text2);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Send to</div>
        <div style="display:flex;flex-direction:column;gap:4px;font-size:0.85rem">
          <label style="display:flex;align-items:center;gap:6px;color:var(--text2);cursor:not-allowed;user-select:none" title="The primary chat channel is configured in Settings → Pod Config and is always on.">
            <input type="checkbox" checked disabled style="cursor:not-allowed">
            <span><code>${escHtml(channel)}</code> · ${escHtml(chatId)}</span>
          </label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none">
            <input type="checkbox" id="al-pwa-push-toggle"
                   ${pwaActive ? 'checked' : ''}
                   onchange="_alPwaPushChannelToggle(this)">
            <span>PWA Push — every registered device</span>
          </label>
        </div>
      </div>
      <div style="font-size:0.85rem;color:var(--text2);display:flex;align-items:center;gap:8px">
        <label for="al-digest-hour">Digest hour:</label>
        <select id="al-digest-hour" class="input-w-auto"
                style="font-size:0.78rem;padding:3px 8px"
                onchange="_alSubsDigestHour(this)"
                title="Local hour at which the daily/weekly digest flushes. Interpreted via network.timezone.">
          ${Array.from({length: 24}, (_, h) => {
            const hh = String(h).padStart(2, '0');
            const sel = (data.digest_hour_local || 8) === h ? ' selected' : '';
            return `<option value="${h}"${sel}>${hh}:00</option>`;
          }).join('')}
        </select>
        <span style="color:var(--text3)">local</span>
      </div>
    </div>
  `;

  // Devices receiving push — fetched lazily, rendered into this div
  // after the main render completes. Living below the "Send to" row
  // makes the relationship visual: turn on PWA Push above, manage
  // the device list here.
  const devicesSection = `
    <div id="al-pwa-devices" style="margin-bottom:22px"></div>
  `;
  // Kick off the devices fetch — fires off a microtask so it doesn't
  // block the synchronous render path. (Errors handled inside.)
  setTimeout(_alLoadPushDevices, 0);

  // Phase 2: the operator surface is the 13 Subscription GROUPS, not the
  // ~65 per-event toggles. Each group renders one enable/disable toggle that
  // gates every member event; the member list is read-only (expandable) so
  // the operator can see what's inside without per-event controls.
  const groups = data.subscriptions || [];
  const intro = `
    <div style="font-size:0.8rem;color:var(--text3);margin-bottom:14px">
      ${groups.length} subscription group${groups.length === 1 ? '' : 's'} — each gates a coherent
      class of messages. Expand a group to see the underlying events it covers.
    </div>
  `;
  const sections = groups.length
    ? `<div style="display:flex;flex-direction:column;gap:8px">${groups.map(g => _alSubsGroupRow(g)).join('')}</div>`
    : '<div class="empty">No subscription groups available — every underlying signal source is disabled in Settings → Pod Config.</div>';

  const footer = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:18px;padding-top:14px;border-top:1px solid var(--border)">
      <div style="font-size:0.8rem;color:var(--text3)">Changes save automatically.</div>
      <button class="btn btn-ghost btn-sm" onclick="_alSubsReset()" title="Wipe operator overrides; everything reverts to catalog defaults">Reset all to defaults</button>
    </div>
  `;

  return dispatcherBanner + head + devicesSection + intro + sections + footer;
}

// ── PWA Push: Devices receiving push (Phase 1.2 §6.6 + spec §6.4) ──────────
//
// Lists every browser/device registered against this pod's VAPID key.
// Each row shows the device label, push host (so the operator can tell
// Apple from Google from Mozilla at a glance), and a Remove button that
// calls POST /api/push/unsubscribe.
//
// The "Enable on this device" button at the top hides itself when the
// browser doesn't support service workers + Push (every modern browser
// does, but check explicitly so a fallback browser shows something
// helpful). On iOS Safari we additionally check standalone-mode — see
// _alEnablePushOnThisDevice.

async function _alLoadPushDevices() {
  const host = document.getElementById('al-pwa-devices');
  if (!host) return;
  host.innerHTML = '<div style="font-size:0.78rem;color:var(--text3);padding:6px 0">Loading devices…</div>';
  try {
    const data = await api('GET', '/api/push/subscriptions');
    host.innerHTML = _alPushDevicesRender(data);
  } catch (e) {
    host.innerHTML = `<div style="font-size:0.78rem;color:var(--red);padding:6px 0">Could not load devices: ${escHtml(String(e))}</div>`;
  }
}

function _alPushDevicesRender(data) {
  const subs = (data && data.subscriptions) || [];
  const blocker = _alPushEnableBlocker();
  const enableButton = blocker
    ? `<div style="font-size:0.78rem;color:var(--text2);padding:6px 10px;background:var(--bg2);border:1px dashed var(--border);border-radius:6px;max-width:520px">${blocker}</div>`
    : `<button class="btn btn-primary btn-sm" onclick="_alEnablePushOnThisDevice(this)" style="font-size:0.8rem">Enable on this device</button>`;

  const rows = subs.length ? subs.map(s => {
    const labelEsc = escHtml(s.device_label || 'Unnamed device');
    const host = escHtml(s.endpoint_host || '');
    const created = s.created_at ? ago(s.created_at) : '—';
    const last = s.last_delivery
      ? `last push ${ago(s.last_delivery)}`
      : '<span style="color:var(--text3)">no pushes yet</span>';
    const failure = s.last_failure
      ? `<div style="color:var(--yellow);font-size:0.72rem;margin-top:2px">⚠ ${escHtml(s.last_failure)}</div>`
      : '';
    const invalidPill = s.invalid
      ? ' <span style="font-size:0.7rem;padding:1px 6px;background:var(--bg);border:1px solid var(--red);color:var(--red);border-radius:10px;margin-left:4px">unreachable</span>'
      : '';
    return `
      <div style="display:grid;grid-template-columns:1fr auto auto;gap:14px;align-items:center;padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:6px">
        <div>
          <div style="font-weight:500">${labelEsc}${invalidPill}</div>
          <div style="font-size:0.72rem;color:var(--text3);margin-top:2px"><code>${host}</code> · registered ${created} · ${last}</div>
          ${failure}
        </div>
        <button class="btn btn-ghost btn-sm" style="font-size:0.74rem"
                onclick="_alPushTestOne('${escHtml(s.id)}', this)"
                title="Send a single test push to this device">Test</button>
        <button class="btn btn-ghost btn-sm" style="font-size:0.74rem;color:var(--red)"
                data-endpoint="${escHtml(s.endpoint || '')}"
                onclick="_alPushRemoveDevice('${escHtml(s.id)}', '${escHtml(s.device_label || 'this device')}', this.dataset.endpoint)"
                title="Remove this device's subscription">Remove</button>
      </div>
    `;
  }).join('') : `
    <div style="padding:10px 12px;font-size:0.8rem;color:var(--text3);background:var(--bg2);border:1px solid var(--border);border-radius:6px">
      No devices receiving push yet. Click <strong>Enable on this device</strong> to register the browser you're using now.
    </div>
  `;

  return `
    <div style="font-size:0.85rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:0.6px;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">
      📱 Devices receiving push
    </div>
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:8px">
      <div style="font-size:0.78rem;color:var(--text3);max-width:540px">
        Each device installs the PWA and registers once; pushes then go to the device whether or not it's on the tailnet.
      </div>
      <div>${enableButton}</div>
    </div>
    <div style="display:flex;flex-direction:column;gap:6px">
      ${rows}
    </div>
  `;
}

// Why the Enable button might be hidden:
//   • Browser doesn't expose serviceWorker / PushManager / Notification
//     APIs (rare today; old Edge, restricted contexts).
//   • Page is on http:// — Web Push requires a secure context.
//   • iOS Safari in browser tab — Apple only delivers Web Push to PWAs
//     added to the home screen (iOS 16.4+). Subscribing from Safari
//     would succeed silently but no pushes would ever arrive, which is
//     the worst failure mode. We refuse cleanly with the install hint.
//   • The user already permanently denied notification permission for
//     this origin. Calling Notification.requestPermission() returns
//     'denied' immediately without re-prompting — clicking the Enable
//     button just toasts 'denied' every time. Show the operator how to
//     reverse the choice instead of pretending it's still actionable.
function _alPushEnableBlocker() {
  if (typeof navigator === 'undefined') return 'Browser unavailable.';

  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    return 'This browser doesn\'t support Web Push. Try Chrome, Firefox, Edge, or Safari 16.4+.';
  }
  if (location.protocol !== 'https:' && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
    return 'Web Push requires HTTPS. Enable HTTPS for your pod first (see Pod settings → Connectivity).';
  }
  // iOS detection (Phase 1.2 §6.7 + the "gotcha" callout). Chrome on
  // iOS is still WebKit under the hood; navigator.standalone is the
  // signal that distinguishes home-screen PWA mode from a Safari tab.
  // We check for iPhone/iPad/iPod in the UA, then *also* require
  // standalone mode to be true before allowing subscribe.
  const ua = navigator.userAgent || '';
  const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (isIOS) {
    // navigator.standalone is iOS-Safari-specific; in the PWA it's true.
    // In a regular Safari tab (or Chrome-on-iOS tab) it's false/undefined.
    if (!('standalone' in navigator) || navigator.standalone !== true) {
      return 'On iPhone/iPad, install Evolve to your home screen first (Share → Add to Home Screen). Then open Evolve from the home screen and tap Enable here.';
    }
  }
  if (Notification.permission === 'denied') {
    return 'Notifications are blocked for this site. Re-enable them in your browser\'s site settings (lock/info icon in the address bar → Notifications → Allow), then reload.';
  }
  return null;
}

// Decode a URL-safe base64 string into a Uint8Array — required shape
// for pushManager.subscribe({applicationServerKey}). Web Push uses the
// unpadded urlsafe form; restore padding before atob.
function _alB64UrlToUint8(b64url) {
  const pad = '='.repeat((4 - b64url.length % 4) % 4);
  const b64 = (b64url + pad).replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// Best-effort device label from the UA. Mirrors the server-side
// fallback in push_routes._label_from_user_agent so the operator sees
// the same string whether they let the client or the server fill it in.
function _alPushDeviceLabel() {
  const ua = (navigator.userAgent || '').toLowerCase();
  const device =
    ua.includes('iphone') ? 'iPhone'
    : ua.includes('ipad') ? 'iPad'
    : ua.includes('android') ? 'Android'
    : ua.includes('macintosh') || ua.includes('mac os') ? 'Mac'
    : ua.includes('windows') ? 'Windows'
    : ua.includes('linux') ? 'Linux'
    : 'Device';
  const browser =
    (ua.includes('edg/') || ua.includes('edgios')) ? 'Edge'
    : (ua.includes('firefox') || ua.includes('fxios')) ? 'Firefox'
    : (ua.includes('chrome') || ua.includes('crios')) ? 'Chrome'
    : ua.includes('safari') ? 'Safari'
    : 'Browser';
  return `${device} ${browser}`;
}

async function _alEnablePushOnThisDevice(btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Registering…';
  try {
    // 1. Get the SW registration (already installed on page load).
    const reg = await navigator.serviceWorker.ready;

    // 2. Ask the browser for notification permission. iOS only prompts
    //    from a user gesture — this onclick handler qualifies.
    let perm = Notification.permission;
    if (perm === 'default') {
      perm = await Notification.requestPermission();
    }
    if (perm !== 'granted') {
      toast('Notification permission denied; nothing was registered.', 'err');
      return;
    }

    // 3. Fetch the pod's VAPID public key, then subscribe.
    const vapid = await api('GET', '/api/push/vapid-public-key');
    if (!vapid || !vapid.public_key) {
      toast('Could not fetch VAPID key from the admin server.', 'err');
      return;
    }
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: _alB64UrlToUint8(vapid.public_key),
    });

    // 4. POST the subscription to the admin server.
    const sj = sub.toJSON();
    const out = await api('POST', '/api/push/subscribe', {
      endpoint:     sj.endpoint,
      keys:         sj.keys,
      device_label: _alPushDeviceLabel(),
      user_agent:   navigator.userAgent,
    });
    toast(`Registered ${out.device_label || 'this device'} for push.`, 'ok');
    // Re-render the devices section and the channel-toggle row (subscribing
    // also auto-flips the pwa_push channel on the first time).
    _alLoadSubscriptions();
  } catch (e) {
    console.error('push subscribe failed', e);
    toast(`Push registration failed: ${e && e.message ? e.message : e}`, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function _alPushRemoveDevice(subscriptionId, label, removedEndpoint) {
  if (!await confirmModal({body: `Stop sending push notifications to ${label}?`, danger: true})) return;
  try {
    await api('POST', '/api/push/unsubscribe', { subscription_id: subscriptionId });
    // Tear down the local browser-side subscription ONLY if the row
    // the operator removed is *this* browser's own subscription.
    // Removing another device's row must not blow away the laptop /
    // phone we're currently using to manage devices.
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = reg && (await reg.pushManager.getSubscription());
      if (sub && sub.endpoint && removedEndpoint && sub.endpoint === removedEndpoint) {
        await sub.unsubscribe();
      }
    } catch (_e) {
      // Local teardown is best-effort.
    }
    _alLoadPushDevices();
  } catch (e) {
    toast(`Remove failed: ${e}`, 'err');
  }
}

async function _alPushTestOne(subscriptionId, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    const out = await api('POST', '/api/push/test', { subscription_id: subscriptionId });
    btn.textContent = out.result === 'sent' ? 'Sent ✓' : `(${out.result || 'failed'})`;
  } catch (e) {
    btn.textContent = 'Failed';
    console.error('push test failed', e);
  } finally {
    setTimeout(() => {
      btn.textContent = original;
      btn.disabled = false;
      _alLoadPushDevices();
    }, 2200);
  }
}

async function _alPwaPushChannelToggle(checkbox) {
  try {
    await api('POST', '/api/push/channel', { enabled: checkbox.checked });
  } catch (e) {
    toast(`Could not save channel setting: ${e}`, 'err');
    checkbox.checked = !checkbox.checked;
  }
}

// Phase 2: render one Subscription GROUP (not a per-event row). The toggle
// gates the whole group; the member events are shown read-only inside an
// expandable <details> so the operator can see what a group covers without
// any per-event control (gating moved per-event → per-group in the backend).
function _alSubsGroupRow(g) {
  const enabled = !!g.enabled;
  const overrideMark = g.is_overridden
    ? ` <span title="Operator override" style="color:var(--text3);font-size:0.7rem">●</span>`
    : '';
  const safetyAttr = g.is_safety_critical ? ' data-safety-critical="1"' : '';
  const safetyBadge = g.is_safety_critical
    ? ` <span class="badge badge-sm badge-warn" title="Disabling this group silences a safety-critical alert">Safety-critical</span>`
    : '';
  const memberCount = g.member_event_count != null
    ? g.member_event_count
    : (g.events || []).length;

  // Read-only member list, behind a .expand-icon chevron (style-guide §9.13).
  // No per-member control — the group is the unit of operator action.
  const memberRows = (g.events || []).map(e => {
    const ev = e || {};
    const freq = (ev.subscription && ev.subscription.frequency) || ev.default_frequency || '';
    return `
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:5px 0;border-top:1px solid var(--border)">
        <div>
          <div style="font-size:0.8rem;color:var(--text)">${escHtml(ev.label || ev.key || '')}</div>
          <div style="font-size:0.74rem;color:var(--text3);margin-top:1px">
            <code class="key-tag" style="font-size:0.72rem;color:var(--text3)">${escHtml(ev.key || '')}</code>${freq ? ' &middot; ' + escHtml(freq) : ''}
          </div>
        </div>
        <button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 8px;flex-shrink:0"
                onclick="event.stopPropagation(); _alSubsTest('${escHtml(ev.key || '')}', this)"
                title="Send a sample message for this event right now">Test</button>
      </div>
    `;
  }).join('');

  const memberSection = memberRows
    ? `
      <details style="margin-top:8px">
        <summary style="font-size:0.76rem;color:var(--text3);display:inline-flex;align-items:center;gap:5px;cursor:pointer;user-select:none">
          <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
          ${memberCount} event${memberCount === 1 ? '' : 's'} in this group
        </summary>
        <div style="margin-top:4px">${memberRows}</div>
      </details>`
    : `<div style="margin-top:6px;font-size:0.76rem;color:var(--text3)">${memberCount} event${memberCount === 1 ? '' : 's'} in this group</div>`;

  return `
    <div class="al-subs-group" data-subscription-id="${escHtml(g.id)}"${safetyAttr}
         style="padding:12px 14px;background:var(--bg2);border:1px solid var(--border);border-radius:6px">
      <div style="display:grid;grid-template-columns:1fr auto;gap:14px;align-items:start">
        <div>
          <div style="font-weight:600">${escHtml(g.label || g.id)}${safetyBadge}${overrideMark}</div>
          <div style="font-size:0.78rem;color:var(--text3);margin-top:2px">${escHtml(g.description || '')}</div>
        </div>
        <label style="display:flex;align-items:center;gap:6px;font-size:0.8rem;color:var(--text2);user-select:none;cursor:pointer">
          <input type="checkbox"
                 ${enabled ? 'checked' : ''}
                 onchange="_alSubsGroupToggle('${escHtml(g.id)}', this)"
                 style="cursor:pointer">
          <span>${enabled ? 'On' : 'Off'}</span>
        </label>
      </div>
      ${memberSection}
    </div>
  `;
}

// Toggle a whole Subscription group. Disabling a safety-critical group pops
// the confirm-warning (operator decision: toggle-WITH-warning, not block).
async function _alSubsGroupToggle(subscriptionId, checkbox) {
  if (!checkbox.checked) {
    const row = checkbox.closest('.al-subs-group');
    if (row && row.dataset.safetyCritical === '1') {
      const ok = await confirmModal({body: (
        'You\'re disabling a safety-critical subscription. You won\'t be ' +
        'notified when alerts in this group fire. Continue?'
      ), danger: true});
      if (!ok) { checkbox.checked = true; return; }
    }
  }
  try {
    const r = await api('POST', '/api/alerts/subscriptions', {
      subscription_id: subscriptionId,
      enabled: checkbox.checked,
    });
    if (r && r.error) throw new Error(r.error);
  } catch (e) {
    toast(`Failed to save subscription: ${e}`, 'err');
  }
  // Re-read so the group state (and member render) reflects what's on disk.
  _alLoadSubscriptions();
}

async function _alSubsDigestHour(select) {
  const hour = parseInt(select.value, 10);
  try {
    await api('POST', '/api/alerts/digest-hour', { hour });
  } catch (e) {
    toast(`Failed to save digest hour: ${e}`, 'err');
    _alLoadSubscriptions();   // re-read to undo the optimistic UI change
  }
}

async function _alSubsTest(eventKey, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    const out = await api('POST', '/api/alerts/subscriptions/test', { event_key: eventKey });
    btn.textContent = out.result === 'sent' ? 'Sent ✓' : `(${out.result})`;
  } catch (e) {
    btn.textContent = 'Failed';
    console.error('subs test failed', e);
  } finally {
    setTimeout(() => {
      btn.textContent = original;
      btn.disabled = false;
    }, 2500);
  }
}

async function _alSubsReset() {
  const ok = await confirmModal({body: (
    'Reset every subscription to its catalog default? Operator overrides ' +
    'will be cleared. (This does not affect the source-level toggles in Settings → Pod Config.)'
  ), danger: true});
  if (!ok) return;
  try {
    const data = await api('POST', '/api/alerts/subscriptions/reset', {});
    const body = document.getElementById('al-subs-body');
    if (body) body.innerHTML = _alSubsRender(data);
  } catch (e) {
    toast(`Reset failed: ${e}`, 'err');
  }
}

// Re-run live monitors (integration_probe, host_health, error_reporter,
// pod_health) before reloading the signal list. Without this, the Refresh
// button only re-reads cached signals from disk — confusing when a fix has
// just shipped and the user expects stale rows to clear.
async function _alRefreshNow(flavor) {
  const btn = document.getElementById(`al-refresh-${flavor}`);
  const original = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:10px;height:10px"></span> Refreshing…';
  }
  try {
    const d = await api('POST', '/api/signals/refresh');
    const monitors = d && d.monitors ? d.monitors : [];
    const failed = monitors.filter(m => !m.ok);
    if (failed.length) {
      const names = failed.map(m => `${m.name}${m.error ? ` (${m.error})` : ''}`).join(', ');
      toast(`Refreshed with errors: ${names}`, 'err');
    } else if (monitors.length) {
      toast('Monitors refreshed', 'ok');
    } else if (d && d.error) {
      toast(`Refresh failed: ${d.error}`, 'err');
    }
  } catch (e) {
    toast(`Refresh failed: ${e}`, 'err');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = original || '↻ Refresh';
    }
    _alLoadLane(flavor);
    _alLoadCount(flavor === 'activity' ? 'maintenance' : 'activity');
  }
}
