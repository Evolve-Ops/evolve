/**
 * routingRule — the rule that decides which rung answers a turn, from
 * what is already known about the turn.
 *
 * Decision: internal/decision-evolve-overhead-2026-09-07.md D-OH2.
 *
 * Before this module, "which model answers" was decided by a small
 * committee of LLM calls that ran AROUND every real turn — a preflight
 * haiku classifier, a post-turn tier classifier, and a per-session
 * struggle judge. Two days of turns (decision §2) put the entire
 * measurable benefit of that machinery at ~$3-4/day pod-wide, and every
 * dollar of it came from moving heartbeats and cron to the cheap rung —
 * turns that are identifiable by their TRIGGER, with no model call at
 * all. One of those classifiers then recursed into ~3,000 calls in an
 * afternoon (finding-tier-router-self-call-loop-2026-09-07.md).
 *
 * So the rule keeps the outcome and drops the machinery. It decides
 * from three free inputs:
 *
 *   1. trigger kind   — heartbeat / cron / in-session scaffolding are
 *                       clock- or system-fired and route to the cheap
 *                       rung. This is the row that carries the savings.
 *   2. the bot's learned prior — computed OFFLINE by the nightly
 *                       analyzer job (``preflight_prior.py``) from the
 *                       last 14 days of that bot's own turns. Consulted
 *                       only for user turns, only when confident.
 *   3. nothing else   — a user turn with no confident prior falls
 *                       through to the bot's primary, which is what
 *                       happens today.
 *
 * STANDING RULE: nothing here changes which model answers. The rule
 * reproduces the decisions the classifier ladder produces today; the
 * replay test (tests/routingReplay.test.mjs) is the proof, and it runs
 * over a fixture of real turn shapes.
 *
 * What the rule does NOT decide, and never will: operator tier
 * preference and the cost breakers sit ABOVE it in
 * ``ModelRouter._resolveModelAndTier`` and are untouched. The rule only
 * supplies the anchor the classifier used to supply.
 *
 * Pure. No I/O, no clock, no randomness — every input is a parameter, so
 * the replay test can run it over recorded turns.
 */
/** Default confidence a learned prior must clear before it may route. */
export const DEFAULT_PRIOR_MIN_CONFIDENCE = 0.8;
const VALID_ROLES = new Set(["fast", "standard", "power", "max"]);
/**
 * Roles the RULE is allowed to pick. ``max`` is pull-only — reachable
 * by an explicit operator/user request and never by a rule, a prior, or
 * a classifier (spec-model-rungs-and-roles §max semantics). A prior that
 * somehow named ``max`` is refused rather than honoured.
 */
const RULE_SELECTABLE_ROLES = new Set(["fast", "standard", "power"]);
/**
 * Role → cascade tier. Mirrors ``ModelRouter._roleToCascadeTier``,
 * including its deliberate omission of ``max`` — the cascade tier
 * vocabulary stops at tier1 and ``max`` is pull-only, so it maps to
 * nothing. Unreachable from the rule; present so a future caller that
 * hands us a ``max`` prior gets a null tier rather than a wrong one.
 */
const ROLE_TO_TIER = {
    fast: "tier3",
    standard: "tier2",
    power: "tier1",
};
/**
 * Map a turn's trigger_kind to the session class the rule anchors.
 *
 * This is the ONE mapping — ``TurnObserver._triggerKindToSessionClass``
 * delegates here so the hot path and the replay test cannot drift.
 *
 *   - heartbeat / cron_app             → background   (clock-fired work)
 *   - subagent / summarizer /
 *     classifier / task_extractor /
 *     fallback                         → maintenance  (in-session scaffolding)
 *   - user_turn / unknown              → null         (a person is waiting;
 *                                                      the rule does not
 *                                                      anchor, the prior may)
 *
 * Keep aligned with ``cost_event_converter.py``'s trigger_kind taxonomy
 * and ``tile_metrics._SCHEDULED_KINDS`` — plugin, rollup and dashboard
 * must agree on what "background" means or the operator reads three
 * different stories off one pod.
 */
