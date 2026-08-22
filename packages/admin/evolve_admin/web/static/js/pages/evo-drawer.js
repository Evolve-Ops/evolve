// ════════════════════════════════════════════════════════════════════════
// Page: Evo-Drawer (right-side slide-in chat available from every page)
//
// Despite the pages/ location, this is conceptually a widget — it's the
// chat surface available on every nav-item via the top-right ✦ FAB,
// not a standalone page. Hosted in pages/ so the SPA_SCRIPTS auto-
// discovery (Phase 4a) picks it up; widgets/ stays for the four
// trailing-tag IIFE widgets (pwa-install / pwa-tunnel / chat-uploads /
// terminal) that load at body-end and don't go through SPA_SCRIPTS.
//
// Three responsibilities (verbatim from the original section header):
//
//   1. Drawer UX  — header + scrolling thread + prompt pinned to bottom.
//                   Same Home-page pattern.
//   2. Per-page threads — each sidebar page has an independent
//      conversation under `evolve_chat_<page_id>`. Navigating away does
//      not lose the thread; navigating back resumes it.
//   3. Page-context-packs — _EVO_CONTEXT_PACKS registers a per-page
//      builder that returns a JSON-shaped snapshot of what the operator
//      is currently looking at. Sent to the server with each chat turn
//      so evo can answer "what's the worst alert here" without making
//      the operator paste anything.
//
// Storage shape (per-page):
//   localStorage["evolve_chat_<page>"]          = current thread
//   localStorage["evolve_chat_<page>_archive"]  = archived prior threads
//   localStorage["evolve_chat_<page>_oc_salt"]  = OC-session salt (↺ rotation)
//
// Caps:
//   EVO_DRAWER_MAX_TURNS (50)     — live-thread softcap; overflow moves
//                                   to a trimmed-archive entry recoverable
//                                   via ↶
//   EVO_DRAWER_MAX_ARCHIVES (3)   — keep last 3 per page
//   EVO_DRAWER_PENDING_TIMEOUT_MS — fetch timeout for one chat send
//   EVO_DRAWER_SLOW_INDICATOR_MS  — when to swap the spinner for the
//                                   live-elapsed indicator
//
// SSE activity helpers (loaded first so the main cluster's send loop
// can call them at parse time):
//   _evoDrawerFormatActivity(ev)  — formats one SSE event into a short
//                                   status string
//   _evoSetPendingActivity(...)   — updates a pending bubble's body
//
// Main cluster (loaded second, in original order):
//   const tables: EVO_DRAWER_MAX_TURNS / MAX_ARCHIVES / TIMEOUT_MS /
//                 SLOW_INDICATOR_MS,
//                 _EVO_PAGE_PROMPTS (per-page suggested prompts),
//                 _EVO_CONTEXT_PACKS (per-page snapshot builders)
//   functions: drawer open/close, history panel, archive restore /
//              discard, header counter sync, authority selector,
//              page-change hook, FAB badge sync, keydown / send /
//              run-suggested
//
// Cross-file linkages preserved via runtime free-variable lookup:
//   - api(), apiStream(), _parseSseBlock() — core/api.js
//   - toast(), escHtml(), attrJsLiteral() — core/dom-utils.js
//   - nav() / window.nav — core/router.js (the page-change hook is
//                          called from inside nav())
//   - _evoChatPendingIndicator — home.js (shared chat helper; both
//                                surfaces use it)
//   - botLabel() — still inline in main script (a future utilities
//                  extract will fold it into core/)
// ════════════════════════════════════════════════════════════════════════

// ── SSE activity helpers (originally line 8060 — formerly in the
// pre-Phase-2 Utilities block, then mistakenly left there when api +
// dom-utils moved to core/. Re-united here with the rest of evo-drawer.)

function _evoDrawerFormatActivity(ev) {
  if (!ev || !ev.data) return null;
  if (ev.event === 'tool_call') {
    return `looking up ${ev.data.tool || 'a tool'}…`;
  }
  if (ev.event === 'tool_result') {
    const ok = ev.data.outcome === 'ok';
    return `${ok ? '✓' : '⚠'} ${ev.data.tool || 'tool'} ${ok ? 'done' : 'error'}`;
  }
  if (ev.event === 'assistant_text') {
    // Interim text — truncate to keep the bubble small.
    const t = String(ev.data.text || '').slice(0, 80).replace(/\s+/g, ' ').trim();
    return t ? `evo: ${t}${ev.data.text.length > 80 ? '…' : ''}` : null;
  }
  if (ev.event === 'heartbeat') {
    const s = ev.data.elapsed_s;
    return Number.isFinite(s) ? `…still working (${Math.round(s)}s)…` : null;
  }
  return null;
}

// Update the pending bubble's body text with the latest activity.
// Mirrors _evoChatPendingIndicator's DOM access pattern — by id so we
// don't crash if the operator switched threads mid-stream.
function _evoSetPendingActivity(pendingId, text) {
  if (!pendingId || !text) return;
  const bubble = document.getElementById(pendingId);
  if (!bubble) return;
  const bodyEl = bubble.querySelector('.home-msg-body');
  if (!bodyEl) return;
  bodyEl.textContent = text;
}



// ── Main cluster (originally lines 34374-36588) ────────────────────


// ══ EVO DRAWER ═══════════════════════════════════════════════════════════════
// Right-side slide-in chat drawer accessible from every page via the
// top-right ✦ FAB. Replaces the old single-shot "help" panel.
//
// Three responsibilities, all backed by the same /api/home/chat endpoint
// the Home page uses (per the "per-bot LLM inference, never centralized"
// memory — calls still go through the primary bot's Anthropic key):
//
//   1. Drawer UX — header + scrolling thread + prompt pinned to bottom.
//      Same Home-page pattern: prompt is always on screen, thread scrolls
//      above it.
//   2. Per-page threads — each sidebar page has an independent
//      conversation under `evolve_chat_<page_id>`. Navigating away does
//      not lose the thread; navigating back resumes it.
//   3. Page-context-packs — _EVO_CONTEXT_PACKS registers a per-page
//      builder that returns a JSON-shaped snapshot of what the operator
//      is currently looking at. Sent to the server with each chat turn
//      so evo can answer "what's the worst alert here" without making
//      the operator paste anything. v1 stubs `maintenance` (Alerts);
//      other pages get the generic page-label-only pack.
//
// Storage shape (per-page):
//   localStorage["evolve_chat_<page>"]          = current thread, list of
//                                                  {role: 'user'|'evo', text, ts, ...}
//   localStorage["evolve_chat_<page>_archive"]  = list of archived prior
//                                                  threads, oldest first.
//                                                  Each archive entry is
//                                                  {archived_at, turns[]}.
//
// Caps:
//   EVO_DRAWER_MAX_TURNS (50)  — live thread softcap. When exceeded, the
//                                oldest dropped turns are NOT lost — they
//                                move into a "trimmed" archive entry so the
//                                operator can recover them via ↶. (Prior
//                                behavior silently dropped from the head.)
//   EVO_DRAWER_MAX_ARCHIVES (3) — keep last 3 archives per page. Older
//                                archives are evicted on the next archive.
//                                Combined storage budget per page is
//                                roughly 4× a 50-turn thread; with ~20
//                                pages and ~1KB/turn we sit around 4MB
//                                worst case — well under the 5–10MB
//                                localStorage quota every modern browser
//                                gives us per origin.

const EVO_DRAWER_MAX_TURNS = 50;
const EVO_DRAWER_MAX_ARCHIVES = 3;
// If a chat send doesn't get a response within this window, abort the
// fetch and flip the pending placeholder into a 'took too long —
// retry?' error so the operator isn't stuck staring at a spinner
// forever. 5 minutes covers long multi-tool turns; matches
// HOME_CHAT_PENDING_TIMEOUT_MS so the home composer + drawer behave
// identically (both call /api/home/chat).
const EVO_DRAWER_PENDING_TIMEOUT_MS = 300_000;
// After this many ms with no response, swap the pending bubble's
// '…thinking…' text for a live elapsed-time indicator that updates
// every second — same UX as the home composer.
const EVO_DRAWER_SLOW_INDICATOR_MS = 10_000;
function _evoDrawerKey(page_id) { return `evolve_chat_${page_id || 'general'}`; }
function _evoDrawerArchiveKey(page_id) { return `evolve_chat_${page_id || 'general'}_archive`; }
// OC session id for this page's drawer thread. Per-page-derived by
// default (matches what derive_session_id(page_id) produces server-
// side), so an admin-server with no body-supplied session_id still
// resolves to the same session. The ↺ archive path bumps a salt
// stored under this key so the NEXT send rotates to a fresh OC
// session — without rotation, OC keeps remembering a thread the
// operator already cleared. See _evoDrawerOcSessionId +
// _evoDrawerRotateOcSession.
function _evoDrawerOcSaltKey(page_id) { return `evolve_chat_${page_id || 'general'}_oc_salt`; }

function _evoDrawerCurrentPage() {
  return document.querySelector('.nav-item.active')?.dataset?.page || 'general';
}

function _evoDrawerPageLabel(page_id) {
  const nav = document.querySelector(`.nav-item[data-page="${page_id}"]`);
  const raw = nav?.textContent?.trim() || page_id || 'general';
  // Strip the leading icon glyph ("✦ Chat", "◈ Dashboard", "⚠ Alerts", etc.).
  return raw.replace(/^[^\w\s]+\s*/, '').trim() || page_id || 'general';
}

// Resolve the currently-active subtab for a page, or null when the page
// has no subtab system (or none is active). subTab() persists the
// chosen subtab in ``localStorage.evolve_subtab_<group>`` and toggles
// the ``.subtab.active`` class within the page; we prefer the live DOM
// check (handles initial load) and fall back to localStorage.
function _evoDrawerCurrentSubtab(page_id) {
  if (!page_id) return null;
  const pageEl = document.getElementById(`page-${page_id}`);
  if (!pageEl) return null;
  // The subtab row's parent contains both the .subtab buttons and the
  // .subtab-page panels. We want the active .subtab's identity. The
  // most reliable signal is the active .subtab-page's id, which is
  // ``${group}-${name}`` — easier to parse than reading onclick args.
  const activeSubPage = pageEl.querySelector('.subtab-page.active');
  if (activeSubPage && activeSubPage.id && activeSubPage.id.startsWith(`${page_id}-`)) {
    return activeSubPage.id.slice(page_id.length + 1);
  }
  // Fallback: localStorage. Doesn't fire on the first page load before
  // any subtab has been clicked, but covers tabs that have been
  // navigated to at least once.
  try {
    return localStorage.getItem(`evolve_subtab_${page_id}`) || null;
  } catch (_) {
    return null;
  }
}

