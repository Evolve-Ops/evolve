// ════════════════════════════════════════════════════════════════════════
// Page: Integrations & Keys / Plugins (merged page)
//
// The Plugins page (formerly "Integrations & Keys" — pageRedirectRegistry
// still resolves "integrations-keys" → "plugins" for old deeplinks).
// Five subtabs share this cluster:
//
//   Plugins        — _ikPlugin* + ikLoadPlugins + ikRenderPlugins +
//                    role badges + per-plugin audit chip
//   MCP            — ikLoadMcp + ikRenderMcp + scan management
//   Activity       — ikLoadActivity (audit-log viewer)
//   Hooks          — ikLoadHooks + ikRenderHooks
//   Credentials    — ikRefreshKeys + GitHub status + general-access info +
//                    the per-credential rotation / scope display
//   Embeddings     — ikLoadEmbeddings + ikRenderEmbeddings +
//                    ikSetEmbeddingPrimary + chain-editor jump
//
// State (top of the file):
//   _ikBot              — currently selected bot tab
//   _ikIntData          — merged inventory response
//   _ikMcpData          — MCP scan response
//   _ikPluginsData      — plugin inventory response
//   _ikHooksData        — hooks inventory response
//
// Lookup tables (sets for category gating):
//   _IK_PLUGIN_MESSAGING / _IK_PLUGIN_LLM_PROVIDERS /
//   _IK_PLUGIN_INFRASTRUCTURE / _IK_INFRA_NAMES /
//   _IK_PLUGIN_TO_AUDIT_SKILLS
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), toast(), escHtml(), botLabel() — core/
//   - aiSwitchBot — pages/ai-optimization.js (embedding chain editor
//                   jumps to AI-Optimization with the selected bot)
//   - nav() / window.nav — core/router.js
//   - _gwStatusCache — pages/maintenance.js (Phase 3w; the ik-row
//                      renderer reads gateway status from there)
//   - _renderSubstrateAuditRows + the substrate-audit chip helpers —
//     pages/settings.js (Phase 3u)
//   - _loadSubstrateAuditStatusForAllBots — pages/skills.js (line ~632)
//
// Out of scope (separate clusters, future phases):
//   ikRenderKeys (~line 17194) — credential row renderer; far from
//     this cluster.
//   _ikRenderOnboardBanner (~line 17678) — onboarding flow renderer.
// ════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
// Integrations & Keys (merged page)
// ══════════════════════════════════════════════════════
// The tab strip is "POD" + one tab per bot. POD selects pod-wide
// credentials (GitHub intake PAT, GitHub general-access PAT, etc.) — the
// per-bot subtabs (Plugins / Embeddings / MCP / Hooks / Activity) are
// disabled in POD mode because those config surfaces are per-bot by
// construction. Per-bot tabs work the way they always did.
const IK_POD_SENTINEL = '__pod__';
let _ikBot = null;
let _ikIntData = null;

function _ikIsPod() { return _ikBot === IK_POD_SENTINEL; }

// Snapshot writer for the Plugins page's evo context-pack. Each subtab's
// loader/renderer calls this with the slice it just refreshed; the helper
// folds the patch into ``_evoContextSnapshots['integrations-keys']`` while
// preserving sibling fields written by OTHER subtab loaders. Matches the
// ``..._prev`` clobber-prevention pattern from PR #1366 (security snapshot)
// and PR #1369 (reports snapshot). Without the spread, switching subtabs
// or running a periodic refresh wipes the other subtabs' data.
function _ikWriteContextSnapshot(patch) {
  try {
    window._evoContextSnapshots = window._evoContextSnapshots || {};
    const _prev = window._evoContextSnapshots['integrations-keys'] || {};
    window._evoContextSnapshots['integrations-keys'] = { ..._prev, ...patch };
    if (typeof _evoDrawerUpdateContextChip === 'function') _evoDrawerUpdateContextChip();
  } catch (_) { /* tolerated — context-pack is best-effort */ }
}

async function loadIntegrationsKeys() {
  const bots = orderedBotIds(_networkData.bots);
  if (!bots.length) {
    const stripEl = document.getElementById('ik-bot-header-strip');
    if (stripEl) stripEl.innerHTML = '<div class="empty">No bots configured.</div>';
    return;
  }
  // Default selection: POD tab. Operators landing here typically come to
  // see pod-wide credential health first; per-bot drills are explicit.
  const valid = (b) => b === IK_POD_SENTINEL || bots.includes(b);
  if (!_ikBot || !valid(_ikBot)) _ikBot = IK_POD_SENTINEL;

  // Render tabs: POD pinned at the left, then one per bot.
  const tabsEl = document.getElementById('ik-bot-tabs');
  if (tabsEl) {
    const podActive = _ikBot === IK_POD_SENTINEL ? 'active' : '';
    const podTab = `<div class="bot-tab ${podActive}" data-bot="${IK_POD_SENTINEL}" onclick="ikSwitchBot('${IK_POD_SENTINEL}')" title="Pod-wide credentials">POD</div>`;
    tabsEl.innerHTML = podTab + bots.map(b =>
      `<div class="bot-tab ${b === _ikBot ? 'active' : ''}" data-bot="${escHtml(b)}" onclick="ikSwitchBot('${escHtml(b)}')">${escHtml(botLabel(b))}</div>`
    ).join('');
  }

  // Seed the evo context-pack snapshot with the selected bot + the
  // active subtab (taken from the DOM, since the page may have been
  // restored to a non-default subtab via localStorage). Each subtab's
  // loader will fold its own data into the same snapshot as it lands.
  try {
    const activeSubPage = document.querySelector(
      '#page-integrations-keys .subtab-page.active'
    );
    const activeSubtab = activeSubPage && activeSubPage.id
      ? activeSubPage.id.slice('integrations-keys-'.length)
      : 'plugins';
    _ikWriteContextSnapshot({ bot: _ikBot, active_subtab: activeSubtab });
  } catch (_) {}

  // POD mode: the only meaningful surface is Credentials. Render the pod-
  // wide GitHub block, gray out the per-bot subtabs, hide the bot-runtime
  // strip, and skip the per-bot loaders entirely.
  if (_ikIsPod()) {
    _ikApplyPodMode();
    await ikLoadGithubStatus();
    return;
  }

  _ikApplyPodMode();

  // ikLoadIntegrations populates the header chip strip and the Plugins
  // Messaging section (formerly the Channels tab).
  await Promise.all([
    ikLoadIntegrations(),
    ikRenderKeys(_ikBot),
    ikLoadGithubStatus(),
    ikLoadEmbeddings(),
    ikLoadMcp(),
    ikLoadPlugins(),
    ikLoadHooks(),
    ikLoadActivity(),
    // Audit chips: skills feed per-row pills on the Plugins tab; providers
    // feed per-row chips on the Credentials tab. Best-effort; failure
    // leaves the chip rendering "never audited".
    _loadSubstrateAuditStatusForAllBots('skill'),
    _loadSubstrateAuditStatusForAllBots('provider'),
  ]);
  // Re-render plugins once audit data has settled so the new chip column
  // is hydrated even when ikLoadPlugins finished first.
  if (_ikPluginsData) ikRenderPlugins();
}

// Apply pod-mode visual gating: hide the bot-runtime header strip, force
// the active subtab to Credentials, and visually disable the per-bot
// subtabs. Re-runs on every tab switch so flipping pod→bot rehydrates.
function _ikApplyPodMode() {
  const pod = _ikIsPod();
  const page = document.getElementById('page-integrations-keys');
  if (page) page.classList.toggle('ik-pod-mode', pod);
  const strip = document.getElementById('ik-bot-header-strip');
  if (strip) strip.style.display = pod ? 'none' : '';
  if (pod) {
    // Force Credentials as the active subtab. Find by its onclick payload.
    const tabs = document.querySelectorAll('#page-integrations-keys > .subtabs .subtab');
    let credTab = null;
    tabs.forEach(t => {
      const oc = t.getAttribute('onclick') || '';
      if (oc.indexOf("'credentials'") !== -1) credTab = t;
    });
    if (credTab && !credTab.classList.contains('active')) credTab.click();
  }
}

function ikSwitchBot(botId) {
  _ikBot = botId;
  // Match by data-bot rather than textContent — display names differ
  // from bot ids after a rename, so .textContent === botId would
  // silently fail to toggle the active state.
  document.querySelectorAll('#ik-bot-tabs .bot-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.bot === botId)
  );

  if (_ikIsPod()) {
    _ikApplyPodMode();
    ikLoadGithubStatus();
    return;
  }

  _ikApplyPodMode();
  // MCP / plugins / hooks data are pod-wide cached; re-render against the new bot.
  if (_ikMcpData) ikRenderMcp();
  if (_ikPluginsData) ikRenderPlugins();
  if (_ikHooksData) ikRenderHooks();
  // Re-render the header strip with whatever integration data we already have,
  // then refresh from the API in parallel with the other per-bot loads.
  ikRenderBotHeaderStrip();
  Promise.all([
    ikLoadIntegrations(),
    ikRenderKeys(botId),
    ikLoadGithubStatus(),
    ikLoadEmbeddings(),
    ikLoadActivity(),
  ]);
}

// ── MCP Servers sub-tab (Phase A of spec-mcp-administration) ─────────────────
let _ikMcpData = null;

async function ikLoadMcp(forceRescan) {
  const el = document.getElementById('ik-mcp-content');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    try { await api('POST', '/api/mcp-admin/scan'); } catch (_e) { /* tolerated */ }
  }
  try {
    _ikMcpData = await api('GET', '/api/mcp-admin/inventory');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load inventory: ${escHtml(String(e))}</div>`;
    return;
  }
  ikRenderMcp();
}

function ikRenderMcp() {
  const el = document.getElementById('ik-mcp-content');
  if (!el || !_ikMcpData) return;
  const bot = _ikBot;
  const inv = _ikMcpData.bots ? _ikMcpData.bots[bot] : null;
  if (!inv) {
    el.innerHTML = '<div class="empty">No inventory cached yet. Click ↻ Re-scan to run the MCP monitor now.</div>';
    return;
  }
  if (!inv.openclaw_config_present) {
    el.innerHTML = `<div class="empty">Bot has no openclaw.json (${escHtml(inv.read_error || 'not found')}). Configure the bot first.</div>`;
    return;
  }
  const allowEntries = (_ikMcpData.allowlist && _ikMcpData.allowlist.entries) || [];
  const findAllow = (name) => allowEntries.find(e => e.name === name && (e.bot_id === bot || e.bot_id === '*'));

  const selfMutationBanner = (inv.self_mutation_commands_mcp || inv.self_mutation_commands_plugins)
    ? `<div style="margin-bottom:10px;padding:8px 12px;background:var(--bg2);border-left:3px solid var(--yellow);font-size:0.78rem;color:var(--text2)">
        ⚠ Runtime config mutation enabled —
        ${inv.self_mutation_commands_mcp ? '<code>commands.mcp = true</code>' : ''}
        ${inv.self_mutation_commands_mcp && inv.self_mutation_commands_plugins ? ' &amp; ' : ''}
        ${inv.self_mutation_commands_plugins ? '<code>commands.plugins = true</code>' : ''}.
        This bot can add MCP servers via slash commands, bypassing the proposal pipeline.
      </div>`
    : '';

  if (!inv.servers || inv.servers.length === 0) {
    el.innerHTML = `${selfMutationBanner}
      <div style="padding:14px;color:var(--text2);font-size:0.85rem">
        <div style="margin-bottom:6px">✓ No MCP servers configured.</div>
        <div style="color:var(--text3);font-size:0.78rem">
          Last observed: ${escHtml(inv.observed_at || '—')}.
          Install from catalog lands in Phase B.
        </div>
      </div>`;
    return;
  }

  const rows = inv.servers.map(s => {
    const allow = findAllow(s.name);
    let statusBadge;
    if (!allow) {
      statusBadge = '<span class="badge badge-red">unknown</span>';
    } else if (allow.config_signature && allow.config_signature !== s.config_signature) {
      statusBadge = '<span class="badge badge-yellow">changed</span>';
    } else {
      statusBadge = '<span class="badge badge-green">approved</span>';
    }
    // Health badge from the last_probe field enriched in /api/mcp-admin/inventory
    let healthBadge = '<span style="color:var(--text3)">—</span>';
    let healthTitle = 'No probe history yet';
    if (s.last_probe) {
      const lp = s.last_probe;
      const lat = lp.latency_ms != null ? ` ${lp.latency_ms}ms` : '';
      if (lp.ok) {
        healthBadge = `<span class="badge badge-green" title="Last probe ${escHtml(lp.probed_at || '')}${escHtml(lat)}">healthy</span>`;
      } else if (lp.error_class === 'credential_invalid') {
        healthBadge = `<span class="badge badge-red" title="${escHtml(lp.error_detail || lp.error_class)}">cred</span>`;
      } else {
        healthBadge = `<span class="badge badge-yellow" title="${escHtml(lp.error_class || '')}: ${escHtml(lp.error_detail || '')}">${escHtml(lp.error_class || 'fail')}</span>`;
      }
      healthTitle = `${lp.probed_at || ''} — ${lp.error_class || (lp.ok ? 'ok' : 'fail')}`;
    }
    // CVE badge from the advisory_count field (Phase D)
    let cveBadge = '<span style="color:var(--text3)">—</span>';
    if (s.advisory_count > 0) {
      cveBadge = `<span class="badge badge-red" title="${escHtml(s.advisory_count)} open advisory(ies) for ${escHtml(s.package_name || '')}">${s.advisory_count} CVE</span>`;
    } else if (s.package_name) {
      cveBadge = '<span class="badge badge-green" title="No advisories for this catalog package">clean</span>';
    }
    const envChips = (s.env_keys || []).map(k =>
      `<code style="font-size:0.7rem;background:var(--bg3);padding:1px 5px;border-radius:3px;margin-right:4px">${escHtml(k)}</code>`
    ).join('') || '<span style="color:var(--text3)">—</span>';
    const target = s.command || s.url || '';
    const argsStr = (s.args && s.args.length) ? (' ' + s.args.join(' ')) : '';
    const cveAction = s.advisory_count > 0
      ? `<button class="btn btn-ghost btn-sm" onclick="openMcpAdvisoryModal('${escHtml(s.package_name || '')}','${escHtml(s.name)}')" title="View open advisories for this server">CVEs…</button>`
      : '';
    return `<tr>
      <td><strong>${escHtml(s.name)}</strong></td>
      <td><span class="badge badge-muted">${escHtml(s.kind)}</span></td>
      <td style="font-family:monospace;font-size:0.75rem;color:var(--text2);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(target + argsStr)}">${escHtml(target + argsStr)}</td>
      <td>${envChips}</td>
      <td>${statusBadge}</td>
      <td title="${escHtml(healthTitle)}">${healthBadge}</td>
      <td>${cveBadge}</td>
      <td data-label="" style="display:flex;gap:4px;flex-wrap:wrap">
        <button class="btn btn-ghost btn-sm" onclick="openMcpHealthModal('${escHtml(bot)}','${escHtml(s.name)}')" title="View recent probe history">Probes</button>
        ${cveAction}
        <button class="btn btn-ghost btn-sm" onclick="proposeMcpRemove('${escHtml(bot)}','${escHtml(s.name)}')" title="Remove this server">Remove…</button>
      </td>
    </tr>`;
  }).join('');
  el.innerHTML = `${selfMutationBanner}
    <div class="resp-table-wrap"><table class="resp-table data-table" style="width:100%">
      <thead><tr><th>Server</th><th>Transport</th><th>Target</th><th>Env keys</th><th>Allow</th><th>Health</th><th>CVE</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div style="margin-top:8px;font-size:0.72rem;color:var(--text3)">
      Last observed: ${escHtml(inv.observed_at || '—')}.
      Signatures hash launch shape (command + args + url + env <em>key names</em>) — credential rotation doesn't trip drift.
    </div>`;
  _respTableLabelize(el);

  // Snapshot for the evo context-pack. Each server's name + transport
  // + health + CVE count gives evo enough framing to answer "is the
  // github MCP healthy" / "what MCP servers are configured here?" /
  // "any servers with open advisories?" — without a fresh fetch.
  try {
    _ikWriteContextSnapshot({
      bot: bot,
      mcp: {
        server_count: inv.servers.length,
        unhealthy_count: inv.servers.filter(s => s.last_probe && !s.last_probe.ok).length,
        cve_count: inv.servers.reduce((n, s) => n + (s.advisory_count || 0), 0),
        servers: inv.servers.slice(0, 12).map(s => ({
          name: s.name,
          kind: s.kind,
          healthy: s.last_probe ? !!s.last_probe.ok : null,
          health_error: (s.last_probe && !s.last_probe.ok) ? (s.last_probe.error_class || 'fail') : null,
          advisory_count: s.advisory_count || 0,
          allow_status: (() => {
            const al = findAllow(s.name);
            if (!al) return 'unknown';
            if (al.config_signature && al.config_signature !== s.config_signature) return 'changed';
            return 'approved';
          })(),
        })),
        observed_at: inv.observed_at || null,
        commands_mcp_open: !!inv.self_mutation_commands_mcp,
      },
    });
  } catch (_) {}
}

// ── Plugins sub-tab (Phase A of spec-plugin-inventory) ───────────────────────
let _ikPluginsData = null;

async function ikLoadPlugins(forceRescan) {
  const el = document.getElementById('ik-plugins-content');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    try { await api('POST', '/api/plugins-admin/scan'); } catch (_e) { /* tolerated */ }
  }
  try {
    _ikPluginsData = await api('GET', '/api/plugins-admin/inventory');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load plugin inventory: ${escHtml(String(e))}</div>`;
    return;
  }
  ikRenderPlugins();
}

