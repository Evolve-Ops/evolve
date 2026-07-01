// ════════════════════════════════════════════════════════════════════════
// Page: AI Optimization
//
// The AI-Optimization page under the Improve bucket. Surfaces:
//   - Catalog       — per-bot model catalog (what models the bot can
//                     reach + their tier assignments)
//   - Tiers         — primary / fallback / cheap tier composition
//   - Cascade       — routing cascade ordering (cheap → primary → …)
//   - Routing       — heuristic routes (per-tool, per-message-shape)
//   - Fallback      — explicit fallback chain editor
//   - Embeddings    — embedding-model config (per-bot provider chain)
//   - Compaction    — compaction config (mode + reserve token budget)
//   - Pod engine    — pod-level engine override
//   - Freshness     — catalog drift + diversity drift signals
//
// State (top of the file):
//   _aiBot, _aiBotConfig, _aiCatalog, _aiPendingTiers,
//   _aiDirtySection, _aiTierOptions, _aiLastFreshnessSummary
//   _aiEmbeddingData, _aiPendingEmbChain, _aiEmbProvById
//
// Loaders + renderers dispatched via onPageActivate('ai-optimization'):
//   loadAiOptimization()         — entry; renders per-bot tabs + sections
//   _aiLoadFreshnessStatus()     — freshness/diversity/drift card
//   _aiLoadEmbeddings(botName)   — embeddings sub-section
//   _aiLoadPod()                 — pod engine + override card
//   _aiRenderBotData(botName)    — full bot render after data fetch
//   aiSwitchBot(botName)         — bot tab switch handler
//
// Cross-file linkages (runtime free-variable lookup):
//   - api(), toast(), escHtml(), botLabel() — core/
//   - loadStatus() (Overview) — refreshed after pod-engine changes
// ════════════════════════════════════════════════════════════════════════

// AI Optimization
// ══════════════════════════════════════════════════════
let _aiBot = null;
let _aiBotConfig = {};
let _aiCatalog = [];
let _aiPendingTiers = {};
let _aiDirtySection = null;

// Compat shim — used by _aiRenderCompaction / _aiCompactTierChanged
let _aiTierOptions = {};

// ── Model-listings cache (Phase 9 validated picker) ─────────────────────────
// The candidate source for the per-tier "Add model" picker: each credentialed
// provider's live model listing, persisted by discovery. Loaded once per page
// view via _aiLoadListings(); both the POD-default and bot tier editors read
// from it. Shape: {refreshed_at, providers:{<p>:[{model_id,qualified_id,...}]},
// degraded:[{provider,reason}], credentialed_providers:[...], llm_providers:[...]}.
// llm_providers is the LLM-capable subset of credentialed_providers (the
// easy-setup wizard ranks these). No provider or model literals live here — it
// is all server-sourced data.
let _aiListings = null;
let _aiListingsLoading = false;

// ── Observed per-model cost cache (cost-observability aspect) ────────────────
// Pod-wide observed $/1k-token figures, keyed by the BARE model id (last path
// segment, lowercased, `:suffix` stripped) so it joins to whatever id form a
// chip carries. Sourced from /api/analytics/usage's by_model payload — the
// same telemetry the Cost page's By Model table reads — over the 28d window so
// the chips show a stable rate. Distinct from the categorical costClass band
// (external pricing): this is what the model has actually cost THIS pod.
//   Shape: { "<bare-id>": { usd_per_1k_blended, low_confidence, calls } }
// Rebuilt by _aiLoadObservedCost(); read by _aiObservedCostFor() in the chip.
let _aiObservedCost = null;
let _aiObservedCostLoading = false;

// Normalise any model id form to the bare join key: drop a provider prefix,
// drop a trailing ":api_key"/":unexpected_billing" series suffix, lowercase.
function _aiBareModelKey(modelId) {
  let s = String(modelId || '').toLowerCase().trim();
  if (!s) return '';
  const colon = s.indexOf(':');
  if (colon > 0) s = s.slice(0, colon);
  const slash = s.lastIndexOf('/');
  if (slash >= 0) s = s.slice(slash + 1);
  return s;
}

// Build the observed-cost map from /api/analytics/usage by_model rows. Rows
// that collapse to the same bare key (e.g. a model seen under both token and
// api_key auth) are merged by summing cost + tokens, then the blended $/1k is
// recomputed from the merged totals so the chip rate matches what the operator
// would get aggregating the By Model table by model family.
async function _aiLoadObservedCost(force) {
  if (_aiObservedCostLoading) return _aiObservedCost;
  if (_aiObservedCost && !force) return _aiObservedCost;
  _aiObservedCostLoading = true;
  try {
    const d = await api('GET', '/api/analytics/usage?bot=all&days=28').catch(() => null);
    const map = {};
    const rows = (d && d.by_model) || [];
    // Merge to bare-key totals first.
    const acc = {};
    for (const r of rows) {
      const key = _aiBareModelKey(r.model);
      if (!key) continue;
      const a = acc[key] || { cost: 0, input: 0, output: 0, calls: 0 };
      a.cost   += Number(r.cost) || 0;
      a.input  += Number(r.input_tokens) || 0;
      a.output += Number(r.output_tokens) || 0;
      a.calls  += Number(r.calls) || 0;
      acc[key] = a;
    }
    for (const [key, a] of Object.entries(acc)) {
      const billed = a.input + a.output;
      const blended = (a.cost > 0 && billed > 0)
        ? a.cost / (billed / 1000) : null;
      map[key] = {
        usd_per_1k_blended: blended,
        // Mirror usage_analytics MODEL_COST_MIN_TOKENS (10k billed tokens).
        low_confidence: billed < 10000,
        calls: a.calls,
      };
    }
    _aiObservedCost = map;
  } finally {
    _aiObservedCostLoading = false;
  }
  return _aiObservedCost;
}

// Look up the observed $/1k for a model id; null when unknown / no data /
// below the min-sample threshold (the chip then shows only the costClass band).
function _aiObservedCostFor(modelId) {
  const key = _aiBareModelKey(modelId);
  if (!key || !_aiObservedCost) return null;
  const rec = _aiObservedCost[key];
  if (!rec || rec.low_confidence || rec.usd_per_1k_blended == null) return null;
  return rec;
}

// Render a compact observed-$/1k pill for a model chip — sits next to the
// costClass band so cost is visible while configuring tiers. Returns '' when
// there's no trustworthy observed figure (keeps the chip clean).
function _aiObservedCostPill(modelId) {
  const rec = _aiObservedCostFor(modelId);
  if (!rec) return '';
  const v = rec.usd_per_1k_blended;
  const txt = v < 0.01 ? '$' + v.toFixed(4) : '$' + v.toFixed(3);
  return `<span class="ai-observed-cost-pill" title="Effective $/1k — total spend (incl. cache) ÷ input+output volume on this pod (28d), ${rec.calls.toLocaleString()} turns. Your real usage, not a clean per-token list rate.">${txt}/1k</span>`;
}

