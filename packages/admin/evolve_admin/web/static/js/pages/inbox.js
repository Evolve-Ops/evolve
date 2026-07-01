// ════════════════════════════════════════════════════════════════════════
// Page: Inbox
//
// Issue Inbox surface — the durable tracking UI for filed intakes, plus
// the Tracked-repos card and the inbound-watcher feature pill.
//
// Four loaders dispatched from onPageActivate('inbox') on nav:
//   loadInbox()           — primary intake list + table + detail
//   loadInboxDrafts()     — pending evo-drafted replies
//   loadInboxRepos()      — Tracked repos card (Phase 3)
//   loadInboxFeatures()   — inbound watcher pill (Phase 4c)
//   loadInboxTriage()     — server-side AI-triage queue
//   loadInboxPolicy()     — policy/permission view
//
// State (top-level let, script-scope-shared per ES2015 semantics)
// captures the in-flight view: which intake is open, which draft set
// is cached, etc. Nothing here persists across reloads — state is
// re-fetched on nav.
//
// Loaded alongside the other static/js/pages/ modules in the script-tag
// cluster before the main inline <script>; same load-order discipline
// as pages/errors.js.
// ════════════════════════════════════════════════════════════════════════

// ── Inbox (Phase 1b of Issue Inbox spec) ─────────────────────────────────
//
// Durable tracking UI for filed intakes. Reads /api/inbox for the list of
// rows + computed unread_activity_count, /api/evo/intake/<id> for detail,
// /api/evo/intake/<id>/seen to clear the unread badge. Pure read-side
// state — the underlying activity_log is written by upstream_issues_watcher
// on the backend (see PR 1a). No evo / draft / approval flow here yet;
// that's Phase 2.

let _inboxItems = [];     // last fetched list (intake-row shape from /api/inbox)
let _inboxOpenId = null;  // id of intake whose detail card is currently shown

// ── Tracked repos (Phase 3 of Issue Inbox) ─────────────────────────────
//
// The "Tracked repos" card on the Inbox tab. Reads /api/inbox/repos for
// the list + per-target permission, renders a row per target with a
// maintainer-tier badge, and exposes an Add-repo modal that POSTs to
// the same endpoint. Removing a target hits DELETE /api/inbox/repos/<name>.
//
// Pure read+mutate on network.intake.github via the backend; no client
// state here that survives a page reload.

let _inboxRepos = [];
let _inboxSuggestions = [];

// ── Phase 4c: inbound watcher feature toggle ────────────────────────────
//
// The "Inbound triage watcher" pill on the Inbox tab. Reads
// /api/features/inbound_issues_watcher and renders enable/disable +
// last-run hint. Flipping the toggle POSTs to /api/features/<name>
// which writes install.json AND installs/uninstalls the launchd plist
// without the operator dropping to the CLI.
//
// Hidden by default — it only reveals when at least one repo is tracked
// (no point polling for inbound issues if you don't track any). The
// pill itself is always rendered when loadInboxFeatures runs; the
// rendering function decides whether to show it.

let _inboxWatcherStatus = null;

async function loadInboxFeatures() {
  const pill = document.getElementById('inbox-watcher-pill');
  if (!pill) return;
  try {
    const r = await fetch('/api/features/inbound_issues_watcher', { cache: 'no-store' });
    if (!r.ok) {
      pill.style.display = 'none';
      return;
    }
    const d = await r.json();
    _inboxWatcherStatus = d.status || null;
    _renderInboxWatcherPill();
  } catch (_e) {
    pill.style.display = 'none';
  }
}

function _renderInboxWatcherPill() {
  const pill = document.getElementById('inbox-watcher-pill');
  if (!pill || !_inboxWatcherStatus) return;
  const s = _inboxWatcherStatus;

  // Don't show when there are no tracked repos AND the watcher is off —
  // standard installs shouldn't have a stray "enable this" prompt before
  // they've added a repo to triage.
  const reposCount = (_inboxRepos || []).length;
  if (reposCount === 0 && !s.enabled) {
    pill.style.display = 'none';
    return;
  }

  pill.style.display = '';
  const stateEl = document.getElementById('inbox-watcher-state');
  const indicator = document.getElementById('inbox-watcher-indicator');
  const sub = document.getElementById('inbox-watcher-sub');
  const btn = document.getElementById('inbox-watcher-toggle');

  if (stateEl) {
    stateEl.textContent = s.enabled
      ? (s.plist_installed ? '— running' : '— enabled, daemon not yet installed')
      : '— off';
  }
  if (indicator) {
    indicator.style.color = s.enabled
      ? (s.plist_installed ? 'var(--good, #2f855a)' : 'var(--warn, #b7791f)')
      : 'var(--text3)';
  }
  if (sub) {
    const parts = [];
    if (s.enabled && s.last_activity_at) {
      parts.push(`last ran ${_relTime(new Date(s.last_activity_at))}`);
    } else if (s.enabled && s.plist_installed) {
      parts.push('hasn\'t reported yet — first poll within ~15 min');
    } else if (!s.enabled) {
      parts.push('Polls your tracked repos every ~15 min for new third-party issues, runs the LLM triage classifier, and surfaces them on the queue above.');
    }
    if (s.on_dev_profile && !s.enabled) {
      parts.push('Requires GitHub auth: `sudo -u evolve gh auth login`.');
    }
    sub.textContent = parts.join(' · ');
  }
  if (btn) {
    btn.textContent = s.enabled ? 'Turn off' : 'Turn on';
    btn.disabled = false;
  }
}

async function _inboxWatcherToggle() {
  if (!_inboxWatcherStatus) return;
  const want = !_inboxWatcherStatus.enabled;
  if (want && !await confirmModal(
    'Turn on the inbound triage watcher? It will poll the repos you ' +
    'maintain every ~15 minutes and capture new third-party issues as ' +
    'intakes for triage. Requires gh auth under the evolve user.'
  )) return;
  const btn = document.getElementById('inbox-watcher-toggle');
  if (btn) {
    btn.disabled = true;
    btn.textContent = want ? 'Turning on…' : 'Turning off…';
  }
  const logEl = document.getElementById('inbox-watcher-log');
  if (logEl) logEl.style.display = 'none';
  try {
    const r = await fetch('/api/features/inbound_issues_watcher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: want }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Watcher toggle failed: ${err.error || `HTTP ${r.status}`}`, 'err');
      await loadInboxFeatures();
      return;
    }
    const d = await r.json();
    _inboxWatcherStatus = d.status || _inboxWatcherStatus;
    _renderInboxWatcherPill();
    // Surface the install/uninstall log so the operator can see what happened.
    if (logEl && (d.log || []).length > 0) {
      logEl.textContent = d.log.join('\n');
      logEl.style.display = '';
    }
  } catch (e) {
    toast(`Watcher toggle failed: ${e}`, 'err');
    await loadInboxFeatures();
  }
}

async function loadInboxRepos() {
  const list = document.getElementById('inbox-repos-list');
  const empty = document.getElementById('inbox-repos-empty');
  if (!list || !empty) return;
  list.innerHTML = '<div style="font-size:0.82rem;color:var(--text3)">Loading…</div>';
  empty.style.display = 'none';
  // Split fetch from render so a render-side ReferenceError doesn't masquerade
  // as "Failed to load" (PR #1671 — _relTime undef looked like an API failure).
  let d;
  try {
    const r = await fetch('/api/inbox/repos', { cache: 'no-store' });
    if (!r.ok) {
      list.innerHTML = `<div style="font-size:0.82rem;color:#c53030">Couldn't load tracked repos (HTTP ${r.status}).</div>`;
      return;
    }
    d = await r.json();
  } catch (e) {
    list.innerHTML = `<div style="font-size:0.82rem;color:#c53030">Failed to load: ${escHtml(String(e))}</div>`;
    return;
  }
  _inboxRepos = d.repos || [];
  _inboxSuggestions = d.suggestions || [];
  _renderInboxRepos(_inboxRepos);
  // Re-render the watcher pill — the show/hide gate depends on repo count.
  if (_inboxWatcherStatus) _renderInboxWatcherPill();
}

