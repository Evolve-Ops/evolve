/**
 * ModelRouter — wires session classification to OC's before_model_resolve hook.
 *
 * Reads session type from TurnObserver's in-memory state and returns the
 * appropriate model override based on tier configuration.
 *
 * Design principles:
 * - NEVER changes model for productive or ambiguous sessions (use default)
 * - Only overrides for: maintenance, background, cron
 * - Respects user-explicit overrides (/model command already set)
 * - Fails open: if classification unavailable, return {} (no override)
 * - No LLM calls, no async I/O in the hot path — pure in-memory lookup
 *
 * Config source priority:
 *   1. ~/.openclaw/evolve-tiers.json   — written by admin UI's AI
 *                                        Optimization page (canonical)
 *   2. {sharedDir}/{botId}/tiers.json  — legacy / hand-rolled fallback
 *   3. network.json models.*           — pod-wide fallback
 *   4. Fail open (empty config → bot default model wins everything)
 *
 * NOTE: tiers are NOT read from openclaw.json. OC's schema validator rejects
 * unknown fields under agents.defaults.model, so Evolve stores tier/routing
 * config in its own evolve-tiers.json file under the bot's .openclaw dir.
 */
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
/**
 * Pod-local "today" as YYYY-MM-DD.
 *
 * `Date.prototype.toISOString()` always emits UTC; on a pod west of UTC that
 * means the date string rolls over hours before pod-local midnight (e.g. in
 * UTC-6 it flips at 6pm local). Daily windows must use the operator's day,
 * not Greenwich's — see docs/spec-user-tier-control-2026-05-26.md §"Pod-local
 * midnight". Construct from the local-TZ getters instead.
 */
function localDateYMD(d = new Date()) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}
/** Path to today's spend-cap enforcement flag for a bot. */
function spendCapFlagPath(sharedDir, botId) {
    return path.join(sharedDir, "spend-caps", `${botId}-${localDateYMD()}.json`);
}
/**
 * Return true if a hard-spend-cap enforcement flag is active for this bot today.
 * Checks {sharedDir}/spend-caps/{botId}-{YYYY-MM-DD}.json.
 * Only tier-downgrade actions are enforced here; other actions (pause-crons,
 * suspend-bot) are executed by spend_alert.py at trigger time.
 */
function isSpendCapActive(sharedDir, botId) {
    try {
        const fp = spendCapFlagPath(sharedDir, botId);
        const data = JSON.parse(fs.readFileSync(fp, "utf8"));
        return !data.cleared && data.action === "downgrade-tier";
    }
    catch {
        return false;
    }
}
/**
 * Session classes that ACTUALLY drive a routing decision in
 * resolveModelOverride (productive falls through to bot default but is
 * still informative; the other two route to grunt-tier). ``ambiguous``
 * is the "no useful information" sentinel — keyword classifier returns
 * it for empty/unscoreable inputs.
 */
const _SPECIFIC_SESSION_CLASSES = new Set([
    "productive",
    "maintenance",
    "background",
]);
function _IS_SPECIFIC_CLASS(c) {
    return typeof c === "string" && _SPECIFIC_SESSION_CLASSES.has(c);
}
/**
 * Sentinel returned by the safety-net branches (runaway-rate, spend-cap)
 * when the configured downgrade target (the `fast` role) is empty. OC will
 * fail to resolve this model — refusing the turn — which is the correct
 * behavior: a breaker firing means we want to STOP cost, not silently
 * fall back to bot default and keep spending.
 *
 * Replaces the previous behavior (PR #1767) which substituted a
 * hardcoded model literal "anthropic/claude-haiku-4-5". That worked
 * but violated the principle "no hardcoded model names in code" —
 * Evolve must never embed knowledge of specific provider models in
 * conditional logic, only tier names that resolve via operator config.
 *
 * The previous chain of safety-net behaviors:
 *
 *   Original (pre-#1767):   return null  → bot default → lying telemetry
 *                           (breaker reported as fired; actually no-op)
 *   #1767:                  return hardcoded haiku → cost capped but
 *                           operator's "no hardcoded models" principle
 *                           violated
 *   This PR:                return sentinel → OC fails to resolve →
 *                           turn errors out → bot stops spending → loud
 *                           operator-visible signal in gateway.log
 *
 * The startup-time warn (_warnIfSafetyNetWithoutTier3) still fires
 * when this is wired up but tier3 is empty — the operator hears
 * about the misconfig at boot, before any breaker has to refuse.
 * Telemetry remains honest: driver stamps "spend_cap" / "runaway"
 * and the model is this sentinel, so audits can see the refusal
 * count distinctly from successful downgrades.
 *
 * Format: ``evolve/<reason>`` — colon-free, slash-separated so OC's
 * tokenizer parses it as a provider/model pair (provider "evolve" is
 * never registered, so the lookup fails cleanly).
 */
const _SAFETY_NET_REFUSE_SENTINEL = "evolve/safety-net-blocked-fast-unconfigured"; // provider-literal-allow: sentinel, "evolve" is not a real provider
/**
 * Roles a classifier / per-bot default may legitimately resolve to.
 * `max` is pull-only (§max semantics #3): configuring it as a default,
 * maintenance, background, or ambiguous role is a config error and is
 * rejected by validation. `judge` is selected by provider diversity, not
 * by classifier routing, so it is not a classifier role either.
 */
const _CLASSIFIER_ROLES = new Set(["fast", "standard", "power"]);
/**
 * Read the bot's tier/routing config — the JSON the admin UI's "AI
 * Optimization" page writes to.
 *
 * Path resolution:
 *   1. `~/.openclaw/evolve-tiers.json` — the canonical location the
 *      admin UI writes to, via the same backend route that powers
 *      the Tier Definitions / Model Catalog / Fallback panels.
 *   2. (Legacy) `{sharedDir}/{botId}/tiers.json` — the path the plugin
 *      USED to read but the admin UI never wrote to. Read here as a
 *      back-compat fallback so any pod that hand-rolled the file
 *      keeps working.
 *
 * Returns {} when neither file exists; ModelRouter handles the
 * empty-config case by falling through to bot defaults.
 *
 * Pre-2026-05-28: the plugin ONLY read path #2, but the UI ONLY wrote
 * path #1 — a silent file-path mismatch that made the operator's tier
 * configuration invisible to routing. The plugin's intent was that
 * the admin UI would write the file the plugin reads; correcting
 * here matches that intent.
 */
function loadTiersFile(sharedDir, botId) {
    // #1: Bot-home evolve-tiers.json (admin UI writes here).
    try {
        const homePath = path.join(os.homedir(), ".openclaw", "evolve-tiers.json");
        return JSON.parse(fs.readFileSync(homePath, "utf8"));
    }
    catch {
        /* try the legacy fallback */
    }
    // #2: Shared-dir tiers.json (legacy / hand-rolled).
    try {
        const tiersPath = path.join(sharedDir, botId, "tiers.json");
        return JSON.parse(fs.readFileSync(tiersPath, "utf8"));
    }
    catch {
        return {};
    }
}
/**
 * Map from a legacy `tierN` key to the role it became
 * (spec §Legacy-shape fallback). tier3->fast, tier2->standard,
 * tier1->power, tier0->judge. Doubles as the read-side translation for
 * historical telemetry that recorded `model_tier: "tierN"`.
 */
const _LEGACY_TIER_TO_ROLE = {
    tier3: "fast",
    tier2: "standard",
    tier1: "power",
    tier0: "judge",
};
/** Stable rung slug a synthesized role points at, per legacy tier. */
const _LEGACY_TIER_TO_RUNG = {
    tier3: "haiku-class",
    tier2: "sonnet-class",
    tier1: "opus-class",
    tier0: "sonnet-class", // judge rides the sonnet-class rung (provider-diverse)
};
/**
 * DEFAULT_MODEL_CATALOG — Evolve's blessed model ladder, shipped in code.
 *
 * KEEP IN SYNC with `DEFAULT_MODEL_CATALOG` in
 * packages/analyzer/primary_bot.py — the two must resolve a given (pod, bot)
 * override pair to byte-identical merged catalogs. A reviewer traces parity
 * rule-by-rule; the parity fixtures on both sides enforce it.
 *
 * Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2 (2026-06-10):
 * product capabilities ship as code defaults; proposals/config carry instance
 * state. This is the BASE layer of the keyed merge:
 *
 *     code defaults (this) ← network.json (pod) ← evolve-tiers.json (bot)
 *
 * Max ships ARMED, not dormant — cost safety holds via pull-only routing, the
 * per-role daily cap (roleCaps.max.maxPerDayPerBot), and the per-bot breaker.
 *
 * MODEL LAUNCH: update this catalog (and the Python mirror) at each release
 * when the blessed ladder changes — new frontier SKU, new fast-band entry, a
 * retired model. Discovery surfaces market drift when this lags (by design).
 */
// provider-literal-allow-begin: catalog DATA (home #1 of the three-homes rule)
export const DEFAULT_MODEL_CATALOG = {
    rungs: [
        {
            id: "haiku-class",
            costClass: "low",
            // anthropic FIRST (blessed default primary); openai/google/xai are each
            // provider's latest tier3-appropriate model so easy-setup can populate
            // every tier with every credentialed provider's model (Addendum 7 #13).
            models: [
                "anthropic/claude-haiku-4-5",
                "openai/gpt-4.1-mini",
                "google/gemini-2.0-flash",
                "xai/grok-4-mini",
            ],
        },
        {
            id: "sonnet-class",
            costClass: "medium",
            models: [
                "anthropic/claude-sonnet-4-6",
                "openai/gpt-4.1",
                "google/gemini-2.5-pro",
                "xai/grok-4",
            ],
        },
        {
            id: "opus-class",
            costClass: "high",
            models: [
                "anthropic/claude-opus-4-8",
                "openai/gpt-4.1",
                "google/gemini-2.5-pro",
                "xai/grok-4",
            ],
        },
        {
            id: "fable-class",
            costClass: "premium",
            // max stays anthropic-only — no peer frontier SKU yet.
            models: ["anthropic/claude-fable-5"],
        },
    ],
    roles: {
        fast: "haiku-class",
        standard: "sonnet-class",
        power: "opus-class",
        max: "fable-class",
        // judge is rung-constrained: sonnet-class, diversity-constrained to a
        // provider other than the standard role's primary (sonnet-class holds
        // several non-anthropic providers so judge diversity is satisfiable from
        // defaults alone).
        judge: { rung: "sonnet-class", provider: "not-standard" },
    },
    roleCaps: {
        power: { maxPerDayPerBot: 10 },
        max: { maxPerDayPerBot: 5 },
    },
};
// provider-literal-allow-end
/** Deep copy of DEFAULT_MODEL_CATALOG — never hand out the shared constant. */
export function defaultModelCatalog() {
    return JSON.parse(JSON.stringify(DEFAULT_MODEL_CATALOG));
}
let _loggedLegacySynthesis = false;
/**
 * Synthesize the rungs/roles config from whichever shape the source
 * carries. Accepts both:
 *   - new shape: { rungs: [...], roles: {...}, roleCaps: {...} }
 *   - legacy shape: { tiers: { tier0..tier3: {models} } } — fail-open
 *     back-compat for an un-migrated pod (spec §Legacy-shape fallback).
 *     Synthesizes one rung per tier (preserving cost order
 *     tier3<tier2<tier1<tier0-onto-sonnet) and a role map per
 *     _LEGACY_TIER_TO_ROLE, logging a one-time deprecation warning.
 *
 * Returns `{ rungs, roles, roleCaps }` — never throws. An empty/absent
 * source yields empty rungs + roles (the loader then falls through to
 * bot defaults exactly as before).
 *
 * `legacyCaps` carries the old `userTierOverride.dailyCap` (power-role
 * cap) so the caller can fold it into roleCaps.power when the new
 * roleCaps block is absent.
 *
 * NOTE: legacy `tiers.tierN.fallbacks` are read on the Python side only
 * (primary_bot._normalize_legacy_layer folds them into the cluster) — by
 * design, see F2 of the #2561 review. TS reads `tiers.tierN.models` only and
 * routes legacy fallbacks via the profile-fallback chain; don't "fix" this to
 * fold fallbacks here.
 */
export function synthesizeRungsRoles(source) {
    const src = source && typeof source === "object" ? source : {};
    // New shape wins when present — rungs[] is the discriminator.
    if (Array.isArray(src.rungs) && src.rungs.length > 0) {
        return {
            rungs: src.rungs,
            roles: (src.roles ?? {}),
            roleCaps: src.roleCaps,
        };
    }
    // Legacy shape: synthesize from tiers.tierN. costClass cost-order is
    // implicit in the rung array order (cheapest first).
    const tiers = src.tiers;
    if (tiers && typeof tiers === "object") {
        const rungs = [];
        const roles = {};
        // Emit rungs in cost order so array position stays a valid cost rank.
        const COST_ORDER = [
            ["tier3", "low"],
            ["tier2", "medium"],
            ["tier1", "high"],
        ];
        let synthesized = false;
        for (const [tierKey, costClass] of COST_ORDER) {
            const t = tiers[tierKey];
            const models = Array.isArray(t?.models) ? t.models : [];
            if (models.length === 0)
                continue;
            const rungId = _LEGACY_TIER_TO_RUNG[tierKey];
            rungs.push({ id: rungId, models, costClass });
            roles[_LEGACY_TIER_TO_ROLE[tierKey]] = rungId;
            synthesized = true;
        }
        // tier0 (judge) shares the sonnet-class rung but carries the
        // provider-diversity constraint. If tier0 has models that aren't in
        // the synthesized sonnet-class rung, fold them in as fallbacks so
        // role resolution can find a provider-diverse option.
        const tier0 = tiers.tier0;
        const tier0Models = Array.isArray(tier0?.models) ? tier0.models : [];
        if (tier0Models.length > 0) {
            const sonnetRung = rungs.find((r) => r.id === "sonnet-class");
            if (sonnetRung) {
                for (const m of tier0Models) {
                    if (!sonnetRung.models.includes(m))
                        sonnetRung.models.push(m);
                }
            }
            else {
                rungs.unshift({ id: "sonnet-class", models: tier0Models, costClass: "medium" });
            }
            roles.judge = { rung: "sonnet-class", provider: "not-standard" };
            synthesized = true;
        }
        if (synthesized && !_loggedLegacySynthesis) {
            _loggedLegacySynthesis = true;
            try {
                // eslint-disable-next-line no-console
                console.warn("[Evolve ModelRouter] DEPRECATION: synthesized rungs/roles from a " +
                    "legacy models.tiers (tier0-tier3) config. Run " +
                    "`evolve-admin migrate-model-roles` to rewrite config to the " +
                    "rungs/roles shape. The legacy-shape fallback is removed after " +
                    "one release cycle.");
            }
            catch { /* never let logging crash a load */ }
        }
        return { rungs, roles };
    }
    return { rungs: [], roles: {} };
}
/**
 * Keyed merge of a pod-base catalog with a per-bot override layer.
 *
 * Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum (A.4).
 * Mirrors `primary_bot.merge_model_catalog` on the Python read side — keep
 * the two in sync.
 *
 * Both inputs are `models`-block shapes ({ rungs, roles, roleCaps, ... }).
 * The merge is keyed, not block-precedence:
 *   - rungs    — merged by `id`. A per-bot rung with the same id wins
 *     wholesale; base-only rungs are appended after the merged set in pod
 *     order; override-only rungs keep their order at the end.
 *   - roles    — merged by role key; the per-bot entry wins.
 *   - roleCaps — merged by role key; the per-bot entry wins.
 * Other scalar keys take the override value when present, else base.
 *
 * Block-precedence (the pre-Addendum behavior) made a pod-wide adoption
 * invisible: every bot carries per-bot rungs, so override always won the
 * whole block and a base-only rung (a freshly adopted model) never
 * surfaced. Keyed merge fixes exactly that.
 *
 * When neither side carries a new-shape `rungs` array there is nothing to
 * keyed-merge, so block-precedence is preserved (override wins) — a
 * legacy-only or empty file resolves exactly as before.
 */