// Presentation-only provider label (title-cased) — no provider literals in
// logic; this is a pure display transform over whatever provider keys the
// listings carry.
function _aiProviderLabel(p) {
  const s = String(p || '');
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// Presentation-only provider→color mapping (Phase 10a — explicit, bold,
// distinct color-by-provider chips). Provider is a fact, so its color is
// permanent (NOT tier-fit, which stays a contextual "suggested" highlight).
//
// This is presentation DATA, same category as _aiProviderLabel — NOT routing
// logic. The actual provider→hue mapping lives in base.css as
// `.ai-provider-<name>` classes (dark/light token pairs); the JS only
// slugifies the provider key into a class name. `_AI_PROVIDER_COLOR_NAMED` is
// the small presentation allow-list of providers base.css names directly
// (Anthropic=orange, OpenAI=green, Google=yellow, xAI=purple) — the same shape
// of display data as a label map, not a switch in any resolution path. Any
// other credentialed provider gets a stable, distinct overflow hue
// (pink/red/blue via the alt-N tokens); an empty/unknown provider gets gray.
const _AI_PROVIDER_COLOR_NAMED = ['anthropic', 'openai', 'google', 'xai'];
const _AI_PROVIDER_COLOR_OVERFLOW = ['alt-1', 'alt-2', 'alt-3'];

// Slugify a provider key for use in a CSS class (lowercase, non-alnum → '-').
function _aiProviderSlug(p) {
  return String(p || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

// Resolve a provider key to its `ai-provider-<name>` chip class. Named
// providers map directly; unknown-but-credentialed ones get a deterministic
// distinct overflow hue; empty → the gray "unknown" class.
function _aiProviderColorClass(p) {
  const slug = _aiProviderSlug(p);
  if (!slug) return 'ai-provider-unknown';
  if (_AI_PROVIDER_COLOR_NAMED.includes(slug)) return 'ai-provider-' + slug;
  let h = 0;
  for (let i = 0; i < slug.length; i++) h = (h * 31 + slug.charCodeAt(i)) >>> 0;
  return 'ai-provider-' + _AI_PROVIDER_COLOR_OVERFLOW[h % _AI_PROVIDER_COLOR_OVERFLOW.length];
}

// Canonical/bare model id → owning provider key, mirroring _aiListedIdIndex.
// Lets the tier chips color by provider without any provider literals: the
// provider key comes straight from the listings data. Returns '' for ids not
// present in the current listings (chip falls back to a neutral color).
function _aiProviderForModel(modelId) {
  const id = String(modelId || '').toLowerCase();
  if (!id) return '';
  const providers = (_aiListings && _aiListings.providers) || {};
  for (const [prov, models] of Object.entries(providers)) {
    for (const m of (models || [])) {
      const canonical = (m.qualified_id || m.model_id || '').toLowerCase();
      const bare = (m.model_id || '').toLowerCase();
      if ((canonical && canonical === id) || (bare && bare === id)) return prov;
    }
  }
  // Fall back to a qualifier prefix if the id is namespaced (e.g. "<p>/<m>")
  // but not in the listings — still avoids literals, derived from the id.
  const slash = id.indexOf('/');
  return slash > 0 ? id.slice(0, slash) : '';
}

// Render one provider-colored model chip. Unified component (Phase 10a) —
// shared by the model catalog, the POD and bot tier editors, and the read-only
// easy-setup wizard preview. The hue comes from the `ai-provider-<name>` class
// (base.css owns the provider→token map); no inline color, no provider literal.
//   short      — display id (anthropic/ etc. already stripped)
//   provider   — provider key (for color); '' → gray "unknown"
//   opts.onRemove — JS expression string; when set, renders an inline × that
//                   calls it (catalog + editable tier rows). Omit for read-only
//                   surfaces (wizard preview, generated-fallback list).
//   opts.keyDot   — optional leading status-dot HTML (catalog "has API key?"
//                   indicator); rendered before the model id, color intact.
function _aiModelChip(short, provider, opts) {
  const cls = _aiProviderColorClass(provider);
  const o = opts || {};
  const dot = o.keyDot || '';
  const x = o.onRemove
    ? `<button class="ai-chip-x" onclick="${o.onRemove}" title="Remove model">×</button>`
    : '';
  // Observed $/1k pill (cost-observability aspect): inline so cost is visible
  // while configuring tiers, alongside the costClass band on the tier header.
  // Renders '' when there's no trustworthy observed figure, so chips for
  // never-used / lightly-used models stay clean.
  const costPill = _aiObservedCostPill(short);
  return `<span class="ai-model-provider-chip ${cls}"
    title="${escHtml(_aiProviderLabel(provider) || 'provider unknown')}">${dot}${escHtml(short)}${costPill}${x}</span>`;
}

// ── Within-tier reorder (Phase 9 DnD; ↑↓ buttons restored in Phase 10c) ──
// The model order in a tier is the fallback chain (the resolver treats
// models[] order as the fallback order). Two reorder paths share one mutation:
//   • ↑/↓ move buttons on each row — the RELIABLE, webview-safe path. They work
//     in the Tauri/wry desktop app, in a browser, and under touch, because they
//     ride a plain click, not native HTML5 drag events.
//   • Native HTML5 drag-and-drop — a browser-only progressive enhancement.
// Phase 10a had retired the buttons on the assumption DnD sufficed, but native
// HTML5 DnD events do NOT fire in the Tauri/wry (WebKitGTK) desktop webview —
// the same webview-limitation class that made native confirm() a silent no-op
// (#3255/#3258) — so desktop and touch operators were left with NO working
// reorder path. The buttons are back as the primary control; DnD stays for the
// browser. Both scopes (the bot custom editor 'bot' and the POD default editor
// 'pod') route the array mutation through their own dirty+render side effects
// via _aiTierReorder(scope, ...), the single source of truth for the splice.
let _aiDragState = null; // { scope, key, from }

// Render one tier-model row: drag handle + ↑/↓ move buttons + index +
// provider-colored chip with an inline × (the removal control). scope:
// 'bot' | 'pod'; key: tier id or role id; removeFn: global fn expression for
// the inline ×; total: tier model count (to disable ↑ at the head / ↓ at the
// tail). The ↑/↓ buttons are the webview-safe reorder path; DnD is a browser
// enhancement layered on the same row.
function _aiTierModelRow(scope, key, m, i, removeFn, total) {
  const short = m.replace('anthropic/', '');
  const provider = _aiProviderForModel(m);
  const ek = escHtml(key);
  const es = escHtml(scope);
  const n = typeof total === 'number' ? total : 0;
  // ↑/↓ route through the SAME _aiTierReorder() as DnD — no duplicated splice.
  // Disabled at the ends (the reorder helper also no-ops an out-of-range move,
  // so this is belt-and-suspenders). aria-label names the model so the control
  // is legible to screen readers / keyboard operators.
  const upDis = i <= 0 ? ' disabled' : '';
  const downDis = i >= n - 1 ? ' disabled' : '';
  const moveBtns =
    `<span class="ai-tier-move">` +
      `<button type="button" class="ai-tier-move-btn"${upDis}` +
        ` aria-label="Move ${escHtml(short)} up in the fallback chain" title="Move up"` +
        ` onclick="_aiTierReorder('${es}','${ek}',${i},${i - 1})">↑</button>` +
      `<button type="button" class="ai-tier-move-btn"${downDis}` +
        ` aria-label="Move ${escHtml(short)} down in the fallback chain" title="Move down"` +
        ` onclick="_aiTierReorder('${es}','${ek}',${i},${i + 1})">↓</button>` +
    `</span>`;
  return `<div class="ai-tier-model-row" draggable="true"
      ondragstart="_aiTierDragStart(event,'${es}','${ek}',${i})"
      ondragend="_aiTierDragEnd(event)"
      ondragover="_aiTierDragOver(event,'${es}','${ek}',${i})"
      ondragleave="_aiTierDragLeave(event)"
      ondrop="_aiTierDrop(event,'${es}','${ek}',${i})">
      <span class="ai-tier-drag-handle" title="Drag to reorder fallback" aria-hidden="true">⠿</span>
      ${moveBtns}
      <span style="color:var(--text3);min-width:18px;text-align:right">${i+1}.</span>
      <span style="flex:1">${_aiModelChip(short, provider, { onRemove: `${removeFn}(${i})` })}</span>
    </div>`;
}

function _aiTierDragStart(ev, scope, key, from) {
  _aiDragState = { scope, key, from };
  if (ev.dataTransfer) { ev.dataTransfer.effectAllowed = 'move'; try { ev.dataTransfer.setData('text/plain', String(from)); } catch (_e) { /* some browsers require a payload */ } }
  if (ev.currentTarget && ev.currentTarget.classList) ev.currentTarget.classList.add('ai-dragging');
}

function _aiTierDragEnd(ev) {
  if (ev.currentTarget && ev.currentTarget.classList) ev.currentTarget.classList.remove('ai-dragging');
  _aiDragState = null;
}

function _aiTierDragOver(ev, scope, key, idx) {
  // Only react to drags that started in the same tier of the same editor.
  if (!_aiDragState || _aiDragState.scope !== scope || _aiDragState.key !== key) return;
  ev.preventDefault();
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
  if (ev.currentTarget && ev.currentTarget.classList) ev.currentTarget.classList.add('ai-drag-over');
}

function _aiTierDragLeave(ev) {
  if (ev.currentTarget && ev.currentTarget.classList) ev.currentTarget.classList.remove('ai-drag-over');
}

function _aiTierDrop(ev, scope, key, to) {
  if (ev.currentTarget && ev.currentTarget.classList) ev.currentTarget.classList.remove('ai-drag-over');
  if (!_aiDragState || _aiDragState.scope !== scope || _aiDragState.key !== key) return;
  ev.preventDefault();
  const from = _aiDragState.from;
  _aiDragState = null;
  if (from === to) return;
  _aiTierReorder(scope, key, from, to);
}

// Move models[from] to position `to` within a tier, then route through the
// scope's existing dirty+render side effects (same save path as the buttons).
function _aiTierReorder(scope, key, from, to) {
  const store = scope === 'pod' ? _aiPodDefaultsPending : _aiPendingTiers;
  const models = store[key]?.models;
  if (!models || from < 0 || from >= models.length || to < 0 || to >= models.length) return;
  const [moved] = models.splice(from, 1);
  models.splice(to, 0, moved);
  if (scope === 'pod') {
    _aiPodDefaultsDirty = true;
    _aiRenderPodDefaults();
  } else {
    _aiDirtySection = 'tiers';
    _aiRenderTiers(_aiBot, _aiBotConfig);
  }
}

async function _aiLoadListings(force) {
  if (_aiListingsLoading) return _aiListings;
  if (_aiListings && !force) return _aiListings;
  _aiListingsLoading = true;
  try {
    const r = await api('GET', '/api/models/listings').catch(() => null);
    // A fetch failure must NOT silently yield an empty picker — surface it on
    // the listings object so the refresh control can show "couldn't load".
    if (!r || r.error) {
      console.warn('ai-optimization: model listings fetch failed', r && r.error);
      _aiListings = { refreshed_at: null, providers: {}, degraded: [],
        credentialed_providers: [], _error: (r && r.error) || 'fetch failed' };
    } else {
      _aiListings = r;
    }
  } finally {
    _aiListingsLoading = false;
  }
  return _aiListings;
}

// Trigger a fresh server-side enumeration, then re-render whichever editor is
// open. Bound to the "Refresh" control next to "refreshed at X".
async function _aiRefreshListings(rerender) {
  const r = await api('POST', '/api/models/listings/refresh', {}).catch(() => null);
  if (!r || r.error || r.ok === false) {
    toast('Could not refresh model listings: ' + ((r && r.error) || 'enumeration failed'), 'err');
    return;
  }
  _aiListings = r;
  toast('Model listings refreshed', 'ok');
  if (typeof rerender === 'function') rerender();
}

// Flat set of canonical ids across every credentialed provider's listing —
// the "is this a real, currently-listed model?" lookup. Lower-cased keys map
// to canonical ids (used for the picker's "already-listed" check). Bare and
// qualified spellings both resolve.
//
// `scopeProviders` (optional) restricts the index to that provider set — the
// per-bot free-text path passes the bot's credentialed providers so a locally-
// listed id from a provider the bot can't run is NOT silently accepted. An
// empty/absent set indexes every listed provider (pod-wide).
function _aiListedIdIndex(scopeProviders) {
  const idx = {};
  const providers = (_aiListings && _aiListings.providers) || {};
  const scope = (Array.isArray(scopeProviders) && scopeProviders.length)
    ? new Set(scopeProviders.map(p => String(p).toLowerCase()))
    : null;
  for (const [prov, models] of Object.entries(providers)) {
    if (scope && !scope.has(String(prov).toLowerCase())) continue;
    for (const m of (models || [])) {
      const canonical = m.qualified_id || m.model_id;
      if (!canonical) continue;
      idx[canonical.toLowerCase()] = canonical;
      if (m.model_id) idx[m.model_id.toLowerCase()] = canonical;
    }
  }
  return idx;
}

// Render the validated grouped picker row. Phase 10b generalizes it across
// three call sites that source from different pools and differ on free-text /
// latest-bolding. `opts`:
//   pickFnName              — global fn expr called with the picked canonical id
//   suggested[]             — RECOMMENDED ids for this tier: highlighted (★),
//                             sorted first per group (soft signal, NOT a gate).
//                             Always shown — a recommended id that's also in
//                             `already` renders disabled "(already added)" so
//                             the operator sees it's present.
//   already[]               — ids already picked. Non-recommended ones are
//                             excluded; recommended ones stay visible as a
//                             disabled "(already added)" option (see above).
//   pool[]                  — OPTIONAL explicit pool of qualified ids the picker
//                             is constrained to (the bot catalog). Provider is
//                             derived from each id's "<prov>/<model>" prefix.
//                             When omitted, the source is the full credentialed
//                             listings (`_aiListings.providers`) — the POD-tier
//                             and catalog sources.
//   credentialedProviders[] — OPTIONAL per-bot provider scope (catalog only).
//                             When set, the full-listings source is intersected
//                             with this set so the bot is offered only models
//                             from providers it holds a key for. Empty/absent →
//                             pod-wide credentialed listing (the POD editor, and
//                             the fail-open path when a bot's creds are unknown).
//   freeText (bool)         — render the "or type a model ID" input + "+ Add"
//                             (catalog only — tier editors pick from a fixed
//                             pool, no typing). Requires inputId + addFnName.
//   boldLatest (bool)       — mark the latest-in-family option per provider
//                             (catalog exhaustive picker only; reads the
//                             discovery-annotated is_family_latest flag).
//   inputId                 — id for the free-text input element (freeText only)
//   addFnName               — global fn for the free-text add (freeText only)
//   rerenderFnName          — global fn base whose "<name>_refresh()" reloads
//                             the listings (Refresh control; full-listings only)
function _aiModelPickerRow(opts) {
  const listings = (_aiListings && _aiListings.providers) || {};
  const degraded = (_aiListings && _aiListings.degraded) || [];
  const suggested = new Set((opts.suggested || []).map(s => String(s).toLowerCase()));
  const already = new Set((opts.already || []).map(s => String(s).toLowerCase()));
  const boldLatest = !!opts.boldLatest;

  // Source providers→models. With an explicit pool, group the pool ids by their
  // "<prov>/<model>" prefix (the bot catalog is just a flat qualified-id list).
  // Without one, use the full credentialed listings. Either way the result is
  // {provider: [{qualified_id, is_family_latest?}, ...]} so one code path below.
  let providerModels;        // {provider: [{qualified_id, is_family_latest}]}
  let providerOrder;
  if (Array.isArray(opts.pool)) {
    providerModels = {};
    for (const raw of opts.pool) {
      const id = String(raw || '');
      if (!id) continue;
      const slash = id.indexOf('/');
      const prov = slash > 0 ? id.slice(0, slash) : '';
      (providerModels[prov] = providerModels[prov] || []).push({ qualified_id: id });
    }
    providerOrder = Object.keys(providerModels);
  } else {
    let credentialed = (_aiListings && _aiListings.credentialed_providers) || Object.keys(listings);
    // Per-bot scope (catalog picker): `opts.credentialedProviders` is THIS
    // bot's credentialed providers, derived from its keyStatus (no literals).
    // Intersect so a bot is only offered models from providers it can actually
    // run (e.g. no xai/ on a bot without an xAI key). Fail open: an empty/absent
    // bot set leaves the pod-wide credentialed listing untouched, so unknown
    // creds never blank the picker. The POD editor passes nothing here and keeps
    // the full credentialed union.
    if (Array.isArray(opts.credentialedProviders) && opts.credentialedProviders.length) {
      const botSet = new Set(opts.credentialedProviders.map(p => String(p).toLowerCase()));
      credentialed = credentialed.filter(p => botSet.has(String(p).toLowerCase()));
    }
    providerModels = {};
    for (const prov of credentialed) {
      providerModels[prov] = (listings[prov] || []).map(m => ({
        qualified_id: m.qualified_id || m.model_id,
        is_family_latest: !!m.is_family_latest,
      }));
    }
    providerOrder = credentialed;
  }

  // Build one optgroup per provider. The RECOMMENDED set (`suggested`, spec
  // §Addendum 6) is always shown so the operator sees it: not-yet-added ones
  // are pickable + starred "(suggested)"; already-present ones stay VISIBLE but
  // disabled + greyed "(already added)" so the operator can tell the
  // recommended model IS there (and won't add a worse one thinking it's
  // missing). Non-recommended models that are already added stay excluded (no
  // value in re-listing a non-recommended pick). The star/markers are textual,
  // not CSS — <option> styling is unreliable cross-browser.
  const groups = providerOrder.map(prov => {
    const models = (providerModels[prov] || [])
      .filter(m => m.qualified_id)
      // Keep already-added models ONLY when they're recommended (so the
      // operator sees the recommended one is present); drop already-added
      // non-recommended picks (today's behavior).
      .filter(m => {
        const lc = m.qualified_id.toLowerCase();
        return !already.has(lc) || suggested.has(lc);
      });
    if (!models.length) return '';
    // Sort: recommended first, then within recommended the actionable (not-yet-
    // added) ones above the disabled "(already added)" ones, then alphabetical.
    const sortKey = m => {
      const lc = m.qualified_id.toLowerCase();
      const isSug = suggested.has(lc) ? 0 : 1;
      const isAdded = already.has(lc) ? 1 : 0;
      return [isSug, isAdded];
    };
    models.sort((a, b) => {
      const ka = sortKey(a), kb = sortKey(b);
      if (ka[0] !== kb[0]) return ka[0] - kb[0];
      if (ka[1] !== kb[1]) return ka[1] - kb[1];
      return a.qualified_id.localeCompare(b.qualified_id);
    });
    const opts2 = models.map(m => {
      const id = m.qualified_id;
      const lc = id.toLowerCase();
      const isSug = suggested.has(lc);
      const isAdded = already.has(lc);
      const short = id.replace(/^[^/]+\//, '');
      // Bold-latest is a textual marker (●), not font-weight: <option> styling
      // is unreliable cross-browser, but a glyph prefix renders everywhere.
      const latest = boldLatest && m.is_family_latest;
      let label = short;
      // A recommended model already in the tier: keep the ★ but mark it present
      // and disable selection (re-adding would be a no-op / dup).
      if (isSug && isAdded) label = `★ ${label} (already added)`;
      else if (isSug) label = `★ ${label} (suggested)`;
      if (latest) label = `● ${label}`;
      const dis = isAdded ? ' disabled' : '';
      return `<option value="${escHtml(id)}"${dis}>${escHtml(label)}</option>`;
    }).join('');
    return `<optgroup label="${escHtml(_aiProviderLabel(prov))}">${opts2}</optgroup>`;
  }).filter(Boolean).join('');

  const emptyMsg = Array.isArray(opts.pool)
    ? 'No models in this bot\'s catalog yet — add models to the catalog above.'
    : 'No credentialed models listed yet — Refresh to load them.';
  const hasOptions = !!groups;
  const picker = hasOptions
    ? `<select class="input-w-lg" onchange="${opts.pickFnName}(this.value);this.value=''"
         style="background:var(--bg2);border:1px solid var(--border);color:var(--text1);border-radius:5px;padding:3px 8px;font-family:monospace;font-size:0.78rem">
         <option value="">Add a model…</option>
         ${groups}
       </select>`
    : `<span style="font-size:0.76rem;color:var(--text3)">${escHtml(emptyMsg)}</span>`;

  // Degraded + Refresh only make sense for the full-listings sources (the pool
  // path is a fixed local allowlist, not a live enumeration).
  const isListings = !Array.isArray(opts.pool);
  const degradedRow = (isListings && degraded.length)
    ? `<div style="font-size:0.72rem;color:var(--yellow);margin-top:4px">Couldn't refresh: ${degraded.map(d => escHtml(_aiProviderLabel(d.provider))).join(', ')}</div>`
    : '';

  let refreshCtl = '';
  if (isListings && opts.rerenderFnName) {
    const refreshedAt = (_aiListings && _aiListings.refreshed_at)
      ? new Date(_aiListings.refreshed_at).toLocaleString()
      : 'never';
    refreshCtl = `<span style="font-size:0.72rem;color:var(--text3);display:inline-flex;align-items:center;gap:6px">
      refreshed ${escHtml(refreshedAt)}
      <button class="btn btn-ghost btn-sm" style="font-size:0.7rem;padding:1px 6px" onclick="${opts.rerenderFnName}_refresh()">Refresh</button>
    </span>`;
  }

  // Free-text "or type a model ID" lives only where opts.freeText is set
  // (the catalog). Tier editors pick from a fixed source — no typing.
  const freeTextCtl = opts.freeText
    ? `<input id="${escHtml(opts.inputId)}" class="input-w-lg"
        style="background:var(--bg2);border:1px solid var(--border);color:var(--text1);border-radius:5px;padding:3px 8px;font-family:monospace;font-size:0.78rem"
        placeholder="or type a model ID…"
        onkeydown="if(event.key==='Enter')${opts.addFnName}()">
      <button class="btn btn-ghost btn-sm" style="font-size:0.75rem" onclick="${opts.addFnName}()">+ Add</button>`
    : '';

  return `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px">
      ${picker}
      ${freeTextCtl}
      ${refreshCtl}
    </div>${degradedRow}`;
}

// Shared free-text "add anyway" validation: validates a typed id against the
// listing via the server (nearest-match suggestion on a miss). On success the
// CANONICAL id is added (correct spelling). onPick receives the canonical id.
//
// `opts.botId` scopes validation to that bot's credentialed providers — both
// the server round-trip (passes bot_id) and the fast local index path (filtered
// to `opts.credentialedProviders`) — so a bot can't free-type a model from a
// provider it holds no key for. Omitting them validates pod-wide (the prior
// behavior; also the fail-open path when the bot's creds are unknown).
async function _aiValidateAndAdd(typed, onPick, opts) {
  const o = opts || {};
  const model = (typed || '').trim();
  if (!model) { toast('Enter a model ID', 'err'); return; }
  // Fast local path: an exact listed id needs no round-trip — but only when it
  // belongs to a provider this bot can run (else it would bypass the scoped
  // server check below).
  const idx = _aiListedIdIndex(o.credentialedProviders);
  const local = idx[model.toLowerCase()];
  if (local) { onPick(local); return; }
  const r = await api('POST', '/api/models/validate-model',
    o.botId ? { model, bot_id: o.botId } : { model }).catch(() => null);
  if (r && r.ok) { onPick(r.canonical); return; }
  const sug = r && r.suggestion;
  if (sug) {
    toast(`"${model}" isn't in the current model listings — did you mean ${sug}?`, 'err');
  } else {
    toast(`"${model}" isn't in the current model listings (Refresh if you just added a provider).`, 'err');
  }
}

function _aiModelStr(v) {
  if (!v) return '—';
  if (typeof v === 'object') return v.primary || Object.values(v).find(x => typeof x === 'string') || '—';
  return v;
}

function _aiTierForModel(model, tiers) {
  if (!model || !tiers) return null;
  const norm = m => (m || '').replace('anthropic/', '').toLowerCase();
  for (const [tid, tc] of Object.entries(tiers)) {
    if ((tc.models || []).some(m => norm(m) === norm(model))) return tid;
  }
  return null;
}

function _aiRoutingText(routing, tiers) {
  const enabled = routing?.enabled !== false;
  const maintTier = routing?.maintenanceTier || 'tier3';
  const bgTier = routing?.backgroundTier || 'tier3';
  const t2model = (tiers?.tier2?.models || ['claude-sonnet-4-6'])[0].replace('anthropic/', '');
  const t3model = (tiers?.tier3?.models || ['claude-haiku-4-5'])[0].replace('anthropic/', '');
  const maintModel = maintTier === 'tier2' ? t2model : t3model;
  const bgModel = bgTier === 'tier2' ? t2model : t3model;
  return `Productive   → ${t2model}  (full capability)
Maintenance  → ${maintModel}  (fast + cheap)${enabled ? '  ✓ routing active' : ''}
Background   → ${bgModel}  (fast + cheap)${enabled ? '  ✓ routing active' : ''}
Ambiguous    → ${t2model}  (safe default)`;
}

// ── Gateway restart banner ─────────────────────────────────────────────────────

function _aiShowRestartBanner() {
  const el = document.getElementById('ai-restart-banner');
  if (el) el.style.display = 'flex';
}

async function _aiRestartGateway() {
  if (!_aiBot) return;
  const r = await api('POST', '/api/admin/gateway/' + _aiBot + '/restart', { confirm: true });
  if (r.error) {
    toast(r.error, 'err');
  } else {
    toast('Gateway restart queued', 'ok');
    const el = document.getElementById('ai-restart-banner');
    if (el) el.style.display = 'none';
  }
}

// ── Model Freshness (pod-wide best-in-class check) ───────────────────────────

// Map the backend's legacy tierN keys (freshness/drift advisories still emit
// them) to canonical, numeral-free ROLE labels (spec §Addendum3.C.1). The
// rung/role migration retired "Tier 0–3" — these are the role names the rest
// of the UI uses. role-id keys are also accepted for forward-compatible
// advisories that emit a role directly (e.g. role_resolves_outside_catalog).
const TIER_DISPLAY = {
  tier0: 'Judge',
  tier1: 'Power',
  tier2: 'Standard',
  tier3: 'Fast',
  judge: 'Judge',
  power: 'Power',
  standard: 'Standard',
  fast: 'Fast',
  max: 'Max',
};

// Display labels for LLM providers. Presentation only — logic never branches
// on provider identity (no provider literals in logic); an unknown/new
// provider falls back to its raw id so it still renders. Shared by the
// provider-diversity card and the credential-gap drift advisory.
const PROVIDER_LABELS = { anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google', xai: 'xAI' };
function providerLabel(p) { return PROVIDER_LABELS[p] || p; }

// Cached last freshness summary — _aiApplyAllFreshness() reads
// .advisories from this rather than re-running Check Now (which would
// re-shell the OC CLI on every bot). Refreshed by every renderer call.
let _aiLastFreshnessSummary = null;

// ── Model discovery (spec §Addendum 12 + 13) ─────────────────────────────────
// New models a provider lists that aren't in any of this pod's rungs. The
// model_discovery generator is signal-only — it no longer drops an AdoptModel
// proposal in the Recommendations queue; the operator adopts here with one
// click. Source: the gated firing model_discovery Signals
// (/api/models/discoveries), now split by the capability-aware placement
// verdict (Addendum 13):
//   • fits_existing → the per-rung adopt list (`discoveries`), collapsed
//     server-side to the single BEST model per (provider, role) so a provider
//     listing many variants of a rung the operator already fills shows one row.
//   • new_tier → a separate "create a rung?" section (`new_tiers`) — a
//     genuinely-new model line (e.g. a frontier model) that fits no rung.
//   • mode_variant / specialist / cannot_place are unroutable, so the server
//     never surfaces them here (signal-only).
// The unit shown is "best available model for a role I actually use", not
// "every model the provider lists".
let _aiLastDiscoveries = [];
// new_tier discoveries — model lines that fit NO existing rung; rendered in the
// separate "create a rung?" section, never mixed into the per-rung adopt list.
let _aiLastNewTiers = [];
// ── Version upgrades — the PRIMARY freshness surface (spec §Addendum 15) ──────
// Models the pod already runs that have a newer version of the SAME class
// available (Sonnet 4-5 → Sonnet 5, Opus 4-7 → 4-8). This is the everyday "ride
// the latest version of the model class I already chose" action — rendered FIRST
// on the card with a one-click per-row Update and a bulk "Update all to latest".
// Source: /api/models/discoveries `upgrades` (deterministic, off the listings
// cache; no Signals, no recency/modality gate). New-line discovery is secondary.
let _aiLastUpgrades = [];
// Role buttons the segmented picker offers (server-sourced from the pod
// catalog). Fast/Standard/Power/Max always; Judge only when the pod defines it.
let _aiPickerRoles = ['fast', 'standard', 'power', 'max'];
// Qualified ids the operator has already SEEN this session (cleared the nav
// badge for). Visiting the AI Optimization page acknowledges everything shown;
// the badge re-appears only for genuinely-new discoveries on the next poll.
const _aiDiscoSeen = new Set();
const _AI_DISCO_CAP_ROLES = new Set(['max', 'power']);
const _AI_DISCO_DEFAULT_MAX_CAP = 5;

// Neutral nav-badge count of un-acknowledged discoveries (adopt rows + new-tier
// rows). Called from the global status poll (index.html) and after the card
// loads/adopts/ignores. Exposed on window for the inline poll; classic-script
// function declarations are already global, this is just the explicit contract.
function _aiRefreshNavBadge(discoveries, newTiers, upgrades) {
  const list = (Array.isArray(discoveries) ? discoveries : [])
    .concat(Array.isArray(newTiers) ? newTiers : [])
    // Upgrades have no qualified_id — key them on latest_model so an
    // acknowledged upgrade clears like an acknowledged discovery.
    .concat((Array.isArray(upgrades) ? upgrades : [])
      .map(u => ({ qualified_id: u.latest_model })));
  const fresh = list.filter(d => !_aiDiscoSeen.has(d.qualified_id));
  const el = document.getElementById('badge-ai-optimization');
  if (!el) return;
  el.textContent = String(fresh.length);
  el.style.display = fresh.length ? '' : 'none';
}
window._aiRefreshNavBadge = _aiRefreshNavBadge;

// Acknowledge every currently-shown row (upgrades + both discovery sections) +
// hide the badge — the operator is looking at the card.
function _aiMarkDiscoSeenAndClearBadge() {
  for (const u of _aiLastUpgrades) _aiDiscoSeen.add(u.latest_model);
  for (const d of _aiLastDiscoveries) _aiDiscoSeen.add(d.qualified_id);
  for (const d of _aiLastNewTiers) _aiDiscoSeen.add(d.qualified_id);
  const el = document.getElementById('badge-ai-optimization');
  if (el) { el.textContent = '0'; el.style.display = 'none'; }
}

// Segmented role picker — mirrors the chat-bar model-tier control
// (.home-tier-buttons / .home-tier-btn / .active; see index.html home model
// bar). `activeRole` is pre-selected. Buttons carry the canonical ROLE labels
// (Fast/Standard/Power/Max[/Judge]) via TIER_DISPLAY — never a rung slug. The
// role strings come from a fixed server allow-list, safe to inline in onclick.
function _aiRenderRolePicker(i, activeRole) {
  const roles = (_aiPickerRoles && _aiPickerRoles.length)
    ? _aiPickerRoles : ['fast', 'standard', 'power', 'max'];
  const btns = roles.map(r => {
    const on = (r === activeRole) ? ' active' : '';
    return `<button type="button" class="home-tier-btn ai-disco-role-btn${on}" data-role="${r}" onclick="_aiDiscoverySelectRole(${i}, '${r}')">${escHtml(TIER_DISPLAY[r] || r)}</button>`;
  }).join('');
  return `<div class="home-tier-buttons ai-disco-roles" id="ai-disco-roles-${i}" role="group" aria-label="Adopt as role">${btns}</div>`;
}

// One per-rung adopt row: provider-colored model chip + the ROLE it's the best
// available for (role label, never a slug) + listing evidence + the plain fit
// reason, then a segmented role picker (pre-selected to the recommended role), a
// cap input shown only for max/power, an Ignore and an Adopt button.
function _aiRenderDiscoveryRow(d, i) {
  const role = d.role || d.recommended_role || '';
  const roleLabel = TIER_DISPLAY[role] || role || '';
  const cost = d.suggested_cost_class ? ` · ${escHtml(d.suggested_cost_class)}` : '';
  const ev = d.evidence || {};
  const evBits = [];
  if (ev.context_window) evBits.push(`ctx ${Number(ev.context_window).toLocaleString()}`);
  if (ev.max_output_tokens) evBits.push(`out ${Number(ev.max_output_tokens).toLocaleString()}`);
  const evLine = evBits.length
    ? `<span style="color:var(--text3);font-size:0.74rem"> · ${escHtml(evBits.join(' · '))}</span>` : '';
  const fitLine = d.fit_reason
    ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">${escHtml(d.fit_reason)}</div>` : '';
  const roleBit = roleLabel
    ? `best available for your <strong>${escHtml(roleLabel)}</strong> role`
    : 'fits an existing role';
  const showCap = _AI_DISCO_CAP_ROLES.has(role);
  return `
    <div class="card" style="margin-bottom:6px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border)">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px">
        ${_aiModelChip(d.model_id, d.provider, {})}
        <span style="font-size:0.74rem;color:var(--text2)">${roleBit}${cost}</span>
        ${evLine}
      </div>
      ${fitLine}
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px">
        <span style="font-size:0.78rem;color:var(--text2)">Adopt as</span>
        ${_aiRenderRolePicker(i, role)}
        <label id="ai-disco-cap-wrap-${i}" style="font-size:0.78rem;color:var(--text2);display:${showCap ? '' : 'none'}">Daily cap / bot
          <input id="ai-disco-cap-${i}" class="input-w-sm" type="number" min="1" step="1" value="${_AI_DISCO_DEFAULT_MAX_CAP}" style="margin-left:6px">
        </label>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" onclick="_aiIgnoreDiscovery(${i})" title="Never surface this model again">Ignore</button>
        <button class="btn btn-sm" onclick="_aiAdoptDiscovery(${i})">Adopt</button>
      </div>
    </div>`;
}

// One new-tier row: a model line that fits no existing rung. The action is
// creating a rung for it (not slotting it into one), so the framing + button say
// so. No role picker — there's no existing role to map; the operator assigns one
// from Settings → Models after the tier exists.
function _aiRenderNewTierRow(d, i) {
  const ev = d.evidence || {};
  const evBits = [];
  if (ev.context_window) evBits.push(`ctx ${Number(ev.context_window).toLocaleString()}`);
  if (ev.max_output_tokens) evBits.push(`out ${Number(ev.max_output_tokens).toLocaleString()}`);
  const evLine = evBits.length
    ? `<span style="color:var(--text3);font-size:0.74rem"> · ${escHtml(evBits.join(' · '))}</span>` : '';
  const fitLine = d.fit_reason
    ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:4px">${escHtml(d.fit_reason)}</div>` : '';
  return `
    <div class="card" style="margin-bottom:6px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border)">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px">
        ${_aiModelChip(d.model_id, d.provider, {})}
        <span style="font-size:0.74rem;color:var(--text2)">would add a new tier</span>
        ${evLine}
      </div>
      ${fitLine}
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px">
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" onclick="_aiIgnoreNewTier(${i})" title="Never surface this model again">Ignore</button>
        <button class="btn btn-sm" onclick="_aiAdoptNewTier(${i})" title="Create the new rung for this model — assign it a role afterward from Settings → Models">Create tier</button>
      </div>
    </div>`;
}

// One version-upgrade row: the latest model chip + "you're on <current>" + the
// role(s) it serves, then a one-click Update button. The action extends the rung
// the predecessor occupies — the resolver then routes to the newest member, so
// no role change is needed.
function _aiRenderUpgradeRow(u, i) {
  const cur = u.current_model || '';
  const roleBit = u.role_label
    ? ` · <span style="color:var(--text2)">${escHtml(u.role_label)} role</span>`
    : '';
  return `
    <div class="card" style="margin-bottom:6px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border)">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${_aiModelChip(u.latest_model_id, u.provider, {})}
        <span style="font-size:0.78rem;color:var(--text2)">is available — you're on <code style="font-size:0.76rem">${escHtml(cur)}</code>${roleBit}</span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" onclick="_aiApplyUpgrade(${i})" title="Update this model class to its latest version">Update</button>
      </div>
    </div>`;
}

// The PRIMARY "newer versions available" section (spec §Addendum 15). Returns ''
// when nothing is stale. Leads with a bulk "Update all to latest" — the everyday
// one-click — then one Update row per stale model class. Stashes per-row
// identities on window so index-keyed handlers avoid escaping slashes in onclick.
function _aiRenderUpgrades(upgrades) {
  const list = Array.isArray(upgrades) ? upgrades : [];
  if (!list.length) return '';
  window._aiUpgrades = list.map(u => ({
    provider: u.provider, latest_model_id: u.latest_model_id,
    latest_model: u.latest_model,
  }));
  const one = list.length === 1;
  const rows = list.map((u, i) => _aiRenderUpgradeRow(u, i)).join('');
  const bulkBtn = list.length > 1
    ? `<button class="btn btn-sm" id="ai-upgrade-all-btn" onclick="_aiApplyAllUpgrades()">Update all to latest (${list.length})</button>`
    : '';
  return `
    <div style="margin-bottom:16px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
        <span style="color:var(--accent);font-weight:600;font-size:0.85rem">${list.length} model${one ? '' : 's'} ${one ? 'has' : 'have'} a newer version available</span>
        <span style="flex:1"></span>
        ${bulkBtn}
      </div>
      <div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">
        ${one ? 'This is a model' : 'These are models'} you already run, with a newer version of the same class now available. Updating keeps you on the latest version of the class you chose — your roles don't change. <strong>Nothing updates on its own</strong> — this is one-click only.
      </div>
      ${rows}
    </div>`;
}

// The full discovery section for the Model Freshness card. Returns '' when
// there's nothing to adopt. Stashes per-row identities on window so the
// index-keyed handlers look them up without escaping slashes in onclick.
function _aiRenderDiscoveries(discoveries, newTiers) {
  const list = Array.isArray(discoveries) ? discoveries : [];
  const tiers = Array.isArray(newTiers) ? newTiers : [];
  if (!list.length && !tiers.length) return '';
  window._aiDiscoveries = list.map(d => ({
    provider: d.provider, model_id: d.model_id, qualified_id: d.qualified_id,
  }));
  window._aiNewTiers = tiers.map(d => ({
    provider: d.provider, model_id: d.model_id, qualified_id: d.qualified_id,
  }));

  let adoptBlock = '';
  if (list.length) {
    const one = list.length === 1;
    const rows = list.map((d, i) => _aiRenderDiscoveryRow(d, i)).join('');
    adoptBlock = `
      <div style="margin-bottom:16px">
        <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px">
          <span style="color:var(--accent);font-weight:600;font-size:0.85rem">${list.length} new model${one ? '' : 's'} available to adopt</span>
        </div>
        <div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">
          ${one ? 'This is the best new model' : 'These are the best new models'} a provider lists for ${one ? 'a role' : 'roles'} you already run. Adopting maps ${one ? 'it' : 'each'} to that role — change the role with the buttons if you want it elsewhere. <strong>Adoption never happens on its own</strong> — these are one-click only.
        </div>
        ${rows}
      </div>`;
  }

  let tierBlock = '';
  if (tiers.length) {
    const one = tiers.length === 1;
    const rows = tiers.map((d, i) => _aiRenderNewTierRow(d, i)).join('');
    tierBlock = `
      <div style="margin-bottom:16px">
        <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px">
          <span style="color:var(--accent);font-weight:600;font-size:0.85rem">${one ? 'New frontier model' : `${tiers.length} new frontier models`} — create a rung?</span>
        </div>
        <div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">
          ${one ? 'This model fits' : 'These models fit'} none of your existing rungs — ${one ? "it's" : "they're"} priced and capable unlike anything you run today. Creating a tier adds ${one ? 'it' : 'them'} to your line-up; you assign a role afterward from Settings → Models.
        </div>
        ${rows}
      </div>`;
  }
  return adoptBlock + tierBlock;
}

// Select a role in a row's segmented picker (toggles .active) and show/hide the
// cap input for max/power. Replaces the retired "Map role" dropdown.
function _aiDiscoverySelectRole(i, role) {
  const wrap = document.getElementById('ai-disco-roles-' + i);
  if (wrap) {
    wrap.querySelectorAll('.ai-disco-role-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.role === role);
    });
  }
  const capWrap = document.getElementById('ai-disco-cap-wrap-' + i);
  if (capWrap) {
    const show = _AI_DISCO_CAP_ROLES.has(role);
    capWrap.style.display = show ? '' : 'none';
    if (show) {
      const cap = document.getElementById('ai-disco-cap-' + i);
      if (cap && !cap.value) cap.value = String(_AI_DISCO_DEFAULT_MAX_CAP);
    }
  }
}
window._aiDiscoverySelectRole = _aiDiscoverySelectRole;

// The role of a row's active segmented button.
function _aiActiveDiscoveryRole(i) {
  const wrap = document.getElementById('ai-disco-roles-' + i);
  const active = wrap && wrap.querySelector('.ai-disco-role-btn.active');
  return (active && active.dataset.role) || 'none';
}

// Add a model (from either section) to the operator ignore list, then reload.
async function _aiIgnoreEntry(entry) {
  if (!entry) { _aiLoadFreshnessStatus(); return; }
  if (!await confirmModal({ body: `Stop surfacing ${entry.qualified_id}? It's added to the discovery ignore list; remove it there to see it again.`, danger: true })) return;
  const r = await api('POST', '/api/models/ignore-discovery',
    { provider: entry.provider, model_id: entry.model_id });
  if (!r || !r.ok) { toast((r && (r.error || r.message)) || 'Ignore failed', 'err'); return; }
  await _aiLoadFreshnessStatus();
}

// Adopt ONE discovered model with the row's active role/cap choice. Drives the
// AdoptModel applier server-side (no Proposal), then reloads the card + badge.
async function _aiAdoptDiscovery(i) {
  const entry = (window._aiDiscoveries || [])[i];
  if (!entry) { _aiLoadFreshnessStatus(); return; }
  const role = _aiActiveDiscoveryRole(i);
  const payload = { provider: entry.provider, model_id: entry.model_id, role };
  if (_AI_DISCO_CAP_ROLES.has(role)) {
    const capEl = document.getElementById('ai-disco-cap-' + i);
    const cap = capEl && capEl.value ? parseInt(capEl.value, 10) : null;
    if (cap != null && Number.isFinite(cap)) payload.cap = cap;
  }
  const r = await api('POST', '/api/models/adopt-discovery', payload);
  if (!r || !r.ok) { toast((r && (r.error || r.message)) || 'Adopt failed', 'err'); return; }
  toast(`Adopted ${entry.qualified_id}`, 'ok');
  _aiShowRestartBanner();
  await _aiLoadFreshnessStatus();
}

// Ignore (never surface again) ONE discovered model from the adopt list.
async function _aiIgnoreDiscovery(i) {
  return _aiIgnoreEntry((window._aiDiscoveries || [])[i]);
}

// Apply ONE version upgrade — ride the latest version of a model class the pod
// already runs. Extends the rung the predecessor occupies; the resolver then
// routes to the newest member. Drives the AdoptModel applier server-side.
async function _aiApplyUpgrade(i) {
  const entry = (window._aiUpgrades || [])[i];
  if (!entry) { _aiLoadFreshnessStatus(); return; }
  const r = await api('POST', '/api/models/apply-upgrade',
    { provider: entry.provider, latest_model_id: entry.latest_model_id });
  if (!r || !r.ok) { toast((r && (r.error || r.message)) || 'Update failed', 'err'); return; }
  toast(`Updated to ${entry.latest_model}`, 'ok');
  _aiShowRestartBanner();
  await _aiLoadFreshnessStatus();
}
window._aiApplyUpgrade = _aiApplyUpgrade;

// Apply EVERY available version upgrade in one pass — "Update all to latest".
async function _aiApplyAllUpgrades() {
  const list = window._aiUpgrades || [];
  if (!list.length) { _aiLoadFreshnessStatus(); return; }
  const btn = document.getElementById('ai-upgrade-all-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Updating…'; }
  const r = await api('POST', '/api/models/apply-all-upgrades', {});
  if (!r || (!r.ok && !r.applied)) {
    toast((r && (r.error || r.message)) || 'Update all failed', 'err');
    if (btn) { btn.disabled = false; }
    await _aiLoadFreshnessStatus();
    return;
  }
  const applied = r.applied || 0;
  toast(r.failed ? `Updated ${applied}, ${r.failed} failed` : `Updated ${applied} to latest`,
    r.failed ? 'info' : 'ok');
  _aiShowRestartBanner();
  await _aiLoadFreshnessStatus();
}
window._aiApplyAllUpgrades = _aiApplyAllUpgrades;

// Create the new rung for a new_tier model (role 'none' — the applier mints the
// rung from the model's proposed slug; the operator maps a role afterward).
async function _aiAdoptNewTier(i) {
  const entry = (window._aiNewTiers || [])[i];
  if (!entry) { _aiLoadFreshnessStatus(); return; }
  const r = await api('POST', '/api/models/adopt-discovery',
    { provider: entry.provider, model_id: entry.model_id, role: 'none' });
  if (!r || !r.ok) { toast((r && (r.error || r.message)) || 'Create tier failed', 'err'); return; }
  toast(`Created tier for ${entry.qualified_id}`, 'ok');
  _aiShowRestartBanner();
  await _aiLoadFreshnessStatus();
}
window._aiAdoptNewTier = _aiAdoptNewTier;

// Ignore (never surface again) ONE new_tier model.
async function _aiIgnoreNewTier(i) {
  return _aiIgnoreEntry((window._aiNewTiers || [])[i]);
}
window._aiIgnoreNewTier = _aiIgnoreNewTier;

function _aiFreshnessRender(summary) {
  const el = document.getElementById('ai-freshness-panel');
  _aiLastFreshnessSummary = summary;
  _aiSyncApplyAllButton(summary);
  _aiSyncFreshnessSummaryCount(summary);
  if (!el) return;

  // Version upgrades (the PRIMARY surface, spec §Addendum 15) lead the card,
  // then new-line discovery — both render at the top, independent of whether the
  // legacy staleness check has run.
  const discoveryBlock =
    _aiRenderUpgrades(_aiLastUpgrades)
    + _aiRenderDiscoveries(_aiLastDiscoveries, _aiLastNewTiers);

  // Loud-fail banner: Evolve couldn't read OpenClaw's credential store for one
  // or more bots (server-side source="none" marker). Render it ABOVE every
  // other block — when it fires, the "No credentialed model" emptiness below is
  // a read failure, not a real lack of credentials.
  const credStoreBlock = _aiRenderCredStoreUnreadable(
    summary && summary.credential_store_unreadable
  );

  if (!summary || !summary.checked_at) {
    el.innerHTML = discoveryBlock + `<div class="empty" style="padding:8px 0;font-size:0.8rem;color:var(--text3)">
      No staleness check has run yet. Click <strong>Check Now</strong> to compare each credentialed provider's listing against this pod's rungs and verify every role's resolved model is in the bot's catalog.
    </div>`;
    return;
  }

  // Storage is UTC; format in the pod's configured timezone (Settings →
  // Pod Config → Timezone). Fall back to raw ISO if the timestamp is
  // missing so operators still see something during a degraded check.
  const checkedAt = summary.checked_at
    ? (fmtPodTimeFull(summary.checked_at) || summary.checked_at)
    : '';
  const advisories = summary.advisories || [];
  const driftFindings = summary.drift_findings || [];
  const diversity = summary.diversity_advisories || [];
  const providers = summary.providers_checked || [];
  const provText = providers.length
    ? `Providers checked: ${providers.join(', ')}`
    : 'No providers with API keys detected';

  // Catalog drift is a CORRECTNESS issue (runtime drops non-catalog
  // tier entries silently), so we render it above the freshness
  // advisories — a red banner the operator can't miss. The
  // "tier_member_missing" kind means OC is actively ignoring a
  // configured model right now. "recommended_missing" is informational.
  // Both kinds are the same correctness failure (a model OC will silently
  // drop): tier_member_missing names a model in a role's models[]; the
  // role_resolves_outside_catalog kind (spec §Addendum3.D) catches a role that
  // resolves to a code-DEFAULT model the catalog lacks — the max →
  // claude-fable-5 case that no tier entry names. Render both in the drift
  // banner with the same one-click catalog-add affordance.
  const tierDrift = driftFindings.filter(
    d => d.kind === 'tier_member_missing' || d.kind === 'role_resolves_outside_catalog'
  );
  // Credential-aware split (spec §Addendum 10 §B). A tier/role names a model
  // whose provider the bot has NO API key for → "Reconcile catalog" can't help
  // (the server skips uncredentialed providers, leaving OC's silent drop rather
  // than promoting it to a fatal "no API key"). The server marks those
  // provider_credentialed=false; route them to a missing-credentials advisory
  // (copy the key, or remove the entry) instead of the red reconcile banner.
  // provider_credentialed mirrors exactly whether the reconcile pass would add
  // the model, so the two surfaces never disagree.
  const reconcilableDrift = tierDrift.filter(d => d.provider_credentialed !== false);
  const credGapDrift = tierDrift.filter(d => d.provider_credentialed === false);
  // Severity split (spec §Addendum 10 §C). hard_break_tiers is a pure-resolution
  // side-car: the authoritative list of roles whose entire chain has no
  // credentialed model, so they WON'T ROUTE. It also catches the in-catalog
  // break (e.g. judge with no diverse provider) that carries no drift finding.
  // Render it prominently at the top. The dormant section then shows only the
  // credential-gap entries whose role still routes (tier_severity !== hard_break)
  // — entries on a hard-broken role are folded into the hard-break panel.
  const hardBreakTiers = summary.hard_break_tiers || [];
  // Soft same-vendor judge advisory (2026-06-19): routes fine, amber nudge only.
  const advisoryTiers = summary.advisory_tiers || [];
  const dormantDrift = credGapDrift.filter(d => d.tier_severity !== 'hard_break');
  const hardBreakBlock = hardBreakTiers.length > 0 ? _aiRenderHardBreak(hardBreakTiers) : '';
  const advisoryBlock = advisoryTiers.length > 0 ? _aiRenderAdvisoryTiers(advisoryTiers) : '';
  const driftBlock = reconcilableDrift.length > 0 ? _aiRenderCatalogDrift(reconcilableDrift) : '';
  const credGapBlock = dormantDrift.length > 0 ? _aiRenderCatalogCredGap(dormantDrift) : '';
  const diversityBlock = diversity.length > 0 ? _aiRenderDiversity(diversity) : '';
  // Redundant-config advisory (spec §Addendum 5): Custom bots whose config
  // resolves equal to the pod default. Derived, operator-owned, NO auto-action.
  const redundantBots = summary.redundant_bots || [];
  const redundantBlock = redundantBots.length > 0 ? _aiRenderRedundant(redundantBots) : '';

  if (advisories.length === 0 && driftFindings.length === 0 && diversity.length === 0 && redundantBots.length === 0 && hardBreakTiers.length === 0 && advisoryTiers.length === 0) {
    el.innerHTML = discoveryBlock + credStoreBlock + `
      <div style="display:flex;align-items:center;gap:10px;font-size:0.85rem">
        <span style="color:var(--green);font-weight:600">✓ All tiers are current.</span>
        <span style="color:var(--text3);font-size:0.78rem">Last checked: ${escHtml(checkedAt)}</span>
      </div>
      <div style="font-size:0.72rem;color:var(--text3);margin-top:4px">${escHtml(provText)}</div>`;
    return;
  }

  // If only drift/diversity/redundant findings (no tier-advisory mismatches),
  // render those blocks — no need for the "Update tier" table.
  if (advisories.length === 0) {
    el.innerHTML = discoveryBlock + credStoreBlock + `
      <div style="display:flex;align-items:center;gap:10px;font-size:0.85rem;margin-bottom:10px">
        <span style="color:var(--green);font-weight:600">✓ All tiers are current.</span>
        <span style="color:var(--text3);font-size:0.78rem">Last checked: ${escHtml(checkedAt)}</span>
      </div>
      ${hardBreakBlock}
      ${advisoryBlock}
      ${driftBlock}
      ${credGapBlock}
      ${diversityBlock}
      ${redundantBlock}
      <div style="font-size:0.72rem;color:var(--text3);margin-top:8px">${escHtml(provText)}</div>`;
    return;
  }

  const rows = advisories.map((a, i) => {
    const tierName = TIER_DISPLAY[a.tier] || a.tier;
    const current = a.current_model
      ? `<code style="font-size:0.78rem">${escHtml(a.current_model)}</code>`
      : `<span style="color:var(--text3);font-style:italic;font-size:0.78rem">no ${escHtml(a.provider)} model in this tier</span>`;
    const released = a.recommended_released
      ? `<span style="color:var(--text3);font-size:0.7rem">since ${escHtml(a.recommended_released)}</span>`
      : '';
    const updateBtn = `<button class="btn btn-secondary btn-sm"
        onclick='_aiUpdateRecommendedModel(${JSON.stringify(a)})'
        style="font-size:0.72rem;padding:3px 9px">Update</button>`;
    return `<tr>
      <td style="padding:6px 8px"><strong>${escHtml(a.bot_id)}</strong></td>
      <td style="padding:6px 8px">${escHtml(tierName)}</td>
      <td style="padding:6px 8px;color:var(--text2)">${escHtml(a.provider)}</td>
      <td style="padding:6px 8px">${current}</td>
      <td style="padding:6px 8px">
        <code style="font-size:0.78rem;color:var(--green)">${escHtml(a.recommended_model)}</code>
        <div>${released}</div>
      </td>
      <td style="padding:6px 8px;text-align:right">${updateBtn}</td>
    </tr>`;
  }).join('');

  el.innerHTML = discoveryBlock + credStoreBlock + `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;font-size:0.85rem">
      <span style="color:var(--yellow);font-weight:600">${advisories.length} tier${advisories.length===1?'':'s'} could be updated.</span>
      <span style="color:var(--text3);font-size:0.78rem">Last checked: ${escHtml(checkedAt)}</span>
    </div>
    <div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">${escHtml(provText)}</div>
    ${hardBreakBlock}
    ${advisoryBlock}
    ${driftBlock}
    ${credGapBlock}
    ${diversityBlock}
    ${redundantBlock}
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <thead>
          <tr style="text-align:left;color:var(--text2);font-size:0.72rem;text-transform:uppercase">
            <th style="padding:6px 8px;border-bottom:1px solid var(--border)">Bot</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border)">Tier</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border)">Provider</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border)">Current</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border)">Recommended</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border)"></th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// Render the "this bot has only one LLM provider; consider adding a
// second" soft advisories. Each row gets:
//   - "Copy from <bot>" button per (suggested_provider × source_bot)
//     pre-computed by the server's borrow_candidates map
//   - "+ Add manually" — opens the Add Key modal pre-filled with the
//     first suggested provider, after switching the Credentials tab
//     to this bot
//   - "Dismiss" — POSTs to /api/models/freshness-advisory/dismiss and
//     hides the row for this bot until the operator resets via the
//     panel header
// Redundant-config advisory (spec §Addendum 5 "Migration / cleanup"). A Custom
// bot whose explicit config resolves equal-after-merge to the pod default —
// clearing it changes nothing. No auto-action: each row offers "Reset to pod
// defaults" (the existing per-bot tier-mode reset). Informational, low-key —
// not a correctness error, so it uses the neutral panel chrome, not the red
// drift banner.
function _aiRenderRedundant(redundantBots) {
  if (!redundantBots || !redundantBots.length) return '';
  const rows = redundantBots.map(b => {
    const botId = b.bot_id;
    return `<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);font-size:0.82rem">
      <span class="ai-layer-chip ai-layer-default">redundant</span>
      <span style="flex:1"><strong>${escHtml(botLabel(botId))}</strong> — its custom tier definitions resolve to the pod default. Resetting would change nothing about its routing.</span>
      <button class="btn btn-ghost btn-sm" style="font-size:0.74rem"
        onclick="_aiResetRedundantBot('${escHtml(botId)}')"
        title="Discard this bot's redundant custom tiers and inherit the pod defaults">Reset to pod defaults</button>
    </div>`;
  }).join('');
  return `<div style="margin-bottom:12px;padding:10px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:8px">
    <div style="font-weight:600;font-size:0.82rem;margin-bottom:4px">Redundant tier definitions</div>
    <div style="font-size:0.74rem;color:var(--text2);margin-bottom:6px">These bots carry their own tier definitions that match the pod default. Resetting them to "Use pod defaults" keeps routing identical and lets future default changes flow through automatically.</div>
    ${rows}
  </div>`;
}

async function _aiResetRedundantBot(botId) {
  if (!botId) return;
  if (!await confirmModal({ body: `Reset ${botLabel(botId)} to the pod's default tier definitions?\n\nIts current custom tiers already resolve to the pod default, so routing is unchanged. It will inherit future default changes automatically.`, danger: true })) {
    return;
  }
  const r = await api('PUT', '/api/admin/config/' + botId + '/tier-mode', { mode: 'default' });
  if (r.error) {
    toast('Reset failed: ' + r.error, 'err');
    return;
  }
  toast(`${botLabel(botId)} reset to pod defaults`, 'ok');
  // Re-run the freshness check so the redundant row drops off.
  await _aiCheckModelFreshness();
}

function _aiRenderDiversity(diversity) {
  if (!diversity || !diversity.length) return '';

  // providerLabel is module-level (shared with the credential-gap drift card).

  const blocks = diversity.map(adv => {
    const bot = adv.bot_id;
    const current = (adv.current_providers || []).map(providerLabel).join(', ') || '(none)';
    const suggested = adv.suggested_providers || [];
    const byProvider = adv.borrow_candidates || {};

    // Per-suggested-provider copy buttons. Empty when no other bot
    // in the pod has that provider configured — in that case only the
    // "+ Add manually" path is offered.
    const copyButtons = suggested.flatMap(prov => {
      const sources = byProvider[prov] || [];
      return sources.map(src => {
        const label = `Copy ${providerLabel(prov)} from ${src.bot_id}`;
        return `<button class="btn btn-secondary btn-sm"
          style="font-size:0.72rem;padding:3px 9px;margin:2px 4px 2px 0"
          onclick="_aiDiversityBorrow('${escHtml(bot)}','${escHtml(prov)}','${escHtml(src.bot_id)}')">
          ${escHtml(label)}
        </button>`;
      });
    }).join('');

    const firstSuggested = suggested[0] || '';
    const addManually = `<button class="btn btn-ghost btn-sm"
      style="font-size:0.72rem;padding:3px 9px;margin:2px 4px 2px 0"
      onclick="_aiDiversityAddManually('${escHtml(bot)}','${escHtml(firstSuggested)}')">
      + Add manually
    </button>`;

    const dismiss = `<button class="btn btn-ghost btn-sm"
      style="font-size:0.72rem;padding:3px 9px;margin:2px 0;color:var(--text3)"
      onclick="_aiDiversityDismiss('${escHtml(bot)}')">
      Dismiss
    </button>`;

    const suggestedText = suggested.length
      ? suggested.map(providerLabel).join(' or ')
      : 'another provider';

    return `
      <div style="display:flex;flex-direction:column;gap:4px;padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="font-size:0.82rem">
          <strong>${escHtml(bot)}</strong>
          <span style="color:var(--text2)"> has only ${escHtml(current)}. Consider adding ${escHtml(suggestedText)} for fallback or judge-tier use.</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;align-items:center">
          ${copyButtons}${addManually}${dismiss}
        </div>
      </div>`;
  }).join('');

  return `
    <div style="background:rgba(126,184,247,0.06);border:1px solid rgba(126,184,247,0.25);border-radius:6px;padding:8px 12px 4px 12px;margin-bottom:14px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
        <div style="font-size:0.82rem;font-weight:600;color:var(--blue)">
          Provider diversity — consider adding a second provider
        </div>
        <span style="font-size:0.7rem;color:var(--text3)">soft recommendation · dismissible</span>
      </div>
      <div style="font-size:0.74rem;color:var(--text2);margin-bottom:4px">
        A second LLM provider gives you a fallback when the primary is rate-limited or down, and a cross-vendor option for the judge tier.
      </div>
      ${blocks}
    </div>`;
}

async function _aiDiversityBorrow(botId, provider, fromBot) {
  if (!await confirmModal(`Copy ${provider} credentials from ${botLabel(fromBot)} into ${botLabel(botId)}?\n\nA fresh independent copy is written to ${botLabel(botId)}'s auth-profiles. Rotating ${botLabel(fromBot)}'s key later does NOT propagate.`)) return;
  const r = await api('POST', `/api/admin/keys/${botId}/${provider}/borrow`, { from_bot: fromBot });
  if (r.error) {
    toast(r.error, 'err');
    return;
  }
  toast(`Copied ${provider} from ${botLabel(fromBot)} → ${botLabel(botId)}`, 'ok');
  _aiShowRestartBanner();
  await _aiCheckModelFreshness();
}

function _aiDiversityAddManually(botId, suggestedProvider) {
  // Navigate to Plugins page (the Credentials surface), switch the
  // bot tab, then open the Add Key modal pre-filled with the first
  // suggested provider.
  const pluginsNav = document.querySelector('[data-page=plugins]');
  if (pluginsNav) nav(pluginsNav);
  setTimeout(() => {
    if (typeof ikSwitchBot === 'function') ikSwitchBot(botId);
    setTimeout(() => openAddKeyModal(suggestedProvider || ''), 100);
  }, 200);
}

async function _aiDiversityDismiss(botId) {
  const r = await api('POST', '/api/models/freshness-advisory/dismiss', {
    type: 'diversity', key: botId,
  });
  if (r.error) {
    toast(r.error, 'err');
    return;
  }
  toast(`Dismissed for ${botLabel(botId)}`, 'ok');
  await _aiLoadFreshnessStatus();
}

// Render the "tier references model X not in catalog" drift block.
// `tierDrift` is an array of {bot_id, tier, provider, model_id,
// is_recommended} from the freshness summary's drift_findings.
// Groups by bot so team_bot_a's 9-row drift renders compactly.
function _aiRenderCatalogDrift(tierDrift) {
  if (!tierDrift || !tierDrift.length) return '';
  // Group by bot_id
  const byBot = {};
  for (const d of tierDrift) {
    if (!byBot[d.bot_id]) byBot[d.bot_id] = [];
    byBot[d.bot_id].push(d);
  }
  const botBlocks = Object.entries(byBot).map(([botId, drifts]) => {
    const rows = drifts.map(d => {
      // d.tier is a legacy tierN key for tier_member_missing, or a role id for
      // role_resolves_outside_catalog — TIER_DISPLAY maps both to a role label.
      const roleName = TIER_DISPLAY[d.tier] || d.tier;
      const phrase = d.kind === 'role_resolves_outside_catalog'
        ? `is the resolved model for <strong>${escHtml(roleName)}</strong> but not in catalog — pulls die at the OpenClaw layer.`
        : `is in <strong>${escHtml(roleName)}</strong> but not in catalog — OC drops it at runtime.`;
      return `<li><code style="font-size:0.78rem">${escHtml(d.model_id)}</code> ${phrase}</li>`;
    }).join('');
    return `
      <div style="margin-bottom:8px">
        <div style="font-size:0.78rem;margin-bottom:4px"><strong>${escHtml(botId)}</strong> · ${drifts.length} drift${drifts.length === 1 ? '' : 's'}</div>
        <ul style="margin:0 0 6px 18px;padding:0;font-size:0.78rem;color:var(--text2)">${rows}</ul>
        <button class="btn btn-secondary btn-sm" onclick="_aiReconcileCatalog('${escHtml(botId)}')"
                style="font-size:0.72rem;padding:3px 9px">Reconcile catalog for ${escHtml(botId)}</button>
      </div>`;
  }).join('');
  // Top-of-card bulk action mirrors the freshness "Apply all" button: when
  // more than one bot has drift, offer a single "Reconcile all" so the
  // operator isn't clicking N per-bot buttons one at a time. The loop reuses
  // the per-bot reconcile endpoint sequentially (see _aiReconcileAllCatalog),
  // matching how the per-bot path already works — no new server route needed.
  const botCount = Object.keys(byBot).length;
  const reconcileAll = botCount > 1
    ? `<button class="btn btn-secondary btn-sm" onclick="_aiReconcileAllCatalog()"
              style="font-size:0.72rem;padding:3px 9px;margin-bottom:10px">Reconcile all (${botCount} bots)</button>`
    : '';
  return `
    <div style="background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.3);border-radius:6px;padding:10px 12px;margin-bottom:14px;font-size:0.82rem">
      <div style="font-weight:600;color:var(--red);margin-bottom:6px">⚠ Catalog drift detected — models referenced but not registered</div>
      <div style="font-size:0.74rem;color:var(--text2);margin-bottom:8px">
        OpenClaw uses the catalog (<code style="font-size:0.7rem">agents.defaults.models</code>) as the runtime whitelist. Tier entries naming a model not in the catalog are silently dropped — the model is effectively unconfigured.
      </div>
      ${reconcileAll}
      ${botBlocks}
    </div>`;
}

// Render the CREDENTIAL-STORE-UNREADABLE banner. `info` is the server's
// `credential_store_unreadable` side-car (set only when the presence reader
// returned source="none" for ≥1 bot — i.e. Evolve found NO readable OpenClaw
// credential store at all, typically because the store format moved under us).
// This is operator-facing legibility: it explains that the "No credentialed
// model" emptiness below is a READ failure, not a real lack of credentials, and
// that routing itself is unaffected (the gateway reads its own store). A
// critical severity (every bot blind) is red; a single-bot warning is amber.
function _aiRenderCredStoreUnreadable(info) {
  if (!info || !info.message) return '';
  const critical = info.severity === 'critical';
  const accent = critical ? 'var(--red)' : 'var(--yellow)';
  const bg = critical ? 'rgba(248,113,113,0.08)' : 'rgba(240,180,41,0.1)';
  const border = critical ? 'rgba(248,113,113,0.3)' : 'rgba(240,180,41,0.35)';
  const bots = Array.isArray(info.bots) ? info.bots : [];
  const botList = bots.length
    ? `<div style="font-size:0.74rem;color:var(--text2);margin-top:6px">Affected: ${bots.map(b => escHtml(botLabel(b))).join(', ')}</div>`
    : '';
  return `
    <div style="background:${bg};border:1px solid ${border};border-radius:6px;padding:10px 12px;margin-bottom:14px;font-size:0.82rem">
      <div style="font-weight:600;color:${accent};margin-bottom:6px">⚠ Evolve can't read OpenClaw's credential store</div>
      <div style="font-size:0.78rem;color:var(--text2)">${escHtml(info.message)}</div>
      ${botList}
    </div>`;
}

// Render the HARD-BREAK panel (spec §Addendum 10 §C). `hardBreakTiers` comes
// from the server's `hard_break_tiers` side-car: every (bot, role) whose entire
// chain resolves to NO credentialed model, so the role WON'T ROUTE — a real
// routing failure (`reason: uncredentialed`), distinct from an inert fallback.
// This is the prominent, top-of-panel error. It is pure-resolution, so it
// catches the in-catalog break (e.g. judge with no diverse provider) that has
// no drift finding. Each record carries the providers it needs (`needed_providers`,
// filtered to those the bot LACKS), per-provider `borrow_candidates`, and the
// offending uncredentialed tier entries (`offending_models`) so we can offer the
// SAME copy-creds (_aiDiversityBorrow) and remove-from-tier (_aiRemoveFromTier)
// remedies the §B credential-gap advisory uses. Red, matching _aiRenderCatalogDrift.
function _aiRenderHardBreak(hardBreakTiers) {
  if (!hardBreakTiers || !hardBreakTiers.length) return '';
  const byBot = {};
  for (const t of hardBreakTiers) {
    if (!byBot[t.bot_id]) byBot[t.bot_id] = [];
    byBot[t.bot_id].push(t);
  }
  const botBlocks = Object.entries(byBot).map(([botId, tiers]) => {
    const rows = tiers.map(t => {
      const roleName = TIER_DISPLAY[t.role] || t.role;
      const needed = t.needed_providers || [];
      const cand = t.borrow_candidates || {};
      // Copy a key for any provider that would make this role route. Reuses the
      // diversity card's borrow flow (confirm → POST borrow → restart → re-check).
      const copyButtons = needed.flatMap(prov => {
        const provName = providerLabel(prov);
        return (cand[prov] || []).map(src => `<button class="btn btn-secondary btn-sm"
          style="font-size:0.72rem;padding:3px 9px;margin:2px 4px 2px 0"
          onclick="_aiDiversityBorrow('${escHtml(botId)}','${escHtml(prov)}','${escHtml(src.bot_id)}')">
          Copy ${escHtml(provName)} from ${escHtml(botLabel(src.bot_id))}
        </button>`);
      }).join('');
      // Remove the dead uncredentialed tier entries (tier_member_missing carries
      // the legacy tierN key the saved tiers doc uses; role-resolved defaults
      // have no entry to strip).
      const removeButtons = (t.offending_models || [])
        .filter(o => o.kind === 'tier_member_missing')
        .map(o => `<button class="btn btn-ghost btn-sm"
          style="font-size:0.72rem;padding:3px 9px;margin:2px 4px 2px 0"
          onclick="_aiRemoveFromTier('${escHtml(botId)}','${escHtml(o.tier)}','${escHtml(o.model_id)}')">
          Remove ${escHtml(o.model_id)} from ${escHtml(roleName)}
        </button>`).join('');
      const neededNames = needed.map(providerLabel).join(', ');
      const noSource = !copyButtons && neededNames
        ? `<span style="font-size:0.72rem;color:var(--text3)">Add a key for ${escHtml(neededNames)} in Credentials to make ${escHtml(roleName)} route.</span>`
        : '';
      const need = neededNames
        ? ` It needs a key for one of: <strong>${escHtml(neededNames)}</strong>.`
        : '';
      return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
        <div style="font-size:0.78rem;margin-bottom:4px">
          <strong>${escHtml(roleName)}</strong> won't route — no credentialed model in its chain.${need}
        </div>
        <div style="display:flex;flex-wrap:wrap;align-items:center">${copyButtons}${removeButtons}${noSource}</div>
      </div>`;
    }).join('');
    return `<div style="margin-bottom:8px">
      <div style="font-size:0.78rem;margin-bottom:4px"><strong>${escHtml(botLabel(botId))}</strong> · ${tiers.length} tier${tiers.length === 1 ? '' : 's'} won't route</div>
      ${rows}
    </div>`;
  }).join('');
  return `
    <div style="background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.3);border-radius:6px;padding:10px 12px;margin-bottom:14px;font-size:0.82rem">
      <div style="font-weight:600;color:var(--red);margin-bottom:6px">⚠ Tier won't route — no credentialed model</div>
      <div style="font-size:0.74rem;color:var(--text2);margin-bottom:8px">
        These roles have NO credentialed, resolvable model anywhere in their fallback chain, so OpenClaw can't route them at all. Copy a provider key from a bot that has it, or remove the dead entry and add a credentialed model.
      </div>
      ${botBlocks}
    </div>`;
}

// Render the SOFT same-vendor judge advisory (2026-06-19). `advisoryTiers`
// comes from the server's `advisory_tiers` side-car: every judge that ROUTES
// but on the same vendor as Standard. Provider diversity is a RECOMMENDATION,
// not a requirement — judge still routes, so this is an AMBER nudge ("add a
// cross-vendor model for independent checks"), never the red "won't route"
// panel. Each entry carries the doubled-up provider + per-cross-vendor-provider
// borrow candidates, reusing the same copy-creds flow (_aiDiversityBorrow) the
// hard-break panel and diversity card use.
function _aiRenderAdvisoryTiers(advisoryTiers) {
  if (!advisoryTiers || !advisoryTiers.length) return '';
  const byBot = {};
  for (const t of advisoryTiers) {
    if (!byBot[t.bot_id]) byBot[t.bot_id] = [];
    byBot[t.bot_id].push(t);
  }
  const botBlocks = Object.entries(byBot).map(([botId, tiers]) => {
    const rows = tiers.map(t => {
      const roleName = TIER_DISPLAY[t.role] || t.role;
      const sameVendor = providerLabel(t.same_vendor_provider);
      const cand = t.borrow_candidates || {};
      const cross = t.cross_vendor_providers || [];
      // One copy button per (cross-vendor provider × source bot that holds it).
      const copyButtons = cross.flatMap(prov => {
        const provName = providerLabel(prov);
        return (cand[prov] || []).map(src => `<button class="btn btn-secondary btn-sm"
          style="font-size:0.72rem;padding:3px 9px;margin:2px 4px 2px 0"
          onclick="_aiDiversityBorrow('${escHtml(botId)}','${escHtml(prov)}','${escHtml(src.bot_id)}')">
          Copy ${escHtml(provName)} from ${escHtml(botLabel(src.bot_id))}
        </button>`);
      }).join('');
      return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
        <div style="font-size:0.78rem;margin-bottom:4px">
          <strong>${escHtml(roleName)}</strong> is using the same vendor as Standard (${escHtml(sameVendor)}). It still routes — add a model from another provider for independent cross-vendor checks.
        </div>
        <div style="display:flex;flex-wrap:wrap;align-items:center">${copyButtons}</div>
      </div>`;
    }).join('');
    return `<div style="margin-bottom:8px">
      <div style="font-size:0.78rem;margin-bottom:4px"><strong>${escHtml(botLabel(botId))}</strong></div>
      ${rows}
    </div>`;
  }).join('');
  return `
    <div style="background:rgba(240,180,41,0.08);border:1px solid rgba(240,180,41,0.3);border-radius:6px;padding:10px 12px;margin-bottom:14px;font-size:0.82rem">
      <div style="font-weight:600;color:var(--yellow);margin-bottom:6px">Judge uses the same vendor as Standard</div>
      <div style="font-size:0.74rem;color:var(--text2);margin-bottom:8px">
        These judges route fine, but on the same provider as the Standard role they're checking. A different provider is recommended for cross-vendor checks but not required. Copy a provider key from a bot that has it to add a cross-vendor option.
      </div>
      ${botBlocks}
    </div>`;
}

// Render the DORMANT credential-gap advisory (spec §Addendum 10 §C). `credGap`
// is the subset of tier/role drift whose provider the bot has NO API key for
// (provider_credentialed === false) AND whose role STILL ROUTES (the server
// tagged tier_severity !== 'hard_break'). These entries are INERT — OpenClaw
// skips them at runtime, the role resolves to a credentialed model anyway, and
// they auto-activate if the operator adds that provider's key later. So this is
// surfaced QUIETLY (muted + collapsed-by-default), NEVER auto-stripped: "dormant
// — add creds to activate / remove". "Reconcile catalog" can't fix these (the
// server skips uncredentialed providers), so it offers copy-the-key
// (_aiDiversityBorrow) or remove-the-entry (_aiRemoveFromTier) instead. Genuine
// breaks are folded into the prominent hard-break panel above.
function _aiRenderCatalogCredGap(credGap) {
  if (!credGap || !credGap.length) return '';
  const byBot = {};
  for (const d of credGap) {
    if (!byBot[d.bot_id]) byBot[d.bot_id] = [];
    byBot[d.bot_id].push(d);
  }
  const botBlocks = Object.entries(byBot).map(([botId, drifts]) => {
    const rows = drifts.map(d => {
      const roleName = TIER_DISPLAY[d.tier] || d.tier;
      const provName = providerLabel(d.provider);
      // Copy-from-bot buttons — one per source bot that holds the provider key.
      // Reuses _aiDiversityBorrow (the same copy-creds flow the diversity card
      // drives): confirm → POST borrow → restart banner → re-check freshness.
      const sources = d.borrow_candidates || [];
      const copyButtons = sources.map(src => `<button class="btn btn-secondary btn-sm"
          style="font-size:0.72rem;padding:3px 9px;margin:2px 4px 2px 0"
          onclick="_aiDiversityBorrow('${escHtml(botId)}','${escHtml(d.provider)}','${escHtml(src.bot_id)}')">
          Copy ${escHtml(provName)} from ${escHtml(botLabel(src.bot_id))}
        </button>`).join('');
      // Remove-from-tier only applies to a tier-named model (tier_member_missing).
      // A role-resolved default (role_resolves_outside_catalog) has no tier
      // entry to strip, so it gets the copy-key path only. d.tier holds the
      // legacy tierN key for tier_member_missing — the key the saved tiers doc uses.
      const removeBtn = d.kind === 'tier_member_missing'
        ? `<button class="btn btn-ghost btn-sm"
            style="font-size:0.72rem;padding:3px 9px;margin:2px 4px 2px 0"
            onclick="_aiRemoveFromTier('${escHtml(botId)}','${escHtml(d.tier)}','${escHtml(d.model_id)}')">
            Remove from ${escHtml(roleName)}
          </button>`
        : '';
      const noSource = sources.length === 0
        ? `<span style="font-size:0.72rem;color:var(--text3)">No other bot has a ${escHtml(provName)} key — add one in Credentials, or remove the entry.</span>`
        : '';
      const phrase = d.kind === 'role_resolves_outside_catalog'
        ? `is the resolved model for <strong>${escHtml(roleName)}</strong>`
        : `is in <strong>${escHtml(roleName)}</strong>`;
      return `<div style="padding:6px 0;border-bottom:1px solid var(--border)">
        <div style="font-size:0.78rem;margin-bottom:4px">
          <code style="font-size:0.78rem">${escHtml(d.model_id)}</code> ${phrase} but ${escHtml(botLabel(botId))} has no ${escHtml(provName)} key — inert (the role still routes). Add a key to activate, or remove.
        </div>
        <div style="display:flex;flex-wrap:wrap;align-items:center">${copyButtons}${removeBtn}${noSource}</div>
      </div>`;
    }).join('');
    return `<div style="margin-bottom:8px">
      <div style="font-size:0.78rem;margin-bottom:4px"><strong>${escHtml(botLabel(botId))}</strong> · ${drifts.length} dormant fallback${drifts.length === 1 ? '' : 's'}</div>
      ${rows}
    </div>`;
  }).join('');
  const n = credGap.length;
  // Muted + collapsed-by-default: dormant entries are inert, not a break.
  return `
    <details style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin-bottom:14px;font-size:0.82rem">
      <summary style="font-weight:600;color:var(--text2);display:flex;align-items:center;gap:7px;cursor:pointer">
        <span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>
        ${n} dormant model fallback${n === 1 ? '' : 's'} — inert until you add the key
      </summary>
      <div style="font-size:0.74rem;color:var(--text3);margin:8px 0">
        These tier entries name models whose provider this bot has no API key for. OpenClaw skips them at runtime and the role resolves to a credentialed model anyway, so they're harmless — they'll auto-activate if you add the provider key later. Copy a key from a bot that has it, or remove the entry to tidy up.
      </div>
      ${botBlocks}
    </details>`;
}

// Remove a single model from one of a bot's tier definitions, then re-save via
// the SAME safe tier-write path the editor uses (PUT .../tiers, which re-runs
// the server-side reconcile pass). Used by the missing-credentials drift
// advisory to drop a dead entry naming an uncredentialed provider's model.
async function _aiRemoveFromTier(botId, tier, modelId) {
  const roleName = TIER_DISPLAY[tier] || tier;
  if (!await confirmModal({ body: `Remove ${modelId} from ${botLabel(botId)}'s ${roleName} tier?\n\nThe bot has no API key for this model's provider, so OpenClaw already ignores it at runtime. Removing it cleans up the dead entry — you can re-add it later if you add the provider key.`, danger: true })) return;
  const cfg = await api('GET', `/api/admin/config/${botId}`);
  if (!cfg || cfg.error) {
    toast('Could not read bot config: ' + (cfg?.error || 'unknown'), 'err');
    return;
  }
  const tiers = cfg.tiers || {};
  const entry = tiers[tier];
  if (!entry || !Array.isArray(entry.models)) {
    toast(`No ${roleName} tier to edit`, 'warn');
    return;
  }
  const before = entry.models.length;
  // Tier models may be plain strings or {id|model} objects — match both shapes.
  entry.models = entry.models.filter(m => {
    const mid = typeof m === 'string' ? m : (m && (m.id || m.model));
    return mid !== modelId;
  });
  if (entry.models.length === before) {
    toast('Entry already gone — refreshing', 'info');
    await _aiCheckModelFreshness();
    return;
  }
  // Send ONLY the tier we edited — never the full synthesized tiers dict.
  // Under the rungs/roles shape several legacy tiers can collapse onto one
  // rung (e.g. standard+judge → sonnet-class); resending an unchanged sibling
  // lets the server-side fold clobber this edit last-writer-wins (the write
  // "succeeds" but the model never moves). One tier can't collide with itself.
  const r = await api('PUT', `/api/admin/config/${botId}/tiers`, { tiers: { [tier]: entry } });
  if (!r || r.error) {
    toast('Remove failed: ' + (r?.error || 'unknown'), 'err');
    return;
  }
  toast(`Removed ${modelId} from ${roleName}`, 'ok');
  _aiShowRestartBanner();
  await _aiCheckModelFreshness();
  if (_aiBot === botId) await _aiRenderBotData(_aiBot);
}

// Reconcile catalog with current tier definitions. Calls the same
// endpoint as `evolve-admin reconcile-catalog` — adds every tier-
// referenced model to the bot's catalog. Idempotent, restart needed.
async function _aiReconcileCatalog(botId, opts = {}) {
  // `opts.quiet` suppresses the per-bot confirm + toast + panel refresh so
  // the bulk "Reconcile all" loop can drive this once per bot and report a
  // single aggregate result. Returns {ok, added, skipped, error} so the
  // caller can tally success/failure without re-reading the response shape.
  const quiet = !!opts.quiet;
  if (!quiet && !await confirmModal(`Reconcile catalog for ${botLabel(botId)}?\n\nThis adds every model referenced in tier definitions to the catalog so OpenClaw actually uses them at runtime. Safe and idempotent.`)) {
    return { ok: false, cancelled: true };
  }
  // We don't have a dedicated endpoint for this yet; rewrite the
  // catalog via the existing PUT endpoint after computing the union
  // client-side from the current freshness data. (Server-side, the
  // tiers-set endpoint now auto-reconciles too — so re-saving the
  // tiers would also fix it. We re-save tiers as the simplest path
  // since the existing endpoint will run the same reconcile pass.)
  // Fetch current config to grab tiers
  const cfg = await api('GET', `/api/admin/config/${botId}`);
  if (!cfg || cfg.error) {
    const err = cfg?.error || 'unknown';
    if (!quiet) toast('Could not read bot config: ' + err, 'err');
    return { ok: false, error: err };
  }
  const tiers = cfg.tiers || {};
  // Re-save the same tiers — server's reconcile pass will add the
  // missing models to catalog automatically.
  const r = await api('PUT', `/api/admin/config/${botId}/tiers`, { tiers });
  if (!r || r.error) {
    const err = r?.error || 'unknown';
    if (!quiet) toast('Reconcile failed: ' + err, 'err');
    return { ok: false, error: err };
  }
  const added = r.catalog_auto_added || [];
  const skipped = r.skipped_uncredentialed || [];
  if (!quiet) {
    // Build a message that names what we did AND what we left aspirational.
    // "Added X, skipped Y (no API key)" is more useful than "Added X" alone —
    // explains why the drift advisory may still show entries after a
    // reconcile (the leftover entries are tier-referenced models from
    // providers the bot doesn't have a key for; OC silently drops them at
    // runtime, which is the safe behaviour — adding them to the catalog
    // without an API key would error at runtime).
    let msg;
    if (added.length === 0 && skipped.length === 0) {
      msg = 'Already in sync';
    } else if (added.length > 0 && skipped.length === 0) {
      msg = `Added ${added.length} model${added.length === 1 ? '' : 's'} to catalog`;
    } else if (added.length === 0 && skipped.length > 0) {
      msg = `No models added — ${skipped.length} skipped (no API key for those providers)`;
    } else {
      msg = `Added ${added.length}, skipped ${skipped.length} (no API key)`;
    }
    toast(msg, 'ok');
    // Refresh the freshness panel so the drift advisory updates.
    _aiCheckModelFreshness();
    // Also refresh the catalog chips panel if the reconciled bot is the
    // currently-selected one — otherwise the chips stay stale and the
    // operator can't see that the new models were actually added, which
    // reads as "the button did nothing."
    if (_aiBot === botId) {
      await _aiRenderBotData(_aiBot);
    }
  }
  return { ok: true, added, skipped };
}

// Bulk-reconcile every bot that currently has catalog drift. Mirrors the
// freshness "Apply all" affordance: one confirm summarizing per-bot drift
// counts, then a sequential loop over the per-bot reconcile endpoint with
// an aggregate success/failure toast. Sequential (not a single bulk route)
// because reconcile is a re-save of each bot's own tiers — there is no
// cross-bot batching to win, and the per-bot endpoint already does the
// reconcile pass server-side. Pulls the bot list from the cached freshness
// summary so it reconciles exactly the bots the drift card is showing.
async function _aiReconcileAllCatalog() {
  const summary = _aiLastFreshnessSummary;
  const driftFindings = (summary && summary.drift_findings) || [];
  // Match the drift card's filter: only the tier/role drift kinds get a
  // Reconcile button (recommended_missing is informational, no reconcile).
  const tierDrift = driftFindings.filter(
    d => d.kind === 'tier_member_missing' || d.kind === 'role_resolves_outside_catalog',
  );
  const byBot = {};
  for (const d of tierDrift) {
    if (!byBot[d.bot_id]) byBot[d.bot_id] = 0;
    byBot[d.bot_id]++;
  }
  const bots = Object.keys(byBot);
  if (bots.length === 0) return;

  const botSummary = Object.entries(byBot)
    .sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
    .map(([bot, n]) => `  • ${bot}: ${n} drift${n === 1 ? '' : 's'}`)
    .join('\n');
  const msg = `Reconcile catalog for all ${bots.length} bot${bots.length === 1 ? '' : 's'} with drift?\n\n${botSummary}\n\nThis adds every tier-referenced model to each bot's catalog so OpenClaw uses them at runtime. Safe and idempotent. Restart each gateway after for changes to take effect on new sessions.`;
  if (!await confirmModal(msg)) return;

  let okCount = 0;
  const failures = [];
  // Sequential so we don't fan out N concurrent sudo /bin/cp writes; each
  // bot is independent, a failure on one doesn't abort the rest.
  for (const botId of bots) {
    const res = await _aiReconcileCatalog(botId, { quiet: true });
    if (res && res.ok) okCount++;
    else failures.push(`${botId}: ${res?.error || 'unknown'}`);
  }

  if (failures.length === 0) {
    toast(`Reconciled ${okCount} bot${okCount === 1 ? '' : 's'}`, 'ok');
  } else {
    const sample = failures.slice(0, 3).join('; ');
    const more = failures.length > 3 ? ` (+${failures.length - 3} more)` : '';
    toast(`Reconciled ${okCount}/${bots.length}. Failed: ${sample}${more}`, 'warn');
  }
  // Single refresh after the whole batch (quiet mode skipped per-bot
  // refreshes) so the drift card reflects what actually landed.
  _aiCheckModelFreshness();
  if (_aiBot && bots.includes(_aiBot)) {
    await _aiRenderBotData(_aiBot);
  }
}

async function _aiLoadFreshnessStatus() {
  const [r, disc] = await Promise.all([
    api('GET', '/api/models/freshness-status'),
    api('GET', '/api/models/discoveries').catch(() => null),
  ]);
  _aiLastDiscoveries = (disc && Array.isArray(disc.discoveries)) ? disc.discoveries : [];
  _aiLastNewTiers = (disc && Array.isArray(disc.new_tiers)) ? disc.new_tiers : [];
  _aiLastUpgrades = (disc && Array.isArray(disc.upgrades)) ? disc.upgrades : [];
  if (disc && Array.isArray(disc.roles) && disc.roles.length) _aiPickerRoles = disc.roles;
  _aiFreshnessRender(r && !r.error ? r : null);
  // The operator is now looking at the card → acknowledge these discoveries so
  // the nav badge clears; it re-appears for genuinely-new ones on the next poll.
  _aiMarkDiscoSeenAndClearBadge();
}

async function _aiCheckModelFreshness() {
  const btn = document.getElementById('ai-freshness-check-btn');
  const panel = document.getElementById('ai-freshness-panel');
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  if (panel) panel.innerHTML = '<div class="loading"><div class="spinner"></div> Checking each bot against the registry…</div>';
  try {
    const r = await api('POST', '/api/models/check-freshness', {});
    if (r.error) {
      toast(r.error, 'err');
      if (panel) panel.innerHTML = `<div class="empty" style="color:var(--danger);font-size:0.82rem">${escHtml(r.error)}</div>`;
      return;
    }
    _aiFreshnessRender(r);
    const n = r.advisory_count || 0;
    toast(n === 0 ? 'All tiers current' : `${n} update${n===1?'':'s'} available`, n === 0 ? 'ok' : 'info');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Check Now'; }
  }
}

// Show / hide the "Apply All" header button based on whether the
// currently-rendered summary has any advisories. Label includes the
// count so the operator knows what they're about to commit to before
// clicking — no "click to find out how many" surprises.
function _aiSyncApplyAllButton(summary) {
  const btn = document.getElementById('ai-freshness-apply-all-btn');
  if (!btn) return;
  const advisories = (summary && summary.advisories) || [];
  if (advisories.length === 0) {
    btn.style.display = 'none';
    return;
  }
  btn.style.display = '';
  btn.disabled = false;
  btn.textContent = `Apply All (${advisories.length})`;
}

// Surface the advisory count in the collapsed Model Freshness summary so an
// operator knows to expand the (collapsed-by-default) card when drift exists.
// Counts every actionable finding class — tier advisories, catalog drift,
// diversity, redundant-config, and hard-break tiers — so the badge reflects
// total work, not just the Apply-All-eligible tier updates.
function _aiSyncFreshnessSummaryCount(summary) {
  const el = document.getElementById('ai-freshness-summary-count');
  if (!el) return;
  // Hard breaks backed by a drift finding are already counted in drift_findings;
  // add only the pure-resolution ones (no offending tier entry) so we surface a
  // judge-with-no-diverse-provider break without double-counting the rest.
  const extraHardBreaks = ((summary && summary.hard_break_tiers) || [])
    .filter(t => !((t.offending_models) || []).length).length;
  // Discoveries (new models to adopt) count too — they're signal-sourced, so
  // they show even before a staleness check has run (summary may be null).
  const discoveries = _aiLastDiscoveries.length;
  // The credential-store-unreadable banner is one actionable advisory when set.
  const credStoreUnreadable = (summary && summary.credential_store_unreadable) ? 1 : 0;
  const n = ((summary && summary.advisories) || []).length
    + ((summary && summary.drift_findings) || []).length
    + ((summary && summary.diversity_advisories) || []).length
    + ((summary && summary.redundant_bots) || []).length
    + ((summary && summary.advisory_tiers) || []).length
    + extraHardBreaks
    + credStoreUnreadable
    + discoveries;
  if (n === 0) {
    el.textContent = '';
    el.style.color = 'var(--text3)';
    return;
  }
  el.textContent = `· ${n} advisor${n === 1 ? 'y' : 'ies'}`;
  el.style.color = 'var(--yellow)';
}

// Bulk-apply every advisory in one server round-trip. Groups by bot
// internally (see /api/models/update-tier-bulk) so a 50-advisory pod
// is ~8 OC-CLI invocations instead of ~100. The advisory list comes
// from the cached _aiLastFreshnessSummary so we don't have to re-run
// Check Now first — operator sees the same rows we're about to apply.
async function _aiApplyAllFreshness() {
  const summary = _aiLastFreshnessSummary;
  const advisories = (summary && summary.advisories) || [];
  if (advisories.length === 0) return;

  // Group by bot for the confirm dialog so the operator sees scope, not
  // a flat 50-line dump. Sorted: most-affected bots first, then alpha.
  const byBot = {};
  for (const a of advisories) {
    if (!byBot[a.bot_id]) byBot[a.bot_id] = 0;
    byBot[a.bot_id]++;
  }
  const botSummary = Object.entries(byBot)
    .sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
    .map(([bot, n]) => `  • ${bot}: ${n} update${n === 1 ? '' : 's'}`)
    .join('\n');
  const msg = `Apply all ${advisories.length} freshness updates across ${Object.keys(byBot).length} bot${Object.keys(byBot).length === 1 ? '' : 's'}?\n\n${botSummary}\n\nThis rewrites each bot's tier definitions and adds any new models to their catalog. Restart the gateway after for changes to take effect on new sessions.`;
  if (!await confirmModal(msg)) return;

  const btn = document.getElementById('ai-freshness-apply-all-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Applying…'; }

  const payload = {
    updates: advisories.map(a => ({
      bot_id: a.bot_id, tier: a.tier,
      provider: a.provider, model: a.recommended_model,
    })),
  };

  try {
    const r = await api('POST', '/api/models/update-tier-bulk', payload);
    if (r.error) {
      toast(r.error, 'err');
      return;
    }
    if (r.failed > 0) {
      // Some succeeded, some didn't — name the first few failures so
      // the operator knows what to look at. Toast keeps it short;
      // the freshness re-render will leave the failed rows visible.
      const fails = (r.results || []).filter(x => !x.success);
      const sample = fails.slice(0, 3).map(f => `${f.bot_id}/${f.tier}`).join(', ');
      const more = fails.length > 3 ? ` (+${fails.length - 3} more)` : '';
      toast(`Applied ${r.applied}/${r.total}. Failed: ${sample}${more}`, 'warn');
    } else {
      toast(`Applied ${r.applied} update${r.applied === 1 ? '' : 's'} across ${r.bots_touched} bot${r.bots_touched === 1 ? '' : 's'}`, 'ok');
    }
    _aiShowRestartBanner();
    // Re-run the check so the table reflects what actually landed.
    // The check is one OC call per bot — already pretty fast.
    await _aiCheckModelFreshness();
    // If the currently-selected bot tab was in the batch, refresh
    // its panels so the new models show in the per-bot tier definitions.
    if (_aiBot && payload.updates.some(u => u.bot_id === _aiBot)) {
      await _aiRenderBotData(_aiBot);
    }
  } catch (e) {
    toast('Bulk apply failed: ' + (e?.message || String(e)), 'err');
  } finally {
    if (btn) { btn.disabled = false; }
    // Re-sync from the new summary _aiCheckModelFreshness wrote.
    _aiSyncApplyAllButton(_aiLastFreshnessSummary);
  }
}

async function _aiUpdateRecommendedModel(advisory) {
  if (!advisory || !advisory.bot_id) return;
  const msg = `Update ${advisory.bot_id} ${TIER_DISPLAY[advisory.tier] || advisory.tier} `
    + `from ${advisory.current_model || '(none)'} to ${advisory.recommended_model}?\n\n`
    + `This rewrites evolve-tiers.json. Restart the gateway after the update for the change to take effect on new sessions.`;
  if (!await confirmModal(msg)) return;

  const r = await api('POST', '/api/models/update-tier', {
    bot_id: advisory.bot_id,
    tier: advisory.tier,
    provider: advisory.provider,
    model: advisory.recommended_model,
  });
  if (r.error) {
    toast(r.error, 'err');
    return;
  }
  toast(`Updated ${advisory.bot_id} ${advisory.tier}`, 'ok');
  _aiShowRestartBanner();
  // Re-run the check so the row disappears and the summary updates
  await _aiCheckModelFreshness();
  // If the bot whose tier just changed is the currently-selected bot,
  // refresh its panels so the user sees the new tier model immediately.
  if (_aiBot === advisory.bot_id) {
    await _aiRenderBotData(_aiBot);
  }
}

// ── Section 1: Model Catalog ──────────────────────────────────────────────────

async function _aiRefreshBotConfig(botId) {
  await _aiRenderBotData(botId);
}

// The providers THIS bot holds a key for, read off the server-computed
// keyStatus map (provider→bool). Lower-cased to match the listings' provider
// keys. Returns [] when keyStatus is absent/empty so callers fall open to the
// pod-wide credentialed listing rather than blanking the picker. No provider
// literals — the set is purely whatever auth-profiles the bot carries.
function _aiBotCredentialedProviders(config) {
  const keyStatus = (config && config.keyStatus) || {};
  return Object.keys(keyStatus)
    .filter(p => keyStatus[p])
    .map(p => String(p).toLowerCase());
}

function _aiRenderCatalog(botId, config) {
  const el = document.getElementById('ai-mgmt-panel');
  const lbl = document.getElementById('ai-mgmt-bot-label');
  if (!el) return;
  if (lbl) lbl.textContent = '(' + botLabel(botId) + ')';

  const catalog = config.catalog || [];
  const keyStatus = config.keyStatus || {};

  const getProvider = model => {
    const slash = model.indexOf('/');
    return slash > -1 ? model.slice(0, slash) : null;
  };

  // This bot's credentialed providers, derived from keyStatus (provider→bool,
  // server-computed from the bot's auth-profiles). No provider literals — the
  // set is whatever keys the bot actually holds. Used to scope the catalog
  // picker to providers the bot can run (the listings cache is the pod union;
  // a bot must not be offered an xai/ model when only another bot keys xAI).
  // Empty → the picker falls open to the pod-wide credentialed listing.
  const botCredentialedProviders = _aiBotCredentialedProviders(config);

  // The catalog IS the pool the bot's tiers draw from (Phase 10b / Addendum 7
  // workstream C): its add-model control offers the full credentialed-provider
  // listing — the exhaustive grouped picker — plus the validated free-text
  // "type a model ID" path, BOTH scoped to this bot's credentialed providers.
  // The latest-in-family option per provider is marked (boldLatest) to cut
  // version confusion. This is the ONLY surface with the exhaustive listing +
  // free-text; the bot tier editor is constrained to this catalog, the pod tier
  // editor to the (pod-wide) listings, neither offers typing.
  const addRow = _aiModelPickerRow({
    already: catalog,
    pickFnName: `(v=>_aiCatalogAddModel('${escHtml(botId)}',v))`,
    boldLatest: true,
    freeText: true,
    credentialedProviders: botCredentialedProviders,
    inputId: 'ai-mgmt-add-input',
    addFnName: `(()=>_aiCatalogAdd('${escHtml(botId)}'))`,
    rerenderFnName: '_aiCatalog',
  });

  // Catalog chips now use the unified provider-colored _aiModelChip (Phase 10a
  // catalog⇄tier unification) — same component as the tier rows. The
  // provider-key-status ● dot (green = key present, red = no API key for that
  // provider) is folded INTO the chip via keyDot so the "no key for X"
  // affordance is preserved. The inline × is wired to _aiCatalogRemove.
  const pills = catalog.map(m => {
    const provider = getProvider(m);
    const hasKey = provider ? !!keyStatus[provider] : true;
    const dot = provider
      ? `<span class="ai-chip-keydot" style="color:${hasKey ? 'var(--green)' : 'var(--danger)'}"
           title="${hasKey ? 'Provider has API key' : 'No API key for ' + escHtml(provider)}">●</span>`
      : '';
    return _aiModelChip(m, provider || '', {
      keyDot: dot,
      onRemove: `_aiCatalogRemove('${escHtml(botId)}','${escHtml(m)}')`,
    });
  }).join('');

  el.innerHTML = `
    <div style="font-size:0.8rem;color:var(--text2);margin-bottom:10px">
      The model catalog controls which models this bot can use. Click <strong>×</strong> to remove a model, or add a new one below.
      Changes take effect after gateway restart.
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px;min-height:32px;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:8px">
      ${pills || '<span style="color:var(--text3);font-size:0.8rem;font-style:italic">No models in catalog</span>'}
    </div>
    ${!pills ? `
      <div style="margin:-10px 0 14px 0;padding:10px 12px;background:rgba(255,183,77,0.08);border:1px solid rgba(255,183,77,0.3);border-radius:6px;font-size:0.8rem;color:var(--text2)">
        <b style="color:var(--yellow)">Catalog is empty.</b>
        Without a model in the catalog, the bot's agent falls back to OpenClaw's bundled default
        (usually <code style="font-size:0.74rem">openai/gpt-*</code>) — which fails if you haven't configured an OpenAI key or are using a different LLM provider (see <a href="#" onclick="nav(document.querySelector('[data-page=plugins]'));return false" style="color:var(--accent)">Plugins → Credentials</a>).
        <button class="btn btn-primary btn-sm" onclick="_aiSeedDefaultCatalog('${escHtml(botId)}')" style="margin-left:8px;padding:3px 10px">
          Seed defaults
        </button>
        <span style="color:var(--text3);font-size:0.74rem;margin-left:4px">— uses RECOMMENDED models for the providers this bot has keys for</span>
      </div>` : ''}
    ${addRow}
    <div id="ai-mgmt-result" style="margin-top:8px;font-size:0.8rem"></div>`;
}

// Seed a default model catalog + tier assignments for bots whose
// openclaw.json has no model.primary. Pairs with the "Seed defaults"
// button in the empty-catalog warning (this PR adds that warning
// above the catalog list). Calls /api/wizard/seed-model-config,
// which picks the bot's first available LLM provider from its
// auth-profiles and writes the recommended models from
// packages/analyzer/model_registry.RECOMMENDED.
async function _aiSeedDefaultCatalog(botId) {
  if (!await confirmModal(`Seed default model catalog for ${botLabel(botId)}?\n\nThis will write the recommended models for whichever LLM provider ${botLabel(botId)} has a key for (e.g. Anthropic, OpenAI). Safe to skip if you already have specific models in mind.`)) return;
  const resultEl = document.getElementById('ai-mgmt-result');
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text2)">Seeding defaults…</span>';
  const d = await api('POST', '/api/wizard/seed-model-config', { bot_id: botId });
  if (d && d.error) {
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(d.error)}</span>`;
    toast('Seed failed: ' + d.error, 'err');
    return;
  }
  if (!d.seeded) {
    toast(d.reason || 'Nothing to seed', 'info');
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--text2)">${escHtml(d.reason || '')}</span>`;
    return;
  }
  toast(`Seeded ${d.catalog_count} ${d.provider} models`, 'ok');
  if (resultEl) resultEl.innerHTML = `<span style="color:var(--green)">✓ ${escHtml(d.reason || '')}</span>`;
  // Refresh the AI Optimization page so the catalog + tier pills appear
  if (typeof loadAiOptimization === 'function') loadAiOptimization();
  _aiShowRestartBanner();
}