// ── Plugin categorization (Messaging / LLM Providers / Tools / Infrastructure) ─
// Plugin IDs that act as a messaging surface — the bot's reachable-via
// channels. Source of truth for messaging-vs-tool classification; reused
// below by ikIntegrationMessagingRows() so the integrations heal scan and
// the Plugins tab agree on what counts as messaging.
//
// brave is *required* pod-wide in plugins/bootstrap.py but stays in the
// "tools" bucket here (web search is a functional capability, not platform
// infrastructure); the category drives UI grouping, not the baseline role.
const _IK_PLUGIN_MESSAGING = new Set([
  'telegram', 'slack', 'whatsapp', 'discord', 'signal', 'matrix', 'email', 'sms',
]);
// LLM provider adapter plugins. Each maps 1:1 to a credential row.
const _IK_PLUGIN_LLM_PROVIDERS = new Set([
  'anthropic', 'openai', 'google', 'gemini', 'xai', 'voyage', 'mistral',
  'copilot', 'github-copilot', 'perplexity',
]);
// Infrastructure plugins — required by the platform, not user-installable.
const _IK_PLUGIN_INFRASTRUCTURE = new Set(['evolve', 'oc_plugin']);

// Plugin-name → audit skill_id mapping. Plugin entries are keyed by their
// openclaw name (`slack`, `google`, ...); the substrate auditor enumerates
// from analyzer/substrate_audit_state.KNOWN_SKILLS which uses skill_ids
// like `gmail`, `calendar`, `obsidian`. This map bridges the two so the
// per-row chip on the Plugins page renders the same audit data the Skills
// catalog used to point at, without changing the API surface.
//
// Plugins not present here have no audit data (LLM-provider plugins,
// platform infrastructure, channel-only entries) and we render no chip.
// For multi-skill plugins (google → gmail + calendar) we render the
// gmail chip as the primary — calendar shares the same google_workspace
// OAuth provider so the credential-health story is the same; an operator
// who wants the calendar-specific trail clicks through to the trail
// viewer which carries the element_id.
const _IK_PLUGIN_TO_AUDIT_SKILLS = {
  // Messaging — 1:1 with audit skill_ids
  slack:          ['slack'],
  telegram:       ['telegram'],
  discord:        ['discord'],
  imessage:       ['imessage'],
  // whatsapp / signal / matrix / sms / email — no audit module yet
  // Tools — 1:1 mostly; obsidian plugin maps to obsidian audit
  obsidian:       ['obsidian'],
  obsidian_vault: ['obsidian'],
  notion:         ['notion'],
  linear:         ['linear'],
  home_assistant: ['home_assistant'],
  autocad:        ['autocad'],
  runway:         ['runway'],
  apple_local:    ['apple_local'],
  // Google plugin spans gmail + calendar — show gmail as primary
  google:         ['gmail', 'calendar'],
  // Upstream-OC plugins are a single audit bucket today
  brave:          ['upstream_plugin_skills'],
  github:         ['upstream_plugin_skills'],
  dropbox:        ['upstream_plugin_skills'],
  gdrive:         ['upstream_plugin_skills'],
};

function _ikAuditSkillsForPlugin(pluginName) {
  return _IK_PLUGIN_TO_AUDIT_SKILLS[(pluginName || '').toLowerCase()] || null;
}

function _ikCategorizePlugin(name) {
  const n = (name || '').toLowerCase();
  if (_IK_PLUGIN_INFRASTRUCTURE.has(n)) return 'infrastructure';
  if (_IK_PLUGIN_MESSAGING.has(n)) return 'messaging';
  if (_IK_PLUGIN_LLM_PROVIDERS.has(n)) return 'llm';
  return 'tools';
}

function _ikRoleTooltip() {
  return 'required = pod-wide invariant in the baseline (every bot must have it). '
    + 'denied = explicitly denied in the baseline; flagged red if enabled. '
    + 'A blank Role cell means the plugin is enabled at the bot\'s discretion '
    + '— plugin posture is per-bot, not baseline-curated.';
}

function _ikRoleBadge(name, required, denied) {
  if (denied.has(name)) return '<span class="badge badge-red">denied</span>';
  if (required.has(name)) return '<span class="badge badge-muted">required</span>';
  return '<span style="color:var(--text3)">—</span>';
}

// Plugin name display: title-case the openclaw id for the column cell, to
// match the Credentials table's "Anthropic"/"Brave"/"Telegram" style. The
// underlying id stays lowercase — only the rendered text is capitalized.
function _ikDisplayPluginName(name) {
  if (!name) return '';
  // Hyphenated/multi-segment ids get each segment capitalized:
  // github-copilot → Github-Copilot.
  return String(name).split(/([-_])/).map((part, i) =>
    (i % 2 === 0 && part) ? part.charAt(0).toUpperCase() + part.slice(1) : part
  ).join('');
}

// Status column rendering. Switched from .badge spans to plain colored text
// with an emoji prefix (mirroring the Credentials table's "✅ Active" style).
// Two reasons:
//   1. Visual consistency with the Credentials page on the adjacent sub-tab.
//   2. The .badge class has internal padding that shifts content right of the
//      column header — plain text aligns flush with the header text.
function _ikStatusText(e, required, denied) {
  if (e.enabled) {
    if (denied.has(e.name)) {
      return '<span style="color:var(--red)">❌ Enabled (denied!)</span>';
    }
    return '<span style="color:var(--green)">✅ Enabled</span>';
  }
  if (required.has(e.name)) {
    return '<span style="color:var(--yellow)">⚠️ Disabled (required on)</span>';
  }
  return '<span style="color:var(--text3)">— Disabled</span>';
}

// Render the per-plugin provenance cell — what OC's install record says about
// where this plugin came from. Inventory carries source / install_spec /
// resolved_name / resolved_version / clawhub_channel / clawhub_family per
// docs/spec-plugin-posture-rework-2026-06-06.md §1.4. Display priority:
//
//   1. resolved_name + version when present (the npm spec; most informative)
//   2. install_spec / sourcePath as fallback (raw spec for non-npm installs)
//   3. source alone when nothing else is known
//   4. "—" when no install record exists (older bots or built-in plugins)
//
// clawhub_channel renders as a small chip next to the spec when set:
// 'official' is green, 'community' / 'private' are yellow. No alerts attached
// — this is advisory display only.
function _ikProvenanceCellHtml(e) {
  if (!e.install_source && !e.install_spec && !e.resolved_name) {
    return '<span style="color:var(--text3)">—</span>';
  }
  const parts = [];
  if (e.resolved_name) {
    const ver = e.resolved_version ? `@${e.resolved_version}` : '';
    parts.push(`<code style="font-size:0.72rem">${escHtml(e.resolved_name + ver)}</code>`);
  } else if (e.install_spec) {
    parts.push(`<code style="font-size:0.72rem">${escHtml(e.install_spec)}</code>`);
  } else if (e.install_source) {
    parts.push(`<code style="font-size:0.72rem">${escHtml(e.install_source)}</code>`);
  }
  if (e.clawhub_channel) {
    const colour = e.clawhub_channel === 'official' ? 'badge-green' : 'badge-yellow';
    parts.push(`<span class="badge ${colour}" style="margin-left:4px" title="ClawHub channel">${escHtml(e.clawhub_channel)}</span>`);
  } else if (e.install_source && e.install_source !== 'path' && !e.resolved_name) {
    // npm without a resolved name = raw spec we don't fully understand;
    // surface the source so the operator can see it at a glance.
    parts.push(`<span class="badge badge-muted" style="margin-left:4px">${escHtml(e.install_source)}</span>`);
  }
  return parts.join('');
}

function _ikPluginRowHtml(bot, e, sets, extraStatus) {
  const {required, denied} = sets;
  const roleBadge = _ikRoleBadge(e.name, required, denied);
  const statusText = _ikStatusText(e, required, denied);
  // Optional extra status fragment (e.g. messaging-row connection state)
  // shows inline next to the primary Enabled/Disabled state, separated by
  // a middot — keeps the row to a single line.
  const status = extraStatus
    ? `${statusText} <span style="color:var(--text3);margin:0 4px">·</span> ${extraStatus}`
    : statusText;
  const policyChips = [
    e.has_hooks_policy ? '<code style="font-size:0.7rem;background:var(--bg3);padding:1px 5px;border-radius:3px;margin-right:4px">hooks</code>' : '',
    e.has_subagent_policy ? '<code style="font-size:0.7rem;background:var(--bg3);padding:1px 5px;border-radius:3px;margin-right:4px">subagent</code>' : '',
  ].filter(Boolean).join('') || '<span style="color:var(--text3)">—</span>';
  const source = _ikProvenanceCellHtml(e);
  const action = e.enabled
    ? `<button class="btn btn-ghost btn-sm" onclick="proposePluginDisable('${escHtml(bot)}','${escHtml(e.name)}')" title="Disable this plugin">Disable…</button>`
    : `<button class="btn btn-ghost btn-sm" onclick="proposePluginEnable('${escHtml(bot)}','${escHtml(e.name)}')" title="Enable this plugin">Enable…</button>`;
  const editConfig = `<button class="btn btn-ghost btn-sm" onclick="openPluginConfigModal('${escHtml(bot)}','${escHtml(e.name)}')" title="Edit this plugin's config block via proposal">Config…</button>`;
  // Cross-link: LLM-provider plugins link to the Credentials tab for key
  // rotation rather than duplicating the rotate flow here.
  const credentialLink = _IK_PLUGIN_LLM_PROVIDERS.has(e.name.toLowerCase())
    ? `<button class="btn btn-ghost btn-sm" onclick="ikJumpToCredentials('${escHtml(e.name)}')" title="Manage this provider's API key in the Credentials tab">Credentials →</button>`
    : '';
  // Audit chip + Run audit + cadence — only for plugins that map to a
  // substrate audit skill. Per-bot management surface (was previously on
  // Skills → Browse, moved here in fix(ui): catalog-tab regression).
  const auditCell = _ikPluginAuditCellHtml(bot, e.name);
  const auditActions = auditCell ? _ikPluginAuditActionsHtml(bot, e.name) : '';
  return `<tr>
    <td><strong>${escHtml(_ikDisplayPluginName(e.name))}</strong></td>
    <td>${status}</td>
    <td>${auditCell}</td>
    <td>${roleBadge}</td>
    <td>${policyChips}</td>
    <td>${source}</td>
    <td style="display:flex;gap:4px;flex-wrap:wrap">${action}${editConfig}${credentialLink}${auditActions}</td>
  </tr>`;
}

// Audit pill for a single plugin row. Returns '' when the plugin has no
// audit module so the cell stays empty — no chip is better than a noisy
// "n/a" badge on rows where audit doesn't apply (LLM providers, evolve,
// oc_plugin, whatsapp, etc).
function _ikPluginAuditCellHtml(botId, pluginName) {
  const skills = _ikAuditSkillsForPlugin(pluginName);
  if (!skills || !skills.length) return '<span style="color:var(--text3)">—</span>';
  // Show the pill for the first (primary) audit skill. For multi-skill
  // plugins (google → gmail + calendar), the click target opens the
  // primary's trail; operators wanting the secondary's trail use the
  // chevron menu (future) or run an audit which surfaces a per-skill
  // proposal.
  const primary = skills[0];
  const pill = _substrateAuditPill('skill', botId, primary);
  const tip = skills.length > 1
    ? ` <span style="color:var(--text3);font-size:0.7rem" title="Plugin spans ${skills.join(' + ')} — click pill to open ${primary} audit trail">+${skills.length - 1}</span>`
    : '';
  return `<a href="#" onclick="_openSubstrateTrail('skill', '${escHtml(botId)}', '${escHtml(primary)}'); return false" style="text-decoration:none">${pill}</a>${tip}`;
}

