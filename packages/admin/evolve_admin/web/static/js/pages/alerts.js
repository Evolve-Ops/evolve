// ════════════════════════════════════════════════════════════════════════
// Page: Alerts (Phase 1 of the alerts/signal-store consolidation)
//
// Spec: internal/spec-alerts-signal-store-2026-05-07.md
//
// The Alerts page renders the unified signal store — one row per
// firing signal grouped by signature, with per-severity coloring,
// remediation buttons, deeplinks to the originating surface,
// snooze/dismiss/resolve actions, and a sub-tab for snoozed signals.
//
// State:
//   _alShowInfo            — toggle for the "show info-severity" check
//   _alGroupSimilar        — toggle for "group similar signals" check
//   _alSignals             — last fetched signals (cached on the lane key)
//   _AL_SEVERITY_RANK      — ordering for sort
//   _AL_CHANNEL_KIND_LABELS — channel labels for the dispatch detail
//
// Loaders + renderers dispatched from onPageActivate('alerts') /
// subtab activators:
//   loadAlerts()           — entry; defaults to Activity subtab
//   _alLoadCount(flavor)   — per-flavor badge counts
//   _alLoadLane(lane)      — per-lane signal list
//   _alLoadSnoozed()       — snoozed-signals subtab
//
// Out of scope (other clusters that share the _al prefix):
//   _alLoadTracked + the filter chips / multi-select / subscriptions /
//   dispatcher health / PWA push extensions (~line 51676+) — those
//   live in a separate "Reports → Alerts page" cluster after the
//   Identity page block and will move as a follow-up phase.
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Alerts (Phase 1 of the alerts/signal-store consolidation)
// Spec: internal/spec-alerts-signal-store-2026-05-07.md
// ══════════════════════════════════════════════════════

async function loadAlerts() {
  // Default subtab is Activity. Also pre-load Maintenance count badge.
  _alLoadLane('activity');
  _alLoadCount('maintenance');
}

async function _alLoadCount(flavor) {
  try {
    const d = await api('GET', `/api/signals?flavor=${flavor}&limit=1000`);
    const el = document.getElementById(`al-count-${flavor}`);
    if (el) el.textContent = d.count || '0';
  } catch(_e) { /* badge is best-effort */ }
}

function _alSeverityClass(sev) {
  if (sev === 'alert') return 'severity-red';
  if (sev === 'warn')  return 'severity-yellow';
  return 'severity-info';
}

function _alSeverityDot(sev) {
  // Reuse existing severity colors via inline style — no new CSS needed.
  // Title attribute surfaces the severity tier on hover so the dot isn't
  // just a colored bauble for operators who don't recall the legend.
  const color = sev === 'alert' ? '#e54' : sev === 'warn' ? '#eb4' : '#888';
  const label = sev === 'alert' ? 'alert (critical)'
              : sev === 'warn'  ? 'warn (real problem)'
              : 'info (advisory)';
  return `<span title="${label}" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color};margin-right:8px;vertical-align:middle"></span>`;
}

// Phase 4 PR-2: client-side severity filter.
//   - default: hide info-tier signals (advisory; mostly noise on the page)
//   - toggle (page-level checkbox) reveals them
// Filter is applied BEFORE _alSignalRow so info signals don't take up
// DOM nodes when hidden — saves render time on noisy pods.
let _alShowInfo = false;

// Bootstrap-cost calibration signals (principle-apps-minimize-bootstrap-cost)
// are info-severity by design — they populate a per-bot measurement that
// surfaces on the "App bootstrap footprint" chip on the bot detail page.
// They are not actionable individually; the chip is the action surface.
// Always suppress them on the Alerts page, even when "show info" is on,
// so the operator's deeper view isn't dominated by calibration noise.
const _AL_CALIBRATION_FAMILY = new Set([
  'app_cron_eligible_used_heartbeat',
  'app_invocation_mode_not_subagent',
  'app_bot_guidance_oversized',
  'app_heartbeat_baseline_inflation',
]);

function _alIsCalibrationSignal(s) {
  return _AL_CALIBRATION_FAMILY.has(s && s.type);
}

function _alFilterBySeverity(sigs) {
  const noCalibration = sigs.filter(s => !_alIsCalibrationSignal(s));
  if (_alShowInfo) return noCalibration;
  return noCalibration.filter(s => s.severity !== 'info');
}

function _alToggleShowInfo(checkbox) {
  _alShowInfo = !!checkbox.checked;
  // Keep the other show-info toggles in sync (Reports + Maintenance share state).
  document.querySelectorAll('[id^="al-show-info-"]').forEach(c => {
    if (c !== checkbox) c.checked = _alShowInfo;
  });
  _alLoadLane('activity');
  _alLoadLane('maintenance');
  if (document.getElementById('reports-alerts-body')) _alLoadLane('reports');
}

function _alScopeLabel(sig) {
  if (sig.scope === 'bot' && sig.bot_id) return sig.bot_id;
  if (sig.scope === 'pod') return 'pod-wide';
  if (sig.scope === 'host') return 'host';
  if (sig.scope === 'integration') return 'integration';
  return sig.scope || '';
}

function _alStateBadge(sig) {
  if (sig.state === 'snoozed') {
    const until = sig.snoozed_until ? `until ${ago(sig.snoozed_until)}` : '';
    return `<span class="badge badge-muted" title="Snoozed ${escHtml(until)}">snoozed</span>`;
  }
  return '';
}

function _alPostureBadge(sig) {
  // R-3 event-vs-posture taxonomy: audit criticals classified "posture" are
  // standing configuration violations (fix once, then a board item, not a
  // bell). Only posture gets a badge — "event" stays the unmarked default.
  if (sig.details && sig.details.finding_kind === 'posture') {
    return `<span class="badge badge-muted" title="Standing posture violation — needs a one-time fix; does not re-page while it stands">posture</span>`;
  }
  return '';
}

function _alExplanation(sig) {
  // Producer-supplied "What this means" + "How to fix" sections. Audit
  // findings populate these via details.what_it_means / details.fix_steps;
  // other producers can opt in by writing the same keys.
  const d = sig.details || {};
  const what = typeof d.what_it_means === 'string' ? d.what_it_means.trim() : '';
  const fix = typeof d.fix_steps === 'string' ? d.fix_steps.trim() : '';
  if (!what && !fix) return '';
  const whatHtml = what
    ? `<div style="margin-top:10px">
         <div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--text3);margin-bottom:4px">What this means</div>
         <div style="font-size:0.82rem;color:var(--text2);white-space:pre-wrap">${escHtml(what)}</div>
       </div>`
    : '';
  // fix_steps is conventionally a numbered list ("1. …\n2. …"). Render
  // each "N. " line as a <li>; preserve continuation lines (commands,
  // indented detail) inside the same <li> via white-space:pre-wrap.
  let fixHtml = '';
  if (fix) {
    const items = [];
    let current = '';
    for (const line of fix.split('\n')) {
      if (/^\s*\d+\.\s/.test(line)) {
        if (current) items.push(current);
        current = line.replace(/^\s*\d+\.\s*/, '');
      } else {
        current = current ? current + '\n' + line : line;
      }
    }
    if (current) items.push(current);
    if (items.length) {
      const lis = items.map(it =>
        `<li style="margin-bottom:6px;white-space:pre-wrap">${escHtml(it)}</li>`
      ).join('');
      fixHtml = `<div style="margin-top:10px">
        <div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--text3);margin-bottom:4px">How to fix</div>
        <ol style="margin:0;padding-left:22px;font-size:0.82rem;color:var(--text2)">${lis}</ol>
      </div>`;
    } else {
      fixHtml = `<div style="margin-top:10px">
        <div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--text3);margin-bottom:4px">How to fix</div>
        <div style="font-size:0.82rem;color:var(--text2);white-space:pre-wrap">${escHtml(fix)}</div>
      </div>`;
    }
  }
  return whatHtml + fixHtml;
}

// ───────────────────────────────────────────────────────────────────────────
// Grouping — collapse repeats so the 8-bots-one-cause shapes don't drown
// the page.
// ───────────────────────────────────────────────────────────────────────────
//
// The current Alerts page treats every Signal as independent. In practice
// most operator decisions are *group* decisions: "wire up the backup PAT"
// clears 8 signals; "re-baseline plugin allowlist" clears another 7; "fix
// the config_drift Evolve bug" clears another 8. The UI needs to surface
// the structure.
//
// Grouping key = (producer, type, severity, normalized_title). The
// normalized title is the literal title with:
//   - the signal's bot_id replaced by the placeholder {bot}
//   - single-quoted contents (cron names, plugin names, MCP server names)
//     replaced by 'X'
// That collapses the common shapes without needing producer cooperation:
//   "{bot}: legacy credential key shape ..."   → groups 8 bot variants
//   "{bot}: unexpected plugin 'X' enabled"     → groups codex × 5 across bots
//                                                AND across the same bot's
//                                                multiple plugins
//   "{bot}: cron job 'X' missing caps"         → groups one bot's 5 crons
//
// Storage stays per-Signal. Group actions (snooze-all / resolve-all /
// dismiss-all) fan out to the existing /api/signals/bulk-action endpoint
// with the member ids — no new server-side concept.

const _AL_SEVERITY_RANK = {info: 0, warn: 1, alert: 2};

function _alNormalizeTitle(sig) {
  let t = sig.title || '';
  if (sig.bot_id) {
    // Replace whole-word bot_id matches. The literal split-join handles
    // multiple occurrences within the title (rare but possible).
    t = t.split(sig.bot_id).join('{bot}');
  }
  // Collapse quoted contents — cron names, plugin names, MCP server names.
  // The grouping intent is "same shape of finding, different name"; the
  // expanded member row still shows the original title verbatim.
  t = t.replace(/'[^']*'/g, "'X'");
  return t;
}