// ── Per-page suggested-prompt registry ──────────────────────────────────────
// Inline coaching for the chat widget's empty state. Each entry is either
// a list of suggested-prompt strings or an object with default + per-subtab
// overrides. Strings should be short, action-oriented, and map to
// capabilities evo actually has (per its tool registry). When updating:
//
//   * Honest about what evo can do — don't list a prompt for a capability
//     that hasn't shipped yet (the model will say "I can't do that"
//     which is worse coaching than no example).
//   * Action verbs ("restart [bot]", "apply the cron-caps proposal") tend
//     to teach more than questions; mix in 1-2 questions for variety.
//   * 2-4 examples per page works best — beyond that the placeholder
//     gets crowded and the operator skims past it.
//   * Per-subtab overrides only when the right examples genuinely
//     diverge from the page default; otherwise inherit.
//   * NEVER hardcode the test-pod bot names (team_bot_a, admin_bot, etc.) in
//     examples — use ``[bot]`` as a generic placeholder. Each install
//     has different bot names; showing another install's names in
//     examples looks broken. ``evo`` and ``evolve`` (the product /
//     pod-meta names) are fine to use literally — they're invariants
//     across every install.
//
// People don't read manuals; they read what's right in front of them. This
// registry is one of the load-bearing places where we teach operators how
// to use evo.
const _EVO_PAGE_PROMPTS = {
  'home': [
    "what alerts are firing?",
    "any pending proposals?",
    "how much are we spending this week?",
  ],
  'overview': [
    "what's wrong with [bot]?",
    "restart [bot]'s gateway",
    "redeploy evolve",
  ],
  'reports': [
    "show the latest pod report",
    "which monitor flagged this?",
  ],
  'security': [
    "investigate the open security findings",
    "audit [bot]'s apps",
    "which bots have the worst posture?",
  ],
  'integrations-keys': {
    'default': [
      "are any plugins missing required modules?",
      "what's drifted from the plugin baseline?",
    ],
    'credentials': [
      "which integrations have unhealthy keys?",
      "are any credentials about to expire?",
    ],
    'embeddings': [
      "what's the embedding spend look like?",
      "is the embeddings index healthy?",
    ],
    'mcp': [
      "is the github MCP server healthy?",
      "what MCP servers are configured?",
    ],
    'hooks': [
      "what hooks are enabled on [bot]?",
      "review the active hook policy",
    ],
    'activity': [
      "what changed on plugins this week?",
      "show recent permission changes",
    ],
  },
  'cost': {
    'default': [
      "how much are we spending?",
      "which bot costs most?",
      "show the projected month-end",
    ],
    'sessions': [
      "show the most expensive sessions",
      "any sessions over the context cap?",
    ],
  },
  'maintenance': [
    "pause all bots",
    "resume all bots",
    "redeploy evolve",
    "any infra daemons down?",
  ],
  'apps': {
    'default': [
      "audit [bot]'s morning-brief app",
      "what apps are installed on [bot]?",
    ],
    'apps': [
      "audit a specific app",
      "which apps are stale?",
    ],
    'discovered': [
      "what has the scanner found on [bot]?",
      "promote [app] on [bot]",
    ],
    'gallery': [
      "install morning-brief on [bot]",
      "what apps are available in the gallery?",
    ],
    'activity': [
      "what forge jobs are running?",
      "check the status of job j-xxxxx",
    ],
  },
  'self-improvement': {
    'default': [
      "what proposals are pending?",
      "apply the cron-caps proposal",
      "snooze improvement proposals for a week",
    ],
    'history': [
      "show recently applied proposals",
      "any rolled-back proposals lately?",
    ],
    'generators': [
      "which generators are most active?",
      "throttle the noisy generator",
    ],
    'observations': [
      "what observations has evo collected on [bot]?",
    ],
  },
  'skills': [
    "what skills does [bot] have?",
    "any skills obviated by new tools?",
  ],
  'ai-optimization': [
    "is the classifier accurate?",
    "tune the tier confidence floor",
  ],
  'cost-measures': [
    "what cost optimizations are open?",
    "apply the tier downgrade for [bot]",
  ],
  'settings': {
    'default': [
      "show pod settings",
      "wipe my evo telemetry",
    ],
    'modules': [
      "what modules are enabled?",
    ],
    'pod-config': [
      "edit the pod alert chat target",
      "change the primary bot",
    ],
  },
  'errors': [
    "what errors are firing right now?",
    "show [bot]'s recent errors",
  ],
  'getting-started': [
    "what can evo do?",
    "what should I check first?",
  ],
  'help': [
    "what does the version_drift chip mean?",
    "where do I see my pod's spend?",
    "how do I add a new bot?",
  ],
  'feedback': [
    "the recommendations page is slow to load",
    "I want a way to mute alerts overnight",
    "team_bot_a stopped replying yesterday",
  ],
};

// Resolve the right prompt list for the current page + subtab. Returns
// either a list of strings (caller renders them as `<code>` chips) or
// null when the page has no registered prompts (caller falls back to
// the generic placeholder).
function _evoDrawerPromptsForPage(page_id, subtab_id) {
  const entry = _EVO_PAGE_PROMPTS[page_id];
  if (!entry) return null;
  if (Array.isArray(entry)) return entry;
  if (typeof entry === 'object') {
    if (subtab_id && Array.isArray(entry[subtab_id])) return entry[subtab_id];
    if (Array.isArray(entry.default)) return entry.default;
  }
  return null;
}

// Build the empty-thread placeholder HTML. Caller passes the resolved
// prompts list (or null). When prompts are present we render them as
// `<code>`-wrapped chips so the visual matches the existing
// "alerts/cost" pattern.
function _evoDrawerEmptyPlaceholderHTML(prompts) {
  if (!prompts || prompts.length === 0) {
    return `Ask evo anything about this page or any bot.`;
  }
  const escape = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const chips = prompts.map(p => `<code>${escape(p)}</code>`);
  let phrase;
  if (chips.length === 1) phrase = chips[0];
  else if (chips.length === 2) phrase = chips.join(' or ');
  else phrase = chips.slice(0, -1).join(', ') + ', or ' + chips[chips.length - 1];
  return `Ask evo — try ${phrase}.`;
}