// Run-audit + cadence dropdown for a single plugin row.
function _ikPluginAuditActionsHtml(botId, pluginName) {
  const skills = _ikAuditSkillsForPlugin(pluginName);
  if (!skills || !skills.length) return '';
  const primary = skills[0];
  const cadenceOpts = [
    { v: '',          l: 'inherit pod default' },
    { v: 'never',     l: 'never (manual only)' },
    { v: 'daily',     l: 'daily' },
    { v: 'weekly',    l: 'weekly' },
    { v: 'monthly',   l: 'monthly' },
    { v: 'quarterly', l: 'quarterly' },
  ];
  const cad = _substrateAuditCadence.skill?.[botId] || {};
  const sel = cad.bot_override || '';
  const optsHtml = cadenceOpts.map(o =>
    `<option value="${escHtml(o.v)}" ${sel === o.v ? 'selected' : ''}>${escHtml(o.l)}</option>`
  ).join('');
  return `<button class="btn btn-ghost btn-sm" title="Run an audit on this skill now"
    onclick="_runSubstrateAuditNow('skill', '${escHtml(botId)}', '${escHtml(primary)}')">Run audit now</button>
    <select class="form-select input-w-auto" style="font-size:0.72rem;padding:2px 4px"
            title="Audit cadence for skills on this bot (shared across every skill)"
            onchange="_setSubstrateAuditCadence('skill', '${escHtml(botId)}', this.value)">${optsHtml}</select>`;
}

function _ikSectionTable(title, subtitle, rowsHtml, headerExtra) {
  if (!rowsHtml) return '';
  return `
    <div class="section-head" style="margin-top:16px;font-size:0.85rem;text-transform:uppercase;letter-spacing:0.06em;color:var(--text2)">
      ${escHtml(title)}
      ${headerExtra || ''}
    </div>
    ${subtitle ? `<div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">${subtitle}</div>` : ''}
    <div class="resp-table-wrap"><table class="resp-table data-table" style="width:100%">
      <thead><tr>
        <th>Plugin</th>
        <th>Status</th>
        <th title="Latest skill-audit result for this plugin on this bot. Click to open the audit trail.">Audit</th>
        <th title="${_ikRoleTooltip()}">Role <span style="color:var(--text3);font-size:0.7rem;cursor:help">ⓘ</span></th>
        <th>Policies</th>
        <th>Source</th>
        <th></th>
      </tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table></div>
  `;
}

// Synthetic row for a messaging surface that lives in openclaw.json's
// `channels` block but has no corresponding entry in `plugins.entries`.
// OpenClaw engages the channel-handler plugin implicitly when the channel
// block is configured — so the surface IS active even though the Plugin
// Inventory (which reads plugins.entries only) doesn't see it. This row
// preserves the Plugins-tab framing while telling the operator where the
// config actually lives (e.g., personal_bot has channels.telegram but no
// plugins.entries.telegram, and the bot still receives Telegram messages
// just fine).
function _ikChannelOnlyRowHtml(bot, channelId, intRow) {
  const display = _ikDisplayPluginName(channelId);
  const dp = `data-provider="${escHtml(channelId.toLowerCase())}"`;
  const isDisabled = intRow && intRow.auth_state === 'disabled';
  const detailText = intRow && intRow.details && intRow.details !== 'configured'
    ? ` <span style="color:var(--text3)">(${escHtml(intRow.details)})</span>` : '';
  const connBadge = intRow && intRow.status
    ? `${_ikInlineConnBadge(intRow.status)}${detailText}`
    : '';
  const configuredLabel = isDisabled
    ? `<span style="color:var(--text3)">⊘ Disabled</span>`
    : `<span style="color:var(--green)">✅ Configured</span>`;
  const status = connBadge
    ? `${configuredLabel} <span style="color:var(--text3);margin:0 4px">·</span> ${connBadge}`
    : configuredLabel;
  const auditCell = _ikPluginAuditCellHtml(bot, channelId);
  const auditActions = auditCell ? _ikPluginAuditActionsHtml(bot, channelId) : '';
  return `<tr ${dp}>
    <td><strong>${escHtml(display)}</strong></td>
    <td>${status}</td>
    <td>${auditCell}</td>
    <td><span class="badge badge-muted" title="No plugins.entries record; channel-block config drives this surface">channel-block</span></td>
    <td><span style="color:var(--text3)">—</span></td>
    <td><code style="font-size:0.72rem">channels.${escHtml(channelId)}</code></td>
    <td style="display:flex;gap:4px;flex-wrap:wrap">
      <button class="btn btn-ghost btn-sm" onclick="ikJumpToCredentials('${escHtml(channelId)}')" title="Manage this channel's token in the Credentials tab">Credentials →</button>
      ${auditActions}
    </td>
  </tr>`;
}

// Build the Messaging section. Sources from two places:
//   1. plugin inventory (plugins.entries with messaging-category plugins)
//   2. /api/integrations channels-block rows for plugins NOT in the
//      inventory — implicit-enable shape that OpenClaw supports and the
//      Plugin Inventory module can't see on its own (it reads
//      plugins.entries only).
// Each plugin-inventory row is augmented with its heal-probe connection
// state inline so config + runtime show on one line.
function _ikMessagingSectionHtml(bot, msgEntries, sets) {
  // Pull in the integration row (heal probe state) per plugin if available.
  const intRows = ikIntegrationMessagingRows(bot) || [];
  const intByName = new Map(intRows.map(r => {
    const id = _ikRowMessagingId(r);
    return id ? [id, r] : [r.name, r];
  }));
  const inventoryIds = new Set((msgEntries || []).map(e => e.name.toLowerCase()));

  // For each messaging plugin, render the standard plugin row with the heal-
  // scan connection state appended inline in the Status cell.
  const pluginRows = (msgEntries || []).map(e => {
    const intRow = intByName.get(e.name.toLowerCase());
    if (!intRow || !intRow.status) return _ikPluginRowHtml(bot, e, sets);
    const detailText = intRow.details && intRow.details !== 'configured'
      ? ` <span style="color:var(--text3)">(${escHtml(intRow.details)})</span>` : '';
    const extra = `${_ikInlineConnBadge(intRow.status)}${detailText}`;
    return _ikPluginRowHtml(bot, e, sets, extra);
  });

  // Synthesize rows for channel-block configurations that don't have a
  // matching plugin-entry. The bot is reachable on these surfaces even
  // though the plugin inventory doesn't list them.
  const channelOnlyRows = [];
  for (const [id, intRow] of intByName.entries()) {
    if (!id || inventoryIds.has(id)) continue;
    channelOnlyRows.push(_ikChannelOnlyRowHtml(bot, id, intRow));
  }

  if (pluginRows.length === 0 && channelOnlyRows.length === 0) {
    return `
      <div class="section-head" style="margin-top:0;font-size:0.85rem;text-transform:uppercase;letter-spacing:0.06em;color:var(--text2)">Messaging</div>
      <div style="font-size:0.78rem;color:var(--text3);padding:6px 0">No messaging plugins enabled. Use the Catalog button above to install slack / telegram / discord / whatsapp.</div>
    `;
  }

  return _ikSectionTable(
    'Messaging',
    'How humans reach this bot. Slack, Telegram, WhatsApp, Discord configurations live here.',
    pluginRows.concat(channelOnlyRows).join('')
  );
}

// Compact one-shot connection-state span — used inline inside the Status cell
// next to "✅ Enabled". Mirrors the Credentials table's plain-text-with-color
// approach so vertical rhythm and column alignment match across sub-tabs.
function _ikInlineConnBadge(status) {
  const map = {
    ok: ['var(--green)', '✅ Connected'],
    warning: ['var(--yellow)', '⚠️ Degraded'],
    error: ['var(--red)', '❌ Error'],
    unknown: ['var(--text3)', '— Unknown'],
  };
  const [color, label] = map[status] || map.unknown;
  return `<span style="color:${color}">${label}</span>`;
}

function ikRenderPlugins() {
  const el = document.getElementById('ik-plugins-content');
  if (!el || !_ikPluginsData) return;
  const bot = _ikBot;
  const inv = _ikPluginsData.bots ? _ikPluginsData.bots[bot] : null;
  const resolved = (_ikPluginsData.resolved || {})[bot];
  if (!inv) {
    el.innerHTML = '<div class="empty">No plugin inventory cached yet. Click ↻ Re-scan to run the monitor now.</div>';
    return;
  }
  if (!inv.openclaw_config_present) {
    el.innerHTML = `<div class="empty">Bot has no openclaw.json (${escHtml(inv.read_error || 'not found')}).</div>`;
    return;
  }
  const entries = inv.entries || [];
  const enabled = new Set(entries.filter(e => e.enabled).map(e => e.name));
  const required = new Set((resolved && resolved.required) || []);
  const denied = new Set((resolved && resolved.denied) || []);
  const sets = {required, denied};

  // Banner: command gate
  const cmdGateBanner = inv.self_mutation_commands_plugins
    ? `<div style="margin-bottom:10px;padding:8px 12px;background:var(--bg2);border-left:3px solid var(--yellow);font-size:0.78rem;color:var(--text2)">
        ⚠ <code>commands.plugins = true</code> — the bot can enable/disable plugins via <code>/plugins</code> at runtime, bypassing the proposal pipeline.
      </div>`
    : '';

  // Group entries by category, sorted within each section the same way the
  // original flat table sorted: required first, then enabled, then alpha.
  const sortFn = (a, b) => {
    const aReq = required.has(a.name) ? 0 : 1;
    const bReq = required.has(b.name) ? 0 : 1;
    if (aReq !== bReq) return aReq - bReq;
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
    return a.name.localeCompare(b.name);
  };
  const byCat = {messaging: [], llm: [], tools: [], infrastructure: []};
  entries.slice().sort(sortFn).forEach(e => {
    const cat = _ikCategorizePlugin(e.name);
    byCat[cat].push(e);
  });

  const messagingHtml = _ikMessagingSectionHtml(bot, byCat.messaging, sets);
  const llmRows = byCat.llm.map(e => _ikPluginRowHtml(bot, e, sets)).join('');
  const toolRows = byCat.tools.map(e => _ikPluginRowHtml(bot, e, sets)).join('');
  const infraRows = byCat.infrastructure.map(e => _ikPluginRowHtml(bot, e, sets)).join('');

  const llmHtml = _ikSectionTable('LLM Providers',
    'Provider-adapter plugins. API keys are managed in the Credentials tab.',
    llmRows);
  const toolsHtml = _ikSectionTable('Tools & Capabilities',
    'Functional plugins — search, memory, app-specific tools.',
    toolRows);
  const infraHtml = _ikSectionTable('Infrastructure',
    'Platform plugins. Always required; managed by deploy.py.',
    infraRows);

  // Missing-required list (entries that DON'T appear in any section).
  // missing-expected was retired with the v1 per-bot expected sets.
  const missingRequired = [...required].filter(n => !enabled.has(n));
  let missingBlock = '';
  if (missingRequired.length) {
    missingBlock = `<div style="margin-top:10px;padding:10px 12px;background:var(--bg2);border-radius:6px;font-size:0.78rem">
      <div><span class="badge badge-red">missing required</span> ${missingRequired.map(n => `<code>${escHtml(n)}</code>`).join(', ')}</div>
    </div>`;
  }

  // Allow / deny list + load paths panel
  const allowDisplay = inv.allow_list === null
    ? '<span style="color:var(--yellow)">absent</span>'
    : (inv.allow_list.length === 0 ? '<span style="color:var(--text3)">empty</span>' : inv.allow_list.map(n => `<code>${escHtml(n)}</code>`).join(', '));
  const denyDisplay = inv.deny_list === null
    ? '<span style="color:var(--text3)">absent</span>'
    : (inv.deny_list.length === 0 ? '<span style="color:var(--text3)">empty</span>' : inv.deny_list.map(n => `<code>${escHtml(n)}</code>`).join(', '));
  const loadPaths = (inv.load_paths || []).map(p => `<code>${escHtml(p)}</code>`).join(', ') || '<span style="color:var(--text3)">—</span>';

  // Snapshot the rendered plugin state for the evo context-pack. We
  // surface the active bot, counts (enabled/disabled/required/missing),
  // and a compact list of plugin names by status so evo can answer
  // "what's drifted?" / "are any required plugins missing?" without a
  // re-fetch. Bounded to keep the context payload small.
  try {
    _ikWriteContextSnapshot({
      bot: bot,
      plugins: {
        enabled_count: [...enabled].length,
        disabled_count: entries.length - [...enabled].length,
        required_count: required.size,
        missing_required: missingRequired.slice(0, 12),
        denied_present: entries.filter(e => denied.has(e.name)).map(e => e.name).slice(0, 8),
        commands_plugins_open: !!inv.self_mutation_commands_plugins,
        allow_list_absent: inv.allow_list === null,
        observed_at: inv.observed_at || null,
      },
    });
  } catch (_) {}

  // Section order matches the Credentials sub-tab on purpose — LLM providers
  // first because they're the most-touched (key rotation), then messaging
  // (the "reachable?" answer), then tools, then platform infrastructure.
  el.innerHTML = `${cmdGateBanner}
    ${llmHtml}
    ${messagingHtml}
    ${toolsHtml}
    ${infraHtml}
    ${missingBlock}
    <div style="margin-top:16px;display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;font-size:0.78rem">
      <div style="color:var(--text3)">Allow list:</div><div>${allowDisplay}</div>
      <div style="color:var(--text3)">Deny list:</div><div>${denyDisplay}</div>
      <div style="color:var(--text3)">Load paths:</div><div>${loadPaths}</div>
    </div>
    <div style="margin-top:8px;font-size:0.72rem;color:var(--text3)">
      Last observed: ${escHtml(inv.observed_at || '—')}.
      Baseline bootstrapped at ${escHtml((_ikPluginsData.baseline || {}).bootstrapped_at || '—')}.
      Roles: <span class="badge badge-muted">required</span> pod-wide invariant ·
      <span class="badge badge-red">denied</span> blocklisted ·
      blank Role = enabled at this bot's discretion (plugin posture is per-bot in v2).
    </div>`;
  _respTableLabelize(el);
}

// ── Hook proposal-creating actions (Phase B of spec-hook-governance) ────────
let _hookPolicyTarget = null;  // {bot, plugin, prior_conv, prior_inj}

function openHookPolicyModal(botId, pluginName, priorConv, priorInj) {
  _hookPolicyTarget = {bot: botId, plugin: pluginName, prior_conv: priorConv, prior_inj: priorInj};
  document.getElementById('hook-policy-title').textContent = `Hook policy — ${botLabel(botId)} / ${pluginName}`;
  document.getElementById('hook-policy-conv').checked = !!priorConv;
  document.getElementById('hook-policy-inj').checked = !!priorInj;
  document.getElementById('hook-policy-error').style.display = 'none';
  document.getElementById('hook-policy-modal').classList.add('open');
}

function closeHookPolicyModal() {
  document.getElementById('hook-policy-modal').classList.remove('open');
  _hookPolicyTarget = null;
}

async function submitHookPolicy() {
  if (!_hookPolicyTarget) return;
  const errEl = document.getElementById('hook-policy-error');
  errEl.style.display = 'none';
  const conv = document.getElementById('hook-policy-conv').checked;
  const inj = document.getElementById('hook-policy-inj').checked;
  // Send only the flags that changed so the action's None-means-leave-alone semantics
  // are preserved on the server side.
  const body = {bot_id: _hookPolicyTarget.bot, plugin_name: _hookPolicyTarget.plugin};
  if (conv !== !!_hookPolicyTarget.prior_conv) body.allow_conversation_access = conv;
  if (inj !== !!_hookPolicyTarget.prior_inj) body.allow_prompt_injection = inj;
  if (body.allow_conversation_access === undefined && body.allow_prompt_injection === undefined) {
    errEl.textContent = 'No change vs. current values.';
    errEl.style.display = 'block';
    return;
  }
  try {
    const resp = await api('POST', '/api/hooks-admin/propose-plugin-policy', body);
    if (!resp.ok) { errEl.textContent = resp.error || 'Proposal failed'; errEl.style.display = 'block'; return; }
    closeHookPolicyModal();
    const verb = `Hook policy updated for '${_hookPolicyTarget.plugin}' on ${_hookPolicyTarget.bot}`;
    if (handleOperatorProposalResult(verb, resp)) ikLoadHooks(true);
  } catch (e) { errEl.textContent = String(e); errEl.style.display = 'block'; }
}

async function proposeDisableIngress(botId) {
  if (!await confirmModal({body: `Disable webhook ingress on ${botLabel(botId)}?\n\nThe hooks block stays in openclaw.json (audit trail) but hooks.enabled flips to false.`, danger: true})) return;
  try {
    const resp = await api('POST', '/api/hooks-admin/propose-disable-ingress', {bot_id: botId});
    if (handleOperatorProposalResult(`Webhook ingress disabled on ${botLabel(botId)}`, resp)) {
      ikLoadHooks(true);
    }
  } catch (e) { toast(`Disable failed: ${e}`, 'err'); }
}

const _HOOK_BASELINE_HINTS = {
  set_trusted_mutators: 'Fields: {"plugins": ["evolve", "experimental"]} — replaces the trusted_prompt_mutators list.',
  set_plugin_policy: 'Fields: {"plugin_name": "evolve", "allow_conversation_access": true, "allow_prompt_injection": false, "rationale": "..."} — replaces the expectation for this plugin.',
  set_webhook_ingress: 'Fields: {"enabled": false, "rationale": "..."} — replaces the pod-wide ingress expectation.',
};

const _HOOK_BASELINE_TEMPLATES = {
  set_trusted_mutators: '{\n  "plugins": ["evolve"]\n}',
  set_plugin_policy: '{\n  "plugin_name": "evolve",\n  "allow_conversation_access": true,\n  "allow_prompt_injection": false,\n  "rationale": ""\n}',
  set_webhook_ingress: '{\n  "enabled": false,\n  "rationale": ""\n}',
};

function openHookBaselineModal() {
  document.getElementById('hook-baseline-op').value = 'set_trusted_mutators';
  document.getElementById('hook-baseline-fields').value = _HOOK_BASELINE_TEMPLATES.set_trusted_mutators;
  document.getElementById('hook-baseline-hint').textContent = _HOOK_BASELINE_HINTS.set_trusted_mutators;
  document.getElementById('hook-baseline-error').style.display = 'none';
  document.getElementById('hook-baseline-modal').classList.add('open');
}

function closeHookBaselineModal() {
  document.getElementById('hook-baseline-modal').classList.remove('open');
}

function hookBaselineOpChanged() {
  const op = document.getElementById('hook-baseline-op').value;
  document.getElementById('hook-baseline-fields').value = _HOOK_BASELINE_TEMPLATES[op] || '{}';
  document.getElementById('hook-baseline-hint').textContent = _HOOK_BASELINE_HINTS[op] || '';
}

async function submitHookBaseline() {
  const errEl = document.getElementById('hook-baseline-error');
  errEl.style.display = 'none';
  const op = document.getElementById('hook-baseline-op').value;
  let fields;
  try {
    fields = JSON.parse(document.getElementById('hook-baseline-fields').value);
  } catch (e) {
    errEl.textContent = `Fields must be valid JSON: ${e.message || e}`;
    errEl.style.display = 'block';
    return;
  }
  if (typeof fields !== 'object' || Array.isArray(fields) || fields === null) {
    errEl.textContent = 'Fields must be a JSON object.';
    errEl.style.display = 'block';
    return;
  }
  try {
    const resp = await api('POST', '/api/hooks-admin/propose-baseline-update', {operation: op, fields});
    if (!resp.ok) { errEl.textContent = resp.error || 'Proposal failed'; errEl.style.display = 'block'; return; }
    closeHookBaselineModal();
    if (handleOperatorProposalResult(`Hook baseline updated (${op})`, resp)) ikLoadHooks(true);
  } catch (e) { errEl.textContent = String(e); errEl.style.display = 'block'; }
}

// ── Activity sub-tab — per-bot operator-UI change log ───────────────────────
// Surfaces the audit trail of operator-clicked config changes. Reads
// archived proposals where generator_id=operator_ui and the bot matches
// the currently-selected bot. No mutation — read-only history.

async function ikLoadActivity(force) {
  const el = document.getElementById('ik-activity-content');
  if (!el || !_ikBot) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  const params = new URLSearchParams({
    bot_id: _ikBot,
    generator_id: 'operator_ui',
    // include applied/ in case a future operator-UI proposal stays applied
    // pending verify; archived/ is where succeeded + failed_flagged land.
    include: 'applied,archived',
    limit: '20',
  });
  let data;
  try {
    data = await api('GET', `/api/arbiter/proposals?${params.toString()}`);
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load activity: ${escHtml(String(e))}</div>`;
    return;
  }
  if (!data || !data.ok) {
    el.innerHTML = `<div class="empty">${escHtml((data && data.error) || 'No data')}</div>`;
    return;
  }
  const proposals = data.proposals || [];
  if (!proposals.length) {
    el.innerHTML = `<div class="empty" style="color:var(--text3)">No operator-UI changes on ${escHtml(_ikBot)} yet. Anything you Enable / Install / Configure on the other tabs will land here as an audit-log entry.</div>`;
    return;
  }

  // Action-kind → short human-readable label. Falls through to the raw
  // kind name for anything not mapped.
  const KIND_LABELS = {
    EnablePluginEntry: 'Enable plugin',
    DisablePluginEntry: 'Disable plugin',
    UpdatePluginConfig: 'Plugin config',
    UpdatePluginAllowDeny: 'Plugin allow/deny',
    UpdatePluginLoadPaths: 'Plugin load paths',
    UpdatePluginBaseline: 'Plugin baseline',
    InstallMcpServer: 'Install MCP',
    RemoveMcpServer: 'Remove MCP',
    UpdateMcpServerConfig: 'MCP config',
    EnableWebhookIngress: 'Enable webhook ingress',
    DisableWebhookIngress: 'Disable webhook ingress',
    UpdateWebhookMapping: 'Webhook mapping',
    UpdatePluginHookPolicy: 'Hook policy',
    UpdateHookBaseline: 'Hook baseline',
    UpdatePermissionConfig: 'Permission config',
    UpdateExecApproval: 'Exec approval',
    UpsertCronJob: 'Cron upsert',
    RemoveCronJob: 'Cron remove',
    UpdatePermissionBaseline: 'Permission baseline',
    UpdateContentScanCatalog: 'Content-scan catalog',
  };

  const rows = proposals.map(p => {
    const kind = p._action_kind || '';
    const kindLabel = KIND_LABELS[kind] || kind;
    const summary = p.problem || p.admin_surface_summary || '';
    const status = p.status;
    let statusBadge;
    if (status === 'succeeded') {
      statusBadge = '<span class="badge badge-green">applied</span>';
    } else if (status === 'failed_flagged' || status === 'failed_reverted' || status === 'failed_revert_failed') {
      statusBadge = `<span class="badge badge-red">${escHtml(status === 'failed_flagged' ? 'blocked' : status)}</span>`;
    } else if (status === 'applied') {
      statusBadge = '<span class="badge badge-yellow">awaiting verify</span>';
    } else {
      statusBadge = `<span class="badge badge-muted">${escHtml(status)}</span>`;
    }
    // The last history entry carries the apply outcome message — useful
    // when status is failed_flagged so the operator sees the refusal reason.
    const history = p.history || [];
    const lastReason = history.length ? (history[history.length - 1].reason || '') : '';
    const reasonLine = (status !== 'succeeded' && lastReason)
      ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:2px">${escHtml(lastReason)}</div>`
      : '';
    const when = p.created_at
      ? new Date(p.created_at).toLocaleString()
      : '—';
    return `<tr>
      <td style="white-space:nowrap;font-size:0.78rem;color:var(--text2)">${escHtml(when)}</td>
      <td style="white-space:nowrap;font-size:0.82rem">${escHtml(kindLabel)}</td>
      <td>${escHtml(summary)}${reasonLine}</td>
      <td style="text-align:right">${statusBadge}</td>
    </tr>`;
  }).join('');

  const showingHint = (data.total && data.total > data.count)
    ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:8px">Showing the ${data.count} most recent of ${data.total} total entries.</div>`
    : '';

  el.innerHTML = `
    <div class="resp-table-wrap"><table class="resp-table data-table" style="width:100%">
      <thead>
        <tr>
          <th style="text-align:left;width:1%">When</th>
          <th style="text-align:left;width:1%">Action</th>
          <th style="text-align:left">Summary</th>
          <th style="text-align:right;width:1%">Result</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${showingHint}`;
  _respTableLabelize(el);

  // Snapshot for evo's context-pack. Surface the most recent operator-UI
  // changes (top 10) so evo can answer "what changed on plugins this
  // week?" / "show recent permission changes" from this tab without a
  // re-fetch. We project to the same KIND_LABELS mapping the table uses.
  try {
    const projected = proposals.slice(0, 10).map(p => ({
      id: p.id || null,
      when: p.created_at || null,
      kind: p._action_kind || null,
      kind_label: KIND_LABELS[p._action_kind || ''] || (p._action_kind || ''),
      summary: p.problem || p.admin_surface_summary || '',
      status: p.status || null,
    }));
    _ikWriteContextSnapshot({
      bot: _ikBot,
      activity: {
        count: projected.length,
        total: data.total ?? proposals.length,
        items: projected,
      },
    });
  } catch (_) {}
}