function _alGroupKey(sig) {
  // Producer-declared incident_key takes precedence — it expresses
  // "these signals are symptoms of one root cause" precisely, no matter
  // how the per-signal titles read. The structural verifier uses it
  // to coalesce a manifest's 4-6 distinct assertion findings into one
  // expandable row. Spec: internal/spec-recommendations-rework-2026-06-02.md.
  if (sig.incident_key) return `incident:${sig.incident_key}`;
  return [
    sig.producer || '',
    sig.type || '',
    sig.severity || '',
    _alNormalizeTitle(sig),
  ].join('|');
}

function _alGroupSignals(sigs) {
  // Returns array of group objects: {key, members, template_title,
  // severity, producer, last_observed_at, observation_count_sum,
  // distinct_bot_count}. Order is preserved from the input — the caller
  // is responsible for sorting (typically by last_observed_at desc).
  const byKey = new Map();
  const order = [];
  for (const sig of sigs) {
    const key = _alGroupKey(sig);
    let g = byKey.get(key);
    if (!g) {
      g = {
        key,
        members: [],
        template_title: _alNormalizeTitle(sig),
        severity: sig.severity,
        producer: sig.producer,
        type: sig.type,
        last_observed_at: sig.last_observed_at || '',
        observation_count_sum: 0,
        distinct_bots: new Set(),
      };
      byKey.set(key, g);
      order.push(g);
    }
    g.members.push(sig);
    g.observation_count_sum += sig.observation_count || 0;
    if (sig.bot_id) g.distinct_bots.add(sig.bot_id);
    if ((sig.last_observed_at || '') > g.last_observed_at) {
      g.last_observed_at = sig.last_observed_at;
    }
  }
  for (const g of order) g.distinct_bot_count = g.distinct_bots.size;
  return order;
}

// Toggle state. Default ON — the noise reduction is the whole point of
// the feature; an operator who wants the flat view can flip it off. The
// helper keeps multiple lane checkboxes in sync (mirrors _alShowInfo).
let _alGroupSimilar = true;

function _alToggleGroupSimilar(checkbox) {
  _alGroupSimilar = !!checkbox.checked;
  document.querySelectorAll('[id^="al-group-similar-"]').forEach(c => {
    if (c !== checkbox) c.checked = _alGroupSimilar;
  });
  _alLoadLane('activity');
  _alLoadLane('maintenance');
  if (document.getElementById('reports-alerts-body')) _alLoadLane('reports');
}

// Strip the {bot} grouping placeholder for human display. The placeholder
// is correct for the grouping key (lets producers emit "team-bot-a: legacy
// key shape" + the normalizer collapses it to "{bot}: legacy key shape"
// so it groups with team-bot-a/team-bot-b/etc), but rendering the raw template leaks
// "{bot}" into the operator-facing title. Strip the placeholder and its
// natural connective ("{bot}:", "on {bot}", " {bot} (", ...) so the
// rolled-up row reads cleanly — the "N bots" count badge already shows
// the audience scope. Discovered in the 2026-06-03 Alerts review where
// rolled-up audit, security_warden, backup_signal, sysadmin_watchdog,
// install_integrity_monitor, primary_model_floor_advisor rows all
// displayed literal "{bot}" text.
function _alPresentTemplateTitle(template) {
  return (template || '')
    .replace(/^\{bot\}:\s*/, '')                // "{bot}: foo" → "foo"
    .replace(/^\{bot\}\s+/, '')                 // "{bot} foo" → "foo"
    .replace(/\s+on\s+\{bot\}\s*$/, '')         // "foo on {bot}" (end) → "foo"
    .replace(/\s+on\s+\{bot\}(?=[\s.,;:])/g, '')// "foo on {bot}, bar" → "foo, bar"
    .replace(/\s*\{bot\}\s*(?=\()/g, ' ')       // "X {bot} (rest)" → "X (rest)" (keeps the X's trailing punct, e.g. ":")
    .replace(/\s+\{bot\}\s*/g, ' ')             // " {bot}" anywhere else → " "
    .replace(/\{bot\}/g, '')                    // catchall
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([.,;:!?])/g, '$1')            // tidy "Quiet ." → "Quiet."
    .trim();
}

function _alGroupRow(group) {
  // Singleton — defer to the per-signal renderer so visual behavior is
  // identical to ungrouped mode for non-clustered findings.
  if (group.members.length === 1) {
    return _alSignalRow(group.members[0]);
  }

  const count = group.members.length;
  const bots = group.distinct_bot_count;
  // Count badge phrasing — N bots when each member is a different bot;
  // otherwise "N findings" (e.g. one bot's 5 different cron jobs).
  const countLabel = bots === count
    ? `${count} bot${count === 1 ? '' : 's'}`
    : (bots > 1
        ? `${count} findings across ${bots} bot${bots === 1 ? '' : 's'}`
        : `${count} findings`);

  // The grouping template carries a {bot} placeholder; the present-title
  // helper strips it + its connective so the operator sees a readable
  // headline next to the count badge.
  let displayTitle = _alPresentTemplateTitle(group.template_title);

  // Incident-key groups (producer-declared coalesce) get a headline
  // built from the manifest display name rather than the first
  // member's assertion-id-suffixed title. So instead of "Health
  // Tracking: app_no_producer_surface +2 related" the operator
  // reads "Health Tracking: 3 structural issues".
  // The display_name lives in details on every member (audit_poller
  // writes it when emitting); fall back to the prefix of the
  // template title if absent (older signals from before the field
  // landed).
  const first = group.members[0] || {};
  if (first.incident_key && String(first.incident_key).startsWith('app_structural:')) {
    const dn = (first.details && first.details.display_name)
      || (displayTitle.split(':')[0] || '').trim();
    if (dn) {
      displayTitle = `${dn}: ${count} structural issue${count === 1 ? '' : 's'}`;
    }
  }

  // Stable id for the group so per-group menus + bulk-action handlers
  // can address it. Base64 of the key would be cleaner; a simple hash
  // suffices here and stays human-readable in the DOM.
  const groupId = 'g-' + group.key.replace(/[^a-zA-Z0-9_-]+/g, '_').slice(0, 80);
  const memberIdsJson = JSON.stringify(group.members.map(m => m.id));
  const memberIdsAttr = escHtml(memberIdsJson);

  // Inline bulk actions on the group header. Each calls the existing
  // /api/signals/bulk-action with all member ids.
  const stopProp = "event.preventDefault();event.stopPropagation();";
  const groupActions = `
    <span class="al-row-actions" style="display:inline-flex;gap:6px;align-items:center"
          onclick="${stopProp}">
      <span class="al-row-menu" style="position:relative;display:inline-block">
        <button class="btn btn-ghost btn-sm"
                title="Snooze every signal in this group"
                onclick="${stopProp}_alOpenGroupMenu('${groupId}', 'snooze')">Snooze all ▾</button>
        <div id="al-group-menu-snooze-${groupId}" class="al-row-menu-pop"
             style="display:none;position:absolute;top:100%;right:0;z-index:30;background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:4px;min-width:130px;box-shadow:0 4px 10px rgba(0,0,0,0.25)">
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alGroupBulk('${groupId}', 'snooze', '1h');_alCloseRowMenus()">1 hour</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alGroupBulk('${groupId}', 'snooze', '1d');_alCloseRowMenus()">1 day</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alGroupBulk('${groupId}', 'snooze', '1w');_alCloseRowMenus()">1 week</button>
        </div>
      </span>
      <span class="al-row-menu" style="position:relative;display:inline-block">
        <button class="btn btn-ghost btn-sm"
                title="Dismiss every signal in this group with a verdict"
                onclick="${stopProp}_alOpenGroupMenu('${groupId}', 'dismiss')">Dismiss all ▾</button>
        <div id="al-group-menu-dismiss-${groupId}" class="al-row-menu-pop"
             style="display:none;position:absolute;top:100%;right:0;z-index:30;background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:4px;min-width:180px;box-shadow:0 4px 10px rgba(0,0,0,0.25)">
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alGroupBulk('${groupId}', 'dismiss', null, 'false_positive');_alCloseRowMenus()">False positive</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alGroupBulk('${groupId}', 'dismiss', null, 'bad_inference');_alCloseRowMenus()">Bad inference</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alGroupBulk('${groupId}', 'dismiss', null, 'not_actionable');_alCloseRowMenus()">Not actionable</button>
        </div>
      </span>
      <button class="btn btn-ghost btn-sm"
              title="Mark every signal in this group resolved"
              onclick="${stopProp}_alGroupBulk('${groupId}', 'resolve')">Resolve all</button>
    </span>`;

  // Per-group member ids stash so the bulk handler can look up without
  // re-walking the DOM. Cleared by _alSelectionChanged on lane reload.
  if (!window._alGroupMembers) window._alGroupMembers = {};
  window._alGroupMembers[groupId] = group.members.map(m => m.id);

  // Worst-severity dot. Group key already pins severity, so every member
  // has the same — read from the group.
  const dot = _alSeverityDot(group.severity);

  const summary = `
    <summary style="padding:10px 12px;cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--bg2)">
      <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      ${dot}
      <strong style="flex:1 1 auto;min-width:200px">${escHtml(displayTitle)}</strong>
      <span class="badge" style="background:var(--accent);color:white" title="${escHtml(String(group.observation_count_sum))} total observations across the group">${escHtml(countLabel)}</span>
      <span class="badge badge-muted">${escHtml(group.producer || '')}</span>
      ${groupActions}
      <span style="color:var(--text3);font-size:0.78rem;min-width:70px;text-align:right">${ago(group.last_observed_at)}</span>
    </summary>`;

  const memberHtml = group.members.map(_alSignalRow).join('');
  const body = `
    <div style="padding:4px 0 4px 18px;background:var(--bg);border-left:3px solid var(--accent)">
      ${memberHtml}
    </div>`;

  return `<details class="al-signal-group" data-group-id="${escHtml(groupId)}" data-member-ids='${memberIdsAttr}' style="border-bottom:2px solid var(--border)">${summary}${body}</details>`;
}