function _renderInboxRepos(repos) {
  const list = document.getElementById('inbox-repos-list');
  const empty = document.getElementById('inbox-repos-empty');
  if (!list || !empty) return;
  // Suggestions are rendered regardless of whether any repos are configured —
  // a single un-added preset (e.g. evolve still un-added after openclaw was
  // clicked) keeps its chip until that preset is also added. The backend
  // does the filtering against existing targets.
  _renderInboxSuggestions();
  if (repos.length === 0) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  list.innerHTML = repos.map(r => {
    // Badge styling depends on the maintainer tier. ``scope_mismatch``
    // gets a red badge so the operator notices it on a glance — the
    // 404 it causes on /promote is the bug the inbox-token rotate path
    // exists to fix.
    const isScopeMismatch = r.permission === 'scope_mismatch';
    const badgeColor = r.is_maintainer
      ? 'background:rgba(56,161,105,0.15);color:#2f855a'  // green
      : isScopeMismatch
        ? 'background:rgba(220,38,38,0.15);color:#c53030'  // red — actionable
        : r.permission === 'unknown'
          ? 'background:rgba(120,120,120,0.10);color:var(--text3)'
          : 'background:rgba(120,120,120,0.10);color:var(--text2)';
    const badge = isScopeMismatch
      ? `<a style="font-size:0.7rem;padding:2px 6px;border-radius:3px;${badgeColor};cursor:pointer;text-decoration:none" onclick="_inboxOpenIntakeTokenModal('${escHtml(r.token_slot)}')" title="PAT lacks scope to see ${escHtml(r.owner)}/${escHtml(r.repo)} (private repos invisible without \`repo\` scope on classic PATs). Click to rotate.">${escHtml(r.tier_label)} — rotate →</a>`
      : `<span style="font-size:0.7rem;padding:2px 6px;border-radius:3px;${badgeColor}">${escHtml(r.tier_label)}</span>`;
    const defaultMark = r.is_default
      ? `<span style="font-size:0.7rem;padding:2px 6px;border-radius:3px;background:rgba(120,120,255,0.15);color:#5a67d8">default</span>`
      : '';
    // Identity row. Three states:
    //   - no token  → loud "no token — set up →" link (same as before)
    //   - configured + scope_mismatch → loud "rotate" link (deep-link to modal)
    //   - configured + healthy → quiet "as @login · rotate" so the operator
    //     always has a rotate path without waiting for a failure to surface
    const identity = !r.self_login
      ? `<a style="font-size:0.74rem;color:#c53030;cursor:pointer;text-decoration:underline" onclick="_inboxOpenIntakeTokenModal('${escHtml(r.token_slot)}')" title="Click to set up a GitHub token for this slot">no token in <code>${escHtml(r.token_slot)}</code> — set up →</a>`
      : `<span style="font-size:0.74rem;color:var(--text3)">as @${escHtml(r.self_login)} · <a style="color:var(--text3);text-decoration:underline;cursor:pointer" onclick="_inboxOpenIntakeTokenModal('${escHtml(r.token_slot)}')" title="Rotate the PAT stored in keystore slot &quot;${escHtml(r.token_slot)}&quot;">rotate</a></span>`;
    return `<div style="display:flex;align-items:center;gap:10px;padding:8px;border-radius:4px;background:var(--bg2)">
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <strong style="font-size:0.85rem">${escHtml(r.owner)}/${escHtml(r.repo)}</strong>
          ${defaultMark}
          ${badge}
        </div>
        <div style="display:flex;gap:8px;margin-top:4px">
          <span style="font-size:0.74rem;color:var(--text3)">name: <code>${escHtml(r.name)}</code></span>
          ${identity}
        </div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="_inboxRemoveRepo('${escHtml(r.name)}')" title="Remove this target">Remove</button>
    </div>`;
  }).join('');
}

// Render the quick-add chips inside the empty-state. Suggestions come from
// /api/inbox/repos.suggestions — the backend filters out anything already
// configured, so this just needs to render what it's given. Hidden when the
// suggestion list is empty (both presets added, or self-hosted fork where
// the dynamic preset doesn't resolve).
function _renderInboxSuggestions() {
  const container = document.getElementById('inbox-repos-suggestions');
  if (!container) return;
  const suggestions = _inboxSuggestions || [];
  if (suggestions.length === 0) {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }
  const chips = suggestions.map((s, idx) => {
    // Encode the index so the click handler doesn't need to re-parse owner/repo
    // — we read from the stashed _inboxSuggestions array.
    return `<button class="btn btn-ghost btn-sm"
      style="font-size:0.78rem;padding:4px 10px"
      onclick="_inboxQuickAdd(${idx})"
      title="${escHtml(s.hint || '')}">+ ${escHtml(s.label)}${s.make_default ? ' <span style=\"opacity:0.7\">(default)</span>' : ''}</button>`;
  }).join('');
  container.innerHTML =
    '<span style="color:var(--text3);font-style:normal">Quick add:</span>' + chips +
    '<span id="inbox-suggestions-status" style="font-size:0.78rem;color:var(--text3);font-style:italic;margin-left:6px"></span>';
  container.style.display = 'flex';
}