/** Models in `rung` that actually name something (non-blank strings). */
function usableModels(rung) {
    if (!rung || typeof rung !== "object" || !Array.isArray(rung.models))
        return [];
    return rung.models.filter((m) => typeof m === "string" && m.trim() !== "");
}
/**
 * Merge one same-id override rung onto its base rung (override wins).
 *
 * Empty-models override = no-op FOR MODELS. The spec's rule is "explicit
 * config wins wherever it speaks" (§Addendum 2). A rung whose `models` list is
 * empty or all-whitespace does NOT speak for models — so it must not SHADOW
 * the base (code-default / pod) rung and brick resolution for that rung's role
 * (e.g. an operator-written `sonnet-class: {models: []}` silently bricking
 * `standard`). Such an override keeps the base rung's models while still
 * applying its other fields (`costClass` etc.). A rung with at least one
 * usable model wins wholesale, as before.
 *
 * Mirrors `_merge_one_rung` in primary_bot.py — keep the two in sync.
 */
function mergeOneRung(baseRung, overRung) {
    if (usableModels(overRung).length > 0)
        return overRung;
    return { ...baseRung, ...overRung, models: baseRung?.models ?? [] };
}
function mergeTwo(base, override) {
    const b = base && typeof base === "object" ? base : {};
    const o = override && typeof override === "object" ? override : {};
    const baseRungs = Array.isArray(b.rungs) ? b.rungs : [];
    const overRungs = Array.isArray(o.rungs) ? o.rungs : [];
    if (baseRungs.length === 0 && overRungs.length === 0) {
        // Nothing to keyed-merge — preserve block precedence (override wins).
        return Object.keys(o).length > 0 ? { ...o } : { ...b };
    }
    const merged = { ...b, ...o }; // override wins for scalar/other keys
    // rungs: merge by id.
    const overById = new Map();
    const overOrder = [];
    for (const r of overRungs) {
        if (r && typeof r === "object" && typeof r.id === "string") {
            overById.set(r.id, r);
            overOrder.push(r.id);
        }
    }
    const mergedRungs = [];
    const seen = new Set();
    for (const r of baseRungs) {
        if (!r || typeof r !== "object")
            continue;
        const rid = r.id;
        if (typeof rid === "string" && overById.has(rid)) {
            mergedRungs.push(mergeOneRung(r, overById.get(rid)));
            seen.add(rid);
        }
        else {
            mergedRungs.push(r);
            if (typeof rid === "string")
                seen.add(rid);
        }
    }
    for (const rid of overOrder) {
        if (!seen.has(rid)) {
            mergedRungs.push(overById.get(rid));
            seen.add(rid);
        }
    }
    merged.rungs = mergedRungs;
    // roles: merge by key (override wins).
    const baseRoles = b.roles && typeof b.roles === "object" ? b.roles : {};
    const overRoles = o.roles && typeof o.roles === "object" ? o.roles : {};
    if (Object.keys(baseRoles).length || Object.keys(overRoles).length) {
        merged.roles = { ...baseRoles, ...overRoles };
    }
    // roleCaps: merge by key (override wins).
    const baseCaps = b.roleCaps && typeof b.roleCaps === "object" ? b.roleCaps : {};
    const overCaps = o.roleCaps && typeof o.roleCaps === "object" ? o.roleCaps : {};
    if (Object.keys(baseCaps).length || Object.keys(overCaps).length) {
        merged.roleCaps = { ...baseCaps, ...overCaps };
    }
    return merged;
}
/**
 * Keyed merge folding the code-default base layer beneath base/override.
 *
 * Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2 — the
 * canonical layering is:
 *
 *     DEFAULT_MODEL_CATALOG ← base (network.json pod) ← override (bot)
 *
 * The two existing arguments keep their meaning — `base` is the pod layer
 * (network.json.models), `override` the per-bot layer (evolve-tiers.json).
 * The default catalog is prepended as the deepest base so EVERY existing call
 * site gains the defaults layer for free. The fold is two applications of the
 * pure two-layer kernel `mergeTwo`: mergeTwo(mergeTwo(defaults, pod), bot) —
 * defaults < pod < bot, override-wins-wholesale per key at each step.
 *
 * Pass `includeDefaults: false` to reproduce the pre-Addendum-2 pure two-layer
 * behavior (pod ← bot only) — used by the parity kernel tests.
 *
 * Keep in lockstep with `merge_model_catalog` in primary_bot.py, which folds
 * its own DEFAULT_MODEL_CATALOG the same way.
 */
/**
 * Normalize a `models`-block layer so a legacy `tiers.tierN` shape participates
 * in the keyed merge as `rungs`/`roles`.
 *
 * Without this, folding the code-default `rungs` as the base layer (spec
 * §Addendum 2) would silently SHADOW an un-migrated bot/pod whose config only
 * speaks the legacy `tiers` shape: the merged catalog carries the defaults'
 * rungs, `synthesizeRungsRoles` keys off `rungs` first, and the legacy `tiers`
 * never resolves — violating the spec's "existing config wins wherever it
 * speaks". Synthesizing the legacy tiers into rungs/roles up front lets them
 * override the defaults by id/key like any other layer.
 *
 * A layer that already carries `rungs` is returned unchanged. A null/empty
 * layer is returned as-is (the defaults still fold in beneath it).
 */
function _normalizeLegacyLayer(layer) {
    if (!layer || typeof layer !== "object")
        return layer;
    if (Array.isArray(layer.rungs) && layer.rungs.length > 0)
        return layer;
    if (!layer.tiers || typeof layer.tiers !== "object")
        return layer;
    const synth = synthesizeRungsRoles(layer);
    if (!synth.rungs.length)
        return layer;
    // Preserve the layer's other keys (routing, roleCaps, userTierOverride, …);
    // replace the legacy tiers with the synthesized rungs/roles so it overrides.
    const { tiers: _legacyTiers, ...rest } = layer;
    return {
        ...rest,
        rungs: synth.rungs,
        roles: { ...(synth.roles ?? {}), ...(layer.roles ?? {}) },
    };
}
export function mergeModelCatalog(base, override, opts) {
    const normBase = _normalizeLegacyLayer(base);
    const normOverride = _normalizeLegacyLayer(override);
    if (opts && opts.includeDefaults === false) {
        return mergeTwo(normBase, normOverride);
    }
    const withPod = mergeTwo(defaultModelCatalog(), normBase);
    return mergeTwo(withPod, normOverride);
}
/**
 * True iff `override` EXPLICITLY speaks for `tier` but yields no usable model.
 *
 * The companion to the empty-rung-is-a-no-op merge rule (`mergeOneRung`). The
 * merge intentionally lets the code-default / pod base fill a tier whose
 * per-bot override is empty, so the bot still resolves and works. But an
 * operator who hand-wrote `tier2: {models: []}` (or all-whitespace) authored
 * BROKEN config, not absent config — the Evolve admin onboarding /
 * setup-checklist surfaces flag it so they fix it, even though the runtime
 * falls back gracefully.
 *
 * Inspects the RAW per-bot override (never the merged catalog): the merge has
 * already hidden the breakage by design. "Explicitly speaks" = the override
 * carries an entry keyed to this tier's role/rung, via either shape:
 *   - legacy: `tiers.<tierN>` present (an object) but its `models` are nothing
 *     usable.
 *   - new shape: a `rungs` entry whose id is the role's rung (e.g.
 *     `sonnet-class` for tier2->standard), present but with no usable models.
 * Returns false when the override simply OMITS the tier (absent → defaulted,
 * which is configured per spec §Addendum 2).
 *
 * Mirrors `tier_override_is_broken` in primary_bot.py — keep the two in sync.
 */
export function tierOverrideIsBroken(override, tier) {
    if (!override || typeof override !== "object")
        return false;
    const role = _LEGACY_TIER_TO_ROLE[tier];
    // ── new shape: an explicit rung for this tier's rung id ──────────────────
    if (Array.isArray(override.rungs) && override.rungs.length > 0) {
        let rungId;
        const roles = override.roles && typeof override.roles === "object" ? override.roles : {};
        const entry = role ? roles[role] : undefined;
        if (typeof entry === "string" && entry)
            rungId = entry;
        else if (entry && typeof entry === "object" && typeof entry.rung === "string")
            rungId = entry.rung;
        else if (role)
            rungId = _LEGACY_TIER_TO_RUNG[tier];
        if (rungId) {
            const r = override.rungs.find((x) => x && typeof x === "object" && x.id === rungId);
            if (r)
                return usableModels(r).length === 0;
        }
    }
    // ── legacy shape: an explicit `tiers.<tierN>` entry ──────────────────────
    if (override.tiers && typeof override.tiers === "object" && tier in override.tiers) {
        const cfg = override.tiers[tier];
        if (cfg && typeof cfg === "object") {
            const models = Array.isArray(cfg.models)
                ? cfg.models.filter((m) => typeof m === "string" && m.trim() !== "")
                : [];
            return models.length === 0;
        }
    }
    return false;
}
/**
 * Normalize a routing block to the role-shaped field names, tolerating
 * the legacy `*Tier` keys. Maps a legacy `tierN` value to its role via
 * _LEGACY_TIER_TO_ROLE. Never throws.
 */
export function normalizeRouting(raw) {
    return _normalizeRouting(raw);
}
function _normalizeRouting(raw) {
    const r = raw && typeof raw === "object" ? raw : {};
    const toRole = (v) => {
        if (v === null)
            return null;
        if (typeof v !== "string")
            return undefined;
        return _LEGACY_TIER_TO_ROLE[v] ?? v; // map tierN, else pass role through
    };
    return {
        enabled: r.enabled,
        confidenceThreshold: r.confidenceThreshold,
        maintenanceRole: toRole(r.maintenanceRole ?? r.maintenanceTier) ?? undefined,
        backgroundRole: toRole(r.backgroundRole ?? r.backgroundTier) ?? undefined,
        // ambiguousRole/Tier may legitimately be null (use bot default).
        ambiguousRole: "ambiguousRole" in r ? toRole(r.ambiguousRole)
            : "ambiguousTier" in r ? toRole(r.ambiguousTier)
                : null,
    };
}
/**
 * Read the per-user-per-bot tier preferences file (audit #69 Phase C).
 *
 * Path: ``{sharedDir}/{botId}/user-tier-prefs.json``
 * Shape: ``{users: {<user_key>: {defaultTier?: string, ...}}}``
 *
 * Writes happen on the admin side (evolve user) via
 * ``evo tier-default X``; the plugin only reads. Returns ``{users: {}}``
 * when the file doesn't exist yet or is malformed — defaults to "no
 * per-user prefs", which falls through to the Phase A operator
 * default in ``_resolveOperatorDefaultTier``.
 */
function loadUserTierPrefsFile(sharedDir, botId) {
    if (!sharedDir || !botId)
        return { users: {} };
    try {
        const prefsPath = path.join(sharedDir, botId, "user-tier-prefs.json");
        const data = JSON.parse(fs.readFileSync(prefsPath, "utf8"));
        if (data && typeof data === "object" &&
            data.users && typeof data.users === "object") {
            return { users: data.users };
        }
    }
    catch {
        /* file missing or unreadable — fall through to empty */
    }
    return { users: {} };
}
/**
 * Machine-readable per-turn tier directive embedded in the message
 * envelope by the admin proxy's session-context block.
 *
 * Why a message-borne directive AND the EVOLVE_TIER_PREFERENCE env var:
 * the env var only reaches the process that the proxy spawns. For
 * spawn-per-turn surfaces (a member bot whose turn OC runs in a fresh
 * subprocess) that process IS where the plugin's before_model_resolve
 * runs, so the env var is visible. But evo's admin home chat routes
 * through a LONG-RUNNING gateway: the proxy spawns a thin `openclaw
 * agent` CLI client that forwards the turn over JSON-RPC, and the model
 * turn (hence this plugin hook) executes inside the pre-existing gateway
 * daemon — a different process whose environment never carries the
 * per-turn pick. The env var is therefore silently empty for every
 * home-chat turn, so the operator's "Max" chip never reached
 * setUserTier and the classifier won (the home-chat Max routing bug).
 *
 * The directive travels in `--message`, which the gateway always
 * receives, closing that seam.
 *
 * ── SECURITY: the directive is a privilege/cost gate ─────────────────
 * `tier=max` pins the premium Fable rung, bypassing the operator-only
 * chip, the `allowBotInitiated.max=false` default, and the per-day max
 * cap. So the directive must be UNFORGEABLE by any untrusted text. The
 * full prompt (`event.prompt`) is `<session-context>…</session-context>`
 * (server-emitted by the proxy) followed by the RAW USER BODY (a chat
 * message, a quoted email, a fetched doc, member-bot inbound). A naive
 * unanchored first-match over the whole prompt let any body containing
 * the literal token self-escalate. Two hardening layers, both required:
 *
 *   1. TRUSTED-BLOCK ANCHORING. The directive is honored ONLY when it
 *      appears inside the FIRST `<session-context>…</session-context>`
 *      span. The proxy always prepends that block (it always renders at
 *      least the "Operator:" line, so it is never empty) BEFORE the user
 *      body — see proxy.format_session_context / send_to_evo (sc_block +
 *      "\n\n" + msg). The attacker controls only the body, which is
 *      strictly AFTER the first `</session-context>`, so a token (or even
 *      a forged second `<session-context>` wrapper) in the body is never
 *      the first block and is ignored.
 *
 *   2. SURFACE GATE (`trustMessageDirective`). Only the admin/home-chat
 *      gateway surface legitimately receives a server-emitted directive.
 *      Member bots get NO session-context block from the proxy (their
 *      per-turn surface uses session_surface systemAppend, never a
 *      session-context envelope) and route via the env var instead. So
 *      on member-bot surfaces we honor NO message-borne directive at all
 *      — this closes the smuggling hole where untrusted inbound text on a
 *      member bot is itself the FIRST thing in the prompt and could
 *      otherwise masquerade as a trusted first block.
 *
 *   3. NONCE TOKEN (defense in depth). The proxy emits the directive with
 *      a per-turn random nonce the user cannot predict (it is never
 *      echoed into user-visible context):
 *        `[evolve-routing nonce=<rand>] tier=<choice>`
 *      A bare `[evolve-routing] tier=max` with no well-formed nonce is
 *      NOT honored. This rejects copy-pasted legacy tokens and any
 *      attacker string that lacks the structural nonce shape.
 *
 * Parsed case-insensitively; only the four canonical choices are
 * accepted (anything else → null, treated as "no directive").
 */