// ── Page-context-pack registry ──────────────────────────────────────────────
// Each entry is a function that returns the page's current summary in the
// shape the Phase 4.1 proxy expects (see evo/proxy.py::format_page_context):
//
//   { headline?: string,
//     counts?:   { key: int, ... },
//     items?:    [ {k:v, ...} | "literal string", ... ],
//     elided_count?: int,
//     tool_pointers?: [ { tool: "...", for: "..." }, ... ] }
//
// The proxy wraps that into <page-context> in the user message and evo's
// AGENTS.md teaches the model to read it as ground truth about the
// operator's current view (with explicit tool pointers for fetch-on-demand
// when the summary elides what the operator is asking about).
//
// Pages without a registered pack still work — the AGENTS.md "page-tool
// map" tells evo which tool to call when the operator asks about something
// page-specific. Bespoke packs are an optimization (one fewer round-trip)
// for pages where the model usually needs the data anyway.
const _EVO_CONTEXT_PACKS = {
  // Feedback page. The operator came here to tell us how to improve
  // Evolve. Two paths are visible on the page: a structured form (the
  // "🐛 File a bug report" / "✨ Request a feature" buttons that open a
  // modal posting to /api/report/github-url) and this chat. Filing
  // happens via the form, not via any tool you call — your job is to
  // help the operator articulate the issue and to fix it in-chat when
  // it's a local/config problem.
  'feedback': () => ({
    headline:
      'Operator is on the Feedback surface — they want to tell us how to ' +
      'make Evolve better, or to flag something broken. You CAN file ' +
      'directly: action.feedback.file_issue posts to GitHub via the ' +
      'admin server (same path the on-page "File GitHub Issue →" button ' +
      'uses; you never see the token). Default workflow when the ' +
      'operator asks you to file: ' +
      '(1) understand what is bothering them — ask one focused ' +
      'clarifying question if the issue is unclear; ' +
      '(2) try to fix it in-chat if it is a local-environment or config ' +
      'issue (use the read-only diagnostic tools); ' +
      '(3) draft a title + body and show them inline so they can correct ' +
      'before you file — filed issues are public-facing under their ' +
      'identity, and once posted they have to close + delete to retract; ' +
      '(4) on operator confirmation, call action.feedback.file_issue ' +
      'and report the resulting issue number + URL. ' +
      'Do NOT punt to the modal button when the operator asked you to ' +
      'file — that is a tool gap the user explicitly flagged on ' +
      '2026-06-07 ("the whole point of putting a chat bot on the evo ' +
      'page was so it could help users conceive, craft, refine, and ' +
      'post issues"). Use the button only as a fallback when action.' +
      'feedback.file_issue returns ok:false with a fallback_url ' +
      '(keystore has no PAT). ' +
      'Never describe a missing tool, a missing spec, an unauthenticated ' +
      'CLI, a missing search key, or any other internal implementation ' +
      'gap to the operator — those are not their problem to solve. ' +
      'Silently route around the gap, ask a focused user-facing ' +
      'question, or call action.evo.log_tool_gap if it matters.',
    tool_pointers: [
      { tool: 'action.feedback.file_issue', for: 'file a GitHub issue directly — DRAFT and confirm with the operator before calling' },
      { tool: 'signal_store_query', for: 'check whether the operator\'s complaint matches a recent monitor signal' },
      { tool: 'pod_state(query="signals.firing")', for: 'see what is currently firing while you investigate' },
      { tool: 'action.evo.log_tool_gap', for: 'record a missing capability for the dev team — do NOT mention this to the operator' },
    ],
  }),

  // Help page. The operator wants to know how something works, what a
  // term means, or where to find a feature. Bias the model AWAY from
  // filing issues — answer the question, walk them to the right page or
  // tool, and only pivot to issue-filing if the operator explicitly
  // describes something broken.
  'help': () => ({
    headline:
      'Operator is on the Help surface — they are asking how something ' +
      'works or where to find a feature. Default behavior: answer the ' +
      'question with explanation. Walk them to the right page + button ' +
      'when a UI affordance exists. Do NOT default to "want me to file ' +
      'an issue?" — that belongs on the Feedback surface. If the ' +
      'question pivots to "this is broken," handle it like the Feedback ' +
      'surface (classify + draft + approve + file) from that point on.',
    tool_pointers: [
      { tool: 'evolve_code_search', for: 'find authoritative answers in the codebase + docs' },
    ],
  }),

  // Chat (the home page). The general-purpose conversation surface.
  // Pack captures the Evo's-report narrative text + the rendered
  // extras (proposal items, suggested actions) so evo can answer
  // follow-ups about its OWN report without re-deriving the data.
  //
  // Why this matters: a 2026-05-20 transcript showed evo asking
  // "which baseline do you want to reset?" right after its own
  // report banner said "personal_bot's permission config has drifted from
  // baseline" — because the report's text was never reaching evo's
  // chat context. The data was technically in the pod-state digest,
  // but evo had no awareness of HOW the report had framed it.
  //
  // We read directly from the DOM (#home-narr-prose, #home-narr-extras)
  // rather than caching to ``window._evoContextSnapshots`` because the
  // report's text-content is what we actually want — already formatted
  // by the renderer, no separate state to keep in sync.
  'home': () => {
    const proseEl = document.getElementById('home-narr-prose');
    const extrasEl = document.getElementById('home-narr-extras');
    const reportText = (proseEl?.textContent || '').trim();
    const extrasText = (extrasEl?.textContent || '').trim();

    // Capture the visible bot rail so evo can answer about a bot the
    // operator just glanced at. Each rail tile has data-bot-id; pull
    // the live chip text and status from the compact head.
    const railTiles = document.querySelectorAll('.home-rail-tile[data-bot-id]');
    const bots = Array.from(railTiles).slice(0, 12).map(t => {
      const botId = t.getAttribute('data-bot-id') || '';
      const status = (t.querySelector('.home-tile-status')?.textContent || '').trim();
      const chips = Array.from(t.querySelectorAll('.tile-chip')).slice(0, 6)
        .map(c => (c.textContent || '').trim())
        .filter(Boolean);
      return { bot_id: botId, status, chips };
    });

    // Active session metadata — useful when the chat-page has multiple
    // browser-side sessions and evo's reply should reference "this
    // session" semantically.
    let session = null;
    try {
      if (typeof _homeGetActiveSession === 'function') {
        const s = _homeGetActiveSession();
        session = {
          title: s.title || 'New conversation',
          turn_count: (s.turns || []).length,
        };
      }
    } catch (_) {}

    return {
      headline: reportText
        ? "Evo's-report banner is rendered above the chat. Use its content as the authoritative framing of what's on the operator's screen right now."
        : "Chat page (home). Report banner not yet rendered.",
      report_text: reportText || null,
      report_extras: extrasText || null,
      session,
      items: bots,
      elided_count: 0,
      tool_pointers: [
        { tool: 'pod_state(query="signals.firing")',
          for: 'underlying firing-signal data the report summarizes (use bot_id filter when the operator asks about one bot)' },
        { tool: 'pod_state(query="proposals.pending")',
          for: 'underlying pending-proposal data the report extras list — useful when the operator says "those proposals" or "the cron-caps one"' },
        { tool: 'pod_state(query="bots", bot_id=...)',
          for: 'per-bot tile detail when the operator references a rail tile' },
      ],
    };
  },

  // Dashboard / Overview. The first surface the operator sees; shows
  // the bot tiles with health chips, status, and version. Pack captures
  // each bot's chip list + status so evo can answer "what does this
  // chip mean?" / "which bots are unhealthy?" directly. Closes the
  // 2026-05-19 case where the operator asked about a "scan needed"
  // pill on evo's tile and the model had no idea the chip existed.
  'overview': () => {
    const snap = (window._evoContextSnapshots || {}).overview || {};
    const bots = snap.bots || [];
    // Highlight the bots that have any chip firing — usually the
    // operator's concern when chatting from the Dashboard.
    const flagged = bots.filter(b => (b.chips || []).length > 0);
    return {
      headline: bots.length
        ? `${bots.length} bot(s) on the Dashboard. ${flagged.length} have health chip(s) firing.`
        : 'No bots loaded yet on the Dashboard.',
      counts: {
        total: bots.length,
        flagged: flagged.length,
        primary: snap.primary || 'unknown',
      },
      items: bots.map(b => ({
        bot_id: b.bot_id,
        role: b.role,
        status: b.status,
        chips: (b.chips || []).map(c => `${c.id}:${c.severity}`).join(',') || 'none',
        evolve_synced: b.evolve_synced,
        cost_7d_usd: b.cost_usd_7d,
        apps_used_7d: b.apps_used_7d,
      })),
      elided_count: 0,
      available_actions: [
        { label: 'Redeploy / Restart / Remove',
          description: 'inline buttons on each bot tile — apply to that specific bot' },
        { label: '+ Add Bot',
          description: 'top-right; opens the new-bot wizard' },
      ],
      tool_pointers: [
        { tool: 'pod_state(query="bots", bot_id=...)',
          for: 'full per-bot data INCLUDING tile_chips — call this whenever the operator asks about a tile, a chip, or "why is X showing Y"' },
        { tool: 'pod_state(query="host")',
          for: 'host CPU / memory / disk if the operator asks about pod-wide health' },
      ],
    };
  },

  // Security page. The audit landing surface — per-bot score + critical/
  // warn/info advisory counts + top findings. Pack mirrors what the
  // operator sees in the per-bot cards. Closes the 2026-05-19 case
  // where evo confused team_bot_a's plugin advisories with team_bot_b's plugin
  // config drift (different bots, different findings).
  'security': () => {
    const snap = (window._evoContextSnapshots || {}).security || {};
    const perBot = snap.per_bot || [];
    const backupDrift = snap.backup_drift || [];
    const activeSubtab = snap.active_subtab || null;

    const headline = (() => {
      if (activeSubtab === 'backups' && backupDrift.length) {
        return (
          `Backup → Cloud tab. ${backupDrift.length} bot(s) have ` +
          `config drift (live openclaw.json differs from backup baseline). ` +
          `Each row has an "Accept as baseline" button right there.`
        );
      }
      if (activeSubtab === 'backups') {
        return 'Backup → Cloud tab. No config drift detected.';
      }
      if (perBot.length) {
        return `Security audit cached at ${new Date((snap.cached_at || 0) * 1000).toISOString()}. ${perBot.length} bot(s) audited.`;
      }
      return 'Security audit data not yet loaded (or no bots audited yet).';
    })();

    return {
      headline,
      active_subtab: activeSubtab,
      counts: perBot.reduce((acc, b) => {
        acc.critical += b.critical || 0;
        acc.warn += b.warn || 0;
        acc.info += b.info || 0;
        return acc;
      }, { critical: 0, warn: 0, info: 0, drifted_bots: backupDrift.length }),
      items: perBot.map(b => ({
        bot_id: b.bot_id,
        score: b.score,
        critical: b.critical,
        warn: b.warn,
        info: b.info,
        top_findings: (b.top_findings || []).map(f =>
          `${f.code}/${f.severity}: ${f.title}`).join(' | ') || '(none)',
      })),
      // Backup-drift state. Distinct from audit findings — drift is
      // *baseline-vs-live*, audit is *config-against-policy*. Same
      // page, different mechanisms. See AGENTS.md operations map.
      backup_drift_items: backupDrift.map(b => ({
        bot_id: b.bot_id,
        drifted_keys: b.drifted_keys.join(', '),
        stale_backup: b.stale_backup,
        last_backup_at: b.last_backup_at,
      })),
      elided_count: 0,
      available_actions: [
        { label: 'Run Audit',
          description: 'top-right; re-runs the audit across all bots (~30s)' },
        { label: 'Re-run audit',
          description: 'per-bot button on each audit card; targets that one bot' },
        { label: 'Mute',
          description: 'per-advisory button on the Findings subtab; suppresses an info-tier audit advisory. NOT the same as "Accept as baseline" — see action.security.accept_drift.' },
        { label: 'Accept as baseline',
          description: 'per-bot button on the Backups subtab; commits the live openclaw.json into evolve-backup/ so the next heal pass sees no drift. The action.security.accept_drift tool is the same code path.' },
      ],
      tool_pointers: [
        { tool: 'pod_state(query="audit", bot_id=...)',
          for: 'the FULL per-bot audit detail — call when the operator asks about a specific finding or advisory by name. The summary lists only top 5 findings per bot.' },
        { tool: 'pod_state(query="config_drift")',
          for: 'enumerating which bots have backup-baseline drift — call before iterating action.security.accept_drift for "accept all configs as baseline" requests.' },
        { tool: 'action.security.accept_drift(bot_id=...)',
          for: 'committing one bot\'s live openclaw.json as the new baseline. Iterate this for each drifted bot when the operator says "accept all".' },
        { tool: 'pod_state(query="config_bot", bot_id=...)',
          for: 'the underlying openclaw.json the audit evaluates — useful when the operator wants to know what is/isn\'t configured' },
      ],
    };
  },

  // Plugins page (page_id: integrations-keys). The cross-substrate
  // admin surface: six subtabs covering plugin enable/disable + drift
  // (Plugins), per-bot credentials (Credentials), embedding-provider
  // config (Embeddings), MCP servers (MCP), hook policy + webhook
  // ingress (Hooks), and the operator-UI audit trail (Activity).
  //
  // Operator chatting from this page typically asks one of:
  //   - "which integrations have unhealthy keys?" (Credentials)
  //   - "is the github MCP server healthy?" (MCP)
  //   - "what plugins are drifted on [bot]?" (Plugins)
  //   - "what hooks are enabled on [bot]?" (Hooks)
  //   - "what's the embedding spend look like?" (Embeddings)
  //   - "what changed on plugins this week?" (Activity)
  //
  // Each subtab's loader writes its slice into
  // ``_evoContextSnapshots['integrations-keys']`` via
  // ``_ikWriteContextSnapshot(patch)`` (which spreads ``..._prev`` to
  // preserve siblings). The pack folds them all in and lets the
  // headline emphasize whichever subtab the operator is on.
  'integrations-keys': () => {
    const snap = (window._evoContextSnapshots || {})['integrations-keys'] || {};
    const activeSubtab = snap.active_subtab || 'plugins';
    const bot = snap.bot || null;

    const plugins = snap.plugins || null;
    const credentials = snap.credentials || null;
    const embeddings = snap.embeddings || null;
    const mcp = snap.mcp || null;
    const hooks = snap.hooks || null;
    const activity = snap.activity || null;
    const integrations = snap.integrations || null;

    // Subtab-aware headline. Mirrors the alerts pack pattern from
    // PR #1369 — the operator on Credentials isn't asking about MCP
    // servers, and vice versa. Each branch leads with the data the
    // active subtab surfaces.
    let headline;
    if (activeSubtab === 'credentials' && credentials) {
      const missing = (credentials.missing_pod_invariants || []);
      headline = bot
        ? `Plugins → Credentials on ${bot}: ${credentials.active_count}/${credentials.total} active, ${credentials.unhealthy_count} unhealthy${missing.length ? `, missing pod-invariant(s): ${missing.join(', ')}` : ''}.`
        : 'Plugins → Credentials (no bot selected).';
    } else if (activeSubtab === 'embeddings' && embeddings) {
      headline = bot
        ? `Plugins → Embeddings on ${bot}: chain = ${embeddings.chain.length ? embeddings.chain.join(' → ') : 'none'} (${embeddings.per_bot_override ? 'per-bot override' : 'pod default'}).${embeddings.warning ? ' ⚠ ' + embeddings.warning : ''}`
        : 'Plugins → Embeddings (no bot selected).';
    } else if (activeSubtab === 'mcp' && mcp) {
      headline = bot
        ? `Plugins → MCP Servers on ${bot}: ${mcp.server_count} server(s), ${mcp.unhealthy_count} unhealthy, ${mcp.cve_count} open advisor(ies).`
        : 'Plugins → MCP Servers (no bot selected).';
    } else if (activeSubtab === 'hooks' && hooks) {
      const flags = [];
      if (hooks.webhook_ingress_unexpected) flags.push('unexpected webhook ingress');
      if (hooks.silent_disable_count) flags.push(`${hooks.silent_disable_count} silent-disable`);
      if (hooks.untrusted_injection_count) flags.push(`${hooks.untrusted_injection_count} untrusted-injection`);
      headline = bot
        ? `Plugins → Hooks on ${bot}: ${hooks.policy_count} plugin policy row(s)${flags.length ? '; flags: ' + flags.join(', ') : '; no policy flags'}.`
        : 'Plugins → Hooks (no bot selected).';
    } else if (activeSubtab === 'activity' && activity) {
      headline = bot
        ? `Plugins → Activity on ${bot}: ${activity.count} recent operator-UI change(s) (of ${activity.total} total).`
        : 'Plugins → Activity (no bot selected).';
    } else if (plugins) {
      // v2: only "missing_required" survives as a baseline-driven drift
      // signal. missing_expected and unexpected_enabled were retired with
      // the v1 per-bot expected/permitted sets.
      const drift = (plugins.missing_required || []).length;
      headline = bot
        ? `Plugins → Plugins on ${bot}: ${plugins.enabled_count} enabled, ${plugins.disabled_count} disabled, ${drift} missing-required.`
        : 'Plugins page (no bot selected — first bot loads when the page settles).';
    } else {
      headline = bot
        ? `Plugins page on ${bot} (subtab: ${activeSubtab}). Subtab data not yet loaded.`
        : 'Plugins page (no bot selected).';
    }

    // Compact roll-up so evo can answer "which subtab has the most
    // issues?" without checking each one. We surface only the
    // highest-signal items per subtab here; richer detail lives on
    // each subtab's namespaced block.
    const other_subtab_summaries = {
      plugins: plugins ? {
        enabled: plugins.enabled_count,
        disabled: plugins.disabled_count,
        missing_required: (plugins.missing_required || []).length,
      } : null,
      credentials: credentials ? {
        total: credentials.total,
        active: credentials.active_count,
        missing: credentials.missing_count,
        unhealthy: credentials.unhealthy_count,
        missing_pod_invariants: credentials.missing_pod_invariants || [],
      } : null,
      embeddings: embeddings ? {
        primary: embeddings.primary,
        chain_length: embeddings.chain ? embeddings.chain.length : 0,
        per_bot_override: embeddings.per_bot_override,
        configured_count: embeddings.configured_count,
        missing_credentials: embeddings.missing_credentials || [],
      } : null,
      mcp: mcp ? {
        servers: mcp.server_count,
        unhealthy: mcp.unhealthy_count,
        open_cves: mcp.cve_count,
      } : null,
      hooks: hooks ? {
        ingress_enabled: hooks.webhook_ingress_enabled,
        ingress_unexpected: hooks.webhook_ingress_unexpected,
        silent_disable_count: hooks.silent_disable_count,
        untrusted_injection_count: hooks.untrusted_injection_count,
      } : null,
      activity: activity ? { count: activity.count, total: activity.total } : null,
    };

    // Surface integration-row health across all bots (header chip
    // strip data). Helps evo answer cross-bot questions like "which
    // bots can't reach discord" from anywhere on the page.
    const integrations_per_bot = integrations ? integrations.per_bot : null;

    return {
      headline,
      active_subtab: activeSubtab,
      bot,
      counts: {
        plugins_enabled: plugins ? plugins.enabled_count : null,
        plugins_disabled: plugins ? plugins.disabled_count : null,
        missing_required: plugins ? (plugins.missing_required || []).length : null,
        credentials_total: credentials ? credentials.total : null,
        credentials_unhealthy: credentials ? credentials.unhealthy_count : null,
        mcp_servers: mcp ? mcp.server_count : null,
        mcp_unhealthy: mcp ? mcp.unhealthy_count : null,
        hooks_policies: hooks ? hooks.policy_count : null,
      },
      // Full per-subtab data — evo reads whichever block matches the
      // active subtab; the others are available for cross-subtab
      // questions ("does the MCP gap explain the credential warning?").
      plugins_items: plugins,
      credentials_items: credentials,
      embeddings_items: embeddings,
      mcp_items: mcp,
      hooks_items: hooks,
      activity_items: activity,
      integrations_per_bot,
      other_subtab_summaries,
      available_actions: (() => {
        // Show actions for the active subtab first; everything else is
        // available_actions but lower-priority. Each row's affordances
        // live on its row — these are the page-level buttons + the
        // common row buttons by subtab.
        if (activeSubtab === 'credentials') return [
          { label: '+ Add Key',
            description: 'top-right; opens the Add Key modal to add a new credential' },
          { label: '+ Add Gmail/Calendar',
            description: 'top-right; runs the Google Workspace OAuth wizard for skill install' },
          { label: '↺ Rotate',
            description: 'per-row; rotates an active key (replaces the masked value)' },
          { label: '↩ Rollback',
            description: 'per-row when has_prev; restores the previous key' },
          { label: '▶ Set up',
            description: 'per-row when status=missing on a pod-invariant; opens the right wizard' },
        ];
        if (activeSubtab === 'embeddings') return [
          { label: 'Set as primary',
            description: 'per-row for configured cloud providers not currently chain[0]' },
          { label: '↻ Rotate in Credentials',
            description: 'per-row link; jumps to Credentials subtab with the row highlighted' },
          { label: 'Configure chain →',
            description: 'top-right; opens AI Optimization for full chain editing' },
        ];
        if (activeSubtab === 'mcp') return [
          { label: '+ Install from Catalog',
            description: 'top-right; opens the MCP catalog modal' },
          { label: '↻ Re-scan',
            description: 'top-right; re-runs the MCP monitor against this bot' },
          { label: 'Probes',
            description: 'per-row; opens the recent-probe history modal' },
          { label: 'Remove…',
            description: 'per-row; proposes removing the MCP server (proposal pipeline)' },
        ];
        if (activeSubtab === 'hooks') return [
          { label: 'Edit baseline…',
            description: 'top-right; edits pod-wide hook policy baseline (trusted_prompt_mutators / set_plugin_policy / set_webhook_ingress)' },
          { label: 'Edit…',
            description: 'per-row; edits this plugin\'s typed hook policy (allowConversationAccess / allowPromptInjection)' },
          { label: 'Disable…',
            description: 'on the webhook-ingress panel when enabled; flips hooks.enabled=false' },
        ];
        if (activeSubtab === 'activity') return [
          { label: '↻ Refresh',
            description: 'top-right; re-fetches the operator-UI audit trail for the active bot' },
        ];
        // default: 'plugins' subtab
        return [
          { label: 'Adopt allow list…',
            description: 'top-right; sets the bot\'s plugins.allow list to the baseline-expected set' },
          { label: '↻ Re-scan',
            description: 'top-right; re-runs the plugin monitor against the current bot' },
          { label: 'Enable / Disable',
            description: 'per-row; proposes enabling or disabling a plugin entry (proposal pipeline)' },
          { label: 'Browse skills →',
            description: 'top-right; jumps to the Skills page catalog' },
        ];
      })(),
      tool_pointers: [
        { tool: 'pod_state(query="config_bot", bot_id=...)',
          for: 'the underlying openclaw.json — plugins, mcp.servers, hooks, embedding overrides all live here. Use whenever the operator asks "is X configured" for any of these surfaces.' },
        { tool: 'pod_state(query="config_network")',
          for: 'pod-wide invariants (which integrations every bot is expected to have) and pod_default_github_account — relevant for credentials.missing_pod_invariants explanations.' },
        { tool: 'plugin_action(action="enable", bot_id=..., plugin_name=...)',
          for: 'enabling a plugin entry when the operator says "enable discord on [bot]" / "turn on the github plugin". Creates an EnablePluginEntry proposal. Use this on the Plugins subtab.' },
        { tool: 'plugin_action(action="disable", bot_id=..., plugin_name=...)',
          for: 'disabling a plugin entry. Creates a DisablePluginEntry proposal. Refuses if the plugin is in the baseline required set.' },
        { tool: 'pod_state(query="audit", bot_id=...)',
          for: 'audit findings + advisories on a bot — useful when the operator asks "is this credential gap a security finding?" / "does the MCP setup pass audit?"' },
      ],
    };
  },

  // Usage / Cost page. The spend & activity surface. Pack captures
  // top-level totals plus per-bot breakdown for the currently-selected
  // window so evo can answer "what's our spend?", "is bot X expensive
  // this week?", etc. without re-fetching.
  'cost': () => {
    const snap = (window._evoContextSnapshots || {}).cost || {};
    const perBot = snap.per_bot || [];
    return {
      headline: snap.total_usd != null
        ? `Usage page (window=${snap.window_days || '?'}d, bot filter=${snap.bot_filter || 'all'}). Total: $${snap.total_usd?.toFixed?.(2) ?? snap.total_usd}.`
        : 'Usage page (no data loaded yet).',
      counts: {
        total_usd: snap.total_usd,
        turns: snap.turns,
        sessions: snap.sessions,
      },
      items: perBot.map(b => ({
        bot_id: b.bot_id,
        cost_usd: b.cost_usd,
        turns: b.turns,
      })),
      elided_count: 0,
      available_actions: [
        { label: 'Range selector (1d / 7d / 28d)',
          description: 'top of page; switches the analytics window' },
        { label: 'Bot tabs',
          description: 'top of page; filters to one bot or "all"' },
        { label: 'Unit toggle (Turns / Cost)',
          description: 'switches the chart axis between turn counts and USD' },
      ],
      tool_pointers: [
        { tool: 'pod_state(query="bots")',
          for: 'cost_7d / cost_28d per bot as part of the tile data' },
        { tool: 'pod_state(query="config_network")',
          for: 'pod-level spend caps and tier assignments if the operator asks about budget guardrails' },
      ],
    };
  },

  // Apps page (Capabilities subtab). Lists installed apps for the
  // currently-selected bot. Pack captures the manifest list so evo can
  // answer "what apps does X have?" or "is Y installed?" without a
  // separate fetch.
  'apps': () => {
    const snap = (window._evoContextSnapshots || {}).apps || {};
    const top = snap.top || [];
    const total = snap.total ?? top.length;
    return {
      headline: total
        ? `Apps page (Capabilities subtab) for ${snap.bot || '?'}: ${total} app(s) installed.`
        : 'Apps page (no bot selected or no apps installed).',
      counts: { total: total || 0, bot: snap.bot || null },
      items: top.map(a => ({
        name: a.name,
        kind: a.kind,
        last_used: a.last_used,
        schema_version: a.schema_version,
      })),
      elided_count: Math.max(0, total - top.length),
      available_actions: [
        { label: 'Run Scan',
          description: 'per-bot button; re-inventories the bot\'s app manifests (resolves the scan_needed chip)' },
        { label: 'Install from Gallery',
          description: 'switches to the Gallery subtab; pod-wide app catalog' },
        { label: 'Forge Jobs',
          description: 'subtab; queue of in-progress app builds/edits' },
      ],
      tool_pointers: [
        { tool: 'pod_state(query="bots", bot_id=...)',
          for: 'app counts + scan_needed chip status on each bot' },
      ],
    };
  },

  // Reports page (umbrella: Subscriptions / Alerts / Watchlist). The
  // Alerts subtab is the operator's primary triage surface for firing
  // signals — when the chat drawer opens on this page, the operator
  // is usually asking about an alert they can see on screen.
  //
  // Closes the gap where the operator on Reports → Alerts asks evo
  // "what's the worst alert?" / "snooze that one" and evo has no
  // structured awareness of which signals are visible — only the raw
  // pod_state(query="signals.firing") tool with no notion of what's on the
  // operator's screen right now.
  //
  // Snapshot is written by ``_alLoadLane`` (which fetches the same
  // signal list the page renders) into ``_evoContextSnapshots.reports``.
  // Active inner subtab (firing / history / configure) is tracked so
  // headline framing matches what the operator's looking at.
  'reports': () => {
    const snap = (window._evoContextSnapshots || {}).reports || {};
    const firingTop = snap.firing_top || [];
    const firingCount = snap.firing_count ?? firingTop.length;
    const snoozedTop = snap.snoozed_top || [];
    const snoozedCount = snap.snoozed_count ?? snoozedTop.length;
    const activeSubtab = snap.active_subtab || null;
    const activeInner = snap.active_inner || null;
    const elided = Math.max(0, firingCount - firingTop.length);

    // Bucket firing alerts by producer so the model can quickly tell
    // what KIND of alerts are firing without scanning every item —
    // useful when the operator's question is producer-shaped ("any
    // integration_probe alerts?", "what's pod_report flagging?").
    const by_producer = {};
    const by_severity = {};
    for (const s of firingTop) {
      const p = s.producer || 'unknown';
      by_producer[p] = (by_producer[p] || 0) + 1;
      const sev = s.severity || 'info';
      by_severity[sev] = (by_severity[sev] || 0) + 1;
    }

    // Headline phrases what the operator is currently looking at.
    // The Reports page has three outer sections — only Alerts has
    // the firing list. If the operator is on Subscriptions or
    // Watchlist, the firing count is still useful context but
    // shouldn't dominate the framing.
    let headline;
    if (activeSubtab === 'alerts' || !activeSubtab) {
      if (activeInner === 'history') {
        headline = 'Operator is on Reports → Alerts → History (state-change log of past signal transitions).';
      } else if (activeInner === 'configure') {
        headline = 'Operator is on Reports → Alerts → Configure (pod_report sensitivity thresholds).';
      } else {
        headline = firingCount
          ? `Operator is on Reports → Alerts (Firing) — ${firingCount} alert(s) firing.`
          : 'Operator is on Reports → Alerts (Firing) — no alerts firing right now.';
      }
    } else if (activeSubtab === 'subscriptions') {
      headline = `Operator is on Reports → Subscriptions (chat-delivered digest config). ${firingCount} alert(s) firing in the background.`;
    } else if (activeSubtab === 'watchlist') {
      headline = `Operator is on Reports → Watchlist (sub-threshold candidates the synthesizer is tracking). ${firingCount} alert(s) firing in the background.`;
    } else {
      headline = `Operator is on Reports → ${activeSubtab}. ${firingCount} alert(s) firing.`;
    }

    return {
      headline,
      active_subtab: activeSubtab,
      active_inner: activeInner,
      counts: {
        firing: firingCount,
        snoozed: snoozedCount,
        show_info_tier: !!snap.show_info_tier,
        by_producer,
        by_severity,
      },
      // Each item carries id so evo can call signal_action(action="snooze") /
      // signal_action(action="dismiss") directly when the operator says "the
      // top one" / "that integration_probe alert" — no extra fetch
      // required to resolve a visible alert to its id.
      items: firingTop.map(s => ({
        id: s.id,
        title: s.title || '(untitled)',
        bot: s.bot_id || s.scope || 'pod-wide',
        severity: s.severity || 'info',
        producer: s.producer || 'unknown',
        last_observed_at: s.last_observed_at || null,
      })),
      snoozed_items: snoozedTop.map(s => ({
        id: s.id,
        title: s.title || '(untitled)',
        bot: s.bot_id || s.scope || 'pod-wide',
        severity: s.severity || 'info',
        producer: s.producer || 'unknown',
      })),
      elided_count: elided,
      available_actions: [
        { label: 'Snooze 24h / Snooze 7d',
          description: 'per-alert button on each firing row — defers the signal so it stops appearing in firing for the chosen window' },
        { label: 'Mark resolved',
          description: 'per-alert button — flags the underlying condition as cleared (signal returns if the condition re-fires)' },
        { label: 'Dismiss…',
          description: 'per-alert button — terminally closes the signal with an optional verdict (false_positive / bad_inference / not_actionable) that feeds producer-tuning feedback' },
        { label: 'Adjust cap →',
          description: 'inline on budget_hawk signals — opens an editor to retune the per_bot_daily_warn_usd cap directly from the alert' },
        { label: 'Show info-tier signals',
          description: 'checkbox below the list — info-severity signals are hidden by default; flip on to surface them' },
        { label: 'Subtab navigation',
          description: 'Subscriptions / Alerts / Watchlist at the top of the page; within Alerts, inner subtabs Firing / History / Configure' },
      ],
      tool_pointers: [
        { tool: 'pod_state(query="signals.firing")',
          for: 'the FULL firing-alerts list when the visible top-12 isn\'t enough. Filter by producer / bot_id / scope when the operator names one. Each Signal has the same id evo can pass to signal_action(action="snooze") / signal_action(action="dismiss").' },
        { tool: 'pod_state(query="signals.history")',
          for: 'state-change log (firing → snoozed / resolved / dismissed transitions). Use when the operator asks about something not currently firing ("what happened to that disk-full alert?") or when on the History inner subtab.' },
        { tool: 'signal_action(action="snooze")',
          for: 'snoozing a specific alert yourself — operator says "snooze that one for a week" / "shut up the budget alerts for 24h". Default duration is 24h; pass duration_iso8601 (e.g. PT24H, P7D) to override. You can do this directly — no need to ask the operator to click the row button.' },
        { tool: 'signal_action(action="resolve")',
          for: 'marking a specific alert resolved — operator says "that\'s fixed now" / "mark resolved". The signal can re-open if the underlying condition fires again (sweep_resolve / observe re-entry).' },
        { tool: 'signal_action(action="dismiss")',
          for: 'dismissing a specific alert yourself with an optional verdict. Use when the operator says "that\'s a false positive" / "dismiss it" / "not actionable" — the verdict feeds the producer-tuning feedback log.' },
        { tool: 'pod_state(query="proposals.pending")',
          for: 'the Watchlist outer subtab contents (sub-threshold proposals the synthesizer is tracking) — use when the operator asks about that section, not the Alerts section.' },
      ],
    };
  },

  // Recommendations page. Each proposal row has inline "Take this on" /
  // "Snooze 1w" / "Dismiss" buttons — those are the actions evo should
  // suggest, NOT fabricated UI nav paths ("Dashboard → Team_bot_b → Config")
  // or messaging commands (`evo fail`). Observed failure mode 2026-05-19.
  'self-improvement': () => {
    const snap = (window._evoContextSnapshots || {})['self-improvement'] || {};
    // The Recommendations page has multiple subtabs; the operator's
    // attention is on the active one. We surface ALL visible
    // proposals (Inbox + In Process) so evo can answer about either,
    // but flag the active subtab so the model knows what the operator
    // is actually pointing at.
    //
    // ``inbox_top`` + ``in_process_top`` are written by
    // ``loadArbiterProposals`` (which loads the page itself). The
    // ``top`` field is back-compat from the old loadBetterStrip
    // shape; new readers should use the typed lists.
    const inboxTop = snap.inbox_top || snap.top || [];
    const inboxTotal = snap.inbox_total ?? snap.total ?? inboxTop.length;
    const inProcessTop = snap.in_process_top || [];
    const inProcessTotal = snap.in_process_total ?? inProcessTop.length;
    const activeSubtab = snap.active_subtab || null;

    const inboxElided = Math.max(0, inboxTotal - inboxTop.length);
    const inProcessElided = Math.max(0, inProcessTotal - inProcessTop.length);

    const projectItem = (r) => ({
      id: r.id || null,
      title: r.title || '(untitled)',
      bot: r.bot || 'pod-wide',
      score: r.score ?? null,
      status: r.status || null,
      action_kind: r.action_kind || null,
      urgency: r.urgency || null,
      generator: r.generator || null,
    });

    // Headline emphasizes the active subtab so evo opens with the
    // right framing — operators on In Process aren't asking about
    // Inbox items, and vice versa.
    let headline;
    if (activeSubtab === 'in-process') {
      headline = inProcessTotal
        ? `Operator is on the In Process tab — ${inProcessTotal} proposal(s) awaiting manual completion.`
        : 'Operator is on the In Process tab — no proposals awaiting manual completion right now.';
    } else if (activeSubtab === 'proposals' || !activeSubtab) {
      headline = inboxTotal
        ? `Operator is on the Inbox tab — ${inboxTotal} pending proposal(s).`
        : 'Operator is on the Inbox tab — no pending proposals right now.';
    } else {
      headline = `Operator is on the ${activeSubtab} tab of Recommendations. Inbox: ${inboxTotal}; In Process: ${inProcessTotal}.`;
    }

    return {
      headline,
      active_subtab: activeSubtab,
      counts: {
        inbox: inboxTotal,
        in_process: inProcessTotal,
      },
      inbox_items: inboxTop.map(projectItem),
      in_process_items: inProcessTop.map(projectItem),
      // Back-compat: legacy ``items`` field mirrors inbox (the
      // pre-this-PR behavior). Drop in a future cleanup once no
      // consumer references it.
      items: inboxTop.map(projectItem),
      elided_count: inboxElided + inProcessElided,
      available_actions: [
        { label: 'Take this on',
          description: 'on Inbox items — applies the proposal (config edit applies automatically; hand-rolled instructions move to In Process for follow-through)' },
        { label: 'Snooze 1w',
          description: 'on Inbox items — defers the proposal a week so the queue stays focused' },
        { label: 'Dismiss',
          description: 'on Inbox or In Process items — declines / closes terminally (no auto-reopen)' },
        { label: 'Mark complete',
          description: 'on In Process items — confirms the operator finished the offline follow-through (e.g. Investigation, WorkflowInstruction)' },
      ],
      tool_pointers: [
        { tool: 'pod_state(query="proposals.pending")',
          for: 'Inbox items (status=pending). Filter by bot_id when the operator names one. Includes id, title, score, urgency.' },
        { tool: 'pod_state(query="proposals.in_process")',
          for: 'In Process items (status=applied + manual-completion kinds). Same shape as .pending. Use when the operator says "the proposal I accepted" / "mark this complete" / asks about something on the In Process tab.' },
        { tool: 'proposal_action(action="apply")',
          for: 'applying a pending proposal (the "Take this on" button equivalent).' },
        { tool: 'proposal_action(action="mark_complete")',
          for: 'closing out an In Process proposal once the operator confirms the offline work is done.' },
        { tool: 'proposal_action(action="snooze")',
          for: 'snoozing a specific pending proposal yourself.' },
      ],
    };
  },

  // NOTE: the legacy ``maintenance: () => { ... }`` builder that lived
  // here pulled from ``snap.firing_top`` / ``snap.firing_count`` and
  // actually surfaced Alerts data — that's covered by the ``reports``
  // builder above. It's been replaced by the new ``'maintenance':``
  // builder further down (which surfaces the Maintenance → Status
  // page's Recent Errors column from ``_gwStatusCache``).

  // Errors page. The deduplicated error-log view — one row per
  // fingerprint with occurrence count, first/last seen, severity,
  // module, and a sample line. When the operator chats from this
  // page they're usually asking about a specific row ("what's that
  // CRITICAL one?", "how many of these are new?") or the page total
  // ("are errors spiking?"). Pack mirrors what's in the table so evo
  // can answer with the SAME view the operator just read — no
  // separate fetch. Closes a May 2026 coverage gap (audit identified
  // Errors as having tool support via pod_state(query="errors") but no page
  // context pack — evo was answering from stale heal-status logs
  // instead of the deduplicated admin-log view).
  'errors': () => {
    const snap = (window._evoContextSnapshots || {}).errors || {};
    const top = (snap.top || []).slice(0, 10);
    const totalSigs = snap.total_signatures ?? top.length;
    const totalOccurrences = snap.total_occurrences ?? null;
    const lastAt = snap.last_error_at || null;
    // Count "new" (unacknowledged) signatures — that's the badge count
    // the operator sees in the sidebar nav.
    const newCount = top.filter(e => e.status === 'new').length;
    return {
      headline: totalSigs
        ? `Errors page: ${totalSigs} unique signature(s)${totalOccurrences != null ? `, ${totalOccurrences} total occurrence(s)` : ''} in the last 7 days. ${newCount} unacknowledged.`
        : 'Errors page: no errors in the last 7 days.',
      counts: {
        unique_signatures: totalSigs,
        total_occurrences: totalOccurrences,
        new_unacknowledged: newCount,
        last_error_at: lastAt,
      },
      items: top.map(e => ({
        signature: e.signature,
        title: e.title,
        severity: e.severity,    // 'alert' (CRITICAL) | 'warn' (ERROR)
        module: e.module,
        count: e.count,
        first_seen: e.first_seen,
        last_seen: e.last_seen,
        status: e.status,        // 'new' | 'acknowledged' | 'submitted' | 'wontfix'
      })),
      elided_count: Math.max(0, totalSigs - top.length),
      available_actions: [
        { label: 'Detail',
          description: 'per-row toggle; shows the full fingerprint + sample log line + GitHub issue link if submitted' },
        { label: 'Ack',
          description: 'per-row button; marks an error as seen locally (clears the nav-badge count)' },
        { label: 'Submit ↗',
          description: 'per-row button; pre-flight searches GitHub for similar open issues, then opens a pre-filled new-issue form' },
      ],
      tool_pointers: [
        { tool: 'pod_state(query="errors")',
          for: 'raw recent error log lines per bot (the heal-status snapshot) — complementary view; use when the operator wants the verbatim error message from a SPECIFIC bot rather than the deduplicated admin-side aggregation shown on this page' },
        { tool: 'pod_state(query="signals.firing")',
          for: 'curated "this matters" observations from monitors (error_reporter, etc.) — use when the operator asks "what alerts is the error layer producing?"' },
      ],
    };
  },

  // Maintenance page. Multiple subtabs (status / system / logs / health /
  // recovery / cron / infrajobs / adminserver / setup / mcp / ocversion).
  // The Status subtab is the data-rich one — it renders a per-bot table
  // with the Recent Errors column populated from /api/gateway/status.
  // That column is what the operator sees when they say "these errors"
  // on this page, so the pack must surface those raw lines or the model
  // will (and historically did) fall back to pod_state(query="signals.firing")
  // and answer from the global Signal store instead.
  //
  // We read directly from window._gwStatusCache — the same cache the
  // Status table renders from, so what we send to the model is exactly
  // what's on screen. For non-Status subtabs we emit just a headline
  // naming the subtab; AGENTS.md's page-tool map covers what to call.
  'maintenance': () => {
    const subtab = (typeof _evoDrawerCurrentSubtab === 'function')
      ? (_evoDrawerCurrentSubtab('maintenance') || 'status')
      : 'status';

    if (subtab === 'status') {
      const cache = window._gwStatusCache || {};
      const entries = Object.entries(cache);
      if (!entries.length) {
        return {
          headline: 'Maintenance → Status: gateway-status table not yet loaded.',
          tool_pointers: [
            { tool: 'pod_state(query="errors")',
              for: 'fetch per-bot recent_errors directly (heal-status snapshot, refreshed every ~5min)' },
          ],
        };
      }
      const items = entries.map(([botId, s]) => {
        const all = Array.isArray(s.recent_errors) ? s.recent_errors : [];
        const tss = Array.isArray(s.recent_errors_ts) ? s.recent_errors_ts : [];
        // Surface up to 5 lines per bot; the rest are reachable via
        // the pod_state(query="errors") tool (pointer below). Each line carries
        // its timestamp when heal parsed one.
        const sample = all.slice(0, 5).map((line, i) => ({
          line: typeof line === 'string' ? line : String(line),
          ts: tss[i] || null,
        }));
        return {
          bot_id: botId,
          running: !!s.gateway_running,
          reachable: !!s.gateway_reachable,
          stale: !!s.stale,
          pid: s.gateway_pid || null,
          status_ts: s.ts || null,
          recent_errors_total: all.length,
          recent_errors_sample: sample,
        };
      });
      const withErrors = items.filter(b => b.recent_errors_total > 0);
      const unreachable = items.filter(b => !b.reachable && !b.stale);
      return {
        headline: items.length === 0
          ? 'Maintenance → Status: no bots configured.'
          : (withErrors.length
              ? `Maintenance → Status: ${withErrors.length} of ${items.length} bots have recent gateway-error lines visible in the Recent Errors column. These are RAW per-bot heal-status entries — distinct from the Alerts/Signals surface.`
              : `Maintenance → Status: all ${items.length} bots show no recent gateway errors. Reachable: ${items.filter(b => b.reachable).length}/${items.length}.`),
        counts: {
          total_bots: items.length,
          bots_with_errors: withErrors.length,
          reachable: items.filter(b => b.reachable).length,
          unreachable: unreachable.length,
        },
        items,
        elided_count: 0,
        tool_pointers: [
          { tool: 'pod_state(query="errors")',
            for: 'full per-bot recent_errors history (beyond the 5-line sample above) + last_error timestamps. Pass bot_id when the operator asks about one bot.' },
          { tool: 'pod_state(query="bots")',
            for: 'broader per-bot runtime state (chips, activity, version) when the operator asks "what is X doing?"' },
        ],
      };
    }

    // Other subtabs: emit a short label-only headline so the model knows
    // which Maintenance surface is active. The page-tool map in AGENTS.md
    // covers which tool to call for each.
    const subtabLabels = {
      'system':       'System (pod version, disk usage, pod-check results)',
      'logs':         'Gateway Logs (raw recent log tail per bot)',
      'health':       'Pod Health (heal.py probe results, healing actions)',
      'recovery':     'Recovery (per-bot reactivate / wipe / breaker reset)',
      'cron':         'Cron Jobs (registered openclaw cron entries pod-wide)',
      'infrajobs':    'Infra Jobs (analyzer / heal / repo-puller / spend-alert daemons)',
      'adminserver':  'Admin Server (this admin-UI process status)',
      'setup':        'Setup Wizard',
      'mcp':          'MCP / Claude Access (auth + MCP server registry)',
      'ocversion':    'OpenClaw Version (deployed vs current)',
    };
    const label = subtabLabels[subtab] || subtab;
    return {
      headline: `Maintenance → ${label}. The Status subtab carries per-bot Recent Errors if the operator asks about errors.`,
      tool_pointers: [
        { tool: 'pod_state(query="errors")',
          for: 'per-bot raw error lines (same data the Status subtab surfaces)' },
        { tool: 'pod_state(query="host")',
          for: 'host-level metrics (disk usage, uptime) visible on the System subtab' },
      ],
    };
  },

  // Settings page. The operator's per-bot configuration surface lives
  // under Pod Config → Bot (which the AGENTS.md page-tool map calls
  // "Bot detail"). When the operator has a bot selected there, the
  // page is showing that bot's archetype/caps/timezone/models/
  // compaction/slack-policy. Snapshot lets evo answer "tell me about
  // team_bot_a's setup" without re-fetching — and tells the model which bot
  // is currently on screen so a follow-up pronoun ("its monthly cap")
  // resolves.
  //
  // Settings has three sibling subtabs after the 2026-06-01
  // restructure: Modules, Pod Config, Bots. The bot-detail snapshot is
  // populated only from the Bots subtab loader.
  'settings': () => {
    const snap = (window._evoContextSnapshots || {}).settings || {};
    const botDetail = snap.bot_detail || null;
    const configSubtab = snap.config_subtab || null;

    const headline = (() => {
      if (configSubtab === 'bot' && botDetail && botDetail.bot_id) {
        return (
          `Operator is on Settings → Bots, looking at ` +
          `${botDetail.bot_id}'s configuration (archetype, caps, ` +
          `model, compaction, slack policy).`
        );
      }
      return 'Operator is on the Settings page.';
    })();

    const summary = { headline };
    if (configSubtab) summary.active_subtab = configSubtab;
    if (botDetail && botDetail.bot_id) {
      // Surface the loaded bot-detail cards in a flat shape so the
      // model can cite specific fields in its reply.
      summary.bot_id = botDetail.bot_id;
      summary.bot_detail = {
        archetype: botDetail.archetype,
        surfacing_cadence: botDetail.surfacing_cadence,
        monthly_cap_usd: botDetail.monthly_cap_usd,
        daily_warn_usd: botDetail.daily_warn_usd,
        daily_hard_usd: botDetail.daily_hard_usd,
        timezone: botDetail.timezone,
        primary_model: botDetail.primary_model,
        fallback_models: botDetail.fallback_models,
        per_agent_models: botDetail.per_agent_models,
        compaction: botDetail.compaction,
        slack_enabled: botDetail.slack_enabled,
        slack_transport_mode: botDetail.slack_transport_mode,
        slack_drift_state: botDetail.slack_drift_state,
        slack_listening_channels: botDetail.slack_listening_channels,
        has_user_profile: botDetail.has_user_profile,
      };
    }
    summary.available_actions = [
      { label: 'Save (Bot setup)',
        description: 'on Bot subtab — persists archetype / cadence / monthly+daily caps / timezone for the selected bot' },
      { label: 'Hand off',
        description: 'on Bot subtab — generates a 7-day onboarding link for the selected bot (CLI equivalent: evolve-admin handover)' },
      { label: 'Display Name',
        description: 'on Bot subtab — renames how the bot identifies itself in chat (openclaw agents set-identity)' },
      { label: 'Initialize / Apply (Slack)',
        description: 'on Bot subtab → Slack Policy card — bootstraps slack-policy.json from openclaw.json, or re-renders openclaw.json from policy when drifted' },
    ];
    summary.tool_pointers = [
      { tool: 'pod_state(query="config_bot", bot_id=...)',
        for: 'the FULL openclaw.json for a bot (sanitized) — call when the operator asks about something the rendered card elides (e.g. specific tool grants, hook config, plugin params). The bot-detail snapshot only surfaces the cards visible on screen.' },
      { tool: 'pod_state(query="bots", bot_id=...)',
        for: 'runtime tile data (chips, status, version, recent activity) for the same bot — pairs with pod_state(query="config_bot") when the operator asks "why is X showing Y?"' },
      { tool: 'pod_state(query="config_network")',
        for: 'pod-wide network.json — alert chat target, tier overrides, members. Use when the operator is on the Pod Config subtab.' },
    ];
    summary.elided_count = 0;
    return summary;
  },
};