export function triggerKindToSessionClass(triggerKind) {
    switch (triggerKind) {
        case "heartbeat":
        case "cron_app":
            return "background";
        case "subagent":
        case "summarizer":
        case "classifier":
        case "task_extractor":
        case "fallback":
            return "maintenance";
        default:
            return null;
    }
}
function coerceRole(raw, fallback) {
    return typeof raw === "string" && VALID_ROLES.has(raw) ? raw : fallback;
}
/**
 * Read the prior that applies to this surface, or null when the prior
 * is absent, unconfident, or names a role the rule may not pick.
 *
 * Surface match is exact on the lowercased channel — we do not guess at
 * near-misses, because a prior applied to the wrong surface is a silent
 * routing change and the standing rule forbids those.
 */
export function priorRoleForSurface(prior, surface, minConfidence = DEFAULT_PRIOR_MIN_CONFIDENCE) {
    if (!prior)
        return null;
    const confidence = typeof prior.confidence === "number" ? prior.confidence : 0;
    if (!(confidence >= minConfidence))
        return null;
    const key = typeof surface === "string" ? surface.trim().toLowerCase() : "";
    if (key && prior.surfaces && typeof prior.surfaces === "object") {
        const perSurface = prior.surfaces[key];
        if (typeof perSurface === "string" && RULE_SELECTABLE_ROLES.has(perSurface)) {
            return { role: perSurface, scope: "surface" };
        }
        // A surface entry naming a role the rule may not pick (``max``, or
        // junk) is dropped, and we fall back to the bot-wide prior rather
        // than refusing outright — the bot-wide value was computed from a
        // superset of the same evidence.
    }
    if (typeof prior.role === "string" && RULE_SELECTABLE_ROLES.has(prior.role)) {
        return { role: prior.role, scope: "bot" };
    }
    return null;
}
/**
 * Decide the rung for one turn.
 *
 * Precedence inside the rule (the safety nets and the operator's own
 * tier preference sit above it, in ``_resolveModelAndTier``):
 *
 *   1. trigger kind — heartbeat / cron / in-session scaffolding go to
 *      the operator's configured background/maintenance role (default
 *      ``fast``). This is the whole savings, and it costs nothing.
 *   2. learned prior — a user turn on a bot with a confident prior for
 *      this surface routes to that rung.
 *   3. otherwise — the bot's primary. Returning ``role: null`` means
 *      "we did not decide", and the ladder falls through exactly as it
 *      does today.
 */
export function decideRoutingRule(input) {
    const triggerKind = typeof input?.triggerKind === "string" ? input.triggerKind : "unknown";
    const sessionClass = triggerKindToSessionClass(triggerKind);
    if (sessionClass) {
        const configured = sessionClass === "background" ? input.backgroundRole : input.maintenanceRole;
        // `max` is pull-only; a misconfigured `max`/unknown lands on `fast`,
        // the same clamp ``_resolveModelAndTier`` applies to the configured
        // classifier role today.
        const role = RULE_SELECTABLE_ROLES.has(String(configured))
            ? coerceRole(configured, "fast")
            : "fast";
        return {
            sessionClass,
            role,
            tier: ROLE_TO_TIER[role] ?? null,
            driver: "trigger",
            reason: `trigger:${triggerKind}`,
        };
    }
    const prior = priorRoleForSurface(input.prior ?? null, input.surface ?? null, typeof input.priorMinConfidence === "number"
        ? input.priorMinConfidence
        : DEFAULT_PRIOR_MIN_CONFIDENCE);
    if (prior) {
        return {
            sessionClass: null,
            role: prior.role,
            tier: ROLE_TO_TIER[prior.role] ?? null,
            driver: "bot_prior",
            reason: `bot_prior:${prior.scope}`,
        };
    }
    return {
        sessionClass: null,
        role: null,
        tier: null,
        driver: "primary",
        reason: `primary:${triggerKind}`,
    };
}
//# sourceMappingURL=routingRule.js.map