function _alOpenGroupMenu(groupId, kind) {
  const target = document.getElementById(`al-group-menu-${kind}-${groupId}`);
  if (!target) return;
  const wasOpen = target.style.display === 'block';
  _alCloseRowMenus();
  if (!wasOpen) target.style.display = 'block';
}

async function _alGroupBulk(groupId, action, duration, verdict) {
  const ids = (window._alGroupMembers || {})[groupId] || [];
  if (!ids.length) {
    if (typeof toast === 'function') toast('Group has no member ids — try reloading the page', 'error');
    return;
  }
  const payload = {signal_ids: ids, action};
  if (duration) payload.duration = duration;
  if (verdict) payload.verdict = verdict;
  try {
    const resp = await api('POST', '/api/signals/bulk-action', payload);
    const ok = (resp && resp.results || []).filter(r => r.ok).length;
    const total = ids.length;
    if (typeof toast === 'function') {
      toast(`${action}: ${ok}/${total} succeeded`, ok === total ? 'success' : 'warning');
    }
    // Refresh all lanes so the now-archived/snoozed members disappear.
    _alLoadLane('activity');
    _alLoadLane('maintenance');
    if (document.getElementById('reports-alerts-body')) _alLoadLane('reports');
  } catch (e) {
    if (typeof toast === 'function') toast(`Bulk ${action} failed: ${e}`, 'error');
  }
}

// ───────────────────────────────────────────────────────────────────────────
//  Cost context line (PR E — schema v2 cost_event enrichment)
// ───────────────────────────────────────────────────────────────────────────
//
// Cost-related Signals (cost_watchdog producer) carry per-event context in
// ``details``: timestamp_local, user_id, user_display_name, channel_id,
// channel_kind. When present, render a one-line "Triggered by …" summary
// so the operator sees WHO and WHEN at a glance without expanding the
// raw JSON block. Other Signal types fall through to no-op.

const _AL_CHANNEL_KIND_LABELS = {
  slack_dm: 'Slack DM',
  slack_channel: 'Slack channel',
  telegram_dm: 'Telegram DM',
  telegram_group: 'Telegram group',
  discord_dm: 'Discord DM',
  discord_channel: 'Discord channel',
  internal: 'internal (heartbeat/cron)',
};

function _alFormatLocalTs(iso) {
  // Show "May 28, 17:52" in pod-local time. Strip seconds + tz suffix —
  // the operator already knows the timezone is the pod's.
  if (!iso || typeof iso !== 'string') return '';
  try {
    // ISO-8601 with offset (e.g. "2026-05-28T17:52:00-07:00") — slice off
    // the offset, then format with the Date constructor in local-string
    // form. Using new Date() would re-convert to the browser's tz, so we
    // parse the local-clock components by hand.
    const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2})?/);
    if (!m) return escHtml(iso);
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const mon = months[parseInt(m[2], 10) - 1] || m[2];
    return `${mon} ${parseInt(m[3], 10)}, ${m[4]}:${m[5]}`;
  } catch (_) {
    return escHtml(iso);
  }
}

function _alCostContextLine(sig) {
  // Only producers that opted in to the schema v2 enrichment carry these
  // keys. Quietly no-op when missing — older producers + non-cost
  // signals get their existing render with no extra row.
  if (!sig || sig.producer !== 'cost_watchdog') return '';
  const d = sig.details || {};
  const hasAny = (
    d.timestamp_local || d.user_id || d.user_display_name
    || d.channel_id || d.channel_kind
  );
  if (!hasAny) return '';

  // Who:
  //   display_name (id) → "Peter (U0518A544N5)"
  //   id only           → "U0518A544N5"
  //   neither           → omit the "by" clause entirely; the operator
  //                       still sees the channel + time.
  let whoHtml = '';
  if (d.user_display_name) {
    whoHtml = `<strong>${escHtml(d.user_display_name)}</strong>`;
    if (d.user_id) {
      whoHtml += ` <span style="color:var(--text3)">(${escHtml(d.user_id)})</span>`;
    }
  } else if (d.user_id) {
    // Cache miss / DNT — fall back to the raw id so the operator still
    // has something to grep their identity table with.
    whoHtml = `<code style="font-size:0.78rem">${escHtml(d.user_id)}</code>`;
    whoHtml += ` <span style="color:var(--text3)" title="Display name not cached — visit the Identity page to refresh">(unknown name)</span>`;
  }

  // Where:
  //   channel_kind known → "Slack DM" / "Telegram group" / "internal"
  //   only channel_id    → "channel D0AKX41HELU"
  //   neither            → omit
  let whereHtml = '';
  const kindLabel = _AL_CHANNEL_KIND_LABELS[d.channel_kind];
  if (kindLabel) {
    whereHtml = escHtml(kindLabel);
    if (d.channel_id && d.channel_kind !== 'internal') {
      whereHtml += ` <code style="font-size:0.74rem;color:var(--text3)">${escHtml(d.channel_id)}</code>`;
    }
  } else if (d.channel_id) {
    whereHtml = `channel <code style="font-size:0.74rem;color:var(--text3)">${escHtml(d.channel_id)}</code>`;
  }

  // When: prefer pod-local time over UTC — operators triaging at
  // 18:00 PT shouldn't have to convert "01:00 UTC" in their head.
  const whenHtml = d.timestamp_local
    ? `at <span style="color:var(--text2)">${escHtml(_alFormatLocalTs(d.timestamp_local))}</span>`
    : '';

  // Assemble the parts — gracefully drop missing ones rather than emit
  // a stuttering "by  in  at". Single line + muted background so the
  // existing body text remains primary.
  const parts = [];
  if (whoHtml) parts.push(`by ${whoHtml}`);
  if (whereHtml) parts.push(`in ${whereHtml}`);
  if (whenHtml) parts.push(whenHtml);
  if (parts.length === 0) return '';
  return `<div style="margin-top:6px;padding:6px 10px;background:var(--bg2);border-radius:3px;font-size:0.78rem;color:var(--text2)">
    Triggered ${parts.join(' ')}
  </div>`;
}