async function _aiCatalogRemove(botId, model) {
  const tiers = _aiBotConfig.tiers || {};
  const tierWithModel = _aiTierForModel(model, tiers);
  let msg = `Remove "${model}" from catalog?`;
  if (tierWithModel) {
    const roleName = TIER_DISPLAY[tierWithModel] || tierWithModel;
    msg += `\n\nWarning: this model is also assigned to the ${roleName} role. It will be removed from that role too.`;
  }
  if (!await confirmModal({ body: msg, danger: true })) return;

  const newCatalog = _aiCatalog.filter(m => m !== model);
  const resultEl = document.getElementById('ai-mgmt-result');
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  const d = await api('PUT', '/api/admin/config/' + botId + '/catalog', { catalog: newCatalog });
  if (d.error) {
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(d.error)}</span>`;
    toast('Failed to remove model', 'err');
  } else {
    _aiBotConfig.catalog = newCatalog;
    _aiCatalog = newCatalog;
    if (resultEl) resultEl.innerHTML = '';
    _aiRenderCatalog(botId, _aiBotConfig);
    _aiRenderTiers(botId, _aiBotConfig);
    _aiRenderFallback(botId, _aiBotConfig);
    toast('Removed ' + model, 'ok');
    _aiShowRestartBanner();
  }
}

// Add one already-canonical model id to the bot's catalog (the picker's
// onPick path — the id came straight from the listings, so it's valid). Shared
// by the grouped-picker selection and the validated free-text path below.
async function _aiCatalogAddModel(botId, model) {
  if (!model) { toast('Enter a model name', 'err'); return; }
  if (_aiCatalog.includes(model)) { toast('Model already in catalog', 'warn'); return; }

  const getProvider = m => { const s = m.indexOf('/'); return s > -1 ? m.slice(0, s) : null; };
  const provider = getProvider(model);
  const keyStatus = _aiBotConfig.keyStatus || {};
  if (provider && keyStatus[provider] === false) {
    toast('No API key found for ' + provider + '. Add one in Keys & Usage.', 'warn');
  }

  const newCatalog = [..._aiCatalog, model];
  const resultEl = document.getElementById('ai-mgmt-result');
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  const d = await api('PUT', '/api/admin/config/' + botId + '/catalog', { catalog: newCatalog });
  if (d.error) {
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(d.error)}</span>`;
    toast('Failed to add model', 'err');
  } else {
    _aiBotConfig.catalog = newCatalog;
    _aiCatalog = newCatalog;
    if (resultEl) resultEl.innerHTML = '';
    _aiRenderCatalog(botId, _aiBotConfig);
    _aiRenderTiers(botId, _aiBotConfig);
    toast('Added ' + model, 'ok');
    _aiShowRestartBanner();
  }
}

