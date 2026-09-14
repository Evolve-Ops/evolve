// ════════════════════════════════════════════════════════════════════════
// Page subtab: Sessions Anomaly + TTL recommendations
//
// Renders the Cost → Sessions subtab's auxiliary surfaces — the inline
// anomaly card strip (firing session_economics Signals shown as scoped
// cards) and the TTL-recommendation accordion. The main Sessions
// loader (loadMonitoring + the session browser) lives in
// pages/monitoring.js (Phase 3o); this file holds the alongside
// surfaces.
//
// Slice 5 of the Sessions redesign — the "all stable" empty state is
// itself a meaningful signal.
//
// Functions:
//   loadMonAnomalies(botId)          — firing-strip fetch + render
//   _monAnomalyCard(sig)             — inline-compact severity card
//   _monAnomalySnooze(sigId, dur)    — snooze a signal
//   _monAnomalyView(sigId)           — open in Alerts page
//   loadMonTtlRecommendation(...)    — multi-bot TTL recommendation
//   _monTtlActionCard(rec)           — recommendation render
//   _monTtlFooterLine(recs)          — explainer footer
//   _monTtlToggleWhy(linkEl)         — expand-why disclosure
//   _monTtlSnooze(signalId, botId)   — snooze a single recommendation
//   _monTtlApply(botId, knob)        — accept-and-apply
//   _monCurationRow(entry)           — curation row (TTL drift audit)
//   _viridis + _renderActivityHeatmap — palette + heatmap renderer for
//                                       the session activity overview
//
// Cross-file linkages (runtime free-variable lookup):
//   - api(), toast(), escHtml(), botLabel() — core/
//   - nav() — core/router.js (the _monAnomalyView jumps to Alerts)
//   - _monBot — declared in pages/monitoring.js (Phase 3o); the
//               anomaly fetch follows the same bot tab
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Signal types that are SUBSUMED by the TTL recommendation card above.
// They get filtered out of the anomaly strip to avoid double-rendering
// the same finding once as a recommendation and once as a raw signal.
// bot_unused stays in the strip because it has no TTL action.
const _MON_ANOMALY_HIDDEN_TYPES = new Set([
  'cache_invalidation_elevated',
  'cache_hit_rate_low',
]);

async function loadMonAnomalies(botId) {
  const el = document.getElementById('mon-anomaly-strip');
  if (!el) return;
  const botParam = botId ? `&bot_id=${encodeURIComponent(botId)}` : '';
  const d = await api(
    'GET',
    `/api/signals?producer=session_economics&state=firing${botParam}&limit=10`,
  );
  let sigs = Array.isArray(d?.signals) ? d.signals : [];
  // Drop signals that the TTL recommendation card already surfaces — operator
  // sees the synthesized action above; raw signal here would be duplication.
  sigs = sigs.filter(s => !_MON_ANOMALY_HIDDEN_TYPES.has(s.type));

  if (!sigs.length) {
    // Empty: render nothing. The recommendation card carries the "all good"
    // narrative for cache health; we don't need a separate green tile here.
    el.innerHTML = '';
    return;
  }

  el.innerHTML = '<div style="display:flex;flex-direction:column;gap:6px">'
    + sigs.map(_monAnomalyCard).join('')
    + '</div>';
}