function _evoDrawerContextPack() {
  const page_id = _evoDrawerCurrentPage();
  const pack = {
    page_id,
    page_label: _evoDrawerPageLabel(page_id),
    // Surface awareness (Phase 1 of the surface-aware help-style spec —
    // docs/spec-surface-aware-help-style-2026-05-22.md §2.3.1). The
    // model uses ``surface_type`` to gate CLI emission: never on
    // mobile, last-resort on admin_ui/laptop, allowed on Telegram. The
    // surface itself is always ``admin_ui`` here since this pack is
    // built from the browser; Telegram's dispatcher supplies a
    // different value.
    surface: 'admin_ui',
    surface_type: _evoSurfaceType(),
  };
  const builder = _EVO_CONTEXT_PACKS[page_id];
  if (builder) {
    try {
      const summary = builder();
      if (summary && Object.keys(summary).length) pack.summary = summary;
    } catch (_) {}
  }
  return pack;
}

// Two-tier classifier for the operator's viewport. The model reads
// ``surface_type`` from the <page-context> / <session-context> blocks
// and gates CLI emission on it. The conservative bias is intentional:
// when in doubt, classify as ``laptop`` so the failure mode "we
// suppressed a useful CLI" is preferred over "we recommended a CLI on
// a phone the operator can't paste it into".
//
//   ``mobile`` — viewport <= 720px wide OR pointer:coarse (touch-primary).
//   ``laptop`` — otherwise (desktop + iPad-in-desktop-mode + tablets).
//
// Spec §2.3.2.
function _evoSurfaceType() {
  try {
    if (typeof window === 'undefined' || !window.matchMedia) return 'laptop';
    const narrowMQ = window.matchMedia('(max-width: 720px)');
    const coarseMQ = window.matchMedia('(pointer: coarse)');
    if ((narrowMQ && narrowMQ.matches) || (coarseMQ && coarseMQ.matches)) {
      return 'mobile';
    }
  } catch (_) {}
  return 'laptop';
}