function _alSignalRow(sig) {
  // Producer-supplied "What this means" / "How to fix" sections (audit
  // populates these; other producers can opt in).
  const explanationHtml = _alExplanation(sig);
  // PR E: cost context (who / where / when) for cost_watchdog signals.
  const costContextHtml = _alCostContextLine(sig);
  // Raw-details fallback: only the keys not already surfaced by the
  // structured sections (and not duplicating the title/body).
  const SUPPRESSED_KEYS = new Set([
    'what_it_means', 'fix_steps',  // shown above as structured sections
    'category', 'level', 'message',  // duplicate of body / chips
    'finding_kind',  // rendered as the posture badge on the summary row
    // PR E: schema v2 cost context fields are rendered in the
    // costContextHtml block above. Suppress here so the Raw details
    // disclosure doesn't double-list them.
    'timestamp_local', 'user_id', 'user_display_name',
    'channel_id', 'channel_kind',
  ]);
  const filteredDetails = {};
  for (const [k, v] of Object.entries(sig.details || {})) {
    if (!SUPPRESSED_KEYS.has(k) && v !== '' && v !== null && v !== undefined) {
      filteredDetails[k] = v;
    }
  }
  const detailsJson = Object.keys(filteredDetails).length
    ? `<details style="margin-top:10px"><summary style="font-size:0.72rem;color:var(--text3);cursor:pointer;display:inline-flex;align-items:center;gap:5px"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Raw details (JSON)</summary><pre style="margin:6px 0 0 0;font-size:0.7rem;color:var(--text3);white-space:pre-wrap">${escHtml(JSON.stringify(filteredDetails, null, 2))}</pre></details>`
    : '';
  // 2026-06-04 paired-row rendering: when a Signal has a motivated
  // Proposal, surface the action affordance INLINE — the observation
  // and the action belong on one row, not two. Server hydrates
  // motivated_proposals_view with title/status/kind so we don't need
  // a separate fetch. Falls back to the count-only badge when
  // hydration isn't present (older API) or every linked proposal
  // is archived/missing.
  let proposalLink = '';
  const motivatedView = Array.isArray(sig.motivated_proposals_view) ? sig.motivated_proposals_view : [];
  // An "actionable" linked proposal is one whose status is on the
  // pending side of the lifecycle — operator can still act on it.
  const _ACTIONABLE_PROP_STATUSES = new Set([
    'draft', 'pending', 'approved_auto', 'approved_human', 'dispatched',
  ]);
  const actionable = motivatedView.filter(p => p && _ACTIONABLE_PROP_STATUSES.has(p.status || ''));
  if (actionable.length === 1) {
    const p = actionable[0];
    const pidJs = escHtml(p.id);
    const titleJs = escHtml((p.title || '').slice(0, 80));
    proposalLink = `
      <button class="btn btn-primary btn-sm al-paired-act"
              onclick="event.preventDefault();event.stopPropagation();_alPairedAct('${pidJs}', this)"
              title="Act on the linked proposal: ${titleJs}"
              style="padding:3px 10px;font-size:0.74rem">
        Act: ${titleJs || 'open proposal'}
      </button>`;
  } else if (actionable.length > 1) {
    // Multiple linked proposals — rare. Surface a count badge and
    // defer to the existing per-row expand for triage.
    proposalLink = `<span class="badge badge-muted" title="${actionable.length} RSI proposals motivated by this signal">→ ${actionable.length} proposals</span>`;
  } else if (sig.motivated_proposals && sig.motivated_proposals.length) {
    // Linked proposals exist but all are terminal (succeeded /
    // failed / archived). Show the link in muted form so the
    // operator sees the audit trail without an Act prompt.
    proposalLink = `<span class="badge badge-muted" title="${sig.motivated_proposals.length} RSI proposal(s) motivated this signal (archived)">→ ${sig.motivated_proposals.length} proposal${sig.motivated_proposals.length === 1 ? '' : 's'}</span>`;
  }

  // "Adjust cap →" inline shortcut: rendered when the signal carries a
  // config_hint pointing at a generator parameter the operator can tune
  // directly. Today only budget_hawk emits this hint (per_bot_daily_warn_usd),
  // but the mechanism is generic — any signal whose root cause is a tunable
  // threshold can opt in by setting config_hint at emission time.
  let configBtn = '';
  let configEditor = '';
  if (sig.config_hint && sig.config_hint.generator === 'budget_hawk'
      && sig.config_hint.param === 'per_bot_daily_warn_usd'
      && sig.config_hint.bot_id) {
    const currentCap = (sig.details && sig.details.cap_usd) || '';
    const currentSpend = (sig.details && sig.details.current_usd) || '';
    const editorId = `al-cap-edit-${sig.id}`;
    configBtn = `<button class="btn btn-ghost btn-sm" onclick="_alToggleCapEditor('${sig.id}')">Adjust cap →</button>`;
    configEditor = `
      <div id="${editorId}" style="display:none;margin-top:10px;padding:10px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:4px">
        <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">
          Current daily warn cap for <strong>${escHtml(sig.config_hint.bot_id)}</strong>: $${escHtml(String(currentCap))}.
          Today's spend: $${escHtml(String(currentSpend))}.
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <label style="font-size:0.78rem;color:var(--text2)">New warn cap:</label>
          $<input id="${editorId}-input" type="number" min="0" step="0.01" value="${escHtml(String(currentCap))}" style="width:100px">
          <button class="btn btn-primary btn-sm" onclick="_alSaveCap('${sig.id}', '${escHtml(sig.config_hint.bot_id)}')">Save</button>
          <button class="btn btn-ghost btn-sm" onclick="_alToggleCapEditor('${sig.id}')">Cancel</button>
          <span id="${editorId}-status" class="subtle" style="font-size:0.74rem;margin-left:auto"></span>
        </div>
      </div>`;
  }

  // Disk-reclaim inline remediation: rendered on the host_health disk_low
  // signal when the scan found reclaimable npm caches. The panel IS the
  // confirm step — it's destructive (deletes regenerable npm caches), so it
  // shows the per-category breakdown before the operator commits. "Reclaim
  // now" posts to POST /api/host-health/reclaim, which runs server-side: the
  // admin server is the evolve user and holds the §3.2 sudo grants, so
  // there's no `sudo …` CLI hint to copy-paste.
  //
  // npm caches ONLY. The scanner still reports oversized-log sizes (read-only)
  // but logs are not one-click reclaimable — a sudo `truncate` grant follows
  // symlinks in both intermediate and final components (a root arbitrary-file-
  // zero primitive), and logs are already bounded at source. See the PR2
  // audit (Option B) in disk_reclaim_apply.py.
  let reclaimBtn = '';
  let reclaimPanel = '';
  {
    const d = sig.details || {};
    // Only the npm-cache (method 'rm') categories are reclaimable here.
    const cats = Array.isArray(d.reclaimable)
      ? d.reclaimable.filter(c => c.method === 'rm' && (c.bytes || 0) > 0) : [];
    const npmBytes = cats.reduce((s, c) => s + (Number(c.bytes) || 0), 0);
    if (sig.type === 'disk_low' && npmBytes > 0 && cats.length) {
      const panelId = `al-reclaim-${sig.id}`;
      const anyPartial = cats.some(c => c.partial);
      const rows = cats.map(c => {
        const partial = c.partial
          ? ' <span style="color:var(--text3)" title="Some of this category was unreadable at scan time">· partial</span>'
          : '';
        return `<li style="margin-bottom:4px"><strong>${escHtml(_alHumanBytes(c.bytes))}</strong> — ${escHtml(c.label || c.category)} <span style="color:var(--text3)">(delete)</span>${partial}</li>`;
      }).join('');
      reclaimBtn = `<button class="btn btn-primary btn-sm" onclick="_alToggleReclaimPanel('${sigIdJs}')">Reclaim npm caches (${escHtml(_alHumanBytes(npmBytes))}) →</button>`;
      reclaimPanel = `
        <div id="${panelId}" style="display:none;margin-top:10px;padding:10px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:4px">
          <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">
            Frees <strong>${escHtml(_alHumanBytes(npmBytes))}</strong> on the pod host by deleting the
            regenerable npm caches below — they rebuild on next install. Runs on the server — nothing to type.
          </div>
          <ul style="margin:0 0 10px 0;padding-left:20px;font-size:0.8rem;color:var(--text2)">${rows}</ul>
          ${anyPartial ? '<div style="font-size:0.74rem;color:var(--text3);margin-bottom:8px">Some directories were unreadable at scan time — the freed total may be higher.</div>' : ''}
          <div style="display:flex;gap:8px;align-items:center">
            <button class="btn btn-primary btn-sm" id="${panelId}-run" onclick="_alRunReclaim('${sigIdJs}')">Reclaim now</button>
            <button class="btn btn-ghost btn-sm" onclick="_alToggleReclaimPanel('${sigIdJs}')">Cancel</button>
            <span id="${panelId}-status" class="subtle" style="font-size:0.74rem;margin-left:auto"></span>
          </div>
        </div>`;
    }
  }

  // Per-row inline action buttons in the row header (Part 1). These
  // mirror the in-body actions but are visible without expanding the
  // details disclosure — the 2026-05-21 transcript surfaced the
  // workflow gap where the operator landed on 87 firing alerts and had
  // no path to act on them without click-expanding each row. The same
  // three actions are also what evo's signal_action tool (snooze /
  // resolve / dismiss) calls; two surfaces (UI buttons + tools) routed to identical
  // endpoints. The buttons are wrapped in onclick={event.preventDefault()}
  // to keep the <details> from toggling when the operator clicks an
  // action.
  //
  // Snooze and Dismiss render as split buttons with a popover menu —
  // _alOpenRowMenu() toggles a per-row menu so the operator picks
  // duration (1h/1d/1w) or verdict (3 choices) in one click.
  const sigIdJs = escHtml(sig.id || '');
  const stopProp = "event.preventDefault();event.stopPropagation();";
  const rowActions = `
    <span class="al-row-actions" style="display:inline-flex;gap:6px;align-items:center"
          onclick="${stopProp}">
      <span class="al-row-menu" data-sig-id="${sigIdJs}" style="position:relative;display:inline-block">
        <button class="btn btn-ghost btn-sm al-row-snooze"
                title="Snooze this signal"
                onclick="${stopProp}_alOpenRowMenu('${sigIdJs}', 'snooze')">Snooze ▾</button>
        <div id="al-row-menu-snooze-${sigIdJs}" class="al-row-menu-pop"
             style="display:none;position:absolute;top:100%;right:0;z-index:30;background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:4px;min-width:130px;box-shadow:0 4px 10px rgba(0,0,0,0.25)">
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alSnooze('${sigIdJs}', '1h');_alCloseRowMenus()">1 hour</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alSnooze('${sigIdJs}', '1d');_alCloseRowMenus()">1 day</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alSnooze('${sigIdJs}', '1w');_alCloseRowMenus()">1 week</button>
        </div>
      </span>
      <span class="al-row-menu" data-sig-id="${sigIdJs}" style="position:relative;display:inline-block">
        <button class="btn btn-ghost btn-sm al-row-dismiss"
                title="Dismiss this signal with a verdict"
                onclick="${stopProp}_alOpenRowMenu('${sigIdJs}', 'dismiss')">Dismiss ▾</button>
        <div id="al-row-menu-dismiss-${sigIdJs}" class="al-row-menu-pop"
             style="display:none;position:absolute;top:100%;right:0;z-index:30;background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:4px;min-width:180px;box-shadow:0 4px 10px rgba(0,0,0,0.25)">
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alSingleDismiss('${sigIdJs}', 'false_positive');_alCloseRowMenus()">False positive</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alSingleDismiss('${sigIdJs}', 'bad_inference');_alCloseRowMenus()">Bad inference</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left"
                  onclick="${stopProp}_alSingleDismiss('${sigIdJs}', 'not_actionable');_alCloseRowMenus()">Not actionable</button>
          <button class="btn btn-ghost btn-sm" style="display:block;width:100%;text-align:left;border-top:1px solid var(--border);margin-top:2px"
                  onclick="${stopProp}_alDismiss('${sigIdJs}');_alCloseRowMenus()">More options…</button>
        </div>
      </span>
      <button class="btn btn-ghost btn-sm al-row-resolve" title="Mark this signal resolved"
              onclick="${stopProp}_alResolve('${sigIdJs}')">Resolve</button>
    </span>`;

  // Multi-select checkbox (Part 2). Inserted at the left edge of the
  // summary row, before the severity dot. Clicking the checkbox must
  // NOT toggle the <details> — onclick stops propagation. The change
  // handler refreshes the sticky bulk-action bar.
  const checkbox = `
    <input type="checkbox" class="al-row-select" data-sig-id="${sigIdJs}"
           onclick="event.stopPropagation()"
           onchange="_alSelectionChanged()"
           style="margin:0;cursor:pointer;flex:0 0 auto"
           title="Select for bulk action">`;

  const summary = `
    <summary style="padding:10px 12px;cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
      ${checkbox}
      ${_alSeverityDot(sig.severity)}
      <strong style="flex:1 1 auto;min-width:200px">${escHtml(sig.title || sig.type)}</strong>
      <span class="badge badge-muted">${escHtml(sig.producer)}</span>
      <span class="badge badge-muted">${escHtml(_alScopeLabel(sig))}</span>
      ${_alStateBadge(sig)}
      ${_alPostureBadge(sig)}
      ${proposalLink}
      ${rowActions}
      <span style="color:var(--text3);font-size:0.78rem;min-width:70px;text-align:right">${ago(sig.last_observed_at)}</span>
    </summary>`;
  const body = `
    <div style="padding:0 12px 12px 30px">
      <div style="font-size:0.82rem;color:var(--text2)">${escHtml(sig.body || '')}</div>
      ${costContextHtml}
      ${explanationHtml}
      ${detailsJson}
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        ${_alRemediationBtn(sig)}
        ${_alDeeplinkBtn(sig)}
        ${reclaimBtn}
        ${configBtn}
        <button class="btn btn-ghost btn-sm" onclick="_alSnooze('${sigIdJs}', '24h')">Snooze 24h</button>
        <button class="btn btn-ghost btn-sm" onclick="_alSnooze('${sigIdJs}', '7d')">Snooze 7d</button>
        <button class="btn btn-ghost btn-sm" onclick="_alResolve('${sigIdJs}')">Mark resolved</button>
        <button class="btn btn-ghost btn-sm" onclick="_alDismiss('${sigIdJs}')">Dismiss…</button>
        <span style="flex:1"></span>
        <span style="font-size:0.7rem;color:var(--text3)">obs ×${sig.observation_count} · created ${ago(sig.created_at)}</span>
      </div>
      ${configEditor}
      ${reclaimPanel}
      <div id="al-rem-result-${sigIdJs}" style="margin-top:10px"></div>
    </div>`;
  return `<details class="al-signal-row" data-sig-id="${sigIdJs}" data-producer="${escHtml(sig.producer || '')}" data-bot-id="${escHtml(sig.bot_id || '')}" data-severity="${escHtml(sig.severity || '')}" style="border-bottom:1px solid var(--border)">${summary}${body}</details>`;
}