// One-click add a suggested preset. Pulls the body from the stashed
// _inboxSuggestions entry (preserves make_default + name without re-deriving),
// POSTs to /api/inbox/repos, refreshes on success. Errors land inline next to
// the chips so the operator sees what went wrong without a modal.
async function _inboxQuickAdd(idx) {
  const s = (_inboxSuggestions || [])[idx];
  if (!s) return;
  const statusEl = document.getElementById('inbox-suggestions-status');
  if (statusEl) {
    statusEl.textContent = `Adding ${s.label}…`;
    statusEl.style.color = 'var(--text3)';
  }
  try {
    const r = await fetch('/api/inbox/repos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        owner: s.owner,
        repo: s.repo,
        name: s.name,
        make_default: !!s.make_default,
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      if (statusEl) {
        statusEl.textContent = `Failed: ${d.error || `HTTP ${r.status}`}`;
        statusEl.style.color = '#c53030';
      }
      return;
    }
    // Reload — the just-added preset disappears from suggestions and shows
    // up in the repos list.
    await loadInboxRepos();
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Failed: ${e}`;
      statusEl.style.color = '#c53030';
    }
  }
}

function _inboxOpenAddRepoModal() {
  const modal = document.getElementById('inbox-add-repo-modal');
  if (!modal) return;
  // Reset form
  ['inbox-add-repo-owner', 'inbox-add-repo-repo',
   'inbox-add-repo-name', 'inbox-add-repo-slot'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const defaultCheck = document.getElementById('inbox-add-repo-default');
  if (defaultCheck) defaultCheck.checked = false;
  const errEl = document.getElementById('inbox-add-repo-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  modal.style.display = 'flex';
  // Focus the owner field for a fast keyboard path.
  setTimeout(() => document.getElementById('inbox-add-repo-owner')?.focus(), 50);
}

// ── Intake-token modal (focused setup for github_intake) ──────────────────
//
// Opened from the inline "no token in <slot>" link on each repo row. Saves
// via POST /api/inbox/intake-token; backend validates against GitHub /user
// before writing. The slot field is pre-filled and read-only — clicking the
// link on a specific row scopes the modal to that row's slot, so per-target
// custom slots (github_intake_openclaw, etc.) just work without a dropdown.

function _inboxOpenIntakeTokenModal(slot) {
  const modal = document.getElementById('inbox-intake-token-modal');
  if (!modal) return;
  const slotEl = document.getElementById('inbox-intake-token-slot');
  const valueEl = document.getElementById('inbox-intake-token-value');
  const resultEl = document.getElementById('inbox-intake-token-result');
  if (slotEl) slotEl.value = slot || 'github_intake';
  if (valueEl) valueEl.value = '';
  if (resultEl) { resultEl.style.display = 'none'; resultEl.innerHTML = ''; }
  modal.style.display = 'flex';
  // Focus the token field — slot is read-only, so the operator goes
  // straight to the paste step.
  setTimeout(() => document.getElementById('inbox-intake-token-value')?.focus(), 50);
}

function _inboxCloseIntakeTokenModal() {
  const modal = document.getElementById('inbox-intake-token-modal');
  if (modal) modal.style.display = 'none';
}

async function _inboxSubmitIntakeToken() {
  const slot = (document.getElementById('inbox-intake-token-slot')?.value || '').trim();
  const token = (document.getElementById('inbox-intake-token-value')?.value || '').trim();
  const resultEl = document.getElementById('inbox-intake-token-result');
  const submitBtn = document.getElementById('inbox-intake-token-submit');

  function _showResult(msg, kind) {
    if (!resultEl) return;
    const color = kind === 'error' ? '#c53030' : (kind === 'ok' ? '#2f855a' : 'var(--text2)');
    const bg = kind === 'error' ? 'rgba(197,48,48,0.06)' : (kind === 'ok' ? 'rgba(56,161,105,0.06)' : 'var(--bg2)');
    resultEl.style.color = color;
    resultEl.style.background = bg;
    resultEl.innerHTML = msg;
    resultEl.style.display = '';
  }

  if (!token) {
    _showResult('Enter the token.', 'error');
    return;
  }

  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Saving…'; }
  try {
    const r = await fetch('/api/inbox/intake-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, slot }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      _showResult(escHtml(d.error || `HTTP ${r.status}`), 'error');
      return;
    }
    // Success — backend may include the resolved GitHub login when validation
    // succeeded, or a validation_warning when it couldn't reach GitHub.
    const loginMsg = d.login
      ? `Saved — authenticated as <strong>@${escHtml(d.login)}</strong>.`
      : (d.validation_warning
        ? `Saved. (Could not validate against GitHub right now: ${escHtml(d.validation_warning)}. The watcher will report any token problem on its next poll.)`
        : 'Saved.');
    _showResult(loginMsg, 'ok');
    // Refresh the repos list so the row flips from "no token" → "as @login".
    await loadInboxRepos();
    // Auto-close after a beat so the operator sees confirmation.
    setTimeout(_inboxCloseIntakeTokenModal, 1500);
  } catch (e) {
    _showResult(`Failed: ${escHtml(String(e))}`, 'error');
  } finally {
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Save'; }
  }
}

function _inboxCloseAddRepoModal() {
  const modal = document.getElementById('inbox-add-repo-modal');
  if (modal) modal.style.display = 'none';
}

async function _inboxSubmitAddRepo() {
  const owner = (document.getElementById('inbox-add-repo-owner')?.value || '').trim();
  const repo = (document.getElementById('inbox-add-repo-repo')?.value || '').trim();
  const name = (document.getElementById('inbox-add-repo-name')?.value || '').trim();
  const slot = (document.getElementById('inbox-add-repo-slot')?.value || '').trim();
  const makeDefault = document.getElementById('inbox-add-repo-default')?.checked || false;
  const errEl = document.getElementById('inbox-add-repo-error');
  const submitBtn = document.getElementById('inbox-add-repo-submit');

  if (!owner || !repo) {
    if (errEl) {
      errEl.textContent = 'Owner and repo are both required.';
      errEl.style.display = '';
    }
    return;
  }

  const body = { owner, repo };
  if (name) body.name = name;
  if (slot) body.token_slot = slot;
  if (makeDefault) body.make_default = true;

  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Adding…'; }
  try {
    const r = await fetch('/api/inbox/repos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) {
      if (errEl) {
        errEl.textContent = d.error || `HTTP ${r.status}`;
        errEl.style.display = '';
      }
      return;
    }
    _inboxCloseAddRepoModal();
    await loadInboxRepos();
  } catch (e) {
    if (errEl) {
      errEl.textContent = `Failed: ${e}`;
      errEl.style.display = '';
    }
  } finally {
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Add'; }
  }
}

async function _inboxRemoveRepo(name) {
  // Confirm via the native confirm dialog. A custom modal would be
  // friendlier but adds a chunk of UI; the native dialog is at least
  // an explicit second click before destructive action.
  if (!await confirmModal({ body: `Remove the "${name}" tracked repo? This stops the watcher from polling it; existing filed intakes are unaffected.`, danger: true })) {
    return;
  }
  try {
    const r = await fetch(`/api/inbox/repos/${encodeURIComponent(name)}`, {
      method: 'DELETE', cache: 'no-store',
    });
    if (!r.ok) {
      const d = await r.json();
      toast(`Couldn't remove: ${d.error || `HTTP ${r.status}`}`, 'err');
      return;
    }
    await loadInboxRepos();
  } catch (e) {
    toast(`Failed: ${e}`, 'err');
  }
}

// ── Triage queue (Phase 4b of Issue Inbox) ─────────────────────────────
//
// Surfaces inbound intakes (issues filed by OTHERS on tracked repos
// we maintain) as a row-per-item card with category / urgency badges
// and the classifier's recommendation. Hidden when empty so pods
// that don't have any inbound triage to do see a clean Inbox.

let _inboxTriageItems = [];
let _inboxTriageOpenId = null;

async function loadInboxTriage() {
  const tbody = document.getElementById('inbox-triage-tbody');
  const card = document.getElementById('inbox-triage-card');
  if (!tbody || !card) return;
  const urgencyFilter = document.getElementById('inbox-triage-filter-urgency')?.value || '';
  const url = urgencyFilter
    ? `/api/inbox/triage?urgency=${encodeURIComponent(urgencyFilter)}`
    : '/api/inbox/triage';
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) {
      // 404 is fine on installs where the route isn't wired (e.g. an
      // older admin daemon during a partial deploy); just hide the
      // card and move on.
      card.style.display = 'none';
      return;
    }
    const d = await r.json();
    _inboxTriageItems = d.items || [];
    if (_inboxTriageItems.length === 0) {
      // Empty triage queue → hide the entire card so non-maintainer
      // pods don't see a perpetually-empty section.
      card.style.display = 'none';
      return;
    }
    card.style.display = '';
    _renderInboxTriage(_inboxTriageItems);
    // Refresh the detail card if one is open AND still in the list.
    if (_inboxTriageOpenId) {
      const stillThere = _inboxTriageItems.find(
        r => r.intake?.id === _inboxTriageOpenId
      );
      if (stillThere) _inboxRenderTriageDetail(stillThere);
      else _inboxCloseTriageDetail();
    }
  } catch (e) {
    // Network/error: hide rather than wedge — operator can refresh.
    card.style.display = 'none';
  }
}