// ── Thread storage ─────────────────────────────────────────────────────────

function _evoDrawerLoad(page_id) {
  try {
    const raw = localStorage.getItem(_evoDrawerKey(page_id));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) { return []; }
}

function _evoDrawerSave(page_id, history) {
  try {
    if (history.length > EVO_DRAWER_MAX_TURNS) {
      // We're over the cap. Instead of silently dropping the head turns
      // (the prior behavior), spill the overflow into a "trimmed" archive
      // entry so the operator can recover the older context via ↶. We
      // tag it with kind="trimmed" so the history picker can render it
      // distinct from operator-initiated archives ("you trimmed at turn
      // 51" vs "you cleared this thread").
      const overflow = history.slice(0, history.length - EVO_DRAWER_MAX_TURNS);
      const kept = history.slice(-EVO_DRAWER_MAX_TURNS);
      _evoDrawerArchivePush(page_id, {
        archived_at: new Date().toISOString(),
        kind: 'trimmed',
        turns: overflow,
      });
      localStorage.setItem(_evoDrawerKey(page_id), JSON.stringify(kept));
    } else {
      localStorage.setItem(_evoDrawerKey(page_id), JSON.stringify(history));
    }
  } catch (_) {}
}

// ── Archive store ──────────────────────────────────────────────────────────
// Per-page list of previously-cleared (or auto-trimmed) threads. Kept in
// localStorage under ``evolve_chat_<page>_archive``. The shape is a list
// of {archived_at, kind, turns[]} entries, newest at the tail. Caps at
// EVO_DRAWER_MAX_ARCHIVES — older archives are evicted from the head.