// ───────────────────────────────────────────────────────────────────────────
// Per-row Snooze/Dismiss menu popovers (Part 1)
// ───────────────────────────────────────────────────────────────────────────
// Single global "currently-open" menu — only one is allowed at a time so
// clicking a different row's button auto-closes the previous popover.

function _alCloseRowMenus() {
  document.querySelectorAll('.al-row-menu-pop').forEach(el => {
    el.style.display = 'none';
  });
}

function _alOpenRowMenu(sigId, kind) {
  const target = document.getElementById(`al-row-menu-${kind}-${sigId}`);
  if (!target) return;
  const wasOpen = target.style.display === 'block';
  _alCloseRowMenus();
  if (!wasOpen) target.style.display = 'block';
}

// Single-row fast-path dismiss — bypasses the modal so the per-row
// verdict picker is one-click. The full modal stays available via the
// "More options…" entry for when the operator wants to add a note.
async function _alSingleDismiss(id, verdict) {
  try {
    await api('POST', `/api/signals/${encodeURIComponent(id)}/dismiss`, { verdict });
    _alRefreshActive();
  } catch (e) {
    toast(`Dismiss failed: ${e}`, 'err');
  }
}

// Close any open row menus when clicking outside an action area.
document.addEventListener('click', (ev) => {
  const inside = ev.target && (
    ev.target.closest && (
      ev.target.closest('.al-row-menu') || ev.target.closest('.al-row-menu-pop')
    )
  );
  if (!inside) _alCloseRowMenus();
}, true);

// Phase 5: render a "Configure →" button when the signal's details
// carry a deeplink pointing at an admin SPA page. Path format is
// "/admin/<page>" possibly with a ?query — the page id is extracted and
// passed to nav() to switch the SPA's active page. Query params are
// passed through window history so the destination's loader can read them.
function _alDeeplinkBtn(sig) {
  const details = sig.details || {};
  const link = details.deeplink;
  if (!link || typeof link !== 'string') return '';
  // Only handle relative /admin/<page> paths — absolute URLs go elsewhere.
  const m = link.match(/^\/admin\/([a-z0-9-]+)(\?.*)?$/i);
  if (!m) return '';
  const page = m[1];
  // Cache the query so the destination loader can read it from history.
  const q = (m[2] || '').replace(/'/g, "");
  return `<button class="btn btn-ghost btn-sm" onclick="_alDeeplinkGo('${page}', '${escHtml(q)}')">Configure →</button>`;
}

function _alDeeplinkGo(page, query) {
  // Push the query so the destination loader can read it via
  // window.location.search if it wants to highlight a specific row.
  try {
    if (query) {
      const url = new URL(window.location.href);
      url.search = query;
      window.history.pushState({}, '', url);
    }
  } catch (e) { /* fall through to nav */ }
  // Resolve aliases via the same registry that nav() uses so a deeplink
  // like /admin/daemons can land on /admin/maintenance#health without each
  // producer having to know the current IA. Falls back to the page id
  // when no entry exists.
  const dest = (typeof resolveDestination === 'function')
    ? resolveDestination(page)
    : { page };
  // ?subtab=X in the deeplink wins over the alias entry's default subtab
  // so producers can override per-signal (e.g. tasks#infrajobs vs tasks).
  let subtabOverride = null;
  if (query) {
    try {
      const params = new URLSearchParams(query.replace(/^\?/, ''));
      const s = params.get('subtab');
      if (s) subtabOverride = s;
    } catch (e) { /* ignore */ }
  }
  const target = document.querySelector(`[data-page="${dest.page}"]`);
  if (!target) {
    console.warn(`[alerts] Configure → deeplink points at unknown page "${page}" (resolved to "${dest.page}"). Update the signal's details.deeplink or add a pageRedirectRegistry entry.`);
    return;
  }
  nav(target);
  const subtab = subtabOverride || dest.subtab;
  if (subtab) {
    // nav() already activates dest.subtab when it comes from the registry,
    // but the query-string override needs a manual activation. Run after a
    // tick so the page's onPageActivate hook has finished mounting subtabs.
    setTimeout(() => {
      const subtabEl = document.querySelector(`#page-${dest.page} .subtab[data-subtab="${subtab}"]`);
      if (subtabEl) subtabEl.click();
      else console.warn(`[alerts] subtab "${subtab}" not found on page "${dest.page}".`);
    }, 80);
  }
}

// Phase 4 PR-2: render the "Run fix" button when the signal carries a
// structured Remediation. The button's onclick passes the signal id + the
// remediation object (URL-encoded JSON) so the modal opener doesn't need
// to refetch the signal — the data is already on the page.
function _alRemediationBtn(sig) {
  if (!sig.remediation || !sig.remediation.kind) return '';
  const label = sig.remediation.label || `Run ${sig.remediation.kind}`;
  // encodeURIComponent leaves ' unescaped (per RFC 3986 unreserved set),
  // and remediation.confirm text routinely contains apostrophes (e.g.
  // "won't help"). An unescaped ' inside a single-quoted onclick attribute
  // terminates the JS string literal mid-payload and silently breaks the
  // button. Hand-escape ' to %27 (and ( ) too, for symmetry).
  const payload = encodeURIComponent(JSON.stringify(sig.remediation))
    .replace(/'/g, '%27').replace(/\(/g, '%28').replace(/\)/g, '%29');
  return `<button class="btn btn-primary btn-sm" onclick="_alOpenRemediation('${sig.id}', '${payload}')">${escHtml(label)}</button>`;
}

// Modal opener: builds + appends a remediation-confirm modal (lazy so
// the alerts page DOM stays small until a button is clicked). The modal
// shows the producer-supplied confirm text, then Run/Cancel buttons.
// Run posts to /api/admin/remediation/execute and polls the job endpoint
// until status leaves "queued"/"running", then renders the result inline
// on the signal row.
function _alOpenRemediation(sigId, payload) {
  let rem;
  try {
    rem = JSON.parse(decodeURIComponent(payload));
  } catch (e) {
    toast('Could not parse remediation payload: ' + e, 'err');
    return;
  }
  // Build the modal DOM. Existing .modal-overlay + .modal CSS lives at
  // ~line 451 — same vis pattern as the dismiss-reason modal.
  let overlay = document.getElementById('al-rem-modal');
  if (overlay) overlay.remove();
  overlay = document.createElement('div');
  overlay.id = 'al-rem-modal';
  overlay.className = 'modal-overlay open';
  const labelText = rem.label || ('Run ' + rem.kind);
  overlay.innerHTML = `
    <div class="modal">
      <h2>${escHtml(labelText)}</h2>
      <div style="font-size:0.85rem;color:var(--text2);margin-bottom:14px;white-space:pre-wrap">${escHtml(rem.confirm || 'Run this remediation?')}</div>
      <div style="font-size:0.74rem;color:var(--text3);margin-bottom:14px">
        <div><strong>kind:</strong> <code>${escHtml(rem.kind)}</code></div>
        ${Object.keys(rem.params || {}).length
          ? `<div><strong>params:</strong> <code>${escHtml(JSON.stringify(rem.params))}</code></div>`
          : ''}
      </div>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button class="btn btn-ghost btn-sm" onclick="_alCloseRemediation()">Cancel</button>
        <button class="btn btn-primary btn-sm" id="al-rem-run-btn" onclick="_alRunRemediation('${sigId}')">Run</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  // Stash the parsed payload on the overlay so the run handler can read
  // it without a second JSON.parse roundtrip.
  overlay._remediation = rem;
  overlay._signalId = sigId;
}

function _alCloseRemediation() {
  const overlay = document.getElementById('al-rem-modal');
  if (overlay) overlay.remove();
}

async function _alRunRemediation(sigId) {
  const overlay = document.getElementById('al-rem-modal');
  if (!overlay) return;
  const rem = overlay._remediation;
  const runBtn = document.getElementById('al-rem-run-btn');
  const resultEl = document.getElementById(`al-rem-result-${sigId}`);
  if (runBtn) {
    runBtn.disabled = true;
    runBtn.textContent = 'Running…';
  }
  let jobId = null;
  try {
    const r = await fetch('/api/admin/remediation/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind: rem.kind,
        params: rem.params || {},
        signal_id: sigId,
      }),
    });
    if (r.status !== 202) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${r.status}`);
    }
    const j = await r.json();
    jobId = j.job_id;
  } catch (e) {
    if (resultEl) {
      resultEl.innerHTML = `<div style="padding:8px 10px;background:rgba(255,80,80,0.08);border:1px solid rgba(255,80,80,0.3);border-radius:4px;font-size:0.78rem;color:#ff8c8c">Could not dispatch remediation: ${escHtml(String(e))}</div>`;
    }
    _alCloseRemediation();
    return;
  }
  _alCloseRemediation();
  if (resultEl) {
    resultEl.innerHTML = `<div style="padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;font-size:0.78rem;color:var(--text2)">⏳ Remediation queued (job <code>${jobId.slice(0,8)}</code>)…</div>`;
  }
  // Poll the job endpoint until status leaves queued/running. 180s cap
  // matches install_infra_jobs' subprocess timeout — anything that takes
  // longer than that has gone wrong on the server side.
  const start = Date.now();
  const maxMs = 180_000;
  while (Date.now() - start < maxMs) {
    await new Promise(res => setTimeout(res, 1500));
    let job;
    try {
      const r = await fetch(`/api/admin/remediation/job/${jobId}`);
      if (!r.ok) {
        await new Promise(res => setTimeout(res, 2000));
        continue;
      }
      job = await r.json();
    } catch (e) {
      continue;  // transient — keep polling
    }
    if (job.status === 'succeeded' || job.status === 'failed') {
      _alRenderRemediationResult(sigId, jobId, job);
      // After a successful fix, refresh the alerts lane so swept-resolved
      // signals fall off the page automatically.
      if (job.status === 'succeeded') {
        setTimeout(_alRefreshActive, 2000);
      }
      return;
    }
  }
  if (resultEl) {
    resultEl.innerHTML = `<div style="padding:8px 10px;background:rgba(255,140,0,0.08);border:1px solid rgba(255,140,0,0.3);border-radius:4px;font-size:0.78rem;color:#ffb060">Remediation still running after 180s — check <a href="#" onclick="toast('Open dev tools and GET /api/admin/remediation/job/${jobId} for current state.','ok');return false;">job ${jobId.slice(0,8)}</a> manually.</div>`;
  }
}