// Catalog free-text "add anyway" (catalog-only, Phase 10b): validate the typed
// id against the listings (nearest-match suggestion on a miss) and add only the
// canonical spelling. The grouped picker covers the common path; this is the
// escape hatch for a model not yet in the listings.
async function _aiCatalogAdd(botId) {
  const input = document.getElementById('ai-mgmt-add-input');
  if (!input) return;
  const typed = input.value;
  // Scope free-text validation to THIS bot's credentialed providers (server +
  // local index) so the bot can't add a model from a provider it can't run.
  _aiValidateAndAdd(typed, (canonical) => {
    input.value = '';
    _aiCatalogAddModel(botId, canonical);
  }, {
    botId,
    credentialedProviders: _aiBotCredentialedProviders(_aiBotConfig),
  });
}

// Refresh hook for the catalog picker's "Refresh" listings control.
function _aiCatalog_refresh() {
  _aiRefreshListings(() => _aiRenderCatalog(_aiBot, _aiBotConfig));
}

// ── Section 2: Tier Definitions ────────────────────────────────────────────────

// Per-bot tier-definition metadata. storeKey is the buffer key the Custom-mode
// editor and the save path fold under (tierN for the four legacy roles, the
// role key `max` for the frontier rung). Hints describe each role's purpose.
const _AI_TIER_ROLE_META = {
  fast:     { storeKey: 'tier3', hint: 'Background tasks: analysis, summarization, maintenance. Users never see its output.' },
  standard: { storeKey: 'tier2', hint: 'Runs every human-facing turn — required.' },
  power:    { storeKey: 'tier1', hint: 'Reserved for explicit user requests; daily-capped (default 10/day).' },
  max:      { storeKey: 'max',   hint: 'The frontier model — above Power, premium cost. Pull-only and daily-capped (default 5/day).' },
  judge:    { storeKey: 'tier0', hint: 'Evaluates session outcomes. Defaults to the Standard model if empty. A different provider is recommended for cross-vendor checks but not required.' },
};