function _monAnomalyCard(sig) {
  // Inline-compact card: severity dot + title + last-observed age, with
  // snooze + view-in-alerts buttons on the right. Body is hidden by
  // default — operators click "View" to drill into the Alerts page for
  // full detail + dismiss workflow.
  const id = escHtml(sig.id || '');
  const title = escHtml(sig.title || sig.type || '(untitled)');
  const ageText = sig.last_observed_at ? ago(sig.last_observed_at) : '';
  const obsCount = sig.observation_count > 1
    ? `<span style="opacity:0.6;margin-left:8px">×${sig.observation_count}</span>`
    : '';
  return `<div style="display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);font-size:0.85rem">
    ${_alSeverityDot(sig.severity)}
    <div style="flex:1;min-width:0">
      <div style="display:flex;align-items:baseline;gap:10px">
        <span style="color:var(--text1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${title}</span>
        ${obsCount}
      </div>
    </div>
    <span style="color:var(--text3);font-size:0.74rem;white-space:nowrap">${escHtml(ageText)}</span>
    <button class="btn btn-ghost btn-sm" style="padding:2px 8px;font-size:0.74rem" onclick="_monAnomalySnooze('${id}','24h')" title="Snooze for 24 hours">Snooze 24h</button>
    <button class="btn btn-ghost btn-sm" style="padding:2px 8px;font-size:0.74rem" onclick="_monAnomalyView('${id}')" title="Open on Reports → Alerts for full detail and dismiss">View</button>
  </div>`;
}

async function _monAnomalySnooze(sigId, duration) {
  try {
    await api('POST', `/api/signals/${encodeURIComponent(sigId)}/snooze`, { duration });
    // Refresh the strip (and only the strip) — don't trigger a full
    // loadMonitoring() round-trip just because one signal got snoozed.
    loadMonAnomalies(_monBot).catch(e => console.warn('strip refresh after snooze failed', e));
  } catch (e) {
    toast(`Snooze failed: ${e}`, 'err');
  }
}

function _monAnomalyView(sigId) {
  // Jump to Reports · Alerts. The page-alerts redirect stub forwards to
  // Reports → Alerts (Alerts moved out of Maintenance in V2.2-3).
  const alertsLink = document.querySelector('[data-page="alerts"]');
  if (alertsLink) nav(alertsLink);
}

// ══════════════════════════════════════════════════════
// Sessions — TTL recommendation card (Slice 9, revised in Slice 10)
//
// Action-first redesign per operator feedback. The previous design buried
// the recommendation under the finding ("21% of cached turns are
// invalidated" — and then what?). This version leads with the action:
//
//   ┌──────────────────────────────────────────────────────────────┐
//   │  Raise team_bot_b TTL: 1h → 4h                  ~$3.40/mo savings  │
//   │  60% of cached turns are invalidated.                        │
//   │  [ Apply on Cost Measures → ]  [ Snooze 24h ]    Why this?  │
//   └──────────────────────────────────────────────────────────────┘
//
// "hold" and "not_enough_data" verdicts collapse to a single footer line
// so they don't bury the actionable cards above.
// ══════════════════════════════════════════════════════
const _MON_TTL_VERDICT_COLORS = {
  raise: { fg: 'var(--orange)', bg: 'rgba(251,146,60,0.10)', verb: 'Raise' },
  lower: { fg: 'var(--blue)',   bg: 'rgba(126,184,247,0.10)', verb: 'Lower' },
  hold:  { fg: 'var(--green)',  bg: 'rgba(74,222,128,0.06)',  verb: 'Hold' },
  not_enough_data: { fg: 'var(--text3)', bg: 'rgba(120,120,120,0.04)', verb: 'No data' },
};

// A recommendation names WHICH knob it means, and the two are not
// interchangeable: cache_retention is the prompt-cache lifetime (the only
// thing that moves the invalidation rate), contextPruning.ttl only gates when
// pruning may run. Rendering both as an undifferentiated "TTL" is what let the
// endpoint ship a cache diagnosis attached to a pruning prescription.
// `field` is the matrix row key on Cost Optimization — see _CM_FIELDS.
const _MON_TTL_KNOBS = {
  cache_retention:    { label: 'prompt-cache TTL', field: 'cache_retention' },
  context_pruning_ttl:{ label: 'pruning TTL',      field: 'contextPruning.ttl' },
};