// ── Hooks sub-tab (Phase A of spec-hook-governance) ──────────────────────────
let _ikHooksData = null;

async function ikLoadHooks(forceRescan) {
  const el = document.getElementById('ik-hooks-content');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  if (forceRescan) {
    try { await api('POST', '/api/hooks-admin/scan'); } catch (_e) { /* tolerated */ }
  }
  try {
    _ikHooksData = await api('GET', '/api/hooks-admin/inventory');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load hook inventory: ${escHtml(String(e))}</div>`;
    return;
  }
  ikRenderHooks();
}

function ikRenderHooks() {
  const el = document.getElementById('ik-hooks-content');
  if (!el || !_ikHooksData) return;
  const bot = _ikBot;
  const inv = _ikHooksData.bots ? _ikHooksData.bots[bot] : null;
  const resolved = (_ikHooksData.resolved || {})[bot];
  if (!inv) {
    el.innerHTML = '<div class="empty">No hook inventory cached yet. Click ↻ Re-scan to run the monitor now.</div>';
    return;
  }
  if (!inv.openclaw_config_present) {
    el.innerHTML = `<div class="empty">Bot has no openclaw.json (${escHtml(inv.read_error || 'not found')}).</div>`;
    return;
  }

  // Webhook-ingress panel
  const ingress = inv.webhook_ingress || {configured: false};
  const expectIngressOff = resolved && !resolved.webhook_ingress_enabled;
  let ingressPanel;
  if (!ingress.configured) {
    ingressPanel = `<div style="padding:8px 12px;background:var(--bg2);border-radius:6px;font-size:0.82rem">
      <strong>Webhook ingress:</strong> <span class="badge badge-muted">not configured</span>
      <span style="color:var(--text3);margin-left:8px">No external HTTP automation surface on this bot.</span>
    </div>`;
  } else {
    const statusBadge = ingress.enabled
      ? (expectIngressOff
          ? '<span class="badge badge-red">enabled (unexpected)</span>'
          : '<span class="badge badge-yellow">enabled</span>')
      : '<span class="badge badge-muted">configured, disabled</span>';
    const disableBtn = ingress.enabled
      ? `<button class="btn btn-ghost btn-sm" style="margin-left:8px" onclick="proposeDisableIngress('${escHtml(bot)}')" title="Set hooks.enabled=false">Disable…</button>`
      : '';
    ingressPanel = `<div style="padding:8px 12px;background:var(--bg2);border-radius:6px;font-size:0.82rem">
      <div style="margin-bottom:4px"><strong>Webhook ingress:</strong> ${statusBadge}${disableBtn}</div>
      <div style="font-size:0.75rem;color:var(--text3)">
        path: <code>${escHtml(ingress.path || '—')}</code> ·
        mappings: ${ingress.mapping_count} ·
        allowed agents: ${ingress.allowed_agent_ids && ingress.allowed_agent_ids.length ? ingress.allowed_agent_ids.map(escHtml).join(', ') : '—'} ·
        token: ${ingress.has_token ? 'configured' : '<span style="color:var(--yellow)">missing</span>'}${ingress.transforms_dir ? ` · transformsDir: <code>${escHtml(ingress.transforms_dir)}</code>` : ''}
      </div>
    </div>`;
  }

  // Command-gate banner
  let cmdGateBanner = '';
  if (inv.self_mutation_commands_plugins || inv.self_mutation_commands_mcp) {
    const which = [];
    if (inv.self_mutation_commands_plugins) which.push('<code>commands.plugins</code>');
    if (inv.self_mutation_commands_mcp) which.push('<code>commands.mcp</code>');
    cmdGateBanner = `<div style="margin-top:10px;padding:8px 12px;background:var(--bg2);border-left:3px solid var(--yellow);font-size:0.78rem;color:var(--text2)">
      ⚠ ${which.join(' + ')} = true. Bot can mutate hook surfaces via slash commands, bypassing the proposal pipeline.
    </div>`;
  }

  // Plugin-policy table
  const expectedPolicies = (resolved && resolved.expected_plugin_policies) || {};
  const trustedMutators = new Set((resolved && resolved.trusted_prompt_mutators) || []);
  const policies = inv.plugin_policies || [];
  const rows = policies.map(p => {
    const expected = expectedPolicies[p.plugin_name];
    let convBadge, injBadge, expectedCol;
    if (p.allow_conversation_access) {
      convBadge = '<span class="badge badge-green">on</span>';
    } else if (expected && expected.allow_conversation_access && p.plugin_enabled) {
      convBadge = '<span class="badge badge-red" title="silent-disable: baseline says on">off!</span>';
    } else {
      convBadge = '<span class="badge badge-muted">off</span>';
    }
    if (p.allow_prompt_injection) {
      injBadge = trustedMutators.has(p.plugin_name)
        ? '<span class="badge badge-yellow">on (trusted)</span>'
        : '<span class="badge badge-red" title="not in trusted_prompt_mutators allowlist">on!</span>';
    } else {
      injBadge = '<span class="badge badge-muted">off</span>';
    }
    if (expected) {
      expectedCol = `conv=${expected.allow_conversation_access ? 'on' : 'off'} · inj=${expected.allow_prompt_injection ? 'on' : 'off'}`;
    } else {
      expectedCol = '<span style="color:var(--text3)">no baseline</span>';
    }
    const enabledLabel = p.plugin_enabled
      ? '<span style="color:var(--text2)">enabled</span>'
      : '<span style="color:var(--text3)">disabled</span>';
    return `<tr>
      <td><strong>${escHtml(p.plugin_name)}</strong> <span style="font-size:0.72rem">${enabledLabel}</span></td>
      <td>${convBadge}</td>
      <td>${injBadge}</td>
      <td style="font-size:0.72rem;color:var(--text3)">${expectedCol}</td>
      <td><button class="btn btn-ghost btn-sm" onclick="openHookPolicyModal('${escHtml(bot)}','${escHtml(p.plugin_name)}',${p.allow_conversation_access},${p.allow_prompt_injection})" title="Edit this plugin's hook policy flags">Edit…</button></td>
    </tr>`;
  }).join('');

  el.innerHTML = `${ingressPanel}${cmdGateBanner}
    <div style="margin-top:14px;font-size:0.78rem;color:var(--text3);margin-bottom:6px">Plugin typed-hook policies (allowConversationAccess, allowPromptInjection):</div>
    <div class="resp-table-wrap"><table class="resp-table data-table" style="width:100%">
      <thead><tr><th>Plugin</th><th>Conversation access</th><th>Prompt injection</th><th>Baseline expected</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="resp-table-fullspan" style="color:var(--text3);padding:10px">No plugin entries on this bot.</td></tr>'}</tbody>
    </table></div>
    <div style="margin-top:8px;font-size:0.72rem;color:var(--text3)">
      Last observed: ${escHtml(inv.observed_at || '—')}.
      Cells: <span class="badge badge-green">on</span> active · <span class="badge badge-red">off!</span> silent-disable detected ·
      <span class="badge badge-red">on!</span> allowPromptInjection without trusted-mutator allowlist entry ·
      <span class="badge badge-yellow">on (trusted)</span> approved exception.
    </div>`;
  _respTableLabelize(el);

  // Snapshot for evo's context-pack. Surface enough hook surface state
  // — webhook ingress mode, per-plugin policy flags, silent-disable
  // detections — so evo can answer "what hooks are enabled on [bot]?"
  // / "any policy violations?" without re-fetching. The detection
  // results (silent_disable_count, untrusted_injection_count) are the
  // load-bearing summary, since they're the only things on this tab
  // that the operator can usefully act on.
  try {
    const silentDisable = policies.filter(p => {
      const exp = expectedPolicies[p.plugin_name];
      return p.plugin_enabled && exp && exp.allow_conversation_access && !p.allow_conversation_access;
    }).map(p => p.plugin_name);
    const untrustedInjection = policies.filter(p =>
      p.allow_prompt_injection && !trustedMutators.has(p.plugin_name)
    ).map(p => p.plugin_name);
    _ikWriteContextSnapshot({
      bot: bot,
      hooks: {
        webhook_ingress_configured: !!ingress.configured,
        webhook_ingress_enabled: !!ingress.enabled,
        webhook_ingress_unexpected: !!(ingress.enabled && expectIngressOff),
        mapping_count: ingress.mapping_count || 0,
        policy_count: policies.length,
        silent_disable_count: silentDisable.length,
        silent_disable_plugins: silentDisable.slice(0, 8),
        untrusted_injection_count: untrustedInjection.length,
        untrusted_injection_plugins: untrustedInjection.slice(0, 8),
        commands_mutation_open: !!(inv.self_mutation_commands_plugins || inv.self_mutation_commands_mcp),
        observed_at: inv.observed_at || null,
      },
    });
  } catch (_) {}
}

// ── Plugin Phase B + C actions (proposal-creating UI handlers) ──────────────
let _pluginConfigModalTarget = null;  // {bot, plugin, current_config}

function openPluginConfigModal(botId, pluginName) {
  // Look up the current config block from the cached inventory
  let current = null;
  if (_ikPluginsData && _ikPluginsData.bots) {
    const inv = _ikPluginsData.bots[botId];
    if (inv && inv.entries) {
      const entry = inv.entries.find(e => e.name === pluginName);
      // Inventory doesn't surface the full config block (only signature) —
      // we'd need a separate fetch for the live config. For Phase C v1 we
      // present an empty textarea with the operator's intent, plus a
      // reminder that we don't show the current values (to avoid leaking
      // secrets through the inventory cache).
      current = entry || null;
    }
  }
  _pluginConfigModalTarget = {bot: botId, plugin: pluginName, current};
  document.getElementById('plugin-config-title').textContent = `Edit config — ${botLabel(botId)} / ${pluginName}`;
  document.getElementById('plugin-config-op').value = 'set_keys';
  document.getElementById('plugin-config-fields').value = '{\n  \n}';
  document.getElementById('plugin-config-error').style.display = 'none';
  document.getElementById('plugin-config-current').innerHTML = (
    '<em>Note:</em> the inventory cache redacts secret-looking values, so the current ' +
    'config isn\'t shown here. Submit only the keys you want to change.'
  );
  document.getElementById('plugin-config-modal').classList.add('open');
}

function closePluginConfigModal() {
  document.getElementById('plugin-config-modal').classList.remove('open');
  _pluginConfigModalTarget = null;
}

async function submitPluginConfig() {
  if (!_pluginConfigModalTarget) return;
  const errEl = document.getElementById('plugin-config-error');
  errEl.style.display = 'none';
  const op = document.getElementById('plugin-config-op').value;
  const fieldsRaw = document.getElementById('plugin-config-fields').value;
  let fields;
  try {
    fields = JSON.parse(fieldsRaw);
  } catch (e) {
    errEl.textContent = `Fields must be valid JSON: ${e.message || e}`;
    errEl.style.display = 'block';
    return;
  }
  if (typeof fields !== 'object' || Array.isArray(fields) || fields === null) {
    errEl.textContent = 'Fields must be a JSON object.';
    errEl.style.display = 'block';
    return;
  }
  if (op !== 'replace_block' && Object.keys(fields).length === 0) {
    errEl.textContent = 'Provide at least one key.';
    errEl.style.display = 'block';
    return;
  }
  try {
    const resp = await api('POST', '/api/plugins-admin/propose-config', {
      bot_id: _pluginConfigModalTarget.bot,
      plugin_name: _pluginConfigModalTarget.plugin,
      operation: op,
      fields,
    });
    if (!resp.ok) {
      errEl.textContent = resp.error || 'Proposal creation failed.';
      errEl.style.display = 'block';
      return;
    }
    closePluginConfigModal();
    const verb = `Plugin config updated on ${_pluginConfigModalTarget.bot}`;
    if (handleOperatorProposalResult(verb, resp)) ikLoadPlugins(true);
  } catch (e) {
    errEl.textContent = String(e);
    errEl.style.display = 'block';
  }
}

// ── Plugin Phase B actions (proposal-creating UI handlers) ───────────────────

async function proposePluginEnable(botId, pluginName) {
  if (!await confirmModal(`Enable plugin '${pluginName}' on ${botLabel(botId)}?`)) return;
  try {
    const resp = await api('POST', '/api/plugins-admin/propose-enable', {bot_id: botId, plugin_name: pluginName});
    if (handleOperatorProposalResult(`Enabled '${pluginName}' on ${botLabel(botId)}`, resp)) {
      ikLoadPlugins(true);
    }
  } catch (e) { toast(`Enable failed: ${e}`, 'err'); }
}

async function proposePluginDisable(botId, pluginName) {
  if (!await confirmModal({body: `Disable plugin '${pluginName}' on ${botLabel(botId)}?\n\nThe applier refuses if the plugin is in the baseline's required_plugins.`, danger: true})) return;
  try {
    const resp = await api('POST', '/api/plugins-admin/propose-disable', {bot_id: botId, plugin_name: pluginName});
    if (handleOperatorProposalResult(`Disabled '${pluginName}' on ${botLabel(botId)}`, resp)) {
      ikLoadPlugins(true);
    }
  } catch (e) { toast(`Disable failed: ${e}`, 'err'); }
}

async function proposePluginAdoptAllow() {
  // Set THIS bot's plugins.allow to the currently-enabled plugin set.
  // (v2 retired the "baseline-expected" set, so the operator-meaningful
  // default is "what's actually running now" — same intent as the old
  // bulk-adopt button minus the cross-bot fanout.)
  const bot = _ikBot;
  if (!_ikPluginsData) { toast('Plugin data not loaded yet.', 'err'); return; }
  const inv = _ikPluginsData.bots ? _ikPluginsData.bots[bot] : null;
  if (!inv || !inv.entries) { toast(`No plugin inventory for ${botLabel(bot)}.`, 'err'); return; }
  const expected = inv.entries.filter(e => e.enabled).map(e => e.name).sort();
  if (!expected.length) { toast(`No enabled plugins on ${botLabel(bot)} — nothing to adopt.`, 'err'); return; }
  if (!await confirmModal(`Set ${botLabel(bot)}'s plugins.allow list to the currently-enabled set:\n\n${expected.join(', ')}`)) return;
  try {
    const resp = await api('POST', '/api/plugins-admin/propose-allow-deny', {bot_id: bot, allow: expected});
    if (handleOperatorProposalResult(`Allow list set on ${botLabel(bot)}`, resp)) {
      ikLoadPlugins(true);
    }
  } catch (e) { toast(`Allow-list update failed: ${e}`, 'err'); }
}

// proposePluginBulkAdoptAllow was retired with the v2 plugin posture
// rework (docs/spec-plugin-posture-rework-2026-06-06.md). The
// baseline-expected set it computed from no longer exists. The
// per-bot "Adopt allow list" button (proposePluginAdoptAllow) handles
// the same intent against the live-enabled set.

// ── MCP Catalog browser + Install workflow (Phase B) ─────────────────────────
let _mcpCatalog = null;
let _mcpInstallEntry = null;

async function openMcpCatalogModal() {
  document.getElementById('mcp-catalog-modal').classList.add('open');
  const listEl = document.getElementById('mcp-catalog-list');
  listEl.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  let advByPkg = {};
  try {
    _mcpCatalog = await api('GET', '/api/mcp-admin/catalog');
    // Best-effort: fetch advisory counts in parallel; tolerate failure
    try {
      const advData = await api('GET', '/api/mcp-admin/advisories');
      advByPkg = (advData && advData.packages) || {};
    } catch (_e) { /* tolerated */ }
  } catch (e) {
    listEl.innerHTML = `<div class="empty">Failed to load catalog: ${escHtml(String(e))}</div>`;
    return;
  }
  const entries = (_mcpCatalog && _mcpCatalog.entries) || [];
  if (!entries.length) {
    listEl.innerHTML = '<div class="empty">Catalog is empty. Edit {shared_dir}/mcp/catalog/pod-catalog.json to add entries.</div>';
    return;
  }
  listEl.innerHTML = entries.map(e => {
    const statusBadge = e.vetting_status === 'approved'
      ? '<span class="badge badge-green">approved</span>'
      : e.vetting_status === 'withdrawn'
      ? '<span class="badge badge-red">withdrawn</span>'
      : '<span class="badge badge-yellow">candidate</span>';
    const transport = e.transport === 'stdio'
      ? '<span class="badge badge-muted">stdio</span>'
      : '<span class="badge badge-muted">http</span>';
    const advCount = e.package_name && advByPkg[e.package_name] ? advByPkg[e.package_name].length : 0;
    const advBadge = advCount > 0
      ? `<span class="badge badge-red" title="${advCount} open advisory(ies)">${advCount} CVE</span>`
      : (e.package_name ? '<span class="badge badge-green" title="No cached advisories">clean</span>' : '');
    const advAction = advCount > 0
      ? `<a href="javascript:void(0)" onclick="openMcpAdvisoryModal('${escHtml(e.package_name)}','${escHtml(e.id)}')" style="font-size:0.72rem;margin-left:6px">view…</a>`
      : '';
    const tools = (e.advertised_tools || []).slice(0, 6).map(t =>
      `<code style="font-size:0.7rem;background:var(--bg3);padding:1px 5px;border-radius:3px;margin-right:4px">${escHtml(t)}</code>`
    ).join('');
    const moreTools = (e.advertised_tools || []).length > 6
      ? `<span style="font-size:0.72rem;color:var(--text3)">+${e.advertised_tools.length - 6} more</span>`
      : '';
    const envSummary = (e.required_env || []).length
      ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">Requires: ${e.required_env.map(re => `<code>${escHtml(re.name)}</code>`).join(', ')}</div>`
      : '<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">No credentials required.</div>';
    // Install affordance: three states.
    //   - withdrawn → fully blocked, faded
    //   - has open CVEs → blocked, but show a clear "Review CVEs" button that
    //     opens the advisory modal (rather than a misleading faded "Install…")
    //   - clean → normal Install
    let installBtn;
    if (e.vetting_status === 'withdrawn') {
      installBtn = `<button class="btn btn-ghost btn-sm" disabled
        title="This catalog entry is withdrawn"
        style="white-space:nowrap;min-width:90px;flex:0 0 auto">Withdrawn</button>`;
    } else if (advCount > 0) {
      installBtn = `<button class="btn btn-ghost btn-sm"
        onclick="openMcpAdvisoryModal('${escHtml(e.package_name)}','${escHtml(e.id)}')"
        title="${advCount} open advisory(ies) — review before installing"
        style="white-space:nowrap;min-width:90px;flex:0 0 auto;border:1px solid var(--red);color:var(--red)">Review CVEs</button>`;
    } else {
      installBtn = `<button class="btn btn-primary btn-sm"
        onclick="openMcpInstallModal('${escHtml(e.id)}')"
        style="white-space:nowrap;min-width:90px;flex:0 0 auto">Install…</button>`;
    }
    return `<div class="card" style="margin-bottom:10px">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">
            <strong style="font-size:0.95rem">${escHtml(e.name)}</strong>
            ${transport} ${statusBadge} ${advBadge}${advAction}
          </div>
          <div style="font-size:0.82rem;color:var(--text2);margin-bottom:6px">${escHtml(e.description || '')}</div>
          <div style="margin-bottom:4px">${tools}${moreTools}</div>
          ${envSummary}
        </div>
        ${installBtn}
      </div>
    </div>`;
  }).join('');
}

function closeMcpCatalogModal() {
  document.getElementById('mcp-catalog-modal').classList.remove('open');
}

// Expand a keystore_hint glob like "github-*" against the currently-picked
// bot. The catalog uses "<provider>-*" as the suggested per-bot slot pattern,
// so swapping in the bot id is the natural default. If the hint has no
// wildcard, return it verbatim. Used to auto-suggest a keystore slot name
// so the operator doesn't have to invent one.
function _mcpExpandKeystoreHint(hint, botId) {
  if (!hint) return '';
  if (hint.indexOf('*') === -1) return hint;
  return hint.replace(/\*/g, botId || '');
}

function openMcpInstallModal(catalogId) {
  const entry = (_mcpCatalog && _mcpCatalog.entries || []).find(e => e.id === catalogId);
  if (!entry) return;
  _mcpInstallEntry = entry;
  document.getElementById('mcp-install-title').textContent = `Install ${entry.name}`;
  document.getElementById('mcp-install-description').textContent = entry.description || '';
  // Bot picker — populate from network. Wire onchange so the keystore slot
  // suggestions update when the operator picks a different bot (e.g.
  // hint "github-*" becomes "github-team_bot_a" vs "github-personal_bot").
  const botSel = document.getElementById('mcp-install-bot');
  const bots = Object.keys(_networkData.bots || {});
  botSel.innerHTML = bots.map(b => `<option value="${escHtml(b)}" ${b === _ikBot ? 'selected' : ''}>${escHtml(botLabel(b))}</option>`).join('');
  botSel.onchange = _mcpRenderInstallEnvs;
  _mcpRenderInstallEnvs();
  document.getElementById('mcp-install-error').style.display = 'none';
  document.getElementById('mcp-install-modal').classList.add('open');
}

// Render the env-binding form. Separated from openMcpInstallModal so the
// bot-picker onchange can re-render (which updates the auto-suggested
// keystore slot name based on the new bot id).
function _mcpRenderInstallEnvs() {
  const entry = _mcpInstallEntry;
  const envBox = document.getElementById('mcp-install-envs');
  if (!entry || !envBox) return;
  const botSel = document.getElementById('mcp-install-bot');
  const bot = botSel ? botSel.value : (_ikBot || '');
  if (!entry.required_env || !entry.required_env.length) {
    envBox.innerHTML = '<div style="font-size:0.82rem;color:var(--text3)">No credentials required by this server.</div>';
    return;
  }
  envBox.innerHTML = entry.required_env.map(re => {
    const suggestedSlot = _mcpExpandKeystoreHint(re.keystore_hint, bot);
    const scopeNote = re.scope_recommendation
      ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:4px">${escHtml(re.scope_recommendation)}</div>`
      : '';
    return `<div class="form-field" style="margin-bottom:14px;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2)">
      <div style="font-weight:600;font-size:0.85rem;margin-bottom:6px"><code>${escHtml(re.name)}</code></div>
      <div style="font-size:0.78rem;color:var(--text2);margin-bottom:8px">${escHtml(re.purpose || '')}</div>
      <label style="display:block;margin-bottom:6px">
        <span style="display:block;font-size:0.78rem;color:var(--text2);margin-bottom:3px">Keystore slot name</span>
        <input type="text" class="mcp-env-input" data-env-name="${escHtml(re.name)}" value="${escHtml(suggestedSlot)}" placeholder="${escHtml(suggestedSlot || re.keystore_hint || 'my-key')}" style="width:100%;padding:6px 8px;font-family:monospace;font-size:0.85rem">
      </label>
      <label style="display:block">
        <span style="display:block;font-size:0.78rem;color:var(--text2);margin-bottom:3px">Token value <span style="color:var(--text3)">(optional — leave blank to use existing keystore value)</span></span>
        <input type="password" class="mcp-env-token-input" data-env-name="${escHtml(re.name)}" placeholder="paste token here, or leave blank if already in keystore" style="width:100%;padding:6px 8px;font-family:monospace;font-size:0.85rem">
      </label>
      ${scopeNote}
    </div>`;
  }).join('');
}

function closeMcpInstallModal() {
  document.getElementById('mcp-install-modal').classList.remove('open');
  _mcpInstallEntry = null;
}

async function submitMcpInstall() {
  if (!_mcpInstallEntry) return;
  const bot = document.getElementById('mcp-install-bot').value;
  const errEl = document.getElementById('mcp-install-error');
  errEl.style.display = 'none';
  if (!bot) {
    errEl.textContent = 'Pick a bot.';
    errEl.style.display = 'block';
    return;
  }
  // Collect both the keystore slot names and any inline token values. The
  // token-value field is optional per env binding — operators who already
  // have a PAT stored at that slot can leave it blank.
  const envBindings = {};
  const tokenValues = {};
  const slotInputs = document.querySelectorAll('#mcp-install-envs .mcp-env-input');
  const tokenInputs = document.querySelectorAll('#mcp-install-envs .mcp-env-token-input');
  const tokensByEnv = {};
  for (const inp of tokenInputs) {
    tokensByEnv[inp.dataset.envName] = inp.value.trim();
  }
  for (const inp of slotInputs) {
    const name = inp.dataset.envName;
    const slot = inp.value.trim();
    if (!slot) {
      errEl.textContent = `Provide a keystore slot name for ${name}.`;
      errEl.style.display = 'block';
      return;
    }
    envBindings[name] = `keystore:${slot}`;
    const inlineToken = tokensByEnv[name];
    if (inlineToken) {
      tokenValues[slot] = inlineToken;
    }
  }
  try {
    const reqBody = {
      bot_id: bot,
      server_id: _mcpInstallEntry.id,
      catalog_id: _mcpInstallEntry.id,
      env_bindings: envBindings,
    };
    if (Object.keys(tokenValues).length) {
      reqBody.token_values = tokenValues;
    }
    const resp = await api('POST', '/api/mcp-admin/install', reqBody);
    if (!resp.ok) {
      errEl.textContent = resp.error || 'Install proposal failed.';
      errEl.style.display = 'block';
      return;
    }
    closeMcpInstallModal();
    closeMcpCatalogModal();
    const tokensNote = resp.tokens_saved
      ? ` (saved ${resp.tokens_saved} token${resp.tokens_saved === 1 ? '' : 's'} to keystore)`
      : '';
    if (handleOperatorProposalResult(`Installed '${_mcpInstallEntry.id}' on ${botLabel(bot)}${tokensNote}`, resp)) {
      ikLoadMcp(true);
    }
  } catch (e) {
    errEl.textContent = String(e);
    errEl.style.display = 'block';
  }
}

async function openMcpAdvisoryModal(packageName, serverId) {
  document.getElementById('mcp-advisory-title').textContent = `Advisories — ${serverId} (${packageName})`;
  document.getElementById('mcp-advisory-modal').classList.add('open');
  const el = document.getElementById('mcp-advisory-content');
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  let data;
  try {
    data = await api('GET', '/api/mcp-admin/advisories');
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load advisories: ${escHtml(String(e))}</div>`;
    return;
  }
  const advisories = ((data && data.packages) || {})[packageName] || [];
  if (!advisories.length) {
    el.innerHTML = '<div class="empty">No cached advisories for this package. Cache may be empty until the next refresh cycle.</div>';
    return;
  }
  // Sort newest-first by updated_at
  advisories.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
  el.innerHTML = advisories.map(a => {
    const sevBadge = a.severity === 'critical' || a.severity === 'high'
      ? `<span class="badge badge-red">${escHtml(a.severity)}</span>`
      : a.severity === 'medium' || a.severity === 'moderate'
      ? `<span class="badge badge-yellow">${escHtml(a.severity)}</span>`
      : `<span class="badge badge-muted">${escHtml(a.severity || 'unknown')}</span>`;
    const patched = a.patched_version
      ? `<div style="font-size:0.78rem;color:var(--text2);margin-top:4px">Patched in <code>${escHtml(a.patched_version)}</code></div>`
      : '<div style="font-size:0.78rem;color:var(--text3);margin-top:4px">No patched version published yet</div>';
    const link = a.html_url
      ? ` · <a href="${escHtml(a.html_url)}" target="_blank" rel="noopener">advisory</a>`
      : '';
    return `<div class="card" style="margin-bottom:10px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        ${sevBadge}
        <strong>${escHtml(a.ghsa_id || '')}</strong>
        ${a.cve_id ? `<span style="font-size:0.78rem;color:var(--text2)">${escHtml(a.cve_id)}</span>` : ''}
        ${link}
      </div>
      <div style="font-size:0.85rem;color:var(--text)">${escHtml(a.summary || '')}</div>
      <div style="font-size:0.75rem;color:var(--text3);margin-top:6px">
        Vulnerable range: <code>${escHtml(a.vulnerable_version_range || 'unspecified')}</code>
      </div>
      ${patched}
      <div style="font-size:0.7rem;color:var(--text3);margin-top:6px">
        Published ${escHtml(a.published_at || '?')} · Updated ${escHtml(a.updated_at || '?')}
      </div>
    </div>`;
  }).join('');
}

function closeMcpAdvisoryModal() {
  document.getElementById('mcp-advisory-modal').classList.remove('open');
}

async function refreshAdvisories() {
  const btn = document.getElementById('mcp-catalog-refresh-adv');
  if (btn) { btn.disabled = true; btn.textContent = '↻ Refreshing…'; }
  try {
    const resp = await api('POST', '/api/mcp-admin/advisories/refresh');
    if (resp.ok) {
      toast(`Advisory cache refreshed: ${resp.refreshed} package(s) updated.`, 'ok');
    } else {
      toast(`Advisory refresh failed: ${resp.error || 'unknown error'}`, 'err');
    }
  } catch (e) {
    toast(`Advisory refresh failed: ${e}`, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh advisories'; }
  }
}

async function openMcpHealthModal(botId, serverId) {
  document.getElementById('mcp-health-title').textContent = `Probe History — ${botLabel(botId)} / ${serverId}`;
  document.getElementById('mcp-health-modal').classList.add('open');
  const el = document.getElementById('mcp-health-content');
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  let data;
  try {
    data = await api('GET', `/api/mcp-admin/health/${encodeURIComponent(botId)}/${encodeURIComponent(serverId)}`);
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed to load probes: ${escHtml(String(e))}</div>`;
    return;
  }
  const entries = (data && data.entries) || [];
  if (!entries.length) {
    el.innerHTML = '<div class="empty">No probe history yet. Click ↻ Re-scan on the MCP tab to run one now.</div>';
    return;
  }
  const ok = entries.filter(e => e.ok).length;
  const fail = entries.length - ok;
  const summary = `<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:12px;font-size:0.82rem">
    <div><strong>${entries.length}</strong> probe${entries.length === 1 ? '' : 's'}</div>
    <div style="color:${ok ? 'var(--green)' : 'var(--text2)'}"><strong>${ok}</strong> ok</div>
    <div style="color:${fail ? 'var(--red)' : 'var(--text2)'}"><strong>${fail}</strong> fail</div>
  </div>`;
  const rows = entries.map(e => {
    const okBadge = e.ok
      ? '<span class="badge badge-green">ok</span>'
      : (e.error_class === 'credential_invalid'
          ? '<span class="badge badge-red">cred</span>'
          : `<span class="badge badge-yellow">${escHtml(e.error_class || 'fail')}</span>`);
    const info = e.ok && e.server_info
      ? `${escHtml(e.server_info.name || '')} ${escHtml(e.server_info.version || '')}`
      : escHtml(e.error_detail || '');
    return `<tr>
      <td style="font-family:monospace;font-size:0.75rem;color:var(--text2);white-space:nowrap">${escHtml(e.probed_at || '')}</td>
      <td>${okBadge}</td>
      <td style="font-size:0.78rem;color:var(--text2)">${e.latency_ms != null ? escHtml(String(e.latency_ms)) + 'ms' : '—'}</td>
      <td style="font-size:0.78rem;color:var(--text2);max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(e.error_detail || '')}">${info || '<span style="color:var(--text3)">—</span>'}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `${summary}
    <table class="data-table" style="width:100%">
      <thead><tr><th>Probed at</th><th>Result</th><th>Latency</th><th>Detail</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function closeMcpHealthModal() {
  document.getElementById('mcp-health-modal').classList.remove('open');
}

async function proposeMcpRemove(botId, serverId) {
  if (!await confirmModal({body: `Remove MCP server '${serverId}' from ${botLabel(botId)}?`, danger: true})) {
    return;
  }
  try {
    const resp = await api('POST', '/api/mcp-admin/remove', {bot_id: botId, server_id: serverId});
    if (handleOperatorProposalResult(`Removed '${serverId}' from ${botId}`, resp)) {
      ikLoadMcp(true);
    }
  } catch (e) {
    toast(`Remove failed: ${e}`, 'err');
  }
}

// ── Bot header chip strip + integration data ────────────────────────────────
// Replaces the retired Channels sub-tab. `/api/integrations` returns the heal
// scan data per bot (gateway probe, evolve plugin probe, channels{} entries,
// plugins.entries{} entries prefixed "Plugin: ..."). We re-use that data to:
//   - render a "reachable via / gateway live / evolve plugin live" chip strip
//     above the sub-tabs (header-level at-a-glance answer)
//   - feed the Messaging section at the top of the Plugins tab via
//     ikIntegrationMessagingRows() (rows belonging to messaging plugins only)
//
// Infrastructure probes that appear as rows in /api/integrations — these
// drive the Runtime chips in the bot header strip, not the Messaging
// section. (Single canonical messaging-id set lives at _IK_PLUGIN_MESSAGING
// above; reused here so the heal-scan view and the Plugins tab can't drift.)
const _IK_INFRA_NAMES = new Set(['OC Gateway', 'Evolve Plugin']);

function _ikRowMessagingId(row) {
  // Returns the plugin id if this row is a messaging surface, else null.
  if (!row) return null;
  const m = (row.name || '').match(/^Plugin:\s*(.+)$/i);
  const id = (m ? m[1] : (row.id || row.name || '')).trim().toLowerCase();
  return _IK_PLUGIN_MESSAGING.has(id) ? id : null;
}

async function ikLoadIntegrations() {
  _ikIntData = await api('GET', '/api/integrations');
  ikRenderBotHeaderStrip();
  // The Plugins tab's Messaging section pulls from _ikIntData too, so re-render
  // it if the plugin inventory is already cached.
  if (_ikPluginsData) ikRenderPlugins();
  // Snapshot the cross-bot integration health for evo's context-pack.
  // Per-bot rows summarize the runtime + messaging chip state visible
  // in the header strip on each bot. Helps evo answer "is this bot
  // reachable via discord?" / "which bots have unhealthy integrations?"
  try {
    const perBot = {};
    for (const [bid, d] of Object.entries(_ikIntData || {})) {
      if (!d || d.error) continue;
      const rows = d.channels || [];
      const issues = rows.filter(r => r.status && r.status !== 'ok')
        .map(r => `${r.name}:${r.status}`);
      perBot[bid] = {
        channel_count: rows.length,
        issue_count: issues.length,
        issues: issues.slice(0, 6),
      };
    }
    _ikWriteContextSnapshot({ integrations: { per_bot: perBot } });
  } catch (_) {}
}

function ikIntegrationRowsForBot(botId) {
  if (!_ikIntData) return null;
  const d = _ikIntData[botId];
  if (!d || d.error) return null;
  return d.channels || [];
}

// Messaging rows for the Plugins tab's top section. Dedupes by plugin id —
// `channels{}` block rows take precedence over "Plugin: ..." rows when both
// exist for the same plugin, because the channels-block row carries the
// richer config + connection state.
function ikIntegrationMessagingRows(botId) {
  const rows = ikIntegrationRowsForBot(botId);
  if (!rows) return [];
  const out = new Map();
  for (const r of rows) {
    const id = _ikRowMessagingId(r);
    if (!id) continue;
    // Prefer the unprefixed (channels-block) row over the "Plugin: ..." row.
    const isPluginPrefixed = (r.name || '').toLowerCase().startsWith('plugin:');
    if (out.has(id) && !isPluginPrefixed) {
      // already have a plugin-prefixed entry; replace with the channel-block one
      out.set(id, r);
    } else if (!out.has(id)) {
      out.set(id, r);
    }
  }
  return Array.from(out.values());
}

function _ikStatusBadge(s) {
  const map = {ok: 'var(--green)', warning: 'var(--yellow)', error: 'var(--red)', unknown: 'var(--text3)'};
  const label = {ok: '✅ Connected', warning: '⚠️ Degraded', error: '❌ Error', unknown: '— Unknown'};
  return `<span style="color:${map[s]||map.unknown};font-size:0.82rem">${label[s]||s}</span>`;
}

function ikRenderBotHeaderStrip() {
  const el = document.getElementById('ik-bot-header-strip');
  if (!el) return;
  // POD tab has no per-bot runtime; _ikApplyPodMode already display:none'd
  // the container, but bail here too so the inner HTML doesn't get a
  // "Data unavailable for __pod__" red error painted into it.
  if (_ikIsPod()) { el.innerHTML = ''; return; }
  if (!_ikIntData) { el.innerHTML = ''; return; }
  const botId = _ikBot;
  const rows = ikIntegrationRowsForBot(botId);
  if (rows === null) {
    el.innerHTML = `<div style="color:var(--red);font-size:0.85rem;padding:10px 0">⚫ Data unavailable for ${escHtml(botId || '')}. Click ⟳ Scan to refresh.</div>`;
    return;
  }
  // Infra chips (OC Gateway, Evolve Plugin) first; messaging chips second.
  const infraChips = rows.filter(r => _IK_INFRA_NAMES.has(r.name || '')).map(r => {
    const ok = (r.status || '') === 'ok';
    const color = ok ? 'var(--green)' : (r.status === 'warning' ? 'var(--yellow)' : 'var(--red)');
    const dot = ok ? '●' : '○';
    return `<span class="badge" style="background:transparent;border:1px solid ${color};color:${color};margin-right:6px">${dot} ${escHtml(r.name)}${r.details && r.details !== 'configured' ? ` <span style="opacity:0.7;font-weight:400">· ${escHtml(r.details)}</span>` : ''}</span>`;
  }).join('');

  const messagingRows = ikIntegrationMessagingRows(botId);
  const reachableChips = messagingRows.length
    ? messagingRows.map(r => {
        const id = _ikRowMessagingId(r) || 'channel';
        const ok = (r.status || '') === 'ok';
        const color = ok ? 'var(--green)' : (r.status === 'warning' ? 'var(--yellow)' : 'var(--red)');
        return `<span class="badge" style="background:transparent;border:1px solid ${color};color:${color};margin-right:6px">${escHtml(id)}</span>`;
      }).join('')
    : '<span style="color:var(--text3);font-size:0.78rem">no messaging channels configured</span>';

  el.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:14px;align-items:center;padding:8px 0;font-size:0.78rem;color:var(--text2);border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="color:var(--text3);text-transform:uppercase;letter-spacing:0.04em;font-size:0.7rem">Runtime</span>
        ${infraChips || '<span style="color:var(--text3);font-size:0.78rem">no runtime data</span>'}
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <span style="color:var(--text3);text-transform:uppercase;letter-spacing:0.04em;font-size:0.7rem">Reachable via</span>
        ${reachableChips}
      </div>
    </div>`;
}

async function ikScanNow() {
  const btn = document.getElementById('ik-scan-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  try {
    const r = await api('POST', '/api/integrations/scan');
    if (r.ok && r.data) {
      _ikIntData = r.data;
      ikRenderBotHeaderStrip();
      if (_ikPluginsData) ikRenderPlugins();
      toast('Scan complete', 'ok');
    } else {
      toast(r.error || 'Scan failed', 'err');
    }
  } catch(e) {
    toast('Scan error: ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⟳ Scan'; }
  }
}

async function ikRefreshKeys() {
  if (!_ikBot) return;
  await Promise.all([ikRenderKeys(_ikBot), ikLoadGithubStatus()]);
}

// ── GitHub three-purposes summary row ───────────────────────────────────────
// Renders a pod-wide GitHub row above the per-bot API Keys table covering:
//   1. Workspace backup     — per-bot, via openOnboardModal('github', [bot])
//   2. Issue tracking       — pod-wide, via _inboxOpenIntakeTokenModal()
//   3. General code access  — placeholder until the MCP-curated flow exists
// Routes each "Set up" CTA into the existing flow rather than replicating it.
async function ikLoadGithubStatus() {
  const el = document.getElementById('ik-github-row');
  if (!el) return;
  let r;
  try {
    r = await api('GET', '/api/credentials/github/status');
  } catch (e) {
    el.innerHTML = `<div style="font-size:0.82rem;color:var(--red)">Failed to load GitHub status: ${escHtml(e.message || String(e))}</div>`;
    return;
  }
  if (!r || r.error) {
    el.innerHTML = `<div style="font-size:0.82rem;color:var(--red)">${escHtml((r && r.error) || 'Failed to load GitHub status')}</div>`;
    return;
  }
  el.innerHTML = _ikRenderGithubStatus(r);
}

function _ikRenderGithubStatus(r) {
  // Dispatch by tab: POD shows the pod-wide credentials (intake + general
  // access) + a backup roll-up that links into per-bot. Per-bot shows
  // THIS bot's backup status + a general-access row that reflects whether
  // the bot is using the pod-wide token or its own override.
  return _ikIsPod()
    ? _ikRenderGithubBlockForPod(r)
    : _ikRenderGithubBlockForBot(r, _ikBot);
}

// ── Shared row styling ──────────────────────────────────────────────────
const _IK_GH_ROW = 'display:grid;grid-template-columns:200px 1fr auto;gap:14px;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)';
const _IK_GH_ROW_LAST = 'display:grid;grid-template-columns:200px 1fr auto;gap:14px;align-items:center;padding:8px 0';
const _IK_GH_LABEL = 'font-size:0.85rem;color:var(--text2)';
const _IK_GH_NOTE = 'font-size:0.7rem;color:var(--text3);margin-top:2px';

function _ikRenderGithubBlockForPod(r) {
  const backup = r.backup || {configured_count: 0, total_bots: 0, bot_status: []};
  const intake = r.intake || {configured: false, login: null, slot: 'github_intake'};
  const general = r.general_access || {pod: {configured: false, login: null, slot: 'github_general_access'}, overrides: []};
  const pod = general.pod || {configured: false, login: null, slot: 'github_general_access'};
  const overrides = Array.isArray(general.overrides) ? general.overrides : [];

  // ── Row 1: Issue tracking (intake) — pod-wide PAT ─────────────────────
  let intakeBadge, intakeCta;
  if (intake.configured && intake.login) {
    intakeBadge = `<span style="color:var(--green)">✅ as @${escHtml(intake.login)}</span>`;
    intakeCta = 'Manage →';
  } else if (intake.configured) {
    intakeBadge = '<span style="color:var(--orange)">⚠ Token saved but not validating</span>';
    intakeCta = 'Manage →';
  } else {
    intakeBadge = '<span style="color:var(--orange)">⚠ Not configured</span>';
    intakeCta = 'Set up →';
  }
  const intakeSlot = escHtml(intake.slot || 'github_intake');

  // ── Row 2: General code access (pod-wide PAT used by GitHub MCP) ──────
  let podBadge, podCta;
  if (pod.configured && pod.login) {
    podBadge = `<span style="color:var(--green)">✅ as @${escHtml(pod.login)}</span>`;
    podCta = 'Manage →';
  } else if (pod.configured) {
    podBadge = '<span style="color:var(--orange)">⚠ Token saved but not validating</span>';
    podCta = 'Manage →';
  } else {
    podBadge = '<span style="color:var(--text3)">○ Not configured</span>';
    podCta = 'Set up →';
  }
  const podSlot = escHtml(pod.slot || 'github_general_access');

  // ── Overrides summary ─────────────────────────────────────────────────
  let overridesBlock = '';
  if (overrides.length) {
    overridesBlock = `
      <div style="margin-top:6px;padding:8px 10px;background:var(--bg2);border-radius:6px;font-size:0.78rem;color:var(--text2)">
        <div style="margin-bottom:4px;color:var(--text3);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.04em">Per-bot overrides (${overrides.length})</div>
        ${overrides.map(o => {
          const who = o.login ? `as @${escHtml(o.login)}` : '<span style="color:var(--orange)">⚠ token saved, not validating</span>';
          return `<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0">
            <span>• <strong>${escHtml(o.bot)}</strong> · ${who}</span>
            <a href="#" onclick="ikSwitchBot('${escHtml(o.bot)}');return false" style="color:var(--accent);text-decoration:none;font-size:0.75rem">Open ${escHtml(botLabel(o.bot))} →</a>
          </div>`;
        }).join('')}
      </div>`;
  }

  // ── Backup roll-up: pod-level summary, deep-link to each per-bot ───
  let backupSummary;
  if (backup.total_bots === 0) {
    backupSummary = '<span style="color:var(--text3)">No bots configured</span>';
  } else if (backup.configured_count === backup.total_bots) {
    backupSummary = `<span style="color:var(--green)">✅ ${backup.configured_count}/${backup.total_bots} bots configured</span>`;
  } else if (backup.configured_count === 0) {
    backupSummary = `<span style="color:var(--orange)">⚠ Not configured (0/${backup.total_bots} bots)</span>`;
  } else {
    backupSummary = `<span style="color:var(--orange)">⚠ ${backup.configured_count}/${backup.total_bots} bots configured</span>`;
  }

  return `
    <div style="font-size:0.72rem;color:var(--text3);margin-bottom:10px">
      Pod-wide GitHub credentials. Issue tracking and general code access are stored once and used by every bot. Workspace backups are per-bot — manage each from its tab.
    </div>
    <div style="${_IK_GH_ROW}">
      <div>
        <div style="${_IK_GH_LABEL}"><strong>Issue tracking (inbox)</strong></div>
        <div style="${_IK_GH_NOTE}">PAT that lets evo file intakes as GitHub issues.</div>
      </div>
      <div>${intakeBadge}</div>
      <div>
        <button class="btn btn-ghost btn-sm" id="ik-github-intake-setup"
                onclick="_inboxOpenIntakeTokenModal('${intakeSlot}')">
          ${intakeCta}
        </button>
      </div>
    </div>
    <div style="${_IK_GH_ROW}">
      <div>
        <div style="${_IK_GH_LABEL}"><strong>General code access</strong></div>
        <div style="${_IK_GH_NOTE}">Default PAT used by any bot that installs the GitHub MCP server.</div>
      </div>
      <div>
        ${podBadge}
        ${overridesBlock}
      </div>
      <div>
        <button class="btn btn-ghost btn-sm" id="ik-github-general-setup"
                onclick="_inboxOpenIntakeTokenModal('${podSlot}')">
          ${podCta}
        </button>
      </div>
    </div>
    <div style="${_IK_GH_ROW_LAST}">
      <div>
        <div style="${_IK_GH_LABEL}"><strong>Workspace backup</strong></div>
        <div style="${_IK_GH_NOTE}">Per-bot push to a private repo — managed from each bot's tab.</div>
      </div>
      <div>
        ${backupSummary}
        <div style="${_IK_GH_NOTE}">
          <a href="#" onclick="_ikNavToBackup();return false" style="color:var(--accent);text-decoration:none">View backup status →</a>
        </div>
      </div>
      <div>
        <span style="color:var(--text3);font-size:0.75rem">per-bot</span>
      </div>
    </div>
  `;
}

function _ikRenderGithubBlockForBot(r, botId) {
  const backup = r.backup || {configured_count: 0, total_bots: 0, bot_status: []};
  const general = r.general_access || {pod: {configured: false, login: null, slot: 'github_general_access'}, overrides: []};
  const pod = general.pod || {configured: false, login: null, slot: 'github_general_access'};
  const overrides = Array.isArray(general.overrides) ? general.overrides : [];

  // ── Row 1: Workspace backup for THIS bot ──────────────────────────────
  const thisBackup = (backup.bot_status || []).find(b => b.bot === botId);
  const backupConfigured = !!(thisBackup && thisBackup.configured);
  const backupBadge = backupConfigured
    ? '<span style="color:var(--green)">✅ Configured</span>'
    : '<span style="color:var(--orange)">⚠ Not configured</span>';
  const backupCta = backupConfigured ? 'Manage →' : 'Set up →';
  const backupBotsJson = JSON.stringify([botId]);

  // ── Row 2: General code access — pod-wide vs override ─────────────────
  const myOverride = overrides.find(o => o.bot === botId);
  let generalBadge, generalCta, generalCtaSlot, generalNote;
  if (myOverride) {
    generalBadge = myOverride.login
      ? `<span style="color:var(--green)">✅ Override: as @${escHtml(myOverride.login)}</span>`
      : '<span style="color:var(--orange)">⚠ Override set, not validating</span>';
    generalCta = 'Manage override →';
    generalCtaSlot = escHtml(myOverride.slot || `github-${botId}`);
    generalNote = `This bot uses its own PAT instead of the pod-wide token.`;
  } else if (pod.configured) {
    generalBadge = pod.login
      ? `<span style="color:var(--text2)">Uses pod-wide token (@${escHtml(pod.login)})</span>`
      : '<span style="color:var(--text2)">Uses pod-wide token</span>';
    generalCta = 'Set override →';
    generalCtaSlot = `github-${escHtml(botId)}`;
    generalNote = `No override — this bot will use the pod-wide token when the GitHub MCP server is installed.`;
  } else {
    generalBadge = '<span style="color:var(--text3)">○ No pod-wide token, no override</span>';
    generalCta = 'Set override →';
    generalCtaSlot = `github-${escHtml(botId)}`;
    generalNote = `Set the pod-wide token in the POD tab, or paste a per-bot PAT here.`;
  }

  return `
    <div style="font-size:0.72rem;color:var(--text3);margin-bottom:10px">
      Per-bot GitHub credentials for <strong>${escHtml(botLabel(botId))}</strong>. Issue-tracking and general code access live in the
      <a href="#" onclick="ikSwitchBot('${IK_POD_SENTINEL}');return false" style="color:var(--accent);text-decoration:none">POD tab</a>;
      this view shows only what's specific to this bot.
    </div>
    <div style="${_IK_GH_ROW}">
      <div>
        <div style="${_IK_GH_LABEL}"><strong>Workspace backup</strong></div>
        <div style="${_IK_GH_NOTE}">Pushes this bot's workspace to a private repo. Survives disk loss.</div>
      </div>
      <div>
        ${backupBadge}
        <div style="${_IK_GH_NOTE}">
          <a href="#" onclick="_ikNavToBackup();return false" style="color:var(--accent);text-decoration:none">View backup status →</a>
        </div>
      </div>
      <div>
        <button class="btn btn-ghost btn-sm"
                onclick='_ikGithubBackupSetup(${backupBotsJson})'>
          ${backupCta}
        </button>
      </div>
    </div>
    <div style="${_IK_GH_ROW_LAST}">
      <div>
        <div style="${_IK_GH_LABEL}"><strong>General code access</strong></div>
        <div style="${_IK_GH_NOTE}">${generalNote}</div>
      </div>
      <div>${generalBadge}</div>
      <div>
        <button class="btn btn-ghost btn-sm"
                onclick="_inboxOpenIntakeTokenModal('${generalCtaSlot}')">
          ${generalCta}
        </button>
      </div>
    </div>
  `;
}

function _ikGithubBackupSetup(bots) {
  // Empty array means there are no bots at all — fall through to the
  // onboard modal with no preselection so the wizard explains.
  const targets = Array.isArray(bots) && bots.length ? bots : [];
  openOnboardModal('github', targets);
}

// Phase 4e: deep-link from the Credentials backup row to the Backup page.
// Distinct from _ikGithubBackupSetup (which opens the GitHub credential
// wizard) — this routes the operator to the operational Backup → Cloud
// surface where they manage repo URLs, see push status, and trigger
// manual backups. The 80ms setTimeout lets nav()'s page swap settle
// before subTab() looks for the cloud subtab inside #page-backup.
function _ikNavToBackup() {
  nav(document.querySelector('[data-page=backup]'));
  setTimeout(() => {
    const cloud = document.querySelector('#page-backup .subtab[data-subtab="cloud"]');
    if (cloud) cloud.click();
  }, 80);
}

// ── Embedding-providers panel (read-only here; edit lives in AI Optimization)
async function ikLoadEmbeddings() {
  const el = document.getElementById('ik-embeddings-content');
  if (!el || !_ikBot) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  try {
    const r = await api('GET', `/api/admin/embedding-config/${encodeURIComponent(_ikBot)}`);
    if (!r || r.error) {
      el.innerHTML = `<div style="color:var(--red);font-size:0.85rem;padding:12px 0">⚫ ${escHtml(r && r.error || 'Failed to load embedding config')}</div>`;
      return;
    }
    ikRenderEmbeddings(r);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);font-size:0.85rem;padding:12px 0">⚫ ${escHtml(e.message)}</div>`;
  }
}

// Map embedding-provider IDs → the credential id used by the Add Key modal /
// auth-profiles. Most match 1:1; gemini stores credentials under "google"
// (the chat-provider id) per the credential_keys aliases in embeddings.py.
// Providers not in this map have no Add Key path (e.g., bedrock uses AWS
// credential chain; local/ollama need no key; copilot uses a subscription).
const _EMBEDDING_TO_CREDENTIAL_PROVIDER = {
  gemini:  'google',
  openai:  'openai',
  voyage:  'voyage',
  mistral: 'mistral',
};

function ikRenderEmbeddings(d) {
  const el = document.getElementById('ik-embeddings-content');
  if (!el) return;

  const chain = d.resolved_chain || [];
  const providers = d.providers || [];
  const warning = d.warning;
  const perBot = d.per_bot_chain;

  // Provider lookup for chain rendering.
  const provById = {};
  providers.forEach(p => { provById[p.id] = p; });

  function capBadge(cap) {
    const map = {
      multimodal:        ['Multimodal', 'var(--blue)'],
      retrieval_tuned:   ['Retrieval-tuned', 'var(--green)'],
      subscription:      ['Subscription', 'var(--text2)'],
      local:             ['Local', 'var(--text3)'],
      offline:           ['Offline', 'var(--text3)'],
    };
    const [label, color] = map[cap] || [cap, 'var(--text3)'];
    return `<span style="font-size:0.65rem;padding:1px 5px;border-radius:3px;border:1px solid ${color};color:${color};margin-left:4px">${escHtml(label)}</span>`;
  }

  // Active chain: provider → fallback (→ next) etc.
  const chainRow = chain.length
    ? chain.map((pid, i) => {
        const p = provById[pid] || {id: pid, label: pid};
        const isPrimary = i === 0;
        return `<span style="display:inline-flex;align-items:center;gap:4px">
          <span style="font-weight:${isPrimary ? 600 : 400};color:${isPrimary ? 'var(--text)' : 'var(--text2)'}">${escHtml(p.label)}</span>
          ${isPrimary ? '<span style="font-size:0.65rem;color:var(--text3)">(primary)</span>' : ''}
        </span>${i < chain.length - 1 ? '<span style="color:var(--text3);margin:0 6px">→</span>' : ''}`;
      }).join('')
    : '<span style="color:var(--text3)">none</span>';

  const warningHtml = warning
    ? `<div style="background:var(--bg3);border-left:3px solid var(--yellow);padding:8px 12px;margin-bottom:12px;border-radius:3px;font-size:0.82rem;color:var(--text2)">
         ⚠ ${escHtml(warning)}
       </div>`
    : '';

  // Provider table: configured first, then unconfigured (grayed).
  const rows = providers.map(p => {
    const inChain = chain.indexOf(p.id);
    const status = !p.is_configured
      ? '<span style="color:var(--text3);font-size:0.72rem">no credentials</span>'
      : inChain === 0
        ? '<span style="color:var(--green);font-size:0.72rem">● primary</span>'
        : inChain > 0
          ? `<span style="color:var(--blue);font-size:0.72rem">● fallback ${inChain}</span>`
          : '<span style="color:var(--text3);font-size:0.72rem">available</span>';
    const caps = (p.capabilities || []).map(capBadge).join('');

    // Inline action: + Add key for unconfigured providers that need an API
    // key, but only when the credential is one the Add Key modal supports.
    // Set as primary for credentialed cloud providers that aren't already
    // chain[0]. Limited to needs_api_key=true so we don't surface the action
    // for bedrock / copilot / ollama / local — those auth differently (AWS
    // chain, Copilot subscription, no key) and routing memory_search through
    // them via a one-click button would fail silently. Operators reorder
    // those via AI Optimization → Embedding Providers.
    //
    // Rotate is exclusively a Credentials-tab concern (single canonical edit
    // path). On rows where we DO have a key, we surface a small pointer link
    // back to Credentials so operators don't hunt for it.
    let action = '';
    const credProvider = _EMBEDDING_TO_CREDENTIAL_PROVIDER[p.id];
    const noKeyHint = !p.needs_api_key
      ? (p.id === 'bedrock' ? 'AWS credential chain'
       : p.id === 'github-copilot' ? 'Copilot subscription'
       : p.id === 'ollama' ? 'local — no key'
       : p.id === 'local' ? 'offline GGUF'
       : 'no key required')
      : '';
    if (!p.is_configured && p.needs_api_key && credProvider) {
      action = `<button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 8px"
        onclick="ikJumpToCredentials('${escHtml(credProvider)}')" title="Add this provider's key in the Credentials tab">+ Add key in Credentials</button>`;
    } else if (p.is_configured && p.needs_api_key && credProvider) {
      const primaryBtn = inChain !== 0
        ? `<button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 8px;margin-right:4px"
            onclick="ikSetEmbeddingPrimary('${escHtml(p.id)}','${escHtml(p.label)}')">Set as primary</button>`
        : '';
      action = `${primaryBtn}<button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 8px"
        onclick="ikJumpToCredentials('${escHtml(credProvider)}')" title="Rotate this provider's key in the Credentials tab">↻ Rotate in Credentials</button>`;
    } else if (p.is_configured && p.needs_api_key && inChain !== 0) {
      // Configured but we don't have a credential-tab mapping (rare); allow
      // primary-set only.
      action = `<button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 8px"
        onclick="ikSetEmbeddingPrimary('${escHtml(p.id)}','${escHtml(p.label)}')">Set as primary</button>`;
    } else if (noKeyHint) {
      action = `<span style="font-size:0.7rem;color:var(--text3)">${escHtml(noKeyHint)}</span>`;
    }

    return `<tr style="border-bottom:1px solid var(--border);font-size:0.82rem;${p.is_configured ? '' : 'opacity:0.65'}">
      <td style="padding:7px 8px;font-weight:600">${escHtml(p.label)}${caps}</td>
      <td style="padding:7px 8px;font-family:var(--mono);font-size:0.72rem;color:var(--text2)">${escHtml(p.model || p.default_model || '—')}</td>
      <td style="padding:7px 8px">${status}</td>
      <td style="padding:7px 8px;color:var(--text3);font-size:0.72rem">${escHtml(p.notes || '')}</td>
      <td style="padding:7px 8px;text-align:right">${action}</td>
    </tr>`;
  }).join('');

  const sourceLine = perBot
    ? `<span style="color:var(--text3);font-size:0.72rem">per-bot override</span>`
    : `<span style="color:var(--text3);font-size:0.72rem">pod default</span>`;

  // Snapshot for evo's context-pack. Surface enough embedding-config
  // state — active chain, primary provider, configured-but-unused list,
  // missing-credential list — so evo can answer "is the embeddings
  // index healthy?" / "what's the embedding spend look like?" without
  // a re-fetch. Per-bot vs pod-default flag matters for "did I override
  // this on this bot?" questions.
  try {
    _ikWriteContextSnapshot({
      bot: _ikBot,
      embeddings: {
        chain: chain.slice(0, 6),
        primary: chain.length ? chain[0] : null,
        fallback: chain.length > 1 ? chain[1] : null,
        per_bot_override: !!perBot,
        warning: warning || null,
        configured_count: providers.filter(p => p.is_configured).length,
        configured_providers: providers
          .filter(p => p.is_configured).map(p => p.id).slice(0, 8),
        missing_credentials: providers
          .filter(p => !p.is_configured && p.needs_api_key).map(p => p.id).slice(0, 8),
      },
    });
  } catch (_) {}

  el.innerHTML = `
    ${warningHtml}
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:0.72rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.04em">Active chain</div>
      <div style="font-size:0.85rem">${chainRow}</div>
      <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
        ${sourceLine}
        <button class="btn btn-ghost btn-sm" style="font-size:0.72rem;padding:2px 10px"
          onclick="ikJumpToEmbeddingChainEditor()" title="Edit the full chain (reorder, add fallbacks, clear override)">
          Configure chain →
        </button>
      </div>
    </div>
    <div class="resp-table-wrap"><table class="resp-table" style="width:100%;border-collapse:collapse">
      <thead><tr style="font-size:0.72rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.04em;border-bottom:1px solid var(--border)">
        <th style="text-align:left;padding:6px 8px">Provider</th>
        <th style="text-align:left;padding:6px 8px">Model</th>
        <th style="text-align:left;padding:6px 8px">Status</th>
        <th style="text-align:left;padding:6px 8px">Notes</th>
        <th style="text-align:right;padding:6px 8px;width:1%;white-space:nowrap"></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  _respTableLabelize(el);
}

// "Set as primary" fast-path — establishes a per-bot chain of [<provider>,
// "local"] without rewriting any existing override the operator may have
// configured. For richer control (preserve fallback order, add multiple
// fallbacks, clear override), Configure chain → goes to AI Optimization.
async function ikSetEmbeddingPrimary(providerId, providerLabel) {
  if (!_ikBot) return;
  if (!await confirmModal(
    `Set ${providerLabel} as the primary embedding provider for ${_ikBot}?\n\n`
    + `This writes a per-bot override of [${providerId}, local]. Use AI `
    + `Optimization → Embedding Providers to configure a richer chain.`
  )) return;
  const r = await api('PUT', `/api/admin/embedding-config/${encodeURIComponent(_ikBot)}`,
    { chain: [providerId, 'local'] });
  if (r.error) {
    toast(`Failed: ${r.error}`, 'err');
    return;
  }
  toast(`${providerLabel} set as primary`, 'ok');
  await ikLoadEmbeddings();
}

// Switch to the Credentials sub-tab. Optional providerHint scrolls to and
// briefly highlights the matching <tr data-provider="..."> row so the
// operator can locate it after the jump. The canonical rotate/add flow
// lives on the Credentials tab — this is just a pointer from Plugins /
// Embeddings to that flow.
function ikJumpToCredentials(providerHint) {
  const tabEl = document.querySelector('#page-integrations-keys .subtab[onclick*="\'credentials\'"]');
  if (tabEl) subTab(tabEl, 'integrations-keys', 'credentials');
  if (providerHint) {
    setTimeout(() => {
      const target = document.querySelector('#ik-keys-table tr[data-provider="' + CSS.escape(providerHint.toLowerCase()) + '"]');
      if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'center'});
        target.style.transition = 'background-color 1.5s';
        const prev = target.style.backgroundColor;
        target.style.backgroundColor = 'var(--bg3)';
        setTimeout(() => { target.style.backgroundColor = prev; }, 1500);
      }
    }, 200);
  }
}

// Jump from the Integrations & Keys read-only panel into the AI Optimization
// chain editor, with the same bot pre-selected. Two setTimeouts because
// nav() triggers an async page load and aiSwitchBot must run after the page
// is rendered; the inner timeout gives _aiRenderBotData time to populate
// the embedding panel before we scroll to it.
function ikJumpToEmbeddingChainEditor() {
  const navEl = document.querySelector('[data-page=ai-optimization]');
  if (navEl) nav(navEl);
  setTimeout(() => {
    if (typeof aiSwitchBot === 'function' && _ikBot) {
      try { aiSwitchBot(_ikBot); } catch (e) { /* non-fatal */ }
    }
    setTimeout(() => {
      const panel = document.getElementById('ai-embedding-panel');
      if (panel) panel.scrollIntoView({behavior: 'smooth', block: 'start'});
    }, 250);
  }, 100);
}