const _SESSION_CONTEXT_RE = /<session-context>([\s\S]*?)<\/session-context>/i;
// Requires a well-formed nonce token (>=8 url-safe chars) between the
// marker name and the closing bracket. The bare legacy form (no nonce)
// is intentionally NOT matched.
const _TIER_DIRECTIVE_RE = /\[evolve-routing\s+nonce=[A-Za-z0-9_-]{8,}\]\s+tier=([a-z]+)/i;
export function parseTierDirective(message, opts) {
    if (!message)
        return null;
    // SURFACE GATE: only the admin/home-chat gateway surface honors a
    // message-borne directive. Fail-closed default.
    if (!opts?.trustMessageDirective)
        return null;
    // TRUSTED-BLOCK ANCHORING: extract the FIRST session-context span only.
    // The proxy always prepends this block before the user body, so the
    // first span is the trusted one; any directive in the user body (which
    // is strictly after the first </session-context>) is ignored.
    const blk = _SESSION_CONTEXT_RE.exec(message);
    if (!blk)
        return null;
    const trustedRegion = blk[1];
    // NONCE-GATED match within the trusted region only.
    const m = _TIER_DIRECTIVE_RE.exec(trustedRegion);
    if (!m)
        return null;
    const v = m[1].toLowerCase();
    if (v === "fast" || v === "standard" || v === "power" || v === "max") {
        return v;
    }
    return null;
}
/**
 * Operational preference rank for reverse model->role lookup tie-breaks
 * (lower = more preferred when multiple roles' rungs tie on specificity +
 * length).
 *
 *   standard  → 0
 *   power     → 1
 *   max       → 2
 *   fast      → 3
 *   judge     → 4
 *   (anything else) → 99 (lose to all)
 *
 * See getRoleForModel's tie-break docstring for why this order: prefer
 * the most-operationally-likely role when a model could be attributed to
 * several. `judge` (provider-diversity selection) loses to all real
 * routing roles, as the old tier0 did.
 */