function _renderInboxTriage(items) {
  const tbody = document.getElementById('inbox-triage-tbody');
  if (!tbody) return;
  tbody.innerHTML = items.map(row => {
    const ix = row.intake || {};
    const triage = ix.triage || {};
    const prom = ix.promotion || {};
    const url = prom.github_issue_url || '';
    const number = prom.github_issue_number ?? '';
    const m = url.match(/github\.com\/([^/]+\/[^/]+)\/issues\//);
    const repoLabel = m ? m[1] : '—';
    const issueLabel = number
      ? `${escHtml(repoLabel)}#${escHtml(String(number))}`
      : escHtml(repoLabel);
    const title = escHtml((triage.inbound_title || (ix.body || '').split('\n')[0] || '').slice(0, 80));
    const author = escHtml(triage.inbound_author || '');

    // Urgency badge color
    const urgencyColors = {
      p0: 'background:#fee;color:#c53030',
      p1: 'background:#fef5e7;color:#dd6b20',
      p2: 'background:#fefcbf;color:#975a16',
      p3: 'background:rgba(120,120,120,0.10);color:var(--text2)',
    };
    const urgColor = urgencyColors[triage.urgency] || 'background:rgba(120,120,120,0.10);color:var(--text3)';
    const urgBadge = `<span style="font-size:0.7rem;padding:2px 6px;border-radius:3px;font-weight:600;${urgColor}">${escHtml(triage.urgency || 'unknown')}</span>`;

    // Category label
    const categoryLabels = {
      bug: '🐛 bug',
      feature_request: '✨ feature',
      question: '❓ question',
      duplicate: '🔗 duplicate',
      spam: '🚫 spam',
      docs: '📄 docs',
      unknown: '· unknown',
    };
    const catLabel = escHtml(categoryLabels[triage.category] || triage.category || 'unknown');

    // Recommendation summary
    const recLabels = {
      auto_close_duplicate: 'close as duplicate',
      auto_reply_clarifying: 'ask for clarification',
      route_to_admin: 'review yourself',
      needs_investigation: 'needs investigation',
      unknown: '—',
    };
    const recLabel = escHtml(recLabels[triage.recommendation] || triage.recommendation || '—');

    const triagedAt = row.triaged_at ? _relTime(new Date(row.triaged_at)) : '—';

    return `<tr style="cursor:pointer" onclick="_inboxOpenTriageDetail('${escHtml(ix.id)}')">
      <td>${urgBadge}</td>
      <td>${catLabel}</td>
      <td><strong>${issueLabel}</strong><div style="font-size:0.78rem;color:var(--text2)">${title}</div></td>
      <td>@${author}</td>
      <td>${recLabel}</td>
      <td>${escHtml(triagedAt)}</td>
    </tr>`;
  }).join('');
}

function _inboxOpenTriageDetail(intakeId) {
  const row = _inboxTriageItems.find(r => r.intake?.id === intakeId);
  if (!row) return;
  _inboxTriageOpenId = intakeId;
  _inboxRenderTriageDetail(row);
  document.getElementById('inbox-triage-detail-card')?.scrollIntoView({
    behavior: 'smooth', block: 'nearest',
  });
}

function _inboxCloseTriageDetail() {
  _inboxTriageOpenId = null;
  const card = document.getElementById('inbox-triage-detail-card');
  if (card) card.style.display = 'none';
}

function _inboxRenderTriageDetail(row) {
  const ix = row.intake || {};
  const triage = ix.triage || {};
  const prom = ix.promotion || {};
  const card = document.getElementById('inbox-triage-detail-card');
  if (!card) return;
  card.style.display = '';

  const url = prom.github_issue_url || '';
  const m = url.match(/github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)/);

  const titleEl = document.getElementById('inbox-triage-detail-title');
  if (titleEl) {
    titleEl.innerHTML = m
      ? `${escHtml(m[1])}#${escHtml(m[2])} — ${escHtml(triage.inbound_title || '')}`
      : escHtml(triage.inbound_title || '(no title)');
  }

  const meta = document.getElementById('inbox-triage-detail-meta');
  if (meta) {
    const parts = [
      `filed by @${escHtml(triage.inbound_author || '?')}`,
      `triaged ${row.triaged_at ? _relTime(new Date(row.triaged_at)) : '—'}`,
    ];
    if (url) {
      parts.push(`<a href="${escHtml(url)}" target="_blank" rel="noopener">view on GitHub ↗</a>`);
    }
    meta.innerHTML = parts.join(' · ');
  }

  const bodyEl = document.getElementById('inbox-triage-detail-body');
  if (bodyEl) {
    // The intake body has "title\n\nbody"; the triage record has the
    // pre-truncated snippet. Use the full intake body (textContent for
    // XSS safety on the actual issue content).
    bodyEl.textContent = ix.body || triage.inbound_body_short || '(empty)';
  }

  const verdictEl = document.getElementById('inbox-triage-detail-verdict');
  if (verdictEl) {
    const lines = [];
    lines.push(`<div><strong>Category:</strong> ${escHtml(triage.category || 'unknown')}</div>`);
    lines.push(`<div><strong>Merit:</strong> ${escHtml(triage.merit || 'unknown')}</div>`);
    lines.push(`<div><strong>Urgency:</strong> ${escHtml(triage.urgency || 'unknown')}</div>`);
    lines.push(`<div><strong>Recommendation:</strong> ${escHtml(triage.recommendation || 'unknown')}</div>`);
    if (triage.estimated_effort && triage.estimated_effort !== 'unknown') {
      lines.push(`<div><strong>Effort:</strong> ${escHtml(triage.estimated_effort)}</div>`);
    }
    if ((triage.duplicate_of || []).length > 0) {
      lines.push(`<div><strong>Duplicate of:</strong> ${escHtml((triage.duplicate_of || []).join(', '))}</div>`);
    }
    if ((triage.draft_labels || []).length > 0) {
      lines.push(`<div><strong>Suggested labels:</strong> ${escHtml((triage.draft_labels || []).join(', '))}</div>`);
    }
    lines.push(`<div><strong>Confidence:</strong> ${escHtml(((triage.confidence ?? 0) * 100).toFixed(0))}%</div>`);
    if (triage.reasoning) {
      lines.push(`<div style="margin-top:6px;color:var(--text2);font-style:italic">${escHtml(triage.reasoning)}</div>`);
    }
    verdictEl.innerHTML = lines.join('');
  }

  const draftWrap = document.getElementById('inbox-triage-detail-draft-wrap');
  const draft = document.getElementById('inbox-triage-detail-draft');
  if (draftWrap && draft) {
    if (triage.draft_reply) {
      // Operator-trusted textContent — the draft is from our classifier,
      // but the issue body that may have influenced it is untrusted, so
      // belt-and-suspenders here.
      draft.textContent = triage.draft_reply;
      draftWrap.style.display = '';
    } else {
      draftWrap.style.display = 'none';
    }
  }

  // Phase 5: auto-action status pill + Apply control.
  _inboxRenderTriageAutoSection(ix);
}

// ── Phase 5: triage auto-action status + manual apply ────────────────────