// Section 2: Tier Definitions — per-bot Use-defaults/Custom toggle (spec
// §Addendum 5). STRICT all-or-nothing: a bot is either "Use pod defaults"
// (inherits the merged default — read-only preview) or "Custom" (its own full
// editable tier set). The single toggle IS the control; there are no per-tier
// provenance badges or per-tier Customize/Revert (those were the Phase 7
// design this replaces). Custom-vs-default is read from config.customTiers,
// which the config GET derives from the bot's RAW per-bot rungs/roles (never
// from the merged view, which always carries default values).
function _aiRenderTiers(botId, config) {
  const el = document.getElementById('ai-tiers-panel');
  const lbl = document.getElementById('ai-tiers-bot-label');
  if (!el) return;
  if (lbl) lbl.textContent = '(' + botLabel(botId) + ')';

  const isCustom = !!(config && config.customTiers);
  if (isCustom) {
    _aiRenderTiersCustom(botId, config, el);
  } else {
    _aiRenderTiersUseDefaults(botId, config, el);
  }
}

// Resolved-role header (label + rank meter + cost chip). Shared by both modes;
// it carries no provenance badge — provenance is now a whole-bot property, not
// a per-tier one. Derived purely from the resolver view, no model literals.
function _aiTierRoleHeader(roleId, rv) {
  const meter = roleId === 'judge' ? '' : _aiRankMeter(rv.rank, rv.rungCount);
  const costChip = _aiCostClassChip(rv.costClass);
  return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:2px">
      <span style="font-weight:600;font-size:0.85rem">${escHtml(_AI_ROLE_LABELS[roleId] || roleId)}</span>
      ${meter}${costChip}
    </div>`;
}

// ── Credential-severity rows (spec §Addendum 10 §C — 12c) ──────────────────
// Returns the advisory row(s) for a role based on credential resolution state.
//
//  • Hard-break  — rv.available === false AND rv.unavailableReason === "uncredentialed"
//    The entire rung chain has no credentialed model; routing genuinely fails.
//    Shown prominently in amber so the operator knows this needs action.
//
//  • Same-vendor judge advisory — rv.available !== false AND
//    rv.advisoryReason === "same_vendor_as_standard". The judge ROUTES, but on
//    the same provider as Standard. Provider diversity is a recommendation, not
//    a requirement (2026-06-19), so this is a soft amber nudge, NOT a hard
//    break. rv.advisoryProvider carries the doubled-up provider (derived data).
//
//  • Soft-dormant — rv.available !== false AND rv.inertProviders is non-empty.
//    The role resolves fine, but inert non-credentialed fallbacks are present.
//    Quiet muted row: "dormant — auto-activates when you add <provider> creds".
//    This is a REVERSIBLE, EXPECTED state for "Use pod defaults" bots (the pod
//    template is full; each bot ignores entries it can't auth — by design).
//
// None of these fire when credentials are unknown (inertProviders == [],
// advisoryReason == null, available stays true via fail-open), so existing
// presentation readers are safe.
function _aiRoleCredSeverityRows(rv) {
  const available = rv.available !== false;
  const inert = rv.inertProviders || [];

  // Hard-break: no credentialed model at all for this role.
  if (!available && rv.unavailableReason === 'uncredentialed') {
    return `<div style="background:rgba(240,180,41,0.1);border:1px solid rgba(240,180,41,0.35);border-radius:5px;padding:7px 10px;margin-bottom:6px;font-size:0.78rem">
      <span style="font-weight:600;color:var(--yellow)">No credentialed model for this role</span>
      <span style="color:var(--text2);margin-left:6px">Add a provider key or copy one from another bot — routing cannot resolve this role until at least one provider in the rung is credentialed.</span>
    </div>`;
  }

  // Same-vendor judge advisory: routes fine, but on Standard's vendor. Soft
  // amber nudge to add a cross-vendor model — never a routing failure.
  if (available && rv.advisoryReason === 'same_vendor_as_standard') {
    const vendor = _aiProviderLabel(rv.advisoryProvider);
    const vendorTxt = vendor ? ` (${escHtml(vendor)})` : '';
    return `<div style="background:rgba(240,180,41,0.1);border:1px solid rgba(240,180,41,0.35);border-radius:5px;padding:7px 10px;margin-bottom:6px;font-size:0.78rem">
      <span style="font-weight:600;color:var(--yellow)">Same vendor as Standard${vendorTxt}</span>
      <span style="color:var(--text2);margin-left:6px">This role still routes. A different provider is recommended for independent cross-vendor checks but not required — add a model from another provider to this rung.</span>
    </div>`;
  }

  // Soft-dormant: resolves fine but carries non-credentialed fallbacks.
  if (available && inert.length > 0) {
    const provList = inert.map(p => `<span style="font-weight:600">${escHtml(_aiProviderLabel(p))}</span>`).join(', ');
    return `<div style="background:var(--bg3);border:1px solid var(--border);border-radius:5px;padding:5px 10px;margin-bottom:6px;font-size:0.76rem;color:var(--text3)">
      Dormant fallback${inert.length > 1 ? 's' : ''}: ${provList} — auto-activates when you add credentials, or remove from this tier.
    </div>`;
  }

  return '';
}

// ── Use-pod-defaults mode: read-only preview + "Customize this bot" ──────────
// Renders all five resolved clusters (from config.roles, the
// resolve_roles_with_provenance view) read-only — no per-tier edit affordances.
// The Phase 7 merged-view rendering survives HERE as the preview, demoted from
// a control to an informational display.
function _aiRenderTiersUseDefaults(botId, config, el) {
  const rolesView = (config && config.roles) || {};

  const renderRole = (roleId) => {
    const meta = _AI_TIER_ROLE_META[roleId];
    const rv = rolesView[roleId] || {};
    const available = rv.available !== false;
    const dimStyle = available ? '' : 'opacity:0.6';
    const hintRow = meta.hint
      ? `<div style="font-size:0.75rem;color:var(--text3);margin-bottom:6px">${escHtml(meta.hint)}</div>`
      : '';
    // Generic unavail row for non-uncredentialed reasons (cap_exhausted, unconfigured).
    // Hard-break (uncredentialed) is handled by _aiRoleCredSeverityRows below.
    const unavailRow = !available && rv.unavailableReason && rv.unavailableReason !== 'uncredentialed'
      ? `<div style="font-size:0.74rem;color:var(--yellow);margin-bottom:6px">Unavailable on this bot — ${escHtml(_aiAvailabilityReasonText(rv.unavailableReason, roleId))}</div>`
      : '';
    const credSeverityRow = _aiRoleCredSeverityRows(rv);
    // Resolved model list from the resolver — rung models, else the single
    // availability-resolved model. Never hardcoded names.
    const resolved = (Array.isArray(rv.models) && rv.models.length)
      ? rv.models
      : (rv.resolvedModel ? [rv.resolvedModel] : []);
    const modelRows = resolved.length
      ? resolved.map(m => `<div style="font-family:monospace;color:var(--text2);font-size:0.82rem;padding:2px 0">${escHtml(m.replace('anthropic/', ''))}</div>`).join('')
      : `<div style="font-size:0.8rem;color:var(--text3);padding:3px 0;font-style:italic">(no model resolves for this role)</div>`;
    return `<div style="margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--border);${dimStyle}">
      ${_aiTierRoleHeader(roleId, rv)}
      ${hintRow}
      ${credSeverityRow}
      ${unavailRow}
      <div style="padding-left:4px">${modelRows}</div>
    </div>`;
  };

  const ladderSections = _AI_LADDER_ROLES.map(renderRole).join('');
  const judgeSection = `
    <div style="border-top:1px solid var(--border);margin:4px 0 16px 0"></div>
    ${renderRole('judge')}`;

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;padding:10px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:6px">
      <span class="ai-layer-chip ai-layer-default">Use pod defaults</span>
      <span style="font-size:0.78rem;color:var(--text2)">This bot inherits the pod's tier definitions (provider-matched to its credentials). The clusters below are read-only.</span>
      <button class="btn btn-ghost btn-sm" style="margin-left:auto" onclick="_aiOpenEasySetup('${escHtml(botId)}')" id="ai-tiers-easy-btn" title="Pick a provider order; Evolve fills every tier with sensible defaults in that order (switches this bot to Custom)">Easy setup</button>
      <button class="btn btn-primary btn-sm" onclick="_aiCustomizeBot()" id="ai-tiers-customize-btn">Customize this bot</button>
    </div>
    ${ladderSections}
    ${judgeSection}
    <span id="ai-tiers-save-status" style="font-size:0.8rem"></span>`;
}

// ── Custom mode: editable per-tier list + "Reset to pod defaults" ───────────
// The full per-bot editor (this bot defines its own rungs/roles). No per-tier
// provenance badge or per-tier Customize/Revert — the whole-bot toggle owns
// the inherit/override decision.
function _aiRenderTiersCustom(botId, config, el) {
  const rolesView = (config && config.roles) || {};

  const renderRole = (roleId) => {
    const meta = _AI_TIER_ROLE_META[roleId];
    const tid = meta.storeKey;
    const rv = rolesView[roleId] || {};
    const available = rv.available !== false;
    const dimStyle = available ? '' : 'opacity:0.6';
    const tobj = _aiPendingTiers[tid] || { models: [] };
    const models = tobj.models || [];
    const hintRow = meta.hint
      ? `<div style="font-size:0.75rem;color:var(--text3);margin-bottom:6px">${escHtml(meta.hint)}</div>`
      : '';
    // Generic unavail row for non-uncredentialed reasons (cap_exhausted, unconfigured).
    // Hard-break (uncredentialed) is handled by _aiRoleCredSeverityRows below.
    const unavailRow = !available && rv.unavailableReason && rv.unavailableReason !== 'uncredentialed'
      ? `<div style="font-size:0.74rem;color:var(--yellow);margin-bottom:6px">Unavailable on this bot — ${escHtml(_aiAvailabilityReasonText(rv.unavailableReason, roleId))}</div>`
      : '';
    const credSeverityRow = _aiRoleCredSeverityRows(rv);

    const modelRows = models.map((m, i) =>
        _aiTierModelRow('bot', tid, m, i,
          `(idx=>_aiTierRemoveModel('${escHtml(tid)}',idx))`, models.length)
      ).join('') || `<div style="font-size:0.8rem;color:var(--text3);padding:3px 0;font-style:italic">(empty — add models below)</div>`;

    // Bot tier editor is constrained to the bot's CATALOG (Phase 10b /
    // Addendum 7 workstream C): the catalog is the pool the tiers draw from, so
    // the picker offers only models already in `_aiCatalog`, organized by
    // provider. The soft "recommended for this tier" set (★ suggested — spec
    // §Addendum 6) is the role's RESOLVED DEFAULT CLUSTER (`rv.defaultModels`:
    // the pod-merged, credential-matched default — what "Reset to pod defaults"
    // would put here), NOT the bot's current `models` (which are all in
    // `already`, so suggesting them would star nothing). Already-present
    // recommended models stay visible in the dropdown, greyed "(already added)".
    // No free-text and no exhaustive listing here — those live on the catalog.
    // To add a model not in the pool, add it to the catalog first.
    const addRow = _aiModelPickerRow({
      pool: _aiCatalog,
      suggested: (rv.defaultModels || []),
      already: models,
      pickFnName: `(v=>_aiTierAddModel('${escHtml(tid)}',v))`,
      freeText: false,
    });

    return `<div style="margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--border);${dimStyle}">
      ${_aiTierRoleHeader(roleId, rv)}
      ${hintRow}
      ${credSeverityRow}
      ${unavailRow}
      <div style="padding-left:4px">${modelRows}</div>
      ${addRow}
    </div>`;
  };

  const ladderSections = _AI_LADDER_ROLES.map(renderRole).join('');
  const judgeSection = `
    <div style="border-top:1px solid var(--border);margin:4px 0 16px 0"></div>
    ${renderRole('judge')}`;

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;padding:10px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:6px">
      <span class="ai-layer-chip ai-layer-bot">Custom</span>
      <span style="font-size:0.78rem;color:var(--text2)">This bot defines its own tier set, below. Reset to discard it and inherit the pod defaults.</span>
      <button class="btn btn-ghost btn-sm" style="margin-left:auto" onclick="_aiOpenEasySetup('${escHtml(botId)}')" id="ai-tiers-easy-btn" title="Pick a provider order; Evolve refills every tier with sensible defaults in that order">Easy setup</button>
      <button class="btn btn-ghost btn-sm" onclick="_aiResetBotToDefaults()" id="ai-tiers-reset-btn" title="Discard this bot's custom tier definitions and inherit the pod defaults">Reset to pod defaults</button>
    </div>
    ${ladderSections}
    ${judgeSection}
    <div style="display:flex;align-items:center;gap:10px;margin-top:4px">
      <button class="btn btn-primary btn-sm" onclick="_aiSaveTiers()" id="ai-tiers-save-btn">Save Tiers</button>
      <button class="btn btn-ghost btn-sm" onclick="_aiRevertTiers()">Revert</button>
      <span id="ai-tiers-save-status" style="font-size:0.8rem"></span>
    </div>`;
}

function _aiTierAddModel(tier, model) {
  if (!model) return;
  if (!_aiPendingTiers[tier]) _aiPendingTiers[tier] = { models: [] };
  if (!(_aiPendingTiers[tier].models || []).includes(model)) {
    _aiPendingTiers[tier].models = [...(_aiPendingTiers[tier].models || []), model];
    _aiDirtySection = 'tiers';
    _aiRenderTiers(_aiBot, _aiBotConfig);
  }
}

function _aiTierRemoveModel(tier, idx) {
  if (!_aiPendingTiers[tier]?.models) return;
  _aiPendingTiers[tier].models.splice(idx, 1);
  _aiDirtySection = 'tiers';
  _aiRenderTiers(_aiBot, _aiBotConfig);
}

// Customize this bot — flip from Use-pod-defaults to Custom (spec §Addendum 5).
// The server materializes a per-bot override SEEDED from this bot's current
// resolved view (full merged rungs/roles), so nothing about the bot's routing
// changes — it just becomes editable. The seed is computed server-side (no
// model literals cross the wire); we POST the mode flip and re-fetch so the
// panel re-renders in Custom mode with config.customTiers === true.
async function _aiCustomizeBot() {
  if (!_aiBot) { toast('No bot selected', 'err'); return; }
  const statusEl = document.getElementById('ai-tiers-save-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Customizing…</span>';
  const r = await api('PUT', '/api/admin/config/' + _aiBot + '/tier-mode', { mode: 'custom' });
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    toast('Customize failed: ' + r.error, 'err');
    return;
  }
  toast(`${botLabel(_aiBot)} now has custom tier definitions`, 'ok');
  // Re-fetch — customTiers flips to true and _aiRenderBotData re-seeds the
  // editable buffer from the freshly-materialized bot file.
  await _aiRenderBotData(_aiBot);
  _aiShowRestartBanner();
}

// Reset to pod defaults — flip from Custom back to Use-pod-defaults (spec
// §Addendum 5). Clears the bot's per-bot rungs/roles so the merge inherits the
// pod/code default again. Confirm first — this discards the bot's custom tier
// definitions wholesale (strict all-or-nothing, no per-tier salvage).
async function _aiResetBotToDefaults() {
  if (!_aiBot) { toast('No bot selected', 'err'); return; }
  if (!await confirmModal({ body: `Reset ${botLabel(_aiBot)} to the pod's default tier definitions?\n\nThis discards this bot's custom tier definitions. It will inherit the pod defaults (provider-matched to its credentials).`, danger: true })) {
    return;
  }
  const statusEl = document.getElementById('ai-tiers-save-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Resetting…</span>';
  const r = await api('PUT', '/api/admin/config/' + _aiBot + '/tier-mode', { mode: 'default' });
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    toast('Reset failed: ' + r.error, 'err');
    return;
  }
  _aiDirtySection = null;
  toast(`${botLabel(_aiBot)} reset to pod defaults`, 'ok');
  // Re-fetch — customTiers flips to false and the panel re-renders the
  // read-only Use-defaults preview.
  await _aiRenderBotData(_aiBot);
  _aiShowRestartBanner();
}

async function _aiSaveTiers() {
  if (!_aiBot) { toast('No bot selected', 'err'); return; }
  const statusEl = document.getElementById('ai-tiers-save-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  // Validate all models exist in catalog
  const allModels = Object.values(_aiPendingTiers).flatMap(t => t.models || []);
  const unknown = allModels.filter(m => !_aiCatalog.includes(m));
  if (unknown.length) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ Models not in catalog: ${escHtml(unknown.join(', '))}</span>`;
    return;
  }
  const r = await api('PUT', '/api/admin/config/' + _aiBot + '/tiers', { tiers: _aiPendingTiers });
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    toast('Save failed: ' + r.error, 'err');
  } else {
    _aiBotConfig.tiers = JSON.parse(JSON.stringify(_aiPendingTiers));
    _aiTierOptions = _aiBotConfig.tiers; // keep compat shim in sync
    _aiDirtySection = null;
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    toast('Tier assignments saved', 'ok');
    _aiRenderTiers(_aiBot, _aiBotConfig);
    _aiRenderFallback(_aiBot, _aiBotConfig);
    _aiShowRestartBanner();
  }
}

function _aiRevertTiers() {
  _aiPendingTiers = JSON.parse(JSON.stringify(_aiBotConfig.tiers || {}));
  _aiDirtySection = null;
  _aiRenderTiers(_aiBot, _aiBotConfig);
  const statusEl = document.getElementById('ai-tiers-save-status');
  if (statusEl) statusEl.innerHTML = '';
}

// ── Section 2b: Cascade Routing ────────────────────────────────────────────────
//
// CascadeController toggle — one boolean per bot. When ON, struggle-based
// tier escalation is live (the controller watches turn outcomes and moves
// the routing tier up the cascade on signs of distress). When OFF, every
// turn is pinned to the configured primary regardless of how it's going.
//
// State lives in ~/.openclaw/evolve-tiers.json::cascade.enabled and is set
// via PUT /api/admin/config/<bot>/cascade. The bot-tile "cascade" chip on
// Cost Optimization deep-links here via _aiJumpToCascadeForBot.

function _aiRenderCascade(botId, config) {
  const el = document.getElementById('ai-cascade-panel');
  const lbl = document.getElementById('ai-cascade-bot-label');
  if (!el) return;
  if (lbl) lbl.textContent = '(' + botLabel(botId) + ')';
  const cascade = (config && config.cascade) || {};
  const enabled = !!cascade.enabled;
  const stateLabel = enabled
    ? `<span style="color:var(--green);font-weight:600">Enabled</span>`
    : `<span style="color:var(--text3);font-weight:600">Disabled</span>`;
  el.innerHTML = `
    <div style="font-size:0.82rem;color:var(--text2);line-height:1.5;margin-bottom:14px;max-width:680px">
      With cascade routing <strong>on</strong>, the CascadeController watches each turn's
      outcome (errors, ask-for-help hints, struggle signals) and automatically routes the
      next turn to the next tier in this bot's cascade order when the model is struggling.
      Auto-reverts to the primary on recovery. With cascade routing <strong>off</strong>,
      every turn pins to the configured primary model — useful for control-group testing
      or for bots where deterministic routing matters more than adaptive resilience.
    </div>
    <div style="display:flex;align-items:center;gap:14px">
      <div style="font-size:0.85rem">Current state: ${stateLabel}</div>
      <button class="btn btn-sm" onclick="_aiSaveCascade('${escHtml(botId)}', ${!enabled})">
        ${enabled ? 'Disable cascade routing' : 'Enable cascade routing'}
      </button>
      <span id="ai-cascade-save-status" style="font-size:0.78rem;color:var(--text2)"></span>
    </div>`;
}