async function loadMonTtlRecommendation(botId, days) {
  const el = document.getElementById('mon-ttl-recommendation');
  if (!el) return;
  const botParam = botId ? `&bot=${encodeURIComponent(botId)}` : '';
  const d = await api('GET', `/api/analytics/ttl-recommendation?days=${days || 30}${botParam}`);
  const recs = Array.isArray(d?.recommendations) ? d.recommendations : [];
  if (!recs.length) { el.innerHTML = ''; return; }

  // Split: action cards (raise / lower) come first; non-actionable verdicts
  // collapse to a footer line so they don't drown the actionable ones.
  const actionable = recs.filter(r => r.verdict === 'raise' || r.verdict === 'lower');
  const nonAction = recs.filter(r => r.verdict !== 'raise' && r.verdict !== 'lower');

  let html = '';
  if (!actionable.length) {
    // All bots are hold / not_enough_data: render a single calm summary.
    html = _monTtlFooterLine(nonAction);
  } else {
    html = '<div style="display:flex;flex-direction:column;gap:8px">'
      + actionable.map(_monTtlActionCard).join('')
      + '</div>';
    if (nonAction.length) {
      html += `<div style="margin-top:8px">${_monTtlFooterLine(nonAction)}</div>`;
    }
  }
  el.innerHTML = html;
}

function _monTtlActionCard(rec) {
  const v = _MON_TTL_VERDICT_COLORS[rec.verdict] || _MON_TTL_VERDICT_COLORS.hold;
  const bot = escHtml(rec.bot_id || '');
  const botName = escHtml(botLabel(rec.bot_id));
  const knob = _MON_TTL_KNOBS[rec.knob] || null;
  const knobLabel = escHtml(knob ? knob.label : 'setting');
  // current_setting/target_setting are the knob-specific display strings
  // ("short (5m)" → "long (1h)", or "15m" → "5m"). current_ttl is only the
  // pruning ttl, so it is not a fallback for a cache-retention card.
  const current = escHtml(rec.current_setting || '—');
  const target = escHtml(rec.target_setting || '');
  const conf = escHtml(rec.confidence || '');
  const savings = rec.estimated_monthly_savings_usd;
  const savingsBadge = (savings && savings >= 0.5)
    ? `<span style="color:var(--green);font-weight:600">~$${savings.toFixed(2)}/mo savings</span>`
    : (savings && savings > 0)
      ? `<span style="color:var(--text3)">~$${savings.toFixed(2)}/mo savings</span>`
      : '';

  // Headline: ACTION + bot + WHICH KNOB + from → to. Naming the knob is
  // load-bearing — "Raise bot TTL" reads identically for two settings that
  // do completely different things.
  const headline = `<span style="color:${v.fg};font-weight:700">${v.verb} ${botName} ${knobLabel}:</span>
    <span style="font-family:var(--mono,monospace);color:var(--text2)">${current}</span>
    <span style="color:var(--text3);margin:0 4px">→</span>
    <span style="font-family:var(--mono,monospace);color:var(--text1);font-weight:700">${target}</span>`;

  // Reasoning: one-line summary up front. Full reasoning hidden behind "Why?" link.
  const reasoning = escHtml(rec.reasoning || '');
  // The "headline reason" is the punchy first clause of the reasoning string
  // — everything up to the first period or semicolon, capped at ~80 chars.
  const reasonMatch = reasoning.match(/^[^.;]+/);
  const summaryReason = reasonMatch
    ? reasonMatch[0].replace(/^\d+%\s+(of\s+)?/i, m => m).slice(0, 110)
    : reasoning.slice(0, 110);

  const snoozeBtn = rec.motivating_signal_id
    ? `<button class="btn btn-ghost btn-sm" style="padding:3px 10px;font-size:0.76rem"
         onclick="_monTtlSnooze('${escHtml(rec.motivating_signal_id)}','${bot}')"
         title="Snooze the underlying signal for 24 hours">Snooze 24h</button>`
    : '';

  const applyBtn = `<button class="btn btn-sm" style="padding:3px 14px;font-size:0.78rem;background:${v.fg};border-color:${v.fg};color:#1a1a1a;font-weight:600"
       onclick="_monTtlApply('${bot}','${escHtml(rec.knob || '')}')"
       title="Open Cost Optimization with ${botName} pre-selected and the ${knobLabel} row highlighted">Apply on Cost Optimization →</button>`;

  return `<div class="card" style="background:${v.bg};border-left:3px solid ${v.fg};padding:12px 14px">
    <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px">
      <div style="font-size:1.0rem">${headline}</div>
      <span style="flex:1"></span>
      ${savingsBadge}
    </div>
    <div style="font-size:0.85rem;color:var(--text2);line-height:1.45;margin-bottom:10px">
      ${escHtml(summaryReason)}.
      <span style="color:var(--text3);font-size:0.74rem;margin-left:6px">(${conf} confidence)</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      ${applyBtn}
      ${snoozeBtn}
      <span style="flex:1"></span>
      <a href="javascript:void(0)" onclick="_monTtlToggleWhy(this)"
         style="font-size:0.76rem;color:var(--text3);text-decoration:underline">Why?</a>
    </div>
    <div class="mon-ttl-why" style="display:none;margin-top:10px;padding:10px 12px;background:rgba(0,0,0,0.20);border-radius:4px;font-size:0.8rem;color:var(--text2);line-height:1.5">
      ${escHtml(reasoning)}
    </div>
  </div>`;
}