function _alRenderRemediationResult(sigId, jobId, job) {
  const resultEl = document.getElementById(`al-rem-result-${sigId}`);
  if (!resultEl) return;
  if (job.status === 'succeeded') {
    const outStr = job.output
      ? `<pre style="margin:6px 0 0 0;font-size:0.7rem;white-space:pre-wrap;color:var(--text3)">${escHtml(JSON.stringify(job.output, null, 2))}</pre>`
      : '';
    resultEl.innerHTML = `
      <div style="padding:8px 10px;background:rgba(80,200,120,0.08);border:1px solid rgba(80,200,120,0.3);border-radius:4px;font-size:0.78rem;color:#7fff9e">
        ✓ Remediation succeeded (job <code>${jobId.slice(0,8)}</code>)
        ${outStr}
      </div>`;
  } else {
    const errStr = job.error
      ? `<pre style="margin:6px 0 0 0;font-size:0.7rem;white-space:pre-wrap;color:var(--text3)">${escHtml(job.error)}</pre>`
      : '';
    resultEl.innerHTML = `
      <div style="padding:8px 10px;background:rgba(255,80,80,0.08);border:1px solid rgba(255,80,80,0.3);border-radius:4px;font-size:0.78rem;color:#ff8c8c">
        ✗ Remediation failed (job <code>${jobId.slice(0,8)}</code>)
        ${errStr}
      </div>`;
  }
}

function _alToggleCapEditor(sigId) {
  const el = document.getElementById(`al-cap-edit-${sigId}`);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
  if (el.style.display === 'block') {
    const input = document.getElementById(`al-cap-edit-${sigId}-input`);
    if (input) input.focus();
  }
}