async function _aiSaveCascade(botId, newEnabled) {
  const status = document.getElementById('ai-cascade-save-status');
  if (status) status.innerHTML = '<span class="spinner" style="width:12px;height:12px;display:inline-block"></span> Saving…';
  try {
    const r = await api('PUT', `/api/admin/config/${encodeURIComponent(botId)}/cascade`, { enabled: newEnabled });
    if (r && r.ok) {
      // Reflect the new value locally so the next render uses it.
      if (_aiBotConfig) _aiBotConfig.cascade = { enabled: newEnabled };
      _aiRenderCascade(botId, _aiBotConfig || { cascade: { enabled: newEnabled } });
      if (status) status.innerHTML = `<span style="color:var(--green)">✓ Saved</span>`;
      setTimeout(() => { if (status) status.innerHTML = ''; }, 2500);
      _aiShowRestartBanner();
    } else {
      if (status) status.innerHTML = `<span style="color:var(--danger)">Failed: ${escHtml((r && r.error) || 'unknown')}</span>`;
    }
  } catch (e) {
    if (status) status.innerHTML = `<span style="color:var(--danger)">Failed: ${escHtml(String(e && e.message || e))}</span>`;
  }
}

// Deep-link: navigate to AI Optimization for `botId`, select that bot's
// tab, and scroll the cascade card into view. Used by the bot-tile
// cascade chip on Cost Optimization so the chip is a usable surface,
// not just a passive label.
function _aiJumpToCascadeForBot(botId) {
  const navEl = document.querySelector('.nav-item[data-page="ai-optimization"]');
  if (navEl && typeof nav === 'function') nav(navEl);
  if (!botId) return;
  let tries = 0;
  const apply = () => {
    if (typeof aiSwitchBot !== 'function') {
      if (tries++ < 10) setTimeout(apply, 50);
      return;
    }
    const tabExists = document.querySelector(`#ai-bot-tabs [data-bot="${CSS.escape(botId)}"]`);
    if (!tabExists && tries++ < 10) {
      setTimeout(apply, 50);
      return;
    }
    try { aiSwitchBot(botId); } catch (_) {}
    // After the bot-switch fetch resolves, the cascade card is in the DOM;
    // give it one tick + small retry to scroll smoothly into view.
    let scrollTries = 0;
    const tryScroll = () => {
      const card = document.getElementById('ai-cascade-card');
      if (card && card.scrollIntoView) {
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      if (scrollTries++ < 20) setTimeout(tryScroll, 80);
    };
    setTimeout(tryScroll, 200);
  };
  apply();
}

// ── Section 3: Routing Rules ───────────────────────────────────────────────────

function _aiRenderRouting(botId, config) {
  const el = document.getElementById('ai-botcfg-models');
  const lbl = document.getElementById('ai-model-bot-label');
  if (!el) return;
  if (lbl) lbl.textContent = '(' + botLabel(botId) + ')';

  const routing = config.routing || {};
  const tiers = config.tiers || {};
  const enabled = routing.enabled !== false;
  const maintenanceTier = routing.maintenanceTier || 'tier3';
  const backgroundTier = routing.backgroundTier || 'tier3';
  const ambiguousTier = routing.ambiguousTier || null;
  const confidenceThreshold = routing.confidenceThreshold != null ? routing.confidenceThreshold : 0.65;

  // Routing stores tierN values internally; the UI shows numeral-free role
  // labels (spec §Addendum3.C.1). Canonical ascending order in the picker.
  const tierIds = ['tier3', 'tier2', 'tier1'];

  const getFirstModel = tid => ((tiers[tid]?.models || [])[0] || '').replace('anthropic/', '') || '—';

  const tierOpts = (includeNone) => {
    const none = includeNone ? `<option value="">— none —</option>` : '';
    return none + tierIds.map(tid => {
      const name = TIER_DISPLAY[tid] || tid;
      return `<option value="${escHtml(tid)}">${escHtml(name)}</option>`;
    }).join('');
  };

  const lockBadge = `<span style="font-size:0.72rem;color:var(--text3);margin-left:6px">fixed</span>`;
  const t2model = getFirstModel('tier2');

  el.innerHTML = `
    <div style="margin-bottom:12px;display:flex;align-items:center;gap:10px">
      <label style="font-size:0.85rem;display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="ai-route-enabled" onchange="_aiRoutingDirty()"${enabled ? ' checked' : ''}>
        Routing enabled
      </label>
    </div>
    <table style="width:100%;margin-bottom:12px">
      <thead><tr>
        <th style="text-align:left">Session Type</th>
        <th style="text-align:left">Tier</th>
        <th style="text-align:left">Primary Model</th>
      </tr></thead>
      <tbody>
        <tr>
          <td style="padding:7px 6px"><strong>Productive</strong></td>
          <td style="padding:7px 6px"><span style="font-weight:600;color:var(--blue);font-size:0.82rem">Standard</span>${lockBadge}</td>
          <td style="font-family:monospace;font-size:0.78rem;color:var(--teal);padding:7px 6px">${escHtml(t2model)}</td>
        </tr>
        <tr>
          <td style="padding:7px 6px"><strong>Maintenance</strong></td>
          <td style="padding:7px 6px">
            <select id="ai-route-maintenanceTier" class="form-select input-w-md" onchange="_aiRoutingDirty()"
              style="padding:3px 6px;font-size:0.78rem">
              ${tierOpts(false)}
            </select>
          </td>
          <td id="ai-route-maint-model" style="font-family:monospace;font-size:0.78rem;color:var(--teal);padding:7px 6px">—</td>
        </tr>
        <tr>
          <td style="padding:7px 6px"><strong>Background</strong></td>
          <td style="padding:7px 6px">
            <select id="ai-route-backgroundTier" class="form-select input-w-md" onchange="_aiRoutingDirty()"
              style="padding:3px 6px;font-size:0.78rem">
              ${tierOpts(false)}
            </select>
          </td>
          <td id="ai-route-bg-model" style="font-family:monospace;font-size:0.78rem;color:var(--teal);padding:7px 6px">—</td>
        </tr>
        <tr>
          <td style="padding:7px 6px"><strong>Ambiguous</strong></td>
          <td style="padding:7px 6px">
            <select id="ai-route-ambiguousTier" class="form-select input-w-md" onchange="_aiRoutingDirty()"
              style="padding:3px 6px;font-size:0.78rem">
              ${tierOpts(true)}
            </select>
          </td>
          <td id="ai-route-amb-model" style="font-family:monospace;font-size:0.78rem;color:var(--teal);padding:7px 6px">—</td>
        </tr>
      </tbody>
    </table>
    <div style="margin-bottom:14px">
      <div style="font-size:0.82rem;color:var(--text2);margin-bottom:6px">Confidence Threshold</div>
      <div style="display:flex;align-items:center;gap:10px">
        <input type="range" id="ai-route-confidence" min="0" max="1" step="0.05" value="${confidenceThreshold}"
          style="flex:1;max-width:200px"
          oninput="document.getElementById('ai-route-confidence-val').textContent=parseFloat(this.value).toFixed(2);_aiRoutingDirty()">
        <span id="ai-route-confidence-val" style="font-family:monospace;font-size:0.85rem">${confidenceThreshold.toFixed(2)}</span>
      </div>
      <div style="font-size:0.72rem;color:var(--text3);margin-top:4px">Routing preview will appear once annotation data is collected.</div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <button class="btn btn-primary btn-sm" id="ai-route-save-btn" onclick="_aiSaveRouting()" disabled>Save Routing</button>
      <button class="btn btn-ghost btn-sm" id="ai-route-revert-btn" onclick="_aiRevertRouting()" disabled>Revert</button>
      <span id="ai-route-status" style="font-size:0.8rem"></span>
    </div>`;

  // Pre-populate dropdowns
  const maintSel = document.getElementById('ai-route-maintenanceTier');
  const bgSel = document.getElementById('ai-route-backgroundTier');
  const ambSel = document.getElementById('ai-route-ambiguousTier');
  if (maintSel) maintSel.value = maintenanceTier;
  if (bgSel) bgSel.value = backgroundTier;
  if (ambSel) ambSel.value = ambiguousTier || '';
  _aiRoutingUpdateModels();
}

function _aiRoutingUpdateModels() {
  const tiers = _aiBotConfig.tiers || {};
  const getFirst = tid => tid ? ((tiers[tid]?.models || [])[0] || '').replace('anthropic/', '') || '—' : '—';
  const maintTier = document.getElementById('ai-route-maintenanceTier')?.value || 'tier3';
  const bgTier = document.getElementById('ai-route-backgroundTier')?.value || 'tier3';
  const ambTier = document.getElementById('ai-route-ambiguousTier')?.value || '';
  const e1 = document.getElementById('ai-route-maint-model');
  const e2 = document.getElementById('ai-route-bg-model');
  const e3 = document.getElementById('ai-route-amb-model');
  if (e1) e1.textContent = getFirst(maintTier);
  if (e2) e2.textContent = getFirst(bgTier);
  if (e3) e3.textContent = getFirst(ambTier);
}

function _aiRoutingDirty() {
  _aiDirtySection = 'routing';
  document.getElementById('ai-route-save-btn')?.removeAttribute('disabled');
  document.getElementById('ai-route-revert-btn')?.removeAttribute('disabled');
  _aiRoutingUpdateModels();
}

function _aiRevertRouting() {
  _aiDirtySection = null;
  _aiRenderRouting(_aiBot, _aiBotConfig);
  const saveBtn = document.getElementById('ai-route-save-btn');
  const revertBtn = document.getElementById('ai-route-revert-btn');
  const statusEl = document.getElementById('ai-route-status');
  if (saveBtn) saveBtn.disabled = true;
  if (revertBtn) revertBtn.disabled = true;
  if (statusEl) { statusEl.textContent = 'Reverted.'; setTimeout(() => { statusEl.textContent = ''; }, 2000); }
}

async function _aiSaveRouting() {
  const saveBtn = document.getElementById('ai-route-save-btn');
  const statusEl = document.getElementById('ai-route-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  if (saveBtn) saveBtn.disabled = true;
  const ambVal = document.getElementById('ai-route-ambiguousTier')?.value || null;
  const routing = {
    enabled: document.getElementById('ai-route-enabled')?.checked !== false,
    maintenanceTier: document.getElementById('ai-route-maintenanceTier')?.value || 'tier3',
    backgroundTier: document.getElementById('ai-route-backgroundTier')?.value || 'tier3',
    ambiguousTier: ambVal || null,
    confidenceThreshold: parseFloat(document.getElementById('ai-route-confidence')?.value || '0.65'),
  };
  const r = await api('PUT', '/api/admin/config/' + _aiBot + '/routing', { routing });
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    if (saveBtn) saveBtn.disabled = false;
  } else {
    _aiBotConfig.routing = routing;
    _aiDirtySection = null;
    document.getElementById('ai-route-revert-btn')?.setAttribute('disabled', 'true');
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    toast('Routing config saved', 'ok');
    _aiShowRestartBanner();
  }
}

// ── Section 5: Fallback Configuration ─────────────────────────────────────────

function _aiComputeFallbackList(tiers, cascade) {
  const seen = new Set();
  const list = [];
  for (const tid of (cascade || [])) {
    for (const m of (tiers[tid]?.models || [])) {
      if (!seen.has(m)) { seen.add(m); list.push(m); }
    }
  }
  return list;
}

function _aiRenderFallback(botId, config) {
  const el = document.getElementById('ai-fallback-panel');
  const lbl = document.getElementById('ai-fallback-bot-label');
  if (!el) return;
  if (lbl) lbl.textContent = '(' + botLabel(botId) + ')';

  const tierCascade = config.tierCascade || ['tier2', 'tier3', 'tier1'];
  const tiers = config.tiers || {};

  // Cascade order editor (tier0 excluded — judge tier doesn't participate in fallback)
  const cascadeIds = tierCascade.filter(t => t !== 'tier0');
  const tierMeta = {
    tier1: { name: 'Power',    color: 'var(--text2)', oneLiner: 'top-of-line, expensive' },
    tier2: { name: 'Standard', color: 'var(--blue)',  oneLiner: 'normal user-facing model' },
    tier3: { name: 'Fast',     color: 'var(--teal)',  oneLiner: 'cheap + fast' },
  };
  const cascadeRows = cascadeIds.map((tid, i) => {
    const meta = tierMeta[tid] || { name: tid, color: 'var(--text2)', oneLiner: '' };
    const oneLiner = meta.oneLiner ? `<span style="color:var(--text3);font-size:0.74rem;margin-left:6px">${escHtml(meta.oneLiner)}</span>` : '';
    return `<div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:0.85rem">
      <span style="color:var(--text3);min-width:18px;text-align:right">${i+1}.</span>
      <span style="flex:1"><span style="font-weight:600;color:${meta.color}">${escHtml(meta.name)}</span>${oneLiner}</span>
      <button class="btn btn-ghost btn-sm" style="padding:1px 5px;font-size:0.72rem" onclick="_aiCascadeMove(${i},-1)" ${i===0?'disabled':''}>↑</button>
      <button class="btn btn-ghost btn-sm" style="padding:1px 5px;font-size:0.72rem" onclick="_aiCascadeMove(${i},1)" ${i===cascadeIds.length-1?'disabled':''}>↓</button>
    </div>`;
  }).join('');

  // Generated fallback list
  const genList = _aiComputeFallbackList(tiers, cascadeIds);
  const genHTML = genList.length
    ? genList.map((m, i) => `${i > 0 ? '<span style="color:var(--text3);margin:0 4px">→</span>' : ''}<span class="ai-model-pill" style="cursor:default">${escHtml(m.replace('anthropic/',''))}</span>`).join('')
    : '<span style="color:var(--text3);font-size:0.8rem;font-style:italic">No models in cascade tiers</span>';

  el.innerHTML = `
    <div style="font-size:0.8rem;color:var(--text2);margin-bottom:6px">
      <strong style="color:var(--text1)">How fallback works.</strong>
      Routing picks the model a turn <em>starts</em> with. Fallback is what happens when that model errors out, times out, or hits a rate limit — OpenClaw walks down a flat list of backup models until one succeeds.
    </div>
    <div style="font-size:0.8rem;color:var(--text2);margin-bottom:14px">
      Evolve builds that flat list from your <strong>tier definitions</strong> and the <strong>Tier Cascade Order</strong> below, then writes it to openclaw.json. Routing and fallback are independent — changing the cascade doesn't change which model a turn starts with, only which models it falls back to if that fails.
    </div>
    <div id="ai-fallback-cascade-section" style="margin-bottom:16px">
      <div style="font-size:0.82rem;font-weight:600;color:var(--text1);margin-bottom:4px">Tier Cascade Order</div>
      <div style="font-size:0.78rem;color:var(--text2);margin-bottom:4px">
        When the starting model fails, OpenClaw tries every model in the next tier on this list, then the next, and so on.
      </div>
      <div style="font-size:0.74rem;color:var(--text3);margin-bottom:10px">
        The default <strong>Standard → Fast → Power</strong> is right for most bots: stay on the normal model first, drop to cheap-and-fast for resilience, escalate to Power only as a last resort. Max (Fable-class) is not in the fallback chain — it is pull-only and never reached automatically. Reorder for a specific reason — for example, a bot doing high-stakes reasoning might prefer <strong>Power</strong> earlier.
      </div>
      <div id="ai-fallback-cascade-list">${cascadeRows}</div>
    </div>
    <div style="margin-bottom:16px">
      <div style="font-size:0.82rem;font-weight:600;color:var(--text1);margin-bottom:4px">Generated Fallback List</div>
      <div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:8px;min-height:36px">
        ${genHTML}
      </div>
      <div style="font-size:0.72rem;color:var(--text3);margin-top:4px">This list is written to openclaw.json on save.</div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <button class="btn btn-primary btn-sm" id="ai-fallback-save-btn" onclick="_aiSaveFallback()" disabled>Save Fallback</button>
      <button class="btn btn-ghost btn-sm" id="ai-fallback-revert-btn" onclick="_aiRevertFallback()" disabled>Revert</button>
      <span id="ai-fallback-status" style="font-size:0.8rem"></span>
    </div>`;
}

function _aiCascadeMove(idx, dir) {
  const cascade = (_aiBotConfig.tierCascade || ['tier2','tier3','tier1']).filter(t => t !== 'tier0');
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= cascade.length) return;
  [cascade[idx], cascade[newIdx]] = [cascade[newIdx], cascade[idx]];
  // Preserve tier0 if it was present
  const hadTier0 = (_aiBotConfig.tierCascade || []).includes('tier0');
  _aiBotConfig.tierCascade = hadTier0 ? [...cascade, 'tier0'] : cascade;
  _aiDirtySection = 'fallback';
  _aiRenderFallback(_aiBot, _aiBotConfig);
  document.getElementById('ai-fallback-save-btn')?.removeAttribute('disabled');
  document.getElementById('ai-fallback-revert-btn')?.removeAttribute('disabled');
}

function _aiRevertFallback() {
  _aiDirtySection = null;
  if (_aiBotConfig._origTierCascade) _aiBotConfig.tierCascade = JSON.parse(JSON.stringify(_aiBotConfig._origTierCascade));
  if (_aiBotConfig._origFallbackMode) _aiBotConfig.fallbackMode = _aiBotConfig._origFallbackMode;
  _aiRenderFallback(_aiBot, _aiBotConfig);
  const s = document.getElementById('ai-fallback-save-btn'); if (s) s.disabled = true;
  const rv = document.getElementById('ai-fallback-revert-btn'); if (rv) rv.disabled = true;
  const st = document.getElementById('ai-fallback-status');
  if (st) { st.textContent = 'Reverted.'; setTimeout(() => { st.textContent = ''; }, 2000); }
}

// _aiFallbackDirty was tied to the mode-selector radio's onchange.
// The selector was retired (only "static" exists today), but we keep
// this entrypoint as a stable hook in case future surfaces add their
// own dirty-triggering controls. _aiCascadeMove already sets dirty
// state inline, so the cascade reorder path doesn't need this.
function _aiFallbackDirty() {
  _aiDirtySection = 'fallback';
  document.getElementById('ai-fallback-save-btn')?.removeAttribute('disabled');
  document.getElementById('ai-fallback-revert-btn')?.removeAttribute('disabled');
}

async function _aiSaveFallback() {
  const saveBtn = document.getElementById('ai-fallback-save-btn');
  const statusEl = document.getElementById('ai-fallback-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  if (saveBtn) saveBtn.disabled = true;

  // Fallback mode is hardcoded to "static" — the multi-mode selector
  // was retired since reactive/dynamic were never specced or built.
  // The backend still accepts the field for forward compatibility, so
  // we send it explicitly.
  const fallbackMode = 'static';
  const cascade = (_aiBotConfig.tierCascade || ['tier2','tier3','tier1']).filter(t => t !== 'tier0');

  const r = await api('PUT', '/api/admin/config/' + _aiBot + '/fallback', { fallbackMode, tierCascade: cascade });
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    if (saveBtn) saveBtn.disabled = false;
  } else {
    _aiBotConfig.fallbackMode = fallbackMode;
    _aiBotConfig.tierCascade = cascade;
    _aiBotConfig._origFallbackMode = fallbackMode;
    _aiBotConfig._origTierCascade = JSON.parse(JSON.stringify(cascade));
    _aiDirtySection = null;
    document.getElementById('ai-fallback-revert-btn')?.setAttribute('disabled', 'true');
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    toast('Fallback config saved', 'ok');
    _aiShowRestartBanner();
  }
}

// ── Section 5b: Embedding Providers ──────────────────────────────────────────
// Edits the per-bot embedding chain (memory_search provider order). Save PUTs
// to /api/admin/embedding-config which writes both network.json and the bot's
// openclaw.json so OpenClaw can fail over without evolve in the loop.

let _aiEmbeddingData = null;       // last response from GET /embedding-config
let _aiPendingEmbChain = [];       // mutable in-flight chain for the editor
let _aiEmbProvById = {};           // id → provider row from data.providers

async function _aiLoadEmbeddings(botName) {
  const el = document.getElementById('ai-embedding-panel');
  if (!el || !botName) return;
  try {
    const r = await api('GET', '/api/admin/embedding-config/' + encodeURIComponent(botName));
    if (!r || r.error) {
      el.innerHTML = `<div class="empty" style="color:var(--danger);padding:8px 0">${escHtml(r && r.error || 'Failed to load embedding config')}</div>`;
      return;
    }
    _aiEmbeddingData = r;
    _aiPendingEmbChain = (r.per_bot_chain && r.per_bot_chain.length) ? [...r.per_bot_chain] : [...(r.resolved_chain || [])];
    _aiEmbProvById = {};
    (r.providers || []).forEach(p => { _aiEmbProvById[p.id] = p; });
    _aiRenderEmbeddings();
  } catch (e) {
    el.innerHTML = `<div class="empty" style="color:var(--danger);padding:8px 0">${escHtml(e.message)}</div>`;
  }
}

function _aiRenderEmbeddings() {
  const el = document.getElementById('ai-embedding-panel');
  if (!el || !_aiEmbeddingData) return;

  const data = _aiEmbeddingData;
  const chain = _aiPendingEmbChain;
  const providers = data.providers || [];

  function capBadge(cap) {
    const map = {
      multimodal:      ['Multimodal',      'var(--blue)'],
      retrieval_tuned: ['Retrieval-tuned', 'var(--green)'],
      subscription:    ['Subscription',    'var(--text2)'],
      local:           ['Local',           'var(--text3)'],
      offline:         ['Offline',         'var(--text3)'],
    };
    const [label, color] = map[cap] || [cap, 'var(--text3)'];
    return `<span style="font-size:0.62rem;padding:1px 5px;border-radius:3px;border:1px solid ${color};color:${color};margin-left:4px">${escHtml(label)}</span>`;
  }

  const warningHtml = data.warning
    ? `<div style="background:var(--bg3);border-left:3px solid var(--yellow);padding:8px 12px;margin-bottom:12px;border-radius:3px;font-size:0.82rem;color:var(--text2)">⚠ ${escHtml(data.warning)}</div>`
    : '';

  // Active chain editor — ordered list with up/down/remove.
  const chainRows = chain.length
    ? chain.map((pid, i) => {
        const p = _aiEmbProvById[pid] || { id: pid, label: pid, model: '', capabilities: [] };
        const tagText = i === 0 ? 'primary' : `fallback ${i}`;
        const tagColor = i === 0 ? 'var(--green)' : 'var(--blue)';
        return `<div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:0.82rem;border-bottom:1px solid var(--border)">
          <span style="color:var(--text3);min-width:18px;text-align:right">${i+1}.</span>
          <span style="font-weight:600;flex:0 0 auto">${escHtml(p.label)}</span>
          ${(p.capabilities || []).map(capBadge).join('')}
          <span style="font-family:monospace;color:var(--text3);font-size:0.72rem;flex:1">${escHtml(p.model || p.default_model || '—')}</span>
          <span style="font-size:0.65rem;color:${tagColor};text-transform:uppercase;letter-spacing:0.04em">${tagText}</span>
          <button class="btn btn-ghost btn-sm" style="padding:1px 5px;font-size:0.72rem" title="Move up"
            onclick="_aiEmbMove(${i},-1)" ${i===0?'disabled':''}>↑</button>
          <button class="btn btn-ghost btn-sm" style="padding:1px 5px;font-size:0.72rem" title="Move down"
            onclick="_aiEmbMove(${i},1)" ${i===chain.length-1?'disabled':''}>↓</button>
          <button class="btn btn-ghost btn-sm" style="padding:1px 5px;font-size:0.72rem;color:var(--danger)" title="Remove"
            onclick="_aiEmbRemove(${i})">×</button>
        </div>`;
      }).join('')
    : `<div style="font-size:0.8rem;color:var(--text3);padding:6px 0;font-style:italic">(empty — add a provider below)</div>`;

  // Add picker — providers not yet in the chain. Configured providers come
  // first; unconfigured ones are listed but disabled with an explanation.
  const inChain = new Set(chain);
  const candidates = providers.filter(p => !inChain.has(p.id));
  const pickerOpts = candidates.map(p => {
    const note = p.is_configured ? '' : ' — no credentials';
    return `<option value="${escHtml(p.id)}" ${p.is_configured ? '' : 'disabled'}>${escHtml(p.label)}${note}</option>`;
  }).join('');
  const addRow = candidates.length
    ? `<div style="display:flex;gap:6px;align-items:center;margin-top:10px">
         <select id="ai-emb-add-select" class="form-select"
           style="flex:1;padding:4px 8px;font-size:0.82rem">
           <option value="">Add provider…</option>
           ${pickerOpts}
         </select>
         <button class="btn btn-ghost btn-sm" style="font-size:0.78rem" onclick="_aiEmbAddFromSelect()">+ Add</button>
       </div>`
    : `<div style="font-size:0.8rem;color:var(--text3);margin-top:8px;font-style:italic">All available providers are in the chain.</div>`;

  const isDirty = JSON.stringify(chain) !== JSON.stringify(data.per_bot_chain || data.resolved_chain || []);
  const sourceNote = data.per_bot_chain
    ? `Per-bot override (pod default: ${escHtml((data.pod_default_chain || []).join(' → '))})`
    : `Inheriting pod default. Edits become a per-bot override.`;

  el.innerHTML = `
    ${warningHtml}
    <div style="font-size:0.72rem;color:var(--text3);margin-bottom:8px">${sourceNote}</div>
    <div>${chainRows}</div>
    ${addRow}
    <div style="display:flex;align-items:center;gap:10px;margin-top:14px">
      <button class="btn btn-primary btn-sm" onclick="_aiEmbSave()" id="ai-emb-save-btn" ${isDirty?'':'disabled'}>Save Chain</button>
      <button class="btn btn-ghost btn-sm" onclick="_aiEmbRevert()" ${isDirty?'':'disabled'}>Revert</button>
      <button class="btn btn-ghost btn-sm" onclick="_aiEmbClearOverride()" ${data.per_bot_chain?'':'disabled'} title="Remove the per-bot override and inherit the pod default">Clear override</button>
      <span id="ai-emb-status" style="font-size:0.8rem"></span>
    </div>`;
}