function _monTtlFooterLine(recs) {
  // Calm summary of holds + not-enough-data. Single line per bot, dim text.
  const items = recs.map(r => {
    const v = _MON_TTL_VERDICT_COLORS[r.verdict] || _MON_TTL_VERDICT_COLORS.hold;
    const tag = r.verdict === 'hold' ? 'looks good' : 'collecting data';
    // Show the cache lifetime, not the pruning ttl — the cache is what the
    // holds are asserting is well-matched.
    const cache = r.effective_cache_retention === 'long' ? '1h cache' : '5m cache';
    return `<span style="color:var(--text3)"><span style="color:${v.fg}">●</span>
      ${escHtml(botLabel(r.bot_id))} <span style="opacity:0.7">— ${escHtml(cache)} ${tag}</span></span>`;
  }).join('<span style="color:var(--text3);margin:0 8px">·</span>');
  return `<div style="padding:8px 14px;border:1px solid var(--border);border-radius:6px;font-size:0.78rem;display:flex;flex-wrap:wrap;gap:4px;align-items:center">
    ${items}
  </div>`;
}

function _monTtlToggleWhy(linkEl) {
  const card = linkEl.closest('.card');
  const why = card?.querySelector('.mon-ttl-why');
  if (!why) return;
  const open = why.style.display !== 'none';
  why.style.display = open ? 'none' : 'block';
  linkEl.textContent = open ? 'Why?' : 'Hide';
}

async function _monTtlSnooze(signalId, botId) {
  try {
    await api('POST', `/api/signals/${encodeURIComponent(signalId)}/snooze`, { duration: '24h' });
    // Refresh both the recommendation card and the anomaly strip so the
    // snoozed item disappears from both surfaces.
    loadMonTtlRecommendation(_monBot, _monDays).catch(() => {});
    loadMonAnomalies(_monBot).catch(() => {});
  } catch (e) {
    toast(`Snooze failed: ${e}`, 'err');
  }
}