function _evoDrawerArchiveLoad(page_id) {
  try {
    const raw = localStorage.getItem(_evoDrawerArchiveKey(page_id));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) { return []; }
}

function _evoDrawerArchiveSave(page_id, archives) {
  try {
    const trimmed = archives.slice(-EVO_DRAWER_MAX_ARCHIVES);
    localStorage.setItem(
      _evoDrawerArchiveKey(page_id), JSON.stringify(trimmed),
    );
  } catch (_) {}
}

function _evoDrawerArchivePush(page_id, entry) {
  if (!entry || !Array.isArray(entry.turns) || entry.turns.length === 0) {
    // Don't archive empty threads — operator hitting ↺ on a fresh empty
    // drawer shouldn't pollute the history picker with stub entries.
    return;
  }
  const archives = _evoDrawerArchiveLoad(page_id);
  archives.push(entry);
  _evoDrawerArchiveSave(page_id, archives);
}

function _evoDrawerAppend(page_id, msg) {
  // Append a message to a SPECIFIC page's drawer thread by page_id.
  // ``_evoDrawerSend`` captures the originating page_id at the top
  // of the function and uses this helper so the user message + evo
  // reply land on the page they typed in, even if the operator
  // navigated to a different page (or closed the drawer) while the
  // response was in flight.
  //
  // The storage write always happens; the DOM render is gated on
  // page_id being the currently-shown page. Without the gate, an
  // in-flight nav routes the reply bubble to the wrong drawer
  // (same class of bug as #1343 fixed for the Chat page).
  const history = _evoDrawerLoad(page_id);
  history.push(msg);
  _evoDrawerSave(page_id, history);
  if (page_id === _evoDrawerCurrentPage()) {
    _evoDrawerRenderBubble(msg);
  }
  _evoDrawerSyncFabBadge();
  _evoDrawerSyncHeaderCounters();
}

// ── Render ─────────────────────────────────────────────────────────────────