function _aiEmbMove(idx, dir) {
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= _aiPendingEmbChain.length) return;
  [_aiPendingEmbChain[idx], _aiPendingEmbChain[newIdx]] = [_aiPendingEmbChain[newIdx], _aiPendingEmbChain[idx]];
  _aiDirtySection = 'embedding';
  _aiRenderEmbeddings();
}

function _aiEmbRemove(idx) {
  _aiPendingEmbChain.splice(idx, 1);
  _aiDirtySection = 'embedding';
  _aiRenderEmbeddings();
}

function _aiEmbAddFromSelect() {
  const sel = document.getElementById('ai-emb-add-select');
  if (!sel || !sel.value) return;
  if (!_aiPendingEmbChain.includes(sel.value)) {
    _aiPendingEmbChain.push(sel.value);
    _aiDirtySection = 'embedding';
  }
  sel.value = '';
  _aiRenderEmbeddings();
}

function _aiEmbRevert() {
  if (!_aiEmbeddingData) return;
  _aiPendingEmbChain = _aiEmbeddingData.per_bot_chain
    ? [..._aiEmbeddingData.per_bot_chain]
    : [...(_aiEmbeddingData.resolved_chain || [])];
  _aiDirtySection = null;
  _aiRenderEmbeddings();
}

async function _aiEmbClearOverride() {
  if (!_aiEmbeddingData?.per_bot_chain) return;
  if (!await confirmModal({ body: 'Remove this bot\'s embedding override and fall back to the pod default?', danger: true })) return;
  await _aiEmbPut([], 'Override cleared');
}

async function _aiEmbSave() {
  await _aiEmbPut(_aiPendingEmbChain, 'Embedding chain saved');
}

async function _aiEmbPut(chain, successMsg) {
  const statusEl = document.getElementById('ai-emb-status');
  const saveBtn = document.getElementById('ai-emb-save-btn');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  if (saveBtn) saveBtn.disabled = true;
  const r = await api('PUT', '/api/admin/embedding-config/' + encodeURIComponent(_aiBot), { chain });
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    if (saveBtn) saveBtn.disabled = false;
    return;
  }
  _aiDirtySection = null;
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
  setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
  toast(successMsg, 'ok');
  // Re-fetch so the UI reflects what the server actually persisted (incl. resolver
  // filtering, e.g. dropping providers without credentials).
  await _aiLoadEmbeddings(_aiBot);
  _aiShowRestartBanner();
}

// ── Section 4: Context Management ─────────────────────────────────────────────

let _aiCompactionBot = null;
let _aiCompactionData = {};

async function _aiRenderCompaction(botName, compaction) {
  const aiCompEl = document.getElementById('ai-botcfg-compaction');
  if (!aiCompEl) return;
  _aiCompactionBot = botName;
  _aiCompactionData = compaction || {};

  if (compaction.error || !Object.keys(compaction).length) {
    aiCompEl.innerHTML = `<div class="empty">${escHtml(compaction.error || 'No compaction config found.')}</div>`;
    return;
  }

  const currentModel = compaction.model ? _aiModelStr(compaction.model) : '';
  // Reverse-lookup tier from _aiTierOptions (populated by routing fetch)
  const currentTier = _aiTierForModel(currentModel, _aiTierOptions) || 'tier3';

  // Build tier dropdown from _aiTierOptions (same source as Section 1 & 2).
  // Numeral-free role labels (spec §Addendum3.C.1).
  const tierOpts = Object.entries(_aiTierOptions).map(([tid, tobj]) => {
    const name = TIER_DISPLAY[tid] || tid;
    const model = _aiModelStr(tobj).replace('anthropic/', '') || tid;
    return `<option value="${escHtml(tid)}">${escHtml(name)} — ${escHtml(model)}</option>`;
  }).join('');

  const mode = compaction.mode || compaction.type || '—';
  const reserve = compaction.reserveTokens ?? compaction.reserve_tokens ?? compaction.contextReserve ?? null;
  const reserveStr = reserve != null ? Number(reserve).toLocaleString() + ' tokens' : '—';
  const modeDesc = {
    safeguard: 'Summarizes only when context is nearly full — safest option.',
    auto: 'Summarizes automatically as needed.',
    manual: 'Only compacts when explicitly triggered.',
  }[mode] || '';

  // Derive model from selected tier (shown as read-only label)
  const derivedModel = (_aiTierOptions[currentTier] ? _aiModelStr(_aiTierOptions[currentTier]).replace('anthropic/', '') : null) || currentModel.replace('anthropic/', '') || '—';

  aiCompEl.innerHTML = `
    <div style="display:grid;gap:12px">
      <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start">
        <div>
          <div style="font-size:0.72rem;color:var(--text2);margin-bottom:4px">Mode</div>
          <select id="ai-compact-mode" class="form-select input-w-md"
            style="padding:4px 8px;font-size:0.82rem">
            <option value="safeguard"${mode==='safeguard'?' selected':''}>safeguard</option>
            <option value="auto"${mode==='auto'?' selected':''}>auto</option>
            <option value="manual"${mode==='manual'?' selected':''}>manual</option>
          </select>
          ${modeDesc ? `<div style="font-size:0.72rem;color:var(--text3);margin-top:3px;max-width:200px">${escHtml(modeDesc)}</div>` : ''}
        </div>
        <div>
          <div style="font-size:0.72rem;color:var(--text2);margin-bottom:4px">Compaction Tier</div>
          <select id="ai-compact-tier" class="form-select input-w-md" onchange="_aiCompactTierChanged()"
            style="padding:4px 8px;font-size:0.82rem">
            ${tierOpts}
          </select>
        </div>
        <div>
          <div style="font-size:0.72rem;color:var(--text2);margin-bottom:4px">Current Model <span style="color:var(--text3)">(derived)</span></div>
          <span id="ai-compact-derived-model" style="font-family:monospace;font-size:0.82rem;color:var(--teal)">${escHtml(derivedModel)}</span>
        </div>
        <div>
          <div style="font-size:0.72rem;color:var(--text2);margin-bottom:4px">Reserve</div>
          <span style="font-size:0.82rem">${escHtml(reserveStr)}</span>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <button class="btn btn-primary btn-sm" onclick="_aiCompactionSave()">Save</button>
        <span id="ai-compact-status" style="font-size:0.8rem"></span>
      </div>
      <details style="font-size:0.75rem;color:var(--text2)">
        <summary style="display:flex;align-items:center;gap:8px;cursor:pointer"><span class="expand-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span>Raw values</summary>
        <div style="margin-top:6px;font-family:monospace;white-space:pre-wrap">${escHtml(JSON.stringify(compaction, null, 2))}</div>
      </details>
    </div>`;

  // Set tier dropdown to current tier
  const tierSel = document.getElementById('ai-compact-tier');
  if (tierSel && currentTier) tierSel.value = currentTier;
}

function _aiCompactTierChanged() {
  const tierSel = document.getElementById('ai-compact-tier');
  const modelLabel = document.getElementById('ai-compact-derived-model');
  if (!tierSel || !modelLabel) return;
  const tid = tierSel.value;
  const tobj = _aiTierOptions[tid];
  const model = tobj ? (tobj.primary || (tobj.models || [])[0] || '').replace('anthropic/', '') || '—' : '—';
  modelLabel.textContent = model;
}

async function _aiCompactionSave() {
  const tierSel = document.getElementById('ai-compact-tier');
  const modeSel = document.getElementById('ai-compact-mode');
  const statusEl = document.getElementById('ai-compact-status');
  if (!tierSel || !_aiCompactionBot) return;
  const tid = tierSel.value;
  const mode = modeSel?.value;
  const tiers = _networkData.models?.tiers || {};
  // Derive full model name from selected tier (prefer models[0] for explicitness)
  const tobj = _aiTierOptions[tid];
  const rawModel = (tobj ? ((tobj.models || [])[0] || tobj.primary || _aiModelStr(tobj)) : '') ||
    (tiers[tid]?.models?.[0] || '');
  if (!rawModel) { toast('No model found for selected tier', 'err'); return; }
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  const results = await Promise.all([
    api('POST', '/api/config/push', { key: 'agents.defaults.compaction.model', value: rawModel, bots: [_aiCompactionBot] }),
    mode && mode !== '—' ? api('POST', '/api/config/push', { key: 'agents.defaults.compaction.mode', value: mode, bots: [_aiCompactionBot] }) : Promise.resolve({ ok: true }),
  ]);
  const err = results.find(r => r.error)?.error;
  if (err) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(err)}</span>`;
  } else {
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    toast('Compaction config saved', 'ok');
  }
}

// ── Main bot data loader ───────────────────────────────────────────────────────

async function _aiRenderBotData(botName) {
  if (!botName) return;
  // Friendly display name (display_name from network.json), falling
  // back to the raw bot ID. Matches what's on the bot tabs above —
  // avoids the "tab says evo, cards say evolve" disagreement on pods
  // where the primary bot was renamed but the on-disk ID is legacy.
  const labelSuffix = '(' + botLabel(botName) + ')';
  ['ai-model-bot-label','ai-compact-bot-label','ai-mgmt-bot-label','ai-tiers-bot-label','ai-cascade-bot-label','ai-fallback-bot-label','ai-embedding-bot-label'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = labelSuffix;
  });

  ['ai-mgmt-panel','ai-tiers-panel','ai-cascade-panel','ai-botcfg-models','ai-fallback-panel','ai-embedding-panel'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
  });

  const config = await api('GET', '/api/admin/config/' + botName);
  if (config.error) {
    ['ai-mgmt-panel','ai-tiers-panel','ai-cascade-panel','ai-botcfg-models','ai-fallback-panel'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '<div class="empty" style="color:var(--danger)">' + escHtml(config.error) + '</div>';
    });
    return;
  }

  _aiBotConfig = config;
  _aiCatalog = config.catalog || [];
  _aiPendingTiers = JSON.parse(JSON.stringify(config.tiers || {}));
  _aiTierOptions = config.tiers || {};  // keep compat shim in sync

  // Load the validated-picker candidate listings before rendering tiers, so
  // the custom tier editor's "Add model" picker is populated on first paint.
  await _aiLoadListings();

  // Store originals for fallback revert
  _aiBotConfig._origFallbackMode = config.fallbackMode || 'static';
  _aiBotConfig._origTierCascade = JSON.parse(JSON.stringify(config.tierCascade || ['tier2','tier3','tier1']));

  _aiRenderCatalog(botName, config);
  _aiRenderTiers(botName, config);
  _aiRenderCascade(botName, config);
  _aiRenderRouting(botName, config);
  _aiRenderFallback(botName, config);
  _aiLoadEmbeddings(botName);

  // Compaction (separate fetch)
  const qs = '?bot=' + encodeURIComponent(botName);
  const agentsDefaults = await api('GET', '/api/bot/agents-defaults' + qs);
  const compactionData = agentsDefaults.error ? {} : (agentsDefaults.compaction || {});
  await _aiRenderCompaction(botName, compactionData);

  document.getElementById('ai-mgmt-refresh-btn') && (document.getElementById('ai-mgmt-refresh-btn').disabled = false);
}

async function loadAiOptimization() {
  // Pod-wide model-freshness summary (cached from last check)
  _aiLoadFreshnessStatus();

  // Observed per-model cost (28d) — populate the cache BEFORE rendering so the
  // model chips can show this pod's real $/1k alongside the costClass band.
  // Awaited (not fire-and-forget) because the chips are built synchronously
  // below; a missed cache just means the pills are absent until next render.
  await _aiLoadObservedCost();

  const routing = _networkData.models?.routing || {};
  const tiers = _networkData.models?.tiers || {};

  // Routing flow text
  const flowEl = document.getElementById('ai-routing-flow');
  if (flowEl) flowEl.textContent = _aiRoutingText(routing, tiers);

  // Bot tabs — POD first (default landing), then per-bot, then
  // grayed-out evolve placeholder when not provisioned.
  const bots = orderedBotIds(_networkData.bots);
  const tabsEl = document.getElementById('ai-bot-tabs');
  if (tabsEl) {
    // _aiBot === "" sentinel means POD tab. Default to POD on first
    // visit so operators see the explainer before drilling into bots.
    if (_aiBot == null) _aiBot = "";
    if (_aiBot !== "" && !bots.includes(_aiBot)) _aiBot = "";

    const podActive = _aiBot === "";
    const podTab =
      `<div class="subtab ${podActive ? 'active' : ''}" data-bot="" onclick="aiSwitchBot('')">POD</div>`;

    const botTabs = bots.map(b =>
      `<div class="subtab ${b === _aiBot ? 'active' : ''}" data-bot="${escHtml(b)}" onclick="aiSwitchBot('${escHtml(b)}')">${escHtml(botLabel(b))}</div>`
    ).join('');

    // Show evolve tab as grayed-out placeholder if not yet provisioned
    const evolveTab = bots.includes('evolve') ? '' :
      `<div class="subtab" style="opacity:0.38;cursor:not-allowed;pointer-events:none"
           title="Evolve bot not yet provisioned. Set up in Setup Wizard.">
         evolve <span style="font-size:0.65rem;color:var(--text3);vertical-align:middle">(not provisioned)</span>
       </div>`;

    tabsEl.innerHTML = podTab + botTabs + evolveTab;
    tabsEl.style.display = '';
  }

  // Toggle POD vs per-bot view visibility
  const podView = document.getElementById('ai-pod-view');
  const botView = document.getElementById('ai-bot-view');
  if (_aiBot === "") {
    if (podView) podView.style.display = '';
    if (botView) botView.style.display = 'none';
    await _aiLoadPod();
  } else {
    if (podView) podView.style.display = 'none';
    if (botView) botView.style.display = '';
    await _aiRenderBotData(_aiBot);
  }
}

// ── POD view loaders ───────────────────────────────────────────────────────

// Tier display names — used in the engine tier defaults panel to label
// rows with both the tier ID and its human-friendly role. "Max" is
// the display label for fable-class; it has no legacy tierN key (it
// is not in the per-bot tier editor — pod-wide via rungs[]).
// Winning-layer label for a resolved role (spec §Addendum 2.3). The layer
// is which config decided the role: `default` = Evolve's code-shipped
// DEFAULT_MODEL_CATALOG, `pod` = network.json, `bot` = the primary bot's
// own definitions. A small colored chip + plain-language phrase.
function _aiLayerLabel(layer, primaryBotName) {
  if (layer === 'bot') {
    return `<span class="ai-layer-chip ai-layer-bot">bot</span> from ${escHtml(primaryBotName)}'s definitions`;
  }
  if (layer === 'pod') {
    return `<span class="ai-layer-chip ai-layer-pod">pod</span> from network.json (pod-wide)`;
  }
  // default (or unknown) — Evolve's shipped catalog.
  return `<span class="ai-layer-chip ai-layer-default">default</span> Evolve default`;
}

// Rank presentation (spec §Addendum3.C). Roles are NEVER numbered in the UI —
// the legacy "Tier 0–3" numerals (lower = stronger, judge wedged at 0) are
// retired. A role's strength is shown by a filled-steps meter whose rank is
// DERIVED at render time (the array index of the rung it points at), so adding
// a future rung shifts the meter without renumbering anything.
//
// _AI_ROLE_LABELS — canonical display label per role id. Tier numerals gone.
const _AI_ROLE_LABELS = {
  fast:     'Fast',
  standard: 'Standard',
  power:    'Power',
  max:      'Max',
  judge:    'Judge',
};

// Relational subtitle per role — describes each role by its position on the
// ladder, not an absolute number, so the copy stays correct when the rung
// array changes. Reuses the spec's role-table purposes (§Addendum role table).
const _AI_ROLE_SUBTITLES = {
  fast:     'the cheapest rung — background and internal work users never see',
  standard: 'the everyday rung — runs productive user-facing sessions',
  power:    'above Standard — high-complexity work, daily-capped',
  max:      'the frontier model — above Power, premium cost',
  judge:    'Off the power ladder — chosen for provider diversity (cross-checks the pod’s own work), not for strength.',
};

// Canonical ascending order everywhere (spec §Addendum3.C.2): Fast → Standard
// → Power → Max. judge is rendered separately (off-ladder, no meter).
const _AI_LADDER_ROLES = ['fast', 'standard', 'power', 'max'];

// Legacy engine-override values are still tierN-keyed (the engine override is a
// separate pod-wide pin, not a role); map them to role labels for the banner.
const _AI_TIER_TO_ROLE_LABEL = {
  tier0: 'Judge', tier1: 'Power', tier2: 'Standard', tier3: 'Fast',
};

// Filled-steps rank meter (spec §Addendum3.C.3). steps = rung count, filled =
// rank. Token vars only; accessible aria-label "rank N of M". judge gets no
// meter (off-ladder), so callers skip it for the judge role.
function _aiRankMeter(rank, rungCount) {
  const total = Number.isInteger(rungCount) && rungCount > 0 ? rungCount : 0;
  const filled = Number.isInteger(rank) && rank > 0 ? rank : 0;
  if (!total) return '';
  let steps = '';
  for (let i = 0; i < total; i++) {
    const on = i < filled;
    steps += `<span aria-hidden="true" style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${on ? 'var(--accent)' : 'var(--border)'}"></span>`;
  }
  return `<span role="img" aria-label="rank ${filled} of ${total}" title="rank ${filled} of ${total}" style="display:inline-flex;gap:3px;align-items:center;vertical-align:middle">${steps}</span>`;
}

// costClass chip — semantic-ish weight without inventing colors. Uses the
// neutral border/bg tokens so it themes; the meter carries the strength signal.
function _aiCostClassChip(costClass) {
  if (!costClass) return '';
  return `<span style="display:inline-block;font-size:0.68rem;font-weight:600;padding:1px 6px;border-radius:4px;background:var(--bg);border:1px solid var(--border);color:var(--text2)">${escHtml(String(costClass))}</span>`;
}

async function _aiLoadPod() {
  const engineBody = document.getElementById('ai-pod-engine-body');
  const overrideBody = document.getElementById('ai-pod-override-body');
  if (engineBody) engineBody.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  if (overrideBody) overrideBody.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  try {
    const [resolutionData, overrideData] = await Promise.all([
      api('GET', '/api/models/tier-resolution').catch(() => null),
      api('GET', '/api/admin/engine-tier-override').catch(() => null),
    ]);
    _aiRenderPodEngine(engineBody, resolutionData);
    _aiRenderPodOverride(overrideBody, overrideData?.engine_default_tier ?? null);
  } catch (e) {
    if (engineBody) engineBody.innerHTML = `<div class="empty">Could not load: ${escHtml(String(e))}</div>`;
  }
  // Default tier definitions editor — independent fetch so an engine-panel
  // failure doesn't blank the editor (and vice-versa).
  await _aiLoadPodDefaults();
}

// ── POD "Default tier definitions" editor (spec §Addendum 5) ─────────────────
//
// Edits the pod layer (network.json::models.rungs/roles) exactly like the bot
// tier editor: multi-model clusters per role, add/remove/reorder. Seeded from
// the effective default (defaults ← pod merge). "Reset to Evolve defaults"
// clears the pod layer. Provenance per role: `Evolve default` until an edit
// materializes the pod layer, then `pod` — reuses the ai-layer-chip classes.

// Pending edit buffer — keyed by role id, { models: [...] }. Mirrors the bot
// editor's _aiPendingTiers shape, but pod-scoped.
let _aiPodDefaultsPending = {};
let _aiPodDefaultsView = null;     // last GET response (roleLayers, podConfigured)
let _aiPodDefaultsDirty = false;

async function _aiLoadPodDefaults() {
  const el = document.getElementById('ai-pod-defaults-body');
  if (!el) return;
  el.innerHTML = '<div class="loading"><span class="spinner"></span> Loading…</div>';
  const r = await api('GET', '/api/admin/config/pod/models').catch(() => null);
  if (!r || r.error) {
    el.innerHTML = `<div class="empty">Could not load default tier definitions: ${escHtml(r?.error || 'unknown')}</div>`;
    return;
  }
  _aiPodDefaultsView = r;
  // Load the validated-picker candidate listings (shared with the bot editor).
  await _aiLoadListings();
  // Seed the pending buffer from the effective default clusters, keyed by role.
  _aiPodDefaultsPending = {};
  for (const roleId of _AI_POD_ROLE_ORDER) {
    const rungId = _aiPodRoleToRung(r, roleId);
    const models = rungId ? _aiPodRungModels(r, rungId) : [];
    _aiPodDefaultsPending[roleId] = { models: models.slice() };
  }
  _aiPodDefaultsDirty = false;
  _aiRenderPodDefaults();
}

// Role order + rung-resolution helpers, mirroring the server's role→rung map.
// No model literals — every value comes from the GET response's rungs/roles.
const _AI_POD_ROLE_ORDER = ['fast', 'standard', 'power', 'max', 'judge'];

function _aiPodRoleToRung(view, roleId) {
  const roles = (view && view.roles) || {};
  const entry = roles[roleId];
  if (typeof entry === 'string' && entry) return entry;
  if (entry && typeof entry === 'object' && typeof entry.rung === 'string') return entry.rung;
  return null;
}

function _aiPodRungModels(view, rungId) {
  for (const rung of ((view && view.rungs) || [])) {
    if (rung && rung.id === rungId && Array.isArray(rung.models)) {
      return rung.models.filter(m => typeof m === 'string' && m);
    }
  }
  return [];
}

function _aiRenderPodDefaults() {
  const el = document.getElementById('ai-pod-defaults-body');
  const view = _aiPodDefaultsView;
  if (!el || !view) return;
  const roleLayers = view.roleLayers || {};

  // Zero-credential empty state: the server credential-matches each tier's
  // models to the pod's credentialed providers, so when EVERY ladder+judge role
  // resolves to no model the pod has no credentialed LLM provider at all. Show
  // one legible card-level line instead of a column of "(empty)" tier rows or a
  // phantom fallback — adding a provider key in Settings un-filters the catalog
  // and the tiers fill automatically (the catalog itself is unchanged). Read
  // the server view (not the editable pending buffer) so an operator who merely
  // emptied a tier by hand still sees the editor. Partial credentials (some
  // tiers populated) fall through to the normal per-tier editor below.
  const allTiersEmpty = _AI_POD_ROLE_ORDER.every((roleId) => {
    const rungId = _aiPodRoleToRung(view, roleId);
    return !rungId || _aiPodRungModels(view, rungId).length === 0;
  });
  if (allTiersEmpty) {
    el.innerHTML = '<div class="empty">No credentialed LLM provider yet — add a provider key in Settings and your tiers fill automatically.</div>';
    return;
  }

  const renderRole = (roleId) => {
    const meta = _AI_TIER_ROLE_META[roleId] || {};
    const tobj = _aiPodDefaultsPending[roleId] || { models: [] };
    const models = tobj.models || [];
    const layer = roleLayers[roleId] === 'pod' ? 'pod' : 'default';
    const layerChip = layer === 'pod'
      ? '<span class="ai-layer-chip ai-layer-pod">pod</span>'
      : '<span class="ai-layer-chip ai-layer-default">Evolve default</span>';
    const label = _AI_ROLE_LABELS[roleId] || roleId;
    const hintRow = meta.hint
      ? `<div style="font-size:0.75rem;color:var(--text3);margin-bottom:6px">${escHtml(meta.hint)}</div>`
      : '';

    const modelRows = models.map((m, i) =>
        _aiTierModelRow('pod', roleId, m, i,
          `(idx=>_aiPodTierRemoveModel('${escHtml(roleId)}',idx))`, models.length)
      ).join('') || `<div style="font-size:0.8rem;color:var(--text3);padding:3px 0;font-style:italic">(empty — add models below)</div>`;

    // POD tier editor draws from the FULL credentialed listings, NOT a catalog
    // (the pod default isn't gated by a per-bot allowlist — Phase 10b /
    // Addendum 7 workstream C; this asymmetry vs the bot editor is intended).
    // The soft "recommended for this tier" set (★ suggested — spec §Addendum 6)
    // is the role's CODE-DEFAULT cluster (`view.roleDefaultModels[roleId]`: the
    // layer ABOVE the pod — what "Reset to Evolve defaults" would put here),
    // NOT the pod's current `models` (which are all in `already`, so suggesting
    // them would star nothing). Already-present recommended models stay visible,
    // greyed "(already added)". All credentialed models stay pickable (soft
    // gate). No free-text — pick only.
    const suggested = (view.roleDefaultModels || {})[roleId] || [];
    const addRow = _aiModelPickerRow({
      suggested: suggested,
      already: models,
      pickFnName: `(v=>_aiPodTierAddModel('${escHtml(roleId)}',v))`,
      freeText: false,
      rerenderFnName: '_aiPodDefaults',
    });

    return `<div style="margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:2px">
        <span style="font-weight:600;font-size:0.85rem">${escHtml(label)}</span>
        ${layerChip}
      </div>
      ${hintRow}
      <div style="padding-left:4px">${modelRows}</div>
      ${addRow}
    </div>`;
  };

  const ladderSections = _AI_LADDER_ROLES.map(renderRole).join('');
  const judgeSection = `
    <div style="border-top:1px solid var(--border);margin:4px 0 16px 0"></div>
    ${renderRole('judge')}`;

  const resetDisabled = view.podConfigured ? '' : 'disabled';
  const resetTitle = view.podConfigured
    ? "Clear the pod's tier definitions and fall back to the models Evolve ships with"
    : 'Already on Evolve defaults — nothing to reset';

  // Easy setup is the 90% path (spec §Addendum 7 item 11): a prominent CTA at
  // the TOP of the tier-definitions block, not a small secondary button buried
  // in the footer. The manual ladder editor below is the fallback for the rest.
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px;padding:12px 14px;background:var(--bg2);border:1px solid var(--border);border-radius:8px">
      <button class="btn btn-primary" onclick="_aiOpenEasySetup('pod')" id="ai-pod-defaults-easy-btn" title="Pick a provider order; Evolve fills every tier with sensible defaults in that order">Easy setup</button>
      <span style="font-size:0.8rem;color:var(--text2);flex:1;min-width:220px">Pick your preferred provider order and Evolve fills every tier with a sensible default. Most operators never need the manual editor below.</span>
    </div>
    ${ladderSections}
    ${judgeSection}
    <div style="display:flex;align-items:center;gap:10px;margin-top:4px;flex-wrap:wrap">
      <button class="btn btn-primary btn-sm" onclick="_aiSavePodDefaults()" id="ai-pod-defaults-save-btn">Save default tiers</button>
      <button class="btn btn-ghost btn-sm" onclick="_aiResetPodDefaults()" id="ai-pod-defaults-reset-btn" ${resetDisabled} title="${escHtml(resetTitle)}">Reset to Evolve defaults</button>
      <span id="ai-pod-defaults-status" style="font-size:0.8rem"></span>
    </div>`;
}

function _aiPodTierAddModel(roleId, model) {
  if (!model) return;
  if (!_aiPodDefaultsPending[roleId]) _aiPodDefaultsPending[roleId] = { models: [] };
  if (!(_aiPodDefaultsPending[roleId].models || []).includes(model)) {
    _aiPodDefaultsPending[roleId].models = [...(_aiPodDefaultsPending[roleId].models || []), model];
    _aiPodDefaultsDirty = true;
    _aiRenderPodDefaults();
  }
}

function _aiPodTierRemoveModel(roleId, idx) {
  if (!_aiPodDefaultsPending[roleId]?.models) return;
  _aiPodDefaultsPending[roleId].models.splice(idx, 1);
  _aiPodDefaultsDirty = true;
  _aiRenderPodDefaults();
}

// Refresh hook for the POD default editor's "Refresh" listings control.
function _aiPodDefaults_refresh() {
  _aiRefreshListings(() => _aiRenderPodDefaults());
}

// CANONICAL role → rung id + costClass (catalog ordering data, mirrors
// DEFAULT_MODEL_CATALOG / _DEFAULT_ROLE_TO_RUNG in primary_bot.py — keep in
// sync). judge shares standard's rung (sonnet-class). Emitting these instead of
// synthetic "<role>-default" ids makes a saved pod default OVERLAY the code
// default (matching ids → replace in the keyed merge) instead of accumulating
// (spec Addendum 8 §D). The server canonicalizes again as the enforcement
// point, so an older client still produces a correct catalog; this just keeps
// the wire payload clean. Rung ids/costClass are catalog data, not provider
// literals.
const _AI_ROLE_TO_RUNG = {
  fast: 'haiku-class',
  standard: 'sonnet-class',
  power: 'opus-class',
  max: 'fable-class',
  judge: 'sonnet-class',
};
const _AI_RUNG_COST_CLASS = {
  'haiku-class': 'low',
  'sonnet-class': 'medium',
  'opus-class': 'high',
  'fable-class': 'premium',
};

// Build the rungs/roles payload from the pending per-role clusters, on CANONICAL
// rung ids + costClass. judge folds into the standard (sonnet-class) rung — its
// models append as fallbacks so the diversity option survives — and keeps the
// {rung, provider} shape. No model literals; rung ids/costClass are catalog data.
function _aiPodDefaultsPayload() {
  const rungsById = {};
  const rungs = [];
  const roles = {};
  for (const roleId of _AI_POD_ROLE_ORDER) {
    const models = (_aiPodDefaultsPending[roleId]?.models || []).slice();
    const rungId = _AI_ROLE_TO_RUNG[roleId] || (roleId + '-default');
    let rung = rungsById[rungId];
    if (!rung) {
      rung = { id: rungId, costClass: _AI_RUNG_COST_CLASS[rungId] || null, models: [] };
      rungsById[rungId] = rung;
      rungs.push(rung);
    }
    for (const m of models) {
      if (!rung.models.includes(m)) rung.models.push(m);
    }
    if (roleId === 'judge') {
      roles[roleId] = { rung: rungId, provider: 'not-standard' };
    } else {
      roles[roleId] = rungId;
    }
  }
  return { rungs, roles };
}

async function _aiSavePodDefaults() {
  const statusEl = document.getElementById('ai-pod-defaults-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Saving…</span>';
  const payload = _aiPodDefaultsPayload();
  const r = await api('PUT', '/api/admin/config/pod/models', payload);
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    toast('Save failed: ' + r.error, 'err');
    return;
  }
  _aiPodDefaultsDirty = false;
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
  toast('Default tier definitions saved', 'ok');
  await _aiLoadPodDefaults();
  _aiShowRestartBanner();
}

async function _aiResetPodDefaults() {
  if (!await confirmModal({ body: "Reset the pod's default tier definitions to the models Evolve ships with?\n\nThis clears the pod layer in network.json. Bots set to Use pod defaults will fall back to Evolve's built-in defaults.", danger: true })) {
    return;
  }
  const statusEl = document.getElementById('ai-pod-defaults-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Resetting…</span>';
  const r = await api('PUT', '/api/admin/config/pod/models', { mode: 'reset' });
  if (r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(r.error)}</span>`;
    toast('Reset failed: ' + r.error, 'err');
    return;
  }
  _aiPodDefaultsDirty = false;
  toast('Default tier definitions reset to Evolve defaults', 'ok');
  await _aiLoadPodDefaults();
  _aiShowRestartBanner();
}