function _monTtlApply(botId, knob) {
  // Deeplink to Cost Optimization, pre-select the bot, and flash the matrix
  // row for the knob this recommendation is actually about. Doesn't
  // auto-apply — the operator clicks the profile column carrying the value,
  // which matches the conservative "operator-mediated" stance from Slice 2.
  //
  // Targets the settings-matrix row by its data-field key. The pre-matrix
  // markup this used to look for (#cm-prune-ttl) no longer exists, so the old
  // deeplink silently fell through its retry budget and did nothing.
  const field = (_MON_TTL_KNOBS[knob] || {}).field;
  const nav_el = document.querySelector('[data-page="cost-measures"]');
  if (nav_el) nav(nav_el);
  // After nav, the cost-measures page loads asynchronously. Schedule the
  // bot selection + highlight for the next tick once #cm-bot-tabs exists.
  const tryHighlight = (attemptsLeft) => {
    if (attemptsLeft <= 0) return;
    if (typeof _cmSelectBot === 'function' && document.getElementById('cm-bot-tabs')) {
      _cmSelectBot(botId);
      // _cmSelectBot triggers loadCostMeasures(); wait again for the matrix
      // row to render, then scroll + flash it.
      const tryFocusRow = (n) => {
        if (n <= 0 || !field) return;
        const cell = document.querySelector(`#cm-settings-body [data-field="${CSS.escape(field)}"]`);
        if (!cell) { setTimeout(() => tryFocusRow(n - 1), 100); return; }
        if (typeof _cmJumpToField === 'function') _cmJumpToField(field);
        else cell.closest('tr')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      };
      setTimeout(() => tryFocusRow(20), 100);
      return;
    }
    setTimeout(() => tryHighlight(attemptsLeft - 1), 100);
  };
  setTimeout(() => tryHighlight(20), 100);
}


// ══════════════════════════════════════════════════════
// Sessions — Activity rhythm (Slice 6 of Sessions redesign)
//
// Three rhythm-of-use views off /api/analytics/activity-rhythm:
//   1. Time-of-day heatmap (hour × weekday, UTC) — CSS grid; no chart
//      library plugin required.
//   2. Sessions per day — Chart.js bar chart of distinct session counts.
//   3. Inter-turn gap histogram — Chart.js bar chart of seconds between
//      consecutive user_turn events within a session. The decisive
//      number for "should I raise the TTL?" decisions.
// ══════════════════════════════════════════════════════
async function loadMonRhythm(botId, days) {
  const botParam = botId ? `&bot=${encodeURIComponent(botId)}` : '';
  const d = await api(
    'GET',
    `/api/analytics/activity-rhythm?days=${days || 30}${botParam}`,
  );

  // ── 1) Time-of-day heatmap ──────────────────────────────────────────
  _renderActivityHeatmap(d.time_of_day_heatmap || {});

  // ── 2) Sessions per day ─────────────────────────────────────────────
  const daily = d.daily_session_counts || [];
  mkChart('chart-mon-rhythm-daily', 'bar', {
    labels: daily.map(r => (r.date || '').slice(5)),
    datasets: [{
      label: 'Sessions',
      data: daily.map(r => r.session_count || 0),
      backgroundColor: 'rgba(126,184,247,0.5)',
      borderColor: 'var(--blue)',
      borderWidth: 1,
    }],
  });

  // ── 3) Inter-turn gap histogram ─────────────────────────────────────
  const gaps = d.inter_turn_gap_histogram || {};
  const bins = gaps.bins || [];
  mkChart('chart-mon-rhythm-gaps', 'bar', {
    labels: bins.map(b => b.label),
    datasets: [{
      label: 'Gaps',
      data: bins.map(b => b.count || 0),
      // Color ramp: short gaps (left) → blue; long gaps (right) → orange.
      // Long gaps are the TTL-stress region; the gradient draws the eye there.
      backgroundColor: bins.map((_, i) => {
        const t = bins.length > 1 ? i / (bins.length - 1) : 0;
        const r = Math.round(126 + (251 - 126) * t);
        const g = Math.round(184 + (146 - 184) * t);
        const b = Math.round(247 + (60 - 247) * t);
        return `rgba(${r},${g},${b},0.55)`;
      }),
      borderWidth: 1,
    }],
  });
  document.getElementById('mon-rhythm-gap-percentiles').innerHTML = gaps.gap_count
    ? `${gaps.gap_count.toLocaleString()} gaps · median ${_fmtSeconds(gaps.median_seconds)} · p95 ${_fmtSeconds(gaps.p95_seconds)}`
    : 'No multi-turn user sessions in this window.';
}

