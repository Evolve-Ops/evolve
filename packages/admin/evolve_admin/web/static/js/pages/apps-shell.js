// ════════════════════════════════════════════════════════════════════════
// Page: Apps — the shell (AL-1.8a)
//
// docs/build-AL-1.8a-apps-shell.md · docs/design-apps-surface-2026-08-16.md
//
// The Apps page is pod-first: the row is an APP (one per app_id, across
// every bot), and the bot is a column and a filter — not a tab bar. This
// module owns the plumbing shared by the four lifecycle subtabs:
//
//   apps-shell.js       ← you are here: activation, view state, shared cells
//   apps-list.js        — the pod's defined apps (the Apps subtab)
//   apps-detail.js      — one app: header, bots × facts, signals
//   apps-discovered.js  — pod-wide drafts (the Discovered subtab)
//   apps-activity.js    — authoring / promotion / publish feed
//
// The Gallery subtab keeps its existing module (pages/gallery.js) — it was
// already extracted, and 1.8a moves rather than rewrites what survives.
// The manifest modal, the lifecycle actions (pause / archive / uninstall /
// adopt) and the repair chat likewise stay in pages/apps.js; the detail
// view reaches them through the same globals every other caller uses.
//
// THE HONESTY RULES THIS FILE ENCODES (design §7, brief §6):
//
//   * A missing number renders as "not measured", never as 0 or $0.00. The
//     API sends null for exactly this reason, so `== null` is the test —
//     `|| 0` would silently invent data.
//   * "Last ran" is tri-state: `seen` (AL-1.3 recorded it) or `cant_measure`
//     (nothing on the pod can answer yet). There is deliberately no third
//     branch claiming an app DIDN'T run — that needs the delivery ledger
//     (AL-2.1), which this chip does not build.
//   * No Active / Quiet / Inactive verdict, ever (removed 2026-06-13).
//   * No manifest field name reaches the screen: the words are "defined" and
//     "discovered", and an app is an app, never an "instance".
// ════════════════════════════════════════════════════════════════════════

// Which subtab is showing, and (within Apps) list vs. detail.
let _appsSubtab = 'apps';
let _appsDetailId = null;

// Page-level activation. Fires the visible subtab's loader only — the other
// three fetch when the operator opens them, which keeps a page visit to one
// round trip instead of four.
function appsPageActivate() {
  const active = document.querySelector('#page-apps .subtabs > .subtab.active');
  const name = (active && active.dataset.subtab) || 'apps';
  _appsSubtab = name;
  appsSubTabActivate(name);
}

function appsSubTabActivate(name) {
  _appsSubtab = name;
  // The Discovered drawer belongs to that subtab. Leaving the subtab with it
  // still marked open would bring it back on return, showing a draft the
  // operator has stopped thinking about.
  if (name !== 'discovered' && typeof appsCloseDiscoveredDraft === 'function') appsCloseDiscoveredDraft();
  if (name === 'apps') {
    // Returning to the tab from elsewhere lands on the list, not on
    // whatever app happened to be open three navigations ago.
    if (_appsDetailId) appsShowDetail(_appsDetailId);
    else appsLoadList();
  }
  if (name === 'discovered') appsLoadDiscovered();
  if (name === 'gallery' && typeof loadGallery === 'function') loadGallery();
  if (name === 'activity') appsLoadActivity();
}

// ── Shared cells ───────────────────────────────────────────────────────────

// Cost for a 7-day window. null → "not measured yet" (muted), which is a
// different statement from "$0.00".
function _appsCostCell(cost, opts) {
  const o = opts || {};
  if (cost == null) {
    return `<span style="color:var(--text3)" title="The per-app usage rollup (AL-1.3) has no entry here yet. That is 'not measured', not 'not used'.">not measured</span>`;
  }
  const measured = o.measuredBots, total = o.totalBots;
  const partial = (measured != null && total != null && measured < total)
    ? `<span style="color:var(--text3);font-size:0.72rem"> · ${measured} of ${total} bots measured</span>`
    : '';
  return `<span>$${Number(cost).toFixed(2)}</span>${partial}`;
}

// Per-grade turns, shown side by side. Grades are shown, never collapsed:
// `inferred` is a classifier's guess and may not be added to the
// deterministic total (design-app-attribution §3).
function _appsGradeBadges(breakdown) {
  const b = breakdown || {};
  const parts = [];
  if (b.scheduled && b.scheduled.turns) {
    parts.push(`<span class="badge badge-sm badge-neutral" title="Turns this app served on a schedule — attributed deterministically.">${b.scheduled.turns} scheduled</span>`);
  }
  if (b.explicit && b.explicit.turns) {
    parts.push(`<span class="badge badge-sm badge-neutral" title="Turns where the app was named explicitly — attributed deterministically.">${b.explicit.turns} explicit</span>`);
  }
  if (b.inferred && b.inferred.turns) {
    parts.push(`<span class="badge badge-sm badge-neutral" title="Turns a classifier only GUESSED belonged to this app. Shown separately and never added into the total.">~${b.inferred.turns} inferred</span>`);
  }
  return parts.join(' ');
}