function _rolePreferenceRank(role) {
    switch (role) {
        case "standard": return 0;
        case "power": return 1;
        case "max": return 2;
        case "fast": return 3;
        case "judge": return 4;
        default: return 99;
    }
}
export class ModelRouter {
    config;
    sessionTypes; // sessionKey → 'productive'|'maintenance'|'ambiguous'|'background'
    sessionUserTiers; // sessionKey → operator's per-turn pick
    // Per-session consent_source (spec § 4.1). Tracks WHO/WHAT caused the
    // current sessionUserTiers entry. Used by cascade controller's
    // de-escalation logic (spec § 2.2): `ui_chip` is sticky-no-deescalate;
    // `ask_hint_agreed` allows auto-deescalation; `bot_initiated` is
    // sticky too. Set in setUserTier alongside the choice.
    sessionConsentSources;
    // Per-session user_key for per-user-per-bot tier prefs (audit #69
    // Phase C). TurnObserver computes ``ext:<channel>:<sender_external_id>``
    // on every user turn (when both are present) and pins it via
    // setSessionUserKey. _resolveOperatorDefaultTier looks up the user's
    // pref in userTierPrefs.users[<user_key>] BEFORE falling back to the
    // operator's bot-wide userTierOverride.defaultTier.
    //
    // Absent when the surface doesn't expose a user identity (heartbeat
    // sessions, internal subagents, admin-driven flows without channel
    // context). In those cases _resolveOperatorDefaultTier skips the
    // per-user lookup and routes per the operator default — the same
    // shape pre-Phase-C used.
    sessionUserKeys;
    sharedDir;
    botId;
    // Runaway-rate hard cap (spec § 2.6 cost management — the single
    // minimal per-session safety net retained after the cap framing was
    // dropped). Tracks per-session (timestamp, cost) tuples in a rolling
    // window; if their sum exceeds `dollars_per_window` within
    // `window_minutes`, the session is "tripped" — force tier3 for any
    // subsequent turn regardless of consent source.
    //
    // Different from `daily_cap_usd` (per-bot, per-day, daily emergency)
    // and from the monthly budget (per-bot, steady-state observation).
    // This catches genuine runaway loops where a single session burns
    // dollars in minutes — a tripwire for *broken behavior*, not a
    // cost-control mechanism for intentional work.
    sessionCostHistory;
    sessionRunawayTripped; // sessionKey → trip count this session
    // Pod-wide count of trips today (rough — in-memory, resets on plugin restart).
    // Used to escalate the signal severity from WARNING to CRITICAL after 3+ trips/24h.
    _runawayTripsToday = { count: 0, dayIso: "" };
    // In-process tier1 session tracker for the pressure_watchdog's
    // telemetry-coupled-failure defense (spec § pressure watchdog). The
    // watchdog reads {sharedDir}/{botId}/cascade/tier1_active.json on
    // every 60s poll and merges the count with span-derived counts via
    // max(spans, in_process). When cascade telemetry fails to write
    // spans (disk full, JsonlBackend bug, OC version drift), these
    // in-process counters keep working — that's the whole point of
    // *having* an in-process counter rather than just deriving from
    // spans.
    //
    // We track sessions whose MOST RECENT resolution was tier1. A
    // session that bounced through tier1 once but is now on tier2 does
    // NOT count — the watchdog cares about current pressure, not
    // historical exposure. resolveModelOverride is the single point
    // that updates the set; clearSession removes on session end.
    _tier1ActiveSessions = new Set();
    _tier1ActiveFileInitialized = false;
    // Per-bot tier1 turn count for the day, reset at pod-local midnight.
    // Used by canEscalateToTier1() to enforce the operator's
    // `userTierOverride.dailyCap` on bot-initiated session_set_tier
    // calls. Incremented in _markSessionTier when a turn resolves to
    // tier1 (any driver: user_request via chip, user_request via bot
    // tool, cascade, etc.). Both paths now converge on the disk-backed
    // counter (get_tier_usage_today): _markSessionTier mirrors each
    // transition to the JSONL the chip gate reads, and the constructor
    // seeds this in-memory count from that file via
    // _seedRoleCountersFromDisk so a plugin restart no longer zeroes the
    // cap. This in-memory copy is the fast path for the bot-tool gate
    // within a single plugin lifecycle.
    _tier1CallsToday = { count: 0, dayIso: "" };
    // Per-bot daily turn count for the `max` role, reset at pod-local
    // midnight — the analog of _tier1CallsToday for power. Bumped in
    // _markSessionTier on a transition into the max role; read by
    // canEscalateToRole("max") against roleCaps.max.maxPerDayPerBot.
    _maxCallsToday = { count: 0, dayIso: "" };
    // Phase 3 cascade-routing state (spec § 2.2). When `config.cascade.enabled`
    // is true, the cascade controller's most-recent verdict drives routing
    // for the NEXT turn. TurnObserver computes the verdict at the END of
    // turn N (in agent_end) and stashes it here; resolveModelOverride at
    // the START of turn N+1 reads it.
    //
    // Empty until the first turn's verdict arrives — until then routing
    // falls through to classifier (intentional: the controller can't
    // decide without struggle data, so the first turn always uses the
    // classifier's choice).
    sessionCascadeVerdicts = new Map();
    // Sticky per-session driver of the LAST resolveModelOverride call.
    // TurnObserver reads this to set `tier_chosen_by` on the cascade span
    // — when cascade actually drove routing this turn, the audit layer
    // must see chosen_by="cascade" (not "classifier") for Phase 4
    // calibration attribution.
    sessionLastDecisionDriver = new Map();
    // Per-session pre-flight intent router decision (Phase 1 of
    // spec-preflight-intent-router-2026-06-06.md). Populated by TurnObserver
    // from `before_model_resolve` BEFORE the LLM call; consumed by
    // _resolveModelAndTier in the productive/ambiguous branch (between
    // operator/user defaults and bot default). Cleared on session end.
    //
    // Phase 1 ships with the router returning ABSTAIN universally — the map
    // exists but stays empty. Phase 2/3+ have the router producing real
    // tier hints; the slot is in place so the rollout is a router-side
    // change, not a routing-ladder change.
    sessionPreflightDecisions = new Map();
    // Cache for auth-profiles.json — re-read at most once per minute
    _authProfilesCache = null;
    static _AUTH_PROFILES_TTL = 60_000;
    // Test seam: when set, _credentialedProviders returns this set instead of
    // reading auth-profiles.json. Production never sets it (the disk read is the
    // single source). Used by the availability parity fixture, which mirrors the
    // Python side that takes the credentialed set as a parameter.
    _credentialedProvidersOverride = null;
    /** @internal test-only — inject the credentialed-provider set. */
    _setCredentialedProvidersForTest(providers) {
        this._credentialedProvidersOverride =
            providers == null ? null : new Set([...providers].map((p) => p.toLowerCase()));
    }
    /**
     * Normalize a config passed to the constructor. Accepts both the new
     * {rungs, roles} shape and the legacy {tiers:{tierN}} shape (the latter
     * synthesized via synthesizeRungsRoles). Routing keys are normalized to
     * the *Role form (legacy *Tier tolerated). A legacy
     * userTierOverride.dailyCap is folded into roleCaps.power when no
     * explicit roleCaps is present. This makes both production loaders and
     * direct test construction tolerate either shape.
     */
    static _normalizeConfig(config) {
        const c = (config ?? {});
        const hasNewModels = Array.isArray(c.rungs) && c.rungs.length > 0;
        const hasLegacyModels = c.tiers && typeof c.tiers === "object";
        let rungs = c.rungs;
        let roles = c.roles;
        let roleCaps = c.roleCaps;
        if (!hasNewModels && hasLegacyModels) {
            const synth = synthesizeRungsRoles(c);
            rungs = synth.rungs;
            roles = synth.roles;
            roleCaps = roleCaps ?? synth.roleCaps;
        }
        // Fold legacy power cap into roleCaps.power.
        if (!roleCaps && typeof c.userTierOverride?.dailyCap === "number") {
            roleCaps = { power: { maxPerDayPerBot: c.userTierOverride.dailyCap } };
        }
        return {
            ...c,
            rungs: Array.isArray(rungs) ? rungs : [],
            roles: roles ?? {},
            roleCaps,
            routing: _normalizeRouting(c.routing ?? { enabled: true }),
        };
    }
    constructor(config, sharedDir = "", botId = "") {
        this.config = ModelRouter._normalizeConfig(config);
        this.sessionTypes = new Map();
        this.sessionUserTiers = new Map();
        this.sessionConsentSources = new Map();
        this.sessionUserKeys = new Map();
        this.sessionCostHistory = new Map();
        this.sessionRunawayTripped = new Map();
        this.sharedDir = sharedDir;
        this.botId = botId;
        // Wipe any stale tier1_active.json from a crashed prior plugin
        // process. Earlier this was deferred to reloadConfig's first call —
        // but production never calls reloadConfig (TurnObserver builds the
        // router via direct config load), so the stale-file defense was
        // dead code. Doing it here works because sharedDir + botId are
        // already set, and the call is a no-op when either is empty.
        this._clearTier1ActiveFileOnce();
        // Seed the per-day power/max counters from today's on-disk tier-usage
        // JSONL so a plugin restart doesn't reset the admin-UI daily cap to
        // zero (the disk-counter convergence — see _seedRoleCountersFromDisk
        // and the §595-599 "converge both paths" follow-up this resolves).
        this._seedRoleCountersFromDisk();
        // L5 audit follow-up: if a safety net (runaway-rate or spend-cap)
        // could ever fire on this bot but the `fast` role has no models
        // configured, a breaker firing will refuse the turn (returning an
        // unresolvable sentinel). That's correct (cost = $0 vs lying
        // telemetry), but it surprises operators — the warning here gives
        // them the heads-up at boot so they can configure `fast` before any
        // turn gets refused.
        this._warnIfSafetyNetWithoutFastRole();
        // Boot-time config advisory (spec §judge provider-diversity): note when
        // the judge role can only resolve to standard's own provider. Diversity is
        // a PREFERENCE (2026-06-19) — judge still routes same-vendor, so this is an
        // advisory nudge to add a cross-vendor model, not an error.
        this._warnIfJudgeSameVendor();
    }
    // ── Role resolution ──────────────────────────────────────────────────────
    /** Lookup a rung by slug; null when absent. */
    _rung(slug) {
        if (!slug)
            return null;
        const rungs = Array.isArray(this.config.rungs) ? this.config.rungs : [];
        return rungs.find((r) => r && r.id === slug) ?? null;
    }
    /** Provider prefix of a model string ("anthropic/claude-..." -> "anthropic"). */
    _providerOf(model) {
        if (!model || typeof model !== "string")
            return null;
        const slash = model.indexOf("/");
        return slash > 0 ? model.slice(0, slash).toLowerCase() : null;
    }
    /**
     * Resolve a role ID to a concrete model string (or null when the role,
     * its rung, or the rung's models are unconfigured). The single
     * translation point between the role namespace code/users speak and
     * the model string OC consumes.
     *
     *   fast/standard/power/max → roles[role] is a rung slug → rung.models[0]
     *   judge → structured {rung, provider:"not-standard"}: prefers the first
     *           rung model whose provider differs from standard's, but falls
     *           back to the first model (same vendor) so judge still routes —
     *           diversity is a preference, not a hard constraint.
     */
    resolveRoleToModel(role) {
        if (role === "judge")
            return this._resolveJudgeModel();
        const slug = (this.config.roles ?? {})[role];
        if (typeof slug !== "string")
            return null;
        const rung = this._rung(slug);
        return rung?.models?.[0] ?? null;
    }
    /**
     * Resolve the judge model honoring provider diversity vs standard's
     * resolved provider as a PREFERENCE, not a hard constraint (2026-06-19).
     * Prefers the first rung model whose provider differs from standard's; if
     * none exists, falls back to the first rung model (same vendor) so judge
     * STILL ROUTES — diversity is a recommendation. Null only when the rung is
     * empty. Mirrors primary_bot._resolve_judge_availability (None-creds path).
     */
    _resolveJudgeModel() {
        const judge = this.config.roles?.judge;
        if (!judge)
            return null;
        const slug = typeof judge === "string" ? judge : judge.rung;
        const rung = this._rung(slug);
        const models = rung?.models ?? [];
        if (models.length === 0)
            return null;
        // Plain-string judge (no provider constraint) → first model.
        if (typeof judge === "string" || judge.provider !== "not-standard") {
            return models[0] ?? null;
        }
        const standardModel = this.resolveRoleToModel("standard");
        const standardProvider = this._providerOf(standardModel);
        // Pass 1 — prefer a cross-vendor model.
        for (const m of models) {
            if (this._providerOf(m) !== standardProvider)
                return m;
        }
        // Pass 2 — soft fallback: same vendor as standard still routes.
        return models[0] ?? null;
    }
    // ── Availability-aware resolution (spec §Addendum3.A) ──────────────────
    //
    // A role resolves to the FIRST model in its rung whose provider is
    // credentialed AND LLM-capable. The credentialed set is read from
    // auth-profiles (presence of a usable key, BY the profile's provider field,
    // not by a provider-name literal). The LLM-capable set is DERIVED from the
    // catalog's rung clusters (three-homes rule §Addendum3.B). When a rung has
    // no available provider the role degrades DOWN the ladder through the SAME
    // chain + reason machinery as a cap hit. Reasons unify:
    //   cap_exhausted | uncredentialed | unconfigured.
    // Mirrors primary_bot.py resolve_role_with_availability — keep in sync.
    /** Providers naming ≥1 model in any rung cluster (the LLM-capable set). */
    _llmProvidersFromCatalog() {
        const out = new Set();
        const rungs = Array.isArray(this.config.rungs) ? this.config.rungs : [];
        for (const r of rungs) {
            const models = Array.isArray(r?.models) ? r.models : [];
            for (const m of models) {
                const p = this._providerOf(m);
                if (p)
                    out.add(p);
            }
        }
        return out;
    }
    /**
     * The set of providers that have a usable credential in auth-profiles,
     * keyed by each profile's own `provider` field (no provider-name literal).
     * A profile counts only when it carries a non-empty key/token/api_key.
     */
    _credentialedProviders() {
        if (this._credentialedProvidersOverride != null) {
            return this._credentialedProvidersOverride;
        }
        const out = new Set();
        const profiles = this._loadAuthProfiles();
        for (const [key, profile] of Object.entries(profiles)) {
            if (!profile || typeof profile !== "object")
                continue;
            let hasKey = false;
            for (const field of ["key", "token", "api_key"]) {
                const val = profile[field];
                if (typeof val === "string" && val.trim().length > 0) {
                    hasKey = true;
                    break;
                }
            }
            if (!hasKey)
                continue;
            // Provider name comes from the profile's own field, falling back to the
            // key prefix ("anthropic:api" / "brave_api_key") — same shape as the
            // admin reader. This is string parsing of DATA, not a literal in logic.
            let provider = profile.provider;
            if (typeof provider === "string" && provider.trim()) {
                provider = provider.toLowerCase();
            }
            else if (key.includes(":")) {
                provider = key.split(":", 1)[0].toLowerCase();
            }
            else if (key.includes("_")) {
                provider = key.split("_", 1)[0].toLowerCase();
            }
            else {
                provider = key.toLowerCase();
            }
            if (provider)
                out.add(provider);
        }
        return out;
    }
    /**
     * Providers role resolution may pick from: credentialed ∩ llm-capable.
     * Intersecting against the catalog-derived LLM set drops non-LLM
     * credentials (brave, runway, …) without naming any provider.
     */
    availableProviders() {
        const credentialed = this._credentialedProviders();
        const llm = this._llmProvidersFromCatalog();
        const out = new Set();
        for (const p of credentialed)
            if (llm.has(p))
                out.add(p);
        return out;
    }
    /**
     * Resolve a role to a concrete model, degrading down the ladder when no
     * provider in its rung is available, tagging the outcome with a unified
     * reason. judge resolves via its own diversity machinery and never
     * degrades through this ladder.
     */
    resolveRoleAvailability(role) {
        const avail = this.availableProviders();
        if (role === "judge") {
            // Diversity is a PREFERENCE: prefer cross-vendor, fall back to same-vendor
            // (still routes, flagged `same_vendor_as_standard`), hard-break only when
            // no credentialed model exists. Mirrors primary_bot._resolve_judge_availability.
            const { model, sameVendor } = this._resolveJudgeModelAvailable(avail);
            const rung = this._rung(typeof this.config.roles?.judge === "string"
                ? this.config.roles.judge
                : this.config.roles?.judge?.rung);
            const providers = this._rungProviders(rung);
            let reason;
            if (model)
                reason = sameVendor ? "same_vendor_as_standard" : null;
            else
                reason = providers.length ? "uncredentialed" : "unconfigured";
            return {
                requestedRole: "judge",
                resolvedRole: model ? "judge" : null,
                model,
                degraded: false,
                reason,
                providers,
            };
        }
        const seen = new Set();
        let cur = role;
        while (cur && !seen.has(cur)) {
            seen.add(cur);
            const slug = (this.config.roles ?? {})[cur];
            const rung = typeof slug === "string" ? this._rung(slug) : null;
            const models = Array.isArray(rung?.models) ? rung.models : [];
            const providers = this._rungProviders(rung);
            if (models.length === 0) {
                const nxt = this._degradeRole(cur);
                if (nxt === null) {
                    return { requestedRole: role, resolvedRole: null, model: null,
                        degraded: cur !== role, reason: "unconfigured", providers };
                }
                cur = nxt;
                continue;
            }
            for (const m of models) {
                const p = this._providerOf(m);
                if (p && avail.has(p)) {
                    return { requestedRole: role, resolvedRole: cur, model: m,
                        degraded: cur !== role,
                        reason: cur !== role ? "uncredentialed" : null, providers };
                }
            }
            const nxt = this._degradeRole(cur);
            if (nxt === null) {
                return { requestedRole: role, resolvedRole: null, model: null,
                    degraded: cur !== role, reason: "uncredentialed", providers };
            }
            cur = nxt;
        }
        return { requestedRole: role, resolvedRole: null, model: null,
            degraded: true, reason: "uncredentialed", providers: [] };
    }
    /** Sorted provider set of a rung's models. */
    _rungProviders(rung) {
        const set = new Set();
        for (const m of (rung?.models ?? [])) {
            const p = this._providerOf(m);
            if (p)
                set.add(p);
        }
        return Array.from(set).sort();
    }
    /**
     * Downward degradation step shared by cap-exhaustion and availability:
     * max→power→standard→fast, fast/judge terminal (null). The cap path's
     * degradeRoleOnCap terminates at standard; this one continues to fast so
     * an uncredentialed standard can still reach a cheaper credentialed rung.
     */
    _degradeRole(role) {
        switch (role) {
            case "max": return "power";
            case "power": return "standard";
            case "standard": return "fast";
            default: return null; // fast, judge, unknown
        }
    }
    /**
     * Judge model honoring diversity as a PREFERENCE plus availability.
     * Returns `{model, sameVendor}`:
     *   - Pass 1 — an available provider OTHER than standard's → `sameVendor:false`.
     *   - Pass 2 — an available provider even if it equals standard's → routes
     *     with `sameVendor:true` (the soft `same_vendor_as_standard` advisory).
     *   - none available → `{model:null, sameVendor:false}` (genuine hard break).
     * Mirrors primary_bot._resolve_judge_availability's two-pass ladder.
     */
    _resolveJudgeModelAvailable(avail) {
        const judge = this.config.roles?.judge;
        if (!judge)
            return { model: null, sameVendor: false };
        const slug = typeof judge === "string" ? judge : judge.rung;
        const rung = this._rung(slug);
        const models = rung?.models ?? [];
        if (models.length === 0)
            return { model: null, sameVendor: false };
        const constrained = typeof judge !== "string" && judge.provider === "not-standard";
        const standardProvider = constrained
            ? this._providerOf(this.resolveRoleAvailability("standard").model)
            : null;
        // Pass 1 — ideal cross-vendor (only when constrained; an unconstrained
        // judge has no vendor preference and takes the first available model).
        if (constrained) {
            for (const m of models) {
                const p = this._providerOf(m);
                if (p && p !== standardProvider && avail.has(p)) {
                    return { model: m, sameVendor: false };
                }
            }
        }
        // Pass 2 — soft fallback: first available model (same vendor for a
        // constrained judge; the only pass for an unconstrained one).
        for (const m of models) {
            const p = this._providerOf(m);
            if (p && avail.has(p)) {
                return { model: m, sameVendor: constrained && p === standardProvider };
            }
        }
        return { model: null, sameVendor: false };
    }
    /**
     * Map a user/role choice to the cascade controller's internal Tier
     * symbol (tier0-tier3), which the controller's state machine still
     * speaks. `max` has NO cascade Tier — it is pull-only and the cascade
     * can never produce it (spec §max semantics #2). Returns null for
     * `max` and any unknown role so callers drop it from cascade inputs.
     */
    _roleToCascadeTier(role) {
        switch (role) {
            case "fast": return "tier3";
            case "standard": return "tier2";
            case "power": return "tier1";
            case "judge": return "tier0";
            // `max` deliberately unmapped — pull-only, excluded from cascade.
            default: return null;
        }
    }
    /**
     * Build the effective roleCaps, preferring the explicit new-shape
     * block (tiersFile then network.models) and folding a legacy
     * `userTierOverride.dailyCap` into roleCaps.power when no explicit
     * power cap is present. Used only by reloadConfig.
     */
    _mergeRoleCaps(fromTiersFile, fromNetwork, legacyOverride) {
        const explicit = fromTiersFile ?? fromNetwork;
        if (explicit)
            return explicit;
        const legacyCap = legacyOverride?.dailyCap;
        if (typeof legacyCap === "number") {
            return { power: { maxPerDayPerBot: legacyCap } };
        }
        return undefined;
    }
    /** Inverse of _roleToCascadeTier for reading cascade verdicts back. */
    _cascadeTierToRole(tier) {
        switch (tier) {
            case "tier3": return "fast";
            case "tier2": return "standard";
            case "tier1": return "power";
            case "tier0": return "judge";
            default: return null;
        }
    }
    /**
     * Return the model the safety-net branches should downgrade to when
     * they fire. Prefers tier3's configured model; when tier3 is
     * unconfigured or empty, returns ``_SAFETY_NET_REFUSE_SENTINEL`` —
     * an unresolvable model id that causes OC to fail the turn entirely.
     *
     * Refusing the turn is the correct behavior when the breaker fires
     * with no configured downgrade target. The previous chain:
     *   Pre-#1767:   returned null → OC used bot default → lying telemetry
     *                (breaker reported as fired; actually no-op cost-wise)
     *   #1767:       returned hardcoded haiku → cost capped but violated
     *                "no hardcoded models in code" principle
     *   This change: returns sentinel → OC fails → bot stops spending →
     *                loud operator-visible signal in gateway.log
     *
     * Telemetry honesty: ``sessionLastDecisionDriver`` is still stamped
     * ``runaway`` / ``spend_cap`` so audits can attribute the refusal to
     * the breaker that triggered it. The model field carries the sentinel
     * so audits can distinguish "breaker fired successfully (tier3
     * downgrade)" from "breaker fired but turn refused (tier3 empty)".
     *
     * Logs a warning the first time a refusal happens so operators see
     * the cause in the gateway log. Repeated refusals in the same
     * process are silent (avoid spamming during a runaway session).
     */
    _safetyNetRefusalWarned = false;
    _safetyNetDowngradeModel(driver) {
        const fastModel = this.resolveRoleToModel("fast");
        if (fastModel)
            return fastModel;
        if (!this._safetyNetRefusalWarned) {
            // Log once per process — repeated warnings would spam if every
            // turn of a runaway session re-fires the same refusal.
            try {
                // eslint-disable-next-line no-console
                console.warn(`[Evolve ModelRouter] WARN: ${driver} safety-net fired but the ` +
                    `'fast' role is unconfigured — refusing the turn (returning the ` +
                    `unresolvable sentinel '${_SAFETY_NET_REFUSE_SENTINEL}'). ` +
                    `Configure the 'fast' role in evolve-tiers.json so the breaker ` +
                    `has a defined downgrade target. Until then, the breaker ` +
                    `correctly stops cost (bot can't run) at the price of refusing ` +
                    `user / auto turns when the breaker condition holds.`);
            }
            catch { /* never let logging crash a hot-path hook */ }
            this._safetyNetRefusalWarned = true;
        }
        return _SAFETY_NET_REFUSE_SENTINEL;
    }
    /**
     * Startup-time validation: if a safety net is wired up (runawayRateCap
     * enabled, or sharedDir+botId set such that spend-cap can fire) but the
     * `fast` role has no models, warn the operator. The breaker will REFUSE
     * turns when it fires (via the unresolvable sentinel) instead of
     * silently routing to bot default. Operator gets the heads-up at
     * boot, before any turn has to be refused.
     */
    _warnIfSafetyNetWithoutFastRole() {
        const fastHasModels = !!this.resolveRoleToModel("fast");
        if (fastHasModels)
            return;
        const runawayWired = this.config.runawayRateCap?.enabled !== false; // default true
        const spendCapWired = !!(this.sharedDir && this.botId);
        if (!runawayWired && !spendCapWired)
            return;
        try {
            // eslint-disable-next-line no-console
            console.warn(`[Evolve ModelRouter] WARN at startup: the 'fast' role has no ` +
                `models, but safety nets are wired (runaway=${runawayWired}, ` +
                `spend_cap=${spendCapWired}). When the breaker fires it will ` +
                `REFUSE the turn (returning sentinel ` +
                `'${_SAFETY_NET_REFUSE_SENTINEL}'). Configure the 'fast' role in ` +
                `evolve-tiers.json so the breaker has an explicit downgrade ` +
                `target — otherwise breaker-triggered turns will error out ` +
                `until 'fast' is set.`);
        }
        catch { /* never let logging crash construction */ }
    }
    /**
     * Startup-time advisory (spec §judge provider-diversity): note when the judge
     * role prefers a provider different from `standard` but its rung contains ONLY
     * models from standard's provider. Diversity is a PREFERENCE (2026-06-19):
     * _resolveJudgeModel still returns standard's vendor so judge ROUTES — this is
     * a soft nudge to add a cross-vendor model for independent evaluation, not a
     * routing failure. (An empty rung / genuinely unroutable judge is handled by
     * the resolver's `unconfigured` / `uncredentialed` reasons, not here.)
     */
    _warnIfJudgeSameVendor() {
        const judge = this.config.roles?.judge;
        if (!judge || typeof judge === "string")
            return; // no preference to check
        if (judge.provider !== "not-standard")
            return;
        const rung = this._rung(judge.rung);
        const models = rung?.models ?? [];
        if (models.length === 0)
            return; // empty-rung warn handled elsewhere
        const standardProvider = this._providerOf(this.resolveRoleToModel("standard"));
        // Cross-vendor option present → diversity satisfiable, nothing to nudge.
        if (models.some((m) => this._providerOf(m) !== standardProvider))
            return;
        try {
            // eslint-disable-next-line no-console
            console.warn(`[Evolve ModelRouter] NOTE at startup: the 'judge' role prefers a ` +
                `provider different from 'standard' (${standardProvider ?? "unknown"}), ` +
                `but its rung ('${judge.rung}') contains only models from that provider. ` +
                `Judge will still route on ${standardProvider ?? "the same vendor"}; ` +
                `add a model from another provider to the '${judge.rung}' rung for ` +
                `independent cross-vendor evaluation.`);
        }
        catch { /* never let logging crash construction */ }
    }
    /**
     * Resolve the per-user OR operator default tier (audit #69 Phase A +
     * Phase C).
     *
     * Precedence within the "fall-back-to-bot-default" branch:
     *   1. Per-user pref (Phase C) — when sessionUserKeys has an entry
     *      for this session AND userTierPrefs.users contains an entry
     *      for that user_key with a non-"auto" defaultTier, that wins.
     *   2. Operator default (Phase A) — userTierOverride.defaultTier.
     *
     * Choice mapping for both sources:
     *   • "auto" / missing / unknown → fall through (next source, then
     *     OC bot default)
     *   • "fast"     → ``[tier3.models[0], "tier3"]``
     *   • "standard" → ``[tier2.models[0], "tier2"]``
     *   • "power"    → ``[tier1.models[0], "tier1"]``
     *
     * Called from _resolveModelAndTier's classifier branch (sessionType
     * unset / productive / ambiguous) BEFORE the bot-default fallback.
     * Slots BELOW the chip / session_set_tier user override (level 2)
     * and BELOW the cascade verdict (level 3). Both per-user and
     * operator defaults sit at level 4a; the per-user pref simply wins
     * the tie-break.
     *
     * Returns ``[null, null]`` when both sources resolve to no override
     * (auto / missing / unknown / empty tier — same conservative
     * behavior as the operator default in Phase A).
     *
     * Implementation note: the resolver scans the per-user side first.
     * Callers can't distinguish source from the return value alone —
     * the driver string ("user_default" vs "operator_default") is set
     * by the caller using ``getLastResolvedDefaultSource()``.
     */
    _resolveOperatorDefaultRole(sessionKey) {
        // Reset the resolved-source tracker; gets stamped to "user" or
        // "operator" iff we actually pick a role, otherwise stays null.
        this._lastResolvedDefaultSource = null;
        // Per-user pref (Phase C) — tries first. A per-user default MAY be
        // `max` (a user pinning Fable as their personal default is an
        // explicit pull, §max semantics #1), so allowMax=true here.
        if (sessionKey) {
            const userKey = this.sessionUserKeys.get(sessionKey);
            if (userKey) {
                const prefs = this.config.userTierPrefs?.users ?? {};
                const userPref = prefs[userKey];
                if (userPref) {
                    const choice = userPref.defaultRole ?? userPref.defaultTier;
                    const userResolved = this._resolveRoleFromChoice(choice, true);
                    if (userResolved[0] !== null) {
                        this._lastResolvedDefaultSource = "user";
                        return userResolved;
                    }
                }
            }
        }
        // Operator default (Phase A) — the fallback within this helper.
        // The operator bot-wide default is a CLASSIFIER role and must NOT be
        // `max` (pull-only, §max semantics #3): allowMax=false rejects it.
        const override = this.config.userTierOverride ?? {};
        const opChoice = override.defaultRole ?? override.defaultTier;
        const opResolved = this._resolveRoleFromChoice(opChoice, false);
        if (opResolved[0] !== null) {
            this._lastResolvedDefaultSource = "operator";
        }
        return opResolved;
    }
    // Source of the last _resolveOperatorDefaultRole hit; "user" for a
    // Phase-C per-user pref, "operator" for Phase A. Caller reads this
    // immediately after the helper to set the right driver tag
    // ("user_default" vs "operator_default") on
    // sessionLastDecisionDriver — done outside the helper because the
    // helper return shape (model, role) stays uniform across sources.
    _lastResolvedDefaultSource = null;
    /**
     * Resolve a role-choice string to [model, roleId]. "auto" / missing /
     * unknown → [null, null] (fall through). `max` resolves only when
     * allowMax is true — classifier / operator-default callers pass false
     * (pull-only). `judge` is never a default-routing choice.
     */
    _resolveRoleFromChoice(choice, allowMax) {
        const norm = (choice ?? "").trim().toLowerCase();
        if (!norm || norm === "auto")
            return [null, null];
        const VALID = allowMax
            ? new Set(["fast", "standard", "power", "max"])
            : _CLASSIFIER_ROLES;
        if (!VALID.has(norm))
            return [null, null];
        const model = this.resolveRoleToModel(norm);
        if (!model)
            return [null, null];
        return [model, norm];
    }
    /**
     * Pin a user identity onto a session (audit #69 Phase C). Called by
     * TurnObserver on every user turn with ``ext:<channel>:<sender_external_id>``
     * (when both pieces of identity are present). Subsequent routing
     * decisions for this session consult userTierPrefs[user_key] before
     * falling back to the operator default.
     *
     * Pass ``null`` / empty string to clear the binding — used when a
     * surface lacks identity context (heartbeat, anonymous flows). The
     * session then routes per the operator default only.
     *
     * No-op when sessionKey is empty (defense in depth — same posture as
     * setUserTier).
     */
    setSessionUserKey(sessionKey, userKey) {
        if (!sessionKey)
            return;
        if (userKey) {
            this.sessionUserKeys.set(sessionKey, userKey);
        }
        else {
            this.sessionUserKeys.delete(sessionKey);
        }
    }
    /**
     * Read the user_key pinned on a session, or null if none.
     * Test helper / diagnostic surface.
     */
    getSessionUserKey(sessionKey) {
        return this.sessionUserKeys.get(sessionKey) ?? null;
    }
    /**
     * Reverse-lookup: given a model string (e.g. "claude-sonnet-4-6" or
     * "anthropic/claude-sonnet-4-6"), return the ROLE ID ("fast" |
     * "standard" | "power" | "max" | "judge") whose rung this model
     * belongs to per the pod's config.
     *
     * Used by cascade telemetry (Phase 1 of spec-tier-cascade-2026-05-26)
     * to record what role was *actually* used by OC, not the role the
     * classifier intended. See spec § 6.3 and the failure-mode review
     * finding F8 — the calibration loop must read what was used from
     * reality, not intent.
     *
     * Returns null when the model isn't found in any role's rung (unknown
     * model, stale config, OC override of a model not in our catalog).
     * Callers should fall back to the intended role with an
     * "unknown" marker so audits can flag the case.
     *
     * Match logic: the model string in OC may include a provider prefix
     * ("anthropic/claude-sonnet-4-6"), a bare model name
     * ("claude-sonnet-4-6"), or a versioned alias. We try exact match
     * first, then suffix/prefix match (provider-prefix tolerance), then
     * substring match.
     *
     * Tie-break order when multiple roles tie on specificity + length:
     *   standard → power → max → fast → judge (see _rolePreferenceRank).
     *
     * Rationale: operators routinely list the same model in multiple rungs
     * (e.g. claude-sonnet-4-6 in the standard rung AND as a judge
     * fallback). Without an operational preference, iteration order could
     * pick judge — but judge is reserved for cross-provider validation,
     * NOT the typical operational role. Prefer the most-operationally-
     * likely role so spans attribute as the operator expects.
     */
    getRoleForModel(modelString) {
        if (!modelString || typeof modelString !== "string")
            return null;
        const lower = modelString.toLowerCase();
        // Iterate roles; each role's rung supplies the candidate models.
        // Longest exact/suffix candidate wins; ties break by the operational
        // role preference (see _rolePreferenceRank).
        const roleIds = ["fast", "standard", "power", "max", "judge"];
        let bestRole = null;
        let bestSpecificity = -1; // exact = 3, ends/starts = 2, substring = 1
        let bestLength = 0;
        for (const role of roleIds) {
            const roleDef = this.config.roles?.[role];
            if (!roleDef)
                continue;
            const slug = typeof roleDef === "string" ? roleDef : roleDef.rung;
            const rung = this._rung(slug);
            const models = Array.isArray(rung?.models) ? rung.models : [];
            for (const candidate of models) {
                if (typeof candidate !== "string" || !candidate)
                    continue;
                const c = candidate.toLowerCase();
                let specificity = 0;
                if (c === lower) {
                    specificity = 3;
                }
                else if (lower.endsWith(c) || c.endsWith(lower)) {
                    specificity = 2;
                }
                else if (c.length >= 6 && lower.includes(c)) {
                    specificity = 1;
                }
                else if (lower.length >= 6 && c.includes(lower)) {
                    specificity = 1;
                }
                if (specificity === 0)
                    continue;
                const better = specificity > bestSpecificity
                    || (specificity === bestSpecificity && c.length > bestLength)
                    || (specificity === bestSpecificity
                        && c.length === bestLength
                        && bestRole !== null
                        && _rolePreferenceRank(role) < _rolePreferenceRank(bestRole));
                if (better) {
                    bestRole = role;
                    bestSpecificity = specificity;
                    bestLength = c.length;
                }
            }
        }
        return bestRole;
    }
    /**
     * Legacy reverse-lookup alias: returns the old tier key ("tier0".."tier3")
     * for a model. Kept so any un-migrated external caller still resolves;
     * derived from getRoleForModel via the role->tier map. Prefer
     * getRoleForModel in new code.
     */
    getTierForModel(modelString) {
        const role = this.getRoleForModel(modelString);
        return role ? this._roleToCascadeTier(role) : null;
    }
    /**
     * Read auth-profiles.json from the bot's home directory (cached, 1-min TTL).
     * Returns the "profiles" object (profile_id → profile entry).
     * Never throws — returns {} on any error.
     */
    _loadAuthProfiles() {
        const now = Date.now();
        if (this._authProfilesCache && now - this._authProfilesCache.at < ModelRouter._AUTH_PROFILES_TTL) {
            return this._authProfilesCache.profiles;
        }
        try {
            const authPath = path.join(os.homedir(), ".openclaw", "agents", "main", "agent", "auth-profiles.json");
            const raw = JSON.parse(fs.readFileSync(authPath, "utf8"));
            const profiles = raw.profiles ?? {};
            this._authProfilesCache = { profiles, at: now };
            return profiles;
        }
        catch {
            // If we can't read the file, cache an empty result briefly (5s) to
            // avoid hammering the filesystem on every call during startup.
            this._authProfilesCache = { profiles: {}, at: now - ModelRouter._AUTH_PROFILES_TTL + 5_000 };
            return {};
        }
    }
    /**
     * Return true if the given auth profile ID has a non-empty key or token
     * in the bot's auth-profiles.json.
     *
     * Profile ID format matches the keys in auth-profiles.json
     * (e.g. "anthropic_token", "anthropic_api_key", or the full
     * "anthropic:user@example.com" format openclaw uses for Max accounts).
     */
    _profileHasKey(profileId) {
        const profiles = this._loadAuthProfiles();
        const profile = profiles[profileId];
        if (!profile || typeof profile !== "object")
            return false;
        // Key field may be named: key, token, api_key — check all
        for (const field of ["key", "token", "api_key"]) {
            const val = profile[field];
            if (typeof val === "string" && val.trim().length > 0)
                return true;
        }
        return false;
    }
    /**
     * Called by TurnObserver when session type is classified/updated.
     * Stores classification in memory for use by before_model_resolve.
     *
     * Most callers should prefer ``setSessionTypeIfMoreSpecific`` — this
     * raw setter unconditionally overwrites, which is correct for fresh
     * sessions or explicit reclassification, but wrong for the agent_end
     * classifier path (see the IfMoreSpecific docstring for the failure
     * mode this guards against).
     */
    setSessionType(sessionKey, sessionType) {
        this.sessionTypes.set(sessionKey, sessionType);
    }
    /**
     * Set the session class IFF it would not downgrade specificity.
     *
     * Specificity ranking (low → high):
     *   undefined < "ambiguous" < {"productive", "maintenance", "background"}
     *
     * Rules:
     *   • Existing undefined  → always write (no info loss)
     *   • Existing ambiguous  → write iff new is specific (upgrade)
     *   • Existing specific   → write iff new is also specific (lateral
     *     reclassification is allowed; downgrade to ambiguous is not)
     *
     * WHY THIS EXISTS (the L4 audit / PR #1737 follow-up):
     *   The agent_end keyword classifier returns ``ambiguous`` when given
     *   empty user/assistant text — which is exactly the shape of a
     *   heartbeat session ("", ""). On turn 1 the trigger anchor sets
     *   sessionType=background; turn 1's agent_end then USED to unconditionally
     *   overwrite it with ``ambiguous`` (raw setSessionType call). On turn 2,
     *   the trigger-anchor guard at TurnObserver.resolveModelRouting saw a
     *   truthy existing class and skipped, then resolveModelOverride read
     *   ``ambiguous`` and returned null (bot default = Sonnet). Net effect:
     *   every turn 2+ of every heartbeat session silently ran on primary
     *   instead of the configured tier3 floor — same user-visible symptom
     *   as the PR #1737 bug, recreated by the post-hoc classifier.
     */
    setSessionTypeIfMoreSpecific(sessionKey, newType) {
        if (!_IS_SPECIFIC_CLASS(newType)) {
            const existing = this.sessionTypes.get(sessionKey);
            if (_IS_SPECIFIC_CLASS(existing)) {
                // Existing is specific (productive/maintenance/background), new
                // is ambiguous or otherwise non-specific. Don't downgrade.
                return;
            }
        }
        this.sessionTypes.set(sessionKey, newType);
    }
    /**
     * Read the current session classification, or undefined when none is
     * cached yet. Used by the trigger-kind pre-classification path in
     * TurnObserver.resolveModelRouting to decide whether to anchor a
     * new session's class on its trigger before model selection runs —
     * a prior turn's classifier verdict (set via setSessionType from
     * agent_end) wins over the trigger anchor when present.
     */
    getSessionType(sessionKey) {
        return this.sessionTypes.get(sessionKey);
    }
    /**
     * Set the operator's tier preference for this session. Used by the
     * admin-UI chat composer: each turn carries a tier choice (Auto / Fast
     * / Standard / Power); the admin proxy forwards it via the
     * EVOLVE_TIER_PREFERENCE env var, and TurnObserver calls this on
     * every before_model_resolve fire so the override always reflects the
     * latest choice (not the first turn's).
     *
     * "auto" / null / unknown values clear the entry — the classifier
     * then drives routing as before.
     */
    setUserTier(sessionKey, choice, consentSource = "ui_chip") {
        if (!sessionKey)
            return;
        const v = (choice ?? "").trim().toLowerCase();
        if (v === "fast" || v === "standard" || v === "power" || v === "max") {
            this.sessionUserTiers.set(sessionKey, v);
            this.sessionConsentSources.set(sessionKey, consentSource);
        }
        else {
            // auto / empty / unknown → no override
            this.sessionUserTiers.delete(sessionKey);
            this.sessionConsentSources.delete(sessionKey);
        }
    }
    /**
     * Read the operator's per-role bot-initiated permission. The
     * `allowBotInitiated` config is now per-role
     * ({power, max}); a legacy boolean maps to {power: <value>, max: false}
     * (§max semantics #4 — max defaults to false even under a legacy
     * `allowBotInitiated: true`, because a bot may forward a user's
     * explicit ask but never unilaterally pin Fable). Default for an
     * absent value is power=true (legacy unrestricted), max=false.
     */
    _allowBotInitiated(role) {
        const raw = (this.config.userTierOverride ?? {}).allowBotInitiated;
        if (typeof raw === "boolean") {
            return role === "power" ? raw : false;
        }
        if (raw && typeof raw === "object") {
            if (role === "power")
                return raw.power !== false; // default true
            return raw.max === true; // default false
        }
        return role === "power"; // absent block: power true, max false
    }
    /** Per-role daily cap (turns/bot/day). power default 10, max default 5. */
    _roleCap(role) {
        const caps = this.config.roleCaps ?? {};
        const fromNew = caps[role]?.maxPerDayPerBot;
        if (typeof fromNew === "number")
            return fromNew;
        // Legacy fallback: userTierOverride.dailyCap was the power cap.
        if (role === "power") {
            const legacy = (this.config.userTierOverride ?? {}).dailyCap;
            if (typeof legacy === "number")
                return legacy;
            return 10;
        }
        return 5;
    }
    /** Per-role used-today counter, rolled at pod-local midnight. */
    _roleUsedToday(role) {
        const dayIso = localDateYMD();
        const slot = role === "power" ? this._tier1CallsToday : this._maxCallsToday;
        if (slot.dayIso !== dayIso) {
            slot.count = 0;
            slot.dayIso = dayIso;
        }
        return slot.count;
    }
    /**
     * Check whether escalating this session to `role` is allowed under the
     * operator's config + current daily usage, generalizing the old
     * `canEscalateToTier1` to per-role caps (spec §max semantics #6).
     *
     * Roles `fast`/`standard` have no cap and are always allowed. For
     * `power` and `max`:
     *   1. ``userTierOverride.enabled`` (default true) — global kill-switch
     *      for the operator-tier-control feature.
     *   2. per-role ``allowBotInitiated`` (power default true, max default
     *      false) — operator can forbid bot self-escalation per role.
     *   3. per-role daily cap (``roleCaps.<role>.maxPerDayPerBot``;
     *      power default 10, max default 5). On exhaustion the caller
     *      degrades down the chain (max→power→standard) via
     *      degradeRoleOnCap().
     *
     * Returns ``{allowed: true}`` when no gate trips.
     */
    canEscalateToRole(role) {
        if (role !== "power" && role !== "max") {
            // fast / standard / unknown → no cap gate.
            return { allowed: true };
        }
        const override = (this.config.userTierOverride ?? {});
        // Gate 1: global enable.
        if (override.enabled === false) {
            return {
                allowed: false,
                reason: "feature_disabled",
                detail: "userTierOverride.enabled is false in evolve-tiers.json — " +
                    "operator has disabled all tier-control surfaces (chip + bot tool)",
            };
        }
        // Gate 2: per-role bot-initiated permission.
        if (!this._allowBotInitiated(role)) {
            return {
                allowed: false,
                reason: "bot_initiated_disabled",
                detail: `allowBotInitiated.${role} is false in evolve-tiers.json — ` +
                    `operator has forbidden bot self-escalation to '${role}' via ` +
                    `session_set_tier. Operator can still escalate via the admin-UI chip.`,
            };
        }
        // Gate 3: per-role daily cap. Cap of 0 is a valid "role disabled"
        // sentinel (per chip-path semantics).
        const cap = this._roleCap(role);
        const used = this._roleUsedToday(role);
        if (used >= cap) {
            return {
                allowed: false,
                reason: "daily_cap_exhausted",
                detail: `'${role}' daily cap reached (${used}/${cap} turns today). ` +
                    `Operator can raise the cap via roleCaps.${role}.maxPerDayPerBot in ` +
                    `evolve-tiers.json, or wait until pod-local midnight for the ` +
                    `counter to reset.`,
            };
        }
        return { allowed: true };
    }
    /**
     * Back-compat alias for the old single-tier gate. SetTierTool and any
     * un-migrated caller can keep calling this; it delegates to the
     * power-role gate.
     */
    canEscalateToTier1() {
        return this.canEscalateToRole("power");
    }
    /**
     * Degradation chain for a capped role (spec §max semantics #6):
     * max→power→standard, power→standard. Returns the next role to try.
     * `standard` (and any uncapped role) degrades to itself — the chain
     * terminates at the workhorse. Pure function; the caller re-checks
     * canEscalateToRole on the returned role.
     */
    degradeRoleOnCap(role) {
        if (role === "max")
            return "power";
        if (role === "power")
            return "standard";
        return "standard";
    }
    /**
     * Read the consent source for the active user-tier override, or null if
     * no override is set. Used by:
     *   - Cascade controller (Phase 2+) for de-escalation gating per spec § 2.2
     *   - Cascade telemetry to record consent_source on each span
     *
     * Defaults to "ui_chip" when not explicitly set (back-compat with the
     * pre-Phase-2 setUserTier calls from api_home_chat which doesn't pass
     * a consent_source).
     */
    getConsentSource(sessionKey) {
        return this.sessionConsentSources.get(sessionKey) ?? null;
    }
    /**
     * Read the active user-requested ROLE for a session, or null if no
     * override is set. Returns a role ID
     * ("fast" | "standard" | "power" | "max").
     */
    getUserRole(sessionKey) {
        return this.sessionUserTiers.get(sessionKey) ?? null;
    }
    /**
     * Read the active user-requested tier for a session in the cascade
     * controller's internal Tier form ("tier1" | "tier2" | "tier3"), or
     * null when there's no override OR the override is `max` (pull-only,
     * §max semantics #2 — the cascade never sees `max`, so a max-pinned
     * session reads as "no cascade-visible request"; the user-override
     * branch in resolveModelOverride already applied it above the cascade).
     *
     * Used by CascadeController's shadow-mode integration in TurnObserver.
     */
    getUserTier(sessionKey) {
        const choice = this.sessionUserTiers.get(sessionKey);
        if (!choice)
            return null;
        const t = this._roleToCascadeTier(choice);
        return t === "tier1" || t === "tier2" || t === "tier3" ? t : null;
    }
    // ── Phase 3 cascade routing ────────────────────────────────────────────
    /**
     * True when the operator has opted this bot in to cascade-driven
     * routing. Reads `config.cascade.enabled` (loaded from
     * `{shared}/{bot}/tiers.json::cascade.enabled`). Default false:
     * config-omitted bots stay on the classifier post-cutover until
     * the operator explicitly flips the flag per-bot.
     */
    isCascadeEnabled() {
        return this.config.cascade?.enabled === true;
    }
    /**
     * Store the cascade controller's verdict for application on the
     * NEXT turn of this session. Called by TurnObserver at the end of
     * each turn (in agent_end) after `cascadeController.decide()`.
     *
     * Pure write — no I/O, no routing side effects. The verdict is
     * applied only when `isCascadeEnabled()` is true AND the next
     * turn's `resolveModelOverride()` reaches the cascade branch (i.e.,
     * not pre-empted by runaway / spend-cap / user-override).
     *
     * Passing `null` clears the verdict (used in tests + on session end).
     */
    setCascadeVerdict(sessionKey, verdict, tsMs = Date.now()) {
        if (!sessionKey)
            return;
        if (verdict === null) {
            this.sessionCascadeVerdicts.delete(sessionKey);
            return;
        }
        this.sessionCascadeVerdicts.set(sessionKey, {
            tier: verdict.tier,
            decidedAt: tsMs,
        });
    }
    /**
     * Read the current cascade verdict for a session. Returns null
     * when no verdict has been recorded (e.g., before the first turn
     * completes) — caller should fall through to the classifier.
     */
    getCascadeVerdict(sessionKey) {
        const v = this.sessionCascadeVerdicts.get(sessionKey);
        return v ? { tier: v.tier } : null;
    }
    /**
     * Store a pre-flight intent router decision for application on THIS
     * turn (the one whose `before_model_resolve` hook just fired). Called
     * by TurnObserver after `PreflightIntentRouter.classify()`.
     *
     * Pure write. The decision is applied only when `_resolveModelAndTier`
     * reaches the productive/ambiguous branch AND no explicit operator /
     * user default has fired above it.
     *
     * Passing `null` clears the slot (used on session end + when the
     * router returns ABSTAIN, so we don't keep a stale decision around).
     *
     * Phase 1: this map stays empty in production because the router
     * always returns ABSTAIN (tier=null) and the caller doesn't store
     * abstains. The setter wiring exists so Phase 2/3+ are router-only
     * changes.
     */
    setSessionPreflightDecision(sessionKey, decision) {
        if (!sessionKey)
            return;
        if (decision === null) {
            this.sessionPreflightDecisions.delete(sessionKey);
            return;
        }
        this.sessionPreflightDecisions.set(sessionKey, {
            tier: decision.tier,
            reason: decision.reason,
        });
    }
    /**
     * Read the current pre-flight decision for a session, or null when
     * none was recorded. Used by `_resolveModelAndTier` to consult the
     * slot, and by TurnObserver to mirror the decision onto the cascade
     * span as `cascade.preflight.tier` + `cascade.preflight.reason`.
     */
    getSessionPreflightDecision(sessionKey) {
        const d = this.sessionPreflightDecisions.get(sessionKey);
        return d ? { tier: d.tier, reason: d.reason } : null;
    }
    /**
     * Read what drove the last `resolveModelOverride()` call for a
     * session. Returns null if no resolution has happened yet (or the
     * session has been cleared). Used by TurnObserver to set
     * `cascade.tier_chosen_by` on the telemetry span — when this
     * returns "cascade", the audit layer knows the controller drove
     * routing (vs. shadow mode where it would have but didn't).
     */
    getLastDecisionDriver(sessionKey) {
        return this.sessionLastDecisionDriver.get(sessionKey) ?? null;
    }
    /**
     * Called by TurnObserver when a session ends. Cleans up memory.
     */
    clearSession(sessionKey) {
        this.sessionTypes.delete(sessionKey);
        this.sessionUserTiers.delete(sessionKey);
        this.sessionConsentSources.delete(sessionKey);
        this.sessionUserKeys.delete(sessionKey);
        this.sessionCostHistory.delete(sessionKey);
        this.sessionRunawayTripped.delete(sessionKey);
        this.sessionCascadeVerdicts.delete(sessionKey);
        this.sessionPreflightDecisions.delete(sessionKey);
        this.sessionLastDecisionDriver.delete(sessionKey);
        this._lastResolvedRoleWasMax.delete(sessionKey);
        // Drop from the power-role in-process counter if the session was
        // currently power — and rewrite the watchdog's heartbeat file so
        // the watchdog sees the decrement on its next 60s poll.
        if (this._tier1ActiveSessions.delete(sessionKey)) {
            this._writeTier1ActiveFile();
        }
    }
    // ── tier1 in-process counter (pressure watchdog defense) ────────────────
    //
    // The watchdog at packages/analyzer/cascade/pressure_watchdog.py
    // reads {sharedDir}/{botId}/cascade/tier1_active.json every 60s
    // (function `read_in_process_tier1_counts`). The expected JSON
    // shape is `{"active_count": <int>, ...}` — additional fields are
    // ignored by the reader but useful for forensics.
    //
    // Atomicity: write to a sibling .tmp file then rename. The reader
    // catches partial reads via the json.JSONDecodeError except clause,
    // but rename is safer (no torn read) and is cheap on APFS.
    //
    // No throw guarantee: this is best-effort hot-path code. Any
    // filesystem error (no shared dir, read-only mount, ENOSPC) is
    // swallowed silently — the watchdog has its own fallback to
    // span-derived counts when the file is absent.
    /**
     * Update the in-process power-role set for a session, given the ROLE
     * the turn resolved to (or null when we fell through to bot-default).
     * Idempotent: a session that flips from power → power across turns
     * produces no I/O. Also bumps the per-day power/max counters on
     * transition into the respective role.
     *
     * The watchdog's tier1_active.json tracks the `power` role (formerly
     * tier1); the file name is kept for back-compat with the reader.
     */
    _markSessionTier(sessionKey, resolvedRole) {
        if (!sessionKey)
            return;
        const wasPower = this._tier1ActiveSessions.has(sessionKey);
        const isPower = resolvedRole === "power";
        // Per-day power-role turn counter — bumped on every TRANSITION INTO
        // power (not on power→power carryover, which would over-count a
        // multi-turn session). canEscalateToRole("power") reads this against
        // roleCaps.power.maxPerDayPerBot. Reset at pod-local midnight.
        if (isPower && !wasPower) {
            const dayIso = localDateYMD();
            if (this._tier1CallsToday.dayIso !== dayIso) {
                this._tier1CallsToday = { count: 0, dayIso };
            }
            this._tier1CallsToday.count += 1;
            // Mirror to the disk-backed counter the admin-UI chip gate reads.
            this._appendTierUsageRecord("power", this.resolveRoleToModel("power"));
        }
        // Per-day max-role turn counter — same transition-edge logic, keyed
        // off whether THIS turn is max and the prior resolution wasn't.
        if (resolvedRole === "max") {
            const wasMax = this._lastResolvedRoleWasMax.has(sessionKey);
            if (!wasMax) {
                const dayIso = localDateYMD();
                if (this._maxCallsToday.dayIso !== dayIso) {
                    this._maxCallsToday = { count: 0, dayIso };
                }
                this._maxCallsToday.count += 1;
                this._lastResolvedRoleWasMax.add(sessionKey);
                // Mirror to the disk-backed counter the admin-UI chip gate reads.
                this._appendTierUsageRecord("max", this.resolveRoleToModel("max"));
            }
        }
        else {
            this._lastResolvedRoleWasMax.delete(sessionKey);
        }
        if (isPower === wasPower)
            return; // no change → no I/O
        if (isPower) {
            this._tier1ActiveSessions.add(sessionKey);
        }
        else {
            this._tier1ActiveSessions.delete(sessionKey);
        }
        this._writeTier1ActiveFile();
    }
    // Tracks sessions whose most-recent resolution was the `max` role, so
    // _markSessionTier bumps the per-day max counter only on a transition
    // INTO max (mirrors the power tracker's edge semantics). Cleared on
    // session end.
    _lastResolvedRoleWasMax = new Set();
    /**
     * Atomically write the current tier1-active count to
     * {sharedDir}/{botId}/cascade/tier1_active.json. Best-effort: any
     * filesystem error is swallowed. Logs a hint via the silent fail-
     * open path so a misconfigured sharedDir/botId is recoverable on
     * the next call.
     *
     * Made `protected` so test subclasses can override and assert on
     * call frequency without needing real filesystem state.
     */
    _writeTier1ActiveFile() {
        if (!this.sharedDir || !this.botId)
            return;
        try {
            const dir = path.join(this.sharedDir, this.botId, "cascade");
            fs.mkdirSync(dir, { recursive: true });
            const filepath = path.join(dir, "tier1_active.json");
            const tmpPath = `${filepath}.tmp.${process.pid}`;
            const payload = {
                active_count: this._tier1ActiveSessions.size,
                updated_at: new Date().toISOString(),
                // Identifying fields for forensics — the watchdog ignores
                // them but a crashed prior process's stale file is easy to
                // recognize: the pid won't be live anymore.
                pid: process.pid,
                bot_id: this.botId,
            };
            fs.writeFileSync(tmpPath, JSON.stringify(payload));
            fs.renameSync(tmpPath, filepath);
        }
        catch (err) {
            // First-occurrence EACCES gets a warn — this surfaces the
            // "bot user lacks write on /Users/Shared/evolve/<bot>/cascade/"
            // class of bug that silently dropped Phase 2's tier1 counter
            // pre-2026-05-28. fix_shared_dir_permissions() pre-creates the
            // dir with the right ownership; if the warn fires, that helper
            // didn't run for this bot.
            //
            // Anything else stays at the debug-only "fail open" path —
            // missing/stale file is the documented "telemetry-partially-
            // lost" degradation.
            if (err?.code === "EACCES" && !this._tier1ActiveWarnedEACCES) {
                this._tier1ActiveWarnedEACCES = true;
                try {
                    // Logger may not exist on raw ModelRouter — best-effort.
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    this.logger?.warn?.(`ModelRouter: cannot write tier1_active.json under ` +
                        `${this.sharedDir}/${this.botId}/cascade/; bot user lacks ` +
                        `write on the parent. Run 'sudo evolve-admin deploy ` +
                        `${this.botId}' on the mini.`);
                }
                catch {
                    /* swallow */
                }
            }
        }
    }
    // First-EACCES guard for the warn above. Promoted to a field so the
    // best-effort log path doesn't re-fire every turn.
    _tier1ActiveWarnedEACCES = false;
    /**
     * Reset the tier1 file to active_count=0 once on plugin startup.
     * If a prior plugin process crashed mid-flight, its stale file
     * could be permanently reporting an inflated count to the watchdog
     * (the spec's intra-process counter has no PID-aliveness check on
     * the reader side, by design — keeps the reader trivial). Calling
     * this from reloadConfig closes the gap: the FIRST reload after
     * startup wipes the stale value.
     *
     * Idempotent — only writes the first time per process via the
     * `_tier1ActiveFileInitialized` flag.
     */
    _clearTier1ActiveFileOnce() {
        if (this._tier1ActiveFileInitialized)
            return;
        if (!this.sharedDir || !this.botId)
            return;
        this._tier1ActiveFileInitialized = true;
        this._writeTier1ActiveFile();
    }
    // ── Disk-backed per-day role counter (chip-path convergence) ────────────
    //
    // The admin-UI chip gate (home_chat_routes.py) reads the disk counter via
    // models.get_tier_usage_today, counting JSONL records under
    //   {sharedDir}/cost/tier-usage/{botId}/{YYYY-MM-DD}.jsonl
    // by their `tier` field. The server queries tier="max" for the Max cap and
    // tier="tier1" for the Power cap. Until now the SOLE Python writer
    // (models.record_tier_usage) had ZERO callers — the disk cap was dead
    // theatre and only the plugin's in-memory _maxCallsToday/_tier1CallsToday
    // enforced anything (and reset on plugin restart). We converge both paths
    // on this disk counter: the plugin appends here on every transition into
    // power/max, and seeds its in-memory counters from this file at boot
    // (_seedRoleCountersFromDisk) so a plugin restart no longer zeroes the cap.
    //
    // Record schema MUST match models.get_tier_usage_today's parser
    // (packages/analyzer/models.py:500-559): one JSON object per line with a
    // `tier` field the reader counts. The date in the filename uses
    // localDateYMD() so it agrees with the Python reader's
    // datetime.now().strftime("%Y-%m-%d") (both pod-local; same host) — see
    // the localDateYMD doc for why UTC would roll over at the wrong hour.
    //
    // No-throw guarantee: this is hot-path code on a user turn. Any FS error
    // is swallowed, but the FIRST one logs LOUDLY (warn) so a cap silently
    // failing to record is not invisible drift — the server-side gate would
    // otherwise let elevated turns through forever.
    /** Map a capped role to the `tier` value the Python reader counts. */
    _roleToDiskTierField(role) {
        // power → "tier1" (server queries get_tier_usage_today(tier="tier1"));
        // max   → "max"   (server queries get_tier_usage_today(tier="max")).
        return role === "power" ? "tier1" : "max";
    }
    /** Path to today's tier-usage JSONL for this bot. */
    _tierUsageLogPath() {
        return path.join(this.sharedDir, "cost", "tier-usage", this.botId, `${localDateYMD()}.jsonl`);
    }
    /**
     * Append one tier-usage record to today's JSONL for a transition into
     * `role`. Best-effort + no-throw; first failure logs a warn so the
     * cap's disk counter silently failing to record surfaces.
     */
    _appendTierUsageRecord(role, model) {
        if (!this.sharedDir || !this.botId)
            return;
        try {
            const dir = path.join(this.sharedDir, "cost", "tier-usage", this.botId);
            fs.mkdirSync(dir, { recursive: true });
            const record = JSON.stringify({
                ts: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
                tier: this._roleToDiskTierField(role),
                model: model ?? "",
                context: "plugin_session_tier",
                bot_id: this.botId,
            });
            // Append is atomic enough for single-line JSONL on APFS; the reader
            // tolerates a torn final line via its per-line try/except.
            fs.appendFileSync(this._tierUsageLogPath(), record + "\n");
        }
        catch (err) {
            if (!this._tierUsageWarned) {
                this._tierUsageWarned = true;
                try {
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    this.logger?.warn?.(`ModelRouter: FAILED to append tier-usage record under ` +
                        `${this.sharedDir}/cost/tier-usage/${this.botId}/; the admin-UI ` +
                        `daily cap for '${role}' will UNDER-count and may never fire. ` +
                        `code=${err?.code ?? "?"} — run 'sudo evolve-admin deploy ` +
                        `${this.botId}' to fix shared-dir permissions.`);
                }
                catch {
                    /* swallow */
                }
            }
        }
    }
    // First-failure guard for the warn above (one warn per process, not per turn).
    _tierUsageWarned = false;
    /**
     * Seed _tier1CallsToday / _maxCallsToday from today's on-disk JSONL at
     * boot so a plugin restart doesn't reset the daily cap to zero. Counts
     * records by their `tier` field (tier1 → power, max → max), matching the
     * server-side reader. Tolerates a missing/unreadable file (count stays 0)
     * and a torn final line (per-line parse, skip on error). No-throw.
     */
    _seedRoleCountersFromDisk() {
        if (!this.sharedDir || !this.botId)
            return;
        const dayIso = localDateYMD();
        let tier1 = 0;
        let max = 0;
        try {
            const raw = fs.readFileSync(this._tierUsageLogPath(), "utf8");
            for (const line of raw.split("\n")) {
                if (!line)
                    continue;
                let rec;
                try {
                    rec = JSON.parse(line);
                }
                catch {
                    continue; // skip torn/partial line
                }
                const t = rec?.tier;
                if (t === "tier1")
                    tier1 += 1;
                else if (t === "max")
                    max += 1;
            }
        }
        catch {
            // Missing or unreadable file → counts stay 0. This is the normal
            // first-run-of-the-day path, not an error.
            return;
        }
        this._tier1CallsToday = { count: tier1, dayIso };
        this._maxCallsToday = { count: max, dayIso };
    }
    // ── Runaway-rate hard cap ────────────────────────────────────────────────
    //
    // Per spec § 2.6: per-session $/window safety net for catching runaway
    // loops. Different from monthly budget (steady-state) and daily_cap_usd
    // (daily emergency). Designed to catch *broken behavior* — a session
    // burning $20 in 5 minutes is almost always a thrashing loop, not a
    // user doing intentional expensive work.
    /**
     * Record cost incurred by a turn in the given session. Called by
     * TurnObserver after each agent_end completes. Bounded memory: old
     * entries outside the window are pruned on each call.
     */
    recordTurnCost(sessionKey, costUsd, tsMs = Date.now()) {
        // Reject NaN explicitly — typeof NaN === "number" but Number.isFinite catches it.
        if (typeof costUsd !== "number" || !Number.isFinite(costUsd) || costUsd <= 0)
            return;
        if (!sessionKey)
            return;
        const cfg = this.config.runawayRateCap ?? {};
        if (cfg.enabled === false)
            return;
        const windowMs = (cfg.windowMinutes ?? 5) * 60 * 1000;
        const cutoff = tsMs - windowMs;
        const history = this.sessionCostHistory.get(sessionKey) ?? [];
        // Prune entries outside the window. Cheap (history is bounded by
        // turn rate × window — single-digit entries in practice).
        const pruned = history.filter((e) => e.ts >= cutoff);
        pruned.push({ ts: tsMs, cost: costUsd });
        this.sessionCostHistory.set(sessionKey, pruned);
    }
    /**
     * Check whether the session's rolling-window spend has exceeded the
     * runaway-rate cap. Returns `{tripped: true, ...}` when it has, with
     * the suggested Signal severity (escalates to CRITICAL after N trips
     * in 24h).
     *
     * Pure function over the in-memory cost history — no I/O.
     */
    checkRunawayRate(sessionKey, tsMs = Date.now()) {
        const cfg = this.config.runawayRateCap ?? {};
        if (cfg.enabled === false)
            return { tripped: false, totalUsd: 0 };
        const windowMs = (cfg.windowMinutes ?? 5) * 60 * 1000;
        const threshold = cfg.dollarsPerWindow ?? 20.0;
        const cutoff = tsMs - windowMs;
        const history = this.sessionCostHistory.get(sessionKey) ?? [];
        const inWindow = history.filter((e) => e.ts >= cutoff);
        const total = inWindow.reduce((s, e) => s + e.cost, 0);
        if (total <= threshold)
            return { tripped: false, totalUsd: total };
        // Mark this session as tripped (sticky) — once tripped, every
        // remaining turn forces tier3. Bot can't recover from a trip;
        // operator clears by ending the session.
        if (!this.sessionRunawayTripped.has(sessionKey)) {
            this.sessionRunawayTripped.set(sessionKey, 1);
            // Account toward pod-wide trip count for severity escalation.
            const dayIso = new Date(tsMs).toISOString().slice(0, 10);
            if (this._runawayTripsToday.dayIso !== dayIso) {
                this._runawayTripsToday = { count: 0, dayIso };
            }
            this._runawayTripsToday.count += 1;
        }
        const tripsToday = this._runawayTripsToday.count;
        const criticalThreshold = cfg.criticalTripsPer24h ?? 3;
        const severity = tripsToday >= criticalThreshold ? "critical" : "warning";
        return { tripped: true, totalUsd: total, severity, tripsToday };
    }
    /**
     * True if the session has been tripped by runaway-rate at any point
     * during its lifetime. Sticky — once tripped, stays tripped until
     * the session ends (clearSession).
     */
    isRunawayTripped(sessionKey) {
        return this.sessionRunawayTripped.has(sessionKey);
    }
    /**
     * Returns true if the bot is currently forced to tier3 by either:
     *   - the per-session runaway-rate hard cap (sticky once tripped), OR
     *   - today's daily spend-cap flag (downgrade-tier action).
     *
     * Exposed for callers that need to know about the forced state outside
     * of the resolveModelOverride hot path — chiefly the cascade telemetry
     * span, which records `tier_chosen_by="spend_cap"` when forced. Without
     * this, the audit-layer Labeler (Signal #1 — UI-chip override) sees
     * every span as `tier_chosen_by="classifier"` even when a cap is in
     * play, defeating ground-truth attribution.
     */
    isSpendCapForced(sessionKey) {
        if (this.isRunawayTripped(sessionKey))
            return true;
        if (this.sharedDir && this.botId && isSpendCapActive(this.sharedDir, this.botId)) {
            return true;
        }
        return false;
    }
    /**
     * Returns model override for this session, or null if no override needed.
     * This is called from the before_model_resolve hook handler.
     *
     * Returns null (no override) when:
     * - routing is disabled in config
     * - session type is 'productive' or 'ambiguous'
     * - session type is unknown
     * - target tier has no models configured
     *
     * Hard-spend-cap enforcement: if a downgrade-tier cap flag is active for
     * this bot today, ALL sessions are forced to tier3 regardless of type.
     */
    resolveModelOverride(sessionKey) {
        // Compute the model and the tier we explicitly chose (or null if
        // we fell through to bot-default), then update the tier1 in-process
        // counter, then return. Routing decisions other than ours (bot
        // defaults from OC's own resolution path) are captured by spans
        // via getTierForModel; the in-process counter only tracks the
        // grants ModelRouter itself authorized.
        const [model, chosenTierKey] = this._resolveModelAndTier(sessionKey);
        this._markSessionTier(sessionKey, chosenTierKey);
        return model;
    }
    /**
     * Internal — same logic as resolveModelOverride but ALSO returns the
     * ROLE we picked (or null when we fell through to bot-default). Split
     * out so the public hot-path function can write the power-role counter
     * as a side-effect without duplicating branch logic.
     */
    _resolveModelAndTier(sessionKey) {
        // Fail open: routing explicitly disabled
        if (this.config.routing?.enabled === false)
            return [null, null];
        // Precedence (highest → lowest) — spec §Routing precedence:
        //   0. Runaway-rate hard cap (per-session) — force `fast`
        //   1. Spend-cap safety net  — force `fast` regardless of intent
        //   2. User role choice      — fast|standard|power|max (explicit pull)
        //   3. Cascade controller    — may reach `power`, NEVER `max`
        //   4. Classifier branch (productive/ambiguous only):
        //     4a. Per-user / operator default role (explicit intent)
        //     4b. Pre-flight intent router (per-turn prediction, Phase 1+)
        //     4c. Fall through to bot default (return null, OC picks)
        //   5. Classifier maintenance/background → maintenance/background role
        // Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Routing
        // precedence + docs/spec-user-tier-control-2026-05-26.md.
        // 0. Runaway-rate hard cap — once a session has tripped, every
        //    subsequent turn is forced to `fast` regardless of any state.
        if (this.isRunawayTripped(sessionKey)) {
            this.sessionLastDecisionDriver.set(sessionKey, "runaway");
            return [this._safetyNetDowngradeModel("runaway"), "fast"];
        }
        // 1. Hard spend cap: force `fast` for all sessions if cap flag active.
        if (this.sharedDir && this.botId && isSpendCapActive(this.sharedDir, this.botId)) {
            this.sessionLastDecisionDriver.set(sessionKey, "spend_cap");
            return [this._safetyNetDowngradeModel("spend_cap"), "fast"];
        }
        // 2. User role choice — beats classification, ignored only by the
        //    safety nets above and by ctx.userModelOverride (checked in
        //    TurnObserver before this method runs). `max` is reachable ONLY
        //    here (pull-only). Always emit the chosen role's model so a bot
        //    whose default role is `fast` (forge etc.) still honors a
        //    "Standard"/"Power"/"Max" pick rather than silently no-op'ing.
        //
        //    Cap enforcement + degradation (§max semantics #6) happens at the
        //    DECISION points that set the override (SetTierTool /
        //    canEscalateToRole + degradeRoleOnCap, and the admin-UI chip
        //    path's own disk-backed counter), NOT here. By the time a role is
        //    pinned on sessionUserTiers it has already cleared the gate, so
        //    the routing hot path honors it directly — keeping the in-process
        //    counter a faithful record of actual power/max routings.
        const userRole = this.sessionUserTiers.get(sessionKey);
        if (userRole) {
            this.sessionLastDecisionDriver.set(sessionKey, "user_request");
            // Availability-aware (spec §Addendum3.A): a forced pull to a role whose
            // rung has no credentialed provider degrades DOWN the same ladder as a
            // cap hit, honestly — never a working-looking pull that dies at the
            // provider. The resolved (possibly-degraded) role is returned so the
            // chip/telemetry reflect what actually ran.
            const a = this.resolveRoleAvailability(userRole);
            if (a.model)
                return [a.model, a.resolvedRole ?? userRole];
            // Nothing in the ladder is available — emit the configured model[0] so
            // OC still routes (legacy behavior); telemetry already stamped the
            // user_request driver. Returning null would drop the turn silently.
            return [this.resolveRoleToModel(userRole), userRole];
        }
        // 3. Cascade controller (Phase 3 cutover, gated on cascade.enabled).
        //    The cascade's verdict is in its internal Tier form (tier1..tier3);
        //    it can never be tier0/tier-above-power, so it maps to fast |
        //    standard | power and NEVER `max` (spec §max semantics #2 —
        //    pull-only). Verdict was stashed at the end of the prior turn.
        if (this.isCascadeEnabled()) {
            const verdict = this.sessionCascadeVerdicts.get(sessionKey);
            if (verdict) {
                const role = this._cascadeTierToRole(verdict.tier);
                // Defensive: a cascade verdict that maps to a non-classifier
                // role (only possible via tier0→judge, which the controller
                // never picks) is dropped to standard rather than routed.
                const safeRole = role && _CLASSIFIER_ROLES.has(role) ? role : "standard";
                this.sessionLastDecisionDriver.set(sessionKey, "cascade");
                return [this.resolveRoleToModel(safeRole), safeRole];
            }
            // No verdict yet — fall through to classifier.
        }
        // 4. Classifier-driven routing.
        const sessionType = this.sessionTypes.get(sessionKey);
        if (!sessionType || sessionType === "productive" || sessionType === "ambiguous") {
            // 4a. Per-user default (Phase C) + operator default (Phase A).
            //     _resolveOperatorDefaultRole checks the per-user pref FIRST
            //     (which MAY be `max`), then the operator bot-wide default
            //     (which may NOT be `max` — pull-only classifier role).
            const [opModel, opRole] = this._resolveOperatorDefaultRole(sessionKey);
            if (opModel) {
                const driver = this._lastResolvedDefaultSource === "user"
                    ? "user_default"
                    : "operator_default";
                this.sessionLastDecisionDriver.set(sessionKey, driver);
                return [opModel, opRole];
            }
            // 4b. Pre-flight intent router (Phase 1+). Phase 1: always ABSTAIN
            //     (no slot entry stored), so dead in production until Phase 2.
            const preflight = this.sessionPreflightDecisions.get(sessionKey);
            if (preflight) {
                const role = this._cascadeTierToRole(preflight.tier);
                const safeRole = role && _CLASSIFIER_ROLES.has(role) ? role : "standard";
                this.sessionLastDecisionDriver.set(sessionKey, "preflight");
                return [this.resolveRoleToModel(safeRole), safeRole];
            }
            // Fell through to bot default. ModelRouter didn't decide; returning
            // null drops the session from the power-role set (no longer
            // actively routed to power by us). Spans cover the "bot-default IS
            // power" case via getRoleForModel(llm.model).
            this.sessionLastDecisionDriver.set(sessionKey, "classifier");
            return [null, null];
        }
        // maintenance or background → configured classifier role (default fast).
        // The role is validated against {fast, standard, power} (max is
        // pull-only); a misconfigured `max`/unknown falls back to `fast`.
        const configuredRole = sessionType === "background"
            ? (this.config.routing?.backgroundRole ?? "fast")
            : (this.config.routing?.maintenanceRole ?? "fast");
        const role = _CLASSIFIER_ROLES.has(configuredRole) ? configuredRole : "fast";
        this.sessionLastDecisionDriver.set(sessionKey, "classifier");
        return [this.resolveRoleToModel(role), role];
    }
    /**
     * Force the `fast` role for this turn regardless of session
     * classification. Used by the `evo` keyword surface: every dispatched
     * evo turn is either a stay-silent ack or a verbatim echo of
     * dispatcher-supplied content — neither justifies the bot's default
     * (Sonnet/Opus) model.
     *
     * Returns the first model in the `fast` rung, or null when:
     *   - routing is disabled in config
     *   - the `fast` role is unconfigured (legacy pod without tiers.json)
     */
    resolveFastRoleOverride() {
        if (this.config.routing?.enabled === false)
            return null;
        return this.resolveRoleToModel("fast");
    }
    /**
     * Back-compat alias for resolveFastRoleOverride — kept for the
     * `evo`-keyword caller in TurnObserver until it is renamed.
     */
    resolveTier3Override() {
        return this.resolveFastRoleOverride();
    }
    /**
     * Returns authProfileOverride for this session, or null if no override.
     * Only returns a value if account routing is enabled AND session type
     * matches a configured account tier AND at least one profile in that tier
     * has a valid key in auth-profiles.json.
     *
     * Profile fallback within a tier: profiles[] is tried in order; the first
     * profile that has a non-empty key/token in auth-profiles.json is returned.
     * If NO profile in the matching tier has a valid key, returns null — causing
     * openclaw to use its default auth profile (the API key) rather than failing
     * the model call and cascading to the next model in the fallback list.
     */
    resolveAuthProfileOverride(sessionKey) {
        if (!this.config.accountRouting?.enabled)
            return null;
        if (!this.config.accountTiers)
            return null;
        const sessionType = this.sessionTypes.get(sessionKey);
        if (!sessionType)
            return null;
        for (const [, tier] of Object.entries(this.config.accountTiers)) {
            if (tier.for_session_types.includes(sessionType)) {
                // Try each profile in order — return first one with a valid key
                for (const profileId of tier.profiles) {
                    if (this._profileHasKey(profileId)) {
                        return profileId;
                    }
                }
                // No profile in this tier has a valid key — don't override.
                // Openclaw will use the default API key profile for the same model,
                // which is correct: we want auth fallback, not model fallback.
                return null;
            }
        }
        return null;
    }
    /**
     * Re-reads config in place.
     *
     * Priority:
     *   1. Explicit tiersJsonPath arg (test/programmatic override)
     *   2. ~/.openclaw/evolve-tiers.json — admin UI canonical location
     *   3. {sharedDir}/{botId}/tiers.json — legacy / hand-rolled fallback
     *   4. network.json models.*         — pod-wide fallback
     *   5. Keep existing config on any failure — always fail open
     */
    reloadConfig(networkPath, tiersJsonPath, sharedDir, botId) {
        try {
            let tiersFile = {};
            let network = {};
            // Explicit override always wins (tests, programmatic reloads).
            if (tiersJsonPath) {
                try {
                    tiersFile = JSON.parse(fs.readFileSync(tiersJsonPath, "utf8"));
                }
                catch { /* ok */ }
            }
            // Otherwise consult the same lookup order as the public load path,
            // so a reload and a fresh construction see the same effective config.
            if (Object.keys(tiersFile).length === 0) {
                const effectiveShared = sharedDir ?? this.sharedDir ?? "";
                const effectiveBotId = botId ?? this.botId ?? "";
                tiersFile = loadTiersFile(effectiveShared, effectiveBotId);
            }
            try {
                network = JSON.parse(fs.readFileSync(networkPath, "utf8"));
            }
            catch { /* ok */ }
            // Rungs/roles source: KEYED merge of the pod-base catalog
            // (network.models) with the per-bot override (tiersFile) — rungs by id,
            // roles/roleCaps by key (spec §Addendum A.4). Block-precedence here made
            // a pod-wide adoption invisible because every bot carries per-bot rungs.
            // synthesizeRungsRoles accepts both the new {rungs,roles} shape and the
            // legacy {tiers:{tierN}} shape (fail-open back-compat).
            const modelsSource = mergeModelCatalog(network.models ?? {}, tiersFile);
            const synthesized = synthesizeRungsRoles(modelsSource);
            const rawRouting = tiersFile.routing ?? network.models?.routing ?? { enabled: true };
            this.config = {
                rungs: synthesized.rungs,
                roles: synthesized.roles,
                roleCaps: this._mergeRoleCaps(synthesized.roleCaps, network.models?.roleCaps, tiersFile.userTierOverride ?? network.userTierOverride),
                routing: _normalizeRouting(rawRouting),
                accountTiers: network.accounts?.tiers ?? {},
                accountRouting: network.accounts?.routing ?? { enabled: false },
                // Preserve runaway-rate cap across reloads (spec § 2.6). Without
                // this fallback to the prior in-memory value, a tiers.json reload
                // that doesn't carry the block would silently drop the cap.
                runawayRateCap: tiersFile.runawayRateCap
                    ?? network.runawayRateCap
                    ?? this.config?.runawayRateCap,
                // Phase 3 cascade-routing flag. Read from tiers.json so the
                // operator can flip per-bot without a deploy. Default false:
                // an absent flag stays on the classifier (safe rollback path
                // is just removing the key).
                cascade: tiersFile.cascade ?? this.config?.cascade,
                // Operator-controlled per-bot defaults (audit #69 Phase A):
                // enabled / allowBotInitiated (now per-role) / defaultRole.
                // Preserve across reloads so a partial tiers.json (missing block)
                // doesn't silently clear the operator's picker setting. Legacy
                // dailyCap inside this block is folded into roleCaps.power above.
                userTierOverride: tiersFile.userTierOverride ?? this.config?.userTierOverride,
                // Per-user-per-bot tier prefs (audit #69 Phase C). Re-read on
                // every reload — the admin handler writes this file directly,
                // so reload is when the plugin picks up new entries without a
                // gateway restart. Falls back to the previous in-memory copy
                // when the file is briefly unreadable (mid-rename, ACL drift)
                // so we never silently zero out the user's prefs.
                userTierPrefs: (() => {
                    const effectiveShared = sharedDir ?? this.sharedDir ?? "";
                    const effectiveBotId = botId ?? this.botId ?? "";
                    const fresh = loadUserTierPrefsFile(effectiveShared, effectiveBotId);
                    if (fresh.users && Object.keys(fresh.users).length > 0)
                        return fresh;
                    // Empty result on disk → could be genuine "no users have set
                    // a pref yet" OR a transient read failure. Prefer the previous
                    // in-memory value if we had one, falling back to the empty.
                    return this.config?.userTierPrefs ?? fresh;
                })(),
            };
            // Update sharedDir/botId if provided (needed for spend-cap flag checks)
            if (sharedDir)
                this.sharedDir = sharedDir;
            if (botId)
                this.botId = botId;
            // Fallback: derive sharedDir from network.json if not set
            if (!this.sharedDir && network.sharedDir) {
                this.sharedDir = network.sharedDir;
            }
            // First reload after construction → wipe any stale
            // tier1_active.json from a prior plugin process. The watchdog
            // has no PID-aliveness check on its read path (kept trivial by
            // design); without this, a crashed prior process's stale count
            // would inflate the watchdog's pod-wide tier1 reading forever.
            // Idempotent — only writes the first time per process.
            this._clearTier1ActiveFileOnce();
        }
        catch {
            // Keep existing config on reload failure — fail open
        }
    }
    /**
     * Get routing status for diagnostics/UI.
     */
    getRoutingStatus() {
        return {
            enabled: this.config.routing?.enabled !== false,
            accountRoutingEnabled: this.config.accountRouting?.enabled === true,
            sessions: Object.fromEntries(this.sessionTypes),
            userTiers: Object.fromEntries(this.sessionUserTiers),
        };
    }
}
//# sourceMappingURL=ModelRouter.js.map