function _fmtSeconds(s) {
  // Compact duration: 90s → "1m 30s", 3600s → "1h", 86400s → "24h".
  // Used in the inter-turn gap percentile line.
  s = Math.round(Number(s) || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const r = s - m * 60;
    return r ? `${m}m ${r}s` : `${m}m`;
  }
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s - h * 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(s / 86400);
  const h = Math.floor((s - d * 86400) / 3600);
  return h ? `${d}d ${h}h` : `${d}d`;
}

// ══════════════════════════════════════════════════════
// Sessions — Curation list (Slice 7 of Sessions redesign)
//
// Renders /api/analytics/curated-sessions as a category-tagged list of
// session shortcuts. Each row shows the headline metric, session id,
// bot, and the rationale for surfacing the row. A session appearing in
// multiple categories is intentional — that's a signal worth seeing.
// ══════════════════════════════════════════════════════
const _MON_CURATION_CATEGORY_COLORS = {
  most_expensive:    'rgba(251,146,60,0.30)',   // orange — money
  longest_by_turns:  'rgba(192,132,252,0.30)',  // violet — duration
  biggest_context:   'rgba(126,184,247,0.30)',  // blue   — bloat
  most_invalidated:  'rgba(248,113,113,0.30)',  // red    — waste
};

async function loadMonCuration(botId, days) {
  const el = document.getElementById('mon-curation-list');
  if (!el) return;
  const botParam = botId ? `&bot=${encodeURIComponent(botId)}` : '';
  const d = await api(
    'GET',
    `/api/analytics/curated-sessions?days=${days || 30}${botParam}`,
  );
  const items = Array.isArray(d?.curated) ? d.curated : [];
  if (!items.length) {
    el.innerHTML = '<div class="empty" style="font-size:0.85rem">No standout sessions in this window.</div>';
    return;
  }
  el.innerHTML = '<div style="display:flex;flex-direction:column;gap:6px">'
    + items.map(_monCurationRow).join('')
    + '</div>';
}

function _monCurationRow(entry) {
  const bg = _MON_CURATION_CATEGORY_COLORS[entry.category] || 'rgba(150,150,150,0.20)';
  const label = escHtml(entry.label || entry.category || '');
  const metric = escHtml(entry.metric_label || '');
  const bot = escHtml(entry.bot_id || '');
  // 12 chars of session_id is enough to disambiguate while staying scannable.
  const sid = escHtml((entry.session_id || '').slice(0, 12));
  const rationale = escHtml(entry.rationale || '');
  const events = entry.event_count || 0;
  const triggerKinds = (entry.trigger_kinds || []).join(', ');
  const tsSpan = entry.first_ts && entry.last_ts && entry.first_ts !== entry.last_ts
    ? `${escHtml(entry.first_ts.slice(5,16).replace('T',' '))} → ${escHtml(entry.last_ts.slice(5,16).replace('T',' '))}`
    : escHtml((entry.first_ts || entry.last_ts || '').slice(5,16).replace('T',' '));
  return `<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);font-size:0.85rem">
    <span style="background:${bg};color:var(--text1);padding:2px 8px;border-radius:10px;font-size:0.72rem;font-weight:500;white-space:nowrap">${label}</span>
    <span style="font-weight:700;color:var(--text1);min-width:130px">${metric}</span>
    <span style="font-family:var(--mono,monospace);font-size:0.74rem;color:var(--text2);opacity:0.85">${sid}</span>
    <span style="color:var(--text2);font-size:0.78rem">${escHtml(botLabel(bot))}</span>
    <span style="color:var(--text3);font-size:0.72rem" title="${escHtml(triggerKinds)}">${events} event${events === 1 ? '' : 's'}</span>
    <span style="flex:1"></span>
    <span style="color:var(--text3);font-size:0.72rem;white-space:nowrap" title="${escHtml(rationale)}">${tsSpan}</span>
  </div>`;
}

// Viridis-style perceptually uniform palette. The eye picks up both
// hue and brightness, so contrast is much higher than a single-hue
// alpha ramp without being garish. Color-blind safe. Five canonical
// stops; linear interpolation between them for smooth gradients.
const _VIRIDIS_STOPS = [
  [68, 1, 84],     // 0.00 — dark purple (also looks "empty" against dark bg)
  [59, 82, 139],   // 0.25 — blue
  [33, 144, 141],  // 0.50 — teal
  [94, 201, 98],   // 0.75 — green
  [253, 231, 37],  // 1.00 — yellow
];