// ── Easy-setup wizard (spec §Addendum 6 #2) ─────────────────────────────────
// The 90% path. One button on the POD default editor AND each bot tab. The
// modal asks for a ranked provider-order preference (↑↓ ordering), previews the
// resulting per-tier model order (server-computed: each role's default cluster
// reordered by the preference, filtered to credentialed providers), then writes
// on confirm through the existing safe paths (POST /api/models/easy-setup —
// pod scope splices network.json::models, bot scope flips the bot to Custom).
// No provider/model literals here — the provider list comes from the listings'
// llm_providers (LLM-capable subset; Brave/Runway excluded, DeepSeek included)
// and the per-tier models come back from the server.
let _aiEasyScope = null;        // "pod" | "<bot_id>"
let _aiEasyOrder = [];          // ranked provider ids (lower-cased)
let _aiEasyPreview = null;      // last previewed catalog {rungs, roles, ...}

// Launch the wizard for a scope. Seeds the provider order from the pod's
// LLM-capable credentialed providers (listings-sourced) in their current order.
// Falls back to the raw credentialed set only if an older server omits
// llm_providers (spec §Addendum 7 #12).
async function _aiOpenEasySetup(scope) {
  _aiEasyScope = scope;
  _aiEasyPreview = null;
  await _aiLoadListings();
  const llm = _aiListings && _aiListings.llm_providers;
  const providers = (Array.isArray(llm) ? llm : null)
    || (_aiListings && _aiListings.credentialed_providers) || [];
  _aiEasyOrder = providers.map(p => String(p).toLowerCase());
  _aiRenderEasySetupModal();
}

function _aiCloseEasySetup() {
  const overlay = document.getElementById('ai-easy-setup-modal');
  if (overlay) overlay.remove();
  _aiEasyScope = null;
  _aiEasyOrder = [];
  _aiEasyPreview = null;
}

function _aiEasyMoveProvider(idx, dir) {
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= _aiEasyOrder.length) return;
  [_aiEasyOrder[idx], _aiEasyOrder[newIdx]] = [_aiEasyOrder[newIdx], _aiEasyOrder[idx]];
  _aiEasyPreview = null;   // order changed — the prior preview is stale
  _aiRenderEasySetupModal();
}

function _aiRenderEasySetupModal() {
  let overlay = document.getElementById('ai-easy-setup-modal');
  if (overlay) overlay.remove();
  overlay = document.createElement('div');
  overlay.id = 'ai-easy-setup-modal';
  overlay.className = 'modal-overlay open';

  const scopeLabel = _aiEasyScope === 'pod'
    ? 'pod defaults'
    : (typeof botLabel === 'function' ? botLabel(_aiEasyScope) : _aiEasyScope);

  const orderRows = _aiEasyOrder.length
    ? _aiEasyOrder.map((p, i) => `
        <div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:0.85rem">
          <span style="color:var(--text3);min-width:18px;text-align:right">${i + 1}.</span>
          <span style="flex:1;font-weight:500">${escHtml(_aiProviderLabel(p))}</span>
          <button class="btn btn-ghost btn-sm" style="padding:1px 6px;font-size:0.74rem" title="Higher preference"
            onclick="_aiEasyMoveProvider(${i},-1)" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="btn btn-ghost btn-sm" style="padding:1px 6px;font-size:0.74rem" title="Lower preference"
            onclick="_aiEasyMoveProvider(${i},1)" ${i === _aiEasyOrder.length - 1 ? 'disabled' : ''}>↓</button>
        </div>`).join('')
    : `<div style="font-size:0.82rem;color:var(--text3);font-style:italic;padding:6px 0">No credentialed providers found — add a provider key first.</div>`;

  // Preview: the server-returned reordered per-tier model order. Shown only
  // after Preview is clicked, so the operator confirms an explicit overwrite.
  const previewBlock = _aiEasyPreview
    ? `<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
         <div style="font-size:0.8rem;font-weight:600;margin-bottom:8px">Resulting tiers — preview before writing</div>
         ${_aiEasyPreviewRows(_aiEasyPreview)}
       </div>`
    : `<div style="margin-top:14px;font-size:0.78rem;color:var(--text3)">Click <strong>Preview</strong> to see the resulting per-tier order before writing.</div>`;

  const confirmDisabled = _aiEasyPreview && _aiEasyOrder.length ? '' : 'disabled';

  overlay.innerHTML = `
    <div class="modal" style="max-width:560px">
      <h2>Easy setup — ${escHtml(scopeLabel)}</h2>
      <div style="font-size:0.82rem;color:var(--text2);margin-bottom:12px">
        Rank your providers by preference. Each tier keeps Evolve's sensible default models, reordered so your preferred provider leads its fallback chain. This overwrites the current ${_aiEasyScope === 'pod' ? 'pod default' : 'tier'} config.
      </div>
      <div style="font-size:0.78rem;font-weight:600;color:var(--text2);margin-bottom:4px">Provider preference (most preferred first)</div>
      <div style="padding-left:4px">${orderRows}</div>
      ${previewBlock}
      <div id="ai-easy-setup-status" style="font-size:0.8rem;margin-top:10px"></div>
      <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">
        <button class="btn btn-ghost btn-sm" onclick="_aiCloseEasySetup()">Cancel</button>
        <button class="btn btn-ghost btn-sm" id="ai-easy-preview-btn" onclick="_aiEasyPreviewRun()" ${_aiEasyOrder.length ? '' : 'disabled'}>Preview</button>
        <button class="btn btn-primary btn-sm" id="ai-easy-confirm-btn" onclick="_aiEasyConfirm()" ${confirmDisabled} title="Overwrites the current config with the previewed tiers">Apply easy setup</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
}

// Render the previewed catalog's per-tier model order, role-by-role. Maps each
// role to its rung's models[] (the reordered fallback chain). No literals — the
// rung/role wiring all comes from the server-computed catalog.
//
// The judge row is the one exception: judge SHARES standard's rung in the
// written config ({rung, provider:"not-standard"}), so rendering its rung raw
// would print byte-identical to Standard and mislead the operator into thinking
// judge uses standard's provider. Instead this mirrors the runtime resolver
// (_resolve_judge_availability / ModelRouter._resolveJudgeModel): judge skips
// standard's resolved provider and leads with the first non-standard model
// (resolving to null, never falling back to standard). We render that effective
// chain so the preview is honest — without touching the written config.
function _aiEasyPreviewRows(catalog) {
  const rungs = (catalog && catalog.rungs) || [];
  const roles = (catalog && catalog.roles) || {};
  const rungById = {};
  for (const r of rungs) { if (r && r.id) rungById[r.id] = r; }
  const rungForRole = (roleId) => {
    const entry = roles[roleId];
    if (typeof entry === 'string') return rungById[entry];
    if (entry && typeof entry === 'object' && entry.rung) return rungById[entry.rung];
    return null;
  };
  const modelsForRole = (roleId) => {
    const rung = rungForRole(roleId);
    return (rung && rung.models) || [];
  };
  const chip = (m) => _aiModelChip(String(m).replace('anthropic/', ''), _aiProviderForModel(m));
  const arrow = ' <span style="color:var(--text3)">→</span> ';

  // Standard's resolved provider = provider of the first model in STANDARD's own
  // rung chain (the resolver walks it head-to-tail; the preview catalog is
  // already credentialed-filtered + preference-ordered, so models[0] is what
  // standard resolves to). Derived from standard's own rung — NOT judge's first
  // entry — because, though they share a rung in the default catalog, the
  // renderer must match the runtime contract, which compares to standard's
  // resolved provider specifically.
  const stdModels = modelsForRole('standard');
  const stdProvider = stdModels.length ? _aiProviderForModel(stdModels[0]) : '';

  return _AI_POD_ROLE_ORDER.map((roleId) => {
    const models = modelsForRole(roleId);
    const label = _AI_ROLE_LABELS[roleId] || roleId;

    let modelText;
    let caption = '';
    if (roleId === 'judge') {
      // Lead with the non-standard models (what judge actually resolves to);
      // trail any standard-provider model(s) muted + marked "skipped" so the
      // operator sees they're present in the rung but never used for judge.
      const nonStd = models.filter(m => _aiProviderForModel(m) !== stdProvider);
      const skipped = models.filter(m => _aiProviderForModel(m) === stdProvider);
      if (!nonStd.length) {
        // Validator already rejects a judge rung with only standard-provider
        // models on write; render honestly if a preview ever hits it.
        modelText = `<span style="font-size:0.76rem;color:var(--text3);font-style:italic">(no non-Standard model)</span>`;
      } else {
        const lead = nonStd.map(chip).join(arrow);
        const trail = skipped.length
          ? `&nbsp;<span style="font-size:0.76rem;color:var(--text3)">skipped:</span> `
            + skipped.map(m => `<span style="opacity:0.5" title="Skipped for Judge — same provider as Standard">${chip(m)}</span>`).join(' ')
          : '';
        modelText = lead + trail;
      }
      caption = `<div style="font-size:0.76rem;color:var(--text3);margin-top:1px">Provider diversity — Judge avoids Standard’s provider.</div>`;
    } else {
      modelText = models.length
        ? models.map(chip).join(arrow)
        : `<span style="font-size:0.76rem;color:var(--text3);font-style:italic">(no credentialed model — tier empty)</span>`;
    }
    return `<div style="display:flex;gap:8px;padding:3px 0;font-size:0.8rem">
        <span style="min-width:70px;font-weight:600">${escHtml(label)}</span>
        <span style="flex:1">${modelText}${caption}</span>
      </div>`;
  }).join('');
}

async function _aiEasyPreviewRun() {
  if (!_aiEasyScope || !_aiEasyOrder.length) return;
  const statusEl = document.getElementById('ai-easy-setup-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Computing preview…</span>';
  const r = await api('POST', '/api/models/easy-setup', {
    scope: _aiEasyScope, provider_order: _aiEasyOrder, preview: true,
  });
  if (!r || r.error) {
    _aiEasyPreview = null;
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml((r && r.error) || 'preview failed')}</span>`;
    _aiRenderEasySetupModal();
    return;
  }
  _aiEasyPreview = r.catalog || null;
  _aiRenderEasySetupModal();
}

async function _aiEasyConfirm() {
  if (!_aiEasyScope || !_aiEasyPreview) return;
  // No native confirm() here: the easy-setup modal already IS the confirmation
  // surface — the operator ranks providers, clicks Preview, reviews the per-tier
  // model order, then clicks "Apply easy setup" (whose title states it overwrites
  // the current config with the previewed tiers). A native window.confirm() is a
  // redundant second gate AND a silent no-op in the Tauri/wry desktop webview
  // (no script-dialog delegate wired), which made this button do nothing on the
  // desktop app while working in a localhost browser.
  const scope = _aiEasyScope;
  const order = _aiEasyOrder.slice();
  const statusEl = document.getElementById('ai-easy-setup-status');
  if (statusEl) statusEl.innerHTML = '<span style="color:var(--text2)">Writing…</span>';
  const r = await api('POST', '/api/models/easy-setup', {
    scope, provider_order: order,
  });
  if (!r || r.error) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml((r && r.error) || 'write failed')}</span>`;
    toast('Easy setup failed: ' + ((r && r.error) || 'write failed'), 'err');
    return;
  }
  toast('Easy setup applied', 'ok');
  _aiCloseEasySetup();
  // Reload the editor that launched the wizard so it reflects the new config.
  if (scope === 'pod') {
    await _aiLoadPodDefaults();
  } else if (typeof _aiRefreshBotConfig === 'function') {
    await _aiRefreshBotConfig(scope);
  }
  _aiShowRestartBanner();
}

function _aiRenderPodEngine(el, data) {
  if (!el) return;
  if (!data || data.error) {
    el.innerHTML = `<div class="empty">Could not load tier resolution: ${escHtml(data?.error || 'unknown')}</div>`;
    return;
  }
  const primaryBot = data.primary_bot || '';
  // botLabel() falls back to the raw ID when no display_name is set.
  // On pods where the primary bot was renamed (e.g. ID is still the
  // legacy "evolve" account but the friendly name is "evo"), this is
  // what makes the UI match what the operator sees on the bot tabs.
  const primaryBotName = primaryBot ? botLabel(primaryBot) : '<no primary bot>';
  const providers = data.available_providers || [];
  const engineOverride = data.engine_default_tier;

  const providersStr = providers.length
    ? providers.map(p => `<code style="background:rgba(0,0,0,0.18);padding:1px 6px;border-radius:4px">${escHtml(p)}</code>`).join(' ')
    : '<span class="subtle">none readable</span>';

  const overrideBanner = engineOverride
    ? `<div style="background:rgba(127,200,255,0.10);border:1px solid rgba(127,200,255,0.30);border-radius:6px;padding:8px 12px;margin-bottom:10px;font-size:0.82rem">
         <strong>Engine override active</strong> — all background calls collapse to
         <code>${escHtml(engineOverride)}</code> (${escHtml(_AI_TIER_TO_ROLE_LABEL[engineOverride] || engineOverride)})
         regardless of what the caller requests.
       </div>`
    : '';

  // ALL FIVE roles render unconditionally from the merged view
  // (defaults ← pod ← bot). The per-row winning-LAYER provenance chip was
  // dropped on this panel (Phase 10c / Addendum 7 item 9) — the engine
  // resolves entirely from the primary bot, so the chip was confusing noise.
  // Max ships armed via the code defaults, so it always has a row even with
  // no pod/bot config.
  //
  // Rank presentation (spec §Addendum3.C): ladder roles render in canonical
  // ASCENDING order (Fast → Standard → Power → Max) with a derived filled-
  // steps meter; judge renders in its OWN section under a divider with NO
  // meter (off-ladder by design). No "Tier N" numerals anywhere.
  const roles = data.roles || {};
  const ladderCards = _AI_LADDER_ROLES.map(roleId =>
    _aiRenderRoleCard(roleId, roles[roleId], primaryBotName)
  ).filter(Boolean).join('');
  const judgeCard = _aiRenderJudgeCard(roles['judge'], primaryBotName);

  el.innerHTML = `
    ${overrideBanner}
    <div style="font-size:0.82rem;color:var(--text2);margin-bottom:12px">
      Primary bot: <code>${escHtml(primaryBotName)}</code> &nbsp;·&nbsp;
      Providers with credentials: ${providersStr}
    </div>
    <div style="font-size:0.76rem;color:var(--text3);margin-bottom:12px;font-style:italic">
      Resolved engine models — read-only. To change these, edit the primary
      bot's tier definitions.
    </div>
    <div style="display:flex;flex-direction:column;gap:8px">${ladderCards}</div>
    ${judgeCard ? `
      <div style="border-top:1px solid var(--border);margin:16px 0 12px 0"></div>
      ${judgeCard}` : ''}`;
}

// One ladder-role card: label, derived rank meter, costClass chip, resolved
// model, relational subtitle, and availability (no per-row layer provenance —
// dropped on the engine panel in Phase 10c, item 9). An unavailable role is
// grayed with its computed reason (spec §Addendum3.A.3 / §Addendum3.C) — never
// hidden.
function _aiRenderRoleCard(roleId, r, primaryBotName) {
  if (!r) return '';
  const label = _AI_ROLE_LABELS[roleId] || roleId;
  const subtitle = _AI_ROLE_SUBTITLES[roleId] || '';
  const available = r.available !== false;
  const meter = _aiRankMeter(r.rank, r.rungCount);
  const costChip = _aiCostClassChip(r.costClass);
  // Prefer the availability-resolved model; fall back to the rung primary.
  const model = (available ? (r.resolvedModel || r.primary) : (r.resolvedModel || r.primary)) || '—';
  const dimStyle = available ? '' : 'opacity:0.55';
  const reasonRow = !available && r.unavailableReason
    ? `<div style="font-size:0.74rem;color:var(--yellow);margin-top:3px">Unavailable — ${escHtml(_aiAvailabilityReasonText(r.unavailableReason, roleId))}</div>`
    : '';
  const degradeRow = available && r.degraded && r.degradeReason
    ? `<div style="font-size:0.74rem;color:var(--text3);margin-top:3px">Degraded — ${escHtml(_aiAvailabilityReasonText(r.degradeReason, roleId))}</div>`
    : '';
  return `
    <div style="display:flex;gap:12px;align-items:flex-start;padding:9px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:8px;${dimStyle}">
      <div style="min-width:118px">
        <div style="font-weight:600;font-size:0.85rem">${escHtml(label)}</div>
        <div style="display:flex;gap:6px;align-items:center;margin-top:4px">${meter}${costChip}</div>
      </div>
      <div style="flex:1;min-width:0">
        <div style="font-family:monospace;font-size:0.82rem;color:var(--text1)">${escHtml(model)}</div>
        <div style="font-size:0.74rem;color:var(--text2);margin-top:2px">${escHtml(subtitle)}</div>
        ${reasonRow}${degradeRow}
      </div>
    </div>`;
}

// Judge card — its own section, NO meter (off-ladder), with the fixed
// diversity copy (spec §Addendum3.C.4). Availability still surfaces grayed.
function _aiRenderJudgeCard(r, primaryBotName) {
  if (!r) return '';
  const available = r.available !== false;
  const dimStyle = available ? '' : 'opacity:0.55';
  const model = (r.resolvedModel || r.primary) || '—';
  const reasonRow = !available && r.unavailableReason
    ? `<div style="font-size:0.74rem;color:var(--yellow);margin-top:3px">Unavailable — ${escHtml(_aiAvailabilityReasonText(r.unavailableReason, 'judge'))}</div>`
    : '';
  return `
    <div style="display:flex;gap:12px;align-items:flex-start;padding:9px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:8px;${dimStyle}">
      <div style="min-width:118px">
        <div style="font-weight:600;font-size:0.85rem">Judge</div>
        ${_aiCostClassChip(r.costClass)}
      </div>
      <div style="flex:1;min-width:0">
        <div style="font-family:monospace;font-size:0.82rem;color:var(--text1)">${escHtml(model)}</div>
        <div style="font-size:0.74rem;color:var(--text2);margin-top:2px">${escHtml(_AI_ROLE_SUBTITLES.judge)}</div>
        ${reasonRow}
      </div>
    </div>`;
}

// Humanize the unified degradation reason (spec §Addendum3.A.2):
// cap_exhausted | uncredentialed | unconfigured. No provider literals — the
// computed provider set, when present, is appended from the resolver data.
function _aiAvailabilityReasonText(reason, roleId) {
  const label = _AI_ROLE_LABELS[roleId] || roleId;
  const map = {
    uncredentialed: `no credentialed provider for ${label} on this pod`,
    unconfigured:   `${label} has no configured rung`,
    cap_exhausted:  `${label}’s daily cap is exhausted`,
    diversity_unsatisfiable: `no provider distinct from Standard is available for Judge`,
  };
  return map[reason] || String(reason || 'unavailable');
}

function _aiRenderPodOverride(el, currentVal) {
  if (!el) return;
  // Render four radio-like buttons: Auto (null), then the three ladder roles
  // in canonical ascending order. The Judge role is deliberately not in the
  // picker — it is about cross-vendor evaluation and shouldn't be overridable
  // to the same value pod-wide. Numeral-free role labels (spec §Addendum3.C.1);
  // the stored value stays tierN (the engine override is a tier pin, not a role).
  const opts = [
    { val: null,    label: 'Auto',     hint: 'Engine code uses whatever role it requests (current default)' },
    { val: 'tier3', label: 'Fast',     hint: 'Pin all background work to the cheapest role — recommended for cost control' },
    { val: 'tier2', label: 'Standard', hint: 'Pin to the everyday model — rarely needed' },
    { val: 'tier1', label: 'Power',    hint: 'Pin to the top role — DO NOT use except for testing; pricey' },
  ];
  const rows = opts.map(o => {
    const checked = (o.val === currentVal);
    const yellowWarn = o.val === 'tier1' ? 'color:var(--yellow)' : '';
    return `
      <label style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;cursor:pointer;border-bottom:1px solid var(--border)" onclick="_aiSetEngineTier(${JSON.stringify(o.val)})">
        <input type="radio" name="ai-engine-override" ${checked ? 'checked' : ''} style="margin-top:4px;cursor:pointer">
        <div style="flex:1">
          <div style="font-weight:600;font-size:0.85rem">${escHtml(o.label)}</div>
          <div style="font-size:0.76rem;color:var(--text2);margin-top:2px;${yellowWarn}">${escHtml(o.hint)}</div>
        </div>
      </label>`;
  }).join('');
  el.innerHTML = rows;
}

async function _aiSetEngineTier(newVal) {
  try {
    const r = await api('PUT', '/api/admin/engine-tier-override', { engine_default_tier: newVal });
    if (r.error) {
      toast('Could not save: ' + r.error, 'err');
      return;
    }
    toast(newVal === null ? 'Engine override cleared' : `Engine tier pinned to ${newVal}`, 'ok');
    // Reload the POD view so the banner + table reflect the new state
    await _aiLoadPod();
  } catch (e) {
    toast('Save failed: ' + String(e), 'err');
  }
}

async function aiSwitchBot(botName) {
  if (_aiDirtySection !== null) {
    if (!await confirmModal({ body: 'You have unsaved changes in ' + _aiDirtySection + '. Discard and switch?', danger: true })) return;
  }
  _aiBot = botName;  // "" sentinel for POD tab
  _aiDirtySection = null;
  // Hide the restart banner — it belongs to the previous bot's pending changes
  const bannerEl = document.getElementById('ai-restart-banner');
  if (bannerEl) bannerEl.style.display = 'none';
  // Match by data-bot — display names diverge from bot ids after a rename.
  // POD tab uses data-bot="" so the empty-string match works here.
  document.querySelectorAll('#ai-bot-tabs .subtab').forEach(t => {
    t.classList.toggle('active', t.dataset.bot === botName);
  });
  // Toggle POD vs per-bot view
  const podView = document.getElementById('ai-pod-view');
  const botView = document.getElementById('ai-bot-view');
  if (botName === "") {
    if (podView) podView.style.display = '';
    if (botView) botView.style.display = 'none';
    await _aiLoadPod();
  } else {
    if (podView) podView.style.display = 'none';
    if (botView) botView.style.display = '';
    await _aiRenderBotData(botName);
  }
}