function _inboxRenderTriageAutoSection(ix) {
  const triage = ix.triage || {};
  const auto = ix.auto_action || null;
  const statusEl = document.getElementById('inbox-triage-detail-auto-status');
  const applyWrap = document.getElementById('inbox-triage-detail-apply-wrap');
  const applyHelp = document.getElementById('inbox-triage-detail-apply-help');
  const applyBtn = document.getElementById('inbox-triage-detail-apply-btn');

  if (auto) {
    // Auto-action has already fired. Show the pill, hide the Apply
    // controls. Within the 24h window, expose an Undo button.
    if (applyWrap) applyWrap.style.display = 'none';
    if (!statusEl) return;
    statusEl.style.display = '';
    const acted = auto.acted_at ? _relTime(new Date(auto.acted_at)) : '?';
    const kindLabel = ({
      close_duplicate: 'Closed as duplicate',
      reply_clarifying: 'Posted clarifying reply',
      label_only: 'Applied labels',
    })[auto.kind] || auto.kind;
    const lines = [];
    if (auto.undone) {
      lines.push(`<div><strong>${escHtml(kindLabel)}</strong> — undone</div>`);
      lines.push(`<div style="font-size:0.78rem;color:var(--text3);margin-top:2px">Action ${escHtml(acted)} · reversed.</div>`);
    } else {
      const undoable = auto.undo_deadline_at && (new Date(auto.undo_deadline_at) > new Date());
      lines.push(`<div><strong>${escHtml(kindLabel)}</strong> · ${escHtml(acted)}</div>`);
      if (auto.reason) {
        lines.push(`<div style="font-size:0.78rem;color:var(--text3);margin-top:2px">via ${escHtml(auto.reason)} policy</div>`);
      }
      if (undoable) {
        const deadlineStr = _relTime(new Date(auto.undo_deadline_at));
        lines.push(
          `<div style="margin-top:6px;display:flex;gap:8px;align-items:center">` +
          `<button class="btn btn-ghost btn-sm" onclick="_inboxTriageUndo()">Undo</button>` +
          `<span style="font-size:0.78rem;color:var(--text3)">undo expires ${escHtml(deadlineStr)}</span>` +
          `</div>`
        );
      } else {
        lines.push(`<div style="font-size:0.78rem;color:var(--text3);margin-top:6px">Undo window expired — this action is permanent.</div>`);
      }
    }
    statusEl.innerHTML = lines.join('');
    return;
  }

  // No auto-action yet. Hide the pill, show the Apply button if the
  // recommendation maps to a runnable action.
  if (statusEl) statusEl.style.display = 'none';
  if (!applyWrap || !applyBtn) return;

  const rec = (triage.recommendation || '').toString();
  const map = {
    auto_close_duplicate: 'Close as duplicate',
    auto_reply_clarifying: 'Post the suggested reply',
  };
  const label = map[rec];
  if (label) {
    applyWrap.style.display = '';
    applyBtn.textContent = label;
    applyBtn.disabled = false;
    if (applyHelp) {
      applyHelp.textContent = 'Runs immediately. Undo is available for 24 hours.';
    }
  } else if ((triage.draft_labels || []).length > 0) {
    // No actionable recommendation, but the classifier suggested labels.
    // Surface a labels-only action as a secondary path.
    applyWrap.style.display = '';
    applyBtn.textContent = 'Apply suggested labels';
    applyBtn.dataset.kindOverride = 'label_only';
    applyBtn.disabled = false;
    if (applyHelp) {
      applyHelp.textContent = `Applies: ${(triage.draft_labels || []).join(', ')}`;
    }
  } else {
    applyWrap.style.display = 'none';
  }
}

async function _inboxTriageApply() {
  if (!_inboxTriageOpenId) return;
  const btn = document.getElementById('inbox-triage-detail-apply-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Applying…';
  }
  const body = {};
  if (btn?.dataset?.kindOverride) body.kind = btn.dataset.kindOverride;
  try {
    const r = await fetch(`/api/inbox/triage/${encodeURIComponent(_inboxTriageOpenId)}/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Apply failed: ${err.error || r.status}`, 'err');
      if (btn) btn.disabled = false;
      return;
    }
    // Reload the triage list so the row reflects the new auto_action,
    // then re-render the detail view.
    await loadInboxTriage();
  } finally {
    // The reload either repaints the controls or removes the row.
  }
}