async function _alSaveCap(sigId, botId) {
  const input = document.getElementById(`al-cap-edit-${sigId}-input`);
  const status = document.getElementById(`al-cap-edit-${sigId}-status`);
  if (!input || !status) return;
  const raw = input.value.trim();
  if (raw === '') {
    status.style.color = '#ff8c42';
    status.textContent = 'Enter a value, or use the bot setup page to clear the override.';
    return;
  }
  const n = parseFloat(raw);
  if (!isFinite(n) || n <= 0) {
    status.style.color = '#ff8c42';
    status.textContent = 'Cap must be a positive number.';
    return;
  }
  status.style.color = 'var(--muted)';
  status.textContent = 'Saving…';
  try {
    const r = await fetch(`/api/arbiter/bot-setup/${encodeURIComponent(botId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ daily_warn_usd: n }),
    });
    const j = await r.json();
    if (!j.ok) {
      status.style.color = '#ff8c42';
      status.textContent = `Error: ${j.error || 'unknown'}`;
      return;
    }
    status.style.color = '#7fff9e';
    status.textContent = 'Saved. Alert will clear on the next budget_hawk run if today’s spend is under the new cap.';
    // Refresh after a moment so the operator sees the confirmation, then
    // sees the alert state update (or stay, if spend still exceeds new cap).
    setTimeout(_alRefreshActive, 1200);
  } catch (e) {
    status.style.color = '#ff8c42';
    status.textContent = `Request failed: ${e}`;
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Disk reclaim (host_health disk_low remediation)
// ───────────────────────────────────────────────────────────────────────────

// Decimal byte humanizer — mirrors disk_reclaim.human_bytes server-side (GB,
// /1000) so the UI breakdown reads the same as the alert body.
function _alHumanBytes(n) {
  n = Number(n) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(n) >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(1)} ${units[i]}`;
}

function _alToggleReclaimPanel(sigId) {
  const el = document.getElementById(`al-reclaim-${sigId}`);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function _alRunReclaim(sigId) {
  const runBtn = document.getElementById(`al-reclaim-${sigId}-run`);
  const status = document.getElementById(`al-reclaim-${sigId}-status`);
  const resultEl = document.getElementById(`al-rem-result-${sigId}`);
  if (runBtn) { runBtn.disabled = true; runBtn.textContent = 'Reclaiming…'; }
  if (status) { status.style.color = 'var(--text3)'; status.textContent = ''; }
  try {
    // Reclaim the npm-cache category only (logs are not one-click reclaimable).
    // The server re-scans and acts only on what it finds NOW (TOCTOU-safe), so
    // the breakdown the operator just confirmed is advisory, not the authority
    // on what's freed.
    const r = await fetch('/api/host-health/reclaim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories: ['npm_cache'] }),
    });
    const j = await r.json().catch(() => ({}));
    // 207 = partial (some paths failed) — still a result worth showing.
    if (!r.ok && r.status !== 207) throw new Error(j.error || `HTTP ${r.status}`);
    _alToggleReclaimPanel(sigId);  // collapse the confirm panel
    if (resultEl) {
      const freed = j.freed_human || _alHumanBytes(j.freed_bytes || 0);
      const diskPct = j.disk && typeof j.disk.percent === 'number'
        ? `, disk now ${j.disk.percent}%` : '';
      const errs = (j.errors && j.errors.length)
        ? ` <span style="color:var(--text3)">(${j.errors.length} path(s) could not be reclaimed)</span>` : '';
      resultEl.innerHTML = `<div style="padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;font-size:0.78rem;color:var(--text2)">✓ Reclaimed ${escHtml(freed)}${escHtml(diskPct)}.${errs}</div>`;
    }
    // Refresh so the disk_low row falls off once the next host_health sweep
    // resolves it (the scan cache was reset server-side).
    setTimeout(_alRefreshActive, 2000);
  } catch (e) {
    if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Reclaim now'; }
    if (status) { status.style.color = 'var(--red)'; status.textContent = `Failed: ${e}`; }
  }
}

async function _alLoadLane(flavor) {
  // "reports" is a UI-only alias for the maintenance lane — it surfaces the
  // same signals under the Reports tab.
  const apiFlavor = flavor === 'reports' ? 'maintenance' : flavor;
  const el = document.getElementById(`al-${flavor}-body`);
  const countEl = document.getElementById(`al-count-${flavor}`);
  // Mirror targets so a single signal fetch can populate every visible lane:
  //   maintenance → maint-alerts-body (legacy)
  //   reports     → reports-alerts-body (new canonical home)
  const mirrorEls = [];
  if (apiFlavor === 'maintenance') {
    const maintEl = document.getElementById('maint-alerts-body');
    const reportsEl = document.getElementById('reports-alerts-body');
    if (maintEl) mirrorEls.push(maintEl);
    if (reportsEl) mirrorEls.push(reportsEl);
  }
  if (!el && mirrorEls.length === 0) return;
  try {
    // limit=1000 matches the server max. With ~250 active maintenance
    // signals on a typical pod, 200 was hitting the cap and silently
    // truncating producers from the client-side chip filter (the
    // 2026-05-26 bulk-dismiss bug: 28 of 42 compliance_scan signals
    // were hidden behind the cap, so dismissing "all visible"
    // re-populated from the truncated tail on refresh). If the server
    // still truncates, the banner below surfaces it.
    // state=firing, no flavor filter — see spec-alerts-count-normalization-2026-06-06.
    // Prior URL was `flavor=${apiFlavor}` (no state), which silently
    // mixed snoozed rows into the "FIRING ALERTS (NEEDING ACTION)"
    // heading AND hid 22 security-flavor + 7 activity-flavor signals
    // that producers were writing all along. Snoozed signals now live
    // on a dedicated Snoozed sub-tab; the flavor axis is no longer a
    // visibility gate.
    const d = await api('GET', `/api/signals?state=firing&limit=1000`);
    const allSigs = d.signals || [];
    // Severity gate runs first (info-tier hidden by default); category
    // gate runs second so the tab counts reflect what the operator
    // would actually see if they switched tabs. Order matters — the
    // tabs are rendered from the post-severity list so the "(N)" count
    // matches the visible row count when that tab is active.
    const severityFiltered = _alFilterBySeverity(allSigs);
    if (apiFlavor === 'maintenance') _alRenderCategoryTabs(severityFiltered);
    const sigs = _alFilterByCategory(severityFiltered);
    // Snapshot for the evo chat drawer's page-context-pack. The drawer
    // sends this along with every chat turn so evo can answer "what's
    // the worst alert here" without the operator pasting anything.
    // Only firing signals are surfaced; bound to top 12 to keep the
    // context-pack payload bounded.
    //
    // Two snapshot buckets get written:
    //   * maintenance — legacy alerts lane (page-id when alerts were
    //     under Maintenance). Kept for back-compat with the
    //     'maintenance' context-pack entry.
    //   * reports — the new canonical home (page-id = reports when
    //     the operator is on Reports → Alerts). Read by the
    //     'reports' context-pack entry. Without this writer, evo on
    //     the Reports page had no structured awareness of which
    //     alerts were on screen — only the raw signals tool.
    //
    // Both writers spread ``_prev`` so concurrent producers
    // (subscription poller, watchlist loader, etc.) can't clobber
    // each other's sibling fields — same pattern as the security
    // snapshot writers (see PR #1366 / test_security_context_snapshot).
    if (apiFlavor === 'maintenance') {
      window._evoContextSnapshots = window._evoContextSnapshots || {};
      const firing = allSigs.filter(s => (s.state || '') === 'firing');
      const snoozed = allSigs.filter(s => (s.state || '') === 'snoozed');
      const projectSig = (s) => ({
        id: s.id || null,
        title: s.title || s.type || s.id,
        bot_id: s.bot_id || null,
        severity: s.severity || null,
        producer: s.producer || null,
        scope: s.scope || null,
        last_observed_at: s.last_observed_at || null,
        state: s.state || null,
      });
      const _prevMaint = window._evoContextSnapshots.maintenance || {};
      window._evoContextSnapshots.maintenance = {
        ..._prevMaint,
        firing_count: firing.length,
        firing_top: firing.slice(0, 12).map(s => ({
          title: s.title || s.type || s.id,
          bot_id: s.bot_id || null,
          severity: s.severity || null,
          producer: s.producer || null,
        })),
      };
      // Reports → Alerts canonical snapshot. Richer than 'maintenance'
      // — includes id (so evo can call signal_action(action="snooze") /
      // signal_action(action="dismiss") without re-fetching), severity, scope,
      // observation timing, plus the active inner-subtab so evo knows
      // whether the operator is on Firing / History / Configure.
      const _prevReports = window._evoContextSnapshots.reports || {};
      const _activeInner = (() => {
        try {
          // Two possible parents — when alerts is the OUTER subtab on
          // the Reports page, the inner key is 'reports-alerts'.
          return localStorage.getItem('evolve_inner_reports-alerts')
            || document.querySelector('#reports-alerts .subtabs-inner .subtab-inner.active')?.dataset?.inner
            || 'firing';
        } catch (_) { return 'firing'; }
      })();
      const _activeOuter = (() => {
        try {
          return document.querySelector('#page-reports > .subtabs .subtab.active')?.dataset?.subtab
            || localStorage.getItem('evolve_subtab_reports')
            || null;
        } catch (_) { return null; }
      })();
      window._evoContextSnapshots.reports = {
        ..._prevReports,
        active_subtab: _activeOuter,
        active_inner: _activeInner,
        firing_count: firing.length,
        firing_top: firing.slice(0, 12).map(projectSig),
        snoozed_count: snoozed.length,
        snoozed_top: snoozed.slice(0, 6).map(projectSig),
        show_info_tier: !!_alShowInfo,
        cached_at: Math.floor(Date.now() / 1000),
      };
      // Refresh the drawer's context chip if it's open — the state
      // count just changed.
      if (typeof _evoDrawerUpdateContextChip === 'function') {
        _evoDrawerUpdateContextChip();
      }
    }
    if (countEl) {
      countEl.textContent = sigs.length || '0';
      if (allSigs.length !== sigs.length) {
        const calibrationHidden = allSigs.filter(_alIsCalibrationSignal).length;
        const parts = [];
        const infoHidden = (allSigs.length - sigs.length) - calibrationHidden;
        if (infoHidden > 0) {
          parts.push(`${infoHidden} info-tier signal(s) hidden — enable "show info" to reveal`);
        }
        if (calibrationHidden > 0) {
          parts.push(`${calibrationHidden} bootstrap-cost calibration signal(s) hidden — see the App bootstrap footprint chip on the bot detail page`);
        }
        countEl.title = parts.join('\n');
      } else {
        countEl.title = '';
      }
    }
    // Badge counts severity='alert' rows — the actual schema is
    // {info, warn, alert}, NOT {info, warn, critical, error}. The
    // old strings never matched, so the badge stayed hidden no matter
    // how many critical signals were firing. The state guard exists so
    // a future lane that pulls broader state can't accidentally relight
    // the badge for snoozed-with-severity-alert signals.
    const urgentCount = sigs.filter(s => s.state === 'firing' && s.severity === 'alert').length;
    const setBadge = id => {
      const b = document.getElementById(id);
      if (!b) return;
      if (urgentCount > 0) { b.textContent = String(urgentCount); b.style.display = ''; }
      else b.style.display = 'none';
    };
    if (apiFlavor === 'maintenance') {
      setBadge('badge-maint-alerts');
      setBadge('badge-reports-alerts');
    }
    // Reset per-render group registry so stale group-id → ids mappings
    // from a previous render can't leak into a fresh bulk-action click.
    window._alGroupMembers = {};
    // Reset the proposals-coalescer stash too — same shape concern; the
    // bulk-action handler reads from window._propGroupMembers.
    if (window._propGroupMembers) window._propGroupMembers = {};

    // Proposals routing — 2026-06-04 tab-split.
    //
    // Pre-tab-split, proposals with surface ∈ {firing, drift, cleanup,
    // null} were rendered as a tacked-on section below the Signal rows
    // on this lane (PR #2085 "intermix"). Operator feedback: the
    // half-intermix produced a visually-confusing two-block view that
    // matched neither the original "tab-separated" mental model nor a
    // true severity-interleaved one. The clean answer is to give
    // Proposals their own subtab on Reports → Alerts; the Firing lane
    // shows only Signals. See _alLoadProposalsTab below.
    //
    // The dedup logic that previously hid Signal-linked proposals from
    // the Firing-lane render now lives in _alLoadProposalsTab — same
    // rule, different surface. Paired-row Act buttons on Signals
    // (PR #2085 backref work) continue to surface action affordances
    // for the linked-pair case directly on the Firing lane.
    const html = (!sigs.length)
      ? (apiFlavor === 'activity'
        ? '<div class="empty">No active activity signals. The pod is quiet — the next watchdog run will populate this view if any anomaly fires.</div>'
        : '<div class="empty">No alerts. Nothing in the pod needs operator attention right now.</div>')
      : (
          (_alGroupSimilar
            ? _alGroupSignals(sigs).map(_alGroupRow).join('')
            : sigs.map(_alSignalRow).join(''))
      );
    if (el) el.innerHTML = html;
    mirrorEls.forEach(m => { m.innerHTML = html; });
    // Reports → Alerts: re-render filter chips + reset selection bar
    // any time the lane reloads. Selection is intentionally cleared on
    // reload — the underlying signal ids may have moved subdirs.
    if (apiFlavor === 'maintenance' && document.getElementById('reports-alerts-body')) {
      _alRenderTruncationBanner(d);
      _alRenderFilterChips();
      _alSelectionChanged();
      // Refresh the Proposals subtab badge whenever the Firing lane
      // reloads so the count stays current even when the operator
      // hasn't clicked into the Proposals subtab. The render writes
      // into the (potentially hidden) subtab body — small redundancy,
      // keeps the badge truthful.
      if (typeof _alLoadProposalsTab === 'function'
          && document.getElementById('reports-proposals-body')) {
        _alLoadProposalsTab();
      }
    }
  } catch(e) {
    const err = `<div class="empty">Error: ${escHtml(String(e))}</div>`;
    if (el) el.innerHTML = err;
    mirrorEls.forEach(m => { m.innerHTML = err; });
  }
}


// _alLoadProposalsTab — render the Proposals subtab on Reports → Alerts.
//
// 2026-06-04: surfaces proposals routed to the Alerts page (surface ∈
// {firing, drift, cleanup, null}) on their own subtab rather than
// appending them under the Firing lane. Same data source and dedup
// rules as the earlier intermix shape; only the rendering surface
// changes.
//
// Dedup rule (same as the pre-tab-split intermix): proposals linked to
// an actively-firing Signal via Signal.motivated_proposals are HIDDEN
// from this list — the operator acts on them via the paired-row Act
// button on the Firing lane. Showing them in two places would
// duplicate the affordance.
async function _alLoadProposalsTab() {
  // Top-level Reports → Proposals page. (Previously an inner subtab
  // under Reports → Alerts in PR #2132; promoted to a top-level peer
  // 2026-06-04.) The function name + write surface stay loosely
  // coupled so the click handler from the outer subtabs row reaches
  // here without any rewire.
  const el = document.getElementById('reports-proposals-body');
  if (!el) return;
  // Clear any stale group-id → ids stash from a previous render so
  // bulk-action handlers can't pick up dangling ids.
  if (window._propGroupMembers) window._propGroupMembers = {};
  try {
    // Fetch in parallel: proposals (the rendered content) + the active
    // signal list (used for dedup of paired-row proposals). Best-effort
    // on the signals fetch — if it fails, fall back to showing the
    // unfiltered proposals.
    const propParams = new URLSearchParams({
      include: 'pending,snoozed',
      exclude_generator_id: 'operator_ui',
    });
    const [propResp, sigResp] = await Promise.all([
      api('GET', `/api/arbiter/proposals?${propParams.toString()}`).catch(() => null),
      api('GET', '/api/signals?flavor=maintenance&limit=1000').catch(() => null),
    ]);
    const allProps = (propResp && propResp.proposals) || [];
    const allSigs = (sigResp && sigResp.signals) || [];

    // Surface routing inverse of Recommendations: include proposals
    // whose charter surface is one of firing/drift/cleanup OR is
    // null/unclassified. Only surface=improvement stays on
    // Recommendations.
    const _ALERT_SURFACES = new Set(['firing', 'drift', 'cleanup']);
    const inAlerts = allProps.filter(p =>
      !p.surface || _ALERT_SURFACES.has(p.surface)
    );

    // Dedup: proposals already represented via an actively-firing
    // Signal's paired-row Act button stay off this list. The Firing
    // lane is the canonical surface for the linked-pair case.
    const _ACTIONABLE = new Set([
      'draft', 'pending', 'approved_auto', 'approved_human', 'dispatched',
    ]);
    const linkedPropIds = new Set();
    for (const s of allSigs) {
      const view = Array.isArray(s.motivated_proposals_view)
        ? s.motivated_proposals_view : [];
      for (const pv of view) {
        if (pv && pv.id && _ACTIONABLE.has(pv.status || '')) {
          linkedPropIds.add(pv.id);
        }
      }
    }
    const standaloneProps = inAlerts.filter(p => !linkedPropIds.has(p.id));

    // Render
    let html = '';
    if (!standaloneProps.length) {
      html = '<div class="empty">No standalone findings. Generator-emitted actions linked to an active Signal show up as inline Act buttons on the Firing tab; app-side improvements live on <a href="#" onclick="event.preventDefault();document.querySelector(\'.nav-item[data-page=&quot;self-improvement&quot;]\')?.click()" style="color:var(--accent)">Recommendations</a>.</div>';
    } else if (typeof _propGroupProposals === 'function') {
      html = _alGroupSimilar
        ? _propGroupProposals(standaloneProps)
            .sort((a, b) => {
              const am = a.members.length, bm = b.members.length;
              if ((am > 1) !== (bm > 1)) return bm > 1 ? 1 : -1;
              return (b.top_score ?? 0) - (a.top_score ?? 0);
            })
            .map(g => _propGroupRow(g))
            .join('')
        : standaloneProps.map(p => renderProposalCard(p)).join('');
    } else {
      // Defensive fallback: _propGroupProposals isn't loaded.
      html = standaloneProps.map(p => `<div>${escHtml(p.problem || p.id)}</div>`).join('');
    }
    el.innerHTML = html;

    // Update the subtab badge with the actionable count (zero hides it).
    const badge = document.getElementById('badge-reports-proposals');
    if (badge) {
      const n = standaloneProps.length;
      if (n > 0) { badge.textContent = String(n); badge.style.display = ''; }
      else { badge.style.display = 'none'; }
    }

    // Reset the page-level bulk-action bar: selection state is wiped on
    // re-render (the underlying proposal ids may have moved subdirs).
    if (typeof _propSelectionChanged === 'function') _propSelectionChanged();
  } catch (e) {
    el.innerHTML = `<div class="empty">Error loading findings: ${escHtml(String(e))}</div>`;
  }
}


async function _alLoadHistory() {
  const targets = [
    document.getElementById('al-history-body'),
    document.getElementById('maint-alert-history-body'),
    document.getElementById('reports-alert-history-body'),
  ].filter(Boolean);
  if (!targets.length) return;
  const set = html => targets.forEach(t => { t.innerHTML = html; });
  try {
    const d = await api('GET', '/api/signals/history?limit=200');
    const entries = d.entries || [];
    if (!entries.length) {
      set('<div class="empty">No history yet. State changes (firing, snoozed, resolved, dismissed) appear here as they happen.</div>');
      return;
    }
    const html = `<div class="resp-table-wrap"><table class="resp-table data-table"><thead><tr>
      <th>When</th><th>Producer</th><th>Type</th><th>Title</th><th>Transition</th><th>Actor</th><th>Reason</th>
    </tr></thead><tbody>` + entries.map(e => {
      const transition = e.from_state ? `${escHtml(e.from_state)} → ${escHtml(e.to_state)}` : `(created) → ${escHtml(e.to_state)}`;
      return `<tr>
        <td style="white-space:nowrap">${ago(e.at)}</td>
        <td>${escHtml(e.producer || '')}</td>
        <td>${escHtml(e.type || '')}</td>
        <td>${escHtml(e.title || '')}</td>
        <td><code style="font-size:0.74rem">${transition}</code></td>
        <td>${escHtml(e.actor || '')}</td>
        <td>${escHtml(e.reason || '')}</td>
      </tr>`;
    }).join('') + '</tbody></table></div>';
    set(html);
    targets.forEach(_respTableLabelize);
  } catch(e) {
    set(`<div class="empty">Error: ${escHtml(String(e))}</div>`);
  }
}

function _alMagnitudeChip(m) {
  if (!m) return '';
  const v = typeof m.value === 'number' ? m.value : Number(m.value);
  const display = Number.isFinite(v) ? (v >= 100 ? v.toFixed(0) : v.toFixed(2)) : String(m.value);
  return `<span class="badge badge-muted" title="magnitude">${escHtml(display)} ${escHtml(m.unit || '')}</span>`;
}

// ── Snoozed sub-tab ────────────────────────────────────────────────────
//
// Reports → Alerts → Snoozed lists Signals in state=snoozed. They auto-wake
// at snoozed_until via the snooze-wake daemon; the "Unsnooze" button is
// for "I want to address it now." Kept deliberately simple (table, no
// category chips, no bulk actions) — snoozed is a short list by design.
// See internal/spec-alerts-count-normalization-2026-06-06.md.
function _alFormatSnoozedUntil(iso) {
  if (!iso) return '<span class="subtle">—</span>';
  let d;
  try { d = new Date(iso); } catch (_) { return escHtml(iso); }
  if (!(d instanceof Date) || isNaN(d)) return escHtml(iso);
  const now = new Date();
  const deltaMs = d.getTime() - now.getTime();
  const past = deltaMs < 0;
  const absMin = Math.abs(deltaMs) / 60000;
  let rel;
  if (absMin < 60) rel = `${Math.round(absMin)}m`;
  else if (absMin < 60 * 24) rel = `${(absMin / 60).toFixed(1)}h`;
  else rel = `${(absMin / 60 / 24).toFixed(1)}d`;
  const abs = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const label = past ? `${rel} ago (overdue)` : `in ${rel}`;
  return `<span title="${escHtml(abs)}">${escHtml(label)}</span>`;
}

async function _alLoadSnoozedLane() {
  const el = document.getElementById('reports-alerts-snoozed-body');
  const countEl = document.getElementById('count-reports-alerts-snoozed');
  if (!el) return;
  try {
    const d = await api('GET', '/api/signals?state=snoozed&limit=1000');
    const sigs = (d.signals || []).filter(s => s.severity !== 'info');
    if (countEl) countEl.textContent = sigs.length ? `(${sigs.length})` : '';
    if (!sigs.length) {
      el.innerHTML = '<div class="empty">No snoozed alerts. Snoozing a Firing alert moves it here until its wake time.</div>';
      return;
    }
    const sevClass = (s) => s === 'alert' ? 'badge-red' : s === 'warn' ? 'badge-yellow' : 'badge-muted';
    const html = `<div class="resp-table-wrap"><table class="resp-table data-table"><thead><tr>
      <th>Alert</th><th>Bot</th><th>Producer</th><th>Severity</th><th>Snoozed until</th><th></th>
    </tr></thead><tbody>` + sigs.map(s => {
      const sid = s.id || '';
      const title = s.title || s.type || s.id || '(no title)';
      return `<tr data-signal-id="${escHtml(sid)}">
        <td>${escHtml(title)}</td>
        <td>${escHtml(s.bot_id ? botLabel(s.bot_id) : (s.scope || ''))}</td>
        <td>${escHtml(s.producer || '')}</td>
        <td><span class="badge ${sevClass(s.severity)}">${escHtml(s.severity || '')}</span></td>
        <td style="white-space:nowrap">${_alFormatSnoozedUntil(s.snoozed_until)}</td>
        <td><button class="btn btn-ghost btn-sm" onclick="_alUnsnooze('${escHtml(sid)}', this)" title="Move back to Firing now">Unsnooze</button></td>
      </tr>`;
    }).join('') + '</tbody></table></div>';
    el.innerHTML = html;
    _respTableLabelize(el);
  } catch (e) {
    el.innerHTML = `<div class="empty">Error: ${escHtml(String(e))}</div>`;
  }
}

async function _alUnsnooze(signalId, btn) {
  if (!signalId) return;
  if (btn) { btn.disabled = true; btn.textContent = 'Unsnoozing…'; }
  try {
    const r = await api('POST', `/api/signals/${encodeURIComponent(signalId)}/unsnooze`, {});
    if (r && r.ok) {
      _alLoadSnoozedLane();
      // Refresh the Firing lane in the background so the row appears
      // there immediately if the operator switches tabs.
      try { _alLoadLane('reports'); } catch (_) {}
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Unsnooze'; }
      toast(`Unsnooze failed: ${(r && r.error) || 'unknown error'}`, 'err');
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Unsnooze'; }
    toast(`Unsnooze failed: ${e}`, 'err');
  }
}