function _viridis(t) {
  // t ∈ [0, 1]. Returns "rgb(r,g,b)".
  if (t <= 0) return `rgb(${_VIRIDIS_STOPS[0].join(',')})`;
  if (t >= 1) return `rgb(${_VIRIDIS_STOPS[_VIRIDIS_STOPS.length - 1].join(',')})`;
  const scaled = t * (_VIRIDIS_STOPS.length - 1);
  const i = Math.floor(scaled);
  const f = scaled - i;
  const a = _VIRIDIS_STOPS[i], b = _VIRIDIS_STOPS[i + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * f);
  const g = Math.round(a[1] + (b[1] - a[1]) * f);
  const bl = Math.round(a[2] + (b[2] - a[2]) * f);
  return `rgb(${r},${g},${bl})`;
}

function _renderActivityHeatmap(h) {
  const el = document.getElementById('mon-rhythm-heatmap');
  if (!el) return;
  const matrix = Array.isArray(h.matrix) ? h.matrix : [];
  const maxCount = Number(h.max_count) || 0;

  if (!matrix.length || maxCount === 0) {
    el.innerHTML = '<div class="empty" style="font-size:0.85rem">No events in the window.</div>';
    return;
  }

  // CSS grid: 1 header row + 24 hour rows; 1 hour-label column + 7 day cols.
  // Non-empty cells are painted with the viridis ramp at their share-of-max
  // position. Empty cells get a flat dim color to differentiate from "1 event
  // = dark purple at the bottom of viridis."
  const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const _hourLabel = hr => (hr % 3 === 0) ? `${hr.toString().padStart(2,'0')}` : '';
  const _emptyCellBg = 'rgba(255,255,255,0.025)';

  let html = '<div style="display:grid;grid-template-columns:36px repeat(7,1fr);gap:2px;font-size:0.7rem;color:var(--text2)">';
  html += '<div></div>';
  for (const d of dayLabels) {
    html += `<div style="text-align:center;padding:2px 0">${d}</div>`;
  }
  for (let hour = 0; hour < 24; hour++) {
    html += `<div style="text-align:right;padding-right:6px;color:var(--text3)">${_hourLabel(hour)}</div>`;
    for (let dow = 0; dow < 7; dow++) {
      const count = (matrix[hour] && matrix[hour][dow]) || 0;
      // Empty stays flat-grey; non-empty maps to viridis. Bump the floor
      // share to 0.10 so a "1 event" cell sits clearly above empty in hue.
      const share = count / maxCount;
      const bg = count === 0
        ? _emptyCellBg
        : _viridis(Math.max(0.10, share));
      const tooltip = `${dayLabels[dow]} ${hour.toString().padStart(2,'0')}:00 UTC — ${count} event${count === 1 ? '' : 's'}`;
      html += `<div title="${escHtml(tooltip)}" style="height:14px;background:${bg};border-radius:2px"></div>`;
    }
  }
  html += '</div>';
  // Legend (low → high) sampling the same ramp at 5 points.
  const _legendStops = [0.10, 0.30, 0.55, 0.80, 1.00];
  html += `<div style="margin-top:8px;display:flex;align-items:center;gap:10px;font-size:0.72rem;color:var(--text3)">
    <span>Less</span>
    ${_legendStops.map(t => `<span style="display:inline-block;width:14px;height:10px;background:${_viridis(t)};border-radius:2px"></span>`).join('')}
    <span>More</span>
    <span style="margin-left:auto">Max: ${maxCount} event${maxCount === 1 ? '' : 's'} in a cell · ${(h.total_events || 0).toLocaleString()} total · UTC</span>
  </div>`;
  el.innerHTML = html;
}

// ══════════════════════════════════════════════════════