function _evoDrawerRenderBubble(msg) {
  const thread = document.getElementById('evo-drawer-thread');
  if (!thread) return;
  // Drop the empty placeholder once a real turn lands.
  const empty = thread.querySelector('.evo-drawer-empty');
  if (empty) empty.remove();
  const isUser = msg.role === 'user';
  const cls = isUser ? 'home-msg home-msg-user' : 'home-msg home-msg-evo';
  // .home-msg-warn — proxy_warn (empty-reply-with-synthesized-confirm)
  // styling; see spec-surface-aware-help-style-2026-05-22.md §8 +
  // diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md.
  // ``error`` (red) wins when both are set.
  const errCls = msg.error
    ? ' home-msg-error'
    : (msg.warn ? ' home-msg-warn' : '');
  const pendingCls = msg.pending ? ' home-msg-pending' : '';
  const name = isUser ? 'you' : 'evo';
  const avatar = isUser
    ? '<span class="home-evo-avatar" style="background:rgba(124,92,255,0.28)">●</span>'
    : '<span class="home-evo-avatar">✦</span>';
  const tsStr = msg.ts ? ago(msg.ts) : '';
  let suggestedHtml = '';
  if (!isUser && Array.isArray(msg.suggested_actions) && msg.suggested_actions.length) {
    suggestedHtml = `<div class="home-msg-suggested">${
      msg.suggested_actions.map(a => {
        const label = escHtml(a.label || `Run evo ${a.subcommand}`);
        const cmd = escHtml(a.subcommand || '');
        return `<button class="btn btn-ghost btn-sm" onclick="_evoDrawerRunSuggested('${cmd}')">${label}</button>`;
      }).join('')
    }</div>`;
  }
  // Retry affordance — appears on error bubbles that carry the
  // original message text under ``retry_text``. Click drops the text
  // back into the prompt + fires send so the operator doesn't have
  // to re-type. Set by the timeout + catch paths in _evoDrawerSend.
  let retryHtml = '';
  if (!isUser && msg.error && typeof msg.retry_text === 'string' && msg.retry_text) {
    retryHtml = `<div class="home-msg-suggested">` +
      `<button class="btn btn-ghost btn-sm" onclick="_evoDrawerRetrySend(${attrJsLiteral(msg.retry_text)})">↻ Retry</button>` +
      `</div>`;
  }
  // Gateway-down affordance — when the proxy detected that evo's gateway
  // isn't responding, offer a one-click switch to the diagnostic LLM
  // so the operator can still get help during the outage. Passing
  // 'drawer' routes the re-send back through this same drawer composer
  // (not the home-chat composer that PR #2064 wired by default).
  let diagnosticHtml = '';
  if (!isUser && msg.gateway_down) {
    const txt = typeof msg.retry_text === 'string' ? msg.retry_text : '';
    diagnosticHtml = `<div class="home-msg-suggested">` +
      `<button class="btn btn-ghost btn-sm" onclick="_enterDiagnosticMode(${attrJsLiteral(txt)}, 'drawer')">` +
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
    <div class="home-msg-body">${_evoDrawerFormatBody(msg.text)}</div>
    ${suggestedHtml}
    ${retryHtml}
    ${diagnosticHtml}
  `;
  thread.appendChild(bubble);
  thread.scrollTop = thread.scrollHeight;
}

// Retry-send handler — called from the ↻ button on an error/timeout
// bubble. Drops the original message back into the input + fires
// send, exactly as if the operator had retyped it.
function _evoDrawerRetrySend(text) {
  const input = document.getElementById('evo-drawer-input');
  if (!input) return;
  input.value = String(text || '');
  _evoDrawerSend();
}

function _evoDrawerFormatBody(text) {
  // Reuse the Home-chat formatter so markdown/code-fence handling is
  // identical between the Chat page and the per-page drawer.
  if (typeof _homeChatFormatBody === 'function') return _homeChatFormatBody(text);
  return escHtml(String(text || '')).replace(/\n/g, '<br>');
}

function _evoDrawerRenderThread(page_id) {
  const thread = document.getElementById('evo-drawer-thread');
  if (!thread) return;
  thread.innerHTML = '';
  const history = _evoDrawerLoad(page_id);
  if (history.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'evo-drawer-empty';
    // Resolve page + subtab → suggested-prompt chips. Falls back to a
    // generic placeholder when the page has no registered prompts.
    const subtab_id = _evoDrawerCurrentSubtab(page_id);
    const prompts = _evoDrawerPromptsForPage(page_id, subtab_id);
    empty.innerHTML = _evoDrawerEmptyPlaceholderHTML(prompts);
    thread.appendChild(empty);
    return;
  }
  for (const msg of history) _evoDrawerRenderBubble(msg);
}

function _evoDrawerUpdateContextChip() {
  const chip = document.getElementById('evo-drawer-context-chip');
  if (!chip) return;
  const pack = _evoDrawerContextPack();
  const hasState = !!pack.state;
  chip.textContent = pack.page_label;
  chip.classList.toggle('with-state', hasState);
  chip.title = hasState
    ? `evo can see this page's state: ${JSON.stringify(pack.state)}`
    : `evo knows you're on the ${pack.page_label} page`;
}

// ── Open/close + nav swap ──────────────────────────────────────────────────

function _evoDrawerOpen(opts) {
  // opts.prefill — optional string to put in the prompt (used by the
  // legacy in-page "? Help" affordance on Cost Optimization). When
  // prefill is set, this always opens (so the prefill lands somewhere
  // visible). When called without prefill — the FAB-click path — this
  // acts as a TOGGLE: clicking it again with the drawer already open
  // closes it. No need to hunt for the ✕.
  const drawer = document.getElementById('evo-drawer');
  const alreadyOpen = drawer && drawer.classList.contains('open');
  if (alreadyOpen && !(opts && opts.prefill)) {
    _evoDrawerClose();
    return;
  }
  const page_id = _evoDrawerCurrentPage();
  _evoDrawerRenderThread(page_id);
  _evoDrawerUpdateContextChip();
  _evoDrawerSyncHeaderCounters();
  _updateDrawerAuthorityBadge();
  drawer.classList.add('open');
  // Dock the drawer — body class makes .main shrink at ≥1280px (CSS
  // media query handles the breakpoint, so below 1280px the class is
  // set but has no visual effect: drawer stays an overlay).
  document.body.classList.add('evo-drawer-docked');
  const input = document.getElementById('evo-drawer-input');
  if (input) {
    if (opts && opts.prefill) input.value = opts.prefill;
    // Re-measure so a prefilled multi-line string opens at the right
    // height instead of clipping. No-op on an empty input (collapses
    // back to the 1-row baseline).
    autoResizeComposer(input);
    input.focus();
  }
}

function _evoDrawerClose() {
  document.getElementById('evo-drawer').classList.remove('open');
  // Release the dock so .main reclaims its full width as the drawer slides out.
  document.body.classList.remove('evo-drawer-docked');
}

// OC session id for a page's drawer thread. Defaults to whatever
// derive_session_id(page_id) produces on the server (i.e.
// "admin-ui-<page>"), so an admin-server with no body-supplied
// session_id resolves to the same session. A salt is appended when
// the operator has hit ↺ since the last login — bumped by
// _evoDrawerRotateOcSession so the next send starts a fresh OC
// session memory instead of inheriting the cleared conversation.
function _evoDrawerOcSessionId(page_id) {
  const base = `admin-ui-${(page_id || 'general')
    .replace(/\//g, '-').replace(/\s+/g, '-').toLowerCase()}`;
  let salt = '';
  try { salt = localStorage.getItem(_evoDrawerOcSaltKey(page_id)) || ''; }
  catch (_) {}
  return salt ? `${base}-${salt}` : base;
}

// Bump the per-page OC-session salt. The next send will resolve to a
// different OC session id, so OC starts with empty memory instead of
// reading the just-cleared conversation. Called from ↺ archive.
function _evoDrawerRotateOcSession(page_id) {
  // Cheap unique-enough salt — second-level timestamp + 4 random
  // chars. Collision-free at the cadence of an operator manually
  // hitting ↺.
  const salt =
    Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try {
    localStorage.setItem(_evoDrawerOcSaltKey(page_id), salt);
  } catch (_) {
    // Quota or denial — best effort. Next send still goes to the
    // old OC session, but the browser-side thread is empty so the
    // operator-visible behavior is just "evo remembers more than I
    // expect" rather than data corruption.
  }
}

function _evoDrawerClearThread() {
  const page_id = _evoDrawerCurrentPage();
  // Archive the current thread before clearing — the operator can
  // restore it via ↶. Empty threads are dropped silently by
  // _evoDrawerArchivePush so the picker doesn't fill with stubs.
  const current = _evoDrawerLoad(page_id);
  _evoDrawerArchivePush(page_id, {
    archived_at: new Date().toISOString(),
    kind: 'cleared',
    turns: current,
  });
  try { localStorage.removeItem(_evoDrawerKey(page_id)); } catch (_) {}
  // Rotate the OC session id so the NEXT send starts a fresh OC
  // memory. Without this, OC remembers the cleared conversation and
  // the operator-visible "I cleared the thread" promise is a lie.
  _evoDrawerRotateOcSession(page_id);
  // Close the history panel if it was open — its contents have changed
  // (we just pushed an entry) and we want the operator to start fresh.
  const panel = document.getElementById('evo-drawer-history-panel');
  if (panel) panel.style.display = 'none';
  _evoDrawerRenderThread(page_id);
  _evoDrawerSyncFabBadge();
  _evoDrawerSyncHeaderCounters();
}

// ── History picker ─────────────────────────────────────────────────────────
// Toggles the inline panel showing this page's archived threads. Operator
// can restore (replaces the live thread) or discard individual entries.

function _evoDrawerToggleHistory() {
  const panel = document.getElementById('evo-drawer-history-panel');
  if (!panel) return;
  if (panel.style.display === 'none' || !panel.style.display) {
    _evoDrawerRenderHistoryPanel();
    panel.style.display = 'block';
  } else {
    panel.style.display = 'none';
  }
}

function _evoDrawerRenderHistoryPanel() {
  const panel = document.getElementById('evo-drawer-history-panel');
  if (!panel) return;
  const page_id = _evoDrawerCurrentPage();
  const archives = _evoDrawerArchiveLoad(page_id);
  if (archives.length === 0) {
    panel.innerHTML = `<div class="subtle" style="color:var(--text3)">No previous threads on this page.</div>`;
    return;
  }
  // Newest at top — humans read time-descending in history pickers.
  const rows = archives.slice().reverse().map((a, idxFromEnd) => {
    const realIdx = archives.length - 1 - idxFromEnd;
    const ts = a.archived_at ? ago(a.archived_at) : '—';
    const kindLabel = a.kind === 'trimmed'
      ? '<span class="badge badge-muted" style="font-size:0.62rem;margin-right:6px" title="Older turns spilled here when this thread exceeded the 50-turn cap">trimmed</span>'
      : '<span class="badge badge-muted" style="font-size:0.62rem;margin-right:6px" title="You cleared this thread via ↺">cleared</span>';
    const firstUser = (a.turns || []).find(t => t.role === 'user');
    const previewText = firstUser
      ? firstUser.text
      : ((a.turns && a.turns[0] && a.turns[0].text) || '(no turns)');
    const preview = escHtml((previewText || '').slice(0, 80));
    const count = (a.turns || []).length;
    return `
      <div class="archive-row">
        ${kindLabel}
        <span class="archive-preview" title="${preview}">${preview}</span>
        <span class="archive-meta">${count} turn${count === 1 ? '' : 's'} · ${escHtml(ts)}</span>
        <button class="btn btn-sm" onclick="_evoDrawerRestoreArchive(${realIdx})" style="padding:2px 8px;font-size:0.72rem">Restore</button>
        <button class="btn btn-sm" onclick="_evoDrawerDiscardArchive(${realIdx})" style="padding:2px 8px;font-size:0.72rem;color:var(--text3)" title="Permanently remove this archived thread">Discard</button>
      </div>`;
  }).join('');
  panel.innerHTML = rows;
}

// Restore an archived thread → becomes the live thread, replacing
// whatever's currently there. The replaced thread itself gets archived
// (round-tripped via ↺ logic) so the operator can't accidentally lose
// the in-progress one by clicking Restore on the wrong row.
function _evoDrawerRestoreArchive(index) {
  const page_id = _evoDrawerCurrentPage();
  const archives = _evoDrawerArchiveLoad(page_id);
  if (index < 0 || index >= archives.length) return;
  // Archive the current live thread first (if any) so it's recoverable.
  const current = _evoDrawerLoad(page_id);
  if (current.length > 0) {
    _evoDrawerArchivePush(page_id, {
      archived_at: new Date().toISOString(),
      kind: 'cleared',
      turns: current,
    });
  }
  // Re-read archives after the push (the cap may have evicted one).
  const updated = _evoDrawerArchiveLoad(page_id);
  // The index the operator clicked refers to the pre-push list. But the
  // archive-push above appends to the tail and may evict from the head
  // if the list was at-cap. Re-locate the entry by archived_at + length,
  // which is unique enough in practice; fall back to the original index
  // if the lookup fails.
  const want = archives[index];
  let found = -1;
  for (let i = 0; i < updated.length; i++) {
    const u = updated[i];
    if (u.archived_at === want.archived_at
        && (u.turns || []).length === (want.turns || []).length) {
      found = i;
      break;
    }
  }
  const useIdx = found >= 0 ? found : Math.min(index, updated.length - 1);
  const restored = updated[useIdx];
  // Remove the entry from the archive list — it's now live again.
  updated.splice(useIdx, 1);
  _evoDrawerArchiveSave(page_id, updated);
  // Write the restored turns into the live slot.
  try {
    localStorage.setItem(
      _evoDrawerKey(page_id), JSON.stringify(restored.turns || []),
    );
  } catch (_) {}
  // Close the panel + re-render.
  const panel = document.getElementById('evo-drawer-history-panel');
  if (panel) panel.style.display = 'none';
  _evoDrawerRenderThread(page_id);
  _evoDrawerSyncFabBadge();
  _evoDrawerSyncHeaderCounters();
}

function _evoDrawerDiscardArchive(index) {
  const page_id = _evoDrawerCurrentPage();
  const archives = _evoDrawerArchiveLoad(page_id);
  if (index < 0 || index >= archives.length) return;
  archives.splice(index, 1);
  _evoDrawerArchiveSave(page_id, archives);
  _evoDrawerRenderHistoryPanel();
  _evoDrawerSyncHeaderCounters();
}

// Header counters + history-button visibility. Called any time the live
// thread or the archive list changes for the current page. Cheap; safe
// to call frequently.
function _evoDrawerSyncHeaderCounters() {
  const page_id = _evoDrawerCurrentPage();
  const counter = document.getElementById('evo-drawer-counter');
  const histBtn = document.getElementById('evo-drawer-history-btn');
  const live = _evoDrawerLoad(page_id);
  const archives = _evoDrawerArchiveLoad(page_id);
  if (counter) {
    if (live.length === 0) {
      counter.style.display = 'none';
      counter.textContent = '';
      counter.classList.remove('near-cap', 'at-cap');
    } else {
      counter.style.display = 'inline-block';
      counter.textContent = `${live.length}/${EVO_DRAWER_MAX_TURNS}`;
      counter.classList.toggle('at-cap', live.length >= EVO_DRAWER_MAX_TURNS);
      counter.classList.toggle(
        'near-cap',
        live.length >= EVO_DRAWER_MAX_TURNS - 5
          && live.length < EVO_DRAWER_MAX_TURNS,
      );
      counter.title = live.length >= EVO_DRAWER_MAX_TURNS
        ? `At the ${EVO_DRAWER_MAX_TURNS}-turn cap — older turns will be auto-archived. Use ↶ to recover them.`
        : `${live.length} of ${EVO_DRAWER_MAX_TURNS} turns. After the cap, oldest turns are archived (not lost — see ↶).`;
    }
  }
  if (histBtn) {
    histBtn.style.display = archives.length > 0 ? 'inline-block' : 'none';
    histBtn.title = archives.length > 0
      ? `${archives.length} previous thread${archives.length === 1 ? '' : 's'} on this page — click to view`
      : 'Previous threads on this page';
  }
}

// ── Authority badge ────────────────────────────────────────────────────────
// Visibility-only surface in the drawer header. Reads the same
// `_homeReadTier()` value the drawer's send uses, so what the operator sees
// is exactly what the next send will carry. Clicking routes to the Chat
// page (where the three-button selector lives) — the badge itself is NOT
// a selector. Per the design discussion, per-drawer authority override is
// deferred to a separate, larger change.

const _DRAWER_AUTHORITY_LABELS = {
  'ask':        '🤔 Ask',
  'auto-small': '⚡ Ask big',
  'auto':       '🤖 Auto',
};

function _updateDrawerAuthorityBadge() {
  const badge = document.getElementById('evo-drawer-authority-badge');
  if (!badge) return;
  const tier = (typeof _homeReadTier === 'function') ? _homeReadTier() : 'ask';
  const label = _DRAWER_AUTHORITY_LABELS[tier] || _DRAWER_AUTHORITY_LABELS['ask'];
  badge.textContent = label;
  // Title gets the verbose plain-English version so a hover explains what
  // tier means without forcing the operator to navigate to Chat just to
  // read the selector's tooltips.
  const verbose = {
    'ask':        'Ask — evo always asks before doing anything',
    'auto-small': 'Ask big — evo handles low-risk hygiene silently; everything else asks',
    'auto':       'Auto — evo handles anything with a clear answer; security + high-risk always confirm',
  }[tier] || 'Ask';
  badge.title = `Authority: ${verbose}. Click to change on Chat page.`;
}

function _evoDrawerOpenAuthoritySelector() {
  // Route to the Chat page, where the three-button authority selector lives.
  // Visibility-only by design — the drawer never changes the tier itself.
  const home = document.querySelector('.nav-item[data-page="home"]');
  if (home && typeof nav === 'function') {
    nav(home);
  }
  // After the nav swap, scroll the selector into view so the operator
  // doesn't have to hunt for it at the bottom of the Chat page. Wrapped
  // in setTimeout so the page swap (which is synchronous DOM mutation
  // but follows our handler) has landed before we measure scroll.
  setTimeout(function () {
    const sel = document.querySelector('.home-tier-bar');
    if (sel && typeof sel.scrollIntoView === 'function') {
      sel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, 0);
}

function _evoDrawerOnPageChange() {
  // Called from nav() after the active page swaps. If the drawer is open,
  // re-render with the new page's thread; either way refresh the FAB
  // badge so the dot shows on any page that already has history.
  const drawer = document.getElementById('evo-drawer');
  const page_id = _evoDrawerCurrentPage();
  if (drawer && drawer.classList.contains('open')) {
    _evoDrawerRenderThread(page_id);
    _evoDrawerUpdateContextChip();
    _evoDrawerSyncHeaderCounters();
    // Close the per-page history panel on nav so the operator doesn't
    // see one page's archive list bleed into another page's drawer.
    const panel = document.getElementById('evo-drawer-history-panel');
    if (panel) panel.style.display = 'none';
  }
  _evoDrawerSyncFabBadge();
  // Hide the FAB on pages that ARE a chat surface (Chat / Feedback /
  // Help) — a drawer there would be a second, redundant evo prompt.
  // CSS handles the actual hide via `body.evo-on-chat #evo-fab`. Both
  // the Feedback and Help pages mount their own page-chat thread that
  // shares the same localStorage key as the drawer (`evolve_chat_<id>`),
  // so the conversation is in one place either way.
  const onChatSurface = (
    page_id === 'home' || page_id === 'feedback' || page_id === 'help'
  );
  document.body.classList.toggle('evo-on-chat', onChatSurface);
  // Defensive: if the drawer happened to be open when the operator
  // navigated to a chat surface, close it so they don't end up with
  // two prompts.
  if (onChatSurface && drawer && drawer.classList.contains('open')) {
    _evoDrawerClose();
  }
}

function _evoDrawerSyncFabBadge() {
  // Green dot on the FAB whenever the current page has prior history,
  // so the operator knows there's a thread to resume here.
  const fab = document.getElementById('evo-fab');
  if (!fab) return;
  const page_id = _evoDrawerCurrentPage();
  const has = _evoDrawerLoad(page_id).length > 0;
  fab.classList.toggle('has-thread', has);
}

// ── Send ───────────────────────────────────────────────────────────────────

function _evoDrawerKeydown(ev) {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    _evoDrawerSend();
  } else if (ev.key === 'Escape') {
    _evoDrawerClose();
  }
}

async function _evoDrawerSend() {
  const input = document.getElementById('evo-drawer-input');
  const sendBtn = document.getElementById('evo-drawer-send');
  if (!input) return;
  const text = (input.value || '').trim();
  // PWA Phase 1.1.B: allow sends with attachments only (no text). The
  // bot still gets a turn — the attached files are referenced inline
  // in the message body via the chat-uploads helper.
  const hasPending = !!(window._pwaPending && window._pwaPending['evo-drawer']
                        && window._pwaPending['evo-drawer'].length);
  if (!text && !hasPending) return;
  const page_id = _evoDrawerCurrentPage();

  const userMsg = { role: 'user', text, ts: new Date().toISOString() };
  _evoDrawerAppend(page_id, userMsg);
  input.value = '';
  autoResizeComposer(input);  // collapse multi-line textarea back to 1 row
  if (sendBtn) sendBtn.disabled = true;
  input.disabled = true;

  const pendingId = 'evo-drawer-pending-' + Date.now();
  // Render the pending '…thinking…' placeholder ONLY if the operator
  // is still on the sending page. If they've already navigated, the
  // placeholder would land in the WRONG page's drawer DOM. The
  // storage path doesn't need this guard — pending bubbles don't
  // get saved.
  if (page_id === _evoDrawerCurrentPage()) {
    _evoDrawerRenderBubble({
      role: 'evo', text: '…thinking…', pending: true, dom_id: pendingId,
      ts: new Date().toISOString(),
    });
  }
  // Stalled-bubble guards. Mirrors _homeChatSend — same
  // three pieces, same rationale:
  //   (a) AbortController so the hard ceiling actually cancels the
  //       fetch (important on mobile Safari where backgrounded tabs
  //       can leave fetches dangling).
  //   (b) Slow-indicator interval — after EVO_DRAWER_SLOW_INDICATOR_MS
  //       (10 s) update the pending bubble body with "still thinking
  //       (Xs)" so the operator can see the turn is alive.
  //   (c) Hard timeout — at EVO_DRAWER_PENDING_TIMEOUT_MS (5 min)
  //       abort + flip the placeholder to a retry-able error.
  const abortCtl = new AbortController();
  const stopIndicator = _evoChatPendingIndicator(
    pendingId, EVO_DRAWER_SLOW_INDICATOR_MS,
  );
  let hardTimeoutFired = false;
  const pendingTimeoutId = setTimeout(() => {
    hardTimeoutFired = true;
    try { abortCtl.abort(); } catch (_) {}
    stopIndicator();
    if (page_id === _evoDrawerCurrentPage()) {
      const stale = document.getElementById(pendingId);
      if (stale) stale.remove();
    }
    const ceilingS = Math.round(EVO_DRAWER_PENDING_TIMEOUT_MS / 1000);
    _evoDrawerAppend(page_id, {
      role: 'evo',
      text: `(no reply after ${ceilingS}s — server may be busy)`,
      error: true,
      retry_text: text,  // surfaced as a "↻ retry" button below
      ts: new Date().toISOString(),
    });
  }, EVO_DRAWER_PENDING_TIMEOUT_MS);

  try {
    // PWA Phase 1.1.B — upload any pending attachments to /api/chat-uploads
    // BEFORE the chat POST, then include the resulting metadata in the
    // body so the server-side reference block reaches evo in the same
    // turn. flushPending clears the chip strip + queue; on failure
    // we re-throw into the catch below so the operator gets the
    // normal error bubble.
    let attachments = [];
    if (hasPending && typeof window._pwaFlushPending === 'function') {
      attachments = await window._pwaFlushPending('evo-drawer');
    }
    // History sent to the server is just this page's prior turns (minus
    // the user message we just appended — the server reads that from
    // the top-level `message` field). Per-page isolation by design;
    // page X's thread does not leak into page Y's prompt.
    const history = _evoDrawerLoad(page_id)
      .filter(m => !m.pending && !m.error)
      .filter(m => m.role === 'user' || m.role === 'evo')
      .slice(0, -1);
    const page_context = _evoDrawerContextPack();
    // Same authority tier the Chat page sends — the drawer is just
    // another evo surface, so the operator's "ask vs auto" choice
    // applies uniformly across both.
    const authority = (typeof _homeReadTier === 'function') ? _homeReadTier() : 'ask';
    // local_time anchors temporal references against the operator's
    // clock — see <session-context> block in evo/proxy.py.
    const local_time = new Date().toISOString();
    // Send the page's OC session id explicitly. Defaults match what
    // derive_session_id(page_id) produces server-side, but if the
    // operator has hit ↺ since the last send, this carries a salt so
    // we resolve to a fresh OC session instead of inheriting the
    // cleared conversation's memory.
    const session_id = _evoDrawerOcSessionId(page_id);
    // Per-conversation model tier — same sessionStorage value the home
    // composer uses (spec: docs/spec-user-tier-control-2026-05-26.md
    // "sessionStorage stickiness"). Drawer + chat-page surfaces don't
    // expose their own selector in v1; they read whatever the home
    // composer last set.
    const tier = (typeof _homeReadModelTier === 'function') ? _homeReadModelTier() : 'auto';
    // Diagnostic mode mirrors the home-chat flow — when evo's gateway is
    // confirmed down and the operator switched into the diagnostic LLM,
    // every drawer send routes to /api/diagnostic/chat instead. The
    // diagnostic endpoint accepts a stripped body (message + history).
    const endpoint = _diagnosticMode ? '/api/diagnostic/chat' : '/api/home/chat';
    const sendBody = _diagnosticMode
      ? { message: text, history }
      : { message: text, history, page_context, authority, tier, local_time,
          session_id, attachments };
    // Stream when talking to evo (so the pending bubble can show tool
    // activity in real time); buffer when in diagnostic mode (the
    // /api/diagnostic/chat endpoint has no streaming surface and is
    // fast enough that the wait isn't visible). The stream gracefully
    // falls back to buffered if the backend decides not to stream —
    // same downstream handling.
    const resp = _diagnosticMode
      ? await api('POST', endpoint, sendBody, { signal: abortCtl.signal })
      : await apiStream(endpoint, sendBody, {
          signal: abortCtl.signal,
          onActivity: (ev) => {
            const txt = _evoDrawerFormatActivity(ev);
            _evoSetPendingActivity(pendingId, txt);
          },
        });
    // Reply arrived (or was aborted) — clear the slow-indicator and
    // the hard timeout. If the hard timeout fired first it already
    // rendered the error bubble; skip the render below.
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
      _evoDrawerAppend(page_id, {
        role: 'evo',
        text: "Sorry — something went wrong and I didn't get a response. Please try again.",
        error: true,
        retry_text: text,
        ts: new Date().toISOString(),
      });
      return;
    }
    const reply = (resp && resp.reply) || resp?.error || '(no response)';
    const pending = document.getElementById(pendingId);
    if (pending) pending.remove();
    const meta = { role: 'evo', text: reply, ts: new Date().toISOString() };
    // Phase 4.1 proxy: source === 'evo' on success, 'proxy_error' on
    // subprocess/timeout/gateway failure, ``proxy_warn`` for the
    // empty-reply case where OC ran but produced no closing text turn
    // (the proxy synthesized a confirmation from session-jsonl tool
    // calls). See diagnosis-empty-reply-after-successful-tool-calls-
    // 2026-05-21.md + spec-surface-aware-help-style-2026-05-22.md §8.
    // Legacy 'llm'/'cap_exceeded'/'llm_error' sources are no longer
    // emitted but kept here for any backend rollback during a deploy.
    if (resp?.source === 'evo' && resp.model) {
      const usage = resp.usage || {};
      const tokens = (usage.input || 0) + (usage.output || 0);
      const tokenStr = tokens ? ` · ${tokens} tok` : '';
      const tierLabel = (typeof _modelTierLabel === 'function') ? _modelTierLabel(resp.model) : '';
      const tierPrefix = tierLabel ? `${tierLabel}: ` : '';
      meta.text += `\n\n_(${tierPrefix}${resp.model}${tokenStr})_`;
    }
    // tier_capped — Power was downgraded because the tier1 daily cap is
    // exhausted for the primary bot today. Spec:
    // docs/spec-user-tier-control-2026-05-26.md "Power capped surface".
    // Italics under the reply, never a banner.
    if (resp?.tier_capped) {
      meta.text += `\n\n_Power capped today — used Standard for this turn._`;
    }
    if (resp?.source === 'proxy_warn') {
      meta.warn = true;
    } else if (resp?.source === 'gateway_down') {
      // Evo's gateway is down — the proxy already confirmed via probe.
      // Render as an error bubble with the diagnostic-LLM affordance
      // (button injected by _evoDrawerRenderBubble when msg.gateway_down).
      // ``retry_text`` carries the operator's original message so the
      // one-click switch can re-send it through the diagnostic LLM
      // without retyping.
      meta.error = true;
      meta.gateway_down = true;
      meta.retry_text = text;
    } else if (resp?.source === 'proxy_error') {
      meta.error = true;
    } else if (resp?.source === 'llm' && typeof resp.cost_usd === 'number') {
      // Legacy path — preserved for rollback safety.
      const cap = resp.cap_status || {};
      meta.text +=
        `\n\n_(${resp.model} · ${resp.input_tokens}+${resp.output_tokens} tok` +
        ` · ~$${resp.cost_usd.toFixed(4)} · ${cap.used ?? '?'}/${cap.daily_cap ?? '?'} today)_`;
    } else if (resp?.source === 'cap_exceeded' || resp?.source === 'llm_error') {
      meta.error = true;
    }
    if (Array.isArray(resp?.suggested_actions) && resp.suggested_actions.length) {
      meta.suggested_actions = resp.suggested_actions;
    }
    _evoDrawerAppend(page_id, meta);
  } catch (e) {
    clearTimeout(pendingTimeoutId);
    stopIndicator();
    // The hard-timeout handler already rendered the timeout bubble;
    // don't double-render here.
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
    _evoDrawerAppend(page_id, {
      role: 'evo',
      text: "Sorry — something went wrong on my end. Please try again.",
      error: true,
      retry_text: text,  // surfaced as a "↻ retry" button below
      ts: new Date().toISOString(),
    });
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

function _evoDrawerRunSuggested(subcommand) {
  // One-click handler for LLM-suggested action buttons. Drops the
  // suggestion into the input and sends through the normal path.
  const input = document.getElementById('evo-drawer-input');
  if (!input) return;
  input.value = `evo ${subcommand}`;
  _evoDrawerSend();
}

// ─── Page-mounted chat surfaces (Feedback + Help) ──────────────────────────
//
// The Feedback and Help sidebar pages render an embedded evo chat as their
// primary surface — no form on Feedback, no docs-first layout on Help. The
// chat reuses /api/home/chat (same backend, same page_context plumbing),
// the same .home-msg-* render classes, and the same localStorage key
// convention as the side-drawer (``evolve_chat_<chatId>``). On these two
// pages the ✦ FAB drawer is hidden (``_evoDrawerOnPageChange``) so the
// operator sees exactly one evo prompt.
//
// Each ``chatId`` ("feedback" / "help") is one fixed thread per page —
// the home-chat's multi-session UX is overkill here.

const PAGE_CHAT_IDS = ['feedback', 'help'];