// The tri-state "last ran / delivered" cell.
function _appsLastRunCell(lastRun) {
  const lr = lastRun || {};
  if (lr.state === 'seen' && lr.ts) {
    return `<span title="${escHtml(lr.ts)}">${escHtml(ago(lr.ts))}</span>`;
  }
  return `<span style="color:var(--text3)" title="Nothing on this pod records whether this app ran. Per-run and delivery history arrive with the delivery work (AL-2.1) — until then the honest answer is that we cannot tell.">can't measure</span>`;
}

// Bots-installed-on chips. Clicking one filters the list to that bot.
function _appsBotChips(bots, opts) {
  const o = opts || {};
  const list = bots || [];
  if (!list.length) return `<span style="color:var(--text3)">—</span>`;
  return list.map(b => {
    const id = b.bot_id || b;
    const label = typeof botLabel === 'function' ? botLabel(id) : id;
    if (o.plain) return `<span class="badge badge-sm badge-neutral">${escHtml(label)}</span>`;
    return `<span class="badge badge-sm badge-neutral apps-bot-chip" role="button" tabindex="0"
      onclick="event.stopPropagation();appsFilterByBot('${escHtml(id)}')"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();appsFilterByBot('${escHtml(id)}')}"
      title="Show only apps installed on ${escHtml(label)}">${escHtml(label)}</span>`;
  }).join(' ');
}

// Plain-language kind. "on_request" is a field value; "when asked" is what
// it means (design §7 — the Plex test applies to every label).
function _appsKindLabel(kind) {
  if (kind === 'scheduled') return 'On a schedule';
  if (kind === 'both') return 'Scheduled + when asked';
  if (kind === 'on_request') return 'When asked';
  return kind ? escHtml(kind) : '—';
}

// The v-next spec_version is an INT so updates can compare greater
// (app_spec.derive_spec_version packs YYYYMMDD * 100 + major*10 + minor).
// Ten digits on screen is a field value, not a fact an operator can use —
// so a packed value is decoded back into the date and version it was
// minted from, and anything else is shown as the plain number it is.
function _appsSpecVersionLabel(version) {
  const v = Number(version);
  if (!Number.isFinite(v) || v < 1) return '—';
  if (v >= 1000000000) {                       // packed YYYYMMDDvv
    const date = Math.floor(v / 100), tail = v % 100;
    const y = Math.floor(date / 10000);
    const m = String(Math.floor(date / 100) % 100).padStart(2, '0');
    const d = String(date % 100).padStart(2, '0');
    if (y > 2000 && y < 2200) {
      return `${y}-${m}-${d} · v${Math.floor(tail / 10)}.${tail % 10}`;
    }
  }
  return `v${v}`;
}

function _appsStatusBadge(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'active') return `<span class="badge badge-sm badge-ok">Active</span>`;
  if (s === 'paused') return `<span class="badge badge-sm badge-warn">Paused</span>`;
  if (s === 'hidden' || s === 'archived') return `<span class="badge badge-sm badge-neutral">Archived</span>`;
  if (s === 'draft') return `<span class="badge badge-sm badge-neutral">Draft</span>`;
  if (s === 'mixed') {
    return `<span class="badge badge-sm badge-neutral" title="This app is in different states on different bots — open it to see which.">Mixed</span>`;
  }
  return `<span class="badge badge-sm badge-neutral">${escHtml(status || 'unknown')}</span>`;
}

// "Install to…" is disabled for the whole of 1.8a: deterministic install is
// AL-1.5b, and wiring the non-deterministic path here would mean an install
// that may or may not reproduce the app. One helper so every disabled
// affordance carries the SAME explanation of what is missing and when it
// arrives (principle-alerts-explain-and-remediate).
function _appsInstallToButton(label) {
  return `<button class="btn btn-ghost btn-sm" disabled
    title="Installing an app onto another bot lands with deterministic install (AL-1.5b). Until then this pod can copy an app only through the Gallery, which is why the button is off rather than hidden.">${escHtml(label || 'Install to…')}</button>`;
}

function _appsEmpty(message, hint) {
  return `<div class="empty">${escHtml(message)}${
    hint ? `<div style="font-size:0.78rem;color:var(--text3);margin-top:6px">${escHtml(hint)}</div>` : ''
  }</div>`;
}

function _appsErrorBox(where, err) {
  const msg = (err && (err.error || err.message)) || 'the request failed';
  return `<div class="empty">Couldn't load ${escHtml(where)} — ${escHtml(String(msg))}.
    <div style="font-size:0.78rem;color:var(--text3);margin-top:6px">The page is showing nothing rather than something stale. Try refresh; if it persists, check the admin log.</div>
  </div>`;
}

window.appsPageActivate = appsPageActivate;
window.appsSubTabActivate = appsSubTabActivate;
window._appsCostCell = _appsCostCell;
window._appsGradeBadges = _appsGradeBadges;
window._appsLastRunCell = _appsLastRunCell;
window._appsBotChips = _appsBotChips;
window._appsKindLabel = _appsKindLabel;
window._appsStatusBadge = _appsStatusBadge;
window._appsInstallToButton = _appsInstallToButton;
window._appsSpecVersionLabel = _appsSpecVersionLabel;