async function _inboxTriageUndo() {
  if (!_inboxTriageOpenId) return;
  if (!await confirmModal({ body: 'Reverse this auto-action? The GitHub issue will be returned to its prior state.', danger: true })) {
    return;
  }
  try {
    const r = await fetch(`/api/inbox/triage/${encodeURIComponent(_inboxTriageOpenId)}/undo`, {
      method: 'POST',
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Undo failed: ${err.error || r.status}`, 'err');
      return;
    }
    await loadInboxTriage();
  } catch (e) {
    toast(`Undo failed: ${e}`, 'err');
  }
}

// ── Phase 5: auto-response policy panel ─────────────────────────────────

let _inboxPolicyOpen = false;

function _inboxPolicyToggle() {
  _inboxPolicyOpen = !_inboxPolicyOpen;
  const body = document.getElementById('inbox-triage-policy-body');
  const chev = document.getElementById('inbox-triage-policy-chevron');
  if (body) body.style.display = _inboxPolicyOpen ? '' : 'none';
  // chev is now a .expand-icon SVG; toggle .is-open instead of textContent.
  if (chev) chev.classList.toggle('is-open', _inboxPolicyOpen);
  if (_inboxPolicyOpen) loadInboxPolicy();
}

async function loadInboxPolicy() {
  try {
    const r = await fetch('/api/inbox/triage/policy', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    const p = d.policy || {};
    document.getElementById('inbox-triage-policy-enabled').checked = !!p.enabled;
    document.getElementById('inbox-triage-policy-close-dup').checked = !!p.close_duplicate_enabled;
    document.getElementById('inbox-triage-policy-close-dup-conf').value = (p.close_duplicate_min_confidence ?? 0.9).toFixed(2);
    document.getElementById('inbox-triage-policy-reply').checked = !!p.reply_clarifying_enabled;
    document.getElementById('inbox-triage-policy-reply-conf').value = (p.reply_clarifying_min_confidence ?? 0.85).toFixed(2);
    document.getElementById('inbox-triage-policy-label').checked = !!p.label_only_enabled;
    document.getElementById('inbox-triage-policy-label-conf').value = (p.label_only_min_confidence ?? 0.7).toFixed(2);
    const summary = document.getElementById('inbox-triage-policy-summary');
    if (summary) {
      if (!p.enabled) {
        summary.textContent = 'disabled (default — opt in below)';
      } else {
        const on = [];
        if (p.close_duplicate_enabled) on.push('close-dup');
        if (p.reply_clarifying_enabled) on.push('reply');
        if (p.label_only_enabled) on.push('label');
        summary.textContent = on.length
          ? `enabled — ${on.join(' · ')}`
          : 'globally on, but no actions selected';
      }
    }
  } catch (_e) { /* silent */ }
}

async function _inboxPolicySave() {
  const body = {
    enabled: document.getElementById('inbox-triage-policy-enabled').checked,
    close_duplicate_enabled: document.getElementById('inbox-triage-policy-close-dup').checked,
    close_duplicate_min_confidence: parseFloat(document.getElementById('inbox-triage-policy-close-dup-conf').value || '0.9'),
    reply_clarifying_enabled: document.getElementById('inbox-triage-policy-reply').checked,
    reply_clarifying_min_confidence: parseFloat(document.getElementById('inbox-triage-policy-reply-conf').value || '0.85'),
    label_only_enabled: document.getElementById('inbox-triage-policy-label').checked,
    label_only_min_confidence: parseFloat(document.getElementById('inbox-triage-policy-label-conf').value || '0.7'),
  };
  const saved = document.getElementById('inbox-triage-policy-saved');
  if (saved) saved.textContent = 'Saving…';
  try {
    const r = await fetch('/api/inbox/triage/policy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      if (saved) saved.textContent = `Save failed (HTTP ${r.status})`;
      return;
    }
    if (saved) saved.textContent = 'Saved.';
    setTimeout(() => { if (saved) saved.textContent = ''; }, 2500);
    await loadInboxPolicy();
  } catch (e) {
    if (saved) saved.textContent = `Save failed: ${e}`;
  }
}


async function loadInbox() {
  const tbody = document.getElementById('inbox-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="6" class="resp-table-fullspan"><div class="err-empty">Loading…</div></td></tr>';

  const unreadOnly = document.getElementById('inbox-filter-unread')?.checked ? '1' : '';
  const url = unreadOnly ? '/api/inbox?unread_only=1' : '/api/inbox';

  // Split fetch from render so a render-side ReferenceError doesn't masquerade
  // as "Failed to load" (PR #1671 — _relTime undef looked like an API failure).
  let d;
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) {
      tbody.innerHTML = `<tr><td colspan="6" class="resp-table-fullspan"><div class="err-empty">Could not load inbox (HTTP ${r.status}).</div></td></tr>`;
      return;
    }
    d = await r.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="resp-table-fullspan"><div class="err-empty">Failed to load: ${escHtml(String(e))}</div></td></tr>`;
    return;
  }
  _inboxItems = d.items || [];
  _renderInboxSummary(_inboxItems);
  _renderInboxBadge(_inboxItems);
  _renderInboxTable(_inboxItems);
  // If a detail card is currently open, refresh its content too so
  // a new activity event lands without the operator having to close
  // and reopen.
  if (_inboxOpenId) {
    const stillThere = _inboxItems.find(r => r.intake?.id === _inboxOpenId);
    if (stillThere) _inboxRenderDetail(stillThere);
    else _inboxCloseDetail();
  }
}

function _renderInboxSummary(items) {
  const total = document.getElementById('inbox-total');
  const unread = document.getElementById('inbox-unread');
  if (total) total.textContent = items.length;
  if (unread) {
    const n = items.reduce((s, r) => s + (r.unread_activity_count || 0), 0);
    unread.textContent = n;
  }
}

function _renderInboxBadge(items) {
  const badge = document.getElementById('badge-inbox');
  if (!badge) return;
  // Badge counts unread-tracked + pending-drafts so the operator sees
  // "things waiting for me." _inboxDraftsCount is set by loadInboxDrafts.
  const unread = items.filter(r => (r.unread_activity_count || 0) > 0).length;
  const drafts = typeof _inboxDraftsCount === 'number' ? _inboxDraftsCount : 0;
  const n = unread + drafts;
  if (n > 0) {
    badge.textContent = String(n);
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

// ── Drafts (pre-promotion intakes from `evo intake` / `evo improve`)
//
// Reads /api/evo/intake?state=open and ?state=triaged, renders a card
// table above the Tracked-issues table. The intake store already
// supports promote and dismiss endpoints (/api/evo/intake/<id>/promote
// and /dismiss) — this is purely a UI surface over the existing
// backend; no new server-side state.
//
// Card visibility: shown when there's at least one draft, otherwise
// hidden so a clean pod doesn't show an empty section.
let _inboxDraftsCount = 0;
let _inboxDraftsCache = [];
let _inboxDraftsOpenId = null;

async function loadInboxDrafts() {
  const tbody = document.getElementById('inbox-drafts-tbody');
  const card = document.getElementById('inbox-drafts-card');
  const countEl = document.getElementById('inbox-drafts-count');
  if (!tbody || !card) return;
  tbody.innerHTML = '<tr><td colspan="4" class="resp-table-fullspan"><div class="err-empty">Loading…</div></td></tr>';

  // open + triaged are both "pre-promotion" — combine into a single list
  // sorted newest-first. Promoted intakes (state=filed) appear in the
  // Tracked-issues table below, NOT here.
  let drafts = [];
  try {
    const [openR, triagedR] = await Promise.all([
      fetch('/api/evo/intake?state=open&limit=200', { cache: 'no-store' }),
      fetch('/api/evo/intake?state=triaged&limit=200', { cache: 'no-store' }),
    ]);
    if (openR.ok) {
      const d = await openR.json();
      drafts = drafts.concat(d.intakes || []);
    }
    if (triagedR.ok) {
      const d = await triagedR.json();
      drafts = drafts.concat(d.intakes || []);
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="resp-table-fullspan"><div class="err-empty">Failed to load drafts: ${escHtml(String(e))}</div></td></tr>`;
    card.style.display = '';
    return;
  }
  // Sort newest-first by id (which is date-prefixed, so this is
  // chronological).
  drafts.sort((a, b) => String(b.id || '').localeCompare(String(a.id || '')));
  _inboxDraftsCache = drafts;
  _inboxDraftsCount = drafts.length;
  if (countEl) countEl.textContent = String(drafts.length);

  if (drafts.length === 0) {
    card.style.display = 'none';
    // Recompute the nav badge — drafts count contributes.
    _renderInboxBadge(_inboxItems || []);
    return;
  }
  card.style.display = '';
  tbody.innerHTML = drafts.map(_renderInboxDraftRow).join('');
  _renderInboxBadge(_inboxItems || []);
}

function _renderInboxDraftRow(ix) {
  const id = String(ix.id || '');
  const kind = String(ix.kind || '');
  // Title: first line of body, capped — mirrors intake.promote._make_title
  // so the operator sees the same string that would land on GitHub.
  const firstLine = String(ix.body || '').split('\n')[0].trim();
  const title = firstLine.length > 72 ? firstLine.slice(0, 69) + '…' : (firstLine || '(no title)');
  const created = ix.created_at ? _relTime(ix.created_at) : '';
  const idEsc = escHtml(id);
  return `
    <tr style="cursor:pointer" onclick="_inboxDraftsOpenDetail('${idEsc}')">
      <td><span style="font-family:monospace;font-size:0.78rem;color:var(--text3);margin-right:8px">${idEsc.slice(-8)}</span>${escHtml(title)}</td>
      <td><span class="badge badge-muted" style="font-size:0.7rem">${escHtml(kind)}</span></td>
      <td style="font-size:0.78rem;color:var(--text3)" title="${escHtml(ix.created_at || '')}">${escHtml(created)}</td>
      <td style="text-align:right;white-space:nowrap" onclick="event.stopPropagation()">
        <button class="btn btn-ghost btn-sm" onclick="_inboxDraftsDismiss('${idEsc}')" title="Move to closed without filing">Dismiss</button>
        <button class="btn btn-primary btn-sm" onclick="_inboxDraftsPromote('${idEsc}')" title="File as a GitHub issue">Promote →</button>
      </td>
    </tr>`;
}

function _inboxDraftsOpenDetail(id) {
  const ix = _inboxDraftsCache.find(d => d.id === id);
  if (!ix) return;
  _inboxDraftsOpenId = id;
  const modal = document.getElementById('inbox-drafts-detail-modal');
  const titleEl = document.getElementById('inbox-drafts-detail-title');
  const metaEl = document.getElementById('inbox-drafts-detail-meta');
  const bodyEl = document.getElementById('inbox-drafts-detail-body');
  const errEl = document.getElementById('inbox-drafts-detail-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  if (titleEl) {
    const firstLine = String(ix.body || '').split('\n')[0].trim();
    titleEl.textContent = firstLine || ix.id;
  }
  if (metaEl) {
    metaEl.textContent = `${ix.id} · kind=${ix.kind || '?'} · state=${ix.state || '?'} · created ${ix.created_at || ''}`;
  }
  // textContent (not innerHTML) — intake body is operator/evo-authored
  // prose, may include angle brackets.
  if (bodyEl) bodyEl.textContent = ix.body || '';
  if (modal) modal.style.display = 'flex';
}

function _inboxDraftsCloseDetail() {
  _inboxDraftsOpenId = null;
  const modal = document.getElementById('inbox-drafts-detail-modal');
  if (modal) modal.style.display = 'none';
}

function _inboxDraftsPromoteFromDetail() {
  if (_inboxDraftsOpenId) _inboxDraftsPromote(_inboxDraftsOpenId, /*fromDetail=*/true);
}

function _inboxDraftsDismissFromDetail() {
  if (_inboxDraftsOpenId) _inboxDraftsDismiss(_inboxDraftsOpenId, /*fromDetail=*/true);
}

async function _inboxDraftsPromote(id, fromDetail) {
  const errEl = fromDetail ? document.getElementById('inbox-drafts-detail-error') : null;
  try {
    const r = await fetch(`/api/evo/intake/${encodeURIComponent(id)}/promote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ promoted_by: 'admin-ui' }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = d.error || `HTTP ${r.status}`;
      if (errEl) {
        errEl.style.display = '';
        errEl.textContent = `Could not promote: ${msg}`;
      } else {
        toast(`Could not promote ${id}: ${msg}`, 'err');
      }
      return;
    }
    // Success — close any open detail, refresh both lists.
    _inboxDraftsCloseDetail();
    await Promise.all([loadInboxDrafts(), loadInbox()]);
    const url = d.intake?.promotion?.github_issue_url;
    if (url) {
      // Best-effort: open the new GitHub issue in a new tab so the
      // operator immediately lands where the conversation will happen.
      window.open(url, '_blank', 'noopener');
    }
  } catch (e) {
    if (errEl) {
      errEl.style.display = '';
      errEl.textContent = `Promote failed: ${String(e)}`;
    } else {
      toast(`Promote failed: ${String(e)}`, 'err');
    }
  }
}

async function _inboxDraftsDismiss(id, fromDetail) {
  if (!fromDetail && !await confirmModal({ body: `Dismiss draft ${id} without filing?`, danger: true })) return;
  const errEl = fromDetail ? document.getElementById('inbox-drafts-detail-error') : null;
  try {
    const r = await fetch(`/api/evo/intake/${encodeURIComponent(id)}/dismiss`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actor: 'admin-ui', note: 'dismissed via Issues page' }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      const msg = d.error || `HTTP ${r.status}`;
      if (errEl) {
        errEl.style.display = '';
        errEl.textContent = `Could not dismiss: ${msg}`;
      } else {
        toast(`Could not dismiss ${id}: ${msg}`, 'err');
      }
      return;
    }
    _inboxDraftsCloseDetail();
    await loadInboxDrafts();
  } catch (e) {
    if (errEl) {
      errEl.style.display = '';
      errEl.textContent = `Dismiss failed: ${String(e)}`;
    } else {
      toast(`Dismiss failed: ${String(e)}`, 'err');
    }
  }
}

function _renderInboxTable(items) {
  const tbody = document.getElementById('inbox-tbody');
  if (!tbody) return;
  if (items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="resp-table-fullspan"><div class="err-empty">Nothing to show yet. Issues you file via <code>evo improve</code> or <code>evo intake promote</code> appear here once the watcher confirms they posted.</div></td></tr>';
    return;
  }
  tbody.innerHTML = items.map(row => {
    const ix = row.intake || {};
    const prom = ix.promotion || {};
    const url = prom.github_issue_url || '';
    const number = prom.github_issue_number ?? '';
    const unread = row.unread_activity_count || 0;
    const repoLabel = (() => {
      // Pull "owner/repo" out of the github_issue_url, e.g.
      // https://github.com/openclaw/openclaw/issues/84820 → openclaw/openclaw
      const m = (url || '').match(/github\.com\/([^/]+\/[^/]+)\/issues\//);
      return m ? m[1] : '—';
    })();
    const lastActivity = row.last_activity_at
      ? _relTime(new Date(row.last_activity_at))
      : '—';
    const filed = prom.promoted_at ? _relTime(new Date(prom.promoted_at)) : '—';
    const dot = unread > 0
      ? '<span title="' + unread + ' unread" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent)"></span>'
      : '<span style="display:inline-block;width:8px;height:8px"></span>';
    const issueLabel = number
      ? `${escHtml(repoLabel)}#${escHtml(String(number))}`
      : escHtml(repoLabel);
    const titlePreview = escHtml((ix.body || '').split('\n')[0].slice(0, 80));
    const ghLink = url
      ? `<a href="${escHtml(url)}" target="_blank" rel="noopener" class="btn btn-ghost btn-sm" title="Open on GitHub">↗ GitHub</a>`
      : '';
    return `<tr style="cursor:pointer" onclick="_inboxOpenDetail('${escHtml(ix.id)}')">
      <td>${dot}</td>
      <td><strong>${issueLabel}</strong><div style="font-size:0.78rem;color:var(--text2)">${titlePreview}</div></td>
      <td>${escHtml(ix.kind || '')}</td>
      <td>${escHtml(lastActivity)}</td>
      <td>${escHtml(filed)}</td>
      <td onclick="event.stopPropagation()">${ghLink}</td>
    </tr>`;
  }).join('');
}

function _inboxOpenDetail(intakeId) {
  const row = _inboxItems.find(r => r.intake?.id === intakeId);
  if (!row) return;
  _inboxOpenId = intakeId;
  _inboxRenderDetail(row);
  // Scroll into view so the operator doesn't lose context on a long list.
  document.getElementById('inbox-detail-card')?.scrollIntoView({
    behavior: 'smooth', block: 'nearest',
  });
}

function _inboxCloseDetail() {
  _inboxOpenId = null;
  const card = document.getElementById('inbox-detail-card');
  if (card) card.style.display = 'none';
}

function _inboxRenderDetail(row) {
  const ix = row.intake || {};
  const prom = ix.promotion || {};
  const card = document.getElementById('inbox-detail-card');
  if (!card) return;
  card.style.display = '';

  const title = document.getElementById('inbox-detail-title');
  if (title) {
    const url = prom.github_issue_url || '';
    const m = url.match(/github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)/);
    title.innerHTML = m
      ? `${escHtml(m[1])}#${escHtml(m[2])} — ${escHtml((ix.body || '').split('\n')[0].slice(0, 80))}`
      : escHtml((ix.body || '').split('\n')[0].slice(0, 80));
  }

  const meta = document.getElementById('inbox-detail-meta');
  if (meta) {
    const parts = [`intake \`${ix.id || '—'}\``, ix.kind || '—', ix.state || '—'];
    if (prom.promoted_at) parts.push(`filed ${_relTime(new Date(prom.promoted_at))}`);
    if (prom.github_issue_url) {
      parts.push(`<a href="${escHtml(prom.github_issue_url)}" target="_blank" rel="noopener">view on GitHub ↗</a>`);
    }
    meta.innerHTML = parts.join(' · ');
  }

  const bodyEl = document.getElementById('inbox-detail-body');
  if (bodyEl) bodyEl.textContent = ix.body || '';

  const activity = document.getElementById('inbox-detail-activity');
  if (activity) {
    const events = (ix.activity_log || []).slice().reverse();  // newest first
    if (events.length === 0) {
      activity.innerHTML = '<div style="font-size:0.85rem;color:var(--text3);font-style:italic">No activity yet. When a maintainer comments or the issue state changes, it shows up here.</div>';
    } else {
      const cursor = ix.last_seen_activity_at || '';
      activity.innerHTML = events.map(e => {
        const isUnread = e.observed_at > cursor;
        const ts = e.observed_at ? _relTime(new Date(e.observed_at)) : '—';
        const kindLabel = {
          new_comment: '💬 new comment',
          state_change: '↻ state change',
          closed: '✓ closed',
          reopened: '↺ reopened',
        }[e.kind] || `· ${escHtml(e.kind || 'event')}`;
        const unreadStyle = isUnread
          ? 'border-left:3px solid var(--accent);background:rgba(120,120,255,0.04)'
          : 'border-left:3px solid var(--border)';
        const snippet = e.snippet ? `<div style="margin-top:4px;font-size:0.85rem;line-height:1.5;color:var(--text2)">${escHtml(e.snippet)}</div>` : '';
        return `<div style="padding:8px 10px;margin-bottom:6px;${unreadStyle};border-radius:0 4px 4px 0">
          <div style="display:flex;gap:8px;font-size:0.78rem">
            <span style="font-weight:600">${kindLabel}</span>
            <span style="color:var(--text3)">·</span>
            <span style="color:var(--text2)">${escHtml(e.actor || '(unknown)')}</span>
            <span style="flex:1"></span>
            <span style="color:var(--text3)">${escHtml(ts)}</span>
          </div>
          ${snippet}
        </div>`;
      }).join('');
    }
  }

  // Hide the Mark-as-seen button if there's nothing new to mark.
  const btn = document.getElementById('inbox-mark-seen-btn');
  if (btn) btn.style.display = (row.unread_activity_count || 0) > 0 ? '' : 'none';

  // Render triage section — existing verdict if present, else "not triaged yet".
  _inboxRenderTriage(ix.triage);
}

// Render the triage block. Called from _inboxRenderDetail (on row click)
// and again from _inboxRunTriage (after a successful POST). Reads from
// the intake's triage field; if null, shows the "not triaged yet"
// invitation. Button label flips Triage now → Re-triage based on state.
function _inboxRenderTriage(triage) {
  const block = document.getElementById('inbox-detail-triage');
  const btn = document.getElementById('inbox-triage-btn');
  const errEl = document.getElementById('inbox-triage-error');
  if (errEl) errEl.style.display = 'none';
  if (!block) return;
  if (!triage || typeof triage !== 'object') {
    block.innerHTML = 'Not triaged yet. Click <strong>Triage now</strong> to ask the classifier for a verdict.';
    block.style.cssText = 'font-size:0.85rem;color:var(--text3);font-style:italic';
    if (btn) btn.textContent = 'Triage now';
    return;
  }
  // Render verdict. Show confidence + recommendation + category as the
  // top line; reasoning + draft_reply as expandable detail. Color-code
  // the recommendation to make routing decisions skim-readable.
  const cat = escHtml(triage.category || 'unknown');
  const rec = escHtml(triage.recommendation || 'unknown');
  const merit = escHtml(triage.merit || 'unknown');
  const urgency = escHtml(triage.urgency || 'unknown');
  const effort = escHtml(triage.estimated_effort || 'unknown');
  const conf = typeof triage.confidence === 'number' ? Math.round(triage.confidence * 100) : 0;
  const recColor = {
    auto_close_duplicate: '#c53030',
    auto_reply_clarifying: '#d97706',
    route_to_admin:        '#2563eb',
    needs_investigation:   '#7c3aed',
    unknown:               'var(--text3)',
  }[triage.recommendation || 'unknown'] || 'var(--text2)';
  const dups = (triage.duplicate_of || []).slice().map(d => escHtml(d)).join(', ');
  const labels = (triage.draft_labels || []).slice().map(l => `<span style="background:var(--bg2);padding:2px 6px;border-radius:3px;font-size:0.72rem;margin-right:4px">${escHtml(l)}</span>`).join('');
  const reasoning = triage.reasoning
    ? `<div style="margin-top:8px"><div style="font-weight:600;font-size:0.78rem;color:var(--text3);margin-bottom:4px">Reasoning</div><div style="white-space:pre-wrap;font-size:0.82rem;line-height:1.5;color:var(--text2)">${escHtml(triage.reasoning)}</div></div>`
    : '';
  const draft = triage.draft_reply
    ? `<div style="margin-top:8px"><div style="font-weight:600;font-size:0.78rem;color:var(--text3);margin-bottom:4px">Draft reply (not posted)</div><div style="white-space:pre-wrap;font-size:0.82rem;line-height:1.5;color:var(--text2);background:var(--bg2);padding:8px;border-radius:4px">${escHtml(triage.draft_reply)}</div></div>`
    : '';
  const triagedAt = triage.triaged_at
    ? `<span style="color:var(--text3);font-size:0.72rem">${escHtml(_relTime(new Date(triage.triaged_at)))}</span>`
    : '';
  block.style.cssText = 'font-size:0.85rem';
  block.innerHTML = `
    <div style="background:var(--bg2);border-radius:6px;padding:10px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span style="font-weight:600;color:${recColor}">${rec}</span>
        <span style="color:var(--text3)">·</span>
        <span><strong>${cat}</strong></span>
        <span style="color:var(--text3)">·</span>
        <span>merit: <strong>${merit}</strong></span>
        <span style="color:var(--text3)">·</span>
        <span>urgency: <strong>${urgency}</strong></span>
        <span style="color:var(--text3)">·</span>
        <span>effort: <strong>${effort}</strong></span>
        <span style="flex:1"></span>
        <span style="color:var(--text3)">confidence ${conf}%</span>
        ${triagedAt ? '<span style="color:var(--text3)">·</span>' : ''}
        ${triagedAt}
      </div>
      ${dups ? `<div style="margin-top:6px;font-size:0.78rem;color:var(--text2)">Possible duplicates: ${dups}</div>` : ''}
      ${labels ? `<div style="margin-top:8px">${labels}</div>` : ''}
      ${reasoning}
      ${draft}
    </div>
  `;
  if (btn) btn.textContent = 'Re-triage';
}

async function _inboxRunTriage() {
  if (!_inboxOpenId) return;
  const btn = document.getElementById('inbox-triage-btn');
  const errEl = document.getElementById('inbox-triage-error');
  if (errEl) errEl.style.display = 'none';
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await fetch(`/api/evo/intake/${encodeURIComponent(_inboxOpenId)}/triage`, {
      method: 'POST', cache: 'no-store',
    });
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      throw new Error(`HTTP ${r.status}${text ? ': ' + text : ''}`);
    }
    const data = await r.json();
    const ix = data.intake || {};
    // Update the cached row so subsequent re-renders see the new triage
    // without a full /api/inbox refresh.
    const row = _inboxItems.find(r => r.intake?.id === _inboxOpenId);
    if (row && row.intake) row.intake.triage = ix.triage || null;
    _inboxRenderTriage(ix.triage || null);
  } catch (e) {
    if (errEl) {
      errEl.textContent = `Triage failed: ${e.message || e}`;
      errEl.style.display = '';
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Triage now'; }
    return;
  }
  if (btn) btn.disabled = false;
}

async function _inboxMarkSeen() {
  if (!_inboxOpenId) return;
  const btn = document.getElementById('inbox-mark-seen-btn');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await fetch(`/api/evo/intake/${encodeURIComponent(_inboxOpenId)}/seen`, {
      method: 'POST', cache: 'no-store',
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    // Refresh list — the row's unread count drops to 0, detail re-renders.
    await loadInbox();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Mark as seen'; }
    toast(`Couldn't mark as seen: ${e}`, 'err');
  }
}
